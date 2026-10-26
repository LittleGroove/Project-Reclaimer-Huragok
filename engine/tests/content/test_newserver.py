"""Setting servers up: the `dedicated` runner, packages, new server folders, the window's N/G/C/E screens and
the commands, all against the fake `dedicated` (Windows and Linux)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, staff, write_settings
from testkit.fake_dedicated import TEMPLATE
from testkit.setup import (
    ALPHA, FAKE, PYTHON, SetupController, StrictKeyboard, fake_tool, make_server_folder, setup_root,
)
from testkit.window import FakeTerminal

from huragok.commandline import cli, firstrun
from huragok.content.newserver import NewServer, SetupError, create_server, next_free_port, relative_posix, valid_name
from huragok.content.packages import (
    PackageError, apply_package, install_starters, list_packages, load_package, save_package,
)
from huragok.core import desktop
from huragok.core.logs import RecentLog
from huragok.core.settings import load
from huragok.core.tomledit import (
    read_toml, replace_server_blocks, server_blocks, set_server_setting, set_table_key, set_top_key,
)
from huragok.program.dedicated import DedicatedError, parse_content, parse_playlists, tool_for, workshop_references
from huragok.window.dashboard import Menu


def starters(tmp_path: Path) -> Path:
    folder = tmp_path / "PACKAGES"
    folder.mkdir(exist_ok=True)
    install_starters(folder)
    return folder


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
    made = tool.playlist_new(folder, "Snipers", ["Guardian", "The Pit"], ["Slayer", "rockets"], vote_seconds=30)
    assert made.ok and (folder / "playlists" / "snipers.playlist.json").is_file()
    assert "exists" in tool.playlist_new(folder, "Snipers", ["Guardian"], ["Slayer"]).problem
    assert tool.playlist_new(folder, "Snipers", ["Guardian", "Narrows"], ["Slayer", "rockets"], replace=True).ok
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


# Packages ---------------------------------------------------------------------------------------

def test_starter_packages_load(tmp_path: Path) -> None:
    folder = starters(tmp_path)
    packages = list_packages(folder)
    assert {p.key for p in packages} == {"team-slayer", "big-team-battle", "mlg-hardcore", "infection",
                                         "scripted-modes", "campaign-coop", "odst-firefight"}
    infection = load_package(folder / "infection")
    assert infection.plays == "playlist infection" and infection.server["max_players"] == 16
    assert [p.name for p in infection.playlists] == ["infection.playlist.json"] and not infection.workshop_items
    coop = load_package(folder / "campaign-coop")
    assert coop.needs_campaign and coop.plays.startswith("campaign co-op") and coop.server["campaign"]["mission"] == 3010
    odst = load_package(folder / "odst-firefight")
    assert odst.needs_odst and odst.plays == "ODST Firefight Crater (Night), Ff Classic (heroic)"
    assert odst.server["max_players"] == 4 and "needs Halo 3: ODST's files" in odst.summary()
    assert install_starters(folder) == []  # a second time copies nothing: the host may have edited them
    (folder / "broken").mkdir()
    (folder / "broken" / "package.toml").write_text('name = "x"\n[server]\nport = 5\n', encoding="utf-8")
    broken = next(p for p in list_packages(folder) if p.key == "broken")
    assert broken.name.endswith("(broken)") and "chosen when the server is made" in broken.description
    # A shared package can't hand its author the new server's admin list or console through [defaults] either.
    for key, value in (("admins", '["' + "a" * 64 + '"]'), ("rcon_password", '"theirs"')):
        (folder / "shared").mkdir(exist_ok=True)
        (folder / "shared" / "package.toml").write_text(f'name = "x"\n[defaults]\n{key} = {value}\n', encoding="utf-8")
        with pytest.raises(PackageError, match=rf"\[defaults\] can't set '{key}'"):
            load_package(folder / "shared")
    with pytest.raises(PackageError, match="isn't in the package"):
        (folder / "nofile").mkdir()
        (folder / "nofile" / "package.toml").write_text('[server]\nplaylist = "playlists/x.json"\n', encoding="utf-8")
        load_package(folder / "nofile")


def test_saving_a_server_as_a_package(tmp_path: Path) -> None:
    folder = tmp_path / "Slayer"
    make_server_folder(folder, "Slayer", free_port())
    playlist = {"version": 2, "name": "Mixed", "games": [
        {"map": "Maps/my forge map.json", "base_map": "workshop:3014096841/forge_halo", "game": "Game Modes/snipers.bin",
         "preview_image": "snipers.png"},
        {"map": "Guardian", "game": "Slayer"}, {"map": "workshop:55/x", "game": "Slayer"}]}
    (folder / "playlists" / "mixed.playlist.json").write_text(json.dumps(playlist), encoding="utf-8")
    for relative in ("Maps/my forge map.json", "Game Modes/snipers.bin", "Images/snipers.png"):
        (folder / "content" / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / "content" / relative).write_bytes(b"x")
    config = folder / "dedicated.toml"
    replace_server_blocks(config, [{"name": "Slayer", "port": free_port(), "playlist": "playlists/mixed.playlist.json",
                                    "max_players": 12, "password": "secret", "admins": ["a" * 64], "voice": False}])
    package = save_package(folder, "Slayer", tmp_path / "PACKAGES" / "mixed", name="Mixed", description="Saved")
    assert package.server == {"playlist": "playlists/mixed.playlist.json", "max_players": 12, "voice": False}
    assert package.workshop_items == [3014096841, 55]  # from the playlist's references
    assert {p.relative_to(package.folder / "content").as_posix() for p in package.content_files} == {
        "Maps/my forge map.json", "Game Modes/snipers.bin", "Images/snipers.png"}
    with pytest.raises(PackageError, match="already exists"):
        save_package(folder, "Slayer", tmp_path / "PACKAGES" / "mixed")
    target = tmp_path / "Other"
    (target / "playlists").mkdir(parents=True)
    copied = apply_package(package, target, target / "content")
    assert "playlists/mixed.playlist.json" in copied and (target / "content" / "Images" / "snipers.png").is_file()


# New servers ------------------------------------------------------------------------------------

def test_names_ports_and_paths(tmp_path: Path) -> None:
    assert valid_name("Infection Night") is None and valid_name("Rat's Nest BTB") is None
    assert "ignores" in valid_name(".hidden") and "empty" in valid_name("  ")
    assert "can't have" in valid_name("a/b") and "at most" in valid_name("x" * 65)
    settings = load(setup_root(tmp_path))
    port = next_free_port(settings)
    assert port >= 49176 and port not in {settings.servers[0].game_port, 49175}
    assert relative_posix(tmp_path / "reclaimer" / "game", tmp_path / "reclaimer" / "SERVERS" / "X") == "../../game"


def test_a_new_server_shares_the_roots_folders(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    package = load_package(starters(tmp_path) / "infection")
    port = free_port()
    said: list[str] = []
    tool = tool_for(settings)  # Alpha's project-reclaimer.exe, run as the fake through [process] command
    outcome = create_server(settings, NewServer("Infection Night", port=port, package=package, hidden=True,
                                                password="letmein", welcome=["hi {name}"]), tool, said.append)
    root = tmp_path / "reclaimer"
    assert outcome.ready and outcome.folder == root / "SERVERS" / "Infection Night", outcome.check and outcome.check.text
    assert not [p for p in (root / "SERVERS").iterdir() if p.name.startswith(".new")]
    assert (root / "game" / "halo3" / "halo3.dll").is_file()  # linked from Alpha, which had them
    assert (root / "content" / "Game Modes").is_dir()
    data = read_toml(outcome.folder / "dedicated.toml")
    assert data["game"] == "../../game" and data["content"] == "../../content"
    # Never the ban list: every server keeps its own (a /ban there is that server's), even when share says bans.
    assert data.get("bans", "bans.json") == "bans.json" and not (root / "bans.json").exists()
    assert not (outcome.folder / "game").exists() and not (outcome.folder / "content").exists()
    assert len(data["rcon"]["password"]) == 32 and data["rcon"]["password"] == outcome.rcon_password
    assert data["host_name"] == "Dedicated server"  # copied from Alpha's folder
    assert data["auto_update"] is True  # new servers install Project Reclaimer releases themselves ([setup])
    assert data["server"] == [{"name": "Infection Night", "port": port, "playlist": "playlists/infection.playlist.json",
                               "max_players": 16, "hidden": True, "password": "letmein", "welcome": ["hi {name}"]}]
    assert (outcome.folder / "playlists" / "infection.playlist.json").is_file()
    assert (outcome.folder / "project-reclaimer.exe").is_file()  # the program, linked in: the root has none
    assert outcome.check is not None and ".new-" not in outcome.check.text
    assert any("sharing Halo 3's files" in s for s in said) and any("checking" in s for s in said)
    fresh = load(settings_path)
    assert [s.name for s in fresh.servers] == [ALPHA, "Infection Night"]
    new = fresh.server("Infection Night")
    assert new.game_port == port and new.plays == "playlist infection" and new.autostart and not new.errors
    with pytest.raises(SetupError, match="exists already"):
        create_server(fresh, NewServer("infection night", port=free_port()), fake_tool(), said.append)
    with pytest.raises(SetupError, match="used by another server"):
        create_server(fresh, NewServer("Third", port=port), fake_tool(), said.append)
    with pytest.raises(SetupError, match="folder name"):
        create_server(fresh, NewServer("a/b"), fake_tool(), said.append)
    with pytest.raises(SetupError, match="would share their records and logs"):  # infection-night to Huragok
        create_server(fresh, NewServer("Infection-Night", port=free_port()), fake_tool(), said.append)
    # A second new server links nothing: the root's game folder is there already.
    said.clear()
    (root / "project-reclaimer.exe").write_bytes(b"")  # one copy at the root for every server...
    second = create_server(fresh, NewServer("Second", port=free_port()), tool_for(fresh), said.append)
    assert second.ready and not any("linking" in s for s in said)
    assert read_toml(second.folder / "dedicated.toml")["server"][0]["playlist"] == "playlists/slayer.playlist.json"
    assert (second.folder / "project-reclaimer.exe").is_file()  # ...but one that updates itself needs its own
    later = load(settings_path)
    later.setup.steamcmd, later.setup.steam_username = "C:/steamcmd/steamcmd.exe", "host"  # the I key's sign-in
    third = create_server(later, NewServer("Third", port=free_port(), auto_update=False), tool_for(later), said.append)
    data = read_toml(third.folder / "dedicated.toml")
    assert data["auto_update"] is False and not (third.folder / "project-reclaimer.exe").exists()
    assert data["workshop"] == {"steamcmd": "C:/steamcmd/steamcmd.exe", "username": "host"}


def test_a_new_server_with_its_own_files_from_mcc(tmp_path: Path) -> None:
    settings = load(setup_root(tmp_path, with_game=False))
    said: list[str] = []
    with pytest.raises(SetupError, match="no Halo 3 files to share"):
        create_server(settings, NewServer("Own", port=free_port()), fake_tool(), said.append)
    outcome = create_server(settings, NewServer("Own", port=free_port(), share=set(), mcc_folder="",
                                                maps=["Guardian", "Valhalla", "Last Resort", "The Pit"]),
                            fake_tool(), said.append)
    assert outcome.ready
    assert (outcome.folder / "game" / "halo3" / "maps" / "guardian.map").is_file()
    assert not (tmp_path / "reclaimer" / "game").exists()
    data = read_toml(outcome.folder / "dedicated.toml")
    assert data["game"] == "game" and data["content"] == "content" and data["bans"] == "bans.json"
    # Sharing from a root with no game files yet copies them from MCC into the root once.
    settings = load(tmp_path / "reclaimer" / "settings.toml")
    (outcome.folder / "game").rename(outcome.folder / "game-off")  # Own has none to link any more
    # What game/ holds already (ODST's files, or what the background fetch is putting there) stays: merged into,
    # never deleted, and never a game/game inside it.
    odst = tmp_path / "reclaimer" / "game" / "halo3odst" / "maps" / "h100.map"
    odst.parent.mkdir(parents=True)
    odst.write_bytes(b"odst")
    outcome2 = create_server(settings, NewServer("Shared", port=free_port(), mcc_folder=""), fake_tool(), said.append)
    assert outcome2.ready and (tmp_path / "reclaimer" / "game" / "halo3" / "halo3.dll").is_file()
    assert odst.read_bytes() == b"odst" and not (tmp_path / "reclaimer" / "game" / "game").exists()
    assert not (settings.records / "new-server-stage").exists()


def test_a_server_whose_check_fails_is_kept_but_not_started(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    package = load_package(starters(tmp_path) / "team-slayer")
    outcome = create_server(settings, NewServer("Partial", port=free_port(), package=package, share=set(),
                                                mcc_folder="", maps=["Guardian"]), fake_tool(), lambda _: None)
    assert not outcome.ready and outcome.folder.is_dir() and outcome.check is not None
    assert any("autostart is off" in n for n in outcome.notes) and "The Pit" in outcome.check.text
    fresh = load(settings_path)
    assert fresh.server("Partial").autostart is False
    assert 'autostart = false' in settings_path.read_text(encoding="utf-8")


# The commands -----------------------------------------------------------------------------------

def test_the_setup_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    firstrun.write_files(root)  # the first start: PACKAGES/ with the starters
    assert (root / "PACKAGES" / "infection" / "package.toml").is_file()
    assert cli.main(["-c", str(settings_path), "package", "list"]) == 0
    out = capsys.readouterr().out
    assert "team-slayer" in out and "Campaign Co-op" in out
    port = free_port()
    assert cli.main(["-c", str(settings_path), "new", "Beta", "--package", "team-slayer", "--port", str(port),
                     "--no-start", "--welcome", "Welcome {name}"]) == 0
    out = capsys.readouterr().out
    assert "Made" in out and f"UDP {port}" in out
    beta = read_toml(root / "SERVERS" / "Beta" / "dedicated.toml")
    assert beta["server"][0]["welcome"] == ["Welcome {name}"] and beta["server"][0]["max_players"] == 16
    assert cli.main(["-c", str(settings_path), "new", "Beta", "--no-start"]) == 1
    assert "exists already" in capsys.readouterr().err
    assert cli.main(["-c", str(settings_path), "new", "Gamma", "--package", "nope", "--no-start"]) == 2
    assert "Packages:" in capsys.readouterr().err

    assert cli.main(["-c", str(settings_path), "content", "-s", "Beta", "pit"]) == 0
    assert "The Pit" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta"]) == 0
    assert "team-slayer.playlist.json" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "show", "team-slayer"]) == 0
    assert "Every game is installed" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "new", "Snipers", "--maps", "Guardian,Narrows",
                     "--games", "rockets,Slayer", "--vote-seconds", "30"]) == 0
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "use", "snipers"]) == 0
    assert "from the next vote" in capsys.readouterr().out
    assert read_toml(root / "SERVERS" / "Beta" / "dedicated.toml")["server"][0]["playlist"] == "playlists/snipers.playlist.json"
    assert cli.main(["-c", str(settings_path), "playlist", "-s", "Beta", "use", "missing"]) == 2

    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta"]) == 0
    assert "no Workshop items" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta", "add", "3014096841",
                     "https://steamcommunity.com/sharedfiles/filedetails/?id=2905749191"]) == 0
    assert read_toml(root / "SERVERS" / "Beta" / "dedicated.toml")["workshop"]["items"] == [3014096841, 2905749191]
    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta", "remove", "2905749191"]) == 0
    assert cli.main(["-c", str(settings_path), "workshop", "-s", "Beta", "install"]) == 0
    assert "workshop:3014096841/fake3014096841" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "dedicated", "-s", "Beta", "check"]) == 0
    assert "Everything is ready" in capsys.readouterr().out
    assert cli.main(["-c", str(settings_path), "dedicated", "-s", "Beta"]) == 2

    assert cli.main(["-c", str(settings_path), "package", "save", "Beta", "Beta Copy", "--description", "mine"]) == 0
    assert "Saved" in capsys.readouterr().out
    saved = load_package(root / "PACKAGES" / "beta-copy")
    assert saved.workshop_items == [3014096841] and saved.server["playlist"] == "playlists/snipers.playlist.json"
    assert cli.main(["-c", str(settings_path), "package", "show", "beta-copy"]) == 0
    out = capsys.readouterr().out
    assert "Workshop items" in out and "3014096841" in out and "playlists/snipers.playlist.json" in out
    assert cli.main(["-c", str(settings_path), "check"]) == 0  # the supervisor's own check still passes
    assert "Beta" in capsys.readouterr().out


# The window -------------------------------------------------------------------------------------


def test_the_setup_screens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    settings_path = setup_root(tmp_path)
    firstrun.write_files(settings_path.parent)
    opened: list[Path] = []
    monkeypatch.setattr(desktop, "open_editor", lambda path: opened.append(path) or f"opened {path.name}")
    monkeypatch.setattr(desktop, "open_path", lambda path: f"opened {path}")
    controller = SetupController(settings_path)
    port = free_port()
    keys = [
        # N: package 3 (infection, in the starters' folder order), name, port, not hidden, no password, 16 players,
        # make it, start it, a key to return.
        "N", "3", "ENTER", *"Night Server", "ENTER", *str(port), "ENTER", "N", "ENTER", "ENTER", "Y", "Y", "x",
        # C: check both servers.
        "C", "A", "ENTER", "x",
        # G on server 2: browse its maps, playlists (show the first, then back), check, then back out.
        "G", "2", "ENTER", "1", "1", "ESC", "2", "S", "1", "ENTER", "ESC", "4", "ESC",
        # O: the second server's files (the folder, then the four files of Huragok's, come first), its
        # dedicated.toml (its folder comes first).
        "O", "8", "ENTER", "2", "ENTER", "x",
        "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    try:
        outcome = run(menu.run())
    except RuntimeError as error:
        raise AssertionError(f"{error}\n{''.join(terminal.output)}") from None
    assert outcome == "quit"
    printed = "".join(terminal.output)
    assert "Made" in printed and "Night Server" in printed, printed
    assert controller.discovered == 1 and controller.calls == [("start", "Night Server", {})]
    folder = settings_path.parent / "SERVERS" / "Night Server"
    assert read_toml(folder / "dedicated.toml")["server"][0] == {"name": "Night Server", "port": port,
                                                                 "playlist": "playlists/infection.playlist.json",
                                                                 "max_players": 16}
    assert printed.count("Everything is ready") >= 3  # C on both, G's check
    assert "The Pit" in printed and "Every game is installed" in printed
    assert opened == [folder / "dedicated.toml"] and "re-checks its files" in printed
    assert (folder / "intervalmessages.txt").read_text(encoding="utf-8").startswith("# This server's own interval")


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
    assert load(settings_path).setup.share == ["game"] and load(settings_path).setup.mcc_folder is None
    # "bans" (what 0.4.0 to 0.4.4 wrote) is read and does nothing: every server keeps a ban list of its own.
    write_settings(root, None, 'backend = "attach"\n[setup]\nshare = ["bans"]\nmcc_folder = ""\n', staff())
    assert load(settings_path).setup.share == [] and load(settings_path).setup.mcc_folder == ""
    write_settings(root, None, 'backend = "attach"\n[setup]\nshare = ["maps"]\n', staff())
    with pytest.raises(Exception, match="isn't one of game, content"):
        load(settings_path)


# Names the program refuses, and what other servers use ---------------------------------------------

def test_names_the_server_program_refuses_are_refused(tmp_path: Path) -> None:
    assert valid_name("A" * 48) is None and valid_name("Café Ünicode 😀") is None
    assert "at most 48" in valid_name("A" * 49)
    for bad in ("DEL\x7fNAME", "a\u2028b", "a\x85b", "zero\u200bwidth", "tab\tname", "nul\x00"):
        assert "invisible" in valid_name(bad), repr(bad)
    assert "Windows keeps" in valid_name("CON") and "Windows keeps" in valid_name("nul.txt")


def test_a_new_server_never_takes_a_disabled_or_left_out_servers_name_or_port(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    bravo_port = free_port()
    make_server_folder(root / "SERVERS" / "Bravo", "Bravo", bravo_port)
    set_server_setting(root / "SERVERS" / "Bravo" / "dedicated.toml", "Bravo", "enabled", False)
    (root / "SERVERS" / "Broken").mkdir()
    (root / "SERVERS" / "Broken" / "dedicated.toml").write_text("[[server]\nname = ", encoding="utf-8")
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == [ALPHA]  # Bravo is off, Broken left out
    with pytest.raises(SetupError, match="exists already"):
        create_server(settings, NewServer("bravo", port=free_port()), fake_tool(), lambda _: None)
    with pytest.raises(SetupError, match=f"port {bravo_port} is used by another server"):
        create_server(settings, NewServer("Charlie", port=bravo_port), fake_tool(), lambda _: None)
    assert next_free_port(settings, start=bravo_port) != bravo_port


def test_ports_and_player_counts_the_program_refuses_are_refused(tmp_path: Path) -> None:
    settings = load(setup_root(tmp_path))
    for port in (70000, 80):
        with pytest.raises(SetupError, match="from 1024 to 65535"):
            create_server(settings, NewServer("Ported", port=port), fake_tool(), lambda _: None)
    for players in (0, 500):
        with pytest.raises(SetupError, match="max_players must be from 1 to 127"):
            create_server(settings, NewServer("Crowded", port=free_port(), max_players=players), fake_tool(),
                          lambda _: None)
    package = load_package(starters(tmp_path) / "campaign-coop")
    with pytest.raises(SetupError, match="from 1 to 16 \\(campaign co-op and ODST hold 16\\)"):
        create_server(settings, NewServer("Co-op", port=free_port(), max_players=24, package=package), fake_tool(),
                      lambda _: None)
    assert not [p for p in (tmp_path / "reclaimer" / "SERVERS").iterdir() if p.name.startswith(".new-")]


def test_a_build_that_fails_leaves_nothing_and_a_failed_check_is_off_before_it_appears(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    servers = tmp_path / "reclaimer" / "SERVERS"
    package = load_package(starters(tmp_path) / "team-slayer")

    def denied(*args: Any, **kwargs: Any) -> list[str]:
        raise PermissionError(13, "Access is denied", "content")

    monkeypatch.setattr("huragok.content.newserver.apply_package", denied)
    with pytest.raises(SetupError, match="couldn't make Broken: .*Access is denied.*nothing was left half-made"):
        create_server(settings, NewServer("Broken", port=free_port(), package=package), fake_tool(), lambda _: None)
    assert sorted(p.name for p in servers.iterdir()) == [ALPHA]
    monkeypatch.undo()

    real = os.replace
    seen: list[str] = []

    def spy(source: Any, target: Any) -> None:
        if Path(source).name.startswith(".new-"):
            seen.append(settings_path.read_text(encoding="utf-8"))
        real(source, target)

    monkeypatch.setattr(os, "replace", spy)
    outcome = create_server(settings, NewServer("Partial", port=free_port(), package=package, share=set(),
                                                mcc_folder="", maps=["Guardian"]), fake_tool(), lambda _: None)
    assert not outcome.ready and "autostart = false" in seen[0]  # written before discovery could see the folder


def test_a_server_that_shares_no_game_links_the_game_files_here(tmp_path: Path) -> None:
    """With neither the root's game shared nor an MCC to copy from, the first game folder here is linked."""
    settings = load(setup_root(tmp_path))
    outcome = create_server(settings, NewServer("Linked", port=free_port(), share={"content", "bans"}), fake_tool(),
                            lambda _: None)
    assert outcome.ready and (outcome.folder / "game" / "halo3" / "halo3.dll").is_file()
    mcc = tmp_path / "MCC"
    (mcc / "halo3" / "maps").mkdir(parents=True)
    (mcc / "halo3" / "halo3.dll").write_bytes(b"")
    (mcc / "halo3" / "maps" / "shared.map").write_bytes(b"")
    (mcc / "mcclauncher.exe").write_bytes(b"")
    from huragok.content.newserver import game_link_source
    from huragok.steam import is_mcc_install

    assert is_mcc_install(mcc) and game_link_source(settings) != mcc


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


