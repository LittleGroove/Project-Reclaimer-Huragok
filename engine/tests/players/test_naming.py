"""Who a player ID is, written the one way everywhere (players/naming.py, core/tags.py), and the #numbers and @numbers
that pick them (players/targets.py): the player identity audit, 21 November 2026."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import ALICE, BRAVO, MOD, free_port, staff, write_server_folder, write_settings

from huragok.core.events import EventBus
from huragok.core.settings import load
from huragok.core.storage import PICKED, picked_as
from huragok.players.naming import Names
from huragok.players.roles import Roles
from huragok.players.router import CommandCall, CommandRouter
from huragok.players.targets import Candidate, Picks, find
from huragok.rcon import Reply
from huragok.servers.state import Player, ServerState

HERE, THERE = "Alpha", "Beta"
GONE = "7" * 64


class _Client:
    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args))
        return Reply(True, "ok", None, {})


def _servers() -> dict[str, ServerState]:
    here, there = ServerState(HERE), ServerState(THERE)
    here.players[4] = Player(4, "Bravo", BRAVO)
    here.players[2] = Player(2, "Mod", MOD)
    there.players[7] = Player(7, "Alice", ALICE)
    return {HERE: here, THERE: there}


def test_names_finds_the_server_and_the_name_now() -> None:
    names = Names(_servers(), None)
    assert names.on(ALICE)[0] == THERE and names.on(ALICE)[1].number == 7
    assert names.on("0" * 64) == ("", None)
    assert names.name(BRAVO, "Old Bravo") == "Bravo"  # on now: their name there beats what a record kept
    assert names.name("0" * 64, "Kept") == "Kept"
    assert names.tag(ALICE) == f"Alice {ALICE[:8]} [{THERE}]"
    assert names.public(ALICE) == f"Alice {ALICE[:8]} (#7) [{THERE}]"  # everyone there can use the #number
    assert names.describe(BRAVO) == f"Bravo {BRAVO} [{HERE}]"  # the log: the whole ID


def test_a_server_whose_console_is_down_counts_last() -> None:
    servers = _servers()
    servers[THERE].connected = False
    servers[HERE].players[9] = Player(9, "Alice", ALICE)  # left Beta for Alpha while Beta's console was down
    assert Names(servers, None).on(ALICE)[0] == HERE


def test_at_numbers_stick() -> None:
    """Someone keeps their @number in every list that names them, and a new list never renumbers anyone (E2)."""
    picks = Picks()
    a, b, c = (Candidate(pid, name) for pid, name in ((ALICE, "Alice"), (BRAVO, "Bravo"), (GONE, "Gone")))
    assert picks.offer("caller", [a, b]) == [(1, a), (2, b)]
    assert picks.offer("caller", [c, a]) == [(3, c), (1, a)]  # !mutes after !bans: Alice is still @1
    got = picks.get("caller", 2)
    assert isinstance(got, Candidate) and got.id == BRAVO
    assert picks.number_for("caller", b) == 2
    assert picks.number_for("someone else", b) == 1  # each caller's own


def test_how_a_player_was_picked_is_kept_for_the_ledger(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server", commands=staff(cooldown=0)))
    servers = _servers()
    client = _Client()
    router = CommandRouter(settings, EventBus(), servers, {HERE: client, THERE: client}, Roles(settings))  # type: ignore[dict-item]

    async def tell(_server: str, _player: str, text: str) -> None:
        told.append(text)

    told: list[str] = []
    call = CommandCall(HERE, servers[HERE].players[2], "moderator", "whois", [], "", "all", servers[HERE], tell,
                       states=servers, picks=router.picks)

    async def scenario() -> None:
        player = await find(call, "#4")
        assert player is not None and call.picked[BRAVO] == "#4"
        assert call.tag(player) == f"Bravo {BRAVO[:8]} (#4) [{HERE}]"
        # Someone on another server gets the caller's @number, so the reply names them with something to type.
        assert call.tag(servers[THERE].players[7]) == f"Alice {ALICE[:8]} (@1) [{THERE}]"
        again = await find(call, "@1", scope="any")  # a command that reaches every server (a kick)
        assert again is not None and again.id == ALICE and call.picked[ALICE] == "@1"
        token = PICKED.set(call.picked)
        try:
            assert picked_as(BRAVO) == "#4" and picked_as(ALICE) == "@1" and picked_as(GONE) == ""
        finally:
            PICKED.reset(token)

    asyncio.run(scenario())


def test_could_be_is_one_player_a_line(tmp_path: Path) -> None:
    servers = _servers()
    servers[HERE].players[5] = Player(5, "Bravo", "b" * 64)
    told: list[str] = []

    async def tell(_server: str, _player: str, text: str) -> None:
        told.append(text)

    call = CommandCall(HERE, servers[HERE].players[2], "moderator", "kick", [], "", "all", servers[HERE], tell,
                       states=servers, picks=Picks())
    assert asyncio.run(find(call, "bravo")) is None
    assert told[0] == "'bravo' could be 2 players. Use the #number:"
    assert told[1].startswith(f"Bravo {BRAVO[:8]} (#4) [{HERE}]") and told[2].startswith(f"Bravo bbbbbbbb (#5) [{HERE}]")
