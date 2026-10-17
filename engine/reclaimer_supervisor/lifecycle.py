"""How servers run on this machine: the backends, and the keepalive.

A backend starts and stops server processes; it decides nothing. The decisions
(when to start, warn, restart, give up) are the per-server managers' (manager.py).

  process  one `dedicated` per server, run by the supervisor, on Windows (the exe
           itself) or Linux (through `wine`, or whatever [process] command says)
  docker   one container per server, through `docker compose`
  command  commands the host gives (systemd units, scripts...), Windows or Linux
  attach   the host's own setup runs them; the supervisor watches and moderates only

Keepalive restarts a server that has stopped responding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .config import ConfigError, KeepaliveSettings, ServerConfig, Settings, Table
from .events import EventBus
from .files import FileTail, parse_console_line
from .state import ServerState

if TYPE_CHECKING:
    from .manager import Lifecycle

log = logging.getLogger(__name__)


class LifecycleUnavailable(Exception):
    """This backend can't do that (e.g. attach mode can't restart)."""


class LifecycleError(Exception):
    """Starting or stopping failed."""


def port_in_use(server: ServerConfig, timeout: float = 0.5) -> bool:
    """Something listens on the server's RCON port (the same number as its game port)."""
    endpoint = server.rcon_endpoint()
    host, port = (endpoint.host, endpoint.port) if endpoint else ("127.0.0.1", server.rcon_port)
    if not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Backend:
    """attach: the host's own setup starts and stops the servers."""

    name = "attach"
    can_start = False
    can_stop = False
    can_restart = False
    sees_processes = False  # True: alive() is certain; False: the manager infers it from RCON and status.json
    restart_only = False  # True: restart() is the only way to restart (a `command` backend with just `restart`)

    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> None:
        self.settings = settings
        self.bus = bus
        self.states = states

    def _unavailable(self, what: str) -> LifecycleUnavailable:
        return LifecycleUnavailable(f"backend = {self.name}: the supervisor doesn't {what} the servers; "
                                    "the host's own setup does")

    async def start(self, server: ServerConfig) -> None:
        raise self._unavailable("start")

    async def stop(self, server: ServerConfig, timeout: float | None = None) -> None:
        raise self._unavailable("stop")

    async def restart(self, server: ServerConfig) -> None:
        await self.stop(server)
        await self.start(server)

    def alive(self, server: ServerConfig) -> bool | None:
        """True/False when the backend knows the process; None when it can't tell."""
        return None

    def started_at(self, server: ServerConfig) -> float | None:
        """When the server's process started (Unix seconds), if the backend knows."""
        return None

    def exit_code(self, server: ServerConfig) -> int | None:
        return None

    def adopt(self) -> None:
        """Pick up servers a previous supervisor left running."""

    def forget(self, server: ServerConfig) -> None:
        """The server's process is gone (not stopped by us): drop what we kept about it."""

    def drain(self, server: ServerConfig) -> None:
        """Turn new console output into events."""

    def last_console_lines(self, server: ServerConfig, count: int = 200) -> list[str]:
        return []

    def problems(self, server: ServerConfig) -> list[str]:
        """What would stop this backend from starting the server, for `check`."""
        return []

    async def stop_all(self) -> list[str]:
        """Stop every server this backend runs (for `stop` with no supervisor running). Returns their names."""
        raise self._unavailable("stop")

    async def close(self, stop_servers: bool | None = None) -> None:
        """The supervisor is stopping. stop_servers: True stops them too, False leaves them running, None
        does what the settings say."""


def _quote(value: str) -> str:
    if os.name == "nt":
        return '"' + value.replace('"', '\\"') + '"' if any(c in value for c in ' "&|<>^') else value
    return shlex.quote(value)


