"""Seeing what's there and choosing from it: the catalogue, the playlist builder and editor, mods' Forge
layouts, and SteamCMD setup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import free_port
from test_menu import FakeTerminal
from test_setup import SetupController, StrictKeyboard, fake_tool, make_server_folder, setup_root

from reclaimer_supervisor import cli, steamsetup
from reclaimer_supervisor.catalogue import install_layouts, installed_mods, load_catalogue, parse_listing, parse_picks
from reclaimer_supervisor.config import load
from reclaimer_supervisor.contentimport import content_root_of
from reclaimer_supervisor.menu import Menu
from reclaimer_supervisor.playlists import PlaylistError, load_playlist, new_playlist, resolve
from reclaimer_supervisor.tomledit import read_toml, set_table_key
from reclaimer_supervisor.tui import RecentLog

REAL_LISTING = '''Maps: map = "…" (and base_map), or a playlist game's "map"
  "Assembly"      or "descent"
  "Guardian"
  "The Pit"       or "cyberdyne"
  "HugeGrass Rebalance · Custom"  mod · HugeGrass Rebalance
Map variants: map = "…"; each plays on its own map
  From Halo 3:
  "MLG Guardian 8"   map variant · Guardian
  From your content folder:
  "MLG Amp V7"                                map variant · Foundry
  "Maps/From MCC/mlg_amplified_v8_012.mvar"   "MLG Amplified 8" · Foundry
Game types: game = "…", or a playlist game's "game"
  "Slayer"
  "Capture the Flag"
Game variants: game = "…"
  From your content folder:
  "Gun Game"  game variant · Slayer
  From Halo 3:
  "h3_4v4_hardcoreFlag_pit_3points.bin"      "HARDCORE CTF" · Capture the Flag
  "HARDCORE TS"                              game variant · Slayer

Names ignore case. Put map variants in content\\Maps; Workshop mods appear once installed (dedicated mods).'''


def test_the_catalogue_reads_the_real_listing() -> None:
    catalogue = parse_listing(REAL_LISTING)
    assert [(e.name, e.reference) for e in catalogue.maps] == [("Assembly", "descent"), ("Guardian", "Guardian"),
                                                              ("The Pit", "cyberdyne")]
    assert [(e.name, e.source) for e in catalogue.mod_maps] == [("HugeGrass Rebalance · Custom", "mod HugeGrass Rebalance")]
    variants = {e.name: (e.reference, e.base, e.source) for e in catalogue.map_variants}
    assert variants["MLG Guardian 8"] == ("MLG Guardian 8", "Guardian", "Halo 3")
    assert variants["MLG Amp V7"] == ("MLG Amp V7", "Foundry", "your content folder")
    assert variants["MLG Amplified 8"] == ("Maps/From MCC/mlg_amplified_v8_012.mvar", "Foundry", "your content folder")
    assert [e.name for e in catalogue.game_types] == ["Slayer", "Capture the Flag"]
    games = {e.name: (e.reference, e.base) for e in catalogue.game_variants}
    assert games["HARDCORE CTF"] == ("h3_4v4_hardcoreFlag_pit_3points.bin", "Capture the Flag")
    assert games["Gun Game"] == ("Gun Game", "Slayer") and games["HARDCORE TS"] == ("HARDCORE TS", "Slayer")
    assert catalogue.find_map("cyberdyne").name == "The Pit" and catalogue.find_game("hardcore ts") is not None
    assert "3 maps, 1 mod maps, 3 map variants, 2 game types, 3 game variants" in catalogue.counts()
    assert parse_picks("1,3 5-7", 10) == [1, 3, 5, 6, 7] and parse_picks("A", 3) == [1, 2, 3]
    assert parse_picks("", 3) == [] and parse_picks("x", 3) is None and parse_picks("9", 3) is None


def fake_mod(folder: Path, title: str, workshop_id: int | None = None, layouts: int = 2) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {"ModIdentifier": {"ModGuid": "g"}, "Engine": "Halo3", "Title": {"Neutral": title},
                            "GameModContents": {"MultiplayerMaps": ["multiplayer/map.json"]}}
    if workshop_id:
        info["ModIdentifier"]["HostedModIds"] = {"SteamWorkshopId": workshop_id}
    (folder / "ModInfo.json").write_text(json.dumps(info), encoding="utf-8")
    (folder / "multiplayer").mkdir(exist_ok=True)
    (folder / "multiplayer" / "map.json").write_text(json.dumps({"Title": {"Neutral": f"{title} Map"}}), encoding="utf-8")
    (folder / "maps").mkdir(exist_ok=True)
    (folder / "maps" / "x.map").write_bytes(b"map")
    (folder / "Project Reclaimer Layouts").mkdir(exist_ok=True)
    for number in range(layouts):
        (folder / "Project Reclaimer Layouts" / f"{number}.json").write_text(
            json.dumps({"version": 1, "name": f"Layout {number}", "base_map": {"stem": "x"}}), encoding="utf-8")
    return folder


def test_mods_and_their_layouts(tmp_path: Path) -> None:
    content = tmp_path / "content"
    fake_mod(content / "Mods" / "HugeGrass", "HugeGrass Rebalance")
    fake_mod(content / "Mods" / "Workshop" / "3820687243", "Remix", 3820687243, layouts=1)
    mods = installed_mods(content)
    assert [(m.title, m.workshop_id, m.maps, [n for n, _ in m.layouts]) for m in mods] == [
        ("HugeGrass Rebalance", None, ["HugeGrass Rebalance Map"], ["Layout 0", "Layout 1"]),
        ("Remix", 3820687243, ["Remix Map"], ["Layout 0"])]
    assert "Workshop 3820687243" in mods[1].label and "2 Forge layout(s)" in mods[0].label
    copied = install_layouts(mods[0], content)
    assert [p.name for p in copied] == ["Layout 0.json", "Layout 1.json"] and copied[0].parent.name == "HugeGrass Rebalance"
    assert install_layouts(mods[0], content) == []  # already there


def test_building_and_editing_playlists(tmp_path: Path) -> None:
    folder = tmp_path / "Slayer"
    make_server_folder(folder, "Slayer", 49190)
    playlist = new_playlist(folder, "Friday Night!", vote_seconds=30)
    assert playlist.path == folder / "playlists" / "friday-night.playlist.json"
    assert playlist.add("Guardian", "team_slayer") and not playlist.add("Guardian", "team_slayer")
    with pytest.raises(PlaylistError, match="3 or more"):
        playlist.save()
    playlist.add("The Pit", "team_slayer")
    playlist.add("cyberdyne", "rockets", name="Rockets!", weight=2)
    playlist.set_weight(1, 3)
    playlist.save()
    loaded = load_playlist(playlist.path)
    assert loaded.name == "Friday Night!" and loaded.vote_seconds == 30
    assert loaded.games == [{"map": "Guardian", "game": "team_slayer", "weight": 3}, {"map": "The Pit", "game": "team_slayer"},
                            {"map": "cyberdyne", "game": "rockets", "name": "Rockets!", "weight": 2}]
    assert loaded.labels()[2] == "Rockets! on cyberdyne (rockets) · weight 2"
    assert loaded.remove([1, 9]) == 1 and len(loaded.games) == 2
    loaded.set_weight(2, 1)
    assert "weight" not in loaded.games[1]
    with pytest.raises(PlaylistError):
        loaded.set_weight(1, 500)
    assert resolve(folder, "friday-night") == playlist.path and resolve(folder, "Friday Night!") == playlist.path
    assert resolve(folder, "playlists/friday-night.playlist.json") == playlist.path
    assert fake_tool().playlist_show(folder, "friday-night").ok  # the (fake) program accepts the file


def test_the_content_screens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    settings_path = setup_root(tmp_path)
    cli.write_files(settings_path.parent)
    root = settings_path.parent
    content = content_root_of(root / "SERVERS" / "Alpha Server")
    fake_mod(content / "Mods" / "HugeGrass", "HugeGrass Rebalance")
    monkeypatch.setattr(steamsetup, "find_steamcmd", lambda home, hint="": (None, ""))
    controller = SetupController(settings_path)
    keys = [
        "G", "1", "ENTER",  # Alpha; the catalogue is read
        "1", "1", "ESC",  # browse: Halo 3's maps, back to the content menu
        "2",  # playlists
        "B", *"Night", "ENTER", "ENTER",  # build: name, vote 20 s
        "ENTER", "ENTER", *"1-4", "ENTER",  # maps: no words to narrow, next page, pick the first four
        *"1", "ENTER",  # game types: Slayer
        "ENTER",  # leave none out
        *"1=2", "ENTER",  # weights
        "Y",  # use it for this server
        "E", "1", "ENTER", "R", *"4", "ENTER", "S", "N",  # edit it: remove the fourth game, save, don't switch
        "ESC",  # playlists -> content menu
        "3", "L", "1", "ENTER", "ESC",  # Workshop and mods: install mod 1's Forge layouts, back
        "ESC",  # leave G
        "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    try:
        assert run(menu.run()) == "quit"
    except RuntimeError as error:
        raise AssertionError(f"{error}\n{''.join(terminal.output)}") from None
    printed = "".join(terminal.output)
    assert "Halo 3's maps" in printed and "[  1] Assembly  [descent]" in printed
    playlist = load_playlist(root / "SERVERS" / "Alpha Server" / "playlists" / "night.playlist.json")
    assert playlist.name == "Night" and len(playlist.games) == 3  # four built, one removed in the editor
    assert playlist.games[0] == {"map": "descent", "game": "Slayer", "weight": 2}
    assert "Every game is installed" in printed and "now plays playlists/night.playlist.json" in printed
    data = read_toml(root / "SERVERS" / "Alpha Server" / "dedicated.toml")
    assert data["server"][0]["playlist"] == "playlists/night.playlist.json"
    assert "SteamCMD isn't installed" in printed and "HugeGrass Rebalance" in printed
    assert (content / "Maps" / "HugeGrass Rebalance" / "Layout 0.json").is_file() and "Copied 2 layout(s)" in printed
    transcript = (root / "logs" / "window.log").read_text(encoding="utf-8")  # what was shown, asked and answered
    assert "key G =====" in transcript and "Which maps?" in transcript and "> 1-4" in transcript
    assert "Playlist name (shown in the lobby): > Night" not in transcript and "> Night" in transcript
    assert "back to the dashboard after" in transcript and "Every game is installed" in transcript


# SteamCMD ------------------------------------------------------------------------------------------

def fake_steamcmd(folder: Path, accounts: list[str]) -> Path:
    """A stand-in SteamCMD: prints what the real one prints for a cached sign-in, and keeps a config.vdf."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config").mkdir(exist_ok=True)
    entries = "".join(f'\t\t\t"{a}"\n\t\t\t{{\n\t\t\t\t"SteamID"\t\t"1"\n\t\t\t}}\n' for a in accounts)
    (folder / "config" / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"Accounts"\n\t\t\t\t{\n'
        + entries.replace("\t\t\t", "\t\t\t\t\t") + '\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n', encoding="utf-8")
    if os.name == "nt":
        program = folder / "steamcmd.exe"
        # A .exe name is what's looked for; a batch file renamed .exe doesn't run, so install a tiny launcher instead.
        program = folder / "steamcmd.exe"
        script = folder / "steamcmd.cmd"
        script.write_text("@echo off\r\necho Loading Steam API...OK\r\necho Waiting for user info...OK\r\n", encoding="utf-8")
        program.write_bytes(b"")  # found by name; `run` is replaced in the test
    else:
        program = folder / "steamcmd.sh"
        program.write_text("#!/bin/sh\necho 'Loading Steam API...OK'\necho 'Waiting for user info...OK'\n", encoding="utf-8")
        program.chmod(0o755)
    return program


def test_steamcmd_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    status = steamsetup.status(settings)
    program = fake_steamcmd(tmp_path / "reclaimer" / "steamcmd", ["lilgremlin", "other"])
    monkeypatch.setattr(steamsetup, "run", lambda exe, words, timeout=600.0: type("R", (), {
        "returncode": 0, "stdout": "Loading Steam API...OK\nWaiting for user info...OK\n"})())
    status = steamsetup.status(settings)
    assert status.executable == program and status.where == "the root's steamcmd folder"
    assert status.accounts == ["lilgremlin", "other"] and not status.ready and "no Steam account set" in status.summary()
    assert steamsetup.signed_in(program, "LilGremlin") == (True, "Loading Steam API...OK\nWaiting for user info...OK\n")
    assert steamsetup.signed_in(program, "nobody")[0] is False
    written = steamsetup.configure(settings, program, "lilgremlin")
    assert len(written) == 1
    data = read_toml(written[0])
    assert data["workshop"] == {"steamcmd": str(program), "username": "lilgremlin"} and data["master"]["port"] == 49175
    fresh = load(settings_path)
    assert fresh.setup.steamcmd == str(program) and fresh.setup.steam_username == "lilgremlin"
    status = steamsetup.status(fresh)
    assert status.ready and status.servers_configured == 1 and "account lilgremlin" in status.summary()
    assert cli.main(["-c", str(settings_path), "steam"]) == 0
    assert "account lilgremlin" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "steam", "login", "lilgremlin"]) == 0
    assert "signed in" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "steam", "install"]) == 0  # already there
    assert "already at" in capsys.readouterr().out
    # The window's I screen with everything in place: account offered, checked, written.
    controller = SetupController(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["I", "ENTER", "N", "x", "Q", "Y"]), RecentLog())  # type: ignore[arg-type]
    assert run_menu(menu) == "quit"
    printed = "".join(terminal.output)
    assert "lilgremlin is signed in" in printed and "Written: [workshop]" in printed


