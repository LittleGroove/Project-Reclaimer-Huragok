"""stats: every finished game from its server's report, in Huragok's database (0.6.1)."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, wait_for
from testkit.running import SERVER, running

from huragok.core.storage import Storage
from huragok.servers.stats import (LOSS, NONE, SCHEMA, TIE, WIN, read_game, real_ids, store, store_official)
from huragok.servers.statsrelay import configured
from huragok.servers.tempdiscord import Scrubber

CHARLIE = "e" * 64
ENDED = 1793500000  # 2026-11-01, UTC


def player(player_id: str, name: str, team: int, place: int, kills: int = 0, **more: Any) -> dict[str, Any]:
    return {"id": player_id, "name": name, "team": team, "place": place, "score": kills, "kills": kills, "deaths": 1,
            "assists": 0, "betrayals": 0, "suicides": 0, "headshots": 0, "best_streak": kills, "time_played": 600,
            "quit": False, **more}


def report(game_id: str = "g-1", *, team_game: bool = True, scores: dict[str, int] | None = None,
           players: list[dict[str, Any]] | None = None, ended: int = ENDED) -> dict[str, Any]:
    return {"protocol": 1, "game_id": game_id, "server_name": "SPR 1", "map": "guardian", "map_title": "Guardian",
            "variant": "Team Slayer", "mode": "Slayer", "team_game": team_game, "ranked": True,
            "started_at": ended - 600, "ended_at": ended, "team_scores": {"0": 50, "1": 41} if scores is None else scores,
            "players": players if players is not None else [
                player(ALICE, "Alpha", 0, 1, 12, medals={"double_kill": 2, "killing_spree": 1},
                       weapons={"battle_rifle": {"kills": 10, "deaths": 0, "headshots": 7, "betrayals": 0, "suicides": 0},
                                "melee": {"kills": 2, "deaths": 1, "headshots": 0, "betrayals": 0, "suicides": 0}},
                       versus={BRAVO: {"kills": 12, "deaths": 1}}),
                player(BRAVO, "Bravo", 1, 2, 1, versus={ALICE: {"kills": 1, "deaths": 12}})]}


def database(tmp_path: Path) -> Any:
    storage = Storage(tmp_path / "huragok.db")
    storage.migrate("stats", SCHEMA)
    return storage


def players_in(db: Any) -> set[str]:
    return {row["player_id"] for row in db.query("SELECT player_id FROM stats_players JOIN stats_people ON stats_people.number = person")}


def versus_of(db: Any, player_id: str) -> list[tuple[str, int, int, int]]:
    return [(row["other_id"], row["month"], row["kills"], row["deaths"]) for row in db.query(
        "SELECT them.player_id AS other_id, month, kills, deaths FROM stats_versus JOIN stats_people me ON "
        "me.number = person JOIN stats_people them ON them.number = other WHERE me.player_id = ? ORDER BY month",
        (player_id,))]


def test_a_team_game_is_won_by_the_highest_score() -> None:
    game = read_game(report())
    assert game is not None and game.winner == "0" and game.scores == {0: 50, 1: 41}
    assert [part.result for part in game.players] == [WIN, LOSS]
    assert game.players[0].medals == {"double_kill": 2, "killing_spree": 1}
    assert game.players[0].weapons["battle_rifle"]["headshots"] == 7 and game.players[0].versus == {BRAVO: (12, 1)}


def test_a_shared_top_score_is_a_tie_and_one_side_alone_counts_for_nobody() -> None:
    tied = read_game(report(scores={"0": 3, "1": 3}))
    assert tied is not None and tied.winner == "tie" and [part.result for part in tied.players] == [TIE, TIE]
    alone = read_game(report(scores={"1": 5}, players=[player(ALICE, "Alpha", 1, 1), player(BRAVO, "Bravo", 1, 2)]))
    assert alone is not None and alone.winner == "" and {part.result for part in alone.players} == {NONE}


def test_free_for_all_is_first_place_only() -> None:
    """The host's call, 3 November 2026: not Halo 3's "top half"."""
    game = read_game(report(team_game=False, scores={}, players=[player(ALICE, "Alpha", 0, 1), player(BRAVO, "Bravo", 0, 2),
                                                                 player(CHARLIE, "Charlie", 0, 3)]))
    assert game is not None and [part.result for part in game.players] == [WIN, LOSS, LOSS]
    assert all(part.team is None for part in game.players)
    alone = read_game(report(team_game=False, scores={}, players=[player(ALICE, "Alpha", 0, 1)]))
    assert alone is not None and alone.players[0].result == NONE


def test_what_isnt_a_report_is_left_out() -> None:
    assert read_game(None) is None and read_game({"players": []}) is None and read_game({"game_id": "x"}) is None
    odd = read_game(report(players=[{"id": ALICE, "name": "Alpha", "team": 0, "place": "first", "kills": "many",
                                     "medals": {"double_kill": -1}, "weapons": {"sword": "lots"}, "versus": []},
                                    "not a player"]))
    assert odd is not None and len(odd.players) == 1 and odd.players[0].totals["kills"] == 0
    assert odd.players[0].medals == {} and odd.players[0].weapons == {} and odd.players[0].versus == {}


def test_a_game_goes_in_once_and_versus_is_summed_by_month(tmp_path: Path) -> None:
    db = database(tmp_path)
    assert store(db, "SPR 1", read_game(report("g-1")), ENDED)
    assert not store(db, "SPR 1", read_game(report("g-1")), ENDED + 5)  # the server sent it again after a failure
    assert store(db, "SPR 1", read_game(report("g-2")), ENDED + 900)
    games = db.query("SELECT * FROM stats_games ORDER BY game_id")
    assert [row["game_id"] for row in games] == ["g-1", "g-2"] and games[0]["winner"] == "0" and games[0]["red"] == 50
    rows = db.query("SELECT player_id, result, kills FROM stats_players JOIN stats_people ON stats_people.number = person "
                    "JOIN stats_games ON stats_games.number = game WHERE game_id = 'g-1' ORDER BY place")
    assert [(row["player_id"], row["result"], row["kills"]) for row in rows] == [(ALICE, WIN, 12), (BRAVO, LOSS, 1)]
    assert db.query("SELECT SUM(count) AS n FROM stats_medals WHERE medal = 'double_kill'")[0]["n"] == 4
    assert db.query("SELECT SUM(headshots) AS n FROM stats_weapons WHERE weapon = 'battle_rifle'")[0]["n"] == 14
    assert versus_of(db, ALICE) == [(BRAVO, 202611, 24, 2)]


def test_the_services_record_is_kept_when_its_newer(tmp_path: Path) -> None:
    db = database(tmp_path)
    answer = {"players": [{"id": ALICE, "name": "Alpha", "rank": 8, "title": "Sergeant", "grade": 2, "exp": 37,
                           "skill": 21, "games": 40, "wins": 22, "time_played": 36000}]}
    assert store_official(db, answer, 100.0) == 1
    assert store_official(db, {"players": [{"id": ALICE, "name": "Alpha", "title": "Recruit"}]}, 50.0) == 0  # older
    row = db.query("SELECT * FROM stats_official")[0]
    assert (row["title"], row["exp"], row["seconds"], row["as_of"]) == ("Sergeant", 37, 36000, 100.0)
    assert store_official(db, "not an answer", 200.0) == 0


def copies(folder: Path, scrub: Scrubber) -> Path:
    """statsrelay's copies as the stats part (and tempdiscord before it) wrote them, stand-ins for IDs: one gzipped,
    from before 0.6.0, when the keys of `versus` were kept whole; one still being written. Today's, so nothing
    tidies them away while the test runs. The one still being written."""
    folder.mkdir(parents=True, exist_ok=True)
    copy = {"at": ENDED + 2.0, "server": "SPR 1", "request": {"method": "POST", "path": "/v1/matches", "bytes": 1,
            "body": {"json": {"report": report("g-1"), "key": "k=", "signature": "s="}}},
            "answer": {"status": 202, "body": {"json": {"game_id": "g-1", "players": [
                {"id": ALICE, "name": "Alpha", "title": "Sergeant", "rank": 8}]}}}}
    scrubbed = scrub.value(copy)
    for given in scrubbed["request"]["body"]["json"]["report"]["players"]:
        given["versus"] = {(BRAVO if given["name"] == "Alpha" else ALICE): next(iter(given["versus"].values()))}
    lookup = {"at": ENDED + 3.0, "server": "SPR 1", "request": {"path": "/v1/unlocks/lookup"}, "answer": {"status": 200}}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    (folder / f"stats-reports-{today}.jsonl.gz").write_bytes(
        gzip.compress((json.dumps(scrubbed) + "\n" + json.dumps(lookup) + "\n{cut short").encode()))
    second = scrub.value({"at": ENDED + 90000.0, "server": "SPR 2", "request": {"path": "/v1/matches", "body": {
        "json": {"report": report("g-2", ended=ENDED + 86400)}}}, "answer": {"status": 202}})
    growing = folder / f"stats-reports-{today}.jsonl"
    growing.write_text(json.dumps(second) + "\n", encoding="utf-8")
    return growing


def test_the_backfill_turns_stand_ins_back_into_the_players_ids(tmp_path: Path) -> None:
    scrub = Scrubber(b"k" * 32)
    ids = real_ids(scrub, [ALICE, BRAVO])
    assert ids(scrub.stand_in(ALICE)) == ALICE and ids(scrub.stand_in(CHARLIE)) is None and ids(BRAVO) == BRAVO
    assert ids("someone") is None


def test_the_reports_already_kept_are_read_in_once(tmp_path: Path, run) -> None:
    """As it starts, the stats part reads statsrelay's copies back in: the stats begin with what's been played."""
    folder = tmp_path / "logs" / "temp-discord" / "stats-reports"

    async def scenario() -> None:
        async with running(tmp_path) as (huragok, _, _):
            for player_id, name in ((ALICE, "Alpha"), (BRAVO, "Bravo")):
                huragok.storage.record_join(player_id, name, None, when=ENDED - 100)
            stats = huragok.runners["stats"].part
            growing = copies(folder, stats.scrub)
            assert await stats.backfill() == 2
            games = huragok.storage.query("SELECT game_id, server FROM stats_games ORDER BY game_id")
            assert [(row["game_id"], row["server"]) for row in games] == [("g-1", "SPR 1"), ("g-2", "SPR 2")]
            assert players_in(huragok.storage) == {ALICE, BRAVO}
            assert [(other, kills) for other, _, kills, _ in versus_of(huragok.storage, ALICE)] == [(BRAVO, 24)]  # both in November
            assert huragok.storage.query("SELECT title FROM stats_official JOIN stats_people ON stats_people.number = person WHERE "
                                         "player_id = ?", (ALICE,))[0]["title"] == "Sergeant"
            assert await stats.backfill() == 0  # read once
            with growing.open("a", encoding="utf-8") as file:
                file.write(json.dumps(stats.scrub.value({"at": ENDED + 99999.0, "server": "SPR 2", "request": {
                    "path": "/v1/matches", "body": {"json": {"report": report("g-3", ended=ENDED + 90000)}}},
                    "answer": {"status": 202}})) + "\n")
            assert await stats.backfill() == 1  # grown since: only what's new comes in

    run(scenario())


def test_a_report_through_the_relay_is_counted_and_its_copy_kept(tmp_path: Path, run) -> None:
    from test_tempdiscord import FakeService, post

    async def scenario() -> None:
        async with FakeService() as service:
            async with running(tmp_path) as (huragok, _, events):
                config = tmp_path / "server" / "dedicated.toml"
                log = huragok.settings.server(SERVER).log_path
                with log.open("a", encoding="utf-8") as file:
                    file.write(f"10:00:00 Stats reporting to {service.url}; server key k= (whitelist this public key "
                               "on the service)\n")
                url = await wait_for(lambda: (configured(config)[1] or "").startswith("http://127.0.0.1:")
                                     and configured(config)[1])
                body = json.dumps({"report": report("g-live"), "key": "k=", "signature": "s="}).encode()
                status, _ = await post(url, body)
                assert status == 201 and service.received[0][2] == body
                game = await wait_for(lambda: huragok.storage.query("SELECT * FROM stats_games"))
                assert game[0]["game_id"] == "g-live" and game[0]["server"] == SERVER
                assert players_in(huragok.storage) == {ALICE, BRAVO}
                told = await wait_for(lambda: [e for e in events if e.name == "stats.game"])
                assert told[0].get("game_id") == "g-live" and told[0].get("players") == 2
                kept = list((tmp_path / "logs" / "temp-discord" / "stats-reports").glob("*.jsonl"))
                assert kept and ALICE not in kept[0].read_text(encoding="utf-8")  # the copy has stand-ins

    run(scenario())