class CommandBackend(Backend):
    """Runs the host's own commands. {name}, {slug}, {service}, {port}, {folder} and {config} are filled in."""

    name = "command"
    DEFAULTS: dict[str, Any] = {}

    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> None:
        super().__init__(settings, bus, states)
        options = Table({**self.DEFAULTS, **settings.backend_options}, self.name)
        self.options = options
        self.commands = {action: options.get(action, str, "") for action in ("start", "stop", "restart")}
        self.timeout = options.get("timeout", float, 120.0)
        self.can_start = bool(self.commands["start"])
        self.can_stop = bool(self.commands["stop"])
        self.can_restart = bool(self.commands["restart"] or (self.commands["start"] and self.commands["stop"]))
        self.restart_only = bool(self.commands["restart"]) and not (self.can_start and self.can_stop)

    def values(self, server: ServerConfig) -> dict[str, str]:
        index = next((i for i, s in enumerate(self.settings.servers, 1) if s.name == server.name), 1)
        return {"name": server.name, "slug": server.slug, "service": server.service or f"server-{index}",
                "port": str(server.game_port), "folder": str(server.folder), "config": str(server.config_path)}

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
            command, cwd=server.folder,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            process.kill()
            raise LifecycleError(f"{action} took longer than {self.timeout:.0f} s: {command}") from None
        text = output.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise LifecycleError(f"{action} failed with exit code {process.returncode}"
                                 + (f": {text[-500:]}" if text else ""))
        if text:
            log.debug("[%s] %s output: %s", server.name, action, text[-2000:])

    async def start(self, server: ServerConfig) -> None:
        await self._run("start", server)
        self.bus.emit("server.starting", server.name)

    async def stop(self, server: ServerConfig, timeout: float | None = None) -> None:
        self.bus.emit("server.stopping", server.name)
        await self._run("stop", server)
        self.bus.emit("server.stopped", server.name, expected=True)

    async def restart(self, server: ServerConfig) -> None:
        if self.commands["restart"]:
            await self._run("restart", server)
        else:
            await super().restart(server)

    def problems(self, server: ServerConfig) -> list[str]:
        if not self.can_restart:
            return [f"[{self.name}] needs restart, or start and stop, to control servers"]
        return []

    async def stop_all(self) -> list[str]:
        if not self.can_stop:
            raise self._unavailable("stop")
        stopped = []
        for server in self.settings.servers:
            try:
                await self.stop(server)
                stopped.append(server.name)
            except LifecycleError as error:
                log.error("[%s] not stopped: %s", server.name, error)
        return stopped

    async def close(self, stop_servers: bool | None = None) -> None:
        if stop_servers and self.can_stop:
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
    exit_code: int | None = None


