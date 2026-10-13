"""TOML reader for the subset herdr's own files use.

Generalised from herdr-sort's parse_rules, which accepts `[[rule]]` and nothing
else. This one reads a config file it does not own, so anything the subset
cannot express raises with a line number instead of being skipped: a dropped
`[[keys.command]]` would render a cheat-sheet that is wrong rather than short.

Not supported, and rejected loudly: multi-line strings.
"""

import re

__all__ = ["TomlError", "load", "loads"]


class TomlError(Exception):
    pass


_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
            '"': '"', "\\": "\\"}
_INT = re.compile(r"^[+-]?(0|[1-9](_?[0-9])*)$")
_FLOAT = re.compile(
    r"^[+-]?((0|[1-9](_?[0-9])*)(\.[0-9](_?[0-9])*)?([eE][+-]?[0-9]+)?|nan|inf)$")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return loads(handle.read(), path)


def loads(text, name="<string>"):
    return _Parser(text, name).parse()


def _scan(text):
    """Bracket depth outside strings and comments, plus whether a string is open."""
    depth, quote, escaped, comment = 0, None, False, False
    for ch in text:
        if comment:
            comment = ch != "\n"
        elif quote:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            elif ch == "\n":
                quote = None  # a single-line string cannot span lines; let the
                              # statement parser report it with a line number
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            comment = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
    return depth, quote is not None


def _skip(text, i):
    while i < len(text):
        if text[i] in " \t\r\n":
            i += 1
        elif text[i] == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
        else:
            break
    return i


def _outside(text, wanted, start=0):
    """Index of the first `wanted` character that is not inside a string."""
    quote, escaped = None, False
    for i in range(start, len(text)):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in wanted:
            return i
    return -1


