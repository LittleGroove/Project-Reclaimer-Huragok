"""Setting servers up: the `dedicated` runner, new server folders (the program's own init, then the quick actions),
the window's N/G/C/E screens and the commands, all against the fake `dedicated` (Windows and Linux)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, free_ports, staff, write_settings
from testkit.fake_dedicated import TEMPLATE
from testkit.setup import (
    ALPHA, FAKE, PYTHON, SetupController, StrictKeyboard, fake_tool, make_server_folder, setup_root, write_playlist,
)
from testkit.window import FakeTerminal

from huragok.commandline import cli, firstrun
from huragok.content import quickactions
from huragok.content.newserver import SetupError, create_server, finish, read_folder, relative_posix, valid_name
from huragok.core import desktop, versions
from huragok.core.logs import RecentLog
from huragok.core.settings import load
from huragok.core.tomledit import (
    read_toml, replace_server_blocks, server_blocks, set_server_setting, set_table_key, set_top_key,
)
from huragok.program.dedicated import (
    DedicatedError, ToolResult, parse_content, parse_playlists, tool_for, workshop_references,
)
from huragok.window.dashboard import Menu


# The runner and what it reads -------------------------------------------------------------------

def test_parsing_the_programs_listings() -> None:
    content = '''Maps: map = "…" (and base_map), or a playlist game's "map"
  "Assembly"      or "descent"
  "Guardian"
  "The Pit"       or "cyberdyne"
Map variants: map = "…"; each plays on its own map
  From Halo 3:
  "MLG Guardian 8"   map variant · Guardian
Game types: game = "…", or a playlist game's "game"
  "Slayer"
Game variants: game = "…"
  From your content folder:
  "Gun Game"  game variant · Slayer
  49 from Halo 3 (search to list them: dedicated content <words>)

Names ignore case.'''
    items = parse_content(content)
    assert [(i.name, i.reference, i.kind) for i in items] == [
        ("Assembly", "descent", "map"), ("Guardian", "Guardian", "map"), ("The Pit", "cyberdyne", "map"),
        ("MLG Guardian 8", "MLG Guardian 8", "map variant"), ("Slayer", "Slayer", "game type"),
        ("Gun Game", "Gun Game", "game variant")]
    assert items[3].note == "map variant · Guardian"
    playlists = parse_playlists('playlists/slayer.playlist.json · "Halo 3 community rotation" · 5 games · vote 20 s · '
                                'played by LG Backend Test\nplaylists/odd.playlist.json · "Odd" · 2 games · vote 20 s\n'
                                '    a playlist needs 3 or more games\n')
    assert playlists[0].file == "playlists/slayer.playlist.json" and playlists[0].games == 5
    assert playlists[0].vote_seconds == 20 and playlists[0].played_by == "LG Backend Test"
    assert playlists[1].problem == "a playlist needs 3 or more games" and playlists[1].played_by == ""
    assert workshop_references("workshop:301/forge_halo (Forge Halo)\nworkshop:301/forge_halo again\nworkshop:7/x") == [
        "workshop:301/forge_halo", "workshop:7/x"]


def test_the_fake_program_inits_checks_and_lists(tmp_path: Path) -> None:
    tool = fake_tool()
    folder = tmp_path / "Slayer"
    assert tool.init(folder).ok and (folder / "dedicated.toml").is_file() and (folder / "bans.json").is_file()
    check = tool.check(folder)
    assert not check.ok and "missing files" in check.problem  # no game files without --from
    assert tool.init(folder, mcc="", maps=["Guardian", "Valhalla"]).ok
    assert sorted(p.stem for p in (folder / "game" / "halo3" / "maps").glob("*.map")) == ["guardian", "riverworld",
                                                                                           "shared"]
    check = tool.check(folder)  # the example playlist names The Pit and Last Resort, which aren't there
    assert not check.ok and "The Pit" in check.text
    assert tool.init(folder, mcc="").ok and tool.check(folder).ok
    assert (folder / "content" / "Game Modes" / "Project Reclaimer" / "gun-game.json").is_file()  # written by check
    names = {i.name for i in parse_content(tool.content(folder).text)}
    assert {"Guardian", "The Pit", "Slayer", "team_slayer", "Gun Game"} <= names
    narrowed = {i.name for i in parse_content(tool.content(folder, ["the", "pit"]).text)}
    assert "The Pit" in narrowed and "MLG Pit 8" in narrowed and "Epitaph" not in narrowed and "Guardian" not in narrowed
    shown = tool.playlist_show(folder, "slayer")
    assert shown.ok and "Every game is installed" in shown.text and "33%" in shown.text
    write_playlist(folder, "snipers", [{"map": name, "game": game} for name in ("Guardian", "The Pit")
                                       for game in ("Slayer", "rockets")], name="Snipers", vote_seconds=30)
    assert tool.playlist_show(folder, "playlists/snipers.playlist.json").ok  # one made by hand, as hosts do
    assert len(parse_playlists(tool.playlists(folder).text)) == 2
    assert "No mod maps" in tool.mods(folder).text


def test_tool_for_needs_the_program(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    tool = tool_for(load(settings_path))
    assert tool.prefix == [PYTHON, FAKE.as_posix()]  # the words before `dedicated` in [process] command
    (tmp_path / "reclaimer" / "SERVERS" / ALPHA / "project-reclaimer.exe").unlink()
    with pytest.raises(DedicatedError, match="isn't in any server folder"):
        tool_for(load(settings_path))
    (tmp_path / "reclaimer" / "project-reclaimer.exe").write_bytes(b"")  # one copy at the root serves every server
    assert tool_for(load(settings_path)).executable == tmp_path / "reclaimer" / "project-reclaimer.exe"


# Editing dedicated.toml in place ---------------------------------------------------------------

def test_editing_dedicated_toml_keeps_its_comments(tmp_path: Path) -> None:
    config = tmp_path / "dedicated.toml"
    config.write_text(TEMPLATE, encoding="utf-8")
    set_top_key(config, "game", "../../game")
    set_top_key(config, "host_name", "LG")
    set_table_key(config, "rcon", "password", "secretsecret")
    set_table_key(config, "defaults", "max_ping", 150)  # a commented-out setting comes to life
    set_table_key(config, "workshop", "items", [3014096841, 7], before="master")  # a table that wasn't there
    replace_server_blocks(config, [{"name": "Infection Night", "port": 39180, "playlist": "playlists/i.playlist.json",
                                    "hidden": True, "welcome": ["hi {name}"],
                                    "campaign": {"mission": 3010, "difficulty": "heroic"}}])
    data = read_toml(config)
    assert data["game"] == "../../game" and data["host_name"] == "LG" and data["content"] == "content"
    assert data["rcon"] == {"password": "secretsecret", "address": "127.0.0.1"}
    assert data["defaults"]["max_ping"] == 150 and data["defaults"]["max_players"] == 16
    assert data["workshop"] == {"items": [3014096841, 7]} and data["master"]["port"] == 49175
    assert server_blocks(config) == [{"name": "Infection Night", "port": 39180, "playlist": "playlists/i.playlist.json",
                                      "hidden": True, "welcome": ["hi {name}"],
                                      "campaign": {"mission": 3010, "difficulty": "heroic"}}]
    text = config.read_text(encoding="utf-8")
    assert "# Relative paths are relative to this file's folder." in text and "# Off while the password is empty" in text
    assert text.index("[workshop]") < text.index("[master]")
    set_server_setting(config, "Infection Night", "playlist", "playlists/other.playlist.json")  # the same code as T
    assert server_blocks(config)[0]["playlist"] == "playlists/other.playlist.json"


# New servers: the program's own `dedicated init`, then the quick actions ------------------------------------------

INIT_PORTS = {49175, 49176, 49177}  # init's master and two servers: taken on most hosts (simulation S4)


def test_names_ports_and_paths(tmp_path: Path) -> None:
    assert valid_name("Infection Night") is None and valid_name("Rat's Nest BTB") is None
    assert "ignores" in valid_name(".hidden") and "empty" in valid_name("  ")
    assert "can't have" in valid_name("a/b") and "at most" in valid_name("x" * 65)
    assert relative_posix(tmp_path / "reclaimer" / "game", tmp_path / "reclaimer" / "SERVERS" / "X") == "../../game"


def test_a_new_server_is_what_init_makes_and_nothing_more(tmp_path: Path) -> None:
    """Answer 6: make the folder, run the program's own `dedicated init` in it, and stop. No package, no shared
    folders, no console password, no auto_update, no SteamCMD lines, nothing of Huragok's."""
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    root = settings_path.parent
    said: list[str] = []
    outcome = create_server(settings, "LG Test 2", tool_for(settings), said.append)
    assert outcome.staging == root / "SERVERS" / ".new-lg-test-2" and outcome.folder == root / "SERVERS" / "LG Test 2"
    assert not outcome.folder.exists() and outcome.config == outcome.staging / "dedicated.toml"
    assert [s.name for s in load(settings_path).servers] == [ALPHA]  # a folder being made is never picked up
    init = tmp_path / "init"  # what `dedicated init` writes, by itself
    assert fake_tool().init(init).ok
    assert outcome.config.read_text(encoding="utf-8") == (init / "dedicated.toml").read_text(encoding="utf-8")
    made = {p.relative_to(outcome.staging).as_posix() for p in outcome.staging.rglob("*")}
    assert made - {"project-reclaimer.exe"} == {p.relative_to(init).as_posix() for p in init.rglob("*")}
    assert outcome.program == outcome.staging / "project-reclaimer.exe"  # Huragok's folder has none: linked in
    assert any("linked from" in line for line in said)
    assert outcome.servers == [("Reclaimer Slayer", 49176), ("Reclaimer Guardian", 49177)] and outcome.master == 49175
    assert outcome.ports == [49176, 49177, 49175] and outcome.shown == str(Path("SERVERS") / "LG Test 2" / "dedicated.toml")
    data = read_toml(outcome.config)
    assert data["rcon"]["password"] == "" and data["auto_update"] is False and "workshop" not in data
    assert data["game"] == "game" and data["content"] == "content" and data["bans"] == "bans.json"
    assert finish(outcome) == outcome.folder and not outcome.staging.exists()
    assert finish(outcome) == outcome.folder  # done already: nothing to do
    assert [s.name for s in load(settings_path).servers] == [ALPHA, "Reclaimer Slayer", "Reclaimer Guardian"]
    # With a program in Huragok's folder, a new server runs that one: nothing linked in.
    (root / "project-reclaimer.exe").write_bytes(b"")
    second = create_server(load(settings_path), "Second", tool_for(load(settings_path)), said.append)
    assert second.program is None and not (second.staging / "project-reclaimer.exe").exists()