# What it plays ---------------------------------------------------------------------------------------

def test_using_a_playlist_takes_out_the_servers_map_and_game(tmp_path: Path, capsys, run) -> None:
    settings_path = setup_root(tmp_path)
    config = settings_path.parent / "SERVERS" / ALPHA / "dedicated.toml"
    port = free_port()
    replace_server_blocks(config, [{"name": ALPHA, "port": port, "map": "Guardian", "game": "Slayer"}])
    assert cli.main(["-c", str(settings_path), "playlist", "-s", ALPHA, "use", "slayer"]) == 0
    assert server_blocks(config) == [{"name": ALPHA, "port": port, "playlist": "playlists/slayer.playlist.json"}]
    replace_server_blocks(config, [{"name": ALPHA, "port": port, "campaign": {"mission": 3010}}])
    settings = load(settings_path)
    menu = Menu(SetupController(settings_path), FakeTerminal(), StrictKeyboard([]), RecentLog())
    menu._use_playlist(settings.servers[0], config.parent / "playlists" / "slayer.playlist.json")
    assert server_blocks(config) == [{"name": ALPHA, "port": port, "playlist": "playlists/slayer.playlist.json"}]


# Packages --------------------------------------------------------------------------------------------

def test_a_package_with_a_mistyped_field_is_listed_as_broken(tmp_path: Path) -> None:
    folder = tmp_path / "PACKAGES"
    for name, line in (("decimal", 'version = "1.0"'), ("yes", 'needs_campaign = "false"')):
        (folder / name).mkdir(parents=True)
        (folder / name / "package.toml").write_text(f'name = "{name}"\n{line}\n', encoding="utf-8")
    listed = {p.key: p for p in list_packages(folder)}
    assert "version must be a whole number" in listed["decimal"].description
    assert "needs_campaign must be true or false" in listed["yes"].description
    assert all(p.name.endswith("(broken)") for p in listed.values())


