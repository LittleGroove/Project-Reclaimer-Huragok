"""The audit before 0.6.1 (plans/bug-audit-0.6.1.md): stats intake (area 1)."""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO
from test_stats import ENDED, report
from test_tempdiscord import FakeService, dedicated, post
from testkit.running import running

from huragok.core.storage import Storage
from huragok.servers import weapons
from huragok.servers.leaderboards import red_blue
from huragok.servers.stats import SCHEMA, read_copies, read_game, store
from huragok.servers.statsrelay import StatsCapture, configured
from huragok.servers.tempdiscord import Scrubber

SCRUB = Scrubber(b"k" * 32)


def test_a_server_still_running_reports_through_a_clean_restart_and_a_rename(tmp_path: Path, run) -> None:
    """1.1: Huragok stopping puts stats_server back as it was; started again, it heard nothing of where the service
    was (server.log had moved on) or knew the server by a new name, and the server, still running, reported into a port
    nobody listened on until it was next empty."""
    async def scenario() -> None:
        async with FakeService() as service:
            config = dedicated(tmp_path / "one", "")  # the project's own service
            records = tmp_path / "records"
            first = StatsCapture(records, lambda s, d: None, SCRUB)
            await first.apply(["One"], {"One": (config, None)})
            await first.learned("One", service.url, config, None)  # its server.log says, as it starts
            _, relay = configured(config)
            await first.close()
            assert configured(config)[1] == ""  # put back as it was, while the server runs on with the relay's address
            again = StatsCapture(records, lambda s, d: None, SCRUB)
            await again.apply(["One"], {"One": (config, None)})
            assert (await post(relay, json.dumps({"report": report("g-1")}).encode()))[0] == 201
            await again.close()
            renamed = StatsCapture(records, lambda s, d: None, SCRUB)
            await renamed.apply(["Two"], {"Two": (config, None)})
            assert (await post(relay, json.dumps({"report": report("g-2")}).encode()))[0] == 201
            assert set(renamed.saved["servers"]) == {"Two"}
            await renamed.close()

    run(scenario())


def test_a_copy_that_cant_be_kept_doesnt_lose_the_game(tmp_path: Path, run) -> None:
    """1.2: the copy ran first, in the same try: one that couldn't be written (its folder moved) lost the game."""
    counted: list[str] = []

    def record(server: str, data: dict[str, Any]) -> None:
        raise FileNotFoundError("logs/temp-discord/stats-reports")

    async def scenario() -> None:
        async with FakeService() as service:
            config = dedicated(tmp_path / "one", service.url)
            capture = StatsCapture(tmp_path / "records", record, SCRUB, on_report=lambda server, *_: counted.append(server))
            await capture.apply(["One"], {"One": (config, None)})
            assert (await post(configured(config)[1], json.dumps({"report": report("g-x")}).encode()))[0] == 201
            await capture.close()

    run(scenario())
    assert counted == ["One"]


def test_one_odd_line_never_stops_the_backfill(tmp_path: Path, run) -> None:
    """1.3: a count past 64 bits (one gone round from 0) stopped the backfill at the same line at every start, and
    the games after it, and every later file, never came in."""
    folder = tmp_path / "logs" / "temp-discord" / "stats-reports"

    async def scenario() -> None:
        async with running(tmp_path) as (huragok, _, _):
            for player_id, name in ((ALICE, "Alpha"), (BRAVO, "Bravo")):
                huragok.storage.record_join(player_id, name, None, when=ENDED - 100)
            stats = huragok.runners["stats"].part
            folder.mkdir(parents=True, exist_ok=True)
            bad = report("g-bad")
            bad["players"][0]["time_played"] = 2 ** 64 - 5
            lines = [stats.scrub.value({"at": 1.0, "server": "S", "request": {"path": "/v1/matches", "body": {"json": {"report": one}}},
                                        "answer": {"status": 202}}) for one in (bad, report("g-good"))]
            lines.append({"at": 1.0, "server": "S", "request": {"path": "/v1/matches", "body": {"json": [1, 2]}}, "answer": {"status": 400}})
            (folder / "stats-reports-2026-10-20.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            await stats.backfill()
            games = {row["game_id"] for row in huragok.storage.query("SELECT game_id FROM stats_games")}
            assert games == {"g-bad", "g-good"}  # the odd count read as 0, the line that isn't a report passed over
            assert huragok.storage.query("SELECT file FROM stats_read")

    run(scenario())
    assert len(read_copies(folder / "stats-reports-2026-10-20.jsonl")) == 2  # the line that isn't a report passed over


def test_a_report_keeps_its_own_games_kills(tmp_path: Path, run) -> None:
    """1.4: a report that came after the next game began took that game's first kills, and lost its own."""
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, _, _):
            stats = huragok.runners["stats"].part
            game = read_game(report("g-k"))
            assert game is not None and store(huragok.storage, "S", game, ENDED + 5.0)
            stats.before["S"] = [(ENDED - 300.0, "1", "2", 11), (ENDED - 10.0, "2", "1", 20)]  # the game that ended
            stats.kills["S"] = [(ENDED + 40.0, "1", "2", 11)]  # the next one, begun before the report came
            stats._keep_kills("S", game)
            kept = huragok.storage.query("SELECT kills FROM stats_kills")
            assert len(json.loads(gzip.decompress(kept[0]["kills"]))) == 2
            assert stats.kills["S"] == [(ENDED + 40.0, "1", "2", 11)]  # the next game's stays for its own report

    run(scenario())


