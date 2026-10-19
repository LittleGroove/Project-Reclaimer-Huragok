"""Change values in a TOML file in place, keeping every comment and the layout.

Used for settings.toml (the window's "toggle autostart" writes `autostart = false`
into the server's [[server]] block) and for a server's dedicated.toml when the
supervisor makes a server folder: top-level keys (`game = "../../game"`), keys in a
table (`[rcon] password`, `[workshop] items`) and the [[server]] blocks themselves.
The file is never rewritten from scratch, so the host's comments stay.

Every change is checked before the file is replaced: the new text is read back with tomllib and compared with
what the old text read as plus the one change, and nothing else may differ. TOML written in a form this module
doesn't edit (a dotted key such as `rcon.password = …`, an inline table `rcon = { … }`, a `[server.campaign]`
sub-table) is refused with a TomlEditError rather than guessed at, and so is any result that would read
differently; the file is then left exactly as it was, and the host changes that value by hand. Lines inside a
multi-line array or string are never taken for headers, keys or commented-out keys.
"""

from __future__ import annotations

import copy
import itertools
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Za-z0-9_-]+)\s*=")
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
_temp_names = itertools.count()


class TomlEditError(OSError):
    """A change that couldn't be made safely; the file wasn't touched. An OSError, so every caller that reports a
    failed write ("couldn't write dedicated.toml: …") reports this one the same way."""


def _toml_string(text: str) -> str:
    """A TOML basic string. Control characters (C0, DEL, C1) are escaped as TOML requires, and so are the Unicode
    line and paragraph separators, which editors and str.splitlines() take for line breaks."""
    out = []
    for char in text:
        code = ord(char)
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif code < 0x20 or 0x7F <= code <= 0x9F or code in (0x2028, 0x2029):
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:  # a lone surrogate can't be written as UTF-8
            out.append("\\ufffd")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _key(name: str) -> str:
    return name if BARE_KEY.fullmatch(name) else _toml_string(name)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(str(k))} = {_toml_value(v)}" for k, v in value.items()) + " }"
    return _toml_string(str(value))


def _plain(value: object) -> Any:
    """The value as tomllib reads back what _toml_value writes: tuples are lists, anything else is a string."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return re.sub("[\ud800-\udfff]", "�", str(value))


def _scan(text: str, depth: int = 0, quote: str | None = None) -> tuple[int, str | None, int | None]:
    """Walk one line of a value: (bracket depth after it, the string still open, where a comment starts).

    Only a `#` outside quotes starts a comment, so `password = "a#b"` keeps its value.
    """
    i = 0
    while i < len(text):
        char = text[i]
        if quote is not None:
            if quote in ('"', '"""') and char == "\\":
                i += 2
                continue
            if len(quote) == 3 and text.startswith(quote, i):
                quote, i = None, i + 3
                continue
            if len(quote) == 1 and char == quote:
                quote = None
            i += 1
            continue
        if char == "#":
            return depth, None, i
        if text.startswith('"""', i) or text.startswith("'''", i):
            quote, i = text[i:i + 3], i + 3
            continue
        if char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        i += 1
    if quote in ('"', "'"):
        quote = None  # a one-line string can't run on to the next line
    return depth, quote, None


def _key_at(text: str, i: int) -> tuple[tuple[str, ...], int] | None:
    """The key starting at text[i] (bare, "quoted" or 'literal', dotted or not): its parts and where it ends."""
    parts: list[str] = []
    while True:
        while i < len(text) and text[i] in " \t":
            i += 1
        if text.startswith('"', i):
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            try:
                parts.append(tomllib.loads("k = " + text[i:end + 1])["k"])
            except tomllib.TOMLDecodeError:
                return None
            i = end + 1
        elif text.startswith("'", i):
            end = text.find("'", i + 1)
            if end < 0:
                return None
            parts.append(text[i + 1:end])
            i = end + 1
        else:
            match = BARE_KEY.match(text, i)
            if match is None:
                return None
            parts.append(match.group())
            i = match.end()
        while i < len(text) and text[i] in " \t":
            i += 1
        if not text.startswith(".", i):
            return tuple(parts), i
        i += 1


