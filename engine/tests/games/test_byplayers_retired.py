"""Playlists by player count, retired in 0.7.0: a host's old playlists-by-players.toml is left where it is, and its
steps are written as the server's own `playlists = [...]` (Project Reclaimer 0.10.1), for the host to paste in. Said
once per server as Huragok starts, on its status card, and by `start.cmd check`."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from testkit.setup import setup_root

from huragok.core.settings import load
from huragok.games.byplayers_retired import FILE, native_playlists, retired_warning

OLD = """\
# Playlists by player count (Huragok): which playlist each server here plays for how many players are on.
[[server]]
name = "Alpha Server"
on = true
default = "playlists/slayer.playlist.json"
wait_seconds = 30
margin = 2

[[server.step]]
playlist = "playlists/small.playlist.json"
up_to = 10

[[server.step]]
playlist = "playlists/medium.playlist.json"
up_to = 50

[[server.step]]
playlist = "playlists/big.playlist.json"

[[server]]
name = "Bravo"
on = false

[[server.step]]
playlist = "playlists/one.playlist.json"
up_to = 1

[[server.step]]
playlist = "playlists/two.playlist.json"
up_to = 1

[[server.step]]
playlist = "playlists/everyone.playlist.json"
"""


def test_the_steps_become_the_servers_own_playlists_setting(tmp_path: Path) -> None:
    (tmp_path / FILE).write_text(OLD, encoding="utf-8")
    native = native_playlists(tmp_path, "alpha server")  # by name, as its dedicated.toml names it, any case
    assert native is not None and native.on and native.notes == []
    assert native.lines == [
        "playlists = [",
        '  { file = "playlists/small.playlist.json", players = "1-10" },',
        '  { file = "playlists/medium.playlist.json", players = "11-50" },',
        '  { file = "playlists/big.playlist.json", players = "51+" },',
        "]"]
    # TOML the server reads: 1 to 16 entries of {file, players}, players a lobby size ("8", "8-20", "12+").
    pasted = tomllib.loads("\n".join(native.lines))
    assert pasted == {"playlists": [{"file": "playlists/small.playlist.json", "players": "1-10"},
                                    {"file": "playlists/medium.playlist.json", "players": "11-50"},
                                    {"file": "playlists/big.playlist.json", "players": "51+"}]}
    # A step no lobby reaches (its cap isn't above the one before) is left out, and said.
    bravo = native_playlists(tmp_path, "Bravo")
    assert bravo is not None and not bravo.on
    assert tomllib.loads("\n".join(bravo.lines))["playlists"] == [
        {"file": "playlists/one.playlist.json", "players": "1"},
        {"file": "playlists/everyone.playlist.json", "players": "2+"}]
    assert bravo.notes == ["playlists/two.playlist.json (step 2) is for no lobby the server has (1 to 128 players): "
                           "left out"]
    assert native_playlists(tmp_path, "Charlie") is None  # no block: nothing to say
    assert native_playlists(tmp_path / "elsewhere", "Alpha Server") is None  # no file
    assert (tmp_path / FILE).read_text(encoding="utf-8") == OLD  # read, never written


def test_what_the_server_cant_take_is_left_out_and_said(tmp_path: Path) -> None:
    steps = "".join(f'[[server.step]]\nplaylist = "playlists/p{n}.playlist.json"\nup_to = {n * 5}\n\n'
                    for n in range(1, 18))
    (tmp_path / FILE).write_text(f'[[server]]\nname = "Many"\non = true\n\n{steps}'
                                 '[[server.step]]\nplaylist = "playlists/last.playlist.json"\n\n'
                                 '[[server.step]]\nplaylist = "playlists/never.playlist.json"\n', encoding="utf-8")
    native = native_playlists(tmp_path, "Many")
    assert native is not None
    entries = tomllib.loads("\n".join(native.lines))["playlists"]
    assert len(entries) == 16 and entries[0]["players"] == "1-5" and entries[-1]["players"] == "76-80"
    assert "the server takes 16 playlists at most: the last 2 are left out" in native.notes
    assert "the steps after step 18 never played (it has no cap): left out" in native.notes
    (tmp_path / FILE).write_text('[[server]]\nname = "Huge"\n[[server.step]]\nplaylist = "a.playlist.json"\n'
                                 'up_to = 200\n[[server.step]]\nplaylist = "b.playlist.json"\n', encoding="utf-8")
    huge = native_playlists(tmp_path, "Huge")
    assert huge is not None and tomllib.loads("\n".join(huge.lines))["playlists"] == [
        {"file": "a.playlist.json", "players": "1-128"}]  # 128 is the most a lobby holds
    assert huge.notes == ["b.playlist.json (step 2) is for no lobby the server has (1 to 128 players): left out"]


def test_the_warning_says_huragok_no_longer_switches_with_the_lines(tmp_path: Path) -> None:
    (tmp_path / FILE).write_text(OLD, encoding="utf-8")
    lines = retired_warning(tmp_path, "Alpha Server")
    assert lines[0].startswith("Huragok no longer switches playlists by player count (0.7.0; it was on for this "
                               "server). The server does it itself from Project Reclaimer 0.10.1")
    assert "in place of `playlist = ...`" in lines[0] and f"({FILE} is left as it is, and no longer read)" in lines[0]
    assert lines[1:] == native_playlists(tmp_path, "Alpha Server").lines
    assert "it was off for this server" in retired_warning(tmp_path, "Bravo")[0]
    assert retired_warning(tmp_path, "Charlie") == []
    (tmp_path / FILE).write_text("not = [toml", encoding="utf-8")
    broken = retired_warning(tmp_path, "Alpha Server")
    assert len(broken) == 1 and f"{FILE} can't be read" in broken[0] and "playlists = [...]" in broken[0]


def test_each_server_with_old_steps_is_told_at_start_on_its_card_and_by_check(tmp_path: Path, caplog, capsys) -> None:
    from huragok.commandline import cli
    from huragok.superintendent import _warn_by_players
    from huragok.window import statuscard

    settings_path = setup_root(tmp_path)
    settings = load(settings_path)
    server = settings.servers[0]
    (server.folder / FILE).write_text(OLD, encoding="utf-8")
    before = server.config_path.read_text(encoding="utf-8")
    caplog.set_level(logging.WARNING)
    _warn_by_players(server)  # as the Superintendent takes each server on
    assert caplog.text.count("Huragok no longer switches playlists") == 1
    assert '{ file = "playlists/big.playlist.json", players = "51+" },' in caplog.text
    card = dict(statuscard.build(settings, server, {"status": "stopped"}))
    notes = [text for _, text, _ in card["Problems and notes"]]
    assert notes[0].startswith("Huragok no longer switches playlists") and notes[1] == "playlists = ["
    assert all(label != "By players" for label, _, _ in card["What it plays"])
    cli.main(["-c", str(settings_path), "check"])
    out = " ".join(capsys.readouterr().out.split())
    assert "warning: Huragok no longer switches playlists by player count" in out
    assert '{ file = "playlists/small.playlist.json", players = "1-10" },' in out
    assert server.config_path.read_text(encoding="utf-8") == before  # nothing of the server's is changed
    assert (server.folder / FILE).read_text(encoding="utf-8") == OLD
