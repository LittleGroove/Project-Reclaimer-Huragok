"""Is a mod good to go (modcheck.py)? Mods built here the way the server program reads them; the pairing rule was
tried on 0.9.21's `dedicated content` on 20 October 2026, and the faults are ones the hosts' own mods had: a leftover
fixedHugeGrassTrapasRemix-old.map on the live server, a description naming the scenario's path instead of the map's
file name (HugeGrass Rebalance's upload of 16 October), a ModInfo.json listing a description that isn't there."""

from __future__ import annotations

import json
from pathlib import Path

from testkit.mapfiles import broken
from testkit.mods import a_map, a_mod

from huragok.content import modcheck
from huragok.content.modcheck import BROKEN, NOTE, WARNING, check_mods
from huragok.games import halo3


def texts(report: modcheck.ModReport, level: str | None = None) -> str:
    return "\n".join(f.text for f in report.findings if level is None or f.level == level)


def test_a_workshop_mod_as_the_program_installs_it_is_good_to_go(tmp_path: Path) -> None:
    content = tmp_path / "content"
    a_mod(content / "Mods" / "Workshop" / "3820687243", {"fixedHugeGrassTrapasRemix": a_map(8752)},
          title="HugeGrass Rebalance", workshop=3820687243)
    a_mod(content / "Mods" / "canyon", {"canyon": a_map(9002)}, title="Canyon")
    (content / "Mods" / "lone").mkdir()
    (content / "Mods" / "lone" / "blam_mountain.map").write_bytes(a_map(9003))  # a single map: no ModInfo needed
    reports = {r.folder.name: r for r in check_mods(content)}
    assert set(reports) == {"3820687243", "canyon", "lone"} and all(r.good for r in reports.values())
    hugegrass = reports["3820687243"]
    assert hugegrass.maps == ["fixedHugeGrassTrapasRemix"] and not hugegrass.findings
    assert hugegrass.summary() == ("HugeGrass Rebalance (Workshop 3820687243, uploaded 20 Oct 2026 05:57 UTC): good "
                                   "to go · 1 map")
    assert reports["lone"].maps == ["blam_mountain"]
    assert check_mods(tmp_path / "nothing here") == []


def test_maps_the_server_skips_for_want_of_a_description(tmp_path: Path) -> None:
    mods = tmp_path / "content" / "Mods"
    # the live host's: an old copy left beside the map the description names
    a_mod(mods / "HugeGrass-Rebalance-Los-Angeles", {"fixedHugeGrassTrapasRemix": a_map(8752),
                                                     "fixedHugeGrassTrapasRemix-old": a_map(8752)},
          {"fixedHugeGrassTrapasRemix": "fixedHugeGrassTrapasRemix"})
    # the Workshop upload of 16 October: its description named the scenario's path, not the map's file name
    a_mod(mods / "Workshop" / "3820687243", {"fixedHugeGrassTrapasRemix": a_map(8752)},
          {"hugeass_remastered_mcc": "levels/multi/hugeass_remastered_ed/hugeass_remastered_mcc"}, workshop=3820687243)
    # a ModInfo.json listing a description that isn't there; one listing none
    a_mod(mods / "listed", {"canyon": a_map(9010)}, listed=["multiplayer/canyon.json", "multiplayer/Spiritsmix.json"])
    a_mod(mods / "unlisted", {"mesa": a_map(9011)}, listed=[])
    reports = {r.folder.name: r for r in check_mods(tmp_path / "content")}
    old = reports["HugeGrass-Rebalance-Los-Angeles"]
    assert old.maps == ["fixedHugeGrassTrapasRemix"] and not old.good and not old.broken
    assert ("fixedHugeGrassTrapasRemix-old.map looks like an old copy of fixedHugeGrassTrapasRemix.map: the server "
            "skips it at every start (\"Map needs matching .mapinfo metadata\"); delete it to free 20.3 KB"
            ) in texts(old, WARNING)
    upload = reports["3820687243"]
    assert upload.broken and upload.maps == []
    assert ("fixedHugeGrassTrapasRemix.map is skipped by the server (\"Map needs matching .mapinfo metadata\"): none "
            "of the descriptions ModInfo.json lists (MultiplayerMaps) has a ScenarioFile ending in "
            "fixedHugeGrassTrapasRemix") in texts(upload, BROKEN)
    assert ("multiplayer/hugeass_remastered_mcc.json describes hugeass_remastered_mcc.map, which isn't in the mod"
            ) in texts(upload, WARNING)
    assert texts(reports["listed"]) == "ModInfo.json lists multiplayer/Spiritsmix.json, which isn't in the mod"
    assert reports["listed"].maps == ["canyon"]
    assert "mesa.map is skipped by the server" in texts(reports["unlisted"], BROKEN)


