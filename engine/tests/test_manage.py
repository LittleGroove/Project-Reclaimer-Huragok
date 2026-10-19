"""Managing a running server: settings changed live, the console with events, bans, the server program
updates, and the console relay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import ProgramLifecycle, free_port, free_ports, staff, wait_for, write_root
from fake_dedicated import TEMPLATE
from test_menu import FakeTerminal
from test_setup import StrictKeyboard

from huragok import serverupdates
from huragok.config import ConfigError, load
from huragok.events import Event, EventBus
from huragok.livesettings import FOLDER_SETTINGS, SERVER_SETTINGS, can_reset, catalogue, parse, reset, write
from huragok.manager import Result
from huragok.menu import Menu
from huragok.menu_manage import describe_event
from huragok.permissions import Permissions, load_permissions
from huragok.rcon import Reply
from huragok.relay import Relay, required_role
from huragok.files import parse_console_line
from huragok.serverupdates import ProgramRelease, ProgramUpdateService, install_program, parse_sums, swap_program
from huragok.state import ServerState
from huragok.tomledit import read_toml, replace_server_blocks, set_top_key
from huragok.tui import RecentLog


def setting(key: str, scope: str = "server") -> Any:
    return next(s for s in [*SERVER_SETTINGS, *FOLDER_SETTINGS] if s.key == key and s.scope in (scope, "workshop"))


# Settings changed live ---------------------------------------------------------------------------

def test_the_settings_catalogue_reads_parses_and_writes(tmp_path: Path) -> None:
    config = tmp_path / "dedicated.toml"
    config.write_text(TEMPLATE, encoding="utf-8")
    replace_server_blocks(config, [{"name": "Slayer", "port": free_port(), "playlist": "playlists/s.playlist.json",
                                    "max_players": 12}])
    entries = {(s.key, s.scope): (v, where) for s, v, where in catalogue(config, "Slayer")}
    assert entries[("max_players", "server")] == (12, "this server")
    assert entries[("votes", "server")] == (True, "[defaults]")
    assert entries[("max_ping", "server")] == (0, "default")  # unset: the program's own value
    assert entries[("host_name", "folder")] == ("Dedicated server", "the file")
    assert entries[("password", "rcon")] == ("", "[rcon]")
    assert parse(setting("votes"), "off") is False and parse(setting("max_ping"), "150") == 150
    # A welcome line keeps its commas; | separates lines, and one line stays text.
    assert parse(setting("welcome"), "hi {name}, rules in Discord") == "hi {name}, rules in Discord"
    assert parse(setting("welcome"), "hi {name} | rules in Discord") == ["hi {name}", "rules in Discord"]
    assert parse(setting("admins", "folder"), "A" * 64) == ["a" * 64]
    assert parse(setting("team_changing"), "OFF") == "off"
    for key, text in (("max_players", "200"), ("max_players", "x"), ("votes", "maybe"), ("team_changing", "never"),
                      ("password", "rcon")):
        with pytest.raises(ValueError):
            parse(setting(key, "rcon" if text == "rcon" else "server"), "short" if text == "rcon" else text)
    with pytest.raises(ValueError, match="not a player ID"):
        parse(setting("admins", "folder"), "Alice")
    with pytest.raises(ValueError, match="5 lines at most"):
        parse(setting("welcome"), "|".join("abcdef"))
    write(config, "Slayer", setting("max_players"), 8)
    write(config, "Slayer", setting("welcome"), ["hi"])
    write(config, "Slayer", setting("host_name", "folder"), "LG")
    write(config, "Slayer", setting("password", "rcon"), "longenoughpassword")
    write(config, "Slayer", setting("enabled", "master"), False)
    data = read_toml(config)
    assert data["server"][0]["max_players"] == 8 and data["server"][0]["welcome"] == ["hi"]
    assert data["host_name"] == "LG" and data["rcon"]["password"] == "longenoughpassword"
    assert data["master"]["enabled"] is False and "# Relative paths" in config.read_text(encoding="utf-8")
    # The settings auto-update and Workshop updates need are on the screen too.
    assert entries[("auto_update", "folder")] == (False, "the file")
    assert entries[("update_check_minutes", "workshop")] == (60, "default")
    write(config, "Slayer", setting("auto_update", "folder"), True)
    write(config, "Slayer", setting("update_check_minutes", "workshop"), 5)
    data = read_toml(config)
    assert data["auto_update"] is True and data["workshop"]["update_check_minutes"] == 5
    # A value the server sets itself can be taken out again, so [defaults] applies.
    assert can_reset(setting("max_players"), "this server") and not can_reset(setting("votes"), "[defaults]")
    assert reset(config, "Slayer", setting("max_players")) and not reset(config, "Slayer", setting("max_players"))
    assert "max_players" not in read_toml(config)["server"][0]


def test_multi_line_values_are_replaced_whole(tmp_path: Path) -> None:
    """`dedicated init` writes admins over three lines: replacing only the first broke the file (2026-10-18)."""
    config = tmp_path / "dedicated.toml"
    config.write_text(TEMPLATE.replace("admins = [\n]", 'admins = [\n    # "0123",\n]'), encoding="utf-8")
    write(config, "Reclaimer Slayer", setting("admins", "folder"), ["a" * 64])
    write(config, "Reclaimer Slayer", setting("password", "rcon"), 'pass#word"1')  # a # inside quotes is no comment
    write(config, "Reclaimer Slayer", setting("welcome"), ["one", "two, three"])
    write(config, "Reclaimer Slayer", setting("welcome"), "just one")
    data = read_toml(config)  # parses: no stray ] or half-written list left behind
    assert data["admins"] == ["a" * 64] and data["rcon"]["password"] == 'pass#word"1'
    assert data["server"][0]["welcome"] == "just one" and data["stats_server"] == ""
    from huragok.tomledit import set_table_key

    set_table_key(config, "workshop", "items", [1, 2])  # the commented `# [workshop]` example comes to life
    text = config.read_text(encoding="utf-8")
    assert text.count("[workshop]") == 1 and "\nitems = [1, 2]\n" in text and "# update_check_minutes" in text
    assert read_toml(config)["workshop"] == {"items": [1, 2]}


def test_events_as_console_lines() -> None:
    assert describe_event(Event("rcon.chat", "S", {"channel": "all", "name": "Alice", "text": "gg"})).endswith(" Alice: gg")
    assert "[team] Bo: go" in describe_event(Event("rcon.chat", "S", {"channel": "team", "name": "Bo", "text": "go"}))
    assert "+ Cy joined (#3)" in describe_event(Event("rcon.join", "S", {"name": "Cy", "number": 3}))
    assert "* kick Dee by Alice: camping" in describe_event(Event("rcon.kick", "S", {"name": "Dee", "by": "Alice",
                                                                                      "reason": "camping"}))
    assert "vote passed: kick Dee" in describe_event(Event("rcon.vote", "S", {"state": "passed", "subject": "kick Dee"}))
    assert "game: playing Guardian Slayer" in describe_event(Event("rcon.phase", "S", {"phase": "playing", "map": "Guardian",
                                                                                        "mode": "Slayer"}))
    assert describe_event(Event("rcon.kill", "S", {"killer": "1", "victim": "2"})) is None


class ManageController:
    """A controller over a real root: rcon and restart calls are logged, events can be pushed to a watcher."""

    supervising = True
    launcher = "start.cmd"

    def __init__(self, settings_path: Path) -> None:
        self.settings = load(settings_path)
        self.home = self.settings.home
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.watchers: dict[str, Any] = {}

    def refresh_settings(self) -> Any:
        self.settings = load(self.settings.path)
        return self.settings

    def discover(self) -> None:
        pass

    def watch(self, server: str, listener: Any) -> Any:
        self.watchers[server] = listener
        return lambda: self.watchers.pop(server, None)

    def header(self) -> list[str]:
        return []

    def rows(self) -> list[dict[str, Any]]:
        return [{"index": i, "name": s.name, "slug": s.slug, "port": s.game_port, "autostart": True, "phase": "running",
                 "status": "RUNNING", "color": "green", "players": "1/16", "names": "Alice", "game": "", "detail": "",
                 "plays": s.plays, "can": {"start": False, "stop": True, "restart": True, "cancel": False, "talk": True}}
                for i, s in enumerate(self.settings.servers, 1)]

    def events(self, count: int) -> list[tuple[int, str]]:
        return []

    def problems(self) -> list[str]:
        return []

    def finished(self) -> str | None:
        return None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        if verb == "rcon":
            if server in self.watchers:  # the server answers with an event too, as a real one does
                self.watchers[server](Event("rcon.chat", server, {"channel": "all", "name": "Alice", "text": "hi"}))
            return [(server, Result(True, "ok", f"reply to {options.get('text')}"))]
        if verb == "check_program_update":
            return [("Huragok", Result(True, "server program 0.9.19 is out (Alpha 0.9.18)", "notes"))]
        if verb == "update_program":
            return [("Huragok", Result(True, f"installed; restarting {options.get('when')}"))]
        return [(server, Result(True, "done"))]

    def request_quit(self) -> None:
        pass


def manage_root(tmp_path: Path) -> Path:
    root = tmp_path / "reclaimer"
    folder = root / "SERVERS" / "Alpha"
    folder.mkdir(parents=True)
    (folder / "dedicated.toml").write_text(TEMPLATE, encoding="utf-8")
    replace_server_blocks(folder / "dedicated.toml", [{"name": "Alpha", "port": free_port(), "map": "Guardian", "game": "Slayer"}])
    from huragok.tomledit import set_table_key

    set_table_key(folder / "dedicated.toml", "rcon", "password", "secretsecret")
    (folder / "project-reclaimer.exe").write_bytes(b"")
    return write_root(root, [], 'backend = "attach"\n', staff())


def test_the_manage_screens(tmp_path: Path, run) -> None:
    settings_path = manage_root(tmp_path)
    controller = ManageController(settings_path)
    keys = [
        # A: the console on Alpha, name Alice, one command (an event arrives with the reply), then out.
        "A", "1", "ENTER", *"Alice", "ENTER", *"players", "ENTER", "ENTER",
        # D: max_players -> 8, then public_address -> play.example.com with a restart, then back.
        # D: Joining -> max_players (3rd) -> 8; Listing and network -> public_address (4th) with a restart after the
        # game; then back out of the group and the screen.
        "D", "1", "ENTER", "1", "ENTER", "3", "ENTER", *"8", "ENTER", "ESC",
        "8", "ENTER", "4", "ENTER", *"play.example.com", "ENTER", "A", "ESC", "ESC",
        # B: list bans, pardon Bravo, add an admin, back.
        "B", "1", "ENTER", "L", "P", *"Bravo", "ENTER", "+", *"#2", "ENTER", "ESC",
        # U: the server program, after the game.
        "U", "2", "A", "x",
        "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    try:
        outcome = run(menu.run())
    except RuntimeError as error:
        raise AssertionError(f"{error}\n{''.join(terminal.output)}") from None
    assert outcome == "quit"
    printed = "".join(terminal.output)
    assert controller.calls == [
        ("rcon", "Alpha", {"text": "players", "by": "Alice"}),
        ("restart", "Alpha", {"when": "after_game", "reason": "public_address changed"}),
        ("rcon", "Alpha", {"text": "bans", "by": "Alice"}),
        ("rcon", "Alpha", {"text": "pardon Bravo", "by": "Alice"}),
        ("rcon", "Alpha", {"text": "admin add #2", "by": "Alice"}),
        ("check_program_update", "all", {}),
        ("update_program", "all", {"when": "after_game"}),
    ]
    assert "Alice: hi" in printed and "reply to players" in printed  # the event and the reply
    data = read_toml(settings_path.parent / "SERVERS" / "Alpha" / "dedicated.toml")
    assert data["server"][0]["max_players"] == 8 and data["public_address"] == "play.example.com"
    assert "at once; players stay connected" in printed and "when `dedicated` restarts" in printed
    assert not controller.watchers  # the console screen stopped watching when it closed
    assert "0.9.19 is out" in printed and "restarting after_game" in printed


def test_enter_at_a_new_value_leaves_the_setting_and_passwords_stay_hidden(tmp_path: Path, run) -> None:
    import io

    settings_path = manage_root(tmp_path)
    controller = ManageController(settings_path)
    settings = controller.refresh_settings()
    server = settings.servers[0]
    config = server.config_path
    set_top_key(config, "admins", ["a" * 64])
    entries = {(s.scope, s.key): (s, v, w) for s, v, w in catalogue(config, server.name)}

    def edit(scope: str, key: str, keys: list[str]) -> str:
        s, v, w = {(e.scope, e.key): (e, v, w) for e, v, w in catalogue(config, server.name)}[(scope, key)]
        terminal = FakeTerminal()
        terminal.transcript = io.StringIO()
        menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
        run(menu._edit_setting(settings, server, controller.rows()[0], s, v, w))
        return "".join(terminal.output) + terminal.transcript.getvalue()

    assert ("rcon", "password") in entries
    # Enter at the console password, the admins and the join password: nothing changes.
    assert "Left as it was" in edit("rcon", "password", ["ENTER", "x"])
    assert "Left as it was" in edit("folder", "admins", ["ENTER", "N", "x"])
    data = read_toml(config)
    assert data["rcon"]["password"] == "secretsecret" and data["admins"] == ["a" * 64]
    # A new console password shows as stars and goes into the transcript masked.
    shown = edit("rcon", "password", [*"hunter2hunter2", "ENTER", "x"])
    assert read_toml(config)["rcon"]["password"] == "hunter2hunter2"
    assert "hunter2" not in shown and "**************" in shown and "> ********" in shown
    # Emptying a list is still possible, after a yes.
    edit("folder", "admins", ["ENTER", "Y", "x"])
    assert read_toml(config)["admins"] == []


def test_console_lines_that_set_a_password_are_masked() -> None:
    from huragok.rcon import masked

    assert masked("password hunter22") == "password ********"
    assert masked("PASSWORD   x y") == "PASSWORD ********"
    assert masked("password\thunter22") == "password ********"  # a tab hides nothing
    assert masked("password") == "password" and masked("players") == "players"
    assert masked("say my password is x") == "say my password is x"


# The server program updates ----------------------------------------------------------------------

def test_sums_and_install(tmp_path: Path) -> None:
    assert parse_sums("abc  x.exe\n" + "e" * 64 + "  project-reclaimer-dedicated-v0.9.19.exe\n" + "f" * 64 + " *RCON.md\n") == {
        "project-reclaimer-dedicated-v0.9.19.exe": "e" * 64, "RCON.md": "f" * 64}
    root = tmp_path / "reclaimer"
    write_root(root, list(zip(["Alpha", "Beta"], free_ports(2))), 'backend = "attach"\n', staff(), exe=True)
    (root / "project-reclaimer.exe").write_bytes(b"root old")
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(f"{name} old".encode())
    settings = load(root / "settings.toml")
    new = tmp_path / "project-reclaimer-dedicated-v0.9.19.exe"
    new.write_bytes(b"new program")
    replaced = install_program(settings, new, "0.9.19")
    assert [p.parent.name for p in replaced] == ["reclaimer", "Alpha", "Beta"]
    assert all(p.read_bytes() == b"new program" for p in replaced)
    backups = list((settings.records / "previous-program").glob("*/*/project-reclaimer.exe"))
    assert sorted(b.read_bytes() for b in backups) == [b"Alpha old", b"Beta old", b"root old"]


def test_a_copy_as_new_or_newer_is_never_replaced(tmp_path: Path, monkeypatch, run, caplog) -> None:
    import logging

    root = tmp_path / "reclaimer"
    write_root(root, list(zip(["Alpha", "Beta", "Gamma"], free_ports(3))), 'backend = "attach"\n', staff(),
               exe=True)
    versions = {"Alpha": "0.9.18", "Beta": "0.9.21", "Gamma": "0.9.20"}  # Beta: a pre-release someone put in
    for name in versions:
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(name.encode())
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: versions.get(path.parent.name))
    settings = load(root / "settings.toml")
    release, payload = program_release("0.9.20"), b"program 0.9.20"
    serve_program(monkeypatch, release, payload)
    lifecycle = ProgramLifecycle(up=("Alpha", "Beta", "Gamma"))
    clients = {name: type("Client", (), {"version": version})() for name, version in versions.items()}
    service = ProgramUpdateService(settings, EventBus(), lifecycle, {n: ServerState(n) for n in versions},  # type: ignore
                                   clients)  # type: ignore[arg-type]

    async def scenario() -> None:
        ok, message = await service.install(when="after_game")
        assert ok and message.startswith("server program 0.9.20 installed in 1 place(s); restarting Alpha after game")
        assert lifecycle.restarts == [("Alpha", "after_game", "updating to 0.9.20")]
        assert [(root / "SERVERS" / n / "project-reclaimer.exe").read_bytes() for n in versions] == [
            payload, b"Beta", b"Gamma"]
        # Nothing older left: nothing is changed at all.
        versions["Alpha"] = "0.9.20"
        (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").write_bytes(b"Alpha")
        service.pending, service.last_installed = release, None
        ok, message = await service.install(when="now")
        assert not ok and message == "not installed: every copy is Project Reclaimer 0.9.20 or newer already; " \
                                     "nothing was changed"

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert f"Left {root / 'SERVERS' / 'Beta' / 'project-reclaimer.exe'} as it is: it is Project Reclaimer 0.9.21, " \
           "newer than 0.9.20" in caplog.text
    assert f"Left {root / 'SERVERS' / 'Gamma' / 'project-reclaimer.exe'} as it is: it is Project Reclaimer 0.9.20 " \
           "already" in caplog.text


def test_the_program_update_service_installs_and_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, list(zip(["Alpha", "Beta"], free_ports(2))), 'backend = "attach"\n', staff(), exe=True)
    settings = load(root / "settings.toml")
    release = ProgramRelease("0.9.19", "https://example/r", "## New\n- faster", "https://example/x.exe",
                             "project-reclaimer-dedicated-v0.9.19.exe", "https://example/sums")
    payload = b"the new program"
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)

    def fake_get(url: str, timeout: float, accept: str = "") -> bytes:
        if url.endswith("sums"):
            return (hashlib.sha256(payload).hexdigest() + "  project-reclaimer-dedicated-v0.9.19.exe\n").encode()
        return payload

    monkeypatch.setattr(serverupdates, "_get", fake_get)

    class Client:
        def __init__(self, version: str | None) -> None:
            self.version = version

    class Lifecycle:
        def __init__(self) -> None:
            self.restarts: list[tuple[str, str, str]] = []

        def is_up(self, name: str) -> bool:
            return name == "Alpha"

        async def restart(self, server: str, *, when: str, reason: str, by: str | None = None, countdown=None) -> Any:
            self.restarts.append((server, when, reason))

    lifecycle = Lifecycle()
    clients = {"Alpha": Client("0.9.18"), "Beta": Client(None)}
    states = {"Alpha": ServerState("Alpha"), "Beta": ServerState("Beta")}
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("server.program_updated", seen.append)
    service = ProgramUpdateService(settings, bus, lifecycle, states, clients)  # type: ignore[arg-type]

    async def scenario() -> None:
        found = await service.check(quiet=False)
        assert found is release and service.pending is release and service.is_newer(release) is True
        ok, message = await service.install(when="after_game")
        assert ok, message
        assert "installed in 2 place(s)" in message and "restarting Alpha after game" in message
        assert lifecycle.restarts == [("Alpha", "after_game", "updating to 0.9.19")]
        assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == payload
        assert service.last_installed == "0.9.19" and service.pending is None and seen
        clients["Alpha"].version = "0.9.19"
        assert await service.check(quiet=False) is None  # now the newest
        # A checksum that doesn't match changes nothing.
        service.last_installed = None
        clients["Alpha"].version = "0.9.18"
        monkeypatch.setattr(serverupdates, "_get", lambda url, t, accept="": b"x" if url.endswith("exe") else
                            ("0" * 64 + "  project-reclaimer-dedicated-v0.9.19.exe\n").encode())
        ok, message = await service.install(when="now")
        assert not ok and "checksum" in message
        assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == payload

    run(scenario())


def self_updating_root(tmp_path: Path, *auto: str, body: str = 'backend = "attach"\n') -> Path:
    """Alpha and Beta, each with its own program; the ones named in `auto` have auto_update = true."""
    root = tmp_path / "reclaimer"
    write_root(root, list(zip(["Alpha", "Beta"], free_ports(2))), body, staff(), exe=True)
    for name in auto:
        config = root / "SERVERS" / name / "dedicated.toml"
        config.write_text("auto_update = true\n" + config.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def test_servers_that_update_themselves_are_left_to_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    root = self_updating_root(tmp_path, "Beta")
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(f"{name} old".encode())
    settings = load(root / "settings.toml")
    assert [s.auto_update for s in settings.servers] == [False, True]
    assert serverupdates.self_updating(settings) == {"Beta"}
    new = tmp_path / "project-reclaimer-dedicated-v0.9.19.exe"
    new.write_bytes(b"new program")
    replaced = install_program(settings, new, "0.9.19")
    assert [p.parent.name for p in replaced] == ["Alpha"]  # Beta's copy is Beta's own updater's business
    assert (root / "SERVERS" / "Beta" / "project-reclaimer.exe").read_bytes() == b"Beta old"
    # 0.2.1's [process] disable_auto_update does nothing now (each server's auto_update decides), and says so.
    old = load(self_updating_root(tmp_path / "old", "Beta",
                                  body='backend = "process"\n[process]\ndisable_auto_update = true\n')
               / "settings.toml")
    assert serverupdates.self_updating(old) == {"Beta"}
    assert len(old.retired) == 1 and "[process] disable_auto_update does nothing any more" in old.retired[0]
    assert "auto_update" in old.retired[0] and not settings.retired

    class Lifecycle:
        def __init__(self) -> None:
            self.restarts: list[tuple[str, str, str]] = []

        def is_up(self, name: str) -> bool:
            return True

        async def restart(self, server: str, *, when: str, reason: str, by: str | None = None, countdown=None) -> Any:
            self.restarts.append((server, when, reason))

    class Client:
        def __init__(self, version: str | None) -> None:
            self.version = version

    release = ProgramRelease("0.9.19", "https://example/r", "", "https://example/x.exe",
                             "project-reclaimer-dedicated-v0.9.19.exe", None)
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)
    monkeypatch.setattr(serverupdates, "_get", lambda url, timeout, accept="": b"the new program")
    lifecycle = Lifecycle()
    states = {"Alpha": ServerState("Alpha"), "Beta": ServerState("Beta")}
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, states,  # type: ignore[arg-type]
                                   {"Alpha": Client("0.9.18"), "Beta": Client("0.9.18")})

    async def scenario() -> None:
        ok, message = await service.install(when="after_game")
        assert ok and "restarting Alpha after game" in message and "Beta update themselves" in message
        assert lifecycle.restarts == [("Alpha", "after_game", "updating to 0.9.19")]
        assert service.status()["self_updating"] == ["Beta"]
        # With every server updating itself there's nothing for Huragok to install.
        for server in settings.servers:
            server.auto_update = True
        service.last_installed, service.pending = None, release
        ok, message = await service.install(when="now")
        assert not ok and "every server updates itself" in message

    run(scenario())


def test_one_server_updating_itself_brings_the_others_along(tmp_path: Path, run, caplog) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta")
    settings = load(root / "settings.toml")

    class Lifecycle:
        def __init__(self) -> None:
            self.restarts: list[tuple[str, str, str]] = []

        def is_up(self, name: str) -> bool:
            return True

        async def restart(self, server: str, *, when: str, reason: str, by: str | None = None, countdown=None) -> Any:
            self.restarts.append((server, when, reason))

    lifecycle = Lifecycle()
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("server.program_updated", seen.append)
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        await asyncio.sleep(0)
        assert lifecycle.restarts == [] and not seen  # first sight of a version is no update
        bus.emit("server.updating", "Alpha")
        bus.emit("rcon.connected", "Alpha", version="0.9.19")  # back on the new version, seconds later
        await wait_for(lambda: lifecycle.restarts)
        assert lifecycle.restarts == [("Beta", "after_game", "Project Reclaimer 0.9.19 is out; it updates as it starts")]
        assert seen and seen[0].get("before") == "0.9.18"
        bus.emit("rcon.connected", "Beta", version="0.9.19")  # Beta caught up: nobody left to follow
        await asyncio.sleep(0.05)
        assert len(lifecycle.restarts) == 1

    import logging

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = caplog.text
    assert "installed a new release by itself (auto_update)" in text and "now on Project Reclaimer 0.9.19" in text
    assert "Following Alpha onto Project Reclaimer 0.9.19: Beta restart after their game" in text


def program_release(version: str = "0.9.19", published: float | None = None) -> ProgramRelease:
    return ProgramRelease(version, f"https://example/v{version}", "", "https://example/x.exe",
                          f"project-reclaimer-dedicated-v{version}.exe", "https://example/sums", published)


def serve_program(monkeypatch: pytest.MonkeyPatch, release: ProgramRelease, payload: bytes) -> None:
    """GitHub, as far as the updater asks it: the release, its program and its checksum list."""
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)

    def fake_get(url: str, timeout: float, accept: str = "") -> bytes:
        if url.endswith("sums"):
            return (hashlib.sha256(payload).hexdigest() + f"  {release.exe_name}\n").encode()
        return payload

    monkeypatch.setattr(serverupdates, "_get", fake_get)


def test_the_programs_own_update_lines_are_read() -> None:
    assert parse_console_line("Automatic updates enabled: checking now and hourly; updates restart all servers.") == (
        "server.self_update", {"text": "Automatic updates enabled: checking now and hourly; updates restart all "
                                       "servers"})
    assert parse_console_line("Automatic updates requested, but the update feed is disabled (local build or "
                              "RECLAIMER_UPDATE_URL=off).")[0] == "server.self_update"
    assert parse_console_line("Downloading Project Reclaimer 0.9.19...") == (
        "server.update_downloading", {"version": "0.9.19"})
    assert parse_console_line("11:08:01 Automatic update failed; retrying in an hour: The download stopped before "
                              "it finished. Try again.") == (
        "server.update_failed", {"clock": "11:08:01", "reason": "The download stopped before it finished. Try again"})


def test_a_self_update_that_didnt_arrive_is_installed_after_the_game(tmp_path: Path, monkeypatch, run) -> None:
    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(f"{name} 0.9.18".encode())
    (root / "SERVERS" / "Alpha" / "project-reclaimer.exe.old").write_bytes(b"0.9.17")
    settings = load(root / "settings.toml")
    release, payload = program_release(published=time.time() - 3 * 3600), b"program 0.9.19"
    serve_program(monkeypatch, release, payload)
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        await service.check()
        # Alpha has only just been seen: it gets its look (at its start, then hourly) first.
        assert service.taken == {} and lifecycle.restarts == []
        assert "should have updated itself by" in service.note("Alpha") and service.note("Beta") == ""
        later = time.time() + serverupdates.OVERDUE + 1
        assert service.review(now=later) == ["Alpha"]  # Beta (auto_update off) is only told
        await wait_for(lambda: lifecycle.restarts)
        assert lifecycle.restarts == [("Alpha", "after_game", "updating to Project Reclaimer 0.9.19")]
        assert service.review(now=later) == []  # once
        assert lifecycle.flags[("Alpha", "program")] == "Project Reclaimer 0.9.19: installs after this game"
        assert "Huragok is installing Project Reclaimer 0.9.19" in service.note("Alpha")
        alpha = root / "SERVERS" / "Alpha"
        said = await lifecycle.jobs["Alpha"]["program"].run()  # Alpha is stopped after its game
        assert said.startswith(f"Project Reclaimer 0.9.19 put in place at {alpha / 'project-reclaimer.exe'}")
        assert (alpha / "project-reclaimer.exe").read_bytes() == payload
        assert not (alpha / "project-reclaimer.exe.old").exists()  # nothing runs it while it's stopped
        assert [b.read_bytes() for b in (settings.records / "previous-program").glob("*/Alpha/*.exe")] == [
            b"Alpha 0.9.18"]
        assert (root / "SERVERS" / "Beta" / "project-reclaimer.exe").read_bytes() == b"Beta 0.9.18"
        assert ("Alpha", "program") not in lifecycle.flags
        bus.emit("rcon.connected", "Alpha", version="0.9.19")
        assert service.taken == {} and service.note("Alpha") == ""
        # The download is kept for the next server, and only the newest one.
        assert [p.name for p in (settings.records / "program-download").iterdir()] == [release.exe_name]

    run(scenario())


def test_a_failed_self_update_is_taken_over_at_once(tmp_path: Path, monkeypatch, run, caplog) -> None:
    import logging

    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    release = program_release(published=time.time())
    serve_program(monkeypatch, release, b"program 0.9.19")
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("server.self_update", "Beta", text="Automatic updates enabled: checking now and hourly")
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        await service.check()
        assert service.updater == {"Beta": "on"} and lifecycle.restarts == []
        bus.emit("server.update_failed", "Beta", reason="The download stopped before it finished. Try again")
        await wait_for(lambda: lifecycle.restarts)
        assert lifecycle.restarts == [("Beta", "after_game", "updating to Project Reclaimer 0.9.19")]
        assert service.note("Beta").startswith("Huragok is installing Project Reclaimer 0.9.19")
        # Its own retry worked before its game ended: Huragok's restart and job are called off, and the
        # `dedicated` left running from the old file is restarted on the new one once nobody is on.
        (root / "SERVERS" / "Beta" / "project-reclaimer.exe.old").write_bytes(b"0.9.18")
        bus.emit("server.updating", "Beta")
        bus.emit("rcon.connected", "Beta", version="0.9.19")
        await wait_for(lambda: len(lifecycle.restarts) == 3)
        assert lifecycle.cancelled == ["Beta"] and "program" not in lifecycle.jobs["Beta"]
        assert set(lifecycle.restarts[1:]) == {
            ("Beta", "when_empty", "its own update left `dedicated` running from the old program file; restarting "
                                   "on the new one"),
            ("Alpha", "after_game", "Project Reclaimer 0.9.19 is out; it updates as it starts")}
        said = await lifecycle.jobs["Beta"]["program-old"].run()
        assert said == "removed project-reclaimer.exe.old (the program from before its update)"
        assert service.taken == {} and service.failed == {}

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = caplog.text
    assert "[Beta] its own update failed: The download stopped before it finished. Try again" in text
    assert ("[Beta] its own update failed (The download stopped before it finished. Try again): Huragok installs "
            "Project Reclaimer 0.9.19 after its game") in text


def test_a_server_that_updated_itself_as_it_started_is_moved_off_the_old_file(tmp_path: Path, run, caplog) -> None:
    import logging

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha")}, {})  # type: ignore

    async def scenario() -> None:
        bus.emit("server.updating", "Alpha")  # before its console ever signed in on the old version
        bus.emit("rcon.connected", "Alpha", version="0.9.20")
        await wait_for(lambda: lifecycle.restarts)
        assert lifecycle.restarts[0][:2] == ("Alpha", "when_empty") and "program-old" in lifecycle.jobs["Alpha"]
        bus.emit("rcon.connected", "Alpha", version="0.9.20")  # after that restart: nothing more
        await asyncio.sleep(0.05)
        assert len(lifecycle.restarts) == 1

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "[Alpha] now on Project Reclaimer 0.9.20 (it updated itself as it started)" in caplog.text


OLD_PROGRAM_FAILS = ["Error: dedicated.toml: TOML parse error at line 47, column 1", "   |",
                     "47 | auto_reload = true", "   | ^^^^^^^^^^^",
                     "unknown field `auto_reload`, expected one of `game`, `content`, `data`, `bans`"]


def test_the_programs_version_comes_from_the_program(monkeypatch) -> None:
    from huragok.dedicated import Dedicated, ToolResult

    tool = Dedicated(Path("project-reclaimer.exe"))
    monkeypatch.setattr(tool, "run_sync", lambda *a, **k: ToolResult(True, 0, "project-reclaimer 0.9.20\n"))
    assert tool.version() == "0.9.20"
    monkeypatch.setattr(tool, "run_sync", lambda *a, **k: ToolResult(False, 2, "error: unexpected argument"))
    assert tool.version() is None


def test_a_program_older_than_its_settings_is_replaced_before_the_next_try(tmp_path: Path, monkeypatch, run,
                                                                          caplog) -> None:
    import logging

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(b"0.9.15")
    settings = load(root / "settings.toml")
    release, payload = program_release("0.9.20", published=time.time()), b"program 0.9.20"
    serve_program(monkeypatch, release, payload)
    versions = {"Alpha": "0.9.15", "Beta": "0.9.15"}
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: versions[path.parent.name])
    lifecycle = ProgramLifecycle(up=())
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("server.start_failed", "Alpha", why="the server exited while it was starting",
                 lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: "program" in lifecycle.jobs.get("Alpha", {}))
        said = await lifecycle.jobs["Alpha"]["program"].run()  # the next try's start, with it stopped
        assert said.startswith("Project Reclaimer 0.9.20 put in place at ")
        assert said.endswith("(Project Reclaimer 0.9.15 didn't know `auto_reload` in its dedicated.toml)")
        assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == payload
        # The newest doesn't know it either: that line is a mistake, said once.
        versions["Alpha"] = "0.9.20"
        lifecycle.jobs.clear()
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: ("Alpha", "auto_reload") in service.mistakes)
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await asyncio.sleep(0.05)
        assert lifecycle.jobs == {}
        # Beta has auto_update off: told what to do, its program left alone.
        bus.emit("server.start_failed", "Beta", lines=["unknown variant `player_choice`, expected one of "
                                                       "`first_person` in dedicated.toml"])
        await wait_for(lambda: "Beta" not in service.diagnosing and caplog.text.count("Huragok leaves it"))
        assert lifecycle.jobs == {}
        assert (root / "SERVERS" / "Beta" / "project-reclaimer.exe").read_bytes() == b"0.9.15"
        # A failure that isn't about a setting is none of this.
        bus.emit("server.start_failed", "Beta", lines=["Error: the port is in use"])
        await asyncio.sleep(0.05)
        assert "Beta" not in service.diagnosing

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = caplog.text
    assert ("[Alpha] it won't start: Project Reclaimer 0.9.15 is older than its dedicated.toml (it doesn't know "
            "`auto_reload`). Huragok puts Project Reclaimer 0.9.20 in place before it tries again") in text
    assert ("[Alpha] it won't start: its program is Project Reclaimer 0.9.20, the newest, and doesn't know "
            "`auto_reload` in its dedicated.toml: that line is a mistake. Fix or remove it (D, or E for the file), "
            "then start it again") in text
    assert text.count("that line is a mistake") == 1
    assert ("[Beta] it won't start: Project Reclaimer 0.9.15 is older than its dedicated.toml (it doesn't know the "
            "value `player_choice`; the newest is 0.9.20). auto_update is off, so Huragok leaves it: put the newest "
            "program in its folder (U -> The server program does it), or switch auto_update on (D)") in text


def test_a_pre_release_that_doesnt_know_a_setting_is_left_alone(tmp_path: Path, monkeypatch, run, caplog) -> None:
    import logging

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"' + chr(10))
    settings = load(root / "settings.toml")
    serve_program(monkeypatch, program_release("0.9.20"), b"program 0.9.20")
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: "0.9.21")
    lifecycle = ProgramLifecycle(up=())
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha")}, {})  # type: ignore

    async def scenario() -> None:
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: ("Alpha", "auto_reload") in service.mistakes)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert lifecycle.jobs == {}  # never back to the release
    assert ("its program is Project Reclaimer 0.9.21, newer than the newest release (0.9.20), and doesn't know "
            "`auto_reload`") in caplog.text


def test_a_program_older_than_its_settings_when_github_or_the_disk_says_no(tmp_path: Path, monkeypatch, run,
                                                                          caplog) -> None:
    import logging

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: "0.9.15")
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: (_ for _ in ()).throw(
        serverupdates.UpdateError("couldn't reach GitHub: offline")))
    lifecycle = ProgramLifecycle(up=())
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha")}, {})  # type: ignore

    async def scenario() -> None:
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: "isn't known" in caplog.text)
        await wait_for(lambda: "Alpha" not in service.diagnosing)
        # GitHub answers, but the download fails.
        release = program_release("0.9.20")
        monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)
        monkeypatch.setattr(serverupdates, "_get", lambda url, t, accept="": (_ for _ in ()).throw(
            serverupdates.UpdateError("GitHub answered 503 Unavailable")))
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: "couldn't be put in place" in caplog.text)
        await wait_for(lambda: "Alpha" not in service.diagnosing)
        # The download works, the swap doesn't.
        serve_program(monkeypatch, release, b"program 0.9.20")
        monkeypatch.setattr(serverupdates, "swap_program", lambda *a: (_ for _ in ()).throw(
            serverupdates.UpdateError("disk full")))
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: "program" in lifecycle.jobs.get("Alpha", {}))
        assert await lifecycle.jobs["Alpha"]["program"].run() == ""

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = caplog.text
    assert ("[Alpha] it won't start: its program doesn't know `auto_reload` in its dedicated.toml, and the newest "
            "release isn't known (GitHub couldn't be asked) to tell whether the program is too old") in text
    assert ("[Alpha] its program looks older than its dedicated.toml, but the newest couldn't be put in place: GitHub "
            "answered 503 Unavailable") in text
    assert "[Alpha] Project Reclaimer 0.9.20 didn't install: disk full; it tries the old one" in text


def test_huragok_gives_a_server_its_own_copy_rather_than_update_a_shared_one(tmp_path: Path) -> None:
    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").unlink()
    (root / "project-reclaimer.exe").write_bytes(b"0.9.18")  # both run the root's copy
    settings = load(root / "settings.toml")
    new = tmp_path / "download.exe"
    new.write_bytes(b"0.9.19")
    said = swap_program(settings, settings.server("Alpha"), new, "0.9.19")
    assert said == f"Project Reclaimer 0.9.19 put in place at {root / 'SERVERS' / 'Alpha' / 'project-reclaimer.exe'}"
    assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == b"0.9.19"
    assert (root / "project-reclaimer.exe").read_bytes() == b"0.9.18"  # Beta's (auto_update off) stays
    assert swap_program(settings, settings.server("Alpha"), new, "0.9.19") == \
        "its program file is Project Reclaimer 0.9.19 already"


def test_auto_update_needs_a_program_file_of_its_own(tmp_path: Path) -> None:
    from huragok.lifecycle import ProcessBackend

    root = self_updating_root(tmp_path, "Alpha", body="")
    settings = load(root / "settings.toml")
    backend = ProcessBackend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    assert backend.auto_update_problems(settings.server("Alpha")) == []
    for name in ("Alpha", "Beta"):  # both run the one copy at the root
        (root / "SERVERS" / name / "project-reclaimer.exe").unlink()
    (root / "project-reclaimer.exe").write_bytes(b"")
    found = backend.problems(settings.server("Alpha"))
    assert any("auto_update is on, but it runs" in p and "together with Beta" in p for p in found)
    assert not any("auto_update" in p for p in backend.problems(settings.server("Beta")))  # Beta doesn't update


def test_workshop_update_settings(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("Alpha", free_port())], 'backend = "attach"\n[workshop_updates]\ninstall = "when_empty"\n'
                                         'check_minutes = 10\n[[server]]\nname = "Alpha"\nworkshop_updates = "off"\n',
               staff())
    settings = load(root / "settings.toml")
    assert settings.workshop_updates.install == "when_empty" and settings.workshop_updates.check_minutes == 10
    assert settings.workshop_install(settings.server("Alpha")) == "off"
    assert settings.server_updates.follow and settings.setup.auto_update
    for body, error in (('[workshop_updates]\ninstall = "later"\n', "after_game, when_empty, off"),
                        ("[workshop_updates]\ncheck_minutes = 2\n", "5 to 10080"),
                        ('[[server]]\nname = "Alpha"\nworkshop_updates = "x"\n', "must be one of")):
        write_root(root, [("Alpha", free_port())], 'backend = "attach"\n' + body, staff())
        with pytest.raises(ConfigError, match=error):
            load(root / "settings.toml")


# The relay ----------------------------------------------------------------------------------------

def test_required_roles() -> None:
    permissions = Permissions()  # every command at its default role
    assert required_role("players", permissions) == "moderator" and required_role("kick #3 x", permissions) == "moderator"
    assert required_role("ban #3 7d x", permissions) == "admin" and required_role("say hi", permissions) == "admin"
    assert required_role("nextmap", permissions) == "moderator" and required_role("nextmap Guardian", permissions) == "admin"
    assert required_role("admin", permissions) == "moderator" and required_role("admin add #2", permissions) == "owner"
    assert required_role("reload", permissions) == "owner" and required_role("password x", permissions) == "owner"
    assert required_role("banip #3", permissions) == "admin" and required_role("frobnicate", permissions) == "owner"


class StubClient:
    def __init__(self) -> None:
        self.version = "0.9.18"
        self.connected = True
        self.sent: list[tuple[str, tuple[str, ...], str | None]] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args, by))
        if command.startswith("players"):
            return Reply(True, "1/16 players", {"count": 1}, {})
        return Reply(True, f"did {command}", None, {})


def test_the_relay_signs_moderators_in_and_gates_commands(tmp_path: Path, run) -> None:
    from websockets.asyncio.client import connect

    root = tmp_path / "reclaimer"
    port, relay_port = free_ports(2)
    offset = relay_port - port  # any whole number: the relay listens at game port + offset
    body = (f'backend = "attach"\n[relay]\nenabled = true\nport_offset = {offset}\n'
            '[[relay.user]]\nname = "Alice"\npassword = "alicealice"\nrole = "moderator"\n'
            '[[relay.user]]\nname = "Bob"\npassword = "bobbobbob"\nrole = "admin"\n')
    settings_path = write_root(root, [("Alpha", port)], body, staff())
    settings = load(settings_path)
    assert [u.name for u in settings.relay.users] == ["Alice", "Bob"]
    bus = EventBus()
    client = StubClient()
    relay = Relay(settings, {"Alpha": client}, {"Alpha": ServerState("Alpha")}, bus)  # type: ignore[arg-type]

    async def scenario() -> None:
        task = asyncio.ensure_future(relay.run())
        await wait_for(lambda: "Alpha" in relay.listeners)
        url = f"ws://127.0.0.1:{port + offset}/"
        async with connect(url, subprotocols=["reclaimer-rcon"]) as ws:
            await ws.send(json.dumps({"type": "auth", "password": "wrong"}))
            assert json.loads(await ws.recv()) == {"type": "auth", "ok": False, "error": "Wrong password."}
        async with connect(url, subprotocols=["reclaimer-rcon"]) as ws:
            await ws.send(json.dumps({"type": "auth", "password": "alicealice"}))
            auth = json.loads(await ws.recv())
            assert auth["ok"] and auth["server"] == "Alpha" and auth["version"] == "0.9.18" and auth["role"] == "moderator"
            assert relay.connected() == ["Alice (Alpha)"]
            await ws.send(json.dumps({"type": "command", "id": 1, "command": "players"}))
            reply = json.loads(await ws.recv())
            assert reply == {"type": "reply", "id": 1, "ok": True, "text": "1/16 players", "data": {"count": 1}}
            await ws.send(json.dumps({"type": "command", "id": 2, "command": "kick", "args": ["#3", "camping"]}))
            reply = json.loads(await ws.recv())
            assert reply["ok"] and client.sent[-1] == ("kick", ("#3", "camping"), "Alice")
            await ws.send(json.dumps({"type": "command", "id": 3, "command": "ban #3 7d"}))
            reply = json.loads(await ws.recv())
            assert not reply["ok"] and "needs the admin role" in reply["text"] and len(client.sent) == 2
            bus.publish(Event("rcon.join", "Alpha", {"number": 4, "name": "Dee", "id": "d" * 64, "ip": "203.0.113.4",
                                                   "admin": False}))
            bus.publish(Event("rcon.chat", "Beta", {"channel": "all", "name": "x", "text": "other server"}))
            event = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert event["type"] == "event" and event["event"] == "join" and event["name"] == "Dee"
            assert "ip" not in event  # moderators don't get addresses
            await ws.send("not json")
            assert json.loads(await ws.recv())["type"] == "error"
        await wait_for(lambda: not relay.connected())
        async with connect(url, subprotocols=["reclaimer-rcon"]) as ws:
            await ws.send(json.dumps({"type": "auth", "password": "bobbobbob"}))
            assert json.loads(await ws.recv())["role"] == "admin"
            await ws.send(json.dumps({"type": "command", "id": 9, "command": "ban #3 7d"}))
            assert json.loads(await ws.recv())["ok"]
            bus.publish(Event("rcon.join", "Alpha", {"number": 5, "name": "Eve", "ip": "203.0.113.5"}))
            assert json.loads(await asyncio.wait_for(ws.recv(), 5))["ip"] == "203.0.113.5"
        for _ in range(5):  # five wrong passwords lock the address out of the relay, not the server
            async with connect(url, subprotocols=["reclaimer-rcon"]) as ws:
                await ws.send(json.dumps({"type": "auth", "password": "nope"}))
                await ws.recv()
        async with connect(url, subprotocols=["reclaimer-rcon"]) as ws:
            await ws.send(json.dumps({"type": "auth", "password": "alicealice"}))
            assert "Too many wrong passwords" in json.loads(await ws.recv())["error"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())


def test_relay_settings_are_checked(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    for body, message in (
        ('[relay]\n[[relay.user]]\nname = "A"\npassword = "short"\n', "8 to 128"),
        ('[relay]\n[[relay.user]]\nname = "A"\npassword = "samesamesame"\n[[relay.user]]\nname = "B"\n'
         'password = "samesamesame"\n', "same password"),
        ('[relay]\n[[relay.user]]\nname = "A"\npassword = "longenough"\nrole = "god"\n', "role must be"),
        ('[server_updates]\ninstall = "later"\n', "ask, after_game or now"),
    ):
        write_root(root, [("Alpha", free_port())], 'backend = "attach"\n' + body, staff())
        with pytest.raises(ConfigError, match=message):
            load(root / "settings.toml")
    write_root(root, [("Alpha", free_port())], 'backend = "attach"\n[server_updates]\ninstall = "after_game"\ncheck_hours = 0.1\n',
               staff())
    settings = load(root / "settings.toml")
    assert settings.server_updates.install == "after_game" and settings.server_updates.check_hours == 1.0
    assert settings.relay.enabled is False and settings.relay.port_offset == 1000
