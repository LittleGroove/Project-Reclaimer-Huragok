"""New versions from GitHub: found, announced, installed only when asked, and never touching the host's files."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import free_port, staff, wait_for, write_server_folder, write_settings
from testkit.fake_rcon import FakeRcon
from testkit.releases import NEW, RELEASE, make_install, make_release_zip

from huragok import __version__
from huragok.commandline import cli, firstrun
from huragok.commandline import check as check_module
from huragok.core import mailbox
from huragok.core.settings import load
from huragok.core.tables import ConfigError
from huragok.core.web import UpdateError, is_prerelease, parse_version, version_key
from huragok.huragok import Huragok
from huragok.updates import engine as updates
from huragok.updates import program as program_updates_module
from huragok.updates.engine import Release, UpdateService, install, is_newer

LATEST_RELEASE = updates.latest_release  # the real one: conftest swaps it out so tests stay off the network


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
                         {"name": "huragok-1.2.3.zip", "browser_download_url": "https://x/r.zip",
                          "digest": "sha256:abc"}]}
    monkeypatch.setattr(updates, "fetch", lambda url, timeout, accept="": json.dumps(answer).encode())
    found = LATEST_RELEASE("x/y")
    assert (found.version, found.download, found.digest) == ("1.2.3", "https://x/r.zip", "sha256:abc")
    answer["assets"] = []
    assert LATEST_RELEASE("x/y").download == "https://api.github.com/zipball"  # GitHub's own zip


def test_a_beta_comes_before_its_release_however_its_written() -> None:
    order = ["0.4.9", "0.5.0-dev", "0.5.0a1", "0.5.0-beta.1", "0.5.0b2", "0.5.0-beta.10", "v0.5.0-rc.1", "0.5.0",
             "0.5.1-beta.1", "0.5.1", "0.10.0-beta"]
    assert sorted(order, key=version_key) == order
    assert version_key("0.5.0-beta.2") == version_key("0.5.0b2") == version_key("0.5.0.beta2") \
        == version_key("0.5.0-BETA-2")  # one version, in __init__.py and in its tag
    assert version_key("0.5.0+build.7") == version_key("0.5.0")  # build data never orders
    assert is_prerelease("0.5.1-beta.1") and is_prerelease("0.5.1b1") and not is_prerelease("v0.5.1")
    assert is_newer(Release("0.5.0", "", "", ""), "0.5.0-beta.3")  # a beta tester gets the full release
    assert not is_newer(Release("0.5.0", "", "", ""), "0.5.1-beta.1")  # and isn't taken back to it by beta = false
    assert not is_newer(Release("0.5.0-beta.3", "", "", ""), "0.5.0")


def test_beta_takes_the_newest_release_betas_and_all_and_only_then(monkeypatch) -> None:
    """[updates] beta = false asks for /releases/latest, which never names a pre-release (every version since 0.4.0
    asks the same, so a beta reaches only hosts who turn it on). With beta, the highest version among the releases,
    drafts and tags that aren't versions passed over, so a full release passes the betas before it."""
    def release(tag: str, **more: Any) -> dict[str, Any]:
        return {"tag_name": tag, "html_url": f"https://github.com/x/y/releases/tag/{tag}", "body": "",
                "assets": [{"name": f"huragok-{tag}.zip", "browser_download_url": f"https://x/{tag}.zip"}], **more}

    asked: list[str] = []
    listed = [release("v0.5.1-beta.2", prerelease=True), release("v0.6.0", draft=True), release("nightly"),
              release("v0.5.0"), release("v0.5.1-beta.1", prerelease=True)]

    def fetch(url: str, timeout: float, accept: str = "") -> bytes:
        asked.append(url)
        return json.dumps(listed if url.endswith("/releases?per_page=30") else release("v0.5.0")).encode()

    monkeypatch.setattr(updates, "fetch", fetch)
    full = LATEST_RELEASE("x/y")
    assert (full.version, full.beta) == ("0.5.0", False) and asked[-1].endswith("/releases/latest")
    beta = LATEST_RELEASE("x/y", beta=True)
    assert (beta.version, beta.beta, beta.download) == ("0.5.1-beta.2", True, "https://x/v0.5.1-beta.2.zip")
    listed.append(release("v0.5.1"))  # the full release is out: it passes its betas
    assert LATEST_RELEASE("x/y", beta=True).version == "0.5.1"
    listed[:] = [release("nightly")]
    with pytest.raises(UpdateError, match="none of the newest releases' tags is a version number"):
        LATEST_RELEASE("x/y", beta=True)
    listed[:] = []
    assert LATEST_RELEASE("x/y", beta=True) is None


def test_a_beta_published_as_a_full_release_isnt_offered_with_beta_off(monkeypatch) -> None:
    """The rule is that a beta is always a GitHub pre-release; one published without the mark comes back from
    /releases/latest, and 0.5.0 on doesn't take it with beta off. And only the release's own huragok zip, or GitHub's
    zip of the tag, is ever downloaded: never another zip that happens to be attached."""
    answer = {"tag_name": "v0.5.1-beta.1", "zipball_url": "https://api.github.com/zipball",
              "assets": [{"name": "maps.zip", "browser_download_url": "https://x/maps.zip"}]}
    monkeypatch.setattr(updates, "fetch", lambda url, timeout, accept="": json.dumps(answer).encode())
    with pytest.raises(UpdateError, match=r"v0\.5\.1-beta\.1, is a beta published as a full release"):
        LATEST_RELEASE("x/y")
    answer["tag_name"] = "v0.5.1"
    assert LATEST_RELEASE("x/y").download == "https://api.github.com/zipball"  # not maps.zip