def test_the_name_ends_the_scenario_file_in_any_case(tmp_path: Path) -> None:
    a_mod(tmp_path / "Mods" / "kilo", {"Kilo": a_map(9101)}, {"k": "levels/multi/x/KILO"})
    (report,) = check_mods(tmp_path)
    assert report.good and report.maps == ["Kilo"]


def test_what_the_program_refuses_or_ignores(tmp_path: Path) -> None:
    mods = tmp_path / "content" / "Mods"
    a_mod(mods / "reach", {"golf": a_map()}, engine="HaloReach")
    a_mod(mods / "Workshop" / "123457", {"oscar": a_map(9105)}, workshop=999)
    a_mod(mods / "Workshop" / "123458", {"papa": a_map(9106)}, workshop=123458, receipt=False)
    item = a_mod(mods / "Workshop" / "123459", {"quebec": a_map(9107), "romeo": a_map(9108)}, workshop=123459)
    (item / "maps" / "romeo.map").unlink()
    (item / "maps" / "quebec.map").write_bytes(a_map(9107) + b"more")
    (mods / "unreadable").mkdir()
    (mods / "unreadable" / "ModInfo.json").write_text("{\"Title\": ", encoding="utf-8")
    reports = {r.folder.name: r for r in check_mods(tmp_path / "content")}
    assert texts(reports["reach"]) == "it is made for HaloReach, not Halo 3: the server ignores it without a word"
    assert "names Workshop item 999, not 123457 (this folder's): the server refuses to install or update it" in texts(
        reports["123457"], WARNING)
    assert "it has no installation receipt (reclaimer-installed.json)" in texts(reports["123458"], WARNING)
    incomplete = texts(reports["123459"], BROKEN)
    assert "its Workshop installation is incomplete: 2 of " in incomplete
    assert "maps/quebec.map (another size)" in incomplete and "maps/romeo.map (missing)" in incomplete
    assert texts(reports["unreadable"], BROKEN).startswith("its ModInfo.json can't be read (line 1:")


def test_maps_the_server_cant_use(tmp_path: Path) -> None:
    mods = tmp_path / "Mods"
    a_mod(mods / "damaged", {"broken": bytes(broken("table"))})
    a_mod(mods / "campaign", {"echo": a_map(9005, kind=0)})
    reports = {r.folder.name: r for r in check_mods(tmp_path)}
    assert ("broken.map is skipped by the server (its tag index is damaged): its tag table (600 tags) runs to "
            "0x1ca1a12c0") in texts(reports["damaged"], BROKEN)
    assert reports["damaged"].maps == []
    assert "echo.map holds a campaign scenario, not a multiplayer one" in texts(reports["campaign"], WARNING)


def test_clashes_between_mods(tmp_path: Path) -> None:
    mods = tmp_path / "Mods"
    a_mod(mods / "first", {"arena": a_map(7000)}, title="First")
    a_mod(mods / "second", {"arena": a_map(7001), "dome": a_map(7000)}, title="Second")
    a_mod(mods / "off", {"twin": a_map(7000)}, title="Off")
    (tmp_path / "disabled-mods.json").write_text(json.dumps({"packages": ["Mods/off"]}))
    reports = {r.folder.name: r for r in check_mods(tmp_path)}
    assert ("arena.map and dome.map (Second) share map ID 7000: they can't both be played in one session or playlist"
            ) in texts(reports["first"], WARNING)
    assert "dome.map and arena.map (First) share map ID 7000" in texts(reports["second"], WARNING)
    assert "arena.map is also in Second: two mods' maps with one file name can't be in one playlist" in texts(
        reports["first"], NOTE)
    assert reports["off"].disabled and not reports["off"].findings and "switched off" in reports["off"].summary()


