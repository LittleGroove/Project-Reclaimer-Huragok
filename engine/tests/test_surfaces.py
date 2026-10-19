"""The messages the main flows don't reach: the window's edge cases, the commands' refusals, and the log lines
for what goes wrong at run time. Each test provokes the condition and checks the words the host reads, so every
line the supervisor can show is one a test has seen."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from conftest import (ALICE, ProgramLifecycle, free_port, free_ports, staff, wait_for, write_root, write_server_folder,
                      write_settings)
from fake_rcon import FakeRcon
from test_extras import fake_mcc
from test_manage import ManageController, manage_root, program_release, self_updating_root, serve_program
from test_manager import SERVER, Clock, FakeBackend, FakeClient, cycle
from test_menu import FakeController, FakeKeyboard, FakeTerminal, row
from test_setup import ALPHA, SetupController, StrictKeyboard, fake_tool, make_server_folder, setup_root
from test_updates import RELEASE, make_install, make_release_zip

from huragok import cli, desktop, mailbox, serverupdates, steamsetup, updates
from huragok import manager as manager_module
from huragok import menu_manage as menu_manage_module
from huragok import menu_setup as menu_setup_module
from huragok import rcon as rcon_module
from huragok.catalogue import Catalogue, Entry
from huragok.chat import CommandRouter
from huragok.config import load
from huragok.core import Supervisor
from huragok.dedicated import ToolResult
from huragok.events import Event, EventBus
from huragok.files import FileTail
from huragok.lifecycle import CommandBackend, Keepalive, LifecycleError, LifecycleUnavailable, Managed, ProcessBackend
from huragok.livesettings import SERVER_SETTINGS
from huragok.manager import Fleet, Result
from huragok.menu import Menu, RemoteController
from huragok.newserver import SetupError
from huragok.part import Part
from huragok.playlists import Playlist, PlaylistError, new_playlist
from huragok.rcon import RconClient, RconUnavailable, Reply
from huragok.relay import Relay
from huragok.runner import PartRunner
from huragok.serverupdates import ProgramRelease, ProgramUpdateService
from huragok.state import Player, ServerState, StateTracker
from huragok.steamsetup import SteamStatus
from huragok.tui import Interruptions, RecentLog, format_listing, wrap_cell
from huragok.updates import UpdateError, UpdateService
from huragok.watch import follow

PROGRAM_RELEASE = ProgramRelease("0.9.19", "https://example.invalid/releases/v0.9.19", "- a fix",
                                 "https://example.invalid/project-reclaimer-dedicated-v0.9.19.exe",
                                 "project-reclaimer-dedicated-v0.9.19.exe")


def printed(terminal: FakeTerminal) -> str:
    return "".join(terminal.output)


def messages(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def both(capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def python_command(code: str) -> str:
    return f'"{Path(sys.executable).as_posix()}" -c "{code}"'


# The commands --------------------------------------------------------------------------------------

def test_a_broken_settings_file_is_refused_with_its_reason(tmp_path: Path, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("Alpha", free_port())])
    path.write_text("this is = not [toml\n", encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        cli.main(["-c", str(path), "status"])
    assert stop.value.code == 2 and "Settings problem:" in both(capsys)


def test_the_start_summary_warns_about_no_owner_and_no_servers(tmp_path: Path, caplog) -> None:
    root = tmp_path / "reclaimer"
    (root / "SERVERS").mkdir(parents=True)
    settings = load(write_settings(root))
    with caplog.at_level(logging.INFO):
        cli._summary(settings)
    text = messages(caplog)
    assert "Running: commands" in text and "Nobody is owner yet: put your player ID under [owner] players" in text
    assert f"Each server is one folder in {root / 'SERVERS'}" in text


def test_the_second_window_takes_over_when_the_first_closes(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, [("Alpha", free_port())])
    cli.write_files(root)
    monkeypatch.setattr(cli, "running_supervisor", lambda settings: 4242)
    monkeypatch.setattr(cli, "_want_menu", lambda args: True)
    monkeypatch.setattr(cli, "_remote_window", lambda settings: "takeover")
    monkeypatch.setattr(cli, "_start_loop", lambda args, settings: 0)
    assert cli.main(["-c", str(path), "start"]) == 0
    assert "The supervising window closed; this one takes over." in both(capsys)


def test_the_start_loop_runs_the_new_version_after_an_update(tmp_path: Path, monkeypatch, capsys) -> None:
    codes = [cli.RELAUNCH, 0]
    started: list[list[str]] = []

    class FakeChild:
        def __init__(self, command: list[str], **_: Any) -> None:
            started.append(command)

        def wait(self) -> int:
            return codes.pop(0)

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(cli.subprocess, "Popen", FakeChild)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    settings = load(write_root(tmp_path / "reclaimer", [("Alpha", free_port())]))
    args = argparse.Namespace(config=str(settings.path), verbose=False, no_menu=True)
    assert cli._start_loop(args, settings) == 0
    assert len(started) == 2 and "--child" in started[0] and "--no-menu" in started[0]
    assert "Starting the new version…" in both(capsys)


def test_a_second_supervisor_in_the_same_folder_is_refused(tmp_path: Path, monkeypatch, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("Alpha", free_port())])

    def taken(settings: Any) -> Path:
        raise cli.AlreadyRunning(4242)

    monkeypatch.setattr(cli, "_take_lock", taken)
    monkeypatch.setattr(cli, "_setup_logging", lambda *args, **options: None)
    args = argparse.Namespace(config=str(path), verbose=False, no_menu=True)
    assert cli._run_supervisor(args) == cli.ALREADY_RUNNING
    assert "Huragok is already running from this folder (process 4242)." in both(capsys)


def test_update_program_asks_before_installing(tmp_path: Path, monkeypatch, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("Alpha", free_port())])
    answers: list[str] = []
    monkeypatch.setattr("huragok.tui.is_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    sent: list[str] = []

    def send(records: Path, verb: str, server: str = "all", wait: float = 15.0, **options: Any) -> mailbox.Reply:
        sent.append(verb)
        if verb == "check_program_update":
            return mailbox.Reply(True, "server program 0.9.19 is out (Alpha 0.9.18)", "- a fix")
        return mailbox.Reply(True, f"installing; restarting {options.get('when')}")

    monkeypatch.setattr(cli.mailbox, "send", send)
    # A supervisor is running: it does the install, once the host says yes.
    monkeypatch.setattr(cli, "running_supervisor", lambda settings: 4242)
    answers.append("n")
    assert cli.main(["-c", str(path), "update", "--program"]) == 0
    assert "Not installed." in both(capsys) and sent == ["check_program_update"]
    answers.append("y")
    assert cli.main(["-c", str(path), "update", "--program", "--now"]) == 0
    text = both(capsys)
    assert "Asking Huragok (process 4242) to install it…" in text and "restarting now" in text
    # No supervisor: the release is fetched here, and put in place only after a yes.
    monkeypatch.setattr(cli, "running_supervisor", lambda settings: None)
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda repository: PROGRAM_RELEASE)
    answers.append("n")
    assert cli.main(["-c", str(path), "update", "--program"]) == 0
    text = both(capsys)
    assert "Huragok isn't running, so the servers' versions aren't known" in text and "Not installed." in text

    def offline(release: ProgramRelease, folder: Path) -> Path:
        raise UpdateError("couldn't reach GitHub: offline")

    monkeypatch.setattr(serverupdates, "download_program", offline)
    assert cli.main(["-c", str(path), "update", "--program", "--yes"]) == 1
    assert "Not installed: couldn't reach GitHub: offline" in both(capsys)
    monkeypatch.setattr("huragok.tui.is_terminal", lambda: False)  # from a script: nothing to ask
    assert cli.main(["-c", str(path), "update", "--program"]) == 1
    assert "To install it:" in both(capsys)


def test_update_asks_then_hands_the_install_to_the_running_supervisor(tmp_path: Path, monkeypatch, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("Alpha", free_port())])
    monkeypatch.setattr(updates, "latest_release", lambda repository: RELEASE)
    monkeypatch.setattr(updates, "installed_here", lambda home: True)
    monkeypatch.setattr("huragok.tui.is_terminal", lambda: True)
    answers = ["n", "y"]
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    assert cli.main(["-c", str(path), "update"]) == 0
    assert "Not installed." in both(capsys)
    monkeypatch.setattr(cli, "running_supervisor", lambda settings: 4242)
    monkeypatch.setattr(cli.mailbox, "send",
                        lambda *args, **options: mailbox.Reply(False, "an install is already under way"))
    assert cli.main(["-c", str(path), "update"]) == 1
    text = both(capsys)
    assert "Asking Huragok (process 4242) to install it…" in text
    assert "an install is already under way" in text


def test_say_rcon_and_status_refuse_clearly(tmp_path: Path, capsys) -> None:
    path = write_root(tmp_path / "reclaimer", [("Alpha", free_port())])
    assert cli.main(["-c", str(path), "say", "Alpha"]) == 2
    assert "Nothing to say." in both(capsys)
    assert cli.main(["-c", str(path), "rcon", "--server", "Nope", "players"]) == 2
    assert "No server named Nope" in both(capsys)
    empty = tmp_path / "empty"
    (empty / "SERVERS").mkdir(parents=True)
    assert cli.main(["-c", str(write_settings(empty)), "status"]) == cli.NOT_RUNNING
    text = both(capsys)
    assert "not running" in text and f"Each server is one folder in {empty / 'SERVERS'}" in text


def test_rcon_prints_the_servers_own_answer(tmp_path: Path, run, capsys) -> None:
    async def scenario() -> str:
        async with FakeRcon() as fake:
            path = write_root(tmp_path / "reclaimer", [("Alpha", fake.port)])
            code = await asyncio.to_thread(cli.main, ["-c", str(path), "rcon", "players"])
            assert code == 0 and fake.named("players")
            return both(capsys)

    assert "0 players" in run(scenario())


def test_check_names_the_problems_and_the_switched_off_commands(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "Alpha", free_port(), name="Alpha", rcon_password=None)
    path = write_settings(root, None, "[watchdog]\nenabled = false\n", staff())
    assert cli.main(["-c", str(path), "check"]) == 1
    text = both(capsys)
    assert "PROBLEM  RCON is off" in text and "!health*" in text and "* its part is switched off" in text


def test_setup_commands_refuse_with_the_reason(tmp_path: Path, capsys) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    with pytest.raises(SystemExit):
        cli.main(["-c", str(settings_path), "content", "-s", "Nope"])
    assert "No server named Nope. Servers: Alpha Server" in both(capsys)
    make_server_folder(root / "SERVERS" / "Beta", "Beta", free_port())
    with pytest.raises(SystemExit):
        cli.main(["-c", str(settings_path), "content"])
    assert "Several servers: name one with --server" in both(capsys)
    none = tmp_path / "none"
    (none / "SERVERS").mkdir(parents=True)
    with pytest.raises(SystemExit):
        cli.main(["-c", str(write_settings(none)), "content"])
    assert "No servers yet: make one with `new`" in both(capsys)
    missing = tmp_path / "missing"
    write_root(missing, [("Gamma", free_port())], 'backend = "process"\n', backend=None)
    with pytest.raises(SystemExit):
        cli.main(["-c", str(missing / "settings.toml"), "content"])
    assert "Problem:" in both(capsys)
    broken = root / "PACKAGES" / "broken"
    broken.mkdir(parents=True)
    (broken / "package.toml").write_text('name = "Broken"\n[server]\nname = "x"\n', encoding="utf-8")
    assert cli.main(["-c", str(settings_path), "new", "Delta", "--package", "broken", "--no-start"]) == 2
    assert "Package broken is broken:" in both(capsys)
    assert cli.main(["-c", str(settings_path), "new", "Delta", "--port", str(free_port())]) == 0
    text = both(capsys)
    assert "Made" in text and "Feet first into hell." in text and "Start Huragok to run it:" in text


def test_package_commands_on_an_empty_folder_and_a_saved_package(tmp_path: Path, capsys) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    assert cli.main(["-c", str(settings_path), "package", "list"]) == 0
    assert f"No packages in {root / 'PACKAGES'}" in both(capsys)
    maps = root / "SERVERS" / ALPHA / "content" / "Maps"
    maps.mkdir(parents=True, exist_ok=True)
    (maps / "fort.mvar").write_bytes(b"x")
    playlist = root / "SERVERS" / ALPHA / "playlists" / "slayer.playlist.json"  # a package keeps what it names
    data = json.loads(playlist.read_text(encoding="utf-8"))
    data["games"].append({"map": "Maps/fort.mvar", "game": "Slayer"})
    playlist.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(["-c", str(settings_path), "package", "save", ALPHA, "Mine"]) == 0
    text = both(capsys)
    assert "Saved" in text and "playlists/slayer.playlist.json" in text and "content/Maps/fort.mvar" in text


def test_importing_nothing_and_importing_many(tmp_path: Path, capsys) -> None:
    settings_path = setup_root(tmp_path)
    mcc = fake_mcc(tmp_path / "MCC")
    assert cli.main(["-c", str(settings_path), "content", "-s", ALPHA, "--import", "--from", str(mcc), "zzz"]) == 1
    assert f"Nothing to import from {mcc} matching zzz" in both(capsys)
    many = tmp_path / "many"
    many.mkdir()
    for number in range(31):
        (many / f"layout{number:02}.mvar").write_bytes(b"x")
    assert cli.main(["-c", str(settings_path), "content", "-s", ALPHA, "--import", "--from", str(many)]) == 0
    text = both(capsys)
    assert "Copied 31 file(s)" in text and "... 1 more" in text


def test_playlist_and_steam_commands_refuse_clearly(tmp_path: Path, capsys, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    assert cli.main(["-c", str(settings_path), "playlist", "use"]) == 2
    assert "Name the playlist file to use" in both(capsys)
    monkeypatch.setattr(steamsetup, "status", lambda settings: SteamStatus(None, "", "", [], 0, 1))

    def no_install(target: Path, say: Any = print, timeout: float = 900.0) -> Path:
        raise steamsetup.SteamSetupError("couldn't reach Steam")

    monkeypatch.setattr(steamsetup, "install", no_install)
    assert cli.main(["-c", str(settings_path), "steam", "install"]) == 1
    assert "Not installed: couldn't reach Steam" in both(capsys)
    exe = tmp_path / "steamcmd" / "steamcmd.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(steamsetup, "status", lambda settings: SteamStatus(exe, "settings.toml", "", [], 0, 1))
    assert cli.main(["-c", str(settings_path), "steam", "login"]) == 2
    assert "Name the Steam account (it must own MCC)" in both(capsys)
    monkeypatch.setattr(steamsetup, "add_to_path", lambda folder: f"{folder} added on your PATH")
    assert cli.main(["-c", str(settings_path), "steam", "path"]) == 0
    assert "added on your PATH" in both(capsys)


# The window --------------------------------------------------------------------------------------

def test_results_with_details_an_empty_server_and_nobody_on(run) -> None:
    terminal = FakeTerminal()
    menu = Menu(FakeController(), terminal, FakeKeyboard(["x"]))  # type: ignore[arg-type]
    menu.show_results([("Alpha Server", Result(False, "refused", "line one\nline two"))])
    assert "  Alpha Server: refused" in printed(terminal) and "      line two" in printed(terminal)

    async def scenario() -> None:
        assert await menu.warn_or_now(row("Alpha Server", players="0/16"), "stop") is False
        quiet = FakeController()
        quiet._rows = [row("Alpha Server", players="0/16", names="")]
        menu.controller = quiet  # type: ignore[assignment]
        await menu.screen_players()

    run(scenario())
    text = printed(terminal)
    assert "Alpha Server: nobody is on, so it goes straight away." in text and "(nobody right now)" in text


def test_a_settings_file_that_stops_parsing_is_not_re_read(tmp_path: Path, caplog) -> None:
    path = write_root(tmp_path / "reclaimer", [("Alpha", free_port())])
    controller = RemoteController(load(path), lambda: None)
    path.write_text("not = [toml\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert controller.refresh_settings().servers[0].name == "Alpha"
    assert "settings not re-read:" in messages(caplog)


def test_held_lines_are_logged_when_the_terminal_fails(caplog) -> None:
    class Broken(FakeTerminal):
        def print_parts(self, *args: Any, **options: Any) -> None:
            raise RuntimeError("no terminal")

    held = Interruptions(Broken())
    held.push("a line")
    with caplog.at_level(logging.ERROR):
        held.flush()
    assert "held lines not shown" in messages(caplog)


def test_a_setting_that_cannot_be_written_says_so(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = manage_root(tmp_path)

    def locked(config: Path, server: str, setting: Any, value: Any) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(menu_manage_module, "write", locked)
    terminal = FakeTerminal()
    keys = ["1", "ENTER", "1", "ENTER", "3", "ENTER", *"8", "ENTER", "x", "ESC", "ESC"]  # Joining -> max_players
    menu = Menu(ManageController(settings_path), terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_settings())
    assert "Couldn't write dedicated.toml: read-only" in printed(terminal)


def setup_menu(settings_path: Path, keys: list[str]) -> tuple[Menu, FakeTerminal, SetupController]:
    controller = SetupController(settings_path)
    terminal = FakeTerminal()
    return Menu(controller, terminal, StrictKeyboard(keys), RecentLog()), terminal, controller  # type: ignore[arg-type]


def test_setup_helpers_say_what_is_missing(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    menu, terminal, controller = setup_menu(settings_path, [])
    assert menu._server(controller.settings, {"name": "Ghost"}) is None
    menu._print_result(ToolResult(True, 0, "", ["dedicated", "status"]))
    assert run(menu._pick([], "maps")) is None
    text = printed(terminal)
    assert "Ghost isn't in the settings any more." in text and "(no output)" in text
    assert "No maps to choose from." in text


class EscapingKeyboard(StrictKeyboard):
    """Presses Esc the moment a screen polls for it while a command runs."""

    async def read(self, timeout: float | None = None) -> str | None:
        if timeout is not None and timeout < 1:
            await asyncio.sleep(0)
            return "ESC"
        return await super().read(timeout)


def test_a_running_command_is_cancelled_with_esc(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, EscapingKeyboard([]), RecentLog())  # type: ignore[arg-type]
    result = run(menu._dedicated(fake_tool(), controller.settings.servers[0].folder, "check"))
    assert result.code == 130
    text = printed(terminal)
    assert "cancelling…" in text and "cancelled." in text


def test_the_content_screen_copes_with_an_unreadable_listing(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)

    def unreadable(*args: Any, **options: Any) -> Catalogue:
        raise ValueError("the listing has no Maps: section")

    monkeypatch.setattr(menu_setup_module, "load_catalogue", unreadable)
    menu, terminal, _ = setup_menu(settings_path, ["1", "ENTER", "x"])
    run(menu.screen_content())
    assert "couldn't read the content listing: the listing has no Maps: section" in printed(terminal)


def test_viewing_a_package_shows_its_broken_playlist_and_its_items(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    package = settings_path.parent / "PACKAGES" / "odd"
    (package / "playlists").mkdir(parents=True)
    (package / "package.toml").write_text('name = "Odd"\ndescription = "an odd one"\n[workshop]\nitems = [123456]\n',
                                          encoding="utf-8")
    (package / "playlists" / "bad.playlist.json").write_text("{not json", encoding="utf-8")
    menu, terminal, controller = setup_menu(settings_path, ["v", "1", "ENTER", "ESC"])
    assert run(menu._choose_package(controller.settings)) == "cancel"
    text = printed(terminal)
    assert "bad.playlist.json:" in text and "Workshop items: [123456]" in text


def test_a_new_server_refuses_a_bad_player_count_and_reports_a_failed_build(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    keys = ["ENTER", *"Zed", "ENTER", "ENTER", "N", "ENTER", *"lots", "ENTER", "ESC"]  # asked again: Esc leaves
    menu, terminal, _ = setup_menu(settings_path, keys)
    run(menu.screen_new())
    assert "'lots' isn't a player count from 1 to 127" in printed(terminal)

    def no_room(settings: Any, spec: Any, tool: Any, say: Any = print) -> Any:
        raise SetupError("no room on the drive")

    monkeypatch.setattr(menu_setup_module, "create_server", no_room)
    keys = ["ENTER", *"Zed", "ENTER", "ENTER", "N", "ENTER", "ENTER", "Y", "x"]
    menu, terminal, _ = setup_menu(settings_path, keys)
    run(menu.screen_new())
    assert "Not made: no room on the drive" in printed(terminal)


def test_browsing_a_long_list_asks_for_words_first(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    menu, terminal, controller = setup_menu(settings_path, ["5", *"hardcore", "ENTER", "ESC"])
    catalogue = Catalogue(game_variants=[Entry(f"variant {n}", f"v{n}", "game variant") for n in range(31)]
                          + [Entry("Hardcore TS", "HARDCORE TS", "game variant")])
    run(menu._browse(controller.settings.servers[0], catalogue))
    text = printed(terminal)
    assert "Words to narrow the list" in text and "Hardcore TS" in text and "variant 30" not in text


def test_the_playlists_screen_with_no_files_and_with_a_broken_one(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    folder = server.folder / "playlists"
    for path in folder.glob("*.playlist.json"):
        path.unlink()
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["E", "ESC"]), RecentLog())  # type: ignore[arg-type]
    run(menu._playlists(controller.settings, fake_tool(), server, Catalogue()))
    text = printed(terminal)
    assert "(no playlists in playlists/ yet)" in text and "Build one first." in text
    (folder / "bad.playlist.json").write_text("{not json", encoding="utf-8")
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["E", "1", "ENTER", "ESC"]), RecentLog())  # type: ignore[arg-type]
    run(menu._playlists(controller.settings, fake_tool(), server, Catalogue()))
    text = printed(terminal)
    assert "(can't read it:" in text and text.count("bad.playlist.json") >= 2  # in the list, then when picked


def test_building_with_no_game_types_builds_nothing(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    menu, terminal, _ = setup_menu(settings_path, ["1", "ENTER", "ESC"])
    catalogue = Catalogue(maps=[Entry("Guardian", "guardian", "map")],
                          game_types=[Entry("Slayer", "Slayer", "game type")])
    assert run(menu._pick_pairs(catalogue)) is None
    assert "No game types picked, so nothing was built." in printed(terminal)


def test_saving_a_playlist_reports_what_the_server_program_cannot_play(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    tool = fake_tool()

    def save_with(keys: list[str], playlist: Playlist) -> tuple[bool, str]:
        terminal = FakeTerminal()
        menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
        return run(menu._save_playlist(tool, server, playlist)), printed(terminal)

    # Every game names a map the server doesn't have: dropping them would leave nothing.
    nowhere = new_playlist(server.folder, "Nowhere")
    for number in range(3):
        nowhere.add(f"Nowhere {number}", "Slayer")
    ok, text = save_with([], nowhere)
    assert not ok and "The server can't play 3 of its games:" in text
    assert "Dropping them would leave fewer than 3 games; add others with [E] edit." in text
    # The program refuses the file without naming a game.
    monkeypatch.setattr(menu_setup_module, "problems_from_show", lambda text: {})
    ok, text = save_with([], nowhere)
    assert not ok and "The server program can't play it as it is; the file is saved, fix it with [E] edit." in text
    # The file can't be written.

    def locked(self: Playlist) -> Path:
        raise PlaylistError("locked")

    monkeypatch.setattr(Playlist, "save", locked)
    ok, text = save_with([], nowhere)
    assert not ok and "Not saved: locked" in text


def test_adding_workshop_items_before_steamcmd_is_set_up_says_so(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    monkeypatch.setattr(steamsetup, "status", lambda settings: SteamStatus(None, "", "", [], 0, 1))
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    terminal = FakeTerminal()
    keys = ["A", *"123456", "ENTER", "ESC"]
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    run(menu._workshop(controller.settings, fake_tool(), server, Catalogue()))
    text = printed(terminal)
    assert "items = [123456]" in text
    assert "SteamCMD isn't set up with a signed-in account, so nothing can download yet: [S] first." in text


def test_the_steam_screen_offers_the_path(tmp_path: Path, run, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    exe = tmp_path / "steamcmd" / "steamcmd.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(steamsetup, "status", lambda settings: SteamStatus(exe, "settings.toml", "me", ["me"], 1, 1))
    monkeypatch.setattr(steamsetup, "cached_accounts", lambda executable: ["me"])
    monkeypatch.setattr(steamsetup, "signed_in", lambda executable, username, timeout=120.0: (True, ""))
    monkeypatch.setattr(steamsetup, "configure", lambda settings, executable, username: [])
    monkeypatch.setattr(steamsetup, "add_to_path", lambda folder: f"{folder} added on your PATH (new windows see it)")
    menu, terminal, _ = setup_menu(settings_path, ["1", "ENTER", "Y", "x", "ESC"])
    run(menu.screen_steam())
    text = printed(terminal)
    assert "me is signed in: the servers can download Workshop items." in text and "added on your PATH" in text


# What goes wrong at run time, as the log says it ---------------------------------------------------

def test_a_failing_command_reply_is_logged(run, caplog) -> None:
    async def boom() -> None:
        raise RuntimeError("the console is gone")

    with caplog.at_level(logging.ERROR):
        run(CommandRouter._safe(boom()))
    assert "command reply failed" in messages(caplog)


_never_run: list[Supervisor] = []  # made by supervisor_on: their database is closed after the test


@pytest.fixture(autouse=True)
def _close_supervisors_never_run() -> Any:
    yield
    while _never_run:
        _never_run.pop().storage.close()


def supervisor_on(tmp_path: Path, servers: list[tuple[str, int]] | None = None, body: str = "") -> Supervisor:
    """A Supervisor over a root with these servers (Alpha alone by default), made but not run."""
    root = tmp_path / "reclaimer"
    path = write_root(root, [("Alpha", free_port())] if servers is None else servers, body, staff())
    cli.write_files(root)
    supervisor = Supervisor(load(path))
    _never_run.append(supervisor)
    return supervisor


def test_the_supervisor_logs_folder_changes_and_what_the_servers_say(tmp_path: Path, caplog) -> None:
    sup = supervisor_on(tmp_path)
    root = tmp_path / "reclaimer"
    with caplog.at_level(logging.INFO):
        sup.bus.emit("rcon.control", "Alpha", action="reload", ok=True, text="playlist re-read")
        sup.bus.emit("rcon.control", "Alpha", action="reload", ok=False, text="dedicated.toml line 3: bad value")
        sup.bus.emit("server.map_skipped", "Alpha", path="C:/x/Workshop/123/odd.map", reason="not a map package")
        sup.bus.emit("server.workshop", "Alpha", item=123, text="installed")
        write_server_folder(root / "SERVERS" / "Alpha Two", free_port(), name="Alpha")  # the same name twice
        sup.discover()
        assert any("two servers are called 'Alpha'" in problem for problem in sup.problems)
        shutil.rmtree(root / "SERVERS" / "Alpha Two")
        shutil.rmtree(root / "SERVERS" / "Alpha")
        sup.discover()
        assert "Alpha" not in sup.fleet.managers and not sup.problems
    text = messages(caplog)
    assert "[Alpha] settings reloaded: playlist re-read" in text
    assert "[Alpha] settings NOT reloaded, nothing changed: dedicated.toml line 3: bad value" in text
    assert "[Alpha] a mod map can't be used: odd.map (not a map package)" in text
    assert "[Alpha] Workshop 123: installed" in text
    assert "Server folders: two servers are called 'Alpha'" in text
    assert "[Alpha] its server folder is gone" in text


def test_auto_update_switched_in_the_file_is_noticed(tmp_path: Path, caplog) -> None:
    sup = supervisor_on(tmp_path)
    alpha = tmp_path / "reclaimer" / "SERVERS" / "Alpha" / "dedicated.toml"
    alpha.write_text("auto_update = true\n" + alpha.read_text(encoding="utf-8"), encoding="utf-8")
    with caplog.at_level(logging.INFO):
        sup.discover()
        assert sup.settings.server("Alpha").auto_update
        sup.bus.emit("server.updating", "Alpha")
    text = messages(caplog)
    assert "[Alpha] auto_update is now on in its dedicated.toml" in text
    assert "[Alpha] PARDON OUR DUST! Project Reclaimer installed a new release by itself" in text


def test_workshop_update_surfaces(tmp_path: Path, run, caplog) -> None:
    """The Workshop service's quieter lines: not looking, Steam unreachable, all up to date, a leftover lock,
    a restart that can't be planned, a server that updated the item itself."""
    from test_workshop_updates import ALPHA as WS_ALPHA, ITEM, NEW, OLD, FakeLifecycle, service_for, workshop_root

    from huragok.workshopupdates import SteamItem, WorkshopUpdateService

    settings_path, folder = workshop_root(tmp_path)
    content = folder / "content"
    with caplog.at_level(logging.INFO):
        idle = load(settings_path)
        idle.workshop_updates.check_minutes = 0
        run(WorkshopUpdateService(idle, EventBus(), FakeLifecycle()).run())  # type: ignore[arg-type]

        def unreachable(items: list[int]) -> dict[int, SteamItem]:
            raise UpdateError("couldn't reach Steam: refused")

        service = service_for(settings_path, FakeLifecycle(), {})
        service.fetch = unreachable
        run(service.check(quiet=False))
        assert service.last_error and "Last look at Steam failed" in "\n".join(service.report())
        current = service_for(settings_path, FakeLifecycle(), {ITEM: SteamItem(ITEM, "HugeGrass", OLD, "")})
        run(current.check(quiet=False))
        assert "Last look at Steam:" in "\n".join(current.report())

        class NoRestarts(FakeLifecycle):
            async def restart(self, server: str, **options: Any) -> Any:
                raise LifecycleUnavailable("it isn't running (stopping)")

        refused = service_for(settings_path, NoRestarts(), {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})

        async def plan_then_swap() -> str:
            await refused.check()
            await asyncio.sleep(0.01)
            (content / "workshop-install.lock").write_text("", encoding="utf-8")  # a crashed install's
            (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): NEW}), encoding="utf-8")
            return await refused.install(refused.settings.server(WS_ALPHA))

        said = run(plan_then_swap())
        assert said.startswith("Workshop updated") and not (content / "workshop-install.lock").exists()
        async def server_does_it_itself() -> None:
            refused._found(refused.settings.server(WS_ALPHA), ITEM, NEW, NEW + 1)
            refused.bus.emit("server.workshop", WS_ALPHA, item=str(ITEM), text="installing the update")
            await asyncio.sleep(0.01)

        run(server_does_it_itself())
        assert not refused.waiting
        assert run(refused.install(refused.settings.server(WS_ALPHA))) == "no Workshop update waiting any more"
        assert run(refused.install_now(WS_ALPHA, "now")) == (False, "nothing is waiting: the server has the "
                                                                    "newest version of every item")
        assert run(refused.install_now("Ghost", "now"))[0] is False
    text = messages(caplog)
    assert "Not looking for new Workshop versions ([workshop_updates] check_minutes = 0)" in text
    assert "Couldn't look for new Workshop versions: couldn't reach Steam: refused" in text
    assert "Workshop: 1 item(s) looked at, every server has the newest" in text
    assert "couldn't plan a restart for the Workshop update" in text and "installs at its next start" in text
    assert "removed a leftover workshop-install.lock" in text
    assert "the server installed the new version of HugeGrass itself" in text