def test_beta_is_off_unless_the_host_turns_it_on_and_the_service_asks_as_it_says(tmp_path: Path, monkeypatch,
                                                                                run) -> None:
    write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    assert settings.updates.beta is False  # full releases only, by default
    on = load(write_settings(tmp_path / "on", tmp_path / "server", "[updates]\nbeta = true\n"))
    assert on.updates.beta is True
    asked: list[dict[str, Any]] = []
    beta = Release("9.0.0-beta.1", "https://x", "", "https://x/h.zip", beta=True)

    def latest(repository: str, **options: Any) -> Release:
        asked.append(options)
        return beta

    monkeypatch.setattr(updates, "latest_release", latest)
    service = UpdateService(on, None, lambda: None)  # type: ignore[arg-type]
    service.can_install = True
    assert run(service.check()) is beta and asked == [{"beta": True}]
    assert service.status_text() == "Huragok 9.0.0-beta.1 beta is out (U)"
    run(UpdateService(settings, None, lambda: None).check())  # type: ignore[arg-type]
    assert asked[-1] == {"beta": False}


def test_installing_replaces_the_program_and_nothing_of_yours(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    assert install(make_release_zip(tmp_path / "r.zip"), home, NEW) == NEW
    engine = home / "engine" / "huragok"
    assert (engine / "new_module.py").exists() and not (engine / "old_module.py").exists()
    assert (home / "engine" / ".venv" / "marker").read_text(encoding="utf-8") == "installed packages"
    assert (home / "start.cmd").read_text(encoding="utf-8") == "new start.cmd"
    assert (home / "docs" / "automod.md").exists() and not (home / "docs" / "old.md").exists()
    for name, text in {"settings.toml": "mine", "commands.toml": "mine", "automod.txt": "my words",
                       "records/supervisor.sqlite3": "my records"}.items():
        assert (home / name).read_text(encoding="utf-8") == text, name
    backup = home / "records" / "previous-0.0.1"  # named after the version that was on disk
    assert (backup / "engine" / "huragok" / "old_module.py").exists()
    assert (backup / "start.cmd").read_text(encoding="utf-8") == "old"
    assert not (backup / "engine" / ".venv").exists()  # the packages never move
    assert not (home / "records" / "update").exists()
    pointer = json.loads((home / "records" / "last-update.json").read_text(encoding="utf-8"))
    assert pointer["version"] == NEW and pointer["replaced"] == "0.0.1" and pointer["backup"] == "previous-0.0.1"
    assert "engine/requirements.txt" in pointer["added"] and "engine/huragok" not in pointer["added"]


def test_githubs_own_zip_works_too(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    zipball = make_release_zip(tmp_path / "r.zip", top="LittleGroove-Project-Reclaimer-Huragok-1a2b3c4")
    assert install(zipball, home) == NEW


def test_a_download_that_isnt_what_it_says_changes_nothing(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    with pytest.raises(UpdateError, match="says it's version 1.0.0"):
        install(make_release_zip(tmp_path / "r.zip", version="1.0.0"), home, NEW)
    (tmp_path / "bad.zip").write_text("not a zip", encoding="utf-8")
    with pytest.raises(UpdateError, match="isn't a zip"):
        install(tmp_path / "bad.zip", home)
    assert (home / "start.cmd").read_text(encoding="utf-8") == "old"
    assert (home / "engine" / "huragok" / "old_module.py").exists()


def test_a_checksum_mismatch_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(updates, "fetch", lambda url, timeout, accept="": b"zip bytes")
    with pytest.raises(UpdateError, match="checksum"):
        updates.download(Release(NEW, "", "", "https://x/r.zip", "sha256:" + "0" * 64), tmp_path)


def test_settings(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    assert settings.updates.check and not settings.updates.auto_update  # off unless the host turns it on
    # 0.4.0 and 0.4.1 called it install_on_server_restart: read as auto_update until the start renames the line.
    old = load(write_settings(tmp_path, tmp_path / "server", "[updates]\ninstall_on_server_restart = true"))
    assert old.updates.auto_update and any("is auto_update's name before 0.4.2" in note for note in old.notes)
    both = load(write_settings(tmp_path, tmp_path / "server",
                               "[updates]\nauto_update = false\ninstall_on_server_restart = true"))
    assert not both.updates.auto_update  # the new name wins
    assert settings.updates.repository == "LittleGroove/Project-Reclaimer-Huragok"
    with pytest.raises(ConfigError, match="owner/name"):
        load(write_settings(tmp_path, tmp_path / "server", '[updates]\nrepository = "https://github.com/x"'))


def service(tmp_path: Path, monkeypatch, body: str = "") -> tuple[UpdateService, list[str], list[str]]:
    write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server", body))
    installed: list[str] = []
    relaunched: list[str] = []
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
    monkeypatch.setattr(updates, "update", lambda home, release: installed.append(release.version) or release.version)
    from huragok.core.events import EventBus

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
    assert sum("Update available: Huragok 99.0.0" in m for m in said) == 1
    assert any("To install it: U in the window" in m for m in said) and any("- Better things" in m for m in said)
    assert any("[updates] auto_update = true" in m for m in said)  # how to have it install itself
    assert svc.pending == RELEASE and installed == []
    assert svc.status_text() == "Huragok 99.0.0 is out (U)"  # what stays on the window's HURAGOK line


def test_installing_when_asked_then_starting_again(tmp_path: Path, monkeypatch, run) -> None:
    svc, installed, relaunched = service(tmp_path, monkeypatch)
    assert run(svc.install("asked in this window")) and installed == [NEW] and relaunched == ["yes"]


def test_auto_update_installs_a_new_version_as_soon_as_its_found(tmp_path: Path, monkeypatch, run, caplog) -> None:
    """Off by default: found, said, kept on the window's line, waiting for U. On: installed at once, whatever the
    servers are doing (a server that keeps failing to start never restarts, which 0.4.1's switch waited for)."""

    async def scenario(svc: UpdateService) -> None:
        await svc.check()
        await asyncio.sleep(0.05)

    svc, installed, _ = service(tmp_path, monkeypatch)
    run(scenario(svc))
    assert installed == []
    svc, installed, relaunched = service(tmp_path / "auto", monkeypatch, "[updates]\nauto_update = true")
    with caplog.at_level(logging.WARNING):
        run(scenario(svc))
    assert installed == [NEW] and relaunched == ["yes"]
    assert "auto_update is on: it installs now" in caplog.text
    svc, installed, _ = service(tmp_path / "old", monkeypatch, "[updates]\ninstall_on_server_restart = true")
    run(scenario(svc))
    assert installed == [NEW]  # an older file's switch still works before the start renames it


def test_the_window_keeps_saying_a_new_version_is_out(tmp_path: Path, monkeypatch, run) -> None:
    """The events scroll it away (a server that keeps failing to start writes seven lines a try): the HURAGOK line
    in both windows says so until it's installed."""
    from huragok.window.controllers import LocalController, RemoteController

    async def scenario() -> None:
        write_server_folder(tmp_path / "server", free_port())
        settings = load(write_settings(tmp_path, tmp_path / "server", commands=staff()))
        supervisor = Huragok(settings)
        monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
        supervisor.updates.can_install = True
        local = LocalController(supervisor, None)
        assert not any("Huragok 99" in line for line in local.header())
        await supervisor.updates.check()
        assert any(line.startswith("HRGK") and "Huragok 99.0.0 is out (U)" in line for line in local.header())
        supervisor.superintendent.fleet.write_live()
        live = json.loads((settings.records / "live.json").read_text(encoding="utf-8"))
        assert live["huragok_update"] == "Huragok 99.0.0 is out (U)"
        remote = RemoteController(settings, lambda: 4242)
        assert any("process 4242 · Huragok 99.0.0 is out (U)" in line for line in remote.header())
        supervisor.updates.failed = NEW
        assert supervisor.updates.status_text() == "Huragok 99.0.0 didn't install: U tries again"
        supervisor.updates.installing = True
        assert supervisor.updates.status_text() == "installing Huragok 99.0.0"
        supervisor.storage.close()

    run(scenario())


def test_a_copy_not_run_from_its_folder_wont_update_itself(tmp_path: Path, monkeypatch, run) -> None:
    svc, installed, _ = service(tmp_path, monkeypatch)
    svc.can_install = False
    assert not run(svc.install("asked")) and installed == []


def test_update_asked_from_outside_restarts_with_the_servers_left_running(tmp_path: Path, monkeypatch, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            write_server_folder(tmp_path / "server", fake.port)
            supervisor = Huragok(load(write_settings(tmp_path, tmp_path / "server", commands=staff())))
            monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
            monkeypatch.setattr(updates, "update", lambda home, release: release.version)
            supervisor.updates.can_install = True
            task = asyncio.ensure_future(supervisor.run())
            await wait_for(lambda: supervisor.superintendent.states["LG Test Server"].connected)
            mailbox.post(tmp_path / "records", "update", why="a test")
            await asyncio.wait_for(task, 10)
            assert supervisor.relaunch and supervisor.stop_servers is False

    run(scenario())


def test_update_command_checks_and_asks(tmp_path: Path, monkeypatch, capsys) -> None:
    write_server_folder(tmp_path, free_port())
    home = tmp_path / "huragok"
    home.mkdir()
    firstrun.write_files(home)
    config = str(home / "settings.toml")
    assert cli.main(["-c", config, "update"]) == 0
    assert f"{__version__} is the newest" in capsys.readouterr().out
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: RELEASE)
    assert cli.main(["-c", config, "update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "Update available: Huragok 99.0.0" in out and "- Better things" in out
    monkeypatch.setattr(updates, "installed_here", lambda home: True)
    assert cli.main(["-c", config, "update"]) == 1  # nobody at a console to say yes
    assert "update --yes" in capsys.readouterr().out
    assert "99.0.0 is out" in (check_module.update_note(load(home / "settings.toml")))


# Installing all or nothing, never wiping a backup -------------------------------------------------

def _flaky_rename(monkeypatch: pytest.MonkeyPatch, fail: Callable[[Path, Path], bool]) -> list[tuple[Path, Path]]:
    """os.rename failing (as Windows does for a folder something holds) where `fail` says; recorded."""
    import os

    real = os.rename
    calls: list[tuple[Path, Path]] = []

    def rename(source: Any, target: Any) -> None:
        calls.append((Path(source), Path(target)))
        if fail(Path(source), Path(target)):
            raise PermissionError(13, "Access is denied", str(source))
        real(source, target)

    monkeypatch.setattr(updates.os, "rename", rename)
    return calls


def assert_old_install(home: Path) -> None:
    engine = home / "engine" / "huragok"
    assert sorted(p.name for p in engine.iterdir()) == ["__init__.py", "old_module.py"]
    assert (home / "start.cmd").read_text(encoding="utf-8") == "old" and (home / "docs" / "old.md").exists()
    assert not (home / "engine" / "requirements.txt").exists() and not (home / "records" / "last-update.json").exists()
    assert not list((home / "records").glob("previous-*"))  # the undone install leaves no backup behind


def test_an_old_folder_that_cant_be_moved_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    home = make_install(tmp_path / "home")
    _flaky_rename(monkeypatch, lambda source, target: source == home / "engine" / "huragok")
    with pytest.raises(UpdateError, match="nothing was changed") as raised:
        install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    assert "close it and try again" in str(raised.value)
    assert_old_install(home)


def test_a_new_folder_that_cant_be_moved_puts_every_old_one_back(tmp_path: Path, monkeypatch) -> None:
    home = make_install(tmp_path / "home")
    staging = home / "records" / "update"
    calls = _flaky_rename(monkeypatch, lambda source, target: staging in source.parents and source.name == "docs")
    with pytest.raises(UpdateError, match="nothing was changed"):
        install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    assert len(calls) > 4  # engine's items were swapped already, then undone one by one
    assert_old_install(home)
    assert not (home / "engine" / "huragok" / "huragok").exists()  # never the old folder inside the new one


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows holds a folder a process has as its current one")
def test_a_console_open_in_engine_huragok_stops_the_install_with_nothing_changed(tmp_path: Path) -> None:
    import subprocess
    import sys

    home = make_install(tmp_path / "home")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=home / "engine" / "huragok")
    try:
        with pytest.raises(UpdateError, match="nothing was changed"):
            install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    finally:
        holder.kill()
        holder.wait()
    assert_old_install(home)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows holds a folder a process has as its current one")
def test_an_unpacked_release_held_by_something_stops_the_install_with_nothing_changed(tmp_path: Path,
                                                                                    monkeypatch) -> None:
    import subprocess
    import sys

    home = make_install(tmp_path / "home")
    holders: list[subprocess.Popen[bytes]] = []
    real_root = updates.program_root

    def held(extracted: Path) -> Path:  # an antivirus scanning the freshly unpacked files, say
        root = real_root(extracted)
        holders.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                        cwd=root / "engine" / "huragok"))
        return root

    monkeypatch.setattr(updates, "program_root", held)
    try:
        with pytest.raises(UpdateError, match="nothing was changed"):
            install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()
    assert_old_install(home)


def test_a_second_install_never_touches_the_first_backup(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    first = home / "records" / "previous-0.0.1"
    install(make_release_zip(tmp_path / "r2.zip"), home, NEW)  # retried over itself (as after a failed relaunch)
    assert (first / "engine" / "huragok" / "old_module.py").exists()  # still the old version's own files
    assert (home / "records" / f"previous-{NEW}" / "engine" / "huragok" / "new_module.py").exists()
    install(make_release_zip(tmp_path / "r3.zip"), home, NEW)  # and again: the earlier backup is kept, dated
    backups = sorted(p.name for p in (home / "records").glob(f"previous-{NEW}*"))
    assert len(backups) == 2 and backups[0] == f"previous-{NEW}"
    assert json.loads((home / "records" / "last-update.json").read_text(encoding="utf-8"))["backup"] == \
        f"previous-{NEW}"


def test_put_back_restores_what_the_last_update_replaced(tmp_path: Path) -> None:
    home = make_install(tmp_path / "home")
    install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    last = updates.last_update(home)
    assert last is not None and last.replaced == "0.0.1" and last.backup == home / "records" / "previous-0.0.1"
    assert updates.put_back(home, last) == "0.0.1"
    assert_old_install(home)
    assert (home / "engine" / ".venv" / "marker").exists() and (home / "settings.toml").read_text() == "mine"


def test_update_installs_the_new_packages_or_puts_the_old_version_back(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(updates, "installed_here", lambda home: True)
    monkeypatch.setattr(updates, "download", lambda release, folder: make_release_zip(folder / "r.zip"))
    pip: list[list[str]] = []

    def fake_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        pip.append(command)
        failed = len(pip) > 1
        return subprocess.CompletedProcess(command, 1 if failed else 0, "",
                                           "ERROR: No matching distribution found for websockets>=99" if failed else "")

    monkeypatch.setattr(updates.subprocess, "run", fake_run)
    home = make_install(tmp_path / "home")
    assert updates.update(home, RELEASE) == NEW
    assert pip[0][1:4] == ["-m", "pip", "install"] and pip[0][-2:] == ["-r", str(home / "engine" / "requirements.txt")]
    assert (home / "engine" / ".venv" / "requirements.txt").read_text(encoding="utf-8") == "websockets>=13\n"
    updates.refresh_packages(home)
    assert len(pip) == 1  # the same requirements: nothing to install
    other = make_install(tmp_path / "other")
    with pytest.raises(UpdateError, match="packages failed: ERROR: No matching distribution") as raised:
        updates.update(other, RELEASE)
    assert str(raised.value).endswith("the old version's files are back in place")
    assert_old_install(other)


def serve_once(body: bytes, promised: int) -> str:
    """A web server on this PC that promises `promised` bytes, sends `body` and closes the connection cleanly."""
    import socket
    import threading

    from conftest import free_port

    port = free_port()
    listener = socket.socket()
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    def serve() -> None:
        connection, _ = listener.accept()
        connection.recv(65536)
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: "
                           + str(promised).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        connection.shutdown(socket.SHUT_WR)
        connection.close()
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{port}/project-reclaimer-dedicated-v0.9.20.exe"


def test_a_download_cut_short_is_never_used(tmp_path: Path) -> None:
    with pytest.raises(UpdateError, match="stopped after 400 of 1000 bytes; nothing was changed"):
        updates.fetch(serve_once(b"M" * 400, 1000), 5, accept="application/octet-stream")
    assert updates.fetch(serve_once(b"M" * 400, 400), 5, accept="application/octet-stream") == b"M" * 400
    with pytest.raises(UpdateError, match="stopped after 400 of 2000 bytes"):  # GitHub's listed size counts too
        updates.fetch(serve_once(b"M" * 400, 400), 5, accept="application/octet-stream", size=2000)


def test_the_server_program_has_a_larger_download_limit(tmp_path: Path, monkeypatch) -> None:
    from huragok.core import web
    from huragok.program import releases as serverupdates

    asked: list[dict[str, Any]] = []

    def fake_get(url: str, timeout: float, accept: str = "", **options: Any) -> bytes:
        asked.append({"url": url, **options})
        return b"x" * 10

    monkeypatch.setattr(web, "fetch", fake_get)  # every download goes through it
    release = serverupdates.ProgramRelease("0.9.20", "p", "", "https://x/p.exe", "p.exe", None, None, exe_size=10)
    serverupdates.download_program(release, tmp_path)
    assert asked == [{"url": "https://x/p.exe", "limit": web.MAX_PROGRAM_DOWNLOAD, "size": 10}]
    assert web.MAX_PROGRAM_DOWNLOAD >= 300_000_000 > web.MAX_DOWNLOAD
    serverupdates.download_program(serverupdates.ProgramRelease("0.9.20", "p", "", "https://x/q.exe", "q.exe"),
                                   tmp_path)
    assert asked[-1] == {"url": "https://x/q.exe", "limit": web.MAX_PROGRAM_DOWNLOAD}  # no size listed


# A version that isn't a number, GitHub out of reach, a failed install retried --------------------------

def test_a_release_tag_that_isnt_a_version_is_an_error_not_a_crash(tmp_path: Path, monkeypatch, run) -> None:
    monkeypatch.setattr(updates, "fetch", lambda url, timeout, accept="": json.dumps(
        {"tag_name": "huragok-0.3.0", "assets": []}).encode())
    with pytest.raises(UpdateError, match=r"the newest release's tag \(huragok-0.3.0\) isn't a version number"):
        LATEST_RELEASE("x/y")
    svc, installed, _ = service(tmp_path, monkeypatch)
    monkeypatch.setattr(updates, "latest_release", lambda *a, **k: Release("huragok-0.3.0", "p", "", "https://x/h.zip"))
    assert run(svc.check()) is None and "isn't a version number" in svc.last_error and installed == []


def test_the_update_loops_survive_a_bad_round(tmp_path: Path, monkeypatch, run, caplog) -> None:
    import time

    from huragok.program import releases as serverupdates
    from huragok.core.events import EventBus
    from huragok.updates.program import ProgramUpdateService
    from testkit.manage import program_release

    from conftest import ProgramLifecycle

    svc, _, _ = service(tmp_path, monkeypatch)
    rounds: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> Any:
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("something odd in GitHub's answer")
        return RELEASE

    monkeypatch.setattr(updates, "latest_release", flaky)
    program_rounds: list[int] = []

    def program_flaky(*args: Any, **kwargs: Any) -> Any:
        program_rounds.append(1)
        if len(program_rounds) == 1:
            raise RuntimeError("something odd")
        return program_release("0.9.20")

    monkeypatch.setattr(serverupdates, "latest_program_release", program_flaky)
    program = ProgramUpdateService(svc.settings, EventBus(), ProgramLifecycle(), {}, {})  # type: ignore[arg-type]
    real_sleep = asyncio.sleep
    monkeypatch.setattr(updates.asyncio, "sleep", lambda seconds: real_sleep(0.01))

    async def scenario() -> None:
        tasks = [asyncio.ensure_future(svc.run()), asyncio.ensure_future(program.run())]
        deadline = time.monotonic() + 10
        while (svc.pending is None or program.newest is None) and time.monotonic() < deadline:
            await real_sleep(0.02)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert svc.pending == RELEASE and program.newest is not None and program.newest.version == "0.9.20"
    assert "Looking for a new Huragok went wrong" in caplog.text
    assert "Looking for server program updates went wrong" in caplog.text


def test_a_failed_self_update_isnt_tried_again_at_every_server_start(tmp_path: Path, monkeypatch, run,
                                                                    caplog) -> None:
    svc, _, _ = service(tmp_path, monkeypatch, "[updates]\nauto_update = true")
    calls: list[str] = []

    def failing(home: Path, release: Release) -> str:
        calls.append(release.version)
        raise UpdateError("installing the new version's Python packages failed: no wheel")

    monkeypatch.setattr(updates, "update", failing)

    async def scenario() -> None:
        for _ in range(3):  # Huragok looks every check_hours
            await svc.check()
            await asyncio.sleep(0.05)
        assert calls == [NEW]  # once, not at every look
        assert svc.status_text() == "Huragok 99.0.0 didn't install: U tries again"
        assert not await svc.install("asked in this window") and calls == [NEW, NEW]  # a person asks: tried again
        newer = Release("99.0.1", "p", "", "https://example.invalid/huragok-99.0.1.zip")
        monkeypatch.setattr(updates, "latest_release", lambda *a, **k: newer)
        await svc.check()
        await asyncio.sleep(0.05)
        assert calls == [NEW, NEW, "99.0.1"]  # a newer release is tried by itself

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "Huragok doesn't try 99.0.0 again by itself; it tries a newer release when one is out" in caplog.text
    assert "To try 99.0.0 again: U in the window" in caplog.text


def test_a_version_the_start_loop_took_back_out_isnt_installed_again_by_itself(tmp_path: Path, monkeypatch, run,
                                                                               caplog) -> None:
    import json

    from huragok import __version__
    from huragok.core.events import EventBus

    svc, installed, _ = service(tmp_path, monkeypatch, "[updates]\nauto_update = true")
    note = svc.settings.records / "failed-update.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(json.dumps({"version": NEW, "restored": __version__, "exit_code": 2, "time": 0}), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        svc = UpdateService(svc.settings, EventBus(), lambda: None)
    svc.can_install = True
    assert f"Huragok {NEW} it was installed but didn't start (exit code 2)" in caplog.text

    async def scenario() -> None:
        await svc.check()
        await asyncio.sleep(0.05)

    run(scenario())
    assert installed == []  # auto_update doesn't put the version that didn't start back in
    note.write_text(json.dumps({"version": __version__, "restored": "0.0.1"}), encoding="utf-8")
    UpdateService(svc.settings, EventBus(), lambda: None)
    assert not note.exists()  # that version runs now: the note is done with


def test_the_update_screen_says_when_github_couldnt_be_asked(tmp_path: Path, monkeypatch, run) -> None:
    from conftest import free_port

    from huragok.program import releases as serverupdates

    write_server_folder(tmp_path / "server", free_port())
    supervisor = Huragok(load(write_settings(tmp_path, tmp_path / "server", commands=staff())))

    def offline(*args: Any, **kwargs: Any) -> Any:
        raise UpdateError("couldn't reach GitHub: getaddrinfo failed")

    monkeypatch.setattr(updates, "latest_release", offline)
    monkeypatch.setattr(serverupdates, "latest_program_release", offline)
    try:
        result = run(supervisor.request("check_update", {}))
        assert not result.ok
        assert result.message == "couldn't ask GitHub whether there's a newer Huragok: couldn't reach GitHub: " \
                                 "getaddrinfo failed"
        result = run(supervisor.request("check_program_update", {}))
        assert not result.ok and result.message.startswith(
            "couldn't tell whether there's a newer server program: couldn't ask GitHub: couldn't reach GitHub")
        result = run(supervisor.request("update_program", {}))
        assert not result.ok and result.message.startswith("not installed: couldn't ask GitHub")
        monkeypatch.setattr(updates, "latest_release", lambda *a, **k: None)
        result = run(supervisor.request("check_update", {}))
        assert result.ok and result.message == f"Huragok {__version__} is the newest version"
    finally:
        supervisor.storage.close()


# The `dedicated` runner: which copy runs, and nothing waits on SteamCMD -------------------------------

# The program says it started a child once that child is running (its process number written down): ended
# before then, the grandchild might never have run at all, and nothing would show whether it was left behind.
CHILD_PROGRAM = '''import os, subprocess, sys, time
from pathlib import Path
pid_file = Path(__file__).with_name("grand.pid")
if sys.argv[1:2] == ["grand"]:  # SteamCMD, as far as this goes: it inherits the output pipe
    pid_file.with_suffix(".new").write_text(str(os.getpid()))
    pid_file.with_suffix(".new").replace(pid_file)
    print("grandchild running", flush=True)
    time.sleep(60)
    sys.exit(0)
subprocess.Popen([sys.executable, __file__, "grand"])
deadline = time.monotonic() + 30
while not pid_file.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
print("program started a child", flush=True)
time.sleep(60)
'''


def child_tool(tmp_path: Path) -> tuple[Any, Path]:
    import sys

    from huragok.program.dedicated import Dedicated

    script = tmp_path / "program.py"
    script.write_text(CHILD_PROGRAM, encoding="utf-8")
    return Dedicated(Path(sys.executable), template=[sys.executable, str(script), "dedicated"]), \
        tmp_path / "grand.pid"


def assert_gone(pid_file: Path) -> None:
    import time

    import psutil

    assert pid_file.exists(), "the grandchild never ran: the test ended the program too soon"
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        process = psutil.Process(pid)
        process.wait(5)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        process.kill()  # ours: never left behind
        raise AssertionError("the grandchild was left running") from None


def test_a_timeout_ends_the_whole_process_tree_at_once(tmp_path: Path) -> None:
    import time

    tool, pid_file = child_tool(tmp_path)
    started = time.monotonic()
    result = tool.run_sync(tmp_path, "slow", timeout=4)
    took = time.monotonic() - started
    assert result.code == 124 and result.problem == "slow took longer than 4 s and was stopped"
    assert took < 14, took  # not the grandchild's 60 s
    assert "program started a child" in result.text
    assert_gone(pid_file)


def test_stopping_a_run_ends_it_and_its_children(tmp_path: Path) -> None:
    import threading
    import time

    tool, pid_file = child_tool(tmp_path)
    stop = threading.Event()

    def stop_once_the_grandchild_runs() -> None:
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()

    threading.Thread(target=stop_once_the_grandchild_runs, daemon=True).start()
    started = time.monotonic()
    result = tool.run_sync(tmp_path, "slow", timeout=60, stop=stop)
    assert result.code == 130 and "was stopped before it finished" in result.problem
    assert time.monotonic() - started < 40
    assert_gone(pid_file)


def test_esc_on_a_streamed_command_ends_it_and_steamcmd(tmp_path: Path, run) -> None:
    import time

    tool, pid_file = child_tool(tmp_path)
    lines: list[str] = []

    async def scenario() -> Any:
        cancel = asyncio.Event()

        def on_line(line: str) -> None:
            lines.append(line)
            if "started a child" in line:
                cancel.set()  # Esc

        return await tool.stream(tmp_path, "slow", on_line=on_line, cancel=cancel, timeout=60)

    started = time.monotonic()
    result = run(scenario())
    assert result.code == 130 and "(cancelled)" in result.text and "program started a child" in lines
    assert time.monotonic() - started < 15
    assert_gone(pid_file)


def test_the_output_pipe_is_closed_once_read(tmp_path: Path, monkeypatch, run) -> None:
    """Every `dedicated` command left its output pipe open for the garbage collector (an "unclosed file" for each
    content listing, check and Workshop fetch the window ran)."""
    import sys

    from huragok.program import dedicated

    script = tmp_path / "program.py"
    script.write_text("print('a line')\n", encoding="utf-8")
    tool = dedicated.Dedicated(Path(sys.executable), template=[sys.executable, str(script), "dedicated"])
    started: list[Any] = []
    real_start = dedicated._start
    monkeypatch.setattr(dedicated, "_start", lambda *a, **k: started.append(real_start(*a, **k)) or started[-1])
    assert tool.run_sync(tmp_path, "content").text == "a line\n"
    lines: list[str] = []
    assert run(tool.stream(tmp_path, "content", on_line=lines.append)).ok and lines == ["a line"]
    assert len(started) == 2 and all(process.stdout.closed for process in started)


def test_each_server_folder_runs_its_own_copy_of_the_program(tmp_path: Path, monkeypatch) -> None:
    from huragok.program import dedicated
    from huragok.program.dedicated import tool_for
    from testkit.manage import self_updating_root

    root = self_updating_root(tmp_path, "Alpha", "Beta")
    (root / "project-reclaimer.exe").write_bytes(b"stale 0.9.15")  # nothing updates the root's copy
    settings = load(root / "settings.toml")
    exe = {name: root / "SERVERS" / name / "project-reclaimer.exe" for name in ("Alpha", "Beta")}
    versions = {root / "project-reclaimer.exe": "0.9.15", exe["Alpha"]: "0.9.20", exe["Beta"]: "0.9.19"}
    monkeypatch.setattr(dedicated, "cached_version", lambda path, template=None: versions.get(path))
    tool = tool_for(settings)
    assert tool.executable == exe["Alpha"]  # init, mode-check: the newest copy, not the stale root one
    beta = settings.server("Beta")
    assert tool.executable_for(beta.folder) == exe["Beta"]
    assert str(exe["Beta"]) in tool.command("mods", folder=beta.folder)
    assert tool_for(settings, beta).executable == exe["Beta"]
    exe["Beta"].unlink()  # a server without a copy of its own runs the root's, so its commands do too
    root_copy = root / "project-reclaimer.exe"
    assert tool.executable_for(beta.folder) == root_copy and tool_for(settings, beta).executable == root_copy
    assert tool.executable_for(root / "SERVERS") == exe["Alpha"]  # not a server folder (init): the newest
    settings.setup.program = str(root / "project-reclaimer.exe")  # [setup] program: always that one
    assert tool_for(settings).executable == root / "project-reclaimer.exe"
    versions.clear()  # no copy says its version: the root's, as before
    settings.setup.program = None
    assert tool_for(settings).executable == root / "project-reclaimer.exe"


def test_the_release_named_program_is_picked_by_version(tmp_path: Path) -> None:
    from huragok.program.dedicated import find_executable

    for version in ("0.9.9", "0.9.10", "0.9.8"):
        (tmp_path / f"project-reclaimer-dedicated-v{version}.exe").write_bytes(b"")
    assert find_executable(tmp_path).name == "project-reclaimer-dedicated-v0.9.10.exe"


def test_the_programs_version_answer_is_read_loosely_and_unknown_is_never_older(tmp_path: Path,
                                                                              monkeypatch) -> None:
    from huragok.program import releases as serverupdates
    from huragok.program.dedicated import Dedicated, ToolResult

    tool = Dedicated(Path("project-reclaimer.exe"))
    for text, version in (("Project Reclaimer v0.9.21 (pre-release)\n", "0.9.21"),
                          ("project-reclaimer-dedicated 0.9.20\n", "0.9.20"), ("PROJECT-RECLAIMER 1.0\n", "1.0"),
                          ("0.9.19\n", "0.9.19"), ("nightly build abc123\n", "nightly build abc123")):
        monkeypatch.setattr(tool, "run_sync", lambda *a, text=text, **k: ToolResult(True, 0, text))
        assert tool.version() == version, text
    from conftest import free_port

    write_server_folder(tmp_path / "s", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "s"))
    path = tmp_path / "project-reclaimer.exe"
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: "nightly build abc123")
    assert serverupdates.as_new_as(settings, path, "0.9.20") == "nightly build abc123"  # kept, never replaced
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: None)
    assert serverupdates.as_new_as(settings, path, "0.9.20") is None  # doesn't answer at all: an old program
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: "0.9.19")
    assert serverupdates.as_new_as(settings, path, "0.9.20") is None


# The server program's releases: pulled ones, odd versions, swaps that fail -----------------------------

def program_service(root: Path, names: tuple[str, ...] = ("Alpha", "Beta"), clients: dict[str, Any] | None = None,
                    up: tuple[str, ...] = ("Alpha", "Beta")) -> tuple[Any, Any, Any]:
    from conftest import ProgramLifecycle

    from huragok.core.events import EventBus
    from huragok.updates.program import ProgramUpdateService
    from huragok.servers.state import ServerState

    settings = load(root / "settings.toml")
    lifecycle, bus = ProgramLifecycle(up=up), EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {n: ServerState(n) for n in names},  # type: ignore
                                   clients or {})
    return service, lifecycle, bus


