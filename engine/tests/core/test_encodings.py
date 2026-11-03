"""TOML files as Windows editors save them (audit 2026-11, 4.1): one server's dedicated.toml in ANSI (Notepad++ on an
ASCII file, Notepad on Windows Server) stopped Huragok for every server with a traceback, and the start loop restarted
it for good. Now that folder is left out and says why; a BOM or Notepad's "Unicode" is read as the text it is."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import free_ports, write_root

from huragok.core.commandsfile import PermissionsError, load_permissions
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_server_setting


def two_servers(tmp_path: Path) -> Path:
    return write_root(tmp_path / "reclaimer", list(zip(("Alpha", "Beta"), free_ports(2))), "")


def test_one_dedicated_toml_in_ansi_leaves_that_folder_out_not_every_server(tmp_path: Path) -> None:
    settings_path = two_servers(tmp_path)
    beta = settings_path.parent / "SERVERS" / "Beta" / "dedicated.toml"
    beta.write_bytes(beta.read_text(encoding="utf-8").replace('name = "Beta"', 'name = "Beta Café"').encode("cp1252"))
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == ["Alpha"]
    assert any("isn't saved as UTF-8" in str(problem) for problem in settings.problems)


def test_a_server_entry_that_isnt_a_block_leaves_that_folder_out(tmp_path: Path) -> None:
    settings_path = two_servers(tmp_path)
    beta = settings_path.parent / "SERVERS" / "Beta" / "dedicated.toml"
    text = beta.read_text(encoding="utf-8")
    beta.write_text('server = ["Beta"]\n' + text[:text.index("[[server]]")], encoding="utf-8")  # at the top
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == ["Alpha"]
    assert any("isn't a [[server]] block" in str(problem) for problem in settings.problems)


def test_a_bom_or_notepads_unicode_is_read_and_edited_as_the_text_it_is(tmp_path: Path) -> None:
    settings_path = two_servers(tmp_path)
    settings_path.write_bytes(b"\xef\xbb\xbf" + settings_path.read_bytes())
    alpha = settings_path.parent / "SERVERS" / "Alpha" / "dedicated.toml"
    alpha.write_bytes(alpha.read_text(encoding="utf-8").encode("utf-16"))
    settings = load(settings_path)
    assert [s.name for s in settings.servers] == ["Alpha", "Beta"]
    set_server_setting(alpha, "Alpha", "max_players", 12)  # written back as UTF-8
    assert read_toml(alpha)["server"][0]["max_players"] == 12


def test_commands_toml_in_ansi_says_to_save_it_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    path.write_bytes('# Café rules\nprefix = "!"\n'.encode("cp1252"))
    with pytest.raises(PermissionsError, match="save it as UTF-8"):
        load_permissions(path)


def test_a_change_for_a_server_renamed_by_hand_adds_no_phantom_server(tmp_path: Path) -> None:
    """D, G's playlists and playlists by player count wrote to the name they knew: after a rename by hand that added
    a [[server]] block with the old name, which Huragok then listed as a new server on port 0 (audit 2026-11, 4.4)."""
    from huragok.core.tomledit import TomlEditError
    from huragok.program.plays import write_plays
    from huragok.program.settings import find, write

    config = tmp_path / "dedicated.toml"
    config.write_text('[[server]]\nname = "Slayer EU"\nport = 46300\n', encoding="utf-8")
    before = config.read_text(encoding="utf-8")
    with pytest.raises(TomlEditError, match=r"no \[\[server\]\] block named Slayer now"):
        write(config, "Slayer", find("max_players", "server"), 12)
    with pytest.raises(TomlEditError):
        write_plays(config, "Slayer", {"playlist": "playlists/big.playlist.json"})
    assert config.read_text(encoding="utf-8") == before
    settings_toml = tmp_path / "settings.toml"
    settings_toml.write_text("", encoding="utf-8")
    set_server_setting(settings_toml, "Slayer", "autostart", False)  # Huragok's own blocks are still added
    assert read_toml(settings_toml)["server"] == [{"name": "Slayer", "autostart": False}]