def test_the_workshop_choice_on_the_updates_screen(tmp_path: Path, run) -> None:
    class Controller(ManageController):
        async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
            self.calls.append((verb, server, options))
            if verb == "check_workshop":
                return [("Huragok", Result(True, "new versions waiting on Alpha",
                                           "Alpha · 1 item(s) · new versions installed after the game\n"
                                           "  3820687243 HugeGrass   installed 16 Oct 2026 · NEW VERSION WAITING"))]
            return [("Huragok", Result(True, "Alpha: installing after this game"))]

    controller = Controller(manage_root(tmp_path))
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["3", "A", "x"]), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_update())
    assert controller.calls == [("check_workshop", "all", {}), ("update_workshop", "all", {"when": "after_game"})]
    text = printed(terminal)
    assert "NEW VERSION WAITING" in text and "installing after this game" in text and "Workshop mods" in text


def test_a_broken_server_folder_is_left_out_and_never_stops_the_others(tmp_path: Path, caplog) -> None:
    root = tmp_path / "reclaimer"
    typo = root / "SERVERS" / "Typo"
    typo.mkdir(parents=True)
    (typo / "dedicated.toml").write_text('[[server]]\nname = "Typo\n', encoding="utf-8")
    with caplog.at_level(logging.INFO):
        sup = supervisor_on(tmp_path)
        assert list(sup.fleet.managers) == ["Alpha"]
        assert any("Typo" in p and "left out until that's fixed" in p for p in sup.problems)
        # Alpha's own file breaks while it runs: it's kept as it was, not forgotten.
        alpha = root / "SERVERS" / "Alpha" / "dedicated.toml"
        good = alpha.read_text(encoding="utf-8")
        alpha.write_text(good + "\nport = = 1\n", encoding="utf-8")
        sup.discover()
        assert "Alpha" in sup.fleet.managers and any("Alpha" in p for p in sup.problems)
        alpha.write_text(good, encoding="utf-8")
        write_server_folder(typo, free_port(), name="Typo")  # fixed: picked up
        sup.discover()
        assert sorted(sup.fleet.managers) == ["Alpha", "Typo"] and not sup.problems
    text = messages(caplog)
    assert "Server folders:" in text and "Its servers are left out until that's fixed" in text
    assert "its server folder is gone" not in text


