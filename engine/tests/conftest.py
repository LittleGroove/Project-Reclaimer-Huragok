"""What every test shares: folders as Huragok finds them, free ports, and the guards each test runs inside.

The PC the tests run on may run real Project Reclaimer servers (this one does: UDP/TCP 49175-49185, RCON on
127.0.0.1:49176, where five refused passwords lock every tool out). So a test never gets a port from 49170 to
49199, never connects to one, never reaches past this machine (GitHub, Steam), never reads this PC's own
SteamCMD or MCC, and never leaves a fake server running: each fake ends itself once this test session's
process is gone (HURAGOK_TEST_PARENT), and the servers a test's running-servers.json names are ended after it.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_rcon import PASSWORD  # noqa: E402

# Real servers' ports (49175-49185 on this PC) and the ones next to them, which Windows also hands out as
# ephemeral ports (from 49152 up): free_port() never returns one, and the network guard refuses them.
REAL_SERVER_PORTS = range(49170, 49200)
# The fakes a test starts watch this process and end themselves when it's gone (fake_dedicated.parent_gone).
TEST_PARENT = "HURAGOK_TEST_PARENT"
os.environ[TEST_PARENT] = str(os.getpid())

# Player IDs for tests (not real players).
OWNER = "a" * 64
MOD = "b" * 64
ALICE = "c" * 64
BRAVO = "d" * 64
ADMIN = "e" * 64


def init_files(folder: Path) -> None:
    """What `dedicated init` makes beside dedicated.toml, and the two game files a start needs: a server the process
    backend runs has them, or Huragok waits for them before it starts it (repair.py)."""
    for file in ("game/halo3/halo3.dll", "game/halo3/maps/shared.map"):
        (folder / file).parent.mkdir(parents=True, exist_ok=True)
        (folder / file).write_bytes(b"")
    for sub in ("Maps", "Game Modes", "Images", "Mods"):
        (folder / "content" / sub).mkdir(parents=True, exist_ok=True)
    (folder / "bans.json").write_text(json.dumps({"players": [], "ips": []}), encoding="utf-8")


def write_server_folder(folder: Path, port: int, *, name: str = "LG Test Server", host_key: str | None = None,
                        status_age: float | None = None, extra_server: str = "", exe: bool = False,
                        rcon_password: str | None = PASSWORD) -> Path:
    """A server folder like `dedicated init` makes, with one server whose RCON is on `port`."""
    folder.mkdir(parents=True, exist_ok=True)
    rcon = f'[rcon]\npassword = "{rcon_password}"\n' if rcon_password else ""
    (folder / "dedicated.toml").write_text(f"""
data = "data"
admins = ["{ADMIN}"]
{rcon}
[[server]]
name = "{name}"
port = {port}
{extra_server}
""", encoding="utf-8")
    if exe:
        (folder / "project-reclaimer.exe").write_bytes(b"")
        init_files(folder)
    data = folder / "data" / name.lower().replace(" ", "-")
    data.mkdir(parents=True, exist_ok=True)
    if host_key:
        (data / "rcon-host.key").write_text(host_key, encoding="utf-8")
    if status_age is not None:
        (data / "status.json").write_text(json.dumps({"name": name, "players": [],
                                                      "updated": int(time.time() - status_age)}), encoding="utf-8")
    return data


def write_settings(folder: Path, server_folder: Path | None = None, body: str = "", commands: str | None = None,
                   backend: str | None = "attach") -> Path:
    """settings.toml (no database or log file), and commands.toml when `commands` is given.

    server_folder names one server folder (the 0.1 layout); None means SERVERS/ next to the
    settings. The backend is attach unless the body sets one, so tests act on nothing real.
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "settings.toml"
    lines = ['database = ""', 'log_file = ""']
    if server_folder is not None:
        lines.append(f'server_folder = "{server_folder.as_posix()}"')
    if backend and "backend" not in body:
        lines.append(f'backend = "{backend}"')
    path.write_text("\n".join(lines) + "\n" + body + "\n", encoding="utf-8")
    if commands is not None:
        (folder / "commands.toml").write_text(commands, encoding="utf-8")
    return path


def write_root(root: Path, servers: list[tuple[str, int]], body: str = "", commands: str | None = None,
               backend: str | None = "attach", exe: bool = False) -> Path:
    """The 0.2 layout: settings at the root, one folder per server in SERVERS/."""
    for name, port in servers:
        write_server_folder(root / "SERVERS" / name, port, name=name, exe=exe)
    return write_settings(root, None, body, commands, backend)


def staff(extra: str = "", cooldown: float = 0) -> str:
    """commands.toml with OWNER and MOD listed, every command at its default role."""
    return f"""
cooldown_seconds = {cooldown}
[owner]
players = ["{OWNER}"]
[moderator]
players = ["{MOD}"]
{extra}"""


def free_port() -> int:
    """A TCP port nothing listens on right now, never one of REAL_SERVER_PORTS."""
    return free_ports(1)[0]


