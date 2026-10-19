"""The process backend against a fake `dedicated`: starting, adopting, clean stops, crashes. Windows and Linux.
And the rest of lifecycle.py: the command backend, the server's command line, the keepalive's hang test."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest
from conftest import free_port, free_ports, staff, wait_for, write_root

from huragok import lifecycle as lifecycle_module
from huragok import winproc
from huragok.config import KeepaliveSettings, load
from huragok.core import Supervisor
from huragok.events import Event, EventBus
from huragok.lifecycle import CommandBackend, Keepalive, LifecycleError, ProcessBackend
from huragok.state import ServerState

FAKE = Path(__file__).resolve().parent / "fake_dedicated.py"
ONE, TWO = "Alpha Server", "Beta Server"
PYTHON = Path(sys.executable).as_posix()
SLEEPER = f'[process]\ncommand = ["{PYTHON}", "-c", "import time; time.sleep(60)"]\nstop_timeout = 1\n'


def process_root(tmp_path: Path, extra: str = "") -> Path:
    root = tmp_path / "reclaimer"
    ports = free_ports(2)
    python = Path(sys.executable).as_posix()
    body = (f'backend = "process"\n[process]\ncommand = ["{python}", "{FAKE.as_posix()}", "dedicated", "run", '
            f'"--config", "{{config}}"]\nstop_timeout = 5\n[restarts]\nbackoff_seconds = [0.5]\nready_seconds = 30\n'
            f'warn_seconds = [1]\nmax_failures = 2\n' + extra)
    return write_root(root, [(ONE, ports[0]), (TWO, ports[1])], body, staff(), backend=None)


def reap(*backends: Any) -> None:
    """Collect the processes these backends started once they have ended (a closed Huragok leaves its servers
    running on purpose, so nobody else waits on them; a stopped one's is never asked again)."""
    for backend in backends:
        for managed in backend.managed.values():
            if managed.popen is not None:
                try:
                    managed.popen.wait(10)
                except subprocess.TimeoutExpired:
                    pass


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


@pytest.mark.slow
def test_two_servers_run_crash_and_are_adopted(tmp_path: Path, run) -> None:
    settings_path = process_root(tmp_path)

    async def scenario() -> None:
        settings = load(settings_path)
        sup = Supervisor(settings)
        seen: list[Event] = []
        sup.bus.subscribe("server.*", seen.append)
        task = asyncio.ensure_future(sup.run())
        managers = sup.fleet.managers
        try:
            await wait_for(lambda: all(m.phase == "running" for m in managers.values()), 30)
            assert all(sup.states[n].connected for n in (ONE, TWO))
            saved = json.loads((settings.records / "running-servers.json").read_text(encoding="utf-8"))
            assert set(saved) == {ONE, TWO}
            pid_one = managers[ONE].pid
            assert alive(pid_one) and alive(managers[TWO].pid)
            console = settings.logs / "console-alpha-server.log"
            await wait_for(lambda: "started (process" in console.read_text(encoding="utf-8"))
            assert any(e.name == "server.started" and e.server == ONE for e in seen)  # from the console line

            # Stop one server from the dashboard's point of view: a clean stop, the other untouched.
            (name, result), = await sup.fleet.verb("stop", TWO, immediate=True)
            assert result.ok and name == TWO
            await wait_for(lambda: managers[TWO].phase == "idle", 15)
            assert not alive(saved[TWO]["pid"])
            assert "All servers stopped" in (settings.logs / "console-beta-server.log").read_text(encoding="utf-8")
            assert managers[ONE].phase == "running" and alive(pid_one)
            assert managers[TWO].record.wanted == "stopped"

            # The other crashes: backoff, then a new process, without anyone asking.
            (settings.servers[0].folder / "fake_crash").write_text("", encoding="utf-8")
            await wait_for(lambda: managers[ONE].phase in ("backoff", "launch", "booting"), 15)
            assert any(e.name == "server.stopped" and not e["expected"] for e in seen)
            (settings.servers[0].folder / "fake_crash").unlink()
            await wait_for(lambda: managers[ONE].phase == "running" and managers[ONE].pid != pid_one, 30)
            assert managers[ONE].record.fail_count == 1
            pid_one = managers[ONE].pid
        finally:
            sup.stop()  # the window closes: the servers keep running
            await asyncio.wait_for(task, 20)
        assert alive(pid_one)
        first = sup.backend

        # A new supervisor picks it up without restarting it, and starts the stopped one only when asked.
        settings = load(settings_path)
        sup = Supervisor(settings)
        seen = []
        sup.bus.subscribe("server.*", seen.append)
        task = asyncio.ensure_future(sup.run())
        try:
            await wait_for(lambda: sup.fleet.managers[ONE].phase == "running", 20)
            assert sup.fleet.managers[ONE].pid == pid_one and any(e.name == "server.adopted" for e in seen)
            # autostart: the one we stopped is wanted again at a fresh start.
            await wait_for(lambda: sup.fleet.managers[TWO].phase == "running", 30)
            await wait_for(lambda: all(sup.states[n].connected for n in (ONE, TWO)), 20)
            # stop everything: the servers go, then the supervisor itself.
            sup.stop_everything(immediate=True)
            await asyncio.wait_for(task, 30)
        finally:
            if not task.done():
                sup.stop()
                await asyncio.wait_for(task, 20)
        assert not alive(pid_one) and not alive(sup.fleet.managers[TWO].pid)
        assert sup.fleet.managers[ONE].phase == "idle" and sup.fleet.managers[TWO].phase == "idle"
        reap(first)

    run(scenario())


@pytest.mark.slow
def test_a_server_that_wont_start_faults_and_can_be_retried(tmp_path: Path, run) -> None:
    settings_path = process_root(tmp_path)
    settings = load(settings_path)
    (settings.servers[1].folder / "fake_crash_on_start").write_text("", encoding="utf-8")

    async def scenario() -> None:
        sup = Supervisor(settings)
        task = asyncio.ensure_future(sup.run())
        try:
            two = sup.fleet.managers[TWO]
            await wait_for(lambda: two.phase == "faulted", 30)
            assert two.record.fail_count == 2 and (settings.logs / "faulted-beta-server.txt").exists()
            await wait_for(lambda: sup.fleet.managers[ONE].phase == "running", 30)  # the other one is unaffected
            (settings.servers[1].folder / "fake_crash_on_start").unlink()
            (name, result), = await sup.fleet.verb("start", TWO)
            assert result.ok
            await wait_for(lambda: two.phase == "running", 30)
            sup.stop_everything(immediate=True)
            await asyncio.wait_for(task, 30)
        finally:
            if not task.done():
                sup.stop()
                await asyncio.wait_for(task, 20)

    run(scenario())


@pytest.mark.slow
def test_stop_from_the_command_line_with_no_supervisor(tmp_path: Path, run, capsys) -> None:
    """`stop` with nothing running finds the servers a closed supervisor left behind and stops them."""
    from huragok import cli

    settings_path = process_root(tmp_path)

    async def leave_running() -> tuple[int, Any]:
        sup = Supervisor(load(settings_path))
        task = asyncio.ensure_future(sup.run())
        await wait_for(lambda: sup.fleet.managers[ONE].phase == "running", 30)
        pid = sup.fleet.managers[ONE].pid
        sup.stop()
        await asyncio.wait_for(task, 20)
        return pid, sup.backend

    pid, backend = run(leave_running())
    assert alive(pid)
    assert cli.main(["-c", str(settings_path), "status"]) == cli.NOT_RUNNING
    assert "nobody is watching" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "stop", "--now"]) == 0
    assert "Stopped:" in capsys.readouterr().out
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not alive(pid)
    reap(backend)