def _header(text: str) -> tuple[tuple[str, ...] | None, bool]:
    """A `[table]` or `[[array]]` header: (its key parts, whether it's an array); the parts are None when the
    header can't be read (it still ends the section above it)."""
    array = text.startswith("[[")
    found = _key_at(text, 2 if array else 1)
    if found is None:
        return None, array
    parts, i = found
    close = "]]" if array else "]"
    rest = text[i + len(close):].strip()
    if not text.startswith(close, i) or rest and not rest.startswith("#"):
        return None, array
    return parts, array


@dataclass
class _Line:
    """What one line of the file is. "more" is a line inside a multi-line value begun on a line above."""

    kind: str  # "blank", "comment", "header", "key", "more" or "other"
    parts: tuple[str, ...] | None = None  # a header's or a key's name, dotted parts apart
    array: bool = False  # a [[header]]
    at: int = 0  # a key line: where its value starts
    end: int = 0  # a key line: the last line of its value


def _layout(lines: list[str]) -> list[_Line]:
    found: list[_Line] = []
    number = 0
    while number < len(lines):
        line = lines[number]
        text = line.strip()
        key = None if not text or text[0] in "#[" else _key_at(line, 0)
        if not text:
            found.append(_Line("blank"))
        elif text.startswith("#"):
            found.append(_Line("comment"))
        elif text.startswith("["):
            parts, array = _header(text)
            found.append(_Line("header", parts, array))
        elif key is None or not line.startswith("=", key[1]):
            found.append(_Line("other"))
        else:
            at = key[1] + 1
            while at < len(line) and line[at] in " \t":
                at += 1
            depth, quote, _ = _scan(line[at:])
            end = number
            while (depth > 0 or quote is not None) and end + 1 < len(lines):
                end += 1
                depth, quote, _ = _scan(lines[end], depth, quote)
            found.append(_Line("key", key[0], at=at, end=end))
            found.extend(_Line("more") for _ in range(number + 1, end + 1))
            number = end
        number += 1
    return found


def _comment(value: str) -> str:
    """A one-line value's own comment (`key = 1  # why`), kept when the value changes."""
    _, _, at = _scan(value)
    return "" if at is None else value[len(value[:at].rstrip()):]


