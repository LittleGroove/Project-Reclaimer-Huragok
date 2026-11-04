"""The audit before 0.6.1 (plans/bug-audit-0.6.1.md): what the bot shows for stats (area 5)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE
from test_links import up
from test_seasons_and_sides import GAMES, STATS, full_file
from test_standings import CHARLIE, games, stats_file
from testkit.fake_discord import FakeDiscord, use_fake_discord
from testkit.running import running, wait_for

from huragok.discordbot import standings
from huragok.discordbot.config import set_switch
from huragok.discordbot.feeds import _no_buttons
from huragok.servers.leaderboards import Board, card, season_named


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.channels += [discord._channel(GAMES, "games"), discord._channel(STATS, "stats")]
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def test_names_are_cleaned_once_and_a_hidden_one_never_shows(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """5.4: linked names were escaped twice ("xX\\_Sn1per\\_Xx" with the backslashes showing), and a name Vergil
    wouldn't let in a game showed beside a Discord name ("Cool_Guy (Bannedname)")."""
    async def scenario() -> None:
        stats_file(tmp_path)
        (tmp_path / "automod.txt").write_text("", encoding="utf-8")
        (tmp_path / "names.txt").write_text("Bannedname\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            part.links.use_code(part.links.new_code(CHARLIE, "Bannedname"), "5555", "Cool_Guy")
            part.links.show("5555", "both")
            part.links.use_code(part.links.new_code(ALICE, "Alpha"), "6666", "xX_Sn1per_Xx")
            text = (await standings.board_of(part, Board("kills")))["description"]
            assert "xX\\_Sn1per\\_Xx" in text and "\\\\" not in text
            assert "Bannedname" not in text and "Cool\\_Guy" in text
            assert "Bannedname" not in standings.record_of(part, card(huragok.storage, CHARLIE), mine=True)["title"]

    run(scenario())


def test_a_board_fits_discord(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """5.5, 5.9: 25 long names made a board's text too long, and a long map typed in a title too long: refused."""
    async def scenario() -> None:
        stats_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            for number in range(25):
                player_id = f"{number:02d}" + "e" * 62
                part.ctx.db.execute("INSERT OR IGNORE INTO stats_people (player_id) VALUES (?)", (player_id,))
            form = await standings.board_of(part, Board("kills", map="x" * 300))
            assert len(form["title"]) <= 256 and len(form["description"]) <= 4096

    run(scenario())


def test_kept_boards_stay_while_discord_isnt_read(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """5.6: a bot started again (or a role feature switched on) took every kept board down until the next update."""
    async def scenario() -> None:
        path = full_file(tmp_path)
        set_switch(path, ("leaderboards",), "boards", ["kills all", "red-blue"])
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            part.boards_kept = 0.0
            await part.status_tick()
            await wait_for(lambda: len(part.ctx.db.query("SELECT 1 FROM discord_posts WHERE key LIKE 'leaderboard:%' AND message != ''")) == 2, timeout=15)
            part.facts = None  # just after a sign-in, before Discord's been read again
            await standings.keep_boards(part, time.time() + 3600)
            await asyncio.sleep(0.5)
            assert not any(method == "DELETE" for method, _ in fake.requests)

    run(scenario())


def test_seasons_named_once_and_buttons_only_from_the_bot() -> None:
    """5.10: "Season 1 (Season 1) has begun". 5.7: the Kill feed button went through webhooks that aren't the bot's."""
    assert season_named(1, "Season 1") == "Season 1" and season_named(2, "Winter") == "Season 2 (Winter)"
    assert "components" not in _no_buttons({"content": "x", "components": [{"type": 1}]})
    assert standings.board_wanted("kills seasonal") is None and standings.board_wanted("kills season") == Board("kills", "season")


def test_a_change_to_the_boards_shows_at_the_next_tick(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host, 4 November 2026: with update_minutes = 60, a new list and order changed nothing for up to an hour.
    What the boards are of is kept at the next tick; only their numbers wait for update_minutes."""
    from test_seasons_and_sides import posts_in

    async def scenario() -> None:
        path = full_file(tmp_path)
        set_switch(path, ("leaderboards",), "boards", ["kills all"])
        set_switch(path, ("leaderboards",), "update_minutes", 60)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            await part.status_tick()
            await wait_for(lambda: len(posts_in(fake, STATS)) == 1, timeout=15)
            set_switch(path, ("leaderboards",), "boards", ["kd all", "kills all"])  # a board added, and first
            await part.reload(force=True)
            await part.status_tick()  # no hour waited
            await wait_for(lambda: any("K/D" in str(post.get("embeds")) for post in posts_in(fake, STATS)), timeout=15)

    run(scenario())


def test_red_against_blue_over_a_period(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host, 4 November 2026: /leaderboard Red against Blue with a period counts over it too (it was ignored)."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            form = await standings.red_blue_of(part, None, "week")
            assert form["description"].startswith("**The last 7 days: Red ") and "All time: Red" in form["description"]
            assert not (await standings.red_blue_of(part))["description"].startswith("**The last")

    run(scenario())
