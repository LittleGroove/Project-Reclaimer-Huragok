"""G -> P, a server's playlists by player count (window/screens/byplayers.py): set up a step at a time, each playlist
checked by the server's own program; warnings, never refusals, for what max_players never reaches; on and off; and U
asking first while it's on."""

from __future__ import annotations

import json
from pathlib import Path

from testkit.setup import SetupController, StrictKeyboard, fake_tool, setup_root
from testkit.window import FakeTerminal

from huragok.core.logs import RecentLog
from huragok.core.settings import load
from huragok.core.tomledit import read_toml
from huragok.games import byplayers as bp
from huragok.window.dashboard import Menu
from huragok.window.screens.byplayers import live_count


def root_with_playlists(tmp_path: Path) -> tuple[Path, Path]:
    settings_path = setup_root(tmp_path)
    folder = settings_path.parent / "SERVERS" / "Alpha Server"
    slayer = json.loads((folder / "playlists" / "slayer.playlist.json").read_text(encoding="utf-8"))
    for name in ("small", "medium", "big"):
        (folder / "playlists" / f"{name}.playlist.json").write_text(json.dumps({**slayer, "name": name.title()}),
                                                                    encoding="utf-8")
    return settings_path, folder  # playlists/, sorted: big, medium, slayer, small


def screen(settings_path: Path, keys: list[str]) -> tuple[Menu, FakeTerminal]:
    terminal = FakeTerminal()
    return Menu(SetupController(settings_path), terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


def playing(folder: Path) -> str:
    return read_toml(folder / "dedicated.toml")["server"][0]["playlist"]


def test_setting_it_up_a_playlist_and_a_cap_at_a_time_then_off_puts_the_default_back(tmp_path: Path, run) -> None:
    settings_path, folder = root_with_playlists(tmp_path)
    server = load(settings_path).servers[0]
    keys = ["S",
            "4", "ENTER", "1", "0", "ENTER",  # small, up to 10
            "2", "ENTER", "5", "ENTER",  # medium: 5 isn't above 10, asked again...
            "5", "0", "ENTER",  # ...up to 50
            "1", "ENTER", "ENTER",  # big, no cap: the last
            "Y", "ENTER",  # save it and turn it on
            "O", "ENTER",  # off again: the default back
            "ESC"]
    menu, terminal = screen(settings_path, keys)
    try:
        run(menu._by_players(load(settings_path), fake_tool(), server, {"name": server.name, "players": ""}))
    except RuntimeError as error:
        raise AssertionError(f"{error}\n{''.join(terminal.output)}") from None
    printed = "".join(terminal.output)
    assert "not set up: it plays one playlist" in printed
    assert "'5' isn't a number of 11 or more (above the cap before, 10)" in printed
    assert "up to 10 players" in printed and "11 to 50 players" in printed and "51 players and up" in printed
    # max_players is 16 here: warned of, not refused
    assert "playlist 3 (big) never plays while max_players is 16: it starts at 51 players" in printed
    assert "It isn't running, so it starts on small (for up to 10 players): from the next vote" in printed
    setup = bp.setup_of(folder, server.name)
    assert setup is not None and not setup.on  # turned off at the end
    assert [(s.playlist, s.up_to) for s in setup.steps] == [("playlists/small.playlist.json", 10),
                                                             ("playlists/medium.playlist.json", 50),
                                                             ("playlists/big.playlist.json", None)]
    assert setup.default == "playlists/slayer.playlist.json"  # what it played before it was set up
    assert "It plays the default, slayer, from the next vote." in printed
    assert playing(folder) == "playlists/slayer.playlist.json"


def test_the_first_playlist_needs_a_cap_and_esc_saves_nothing(tmp_path: Path, run) -> None:
    settings_path, folder = root_with_playlists(tmp_path)
    server = load(settings_path).servers[0]
    menu, terminal = screen(settings_path, ["S", "4", "ENTER", "ENTER", "ESC", "ESC"])
    run(menu._by_players(load(settings_path), fake_tool(), server, {"name": server.name, "players": ""}))
    printed = "".join(terminal.output)
    assert "The first playlist needs a cap" in printed and "Nothing saved." in printed
    assert bp.setup_of(folder, server.name) is None and playing(folder) == "playlists/slayer.playlist.json"


def test_using_a_playlist_while_its_on_asks_first(tmp_path: Path, run) -> None:
    settings_path, folder = root_with_playlists(tmp_path)
    server = load(settings_path).servers[0]
    bp.save(folder, bp.Setup(server.name, True, "playlists/slayer.playlist.json", 30, [
        bp.Step("playlists/small.playlist.json", 10), bp.Step("playlists/big.playlist.json")]))
    menu, terminal = screen(settings_path, ["D"])  # make it the default
    assert run(menu._by_players_before_use(server, "playlists/medium.playlist.json")) is False
    setup = bp.setup_of(folder, server.name)
    assert setup.on and setup.default == "playlists/medium.playlist.json"
    menu, terminal = screen(settings_path, ["O"])  # or turn it off and play it
    assert run(menu._by_players_before_use(server, "playlists/big.playlist.json")) is True
    assert not bp.setup_of(folder, server.name).on
    bp.save(folder, setup)  # on again
    menu, terminal = screen(settings_path, [])  # a campaign or one map picked: it goes off, nothing asked
    assert run(menu._by_players_before_use(server, None)) is True
    assert not bp.setup_of(folder, server.name).on
    assert "Playlists by player count turned off" in "".join(terminal.output)


def test_the_count_comes_from_the_dashboards_row() -> None:
    assert live_count({"players": "12/16"}) == 12
    assert live_count({"players": "0"}) == 0
    assert live_count({"players": ""}) is None and live_count({}) is None
