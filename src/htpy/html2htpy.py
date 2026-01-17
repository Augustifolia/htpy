from __future__ import annotations

import argparse
import keyword
import re
import shutil
import subprocess
import sys
import textwrap
import typing as t
from abc import ABC, abstractmethod
from html.parser import HTMLParser

import htpy

__all__ = ["html2htpy"]


_void_elements: set[str] = {
    element._name  # pyright: ignore [reportPrivateUsage]
    for element in htpy.__dict__.values()  # type: ignore[attr-defined]
    if isinstance(element, htpy.VoidElement)
}


def _quote(x: str) -> str:
    if '"' in x:
        return f"'{x}'"

    return f'"{x}"'


def _format_value(value: str | None) -> str:
    if value is None:
        return "True"

    return _quote(value)


def _format_id_class_shorthand_attrs(id_: str, class_: str) -> str:
    classes = class_.split(" ") if class_ else []
    result = (f"#{id_}" if id_ else "") + (("." + ".".join(classes)) if classes else "")

    if result:
        return f'"{result}"'

    return ""


def _format_keyword_attrs(attrs: dict[str, str | None]) -> str:
    if not attrs:
        return ""

    return ", ".join(f"{key}={_format_value(value)}" for key, value in attrs.items())


def _format_dict_attrs(attrs: dict[str, str | None]) -> str:
    if not attrs:
        return ""

    return (
        "{"
        + ", ".join(f"{_quote(key)}: {_format_value(value)}" for key, value in attrs.items())
        + "}"
    )


def _can_use_shorthand_id_class(attrs: dict[str, str | None]) -> bool:
    class_ = attrs.get("class") or ""
    return "#" not in class_ and "." not in class_


def _format_attrs(attrs: dict[str, str | None], shorthand_id_class: bool) -> str:
    keyword_attrs: dict[str, str | None] = {}
    dict_attrs: dict[str, str | None] = {}

    shorthand_id_class_str = (
        _format_id_class_shorthand_attrs(attrs.pop("id", "") or "", attrs.pop("class", "") or "")
        if shorthand_id_class and _can_use_shorthand_id_class(attrs)
        else ""
    )

    for key, value in attrs.items():
        potential_keyword_key = key.replace("-", "_")
        if potential_keyword_key.isidentifier():
            if keyword.iskeyword(potential_keyword_key):
                keyword_attrs[potential_keyword_key + "_"] = value
            else:
                keyword_attrs[potential_keyword_key] = value
        else:
            dict_attrs[key] = value

    _attrs = ", ".join(
        x
        for x in [
            shorthand_id_class_str,
            _format_keyword_attrs(keyword_attrs),
            _format_dict_attrs(dict_attrs),
        ]
        if x
    )

    if not _attrs:
        return ""

    return f"({_attrs})"


def _format_element(python_element_name: str, use_h_prefix: bool) -> str:
    if use_h_prefix:
        return f"h.{python_element_name}"
    return python_element_name


def _format_child(child: Tag | str, *, shorthand_id_class: bool, use_h_prefix: bool) -> str:
    if isinstance(child, Tag):
        return child.serialize(shorthand_id_class=shorthand_id_class, use_h_prefix=use_h_prefix)
    else:
        return str(child)


def _format_children(
    children: list[Tag | str], *, shorthand_id_class: bool, use_u_prefix: bool
) -> str:
    if not children:
        return ""
    return (
        "["
        + ", ".join(
            _format_child(child, shorthand_id_class=shorthand_id_class, use_h_prefix=use_u_prefix)
            for child in children
        )
        + "]"
    )


class Tag:
    def __init__(
        self,
        html_tag: str,
        attrs: dict[str, str | None],
        parent: Tag | None,
    ):
        self.html_tag = html_tag
        self.attrs = attrs
        self.children: list[Tag | str] = []
        self.parent = parent

    @property
    def python_element_name(self) -> str:
        if keyword.iskeyword(self.html_tag):
            return self.html_tag + "_"
        return self.html_tag.replace("-", "_")

    def serialize(self, *, shorthand_id_class: bool, use_h_prefix: bool) -> str:
        return (
            _format_element(self.python_element_name, use_h_prefix)
            + _format_attrs(dict(self.attrs), shorthand_id_class)
            + _format_children(
                self.children, shorthand_id_class=shorthand_id_class, use_u_prefix=use_h_prefix
            )
        )