def _ports_in_use() -> set[int]:
    """Every local port a socket holds now, TCP or UDP, on any address. Windows lets 127.0.0.1:N be bound while
    another program listens on 0.0.0.0:N, and a server uses N for UDP too: binding alone doesn't show those."""
    try:
        import psutil

        return {c.laddr.port for c in psutil.net_connections("inet") if c.laddr}
    except Exception:  # noqa: BLE001 - the system won't say: the bind below is all there is
        return set()


def free_ports(count: int) -> list[int]:
    """Several ports nothing uses right now (TCP for RCON, UDP for the game), all different, none of
    REAL_SERVER_PORTS.

    The system picks them; one it picks that's no good is held while asking again, so the next pick is another
    port."""
    in_use = _ports_in_use()
    held: list[socket.socket] = []
    ports: list[int] = []
    try:
        while len(ports) < count:
            if len(held) >= count + len(REAL_SERVER_PORTS):
                raise RuntimeError(f"the system keeps handing out ports from {REAL_SERVER_PORTS.start} to "
                                   f"{REAL_SERVER_PORTS.stop - 1}, or ports in use: "
                                   f"{[s.getsockname()[1] for s in held]}")
            sock = socket.socket()
            held.append(sock)
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            if port not in REAL_SERVER_PORTS and port not in in_use:
                ports.append(port)
        return ports
    finally:
        for sock in held:
            sock.close()


async def wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for a condition")


# The guards ---------------------------------------------------------------------------------------------------


def _local(host: object) -> bool:
    """This machine: localhost, a loopback or "any" address, or no host at all (a server on every address)."""
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    text = str(host).strip("[]").lower()
    if text in ("", "localhost", "localhost."):
        return True
    try:
        address = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _in_socketpair() -> bool:
    """A connect made by socket.socketpair itself. On Windows it joins its two sockets through a listener of its own
    on a free loopback port (every asyncio loop makes one), and that port can be one of the real servers' range,
    which sits in Windows' dynamic ports: the guard refused it and failed whatever test made the loop (CI, 0.4.x)."""
    frame = sys._getframe(2)
    for _ in range(6):
        if frame is None:
            return False
        if frame.f_code.co_name in ("_fallback_socketpair", "socketpair") and frame.f_globals.get("__name__") == "socket":
            return True
        frame = frame.f_back
    return False


class NetworkGuard:
    """Refuses a connection or a name lookup that would leave this machine, and a connection to a real server's
    port; each refusal is remembered so the test fails even when the code under test took it in its stride."""

    def __init__(self) -> None:
        self.refused: list[str] = []

    def _refuse(self, what: str) -> str:
        self.refused.append(what)
        return f"refused by the tests' network guard: {what} (tests stay on this machine, off 49170-49199)"

    def check_connect(self, address: object) -> None:
        if not isinstance(address, tuple) or len(address) < 2:
            return  # a Unix socket's path
        host, port = address[0], address[1]
        if not _local(host) or (isinstance(port, int) and port in REAL_SERVER_PORTS):
            raise ConnectionRefusedError(errno.ECONNREFUSED, self._refuse(f"a connection to {host}:{port}"))

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if not event.startswith("socket."):
            return
        if event == "socket.connect" and not _in_socketpair():  # connect and connect_ex: blocking code, and
            self.check_connect(args[1])                          # asyncio's loop on Linux
        elif event == "socket.getaddrinfo" and not _local(args[0]):  # every name looked up: urllib, asyncio
            raise socket.gaierror(socket.EAI_NONAME, self._refuse(f"looking up {args[0]}"))


NETWORK = NetworkGuard()
sys.addaudithook(NETWORK.audit)
if os.name == "nt":  # asyncio's Windows loop connects through ConnectEx, which raises no audit event
    from asyncio import windows_events

    _iocp_connect = windows_events.IocpProactor.connect

    def _guarded_iocp_connect(self: Any, conn: socket.socket, address: Any) -> Any:
        NETWORK.check_connect(address)
        return _iocp_connect(self, conn, address)

    windows_events.IocpProactor.connect = _guarded_iocp_connect  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def stays_on_this_machine() -> Iterator[None]:
    """No test reaches GitHub, Steam or anything else past this machine, nor a real server's port (the guard
    refuses at once; this fails the test that tried)."""
    NETWORK.refused.clear()
    yield
    if NETWORK.refused:
        tried = sorted(set(NETWORK.refused))
        NETWORK.refused.clear()
        pytest.fail("the test tried to leave this machine or reach a real server's port: " + "; ".join(tried),
                    pytrace=False)


