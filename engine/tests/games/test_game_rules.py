"""What each game is (games/): the files a server needs for what it plays, the console commands an ODST server
refuses, and co-op. Each rule lives in one place, and everything that once asked `plays_kind == "odst"` asks it."""

from __future__ import annotations

from pathlib import Path

import pytest

from huragok.games import files, plays
from huragok.games.odst import REFUSAL


def game_folder(root: Path, *kinds: str) -> Path:
    """A game folder holding the files of each kind given, as the server program checks them."""
    if "halo3" in kinds:
        (root / "halo3" / "maps").mkdir(parents=True)
        (root / "halo3" / "halo3.dll").write_bytes(b"")
        (root / "halo3" / "maps" / "shared.map").write_bytes(b"")
    if "campaign" in kinds:
        for name in ("campaign", "010_jungle", "040_voi"):
            (root / "halo3" / "maps" / f"{name}.map").write_bytes(b"")
    if "odst" in kinds:
        (root / "halo3odst" / "maps").mkdir(parents=True)
        (root / "halo3odst" / "halo3odst.dll").write_bytes(b"")
        (root / "halo3odst" / "maps" / "shared.map").write_bytes(b"")
    return root


@pytest.mark.parametrize("plays_kind, needed", [
    ("playlist", ["halo3"]), ("map", ["halo3"]), ("default", ["halo3"]),
    ("campaign", ["halo3", "campaign"]), ("odst", ["halo3", "odst"]),
])
def test_every_server_needs_halo_3s_files_and_some_more(plays_kind: str, needed: list[str]) -> None:
    assert files.needed(plays_kind) == needed
    assert set(needed) <= set(files.DEPOTS) == set(files.LABELS) == set(files.MAPS_FOR)


def test_what_a_game_folder_lacks_comes_halo_3s_first(tmp_path: Path) -> None:
    assert files.missing(tmp_path / "empty", "odst") == ["halo3"]  # the others go beside Halo 3's
    halo3 = game_folder(tmp_path / "h3", "halo3")
    assert files.missing(halo3, "playlist") == []
    assert files.missing(halo3, "odst") == ["odst"] and files.missing(halo3, "campaign") == ["campaign"]
    everything = game_folder(tmp_path / "all", "halo3", "campaign", "odst")
    assert all(files.has(everything, kind) for kind in files.DEPOTS)
    assert files.engine_files(everything) == ["halo3/halo3.dll", "halo3odst/halo3odst.dll"]
    assert files.engine_files(halo3) == ["halo3/halo3.dll"]


def test_a_failed_start_says_which_files_it_lacked() -> None:
    said = [f"error: the game folder {files.MISSING_FILES}:", "  halo3odst/maps/shared.map", "  halo3/maps/010_jungle.map"]
    assert files.kinds_in_failure(said) == ["odst", "campaign"]
    assert files.kinds_in_failure([f"x {files.MISSING_FILES}", "  halo3/halo3.dll"]) == ["halo3"]
    assert files.kinds_in_failure(["Port 11774 is in use"]) == []


@pytest.mark.parametrize("line", ["map Guardian", "mode Slayer", "load Guardian Slayer"])
def test_only_an_odst_server_refuses_the_commands_that_change_its_game(line: str) -> None:
    assert plays.console_refusal("odst", line) == REFUSAL
    assert plays.console_refusal("playlist", line) is None
    assert plays.console_refusal("odst", "players") is None
    assert plays.console_refusal("odst", line, "0.9.20") == REFUSAL and plays.console_refusal("odst", line, "0.9.21") is None


def test_campaign_and_odst_servers_are_co_op() -> None:
    assert plays.is_co_op({"campaign": {"mission": 3010}}) and plays.is_co_op({"odst": {"mode": "firefight"}})
    assert not plays.is_co_op({"playlist": "slayer.playlist.json"}) and not plays.is_co_op({})
    assert plays.CO_OP_MAX_PLAYERS == 16
