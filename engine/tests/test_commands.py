from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, free_port, staff, wait_for, write_server_folder, write_settings
from test_supervisor import everyone_joins, running, tells_to

from huragok.chat import CommandCall, CommandRouter, split_words, wrap
from huragok.config import load
from huragok.events import Event, EventBus
from huragok.part import CommandSpec
from huragok.permissions import Roles
from huragok.rcon import Reply, RconTimeout
from huragok.state import Player, ServerState
from huragok.storage import Storage

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
    from huragok.state import StateTracker
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
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            sup.clients[SERVER].command_timeout = 0.2
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
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
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
            await wait_for(lambda: any(t.startswith("Bravo (dddddddd): 1 visits since") for t in tells_to(fake, MOD)))

    run(scenario())


def test_an_action_whose_reply_times_out_is_recorded_once_and_not_sent_again(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            sup.clients[SERVER].command_timeout = 0.3
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
            await wait_for(lambda: len(sup.states[SERVER].players) == 5 and sup.states[SERVER].admins)
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
            await wait_for(lambda: len(sup.states[SERVER].players) == 6)
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


# Who's who, changed in game ------------------------------------------------------------------------------

def test_one_role_each_moved_a_step_at_a_time(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            path = sup.settings.permissions.path
            bravo = sup.states[SERVER].players[4]

            def role() -> str:
                return sup.roles.role_of(sup.states[SERVER], bravo)

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
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
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
