"""`start.cmd stop`, the console's stop: Huragok waits for every server to stop, then goes. Nothing may start, restart
or keep running meanwhile, or it would wait for good, or leave a server running once it's gone (audit 2026-11, 1.1
and 1.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conftest import ALICE, free_ports, write_root
from testkit.fleet import RESTARTS, Clock, FakeBackend, FakeClient

from huragok.core.events import EventBus
from huragok.core.settings import load
from huragok.servers import server as manager_module
from huragok.servers.fleet import Fleet
from huragok.servers.state import Player, ServerState


def two_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Clock, FakeBackend, Fleet, dict, dict, list]:
    clock = Clock(1_800_000_000.0)
    monkeypatch.setattr(manager_module, "time", clock)
    names = ("A", "B")
    settings = load(write_root(tmp_path / "reclaimer", list(zip(names, free_ports(2))), RESTARTS))
    bus = EventBus()
    states = {n: ServerState(n) for n in names}
    backend = FakeBackend(settings, bus, states)
    done: list[bool] = []
    fleet = Fleet(settings, backend, bus, on_all_stopped=lambda: done.append(True))
    managers = {server.name: fleet.add(server, FakeClient(states[server.name]), states[server.name])
                for server in settings.servers}
    return clock, backend, fleet, managers, states, done


async def ticks(fleet: Fleet, clock: Clock, advance: float = 0, times: int = 3) -> None:
    clock.now += advance
    for _ in range(times):
        fleet.tick(clock.now)
        await asyncio.sleep(0.01)


async def both_up(fleet: Fleet, clock: Clock, managers: dict, states: dict) -> None:
    fleet.boot()
    await ticks(fleet, clock)
    for state in states.values():
        state.connected = True
    await ticks(fleet, clock)
    assert all(m.phase == "running" for m in managers.values())


def test_nothing_starts_again_while_everything_is_stopping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    async def scenario() -> None:
        clock, backend, fleet, managers, states, done = two_servers(tmp_path, monkeypatch)
        await both_up(fleet, clock, managers, states)
        a, b = managers["A"], managers["B"]
        states["A"].players[1] = Player(1, "Alice", ALICE)  # A warns its players first: a countdown
        fleet.stop_all()
        await ticks(fleet, clock, 1)
        assert a.phase == "countdown"
        for asked in (a.restart(reason="!restartserver", by="Alice"), a.start(), a.cancel(), b.start()):
            assert not asked.ok and "every server is being stopped" in asked.message
        for _ in range(60):
            await ticks(fleet, clock, 1)
            states["A"].connected = backend.alive(a.server)
            if done:
                break
        assert done == [True] and a.phase == "idle" and not backend.alive(a.server)

    run(scenario())


def test_a_faulted_servers_try_waits_and_nothing_starts_in_huragoks_last_moments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                                 run) -> None:
    async def scenario() -> None:
        clock, backend, fleet, managers, states, done = two_servers(tmp_path, monkeypatch)
        await both_up(fleet, clock, managers, states)
        a, b = managers["A"], managers["B"]
        states["A"].players[1] = Player(1, "Alice", ALICE)
        for _ in range(2):  # B crashes until it's FAULTED
            backend.up.discard("B")
            states["B"].connected = False
            await ticks(fleet, clock)
            if b.phase == "backoff":
                await ticks(fleet, clock, 6)
        assert b.phase == "faulted"
        b.record.retry_at = clock.now + 4  # its cool-down ends during A's countdown
        fleet.stop_all()
        for _ in range(60):
            await ticks(fleet, clock, 1)
            states["A"].connected = backend.alive(a.server)
            if done:
                break
        await ticks(fleet, clock, 10)  # Huragok is going: the ticks it still makes start nothing
        assert done == [True] and fleet.stopping_all
        assert not backend.alive(a.server) and not backend.alive(b.server)
        assert b.phase == "faulted"

    run(scenario())
