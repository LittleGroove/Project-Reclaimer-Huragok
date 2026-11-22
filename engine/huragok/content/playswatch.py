"""What each server plays, watched while Huragok runs (0.7.0, plan section 5 "When it runs"): its file health
(content/filehealth.py) read as Huragok starts, again whenever a file it plays from changes (its dedicated.toml,
playlists, Game Modes, Maps, Images, Mods: looked at every POLL_SECONDS, by size and time), and after a reload; the
findings go into the warnings catalogue under source "plays", so what's put right clears itself.

The server's own program has the last word: `dedicated check` of a draft and `dedicated playlist list`
(program/plays.py plays_verdict). It takes seconds, so it runs in a thread, one server at a time, at most once per
change (once the files have been still for SETTLE_SECONDS), never on the event loop; what it said is kept in
records/plays-checked.json for any window to read. A server whose playlists name a map as `workshop:<item>/<map>` gets
only the list: its check would fetch that item.

What a server says while it plays (servers/logs.py: server.game_skip from its console, log.game_skip from its
server.log) becomes "seen" warnings, kept per game: a game skipped (until its skip runs out, or the server starts
again: the program keeps skips in memory), one it can't play, a map that failed to load and was tried again. The last
week of them is kept in records/games-skipped.json, so G can say how often a game was skipped.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..core.background import wait_until
from ..core.events import Event, EventBus
from ..core.text import one_line
from ..core.tomledit import read_toml
from ..games.gamemodes import GAME_MODES, IMAGES, MAPS
from ..games.plays import plays_block, playlist_refs
from ..program.plays import Verdict, plays_verdict
from .filehealth import (
    SOURCE, ServerFiles, SkipSeen, current_skips, keep_skips, keep_verdict, load_skips, load_verdict, server_files,
)
from .mccimport import content_root_of

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.serverwarnings import Warnings
    from ..core.settings import Settings

log = logging.getLogger(__name__)

POLL_SECONDS = 10.0  # each server's files are looked at this often (sizes and times only)
SETTLE_SECONDS = 5.0  # a change is checked by the program once its files have been still this long
SAME_LINE_SECONDS = 15.0  # one game's line said twice (its console and its server.log) within this counts once
RELOAD = "plays-reload"  # the warnings catalogue's source for a refused reload


def fingerprint(server: ServerConfig, config_name: str = "dedicated.toml") -> tuple[Any, ...]:
    """What changes when a file the server plays from does: each file's size and time, in its dedicated.toml, its
    playlists (its folder's and any its file names elsewhere), and its content folder's Game Modes, Maps, Images,
    disabled-mods.json and each mod's folder, ModInfo.json and Workshop receipt."""
    seen: list[tuple[str, int, int]] = []

    def stat(path: Path) -> None:
        try:
            info = path.stat()
        except OSError:
            seen.append((str(path), -1, -1))
            return
        seen.append((str(path), info.st_size, info.st_mtime_ns))

    def walk(folder: Path) -> None:
        if not folder.is_dir():
            seen.append((str(folder), -1, -1))
            return
        for root, dirs, names in os.walk(folder):
            dirs.sort()
            for name in sorted(names):
                stat(Path(root) / name)

    stat(server.config_path)
    walk(server.folder / "playlists")
    try:
        data = read_toml(server.config_path)
    except (OSError, ValueError):
        data = {}
    blocks = data.get("server", []) if isinstance(data.get("server"), list) else []
    block = next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == server.name.lower()),
                 {})
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else None
    for ref in playlist_refs(plays_block(block, defaults)):
        stat(ref.path(server.folder))
    content = server.content_dir or content_root_of(server.folder, config_name)
    for folder in (GAME_MODES, MAPS, IMAGES):
        walk(content / folder)
    stat(content / "disabled-mods.json")
    mods = content / "Mods"
    if mods.is_dir():
        for top in sorted(mods.iterdir()):
            for folder in ([top, *sorted(p for p in top.iterdir() if p.is_dir())] if top.name.casefold() == "workshop"
                           and top.is_dir() else [top]):
                stat(folder)
                stat(folder / "ModInfo.json")
                stat(folder / "reclaimer-installed.json")
    return tuple(seen)


