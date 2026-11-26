"""Seeing what's there: the catalogue, a server's playlists and custom games (shown, their history put back, copied
to another server and set aside, only when the host asks), mods' Forge layouts, and SteamCMD setup."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port
from testkit import DATA, system_as
from testkit.setup import ALPHA, SetupController, StrictKeyboard, fake_tool, make_server_folder, setup_root, write_playlist
from testkit.window import FakeTerminal

from huragok import steam as steamsetup
from huragok.commandline import cli, firstrun
from huragok.content.catalogue import Catalogue, install_layouts, installed_mods, parse_listing, parse_picks
from huragok.content.mccimport import content_root_of
from huragok.core.logs import RecentLog
from huragok.core.settings import load
from huragok.core.tomledit import read_toml
from huragok.games.halo3 import load_playlist
from huragok.window.dashboard import Menu

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


def test_a_playlist_is_read_as_the_host_wrote_it(tmp_path: Path) -> None:
    folder = tmp_path / "Slayer"
    make_server_folder(folder, "Slayer", free_port())
    path = write_playlist(folder, "friday-night", [
        {"map": "Guardian", "game": "team_slayer", "weight": 3}, {"map": "The Pit", "game": "team_slayer"},
        {"map": "cyberdyne", "game": "rockets", "name": "Rockets!", "weight": 2}], name="Friday Night!", vote_seconds=30)
    text = path.read_text(encoding="utf-8")
    loaded = load_playlist(path)
    assert loaded.name == "Friday Night!" and loaded.vote_seconds == 30 and loaded.relative == "playlists/friday-night.playlist.json"
    assert loaded.labels()[2] == "Rockets! on cyberdyne (rockets) · weight 2"
    assert fake_tool().playlist_show(folder, "friday-night").ok  # the (fake) program accepts the file
    assert path.read_text(encoding="utf-8") == text  # read, never written


def test_the_content_screens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    """G, one server's home (mockups-0.7.0.md 1): what it plays, its file health, then 1 playlists, 3 maps and
    layouts, 4 mods, F its folder and E its dedicated.toml; the keys that have gone are no choices, and nothing it
    plays is changed."""
    from huragok.core import desktop

    settings_path = setup_root(tmp_path)
    firstrun.write_files(settings_path.parent)
    root = settings_path.parent
    folder = root / "SERVERS" / "Alpha Server"
    content = content_root_of(folder)
    fake_mod(content / "Mods" / "HugeGrass", "HugeGrass Rebalance")
    monkeypatch.setattr(steamsetup, "find_steamcmd", lambda home, hint="": (None, ""))
    opened: list[Path] = []
    monkeypatch.setattr(desktop, "open_path", lambda p: opened.append(p) or f"opened {p}")
    controller = SetupController(settings_path)
    before = (folder / "dedicated.toml").read_text(encoding="utf-8")
    keys = [
        "G", "1", "ENTER",  # Alpha; the catalogue is read
        "W", "P",  # gone: not choices any more (the note says where they went)
        "3", "1", "ESC",  # maps and layouts: Halo 3's maps, back to G's home
        "1",  # playlists
        "B", "E", "U",  # gone too
        "S", "1", "ENTER",  # show slayer as the server reads it
        "ESC",  # playlists -> G's home
        "4", "L", "1", "ENTER", "ESC",  # mods: install mod 1's Forge layouts, back
        "F", "E",  # its folder, its dedicated.toml
        "ESC",  # leave G
        "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    try:
        assert run(menu.run()) == "quit"
    except RuntimeError as error:
        raise AssertionError(f"{error}\n{''.join(terminal.output)}") from None
    printed = " ".join("".join(terminal.output).split())
    assert "Halo 3's maps" in printed and "[ 1] Assembly [descent]" in printed
    # Its file health: no warnings, so nothing to say and no X to dismiss one.
    assert "File health" in printed and "nothing to say" in printed and "Dismiss a warning" not in printed
    # Every removed key's place says where it went (for one release), and nothing it plays was changed.
    assert ("Playlists and custom games are made by hand now, and what it plays is set in its dedicated.toml: F opens "
            "its folder, E the file. (W, P, the builders and packages have gone; this note shows for one release.)") \
        in printed
    assert "Playlists are made and changed by hand now (F opens one in your editor)" in printed
    assert "(B, M, E and U have gone; this note shows for one release.)" in printed
    assert "What it plays: set it" not in printed and "Build one" not in printed and "Use one" not in printed
    assert "Reload the listing" not in printed  # no reload key any more
    assert "dedicated playlist show playlists/slayer.playlist.json" in printed
    assert opened == [folder, folder / "dedicated.toml"]
    assert (folder / "dedicated.toml").read_text(encoding="utf-8") == before
    assert "SteamCMD isn't installed" in printed and "HugeGrass Rebalance" in printed
    assert (content / "Maps" / "HugeGrass Rebalance" / "Layout 0.json").is_file() and "Copied 2 layout(s)" in printed
    transcript = (root / "logs" / "window.log").read_text(encoding="utf-8")  # what was shown, asked and answered
    assert "key G =====" in transcript and "Which playlist? (number)" in transcript
    assert "back to the dashboard after" in transcript


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
    config = settings.servers[0].config_path
    before = config.read_bytes()
    assert steamsetup.configure(settings, program, "lilgremlin") is None
    assert config.read_bytes() == before  # never a server's own file (0.7.0): that's a quick action the host asks for
    fresh = load(settings_path)
    assert fresh.setup.steamcmd == str(program) and fresh.setup.steam_username == "lilgremlin"
    status = steamsetup.status(fresh)
    assert status.ready and status.servers_configured == 0 and "account lilgremlin" in status.summary()
    assert cli.main(["-c", str(settings_path), "steam"]) == 0
    assert "account lilgremlin" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "steam", "login", "lilgremlin"]) == 0
    out = capsys.readouterr().out
    assert "signed in" in out and "steam lines <server>" in out and config.read_bytes() == before
    # A server's own lines: one server, shown first, written only with a yes (--yes here: no terminal to answer).
    assert cli.main(["-c", str(settings_path), "steam", "lines", ALPHA]) == 1
    out = capsys.readouterr().out
    assert "+ steamcmd = " in out and "SteamCMD on this PC" in out and '+ username = "lilgremlin"' in out
    assert "Not written" in out and config.read_bytes() == before
    assert cli.main(["-c", str(settings_path), "steam", "lines", ALPHA, "--yes"]) == 0
    assert read_toml(config)["workshop"] == {"steamcmd": str(program), "username": "lilgremlin"}
    assert read_toml(config)["master"]["port"] == 49175 and "Written in" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "steam", "lines", ALPHA, "--yes"]) == 0  # nothing to write twice
    assert "name this PC's SteamCMD and account already" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "steam", "install"]) == 0  # already there
    assert "already at" in capsys.readouterr().out
    # The window's I screen with everything in place: account offered, checked, written.
    controller = SetupController(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["I", "1", "ENTER", "N", "N", "x", "ESC", "Q", "Y"]), RecentLog())  # type: ignore[arg-type]
    assert run_menu(menu) == "quit"
    printed = "".join(terminal.output)
    assert "lilgremlin is signed in" in printed and "Written: [setup] steamcmd and steam_username" in printed


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


def test_the_programs_words_for_each_game_are_read() -> None:
    from huragok.games.halo3 import problems_from_show

    problems = problems_from_show(REAL_SHOW)
    assert set(problems) == {2, 3} and problems[2].startswith("This game was made on the mod map")


# The window's screens, fixed after the 2026-10-18 audit -----------------------------------------------------

def screen(settings_path: Path, keys: list[str], terminal: FakeTerminal | None = None) -> tuple[Menu, FakeTerminal]:
    terminal = terminal or FakeTerminal()
    return Menu(SetupController(settings_path), terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


def on_the_loop() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()  # asyncio.run's loop runs on the main thread


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
    from huragok.content import mccimport as contentimport

    settings_path = setup_root(tmp_path)
    # I -> "somewhere else": the folder as Explorer's "Copy as path" gives it, quotes and all.
    folder = tmp_path / "Program Files" / "SteamCMD"
    program = fake_steamcmd(folder, ["lilgremlin"])
    real_find = steamsetup.find_steamcmd
    monkeypatch.setattr(steamsetup, "find_steamcmd", lambda home, hint="": real_find(home, hint) if hint else (None, ""))
    monkeypatch.setattr(steamsetup, "signed_in", lambda executable, username, timeout=120.0: (True, ""))
    on_path: list[bool] = []  # PowerShell, which waits on every open window: never on the loop
    monkeypatch.setattr(steamsetup, "add_to_path", lambda folder: on_path.append(on_the_loop()) or "on your PATH")
    menu, terminal = screen(settings_path, ["1", "3", *f'"{folder}"', "ENTER", "ENTER", "Y", "N", "x", "ESC"])
    run(menu.screen_steam())
    assert "Written: [setup]" in "".join(terminal.output) and on_path == [False]
    assert load(settings_path).setup.steamcmd == str(program)
    # G -> Import variants, from a quoted folder; looking for MCC and reading the folder happen off the loop.
    source = tmp_path / "My Variants"
    source.mkdir()
    (source / "mlg_guardian.mvar").write_bytes(b"mvar")
    threads: list[bool] = []
    real_importable = contentimport.importable
    monkeypatch.setattr(steamsetup, "find_mcc", lambda hint=None: threads.append(on_the_loop()))  # None: not found
    monkeypatch.setattr(contentimport, "importable",
                        lambda path, words=None: threads.append(on_the_loop()) or real_importable(path, words))
    settings = load(settings_path)
    menu, terminal = screen(settings_path, [*f"'{source}'", "ENTER", "ENTER", "Y"])
    run(menu._import_content(settings, settings.servers[0]))
    assert "Copied 1" in "".join(terminal.output)
    assert list(content_root_of(settings.servers[0].folder).rglob("mlg_guardian.mvar"))
    assert threads == [False, False]


def test_a_custom_game_is_set_aside_never_deleted(tmp_path: Path, run) -> None:
    """B14: D on the custom games screen moves the file into "content set aside", never unlinks it. A server that
    plays it is named, with what setting it aside does, and the host asked once: a warning, not a refusal."""
    from huragok.content.catalogue import SET_ASIDE
    from huragok.games import halo3_options as go
    from testkit.setup import write_plays

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    modes = server.content_dir / "Game Modes"
    modes.mkdir(parents=True, exist_ok=True)
    path = modes / "snipers.json"
    text = json.dumps({"version": 1, "name": "Snipers", "mode": "Slayer", "rules": {}, "options": {"map.weapons": 7}})
    path.write_text(text, encoding="utf-8")
    write_plays(server.config_path, server.name, {"variant": os.path.relpath(path, server.folder).replace("\\", "/")})
    number = str([p.path for p in go.list_presets(server.content_dir)].index(path) + 1)
    settings = load(settings_path)
    menu, terminal = screen(settings_path, ["D", *number, "ENTER", "N", "ESC"])  # it plays it: asked, and left
    run(menu._custom_games(settings, None, settings.servers[0]))
    printed = " ".join("".join(terminal.output).split())
    assert path.is_file() and "Alpha Server plays Game Modes/snipers.json. Set aside, it's gone from its content" in printed
    assert "Set Game Modes/snipers.json aside anyway (moved to" in printed and "Not deleted" not in printed
    menu, terminal = screen(settings_path, ["D", *number, "ENTER", "Y", "ESC"])  # asked once: yes
    run(menu._custom_games(load(settings_path), None, load(settings_path).servers[0]))
    aside = server.content_dir.parent / SET_ASIDE / "Game Modes" / "snipers.json"
    assert not path.exists() and aside.read_text(encoding="utf-8") == text  # moved whole, never deleted
    assert "Set aside: Game Modes/snipers.json is in" in "".join(terminal.output)
    assert go.list_presets(server.content_dir) == []


def test_the_workshop_screen_reads_only_the_end_of_the_console_log(tmp_path: Path, run, monkeypatch) -> None:
    from huragok.content.catalogue import Catalogue
    from huragok.window.screens.setup import CONSOLE_TAIL, SetupScreens, tail_lines
    from huragok.steam import SteamStatus

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


def test_the_workshop_screen_says_whether_each_mod_is_good_to_go(tmp_path: Path) -> None:
    from testkit.mods import a_map, a_mod

    from huragok.window.screens.setup import SetupScreens

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    content = server.content_dir or content_root_of(server.folder, settings.config_name)
    assert SetupScreens._mod_health(server, settings) == []  # no mods: nothing to say
    a_mod(content / "Mods" / "canyon", {"canyon": a_map(9002)}, title="Canyon")
    a_mod(content / "Mods" / "listed", {"mesa": a_map(9003)}, title="Mesa", listed=[])
    assert SetupScreens._mod_health(server, settings) == [
        ("Is each mod good to go?", "white"),
        ("  Canyon: good to go · 1 map", "green"),
        ("  Mesa: 1 problem", "red"),
        ("    broken: mesa.map is skipped by the server (\"Map needs matching .mapinfo metadata\"): none of the "
         "descriptions ModInfo.json lists (MultiplayerMaps) has a ScenarioFile ending in mesa; the item's own "
         "packaging, ask its author", "red")]


def test_g_lists_a_servers_mods_as_its_disabled_mods_json_has_them(tmp_path: Path, run, monkeypatch) -> None:
    """0.7.0: a server's mods are G's (4), no longer D's (M): each mod installed, and the one the host's
    disabled-mods.json switches off said so. The file is read, never written: Huragok never switches a mod off or on."""
    settings_path = setup_root(tmp_path)
    folder = settings_path.parent / "SERVERS" / ALPHA
    for name in ("canyon", "valley"):
        mod = folder / "content" / "Mods" / name
        mod.mkdir(parents=True)
        (mod / "ModInfo.json").write_text(json.dumps({"Title": {"Neutral": name.title()}}), encoding="utf-8")
    disabled = folder / "content" / "disabled-mods.json"
    disabled.write_text('{"packages": ["Mods/valley"]}', encoding="utf-8")
    before = disabled.read_bytes()
    monkeypatch.setattr(steamsetup, "find_steamcmd", lambda home, hint="": (None, ""))  # not this PC's own SteamCMD
    menu, terminal = screen(settings_path, ["1", "ENTER", "4", "ESC", "ESC"])  # Alpha; Mods; back out of it and G
    run(menu.screen_content())
    printed = "".join(terminal.output)
    assert "Mods installed (2):" in printed and "Canyon: good to go" in printed
    assert "Valley: switched off (disabled-mods.json)" in printed
    assert disabled.read_bytes() == before  # read, never written


def test_the_workshop_screen_says_why_a_mod_map_was_skipped(tmp_path: Path) -> None:
    from testkit.mapfiles import broken, written

    from huragok.window.screens.setup import SetupScreens

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    path = written(tmp_path / "content" / "Mods" / "Workshop" / "3820687243" / "maps", broken("no scnr group"),
                   "fixed.map")
    log = settings.logs / f"console-{server.slug}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    skipped = f"Skipped map package {path}: it has no scenario\n"  # once a start: listed once
    log.write_text(f"Workshop 3820687243: installed\n{skipped}Workshop 3820687243: installed\n{skipped}",
                   encoding="utf-8")
    assert SetupScreens._workshop_verdicts(None, server, settings) == [  # type: ignore[arg-type]
        "3820687243: installed",
        "3820687243: its map fixed.map was skipped: it has no scenario: none of its 3 tag groups is a scenario "
        "('scnr'): there's no level in it. The map file itself is broken, not your setup: ask its author (Workshop "
        "3820687243) for a fixed upload; the server installs the fixed version when it comes out"]


def test_odst_progress_from_the_worker_thread_is_printed_on_the_loop(tmp_path: Path, run, monkeypatch) -> None:
    from huragok.window.screens import setup as menu_setup

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
    from huragok.content.mccimport import Importable, copy_replacing, import_content
    from huragok.content.serverops import own_content

    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    shared = root / "content"
    (shared / "Maps").mkdir(parents=True)
    (shared / "Maps" / "From MCC").mkdir()
    (shared / "Maps" / "From MCC" / "pit.mvar").write_bytes(b"SHARED")
    (shared / "Maps" / "arena.json").write_bytes(b"SHARED")
    config = root / "SERVERS" / "Alpha Server" / "dedicated.toml"
    from huragok.core.tomledit import set_top_key

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
    from huragok.content import mccimport as contentimport

    steam = tmp_path / "Steam"
    other = tmp_path / "Games" / "Other"
    library = tmp_path / "Games" / "SteamLibrary"
    mcc = library / "steamapps" / "common" / "MCC Installed Here"  # the folder the manifest names, not a guess
    (mcc / "halo3").mkdir(parents=True)
    decoy = other / "steamapps" / "common" / steamsetup.MCC_FOLDER  # MCC's usual name, where Steam says it isn't
    (decoy / "halo3").mkdir(parents=True)
    (other / "steamapps" / f"appmanifest_{steamsetup.MCC_APP}.acf").write_text(
        f'"AppState"\n{{\n\t"installdir"\t\t"{steamsetup.MCC_FOLDER}"\n}}\n', encoding="utf-8")
    (library / "steamapps" / f"appmanifest_{steamsetup.MCC_APP}.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"976730"\n\t"installdir"\t\t"MCC Installed Here"\n}\n', encoding="utf-8")
    (steam / "steamapps").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n' + steam_library(steam, ["228980"]) + '\t}\n\t"1"\n\t{\n'
        + steam_library(other, ["21970"]) + '\t}\n\t"2"\n\t{\n' + steam_library(library, ["976730", "1695794"])
        + '\t}\n}\n', encoding="utf-8")
    monkeypatch.setattr(steamsetup, "_steam_roots", lambda: [steam])
    assert steamsetup.find_mcc() == mcc
    assert [lib for lib, _ in steamsetup.steam_libraries()] == [steam, other, library]  # each once
    assert steamsetup.find_mcc(f'"{mcc}"') == mcc  # Explorer's "Copy as path" keeps its quotes
    assert contentimport.clean_path("  '\"C:\\x y\"'  ") == "C:\\x y"
    # The list from before 2021 ("1" "D:\\SteamLibrary", no apps): the manifest alone says where MCC is.
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"LibraryFolders"\n{\n\t"contentstatsid"\t\t"1"\n\t"1"\t\t"' + str(library).replace("\\", "\\\\")
        + '"\n}\n', encoding="utf-8")
    assert steamsetup.find_mcc() == mcc
    # No record names it: not found, whatever folders exist on the PC.
    (library / "steamapps" / f"appmanifest_{steamsetup.MCC_APP}.acf").unlink()
    assert steamsetup.find_mcc() is None
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
        system_as(monkeypatch, "linux")  # a Mac signs in in Terminal instead (test_macos.py)
        monkeypatch.setattr(steamsetup.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xterm" else None)
        monkeypatch.setenv("DISPLAY", ":0")
        steamsetup.login_window(executable, "bob")
        import shlex

        script = opened[-1][0][0][-1]
        outer = shlex.split(script)
        assert outer[:2] == ["sh", "-c"] and shlex.split(outer[2])[0] == str(executable)  # quoted for sh, apostrophe too


def test_halo_3s_own_game_variants_are_offered_though_the_listing_only_counts_them() -> None:
    """0.9.20 on: "49 from Halo 3 (search to list them …)". One search per vowel lists them; each is offered once."""
    from types import SimpleNamespace

    from huragok.content.catalogue import SEARCH_WORDS, load_catalogue

    main = (DATA / "content-with-odst-0.9.20.txt").read_text(encoding="utf-8")
    searched = (DATA / "content-search-o-0.9.21.txt").read_text(encoding="utf-8")
    asked: list[list[str] | None] = []

    class Tool:
        def content(self, folder: Path, words: list[str] | None = None) -> Any:
            asked.append(words)
            return SimpleNamespace(ok=True, text=searched if words and words[0] else main, problem="")

    catalogue = load_catalogue(Tool(), Path("."), Path("content"), Path("dedicated.toml"))  # type: ignore[arg-type]
    assert asked == [[""], *([w] for w in SEARCH_WORDS)]  # 0.9 takes the empty word as none: its plain listing
    names = [e.name for e in catalogue.game_variants]
    assert names[:6] == ["Flood Infection", "Growth", "Gun Game", "One in the Chamber", "UNSC vs Covenant",
                         "Zone Control"]  # the content folder's first, as listed
    assert {"HARDCORE TS", "rocketball", "one_flag", "lowball"} <= set(names) and len(names) == len(set(
        e.reference.lower() for e in catalogue.game_variants))
    ctf = [e for e in catalogue.game_variants if e.name == "HARDCORE CTF"]
    assert len(ctf) == 3 and all(e.reference.endswith(".bin") and e.source == "Halo 3" for e in ctf)



def test_content_files_the_server_cant_read_are_named_and_set_aside(tmp_path: Path, capsys) -> None:
    """The live server's two GUID-named files (not MCC variants: "Expected an MCC BLF variant file") were skipped at
    every start. `check` names them; --set-aside moves them beside the content folder, deleting nothing."""
    from testkit.setup import setup_root

    from huragok.commandline import cli
    from huragok.content.catalogue import SET_ASIDE, set_aside, unreadable_variants
    from huragok.core.settings import load

    settings_path = setup_root(tmp_path)
    content = load(settings_path).servers[0].content_dir
    (content / "Game Modes" / "MCC").mkdir(parents=True, exist_ok=True)
    (content / "Maps").mkdir(parents=True, exist_ok=True)
    (content / "Mods" / "Some Mod").mkdir(parents=True, exist_ok=True)
    (content / "Game Modes" / "MCC" / "good.bin").write_bytes(b"_blf" + bytes(60))
    (content / "Game Modes" / "MCC" / "7ef5.bin").write_bytes(b"\x00\x01not a variant")
    (content / "Maps" / "a1b2.mvar").write_bytes(b"MCC new format")
    (content / "Mods" / "Some Mod" / "odd.bin").write_bytes(b"a mod's own")  # the mod checks' business
    bad = unreadable_variants(content)
    assert [p.relative_to(content).as_posix() for p in bad] == ["Game Modes/MCC/7ef5.bin", "Maps/a1b2.mvar"]
    cli.main(["-c", str(settings_path), "check"])  # its exit code is about the rest of the root (no automod.txt here)
    said = capsys.readouterr().out
    assert "Content the server can't read" in said and "Maps/a1b2.mvar: not an MCC variant file" in said
    assert "check --set-aside moves them" in said and all(p.exists() for p in bad)  # check alone moves nothing
    cli.main(["-c", str(settings_path), "check", "--set-aside"])
    aside = content.parent / SET_ASIDE
    assert (aside / "Game Modes" / "MCC" / "7ef5.bin").read_bytes() == b"\x00\x01not a variant"
    assert not any(p.exists() for p in bad) and (content / "Game Modes" / "MCC" / "good.bin").exists()
    (content / "Maps" / "a1b2.mvar").write_bytes(b"again")  # set aside again: the first copy stays
    assert set_aside(content, [content / "Maps" / "a1b2.mvar"]) == ([aside / "Maps" / "a1b2 (2).mvar"], [])
    # One that can't be moved (gone since the listing, held open) stays and is said; the others still move.
    (content / "Maps" / "b.mvar").write_bytes(b"bad")
    moved, kept = set_aside(content, [content / "Maps" / "gone.mvar", content / "Maps" / "b.mvar"])
    assert moved == [aside / "Maps" / "b.mvar"] and len(kept) == 1 and kept[0].startswith("Maps/gone.mvar (")
    # A mod's files are the mod checks', in a folder named mods as much as Mods (the same folder on Windows).
    (content / "mods" / "SomeMod").mkdir(parents=True)
    (content / "mods" / "SomeMod" / "data.bin").write_bytes(b"not blf")
    assert not [p for p in unreadable_variants(content) if "SomeMod" in p.parts]


def test_every_playlist_version_is_kept_and_one_is_put_back(tmp_path: Path, run) -> None:
    """Decision 1: a change by hand is found as G lists the playlists; a version is put back exactly as it was kept,
    when the host asks, checked first by the server's own program, with the one it replaces kept."""
    from testkit.setup import fake_tool as tool

    from huragok.core import versions

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    path = server.folder / "playlists" / "slayer.playlist.json"
    original = path.read_bytes()
    menu, _ = screen(settings_path, ["ESC"])
    run(menu._playlists(settings, tool(), server, Catalogue()))  # listed: kept as Huragok first found it
    path.write_text(path.read_text(encoding="utf-8").replace("Halo 3 community rotation", "Edited by hand"),
                    encoding="utf-8")
    keys = ["H", "1", "ENTER",  # Playlists -> History of slayer
            "P", "2", "ENTER",  # put back the oldest: as Huragok first found it (taken by the program: no question)
            "x", "ESC"]
    menu, terminal = screen(settings_path, keys)
    run(menu._playlists(settings, tool(), server, Catalogue()))
    assert path.read_bytes() == original  # exactly as it was kept
    hows = [v.how for v in versions.versions(settings.records, "playlists", server.slug, path.name)]
    assert hows == ["put back", versions.BY_HAND, versions.FIRST]
    printed = "".join(terminal.output)
    assert "changed outside Huragok" in printed and "Put back: slayer.playlist.json is the version of" in printed
    # One the server's program refuses: its words, what putting it back does, asked once. No: left as it is.
    write_playlist(server.folder, "slayer", [{"map": "Nowhere Land", "game": "Slayer"}] * 3, name="broken")
    versions.seen(settings.records, "playlists", server.slug, path)
    path.write_bytes(original)
    versions.seen(settings.records, "playlists", server.slug, path)  # as the listing keeps it
    kept =versions.versions(settings.records, "playlists", server.slug, path.name)
    number = str(next(n for n, v in enumerate(kept, 1) if "Nowhere Land" in versions.read(v)))
    menu, terminal = screen(settings_path, ["H", "1", "ENTER", "P", number, "ENTER", "N", "ESC", "ESC"])
    run(menu._playlists(settings, tool(), server, Catalogue()))
    printed = " ".join("".join(terminal.output).split())
    assert "its server program won't play it as it is" in printed and "Nowhere Land" in printed
    assert "Put back anyway, Alpha Server refuses the change and keeps playing the playlist as it is now" in printed
    assert path.read_bytes() == original


