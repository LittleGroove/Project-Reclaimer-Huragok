from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ADMIN, MOD, staff, write_root, write_server_folder, write_settings
from fake_rcon import PASSWORD

from reclaimer_supervisor.config import ConfigError, discover_servers, load, slugify


def test_reads_servers_from_dedicated_toml(tmp_path: Path) -> None:
    server = tmp_path / "server"
    data = write_server_folder(server, 49176, host_key="k" * 64)
    settings = load(write_settings(tmp_path, server, commands=staff()))
    assert [s.name for s in settings.servers] == ["LG Test Server"]
    s = settings.servers[0]
    assert s.data_dir == data and s.slug == "lg-test-server" and s.folder == server.resolve()
    assert s.config_path == server.resolve() / "dedicated.toml" and s.autostart and s.daily_restart is None
    endpoint = s.rcon_endpoint()
    assert (endpoint.host, endpoint.port, endpoint.password) == ("127.0.0.1", 49176, PASSWORD)
    assert s.host_key() == "k" * 64
    assert ADMIN in s.admins and s.errors == []
    assert settings.permissions.members["moderator"] == {MOD}
    assert settings.backend == "attach" and settings.database is None
    assert settings.home == tmp_path.resolve() and settings.records == tmp_path.resolve() / "records"
    assert settings.restarts.warn_seconds == [300, 180, 60, 30] and settings.restarts.max_failures == 5


def test_the_servers_folder_layout(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, [("Big Team Battle", 49176), ("Slayer", 49178)], '[[server]]\nname = "Slayer"\n'
                                                                              'autostart = false\n'
                                                                              'daily_restart = "05:30"\n')
    settings = load(path)
    assert [s.name for s in settings.servers] == ["Big Team Battle", "Slayer"]
    assert settings.servers[0].folder == (root / "SERVERS" / "Big Team Battle").resolve()
    assert settings.servers[1].folder == (root / "SERVERS" / "Slayer").resolve()
    assert settings.servers[0].autostart and not settings.servers[1].autostart
    assert settings.servers[1].daily_restart == "05:30"
    assert settings.server_folders == [s.folder for s in settings.servers]
    # A folder added later is found without a restart.
    write_server_folder(root / "SERVERS" / "Third", 49180, name="Third")
    assert [s.name for s in discover_servers(settings)] == ["Big Team Battle", "Slayer", "Third"]
    # The same name in two folders is a mistake worth naming; the same port is a per-server error.
    write_server_folder(root / "SERVERS" / "Copy", 49181, name="Slayer")
    with pytest.raises(ConfigError, match="two servers are called 'Slayer'"):
        discover_servers(settings)
    (root / "SERVERS" / "Copy" / "dedicated.toml").unlink()
    write_server_folder(root / "SERVERS" / "Clash", 49176, name="Clash")
    clash = next(s for s in discover_servers(settings) if s.name == "Clash")
    assert clash.errors == ["port 49176 is also used by Big Team Battle"]


def test_a_broken_or_copied_server_folder_leaves_only_itself_out(tmp_path: Path) -> None:
    """A hand edit gone wrong in one folder, or a copied folder, never keeps the other servers from starting."""
    root = tmp_path / "reclaimer"
    path = write_root(root, [("Good", 49176)], '[[server]]\nname = "Gone"\nautostart = false\n')
    typo = root / "SERVERS" / "Typo"
    typo.mkdir()
    (typo / "dedicated.toml").write_text('[[server]]\nname = "Typo\nport = 49178\n', encoding="utf-8")
    write_server_folder(root / "SERVERS" / "Copy of Good", 49180, name="Good")  # sorts first; the real one wins
    settings = load(path)
    assert [s.name for s in settings.servers] == ["Good"]
    assert settings.servers[0].folder == (root / "SERVERS" / "Good").resolve()
    texts = [str(p) for p in settings.problems]
    assert len(texts) == 3, texts
    assert "Typo" in texts[0] and texts[0].endswith("Its servers are left out until that's fixed")
    assert "two servers are called 'Good'" in texts[1] and "Copy of Good is left out until one is renamed" in texts[1]
    assert "[[server]] 'Gone' in settings.toml is not an enabled server" in texts[2]
    assert [p.folder for p in settings.problems] == [typo.resolve(), (root / "SERVERS" / "Copy of Good").resolve(), None]


def test_no_servers_yet_is_not_an_error(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    root.mkdir()
    settings = load(write_settings(root))
    assert settings.servers == [] and settings.servers_folder == (root / "SERVERS").resolve()


def test_rcon_off_is_a_server_error_not_a_crash(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "Quiet", 49176, name="Quiet", rcon_password=None)
    settings = load(write_settings(root))
    assert settings.servers[0].errors and "RCON is off" in settings.servers[0].errors[0]
    assert settings.servers[0].rcon_endpoint() is None


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
    off = load(write_settings(tmp_path, server, 'servers = ["Off"]'))  # named, but not there: noted, not fatal
    assert off.servers == [] and "servers: 'Off' is not an enabled server" in str(off.problems[0])


def test_the_windows_backend_name_still_means_process(tmp_path: Path) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 49176)
    settings = load(write_settings(tmp_path, server, 'backend = "windows"\n[windows]\nstop_timeout = 7\n'))
    assert settings.backend == "process" and settings.backend_options["stop_timeout"] == 7


@pytest.mark.parametrize("body, message", [
    ('backend = "systemd"', "backend"),
    ('[roles]\nmoderator = []', "commands.toml"),
    ('[commands]\nprefix = "!"', "commands.toml"),
    ('[[server]]\nname = "LG Test Server"\nmemory = "16g"', "isn't a setting"),
    ('[automd]\nenabled = true', "isn't a section of settings.toml"),
    ('[manager]\nenabled = true', "isn't a section of settings.toml"),
    ('[automod]\nenabled = "yes"', "automod.enabled must be bool"),
    ('[restarts]\ndaily_restart = "4am"', "time of day"),
    ('[restarts]\nwarn_seconds = "300"', "list of seconds"),
    ('[restarts]\nmax_failures = 0', "1 or more"),
])
def test_errors_name_the_setting(tmp_path: Path, body: str, message: str) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 49176)
    with pytest.raises(ConfigError, match=message):
        load(write_settings(tmp_path, server, body))


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="start the supervisor once"):
        load(tmp_path / "settings.toml")
    with pytest.raises(ConfigError, match="server_folder should name a server folder"):
        load(write_settings(tmp_path, tmp_path / "nowhere"))


def test_a_broken_commands_file_is_a_settings_problem(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", 49176)
    with pytest.raises(ConfigError, match="no !mutee"):
        load(write_settings(tmp_path, tmp_path / "server", commands='[moderator]\ncommands = ["mutee"]'))


def test_slugify() -> None:
    assert slugify("LG Test Server") == "lg-test-server"
    assert slugify("  Big Team Battle!! ") == "big-team-battle"