def test_a_pulled_release_is_dropped_and_never_installed(tmp_path: Path, monkeypatch, run, caplog) -> None:
    import time

    from huragok.updates.program import OVERDUE
    from testkit.manage import program_release, self_updating_root, serve_program

    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(b"0.9.20")
    service, lifecycle, bus = program_service(root)

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.20")
        bus.emit("rcon.connected", "Beta", version="0.9.20")
        serve_program(monkeypatch, program_release("0.9.21", published=time.time() - 600), b"broken 0.9.21")
        await service.check()
        bus.emit("server.update_failed", "Alpha", reason="The download stopped before it finished")
        await wait_for(lambda: "program" in lifecycle.jobs.get("Alpha", {}))
        job, download = lifecycle.jobs["Alpha"]["program"], service.downloads["0.9.21"]
        assert lifecycle.planned["Alpha"].reason == "updating to Project Reclaimer 0.9.21"
        # 0.9.21 is pulled: GitHub's latest is 0.9.20 again.
        serve_program(monkeypatch, program_release("0.9.20", published=time.time() - 86400), b"good 0.9.20")
        await service.check()
        assert service.newest.version == "0.9.20" and service.taken == {} and not download.exists()
        assert "program" not in lifecycle.jobs["Alpha"] and lifecycle.cancelled == ["Alpha"]
        assert service.review(now=time.time() + OVERDUE + 1) == []  # nobody is behind 0.9.20
        assert (await job.run()).startswith("left as it is: Project Reclaimer 0.9.21 is no longer the newest")

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert [(root / "SERVERS" / n / "project-reclaimer.exe").read_bytes() for n in ("Alpha", "Beta")] == [b"0.9.20"] * 2
    assert "Project Reclaimer 0.9.21 is no longer GitHub's newest release (0.9.20 is, again)" in caplog.text