def run_menu(menu: Menu) -> str:
    import asyncio

    return asyncio.run(menu.run())


def test_a_new_root_with_no_steamcmd_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    settings_path = setup_root(tmp_path)
    monkeypatch.setattr(steamsetup, "find_steamcmd", lambda home, hint="": (None, ""))
    assert cli.main(["-c", str(settings_path), "steam"]) == 1
    out = capsys.readouterr().out
    assert "isn't installed" in out and "steam install" in out
    assert cli.main(["-c", str(settings_path), "steam", "login", "x"]) == 1


REAL_SHOW = '''playlists/content-test-build.playlist.json · "Content Test Build" · 5 games · vote 20 s
   1. Slayer on Hugegrass · Custom · 20%
   2. Slayer on HugeGrass Balanced by trapa · 20%
        PROBLEM: This game was made on the mod map HugeGrass Rebalance Los Angeles (fixedHugeGrassTrapasRemix.map), which this PC does not have. Install that mod, then refresh.
   3. Slayer on Spirits Mix · 20%
        PROBLEM: This game was made on the mod map HugeGrass Rebalance (fixedHugeGrassTrapasRemix.map), which this PC does not have. Install that mod, then refresh.
   4. Slayer on Assembly · 20%
   5. Slayer on Avalanche · 20%
Error: 2 of 5 games cannot be played; `dedicated content` lists the names this server has'''


