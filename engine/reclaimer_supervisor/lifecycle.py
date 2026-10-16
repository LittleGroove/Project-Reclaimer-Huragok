"""Starting, stopping and restarting servers.

A backend knows how servers run on this machine:
  attach   the host's own setup runs them; we can't start or stop anything
  windows  we run one `dedicated` per server (RECLAIMER_DEDICATED_ONLY), Windows only
  docker   one container per server, through `docker compose`
  command  commands the host gives (systemd units, scripts…), Windows or Linux

Lifecycle plans restarts (now, after this game, when empty) on top of any backend,
and Keepalive restarts a server that has stopped responding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, KeepaliveSettings, ServerConfig, Settings, Table
from .events import Event, EventBus
from .files import FileTail, parse_console_line
from .rcon import RconClient, RconError
from .state import ServerState

log = logging.getLogger(__name__)

RESTART_BACKOFF = (5, 10, 30, 60, 300)  # after `dedicated` itself exits unexpectedly


class LifecycleUnavailable(Exception):
    """This backend can't do that (e.g. attach mode can't restart)."""


class LifecycleError(Exception):
    """Starting or stopping failed."""


class RunningElsewhere(LifecycleError):
    """The server is already running, started some other way: the usual first step of handing it over."""


class Backend:
    name = "attach"
    can_control = False

    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> None:
        self.settings = settings
        self.bus = bus
        self.states = states

    async def start(self, server: ServerConfig) -> None:
        raise LifecycleUnavailable("backend = attach: the host's own setup starts and stops the servers")

    async def stop(self, server: ServerConfig) -> None:
        raise LifecycleUnavailable("backend = attach: the host's own setup starts and stops the servers")

    async def restart(self, server: ServerConfig) -> None:
        await self.stop(server)
        await self.start(server)

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def stop_all(self) -> list[str]:
        """Stop every server this backend runs, e.g. for `stop`. Returns their names."""
        raise LifecycleUnavailable("backend = attach: the supervisor doesn't run the servers; stop them the way "
                                   "they were started")

    async def close(self, stop_servers: bool | None = None) -> None:
        """The supervisor is stopping. stop_servers: True stops them too (`stop`), False leaves them running (an
        update), None does what the settings say."""


def _quote(value: str) -> str:
    if os.name == "nt":
        return '"' + value.replace('"', '\\"') + '"' if any(c in value for c in ' "&|<>^') else value
    return shlex.quote(value)


class CommandBackend(Backend):
    """Runs the host's own commands. {name}, {slug}, {service}, {port} and {folder} are filled in."""

    name = "command"
    DEFAULTS: dict[str, Any] = {}

    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> None:
        super().__init__(settings, bus, states)
        options = Table({**self.DEFAULTS, **settings.backend_options}, self.name)
        self.options = options
        self.commands = {action: options.get(action, str, "") for action in ("start", "stop", "restart")}
        self.timeout = options.get("timeout", float, 120.0)
        self.can_control = bool(self.commands["restart"] or (self.commands["start"] and self.commands["stop"]))

    def values(self, server: ServerConfig) -> dict[str, str]:
        index = next((i for i, s in enumerate(self.settings.servers, 1) if s.name == server.name), 1)
        return {"name": server.name, "slug": server.slug, "service": server.service or f"server-{index}",
                "port": str(server.game_port), "folder": str(self.settings.server_folder)}

    def render(self, action: str, server: ServerConfig) -> str:
        template = self.commands[action]
        if not template:
            raise LifecycleUnavailable(f"[{self.name}] {action} isn't set")
        values = {k: _quote(v) for k, v in self.values(server).items()}
        values.update({k: _quote(str(v)) for k, v in self.options.data.items()
                       if k not in ("start", "stop", "restart") and isinstance(v, (str, int, float))})
        return template.format(**values)

    async def _run(self, action: str, server: ServerConfig) -> None:
        command = self.render(action, server)
        log.info("[%s] %s: %s", server.name, action, command)
        process = await asyncio.create_subprocess_shell(
            command, cwd=self.settings.server_folder,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            process.kill()
            raise LifecycleError(f"{action} took longer than {self.timeout:.0f} s: {command}") from None
        text = output.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise LifecycleError(f"{action} failed with exit code {process.returncode}: {text[-500:]}")
        if text:
            log.debug("[%s] %s output: %s", server.name, action, text[-2000:])

    async def start(self, server: ServerConfig) -> None:
        await self._run("start", server)

    async def stop(self, server: ServerConfig) -> None:
        await self._run("stop", server)

    async def restart(self, server: ServerConfig) -> None:
        if self.commands["restart"]:
            await self._run("restart", server)
        else:
            await super().restart(server)

    async def stop_all(self) -> list[str]:
        stopped = []
        for server in self.settings.servers:
            try:
                await self.stop(server)
                stopped.append(server.name)
            except LifecycleError as error:
                log.error("[%s] not stopped: %s", server.name, error)
        return stopped

    async def close(self, stop_servers: bool | None = None) -> None:
        if stop_servers and self.commands["stop"]:
            await self.stop_all()


class DockerBackend(CommandBackend):
    """One container per server, as the official Docker setup runs them."""

    name = "docker"
    DEFAULTS = {
        "compose_file": "compose.yaml",
        "start": "docker compose -f {compose_file} up -d {service}",
        "stop": "docker compose -f {compose_file} stop {service}",
        "restart": "docker compose -f {compose_file} restart {service}",
    }


@dataclass
class Managed:
    server: ServerConfig
    pid: int
    create_time: float
    console: FileTail
    console_path: Path
    popen: subprocess.Popen[bytes] | None = None  # None when adopted after our own restart
    stopping: bool = False
    started: float = field(default_factory=time.monotonic)


class WindowsBackend(Backend):
    """Runs `project-reclaimer.exe dedicated run` once per server, each with
    RECLAIMER_DEDICATED_ONLY, so restarting one server never touches the others.
    The built-in supervisor inside each keeps restarting its game after crashes;
    we restart `dedicated` itself if it ever exits."""

    name = "windows"
    can_control = True

    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> None:
        if os.name != "nt":
            raise ConfigError("backend = windows runs only on Windows; on Linux use docker or command")
        super().__init__(settings, bus, states)
        options = Table(settings.backend_options, "windows")
        self.executable = settings.server_folder / options.get("executable", str, "project-reclaimer.exe")
        self.stop_timeout = options.get("stop_timeout", float, 30.0)
        self.stop_on_exit = options.get("stop_servers_on_exit", bool, False)
        self.disable_auto_update = options.get("disable_auto_update", bool, True)
        self.state_file = settings.records / "running-servers.json"
        self.managed: dict[str, Managed] = {}
        self._failures: dict[str, int] = {}
        self._restarts: dict[str, asyncio.Task[None]] = {}

    # Processes ----------------------------------------------------------------

    def _alive(self, managed: Managed) -> bool:
        if managed.popen is not None:
            return managed.popen.poll() is None
        import psutil
        try:
            process = psutil.Process(managed.pid)
            return abs(process.create_time() - managed.create_time) < 1 and process.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def _save(self) -> None:
        data = {name: {"pid": m.pid, "create_time": m.create_time, "console": str(m.console_path)}
                for name, m in self.managed.items()}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def _adopt(self) -> None:
        """Pick up servers we started before our own restart."""
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for server in self.settings.servers:
            entry = saved.get(server.name)
            if not entry:
                continue
            path = Path(entry["console"])
            tail = FileTail(path, from_start=False)
            tail.read_lines()
            managed = Managed(server, int(entry["pid"]), float(entry["create_time"]), tail, path)
            if self._alive(managed):
                self.managed[server.name] = managed
                self.states[server.name].pid = None
                log.info("[%s] found our `dedicated` still running (process %s)", server.name, managed.pid)
        self._save()

    @staticmethod
    def _running_elsewhere(server: ServerConfig) -> bool:
        """Something already listens on the server's RCON port (the same number as its game port)."""
        endpoint = server.rcon_endpoint()
        host, port = (endpoint.host, endpoint.port) if endpoint else ("127.0.0.1", server.rcon_port)
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    async def start(self, server: ServerConfig) -> None:
        current = self.managed.get(server.name)
        if current and self._alive(current):
            return
        if self._running_elsewhere(server):
            raise RunningElsewhere(f"it's already running, started some other way (something answers on its "
                                 f"RCON port {server.rcon_port}). Until it's started from here, the supervisor "
                                 "watches and moderates it but can't restart it. To hand it over: stop it (and "
                                 "remove whatever started it, such as a startup task), then start the "
                                 "supervisor again")
        if not self.executable.is_file():
            raise LifecycleError(f"{self.executable} not found: set [windows] executable")
        from . import winproc
        import psutil

        logs = self.settings.logs
        logs.mkdir(parents=True, exist_ok=True)
        console_path = logs / f"console-{server.slug}.log"
        console_path.touch()
        tail = FileTail(console_path, from_start=False)
        tail.read_lines()  # start reading at today's end of the file
        env = dict(os.environ, RECLAIMER_DEDICATED_ONLY=server.name)
        if self.disable_auto_update:
            env["RECLAIMER_DEDICATED_AUTO_UPDATE"] = "false"  # our updater picks a quiet moment instead
        with console_path.open("ab") as output:
            popen = subprocess.Popen(
                [str(self.executable), "dedicated", "run", "--config", str(self.settings.dedicated_config)],
                cwd=self.settings.server_folder, env=env, stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, creationflags=winproc.creation_flags())
        try:
            create_time = psutil.Process(popen.pid).create_time()
        except psutil.Error:
            create_time = time.time()
        self.managed[server.name] = Managed(server, popen.pid, create_time, tail, console_path, popen)
        self._save()
        log.info("[%s] started `dedicated` (process %s)", server.name, popen.pid)
        self.bus.emit("server.starting", server.name, pid=popen.pid)

    async def stop(self, server: ServerConfig) -> None:
        managed = self.managed.get(server.name)
        if managed is None:
            return
        managed.stopping = True
        if self._alive(managed):
            from . import winproc

            self.bus.emit("server.stopping", server.name, pid=managed.pid)
            sent = await asyncio.to_thread(winproc.ctrl_break, managed.pid)
            if not sent or not await self._wait_exit(managed, self.stop_timeout):
                log.warning("[%s] `dedicated` didn't stop cleanly; ending it", server.name)
                await asyncio.to_thread(self._kill_tree, managed.pid)
                await self._wait_exit(managed, 10)
        self._drain_console(managed)
        self.managed.pop(server.name, None)
        self.states[server.name].pid = None
        self._save()
        self.bus.emit("server.stopped", server.name, expected=True)

    async def restart(self, server: ServerConfig) -> None:
        await self.stop(server)
        try:
            await self.start(server)
        except LifecycleError:
            self._schedule_restart(server)  # never leave it stopped: keep trying with backoff
            raise

    async def _wait_exit(self, managed: Managed, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive(managed):
                return True
            await asyncio.sleep(0.2)
        return not self._alive(managed)

    @staticmethod
    def _kill_tree(pid: int) -> None:
        import psutil

        try:
            parent = psutil.Process(pid)
            for process in [*parent.children(recursive=True), parent]:
                try:
                    process.kill()
                except psutil.Error:
                    pass
        except psutil.Error:
            pass

    # Watching -------------------------------------------------------------------

    def _drain_console(self, managed: Managed) -> None:
        for line in managed.console.read_lines():
            parsed = parse_console_line(line)
            if parsed is None:
                continue
            name, data = parsed
            data.pop("server", None)
            if name == "server.started":
                self.states[managed.server.name].pid = data.get("pid")
            if name == "server.crashed":
                log.warning("[%s] %s", managed.server.name, line.strip())
            self.bus.emit(name, managed.server.name, **data)

    async def run(self) -> None:
        self._adopt()
        for server in self.settings.servers:
            if server.name not in self.managed:
                try:
                    await self.start(server)
                except RunningElsewhere as error:
                    log.warning("[%s] %s", server.name, error)
                except LifecycleError as error:
                    log.error("[%s] not started: %s", server.name, error)
        while True:
            await asyncio.sleep(1)
            for name, managed in list(self.managed.items()):
                self._drain_console(managed)
                if managed.stopping or self._alive(managed):
                    if time.monotonic() - managed.started > 600:
                        self._failures.pop(name, None)
                    continue
                code = managed.popen.returncode if managed.popen else None
                self.managed.pop(name)
                self._save()
                log.error("[%s] `dedicated` exited unexpectedly (exit code %s)", name, code)
                self.bus.emit("server.stopped", name, expected=False, exit_code=code)
                self._schedule_restart(managed.server)

    def _schedule_restart(self, server: ServerConfig) -> None:
        failures = self._failures.get(server.name, 0)
        self._failures[server.name] = failures + 1
        delay = RESTART_BACKOFF[min(failures, len(RESTART_BACKOFF) - 1)]
        log.info("[%s] starting `dedicated` again in %s s", server.name, delay)

        async def later() -> None:
            await asyncio.sleep(delay)
            try:
                await self.start(server)
            except LifecycleError as error:
                log.error("[%s] couldn't start: %s", server.name, error)
                self._schedule_restart(server)

        self._restarts[server.name] = asyncio.ensure_future(later())

    async def stop_all(self) -> list[str]:
        if not self.managed:
            self._adopt()  # no supervisor running: the servers a previous one left running
        stopped = list(self.managed)
        for name in stopped:
            await self.stop(self.managed[name].server)
        return stopped

    async def close(self, stop_servers: bool | None = None) -> None:
        for task in self._restarts.values():
            task.cancel()
        if self.stop_on_exit if stop_servers is None else stop_servers:
            await self.stop_all()
        else:
            self._save()
            if self.managed:
                log.info("Left running, so players weren't disturbed: %s. Start the supervisor again to pick "
                         "them up, or stop them with: start.cmd stop", ", ".join(self.managed))


BACKEND_TYPES: dict[str, type[Backend]] = {
    "attach": Backend, "command": CommandBackend, "docker": DockerBackend, "windows": WindowsBackend,
}


def make_backend(settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> Backend:
    return BACKEND_TYPES[settings.backend](settings, bus, states)


# Planned restarts -------------------------------------------------------------------

WHEN = ("now", "after_game", "when_empty")


@dataclass
class PlannedRestart:
    server: str
    when: str
    reason: str
    by: str
    requested: float = field(default_factory=time.time)


class Lifecycle:
    """Restarts at a good moment: now (with a countdown), after this game, or when empty."""

    def __init__(self, backend: Backend, settings: Settings, clients: dict[str, RconClient],
                 states: dict[str, ServerState], bus: EventBus, *, after_game_limit: float = 1800,
                 countdown: float = 30) -> None:
        self.backend = backend
        self.settings = settings
        self.clients = clients
        self.states = states
        self.bus = bus
        self.after_game_limit = after_game_limit
        self.countdown = countdown
        self.planned: dict[str, PlannedRestart] = {}
        self.busy: set[str] = set()
        self.last_restart: dict[str, float] = {}
        # (map, mode) of the game each server is playing. The server can send `finished`
        # for the next game before it starts (seen on 0.9.18), which must not count.
        self.playing: dict[str, tuple[Any, Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        bus.subscribe("rcon.phase", self._on_phase)
        bus.subscribe("rcon.leave", self._on_leave)

    @property
    def can_control(self) -> bool:
        return self.backend.can_control

    async def restart(self, server: str, *, when: str = "now", reason: str = "", by: str | None = None,
                      countdown: float | None = None) -> PlannedRestart:
        if not self.can_control:
            raise LifecycleUnavailable(f"backend = {self.backend.name} can't restart servers")
        if when not in WHEN:
            raise ValueError(f"when must be one of {', '.join(WHEN)}")
        state = self.states[server]
        plan = PlannedRestart(server, when, reason, by or self.settings.name)
        self.planned[server] = plan
        log.info("[%s] restart planned (%s): %s", server, when, reason or "no reason given")
        self.bus.emit("server.restart_planned", server, when=when, reason=reason, by=plan.by)
        if state.phase == "playing" and server not in self.playing:
            self.playing[server] = (state.map, state.mode)
        empty = not state.players
        if when == "now" or empty or (when == "after_game" and state.phase == "finished"):
            self._spawn(self._execute(server, self.countdown if countdown is None else countdown))
        elif when == "after_game":
            await self._say(server, f"This server restarts after this game{': ' + reason if reason else '.'}")
        return plan

    def cancel(self, server: str) -> bool:
        return self.planned.pop(server, None) is not None

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.ensure_future(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_phase(self, event: Event) -> None:
        server, phase = event.server or "", event.get("phase")
        game = (event.get("map"), event.get("mode"))
        if phase == "playing":
            self.playing[server] = game
            return
        if phase != "finished" or self.playing.get(server) != game:
            return
        self.playing.pop(server, None)
        plan = self.planned.get(server)
        if plan and plan.when == "after_game":
            self._spawn(self._execute(plan.server, 0))

    def _on_leave(self, event: Event) -> None:
        plan = self.planned.get(event.server or "")
        if plan and plan.when in ("after_game", "when_empty"):
            state = self.states[plan.server]
            # The state tracker removes the player in its own listener; count without them.
            others = [p for p in state.players.values() if p.number != event.get("number")]
            if not others:
                self._spawn(self._execute(plan.server, 0))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(5)
            for plan in list(self.planned.values()):
                if plan.when == "after_game" and time.time() - plan.requested > self.after_game_limit:
                    self._spawn(self._execute(plan.server, self.countdown))

    async def _say(self, server: str, text: str) -> None:
        client = self.clients.get(server)
        if client and client.connected:
            try:
                await client.command("say", text)
            except RconError:
                pass

    async def _execute(self, server: str, countdown: float) -> None:
        if server in self.busy:
            return
        plan = self.planned.pop(server, None) or PlannedRestart(server, "now", "", self.settings.name)
        self.busy.add(server)
        try:
            reason = f": {plan.reason}" if plan.reason else "."
            if countdown and self.states[server].players:
                await self._say(server, f"Server restarting in {countdown:.0f} seconds{reason}")
                if countdown > 10:
                    await asyncio.sleep(countdown - 10)
                    await self._say(server, "Server restarting in 10 seconds.")
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(countdown)
            elif self.states[server].players:
                await self._say(server, f"Server restarting now{reason}")
            self.bus.emit("server.restarting", server, reason=plan.reason, by=plan.by)
            await self.backend.restart(self.settings.server(server))
            self.last_restart[server] = time.time()
            self.bus.emit("server.restarted", server, reason=plan.reason, by=plan.by)
        except Exception as error:  # noqa: BLE001 - report every failure, keep running
            log.error("[%s] restart failed: %s", server, error)
            self.bus.emit("server.restart_failed", server, error=str(error))
        finally:
            self.busy.discard(server)


# Keepalive -----------------------------------------------------------------------------

class Keepalive:
    """Notices a server that has stopped responding, and restarts it when the backend can.

    Hung means both: status.json not rewritten (normally every 5 s) and RCON not
    answering, each for hang_seconds. Two independent signals keep false alarms
    rare; a slow server that still answers is the watchdog's business.
    """

    MIN_GAP = 600.0  # never restart the same server for hanging more often than this

    def __init__(self, settings: KeepaliveSettings, lifecycle: Lifecycle, states: dict[str, ServerState],
                 bus: EventBus) -> None:
        self.settings = settings
        self.lifecycle = lifecycle
        self.states = states
        self.bus = bus
        self.hung: set[str] = set()
        self._acted: dict[str, float] = {}

    def is_hung(self, state: ServerState) -> bool:
        age = state.status_age
        quiet = None if state.last_rcon_ok is None else time.monotonic() - state.last_rcon_ok
        limit = self.settings.hang_seconds
        return age is not None and age > limit and (quiet is None or quiet > limit)

    def check(self) -> None:
        for name, state in self.states.items():
            if name in self.lifecycle.busy:
                continue
            if self.is_hung(state):
                if name not in self.hung:
                    self.hung.add(name)
                    log.error("[%s] not responding: status.json last written %.0f s ago, RCON quiet",
                              name, state.status_age or 0)
                    self.bus.emit("server.hung", name, status_age=state.status_age)
                self._maybe_act(name)
            elif name in self.hung:
                self.hung.discard(name)
                log.info("[%s] responding again", name)
                self.bus.emit("server.recovered", name)

    def _maybe_act(self, name: str) -> None:
        if not (self.settings.act and self.lifecycle.can_control):
            return
        if time.monotonic() - self._acted.get(name, -self.MIN_GAP) < self.MIN_GAP:
            return
        self._acted[name] = time.monotonic()
        asyncio.ensure_future(self.lifecycle.restart(name, when="now", reason="not responding", countdown=0))

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.check_seconds)
            self.check()
