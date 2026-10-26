from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, free_port, staff, wait_for, write_server_folder, write_settings
from testkit.commands import FrozenServer, OtherServer, ThawingServer
from testkit.running import everyone_joins, running, says, tells_to

from huragok.core.events import Event, EventBus
from huragok.core.part import CommandSpec
from huragok.core.settings import load
from huragok.core.storage import Storage
from huragok.players.roles import Roles
from huragok.players.router import CommandCall, CommandRouter, split_words, wrap
from huragok.rcon import RconTimeout, Reply
from huragok.servers.state import Player, ServerState

SERVER = "LG Test Server"
ZED = "9" * 64


class FakeClient:
    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args))
        return Reply(True, "ok", None, {})


def _setup(tmp_path: Path) -> tuple[CommandRouter, EventBus, ServerState, FakeClient]:
    write_server_folder(tmp_path / "server", free_port())
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
    from huragok.servers.state import StateTracker
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


# Replies that can't be delivered --------------------------------------------------------------

def test_a_reply_the_console_cant_deliver_is_dropped_not_raised(run) -> None:
    told: list[str] = []

    async def slow(server: str, player_id: str, text: str) -> None:
        told.append(text)
        raise RconTimeout("no reply to 'tell' from LG Test Server")

    state = ServerState(SERVER)
    call = CommandCall(SERVER, Player(1, "Mod", MOD), "moderator", "players", [], "", "all", state, slow)
    run(call.reply("one line\nanother line\na third"))  # no exception, and no more tries after the first
    assert told == ["one line"]


