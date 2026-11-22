"""B13: the program's own lines about a game it couldn't play (0.10.6-0.10.9), read from its console and its
server.log. The shapes are the program's own text: the 0.10.8 and 0.10.9 files read as bytes on 22 November 2026, the
same in both; no server here has said one yet, so the lines below are written from those templates."""

from __future__ import annotations

import pytest

from huragok.servers.logs import parse_console_line, parse_log_line

CONSOLE = [
    ('[server] "LG Races": the map of its game Grand Prix on Daybreak failed to load (its cache is damaged (tag 4)), '
     "1 of 3 times in a row: starting it again in 5 s. See C:\\SERVERS\\LG Races\\data\\lg-races\\server.log.",
     {"what": "retry", "game": "Grand Prix on Daybreak", "reason": "its cache is damaged (tag 4)", "times": 1,
      "of": 3, "wait": 5}),
    ('[server] "LG Races": the map of its game Grand Prix on Daybreak failed to load 3 times in a row (its cache is '
     "damaged): starting the next game; it is skipped for 30 minutes. See C:\\x\\server.log.",
     {"what": "skipped", "game": "Grand Prix on Daybreak", "times": 3, "reason": "its cache is damaged",
      "skip": "30 minutes"}),
    ('[server] "LG Forge": its game Forge on HugeGrass SPR cannot be played (its content needs 900 MB more than the '
     "engine has): starting the next game; it is skipped from now on. See x.log.",
     {"what": "unplayable", "game": "Forge on HugeGrass SPR",
      "reason": "its content needs 900 MB more than the engine has"}),
    ('[server] "LG Races": no game of its playlist can be played (the last: Death Race on Endurance): stopped. See x.',
     {"what": "none_playable", "game": "Death Race on Endurance"}),
    ('[server] "LG Races": a game that is not in the playlist, Slayer on Nowhere, could not be played (no such map): '
     "no game is skipped for it; starting the playlist's next game. See x.",
     {"what": "outside", "game": "Slayer on Nowhere", "reason": "no such map"}),
]


@pytest.mark.parametrize("line, expected", CONSOLE)
def test_the_supervisors_lines_about_a_game_it_couldnt_play(line: str, expected: dict) -> None:
    name, data = parse_console_line(line)
    assert name == "server.game_skip"
    assert {k: data[k] for k in expected} == expected
    # The same lines, should one reach its server.log under its [server] tag, are read there too
    name, data = parse_log_line(line)
    assert name == "log.game_skip" and data["what"] == expected["what"] and data["game"] == expected["game"]


@pytest.mark.parametrize("line, what, game", [
    ("[server] Grand Prix on Daybreak is skipped no more: its skip for failed loads ran out", "skip_ended",
     "Grand Prix on Daybreak"),
    ("[server] Slayer on Guardian could not be loaded (the map's tags are damaged): the server starts again",
     "load_failed", "Slayer on Guardian"),
    ("[server] Race on Road Course ended with no results 2 times in a row with players in it: the playlist goes on "
     "without it", "no_results", "Race on Road Course"),
])
def test_the_games_own_lines_in_its_log(line: str, what: str, game: str) -> None:
    name, data = parse_log_line(line)
    assert (name, data["what"], data["game"]) == ("log.game_skip", what, game)


def test_other_lines_are_left_as_they_were() -> None:
    """A crash line, a player leaving, a line someone typed: never a game skipped."""
    assert parse_console_line('[server] "LG Races": the engine ended; starting it again with the next game')[0] == \
        "server.crashed"
    assert parse_log_line("[server] player #3 (Bob) left")[0] == "log.left"
    assert parse_log_line("[chat] Bob: Slayer on Guardian is skipped no more: its skip for failed loads ran out") is None
