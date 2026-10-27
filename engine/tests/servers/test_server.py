"""The per-server state machine, with a fake backend and a clock we move by hand."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time as real_time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import ALICE, free_port, free_ports, write_root, write_server_folder
from testkit.fleet import RESTARTS, SERVER, Clock, FakeBackend, FakeClient, build_world, cycle, world

from huragok.core import mailbox
from huragok.core.events import Event, EventBus
from huragok.core.jobs import StoppedJob
from huragok.core.settings import load
from huragok.servers import ports as ports_module
from huragok.servers import server as manager_module
from huragok.servers.backends import Backend, LifecycleError, make_backend
from huragok.servers.fleet import Fleet, Lifecycle
from huragok.servers.server import ServerRecord, daily_slot, next_daily, warning_text
from huragok.servers.state import Player, ServerState


async def bring_up(fleet: Fleet, manager: Any, clock: Clock, state: ServerState) -> None:
    fleet.boot()
    await cycle(manager, clock)
    state.connected = True
    await cycle(manager, clock)
    assert manager.phase == "running", manager.phase


def test_a_wanted_server_is_started_and_is_up_once_rcon_signs_in(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        fleet.boot()
        assert manager.record.wanted == "running"
        await cycle(manager, clock)
        assert manager.phase == "booting" and backend.starts == [SERVER] and client.woken == 1
        await cycle(manager, clock, 10)
        assert manager.phase == "booting"  # alive, but RCON hasn't signed in
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running" and manager.uptime is not None
        assert seen == ["server.ready"]  # the fake backend says nothing itself: ready once, nothing else
        row = manager.row(1, clock.now)
        assert row["status"].startswith("RUNNING") and row["color"] == "green" and row["can"]["restart"]

    run(scenario())


def test_the_countdown_ladder_warns_then_restarts(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def up() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running"

    async def scenario() -> None:
        await up()
        state.players[1] = Player(1, "Alice", ALICE)
        result = manager.restart(reason="testing")
        assert result.ok and "warning players" in result.message and manager.phase == "countdown"
        await cycle(manager, clock)
        assert client.says() == ["[o_O] PLEASE REMAIN CALM! Server restarting in 10 seconds."]
        await cycle(manager, clock, 5)
        assert client.says()[-1] == "[o_O] PLEASE REMAIN CALM! Server restarting in 5 seconds."
        await cycle(manager, clock, 3.5)  # several rungs fall due at once: only the smallest is said
        assert client.says()[-1] == "[o_O] PLEASE REMAIN CALM! Server restarting in 2 seconds." and len(client.says()) == 3
        assert "RESTART IN" in manager.row(1, clock.now)["status"]
        await cycle(manager, clock, 2)
        assert client.says()[-1] == "[-.-] PARDON OUR DUST! Server is restarting now: join again in a minute or two." and backend.stops == [SERVER]
        assert "server.restarting" in seen
        await cycle(manager, clock, 1)  # the stop is done, the process gone: started again
        assert manager.phase in ("booting", "running") and backend.starts == [SERVER, SERVER]
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running" and "server.restarted" in seen

    run(scenario())


def test_cancel_and_the_empty_shortcut(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        state.players[1] = Player(1, "Alice", ALICE)
        manager.stop()
        await cycle(manager, clock)
        assert client.says() == ["[o_O] PLEASE REMAIN CALM! Server shutting down in 10 seconds."] and "STOP IN" in manager.row(1)["status"]
        assert manager.cancel().ok and manager.phase == "running"
        await asyncio.sleep(0.01)  # the chat line is sent without waiting for it
        assert client.says()[-1] == "[^_^] The server shutdown has been cancelled. Keep it clean!" and manager.record.wanted == "running"
        assert "server.countdown_cancelled" in seen
        # Everyone leaves during a countdown: it ends after empty_seconds.
        manager.restart()
        await cycle(manager, clock)
        state.players.clear()
        await cycle(manager, clock, 1)
        assert manager.phase == "countdown"
        await cycle(manager, clock, 4)
        assert manager.phase in ("stopping", "booting", "launch") and backend.stops
        # Nobody on at all: no countdown.
        state.connected = True
        await cycle(manager, clock, 1)
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        before = len(backend.stops)
        assert manager.restart().message == "restarting now"
        assert manager.phase == "stopping"
        await asyncio.sleep(0.01)  # the stop runs as a task
        assert len(backend.stops) == before + 1

    run(scenario())


def test_a_crash_backs_off_then_faults(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        backend.up.clear()  # it died
        state.connected = False
        await cycle(manager, clock)
        assert manager.phase == "backoff" and manager.record.fail_count == 1
        assert "CRASHED - retry in" in manager.row(1, clock.now)["status"]
        assert "server.stopped" not in seen  # the fake backend has no events; the real one emits it
        await cycle(manager, clock, 5)
        assert manager.phase == "booting" and len(backend.starts) == 2
        backend.up.clear()  # died while starting
        await cycle(manager, clock)
        assert manager.phase == "faulted" and manager.record.faulted
        assert (settings.logs / "faulted-lg-test-server.txt").exists()
        assert "server.faulted" in seen and "FAULTED" in manager.row(1)["status"]
        # S starts it again, from zero failures.
        assert manager.start().ok and manager.phase == "launch" and manager.record.fail_count == 0
        await cycle(manager, clock)
        assert manager.phase == "booting"

    run(scenario())


def test_a_ban_list_the_program_cant_read_is_named_as_the_cause(world, run, caplog) -> None:
    """The simulation's finding 3 (26 October): the program refuses to start on a ban list it can't read, and the host
    read only "5 starts in a row failed". Its own line is recognised now: said once as it happens, and the fault's
    reason, with the file and where in it."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    console = ["──── 2026-10-26 12:00:00 UTC · started by the Superintendent · project-reclaimer.exe dedicated run ────",
               "Error: H:\\Hosts\\SERVERS\\LG Test Server\\../../bans.json is not a valid ban list: UUID parsing "
               "failed: invalid length: found 12 at line 9 column 29",
               "Error: Dedicated supervisor stopped (exit code: 1)"]
    backend.last_console_lines = lambda server, count=200: console[-count:]  # type: ignore[method-assign]

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        backend.up.clear()  # died while starting
        await cycle(manager, clock)
        assert manager.phase == "backoff"
        await cycle(manager, clock, 5)
        backend.up.clear()
        await cycle(manager, clock)
        assert manager.phase == "faulted"
        reason = manager.record.fault_reason
        assert "can't read its ban list" in reason and "line 9 column 29" in reason and "bans.json" in reason
        said = [r.message for r in caplog.records if "it won't start" in r.message]
        assert len(said) == 1  # once, not at every try

    run(scenario())


