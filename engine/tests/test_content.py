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

from huragok import cli, steamsetup
from huragok.catalogue import install_layouts, installed_mods, load_catalogue, parse_listing, parse_picks
from huragok.config import load
from huragok.contentimport import content_root_of
from huragok.menu import Menu
from huragok.playlists import PlaylistError, load_playlist, new_playlist, resolve
from huragok.tomledit import read_toml, set_table_key
from huragok.tui import RecentLog

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
    make_server_folder(folder, "Slayer", free_port())
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
    assert status.executable is None and status.accounts == [] and "isn't installed" in status.summary()
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
    menu = Menu(controller, terminal, StrictKeyboard(["I", "1", "ENTER", "N", "x", "ESC", "Q", "Y"]), RecentLog())  # type: ignore[arg-type]
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
    from huragok.playlists import problems_from_show

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


# The window's screens, fixed after the 2026-10-18 audit -----------------------------------------------------

def screen(settings_path: Path, keys: list[str], terminal: FakeTerminal | None = None) -> tuple[Menu, FakeTerminal]:
    terminal = terminal or FakeTerminal()
    return Menu(SetupController(settings_path), terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


def on_the_loop() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()  # asyncio.run's loop runs on the main thread


def test_the_playlist_a_server_plays_is_checked_before_it_is_replaced(tmp_path: Path, run) -> None:
    from test_setup import fake_tool as tool

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    assert server.plays == "playlist slayer"
    path = server.folder / "playlists" / "slayer.playlist.json"
    before = path.read_text(encoding="utf-8")
    playlist = load_playlist(path)
    playlist.add("Nowhere Land", "Slayer")  # a map the (fake) server doesn't have
    menu, terminal = screen(settings_path, ["L"])  # no "Keep it as it is" for the playlist it plays
    assert run(menu._save_playlist(tool(), server, playlist)) is False
    printed = "".join(terminal.output)
    assert "can't play 1 of its games" in printed and "Keep it as it is" not in printed
    assert "Not saved: Alpha Server plays this playlist, and keeps the one it has" in printed
    assert path.read_text(encoding="utf-8") == before  # the live file never held the refused draft
    assert not list(path.parent.glob(".*huragok-draft*"))
    menu, terminal = screen(settings_path, ["D", "N"])  # drop the game it can't play: checked again, then saved
    assert run(menu._save_playlist(tool(), server, playlist)) is True
    assert "Nowhere Land" not in path.read_text(encoding="utf-8") and "Saved playlists/slayer.playlist.json" in \
        "".join(terminal.output)


def test_n_asks_again_for_a_port_or_a_player_count_out_of_range(tmp_path: Path, run, monkeypatch) -> None:
    from conftest import free_port

    from huragok import menu_setup

    settings_path = setup_root(tmp_path)
    probed: list[bool] = []

    def next_free_port(settings: Any) -> int:
        probed.append(on_the_loop())
        return free_port()

    monkeypatch.setattr(menu_setup, "next_free_port", next_free_port)
    keys = ["ENTER", *"Zed", "ENTER",  # no package, the name
            *"70000", "ENTER", *"80", "ENTER", *str(free_port()), "ENTER",  # asked again until it's a port
            "N", "ENTER",  # not hidden, no join password
            *"0", "ENTER", *"500", "ENTER", "ESC"]  # players: asked again; Esc leaves
    menu, terminal = screen(settings_path, keys)
    run(menu.screen_new())
    printed = "".join(terminal.output)
    assert "'70000' isn't a port from 1024 to 65535" in printed and "'80' isn't a port from 1024 to 65535" in printed
    assert "'0' isn't a player count from 1 to 127" in printed and "'500' isn't a player count" in printed
    assert "That didn't work" not in printed and not (settings_path.parent / "SERVERS" / "Zed").exists()
    assert probed == [False]  # the port probe ran in a thread, not on the loop


def test_leaving_the_steam_sign_in_writes_nothing(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    fake_steamcmd(settings_path.parent / "steamcmd", ["lilgremlin"])
    monkeypatch.setattr(steamsetup, "signed_in", lambda executable, username, timeout=120.0: (False, ""))
    monkeypatch.setattr(steamsetup, "login_window", lambda executable, username: "a sign-in window opened")
    menu, terminal = screen(settings_path, ["1", "ENTER", "ESC", "x", "ESC"])  # the account offered; Esc at "Press Enter once…"
    run(menu.screen_steam())
    printed = "".join(terminal.output)
    assert "Left as it was: nothing was written" in printed and "Written:" not in printed
    server = load(settings_path).servers[0]
    assert "username" not in read_toml(server.config_path).get("workshop", {})
    assert not load(settings_path).setup.steam_username


def test_a_quoted_path_is_the_path(tmp_path: Path, run, monkeypatch) -> None:
    from huragok import contentimport

    settings_path = setup_root(tmp_path)
    # I -> "somewhere else": the folder as Explorer's "Copy as path" gives it, quotes and all.
    folder = tmp_path / "Program Files" / "SteamCMD"
    program = fake_steamcmd(folder, ["lilgremlin"])
    real_find = steamsetup.find_steamcmd
    monkeypatch.setattr(steamsetup, "find_steamcmd", lambda home, hint="": real_find(home, hint) if hint else (None, ""))
    monkeypatch.setattr(steamsetup, "signed_in", lambda executable, username, timeout=120.0: (True, ""))
    on_path: list[bool] = []  # PowerShell, which waits on every open window: never on the loop
    monkeypatch.setattr(steamsetup, "add_to_path", lambda folder: on_path.append(on_the_loop()) or "on your PATH")
    menu, terminal = screen(settings_path, ["1", "3", *f'"{folder}"', "ENTER", "ENTER", "Y", "x", "ESC"])
    run(menu.screen_steam())
    assert "Written: [workshop]" in "".join(terminal.output) and on_path == [False]
    assert load(settings_path).setup.steamcmd == str(program)
    # G -> Import variants, from a quoted folder; looking for MCC and reading the folder happen off the loop.
    source = tmp_path / "My Variants"
    source.mkdir()
    (source / "mlg_guardian.mvar").write_bytes(b"mvar")
    threads: list[bool] = []
    real_importable = contentimport.importable
    monkeypatch.setattr(contentimport, "find_mcc", lambda hint=None: threads.append(on_the_loop()))  # None: not found
    monkeypatch.setattr(contentimport, "importable",
                        lambda path, words=None: threads.append(on_the_loop()) or real_importable(path, words))
    settings = load(settings_path)
    menu, terminal = screen(settings_path, [*f"'{source}'", "ENTER", "ENTER", "Y"])
    run(menu._import_content(settings, settings.servers[0]))
    assert "Copied 1" in "".join(terminal.output)
    assert list(content_root_of(settings.servers[0].folder).rglob("mlg_guardian.mvar"))
    assert threads == [False, False]


def test_a_custom_game_a_server_plays_is_not_deleted(tmp_path: Path, run) -> None:
    from huragok import gameoptions as go
    from huragok.livesettings import write_plays

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    path = go.new_preset("Snipers", "Slayer").save(go.preset_path(server.content_dir, "Snipers"))
    write_plays(server.config_path, server.name, {"variant": os.path.relpath(path, server.folder).replace("\\", "/")})
    number = str([p.path for p in go.list_presets(server.content_dir)].index(path) + 1)
    settings = load(settings_path)
    menu, terminal = screen(settings_path, ["D", *number, "ENTER", "ESC"])
    run(menu._custom_games(settings, None, settings.servers[0]))
    assert path.is_file() and "Not deleted: Alpha Server plays Game Modes/snipers.json" in "".join(terminal.output)
    write_plays(server.config_path, server.name, {"playlist": "playlists/slayer.playlist.json"})
    menu, terminal = screen(settings_path, ["D", *number, "ENTER", "Y", "ESC"])
    run(menu._custom_games(load(settings_path), None, load(settings_path).servers[0]))
    assert not path.exists() and "Deleted Game Modes/snipers.json" in "".join(terminal.output)


def test_the_workshop_screen_reads_only_the_end_of_the_console_log(tmp_path: Path, run, monkeypatch) -> None:
    from huragok.catalogue import Catalogue
    from huragok.menu_setup import CONSOLE_TAIL, SetupScreens, tail_lines
    from huragok.steamsetup import SteamStatus

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    log = settings.logs / f"console-{server.slug}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    filler = "17:11:29 [Alpha Server] Bravo said something in the chat, status ok\n" * (3 * CONSOLE_TAIL // 60)
    log.write_text("17:00:00 Workshop 111: an old verdict, long gone\n" + filler
                   + "17:12:00 Workshop 2984061723: ready\n", encoding="utf-8")
    lines = tail_lines(log)
    assert lines[-1].endswith("Workshop 2984061723: ready") and sum(len(line) + 1 for line in lines) <= CONSOLE_TAIL
    assert not lines[0].startswith("Bravo") and lines[0].startswith("17:11:29")  # the cut first line is dropped
    threads: list[bool] = []

    def verdicts(self: Any, server: Any, settings: Any) -> list[str]:
        threads.append(on_the_loop())
        return SetupScreens._workshop_verdicts(self, server, settings)

    monkeypatch.setattr(Menu, "_workshop_verdicts", verdicts)
    monkeypatch.setattr(steamsetup, "status", lambda settings: SteamStatus(None, "", "", [], 0, 1))
    menu, terminal = screen(settings_path, ["ESC"])
    run(menu._workshop(settings, fake_tool(), server, Catalogue()))
    printed = "".join(terminal.output)
    assert "2984061723: ready" in printed and "111" not in printed and threads == [False]


def test_odst_progress_from_the_worker_thread_is_printed_on_the_loop(tmp_path: Path, run, monkeypatch) -> None:
    from huragok import menu_setup

    class Watched(FakeTerminal):
        threads: list[bool] = []

        def print_parts(self, *args: Any, **options: Any) -> None:
            self.threads.append(on_the_loop())
            super().print_parts(*args, **options)

    def add_odst_files(tool: Any, game: Path, mcc: str, stage: Path, say: Any) -> tuple[int, int]:
        say("copying Halo 3: ODST's files (about 3.5 GB)")  # from the worker thread, as the real one does
        return 3, 0

    monkeypatch.setattr(menu_setup, "has_odst_files", lambda game: False)
    monkeypatch.setattr(menu_setup, "odst_mcc", lambda hint: tmp_path / "MCC")
    monkeypatch.setattr(menu_setup, "add_odst_files", add_odst_files)
    settings_path = setup_root(tmp_path)
    terminal = Watched()
    menu, _ = screen(settings_path, ["Y"], terminal)
    assert run(menu._offer_odst_files(load(settings_path), tmp_path / "game")) is True
    printed = "".join(terminal.output)
    assert "copying Halo 3: ODST's files" in printed and "keys pressed meanwhile are ignored" in printed
    assert terminal.threads and all(terminal.threads)  # every line printed on the loop, none from the thread
    assert menu.keyboard.drained == 1  # keys pressed during the copy were dropped


# A server's own content is hard links: writing into it never changes the shared copy ----------------

def test_writing_into_an_own_content_folder_leaves_the_shared_file(tmp_path: Path) -> None:
    from huragok.contentimport import Importable, copy_replacing, import_content
    from huragok.serverops import own_content

    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    shared = root / "content"
    (shared / "Maps").mkdir(parents=True)
    (shared / "Maps" / "From MCC").mkdir()
    (shared / "Maps" / "From MCC" / "pit.mvar").write_bytes(b"SHARED")
    (shared / "Maps" / "arena.json").write_bytes(b"SHARED")
    config = root / "SERVERS" / "Alpha Server" / "dedicated.toml"
    from huragok.tomledit import set_top_key

    set_top_key(config, "content", "../../content")
    server = load(settings_path).servers[0]
    own = own_content(load(settings_path), server, lambda _: None)
    new = tmp_path / "pit.mvar"
    new.write_bytes(b"NEW FROM MCC")
    copied, _ = import_content([Importable(new, "Maps")], own, overwrite=True)
    assert copied[0].read_bytes() == b"NEW FROM MCC"
    assert (shared / "Maps" / "From MCC" / "pit.mvar").read_bytes() == b"SHARED"
    copy_replacing(new, own / "Maps" / "arena.json")
    assert (shared / "Maps" / "arena.json").read_bytes() == b"SHARED"
    assert not list((own / "Maps").glob(".*huragok-new"))


def test_a_broken_mod_info_never_hides_the_other_mods(tmp_path: Path) -> None:
    content = tmp_path / "content"
    fake_mod(content / "Mods" / "Good", "Good Mod")
    for name, info in (("StringTitle", {"Title": "Plain Title", "ModIdentifier": {"HostedModIds": {
            "SteamWorkshopId": "3014096841"}}}),
                       ("NotADict", ["a", "list"]), ("BadId", {"Title": {"Neutral": "Bad Id"},
                                                            "ModIdentifier": {"HostedModIds": {"SteamWorkshopId": "x"}},
                                                            "GameModContents": {"MultiplayerMaps": "not a list"}})):
        (content / "Mods" / name).mkdir(parents=True)
        (content / "Mods" / name / "ModInfo.json").write_text(json.dumps(info), encoding="utf-8")
    mods = {m.folder.name: m for m in installed_mods(content)}
    assert sorted(mods) == ["BadId", "Good", "StringTitle"]  # the list one isn't a ModInfo: skipped
    assert mods["StringTitle"].title == "Plain Title" and mods["StringTitle"].workshop_id == 3014096841
    assert mods["BadId"].workshop_id is None and mods["BadId"].maps == []


def test_layouts_of_a_mod_with_an_odd_title_stay_in_maps(tmp_path: Path) -> None:
    content = tmp_path / "content"
    for title in ("..", "CON", "a/../../b", "  .  "):
        folder = fake_mod(content / "Mods" / f"m{len(title)}{title.count('.')}", title, layouts=1)
        (folder / "Project Reclaimer Layouts" / "0.json").write_text(json.dumps({"name": ".."}), encoding="utf-8")
    for mod in installed_mods(content):
        for path in install_layouts(mod, content, overwrite=True):
            assert path.resolve().parent.parent == (content / "Maps").resolve(), path
            assert path.parent.name not in ("", ".", "..", "CON") and path.name not in ("..json", ".json")


def steam_library(library: Path, apps: list[str]) -> str:
    """One library's entry in libraryfolders.vdf, as Steam writes it (backslashes doubled)."""
    escaped = str(library).replace("\\", "\\\\")
    listed = "".join(f'\t\t\t"{app}"\t\t"1000"\n' for app in apps)
    return f'\t\t"path"\t\t"{escaped}"\n\t\t"label"\t\t""\n\t\t"apps"\n\t\t{{\n{listed}\t\t}}\n'


def test_mcc_is_found_only_where_steam_says_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Steam's own records (libraryfolders.vdf: each library and its games; appmanifest_976730.acf: MCC's folder)
    and nothing else: no drive is searched, no folder guessed, a library Steam says has no MCC isn't looked in."""
    from huragok import contentimport

    steam = tmp_path / "Steam"
    other = tmp_path / "Games" / "Other"
    library = tmp_path / "Games" / "SteamLibrary"
    mcc = library / "steamapps" / "common" / "MCC Installed Here"  # the folder the manifest names, not a guess
    (mcc / "halo3").mkdir(parents=True)
    decoy = other / "steamapps" / "common" / contentimport.MCC_FOLDER  # MCC's usual name, where Steam says it isn't
    (decoy / "halo3").mkdir(parents=True)
    (other / "steamapps" / f"appmanifest_{contentimport.MCC_APP}.acf").write_text(
        f'"AppState"\n{{\n\t"installdir"\t\t"{contentimport.MCC_FOLDER}"\n}}\n', encoding="utf-8")
    (library / "steamapps" / f"appmanifest_{contentimport.MCC_APP}.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"976730"\n\t"installdir"\t\t"MCC Installed Here"\n}\n', encoding="utf-8")
    (steam / "steamapps").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n' + steam_library(steam, ["228980"]) + '\t}\n\t"1"\n\t{\n'
        + steam_library(other, ["21970"]) + '\t}\n\t"2"\n\t{\n' + steam_library(library, ["976730", "1695794"])
        + '\t}\n}\n', encoding="utf-8")
    monkeypatch.setattr(contentimport, "_steam_roots", lambda: [steam])
    assert contentimport.find_mcc() == mcc
    assert [lib for lib, _ in contentimport.steam_libraries()] == [steam, other, library]  # each once
    assert contentimport.find_mcc(f'"{mcc}"') == mcc  # Explorer's "Copy as path" keeps its quotes
    assert contentimport.clean_path("  '\"C:\\x y\"'  ") == "C:\\x y"
    # The list from before 2021 ("1" "D:\\SteamLibrary", no apps): the manifest alone says where MCC is.
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"LibraryFolders"\n{\n\t"contentstatsid"\t\t"1"\n\t"1"\t\t"' + str(library).replace("\\", "\\\\")
        + '"\n}\n', encoding="utf-8")
    assert contentimport.find_mcc() == mcc
    # No record names it: not found, whatever folders exist on the PC.
    (library / "steamapps" / f"appmanifest_{contentimport.MCC_APP}.acf").unlink()
    assert contentimport.find_mcc() is None
    assert not hasattr(contentimport, "_fixed_drives")  # there's no drive search to fall back on


def test_the_steam_sign_in_takes_only_a_steam_account_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "steam cmd's" / steamsetup.executable_name()
    executable.parent.mkdir()
    executable.write_bytes(b"")
    opened: list[Any] = []
    monkeypatch.setattr(steamsetup.subprocess, "Popen", lambda *a, **k: opened.append((a, k)))
    for bad in ("bob & calc", 'bob"', "bob|x", "x", "a;b", "bob's"):
        assert "isn't a Steam account name" in steamsetup.login_window(executable, bad)
        assert steamsetup.signed_in(executable, bad)[0] is False
    assert opened == []
    assert steamsetup.check_username("Lil_Gremlin42") is None
    found, _ = steamsetup.find_steamcmd(tmp_path, f'"{executable}"')  # a pasted, quoted path
    assert found == executable
    if os.name != "nt":
        monkeypatch.setattr(steamsetup.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xterm" else None)
        monkeypatch.setenv("DISPLAY", ":0")
        steamsetup.login_window(executable, "bob")
        import shlex

        script = opened[-1][0][0][-1]
        assert shlex.split(script)[:2] == ["sh", "-c"] and str(executable) in shlex.split(script)[2]