def test_a_failing_task_a_closed_window_and_a_failing_backend_close_are_logged(tmp_path: Path, caplog, run) -> None:
    sup = supervisor_on(tmp_path, servers=[])

    class Window:
        async def run(self) -> str:
            return "quit"

    async def failing_close(stop_servers: bool | None = None) -> None:
        raise RuntimeError("the backend is gone")

    async def boom() -> None:
        raise RuntimeError("no such thing")

    async def scenario() -> None:
        await sup._spawn("the thing", boom, restart=False)
        sup.backend.close = failing_close  # type: ignore[method-assign]
        await asyncio.wait_for(sup.run(menu=Window()), 15)

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert "the thing failed" in text and "Window closed; the servers keep running" in text
    assert "backend close failed" in text


def test_starting_at_login_writes_the_startup_item(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(desktop.subprocess, "run", lambda *args, **options: SimpleNamespace(returncode=0, stderr=""))
    home = tmp_path / "reclaimer"
    home.mkdir()
    result = desktop.enable_startup(home)
    path = desktop.startup_path()
    assert path is not None and path.is_file() and "Huragok will now start when" in result
    assert str(home) in path.read_text(encoding="utf-8")
    # Both halves, whichever platform this runs on.
    windows = tmp_path / "Startup" / "Huragok.cmd"
    windows.parent.mkdir()
    assert desktop.write_windows_startup(windows, home) == "Huragok will now start when Windows logs in"
    assert f'/D "{home}"' in windows.read_text(encoding="utf-8")
    unit = tmp_path / "systemd" / "huragok.service"
    unit.parent.mkdir()
    assert desktop.write_linux_startup(unit, home).startswith("Huragok will now start when you log in")
    assert f"WorkingDirectory={home}" in unit.read_text(encoding="utf-8")
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda *args, **options: SimpleNamespace(returncode=1, stderr="Failed to connect to bus"))
    assert "systemctl --user enable failed: Failed to connect to bus" in desktop.write_linux_startup(unit, home)


