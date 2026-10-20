"""The servers' processor priority ([process] priority, priority.py): every process in a server's tree set and
looked at again, what the system gave read back and said; the setting in settings.toml, one server's own (D, V)
and `check`."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from conftest import free_port, free_ports, staff, wait_for, write_root
from test_manager import SERVER, Clock, FakeBackend, FakeClient

from huragok import cli, priority
from huragok import manager as manager_module
from huragok.config import ConfigError, load
from huragok.events import EventBus
from huragok.lifecycle import ProcessBackend, kill_tree
from huragok.livesettings import catalogue
from huragok.manager import Fleet
from huragok.priority import Outcome, PriorityKeeper
from huragok.state import ServerState
from huragok.tomledit import read_toml

PYTHON = Path(sys.executable).as_posix()
# A venv's python.exe on Windows is a launcher that starts the real one: a tree of it would be twice as deep.
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
# Windows doesn't hand above normal on to a process's own processes (only idle and below normal), so a new one
# shows that the keeper looks again; Linux hands every nice value on, and only root may raise one.
LEVEL = "above_normal" if os.name == "nt" else "below_normal"


def is_admin() -> bool:
    if os.name != "nt":
        return os.geteuid() == 0
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def start_tree(tmp_path: Path) -> tuple[subprocess.Popen[bytes], Path]:
    """A process that starts one of its own at once, and another once `go` is there (as `dedicated` starts a new
    game worker): what a server's tree is like."""
    go = tmp_path / "go"
    sleeper = "import time; time.sleep(60)"
    script = (f"import os, subprocess, sys, time\n"
              f"subprocess.Popen([sys.executable, '-c', {sleeper!r}])\n"
              f"while not os.path.exists({str(go)!r}): time.sleep(0.05)\n"
              f"subprocess.Popen([sys.executable, '-c', {sleeper!r}])\n"
              f"time.sleep(60)\n")
    popen = subprocess.Popen([BASE_PYTHON, "-c", script])
    deadline = time.monotonic() + 20
    while len(psutil.Process(popen.pid).children(recursive=True)) < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    return popen, go


def tree_levels(pid: int) -> list[str]:
    root = psutil.Process(pid)
    return [priority.level_of(p.nice()) for p in [root, *root.children(recursive=True)]]


def end_tree(popen: subprocess.Popen[bytes]) -> None:
    kill_tree(popen.pid)
    popen.wait(10)


