"""File health (content/filehealth.py, content/playswatch.py): every name a server's playlists give resolved the way
Project Reclaimer 0.10 resolves them, the server's own program's verdict tied to its file and game, the games it said
it couldn't play, Huragok's cheap judgement of what the program lets through, all as warnings that clear themselves.
And B1 in the program's check, B8 in imports, B14 "who uses this file"."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from testkit.setup import fake_tool, setup_root, write_playlist, write_plays

from huragok.content import filehealth
from huragok.content.filehealth import SkipSeen, load_verdict, server_files, who_uses
from huragok.content.playswatch import PlaysWatch, fingerprint
from huragok.core.events import EventBus
from huragok.core.serverwarnings import Warnings, load_rules
from huragok.core.settings import load
from huragok.program.plays import (
    Problem, Verdict, _refusals, check_plays, listed_problems, mode_notes, plays_verdict, skipped_files,
)

RMEG = '--[=[ mode\nname = "Laser Tag"\nmode = "Slayer"\n]=]\nalias size = player.number[0]\n'


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text, encoding="utf-8")
    return path


def add_mod(content: Path, folder: str, title: str, maps: list[tuple[str, str]], item: int | None = None) -> Path:
    """A mod as content/Mods holds one: its ModInfo.json, a description per map (title, file name) and the maps."""
    mod = content / "Mods" / folder
    info: dict[str, Any] = {"Title": {"Neutral": title}, "Engine": "Halo3",
                            "GameModContents": {"MultiplayerMaps": [f"multiplayer/{stem}.json" for _, stem in maps]}}
    if item:
        info["ModIdentifier"] = {"HostedModIds": {"SteamWorkshopId": item}}
    write(mod / "ModInfo.json", json.dumps(info))
    for map_title, stem in maps:
        write(mod / "multiplayer" / f"{stem}.json", json.dumps({"Title": {"Neutral": map_title},
                                                                "ScenarioFile": f"levels/multi/{stem}/{stem}"}))
        write(mod / "maps" / f"{stem}.map", b"map")
    return mod


def layout(name: str, stem: str = "guardian", made_on: int = 320, frozen: int = 0) -> str:
    data = bytearray(0xE0A0)
    for number in range(10):
        struct.pack_into("<H", data, 0x130 + 0x54 * number, 0x1)
    block: dict[str, Any] = {"name": name, "engine_build": "x", "data": base64.b64encode(bytes(data)).decode()}
    if frozen:
        block["frozen"] = base64.b64encode(b"FRZ1" + struct.pack("<IH", frozen, 0)).decode()
    return json.dumps({"version": 2, "name": name, "mode": "Forge", "rules": {},
                       "base_map": {"id": made_on, "stem": stem, "title": stem}, "map": block})


@pytest.fixture
def alpha(tmp_path: Path) -> Any:
    settings = load(setup_root(tmp_path))
    return settings, settings.servers[0]


def game_of(report: Any, file: str, number: int) -> Any:
    playlist = next(p for p in report.playlists if p.relative == f"playlists/{file}.playlist.json")
    return next(g for g in playlist.games if g.number == number)


def rules_found(report: Any) -> set[tuple[str, str]]:
    return {(rule, key) for rule, key, _, _ in report.findings}


# The resolver -------------------------------------------------------------------------------------------------------

def test_every_name_a_playlist_gives_is_answered(alpha: Any) -> None:
    settings, server = alpha
    content = server.content_dir
    add_mod(content, "HugeGrass SPR", "HugeGrass SPR", [("HugeGrass SPR", "hugegrass_spr")], 3832239928)
    write(content / "Game Modes" / "laser.rmeg", RMEG)
    write(content / "Game Modes" / "SPR" / "BR Fiesta CTF.json", json.dumps(
        {"version": 2, "name": "BR Fiesta CTF", "mode": "CaptureTheFlag", "rules": {}}))
    write(content / "Maps" / "SPR" / "GroundWar.json", layout("GroundWar"))
    write_playlist(server.folder, "mix", [
        {"map": "Guardian", "game": "Slayer"},
        {"map": "HugeGrass SPR", "game": "Laser Tag"},
        {"map": "Maps/SPR/GroundWar.json", "game": "BR Fiesta CTF"},
        {"map": "Maps/SPR/Missing.json", "game": "Slayer"},
        {"campaign": {"mission": 3010}},
        {"odst": {"mode": "firefight", "mission": 6100}},
        {"map": "Guardian", "game": "Slayer", "name": "Pictured", "preview_image": "nope.png"},
        {"map": "workshop:3832239928/hugegrass_spr", "game": "Slayer"},
        {"map": "Atlantis", "game": "Slayer"},
    ])
    write_playlist(server.folder, "spare", [{"map": "Valhalla", "game": "Laser Tag"}])
    write_plays(server.config_path, server.name, {"playlists": [{"file": "playlists/mix.playlist.json",
                                                                 "players": "1-8"}]})
    report = server_files(server)
    assert report.kind == "playlists" and report.plays == "playlists by lobby size: mix 1-8"
    assert report.setting == "playlists = [1 entries]"
    used = {p.relative: p.in_use for p in report.playlists}
    assert used["playlists/mix.playlist.json"] == "playlists = 1-8" and used["playlists/spare.playlist.json"] == ""
    statuses = {g.number: g.status for g in report.in_use[0].games}
    assert statuses[1] == statuses[2] == statuses[3] == "ok"
    assert [r.where for r in game_of(report, "mix", 2).refs] == ["mod HugeGrass SPR (Workshop 3832239928)",
                                                                  "Game Modes/laser.rmeg"]
    assert game_of(report, "mix", 3).refs[0].where == "Maps/SPR/GroundWar.json"
    assert statuses[4] == "missing" and "content/Maps/SPR/Missing.json isn't there" in game_of(report, "mix", 4).reason
    assert statuses[5] == "skip" and "campaign files aren't in its game folder" in game_of(report, "mix", 5).reason
    assert game_of(report, "mix", 5).label == "Campaign: Sierra 117 (normal)"
    assert statuses[6] == "skip" and "Unknown Firefight map" in game_of(report, "mix", 6).reason
    assert statuses[7] == "missing" and "nope.png" in game_of(report, "mix", 7).reason
    assert statuses[8] == "ok"  # by its Workshop item and file name
    assert game_of(report, "mix", 8).refs[0].kind == "mod map"
    assert statuses[9] == "unknown"  # the program also reads this PC's Steam Workshop folder: its check says
    modes = {m.relative: m for m in report.game_modes}
    assert modes["Game Modes/laser.rmeg"].used_by == ["playlists/mix.playlist.json game 2",
                                                      "playlists/spare.playlist.json game 1 (not played now)"]
    assert modes["Game Modes/laser.rmeg"].in_use and modes["Game Modes/laser.rmeg"].details == "Slayer · scripted mode (.rmeg)"
    assert {l.relative: l.used_by for l in report.layouts}["Maps/SPR/GroundWar.json"] == [
        "playlists/mix.playlist.json game 3"]
    assert ("plays-image", "playlists/mix.playlist.json, game 7 (Pictured on Guardian (Slayer)):nope.png") in \
        rules_found(report)
    assert ("plays-workshop-names", "workshop:3832239928/hugegrass_spr") in rules_found(report)


def test_who_uses_a_file_sees_playlists_rmeg_and_variants(alpha: Any) -> None:
    """B14: `playlist`, `playlists`, `variant`, `game`, a .rmeg by its header's name; co-op entries name no file."""
    settings, server = alpha
    rmeg = write(server.content_dir / "Game Modes" / "laser.rmeg", RMEG)
    small = write_playlist(server.folder, "small", [{"map": "Guardian", "game": "Laser Tag"},
                                                    {"campaign": {"mission": 3010}}])
    write_plays(server.config_path, server.name, {"playlists": [{"file": "playlists/small.playlist.json",
                                                                 "players": "1-8"}]})
    assert [u.text for u in who_uses(settings.servers, rmeg)] == [
        f"{server.name} (playlists/small.playlist.json game 1)"]
    assert [u.text for u in who_uses(settings.servers, small)] == [f"{server.name} (playlists = 1-8)"]
    write_plays(server.config_path, server.name, {"variant": "content/Game Modes/laser.rmeg"})
    uses = who_uses(settings.servers, rmeg)
    assert uses[-1].in_use and uses[-1].how.startswith("variant = ")
    assert [u.in_use for u in who_uses(settings.servers, small)] == []  # a file it has, playing nothing of it
    from huragok.window.screens.games import servers_playing

    assert servers_playing(settings, type("Info", (), {"path": rmeg})()) == [server.name]


