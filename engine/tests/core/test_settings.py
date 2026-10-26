from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ADMIN, MOD, free_port, free_ports, staff, write_root, write_server_folder, write_settings
from testkit.fake_rcon import PASSWORD

from huragok.commandline import firstrun
from huragok.core.serverfolders import discover_servers, slugify
from huragok.core.settings import load
from huragok.core.tables import ConfigError


def test_reads_servers_from_dedicated_toml(tmp_path: Path) -> None:
    server = tmp_path / "server"
    data = write_server_folder(server, 39176, host_key="k" * 64)
    settings = load(write_settings(tmp_path, server, commands=staff()))
    assert [s.name for s in settings.servers] == ["LG Test Server"]
    s = settings.servers[0]
    assert s.data_dir == data and s.slug == "lg-test-server" and s.folder == server.resolve()
    assert s.config_path == server.resolve() / "dedicated.toml" and s.autostart and s.daily_restart is None
    endpoint = s.rcon_endpoint()
    assert (endpoint.host, endpoint.port, endpoint.password) == ("127.0.0.1", 39176, PASSWORD)
    assert s.host_key() == "k" * 64
    assert ADMIN in s.admins and s.errors == []
    assert settings.permissions.members["moderator"] == {MOD}
    assert settings.backend == "attach" and settings.database is None
    assert settings.home == tmp_path.resolve() and settings.records == tmp_path.resolve() / "records"
    assert settings.restarts.warn_seconds == [300, 180, 60, 30] and settings.restarts.max_failures == 5


def test_the_servers_folder_layout(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, [("Big Team Battle", 39176), ("Slayer", 39178)], '[[server]]\nname = "Slayer"\n'
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
    write_server_folder(root / "SERVERS" / "Third", 39180, name="Third")
    assert [s.name for s in discover_servers(settings)] == ["Big Team Battle", "Slayer", "Third"]
    # The same name in two folders is a mistake worth naming; the same port is a per-server error.
    write_server_folder(root / "SERVERS" / "Copy", 39181, name="Slayer")
    with pytest.raises(ConfigError, match="two servers are called 'Slayer'"):
        discover_servers(settings)
    (root / "SERVERS" / "Copy" / "dedicated.toml").unlink()
    write_server_folder(root / "SERVERS" / "Clash", 39176, name="Clash")
    clash = next(s for s in discover_servers(settings) if s.name == "Clash")
    assert clash.errors == ["port 39176 is also used by Big Team Battle"]


def test_a_broken_or_copied_server_folder_leaves_only_itself_out(tmp_path: Path) -> None:
    """A hand edit gone wrong in one folder, or a copied folder, never keeps the other servers from starting."""
    root = tmp_path / "reclaimer"
    path = write_root(root, [("Good", 39176)], '[[server]]\nname = "Gone"\nautostart = false\n')
    typo = root / "SERVERS" / "Typo"
    typo.mkdir()
    (typo / "dedicated.toml").write_text('[[server]]\nname = "Typo\nport = 39178\n', encoding="utf-8")
    write_server_folder(root / "SERVERS" / "Copy of Good", 39180, name="Good")  # sorts first; the real one wins
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
    write_server_folder(root / "SERVERS" / "Quiet", 39176, name="Quiet", rcon_password=None)
    settings = load(write_settings(root))
    assert settings.servers[0].errors and "RCON is off" in settings.servers[0].errors[0]
    assert settings.servers[0].rcon_endpoint() is None


def test_instance_json_wins(tmp_path: Path) -> None:
    server = tmp_path / "server"
    data = write_server_folder(server, 39176)
    (data / "instance.json").write_text(json.dumps(
        {"name": "LG Test Server", "rcon": {"address": "0.0.0.0", "port": 50000, "password": "from-environment"}}),
        encoding="utf-8")
    endpoint = load(write_settings(tmp_path, server)).servers[0].rcon_endpoint()
    assert (endpoint.host, endpoint.port, endpoint.password) == ("127.0.0.1", 50000, "from-environment")


def test_disabled_servers_and_selection(tmp_path: Path) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 39176, extra_server='[[server]]\nname = "Off"\nport = 39177\nenabled = false\n'
                                                   '[[server]]\nname = "Second"\nport = 39178\n')
    assert [s.name for s in load(write_settings(tmp_path, server)).servers] == ["LG Test Server", "Second"]
    only = load(write_settings(tmp_path, server, 'servers = ["second"]'))
    assert [s.name for s in only.servers] == ["Second"]
    off = load(write_settings(tmp_path, server, 'servers = ["Off"]'))  # named, but not there: noted, not fatal
    assert off.servers == [] and "servers: 'Off' is not an enabled server" in str(off.problems[0])


