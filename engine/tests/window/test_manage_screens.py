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
from testkit.fake_dedicated import TEMPLATE
from testkit.manage import ManageController, StubClient, manage_root, program_release, self_updating_root, serve_program
from testkit.setup import StrictKeyboard
from testkit.window import FakeTerminal

from huragok.core.commandsfile import Permissions
from huragok.core.events import Event, EventBus
from huragok.core.logs import RecentLog
from huragok.core.settings import load
from huragok.core.tables import ConfigError
from huragok.core.tomledit import read_toml, replace_server_blocks, set_top_key
from huragok.players.relay import Relay, required_role
from huragok.program import releases as serverupdates
from huragok.program.releases import ProgramRelease
from huragok.program.settings import FOLDER_SETTINGS, SERVER_SETTINGS, can_reset, catalogue, parse, reset, write
from huragok.servers.logs import parse_console_line
from huragok.servers.state import ServerState
from huragok.updates import program as program_updates_module
from huragok.updates.program import ProgramUpdateService
from huragok.window.dashboard import Menu
from huragok.window.screens.manage import describe_event


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
    from huragok.core.tomledit import set_table_key

    set_table_key(config, "workshop", "items", [1, 2])  # the commented `# [workshop]` example comes to life
    text = config.read_text(encoding="utf-8")
    assert text.count("[workshop]") == 1 and "\nitems = [1, 2]\n" in text and "# update_check_minutes" in text
    assert read_toml(config)["workshop"] == {"items": [1, 2]}


def test_events_as_console_lines() -> None:
    assert describe_event(Event("rcon.chat", "S", {"channel": "all", "name": "Alice", "text": "gg"})).endswith(" Alice: gg")
    assert "[team] Bo: go" in describe_event(Event("rcon.chat", "S", {"channel": "team", "name": "Bo", "text": "go"}))
    assert "+ Cy (#3) [S] joined" in describe_event(Event("rcon.join", "S", {"name": "Cy", "number": 3}))
    assert "* kick Dee by Alice: camping" in describe_event(Event("rcon.kick", "S", {"name": "Dee", "by": "Alice",
                                                                                      "reason": "camping"}))
    assert "vote passed: kick Dee" in describe_event(Event("rcon.vote", "S", {"state": "passed", "subject": "kick Dee"}))
    assert "game: playing Guardian Slayer" in describe_event(Event("rcon.phase", "S", {"phase": "playing", "map": "Guardian",
                                                                                        "mode": "Slayer"}))
    assert describe_event(Event("rcon.kill", "S", {"killer": "1", "victim": "2"})) is None


def test_console_lines_write_every_player_as_their_tag() -> None:
    """Each player the one way (core/tags.py), with the #number they had on that server: what the host's own `kick #3`
    was, a 0.10 join before its name, the name as it comes in, a leave that names nobody, an address ban."""
    from huragok.window.whoswho import Known

    cy, dee, alice = "c" * 64, "d" * 64, "a" * 64
    rows = [{"name": "S", "online": [{"number": 3, "name": "Dee", "id": dee}]}]
    known = Known(lambda: rows, None)
    line = lambda kind, **data: describe_event(Event(kind, "S", data), known)  # noqa: E731
    assert "+ (no name yet) cccccccc (#2) [S] joined" in line("rcon.join", number=2, id=cy, name="")  # 0.10
    assert "= Cy cccccccc (#2) [S]: their name came in" in line("state.named", number=2, id=cy, name="Cy")
    known.learn(cy, "Cy")
    assert "- Cy cccccccc (#2) [S] left" in line("rcon.leave", number=2, id=cy, name="")
    assert "[team] Cy cccccccc (#2) [S]: go" in line("rcon.chat", channel="team", number=2, id=cy, name="Cy", text="go")
    # The kick the host typed as `kick #3`: who #3 was, and who did it by their tag too.
    assert "* kick Dee dddddddd (#3) [S] by Alice aaaaaaaa: camping" in line(
        "rcon.kick", id=dee, name="Dee", by="Alice", by_id=alice, reason="camping")
    assert "* ban 203.0.113.4 by Alice" in line("rcon.ban", ip="203.0.113.4", by="Alice")  # no name: the address
    assert "* unban Gone by the Superintendent" in line("rcon.unban", target="Gone", by="the Superintendent")
    assert "* role moderator given to Dee dddddddd (#3) [S] by Alice" in line("rcon.role_added", id=dee, role="moderator",
                                                                               by="Alice")
    assert "* a role taken from Dee dddddddd (#3) [S]" in line("rcon.role_removed", id=dee, name="Dee")