def test_the_servers_own_map_and_game_and_defaults(alpha: Any) -> None:
    settings, server = alpha
    write(server.content_dir / "Game Modes" / "laser.rmeg", RMEG)
    write_plays(server.config_path, server.name, {"map": "Valhalla", "game": "Laser Tag"})
    report = server_files(server)
    assert report.own is not None and report.own.status == "ok" and report.own.label == "Laser Tag on Valhalla"
    assert {m.relative: m.in_use for m in report.game_modes}["Game Modes/laser.rmeg"]
    # Nothing in its block: [defaults] names what it plays (0.10.8)
    write_plays(server.config_path, server.name, {})
    text = server.config_path.read_text(encoding="utf-8").replace(
        "[defaults]", '[defaults]\nplaylists = [{ file = "playlists/slayer.playlist.json", players = "1-16" }]', 1)
    server.config_path.write_text(text, encoding="utf-8")
    report = server_files(server)
    assert report.from_defaults and report.kind == "playlists" and report.in_use
    assert load(settings.path).servers[0].plays == "playlists by lobby size: slayer 1-16"


# Huragok's own judgement ---------------------------------------------------------------------------------------------

def test_the_cheap_checks_each_with_why(alpha: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, server = alpha
    content = server.content_dir
    for number in range(3):
        add_mod(content, f"Pack {number}", f"Pack {number}", [(f"Map {number}-{m}", f"map_{number}_{m}")
                                                             for m in range(8)])
    add_mod(content, "HugeGrass SPR", "HugeGrass SPR", [("HugeGrass SPR", "hugegrass_spr")])
    ids = {"map_0_0": 7, "map_0_1": 7, "hugegrass_spr": 32}
    monkeypatch.setattr(filehealth, "_map_id", lambda index, stem: ids.get(stem.casefold()))
    write(content / "Maps" / "Big.json", layout("Big", "hugegrass_spr", 31, frozen=9995))
    for folder in ("A", "B"):
        write(content / "Game Modes" / folder / "ctf.json", json.dumps({"name": "BR CTF", "mode": "CaptureTheFlag",
                                                                         "rules": {}}))
    games = [{"map": f"Map {n}-{m}", "game": "Slayer"} for n in range(3) for m in range(8)]
    games += [{"map": "Big", "game": "Forge"},
              {"map": "Blood Gulch", "game": "Slayer", "options": {"respawn.seconds": 3}},
              {"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Vanilla characters"}},
              {"map": "Guardian", "game": "BR CTF"}]
    write_playlist(server.folder, "lots", games)
    write_plays(server.config_path, server.name, {"playlist": "playlists/lots.playlist.json"})
    report = server_files(server, version="0.10.8")
    found = {rule: fields for rule, _, _, fields in report.findings}
    assert found["plays-too-many-mod-maps"]["maps"] == 25 and found["plays-too-many-mod-maps"]["mods"] == 4
    assert found["plays-map-id-clash"] == {"file": "playlists/lots.playlist.json", "maps": "map_0_0.map and "
                                           "map_0_1.map", "id": 7}
    assert found["plays-layout-objects"]["objects"] == "10,005"
    assert (found["plays-layout-other-version"]["made"], found["plays-layout-other-version"]["now"]) == (31, 32)
    assert found["plays-options-own-rules"]["game"] == "Halo: Combat Evolved"
    assert (found["plays-label-refused"]["label"], found["plays-label-refused"]["taken"]) == ("Vanilla characters",
                                                                                              "Halo 3")
    assert found["plays-same-name"]["count"] == 2
    # The words each one says, from templates/warnings.toml
    warnings = Warnings(load_rules(), None)
    warnings.sync(server.name, "plays", report.findings)
    said = {w.rule.id: w.text for w in warnings.of(server.name)}
    assert "25 mod maps from 4 mods" in said["plays-too-many-mod-maps"]
    assert "Project Reclaimer 0.10 places 9,999 at most" in said["plays-layout-objects"]
    assert all(w.rule.area == "plays" for w in warnings.of(server.name))


