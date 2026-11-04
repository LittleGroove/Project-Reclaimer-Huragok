"""The audit before 0.6.1 (plans/bug-audit-0.6.1.md): sides (area 4)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, staff
from test_links import up
from test_seasons_and_sides import GAMES, STATS, full_file, round_on, sides_of
from testkit.fake_discord import FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import sides
from huragok.servers.state import Player

CHARLIE, DELTA, ECHO, FOX = "e" * 64, "f" * 64, "1" * 64, "2" * 64
MEMBER = f'[member]\nplayers = ["{ALICE}", "{BRAVO}"]\n'


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.channels += [discord._channel(GAMES, "games"), discord._channel(STATS, "stats")]
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def moved(rcon: Any) -> list[list[str]]:
    return [command["args"] for command in rcon.named("team")]


def test_a_swap_stops_when_its_first_half_doesnt_happen(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """4.1: a move's answer only says it's on its way. A swap whose first half was refused (the player had left) still
    sent the second, Blue's only player, and the game could end."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Red"), (4, "Bravo", BRAVO, "Blue"), (5, "Delta", DELTA, "Red"))
            rcon.replies["team"] = lambda message: {"ok": False, "text": "No player is named that; players lists them."}
            sides.choose(part, ALICE, "blue")
            sides.choose(part, BRAVO, "red")
            assert await sides.place(part, SERVER) == 0
            assert moved(rcon) == [[ALICE, "blue"]]  # and nothing after it

    run(scenario())


def test_a_swap_is_made_a_move_at_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Red"), (4, "Bravo", BRAVO, "Blue"), (5, "Delta", DELTA, "Red"))
            sides.choose(part, ALICE, "blue")
            sides.choose(part, BRAVO, "red")
            assert await sides.place(part, SERVER) == 2
            assert moved(rcon) == [[ALICE, "blue"], [BRAVO, "red"]]

    run(scenario())


def test_joins_together_move_someone_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """4.2: each join started its own placement, and both sent the same move."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Charlie", CHARLIE, "Blue"), (5, "Delta", DELTA, "Red"))
            sides.choose(part, ALICE, "red")
            await rcon.join(6, "Echo", ECHO)
            await rcon.join(7, "Fox", FOX)
            await wait_for(lambda: moved(rcon), timeout=10)
            await asyncio.sleep(1.0)
            assert moved(rcon) == [[ALICE, "red"]]

    run(scenario())


def test_respawn_and_sides_never_move_players_at_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """4.3: !respawn took a sides move's answer for its own, said it failed, and left the player on the other team."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Charlie", CHARLIE, "Blue"), (5, "Echo", ECHO, "Red"))
            state = part.ctx.servers[SERVER]
            state.team_moving_until = time.monotonic() + 30  # a sides move under way
            await rcon.chat(ECHO, "Echo", "!respawn")
            await wait_for(lambda: any("being moved to their side" in str(c) for c in rcon.named("tell")), timeout=10)
            sides.choose(part, ALICE, "red")
            assert await sides.place(part, SERVER) == 0  # and the other way round: it waits
            assert moved(rcon) == []
            state.team_moving_until = 0.0

    run(scenario())


def test_a_leave_lets_a_waiting_move_happen_and_the_first_to_join_goes_first(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """4.5: only a join tried a waiting move again, though a leave is what most often makes one fit. 4.6: "whoever
    joined first" followed the server's slot order."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Charlie", CHARLIE, "Blue"),
                           (5, "Delta", DELTA, "Red"), (6, "Echo", ECHO, "Red"), (7, "Fox", FOX, "Red"))
            sides.choose(part, ALICE, "red")
            assert await sides.place(part, SERVER) == 0  # 4 to 1 would be too far apart
            await rcon.leave(7)
            await wait_for(lambda: moved(rcon) == [[ALICE, "red"]], timeout=10)
            players = part.ctx.servers[SERVER].players
            for number, joined in ((3, 200.0), (4, 100.0)):
                players[number].joined = joined
            sides.choose(part, BRAVO, "red")
            players[4] = Player(4, "Bravo", BRAVO, joined=100.0)
            assert [player_id for player_id, _ in sides._wanted(part, SERVER)] == [BRAVO, ALICE]

    run(scenario())


def test_a_side_is_a_players_never_the_discord_consoles(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """4.7: !side typed in the Discord console kept a side for an ID that isn't a player's."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, _, _):
            part = await up(huragok)
            heard: list[str] = []

            async def tell(_server: str, _player: str, said: str) -> None:
                heard.append(said)

            await part.ctx.run_line("side", "red", server=SERVER, player=Player(0, "Mod", "discord:123456"), role="moderator",
                                    tell=tell)  # typed in Discord: run_line says so
            assert sides_of(part) == {}

    run(scenario())