def test_the_manage_screens(tmp_path: Path, run) -> None:
    """A running server from the dashboard: the console with its events, D (its own file shown read-only, Huragok's
    own for it changed through the supervisor), bans, and the server program's updates."""
    settings_path = manage_root(tmp_path)
    controller = ManageController(settings_path)
    config = settings_path.parent / "SERVERS" / "Alpha" / "dedicated.toml"
    before = config.read_text(encoding="utf-8")
    keys = [
        # A: the console on Alpha, name Alice, one command (an event arrives with the reply), then out.
        "A", "1", "ENTER", *"Alice", "ENTER", *"players", "ENTER", "ENTER",
        # D: Joining, shown read-only (0.7.0: the server's own file is the host's); Huragok's own (H) -> autostart
        # off; then back out of the group, the server's page and D's list.
        "D", "1", "ENTER", "1", "ENTER", "ESC",
        "H", "ENTER", "1", "ENTER", "N", "ESC", "ESC", "ESC",
        # B: list bans, pardon Bravo, add an admin, back.
        "B", "1", "ENTER", "L", "P", *"Bravo", "ENTER", "+", *"#2", "ENTER", "ESC",
        # U: the server program: a look, nothing installed.
        "U", "2", "x",
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
        ("server_setting", "Alpha", {"key": "autostart", "value": False}),
        ("rcon", "Alpha", {"text": "bans", "by": "Alice"}),
        ("rcon", "Alpha", {"text": "pardon Bravo", "by": "Alice"}),
        ("rcon", "Alpha", {"text": "admin add #2", "by": "Alice"}),
        ("check_program_update", "all", {}),
    ]
    assert "Alice: hi" in printed and "reply to players" in printed  # the event and the reply
    assert "max_players" in printed and "read-only · O opens dedicated.toml in your editor" in printed
    assert config.read_text(encoding="utf-8") == before  # shown, never written
    assert "Takes effect: at once: Huragok's own setting" in printed
    assert not controller.watchers  # the console screen stopped watching when it closed
    assert "0.9.19 is out" in printed and "Huragok installs nothing" in printed


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


def test_the_admins_lists_show_who_each_is(tmp_path: Path, run) -> None:
    """dedicated.toml's admins and vote_kick_immune are bare player IDs: shown with the name Huragok's records have
    (in the list, their tags; where the list is typed again, each whole). The file keeps the IDs alone."""
    import io

    from huragok.core.storage import Storage

    settings_path = manage_root(tmp_path)
    database = tmp_path / "huragok.sqlite3"
    settings_path.write_text(settings_path.read_text(encoding="utf-8").replace(
        'database = ""', f'database = "{database.as_posix()}"'), encoding="utf-8")
    storage = Storage(database)
    storage.record_join("a" * 64, "Alice", None)
    storage.close()
    controller = ManageController(settings_path)
    settings = controller.refresh_settings()
    server = settings.servers[0]
    set_top_key(server.config_path, "admins", ["a" * 64, "e" * 64])
    terminal = FakeTerminal()
    terminal.transcript = io.StringIO()
    menu = Menu(controller, terminal, StrictKeyboard(["ENTER", "N", "x"]), RecentLog())  # type: ignore[arg-type]
    admins = setting("admins", "folder")
    assert menu._setting_text(admins, ["a" * 64, "e" * 64]) == "Alice aaaaaaaa, (no name yet) eeeeeeee"
    s, v, w = next((e, v, w) for e, v, w in catalogue(server.config_path, server.name) if e is admins or
                   (e.key, e.scope) == ("admins", "folder"))
    run(menu._edit_setting(settings, server, controller.rows()[0], s, v, w))
    shown = "".join(terminal.output)
    assert f"Alice {'a' * 64}" in shown and "now the 2 above" in shown
    assert read_toml(server.config_path)["admins"] == ["a" * 64, "e" * 64]


def test_console_lines_that_set_a_password_are_masked() -> None:
    from huragok.rcon import masked

    assert masked("password hunter22") == "password ********"
    assert masked("PASSWORD   x y") == "PASSWORD ********"
    assert masked("password\thunter22") == "password ********"  # a tab hides nothing
    assert masked("password") == "password" and masked("players") == "players"
    assert masked("say my password is x") == "say my password is x"