# Stops that go wrong ----------------------------------------------------------------------------------------

def sleeper_backend(tmp_path: Path, names: tuple[str, ...] = ("Alpha",)) -> tuple[ProcessBackend, list[Any]]:
    settings = load(write_root(tmp_path / "root", list(zip(names, free_ports(len(names)))),
                               'backend = "process"\n' + SLEEPER, backend=None))
    backend = ProcessBackend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    return backend, settings.servers


def end(pid: int | None) -> None:
    if pid and alive(pid):
        psutil.Process(pid).kill()


@pytest.mark.skipif(os.name != "nt", reason="the Ctrl+Break helper is Windows's")
def test_a_ctrl_break_helper_that_fails_still_ends_the_process(tmp_path: Path, run, monkeypatch, caplog) -> None:
    """The helper timing out (or failing to run) escaped the stop before the kill: the process kept running, still
    ours, and the manager later took it back as running, the stop undone."""
    backend, (server,) = sleeper_backend(tmp_path)

    def times_out(pid: int, timeout: float = 10.0) -> bool:
        raise subprocess.TimeoutExpired(["python", "-m", "huragok", "_ctrl-break", str(pid)], timeout)

    async def scenario() -> int | None:
        await backend.start(server)
        pid = backend.pid(server)
        monkeypatch.setattr(winproc, "ctrl_break", times_out)
        try:
            await backend.stop(server)
        finally:
            end(pid)
            reap(backend)
        return pid

    with caplog.at_level(logging.WARNING):
        pid = run(scenario())
    assert not alive(pid) and not backend.alive(server)
    assert f"couldn't send Ctrl+Break to process {pid} (TimeoutExpired" in caplog.text


