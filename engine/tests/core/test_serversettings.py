"""One server's settings as in force (core/serversettings.py): its own [[server]] block, else every server's, with
the same answer everywhere it's asked (the manager, the backend, Vergil, the window, `check`, the status card)."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import free_ports, write_root

from huragok.core.serversettings import CHANGEABLE, OVERRIDES, SERVER_KEYS, automod_mode
from huragok.core.settings import load


def settings_with(tmp_path: Path, body: str = "", backend: str = "attach") -> object:
    alpha, bravo = free_ports(2)
    return load(write_root(tmp_path / "root", [("Alpha", alpha), ("Bravo", bravo)], body, backend=backend))


def test_a_server_without_a_block_has_every_servers_settings(tmp_path: Path) -> None:
    settings = settings_with(tmp_path, '[restarts]\ndaily_restart = "04:00"\nwarn_seconds = [120, 30]\n'
                                       '[workshop_updates]\nrestart = false\n', backend="process")
    alpha = settings.of("Alpha")
    assert alpha.daily_restart == "04:00" and alpha.where("daily_restart") == "[restarts]"
    assert alpha.restart("warn_seconds") == [120.0, 30.0]
    assert alpha.workshop_updates == "off" and alpha.where("workshop_updates") == "[workshop_updates]"
    assert not alpha.workshop_restart
    assert alpha.priority == "high" and alpha.where("priority") == "[process]"
    assert alpha.automod == "on" and alpha.autostart is True


def test_a_servers_own_block_wins_for_it_alone(tmp_path: Path) -> None:
    settings = settings_with(tmp_path, '[restarts]\ndaily_restart = "04:00"\n'
                                       '[[server]]\nname = "Alpha"\ndaily_restart = ""\npriority = "realtime"\n'
                                       'automod = "dry_run"\nworkshop_updates = "off"\nmin_uptime_seconds = 60\n',
                             backend="process")
    alpha, bravo = settings.of("Alpha"), settings.of("Bravo")
    assert alpha.daily_restart == "" and alpha.where("daily_restart") == "settings.toml"  # its own: none at all
    assert bravo.daily_restart == "04:00"
    assert (alpha.priority, bravo.priority) == ("realtime", "high")
    assert (alpha.automod, bravo.automod) == ("dry_run", "on")
    assert (alpha.workshop_updates, bravo.workshop_updates) == ("off", "on")  # [workshop_updates] restart: on
    assert alpha.restart("min_uptime_seconds") == 60.0 and bravo.restart("min_uptime_seconds") == 900.0


def test_only_the_process_backend_runs_servers_at_a_priority(tmp_path: Path) -> None:
    settings = settings_with(tmp_path, '[[server]]\nname = "Alpha"\npriority = "realtime"\n')  # attach
    assert settings.of("Alpha").priority == "off" == settings.of("Bravo").priority


def test_vergil_is_off_where_automod_is_switched_off_and_follows_every_dry_run(tmp_path: Path) -> None:
    off = settings_with(tmp_path / "off", '[automod]\nenabled = false\n[[server]]\nname = "Alpha"\nautomod = "on"\n')
    assert off.of("Alpha").automod == "off" and off.of("Alpha").where("automod") == "[automod] enabled = false"
    everything = settings_with(tmp_path / "all", 'dry_run = true\n')  # all of Huragok in dry run: Vergil too
    assert everything.of("Alpha").automod == "dry_run"
    part = settings_with(tmp_path / "part", '[automod]\ndry_run = true\n[[server]]\nname = "Bravo"\nautomod = "on"\n')
    assert (part.of("Alpha").automod, part.of("Bravo").automod) == ("dry_run", "on")
    assert automod_mode(None, True) == "dry_run" and automod_mode("off", True) == "off" and automod_mode("x", False) == "on"
    # All of Huragok in dry run holds every action back (the ledger's rule), so a server's own "on" reads as a dry
    # run there: players aren't told of strikes that never land. Its own "off" stays off.
    held = settings_with(tmp_path / "held", 'dry_run = true\n[[server]]\nname = "Bravo"\nautomod = "on"\n')
    assert held.of("Bravo").automod == "dry_run"
    assert automod_mode("on", False, everything_dry=True) == "dry_run" and automod_mode("off", False, True) == "off"


def test_a_shared_content_folder_is_said_with_what_it_costs(tmp_path: Path) -> None:
    settings = settings_with(tmp_path)
    alpha, bravo = settings.servers
    bravo.content_dir = alpha.content_dir = tmp_path / "content"
    assert settings.of(alpha).shares_content_with() == ["Bravo"]
    assert settings.of(alpha).workshop_updates == "on" and settings.of(alpha).workshop_restart
    assert settings.of(alpha).workshop_text().endswith("(content shared with Bravo: one update reaches all of them at "
                                                       "once)")
    bravo.content_dir = tmp_path / "its own"
    assert settings.of(alpha).workshop_text() == ("the server installs its own mod updates; Huragok restarts it when "
                                                  "its map in use holds one up")


def test_0_6s_workshop_settings_are_read_and_do_nothing(tmp_path: Path) -> None:
    """install, check_minutes (even one 0.6 refused), adopt and layouts: accepted, never an error; install = "off"
    (no automatic mod swaps, the host's choice) reads as restart = false until the line goes. A server's own
    after_game or when_empty reads as unset."""
    settings = settings_with(tmp_path, '[workshop_updates]\ninstall = "off"\ncheck_minutes = 2\nadopt = false\n'
                                       'layouts = true\n[[server]]\nname = "Bravo"\nworkshop_updates = "when_empty"\n')
    assert settings.workshop_updates.restart is False
    assert settings.of("Alpha").workshop_updates == "off" and settings.of("Bravo").workshop_updates == "off"
    assert settings.retired == [] and settings.problems == []
    on = settings_with(tmp_path / "on", '[workshop_updates]\ninstall = "after_game"\nrestart = true\n'
                                        '[[server]]\nname = "Bravo"\nworkshop_updates = "after_game"\n')
    assert on.of("Bravo").workshop_updates == "on" and on.of("Bravo").where("workshop_updates") == "[workshop_updates]"


def test_a_server_huragok_doesnt_list_gets_every_servers_settings(tmp_path: Path) -> None:
    settings = settings_with(tmp_path, '[restarts]\ndaily_restart = "03:30"\n')
    nobody = settings.of("Not Here")
    assert nobody.daily_restart == "03:30" and nobody.automod == "on" and nobody.shares_content_with() == []


@pytest.mark.parametrize("key", sorted(OVERRIDES))
def test_every_per_server_setting_has_a_value_in_force(tmp_path: Path, key: str) -> None:
    settings = settings_with(tmp_path)
    server = settings.of("Alpha")
    server.value(key)  # never raises, for any setting a block may hold
    assert server.where(key)


def test_the_table_is_what_a_block_may_hold_and_the_window_may_change() -> None:
    assert SERVER_KEYS == {"name", *OVERRIDES}
    assert set(CHANGEABLE) <= set(OVERRIDES)
    for key in CHANGEABLE:
        override = OVERRIDES[key]
        assert override.choices or key in ("autostart", "daily_restart"), f"{key} is changed without its choices"
