from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ADMIN, MOD, staff, write_server_folder, write_settings
from fake_rcon import PASSWORD

from reclaimer_supervisor.config import ConfigError, load, slugify


def test_reads_servers_from_dedicated_toml(tmp_path: Path) -> None:
    server = tmp_path / "server"
    data = write_server_folder(server, 49176, host_key="k" * 64)
    settings = load(write_settings(tmp_path, server, commands=staff()))
    assert [s.name for s in settings.servers] == ["LG Test Server"]
    s = settings.servers[0]
    assert s.data_dir == data and s.slug == "lg-test-server"
    endpoint = s.rcon_endpoint()
    assert (endpoint.host, endpoint.port, endpoint.password) == ("127.0.0.1", 49176, PASSWORD)
    assert s.host_key() == "k" * 64
    assert ADMIN in s.admins
    assert settings.permissions.members["moderator"] == {MOD}
    assert settings.backend == "attach" and settings.database is None
    assert settings.home == tmp_path.resolve() and settings.records == tmp_path.resolve() / "records"


def test_instance_json_wins(tmp_path: Path) -> None:
    server = tmp_path / "server"
    data = write_server_folder(server, 49176)
    (data / "instance.json").write_text(json.dumps(
        {"name": "LG Test Server", "rcon": {"address": "0.0.0.0", "port": 50000, "password": "from-environment"}}),
        encoding="utf-8")
    endpoint = load(write_settings(tmp_path, server)).servers[0].rcon_endpoint()
    assert (endpoint.host, endpoint.port, endpoint.password) == ("127.0.0.1", 50000, "from-environment")


def test_disabled_servers_and_selection(tmp_path: Path) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 49176, extra_server='[[server]]\nname = "Off"\nport = 49177\nenabled = false\n'
                                                   '[[server]]\nname = "Second"\nport = 49178\n')
    assert [s.name for s in load(write_settings(tmp_path, server)).servers] == ["LG Test Server", "Second"]
    only = load(write_settings(tmp_path, server, 'servers = ["second"]'))
    assert [s.name for s in only.servers] == ["Second"]
    with pytest.raises(ConfigError, match="Off"):
        load(write_settings(tmp_path, server, 'servers = ["Off"]'))


@pytest.mark.parametrize("body, message", [
    ('backend = "systemd"', "backend"),
    ('[roles]\nmoderator = []', "commands.toml"),
    ('[commands]\nprefix = "!"', "commands.toml"),
    ('[[server]]\nname = "Nope"', "Nope"),
    ('[automd]\nenabled = true', "isn't a section of settings.toml"),
    ('[manager]\nenabled = true', "isn't a section of settings.toml"),
    ('[automod]\nenabled = "yes"', "automod.enabled must be bool"),
])
def test_errors_name_the_setting(tmp_path: Path, body: str, message: str) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 49176)
    with pytest.raises(ConfigError, match=message):
        load(write_settings(tmp_path, server, body))


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="start the supervisor once"):
        load(tmp_path / "settings.toml")
    with pytest.raises(ConfigError, match="inside your server folder"):
        load(write_settings(tmp_path, tmp_path / "nowhere"))


def test_the_server_folder_defaults_to_the_one_we_sit_in(tmp_path: Path) -> None:
    write_server_folder(tmp_path, 49176)
    home = tmp_path / "reclaimer-supervisor"
    home.mkdir()
    (home / "settings.toml").write_text('database = ""\nlog_file = ""\n', encoding="utf-8")
    assert load(home / "settings.toml").server_folder == tmp_path.resolve()


def test_a_broken_commands_file_is_a_settings_problem(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", 49176)
    with pytest.raises(ConfigError, match="no !mutee"):
        load(write_settings(tmp_path, tmp_path / "server", commands='[moderator]\ncommands = ["mutee"]'))


def test_slugify() -> None:
    assert slugify("LG Test Server") == "lg-test-server"
    assert slugify("  Big Team Battle!! ") == "big-team-battle"
