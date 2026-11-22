"""RECLAIMER_DEDICATED_ variables set where Huragok runs (0.7.0, plan 4.2): passed to the servers as they are, never
copied into their files, and read the way the program reads them, the variable over the file, with every override
said on each server."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from testkit.setup import ALPHA, setup_root
from testkit.workshop import ITEM, OLD

from huragok.core.serverfolders import discover_servers
from huragok.core.settings import load
from huragok.program.dedicated import tool_for
from huragok.program.environment import RECORD, overrides, parse, server_environment, taken_over
from huragok.updates.workshop import installed_version

CONTENT = "RECLAIMER_DEDICATED_CONTENT"


def test_a_server_gets_huragoks_environment_as_it_is() -> None:
    env = server_environment({CONTENT: "elsewhere", "RECLAIMER_DEDICATED_WORKSHOP_ITEMS": "1,2",
                              "RECLAIMER_DEDICATED_AUTO_UPDATE": "true", "PATH": "x"}, RECLAIMER_DEDICATED_ONLY="Alpha")
    assert env == {CONTENT: "elsewhere", "RECLAIMER_DEDICATED_WORKSHOP_ITEMS": "1,2",
                   "RECLAIMER_DEDICATED_AUTO_UPDATE": "true", "PATH": "x", "RECLAIMER_DEDICATED_ONLY": "Alpha"}


def test_values_as_the_program_reads_them() -> None:
    assert parse("WORKSHOP_ITEMS", "3820687243, 3830420359") == [3820687243, 3830420359]
    assert parse("WORKSHOP_ITEMS", "1 2") == [1, 2]
    assert parse("WORKSHOP_UPDATE_CHECK_MINUTES", " 15 ") == 15
    assert parse("CONTENT", "D:/Reclaimer/content") == "D:/Reclaimer/content"
    assert parse("AUTO_UPDATE", "true") is True and parse("AUTO_UPDATE", "0") is False
    for variable, text in (("WORKSHOP_ITEMS", "[1,2]"), ("WORKSHOP_ITEMS", "abc"), ("WORKSHOP_UPDATE_CHECK_MINUTES", "x"),
                           ("AUTO_UPDATE", "maybe")):
        with pytest.raises(ValueError):
            parse(variable, text)


def test_only_the_programs_own_overrides_count() -> None:
    assert overrides({CONTENT: "c", "RECLAIMER_DEDICATED_ONLY": "Alpha", "RECLAIMER_DEDICATED_GAME": "g",
                      "RECLAIMER_DEDICATED_MAX_PLAYERS": "12", "RECLAIMER_DEDICATED_DATA": "  ", "PATH": "x"}) == {
        CONTENT: "c", "RECLAIMER_DEDICATED_MAX_PLAYERS": "12"}


def test_a_variable_wins_over_the_file_and_is_said_on_the_server(tmp_path: Path, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    before = (folder / "dedicated.toml").read_text(encoding="utf-8")
    monkeypatch.setenv(CONTENT, "content2")
    monkeypatch.setenv("RECLAIMER_DEDICATED_WORKSHOP_ITEMS", "1 2")
    monkeypatch.setenv("RECLAIMER_DEDICATED_AUTO_UPDATE", "false")
    monkeypatch.setenv("RECLAIMER_DEDICATED_MAX_PLAYERS", "12")
    server = load(settings_path).server(ALPHA)
    assert server.content_dir == (folder / "content2").resolve()
    assert server.workshop_items == [1, 2] and server.auto_update is False
    [(rule, fields)] = server.warnings
    assert rule == "environment" and f"{CONTENT} = content2 (over its content)" in fields["overrides"]
    assert "RECLAIMER_DEDICATED_MAX_PLAYERS = 12" in fields["overrides"]
    assert (folder / "dedicated.toml").read_text(encoding="utf-8") == before  # nothing written
    assert not (tmp_path / "reclaimer" / "records" / RECORD).exists()


def test_a_value_the_program_refuses_is_said_and_left_out(tmp_path: Path, monkeypatch) -> None:
    settings_path = setup_root(tmp_path)
    monkeypatch.setenv("RECLAIMER_DEDICATED_WORKSHOP_ITEMS", "abc")
    server = load(settings_path).server(ALPHA)
    assert server.workshop_items == [] and "the program refuses it" in server.warnings[0][1]["overrides"]


def test_starting_writes_nothing_into_the_servers_files(tmp_path: Path, monkeypatch) -> None:
    """Before 0.7.0 the variable was copied into every dedicated.toml as Huragok started."""
    from huragok.commandline import cli
    from huragok.commandline import run as run_module

    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    before = (folder / "dedicated.toml").read_text(encoding="utf-8")
    monkeypatch.setenv(CONTENT, "content2")
    args = cli.build_parser().parse_args(["-c", str(settings_path), "start"])
    fresh = run_module.bring_files_up_to_date(args, load(settings_path))
    assert (folder / "dedicated.toml").read_text(encoding="utf-8") == before
    assert fresh.server(ALPHA).content_dir == (folder / "content2").resolve()  # read as the program uses it


def test_huragok_looks_where_the_program_fetches(tmp_path: Path, monkeypatch) -> None:
    """With the variable set, `dedicated mods` fetches into its folder, as the program does, and Huragok reads the
    server's content folder there too: they agree where the mods are."""
    from huragok.core.tomledit import set_table_key

    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): OLD}), encoding="utf-8")
    monkeypatch.setenv(CONTENT, "content2")
    settings = load(settings_path)
    result = tool_for(settings).mods(folder)
    assert result.ok, result.text
    assert settings.server(ALPHA).content_dir == (folder / "content2").resolve()
    assert installed_version(settings.server(ALPHA).content_dir, ITEM) is not None


def test_a_docker_host_reads_nothing_from_huragoks_environment(tmp_path: Path, monkeypatch) -> None:
    """The host's call (6 November): with the docker backend the containers' environment is compose's, not Huragok's."""
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    monkeypatch.setenv(CONTENT, "content2")
    docker = dataclasses.replace(load(settings_path), backend="docker", servers=[])
    [server] = discover_servers(docker)
    assert server.content_dir != (folder / "content2").resolve() and server.warnings == []


def test_what_huragok_copied_before_0_7_0_is_still_read(tmp_path: Path) -> None:
    """For the 0.7.0 update step's list of what Huragok used to set."""
    records = tmp_path / "records"
    records.mkdir()
    (records / RECORD).write_text(json.dumps({CONTENT: {"S:/a": "content2"}, "OLD_FORM": "x"}), encoding="utf-8")
    assert taken_over(records) == {CONTENT: {"S:/a": "content2"}}
    assert taken_over(tmp_path / "nowhere") == {}
