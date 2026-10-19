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
import time
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
from .manager import BUSY, SETTLED, Fleet, Lifecycle, Result
from .moderation import Moderation
from .part import Part
from .parts import PARTS
from .permissions import PermissionsError, Roles, reload_if_changed
from .rcon import RconClient
from .relay import Relay
from .repair import GameFiles
from .runner import PartRunner, handler_time_limit
from .serverupdates import ProgramUpdateService
from .state import StateTracker
from .storage import Storage
from .tui import RecentLog
from .updates import UpdateService, summary
from .workshopupdates import WorkshopUpdateService

log = logging.getLogger(__name__)

DISCOVER_SECONDS = 10.0
RELAUNCH_WAIT = 1800.0  # a new version waits at most this long for servers part way through a restart
RELAUNCH_POLL = 2.0
RESTART_SECONDS = 5.0  # a background task that failed runs again after this long


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
        self._relaunching = False  # a new version is installed: Huragok restarts on it when the servers allow
        self.interactive = interactive
        self.stopping = False
        self.updates = UpdateService(settings, self.bus, self.relaunch_now)
        self.program_updates = ProgramUpdateService(settings, self.bus, self.lifecycle, self.states, self.clients)
        self.workshop_updates = WorkshopUpdateService(settings, self.bus, self.lifecycle, tool=self._tool)
        # What a server arrives without (a ban list, Halo 3's files…), put right before it starts: repair.py.
        self.game_files = GameFiles(settings, self.bus, self._tool, release=self.fleet.release)
        self.fleet.prelaunch = self.game_files.prelaunch
        self.relay = Relay(settings, self.clients, self.states, self.bus)
        self.bus.subscribe("rcon.join", self._remember_player)
        self.bus.subscribe("rcon.control", self._note_reload)
        self.bus.subscribe("server.workshop", self._note_workshop)
        self.bus.subscribe("server.map_skipped", self._note_workshop)
        self.bus.subscribe("log.online", self._wake_rcon)
        self.bus.subscribe("server.started", self._wake_rcon)
        self.bus.subscribe("log.online", self._note_address)
        self.bus.subscribe("server.started", self._note_address)
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
        try:  # a folder copied in from elsewhere: its ban list, content folder, game path and SteamCMD put right
            self.game_files.arrived(server)
        except Exception:  # noqa: BLE001 - the server is looked after as it is; its start says what's wrong
            log.exception("[%s] couldn't look at what its folder needs", server.name)
        if not listed:
            # Found while running (a new folder, or one whose dedicated.toml was fixed): if we started it before,
            # it's picked up again, not taken for a server started some other way.
            settings.servers.append(server)
            try:
                self.backend.adopt_server(server)
            except Exception:  # noqa: BLE001 - a damaged record mustn't keep the server out
                log.exception("[%s] couldn't look for it among the servers started before", server.name)
        self.fleet.add(server, client, state)
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
        problems.extend(self.settings.retired)
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
                known = current[server.name]
                known.errors = server.errors  # e.g. RCON switched on since
                known.autostart = server.autostart
                changed = known.auto_update != server.auto_update
                for key in ("auto_update", "content_dir", "workshop_items", "workshop_check_minutes",
                            "workshop_updates", "plays", "plays_kind"):
                    setattr(known, key, getattr(server, key))
                if changed:
                    log.info("[%s] auto_update is now %s in its dedicated.toml%s", server.name,
                             "on" if server.auto_update else "off",
                             " (from its next restart)" if self.fleet.managers.get(server.name) and
                             self.lifecycle.is_up(server.name) else "")
                    self._refresh_problems()
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

    def _tool(self) -> Any:
        """The server program's own commands (`dedicated mods`…), for jobs like a Workshop update."""
        from .dedicated import tool_for

        return tool_for(self.settings)

    # Events --------------------------------------------------------------------------------

    def _wake_rcon(self, event: Event) -> None:
        client = self.clients.get(event.server or "")
        if client and not client.connected:
            client.wake()

    def _note_address(self, event: Event) -> None:
        state = self.states.get(event.server or "")
        if state is not None and event.get("address"):
            state.address = str(event.get("address"))

    def _remember_player(self, event: Event) -> None:
        if event.get("id"):
            self.storage.record_join(str(event["id"]).lower(), str(event.get("name", "")), event.get("ip"), event.time)

    def _note_reload(self, event: Event) -> None:
        """The server re-read its files (an edit, `reload`): say what it did, so the window shows it."""
        action = str(event.get("action") or "")
        if action not in ("reload", "reload_applied"):
            return
        text = one_line(str(event.get("text") or "")) or "no details"
        ok = bool(event.get("ok", True))
        state = self.states.get(event.server or "")
        if state is not None:
            state.last_reload = (ok, text, event.time)
        if ok:
            log.info("[%s] %s: %s", event.server, "settings reloaded" if action == "reload" else "reload applied", text)
            self.lifecycle.set_flag(event.server or "", "reload", None)
        else:
            log.warning("[%s] settings NOT reloaded, nothing changed: %s", event.server, text)
            self.lifecycle.set_flag(event.server or "", "reload", f"! settings NOT reloaded: {text[:90]}")

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
        runner = PartRunner(part, self.bus, handler_timeout=handler_time_limit(part_settings.handler_timeout,
                                                                              self.settings.command_timeout))
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
                self.bus.emit("supervisor.commands_reloaded")

    async def _watch_files(self) -> None:
        while True:
            await asyncio.sleep(2)
            self.check_files()

    # Requests from the window, the typed console, or outside ----------------------------------------

    async def _request(self, verb: str, options: dict[str, Any]) -> Result:
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
        from .repair import DEPOTS, LABELS, shared_target, sources

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
        running = self.fleet.unstoppable()
        if running:
            log.error("Every server is stopped except %s, whose process couldn't be ended (see above); Huragok stops "
                      "now and leaves it running", ", ".join(running))
        else:
            log.info("Every server is stopped; Huragok stops now")
        self.stop_servers = False
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
        waiting = self.relaunch_waits_for()
        if not waiting:
            self._relaunch()
            return
        log.info("Huragok's new version is installed; it restarts on it once %s %s done (in %s at most)",
                 ", ".join(waiting), "is" if len(waiting) == 1 else "are", f"{RELAUNCH_WAIT / 60:.0f} minutes")
        self._spawn("restart on the new version", self._relaunch_when_idle, restart=False)

    def relaunch_waits_for(self) -> list[str]:
        """The servers a restart of Huragok would disturb now."""
        return [name for name, m in self.fleet.managers.items()
                if m.phase in BUSY or m.phase == "backoff" or m.planned is not None or m.jobs]

    async def _relaunch_when_idle(self) -> None:
        deadline = time.monotonic() + RELAUNCH_WAIT
        waiting = self.relaunch_waits_for()
        while waiting and time.monotonic() < deadline:
            await asyncio.sleep(RELAUNCH_POLL)
            waiting = self.relaunch_waits_for()
        if waiting:
            log.warning("Huragok restarts on its new version now, after waiting %s: %s %s still busy",
                        f"{RELAUNCH_WAIT / 60:.0f} minutes", ", ".join(waiting), "is" if len(waiting) == 1 else "are")
        self._relaunch()

    def _relaunch(self) -> None:
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
                    log.exception("%s failed%s", name, f"; restarting it in {RESTART_SECONDS:.0f} s" if restart else "")
                    if not restart:
                        return
                    await asyncio.sleep(RESTART_SECONDS)

        task = asyncio.ensure_future(guarded())
        self._tasks.append(task)
        return task

    async def run(self, menu: Any = None) -> None:
        settings = self.settings
        log.info("Huragok %s · backend %s · %s server(s): %s", __version__, settings.backend,
                 len(settings.servers), ", ".join(s.name for s in settings.servers) or "none yet")
        if settings.dry_run:
            log.warning("Dry run: moderation actions are recorded, never sent")
        for line in settings.retired:
            log.warning(line)
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
        self._spawn("Workshop updates", self.workshop_updates.run)
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