def test_a_new_server_never_takes_a_name_that_is_there_and_leaves_nothing_when_init_fails(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    make_server_folder(root / "SERVERS" / "Bravo", "Bravo", free_port())
    set_server_setting(root / "SERVERS" / "Bravo" / "dedicated.toml", "Bravo", "enabled", False)
    (root / "SERVERS" / "Broken").mkdir()
    (root / "SERVERS" / "Broken" / "dedicated.toml").write_text("[[server]\nname = ", encoding="utf-8")
    (root / "SERVERS" / "Taken").mkdir()
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == [ALPHA]  # Bravo is off, Broken left out
    for name, why in (("bravo", "exists already"), ("a/b", "folder name"), ("Taken", "exists already"),
                      ("Alpha-Server", "would share their records and logs")):
        with pytest.raises(SetupError, match=why):
            create_server(settings, name, fake_tool(), lambda _: None)
    # A folder left half made by a window closed part way is nobody's: made again from scratch.
    (root / "SERVERS" / ".new-fresh" / "left-over").mkdir(parents=True)
    fresh = create_server(settings, "Fresh", fake_tool(), lambda _: None)
    assert not (fresh.staging / "left-over").exists() and fresh.config.is_file()
    # The program's own init refusing, or the drive: nothing left behind.
    tool = fake_tool()
    tool.init = lambda folder, **more: ToolResult(False, 2, "Error: the drive is full")  # type: ignore[method-assign]
    with pytest.raises(SetupError, match="dedicated init failed: Error: the drive is full"):
        create_server(settings, "Full", tool, lambda _: None)

    def denied(folder: Path, **more: Any) -> ToolResult:
        folder.mkdir(parents=True)
        raise PermissionError(13, "Access is denied", str(folder))

    tool.init = denied  # type: ignore[method-assign]
    with pytest.raises(SetupError, match="couldn't make Denied: .*Access is denied.*nothing was left half-made"):
        create_server(settings, "Denied", tool, lambda _: None)
    assert sorted(p.name for p in (root / "SERVERS").iterdir()) == [".new-fresh", ALPHA, "Bravo", "Broken", "Taken"]


def test_the_quick_actions_after_n_make_one_server_with_free_ports_and_its_console(tmp_path: Path,
                                                                                    monkeypatch) -> None:
    """Answer 33, on the folder init made: each worked out on a copy and shown (the file untouched), then written as
    shown, a version of the file kept first. Ports free on this PC and not another server's."""
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    alpha_port = settings.servers[0].game_port
    monkeypatch.setattr(quickactions, "ports_in_use", lambda: set(INIT_PORTS))  # the live servers' ports, here too
    outcome = create_server(settings, "LG Test 2", fake_tool(), lambda _: None)
    init_text = outcome.config.read_text(encoding="utf-8")
    assert quickactions.taken_ports(settings, outcome.config, outcome.ports) == [
        (49176, ""), (49177, ""), (49175, f"{ALPHA}'s master")]
    where, owner = outcome.shown, "lg-test-2"
    one = quickactions.one_server(outcome.config, "LG Test 2", where=where, owner=owner)
    assert [(line.sign, line.text) for line in one.lines()] == [
        ("~", 'name = "LG Test 2"'), ("-", "[[server]]"), ("-", 'name = "Reclaimer Guardian"'), ("-", "port = 49177"),
        ("-", 'map = "Guardian"'), ("-", 'game = "Slayer"')]
    assert one.lines()[0].note == 'was "Reclaimer Slayer"' and one.ask == "Keep one server?"
    assert outcome.config.read_text(encoding="utf-8") == init_text  # shown, not written
    preview = "\n".join(text for text, _ in one.preview())
    assert f"This writes 6 line(s) in {where}:" in preview and "A copy of the file is kept first" in preview
    quickactions.write(settings, one)
    assert read_folder(outcome.config) == ([("LG Test 2", 49176)], 49175)
    ports = quickactions.free_ports(settings, outcome.config, master="off", where=where, owner=owner)
    lines = {line.text: line for line in ports.lines()}
    assert lines["enabled = false"].sign == "~" and "public masters still list its servers" in lines["enabled = false"].note
    [port_line] = [line for text, line in lines.items() if text.startswith("port = ")]
    new_port = int(port_line.text.split(" = ")[1])
    assert new_port not in INIT_PORTS | {alpha_port} and new_port > 49176
    assert "free on this PC: LG Test 2's game port" in port_line.note and "49176 was in use on this PC" in port_line.note
    quickactions.write(settings, ports)
    console = quickactions.add_console(settings, outcome.config, where=where, owner=owner)
    [line] = console.lines()
    assert line.sign == "~" and line.text.startswith('password = "') and line.text.endswith('…"')
    assert "random, 24 characters, shown in full in the file" in line.note and console.ask == "Add it?"
    assert f"LG Test 2's on TCP {new_port} (its game port's number)" in "\n".join(console.why)
    quickactions.write(settings, console)
    data = read_toml(outcome.config)
    assert len(data["rcon"]["password"]) == 24 and data["master"] == {"enabled": False, "port": 49175, "peers": [],
                                                                        "origin": ""}
    assert data["server"] == [{"name": "LG Test 2", "port": new_port, "playlist": "playlists/slayer.playlist.json"}]
    for again in (quickactions.one_server(outcome.config, "LG Test 2"), quickactions.free_ports(settings, outcome.config),
                  quickactions.add_console(settings, outcome.config)):
        assert not again.needed and again.preview()[0][0] == again.nothing
    kept = versions.versions(settings.records, "settings", owner, "dedicated.toml")
    assert [v.how for v in kept] == ["quick action: Add the console", "quick action: Free ports",
                                     "quick action: One server only", versions.FIRST]
    assert versions.read(kept[-1]) == init_text  # init's own, to put back
    folder = finish(outcome)
    assert quickactions.history(settings, folder / "dedicated.toml") == kept
    server = load(settings_path).server("LG Test 2")
    assert server.rcon_password == data["rcon"]["password"] and server.game_port == new_port and not server.warnings


def test_a_quick_action_is_written_as_shown_or_not_at_all(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    config = settings.servers[0].config_path
    set_table_key(config, "rcon", "password", "")
    action = quickactions.add_console(settings, config, ALPHA, in_use=set())
    assert action.needed and quickactions.write(settings, quickactions.one_server(config, ALPHA)) is None  # nothing
    config.write_text(config.read_text(encoding="utf-8") + "# a line written by hand meanwhile\n", encoding="utf-8")
    with pytest.raises(quickactions.QuickActionError, match="changed since this was shown: nothing was written"):
        quickactions.write(settings, action)
    assert read_toml(config)["rcon"]["password"] == ""
    replace_server_blocks(config, [{"name": "Twin", "port": free_port()}, {"name": "twin", "port": free_port()}])
    with pytest.raises(quickactions.QuickActionError, match="have the same name"):
        quickactions.one_server(config, "Twin")
    # A form tomledit won't guess at: refused, the file as it was.
    inline = tmp_path / "Inline" / "dedicated.toml"
    inline.parent.mkdir()
    written = 'rcon = { password = "" }\n\n[[server]]\nname = "Inline"\nport = 5000\n'
    inline.write_text(written, encoding="utf-8")
    with pytest.raises(quickactions.QuickActionError, match=r"couldn't change \[rcon\] password safely"):
        quickactions.add_console(settings, inline, in_use=set())
    assert inline.read_text(encoding="utf-8") == written


def test_the_console_for_one_server_of_several_and_a_port_of_its_own(tmp_path: Path) -> None:
    """A folder whose servers have passwords of their own: the console goes in the server's own block
    (rcon_password); its TCP port, its game port's number, taken on this PC: an rcon_port of its own."""
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    config = settings.servers[0].config_path
    first, second = free_ports(2)
    set_table_key(config, "rcon", "password", "")
    replace_server_blocks(config, [{"name": ALPHA, "port": first, "rcon_password": "theirs-theirs"},
                                   {"name": "Second", "port": second}])
    action = quickactions.add_console(settings, config, "Second", in_use={second}, password="p" * 24)
    texts = {line.text: line for line in action.lines()}
    [port_text] = [text for text in texts if text.startswith("rcon_port = ")]
    rcon_port = int(port_text.split(" = ")[1])
    assert set(texts) == {'rcon_password = "pppp…"', port_text} and rcon_port not in (first, second)
    assert "its game port's number is taken" in texts[port_text].note
    assert action.ask == "Add them?"
    quickactions.write(settings, action)
    blocks = server_blocks(config)
    assert blocks[1] == {"name": "Second", "port": second, "rcon_password": "p" * 24, "rcon_port": rcon_port}
    assert blocks[0]["rcon_password"] == "theirs-theirs" and read_toml(config)["rcon"]["password"] == ""
    assert not quickactions.add_console(settings, config, ALPHA).needed  # it has its own already
    with pytest.raises(quickactions.QuickActionError, match="no \\[\\[server\\]\\] block named Nobody"):
        quickactions.add_console(settings, config, "Nobody")


def test_free_means_free_on_this_pc_and_no_other_servers(tmp_path: Path, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    bravo, other_master = free_ports(2)
    make_server_folder(root / "SERVERS" / "Bravo", "Bravo", bravo)
    set_server_setting(root / "SERVERS" / "Bravo" / "dedicated.toml", "Bravo", "enabled", False)  # off: still its
    set_table_key(root / "SERVERS" / "Bravo" / "dedicated.toml", "master", "port", other_master)
    settings = load(settings_path)
    alpha = settings.servers[0]
    known = quickactions.known_ports(settings)
    assert known[bravo] == "Bravo" and known[alpha.game_port] == ALPHA and known[other_master] == "Bravo's master"
    assert alpha.game_port not in quickactions.known_ports(settings, skip=alpha.config_path)  # its own don't count
    ports = quickactions.look_at_ports(settings, alpha.config_path, in_use={5000})
    assert ports.taken(bravo) == "used by Bravo" and ports.taken(5000) == "in use on this PC" and ports.taken(5001) is None
    picked = ports.free(5000)
    assert picked == 5001 and ports.taken(5001) == "given to another server here"
    # The system's own table can't be read: each port is probed (never one of the real servers', in the tests)
    assert quickactions.Ports({}, None).taken(49176) == "in use on this PC"
    monkeypatch.setattr(quickactions, "ports_in_use", lambda: None)
    assert quickactions.look_at_ports(settings, alpha.config_path).in_use is None
    # SteamCMD lines need SteamCMD here
    with pytest.raises(quickactions.QuickActionError, match="SteamCMD isn't set up on this PC"):
        quickactions.steamcmd_lines(settings, alpha.config_path)


# The commands -----------------------------------------------------------------------------------

def test_the_setup_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    firstrun.write_files(root)  # the first start: no PACKAGES/ since 0.7.0
    assert not (root / "PACKAGES").exists()
    monkeypatch.setattr(quickactions, "ports_in_use", lambda: set(INIT_PORTS))
    # Its quick actions asked for, with nothing here to answer yes: each shown, nothing written.
    assert cli.main(["-c", str(settings_path), "new", "Gamma", "--console"]) == 1
    out = capsys.readouterr().out
    assert "Add the console" in out and "Not written: nothing here can answer yes" in out and "Made" in out
    assert read_toml(root / "SERVERS" / "Gamma" / "dedicated.toml")["rcon"]["password"] == ""
    assert cli.main(["-c", str(settings_path), "new", "Beta", "--all", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Made" in out and "49176, 49177 in use on this PC" in out and "--free-ports" in out
    beta = read_toml(root / "SERVERS" / "Beta" / "dedicated.toml")
    [block] = beta["server"]
    assert block["name"] == "Beta" and block["port"] not in INIT_PORTS and beta["master"]["enabled"] is False
    assert len(beta["rcon"]["password"]) == 24 and "workshop" not in beta
    assert cli.main(["-c", str(settings_path), "new", "Beta"]) == 1
    assert "exists already" in capsys.readouterr().err
    assert cli.main(["-c", str(settings_path), "new", "Delta", "--package", "team-slayer"]) == 2
    assert "--package is gone" in capsys.readouterr().err and not (root / "SERVERS" / "Delta").exists()
    # Its game files: `repair` points it at Alpha's, which has them (shown first; --yes says yes).
    assert cli.main(["-c", str(settings_path), "repair", "Beta", "--yes"]) == 0
    assert "has no Halo 3 files here: it uses" in capsys.readouterr().out
    assert read_toml(root / "SERVERS" / "Beta" / "dedicated.toml")["game"] == f"../{ALPHA}/game"

    assert cli.main(["-c", str(settings_path), "content", "-s", "Beta", "pit"]) == 0
    assert "The Pit" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta"]) == 0
    assert "slayer.playlist.json" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "show", "slayer"]) == 0
    assert "Every game is installed" in capsys.readouterr().out
    before = (root / "SERVERS" / "Beta" / "dedicated.toml").read_text(encoding="utf-8")
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "new", "Snipers"]) == 2  # gone (0.7.0)
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "use", "slayer"]) == 2
    assert capsys.readouterr().err.count("is gone: Huragok no longer makes playlists") == 2
    assert (root / "SERVERS" / "Beta" / "dedicated.toml").read_text(encoding="utf-8") == before

    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta"]) == 0
    assert "no Workshop items" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta", "add", "3014096841"]) == 2  # 0.7.0: the host's
    assert "write them in [workshop] items" in capsys.readouterr().err
    assert "items" not in read_toml(root / "SERVERS" / "Beta" / "dedicated.toml").get("workshop", {})
    set_table_key(root / "SERVERS" / "Beta" / "dedicated.toml", "workshop", "items", [3014096841], before="master")
    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta", "install"]) == 0
    assert "workshop:3014096841/fake3014096841" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "dedicated", "-s", "Beta", "check"]) == 0
    assert "Everything is ready" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "dedicated", "-s", "Beta"]) == 2
    assert cli.main(["-c", str(settings_path), "package", "list"]) == 2  # gone (0.7.0): says where it went
    assert "package is gone" in capsys.readouterr().err
    code = cli.main(["-c", str(settings_path), "check"])  # the supervisor's own check
    out = capsys.readouterr().out
    assert "Beta" in out and "lacks Halo 3's files" in out and "repair" in out, out  # Gamma's: init made no game files
    assert code == 0, out


