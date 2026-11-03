"""Playlists by player count, the file and its rules (games/byplayers.py): which playlist plays for how many players,
what stops a setup, what's only warned of, and the file Huragok writes beside dedicated.toml."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from huragok.games import byplayers as bp


def three(on: bool = True) -> bp.Setup:
    return bp.Setup("Alpha", on, "playlists/slayer.playlist.json", 30, [
        bp.Step("playlists/small.playlist.json", 10), bp.Step("playlists/medium.playlist.json", 50),
        bp.Step("playlists/big.playlist.json")])


def playlist(folder: Path, name: str, title: str = "") -> None:
    (folder / "playlists").mkdir(parents=True, exist_ok=True)
    games = [{"map": "Guardian", "game": "Slayer"}, {"map": "Valhalla", "game": "CTF"}, {"map": "Narrows", "game": "KOTH"}]
    (folder / "playlists" / f"{name}.playlist.json").write_text(
        json.dumps({"version": 1, "name": title or name.title(), "vote_seconds": 20, "games": games}), encoding="utf-8")


# Which playlist for how many --------------------------------------------------------------------------------------

@pytest.mark.parametrize(("count", "step"), [(0, 0), (10, 0), (11, 1), (50, 1), (51, 2), (128, 2), (5000, 2)])
def test_each_count_plays_the_first_playlist_whose_cap_holds_it_and_the_last_has_none(count: int, step: int) -> None:
    assert three().step_for(count) == step


def test_each_playlist_starts_one_above_the_cap_before_it() -> None:
    assert three().ranges() == [(0, 10), (11, 50), (51, None)]
    assert [bp.describe_range(*r) for r in three().ranges()] == ["up to 10 players", "11 to 50 players",
                                                                "51 players and up"]
    assert bp.describe_range(11, 11) == "11 players"
    assert bp.describe_range(1, 1) == "1 player"


def test_a_cap_has_no_upper_limit() -> None:
    setup = bp.Setup("Alpha", True, "", 30, [bp.Step("playlists/a.playlist.json", 256),
                                             bp.Step("playlists/b.playlist.json")])
    assert setup.problems() == []
    assert setup.step_for(256) == 0 and setup.step_for(257) == 1


# What stops a setup, and what's only a warning ---------------------------------------------------------------------

def test_a_setup_needs_two_playlists_rising_caps_and_only_the_last_uncapped() -> None:
    assert three().problems() == []
    one = bp.Setup("Alpha", True, "", 30, [bp.Step("playlists/a.playlist.json")])
    assert "2 playlists or more" in one.problems()[0]
    falling = bp.Setup("Alpha", True, "", 30, [bp.Step("playlists/a.playlist.json", 50),
                                               bp.Step("playlists/b.playlist.json", 50), bp.Step("playlists/c.playlist.json")])
    assert falling.problems() == ["playlist 2's cap, 50, isn't above the one before it, 50"]
    capped_last = bp.Setup("Alpha", True, "", 30, [bp.Step("playlists/a.playlist.json", 10),
                                                   bp.Step("playlists/b.playlist.json", 20)])
    assert "the last playlist (2) has a cap" in capped_last.problems()[0]
    open_middle = bp.Setup("Alpha", True, "", 30, [bp.Step("playlists/a.playlist.json"),
                                                   bp.Step("playlists/b.playlist.json")])
    assert "has no cap, but isn't the last" in open_middle.problems()[0]


def test_a_playlist_max_players_never_reaches_is_a_warning_not_a_refusal(tmp_path: Path) -> None:
    for name in ("slayer", "small", "medium", "big"):
        playlist(tmp_path, name)
    setup = three()
    assert setup.unreachable(128) == [] and setup.unreachable(None) == []
    assert setup.unreachable(32) == [2]
    assert setup.unreachable(8) == [1, 2]
    assert bp.unreachable_text(setup, 32) == ["playlist 3 (big) never plays while max_players is 32: it starts at 51 "
                                              "players"]
    assert setup.problems() == []  # saved and used all the same
    assert bp.warnings(setup, tmp_path, max_players=32) == bp.unreachable_text(setup, 32)


def test_warnings_name_a_file_gone_auto_reload_off_and_something_that_isnt_a_playlist(tmp_path: Path) -> None:
    for name in ("slayer", "small", "medium"):
        playlist(tmp_path, name)
    found = bp.warnings(three(), tmp_path, max_players=128, auto_reload=False, kind="map")
    assert any(w.startswith("playlist 3 (big) can't be played") for w in found)
    assert any("auto_reload off" in w for w in found)
    assert any("isn't a playlist" in w for w in found)
    assert bp.warnings(three(on=False), tmp_path, max_players=128, auto_reload=False, kind="map") == [
        w for w in found if w.startswith("playlist 3")]  # off: nothing it would do is held up


# The file --------------------------------------------------------------------------------------------------------

def test_the_file_holds_a_block_per_server_and_keeps_the_others(tmp_path: Path) -> None:
    assert bp.read(tmp_path) == {} and bp.setup_of(tmp_path, "Alpha") is None
    bp.save(tmp_path, three())
    beta = bp.Setup("Beta", False, "", 0, [bp.Step("playlists/x.playlist.json", 4), bp.Step("playlists/y.playlist.json")])
    bp.save(tmp_path, beta)
    assert bp.setup_of(tmp_path, "alpha") == three()
    assert bp.setup_of(tmp_path, "Beta") == beta
    text = (tmp_path / bp.FILE).read_text(encoding="utf-8")
    assert text.startswith("# Playlists by player count (Huragok)")
    assert "up_to = 10" in text and text.count("[[server]]") == 2
    tomllib.loads(text)  # what Huragok writes, any TOML reader reads


def test_a_renamed_server_keeps_its_setup_and_one_taken_out_goes(tmp_path: Path) -> None:
    bp.save(tmp_path, three())
    assert bp.rename(tmp_path, "Alpha", "Gamma")
    assert bp.setup_of(tmp_path, "Alpha") is None and bp.setup_of(tmp_path, "Gamma").name == "Gamma"
    assert not bp.rename(tmp_path, "Nobody", "Else")
    assert bp.remove(tmp_path, "Gamma") and not (tmp_path / bp.FILE).exists()


def test_names_and_paths_with_quotes_and_backslashes_survive_the_file(tmp_path: Path) -> None:
    odd = bp.Setup('Bob\'s "Big" Server', True, "playlists\\a b.playlist.json", 30,
                   [bp.Step("playlists/ünï.playlist.json", 3), bp.Step("C:\\Games\\big.playlist.json")])
    bp.save(tmp_path, odd)
    assert bp.setup_of(tmp_path, odd.name) == odd


@pytest.mark.parametrize(("text", "said"), [
    ("[[server]]\non = true\n", "name the server"),
    ('[[server]]\nname = "Alpha"\non = "yes"\n', "on must be true or false"),
    ('[[server]]\nname = "Alpha"\n[[server.step]]\nplaylist = "a"\nup_to = "10"\n', "up_to must be a whole number"),
    ("not toml at all [", "isn't valid TOML"),
])
def test_a_file_that_cant_be_used_says_why(tmp_path: Path, text: str, said: str) -> None:
    (tmp_path / bp.FILE).write_text(text, encoding="utf-8")
    with pytest.raises(bp.ByPlayersError, match=said):
        bp.read(tmp_path)


def test_two_references_to_the_same_file_match_however_written(tmp_path: Path) -> None:
    assert bp.same_playlist(tmp_path, "playlists/big.playlist.json", "playlists\\big.playlist.json")
    assert bp.same_playlist(tmp_path, "./playlists/big.playlist.json", str(tmp_path / "playlists" / "big.playlist.json"))
    assert not bp.same_playlist(tmp_path, "playlists/big.playlist.json", "playlists/small.playlist.json")
    assert not bp.same_playlist(tmp_path, None, "playlists/big.playlist.json")
