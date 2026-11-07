"""A content folder (or Workshop items…) set in the environment is taken over into dedicated.toml as Huragok starts and
never passed on again: Huragok and the server program must agree where the mods are."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from conftest import free_port
from testkit.setup import ALPHA, make_server_folder, setup_root
from testkit.workshop import ITEM, OLD

from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_top_key
from huragok.program.dedicated import tool_for
from huragok.program.environment import RECORD, parse, server_environment, take_over
from huragok.updates.workshop import installed_version

CONTENT = "RECLAIMER_DEDICATED_CONTENT"


def folders(tmp_path: Path, *names: str) -> list[Path]:
    made = []
    for name in names:
        folder = tmp_path / "SERVERS" / name
        make_server_folder(folder, name, free_port())
        made.append(folder)
    return made


def test_what_a_server_is_given_leaves_out_what_its_file_says() -> None:
    env = server_environment({CONTENT: "elsewhere", "RECLAIMER_DEDICATED_WORKSHOP_ITEMS": "1,2",
                              "RECLAIMER_DEDICATED_MAX_PLAYERS": "8", "PATH": "x"}, RECLAIMER_DEDICATED_ONLY="Alpha")
    assert env == {"RECLAIMER_DEDICATED_MAX_PLAYERS": "8", "PATH": "x", "RECLAIMER_DEDICATED_ONLY": "Alpha"}


def test_values_as_the_program_reads_them() -> None:
    assert parse("WORKSHOP_ITEMS", "3820687243, 3830420359") == [3820687243, 3830420359]
    assert parse("WORKSHOP_ITEMS", "1 2") == [1, 2]
    assert parse("WORKSHOP_UPDATE_CHECK_MINUTES", " 15 ") == 15
    assert parse("CONTENT", "D:/Reclaimer/content") == "D:/Reclaimer/content"
    for variable, text in (("WORKSHOP_ITEMS", "[1,2]"), ("WORKSHOP_ITEMS", "abc"), ("WORKSHOP_UPDATE_CHECK_MINUTES", "x")):
        with pytest.raises(ValueError):
            parse(variable, text)


def test_a_content_folder_set_in_the_environment_goes_into_every_servers_file_once(tmp_path: Path, caplog) -> None:
    alpha, beta = folders(tmp_path, "Alpha", "Beta")
    records = tmp_path / "records"
    env = {CONTENT: "D:/Reclaimer/content", "RECLAIMER_DEDICATED_WORKSHOP_ITEMS": f"{ITEM}"}
    with caplog.at_level(logging.WARNING):
        changed = take_over([alpha, beta], "dedicated.toml", records, env)
    assert len(changed) == 4
    for folder in (alpha, beta):
        data = read_toml(folder / "dedicated.toml")
        assert data["content"] == "D:/Reclaimer/content" and data["workshop"]["items"] == [ITEM]
    assert f"{CONTENT} was set where Huragok runs, so the program used it over" in caplog.text
    assert json.loads((records / RECORD).read_text(encoding="utf-8")) == {
        CONTENT: {str(alpha): "D:/Reclaimer/content", str(beta): "D:/Reclaimer/content"},
        "RECLAIMER_DEDICATED_WORKSHOP_ITEMS": {str(alpha): f"{ITEM}", str(beta): f"{ITEM}"}}
    # The host changes it in the file since (D): the same variable doesn't write over that.
    set_top_key(alpha / "dedicated.toml", "content", "content")
    assert take_over([alpha, beta], "dedicated.toml", records, env) == []
    assert read_toml(alpha / "dedicated.toml")["content"] == "content"
    # A new value is new.
    env[CONTENT] = "E:/content"
    assert take_over([alpha, beta], "dedicated.toml", records, env) == [
        f"{alpha / 'dedicated.toml'}: content = 'E:/content', from {CONTENT}",
        f"{beta / 'dedicated.toml'}: content = 'E:/content', from {CONTENT}"]
    # A start with it unset (a service started without it) forgets nothing: set again, the host's change stays.
    set_top_key(alpha / "dedicated.toml", "content", "content3")
    assert take_over([alpha, beta], "dedicated.toml", records, {}) == []
    assert take_over([alpha, beta], "dedicated.toml", records, env) == []
    assert read_toml(alpha / "dedicated.toml")["content"] == "content3"


def test_each_server_folder_is_taken_over_on_its_own(tmp_path: Path, caplog, monkeypatch) -> None:
    """Audit 6 November, 3.3 and 3.4: one folder that couldn't be written kept the value off the record, so every start
    wrote it over the host's changes in the others; and a server switched on or made later never got it."""
    alpha, beta = folders(tmp_path, "Alpha", "Beta")
    records = tmp_path / "records"
    env = {CONTENT: "D:/Reclaimer/content"}
    import huragok.program.environment as environment

    def locked(path: Path, key: str, value: object) -> None:  # Beta's file locked by an editor, say (a read-only file
        if path.parent == beta:                                  # doesn't stop root, in the Linux check)
            raise PermissionError(13, "Access is denied")
        set_top_key(path, key, value)

    monkeypatch.setattr(environment, "set_top_key", locked)
    with caplog.at_level(logging.WARNING):
        assert len(take_over([alpha, beta], "dedicated.toml", records, env)) == 1
    assert "Couldn't write content = 'D:/Reclaimer/content' into" in caplog.text
    set_top_key(alpha / "dedicated.toml", "content", "content")  # the host's change, in G
    assert take_over([alpha, beta], "dedicated.toml", records, env) == []  # Beta still can't be written: Alpha's kept
    assert read_toml(alpha / "dedicated.toml")["content"] == "content"
    monkeypatch.setattr(environment, "set_top_key", set_top_key)  # Beta can be written again
    assert take_over([alpha, beta], "dedicated.toml", records, env) == [
        f"{beta / 'dedicated.toml'}: content = 'D:/Reclaimer/content', from {CONTENT}"]  # once it can be
    (gamma,) = folders(tmp_path, "Gamma")  # made later, or switched on: the folder the program would have used
    assert take_over([alpha, beta, gamma], "dedicated.toml", records, env) == [
        f"{gamma / 'dedicated.toml'}: content = 'D:/Reclaimer/content', from {CONTENT}"]
    assert read_toml(alpha / "dedicated.toml")["content"] == "content"


