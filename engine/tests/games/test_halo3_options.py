"""Saved custom games (presets) as Huragok reads them: listed, shown as they're written, checked by the server's own
program as a copy, from the window and the command line; and the values D checks before writing a setting.
Huragok no longer makes or edits custom games or game options (0.7.0)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port
from testkit.setup import FAKE, TEMPLATE_COMMAND, make_server_folder, setup_root, write_plays

from huragok.core.settings import load
from huragok.games import halo3_options as go
from huragok.program.dedicated import Dedicated


def write_preset(content: Path, stem: str, data: dict[str, Any]) -> Path:
    """A saved custom game as a host makes one (by hand, or MCC's imported): content/Game Modes/<stem>.json."""
    path = content / "Game Modes" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# Presets --------------------------------------------------------------------------------------------

def test_a_preset_is_read_listed_and_checked_as_a_copy(tmp_path: Path) -> None:
    content = tmp_path / "content"
    path = write_preset(content, "br-team-slayer-75", {
        "version": 1, "name": "BR Team Slayer 75", "mode": "Slayer", "rules": {"score_to_win": 75},
        "options": {"player.character_set": "Halo 3", "map.weapons": 9,
                    "respawn.seconds": {"points": [[2, 3], [16, 5]]}}, "world": {"sprint": "stamina"}})
    text = path.read_text(encoding="utf-8")
    listed = go.list_presets(content)
    assert [(p.name, p.mode_label, p.relative) for p in listed] == [("BR Team Slayer 75", "Slayer",
                                                                     "Game Modes/br-team-slayer-75.json")]
    preset = go.load_preset(path)
    assert (preset.name, preset.mode, preset.scripted) == ("BR Team Slayer 75", "Slayer", False)
    # What it changes, as the file writes it: no catalogue of Huragok's own to get out of date.
    assert go.describe(preset.options, preset.rules, preset.world) == [
        "rules.score_to_win: 75", "player.character_set: Halo 3", "map.weapons: 9",
        'respawn.seconds: {"points": [[2, 3], [16, 5]]}', "world.sprint: stamina"]
    assert go.mode_name("Vip") == "VIP" and go.mode_name("Mystery") == "Mystery"
    tool = Dedicated(FAKE, template=TEMPLATE_COMMAND)
    check = tmp_path / "check"
    check.mkdir()
    assert tool.mode_check(preset.check_copy(check)).ok  # checked as a copy with an empty script
    preset.data["options"]["player.speed"] = 99
    result = tool.mode_check(preset.check_copy(check))
    assert not result.ok and "Movement speed must be 0..=10" in result.text
    assert path.read_text(encoding="utf-8") == text  # the host's file is never written
    with pytest.raises(go.PresetError):
        go.load_preset(tmp_path / "missing.json")
    (content / "Game Modes" / "not-a-game.json").write_text('{"name": "a saved map"}', encoding="utf-8")
    with pytest.raises(go.PresetError, match="no mode"):
        go.load_preset(content / "Game Modes" / "not-a-game.json")
    assert len(go.list_presets(content)) == 1  # a .json that isn't a custom game isn't listed


def test_only_the_read_parts_of_the_option_editor_are_left() -> None:
    """The option editor and its catalogue went in 0.7.0; the labels the program renamed stay, for `check`'s
    warnings, and version_tuple lives in core/text.py (still importable here)."""
    from huragok.core.text import version_tuple

    for gone in ("Option", "all_options", "categories", "trait_options", "new_preset", "preset_path", "clone",
                 "choices_as_numbers", "entry_set", "entry_get", "entry_mode"):
        assert not hasattr(go, gone), gone
    assert not hasattr(go.Preset, "save") and not hasattr(go.Preset, "set")
    assert go.version_tuple is version_tuple and version_tuple("v0.9.20-beta") == (0, 9, 20)
    assert version_tuple("x") is None and version_tuple(None) is None
    character_set = next(change for change in go.LABEL_CHANGES if change.option == "character_set")
    assert character_set.taken("0.9.18") == "Vanilla characters" and character_set.taken("0.9.19") == "Halo 3"
    assert character_set.taken(None) is None and character_set.applies_to("teams.red_character_set")
    with pytest.raises(ModuleNotFoundError):
        __import__("huragok.games.halo3_options_data")


def test_custom_games_from_the_command_line(tmp_path: Path, capsys) -> None:
    from huragok.commandline import cli

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    path = write_preset(server.content_dir, "snipers-only", {"version": 1, "name": "Snipers Only", "mode": "Slayer",
                                                             "rules": {}, "options": {"map.weapons": 7}})
    assert cli.main(["-c", str(settings_path), "variant"]) == 0
    assert cli.main(["-c", str(settings_path), "variant", "show", "snipers only"]) == 0
    assert cli.main(["-c", str(settings_path), "variant", "check", "Snipers Only"]) == 0
    text = capsys.readouterr().out
    assert "Snipers Only" in text and "map.weapons: 7" in text and "valid" in text
    assert cli.main(["-c", str(settings_path), "variant", "show"]) == 2
    assert cli.main(["-c", str(settings_path), "variant", "show", "nothing"]) == 1
    broken = json.loads(path.read_text(encoding="utf-8"))
    broken["options"]["player.speed"] = 99
    path.write_text(json.dumps(broken), encoding="utf-8")
    assert cli.main(["-c", str(settings_path), "variant", "check", "Snipers Only"]) == 1
    assert "Snipers Only: Error: Movement speed must be 0..=10" in capsys.readouterr().err  # the program's own words
    path.unlink()
    assert cli.main(["-c", str(settings_path), "variant"]) == 0
    assert "custom games are made by hand (or imported from MCC" in " ".join(capsys.readouterr().out.split())


# Values D checks before writing a setting -----------------------------------------------------------

def test_d_refuses_values_the_program_would_refuse(tmp_path: Path) -> None:
    from huragok.program import settings as ls
    from huragok.core.tomledit import set_server_setting, set_top_key

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    config, name = server.config_path, server.name

    def problem(key: str, scope: str, value: Any, version: str | None = None) -> str | None:
        return ls.check_value(ls.find(key, scope), value, config, name, settings, version)

    set_server_setting(config, name, "max_players", 8)
    assert "min_players (10) can't be more than max_players (8)" in problem("min_players", "server", 10)
    set_server_setting(config, name, "min_players", 6)
    assert "can't be more than max_players (4)" in problem("max_players", "server", 4)
    assert problem("max_players", "server", 24) is None
    write_plays(config, name, {"campaign": {"mission": 3010, "difficulty": "normal"}})
    assert "holds 16 players at most" in problem("max_players", "server", 24)
    assert "48 at most" in problem("host_name", "server", "x" * 49) and problem("host_name", "folder", "x" * 48) is None
    assert "http:// or https://" in problem("stats_server", "folder", "garbage")
    assert problem("stats_server", "folder", "off") is None and problem("stats_server", "folder", "") is None
    assert "IPv4 address or a DNS name" in problem("public_address", "folder", "not an address!")
    assert problem("public_address", "folder", "play.example.org") is None
    assert problem("public_address", "folder", "203.0.113.7") is None
    assert "[master] origin" in problem("origin", "master", "garbage")
    assert "must be an IP address" in problem("address", "rcon", "localhost:1")
    assert problem("address", "rcon", "0.0.0.0") is None
    assert "discovery master's port" in problem("port", "server", 49175)
    set_top_key(config, "host_name", "x")  # still readable after the writes above
    other = free_port()
    make_server_folder(tmp_path / "reclaimer" / "SERVERS" / "Bravo", "Bravo", other)
    set_server_setting(tmp_path / "reclaimer" / "SERVERS" / "Bravo" / "dedicated.toml", "Bravo", "enabled", False)
    assert f"uses port {other}" in problem("port", "server", other)  # a disabled server in another folder too
    assert problem("port", "server", server.game_port) is None  # its own


def test_d_names_the_version_a_setting_needs(tmp_path: Path) -> None:
    from huragok.program import plays
    from huragok.program import settings as ls

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    camera = ls.find("camera")
    assert ls.check_value(camera, "third_person", server.config_path, server.name, settings, "0.9.18") == \
        "camera needs Project Reclaimer 0.9.20 or newer (this server has 0.9.18)"
    assert ls.check_value(camera, "third_person", server.config_path, server.name, settings, "0.9.20") is None
    assert ls.check_value(camera, "third_person", server.config_path, server.name, settings, None) is None
    assert "needs Project Reclaimer 0.9.18" in ls.check_value(ls.find("customization"), ["Pack"], server.config_path,
                                                              server.name, settings, "0.9.15")
    assert plays.plays_problem({"odst": {"mode": "firefight"}}, "0.9.18") == \
        "odst needs Project Reclaimer 0.9.19 or newer (this server has 0.9.18)"
    assert "sprint needs Project Reclaimer 0.9.19" in plays.plays_problem(
        {"campaign": {"mission": 3010, "modifiers": {"sprint": True}}}, "0.9.18")
    assert plays.plays_problem({"campaign": {"mission": 3010, "modifiers": {"third_person": True}}}, "0.9.18") is None
    from huragok.games import halo3

    assert set(halo3.MISSION_STARTS) == set(halo3.MISSIONS)


def test_a_custom_game_a_server_plays_by_name_counts_as_played(tmp_path: Path) -> None:
    """`game = "BR Slayer"` on a map of its own counts as playing it, as `variant` and a playlist game do: what the
    custom games screen warns of before setting one aside."""
    from huragok.window.screens.games import servers_playing

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    write_preset(server.content_dir, "br-slayer", {"version": 1, "name": "BR Slayer", "mode": "Slayer", "rules": {},
                                                   "options": {}})
    info = go.list_presets(server.content_dir)[0]
    assert servers_playing(settings, info) == []
    write_plays(server.config_path, server.name, {"map": "Valhalla", "game": "BR Slayer"})
    assert servers_playing(settings, info) == [server.name]
    # By the reference the server's own content listing gives (its file without the extension): counted too.
    write_plays(server.config_path, server.name, {"map": "Valhalla", "game": "br-slayer"})
    assert servers_playing(settings, info) == [server.name]


def test_a_playlist_weight_that_isnt_a_whole_number_is_shown_as_written() -> None:
    """int() of "two" (or 1.5 cut to 1) broke every playlist screen (audit 2026-11, 7.9)."""
    from huragok.games.halo3 import Playlist

    playlist = Playlist(Path("x.playlist.json"), "x", games=[
        {"game": "Slayer", "map": "Guardian", "weight": "two"}, {"game": "CTF", "map": "Valhalla", "weight": 1.5},
        {"game": "KOTH", "map": "Narrows"}, {"game": "Oddball", "map": "Pit", "weight": 3}])
    labels = playlist.labels()
    assert "weight two" in labels[0] and "weight 1.5" in labels[1] and "weight" not in labels[2] and "weight 3" in labels[3]
