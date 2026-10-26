"""What a server plays, written into its dedicated.toml, and what each version of the program takes of it
(program/plays.py)."""

from __future__ import annotations

from huragok.program.plays import plays_problem


def test_odsts_newer_keys_name_the_version_they_need() -> None:
    firefight = {"mode": "firefight", "mission": 5000, "insertion": 7, "options": {"lives": 5}}
    assert plays_problem({"odst": firefight}, "0.9.20") == \
        "options needs Project Reclaimer 0.9.21 or newer (this server has 0.9.20)"
    assert plays_problem({"odst": firefight}, "0.9.21") is None and plays_problem({"odst": firefight}, None) is None
    assert "story_characters needs" in plays_problem({"odst": {"mission": 6100, "story_characters": True}}, "0.9.19")
    assert plays_problem({"playlist": "playlists/x.playlist.json", "options": 1}, "0.9.18") is None  # ODST's only


def test_when_a_change_of_what_it_plays_applies() -> None:
    """The program's guide: a new playlist from the next vote, a new map or game from the next game, a new kind (or
    a campaign or ODST change) once nobody is on, when the server restarts by itself."""
    from huragok.program.plays import applies_when

    assert applies_when("playlist", "playlist") == "from the next vote"
    assert applies_when("map", "map") == applies_when("default", "map") == "from the next game"
    assert applies_when("playlist", "map").startswith("once nobody is on") and "kind of game" in applies_when("map", "variant")
    assert "campaign and ODST" in applies_when("campaign", "campaign") and "campaign and ODST" in applies_when("map", "odst")


def test_a_playlist_the_server_cant_play_isnt_used(tmp_path, capsys) -> None:
    """`playlist use` asks the server program first: a playlist it can't play is named, and nothing is written."""
    import json

    from testkit.setup import setup_root

    from huragok.commandline import cli
    from huragok.core.settings import load
    from huragok.core.tomledit import read_toml

    settings_path = setup_root(tmp_path)
    server = load(settings_path).servers[0]
    bad = server.folder / "playlists" / "nowhere.playlist.json"
    bad.write_text(json.dumps({"version": 2, "name": "Nowhere", "games": [
        {"map": "Atlantis", "game": "Slayer"}, {"map": "Guardian", "game": "Slayer"},
        {"map": "Valhalla", "game": "Slayer"}]}), encoding="utf-8")
    before = read_toml(server.config_path)["server"][0]
    assert cli.main(["-c", str(settings_path), "playlist", "use", "nowhere"]) == 1
    said = capsys.readouterr()
    assert "Not used: the server program won't play playlists/nowhere.playlist.json" in said.out + said.err
    assert "Game 1: No map is called \"Atlantis\"" in said.out + said.err
    assert read_toml(server.config_path)["server"][0] == before
    assert cli.main(["-c", str(settings_path), "playlist", "use", "slayer"]) == 0  # one it can play: used
    assert read_toml(server.config_path)["server"][0]["playlist"] == "playlists/slayer.playlist.json"


def test_the_gate_asks_the_servers_own_program_and_counts_only_what_the_change_brings(tmp_path) -> None:
    """Decision 3 (25 October): nothing is written that the server's program would refuse; a problem the server
    has already, and that the change doesn't touch, doesn't hold the change up."""
    from testkit.setup import fake_tool, setup_root

    from huragok.core.settings import load
    from huragok.games.halo3 import Playlist
    from huragok.program.plays import CHECK_FILE, check_plays

    server = load(setup_root(tmp_path)).servers[0]
    tool = fake_tool()
    same = Playlist(server.folder / "playlists" / "x.playlist.json", "X", 20, [
        {"map": "Guardian", "game": "Slayer"}, {"map": "Guardian", "game": "Slayer", "name": "Renamed"},
        {"map": "Valhalla", "game": "Slayer"}])
    check = check_plays(tool, server, {"playlist": same.relative}, same)
    assert [(p.game, p.text.split(" (")[0]) for p in check.problems] == [(2, "the same game as game 1")]  # at once
    missing = Playlist(same.path, "X", 20, [{"map": "Guardian", "game": "Slayer"}, {"map": "Atlantis", "game": "Slayer"},
                                            {"map": "Valhalla", "game": "Slayer"}])
    check = check_plays(tool, server, {"playlist": missing.relative}, missing)
    assert not check.ok and [p.game for p in check.problems] == [2] and "Atlantis" in check.problems[0].text
    assert check_plays(tool, server, {"map": "Atlantis", "game": "Slayer"}).problems[0].text.startswith("Alpha")
    assert check_plays(tool, server, {"map": "Valhalla", "game": "Slayer"}).ok
    # A ban list the server can't read is the server's own problem: the change isn't held up by it, but it can't be
    # checked on its own either (the program stops at the first file it can't read), and the gate says so.
    (server.folder / "bans.json").write_text("{not json", encoding="utf-8")
    check = check_plays(tool, server, {"map": "Valhalla", "game": "Slayer"})
    assert check.ok and not check.asked and "bans.json" in check.unasked and "couldn't be checked" in check.unasked
    assert not (server.folder / CHECK_FILE).exists() and not list(server.folder.rglob("*huragok-check*"))


def test_the_draft_checked_has_no_console_port_or_workshop_items_of_its_own(tmp_path) -> None:
    """The running server holds its own console port, and a check mustn't start SteamCMD fetching a Workshop item:
    the draft's console follows its free game port, and it lists no items."""
    from testkit.setup import fake_tool, setup_root

    from huragok.core.settings import load
    from huragok.core.tomledit import read_toml, set_server_setting, set_table_key
    from huragok.program.plays import CHECK_FILE, check_plays

    server = load(setup_root(tmp_path)).servers[0]
    set_server_setting(server.config_path, server.name, "rcon_port", 49171)
    set_table_key(server.config_path, "workshop", "items", [3014096841])
    tool = fake_tool()
    drafts: list[dict] = []
    real = tool.run_sync

    def run_sync(folder, *args, **options):
        drafts.append(read_toml(folder / CHECK_FILE))
        return real(folder, *args, **options)

    tool.run_sync = run_sync
    assert check_plays(tool, server, {"map": "Valhalla", "game": "Slayer"}).ok
    [block] = drafts[0]["server"]
    assert "rcon_port" not in block and block["map"] == "Valhalla"
    assert "items" not in drafts[0].get("workshop", {})
    assert read_toml(server.config_path)["workshop"]["items"] == [3014096841]  # the server's own file untouched