# The program's own words ---------------------------------------------------------------------------------------------

CHECK_0108 = r"""Configuration  dedicated.toml
Skipped content file S:/probe/content\Game Modes\Bad\broken.json: Invalid custom game: EOF while parsing a value at line 1 column 57
Skipped content file S:/probe/content\Maps\Bad\ghostbase.json: Saved map belongs to a different base map
[modes] S:/probe/content\Game Modes\x.json: the game type's value instead: Unknown game option for Slayer: rounds.nope
Server         Alpha · playlists/big.playlist.json: game 2: no game type "Game Modes/Bad/broken.json"; as a variant: Game type 'Game Modes/Bad/broken.json' was not found.
Not ready:
  Alpha: playlists/big.playlist.json: game 2: no game type "Game Modes/Bad/broken.json"; as a variant: Game type 'Game Modes/Bad/broken.json' was not found.; game 4: no multiplayer map "Valhala" is installed; as a variant: Map 'Valhala' was not found. Did you mean 'Valhalla'?
"""
LIST_0108 = r"""[odst] Halo 3: ODST installed in S:/game
playlists\a01.playlist.json · played by Alpha · PROBLEM: playlists\a01.playlist.json: game 1: no multiplayer map "Valhala" is installed
playlists\a12.playlist.json · "a12" · 1 games · vote 20 s · played by Alpha
"""


