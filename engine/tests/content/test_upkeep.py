"""The upkeep screens: every setting in groups (D: what it plays, Huragok's own, renaming, a content folder of its
own), the status card (V), and what the dashboard's line says about a server's health."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, staff, write_root
from testkit import os_as
from testkit.fleet import FakeBackend, FakeClient
from testkit.screens import text_of
from testkit.setup import ALPHA, SetupController, setup_root
from testkit.upkeep import menu_on

from huragok.content.catalogue import Catalogue, Entry
from huragok.core.events import EventBus
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_top_key
from huragok.games import files as game_files
from huragok.program import plays
from huragok.program import settings as ls
from huragok.servers.fleet import Fleet
from huragok.servers.server import ServerRecord
from huragok.servers.state import ServerState
from huragok.window import statuscard
from huragok.window.dashboard import Menu




# The catalogue ------------------------------------------------------------------------------------

def test_every_setting_has_a_group_a_default_or_a_reason(tmp_path: Path) -> None:
    groups = [key for key, _, _ in ls.GROUPS]
    assert all(s.group in groups for s in ls.SETTINGS) and all(ls.group_settings(g) for g in groups)
    assert len({(s.key, s.scope) for s in ls.SETTINGS}) == len(ls.SETTINGS)  # each setting once
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    entries = {(s.key, s.scope): (v, w) for s, v, w in ls.catalogue(server.config_path, server.name, settings)}
    assert entries[("autostart", "huragok")] == (True, "default")
    assert entries[("daily_restart", "huragok")] == ("", "[restarts]")
    assert entries[("workshop_updates", "huragok")] == ("after_game", "[workshop_updates]")
    assert entries[("plays", "plays")][0] == server.plays and entries[("load_timeout", "server")] == (30, "default")
    assert entries[("rcon_port", "server")] == (server.game_port, "its game port")
    assert ls.summary(list(ls.catalogue(server.config_path, server.name, settings)), "joining").startswith("password")
    # Typing checks for the new kinds.
    assert ls.parse(ls.find("daily_restart"), "4:30") == "04:30" and ls.parse(ls.find("daily_restart"), "") == ""
    assert ls.parse(ls.find("items"), "https://steamcommunity.com/sharedfiles/filedetails/?id=2984061723 123456") == \
        [2984061723, 123456]
    assert ls.parse(ls.find("peers"), "https://master.example") == ["https://master.example"]
    for setting, text in ((ls.find("daily_restart"), "25:00"), (ls.find("items"), "hello"),
                          (ls.find("peers"), "master.example"), (ls.find("rcon_password"), "short"),
                          (ls.find("update_check_minutes"), "3"), (ls.find("password", "server"), "x" * 129)):
        with pytest.raises(ValueError):
            ls.parse(setting, text)
    assert ls.check_path(ls.find("game"), server.folder, "nowhere") is not None
    assert ls.check_path(ls.find("game"), server.folder, "game") is None
    assert ls.check_path(ls.find("bans"), server.folder, "bans.json") is None
    # A server plays one kind of thing: writing one takes the others out.
    plays.write_plays(server.config_path, server.name, {"map": "Guardian", "game": "Slayer"})
    block = read_toml(server.config_path)["server"][0]
    assert block["map"] == "Guardian" and "playlist" not in block
    plays.write_plays(server.config_path, server.name, {"campaign": {"mission": 3030, "difficulty": "heroic"}})
    block = read_toml(server.config_path)["server"][0]
    assert "map" not in block and "game" not in block and ls.plays_of(block) == "campaign Tsavo Highway (heroic)"
    with pytest.raises(ValueError):
        ls.write(server.config_path, server.name, ls.find("autostart"), False)


# D --------------------------------------------------------------------------------------------------

def preset(content: Path, name: str = "BR Slayer") -> Path:
    path = content / "Game Modes" / "br-slayer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "name": name, "mode": "Slayer", "rules": {}, "options": {"map.weapons": 9}}),
                    encoding="utf-8")
    return path


def test_what_a_server_plays_is_picked_from_lists(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    preset(server.content_dir)
    small = Catalogue(maps=[Entry("Guardian", "Guardian", "map"), Entry("Valhalla", "Valhalla", "map")],
                      game_types=[Entry("Slayer", "Slayer", "game type"), Entry("Oddball", "Oddball", "game type")])

    async def catalogue(self: Any, tool: Any, settings: Any, server: Any) -> Catalogue:
        return small

    monkeypatch.setattr(Menu, "_catalogue", catalogue)
    keys = ["1", "ENTER",  # Alpha
            "2", "ENTER", "Y", "W",  # What it plays (its only setting)
            "2", "2", "ENTER", "2", "ENTER", "x", "ESC",  # one map and game type: Valhalla, Oddball
            "2", "ENTER", "Y", "W", "3", "1", "ENTER", "1", "ENTER", "x", "ESC",  # a saved custom game: BR Slayer, on Guardian
            "2", "ENTER", "Y", "W", "4", "3", "ENTER", "1", "ENTER", "3", "N", "Y", *"200", "ENTER", "ENTER", "ENTER",
            "ENTER", "N", "x", "ESC",  # campaign: Tsavo Highway, rally point 1, heroic, modifiers: health 200%, no sprint
            "2", "ENTER", "Y", "W", "1", "1", "ENTER", "x", "ESC",  # back to a playlist
            "ESC", "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    assert "now plays Oddball on Valhalla" in text and "now plays BR Slayer on Guardian" in text
    assert "now plays campaign Tsavo Highway (heroic)" in text and "now plays playlist slayer" in text
    kind_change = "once nobody is on (the server restarts by itself when the kind of game changes)"
    assert f"now plays Oddball on Valhalla: {kind_change}" in text  # it played a playlist
    assert "now plays BR Slayer on Guardian: from the next game" in text  # one map and game again: the next game
    assert "now plays campaign Tsavo Highway (heroic): once nobody is on" in text
    assert f"now plays playlist slayer: {kind_change}" in text
    block = read_toml(server.config_path)["server"][0]
    assert block["playlist"] == "playlists/slayer.playlist.json" and not {"map", "game", "variant", "campaign"} & set(block)


def test_campaign_values_are_kept(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    keys = ["1", "ENTER", "2", "ENTER", "Y", "W", "4", "9", "ENTER", "0", "ENTER", "4", "Y", "N", "x", "ESC", "ESC", "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    assert read_toml(server.config_path)["server"][0]["campaign"] == {"mission": 3120, "insertion": 0,
                                                                       "difficulty": "legendary",
                                                                       "custom_characters": True}


def test_choices_are_numbered_and_huragoks_own_go_through_the_window(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    keys = ["1", "ENTER",
            "3", "ENTER", "2", "ENTER", "3", "ESC",  # Gameplay -> team changing -> 3: off
            *"11", "ENTER", "2", "ENTER", *"4:30", "ENTER", "1", "ENTER", "N", "ESC",  # Huragok's own
            "7", "ENTER", "3", "ENTER", "2", "ESC",  # Updates -> workshop_updates -> 2: when_empty
            "ESC", "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    assert read_toml(server.config_path)["server"][0]["team_changing"] == "off"
    assert controller.calls == [("server_setting", ALPHA, {"key": "daily_restart", "value": "04:30"}),
                                ("server_setting", ALPHA, {"key": "autostart", "value": False}),
                                ("server_setting", ALPHA, {"key": "workshop_updates", "value": "when_empty"})]
    text = text_of(terminal)
    assert "Huragok's own" in text and "[1] balanced" in text and "Takes effect: at once: Huragok's own setting" in text


def test_huragoks_own_settings_are_written_and_in_force(tmp_path: Path) -> None:
    path = write_root(tmp_path / "reclaimer", [(ALPHA, free_port())], "", staff())
    settings = load(path)
    bus = EventBus()
    state = ServerState(ALPHA)
    fleet = Fleet(settings, FakeBackend(settings, bus, {ALPHA: state}), bus)
    manager = fleet.add(settings.servers[0], FakeClient(state), state)  # type: ignore[arg-type]
    assert manager.set_setting("daily_restart", "4:30").ok and settings.servers[0].daily_restart == "04:30"
    assert manager.set_setting("workshop_updates", "off").ok and settings.of(settings.servers[0]).workshop_updates == "off"
    assert load(path).server_overrides[ALPHA.lower()]["daily_restart"] == "04:30"
    assert manager.set_setting("daily_restart", None).ok and "daily_restart" not in load(path).server_overrides[
        ALPHA.lower()]
    for key, value in (("daily_restart", "25:00"), ("workshop_updates", "sometimes"), ("port", 1)):
        assert not manager.set_setting(key, value).ok


def test_a_stopped_server_can_be_renamed_and_given_its_own_content(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    root = tmp_path / "reclaimer"
    shared = root / "content"
    (shared / "Maps").mkdir(parents=True)
    (shared / "Maps" / "arena.json").write_text("{}", encoding="utf-8")
    config = root / "SERVERS" / ALPHA / "dedicated.toml"
    set_top_key(config, "content", "../../content")
    make_second = root / "SERVERS" / "Beta"  # a second server sharing the root's content
    from testkit.setup import make_server_folder

    make_server_folder(make_second, "Beta", free_port())
    set_top_key(make_second / "dedicated.toml", "content", "../../content")
    (root / "records" / "servers").mkdir(parents=True)
    (root / "records" / "servers" / "alpha-server.json").write_text("{}", encoding="utf-8")
    controller = SetupController(settings_path)
    keys = ["1", "ENTER",
            "O", "ENTER", "Y", "x",  # its own content folder
            "N", "ENTER", *"Gamma Server", "ENTER", "x", "ESC"]  # then a new name; back out of D's list
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    assert "has its own content folder now" in text and "Renamed to Gamma Server" in text
    folder = root / "SERVERS" / "Gamma Server"
    data = read_toml(folder / "dedicated.toml")
    assert data["content"] == "content" and any(b["name"] == "Gamma Server" for b in data["server"])
    assert (folder / "content" / "Maps" / "arena.json").is_file() and (shared / "Maps" / "arena.json").is_file()
    assert (root / "records" / "servers" / "gamma-server.json").is_file() and controller.discovered == 1
    assert "Beta" in [s.name for s in load(settings_path).servers]


def test_a_running_server_is_not_renamed(tmp_path: Path, run) -> None:
    from testkit.manage import ManageController, manage_root

    controller = ManageController(manage_root(tmp_path))
    menu, terminal = menu_on(controller, ["1", "ENTER", "N", "ENTER", "x", "ESC", "ESC"])
    run(menu.screen_settings())
    assert "stop it first (X), then rename it" in text_of(terminal)


# The status card and V ----------------------------------------------------------------------------

def test_the_status_card_says_what_matters(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    server.workshop_items = [3820687243]
    receipt = server.content_dir / "Mods" / "Workshop" / "3820687243"
    receipt.mkdir(parents=True)
    (receipt / "reclaimer-workshop-version.json").write_text(json.dumps({"item": 3820687243, "updated": 1792148310}),
                                                             encoding="utf-8")
    row = {"status": "RUNNING  up 3h", "color": "green", "players": "2/16", "rcon_ms": 4, "status_age": 2,
           "pid": 1234, "version": "0.9.18", "health": "degraded", "health_reasons": ["slow for 6 of 10 min"],
           "history": [[time.time() - 600, "restart", "after game"], [time.time() - 300, "crash", "exited after 1m"]],
           "flags": ["! settings NOT reloaded: bad value", "mod update waiting: HugeGrass"], "game": "playing Guardian",
           "address": "203.0.113.4:49190", "daily": "daily restart 04:00 (in 6h)", "planned": "after game: x"}
    sections = dict(statuscard.build(settings, server, row, {"release": "0.9.19", "memory_mb": 610.0,
                                                             "workshop_disk": 3 << 30, "startup": True}))
    flat = {title: " | ".join(f"{label} {text}" for label, text, _ in lines) for title, lines in sections.items()}
    assert "RCON 4 ms" in flat["Running"] and "610 MB in use" in flat["Running"]
    assert "203.0.113.4:49190" in flat["Joining"] and "2/16 players" in flat["Joining"]
    assert "playlist slayer" in flat["What it plays"] and "games" in flat["What it plays"]
    assert "by Huragok (U)" in flat["Program"] and "Newest Project Reclaimer 0.9.19" in flat["Program"]
    assert "installed 16 Oct 2026" in flat["Mods"] and "3.0 GB" in flat["Mods"]
    assert "slow for 6 of 10 min" in flat["Health"] and "CRASH: exited after 1m" in flat["Health"]
    assert "enforce" in flat["Safety"] and "Bans" in flat["Safety"]
    assert "daily restart 04:00" in flat["Scheduled"] and "starts at boot" in flat["Scheduled"]
    assert "settings NOT reloaded" in flat["Problems and notes"] and "mod update waiting" in flat["Problems and notes"]
    quiet = dict(statuscard.build(settings, server, {"status": "stopped"}))
    assert "no restarts or crashes" in " ".join(t for _, t, _ in quiet["Health"])
    assert quiet["Problems and notes"] == [("", "none", "green")]


def test_v_shows_the_card_and_jumps_to_its_settings(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)

    class CardController(SetupController):
        def card(self, server: str) -> list[Any]:
            config = self.settings.server(server)
            return statuscard.build(self.settings, config, self.rows()[0])

    controller = CardController(settings_path)
    keys = ["1", "ENTER", "D", "ESC", "F", "ESC"]  # the card; D opens its settings without asking which server
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_view())
    text = text_of(terminal)
    assert "SERVER STATUS" in text and "What it plays" in text and "Problems and notes" in text
    assert "Huragok's own and every server's" in text and text.count("which server?") == 1  # V's own question
    assert "Vergil's word list" not in text  # straight to the server's page, not D's list


def test_the_dashboard_line_says_why(tmp_path: Path) -> None:
    path = write_root(tmp_path / "reclaimer", [(ALPHA, free_port())], "", staff())
    settings = load(path)
    bus = EventBus()
    state = ServerState(ALPHA)
    backend = FakeBackend(settings, bus, {ALPHA: state})
    fleet = Fleet(settings, backend, bus)
    manager = fleet.add(settings.servers[0], FakeClient(state), state)  # type: ignore[arg-type]
    backend.up.add(ALPHA)
    manager.phase, manager.started = "running", time.time()
    bus.emit("watchdog.health", ALPHA, level="degraded", reasons=["slow for 6 of 10 min"])
    manager.record.remember("restart", "after game")
    row = manager.row(1)
    assert "watchdog: degraded (slow for 6 of 10 min)" in row["detail"] and "1 restart(s)/crash(es) in 24 h" in row["detail"]
    assert row["health_reasons"] == ["slow for 6 of 10 min"] and row["history"][-1][1:] == ["restart", "after game"]
    record = ServerRecord()
    for n in range(80):
        record.remember("restart", str(n), now=time.time() - 9 * 86400 + n)  # old ones age out
    record.remember("crash", "now")
    assert len(record.history) == 1 and record.recent()[0][1] == "crash"


# Upkeep jobs: disk, firewall, the engine file, packages ----------------------------------------

def test_what_can_be_cleaned_up(tmp_path: Path, run) -> None:
    from huragok.content import upkeep

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    (server.folder / "project-reclaimer.exe.old").write_bytes(b"x" * 1000)
    for stamp in ("2026-10-01-000000", "2026-10-18-000000"):
        (settings.records / "previous-program" / stamp).mkdir(parents=True)
    aside = server.content_dir.parent / ".huragok-workshop-previous" / "alpha-server-123-20261018"
    aside.mkdir(parents=True)
    (aside / "map.map").write_bytes(b"m" * 2048)
    cache = server.folder / "data" / "workshop-cache" / "steamapps" / "workshop" / "content" / "976730"
    (cache / "999999").mkdir(parents=True)  # no server lists it
    (cache / "123").mkdir()
    server.workshop_items = [123]  # this one is listed: kept
    found = {item.label: item for item in upkeep.cleanable(settings)}
    assert set(found) == {f"{server.folder.name}/project-reclaimer.exe.old", "records/previous-program/2026-10-01-000000",
                          f"{server.folder.name}/.huragok-workshop-previous/alpha-server-123-20261018",
                          f"{server.folder.name}: SteamCMD's copy of 999999"}
    assert found[f"{server.folder.name}/project-reclaimer.exe.old"].size == 1000
    menu, terminal = menu_on(SetupController(settings_path), ["4", "A", "ENTER", "x"])
    from huragok.core import settings as config_module

    original = config_module.load

    def with_items(path: Any) -> Any:  # the window re-reads settings: keep item 123 listed
        loaded = original(path)
        loaded.servers[0].workshop_items = [123]
        return loaded

    menu.controller.refresh_settings = lambda: with_items(settings_path)  # type: ignore[method-assign]
    run(menu.screen_update())
    text = text_of(terminal)
    assert "4 item(s)" in text and "removed" in text
    assert not (server.folder / "project-reclaimer.exe.old").exists() and (cache / "123").is_dir()
    assert (settings.records / "previous-program" / "2026-10-18-000000").is_dir()  # the newest set stays


def test_firewall_rules_get_names_of_their_own(tmp_path: Path) -> None:
    from huragok.content import upkeep

    text = """Players need these ports open in the firewall (and forwarded by a home router, or upnp = true):
  New-NetFirewallRule -DisplayName "Reclaimer games" -Direction Inbound -Protocol UDP -LocalPort 49176-49177 -Action Allow
  New-NetFirewallRule -DisplayName "Reclaimer server browser" -Direction Inbound -Protocol TCP -LocalPort 49175 -Action Allow"""
    commands = upkeep.firewall_commands(text)
    assert commands[0] == ("if (-not (Get-NetFirewallRule -DisplayName 'Reclaimer games UDP 49176-49177' -ErrorAction "
                           "SilentlyContinue)) { New-NetFirewallRule -DisplayName 'Reclaimer games UDP 49176-49177' "
                           "-Direction Inbound -Protocol UDP -LocalPort 49176-49177 -Action Allow | Out-Null }")
    assert "Reclaimer server browser TCP 49175" in commands[1] and upkeep.firewall_commands("Everything is ready.") == []
    assert game_files.engine_out_of_date("halo3.dll  Copy halo3.dll again from an up-to-date MCC installation")
    mcc, game = tmp_path / "mcc", tmp_path / "game"
    (mcc / "halo3").mkdir(parents=True)
    (game / "halo3").mkdir(parents=True)
    (mcc / "halo3" / "halo3.dll").write_bytes(b"new")
    (game / "halo3" / "halo3.dll").write_bytes(b"old")
    assert "picks it up as it restarts" in upkeep.copy_engine(game, mcc)
    assert (game / "halo3" / "halo3.dll").read_bytes() == b"new"
    assert [p.read_bytes() for p in (game / "halo3").glob("halo3.dll.before-*")] == [b"old"]
    with pytest.raises(FileNotFoundError):
        upkeep.copy_engine(game, tmp_path / "nowhere")


def test_check_offers_the_firewall_and_the_engine_file(tmp_path: Path, run, monkeypatch) -> None:
    from huragok.content import upkeep

    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    opened: list[list[str]] = []
    monkeypatch.setattr(upkeep, "open_firewall", lambda commands, folder: opened.append(commands) or "opened")
    from huragok.core import desktop

    monkeypatch.setattr(desktop, "os", os_as("nt"))  # this module alone: never os.name itself
    monkeypatch.setattr("huragok.steam.find_mcc", lambda hint=None: None)  # not this PC's MCC
    menu, terminal = menu_on(controller, ["Y"])
    server = controller.settings.servers[0]
    text = 'New-NetFirewallRule -DisplayName "Reclaimer games" -Direction Inbound -Protocol UDP -LocalPort 1 -Action Allow' \
           "\nCopy halo3.dll again from an up-to-date MCC installation"
    run(menu._after_check(controller.settings, server, text))
    assert opened and "Reclaimer games UDP 1" in opened[0][0]
    assert "don't match this Project Reclaimer" in text_of(terminal)


def test_a_package_keeps_the_folders_defaults(tmp_path: Path) -> None:
    from huragok.content.packages import save_package
    from huragok.core.tomledit import set_table_key

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    set_table_key(server.config_path, "defaults", "max_ping", 150)
    set_table_key(server.config_path, "defaults", "password", "secret")  # the host's own: not packed
    package = save_package(server.folder, server.name, tmp_path / "PACKAGES" / "mine")
    assert package.defaults["max_ping"] == 150 and "password" not in package.defaults


def test_clean_up_keeps_the_newest_huragok_backup_by_version(tmp_path: Path) -> None:
    from huragok.content import upkeep

    settings = load(setup_root(tmp_path))
    for name in ("previous-0.2.9", "previous-0.2.10", "previous-0.2.10-20261001-101010"):
        (settings.records / name / "engine").mkdir(parents=True)

    def offered() -> list[str]:
        return sorted(l.path.name for l in upkeep.cleanable(settings) if l.label.startswith("records/previous-0"))

    assert offered() == ["previous-0.2.10-20261001-101010", "previous-0.2.9"]  # 0.2.10 is newer than 0.2.9
    (settings.records / "last-update.json").write_text(json.dumps({"backup": "previous-0.2.9"}), encoding="utf-8")
    assert offered() == ["previous-0.2.10-20261001-101010"]  # the backup the last update made is kept too
    # The host's own files from before an update brought them up to date are never offered: they aren't a Huragok.
    (settings.records / "previous-files" / "0.4.1").mkdir(parents=True)
    assert not [l for l in upkeep.cleanable(settings) if "previous-files" in l.label]
    # Betas in order: beta.10 is newer than beta.2, so it's the one kept.
    for name in ("previous-0.5.0-beta.2", "previous-0.5.0-beta.10"):
        (settings.records / name / "engine").mkdir(parents=True)
    assert "previous-0.5.0-beta.10" not in offered() and "previous-0.5.0-beta.2" in offered()


def test_the_firewall_command_survives_an_apostrophe_and_says_when_it_failed(tmp_path: Path, monkeypatch) -> None:
    import os
    import shutil
    import subprocess

    from huragok.content import upkeep

    commands = upkeep.firewall_commands('New-NetFirewallRule -DisplayName "Bob\'s $(Remove-Item x) games" -Direction '
                                        'Inbound -Protocol UDP -LocalPort 1 -Action Allow')
    assert "-DisplayName 'Bob''s $(Remove-Item x) games UDP 1'" in commands[0]  # single quotes: nothing in it runs
    script = tmp_path / "Bob's Servers" / "records" / "open-firewall.ps1"
    command = upkeep.firewall_command(script)
    assert command[:3] == ["powershell", "-NoProfile", "-Command"]
    assert ("'" + ('"' + str(script) + '"').replace("'", "''") + "'") in command[3]
    assert command[3].endswith("; exit $p.ExitCode") and "-PassThru" in command[3]
    if os.name == "nt" and shutil.which("powershell"):  # PowerShell's own parser (nothing is run)
        check = ("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseInput("
                 "$env:HURAGOK_TEST_PS, [ref]$null, [ref]$errors); if ($errors) { 'PARSE ERRORS: ' + "
                 "(($errors | ForEach-Object Message) -join '; ') } else { 'parses' }")
        for text in (command[3], upkeep.firewall_script(commands)):
            result = subprocess.run(["powershell", "-NoProfile", "-Command", check], capture_output=True, text=True,
                                    env=dict(os.environ, HURAGOK_TEST_PS=text), timeout=120)
            assert result.stdout.strip() == "parses", result.stdout + result.stderr
    ran: list[list[str]] = []
    answers = [subprocess.CompletedProcess([], 1, "", "Start-Process : This command cannot be run due to the error: "
                                                      "The operation was canceled by the user."),
               subprocess.CompletedProcess([], 0, "", "")]
    monkeypatch.setattr(upkeep.subprocess, "run", lambda command, **options: ran.append(command) or answers.pop(0))
    monkeypatch.setattr(upkeep, "os", os_as("nt"))
    said = upkeep.open_firewall(commands, script.parent)
    assert said.startswith("Windows Firewall wasn't changed: the administrator prompt was declined, or a rule failed "
                           "(exit code 1: Start-Process : This command cannot be run")
    assert upkeep.open_firewall(commands, script.parent) == "the ports are open in Windows Firewall (open-firewall.ps1)"
    assert ran[0] == command and "$ErrorActionPreference = 'Stop'" in script.read_text(encoding="utf-8")


def test_the_engine_file_is_left_alone_when_the_game_folder_is_mccs_own(tmp_path: Path) -> None:
    import os

    from huragok.content import upkeep
    from testkit.setup import make_server_folder

    mcc = tmp_path / "MCC"
    (mcc / "halo3").mkdir(parents=True)
    (mcc / "halo3" / "halo3.dll").write_bytes(b"mcc's own")
    game = tmp_path / "game"
    (game / "halo3").mkdir(parents=True)
    os.link(mcc / "halo3" / "halo3.dll", game / "halo3" / "halo3.dll")  # the very same file
    said = upkeep.copy_engine(game, mcc)
    assert "is MCC's own file" in said and "nothing to copy" in said and "copied" not in said
    assert (mcc / "halo3" / "halo3.dll").read_bytes() == b"mcc's own"
    assert not list(tmp_path.rglob("*.before-*"))  # MCC's own file never renamed away
    if os.name == "nt":  # game\halo3 as a junction to MCC's own folder, as a host may have made it
        import _winapi

        linked = tmp_path / "linked"
        linked.mkdir()
        _winapi.CreateJunction(str(mcc / "halo3"), str(linked / "halo3"))
        assert "is MCC's own file" in upkeep.copy_engine(linked, mcc)
        assert (mcc / "halo3" / "halo3.dll").read_bytes() == b"mcc's own" and not list(mcc.rglob("*.before-*"))
    # Servers sharing one game folder are named, so the screen can say whose file changes.
    settings_path = setup_root(tmp_path / "root")
    beta = tmp_path / "root" / "reclaimer" / "SERVERS" / "Beta"
    make_server_folder(beta, "Beta", free_port())
    set_top_key(beta / "dedicated.toml", "game", f"../{ALPHA}/game")
    settings = load(settings_path)
    shared = settings.server(ALPHA).folder / "game"
    assert upkeep.servers_using(settings, shared) == [ALPHA, "Beta"]
    (mcc / "halo3" / "halo3.dll").unlink()
    (mcc / "halo3" / "halo3.dll").write_bytes(b"new")
    said = upkeep.copy_engine(shared, mcc, upkeep.servers_using(settings, shared))
    assert said.endswith(f"{ALPHA}, Beta pick it up as they restart")


def test_v_says_what_is_playing_what_comes_next_and_what_players_pick(tmp_path: Path) -> None:
    """Decision 7's view: the game the last ballot picked (and its own rules), what's next, a change the server
    refused, and the playlist's most picked games, from what Huragok kept."""
    from huragok.core.storage import Storage
    from huragok.games.halo3 import load_playlist

    settings = load(setup_root(tmp_path))
    server = settings.servers[0]
    settings.database = tmp_path / "records" / "huragok.sqlite3"
    name = load_playlist(server.folder / "playlists" / "slayer.playlist.json").name
    storage = Storage(settings.database)
    for picked in ("Slayer on Guardian", "Slayer on Guardian", "BR Starts on Valhalla"):
        storage.record_pick(server.name, name, picked)
    storage.close()
    row = {"status": "RUNNING", "game": "playing guardian slayer", "picked": "Slayer on Guardian", "overrides": 2,
           "next": "", "voting": True, "reload": [False, "Game 2 duplicates another map/game combination", 0.0]}
    plays = {label: text for label, text, _ in dict(statuscard.build(settings, server, row, {}))["What it plays"]}
    assert plays["Picked"] == "Slayer on Guardian (last ballot; 2 rule(s) and option(s) of its own)"
    assert plays["Up next"] == "players are voting now"
    assert plays["Most picked"] == "Slayer on Guardian (2), BR Starts on Valhalla (1)"
    assert plays["Last change"].startswith("refused by the server: Game 2 duplicates")
    row.update(next="Valhalla Slayer", reload=[True, "applied", 0.0])
    plays = {label: text for label, text, _ in dict(statuscard.build(settings, server, row, {}))["What it plays"]}
    assert plays["Up next"].startswith("Valhalla Slayer (set from the console") and "Last change" not in plays