def test_g_shows_what_players_pick_and_what_never_gets_played(tmp_path: Path, run) -> None:
    """Decision 7: each of the playlist's games, most picked first, with its share; a game never picked marked; a
    pick of a game the playlist no longer has listed apart."""
    from testkit.setup import fake_tool as tool

    from huragok.content.catalogue import Entry
    from huragok.core.storage import Storage

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    playlist = load_playlist(server.folder / "playlists" / "slayer.playlist.json")
    settings.database = tmp_path / "records" / "huragok.sqlite3"  # the test roots keep none
    storage = Storage(settings.database)
    for picked in ("Slayer on Guardian", "Slayer on Guardian", "Slayer on Guardian", "BR Starts on Valhalla",
                   "Gone Game on Nowhere"):
        storage.record_pick(server.name, playlist.name, picked)
    storage.close()
    catalogue = Catalogue(maps=[Entry(name, name, "map") for name in ("Guardian", "Valhalla", "The Pit", "Narrows")],
                          game_types=[Entry("Slayer", "Slayer", "game type")])
    menu, terminal = screen(settings_path, ["P", "1", "ENTER", "x", "ESC"])
    run(menu._playlists(settings, tool(), server, catalogue))
    text = "".join(terminal.output)
    assert f'"{playlist.name}": 5 ballot(s) counted' in text
    assert re.search(r"Slayer on Guardian\s+3 pick\(s\) 60%", text) and re.search(r"BR Starts on Valhalla\s+1 pick", text)
    assert "never picked" in text and "Picked, but not in the playlist now:" in text and "Gone Game on Nowhere" in text


