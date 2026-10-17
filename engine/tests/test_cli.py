"""The root folder with SERVERS/: the first start, finding things, one supervisor at a time, the commands."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import free_port, write_root, write_server_folder

from reclaimer_supervisor import cli
from reclaimer_supervisor.config import load


def test_the_first_start_writes_the_files_and_stops(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "LG Test Server", 49176, exe=True)
    write_server_folder(root / "SERVERS" / "Second", 49178, name="Second", exe=True)
    assert cli.main(["-c", str(root / "settings.toml"), "start"]) == cli.SET_UP
    assert {p.name for p in root.iterdir()} == {"settings.toml", "commands.toml", "automod.txt", "SERVERS", "PACKAGES"}
    assert (root / "PACKAGES" / "team-slayer" / "playlists" / "team-slayer.playlist.json").is_file()
    out = capsys.readouterr().out
    assert "Put your player ID under [owner]" in out and "start again" in out and "2 server folder(s) found" in out
    settings = load(root / "settings.toml")  # works as written
    assert [s.name for s in settings.servers] == ["LG Test Server", "Second"]
    assert settings.servers_folder == (root / "SERVERS").resolve() and settings.single_folder is None
    assert settings.servers[0].folder == (root / "SERVERS" / "LG Test Server").resolve()
    assert settings.backend == "process"
    assert settings.database == root.resolve() / "records" / "supervisor.sqlite3"
    assert settings.parts["automod"].enabled and settings.parts["automod"].dry_run
    text = (root / "settings.toml").read_text(encoding="utf-8")
    assert 'servers_folder = "SERVERS"' in text and 'server_folder = ""' in text
    assert "{executable}" in text and "{{" not in text  # the documented placeholders survive as written
    if os.name == "nt":  # on Linux `check` also wants wine for backend process
        assert cli.main(["-c", str(root / "settings.toml"), "check"]) == 0
        assert "nobody is owner yet" in capsys.readouterr().out


def test_the_first_start_with_no_servers_makes_the_folder_and_explains(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    root.mkdir()
    assert cli.main(["-c", str(root / "settings.toml"), "start"]) == cli.SET_UP
    out = capsys.readouterr().out
    assert (root / "SERVERS").is_dir() and "no server folders yet" in out and "dedicated init" in out
    settings = load(root / "settings.toml")
    assert settings.servers == []
    assert cli.main(["-c", str(root / "settings.toml"), "check"]) == 1
    assert "none found" in capsys.readouterr().out


def test_the_old_drop_in_layout_still_works(tmp_path: Path) -> None:
    """0.1 put the supervisor's folder inside the one server folder."""
    write_server_folder(tmp_path, 49176, exe=True)
    home = tmp_path / "reclaimer-supervisor"
    home.mkdir()
    assert cli.main(["-c", str(home / "settings.toml"), "start"]) == cli.SET_UP
    settings = load(home / "settings.toml")
    assert settings.single_folder == tmp_path.resolve() and settings.servers[0].name == "LG Test Server"
    assert 'server_folder = ".."' in (home / "settings.toml").read_text(encoding="utf-8")
    assert cli.detect_layout(home)[1] == ".."
    write_server_folder(home, 49177, name="Here", exe=True)  # unzipped into a server folder itself
    assert cli.detect_layout(home)[1] == "."


def test_init_never_replaces_your_files_unless_told(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", 49176)])
    (root / "settings.toml").unlink()
    cli.write_files(root)
    (root / "automod.txt").write_text("badword\n", encoding="utf-8")
    (root / "commands.toml").write_text("[off]\ncommands = []\n", encoding="utf-8")
    assert cli.main(["-c", str(root / "settings.toml"), "init"]) == 1
    cli.main(["-c", str(root / "settings.toml"), "init", "--force"])
    assert (root / "automod.txt").read_text(encoding="utf-8") == "badword\n"  # the word list is never replaced
    assert "[moderator]" in (root / "commands.toml").read_text(encoding="utf-8")


def test_the_backend_follows_what_the_server_folders_have(tmp_path: Path) -> None:
    assert cli.pick_backend([])[0] == "process"
    folder = tmp_path / "server"
    folder.mkdir()
    assert cli.pick_backend([folder])[0] == "process"
    (folder / "project-reclaimer-dedicated-v0.9.18.exe").write_bytes(b"")
    assert cli.pick_backend([folder])[:2] == ("process", {"executable": "project-reclaimer-dedicated-v0.9.18.exe"})
    (folder / "project-reclaimer.exe").write_bytes(b"")
    assert cli.pick_backend([folder])[1] == {"executable": "project-reclaimer.exe"}
    (folder / "compose.yaml").write_text("services: {}", encoding="utf-8")
    assert cli.pick_backend([folder])[:2] == ("docker", {"compose_file": "compose.yaml"})


def test_only_one_supervisor_runs_from_a_folder(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", 49176)])
    settings = load(root / "settings.toml")
    assert cli.running_supervisor(settings) is None
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        import psutil

        settings.records.mkdir()
        (settings.records / cli.LOCK_FILE).write_text(json.dumps(
            {"pid": other.pid, "create_time": psutil.Process(other.pid).create_time()}), encoding="utf-8")
        assert cli.running_supervisor(settings) == other.pid
        with pytest.raises(cli.AlreadyRunning):
            cli._take_lock(settings)
        assert cli.main(["-c", str(root / "settings.toml"), "start", "--no-menu"]) == 1
        assert "already running" in capsys.readouterr().err
    finally:
        other.kill()
        other.wait()
    assert cli.running_supervisor(settings) is None  # a lock left by a process that's gone doesn't count
    lock = cli._take_lock(settings)  # and is taken over
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == os.getpid()
    lock.unlink()


def test_stop_and_status_with_nothing_running(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("LG Test Server", free_port())])
    assert cli.main(["-c", str(root / "settings.toml"), "stop"]) == 1
    assert "doesn't stop the servers" in capsys.readouterr().err
    assert cli.main(["-c", str(root / "settings.toml"), "status"]) == 0
    out = capsys.readouterr().out
    assert "No supervisor is running" in out and "LG Test Server" in out and "stopped" in out
    assert cli.main(["-c", str(root / "settings.toml"), "restart", "LG Test Server"]) == 1
    assert "needs one" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "nt", reason="start.cmd is for Windows")
def test_start_cmd_passes_commands_through(tmp_path: Path) -> None:
    """The real script, with a stand-in engine/.venv so nothing is downloaded."""
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "LG Test Server", 49176, exe=True)
    engine = root / "engine"
    shutil.copytree(repo / "engine" / "reclaimer_supervisor", engine / "reclaimer_supervisor",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (engine / ".venv").mkdir()
    for folder in (engine, engine / ".venv"):  # as if the packages were installed already
        (folder / "requirements.txt").write_text("websockets>=13\n", encoding="utf-8")
    # A venv's python.exe can't be copied on its own; point the script at this Python instead.
    script = (repo / "start.cmd").read_text(encoding="utf-8").replace(
        'set "PYTHON=%~dp0engine\\.venv\\Scripts\\python.exe"', f'set "PYTHON={sys.executable}"')
    (root / "start.cmd").write_text(script, encoding="utf-8", newline="\r\n")
    result = subprocess.run(["cmd", "/c", str(root / "start.cmd"), "init"], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Next: start.cmd check" in result.stdout
    assert (root / "settings.toml").exists() and (root / "commands.toml").exists()
