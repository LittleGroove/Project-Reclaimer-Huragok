"""Project Reclaimer 0.10: the pieces that tie the 0.10 work together."""

from __future__ import annotations

import pytest

from huragok.program.dedicated import ToolResult


@pytest.mark.parametrize(("text", "problem"), [
    ("Configuration  dedicated.toml\nNot ready:\n  Alpha: game 2: no multiplayer map \"Valhala\" is installed\n",
     'Alpha: game 2: no multiplayer map "Valhala" is installed'),
    ('[[server]] "Alpha": launch_timeout: 0 to 300 seconds\n', '[[server]] "Alpha": launch_timeout: 0 to 300 seconds'),
    ('Configuration  dedicated.toml\n"Alpha": max_players is 1 through 128\n', '"Alpha": max_players is 1 through 128'),
    ("Skipped content file H:/a b/font.bin: Expected an MCC BLF variant file\ndedicated.toml: invalid type\n",
     "dedicated.toml: invalid type"),
    ("Error: the 0.9 way\nmore\n", "Error: the 0.9 way"),
    ("just one line\n", "just one line"),
    ("", "exit code 1"),
])
def test_a_refusal_is_named_by_its_own_line(text: str, problem: str) -> None:
    assert ToolResult(False, 1, text).problem == problem


def test_a_010_servers_reports_go_to_010s_stats_service(tmp_path) -> None:
    from huragok.servers.statsrelay import last_reported_to, program_in_log, service_for

    log = tmp_path / "server.log"
    log.write_text("[host] Project Reclaimer 0.10.0 (rcl-engine)\n"
                   "[stats] reporting to http://203.0.113.9:8792 as cFzcSeC8Ny760SJyd2z2rB/w53wN=\n", encoding="utf-8")
    assert program_in_log(log) == "0.10.0"
    assert last_reported_to(log, set()) == "http://203.0.113.9:8792"
    assert service_for("http://148.251.153.44:8790", "0.10.0") == "http://148.251.153.44:8792"
    assert service_for("http://148.251.153.44:8790/", "0.9.23") == "http://148.251.153.44:8790/"
    assert service_for("http://148.251.153.44:8790", None) == "http://148.251.153.44:8790"
    assert service_for("http://stats.example:8790", "0.10.0") == "http://stats.example:8790"
    assert program_in_log(tmp_path / "missing.log") is None


def test_a_mods_font_packages_skipped_by_the_server_are_no_warning() -> None:
    from huragok.content.mods import is_font_package

    assert is_font_package(r"H:\SteamLibrary\steamapps\workshop\content\976730\2898916793\halo3\maps\fonts\font_package_x3.bin")
    assert is_font_package("content/Mods/Workshop/3830420359/halo3/maps/fonts/font_package_icon.bin")
    assert not is_font_package("content/Game Modes/Race.bin")
    assert not is_font_package("content/Mods/a/fonts/readme.bin")


def test_reachs_variants_come_along_with_its_files(tmp_path) -> None:
    from huragok.games.files import REACH_VARIANTS, add_reach_variants

    mcc, game = tmp_path / "mcc", tmp_path / "game"
    for folder in REACH_VARIANTS:
        (mcc / "haloreach" / folder).mkdir(parents=True)
        (mcc / "haloreach" / folder / f"{folder}_054.bin").write_bytes(b"_blf")
    assert add_reach_variants(game, mcc) == 0  # no Reach in the game folder: nothing to add to
    (game / "haloreach").mkdir(parents=True)
    (game / "haloreach" / "game_variants").mkdir()
    (game / "haloreach" / "game_variants" / "game_variants_054.bin").write_bytes(b"mine")
    assert add_reach_variants(game, mcc) == 3  # what's there stays as it is
    assert (game / "haloreach" / "game_variants" / "game_variants_054.bin").read_bytes() == b"mine"
    assert (game / "haloreach" / "hopper_game_variants" / "hopper_game_variants_054.bin").is_file()
    assert add_reach_variants(game, mcc) == 0


def test_a_game_folder_with_reach_but_not_its_variants_gets_them_before_the_start(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from huragok import steam
    from huragok.content.gamefiles import fix_reach_variants

    mcc, game = tmp_path / "mcc", tmp_path / "server" / "game"
    (mcc / "haloreach" / "hopper_game_variants").mkdir(parents=True)
    (mcc / "haloreach" / "hopper_game_variants" / "headhunter_054.bin").write_bytes(b"_blf")
    (game / "haloreach").mkdir(parents=True)
    monkeypatch.setattr(steam, "find_mcc", lambda folder=None: mcc)
    settings = SimpleNamespace(setup=SimpleNamespace(mcc_folder=""))
    paths = SimpleNamespace(game=game)
    assert fix_reach_variants(settings, paths) is None  # no Reach here
    (game / "haloreach" / "haloreach.dll").write_bytes(b"MZ")
    said = fix_reach_variants(settings, paths)
    assert said and "Reach's game and map variants" in said and "(1 files)" in said
    assert (game / "haloreach" / "hopper_game_variants" / "headhunter_054.bin").is_file()
    assert fix_reach_variants(settings, paths) is None  # once is enough
