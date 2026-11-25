"""A server with no console, watched through its status.json (0.7.0, answer 7): what changes in the file becomes the
console's own events, `status` and `players` are answered from it, and everything else is refused as no console."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from huragok.core.events import Event, EventBus
from huragok.core.serverfolders import ServerConfig
from huragok.rcon import RconClient, RconUnavailable
from huragok.servers.statusclient import StatusFileClient, console_phase


def _server(tmp_path: Path, password: str = "") -> ServerConfig:
    data = tmp_path / "data" / "quiet"
    data.mkdir(parents=True)
    return ServerConfig(name="Quiet", slug="quiet", folder=tmp_path, config_path=tmp_path / "dedicated.toml",
                        data_dir=data, game_port=46961, rcon_host="127.0.0.1", rcon_port=46961, rcon_password=password)


def _write(server: ServerConfig, *, state: str = "lobby", players: tuple[tuple[int, str, str], ...] = (),
           updated: float | None = None, pid: int | None = None) -> None:
    status = {"name": server.name, "map": "Guardian", "mode": "Slayer", "state": state,
              "players": [{"number": n, "name": name, "id": letter * 64, "admin": False} for n, name, letter in players],
              "max_players": 16, "pid": os.getpid() if pid is None else pid,
              "updated": int(time.time() if updated is None else updated), "phase": "lobby",
              "health": {"version": "0.10.9"}}
    server.status_path.write_text(json.dumps(status), encoding="utf-8")


def _events(bus: EventBus) -> list[Event]:
    seen: list[Event] = []
    bus.subscribe("rcon.*", seen.append)
    return seen


def test_the_phase_reads_as_the_console_names_it() -> None:
    assert console_phase({"state": "lobby", "phase": "lobby"}) == "no_game"
    assert console_phase({"state": "playing", "phase": "in_game"}) == "playing"
    assert console_phase({"state": "finished"}) == "finished"
    assert console_phase({"phase": "in_game"}) == "playing" and console_phase({"phase": "post_game"}) == "finished"
    assert console_phase({"phase": "vote"}) == "no_game"
    # 0.10.9's own words (the beta.2 tests, 25 November 2026): its "post-game" is the console's finished
    assert console_phase({"state": "countdown", "phase": "countdown"}) == "no_game"
    assert console_phase({"state": "loading"}) == "no_game"
    assert console_phase({"state": "post-game", "phase": "post_game"}) == "finished"
    assert console_phase({"state": "voting"}) == "voting"  # one it doesn't know: as it is


def test_a_fresh_status_file_is_a_connection_and_its_changes_are_the_consoles_events(tmp_path: Path) -> None:
    server = _server(tmp_path)
    bus = EventBus()
    seen = _events(bus)
    client = StatusFileClient(server, bus)
    client.poll()
    assert not client.connected and seen == []  # no status.json yet: not up
    _write(server, players=((1, "Alice", "a"),))
    client.poll()
    assert client.connected and client.version == "0.10.9"
    assert [e.name for e in seen] == ["rcon.connected"]  # whoever was on already is read, not joined
    seen.clear()
    _write(server, state="playing", players=((1, "Alice", "a"), (2, "Bravo", "b")))
    client.poll()
    assert [(e.name, e.get("phase") or e.get("number")) for e in seen] == [("rcon.phase", "playing"), ("rcon.join", 2)]
    assert seen[1].get("id") == "b" * 64 and seen[1].get("name") == "Bravo"
    seen.clear()
    _write(server, state="finished", players=((2, "Bravo", "b"), (3, "Charlie", "c")))
    client.poll()
    assert [(e.name, e.get("phase") or e.get("number")) for e in seen] == [
        ("rcon.phase", "finished"), ("rcon.leave", 1), ("rcon.join", 3)]
    seen.clear()
    _write(server, state="lobby", players=((2, "Delta", "d"),))  # someone else has number 2 now
    client.poll()
    assert [(e.name, e.get("number")) for e in seen] == [("rcon.phase", None), ("rcon.leave", 3), ("rcon.leave", 2),
                                                          ("rcon.join", 2)]
    assert seen[0].get("phase") == "no_game"


def test_a_stale_file_or_a_dead_process_is_a_lost_connection(tmp_path: Path) -> None:
    server = _server(tmp_path)
    bus = EventBus()
    seen = _events(bus)
    client = StatusFileClient(server, bus)
    _write(server)
    client.poll()
    assert client.connected
    _write(server, updated=time.time() - 60)  # 0.10.9 rewrites it about once a minute: still up
    client.poll()
    assert client.connected
    _write(server, updated=time.time() - 300)  # not rewritten for five minutes: stopped, or frozen
    client.poll()
    assert not client.connected and seen[-1].name == "rcon.disconnected"
    _write(server, pid=2 ** 30)  # fresh, but its process is gone (what a killed server leaves behind)
    client.poll()
    assert not client.connected


def test_status_and_players_are_answered_from_the_file_and_anything_else_is_refused(tmp_path: Path, run) -> None:
    server = _server(tmp_path)
    client = StatusFileClient(server, EventBus())

    async def scenario() -> None:
        with pytest.raises(RconUnavailable):
            await client.command("status")  # not up yet
        _write(server, state="playing", players=((1, "Alice", "a"),))
        client.poll()
        status = await client.command("status")
        assert status.ok and status.data["map"] == "Guardian"
        assert status.data["phase"] == "playing"  # the console's word, as the state's refresh reads it
        players = await client.command("players")
        assert [p["name"] for p in players.data["players"]] == ["Alice"] and players.data["max_players"] == 16
        with pytest.raises(RconUnavailable, match="no console"):
            await client.command("say", "hello")

    run(scenario())


def test_its_loop_stops_and_says_the_connection_is_gone(tmp_path: Path, run) -> None:
    server = _server(tmp_path)
    bus = EventBus()
    seen = _events(bus)
    client = StatusFileClient(server, bus, poll_seconds=0.01)
    _write(server)

    async def scenario() -> None:
        task = asyncio.ensure_future(client.run())
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.01)
        client.stop()
        await asyncio.wait_for(task, 2)

    run(scenario())
    assert [e.name for e in seen] == ["rcon.connected", "rcon.disconnected"] and not client.connected


def test_a_server_without_a_console_gets_its_status_file_in_the_consoles_place(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from huragok.superintendent import _client_for

    settings = SimpleNamespace(use_host_key=True, command_timeout=10.0)
    assert isinstance(_client_for(_server(tmp_path / "a"), EventBus(), settings), StatusFileClient)
    assert isinstance(_client_for(_server(tmp_path / "b", "a long password"), EventBus(), settings), RconClient)