def test_the_record_before_each_folder_had_its_own_reads_as_done(tmp_path: Path) -> None:
    alpha, beta = folders(tmp_path, "Alpha", "Beta")
    records = tmp_path / "records"
    records.mkdir()
    (records / RECORD).write_text(json.dumps({CONTENT: "D:/Reclaimer/content"}), encoding="utf-8")
    assert take_over([alpha, beta], "dedicated.toml", records, {CONTENT: "D:/Reclaimer/content"}) == []


def test_the_check_minutes_are_huragoks_and_game_isnt_read(tmp_path: Path, caplog) -> None:
    """3.7: [workshop_updates] check_minutes wrote over a taken-over WORKSHOP_UPDATE_CHECK_MINUTES 30 s later: only left
    out now. 3.8: the program doesn't read RECLAIMER_DEDICATED_GAME, so it isn't warned about as if it did."""
    (alpha,) = folders(tmp_path, "Alpha")
    before = (alpha / "dedicated.toml").read_text(encoding="utf-8")
    env = {"RECLAIMER_DEDICATED_WORKSHOP_UPDATE_CHECK_MINUTES": "60", "RECLAIMER_DEDICATED_GAME": "halo3"}
    with caplog.at_level(logging.WARNING):
        assert take_over([alpha], "dedicated.toml", tmp_path / "records", env) == []
    assert (alpha / "dedicated.toml").read_text(encoding="utf-8") == before
    assert "Huragok sets that for every server from [workshop_updates] check_minutes" in caplog.text
    assert "RECLAIMER_DEDICATED_GAME" not in caplog.text
    assert server_environment(env) == {"RECLAIMER_DEDICATED_GAME": "halo3"}


def test_a_value_the_program_would_refuse_and_other_overrides_are_only_said(tmp_path: Path, caplog) -> None:
    (alpha,) = folders(tmp_path, "Alpha")
    before = (alpha / "dedicated.toml").read_text(encoding="utf-8")
    env = {"RECLAIMER_DEDICATED_WORKSHOP_ITEMS": "abc", "RECLAIMER_DEDICATED_MAX_PLAYERS": "8",
           "RECLAIMER_DEDICATED_ONLY": "Alpha", CONTENT: "  "}
    with caplog.at_level(logging.WARNING):
        assert take_over([alpha], "dedicated.toml", tmp_path / "records", env) == []
    assert (alpha / "dedicated.toml").read_text(encoding="utf-8") == before
    assert "RECLAIMER_DEDICATED_WORKSHOP_ITEMS is set where Huragok runs, but not a list of Workshop item IDs" in caplog.text
    assert "RECLAIMER_DEDICATED_MAX_PLAYERS is set where Huragok runs" in caplog.text
    assert "RECLAIMER_DEDICATED_ONLY" not in caplog.text and CONTENT not in caplog.text


def test_huragok_takes_it_over_as_it_starts(tmp_path: Path, monkeypatch) -> None:
    """Before any server is looked after: the settings read again, so Huragok looks where the servers' mods have been."""
    from huragok.commandline import cli
    from huragok.commandline import run as run_module

    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    (folder / "content2").mkdir()
    monkeypatch.setenv(CONTENT, "content2")
    args = cli.build_parser().parse_args(["-c", str(settings_path), "start"])
    fresh = run_module.bring_files_up_to_date(args, load(settings_path))
    assert fresh.server(ALPHA).content_dir == (folder / "content2").resolve()
    assert read_toml(folder / "dedicated.toml")["content"] == "content2"


def test_huragoks_fetch_goes_where_huragok_looks(tmp_path: Path, monkeypatch) -> None:
    """With the variable still set, `dedicated mods` fetched into it (as the program does) while Huragok looked for the
    receipt in the file's folder: the update never went in."""
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    from huragok.core.tomledit import set_table_key

    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): OLD}), encoding="utf-8")
    monkeypatch.setenv(CONTENT, "content2")
    settings = load(settings_path)
    result = tool_for(settings).mods(folder)
    assert result.ok, result.text
    assert installed_version(settings.server(ALPHA).content_dir, ITEM) is not None
    assert not (folder / "content2").exists()


def test_a_docker_host_takes_nothing_over(tmp_path: Path, monkeypatch) -> None:
    """The host's call (6 November): with the docker backend the containers' environment is compose's, not Huragok's,
    and a compose file may use the variable for a volume: nothing is written from it."""
    import dataclasses

    from huragok.commandline import cli
    from huragok.commandline import run as run_module

    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    before = (folder / "dedicated.toml").read_text(encoding="utf-8")
    monkeypatch.setenv(CONTENT, "content2")
    args = cli.build_parser().parse_args(["-c", str(settings_path), "start"])
    run_module.bring_files_up_to_date(args, dataclasses.replace(load(settings_path), backend="docker"))
    assert (folder / "dedicated.toml").read_text(encoding="utf-8") == before
    assert not (tmp_path / "reclaimer" / "records" / RECORD).exists()