def test_a_report_without_an_end_or_an_id(tmp_path: Path) -> None:
    """1.10: a report without ended_at fell out of every season and Red against Blue. 1.11: a player with no ID was
    kept, as someone called ""."""
    odd = report("g-noend")
    del odd["ended_at"]
    db = Storage(tmp_path / "h.db")
    db.migrate("stats", SCHEMA)
    now = time.time()
    store(db, "S", read_game(odd), now)
    assert db.query("SELECT ended FROM stats_games")[0]["ended"] == now and red_blue(db, now - 86400)["games"] == 1
    no_id = report("g-noid")
    del no_id["players"][1]["id"]
    store(db, "S", read_game(no_id), now)
    assert "" not in {row["player_id"] for row in db.query("SELECT player_id FROM stats_people")}


def test_the_kill_bytes_named() -> None:
    """1.9: +192 read as a splatter on anything (the Warthog's gun), and kinds the reports name read "something"."""
    assert weapons.how(47 + 192).words() == "Warthog gun"
    assert weapons.how(40 + 192).words() == "Ghost, splatter"
    assert [weapons.how(kind).weapon for kind in (1, 5, 7, 23, 30, 48)] == ["a fall", "Magnum", "Needler", "Flamethrower", "Firebomb Grenade", "Gauss gun"]


def test_a_timed_season_ends_at_its_time(tmp_path: Path, run) -> None:
    """1.12: a season with a length ended at the next minute's look, and the games in that minute counted toward it."""
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, _, _):
            stats = huragok.runners["stats"].part
            ends = time.time() - 30
            huragok.storage.execute("INSERT INTO stats_seasons (name, started, ends, started_by) VALUES (?, ?, ?, ?)",
                                    ("Short", ends - 3600, ends, "Faculty"))
            await stats.season_ends()
            assert huragok.storage.query("SELECT ended FROM stats_seasons")[0]["ended"] == ends

    run(scenario())


def test_a_games_kill_feed_goes_into_the_temp_discord_record(tmp_path: Path, run) -> None:
    """The host, 4 November 2026: each game's Kill feed, as kept, in the record, to check against real games after the
    update; its player IDs as stand-ins, as everything there."""
    from testkit.running import wait_for

    async def scenario() -> None:
        async with running(tmp_path) as (huragok, _, _):
            stats = huragok.runners["stats"].part
            game = read_game(report("g-feed"))
            assert game is not None and store(huragok.storage, "S", game, ENDED + 5.0)
            stats.engines["S"] = {"1": (ALICE, "Alpha"), "2": (BRAVO, "Bravo")}
            stats.kills["S"] = [(ENDED - 100.0, "1", "2", 11 + 64)]
            stats._keep_kills("S", game)
            folder = tmp_path / "logs" / "temp-discord"

            def written() -> str:
                return "".join(path.read_text(encoding="utf-8") for path in folder.glob("temp-discord-*.jsonl"))

            await wait_for(lambda: "stats.kills" in written(), timeout=10)
            line = next(line for line in written().splitlines() if "stats.kills" in line)
            assert "Battle Rifle, headshot" in line and ALICE not in line and "Alpha" in line

    run(scenario())