# The server program updates ----------------------------------------------------------------------
#
# Since 0.7.0 each server installs its own releases (auto_update in its dedicated.toml): Huragok looks, restarts the
# ones that update themselves after their game, and warns. It never downloads a program or puts one in place.

def test_the_program_update_service_says_a_release_is_out_and_installs_nothing(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch,
                                                                             run) -> None:
    root = tmp_path / "reclaimer"
    write_root(root, list(zip(["Alpha", "Beta"], free_ports(2))), 'backend = "process"\n', staff(), exe=True)
    settings = load(root / "settings.toml")
    release = ProgramRelease("0.9.19", "https://example/r", "## New\n- faster", "https://example/x.exe",
                             "project-reclaimer-dedicated-v0.9.19.exe", "https://example/sums")
    asked: list[str] = []
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)
    monkeypatch.setattr(serverupdates, "_get", lambda url, *a, **k: asked.append(url) or b"")

    class Client:
        def __init__(self, version: str | None) -> None:
            self.version = version

    lifecycle = ProgramLifecycle(up=("Alpha",))
    clients = {"Alpha": Client("0.9.18"), "Beta": Client(None)}
    states = {"Alpha": ServerState("Alpha"), "Beta": ServerState("Beta")}
    service = ProgramUpdateService(settings, EventBus(), lifecycle, states, clients)  # type: ignore[arg-type]

    async def scenario() -> None:
        found = await service.check(quiet=False)
        assert found is release and service.pending is release and service.is_newer(release) is True
        # Both have auto_update off: pinned to their version by their own file. Nothing is restarted or put in place.
        assert lifecycle.restarts == [] and lifecycle.jobs == {} and asked == []
        assert [w.rule.id for w in service.warnings.of("Alpha")] == ["release-pinned"]
        assert service.note("Alpha") == "pinned by its own file (auto_update off): Project Reclaimer 0.9.19 is out"
        report = service.report()
        assert report[0].startswith("Alpha: Project Reclaimer 0.9.18 · auto_update off (pinned by its own file)")
        assert report[1] == "  Info: Pinned to Project Reclaimer 0.9.18 by its own file (auto_update = false), so it " \
                            "stays there: 0.9.19 is out. Nothing to do if that's on purpose; players need the " \
                            "server's exact version to join. To follow releases, set auto_update = true in its " \
                            "dedicated.toml (it counts from its next start)."
        clients["Alpha"].version = "0.9.19"
        service.versions.clear()
        assert await service.check(quiet=False) is None  # now the newest
        assert service.warnings.of("Alpha") == [] and service.pending is None

    run(scenario())
    assert (root / "SERVERS" / "Alpha" / "project-reclaimer.exe").read_bytes() == b""  # as it was


def test_servers_that_update_themselves_are_left_to_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    root = self_updating_root(tmp_path, "Beta", body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(f"{name} old".encode())
    settings = load(root / "settings.toml")
    assert [s.auto_update for s in settings.servers] == [False, True]
    assert serverupdates.self_updating(settings) == {"Beta"}
    # 0.2.1's [process] disable_auto_update does nothing now (each server's auto_update decides), and says so.
    old = load(self_updating_root(tmp_path / "old", "Beta",
                                  body='backend = "process"\n[process]\ndisable_auto_update = true\n')
               / "settings.toml")
    assert serverupdates.self_updating(old) == {"Beta"}
    assert len(old.retired) == 1 and "[process] disable_auto_update does nothing any more" in old.retired[0]
    assert "auto_update" in old.retired[0] and not settings.retired

    class Client:
        def __init__(self, version: str | None) -> None:
            self.version = version

    release = ProgramRelease("0.9.20", "https://example/r", "", "https://example/x.exe",
                             "project-reclaimer-dedicated-v0.9.20.exe", None)
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)
    lifecycle = ProgramLifecycle()
    states = {"Alpha": ServerState("Alpha"), "Beta": ServerState("Beta")}
    service = ProgramUpdateService(settings, EventBus(), lifecycle, states,  # type: ignore[arg-type]
                                   {"Alpha": Client("0.9.18"), "Beta": Client("0.9.18")})

    async def scenario() -> None:
        await service.check()
        # Beta installs it itself as it starts: restarted after its game (nobody on: at once), its players told once.
        assert lifecycle.restarts == [("Beta", "after_game", "Project Reclaimer 0.9.20 is out; it updates as it starts")]
        assert lifecycle.told["Beta"] == "[o_o] PARDON OUR DUST! Project Reclaimer 0.9.20 is out. This server restarts " \
                                         "for it after this game, so the update lands sooner. Update your game to rejoin."
        assert "Beta" in lifecycle.console  # if it was started some other way: through its own console's `restart`
        assert service.status()["self_updating"] == ["Beta"] and service.status()["restarting"] == {"Beta": "0.9.20"}
        assert service.note("Beta") == "Project Reclaimer 0.9.20 is out: it restarts after its game to install it"
        await service.review()
        assert len(lifecycle.restarts) == 1  # once per release
        # Alpha has auto_update off: only said.
        assert [w.rule.id for w in service.warnings.of("Alpha")] == ["release-pinned"]
        assert service.warnings.of("Beta") == []

    run(scenario())
    assert [(root / "SERVERS" / n / "project-reclaimer.exe").read_bytes() for n in ("Alpha", "Beta")] == [
        b"Alpha old", b"Beta old"]


