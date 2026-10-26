"""Read the server's files by path, the same way on Windows and Linux.

- data/<server>/server.log: known lines become log.* events (PATTERNS). It rolls
  over to server.log.old at 20 MB; the tail follows. Lines start with the UTC time.
- data/<server>/status.json: rewritten every 5 s while the server runs. Its
  `updated` going stale is the surest sign of a stuck server.
- the built-in supervisor's console (when our backend runs it): CONSOLE_PATTERNS.

Every file is UTF-8, whatever the console code page says. On Windows the files are
opened so the server can still rename or replace them while Huragok reads (log
rotation, status.json's rewrite): Python's own open() would make that fail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, BinaryIO

from ..core.events import EventBus
from ..core.serverfolders import ServerConfig
from .state import ServerState

log = logging.getLogger(__name__)

CLOCK = r"(?:(?P<clock>\d\d:\d\d:\d\d) )?"

# Lines that carry someone's own words: a console command echoed ("RCON 127.0.0.1:60106: say …", or "… as
# Alice: …"), a refused join with the ban's reason, a kick or ban with its reason. They're looked at first, so
# what someone typed can never pass for one of the server's own lines (a `say Host roster lost 99 of 99 …`
# would otherwise be a dead server, restarted with no warning). None: nothing to report.
ECHOES: list[tuple[str | None, re.Pattern[str]]] = [
    ("log.rcon_command", re.compile(r"^RCON (?P<address>\S+?)(?: as (?P<by>.+?))?: (?P<command>.+)$")),
    (None, re.compile(r"^(?:Refused |Public admission rejected: )")),
    (None, re.compile(r"^.+? (?:kicked|banned|muted|unmuted|unbanned|pardoned) .*?\(player ID ")),
]

# server.log: (event name, pattern). Matched at the start of the message, after the "HH:MM:SS " prefix.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("log.falling_behind", re.compile(
        r"^Server falling behind: under (?P<fps>\d+) frames a second for (?P<behind>\d+) of the last "
        r"(?P<window>\d+) seconds(?: \((?P<average>[\d.]+)[^)]*?)?.*?longest frame (?P<longest_ms>\d+) ms"
        r".*?with (?P<players>\d+) players")),
    ("log.upload", re.compile(
        r"^Network upload over the last (?P<minutes>\d+) minutes: (?P<rate>[\d.]+) (?P<unit>[KMG]?B)/s "
        r"to (?P<players>\d+) players")),
    # Most or all players dropped at once; details is the engine's own JSON snapshot.
    ("log.roster_lost", re.compile(
        r"^Host roster lost (?P<lost>\d+) of (?P<of>\d+) players? in one update \((?P<present>\d+) now present\)"
        r"(?:: (?P<details>.+))?$")),
    ("log.rcon_signin", re.compile(r"^RCON: (?P<address>\S+) signed in \((?P<how>[^)]*)\)")),
    ("log.rcon_refused", re.compile(r"^RCON: (?P<address>\S+) was turned away: (?P<reason>.+?)\.?$")),
    # "RCON: 127.0.0.1:60106 gave a wrong password." and, at the fifth, "… gave a wrong password 5 times; refusing
    # 127.0.0.1 for 10 minutes." (0.9.18). The console's own refusal reads "wrong password" either way.
    ("log.rcon_wrong_password", re.compile(
        r"^RCON: (?P<address>\S+) gave a wrong password(?: (?P<times>\d+) times; refusing (?P<refused>\S+) "
        r"for (?P<minutes>\d+) minutes?)?\.?$")),
    ("log.reload", re.compile(r"^Reload result: (?P<text>.+)$")),
    ("log.online", re.compile(r"^Online: players join at (?P<address>\S+?)\.?$")),
    # A player getting in: connected (past the admission check), then, once the map has loaded, joined.
    ("log.connected", re.compile(r"^(?P<name>.+?) connected from \S+ \(player ID (?P<id>[0-9a-fA-F]+)")),
    ("log.joined", re.compile(r"^Community roster: (?P<name>.+) joined the server$")),
    # On a Halo 3: ODST server (0.9.19) a player getting into the game says so in ODST's own words.
    ("log.joined", re.compile(r"^ODST: player (?P<name>.+?) joined the game in progress\b")),
    # A playlist's ballot: opened (the playlist's name, as the lobby shows it), the game players picked ("BR Fiesta
    # CTF on HugeGrass Rebalance · Custom"), and how many of that game's own rules and options were applied.
    ("log.ballot_opened", re.compile(r"^Playlist lobby voting opened: (?P<playlist>.+?)$")),
    ("log.ballot_picked", re.compile(r"^Playlist selected: (?P<picked>.+?)(?: · (?P<kind>[^·]+?))?$")),
    ("log.game_overrides", re.compile(r"^Applied (?P<count>\d+) native game option overrides for (?P<mode>\S+)\.?$")),
    # A player's vote the server turned down as they called it: "Bravo could not call a vote: Alice can't be
    # vote-kicked on this server." (or another vote is under way, their last one failed…).
    ("log.vote_refused", re.compile(r"^(?P<caller>.+?) could not call a vote: (?P<reason>.+?)$")),
]

# server.log: what the program says about mods and content it couldn't use, as log.mod_problem events (kind, text).
# Some come again every couple of minutes, as the program looks at its content again: the Supervisor says each once a
# start. (A "Refused custom cache data of …" line can't be told from a refused player's here: see ECHOES.)
MOD_LINES: list[tuple[str, re.Pattern[str]]] = [
    ("content", re.compile(r"^Skipped content file (?P<path>.+?): (?P<reason>.+?)\.?$")),
    ("characters", re.compile(r"^Customization mod (?P<mod>.+?) left out: (?P<reason>.+?)\.?$")),
    ("characters", re.compile(r"^No customization mod is named (?P<mod>.+?): its characters are left out\.?$")),
    ("characters", re.compile(r"^Customization: could not dress a player's character: (?P<reason>.+?)\.?$")),
    ("package", re.compile(r"^Mod package (?P<mod>.+?) left out: (?P<reason>.+?)\.?$")),
    ("not_shared", re.compile(r"^.*\bthis server's games use mods it does not share \((?P<mod>.+?)\)")),
    ("too_big", re.compile(r"^This server's game has more Forge objects, characters and mods than a join can carry")),
    ("packs", re.compile(r"^(?:Characters not offered|Forge objects not offered as packs): (?P<reason>.+?)\.?$")),
    ("map_slots", re.compile(r"^(?:Too many custom maps; (?P<count>\d+) left out|Halo 3 could not list the map"
                             r"|Too many custom maps for one session)")),
    ("map_id", re.compile(r"^(?:Custom map (?P<mod>.+?) shares its engine index with another custom map; left out"
                          r"|Maps whose scenario declares no map ID are unavailable: (?P<maps>.+?))\.?$")),
    # (Not "Forge library unavailable: the engine refused it (error 6).": test servers have said it at most starts
    # since 0.1.0, with nothing seen to go wrong; noise until shown otherwise.)
    ("forge", re.compile(r"^(?:Left out of the Forge library: (?P<maps>.+?)|Left out the Forge objects of (?:the "
                         r"(?:mod|campaign) map )?(?P<map>.+?)(?:: (?P<reason>.+?))?)\.?$")),
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
    # Its own updater (auto_update): on or not at the start, downloading, failing (it tries again in an hour).
    ("server.self_update", re.compile(
        CLOCK + r"(?P<text>Automatic updates (?:enabled|requested, but the update feed is disabled).*?)\.?$")),
    ("server.update_downloading", re.compile(CLOCK + r"Downloading Project Reclaimer v?(?P<version>\d[\w.+-]*?)\.*$")),
    ("server.update_failed", re.compile(
        CLOCK + r"(?:Automatic update failed; retrying in an hour|The update check failed): (?P<reason>.+?)\.?$")),
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


# Groups that are text even when they're all digits: a player called "007" is not the number 7.
TEXT_GROUPS = {"clock", "id", "name", "caller", "by", "text", "reason", "server", "address", "command", "details",
               "path", "version", "how", "unit", "playlist", "picked", "kind", "mode"}  # names: "1.10" stays "1.10"


def _clean(groups: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in groups.items():
        if value is None:
            continue
        if key in TEXT_GROUPS:
            data[key] = value
        elif re.fullmatch(r"\d+", value):
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
    for name, pattern in ECHOES:
        if match := pattern.match(line):
            if name is None:
                return None
            return name, {**_clean(match.groupdict()), **({"clock": clock} if clock else {})}
    for name, pattern in PATTERNS:
        if match := pattern.match(line):
            data = _clean(match.groupdict())
            if name == "log.rcon_signin":
                data["host_slot"] = "server's machine" in data.get("how", "")
            if name == "log.connected":
                data["id"] = data["id"].lower()
            if clock:
                data["clock"] = clock
            return name, data
    for kind, pattern in MOD_LINES:
        if match := pattern.match(line):
            data = {key: value for key, value in match.groupdict().items() if value is not None}
            return "log.mod_problem", {**data, "kind": kind, "text": line.strip(),
                                       **({"clock": clock} if clock else {})}
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


_kernel32: Any = None


def _open_windows(path: Path) -> BinaryIO:
    """CreateFileW with every share flag, so a rename or replace by the server goes through while we read."""
    global _kernel32
    import ctypes
    import msvcrt
    from ctypes import wintypes

    if _kernel32 is None:  # our own copy, so these argtypes never reach anyone else's ctypes.windll.kernel32
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32 = kernel32
    generic_read, share_all, open_existing, normal = 0x80000000, 0x1 | 0x2 | 0x4, 3, 0x80
    handle = _kernel32.CreateFileW(str(path), generic_read, share_all, None, open_existing, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())  # an OSError: FileNotFoundError for a missing file
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        _kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def open_shared(path: Path) -> BinaryIO:
    """Open a file to read it without getting in the way of the server renaming, replacing or deleting it."""
    if os.name == "nt":
        try:
            return _open_windows(path)
        except OSError:
            raise
        except Exception as error:  # noqa: BLE001 - ctypes trouble: Python's own open() still reads it
            log.debug("couldn't open %s for shared reading (%s); using open()", path, error)
    return path.open("rb")


class FileTail:
    """Follow a growing text file by polling; survives rotation, truncation and absence.

    A file that's there at the first look is followed from its end (what came before
    Huragok started is history); one that isn't there yet is new when it appears, so
    it's read from its start (a new server's first `Online:` and connections).
    """

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
            if self._offset is None:
                self._offset = 0  # not there at the first look: read all of it once it appears
            return []
        identity = (stat.st_dev, stat.st_ino)
        lines: list[bytes] = []
        if self._offset is None:  # there at the first look: start at the end
            self._offset, self._identity = stat.st_size, identity
            return []
        if identity != self._identity or stat.st_size < self._offset:
            # Rolled over (or truncated): finish the old file, then start the new one. The old file's last line
            # ends there, newline or not; a piece of a line we can't finish is dropped, never glued to the next.
            rest = self._rest_of_rotated() if identity != self._identity else None
            if rest is not None:
                lines.extend(piece for piece in (self._partial + rest).split(b"\n") if piece)
            self._partial = b""
            self._offset, self._identity = 0, identity
        try:
            with open_shared(self.path) as file:
                file.seek(self._offset)
                chunk = file.read()
                self._offset = file.tell()
        except OSError:
            chunk = b""
        *new, self._partial = (self._partial + chunk).split(b"\n")
        lines.extend(new)
        return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]

    def _rest_of_rotated(self) -> bytes | None:
        """What was added to the file we followed after our last look, now that it's been renamed."""
        if self.rotated is None or self._identity is None or self._offset is None:
            return None
        try:
            old = self.rotated.stat()
            if (old.st_dev, old.st_ino) != self._identity:
                return None
            with open_shared(self.rotated) as file:
                file.seek(self._offset)
                return file.read()
        except OSError:
            return None


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        with open_shared(path) as file:
            status = json.loads(file.read().decode("utf-8"))
    except (OSError, ValueError):
        return None  # missing, or caught mid-write: try again next time
    return status if isinstance(status, dict) else None


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
