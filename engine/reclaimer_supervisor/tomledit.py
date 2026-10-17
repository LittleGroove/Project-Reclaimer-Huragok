"""Change values in a TOML file in place, keeping every comment and the layout.

Used for settings.toml (the window's "toggle autostart" writes `autostart = false`
into the server's [[server]] block) and for a server's dedicated.toml when the
supervisor makes a server folder: top-level keys (`game = "../../game"`), keys in a
table (`[rcon] password`, `[workshop] items`) and the [[server]] blocks themselves.
The file is never rewritten from scratch, so the host's comments stay.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

HEADER = re.compile(r"^\s*\[")
TABLE_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
SERVER_HEADER = re.compile(r"^\s*\[\[\s*server\s*\]\]\s*(#.*)?$")
KEY = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*=\s*(.*?)(\s*#.*)?$")
COMMENTED_KEY = re.compile(r"^\s*#\s*([A-Za-z0-9_-]+)\s*=")


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + " }"
    return json.dumps(str(value), ensure_ascii=False)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _is_header(line: str) -> bool:
    return bool(HEADER.match(line)) and not line.lstrip().startswith("#")


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(first line, line after last) of every [[server]] block."""
    found = []
    start = None
    for number, line in enumerate(lines):
        if _is_header(line):
            if start is not None:
                found.append((start, number))
                start = None
            if SERVER_HEADER.match(line):
                start = number
    if start is not None:
        found.append((start, len(lines)))
    return found


def _tables(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Name -> (header line, line after last) of every [table] (not [[server]] blocks)."""
    found: dict[str, tuple[int, int]] = {}
    current: str | None = None
    start = 0
    for number, line in enumerate(lines):
        if not _is_header(line):
            continue
        if current is not None:
            found[current] = (start, number)
        match = TABLE_HEADER.match(line)
        current, start = (match.group(1), number) if match else (None, number)
    if current is not None:
        found[current] = (start, len(lines))
    return found


def _first_header(lines: list[str]) -> int:
    return next((n for n, line in enumerate(lines) if _is_header(line)), len(lines))


def _set_in_range(lines: list[str], start: int, end: int, key: str, rendered: str) -> bool:
    """Set `key = …` between start and end: the live line, else the commented `#key = …`, else at the end."""
    for number in range(start, end):
        match = KEY.match(lines[number])
        if match and match.group(2) == key:
            lines[number] = f"{match.group(1)}{key} = {rendered}{match.group(4) or ''}"
            return True
    for number in range(start, end):
        match = COMMENTED_KEY.match(lines[number])
        if match and match.group(1) == key:
            lines[number] = f"{key} = {rendered}"
            return True
    insert = end
    while insert > start and not lines[insert - 1].strip():  # before the blank lines that end the section
        insert -= 1
    lines.insert(insert, f"{key} = {rendered}")
    return False


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _write(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_server_setting(path: Path, server: str, key: str, value: object) -> None:
    """Set `key` in the [[server]] block named `server`, adding the block if needed."""
    lines = _read(path)
    rendered = _toml_value(value)
    for start, end in _blocks(lines):
        name_line = None
        for number in range(start + 1, end):
            match = KEY.match(lines[number])
            if match and match.group(2) == "name":
                name_line = number
                if _unquote(match.group(3)).lower() != server.lower():
                    name_line = -1
                break
        if name_line is None or name_line < 0:
            continue
        for number in range(start + 1, end):
            match = KEY.match(lines[number])
            if match and match.group(2) == key:
                lines[number] = f"{match.group(1)}{key} = {rendered}{match.group(4) or ''}"
                break
        else:
            lines.insert(name_line + 1, f"{key} = {rendered}")
        break
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[[server]]", f"name = {_toml_value(server)}", f"{key} = {rendered}"])
    _write(path, lines)


def set_top_key(path: Path, key: str, value: object) -> None:
    """Set a key before the first table header (top-level), replacing its live or commented line."""
    lines = _read(path)
    _set_in_range(lines, 0, _first_header(lines), key, _toml_value(value))
    _write(path, lines)


def set_table_key(path: Path, table: str, key: str, value: object, *, before: str | None = None) -> None:
    """Set a key in [table], making the table when it's missing (before the [before] table, else at the end
    of the top-level keys)."""
    lines = _read(path)
    tables = _tables(lines)
    if table not in tables:
        at = tables[before][0] if before and before in tables else _first_header(lines)
        lines[at:at] = [f"[{table}]", "", ""]
        tables = _tables(lines)
    start, end = tables[table]
    _set_in_range(lines, start + 1, end, key, _toml_value(value))
    _write(path, lines)


def render_server_block(values: dict[str, Any]) -> list[str]:
    """A [[server]] block's lines; `name` first, then `port`, then the rest in the order given."""
    ordered = {k: values[k] for k in ("name", "port") if k in values}
    ordered.update({k: v for k, v in values.items() if k not in ordered})
    return ["[[server]]", *(f"{k} = {_toml_value(v)}" for k, v in ordered.items())]


def replace_server_blocks(path: Path, blocks: list[dict[str, Any]]) -> None:
    """Take every [[server]] block out and put these in, at the end, after the comments that explained them."""
    lines = _read(path)
    for start, end in reversed(_blocks(lines)):
        del lines[start:end]
    while lines and not lines[-1].strip():
        lines.pop()
    for block in blocks:
        lines.append("")
        lines.extend(render_server_block(block))
    _write(path, lines)


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def server_blocks(path: Path) -> list[dict[str, Any]]:
    blocks = read_toml(path).get("server", [])
    return [dict(b) for b in blocks] if isinstance(blocks, list) else []
