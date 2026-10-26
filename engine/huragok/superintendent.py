"""The Superintendent: keeps every server up.

New Mombasa's city AI kept its lights, trains and signs running (lore.py); here it keeps every server running. It is
built on Huragok (huragok.py), which hands it the settings, the event bus and the background tasks, and it owns what
keeps a server up, one server apart from another:

    SERVERS/<folder>/dedicated.toml ──► a Server: its console (rcon.py), its live picture (servers/state.py), its
                                        log watcher (servers/logs.py) and its manager (servers/server.py)
    the backend (servers/backends.py) starts, stops and watches the processes; the fleet (servers/fleet.py) ticks
    every manager once a second; the keepalive (servers/keepalive.py) restarts a hung server
    its parts: the watchdog (servers/watchdog.py) and the event log (servers/eventlog.py)

Anything else that keeps something per server (Vergil's relay, Huragok's game files) listens for servers coming and
going (`listen`). The Superintendent never knows Vergil.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from .core.context import PartContext
from .core.events import Event
from .core.serverfolders import FolderProblem, ServerConfig, discover_servers
from .core.tables import ConfigError
from .core.text import one_line
from .rcon import RconClient
from .servers.backends import make_backend
from .servers.fleet import Fleet, Lifecycle
from .servers.keepalive import Keepalive
from .servers.logs import FileWatcher
from .servers.server import BUSY, SETTLED, ServerManager
from .servers.state import Player, ServerState, StateTracker

if TYPE_CHECKING:
    from .huragok import Huragok

log = logging.getLogger(__name__)

DISCOVER_SECONDS = 10.0  # server folders added or removed while running are found this often


class ServerListener(Protocol):
    """Something that keeps a piece of its own for each server: told as servers come and go."""

    def server_added(self, server: ServerConfig, *, running: bool) -> None: ...

    def server_removed(self, name: str) -> None: ...


@dataclass(frozen=True)
class Server:
    """One server, as the Superintendent looks after it."""

    config: ServerConfig
    client: RconClient
    state: ServerState
    watcher: FileWatcher
    manager: ServerManager

    @property
    def name(self) -> str:
        return self.config.name


class Superintendent:
    def __init__(self, huragok: Huragok) -> None:
        self.huragok = huragok
        settings, bus = huragok.settings, huragok.bus
        self.settings = settings
        self.bus = bus
        self._storage = huragok.storage  # where the ballots' picks are kept
        self.clients: dict[str, RconClient] = {}
        self.tracker = StateTracker(bus, self.clients)
        self.states = self.tracker.states
        self.watchers: dict[str, FileWatcher] = {}
        self.backend = make_backend(settings, bus, self.states)
        self.fleet = Fleet(settings, self.backend, bus, on_all_stopped=self._all_stopped, on_request=huragok.request)
        self.lifecycle = Lifecycle(self.fleet)
        self.keepalive = Keepalive(settings.keepalive, self.lifecycle, self.states, bus)
        self.listeners: list[ServerListener] = []
        self.folder_problems: list[str] = [str(p) for p in settings.problems]
        for problem in self.folder_problems:
            log.error("Server folders: %s", problem)
        self._server_tasks: dict[str, list[Any]] = {}
        bus.subscribe("log.online", self._wake_rcon)
        bus.subscribe("server.started", self._wake_rcon)
        bus.subscribe("log.online", self._note_address)
        bus.subscribe("server.started", self._note_address)
        bus.subscribe("rcon.control", self._note_reload)
        bus.subscribe("log.ballot_picked", self._note_pick)

    def listen(self, listener: ServerListener) -> None:
        self.listeners.append(listener)

    # The servers ----------------------------------------------------------------------------------------------

    @property
    def servers(self) -> dict[str, Server]:
        """Each server looked after, by name."""
        return {name: Server(manager.server, manager.client, manager.state, self.watchers[name], manager)
                for name, manager in self.fleet.managers.items() if name in self.watchers}

    def add_server(self, server: ServerConfig, *, listed: bool = False) -> None:
        """One more server to look after, at the start or found in SERVERS/ while running."""
        settings = self.settings
        client = RconClient(server, self.bus, use_host_key=settings.use_host_key,
                            command_timeout=settings.command_timeout)
        self.clients[server.name] = client
        state = self.tracker.add(server.name, client)
        self.watchers[server.name] = FileWatcher(server, state, self.bus)
        for listener in self.listeners:
            try:  # Huragok's game files: what a folder copied in from elsewhere lacks, put right
                listener.server_added(server, running=self.huragok.running)
            except Exception:  # noqa: BLE001 - the server is looked after as it is; its start says what's wrong
                log.exception("[%s] couldn't make ready what %s keeps for it", server.name, type(listener).__name__)
        if not listed:
            # Found while running (a new folder, or one whose dedicated.toml was fixed): if we started it before,
            # it's picked up again, not taken for a server started some other way.
            settings.servers.append(server)
            try:
                self.backend.adopt_server(server)
            except Exception:  # noqa: BLE001 - a damaged record mustn't keep the server out
                log.exception("[%s] couldn't look for it among the servers started before", server.name)
        self.fleet.add(server, client, state)
        if self.huragok.running:
            self._server_tasks[server.name] = self._start_server_tasks(server.name)
        self.huragok.refresh_problems()

    def remove_server(self, name: str) -> None:
        for task in self._server_tasks.pop(name, []):
            task.cancel()
        client = self.clients.pop(name, None)
        if client:
            client.stop()
        self.watchers.pop(name, None)
        for listener in self.listeners:
            listener.server_removed(name)
        self.fleet.remove(name)
        self.tracker.remove(name)
        self.settings.servers[:] = [s for s in self.settings.servers if s.name != name]
        self.huragok.refresh_problems()

    def _start_server_tasks(self, name: str) -> list[Any]:
        return [self.huragok.spawn(f"RCON {name}", self.clients[name].run),
                self.huragok.spawn(f"file watcher {name}", self.watchers[name].run)]

    def problems(self) -> list[str]:
        """What's wrong with the servers' setup, for the dashboard: each backend problem, then the folders."""
        found = [f"{server.name}: {problem}" for server in self.settings.servers
                 for problem in self.backend.problems(server)]
        return found + self.folder_problems

    def discover(self) -> None:
        """Server folders added or removed while running."""
        left_out: list[FolderProblem] = []
        try:
            found = discover_servers(self.settings, left_out)
        except ConfigError as error:
            left_out, found = [FolderProblem(None, str(error))], None
        texts = [str(p) for p in left_out]
        if texts != self.folder_problems:
            for text in texts:
                if text not in self.folder_problems:
                    log.error("Server folders: %s", text)
            self.folder_problems = texts
            self.huragok.refresh_problems()
        if found is None:
            return
        broken = {p.folder for p in left_out if p.folder is not None}
        current = {s.name: s for s in self.settings.servers}
        for server in found:
            if server.name not in current:
                log.info("[%s] new server folder: %s", server.name, server.folder)
                self.add_server(server)
                continue
            known = current[server.name]
            known.errors = server.errors  # e.g. RCON switched on since
            known.autostart = server.autostart
            changed = known.auto_update != server.auto_update
            for key in ("auto_update", "content_dir", "workshop_items", "workshop_check_minutes",
                        "workshop_updates", "priority", "automod", "plays", "plays_kind"):
                setattr(known, key, getattr(server, key))
            if changed:
                log.info("[%s] auto_update is now %s in its dedicated.toml%s", server.name,
                         "on" if server.auto_update else "off",
                         " (from its next restart)" if self.fleet.managers.get(server.name) and
                         self.lifecycle.is_up(server.name) else "")
                self.huragok.refresh_problems()
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

    # What the servers say -----------------------------------------------------------------------------------

    def _wake_rcon(self, event: Event) -> None:
        client = self.clients.get(event.server or "")
        if client and not client.connected:
            client.wake()

    def _note_address(self, event: Event) -> None:
        state = self.states.get(event.server or "")
        if state is not None and event.get("address"):
            state.address = str(event.get("address"))

    def _note_pick(self, event: Event) -> None:
        """A game players picked on a ballot, kept for what's popular and what never gets played (decision 7)."""
        state = self.states.get(event.server or "")
        picked = str(event.get("picked") or "")
        if state is None or not picked or not state.ballot_playlist:
            return
        server = self.settings.server(event.server or "")
        try:
            self._storage.record_pick(event.server or "", state.ballot_playlist, picked,
                                      server.plays if server is not None else "", event.time)
        except sqlite3.Error as error:
            log.debug("[%s] couldn't keep the ballot's pick: %s", event.server, error)

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

    # Stopping -----------------------------------------------------------------------------------------------

    def stop_everything(self, *, immediate: bool = False) -> bool:
        """Stop every server, warning players unless immediate. False when none was running (nothing to wait for)."""
        log.info("Stopping everything%s", " now" if immediate else ", warning players first")
        results = self.fleet.stop_all(immediate=immediate)
        for name, result in results:
            log.info("[%s] %s", name, result.message)
        return bool(results)

    def _all_stopped(self) -> None:
        running = self.fleet.unstoppable()
        if running:
            log.error("Every server is stopped except %s, whose process couldn't be ended (see above); Huragok stops "
                      "now and leaves it running", ", ".join(running))
        else:
            log.info("Every server is stopped; Huragok stops now")
        self.huragok.stop(servers=False)

    def busy(self) -> list[str]:
        """The servers part way through something (a countdown, a stop, a start, the jobs before one, a backoff) or
        with a restart planned or jobs waiting: what a restart of Huragok would disturb now."""
        return [name for name, m in self.fleet.managers.items()
                if m.phase in BUSY or m.phase == "backoff" or m.planned is not None or m.jobs]

    # Running ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Its background work, once Huragok runs: each server's console and log, the live picture, the fleet's
        tick, the keepalive and the folders' discovery."""
        spawn = self.huragok.spawn
        for name in self.clients:
            self._server_tasks.setdefault(name, []).extend(self._start_server_tasks(name))
        spawn("state tracker", self.tracker.run)
        spawn("fleet", self.fleet.run)
        spawn("keepalive", self.keepalive.run)
        spawn("server folders", self._discover_loop)

    async def close(self, *, stop_servers: bool | None) -> None:
        for client in self.clients.values():
            client.stop()
        try:
            await self.backend.close(stop_servers=stop_servers)
        except Exception:  # noqa: BLE001
            log.exception("backend close failed")

    def context(self, **options: Any) -> SuperintendentContext:
        return SuperintendentContext(self.huragok, self, **options)


