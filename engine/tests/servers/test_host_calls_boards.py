"""The host's calls of 3 November 2026 on the boards (plans/bug-audit-0.6.1.md, "The host's answers")."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO
from test_stats import ENDED, database, player, report

from huragok.discordbot.standings import ordered_boards
from huragok.servers import leaderboards
from huragok.servers.leaderboards import Board, board, card, red_blue
from huragok.servers.stats import read_game, store

CHARLIE = "9" * 64


def played(db: Any, game_id: str, players: list[dict[str, Any]], *, ended: int = ENDED, mode: str = "Slayer",
           scores: dict[str, int] | None = None) -> None:
    given = report(game_id, players=players, ended=ended, scores=scores)
    given["mode"] = mode
    game = read_game(given)
    assert game is not None and store(db, "S", game, float(ended))


def test_a_tie_shares_its_place(tmp_path: Path) -> None:
    """16: two first, then third; and everyone tied at the cutoff, for a top role."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 10), player(BRAVO, "Bravo", 1, 2, 10), player(CHARLIE, "Charlie", 1, 3, 4)])
    assert [row.place for row in board(db, Board("kills"))] == [1, 1, 3]
    assert len(board(db, Board("kills", rows=1))) == 1 and len(board(db, Board("kills", rows=1), ties=True)) == 2


def test_a_cards_medals_are_the_rarest_first(tmp_path: Path) -> None:
    """15: an Extermination over a thousand rocket kills."""
    leaderboards._RARITY["at"] = float("-inf")
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 10, medals={"rocket_kill": 50, "extermination": 1}),
                       player(BRAVO, "Bravo", 1, 2, 3, medals={"rocket_kill": 40})])
    leaderboards._RARITY["seen"] = leaderboards._medal_counts(db)  # as the stats part's hourly count leaves it
    found = card(db, ALICE)
    assert found is not None and found.medals[0][0] == "extermination"  # (a Killionaire shows as the best multi-kill)


def test_red_against_blue_is_only_red_against_blue(tmp_path: Path) -> None:
    """9, 10: a third team, or Infection or Juggernaut (where a team is what you are), isn't counted."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 10), player(BRAVO, "Bravo", 1, 2, 3)])
    played(db, "g-2", [player(ALICE, "Alpha", 0, 1, 10), player(BRAVO, "Bravo", 1, 2, 3)], mode="Infection", ended=ENDED + 10)
    played(db, "g-3", [player(ALICE, "Alpha", 0, 1, 10), player(BRAVO, "Bravo", 1, 2, 3), player(CHARLIE, "Charlie", 2, 3, 1)],
           ended=ENDED + 20, scores={"0": 50, "1": 41, "2": 3})
    assert red_blue(db)["games"] == 1


def test_between_seasons_a_season_board_is_the_season_just_ended(tmp_path: Path) -> None:
    """12: and the boards' order, all time's first and the season's below."""
    db = database(tmp_path)
    played(db, "g-1", [player(ALICE, "Alpha", 0, 1, 10), player(BRAVO, "Bravo", 1, 2, 3)], ended=ENDED)
    db.execute("INSERT INTO stats_seasons (name, started, ended, started_by) VALUES ('Winter', ?, ?, 'Faculty')", (ENDED - 3600.0, ENDED + 3600.0))
    played(db, "g-2", [player(BRAVO, "Bravo", 0, 1, 30), player(ALICE, "Alpha", 1, 2, 1)], ended=ENDED + 7200)  # after it
    assert [row.player_id for row in board(db, Board("kills", "season"), now=ENDED + 8000.0)] == [ALICE, BRAVO]
    assert leaderboards.season_numbered(db, 1)["name"] == "Winter" and leaderboards.last_season(db)["number"] == 1
    assert ordered_boards(["kills season", "kills all", "red-blue", "kd all"]) == ["kills all", "kd all", "kills season", "red-blue"]


def test_everyone_tied_at_the_top_and_a_tie_as_players_see_it(tmp_path: Path) -> None:
    """Second audit, 7.5: past 25 tied, the rest missed the top role. And a tie is what's shown: K/D 2.004 and 1.996
    both read 2.00, and share first."""
    db = database(tmp_path)
    tied = [player(f"{number:02d}" + "f" * 62, f"P{number}", number % 2, 1, 1) for number in range(40)]
    played(db, "g-1", tied)
    assert len(board(db, Board("kills", rows=1), ties=True)) == 40
    db2 = database(tmp_path / "two")
    played(db2, "g-2", [player(ALICE, "Alpha", 0, 1, 501, deaths=250), player(BRAVO, "Bravo", 1, 2, 499, deaths=250)])
    assert [row.place for row in board(db2, Board("kd"), min_games=1)] == [1, 1]