def test_the_command_backend_logs_output_and_a_stop_that_fails(tmp_path: Path, caplog, run) -> None:
    body = ("backend = \"command\"\n[command]\n"
            f"start = '''{python_command('print(123)')}'''\n"
            f"stop = '''{python_command('import sys; sys.exit(3)')}'''\n")
    settings = load(write_root(tmp_path / "reclaimer", [("Alpha", free_port())], body, backend=None))
    backend = CommandBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})

    async def scenario() -> None:
        await backend.start(settings.servers[0])
        assert await backend.stop_all() == []

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    text = messages(caplog)
    assert "[Alpha] start output: 123" in text and "[Alpha] not stopped: stop failed with exit code 3" in text


def test_a_process_that_ignores_the_stop_request_is_ended(tmp_path: Path, caplog, run, monkeypatch) -> None:
    settings = load(write_root(tmp_path / "reclaimer", [("Alpha", free_port())], 'backend = "process"\n', backend=None))
    server = settings.servers[0]
    backend = ProcessBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})
    backend.stop_timeout = 0.5
    monkeypatch.setattr(backend, "_signal_stop", lambda managed: False)
    console = settings.logs / "console-alpha.log"
    console.parent.mkdir(parents=True, exist_ok=True)
    console.write_text("", encoding="utf-8")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        backend.managed["Alpha"] = Managed(server, child.pid, psutil.Process(child.pid).create_time(),
                                           FileTail(console), console, child)
        with caplog.at_level(logging.WARNING):
            run(backend.stop(server))
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
    assert "[Alpha] `dedicated` didn't stop cleanly; ending it" in messages(caplog)


