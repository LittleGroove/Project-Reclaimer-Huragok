"""The per-server state machine, with a fake backend and a clock we move by hand."""

from __future__ import annotations

import asyncio
import time as real_time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import ALICE, free_port, write_root

from reclaimer_supervisor import manager as manager_module
from reclaimer_supervisor.config import load
from reclaimer_supervisor.events import EventBus
from reclaimer_supervisor.lifecycle import Backend, LifecycleError
from reclaimer_supervisor.manager import Fleet, Lifecycle, ServerRecord, daily_slot, next_daily, warning_text
from reclaimer_supervisor.state import Player, ServerState

SERVER = "LG Test Server"


class Clock:
    """time.time() under our control; everything else is the real module's."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


class FakeBackend(Backend):
    name = "fake"
    can_start = can_stop = can_restart = True
    sees_processes = True

    def __init__(self, settings: Any, bus: EventBus, states: dict[str, ServerState]) -> None:
        super().__init__(settings, bus, states)
        self.up: set[str] = set()
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.fail_start = False
        self.started_times: dict[str, float] = {}

    async def start(self, server: Any) -> None:
        if self.fail_start:
            raise LifecycleError("the program is missing")
        self.up.add(server.name)
        self.starts.append(server.name)
        self.started_times[server.name] = real_time.time()

    async def stop(self, server: Any, timeout: float | None = None) -> None:
        self.stops.append(server.name)
        self.up.discard(server.name)

    def alive(self, server: Any) -> bool:
        return server.name in self.up

    def started_at(self, server: Any) -> float | None:
        return self.started_times.get(server.name)

    def forget(self, server: Any) -> None:
        self.up.discard(server.name)


class FakeClient:
    def __init__(self, state: ServerState) -> None:
        self.state = state  # connected follows the server's state, as the real client's does
        self.stopped_reason: str | None = None
        self.sent: list[tuple[str, tuple[str, ...]]] = []
        self.woken = 0

    @property
    def connected(self) -> bool:
        return self.state.connected

    def wake(self) -> None:
        self.woken += 1

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Any:
        self.sent.append((command, args))

        class Reply:
            ok = True
            text = f"{command} ok"

        return Reply()

    def says(self) -> list[str]:
        return [args[0] for command, args in self.sent if command == "say"]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = Clock(1_800_000_000.0)
    monkeypatch.setattr(manager_module, "time", clock)
    root = tmp_path / "reclaimer"
    settings = load(write_root(root, [(SERVER, free_port())], "[restarts]\nwarn_seconds = [10, 5, 2]\n"
                                                        "backoff_seconds = [5, 60]\nmax_failures = 2\n"
                                                        "good_run_seconds = 600\nrestarts_per_hour = 3\n"
                                                        "ready_seconds = 30\nempty_seconds = 4\n"
                                                        "after_game_seconds = 3\n"))
    bus = EventBus()
    state = ServerState(SERVER)
    backend = FakeBackend(settings, bus, {SERVER: state})
    fleet = Fleet(settings, backend, bus)
    client = FakeClient(state)
    manager = fleet.add(settings.servers[0], client, state)  # type: ignore[arg-type]
    seen: list[str] = []
    bus.subscribe("server.*", lambda e: seen.append(e.name))
    return clock, settings, backend, fleet, manager, client, state, seen


async def cycle(manager: Any, clock: Clock, advance: float = 0) -> None:
    """A few ticks at one moment, with time moved on first, letting the backend's tasks finish between them."""
    clock.now += advance
    for _ in range(3):
        manager.tick(clock.now)
        await asyncio.sleep(0.01)


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
        assert seen[:2] == ["server.starting", "server.ready"] or "server.ready" in seen
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
        assert client.says() == ["Server restarting in 10 seconds."]
        await cycle(manager, clock, 5)
        assert client.says()[-1] == "Server restarting in 5 seconds."
        await cycle(manager, clock, 3.5)  # several rungs fall due at once: only the smallest is said
        assert client.says()[-1] == "Server restarting in 2 seconds." and len(client.says()) == 3
        assert "RESTART IN" in manager.row(1, clock.now)["status"]
        await cycle(manager, clock, 2)
        assert client.says()[-1] == "Server is restarting now." and backend.stops == [SERVER]
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
        assert client.says() == ["Server shutting down in 10 seconds."] and "STOP IN" in manager.row(1)["status"]
        assert manager.cancel().ok and manager.phase == "running"
        await asyncio.sleep(0.01)  # the chat line is sent without waiting for it
        assert client.says()[-1] == "The server shutdown has been cancelled." and manager.record.wanted == "running"
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
    monkeypatch.setattr(manager_module, "port_in_use", lambda server, timeout=0.5: True)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "external" and backend.starts == []
        assert "started some other way" in manager.start().message
        assert "wasn't started from here" in manager.restart().message
        row = manager.row(1, clock.now)
        assert "not started from here" in row["status"] and not row["can"]["stop"]
        monkeypatch.setattr(manager_module, "port_in_use", lambda server, timeout=0.5: False)
        await cycle(manager, clock)
        assert manager.phase == "booting" and backend.starts == [SERVER]  # wanted: started once the port is free

    run(scenario())


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
        assert client.says() == ["This server restarts after this game: performance"]
        from reclaimer_supervisor.events import Event

        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "playing", "map": "Valhalla", "mode": "Slayer"}))
        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "finished", "map": "Valhalla", "mode": "Slayer"}))
        assert manager.phase == "countdown" and manager.planned is None
        await cycle(manager, clock)
        assert client.says()[-1] == "Server restarting in 3 seconds."
        assert lifecycle.cancel(SERVER) and manager.phase == "running"

    run(scenario())


def test_records_and_helpers(tmp_path: Path) -> None:
    record = ServerRecord(wanted="running", fail_count=2, restart_times=[1.0, 2.0], daily_last_fired="2026-10-16")
    record.save(tmp_path / "r.json")
    loaded = ServerRecord.load(tmp_path / "r.json")
    assert loaded == record
    assert ServerRecord.load(tmp_path / "missing.json") == ServerRecord()
    assert warning_text(300, "daily restart", True) == "Server restarting in 5 minutes: daily restart."
    assert warning_text(30, "daily restart", True) == "Server restarting in 30 seconds."
    assert warning_text(60, "", False) == "Server shutting down in 1 minute."
    now = datetime(2026, 10, 16, 12, 0)
    assert daily_slot("", now) is None
    assert daily_slot("04:00", now) == datetime(2026, 10, 16, 4, 0)
    assert next_daily("04:00", now) == datetime(2026, 10, 17, 4, 0)
    assert next_daily("23:30", now) == datetime(2026, 10, 16, 23, 30)