class SuperintendentContext(PartContext):
    """self.ctx for the Superintendent's parts (and, through VergilContext, Vergil's): the servers too."""

    def __init__(self, huragok: Huragok, superintendent: Superintendent, **options: Any) -> None:
        super().__init__(huragok, **options)
        self._superintendent = superintendent

    @property
    def servers(self) -> Mapping[str, ServerState]:
        """Each server's live picture, by name."""
        return self._superintendent.states

    def server(self, name: str) -> ServerState:
        return self._superintendent.states[name]

    def plays_kind(self, server: str) -> str:
        """What kind of thing the server plays: playlist, map, variant, campaign, odst or default ("" if unknown)."""
        try:
            return self._superintendent.settings.server(server).plays_kind
        except KeyError:
            return ""

    def server_option(self, server: str, key: str) -> Any:
        """One of Huragok's own settings for this server (its [[server]] block), or None for one it doesn't set."""
        try:
            return getattr(self._superintendent.settings.server(server), key, None)
        except KeyError:
            return None

    def config_path(self, server: str) -> Path | None:
        """The server's dedicated.toml (the server re-reads it by itself when it changes), or None for one Huragok's
        settings don't list."""
        try:
            return self._superintendent.settings.server(server).config_path
        except KeyError:
            return None

    def pid(self, server: str) -> int | None:
        """The server's process: the one the backend started (its game runs under it), or the game's own."""
        manager = self._superintendent.fleet.managers.get(server)
        state = self._superintendent.states.get(server)
        return (manager.pid if manager is not None else None) or (state.pid if state is not None else None)

    async def say(self, server: str, text: str) -> None:
        """A [Server] line everyone on the server reads."""
        await self._superintendent.clients[server].command("say", text)

    async def tell(self, server: str, player: Player | str, text: str) -> None:
        """A [Server] line only this player reads."""
        ref = player.id if isinstance(player, Player) else player
        await self._superintendent.clients[server].command("tell", ref, text)

    @property
    def lifecycle(self) -> Lifecycle:
        """Restart a server: now, after this game, or when it's empty."""
        return self._superintendent.lifecycle

    def rcon(self, server: str) -> RconClient:
        """The raw console, for what the rest doesn't cover."""
        return self._superintendent.clients[server]


__all__ = ["Server", "ServerListener", "Superintendent", "SuperintendentContext"]
