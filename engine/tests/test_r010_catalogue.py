"""Project Reclaimer 0.10: `dedicated content`'s new shape (every game's maps, counted variants, repeated names), the
catalogue's games, one call for everything, Halo 3 mod maps from the content folder, ODST from "[odst] … installed"."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from huragok.content.catalogue import (Catalogue, Entry, Mod, add_searched, game_of_source, installed_mods,
                                       load_catalogue, parse_listing, set_mod_disabled)
from huragok.games import odst

# Trimmed from 0.10.0's own output on this PC (2026-11-06), the odd lines kept: the program's notes between entries,
# a Firefight map, CE and Halo 2 mod maps in their game's section, the other games' section, the counted variants.
LISTING = '''[reach] Halo: Reach installed in H:/SteamLibrary/steamapps/common/Halo The Master Chief Collection
[halo1] Halo: Combat Evolved installed in H:/SteamLibrary/steamapps/common/Halo The Master Chief Collection
[halo1] the mods' maps of CE's: "Cursed Halo Again" (30 maps)
[halo2] Halo 2 not installed in game
Maps (map = "..."):
  Assembly                 descent (ID 490)
  Blackout                 lockout (ID 520)
Replaced the default game mode content\\Game Modes\\Project Reclaimer\\gun-game.json with content\\Game Modes\\gun-game.rmeg
  Guardian                 guardian (ID 320)
Halo: Reach's maps (map = "..."):
  Beachhead                ff50_park (ID 7060)
  Sword Base               20_sword_slayer (ID 1000)
Halo: Combat Evolved's maps (map = "..."):
  Blood Gulch              halo1_bloodgulch (ID 1224137234)
  Hang 'Em High            halo1_hangemhigh (ID 1212397335)
  Booglooch                halo1_bloodgulch_6381b55e (ID 1208457912)
Halo 2's maps (map = "..."):
  Lockout                  halo2_lockout (ID 853868150)
  03 Burial Mounds Uncut   halo2_03_burial_mounds_dw3_9c36e428 (ID 853976373)
[host] 56 of Reach's variants are Firefight's or a campaign's, not game types
Skipped content file H:/SteamLibrary/steamapps\\workshop\\content\\976730\\2898916793\\halo3\\maps\\fonts\\font_package.bin: Expected an MCC BLF variant file
[odst] Halo 3: ODST installed in H:/SteamLibrary/steamapps/common/Halo The Master Chief Collection
Other games' mods' maps and campaign canvases (map = "..."):
  Sierra 117 · set null (Campaign Canvas) canvas_010_jungle_z0
  Perfected Forge World (Grass World) · Perfected Forge World (Grass World) super_forge_0f92cfb6
  Scarif · The Battlefront Pack - Reach scarif_b72b9298
Game types (game = "..."):
  Slayer                   slayer
  Capture the Flag         ctf
  Escalation Slayer        escalation
  Forge                    forge
Map variants and Forge saves (map = "..."): 595 (name words to list them)
Game variants and game modes (game = "..."): 758 (name words to list them)'''

# `dedicated content ""` lists every variant: names padded to 32, some repeated, some with spaces of their own.
EVERY_VARIANT = '''Maps (map = "..."):
  Guardian                 guardian (ID 320)
Game types (game = "..."):
  Slayer                   slayer
Map variants and Forge saves (map = "..."):
   Synapse                         Forge (Matchmaking)
  Alembic  Gold                    Forge (Matchmaking)
  Boundless                        Forge (Halo 3)
  Asylum                           Forge (Halo: Reach)
Game variants and game modes (game = "..."):
  2V2 HARDCORE TS                  Slayer (Halo 3)
  2V2 HARDCORE TS                  Slayer (Matchmaking)
  Gun Game                         Slayer (Custom)
  Slayer DMRs                      Slayer (Halo: Reach matchmaking)
  Slayer DMRs                      Slayer (Halo: Reach matchmaking)
  Slayer DMRs                      Slayer (Halo: Reach matchmaking)
  TU ELITE ESCALATION              Slayer (Halo: Reach matchmaking)
  ESCALATION SLAYER                Escalation Slayer (Halo: Reach)
  A Name Thirty Three Letters Long Slayer (Halo 3)
  King of the Hill Classic         King of the Hill (Matchmaking)'''

OLD_LISTING = '''Maps: map = "…" (and base_map), or a playlist game's "map"
  "Assembly"      or "descent"
  "Guardian"
  "workshop:2898980746/shrine_albatross_rc"  mod · Albatross Battle Sandtrap
Game types: game = "…", or a playlist game's "game"
  "Slayer"
Game variants: game = "…"
  From your content folder:
  "Gun Game"  game variant · Slayer
  49 from Halo 3 (search to list them: dedicated content <words>)
Halo 3: ODST campaign missions:
  odst = { mission = 4100 }  Prepare to Drop
'''


def test_every_games_maps_are_read_and_told_apart_from_mod_maps() -> None:
    catalogue = parse_listing(LISTING)
    assert catalogue.shape == "0.10"
    assert [(e.name, e.reference, e.game) for e in catalogue.maps_of("halo3")] == [
        ("Assembly", "descent", "halo3"), ("Blackout", "lockout", "halo3"), ("Guardian", "guardian", "halo3")]
    assert [e.reference for e in catalogue.maps_of("reach")] == ["ff50_park", "20_sword_slayer"]
    assert catalogue.maps_of("reach")[0].note == "Firefight" and catalogue.maps_of("reach")[1].note == ""
    assert [e.name for e in catalogue.maps_of("halo1")] == ["Blood Gulch", "Hang 'Em High"]
    assert [e.name for e in catalogue.maps_of("halo2")] == ["Lockout"]
    assert catalogue.games_present() == ["halo3", "reach", "halo1", "halo2"]
    mods = {e.reference: (e.name, e.game, e.source) for e in catalogue.mod_maps}
    assert mods["halo1_bloodgulch_6381b55e"] == ("Booglooch", "halo1", "Halo: Combat Evolved mod")
    assert mods["halo2_03_burial_mounds_dw3_9c36e428"][1] == "halo2"
    assert mods["scarif_b72b9298"] == ("Scarif", "", "mod The Battlefront Pack - Reach")
    assert mods["super_forge_0f92cfb6"][0] == "Perfected Forge World (Grass World)"
    # campaign canvases stay out of the map pickers, but a typed name still finds one
    assert [(e.name, e.reference, e.kind) for e in catalogue.canvases] == [
        ("Sierra 117 · set null", "canvas_010_jungle_z0", "campaign canvas")]
    assert all(e.kind != "campaign canvas" for e in catalogue.all_maps)
    assert catalogue.find_map("canvas_010_jungle_z0") is catalogue.canvases[0]
    # names as the program resolves them: Halo 3's first, so "lockout" is Blackout and Halo 2's needs its file name
    assert catalogue.find_map("Lockout").name == "Blackout"
    assert catalogue.find_map("halo2_lockout").game == "halo2"
    assert [(e.name, e.reference, e.note) for e in catalogue.game_types][:2] == [("Slayer", "Slayer", "slayer"),
                                                                                ("Capture the Flag", "Capture the Flag", "ctf")]
    assert catalogue.find_game("capture the flag").game == "halo3"
    assert catalogue.counted == {"map variant": 595, "game variant": 758}
    assert not catalogue.map_variants and not catalogue.game_variants
    assert catalogue.installed == {"reach": True, "halo1": True, "halo2": False, "odst": True}
    text = catalogue.counts()
    assert "8 maps (Halo 3 3, Reach 2, CE 2, Halo 2 1), 4 mod maps, 595 map variants (counted, not listed)" in text
    assert "758 game variants (counted, not listed)" in text and "1 campaign canvases" in text


def test_variants_repeated_in_the_listing_are_one_choice_each() -> None:
    catalogue = parse_listing(EVERY_VARIANT)
    assert catalogue.counted == {}
    maps = [(e.name, e.base, e.source, e.game) for e in catalogue.map_variants]
    assert maps == [("Synapse", "Forge", "Matchmaking", "halo3"), ("Alembic  Gold", "Forge", "Matchmaking", "halo3"),
                    ("Boundless", "Forge", "Halo 3", "halo3"), ("Asylum", "Forge", "Halo: Reach", "reach")]
    games = {(e.name, e.source): e for e in catalogue.game_variants}
    assert len(catalogue.game_variants) == 8  # 2V2 HARDCORE TS from two sources is two; Slayer DMRs x3 is one
    dmrs = games[("Slayer DMRs", "Halo: Reach matchmaking")]
    assert (dmrs.reference, dmrs.base, dmrs.copies, dmrs.game) == ("Slayer DMRs", "Slayer", 3, "reach")
    assert dmrs.label == "Slayer DMRs  · Slayer  (Halo: Reach matchmaking)  3 copies"
    assert games[("Gun Game", "Custom")].game == ""
    assert games[("TU ELITE ESCALATION", "Halo: Reach matchmaking")].base == "Slayer"
    assert games[("ESCALATION SLAYER", "Halo: Reach")].base == "Escalation Slayer"
    assert games[("A Name Thirty Three Letters Long", "Halo 3")].base == "Slayer"
    assert games[("King of the Hill Classic", "Matchmaking")].base == "King of the Hill"
    assert [game_of_source(s) for s in ("Halo 3", "Matchmaking", "Halo: Reach", "Halo: Reach matchmaking",
                                        "Halo: Combat Evolved", "Halo 2", "Custom", "")] == [
        "halo3", "halo3", "reach", "reach", "halo1", "halo2", "", ""]


def test_a_0_9_listing_is_all_halo_3() -> None:
    catalogue = parse_listing(OLD_LISTING)
    assert catalogue.shape == "0.9" and catalogue.installed == {}
    assert all(e.game == "halo3" for e in [*catalogue.maps, *catalogue.mod_maps, *catalogue.game_variants])
    assert [e.name for e in catalogue.maps_of("halo3")] == ["Assembly", "Guardian"] and catalogue.maps_of("reach") == []
    assert catalogue.games_present() == ["halo3"] and catalogue.counted == {"game variant": 49}
    assert catalogue.mod_maps[0].reference == "workshop:2898980746/shrine_albatross_rc"
    assert [m.title for m in catalogue.odst.missions] == ["Prepare to Drop"]  # 0.9's own ODST sections
    assert "2 maps, 1 mod maps, 0 map variants, 1 game types, 1 game variants" in catalogue.counts()
    # a map built by hand, with no game, is Halo 3's
    assert Catalogue(maps=[Entry("Guardian", "Guardian", "map")]).maps_of("halo3")[0].name == "Guardian"


def test_odst_on_0_10_comes_from_the_built_in_tables_when_its_files_are_there() -> None:
    there = parse_listing(LISTING).odst
    assert len(there.missions) == len(odst.MISSIONS) and len(there.maps) == len(odst.FIREFIGHT_MAPS)
    assert len(there.types) == len(odst.FIREFIGHT_TYPES)
    gone = LISTING.replace("[odst] Halo 3: ODST installed in", "[odst] Halo 3: ODST not installed in")
    assert parse_listing(gone).odst.empty and odst.parse_listing(gone).empty
    assert odst.parse_listing('Maps (map = "..."):\n  Guardian                 guardian (ID 320)\n').empty


def a_mod(folder: Path, title: str, maps: dict[str, str], *, workshop: int | None = None,
          engine: str = "Halo3") -> Path:
    """A mod as MCC writes one: ModInfo.json and multiplayer/<file>.json naming the map's ScenarioFile."""
    (folder / "multiplayer").mkdir(parents=True)
    info: dict[str, Any] = {"ModIdentifier": {"ModGuid": "g"}, "Engine": engine, "Title": {"Neutral": title},
                            "GameModContents": {"MultiplayerMaps": [f"multiplayer/{n}.json" for n in maps]}}
    if workshop:
        info["ModIdentifier"]["HostedModIds"] = {"SteamWorkshopId": workshop}
    (folder / "ModInfo.json").write_text(json.dumps(info), encoding="utf-8")
    for name, stem in maps.items():
        (folder / "multiplayer" / f"{name}.json").write_text(json.dumps(
            {"Title": {"Neutral": name.title()}, "ScenarioFile": f"levels/multi/x/{stem}"}), encoding="utf-8")
    return folder