# The window -------------------------------------------------------------------------------------


def test_n_makes_a_bare_server_and_offers_its_quick_actions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             run) -> None:
    """Mockup 6: init, then what it holds and which ports are taken, then the quick actions (all three here), each
    shown before it's written and asked; Esc when done gives the folder its name."""
    settings_path = setup_root(tmp_path)
    monkeypatch.setattr(quickactions, "ports_in_use", lambda: set(INIT_PORTS))
    controller = SetupController(settings_path)
    keys = [*"LG Test 2", "ENTER",
            "A", "Y",  # all three: one server only (yes),
            "O", "Y",  # free ports: the master off (yes),
            "Y",  # the console (yes)
            "ESC", "x"]  # done
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_new())
    printed = "".join(terminal.output)
    folder = settings_path.parent / "SERVERS" / "LG Test 2"
    assert f"Made {Path('SERVERS') / 'LG Test 2'} with the program's own `dedicated init`" in printed, printed
    assert "It holds two servers, on ports 49176 and 49177, with its master on 49175." in printed
    assert "! 49176 and 49177 are in use on this PC: it won't start until that changes." in printed
    assert "Its master's port 49175 is in use too" in printed
    assert "Esc leaves the folder exactly as init made it" in printed and "Esc leaves the folder as it is now" in printed
    assert "Keep one server? (y/N)" in printed and "Change them? (y/N)" in printed and "Add it? (y/N)" in printed
    assert "It holds one server, on port" in printed and "with its master off." in printed
    data = read_toml(folder / "dedicated.toml")
    [block] = data["server"]
    assert block["name"] == "LG Test 2" and block["port"] not in INIT_PORTS and len(data["rcon"]["password"]) == 24
    assert data["master"]["enabled"] is False and controller.discovered == 1 and controller.calls == []
    assert f"{folder} is ready" in printed and not (settings_path.parent / "SERVERS" / ".new-lg-test-2").exists()