def test_0108s_output_read_file_by_file_and_game_by_game() -> None:
    refusals = _refusals(CHECK_0108, "Alpha")
    assert [(p.file, p.game) for p in refusals] == [("playlists/big.playlist.json", 2), ("playlists/big.playlist.json", 4)]
    assert skipped_files(CHECK_0108) == {
        "S:/probe/content/Game Modes/Bad/broken.json": "Invalid custom game: EOF while parsing a value at line 1 column 57",
        "S:/probe/content/Maps/Bad/ghostbase.json": "Saved map belongs to a different base map"}
    assert mode_notes(CHECK_0108) == [("S:/probe/content/Game Modes/x.json",
                                       "the game type's value instead: Unknown game option for Slayer: rounds.nope")]
    listed = listed_problems(LIST_0108)
    assert list(listed) == ["playlists/a01.playlist.json"]
    assert listed["playlists/a01.playlist.json"][0].game == 1
    verdict = Verdict(refusals, skipped_files(CHECK_0108), mode_notes(CHECK_0108), listed, version="0.10.8")
    assert Verdict.from_json(json.loads(json.dumps(verdict.to_json()))) == verdict


def test_the_programs_verdict_is_tied_to_the_game_with_why_its_file_is_left_out(alpha: Any) -> None:
    """A refusal says "not found"; the "Skipped content file" line says why. Paired by path (README section 3)."""
    settings, server = alpha
    write(server.content_dir / "Game Modes" / "broken.json", "{not json")
    write_playlist(server.folder, "big", [{"map": "Guardian", "game": "Slayer"},
                                          {"map": "Guardian", "game": "Game Modes/broken.json"},
                                          {"map": "Valhalla", "game": "Slayer"}])
    write_playlist(server.folder, "spare", [{"map": "Atlantis", "game": "Slayer"}, {"map": "Guardian", "game": "Slayer"},
                                            {"map": "Valhalla", "game": "Slayer"}])
    write_plays(server.config_path, server.name, {"playlists": [{"file": "playlists/big.playlist.json"}]})
    before = server.config_path.read_bytes()
    verdict = plays_verdict(fake_tool(), server)
    assert server.config_path.read_bytes() == before and not list(server.folder.glob(".huragok-check*"))
    assert [(p.file, p.game) for p in verdict.refusals] == [("playlists/big.playlist.json", 2)]
    assert any(path.endswith("Game Modes/broken.json") for path in verdict.skipped)
    report = server_files(server, verdict=verdict)
    game = game_of(report, "big", 2)
    assert game.status == "refused" and "left out: Invalid custom game" in game.reason
    assert ("plays-refused", "playlists/big.playlist.json#2") in rules_found(report)
    spare = next(p for p in report.playlists if p.relative == "playlists/spare.playlist.json")
    assert spare.status == "refused" and any(rule == "plays-refused-unused" for rule, _ in rules_found(report))
    assert {m.relative: m.status for m in report.game_modes}["Game Modes/broken.json"] == "skip"


