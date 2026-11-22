"""A server Huragok didn't start (phase "external": by hand, or by another tool) gets an update's restart only through
its own console's `restart`, which restarts its game and leaves its process alone (S1b). It's never stopped, started or
killed from here (0.7.0, answer 30)."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from conftest import ALICE
from testkit.fleet import SERVER, cycle, world  # noqa: F401 - world: the fixture

from huragok.core.events import Event
from huragok.servers.fleet import Lifecycle
from huragok.servers.state import Player

REASON = "Project Reclaimer 0.10.9 is out; it updates as it starts"


def started_elsewhere(world: Any, monkeypatch: Any) -> None:
    """The world's server, found running on its port and writing its own folder's status.json: not Huragok's."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    settings.servers[0].autostart = False
    monkeypatch.setattr(fleet.ports, "in_use", lambda server: True)
    status = manager.server.status_path
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text("{}", encoding="utf-8")
    os.utime(status, (clock.now, clock.now))


def test_an_update_restarts_a_server_started_elsewhere_through_its_console_after_its_game(world, run,
                                                                                       monkeypatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    started_elsewhere(world, monkeypatch)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "external" and manager.record.wanted == "stopped"
        state.connected, state.phase = True, "playing"
        state.players[1] = Player(1, "Alice", ALICE)
        # A person's restart or stop is still refused.
        assert "wasn't started from here" in manager.restart().message
        assert "wasn't started from here" in manager.stop().message
        plan = await Lifecycle(fleet).restart(SERVER, when="after_game", reason=REASON, tell="the players' line",
                                              console=True)
        await asyncio.sleep(0.01)  # said in the background
        assert plan.console and manager.planned is plan and client.says() == ["the players' line"]
        await cycle(manager, clock, 60)
        assert manager.phase == "external"  # waits for its game to end, however long
        assert "wasn't started from here" in manager.stop().message
        fleet._on_phase(Event("rcon.phase", SERVER, {"phase": "finished"}))  # its game ends: the usual countdown
        assert manager.phase == "countdown"
        assert "wasn't started from here" in manager.stop().message  # still never stopped
        await cycle(manager, clock)
        assert client.says()[-1] == "[o_O] PLEASE REMAIN CALM! Server restarting in 3 seconds."
        await cycle(manager, clock, 3)
        assert client.says()[-1] == "[-.-] PARDON OUR DUST! Server is restarting now: join again in a minute or two."
        assert ("restart now", ()) in client.sent and "server.restarting" in seen
        assert manager.phase == "external" and backend.stops == [] and backend.starts == []
        assert manager.record.wanted == "stopped"  # never Huragok's to start, should it go away
        await cycle(manager, clock, 30)
        assert manager.phase == "external" and backend.starts == []

    run(scenario())


def test_nobody_on_it_restarts_through_its_console_at_once_without_a_word(world, run, monkeypatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    started_elsewhere(world, monkeypatch)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True  # its players read: nobody
        await Lifecycle(fleet).restart(SERVER, when="after_game", reason=REASON, tell="unsaid", console=True)
        await cycle(manager, clock)
        assert client.sent == [("restart now", ())] and client.says() == []
        assert manager.phase == "external" and backend.stops == [] and backend.starts == []

    run(scenario())


def test_a_console_restart_is_called_off_when_huragok_stops_and_never_without_a_console(world, run,
                                                                                      monkeypatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    started_elsewhere(world, monkeypatch)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        state.players[1] = Player(1, "Alice", ALICE)
        assert manager.restart(when="now", reason=REASON, manual=False, console=True).ok
        assert manager.phase == "countdown"
        assert manager.cancel().ok and manager.phase == "external"  # cancelled: back to watching it
        assert manager.restart(when="now", reason=REASON, manual=False, console=True).ok
        fleet.stop_all()  # Huragok stops: it's left running, its restart called off
        await cycle(manager, clock, 30)
        assert ("restart now", ()) not in client.sent and manager.phase == "external"
        assert backend.stops == [] and backend.starts == []
        fleet.stopping_all = False
        client.no_console = True  # no [rcon] password: nothing can restart it from here
        result = manager.restart(when="after_game", reason=REASON, console=True)
        assert not result.ok and "has no console" in result.message

    run(scenario())


def test_a_server_huragok_started_restarts_as_ever_whatever_console_says(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running"
        await Lifecycle(fleet).restart(SERVER, when="after_game", reason=REASON, console=True)
        await cycle(manager, clock, 1)
        assert backend.stops == [SERVER] and ("restart now", ()) not in client.sent  # stopped and started

    run(scenario())