def test_n_then_esc_leaves_the_folder_exactly_as_init_made_it(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    menu = Menu(controller, FakeTerminal(), StrictKeyboard([*"Plain", "ENTER", "1", "N", "2", "ESC", "ESC", "x"]),
                RecentLog())  # type: ignore[arg-type]
    run(menu.screen_new())  # one server only: no; free ports: Esc at the master; then done
    init = tmp_path / "init"
    assert fake_tool().init(init).ok
    folder = settings_path.parent / "SERVERS" / "Plain"
    assert (folder / "dedicated.toml").read_text(encoding="utf-8") == (init / "dedicated.toml").read_text(encoding="utf-8")
    assert not (settings_path.parent / "records" / "settings").exists()  # nothing written: no version kept


def test_d_adds_the_console_to_a_server_when_the_host_says_yes(tmp_path: Path, run) -> None:
    """D → a server → Q → Add the console (mockup 5): the D screen calls screen_add_console."""
    settings_path = setup_root(tmp_path)
    config = load(settings_path).servers[0].config_path
    set_table_key(config, "rcon", "password", "")
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["N"]), RecentLog())  # type: ignore[arg-type]
    assert run(menu.screen_add_console(server)) is False
    printed = "".join(terminal.output)
    assert "Alpha Server › Quick action › Add the console" in printed
    assert "Without its console Huragok can't see players, warn them or moderate." in printed
    assert f"This writes 1 line(s) in {Path('SERVERS') / ALPHA / 'dedicated.toml'}:" in printed
    assert "random, 24 characters, shown in full in the file" in printed and "Add it? (y/N)" in printed
    assert "Nothing was written." in printed and read_toml(config)["rcon"]["password"] == ""
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["ESC"]), RecentLog())  # type: ignore[arg-type]
    assert run(menu.screen_add_console(server)) is None and read_toml(config)["rcon"]["password"] == ""
    menu = Menu(controller, FakeTerminal(), StrictKeyboard(["Y"]), RecentLog())  # type: ignore[arg-type]
    assert run(menu.screen_add_console(server)) is True and len(read_toml(config)["rcon"]["password"]) == 24
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard([]), RecentLog())  # type: ignore[arg-type]
    assert run(menu.screen_add_console(server)) is False and "It has its console already" in "".join(terminal.output)