def test_forge_layouts_on_maps_that_arent_there(tmp_path: Path) -> None:
    mod = a_mod(tmp_path / "Mods" / "hugegrass", {"fixedHugeGrassTrapasRemix": a_map(8752)})
    layouts = mod / modcheck.LAYOUTS
    layouts.mkdir()
    for name, base in (("Spirits Mix", {"id": 8752, "stem": "fixedHugeGrassTrapasRemix"}),
                       ("Old Mix", {"id": 7521, "stem": "fixedHugeGrassTrapasRemix"}),
                       ("Elsewhere", {"id": 1, "stem": "hugeass_remastered_mcc"}),
                       ("Stock", {"id": 320, "stem": "guardian"})):
        (layouts / f"{name}.json").write_text(json.dumps({"version": 1, "name": name, "base_map": base}))
    (layouts / "damaged.json").write_text("{", encoding="utf-8")
    (report,) = check_mods(tmp_path)
    found = texts(report, WARNING)
    assert "Spirits Mix" not in found and "Stock" not in found
    assert ("the Forge layout \"Old Mix\" was made on another version of fixedHugeGrassTrapasRemix.map (map ID 7521; "
            "the installed one is 8752)") in found
    assert "the Forge layout \"Elsewhere\" is made on hugeass_remastered_mcc.map, which isn't installed" in found
    assert "the Forge layout damaged.json can't be read" in found


def test_character_mods_and_the_maps_they_need(tmp_path: Path) -> None:
    good = a_mod(tmp_path / "content" / "Mods" / "spartans", {})
    (good / "customization.json").write_text(json.dumps({"version": 1}))
    bad = a_mod(tmp_path / "content" / "Mods" / "elites", {})
    (bad / "customization.json").write_text("not json", encoding="utf-8")
    game = tmp_path / "game"
    (game / "halo3" / "maps").mkdir(parents=True)
    for stem in sorted(halo3.HALO3_MAPS)[:-2]:  # two of the 27 missing
        (game / "halo3" / "maps" / f"{stem}.map").write_bytes(b"")
    reports = {r.folder.name: r for r in check_mods(tmp_path / "content", game)}
    assert "its customization.json can't be read: its characters are left out" in texts(reports["elites"], BROKEN)
    assert ("character mods need all 27 of Halo 3's multiplayer maps in the game folder to make their packs; 2 are "
            "missing (warehouse, zanzibar)") in texts(reports["spartans"], WARNING)
    assert check_mods(tmp_path / "content", None)[1].good  # no game folder to look in: nothing said about it


def test_the_limits_players_downloads_have(tmp_path: Path) -> None:
    maps = {f"map{n}": a_map(9300 + n) for n in range(9)}
    a_mod(tmp_path / "Mods" / "big", maps)
    (report,) = check_mods(tmp_path)
    assert "it has 9 maps; a mod can hold 8, so players can't get it from the server" in texts(report, WARNING)


def test_a_map_is_read_again_only_when_it_changes(tmp_path: Path, monkeypatch) -> None:
    a_mod(tmp_path / "Mods" / "once", {"once": a_map()})
    reads: list[Path] = []
    real = modcheck.halo3_mapfile.check
    monkeypatch.setattr(modcheck.halo3_mapfile, "check", lambda path: reads.append(path) or real(path))
    check_mods(tmp_path)
    check_mods(tmp_path)
    assert len(reads) == 1
    (tmp_path / "Mods" / "once" / "maps" / "once.map").write_bytes(a_map(9999))
    (report,) = check_mods(tmp_path)
    assert len(reads) == 2 and report.good
