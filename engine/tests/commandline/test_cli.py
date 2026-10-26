"""The root folder with SERVERS/: the first start, finding things, one supervisor at a time, the commands."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, free_ports, write_root, write_server_folder
from testkit import REPO

from huragok.commandline import cli, common, firstrun
from huragok.commandline import launcher as launcher_module
from huragok.core import lock as lock_module
from huragok.core.settings import load


def test_the_first_start_writes_the_files_and_stops(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    first, second = free_ports(2)
    write_server_folder(root / "SERVERS" / "LG Test Server", first, exe=True)
    write_server_folder(root / "SERVERS" / "Second", second, name="Second", exe=True)
    assert cli.main(["-c", str(root / "settings.toml"), "start"]) == common.SET_UP
    assert {p.name for p in root.iterdir()} == {"settings.toml", "commands.toml", "automod.txt", "names.txt",
                                              "intervalmessages.txt", "SERVERS", "PACKAGES"}
    assert (root / "PACKAGES" / "team-slayer" / "playlists" / "team-slayer.playlist.json").is_file()
    out = capsys.readouterr().out
    assert "Put your player ID under [owner]" in out and "start again" in out and "2 server folder(s) found" in out
    settings = load(root / "settings.toml")  # works as written
    assert [s.name for s in settings.servers] == ["LG Test Server", "Second"]
    assert settings.servers_folder == (root / "SERVERS").resolve() and settings.single_folder is None
    assert settings.servers[0].folder == (root / "SERVERS" / "LG Test Server").resolve()
    assert settings.backend == "process"
    assert settings.database == root.resolve() / "records" / "huragok.sqlite3"
    assert settings.parts["automod"].enabled and settings.parts["automod"].dry_run
    text = (root / "settings.toml").read_text(encoding="utf-8")
    assert "servers_folder = 'SERVERS'" in text and "server_folder = ''" in text
    assert "{executable}" in text and "{{" not in text  # the documented placeholders survive as written
    if os.name == "nt":  # on Linux `check` also wants wine for backend process
        assert cli.main(["-c", str(root / "settings.toml"), "check"]) == 0
        assert "nobody is owner yet" in capsys.readouterr().out


def test_the_first_start_with_no_servers_makes_the_folder_and_explains(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    root.mkdir()
    assert cli.main(["-c", str(root / "settings.toml"), "start"]) == common.SET_UP
    out = capsys.readouterr().out
    assert (root / "SERVERS").is_dir() and "no server folders yet" in out and "dedicated init" in out
    settings = load(root / "settings.toml")
    assert settings.servers == []
    assert cli.main(["-c", str(root / "settings.toml"), "check"]) == 1
    assert "none found" in capsys.readouterr().out


def test_the_old_drop_in_layout_still_works(tmp_path: Path) -> None:
    """0.1 put the supervisor's folder inside the one server folder."""
    write_server_folder(tmp_path, free_port(), exe=True)
    home = tmp_path / "huragok"
    home.mkdir()
    assert cli.main(["-c", str(home / "settings.toml"), "start"]) == common.SET_UP
    settings = load(home / "settings.toml")
    assert settings.single_folder == tmp_path.resolve() and settings.servers[0].name == "LG Test Server"
    assert "server_folder = '..'" in (home / "settings.toml").read_text(encoding="utf-8")
    assert firstrun.detect_layout(home)[1] == ".."
    write_server_folder(home, free_port(), name="Here", exe=True)  # unzipped into a server folder itself
    assert firstrun.detect_layout(home)[1] == "."