class PlaysWatch:
    """Every server's file health, kept up to date in the warnings catalogue."""

    def __init__(self, settings: Settings, bus: EventBus, warnings: Warnings, spawn: Callable[..., Any] | None = None,
                 *, tool_for: Callable[[Any, Any], Any] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self.warnings = warnings
        self._spawn = spawn
        self._tool_for = tool_for
        self.clock = clock
        self.reports: dict[str, ServerFiles] = {}  # each server's last report, for the window
        self._seen: dict[str, tuple[Any, ...]] = {}  # each server's fingerprint as last read
        self._still_since: dict[str, float] = {}  # when it last changed (monotonic)
        self._checked: dict[str, tuple[Any, ...]] = {}  # the fingerprint its program last checked
        self._force: set[str] = set()  # check again now (a reload)
        self._dirty: set[str] = set()  # read again now (a game skipped)
        self._skips: dict[str, list[SkipSeen]] = {}
        self._verdicts: dict[str, Verdict] = {}  # each server's program's last word (also kept in records)
        self._wake: asyncio.Event | None = None
        bus.subscribe("server.game_skip", self._on_skip)
        bus.subscribe("log.game_skip", self._on_skip)
        bus.subscribe("rcon.control", self._on_reload)
        bus.subscribe("server.reloaded", self._on_reload)
        bus.subscribe("log.reload", self._on_reload)
        bus.subscribe("server.started", self._on_started)

    # Reading -------------------------------------------------------------------------------------------------------

    def _server(self, name: str) -> ServerConfig | None:
        try:
            return self.settings.server(name)
        except KeyError:
            return None

    def skips(self, name: str) -> list[SkipSeen]:
        if name not in self._skips:
            self._skips[name] = load_skips(self.settings.records, name)
        return self._skips[name]

    def refresh(self, server: ServerConfig) -> ServerFiles:
        """The server's report read again (files only: in a thread) and its findings synced into the catalogue."""
        verdict = self._verdicts.get(server.name) or load_verdict(self.settings.records, server.name)
        report = server_files(server, verdict=verdict, skips=self._current(server.name),
                              version=verdict.version if verdict is not None and verdict.version else None,
                              config_name=self.settings.config_name)
        self.reports[server.name] = report
        self.warnings.sync(server.name, SOURCE, report.findings)
        return report

    def _current(self, name: str) -> list[SkipSeen]:
        """Its skips, the ones that have passed made inactive (filehealth.still_active)."""
        self._skips[name] = current_skips(self.skips(name), self.clock())
        return self._skips[name]

    def program_check(self, server: ServerConfig) -> Verdict:
        """The server's own program on what it plays (seconds: in a thread), kept in records for any window."""
        from ..program.dedicated import DedicatedError, tool_for
        from .gamefiles import server_version

        report = self.reports.get(server.name)
        workshop = report is not None and any(
            r.written.casefold().startswith("workshop:") for p in report.in_use for g in p.games for r in g.refs) or \
            report is not None and report.own is not None and any(
                r.written.casefold().startswith("workshop:") for r in report.own.refs)
        try:
            tool = (self._tool_for or tool_for)(self.settings, server)
        except DedicatedError as error:  # said loudly elsewhere (the dashboard, `start.cmd check`): no warning here
            log.debug("[%s] its program can't check what it plays: %s", server.name, error)
            verdict = Verdict(asked=False, when=self.clock())
        else:
            verdict = plays_verdict(tool, server, run_check=not workshop)
            try:
                verdict.version = server_version(self.settings, server) or ""
            except Exception:  # noqa: BLE001 - the labels are only judged with a version: without, not judged
                verdict.version = ""
        self._verdicts[server.name] = verdict
        keep_verdict(self.settings.records, server.name, verdict)
        what = "refused: " + "; ".join(p.text for p in verdict.refusals[:3]) if verdict.refusals else \
            "not checked: " + verdict.unasked if not verdict.asked else "takes what it plays"
        log.info("[%s] its own program checked what it plays: %s", server.name, one_line(what)[:300])
        return verdict

    # Running -------------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Every server read as Huragok starts, then whenever its files change; its program's check after each change
        (once still) and after a reload, one server at a time."""
        self._wake = asyncio.Event()
        checker = asyncio.ensure_future(self._check_loop())
        try:
            while True:
                for server in list(self.settings.servers):
                    await self._look(server)
                for name in [n for n in self.reports if self._server(n) is None]:
                    self.reports.pop(name, None)
                    self._seen.pop(name, None)
                    self._checked.pop(name, None)
                await wait_until(self._wake, POLL_SECONDS)
                self._wake.clear()
        finally:
            checker.cancel()
            await asyncio.gather(checker, return_exceptions=True)

    async def _look(self, server: ServerConfig) -> None:
        try:
            now = await asyncio.to_thread(fingerprint, server, self.settings.config_name)
        except OSError as error:
            log.debug("[%s] its files couldn't be looked at: %s", server.name, error)
            return
        if now != self._seen.get(server.name):
            self._seen[server.name] = now
            self._still_since[server.name] = time.monotonic()
        elif server.name not in self._dirty:
            return
        self._dirty.discard(server.name)
        await asyncio.to_thread(self.refresh, server)

    async def _check_loop(self) -> None:
        while True:
            name = self._next_check()
            if name is None:
                await asyncio.sleep(1.0)
                continue
            server = self._server(name)
            if server is None:
                self._force.discard(name)
                continue
            self._force.discard(name)
            seen = self._seen.get(name)
            await asyncio.to_thread(self.program_check, server)
            self._checked[name] = seen if seen is not None else ()
            await asyncio.to_thread(self.refresh, server)

    def _next_check(self) -> str | None:
        """A server whose program should check what it plays now: one asked for (a reload), or one whose files
        changed since its last check and have been still for SETTLE_SECONDS."""
        for name in list(self._force):
            if name in self._seen:
                return name
        now = time.monotonic()
        for name, seen in self._seen.items():
            if self._checked.get(name) != seen and now - self._still_since.get(name, now) >= SETTLE_SECONDS:
                return name
        return None

    def _again(self, name: str) -> None:
        self._dirty.add(name)
        if self._wake is not None:
            self._wake.set()

    # What the servers say ------------------------------------------------------------------------------------------

    def _on_skip(self, event: Event) -> None:
        """A game the server couldn't play (B13): kept per game, said as a "seen" warning."""
        name = event.server or ""
        what = str(event.get("what") or "")
        game = str(event.get("game") or "").strip()
        if not name or not what or not game:
            return
        now = self.clock()
        skips = self.skips(name)
        if what == "skip_ended":
            self._skips[name] = [SkipSeen(s.what, s.game, s.at, s.reason, s.times, s.skip, False)
                                 if s.active and s.game.casefold() == game.casefold() else s for s in skips]
            log.info("[%s] %s is played again: its skip for failed loads ran out", name, game)
        else:
            if any(s.what == what and s.game.casefold() == game.casefold() and now - s.at < SAME_LINE_SECONDS
                   for s in skips):
                return  # the same line from its console and its log
            times = event.get("times")
            skip = SkipSeen(what, game, now, str(event.get("reason") or ""), times if isinstance(times, int) else None,
                            str(event.get("skip") or ""), True)
            if what in ("skipped", "unplayable"):  # a retry or failed load of this game is over: it's skipped now
                skips = [SkipSeen(s.what, s.game, s.at, s.reason, s.times, s.skip, False)
                         if s.active and s.what in ("retry", "load_failed") and s.game.casefold() == game.casefold()
                         else s for s in skips]
            self._skips[name] = [*skips, skip]
        self._skips[name] = keep_skips(self.settings.records, name, self._skips[name], now)
        self._again(name)
        if self._wake is None:  # not running (a test, a second window): read now
            server = self._server(name)
            if server is not None:
                self.refresh(server)

    def _on_started(self, event: Event) -> None:
        """A server started again: the program's skips start afresh (it keeps them in memory)."""
        name = event.server or ""
        skips = self.skips(name)
        if any(s.active for s in skips):
            self._skips[name] = keep_skips(self.settings.records, name,
                                           [SkipSeen(s.what, s.game, s.at, s.reason, s.times, s.skip, False)
                                            for s in skips], self.clock())
            self._again(name)

    def _on_reload(self, event: Event) -> None:
        """The server re-read its files: its program checks what it plays again; a refused reload is a warning until
        one goes through."""
        name = event.server or ""
        if not name:
            return
        if event.name == "rcon.control":
            if str(event.get("action") or "") not in ("reload", "reload_applied"):
                return
            if event.get("ok", True) is False:
                server = self._server(name)
                self.warnings.set(name, "plays-reload-refused", source=RELOAD,
                                  about=server.config_path if server is not None else None,
                                  why=one_line(str(event.get("text") or "no details")))
            else:
                self.warnings.clear(name, "plays-reload-refused")
        elif event.name == "server.reloaded":
            self.warnings.clear(name, "plays-reload-refused")
        self._force.add(name)
        self._again(name)


__all__ = ["PlaysWatch", "POLL_SECONDS", "SETTLE_SECONDS", "fingerprint"]
