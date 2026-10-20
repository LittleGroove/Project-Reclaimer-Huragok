"""How servers run on this machine: the backends, and the keepalive.

A backend starts and stops server processes; it decides nothing. The decisions
(when to start, warn, restart, give up) are the per-server managers' (manager.py).

  process  one `dedicated` per server, run by the supervisor, on Windows (the exe
           itself) or Linux (through `wine`, or whatever [process] command says)
  docker   one container per server, through `docker compose`
  command  commands the host gives (systemd units, scripts...), Windows or Linux
  attach   the host's own setup runs them; Huragok watches and moderates only

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
from .priority import PriorityKeeper
from .state import ServerState

if TYPE_CHECKING:
    from .manager import Lifecycle

log = logging.getLogger(__name__)


class LifecycleUnavailable(Exception):
    """This backend can't do that (e.g. attach mode can't restart)."""


class LifecycleError(Exception):
    """Starting or stopping failed."""


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _on_windows() -> bool:
    """The server program runs natively here (on Linux it runs through wine)."""
    return os.name == "nt"


def _rcon_address(server: ServerConfig) -> tuple[str, int]:
    endpoint = server.rcon_endpoint()
    return (endpoint.host, endpoint.port) if endpoint else ("127.0.0.1", server.rcon_port)


def listening_ports() -> frozenset[int] | None:
    """The TCP ports something on this machine listens on, from the system's own table of sockets: about a
    millisecond for every server at once. None when the system won't say (macOS without root)."""
    try:
        return frozenset(c.laddr.port for c in psutil.net_connections("tcp")
                         if c.status == psutil.CONN_LISTEN and c.laddr)
    except (psutil.Error, OSError):
        return None