class Formatter(ABC):
    error_return_code: int

    @abstractmethod
    def format(self, s: str) -> str:
        raise NotImplementedError()


class BlackFormatter(Formatter):
    error_return_code = 123

    def format(self, s: str) -> str:
        result = subprocess.run(
            ["black", "-q", "-"],
            input=s.encode("utf8"),
            stdout=subprocess.PIPE,
        )
        if result.returncode == self.error_return_code:
            _printerr("Black failed to parse the input. The output will be left unformatted.")
            _printerr(
                "This is likely a bug in html2htpy. Please report this as an issue to htpy: https://github.com/pelme/htpy/issues."
            )
            return s
        return result.stdout.decode("utf8")


class RuffFormatter(Formatter):
    error_return_code = 2

    def format(self, s: str) -> str:
        result = subprocess.run(
            ["ruff", "format", "-"],
            input=s.encode("utf8"),
            stdout=subprocess.PIPE,
        )
        if result.returncode == self.error_return_code:
            _printerr("Ruff failed to parse the input. The output will be left unformatted.")
            _printerr(
                "This is likely a bug in html2htpy. Please report this as an issue to htpy: https://github.com/pelme/htpy/issues."
            )
            return s
        return result.stdout.decode("utf8")


class HTPYParser(HTMLParser):
    def __init__(self, django) -> None:
        self._collected: list[Tag | str] = []
        self._current: Tag | None = None
        self._blocks = []  # store django block tags to make sure the html parser does not modify them
        self._django = django
        super().__init__()

    def feed(self, data):
        from django.template import base
        if self._django:
            lexer = base.Lexer(data)
            tokens = lexer.tokenize()

            cleaned_data = []
            index = 0
            for token in tokens:
                contents = token.contents
                if token.token_type == base.TokenType.COMMENT:
                    contents = "{# " + contents + " #}"
                elif token.token_type == base.TokenType.TEXT:
                    pass
                elif token.token_type == base.TokenType.VAR:
                    contents = "{{ " + contents + " }}"
                elif token.token_type == base.TokenType.BLOCK:
                    self._blocks.append(contents)
                    contents = f"{{%{index}%}}"
                    index += 1

                cleaned_data.append(contents)

            data = "".join(cleaned_data)

        super().feed(data)


    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = Tag(tag, dict(attrs), parent=self._current)

        if not self._current:
            self._collected.append(t)
        else:
            self._current.children.append(t)

        if tag not in _void_elements:
            self._current = t

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = Tag(tag, dict(attrs), parent=self._current)

        if not self._current:
            self._collected.append(t)
        else:
            self._current.children.append(t)

    def handle_endtag(self, tag: str) -> None:
        if not self._current:
            raise Exception(f"Error parsing html: Closing tag {tag} when not inside any other tag")

        if not self._current.html_tag == tag:
            raise Exception(
                f"Error parsing html: Closing tag {tag} does not match the "
                f"currently open tag ({self._current.html_tag})"
            )

        self._current = self._current.parent

    def handle_data(self, data: str) -> None:
        if not data.isspace():
            stringified_data = _convert_data_to_string(data)

            if self._current:
                self._current.children.append(stringified_data)
            else:
                self._collected.append(stringified_data)

    def serialize_python(
        self,
        shorthand_id_class: bool = False,
        import_mode: t.Literal["yes", "h", "no"] = "yes",
        formatter: Formatter | None = None,
    ) -> str:
        o = ""

        use_h_prefix = False

        if import_mode == "yes":
            unique_tags: set[str] = set()

            def _tags_from_children(parent: Tag) -> None:
                for c in parent.children:
                    if isinstance(c, Tag):
                        unique_tags.add(c.python_element_name)
                        _tags_from_children(c)

            for t in self._collected:
                if isinstance(t, Tag):
                    unique_tags.add(t.python_element_name)
                    _tags_from_children(t)

            sorted_tags = list(unique_tags)
            sorted_tags.sort()

            o += f"from htpy import {', '.join(sorted_tags)}\n"

        elif import_mode == "h":
            o += "import htpy as h\n"
            use_h_prefix = True

        if len(self._collected) == 1:
            o += _serialize(self._collected[0], shorthand_id_class, use_h_prefix)

        else:
            o += "["
            for t in self._collected:
                o += _serialize(t, shorthand_id_class, use_h_prefix) + ","
            o = o[:-1] + "]"

        if self._django:
            # reinsert django block tags
            for index, block in enumerate(self._blocks):
                o = o.replace(f"{{%{index}%}}", f"{{% {block} %}}")

            o = template2htpy(o)

        if formatter:
            return formatter.format(o)
        else:
            return o


