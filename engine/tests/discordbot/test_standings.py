"""The stats in Discord and in game (0.6.1): /stats, /leaderboard and !stats, from every reported game."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO
from test_bot_running import OTHER, answered
from test_links import link_file, told, up
from testkit.fake_discord import GUILD, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.config import set_switch
from huragok.servers.leaderboards import Board, board, card
from huragok.servers.stats import read_game, store

CHARLIE = "e" * 64
ENDED = 1793500000  # 2026-11-01, UTC


def player(player_id: str, name: str, team: int, place: int, kills: int = 0, **more: Any) -> dict[str, Any]:
    return {"id": player_id, "name": name, "team": team, "place": place, "score": kills, "kills": kills, "deaths": 1,
            "assists": 0, "betrayals": 0, "suicides": 0, "headshots": 0, "best_streak": kills, "time_played": 600,
            "quit": False, **more}


def report(game_id: str, *, players: list[dict[str, Any]] | None = None, ended: int = ENDED) -> dict[str, Any]:
    """A team Slayer game on Guardian as a server reports it: Alpha (Red) beats Bravo."""
    return {"protocol": 1, "game_id": game_id, "server_name": "SPR 1", "map": "guardian", "map_title": "Guardian",
            "variant": "Team Slayer", "mode": "Slayer", "team_game": True, "ranked": True, "started_at": ended - 600,
            "ended_at": ended, "team_scores": {"0": 50, "1": 41},
            "players": players if players is not None else [
                player(ALICE, "Alpha", 0, 1, 12, medals={"double_kill": 2, "killing_spree": 1},
                       weapons={"battle_rifle": {"kills": 10, "deaths": 0, "headshots": 7, "betrayals": 0, "suicides": 0},
                                "melee": {"kills": 2, "deaths": 1, "headshots": 0, "betrayals": 0, "suicides": 0}},
                       versus={BRAVO: {"kills": 12, "deaths": 1}}),
                player(BRAVO, "Bravo", 1, 2, 1, versus={ALICE: {"kills": 1, "deaths": 12}})]}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def stats_file(root: Path, **options: Any) -> Path:
    path = link_file(root)
    set_switch(path, ("stats",), "enabled", True)
    for key, value in options.items():
        set_switch(path, ("stats",), key, value)
    return path


def games(db: Any, count: int = 6) -> None:
    """Alpha beating Bravo, again and again; Charlie (a name Vergil bans) once."""
    for number in range(count):
        store(db, SERVER, read_game(report(f"g-{number}", ended=ENDED + number * 900)), ENDED + number * 900)
    store(db, SERVER, read_game(report("g-c", players=[player(CHARLIE, "Bannedname", 0, 1, 40), player(BRAVO, "Bravo", 1, 2, 1)],
                                       ended=ENDED + 99999)), ENDED + 99999)


def embed_of(sent: dict[str, Any]) -> dict[str, Any]:
    return (sent.get("embeds") or [{}])[0]


def test_a_board_is_a_query_over_whats_reported(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        stats_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            games(huragok.storage)
            kills = board(huragok.storage, Board("kills"), now=ENDED + 100000)
            assert [(row.player_id, row.value) for row in kills][:2] == [(ALICE, 72.0), (CHARLIE, 40.0)]
            assert [row.player_id for row in board(huragok.storage, Board("kd"), now=ENDED + 100000)] == [ALICE, BRAVO]  # Charlie: one game
            assert board(huragok.storage, Board("medal:double_kill"), now=ENDED + 100000)[0].value == 12
            assert board(huragok.storage, Board("weapon:battle_rifle"), now=ENDED + 100000)[0].shown == "60"
            assert not board(huragok.storage, Board("kills", map="Valhalla"), now=ENDED + 100000)
            assert board(huragok.storage, Board("kills", map="guardian"), now=ENDED + 100000)  # by file name or title
            record = card(huragok.storage, ALICE)
            assert record is not None and (record.games, record.wins, record.losses) == (6, 6, 0)
            assert record.best_multi_kill == ("double_kill", 12) and record.best_spree == ("killing_spree", 6)
            assert record.weapon == ("battle_rifle", 60) and record.victim is not None and record.victim[0] == BRAVO

    run(scenario())


def test_stats_and_the_board_in_discord(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        stats_file(tmp_path)
        (tmp_path / "automod.txt").write_text("", encoding="utf-8")  # Vergil on, with names.txt to go by
        (tmp_path / "names.txt").write_text("Bannedname\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            games(huragok.storage)
            _, token = fake.command("stats", user=OTHER, player="alpha")
            record = embed_of(await answered(fake, token))
            assert record["title"] == "Service record · Alpha" and "6 games · 6 won, 0 lost (100%)" in record["description"]
            assert {"Best multi-kill", "Most kills with", "Favourite victim"} <= {field["name"] for field in record["fields"]}
            _, token = fake.command("leaderboard", user=OTHER, stat="kills")
            text = embed_of(await answered(fake, token))["description"]
            assert text.startswith("**1.** Alpha · 72") and "(name hidden)" in text and "Bannedname" not in text
            _, token = fake.command("leaderboard", user=OTHER, stat="nonsense")
            assert "no nonsense to count" in str((await answered(fake, token)).get("content"))
            part.links.use_code(part.links.new_code(ALICE, "Alpha"), OTHER, "Buck")
            _, token = fake.command("leaderboard", user=OTHER, stat="Kills")
            here = part.links.of_member(OTHER)["member_name"]
            assert embed_of(await answered(fake, token))["description"].startswith(f"**1.** {here} · 72")  # claimed
            _, token = fake.command("stats", user=OTHER)  # their own, once linked
            assert embed_of(await answered(fake, token))["title"] == f"Service record · {here}"
            part.links.show(OTHER, "hidden")  # not a choice: the boards don't discriminate (the host's call, 3 November 2026)
            _, token = fake.command("leaderboard", user="9911", stat="kills")
            assert embed_of(await answered(fake, token))["description"].startswith(f"**1.** {here} · 72")  # their spot, earned
            _, token = fake.command("stats", user="9911", player="Alpha")
            assert embed_of(await answered(fake, token))["title"] == f"Service record · {here}"

    run(scenario())


def test_stats_in_game(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            games(huragok.storage)
            huragok.storage.record_join(BRAVO, "Bravo", None)
            await rcon.join(3, "Alpha", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].player_by_id(ALICE), timeout=10)
            await rcon.chat(ALICE, "Alpha", "!stats")
            await wait_for(lambda: told(rcon, "6 games, 6 won (100%)"), timeout=10)
            await rcon.chat(ALICE, "Alpha", "!stats Bravo")
            await wait_for(lambda: told(rcon, "Bravo: 7 games"), timeout=10)

    run(scenario())


def test_the_commands_are_only_there_when_wanted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = stats_file(tmp_path, enabled=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            assert not {"stats", "leaderboard"} & {command["name"] for command in fake.commands[GUILD]}
            set_switch(path, ("stats",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: {"stats", "leaderboard"} <= {command["name"] for command in fake.commands[GUILD]}, timeout=15)

    run(scenario())
