"""Project Reclaimer 0.9.19 and 0.9.20: Halo 3: ODST servers (what they play, their files, their console, their game
ending in a new lobby), the camera setting, campaign sprint, and the game option labels 0.9.19 renamed."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from conftest import free_port, staff, write_root
from test_extras import fake_mcc
from test_manage import StubClient
from test_manager import SERVER, cycle, world  # noqa: F401 - world is a fixture
from test_setup import ALPHA, SetupController, fake_tool, setup_root
from test_upkeep import menu_on, text_of

from huragok import livesettings as ls
from huragok import odst, statuscard, upkeep
from huragok.catalogue import Catalogue, Entry, parse_listing
from huragok.config import RelayUser, load
from huragok.events import Event, EventBus
from huragok.files import parse_log_line
from huragok.gameoptions import trait_options
from huragok.manager import Lifecycle
from huragok.menu import Menu
from huragok.newserver import NewServer, add_odst_files, create_server, has_odst_files
from huragok.packages import find_package, install_starters
from huragok.playlists import old_labels, rename_old_labels
from huragok.relay import Relay, Session
from huragok.state import Player, ServerState
from huragok.tomledit import read_toml, set_server_setting

DATA = Path(__file__).resolve().parent / "data"
FIREFIGHT = {"mode": "firefight", "mission": 5000, "insertion": 7, "variant": "odst_ff_classic", "difficulty": "heroic"}


def odst_mcc(folder: Path) -> Path:
    """A fake MCC folder with Halo 3 and Halo 3: ODST installed."""
    mcc = fake_mcc(folder)
    (mcc / "halo3odst").mkdir(parents=True, exist_ok=True)
    (mcc / "halo3odst" / "halo3odst.dll").write_bytes(b"odst dll")
    return mcc


# What the program lists and how Huragok says it ---------------------------------------------------

def test_the_programs_odst_listing_is_read_and_kept_apart() -> None:
    text = (DATA / "content-with-odst-0.9.20.txt").read_text(encoding="utf-8")
    listing = odst.parse_listing(text)
    assert len(listing.missions) == 11 and len(listing.maps) == 10 and len(listing.types) == 27
    streets = next(c for c in listing.missions if c.mission == 5000)
    assert (streets.title, streets.insertion) == ("Mombasa Streets", 6)
    assert next(c for c in listing.missions if c.mission == 4100).insertion == 0  # Prepare to Drop: one start
    night = next(c for c in listing.maps if c.title == "Crater (Night)")
    assert (night.mission, night.insertion) == (5000, 7) and "start 7" in night.label
    assert [c.variant for c in listing.types if c.title == "Floodfight"] == ["odst_ff_floodfight",
                                                                              "odst_ff_floodfight_1set"]
    # The built-in tables are that listing.
    builtin = odst.builtin()
    assert [(c.mission, c.title, c.insertion) for c in builtin.missions] == \
        [(c.mission, c.title, c.insertion) for c in listing.missions]
    assert [(c.mission, c.insertion, c.title) for c in builtin.maps] == \
        [(c.mission, c.insertion, c.title) for c in listing.maps]
    assert sorted(c.variant for c in builtin.types) == sorted(c.variant for c in listing.types)
    # The Halo 3 catalogue doesn't take ODST's lines for its own, and carries them beside.
    catalogue = parse_listing(text)
    assert [e.name for e in catalogue.maps] == ["Guardian"] and len(catalogue.game_types) == 10
    assert not any("odst" in e.reference.lower() for e in catalogue.game_variants + catalogue.map_variants)
    assert len(catalogue.odst.maps) == 10 and "Halo 3: ODST's 11 missions and 10 Firefight maps" in catalogue.counts()
    assert odst.parse_listing('Maps: map = "…"\n  "Guardian"\n').empty


def test_an_odst_setup_in_a_few_words() -> None:
    assert odst.describe(FIREFIGHT) == "ODST Firefight Crater (Night), Ff Classic (heroic)"
    assert odst.describe({"mode": "firefight", "mission": 6100, "insertion": 4}) == "ODST Firefight Crater (normal)"
    assert odst.describe({"mission": 6100, "insertion": 0, "difficulty": "legendary"}) == \
        "ODST campaign Tayari Plaza (legendary)"
    assert odst.describe({"mission": 5000, "insertion": 3}) == "ODST campaign Mombasa Streets, start 3 (normal)"
    assert odst.describe({"mission": 9999}) == "ODST campaign 9999 (normal)" and odst.describe("odd") == \
        "ODST campaign  (normal)"
    assert odst.type_title(None) == "Firefight" and odst.map_title(1, 2) == "map 1/2"


def test_what_an_odst_server_plays_and_the_camera(tmp_path: Path) -> None:
    block = {"name": "ODST", "port": 1, "odst": FIREFIGHT}
    assert ls.plays_of(block) == "ODST Firefight Crater (Night), Ff Classic (heroic)" and ls.plays_kind(block) == "odst"
    assert [ls.plays_kind(b) for b in ({"playlist": "x"}, {"campaign": {}}, {"variant": "v"}, {"map": "Guardian"},
                                       {})] == ["playlist", "campaign", "variant", "map", "default"]
    camera = ls.find("camera", "server")
    assert camera.group == "gameplay" and camera.default == "first_person" and camera.applies == "next game"
    assert ls.parse(camera, "Player_Choice") == "player_choice"
    with pytest.raises(ValueError, match="first_person, player_choice, third_person"):
        ls.parse(camera, "over the shoulder")
    # Switching an ODST server to something else takes `odst` out (a server plays one kind of thing).
    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    ls.write_plays(server.config_path, ALPHA, {"odst": FIREFIGHT})
    reread = load(settings_path).servers[0]
    assert reread.plays == "ODST Firefight Crater (Night), Ff Classic (heroic)" and reread.plays_kind == "odst"
    ls.write_plays(server.config_path, ALPHA, {"playlist": "playlists/slayer.playlist.json"})
    assert "odst" not in read_toml(server.config_path)["server"][0]


# Its game files -------------------------------------------------------------------------------------

def test_odst_files_are_added_to_a_game_folder_that_has_halo_3s(tmp_path: Path) -> None:
    mcc = odst_mcc(tmp_path / "MCC")
    game = tmp_path / "game"
    (game / "halo3").mkdir(parents=True)
    (game / "halo3" / "halo3.dll").write_bytes(b"mine")
    said: list[str] = []
    linked, copied = add_odst_files(fake_tool(), game, str(mcc), tmp_path / "stage", said.append)
    assert has_odst_files(game) and linked + copied > 0 and not (tmp_path / "stage").exists()
    assert (game / "halo3" / "halo3.dll").read_bytes() == b"mine"  # what was there stays as it was
    assert "copying Halo 3: ODST's files" in said[0] and "Halo 3: ODST's files are in" in said[-1]
    with pytest.raises(Exception, match="couldn't copy ODST's files"):
        add_odst_files(fake_tool(), tmp_path / "other", str(fake_mcc(tmp_path / "MCC2")), tmp_path / "stage", said.append)


def test_a_new_odst_server_gets_odsts_files_from_mcc(tmp_path: Path, monkeypatch) -> None:
    mcc = odst_mcc(tmp_path / "MCC")
    settings_path = setup_root(tmp_path, extra=f"[setup]\nmcc_folder = {json.dumps(str(mcc))}\n")
    settings = load(settings_path)
    install_starters(settings.packages_folder)
    package = find_package(settings.packages_folder, "odst-firefight")
    said: list[str] = []
    outcome = create_server(settings, NewServer("ODST Night", package=package, mcc_folder=str(mcc)), fake_tool(),
                            said.append)
    assert outcome.ready, outcome.check.text if outcome.check else outcome.notes
    assert has_odst_files(settings.home / "game") and any("Halo 3: ODST's files are in" in line for line in said)
    block = read_toml(outcome.folder / "dedicated.toml")["server"][0]
    assert block["odst"] == FIREFIGHT and "playlist" not in block and block["max_players"] == 4
    assert "Halo 3: ODST" in outcome.check.text  # the program's check saw an ODST server
    # Without an MCC that has ODST: made all the same, with a note saying how.
    monkeypatch.setattr("huragok.newserver.find_mcc", lambda hint=None: None)
    settings = load(settings_path)
    (settings.home / "game" / "halo3odst" / "halo3odst.dll").unlink()
    outcome = create_server(settings, NewServer("ODST Day", extra={"odst": {"mission": 6100}}), fake_tool(),
                            said.append)
    assert not outcome.ready and any("no MCC with ODST installed was found" in note for note in outcome.notes)
    assert "Halo 3: ODST's files are not in the game folder" in outcome.check.text


def test_the_engine_files_after_an_mcc_update_include_odsts(tmp_path: Path) -> None:
    assert upkeep.engine_out_of_date("Error: This halo3odst.dll is not the ODST build this release supports.")
    assert upkeep.odst_files_missing("Error: Server 'X': Halo 3: ODST's files are not in the game folder C:/g: …")
    mcc, game = odst_mcc(tmp_path / "mcc"), tmp_path / "game"
    for folder in ("halo3", "halo3odst"):
        (game / folder).mkdir(parents=True)
    (game / "halo3" / "halo3.dll").write_bytes(b"old")
    (game / "halo3odst" / "halo3odst.dll").write_bytes(b"old odst")
    text = upkeep.copy_engine(game, mcc)
    assert "halo3odst.dll" in text and (game / "halo3odst" / "halo3odst.dll").read_bytes() == b"odst dll"
    assert [p.read_bytes() for p in (game / "halo3odst").glob("halo3odst.dll.before-*")] == [b"old odst"]
    assert upkeep.engine_files(tmp_path / "nothing") == ["halo3/halo3.dll"]


def test_a_check_offers_odsts_files(tmp_path: Path, run, monkeypatch) -> None:
    mcc = odst_mcc(tmp_path / "MCC")
    settings_path = setup_root(tmp_path, extra=f"[setup]\nmcc_folder = {json.dumps(str(mcc))}\n")
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    menu, terminal = menu_on(controller, ["Y"])
    run(menu._after_check(controller.settings, server, "Error: Server 'Alpha Server': Halo 3: ODST's files are not in "
                                                       "the game folder C:/game: copy them …"))
    assert has_odst_files(server.folder / "game") and "has no Halo 3: ODST files" in text_of(terminal)
    menu, terminal = menu_on(controller, [])
    (server.folder / "game" / "halo3odst" / "halo3odst.dll").unlink()
    monkeypatch.setattr("huragok.menu_setup.odst_mcc", lambda hint=None: None)
    assert not run(menu._offer_odst_files(controller.settings, server.folder / "game"))
    assert "No MCC with Halo 3: ODST installed was found" in text_of(terminal)


# The window: D -> What it plays -> Halo 3: ODST ------------------------------------------------------

def test_an_odst_game_is_picked_from_lists(tmp_path: Path, run, monkeypatch) -> None:
    mcc = odst_mcc(tmp_path / "MCC")
    settings_path = setup_root(tmp_path, extra=f"[setup]\nmcc_folder = {json.dumps(str(mcc))}\n")
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    set_server_setting(server.config_path, ALPHA, "max_players", 24)
    listing = odst.parse_listing((DATA / "content-with-odst-0.9.20.txt").read_text(encoding="utf-8"))

    async def catalogue(self: Any, tool: Any, settings: Any, server: Any) -> Catalogue:
        return Catalogue(maps=[Entry("Guardian", "Guardian", "map")], odst=listing)

    monkeypatch.setattr(Menu, "_catalogue", catalogue)
    keys = ["1", "ENTER",  # Alpha
            "2", "ENTER", "Y",  # What it plays
            "5", "Y",  # Halo 3: ODST; its game folder has none of ODST's files: add them from the fake MCC
            "2", "2", "ENTER", "1", "ENTER", "3",  # Firefight: Crater (Night), Ff Classic, heroic
            "Y", "x",  # max_players 24 -> 16, written
            "2", "ENTER", "Y", "5",  # again: a campaign mission this time
            "1", "2", "ENTER", "3", "ENTER", "4", "Y", *"150", "ENTER", "ENTER", "ENTER", "ENTER",
            "Y", *"150", "ENTER", *"10", "ENTER", "x",  # Mombasa Streets from start 3, legendary, sprint
            "ESC"]
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    assert "now plays ODST Firefight Crater (Night), Ff Classic (heroic)" in text
    assert "max_players is 24 (this server)" in text and "max_players = 16 written for this server" in text
    assert "now plays ODST campaign Mombasa Streets, start 3 (legendary)" in text
    assert "Now: ODST Firefight Crater (Night), Ff Classic (heroic)" in text
    block = read_toml(server.config_path)["server"][0]
    assert block["max_players"] == 16 and "playlist" not in block
    assert block["odst"] == {"mission": 5000, "insertion": 3, "difficulty": "legendary",
                             "modifiers": {"health_percent": 150, "sprint": True, "sprint_speed_percent": 150,
                                           "sprint_stamina_seconds": 10}}
    assert has_odst_files(server.folder / "game")


def test_campaign_modifiers_keep_what_they_dont_ask_about(tmp_path: Path, run) -> None:
    settings_path = setup_root(tmp_path)
    controller = SetupController(settings_path)
    server = controller.settings.servers[0]
    ls.write_plays(server.config_path, ALPHA, {"campaign": {"mission": 3010, "customization": ["Armor Pack"],
                                                            "modifiers": {"skulls": 5, "sprint": True,
                                                                          "sprint_speed_percent": 120}}})
    keys = ["1", "ENTER", "2", "ENTER", "Y", "4", "1", "ENTER", "0", "ENTER", "2", "N",
            "Y", "ENTER", *"33", "ENTER", "ENTER", "ENTER", "N", "x", "ESC"]  # damage 33: refused; sprint off
    menu, terminal = menu_on(controller, keys)
    run(menu.screen_settings())
    campaign = read_toml(server.config_path)["server"][0]["campaign"]
    assert campaign == {"mission": 3010, "insertion": 0, "difficulty": "normal", "customization": ["Armor Pack"],
                        "modifiers": {"skulls": 5}}
    assert "'33' isn't 25-1000 in steps of 25; left as it was." in text_of(terminal)


def test_the_status_card_shows_the_camera(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    set_server_setting(server.config_path, ALPHA, "camera", "player_choice")
    def card() -> dict[str, str]:
        sections = dict(statuscard.build(settings, server, {"status": "stopped"}))
        return {title: " | ".join(f"{label} {text}" for label, text, _ in lines) for title, lines in sections.items()}

    assert "Camera player choice" in card()["Settings"]
    ls.write_plays(server.config_path, ALPHA, {"odst": FIREFIGHT})
    assert "Camera (not for co-op)" in card()["Settings"] and "ODST Firefight Crater (Night)" in card()["What it plays"]


# Its console, its joins, its game ending in a new lobby ----------------------------------------------

def test_the_console_cant_change_an_odst_servers_game() -> None:
    for line in ("map Guardian", "MODE Slayer", "load Guardian Slayer", "nextmap Valhalla", "endgame", "endround",
                 "teamcount 2", "shuffle", "team #2 red", "startvote endgame", "startvote", "maps"):
        assert odst.console_refusal(line) == odst.REFUSAL, line
    for line in ("status", "players", "kick #2 spam", "nextmap", "startvote kick #2", "say hi", ""):
        assert odst.console_refusal(line) is None, line


def test_huragok_refuses_them_itself_on_an_odst_server(world, run, caplog) -> None:  # noqa: F811
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        state.connected = True
        manager.server.plays_kind = "odst"
        caplog.set_level(logging.INFO)
        result = await manager.rcon("map Guardian", by="Alice")
        assert not result.ok and result.text == odst.REFUSAL and client.sent == []
        assert "console command not sent (an ODST server plays what its file names)" in caplog.text
        assert (await manager.rcon("players")).ok and client.sent == [("players", ())]

    run(scenario())


def test_the_relay_refuses_them_on_an_odst_server(tmp_path: Path, run) -> None:
    root = tmp_path / "reclaimer"
    port = free_port()
    settings = load(write_root(root, [("Alpha", port)], 'backend = "attach"\n', staff()))
    client = StubClient()
    relay = Relay(settings, {"Alpha": client}, {"Alpha": ServerState("Alpha")}, EventBus())  # type: ignore[arg-type]
    server = settings.servers[0]
    server.plays_kind = "odst"
    session = Session(RelayUser("Alice", "alicealice", "admin"), "Alpha", "127.0.0.1", lambda: None)
    ok, text, _ = run(relay._run_command(session, server, "load", ["Guardian", "Slayer"]))
    assert not ok and text == odst.REFUSAL and not client.sent
    ok, _, _ = run(relay._run_command(session, server, "players"))
    assert ok and client.sent[-1][0] == "players"


def test_odst_joins_count_for_the_watchdog() -> None:
    assert parse_log_line("12:00:01 ODST: player Bravo joined the game in progress and spawns now.") == \
        ("log.joined", {"name": "Bravo", "clock": "12:00:01"})
    assert parse_log_line("Community roster: Bravo joined the server")[0] == "log.joined"


def test_the_console_join_event_is_a_join(run) -> None:
    from huragok.parts.watchdog import JOIN_GRACE, Watchdog

    class Ctx:
        servers = [SERVER]

        def server(self, name: str) -> ServerState:
            return ServerState(name, max_players=16)

    watchdog = Watchdog.__new__(Watchdog)
    watchdog.config = Watchdog.resolve(dict(Watchdog.defaults))
    watchdog.ctx = Ctx()  # type: ignore[assignment]
    run(Watchdog.setup(watchdog))
    now = 1_800_000_000.0
    for n in range(3):
        run(watchdog.connected(Event("log.connected", SERVER, {"name": f"P{n}"}, time=now - JOIN_GRACE - 60 + n)))
    assert "no_joins" in watchdog.findings(SERVER, now)
    run(watchdog.joined(Event("rcon.join", SERVER, {"name": "P0", "number": 1}, time=now - JOIN_GRACE - 30)))
    assert "no_joins" not in watchdog.findings(SERVER, now)
    assert "rcon.join" in Watchdog.joined._part_on  # type: ignore[attr-defined]


def test_an_odst_game_ending_in_a_new_lobby_ends_the_game_for_a_planned_restart(world, run) -> None:  # noqa: F811
    clock, settings, backend, fleet, manager, client, state, seen = world
    lifecycle = Lifecycle(fleet)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        state.players[1] = Player(1, "Alice", "a" * 64)
        state.phase, state.map, state.mode = "playing", "Crater (Night)", "Firefight (Heroic)"
        await lifecycle.restart(SERVER, when="after_game", reason="an update")
        manager.on_phase(Event("rcon.phase", SERVER, {"phase": "no_game", "map": "Crater (Night)",
                                                      "mode": "Firefight (Heroic)"}))
        assert manager.phase == "countdown" and manager.planned is None
        assert lifecycle.cancel(SERVER)

    run(scenario())


# The labels 0.9.19 renamed ------------------------------------------------------------------------------

def test_renamed_labels_are_read_and_written_as_they_are_called_now(tmp_path: Path) -> None:
    character_set = next(o for o in trait_options("player") if o.key == "player.character_set")
    assert character_set.label_of(1) == "Halo 3" and character_set.value_of("Vanilla characters") == 1
    assert character_set.label_of("vanilla characters") == "Halo 3"
    regeneration = next(o for o in trait_options("player") if o.key == "player.grenade_regeneration")
    assert regeneration.label_of(3) == "On, with firebombs" and regeneration.value_of("On with firebombs") == 3
    playlist = tmp_path / "old.playlist.json"
    games = [{"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Vanilla characters"}},
             {"map": "Guardian", "game": "Slayer", "name": "Vanilla characters only",
              "options": {"player.grenade_regeneration": "on with firebombs"}},
             {"map": "Guardian", "game": "Slayer"}]
    playlist.write_text(json.dumps({"name": "old", "games": games}, indent=4), encoding="utf-8")
    assert old_labels(playlist) == ["Vanilla characters", "on with firebombs"]
    assert rename_old_labels(playlist) == [("Vanilla characters", "Halo 3"), ("on with firebombs", "On, with firebombs")]
    data = json.loads(playlist.read_text(encoding="utf-8"))
    assert data["games"][0]["options"]["player.character_set"] == "Halo 3"
    assert data["games"][1]["options"]["player.grenade_regeneration"] == "On, with firebombs"
    assert data["games"][1]["name"] == "Vanilla characters only"  # only whole labels change
    assert playlist.read_text(encoding="utf-8").startswith('{\n    "name": "old"')  # the rest as it was
    assert rename_old_labels(playlist) == [] and old_labels(tmp_path / "missing.json") == []


def test_labels_are_written_as_the_servers_own_program_calls_them(tmp_path: Path) -> None:
    """0.9.18 refuses "Halo 3", 0.9.19 on refuse "Vanilla characters", and no version takes "On with firebombs";
    only option values change, never a game's name or anything else."""
    playlist = tmp_path / "p.playlist.json"
    games = [{"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Halo 3"}},
             {"map": "Guardian", "game": "Slayer", "name": "Vanilla characters",
              "options": {"player.grenade_regeneration": "On with firebombs",
                          "teams.red_character_set": "Vanilla characters"}},
             {"map": "Guardian", "game": "Slayer", "rules": {"note": "Halo 3"}, "options": {"map.weapons": "Halo 3"}}]
    text = json.dumps({"name": "p", "games": games}, indent=4)
    playlist.write_text(text, encoding="utf-8")
    assert rename_old_labels(playlist, "0.9.18") == [("Halo 3", "Vanilla characters"),
                                                     ("On with firebombs", "On, with firebombs")]
    data = json.loads(playlist.read_text(encoding="utf-8"))
    assert data["games"][0]["options"]["player.character_set"] == "Vanilla characters"
    assert data["games"][1]["name"] == "Vanilla characters" and data["games"][2] == games[2]
    assert playlist.read_text(encoding="utf-8").startswith('{\n    "name": "p"')  # the layout as it was
    assert old_labels(playlist, "0.9.18") == [] and rename_old_labels(playlist, "0.9.18") == []
    assert rename_old_labels(playlist, "v0.9.20") == [("Vanilla characters", "Halo 3")] * 2
    playlist.write_text(text, encoding="utf-8")
    assert rename_old_labels(playlist, None) == [("On with firebombs", "On, with firebombs")]  # version not known
    assert '"player.character_set": "Halo 3"' in playlist.read_text(encoding="utf-8")
    assert old_labels(playlist) == ["Vanilla characters"]  # `check`: as the newest program calls them


