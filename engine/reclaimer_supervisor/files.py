"""Read the server's files by path, the same way on Windows and Linux.

- data/<server>/server.log: known lines become log.* events (PATTERNS). It rolls
  over to server.log.old at 20 MB; the tail follows. Lines start with the UTC time.
- data/<server>/status.json: rewritten every 5 s while the server runs. Its
  `updated` going stale is the surest sign of a stuck server.
- the built-in supervisor's console (when our backend runs it): CONSOLE_PATTERNS.

Every file is UTF-8, whatever the console code page says.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import ServerConfig
from .events import EventBus
from .state import ServerState

log = logging.getLogger(__name__)

CLOCK = r"(?:(?P<clock>\d\d:\d\d:\d\d) )?"

# server.log: (event name, pattern). Matched after the "HH:MM:SS " prefix.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("log.falling_behind", re.compile(
        r"Server falling behind: under (?P<fps>\d+) frames a second for (?P<behind>\d+) of the last "
        r"(?P<window>\d+) seconds(?: \((?P<average>[\d.]+)[^)]*?)?.*?longest frame (?P<longest_ms>\d+) ms"
        r".*?with (?P<players>\d+) players")),
    ("log.upload", re.compile(
        r"Network upload over the last (?P<minutes>\d+) minutes: (?P<rate>[\d.]+) (?P<unit>[KMG]?B)/s "
        r"to (?P<players>\d+) players")),
    # Most or all players dropped at once; details is the engine's own JSON snapshot.
    ("log.roster_lost", re.compile(
        r"Host roster lost (?P<lost>\d+) of (?P<of>\d+) players? in one update \((?P<present>\d+) now present\)"
        r"(?:: (?P<details>.+))?$")),
    ("log.rcon_signin", re.compile(r"RCON: (?P<address>\S+) signed in \((?P<how>[^)]*)\)")),
    ("log.rcon_refused", re.compile(r"RCON: (?P<address>\S+) was turned away: (?P<reason>.+?)\.?$")),
    ("log.rcon_command", re.compile(r"RCON (?P<address>\S+) as (?P<by>.+?): (?P<command>.+)$")),
    ("log.reload", re.compile(r"Reload result: (?P<text>.+)$")),
    ("log.online", re.compile(r"Online: players join at (?P<address>\S+?)\.?$")),
]

# The built-in supervisor's console: (event name, pattern).
CONSOLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("server.started", re.compile(
        CLOCK + r"\[(?P<server>[^\]]+)\] started \(process (?P<pid>\d+)\); players join at (?P<address>\S+?)\.?$")),
    ("server.crashed", re.compile(
        CLOCK + r"\[(?P<server>[^\]]+)\] stopped: (?P<reason>.+?); restarting in (?P<wait>.+?)\. See")),
    ("server.reloaded", re.compile(CLOCK + r"Configuration reloaded\.")),
    ("server.all_stopped", re.compile(CLOCK + r"All servers stopped\.")),
    ("server.updating", re.compile(CLOCK + r"Update installed; restarting all servers\.")),
    # Workshop items being fetched or checked: "Workshop 2984061723: acquiring from Steam" / "ready (…)" /
    # "installed" / an Error line, and a mod map the server can't use.
    ("server.workshop", re.compile(CLOCK + r"(?:Error: )?Workshop (?P<item>\d+): (?P<text>.+?)\.?$")),
    ("server.map_skipped", re.compile(CLOCK + r"Skipped map package (?P<path>.+?): (?P<reason>.+?)\.?$")),
]

CRASH_KINDS = [
    ("hang", "stopped responding"),
    ("menus", "return to MCC's menus"),
    ("error", "could not recover"),
    ("panic", "panic"),
    ("engine_exit", "Engine probe worker failed"),
]


def _clean(groups: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in groups.items():
        if value is None:
            continue
        if re.fullmatch(r"\d+", value) and key not in ("clock",):
            data[key] = int(value)
        elif re.fullmatch(r"\d+\.\d+", value):
            data[key] = float(value)
        else:
            data[key] = value
    return data


def parse_log_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """A server.log line -> (event name, data), or None if it isn't one we know."""
    clock = None
    if re.match(r"\d\d:\d\d:\d\d ", line):
        clock, line = line[:8], line[9:]
    for name, pattern in PATTERNS:
        if match := pattern.search(line):
            data = _clean(match.groupdict())
            if name == "log.rcon_signin":
                data["host_slot"] = "server's machine" in data.get("how", "")
            if clock:
                data["clock"] = clock
            return name, data
    return None