def test_one_server_updating_itself_brings_the_others_along(tmp_path: Path, run, caplog) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    lifecycle = ProgramLifecycle()
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
        await wait_for(lambda: ("Beta", "after_game", "Project Reclaimer 0.9.19 is out; it updates as it starts")
                       in lifecycle.restarts)
        assert seen and seen[0].get("before") == "0.9.18"
        restarts = len(lifecycle.restarts)
        bus.emit("rcon.connected", "Beta", version="0.9.19")  # Beta caught up: its restart is called off
        await asyncio.sleep(0.05)
        assert lifecycle.cancelled == ["Beta"] and len(lifecycle.restarts) == restarts

    import logging

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = caplog.text
    assert "installed a new release by itself (auto_update)" in text and "now on Project Reclaimer 0.9.19" in text
    assert "Following Alpha onto Project Reclaimer 0.9.19: Beta restart after their game" in text


def test_with_follow_off_no_server_is_restarted_for_a_release(tmp_path: Path, monkeypatch, run) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n[server_updates]\nfollow = false\n')
    settings = load(root / "settings.toml")
    serve_program(monkeypatch, program_release("0.9.19", published=time.time()), b"program 0.9.19")
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        await service.check()  # Huragok sees it: nobody restarted
        bus.emit("server.updating", "Alpha")
        bus.emit("rcon.connected", "Alpha", version="0.9.19")  # one updated itself: still nobody
        await asyncio.sleep(0.05)
        assert await service.review(now=time.time() + 30) == []

    run(scenario())
    assert [r for r in lifecycle.restarts if r[1] == "after_game"] == []
    assert lifecycle.restarts == [("Alpha", "when_empty", "its own update left `dedicated` running from the old "
                                                          "program file; restarting on the new one")]


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


def test_a_self_update_that_didnt_arrive_is_warned_about_after_70_minutes(tmp_path: Path, monkeypatch, run) -> None:
    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    settings = load(root / "settings.toml")
    release = program_release(published=time.time() - 3 * 3600)
    serve_program(monkeypatch, release, b"program 0.9.19")
    lifecycle = ProgramLifecycle()
    bus = EventBus()
    raised: list[Event] = []
    bus.subscribe("warning.raised", raised.append)
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.9.18")
        bus.emit("rcon.connected", "Beta", version="0.9.18")
        await service.check()
        # Alpha has only just been seen: it gets its look (at its start, then hourly) first, and its restart after
        # its game (follow) may well come before that.
        assert service.warnings.of("Alpha") == []
        assert service.note("Alpha") == "Project Reclaimer 0.9.19 is out: it restarts after its game to install it"
        lifecycle.planned.clear()  # it didn't happen yet
        assert "should have updated itself by" in service.note("Alpha")
        later = time.time() + program_updates_module.OVERDUE + 1
        await service.review(now=later)
        [warning] = service.warnings.of("Alpha")
        assert warning.rule.id == "release-overdue" and warning.rule.discord  # the host must act: the staff channel
        assert warning.text.startswith("Still on Project Reclaimer 0.9.18, though 0.9.19 came out at ")
        assert warning.text.endswith("and auto_update is on: its own update didn't happen.")
        assert "logs/console-alpha.log" in warning.fix and "https://example/v0.9.19" in warning.fix
        await service.review(now=later)
        assert [e.get("rule") for e in raised if e.server == "Alpha"] == ["release-overdue"]  # once
        bus.emit("rcon.connected", "Alpha", version="0.9.19")  # it got there after all
        await service.review(now=later)
        assert service.warnings.of("Alpha") == []

    run(scenario())
    assert lifecycle.jobs == {}  # nothing put in place: Huragok installs no program


