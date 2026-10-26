"""Huragok, the Engineer: the program itself, and what starts everything.

Quick to Adjust found New Mombasa's dying city AI, mended it and carried it out of the city with its subroutine
Vergil (lore.py). Here the Huragok is the program: it holds what everything shares and builds the other two on it.

    Huragok (this file)     the settings, the event bus, the database, every background task; its own services:
                            its updates (core/updates.py), the server program's (content/programupdates.py), the
                            Workshop's (content/workshop.py), the game files (content/gamefiles.py), the mod checks
                            (content/mods.py); the parts of both named parts below, each with its own context
      Superintendent        (superintendent.py) built on Huragok: keeps every server up
        Vergil              (vergil.py) built on Huragok and the Superintendent: watches over the players

    SERVERS/<folder>/dedicated.toml ──► Superintendent: a console, live state, log watcher and manager per server
    servers' consoles, logs, status.json, the `dedicated` console ──► event bus ──► the live state, the fleet, the
    router (Vergil) and each part's own queue
    the window or typed commands ──► the fleet's verbs; records/requests/ from outside ──► `request`
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from . import __version__
from .content.gamefiles import GameFiles, shared_target, sources
from .content.mods import ModWatch
from .core import background
from .core.context import PartContext
from .core.events import EventBus
from .core.logs import RecentLog
from .core.part import Part
from .core.requests import Result
from .core.runner import PartRunner, handler_time_limit
from .core.settings import Settings
from .core.storage import Storage
from .games.files import DEPOTS, LABELS
from .lore import SUPERINTENDENT
from .parts import PARTS
from .program.dedicated import tool_for
from .superintendent import Superintendent
from .updates import hostfiles
from .updates.engine import UpdateService, summary
from .updates.program import ProgramUpdateService
from .updates.workshop import WorkshopUpdateService
from .vergil import Vergil

log = logging.getLogger(__name__)

RELAUNCH_WAIT = 1800.0  # a new version waits at most this long for servers part way through a restart
RELAUNCH_POLL = 2.0
RESTART_SECONDS = 5.0  # a background task that failed runs again after this long


class Huragok:
    def __init__(self, settings: Settings, *, storage: Storage | None = None, recent: RecentLog | None = None) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.storage = storage or Storage(settings.database)
        self.recent = recent  # the window's recent lines
        self.runners: dict[str, PartRunner] = {}
        self.part_problems: dict[str, str] = {}
        self.problems: list[str] = []  # shown in red on the dashboard
        # On stopping: True stops the servers too (a stop request), False leaves them running (an update),
        # None does what the backend's settings say.
        self.stop_servers: bool | None = None
        self.relaunch = False  # stopped to run a newly installed version
        self._relaunching = False  # a new version is installed: Huragok restarts on it when the servers allow
        self.stopping = False
        self.running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

        self.superintendent = Superintendent(self)
        self.vergil = Vergil(self, self.superintendent)

        # What Huragok builds and mends while it runs.
        fleet, lifecycle = self.superintendent.fleet, self.superintendent.lifecycle
        self.updates = UpdateService(settings, self.bus, self.relaunch_now)
        self.program_updates = ProgramUpdateService(settings, self.bus, lifecycle, self.superintendent.states,
                                                    self.superintendent.clients)
        self.workshop_updates = WorkshopUpdateService(settings, self.bus, lifecycle, tool=self.tool)
        # What a server arrives without (a ban list, Halo 3's files…), put right before it starts.
        self.game_files = GameFiles(settings, self.bus, self.tool, release=fleet.release)
        self.superintendent.listen(self.game_files)
        fleet.prelaunch = self.game_files.prelaunch
        fleet.live_extra = lambda: {"huragok_update": self.updates.status_text()}
        self.mods = ModWatch(settings, self.bus, self.spawn)

        for server in list(settings.servers):
            self.superintendent.add_server(server, listed=True)
        self.refresh_problems()

    def tool(self) -> Any:
        """The server program's own commands (`dedicated mods`…), for jobs like a Workshop update."""
        return tool_for(self.settings)

    def refresh_problems(self) -> None:
        """What the dashboard shows in red: the servers' setup, the relay's, and settings that do nothing now."""
        self.problems = [*self.superintendent.problems(), *self.vergil.problems(), *self.settings.retired]

    # Parts -------------------------------------------------------------------------------------------------

    def start_parts(self) -> None:
        for name, cls in PARTS.items():
            if not self.settings.parts[name].enabled:
                continue
            try:
                self.runners[name] = self._make_runner(cls)
            except ValueError as error:
                self.part_problems[name] = str(error)
                log.error("%s not started: [%s] %s", name, name, error)

    def _make_runner(self, cls: type[Part]) -> PartRunner:
        """The part made with the context of the named part it belongs to, behind its own queue; its commands go to
        Vergil's router (chat is Vergil's, whoever answers)."""
        part_settings = self.settings.parts[cls.name]
        config: dict[str, Any] = {**cls.defaults, **part_settings.options}
        options = dict(name=cls.name, config=config, dry_run=self.settings.dry_run or part_settings.dry_run,
                       acts_as=str(config.get("name") or cls.acts_as or cls.name.title()))
        # The Superintendent's parts see the servers; Vergil's the players too; Huragok's own, like the Engineer that
        # carried both, everything.
        if cls.persona is SUPERINTENDENT:
            ctx: PartContext = self.superintendent.context(**options)
        else:
            ctx = self.vergil.context(**options)
        part = cls(ctx)
        part.check_config()
        runner = PartRunner(part, self.bus, handler_timeout=handler_time_limit(part_settings.handler_timeout,
                                                                              self.settings.command_timeout))
        for command, method in runner.commands:
            self.vergil.commands.register(command, runner, method)
        return runner

    # Files brought up to date ------------------------------------------------------------------------------

    async def _settle_files(self) -> None:
        """hostfiles.py's steps whose change the version before can't read (a new command in commands.toml), once this
        version has been up long enough that the start loop won't put the old one back."""
        await asyncio.sleep(hostfiles.SETTLED_SECONDS)
        settings = self.settings
        try:
            changed = hostfiles.bring_up_to_date(settings.path, settings.records, settings.packages_folder,
                                                     settled=True)
        except Exception:  # noqa: BLE001 - the files as they are still work
            log.exception("Couldn't bring the files up to date; tried again at the next start")
            return
        if changed:
            self.vergil.check_files(force=True)

    # Requests from the window, the typed console, or outside -------------------------------------------------

    async def request(self, verb: str, options: dict[str, Any]) -> Result:
        """The requests Huragok answers itself (the servers' own go to the fleet)."""
        if verb == "check_update":
            release = await self.updates.check(quiet=False)
            if release is None and self.updates.last_error:  # not "the newest": GitHub never answered
                return Result(False, f"couldn't ask GitHub whether there's a newer Huragok: {self.updates.last_error}")
            if release is None:
                return Result(True, f"Huragok {__version__} is the newest version")
            notes = "\n".join(summary(release.notes))
            return Result(True, f"update available: {release.version} (this is {__version__}); what's new: {release.page}",
                          notes or f"version {release.version}")
        if verb == "update":
            if not self.updates.can_install:
                return Result(False, "this copy can't update itself: it doesn't run from its own engine/ folder")
            installed = await self.updates.install(str(options.get("why") or "asked for"))
            return Result(installed, "installing; Huragok restarts on the new version, servers keep running"
                          if installed else f"not installed: {self.updates.last_failure or self.updates.last_error}"
                          if self.updates.last_failure or self.updates.last_error else "not installed (see the log)")
        if verb == "banall":
            ok, text = await self.vergil.ban_everywhere(str(options.get("server") or ""),
                                                         str(options.get("target") or ""), str(options.get("by") or ""))
            return Result(ok, text)
        if verb == "close":
            log.info("Asked to close; the servers keep running")
            self.stop_servers = False
            asyncio.get_running_loop().call_later(0.5, self.stop)  # after the reply is written
            return Result(True, "closing; the servers keep running")
        if verb == "check_program_update":
            release = await self.program_updates.check(quiet=False)
            status = self.program_updates.status()
            running = ", ".join(f"{n} {v}" for n, v in sorted(status["running"].items())) or "no server running"
            if release is None and self.program_updates.last_error:  # not "the newest": no answer to go by
                return Result(False, f"couldn't tell whether there's a newer server program: "
                                     f"{self.program_updates.last_error} ({running})")
            if release is None:
                return Result(True, f"the servers are on the newest server program ({running})")
            notes = "\n".join(summary(release.notes))
            newer = self.program_updates.newer  # a running server's version, or a stopped one's program file
            return Result(True, f"server program {release.version} is out ({running}); what's new: {release.page}"
                          if newer else f"newest release: {release.version} ({running}; whether it's newer isn't "
                                        "known until a server is running)", notes or f"version {release.version}")
        if verb == "update_program":
            when = str(options.get("when") or "after_game")
            ok, message = await self.program_updates.install(when=when, why=str(options.get("why") or "asked for"))
            return Result(ok, message)
        if verb == "check_workshop":
            await self.workshop_updates.check(quiet=False)
            waiting = self.workshop_updates.installable()
            left = [name for name in self.workshop_updates.waiting_lists() if name not in waiting]
            message = (f"new versions waiting on {', '.join(waiting)}" if waiting
                       else "every server has the newest version of its Workshop items" if not left else
                       "nothing for Huragok to install")
            if left:  # a shared content folder: the server program swaps the item for all of them once nobody's on
                message += (f"; {', '.join(left)} share their content folder, so the server program installs theirs "
                            "once nobody's on")
            if self.workshop_updates.last_error:
                message = f"couldn't ask Steam: {self.workshop_updates.last_error}"
            return Result(not self.workshop_updates.last_error, message, "\n".join(self.workshop_updates.report()))
        if verb == "update_workshop":
            when = str(options.get("when") or "after_game")
            results = [(name, await self.workshop_updates.install_now(name, when))
                       for name in (options.get("servers") or self.workshop_updates.installable())]
            if not results:
                return Result(False, "no new Workshop version is waiting")
            return Result(all(ok for _, (ok, _) in results), "; ".join(f"{n}: {m}" for n, (_, m) in results))
        if verb == "game_files":
            return await self._get_game_files(str(options.get("kind") or "halo3"), str(options.get("how") or ""))
        if verb == "game_files_retry":
            self.game_files.try_again()
            return Result(True, "the servers waiting for game files look again now")
        return Result(False, f"unknown request '{verb}'")

    async def _get_game_files(self, kind: str, how: str) -> Result:
        """I's "get Halo 3's files": in the background, so the window stays free; the servers waiting for them start
        once they're in. `how`: "mcc" or "steamcmd" (asked by a person: a download is allowed whatever
        [setup] download_game_files says), or "" for the best way there is."""
        if kind not in DEPOTS:
            return Result(False, f"'{kind}' isn't one of {', '.join(DEPOTS)}")
        target, why = shared_target(self.settings, kind)
        if target is None:
            return Result(False, why)
        found, no_download = await asyncio.to_thread(sources, self.settings, kind, downloads=True)
        source = next((s for s in found if not how or s.how == how), None)
        if source is None:
            return Result(False, f"no way to get {LABELS[kind]} here: " + ("MCC isn't on this PC" if how != "steamcmd"
                                                                            else no_download))
        if not self.game_files.get(kind, target, source):
            return Result(False, f"{LABELS[kind]} are being got already: {self.game_files.getting.get(kind, '')}")
        return Result(True, f"getting {LABELS[kind]} in the background: {source.text(kind)}. The log says when "
                            "they're in, and the servers waiting for them start")

    # Stopping, and restarting on a new version -------------------------------------------------------------

    def stop_everything(self, *, immediate: bool = False) -> None:
        """Stop every server (warning players unless immediate), then Huragok."""
        self.stop_servers = False  # the fleet stops them; close() must not do it again
        if not self.superintendent.stop_everything(immediate=immediate):
            self.stop()

    def relaunch_now(self) -> None:
        """A new version is in place: stop, leaving the servers running, so the start loop runs it.

        Not while a server is part way through something (a countdown, a stop, a start, the jobs before one, a
        backoff) or has a restart planned or jobs waiting: the new Huragok wouldn't know of them, so a countdown
        players were warned of would vanish, a plan or a job would be lost, and a server that doesn't start by
        itself, stopped mid-restart, would be left down. It waits for those, for RELAUNCH_WAIT at most."""
        if self._relaunching:
            return
        self._relaunching = True
        waiting = self.superintendent.busy()
        if not waiting:
            self._relaunch()
            return
        log.info("Huragok's new version is installed; it restarts on it once %s %s done (in %s at most)",
                 ", ".join(waiting), "is" if len(waiting) == 1 else "are", f"{RELAUNCH_WAIT / 60:.0f} minutes")
        self.spawn("restart on the new version", self._relaunch_when_idle, restart=False)

    async def _relaunch_when_idle(self) -> None:
        deadline = time.monotonic() + RELAUNCH_WAIT
        waiting = self.superintendent.busy()
        while waiting and time.monotonic() < deadline:
            await asyncio.sleep(RELAUNCH_POLL)
            waiting = self.superintendent.busy()
        if waiting:
            log.warning("Huragok restarts on its new version now, after waiting %s: %s %s still busy",
                        f"{RELAUNCH_WAIT / 60:.0f} minutes", ", ".join(waiting), "is" if len(waiting) == 1 else "are")
        self._relaunch()

    def _relaunch(self) -> None:
        self.relaunch = True
        self.stop(servers=False)

    # Running -----------------------------------------------------------------------------------------------

    def spawn(self, name: str, factory: Callable[[], Awaitable[Any]], *, restart: bool = True) -> asyncio.Task[None]:
        """A background task: one failing must not stop the rest, so it's logged and (unless restart is False)
        started again after RESTART_SECONDS."""
        async def guarded() -> None:
            while True:
                try:
                    await factory()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one part failing must not stop the rest
                    log.exception("%s failed%s", name, f"; restarting it in {RESTART_SECONDS:.0f} s" if restart else "")
                    if not restart:
                        return
                    await asyncio.sleep(RESTART_SECONDS)

        task = asyncio.ensure_future(guarded())
        self._tasks.append(task)
        task.add_done_callback(self._forget)  # one that ends (a relaunch wait, a skipped map's explanation) goes
        return task

    def _forget(self, task: asyncio.Task[None]) -> None:
        if task in self._tasks:
            self._tasks.remove(task)

    async def run(self, menu: Any = None, *, console: Any = None) -> None:
        """Until stopped. `menu` is the window (window/dashboard.py); without one, `console` reads typed commands
        (commandline/console.py)."""
        settings = self.settings
        log.info("Huragok %s · backend %s · %s server(s): %s", __version__, settings.backend,
                 len(settings.servers), ", ".join(s.name for s in settings.servers) or "none yet")
        if settings.dry_run:
            log.warning("Dry run: moderation actions are recorded, never sent")
        for line in [*settings.retired, *settings.permissions.unknown]:
            log.warning(line)
        self.running = True
        self.start_parts()
        self.superintendent.start()
        self.vergil.start()
        self.spawn("files brought up to date", self._settle_files, restart=False)
        self.spawn("update checks", self.updates.run, restart=False)
        self.spawn("server program updates", self.program_updates.run, restart=False)
        self.spawn("Workshop updates", self.workshop_updates.run)
        self.spawn("mod checks", self.mods.run)
        if menu is not None:
            async def run_menu() -> None:
                outcome = await menu.run()
                if outcome == "quit":
                    log.info("Window closed; the servers keep running")
                    self.stop()

            self.spawn("window", run_menu, restart=False)
        elif console is not None:
            console.start(asyncio.get_running_loop())
        for name, runner in self.runners.items():
            self.spawn(name, runner.run, restart=False)
        self.bus.emit("supervisor.started", None, version=__version__, parts=list(self.runners))
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    def stop(self, *, servers: bool | None = None) -> None:
        """Stop Huragok; `servers` False leaves every server running (an update, `close`), True stops them too."""
        if servers is not None:
            self.stop_servers = servers
        self.stopping = True
        self._stop.set()

    async def shutdown(self) -> None:
        log.info("Stopping")
        self.bus.emit("supervisor.stopping", None)
        for runner in self.runners.values():
            await runner.stop()
        await self.superintendent.close(stop_servers=self.stop_servers)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await background.stop()  # replies, restarts, bans passed on…: before the database they write to closes
        self.storage.close()