def test_keepalive_notices_a_server_answering_again(caplog) -> None:
    lifecycle = SimpleNamespace(busy=set(), is_up=lambda name: True, can_control=False, planned={})
    keepalive = Keepalive(SimpleNamespace(hang_seconds=60, act=False, check_seconds=5), lifecycle,  # type: ignore[arg-type]
                          {"Alpha": ServerState("Alpha")}, EventBus())
    keepalive.hung.add("Alpha")
    with caplog.at_level(logging.INFO):
        keepalive.check()
    assert "[Alpha] responding again" in messages(caplog) and not keepalive.hung


def frozen_since(state: ServerState, seconds: float) -> None:
    """status.json and RCON both last heard from `seconds` ago."""
    state.status_updated = time.time() - seconds
    state.last_rcon_ok = time.monotonic() - seconds


def test_keepalive_gives_each_run_its_own_time_and_restarts_a_new_run_that_freezes(run, caplog) -> None:
    # The live server froze for 76 minutes, was restarted, and froze again 2 s into the next start.
    phases = {"Alpha": "running"}
    asked: list[str] = []

    async def restart(name: str, **options: Any) -> None:
        asked.append(name)

    lifecycle = SimpleNamespace(busy=set(), is_up=lambda name: phases[name] == "running", can_control=True,
                                planned={}, restart=restart)
    state = ServerState("Alpha")
    keepalive = Keepalive(SimpleNamespace(hang_seconds=180, act=True, check_seconds=10), lifecycle,  # type: ignore[arg-type]
                          {"Alpha": state}, EventBus())

    async def scenario() -> None:
        frozen_since(state, 300)
        keepalive._up_since["Alpha"] = time.monotonic() - 3600  # a run that has been up an hour, frozen 5 minutes
        keepalive.check()
        keepalive.check()  # the same run: asked once
        await asyncio.sleep(0)
        assert asked == ["Alpha"]
        phases["Alpha"] = "stopping"
        keepalive.check()
        phases["Alpha"] = "running"  # the next run is up; its files and replies are still the old run's
        keepalive.check()
        assert "Alpha" not in keepalive.hung
        keepalive._up_since["Alpha"] = time.monotonic() - 200  # and it froze as it came up
        keepalive.check()
        await asyncio.sleep(0)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert asked == ["Alpha", "Alpha"]  # minutes after the first restart, not ten
    text = messages(caplog)
    assert text.count("[Alpha] not responding: status.json last written") == 2 and "[Alpha] responding again" in text


def test_keepalive_says_when_it_cant_restart_a_server(run, caplog) -> None:
    async def restart(name: str, **options: Any) -> None:
        raise LifecycleUnavailable("Alpha was started some other way")

    lifecycle = SimpleNamespace(busy=set(), is_up=lambda name: True, can_control=True, planned={}, restart=restart)
    state = ServerState("Alpha")
    keepalive = Keepalive(SimpleNamespace(hang_seconds=60, act=True, check_seconds=5), lifecycle,  # type: ignore[arg-type]
                          {"Alpha": state}, EventBus())

    async def scenario() -> None:
        frozen_since(state, 120)
        keepalive._up_since["Alpha"] = time.monotonic() - 120
        keepalive.check()
        await asyncio.sleep(0)

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert "[Alpha] can't restart it: Alpha was started some other way" in messages(caplog)


RESTARTS = ("[restarts]\nwarn_seconds = [10, 5, 2]\nbackoff_seconds = [5, 60]\nmax_failures = 2\n"
            "good_run_seconds = 600\nrestarts_per_hour = 3\nready_seconds = 30\nempty_seconds = 4\n"
            "after_game_seconds = 3\n")


def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restarts: str = RESTARTS) -> tuple:
    """test_manager's world: a fake backend, a fake client and a clock moved by hand."""
    clock = Clock(1_800_000_000.0)
    monkeypatch.setattr(manager_module, "time", clock)
    settings = load(write_root(tmp_path / "reclaimer", [(SERVER, free_port())], restarts))
    bus = EventBus()
    state = ServerState(SERVER)
    backend = FakeBackend(settings, bus, {SERVER: state})
    fleet = Fleet(settings, backend, bus)
    client = FakeClient(state)
    manager = fleet.add(settings.servers[0], client, state)  # type: ignore[arg-type]
    return clock, settings, backend, fleet, manager, client, state


async def running(fleet: Fleet, manager: Any, clock: Clock, state: ServerState) -> None:
    fleet.boot()
    await cycle(manager, clock)
    state.connected = True
    await cycle(manager, clock)
    assert manager.phase == "running"


def test_the_manager_logs_what_goes_wrong_while_a_server_runs(tmp_path: Path, monkeypatch, caplog, run) -> None:
    clock, settings, backend, fleet, manager, client, state = make_world(tmp_path, monkeypatch)

    async def refused(command: str, *args: str, **options: Any) -> Any:
        raise RconUnavailable("the connection dropped")

    def broken_step(now: float) -> None:
        raise RuntimeError("boom")

    async def scenario() -> None:
        await running(fleet, manager, clock, state)
        real_step = manager._step
        manager._step = broken_step  # type: ignore[method-assign]
        manager.tick(clock.now)
        manager._step = real_step  # type: ignore[method-assign]
        client.command = refused  # type: ignore[method-assign]
        manager._say("Hello")
        await asyncio.sleep(0.05)
        assert manager.set_autostart(False).ok
        state.players[1] = Player(1, "Alice", ALICE)  # with nobody on, a planned restart would fire at once
        assert manager.restart(when="after_game", reason="testing").ok
        assert manager.cancel().message == "the planned restart is cancelled"
        assert manager.restart(when="after_game", reason="testing").ok
        await cycle(manager, clock, manager_module.AFTER_GAME_LIMIT + 1)
        assert manager.phase == "countdown"
        assert manager.stop().message == "the countdown now ends in a stop"
        backend.up.clear()
        state.connected = False
        await cycle(manager, clock)
        assert manager.phase == "idle"

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert f"[{SERVER}] manager error (server untouched): RuntimeError: boom" in text
    assert f"[{SERVER}] could not announce (the connection dropped): Hello" in text
    assert f"[{SERVER}] autostart turned OFF" in text
    assert f"[{SERVER}] planned restart cancelled (testing)" in text
    assert f"[{SERVER}] the game didn't end within" in text
    assert f"[{SERVER}] the countdown now ends in a stop" in text
    assert f"[{SERVER}] the server exited on its own during the countdown" in text


def test_the_manager_logs_a_stop_while_starting_and_a_console_that_cannot_be_used(tmp_path: Path, monkeypatch,
                                                                                  caplog, run) -> None:
    clock, settings, backend, fleet, manager, client, state = make_world(tmp_path, monkeypatch)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        assert manager.phase == "booting"
        assert manager.stop().message == "it was still starting; stopping it"
        await cycle(manager, clock, 1)
        assert manager.phase == "idle"
        assert manager.start().ok
        await cycle(manager, clock)
        client.stopped_reason = "password refused: Wrong password."
        await cycle(manager, clock, 1)
        assert manager.phase == "running"

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert f"[{SERVER}] stop requested while starting" in text
    assert f"[{SERVER}] up, but its console can't be used: password refused: Wrong password." in text


