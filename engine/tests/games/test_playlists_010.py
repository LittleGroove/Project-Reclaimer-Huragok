"""What a server plays, read the way Project Reclaimer 0.10 writes it (plans/server-files-audit/playlists-format.md and
gamemodes-layouts-format.md, verified on 0.10.8): 0.10.1's `playlists = [...]` (B1), co-op entries, curves and
formulas, `teams` (B5, B6, B11), `.rmeg` game modes (B4) and Forge layouts' objects. Read, shown, never refused."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path

from huragok.games import files as game_files
from huragok.games import halo3_options as go
from huragok.games.gamemodes import (
    OBJECTS_MOST, layout_objects, list_game_modes, list_layouts, rmeg_header,
)
from huragok.games.halo3 import Playlist, game_label, is_curve, load_playlist, value_text
from huragok.games.plays import (
    PLAYS_KEYS, describe_playlists, playlist_refs, plays_block, plays_kind, plays_of,
)

SMALL_BIG = [{"file": "playlists/small.playlist.json", "players": "1-8"},
             {"file": "playlists/big.playlist.json", "players": "9+", "recommended_players": "12-24"}]


# B1: playlists = [...] ----------------------------------------------------------------------------------------------

def test_playlists_by_lobby_size_are_their_own_kind_and_said_as_such() -> None:
    """0.10.1's native playlists by lobby size: never "Slayer on Guardian" (what Huragok said before)."""
    block = {"name": "Alpha", "playlists": SMALL_BIG}
    assert "playlists" in PLAYS_KEYS
    assert plays_kind(block) == "playlists"
    assert plays_of(block) == "playlists by lobby size: small 1-8, big 9+"
    assert plays_of({"playlists": [{"file": "playlists/any.playlist.json"}]}) == "playlists by lobby size: any"
    assert describe_playlists([]) == "playlists by lobby size (none named)"
    assert plays_of({"playlists": "nonsense"}).startswith("playlists by lobby size")  # never a traceback


def test_every_playlist_file_a_block_names_is_found() -> None:
    refs = playlist_refs({"playlists": [*SMALL_BIG, {"players": "3"}, "x"]})  # entries without a file: left out
    assert [(r.file, r.how, r.players, r.recommended) for r in refs] == [
        ("playlists/small.playlist.json", "playlists", "1-8", ""),
        ("playlists/big.playlist.json", "playlists", "9+", "12-24")]
    assert [r.label for r in refs] == ["small 1-8", "big 9+"]
    assert [(r.file, r.how) for r in playlist_refs({"playlist": "playlists/one.playlist.json"})] == [
        ("playlists/one.playlist.json", "playlist")]
    assert playlist_refs({"map": "Guardian"}) == []


def test_defaults_name_what_a_server_plays_when_its_block_names_nothing() -> None:
    """`playlist`, `playlists`, `variant` and `map` + `game` may stand in [defaults] (verified on 0.10.8)."""
    defaults = {"playlists": SMALL_BIG, "max_players": 16}
    assert plays_block({"name": "A"}, defaults)["playlists"] == SMALL_BIG
    assert "max_players" not in plays_block({"name": "A"}, defaults)
    own = {"name": "A", "map": "Guardian"}
    assert plays_block(own, defaults) is own  # its own wins
    assert plays_block({"name": "A"}, None) == {"name": "A"}


def test_game_files_needed_by_every_playlist_of_playlists(tmp_path: Path) -> None:
    """B1: games/files.py read only `playlist`, so Reach's or ODST's files a `playlists` file needs were missed."""
    (tmp_path / "playlists").mkdir()
    (tmp_path / "playlists" / "small.playlist.json").write_text(json.dumps({"games": [
        {"map": "Sword Base", "game": "Slayer"}]}), encoding="utf-8")
    (tmp_path / "playlists" / "big.playlist.json").write_text(json.dumps({"games": [
        {"odst": {"mode": "firefight", "mission": 5000, "insertion": 7}}]}), encoding="utf-8")
    kinds = game_files.needed_by({"playlists": SMALL_BIG}, tmp_path, "0.10.8")
    assert kinds[0] == "halo3" and "reach" in kinds and "odst" in kinds


# B5, B6, B11: everything 0.10 writes in a playlist -------------------------------------------------------------------

