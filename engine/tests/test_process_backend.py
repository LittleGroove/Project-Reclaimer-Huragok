"""The process backend against a fake `dedicated`: starting, adopting, clean stops, crashes. Windows and Linux."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import psutil
import pytest
from conftest import staff, wait_for, write_root

from reclaimer_supervisor.config import load
from reclaimer_supervisor.core import Supervisor
from reclaimer_supervisor.events import Event

FAKE = Path(__file__).resolve().parent / "fake_dedicated.py"
ONE, TWO = "Alpha Server", "Beta Server"


def free_ports(count: int) -> list[int]:
    import socket

    ports = []
    sockets = []
    for _ in range(count):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sockets.append(sock)
        ports.append(sock.getsockname()[1])
    for sock in sockets:
        sock.close()
    return ports


def process_root(tmp_path: Path, extra: str = "") -> Path:
    root = tmp_path / "reclaimer"
    ports = free_ports(2)
    python = Path(sys.executable).as_posix()
    body = (f'backend = "process"\n[process]\ncommand = ["{python}", "{FAKE.as_posix()}", "dedicated", "run", '
            f'"--config", "{{config}}"]\nstop_timeout = 5\n[restarts]\nbackoff_seconds = [0.5]\nready_seconds = 20\n'
            f'warn_seconds = [1]\nmax_failures = 2\n' + extra)
    return write_root(root, [(ONE, ports[0]), (TWO, ports[1])], body, staff(), backend=None)


def alive(pid: int | None) -> bool:
    return bool(pid) and psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE


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

    run(scenario())


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
            assert sup.fleet.managers[ONE].phase == "running" or sup.fleet.managers[ONE].phase == "booting"
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


def test_stop_from_the_command_line_with_no_supervisor(tmp_path: Path, run, capsys) -> None:
    """`stop` with nothing running finds the servers a closed supervisor left behind and stops them."""
    from reclaimer_supervisor import cli

    settings_path = process_root(tmp_path)

    async def leave_running() -> int:
        sup = Supervisor(load(settings_path))
        task = asyncio.ensure_future(sup.run())
        await wait_for(lambda: sup.fleet.managers[ONE].phase == "running", 30)
        pid = sup.fleet.managers[ONE].pid
        sup.stop()
        await asyncio.wait_for(task, 20)
        return pid

    pid = run(leave_running())
    assert alive(pid)
    assert cli.main(["-c", str(settings_path), "status"]) == 0
    assert "nobody is watching" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "stop", "--now"]) == 0
    assert "Stopped:" in capsys.readouterr().out
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not alive(pid)