def _connects(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_in_use(server: ServerConfig, timeout: float = 0.5) -> bool:
    """Something listens on the server's RCON port (the same number as its game port).

    Blocking, for the command line. The system's socket table answers at once; a connect is only tried when the
    table can't say (unreadable, or the server's RCON is on another machine), and a connect to a closed port takes
    Windows two seconds to refuse. The managers use PortWatch, which never waits."""
    host, port = _rcon_address(server)
    if not port:
        return False
    ports = listening_ports()
    if ports is not None and (port in ports or host in LOOPBACK):
        return port in ports
    return _connects(host, port, timeout)


class PortWatch:
    """Whether a stopped server's RCON port is taken (by a copy started some other way), for the managers' every
    tick, without ever holding the event loop.

    Asked by connecting, each stopped server held the loop for the whole time Windows takes to refuse a connect to
    a closed port: three stopped servers stalled every other server's RCON, the parts and the window by 1.5 s a
    second. The system's table of listening sockets answers for all of them at once in a millisecond, looked up at
    most once a second. Where it can't be read, a connect runs in a thread and its answer is kept for a while; until
    the first one is in, the answer is None (not known yet)."""

    LOOK_EVERY = 1.0
    PROBE_EVERY = 15.0

    def __init__(self) -> None:
        self._ports: frozenset[int] | None = None
        self._looked = float("-inf")
        self._probed: dict[tuple[str, int], tuple[float, bool]] = {}  # (host, port) -> (when, answer)
        self._probing: set[tuple[str, int]] = set()

    def in_use(self, server: ServerConfig) -> bool | None:
        host, port = _rcon_address(server)
        if not port:
            return False
        now = time.monotonic()
        if now - self._looked >= self.LOOK_EVERY:
            self._looked = now
            self._ports = listening_ports()
        if self._ports is not None:
            return port in self._ports  # the process backend's servers run here, whatever address they listen on
        key = (host, port)
        known = self._probed.get(key)
        if (known is None or now - known[0] >= self.PROBE_EVERY) and key not in self._probing:
            self._probing.add(key)
            asyncio.ensure_future(self._probe(key))
        return None if known is None else known[1]

    async def _probe(self, key: tuple[str, int]) -> None:
        try:
            answer = await asyncio.to_thread(_connects, key[0], key[1], 0.5)
        finally:
            self._probing.discard(key)
        self._probed[key] = (time.monotonic(), answer)


def _pid_alive(pid: int, create_time: float) -> bool:
    """That process (the same one: its start time matches, so a reused number doesn't count) is still running."""
    try:
        process = psutil.Process(pid)
        return abs(process.create_time() - create_time) < 1 and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _entry_alive(entry: dict[str, Any]) -> bool:
    try:
        return _pid_alive(int(entry["pid"]), float(entry["create_time"]))
    except (KeyError, TypeError, ValueError):
        return False


def kill_tree(pid: int) -> None:
    """End a process and everything it started (a shell's command, `dedicated`'s helpers)."""
    try:
        parent = psutil.Process(pid)
        for process in [*parent.children(recursive=True), parent]:
            try:
                process.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass


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
        return LifecycleUnavailable(f"backend = {self.name}: the Superintendent doesn't {what} the servers; "
                                    "the host's own setup does")

    async def start(self, server: ServerConfig) -> None:
        raise self._unavailable("start")

    async def stop(self, server: ServerConfig, timeout: float | None = None) -> None:
        raise self._unavailable("stop")

    async def restart(self, server: ServerConfig) -> None:
        await self.stop(server)
        await self.start(server)

    async def revive(self, server: ServerConfig) -> None:
        """Start a server again after it seemed to die or never came up. A backend that sees the process knows it's
        gone, so this is a start; one that only infers it (docker, command) changes that (see CommandBackend)."""
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

    def adopt_server(self, server: ServerConfig) -> bool:
        """One server found while running: pick it up if a previous supervisor left it running."""
        return False

    def forget(self, server: ServerConfig) -> None:
        """The server's process is gone (not stopped by us): drop what we kept about it."""

    def drain(self, server: ServerConfig) -> None:
        """Turn new console output into events."""

    def priority_text(self, server: ServerConfig) -> str:
        """The priority its processes run at, for the window ("" when the backend doesn't set one)."""
        return ""

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
            await asyncio.to_thread(kill_tree, process.pid)
            raise LifecycleError(f"{action} took longer than {self.timeout:.0f} s: {command}") from None
        except asyncio.CancelledError:  # a stop while it was starting: what it ran mustn't carry on by itself
            kill_tree(process.pid)
            asyncio.ensure_future(process.wait())  # collected once it's gone
            log.info("[%s] %s called off; its command was ended", server.name, action)
            raise
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

    async def revive(self, server: ServerConfig) -> None:
        """Without a process to watch, "dead" means RCON and status.json went quiet: it may only be hung, and a start
        command does nothing to a server still running (`docker compose up -d` on a running container). So it's
        restarted, or stopped then started; a stop that fails there is only logged, as it may be stopped already."""
        if self.commands["restart"]:
            await self._run("restart", server)
            self.bus.emit("server.starting", server.name)
            return
        if self.can_stop:
            try:
                await self.stop(server)
            except LifecycleError as error:
                log.warning("[%s] the stop before starting it again failed (it may be stopped already): %s",
                            server.name, error)
        await self.start(server)

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
    Its processes are kept at [process] priority (High unless set otherwise; priority.py).
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
        self.state_file = settings.records / "running-servers.json"
        self.managed: dict[str, Managed] = {}
        # Entries of running-servers.json for servers not looked after this run (a folder left out by a mistake in
        # its dedicated.toml, or by `servers = [...]`): kept as they were, so the server can be picked up again.
        self.kept: dict[str, dict[str, Any]] = {}
        self.breakaway = True  # False once Windows refused to start a server outside Huragok's job object
        self.priorities = PriorityKeeper()

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
        program = [str(exe)] if _on_windows() else ["wine", str(exe)]
        return [*program, "dedicated", "run", "--config", server.config_path.name]

    def problems(self, server: ServerConfig) -> list[str]:
        found = []
        if not self.command_template:
            if self.executable_for(server) is None:
                found.append(f"{self.executable} is not in {server.folder} or at {self.settings.home} "
                             "(set [process] command to run it another way)")
            if not _on_windows() and shutil.which("wine") is None:
                found.append("wine is not installed, and on Linux the server program runs through it "
                             "(or set [process] command)")
        elif "{executable}" in self.command_template[0]:  # the program itself, through whatever the words say
            if self.executable_for(server) is None:
                found.append(f"[process] command starts with {self.command_template[0]}, but {self.executable} is "
                             f"not in {server.folder} or at {self.settings.home}")
        elif shutil.which(self.command_template[0]) is None and not Path(self.command_template[0]).is_file():
            found.append(f"[process] command: {self.command_template[0]} was not found")
        if server.auto_update and not self.command_template:
            found.extend(self.auto_update_problems(server))
        return found

    def auto_update_problems(self, server: ServerConfig) -> list[str]:
        """auto_update replaces the program file a server runs: that file must be its alone, and writable."""
        exe = self.executable_for(server)
        if exe is None:
            return []
        sharing = []
        for other in self.settings.servers:
            theirs = self.executable_for(other) if other is not server else None
            if theirs is not None and theirs.resolve() == exe.resolve():
                sharing.append(other.name)
        if sharing:
            return [f"auto_update is on, but it runs {exe} together with {', '.join(sharing)}: two servers must "
                    "never update one program file. Give it a copy in its own folder (a hard link is fine)"]
        if not os.access(exe.parent, os.W_OK):
            return [f"auto_update is on, but {exe.parent} can't be written to, so its updates will fail"]
        return []

    # Processes ---------------------------------------------------------------------

    @staticmethod
    def _alive(managed: Managed) -> bool:
        if managed.popen is not None:
            if managed.popen.poll() is None:
                return True
            managed.exit_code = managed.popen.returncode
            return False
        return _pid_alive(managed.pid, managed.create_time)

    def _save(self) -> None:
        """running-servers.json, written whole (a temporary file swapped in): Huragok killed mid-write must not leave
        a file the next start can't read, or it would adopt nothing and every server would look started elsewhere."""
        self.kept = {name: entry for name, entry in self.kept.items()
                     if name not in self.managed and _entry_alive(entry)}
        data: dict[str, Any] = dict(self.kept)
        data.update({name: {"pid": m.pid, "create_time": m.create_time, "console": str(m.console_path)}
                     for name, m in self.managed.items()})
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _saved(self) -> dict[str, dict[str, Any]]:
        """running-servers.json's entries that make sense; a damaged file reads as empty, with a warning."""
        try:
            text = self.state_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as error:
            log.warning("couldn't read %s: %s; servers still running from before aren't picked up",
                        self.state_file, error)
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            log.warning("%s is damaged, so servers still running from before aren't picked up: they show as "
                        "started some other way. Stop them (Task Manager) and start them here", self.state_file)
            return {}
        saved = {}
        for name, entry in data.items():
            try:
                saved[str(name)] = {"pid": int(entry["pid"]), "create_time": float(entry["create_time"]),
                                    "console": str(entry["console"])}
            except (TypeError, KeyError, ValueError):
                log.warning("%s: the entry for %s is damaged; it's left out", self.state_file.name, name)
        return saved

    def adopt(self) -> None:
        """Pick up servers we started before our own restart. Entries for servers not looked after this run are
        kept (adopt_server picks one up if its folder is found again while running)."""
        saved = self._saved()
        if not saved:
            return
        names = {server.name for server in self.settings.servers}
        for server in self.settings.servers:
            self._adopt_entry(server, saved.get(server.name))
        for name, entry in saved.items():
            if name not in names and name not in self.managed:
                self.kept[name] = entry
        self._save()

    def adopt_server(self, server: ServerConfig) -> bool:
        """A server found while running (its folder fixed, or added): pick it up if we started it before."""
        if server.name in self.managed:
            return True
        entry = self.kept.pop(server.name, None) or self._saved().get(server.name)
        adopted = self._adopt_entry(server, entry)
        self._save()
        return adopted

    def _adopt_entry(self, server: ServerConfig, entry: dict[str, Any] | None) -> bool:
        if not entry or server.name in self.managed:
            return False
        path = Path(entry["console"])
        tail = FileTail(path, from_start=False)
        tail.read_lines()
        managed = Managed(server, int(entry["pid"]), float(entry["create_time"]), tail, path)
        if not self._alive(managed):
            return False
        self.managed[server.name] = managed
        if server.name in self.states:
            self.states[server.name].pid = None
        log.info("[%s] found our `dedicated` still running (process %s)", server.name, managed.pid)
        return True

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
        env.pop("RECLAIMER_DEDICATED_AUTO_UPDATE", None)  # the server's own auto_update (dedicated.toml) decides
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        try:
            with console_path.open("ab") as output:
                output.write(f"──── {stamp} UTC · started by the Superintendent · {' '.join(command)} ────\n".encode("utf-8"))
                popen = self._popen(server, command, env, output)
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

    def _popen(self, server: ServerConfig, command: list[str], env: dict[str, str],
               output: Any) -> subprocess.Popen[bytes]:
        """Start the process so it outlives us: its own session (Linux), or its own process group with no window
        (Windows), and outside Huragok's job object when Windows allows it. Run from Task Scheduler, Huragok is in
        the task's job, and ending the job (its time limit is three days by default) would end every server too."""
        options: dict[str, Any] = dict(cwd=server.folder, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                       stderr=subprocess.STDOUT)
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, **options)
        from . import winproc

        if self.breakaway:
            try:
                popen = subprocess.Popen(command, creationflags=winproc.creation_flags(breakaway=True), **options)
            except OSError as error:
                if getattr(error, "winerror", None) != winproc.ERROR_ACCESS_DENIED:
                    raise
                self.breakaway = False
                log.warning("[%s] Windows won't start servers outside Huragok's job object (the job doesn't allow "
                            "it), so they're started inside it: if whatever started Huragok ends its job (a Task "
                            "Scheduler task's time limit), the servers end with it", server.name)
            else:
                log.debug("[%s] started outside Huragok's job object", server.name)
                return popen
        return subprocess.Popen(command, creationflags=winproc.creation_flags(), **options)

    def _signal_stop(self, managed: Managed) -> bool:
        """Ask `dedicated` to stop cleanly. True when the request reached it; False (and the caller ends the
        process) when it couldn't be sent, including the Windows helper timing out or failing to run."""
        if os.name == "nt":
            from . import winproc

            try:
                return winproc.ctrl_break(managed.pid)
            except Exception as error:  # noqa: BLE001 - whatever went wrong, the process must still be ended
                log.warning("[%s] couldn't send Ctrl+Break to process %s (%s: %s)", managed.server.name, managed.pid,
                            type(error).__name__, error)
                return False
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
        """Stop it cleanly, or end it. Raises LifecycleError, keeping it as ours, when the process outlives that."""
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
                if not await self._wait_exit(managed, 10):
                    managed.stopping = False
                    self.drain(server)
                    raise LifecycleError(f"couldn't end process {managed.pid}: it's still running after being "
                                         "told to stop and then ended")
        self.drain(server)
        self.managed.pop(server.name, None)
        self.priorities.forget(server.name)
        self.states[server.name].pid = None
        self._save()
        self.bus.emit("server.stopped", server.name, expected=True, exit_code=managed.exit_code)

    def forget(self, server: ServerConfig) -> None:
        managed = self.managed.pop(server.name, None)
        if managed is None:
            return
        self.drain_managed(managed)
        self.priorities.forget(server.name)
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

    _kill_tree = staticmethod(kill_tree)

    # Watching ------------------------------------------------------------------------

    def drain(self, server: ServerConfig) -> None:
        managed = self.managed.get(server.name)
        if managed is not None:
            self.drain_managed(managed)
            if not managed.stopping:
                self.priorities.keep(server.name, managed.pid, self.settings.priority_for(server))

    def priority_text(self, server: ServerConfig) -> str:
        return self.priorities.text(server.name) if server.name in self.managed else ""

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
                launcher = os.environ.get("HURAGOK_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")
                log.info("Left running, so players weren't disturbed: %s. Start Huragok again to pick "
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

    Each run of a server is judged from when it came up: the files and replies of
    the run before don't count against it, and one that freezes as it starts is
    restarted hang_seconds later (the live server froze twice in a row that way:
    76 minutes, then 2 s into the next start). It acts once per run, again after
    RETRY if that run is still hung; the per-hour restart budget stops a loop.
    """

    RETRY = 600.0  # the same run still hung this long after its restart was asked for: ask again

    def __init__(self, settings: KeepaliveSettings, lifecycle: Lifecycle, states: dict[str, ServerState],
                 bus: EventBus) -> None:
        self.settings = settings
        self.lifecycle = lifecycle
        self.states = states
        self.bus = bus
        self.hung: set[str] = set()
        self._up_since: dict[str, float] = {}  # monotonic time this run was first seen up
        self._acted: dict[str, tuple[float, float]] = {}  # (the run's up_since, when it acted)

    def is_hung(self, state: ServerState, up_for: float | None = None) -> bool:
        age = state.status_age
        quiet = None if state.last_rcon_ok is None else time.monotonic() - state.last_rcon_ok
        if up_for is not None:  # nothing from before this run counts
            answered = quiet is not None and quiet < up_for  # RCON answered during this run
            age = None if age is None else min(age, up_for)
            quiet = up_for if quiet is None else min(quiet, up_for)
            if age is None and answered:
                # No status.json yet (a new server's first run): one that freezes before writing it would never
                # count as hung. Its console answered this run, so the run's age stands in for the file's. (A
                # console that never answered gives no signal at all: that alone mustn't restart it in a loop.)
                age = up_for
        limit = self.settings.hang_seconds
        return age is not None and age > limit and (quiet is None or quiet > limit)

    def check(self) -> None:
        now = time.monotonic()
        for name, state in list(self.states.items()):
            if name in self.lifecycle.busy or not self.lifecycle.is_up(name):
                self._up_since.pop(name, None)
                continue
            since = self._up_since.setdefault(name, now)
            if self.is_hung(state, now - since):
                if name not in self.hung:
                    self.hung.add(name)
                    log.error("[%s] not responding: status.json last written %.0f s ago, RCON quiet",
                              name, state.status_age or 0)
                    self.bus.emit("server.hung", name, status_age=state.status_age)
                self._maybe_act(name, since)
            elif name in self.hung:
                self.hung.discard(name)
                log.info("[%s] responding again", name)
                self.bus.emit("server.recovered", name)

    def _maybe_act(self, name: str, since: float) -> None:
        if not (self.settings.act and self.lifecycle.can_control):
            return
        run, acted = self._acted.get(name, (None, 0.0))
        if run == since and time.monotonic() - acted < self.RETRY:
            return
        self._acted[name] = (since, time.monotonic())
        asyncio.ensure_future(self._restart(name))

    async def _restart(self, name: str) -> None:
        try:
            await self.lifecycle.restart(name, when="now", reason="not responding", countdown=0)
        except (LifecycleUnavailable, LifecycleError) as error:
            log.warning("[%s] can't restart it: %s", name, error)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.check_seconds)
            self.check()