def test_co_op_entries_are_named_by_their_mission() -> None:
    assert game_label({"campaign": {"mission": 3010, "difficulty": "heroic"}}) == "Campaign: Sierra 117 (heroic)"
    assert game_label({"campaign": {"mission": 3070, "insertion": 1}}) == "Campaign: The Ark (normal, start 1)"
    assert game_label({"odst": {"mode": "firefight", "mission": 5000, "insertion": 7}}) == \
        "ODST Firefight: Crater (Night) (normal)"
    assert game_label({"odst": {"mission": 6100}}) == "ODST campaign: Tayari Plaza (normal)"
    assert game_label({"map": "Guardian", "game": "Slayer", "name": "Team Slayer"}) == "Team Slayer on Guardian (Slayer)"


def test_curves_and_formulas_are_kept_as_written_and_follow_the_lobby_size(tmp_path: Path) -> None:
    """0.10.1-0.10.2: vote_seconds, a weight and an option may follow the player count. Huragok used to say the
    playlist "can't be read" (B6)."""
    path = tmp_path / "curves.playlist.json"
    data = {"version": 2, "name": "Curves", "vote_seconds": {"points": [[2, 15], [16, 25], [64, 30]]},
            "players": "1-16", "games": [
                {"map": "Guardian", "game": "Slayer", "weight": {"formula": "3 + p/8", "max": 6}},
                {"map": "Valhalla", "game": "Slayer", "options": {"respawn.seconds": {"points": [[1, 3], [16, 8]]}},
                 "teams": True, "players": "4-16"},
                {"campaign": {"mission": 3010}}]}
    path.write_text(json.dumps(data), encoding="utf-8")
    playlist = load_playlist(path)
    assert playlist.vote == "15-30 s, follows the lobby size" and playlist.players == "1-16"
    labels = playlist.labels()
    assert "weight follows the lobby size" in labels[0]
    assert "options (some follow the lobby size)" in labels[1] and "teams on" in labels[1] and "players 4-16" in labels[1]
    assert labels[2] == "Campaign: Sierra 117 (normal)"
    assert playlist.data()["vote_seconds"] == data["vote_seconds"]  # kept as written
    assert is_curve({"formula": "p"}) and not is_curve(20) and value_text({"formula": "p"}) == "follows the lobby size"


def test_a_playlist_is_refused_only_when_it_isnt_one(tmp_path: Path) -> None:
    """vote_seconds of any shape is the program's to judge; only a file without a games list isn't a playlist."""
    weird = Playlist.from_data(tmp_path / "x.playlist.json", {"vote_seconds": "30s", "games": [{"map": "Guardian"}]})
    assert weird.vote == "30s"
    no_vote = Playlist.from_data(tmp_path / "y.playlist.json", {"games": []})
    assert no_vote.vote_seconds == 20 and "vote_seconds" not in no_vote.data()  # nothing added that it didn't say


# B4: .rmeg game modes, and layouts --------------------------------------------------------------------------------

RMEG = '''-- Laser Tag: one shot kills
--[=[ mode
name = "Laser Tag"
mode = "Slayer"
author = "LG"
[options]
"rounds.limit" = 2
]=]
alias size = player.number[0]
'''


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def test_rmeg_headers_name_a_mode() -> None:
    header = rmeg_header(RMEG)
    assert header is not None and header["name"] == "Laser Tag" and header["options"]["rounds.limit"] == 2
    assert rmeg_header("alias x = 1\n") is None
    try:
        rmeg_header("--[=[ mode\nname = \n]=]")
    except ValueError as error:
        assert "header can't be read" in str(error)
    else:
        raise AssertionError("a broken header was read")


def test_game_modes_of_every_kind_are_listed(tmp_path: Path) -> None:
    content = tmp_path / "content"
    write(content / "Game Modes" / "SPR" / "BR Fiesta CTF.json", json.dumps(
        {"version": 2, "name": "BR Fiesta CTF", "mode": "CaptureTheFlag", "rules": {}}))
    write(content / "Game Modes" / "laser.rmeg", RMEG)
    write(content / "Game Modes" / "no header.rmeg", "alias x = 1\n")
    write(content / "Game Modes" / "MCC" / "team_snipers.bin", b"_blf" + b"\0" * 32)
    write(content / "Game Modes" / "MCC" / "not blf.bin", b"text")
    write(content / "Game Modes" / "bom.json", b"\xef\xbb\xbf" + json.dumps({"name": "B", "mode": "Slayer"}).encode())
    write(content / "Game Modes" / "README.txt", "not a mode")
    modes = {m.relative: m for m in list_game_modes(content)}
    assert set(modes) == {"Game Modes/SPR/BR Fiesta CTF.json", "Game Modes/laser.rmeg", "Game Modes/no header.rmeg",
                          "Game Modes/MCC/team_snipers.bin", "Game Modes/MCC/not blf.bin", "Game Modes/bom.json"}
    laser = modes["Game Modes/laser.rmeg"]
    assert (laser.kind, laser.name, laser.mode, laser.scripted, laser.problem) == ("rmeg", "Laser Tag", "Slayer", True, "")
    assert (modes["Game Modes/no header.rmeg"].name, modes["Game Modes/no header.rmeg"].mode) == ("no header", "Slayer")
    assert modes["Game Modes/MCC/not blf.bin"].problem and not modes["Game Modes/MCC/team_snipers.bin"].problem
    assert "byte-order mark" in modes["Game Modes/bom.json"].problem
    assert "laser tag" in laser.references and "game modes/laser.rmeg" in laser.references
    # G's custom games list and `variant list` show them all (a .json that isn't a game type isn't listed)
    presets = {p.relative: p for p in go.list_presets(content)}
    assert presets["Game Modes/laser.rmeg"].mode_label == "Slayer · scripted mode (.rmeg)"
    assert presets["Game Modes/MCC/team_snipers.bin"].mode_label == "MCC game variant (.bin)"
    assert "Game Modes/bom.json" not in presets