class _Parser:
    def __init__(self, text, name):
        self.text = text
        self.name = name
        self.root = {}
        self.current = self.root
        self.lineno = 0

    def fail(self, message):
        raise TomlError(f"{self.name}:{self.lineno}: {message}")

    def parse(self):
        buf, start = "", 0
        for index, raw in enumerate(self.text.splitlines(), 1):
            start = start or index
            buf = f"{buf}\n{raw}" if buf else raw
            depth, open_string = _scan(buf)
            if depth > 0 or open_string:
                continue
            if depth < 0:
                self.lineno = start
                self.fail("unbalanced ] or }")
            self.lineno = start
            self._statement(buf.strip())
            buf, start = "", 0
        if buf.strip():
            self.lineno = start
            self.fail("unterminated array, inline table, or string")
        return self.root

    def _statement(self, text):
        if not text or text.startswith("#"):
            return
        if text.startswith("["):
            self._header(text)
        else:
            self._pair(text)

    def _header(self, text):
        array = text.startswith("[[")
        open_len = 2 if array else 1
        close = _outside(text, "]", open_len)
        if close < 0:
            self.fail(f"unterminated table header {text!r}")
        if array:
            if not text.startswith("]", close + 1):
                self.fail(f"unterminated array-of-tables header {text!r}")
            end = close + 2
        else:
            end = close + 1
        trailing = text[end:].strip()
        if trailing and not trailing.startswith("#"):
            self.fail(f"trailing text after table header: {trailing!r}")
        self.current = self._descend(self._key(text[open_len:close]), array)

    def _descend(self, parts, array):
        node = self.root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if isinstance(child, list):
                child = child[-1]
            if not isinstance(child, dict):
                self.fail(f"{part!r} is a value, not a table")
            node = child
        last = parts[-1]
        if array:
            bucket = node.setdefault(last, [])
            if not isinstance(bucket, list):
                self.fail(f"{last!r} was defined as a table, not an array of tables")
            table = {}
            bucket.append(table)
            return table
        child = node.setdefault(last, {})
        if not isinstance(child, dict):
            self.fail(f"{last!r} is a value, not a table")
        return child

    def _pair(self, text):
        eq = _outside(text, "=")
        if eq < 0:
            self.fail(f"cannot parse {text!r}")
        parts = self._key(text[:eq])
        value, index = self._value(text, eq + 1)
        trailing = text[index:].strip()
        if trailing and not trailing.startswith("#"):
            self.fail(f"trailing text after value: {trailing!r}")
        node = self.current
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                self.fail(f"{part!r} is a value, not a table")
        node[parts[-1]] = value

    def _key(self, text):
        parts, buf, quote, escaped = [], "", None, False
        for ch in text:
            if quote:
                if escaped:
                    buf += ch
                    escaped = False
                elif quote == '"' and ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = None
                else:
                    buf += ch
            elif ch in "\"'":
                quote = ch
            elif ch == ".":
                parts.append(buf.strip())
                buf = ""
            else:
                buf += ch
        parts.append(buf.strip())
        if any(not part for part in parts):
            self.fail(f"empty key segment in {text!r}")
        return parts

    def _value(self, text, i):
        i = _skip(text, i)
        if i >= len(text):
            self.fail("missing value")
        ch = text[i]
        if text.startswith('"""', i) or text.startswith("'''", i):
            self.fail("multi-line strings are not supported")
        if ch == '"':
            return self._basic(text, i)
        if ch == "'":
            return self._literal(text, i)
        if ch == "[":
            return self._array(text, i)
        if ch == "{":
            return self._inline(text, i)
        return self._bare(text, i)

    def _basic(self, text, i):
        out, i = [], i + 1
        while i < len(text) and text[i] != "\n":
            ch = text[i]
            if ch == '"':
                return "".join(out), i + 1
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            i += 1
            if i >= len(text):
                break
            escape = text[i]
            if escape in _ESCAPES:
                out.append(_ESCAPES[escape])
            elif escape in "uU":
                width = 4 if escape == "u" else 8
                digits = text[i + 1:i + 1 + width]
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    self.fail(f"bad \\{escape} escape {digits!r}")
                i += width
            else:
                self.fail(f"unknown escape \\{escape}")
            i += 1
        self.fail("unterminated string")

    def _literal(self, text, i):
        end = text.find("'", i + 1)
        newline = text.find("\n", i + 1)
        if end < 0 or (0 <= newline < end):
            self.fail("unterminated literal string")
        return text[i + 1:end], end + 1

    def _array(self, text, i):
        items, i = [], i + 1
        while True:
            i = _skip(text, i)
            if i >= len(text):
                self.fail("unterminated array")
            if text[i] == "]":
                return items, i + 1
            value, i = self._value(text, i)
            items.append(value)
            i = _skip(text, i)
            if i >= len(text):
                self.fail("unterminated array")
            if text[i] == ",":
                i += 1
            elif text[i] != "]":
                self.fail(f"expected , or ] in array, found {text[i]!r}")

    def _inline(self, text, i):
        table, i = {}, i + 1
        while True:
            i = _skip(text, i)
            if i >= len(text):
                self.fail("unterminated inline table")
            if text[i] == "}":
                return table, i + 1
            eq = _outside(text, "=", i)
            if eq < 0:
                self.fail("inline table entry has no =")
            parts = self._key(text[i:eq])
            value, i = self._value(text, eq + 1)
            node = table
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            i = _skip(text, i)
            if i >= len(text):
                self.fail("unterminated inline table")
            if text[i] == ",":
                i += 1
            elif text[i] != "}":
                self.fail(f"expected , or }} in inline table, found {text[i]!r}")

    def _bare(self, text, i):
        start = i
        while i < len(text) and text[i] not in ",]}#\n":
            i += 1
        token = text[start:i].strip()
        if not token:
            self.fail("missing value")
        if "true" == token:
            return True, i
        if "false" == token:
            return False, i
        if _INT.match(token):
            return int(token.replace("_", "")), i
        if _FLOAT.match(token):
            return float(token.replace("_", "")), i
        # Dates and times pass through as text; nothing in this plugin reads one.
        return token, i