def test_a_playlist_and_a_custom_game_are_copied_to_other_servers_each_checked_there(tmp_path: Path, run) -> None:
    """Decision 1, as 0.7.0 has it: copied exactly as they are (labels and all), to the servers picked, each copy checked
    first by that server's own program. One it refuses: its words, what copying anyway does, and the host asked once;
    no leaves it out, yes copies it. What a copy replaces is kept in that server's history first."""
    from conftest import free_port
    from testkit.setup import fake_tool as tool
    from testkit.setup import make_server_folder, write_plays

    from huragok.core import versions

    settings_path = setup_root(tmp_path)
    make_server_folder(settings_path.parent / "SERVERS" / "Bravo", "Bravo", free_port())
    settings = load(settings_path)
    alpha = next(s for s in settings.servers if s.name != "Bravo")
    bravo = next(s for s in settings.servers if s.name == "Bravo")
    (alpha.content_dir / "Maps").mkdir(parents=True, exist_ok=True)
    (alpha.content_dir / "Maps" / "Alpha Only.mvar").write_bytes(b"_blf" + bytes(20))  # a map variant Bravo lacks
    good = write_playlist(alpha.folder, "shared-rotation", [
        {"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Vanilla characters"}},
        {"map": "Valhalla", "game": "Slayer"}, {"map": "The Pit", "game": "Slayer"}], name="Shared Rotation")
    bad = write_playlist(alpha.folder, "alpha-rotation", [{"map": name, "game": "Slayer"}
                                                         for name in ("Guardian", "Valhalla", "Alpha Only")])
    modes = alpha.content_dir / "Game Modes"
    modes.mkdir(parents=True, exist_ok=True)
    (modes / "br-slayer.json").write_text(json.dumps({"version": 1, "name": "BR Slayer", "mode": "Slayer",
                                                      "rules": {}, "options": {"player.speed": 99}}), encoding="utf-8")
    (bravo.folder / "playlists" / "shared-rotation.playlist.json").write_text(json.dumps(
        {"version": 2, "name": "Bravo's own", "games": [{"map": "Guardian", "game": "Oddball"}]}), encoding="utf-8")
    write_plays(bravo.config_path, bravo.name, {"playlist": "playlists/shared-rotation.playlist.json"})  # in use there
    files = sorted(p.name for p in (alpha.folder / "playlists").glob("*.playlist.json"))
    keys = ["C", str(files.index("shared-rotation.playlist.json") + 1), "ENTER", "1", "ENTER", "Y", "x",  # to Bravo
            "C", str(files.index("alpha-rotation.playlist.json") + 1), "ENTER", "A", "ENTER", "N", "x",  # refused: no
            "ESC"]
    menu, terminal = screen(settings_path, keys)
    run(menu._playlists(settings, tool(), alpha, Catalogue()))
    text = " ".join("".join(terminal.output).split())
    assert (bravo.folder / "playlists" / "shared-rotation.playlist.json").read_bytes() == good.read_bytes()  # exactly
    hows = [v.how for v in versions.versions(settings.records, "playlists", bravo.slug, "shared-rotation.playlist.json")]
    assert hows[0] == "copied" and versions.FIRST in hows  # Bravo's own kept before it was replaced
    assert "Bravo has a shared-rotation.playlist.json of its own, and plays it now" in text  # said before replacing
    assert "Bravo: copied to playlists/shared-rotation.playlist.json (it plays it: from the next vote)" in text
    assert "Bravo: its server program won't play it as it is" in text and "Alpha Only" in text
    assert "Copied anyway, it sits in Bravo's playlists folder unplayed" in text and "Copy it to Bravo anyway?" in text
    assert not (bravo.folder / "playlists" / "alpha-rotation.playlist.json").exists()  # asked once: no
    keys = ["C", str(files.index("alpha-rotation.playlist.json") + 1), "ENTER", "1", "ENTER", "Y", "x", "ESC"]
    menu, terminal = screen(settings_path, keys)
    run(menu._playlists(settings, tool(), alpha, Catalogue()))
    assert (bravo.folder / "playlists" / "alpha-rotation.playlist.json").read_bytes() == bad.read_bytes()  # yes
    assert text.count("anyway?") == 1  # one question for it, never a refusal
    # A custom game Bravo's program refuses: its words, asked once; yes copies it as it is.
    menu, terminal = screen(settings_path, ["O", "1", "ENTER", "1", "ENTER", "Y", "x", "ESC"])
    run(menu._custom_games(settings, tool(), alpha))
    printed = " ".join("".join(terminal.output).split())
    assert "Bravo: its server program refuses it: Error: Movement speed must be 0..=10" in printed
    assert "Copy it to Bravo anyway?" in printed and "Bravo: copied to Game Modes/br-slayer.json" in printed
    assert (bravo.content_dir / "Game Modes" / "br-slayer.json").read_bytes() == (modes / "br-slayer.json").read_bytes()
    assert [v.how for v in versions.versions(settings.records, "custom-games", bravo.slug, "br-slayer.json")] == ["copied"]
