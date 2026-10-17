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
from conftest import free_port, free_ports, staff, wait_for, write_root
from fake_dedicated import TEMPLATE
from test_menu import FakeTerminal
from test_setup import StrictKeyboard

from reclaimer_supervisor import serverupdates
from reclaimer_supervisor.config import ConfigError, load
from reclaimer_supervisor.events import Event, EventBus
from reclaimer_supervisor.livesettings import FOLDER_SETTINGS, SERVER_SETTINGS, catalogue, parse, write
from reclaimer_supervisor.manager import Result
from reclaimer_supervisor.menu import Menu
from reclaimer_supervisor.menu_manage import describe_event
from reclaimer_supervisor.permissions import Permissions, load_permissions
from reclaimer_supervisor.rcon import Reply
from reclaimer_supervisor.relay import Relay, required_role
from reclaimer_supervisor.serverupdates import ProgramRelease, ProgramUpdateService, install_program, parse_sums
from reclaimer_supervisor.state import ServerState
from reclaimer_supervisor.tomledit import read_toml, replace_server_blocks
from reclaimer_supervisor.tui import RecentLog


def setting(key: str, scope: str = "server") -> Any:
    return next(s for s in [*SERVER_SETTINGS, *FOLDER_SETTINGS] if s.key == key and s.scope == scope)


# Settings changed live ---------------------------------------------------------------------------

def test_the_settings_catalogue_reads_parses_and_writes(tmp_path: Path) -> None:
    config = tmp_path / "dedicated.toml"
    config.write_text(TEMPLATE, encoding="utf-8")
    replace_server_blocks(config, [{"name": "Slayer", "port": 49190, "playlist": "playlists/s.playlist.json",
                                    "max_players": 12}])
    entries = {(s.key, s.scope): (v, where) for s, v, where in catalogue(config, "Slayer")}
    assert entries[("max_players", "server")] == (12, "this server")
    assert entries[("votes", "server")] == (True, "[defaults]")
    assert entries[("max_ping", "server")] == (None, "unset")
    assert entries[("host_name", "folder")] == ("Dedicated server", "the file")
    assert entries[("password", "rcon")] == ("", "[rcon]")
    assert parse(setting("votes"), "off") is False and parse(setting("max_ping"), "150") == 150
    assert parse(setting("welcome"), "hi {name}, rules in Discord") == ["hi {name}", "rules in Discord"]
    assert parse(setting("team_changing"), "OFF") == "off"
    for key, text in (("max_players", "200"), ("max_players", "x"), ("votes", "maybe"), ("team_changing", "never"),
                      ("password", "rcon")):
        with pytest.raises(ValueError):
            parse(setting(key, "rcon" if text == "rcon" else "server"), "short" if text == "rcon" else text)
    write(config, "Slayer", setting("max_players"), 8)
    write(config, "Slayer", setting("welcome"), ["hi"])
    write(config, "Slayer", setting("host_name", "folder"), "LG")
    write(config, "Slayer", setting("password", "rcon"), "longenoughpassword")
    write(config, "Slayer", setting("enabled", "master"), False)
    data = read_toml(config)
    assert data["server"][0]["max_players"] == 8 and data["server"][0]["welcome"] == ["hi"]
    assert data["host_name"] == "LG" and data["rcon"]["password"] == "longenoughpassword"
    assert data["master"]["enabled"] is False and "# Relative paths" in config.read_text(encoding="utf-8")


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
            return [("supervisor", Result(True, "server program 0.9.19 is out (Alpha 0.9.18)", "notes"))]
        if verb == "update_program":
            return [("supervisor", Result(True, f"installed; restarting {options.get('when')}"))]
        return [(server, Result(True, "done"))]

    def request_quit(self) -> None:
        pass


def manage_root(tmp_path: Path) -> Path:
    root = tmp_path / "reclaimer"
    folder = root / "SERVERS" / "Alpha"
    folder.mkdir(parents=True)
    (folder / "dedicated.toml").write_text(TEMPLATE, encoding="utf-8")
    replace_server_blocks(folder / "dedicated.toml", [{"name": "Alpha", "port": 49190, "map": "Guardian", "game": "Slayer"}])
    from reclaimer_supervisor.tomledit import set_table_key

    set_table_key(folder / "dedicated.toml", "rcon", "password", "secretsecret")
    (folder / "project-reclaimer.exe").write_bytes(b"")
    return write_root(root, [], 'backend = "attach"\n', staff())


def test_the_manage_screens(tmp_path: Path, run) -> None:
    settings_path = manage_root(tmp_path)
    controller = ManageController(settings_path)
    max_players = next(i for i, s in enumerate(SERVER_SETTINGS, 1) if s.key == "max_players")
    public_address = len(SERVER_SETTINGS) + next(i for i, s in enumerate(FOLDER_SETTINGS, 1) if s.key == "public_address")
    keys = [
        # A: the console on Alpha, name Alice, one command (an event arrives with the reply), then out.
        "A", "1", "ENTER", *"Alice", "ENTER", *"players", "ENTER", "ENTER",
        # D: max_players -> 8, then public_address -> play.example.com with a restart, then back.
        "D", "1", "ENTER", *str(max_players), "ENTER", *"8", "ENTER",
        *str(public_address), "ENTER", *"play.example.com", "ENTER", "Y", "ESC",
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
        ("restart", "Alpha", {"immediate": False, "reason": "public_address changed"}),
        ("rcon", "Alpha", {"text": "bans", "by": "Alice"}),
        ("rcon", "Alpha", {"text": "pardon Bravo", "by": "Alice"}),
        ("rcon", "Alpha", {"text": "admin add #2", "by": "Alice"}),
        ("check_program_update", "all", {}),
        ("update_program", "all", {"when": "after_game"}),
    ]
    assert "Alice: hi" in printed and "reply to players" in printed  # the event and the reply
    data = read_toml(settings_path.parent / "SERVERS" / "Alpha" / "dedicated.toml")
    assert data["server"][0]["max_players"] == 8 and data["public_address"] == "play.example.com"
    assert "at once; players stay connected" in printed and "needs this server restarted" in printed
    assert not controller.watchers  # the console screen stopped watching when it closed
    assert "0.9.19 is out" in printed and "restarting after_game" in printed


# The server program updates ----------------------------------------------------------------------

def test_sums_and_install(tmp_path: Path) -> None:
    assert parse_sums("abc  x.exe\n" + "e" * 64 + "  project-reclaimer-dedicated-v0.9.19.exe\n" + "f" * 64 + " *RCON.md\n") == {
        "project-reclaimer-dedicated-v0.9.19.exe": "e" * 64, "RCON.md": "f" * 64}
    root = tmp_path / "reclaimer"
    write_root(root, [("Alpha", 49190), ("Beta", 49191)], 'backend = "attach"\n', staff(), exe=True)
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


def test_the_program_update_service_installs_and_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, [("Alpha", 49190), ("Beta", 49191)], 'backend = "attach"\n', staff(), exe=True)
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
        write_root(root, [("Alpha", 49190)], 'backend = "attach"\n' + body, staff())
        with pytest.raises(ConfigError, match=message):
            load(root / "settings.toml")
    write_root(root, [("Alpha", 49190)], 'backend = "attach"\n[server_updates]\ninstall = "after_game"\ncheck_hours = 0.1\n',
               staff())
    settings = load(root / "settings.toml")
    assert settings.server_updates.install == "after_game" and settings.server_updates.check_hours == 1.0
    assert settings.relay.enabled is False and settings.relay.port_offset == 1000
