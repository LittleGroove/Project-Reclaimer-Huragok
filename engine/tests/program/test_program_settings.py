"""How a server runs, as its dedicated.toml says it (program/settings.py): where its folder settings point."""

from __future__ import annotations

from pathlib import Path

from huragok.program.settings import path_setting


def test_where_a_servers_game_content_and_bans_point(tmp_path: Path) -> None:
    folder = tmp_path / "SERVERS" / "Alpha"
    assert path_setting(folder, {}, "bans") == (folder / "bans.json").resolve()  # the catalogue's defaults
    assert path_setting(folder, {}, "content") == (folder / "content").resolve()
    assert path_setting(folder, {}, "game") == (folder / "game").resolve()
    shared = {"bans": "../../bans.json", "game": "../../game"}  # what [setup] share gave servers up to 0.4.4
    assert path_setting(folder, shared, "bans") == (tmp_path / "bans.json").resolve()
    assert path_setting(folder, shared, "game") == (tmp_path / "game").resolve()
    assert path_setting(folder, {"content": ""}, "content") == (folder / "content").resolve()  # blank: the default