def handle_filters(string: str, add_imports: set, _default_filters: list) -> str:
    if "|" not in string:
        return string

    start, end = string.rsplit("|", 1)
    parts = end.split(":", 1)
    if parts[0] in _default_filters:
        start = handle_filters(start, add_imports, _default_filters)
        add_imports.add(f"from django.template.defaultfilters import {parts[0]}\n")
        string = f"{parts[0]}({start}{", " + ", ".join(parts[1:]) if len(parts) > 1 else ""})"

    return string


def handle_f_string(string: str) -> str:
    parts = string.rsplit('"', 1)
    if len(parts) == 2:
        if parts[0].rstrip('"').endswith("f"):
            string = parts[0] + '"' + parts[1]

        else:
            string = parts[0] + 'f"' + parts[1]
    else:
        string = parts[0]

    return string


def template_parser(tokens: list):
    from django.template import base
    from django.template import defaultfilters
    from inspect import getmembers, isfunction
    functions_list = getmembers(defaultfilters, isfunction)
    _default_filters = [function[0] for function in functions_list if not function[0].startswith("_")]
    parsed: list[str] = []
    if_list = []
    for_list = []
    add_imports = set()
    inline_if = False
    in_comment = False
    strip_surrounding = False
    strip_surrounding_quote = False
    strip_next_comma = False
    for index, token in enumerate(tokens):
        contents: str = token.contents
        if token.token_type == base.TokenType.VAR:
            contents = contents.replace('\\"', '"')
            contents = handle_filters(contents, add_imports, _default_filters)
            contents = "{" + contents + "}"
            last_parsed = parsed.pop()
            parsed.append(handle_f_string(last_parsed))

        elif token.token_type == base.TokenType.COMMENT:
            contents = "#" + contents + "\n"
            strip_surrounding_quote = True
            strip_next_comma = True
        elif token.token_type == base.TokenType.TEXT:
            if strip_surrounding:
                contents = contents.lstrip('", ')
                strip_surrounding = False

            if strip_surrounding_quote:
                contents = contents.lstrip('" ')
                strip_surrounding_quote = False

            if strip_next_comma:
                contents = contents.lstrip(",")
                strip_next_comma = False

        elif token.token_type == base.TokenType.BLOCK:
            contents = handle_filters(contents, add_imports, _default_filters)
            if contents.startswith("if "):
                if_list.append(["if", contents])
                _last: str = parsed[-1]
                _next: str = tokens[index+1].contents

                if _last.strip().endswith('"') or _next.strip().startswith('"'):
                    contents = "("
                    strip_surrounding_quote = True
                    strip_next_comma = True
                else:
                    parsed[-1] = handle_f_string(_last)
                    contents = '{"'
                    inline_if = True

            elif contents.startswith("elif "):
                if_start = if_list.pop()
                if_list.append(["elif", contents])
                if inline_if:
                    contents = f'" {if_start[1]} else "'
                else:
                    contents = f') {if_start[1]} else ('
                strip_surrounding = True

            elif contents == "else":
                if_start = if_list.pop()
                if_list.append(["else", contents])
                if inline_if:
                    contents = f'" {if_start[1].removeprefix("el")} else "'
                else:
                    contents = f') {if_start[1].removeprefix("el")} else ('
                strip_surrounding = True

            elif contents == "endif":
                if_start = if_list.pop()
                if inline_if:
                    if if_start[0] == "else":
                        contents = '"}'  # just a normal ending
                    else:
                        contents = f'" {if_start[1].removeprefix("el")} else ""}}'
                else:
                    if if_start[0] == "else":
                        contents = "),"  # just a normal ending
                    else:
                        contents = f') {if_start[1].removeprefix("el")} else "",'  # handle if and elif
                    strip_surrounding = True
                inline_if = False

            elif contents.startswith("for "):
                for_list.append(contents)
                contents = "("
                strip_surrounding = True

            elif contents == "endfor":
                for_start = for_list.pop()
                contents = f' {for_start}),'
                strip_surrounding = True

            elif contents.startswith("url "):
                add_imports.add("from django.urls import reverse\n")
                parts = contents.split()
                args = ""
                if len(parts) > 2:
                    args = f", args={str(parts[2:]).replace("'", "")}"
                contents = f'reverse({parts[1]}{args})'
                strip_surrounding_quote = True

            elif contents.startswith("static "):
                add_imports.add("from django.templatetags.static import static\n")
                parts = contents.split()
                args = ""
                if len(parts) > 2:
                    args = f", args={str(parts[2:]).replace("'", "")}"
                contents = f'static({parts[1]}{args})'
                strip_surrounding_quote = True

            elif contents.startswith("now "):
                args = contents.removeprefix("now ")
                add_imports.add("from django.template.defaultfilters import date\n")
                add_imports.add("from datetime import datetime\n")
                add_imports.add("from django.utils import timezone\n")
                add_imports.add("from django.conf import settings\n")
                contents = f"date(datetime.now(tz=timezone.get_current_timezone() if settings.USE_TZ else None), {args})"
                strip_surrounding_quote = True

            elif contents == "csrf_token":
                contents = f"csrf.get_token(request),"
                add_imports.add("from django.middleware import csrf\n")
                strip_surrounding_quote = True

            elif contents == "comment":
                in_comment = True
                contents = ""

            elif contents == "endcomment":
                in_comment = False
                contents = ""

            else:  # some block that we don't know how to handle
                contents = "{{% " + contents + " %}}"

        if strip_surrounding:
            last_parsed = parsed.pop()
            last_parsed = last_parsed.rstrip('", ')
            parsed.append(last_parsed)

        if strip_surrounding_quote:
            last_parsed = parsed.pop()
            last_parsed = last_parsed.rstrip('" ')
            parsed.append(last_parsed)

        if in_comment and contents != "":
            contents = f"# {contents.replace("\n", "\n# ")} \n"

        parsed.append(contents)

    parsed = list(add_imports) + parsed

    return parsed


