"""What the host types into the supervisor's window when it has no dashboard (--no-menu):
status, start/stop/restart <server>, say, update, check, stop, quit.

Only when the window is a real console; under Task Scheduler, systemd or Docker
there's nothing to read, and `start.cmd stop`, `restart`, `say`, `status` do the
same from outside.

A server name with spaces can be "quoted" (say "Big Team" hello); for `say` it's
passed on as the server's file name (big-team), which every command knows, since
`say` takes one word for the server. Windows sends typed text in the console's
own code page (850, 437...), so it's decoded with that, not as UTF-8.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
from typing import Callable

from .config import slugify

HELP = ("Type here: status · start|stop|restart|cancel <server|all> [--now] · say <server|all> <text> · "
        "update · check (for a new version of Huragok) · stop (everything) · close (servers keep running). "
        "A server name with spaces goes in \"quotes\"")
QUOTED = re.compile(r"""^(["'])(.+?)\1(?=\s|$)""")  # a "quoted name" at the start of what follows the command
NAMED = ("start", "stop", "restart", "cancel", "say")  # the commands that name a server first


def interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def tidy(line: str) -> str:
    """A typed line with a "quoted" server name made plain: `stop "Big Team"` -> `stop Big Team`, and
    `say "Big Team" hi` -> `say big-team hi` (say reads one word as the server). The rest is left as typed: chat
    text keeps its apostrophes and quotes."""
    line = line.strip()
    word, _, rest = line.partition(" ")
    match = QUOTED.match(rest.strip())
    if word.lower() not in NAMED or not match:
        return line
    name, after = match.group(2).strip(), rest.strip()[match.end():].strip()
    if word.lower() == "say" and any(c.isspace() for c in name):
        name = slugify(name)
    return f"{word} {name} {after}".strip()


def decode(data: bytes) -> str:
    """Typed bytes as text: on Windows in the console's input code page (unless that's UTF-8), elsewhere UTF-8."""
    if os.name == "nt":
        try:
            import ctypes

            page = int(ctypes.windll.kernel32.GetConsoleCP())  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            page = 0
        if page and page != 65001:
            try:
                return data.decode(f"cp{page}", errors="replace")
            except LookupError:
                pass
    return data.decode("utf-8", errors="replace")


class Console:
    def __init__(self, loop: asyncio.AbstractEventLoop, handle: Callable[[str], None]) -> None:
        self.loop = loop
        self.handle = handle

    def start(self) -> None:
        threading.Thread(target=self._read, name="console", daemon=True).start()

    def _read(self) -> None:
        # os.read, not sys.stdin: a thread waiting inside sys.stdin's buffer can make
        # Python fail on exit ("could not acquire lock for <stdin>").
        pending = b""
        while True:
            try:
                chunk = os.read(0, 1024)
            except OSError:
                return
            if not chunk:
                return  # nothing more to read
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                text = tidy(decode(line))
                if text:
                    self.loop.call_soon_threadsafe(self.handle, text)
