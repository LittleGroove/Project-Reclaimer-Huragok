"""The server program's releases (Project Reclaimer itself), as Huragok follows them from 0.7.0 (hands off the servers:
plan-0.7.0 section 4.3). Each server installs its own releases, its `auto_update` (its dedicated.toml) the one switch;
Huragok never downloads a program or puts one in place. What it does:

- **Looks** at GitHub every [server_updates] check_minutes (15) and says when a release is out.
- **Restarts the servers that update themselves after their game** once it sees a release they're behind (answer 24),
  or once one of them updated itself: each installs the release as it starts (S2: back in about 13 s) instead of having
  its game cut at its own hourly look (S2d). Players are told once during the game; nobody on, it's at once and nothing
  is said. `follow` is the switch (off: Huragok never restarts a server for a release, which the Reclaimer update tests
  use to see what a server does alone). A server Huragok didn't start is restarted only through its own console's
  `restart` (answer 30); on Docker the image is the update, so there it only warns (answer 36).
- **Warns players** the moment a server says `[update] downloading Project Reclaimer <version>`: its own updater stops
  the game a few seconds later (answer 23), and without a word players would think it crashed.
- **Moves a server off its old program file** after a self-update: the program's supervisor keeps running from the
  locked project-reclaimer.exe.old (S2), so once nobody is on the server is restarted, and that file deleted while it's
  stopped. A server that never empties for it is warned about.
- **Warns** (core/serverwarnings.py, templates/warnings.toml): a server pinned to its version by its own file (said
  once, calmly), one that should have updated and didn't (70 minutes after a release), the busy one above, and on
  Docker a new image.

Which release a program file is, and GitHub's latest, are program/releases.py's."""

from __future__ import annotations

import asyncio
import itertools
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.background import background
from ..core.events import Event
from ..core.jobs import StoppedJob
from ..core.serverwarnings import RuleError, Warnings, load_rules
from ..core.text import wrap
from ..core.web import UpdateError, parse_version
from ..lore import NOTICES, callout
from ..program import releases
from ..rcon import RconError

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings
    from ..core.events import EventBus
    from ..servers.fleet import Lifecycle
    from ..rcon import RconClient
    from ..servers.state import ServerState

log = logging.getLogger(__name__)


OVERDUE = 70 * 60.0  # the program looks at its start and hourly; then a few minutes to download and restart


REVIEW_SECONDS = 60.0  # how often the servers' releases are looked at again (no network)


OLD_FILE_WARN = 3600.0  # still running from the old program file this long after a self-update: warned

COMING_UP = {"launch", "preparing", "booting", "restarting"}  # no restart can be planned yet: it's waited out
OLD_FILE_UP_SECONDS = 5.0  # how often a server coming up is looked at again before its old-file restart is planned
OLD_FILE_UP_WAITS = 120  # ten minutes of it (a start is allowed five: [restarts] ready_seconds)


OLD_JOB = "program-old"


REASON = "Project Reclaimer {version} is out; it updates as it starts"  # a release restart's reason, players read it


SOURCE = "program"  # the warnings this sets, for Warnings.sync


RESTARTS_ON = ("process", "command")  # backends a release restart works on: docker's image is its update, attach can't


GAME_ON = {"playing", "round_over", "between_rounds"}  # the console's phases while a game is being played


SAID_LATELY = 120.0  # what a server was doing as it said it was downloading counts this long for its update line


# What the program says about a setting it doesn't know, as it reads dedicated.toml.
UNKNOWN_SETTING = re.compile(r"unknown (?P<what>field|variant) `(?P<name>[^`]+)`")


