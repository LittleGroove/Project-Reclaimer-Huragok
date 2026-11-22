"""Project Reclaimer 0.9.19 and 0.9.20: Halo 3: ODST servers (what they play, their files, their console, their game
ending in a new lobby), the camera setting, campaign sprint, and the game option labels 0.9.19 renamed."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from conftest import free_port, staff, wait_for, write_root
from testkit import DATA
from testkit.fleet import SERVER, cycle, world
from testkit.manage import StubClient
from testkit.mcc import fake_mcc
from testkit.screens import text_of
from testkit.setup import ALPHA, SetupController, fake_tool, setup_root, write_plays
from testkit.upkeep import menu_on

from huragok.content import upkeep
from huragok.content.catalogue import parse_listing
from huragok.content import gamefiles
from huragok.content.newserver import add_odst_files
from huragok.core.events import Event, EventBus
from huragok.core.settings import RelayUser, load
from huragok.core.tomledit import read_toml, set_server_setting
from huragok.games import odst
from huragok.games import files as game_files
from huragok.games.files import has_odst_files
from huragok.games.halo3 import label_fixes, old_labels
from huragok.players.relay import Relay, Session
from huragok.program import settings as ls
from huragok.servers.fleet import Lifecycle
from huragok.servers.logs import parse_log_line
from huragok.servers.state import Player, ServerState
from huragok.window import statuscard

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
    write_plays(server.config_path, ALPHA, {"odst": FIREFIGHT})
    reread = load(settings_path).servers[0]
    assert reread.plays == "ODST Firefight Crater (Night), Ff Classic (heroic)" and reread.plays_kind == "odst"
    write_plays(server.config_path, ALPHA, {"playlist": "playlists/slayer.playlist.json"})
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


def test_an_odst_server_without_odsts_files_waits_and_repair_adds_them_from_mcc(tmp_path: Path) -> None:
    """0.7.0: nothing is got by itself. An ODST server whose game folder lacks ODST's files waits, warned, and
    `repair` (when the host asks) copies them from MCC beside the Halo 3 it has."""
    mcc = odst_mcc(tmp_path / "MCC")
    settings_path = setup_root(tmp_path, extra=f"[setup]\nmcc_folder = {json.dumps(str(mcc))}\n")
    server = load(settings_path).servers[0]
    write_plays(server.config_path, ALPHA, {"odst": FIREFIGHT, "max_players": 4})
    settings = load(settings_path)
    server = settings.servers[0]
    files = gamefiles.GameFiles(settings, EventBus(), fake_tool)
    wait = files.prelaunch(server) or ""
    assert "Halo 3: ODST's files" in wait and "press I" in wait and not files.tasks  # held, nothing got by itself
    plan = gamefiles.repairs(settings, server, fake_tool, lambda line: None)
    odst_repair = next(r for r in plan if r.text.startswith("Halo 3: ODST's files into"))
    assert "copying them from MCC" in odst_repair.text and not odst_repair.writes  # beside its Halo 3: no path change
    done, failed = gamefiles.run_repairs(settings, server, plan, lambda line: None)
    assert failed == [] and has_odst_files(server.folder / "game")
    assert any(line.startswith("Halo 3: ODST's files are in") for line in done)
    assert "ODST" not in (files.prelaunch(server) or "")  # what this MCC has is in (its campaign it lacks)


def test_the_engine_files_after_an_mcc_update_include_odsts(tmp_path: Path) -> None:
    assert game_files.engine_out_of_date("Error: This halo3odst.dll is not the ODST build this release supports.")
    assert game_files.odst_files_missing("Error: Server 'X': Halo 3: ODST's files are not in the game folder C:/g: …")
    mcc, game = odst_mcc(tmp_path / "mcc"), tmp_path / "game"
    for folder in ("halo3", "halo3odst"):
        (game / folder).mkdir(parents=True)
    (game / "halo3" / "halo3.dll").write_bytes(b"old")
    (game / "halo3odst" / "halo3odst.dll").write_bytes(b"old odst")
    text = upkeep.copy_engine(game, mcc)
    assert "halo3odst.dll" in text and (game / "halo3odst" / "halo3odst.dll").read_bytes() == b"odst dll"
    assert [p.read_bytes() for p in (game / "halo3odst").glob("halo3odst.dll.before-*")] == [b"old odst"]
    assert game_files.engine_files(tmp_path / "nothing") == ["halo3/halo3.dll"]


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
    monkeypatch.setattr("huragok.window.screens.setup.odst_mcc", lambda hint=None: None)
    assert not run(menu._offer_odst_files(controller.settings, server.folder / "game"))
    assert "No MCC with Halo 3: ODST installed was found" in text_of(terminal)


def test_the_status_card_shows_the_camera(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    set_server_setting(server.config_path, ALPHA, "camera", "player_choice")
    def card() -> dict[str, str]:
        sections = dict(statuscard.build(settings, server, {"status": "stopped"}))
        return {title: " | ".join(f"{label} {text}" for label, text, _ in lines) for title, lines in sections.items()}

    assert "Camera player choice" in card()["Settings"]
    write_plays(server.config_path, ALPHA, {"odst": FIREFIGHT})
    assert "Camera (not for co-op)" in card()["Settings"] and "ODST Firefight Crater (Night)" in card()["What it plays"]


# Its console, its joins, its game ending in a new lobby ----------------------------------------------

def test_the_console_cant_change_an_odst_servers_game() -> None:
    for line in ("map Guardian", "MODE Slayer", "load Guardian Slayer", "nextmap Valhalla", "endgame", "endround",
                 "teamcount 2", "shuffle", "team #2 red", "startvote endgame", "startvote", "maps"):
        assert odst.console_refusal(line) == odst.REFUSAL, line
    for line in ("status", "players", "kick #2 spam", "nextmap", "startvote kick #2", "say hi", ""):
        assert odst.console_refusal(line) is None, line
    for version in ("0.9.20", "v0.9.19"):  # the lobby that queued a Halo 3 game: refused here, as when not known
        assert odst.console_refusal("map Guardian", version) == odst.REFUSAL, version


def test_from_0_9_21_an_odst_servers_console_answers_for_itself() -> None:
    """0.9.21 changes the mission, Firefight map and type from the console and refuses the rest in its own words."""
    for line in ("map 5000", "mode firefight", "load 6100 odst_ff_classic", "nextmap 6110", "endgame",
                 "startvote endgame", "maps", "modes", "teamcount 2", "endround"):
        for version in ("0.9.21", "0.9.23", "v1.0.0"):
            assert odst.console_refusal(line, version) is None, (line, version)


def test_huragok_refuses_them_itself_on_an_odst_server(world, run, caplog) -> None:  # noqa: F811
    clock, settings, backend, fleet, manager, client, state, seen = world

    async def scenario() -> None:
        state.connected = True
        manager.server.plays_kind = "odst"
        caplog.set_level(logging.INFO)
        result = await manager.rcon("map Guardian", by="Alice")
        assert not result.ok and result.text == odst.REFUSAL and client.sent == []
        assert "console command not sent (an ODST server before 0.9.21 plays what its file names)" in caplog.text
        assert (await manager.rcon("players")).ok and client.sent == [("players", ())]
        state.version = "0.9.21"  # what the server reports: from here its console answers for itself
        assert (await manager.rcon("map 5000", by="Alice")).ok and client.sent[-1][0] == "map 5000"

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
    from huragok.servers.watchdog import JOIN_GRACE, Watchdog

    class Ctx:
        servers = [SERVER]

        def server(self, name: str) -> ServerState:
            return ServerState(name, max_players=16)

        def run_started(self, name: str) -> float | None:
            return None  # not known: settled

        def restart_setting(self, name: str, key: str) -> float:
            return 900.0

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

def test_renamed_labels_are_found_and_the_file_left_as_it_is(tmp_path: Path) -> None:
    """Huragok no longer rewrites a playlist's labels (0.7.0): it names the ones the server's program refuses, and
    which it takes instead, and the file stays exactly as the host wrote it."""
    playlist = tmp_path / "old.playlist.json"
    games = [{"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Vanilla characters"}},
             {"map": "Guardian", "game": "Slayer", "name": "Vanilla characters only",
              "options": {"player.grenade_regeneration": "on with firebombs"}},
             {"map": "Guardian", "game": "Slayer"}]
    text = json.dumps({"name": "old", "games": games}, indent=4)
    playlist.write_text(text, encoding="utf-8")
    assert old_labels(playlist) == ["Vanilla characters", "on with firebombs"]
    assert label_fixes(playlist) == [("player.character_set", "Vanilla characters", "Halo 3"),
                                     ("player.grenade_regeneration", "on with firebombs", "On, with firebombs")]
    assert playlist.read_text(encoding="utf-8") == text  # read only
    assert old_labels(tmp_path / "missing.json") == [] and label_fixes(tmp_path / "missing.json") == []


def test_labels_are_judged_as_the_servers_own_program_calls_them(tmp_path: Path) -> None:
    """0.9.18 refuses "Halo 3", 0.9.19 on refuse "Vanilla characters", and no version takes "On with firebombs";
    only option values count, never a game's name or anything else."""
    playlist = tmp_path / "p.playlist.json"
    games = [{"map": "Guardian", "game": "Slayer", "options": {"player.character_set": "Halo 3"}},
             {"map": "Guardian", "game": "Slayer", "name": "Vanilla characters",
              "options": {"player.grenade_regeneration": "On with firebombs",
                          "teams.red_character_set": "Vanilla characters"}},
             {"map": "Guardian", "game": "Slayer", "rules": {"note": "Halo 3"}, "options": {"map.weapons": "Halo 3"}}]
    text = json.dumps({"name": "p", "games": games}, indent=4)
    playlist.write_text(text, encoding="utf-8")
    assert [(written, taken) for _, written, taken in label_fixes(playlist, "0.9.18")] == [
        ("Halo 3", "Vanilla characters"), ("On with firebombs", "On, with firebombs")]
    assert old_labels(playlist, "v0.9.20") == ["On with firebombs", "Vanilla characters"]
    assert old_labels(playlist, None) == ["On with firebombs"]  # version not known: only what no version takes
    assert old_labels(playlist) == ["On with firebombs", "Vanilla characters"]  # `check`: as the newest calls them
    assert playlist.read_text(encoding="utf-8") == text


