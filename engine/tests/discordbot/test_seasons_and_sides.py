"""Seasons, Red against Blue, sides, the Carnage Report and the boards kept in a channel (0.6.1, the host's calls of
3 November 2026: only faculty start or end a season, one with no length runs until faculty end it; a linked
player can always be put on the team they chose; no "That's me" button, as !link is what changes a name)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest

import asyncio

from conftest import ALICE, BRAVO, FACULTY, staff
from test_bot_running import OTHER, answered
from test_links import told, up
from test_standings import ENDED, games, player, report, stats_file
from test_tickets import buttons_of, posts_in
from testkit.fake_discord import FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.config import set_channel, set_switch
from huragok.discordbot.sides import moves
from huragok.servers.leaderboards import Board, board, current_season, red_blue
from huragok.servers.stats import read_game, store

GAMES, STATS = "4301", "4302"
CHARLIE, DELTA = "e" * 64, "f" * 64


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.channels += [discord._channel(GAMES, "games"), discord._channel(STATS, "stats")]
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def full_file(root: Path) -> Path:
    path = stats_file(root)
    set_channel(path, "games", GAMES, "games")
    set_channel(path, "stats", STATS, "stats")
    for table in (("feed", "games"), ("leaderboards",), ("sides",)):
        set_switch(path, table, "enabled", True)
    set_switch(path, ("feed", "games"), "min_players", 2)
    return path


# Seasons ---------------------------------------------------------------------------------------------------------


def test_faculty_start_a_season_and_it_runs_until_ended(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, events):
            db = huragok.storage
            await rcon.join(1, "Faculty", FACULTY)
            await rcon.join(3, "Alpha", ALICE)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(ALICE, "Alpha", "!newseason Winter")  # not faculty
            await rcon.chat(FACULTY, "Faculty", "!season")
            await wait_for(lambda: told(rcon, "No season running"), timeout=10)
            assert current_season(db) is None
            await rcon.chat(FACULTY, "Faculty", "!newseason Winter")
            await wait_for(lambda: told(rcon, "runs until faculty type"), timeout=10)
            assert current_season(db)["ends"] is None  # no end unless one's given
            db.execute("UPDATE stats_seasons SET started = started - 10")  # whole seconds, as a report gives them
            season = current_season(db)
            assert season is not None and season["name"] == "Winter"
            store(db, SERVER, read_game(report("in-season", ended=int(time.time()) - 5)), time.time())
            assert red_blue(db, float(season["started"])) == {"red": 1, "blue": 0, "ties": 0, "games": 1}
            games(db)  # all of them before it began
            assert board(db, Board("kills", "season"))[0].value == 12  # only what's in the season
            await rcon.chat(ALICE, "Alpha", "!season")
            await wait_for(lambda: told(rcon, "Red 1, Blue 0"), timeout=10)
            await rcon.chat(FACULTY, "Faculty", "!endseason")
            await wait_for(lambda: told(rcon, "(Winter) is over"), timeout=10)
            assert current_season(db) is None
            ended = [event for event in events if event.name == "stats.season"]
            assert [event.get("action") for event in ended] == ["started", "ended"] and ended[1].get("red") == 1

    run(scenario())


def test_a_season_given_a_length_ends_by_itself(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await rcon.join(1, "Faculty", FACULTY)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await rcon.chat(FACULTY, "Faculty", "!newseason Spring 30d")
            await wait_for(lambda: told(rcon, "for 30 days"), timeout=10)
            season = current_season(huragok.storage)
            assert season is not None and abs(float(season["ends"]) - float(season["started"]) - 30 * 86400) < 5
            huragok.storage.execute("UPDATE stats_seasons SET ends = ?", (time.time() - 1,))
            await huragok.runners["stats"].part.season_ends()
            assert current_season(huragok.storage) is None
            assert huragok.storage.query("SELECT ended_by FROM stats_seasons")[0]["ended_by"] == "its time ran out"

    run(scenario())


# Sides -----------------------------------------------------------------------------------------------------------


def test_moves_never_empty_a_team_and_keep_them_within_two() -> None:
    """The host's calls, 3 November 2026: within most_apart (2) after a move, whoever joined first first; two who'd
    each rather be on the other's team swap, however even the teams are, but never so a team is empty for a moment."""
    teams = {ALICE: "blue", BRAVO: "red", CHARLIE: "blue", DELTA: "red"}
    assert moves(teams, [(ALICE, "red"), (BRAVO, "blue")], 2) == [(ALICE, "red"), (BRAVO, "blue")]  # a swap
    assert moves({ALICE: "blue", BRAVO: "red"}, [(ALICE, "red"), (BRAVO, "blue")], 2) == []  # one each: no swap
    assert moves(teams, [(ALICE, "red")], 2) == [(ALICE, "red")]  # 3 against 1: within 2
    assert moves(teams, [(ALICE, "red")], 1) == []
    assert moves({ALICE: "blue", BRAVO: "red", CHARLIE: "red"}, [(ALICE, "red")], 9) == []  # Blue's last player
    five = {ALICE: "blue", CHARLIE: "blue", DELTA: "blue", BRAVO: "red"}
    assert moves(five, [(CHARLIE, "red"), (ALICE, "red")], 1) == [(CHARLIE, "red")]  # whoever joined first
    assert moves(five, [(CHARLIE, "red"), (ALICE, "red")], 2) == [(CHARLIE, "red"), (ALICE, "red")]
    assert moves(teams, [(BRAVO, "red")], 2) == []  # already there