def test_the_old_file_is_tidied_even_when_another_restart_is_planned(tmp_path: Path, run) -> None:
    from types import SimpleNamespace

    from testkit.manage import self_updating_root

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    service, lifecycle, bus = program_service(root, ("Alpha",))
    lifecycle.planned["Alpha"] = SimpleNamespace(when="after_game", reason="installing a new version of HugeGrass")

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.19")
        bus.emit("server.updating", "Alpha")
        bus.emit("rcon.connected", "Alpha", version="0.9.20")
        await asyncio.sleep(0.05)

    run(scenario())
    assert "program-old" in lifecycle.jobs["Alpha"] and lifecycle.restarts == []  # the planned restart runs it


def test_a_version_that_isnt_a_number_is_left_out(tmp_path: Path, monkeypatch, run, caplog) -> None:
    from types import SimpleNamespace

    from testkit.manage import program_release, self_updating_root, serve_program

    root = self_updating_root(tmp_path, body='backend = "process"\n')
    serve_program(monkeypatch, program_release("0.9.20"), b"x")
    clients = {"Alpha": SimpleNamespace(version="0.9.18"), "Beta": SimpleNamespace(version="unknown")}
    service, _, _ = program_service(root, clients=clients)
    with caplog.at_level(logging.INFO):
        assert run(service.check()).version == "0.9.20" and service.is_newer(service.pending) is True
        run(service.check())
    assert caplog.text.count("[Beta] its console gives its version as 'unknown'") == 1