def _write_text(path: Path, text: str) -> None:
    """Replace the file in one step (a temp file beside it, then os.replace), so it's never left half-written."""
    temp = path.with_name(f"{path.name}.{os.getpid()}-{next(_temp_names)}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        if os.name != "nt" and path.exists():  # keep a chmod 600 file private
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _differences(new: dict[str, Any], wanted: dict[str, Any]) -> str:
    keys = sorted({k for k in set(new) | set(wanted) if new.get(k) != wanted.get(k)})
    return ", ".join(f"[{k}]" if isinstance(wanted.get(k, new.get(k)), (dict, list)) else k for k in keys[:5])


@dataclass
class _Block:
    """A [[server]] block: its header line, where its own keys end, and where it ends after its sub-tables
    ([server.campaign] under a [[server]] belongs to that server)."""

    start: int
    end: int | None = None
    last: int | None = None
    subtables: set[str] = field(default_factory=set)


class _Edit:
    """One change to one file: its lines, what it reads as now, and what it must read as once changed."""

    def __init__(self, path: Path, what: str) -> None:
        self.path, self.what = path, what
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except UnicodeDecodeError:
            raise self.refuse("it isn't UTF-8 text") from None
        self.lines = text.split("\n")  # not splitlines(): U+2028 and the like are legal inside TOML strings
        if self.lines[-1] == "":
            self.lines.pop()
        try:
            self.old = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise self.refuse(f"it isn't valid TOML now ({error})") from None
        self.wanted = copy.deepcopy(self.old)
        self.layout = _layout(self.lines)

    def refuse(self, why: str) -> TomlEditError:
        return TomlEditError(f"couldn't change {self.what} safely in {self.path.name}: {why}; edit it by hand (E)")

    def changed(self) -> None:
        self.layout = _layout(self.lines)

    def save(self) -> None:
        text = "\n".join(self.lines) + "\n"
        try:
            new = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise self.refuse(f"the result wouldn't be valid TOML ({error})") from None
        if new != self.wanted:
            raise self.refuse(f"other values would change too ({_differences(new, self.wanted)})")
        _write_text(self.path, text)

    def form(self, number: int) -> str:
        """How a key line is written, without its value (it may be a password): `rcon.password = …`."""
        entry = self.layout[number]
        value = self.lines[number][entry.at:].lstrip()
        return ".".join(entry.parts or ()) + (" = { … }" if value.startswith("{") else " = …")

    def first_header(self) -> int:
        return next((n for n, entry in enumerate(self.layout) if entry.kind == "header"), len(self.lines))

    def tables(self) -> dict[tuple[str, ...], tuple[int, int]]:
        """Name -> (header line, line after its last) of every [table] ([[arrays]] aren't tables here)."""
        found: dict[tuple[str, ...], tuple[int, int]] = {}
        current: tuple[str, ...] | None = None
        start = 0
        for number, entry in enumerate(self.layout):
            if entry.kind != "header":
                continue
            if current is not None:
                found[current] = (start, number)
            current, start = (None if entry.array else entry.parts), number
        if current is not None:
            found[current] = (start, len(self.lines))
        return found

    def blocks(self) -> list[_Block]:
        found: list[_Block] = []
        for number, entry in enumerate(self.layout):
            if entry.kind != "header":
                continue
            if found and found[-1].last is None:
                block = found[-1]
                if block.end is None:
                    block.end = number
                if entry.parts is not None and len(entry.parts) > 1 and entry.parts[0] == "server":
                    block.subtables.add(entry.parts[1])
                    continue
                block.last = number
            if entry.array and entry.parts == ("server",):
                found.append(_Block(number))
        if found and found[-1].last is None:
            found[-1].end = len(self.lines) if found[-1].end is None else found[-1].end
            found[-1].last = len(self.lines)
        return found

    def block(self, server: str) -> tuple[int, _Block] | None:
        """The first [[server]] block named `server` (any case): its place in the parsed list, and its lines.
        The name comes from the parsed file, so `"name" = …` and escapes in it are read as the program reads them."""
        parsed = self.wanted.get("server")  # the blocks as they are now, any added in this edit too
        blocks = self.blocks()
        if not isinstance(parsed, list) or len(parsed) != len(blocks):
            return None
        for index, (data, block) in enumerate(zip(parsed, blocks)):
            name = data.get("name") if isinstance(data, dict) else None
            if isinstance(name, str) and name.lower() == server.lower():
                return index, block
        return None

    def key_line(self, start: int, end: int, key: str, where: str) -> int | None:
        """The line setting `key` between start and end; a dotted `key.x = …` there is refused."""
        for number in range(start, end):
            entry = self.layout[number]
            if entry.kind == "key" and entry.parts and entry.parts[0] == key:
                if len(entry.parts) > 1:
                    raise self.refuse(f"{where} writes {key} as `{self.form(number)}` (dotted keys)")
                return number
        return None

    def replace(self, number: int, rendered: str) -> None:
        """Put `rendered` in place of the value starting on line `number`, every line of it."""
        entry = self.layout[number]
        line = self.lines[number]
        comment = _comment(line[entry.at:]) if entry.end == number else ""
        self.lines[number:entry.end + 1] = [line[:entry.at] + rendered + comment]
        self.changed()

    def set_in(self, start: int, end: int, key: str, value: object, where: str) -> None:
        """Set `key = …` between start and end: the live line, else the commented `#key = …`, else at the end."""
        rendered = _toml_value(value)
        number = self.key_line(start, end, key, where)
        if number is not None:
            self.replace(number, rendered)
            return
        for number in range(start, end):
            match = COMMENTED_KEY.match(self.lines[number])
            if self.layout[number].kind == "comment" and match and match.group(1) == key:
                self.lines[number] = f"{_key(key)} = {rendered}"
                self.changed()
                return
        insert = end
        while insert > start and self.layout[insert - 1].kind == "blank":  # before the blank lines that end it
            insert -= 1
        self.lines.insert(insert, f"{_key(key)} = {rendered}")
        self.changed()

    def uncomment_header(self, table: str) -> bool:
        """`# [workshop]` (the example `dedicated init` writes) becomes the live table when only comments follow
        it up to the next table; the example's own commented settings then come to life in place."""
        pattern = re.compile(r"^\s*#\s*\[\s*" + re.escape(table) + r"\s*\]\s*$")
        for number, line in enumerate(self.lines):
            if self.layout[number].kind != "comment" or not pattern.match(line):
                continue
            body = itertools.takewhile(lambda entry: entry.kind != "header", self.layout[number + 1:])
            if all(entry.kind in ("blank", "comment") for entry in body):
                self.lines[number] = f"[{_key(table)}]"
                self.changed()
                return True
        return False

    def server_key(self, server: str, key: str, value: object) -> None:
        """Set `key` in the [[server]] block named `server`; a missing block is added (never for `name`)."""
        found = self.block(server)
        rendered = _toml_value(value)
        if found is None:
            if key == "name":
                raise self.refuse(f"there's no [[server]] block named {server}")
            blocks = self.wanted.setdefault("server", [])
            if not isinstance(blocks, list):
                raise self.refuse("server isn't a list of [[server]] blocks there")
            blocks.append({"name": _plain(server), key: _plain(value)})
            if self.lines and self.lines[-1].strip():
                self.lines.append("")
            self.lines.extend(["[[server]]", f"name = {_toml_value(server)}", f"{_key(key)} = {rendered}"])
            self.changed()
            return
        index, block = found
        self.wanted["server"][index][key] = _plain(value)
        if key in block.subtables:
            raise self.refuse(f"{server}'s {key} is a [server.{key}] table")
        assert block.end is not None
        number = self.key_line(block.start + 1, block.end, key, f"{server}'s block")
        if number is not None:
            self.replace(number, rendered)
            return
        name = self.key_line(block.start + 1, block.end, "name", f"{server}'s block")
        self.lines.insert(self.layout[name].end + 1 if name is not None else block.start + 1,
                          f"{_key(key)} = {rendered}")
        self.changed()

    def remove_server_key(self, server: str, key: str) -> bool:
        found = self.block(server)
        if found is None or key == "name":
            return False
        index, block = found
        if key not in self.wanted["server"][index]:
            return False
        del self.wanted["server"][index][key]
        if key in block.subtables:
            raise self.refuse(f"{server}'s {key} is a [server.{key}] table")
        assert block.end is not None
        number = self.key_line(block.start + 1, block.end, key, f"{server}'s block")
        if number is None:
            raise self.refuse(f"{server}'s {key} isn't on a line of its own")
        del self.lines[number:self.layout[number].end + 1]
        self.changed()
        return True


def remove_server_setting(path: Path, server: str, key: str) -> bool:
    """Take `key` out of the [[server]] block named `server`, so the default applies again. False: it wasn't set."""
    edit = _Edit(path, f"{key} for {server}")
    if not edit.remove_server_key(server, key):
        return False
    edit.save()
    return True


def set_server_setting(path: Path, server: str, key: str, value: object) -> None:
    """Set `key` in the [[server]] block named `server`, adding the block if needed (but never to rename one:
    with no block of that name, setting `name` raises TomlEditError)."""
    edit = _Edit(path, f"{key} for {server}")
    edit.server_key(server, key, value)
    edit.save()


def change_server_settings(path: Path, server: str, values: dict[str, Any], remove: Any = (), *,
                           what: str | None = None) -> None:
    """Several keys of one [[server]] block in one write: the `remove` keys taken out, then `values` set. All of
    it is written or, when one part can't be made safely, none of it. `what` names the change in a refusal."""
    edit = _Edit(path, f"{what or ', '.join(values) or ', '.join(remove)} for {server}")
    for key in remove:
        if key not in values:
            edit.remove_server_key(server, key)
    for key, value in values.items():
        edit.server_key(server, key, value)
    edit.save()


def set_top_key(path: Path, key: str, value: object) -> None:
    """Set a key before the first table header (top-level), replacing its live or commented line."""
    edit = _Edit(path, key)
    edit.wanted[key] = _plain(value)
    edit.set_in(0, edit.first_header(), key, value, "the top of the file")
    edit.save()


def set_table_key(path: Path, table: str, key: str, value: object, *, before: str | None = None) -> None:
    """Set a key in [table], making the table when it's missing (before the [before] table, else at the end
    of the top-level keys)."""
    edit = _Edit(path, f"[{table}] {key}")
    current = edit.wanted.setdefault(table, {})
    if not isinstance(current, dict):
        raise edit.refuse(f"{table} isn't a table there")
    current[key] = _plain(value)
    tables = edit.tables()
    if (table,) not in tables:
        for number in range(edit.first_header()):
            entry = edit.layout[number]
            if entry.kind == "key" and entry.parts and entry.parts[0] == table:
                raise edit.refuse(f"[{table}] is written as `{edit.form(number)}` at the top of the file")
        if not edit.uncomment_header(table):
            at = tables[(before,)][0] if before and (before,) in tables else edit.first_header()
            edit.lines[at:at] = [f"[{_key(table)}]", "", ""]
            edit.changed()
        tables = edit.tables()
    start, end = tables[(table,)]
    edit.set_in(start + 1, end, key, value, f"[{table}]")
    edit.save()


def comment_out_table_key(path: Path, table: str, key: str, note: str) -> bool:
    """Turn [table]'s `key` line(s) into comments, with `note` (why; a line break starts another comment line)
    above them, so the host still sees what was there and why it does nothing now. False: the table doesn't set
    it."""
    edit = _Edit(path, f"[{table}] {key}")
    current = edit.wanted.get(table)
    if not isinstance(current, dict) or key not in current:
        return False
    del current[key]
    number, _ = _list_line(edit, table, key)
    if number is None:
        raise edit.refuse(f"[{table}] {key} isn't on a line of its own")
    end = edit.layout[number].end
    old = edit.lines[number:end + 1]
    edit.lines[number:end + 1] = [*(f"# {line}".rstrip() for line in note.splitlines()), *(f"# {line}" for line in old)]
    edit.changed()
    edit.save()
    return True


def add_table_key(path: Path, table: str, key: str, value: object, comments: list[str] | None = None, *,
                  after: str | None = None) -> bool:
    """Add `key = value` to [table] with its comment lines above it: just after `after`'s line(s) when the table
    sets that, else at the end of the table (made when it's missing). False: the table sets `key` already, or
    the file has it commented out (`# key = …`), which is the host's choice to leave it to its default."""
    edit = _Edit(path, f"[{table}] {key}")
    current = edit.wanted.setdefault(table, {})
    if not isinstance(current, dict):
        raise edit.refuse(f"{table} isn't a table there")
    if key in current:
        return False
    tables = edit.tables()
    span = tables.get((table,))
    if span is not None and any(edit.layout[n].kind == "comment" and (m := COMMENTED_KEY.match(edit.lines[n]))
                                and m.group(1) == key for n in range(span[0] + 1, span[1])):
        return False
    current[key] = _plain(value)
    lines = [f"# {line}" if line else "#" for line in (comments or [])] + [f"{_key(key)} = {_toml_value(value)}"]
    if span is None:
        while edit.lines and not edit.lines[-1].strip():
            edit.lines.pop()
        edit.lines += ["", f"[{_key(table)}]", *lines]
    else:
        number = edit.key_line(span[0] + 1, span[1], after, f"[{table}]") if after else None
        if number is not None:
            insert = edit.layout[number].end + 1
        else:
            insert = span[1]
            while insert > span[0] + 1 and edit.layout[insert - 1].kind == "blank":
                insert -= 1
        edit.lines[insert:insert] = lines
    edit.changed()
    edit.save()
    return True


def replace_comments(path: Path, old: list[str], new: list[str]) -> bool:
    """Put the comment lines `new` where the file has exactly the comment lines `old` (each compared with its
    spaces at either end taken off), so a comment Huragok wrote can say something new; a block the host edited
    is left as they made it. False: no such block."""
    edit = _Edit(path, "comments")
    if not old or not all(line.lstrip().startswith("#") for line in old + new):
        raise edit.refuse("only comment lines are swapped")
    wanted = [line.strip() for line in old]
    for number in range(len(edit.lines) - len(old) + 1):
        comments = all(edit.layout[number + i].kind == "comment" for i in range(len(old)))
        if comments and [line.strip() for line in edit.lines[number:number + len(old)]] == wanted:
            indent = edit.lines[number][:len(edit.lines[number]) - len(edit.lines[number].lstrip())]
            edit.lines[number:number + len(old)] = [indent + line.strip() for line in new]
            edit.changed()
            edit.save()
            return True
    return False


def _list_line(edit: _Edit, table: str, key: str) -> tuple[int | None, tuple[int, int] | None]:
    """The line where [table] sets `key` (None: it doesn't), and the table's lines (None: no such table)."""
    span = edit.tables().get((table,))
    if span is None:
        return None, None
    return edit.key_line(span[0] + 1, span[1], key, f"[{table}]"), span


def add_to_list(path: Path, table: str, key: str, item: str, note: str = "") -> bool:
    """Put `item` in [table]'s `key` list on a line of its own (`"item",  # note`), the rest of the file as it
    was, the host's comments included. False: it's in the list already (any case)."""
    edit = _Edit(path, f"[{table}] {key}")
    current = edit.wanted.setdefault(table, {})
    if not isinstance(current, dict):
        raise edit.refuse(f"{table} isn't a table there")
    values = current.setdefault(key, [])
    if not isinstance(values, list):
        raise edit.refuse(f"[{table}] {key} isn't a list")
    if any(isinstance(v, str) and v.lower() == item.lower() for v in values):
        return False
    values.append(item)
    entry = f"    {_toml_string(item)},"
    if note:
        entry += f"  # {note.replace(chr(10), ' ')}"
    number, span = _list_line(edit, table, key)
    if span is None:  # no such table yet: at the end, as the file's own sections are written
        while edit.lines and not edit.lines[-1].strip():
            edit.lines.pop()
        edit.lines += ["", f"[{_key(table)}]", f"{_key(key)} = [", entry, "]"]
    elif number is None:  # the table, but no list in it: after its last line
        insert = span[1]
        while insert > span[0] + 1 and edit.layout[insert - 1].kind == "blank":
            insert -= 1
        edit.lines[insert:insert] = [f"{_key(key)} = [", entry, "]"]
    else:
        layout = edit.layout[number]
        if layout.end == number:  # all on one line (`players = []`, `players = ["a", "b"]`): one per line now
            line = edit.lines[number]
            comment = _comment(line[layout.at:])
            rest = [f"    {_toml_string(v)}," for v in values[:-1] if isinstance(v, str)]
            if len(rest) != len(values) - 1:
                raise edit.refuse(f"[{table}] {key} holds something that isn't text")
            edit.lines[number:number + 1] = [line[:layout.at] + "[" + comment, *rest, entry, "]"]
        else:  # several lines: the new one just before the closing bracket
            close = layout.end
            if not edit.lines[close].strip().startswith("]"):
                raise edit.refuse(f"[{table}] {key} doesn't end with ] on a line of its own")
            last = close - 1  # the last element must end with a comma before another comes after it
            while last > number and (not edit.lines[last].strip() or edit.lines[last].lstrip().startswith("#")):
                last -= 1
            line = edit.lines[last]
            start = layout.at if last == number else 0
            _, _, at = _scan(line[start:], 0 if last == number else 1)
            value = line if at is None else line[:start + at]
            if value.strip() and not value.rstrip().endswith((",", "[")):
                edit.lines[last] = value.rstrip() + "," + line[len(value.rstrip()):]
            edit.lines.insert(close, entry)
    edit.changed()
    edit.save()
    return True


def remove_from_list(path: Path, table: str, key: str, item: str) -> bool:
    """Take `item` (any case) out of [table]'s `key` list: its own line goes, comment and all; the rest of the
    file as it was. False: it isn't in the list."""
    edit = _Edit(path, f"[{table}] {key}")
    values = edit.wanted.get(table, {}).get(key) if isinstance(edit.wanted.get(table), dict) else None
    if not isinstance(values, list) or not any(isinstance(v, str) and v.lower() == item.lower() for v in values):
        return False
    edit.wanted[table][key] = [v for v in values if not (isinstance(v, str) and v.lower() == item.lower())]
    number, _ = _list_line(edit, table, key)
    assert number is not None  # the value is there, so its line is
    layout = edit.layout[number]
    quoted = re.compile(r"""^\s*(["'])""" + re.escape(item) + r"""\1\s*,?\s*(#.*)?$""", re.IGNORECASE)
    if layout.end == number:  # one line: written again without it (it had no per-entry comments to keep)
        line = edit.lines[number]
        edit.lines[number] = line[:layout.at] + _toml_value(edit.wanted[table][key]) + _comment(line[layout.at:])
    else:
        lines = [n for n in range(number + 1, layout.end) if quoted.match(edit.lines[n])]
        if not lines:
            raise edit.refuse(f"{item} isn't on a line of its own in [{table}] {key}")
        for n in reversed(lines):
            del edit.lines[n]
    edit.changed()
    edit.save()
    return True


def render_server_block(values: dict[str, Any]) -> list[str]:
    """A [[server]] block's lines; `name` first, then `port`, then the rest in the order given."""
    ordered = {k: values[k] for k in ("name", "port") if k in values}
    ordered.update({k: v for k, v in values.items() if k not in ordered})
    return ["[[server]]", *(f"{_key(k)} = {_toml_value(v)}" for k, v in ordered.items())]


def replace_server_blocks(path: Path, blocks: list[dict[str, Any]]) -> None:
    """Take every [[server]] block out and put these in, at the end, after the comments that explained them."""
    edit = _Edit(path, "the [[server]] blocks")
    if blocks:
        edit.wanted["server"] = [_plain(block) for block in blocks]
    else:
        edit.wanted.pop("server", None)
    for block in reversed(edit.blocks()):
        del edit.lines[block.start:block.last]
    while edit.lines and not edit.lines[-1].strip():
        edit.lines.pop()
    for block in blocks:
        edit.lines.append("")
        edit.lines.extend(render_server_block(block))
    edit.save()


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def server_blocks(path: Path) -> list[dict[str, Any]]:
    blocks = read_toml(path).get("server", [])
    return [dict(b) for b in blocks] if isinstance(blocks, list) else []