def test_the_manager_ends_a_start_that_never_finishes_and_logs_a_stop_that_fails(tmp_path: Path, monkeypatch,
                                                                                 caplog, run) -> None:
    clock, settings, backend, fleet, manager, client, state = make_world(tmp_path, monkeypatch)

    async def failing_stop(server: Any, timeout: float | None = None) -> None:
        raise LifecycleError("the stop command failed")

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        await cycle(manager, clock, 31)
        assert backend.stops == [SERVER] and manager.phase in ("stopping", "backoff")
        await cycle(manager, clock, 1)
        assert manager.phase == "backoff"
        await cycle(manager, clock, 6)
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        backend.stop = failing_stop  # type: ignore[method-assign]
        assert manager.stop(immediate=True).ok
        await cycle(manager, clock, 1)

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert f"[{SERVER}] ending the process: it never finished starting" in text
    assert f"[{SERVER}] stopping failed: the stop command failed" in text


def test_the_manager_tries_again_at_once_and_runs_the_full_countdown_blind(tmp_path: Path, monkeypatch, caplog,
                                                                            run) -> None:
    clock, settings, backend, fleet, manager, client, state = make_world(
        tmp_path, monkeypatch, RESTARTS.replace("backoff_seconds = [5, 60]", "backoff_seconds = [0, 0]"))

    async def scenario() -> None:
        backend.fail_start = True
        fleet.boot()
        await cycle(manager, clock)
        backend.fail_start = False
        await cycle(manager, clock, 1)
        state.connected = True
        await cycle(manager, clock, 1)
        assert manager.phase == "running"
        state.connected = False  # RCON dropped, the process is still there
        assert manager.restart().message == "restarting (warning players first)"

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert f"[{SERVER}] trying again now (failure 1 of 2)" in text
    assert f"[{SERVER}] the player count is unknown (RCON not connected), so running the full countdown" in text


def test_the_fleet_logs_a_live_file_it_cannot_write_and_a_request_that_fails(tmp_path: Path, monkeypatch, caplog,
                                                                             run) -> None:
    clock, settings, backend, fleet, manager, client, state = make_world(tmp_path, monkeypatch)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    fleet.live_path = blocker / "live.json"

    async def failing_verb(verb: str, server: str, **options: Any) -> Any:
        raise RuntimeError("the fleet is confused")

    async def scenario() -> None:
        fleet.write_live()
        fleet.verb = failing_verb  # type: ignore[method-assign]
        await fleet._answer(mailbox.Request("req1", "rcon", SERVER, {"text": "players"}))

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    text = messages(caplog)
    assert "couldn't write" in text and "request rcon failed" in text
    reply = mailbox.take_reply(settings.records, "req1")
    assert reply is not None and not reply.ok and "RuntimeError: the fleet is confused" in reply.message


def test_the_watchdog_reports_and_says_what_it_would_do(tmp_path: Path, caplog, run) -> None:
    sup = supervisor_on(tmp_path)
    sup.start_parts()
    watchdog = sup.runners["watchdog"].part

    async def scenario() -> None:
        await watchdog.setup()
        watchdog.last_report = -1e9  # the report is due
        await watchdog.evaluate()
        await watchdog._act("Alpha", {"slow": "slow in 3 of the last 5 minutes"})  # slow is watch
        await watchdog._act("Alpha", {"stuck": "stuck after a game for 6 minutes"})  # actnow: attach can't restart
        watchdog.config["mode"] = "watch"
        await watchdog._act("Alpha", {"stuck": "stuck after a game for 6 minutes"})
        watchdog.config["mode"] = "act"
        await watchdog.roster_lost(Event("log.roster_lost", "Alpha", {"lost": 70, "of": 73, "present": 3}))
        # Slow all game (watched), then nobody gets in: a case that's new still acts.
        now = time.time()
        for minute in range(3):
            await watchdog.falling_behind(Event("log.falling_behind", "Alpha", {
                "behind": 60, "window": 60, "longest_ms": 133, "players": 127}, now - 120 + minute * 60))
        await watchdog._update("Alpha")
        for ago in (400, 380, 300):  # each had over two minutes to get in
            await watchdog.connected(Event("log.connected", "Alpha", {"name": "x"}, now - ago))
        await watchdog._update("Alpha")

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert "[Alpha] healthy · 0 players" in text
    assert "[Alpha] the server dropped 70 of 73 players at once (3 now present)" in text
    assert "[Alpha] players are still on, so it's left to recover by itself" in text
    assert '[Alpha] not restarted, as [watchdog] slow = "watch"' in text
    assert '[Alpha] would restart (now) — [watchdog] mode = "watch"' in text
    assert "[Alpha] can't restart: backend = attach can't restart servers" in text
    assert ("[Alpha] healthy → critical: falling behind for 3 minutes running; slow in 3 of the last 5 minutes "
            "(longest frame 133 ms)") in text
    assert '[Alpha] not restarted, as [watchdog] very_slow = "watch", slow = "watch"' in text
    assert "[Alpha] still critical, and now also: 3 players connected in 5 minutes and none got in" in text


def test_watchdog_settings_are_checked_and_the_old_switch_still_counts() -> None:
    from huragok.parts.watchdog import Watchdog

    home = Path(".")
    assert Watchdog.preflight({}, home) == []
    assert Watchdog.preflight({"stuk": "watch"}, home) == ["'stuk' isn't a watchdog setting, so it does nothing"]
    for section, message in [({"stuck": "act now"}, 'stuck must be "watch", "act" or "actnow"'),
                             ({"mode": "on"}, 'mode must be "act" or "watch"'),
                             ({"slow_frames": "yes"}, 'slow_frames must be "watch" or "act"'),
                             ({"stuck_minutes": "5"}, "stuck_minutes must be a number, 0 or more"),
                             ({"drop_share": 50}, "drop_share is a share of a minute or of the players: 0 to 1")]:
        with pytest.raises(ValueError, match=re.escape(message)):
            Watchdog.preflight(section, home)
    old = Watchdog.resolve({**Watchdog.defaults, "slow_frames": "act"})  # 0.2.1's one switch for both
    assert (old["slow"], old["very_slow"]) == ("act", "actnow")
    assert Watchdog.resolve({**Watchdog.defaults, "slow_frames": "act", "slow": "watch"})["slow"] == "watch"
    assert Watchdog.resolve(dict(Watchdog.defaults))["very_slow"] == "watch"


def test_rcon_warns_about_a_strange_protocol_and_odd_messages(tmp_path: Path, run, caplog, monkeypatch) -> None:
    monkeypatch.setattr(rcon_module, "PROTOCOL", 2)

    async def scenario() -> None:
        async with FakeRcon() as fake:
            settings = load(write_root(tmp_path / "reclaimer", [("Alpha", fake.port)]))
            client = RconClient(settings.servers[0], EventBus(), use_host_key=False, command_timeout=2)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            for ws in list(fake.connections):
                await ws.send("this isn't JSON")
                await ws.send(json.dumps({"type": "lagged", "missed": 7}))
                await ws.send(json.dumps({"type": "error", "text": "Unknown command"}))
            await wait_for(lambda: "RCON error: Unknown command" in messages(caplog))
            client.stop()
            await asyncio.wait_for(task, 3)

    with caplog.at_level(logging.WARNING):
        run(scenario())
    text = messages(caplog)
    assert "[Alpha] RCON protocol 1 (this Huragok knows 2); some messages may be misread" in text
    assert "[Alpha] RCON sent something that isn't JSON" in text
    assert "[Alpha] RCON: fell behind and missed 7 events" in text


def test_the_relay_warns_about_no_users_and_ports_it_cannot_take(tmp_path: Path, run, caplog) -> None:
    port, taken, free, source_taken = sorted(free_ports(4))
    blockers = []
    for busy in (taken, source_taken):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", busy))
        blocker.listen(1)
        blockers.append(blocker)

    async def run_until_a_problem(relay: Relay) -> None:
        task = asyncio.ensure_future(relay.run())
        await wait_for(lambda: relay.problems)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        with caplog.at_level(logging.INFO):
            # The console relay's port is in use.
            body = f"[relay]\nenabled = true\nport_offset = {taken - port}\n"
            settings = load(write_root(tmp_path / "one", [("Alpha", port)], body, staff()))
            run(run_until_a_problem(Relay(settings, {}, {"Alpha": ServerState("Alpha")}, EventBus())))
            # The console relay listens, but the Source RCON relay's port is in use.
            body = (f"[relay]\nenabled = true\nport_offset = {free - port}\nsource_port_offset = {source_taken - port}\n"
                    '[[relay.user]]\nname = "Alice"\npassword = "alicealice"\nrole = "moderator"\n')
            settings = load(write_root(tmp_path / "two", [("Alpha", port)], body, staff()))
            run(run_until_a_problem(Relay(settings, {}, {"Alpha": ServerState("Alpha")}, EventBus())))
    finally:
        for blocker in blockers:
            blocker.close()
    text = messages(caplog)
    assert "[relay] enabled, but no [[relay.user]] is listed: nobody can sign in" in text
    assert f"the console relay can't listen on 127.0.0.1:{taken}: the port is in use" in text
    assert f"[Alpha] console relay listening on 127.0.0.1:{free}" in text
    assert f"the Source RCON relay can't listen on 127.0.0.1:{source_taken}: the port is in use" in text