def test_a_saved_package_carries_its_files_and_lands_in_the_right_content(tmp_path: Path) -> None:
    from huragok.content.packages import server_values

    root = tmp_path / "reclaimer"
    server = root / "SERVERS" / "Snipers"
    make_server_folder(server, "Snipers", free_port())
    config = server / "dedicated.toml"
    set_top_key(config, "content", "../../content")
    (root / "content" / "Game Modes").mkdir(parents=True)
    (root / "content" / "Game Modes" / "snipers.bin").write_bytes(b"snipers")
    (root / "content" / "Game Modes" / "night.json").write_text('{"mode": "Slayer"}', encoding="utf-8")
    replace_server_blocks(config, [{"name": "Snipers", "port": 1, "map": "Guardian",
                                    "game": "Game Modes/snipers.bin"}])
    package = save_package(server, "Snipers", tmp_path / "PACKAGES" / "snipers")
    assert (package.folder / "content" / "Game Modes" / "snipers.bin").read_bytes() == b"snipers"
    assert package.server["game"] == "Game Modes/snipers.bin"
    replace_server_blocks(config, [{"name": "Snipers", "port": 1, "variant": "../../content/Game Modes/night.json"}])
    package = save_package(server, "Snipers", tmp_path / "PACKAGES" / "night")
    assert package.server["variant"] == "content/Game Modes/night.json"
    assert (package.folder / "content" / "Game Modes" / "night.json").is_file()
    new = root / "SERVERS" / "New"
    assert server_values(package, new, root / "content")["variant"] == "../../content/Game Modes/night.json"
    assert server_values(package, new, new / "content")["variant"] == "content/Game Modes/night.json"


