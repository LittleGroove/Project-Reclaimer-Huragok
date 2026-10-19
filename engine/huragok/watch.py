"""Follow a log file in a window of its own: `start.cmd watch <server>` or `watch supervisor`.

Lines are coloured by what they say, with the same rules as the dashboard's events: the time
and level in gray, problems red, warnings yellow, a server coming up or a reload green, stops
and restarts magenta, players and the Workshop cyan. Plain when the output isn't a console.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Callable

from .shell import console as out
from .tui import Parts, content_colour

SUPERVISOR_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d+)? (\w+)\s+(.*)$")  # logs/huragok.log
CONSOLE_TIME = re.compile(r"^(\d\d:\d\d:\d\d) (.*)$")  # the server's console, when it stamps a line
LEVELS = {"ERROR": "red", "CRITICAL": "red", "WARNING": "yellow"}
SESSION_MARKS = ("──── ", "--- ")  # the line the Superintendent writes into the console log at each start


def colour_for(line: str) -> str | None:
    return content_colour(line)


def parts_for(line: str) -> Parts:
    """A log line as coloured parts: a gray time (and level), then the message in its colour."""
    if line.startswith(SESSION_MARKS):
        return [(line, "white")]  # a new session's marker in the console log
    match = SUPERVISOR_LINE.match(line)
    if match:
        when, level, message = match.groups()
        colour = LEVELS.get(level) or content_colour(message)
        return [(f"{when} ", "gray"), (f"{level:<7} ", LEVELS.get(level, "gray")), (message, colour)]
    match = CONSOLE_TIME.match(line)
    if match:
        when, message = match.groups()
        return [(f"{when} ", "gray"), (message, content_colour(message))]
    return [(line, content_colour(line))]


def follow(path: Path, *, tail_bytes: int = 32 * 1024, poll: float = 0.5, once: bool = False,
           stop: Callable[[], bool] | None = None) -> None:
    """Print the end of the file, then every line as it's added. `stop` is asked after each look (tests)."""
    out.heading(f"Following {path.name}", note=str(path))
    out.note("Closing this window does NOT stop anything.")
    out.write()
    offset = -1
    rest = b""
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            if once or (stop is not None and stop()):
                return
            time.sleep(poll)
            continue
        if offset < 0:
            offset = max(0, size - tail_bytes)
            rest = b""
            skip_first = offset > 0
        else:
            skip_first = False
        if size < offset:
            out.heading("the file was re-created (a new start)")
            offset, rest = 0, b""
        if size > offset:
            try:
                with path.open("rb") as file:
                    file.seek(offset)
                    chunk = file.read(min(size - offset, 4 * 1024 * 1024))
            except OSError:
                time.sleep(poll)
                continue
            offset += len(chunk)
            data = rest + chunk
            *lines, rest = data.split(b"\n")
            if skip_first and lines:
                lines = lines[1:]
            for raw in lines:
                out.parts(parts_for(raw.decode("utf-8", errors="replace").rstrip("\r")))
            sys.stdout.flush()
        if once or (stop is not None and stop()):
            return
        time.sleep(poll)
