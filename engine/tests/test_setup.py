"""Setting servers up: the `dedicated` runner, packages, new server folders, the window's N/G/C/E screens and
the commands, all against the fake `dedicated` (Windows and Linux)."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from conftest import free_port, staff, write_settings
from fake_dedicated import TEMPLATE
from test_menu import FakeKeyboard, FakeTerminal

from reclaimer_supervisor import cli, desktop
from reclaimer_supervisor.config import load
from reclaimer_supervisor.dedicated import Dedicated, DedicatedError, parse_content, parse_playlists, tool_for, \
    workshop_references
from reclaimer_supervisor.manager import Result
from reclaimer_supervisor.menu import Menu
from reclaimer_supervisor.newserver import NewServer, SetupError, create_server, next_free_port, relative_posix, \
    valid_name
from reclaimer_supervisor.packages import PackageError, apply_package, install_starters, list_packages, load_package, \
    save_package
from reclaimer_supervisor.tomledit import read_toml, replace_server_blocks, server_blocks, set_server_setting, \
    set_table_key, set_top_key
from reclaimer_supervisor.tui import RecentLog

FAKE = Path(__file__).resolve().parent / "fake_dedicated.py"
PYTHON = Path(sys.executable).as_posix()
TEMPLATE_COMMAND = [PYTHON, FAKE.as_posix(), "dedicated", "run", "--config", "{config}"]
ALPHA = "Alpha Server"


def fake_tool() -> Dedicated:
    return Dedicated(FAKE, template=TEMPLATE_COMMAND)


def make_server_folder(folder: Path, name: str, port: int, *, with_game: bool = True) -> None:
    """A server folder as the fake `dedicated init --from` makes it, with one server and RCON on."""
    result = fake_tool().init(folder, mcc="" if with_game else None)
    assert result.ok, result.text
    config = folder / "dedicated.toml"
    set_table_key(config, "rcon", "password", "secretsecret")
    replace_server_blocks(config, [{"name": name, "port": port, "playlist": "playlists/slayer.playlist.json"}])


def setup_root(tmp_path: Path, *, with_game: bool = True, extra: str = "") -> Path:
    """A root with Alpha Server made by the fake init; [process] command runs the fake, so tool_for finds it."""
    root = tmp_path / "reclaimer"
    make_server_folder(root / "SERVERS" / ALPHA, ALPHA, free_port(), with_game=with_game)
    (root / "SERVERS" / ALPHA / "project-reclaimer.exe").write_bytes(b"")  # found; never run (the command is the fake)
    command = ", ".join(json.dumps(w) for w in TEMPLATE_COMMAND)
    body = f'backend = "process"\n[process]\ncommand = [{command}]\n{extra}'
    return write_settings(root, None, body, staff(), backend=None)


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
    replace_server_blocks(config, [{"name": "Infection Night", "port": 49180, "playlist": "playlists/i.playlist.json",
                                    "hidden": True, "welcome": ["hi {name}"],
                                    "campaign": {"mission": 3010, "difficulty": "heroic"}}])
    data = read_toml(config)
    assert data["game"] == "../../game" and data["host_name"] == "LG" and data["content"] == "content"
    assert data["rcon"] == {"password": "secretsecret", "address": "127.0.0.1"}
    assert data["defaults"]["max_ping"] == 150 and data["defaults"]["max_players"] == 16
    assert data["workshop"] == {"items": [3014096841, 7]} and data["master"]["port"] == 49175
    assert server_blocks(config) == [{"name": "Infection Night", "port": 49180, "playlist": "playlists/i.playlist.json",
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
                                         "scripted-modes", "campaign-coop"}
    infection = load_package(folder / "infection")
    assert infection.plays == "playlist infection" and infection.server["max_players"] == 16
    assert [p.name for p in infection.playlists] == ["infection.playlist.json"] and not infection.workshop_items
    coop = load_package(folder / "campaign-coop")
    assert coop.needs_campaign and coop.plays.startswith("campaign co-op") and coop.server["campaign"]["mission"] == 3010
    assert install_starters(folder) == []  # a second time copies nothing: the host may have edited them
    (folder / "broken").mkdir()
    (folder / "broken" / "package.toml").write_text('name = "x"\n[server]\nport = 5\n', encoding="utf-8")
    broken = next(p for p in list_packages(folder) if p.key == "broken")
    assert broken.name.endswith("(broken)") and "chosen when the server is made" in broken.description
    with pytest.raises(PackageError, match="isn't in the package"):
        (folder / "nofile").mkdir()
        (folder / "nofile" / "package.toml").write_text('[server]\nplaylist = "playlists/x.json"\n', encoding="utf-8")
        load_package(folder / "nofile")


def test_saving_a_server_as_a_package(tmp_path: Path) -> None:
    folder = tmp_path / "Slayer"
    make_server_folder(folder, "Slayer", 49190)
    playlist = {"version": 2, "name": "Mixed", "games": [
        {"map": "Maps/my forge map.json", "base_map": "workshop:3014096841/forge_halo", "game": "Game Modes/snipers.bin",
         "preview_image": "snipers.png"},
        {"map": "Guardian", "game": "Slayer"}, {"map": "workshop:55/x", "game": "Slayer"}]}
    (folder / "playlists" / "mixed.playlist.json").write_text(json.dumps(playlist), encoding="utf-8")
    for relative in ("Maps/my forge map.json", "Game Modes/snipers.bin", "Images/snipers.png"):
        (folder / "content" / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / "content" / relative).write_bytes(b"x")
    config = folder / "dedicated.toml"
    replace_server_blocks(config, [{"name": "Slayer", "port": 49190, "playlist": "playlists/mixed.playlist.json",
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
    assert (root / "content" / "Game Modes").is_dir() and (root / "bans.json").is_file()
    data = read_toml(outcome.folder / "dedicated.toml")
    assert data["game"] == "../../game" and data["content"] == "../../content" and data["bans"] == "../../bans.json"
    assert not (outcome.folder / "game").exists() and not (outcome.folder / "content").exists()
    assert len(data["rcon"]["password"]) == 32 and data["rcon"]["password"] == outcome.rcon_password
    assert data["host_name"] == "Dedicated server"  # copied from Alpha's folder
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
    # A second new server links nothing: the root's game folder is there already.
    said.clear()
    second = create_server(fresh, NewServer("Second", port=free_port()), fake_tool(), said.append)
    assert second.ready and not any("linking" in s for s in said)
    assert read_toml(second.folder / "dedicated.toml")["server"][0]["playlist"] == "playlists/slayer.playlist.json"


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
    outcome2 = create_server(settings, NewServer("Shared", port=free_port(), mcc_folder=""), fake_tool(), said.append)
    assert outcome2.ready and (tmp_path / "reclaimer" / "game" / "halo3" / "halo3.dll").is_file()
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
    cli.write_files(root)  # the first start: PACKAGES/ with the starters
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

class StrictKeyboard(FakeKeyboard):
    """Fails the test when the script runs out of keys, instead of letting the window loop forever."""

    async def read(self, timeout: float | None = None) -> str | None:
        key = await super().read(timeout)
        if key is None and not (timeout is not None and timeout < 1):
            raise RuntimeError("the scripted keys ran out: a screen read them differently than the test expected")
        return key


class SetupController:
    """A controller over a real root, as the supervising window's is, with the fleet replaced by a call log."""

    supervising = True
    launcher = "start.cmd"

    def __init__(self, settings_path: Path) -> None:
        self.settings = load(settings_path)
        self.home = self.settings.home
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.discovered = 0

    def refresh_settings(self) -> Any:
        self.settings = load(self.settings.path)
        return self.settings

    def discover(self) -> None:
        self.discovered += 1

    def header(self) -> list[str]:
        return []

    def rows(self) -> list[dict[str, Any]]:
        return [{"index": i, "name": s.name, "slug": s.slug, "port": s.game_port, "autostart": s.autostart,
                 "phase": "idle", "status": "stopped", "color": "gray", "players": "", "names": "", "game": "",
                 "detail": "", "can": {"start": True, "stop": False, "restart": True, "cancel": False, "talk": False}}
                for i, s in enumerate(self.settings.servers, 1)]

    def events(self, count: int) -> list[tuple[int, str]]:
        return []

    def problems(self) -> list[str]:
        return []

    def finished(self) -> str | None:
        return None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        return [(server, Result(True, "starting"))]

    def request_quit(self) -> None:
        pass