MEMBER = f'[member]\nplayers = ["{ALICE}"]\n'


async def round_on(huragok: Any, rcon: Any, *players: tuple[int, str, str, str]) -> None:
    for number, name, player_id, team in players:
        await rcon.join(number, name, player_id)
    await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == len(players), timeout=10)
    for row, (_, _, _, team) in zip(rcon.players, players):
        row["team"] = team
    await rcon.event("phase", phase="playing", map="Guardian", mode="Team Slayer")
    await wait_for(lambda: huragok.superintendent.states[SERVER].phase == "playing", timeout=10)


def test_a_member_is_put_on_their_side(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Bravo", BRAVO, "Blue"), (5, "Charlie", CHARLIE, "Red"))
            await rcon.chat(BRAVO, "Bravo", "!side red")  # a player: members and up choose
            await rcon.chat(ALICE, "Alpha", "!side red")
            await wait_for(lambda: told(rcon, "You're Red from now on"), timeout=10)
            await wait_for(lambda: rcon.named("team"), timeout=10)
            assert ALICE in str(rcon.named("team")[-1]) and "red" in str(rcon.named("team")[-1])
            assert sides_of(part) == {ALICE: "red"}  # no link needed in game
            await rcon.chat(ALICE, "Alpha", "!togglesides")
            await wait_for(lambda: told(rcon, "wherever the game puts you"), timeout=10)
            assert part.ctx.db.query("SELECT forced FROM discord_sides")[0]["forced"] == 0

    run(scenario())


def test_a_move_that_cant_be_made_waits_for_someone_to_join(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await round_on(huragok, rcon, (3, "Alpha", ALICE, "Blue"), (4, "Bravo", BRAVO, "Red"))
            await rcon.chat(ALICE, "Alpha", "!side red")
            await wait_for(lambda: told(rcon, "You're Red from now on"), timeout=10)
            await asyncio.sleep(0.5)
            assert not rcon.named("team")  # Blue's last player: the game would end
            await rcon.join(5, "Charlie", CHARLIE)
            rcon.players[-1]["team"] = "Blue"
            await wait_for(lambda: rcon.named("team"), timeout=10)  # now Blue keeps one
            assert ALICE in str(rcon.named("team")[-1])
            assert part is not None

    run(scenario())


def test_a_side_can_be_kept_for_the_season_and_cleared_at_its_end(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = full_file(tmp_path)
        set_switch(path, ("sides",), "lock_for_season", True)
        set_switch(path, ("sides",), "reset_at_season_end", True)
        async with running(tmp_path, commands=staff(MEMBER)) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.join(1, "Faculty", FACULTY)
            await rcon.join(3, "Alpha", ALICE)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(ALICE, "Alpha", "!side red")  # no season yet: free to switch
            await rcon.chat(ALICE, "Alpha", "!side blue")
            await wait_for(lambda: sides_of(part) == {ALICE: "blue"}, timeout=10)
            await rcon.chat(FACULTY, "Faculty", "!newseason Winter")
            await wait_for(lambda: told(rcon, "has begun"), timeout=10)
            await rcon.chat(ALICE, "Alpha", "!side red")
            await wait_for(lambda: sides_of(part) == {ALICE: "red"}, timeout=10)  # chosen in the season
            await rcon.chat(ALICE, "Alpha", "!side blue")
            await wait_for(lambda: told(rcon, "until this season ends"), timeout=10)
            assert sides_of(part) == {ALICE: "red"}
            await rcon.chat(FACULTY, "Faculty", "!endseason")
            await wait_for(lambda: sides_of(part) == {}, timeout=10)  # everyone chooses again

    run(scenario())


def sides_of(part: Any) -> dict[str, str]:
    return {str(row["player_id"]): str(row["side"]) for row in part.ctx.db.query("SELECT player_id, side FROM discord_sides")}


# In Discord ------------------------------------------------------------------------------------------------------


def test_a_game_gets_its_carnage_report_and_its_rare_medals(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            stats = huragok.runners["stats"].part
            stats.take(SERVER, "/v1/matches", {"report": report("big", players=[
                player(ALICE, "Alpha", 0, 1, 30, medals={"killtacular": 1, "double_kill": 4}), player(BRAVO, "Bravo", 1, 2, 3)])}, 202, None)
            await wait_for(lambda: posts_in(fake, GAMES), timeout=15)
            await asyncio.sleep(1.0)
            texts = [str(post["embeds"][0]) for post in posts_in(fake, GAMES)]
            assert len(texts) == 1  # one post a game: its rare medals are in it (the host's call, 3 November 2026)
            assert "Red 50 – 41 Blue" in texts[0] and "Alpha" in texts[0] and "Killtacular" in texts[0]
            assert "Double kill" not in texts[0]  # not a rare one
            assert part is not None

    run(scenario())


def test_the_boards_are_kept_in_their_channel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = full_file(tmp_path)
        set_switch(path, ("leaderboards",), "boards", ["kills all", "red-blue"])
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            part.boards_kept = 0.0
            await part.status_tick()
            await wait_for(lambda: len(posts_in(fake, STATS)) == 2, timeout=15)
            titles = [post["embeds"][0]["title"] for post in posts_in(fake, STATS)]
            assert titles == ["Kills", "Red against Blue"]
            assert "**1.** Alpha · 72" in posts_in(fake, STATS)[0]["embeds"][0]["description"]
            assert not any("components" in post and post["components"] for post in posts_in(fake, STATS))  # no "That's me"
            store(huragok.storage, SERVER, read_game(report("more", ended=ENDED + 200000)), ENDED + 200000)
            part.boards_kept = 0.0
            await part.status_tick()
            await wait_for(lambda: any("84" in str(edit) for edit in fake.edits), timeout=15)  # edited, not posted again
            assert len(posts_in(fake, STATS)) == 2
            set_switch(path, ("leaderboards",), "boards", ["red-blue"])
            await part.reload(force=True)
            part.boards_kept = 0.0
            await part.status_tick()
            await wait_for(lambda: any(method == "DELETE" for method, _ in fake.requests), timeout=15)

    run(scenario())


def test_the_report_byte_says_the_weapon() -> None:
    from huragok.servers.weapons import how

    assert how(11).words() == "Battle Rifle" and how(75).words() == "Battle Rifle, headshot"  # 11 + 64
    assert how(156).words() == "Plasma Grenade, stuck" and how(131).words() == "melee, from behind"  # + 128
    assert how(232).words() == "Ghost, splatter" and how(61).words() == "something" and how(None).words() == "something"


def test_a_carnage_report_has_its_games_kills_to_see(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """No kill feed in a channel (the host's call, 3 November 2026: there's no way without Discord's limits): a game's
    kills are a button on its Carnage report, for whoever presses it."""
    async def scenario() -> None:
        full_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alpha", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            for row, engine in zip(rcon.players, (100, 200)):
                row["engine_id"] = engine
            await rcon.event("kill", killer="100", victim="200", report=75)
            await rcon.event("kill", killer="200", victim="100", report=156)
            await rcon.event("kill", killer=None, victim="200", report=0)
            stats = huragok.runners["stats"].part
            await wait_for(lambda: len(stats.kills.get(SERVER, [])) == 3 and len(stats.engines.get(SERVER, {})) == 2, timeout=10)
            stats.take(SERVER, "/v1/matches", {"report": report("kills", ended=int(time.time()))}, 202, None)
            post = await wait_for(lambda: next((post for post in posts_in(fake, GAMES) if "Carnage report" in str(post["embeds"][0])), None), timeout=15)
            _, token = fake.press(buttons_of(post)["Kill feed"], post, user=OTHER)
            text = str((await answered(fake, token)).get("content"))
            assert "**Alpha** ▸ Bravo · Battle Rifle, headshot" in text and "**Bravo** ▸ Alpha · Plasma Grenade, stuck" in text
            assert "Bravo died · the Guardians" in text

    run(scenario())


def test_stats_can_answer_for_everyone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = stats_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            _, token = fake.command("leaderboard", user=OTHER, stat="kills")
            assert (await answered(fake, token)).get("flags", 0) & 64  # private, the default
            set_switch(path, ("stats",), "private", False)
            await part.reload(force=True)
            _, token = fake.command("leaderboard", user=OTHER, stat="kills")
            assert not (await answered(fake, token)).get("flags", 0) & 64

    run(scenario())