def test_a_saved_package_carries_what_its_server_names_by_name(tmp_path: Path) -> None:
    """A custom game, a game variant and a map variant named by name, in the playlist and in the server's block
    (one map and a custom game: `game = "BR Slayer"`), are in the package; game types and base maps aren't files."""
    server = tmp_path / "reclaimer" / "SERVERS" / "Snipers"
    make_server_folder(server, "Snipers", free_port())
    content = server / "content"
    (content / "Game Modes").mkdir(parents=True, exist_ok=True)
    (content / "Maps").mkdir(parents=True, exist_ok=True)
    (content / "Game Modes" / "gun-game.json").write_text('{"name": "Gun Game", "mode": "Slayer"}', encoding="utf-8")
    (content / "Game Modes" / "br-slayer.json").write_text('{"name": "BR Slayer", "mode": "Slayer"}', encoding="utf-8")
    (content / "Game Modes" / "snipers.bin").write_bytes(b"snipers")
    (content / "Maps" / "MLG Amp V7.mvar").write_bytes(b"amp")
    playlist = server / "playlists" / "mixed.playlist.json"
    playlist.parent.mkdir(parents=True, exist_ok=True)
    playlist.write_text(json.dumps({"version": 2, "name": "Mixed", "games": [
        {"map": "Guardian", "game": "Gun Game"}, {"map": "MLG Amp V7", "game": "snipers"},
        {"map": "Valhalla", "game": "Slayer"}]}), encoding="utf-8")
    config = server / "dedicated.toml"
    replace_server_blocks(config, [{"name": "Snipers", "port": 1, "playlist": "playlists/mixed.playlist.json"}])
    package = save_package(server, "Snipers", tmp_path / "PACKAGES" / "mixed")
    carried = sorted(p.relative_to(package.folder / "content").as_posix()
                     for p in (package.folder / "content").rglob("*") if p.is_file())
    assert carried == ["Game Modes/gun-game.json", "Game Modes/snipers.bin", "Maps/MLG Amp V7.mvar"]
    replace_server_blocks(config, [{"name": "Snipers", "port": 1, "map": "Valhalla", "game": "BR Slayer"}])
    package = save_package(server, "Snipers", tmp_path / "PACKAGES" / "br")
    assert (package.folder / "content" / "Game Modes" / "br-slayer.json").is_file()
    assert package.server["game"] == "BR Slayer"


def test_applying_a_package_keeps_the_shared_contents_own_files(tmp_path: Path) -> None:
    package_folder = tmp_path / "PACKAGES" / "maps"
    (package_folder / "content" / "Maps").mkdir(parents=True)
    (package_folder / "package.toml").write_text('name = "maps"\n', encoding="utf-8")
    (package_folder / "content" / "Maps" / "same.mvar").write_bytes(b"same")
    (package_folder / "content" / "Maps" / "differs.mvar").write_bytes(b"package's")
    (package_folder / "content" / "Maps" / "new.mvar").write_bytes(b"new")
    shared = tmp_path / "content"
    (shared / "Maps").mkdir(parents=True)
    (shared / "Maps" / "same.mvar").write_bytes(b"same")
    (shared / "Maps" / "differs.mvar").write_bytes(b"another server's")
    kept: list[str] = []
    copied = apply_package(load_package(package_folder), tmp_path / "Server", shared, kept)
    assert copied == ["content/Maps/new.mvar"] and kept == ["content/Maps/differs.mvar"]
    assert (shared / "Maps" / "differs.mvar").read_bytes() == b"another server's"