def test_i_writes_one_servers_steamcmd_lines_when_asked(tmp_path: Path, run, monkeypatch) -> None:
    from huragok import steam as steamsetup

    settings_path = setup_root(tmp_path)
    program = settings_path.parent / "steamcmd" / steamsetup.executable_name()
    program.parent.mkdir()
    program.write_bytes(b"")
    set_table_key(settings_path, "setup", "steam_username", "lilgremlin", before="process")
    controller = SetupController(settings_path)
    config = controller.settings.servers[0].config_path
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["L", "1", "ENTER", "Y", "x", "ESC"]), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_steam())
    printed = "".join(terminal.output)
    assert "SteamCMD lines" in printed and "the account I signed in" in printed and "Write them? (y/N)" in printed
    assert read_toml(config)["workshop"] == {"steamcmd": str(program), "username": "lilgremlin"}


def test_dot_folders_are_ignored_and_plays_is_read(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    make_server_folder(root / "SERVERS" / ".new-x", "Half Made", free_port())
    make_server_folder(root / "SERVERS" / "_aside", "Set Aside", free_port())
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == [ALPHA]
    assert settings.servers[0].plays == "playlist slayer"
    config = root / "SERVERS" / ALPHA / "dedicated.toml"
    replace_server_blocks(config, [{"name": ALPHA, "port": free_port(), "map": "Guardian", "game": "rockets"}])
    assert load(settings_path).servers[0].plays == "rockets on Guardian"
    assert load(settings_path).setup.mcc_folder is None
    # share does nothing since 0.7.0 (read past, whatever it says): named, never an error.
    write_settings(root, None, 'backend = "attach"\n[setup]\nshare = ["maps"]\nmcc_folder = ""\n', staff())
    assert load(settings_path).setup.mcc_folder == ""
    assert any("[setup] share does nothing any more" in line for line in load(settings_path).retired)


# Names the program refuses ------------------------------------------------------------------------

def test_names_the_server_program_refuses_are_refused(tmp_path: Path) -> None:
    assert valid_name("A" * 48) is None and valid_name("Café Ünicode 😀") is None
    assert "at most 48" in valid_name("A" * 49)
    for bad in ("DEL\x7fNAME", "a\u2028b", "a\x85b", "zero\u200bwidth", "tab\tname", "nul\x00"):
        assert "invisible" in valid_name(bad), repr(bad)
    assert "Windows keeps" in valid_name("CON") and "Windows keeps" in valid_name("nul.txt")


# Renaming --------------------------------------------------------------------------------------------

def _rename_root(tmp_path: Path) -> tuple[Path, Any]:
    settings_path = setup_root(tmp_path)
    set_server_setting(settings_path, ALPHA, "autostart", False)
    settings = load(settings_path)
    server = settings.servers[0]
    server.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.records / "servers").mkdir(parents=True, exist_ok=True)
    (settings.records / "servers" / f"{server.slug}.json").write_text("{}", encoding="utf-8")
    return settings_path, settings