def test_init_never_replaces_your_files_unless_told(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    (root / "settings.toml").unlink()
    firstrun.write_files(root)
    (root / "automod.txt").write_text("badword\n", encoding="utf-8")
    (root / "names.txt").write_text("Bob\n", encoding="utf-8")
    (root / "commands.toml").write_text("[off]\ncommands = []\n", encoding="utf-8")
    assert cli.main(["-c", str(root / "settings.toml"), "init"]) == 1
    cli.main(["-c", str(root / "settings.toml"), "init", "--force"])
    assert (root / "automod.txt").read_text(encoding="utf-8") == "badword\n"  # the word list is never replaced
    assert (root / "names.txt").read_text(encoding="utf-8") == "Bob\n"  # nor the banned names
    assert "[moderator]" in (root / "commands.toml").read_text(encoding="utf-8")


def test_the_backend_follows_what_the_server_folders_have(tmp_path: Path) -> None:
    assert firstrun.pick_backend([])[0] == "process"
    folder = tmp_path / "server"
    folder.mkdir()
    assert firstrun.pick_backend([folder])[0] == "process"
    (folder / "project-reclaimer-dedicated-v0.9.18.exe").write_bytes(b"")
    assert firstrun.pick_backend([folder])[:2] == ("process", {"executable": "project-reclaimer-dedicated-v0.9.18.exe"})
    (folder / "project-reclaimer.exe").write_bytes(b"")
    assert firstrun.pick_backend([folder])[1] == {"executable": "project-reclaimer.exe"}
    (folder / "compose.yaml").write_text("services: {}", encoding="utf-8")
    assert firstrun.pick_backend([folder])[:2] == ("docker", {"compose_file": "compose.yaml"})


def test_only_one_supervisor_runs_from_a_folder(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    settings = load(root / "settings.toml")
    assert lock_module.running_supervisor(settings) is None
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        import psutil

        settings.records.mkdir()
        (settings.records / lock_module.LOCK_FILE).write_text(json.dumps(
            {"pid": other.pid, "create_time": psutil.Process(other.pid).create_time()}), encoding="utf-8")
        assert lock_module.running_supervisor(settings) == other.pid
        with pytest.raises(lock_module.AlreadyRunning):
            lock_module.take(settings)
        assert cli.main(["-c", str(root / "settings.toml"), "start", "--no-menu"]) == common.ALREADY_RUNNING
        assert "already running" in capsys.readouterr().err
    finally:
        other.kill()
        other.wait()
    assert lock_module.running_supervisor(settings) is None  # a lock left by a process that's gone doesn't count
    lock = lock_module.take(settings)  # and is taken over
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lock.unlink()


def test_stop_and_status_with_nothing_running(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    assert cli.main(["-c", str(root / "settings.toml"), "stop"]) == 1
    assert "doesn't stop the servers" in capsys.readouterr().err
    assert cli.main(["-c", str(root / "settings.toml"), "status"]) == common.NOT_RUNNING
    out = capsys.readouterr().out
    assert "Huragok isn't running" in out and "LG Test Server" in out and "stopped" in out
    assert cli.main(["-c", str(root / "settings.toml"), "restart", "LG Test Server"]) == 1
    assert "needs the Superintendent" in capsys.readouterr().err



VENV_PYTHON_LINE = 'set "PYTHON=%~dp0engine\\.venv\\Scripts\\python.exe"'


def _script_env(**extra: str) -> dict[str, str]:
    """This environment without Huragok's own variables (HURAGOK_CONFIG is read as the module loads)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HURAGOK_") and k.upper() != "PYTHONPATH"}
    env.update(extra)
    return env


def _engine_copy(root: Path) -> Path:
    engine = root / "engine"
    shutil.copytree(REPO / "engine" / "huragok", engine / "huragok", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "engine" / "requirements.txt", engine / "requirements.txt")
    return engine


@pytest.mark.slow
@pytest.mark.skipif(os.name != "nt", reason="start.cmd is for Windows")
def test_start_cmd_passes_commands_through(tmp_path: Path) -> None:
    """The real script, with a stand-in engine/.venv so nothing is downloaded."""
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "LG Test Server", free_port(), exe=True)
    engine = _engine_copy(root)
    (engine / ".venv").mkdir()
    shutil.copy(engine / "requirements.txt", engine / ".venv" / "requirements.txt")  # as if installed already
    # A venv's python.exe can't be copied on its own; point the script at this Python instead. If this line ever
    # stopped matching, the script would make a real venv and install from the internet: so it must match.
    original = (REPO / "start.cmd").read_text(encoding="utf-8")
    assert original.count(VENV_PYTHON_LINE) == 1
    script = original.replace(VENV_PYTHON_LINE, f'set "PYTHON={sys.executable}"')
    (root / "start.cmd").write_text(script, encoding="utf-8", newline="\r\n")
    result = subprocess.run(["cmd", "/c", str(root / "start.cmd"), "init"], capture_output=True, text=True,
                            timeout=60, env=_script_env())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Next: start.cmd check" in result.stdout and "Installing" not in result.stdout
    assert (root / "settings.toml").exists() and (root / "commands.toml").exists()


@pytest.mark.slow
@pytest.mark.skipif(os.name != "nt", reason="start.cmd is for Windows")
def test_start_cmd_makes_a_broken_venv_again_with_a_python_whose_path_has_accents(tmp_path: Path) -> None:
    """engine\\.venv's Python can't run (its Python was uninstalled): start.cmd makes the venv again, with a Python
    found on PATH in a folder named with accents (read back through `for /f`, its path came out garbled). pip is
    kept off the internet, so the setup stops at the packages, as with no connection."""
    base = tmp_path / "Jösé Pythön"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(base)], check=True, timeout=120)
    root = tmp_path / "reclaimer"
    engine = _engine_copy(root)
    scripts = engine / ".venv" / "Scripts"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(engine / ".venv")], check=True, timeout=120)
    config = engine / ".venv" / "pyvenv.cfg"  # its Python uninstalled: the launcher finds nothing to run
    gone = tmp_path / "uninstalled"
    config.write_text("".join(f"home = {gone}\n" if line.startswith("home") else
                              f"executable = {gone / 'python.exe'}\n" if line.startswith("executable") else line
                              for line in config.read_text(encoding="utf-8").splitlines(True)), encoding="utf-8")
    assert f"home = {gone}\n" in config.read_text(encoding="utf-8")  # the edit took: venv's own layout unchanged
    assert subprocess.run([str(scripts / "python.exe"), "-c", ""], capture_output=True, timeout=60).returncode != 0
    shutil.copy(engine / "requirements.txt", engine / ".venv" / "requirements.txt")  # marked as installed
    shutil.copy(REPO / "start.cmd", root / "start.cmd")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    env = _script_env(PATH=f"{base / 'Scripts'};{system}", PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    result = subprocess.run(["cmd", "/c", str(root / "start.cmd"), "check"], capture_output=True, text=True,
                            timeout=120, env=env, stdin=subprocess.DEVNULL)
    text = result.stdout + result.stderr
    assert "doesn't work any more" in text and "Setting up for the first time" in text, text
    assert "didn't find it" not in text and "cannot find the path" not in text, text
    assert "Installing the Python packages" in text and result.returncode == 1, text
    made = subprocess.run([str(scripts / "python.exe"), "-c", "import sys; print(sys.prefix)"], capture_output=True,
                          text=True, timeout=60)
    assert made.returncode == 0 and Path(made.stdout.strip()) == (engine / ".venv").resolve()


# The start loop: Huragok comes back by itself -------------------------------------------------------------------


class Launches:
    """subprocess.Popen, the clock and sleep for the start loop: each start takes the next (exit code, seconds it
    runs, what happens meanwhile) from the plan, and the clock moves only as they run and as the loop waits."""

    def __init__(self, plan: list[tuple]) -> None:
        self.plan = list(plan)
        self.commands: list[list[str]] = []
        self.now = 1000.0
        self.slept: list[float] = []
        self.handlers: dict[int, object] = {}
        self.gaps: list[float] = []  # seconds from one Huragok's end to the next one's start
        self.ended: float | None = None

    def popen(self, command: list[str], *args: object, **kwargs: object) -> object:
        assert self.plan, "started more often than planned"
        self.commands.append(command)
        if self.ended is not None:
            self.gaps.append(round(self.now - self.ended, 6))
        code, seconds, *during = self.plan.pop(0)
        launches = self

        class Child:
            returncode = None

            def wait(self) -> int:
                launches.now += seconds
                for action in during:
                    action(launches)
                launches.ended = launches.now
                self.returncode = code
                return code

            def poll(self) -> int | None:
                return self.returncode

            def send_signal(self, signum: int) -> None:
                pass

        return Child()

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept.append(seconds)
        if self.on_sleep is not None:
            self.on_sleep(self)

    on_sleep: Any = None


@pytest.fixture
def loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Runs launcher_module.start_loop against a plan, with no real process, clock or signal handler."""
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    settings = load(root / "settings.toml")

    def run(plan: list[tuple], *, no_menu: bool = True, disk: list[str] | None = None,
            restore: object = None, on_sleep: Any = None) -> tuple[int, Launches]:
        launches = Launches(plan)
        launches.on_sleep = on_sleep
        monkeypatch.setattr(launcher_module.subprocess, "Popen", launches.popen)
        monkeypatch.setattr(launcher_module, "_clock", lambda: launches.now)
        monkeypatch.setattr(launcher_module, "_sleep", launches.sleep)
        monkeypatch.setattr(launcher_module.signal, "signal", lambda signum, handler: launches.handlers.__setitem__(signum, handler))
        if disk is not None:
            monkeypatch.setattr(launcher_module, "_version_on_disk", lambda home: disk[0])
        if restore is not None:
            monkeypatch.setattr(launcher_module, "_restore_previous", restore)
        args = argparse.Namespace(config=str(settings.path), verbose=False, no_menu=no_menu, hold=False)
        return launcher_module.start_loop(args, settings), launches

    run.settings = settings  # type: ignore[attr-defined]
    return run


def _launcher_lines(settings) -> str:
    path = settings.home / launcher_module.LAUNCHER_LOG
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_crash_is_followed_by_a_new_start(loop, capsys) -> None:
    code, launches = loop([(1, 30), (0, 60)])
    assert code == 0 and len(launches.commands) == 2
    assert launches.commands[0][-2:] == ["--child", "--no-menu"]
    assert round(sum(launches.slept)) == launcher_module.CRASH_WAITS[0]
    line = "Huragok stopped unexpectedly (exit code 1); starting it again in 5 s"
    assert line in capsys.readouterr().err and line in _launcher_lines(loop.settings)
    assert " ERROR   launcher: " in _launcher_lines(loop.settings)  # shaped like Huragok's own log lines
    assert not (loop.settings.records / lock_module.LOCK_FILE).exists()  # given up at the end


def test_crashes_in_a_row_wait_longer_and_a_good_run_starts_over(loop) -> None:
    plan = [(1, 10), (3221225477, 10), (-11, 10), (1, 10), (1, 10), (1, 10), (1, launcher_module.GOOD_RUN_SECONDS + 1), (0, 5)]
    code, launches = loop(plan)
    assert code == 0 and len(launches.commands) == len(plan)
    assert launches.gaps == [5, 15, 30, 60, 300, 300, 5]
    lines = _launcher_lines(loop.settings)
    assert "exit code 0xc0000005" in lines and "ended by signal 11" in lines


def test_a_request_made_during_the_wait_starts_huragok_to_answer_it(loop) -> None:
    """start.cmd stop or close while Huragok waits to start again: the wait ends, so the new one answers in time.
    A request left from before the crash doesn't end the wait."""
    import time as clock

    def left_over(launches: Launches) -> None:
        cli.mailbox.post(loop.settings.records, "status")
        old = clock.time() - 60
        for path in cli.mailbox.folder(loop.settings.records).glob("*.json"):
            os.utime(path, (old, old))

    code, launches = loop([(1, 10, left_over), (0, 5)])
    assert code == 0 and launches.gaps == [launcher_module.CRASH_WAITS[0]]  # the old request didn't cut the wait short

    def ask(launches: Launches) -> None:
        if len(launches.slept) == 8:  # 5 s waited after the first crash, then 3 s into the 15 s after the second
            # Later than the crash by the file's clock too: Windows stamps files in 15.6 ms ticks, so a request
            # written a moment after the crash (no time passes on this test's clock) could look older than it.
            clock.sleep(0.05)
            cli.mailbox.post(loop.settings.records, "stop_all")

    code, launches = loop([(1, 10), (1, 10), (0, 5)], on_sleep=ask)
    assert code == 0 and launches.gaps == [5, 3]


def test_a_relaunch_starts_the_new_version_at_once(loop, capsys) -> None:
    disk = ["0.2.2"]
    code, launches = loop([(common.RELAUNCH, 3600, lambda launched: disk.__setitem__(0, "0.2.3")), (0, 600)], disk=disk)
    assert code == 0 and len(launches.commands) == 2 and launches.slept == []
    assert "Starting the new version (0.2.3)" in capsys.readouterr().out


def test_a_new_version_that_fails_to_start_is_taken_out_again(loop) -> None:
    disk = ["0.2.2"]
    restored: list[tuple] = []

    def restore(home: Path, version: str, failed: str) -> str:
        restored.append((home, version, failed))
        disk[0] = version
        return version

    code, launches = loop([(common.RELAUNCH, 3600, lambda launched: disk.__setitem__(0, "0.2.3")),
                           (common.SETTINGS_PROBLEM, 3),  # the new version is stricter about settings.toml
                           (0, 900)], disk=disk, restore=restore)
    assert code == 0 and len(launches.commands) == 3 and launches.slept == []
    assert restored == [(loop.settings.home, "0.2.2", "0.2.3")] and disk == ["0.2.2"]
    lines = _launcher_lines(loop.settings)
    assert "Huragok 0.2.3 stopped 3 s after it started (exit code 2), just after an update: putting 0.2.2 back" in lines
    assert "Huragok 0.2.2 is back" in lines
    marker = json.loads((loop.settings.records / launcher_module.FAILED_UPDATE).read_text(encoding="utf-8"))
    assert marker["version"] == "0.2.3" and marker["restored"] == "0.2.2"


def test_a_put_back_that_fails_keeps_trying_with_waits(loop) -> None:
    disk = ["0.2.2"]
    tries: list[str] = []

    def restore(home: Path, version: str, failed: str) -> str:
        tries.append(failed)
        if len(tries) == 1:
            raise launcher_module.RestoreError("engine\\huragok is in use")
        disk[0] = version
        return version

    code, launches = loop([(common.RELAUNCH, 3600, lambda launched: disk.__setitem__(0, "0.2.3")), (1, 2), (1, 2), (0, 900)],
                          disk=disk, restore=restore)
    assert code == 0 and len(launches.commands) == 4 and tries == ["0.2.3", "0.2.3"]
    assert round(sum(launches.slept)) == launcher_module.CRASH_WAITS[0]
    assert "Couldn't put Huragok 0.2.2 back: engine\\huragok is in use" in _launcher_lines(loop.settings)


def test_a_long_run_after_an_update_is_no_failed_update(loop) -> None:
    disk = ["0.2.2"]
    called: list[object] = []
    code, launches = loop([(common.RELAUNCH, 3600, lambda launched: disk.__setitem__(0, "0.2.3")), (1, launcher_module.FAILED_START_SECONDS + 5),
                           (0, 5)], disk=disk, restore=lambda *a: called.append(a))
    assert code == 0 and called == [] and len(launches.commands) == 3


def test_a_settings_problem_with_no_window_is_tried_again_slowly(loop) -> None:
    holders: list[int | None] = []  # who holds the folder while the loop waits

    def waiting(launches: Launches) -> None:
        holders.append(lock_module.running_supervisor(loop.settings))

    code, launches = loop([(common.SETTINGS_PROBLEM, 2), (common.SETTINGS_PROBLEM, 2), (0, 60)], no_menu=True,
                          on_sleep=waiting)
    assert code == 0 and len(launches.commands) == 3
    assert round(sum(launches.slept)) == 2 * launcher_module.SETTINGS_RETRY_SECONDS
    assert "it tries again in 5 minutes" in _launcher_lines(loop.settings)
    assert "check shows it" in _launcher_lines(loop.settings)
    # Nothing answers requests meanwhile, so the folder is free: stop and status act by themselves.
    assert holders and all(holder is None for holder in holders)


def test_a_settings_problem_in_the_window_stops_there(loop) -> None:
    code, launches = loop([(common.SETTINGS_PROBLEM, 2)], no_menu=False)
    assert code == common.SETTINGS_PROBLEM and len(launches.commands) == 1 and launches.slept == []


@pytest.mark.parametrize("code", [0, common.SET_UP, common.ALREADY_RUNNING])
def test_a_deliberate_end_ends_the_loop(loop, code: int) -> None:
    result, launches = loop([(code, 5)])
    assert result == code and len(launches.commands) == 1


def test_after_a_stop_signal_it_never_starts_again(loop) -> None:
    import signal as signals

    stop = signals.SIGBREAK if os.name == "nt" else signals.SIGTERM

    def signalled(launches: Launches) -> None:  # the stop reaches the loop while Huragok runs
        launches.handlers[stop](stop, None)  # type: ignore[operator]

    code, launches = loop([(1, 5, signalled)])
    assert code == 1 and len(launches.commands) == 1 and launches.slept == []


def test_the_loop_holds_the_folder_while_huragok_starts_again(loop) -> None:
    """A Huragok that crashed leaves the lock naming itself; the loop takes it back before waiting, so nobody else
    can take the folder until the next one starts."""
    seen: list[dict] = []

    def crash(launches: Launches) -> None:
        (loop.settings.records / lock_module.LOCK_FILE).write_text(json.dumps({"pid": 999999, "create_time": 1.0}),
                                                           encoding="utf-8")

    def look(launches: Launches) -> None:
        seen.append(lock_module.read(loop.settings))

    code, launches = loop([(1, 5, crash), (0, 5, look)])
    assert code == 0 and seen[0]["pid"] == os.getpid() and seen[0]["launcher"] is True
    assert lock_module.read(loop.settings) == {}  # given up at the end


# The lock: one Huragok per folder ---------------------------------------------------------------------------------

LOCKER = """
import json, sys, time
from pathlib import Path
from huragok.core import lock as lock_module
from huragok.core.settings import load
settings = load(sys.argv[1])
go = Path(sys.argv[2])
while not go.exists():
    time.sleep(0.005)
try:
    path = lock_module.take(settings)
except lock_module.AlreadyRunning as error:
    print("busy", error.pid)
    sys.exit(0)
print("took", json.dumps(json.loads(path.read_text(encoding="utf-8"))))
sys.stdout.flush()
time.sleep(float(sys.argv[3]))
lock_module.release(path)
print("released", json.dumps(lock_module.read(settings)))
"""


def _lockers(settings, count: int, hold: float, tmp_path: Path) -> list[str]:
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": str(REPO / "engine")}
    env.pop("HURAGOK_CONFIG", None)
    processes = [subprocess.Popen([sys.executable, "-c", LOCKER, str(settings.path), str(go), str(hold)],
                                  stdout=subprocess.PIPE, text=True, env=env) for _ in range(count)]
    import time

    time.sleep(1.5)  # every one of them waiting for the go
    go.write_text("", encoding="utf-8")
    return [p.communicate(timeout=60)[0] for p in processes]


@pytest.mark.slow
def test_starters_at_the_same_moment_never_both_win(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    settings = load(root / "settings.toml")
    outputs = _lockers(settings, 6, 1.0, tmp_path)
    assert sum(o.startswith("took") for o in outputs) == 1, outputs
    # The same with a lock left behind by a Huragok that's gone: one of them takes it over, only one.
    (settings.records / lock_module.LOCK_FILE).write_text(json.dumps({"pid": 999999, "create_time": 1.0}), encoding="utf-8")
    (tmp_path / "go").unlink()
    outputs = _lockers(settings, 6, 1.0, tmp_path)
    assert sum(o.startswith("took") for o in outputs) == 1, outputs
    assert not list(settings.records.glob("huragok.pid.*"))  # no temporary or stale copies left


def test_huragok_takes_the_lock_over_from_its_start_loop_and_hands_it_back(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    settings = load(root / "settings.toml")
    path = lock_module.take(settings, launcher=True)  # this process plays the start loop
    try:
        mine = json.loads(path.read_text(encoding="utf-8"))
        assert mine["pid"] == os.getpid() and mine["launcher"] is True
        (tmp_path / "go").write_text("", encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(REPO / "engine")}
        env.pop("HURAGOK_CONFIG", None)
        child = subprocess.run([sys.executable, "-c", LOCKER, str(settings.path), str(tmp_path / "go"), "0.2"],
                               capture_output=True, text=True, env=env, timeout=60)
        took, released = child.stdout.splitlines()
        held = json.loads(took.removeprefix("took "))
        assert held["pid"] != os.getpid() and held["launched_by"] == mine  # the child holds it, lent by the loop
        assert json.loads(released.removeprefix("released ")) == mine  # and gave it back
        assert lock_module.take(settings, launcher=True) == path  # the loop's own again
    finally:
        lock_module.release(path)
    assert not path.exists()


def test_a_0_2_1_window_still_open_counts_as_running(tmp_path: Path) -> None:
    import psutil

    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    settings = load(root / "settings.toml")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        settings.records.mkdir(exist_ok=True)
        (settings.records / lock_module.OLD_LOCK_FILE).write_text(json.dumps(
            {"pid": other.pid, "create_time": psutil.Process(other.pid).create_time(), "version": "0.2.1"}),
            encoding="utf-8")
        assert lock_module.running_supervisor(settings) == other.pid
        with pytest.raises(lock_module.AlreadyRunning):
            lock_module.take(settings)
    finally:
        other.kill()
        other.wait()
    assert lock_module.running_supervisor(settings) is None


# Putting the version before an update back -------------------------------------------------------------------------


def _program(folder: Path, version: str, marker: str) -> None:
    (folder / "engine" / "huragok").mkdir(parents=True, exist_ok=True)
    (folder / "engine" / "huragok" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (folder / "engine" / "requirements.txt").write_text(marker, encoding="utf-8")
    (folder / "start.cmd").write_text(f"rem {marker}\n", encoding="utf-8")


def test_the_files_the_update_replaced_are_put_back(tmp_path: Path) -> None:
    """As an update leaves them: the new version in place, the replaced files in records/previous-<V>/ and
    records/last-update.json naming that backup. The start loop puts them back with the updates module's own
    put_back."""
    from huragok.updates import engine as updates

    home = tmp_path / "reclaimer"
    _program(home, "0.2.3", "new")
    (home / "engine" / ".venv").mkdir()
    _program(home / "records" / "previous-0.2.2", "0.2.2", "old")
    (home / "records" / updates.LAST_UPDATE).write_text(json.dumps(
        {"version": "0.2.3", "replaced": "0.2.2", "backup": "previous-0.2.2", "added": []}), encoding="utf-8")
    with pytest.raises(launcher_module.RestoreError, match="is about 0.2.3, not 0.2.4"):
        launcher_module._restore_previous(home, "0.2.2", "0.2.4")
    assert launcher_module._restore_previous(home, "0.2.2", "0.2.3") == "0.2.2"
    assert (home / "engine" / "huragok" / "__init__.py").read_text(encoding="utf-8") == '__version__ = "0.2.2"\n'
    assert (home / "start.cmd").read_text(encoding="utf-8") == "rem old\n" and (home / "engine" / ".venv").is_dir()
    with pytest.raises(launcher_module.RestoreError, match="doesn't say which files the update replaced"):
        launcher_module._restore_previous(home, "0.2.2", "0.2.3")  # the pointer went with the put back


# --hold, -c, init ---------------------------------------------------------------------------------------------------


@pytest.fixture
def held(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from huragok.window import tui

    prompts: list[str] = []
    monkeypatch.setattr(tui, "is_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    return prompts


@pytest.mark.parametrize("body", ['[restarts]\ndaily_restart = "4am"', '[server]\nname = "LG Test Server"',
                                  'name = "unterminated'])
def test_hold_keeps_the_window_open_on_a_settings_problem(tmp_path: Path, held: list[str], capsys, body: str) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, [("LG Test Server", free_port())], body)
    assert cli.main(["-c", str(path), "start", "--hold"]) == common.SETTINGS_PROBLEM
    assert held == ["Press Enter to close this window."] and "Settings problem" in capsys.readouterr().err


def test_hold_keeps_the_window_open_when_the_start_fails(tmp_path: Path, held: list[str], capsys,
                                                        monkeypatch) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, [("LG Test Server", free_port())])

    def broken(*args: object, **kwargs: object) -> list[str]:
        raise PermissionError(13, "Access is denied", "commands.toml")

    monkeypatch.setattr(firstrun, "write_files", broken)
    assert cli.main(["-c", str(path), "start", "--hold"]) == common.FAILED
    err = capsys.readouterr().err
    assert held and "Access is denied" in err and "Huragok stopped with the error above" in err


def test_a_settings_problem_with_no_menu_is_looked_at_again(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, [("LG Test Server", free_port())], '[restarts]\ndaily_restart = "4am"')
    fixed: list[float] = []

    def pause(seconds: float, **kwargs: object) -> bool:
        fixed.append(seconds)
        path.write_text(path.read_text(encoding="utf-8").replace('"4am"', '"04:00"'), encoding="utf-8")
        return True

    monkeypatch.setattr(launcher_module, "_pause", pause)
    monkeypatch.setattr(launcher_module, "start_loop", lambda args, settings: 0)
    assert cli.main(["-c", str(path), "start", "--no-menu"]) == 0
    assert fixed == [launcher_module.SETTINGS_RETRY_SECONDS]
    assert "tries again in 5 minutes" in (root / launcher_module.LAUNCHER_LOG).read_text(encoding="utf-8")


def test_another_settings_file_is_the_one_written_and_read(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "LG Test Server", free_port(), exe=True)
    assert cli.main(["-c", str(root / "mine.toml"), "start"]) == common.SET_UP
    assert (root / "mine.toml").exists() and not (root / "settings.toml").exists()
    assert "start again: huragok -c mine.toml" in capsys.readouterr().out
    other = tmp_path / "other"
    other.mkdir()
    assert cli.main(["-c", str(other / "theirs.toml"), "init"]) == 0
    assert (other / "theirs.toml").exists() and not (other / "settings.toml").exists()
    assert load(other / "theirs.toml").servers == []


@pytest.mark.parametrize("name", ["new", "Bob's Server", "tab\\there"])
def test_init_with_a_full_path_writes_it_so_it_reads_back(tmp_path: Path, name: str) -> None:
    """A Windows path in a "basic string" was read with \\U, \\n or \\t as escapes (or not at all)."""
    if "\\" in name and os.name != "nt":
        name = name.replace("\\", "_")
    server = tmp_path / "Servers" / name
    write_server_folder(server, free_port(), exe=True)
    root = tmp_path / "reclaimer"
    root.mkdir()
    assert cli.main(["-c", str(root / "settings.toml"), "init", str(server)]) == 0
    assert load(root / "settings.toml").single_folder == server.resolve()


# Talking to a running Huragok, and what the commands say ------------------------------------------------------------


def test_say_to_a_server_that_isnt_there_says_so(tmp_path: Path, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("LG Test Server", free_port())])
    assert cli.main(["-c", str(path), "say", "Nobody", "hello"]) == common.SETTINGS_PROBLEM
    assert "No server named Nobody. Servers: LG Test Server" in capsys.readouterr().err


def _alive_lock(settings, **extra: object) -> subprocess.Popen:
    import psutil

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    settings.records.mkdir(exist_ok=True)
    (settings.records / lock_module.LOCK_FILE).write_text(json.dumps(
        {"pid": other.pid, "create_time": psutil.Process(other.pid).create_time(), "version": "0.2.2", **extra}),
        encoding="utf-8")
    return other


def test_status_trusts_the_lock_when_the_status_file_is_old(tmp_path: Path, capsys) -> None:
    import time

    path = write_root(tmp_path / "reclaimer", [("LG Test Server", free_port())])
    settings = load(path)
    other = _alive_lock(settings)
    try:
        (settings.records / "live.json").write_text(json.dumps({"updated": time.time() - 100, "servers": []}),
                                                    encoding="utf-8")
        assert cli.main(["-c", str(path), "status"]) == 0
        out = capsys.readouterr().out
        assert f"running (process {other.pid})" in out and "status file (records/live.json) is 100 s old" in out
        assert "isn't running" not in out
        (settings.records / lock_module.LOCK_FILE).write_text(json.dumps(
            {**json.loads((settings.records / lock_module.LOCK_FILE).read_text(encoding="utf-8")), "launcher": True}),
            encoding="utf-8")
        assert cli.main(["-c", str(path), "status"]) == 0
        assert "Huragok is starting again" in capsys.readouterr().out
    finally:
        other.kill()
        other.wait()
    assert cli.main(["-c", str(path), "status"]) == common.NOT_RUNNING


def test_watch_follows_a_server_named_like_a_log_word(tmp_path: Path, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", list(zip(["Vergil", "Second"], free_ports(2))))
    settings = load(path)
    settings.logs.mkdir(exist_ok=True)
    (settings.logs / "console-vergil.log").write_text("the server's own line\n", encoding="utf-8")
    assert cli.main(["-c", str(path), "watch", "Vergil", "--once"]) == 0
    assert "console-vergil.log" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "watch", "1", "--once"]) == 0  # by its number in the list
    assert "Second" in capsys.readouterr().out
    assert cli.main(["-c", str(path), "watch", "Nobody", "--once"]) == common.SETTINGS_PROBLEM


def test_stop_a_server_with_nothing_running_doesnt_say_it_stopped_it(tmp_path: Path, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("LG Test Server", free_port())], backend="process", exe=True)
    assert cli.main(["-c", str(path), "stop", "LG Test Server"]) == 0
    out = capsys.readouterr().out
    assert "Stopped" not in out and "Not running (or not started by Huragok" in out and "LG Test Server" in out
    assert cli.main(["-c", str(path), "stop", "Nobody"]) == common.SETTINGS_PROBLEM


@pytest.mark.parametrize("verb, argv, wait", [
    ("rcon", ["rcon", "-s", "lg-test-server", "players"], cli.RCON_WAIT),
    ("say", ["say", "all", "hello"], cli.SAY_WAIT * 2 + cli.ASK_WAIT),
    ("say", ["say", "1", "hello"], cli.SAY_WAIT + cli.ASK_WAIT),
])
def test_commands_wait_as_long_as_the_work_takes(tmp_path: Path, monkeypatch, verb: str, argv: list[str],
                                                 wait: float) -> None:
    path = write_root(tmp_path / "reclaimer", list(zip(["LG Test Server", "Second"], free_ports(2))))
    asked: list[tuple] = []

    def send(records, sent_verb, server="all", wait=15.0, **options):  # type: ignore[no-untyped-def]
        asked.append((sent_verb, server, wait))
        return cli.mailbox.Reply(True, "ok")

    monkeypatch.setattr(lock_module, "running_supervisor", lambda settings: 4242)
    monkeypatch.setattr(cli.mailbox, "send", send)
    assert cli.main(["-c", str(path), *argv]) == 0
    assert asked[0][0] == verb and asked[0][2] == wait
    if verb == "rcon":
        assert asked[0][1] == "LG Test Server"  # -s by its file name, resolved here


def test_an_update_waits_for_the_download_and_the_packages(tmp_path: Path, monkeypatch) -> None:
    from huragok.updates import engine as updates

    path = write_root(tmp_path / "reclaimer", [("LG Test Server", free_port())])
    release = updates.Release("9.9.9", "page", "notes", "url")
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: release)
    monkeypatch.setattr(updates, "installed_here", lambda home: True)
    monkeypatch.setattr(lock_module, "running_supervisor", lambda settings: 4242)
    asked: list[float] = []
    monkeypatch.setattr(cli.mailbox, "send", lambda records, verb, server="all", wait=15.0, **o:
                        asked.append(wait) or cli.mailbox.Reply(False, "no"))
    assert cli.main(["-c", str(path), "update", "--yes"]) == 1
    assert asked == [cli.UPDATE_WAIT] and cli.UPDATE_WAIT >= 600


# check ---------------------------------------------------------------------------------------------------------------


def _checkable(root: Path, body: str = "", **options: object) -> Path:
    """A root `check` can pass: with Vergil's word lists in place, as the first start writes them."""
    path = write_root(root, [("LG Test Server", free_port())], body, **options)  # type: ignore[arg-type]
    for name in ("automod.txt", "names.txt"):
        (root / name).write_text("", encoding="utf-8")
    return path


def _code(argv: list[str]) -> int:
    """cli.main's exit code, also when it ends with SystemExit (a settings problem)."""
    try:
        return cli.main(argv)
    except SystemExit as stop:
        return int(stop.code or 0)


def test_check_names_settings_that_do_nothing_and_old_values(tmp_path: Path, capsys) -> None:
    path = _checkable(tmp_path / "reclaimer", '[relay]\nenable = true\n[watchdog]\nmode = "watch"\nslowframes = "act"\n')
    assert cli.main(["-c", str(path), "check"]) == 0
    out = capsys.readouterr().out
    assert "Settings that do nothing" in out and "[relay] enable isn't a setting" in out
    assert "(did you mean slow_frames?)" in out and "isn't a watchdog setting" not in out  # named once
    assert "Values from an older version" in out and 'mode = "watch" is what 0.1 and 0.2.0 wrote' in out
    assert "Ready, but read the settings warnings above" in out and "Everything looks ready" not in out


def test_check_says_whether_each_mod_is_good_to_go(tmp_path: Path, capsys) -> None:
    from testkit.mods import a_map, a_mod

    path = _checkable(tmp_path / "reclaimer")
    content = load(path).servers[0].content_dir
    assert content is not None
    a_mod(content / "Mods" / "canyon", {"canyon": a_map(9002)}, title="Canyon")
    a_mod(content / "Mods" / "reach", {"golf": a_map(9003)}, title="Reach Pack", engine="HaloReach")
    assert cli.main(["-c", str(path), "check"]) == 0  # a broken mod leaves the mod out, not the server
    out = capsys.readouterr().out
    assert "Mods (LG Test Server: " in out and "Canyon: good to go · 1 map" in out
    assert "Reach Pack: 1 problem" in out
    assert "broken: it is made for HaloReach, not Halo 3: the server ignores it without a word" in out


def test_check_runs_each_parts_own_check(tmp_path: Path, capsys) -> None:
    path = _checkable(tmp_path / "reclaimer", '[automod]\nexempt_roles = ["admins"]')
    assert cli.main(["-c", str(path), "check"]) == 1
    out = capsys.readouterr().out
    assert "PROBLEM  [automod] exempt_roles: 'admins' isn't a role (automod wouldn't start)" in out


def test_check_judges_a_command_that_starts_with_the_executable(tmp_path: Path, capsys) -> None:
    path = _checkable(tmp_path / "reclaimer", '[process]\ncommand = ["{executable}", "dedicated", "run", "--config", '
                                              '"{config}"]', backend="process", exe=True)
    cli.main(["-c", str(path), "check"])
    out = capsys.readouterr().out
    assert "was not found" not in out and "command starts with {executable}" not in out  # the program is there
    (tmp_path / "reclaimer" / "SERVERS" / "LG Test Server" / "project-reclaimer.exe").unlink()
    cli.main(["-c", str(path), "check"])
    assert "[process] command starts with {executable}, but project-reclaimer.exe is not in" in \
        capsys.readouterr().out


@pytest.mark.parametrize("version, label, named", [
    ("0.9.18", "Halo 3", "names 'Halo 3', which Project Reclaimer 0.9.18 (this server's program) calls otherwise"),
    ("0.9.18", "Vanilla characters", None),
    ("0.9.20", "Vanilla characters", "names 'Vanilla characters', which Project Reclaimer 0.9.20"),
    ("0.9.20", "Halo 3", None),
    (None, "Halo 3", None),  # its version not known: only labels no version takes are named
    (None, "On with firebombs", "names 'On with firebombs', which no version of Project Reclaimer takes"),
])
def test_check_judges_playlist_labels_by_the_servers_own_program(tmp_path: Path, capsys, monkeypatch, version,
                                                                label: str, named: str | None) -> None:
    from huragok.program import releases as serverupdates

    root = tmp_path / "reclaimer"
    path = _checkable(root, exe=True)
    playlists = root / "SERVERS" / "LG Test Server" / "playlists"
    playlists.mkdir()
    option = "player.grenade_regeneration" if "firebombs" in label else "player.character_set"
    (playlists / "p.playlist.json").write_text(json.dumps({"name": "p", "games": [
        {"map": "Guardian", "game": "Slayer", "options": {option: label}}]}), encoding="utf-8")
    asked: list[Path] = []
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, program: asked.append(program) or version)
    cli.main(["-c", str(path), "check"])
    out = " ".join(capsys.readouterr().out.split())
    assert asked and asked[0].name == "project-reclaimer.exe"
    if named:
        assert named in out
    else:
        assert "p.playlist.json names" not in out


def test_text_from_servers_reaches_no_terminal_with_control_characters(capsys) -> None:
    """Without colour (a pipe, a file), text was printed as it came: a player's name or a console line carrying
    ESC sequences ran in the terminal reading it."""
    cli.out.write("Bob\x1b]52;c;aGk=\x07 said\thi\x9b2J")
    assert capsys.readouterr().out == "Bob?]52;c;aGk=? said hi?2J\n"


def test_check_with_a_wrong_backend_value_says_so(tmp_path: Path, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("LG Test Server", free_port())], '[process]\nstop_timeout = "30"',
                      backend="process")
    for command in ("check", "stop", "start"):
        assert _code(["-c", str(path), command]) == common.SETTINGS_PROBLEM
        assert 'process.stop_timeout must be a number, not "30"' in capsys.readouterr().err