def test_every_process_in_a_servers_tree_is_set_and_new_ones_are_found(tmp_path: Path) -> None:
    popen, go = start_tree(tmp_path)
    try:
        outcome = priority.apply(popen.pid, LEVEL)
        assert (outcome.processes, outcome.changed, outcome.got, outcome.denied) == (2, 2, LEVEL, "")
        assert tree_levels(popen.pid) == [LEVEL, LEVEL]
        assert priority.apply(popen.pid, LEVEL).changed == 0  # as asked already: nothing set
        go.touch()
        deadline = time.monotonic() + 20
        while len(tree_levels(popen.pid)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        again = priority.apply(popen.pid, LEVEL)
        assert again.processes == 3 and again.changed == (1 if os.name == "nt" else 0)  # Linux handed it on
        assert tree_levels(popen.pid) == [LEVEL] * 3
        back = priority.apply(popen.pid, "normal")
        if os.name == "nt" or is_admin():
            assert back.changed == 3 and tree_levels(popen.pid) == ["normal"] * 3
        else:  # only root raises a nice value on Linux: refused, and said
            assert back.changed == 0 and back.denied and tree_levels(popen.pid) == [LEVEL] * 3
    finally:
        end_tree(popen)
    assert priority.apply(popen.pid, LEVEL) == Outcome()  # gone: nothing to look at


@pytest.mark.skipif(os.name != "nt" or is_admin(), reason="Windows, run without administrator")
def test_windows_gives_high_to_a_realtime_it_wont_allow_and_says_why(tmp_path: Path, caplog) -> None:
    popen, _ = start_tree(tmp_path)
    try:
        outcome = priority.apply(popen.pid, "realtime")
        assert outcome.got == "high" and priority.short_of("realtime", outcome) and outcome.denied == ""
        assert "administrator" in priority.unavailable("realtime") and priority.unavailable("high") == ""
        with caplog.at_level(logging.INFO):
            PriorityKeeper().keep("Alpha", popen.pid, "realtime")  # no event loop: looked at there and then
    finally:
        end_tree(popen)
    assert caplog.messages == ["[Alpha] runs at High priority, not Realtime: Windows only gives Realtime to a "
                               "program started as administrator"]


def test_the_keeper_says_each_outcome_once_a_run(monkeypatch, caplog) -> None:
    outcomes = [Outcome(4, 4, "high"), Outcome(5, 1, "high"), Outcome(4, 0, "high")]
    monkeypatch.setattr(priority, "apply", lambda pid, level: outcomes.pop(0))
    monkeypatch.setattr(priority, "EVERY", 0.0)
    keeper = PriorityKeeper()
    with caplog.at_level(logging.DEBUG, logger="huragok.priority"):
        for _ in range(3):
            keeper.keep("Alpha", 100, "high")
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO] == [
        "[Alpha] its processes run at High priority"]
    assert "[Alpha] 1 new process(es) set to High priority" in caplog.text  # a new game worker: quietly
    assert keeper.text("Alpha") == "High priority"
    caplog.clear()
    monkeypatch.setattr(priority, "apply", lambda pid, level: Outcome(4, 0, "high"))
    keeper.forget("Alpha")
    keeper.keep("Alpha", 200, "high")  # picked up as it was (after Huragok's own restart): nothing to say
    assert caplog.records == [] and keeper.text("Alpha") == "High priority"
    monkeypatch.setattr(priority, "apply", lambda pid, level: Outcome(1, 0, "", "access denied"))
    with caplog.at_level(logging.INFO):
        keeper.keep("Beta", 300, "high")
        keeper.keep("Beta", 300, "high")
    assert [r.getMessage() for r in caplog.records].count(
        "[Beta] couldn't set its priority to High: access denied" + (
            "" if os.name == "nt" else "; above normal needs root or CAP_SYS_NICE (in Docker, --cap-add SYS_NICE)")
    ) == 1
    assert keeper.text("Beta") == "priority not set (High asked)"
    monkeypatch.setattr(priority, "apply", lambda pid, level: Outcome(4, 4, "high"))
    with caplog.at_level(logging.INFO):
        keeper.keep("Gamma", 400, "realtime")
    assert "[Gamma] runs at High priority, not Realtime: " in caplog.text
    assert keeper.text("Gamma") == "High priority (Realtime asked)"
    keeper.keep("Gamma", 400, "off")  # off: left alone, and not shown
    assert keeper.text("Gamma") == ""


def test_the_keeper_looks_in_a_thread_and_not_too_often(tmp_path: Path, run) -> None:
    popen, _ = start_tree(tmp_path)
    keeper = PriorityKeeper()

    async def scenario() -> None:
        keeper.keep("Alpha", popen.pid, LEVEL)
        assert "Alpha" in keeper._busy  # the event loop doesn't wait for it
        await wait_for(lambda: "Alpha" in keeper.now, 20)
        assert tree_levels(popen.pid) == [LEVEL, LEVEL]
        # Moved off it by hand: back to normal, or further down on Linux, where only root raises a nice value.
        moved = priority.value_for("normal") if os.name == "nt" or is_admin() else 10
        psutil.Process(popen.pid).nice(moved)
        keeper.keep("Alpha", popen.pid, LEVEL)  # within EVERY of the last look: not looked at again yet
        assert "Alpha" not in keeper._busy
        await asyncio.sleep(0.2)
        assert psutil.Process(popen.pid).nice() == moved

    try:
        run(scenario())
    finally:
        end_tree(popen)


# The setting --------------------------------------------------------------------------------------------------------