def test_a_rmeg_is_shown_from_its_header_and_checked_as_it_is(tmp_path: Path) -> None:
    content = tmp_path / "content"
    path = write(content / "Game Modes" / "laser.rmeg", RMEG)
    preset = go.load_preset(path)
    assert (preset.kind, preset.name, preset.scripted) == ("rmeg", "Laser Tag", True)
    assert go.describe(preset.options, preset.rules, preset.world) == ["rounds.limit: 2"]
    (tmp_path / "check").mkdir()
    copy = preset.check_copy(tmp_path / "check")
    assert copy.read_text(encoding="utf-8") == RMEG  # mode-check reads it as it is
    binary = go.load_preset(write(content / "Game Modes" / "x.bin", b"_blf"))
    try:
        binary.check_copy(tmp_path / "check")
    except go.PresetError as error:
        assert "mode-check doesn't read those" in str(error)
    else:
        raise AssertionError("a .bin was given to mode-check")


def test_mode_check_is_read_by_its_words_not_its_exit_code() -> None:
    """0.10.8's mode-check answers 0 for good and bad files alike (gamemodes-layouts-format.md 4.4)."""
    from types import SimpleNamespace as Said

    assert go.mode_check_problem(Said(ok=True, text="Valid custom mode: Growth (script version 2)", problem="")) is None
    assert go.mode_check_problem(Said(ok=True, text="[modes] x.json: read without zz, which this version does not "
                                                    "know\nPreset has no custom game script", problem="")) is None
    assert go.mode_check_problem(Said(ok=True, text="6:1: Unterminated block comment", problem="")) == \
        "6:1: Unterminated block comment"
    assert go.mode_check_problem(Said(ok=False, text="Error: Preset has no custom game script",
                                      problem="Error: Preset has no custom game script")) is not None


def _layout(standing: int, frozen: int) -> dict:
    data = bytearray(0xE0A0)
    for number in range(standing):
        struct.pack_into("<H", data, 0x130 + 0x54 * number, 0x1)
    struct.pack_into("<H", data, 0x130 + 0x54 * 639, 0x21)  # a deleted object doesn't count
    listed = b"FRZ1" + struct.pack("<IH", frozen, 0)
    return {"version": 2, "name": "Big", "mode": "Forge", "rules": {},
            "base_map": {"id": 31, "stem": "hugegrass_spr", "title": "HugeGrass SPR"},
            "map": {"name": "Big", "engine_build": "x", "data": base64.b64encode(bytes(data)).decode(),
                    "frozen": base64.b64encode(listed).decode()}}


def test_a_layouts_objects_are_counted_as_the_program_counts_them(tmp_path: Path) -> None:
    assert layout_objects(_layout(600, 9500)) == 10100 > OBJECTS_MOST
    assert layout_objects(_layout(10, 0)) == 10
    assert layout_objects({"map": {"data": "AAAA"}}) is None  # too short to read: the program says why
    write(tmp_path / "content" / "Maps" / "SPR" / "big.json", json.dumps(_layout(600, 9500)))
    write(tmp_path / "content" / "Maps" / "old.mvar", b"_blf")
    layouts = {l.relative: l for l in list_layouts(tmp_path / "content")}
    big = layouts["Maps/SPR/big.json"]
    assert (big.name, big.base_stem, big.base_id, big.objects) == ("Big", "hugegrass_spr", 31, 10100)
    assert layouts["Maps/old.mvar"].kind == "mvar"
