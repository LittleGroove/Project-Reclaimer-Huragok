"""Every game's files, from Project Reclaimer 0.10 (Halo: Reach, Combat Evolved, Halo 2 beside Halo 3 and ODST): what a
game folder holds of each, what a server needs for its map or playlist, what a failed start says is missing, getting
them from MCC (no SteamCMD depot is known for them), I, N, `steam files`, `new --games`, the engine files, packages'
needs_games and the Every Halo starter. The layouts are 0.10.0's `dedicated init --maps …`, probed 2026-11-06."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from testkit.setup import ALPHA, SetupController, StrictKeyboard, fake_tool, setup_root
from testkit.window import FakeTerminal

from huragok import steam as steamsetup
from huragok.commandline import cli
from huragok.content import gamefiles, newserver, upkeep
from huragok.content.catalogue import Catalogue, Entry
from huragok.content.newserver import NewServer, create_server
from huragok.content.packages import Package, install_starters, load_package, write_package_toml
from huragok.core.events import Event, EventBus
from huragok.core.logs import RecentLog
from huragok.core.requests import Result
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, replace_server_blocks, set_table_key
from huragok.games import files
from huragok.program.dedicated import Dedicated, ToolResult
from huragok.window.dashboard import Menu

# What 0.10.0 said when a server's map is another game's that its game folder lacks (dedicated check and the start).
NO_REACH = ('no multiplayer map "Sword Base" is installed (this game folder has no Halo: Reach, Halo 2, whose maps are '
            'played from it)')
NO_GUARDIAN = ('no multiplayer map "Guardian" is installed (this game folder has no Halo: Reach, Halo: Combat Evolved, '
               'Halo 2, whose maps are played from it)')


def put(game: Path, *names: str) -> None:
    for name in names:
        (game / name).parent.mkdir(parents=True, exist_ok=True)
        (game / name).write_bytes(b"x")


def halo3(game: Path) -> Path:
    """What every `--maps <game>` brings of Halo 3's: the engine, shared, Guardian, and Forge's campaign files."""
    put(game, "halo3/halo3.dll", "halo3/maps/shared.map", "halo3/maps/guardian.map", "halo3/maps/campaign.map",
        "halo3/maps/040_voi.map")
    return game


def reach(game: Path, *, campaign: bool = False) -> Path:
    put(game, "haloreach/haloreach.dll", "haloreach/maps/shared.map", "haloreach/maps/campaign.map",
        "haloreach/maps/m05.map", "haloreach/maps/m70.map", "haloreach/maps/20_sword_slayer.map",
        "haloreach/maps/info/20_sword_slayer.mapinfo", "haloreach/maps/ff10_prototype.map")
    if campaign:
        put(game, *(f"haloreach/maps/{name}.map" for name in files.REACH_MISSIONS))
    return game


def halo1(game: Path) -> Path:
    put(game, "halo1/halo1.dll", "halo1/maps/bitmaps.map", "halo1/maps/ui.map", "halo1/maps/bloodgulch.map",
        "halo1/sound/pc/sounds_adpcm.fsb")
    return game


def halo2(game: Path) -> Path:
    put(game, "halo2/halo2.dll", "halo2/h2_maps_win64_dx11/shared.map", "halo2/h2_maps_win64_dx11/lockout.map",
        "halo2/h2_fonts/chs_yahei-10")
    return game


def mcc(folder: Path, *games: str) -> Path:
    """MCC's own folder with these games installed (halo3 always)."""
    halo3(folder)
    put(folder, "mcclauncher.exe", "halo3/maps/010_jungle.map")
    for key in games:
        {"reach": reach, "halo1": halo1, "halo2": halo2}[key](folder)
    return folder