def test_a_failed_self_update_is_warned_about_at_once(tmp_path: Path, monkeypatch, run, caplog) -> None:
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
        assert service.updater == {"Beta": "on"} and len(lifecycle.restarts) == 2  # both, after their game
        bus.emit("server.update_failed", "Beta", reason="The download stopped before it finished. Try again")
        await wait_for(lambda: service.warnings.of("Beta"))
        [warning] = service.warnings.of("Beta")
        assert warning.rule.id == "release-overdue"
        assert warning.text.endswith("(it said: The download stopped before it finished. Try again).")
        assert service.note("Beta").startswith("Project Reclaimer 0.9.19 is out: it restarts after its game")
        # Its own retry worked before its game ended: Huragok's restart is called off, and the `dedicated` left
        # running from the old file is restarted on the new one once nobody is on.
        (root / "SERVERS" / "Beta" / "project-reclaimer.exe.old").write_bytes(b"0.9.18")
        bus.emit("server.updating", "Beta")
        bus.emit("rcon.connected", "Beta", version="0.9.19")
        await wait_for(lambda: len(lifecycle.restarts) == 3)
        assert lifecycle.cancelled == ["Beta"]
        assert lifecycle.restarts[2] == ("Beta", "when_empty", "its own update left `dedicated` running from the old "
                                                               "program file; restarting on the new one")
        said = await lifecycle.jobs["Beta"]["program-old"].run()
        assert said == "removed project-reclaimer.exe.old (the program from before its update)"
        assert service.failed == {} and service.old_file == {}
        await service.review()
        assert service.warnings.of("Beta") == []

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "[Beta] its own update failed: The download stopped before it finished. Try again" in caplog.text


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
    from huragok.program.dedicated import Dedicated, ToolResult

    tool = Dedicated(Path("project-reclaimer.exe"))
    monkeypatch.setattr(tool, "run_sync", lambda *a, **k: ToolResult(True, 0, "project-reclaimer 0.9.20\n"))
    assert tool.version() == "0.9.20"
    monkeypatch.setattr(tool, "run_sync", lambda *a, **k: ToolResult(False, 2, "error: unexpected argument"))
    assert tool.version() is None


def test_a_program_older_than_its_settings_is_said_and_never_replaced(tmp_path: Path, monkeypatch, run,
                                                                     caplog) -> None:
    import logging

    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    for name in ("Alpha", "Beta"):
        (root / "SERVERS" / name / "project-reclaimer.exe").write_bytes(b"0.9.15")
    settings = load(root / "settings.toml")
    serve_program(monkeypatch, program_release("0.9.20", published=time.time()), b"program 0.9.20")
    versions = {"Alpha": "0.9.15", "Beta": "0.9.15"}
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, path: versions[path.parent.name])
    lifecycle = ProgramLifecycle(up=())
    bus = EventBus()
    service = ProgramUpdateService(settings, bus, lifecycle, {"Alpha": ServerState("Alpha"),  # type: ignore[arg-type]
                                                              "Beta": ServerState("Beta")}, {})

    async def scenario() -> None:
        bus.emit("server.start_failed", "Alpha", why="the server exited while it was starting",
                 lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: "Alpha" not in service.diagnosing and "it won't start" in caplog.text)
        # The newest doesn't know it either: that line is a mistake, said once.
        versions["Alpha"] = "0.9.20"
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await wait_for(lambda: ("Alpha", "auto_reload") in service.mistakes)
        bus.emit("server.start_failed", "Alpha", lines=OLD_PROGRAM_FAILS)
        await asyncio.sleep(0.05)
        bus.emit("server.start_failed", "Beta", lines=["unknown variant `player_choice`, expected one of "
                                                       "`first_person` in dedicated.toml"])
        await wait_for(lambda: "Beta" not in service.diagnosing and "[Beta] it won't start" in caplog.text)
        # A failure that isn't about a setting is none of this.
        bus.emit("server.start_failed", "Beta", lines=["Error: the port is in use"])
        await asyncio.sleep(0.05)
        assert "Beta" not in service.diagnosing

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = caplog.text
    assert ("[Alpha] it won't start: Project Reclaimer 0.9.15 is older than its dedicated.toml (it doesn't know "
            "`auto_reload`; the newest is 0.9.20), and its own auto_update can't run before it reads its settings: put "
            "the newest program in its folder by hand (https://example/v0.9.20), or take that line out") in text
    assert ("[Alpha] it won't start: its program is Project Reclaimer 0.9.20, the newest, and doesn't know "
            "`auto_reload` in its dedicated.toml: that line is a mistake. Fix or remove it, then start it again") in text
    assert text.count("that line is a mistake") == 1
    assert "[Beta] it won't start: Project Reclaimer 0.9.15 is older than its dedicated.toml (it doesn't know the " \
           "value `player_choice`; the newest is 0.9.20)" in text
    assert lifecycle.jobs == {} and lifecycle.restarts == []  # nothing put in place
    assert [(root / "SERVERS" / n / "project-reclaimer.exe").read_bytes() for n in ("Alpha", "Beta")] == [b"0.9.15"] * 2


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