def template2htpy(string: str) -> str:
    from django.template import base
    string = string.replace("{{%", "{%").replace("%}}", "%}")
    lexer = base.Lexer(string)
    tokens = lexer.tokenize()
    parsed = template_parser(tokens)

    joined = "".join(parsed)
    joined = joined.replace(",,", ",")

    return joined


def html2htpy(
    html: str,
    shorthand_id_class: bool = True,
    import_mode: t.Literal["yes", "h", "no"] = "yes",
    formatter: Formatter | None = None,
    django: bool = False,
) -> str:
    parser = HTPYParser(django)
    parser.feed(html)

    return parser.serialize_python(shorthand_id_class, import_mode, formatter)


def _convert_data_to_string(data: str) -> str:
    _data = str(data)

    # Normalize leading whitespace
    leading_whitespace_pattern = re.compile(r"^(\s+)(\S)")

    def leading_whitespace_replacer(match: re.Match[str]) -> str:
        whitespace = match.group(1)
        first_char = match.group(2)

        if whitespace.endswith(" "):
            # keep single leading space (' ') before non-whitespace
            # if it was there from before
            return " " + first_char
        else:
            return first_char

    _data = leading_whitespace_pattern.sub(leading_whitespace_replacer, _data)

    # Normalize trailing whitespace
    leading_whitespace_pattern = re.compile(r"(\S)(\s+)$")

    def trailing_whitespace_replacer(match: re.Match[str]) -> str:
        last_char = match.group(1)
        whitespace = match.group(2)

        if whitespace.startswith(" "):
            # keep single trailing space (' ') after non-whitespace
            # if it was there from before
            return last_char + " "
        else:
            return last_char

    _data = leading_whitespace_pattern.sub(trailing_whitespace_replacer, _data)

    is_multiline = "\n" in _data

    # escape unescaped dblquote: " -> \"
    _data = re.compile(r'(?<![\\])"').sub('\\"', _data)

    template_string_pattern = re.compile(r"\{\{\s*[\w\.]+\s*\}\}")

    has_jinja_pattern = re.search(template_string_pattern, _data)
    if has_jinja_pattern:
        # regex replaces these 3 cases:
        # {{ var.xx }} -> { var.xx }
        # { -> {{
        # } -> }}
        template_string_replace_pattern = re.compile(
            r"(\{\{\s*[\w\.]+\s*\}\}|(?<![\{]){(?![\{])|(?<![\}])}(?![\}]))"
        )

        def replacer(match: re.Match[str]) -> str:
            captured = match.group(1)

            if captured.startswith("{{"):
                return captured[1:-1]

            if captured == "{":
                return "{{"

            return "}}"

        _data = template_string_replace_pattern.sub(replacer, _data)
        if is_multiline:
            _data = '""' + _data + '""'

        _data = 'f"' + _data + '"'
    else:
        if is_multiline:
            _data = '""' + _data + '""'

        _data = '"' + _data + '"'

    return _data


