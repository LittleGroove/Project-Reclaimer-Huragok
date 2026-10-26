"""The server program's updates, as Huragok runs them: a server with `auto_update` updates itself (the program
looks at its start and hourly) and Huragok steps in when a release doesn't arrive (`follow`, `fallback`); the
others are updated from GitHub after their game, as a job the manager runs while the server is stopped. What a
release is, where the program is and how its files are swapped is program/releases.py's."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.background import background
from ..core.events import Event
from ..core.jobs import StoppedJob
from ..core.web import UpdateError, parse_version
from ..lore import NOTICES
from ..program import releases

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings
    from ..core.events import EventBus
    from ..servers.fleet import Lifecycle
    from ..rcon import RconClient
    from ..servers.state import ServerState

log = logging.getLogger(__name__)


OVERDUE = 70 * 60.0  # the program looks at its start and hourly; then a few minutes to download and restart


REVIEW_SECONDS = 60.0  # how often the self-updating servers are looked at (no network)


DOWNLOADS = "program-download"  # in records/: the checked release Huragok installs from


JOB = "program"


OLD_JOB = "program-old"


FLAG = "program"


OUR_REASON = "updating to Project Reclaimer"


SWAP_RETRY_HOURS = (1.0, 4.0, 12.0)  # after a swap that failed; one more failure and it waits for U


# What the program says about a setting it doesn't know, as it reads dedicated.toml.
UNKNOWN_SETTING = re.compile(r"unknown (?P<what>field|variant) `(?P<name>[^`]+)`")


class ProgramUpdateService:
    """Looks for new server releases, says so, makes sure self-updating servers got them, and installs them when
    asked (or by itself, per [server_updates])."""

    def __init__(self, settings: Settings, bus: EventBus, lifecycle: Lifecycle, states: dict[str, ServerState],
                 clients: dict[str, RconClient]) -> None:
        self.settings = settings
        self.config = settings.server_updates
        self.bus = bus
        self.lifecycle = lifecycle
        self.states = states
        self.clients = clients
        self.pending: releases.ProgramRelease | None = None
        self.newest: releases.ProgramRelease | None = None  # the newest release seen, whatever the servers run
        self.first_seen: dict[str, float] = {}  # version -> when Huragok first saw it
        self.announced: str | None = None
        self.installing = False
        self.last_installed: str | None = None
        self.versions: dict[str, str] = {}  # each server's version when its console last signed in
        self.since: dict[str, float] = {}  # each server's start as seen from here: its own updater looks then
        self.updater: dict[str, str] = {}  # what each server's console said about its own updater (on / off)
        self.failed: dict[str, str] = {}  # the reason each server's own update last failed
        self.updated_itself: set[str] = set()  # "Update installed" seen; its `dedicated` runs from the old file
        self.taken: dict[str, str] = {}  # server -> the version Huragok is installing for it
        self.swapped: dict[str, str] = {}  # server -> the version Huragok put in place, until it's seen running
        self.retry: dict[str, float] = {}  # server -> when a failed download is tried again
        self.swap_failures: dict[str, tuple[str, int]] = {}  # server -> (version, swaps that failed)
        self.last_error = ""  # why the last look at GitHub gave nothing to go by ("" when it did)
        self.newer: bool | None = None  # the last look: newer than a running server's version or a stopped one's file
        self._unreadable: set[tuple[str, str]] = set()  # (server, version) that isn't a version number: said once
        self.downloads: dict[str, Path] = {}  # version -> the checked download
        self._download_lock = asyncio.Lock()
        self._said_cant: set[tuple[str, str]] = set()
        self.diagnosing: set[str] = set()  # servers whose failed start is being looked into
        self.mistakes: set[tuple[str, str]] = set()  # (server, setting) even the newest program doesn't know
        bus.subscribe("rcon.connected", self._on_connected)
        bus.subscribe("server.started", self._on_started)
        bus.subscribe("server.updating", self._on_updating)
        bus.subscribe("server.self_update", self._on_self_update)
        bus.subscribe("server.update_failed", self._on_update_failed)
        bus.subscribe("server.update_downloading", self._on_downloading)
        bus.subscribe("server.start_failed", self._on_start_failed)

    # Servers that update themselves ----------------------------------------------------------

    def self_updating(self) -> set[str]:
        return releases.self_updating(self.settings)

    def version_of(self, name: str) -> str | None:
        client = self.clients.get(name)
        return self.versions.get(name) or (client.version if client is not None else None) or None

    def behind(self, name: str, version: str | None = None) -> bool:
        """Its running version is older than `version` (the newest release seen); False while it isn't known."""
        version = version or (self.newest.version if self.newest else None)
        mine = self.version_of(name)
        if not version or not mine:
            return False
        try:
            return parse_version(mine) < parse_version(version)
        except UpdateError:
            return False

    def _server(self, name: str) -> ServerConfig | None:
        return next((s for s in self.settings.servers if s.name == name), None)

    def _on_started(self, event: Event) -> None:
        if event.server:
            self.since[event.server] = time.time()

    def _on_self_update(self, event: Event) -> None:
        """At its start the server says whether its own updater is on."""
        name, text = event.server or "", str(event.get("text") or "")
        if text.startswith("Automatic updates enabled"):
            self.updater[name] = "on"
            self.failed.pop(name, None)
            return
        self.updater[name] = "off"
        self.failed[name] = text.rstrip(".")
        log.warning("[%s] auto_update is on, but the server says: %s. %s", name, text.rstrip("."),
                    "Huragok installs new releases for it after its game" if self.can_take_over()
                    else "Its new releases have to be installed another way")

    def _on_downloading(self, event: Event) -> None:
        log.info("[%s] downloading Project Reclaimer %s itself (auto_update)", event.server,
                 str(event.get("version") or "?"))

    def _on_update_failed(self, event: Event) -> None:
        """'Automatic update failed; retrying in an hour: …': it didn't work, so Huragok does it after the game."""
        name, reason = event.server or "", str(event.get("reason") or "no reason given")
        self.failed[name] = reason
        log.warning("[%s] its own update failed: %s", name, reason)
        if name in self.self_updating() and self.config.fallback:
            background(self._after_failure(name), f"[{name}] taking over its failed update", log)

    async def _after_failure(self, name: str) -> None:
        if self.newest is None or not self.behind(name):
            await self.check()  # it may know of a release Huragok hasn't seen yet
        self.review(only=name)

    def _on_updating(self, event: Event) -> None:
        """'Update installed; restarting all servers.': the server's own auto_update is at work."""
        if event.server:
            self.updated_itself.add(event.server)
            self.failed.pop(event.server, None)
        log.warning("[%s] %s Project Reclaimer installed a new release by itself (auto_update): its game ended and "
                    "it is back in a few seconds", event.server, NOTICES["dust"])

    def _on_connected(self, event: Event) -> None:
        name, version = event.server or "", str(event.get("version") or "")
        if not version:
            return
        self.since.setdefault(name, time.time())
        before, self.versions[name] = self.versions.get(name), version
        if name in self.taken:
            if not self.behind(name, self.taken[name]):
                self._done(name)
            elif self.swapped.get(name) == self.taken[name]:
                self._swap_missed(name)
        updated_itself = name in self.updated_itself  # "Update installed" came first; at its start, the old
        if updated_itself:                            # version may never have signed in
            self.updated_itself.discard(name)
            self._restart_off_the_old_file(name)
        try:
            newer = before is not None and parse_version(version) > parse_version(before)
        except UpdateError:
            newer = False
        if not newer and not (updated_itself and before is None):
            return
        log.warning("[%s] now on Project Reclaimer %s (%s)", name, version,
                    f"was {before}" if before else "it updated itself as it started")
        self.bus.emit("server.program_updated", name, version=version, before=before)
        if self.settings.server_updates.follow:
            background(self.follow(name, version), f"[{name}] the others following its update", log)

    def _done(self, name: str) -> None:
        """It is on the version Huragok was installing (by Huragok, or by itself after all)."""
        self.taken.pop(name, None)
        self.retry.pop(name, None)
        self.swap_failures.pop(name, None)
        self.swapped.pop(name, None)
        self.failed.pop(name, None)
        self.lifecycle.drop_job(name, JOB)
        self.lifecycle.set_flag(name, FLAG, None)
        plan = self.lifecycle.planned.get(name)
        if plan is not None and plan.reason.startswith(OUR_REASON):
            self.lifecycle.cancel(name)  # it updated itself before its game ended: no restart needed now

    def _swap_missed(self, name: str) -> None:
        """Huragok put the new file in place, yet the server came back on the old version: it runs another file
        (a [process] command, say). Trying again would only restart it again."""
        version = self.swapped.pop(name)
        server = self._server(name)
        where = releases.program_of(self.settings, server) if server else None
        self.retry.pop(name, None)  # taken stays: not tried again this run
        self.lifecycle.set_flag(name, FLAG, f"! still on {self.version_of(name)} after {version} was put in place")
        log.error("[%s] still on Project Reclaimer %s after Huragok put %s in place at %s: it runs another program "
                  "file. Update that one by hand", name, self.version_of(name), version, where or "?")

    def _restart_off_the_old_file(self, name: str) -> None:
        """After a self-update the `dedicated` Huragok holds still runs from project-reclaimer.exe.old, which the
        next update may not be able to replace. Once nobody is on, restart it on the new file and remove the old."""
        if not self.can_take_over():
            return
        server = self._server(name)
        if server is None:
            return

        async def tidy() -> str:
            removed = await asyncio.to_thread(releases.remove_old_files, server.folder)
            return f"removed {', '.join(p.name for p in removed)} (the program from before its update)" if removed \
                else ""

        self.lifecycle.add_job(name, StoppedJob(OLD_JOB, "removing the program file from before its update", tidy))
        if name in self.lifecycle.planned:
            return  # the restart already planned runs the job too
        background(self._plan(name, "when_empty", "its own update left `dedicated` running from the old "
                                                  "program file; restarting on the new one"),
                   f"[{name}] restarting on its new program file", log)

    async def _plan(self, name: str, when: str, reason: str) -> bool:
        try:
            await self.lifecycle.restart(name, when=when, reason=reason, by=self.settings.name)
            return True
        except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError: at its next start, then
            log.info("[%s] couldn't plan a restart (%s); it happens at its next start", name, error)
            return False

    async def follow(self, updated: str, version: str) -> list[str]:
        """Restart the other self-updating servers still on an older version after their game: they look for
        releases as they start, so they update then instead of within the hour."""
        restarted = []
        for name in sorted(self.self_updating() - {updated}):
            mine = self.version_of(name)
            try:
                behind = bool(mine) and parse_version(str(mine)) < parse_version(version)
            except UpdateError:
                behind = False
            if not behind or not self.lifecycle.is_up(name) or name in self.taken:
                continue
            try:
                await self.lifecycle.restart(name, when="after_game", by=self.settings.name,
                                             reason=f"Project Reclaimer {version} is out; it updates as it starts")
                restarted.append(name)
            except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError
                log.info("[%s] couldn't plan a restart onto %s: %s", name, version, error)
        if restarted:
            log.info("Following %s onto Project Reclaimer %s: %s restart after their game", updated, version,
                     ", ".join(restarted))
        return restarted

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
        background(self._diagnose(server, setting, match.group("what")), f"[{name}] looking into its update", log)

    async def _diagnose(self, server: ServerConfig, setting: str, what: str) -> None:
        name, file = server.name, self.settings.config_name
        unknown = f"`{setting}`" if what == "field" else f"the value `{setting}`"
        try:
            program = releases.program_of(self.settings, server)
            mine = await asyncio.to_thread(releases.program_version, self.settings, program) if program else None
            if self.newest is None:
                await self.check()
            release = self.newest
            if release is None:
                log.warning("[%s] it won't start: its program doesn't know %s in its %s, and the newest release "
                            "isn't known (GitHub couldn't be asked) to tell whether the program is too old", name,
                            unknown, file)
                return
            if mine is not None and not releases.readable(mine):
                log.warning("[%s] it won't start: its program doesn't know %s in its %s, and answers --version with "
                            "'%s', which isn't a version Huragok can compare with the newest (%s), so Huragok leaves "
                            "it: put the newest program in its folder if it's older, or fix that line (D, or E for "
                            "the file)", name, unknown, file, mine, release.version)
                return
            older = mine is None or parse_version(mine) < parse_version(release.version)
            if not older:
                self.mistakes.add((name, setting))
                newest = "the newest" if parse_version(str(mine)) == parse_version(release.version) else \
                    f"newer than the newest release ({release.version})"
                log.error("[%s] it won't start: its program is Project Reclaimer %s, %s, and doesn't know %s "
                          "in its %s: that line is a mistake. Fix or remove it (D, or E for the file), then start "
                          "it again", name, mine, newest, unknown, file)
                return
            was = f"Project Reclaimer {mine}" if mine else "its program"
            if not (server.auto_update and self.config.fallback and self.can_take_over()):
                why = ("auto_update is off" if not server.auto_update else "[server_updates] fallback is off"
                       if not self.config.fallback else f"with backend = {self.settings.backend} Huragok can't")
                log.warning("[%s] it won't start: %s is older than its %s (it doesn't know %s; the newest is %s). "
                            "%s, so Huragok leaves it: put the newest program in its folder (U -> The server program "
                            "does it), or switch auto_update on (D)", name, was, file, unknown, release.version, why)
                return
            path = await self._download(release)

            async def install() -> str:
                if self._pulled(release):
                    return f"left as it is: Project Reclaimer {release.version} is no longer the newest release"
                try:
                    said = await asyncio.to_thread(releases.swap_program, self.settings, server, path, release.version)
                except (UpdateError, OSError) as error:
                    log.error("[%s] Project Reclaimer %s didn't install: %s; it tries the old one", name,
                              release.version, error)
                    return ""
                return f"{said} ({was} didn't know {unknown} in its {file})"

            self.lifecycle.add_job(name, StoppedJob(JOB, f"installing Project Reclaimer {release.version}", install))
            log.warning("[%s] it won't start: %s is older than its %s (it doesn't know %s). Huragok puts Project "
                        "Reclaimer %s in place before it tries again", name, was, file, unknown, release.version)
        except (UpdateError, OSError) as error:
            log.error("[%s] its program looks older than its %s, but the newest couldn't be put in place: %s", name,
                      file, error)
        finally:
            self.diagnosing.discard(name)

    # Making sure it worked --------------------------------------------------------------------

    def can_take_over(self) -> bool:
        """Huragok can put a program file in place only for servers it runs as processes."""
        return self.settings.backend == "process"

    def overdue(self, name: str, now: float | None = None) -> bool:
        """A self-updating server still behind the newest release after it has had its look (at its start, then
        hourly) and time to install it, or at once when it said its own update failed."""
        if self.newest is None or not self.behind(name):
            return False
        if name in self.failed:
            return True
        now = time.time() if now is None else now
        out = self.newest.published or self.first_seen.get(self.newest.version, now)
        return now >= max(out, self.since.get(name, now)) + OVERDUE

    def review(self, *, only: str | None = None, now: float | None = None) -> list[str]:
        """Each running self-updating server that didn't get the newest release itself: Huragok installs it after
        its game. Returns the servers it took over."""
        if not self.config.fallback or self.newest is None:
            return []
        release, taken = self.newest, []
        moment = time.time() if now is None else now
        for name in sorted(self.self_updating()):
            if only is not None and name != only:
                continue
            if not self.lifecycle.is_up(name) or not self.overdue(name, now):
                continue  # a stopped server looks for releases itself as it starts
            if self.taken.get(name) == release.version and moment < self.retry.get(name, float("inf")):
                continue
            if not self.can_take_over():
                if (name, release.version) not in self._said_cant:
                    self._said_cant.add((name, release.version))
                    log.warning("[%s] still on Project Reclaimer %s although %s is out: its own update didn't "
                                "happen, and with backend = %s Huragok can't put the program in place",
                                name, self.version_of(name), release.version, self.settings.backend)
                continue
            why = (f"its own update failed ({self.failed[name]})" if name in self.failed
                   else f"still on {self.version_of(name)} over an hour after {release.version} came out")
            self.taken[name] = release.version
            self.retry.pop(name, None)
            background(self.take_over(name, release, why), f"[{name}] installing {release.version}", log)
            taken.append(name)
        return taken

    async def _download(self, release: releases.ProgramRelease) -> Path:
        """The checked release, downloaded once (records/program-download/); older downloads are removed."""
        async with self._download_lock:
            path = self.downloads.get(release.version)
            if path is not None and path.is_file():
                return path
            folder = self.settings.records / DOWNLOADS
            path = await asyncio.to_thread(releases.download_program, release, folder)
            for other in folder.glob("*.exe"):
                if other != path:
                    other.unlink(missing_ok=True)
            self.downloads = {release.version: path}
            return path

    async def take_over(self, name: str, release: releases.ProgramRelease, why: str, *,
                        when: str = "after_game") -> tuple[bool, str]:
        """Install `release` for one server: a job swaps the file while it's stopped, at a restart after its game
        (or now)."""
        server = self._server(name)
        if server is None:
            return False, f"no server called {name}"
        self.taken[name] = release.version
        try:
            path = await self._download(release)
        except (UpdateError, OSError) as error:
            self.retry[name] = time.time() + self.config.check_hours * 3600
            self.lifecycle.set_flag(name, FLAG, f"! Project Reclaimer {release.version}: the download failed")
            log.error("[%s] %s, but Huragok couldn't download Project Reclaimer %s either: %s; it tries again "
                      "within %s", name, why, release.version, error, _hours(self.config.check_hours))
            return False, f"not installed: {error}"

        async def install() -> str:
            if self._pulled(release):
                return f"left as it is: Project Reclaimer {release.version} is no longer the newest release"
            try:
                said = await asyncio.to_thread(releases.swap_program, self.settings, server, path, release.version)
            except (UpdateError, OSError) as error:
                self._swap_failed(name, release.version, error)
                return ""
            self.lifecycle.set_flag(name, FLAG, None)
            self.swapped[name] = release.version
            return said

        self.lifecycle.add_job(name, StoppedJob(JOB, f"installing Project Reclaimer {release.version}", install))
        self.lifecycle.set_flag(name, FLAG, f"Project Reclaimer {release.version}: installs "
                                            + ("after this game" if when == "after_game" else "now"))
        log.warning("[%s] %s: Huragok installs Project Reclaimer %s %s", name, why, release.version,
                    "after its game" if when == "after_game" else "now")
        if not await self._plan(name, when, f"{OUR_REASON} {release.version}"):
            return True, f"{name} installs {release.version} at its next start"
        return True, f"{name} installs {release.version} {'after its game' if when == 'after_game' else 'now'}"

    def _swap_failed(self, name: str, version: str, error: BaseException) -> None:
        """A swap that didn't work: tried again after 1, 4, then 12 hours (each try restarts the server after its
        game), then left until someone presses U."""
        before, count = self.swap_failures.get(name, (version, 0))
        count = count + 1 if before == version else 1
        self.swap_failures[name] = (version, count)
        if count > len(SWAP_RETRY_HOURS):
            self.retry[name] = float("inf")
            self.lifecycle.set_flag(name, FLAG, f"! Project Reclaimer {version} didn't install ({count} tries): U "
                                                "to try again")
            log.error("[%s] Project Reclaimer %s didn't install again: %s. That's %d tries, so Huragok stops trying "
                      "it by itself (each try restarts the server): find what holds its program file (an antivirus "
                      "or Controlled Folder Access, say), then press U -> The server program", name, version, error,
                      count)
            return
        hours = max(self.config.check_hours, SWAP_RETRY_HOURS[count - 1])
        self.retry[name] = time.time() + hours * 3600
        self.lifecycle.set_flag(name, FLAG, f"! Project Reclaimer {version} didn't install: tried again in "
                                            f"{_hours(hours)} (U to try now)")
        log.error("[%s] Project Reclaimer %s didn't install: %s; it starts on the old one, and Huragok tries again "
                  "in %s", name, version, error, _hours(hours))

    def _pulled(self, release: releases.ProgramRelease) -> bool:
        """`release` is newer than GitHub's latest now: it was pulled."""
        if self.newest is None:
            return False
        try:
            return parse_version(release.version) > parse_version(self.newest.version)
        except UpdateError:
            return False

    def _follow_latest(self, release: releases.ProgramRelease) -> None:
        """The newest release is GitHub's latest, even when that goes back: what was planned for a pulled one is
        dropped (its download, take-overs, jobs and restarts)."""
        before, self.newest = self.newest, release
        if before is None or parse_version(release.version) >= parse_version(before.version):
            return
        pulled = before.version
        log.warning("Project Reclaimer %s is no longer GitHub's newest release (%s is, again): it was pulled, so "
                    "Huragok doesn't install it", pulled, release.version)
        for version, path in list(self.downloads.items()):
            if version == pulled:
                path.unlink(missing_ok=True)
                self.downloads.pop(version, None)
        for name in [n for n, v in self.taken.items() if v == pulled]:
            for table in (self.taken, self.retry, self.swapped, self.swap_failures):
                table.pop(name, None)
            self.lifecycle.drop_job(name, JOB)
            self.lifecycle.set_flag(name, FLAG, None)
            plan = self.lifecycle.planned.get(name)
            if plan is not None and str(plan.reason).startswith(f"{OUR_REASON} {pulled}"):
                self.lifecycle.cancel(name)
        if self.pending is not None and self.pending.version == pulled:
            self.pending = None
        if self.announced == pulled:
            self.announced = None
        self.first_seen.pop(pulled, None)

    def copies_behind(self, version: str) -> list[Path]:
        """The program files the servers with auto_update off run whose own `--version` is older than `version`
        (one older than its dedicated.toml can't even start, so it never reports a version over RCON)."""
        runs = {releases.program_of(self.settings, s) for s in self.settings.servers if s.name not in self.self_updating()}
        behind = []
        for path in sorted(p for p in runs if p is not None):
            theirs = releases.program_version(self.settings, path)
            if releases.readable(theirs) and parse_version(str(theirs)) < parse_version(version):
                behind.append(path)
        return behind

    def note(self, name: str) -> str:
        """For its status card: where its program update stands, or ''."""
        if name in self.taken and self.swap_failures.get(name, ("", 0))[1] > len(SWAP_RETRY_HOURS):
            return (f"Project Reclaimer {self.taken[name]} didn't install in {self.swap_failures[name][1]} tries: "
                    "U to try again")
        if name in self.taken:
            return f"Huragok is installing Project Reclaimer {self.taken[name]} for it (its own update didn't arrive)"
        if name in self.failed and name in self.self_updating():
            return f"its own update failed: {self.failed[name]}"
        if name in self.self_updating() and self.newest is not None and self.behind(name):
            now = time.time()
            out = self.newest.published or self.first_seen.get(self.newest.version, now)
            due = time.strftime("%H:%M", time.localtime(max(out, self.since.get(name, now)) + OVERDUE))
            then = "; if not, Huragok installs it after the game" if self.config.fallback and self.can_take_over() \
                else ""
            return f"Project Reclaimer {self.newest.version} is out: it should have updated itself by {due}{then}"
        return ""

    async def _review_loop(self) -> None:
        while True:
            await asyncio.sleep(REVIEW_SECONDS)
            try:
                self.review()
            except Exception:  # noqa: BLE001 - the look must go on
                log.exception("Looking at the self-updating servers went wrong")

    # Releases -----------------------------------------------------------------------------------

    def running_versions(self) -> dict[str, str]:
        return {name: client.version for name, client in self.clients.items() if client.version}

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
        if not self.config.check:
            log.info("Not looking for server program updates ([server_updates] check = false)")
            return
        await asyncio.sleep(20)  # the servers' consoles answer first, so their versions are known
        reviewer = asyncio.ensure_future(self._review_loop())
        try:
            while True:
                try:
                    await self.check()
                except Exception:  # noqa: BLE001 - one bad look must not end the looking (or the reviews)
                    log.exception("Looking for server program updates went wrong; it looks again in %s",
                                  _hours(self.config.check_hours))
                await asyncio.sleep(self.config.check_hours * 3600)
        finally:
            reviewer.cancel()

    async def check(self, *, quiet: bool = True) -> releases.ProgramRelease | None:
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
        self._follow_latest(release)
        self.first_seen.setdefault(release.version, time.time())
        self.review()
        newer = self.is_newer(release)
        if not newer:  # the running servers are on it; a stopped one's program file may not be
            behind = await asyncio.to_thread(self.copies_behind, release.version)
            if behind:
                newer = True
                if not quiet or self.announced != release.version:
                    log.warning("%s %s older than Project Reclaimer %s", ", ".join(str(p) for p in behind),
                                "is" if len(behind) == 1 else "are", release.version)
        self.newer = newer
        if newer is False or (newer is None and self.last_installed and
                              parse_version(release.version) <= parse_version(self.last_installed)):
            self.pending = None
            if not quiet:
                log.info("The servers are on the newest server program (%s)", release.version)
            return None
        self.pending = release
        if not quiet or self.announced != release.version:
            self.announce(release, newer)
            self.announced = release.version
            managed = set(self.states) - self.self_updating()
            if self.config.install in ("after_game", "now") and newer and managed:
                background(self.install(when=self.config.install, why="[server_updates] install", managed_only=True),
                           f"installing Project Reclaimer {release.version}", log)
        return release

    def announce(self, release: releases.ProgramRelease, newer: bool | None) -> None:
        running = ", ".join(f"{n} {v}" for n, v in sorted(self.running_versions().items())) or "no server running"
        themselves = self.self_updating()
        managed = sorted(set(self.states) - themselves)
        check = (" Huragok makes sure each did, and installs it after the game for one that didn't"
                 if self.config.fallback and self.can_take_over() else "")
        if newer and not managed:
            log.info("Project Reclaimer %s is out (running: %s); every server updates itself (auto_update) within "
                     "the hour.%s", release.version, running, check)
        elif newer:
            log.warning("Server program update available: Project Reclaimer %s (running: %s). Players must be on "
                        "the server's exact version. Press U in the window, or: update --program%s",
                        release.version, running,
                        f" ({', '.join(sorted(themselves))} update themselves)" if themselves else "")
        else:
            log.info("Newest server program release: %s (running: %s)", release.version, running)

    async def install(self, *, when: str = "after_game", why: str = "asked for",
                      managed_only: bool = False) -> tuple[bool, str]:
        """Download, verify, put in place, restart each running server (after its game, or now). Self-updating
        servers still behind get it too (one at a time, through take_over), unless managed_only."""
        if self.installing:
            return False, "an install is already under way"
        release = self.pending or await self.check(quiet=False)
        if release is None:
            return False, (f"not installed: {self.last_error}" if self.last_error else
                           "no newer server program is known")
        themselves = self.self_updating()
        behind = [] if managed_only or not self.can_take_over() else \
            [n for n in sorted(themselves) if self.lifecycle.is_up(n) and self.behind(n, release.version)]
        for name in behind:  # asked: tried again however often a swap failed before
            self.swap_failures.pop(name, None)
            self.retry.pop(name, None)
        managed = set(self.states) - themselves
        if not managed and not behind:
            return False, ("every server updates itself (auto_update on in its dedicated.toml) and none running is "
                           f"behind: Project Reclaimer {release.version} reaches each within the hour, or at its "
                           "next start")
        self.installing = True
        try:
            parts: list[str] = []
            if managed:
                ok, message = await self._install_managed(release, when, why, themselves)
                if not ok:
                    return False, message
                parts.append(message)
            for name in behind:
                ok, message = await self.take_over(name, release, why, when=when)
                parts.append(message)
            if themselves - set(behind):
                parts.append(f"{', '.join(sorted(themselves - set(behind)))} update themselves (auto_update)")
            message = "; ".join(parts)
            log.warning(message)
            return True, message
        finally:
            self.installing = False

    async def _install_managed(self, release: releases.ProgramRelease, when: str, why: str,
                               themselves: set[str]) -> tuple[bool, str]:
        """The servers with auto_update off: every copy they run replaced at once, each restarted."""
        log.warning("Installing server program %s (%s)", release.version, why)
        try:
            path = await asyncio.to_thread(releases.download_program, release, self.settings.records)
            replaced = await asyncio.to_thread(releases.install_program, self.settings, path, release.version)
        except (UpdateError, OSError) as error:
            log.error("Server program update failed: %s", error)
            return False, f"not installed: {error}"
        finally:
            try:
                (self.settings.records / release.exe_name).unlink(missing_ok=True)
            except OSError:
                pass
        self.last_installed = release.version
        self.pending = None
        restarted, left = [], []
        changed = {path.resolve() for path in replaced}
        for name in list(self.states):
            if name in themselves or not self.lifecycle.is_up(name):
                continue
            server = self._server(name)
            program = releases.program_of(self.settings, server) if server else None
            if program is None or program.resolve() not in changed:
                continue  # its copy was left: as new as the release, or newer
            try:
                await self.lifecycle.restart(name, when=when, reason=f"updating to {release.version}",
                                             by=self.settings.name)
                restarted.append(name)
            except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError
                left.append(f"{name}: {error}")
        message = (f"server program {release.version} installed in {len(replaced)} place(s); "
                   + (f"restarting {', '.join(restarted)} {when.replace('_', ' ')}" if restarted
                      else "no server was running; they start on it next time"))
        if left:
            message += "; not restarted: " + "; ".join(left)
        self.bus.emit("server.program_updated", None, version=release.version, restarted=restarted)
        return True, message

    def status(self) -> dict[str, Any]:
        return {"pending": self.pending.version if self.pending else None, "running": self.running_versions(),
                "installing": self.installing, "last_installed": self.last_installed,
                "self_updating": sorted(self.self_updating()),
                "newest": self.newest.version if self.newest else None,
                "taken_over": dict(self.taken), "failed": dict(self.failed), "updater": dict(self.updater)}


def _hours(hours: float) -> str:
    return f"{hours:g} hour{'s' if hours != 1 else ''}"
