"""The host's calls of 3 November 2026 on sides (plans/bug-audit-0.6.1.md, "The host's answers")."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, staff
from test_audit_0_6_1_sides import CHARLIE, DELTA, MEMBER, fake, moved  # noqa: F401 (fake: the fixture)
from test_links import told, up
from test_seasons_and_sides import full_file, round_on
from testkit.fake_discord import FakeDiscord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import sides
from huragok.discordbot.config import set_switch


async def lobby_on(huragok: Any, rcon: Any, *players: tuple[int, str, str, str]) -> None:
    for number, name, player_id, _ in players:
        await rcon.join(number, name, player_id)
    await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == len(players), timeout=10)
    for row, (_, _, _, team) in zip(rcon.players, players):
        row["team"] = team
    await rcon.event("phase", phase="no_game", map="Guardian", mode="Team Slayer")
    await wait_for(lambda: huragok.superintendent.states[SERVER].phase == "no_game", timeout=10)


def test_moved_in_the_lobby_too_never_emptying_a_team(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """7: moved in the lobby too, the teams kept even; and never a team's last player there either: teams lock at
    the countdown, and a game could start with a team empty (second audit, 8.8)."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await lobby_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Charlie", CHARLIE, "Red"))
            sides.choose(part, ALICE, "red")
            assert await sides.place(part, SERVER) == 0  # Blue's last
            await rcon.join(5, "Delta", DELTA)
            rcon.players[-1]["team"] = "Blue"
            await wait_for(lambda: moved(rcon) == [[ALICE, "red"]], timeout=15)  # in the lobby, once Blue keeps one

    run(scenario())


def test_no_moves_in_a_role_mode_or_with_a_third_team(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """10: Infection and Juggernaut are what you are, not a side; a third team isn't Red against Blue."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Charlie", CHARLIE, "Blue"), (5, "Delta", DELTA, "Red"))
            sides.choose(part, ALICE, "red")
            part.ctx.servers[SERVER].mode = "Infection"
            assert await sides.place(part, SERVER) == 0
            part.ctx.servers[SERVER].mode = "Slayer"
            rcon.players[2]["team"] = "Green"
            assert await sides.place(part, SERVER) == 0 and moved(rcon) == []

    run(scenario())


def test_a_switch_of_their_own_is_left_alone_unless_their_side_is_forced(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """11 and 8: switched themselves, not moved again that game; held by force_for_season, moved back, and
    !togglesides can't switch it off."""
    async def scenario() -> None:
        path = full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Red"), (4, "Charlie", CHARLIE, "Red"), (5, "Delta", DELTA, "Blue"),
                           (6, "Bravo", BRAVO, "Blue"))
            sides.choose(part, ALICE, "red")
            assert await sides.place(part, SERVER) == 0  # on Red already: seen there
            rcon.players[0]["team"] = "Blue"  # Alpha switches themselves
            assert await sides.place(part, SERVER) == 0 and moved(rcon) == []
            for key in ("lock_for_season", "force_for_season"):
                set_switch(path, ("sides",), key, True)
            await part.reload(force=True)
            part.ctx.db.execute("INSERT INTO stats_seasons (name, started, started_by) VALUES ('Winter', 0, 'Faculty')")
            assert sides.held(part, ALICE) == "red"
            assert await sides.place(part, SERVER) == 1 and moved(rcon) == [[ALICE, "red"]]  # forced: moved back
            await rcon.chat(ALICE, "Alpha", "!togglesides")
            await wait_for(lambda: told(rcon, "sides are forced here"), timeout=10)
            set_switch(path, ("sides",), "lock_for_season", False)
            await part.reload(force=True)
            assert sides.held(part, ALICE) == ""  # without lock_for_season, force_for_season does nothing
            await asyncio.sleep(0)

    run(scenario())


def test_a_forced_player_an_admin_moved_goes_back_only_as_the_teams_allow() -> None:
    """The host's answer to 21: moved back one at a time, only within most_apart after the move. 8 Red to 5 Blue, an
    admin puts Alice on Blue (7 to 6): back on Red would be 8 to 5, so she stays; at 7 to 7 she goes."""
    teams = {**{f"r{n}": "red" for n in range(7)}, **{f"b{n}": "blue" for n in range(5)}, ALICE: "blue"}
    assert sides.moves(teams, [(ALICE, "red")], 2) == []
    teams["b5"] = "blue"
    assert sides.moves(teams, [(ALICE, "red")], 2) == [(ALICE, "red")]


def test_told_when_moved_and_never_when_not(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host's answer to 22: a move that didn't happen says nothing (no chat filled with tries); one that did is told."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Charlie", CHARLIE, "Blue"), (5, "Delta", DELTA, "Red"))
            sides.choose(part, ALICE, "red")
            rcon.replies["team"] = lambda message: {"ok": False, "text": "Players are joining or leaving; try again shortly."}
            assert await sides.place(part, SERVER) == 0 and not told(rcon, "the side you chose")
            del rcon.replies["team"]
            assert await sides.place(part, SERVER) == 1
            assert told(rcon, "You're on Red, the side you chose")

    run(scenario())