class Tiny(Part):
    name = "tiny"
    description = "A part for the runner's edges"

    def __init__(self, ctx: Any, *, setup_fails: bool = False, teardown_fails: bool = False) -> None:
        super().__init__(ctx)
        self.setup_fails = setup_fails
        self.teardown_fails = teardown_fails

    async def setup(self) -> None:
        if self.setup_fails:
            raise RuntimeError("no database")

    async def teardown(self) -> None:
        if self.teardown_fails:
            raise RuntimeError("already closed")


def tiny_part(**how: bool) -> Tiny:
    return Tiny(SimpleNamespace(name="tiny", config={}, log=logging.getLogger("part.tiny")), **how)


def test_the_runner_logs_a_full_queue_a_failed_setup_and_a_slow_stop(run, caplog, monkeypatch) -> None:
    bus = EventBus()
    real_wait_for = asyncio.wait_for

    async def nothing() -> None:
        return None

    async def scenario() -> None:
        failing = PartRunner(tiny_part(setup_fails=True), bus)
        await failing.run()
        assert not failing.enabled and failing.disabled_reason and "setup failed" in failing.disabled_reason
        small = PartRunner(tiny_part(), bus, queue_size=1)
        small.submit("first", nothing)
        small.submit("second", nothing)
        busy = PartRunner(tiny_part(teardown_fails=True), bus, handler_timeout=30)
        busy._drained = SimpleNamespace(wait=lambda: asyncio.sleep(3600), set=lambda: None)  # type: ignore[assignment]
        monkeypatch.setattr(asyncio, "wait_for",
                            lambda awaitable, timeout: real_wait_for(awaitable, min(timeout, 0.2)))
        task = asyncio.ensure_future(busy.run())
        await asyncio.sleep(0.05)
        busy.submit("slow", lambda: asyncio.sleep(60))
        await asyncio.sleep(0.05)
        await busy.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.WARNING):
        run(scenario())
    text = messages(caplog)
    assert "tiny: setup failed" in text and "tiny is falling behind; dropped second" in text
    assert "tiny: still busy after 5 s; stopping it anyway" in text and "tiny: teardown failed" in text


def test_server_program_update_checks_say_what_they_found(tmp_path: Path, run, caplog, monkeypatch) -> None:
    settings = load(write_root(tmp_path / "reclaimer", [("Alpha", free_port())], "[server_updates]\ncheck = false\n"))
    service = ProgramUpdateService(settings, EventBus(), None, {}, {})  # type: ignore[arg-type]
    answers: list[Any] = [UpdateError("couldn't reach GitHub: offline"), None, PROGRAM_RELEASE]

    def next_answer(repository: str) -> ProgramRelease | None:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(serverupdates, "latest_program_release", next_answer)

    async def scenario() -> None:
        await service.run()
        assert await service.check(quiet=False) is None
        assert await service.check(quiet=False) is None
        assert await service.check(quiet=False) == PROGRAM_RELEASE

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert "Not looking for server program updates ([server_updates] check = false)" in text
    assert "Couldn't look for server program updates: couldn't reach GitHub: offline" in text
    assert f"No server program release found at {settings.server_updates.repository}" in text
    assert "Newest server program release: 0.9.19 (running: no server running)" in text


def test_the_check_on_self_updating_servers_says_what_it_sees(tmp_path: Path, run, caplog, monkeypatch) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    release = program_release(published=time.time())
    serve_program(monkeypatch, release, b"program 0.9.19")
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})
    later = time.time() + serverupdates.OVERDUE + 1

    async def scenario() -> None:
        bus.emit("server.self_update", "Alpha", text="Automatic updates requested, but the update feed is disabled "
                                                     "(local build or RECLAIMER_UPDATE_URL=off)")
        bus.emit("server.update_downloading", "Beta", version="0.9.19")
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        # Alpha said its updater is off: once the release is known it is taken over without waiting, but
        # GitHub's download fails too; it is tried again within check_hours.
        monkeypatch.setattr(serverupdates, "_get", lambda url, t, accept="": (_ for _ in ()).throw(
            UpdateError("couldn't reach GitHub: offline")) if url.endswith("exe") else b"")
        await service.check()
        await wait_for(lambda: ("Alpha", "program") in lifecycle.flags)
        assert lifecycle.flags[("Alpha", "program")] == "! Project Reclaimer 0.9.19: the download failed"
        assert lifecycle.restarts == [] and service.review() == []
        serve_program(monkeypatch, release, b"program 0.9.19")
        assert service.review(now=service.retry["Alpha"] + 1) == ["Alpha"]
        await wait_for(lambda: lifecycle.restarts)
        # The swap itself fails: the old program stays, the server starts on it.
        monkeypatch.setattr(serverupdates, "swap_program", lambda *a: (_ for _ in ()).throw(UpdateError("disk full")))
        assert await lifecycle.jobs["Alpha"]["program"].run() == ""
        assert lifecycle.flags[("Alpha", "program")] == ("! Project Reclaimer 0.9.19 didn't install: tried again in "
                                                         "1 hour (U to try now)")
        # Beta: the file is put in place, yet it comes back on the old version (it runs another file).
        monkeypatch.setattr(serverupdates, "swap_program", lambda *a: "put in place")
        assert service.review(now=later, only="Beta") == ["Beta"]
        await wait_for(lambda: "program" in lifecycle.jobs.get("Beta", {}))
        assert await lifecycle.jobs["Beta"]["program"].run() == "put in place"
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        assert lifecycle.flags[("Beta", "program")] == "! still on 0.9.18 after 0.9.19 was put in place"
        assert service.review(now=later + 7200, only="Beta") == []  # not again this run
        # A restart that can't be planned: it installs at the next start.

        class Unplannable(ProgramLifecycle):
            async def restart(self, server: str, **options: Any) -> Any:
                raise LifecycleUnavailable("backend = attach can't restart servers")

        service.lifecycle = Unplannable()
        assert await service.take_over("Alpha", release, "asked for") == (True, "Alpha installs 0.9.19 at its "
                                                                                "next start")
        # The look that goes wrong is logged and goes on.
        monkeypatch.setattr(serverupdates, "REVIEW_SECONDS", 0.01)
        monkeypatch.setattr(service, "review", lambda **k: 1 / 0)
        loop = asyncio.ensure_future(service._review_loop())
        await asyncio.sleep(0.05)
        loop.cancel()

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert ("[Alpha] auto_update is on, but the server says: Automatic updates requested, but the update feed is "
            "disabled (local build or RECLAIMER_UPDATE_URL=off). Huragok installs new releases for it after its "
            "game") in text
    assert "[Beta] downloading Project Reclaimer 0.9.19 itself (auto_update)" in text
    assert ("[Alpha] its own update failed (Automatic updates requested, but the update feed is disabled (local build "
            "or RECLAIMER_UPDATE_URL=off)), but Huragok couldn't download Project Reclaimer 0.9.19 either: couldn't "
            "reach GitHub: offline; it tries again within 1 hour") in text
    assert "[Alpha] Project Reclaimer 0.9.19 didn't install: disk full; it starts on the old one" in text
    assert "[Beta] still on 0.9.18 over an hour after 0.9.19 came out: Huragok installs Project Reclaimer 0.9.19" \
           " after its game" in text
    assert "[Beta] still on Project Reclaimer 0.9.18 after Huragok put 0.9.19 in place at " in text
    assert "it runs another program file. Update that one by hand" in text
    assert "[Alpha] couldn't plan a restart (backend = attach can't restart servers); it happens at its next start" \
           in text
    assert "Looking at the self-updating servers went wrong" in text