def _serialize(el: Tag | str, shorthand_id_class: bool, use_h_prefix: bool) -> str:
    if isinstance(el, Tag):
        return el.serialize(shorthand_id_class=shorthand_id_class, use_h_prefix=use_h_prefix)
    else:
        return str(el)


def _handle_templates(template):
    if template == "auto":
        django = True
        try:
            from django.template import base
        except ImportError:
            django = False
    elif template == "django":
        django = True
    elif template == "none":
        django = False
    else:
        django = False

    return django


def _get_formatter(format: t.Literal["auto", "ruff", "black", "none"]) -> Formatter | None:
    if format == "ruff":
        if _is_command_available("ruff"):
            return RuffFormatter()
        else:
            _printerr(
                "Selected formatter (ruff) is not installed.",
            )
            _printerr("Please install it or select another formatter.")
            _printerr("`html2htpy -h` for help")
            sys.exit(1)

    if format == "black":
        if _is_command_available("black"):
            return BlackFormatter()
        else:
            _printerr(
                "Selected formatter (black) is not installed.",
            )
            _printerr("Please install it or select another formatter.")
            _printerr("`html2htpy -h` for help")
            sys.exit(1)

    elif format == "auto":
        if _is_command_available("black"):
            return BlackFormatter()
        if _is_command_available("ruff"):
            return RuffFormatter()

    return None


def _is_command_available(command: str) -> bool:
    return shutil.which(command) is not None


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="html2htpy",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "-f",
        "--format",
        choices=["auto", "ruff", "black", "none"],
        default="auto",
        help=textwrap.dedent(
            """
            Format the output code with a code formatter.

            auto (default):
              - If black is installed (exists on PATH), use `black` for formatting.
              - If ruff is installed (exists on PATH): Use `ruff format` for formatting.
              - If neither black or ruff is installed, do not perform any formatting.

            black:
              Use the black formatter (https://black.readthedocs.io/en/stable/).

            ruff:
              Use the ruff formatter (https://docs.astral.sh/ruff/formatter/).

            none:
              Do not format the output code at all.
        """,
        ),
    )
    parser.add_argument(
        "-t",
        "--template",
        choices=["auto", "django", "none"],
        default="auto",
        help=textwrap.dedent(
            """
            Translate some django template tags to python.

            auto (default):
              - If django is installed, use `django` to handle template tags.
              - If django is not installed, do not handle django template tags.

            django:
              Handle django template tags.

            none:
              Do not handle django template tags.
        """,
        ),
    )
    parser.add_argument(
        "-i",
        "--imports",
        choices=["yes", "h", "no"],
        help=textwrap.dedent("""
            Specify formatting for imports.

            yes (default):
                Add `from htpy import div, span` for all found elements.
            h:
                Add a single `import htpy as h`.
                Reference elements with `h.div`, `h.span`.
            no:
                Do not add imports.
        """),
        default="yes",
    )
    parser.add_argument(
        "--no-shorthand",
        help="Use explicit `id` and `class_` kwargs instead of the shorthand #id.class syntax.",
        action="store_true",
    )
    parser.add_argument(
        "input",
        type=argparse.FileType("r"),
        nargs="?",
        default=sys.stdin,
        help=(
            "Input HTML file, e.g. home.html. "
            "Optional. If not specified, html2htpy will read from stdin."
        ),
    )

    args = parser.parse_args()

    try:
        input = args.input.read()
    except KeyboardInterrupt:
        _printerr(
            "\nInterrupted",
        )
        sys.exit(1)

    shorthand = not args.no_shorthand
    imports: t.Literal["yes", "h", "no"] = args.imports

    formatter = _get_formatter(args.format)

    django = _handle_templates(args.template)

    print(html2htpy(input, shorthand, imports, formatter, django))


def _printerr(value: str) -> None:
    print(value, file=sys.stderr)