def test_known_cause_reads_the_programs_own_line() -> None:
    from huragok.servers.server import known_cause

    assert known_cause(["Starting…", "Error: D:\\x\\bans.json is not a valid ban list: expected value at line 1."]) \
        .startswith("the server program can't read its ban list (")
    assert known_cause(["Error: Dedicated supervisor stopped (exit code: 1)"]) == ""


def test_check_reads_a_ban_list_as_the_program_would(tmp_path: Path) -> None:
    import json

    from huragok.content.gamefiles import ban_list_problems

    good = {"players": [{"id": "a" * 64, "name": "Bravo", "reason": "x", "banned_by": "Owner (RCON)",
                         "banned_at": 1793013102, "group": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"}], "ips": []}
    path = tmp_path / "bans.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    assert ban_list_problems(path) == [] and ban_list_problems(tmp_path / "none.json") == []
    bad = json.loads(json.dumps(good))
    bad["players"][0]["group"] = "87d90988ab12"
    bad["players"].append({"id": "Bravo", "banned_at": "yesterday"})
    path.write_text(json.dumps(bad), encoding="utf-8")
    problems = ban_list_problems(path)
    assert any("entry 1: group '87d90988ab12' isn't a UUID" in p for p in problems)
    assert any("entry 2: id 'Bravo' isn't a player ID" in p for p in problems)
    assert any("entry 2: banned_at isn't a number" in p for p in problems)
    path.write_text("{\"players\": [", encoding="utf-8")
    assert "isn't JSON (line 1" in ban_list_problems(path)[0]


def test_a_record_that_cant_be_written_never_counts_a_crash_twice(world, run, monkeypatch, caplog) -> None:
    """Antivirus holding records/servers/<slug>.json for a moment: the crash is counted once and the step carries on
    (it used to stop half way and count the crash again at every tick until a write got through)."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    real_save = type(manager.record).save
    held = {"on": False}

    def save(record, path):
        if held["on"]:
            raise PermissionError(13, "in use by another process")
        real_save(record, path)

    monkeypatch.setattr(type(manager.record), "save", save)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        held["on"] = True
        backend.up.clear()  # it died, while its record can't be written
        state.connected = False
        for _ in range(3):
            await cycle(manager, clock)
        assert manager.phase == "backoff" and manager.record.fail_count == 1
        held["on"] = False
        await cycle(manager, clock, 5)
        assert manager.phase == "booting" and ServerRecord.load(manager.record_path).fail_count == 1

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert caplog.text.count("couldn't write") == 1  # said once, not at every tick


def test_faulted_is_a_pause_one_more_try_after_each_cool_down(world, run, caplog) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world  # max_failures = 2, retries [30, 60] min

    async def scenario() -> None:
        backend.fail_start = True
        fleet.boot()
        await cycle(manager, clock)
        await cycle(manager, clock, 5)
        assert manager.phase == "faulted" and manager.record.retry_at == pytest.approx(clock.now + 1800)
        row = manager.row(1, clock.now)
        assert row["status"].startswith("FAULTED - tries again in 30m") and "next try at" in row["detail"]
        assert "[S] starts it now" in row["detail"] and row["retry_at"] == manager.record.retry_at
        report = (settings.logs / "faulted-lg-test-server.txt").read_text(encoding="utf-8")
        assert "Next try    at" in report and "then every 1h" in report and "until a run lasts 10m" in report
        await cycle(manager, clock, 1799)
        assert manager.phase == "faulted" and backend.starts == []
        await cycle(manager, clock, 1)  # its cool-down is over: one try, which fails, so straight back
        assert manager.phase == "faulted" and manager.record.retries == 1
        assert manager.record.retry_at == pytest.approx(clock.now + 3600)  # the next cool-down: an hour
        assert seen.count("server.fault_retry") == 1
        backend.fail_start = False  # whatever it was is over
        await cycle(manager, clock, 3600)
        assert manager.phase == "booting" and backend.starts == [SERVER]
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running" and not manager.record.faulted and manager.record.retries == 2
        await cycle(manager, clock, 600)  # a run that lasts: back to normal
        assert manager.record.retries == 0 and manager.record.fail_count == 0
        backend.up.clear()  # a crash now gets the usual ladder, not FAULTED at once
        state.connected = False
        await cycle(manager, clock)
        assert manager.phase == "backoff"

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "STOPPED TRYING FOR NOW - it tries once more at" in caplog.text
    assert "trying again after FAULTED (try 1): 2 starts in a row" in caplog.text
    assert "back to normal after FAULTED: up 10m" in caplog.text


def test_a_person_wins_over_the_cool_down(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        backend.fail_start = True
        fleet.boot()
        await cycle(manager, clock)
        await cycle(manager, clock, 5)
        assert manager.phase == "faulted"
        manager.stop()  # X: it stays stopped, whatever the cool-down said
        await cycle(manager, clock, 3600)
        assert manager.phase == "idle" and "server.fault_retry" not in seen and backend.starts == []
        backend.fail_start = False
        assert manager.start().ok  # S: at once, from nothing
        assert manager.record.retry_at is None and manager.record.retries == 0 and manager.record.fail_count == 0
        await cycle(manager, clock)
        assert manager.phase == "booting"

    run(scenario())


def test_without_cool_downs_faulted_waits_for_a_person(tmp_path: Path, monkeypatch, run, caplog) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = build_world(
        tmp_path, monkeypatch, RESTARTS + "faulted_retry_minutes = []\n")

    async def scenario() -> None:
        backend.fail_start = True
        fleet.boot()
        await cycle(manager, clock)
        await cycle(manager, clock, 5)
        assert manager.phase == "faulted" and manager.record.retry_at is None
        await cycle(manager, clock, 86400)
        assert manager.phase == "faulted" and "server.fault_retry" not in seen

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "THE SERVER WILL NOT COME BACK ON ITS OWN" in caplog.text


def test_cool_downs_are_whole_minutes(tmp_path: Path) -> None:
    from huragok.core.tables import ConfigError

    for bad in ("[0]", "[5, 0.5]", '"30"', "[true]"):
        root = tmp_path / f"r{len(list(tmp_path.iterdir()))}"
        with pytest.raises(ConfigError, match="faulted_retry_minutes must be a list of minutes"):
            load(write_root(root, [(SERVER, free_port())], f"[restarts]\nfaulted_retry_minutes = {bad}\n"))
    default = load(write_root(tmp_path / "default", [(SERVER, free_port())], ""))
    assert default.restarts.faulted_retry_minutes == [30.0, 60.0]


def test_a_failed_start_command_counts_as_a_failure(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        backend.fail_start = True
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "backoff" and manager.record.fail_count == 1
        await cycle(manager, clock, 5)
        assert manager.phase == "faulted" and "program is missing" not in manager.record.fault_reason
        assert "2 starts in a row" in manager.record.fault_reason

    run(scenario())


def test_a_start_that_exits_sends_its_console_from_that_start(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    console = ["──── 2026-10-18 23:51:00 UTC · started by the Superintendent · old.exe dedicated run ────",
               "Error: an older run's problem",
               "──── 2026-10-18 23:51:24 UTC · started by the Superintendent · new.exe dedicated run ────",
               "Error: dedicated.toml: TOML parse error at line 47, column 1",
               "unknown field `auto_reload`, expected one of `game`, `content`"]
    backend.last_console_lines = lambda server, count=200: console[-count:]  # type: ignore[method-assign]
    failed: list[Any] = []
    fleet.bus.subscribe("server.start_failed", failed.append)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "booting"
        backend.up.discard(SERVER)  # it read its settings and stopped
        await cycle(manager, clock, 1)
        assert manager.phase == "backoff"

    run(scenario())
    assert failed and failed[0].get("why") == "the server exited while it was starting"
    assert failed[0].get("lines") == console[3:]


def test_too_many_automatic_restarts_fault(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        manager.record.restart_times = [clock.now - 100 * i for i in range(1, 4)]  # three in the last hour
        await lifecycle.restart(SERVER, reason="the server is struggling", countdown=0, by="Watchdog")
        assert manager.phase == "stopping" and len(manager.record.restart_times) == 4
        await cycle(manager, clock, 1)
        assert manager.phase == "faulted" and "over the budget of 3" in manager.record.fault_reason
        # The host fixes the cause and starts it (S), then restarts it (R): theirs aren't held to the budget, which is
        # for automatic restarts, so it doesn't fault again for the next hour.
        manager.start()
        await cycle(manager, clock, 1)
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        manager.restart(immediate=True)
        await cycle(manager, clock, 1)
        assert manager.phase in ("booting", "running") and not manager.record.fault_reason

    run(scenario())


def test_the_daily_restart_fires_once_in_its_window(world, run, monkeypatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.restarts.daily_restart = "04:00"
    settings.restarts.min_uptime_seconds = 0

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        at = datetime.fromtimestamp(clock.now).replace(hour=3, minute=0, second=0)
        clock.now = at.timestamp()
        await cycle(manager, clock)
        assert manager.phase == "running"  # before the slot
        clock.now = at.replace(hour=4, minute=10).timestamp()
        await cycle(manager, clock)
        assert manager.record.daily_last_fired == at.strftime("%Y-%m-%d")
        assert manager.stop_label == "daily restart" and backend.stops == [SERVER]  # nobody on: no countdown
        # Back up, later the same day: not again.
        for _ in range(3):
            state.connected = True
            await cycle(manager, clock, 1)
        clock.now = at.replace(hour=4, minute=20).timestamp()
        await cycle(manager, clock)
        assert manager.phase == "running"
        # Opened at noon: the slot is missed, not fired.
        manager.record.daily_last_fired = ""
        clock.now = at.replace(hour=12).timestamp()
        await cycle(manager, clock)
        assert manager.phase == "running"
        assert "daily restart 04:00" in manager.next_daily_text()

    run(scenario())


def test_a_daily_restart_close_to_midnight_is_still_due_just_after_it(world, run) -> None:
    """23:45, and the server was busy (or not up long enough) until 00:05: yesterday's slot is still inside its
    window, so it fires, once, kept by its own date."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.restarts.daily_restart = "23:45"
    settings.restarts.min_uptime_seconds = 0

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        evening = datetime.fromtimestamp(clock.now).replace(hour=23, minute=40, second=0)
        clock.now = evening.timestamp()
        await cycle(manager, clock)
        assert manager.phase == "running"  # before the slot
        clock.now = (evening + timedelta(minutes=25)).timestamp()  # 00:05, the next day
        await cycle(manager, clock)
        assert manager.record.daily_last_fired == evening.strftime("%Y-%m-%d")  # the slot's own date
        assert manager.stop_label == "daily restart"

    run(scenario())


def test_a_running_server_is_adopted_not_restarted(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.servers[0].autostart = False
    backend.up.add(SERVER)
    backend.started_times[SERVER] = clock.now - 3600

    async def scenario() -> None:
        fleet.boot()
        assert manager.record.wanted == "running"  # running already: looked after, not stopped
        await cycle(manager, clock)
        assert manager.phase == "running" and backend.starts == [] and "server.adopted" in seen
        assert manager.row(1)["status"].startswith("RUNNING  up 1h")

    run(scenario())


def test_a_server_running_elsewhere_is_external(world, run, monkeypatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    monkeypatch.setattr(fleet.ports, "in_use", lambda server: True)
    status = manager.server.status_path  # started by hand from its own folder: it writes that folder's status.json
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text("{}", encoding="utf-8")
    os.utime(status, (clock.now, clock.now))  # written just now, by the clock the manager reads

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "external" and backend.starts == []
        assert "started some other way" in manager.start().message
        assert "wasn't started from here" in manager.restart().message
        row = manager.row(1, clock.now)
        assert "not started from here" in row["status"] and not row["can"]["stop"]
        monkeypatch.setattr(fleet.ports, "in_use", lambda server: False)
        await cycle(manager, clock)
        assert manager.phase == "booting" and backend.starts == [SERVER]  # wanted: started once the port is free

    run(scenario())


def test_a_copy_whose_original_holds_its_port_waits_and_is_never_taken_for_it(world, run, monkeypatch) -> None:
    """A server folder copied from another Huragok folder, whose original still runs there on the same port: what
    answers on the port writes no status.json here, so it isn't this server. It waits, says why, and starts once
    the port is free; nothing is counted as a failed start."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    monkeypatch.setattr(fleet.ports, "in_use", lambda server: True)
    manager.server.status_path.unlink(missing_ok=True)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "idle" and manager.waiting_kind == "port" and backend.starts == []
        row = manager.row(1, clock.now)
        assert row["status"] == "WAITING" and "in use by something that isn't this server" in row["detail"]
        assert manager.record.fail_count == 0
        monkeypatch.setattr(fleet.ports, "in_use", lambda server: False)
        await cycle(manager, clock)
        assert manager.waiting_for == "" and manager.phase == "booting" and backend.starts == [SERVER]

    run(scenario())


def test_a_server_waits_for_what_it_lacks_and_starts_once_it_has_it(world, run) -> None:
    """The check before a start (repair.GameFiles.prelaunch): while it says what's missing the server waits as
    WAITING, looks again every WAIT_RECHECK seconds, never counts a failed start; release() looks again at once."""
    from huragok.servers import server as manager_module

    clock, settings, backend, fleet, manager, client, state, seen = world
    lacking = ["getting Halo 3's files: copying them from MCC"]
    asked: list[str] = []
    fleet.prelaunch = lambda server: (asked.append(server.name), lacking[0] if lacking else None)[1]

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "idle" and backend.starts == [] and asked == [SERVER]
        row = manager.row(1, clock.now)
        assert row["status"] == "WAITING" and row["color"] == "cyan" and "copying them from MCC" in row["detail"]
        await cycle(manager, clock)
        assert asked == [SERVER]  # not asked again before WAIT_RECHECK
        manager.waiting_checked -= manager_module.WAIT_RECHECK
        lacking[0] = "needs Halo 3's files: MCC isn't on this PC; press I"
        await cycle(manager, clock)
        assert len(asked) == 2 and manager.row(1, clock.now)["color"] == "yellow"
        manager.record.fail_count = 2
        lacking.clear()
        fleet.release()  # the files arrived
        assert manager.record.fail_count == 0
        await cycle(manager, clock)
        assert manager.phase == "booting" and backend.starts == [SERVER] and manager.waiting_for == ""

    run(scenario())


def test_a_mistake_in_the_check_before_a_start_never_keeps_a_server_down(world, run, caplog) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    def broken(server: Any) -> str | None:
        raise RuntimeError("a bug")

    fleet.prelaunch = broken

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "booting" and backend.starts == [SERVER]

    with caplog.at_level(logging.ERROR):
        run(scenario())
    assert "starting it anyway" in caplog.text


def test_a_planned_restart_waits_for_the_game_then_gives_short_notice(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        state.players[1] = Player(1, "Alice", ALICE)
        state.phase, state.map, state.mode = "playing", "Valhalla", "Slayer"
        plan = await lifecycle.restart(SERVER, when="after_game", reason="performance")
        assert plan.when == "after_game" and lifecycle.planned[SERVER] is plan
        await asyncio.sleep(0.01)
        assert client.says() == ["[o_o] CONSTRUCTION AHEAD! EXPECT DELAYS! This server restarts after this game: performance."]
        from huragok.core.events import Event

        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "playing", "map": "Valhalla", "mode": "Slayer"}))
        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "finished", "map": "Valhalla", "mode": "Slayer"}))
        assert manager.phase == "countdown" and manager.planned is None
        await cycle(manager, clock)
        assert client.says()[-1] == "[o_O] PLEASE REMAIN CALM! Server restarting in 3 seconds."
        assert lifecycle.cancel(SERVER) and manager.phase == "running"

    run(scenario())


def test_an_admins_restart_warns_everyone_and_isnt_counted_as_automatic(world, run) -> None:
    """!restartserver: the countdown the window's R gives, the reason in it, the admin in the history."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        await bring_up(fleet, manager, clock, state)
        state.players[1] = Player(1, "Alice", ALICE)
        manager.record.restart_times = [clock.now - 100 * i for i in range(1, 4)]  # the automatic budget used up
        await lifecycle.restart(SERVER, reason="lag (requested by Admin)", by="Admin", manual=True)
        assert manager.phase == "countdown" and len(manager.record.restart_times) == 3
        assert manager.record.history[-1][1:] == ["restart", "lag (requested by Admin) (by Admin)"]
        await cycle(manager, clock)
        assert client.says() == ["[o_O] PLEASE REMAIN CALM! Server restarting in 10 seconds."]
        for _ in range(4):
            await cycle(manager, clock, 3)
        assert client.says()[-1] == "[-.-] PARDON OUR DUST! Server is restarting now: join again in a minute or two." and backend.stops == [SERVER]
        await cycle(manager, clock, 1)
        assert manager.phase in ("booting", "running") and backend.starts == [SERVER, SERVER]  # not faulted

    run(scenario())


def test_a_restart_now_replaces_one_planned_after_the_game(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        state.players[1] = Player(1, "Alice", ALICE)
        state.phase, state.map, state.mode = "playing", "Valhalla", "Slayer"
        await lifecycle.restart(SERVER, when="after_game", reason="the server is struggling")
        assert lifecycle.planned[SERVER]
        await lifecycle.restart(SERVER, when="now", reason="nobody can join", countdown=30)
        assert manager.phase == "countdown" and SERVER not in lifecycle.planned  # not again after the next game

    run(scenario())


def test_a_job_runs_while_the_server_is_stopped(world, run) -> None:
    """A Workshop update: restart after the game, swap the mod in between the stop and the start."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)
    from huragok.core.jobs import StoppedJob

    ran: list[tuple[str, bool]] = []

    async def swap_mod() -> str:
        ran.append(("swap", SERVER in backend.up))  # it must be stopped by now
        await asyncio.sleep(0)
        return "Workshop updated: Hugegrass 13 Oct -> 18 Oct"

    async def broken() -> str:
        raise RuntimeError("disk full")

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running"
        lifecycle.add_job(SERVER, StoppedJob("broken", "a job that fails", broken))
        lifecycle.add_job(SERVER, StoppedJob("workshop", "installing new Workshop versions", swap_mod))
        lifecycle.set_flag(SERVER, "workshop", "mod update waiting: Hugegrass")
        assert "mod update waiting: Hugegrass" in manager.row(1)["detail"]
        lifecycle.set_flag(SERVER, "steamcmd", "! SteamCMD sign-in failed: press I to sign in again")
        assert manager.row(1)["detail"].startswith("! SteamCMD")  # first, so the dashboard shows the line red
        lifecycle.set_flag(SERVER, "steamcmd", None)
        await lifecycle.restart(SERVER, when="now", reason="installing a new version of Hugegrass", countdown=0)
        state.connected = False
        for _ in range(4):
            await cycle(manager, clock, 1)
        assert ran == [("swap", False)] and not manager.jobs  # ran once, with the server stopped
        assert backend.starts == [SERVER, SERVER] and manager.phase == "booting"  # and then it started again
        assert "preparing" in manager_module.BUSY

    run(scenario())


def test_records_and_helpers(tmp_path: Path) -> None:
    record = ServerRecord(wanted="running", fail_count=2, restart_times=[1.0, 2.0], daily_last_fired="2026-10-16")
    record.save(tmp_path / "r.json")
    loaded = ServerRecord.load(tmp_path / "r.json")
    assert loaded == record
    assert ServerRecord.load(tmp_path / "missing.json") == ServerRecord()
    assert warning_text(300, "daily restart", True) == "[o_O] PLEASE REMAIN CALM! Server restarting in 5 minutes: daily restart."
    assert warning_text(30, "daily restart", True) == "[o_O] PLEASE REMAIN CALM! Server restarting in 30 seconds."
    assert warning_text(60, "", False) == "[o_O] PLEASE REMAIN CALM! Server shutting down in 1 minute."
    now = datetime(2026, 10, 16, 12, 0)
    assert daily_slot("", now) is None
    assert daily_slot("04:00", now) == datetime(2026, 10, 16, 4, 0)
    assert next_daily("04:00", now) == datetime(2026, 10, 17, 4, 0)
    assert next_daily("23:30", now) == datetime(2026, 10, 16, 23, 30)


# Stopped servers and the event loop ---------------------------------------------------------------

class QuietClient:
    connected = False
    stopped_reason = None

    def wake(self) -> None:
        pass


def test_a_tick_with_stopped_servers_never_waits_on_their_ports(tmp_path: Path, run, monkeypatch) -> None:
    """Asked by connecting, each stopped server held the loop for as long as Windows takes to refuse a connect to a
    closed port (all of 0.5 s): three stopped servers stalled every other server's RCON, the parts and the window by
    1.5 s a second."""
    names = ["Idle 1", "Idle 2", "Idle 3"]
    body = "".join(f'[[server]]\nname = "{n}"\nautostart = false\n' for n in names)
    settings = load(write_root(tmp_path / "root", list(zip(names, free_ports(3))), body, backend="process", exe=True))
    bus = EventBus()
    states = {s.name: ServerState(s.name) for s in settings.servers}
    fleet = Fleet(settings, make_backend(settings, bus, states), bus)
    for server in settings.servers:
        fleet.add(server, QuietClient(), states[server.name])  # type: ignore[arg-type]

    async def scenario() -> list[float]:
        fleet.boot()
        took = []
        for _ in range(3):
            started = real_time.perf_counter()
            fleet.tick()
            took.append(real_time.perf_counter() - started)
            await asyncio.sleep(0.01)
        return took

    took = run(scenario())
    assert max(took) < 0.2, took
    assert [m.phase for m in fleet.managers.values()] == ["idle"] * 3

    # Where the system's socket table can't be read, a connect runs in a thread: the tick still doesn't wait,
    # and until the answer is in, a wanted server isn't started (it might be onto a taken port).
    monkeypatch.setattr(ports_module, "listening_ports", lambda: None)

    def slow_connect(host: str, port: int, timeout: float) -> bool:
        real_time.sleep(0.3)
        return True

    monkeypatch.setattr(ports_module, "_connects", slow_connect)
    first = fleet.managers["Idle 1"]
    first.record.wanted = "running"
    fresh = Fleet(settings, fleet.backend, bus)  # a new watch: nothing known yet
    first.fleet = fresh

    async def unknown() -> None:
        started = real_time.perf_counter()
        first.tick(real_time.time())
        assert real_time.perf_counter() - started < 0.1 and first.phase == "idle"
        for _ in range(50):
            await asyncio.sleep(0.02)
            first.tick(real_time.time())
            if first.phase != "idle" or first.waiting_kind:
                break
        # The probe found something on its port that writes no status.json here: never started over it, and never
        # taken for this server either.
        assert first.phase == "idle" and first.waiting_kind == "port"

    run(unknown())


# A plan belongs to the run it was made for --------------------------------------------------------------

def test_a_planned_restart_goes_with_the_daily_restart_that_did_it(world, run) -> None:
    """A Workshop update planned for after the game at 03:50; the daily restart at 04:00 does it (and runs the
    update's job). The plan must not be left over to restart the server again, with a full countdown, at 04:20."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.restarts.daily_restart = "04:00"
    settings.restarts.min_uptime_seconds = 0
    lifecycle = Lifecycle(fleet)
    ran: list[bool] = []

    async def swap() -> str:
        ran.append(SERVER in backend.up)
        return "swapped"

    async def scenario() -> None:
        at = datetime.fromtimestamp(clock.now).replace(hour=3, minute=50, second=0)
        clock.now = at.timestamp()
        await bring_up(fleet, manager, clock, state)
        state.players[1] = Player(1, "Alice", ALICE)
        state.phase, state.map, state.mode = "playing", "Valhalla", "Slayer"
        lifecycle.add_job(SERVER, StoppedJob("workshop", "installing new Workshop versions", swap))
        plan = await lifecycle.restart(SERVER, when="after_game", reason="installing new Workshop versions")
        assert plan.requested == clock.now  # stamped from the manager's clock
        clock.now = at.replace(hour=4, minute=0, second=5).timestamp()
        await cycle(manager, clock)
        assert manager.phase == "countdown" and manager.countdown and manager.countdown.label == "daily restart"
        for _ in range(12):
            await cycle(manager, clock, 1)
        state.players.clear()
        for _ in range(3):
            await cycle(manager, clock, 1)
        assert manager.phase == "running" and len(backend.starts) == 2 and ran == [False]
        assert manager.planned is None  # the daily restart did what it was for
        state.players[1] = Player(1, "Alice", ALICE)
        clock.now = at.replace(hour=4, minute=21).timestamp()  # past the plan's half-hour limit
        await cycle(manager, clock)
        assert manager.phase == "running" and manager.countdown is None

    run(scenario())


def test_after_a_crash_the_new_runs_first_lobby_isnt_the_planned_games_end(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        await bring_up(fleet, manager, clock, state)
        state.players[1] = Player(1, "Alice", ALICE)
        state.phase, state.map, state.mode = "playing", "Valhalla", "Slayer"
        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "playing", "map": "Valhalla", "mode": "Slayer"}))
        await lifecycle.restart(SERVER, when="after_game", reason="updating to 0.9.21")
        backend.up.clear()  # it crashed mid-game
        state.connected = False
        state.players.clear()
        await cycle(manager, clock, 1)
        assert manager.phase == "backoff" and manager.planned is None
        await cycle(manager, clock, 6)
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        state.players[1] = Player(1, "Alice", ALICE)
        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "no_game", "map": "Guardian", "mode": "Slayer"}))
        assert manager.phase == "running" and manager.countdown is None  # not restarted again on coming back

    run(scenario())


# Jobs before a start -------------------------------------------------------------------------------

def test_a_job_that_adds_itself_back_doesnt_keep_the_server_stopped(world, run) -> None:
    """`dedicated mods` failing made the Workshop job add itself back while it ran; the manager ran it again and
    again instead of starting, and the server stayed in PREPARING for good."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)
    ran: list[str] = []

    async def again() -> str:
        ran.append("ran")
        lifecycle.add_job(SERVER, StoppedJob("workshop", "installing new Workshop versions", again))
        return ""

    async def scenario() -> None:
        await bring_up(fleet, manager, clock, state)
        lifecycle.add_job(SERVER, StoppedJob("workshop", "installing new Workshop versions", again))
        await lifecycle.restart(SERVER, when="now", reason="new Workshop versions", countdown=0)
        state.connected = False
        for _ in range(4):
            await cycle(manager, clock, 1)
        assert ran == ["ran"] and manager.phase == "booting" and len(backend.starts) == 2
        assert [job.key for job in manager.jobs] == ["workshop"]  # it waits for the next start

    run(scenario())


def test_a_job_that_hangs_is_given_up_on_and_a_stop_calls_the_jobs_off(world, run, monkeypatch, caplog) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    monkeypatch.setattr(manager_module, "JOB_LIMIT", 0.05)
    lifecycle = Lifecycle(fleet)

    async def hangs() -> str:
        await asyncio.sleep(3600)
        return ""

    async def preparing() -> None:
        lifecycle.add_job(SERVER, StoppedJob("workshop", "installing new Workshop versions", hangs))
        await lifecycle.restart(SERVER, when="now", reason="new Workshop versions", countdown=0)
        state.connected = False
        await cycle(manager, clock, 1)  # stopped, then the job starts
        assert manager.phase == "preparing"

    async def scenario() -> None:
        await bring_up(fleet, manager, clock, state)
        await preparing()
        await asyncio.sleep(0.15)
        await cycle(manager, clock, 1)
        assert manager.phase == "booting" and len(backend.starts) == 2 and not manager.jobs  # started anyway
        state.connected = True
        await cycle(manager, clock, 1)
        monkeypatch.setattr(manager_module, "JOB_LIMIT", 3600.0)
        await preparing()
        result = manager.stop()
        assert result.ok and "called off" in result.message and manager.phase == "idle"
        await cycle(manager, clock, 1)
        assert manager.phase == "idle" and manager.record.wanted == "stopped" and len(backend.starts) == 2
        assert [job.key for job in manager.jobs] == ["workshop"]  # it runs at the next start

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "installing new Workshop versions took longer than 0s; starting it anyway" in caplog.text


# Stops that don't stop -----------------------------------------------------------------------------

def test_a_stop_that_leaves_the_process_running_isnt_called_a_stop(world, run, caplog) -> None:
    """The process backend raises when the process outlives the stop. The server is still up: before, the manager
    gave up waiting, called it stopped, and picked it up again as running a tick later (the stop silently undone,
    `stop everything` told it was done, a restart reported as done)."""
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def stop_that_fails(server: Any, timeout: float | None = None) -> None:
        raise LifecycleError("couldn't end process 4242: it's still running after being told to stop and then ended")

    finished: list[bool] = []
    fleet.on_all_stopped = lambda: finished.append(True)

    async def scenario() -> None:
        await bring_up(fleet, manager, clock, state)
        backend.stop = stop_that_fails  # type: ignore[method-assign]
        fleet.stop_all(immediate=True)
        for _ in range(3):
            clock.now += 1
            fleet.tick(clock.now)
            await asyncio.sleep(0.01)
        assert manager.phase == "running" and manager.record.wanted == "stopped"
        assert finished and fleet.unstoppable() == [SERVER]  # told, not left waiting for ever
        assert manager.row(1, clock.now)["detail"].startswith("! couldn't stop it")
        assert "server.adopted" not in seen
        assert manager.restart(immediate=True).ok
        for _ in range(3):
            await cycle(manager, clock, 1)
        assert manager.phase == "running" and "server.restarted" not in seen

    with caplog.at_level(logging.ERROR):
        run(scenario())
    assert "couldn't stop it: couldn't end process 4242" in caplog.text and "the stop didn't happen" in caplog.text
    assert "the restart didn't happen" in caplog.text


# Requests from outside ---------------------------------------------------------------------------------

def test_a_malformed_request_is_answered_and_the_fleet_carries_on(world, run) -> None:
    """One request file of the wrong shape escaped the fleet's tick; restarted, Fleet.run booted every manager again:
    a server that doesn't start by itself, in backoff after a crash, was then left down."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.servers[0].autostart = False
    folder = settings.records / "requests"

    async def scenario() -> None:
        fleet.boot()
        assert manager.start().ok
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        backend.up.clear()  # it crashed: tried again after a backoff
        state.connected = False
        await cycle(manager, clock, 1)
        assert manager.phase == "backoff" and manager.record.wanted == "running"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "abc.json").write_text(json.dumps({"verb": "status", "time": "now"}), encoding="utf-8")
        (folder / "def.json").write_text("[1, 2]", encoding="utf-8")
        os.utime(folder / "def.json", (real_time.time() - 60, real_time.time() - 60))
        fleet.tick(clock.now)  # doesn't raise
        reply = mailbox.take_reply(settings.records, "abc")
        assert reply is not None and not reply.ok and "time must be a number" in reply.message
        reply = mailbox.take_reply(settings.records, "def")
        assert reply is not None and not reply.ok and "not understood" in reply.message
        task = asyncio.ensure_future(fleet.run())  # as the supervisor restarts it after a failure
        await asyncio.sleep(0.05)
        task.cancel()
        assert manager.record.wanted == "running" and manager.phase == "backoff"  # not booted again

    run(scenario())


def test_the_first_server_found_while_running_is_booted(tmp_path: Path, run) -> None:
    root = tmp_path / "root"
    settings = load(write_root(root, []))  # Huragok started with no servers yet
    bus = EventBus()
    backend = FakeBackend(settings, bus, {})
    fleet = Fleet(settings, backend, bus)
    fleet.boot()
    for name, port in zip(("First", "Second"), free_ports(2)):
        write_server_folder(root / "SERVERS" / name, port, name=name)
    for server in load(root / "settings.toml").servers:
        state = ServerState(server.name)
        backend.states[server.name] = state
        fleet.add(server, FakeClient(state), state)  # type: ignore[arg-type]

    async def scenario() -> None:
        for _ in range(3):
            fleet.tick()
            await asyncio.sleep(0.01)

    run(scenario())
    assert [(m.booted, m.record.wanted) for m in fleet.managers.values()] == [(True, "running")] * 2
    assert sorted(backend.starts) == ["First", "Second"]


def test_restarting_every_server_leaves_the_stopped_ones_alone(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        manager.record.faulted = True
        manager._set_phase("faulted")
        (name, result), = await fleet.verb("restart", "all")
        assert result.ok and result.message == "not running, so not restarted" and manager.phase == "faulted"
        (name, result), = await fleet.verb("restart", SERVER)  # named: restarting it starts it, as before
        assert result.ok and manager.phase == "launch"

    run(scenario())


def test_a_server_is_found_by_name_before_its_place_in_the_list(tmp_path: Path) -> None:
    ports = free_ports(3)
    settings = load(write_root(tmp_path / "root", [("2", ports[0]), ("Alpha", ports[1]), ("Beta Server", ports[2])]))
    bus = EventBus()
    states = {s.name: ServerState(s.name) for s in settings.servers}
    fleet = Fleet(settings, FakeBackend(settings, bus, states), bus)
    for server in settings.servers:
        fleet.add(server, FakeClient(states[server.name]), states[server.name])  # type: ignore[arg-type]
    assert list(fleet.managers) == ["2", "Alpha", "Beta Server"]
    found = {ref: (m.name if (m := fleet.find(ref)) else None) for ref in ("2", "#2", "3", "#3", "4", "alpha",
                                                                         "beta-server", "BETA SERVER", "nope")}
    assert found == {"2": "2", "#2": "Alpha", "3": "Beta Server", "#3": "Beta Server", "4": None, "alpha": "Alpha",
                     "beta-server": "Beta Server", "BETA SERVER": "Beta Server", "nope": None}


# A backend with only a restart command (and a stop) --------------------------------------------------------

class RestartCommandBackend(FakeBackend):
    restart_only = True

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.restarts = 0
        self.gate = asyncio.Event()

    async def restart(self, server: Any) -> None:
        self.restarts += 1
        await self.gate.wait()


def test_a_stop_during_the_restart_command_is_honoured_and_the_budget_applies(tmp_path, monkeypatch, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = build_world(
        tmp_path, monkeypatch, backend_type=RestartCommandBackend)
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        backend.gate = asyncio.Event()
        await bring_up(fleet, manager, clock, state)
        assert manager.restart(immediate=True).ok and manager.phase == "restarting"
        result = manager.stop()
        assert result.ok and "stopped once that's done" in result.message
        backend.gate.set()
        for _ in range(2):
            await cycle(manager, clock, 1)
        assert manager.phase == "idle" and manager.record.wanted == "stopped" and backend.stops == [SERVER]
        # Over the hourly budget: not restarted again (nothing could stop a loop of them otherwise).
        assert manager.start().ok
        for _ in range(2):
            await cycle(manager, clock, 1)
        assert manager.phase == "running"
        manager.record.restart_times = [clock.now - 100 * i for i in range(1, 4)]
        await lifecycle.restart(SERVER, reason="the server is struggling", countdown=0, by="Watchdog")
        assert manager.phase == "running" and backend.restarts == 1

    run(scenario())


# Without a process to watch --------------------------------------------------------------------------------

class InferredBackend(Backend):
    """docker or command: there's no process to watch, so whether it's up is inferred from RCON and status.json."""

    name = "docker"
    can_start = can_stop = can_restart = True
    sees_processes = False

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.calls: list[str] = []

    async def start(self, server: Any) -> None:
        self.calls.append("start")  # `docker compose up -d`: nothing happens to a container still running

    async def stop(self, server: Any, timeout: float | None = None) -> None:
        self.calls.append("stop")

    async def revive(self, server: Any) -> None:
        self.calls.append("revive")


def test_without_a_process_to_watch_a_hung_server_is_restarted_not_just_started(tmp_path, monkeypatch, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = build_world(
        tmp_path, monkeypatch, backend_type=InferredBackend)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert backend.calls == ["start"]  # nothing seemed to be running: a start
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        state.connected = False  # it hangs: RCON drops and status.json isn't written any more
        await cycle(manager, clock, 1)
        assert manager.phase == "backoff"
        await cycle(manager, clock, 5)
        assert manager.phase == "booting" and backend.calls == ["start", "revive"]
        await cycle(manager, clock, 31)  # it never answers: its stop command runs
        assert backend.calls == ["start", "revive", "stop"] and manager.phase == "faulted"
        assert manager.start().ok
        await cycle(manager, clock)
        assert backend.calls[-1] == "revive"  # S after it faulted: restarted too

    run(scenario())


# Clocks ------------------------------------------------------------------------------------------------------

def test_the_clock_being_set_doesnt_fail_a_start_or_change_a_wait(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "booting"
        clock.step(600)  # set at boot: ten minutes on
        await cycle(manager, clock)
        assert manager.phase == "booting"  # not "never answered within 30s"
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        state.players[1] = Player(1, "Alice", ALICE)
        manager.restart(reason="testing")
        clock.step(3600)
        await cycle(manager, clock)
        assert manager.phase == "countdown" and manager.countdown and manager.countdown.step == "ladder"
        manager.cancel()
        backend.up.clear()
        state.connected = False
        await cycle(manager, clock)
        assert manager.phase == "backoff"
        clock.step(-3600)  # an hour back
        await cycle(manager, clock, 5)
        assert manager.phase == "booting"  # the 5 s backoff took 5 s

    run(scenario())


@pytest.mark.skipif(not hasattr(real_time, "tzset"), reason="sets the time zone with time.tzset (not on Windows)")
def test_a_daily_restart_in_the_hour_the_clocks_skip_still_happens(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.restarts.daily_restart = "01:30"
    settings.restarts.min_uptime_seconds = 0
    before = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    real_time.tzset()
    try:
        async def scenario() -> None:
            clock.now = datetime(2026, 3, 29, 0, 50).timestamp()  # 01:00 GMT is 02:00 BST that night
            await bring_up(fleet, manager, clock, state)
            for _ in range(24):
                await cycle(manager, clock, 300)
                if manager.record.daily_last_fired:
                    break
            assert manager.record.daily_last_fired == "2026-03-29"

        run(scenario())
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        real_time.tzset()


# Failures in a row --------------------------------------------------------------------------------------------

def test_a_good_run_clears_the_failures_before_it(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def died() -> None:
        backend.up.clear()
        state.connected = False
        await cycle(manager, clock)

    async def back() -> None:
        await cycle(manager, clock, 5)
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"

    async def scenario() -> None:
        await bring_up(fleet, manager, clock, state)
        await died()  # right after starting: failure 1 of 2
        assert manager.phase == "backoff" and manager.record.fail_count == 1
        await back()
        assert manager.record.fail_count == 1
        await cycle(manager, clock, 600)  # up for good_run_seconds
        assert manager.record.fail_count == 0
        await died()  # after a long run: failure 1 again, not the second in a row
        assert manager.phase == "backoff" and manager.record.fail_count == 1
        await back()  # up 1 s
        await cycle(manager, clock, 598)
        assert manager.record.fail_count == 1
        clock.now += 2
        await died()  # dies on the tick it passes good_run_seconds: the crash itself clears what came before
        assert manager.phase == "backoff" and manager.record.fail_count == 1

    run(scenario())


@pytest.mark.parametrize("body, expected", [("", [5, 10, 30, 60]),
                                             ("[restarts]\nmax_failures = 7\n", [5, 10, 30, 60, 300, 300])])
def test_failures_climb_the_backoff_ladder_then_fault(tmp_path, monkeypatch, run, body, expected) -> None:
    """With the default ladder (5 s, 10 s, 30 s, 1 min, 5 min): each failure waits the next rung, the last is reused,
    and max_failures in a row fault it."""
    clock, settings, backend, fleet, manager, client, state, seen = build_world(tmp_path, monkeypatch, body)
    backend.fail_start = True

    async def scenario() -> list[int]:
        fleet.boot()
        delays: list[int] = []
        for _ in range(10):
            await cycle(manager, clock)
            if manager.phase == "faulted":
                break
            assert manager.phase == "backoff"
            delays.append(round(manager.backoff_until - clock.mono))
            clock.now += delays[-1]
        return delays

    assert run(scenario()) == expected
    assert manager.phase == "faulted"


def test_a_playlists_labels_are_written_as_the_program_about_to_run_calls_them(world, run, caplog) -> None:
    """0.9.19 renamed some game option labels; 0.9.18 refuses the new names as 0.9.19 refuses the old. The labels
    were rewritten for 0.9.19 whatever the server ran."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    folder = manager.server.folder / "playlists"
    folder.mkdir(exist_ok=True)
    playlist = folder / "old.playlist.json"
    playlist.write_text('{"games": [{"options": {"teams.red_character_set": "Vanilla characters"}}]}',
                        encoding="utf-8")
    state.version = "0.9.18"  # what its console said last time (there's no program file here to ask)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        await asyncio.sleep(0.05)
        assert backend.starts == [SERVER]
        assert '"Vanilla characters"' in playlist.read_text(encoding="utf-8")  # 0.9.18's own name: kept
        backend.up.clear()
        state.version = "0.9.19"
        await cycle(manager, clock, 1)
        await cycle(manager, clock, 5)
        await asyncio.sleep(0.05)
        assert len(backend.starts) == 2 and '"Halo 3"' in playlist.read_text(encoding="utf-8")

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "old.playlist.json: 'Vanilla characters' is now 'Halo 3', as Project Reclaimer 0.9.19 calls them" \
        in caplog.text
