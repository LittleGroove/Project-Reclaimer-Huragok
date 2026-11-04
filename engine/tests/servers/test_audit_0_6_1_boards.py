"""The audit before 0.6.1 (plans/bug-audit-0.6.1.md): the boards (area 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO
from test_stats import ENDED, database, player, report

from huragok.discordbot.standings import PERIOD_WORDS, counted
from huragok.servers import leaderboards
from huragok.servers.leaderboards import Board, board, board_soon, choices, current_season
from huragok.servers.stats import read_game, store

CHARLIE = "9" * 64


def played(db: Any, game_id: str, players: list[dict[str, Any]], *, ended: int = ENDED, scores: dict[str, int] | None = None) -> None:
    game = read_game(report(game_id, players=players, ended=ended, scores=scores))
    assert game is not None and store(db, "S", game, float(ended))


def test_a_season_board_with_no_season_running_has_nobody(tmp_path: Path) -> None:
    """2.2: it showed all time, titled "this season", and the top roles went to the all-time leaders."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 12), player(BRAVO, "Bravo", 1, 2, 3)])
    now = ENDED + 60.0
    assert current_season(db) is None and board(db, Board("kills", "season"), now=now) == []
    db.execute("INSERT INTO stats_seasons (name, started, started_by) VALUES ('Winter', ?, 'Faculty')", (ENDED - 3600.0,))
    assert [row.player_id for row in board(db, Board("kills", "season"), now=now)] == [ALICE, BRAVO]
    assert Board("kills", "day").title == "Kills · last 24 hours" and "season" in PERIOD_WORDS  # 2.6, 2.7


def test_ties_and_rates(tmp_path: Path) -> None:
    """2.8: ties went by whatever order SQLite read them, and on a rate to fewer games. 2.9: win rate's minimum counted
    games nobody won."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 10), player(BRAVO, "Bravo", 1, 2, 10)])  # Alpha's in the stats first
    assert [row.player_id for row in board(db, Board("kills"))] == [ALICE, BRAVO]
    for number in range(4):  # Charlie: 5 games, no deaths; Alpha 1 game more, no deaths either
        played(db, f"g-c{number}", [player(CHARLIE, "Charlie", 0, 1, 2, deaths=0), player(BRAVO, "Bravo", 1, 2, 1)], ended=ENDED + number + 1)
    played(db, "g-c4", [player(CHARLIE, "Charlie", 0, 1, 2, deaths=0), player(ALICE, "Alpha", 1, 2, 2, deaths=0)], ended=ENDED + 10)
    for number in range(4):
        played(db, f"g-a{number}", [player(ALICE, "Alpha", 0, 1, 2, deaths=0), player(BRAVO, "Bravo", 1, 2, 1)], ended=ENDED + 20 + number)
    kd = board(db, Board("kd"), min_games=5)
    assert [row.player_id for row in kd][:2] == [ALICE, CHARLIE] and kd[0].games == 6  # 12 kills (K/D 12) beat 10, by more games
    played(db, "g-t", [player(BRAVO, "Bravo", 0, 1, 1)], ended=ENDED + 40, scores={"0": 0, "1": 0})  # one team: nobody wins
    assert all(row.player_id != BRAVO or row.games >= 5 for row in board(db, Board("win_rate"), min_games=5))


def test_medal_choices_and_a_board_read_off_the_loop(tmp_path: Path, run) -> None:
    """2.4: the suggestions read every medal row; 2.1: a board ran on the event loop."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 12, medals={"overkill": 1, "double_kill": 2}), player(BRAVO, "Bravo", 1, 2, 3)])
    assert choices(db, "medal", most=50) == ["double_kill", "overkill"]
    assert db.path is not None

    async def scenario() -> None:
        assert [row.player_id for row in await board_soon(db, Board("medal:overkill", "day"), now=ENDED + 60.0)] == [ALICE]

    run(scenario())


def test_a_season_over_has_its_own_window(tmp_path: Path) -> None:
    """2.10: the season card read its own query, and showed "(name hidden)" where the next player should be."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 12), player(BRAVO, "Bravo", 1, 2, 3)], ended=ENDED)
    played(db, "g-2", [player(BRAVO, "Bravo", 0, 1, 30), player(ALICE, "Alpha", 1, 2, 1)], ended=ENDED + 7200)
    within = board(db, Board("kills", rows=1), between=(ENDED - 1.0, ENDED + 3600.0), skip={BRAVO})
    assert [row.player_id for row in within] == [ALICE] and within[0].value == 12


def test_counts_from_discord_toml_are_kept_in_range() -> None:
    """2.11: a 0 took a default, an inf stopped every tick, and a big one made a board too long for Discord."""
    assert counted(0, 10, 1, 25) == 1 and counted(float("inf"), 10, 1, 25) == 10 and counted(500, 10, 1, 25) == 25
    assert counted("x", 5, 1, 1000) == 5 and leaderboards.ROWS == 10
