"""Game options: the catalogue, saved custom games (presets), a playlist game's own rules and options, and the
screens that change them without typing a name or a number from memory."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from test_menu import FakeTerminal
from conftest import free_port
from test_setup import FAKE, TEMPLATE_COMMAND, SetupController, StrictKeyboard, make_server_folder, setup_root

from huragok import gameoptions as go
from huragok.catalogue import Catalogue, Entry
from huragok.config import load
from huragok.dedicated import Dedicated, tool_for
from huragok.menu import Menu
from huragok.playlists import Playlist, load_playlist
from huragok.tui import RecentLog

PROBES = Path(__file__).parent / "data" / "game-options-probe.txt"


def plain(terminal: FakeTerminal) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "".join(terminal.output))


def menu_on(controller: Any, keys: list[str]) -> tuple[Menu, FakeTerminal]:
    terminal = FakeTerminal()
    return Menu(controller, terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


# The catalogue ------------------------------------------------------------------------------------

def test_the_catalogue_has_every_option_the_program_named() -> None:
    assert len(go.SHARED_OPTIONS) == 25 and len(go.trait_options("player")) == 24
    weapons = go.all_options("Slayer")["player.primary_weapon"]
    assert weapons.label_of(0) == "Battle rifle" and weapons.value_of("battle RIFLE") == 0 and weapons.value_of("-3") == -3
    with pytest.raises(ValueError):
        weapons.value_of("Lightsaber")
    minutes = go.all_options("Slayer")["rounds.minutes"]
    assert minutes.value_of("12") == 12
    with pytest.raises(ValueError):
        minutes.value_of("61")
    assert [k for k, _ in go.trait_sets("CaptureTheFlag")][-1] == "flag_carrier"
    assert "primary_weapon" not in {o.key.split(".")[1] for o in go.trait_options("red_powerup")}  # no loadout there
    titles = [title for title, _ in go.categories("KingOfTheHill")]
    assert titles == ["King of the Hill rules", "Rounds & teams", "Respawn", "Powerup durations", "Map & equipment",
                      "World"]
    rules = {o.key for o in go.mode_rules("Slayer")}
    assert "rules.kill_points" in rules and "mode.kill" not in rules and "mode.headshot" in rules  # no twin rows
    assert go.mode_id("capture the flag") == "CaptureTheFlag" and go.mode_name("Vip") == "VIP"
    assert go.describe("Slayer", {"map.weapons": 11, "player.ammo": 2, "bogus.key": 1}, {"score_to_win": 50}) == [
        "Score to win (0 = none, -1 = unlimited): 50", "Weapon set: No weapons", "Ammunition: Infinite",
        "bogus.key: 1 (not an option of Slayer)"]
    # Every range the program gave is the catalogue's (the probe's output is test data: it must be there).
    assert PROBES.is_file(), f"{PROBES} is missing: it holds what the program said when its options were probed"
    text = PROBES.read_text(encoding="utf-8")
    for option in go.SHARED_OPTIONS:
        assert re.search(re.escape(option.key) + r"\s+Error: " + re.escape(option.label) +
                         rf" must be {option.low}\.\.={option.high}", text), option.key


# Presets --------------------------------------------------------------------------------------------

def test_a_preset_is_saved_whole_checked_and_never_written_over(tmp_path: Path) -> None:
    content = tmp_path / "content"
    preset = go.new_preset("BR Team Slayer 75", "Slayer")
    options = go.all_options("Slayer")
    preset.set(options["player.primary_weapon"], "Battle rifle")
    preset.set(options["map.weapons"], 9)
    preset.set(go.RULES[0], 75)
    preset.set(go.WORLD[0], "stamina")
    path = preset.save(go.preset_path(content, preset.name))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "name": "BR Team Slayer 75", "mode": "Slayer", "rules": {"score_to_win": 75},
                    "options": {"player.primary_weapon": 0, "map.weapons": 9}, "world": {"sprint": "stamina"}}
    assert go.preset_path(content, preset.name).name == "br-team-slayer-75-2.json"  # the first stays
    linked = tmp_path / "other-server-copy.json"
    os.link(path, linked)  # a hard-linked content folder elsewhere
    preset.set(options["map.weapons"], 11)
    preset.save()
    assert json.loads(linked.read_text(encoding="utf-8"))["options"]["map.weapons"] == 9  # its copy is untouched
    listed = go.list_presets(content)
    assert [(p.name, p.mode_label, p.relative) for p in listed] == [("BR Team Slayer 75", "Slayer",
                                                                     "Game Modes/br-team-slayer-75.json")]
    copy = go.clone(go.load_preset(path), "BR copy")
    assert copy.path is None and copy.options == {"player.primary_weapon": 0, "map.weapons": 11}
    for name, error in (("", "needs a name"), ("Slayer", "game type's own name"), ("x" * 49, "48 at most")):
        with pytest.raises(go.PresetError, match=error):
            go.new_preset(name, "Slayer").save(tmp_path / "x.json")
    bad = go.new_preset("Odd", "Slayer")
    bad.options["mode.flag_type"] = 1
    assert bad.problems() == ["mode.flag_type isn't an option of Slayer"]
    tool = Dedicated(FAKE, template=TEMPLATE_COMMAND)
    assert tool.mode_check(preset.check_copy(tmp_path)).ok  # checked as a copy with an empty script
    preset.options["player.speed"] = 99
    result = tool.mode_check(preset.check_copy(tmp_path))
    assert not result.ok and "Movement speed must be 0..=10" in result.text
    with pytest.raises(go.PresetError):
        go.load_preset(tmp_path / "missing.json")


# A playlist game's overrides ----------------------------------------------------------------------

def test_a_playlist_game_keeps_labels_and_drops_empty_blocks() -> None:
    game: dict[str, Any] = {"map": "Valhalla", "game": "Slayer"}
    options = go.all_options("Slayer")
    go.entry_set(game, options["player.primary_weapon"], 0)
    go.entry_set(game, options["rounds.minutes"], 12)
    go.entry_set(game, go.RULES[0], 50)
    go.entry_set(game, go.WORLD[0], "unlimited")
    assert game == {"map": "Valhalla", "game": "Slayer", "rules": {"score_to_win": 50},
                    "options": {"player.primary_weapon": "Battle rifle", "rounds.minutes": 12},
                    "world": {"sprint": "unlimited"}}
    assert go.entry_get(game, options["player.primary_weapon"]) == "Battle rifle"
    for option in (options["player.primary_weapon"], options["rounds.minutes"], go.RULES[0], go.WORLD[0]):
        go.entry_set(game, option, None)
    assert game == {"map": "Valhalla", "game": "Slayer"}
    assert go.entry_mode({"game": "2V2 HARDCORE TS"}, {"2v2 hardcore ts": "Slayer"}) == "Slayer"
    assert go.entry_mode({"game": "Capture the Flag"}) == "CaptureTheFlag" and go.entry_mode({"game": "?"}) is None


# The screens -----------------------------------------------------------------------------------------

CATALOGUE = Catalogue(
    maps=[Entry("Guardian", "Guardian", "map"), Entry("Valhalla", "Valhalla", "map")],
    game_types=[Entry("Slayer", "Slayer", "game type"), Entry("Oddball", "Oddball", "game type")],
    game_variants=[Entry("2V2 HARDCORE TS", "2V2 HARDCORE TS", "game variant", base="Slayer")],
    mod_maps=[Entry("HugeGrass", "workshop:2984061723/hugeass_remastered_mcc", "mod map"),
              Entry("HugeGrass Rebalance", "workshop:3820687243/fixedHugeGrassTrapasRemix", "mod map")])


def test_one_playlist_game_is_changed_from_numbered_lists(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    images = server.content_dir / "Images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "vote.png").write_bytes(b"\x89PNG")
    playlist = Playlist(tmp_path / "p.playlist.json", "P", 20, [{"map": "Guardian", "game": "Slayer"},
                                                               {"map": "Valhalla", "game": "Oddball"},
                                                               {"map": "Valhalla", "game": "2V2 HARDCORE TS"}])
    keys = ["N", *"BR Starts", "ENTER",
            "O", "5", "ENTER", "1", "ENTER", *"10", "ENTER", "ESC",  # Map & equipment -> Weapon set -> No power weapons
            "7", "ENTER", "1", "ENTER", "6", "ENTER", "4", "ENTER", "ESC", "ESC",  # Traits -> player -> primary: BR
            "1", "ENTER", "1", "ENTER", *"50", "ENTER", "ESC", "ESC",  # Slayer rules -> score to win 50
            "I", "1", "ENTER",  # vote image
            "C", "Y",  # anti-cheat off for this game
            "W", "3", "ENTER",
            "ESC"]
    menu, terminal = menu_on(controller, keys)
    assert run(menu._edit_entry(server, CATALOGUE, playlist, 0))
    assert playlist.games[0] == {"map": "Guardian", "game": "Slayer", "name": "BR Starts",
                                 "options": {"map.weapons": "No power weapons", "player.primary_weapon": "Battle rifle"},
                                 "rules": {"score_to_win": 50}, "preview_image": "vote.png", "anti_cheat": False,
                                 "weight": 3}
    text = plain(terminal)
    assert "Weapon set: No power weapons" in text and "Starting primary weapon: Battle rifle" in text
    # A variant's game type comes from the catalogue: its Slayer options are offered.
    keys = ["O", "1", "ENTER", "ESC", "ESC", "X", "Y", "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu._edit_entry(server, CATALOGUE, playlist, 2))
    assert "Slayer rules" in plain(terminal) and playlist.games[2] == {"map": "Valhalla", "game": "2V2 HARDCORE TS"}


def test_custom_games_are_made_checked_and_played(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    settings = load(settings_path)
    server = settings.servers[0]
    tool = tool_for(settings)
    keys = ["N", "2", *"One Flag Rush", "ENTER",  # from Capture the Flag
            "1", "ENTER", "1", "ENTER", *"3", "ENTER", "ESC", "ESC",  # its rules: score to win 3
            "Y", "x",  # save: checked by the program, saved
            "S", "1", "ENTER", "x",  # show what it changes
            "P", "1", "ENTER",  # play it
            "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu._custom_games(settings, tool, server))
    text = plain(terminal)
    assert "The server program checked it: valid." in text and "Score to win (0 = none, -1 = unlimited): 3" in text
    path = server.content_dir / "Game Modes" / "one-flag-rush.json"
    assert json.loads(path.read_text(encoding="utf-8"))["mode"] == "CaptureTheFlag"
    from huragok.tomledit import read_toml

    block = read_toml(server.config_path)["server"][0]
    assert block["variant"].endswith("Game Modes/one-flag-rush.json") and "playlist" not in block


def test_a_playlist_from_a_mods_maps(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    settings = load(settings_path)
    server = settings.servers[0]

    async def no_check(self: Any, tool: Any, server: Any, playlist: Any) -> bool:
        playlist.save()
        return True

    monkeypatch.setattr(Menu, "_save_playlist", no_check)
    keys = ["ENTER", "ENTER",  # name (the first mod map's), vote seconds
            "A", "ENTER", "A", "ENTER", "ENTER", "ENTER"]  # every mod map × every game type, keep all, no weights
    menu, terminal = menu_on(controller, keys)
    run(menu._build_playlist(settings, tool_for(settings), server, CATALOGUE, maps=CATALOGUE.mod_maps))
    playlist = load_playlist(server.folder / "playlists" / "hugegrass.playlist.json")
    assert {g["map"] for g in playlist.games} == {"workshop:2984061723/hugeass_remastered_mcc",
                                                  "workshop:3820687243/fixedHugeGrassTrapasRemix"}
    assert len(playlist.games) == 6  # 2 mod maps × (2 game types + 1 variant)
    assert "workshop:<item>/<map>" in plain(terminal)


def test_custom_games_from_the_command_line(tmp_path: Path, capsys) -> None:
    from huragok import cli

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    preset = go.new_preset("Snipers Only", "Slayer")
    preset.set(go.all_options("Slayer")["map.weapons"], 7)
    preset.save(go.preset_path(server.content_dir, preset.name))
    assert cli.main(["-c", str(settings_path), "variant"]) == 0
    assert cli.main(["-c", str(settings_path), "variant", "show", "snipers only"]) == 0
    assert cli.main(["-c", str(settings_path), "variant", "check", "Snipers Only"]) == 0
    text = capsys.readouterr().out
    assert "Snipers Only" in text and "Weapon set: Sniper rifles" in text and "valid" in text
    assert cli.main(["-c", str(settings_path), "variant", "show"]) == 2
    assert cli.main(["-c", str(settings_path), "variant", "show", "nothing"]) == 1
    broken = json.loads(preset.path.read_text(encoding="utf-8"))
    broken["options"]["player.speed"] = 99
    preset.path.write_text(json.dumps(broken), encoding="utf-8")
    assert cli.main(["-c", str(settings_path), "variant", "check", "Snipers Only"]) == 1


# Labels that differ by program version, and values D checks before writing -------------------------------

def test_a_choice_whose_label_depends_on_the_version_is_written_as_its_number() -> None:
    """0.9.18 refuses "Halo 3" and 0.9.19 on refuse "Vanilla characters"; every version takes 1 (0.9.18 checked)."""
    options = go.all_options("Slayer")
    game: dict[str, Any] = {"map": "Guardian", "game": "Slayer"}
    go.entry_set(game, options["player.character_set"], "Halo 3")
    go.entry_set(game, options["teams.red_character_set"], "vanilla characters")
    go.entry_set(game, options["player.grenade_regeneration"], 3)
    go.entry_set(game, options["player.primary_weapon"], "Battle rifle")
    assert game["options"] == {"player.character_set": 1, "teams.red_character_set": 1,
                               "player.grenade_regeneration": "On, with firebombs",
                               "player.primary_weapon": "Battle rifle"}
    go.entry_set(game, options["player.character_set"], "Random")  # the same in every version: a label
    assert game["options"]["player.character_set"] == "Random"
    assert go.describe("Slayer", {"player.character_set": 1}) == ["Character set: Halo 3"]


def test_d_refuses_values_the_program_would_refuse(tmp_path: Path) -> None:
    from huragok import livesettings as ls
    from huragok.tomledit import set_server_setting, set_top_key

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
    ls.write_plays(config, name, {"campaign": {"mission": 3010, "difficulty": "normal"}})
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
    from huragok import livesettings as ls

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    camera = ls.find("camera")
    assert ls.check_value(camera, "third_person", server.config_path, server.name, settings, "0.9.18") == \
        "camera needs Project Reclaimer 0.9.20 or newer (this server has 0.9.18)"
    assert ls.check_value(camera, "third_person", server.config_path, server.name, settings, "0.9.20") is None
    assert ls.check_value(camera, "third_person", server.config_path, server.name, settings, None) is None
    assert "needs Project Reclaimer 0.9.18" in ls.check_value(ls.find("customization"), ["Pack"], server.config_path,
                                                              server.name, settings, "0.9.15")
    assert ls.plays_problem({"odst": {"mode": "firefight"}}, "0.9.18") == \
        "odst needs Project Reclaimer 0.9.19 or newer (this server has 0.9.18)"
    assert "sprint needs Project Reclaimer 0.9.19" in ls.plays_problem(
        {"campaign": {"mission": 3010, "modifiers": {"sprint": True}}}, "0.9.18")
    assert ls.plays_problem({"campaign": {"mission": 3010, "modifiers": {"third_person": True}}}, "0.9.18") is None
    assert set(ls.MISSION_STARTS) == set(ls.MISSIONS)  # D's rally point is bounded for every mission
