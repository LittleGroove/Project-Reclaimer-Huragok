"""A server something else already runs is never started a second time (0.7.0, plan 4.6, simulation S4: a second
process for it crash-loops and wipes its status.json and log). Its own supervisor, named in its supervision.json, is
looked at before every start, and its game port is checked against everything on this PC, UDP too."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from testkit.fleet import SERVER, cycle, world  # noqa: F401 - world is a fixture

from huragok.servers.server import supervisor_running


class Processes:
    """psutil's process table, as the test sets it: number -> (name, command line). Anything else isn't running."""

    def __init__(self) -> None:
        self.running: dict[int, tuple[str, list[str]]] = {}

    def process(self, pid: int):
        import psutil

        if pid not in self.running:
            raise psutil.NoSuchProcess(pid)
        name, command = self.running[pid]
        return SimpleNamespace(name=lambda: name, cmdline=lambda: list(command))


@pytest.fixture
def processes(monkeypatch) -> Processes:
    import psutil

    table = Processes()
    monkeypatch.setattr(psutil, "Process", table.process)
    return table


PROGRAM = 4242
WINE = 4343
OTHER = 4444


def _supervision(data_dir: Path, pid: int | None) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "supervision.json").write_text(json.dumps({"supervisor": pid, "worker": None, "worker_started": 0,
                                                          "crashes_in_row": 0}), encoding="utf-8")


def test_the_supervisor_named_in_supervision_json_counts_only_while_it_runs_and_is_the_program(
        world, processes) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    server = manager.server
    processes.running[PROGRAM] = ("project-reclaimer.exe", [r"S:\servers\a\project-reclaimer.exe", "dedicated", "run"])
    processes.running[WINE] = ("wine64-preloader", [r"Z:\srv\project-reclaimer-dedicated-v0.10.9.exe", "dedicated"])
    processes.running[OTHER] = ("python.exe", [r"S:\Projects\ProjectReclaimer\.venv\python.exe", "-m", "pytest"])
    assert supervisor_running(server) is None  # no supervision.json: nothing runs it
    _supervision(server.data_dir, PROGRAM)
    assert supervisor_running(server) == PROGRAM
    assert supervisor_running(server, ours=PROGRAM) is None  # the one Huragok runs for it
    _supervision(server.data_dir, WINE)
    assert supervisor_running(server) == WINE  # on Linux, the program's file is wine's first word
    _supervision(server.data_dir, OTHER)
    assert supervisor_running(server) is None  # the number is another program's now, whatever folder it runs from
    del processes.running[PROGRAM]
    _supervision(server.data_dir, PROGRAM)
    assert supervisor_running(server) is None  # gone
    (server.data_dir / "supervision.json").write_text("{not json", encoding="utf-8")
    assert supervisor_running(server) is None


def test_a_server_whose_own_supervisor_runs_is_watched_not_started_again(world, run, processes) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    processes.running[PROGRAM] = ("project-reclaimer.exe", ["project-reclaimer.exe", "dedicated", "run"])
    _supervision(manager.server.data_dir, PROGRAM)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "external" and backend.starts == []
        await cycle(manager, clock, 30)
        assert manager.phase == "external" and backend.starts == []  # its port is free (no console), it still runs
        del processes.running[PROGRAM]
        await cycle(manager, clock, 10)
        assert manager.phase != "external"  # gone: looked after from here again

    run(scenario())


def test_a_game_port_taken_on_this_pc_keeps_it_waiting(world, run, monkeypatch) -> None:
    clock, settings, backend, fleet, manager, client, state, seen = world
    monkeypatch.setattr(fleet.ports, "in_use", lambda server: False)
    monkeypatch.setattr(fleet.ports, "game_port_in_use", lambda server: True)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "idle" and backend.starts == []
        assert manager.waiting_kind == "port" and "game port" in manager.waiting_for
        monkeypatch.setattr(fleet.ports, "game_port_in_use", lambda server: False)
        await cycle(manager, clock, 2)
        assert backend.starts == [SERVER]

    run(scenario())


def test_a_server_renamed_while_it_ran_isnt_started_again_under_its_old_name(world, run) -> None:
    """Its process was started under the old name, which its dedicated.toml no longer has (0.7.0, answer 32): its next
    restart stops it and leaves it for Huragok to follow the rename and start it under the new one."""
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running" and backend.starts == [SERVER]
        manager.renamed_to = "New Name"
        assert manager.restart(reason="testing").ok
        for _ in range(10):
            await cycle(manager, clock, 1)
        assert backend.starts == [SERVER] and SERVER not in backend.up and manager.phase == "idle"
        assert manager.record.wanted == "running"  # wanted under its new name once it's followed

    run(scenario())


def test_a_game_that_keeps_crashing_is_warned_about(tmp_path: Path, run) -> None:
    """The program's supervisor restarts a crashed game by itself and counts it (supervision.json crashes_in_row): from
    three in a row Huragok says so, and stops once the game runs again."""
    import asyncio

    from conftest import staff, write_server_folder, write_settings

    from huragok.core.settings import load
    from huragok.huragok import Huragok

    async def scenario() -> None:
        root = tmp_path / "reclaimer"
        write_server_folder(root / "SERVERS" / "Shaky", 46395, name="Shaky")
        supervisor = Huragok(load(write_settings(root, None, commands=staff())))
        task = asyncio.ensure_future(supervisor.run())
        try:
            sup = supervisor.superintendent
            for _ in range(200):
                if "Shaky" in sup.fleet.managers:
                    break
                await asyncio.sleep(0.05)
            data = sup.settings.server("Shaky").data_dir
            _supervision(data, None)
            sup.crash_loops()
            assert sup.warnings.of("Shaky") == []
            (data / "supervision.json").write_text('{"supervisor": null, "crashes_in_row": 4}', encoding="utf-8")
            sup.crash_loops()
            [warning] = sup.warnings.of("Shaky")
            assert warning.rule.id == "crash-loop" and "crashed 4 times in a row" in warning.text
            _supervision(data, None)
            sup.crash_loops()
            assert sup.warnings.of("Shaky") == []
        finally:
            supervisor.stop()
            await asyncio.wait_for(task, 10)

    run(scenario())