def test_a_server_starts_with_its_playlists_as_they_are(world, run, caplog) -> None:  # noqa: F811
    """A label its program refuses is the host's to change: the start never rewrites a playlist (0.7.0)."""
    clock, settings, backend, fleet, manager, client, state, seen = world
    folder = manager.server.folder / "playlists"
    folder.mkdir(exist_ok=True)
    text = '{"games": [{"options": {"teams.red_character_set": "Vanilla characters"}}]}'
    (folder / "old.playlist.json").write_text(text, encoding="utf-8")
    state.version = "0.9.20"

    async def scenario() -> None:
        caplog.set_level(logging.INFO)
        fleet.boot()
        await cycle(manager, clock)
        await wait_for(lambda: backend.starts == [SERVER], timeout=15)

    run(scenario())
    assert (folder / "old.playlist.json").read_text(encoding="utf-8") == text
    assert "is now 'Halo 3'" not in caplog.text


def test_check_names_a_playlists_old_labels(tmp_path: Path, capsys, monkeypatch) -> None:
    from huragok.commandline import cli
    from huragok.program import releases as serverupdates

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    (server.folder / "playlists" / "slayer.playlist.json").write_text(
        '{"games": [{"options": {"player.character_set": "Vanilla characters"}}]}', encoding="utf-8")
    monkeypatch.setattr(serverupdates, "program_version", lambda settings, program: "0.9.20")  # the server's own
    cli.main(["-c", str(settings_path), "check"])
    text = " ".join(capsys.readouterr().out.split())
    assert ("slayer.playlist.json names 'Vanilla characters', which Project Reclaimer 0.9.20 (this server's program) "
            "calls otherwise, so it won't play that playlist: write 'Halo 3' instead (the window's O opens it)") in text
    assert "Vanilla characters" in (server.folder / "playlists" / "slayer.playlist.json").read_text(encoding="utf-8")


def test_vergils_game_commands_are_refused_on_an_odst_server(tmp_path: Path, run) -> None:
    from conftest import FACULTY, wait_for
    from testkit.running import SERVER as SUPERVISED, everyone_joins, running, tells_to

    async def scenario() -> None:
        async with running(tmp_path) as (supervisor, fake, _):
            supervisor.settings.server(SUPERVISED).plays_kind = "odst"
            await everyone_joins(fake)
            await wait_for(lambda: len(supervisor.superintendent.states[SUPERVISED].players) == 5)
            await fake.chat(FACULTY, "Faculty", "!map The Pit")
            await wait_for(lambda: any("console cannot change its game" in t for t in tells_to(fake, FACULTY)))
            assert not fake.named("map")
            await fake.chat(FACULTY, "Faculty", "!nextmap")  # only asks: sent
            await wait_for(lambda: fake.named("nextmap"))

    run(scenario())