def _names(settings_path: Path, folder: Path) -> tuple[list[str], list[str]]:
    return ([b["name"] for b in server_blocks(folder / "dedicated.toml")],
            [b["name"] for b in server_blocks(settings_path)])


def test_a_rename_a_locked_folder_stops_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from huragok.content.serverops import ServerOpError, rename_server

    settings_path, settings = _rename_root(tmp_path)
    server = settings.servers[0]
    folder, before = server.folder, (server.config_path.read_bytes(), settings_path.read_bytes())
    real = os.replace

    def locked(source: Any, target: Any) -> None:
        if Path(source) == folder:
            raise PermissionError(5, "Access is denied", str(source))
        real(source, target)

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(ServerOpError, match="couldn't be renamed \\(Access is denied\\).*Nothing was changed"):
        rename_server(settings, server, "Charlie", lambda _: None, phase="idle")
    assert (server.config_path.read_bytes(), settings_path.read_bytes()) == before and server.data_dir.is_dir()
    monkeypatch.undo()
    from dataclasses import replace

    settings.servers.append(replace(server, name="Bravo X", slug="bravo-x"))  # a name another server's files
    with pytest.raises(ServerOpError, match="would share their records and logs"):  # would answer to is refused
        rename_server(settings, server, "BRAVO-X!", lambda _: None, phase="idle")
    settings.servers.pop()
    renamed = rename_server(settings, server, "Charlie", lambda _: None, phase="idle")  # the retry works
    assert renamed.name == "Charlie" and not folder.exists()
    assert _names(settings_path, renamed) == (["Charlie"], ["Charlie"])
    assert (renamed / "data" / "charlie").is_dir() and (settings.records / "servers" / "charlie.json").is_file()