def test_b1_the_check_of_a_change_takes_playlists_out_of_the_draft(alpha: Any) -> None:
    """B1: the draft kept `playlists` beside the new kind, which the program refuses ("choose one of playlist,
    playlists, variant, or map and game"), so every change was refused."""
    settings, server = alpha
    write_plays(server.config_path, server.name, {"playlists": [{"file": "playlists/slayer.playlist.json"}]})
    assert check_plays(fake_tool(), server, {"map": "Valhalla", "game": "Slayer"}).ok


def test_b8_saved_maps_are_imported_into_maps(tmp_path: Path) -> None:
    from huragok.content.mccimport import _json_kind

    assert _json_kind(write(tmp_path / "GroundWar.json", layout("GroundWar"))) == "Maps"
    assert _json_kind(write(tmp_path / "ctf.json", json.dumps({"name": "CTF", "mode": "CaptureTheFlag", "rules": {},
                                                                 "map": None}))) == "Game Modes"
    assert _json_kind(write(tmp_path / "x.json", json.dumps({"name": "?"}))) is None


# Warnings set and cleared -------------------------------------------------------------------------------------------

def watch_on(settings: Any) -> tuple[PlaysWatch, Warnings, EventBus]:
    bus = EventBus()
    warnings = Warnings(load_rules(), settings.records, bus)
    return PlaysWatch(settings, bus, warnings, tool_for=lambda _s, _server: fake_tool()), warnings, bus


def ids(warnings: Warnings, server: str) -> set[str]:
    return {w.rule.id for w in warnings.of(server)}


def test_what_it_finds_is_a_warning_until_its_put_right(alpha: Any) -> None:
    settings, server = alpha
    watch, warnings, _ = watch_on(settings)
    path = write_playlist(server.folder, "slayer", [{"map": "Guardian", "game": "Slayer", "preview_image": "x.png"},
                                                    {"map": "Valhalla", "game": "Slayer"}, {"map": "The Pit", "game": "Slayer"}])
    first = fingerprint(server)
    watch.refresh(server)
    assert "plays-image" in ids(warnings, server.name)
    write(server.content_dir / "Images" / "x.png", b"\x89PNG")
    assert fingerprint(server) != first  # the watch reads it again
    watch.refresh(server)
    assert "plays-image" not in ids(warnings, server.name)
    # The program's own check, kept for any window
    verdict = watch.program_check(server)
    assert verdict.asked and load_verdict(settings.records, server.name) == verdict
    path.write_text(json.dumps({"games": [{"map": "Atlantis", "game": "Slayer"}, {"map": "Guardian", "game": "Slayer"},
                                          {"map": "Valhalla", "game": "Slayer"}]}), encoding="utf-8")
    watch.program_check(server)
    watch.refresh(server)
    assert "plays-refused" in ids(warnings, server.name)
    refused = next(w for w in warnings.of(server.name) if w.rule.id == "plays-refused")
    assert refused.rule.discord and "Atlantis" in refused.text and "keeps playing what it played before" in refused.text


def test_a_game_skipped_is_seen_until_its_skip_ends_or_the_server_starts(alpha: Any) -> None:
    settings, server = alpha
    watch, warnings, bus = watch_on(settings)
    watch.refresh(server)
    bus.emit("server.game_skip", server.name, what="skipped", game="Slayer on Guardian", times=3,
             reason="its cache is damaged", skip="30 minutes")
    bus.emit("log.game_skip", server.name, what="skipped", game="Slayer on Guardian", times=3,
             reason="its cache is damaged", skip="30 minutes")  # the same line from its log: once
    skipped = [w for w in warnings.of(server.name) if w.rule.id == "plays-game-skipped"]
    assert len(skipped) == 1 and skipped[0].rule.discord and skipped[0].kind == "seen"
    assert "Slayer on Guardian was skipped at" in skipped[0].text and "skips it for 30 minutes" in skipped[0].text
    report = watch.reports[server.name]
    assert game_of(report, "slayer", 1).status == "skipped"  # the example rotation's Guardian Slayer
    bus.emit("log.game_skip", server.name, what="skip_ended", game="Slayer on Guardian")
    assert "plays-game-skipped" not in ids(warnings, server.name)
    bus.emit("server.game_skip", server.name, what="unplayable", game="Forge on Big", reason="too big")
    assert "plays-game-unplayable" in ids(warnings, server.name)
    bus.emit("server.started", server.name, pid=1)
    watch.refresh(server)
    assert not ids(warnings, server.name) & {"plays-game-unplayable", "plays-game-skipped"}
    kept = filehealth.load_skips(settings.records, server.name)
    assert [(s.what, s.active) for s in kept] == [("skipped", False), ("unplayable", False)]  # kept a week, for G