@pytest.fixture(autouse=True)
def no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never ask GitHub for releases; test_updates.py hands out its own."""
    from huragok import updates

    monkeypatch.setattr(updates, "latest_release", lambda *args, **kwargs: None)


@pytest.fixture(autouse=True)
def not_this_pcs_installs(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Steam, MCC and SteamCMD are found only where a test puts them, never this PC's own (C:\\steamcmd's
    config.vdf names real accounts; Steam's own records name the real MCC). The lookups themselves are tested
    against folders made in the test (test_content.py). New servers' ports are picked past the real servers'."""
    from huragok import contentimport, newserver, steamsetup

    nowhere = tmp_path_factory.getbasetemp() / "nothing-is-installed-here"
    monkeypatch.setattr(contentimport, "_steam_roots", lambda: [])
    monkeypatch.setattr(steamsetup, "STANDARD_WINDOWS", nowhere / "steamcmd")
    monkeypatch.setattr(steamsetup, "depot_folders", lambda executable, depot: [
        executable.parent / "steamapps" / "content" / f"app_{steamsetup.MCC_APP}" / f"depot_{depot}"])
    real_candidates = steamsetup.candidates
    kept = ("[setup] steamcmd", "the root's steamcmd folder")  # the standard folder, ~/steamcmd and PATH are this PC's
    monkeypatch.setattr(steamsetup, "candidates", lambda home, hint="": [
        (path, where) for path, where in real_candidates(home, hint) if where in kept])
    real_port_free = newserver.port_free
    monkeypatch.setattr(newserver, "port_free", lambda port: port not in REAL_SERVER_PORTS and real_port_free(port))


def end_servers_recorded_in(folder: Path) -> list[int]:
    """End each process a running-servers.json under `folder` names that this test session started: still the
    same process (its start time) and this session's HURAGOK_TEST_PARENT in its environment, with whatever it
    started. Nothing else is touched (other game servers run on this PC). The process numbers ended."""
    import psutil

    ended: list[int] = []
    for path in folder.rglob("running-servers.json"):
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in entries.values() if isinstance(entries, dict) else []:
            pid = entry.get("pid") if isinstance(entry, dict) else None
            if not isinstance(pid, int) or isinstance(pid, bool) or pid == os.getpid():
                continue
            try:
                process = psutil.Process(pid)
                if abs(process.create_time() - float(entry.get("create_time"))) > 1.0:
                    continue  # the number belongs to another process now
                if process.environ().get(TEST_PARENT) != str(os.getpid()):
                    continue  # not started by this test session
                tree = [*process.children(recursive=True), process]
            except (psutil.Error, OSError, TypeError, ValueError):
                continue
            for member in tree:
                try:
                    member.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(tree, timeout=5)
            ended.append(pid)
    return ended


@pytest.fixture(autouse=True)
def no_server_left_running(request: pytest.FixtureRequest) -> Iterator[None]:
    """A test that fails half-way leaves its servers running (closing Huragok leaves them up, on purpose):
    whatever its running-servers.json still names is ended after it."""
    folder = request.getfixturevalue("tmp_path") if "tmp_path" in request.fixturenames else None
    yield
    if folder is not None:
        end_servers_recorded_in(folder)


@pytest.fixture(autouse=True, scope="session")
def no_server_left_running_at_the_end(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """The same for folders made outside tmp_path, once every test has run."""
    yield
    end_servers_recorded_in(tmp_path_factory.getbasetemp())


class _AsyncioErrors(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        error = record.exc_info[1] if record.exc_info else None
        self.lines.append(record.getMessage().splitlines()[0] + (f" ({error!r})" if error else ""))


@pytest.fixture(autouse=True)
def no_asyncio_errors(request: pytest.FixtureRequest) -> Iterator[None]:
    """asyncio logs a task that failed with nobody looking ("Task exception was never retrieved") or a callback
    that raised, and carries on: the test fails instead. A test that provokes one on purpose is marked
    @pytest.mark.asyncio_errors."""
    handler = _AsyncioErrors()
    logger = logging.getLogger("asyncio")
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
    if handler.lines and request.node.get_closest_marker("asyncio_errors") is None:
        pytest.fail("asyncio logged an error during the test: " + "; ".join(handler.lines), pytrace=False)


@pytest.fixture
def run() -> Callable[..., Any]:
    def runner(coroutine: Any) -> Any:
        return asyncio.run(coroutine)

    return runner


class ProgramLifecycle:
    """What the program updater uses of the manager's Lifecycle, recorded (no servers, no clock)."""

    def __init__(self, up: tuple[str, ...] = ("Alpha", "Beta")) -> None:
        from types import SimpleNamespace

        self._plan = SimpleNamespace
        self.up = set(up)
        self.restarts: list[tuple[str, str, str]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.flags: dict[tuple[str, str], str] = {}
        self.planned: dict[str, Any] = {}
        self.cancelled: list[str] = []

    def is_up(self, name: str) -> bool:
        return name in self.up

    async def restart(self, server: str, *, when: str, reason: str, by: str | None = None,
                      countdown: float | None = None) -> Any:
        self.restarts.append((server, when, reason))
        self.planned[server] = self._plan(when=when, reason=reason)
        return self.planned[server]

    def add_job(self, server: str, job: Any) -> None:
        self.jobs.setdefault(server, {})[job.key] = job

    def drop_job(self, server: str, key: str) -> bool:
        return self.jobs.get(server, {}).pop(key, None) is not None

    def set_flag(self, server: str, who: str, text: str | None) -> None:
        if text:
            self.flags[(server, who)] = text
        else:
            self.flags.pop((server, who), None)

    def cancel(self, server: str) -> bool:
        self.cancelled.append(server)
        return self.planned.pop(server, None) is not None
