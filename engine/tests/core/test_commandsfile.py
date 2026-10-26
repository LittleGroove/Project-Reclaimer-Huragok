"""commands.toml: who's who, and which role may use which command."""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import pytest

from conftest import MOD, OWNER

from huragok.core.commandsfile import (
    HELP, SECTIONS, PermissionsError, load_permissions, reload_if_changed, render_commands_toml,
)
from huragok.core.part import commands_of
from huragok.parts import PARTS


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "commands.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_roles_list_players_and_commands(tmp_path: Path) -> None:
    permissions = load_permissions(write(tmp_path, f"""
prefix = "?"
cooldown_seconds = 5
[everyone]
commands = ["players"]
[moderator]
players = ["{MOD.upper()}"]
commands = ["?ban"]
[owner]
players = ["{OWNER}"]
commands = ["say"]
[off]
commands = ["map"]
"""))
    assert permissions.members == {"owner": {OWNER}, "moderator": {MOD}, "member": set()}  # IDs in any case
    assert permissions.roles == {"players": "player", "ban": "moderator", "say": "owner"}
    assert permissions.off == {"map"} and permissions.prefix == "?" and permissions.cooldown == 5
    also_off = load_permissions(write(tmp_path, '[admin]\ncommands = ["say"]\n[off]\ncommands = ["say"]'))
    assert also_off.off == {"say"}  # [off] wins: putting a command there is enough
    kick = next(s for s in commands_of(PARTS["commands"]) if s.name == "kick")
    assert permissions.role_for(kick) == "moderator"  # not listed: its default


def test_a_missing_file_keeps_every_default(tmp_path: Path) -> None:
    permissions = load_permissions(tmp_path / "commands.toml")
    assert permissions.roles == {} and permissions.members == {"owner": set(), "moderator": set(), "member": set()}