def test_the_windows_backend_name_still_means_process(tmp_path: Path) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 39176)
    settings = load(write_settings(tmp_path, server, 'backend = "windows"\n[windows]\nstop_timeout = 7\n'))
    assert settings.backend == "process" and settings.backend_options["stop_timeout"] == 7


@pytest.mark.parametrize("body, message", [
    ('backend = "systemd"', "backend"),
    ('[roles]\nmoderator = []', "commands.toml"),
    ('[commands]\nprefix = "!"', "commands.toml"),
    ('[[server]]\nname = "LG Test Server"\nmemory = "16g"', "isn't a setting"),
    ('[automd]\nenabled = true', "isn't a section of settings.toml"),
    ('[manager]\nenabled = true', "isn't a section of settings.toml"),
    ('[automod]\nenabled = "yes"', 'automod.enabled must be true or false, not "yes"'),
    ('[restarts]\ndaily_restart = "4am"', "time of day"),
    ('[restarts]\nwarn_seconds = "300"', "list of seconds"),
    ('[restarts]\nmax_failures = 0', "1 or more"),
])
def test_errors_name_the_setting(tmp_path: Path, body: str, message: str) -> None:
    server = tmp_path / "server"
    write_server_folder(server, 39176)
    with pytest.raises(ConfigError, match=message):
        load(write_settings(tmp_path, server, body))


def test_missing_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="start Huragok once"):
        load(tmp_path / "settings.toml")
    with pytest.raises(ConfigError, match="server_folder should name a server folder"):
        load(write_settings(tmp_path, tmp_path / "nowhere"))