def test_a_program_older_than_its_settings_when_github_says_no(tmp_path: Path, monkeypatch, run, caplog) -> None:
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

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert ("[Alpha] it won't start: its program doesn't know `auto_reload` in its dedicated.toml, and the newest "
            "release isn't known (GitHub couldn't be asked) to tell whether the program is too old") in caplog.text
    assert lifecycle.jobs == {}


def test_auto_update_needs_a_program_file_of_its_own(tmp_path: Path) -> None:
    from huragok.servers.backends import ProcessBackend

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
    write_root(root, [("Alpha", free_port())], 'backend = "attach"\n[workshop_updates]\nrestart = false\n'
                                         '[[server]]\nname = "Alpha"\nworkshop_updates = "on"\n', staff())
    settings = load(root / "settings.toml")
    assert settings.workshop_updates.restart is False and settings.of(settings.server("Alpha")).workshop_updates == "on"
    assert settings.server_updates.follow and not hasattr(settings.setup, "auto_update")  # retired in 0.7.0
    write_root(root, [("Alpha", free_port())], 'backend = "attach"\n', staff())
    assert load(root / "settings.toml").workshop_updates.restart is True  # on unless set
    for body, error in (('[workshop_updates]\nrestart = "later"\n', "restart"),
                        ('[[server]]\nname = "Alpha"\nworkshop_updates = "x"\n', "must be one of on, off")):
        write_root(root, [("Alpha", free_port())], 'backend = "attach"\n' + body, staff())
        with pytest.raises(ConfigError, match=error):
            load(root / "settings.toml")


# The relay ----------------------------------------------------------------------------------------

def test_required_roles() -> None:
    permissions = Permissions()  # every command at its default role
    assert required_role("players", permissions) == "moderator" and required_role("kick #3 x", permissions) == "moderator"
    assert required_role("ban #3 7d x", permissions) == "admin" and required_role("say hi", permissions) == "admin"
    assert required_role("nextmap", permissions) == "moderator" and required_role("nextmap Guardian", permissions) == "admin"
    assert required_role("admin", permissions) == "moderator" and required_role("admin add #2", permissions) == "faculty"
    assert required_role("reload", permissions) == "faculty" and required_role("password x", permissions) == "faculty"
    assert required_role("banip #3", permissions) == "admin" and required_role("frobnicate", permissions) == "faculty"


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
    ):
        write_root(root, [("Alpha", free_port())], 'backend = "attach"\n' + body, staff())
        with pytest.raises(ConfigError, match=message):
            load(root / "settings.toml")
    write_root(root, [("Alpha", free_port())], 'backend = "attach"\n[server_updates]\ninstall = "after_game"\ncheck_hours = 0.1\n',
               staff())  # install: read and ignored since 0.7.0; check_hours: in minutes, 5 at least
    settings = load(root / "settings.toml")
    assert settings.server_updates.check_minutes == 6.0 and not hasattr(settings.server_updates, "install")
    assert settings.relay.enabled is False and settings.relay.port_offset == 1000