def test_a_swap_that_keeps_failing_backs_off_then_waits_for_u(tmp_path: Path, monkeypatch, run, caplog) -> None:
    import time

    from huragok.program import releases as serverupdates
    from testkit.manage import program_release, self_updating_root, serve_program

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    serve_program(monkeypatch, program_release(published=time.time() - 3 * 3600), b"program 0.9.19")
    monkeypatch.setattr(serverupdates, "swap_program", lambda *a: (_ for _ in ()).throw(
        UpdateError("couldn't put the new program at x: [WinError 5] Access is denied")))
    service, lifecycle, bus = program_service(root, ("Alpha",))

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        await service.check()
        later, waits = time.time() + program_updates_module.OVERDUE + 1, []
        for attempt in range(4):
            assert service.review(now=later) == ["Alpha"], attempt
            await wait_for(lambda: "program" in lifecycle.jobs.get("Alpha", {}))
            assert await lifecycle.jobs["Alpha"].pop("program").run() == ""
            if attempt < 3:
                waits.append(round((service.retry["Alpha"] - time.time()) / 3600))
                assert service.review(now=service.retry["Alpha"] - 60) == []  # not before its time
                later = max(service.retry["Alpha"], time.time() + program_updates_module.OVERDUE) + 1
        assert waits == [1, 4, 12] and len(lifecycle.restarts) == 4
        assert service.review(now=later + 10 ** 7) == []  # it waits for U now
        assert lifecycle.flags[("Alpha", "program")] == "! Project Reclaimer 0.9.19 didn't install (4 tries): U to " \
                                                         "try again"
        assert service.note("Alpha") == "Project Reclaimer 0.9.19 didn't install in 4 tries: U to try again"
        ok, message = await service.install(when="after_game")  # U: tried again
        assert ok and "Alpha installs 0.9.19 after its game" in message and not service.swap_failures

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "Huragok tries again in 4 hours" in caplog.text
    assert "That's 4 tries, so Huragok stops trying it by itself" in caplog.text