def test_the_builder_names_games_the_server_cant_play_and_drops_them(tmp_path: Path, run) -> None:
    from reclaimer_supervisor.playlists import problems_from_show

    problems = problems_from_show(REAL_SHOW)
    assert set(problems) == {2, 3} and problems[2].startswith("This game was made on the mod map")
    settings_path = setup_root(tmp_path)
    cli.write_files(settings_path.parent)
    controller = SetupController(settings_path)
    folder = settings_path.parent / "SERVERS" / "Alpha Server"
    # A playlist file with two maps the (fake) server doesn't have, among four it does.
    bad = new_playlist(folder, "Mixed Bag")
    for map_ref in ("Guardian", "Nowhere Land", "The Pit", "Valhalla", "Also Missing"):
        bad.add(map_ref, "Slayer")
    bad.save()
    keys = [
        "G", "1", "ENTER", "2",  # Alpha, playlists
        "E", "1", "ENTER",  # edit mixed-bag (the first file)
        "S", "D", "Y",  # save: the server can't play two -> drop them and save again -> use it
        "ESC", "ESC",  # back out of playlists and G
        "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    try:
        assert run(menu.run()) == "quit"
    except RuntimeError as error:
        raise AssertionError(f"{error}\n{''.join(terminal.output)}") from None
    printed = "".join(terminal.output)
    assert "can't play 2 of its games" in printed and "Nowhere Land" in printed and "Drop them" in printed
    assert "Every game is installed" in printed
    saved = load_playlist(folder / "playlists" / "mixed-bag.playlist.json")
    assert [g["map"] for g in saved.games] == ["Guardian", "The Pit", "Valhalla"]
    assert read_toml(folder / "dedicated.toml")["server"][0]["playlist"] == "playlists/mixed-bag.playlist.json"