def test_a_broken_commands_file_is_a_settings_problem(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", 39176)
    with pytest.raises(ConfigError, match="listed under both"):
        load(write_settings(tmp_path, tmp_path / "server",
                            commands='[moderator]\ncommands = ["mute"]\n[admin]\ncommands = ["mute"]'))
    # A command this version doesn't have (a typo, or a newer version's, after an update was put back) is named, but
    # Huragok starts.
    settings = load(write_settings(tmp_path, tmp_path / "server", commands='[moderator]\ncommands = ["mutee"]'))
    assert "there's no !mutee" in settings.permissions.unknown[0]


def test_slugify() -> None:
    assert slugify("LG Test Server") == "lg-test-server"
    assert slugify("  Big Team Battle!! ") == "big-team-battle"


# What a host's settings.toml can get wrong, and what it then says ----------------------------------------------


def _root(tmp_path: Path, body: str, servers: list[tuple[str, int]] | None = None) -> Path:
    return write_root(tmp_path / "reclaimer", servers if servers is not None else [("LG Test Server", free_port())], body)


@pytest.mark.parametrize("body, expected", [
    ("[relay]\nenable = true", "settings.toml [relay] enable isn't a setting, so it does nothing (did you mean enabled?)"),
    ("[automod]\ndryrun = false", "[automod] dryrun isn't a setting, so it does nothing (did you mean dry_run?)"),
    # Appended at the end of the file, a line lands in the last section above it.
    ('[rcon]\ncommand_timeout = 10\ndaily_restart = "04:00"',
     "[rcon] daily_restart isn't a setting, so it does nothing: it belongs under [restarts] (a line belongs to the "
     "[section] heading above it)"),
    ('dailyrestart = "04:00"', "dailyrestart (above the first [section]) isn't a setting"),
    ("ready_seconds = 300", "it belongs under [restarts]"),
    ("[watchdog]\nslowframes = \"act\"", "[watchdog] slowframes isn't a setting, so it does nothing (did you mean "
                                       "slow_frames?)"),
    ('[[relay.user]]\nname = "Alice"\npassword = "a long passphrase"\npasswd = "x"',
     "[[relay.user]] passwd isn't a setting"),
    ("[setup]\nshares = [\"game\"]", "(did you mean share?)"),
])
def test_a_key_that_isnt_a_setting_is_named(tmp_path: Path, body: str, expected: str) -> None:
    settings = load(_root(tmp_path, body))
    assert any(expected in line for line in settings.retired), settings.retired


def test_the_keys_every_template_wrote_are_all_known(tmp_path: Path) -> None:
    """The template as the first start writes it, and the [command]/[docker] values used in their commands."""
    from huragok.commandline import cli

    root = tmp_path / "reclaimer"
    firstrun.write_files(root)
    assert load(root / "settings.toml").retired == []
    settings = load(_root(tmp_path, 'backend = "command"\n[command]\nstart = "run {unit} {name}"\nunit = "x"\n'
                                    'spare = "y"'))
    assert [line for line in settings.retired if "unit" in line] == []
    assert any("[command] spare isn't a setting" in line for line in settings.retired)


@pytest.mark.parametrize("body", ['[server]\nname = "LG Test Server"', 'server = "LG Test Server"'])
def test_one_bracket_server_table_says_use_two(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError, match=r"\[\[server\]\] block \(two brackets"):
        load(_root(tmp_path, body))


@pytest.mark.parametrize("line, message", [
    ("warn_seconds = 300", "LG Test Server.warn_seconds must be a list of seconds"),
    ('min_uptime_seconds = "15m"', 'LG Test Server.min_uptime_seconds must be a number, not "15m"'),
    ("min_uptime_seconds = -5", "must be at least 0"),
    ('autostart = "no"', 'settings.toml \\[\\[server\\]\\] LG Test Server.autostart must be true or false, not "no"'),
    ("rcon_port = 70000", "rcon_port must be a port number"),
    ('daily_restart = "4am"', "time of day"),
    ("autostrat = false", "isn't a setting a server can have here \\(did you mean autostart\\?\\)"),
])
def test_a_server_block_is_checked_as_settings_toml_is_read(tmp_path: Path, line: str, message: str) -> None:
    """A wrong value in a [[server]] block names settings.toml (not dedicated.toml) and stops at the start, instead of
    failing every restart later (warn_seconds = 300 made each stop raise, and `stop` stop none of the servers)."""
    with pytest.raises(ConfigError, match=message):
        load(_root(tmp_path, f'[[server]]\nname = "LG Test Server"\n{line}\n'))


def test_server_block_values_are_read_as_restarts_reads_them(tmp_path: Path) -> None:
    settings = load(_root(tmp_path, '[[server]]\nname = "LG Test Server"\nwarn_seconds = [60, 30]\n'
                                    'min_uptime_seconds = 1800\ndaily_restart = "5:00"\n'))
    server = settings.servers[0]
    assert settings.of(server).restart("warn_seconds") == [60.0, 30.0]
    assert settings.of(server).restart("min_uptime_seconds") == 1800.0
    assert settings.of(server).restart("daily_restart") == "05:00" == server.daily_restart


def test_a_block_for_a_server_left_out_on_purpose_is_no_problem(tmp_path: Path) -> None:
    settings = load(_root(tmp_path, 'servers = ["Slayer"]\n[[server]]\nname = "LG Test Server"\nautostart = false\n',
                          list(zip(["LG Test Server", "Slayer"], free_ports(2)))))
    assert [s.name for s in settings.servers] == ["Slayer"] and settings.problems == []


def test_windows_paths_in_double_quotes_get_a_hint(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="write them in 'single quotes'"):
        load(_root(tmp_path, '[setup]\nsteamcmd = "C:\\steamcmd\\steamcmd.exe"'))
    with pytest.raises(ConfigError, match="write them in 'single quotes'"):
        load(_root(tmp_path, '[setup]\nprogram = "C:\\Users\\me\\x.exe"'))  # \U: "Invalid hex value"
    # Read without an error, but \n became a line break: not the path that was typed.
    with pytest.raises(ConfigError, match=r"setup.mcc_folder has a control character in it \(\\n\)"):
        load(_root(tmp_path, '[setup]\nmcc_folder = "D:\\new games"'))
    settings = load(_root(tmp_path, "[setup]\nsteamcmd = 'C:\\steamcmd\\steamcmd.exe'"))
    assert settings.setup.steamcmd == "C:\\steamcmd\\steamcmd.exe"


@pytest.mark.parametrize("body, message", [
    ('[process]\nstop_timeout = "30"', 'process.stop_timeout must be a number, not "30"'),
    ("[process]\nstop_timeout = 0", "process.stop_timeout must be at least 1"),
    ('[process]\ncommand = "wine x"', "process.command must be a list of words in quotes"),
    ('[process]\ncommand = ["wine", "{executable}", "--port", "{port}"]',
     "\\[process\\] command: {port} isn't something Huragok fills in"),
    ('[process]\ncommand = ["wine", "{executable"]', "write {{ and }} for a brace itself"),
    ('backend = "command"\n[command]\nstart = "systemctl start {unit}"', "\\[command\\] start: {unit} isn't"),
    ('backend = "command"\n[command]\ntimeout = "slow"', "command.timeout must be a number"),
    ('backend = "docker"\n[docker]\nstart = 5', "docker.start must be text in quotes"),
])
def test_backend_settings_are_checked_as_settings_toml_is_read(tmp_path: Path, body: str, message: str) -> None:
    body = body if "backend" in body else 'backend = "process"\n' + body
    with pytest.raises(ConfigError, match=message):
        load(_root(tmp_path, body))


def test_backend_placeholders_that_are_filled_in_pass(tmp_path: Path) -> None:
    load(_root(tmp_path, 'backend = "process"\n[process]\ncommand = ["wine", "{executable}", "dedicated", "run", '
                         '"--config", "{config}", "{{literal}}"]'))
    load(_root(tmp_path, 'backend = "command"\n[command]\nstart = "systemctl start reclaimer@{slug} {unit}"\n'
                         'unit = "x"'))
    load(_root(tmp_path, 'backend = "docker"\n[docker]\nrestart = "docker compose -f {compose_file} restart '
                         '{service}"'))


@pytest.mark.parametrize("body, message", [
    ("[keepalive]\ncheck_seconds = 0", "keepalive.check_seconds must be at least 1, not 0"),
    ("[keepalive]\ncheck_seconds = -1", "keepalive.check_seconds must be at least 1"),
    ("[keepalive]\nhang_seconds = 4", "keepalive.hang_seconds must be at least 30"),
    ("[rcon]\ncommand_timeout = 0", "rcon.command_timeout must be at least 1"),
    ("[restarts]\nready_seconds = 0", "restarts.ready_seconds must be at least 30"),
    ("[restarts]\nempty_seconds = -1", "restarts.empty_seconds must be at least 0"),
    ("[automod]\nhandler_timeout = 0", "automod.handler_timeout must be at least 1"),
])
def test_timing_settings_have_a_least_value(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(_root(tmp_path, body))


def test_values_older_versions_wrote_are_named(tmp_path: Path) -> None:
    settings = load(_root(tmp_path, '[watchdog]\nmode = "watch"\nslow_frames = "watch"\n'
                                    '[setup]\nshare = ["game", "content", "bans"]\n'
                                    '[server_updates]\ncheck_hours = 6\n'
                                    '[updates]\nrepository = "LittleGroove/Project-Reclaimer-Server-Supervisor"\n'))
    notes = "\n".join(settings.notes)
    assert '[watchdog] mode = "watch" is what 0.1 and 0.2.0 wrote: the watchdog only logs' in notes
    assert "Since 0.2.1 the default is \"act\"" in notes
    assert '[watchdog] slow_frames = "watch" is 0.2.1\'s one switch' in notes
    assert '[setup] share = ["game", "content", "bans"] is what 0.2.0 and 0.2.1 wrote' in notes
    assert "[server_updates] check_hours = 6 is what 0.2.0 and 0.2.1 wrote" in notes
    assert "old name of Huragok's repository" in notes
    assert settings.retired == [] and settings.problems == []  # legal choices: notes, never problems
    today = load(_root(tmp_path, '[watchdog]\nmode = "act"\n[setup]\nshare = ["game", "bans"]\n'))
    assert today.notes == []


def test_a_retired_setting_is_named_under_the_section_it_is_in(tmp_path: Path) -> None:
    """0.1 wrote [windows] for what is [process] now."""
    old = load(_root(tmp_path, 'backend = "windows"\n[windows]\ndisable_auto_update = true'))
    assert old.retired and old.retired[0].startswith("settings.toml [windows] disable_auto_update does nothing")
    new = load(_root(tmp_path, 'backend = "process"\n[process]\ndisable_auto_update = true'))
    assert new.retired[0].startswith("settings.toml [process] disable_auto_update does nothing")


# One slug, one server: the per-server files are named by it ---------------------------------------------------


def test_two_names_with_one_slug_leave_the_later_one_out(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    path = write_root(root, list(zip(["LG Slayer", "LG-Slayer", "Halo 3 [EU]", "Halo 3 EU"], free_ports(4))))
    settings = load(path)
    assert [s.name for s in settings.servers] == ["Halo 3 [EU]", "LG Slayer"]
    texts = [str(p) for p in settings.problems]
    assert len(texts) == 2, texts
    assert "would share their records and logs: Huragok names both lg-slayer (records/servers/lg-slayer.json" in \
        texts[1] and "Rename one of them; 'LG-Slayer' is left out until then" in texts[1]
    assert [p.folder for p in settings.problems] == [(root / "SERVERS" / "Halo 3 EU").resolve(),
                                                    (root / "SERVERS" / "LG-Slayer").resolve()]
    with pytest.raises(ConfigError, match="would share their records"):
        discover_servers(settings)


def test_the_running_server_keeps_its_slug_when_a_new_folder_clashes(tmp_path: Path) -> None:
    root = tmp_path / "reclaimer"
    settings = load(write_root(root, [("LG Slayer", free_port())]))
    write_server_folder(root / "SERVERS" / "A copy", free_port(), name="LG-Slayer")  # sorts first
    problems: list = []
    assert [s.name for s in discover_servers(settings, problems)] == ["LG Slayer"]
    assert problems and problems[0].folder == (root / "SERVERS" / "A copy").resolve()


def test_a_name_with_no_latin_letters_gets_a_slug_of_its_own() -> None:
    assert slugify("Сервер") == slugify("Сервер ") != slugify("サーバー")
    assert slugify("Сервер").startswith("server-") and len(slugify("Сервер")) == len("server-") + 8
    assert slugify("LG Test Server") == "lg-test-server"


def test_slug_taken_names_the_server_a_name_would_share_files_with(tmp_path: Path) -> None:
    from huragok.core.serverfolders import slug_taken

    settings = load(_root(tmp_path, "", list(zip(["LG Slayer", "Other"], free_ports(2)))))
    assert slug_taken(settings, "LG-Slayer").name == "LG Slayer"
    assert slug_taken(settings, "lg slayer!").name == "LG Slayer"
    assert slug_taken(settings, "LG-Slayer", ignore="LG Slayer") is None  # renaming it to that is fine
    assert slug_taken(settings, "New one") is None


def test_a_server_is_found_by_name_slug_or_number(tmp_path: Path) -> None:
    settings = load(_root(tmp_path, "", list(zip(["LG Test Server", "2", "Zulu"], free_ports(3)))))
    assert settings.server("lg test server").name == "LG Test Server"
    assert settings.server("lg-test-server").name == "LG Test Server"
    assert settings.server("2").name == "2"  # a server really named 2 wins over the second in the list
    # In the list's order (folders by name): 2, LG Test Server, Zulu.
    assert settings.server("3").name == "Zulu" and settings.server("#2").name == "LG Test Server"
    with pytest.raises(KeyError):
        settings.server("4")