def test_a_server_starts_with_its_playlists_labels_renamed(world, run, caplog) -> None:  # noqa: F811
    clock, settings, backend, fleet, manager, client, state, seen = world
    folder = manager.server.folder / "playlists"
    folder.mkdir(exist_ok=True)
    (folder / "old.playlist.json").write_text('{"games": [{"options": {"teams.red_character_set": '
                                              '"Vanilla characters"}}]}', encoding="utf-8")
    state.version = "0.9.20"  # what its console said on its last run (the fake has no program file to ask)

    async def scenario() -> None:
        caplog.set_level(logging.INFO)
        fleet.boot()
        await cycle(manager, clock)
        assert backend.starts == [SERVER]

    run(scenario())
    assert '"Halo 3"' in (folder / "old.playlist.json").read_text(encoding="utf-8")
    assert "old.playlist.json: 'Vanilla characters' is now 'Halo 3', as Project Reclaimer 0.9.20 calls them" in \
        caplog.text


def test_check_names_a_playlists_old_labels(tmp_path: Path, capsys, monkeypatch) -> None:
    from huragok import cli, serverupdates

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    (server.folder / "playlists" / "slayer.playlist.json").write_text(
        '{"games": [{"options": {"player.character_set": "Vanilla characters"}}]}', encoding="utf-8")
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, program: "0.9.20")  # the server's own
    cli.main(["-c", str(settings_path), "check"])
    assert ("slayer.playlist.json names 'Vanilla characters', which Project Reclaimer 0.9.20 (this server's program) "
            "calls otherwise") in " ".join(capsys.readouterr().out.split())


def test_vergils_game_commands_are_refused_on_an_odst_server(tmp_path: Path, run) -> None:
    from conftest import OWNER, wait_for
    from test_supervisor import SERVER as SUPERVISED, everyone_joins, running, tells_to

    async def scenario() -> None:
        async with running(tmp_path) as (supervisor, fake, _):
            supervisor.settings.server(SUPERVISED).plays_kind = "odst"
            await everyone_joins(fake)
            await wait_for(lambda: len(supervisor.states[SUPERVISED].players) == 5)
            await fake.chat(OWNER, "Owner", "!map The Pit")
            await wait_for(lambda: any("console cannot change its game" in t for t in tells_to(fake, OWNER)))
            assert not fake.named("map")
            await fake.chat(OWNER, "Owner", "!nextmap")  # only asks: sent
            await wait_for(lambda: fake.named("nextmap"))

    run(scenario())