def test_the_setup_screens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run) -> None:
    settings_path = setup_root(tmp_path)
    cli.write_files(settings_path.parent)
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
        # E: the second server's files, its dedicated.toml.
        "E", "5", "ENTER", "1", "ENTER", "x",
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


def test_dot_folders_are_ignored_and_plays_is_read(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    root = settings_path.parent
    make_server_folder(root / "SERVERS" / ".new-x", "Half Made", free_port())
    make_server_folder(root / "SERVERS" / "_aside", "Set Aside", free_port())
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == [ALPHA]
    assert settings.servers[0].plays == "playlist slayer"
    config = root / "SERVERS" / ALPHA / "dedicated.toml"
    replace_server_blocks(config, [{"name": ALPHA, "port": 49190, "map": "Guardian", "game": "rockets"}])
    assert load(settings_path).servers[0].plays == "rockets on Guardian"
    assert load(settings_path).setup.share == ["game", "content", "bans"] and load(settings_path).setup.mcc_folder is None
    write_settings(root, None, 'backend = "attach"\n[setup]\nshare = ["bans"]\nmcc_folder = ""\n', staff())
    assert load(settings_path).setup.share == ["bans"] and load(settings_path).setup.mcc_folder == ""
    write_settings(root, None, 'backend = "attach"\n[setup]\nshare = ["maps"]\n', staff())
    with pytest.raises(Exception, match="isn't one of game, content, bans"):
        load(settings_path)