class ProgramUpdateService:
    """Looks for new server releases, says so, has the self-updating servers pick them up after their game, and warns
    about the ones that don't."""

    def __init__(self, settings: Settings, bus: EventBus, lifecycle: Lifecycle, states: dict[str, ServerState],
                 clients: dict[str, RconClient], warnings: Warnings | None = None) -> None:
        self.settings = settings
        self.config = settings.server_updates
        self.bus = bus
        self.lifecycle = lifecycle
        self.states = states
        self.clients = clients
        self.warnings = warnings if warnings is not None else Warnings(_rules(), None, bus)
        self.pending: releases.ProgramRelease | None = None  # the newest release, while a server is behind it
        self.newest: releases.ProgramRelease | None = None  # the newest release seen, whatever the servers run
        self.first_seen: dict[str, float] = {}  # version -> when Huragok first saw it
        self.announced: str | None = None
        self.versions: dict[str, str] = {}  # each server's release when its console last signed in (identify())
        self.said: dict[str, str] = {}  # the version each server's console gave then (0.10.1 says 0.10.0)
        self.put_in_place: dict[str, str] = {}  # server -> the release its own updater said it put in place
        self._told: dict[str, tuple[str, str]] = {}  # server -> (said, release) logged once
        self.since: dict[str, float] = {}  # each server's start as seen from here: its own updater looks then
        # The same, and the look that first saw each release, in the order they came: whether a server started after
        # the look is a matter of order, which times can't tell on Windows, whose clock moves in ~16 ms steps (CI)
        self._order = itertools.count()
        self.since_order: dict[str, int] = {}
        self.seen_order: dict[str, int] = {}
        self.updater: dict[str, str] = {}  # what each server's console said about its own updater (on / off)
        self.failed: dict[str, str] = {}  # the reason each server's own update last failed
        self.updated_itself: set[str] = set()  # "Update installed" seen; its `dedicated` runs from the old file
        self.file_versions: dict[str, str] = {}  # a pinned server not signed in: its program file's release
        self.restarting: dict[str, str] = {}  # server -> the release it restarts for after its game (follow)
        self.old_file: dict[str, tuple[str, float]] = {}  # server -> (release, since when) still on the old file
        self.looked_for_old: set[str] = set()  # servers whose folder was looked at for an old program file, once each
        self.games: dict[str, float] = {}  # server -> when the game it plays began (as seen here)
        self.downloading: dict[str, tuple[float, float | None]] = {}  # server -> (when it said so, its game's start)
        self.last_error = ""  # why the last look at GitHub gave nothing to go by ("" when it did)
        self.newer: bool | None = None  # the last look: newer than a running server's version or a pinned one's file
        self._unreadable: set[tuple[str, str]] = set()  # (server, version) that isn't a version number: said once
        self._said_cant: set[tuple[str, str]] = set()  # (server, release) a restart couldn't be planned for: said once
        self.diagnosing: set[str] = set()  # servers whose failed start is being looked into
        self.mistakes: set[tuple[str, str]] = set()  # (server, setting) even the newest program doesn't know
        bus.subscribe("rcon.connected", self._on_connected)
        bus.subscribe("rcon.phase", self._on_phase)
        # Huragok starting the program, not 0.10's "[server] "X" on port …" line, which comes at every game's start
        # (the beta.2 tests): counted from that, a running server always "started after" a release (beta.3 tests)
        bus.subscribe("server.starting", self._on_started)
        bus.subscribe("server.updating", self._on_updating)
        bus.subscribe("server.self_update", self._on_self_update)
        bus.subscribe("server.update_failed", self._on_update_failed)
        bus.subscribe("server.update_downloading", self._on_downloading)
        bus.subscribe("server.start_failed", self._on_start_failed)

    # Which release each server runs ---------------------------------------------------------------------------

    def self_updating(self) -> set[str]:
        return releases.self_updating(self.settings)

    def version_of(self, name: str) -> str | None:
        """The release it runs: what its console said as Huragok signed in, or its program file's release when
        that is a release which calls itself that (identify())."""
        if name in self.versions:
            return self.versions[name]
        client = self.clients.get(name)
        said = str(client.version or "") if client is not None else ""
        return self.identify(name, said) if said else None

    def identify(self, name: str, said: str) -> str:
        """The release a server runs whose console said `said` (Project Reclaimer 0.10.1's console says 0.10.0):
        first what the running server writes in its status.json (`health.version`, 0.10.1 on: its release), while
        that file is fresh; else its program file's checksum (releases.running_release); else what it said."""
        server = self._server(name)
        state = self.states.get(name)
        pid = state.pid if state is not None and isinstance(getattr(state, "pid", None), int) else None
        reported = status_release(server.status_path, pid) if server is not None else None
        if reported is not None:
            version, how = reported, "its status.json says so"
        else:
            path = releases.program_of(self.settings, server) if server is not None else None
            version, how = releases.running_release(self.settings, path, said), "its program file is that release's " \
                                                                                  "(by its checksum)"
        if version != said and self._told.get(name) != (said, version):
            self._told[name] = (said, version)
            log.info("[%s] Project Reclaimer %s: %s, though its console gives its version as %s", name, version, how,
                     said)
        return version

    def _learn_what_files_say(self) -> None:
        """A thread's work: each signed-in server's program file that Huragok knows the release of, asked once what
        it says it is (releases.learn_says), so identify() can tell whether the server runs it."""
        for name in list(self.said):
            server = self._server(name)
            path = releases.program_of(self.settings, server) if server is not None else None
            if path is not None:
                releases.learn_says(self.settings, path)

    def _identify_again(self) -> None:
        """After Huragok learns a release's checksum: each running server's release, from what it said as it signed
        in. One that turns out to be on the release it was to restart for is done (no restart for it)."""
        for name, said in list(self.said.items()):
            version = self.identify(name, said)
            if version != self.versions.get(name):
                self.versions[name] = version
                self._caught_up(name)

    def _read_files(self) -> None:
        """A thread's work: each pinned server (auto_update off) that hasn't signed in, its program file's release (by
        its checksum when Huragok knows the file, else its own --version, asked once per file)."""
        for server in list(self.settings.servers):
            name = server.name
            path = releases.program_of(self.settings, server) if not server.auto_update else None
            version = releases.file_version(self.settings, path) if path is not None and not self.version_of(name) \
                else None
            if releases.readable(version):
                self.file_versions[name] = str(version)
            else:
                self.file_versions.pop(name, None)

    def known_version(self, name: str) -> str | None:
        """What it runs, or (a pinned server not signed in) its program file's release."""
        return self.version_of(name) or self.file_versions.get(name)

    def behind(self, name: str, version: str | None = None) -> bool:
        """Its running version is older than `version` (the newest release seen); False while it isn't known."""
        version = version or (self.newest.version if self.newest else None)
        return _older(self.version_of(name), version)

    def _server(self, name: str) -> ServerConfig | None:
        return next((s for s in self.settings.servers if s.name == name), None)

    # What the servers say -------------------------------------------------------------------------------------

    def _on_started(self, event: Event) -> None:
        if event.server:
            self.since[event.server] = time.time()
            self.since_order[event.server] = next(self._order)
            self.updater.pop(event.server, None)  # what it says about its own updater at this start

    def _on_phase(self, event: Event) -> None:
        """When the game it plays began, for "stopped its game N minutes in" (rounds don't start it again)."""
        name, phase = event.server or "", str(event.get("phase") or "")
        if phase in GAME_ON:
            self.games.setdefault(name, event.time)
        else:
            self.games.pop(name, None)

    def _on_self_update(self, event: Event) -> None:
        """At its start the server says whether its own updater is on."""
        name, text = event.server or "", str(event.get("text") or "")
        # 0.9: "Automatic updates enabled: …"; 0.10: "[update] automatic updates: checking now and hourly; …"
        if text.lower().startswith(("automatic updates enabled", "automatic updates: checking")):
            self.updater[name] = "on"
            self.failed.pop(name, None)
            return
        self.updater[name] = "off"
        self.failed[name] = text.rstrip(".")
        log.warning("[%s] auto_update is on, but the server says: %s. Its new releases have to be put in its folder by "
                    "hand", name, text.rstrip("."))

    def _on_downloading(self, event: Event) -> None:
        """'[update] downloading Project Reclaimer 0.10.8': its own updater stops the game a few seconds later, at once
        and mid-game (S2d: 8 s, the console still up). Players on are told now, so they don't think it crashed
        (answer 23). Huragok restarts nothing for it."""
        name, version = event.server or "", str(event.get("version") or "?").lstrip("v")
        state = self.states.get(name)
        on = len(state.players) if state is not None else 0
        self.downloading[name] = (time.time(), self.games.get(name))
        log.info("[%s] downloading Project Reclaimer %s itself (auto_update): it restarts on it in a few seconds%s",
                 name, version, f"; telling the {on} player(s) on" if on else "")
        if on:
            text = callout("self update", f"This server is updating to Project Reclaimer {version} and restarts in a "
                                          "few seconds. Update your game to rejoin.")
            background(self._say(name, text), f"[{name}] telling its players about its update", log)

    async def _say(self, name: str, text: str) -> None:
        client = self.clients.get(name)
        if client is None or getattr(client, "no_console", False) or not getattr(client, "connected", False):
            log.info("[%s] couldn't tell its players (its console isn't connected): %s", name, text)
            return
        for line in wrap(text) or [text]:
            try:
                await client.command("say", line)
            except RconError as error:
                log.warning("[%s] couldn't tell its players (%s): %s", name, error, text)
                return

    def _on_update_failed(self, event: Event) -> None:
        """'Automatic update failed; retrying in an hour: …': said, and warned about (overdue at once)."""
        name, reason = event.server or "", str(event.get("reason") or "no reason given")
        self.failed[name] = reason
        log.warning("[%s] its own update failed: %s", name, reason)
        if name in self.self_updating():
            background(self._after_failure(), f"[{name}] its failed update", log)

    async def _after_failure(self) -> None:
        if self.newest is None:
            await self.check()  # it may know of a release Huragok hasn't seen yet (check reviews)
        else:
            await self.review()

    def _on_updating(self, event: Event) -> None:
        """'Update installed; restarting all servers.' (0.9), '[update] Project Reclaimer 0.10.1 is in place: stopping
        every server to start it' (0.10): the server's own auto_update is at work, and doesn't wait for a game to end
        (S2d)."""
        name = event.server or ""
        if name:
            self.updated_itself.add(name)
            self.failed.pop(name, None)
            if event.get("version"):  # 0.10 names the release: its program file is that release's from now on
                self.put_in_place[name] = str(event.get("version")).lstrip("v")
        log.warning("[%s] %s Project Reclaimer installed a new release by itself (auto_update): %s", name,
                    NOTICES["dust"], self._what_it_cut(name))

    def _what_it_cut(self, name: str) -> str:
        """The game its own update stopped, if one was on: as it was when it said it was downloading (its console may
        be gone by now), else as it is."""
        said, game = self.downloading.pop(name, (None, None))
        if said is None or time.time() - said > SAID_LATELY:
            game = self.games.get(name)
        self.games.pop(name, None)
        if game is None:
            return "it is back in a few seconds"
        return (f"it stopped its game {_minutes_in(time.time() - game)} and is back in a few seconds (players must "
                "update their game to rejoin)")

    def _on_connected(self, event: Event) -> None:
        name, said = event.server or "", str(event.get("version") or "")
        if not said:
            return
        self.since.setdefault(name, time.time())
        self.since_order.setdefault(name, next(self._order))
        installed = self.put_in_place.pop(name, None)
        server = self._server(name)
        if installed and server is not None and releases.readable(installed):
            path = releases.program_of(self.settings, server)  # what its own updater put in place, and now runs
            if path is not None and _same_line(said, installed):
                releases.remember_file(self.settings, path, installed, says=said)
        version = self.identify(name, said)
        self.said[name] = said
        before, self.versions[name] = self.versions.get(name), version
        self.file_versions.pop(name, None)
        self._caught_up(name)
        updated_itself = name in self.updated_itself  # "Update installed" came first; at its start, the old
        if updated_itself:                            # version may never have signed in
            self.updated_itself.discard(name)
            self.looked_for_old.add(name)
            self._restart_off_the_old_file(name, version)
        elif name not in self.looked_for_old and self.holds_processes() and _in_a_loop():
            self.looked_for_old.add(name)
            background(self._still_on_the_old_file(name, version), f"[{name}] looking for its old program file", log)
        try:
            newer = before is not None and parse_version(version) > parse_version(before)
        except UpdateError:
            newer = False
        if not newer and not (updated_itself and before is None):
            return
        log.warning("[%s] now on Project Reclaimer %s (%s)", name, version,
                    f"was {before}" if before else "it updated itself as it started")
        self.bus.emit("server.program_updated", name, version=version, before=before)
        if self.config.follow:
            background(self.follow(name, version), f"[{name}] the others following its update", log)

    def _caught_up(self, name: str) -> None:
        """It's on the release it was to restart for (it updated itself first): that restart is called off."""
        version = self.restarting.get(name)
        if version is None or self.behind(name, version):
            return
        self.restarting.pop(name, None)
        plan = self.lifecycle.planned.get(name)
        if plan is not None and plan.reason == REASON.format(version=version):
            self.lifecycle.cancel(name)  # it updated itself before its game ended: no restart needed now
            log.info("[%s] on Project Reclaimer %s already: its restart for it is called off", name, version)

    # The restart onto the new program file ------------------------------------------------------------------

    def holds_processes(self) -> bool:
        """Huragok runs the program's own supervisor itself only with the process backend."""
        return self.settings.backend == "process"

    def _restart_off_the_old_file(self, name: str, version: str) -> None:
        """After a self-update the `dedicated` Huragok holds still runs from project-reclaimer.exe.old, which the
        next update may not be able to replace. Once nobody is on, restart it on the new file and remove the old."""
        if not self.holds_processes():
            return
        server = self._server(name)
        if server is None:
            return

        async def tidy() -> str:
            self.old_file.pop(name, None)
            self.warnings.clear(name, "release-old-file")
            removed = await asyncio.to_thread(releases.remove_old_files, server.folder)
            return f"removed {', '.join(p.name for p in removed)} (the program from before its update)" if removed \
                else ""

        self.lifecycle.add_job(name, StoppedJob(OLD_JOB, "removing the program file from before its update", tidy))
        self.old_file[name] = (version, time.time())
        if name in self.lifecycle.planned:
            return  # the restart already planned runs the job too
        background(self._plan_off_the_old_file(name), f"[{name}] restarting on its new program file", log)

    async def _still_on_the_old_file(self, name: str, version: str) -> None:
        """Once per server, as this Huragok first hears from it: a project-reclaimer.exe.old in its folder is removed,
        and one that can't be is held by the program's own supervisor, still running from it. Updated itself before
        this Huragok started (or before an update of Huragok), it was never restarted onto the new file: the live
        servers on 0.10.10, adopted by 0.7.0-beta.2 and beta.3, ran on 0.10.9's supervisor for days."""
        server = self._server(name)
        if server is None:
            return
        old = await asyncio.to_thread(lambda: sorted(server.folder.glob("*.exe.old")))
        if not old:
            return
        await asyncio.to_thread(releases.remove_old_files, server.folder)
        held = [path for path in old if path.exists()]
        if not held:
            log.info("[%s] removed %s (the program from before an update)", name, ", ".join(p.name for p in old))
            return
        log.info("[%s] its `dedicated` still runs from %s (the program from before an update): it restarts onto the "
                 "new file once nobody is on", name, held[0].name)
        self._restart_off_the_old_file(name, version)

    async def _plan_off_the_old_file(self, name: str) -> None:
        # Restarted for a release, a server updates itself as it starts, so Huragok sees its new version while it's
        # still coming up, when no restart can be planned: wait until it's up. It used to give up, leaving the
        # program's own supervisor on project-reclaimer.exe.old (the live servers on 0.10.10, 24 Nov 2026)
        for _ in range(OLD_FILE_UP_WAITS):
            if self.lifecycle.phase(name) not in COMING_UP:
                break
            await asyncio.sleep(OLD_FILE_UP_SECONDS)
        if not await self._plan(name, "when_empty", "its own update left `dedicated` running from the old program "
                                                    "file; restarting on the new one"):
            self.old_file.pop(name, None)  # not one Huragok can restart: no warning that it isn't

    async def _plan(self, name: str, when: str, reason: str) -> bool:
        try:
            await self.lifecycle.restart(name, when=when, reason=reason, by=self.settings.name)
            return True
        except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError: at its next start, then
            log.info("[%s] couldn't plan a restart (%s); it happens at its next start", name, error)
            return False

    # Restarts for a release (answer 24) ----------------------------------------------------------------------

    def restarts_for_releases(self) -> bool:
        """`follow` on, and a backend whose restart puts a release in (docker's image is its update: answer 36)."""
        return bool(self.config.follow) and self.settings.backend in RESTARTS_ON

    async def follow(self, updated: str, version: str) -> list[str]:
        """One server updated itself: the others with auto_update on still on an older version restart after their
        game, so they install it as they start instead of within the hour."""
        restarted = await self._restart_behind(version, skip={updated})
        if restarted:
            log.info("Following %s onto Project Reclaimer %s: %s", updated, version, _after_their_game(restarted))
        return restarted

    async def _restart_behind(self, version: str, *, skip: set[str] | None = None) -> list[str]:
        """Each running server with auto_update on that's behind `version`, restarted after its game (at once when
        nobody is on), once per release. Not one that started after Huragok saw the release: it looked as it started,
        and if it's still behind, its own update didn't happen (warned)."""
        if not self.restarts_for_releases():
            return []
        seen = self.seen_order.get(version)
        restarted = []
        for name in sorted(self.self_updating() - (skip or set())):
            if self.restarting.get(name) == version or not self.lifecycle.is_up(name) or not self.behind(name, version):
                continue
            # It looked itself as it started if its own updater said so then (or said it can't: a restart won't help).
            # One started with auto_update off says nothing, and turned on since never looked (the beta.3 tests, 26
            # November 2026: never restarted for it)
            if seen is not None and self.since_order.get(name, -1) > seen and name in self.updater:
                continue
            if await self._restart_for(name, version):
                restarted.append(name)
        return restarted

    async def _restart_for(self, name: str, version: str) -> bool:
        self.restarting[name] = version  # first: two looks at once plan it once
        plan = self.lifecycle.planned.get(name)
        if plan is not None and plan.when == "after_game":
            log.info("[%s] the restart planned after its game (%s) installs Project Reclaimer %s as it starts too",
                     name, plan.reason or "no reason given", version)
            return True
        tell = callout("release out", f"Project Reclaimer {version} is out. This server restarts for it after this "
                                      "game, so the update lands sooner. Update your game to rejoin.")
        try:
            await self.lifecycle.restart(name, when="after_game", reason=REASON.format(version=version),
                                         by=self.settings.name, tell=tell, console=True)
        except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError
            self.restarting.pop(name, None)
            if (name, version) not in self._said_cant:
                self._said_cant.add((name, version))
                log.info("[%s] couldn't plan a restart onto Project Reclaimer %s: %s", name, version, error)
            return False
        return True

    # Warnings -----------------------------------------------------------------------------------------------

    def overdue(self, name: str, now: float | None = None) -> bool:
        """A self-updating server still behind the newest release after it has had its look (at its start, then
        hourly) and time to install it, or at once when it said its own update failed."""
        if self.newest is None or not self.behind(name):
            return False
        if name in self.failed:
            return True
        now = time.time() if now is None else now
        return now >= max(self._out(), self.since.get(name, now)) + OVERDUE

    def _out(self) -> float:
        """When the newest release came out: GitHub's word, else when Huragok first saw it."""
        assert self.newest is not None
        return self.newest.published or self.first_seen.get(self.newest.version, time.time())

    def found(self, name: str, now: float | None = None) -> list[tuple[str, str, Path | str | None, dict[str, Any]]]:
        """The server's release warnings now, as Warnings.sync takes them."""
        now = time.time() if now is None else now
        server, newest, found = self._server(name), self.newest, []
        if server is None:
            return []
        mine = self.known_version(name)
        if newest is not None and _older(mine, newest.version):
            if self.settings.backend == "docker":
                found.append(("release-image", "", None, {"newest": newest.version, "version": mine}))
            elif not server.auto_update:
                found.append(("release-pinned", "", server.config_path, {"version": mine, "newest": newest.version}))
            elif self.lifecycle.is_up(name) and self.overdue(name, now):
                why = f" (it said: {self.failed[name]})" if name in self.failed else ""
                found.append(("release-overdue", "", None, {
                    "version": mine, "newest": newest.version, "out": _clock(self._out()), "why": why,
                    "slug": server.slug, "page": newest.page}))
        old = self.old_file.get(name)
        if old is not None and now - old[1] >= OLD_FILE_WARN and self.lifecycle.is_up(name):
            found.append(("release-old-file", "", None, {"version": old[0], "since": _clock(old[1]),
                                                         "old": "project-reclaimer.exe.old"}))
        return found

    async def review(self, *, now: float | None = None) -> list[str]:
        """Every server's release warnings brought up to date, and (follow) the self-updating ones behind the newest
        release restarted after their game. Returns the servers restarted."""
        for server in list(self.settings.servers):
            self.warnings.sync(server.name, SOURCE, self.found(server.name, now))
        if self.newest is None:
            return []
        restarted = await self._restart_behind(self.newest.version)
        if restarted:
            log.info("Project Reclaimer %s is out: %s to install it as %s", self.newest.version,
                     _after_their_game(restarted), "it starts" if len(restarted) == 1 else "they start")
        return restarted

    async def _review_loop(self) -> None:
        while True:
            await asyncio.sleep(REVIEW_SECONDS)
            try:
                await self.review()
            except Exception:  # noqa: BLE001 - the look must go on
                log.exception("Looking at the servers' releases went wrong")

    # A program older than its settings -----------------------------------------------------------

    def _on_start_failed(self, event: Event) -> None:
        """It stopped at a setting in its dedicated.toml it doesn't know: maybe a program older than the file."""
        name = event.server or ""
        text = "\n".join(str(line) for line in event.get("lines") or [])
        match = UNKNOWN_SETTING.search(text) if self.settings.config_name in text else None
        server = self._server(name)
        if match is None or server is None or name in self.diagnosing:
            return
        setting = match.group("name")
        if (name, setting) in self.mistakes:
            return
        self.diagnosing.add(name)
        background(self._diagnose(server, setting, match.group("what")), f"[{name}] looking into its start", log)

    async def _diagnose(self, server: ServerConfig, setting: str, what: str) -> None:
        """Said, never mended: its own auto_update can't run before it has read its settings (0.9.15 stops at 0.9.20's
        `auto_reload`), so the newest program goes in by hand, or that line comes out."""
        name, file = server.name, self.settings.config_name
        unknown = f"`{setting}`" if what == "field" else f"the value `{setting}`"
        try:
            program = releases.program_of(self.settings, server)
            if self.newest is None:
                await self.check()  # first: it learns the newest release's checksum, which may be this file's
            mine = await asyncio.to_thread(releases.file_version, self.settings, program) if program else None
            release = self.newest
            if release is None:
                log.warning("[%s] it won't start: its program doesn't know %s in its %s, and the newest release "
                            "isn't known (GitHub couldn't be asked) to tell whether the program is too old", name,
                            unknown, file)
                return
            if mine is not None and not releases.readable(mine):
                log.warning("[%s] it won't start: its program doesn't know %s in its %s, and answers --version with "
                            "'%s', which isn't a version Huragok can compare with the newest (%s): put the newest "
                            "program in its folder if it's older, or fix that line", name, unknown, file, mine,
                            release.version)
                return
            older = mine is None or parse_version(mine) < parse_version(release.version)
            if not older:
                self.mistakes.add((name, setting))
                newest = "the newest" if parse_version(str(mine)) == parse_version(release.version) else \
                    f"newer than the newest release ({release.version})"
                log.error("[%s] it won't start: its program is Project Reclaimer %s, %s, and doesn't know %s "
                          "in its %s: that line is a mistake. Fix or remove it, then start it again", name, mine,
                          newest, unknown, file)
                return
            was = f"Project Reclaimer {mine}" if mine else "its program"
            log.warning("[%s] it won't start: %s is older than its %s (it doesn't know %s; the newest is %s), and its "
                        "own auto_update can't run before it reads its settings: put the newest program in its folder "
                        "by hand (%s), or take that line out", name, was, file, unknown, release.version, release.page)
        except (UpdateError, OSError) as error:
            log.error("[%s] its program looks older than its %s, but couldn't be looked into: %s", name, file, error)
        finally:
            self.diagnosing.discard(name)

    # Releases -----------------------------------------------------------------------------------

    def _follow_latest(self, release: releases.ProgramRelease) -> None:
        """The newest release is GitHub's latest, even when that goes back: the restarts planned for a pulled one are
        called off."""
        before, self.newest = self.newest, release
        if before is None or parse_version(release.version) >= parse_version(before.version):
            return
        pulled = before.version
        log.warning("Project Reclaimer %s is no longer GitHub's newest release (%s is, again): it was pulled, so "
                    "no server restarts for it", pulled, release.version)
        for name in [n for n, v in self.restarting.items() if v == pulled]:
            self.restarting.pop(name, None)
            plan = self.lifecycle.planned.get(name)
            if plan is not None and plan.reason == REASON.format(version=pulled):
                self.lifecycle.cancel(name)
        if self.pending is not None and self.pending.version == pulled:
            self.pending = None
        if self.announced == pulled:
            self.announced = None
        self.first_seen.pop(pulled, None)
        self.seen_order.pop(pulled, None)

    def running_versions(self) -> dict[str, str]:
        """Each running server's release (version_of: 0.10.1 is 0.10.1 here, though its console says 0.10.0)."""
        found = {}
        for name, client in self.clients.items():
            if client.version and (version := self.version_of(name)):
                found[name] = version
        return found

    def oldest_running(self) -> str | None:
        versions = []
        for name, version in sorted(self.running_versions().items()):
            if releases.readable(version):
                versions.append(version)
            elif (name, version) not in self._unreadable:  # "unknown", say: left out, said once
                self._unreadable.add((name, version))
                log.info("[%s] its console gives its version as '%s', which isn't a version number; it's left out "
                         "of the comparison with new releases", name, version)
        return min(versions, key=parse_version) if versions else None

    def is_newer(self, release: releases.ProgramRelease) -> bool | None:
        """True/False against the oldest running server; None when no server has said its version."""
        oldest = self.oldest_running()
        if oldest is None:
            return None
        return parse_version(release.version) > parse_version(oldest)

    async def run(self) -> None:
        reviewer = asyncio.ensure_future(self._review_loop())  # the old program file's warning needs no look
        try:
            if not self.config.check:
                log.info("Not looking for server program updates ([server_updates] check = false)")
                await asyncio.Event().wait()
            await asyncio.sleep(20)  # the servers' consoles answer first, so their versions are known
            while True:
                try:
                    await self.check()
                except Exception:  # noqa: BLE001 - one bad look must not end the looking (or the reviews)
                    log.exception("Looking for server program updates went wrong; it looks again in %s",
                                  _minutes(self.config.check_minutes))
                await asyncio.sleep(self.config.check_minutes * 60)
        finally:
            reviewer.cancel()

    async def check(self, *, quiet: bool = True) -> releases.ProgramRelease | None:
        """GitHub's latest release; returned when a server is behind it (or that isn't known yet), None when they're
        all on it or GitHub gave nothing to go by (last_error says why)."""
        try:
            release = await asyncio.to_thread(releases.latest_program_release, self.config.repository)
        except UpdateError as error:
            self.last_error = f"couldn't ask GitHub: {error}"
            log.log(logging.INFO if quiet else logging.WARNING, "Couldn't look for server program updates: %s", error)
            return None
        if release is None:
            self.last_error = f"no release with a server program at {self.config.repository}"
            if not quiet:
                log.info("No server program release found at %s", self.config.repository)
            return None
        if not releases.readable(release.version):
            self.last_error = f"the newest release's tag isn't a version number ({release.version or 'empty'})"
            log.info("The newest server program release has no version number (%s)", release.version or "empty")
            return None
        self.last_error = ""
        if release.exe_sha256 and not releases.is_archive(release.exe_name):  # Linux or macOS: the archive's checksum
            if await asyncio.to_thread(releases.remember_release, self.settings, release.exe_sha256, release.version):
                log.info("Project Reclaimer %s's program file is known by its checksum from now on", release.version)
        await asyncio.to_thread(self._learn_what_files_say)
        self._identify_again()  # before anyone is judged behind: a server may say an older version than it runs
        self._follow_latest(release)
        self.first_seen.setdefault(release.version, time.time())
        self.seen_order.setdefault(release.version, next(self._order))
        await asyncio.to_thread(self._read_files)
        newer = self.is_newer(release)
        known = [self.known_version(s.name) for s in self.settings.servers]
        if not newer and any(_older(v, release.version) for v in known):
            newer = True  # a pinned server that isn't running is behind it, say
        elif newer is None and known and all(releases.readable(v) for v in known):
            newer = False  # every server's release is known, and none is older
        self.newer = newer
        if newer is False:
            self.pending = None
            if not quiet:
                log.info("The servers are on the newest server program (%s)", release.version)
        else:
            self.pending = release
            if not quiet or self.announced != release.version:
                self.announce(release, newer)
                self.announced = release.version
        await self.review()
        return None if newer is False else release

    def announce(self, release: releases.ProgramRelease, newer: bool | None) -> None:
        running = ", ".join(f"{n} {v}" for n, v in sorted(self.running_versions().items())) or "no server running"
        if not newer:
            log.info("Newest server program release: %s (running: %s)", release.version, running)
            return
        themselves = sorted(self.self_updating())
        pinned = sorted(s.name for s in self.settings.servers if s.name not in themselves)
        how = []
        if self.settings.backend == "docker":
            how.append("on Docker a new image is the update")
        elif themselves:
            one = len(themselves) == 1  # "B3 Update install it themselves" (the beta.3 tests)
            how.append(f"{', '.join(themselves)} {'installs it itself' if one else 'install it themselves'} "
                       "(auto_update)" + ((", restarted after its game to do so as it starts" if one else
                                           ", each restarted after its game to do so as it starts")
                                          if self.restarts_for_releases() else " within the hour"))
        if pinned and self.settings.backend != "docker":
            one = len(pinned) == 1
            how.append(f"{', '.join(pinned)} {'stays on its' if one else 'stay on their'} version (auto_update off)")
        log.info("Project Reclaimer %s is out (running: %s)%s", release.version, running,
                 f": {'; '.join(how)}" if how else "")

    # What's shown -----------------------------------------------------------------------------------

    def note(self, name: str) -> str:
        """For its status card: where its program update stands, or ''."""
        server = self._server(name)
        newest = self.newest.version if self.newest else None
        if name in self.restarting and name in self.lifecycle.planned:
            return f"Project Reclaimer {self.restarting[name]} is out: it restarts after its game to install it"
        if name in self.failed and name in self.self_updating():
            return f"its own update failed: {self.failed[name]}"
        if name in self.old_file:
            return "its program still runs from the old file: it restarts onto the new one once nobody is on"
        if server is not None and newest and _older(self.known_version(name), newest):
            if self.settings.backend == "docker":
                return f"Project Reclaimer {newest} is out: a new image is the update"
            if not server.auto_update:
                return f"pinned by its own file (auto_update off): Project Reclaimer {newest} is out"
            due = time.strftime("%H:%M", time.localtime(max(self._out(), self.since.get(name, time.time())) + OVERDUE))
            return f"Project Reclaimer {newest} is out: it should have updated itself by {due}"
        return ""

    def report(self) -> list[str]:
        """U → The server program: each server's version, its auto_update, where its update stands and its warnings
        about releases."""
        lines = []
        for server in self.settings.servers:
            name = server.name
            mine = self.known_version(name) or "not known yet"
            how = "auto_update on" if server.auto_update else "auto_update off (pinned by its own file)"
            warned = [warning for warning in self.warnings.of(name) if warning.rule.area == "updates"]
            note = "" if warned else self.note(name)  # a warning says it better
            lines.append(f"{name}: Project Reclaimer {mine} · {how}" + (f" · {note}" if note else ""))
            lines.extend(f"  {warning.line()}" for warning in warned)
        return lines

    def status(self) -> dict[str, Any]:
        return {"pending": self.pending.version if self.pending else None, "running": self.running_versions(),
                "self_updating": sorted(self.self_updating()),
                "newest": self.newest.version if self.newest else None, "restarting": dict(self.restarting),
                "failed": dict(self.failed), "updater": dict(self.updater)}


