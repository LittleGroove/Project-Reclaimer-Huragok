"""Huragok's records (core/storage.py): what players picked on each playlist's ballots."""

from __future__ import annotations

from pathlib import Path

from huragok.core.storage import Storage, read_picks


def test_what_players_pick_is_counted_by_playlist(tmp_path: Path) -> None:
    path = tmp_path / "records" / "huragok.sqlite3"
    storage = Storage(path)
    for server, picked, when in (("Alpha", "BR Fiesta CTF on Hugegrass", 100.0), ("Alpha", "BR Fiesta CTF on hugegrass", 200.0),
                                 ("Bravo", "Team Slayer on Hugegrass", 150.0), ("Alpha", "Slayer on Guardian", 120.0)):
        storage.record_pick(server, "HugeGrass BR Fiesta Rotation" if "Hugegrass" in picked or "hugegrass" in picked
                            else "Other", picked, "playlist x", when)
    assert storage.picks("hugegrass br fiesta rotation") == [("BR Fiesta CTF on hugegrass", 2, 200.0),  # latest spelling
                                                             ("Team Slayer on Hugegrass", 1, 150.0)]
    assert storage.picks("HugeGrass BR Fiesta Rotation", "Bravo") == [("Team Slayer on Hugegrass", 1, 150.0)]
    storage.close()
    picks, since = read_picks(path, "HugeGrass BR Fiesta Rotation")  # read-only, as a second window reads it
    assert [p[:2] for p in picks] == [("BR Fiesta CTF on hugegrass", 2), ("Team Slayer on Hugegrass", 1)] and since == 100.0
    assert read_picks(tmp_path / "nowhere.sqlite3", "x") == ([], None)
