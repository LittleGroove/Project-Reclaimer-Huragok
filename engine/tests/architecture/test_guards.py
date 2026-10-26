"""The guards every test runs inside (conftest.py): ports away from the real servers', nothing past this machine,
none of this PC's own installs, no fake server left running, and no error that asyncio or a destructor swallows."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from conftest import NETWORK, TEST_PARENT, end_servers_recorded_in, free_port, free_ports, write_server_folder
from testkit import FAKE, TESTS

HERE = TESTS


def test_free_ports_are_never_the_real_servers_nor_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows hands out ports from 49152 up, so the system's pick could be a real server's RCON port; and it lets
    127.0.0.1:N be bound while something listens on 0.0.0.0:N, or holds UDP N."""
    import conftest

    handed = iter([49176, 49170, 49199, 40001, 49185, 40002, 40003])
    monkeypatch.setattr(conftest, "_ports_in_use", lambda: {40002})

    class Socket:
        def bind(self, address: tuple[str, int]) -> None:
            self.port = next(handed)

        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", self.port

        def close(self) -> None:
            pass

    monkeypatch.setattr(socket, "socket", Socket)
    assert free_ports(2) == [40001, 40003]
    handed = iter([49180, 40002, 40004])
    assert free_port() == 40004
    handed = iter([49176] * 100)
    with pytest.raises(RuntimeError, match="keeps handing out ports from 49170 to 49199"):
        free_port()


def test_nothing_past_this_machine_nor_a_real_servers_port_is_reached(run) -> None:
    from huragok.updates import engine as updates
    from huragok.core.web import UpdateError

    try:
        with pytest.raises(socket.gaierror, match="network guard"):
            socket.getaddrinfo("api.github.com", 443)
        with pytest.raises(OSError, match="network guard"):
            socket.create_connection(("192.0.2.1", 80), timeout=1)
        with pytest.raises(ConnectionRefusedError, match="network guard"):
            socket.create_connection(("127.0.0.1", 49199), timeout=1)
        with pytest.raises(UpdateError, match="couldn't reach GitHub"):
            updates.fetch("https://api.github.com/repos/x/y/releases/latest", 2)

        async def through_asyncio() -> bytes:
            with pytest.raises(ConnectionRefusedError, match="network guard"):
                await asyncio.open_connection("127.0.0.1", 49198)  # the RCON a regression would reach
            with pytest.raises(OSError, match="network guard"):
                await asyncio.open_connection("steamcommunity.com", 443)

            async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                writer.write(b"here")
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(answer, "127.0.0.1", free_port())  # this machine: fine
            async with server:
                port = server.sockets[0].getsockname()[1]
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                data = await reader.read()
                writer.close()
                return data

        assert run(through_asyncio()) == b"here"
        refused = " ".join(NETWORK.refused)
        assert "looking up api.github.com" in refused and "127.0.0.1:49199" in refused
        assert "127.0.0.1:49198" in refused and "looking up steamcommunity.com" in refused
    finally:
        NETWORK.refused.clear()  # refused on purpose: this test mustn't fail for them


def test_socketpairs_own_connection_is_let_through() -> None:
    """socket.socketpair joins its two sockets over loopback on whatever port is free, the real servers' range
    included (it's in Windows' dynamic ports, and every asyncio loop on Windows makes a pair): that isn't a
    connection to a server. The same port from anything else is still refused."""
    namespace: dict[str, object] = {"__name__": "socket", "NETWORK": NETWORK}
    exec(compile("def _fallback_socketpair():\n    NETWORK.audit('socket.connect', (None, ('127.0.0.1', 49178)))\n",
                 "socket", "exec"), namespace)
    try:
        namespace["_fallback_socketpair"]()  # type: ignore[operator]
        assert NETWORK.refused == []
        with pytest.raises(ConnectionRefusedError, match="network guard"):
            NETWORK.audit("socket.connect", (None, ("127.0.0.1", 49178)))
        a, b = socket.socketpair()  # the real thing, on whatever port
        a.close()
        b.close()
    finally:
        NETWORK.refused.clear()


def test_this_pcs_own_steamcmd_mcc_and_ports_are_left_alone(tmp_path: Path) -> None:
    from huragok.content import mccimport as contentimport, newserver
    from huragok import steam as steamsetup

    assert steamsetup.find_steamcmd(tmp_path) == (None, "")  # not C:\steamcmd, ~/steamcmd or PATH
    assert steamsetup.find_mcc() is None  # no drive walked, no Steam library read
    root = tmp_path / "steamcmd"
    root.mkdir()
    (root / steamsetup.executable_name()).write_bytes(b"")
    assert steamsetup.find_steamcmd(tmp_path)[0] == root / steamsetup.executable_name()  # put there by the test
    assert not any(newserver.port_free(port) for port in (49175, 49176, 49185))  # never even probed


def _sleeper(**env: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            env={**{k: v for k, v in os.environ.items() if k != TEST_PARENT}, **env})