def test_help_lists_the_exit_codes() -> None:
    text = cli.build_parser().format_help()
    assert "Exit codes: 0 done" in text and f"{common.NOT_RUNNING} status: not running" in text
    assert "none named: every server that's running" in " ".join(text.split())


# Typed commands (--no-menu) -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed, passed", [
    ('stop "Big Team Battle" --now', "stop Big Team Battle --now"),
    ("say 'Big Team Battle' it's Bob's turn", "say big-team-battle it's Bob's turn"),
    ('say "Slayer" "quoted" text', 'say Slayer "quoted" text'),
    ("say all don't", "say all don't"),
    ("  status  ", "status"),
    ('update "x"', 'update "x"'),
])
def test_typed_commands_take_a_quoted_server_name(typed: str, passed: str) -> None:
    from huragok.commandline.console import tidy

    assert tidy(typed) == passed


def test_typed_text_is_read_in_the_consoles_code_page(monkeypatch) -> None:
    from huragok.commandline import console

    if os.name != "nt":
        assert console.decode("café".encode("utf-8")) == "café"
    else:
        import ctypes

        monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleCP", lambda: 850)
        assert console.decode("café".encode("cp850")) == "café"
        monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleCP", lambda: 65001)
        assert console.decode("café".encode("utf-8")) == "café"