class Tool:
    """`dedicated content` as 0.10 answers: everything for the empty word."""

    def __init__(self, text: str, *, fail_first: int | None = None) -> None:
        self.text, self.fail_first, self.asked = text, fail_first, []  # type: list[list[str] | None]

    def content(self, folder: Path, words: list[str] | None = None) -> Any:
        self.asked.append(words)
        if self.fail_first is not None and len(self.asked) == 1:
            return SimpleNamespace(ok=False, code=self.fail_first, text="", problem="no")
        return SimpleNamespace(ok=True, code=0, text=self.text, problem="")


def test_one_call_lists_everything_and_halo_3_mods_come_from_the_content_folder(tmp_path: Path) -> None:
    content = tmp_path / "content"
    a_mod(content / "Mods" / "Workshop" / "2898980746", "Albatross Battle", {"sandtrap": "shrine_albatross_rc"},
          workshop=2898980746)
    a_mod(content / "Mods" / "Mine", "My Mod", {"my map": "my_map"})
    a_mod(content / "Mods" / "Off", "Switched Off", {"off map": "off"}, workshop=5)
    a_mod(content / "Mods" / "Booglooch", "CE Mod", {"booglooch": "halo1_bloodgulch_6381b55e"}, engine="Halo1")
    set_mod_disabled(content, "Mods/Off", True)
    heading = "Map variants and Forge saves"
    tool = Tool(LISTING.split(heading)[0] + heading + EVERY_VARIANT.split(heading, 1)[1])
    catalogue = load_catalogue(tool, tmp_path, content, tmp_path / "dedicated.toml")  # type: ignore[arg-type]
    assert tool.asked == [[""]]  # one call: no vowel searches on 0.10
    assert catalogue.counted == {} and len(catalogue.game_variants) == 8
    halo3 = [(e.name, e.reference, e.source) for e in catalogue.mod_maps if e.game == "halo3"]
    assert halo3 == [("My Map", "My Map · Custom", "mod My Mod"),
                     ("Sandtrap", "workshop:2898980746/shrine_albatross_rc", "mod Albatross Battle")]  # in folder
    # order; switched off: left out; a CE mod's maps: the program lists those itself
    assert {m.title: m.stems for m in catalogue.mods}["Albatross Battle"] == ["shrine_albatross_rc"]


