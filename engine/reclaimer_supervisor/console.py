"""What the host types into the supervisor's window: update, check, status, stop, help.

Only when the window is a real console (a double-clicked start.cmd, a terminal);
under Task Scheduler or systemd there's nothing to read, and `start.cmd update` /
`start.cmd stop` do the same from outside.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Callable

HELP = "Type here: update (install a new version), check (look for one), status, stop (with the servers), help"


def interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


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
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self.loop.call_soon_threadsafe(self.handle, text)