def parse_console_line(line: str) -> tuple[str, dict[str, Any]] | None:
    """A line from `dedicated`'s console -> (event name, data incl. 'server' when named)."""
    for name, pattern in CONSOLE_PATTERNS:
        if match := pattern.match(line.strip()):
            data = _clean(match.groupdict())
            if name == "server.crashed":
                data["kind"] = next((k for k, text in CRASH_KINDS if text in data["reason"]), "other")
            return name, data
    return None


class FileTail:
    """Follow a growing text file by polling; survives rotation, truncation and absence."""

    def __init__(self, path: Path, rotated: Path | None = None, from_start: bool = False) -> None:
        self.path = path
        self.rotated = rotated
        self._offset: int | None = 0 if from_start else None
        self._identity: tuple[int, int] | None = None
        self._partial = b""

    def _stat(self) -> os.stat_result | None:
        try:
            return self.path.stat()
        except OSError:
            return None

    def read_lines(self) -> list[str]:
        stat = self._stat()
        if stat is None:
            return []
        identity = (stat.st_dev, stat.st_ino)
        chunks: list[bytes] = []
        if self._offset is None:  # first look: start at the end
            self._offset, self._identity = stat.st_size, identity
            return []
        if identity != self._identity or stat.st_size < self._offset:
            # Rolled over (or truncated): finish the old file, then start the new one.
            if self.rotated is not None and self._identity is not None:
                try:
                    old = self.rotated.stat()
                    if (old.st_dev, old.st_ino) == self._identity:
                        with self.rotated.open("rb") as file:
                            file.seek(self._offset)
                            chunks.append(file.read())
                except OSError:
                    pass
            self._offset, self._identity = 0, identity
        try:
            with self.path.open("rb") as file:
                file.seek(self._offset)
                chunks.append(file.read())
                self._offset = file.tell()
        except OSError:
            return []
        data = self._partial + b"".join(chunks)
        *lines, self._partial = data.split(b"\n")
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # missing, or caught mid-write: try again next time


class FileWatcher:
    """Turns one server's server.log into log.* events and tracks status.json."""

    def __init__(self, server: ServerConfig, state: ServerState, bus: EventBus,
                 poll_seconds: float = 1.0, status_seconds: float = 5.0) -> None:
        self.server = server
        self.state = state
        self.bus = bus
        self.poll_seconds = poll_seconds
        self.status_seconds = status_seconds
        self.tail = FileTail(server.log_path, server.data_dir / "server.log.old")

    def poll_log(self) -> None:
        for line in self.tail.read_lines():
            parsed = parse_log_line(line)
            if parsed:
                name, data = parsed
                self.bus.emit(name, self.server.name, **data)

    def poll_status(self) -> None:
        status = read_status(self.server.status_path)
        if status is None:
            return
        self.state.status_seen = time.time()
        updated = status.get("updated")
        if isinstance(updated, (int, float)):
            self.state.status_updated = float(updated)

    async def run(self) -> None:
        next_status = 0.0
        while True:
            self.poll_log()
            if time.monotonic() >= next_status:
                self.poll_status()
                next_status = time.monotonic() + self.status_seconds
            await asyncio.sleep(self.poll_seconds)