@pytest.mark.parametrize("text, message", [
    ('[moderator]\ncommands = ["mute"]\n[admin]\ncommands = ["mute"]', "both \\[moderator\\] and \\[admin\\]"),
    ('[admin]\nplayers = []', "server's own"),
    ('[everyone]\nplayers = []', "only \\[member\\], \\[moderator\\] and \\[owner\\]"),
    ('[moderator]\nplayers = ["Alice"]', "Alice is not a player ID"),
    ('[trusted]\ncommands = []', "\\[trusted\\] isn't a role"),
    ('[moderator]\ncommands = "mute"', "list of text"),
    ('[moderator]\ncan = []', "players and commands"),
    ('prefix = "/"', "prefix"),
    ('[off]\ncommands = ["help"]', "can't be switched off"),
    ("[moderator\n", "commands.toml"),
])
def test_mistakes_say_what_is_wrong(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(PermissionsError, match=message):
        load_permissions(write(tmp_path, text))


def test_a_command_this_version_doesnt_have_is_left_out_with_why(tmp_path: Path) -> None:
    """A typo, or a newer version's command left listed when an update is put back: the line does nothing, and
    check and the log say so, but the file is read and Huragok starts. Up to 0.4.4 it stopped Huragok starting."""
    permissions = load_permissions(write(tmp_path, '[moderator]\ncommands = ["mutee", "kick"]\n'
                                                   '[admin]\ncommands = ["somedaycommand"]\n[off]\ncommands = ["sya"]'))
    assert permissions.roles == {"kick": "moderator"} and permissions.off == set()
    assert len(permissions.unknown) == 3
    assert "commands.toml [moderator]: there's no !mutee in Huragok" in permissions.unknown[0]
    assert permissions.unknown[0].endswith("so that line does nothing (did you mean mute?)")
    assert permissions.unknown[1].endswith("(from a newer version, perhaps)")
    assert "[off]: there's no !sya" in permissions.unknown[2] and "(did you mean say?)" in permissions.unknown[2]


def test_edits_are_picked_up_and_a_broken_one_is_reported_once(tmp_path: Path) -> None:
    path = write(tmp_path, '[off]\ncommands = ["say"]')
    current = load_permissions(path)
    assert reload_if_changed(current) is None
    path.write_text(f'[moderator]\nplayers = ["{MOD}"]', encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    fresh = reload_if_changed(current)
    assert fresh is not None and fresh.members["moderator"] == {MOD} and not fresh.off
    path.write_text("[moderator\n", encoding="utf-8")
    os.utime(path, (time.time() + 10, time.time() + 10))
    with pytest.raises(PermissionsError):
        reload_if_changed(fresh)
    assert reload_if_changed(fresh) is None  # not again every few seconds


@pytest.mark.parametrize("gone", ["deleted", "emptied", "blanked"])
def test_a_file_gone_missing_or_empty_while_running_keeps_who_is_listed(tmp_path: Path, gone: str) -> None:
    # Some editors empty or replace the file while they save: that isn't "nobody is listed".
    path = write(tmp_path, f'[moderator]\nplayers = ["{MOD}"]\n')
    current = load_permissions(path)
    if gone == "deleted":
        path.unlink()
    else:
        path.write_text("" if gone == "emptied" else "  \n\n", encoding="utf-8")
        os.utime(path, (time.time() + 5, time.time() + 5))
    with pytest.raises(PermissionsError, match="commands.toml is missing or empty"):
        reload_if_changed(current)
    assert reload_if_changed(current) is None  # said once, and what was in force stays
    assert current.members["moderator"] == {MOD}
    path.write_text(f'[moderator]\nplayers = ["{MOD}", "{OWNER}"]\n', encoding="utf-8")  # the save finishes
    os.utime(path, (time.time() + 10, time.time() + 10))
    fresh = reload_if_changed(current)
    assert fresh is not None and fresh.members["moderator"] == {MOD, OWNER}


def test_the_written_file_lists_every_command_once_under_its_default_role(tmp_path: Path) -> None:
    text = render_commands_toml()
    data = tomllib.loads(text)
    listed = [c for section in (*SECTIONS, "off") for c in data[section]["commands"]]
    every = {HELP.name} | {s.name for cls in PARTS.values() for s in commands_of(cls)}
    assert sorted(listed) == sorted(every)
    assert load_permissions(write(tmp_path, text)).roles == {
        s.name: s.role for cls in PARTS.values() for s in commands_of(cls)} | {"help": "player"}
    assert '"mute",' in text and "# Nobody hears their voice" in text  # each with what it does


def test_one_role_each_and_check_names_anyone_listed_twice(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    bravo = "d" * 64
    path.write_text(f'[owner]\nplayers = ["{OWNER}"]\n[moderator]\nplayers = ["{MOD}", "{bravo}"]\n'
                    f'[member]\nplayers = ["{bravo}"]\n', encoding="utf-8")
    permissions = load_permissions(path)
    assert permissions.doubles() == {bravo: ["moderator", "member"]}
    assert permissions.doubles([OWNER.upper(), MOD]) == {OWNER: ["owner", "admin"], MOD: ["admin", "moderator"],
                                                         bravo: ["moderator", "member"]}


def test_a_change_within_one_tick_of_the_clock_is_still_seen(tmp_path: Path) -> None:
    """Windows moves file times in steps of about 16 ms: a second save in the same step (two !promote at once) left
    the time as it was, and the roles in force stayed the old ones until the file changed again."""
    path = tmp_path / "commands.toml"
    path.write_text(f'[moderator]\nplayers = ["{MOD}"]\n', encoding="utf-8")
    current = load_permissions(path)
    stamp = path.stat().st_mtime
    path.write_text(f'[owner]\nplayers = ["{MOD}"]\n', encoding="utf-8")
    os.utime(path, (stamp, stamp))  # the same time, as two saves in one tick get
    fresh = reload_if_changed(current)
    assert fresh is not None and MOD in fresh.members["owner"] and MOD not in fresh.members["moderator"]
    assert reload_if_changed(fresh) is None  # nothing changed since: nothing read again
    assert reload_if_changed(fresh, force=True) is not None  # Huragok just wrote it: read now, whatever