@pytest.mark.slow
def test_a_fake_server_ends_itself_when_its_test_session_is_gone(tmp_path: Path) -> None:
    """A test that failed half-way left fakes running for as long as nobody noticed (they stay up on purpose
    when Huragok closes). Here a stand-in session process is ended under a running fake."""
    session = _sleeper()
    folder = tmp_path / "server"
    data = write_server_folder(folder, free_port())
    env = {**os.environ, TEST_PARENT: str(session.pid), "RECLAIMER_DEDICATED_ONLY": "LG Test Server"}
    fake = subprocess.Popen([sys.executable, str(FAKE), "dedicated", "run", "--config", "dedicated.toml"],
                            cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 30
        while not (data / "rcon-host.key").exists() and fake.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1.5)
        assert fake.poll() is None, fake.communicate()[0]  # its session is there: it runs on
        session.kill()
        session.wait()
        out, _ = fake.communicate(timeout=15)
        assert fake.returncode == 0 and "the test session that started this fake is gone" in out, out
        assert "All servers stopped." in out
    finally:
        for process in (session, fake):
            if process.poll() is None:
                process.kill()
                process.wait()


def test_after_a_test_the_servers_it_left_running_are_ended_and_nothing_else(tmp_path: Path) -> None:
    ours = _sleeper(**{TEST_PARENT: str(os.getpid())})
    theirs = _sleeper()  # not started by this test session (as other game servers on this PC aren't)
    reused = _sleeper(**{TEST_PARENT: str(os.getpid())})  # its number recorded with another start time
    try:
        records = tmp_path / "root" / "records"
        records.mkdir(parents=True)
        entries = {name: {"pid": process.pid, "create_time": psutil.Process(process.pid).create_time() + shift}
                   for name, process, shift in (("Ours", ours, 0), ("Theirs", theirs, 0), ("Reused", reused, 100))}
        (records / "running-servers.json").write_text(json.dumps(entries), encoding="utf-8")
        assert end_servers_recorded_in(tmp_path) == [ours.pid]
        assert ours.wait(10) is not None
        assert theirs.poll() is None and reused.poll() is None
    finally:
        for process in (ours, theirs, reused):
            if process.poll() is None:
                process.kill()
            process.wait()


INNER = '''
import asyncio
import urllib.request

import pytest


async def lost() -> None:
    raise ValueError("nobody looks at this")


async def leave_a_failed_task() -> None:
    asyncio.ensure_future(lost())
    await asyncio.sleep(0.1)


def test_a_task_that_failed_with_nobody_looking() -> None:
    asyncio.run(leave_a_failed_task())


@pytest.mark.asyncio_errors
def test_the_same_on_purpose() -> None:
    asyncio.run(leave_a_failed_task())


def test_a_coroutine_never_awaited() -> None:
    async def nothing() -> None:
        pass

    nothing()


def test_github_reached_quietly() -> None:
    try:
        urllib.request.urlopen("https://api.github.com/", timeout=2)
    except OSError:
        pass  # the code under test took it in its stride


def test_a_quiet_one() -> None:
    assert True
'''


@pytest.mark.slow
def test_errors_nobody_could_catch_fail_the_test(tmp_path: Path) -> None:
    """A session of its own, with this conftest and this pyproject's settings: what asyncio only logs, what a
    destructor raises, a coroutine never awaited and a refused connection each fail their test."""
    inner = tmp_path / "inner"
    inner.mkdir()
    shutil.copy(HERE / "conftest.py", inner / "conftest.py")
    shutil.copytree(HERE / "testkit", inner / "testkit", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(HERE.parent / "pyproject.toml", inner / "pyproject.toml")
    (inner / "test_inner.py").write_text(INNER, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
                             f"--basetemp={tmp_path / 'temp'}", "test_inner.py"], cwd=inner, env=env,
                            capture_output=True, text=True, timeout=120)
    out = result.stdout + result.stderr
    assert "ERROR test_inner.py::test_a_task_that_failed_with_nobody_looking" in out, out  # at its teardown
    assert "asyncio logged an error during the test: Task exception was never retrieved" in out, out
    assert "PASSED test_inner.py::test_the_same_on_purpose" in out, out
    assert "FAILED test_inner.py::test_a_coroutine_never_awaited" in out, out
    assert "ERROR test_inner.py::test_github_reached_quietly" in out, out
    assert "the test tried to leave this machine or reach a real server's port: looking up api.github.com" in out
    assert "PASSED test_inner.py::test_a_quiet_one" in out, out


def test_no_test_changes_os_name_itself() -> None:
    """os.name is the whole process's: a test that sets it to "nt" on Linux makes pathlib, and pytest's own report,
    fail. A test that needs another system patches its module's os (testkit.os_as)."""
    import re

    from testkit import TESTS

    patched = re.compile(r"""setattr\(\s*(?:["'][\w.]*os\.name["']|[\w.]*\bos\s*,\s*["']name["'])""")
    found = [f"{path.relative_to(TESTS)}:{number}" for path in TESTS.rglob("*.py") if path.name != "test_guards.py"
             for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if patched.search(line)]
    assert not found, f"os.name patched for the whole process at {found}: use testkit.os_as"
