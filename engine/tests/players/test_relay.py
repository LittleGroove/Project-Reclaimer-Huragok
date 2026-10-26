"""The console relay's gate: which role a line needs (with the protocol's args too), what never goes through
([off], control characters, blank words), and what moderators below admin don't see (addresses, devices)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, free_ports, staff, wait_for, write_root

from huragok.core.commandsfile import Permissions
from huragok.core.events import Event, EventBus
from huragok.core.settings import load
from huragok.players.relay import Relay, Session, line_refusal, required_role, without_addresses
from huragok.rcon import Reply
from huragok.servers.state import ServerState

FINGERPRINT = "f0" * 32
PLAYER = "d" * 64
IDEOGRAPHIC, NBSP = chr(0x3000), chr(0xA0)  # spaces str.split() splits at and drops
USERS = ('[[relay.user]]\nname = "Alice"\npassword = "alicealice"\nrole = "moderator"\n'
         '[[relay.user]]\nname = "Bob"\npassword = "bobbobbob"\nrole = "admin"\n'
         '[[relay.user]]\nname = "Olga"\npassword = "olgaolgaolga"\nrole = "owner"\n')
PLAYERS_TEXT = f"1/16 players\n#1 Dee {PLAYER[:8]} 203.0.113.4 device {FINGERPRINT}"
PLAYERS_DATA = {"count": 1, "players": [{"number": 1, "name": "Dee", "id": PLAYER, "ip": "203.0.113.4",
                                         "device": FINGERPRINT, "admin": False}]}
BANS_TEXT = (f"Bravo ({PLAYER[:8]}): cheating, by Owner (RCON)\n  ip 198.51.100.7\n  Device ban: {FINGERPRINT}\n"
             "Range 198.51.100.0/24: proxies\nFrom 2001:db8::1: spam")
BANS_DATA = {"players": [{"id": PLAYER, "name": "Bravo", "reason": "cheating", "banned_by": "Owner (RCON)",
                          "group": 3}],
             "ips": [{"ip": "198.51.100.7", "name": "Bravo", "reason": "cheating", "group": 3},
                     {"ip": "198.51.100.0/24", "reason": "proxies"}],
             "devices": [{"id": FINGERPRINT, "name": "Bravo", "reason": "cheating", "group": 3}]}
VPN_DATA = {"block_vpn": True, "ranges": 1200, "allowed": [{"target": "203.0.113.9", "note": "a friend"},
                                                           {"target": PLAYER, "note": "Dee"}]}


class Stub:
    version = "0.9.20"
    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...], str | None]] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args, by))
        word = command.split(" ")[0]
        if word == "players":
            return Reply(True, PLAYERS_TEXT, PLAYERS_DATA, {})
        if word == "bans":
            return Reply(True, BANS_TEXT, BANS_DATA, {})
        if word == "vpn":
            return Reply(True, "VPN block on; allowed: 203.0.113.9 (a friend)", VPN_DATA, {})
        if word == "status":
            return Reply(True, "status", {"name": "Alpha", "address": "192.0.2.10:11774", "players": 1}, {})
        return Reply(True, f"did {command}", None, {})


def relay_for(tmp_path: Path, commands: str = "", port: int | None = None, offset: int = 7) -> tuple[Relay, Stub, Any]:
    body = f'backend = "attach"\n[relay]\nenabled = true\nport_offset = {offset}\n' + USERS
    settings = load(write_root(tmp_path / "reclaimer", [("Alpha", port or free_port())], body, staff(extra=commands)))
    client = Stub()
    bus = EventBus()
    relay = Relay(settings, {"Alpha": client}, {"Alpha": ServerState("Alpha")}, bus)  # type: ignore[dict-item]
    return relay, client, settings


def session(settings: Any, name: str) -> Session:
    user = next(u for u in settings.relay.users if u.name == name)
    return Session(user, "Alpha", "127.0.0.1", lambda: None)


def test_a_query_is_alone_only_when_nothing_follows_it() -> None:
    permissions = Permissions()
    assert required_role("password", permissions) == "moderator"  # reads whether a password is set
    assert required_role("password ", permissions) == "moderator"
    assert required_role("password x", permissions) == "owner"
    assert required_role(f"password {IDEOGRAPHIC}", permissions) == "owner"  # str.split() would lose that word
    assert required_role(f"password{IDEOGRAPHIC}x", permissions) == "owner"
    assert required_role(f"servername {NBSP}", permissions) == "admin"
    # The protocol's split form: anything in args means it isn't alone.
    assert required_role("password", permissions, []) == "moderator"
    assert required_role("password", permissions, [""]) == "owner"
    assert required_role("password", permissions, ["\x1f"]) == "owner"
    assert required_role("servername", permissions, [IDEOGRAPHIC]) == "admin"
    assert required_role("kick", permissions, ["#3", "camping"]) == "moderator"
    assert required_role("ban", permissions, ["#3"]) == "admin"


@pytest.mark.parametrize("command, args, why", [
    ("password", [""], "blank word"),
    ("password", [IDEOGRAPHIC], "blank word"),
    ("servername", ["  "], "blank word"),
    ("password", ["\x1f"], "control characters"),
    ("status\nban #3 cheating", None, "control characters"),
    ("status\rban #3", None, "control characters"),
    ("kick", ["#3", "camping\nban #4"], "control characters"),
    ("players\x00", None, "control characters"),
    ("kick #3", ["camping"], "name alone"),
    ("", ["#3"], "name alone"),
])
def test_lines_that_never_go_through(command: str, args: list[str] | None, why: str) -> None:
    assert why in line_refusal(command, args)


def test_ordinary_lines_go_through() -> None:
    assert line_refusal("kick #3 camping") == "" and line_refusal("kick", ["#3", "being rude"]) == ""
    assert line_refusal(f"say konnichiwa{IDEOGRAPHIC}minna") == ""  # an ideographic space inside a word is fine
    assert line_refusal("password") == "" and line_refusal("password", None) == ""


def test_a_moderator_cannot_reach_owner_commands_through_args(tmp_path: Path, run) -> None:
    relay, client, settings = relay_for(tmp_path)
    alice, server = session(settings, "Alice"), settings.servers[0]

    async def scenario() -> None:
        for command, args in [("password", [""]), ("password", ["\x1f"]), ("password", [IDEOGRAPHIC]),
                              ("servername", [IDEOGRAPHIC]), ("servername", [""]), ("password", ["x"]),
                              ("status\nban #3 cheating", None)]:
            ok, text, _ = await relay._run_command(alice, server, command, args)
            assert not ok, (command, args, text)
        assert client.sent == []
        ok, _, _ = await relay._run_command(alice, server, "password", None)  # whether one is set: read-only
        assert ok and client.sent == [("password", (), "Alice")]
        ok, text, _ = await relay._run_command(alice, server, "password", ["x"])
        assert not ok and "'password' needs the owner role; you're moderator" in text

    run(scenario())


def test_nobody_acts_on_their_own_role_or_above_through_the_relay(tmp_path: Path, run) -> None:
    """The console acts with the host's authority, so the relay holds the in-game rule: a moderator can't kick an
    owner or another moderator, an admin can't ban an owner by ID while they're away; owners can."""
    from conftest import MOD, OWNER

    from huragok.servers.state import Player

    relay, client, settings = relay_for(tmp_path)
    state = relay.states["Alpha"]
    state.players = {1: Player(1, "Big Boss", OWNER), 2: Player(2, "Mod Man", MOD), 3: Player(3, "Dee", PLAYER)}
    alice, bob, olga = (session(settings, name) for name in ("Alice", "Bob", "Olga"))
    server = settings.servers[0]

    async def scenario() -> None:
        for user, command, args in [(alice, "kick #1 camping", None), (alice, 'kick "big boss" camping', None),
                                    (alice, "mute", ["#2", "10m"]), (alice, "kick", ["Mod Man"]),
                                    (bob, f"ban {OWNER} 7d", None), (bob, "startvote kick #1", None),
                                    (bob, "mute 1", None)]:
            ok, text, _ = await relay._run_command(user, server, command, args)
            assert not ok and "you can't" in text and "nobody acts on their own role or above" in text, (command, text)
        assert client.sent == []
        ok, text, _ = await relay._run_command(alice, server, "kick #1 camping", None)
        assert text == "Big Boss is an owner; you can't kick them (nobody acts on their own role or above)."
        for user, command, args in [(alice, "kick #3 camping", None), (alice, "mute", ["Dee", "10m"]),
                                    (bob, "mute #2", None), (olga, "kick #1", None), (alice, "kick Nobody", None)]:
            ok, text, _ = await relay._run_command(user, server, command, args)
            assert ok, (command, text)
        assert len(client.sent) == 5

    run(scenario())


def test_a_blank_or_oversized_command_is_refused_and_the_session_goes_on(tmp_path: Path, run) -> None:
    relay, client, settings = relay_for(tmp_path)
    alice, server = session(settings, "Alice"), settings.servers[0]

    async def too_long(*_: Any, **__: Any) -> Any:
        raise ValueError("command too long for one RCON message (16 KiB)")

    async def scenario() -> None:
        ok, text, _ = await relay._run_command(alice, server, "   ", None)
        assert not ok and "Nothing to send" in text and client.sent == []
        client.command = too_long  # type: ignore[method-assign]
        ok, text, _ = await relay._run_command(alice, server, "kick", ["#3", "x" * 20000])
        assert not ok and "Not sent: command too long" in text  # an answer, not an error that ends the session

    run(scenario())


def test_off_applies_through_the_relay_too_owners_included(tmp_path: Path, run) -> None:
    relay, client, settings = relay_for(tmp_path, '[off]\ncommands = ["say", "kick"]\n')
    server = settings.servers[0]

    async def scenario() -> None:
        for name in ("Alice", "Bob", "Olga"):
            ok, text, _ = await relay._run_command(session(settings, name), server, "say", ["hello"])
            assert not ok and text == "'say' is switched off in commands.toml ([off]), for everyone."
            ok, _, _ = await relay._run_command(session(settings, name), server, "kick #3 x")
            assert not ok
        assert client.sent == []
        ok, _, _ = await relay._run_command(session(settings, "Olga"), server, "ban", ["#3"])
        assert ok and client.sent == [("ban", ("#3",), "Olga")]

    run(scenario())


def test_moderators_get_replies_without_addresses_or_devices(tmp_path: Path, run) -> None:
    relay, _, settings = relay_for(tmp_path)
    server = settings.servers[0]

    async def scenario() -> None:
        alice = session(settings, "Alice")
        ok, text, data = await relay._run_command(alice, server, "players")
        assert ok and "203.0.113.4" not in text and FINGERPRINT not in text and PLAYER[:8] in text
        assert data == {"count": 1, "players": [{"number": 1, "name": "Dee", "id": PLAYER, "admin": False}]}
        _, text, data = await relay._run_command(alice, server, "bans")
        dump = text + json.dumps(data)
        assert "198.51.100" not in dump and FINGERPRINT not in dump and "2001:db8" not in dump
        assert "Device ban: (hidden)" in text and "cheating" in text and data["players"] == BANS_DATA["players"]
        assert data["devices"] == [{"name": "Bravo", "reason": "cheating", "group": 3}]
        _, text, data = await relay._run_command(alice, server, "vpn")
        assert "203.0.113.9" not in text + json.dumps(data) and data["allowed"][1]["target"] == PLAYER
        _, _, data = await relay._run_command(alice, server, "status")
        assert "address" not in data and data["name"] == "Alpha"

        # Admins and owners get it all, as the server sends it.
        ok, text, data = await relay._run_command(session(settings, "Bob"), server, "bans")
        assert text == BANS_TEXT and data == BANS_DATA
        _, text, data = await relay._run_command(session(settings, "Olga"), server, "players")
        assert text == PLAYERS_TEXT and data == PLAYERS_DATA

    run(scenario())


def test_addresses_out_of_text_leave_the_rest_alone() -> None:
    text, data = without_addresses("03:07:07 Dee joined from 203.0.113.4:11774, v0.9.20, 4:55 left, map 1.2.3",
                                   None)
    assert text == "03:07:07 Dee joined from (hidden), v0.9.20, 4:55 left, map 1.2.3" and data is None


def test_events_reach_moderators_without_devices(tmp_path: Path) -> None:
    relay, _, settings = relay_for(tmp_path)
    alice = next(u for u in settings.relay.users if u.name == "Alice")
    bob = next(u for u in settings.relay.users if u.name == "Bob")
    ban = Event("rcon.ban", "Alpha", {"name": "Bravo", "id": PLAYER, "ip": "198.51.100.7", "device": FINGERPRINT,
                                      "by": "Owner (RCON)", "reason": "cheating", "expires": 0})
    message = relay._event_message(ban, alice)
    assert message is not None and "ip" not in message and "device" not in message and message["id"] == PLAYER
    assert relay._event_message(ban, bob)["device"] == FINGERPRINT  # type: ignore[index]


def test_args_are_refused_over_the_websocket_too(tmp_path: Path, run) -> None:
    from websockets.asyncio.client import connect

    port, relay_port = free_ports(2)
    relay, client, _ = relay_for(tmp_path, port=port, offset=relay_port - port)

    async def scenario() -> None:
        task = asyncio.ensure_future(relay.run())
        await wait_for(lambda: "Alpha" in relay.listeners)
        try:
            async with connect(f"ws://127.0.0.1:{relay_port}/", subprotocols=["reclaimer-rcon"]) as ws:
                await ws.send(json.dumps({"type": "auth", "password": "alicealice"}))
                assert json.loads(await ws.recv())["ok"]
                await ws.send(json.dumps({"type": "command", "id": 1, "command": "password", "args": [""]}))
                reply = json.loads(await ws.recv())
                assert reply["id"] == 1 and not reply["ok"] and "blank word" in reply["text"]
                await ws.send(json.dumps({"type": "command", "id": 2, "command": "players"}))
                reply = json.loads(await ws.recv())
                assert reply["ok"] and "ip" not in reply["data"]["players"][0]
            assert client.sent == [("players", (), "Alice")]
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    run(scenario())
