"""Project Reclaimer 0.10: what a server plays, for every game. Each game's maps in the pickers (only on a program that
plays them), each map offered the game types it plays, Combat Evolved's and Halo 2's games left alone by Halo 3's
option catalogue, playlists of 1 to 256 games, the dashboard naming the game, the 0.10 settings in D (and not on 0.9),
and `dedicated check`'s 0.10 refusals read game by game (their text as 0.10.0 printed it on this PC, 2026-11-06)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from testkit.setup import ALPHA, SetupController, StrictKeyboard, setup_root
from testkit.window import FakeTerminal

from huragok.content.catalogue import Catalogue, Entry, parse_listing
from huragok.core.logs import RecentLog
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_server_setting
from huragok.games import halo3_options as go
from huragok.games.bungie import GAMES
from huragok.games.halo3 import Playlist, entry_game, games_range, own_rules_only, problems_from_show, rename_old_labels
from huragok.games.plays import PLAYS_KEYS, games_in, plays_kind, plays_of, takes
from huragok.program import settings as ps
from huragok.program.dedicated import ToolResult
from huragok.program.plays import _refusals, check_plays, plays_problem
from huragok.window.dashboard import Menu
from huragok.window.screens.base import fits, game_sections, map_sections

# Trimmed from 0.10.0's `dedicated content` on this PC: each game's section, a CE mod map in CE's, the other games'.
LISTING = '''[reach] Halo: Reach installed in H:/MCC
[halo1] Halo: Combat Evolved installed in H:/MCC
[halo2] Halo 2 installed in H:/MCC
Maps (map = "..."):
  Guardian                 guardian (ID 320)
  Valhalla                 riverworld (ID 340)
Halo: Reach's maps (map = "..."):
  Beachhead                ff50_park (ID 7060)
  Sword Base               20_sword_slayer (ID 1000)
Halo: Combat Evolved's maps (map = "..."):
  Blood Gulch              halo1_bloodgulch (ID 1224137234)
  Booglooch                halo1_bloodgulch_6381b55e (ID 1208457912)
Halo 2's maps (map = "..."):
  Lockout                  halo2_lockout (ID 853868150)
Other games' mods' maps and campaign canvases (map = "..."):
  Scarif · The Battlefront Pack - Reach scarif_b72b9298
Game types (game = "..."):
  Slayer                   slayer
  Capture the Flag         ctf
Map variants and Forge saves (map = "..."): 595 (name words to list them)
Game variants and game modes (game = "..."): 758 (name words to list them)'''


def catalogue_010() -> Catalogue:
    catalogue = parse_listing(LISTING)
    catalogue.game_variants += [
        Entry("HARDCORE TS", "HARDCORE TS", "game variant", "Halo 3", "Slayer", game="halo3"),
        Entry("Headhunter", "Headhunter", "game variant", "Halo: Reach", "Slayer", game="reach")]
    return catalogue


def plain(terminal: FakeTerminal) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "".join(terminal.output))


def menu_on(controller: Any, keys: list[str]) -> tuple[Menu, FakeTerminal]:
    terminal = FakeTerminal()
    return Menu(controller, terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


def names(entries: list[Entry]) -> list[str]:
    return [e.name for e in entries]


# Playlists: CE's and Halo 2's games left as written, 1 to 256 games ------------------------------------------------

def test_ce_and_halo_2_games_are_left_as_they_are_written() -> None:
    assert entry_game({"map": "Blood Gulch"}).key == "halo1" and entry_game({"map": "halo2_lockout"}).key == "halo2"
    assert entry_game({"map": "halo1_bloodgulch_6381b55e"}).key == "halo1"  # a CE mod map, by MCC's file name
    assert entry_game({"map": "Lockout"}).key == "halo3"  # Halo 3's Blackout by its file name, as the program has it
    assert entry_game({"map": "workshop:1/hugegrass"}) is None and not own_rules_only({"map": "Sword Base"})
    playlist = Playlist(Path("x.playlist.json"), "Every Halo", 20, [
        {"map": "Guardian", "game": "Slayer", "options": {"player.primary_weapon": "Battle rifle"}},
        {"map": "Blood Gulch", "game": "Slayer", "options": {"player.primary_weapon": "Battle rifle"}},
        {"map": "Sword Base", "game": "Slayer", "name": "Reach"}])
    playlist.number_choices()
    assert playlist.games[0]["options"] == {"player.primary_weapon": 0}  # Halo 3's: its number
    assert playlist.games[1]["options"] == {"player.primary_weapon": "Battle rifle"}  # CE's: untouched
    assert playlist.duplicates() == {}
    labels = playlist.labels()
    assert labels == ["Slayer on Guardian · options", "Slayer on Blood Gulch · CE · options",
                      "Reach on Sword Base (Slayer) · Reach"]


def test_old_labels_are_renamed_only_in_halo_3s_games(tmp_path: Path) -> None:
    path = tmp_path / "x.playlist.json"
    path.write_text(json.dumps({"version": 2, "name": "X", "games": [
        {"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Vanilla characters"}},
        {"map": "halo2_lockout", "game": "Slayer", "options": {"player.character_set": "Vanilla characters"}}]}),
        encoding="utf-8")
    assert rename_old_labels(path, "0.10.0") == [("Vanilla characters", "Halo 3")]
    games = json.loads(path.read_text(encoding="utf-8"))["games"]
    assert games[0]["options"]["player.character_set"] == "Halo 3"
    assert games[1]["options"]["player.character_set"] == "Vanilla characters"


def test_a_playlist_holds_1_to_256_games_from_0_10_and_3_to_128_before() -> None:
    assert games_range("0.9.23") == (3, 128) and games_range("0.10.0") == (1, 256) and games_range(None) == (3, 256)
    two = Playlist(Path("x.playlist.json"), "Two", 20, [{"map": "Guardian", "game": "Slayer"},
                                                        {"map": "Blood Gulch", "game": "Slayer"}])
    assert two.problems("0.10.0") == []  # 0.10.0's check took a 2-game playlist; not known: 3, as ever
    assert two.problems() == ["a playlist needs 3 or more games (this one has 2)"]
    assert two.problems("0.9.23") == ["a playlist needs 3 or more games (this one has 2)"]
    big = Playlist(Path("x.playlist.json"), "Big", 20, [{"map": "Guardian", "game": f"Slayer {n}"} for n in range(200)])
    assert big.problems("0.10.0") == []
    assert "at most 128 games before Project Reclaimer 0.10.0" in big.problems("0.9.23")[0]


def test_playlist_show_s_0_10_line_is_read_game_by_game() -> None:
    shown = ('playlists/typo.playlist.json: game 2: no multiplayer map "Atlantiss" is installed; as a variant: Map '
             "'Atlantiss' was not found. Did you mean 'S'?; game 6: Guardian Slayer is listed twice (raise its weight "
             "instead)")
    found = problems_from_show(shown)
    assert sorted(found) == [2, 6] and found[2].startswith('no multiplayer map "Atlantiss" is installed; as a variant')
    assert found[6] == "Guardian Slayer is listed twice (raise its weight instead)"


# What the dashboard says ---------------------------------------------------------------------------------------

def test_the_dashboard_names_the_game_where_it_isnt_halo_3() -> None:
    assert plays_of({"map": "Sword Base", "game": "Slayer"}) == "Slayer on Sword Base (Reach)"
    assert plays_of({"map": "halo2_lockout", "game": "Assault"}) == "Assault on halo2_lockout (Halo 2)"
    assert plays_of({"map": "Guardian", "game": "Slayer"}) == "Slayer on Guardian"
    assert plays_of({"playlist": "playlists/every-halo.playlist.json"}) == "playlist every-halo"
    assert "reach" in PLAYS_KEYS and plays_kind({"reach": {"map": "Beachhead"}}) == "reach"  # replaced by a change
    assert [g.short for g in games_in([{"map": "Blood Gulch"}, {"map": "Guardian"}, {"map": "x"}])] == ["Halo 3", "CE"]


def test_the_status_card_names_a_playlists_games(tmp_path: Path) -> None:
    from huragok.program.plays import write_plays
    from huragok.window import statuscard

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    Playlist(server.folder / "playlists" / "every-halo.playlist.json", "Every Halo", 20, [
        {"map": "Guardian", "game": "Slayer"}, {"map": "Sword Base", "game": "Slayer"},
        {"map": "Blood Gulch", "game": "Slayer"}]).save()
    write_plays(server.config_path, ALPHA, {"playlist": "playlists/every-halo.playlist.json"})
    lines = dict(statuscard.build(settings, server, {"status": "stopped"}))["What it plays"]
    text = " | ".join(f"{label} {said}" for label, said, _ in lines)
    assert "playlist every-halo" in text and '"Every Halo" · 3 games · 20 s votes · Halo 3, Reach, CE' in text


# The pickers: each game's maps, each map's game types -------------------------------------------------------------

def test_each_games_maps_are_offered_only_on_a_program_that_plays_them() -> None:
    catalogue = catalogue_010()
    sections = map_sections(catalogue, "0.10.0")
    assert [name for name, _, _ in sections] == ["Halo 3", "Halo: Reach", "Halo: Combat Evolved", "Halo 2",
                                                 "other games' mods' maps"]
    ce = sections[2]
    assert names(ce[1]) == ["Blood Gulch", "Booglooch"]
    assert ce[2] == [("Halo: Combat Evolved's maps", 1), ("Halo: Combat Evolved's mod maps", 2)]
    assert [name for name, _, _ in map_sections(catalogue, "0.9.23")] == ["Halo 3"]  # a 0.9 program: Halo 3's only
    custom = map_sections(catalogue, None, halo3_types=True)  # a saved custom game: Halo 3's game types
    assert [name for name, _, _ in custom] == ["Halo 3", "Halo: Reach", "other games' mods' maps"]
    before = Catalogue(maps=[Entry("Guardian", "Guardian", "map"), Entry("Forge World", "Forge World", "map")],
                       mod_maps=[Entry("Hugegrass", "workshop:1/hugegrass", "mod map")])
    assert [(n, h) for n, _, h in map_sections(before, "0.9.23")] == [  # before 0.10: as ever, one game
        ("Halo 3", [("Halo 3's maps", 1), ("mod maps", 3)])]


def test_the_map_groups_follow_all_maps(tmp_path: Path) -> None:
    menu, _ = menu_on(SetupController(setup_root(tmp_path)), [])
    groups = menu._map_groups(catalogue_010())
    assert groups == [("Halo 3's maps", 1), ("Halo: Reach's maps", 3), ("Halo: Combat Evolved's maps", 5),
                      ("Halo 2's maps", 6), ("Halo: Combat Evolved's mod maps", 7), ("other games' mods' maps", 8)]


def test_each_map_is_offered_the_game_types_it_plays() -> None:
    catalogue = catalogue_010()
    blood_gulch, sword_base, guardian = (catalogue.find_map(n) for n in ("Blood Gulch", "Sword Base", "Guardian"))
    [(heading, ce)] = game_sections(catalogue, [blood_gulch])
    assert heading == "Halo: Combat Evolved's game types" and names(ce) == list(GAMES["halo1"].game_types)
    assert [e.reference for e in ce] == ["Slayer", "Capture the Flag", "Oddball", "King of the Hill", "Race"]
    reach = game_sections(catalogue, [sword_base])
    assert [(h, names(e)) for h, e in reach] == [
        ("Halo 3's game types (Reach's maps play them too)", ["Slayer", "Capture the Flag"]),
        ("game variants (Reach's play on Reach's maps)", ["Headhunter"])]
    halo3 = game_sections(catalogue, [guardian])
    assert [(h, names(e)) for h, e in halo3] == [("game types", ["Slayer", "Capture the Flag"]),
                                                  ("game variants", ["HARDCORE TS"])]
    headhunter, slayer = catalogue.find_game("Headhunter"), catalogue.find_game("Slayer")
    assert fits(sword_base, headhunter, catalogue) and not fits(guardian, headhunter, catalogue)
    assert fits(sword_base, slayer, catalogue) and not fits(blood_gulch, slayer, catalogue)
    assert fits(catalogue.find_map("Scarif"), headhunter, catalogue)  # its game isn't known: anything
    assert takes(GAMES["halo2"], GAMES["halo2"]) and not takes(GAMES["reach"], GAMES["halo3"], "game variant")


def test_d_offers_a_ce_map_its_own_game_types(tmp_path: Path, run, monkeypatch) -> None:
    async def catalogue(self: Any, tool: Any, settings: Any, server: Any) -> Catalogue:
        return catalogue_010()

    async def version(self: Any, settings: Any, server: Any, row: Any) -> str:
        return "0.10.0"

    async def checked(self: Any, settings: Any, server: Any, values: dict[str, Any]) -> bool:
        return True

    monkeypatch.setattr(Menu, "_catalogue", catalogue)
    monkeypatch.setattr(Menu, "_program_version", version)
    monkeypatch.setattr(Menu, "_plays_checked", checked)
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    keys = ["1", "ENTER",  # Alpha
            "2", "ENTER", "Y", "W",  # What it plays
            "2",  # one map and game type
            "3", "1", "ENTER",  # Combat Evolved's maps: Blood Gulch
            "2", "ENTER",  # its own game types: Capture the Flag
            "x", "ESC", "ESC", "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    text = plain(terminal)
    assert "Their campaigns and Reach's Firefight: not yet" in text
    assert "Halo: Combat Evolved's game types" in text and "Race" in text and "HARDCORE TS" not in text
    assert "now plays Capture the Flag on halo1_bloodgulch (CE)" in text
    block = read_toml(server.config_path)["server"][0]
    assert (block["map"], block["game"]) == ("halo1_bloodgulch", "Capture the Flag") and "playlist" not in block


def test_a_ce_map_is_refused_for_a_0_9_program() -> None:
    problem = plays_problem({"map": "Blood Gulch", "game": "Slayer"}, "0.9.23")
    assert problem == "Blood Gulch is Halo: Combat Evolved's map: it needs Project Reclaimer 0.10.0 or newer (this " \
                      "server has 0.9.23)"
    assert plays_problem({"map": "Blood Gulch", "game": "Slayer"}, "0.10.0") is None
    assert plays_problem({"map": "Guardian", "game": "Slayer"}, "0.9.23") is None
    assert plays_problem({"reach": {"map": "Beachhead"}}, "0.9.23").startswith("reach needs Project Reclaimer 0.10.0")


def test_a_ce_playlist_game_offers_no_halo_3_options(tmp_path: Path, run) -> None:
    controller = SetupController(setup_root(tmp_path))
    server = controller.settings.servers[0]
    playlist = Playlist(server.folder / "playlists" / "x.playlist.json", "X", 20, [
        {"map": "halo1_bloodgulch", "game": "Slayer", "options": {"rounds.minutes": 5}},
        {"map": "Sword Base", "game": "Headhunter"}])
    menu, terminal = menu_on(controller, ["ESC", "ESC"])
    assert not run(menu._edit_entry(server, catalogue_010(), playlist, 0))
    assert not run(menu._edit_entry(server, catalogue_010(), playlist, 1))
    text = plain(terminal)
    assert "Rules and options: not on Halo: Combat Evolved's maps" in text and "options.rounds.minutes: 5" in text
    assert "not for a Reach variant here" in text and "[O] Rules and options" not in text


def test_custom_games_offer_however_many_game_types_there_are(tmp_path: Path, run, monkeypatch) -> None:
    monkeypatch.setattr(go, "GAME_TYPES", [*go.GAME_TYPES, ("Eleventh Type", "Slayer")])
    controller = SetupController(setup_root(tmp_path))
    settings = controller.settings
    server = settings.servers[0]
    menu, terminal = menu_on(controller, ["N", "B", "ESC", "ESC"])  # the 11th, by B; its name not given; back
    run(menu._custom_games(settings, None, server))
    text = plain(terminal)
    assert "[0] Escalation Slayer" in text and "[B] Eleventh Type" in text and "[Eleventh Type custom]" in text


# The settings 0.10 adds ---------------------------------------------------------------------------------------

def test_the_0_10_settings_are_offered_only_from_0_10() -> None:
    new = [s for s in ps.SETTINGS if s.since == "0.10.0"]
    keys = {s.key for s in new}
    assert {"tick_rate", "post_game_seconds", "start_countdown_seconds", "stock_map_defaults", "allow_bots",
            "queue_mode", "joins", "kick_blocks", "voice_quality", "network_model", "coop_network", "reach_objects",
            "halo1_objects", "halo2_objects", "odst_objects", "halo3_objects", "forge_primitives", "bind_address",
            "vote_kick_immune", "launch_timeout"} <= keys
    assert all(not ps.offered(s, "0.9.23") and ps.offered(s, "0.10.0") and ps.offered(s, None) for s in new)
    groups = {key for key, _, _ in ps.GROUPS}
    assert all(s.group in groups for s in new)
    tick = ps.find("tick_rate")
    assert ps.parse(tick, "30") == 30 and ps.parse(tick, "60") == 60  # a number in the file, as the program reads it
    with pytest.raises(ValueError):
        ps.parse(tick, "45")


def test_the_0_10_values_are_checked_as_0_10_checks_them(tmp_path: Path) -> None:
    blocks = ps.find("kick_blocks")
    assert ps.parse(blocks, "10m, 2h, 1d") == ["10m", "2h", "1d"] and ps.parse(blocks, "") == []
    for bad, why in (("10m, 5m", "as long as the one before"), ("8d", "a week at most"), ("1m," * 9, "8 steps")):
        with pytest.raises(ValueError, match=why):
            ps.parse(blocks, bad.strip(","))
    assert ps.parse(ps.find("join_gates"), "Vote, loading") == ["vote", "loading"]
    with pytest.raises(ValueError, match="no phase"):
        ps.parse(ps.find("join_gates"), "lobby")
    with pytest.raises(ValueError, match="not a player ID"):
        ps.parse(ps.find("vote_kick_immune", "server"), "Bob")
    assert ps.parse(ps.find("launch_timeout"), "300") == 300
    with pytest.raises(ValueError):
        ps.parse(ps.find("launch_timeout"), "301")  # 0.10.0: "launch_timeout: 0 to 300 seconds"
    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    bind = ps.find("bind_address")
    assert "IP address" in ps.check_value(bind, "my.host.example", server.config_path, ALPHA, None, "0.10.0")
    assert ps.check_value(bind, "::", server.config_path, ALPHA, None, "0.10.0") is None
    assert "needs Project Reclaimer 0.10.0" in ps.check_value(bind, "::", server.config_path, ALPHA, None, "0.9.23")
    players = ps.find("max_players", "server")
    assert ps.parse(players, "128") == 128
    assert ps.check_value(players, 128, server.config_path, ALPHA, None, "0.10.0") is None
    assert "127 at most before Project Reclaimer 0.10.0" in ps.check_value(players, 128, server.config_path, ALPHA,
                                                                           None, "0.9.23")
    assert ps.check_path(ps.find("game", "folder"), server.folder, "nowhere") is not None  # Halo 3 is always needed


def test_d_leaves_out_0_10_settings_for_a_0_9_server(tmp_path: Path, run, monkeypatch) -> None:
    async def version(self: Any, settings: Any, server: Any, row: Any) -> str:
        return "0.9.23"

    monkeypatch.setattr(Menu, "_program_version", version)
    controller = SetupController(setup_root(tmp_path))
    keys = ["1", "ENTER", "3", "ENTER", "ESC", "ESC", "ESC"]  # Alpha, Gameplay, back
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    text = plain(terminal)
    assert "friendly_fire" in text and "tick_rate" not in text and "reach_objects" not in text


# `dedicated check`'s 0.10 refusals -----------------------------------------------------------------------------

NOT_READY = '''Configuration  .huragok-check-1-1.toml
[reach] Halo: Reach installed in H:/MCC
Server         Alpha Server · playlists/.typo.huragok-check-1-2.json: game 2: no multiplayer map "Atlantiss" is installed
               anti-cheat enforce · camera first_person · team changing balanced · friendly fire on
Ports          UDP 50611
Not ready:
  Alpha Server: playlists/.typo.huragok-check-1-2.json: game 2: no multiplayer map "Atlantiss" is installed; as a variant: Map 'Atlantiss' was not found. Did you mean 'S'?; game 3: Blood Gulch ctf: Halo: Combat Evolved's maps play their game types' own options (name one of its variants instead of options or rules); game 6: Guardian Slayer is listed twice (raise its weight instead)
'''


def test_0_10s_refusals_are_read_game_by_game() -> None:
    found = _refusals(NOT_READY, ALPHA)
    assert [p.game for p in found] == [2, 3, 6]
    assert found[0].text.startswith('Game 2: no multiplayer map "Atlantiss" is installed; as a variant')
    assert found[2].text == "Game 6: Guardian Slayer is listed twice (raise its weight instead)"
    one = _refusals('Not ready:\n  Alpha Server: no multiplayer map "Atlantiss" is installed\n', ALPHA)
    assert [(p.game, p.text) for p in one] == [(None, 'no multiplayer map "Atlantiss" is installed')]
    named = _refusals('[server] [[server]] "Alpha Server": max_players 128 leaves 0 queue places: queue_max 0\n'
                      '[[server]] "Alpha Server": launch_timeout: 0 to 300 seconds\n', ALPHA)
    assert [p.text for p in named] == ["launch_timeout: 0 to 300 seconds"]  # the [server] note isn't a refusal
    assert [p.text for p in _refusals('"Alpha Server": max_players is 1 through 128\n', ALPHA)] == [
        "max_players is 1 through 128"]
    field = _refusals(".huragok-check-1-1.toml: unknown field `rules`, expected one of `name`, `port`, `enabled`, "
                      "`playlist`, `variant`, `map`, `game`, `base_map`, `hidden`, `password`\nin `server`\n\n", ALPHA)
    assert [p.text for p in field] == ["unknown field `rules` (in server)"]
    value = _refusals("x.toml: tick_rate is 30 or 60, not 45\nin `server.tick_rate`\n", ALPHA)
    assert [p.text for p in value] == ["tick_rate is 30 or 60, not 45 (in server.tick_rate)"]
    old = _refusals("Error: Server 'Alpha Server': playlists/x.playlist.json: Game 2 duplicates another one\n", ALPHA)
    assert [(p.game, p.text) for p in old] == [(2, "Game 2 duplicates another one")]  # 0.9's, as before


class Tool010:
    """A 0.10 program's answers to check_plays: the draft that names "Atlantiss" isn't ready."""

    def run_sync(self, folder: Path, *words: str, **options: Any) -> ToolResult:
        draft = (folder / options["config"]).read_text(encoding="utf-8")
        playlist = re.search(r'playlist = "([^"]+)"', draft)
        file = folder / playlist.group(1) if playlist else None
        if file is None or not file.is_file() or "Atlantiss" not in file.read_text(encoding="utf-8"):
            return ToolResult(True, 0, "Everything is ready.")
        return ToolResult(False, 1, NOT_READY.replace("playlists/.typo.huragok-check-1-2.json", playlist.group(1)))

    def playlist_show(self, folder: Path, reference: str) -> ToolResult:
        return ToolResult(False, 1, f"{reference}: game 4: shown only by playlist show")


def test_check_plays_names_each_game_0_10_refuses(tmp_path: Path) -> None:
    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    playlist = Playlist(server.folder / "playlists" / "typo.playlist.json", "Typo", 20, [
        {"map": "Guardian", "game": "Slayer"}, {"map": "Atlantiss", "game": "Slayer"},
        {"map": "Blood Gulch", "game": "ctf"}, {"map": "Valhalla", "game": "Slayer"}])
    check = check_plays(Tool010(), server, {"playlist": playlist.relative}, playlist, program_version="0.10.0")
    assert [p.game for p in check.problems] == [2, 3, 6, 4]
    assert check.problems[3].text == "shown only by playlist show"
    assert not list(server.folder.rglob("*huragok-check*"))
    two = Playlist(playlist.path, "Two", 20, [{"map": "Guardian", "game": "Slayer"}, {"map": "Valhalla",
                                                                                    "game": "Slayer"}])
    assert check_plays(Tool010(), server, {"playlist": two.relative}, two, program_version="0.10.0").ok
    assert "3 or more" in check_plays(Tool010(), server, {"playlist": two.relative}, two,
                                      program_version="0.9.23").problems[0].text


def test_max_players_128_is_written_for_a_0_10_server(tmp_path: Path) -> None:
    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    setting = ps.find("max_players", "server")
    ps.write(server.config_path, ALPHA, setting, ps.parse(setting, "128"))
    assert read_toml(server.config_path)["server"][0]["max_players"] == 128
    set_server_setting(server.config_path, ALPHA, "tick_rate", ps.parse(ps.find("tick_rate"), "30"))
    assert read_toml(server.config_path)["server"][0]["tick_rate"] == 30