def test_a_process_that_outlives_its_stop_stays_ours_and_the_stop_fails(tmp_path: Path, run, monkeypatch) -> None:
    backend, (server,) = sleeper_backend(tmp_path)

    async def never(managed: Any, timeout: float) -> bool:
        return False

    async def scenario() -> None:
        await backend.start(server)
        pid = backend.pid(server)
        try:
            monkeypatch.setattr(backend, "_signal_stop", lambda managed: False)
            monkeypatch.setattr(backend, "_kill_tree", lambda pid: None)  # as when ending it is refused
            monkeypatch.setattr(backend, "_wait_exit", never)
            with pytest.raises(LifecycleError, match=f"couldn't end process {pid}"):
                await backend.stop(server)
            assert backend.alive(server) and backend.pid(server) == pid  # not forgotten: a start won't run another
            saved = json.loads(backend.state_file.read_text(encoding="utf-8"))
            assert saved[server.name]["pid"] == pid
        finally:
            end(pid)
            reap(backend)

    run(scenario())


# running-servers.json ---------------------------------------------------------------------------------------

def test_a_server_left_out_at_startup_keeps_its_record_and_is_picked_up_again(tmp_path: Path, run) -> None:
    """A dedicated.toml typo left Beta out at this start: adopt() rewrote running-servers.json without it, so once
    the folder was fixed, its still-running server was "started some other way" for good."""
    backend, servers = sleeper_backend(tmp_path, ("Alpha", "Beta"))
    settings = backend.settings
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        console = settings.logs / "console-beta.log"
        console.parent.mkdir(parents=True, exist_ok=True)
        console.write_text("", encoding="utf-8")
        settings.records.mkdir(parents=True, exist_ok=True)
        entry = {"pid": child.pid, "create_time": psutil.Process(child.pid).create_time(), "console": str(console)}
        backend.state_file.write_text(json.dumps({"Beta": entry}), encoding="utf-8")
        toml = settings.home / "SERVERS" / "Beta" / "dedicated.toml"
        good = toml.read_text(encoding="utf-8")
        toml.write_text("this is = = not toml", encoding="utf-8")
        broken = load(settings.path)
        assert [s.name for s in broken.servers] == ["Alpha"]

        async def scenario() -> None:
            sup = Supervisor(broken)
            sup.backend.adopt()
            saved = json.loads(sup.backend.state_file.read_text(encoding="utf-8"))
            assert saved["Beta"]["pid"] == child.pid  # kept
            toml.write_text(good, encoding="utf-8")
            sup.discover()  # found again while running
            beta = sup.settings.server("Beta")
            assert sup.backend.alive(beta) and sup.backend.pid(beta) == child.pid  # ours again, not external
            sup.storage.close()

        run(scenario())
    finally:
        child.kill()
        child.wait()


def test_a_damaged_running_servers_file_reads_as_empty_and_is_written_whole(tmp_path: Path, caplog) -> None:
    backend, (server,) = sleeper_backend(tmp_path)
    backend.settings.records.mkdir(parents=True, exist_ok=True)
    backend.state_file.write_text('{"Alpha": {"pid": 12', encoding="utf-8")  # killed mid-write, before 0.2.2
    with caplog.at_level(logging.WARNING):
        backend.adopt()
    assert "is damaged, so servers still running from before aren't picked up" in caplog.text
    backend.state_file.write_text('{"Alpha": {"pid": "twelve"}}', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        backend.adopt()
    assert "the entry for Alpha is damaged" in caplog.text
    backend._save()
    assert json.loads(backend.state_file.read_text(encoding="utf-8")) == {}
    assert [p.name for p in backend.state_file.parent.iterdir()] == ["running-servers.json"]  # no temporary left


# Starting -----------------------------------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="job objects are Windows's")
def test_servers_start_outside_huragoks_job_object_when_windows_allows(tmp_path: Path, run, monkeypatch,
                                                                      caplog) -> None:
    backend, (server,) = sleeper_backend(tmp_path)
    real_popen = subprocess.Popen
    flags: list[int] = []

    def no_breakaway(command: Any, **options: Any) -> Any:
        flags.append(options.get("creationflags", 0))
        if options.get("creationflags", 0) & winproc.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(13, "Access is denied", None, winproc.ERROR_ACCESS_DENIED)
        return real_popen(command, **options)

    monkeypatch.setattr(lifecycle_module.subprocess, "Popen", no_breakaway)

    async def scenario() -> None:
        for _ in range(2):
            await backend.start(server)
            pid = backend.pid(server)
            assert alive(pid)
            end(pid)
            reap(backend)
            backend.managed.pop(server.name)

    with caplog.at_level(logging.WARNING):
        run(scenario())
    breakaway, plain = winproc.creation_flags(breakaway=True), winproc.creation_flags()
    assert flags == [breakaway, plain, plain]  # refused once: the next ones don't ask again
    assert "Windows won't start servers outside Huragok's job object" in caplog.text


def test_the_server_command_line(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    settings = load(write_root(root, [("Alpha", free_port())], 'backend = "process"\n', backend=None, exe=True))
    backend = ProcessBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})
    server = settings.servers[0]
    exe = str(server.folder / "project-reclaimer.exe")
    monkeypatch.setattr(lifecycle_module, "_on_windows", lambda: True)
    assert backend.command_for(server) == [exe, "dedicated", "run", "--config", "dedicated.toml"]
    assert backend.problems(server) == []
    monkeypatch.setattr(lifecycle_module, "_on_windows", lambda: False)  # Linux: through wine
    assert backend.command_for(server) == ["wine", exe, "dedicated", "run", "--config", "dedicated.toml"]
    monkeypatch.setattr(lifecycle_module.shutil, "which", lambda name: None)
    assert any("wine is not installed" in p for p in backend.problems(server))
    (server.folder / "project-reclaimer.exe").unlink()
    with pytest.raises(LifecycleError, match="not found: put the server program"):
        backend.command_for(server)
    assert any("project-reclaimer.exe is not in" in p for p in backend.problems(server))
    backend.command_template = ["/nowhere/run-it", "{executable}", "{config}"]
    assert backend.command_for(server) == ["/nowhere/run-it", exe, "dedicated.toml"]
    assert backend.problems(server) == ["[process] command: /nowhere/run-it was not found"]


