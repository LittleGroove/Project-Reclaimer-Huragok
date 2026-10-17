"""New versions from GitHub: found, announced, installed only when asked, and never touching the host's files."""

from __future__ import annotations

import asyncio
import json
import logging
import zipfile
from pathlib import Path

import pytest
from conftest import staff, wait_for, write_server_folder, write_settings
from fake_rcon import FakeRcon

from reclaimer_supervisor import __version__, cli, updates
from reclaimer_supervisor.config import ConfigError, load
from reclaimer_supervisor import mailbox
from reclaimer_supervisor.core import Supervisor
from reclaimer_supervisor.updates import Release, UpdateError, UpdateService, install, is_newer, parse_version

LATEST_RELEASE = updates.latest_release  # the real one: conftest swaps it out so tests stay off the network
NEW = "99.0.0"
RELEASE = Release(NEW, "https://example.invalid/releases/v99.0.0", "## What's new\n- Better things\n- More things",
                  "https://example.invalid/reclaimer-supervisor-99.0.0.zip")


def make_release_zip(path: Path, version: str = NEW, top: str = "reclaimer-supervisor") -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"{top}/engine/reclaimer_supervisor/__init__.py", f'__version__ = "{version}"\n')
        bundle.writestr(f"{top}/engine/reclaimer_supervisor/new_module.py", "")
        bundle.writestr(f"{top}/engine/requirements.txt", "websockets>=13\n")
        bundle.writestr(f"{top}/start.cmd", "new start.cmd")
        bundle.writestr(f"{top}/start.sh", "new start.sh")
        bundle.writestr(f"{top}/README.md", "new readme")
        bundle.writestr(f"{top}/docs/automod.md", "new docs")
    return path


