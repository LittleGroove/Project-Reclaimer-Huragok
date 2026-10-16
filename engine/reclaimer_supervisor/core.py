"""The supervisor: wires its parts together and keeps them running.

    servers ──RCON──► RconClient ─┐
    server.log, status.json ──► FileWatcher ─┤
    `dedicated` console ──► Backend ─┤──► EventBus ──► StateTracker, Lifecycle, CommandRouter
                                     │              └─► each part's own queue (PartRunner)
    Keepalive ◄── states ◄───────────┘
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from . import __version__
from .chat import CommandRouter
from .config import Settings
from .console import HELP as CONSOLE_HELP
from .console import Console
from .context import PartContext
from .events import Event, EventBus
from .files import FileWatcher
from .lifecycle import Keepalive, Lifecycle, make_backend
from .moderation import Moderation
from .part import Part
from .parts import PARTS
from .permissions import PermissionsError, Roles, reload_if_changed
from .rcon import RconClient
from .runner import PartRunner
from .state import StateTracker
from .storage import Storage
from .updates import UpdateService

log = logging.getLogger(__name__)

STOP_REQUEST = "stop-request"  # in records/: `stop` asks a running supervisor to stop, servers included
UPDATE_REQUEST = "update-request"  # in records/: `update` asks a running supervisor to install a new version


class Supervisor:
    def __init__(self, settings: Settings, *, storage: Storage | None = None, interactive: bool = False) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.storage = storage or Storage(settings.database)
        self.clients = {s.name: RconClient(s, self.bus, use_host_key=settings.use_host_key,
                                           command_timeout=settings.command_timeout) for s in settings.servers}
        self.tracker = StateTracker(self.bus, self.clients)
        self.states = self.tracker.states
        self.watchers = [FileWatcher(s, self.states[s.name], self.bus) for s in settings.servers]
        self.backend = make_backend(settings, self.bus, self.states)
        self.lifecycle = Lifecycle(self.backend, settings, self.clients, self.states, self.bus)
        self.keepalive = Keepalive(settings.keepalive, self.lifecycle, self.states, self.bus)
        self.moderation = Moderation(self.clients, self.states, self.storage, self.bus, dry_run=settings.dry_run)
        self.roles = Roles(settings)
        self.commands = CommandRouter(settings, self.bus, self.states, self.clients, self.roles)
        self.runners: dict[str, PartRunner] = {}
        self.part_problems: dict[str, str] = {}
        # On stopping: True stops the servers too (a stop request), False leaves them running (an update),
        # None does what the backend's settings say.
        self.stop_servers: bool | None = None
        self.relaunch = False  # stopped to run a newly installed version
        self.interactive = interactive
        self.updates = UpdateService(settings, self.bus, self.relaunch_now)
        self.bus.subscribe("rcon.join", self._remember_player)
        self.bus.subscribe("log.online", self._wake_rcon)
        self.bus.subscribe("server.started", self._wake_rcon)
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()

    def _wake_rcon(self, event: Event) -> None:
        client = self.clients.get(event.server or "")
        if client and not client.connected:
            client.wake()

    def _remember_player(self, event: Event) -> None:
        if event.get("id"):
            self.storage.record_join(str(event["id"]).lower(), str(event.get("name", "")), event.get("ip"), event.time)

    # Parts -------------------------------------------------------------------------

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
        part_settings = self.settings.parts[cls.name]
        config: dict[str, Any] = {**cls.defaults, **part_settings.options}
        ctx = PartContext(self, name=cls.name, config=config,
                          dry_run=self.settings.dry_run or part_settings.dry_run,
                          acts_as=str(config.get("name") or cls.acts_as or cls.name.title()))
        part = cls(ctx)
        part.check_config()
        runner = PartRunner(part, self.bus, handler_timeout=part_settings.handler_timeout)
        for command, method in runner.commands:
            self.commands.register(command, runner, method)
        return runner

    # Files edited while running --------------------------------------------------------

    def check_files(self) -> None:
        """commands.toml reloads when edited; a stop request (from `stop`) stops everything."""
        try:
            fresh = reload_if_changed(self.settings.permissions)
        except PermissionsError as error:
            log.error("commands.toml not reloaded, keeping what was in force: %s", error)
        else:
            if fresh is not None:
                self.settings.permissions = fresh
                log.info("commands.toml reloaded")
        request = self.settings.records / STOP_REQUEST
        if request.exists():
            request.unlink(missing_ok=True)
            log.info("Asked to stop, with the servers")
            self.stop_servers = True
            self.stop()
        request = self.settings.records / UPDATE_REQUEST
        if request.exists():
            request.unlink(missing_ok=True)
            asyncio.ensure_future(self.updates.install("asked from outside this window"))

    def relaunch_now(self) -> None:
        """A new version is in place: stop, leaving the servers running, so the start loop runs it."""
        self.relaunch = True
        self.stop_servers = False
        self.stop()

    def on_console(self, line: str) -> None:
        word = line.split()[0].lower()
        if word == "update":
            asyncio.ensure_future(self.updates.install("asked in this window"))
        elif word == "check":
            asyncio.ensure_future(self.updates.check(quiet=False))
        elif word == "status":
            for name, state in self.states.items():
                log.info("[%s] %s players · %s %s · RCON %s", name, len(state.players), state.phase or "?",
                         state.map or "", "connected" if state.connected else "not connected")
        elif word == "stop":
            log.info("Stopping, with the servers")
            self.stop_servers = True
            self.stop()
        else:
            log.info(CONSOLE_HELP)

    async def _watch_files(self) -> None:
        while True:
            await asyncio.sleep(2)
            self.check_files()

    # Running ---------------------------------------------------------------------

    def _spawn(self, name: str, factory: Callable[[], Awaitable[Any]], *, restart: bool = True) -> None:
        async def guarded() -> None:
            while True:
                try:
                    await factory()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one part failing must not stop the rest
                    log.exception("%s failed%s", name, "; restarting it in 5 s" if restart else "")
                    if not restart:
                        return
                    await asyncio.sleep(5)

        self._tasks.append(asyncio.ensure_future(guarded()))

    async def run(self) -> None:
        settings = self.settings
        log.info("Reclaimer Supervisor %s · backend %s · %s server(s): %s", __version__, settings.backend,
                 len(settings.servers), ", ".join(s.name for s in settings.servers))
        if settings.dry_run:
            log.warning("Dry run: moderation actions are recorded, never sent")
        self.start_parts()
        for name, client in self.clients.items():
            self._spawn(f"RCON {name}", client.run)  # returns for good only on a refused password
        self._spawn("state tracker", self.tracker.run)
        for watcher in self.watchers:
            self._spawn(f"file watcher {watcher.server.name}", watcher.run)
        self._spawn(f"backend {self.backend.name}", self.backend.run)
        self._spawn("lifecycle", self.lifecycle.run)
        self._spawn("keepalive", self.keepalive.run)
        self._spawn("file checks", self._watch_files)
        self._spawn("update checks", self.updates.run, restart=False)
        if self.interactive:
            Console(asyncio.get_running_loop(), self.on_console).start()
            log.info(CONSOLE_HELP)
        for name, runner in self.runners.items():
            self._spawn(name, runner.run, restart=False)
        self.bus.emit("supervisor.started", None, version=__version__, parts=list(self.runners))
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        log.info("Stopping")
        self.bus.emit("supervisor.stopping", None)
        for runner in self.runners.values():
            await runner.stop()
        for client in self.clients.values():
            client.stop()
        try:
            await self.backend.close(stop_servers=self.stop_servers)
        except Exception:  # noqa: BLE001
            log.exception("backend close failed")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.storage.close()