def _rules() -> dict[str, Any]:
    try:
        return load_rules()
    except (RuleError, OSError) as error:
        log.error("Huragok's warnings can't be read (%s): it carries on without them", error)
        return {}


def _in_a_loop() -> bool:
    """Whether a background task can be started: the bus hands every event to Huragok's loop, but a caller outside one
    (a test's plain call) would leave the task on no loop, never run."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _older(mine: str | None, version: str | None) -> bool:
    """`mine` is a release older than `version`; False when either isn't a version number."""
    if not mine or not version:
        return False
    try:
        return parse_version(str(mine)) < parse_version(str(version))
    except UpdateError:
        return False


def _minutes(minutes: float) -> str:
    return f"{minutes:g} minute{'s' if minutes != 1 else ''}"


def _minutes_in(seconds: float) -> str:
    minutes = int(max(0.0, seconds) // 60)
    return "less than a minute in" if minutes < 1 else f"{minutes} minute{'s' if minutes != 1 else ''} in"


def _clock(when: float) -> str:
    """A time of day, with its date when it isn't today's."""
    local = time.localtime(when)
    if time.strftime("%Y-%m-%d", local) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", local)
    return time.strftime("%d %b %H:%M", local).lstrip("0")


STATUS_FRESH = 150.0  # seconds: 0.10.9 rewrites status.json about once a minute; older, a stopped process's


def status_release(path: Path, pid: int | None = None) -> str | None:
    """The release a running 0.10.1+ server reports in its status.json (`health.version`: "0.10.1", where its console
    and --version say 0.10.0), when the file was written in the last STATUS_FRESH seconds and, with `pid` (the server
    process the console named as it started), by that process: never the one before its restart. None otherwise, and
    for 0.9 (no health block)."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if pid is not None and data.get("pid") != pid:
        return None
    health, updated = data.get("health"), data.get("updated")
    version = health.get("version") if isinstance(health, dict) else None
    if not isinstance(version, str) or not releases.readable(version):
        return None
    if not isinstance(updated, (int, float)) or isinstance(updated, bool) or abs(time.time() - updated) > STATUS_FRESH:
        return None
    return version


def _after_their_game(names: list[str]) -> str:
    """"Beta restarts after its game", "Beta, Gamma restart after their game" (one read "B2 Update restart after their
    game": the beta.2 tests, 25 November 2026)."""
    return f"{names[0]} restarts after its game" if len(names) == 1 else \
        f"{', '.join(names)} restart after their game"


def _same_line(said: str, release: str) -> bool:
    """A program that says `said` can be release `release`: the same x.y, and not newer (0.10.1 says 0.10.0)."""
    try:
        mine, theirs = parse_version(said), parse_version(release)
    except UpdateError:
        return False
    return mine[:2] == theirs[:2] and mine <= theirs