def root_with(tmp_path: Path, body: str, backend: str | None = None) -> Path:
    return write_root(tmp_path / "root", [("Alpha", free_port()), ("Beta", free_port())],
                      ('backend = "process"\n' if backend is None else "") + body, staff(), backend=backend)


def test_high_unless_settings_toml_says_otherwise(tmp_path: Path) -> None:
    settings = load(root_with(tmp_path / "a", ""))
    assert [settings.priority_for(s) for s in settings.servers] == ["high", "high"]
    settings = load(root_with(tmp_path / "b", '[process]\npriority = "realtime"\n'
                                              '[[server]]\nname = "Beta"\npriority = "off"\n'))
    assert [settings.priority_for(s) for s in settings.servers] == ["realtime", "off"]
    settings = load(root_with(tmp_path / "c", "", backend="attach"))
    assert settings.priority_for(settings.servers[0]) == "off"  # not run by Huragok: never touched
    with pytest.raises(ConfigError, match="priority: must be one of realtime, high, above_normal, normal, "
                                          "below_normal, off"):
        load(root_with(tmp_path / "d", '[process]\npriority = "turbo"\n'))
    with pytest.raises(ConfigError, match=r"\[\[server\]\] Beta priority: must be one of"):
        load(root_with(tmp_path / "e", '[[server]]\nname = "Beta"\npriority = "max"\n'))


def test_a_server_run_by_huragok_is_kept_at_its_priority(tmp_path: Path, run, caplog) -> None:
    sleeper = f'[process]\ncommand = ["{PYTHON}", "-c", "import time; time.sleep(60)"]\nstop_timeout = 1\n' \
              f'priority = "{LEVEL}"\n'
    settings = load(write_root(tmp_path / "root", [("Alpha", free_ports(1)[0])], 'backend = "process"\n' + sleeper,
                               backend=None))
    backend = ProcessBackend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    (server,) = settings.servers

    async def scenario() -> None:
        await backend.start(server)
        backend.drain(server)  # the manager's every tick
        await wait_for(lambda: backend.priority_text(server), 20)
        assert priority.level_of(psutil.Process(backend.pid(server)).nice()) == LEVEL
        await backend.stop(server, timeout=1)
        assert backend.priority_text(server) == ""

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert f"[Alpha] its processes run at {priority.LABELS[LEVEL]} priority" in caplog.text


def test_one_servers_own_priority_from_its_page(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(manager_module, "time", Clock(1_800_000_000.0))
    settings = load(write_root(tmp_path / "reclaimer", [(SERVER, free_port())]))
    bus = EventBus()
    state = ServerState(SERVER)
    fleet = Fleet(settings, FakeBackend(settings, bus, {SERVER: state}), bus)
    manager = fleet.add(settings.servers[0], FakeClient(state), state)  # type: ignore[arg-type]
    entry = next((s, v, w) for s, v, w in catalogue(settings.servers[0].config_path, SERVER, settings)
                 if s.key == "priority")
    assert entry[0].choices == priority.PRIORITIES and entry[1:] == ("off", "[process]")  # attach: not run here
    assert manager.set_setting("priority", "realtime").ok
    assert read_toml(settings.path)["server"][0]["priority"] == "realtime" and manager.server.priority == "realtime"
    assert manager.set_setting("priority", "fast").message == ("priority: one of realtime, high, above_normal, "
                                                               "normal, below_normal, off")
    assert manager.set_setting("priority", None).ok and manager.server.priority is None
    assert "priority" not in read_toml(settings.path)["server"][0]


def test_check_names_each_servers_priority(tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    path = root_with(tmp_path, '[process]\npriority = "realtime"\n[[server]]\nname = "Beta"\npriority = "high"\n')
    monkeypatch.setattr(priority, "unavailable", lambda level: "the reason" if level == "realtime" else "")
    cli.main(["-c", str(path), "check"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert "realtime" in text and "high (its own)" in text
    assert text.count("warning: priority realtime: the reason") == 1  # Alpha's; Beta has its own
