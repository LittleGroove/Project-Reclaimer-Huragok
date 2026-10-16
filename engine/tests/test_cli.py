"""Dropping the supervisor's folder into a server folder: the first start, finding things, one at a time."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import write_server_folder

from reclaimer_supervisor import cli
from reclaimer_supervisor.config import load


def drop_in(tmp_path: Path) -> Path:
    """A server folder with the supervisor's folder inside it, as a host would set it up."""
    write_server_folder(tmp_path, 49176)
    home = tmp_path / "reclaimer-supervisor"
    home.mkdir()
    return home


def test_the_first_start_writes_the_three_files_and_stops(tmp_path: Path, capsys) -> None:
    home = drop_in(tmp_path)
    assert cli.main(["-c", str(home / "settings.toml"), "start"]) == cli.SET_UP
    assert {p.name for p in home.iterdir()} == {"settings.toml", "commands.toml", "automod.txt"}
    out = capsys.readouterr().out
    assert "Put your player ID under [owner]" in out and "start again" in out
    settings = load(home / "settings.toml")  # works as written
    assert settings.server_folder == tmp_path.resolve() and settings.servers[0].name == "LG Test Server"
    assert 'server_folder = ".."' in (home / "settings.toml").read_text(encoding="utf-8")
    assert settings.database == home.resolve() / "records" / "supervisor.sqlite3"
    assert settings.parts["automod"].enabled and settings.parts["automod"].dry_run
    assert cli.main(["-c", str(home / "settings.toml"), "check"]) == 0
    assert "nobody is owner yet" in capsys.readouterr().out


def test_init_never_replaces_your_files_unless_told(tmp_path: Path) -> None:
    home = drop_in(tmp_path)
    cli.write_files(home)
    (home / "automod.txt").write_text("badword\n", encoding="utf-8")
    (home / "commands.toml").write_text("[off]\ncommands = []\n", encoding="utf-8")
    assert cli.main(["-c", str(home / "settings.toml"), "init"]) == 1
    cli.main(["-c", str(home / "settings.toml"), "init", "--force"])
    assert (home / "automod.txt").read_text(encoding="utf-8") == "badword\n"  # the word list is never replaced
    assert "[moderator]" in (home / "commands.toml").read_text(encoding="utf-8")


def test_the_backend_follows_what_the_server_folder_has(tmp_path: Path, monkeypatch) -> None:
    assert cli.pick_backend(None)[0] == "attach"
    folder = tmp_path / "server"
    folder.mkdir()
    monkeypatch.setattr(os, "name", "nt")
    assert cli.pick_backend(folder)[0] == "attach"
    (folder / "project-reclaimer-dedicated-v0.9.18.exe").write_bytes(b"")
    assert cli.pick_backend(folder)[:2] == ("windows", {"executable": "project-reclaimer-dedicated-v0.9.18.exe"})
    (folder / "project-reclaimer.exe").write_bytes(b"")
    assert cli.pick_backend(folder)[1] == {"executable": "project-reclaimer.exe"}
    monkeypatch.setattr(os, "name", "posix")
    assert cli.pick_backend(folder)[0] == "attach"
    (folder / "compose.yaml").write_text("services: {}", encoding="utf-8")
    assert cli.pick_backend(folder)[:2] == ("docker", {"compose_file": "compose.yaml"})


def test_finding_the_server_folder(tmp_path: Path) -> None:
    home = drop_in(tmp_path)
    assert cli.find_server_folder(home) == tmp_path.resolve()  # the folder it sits in
    write_server_folder(home, 49176)
    assert cli.find_server_folder(home) == tmp_path.resolve()  # that one first
    assert cli.find_server_folder(tmp_path / "x", str(home)) == home.resolve()  # or the one given
    assert cli.find_server_folder(tmp_path / "elsewhere" / "x") is None


def test_only_one_supervisor_runs_from_a_folder(tmp_path: Path, capsys) -> None:
    home = drop_in(tmp_path)
    cli.write_files(home)
    settings = load(home / "settings.toml")
    assert cli.running_supervisor(settings) is None
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        import psutil

        settings.records.mkdir()
        (settings.records / cli.LOCK_FILE).write_text(json.dumps(
            {"pid": other.pid, "create_time": psutil.Process(other.pid).create_time()}), encoding="utf-8")
        assert cli.running_supervisor(settings) == other.pid
        assert cli.main(["-c", str(home / "settings.toml"), "start"]) == 1
        assert "already running" in capsys.readouterr().err
    finally:
        other.kill()
        other.wait()
    assert cli.running_supervisor(settings) is None  # a lock left by a process that's gone doesn't count


def test_stop_with_nothing_running(tmp_path: Path, capsys) -> None:
    home = drop_in(tmp_path)
    cli.write_files(home, backend="attach")
    assert cli.main(["-c", str(home / "settings.toml"), "stop"]) == 1
    assert "stop them the way they were started" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="start.cmd is for Windows")
def test_start_cmd_passes_commands_through(tmp_path: Path) -> None:
    """The real script, with a stand-in engine/.venv so nothing is downloaded."""
    root = Path(__file__).resolve().parents[2]
    home = drop_in(tmp_path)
    engine = home / "engine"
    shutil.copytree(root / "engine" / "reclaimer_supervisor", engine / "reclaimer_supervisor",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (engine / ".venv").mkdir()
    for folder in (engine, engine / ".venv"):  # as if the packages were installed already
        (folder / "requirements.txt").write_text("websockets>=13\n", encoding="utf-8")
    # A venv's python.exe can't be copied on its own; point the script at this Python instead.
    script = (root / "start.cmd").read_text(encoding="utf-8").replace(
        'set "PYTHON=%~dp0engine\\.venv\\Scripts\\python.exe"', f'set "PYTHON={sys.executable}"')
    (home / "start.cmd").write_text(script, encoding="utf-8", newline="\r\n")
    result = subprocess.run(["cmd", "/c", str(home / "start.cmd"), "init"], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Next: start.cmd check" in result.stdout
    assert (home / "settings.toml").exists() and (home / "commands.toml").exists()
