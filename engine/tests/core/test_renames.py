"""How a rename is told apart from a server taken out and another added (0.7.0, answer 39), and its history in
Huragok's database carried over to the new name."""

from __future__ import annotations

from pathlib import Path

from huragok.core.renames import pair_renames, rename_in_database
from huragok.core.serverfolders import ServerConfig
from huragok.core.storage import Storage


def _server(folder: Path, name: str, port: int, block: int) -> ServerConfig:
    return ServerConfig(name=name, slug=name.lower(), folder=folder, config_path=folder / "dedicated.toml",
                        data_dir=folder / "data", game_port=port, rcon_host="127.0.0.1", rcon_port=port,
                        rcon_password="x" * 12, block=block)


def test_the_same_file_and_port_is_a_rename(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    old = _server(a, "Old", 46301, 1)
    assert pair_renames([old], [_server(a, "New", 46301, 1)]) == [(old, _server(a, "New", 46301, 1))]
    assert pair_renames([old], [_server(b, "New", 46301, 1)]) == []  # another folder: a new server
    assert pair_renames([old], []) == []


def test_with_the_port_changed_too_the_same_place_in_the_file_is_a_rename(tmp_path: Path) -> None:
    a = tmp_path / "a"
    first, second = _server(a, "First", 46301, 1), _server(a, "Second", 46302, 2)
    renamed = _server(a, "Renamed", 46399, 2)
    assert pair_renames([second], [renamed]) == [(second, renamed)]
    # Two gone, two new: each by its port first, then by its place
    one, two = _server(a, "One", 46301, 1), _server(a, "Two", 46399, 2)
    assert pair_renames([first, second], [two, one]) == [(first, one), (second, two)]


def test_a_new_server_in_the_place_of_one_whose_port_another_took_isnt_paired(tmp_path: Path) -> None:
    """The host moved Second's port to the first block's server and put a new one second: not a rename of Second."""
    a = tmp_path / "a"
    first, second = _server(a, "First", 46301, 1), _server(a, "Second", 46302, 2)
    moved = _server(a, "Moved", 46302, 1)
    pairs = pair_renames([first, second], [moved])
    assert pairs == [(second, moved)]


def test_every_table_naming_the_server_names_the_new_one(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "huragok.sqlite3")
    try:
        storage.record_pick("Old", "slayer", "Guardian", "Slayer on Guardian")
        storage.record_pick("Other", "slayer", "Guardian", "Slayer on Guardian")
        storage.execute("CREATE TABLE extra (server TEXT, on_server TEXT, note TEXT)")
        storage.execute("INSERT INTO extra VALUES ('Old', 'Old', 'kept'), ('Other', 'Old', 'kept')")
        assert rename_in_database(storage, "Old", "New") == 4
        assert sorted(row[0] for row in storage.query("SELECT server FROM ballots")) == ["New", "Other"]
        assert [tuple(row) for row in storage.query("SELECT server, on_server FROM extra ORDER BY rowid")] == [
            ("New", "New"), ("Other", "New")]
        assert rename_in_database(storage, "Nobody", "Someone") == 0
    finally:
        storage.close()


def test_the_same_name_in_two_folders_keeps_the_one_already_looked_after(tmp_path: Path) -> None:
    """0.7.0, answer 31: the other is left off, and said, until one is renamed."""
    from conftest import write_server_folder

    from huragok.core.serverfolders import FolderProblem, read_servers

    copy, real = (tmp_path / "SERVERS" / "Copy of Alpha").resolve(), (tmp_path / "SERVERS" / "Alpha").resolve()
    write_server_folder(copy, 46311, name="Alpha")
    write_server_folder(real, 46312, name="Alpha")
    problems: list[FolderProblem] = []
    kept = read_servers([copy, real], "dedicated.toml", {}, [], problems)
    assert [s.folder for s in kept] == [real]  # nothing known yet: the folder named after it
    problems = []
    kept = read_servers([copy, real], "dedicated.toml", {}, [], problems, known={"alpha"},
                        known_folders={"alpha": copy})
    assert [s.folder for s in kept] == [copy]  # the one already running keeps running
    assert len(problems) == 1 and problems[0].folder == real and "left out until one is renamed" in str(problems[0])