# The command backend --------------------------------------------------------------------------------------------

def command_backend(tmp_path: Path, **commands: str) -> tuple[CommandBackend, Any, Path]:
    marker = tmp_path / "ran.txt"
    lines = ['backend = "command"', "[command]"]
    for action, code in commands.items():
        lines.append(f"{action} = '''\"{PYTHON}\" -c \"{code}\"'''")
    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], "\n".join(lines) + "\n", backend=None))
    backend = CommandBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})
    return backend, settings.servers[0], marker


def note(marker: Path, word: str) -> str:
    return f"open(r'{marker.as_posix()}', 'a').write('{word} ')"


def test_reviving_a_server_without_a_process_to_watch_restarts_it(tmp_path: Path, run) -> None:
    """Seen dead only because RCON and status.json went quiet, it may be hung: a start command does nothing to a
    server still running, so it's restarted, or stopped then started (a stop failing there doesn't stop the start)."""
    marker = tmp_path / "ran.txt"
    backend, server, _ = command_backend(tmp_path / "a", start=note(marker, "start"), stop=note(marker, "stop"),
                                         restart=note(marker, "restart"))
    run(backend.revive(server))
    assert marker.read_text().split() == ["restart"]
    marker.unlink()
    backend, server, _ = command_backend(tmp_path / "b", start=note(marker, "start"),
                                         stop="import sys; sys.exit(1)")
    run(backend.revive(server))
    assert marker.read_text().split() == ["start"]


def test_a_start_command_called_off_is_ended(tmp_path: Path, run) -> None:
    pid_file = tmp_path / "pid.txt"
    backend, server, _ = command_backend(
        tmp_path, start=f"import os, time; open(r'{pid_file.as_posix()}', 'w').write(str(os.getpid())); time.sleep(60)",
        stop="pass")

    async def scenario() -> int:
        task = asyncio.ensure_future(backend.start(server))
        await wait_for(lambda: pid_file.exists() and pid_file.read_text(), 20)
        pid = int(pid_file.read_text())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)  # the ended command is collected
        return pid

    pid = run(scenario())
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid)


# The keepalive ------------------------------------------------------------------------------------------------------

def test_the_keepalive_sees_a_hang_before_the_first_status_file(tmp_path: Path) -> None:
    """A new server's first run, frozen before it wrote status.json: with no file, RCON going quiet counted for
    nothing, so it was never found hung."""
    keepalive = Keepalive(KeepaliveSettings(hang_seconds=60), None, {}, EventBus())  # type: ignore[arg-type]
    state = ServerState("Alpha")
    state.last_rcon_ok = time.monotonic() - 100  # its console answered as it came up, then nothing
    assert keepalive.is_hung(state, up_for=120)
    state.last_rcon_ok = time.monotonic() - 5  # still answering
    assert not keepalive.is_hung(state, up_for=120)
    state.last_rcon_ok = None  # its console never answered this run (unusable): no signal at all, so no restart
    assert not keepalive.is_hung(state, up_for=120)
    state.last_rcon_ok = time.monotonic() - 500  # the run before's
    assert not keepalive.is_hung(state, up_for=120)
    state.status_updated = time.time() - 100  # with the file, as before
    state.last_rcon_ok = time.monotonic() - 100
    assert keepalive.is_hung(state, up_for=120)
