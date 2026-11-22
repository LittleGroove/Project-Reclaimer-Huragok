"""A server's game restarted over its console (RCON `restart`, `restart now`), and a server stopped the program's own
way (RCON `quit`): the two console paths a Workshop update uses (0.7.0, plan-0.7.0.md 4.4). The first is also the one
restart a server Huragok didn't start gets (answer 30)."""

from __future__ import annotations

import pytest

from testkit.fleet import SERVER, FakeClient, build_world, cycle, world  # noqa: F401 - world is a fixture

from huragok.servers import server as manager_module
from huragok.servers.fleet import Lifecycle


class QuittingClient(FakeClient):
    """`quit` ends the program, as the real one does in under a second (S1b)."""

    def __init__(self, state, backend) -> None:  # type: ignore[no-untyped-def]
        super().__init__(state)
        self.backend = backend

    async def command(self, command, *args, by=None, timeout=None):  # type: ignore[no-untyped-def]
        if command == "quit":
            self.backend.up.discard(SERVER)
        return await super().command(command, *args, by=by, timeout=timeout)


async def up(manager, clock, state) -> None:  # type: ignore[no-untyped-def]
    manager.fleet.boot()
    state.connected = True
    for _ in range(20):
        await cycle(manager, clock, 1)
        if manager.phase == "running":
            return
    raise AssertionError(manager.phase)


def test_restarting_its_game_over_the_console_keeps_the_server_up(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        await up(manager, clock, state)
        starts = len(backend.starts)
        result = await lifecycle.restart_worker(SERVER, reason="putting in the new version of HugeGrass")
        assert result.ok and result.message == "restarting its game"
        assert (await lifecycle.restart_worker(SERVER, now=True)).ok
        await cycle(manager, clock, 1)
        assert manager.phase == "running" and len(backend.starts) == starts and not backend.stops  # the process stays

    run(scenario())
    assert [command for command, _ in client.sent] == ["restart", "restart now"]
    assert "server.worker_restarted" in seen
    assert manager.record.recent(kinds=("restart",))[-1][2] == "a restart of its game (its game only, over the console)"


def test_a_server_huragok_didnt_start_is_restarted_over_its_console_and_nothing_else(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        manager.phase = "external"
        state.connected = True
        assert (await lifecycle.restart_worker(SERVER, reason="a mod update")).ok
        refused = lifecycle.quit(SERVER, reason="a mod update")
        assert not refused.ok and "wasn't started from here" in refused.message
        assert not manager.restart(reason="x").ok and not manager.stop().ok  # never stopped, started or killed
        assert manager.phase == "external"

    run(scenario())
    assert [command for command, _ in client.sent] == ["restart"] and not backend.stops and not backend.starts


def test_no_console_or_not_running_no_restart(world, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        assert (await lifecycle.restart_worker(SERVER)).message == "it isn't running (idle)"
        await up(manager, clock, state)
        state.connected = False
        assert (await lifecycle.restart_worker(SERVER)).message == "its console isn't connected"
        assert (await lifecycle.restart_worker("Ghost")).message == "no server called Ghost"

    run(scenario())
    assert client.sent == []


def test_quit_stops_it_the_programs_own_way_and_it_stays_stopped_until_started(tmp_path, monkeypatch, run) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = build_world(tmp_path, monkeypatch)
    client = QuittingClient(state, backend)
    manager.client = client
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        await up(manager, clock, state)
        result = lifecycle.quit(SERVER, reason="stopped to put in the new version of HugeGrass")
        assert result.ok and manager.phase == "stopping"
        for _ in range(20):
            await cycle(manager, clock, 1)
            if manager.phase == "idle":
                break
        assert manager.phase == "idle" and manager.record.wanted == "stopped" and lifecycle.is_stopped(SERVER)
        await cycle(manager, clock, 30)
        assert manager.phase == "idle"  # not taken for a crash, not started again by itself
        assert lifecycle.start(SERVER).ok
        await up(manager, clock, state)

    run(scenario())
    assert [command for command, _ in client.sent] == ["quit"]
    assert "server.crashed" not in seen and backend.stops == [SERVER]  # the backend tidied up after it


def test_a_process_that_outlives_its_quit_gets_the_usual_stop(world, run, monkeypatch: pytest.MonkeyPatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    monkeypatch.setattr(manager_module, "QUIT_WAIT", 0.4)
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        await up(manager, clock, state)
        assert lifecycle.quit(SERVER).ok
        for _ in range(100):
            await cycle(manager, clock, 1)
            if manager.phase == "idle":
                break

    run(scenario())
    assert [command for command, _ in client.sent] == ["quit"] and backend.stops == [SERVER]
    assert manager.phase == "idle"