def test_a_program_that_refuses_the_empty_word_is_asked_plainly_and_0_9_searches_by_vowel(tmp_path: Path) -> None:
    tool = Tool(OLD_LISTING, fail_first=2)
    catalogue = load_catalogue(tool, tmp_path, tmp_path / "content", tmp_path / "dedicated.toml")  # type: ignore[arg-type]
    assert tool.asked == [[""], None, ["a"], ["e"], ["i"], ["o"], ["u"]]
    assert catalogue.counted == {} and catalogue.shape == "0.9"
    assert [e.name for e in catalogue.mod_maps] == ["workshop:2898980746/shrine_albatross_rc"]  # not added twice
    timed_out = Tool(OLD_LISTING, fail_first=124)
    with pytest.raises(ValueError):
        load_catalogue(timed_out, tmp_path, tmp_path / "content", tmp_path / "dedicated.toml")  # type: ignore[arg-type]
    assert timed_out.asked == [[""]]  # a call that ran out of time isn't made twice


def test_searches_of_0_10_keep_each_name_base_and_source_once() -> None:
    catalogue = parse_listing(LISTING)
    found = SimpleNamespace(ok=True, text=EVERY_VARIANT)
    add_searched(catalogue, [found, found, SimpleNamespace(ok=False, text="")])
    assert catalogue.counted == {} and len(catalogue.game_variants) == 8 and len(catalogue.map_variants) == 4
    assert next(e for e in catalogue.game_variants if e.name == "Slayer DMRs").copies == 3


def test_mods_of_every_game_are_named_for_their_game(tmp_path: Path) -> None:
    def label(engine: str) -> str:
        return Mod(tmp_path, "X", engine=engine).label

    assert label("Halo3") == "X" and label("") == "X" and Mod(tmp_path, "X").game == "halo3"
    assert label("HaloReach").endswith("a Halo: Reach mod") and Mod(tmp_path, "X", engine="HaloReach").game == "reach"
    assert label("Halo1").endswith("a Halo: Combat Evolved mod") and label("Halo2").endswith("a Halo 2 mod")
    assert label("Halo4").endswith("Halo4: not a game Reclaimer plays") and Mod(tmp_path, "X", engine="Halo4").game == ""
    folder = a_mod(tmp_path / "Mods" / "R", "Reach Mod", {"r": "r_map"}, engine="HaloReach")
    assert installed_mods(tmp_path)[0].game == "reach" and folder.is_dir()