def test_a_rename_that_fails_later_puts_every_step_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from huragok.content.serverops import ServerOpError, rename_server

    settings_path, settings = _rename_root(tmp_path)
    server = settings.servers[0]
    folder, record = server.folder, settings.records / "servers" / f"{server.slug}.json"
    real = os.replace

    def record_locked(source: Any, target: Any) -> None:
        if Path(source) == record:
            raise PermissionError(13, "The process cannot access the file", str(source))
        real(source, target)

    monkeypatch.setattr(os, "replace", record_locked)
    with pytest.raises(ServerOpError, match="Every step was put back: it's still Alpha Server") as raised:
        rename_server(settings, server, "Charlie", lambda _: None, phase="idle")
    assert not raised.value.partly
    assert folder.is_dir() and not folder.with_name("Charlie").exists() and server.data_dir.is_dir()
    assert _names(settings_path, folder) == ([ALPHA], [ALPHA]) and record.is_file()

    def folder_stuck(source: Any, target: Any) -> None:  # the record fails, and so does moving the folder back
        if Path(source) == record or Path(target) == folder:
            raise PermissionError(13, "Access is denied", str(source))
        real(source, target)

    monkeypatch.setattr(os, "replace", folder_stuck)
    with pytest.raises(ServerOpError, match="partly renamed: putting back failed for the folder SERVERS/Charlie") \
            as raised:
        rename_server(settings, server, "Charlie", lambda _: None, phase="idle")
    assert raised.value.partly