def test_self_updating_servers_huragok_cant_update_are_named(tmp_path: Path, run, caplog, monkeypatch, capsys) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta",
                              body='backend = "attach"\n[process]\ndisable_auto_update = true\n')
    settings = load(root / "settings.toml")
    release = program_release(published=time.time() - 3 * 3600)
    serve_program(monkeypatch, release, b"program 0.9.19")
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    clients = {"Alpha": SimpleNamespace(version="0.9.18"), "Beta": SimpleNamespace(version="0.9.18")}
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, clients)  # type: ignore

    async def scenario() -> None:
        bus.emit("server.self_update", "Alpha", text="Automatic updates requested, but the update feed is disabled")
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        await service.check(quiet=False)
        assert service.review(now=time.time() + serverupdates.OVERDUE + 1) == []
        assert lifecycle.restarts == [] and lifecycle.jobs == {}
        ok, message = await service.install(when="now")
        assert not ok and "every server updates itself" in message
        monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: program_release("latest"))
        assert await service.check(quiet=False) is None

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert ("[Alpha] auto_update is on, but the server says: Automatic updates requested, but the update feed is "
            "disabled. Its new releases have to be installed another way") in text
    assert ("Project Reclaimer 0.9.19 is out (running: Alpha 0.9.18, Beta 0.9.18); every server updates itself "
            "(auto_update) within the hour.") in text
    assert ("[Beta] still on Project Reclaimer 0.9.18 although 0.9.19 is out: its own update didn't happen, and with "
            "backend = attach Huragok can't put the program in place") in text
    assert "The newest server program release has no version number (latest)" in text
    # check names the setting that does nothing any more.
    assert cli.main(["-c", str(root / "settings.toml"), "check"]) in (0, 1)
    out = both(capsys)
    assert "Settings that do nothing" in out and "settings.toml [process] disable_auto_update does nothing any more" in out


def test_updates_from_u_reach_self_updating_servers_left_behind(tmp_path: Path, run, monkeypatch, caplog) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    release = program_release(published=time.time())
    serve_program(monkeypatch, release, b"program 0.9.19")
    lifecycle = ProgramLifecycle(up=("Alpha",))
    clients = {"Alpha": SimpleNamespace(version="0.9.18"), "Beta": SimpleNamespace(version="0.9.18")}
    service = ProgramUpdateService(settings, EventBus(), lifecycle, {  # type: ignore[arg-type]
        "Alpha": ServerState("Alpha"), "Beta": ServerState("Beta")}, clients)  # type: ignore[arg-type]

    async def scenario() -> None:
        await service.check()
        ok, message = await service.install(when="now")
        assert ok and message == "Alpha installs 0.9.19 now; Beta update themselves (auto_update)"
        assert lifecycle.restarts == [("Alpha", "now", "updating to Project Reclaimer 0.9.19")]
        assert lifecycle.flags[("Alpha", "program")] == "Project Reclaimer 0.9.19: installs now"

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert ("every server updates itself (auto_update) within the hour. Huragok makes sure each did, and installs it "
            "after the game for one that didn't") in messages(caplog)


def test_a_refresh_that_fails_is_logged_quietly(run, caplog) -> None:
    class Down:
        connected = True

        async def command(self, *args: Any, **options: Any) -> Any:
            raise RconUnavailable("the connection dropped")

    tracker = StateTracker(EventBus(), {"Alpha": Down()})  # type: ignore[dict-item]
    with caplog.at_level(logging.DEBUG):
        run(tracker.refresh("Alpha"))
    assert "[Alpha] refresh failed: the connection dropped" in messages(caplog)


def test_a_refresh_lists_the_players_already_on(run) -> None:
    # 0.2.0-0.2.1: _apply_players had slipped out of the class, so the first refresh raised and the loop died.
    data = {"status": {"phase": "playing", "max_players": 16},
            "players": {"players": [{"number": 3, "name": "Alice", "id": "C" * 64}], "max_players": 16},
            "admin": {"configured": ["e" * 64], "added": []}}
    replies = {command: Reply(True, "", value, {}) for command, value in data.items()}

    class Up:
        connected = True

        async def command(self, command: str, *args: str, **options: Any) -> Reply:
            return replies[command]

    tracker = StateTracker(EventBus(), {"Alpha": Up()})  # type: ignore[dict-item]
    run(tracker.refresh("Alpha"))
    state = tracker.states["Alpha"]
    assert [(p.number, p.name, p.id) for p in state.players.values()] == [(3, "Alice", "c" * 64)]
    assert state.max_players == 16 and state.admins == {"e" * 64} and state.last_rcon_ok is not None


def test_the_refresh_loop_outlives_a_bad_reply(run, caplog) -> None:
    asked: list[str] = []

    class Odd:
        connected = True

        async def command(self, command: str, *args: str, **options: Any) -> Reply:
            asked.append(command)
            raise ValueError("odd reply")

    tracker = StateTracker(EventBus(), {"Alpha": Odd()}, refresh_seconds=0.01)  # type: ignore[dict-item]

    async def scenario() -> None:
        loop = asyncio.ensure_future(tracker._refresh_loop("Alpha"))
        await asyncio.sleep(0.1)
        assert not loop.done()
        loop.cancel()

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert asked.count("status") >= 2
    assert "[Alpha] couldn't read the server's state: ValueError: odd reply" in messages(caplog)


def test_the_steamcmd_folder_goes_in_the_shell_profile_once(tmp_path: Path) -> None:
    profile = tmp_path / ".profile"
    assert steamsetup.add_to_profile(Path("/opt/steamcmd"), profile) == f"added to {profile} (new shells see it)"
    assert steamsetup.add_to_profile(Path("/opt/steamcmd"), profile) == f"{profile} already has it"
    assert profile.read_text(encoding="utf-8").count('export PATH="$PATH:') == 1


def test_updates_say_when_they_are_off_unreachable_or_fail(tmp_path: Path, run, caplog, monkeypatch) -> None:
    settings = load(write_root(tmp_path / "reclaimer", [("Alpha", free_port())], "[updates]\ncheck = false\n"))
    service = UpdateService(settings, EventBus(), lambda: None)

    def offline(repository: str) -> None:
        raise UpdateError("couldn't reach GitHub: offline")

    def bad_zip(home: Path, release: Any) -> str:
        raise UpdateError("the download isn't a zip file")

    monkeypatch.setattr(updates, "latest_release", offline)
    monkeypatch.setattr(updates, "update", bad_zip)

    async def scenario() -> None:
        await service.run()
        assert await service.check(quiet=False) is None
        service.can_install = False
        service.announce(RELEASE)
        service.can_install = True
        service.pending = RELEASE
        assert await service.install("testing") is False and not service.installing

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = messages(caplog)
    assert "Not looking for updates ([updates] check = false)" in text
    assert "Couldn't look for updates: couldn't reach GitHub: offline" in text
    assert "This copy doesn't run from its own folder's engine/, so it can't update itself" in text
    assert "Update failed, still on" in text and "the download isn't a zip file" in text


def test_a_download_is_written_and_a_half_done_install_is_put_back(tmp_path: Path, monkeypatch, caplog) -> None:
    archive = make_release_zip(tmp_path / "release.zip")
    monkeypatch.setattr(updates, "_get", lambda url, timeout, accept=None: archive.read_bytes())
    path = updates.download(updates.Release("99.0.0", "page", "notes", "https://example.invalid/r.zip"),
                            tmp_path / "downloads")
    assert path.is_file() and path.name == "huragok-99.0.0.zip"
    home = make_install(tmp_path / "home")
    real_rename = updates.os.rename
    renames = {"n": 0}

    def flaky_rename(source: Any, target: Any) -> Any:  # the second move fails, and so does undoing the first
        renames["n"] += 1
        if renames["n"] == 1:
            return real_rename(source, target)
        raise OSError("the disk is full")

    monkeypatch.setattr(updates.os, "rename", flaky_rename)
    with caplog.at_level(logging.ERROR), pytest.raises(UpdateError, match="not everything could be put back") as raised:
        updates.install(archive, home)
    assert "copy them back into" in str(raised.value) and "before starting it" in str(raised.value)
    assert "couldn't put" in messages(caplog) and "back; it is in" in messages(caplog)


def test_following_a_log_notices_when_the_file_starts_over(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "console.log"
    path.write_text("first session line one\nfirst session line two\n", encoding="utf-8")
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    looks = {"n": 0}

    def stop() -> bool:
        looks["n"] += 1
        if looks["n"] == 2:
            path.write_text("second\n", encoding="utf-8")
        return looks["n"] > 4

    follow(path, poll=0.02, stop=stop)
    text = buffer.getvalue()
    assert "the file was re-created (a new start)" in text and text.index("line two") < text.index("second")


def test_wrapped_listings_break_paths_after_a_separator() -> None:
    path = r"C:\Users\host\AppData\Local\Temp\reclaimer\SERVERS\Alpha Server"
    lines = wrap_cell(path, 40)
    assert "".join(lines).replace(" ", "") == path.replace(" ", "")  # nothing lost
    assert all(len(line) <= 40 for line in lines) and all(line.endswith("\\") for line in lines[:-1])
    assert wrap_cell("a plain sentence that is too long for the room it has", 20) == [
        "a plain sentence", "that is too long for", "the room it has"]
    assert wrap_cell("x" * 25, 10) == ["xxxxxxxxxx", "xxxxxxxxxx", "xxxxx"]
    block = format_listing([("Alpha Server", "56256", "stopped", path)], 70, wrap=True)
    texts = ["".join(t for t, _ in parts) for parts in block]
    assert len(texts) >= 2 and texts[0].rstrip().endswith("\\")
    assert not any(t.strip().startswith("RS\\") for t in texts)  # no "SERVE / RS" split any more