def test_slow_replies_dont_switch_the_commands_off(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            sup.superintendent.clients[SERVER].command_timeout = 0.2
            fake.replies["tell"] = lambda m: None  # the server is too busy to answer
            for _ in range(6):
                await fake.chat(MOD, "Mod", "!players")
            await wait_for(lambda: len(fake.named("tell")) >= 6, timeout=10)
            runner = sup.runners["commands"]
            await asyncio.sleep(0.5)
            assert runner.enabled and not runner._failures
            del fake.replies["tell"]
            await fake.chat(MOD, "Mod", "!players")
            await wait_for(lambda: any("#4 Bravo" in t for t in tells_to(fake, MOD)))

    run(scenario())


# Every command, as the console receives it ------------------------------------------------------

COMMANDS = [  # (caller, the line, the command the server gets, its args)
    ("Mod", "!warn bravo spamming", "tell", [BRAVO, "Warning from Mod: spamming"]),
    ("Mod", "!unmute bravo", "unmute", [BRAVO]),
    ("Mod", "!pardon Bravo", "pardon", ["Bravo"]),
    ("Owner", "!unban Bravo", "unban", ["Bravo"]),
    ("Owner", "!say hello everyone", "say", ["hello everyone"]),
    ("Owner", "!mode Slayer", "mode Slayer", []),
    ("Owner", "!load Guardian Slayer", "load Guardian Slayer", []),
    ("Owner", "!nextmap Guardian", "nextmap Guardian", []),
    ("Owner", "!endgame", "endgame", []),
    ("Owner", "!endround", "endround", []),
    ("Owner", "!shuffle", "shuffle", []),
    ("Owner", "!team bravo red", "team", [BRAVO, "red"]),
    ("Owner", "!teamcount 4", "teamcount 4", []),
    ("Owner", "!startvote kick bravo", "startvote", ["kick", BRAVO]),
    ("Owner", "!startvote endround", "startvote endround", []),
    ("Owner", "!cancelvote", "cancelvote", []),
    ("Owner", "!passvote", "passvote", []),
    ("Owner", "!maxping 150", "maxping 150", []),
    ("Owner", "!servername LG Night Games", "servername LG Night Games", []),
    ("Owner", "!admin add bravo", "admin", ["add", BRAVO]),
    ("Owner", "!admin list", "admin list", []),
    ("Owner", "!reload", "reload", []),
]
IDS = {"Mod": MOD, "Owner": OWNER}


def test_every_command_reaches_the_console_as_who_typed_it(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            for caller, line, command, args in COMMANDS:
                before = len(fake.commands)
                by = None if command == "tell" else caller  # a warning is a private line, from the console

                def sent() -> bool:
                    return any(c["command"] == command and c.get("args", []) == args and c.get("by") == by
                               for c in fake.commands[before:])

                await fake.chat(IDS[caller], caller, line)
                await wait_for(sent)
            warn = sup.storage.actions_for(BRAVO)
            assert any(row["kind"] == "warn" and row["by"] == "Mod" and row["reason"] == "spamming" for row in warn)

            await fake.chat(MOD, "Mod", "!whois bravo")  # read from Huragok's records: nothing sent
            await wait_for(lambda: any("by Mod (!warn): spamming" in t for t in tells_to(fake, MOD)))
            assert any(t.startswith("Bravo (dddddddd): ") for t in tells_to(fake, MOD))

    run(scenario())


def test_an_action_whose_reply_times_out_is_recorded_once_and_not_sent_again(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            sup.superintendent.clients[SERVER].command_timeout = 0.3
            fake.replies["kick"] = lambda m: None
            await fake.chat(MOD, "Mod", "!kick bravo spam")
            await wait_for(lambda: any(t.startswith("Couldn't: no reply to 'kick'") for t in tells_to(fake, MOD)))
            await asyncio.sleep(0.5)
            assert len(fake.named("kick")) == 1
            rows = [r for r in sup.storage.actions_for(BRAVO) if r["kind"] == "kick"]
            assert len(rows) == 1 and rows[0]["ok"] == 0 and "no reply" in rows[0]["reply"]
            actions = [e for e in seen if e.name == "moderation.action" and e["kind"] == "kick"]
            assert len(actions) == 1 and actions[0]["ok"] is False

    run(scenario())


# Who a command is for ----------------------------------------------------------------------------

def test_admins_cant_ban_owners_or_admins_by_player_id(tmp_path: Path, run) -> None:
    other_admin = "1" * 64

    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins.append(other_admin)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5 and sup.superintendent.states[SERVER].admins)
            await fake.chat(ADMIN, "Admin", f"!ban {OWNER} 7d")
            await wait_for(lambda: any("is an owner; you can't do that to them" in t for t in tells_to(fake, ADMIN)))
            await fake.chat(ADMIN, "Admin", f"!ban {other_admin}")
            await wait_for(lambda: any("is an admin; you can't do that to them" in t for t in tells_to(fake, ADMIN)))
            assert not fake.named("ban")
            await fake.chat(ADMIN, "Admin", f"!ban {ZED} 7d alt")  # anyone else, offline, by ID: fine
            assert (await wait_for(lambda: fake.named("ban")))[0]["args"] == [ZED, "7d", "alt"]

    run(scenario())


def test_a_name_start_never_lands_on_someone_else(tmp_path: Path, run) -> None:
    bob, bobby = "b0b" + "2" * 61, "6" * 64

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            sup.storage.record_join(bob, "Bob", None)  # Bob was here, and left
            await everyone_joins(fake)
            await fake.join(6, "Bobby", bobby)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 6)
            await fake.chat(MOD, "Mod", "!kick bob")
            await wait_for(lambda: any("Bob isn't on this server now." in t for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!mute bobb 10m")
            await wait_for(lambda: any("Nobody here is called 'bobb'. Did you mean Bobby (#6)? Use #6." in t
                                       for t in tells_to(fake, MOD)))
            assert not fake.named("kick") and not fake.named("mute")
            await fake.chat(MOD, "Mod", "!mute 6 10m")  # the number alone is the #number, as on the console
            assert (await wait_for(lambda: fake.named("mute")))[0]["args"] == [bobby, "10m"]
            await fake.chat(OWNER, "Owner", "!ban bob 1d spam")  # seen: banned by their ID while away
            assert (await wait_for(lambda: fake.named("ban")))[0]["args"] == [bob, "1d", "spam"]
            await fake.chat(OWNER, "Owner", "!admin add bobb")  # the start of a name never makes an admin
            await wait_for(lambda: any("Did you mean Bobby (#6)?" in t for t in tells_to(fake, OWNER)))
            assert not [c for c in fake.named("admin") if c.get("args")]  # (the bare one is the state refresh)
            await fake.chat(MOD, "Mod", f"!unmute {ZED}")  # never seen: the server unmutes by ID
            assert (await wait_for(lambda: fake.named("unmute")))[0]["args"] == [ZED]
            await fake.chat(MOD, "Mod", "!unmute bobb")  # lifting is harmless: the start of a name will do
            await wait_for(lambda: len(fake.named("unmute")) == 2)
            assert fake.named("unmute")[1]["args"] == [bobby]

    run(scenario())


def test_a_name_that_only_looks_like_it_is_asked_about_never_acted_on(tmp_path: Path, run) -> None:
    """Letters of another script or style (a Greek Β, small capitals): "!kick bravo" for a player who wrote ΒRAVO is
    nobody called that, and here's who looks like it, picked by number. And with the real Bravo on beside an
    impersonator, the name alone acts on neither: the caller picks, as between two players with one name."""
    fake_bravo, zulu = "9f" * 32, "8e" * 32

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await fake.join(6, "ΒRΑVΟ", fake_bravo)  # Greek capital Β, Α, Ο
            await fake.join(7, "ᴢᴜʟᴜ", zulu)  # small capitals
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 7)
            await fake.chat(MOD, "Mod", "!kick zulu spam")
            await wait_for(lambda: any(t.startswith("Nobody here is called 'zulu'. This looks like it: #7 ᴢᴜʟᴜ")
                                       and t.endswith("Use the #number.") for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!kick bravo spam")  # Bravo (#4) and ΒRΑVΟ (#6) both on
            await wait_for(lambda: any(t.startswith("'Bravo' could be #4 Bravo") and "#6 ΒRΑVΟ" in t
                                       for t in tells_to(fake, MOD)))
            assert not fake.named("kick")
            await fake.chat(MOD, "Mod", "!kick #6 spam")  # picked by number
            assert (await wait_for(lambda: fake.named("kick")))[0]["args"] == [fake_bravo, "spam"]

    run(scenario())


def test_players_with_one_name_are_told_apart_and_picked_by_number(tmp_path: Path, run) -> None:
    """The host's ask: when players share a name, every command that takes one lists them with what tells them apart
    (as their name is written, team, time on or when last here, role, the start of their ID), #number for those on
    this server and @number for those who aren't, and acts on nobody until one is picked."""
    bob_here, bob_gone, bob_old = "1a" * 32, "2b" * 32, "3c" * 32

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            sup.storage.record_join(bob_old, "BOB", None, when=time.time() - 3 * 86400)
            sup.storage.record_join(bob_gone, "bob", None, when=time.time() - 2 * 3600)
            await everyone_joins(fake)
            await fake.join(6, "Bob", bob_here)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 6)
            sup.superintendent.states[SERVER].players[6].team = "Red"
            # !kick only reaches players on this server: one Bob is, so it's him.
            await fake.chat(MOD, "Mod", "!kick bob spamming")
            assert (await wait_for(lambda: fake.named("kick")))[0]["args"] == [bob_here, "spamming"]
            joined = sup.superintendent.states[SERVER].players[6].joined
            await fake.join(6, "Bob", bob_here)
            await wait_for(lambda: sup.superintendent.states[SERVER].players[6].joined != joined)
            sup.superintendent.states[SERVER].players[6].team = "Red"
            # !ban reaches players who left too: three Bobs, so nobody is banned until one is picked.
            await fake.chat(OWNER, "Owner", "!ban bob 7d cheating")
            asked = await wait_for(lambda: [" ".join(tells_to(fake, OWNER))] if "number." in " ".join(
                tells_to(fake, OWNER)) else [])
            assert re.fullmatch(r"'Bob' could be #6 Bob \(red, on \d+s, 1a1a1a1a\); @1 bob \(seen 2h ago, 2b2b2b2b\); "
                                r"@2 BOB \(seen 3d ago, 3c3c3c3c\)\. Use the #number or @number\.", asked[0]), asked
            assert not fake.named("ban")
            await fake.chat(OWNER, "Owner", "!ban @2 7d cheating")  # the one seen 3 days ago, by his player ID
            assert (await wait_for(lambda: fake.named("ban")))[0]["args"] == [bob_old, "7d", "cheating"]
            await fake.chat(OWNER, "Owner", "!ban @5 7d")
            await wait_for(lambda: any(t == "Your last list has @1 to @2." for t in tells_to(fake, OWNER)))
            await fake.chat(MOD, "Mod", "!kick @1")  # a list is the caller's own
            await wait_for(lambda: any(t.startswith("You have no list for @1") for t in tells_to(fake, MOD)))
            await fake.chat(OWNER, "Owner", "!kick @1")  # @1 left: !kick can't reach him
            await wait_for(lambda: any(t == "bob isn't on this server now." for t in tells_to(fake, OWNER)))
            sup.vergil.commands.picks._lists[OWNER] = (time.monotonic() - 601, sup.vergil.commands.picks._lists[OWNER][1])
            await fake.chat(OWNER, "Owner", "!ban @1")
            await wait_for(lambda: any("over 10 minutes old: ask again" in t for t in tells_to(fake, OWNER)))
            assert len(fake.named("ban")) == 1

    run(scenario())


def test_bans_and_kicks_are_lists_to_lift_from_by_number(tmp_path: Path, run) -> None:
    """!bans and !kicks number their entries, and !unban / !pardon take the number (or a name, asking when two
    entries share it): nobody lifts the wrong player's ban because they share a name."""
    one, two = "1a" * 32, "2b" * 32

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            now = int(time.time())
            fake.ban_list = [
                {"id": one, "name": "Bob", "reason": "cheating", "banned_by": "Alice (RCON)", "banned_at": now - 7200},
                {"id": two, "name": "bob", "reason": "slurs", "banned_by": "Vergil (RCON)", "banned_at": now - 60,
                 "expires": now + 6 * 86400 + 100}]
            await fake.chat(ADMIN, "Admin", "!bans")
            await wait_for(lambda: any("unban @number lifts one" in t for t in tells_to(fake, ADMIN)))
            said = tells_to(fake, ADMIN)
            assert said[0] == f"@1 bob (banned 1m ago by Vergil: slurs; 6d left; this server, {two[:8]})"
            assert said[1] == f"@2 Bob (banned 2h ago by Alice: cheating; this server, {one[:8]})"
            await fake.chat(ADMIN, "Admin", "!unban @2")  # Alice's ban on the first Bob, by his player ID
            assert (await wait_for(lambda: fake.named("unban")))[0]["args"] == [one]
            fake.ban_list.append({"id": one, "name": "Bob", "reason": "again", "banned_at": now})
            await fake.chat(ADMIN, "Admin", "!unban bob")  # two bans with that name: asked, nothing lifted
            await wait_for(lambda: any(t.startswith("'bob' could be @1 Bob (banned") for t in tells_to(fake, ADMIN)))
            assert len(fake.named("unban")) == 1
            await fake.chat(ADMIN, "Admin", "!unban @2")
            await wait_for(lambda: len(fake.named("unban")) == 2)
            assert fake.named("unban")[1]["args"] == [two]
            await fake.chat(ADMIN, "Admin", "!bans nobody")
            await wait_for(lambda: any(t == "Nobody is banned with 'nobody' in their name." for t in tells_to(fake, ADMIN)))

            # Kicks: listed with who, why and how long they're out; !pardon @number lets that one back.
            await fake.chat(MOD, "Mod", "!kick bravo spamming")
            await wait_for(lambda: fake.named("kick"))
            await fake.chat(MOD, "Mod", "!kicks")
            await wait_for(lambda: any(t.startswith("@1 Bravo (kicked") for t in tells_to(fake, MOD)))
            listed = next(t for t in tells_to(fake, MOD) if t.startswith("@1 Bravo"))
            assert "by Mod: spamming; out 9m more" in listed or "by Mod: spamming; out 10m more" in listed
            await fake.chat(MOD, "Mod", "!pardon @1")
            assert (await wait_for(lambda: fake.named("pardon")))[0]["args"] == [BRAVO]
            await fake.chat(OWNER, "Owner", "!kicks")  # pardoned: off the list
            await wait_for(lambda: any(t.startswith("Nobody has been kicked here") for t in tells_to(fake, OWNER)))

    run(scenario())


def test_said_shows_what_a_player_typed_even_after_they_left(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            for n in range(7):
                await fake.chat(BRAVO, "Bravo", f"line {n}", channel="team" if n == 6 else "all")
            await fake.chat(ALICE, "Alice", "hello")
            await wait_for(lambda: len(sup.storage.query("SELECT * FROM commands_chat")) == 8)
            await fake.leave(4)
            await wait_for(lambda: 4 not in sup.superintendent.states[SERVER].players)
            await fake.chat(MOD, "Mod", "!said bravo")  # gone, found by the name he used: the last five, oldest first
            await wait_for(lambda: any("(team) Bravo: line 6" in t for t in tells_to(fake, MOD)))
            said = [t for t in tells_to(fake, MOD) if "Bravo: line" in t]
            assert [t.split("Bravo: ")[1] for t in said] == ["line 2", "line 3", "line 4", "line 5", "line 6"]
            assert said[0].startswith("0s ago Bravo") or said[0].endswith("ago Bravo: line 2")
            await fake.chat(OWNER, "Owner", "!said bravo 2")
            await wait_for(lambda: len([t for t in tells_to(fake, OWNER) if "Bravo: line" in t]) == 2)
            await fake.chat(OWNER, "Owner", "!said admin")  # (commands are chat lines too: the Owner's own are kept)
            await wait_for(lambda: any(t.startswith("Admin hasn't typed anything") for t in tells_to(fake, OWNER)))

    run(scenario())


@pytest.mark.parametrize("across", [True, False])
def test_kicks_and_bans_reach_every_server(tmp_path: Path, run, across: bool) -> None:
    """The host's ask: a kick or a ban on one server reaches the others ([automod] across_servers, on by default): a
    kicked player is kept off every server until the kick's time is up, and a ban reaches every server that keeps a
    ban list of its own. A vote's kick stays where it happened."""
    async def scenario() -> None:
        body = "" if across else "[automod]\nacross_servers = false\n"
        async with running(tmp_path, body) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = OtherServer()
            sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
            sup.superintendent.states["Other"] = ServerState("Other", connected=True, players={2: Player(2, "Bravo", BRAVO)})
            await fake.chat(MOD, "Mod", "!kick bravo spamming")  # on both servers: off both
            await wait_for(lambda: fake.named("kick"))
            if across:
                kick = (await wait_for(lambda: other.named("kick")))[0]
                assert kick[0] == BRAVO and kick[1].startswith("Kicked from LG Test Server by Mod (spamming): you can "
                                                               "join again in ")
                await wait_for(lambda: other.named("pardon"))  # let back in: the block is the first server's
                sup.bus.emit("rcon.join", "Other", number=3, name="Bravo", id=BRAVO, ip="", admin=False)
                await wait_for(lambda: len(other.named("kick")) == 2)  # joining meanwhile: kicked as they join
            # A vote's kick stays where it happened, and so do the game's own /kick and /ban (an admin in game: the
            # server names them without "(RCON)"): / is that server's, ! every server's.
            sup.bus.emit("rcon.kick", SERVER, id=ALICE, name="Alice", by="vote", reason="", seconds=600)
            sup.bus.emit("rcon.kick", SERVER, id=ALICE, name="Alice", by="LilGremlin", reason="name bad", seconds=600)
            sup.bus.emit("rcon.ban", SERVER, id="7" * 64, name="Grok", by="UrDadsBull", reason="spam", expires=0)
            sup.bus.emit("rcon.join", "Other", number=4, name="Alice", id=ALICE, ip="", admin=False)
            # A ban reaches a server with a ban list of its own, for the time it has left; an unban too.
            sup.bus.emit("rcon.ban", SERVER, id=ZED, name="Zed", by="Alice (RCON)", reason="cheating",
                         expires=int(time.time()) + 3600)
            sup.bus.emit("rcon.unban", SERVER, target=ZED, by="Owner (RCON)")
            if across:
                ban = (await wait_for(lambda: other.named("ban")))[0]
                assert ban[0] == ZED and 3590 <= int(ban[1][:-1]) <= 3600 and ban[2] == "cheating"
                assert next(by for command, _, by in other.sent if command == "ban") == "Alice"
                assert (await wait_for(lambda: other.named("unban")))[0] == (ZED,)
                sup.bus.emit("rcon.ban", "Other", id=ZED, name="Zed", by="Alice (RCON)", reason="cheating", expires=0)
            # Moderation commands stay each server's own: someone on another server is that server's to mute.
            sup.superintendent.states["Other"].players[5] = Player(5, "Zed", ZED)
            await fake.chat(MOD, "Mod", f"!mute {ZED} 10m")
            await wait_for(lambda: any(t == f"{ZED[:8]} is on Other, not this server." for t in tells_to(fake, MOD)))
            await asyncio.sleep(0.3)
            assert not [k for k in other.named("kick") if k[0] == ALICE] and not other.named("mute")
            assert [c for c in fake.named("ban") if c.get("args", [""])[0] == ZED] == []  # never sent back
            assert not [b for b in other.named("ban") if b[0] == "7" * 64]  # the /ban stayed on its server
            # …but both are on the players' records, for !whois.
            assert ("kick", "in game", "LilGremlin") in [(r["kind"], r["source"], r["by"])
                                                         for r in sup.storage.actions_for(ALICE)]
            if not across:
                assert other.sent == []
            sup.superintendent.states.pop("Other", None)
            sup.superintendent.clients.pop("Other", None)

    run(scenario())


def test_whois_is_a_rap_sheet_of_everything_however_it_was_given(tmp_path: Path, run) -> None:
    """The host's ask: Huragok keeps a ledger of everyone kicked, muted, banned and warned, with how (a ! command, the
    game's own /, Vergil, a vote, the console), by whom and on which server, and !whois reads it back. Only Huragok's
    own actions are acted on; the rest are history."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text("badword | | | | slurs\n", encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\n") as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!warn bravo stop spamming")
            await wait_for(lambda: len(sup.storage.actions_for(BRAVO)) == 1)
            await fake.event("kick", id=BRAVO, name="Bravo", by="LilGremlin", reason="name bad", seconds=600)  # /kick
            await fake.event("kick", id=BRAVO, name="Bravo", by="vote", reason="", seconds=1800)
            await fake.chat(MOD, "Mod", "!mute bravo 1d spamming")
            await wait_for(lambda: fake.named("mute"))
            # The server announces Huragok's own mute back: not written twice.
            await fake.event("mute", id=BRAVO, name="Bravo", by="Mod (RCON)", reason="spamming",
                             expires=int(time.time()) + 86400)
            await fake.event("ban", id=BRAVO, name="Bravo", by="Bob (RCON)", reason="cheating", expires=0)  # the relay
            await wait_for(lambda: len(sup.storage.actions_for(BRAVO)) == 5)
            await asyncio.sleep(0.2)
            assert sorted((r["kind"], r["source"]) for r in sup.storage.actions_for(BRAVO)) == [
                ("ban", "console"), ("kick", "in game"), ("kick", "vote"), ("mute", "commands"), ("warn", "commands")]
            assert not [c for c in fake.named("pardon")]  # history only: nothing done about the /kick

            await fake.chat(MOD, "Mod", "!whois bravo")
            await wait_for(lambda: any("by Mod (!warn): stop spamming" in t for t in tells_to(fake, MOD)))
            sheet = tells_to(fake, MOD)
            sheet = sheet[next(n for n, t in enumerate(sheet) if t.startswith("Bravo (dddddddd): ")):]
            assert sheet[0].startswith("Bravo (dddddddd): on LG Test Server as #4 · 1 visit since")
            text = " ".join(sheet)
            now = text[text.index("Now: "):text.index("Record: ")]
            assert "muted 23h left (by Mod: spamming)" in now and "kicked from LG Test Server, out " in now
            assert "out 29m more (by a vote)" in now  # the kick keeping them out: the vote's, after the /kick
            assert "banned on every server, for good (by Bob: cheating)" in now  # the relay's: Huragok's list
            record = next(t for t in sheet if t.startswith("Record: "))
            assert record.startswith("Record: 1 ban, 2 kicks, 1 mute, 1 warn (") and "by /" in record
            assert any(t.endswith("LG Test Server: kick 10m by LilGremlin (/kick): name bad") for t in sheet)
            assert any(t.endswith("LG Test Server: kick 30m by a vote") for t in sheet)
            assert any(t.endswith("LG Test Server: mute 1d by Mod (!mute): spamming") for t in sheet)
            assert any(t.endswith("LG Test Server: ban by Bob (console ban): cheating") for t in sheet)
            await fake.chat(OWNER, "Owner", "!whois bravo 2")  # the latest two, and how to see more
            await wait_for(lambda: any(t.startswith("…3 older: !whois Bravo 5") for t in tells_to(fake, OWNER)))
            # "Player 2" alone is a name; with "Player" on too, the 2 could be his lines: asked, never guessed.
            await fake.join(6, "Player 2", "6a" * 32)
            await wait_for(lambda: 6 in sup.superintendent.states[SERVER].players)
            await fake.chat(OWNER, "Owner", "!whois player 2")
            await wait_for(lambda: any(t.startswith("Player 2 (6a6a6a6a): on LG Test Server as #6")
                                       for t in tells_to(fake, OWNER)))
            await fake.join(7, "Player", "7b" * 32)
            await wait_for(lambda: 7 in sup.superintendent.states[SERVER].players)
            await fake.chat(OWNER, "Owner", "!whois player 2")
            await wait_for(lambda: any(t.startswith("'player 2' could be #6 Player 2") and "#7 Player (" in t
                                       for t in tells_to(fake, OWNER)))
            await wait_for(lambda: any(t == "A number after the player is how many lines: !whois #number 2."
                                       for t in tells_to(fake, OWNER)))

    run(scenario())


def test_huragoks_own_bans_are_on_every_servers_list(tmp_path: Path, run) -> None:
    """The host's call: every server keeps a ban list of its own, and Huragok keeps its own list of the bans it gives
    (! commands, Vergil) and the admins it makes, put on every server's: as given, again as a server connects (one
    added since, or reset), and on a player who gets in anyway. A /ban stays its server's until !banall."""
    gone = "7" * 64

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = OtherServer()
            sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
            sup.superintendent.states["Other"] = ServerState("Other", connected=True, admins=set())
            try:
                await fake.chat(OWNER, "Owner", "!ban bravo 7d cheating")  # through Huragok: every server's
                ban = (await wait_for(lambda: other.named("ban")))[0]
                assert ban[0] == BRAVO and 604000 < int(ban[1][:-1]) <= 604800 and ban[2] == "cheating"
                assert next(by for command, _, by in other.sent if command == "ban") == "Owner"
                # The other server's list was reset (a new server, a lost file): put back as its console connects.
                other.banned.clear()
                sup.bus.emit("rcon.connected", "Other", version="0.9.22")
                await wait_for(lambda: BRAVO in other.banned)
                # On Huragok's list but not on the server's yet, and got in: banned there as they join (one sent there
                # moments ago is let be: that join was from before it landed).
                other.banned.clear()
                sup.runners["commands"].part._across_sent().clear()
                sup.bus.emit("rcon.join", "Other", number=3, name="Bravo", id=BRAVO, ip="", admin=False)
                await wait_for(lambda: BRAVO in other.banned)
                # !unban lifts it everywhere: off Huragok's list, off every server's.
                await fake.chat(OWNER, "Owner", f"!unban {BRAVO}")
                await wait_for(lambda: BRAVO not in other.banned)
                sup.bus.emit("rcon.connected", "Other", version="0.9.22")
                await asyncio.sleep(0.3)
                assert BRAVO not in other.banned

                # The game's own /ban stays its server's; !bans says which are which; !banall makes it every server's.
                fake.ban_list = [{"id": gone, "name": "Grok", "reason": "spam bot", "banned_by": "LilGremlin",
                                  "banned_at": int(time.time())}]
                sup.bus.emit("rcon.ban", SERVER, id=gone, name="Grok", by="LilGremlin", reason="spam bot", expires=0)
                await asyncio.sleep(0.3)
                assert gone not in other.banned
                await fake.chat(ADMIN, "Admin", "!bans")
                await wait_for(lambda: any(t.startswith("@1 Grok (banned") and "; this server" in t
                                           for t in tells_to(fake, ADMIN)))
                await fake.chat(ADMIN, "Admin", "!banall @1")
                await wait_for(lambda: any(t.startswith("Grok's ban (by LilGremlin: spam bot, for good) is on every "
                                                        "server now") for t in tells_to(fake, ADMIN)))
                assert gone in other.banned
                assert next(args for command, args, _ in other.sent if command == "ban" and args[0] == gone) == (
                    gone, "spam bot")
                await fake.chat(ADMIN, "Admin", "!banall @1")
                await wait_for(lambda: any(t == "Grok's ban is on every server already." for t in tells_to(fake, ADMIN)))
                await fake.chat(MOD, "Mod", "!banall @1")
                await wait_for(lambda: any("!banall is for admins and up" in t for t in tells_to(fake, MOD)))
                # The window's B screen does the same (E), by the name the list shows.
                zed = "8" * 64
                fake.ban_list.append({"id": zed, "name": "Zed", "reason": "", "banned_by": "LilGremlin",
                                      "banned_at": int(time.time())})
                [(where, result)] = await sup.superintendent.fleet.verb("banall", SERVER, target="zed", by="Host")
                assert where == SERVER and result.ok and zed in other.banned
                assert result.message.startswith("Zed's ban (by LilGremlin, for good) is on every server now")
                [(_, result)] = await sup.superintendent.fleet.verb("banall", SERVER, target="nobody", by="Host")
                assert not result.ok and "Nothing on LG Test Server's ban list is 'nobody'" in result.message

                # An admin Huragok makes is one on every server, and stays on Huragok's list for new ones.
                await fake.chat(OWNER, "Owner", "!admin add alice")
                await wait_for(lambda: ALICE in other.admins)
                other.admins.clear()
                sup.superintendent.states["Other"].admins = set()
                sup.bus.emit("rcon.connected", "Other", version="0.9.22")
                await wait_for(lambda: ALICE in other.admins)
            finally:
                sup.superintendent.states.pop("Other", None)
                sup.superintendent.clients.pop("Other", None)

    run(scenario())


def test_two_frozen_servers_hold_up_neither_a_ban_nor_the_commands(tmp_path: Path, run) -> None:
    """Servers frozen with their consoles still connected (each command waits out its timeout) used to be waited on
    in turn by the handler passing a ban on, past the part's time limit (a failure; five switch the commands off).
    Each server has a queue of its own now: the others get it at once, and the frozen ones hold up only themselves."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other, frozen = OtherServer(), [FrozenServer(4.0), FrozenServer(4.0)]
            for name, client in (("Other", other), ("Frozen 1", frozen[0]), ("Frozen 2", frozen[1])):
                sup.superintendent.clients[name] = client  # type: ignore[assignment]
                sup.superintendent.states[name] = ServerState(name, connected=True, admins=set())
            try:
                started = time.monotonic()
                await fake.chat(OWNER, "Owner", "!ban bravo 7d cheating")
                await wait_for(lambda: BRAVO in other.banned)
                assert time.monotonic() - started < 3  # not behind the frozen servers' 4 s apiece
                await wait_for(lambda: all(f.named("ban") for f in frozen))  # each tried, side by side
                await fake.chat(OWNER, "Owner", f"!unban {BRAVO}")  # asks the others about their own bans: 6 s at most
                await wait_for(lambda: any("Frozen 1, Frozen 2 didn't answer" in t for t in tells_to(fake, OWNER)), 15)
                assert not sup.runners["commands"]._failures
            finally:
                for name in ("Other", "Frozen 1", "Frozen 2"):
                    sup.superintendent.states.pop(name, None)
                    sup.superintendent.clients.pop(name, None)

    run(scenario())


def test_a_frozen_servers_late_echo_neither_bans_again_nor_undoes_an_unban(tmp_path: Path, run,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """The simulation's B5 (26 October): a server frozen with its console up holds the ban Huragok passed on, and an
    !unban is given meanwhile. Once it thaws it carries out the ban and announces it, a minute after it was sent:
    that used to be taken for a new console ban (past a 15 s window) and put on every server again, the unban undone.
    Now an echo is matched to what was sent, whenever it comes, and the unban is asked again of a list that didn't
    answer until it does: the player is banned nowhere, Huragok's list has nothing for them, their record one ban."""
    from huragok.players.commands import across

    class Clock:  # across's own clock, a minute ahead from the thaw on (the event loop's untouched)
        ahead = 0.0

        @staticmethod
        def monotonic() -> float:
            return time.monotonic() + Clock.ahead

        @staticmethod
        def time() -> float:
            return time.time()

    monkeypatch.setattr(across, "time", Clock)
    monkeypatch.setattr(across, "UNBAN_RETRY_SECONDS", 0.5, raising=False)  # (raising: so it runs on the old code too)

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = OtherServer()
            thawing = ThawingServer(1.0, lambda event, data: sup.bus.emit(event, "Thawing", **data))
            for name, client in (("Other", other), ("Thawing", thawing)):
                sup.superintendent.clients[name] = client  # type: ignore[assignment]
                sup.superintendent.states[name] = ServerState(name, connected=True, admins=set())
            try:
                await fake.chat(OWNER, "Owner", "!ban bravo 2h echo test")
                await wait_for(lambda: BRAVO in other.banned)
                await wait_for(lambda: thawing.named("ban"))
                await fake.chat(OWNER, "Owner", f"!unban {BRAVO}")
                await wait_for(lambda: BRAVO not in other.banned, 15)
                await asyncio.sleep(2)  # the unban's read of Thawing's list times out: asked again later
                Clock.ahead = 60.0
                thawing.thaw()  # the held ban, carried out and announced a minute late
                await wait_for(lambda: BRAVO not in thawing.banned, 15)  # the unban, asked again, lifts it
                await asyncio.sleep(1.5)
                assert BRAVO not in other.banned  # not put on every server again
                commands = sup.runners["commands"].part
                assert not commands.every_server("ban", BRAVO)  # Huragok's list: nothing in force
                rows = sup.storage.query("SELECT kind FROM commands_every_server WHERE target = ?", (BRAVO,))
                assert [r["kind"] for r in rows] == ["ban"]  # one row, lifted: none for the echo
                bans = sup.storage.query("SELECT server FROM actions WHERE target_id = ? AND kind = 'ban'", (BRAVO,))
                assert len(bans) == 1, bans  # their record: the one ban
            finally:
                for name in ("Other", "Thawing"):
                    sup.superintendent.states.pop(name, None)
                    sup.superintendent.clients.pop(name, None)

    run(scenario())


def test_banall_holds_to_the_ranks_and_a_name_ending_rcon_isnt_the_console(tmp_path: Path, run) -> None:
    """!banall outranks no one !ban couldn't: an admin can't spread the game's own /ban of an owner. A ban whose time
    has run out is said so, not a failure of the part. And a player who names themselves "Mallory (RCON)" isn't the
    console: their /ban stays on their server, and the roles stay."""
    gone = "7" * 64

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = OtherServer()
            sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
            sup.superintendent.states["Other"] = ServerState("Other", connected=True, admins=set())
            try:
                fake.ban_list = [{"id": OWNER, "name": "Owner", "reason": "x", "banned_by": "Admin",
                                  "banned_at": int(time.time())},
                                 {"id": gone, "name": "Grok", "reason": "spam", "banned_by": "LilGremlin",
                                  "banned_at": int(time.time()) - 100, "expires": int(time.time()) - 5}]
                sup.bus.emit("rcon.ban", SERVER, id=OWNER, name="Owner", by="Admin", reason="x", expires=0)
                await fake.chat(ADMIN, "Admin", f"!banall {OWNER}")
                await wait_for(lambda: any("you can't do that to them" in t for t in tells_to(fake, ADMIN)))
                await fake.chat(ADMIN, "Admin", f"!banall {gone}")
                await wait_for(lambda: any(t == "Grok's ban has run out: there's nothing to put on every server."
                                           for t in tells_to(fake, ADMIN)))
                assert OWNER not in other.banned and gone not in other.banned
                assert not sup.runners["commands"]._failures
                # A player on the server called "Mallory (RCON)" bans the moderator with the game's own /ban.
                state = sup.superintendent.states[SERVER]
                state.players[9] = Player(9, "Mallory (RCON)", "9" * 64)
                sup.bus.emit("rcon.ban", SERVER, id=MOD, name="Mod", by="Mallory (RCON)", reason="", expires=0)
                await asyncio.sleep(0.5)
                assert MOD not in other.banned
                assert sup.vergil.roles.role_of(None, Player(0, "Mod", MOD)) == "moderator"
                # The console itself, named the same way by a player who isn't on: every server's.
                del state.players[9]
                sup.bus.emit("rcon.ban", SERVER, id=MOD, name="Mod", by="Mallory (RCON)", reason="", expires=0)
                await wait_for(lambda: MOD in other.banned)
            finally:
                sup.superintendent.states.pop("Other", None)
                sup.superintendent.clients.pop("Other", None)

    run(scenario())


def test_a_pardon_an_admin_and_an_unmute_are_every_servers_or_this_ones(tmp_path: Path, run) -> None:
    """The second audit (25 October): a kick through Huragok keeps the player off every server, so its pardon,
    wherever it's typed (or sent from the window), lets them back on every server; an admin made or unmade through
    the console some other way than !admin is every server's too; !unmute, like !mute, is this server's."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = OtherServer()
            sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
            sup.superintendent.states["Other"] = ServerState("Other", connected=True, admins=set(),
                                              players={7: Player(7, "Zed", ZED)})
            part = sup.runners["commands"].part

            def told_on_other() -> str:
                return " ".join(args[1] for args in other.named("tell") if args[0] == MOD)

            try:
                await fake.chat(MOD, "Mod", "!kick bravo spamming")  # off every server for its time
                await wait_for(lambda: [k for k in part.recent_kicks() if k.id == BRAVO])
                sup.bus.emit("rcon.join", "Other", number=3, name="Bravo", id=BRAVO, ip="", admin=False)
                await wait_for(lambda: other.named("kick"))  # kicked from Other as they join
                # On Other, !kicks shows the kick keeping them off it, and !pardon lifts it where it was given.
                sup.bus.emit("rcon.chat", "Other", channel="all", name="Mod", id=MOD, text="!kicks")
                await wait_for(lambda: "@1 Bravo (kicked " in told_on_other())
                assert " on LG Test Server by Mod: spamming; out " in told_on_other()
                sup.bus.emit("rcon.chat", "Other", channel="all", name="Mod", id=MOD, text="!pardon @1")
                await wait_for(lambda: "Pardoned on LG Test Server, where the kick was given: they can join every "
                                       "server again." in told_on_other())
                assert [c["args"] for c in fake.named("pardon")] == [[BRAVO]]
                assert not [k for k in part.recent_kicks() if k.id == BRAVO]
                sup.bus.emit("rcon.join", "Other", number=3, name="Bravo", id=BRAVO, ip="", admin=False)
                await asyncio.sleep(0.3)
                assert len(other.named("kick")) == 1  # not kicked again

                # A pardon from the window (the console: "Host (RCON)") on the server where it was given: the same.
                await fake.chat(MOD, "Mod", "!kick alice spamming")
                await wait_for(lambda: [k for k in part.recent_kicks() if k.id == ALICE])
                sup.bus.emit("rcon.pardon", SERVER, id=ALICE, name="Alice", by="Host (RCON)")
                await wait_for(lambda: not [k for k in part.recent_kicks() if k.id == ALICE])

                # An admin made from the window is one on every server, and on Huragok's list; unmade, the same.
                sup.bus.emit("rcon.admin_added", SERVER, id=ALICE, by="Host (RCON)")
                await wait_for(lambda: ALICE in other.admins)
                assert part.every_server("admin", ALICE)
                sup.superintendent.states["Other"].admins = {ALICE}
                sup.bus.emit("rcon.admin_removed", SERVER, id=ALICE, by="Host (RCON)")
                await wait_for(lambda: ALICE not in other.admins)
                assert not part.every_server("admin", ALICE)

                # !unmute, like !mute, is this server's: someone on another server is unmuted there.
                await fake.chat(MOD, "Mod", f"!unmute {ZED}")
                await wait_for(lambda: any(t == f"{ZED[:8]} is on Other, not this server." for t in tells_to(fake, MOD)))
                assert not fake.named("unmute") and not other.named("unmute")
            finally:
                sup.superintendent.states.pop("Other", None)
                sup.superintendent.clients.pop("Other", None)

    run(scenario())


def test_the_hosts_calls_on_ranks_roles_unbans_and_old_bans(tmp_path: Path, run) -> None:
    """The host's calls after the second audit (25 October): A, only a ban through Huragok takes roles away (a /ban
    is that server's); B, a target's rank is the highest they hold on any server; D, !unban leaves another server's
    own /ban and says where it is; E, the bans servers shared before 0.5.0 go on Huragok's list, once."""
    old = "5" * 64
    (tmp_path / "bans.json").write_text(json.dumps({"players": [
        {"id": old, "name": "Oldie", "reason": "griefing", "banned_by": "LilGremlin", "banned_at": 1},
        {"id": "6" * 64, "name": "Gone", "reason": "x", "banned_by": "LilGremlin", "banned_at": 1, "expires": 5}],
        "ips": []}), encoding="utf-8")

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            part = sup.runners["commands"].part
            # E: the shared list's bans in force are on Huragok's list, and only once.
            assert [r["name"] for r in part.every_server("ban")] == ["Oldie"]
            assert part.every_server("ban", old)[0]["by"] == "LilGremlin"
            part.lift_every_server("ban", old, "Owner")
            await part._import_shared_lists()
            assert not part.every_server("ban", old)

            other = OtherServer()
            other.admins.append(ALICE)
            sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
            sup.superintendent.states["Other"] = ServerState("Other", connected=True, admins={ALICE})
            try:
                # B: Alice is an admin on Other only; from here she's still an admin: an admin can't !ban her.
                await fake.chat(ADMIN, "Admin", "!ban alice 1d spam")
                await wait_for(lambda: any(t == "Alice is an admin; you can't do that to them." for t in
                                           tells_to(fake, ADMIN)))
                assert not [c for c in fake.named("ban") if c["args"][0] == ALICE]
                await fake.chat(MOD, "Mod", "!roles")
                await wait_for(lambda: "(on Other)" in " ".join(tells_to(fake, MOD)))
                # …and an owner's !demote takes her admin role off where she has it, not here.
                await fake.chat(OWNER, "Owner", "!demote alice")
                await wait_for(lambda: any(t == "Alice is a moderator now (was an admin)." for t in
                                           tells_to(fake, OWNER)))
                assert ALICE not in other.admins and not [c for c in fake.named("admin") if c.get("args")]

                # A: the game's own /ban of a moderator is that server's: the role stays.
                sup.bus.emit("rcon.ban", SERVER, id=MOD, name="Mod", by="LilGremlin", reason="", expires=0)
                await asyncio.sleep(0.3)
                assert MOD in sup.settings.permissions.members.get("moderator", set())

                # D: !unban lifts Huragok's ban everywhere, and leaves Other's own /ban of them, saying where.
                await fake.chat(OWNER, "Owner", "!ban bravo 7d cheating")
                await wait_for(lambda: BRAVO in other.banned)
                other.banned.append(ZED)
                other.details[ZED] = {"banned_by": "LilGremlin", "reason": "spam"}
                fake.ban_list.append({"id": ZED, "name": "Zed", "reason": "x", "banned_by": "Owner (RCON)",
                                      "banned_at": int(time.time())})
                await fake.chat(OWNER, "Owner", f"!unban {BRAVO}")
                await wait_for(lambda: BRAVO not in other.banned)
                await fake.chat(OWNER, "Owner", f"!unban {ZED}")
                await wait_for(lambda: f"Still banned on Other by LilGremlin (/ban: spam): a /ban is that server's "
                                       f"own; !unban on that server lifts it." in " ".join(tells_to(fake, OWNER)))
                assert ZED in other.banned and (ZED,) not in other.named("unban")
            finally:
                sup.superintendent.states.pop("Other", None)
                sup.superintendent.clients.pop("Other", None)

    run(scenario())


def test_whois_says_where_someone_is_banned_now(tmp_path: Path, run) -> None:
    """The host's ask: !whois shows a player's stuff from every server, to make the right call. With a ban list of
    its own for every server, "banned or not" is each server's: read from their lists (a stopped one's from the
    record), and a ban on Huragok's list is said once, as every server's."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = OtherServer()
            sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
            sup.superintendent.states["Other"] = ServerState("Other", connected=True, admins=set())
            sup.superintendent.states["Stopped"] = ServerState("Stopped", connected=False, admins=set())
            try:
                await fake.chat(OWNER, "Owner", "!ban bravo 7d cheating")  # every server's
                await wait_for(lambda: BRAVO in other.banned)
                # A /ban on Other only, and one on a server that's stopped now (its list can't be read).
                other.banned.append(ALICE)
                other.details[ALICE] = {"banned_by": "LilGremlin", "reason": "spam",
                                        "expires": int(time.time()) + 3 * 86400 + 600}
                sup.bus.emit("rcon.ban", "Stopped", id=ALICE, name="Alice", by="LilGremlin", reason="griefing",
                             expires=0)
                await wait_for(lambda: any(r["server"] == "Stopped" for r in sup.storage.actions_for(ALICE)))

                def now_line(who: str) -> str:
                    text = " ".join(tells_to(fake, MOD))
                    text = text[text.index(f"{who} ("):]
                    return text[text.index("Now: "):text.index("Record: ")].strip()

                await fake.chat(MOD, "Mod", "!whois bravo")
                await wait_for(lambda: "Record: " in " ".join(tells_to(fake, MOD)))
                assert now_line("Bravo") == "Now: banned on every server, 6d left (by Owner: cheating)"  # said once
                await fake.chat(MOD, "Mod", "!whois alice")
                await wait_for(lambda: " ".join(tells_to(fake, MOD)).count("Record: ") == 2)
                now = now_line("Alice")
                assert "banned on Other, 3d left (/ban by LilGremlin: spam)" in now
                assert "banned on Stopped, for good (/ban by LilGremlin: griefing; from the record: Stopped's list " \
                       "can't be read now)" in now
                assert "every server" not in now and "LG Test Server" not in now

                # A server that was down when a ban came off Huragok's list: taken off its list as it connects
                # again, but only the entry Huragok put there.
                sup.superintendent.states["Other"].connected = False
                await fake.chat(OWNER, "Owner", f"!unban {BRAVO}")
                await wait_for(lambda: not sup.runners["commands"].part.every_server("ban", BRAVO))
                assert BRAVO in other.banned and not other.named("unban")
                sup.superintendent.states["Other"].connected = True
                sup.bus.emit("rcon.connected", "Other", version="0.9.22")
                await wait_for(lambda: BRAVO not in other.banned)
                assert ALICE in other.banned  # its own /ban stays
            finally:
                for name in ("Other", "Stopped"):
                    sup.superintendent.states.pop(name, None)
                sup.superintendent.clients.pop("Other", None)

    run(scenario())


def test_someone_on_another_server_is_said_to_be_there(tmp_path: Path, run) -> None:
    router, bus, state, client = _setup(tmp_path)
    other = ServerState("Other Server")
    other.players[2] = Player(2, "Zed", ZED)
    router.states["Other Server"] = other
    storage = Storage(tmp_path / "records.sqlite")
    storage.record_join(ZED, "Zed", None)
    from huragok.core.storage import PlayerHistory
    router.history = PlayerHistory(storage)

    poked: list[Any] = []

    async def poke(call: CommandCall) -> None:
        poked.append(call.target)

    router.register(CommandSpec("poke", usage="<player>", player="on"), None, poke)

    async def scenario() -> None:
        await _chat(bus, MOD, "!poke zed")
        assert _tells(client)[-1] == "Zed is on Other Server, not this server." and poked == []
        await _chat(bus, OWNER, "!poke")  # (an owner: no cooldown between uses)
        assert _tells(client)[-1] == "Use: !poke <player>"
        await _chat(bus, OWNER, "!poke bravo")
        assert [p.name for p in poked] == ["Bravo"]

    run(scenario())
    storage.close()


def test_names_with_spaces_need_no_quotes(tmp_path: Path, run) -> None:
    """Players' names have spaces, and nobody types quotes: the longest name a player on (or one seen) has that the
    line starts with is theirs, and what follows is the time and the reason."""
    nate, natey, oneil, gone = "1a" * 32, "1b" * 32, "1c" * 32, "1e" * 32

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            sup.storage.record_join(gone, "Long Gone Larry", None)  # here once, and left
            await everyone_joins(fake)
            await fake.join(6, "Nate Higgers", nate)
            await fake.join(7, "Nate", natey)
            await fake.join(8, "O'Neil Jr", oneil)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 8)
            await fake.chat(MOD, "Mod", "!mute nate higgers 30m being rude")
            assert (await wait_for(lambda: fake.named("mute")))[0]["args"] == [nate, "30m", "being rude"]
            await fake.chat(MOD, "Mod", "!mute NATE 10m spamming")  # the shorter name: a time follows it
            await wait_for(lambda: len(fake.named("mute")) == 2)
            assert fake.named("mute")[1]["args"] == [natey, "10m", "spamming"]
            await fake.chat(MOD, "Mod", "!kick o'neil jr it's spam, isn't it")  # apostrophes stay as typed
            assert (await wait_for(lambda: fake.named("kick")))[0]["args"] == [oneil, "it's spam, isn't it"]
            await fake.chat(MOD, "Mod", '!warn "Nate Higgers" "last warning"')  # quotes work, as on the console
            await wait_for(lambda: any(c["args"] == [nate, "Warning from Mod: last warning"]
                                       for c in fake.named("tell")))
            await fake.chat(OWNER, "Owner", "!ban long gone larry 7d cheating")  # seen: by their ID while away
            assert (await wait_for(lambda: fake.named("ban")))[0]["args"] == [gone, "7d", "cheating"]
            await fake.chat(MOD, "Mod", "!kick nate higgins spam")  # a typo: never the shorter name's player
            await wait_for(lambda: any(t.startswith("'nate higgins…' could be #7 Nate (on ") and "1b1b1b1b); #6 Nate "
                                       "Higgers (on " in t and t.endswith("1a1a1a1a). Use the #number.")
                                       for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!kick nate spam")
            await wait_for(lambda: any(t.startswith("'nate spam…' could be #7 Nate (") for t in tells_to(fake, MOD)))
            assert len(fake.named("kick")) == 1
            await fake.chat(MOD, "Mod", "!whois long gone larry")  # someone who left, by a name they used
            await wait_for(lambda: any(t.startswith("Long Gone Larry (1e1e1e1e): last here ") for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!whois nate hig")  # looking is harmless: one name on starts so
            await wait_for(lambda: any(t.startswith("Nate Higgers (1a1a1a1a)") for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!players nate")  # a busy server lists more than one answer shows
            await wait_for(lambda: any(t.startswith("#7 Nate 1b1b1b1b (on ") for t in tells_to(fake, MOD)))
            assert any(t.startswith("#6 Nate Higgers 1a1a1a1a (on ") for t in tells_to(fake, MOD))
            await fake.chat(OWNER, "Owner", "!players zz")
            await wait_for(lambda: any(t == "Nobody on has 'zz' in their name." for t in tells_to(fake, OWNER)))
            await fake.chat(ADMIN, "Admin", "!team o'neil jr red")
            assert (await wait_for(lambda: fake.named("team")))[0]["args"] == [oneil, "red"]
            await fake.chat(ADMIN, "Admin", "!startvote kick nate higgers")
            assert (await wait_for(lambda: fake.named("startvote")))[0]["args"] == ["kick", nate]
            await fake.chat(ADMIN, "Admin", "!startvote kick owner")  # a vote on someone above them: no
            await wait_for(lambda: any("Owner is an owner; you can't do that to them." in t
                                       for t in tells_to(fake, ADMIN)))
            assert len(fake.named("startvote")) == 1

    run(scenario())


# Who's who, changed in game ------------------------------------------------------------------------------

def test_one_role_each_moved_a_step_at_a_time(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            path = sup.settings.permissions.path
            bravo = sup.superintendent.states[SERVER].players[4]

            def role() -> str:
                return sup.vergil.roles.role_of(sup.superintendent.states[SERVER], bravo)

            def said(who: str, text: str) -> Any:
                return wait_for(lambda: any(text in t for t in tells_to(fake, who)))

            # Up: a moderator makes members, an admin moderators, an owner admins; never past the caller's own.
            await fake.chat(MOD, "Mod", "!promote bravo")
            await wait_for(lambda: role() == "member")
            assert f'"{BRAVO}",  # Bravo: promoted by Mod on' in path.read_text(encoding="utf-8")
            await said(BRAVO, "Mod made you a member here.")
            await fake.chat(MOD, "Mod", "!promote bravo")
            await said(MOD, "Bravo would be a moderator, and you can promote players only to the role below yours "
                            "(a member at most).")
            await fake.chat(ADMIN, "Admin", "!promote bravo")
            await wait_for(lambda: role() == "moderator")
            members = sup.settings.permissions.members
            assert BRAVO not in members["member"] and BRAVO in members["moderator"]  # one role, never two
            assert path.read_text(encoding="utf-8").count(BRAVO) == 1
            await fake.chat(ADMIN, "Admin", "!promote bravo")
            await said(ADMIN, "(a moderator at most)")
            await fake.chat(OWNER, "Owner", "!promote bravo")
            await wait_for(lambda: role() == "admin" and BRAVO in fake.admins)
            assert BRAVO not in sup.settings.permissions.members["moderator"]
            await fake.chat(OWNER, "Owner", "!promote bravo")
            await said(OWNER, "Bravo is an admin: as high as a promotion goes")
            # Down: one step, only by someone above them.
            await fake.chat(ADMIN, "Admin", "!demote bravo")
            await said(ADMIN, "Bravo is an admin; you can't demote them.")
            await fake.chat(OWNER, "Owner", "!demote bravo")
            await wait_for(lambda: role() == "moderator" and BRAVO not in fake.admins)
            await said(OWNER, "Bravo is a moderator now (was an admin).")
            await fake.chat(MOD, "Mod", "!demote bravo")
            await said(MOD, "Bravo is a moderator; you can't demote them.")
            await fake.chat(ADMIN, "Admin", "!demote bravo")
            await wait_for(lambda: role() == "member")
            await fake.chat(MOD, "Mod", "!demote bravo")
            await wait_for(lambda: role() == "player")
            await said(MOD, "Bravo has no role now (was a member).")
            await fake.chat(MOD, "Mod", "!demote bravo")
            await said(MOD, "Bravo has no role to lose.")
            assert BRAVO not in path.read_text(encoding="utf-8")
            await fake.chat(OWNER, "Owner", "!demote owner")
            await said(OWNER, "Owner is an owner: owners are changed only in commands.toml.")
            # !admin is the owner's, on the same ladder: add makes an admin, remove a moderator.
            await fake.chat(ADMIN, "Admin", "!admin add bravo")
            await said(ADMIN, "!admin is for owners and up")
            await fake.chat(OWNER, "Owner", "!admin add bravo")
            await wait_for(lambda: role() == "admin")
            await fake.chat(OWNER, "Owner", "!admin remove bravo")
            await wait_for(lambda: role() == "moderator")
            await fake.chat(MOD, "Mod", "!roles")
            await said(MOD, "owners (1): Owner (aaaaaaaa)")
            await said(MOD, "moderators (2): Mod (bbbbbbbb), Bravo (dddddddd)")

    run(scenario())


def test_a_ban_takes_every_role_away(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!promote alice")
            await fake.chat(ADMIN, "Admin", "!promote alice")
            await wait_for(lambda: ALICE in sup.settings.permissions.members["moderator"])
            await fake.chat(ADMIN, "Admin", "!ban alice 1d spam")
            await wait_for(lambda: ALICE not in sup.settings.permissions.members["moderator"])
            await wait_for(lambda: any("Alice's moderator role goes with the ban." in t for t in tells_to(fake, ADMIN)))
            # Only an owner can ban an admin; the console's admins lose them, dedicated.toml's are named.
            await fake.chat(ADMIN, "Admin", "!ban mod")  # an admin can't ban an admin, but can a moderator
            await wait_for(lambda: MOD not in sup.settings.permissions.members["moderator"])
            await fake.chat(OWNER, "Owner", "!demote admin")
            await wait_for(lambda: any("dedicated.toml's admins still name them" in t for t in tells_to(fake, OWNER)))

    run(scenario())


def test_seen_players_by_any_name_in_any_case_and_by_a_long_enough_id_start() -> None:
    storage = Storage()
    storage.record_join("3" + "0" * 63, "ÉLODIE", None)
    storage.record_join("cafe" + "1" * 60, "Straße", None)
    assert [r.last_name for r in storage.find_players("élodie")] == ["ÉLODIE"]  # SQLite's lower() can't
    assert [r.last_name for r in storage.find_players("STRASSE")] == ["Straße"]
    assert storage.find_players("3") == [] and storage.find_players("cafe") == []  # too short to be anyone's ID
    assert [r.last_name for r in storage.find_players("CAFE1111")] == ["Straße"]
    assert [r.last_name for r in storage.find_players("3" + "0" * 63)] == ["ÉLODIE"]
    storage.close()


def _block(tmp_path: Path) -> dict[str, Any]:
    from huragok.core.tomledit import server_blocks

    return next(b for b in server_blocks(tmp_path / "server" / "dedicated.toml") if b["name"] == SERVER)


def _timers_due(sup: Any) -> None:
    """Every timed toggle's time up now: the next look (every 2 s) puts it back."""
    sup.storage.execute("UPDATE commands_timed_settings SET until = 0")


def _told(fake: Any, player_id: str, text: str) -> bool:
    return text in " ".join(tells_to(fake, player_id))  # a long reply is wrapped over several lines


def test_admins_switch_each_voice_channel_in_this_servers_own_block(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)

            await fake.chat(ADMIN, "Admin", "!toggleglobal")  # nothing set: on by default, so it goes off, and stays
            await wait_for(lambda: "Admin turned global voice (V) off." in says(fake))
            assert _block(tmp_path)["voice_global"] is False and "voice_team" not in _block(tmp_path)
            assert not sup.storage.query("SELECT * FROM commands_timed_settings")
            await fake.chat(ADMIN, "Admin", "!toggleglobal")
            await wait_for(lambda: "Admin turned global voice (V) on." in says(fake))
            # Back to what [defaults] gives, and the block didn't set it before: its own key comes out again, so a
            # later change to [defaults] reaches this server (the simulation's finding 7, 26 October).
            assert "voice_global" not in _block(tmp_path)
            assert not sup.storage.query("SELECT * FROM commands_toggled")

            await fake.chat(ADMIN, "Admin", "!toggleteam off")
            await wait_for(lambda: _block(tmp_path).get("voice_team") is False)
            await fake.chat(ADMIN, "Admin", "!toggleteam off")
            await wait_for(lambda: _told(fake, ADMIN, "Team voice (B) is already off on LG Test Server."))
            await fake.chat(ADMIN, "Admin", "!toggleteam maybe")
            await wait_for(lambda: _told(fake, ADMIN, "Use: !toggleteam [on | off] [time]"))

            await fake.chat(MOD, "Mod", "!toggleproxy")  # an admin's command
            await wait_for(lambda: any("admins and up" in t for t in tells_to(fake, MOD)))
            assert "voice_proximity" not in _block(tmp_path)
            await fake.chat(ADMIN, "Admin", "!toggleproxy")
            await wait_for(lambda: _block(tmp_path).get("voice_proximity") is False)
            await fake.chat(ADMIN, "Admin", "!toggleglobal")
            await wait_for(lambda: _told(fake, ADMIN, "Note: V, B and N are all off now: nobody hears anyone."))

            # The server refusing the file (another edit broke it) reaches the admin who toggled, once.
            await fake.chat(ADMIN, "Admin", "!toggleproxy on")
            await wait_for(lambda: "voice_proximity" not in _block(tmp_path))
            await fake.event("control", action="reload", ok=False,
                             text="Configuration not reloaded; the servers keep their settings. max_players: 300")
            await wait_for(lambda: _told(fake, ADMIN, "LG Test Server didn't apply it and keeps its settings: "
                                         "Configuration not reloaded; the servers keep their settings. max_players: "
                                         "300."))
            await fake.event("control", action="reload", ok=False, text="again")
            await asyncio.sleep(0.2)
            assert not _told(fake, ADMIN, "settings: again")

    run(scenario())


def test_a_hosts_own_key_stays_and_a_toggled_one_follows_defaults_again(tmp_path: Path, run) -> None:
    """A key the host set in the block is theirs: switched off and on by hand it stays, as they wrote it. One only
    the toggles wrote comes out once switched back, and the server follows [defaults] again when the host changes
    it; a timed toggle after hand ones remembers the block as it was before the first of them."""
    from huragok.core.tomledit import set_array_block_key, set_table_key

    config = tmp_path / "server" / "dedicated.toml"

    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            set_array_block_key(config, ("server",), SERVER, "voice_team", True)  # the host's own
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!toggleteam off")
            await wait_for(lambda: _block(tmp_path).get("voice_team") is False)
            await fake.chat(ADMIN, "Admin", "!toggleteam on")
            await wait_for(lambda: _block(tmp_path).get("voice_team") is True)  # kept: the host's line
            await fake.chat(ADMIN, "Admin", "!toggleglobal off")
            await wait_for(lambda: _block(tmp_path).get("voice_global") is False)
            await fake.chat(ADMIN, "Admin", "!toggleglobal on 5m")  # timed, after a hand toggle
            await wait_for(lambda: _block(tmp_path).get("voice_global") is True)
            _timers_due(sup)
            await wait_for(lambda: _block(tmp_path).get("voice_global") is False)  # back off, as before the 5m
            await fake.chat(ADMIN, "Admin", "!toggleglobal on")
            await wait_for(lambda: "voice_global" not in _block(tmp_path))
            set_table_key(config, "defaults", "voice_global", False)  # the host's later change reaches it
            from huragok.core.tomledit import read_toml
            from huragok.program.settings import current, find

            assert current(read_toml(config), find("voice_global", "server"), SERVER)[0] is False

    run(scenario())


def test_a_channel_toggle_with_a_time_switches_back_once_its_up(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)

            await fake.chat(ADMIN, "Admin", "!toggleglobal 10m")
            await wait_for(lambda: "Admin turned global voice (V) off for 10 minutes." in says(fake))
            assert _block(tmp_path)["voice_global"] is False
            row = sup.storage.query("SELECT * FROM commands_timed_settings")[0]
            assert (row["key"], row["value"], row["before"], row["had_own"], row["by"]) == \
                ("voice_global", 0, 1, 0, "Admin")
            assert 590 < row["until"] - time.time() <= 600
            await fake.chat(ADMIN, "Admin", "!toggleglobal off 2h")  # a new time for the same change
            await wait_for(lambda: "Admin turned global voice (V) off for 2 hours." in says(fake))
            assert sup.storage.query("SELECT before FROM commands_timed_settings")[0]["before"] == 1
            _timers_due(sup)
            await wait_for(lambda: "Global voice (V) is back on after 2 hours." in says(fake))
            assert "voice_global" not in _block(tmp_path)  # the block didn't set it: [defaults] or the default again
            assert not sup.storage.query("SELECT * FROM commands_timed_settings")

            # Either way: on for a while, then off again, as the block had it.
            await fake.chat(ADMIN, "Admin", "!toggleteam off")
            await wait_for(lambda: _block(tmp_path).get("voice_team") is False)
            await fake.chat(ADMIN, "Admin", "!toggleteam 90s")
            await wait_for(lambda: "Admin turned team voice (B) on for 90 seconds." in says(fake))
            _timers_due(sup)
            await wait_for(lambda: "Team voice (B) is off again after 90 seconds." in says(fake))
            assert _block(tmp_path)["voice_team"] is False

            # A toggle without a time ends the timer: it stays.
            await fake.chat(ADMIN, "Admin", "!toggleproxy 1h")
            await wait_for(lambda: _block(tmp_path).get("voice_proximity") is False)
            await fake.chat(ADMIN, "Admin", "!toggleproxy off")  # already off, now for good
            await wait_for(lambda: says(fake)[-1] == "Admin turned proximity voice (N) off.")
            assert not sup.storage.query("SELECT * FROM commands_timed_settings")

            # Someone changing it meanwhile (the window's D) wins: it isn't put back.
            await fake.chat(ADMIN, "Admin", "!toggleproxy on 5m")
            await wait_for(lambda: _block(tmp_path).get("voice_proximity") is True)
            from huragok.core.tomledit import set_server_setting
            set_server_setting(tmp_path / "server" / "dedicated.toml", SERVER, "voice_proximity", False)
            said = len(says(fake))
            _timers_due(sup)
            await wait_for(lambda: not sup.storage.query("SELECT * FROM commands_timed_settings"))
            assert _block(tmp_path)["voice_proximity"] is False and len(says(fake)) == said

    run(scenario())


def test_togglevoice_turns_voice_off_for_ten_minutes_or_back_on(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)

            await fake.chat(ADMIN, "Admin", "!togglevoice")
            await wait_for(lambda: "Admin turned voice chat off for 10 minutes." in says(fake))
            assert _block(tmp_path)["voice"] is False
            row = sup.storage.query("SELECT * FROM commands_timed_settings")[0]
            assert row["key"] == "voice" and 590 < row["until"] - time.time() <= 600
            await fake.chat(ADMIN, "Admin", "!togglevoice 1h")  # no other time
            await wait_for(lambda: _told(fake, ADMIN, "Use: !togglevoice (voice off for 10 minutes, then back on"))
            await fake.chat(ADMIN, "Admin", "!toggleteam")  # a channel while all voice is off: said
            await wait_for(lambda: _told(fake, ADMIN, "Note: voice chat is off on this server altogether (voice = "
                                         "false, back on in 9 minutes"))
            await fake.chat(ADMIN, "Admin", "!togglevoice")  # back on at once, for good
            await wait_for(lambda: "Admin turned voice chat on." in says(fake))
            assert "voice" not in _block(tmp_path)  # back to [defaults]: its own key out again (finding 7)
            assert not sup.storage.query("SELECT * FROM commands_timed_settings")

            await fake.chat(ADMIN, "Admin", "!togglevoice")
            await wait_for(lambda: _block(tmp_path).get("voice") is False)
            _timers_due(sup)  # as if Huragok had been down past the time: put back at its first look
            await wait_for(lambda: "Voice chat is back on after 10 minutes." in says(fake))
            assert "voice" not in _block(tmp_path)  # as the block had it: no key of its own, [defaults] applies

    run(scenario())


def test_a_voice_toggle_says_what_else_keeps_players_quiet(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            config = tmp_path / "server" / "dedicated.toml"
            config.write_text("auto_reload = false\n" + config.read_text(encoding="utf-8")
                              .replace(f'name = "{SERVER}"', f'name = "{SERVER}"\nvoice = false'), encoding="utf-8")
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!toggleteam")
            await wait_for(lambda: _told(fake, ADMIN, "!reload or the next restart"))
            assert "Admin turned team voice (B) off." in says(fake)
            assert _told(fake, ADMIN, "Note: voice chat is off on this server altogether (voice = false), so nobody")

            await fake.chat(ADMIN, "Admin", "!toggleglobal")
            await fake.chat(ADMIN, "Admin", "!toggleproxy")
            await wait_for(lambda: _block(tmp_path).get("voice_proximity") is False)
            await fake.chat(ADMIN, "Admin", "!togglevoice")  # voice on again, but no channel is
            await wait_for(lambda: _told(fake, ADMIN, "Note: V, B and N are all off, so nobody hears anyone yet"))
            assert "Admin turned voice chat on." in says(fake)

            # A server whose block isn't in its file any more (renamed by hand): never a new block without a port.
            config.write_text(config.read_text(encoding="utf-8").replace(f'name = "{SERVER}"', 'name = "Renamed"'),
                              encoding="utf-8")
            before = config.read_text(encoding="utf-8")
            await fake.chat(ADMIN, "Admin", "!toggleglobal")
            await wait_for(lambda: _told(fake, ADMIN, "has no [[server]] block named LG Test Server"))
            assert config.read_text(encoding="utf-8") == before

    run(scenario())


# Restarting the server from the game ---------------------------------------------------------------------

class RestartRecorder:
    """The lifecycle as !restartserver uses it: what it was asked, as the window's R would ask it."""

    def __init__(self, busy: set[str] | None = None) -> None:
        self.busy = busy or set()
        self.asked: list[dict[str, Any]] = []

    async def restart(self, server: str, **options: Any) -> None:
        self.asked.append({"server": server, **options})


def test_an_admin_restarts_the_server_with_the_usual_countdown(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ALICE, "Alice", "!restartserver")
            await wait_for(lambda: _told(fake, ALICE, "!restartserver is for admins and up."))

            # These tests' servers are attach: Huragok didn't start them, so it can't restart them, and says so.
            await fake.chat(ADMIN, "Admin", "!restartserver")
            await wait_for(lambda: _told(fake, ADMIN, "Couldn't: backend = attach can't restart servers"))

            lifecycle = sup.superintendent.lifecycle = RestartRecorder()  # type: ignore[assignment]
            await fake.chat(ADMIN, "Admin", "!restartserver")
            await wait_for(lambda: _told(fake, ADMIN, "Restarting: everyone on gets the usual countdown first."))
            # Now, with no countdown of its own: the server's warn_seconds, as a person's restart (not the budget's).
            assert lifecycle.asked == [{"server": SERVER, "when": "now", "reason": "requested by Admin", "by": "Admin",
                                        "manual": True}]
            await fake.chat(ADMIN, "Admin", "!restartserver the server is lagging")
            await wait_for(lambda: len(lifecycle.asked) == 2)
            assert lifecycle.asked[1]["reason"] == "the server is lagging (requested by Admin)"

            lifecycle.busy.add(SERVER)  # a daily restart's countdown, say
            await fake.chat(OWNER, "Owner", "!restartserver")
            await wait_for(lambda: _told(fake, OWNER, "A countdown or a stop was already under way: it now ends in "
                                                      "a restart."))

    run(scenario())