class ProcessBackend(Backend):
    """Runs `project-reclaimer.exe dedicated run` once per server, each with
    RECLAIMER_DEDICATED_ONLY, so restarting one server never touches the others.

    The process outlives us: its own process group (Windows) or session (Linux), no
    window of its own, output to logs/console-<server>.log, stdin from nowhere. A
    clean stop is Ctrl+Break (Windows) or SIGINT (Linux): `dedicated` prints
    "Stopping every server... All servers stopped." and exits in under a second.
    On Linux the program runs through `wine` unless [process] command says otherwise.
    """

    name = "process"
    can_start = True
    can_stop = True
    can_restart = True
    sees_processes = True

    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> None:
        super().__init__(settings, bus, states)
        options = Table(settings.backend_options, "process")
        self.executable = options.get("executable", str, "project-reclaimer.exe")
        self.command_template = options.strings("command")
        self.stop_timeout = options.get("stop_timeout", float, 30.0)
        self.stop_on_exit = options.get("stop_servers_on_exit", bool, False)
        self.disable_auto_update = options.get("disable_auto_update", bool, True)
        self.state_file = settings.records / "running-servers.json"
        self.managed: dict[str, Managed] = {}

    # The command ------------------------------------------------------------------

    def executable_for(self, server: ServerConfig) -> Path | None:
        """The server program: in the server's folder (the configured name, or the release's own name), or one
        copy at the root for every server (they must all be on the same version anyway)."""
        from .dedicated import find_executable

        return find_executable(server.folder, self.executable) or find_executable(self.settings.home, self.executable)

    def command_for(self, server: ServerConfig) -> list[str]:
        exe = self.executable_for(server)
        values = {"executable": str(exe) if exe else str(server.folder / self.executable),
                  "config": server.config_path.name, "folder": str(server.folder), "name": server.name}
        if self.command_template:
            return [part.format(**values) for part in self.command_template]
        if exe is None:
            raise LifecycleError(f"{server.folder / self.executable} not found: put the server program in the "
                                 f"server's folder or at {self.settings.home} (one copy for every server), or set "
                                 "[process] command")
        program = [str(exe)] if os.name == "nt" else ["wine", str(exe)]
        return [*program, "dedicated", "run", "--config", server.config_path.name]

    def problems(self, server: ServerConfig) -> list[str]:
        found = []
        if not self.command_template:
            if self.executable_for(server) is None:
                found.append(f"{self.executable} is not in {server.folder} or at {self.settings.home} "
                             "(set [process] command to run it another way)")
            if os.name != "nt" and shutil.which("wine") is None:
                found.append("wine is not installed, and on Linux the server program runs through it "
                             "(or set [process] command)")
        elif shutil.which(self.command_template[0]) is None and not Path(self.command_template[0]).is_file():
            found.append(f"[process] command: {self.command_template[0]} was not found")
        return found

    # Processes ---------------------------------------------------------------------

    @staticmethod
    def _alive(managed: Managed) -> bool:
        if managed.popen is not None:
            if managed.popen.poll() is None:
                return True
            managed.exit_code = managed.popen.returncode
            return False
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

    def adopt(self) -> None:
        """Pick up servers we started before our own restart."""
        try:
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for server in self.settings.servers:
            entry = saved.get(server.name)
            if not entry or server.name in self.managed:
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

    def alive(self, server: ServerConfig) -> bool:
        managed = self.managed.get(server.name)
        return managed is not None and self._alive(managed)

    def started_at(self, server: ServerConfig) -> float | None:
        managed = self.managed.get(server.name)
        return managed.create_time if managed else None

    def exit_code(self, server: ServerConfig) -> int | None:
        managed = self.managed.get(server.name)
        return managed.exit_code if managed else None

    def pid(self, server: ServerConfig) -> int | None:
        managed = self.managed.get(server.name)
        return managed.pid if managed else None

    def console_path(self, server: ServerConfig) -> Path:
        return self.settings.logs / f"console-{server.slug}.log"

    async def start(self, server: ServerConfig) -> None:
        current = self.managed.get(server.name)
        if current and self._alive(current):
            return
        command = self.command_for(server)
        logs = self.settings.logs
        logs.mkdir(parents=True, exist_ok=True)
        console_path = self.console_path(server)
        console_path.touch()
        tail = FileTail(console_path, from_start=False)
        tail.read_lines()  # start reading at today's end of the file
        env = dict(os.environ, RECLAIMER_DEDICATED_ONLY=server.name)
        if self.disable_auto_update:
            env["RECLAIMER_DEDICATED_AUTO_UPDATE"] = "false"  # our updater picks a quiet moment instead
        extra: dict[str, Any] = {}
        if os.name == "nt":
            from . import winproc

            extra["creationflags"] = winproc.creation_flags()
        else:
            extra["start_new_session"] = True  # its own session: our exit, or our terminal closing, never reaches it
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        try:
            with console_path.open("ab") as output:
                output.write(f"──── {stamp} UTC · started by the supervisor · {' '.join(command)} ────\n".encode("utf-8"))
                popen = subprocess.Popen(command, cwd=server.folder, env=env, stdin=subprocess.DEVNULL,
                                         stdout=output, stderr=subprocess.STDOUT, **extra)
        except OSError as error:
            raise LifecycleError(f"couldn't run {command[0]}: {error}") from None
        try:
            create_time = psutil.Process(popen.pid).create_time()
        except psutil.Error:
            create_time = time.time()
        self.managed[server.name] = Managed(server, popen.pid, create_time, tail, console_path, popen)
        self._save()
        log.info("[%s] started `dedicated` (process %s)", server.name, popen.pid)
        self.bus.emit("server.starting", server.name, pid=popen.pid)

    def _signal_stop(self, managed: Managed) -> bool:
        """Ask `dedicated` to stop cleanly. True when the request reached it."""
        if os.name == "nt":
            from . import winproc

            return winproc.ctrl_break(managed.pid)
        try:
            os.killpg(os.getpgid(managed.pid), signal.SIGINT)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(managed.pid, signal.SIGINT)
                return True
            except OSError:
                return False

    async def stop(self, server: ServerConfig, timeout: float | None = None) -> None:
        managed = self.managed.get(server.name)
        if managed is None:
            return
        managed.stopping = True
        if self._alive(managed):
            self.bus.emit("server.stopping", server.name, pid=managed.pid)
            sent = await asyncio.to_thread(self._signal_stop, managed)
            if not sent or not await self._wait_exit(managed, self.stop_timeout if timeout is None else timeout):
                log.warning("[%s] `dedicated` didn't stop cleanly; ending it", server.name)
                await asyncio.to_thread(self._kill_tree, managed.pid)
                await self._wait_exit(managed, 10)
        self.drain(server)
        self.managed.pop(server.name, None)
        self.states[server.name].pid = None
        self._save()
        self.bus.emit("server.stopped", server.name, expected=True, exit_code=managed.exit_code)

    def forget(self, server: ServerConfig) -> None:
        managed = self.managed.pop(server.name, None)
        if managed is None:
            return
        self.drain_managed(managed)
        self.states[server.name].pid = None
        self._save()
        self.bus.emit("server.stopped", server.name, expected=False, exit_code=managed.exit_code)

    async def _wait_exit(self, managed: Managed, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._alive(managed):
                return True
            await asyncio.sleep(0.2)
        return not self._alive(managed)

    @staticmethod
    def _kill_tree(pid: int) -> None:
        try:
            parent = psutil.Process(pid)
            for process in [*parent.children(recursive=True), parent]:
                try:
                    process.kill()
                except psutil.Error:
                    pass
        except psutil.Error:
            pass

    # Watching ------------------------------------------------------------------------

    def drain(self, server: ServerConfig) -> None:
        managed = self.managed.get(server.name)
        if managed is not None:
            self.drain_managed(managed)

    def drain_managed(self, managed: Managed) -> None:
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

    def last_console_lines(self, server: ServerConfig, count: int = 200) -> list[str]:
        path = self.console_path(server)
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(max(0, size - 256 * 1024))
                lines = file.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [l.rstrip() for l in lines if l.strip()][-count:]

    async def stop_all(self) -> list[str]:
        if not self.managed:
            self.adopt()  # no supervisor running: the servers a previous one left running
        stopped = list(self.managed)
        for name in stopped:
            await self.stop(self.managed[name].server)
        return stopped

    async def close(self, stop_servers: bool | None = None) -> None:
        if self.stop_on_exit if stop_servers is None else stop_servers:
            await self.stop_all()
        else:
            self._save()
            if self.managed:
                launcher = os.environ.get("RECLAIMER_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")
                log.info("Left running, so players weren't disturbed: %s. Start the supervisor again to pick "
                         "them up, or stop them with: %s stop", ", ".join(self.managed), launcher)


BACKEND_TYPES: dict[str, type[Backend]] = {
    "attach": Backend, "command": CommandBackend, "docker": DockerBackend, "process": ProcessBackend,
}


def make_backend(settings: Settings, bus: EventBus, states: dict[str, ServerState]) -> Backend:
    try:
        return BACKEND_TYPES[settings.backend](settings, bus, states)
    except KeyError:
        raise ConfigError(f"backend: '{settings.backend}' is not one of {', '.join(BACKEND_TYPES)}") from None


# Keepalive ---------------------------------------------------------------------------------

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
        for name, state in list(self.states.items()):
            if name in self.lifecycle.busy or not self.lifecycle.is_up(name):
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