def make_install(home: Path) -> Path:
    """A supervisor folder as start.cmd leaves it, with the host's own files."""
    (home / "engine" / "reclaimer_supervisor").mkdir(parents=True)
    (home / "engine" / "reclaimer_supervisor" / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (home / "engine" / "reclaimer_supervisor" / "old_module.py").write_text("", encoding="utf-8")
    (home / "engine" / ".venv").mkdir()
    (home / "engine" / ".venv" / "marker").write_text("installed packages", encoding="utf-8")
    (home / "docs").mkdir()
    (home / "docs" / "old.md").write_text("old docs", encoding="utf-8")
    for name, text in {"start.cmd": "old", "start.sh": "old", "README.md": "old", "settings.toml": "mine",
                       "commands.toml": "mine", "automod.txt": "my words"}.items():
        (home / name).write_text(text, encoding="utf-8")
    (home / "records").mkdir()
    (home / "records" / "supervisor.sqlite3").write_text("my records", encoding="utf-8")
    return home


def test_versions() -> None:
    assert parse_version("v0.10.2") == (0, 10, 2) and parse_version("1.2") == (1, 2)
    assert parse_version("1.0.0-beta") == (1, 0, 0)
    assert is_newer(RELEASE, "0.1.0") and not is_newer(Release("0.1.0", "", "", ""), "0.1.0")
    assert not is_newer(Release("0.9.0", "", "", ""), "0.10.0")  # numbers, not text
    with pytest.raises(UpdateError):
        parse_version("latest")


def test_reading_githubs_answer(monkeypatch) -> None:
    answer = {"tag_name": "v1.2.3", "html_url": "https://github.com/x/y/releases/tag/v1.2.3", "body": "notes",
              "zipball_url": "https://api.github.com/zipball",
              "assets": [{"name": "SHA256SUMS", "browser_download_url": "a"},
                         {"name": "reclaimer-supervisor-1.2.3.zip", "browser_download_url": "https://x/r.zip",
                          "digest": "sha256:abc"}]}
    monkeypatch.setattr(updates, "_get", lambda url, timeout, accept="": json.dumps(answer).encode())
    found = LATEST_RELEASE("x/y")
    assert (found.version, found.download, found.digest) == ("1.2.3", "https://x/r.zip", "sha256:abc")
    answer["assets"] = []
    assert LATEST_RELEASE("x/y").download == "https://api.github.com/zipball"  # GitHub's own zip


def test_installing_replaces_the_program_and_nothing_of_yours(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    assert install(make_release_zip(tmp_path / "r.zip"), home, NEW) == NEW
    engine = home / "engine" / "reclaimer_supervisor"
    assert (engine / "new_module.py").exists() and not (engine / "old_module.py").exists()
    assert (home / "engine" / ".venv" / "marker").read_text(encoding="utf-8") == "installed packages"
    assert (home / "start.cmd").read_text(encoding="utf-8") == "new start.cmd"
    assert (home / "docs" / "automod.md").exists() and not (home / "docs" / "old.md").exists()
    for name, text in {"settings.toml": "mine", "commands.toml": "mine", "automod.txt": "my words",
                       "records/supervisor.sqlite3": "my records"}.items():
        assert (home / name).read_text(encoding="utf-8") == text, name
    backup = home / "records" / f"previous-{__version__}"
    assert (backup / "engine" / "reclaimer_supervisor" / "old_module.py").exists()
    assert (backup / "start.cmd").read_text(encoding="utf-8") == "old"
    assert not (home / "records" / "update").exists()


def test_githubs_own_zip_works_too(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    zipball = make_release_zip(tmp_path / "r.zip", top="LittleGroove-Project-Reclaimer-Server-Supervisor-1a2b3c4")
    assert install(zipball, home) == NEW


def test_a_download_that_isnt_what_it_says_changes_nothing(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    with pytest.raises(UpdateError, match="says it's version 1.0.0"):
        install(make_release_zip(tmp_path / "r.zip", version="1.0.0"), home, NEW)
    (tmp_path / "bad.zip").write_text("not a zip", encoding="utf-8")
    with pytest.raises(UpdateError, match="isn't a zip"):
        install(tmp_path / "bad.zip", home)
    assert (home / "start.cmd").read_text(encoding="utf-8") == "old"
    assert (home / "engine" / "reclaimer_supervisor" / "old_module.py").exists()


def test_a_checksum_mismatch_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(updates, "_get", lambda url, timeout, accept="": b"zip bytes")
    with pytest.raises(UpdateError, match="checksum"):
        updates.download(Release(NEW, "", "", "https://x/r.zip", "sha256:" + "0" * 64), tmp_path)


def test_settings(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", 49176)
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    assert settings.updates.check and not settings.updates.install_on_server_restart
    assert settings.updates.repository == "LittleGroove/Project-Reclaimer-Server-Supervisor"
    with pytest.raises(ConfigError, match="owner/name"):
        load(write_settings(tmp_path, tmp_path / "server", '[updates]\nrepository = "https://github.com/x"'))


def service(tmp_path: Path, monkeypatch, body: str = "") -> tuple[UpdateService, list[str], list[str]]:
    write_server_folder(tmp_path / "server", 49176)
    settings = load(write_settings(tmp_path, tmp_path / "server", body))
    installed: list[str] = []
    relaunched: list[str] = []
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
    monkeypatch.setattr(updates, "update", lambda home, release: installed.append(release.version) or release.version)
    from reclaimer_supervisor.events import EventBus

    svc = UpdateService(settings, EventBus(), lambda: relaunched.append("yes"))
    svc.can_install = True
    return svc, installed, relaunched


def test_a_new_version_is_announced_once_and_not_installed(tmp_path: Path, monkeypatch, caplog, run) -> None:
    svc, installed, _ = service(tmp_path, monkeypatch)
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        await svc.check()
        await svc.check()

    run(scenario())
    said = [r.getMessage() for r in caplog.records]
    assert sum("Update available: Reclaimer Supervisor 99.0.0" in m for m in said) == 1
    assert any("type update here" in m for m in said) and any("- Better things" in m for m in said)
    assert svc.pending == RELEASE and installed == []


def test_installing_when_asked_then_starting_again(tmp_path: Path, monkeypatch, run) -> None:
    svc, installed, relaunched = service(tmp_path, monkeypatch)
    assert run(svc.install("asked in this window")) and installed == [NEW] and relaunched == ["yes"]


def test_a_server_restart_installs_it_only_when_set_to(tmp_path: Path, monkeypatch, run) -> None:
    from reclaimer_supervisor.events import Event

    async def scenario(svc: UpdateService) -> None:
        await svc.check()
        svc._on_server_restart(Event("server.restarted", "LG Test Server", {}))
        await asyncio.sleep(0.05)

    svc, installed, _ = service(tmp_path, monkeypatch)
    run(scenario(svc))
    assert installed == []
    svc, installed, relaunched = service(tmp_path / "auto", monkeypatch, "[updates]\ninstall_on_server_restart = true")
    run(scenario(svc))
    assert installed == [NEW] and relaunched == ["yes"]


def test_a_copy_not_run_from_its_folder_wont_update_itself(tmp_path: Path, monkeypatch, run) -> None:
    svc, installed, _ = service(tmp_path, monkeypatch)
    svc.can_install = False
    assert not run(svc.install("asked")) and installed == []


def test_update_asked_from_outside_restarts_with_the_servers_left_running(tmp_path: Path, monkeypatch, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            write_server_folder(tmp_path / "server", fake.port)
            supervisor = Supervisor(load(write_settings(tmp_path, tmp_path / "server", commands=staff())))
            monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
            monkeypatch.setattr(updates, "update", lambda home, release: release.version)
            supervisor.updates.can_install = True
            task = asyncio.ensure_future(supervisor.run())
            await wait_for(lambda: supervisor.states["LG Test Server"].connected)
            mailbox.post(tmp_path / "records", "update", why="a test")
            await asyncio.wait_for(task, 10)
            assert supervisor.relaunch and supervisor.stop_servers is False

    run(scenario())


def test_update_command_checks_and_asks(tmp_path: Path, monkeypatch, capsys) -> None:
    write_server_folder(tmp_path, 49176)
    home = tmp_path / "reclaimer-supervisor"
    home.mkdir()
    cli.write_files(home)
    config = str(home / "settings.toml")
    assert cli.main(["-c", config, "update"]) == 0
    assert f"{__version__} is the newest" in capsys.readouterr().out
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
    assert cli.main(["-c", config, "update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "Update available: Reclaimer Supervisor 99.0.0" in out and "- Better things" in out
    monkeypatch.setattr(updates, "installed_here", lambda home: True)
    assert cli.main(["-c", config, "update"]) == 1  # nobody at a console to say yes
    assert "update --yes" in capsys.readouterr().out
    assert "99.0.0 is out" in (cli._update_note(load(home / "settings.toml")))