def test_a_server_about_to_start_or_renamed_by_hand_is_not_renamed(tmp_path: Path) -> None:
    from huragok.content.serverops import ServerOpError, rename_server

    settings_path, settings = _rename_root(tmp_path)
    server = settings.servers[0]
    for phase, said in (("backoff", "about to restart it"), ("booting", "starting"), ("preparing", "starting"),
                        ("running", "running"), ("stopping", "stopping")):
        with pytest.raises(ServerOpError, match=f"{said}.*stop it first \\(X\\)"):
            rename_server(settings, server, "Charlie", lambda _: None, phase=phase)
    with pytest.raises(ServerOpError, match="its program is still running"):
        rename_server(settings, server, "Charlie", lambda _: None, phase="idle", pid=4242)
    set_server_setting(server.config_path, ALPHA, "name", "Zulu")  # the host renamed it in the file meanwhile
    before = server.config_path.read_bytes()
    with pytest.raises(ServerOpError, match="has no \\[\\[server\\]\\] block named Alpha Server now"):
        rename_server(settings, server, "Charlie", lambda _: None, phase="idle")
    assert server.config_path.read_bytes() == before and server.folder.is_dir()


def test_the_rename_screen_reads_the_servers_phase_as_it_is_now(tmp_path: Path, run) -> None:
    settings_path, _ = _rename_root(tmp_path)

    class BackingOff(SetupController):
        def rows(self) -> list[dict[str, Any]]:
            return [{**row, "phase": "backoff"} for row in super().rows()]

    controller = BackingOff(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["1", "ENTER", "N", "ENTER", "x", "ESC", "ESC"]), RecentLog())
    run(menu.screen_settings())
    assert "stop it first (X)" in "".join(terminal.output)
    assert (settings_path.parent / "SERVERS" / ALPHA).is_dir()


def test_a_server_sharing_its_folder_with_a_running_one_is_not_renamed(tmp_path: Path, run) -> None:
    """One dedicated.toml can have several servers: renaming moves the folder under all of them, and own content
    changes `content` for them all, but only the one picked was checked (audit 2026-11, 7.5)."""
    settings_path, _ = _rename_root(tmp_path)
    config = settings_path.parent / "SERVERS" / ALPHA / "dedicated.toml"
    blocks = server_blocks(config)
    replace_server_blocks(config, [*blocks, {"name": "Second Server", "port": free_port()}])
    assert len(load(settings_path).servers) == 2

    class OneRunning(SetupController):
        def rows(self) -> list[dict[str, Any]]:
            return [{**row, "phase": "running" if row["name"] == "Second Server" else "idle"} for row in super().rows()]

    controller = OneRunning(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["1", "ENTER", "N", "ENTER", "x", "ESC", "ESC"]), RecentLog())
    run(menu.screen_settings())
    text = "".join(terminal.output)
    assert "Second Server run from it: stop them first" in text
    assert config.is_file() and [b["name"] for b in server_blocks(config)] == [ALPHA, "Second Server"]


def test_a_rename_takes_everything_under_its_name_along(tmp_path: Path) -> None:
    """Its console log (whose name doesn't start with the slug), its FAULTED record, its playlist history and a
    Workshop update's marker for copies set aside were left under the old name (audit 2026-11, 7.6)."""
    import json

    from huragok.content.serverops import rename_server
    from huragok.updates.workshop import MARKER, aside_folders

    settings_path, settings = _rename_root(tmp_path)
    server = settings.servers[0]
    old = server.slug
    for path in (settings.console_log(old), settings.faulted_file(old), settings.records / "playlists" / old / "x" / "1.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    server.content_dir.mkdir(parents=True, exist_ok=True)
    aside = aside_folders(server.content_dir)
    aside.mkdir(parents=True, exist_ok=True)
    live, kept = server.content_dir / "Mods" / "Workshop" / "42", aside / f"{old}-42-20261102-010101"
    (aside / f"{old}-20261102-010101{MARKER}").write_text(json.dumps({"server": server.name, "moves": [[str(live), str(kept)]]}), encoding="utf-8")
    rename_server(settings, server, "Gamma Server", say=lambda _text: None, phase="idle")
    renamed = load(settings_path)
    new = next(s for s in renamed.servers if s.name == "Gamma Server")
    assert new.slug != old
    for path in (renamed.console_log(new.slug), renamed.faulted_file(new.slug), renamed.records / "playlists" / new.slug / "x" / "1.json"):
        assert path.is_file(), path
    assert not renamed.console_log(old).exists() and not renamed.faulted_file(old).exists()
    markers = list(aside_folders(new.content_dir).glob(f"*{MARKER}"))
    assert [m.name for m in markers] == [f"{new.slug}-20261102-010101{MARKER}"]
    moves = json.loads(markers[0].read_text(encoding="utf-8"))["moves"]
    folder_moved = new.folder != server.folder
    assert moves == [[str(new.content_dir / "Mods" / "Workshop" / "42") if folder_moved and server.content_dir.is_relative_to(server.folder) else str(live),
                      str(aside_folders(new.content_dir) / f"{old}-42-20261102-010101") if folder_moved and aside.is_relative_to(server.folder) else str(kept)]]
