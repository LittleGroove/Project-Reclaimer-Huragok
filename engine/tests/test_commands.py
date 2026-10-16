from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, staff, write_server_folder, write_settings

from reclaimer_supervisor.chat import CommandRouter, split_words, wrap
from reclaimer_supervisor.config import load
from reclaimer_supervisor.events import Event, EventBus
from reclaimer_supervisor.part import CommandSpec
from reclaimer_supervisor.permissions import Roles
from reclaimer_supervisor.rcon import Reply
from reclaimer_supervisor.state import Player, ServerState

SERVER = "LG Test Server"


class FakeClient:
    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args))
        return Reply(True, "ok", None, {})


def _setup(tmp_path: Path) -> tuple[CommandRouter, EventBus, ServerState, FakeClient]:
    write_server_folder(tmp_path / "server", 49176)
    settings = load(write_settings(tmp_path, tmp_path / "server", commands=staff(cooldown=3)))
    bus, client = EventBus(), FakeClient()
    state = ServerState(SERVER)
    for number, (name, pid) in enumerate([("Owner", OWNER), ("Mod", MOD), ("Alice", ALICE), ("Bravo", BRAVO),
                                          ("Alice2", "f" * 64)], 1):
        state.players[number] = Player(number, name, pid)
    router = CommandRouter(settings, bus, {SERVER: state}, {SERVER: client}, Roles(settings))  # type: ignore[dict-item]
    return router, bus, state, client


async def _chat(bus: EventBus, player_id: str, text: str) -> None:
    bus.publish(Event("rcon.chat", SERVER, {"channel": "all", "name": "x", "id": player_id, "text": text}))
    await asyncio.sleep(0.05)


def _tells(client: FakeClient) -> list[str]:
    return [args[1] for command, args in client.sent if command == "tell"]


def test_split_and_wrap() -> None:
    assert split_words('Bravo 30m "being rude"') == ["Bravo", "30m", "being rude"]
    assert split_words('it\'s fine') == ["it's", "fine"]  # a stray quote doesn't break it
    lines = wrap("word " * 60, limit=50)
    assert all(len(l) <= 50 for l in lines) and len(lines) > 1


def test_roles(tmp_path: Path) -> None:
    router, _, state, _ = _setup(tmp_path)
    roles = router.roles
    assert roles.role_of(state, state.players[1]) == "owner"
    assert roles.role_of(state, state.players[2]) == "moderator"
    assert roles.role_of(state, Player(9, "Admin", ADMIN)) == "admin"  # dedicated.toml, until the server answers
    assert roles.role_of(state, Player(9, "Rcon admin", "9" * 64, admin=True)) == "admin"  # flagged on join
    assert roles.role_of(state, state.players[3]) == "player"
    state.admins = {"9" * 64}  # the server's live list wins: ADMIN was taken out of it
    assert roles.role_of(state, Player(9, "Admin", ADMIN)) == "player"
    assert roles.role_of(state, Player(9, "Offline admin", "9" * 64)) == "admin"
    assert Roles.allows("admin", "moderator") and not Roles.allows("moderator", "admin")


def test_admin_list_from_rcon() -> None:
    from reclaimer_supervisor.state import StateTracker
    ids = StateTracker.admin_ids({"configured": ["A" * 64, {"id": "b" * 64}], "added": [{"id": "c" * 64}, "junk"]})
    assert ids == {"a" * 64, "b" * 64, "c" * 64}


def test_finding_players(tmp_path: Path) -> None:
    _, _, state, _ = _setup(tmp_path)
    assert [p.name for p in state.find("#4")] == ["Bravo"]
    assert [p.name for p in state.find(BRAVO)] == ["Bravo"]
    assert [p.name for p in state.find("bravo")] == ["Bravo"]
    assert [p.name for p in state.find("alice")] == ["Alice"]  # exact beats prefix
    assert sorted(p.name for p in state.find("ali")) == ["Alice", "Alice2"]
    assert state.find("nobody") == []


def test_registering(tmp_path: Path) -> None:
    router, *_ = _setup(tmp_path)

    async def handler(call: Any) -> None:
        pass

    with pytest.raises(ValueError, match="vote"):
        router.register(CommandSpec("yes"), None, handler)
    with pytest.raises(ValueError, match="already"):
        router.register(CommandSpec("help"), None, handler)
    with pytest.raises(ValueError, match="role"):
        router.register(CommandSpec("x", role="god"), None, handler)


def test_routing_roles_cooldown_and_help(tmp_path: Path, run) -> None:
    router, bus, _, client = _setup(tmp_path)
    router.settings.permissions.roles["hello"] = "moderator"  # as if listed under [moderator] in commands.toml
    calls: list[Any] = []

    async def hello(call: Any) -> None:
        calls.append(call)
        await call.reply("hi")

    router.register(CommandSpec("hello", aliases=("hi",)), None, hello)

    async def scenario() -> None:
        await _chat(bus, ALICE, "!hello")  # role raised to moderator in commands.toml
        assert _tells(client)[-1] == "!hello is for moderators and up."
        await _chat(bus, MOD, "!HI   there  you")
        assert calls[-1].name == "hello" and calls[-1].args == ["there", "you"] and calls[-1].role == "moderator"
        await _chat(bus, MOD, "!hello")  # within the cooldown: ignored
        assert len(calls) == 1
        await _chat(bus, ALICE, "!nope")
        assert _tells(client)[-1] == "Unknown command !nope. Type !help for commands."
        await _chat(bus, ALICE, "!!! wow")  # not a command at all
        assert _tells(client)[-1].startswith("Unknown command !nope")
        await _chat(bus, ALICE, "!help")
        assert "!help" in _tells(client)[-1] and "!hello" not in _tells(client)[-1]
        await _chat(bus, OWNER, "!help")
        assert "!hello" in _tells(client)[-1]
        bus.publish(Event("rcon.chat", SERVER, {"channel": "server", "text": "!hello"}))  # the server's own lines
        await asyncio.sleep(0.05)
        assert len(calls) == 1

        router.settings.permissions.off.add("hello")  # moved to [off]: gone for everyone, owners included
        await _chat(bus, OWNER, "!hello")
        assert _tells(client)[-1] == "Unknown command !hello. Type !help for commands."
        await _chat(bus, OWNER, "!help")
        assert "!hello" not in _tells(client)[-1]
        router.settings.permissions.off.clear()
        router.settings.permissions.roles["hello"] = "player"  # moved to [everyone]
        router.settings.permissions.prefix = "?"
        await _chat(bus, ALICE, "?hello")
        assert len(calls) == 2 and calls[-1].prefix == "?"

    run(scenario())