def test_a_program_file_is_never_left_half_written(tmp_path: Path, monkeypatch) -> None:
    import os

    from conftest import free_ports, write_root

    from huragok.program import releases as serverupdates
    from huragok.program.releases import install_program, swap_program

    root = tmp_path / "reclaimer"
    names = ("Alpha", "Beta", "Gamma")
    write_root(root, list(zip(names, free_ports(3))), 'backend = "attach"\n', staff(), exe=True)
    for name in names:
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(f"{name} old".encode())
    settings = load(root / "settings.toml")
    new = tmp_path / "project-reclaimer-dedicated-v0.9.19.exe"
    new.write_bytes(b"new program")
    real_replace = os.replace

    def replace(source: Any, target: Any) -> None:
        if str(source).endswith(".new") and Path(target).parent.name == "Beta":
            raise PermissionError(13, "Access is denied", str(target))
        real_replace(source, target)

    monkeypatch.setattr(serverupdates.os, "replace", replace)
    with pytest.raises(UpdateError, match=r"couldn't put the new program at .*Beta.*nothing was changed"):
        install_program(settings, new, "0.9.19")
    # All or nothing: Alpha (done first) is back on its old file too, and no half-written file is left.
    assert [(root / "SERVERS" / n / "project-reclaimer.exe").read_bytes() for n in names] == [
        b"Alpha old", b"Beta old", b"Gamma old"]
    assert not list(root.glob("SERVERS/*/*.new"))
    monkeypatch.setattr(serverupdates.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(
        OSError(28, "No space left on device")))
    with pytest.raises(UpdateError, match="No space left on device; the old one is as it was"):
        swap_program(settings, settings.server("Alpha"), new, "0.9.19")
    assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == b"Alpha old"
    assert not list(root.glob("SERVERS/*/*.new"))


def test_a_stopped_servers_program_older_than_the_release_is_offered_on_u(tmp_path: Path, monkeypatch, run) -> None:
    from types import SimpleNamespace

    from huragok.program import releases as serverupdates
    from testkit.manage import program_release, self_updating_root, serve_program

    root = self_updating_root(tmp_path, body='backend = "process"\n')  # both with auto_update off
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(name.encode())
    versions = {"Alpha": "0.9.20", "Beta": "0.9.15"}  # Beta's can't read its 0.9.20 dedicated.toml: never runs
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: versions[path.parent.name])
    serve_program(monkeypatch, program_release("0.9.20"), b"program 0.9.20")
    service, lifecycle, _ = program_service(root, clients={"Alpha": SimpleNamespace(version="0.9.20")})
    found = run(service.check(quiet=False))
    assert found is not None and service.newer is True and service.pending is found
    ok, message = run(service.install(when="after_game"))
    assert ok and message.startswith("server program 0.9.20 installed in 1 place(s); restarting Beta after game")
    assert (root / "SERVERS" / "Beta" / "project-reclaimer.exe").read_bytes() == b"program 0.9.20"
    assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == b"Alpha"
