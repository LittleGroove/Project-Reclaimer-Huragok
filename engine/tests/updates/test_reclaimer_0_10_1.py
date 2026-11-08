"""Project Reclaimer 0.10.1: a release whose program gives its version as 0.10.0, and 0.10's console lines.

The 0.10.1 program (project-reclaimer-dedicated-v0.10.1.exe, checksum as its release lists it) answers --version with
"project-reclaimer 0.10.0 (rcl-engine)" and its console signs in with version "0.10.0": only its release tag, which
its own updater goes by, says v0.10.1. Huragok 0.6.2 took that for "still on 0.10.0", installed 0.10.1 over itself
after the game and then said the server "runs another program file" (the host's servers, 2026-11-07). Huragok now
goes by the file's checksum (program/releases.py, "Which release a program file is").
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from conftest import ProgramLifecycle, wait_for

from huragok.core.events import EventBus
from huragok.core.settings import load
from huragok.program import releases
from huragok.servers.logs import parse_console_line
from huragok.servers.state import ServerState
from huragok.updates.program import OVERDUE, ProgramUpdateService
from testkit.manage import program_release, self_updating_root, serve_program

NEW = b"the 0.10.1 program, which says 0.10.0"
OLD = b"the 0.10.0 program"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_0101(published: float | None = None) -> releases.ProgramRelease:
    return dataclasses.replace(program_release("0.10.1", published=published or time.time() - 3 * 3600),
                               exe_sha256=digest(NEW))


def root_on(tmp_path: Path, program: bytes, *auto: str) -> Path:
    root = self_updating_root(tmp_path, *auto, body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(program)
    return root


def service_for(root: Path, names: tuple[str, ...] = ("Alpha", "Beta")) -> tuple[Any, Any, Any]:
    settings = load(root / "settings.toml")
    lifecycle, bus = ProgramLifecycle(up=names), EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {n: ServerState(n) for n in names}, {})  # type: ignore
    return service, lifecycle, bus


# Which release a file is ----------------------------------------------------------------------------------------

def test_githubs_checksum_of_the_program_is_read_with_the_release(monkeypatch) -> None:
    listing = {"tag_name": "v0.10.1", "html_url": "https://example/v0.10.1", "body": "", "published_at": None,
               "assets": [{"name": "project-reclaimer-dedicated-v0.10.1.exe", "size": 80359936,
                           "browser_download_url": "https://example/x.exe",
                           "digest": "sha256:56AA513E298B6B2BDA0F74ACBFCC6EC1A1C2311A3B83A1038D457E5173B1AC24"},
                          {"name": "SHA256SUMS.txt", "browser_download_url": "https://example/sums"}]}
    monkeypatch.setattr(releases, "_get", lambda *a, **k: json.dumps(listing).encode())
    monkeypatch.setattr(releases, "linux_download", lambda: None)
    release = releases.latest_program_release()
    assert release is not None and release.version == "0.10.1"
    assert release.exe_sha256 == "56aa513e298b6b2bda0f74acbfcc6ec1a1c2311a3b83a1038d457e5173b1ac24"
    listing["assets"][0]["digest"] = "md5:abc"  # anything else: none
    assert releases.latest_program_release().exe_sha256 is None


def test_a_file_is_known_by_its_checksum_and_kept_in_records(tmp_path: Path) -> None:
    root = root_on(tmp_path, NEW)
    settings = load(root / "settings.toml")
    program = root / "SERVERS" / "Alpha" / "project-reclaimer.exe"
    assert releases.release_of(settings, program) is None
    assert releases.remember_release(settings, digest(NEW), "0.10.1") is True
    assert releases.remember_release(settings, digest(NEW), "0.10.1") is False  # known already
    assert releases.release_of(settings, program) == "0.10.1"
    assert releases.file_version(settings, program) == "0.10.1"  # never asks the program, which would say 0.10.0
    kept = json.loads((settings.records / releases.IDENTITIES).read_text(encoding="utf-8"))
    assert kept == {"releases": {digest(NEW): "0.10.1"}, "says": {}}
    assert releases.remember_release(settings, digest(NEW), "0.10.1", says="0.10.0") is True  # adds what it says
    assert releases.known_says(settings)[digest(NEW)] == "0.10.0"
    assert releases.remember_release(settings, "not a checksum", "0.10.1") is False
    assert releases.remember_release(settings, digest(OLD), "not a version") is False
    # The real 0.10.1 program is known from the start, before Huragok has asked GitHub.
    assert releases.known_releases(settings)["56aa513e298b6b2bda0f74acbfcc6ec1a1c2311a3b83a1038d457e5173b1ac24"] \
        == "0.10.1"


def test_what_a_server_said_becomes_its_files_release_only_when_that_release_can_say_it(tmp_path: Path) -> None:
    root = root_on(tmp_path, NEW)
    settings = load(root / "settings.toml")
    program = root / "SERVERS" / "Alpha" / "project-reclaimer.exe"
    releases.remember_release(settings, digest(NEW), "0.10.1")
    assert releases.running_release(settings, program, "0.10.0") == "0.10.0"  # what the file says isn't known yet
    releases.remember_release(settings, digest(NEW), "0.10.1", says="0.10.0")
    assert releases.running_release(settings, program, "0.10.0") == "0.10.1"
    assert releases.running_release(settings, program, "0.10.1") == "0.10.1"
    assert releases.running_release(settings, program, "0.9.23") == "0.9.23"  # 0.10.1 put in place while 0.9 ran
    assert releases.running_release(settings, program, "0.10.0-beta.1") == "0.10.0-beta.1"  # not what the file says
    assert releases.running_release(settings, program, "0.10.2") == "0.10.2"  # newer than the file: not it
    assert releases.running_release(settings, program, "unknown") == "unknown"
    assert releases.running_release(settings, None, "0.10.0") == "0.10.0"
    other = root / "SERVERS" / "Beta" / "project-reclaimer.exe"
    other.write_bytes(OLD)  # a file Huragok doesn't know: what it said
    assert releases.running_release(settings, other, "0.10.0") == "0.10.0"


def test_a_copy_known_as_the_release_is_never_replaced_or_called_behind(tmp_path: Path, monkeypatch) -> None:
    root = root_on(tmp_path, NEW)
    settings = load(root / "settings.toml")
    monkeypatch.setattr(releases, "program_version", lambda settings, path: "0.10.0")  # what 0.10.1 answers
    program = root / "SERVERS" / "Alpha" / "project-reclaimer.exe"
    assert releases.as_new_as(settings, program, "0.10.1") is None  # unknown file: older, by what it says
    releases.remember_release(settings, digest(NEW), "0.10.1")
    assert releases.as_new_as(settings, program, "0.10.1") == "0.10.1"
    service, _, _ = service_for(root)
    assert service.copies_behind("0.10.1") == []


# The host's servers, 2026-11-07 ----------------------------------------------------------------------------------

def test_a_server_on_0101_saying_0100_is_on_the_newest_and_nobody_installs_it(tmp_path: Path, monkeypatch, run,
                                                                                caplog) -> None:
    root = root_on(tmp_path, NEW, "Alpha", "Beta")
    serve_program(monkeypatch, release_0101(), NEW)
    asked: list[Path] = []
    monkeypatch.setattr(releases, "program_version", lambda settings, path: asked.append(path) or "0.10.0")
    service, lifecycle, bus = service_for(root)

    async def scenario() -> None:
        # They sign in before Huragok has asked GitHub: 0.10.0 is all it knows yet.
        bus.emit("rcon.connected", "Alpha", version="0.10.0")
        bus.emit("rcon.connected", "Beta", version="0.10.0")
        await service.check()  # learns 0.10.1's checksum: both are on it
        assert service.versions == {"Alpha": "0.10.1", "Beta": "0.10.1"}
        assert not service.behind("Alpha") and service.note("Alpha") == ""
        assert service.review(now=time.time() + OVERDUE * 10) == []
        await service.check()
        assert len(asked) == 1  # the same program (one checksum) in both folders: asked once, then kept

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert lifecycle.restarts == [] and lifecycle.jobs == {} and lifecycle.flags == {}
    assert "[Alpha] Project Reclaimer 0.10.1: its program file is that release's (by its checksum), though its " \
           "console gives its version as 0.10.0" in caplog.text
    assert "still on" not in caplog.text and "runs another program file" not in caplog.text


def test_running_versions_and_the_newest_check_go_by_the_release(tmp_path: Path, monkeypatch, run) -> None:
    from types import SimpleNamespace

    root = root_on(tmp_path, NEW)
    releases.remember_release(load(root / "settings.toml"), digest(NEW), "0.10.1", says="0.10.0")
    serve_program(monkeypatch, release_0101(), NEW)
    clients = {"Alpha": SimpleNamespace(version="0.10.0"), "Beta": SimpleNamespace(version="0.10.0")}
    settings = load(root / "settings.toml")
    service = ProgramUpdateService(settings, EventBus(), ProgramLifecycle(), {n: ServerState(n) for n in clients},
                                   clients)  # type: ignore[arg-type]
    assert service.running_versions() == {"Alpha": "0.10.1", "Beta": "0.10.1"}
    assert run(service.check()) is None and service.newer is False  # nothing to install


def test_a_server_huragok_put_0101_in_place_for_is_done_not_running_another_file(tmp_path: Path, monkeypatch, run,
                                                                                  caplog) -> None:
    root = root_on(tmp_path, OLD, "Alpha")
    serve_program(monkeypatch, dataclasses.replace(release_0101(), exe_sha256=None), NEW)  # no checksum listed
    monkeypatch.setattr(releases, "program_version", lambda settings, path: "0.10.0")  # what 0.10.0 and 0.10.1 say
    service, lifecycle, bus = service_for(root, ("Alpha",))

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.10.0")  # really 0.10.0 this time
        await service.check()
        assert service.review(now=time.time() + OVERDUE + 1) == ["Alpha"]
        await wait_for(lambda: "program" in lifecycle.jobs.get("Alpha", {}))
        said = await lifecycle.jobs["Alpha"].pop("program").run()  # swapped while it's stopped
        assert said.startswith("Project Reclaimer 0.10.1 put in place at ")
        bus.emit("rcon.connected", "Alpha", version="0.10.0")  # back up, on 0.10.1, saying 0.10.0

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == NEW
    assert service.versions["Alpha"] == "0.10.1" and service.taken == {} and lifecycle.flags == {}
    assert "runs another program file" not in caplog.text
    assert "[Alpha] now on Project Reclaimer 0.10.1 (was 0.10.0)" in caplog.text


def test_a_flag_from_before_the_checksum_was_known_goes_once_it_is(tmp_path: Path, monkeypatch, run) -> None:
    root = root_on(tmp_path, NEW, "Alpha")
    service, lifecycle, bus = service_for(root, ("Alpha",))
    service.taken["Alpha"] = "0.10.1"
    lifecycle.set_flag("Alpha", "program", "! still on 0.10.0 after 0.10.1 was put in place")
    bus.emit("rcon.connected", "Alpha", version="0.10.0")  # the file isn't known yet: 0.10.0
    assert service.versions["Alpha"] == "0.10.0"
    serve_program(monkeypatch, release_0101(), NEW)
    monkeypatch.setattr(releases, "program_version", lambda settings, path: "0.10.0")
    run(service.check())
    assert service.versions["Alpha"] == "0.10.1" and service.taken == {} and lifecycle.flags == {}


def test_a_09_server_whose_file_is_0101_already_is_still_on_09(tmp_path: Path, monkeypatch, run) -> None:
    root = root_on(tmp_path, NEW, "Alpha")
    releases.remember_release(load(root / "settings.toml"), digest(NEW), "0.10.1", says="0.10.0")
    service, _, bus = service_for(root, ("Alpha",))
    bus.emit("rcon.connected", "Alpha", version="0.9.23")
    assert service.version_of("Alpha") == "0.9.23"


# 0.10's own updater, as its console says it --------------------------------------------------------------------

def test_a_0100_server_updating_itself_to_0101_is_seen_and_its_file_learned(tmp_path: Path, run, caplog) -> None:
    root = root_on(tmp_path, OLD, "Alpha")
    service, lifecycle, bus = service_for(root, ("Alpha",))
    settings = service.settings

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.10.0")
        for line in ("[update] automatic updates: checking now and hourly; an update restarts every server",
                     "[update] downloading Project Reclaimer 0.10.1",
                     "[update] Project Reclaimer 0.10.1 is in place: stopping every server to start it"):
            name, data = parse_console_line(line)
            bus.emit(name, "Alpha", **data)
        (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").write_bytes(NEW)  # what it put in place
        bus.emit("rcon.connected", "Alpha", version="0.10.0")
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert service.updater["Alpha"] == "on" and service.versions["Alpha"] == "0.10.1"
    assert releases.release_of(settings, root / "SERVERS" / "Alpha" / "project-reclaimer.exe") == "0.10.1"
    assert "[Alpha] now on Project Reclaimer 0.10.1 (was 0.10.0)" in caplog.text
    assert "program-old" in lifecycle.jobs["Alpha"]  # restarted off the .old file once nobody is on


def test_0100s_own_updater_off_and_failing_are_heard(tmp_path: Path, run, caplog) -> None:
    root = root_on(tmp_path, OLD, "Alpha")
    service, _, bus = service_for(root, ("Alpha",))
    name, data = parse_console_line("[update] auto_update is on, but this build has no release feed (a local build, "
                                    "or RECLAIMER_UPDATE_URL=off)")
    bus.emit(name, "Alpha", **data)
    assert service.updater["Alpha"] == "off"
    name, data = parse_console_line("[update] the automatic update failed, trying again in an hour: The update check "
                                    "failed: timed out")
    assert (name, data) == ("server.update_failed", {"reason": "The update check failed: timed out"})
    name, data = parse_console_line("[update] 0.10.1 was not put in place, the servers keep running: Access is denied")
    assert (name, data) == ("server.update_failed", {"version": "0.10.1", "reason": "Access is denied"})


def test_the_010_console_lines_are_read() -> None:
    lines = {
        '[server] "LG Test Server" on port 49176: Death Race on Road Course (pid 4312)':
            ("server.started", {"server": "LG Test Server", "port": 49176, "game": "Death Race on Road Course",
                                "pid": 4312}),
        '[server] "LG Test Server" stopped after 35 s (code -1073741819): starting again in 1 s. See H:\\x\\server.log':
            ("server.crashed", {"server": "LG Test Server", "uptime": "35 s", "reason": "code -1073741819",
                                "wait": "1 s", "kind": "other", "why": "code -1073741819"}),
        '[server] "LG Test Server" stopped answering: starting it again':
            ("server.crashed", {"server": "LG Test Server", "reason": "stopped answering", "kind": "hang",
                                "why": "stopped answering"}),
        '[server] "LG Test Server" ran out of memory again: it will keep failing to start until memory is freed.':
            ("server.crashed", {"server": "LG Test Server", "reason": "ran out of memory again", "kind": "memory",
                                "why": "ran out of memory again"}),
        '[server] "LG Test Server": the engine ended; starting it again with the next game':
            ("server.crashed", {"server": "LG Test Server", "reason": "the engine ended", "kind": "engine_exit",
                                "why": "the engine ended"}),
        "[reload] Configuration reloaded.": ("server.reloaded", {}),
        "[reload] Configuration reloaded: nothing changed.": ("server.reloaded", {}),
        "[server] every server is stopped": ("server.all_stopped", {}),
        "[update] Project Reclaimer 0.10.1 is in place: stopping every server to start it":
            ("server.updating", {"version": "0.10.1"}),
        "[update] downloading Project Reclaimer 0.10.1": ("server.update_downloading", {"version": "0.10.1"}),
        "[update] automatic updates: checking now and hourly; an update restarts every server":
            ("server.self_update", {"text": "automatic updates: checking now and hourly; an update restarts every "
                                            "server"}),
        "[workshop] 3830420359: installed": ("server.workshop", {"item": 3830420359, "text": "installed"}),
        "[workshop] 3830420359: downloading the version of 6 Nov 2026 (installed: 3 Nov 2026)":
            ("server.workshop", {"item": 3830420359, "text": "downloading the version of 6 Nov 2026 (installed: 3 Nov "
                                                             "2026)"}),
    }
    for line, expected in lines.items():
        assert parse_console_line(line) == expected, line
    # 0.9's, as before.
    assert parse_console_line("Update installed; restarting all servers.") == ("server.updating", {})
    assert parse_console_line("[Alpha] started (process 12); players join at 1.2.3.4:49176.")[0] == "server.started"
    # The servers' own lines in the console are server.log's, read there.
    assert parse_console_line("[LG Test Server] [server] game over: 1 players in the results") is None


def test_a_server_whose_new_file_waits_for_its_restart_is_still_on_what_it_runs(tmp_path: Path) -> None:
    root = root_on(tmp_path, NEW)
    settings = load(root / "settings.toml")
    releases.remember_release(settings, digest(NEW), "0.10.1", says="0.10.1")  # a release that says its own version
    service, _, bus = service_for(root, ("Alpha",))
    bus.emit("rcon.connected", "Alpha", version="0.10.0")  # 0.10.0 still runs; 0.10.1 starts at its restart
    assert service.version_of("Alpha") == "0.10.0" and service.behind("Alpha", "0.10.1")


# What the running server writes: status.json's health.version -------------------------------------------------

def write_status(path: Path, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "Alpha", "state": "lobby", **fields}), encoding="utf-8")


def test_the_release_a_running_server_writes_in_its_status_wins(tmp_path: Path, caplog) -> None:
    """0.10.1's status.json says `"health": {"version": "0.10.1", …}` (DEPLOY's Fresh Compare, 7 November 2026): the
    running process's own word for its release, even for a file Huragok doesn't know."""
    root = root_on(tmp_path, OLD)  # a program file Huragok has never seen
    service, _, bus = service_for(root, ("Alpha",))
    status = service.settings.server("Alpha").status_path
    write_status(status, pid=4312, updated=time.time(), health={"version": "0.10.1", "uptime_seconds": 38})
    with caplog.at_level(logging.INFO):
        bus.emit("rcon.connected", "Alpha", version="0.10.0")
    assert service.versions["Alpha"] == "0.10.1"
    assert "[Alpha] Project Reclaimer 0.10.1: its status.json says so, though its console gives its version as " \
           "0.10.0" in caplog.text
    service.states["Alpha"].pid = 4312  # the console named it as it started: the same process
    assert service.identify("Alpha", "0.10.0") == "0.10.1"


def test_a_status_from_before_the_restart_or_from_09_says_nothing(tmp_path: Path) -> None:
    root = root_on(tmp_path, OLD)
    service, _, _ = service_for(root, ("Alpha",))
    status = service.settings.server("Alpha").status_path
    write_status(status, pid=4312, updated=time.time() - 120, health={"version": "0.10.1"})  # a stopped process's
    assert service.identify("Alpha", "0.10.0") == "0.10.0"
    write_status(status, pid=4312, updated=time.time(), health={"version": "0.10.0"})
    service.states["Alpha"].pid = 5555  # restarted: the console named another process, which hasn't written yet
    assert service.identify("Alpha", "0.10.1") == "0.10.1"  # what its console said, not the old process's status
    write_status(status, pid=5555, updated=time.time())  # 0.9's status.json: no health block
    assert service.identify("Alpha", "0.9.23") == "0.9.23"
    write_status(status, pid=5555, updated=time.time(), health={"version": "nightly"})
    assert service.identify("Alpha", "0.10.0") == "0.10.0"