class Tool(Dedicated):
    """The fake server program, whose `init --maps` also takes 0.10's game words and lays their files out as 0.10.0
    does (beside Halo 3's engine, shared and Guardian); `version` is what its `--version` says."""

    def __init__(self, version: str | None = "0.10.0") -> None:
        fake = fake_tool()
        super().__init__(fake.executable, template=fake.template)
        self.said = version
        self.inits: list[list[str]] = []

    def init(self, folder: Path, *, mcc: str | None = None, maps: list[str] | None = None) -> ToolResult:
        self.inits.append(list(maps or []))
        new = [word for word in maps or [] if word in ("reach", "reach-campaign", "halo1", "halo2")]
        rest = [word for word in maps or [] if word not in new]
        result = super().init(folder, mcc=mcc, maps=rest or (["Guardian"] if new else None))
        for word in new:
            {"reach": reach, "reach-campaign": lambda g: reach(g, campaign=True), "halo1": halo1,
             "halo2": halo2}[word](folder / "game")
        return result


@pytest.fixture(autouse=True)
def versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Tool's version is its own; every other program (the fake's) says none."""
    monkeypatch.setattr(gamefiles, "program_version_of", lambda program: getattr(program, "said", None))
    monkeypatch.setattr(gamefiles, "server_version", lambda settings, server: None)
    monkeypatch.setattr(gamefiles, "root_version", lambda settings: None)


def flat(text: str) -> str:
    """Printed text as one line: no colours, every run of spaces and line ends one space (notes wrap)."""
    return " ".join(re.sub(r"\[[0-9;]*m", "", text).split())


def playlist(folder: Path, *games: dict[str, Any]) -> str:
    path = folder / "playlists" / "mix.playlist.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "name": "Mix", "games": list(games)}), encoding="utf-8")
    return "playlists/mix.playlist.json"


# What a game folder holds --------------------------------------------------------------------------------------

def test_each_game_is_there_as_the_program_counts_it(tmp_path: Path) -> None:
    game = halo3(tmp_path / "game")
    assert files.present(game) == {kind: False for kind in files.KINDS} | {"halo3": True}
    put(game, "haloreach/haloreach.dll", "haloreach/maps/shared.map", "haloreach/maps/20_sword_slayer.map")
    assert not files.has_reach_files(game)  # 0.10 counts Reach installed only with its campaign.map
    reach(game)
    halo1(game)
    halo2(game)
    assert [k for k, there in files.present(game).items() if there] == ["halo3", "reach", "halo1", "halo2"]
    reach(game, campaign=True)
    assert files.has(game, "reach-campaign")
    assert files.engine_files(game) == ["halo3/halo3.dll", "haloreach/haloreach.dll", "halo1/halo1.dll",
                                        "halo2/halo2.dll"]
    only_dll = tmp_path / "dll"
    put(only_dll, "halo1/halo1.dll", "halo2/halo2.dll")
    assert not files.has_halo1_files(only_dll) and not files.has_halo2_files(only_dll)
    # On a program before 0.10 the new games aren't counted at all.
    assert list(files.present(game, "0.9.23")) == ["halo3", "campaign", "odst"]
    assert files.kinds_for("0.9.18") == ["halo3", "campaign"]


def test_a_server_needs_halo_3_and_each_game_its_maps_are_from(tmp_path: Path) -> None:
    assert files.needed_by({"map": "Sword Base", "game": "Slayer"}) == ["halo3", "reach"]
    assert files.needed_by({"map": "halo2_lockout"}) == ["halo3", "halo2"]
    assert files.needed_by({"map": "Lockout"}) == ["halo3"]  # Halo 3's Blackout, by its file name
    assert files.needed_by({"map": "halo1_bloodgulch_6381b55e"}) == ["halo3", "halo1"]  # a CE mod's map
    assert files.needed_by({"odst": {"mode": "firefight"}}) == ["halo3", "campaign", "odst"]  # 0.10: Halo 3's too
    assert files.needed_by({"odst": {"mode": "firefight"}}, version="0.9.23") == ["halo3", "odst"]
    assert files.needed_by({"reach": {"campaign": True}}) == ["halo3", "reach", "reach-campaign"]
    ref = playlist(tmp_path, {"map": "Guardian", "game": "Slayer"}, {"map": "Blood Gulch", "game": "ctf"},
                   {"map": "Sword Base", "game": "headhunter_054"}, {"campaign": {"mission": 3010}},
                   {"map": "halo2_lockout", "game": "slayer"})
    block = {"name": "Mix", "playlist": ref}
    assert files.needed_by(block, tmp_path) == ["halo3", "campaign", "reach", "halo1", "halo2"]
    assert files.needed_by(block, tmp_path, "0.9.23") == ["halo3", "campaign"]  # a 0.9 program plays no Reach map
    assert files.needed_by({"playlist": "playlists/gone.json"}, tmp_path) == ["halo3"]
    game = halo3(tmp_path / "game")
    assert files.missing(game, files.needed_by(block, tmp_path)) == ["campaign", "reach", "halo1", "halo2"]
    assert files.missing(tmp_path / "none", ["halo3", "reach"]) == ["halo3"]  # Halo 3's first, alone
    assert files.missing(game, "odst") == ["campaign", "odst"]  # the old way still works (0.10's ODST: the campaign too)


def test_a_failed_start_says_which_games_files_it_lacked() -> None:
    lines = ["[reach] Halo: Reach not installed in C:/g", "[halo1] Halo: Combat Evolved installed in C:/g", NO_REACH]
    assert files.kinds_in_failure(lines) == ["reach"]
    assert files.kinds_in_failure([NO_REACH.replace("Sword Base", "halo2_lockout")]) == ["halo2"]
    assert files.kinds_in_failure([NO_GUARDIAN]) == ["halo3"]
    # A map no game owns (a mod's, a typo): nothing is got, Halo 3's least of all.
    assert files.kinds_in_failure([NO_REACH.replace("Sword Base", "HugeGrass")]) == []
    assert files.kinds_in_failure(["[reach] Halo: Reach not installed in C:/g", "Port 46991 is in use"]) == []
    old = [f"Error: The game folder C:/g {files.MISSING_FILES}:", "  haloreach/maps/m10.map", "  halo1/halo1.dll"]
    assert files.kinds_in_failure(old) == ["reach-campaign", "halo1"]
    assert files.kinds_in_failure([f"x {files.MISSING_FILES}", "  nothing it knows"]) == ["halo3"]


# Getting them --------------------------------------------------------------------------------------------------

def test_reach_comes_from_mcc_alone_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(setup_root(tmp_path))
    found, why = gamefiles.sources(settings, "reach", downloads=True)
    assert found == [] and why.startswith("Halo: Reach's files come from your own MCC installation for now")
    assert gamefiles.no_way("reach", why) == why
    assert gamefiles.no_way("halo3", "SteamCMD isn't set up") == "MCC isn't on this PC and SteamCMD isn't set up"
    folder = mcc(tmp_path / "MCC", "reach")
    set_table_key(settings.path, "setup", "mcc_folder", str(folder), before="process")
    settings = load(settings.path)
    assert [s.how for s in gamefiles.sources(settings, "reach")[0]] == ["mcc"]
    assert gamefiles.sources(settings, "halo2")[0] == []  # Halo 2 isn't installed in that MCC
    tool = Tool()
    said = gamefiles.fetch(settings, "reach", gamefiles.Source("mcc", folder), settings.home / "game", lambda: tool,
                           lambda line: None)
    assert said.startswith("Halo: Reach's files are in") and files.has_reach_files(settings.home / "game")
    assert tool.inits == [["reach"]]
    with pytest.raises(OSError, match="need Project Reclaimer 0.10.0 or later"):
        gamefiles.fetch(settings, "halo1", gamefiles.Source("mcc", folder), settings.home / "game",
                        lambda: Tool("0.9.23"), lambda line: None)
    with pytest.raises(OSError, match="come from your own MCC installation"):
        gamefiles.gather(settings, "halo2", gamefiles.Source("steamcmd", folder, "x"), tmp_path / "stage",
                         lambda: tool, lambda line: None)


def test_a_server_whose_playlist_names_reach_waits_for_reachs_files(tmp_path: Path, run, caplog) -> None:
    settings_path = setup_root(tmp_path)
    server_folder = settings_path.parent / "SERVERS" / ALPHA
    ref = playlist(server_folder, {"map": "Guardian", "game": "Slayer"}, {"map": "Sword Base", "game": "Slayer"})
    replace_server_blocks(server_folder / "dedicated.toml", [{"name": ALPHA, "port": 46990, "playlist": ref}])
    settings = load(settings_path)
    tool = Tool()
    game_files = gamefiles.GameFiles(settings, EventBus(), lambda: tool)

    async def nothing_to_copy_from() -> str | None:
        game_files.prelaunch(settings.server(ALPHA))
        await asyncio.gather(*game_files.tasks)
        return game_files.prelaunch(settings.server(ALPHA))

    with caplog.at_level(logging.ERROR):
        wait = run(nothing_to_copy_from())
    assert wait and wait.startswith("needs Halo: Reach's files: Halo: Reach's files come from your own MCC")
    assert "Halo: Reach's files are missing and there's no way to get them here" in caplog.text
    rows = gamefiles.game_status(settings)
    assert ("missing", f"{ALPHA} (Halo: Reach's files)", "yellow") in rows
    folder = mcc(tmp_path / "MCC", "reach")
    set_table_key(settings_path, "setup", "mcc_folder", str(folder), before="process")
    settings = load(settings_path)
    game_files = gamefiles.GameFiles(settings, EventBus(), lambda: tool)

    async def copied() -> str | None:
        wait = game_files.prelaunch(settings.server(ALPHA))
        assert wait and wait.startswith("getting Halo: Reach's files")
        await asyncio.gather(*game_files.tasks)
        return game_files.prelaunch(settings.server(ALPHA))

    assert run(copied()) is None
    paths = gamefiles.read_paths(server_folder, settings.config_name)
    assert paths is not None and files.has_reach_files(paths.game)
    status = " ".join(text for _, text, _ in gamefiles.game_status(settings))
    assert "ODST · Reach · no CE · no Halo 2" in status and "missing" not in [label for label, _, _ in
                                                                             gamefiles.game_status(settings)]
    assert "Reach" not in gamefiles.games_text(paths.game, "0.9.23")  # an older program: not shown


def test_a_start_that_failed_for_a_reach_map_gets_reach_not_halo_3(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    folder = mcc(tmp_path / "MCC", "reach", "halo1")
    set_table_key(settings_path, "setup", "mcc_folder", str(folder), before="process")
    settings = load(settings_path)
    bus = EventBus()
    tool = Tool()
    game_files = gamefiles.GameFiles(settings, bus, lambda: tool)

    async def scenario() -> None:
        assert game_files.prelaunch(settings.server(ALPHA)) is None  # it plays Halo 3's slayer playlist
        bus.publish(Event("server.start_failed", ALPHA, {"why": "it exited", "lines": [NO_REACH]}))
        wait = game_files.prelaunch(settings.server(ALPHA))
        assert wait and wait.startswith("getting Halo: Reach's files")
        await asyncio.gather(*game_files.tasks)
        assert game_files.prelaunch(settings.server(ALPHA)) is None

    run(scenario())
    assert tool.inits == [["reach"]]


def test_a_game_folder_with_halo_3_is_never_swapped_for_another_game(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.server(ALPHA)
    halo1(halo3(settings.home / "game"))
    paths = gamefiles.read_paths(server.folder, settings.config_name)
    assert paths is not None and files.has_game_files(paths.game)
    assert gamefiles.fix_game_path(settings, paths, "playlist", ["halo3", "halo1"]) is None  # it lacks CE: added
    shutil.rmtree(paths.game)
    said = gamefiles.fix_game_path(settings, paths, "playlist", ["halo3", "halo1"])
    assert said and "it uses" in said and read_toml(server.config_path)["game"] == "../../game"


# I, `steam files`, the engine files ---------------------------------------------------------------------------

class Handing(SetupController):
    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        return [("Huragok", Result(True, "getting them in the background"))]


def test_i_offers_every_game_and_says_reach_comes_from_mcc(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    halo3(settings_path.parent / "game")  # the shared game/ the other games go beside
    terminal = FakeTerminal()
    menu = Menu(Handing(settings_path), terminal, StrictKeyboard(["5", "x", "ESC"]), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_steam())
    text = flat("".join(terminal.output))
    assert "[5] Add Halo: Reach's files" in text and "[8] Add Halo 2's files" in text
    assert "Halo: Reach's files come from your own MCC installation for now" in text
    folder = mcc(tmp_path / "MCC", "reach")
    set_table_key(settings_path, "setup", "mcc_folder", str(folder), before="process")
    controller = Handing(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["5", "1", "x", "ESC"]), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_steam())
    assert ("game_files", "all", {"kind": "reach", "how": "mcc"}) in controller.calls
    assert "Copy them from MCC at" in flat("".join(terminal.output))


def test_i_offers_only_what_an_older_program_plays(tmp_path: Path, run, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_path = setup_root(tmp_path)
    monkeypatch.setattr(gamefiles, "root_version", lambda settings: "0.9.23")
    terminal = FakeTerminal()
    menu = Menu(Handing(settings_path), terminal, StrictKeyboard(["ESC"]), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_steam())
    text = flat("".join(terminal.output))
    assert "[4] Add Halo 3: ODST's files" in text and "Reach" not in text


def test_steam_files_for_the_new_games(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings_path = setup_root(tmp_path)
    halo3(settings_path.parent / "game")
    assert cli.main(["-c", str(settings_path), "steam", "files", "halo2"]) == 1
    out = capsys.readouterr()
    assert "No way to get Halo 2's files here: Halo 2's files come from your own MCC" in flat(out.out + out.err)
    assert cli.main(["-c", str(settings_path), "steam", "files", "halo4"]) == 2
    assert cli.main(["-c", str(settings_path), "new", "X", "--games", "reach,halo4"]) == 2
    out = capsys.readouterr()
    assert "--games: 'halo4' isn't one of" in flat(out.out + out.err)


def test_the_engine_files_of_every_game_after_an_mcc_update(tmp_path: Path) -> None:
    game = halo2(reach(halo3(tmp_path / "game")))
    source = mcc(tmp_path / "MCC", "reach", "halo2")
    for name in ("haloreach/haloreach.dll", "halo2/halo2.dll"):
        (source / name).write_bytes(b"new")
    said = upkeep.copy_engine(game, source)
    assert (game / "haloreach" / "haloreach.dll").read_bytes() == b"new" and "halo2.dll" in said
    assert list((game / "haloreach").glob("haloreach.dll.before-*"))


def test_find_mcc_prefers_halo_3_and_takes_another_game_without_it(tmp_path: Path) -> None:
    only_reach = tmp_path / "MCC"
    put(only_reach, "haloreach/haloreach.dll")
    assert steamsetup.find_mcc(str(only_reach)) == only_reach
    assert steamsetup.find_mcc(str(tmp_path / "nothing")) is None
    put(only_reach, "halo3/halo3.dll")
    assert steamsetup.find_mcc(str(only_reach)) == only_reach


# N and `new` ---------------------------------------------------------------------------------------------------

def test_a_new_server_gets_the_games_picked_from_mcc(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    folder = mcc(tmp_path / "MCC", "reach", "halo1")
    settings = load(settings_path)
    halo3(settings.home / "game")
    said: list[str] = []
    tool = Tool()
    outcome = create_server(settings, NewServer("Mixed", port=46989, mcc_folder=str(folder),
                                                games=["reach", "halo1", "halo2"]), tool, said.append)
    game = settings.home / "game"
    assert files.has_reach_files(game) and files.has_halo1_files(game) and not files.has_halo2_files(game)
    assert ["reach", "halo1"] in tool.inits  # one init for both
    assert any("Halo 2's files, which aren't in" in note and "no MCC with them" in note for note in outcome.notes)
    old = Tool("0.9.23")
    outcome = create_server(load(settings_path), NewServer("Older", port=46988, mcc_folder=str(folder),
                                                           games=["halo2"]), old, said.append)
    assert any("need Project Reclaimer 0.10.0 or later, and this program is 0.9.23" in n for n in outcome.notes)


def test_n_asks_which_games_mcc_has_and_copies_them(tmp_path: Path, run, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_path = setup_root(tmp_path)
    folder = mcc(tmp_path / "MCC", "reach", "halo2")
    set_table_key(settings_path, "setup", "mcc_folder", str(folder), before="process")
    halo3(settings_path.parent / "game")
    made: list[NewServer] = []

    def create(settings: Any, spec: NewServer, tool: Any, say: Any = print) -> Any:
        made.append(spec)
        raise newserver.SetupError("stopped here")

    from huragok.window.screens import setup as menu_setup

    monkeypatch.setattr(menu_setup, "create_server", create)
    monkeypatch.setattr(Menu, "_tool", lambda self, settings: asyncio.sleep(0, Tool()), raising=False)
    keys = ["ENTER", *"Mixed", "ENTER", "ENTER", "N", "ENTER", "ENTER", "ENTER", "Y", "x"]  # Enter: the default games
    terminal = FakeTerminal()
    menu = Menu(SetupController(settings_path), terminal, StrictKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    run(menu.screen_new())
    text = flat("".join(terminal.output))
    assert "Halo: Reach's files" in text and "Halo 2's files" in text and "Halo: Reach's campaign" not in text
    assert made and made[0].games == ["reach", "halo2"]


# Packages ------------------------------------------------------------------------------------------------------

def test_a_package_names_the_games_it_needs(tmp_path: Path) -> None:
    package = Package(tmp_path / "mix", "Mix", needs_games=["reach", "halo2"],
                      server={"playlist": "playlists/x.playlist.json"})
    (package.folder / "playlists").mkdir(parents=True)
    (package.folder / "playlists" / "x.playlist.json").write_text("{}", encoding="utf-8")
    write_package_toml(package)
    loaded = load_package(package.folder)
    assert loaded.needs_games == ["reach", "halo2"] and loaded.file_kinds == ["reach", "halo2"]
    assert "needs Halo: Reach's and Halo 2's files (Project Reclaimer 0.10)" in loaded.summary()
    (package.folder / "package.toml").write_text('name = "x"\nneeds_games = ["halo4"]\n', encoding="utf-8")
    with pytest.raises(Exception, match="needs_games must be a list of these"):
        load_package(package.folder)


def test_the_every_halo_starter_mixes_four_games(tmp_path: Path) -> None:
    install_starters(tmp_path)
    package = load_package(tmp_path / "every-halo")
    assert package.needs_games == ["reach", "halo1", "halo2"] and package.server["max_players"] == 16
    games = json.loads(package.playlists[0].read_text(encoding="utf-8"))["games"]
    assert files.needed_by({"playlist": "playlists/every-halo.playlist.json"}, package.folder) == \
        ["halo3", "reach", "halo1", "halo2"]
    from huragok.games.bungie import GAMES

    for game in games:  # CE's and Halo 2's maps play only their own game types (0.10.0 played every one of these)
        owner = files.entry_game(game)
        if owner is not None and owner.own_rules_only:
            assert game["game"].lower() in {"slayer", "ctf", "oddball", "koth"}, game
    assert {files.entry_game(g).key if files.entry_game(g) else "halo3" for g in games} == set(GAMES) - {"odst"}


def test_browse_shows_each_games_maps(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, StrictKeyboard(["2", "ESC"]), RecentLog())  # type: ignore[arg-type]
    catalogue = Catalogue(maps=[Entry("Guardian", "guardian", "map", game="halo3"),
                                Entry("Sword Base", "20_sword_slayer", "map", game="reach")], shape="0.10")
    run(menu._browse(controller.settings.servers[0], catalogue))
    text = flat("".join(terminal.output))
    assert "Halo 3's maps" in text and "Halo: Reach's maps" in text and "Sword Base" in text
