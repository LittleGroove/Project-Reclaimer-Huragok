"""The supervisor: wires its parts together and keeps them running.

    SERVERS/<folder>/dedicated.toml ──► discovery (every 10 s) ──► one manager, RCON client and
    servers ──RCON──► RconClient ─┐                                 file watcher per server
    server.log, status.json ──► FileWatcher ─┤
    `dedicated` console ──► Backend ─┤──► EventBus ──► StateTracker, Fleet (managers), CommandRouter
                                     │              └─► each part's own queue (PartRunner)
    Keepalive ◄── states ◄───────────┘
    the window (menu.py) or typed commands ──► Fleet verbs; records/requests/ from outside
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__
from .chat import CommandRouter
from .config import ConfigError, FolderProblem, ServerConfig, Settings, discover_servers
from .console import HELP as CONSOLE_HELP
from .console import Console
from .context import PartContext
from .events import Event, EventBus
from .files import FileWatcher
from .lifecycle import Keepalive, make_backend
from .manager import SETTLED, Fleet, Lifecycle, Result
from .moderation import Moderation
from .part import Part
from .parts import PARTS
from .permissions import PermissionsError, Roles, reload_if_changed
from .rcon import RconClient
from .relay import Relay
from .runner import PartRunner
from .serverupdates import ProgramUpdateService
from .state import StateTracker
from .storage import Storage
from .tui import RecentLog
from .updates import UpdateService, summary

log = logging.getLogger(__name__)

DISCOVER_SECONDS = 10.0


def one_line(text: str) -> str:
    """A message the server sent on several lines, as one log line (the window shows one line per entry)."""
    return " | ".join(part.strip() for part in text.splitlines() if part.strip())


class Supervisor:
    def __init__(self, settings: Settings, *, storage: Storage | None = None, interactive: bool = False,
                 recent: RecentLog | None = None) -> None:
        self.settings = settings
        self.bus = EventBus()
        self.storage = storage or Storage(settings.database)
        self.recent = recent
        self.clients: dict[str, RconClient] = {}
        self.tracker = StateTracker(self.bus, self.clients)
        self.states = self.tracker.states
        self.watchers: dict[str, FileWatcher] = {}
        self.backend = make_backend(settings, self.bus, self.states)
        self.fleet = Fleet(settings, self.backend, self.bus, on_all_stopped=self._all_stopped, on_request=self._request)
        self.lifecycle = Lifecycle(self.fleet)
        self.keepalive = Keepalive(settings.keepalive, self.lifecycle, self.states, self.bus)
        self.moderation = Moderation(self.clients, self.states, self.storage, self.bus, dry_run=settings.dry_run)
        self.roles = Roles(settings)
        self.commands = CommandRouter(settings, self.bus, self.states, self.clients, self.roles)
        self.runners: dict[str, PartRunner] = {}
        self.part_problems: dict[str, str] = {}
        self.problems: list[str] = []  # shown in red on the dashboard
        # On stopping: True stops the servers too (a stop request), False leaves them running (an update),
        # None does what the backend's settings say.
        self.stop_servers: bool | None = None
        self.relaunch = False  # stopped to run a newly installed version
        self.interactive = interactive
        self.stopping = False
        self.updates = UpdateService(settings, self.bus, self.relaunch_now)
        self.program_updates = ProgramUpdateService(settings, self.bus, self.lifecycle, self.states, self.clients)
        self.relay = Relay(settings, self.clients, self.states, self.bus)
        self.bus.subscribe("rcon.join", self._remember_player)
        self.bus.subscribe("rcon.control", self._note_reload)
        self.bus.subscribe("server.workshop", self._note_workshop)
        self.bus.subscribe("server.map_skipped", self._note_workshop)
        self.bus.subscribe("log.online", self._wake_rcon)
        self.bus.subscribe("server.started", self._wake_rcon)
        self._tasks: list[asyncio.Task[None]] = []
        self._server_tasks: dict[str, list[asyncio.Task[None]]] = {}
        self._running = False
        self._folder_problems: list[str] = [str(p) for p in settings.problems]
        for problem in self._folder_problems:
            log.error("Server folders: %s", problem)
        self._stop = asyncio.Event()
        for server in list(settings.servers):
            self.add_server(server, listed=True)
        self._refresh_problems()

    # Servers ----------------------------------------------------------------------------------

    def add_server(self, server: ServerConfig, *, listed: bool = False) -> None:
        """One more server to look after, at the start or found in SERVERS/ while running."""
        settings = self.settings
        client = RconClient(server, self.bus, use_host_key=settings.use_host_key,
                            command_timeout=settings.command_timeout)
        self.clients[server.name] = client
        state = self.tracker.add(server.name, client)
        self.watchers[server.name] = FileWatcher(server, state, self.bus)
        self.fleet.add(server, client, state)
        if not listed:
            settings.servers.append(server)
        if self._running:
            self._server_tasks[server.name] = [
                self._spawn(f"RCON {server.name}", client.run),
                self._spawn(f"file watcher {server.name}", self.watchers[server.name].run),
            ]
            asyncio.ensure_future(self.relay.add_server(server))
        self._refresh_problems()

    def remove_server(self, name: str) -> None:
        for task in self._server_tasks.pop(name, []):
            task.cancel()
        client = self.clients.pop(name, None)
        if client:
            client.stop()
        self.watchers.pop(name, None)
        self.relay.remove_server(name)
        self.fleet.remove(name)
        self.tracker.remove(name)
        self.settings.servers[:] = [s for s in self.settings.servers if s.name != name]
        self._refresh_problems()

    def _refresh_problems(self) -> None:
        problems = []
        for server in self.settings.servers:
            for problem in self.backend.problems(server):
                problems.append(f"{server.name}: {problem}")
        problems.extend(self._folder_problems)
        problems.extend(self.relay.problems)
        self.problems = problems

    def discover(self) -> None:
        """Server folders added or removed while running."""
        left_out: list[FolderProblem] = []
        try:
            found = discover_servers(self.settings, left_out)
        except ConfigError as error:
            left_out, found = [FolderProblem(None, str(error))], None
        texts = [str(p) for p in left_out]
        if texts != self._folder_problems:
            for text in texts:
                if text not in self._folder_problems:
                    log.error("Server folders: %s", text)
            self._folder_problems = texts
            self._refresh_problems()
        if found is None:
            return
        broken = {p.folder for p in left_out if p.folder is not None}
        current = {s.name: s for s in self.settings.servers}
        for server in found:
            if server.name not in current:
                log.info("[%s] new server folder: %s", server.name, server.folder)
                self.add_server(server)
            else:
                current[server.name].errors = server.errors  # e.g. RCON switched on since
                current[server.name].autostart = server.autostart
        for name, server in current.items():
            if any(s.name == name for s in found) or server.folder in broken:
                continue  # a folder that can't be read for now isn't gone: its server is kept as it was
            manager = self.fleet.managers.get(name)
            if manager is not None and manager.phase in SETTLED and not manager.alive:
                log.info("[%s] its server folder is gone (%s); forgetting it", name, server.folder)
                self.remove_server(name)
            elif manager is not None:
                manager.note(f"its server folder is gone ({server.folder}), but it's still running; it's forgotten "
                             "once it stops")

    async def _discover_loop(self) -> None:
        while True:
            await asyncio.sleep(DISCOVER_SECONDS)
            self.discover()

    # Events --------------------------------------------------------------------------------

    def _wake_rcon(self, event: Event) -> None:
        client = self.clients.get(event.server or "")
        if client and not client.connected:
            client.wake()

    def _remember_player(self, event: Event) -> None:
        if event.get("id"):
            self.storage.record_join(str(event["id"]).lower(), str(event.get("name", "")), event.get("ip"), event.time)

    def _note_reload(self, event: Event) -> None:
        """The server re-read its files (an edit, `reload`): say what it did, so the window shows it."""
        action = str(event.get("action") or "")
        if action not in ("reload", "reload_applied"):
            return
        text = one_line(str(event.get("text") or "")) or "no details"
        if event.get("ok", True):
            log.info("[%s] %s: %s", event.server, "settings reloaded" if action == "reload" else "reload applied", text)
        else:
            log.warning("[%s] settings NOT reloaded, nothing changed: %s", event.server, text)

    def _note_workshop(self, event: Event) -> None:
        """What the server says about its Workshop items, so the window shows the fetch and its outcome."""
        if event.name == "server.map_skipped":
            log.warning("[%s] a mod map can't be used: %s (%s): the item's own packaging; ask its author",
                        event.server, Path(str(event.get("path") or "")).name, event.get("reason"))
            return
        text = str(event.get("text") or "")
        level = logging.WARNING if text.lower().startswith(("error", "failed", "could not")) else logging.INFO
        log.log(level, "[%s] Workshop %s: %s", event.server, event.get("item"), one_line(text))

    # Parts -----------------------------------------------------------------------------------

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

    # Files edited while running ----------------------------------------------------------------

    def check_files(self) -> None:
        """commands.toml reloads when edited."""
        try:
            fresh = reload_if_changed(self.settings.permissions)
        except PermissionsError as error:
            log.error("commands.toml not reloaded, keeping what was in force: %s", error)
        else:
            if fresh is not None:
                self.settings.permissions = fresh
                log.info("commands.toml reloaded")

    async def _watch_files(self) -> None:
        while True:
            await asyncio.sleep(2)
            self.check_files()

    # Requests from the window, the typed console, or outside ----------------------------------------

    async def _request(self, verb: str, options: dict[str, Any]) -> Result:
        if verb == "check_update":
            release = await self.updates.check(quiet=False)
            if release is None:
                return Result(True, f"Reclaimer Supervisor {__version__} is the newest version" if self.settings.updates.check
                              or True else "")
            notes = "\n".join(summary(release.notes))
            return Result(True, f"update available: {release.version} (this is {__version__}); what's new: {release.page}",
                          notes or f"version {release.version}")
        if verb == "update":
            if not self.updates.can_install:
                return Result(False, "this copy can't update itself: it doesn't run from its own engine/ folder")
            installed = await self.updates.install(str(options.get("why") or "asked for"))
            return Result(installed, "installing; the supervisor restarts on the new version, servers keep running"
                          if installed else "not installed (see the log)")
        if verb == "close":
            log.info("Asked to close; the servers keep running")
            self.stop_servers = False
            asyncio.get_running_loop().call_later(0.5, self.stop)  # after the reply is written
            return Result(True, "closing; the servers keep running")
        if verb == "check_program_update":
            release = await self.program_updates.check(quiet=False)
            status = self.program_updates.status()
            running = ", ".join(f"{n} {v}" for n, v in sorted(status["running"].items())) or "no server running"
            if release is None:
                return Result(True, f"the servers are on the newest server program ({running})")
            notes = "\n".join(summary(release.notes))
            newer = self.program_updates.is_newer(release)
            return Result(True, f"server program {release.version} is out ({running}); what's new: {release.page}"
                          if newer else f"newest release: {release.version} ({running}; whether it's newer isn't "
                                        "known until a server is running)", notes or f"version {release.version}")
        if verb == "update_program":
            when = str(options.get("when") or "after_game")
            ok, message = await self.program_updates.install(when=when, why=str(options.get("why") or "asked for"))
            return Result(ok, message)
        return Result(False, f"unknown request '{verb}'")

    def stop_everything(self, *, immediate: bool = False) -> None:
        """Stop every server (warning players unless immediate), then the supervisor."""
        log.info("Stopping everything%s", " now" if immediate else ", warning players first")
        self.stop_servers = False  # the fleet stops them; close() must not do it again
        results = self.fleet.stop_all(immediate=immediate)
        for name, result in results:
            log.info("[%s] %s", name, result.message)
        if not results:
            self.stop()

    def _all_stopped(self) -> None:
        log.info("Every server is stopped; the supervisor stops now")
        self.stop_servers = False
        self.stop()

    def relaunch_now(self) -> None:
        """A new version is in place: stop, leaving the servers running, so the start loop runs it."""
        self.relaunch = True
        self.stop_servers = False
        self.stop()

    def on_console(self, line: str) -> None:
        """Typed commands, when the window has no dashboard (--no-menu)."""
        words = line.split()
        word, rest = words[0].lower(), words[1:]
        immediate = "--now" in rest
        rest = [w for w in rest if w != "--now"]

        async def show(verb: str, server: str, **options: Any) -> None:
            for name, result in await self.fleet.verb(verb, server, **options):
                log.info("[%s] %s%s", name, result.message, f": {result.text}" if result.text else "")

        if word == "update":
            asyncio.ensure_future(self.updates.install("asked in this window"))
        elif word == "check":
            asyncio.ensure_future(self.updates.check(quiet=False))
        elif word == "status":
            for row in self.fleet.rows():
                bits = [str(row["status"]), f"players {row['players']}" if row["players"] else "",
                        str(row.get("game") or (f"plays {row['plays']}" if row.get("plays") else ""))]
                log.info("[%s] %s", row["name"], " · ".join(bit for bit in bits if bit))
        elif word == "stop" and not rest:
            self.stop_everything(immediate=immediate)
        elif word in ("start", "stop", "restart", "cancel") and rest:
            asyncio.ensure_future(show(word, " ".join(rest), immediate=immediate))
        elif word == "say" and len(rest) >= 2:
            asyncio.ensure_future(show("say", rest[0], text=" ".join(rest[1:])))
        elif word in ("quit", "close"):
            log.info("Closing; the servers keep running")
            self.stop_servers = False
            self.stop()
        else:
            log.info(CONSOLE_HELP)

    # Running -----------------------------------------------------------------------------------

    def _spawn(self, name: str, factory: Callable[[], Awaitable[Any]], *, restart: bool = True) -> asyncio.Task[None]:
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

        task = asyncio.ensure_future(guarded())
        self._tasks.append(task)
        return task

    async def run(self, menu: Any = None) -> None:
        settings = self.settings
        log.info("Reclaimer Supervisor %s · backend %s · %s server(s): %s", __version__, settings.backend,
                 len(settings.servers), ", ".join(s.name for s in settings.servers) or "none yet")
        if settings.dry_run:
            log.warning("Dry run: moderation actions are recorded, never sent")
        self._running = True
        self.start_parts()
        for name, client in self.clients.items():
            self._server_tasks.setdefault(name, []).append(self._spawn(f"RCON {name}", client.run))
        for name, watcher in self.watchers.items():
            self._server_tasks.setdefault(name, []).append(self._spawn(f"file watcher {name}", watcher.run))
        self._spawn("state tracker", self.tracker.run)
        self._spawn("fleet", self.fleet.run)
        self._spawn("keepalive", self.keepalive.run)
        self._spawn("file checks", self._watch_files)
        self._spawn("server folders", self._discover_loop)
        self._spawn("update checks", self.updates.run, restart=False)
        self._spawn("server program updates", self.program_updates.run, restart=False)
        self._spawn("console relay", self.relay.run)
        if menu is not None:
            async def run_menu() -> None:
                outcome = await menu.run()
                if outcome == "quit":
                    log.info("Window closed; the servers keep running")
                    self.stop()

            self._spawn("window", run_menu, restart=False)
        elif self.interactive:
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
        self.stopping = True
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
