"""intervalmessages.txt: one message per line, how often then the message, said to everyone on the server.

    30m | Join us on Discord: discord.gg/yourserver
    1h  | Be nice: no slurs, no cheating.

Huragok's folder has one for every server, and each server folder one of its own: a server says both. A line that
can't be read is left out and named (check, the log); the others still go out. No file: no messages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from ...core.files import read_text
from ...core.text import UNITS

FILE = "intervalmessages.txt"  # in Huragok's folder (every server) and in each server's folder (its own)
TEMPLATES = {"every": "templates/intervalmessages.txt", "server": "templates/intervalmessages-server.txt"}
MIN_SECONDS = 60  # once a minute at most: any more often and the chat is nothing but these
EVERY = re.compile(r"(?:\d+[smhd])+")
PIECE = re.compile(r"(\d+)([smhd])")
LAYOUT = "how often | the message"


class MessageError(ValueError):
    pass


@dataclass(frozen=True)
class IntervalMessage:
    every: str  # as written: "30m", "1h30m"
    seconds: int
    text: str  # as players read it
    line: int  # its line in the file


def seconds_every(text: str) -> int:
    """'30m' -> 1800, '1h30m' -> 5400. Raises MessageError for anything else, or for less than MIN_SECONDS."""
    every = text.strip().lower().replace(" ", "")
    if not EVERY.fullmatch(every):
        raise MessageError(f"'{text.strip()}' isn't a time: 30s, 10m, 1h, 1d, or put together, 1h30m")
    seconds = sum(int(number) * UNITS[unit][1] for number, unit in PIECE.findall(every))
    if seconds < MIN_SECONDS:
        raise MessageError(f"every {describe_every(seconds)} is too often: once a minute at most")
    return seconds


def describe_every(seconds: float) -> str:
    """5400 -> '1 hour 30 minutes', 1800 -> '30 minutes', 45 -> '45 seconds'."""
    left = int(seconds)
    words = []
    for unit in ("d", "h", "m", "s"):
        name, size = UNITS[unit]
        if left >= size:
            count, left = divmod(left, size)
            words.append(f"{count} {name}" + ("" if count == 1 else "s"))
    return " ".join(words) or "0 seconds"


def parse_line(text: str, number: int) -> IntervalMessage | None:
    """One line: an IntervalMessage, None for a blank line or a note, MessageError for one that can't be read."""
    line = text.strip()
    if not line or line.startswith("#"):
        return None
    every, bar, message = line.partition("|")
    if not bar:
        raise MessageError(f"has no |: write it as {LAYOUT} (30m | Join us on Discord)")
    message = message.strip()
    if not message:
        raise MessageError(f"has no message after the |: write it as {LAYOUT}")
    return IntervalMessage(every.strip().lower().replace(" ", ""), seconds_every(every), message, number)


def parse_text(text: str) -> tuple[list[IntervalMessage], list[str]]:
    """Every message in the text, and a line for each that can't be read ("line 4 has no |: …")."""
    messages: list[IntervalMessage] = []
    problems: list[str] = []
    for number, line in enumerate(text.split("\n"), 1):
        try:
            message = parse_line(line, number)
        except MessageError as error:
            problems.append(f"line {number} {error}" if str(error).startswith("has ") else f"line {number}: {error}")
            continue
        if message:
            messages.append(message)
    return messages, problems


def read_messages(path: Path) -> tuple[list[IntervalMessage], list[str]]:
    """The file's messages and its problems, each problem starting with the file's name. No file: none of either.
    One saved in an encoding that isn't UTF-8 is a MessageError that says so; OSError passes through."""
    try:
        text = read_text(path, MessageError)
    except FileNotFoundError:
        return [], []
    messages, problems = parse_text(text)
    return messages, [f"{path.name} {problem}" for problem in problems]


def own_file(folder: Path | None, home: Path) -> Path | None:
    """A server's own intervalmessages.txt, in its folder; None for a server whose folder is Huragok's own (the 0.1
    layout, server_folder = ".."), where that file is the one for every server."""
    if folder is None:
        return None
    try:
        if folder.resolve() == home.resolve():
            return None
    except OSError:
        return None
    return folder / FILE


def summary(messages: list[IntervalMessage]) -> str:
    """'2: every 30 minutes, every 1 hour', or 'none yet'."""
    if not messages:
        return "none yet"
    return f"{len(messages)}: " + ", ".join(f"every {describe_every(m.seconds)}" for m in messages)


def overview(home: Path, servers: list[tuple[str, Path | None]]) -> tuple[list[tuple[str, str]], list[str]]:
    """For `check`: a row for the file for every server and one for each server's own (name, folder), and every
    line left out (with its file)."""
    rows: list[tuple[str, str]] = []
    problems: list[str] = []
    for where, path in [("every server", home / FILE)] + [(name, own_file(folder, home)) for name, folder in servers]:
        if path is None:
            continue
        try:
            messages, found = read_messages(path)
        except (MessageError, OSError) as error:
            rows.append((where, f"can't be read: {error}"))
            continue
        rows.append((where, summary(messages) if where == "every server" or messages else "none of its own"))
        problems.extend(line if where == "every server" else f"{where}: {line}" for line in found)
    return rows, problems


def template(kind: str = "every") -> str:
    """The file as `init` (Huragok's folder: "every") and a new server ("server") write it."""
    return resources.files("huragok").joinpath(TEMPLATES[kind]).read_text("utf-8")


def write_template(path: Path, kind: str = "every") -> bool:
    """A missing file written with its notes and no messages; one that's there is never touched. Whether it wrote."""
    if path.exists():
        return False
    path.write_text(template(kind), encoding="utf-8")
    return True


def message_line(every: str, text: str) -> str:
    """The line for a message, checked: '30m | Join us on Discord'. Raises MessageError."""
    text = " ".join(text.split())
    if not text:
        raise MessageError("there's no message")
    seconds_every(every)
    return f"{every.strip().lower().replace(' ', '')} | {text}"


def add_message(path: Path, every: str, text: str, kind: str = "every") -> IntervalMessage:
    """Add a message at the end (a missing file is written with its notes first; the file is written back as UTF-8,
    whatever it was saved as). Raises MessageError or OSError."""
    line = message_line(every, text)
    try:
        current = read_text(path, MessageError)
    except FileNotFoundError:
        current = template(kind)
    if current and not current.endswith("\n"):
        current += "\n"
    current += line + "\n"
    path.write_text(current, encoding="utf-8")
    number = current.count("\n")
    return IntervalMessage(line.split(" | ", 1)[0], seconds_every(every), line.split(" | ", 1)[1], number)


def remove_message(path: Path, message: IntervalMessage) -> bool:
    """Take a message's line out, if it's still there as it was read. Whether it was. Raises MessageError or OSError."""
    try:
        lines = read_text(path, MessageError).split("\n")
    except FileNotFoundError:
        return False
    number = message.line
    if not 1 <= number <= len(lines):
        return False
    try:
        found = parse_line(lines[number - 1], number)
    except MessageError:
        found = None
    if found is None or (found.seconds, found.text) != (message.seconds, message.text):
        return False
    del lines[number - 1]
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


__all__ = ["FILE", "LAYOUT", "MIN_SECONDS", "IntervalMessage", "MessageError", "add_message",
           "describe_every", "message_line", "overview", "own_file", "parse_line", "parse_text", "read_messages",
           "remove_message", "seconds_every", "summary", "template", "write_template"]