def test_a_retry_passes_and_a_refused_reload_lasts_until_one_goes_through(alpha: Any) -> None:
    settings, server = alpha
    clock = [1000.0]
    watch, warnings, bus = watch_on(settings)
    watch.clock = lambda: clock[0]
    bus.emit("server.game_skip", server.name, what="retry", game="Slayer on Guardian", times=1, reason="damaged")
    assert "plays-game-retried" in ids(warnings, server.name)
    clock[0] += 31 * 60
    watch.refresh(server)
    assert "plays-game-retried" not in ids(warnings, server.name)
    bus.emit("rcon.control", server.name, action="reload", ok=False, text="playlists/x.playlist.json: game 2: …")
    assert "plays-reload-refused" in ids(warnings, server.name)
    watch.refresh(server)  # its own source: the file health's sync doesn't clear it
    assert "plays-reload-refused" in ids(warnings, server.name)
    bus.emit("rcon.control", server.name, action="reload", ok=True, text="applied")
    assert "plays-reload-refused" not in ids(warnings, server.name)


def test_skips_round_trip() -> None:
    skip = SkipSeen("skipped", "Slayer on Guardian", 5.0, "x", 3, "30 minutes", True)
    assert SkipSeen.from_json(skip.to_json()) == skip
    assert Problem("a", 2, "playlists/x.playlist.json").file == "playlists/x.playlist.json"


def test_the_screens_mark_each_playlist_of_playlists_in_use(alpha: Any) -> None:
    """B1: G's "◄ in use", the copy's "plays it now" and the V card know `playlists = [...]`."""
    from huragok.window import statuscard
    from huragok.window.screens.setup import _plays_playlist, _playlist_uses

    settings, server = alpha
    small = write_playlist(server.folder, "small", [{"map": "Guardian", "game": "Slayer"}],
                           vote_seconds=20)
    write_playlist(server.folder, "big", [{"campaign": {"mission": 3010}}])
    write_plays(server.config_path, server.name, {"playlists": [
        {"file": "playlists/small.playlist.json", "players": "1-8"}, {"file": "playlists/big.playlist.json"}]})
    assert sorted(_playlist_uses(server).values()) == ["playlists", "playlists, 1-8"]
    assert _plays_playlist(server, small) and not _plays_playlist(server, server.folder / "playlists" / "slayer.playlist.json")
    plays = dict(statuscard.build(settings, server, {"status": "stopped"}))["What it plays"]
    texts = [text for _, text, _ in plays]
    assert texts[0] == "playlists by lobby size: small 1-8, big"
    assert 'small 1-8: "small" · 1 games · 20 s votes' in texts and 'big: "big" · 1 games · 20 s votes' in texts


def test_any_window_makes_the_same_report_from_what_huragok_kept(alpha: Any) -> None:
    """A second window has no running watch: report_for reads the verdict and skips Huragok kept in records/."""
    settings, server = alpha
    watch, _, bus = watch_on(settings)
    watch.program_check(server)
    bus.emit("server.game_skip", server.name, what="skipped", game="Slayer on Guardian", times=3, reason="damaged",
             skip="30 minutes")
    report = filehealth.report_for(settings, server)
    assert report.verdict is not None and report.verdict.asked
    assert [s.game for s in report.skips if s.active] == ["Slayer on Guardian"]
    assert game_of(report, "slayer", 1).skips and game_of(report, "slayer", 1).status == "skipped"
    old = SkipSeen("retry", "Slayer on Guardian", 0.0, "damaged", 1)
    assert filehealth.current_skips([old], now=31 * 60)[0].active is False
