"""D: Huragok's own files a step at a time (settings.toml, commands.toml, automod.txt, names.txt), a server's
mods switched on or off, and O, which opens the folder or any file as it is."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import MOD, OWNER
from testkit.screens import screen, template_root, text_of
from testkit.setup import ALPHA

from huragok.content.catalogue import DisabledModsError, disabled_mods, set_mod_disabled
from huragok.core import desktop
from huragok.core import hostsettings as hs
from huragok.core.commandsfile import (
    ROLES, PermissionsError, add_player, all_commands, load_permissions, player_notes, remove_player, set_command_off,
    set_command_role, set_top,
)
from huragok.core.storage import Storage, recent_players
from huragok.core.tomledit import (
    TomlEditError, add_array_block, read_toml, remove_array_block, remove_top_key, set_array_block_key,
)
from huragok.players.automod.automod import same_name
from huragok.players.automod.rules import (
    RuleError, add_name, change_rule, ladder, line_of, parse_line, read_names, read_rules, remove_name, rule_line,
)
from huragok.window.dashboard import key_rows

BRAVO = "c" * 64


# settings.toml ---------------------------------------------------------------------------------------------

def test_every_setting_the_template_writes_is_in_the_catalogue(tmp_path: Path) -> None:
    path = template_root(tmp_path)
    data = read_toml(path)
    known = {(s.section, s.key) for s in hs.SETTINGS}
    written = {("", key) for key, value in data.items() if not isinstance(value, dict)}
    written |= {(section, key) for section, table in data.items() if isinstance(table, dict)
                for key in table if key != "user"}
    assert written - known == set()
    # [docker] and [command] are there only with their backend; mcc_folder is optional (left out: link instead).
    assert {s.section for s in hs.SETTINGS} - set(data) - {""} == set()
    groups = [key for key, *_ in hs.GROUPS]
    assert all(s.group in groups for s in hs.SETTINGS) and all(any(s.group == g for s in hs.SETTINGS) for g in groups)
    entries = {(s.section, s.key): (v, where) for s, v, where in hs.catalogue(path)}
    assert entries[("automod", "dry_run")] == (True, "the file") and entries[("setup", "mcc_folder")] == (None, "left out")
    assert ("docker", "compose_file") not in entries  # backend attach
    assert hs.problems(path) == []


def test_a_change_is_checked_as_huragok_reads_the_file_before_it_is_made(tmp_path: Path) -> None:
    path = template_root(tmp_path)
    before = path.read_text(encoding="utf-8")
    hs.write(path, hs.find("automod", "dry_run"), False)
    hs.write(path, hs.find("restarts", "warn_seconds"), [120, 30])
    hs.write(path, hs.find("watchdog", "drop_share"), 0.75)
    hs.write(path, hs.find("setup", "mcc_folder"), "D:\\Games\\MCC")
    data = read_toml(path)
    assert data["automod"]["dry_run"] is False and data["restarts"]["warn_seconds"] == [120, 30]
    assert data["watchdog"]["drop_share"] == 0.75 and data["setup"]["mcc_folder"] == "D:\\Games\\MCC"
    text = path.read_text(encoding="utf-8")
    assert "# Vergil acts on the words in automod.txt said in chat" in text  # the comments stay
    assert len(text.splitlines()) == len(before.splitlines())  # the commented mcc_folder example came to life
    hs.write(path, hs.find("setup", "mcc_folder"), None)  # left out again: linked from a server that has them
    assert "mcc_folder" not in read_toml(path)["setup"]
    # A change Huragok would refuse to start with is never written.
    unchanged = path.read_text(encoding="utf-8")
    with pytest.raises(hs.HostSettingsError, match="should look like owner/name"):
        hs.write(path, hs.find("server_updates", "repository"), "not a repository")
    with pytest.raises(hs.HostSettingsError, match="servers_folder|server_folder|not found"):
        hs.write(path, hs.find("", "server_folder"), "no-such-folder")
    assert path.read_text(encoding="utf-8") == unchanged
    assert not list(path.parent.glob(".settings.toml.*"))  # no copy left behind
    # A problem the file had already isn't the change's: it doesn't stop an unrelated one.
    (path.parent / "automod.txt").write_text("bad | 5x\n", encoding="utf-8")
    assert hs.problems(path) and "automod" in hs.problems(path)[0]
    hs.write(path, hs.find("restarts", "daily_restart"), "04:00")
    assert read_toml(path)["restarts"]["daily_restart"] == "04:00"


def test_typed_values_are_read_as_the_file_takes_them() -> None:
    assert hs.parse(hs.find("restarts", "after_game_seconds"), "20") == 20  # a whole number stays one
    assert hs.parse(hs.find("watchdog", "slow_share"), ".5") == 0.5
    assert hs.parse(hs.find("restarts", "backoff_seconds"), "5, 10; 30") == [5, 10, 30]
    assert hs.parse(hs.find("restarts", "daily_restart"), "4:05") == "04:05"
    assert hs.parse(hs.find("process", "command"), 'wine "C:/Program Files/x.exe" run') == \
        ["wine", "C:/Program Files/x.exe", "run"]
    for setting, text, why in ((hs.find("restarts", "max_failures"), "0", "between 1 and 100"),
                               (hs.find("watchdog", "drop_share"), "2", "between 0 and 1"),
                               (hs.find("workshop_updates", "check_minutes"), "3", "5 at least"),
                               (hs.find("restarts", "faulted_retry_minutes"), "30, 0", "each 1 or more"),
                               (hs.find("restarts", "daily_restart"), "25:00", "24-hour clock"),
                               (hs.find("watchdog", "dead"), "restart", "one of watch, act, actnow")):
        with pytest.raises(hs.HostSettingsError, match=why):
            hs.parse(setting, text)


def test_moderators_of_the_console_relay(tmp_path: Path) -> None:
    path = template_root(tmp_path)
    hs.add_relay_user(path, "Alice", "alice-long-password", "moderator")
    hs.add_relay_user(path, "Bob", "bob-long-password", "admin")
    assert [(u.name, u.role) for u in hs.relay_users(path)] == [("Alice", "moderator"), ("Bob", "admin")]
    text = path.read_text(encoding="utf-8")
    assert text.index('# role = "moderator"') < text.index("\n[[relay.user]]") < text.index("# ── HURAGOK ──")
    for name, password, role, why in (("Carol", "bob-long-password", "moderator", "has that password"),
                                      ("alice", "another-password", "moderator", "named Alice already"),
                                      ("Dee", "short", "moderator", "8 to 128"),
                                      ("Dee", "dee-long-password", "god", "the role is one of")):
        with pytest.raises(hs.HostSettingsError, match=why):
            hs.add_relay_user(path, name, password, role)
    hs.change_relay_user(path, "alice", role="owner")
    hs.change_relay_user(path, "Bob", password="bob-new-password")
    hs.remove_relay_user(path, "Alice")
    users = read_toml(path)["relay"]["user"]
    assert users == [{"name": "Bob", "password": "bob-new-password", "role": "admin"}]
    assert hs.problems(path) == []
    hs.remove_relay_user(path, "Bob")  # the last one: [relay] keeps its own settings
    assert "user" not in read_toml(path)["relay"] and read_toml(path)["relay"]["port_offset"] == 1000
    with pytest.raises(OSError, match="settings.toml: there's no"):
        hs.remove_relay_user(path, "Bob")  # the refusal names the file, not the copy it was tried on


def test_array_blocks_and_top_keys_are_changed_in_place(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_text('name = "x"  # a note\n\n[relay]\nenabled = true\n# Moderators below.\n\n[rcon]\nuse_host_key = true\n',
                    encoding="utf-8")
    add_array_block(path, ("relay", "user"), {"name": "A", "password": "aaaaaaaa", "role": "moderator"})
    add_array_block(path, ("relay", "user"), {"name": "B", "password": "bbbbbbbb", "role": "admin"})
    text = path.read_text(encoding="utf-8")
    assert text.index("# Moderators below.") < text.index('name = "A"') < text.index('name = "B"') < text.index("[rcon]")
    set_array_block_key(path, ("relay", "user"), "b", "role", "owner")
    remove_array_block(path, ("relay", "user"), "A")
    assert read_toml(path)["relay"] == {"enabled": True, "user": [{"name": "B", "password": "bbbbbbbb", "role": "owner"}]}
    with pytest.raises(TomlEditError, match="no \\[\\[relay.user\\]\\] block named C"):
        remove_array_block(path, ("relay", "user"), "C")
    only = tmp_path / "only.toml"
    only.write_text("a = 1\n", encoding="utf-8")
    add_array_block(only, ("relay", "user"), {"name": "A"})
    remove_array_block(only, ("relay", "user"), "A")  # the last one: a table only the blocks made goes too
    assert read_toml(only) == {"a": 1}
    assert remove_top_key(path, "name") and not remove_top_key(path, "name") and not remove_top_key(path, "rcon")
    assert "name" not in read_toml(path) and "# Moderators below." in path.read_text(encoding="utf-8")


# commands.toml ---------------------------------------------------------------------------------------------

def test_who_is_who_and_who_may_use_which_command(tmp_path: Path) -> None:
    path = template_root(tmp_path).parent / "commands.toml"
    assert add_player(path, "moderator", BRAVO.upper(), "Bravo: added in the window") == []
    assert player_notes(path)[BRAVO] == "Bravo: added in the window"
    assert add_player(path, "member", BRAVO, "Bravo: a member now") == ["moderator"]  # one role each
    members = load_permissions(path).members
    assert BRAVO in members["member"] and BRAVO not in members["moderator"]
    assert remove_player(path, "member", BRAVO) and not remove_player(path, "member", BRAVO)
    with pytest.raises(PermissionsError, match="not a player ID"):
        add_player(path, "owner", "Alice", "")
    with pytest.raises(PermissionsError, match="admins are the servers' own"):
        add_player(path, "admin", OWNER, "")
    set_command_role(path, "kick", "admin")
    permissions = load_permissions(path)
    assert permissions.roles["kick"] == "admin" and "# " + all_commands()["kick"].help in path.read_text(encoding="utf-8")
    set_command_off(path, "kick", True)
    assert "kick" in load_permissions(path).off
    set_command_off(path, "kick", False)
    assert "kick" not in load_permissions(path).off and load_permissions(path).roles["kick"] == "admin"
    unchanged = path.read_text(encoding="utf-8")
    with pytest.raises(PermissionsError, match="not changed .*keeps lines starting with /"):
        set_top(path, "prefix", "/")
    assert path.read_text(encoding="utf-8") == unchanged
    set_top(path, "prefix", "?")
    assert load_permissions(path).prefix == "?"
    assert MOD not in load_permissions(path).members["moderator"]  # the template lists nobody


# automod.txt and names.txt ---------------------------------------------------------------------------------

def test_rules_are_written_in_their_five_parts_and_read_back(tmp_path: Path) -> None:
    assert rule_line(["badword", "-badwordy"], ["1h"], 1, ["1d", "7d"], 3, "slurs") == \
        "badword, -badwordy | 1h | 1, 1d, 7d | 3 | slurs"
    assert rule_line(["kickword"], [], None, [], None, "") == "kickword"
    assert rule_line(["word"], [], None, [], 0, "") == "word | | | 0"  # four parts ending in a number: not 0.2's
    rule = parse_line("badword, -badwordy | 1h | 1, 1d, 7d | 3 | slurs", 1)
    assert rule is not None and line_of(rule) == "badword, -badwordy | 1h | 1, 1d, 7d | 3 | slurs"
    assert ladder(rule) == ["1st: removed for 1 hour", "2nd: muted for 1 day", "3rd: muted for 7 days",
                            "4th: banned for good"]
    assert ladder(parse_line("w | | 0, 1d, 3d", 1)) == ["1st: muted for 1 day", "2nd and after: muted for 3 days"]
    assert ladder(parse_line("w", 1)) == ["every time: kicked"]
    assert ladder(parse_line("w | | 2, 1d | 9", 1)) == ["1st and 2nd: kicked", "3rd to 9th: muted for 1 day",
                                                        "10th: banned for good"]  # strikes that do the same, as one
    path = template_root(tmp_path).parent / "automod.txt"
    header = path.read_text(encoding="utf-8")
    change_rule(path, None, "badword | 1h | | | rude")
    rules = change_rule(path, None, "worstword | | | 0 | threats")
    assert [r.name for r in rules] == ["rude", "threats"]
    line = rules[0].line
    change_rule(path, line, "badword, rudeword | 1h | | | rude")
    with pytest.raises(RuleError, match="two lines are named threats"):
        change_rule(path, line, "badword | | | | threats")
    with pytest.raises(RuleError, match="isn't a time"):
        change_rule(path, None, "w | soon")
    change_rule(path, rules[1].line, None)
    assert [(r.name, r.words) for r in read_rules(path)] == [("rude", ["badword", "rudeword"])]
    assert path.read_text(encoding="utf-8").startswith(header)  # its notes for the host stay at the top
    with pytest.raises(RuleError, match="isn't a rule any more"):
        change_rule(path, 1, "x")  # line 1 is a note


def test_banned_names_are_added_and_taken_off_with_their_notes(tmp_path: Path) -> None:
    path = tmp_path / "names.txt"
    add_name(path, "BadName", "Alice", "impersonating an admin")
    add_name(path, "*slur*", "Bob")
    text = path.read_text(encoding="utf-8")
    assert "# Vergil's banned names" in text and "by Alice: impersonating an admin\nBadName\n" in text
    assert [n.text for n in read_names(path)] == ["BadName", "*slur*"]
    assert remove_name(path, "BADNAME", same_name) == 1 and "impersonating" not in path.read_text(encoding="utf-8")
    assert remove_name(path, "BadName", same_name) == 0 and [n.text for n in read_names(path)] == ["*slur*"]


# A server's mods, and the players seen -------------------------------------------------------------------------

def test_mods_switched_off_and_on(tmp_path: Path) -> None:
    content = tmp_path / "content"
    (content / "Mods").mkdir(parents=True)
    assert disabled_mods(content) == []
    assert set_mod_disabled(content, "Mods/canyon", True) == ["Mods/canyon"]
    assert json.loads((content / "disabled-mods.json").read_text(encoding="utf-8")) == {"packages": ["Mods/canyon"]}
    assert set_mod_disabled(content, "Mods/Canyon", False) == [] and not (content / "disabled-mods.json").exists()
    (content / "disabled-mods.json").write_text('{"mods": ["Mods/canyon"]}', encoding="utf-8")
    with pytest.raises(DisabledModsError, match="isn't written as"):
        set_mod_disabled(content, "Mods/canyon", True)  # a shape this doesn't know is never written over
    assert json.loads((content / "disabled-mods.json").read_text(encoding="utf-8")) == {"mods": ["Mods/canyon"]}


def test_the_players_seen_lately(tmp_path: Path) -> None:
    assert recent_players(tmp_path / "none.sqlite3") == [] and recent_players(None) == []
    storage = Storage(tmp_path / "records.sqlite3")
    storage.record_join(OWNER, "Alice", None, when=100.0)
    storage.record_join(BRAVO, "Bravo", None, when=200.0)
    storage.close()
    assert recent_players(tmp_path / "records.sqlite3") == [(BRAVO, "Bravo", 200.0), (OWNER, "Alice", 100.0)]


# The screens -------------------------------------------------------------------------------------------------

def test_the_dashboard_has_o_open_and_no_e() -> None:
    keys = " ".join(text for _, text, _ in key_rows(None))
    assert "[O] Open" in keys and "[E]" not in keys and "[D] Settings" in keys


def test_d_changes_settings_toml_a_step_at_a_time(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    keys = ["S", "ENTER",  # D's list: How Huragok runs
            "7", "ENTER",  # Automod
            "2", "ENTER", "N",  # dry_run off
            "7", "ENTER", *"3,4,5", "ENTER",  # exempt_roles: moderator, admin, owner
            "ESC",
            "8", "ENTER", "M", "ENTER",  # the relay's moderators
            "A", "ENTER", *"Alice", "ENTER", *"alice-long-password", "ENTER", "1",  # Alice, moderator
            "1", "ENTER", "R", "2",  # Alice: admin
            "ESC", "ESC",
            *"10", "ENTER", "4", "ENTER", *"not a repository", "ENTER", "x",  # refused: not owner/name
            "ESC", "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    data = read_toml(path)
    assert data["automod"]["dry_run"] is False and data["automod"]["exempt_roles"] == ["moderator", "admin", "owner"]
    assert data["relay"]["user"] == [{"name": "Alice", "password": "alice-long-password", "role": "admin"}]
    assert data["server_updates"]["repository"] == "ProjectReclaimer/project-reclaimer-releases"
    assert "dry_run = off, written to settings.toml" in text and "when Huragok starts again" in text
    assert "Alice added as moderator" in text and "Alice is admin now" in text
    assert "Not changed ([server_updates] repository = not a repository)" in text and "owner/name" in text
    assert "alice-long-password" not in text  # typed as stars
    assert "Vergil's word list" in text and ALPHA in text  # D's list: Huragok's files above the servers


def test_d_changes_who_is_who_and_the_commands(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    storage = Storage(path.parent / "records" / "huragok.sqlite3")
    storage.record_join(BRAVO, "Bravo", None)
    storage.close()
    def number(role_of_kick: str) -> str:  # where !kick is in the list, which is in order of role
        ordered = sorted(all_commands().values(), key=lambda s: (ROLES.index(role_of_kick if s.name == "kick"
                                                                             else s.role), s.name))
        return str(1 + [s.name for s in ordered].index("kick"))

    keys = ["C", "ENTER",
            "2", "A", "ENTER", "1", "ENTER", "ESC",  # Moderators: add Bravo, seen lately
            "3", "A", "ENTER", "1", "ENTER", "1", "ENTER", "Y", "ESC",  # Members: Bravo (out of moderators); out again
            "4", *number("moderator"), "ENTER", "4", *number("admin"), "ENTER", "O", "ESC",  # kick: admin, then off
            "5", "/", "ENTER", "x",  # prefix /: refused
            "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    permissions = load_permissions(path.parent / "commands.toml")
    assert BRAVO not in permissions.members["moderator"] and BRAVO not in permissions.members["member"]
    assert permissions.roles["kick"] == "admin" and "kick" in permissions.off
    assert "Bravo is a moderator now." in text and "Bravo is a member now (no longer moderator: one role each)." in text
    assert "Bravo isn't a member any more." in text and "!kick: admin and up." in text and "!kick is off." in text
    assert "keeps lines starting with /" in text


def test_d_adds_vergils_rules_and_banned_names(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    keys = ["A", "ENTER",
            "A", "ENTER", *"badword, worseword", "ENTER", "3", *"slurs", "ENTER", "Y",  # 1h, mutes, then the ban
            "T", "ENTER", *"what a b4dw0rd", "ENTER", "x",  # caught, disguised
            "1", "ENTER", "S", "1", "Y",  # kicked every time instead
            "A", "ENTER", *"rudeword", "ENTER", "5", *"30m", "ENTER", "Y", *"2", "ENTER", *"1d, perm", "ENTER", "N",
            "ENTER", "Y",  # its own: out 30 min, two strikes then muted 1 day, then for good; unnamed
            "ESC",
            "N", "ENTER",
            "A", "ENTER", *"BadName", "ENTER", *"impersonation", "ENTER",
            "T", "ENTER", *"B4dName", "ENTER", "x",
            "T", "ENTER", *"worseword fan", "ENTER", "x",  # a name with a word from the word list
            "1", "ENTER", "Y",  # taken off again
            "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    rules = read_rules(path.parent / "automod.txt")
    assert [line_of(r) for r in rules] == ["badword, worseword | | | | slurs", "rudeword | 30m | 2, 1d, perm"]
    assert "1st: removed for 1 hour" in text and "4th: banned for good" in text and "Added." in text
    assert "'b4dw0rd' is caught by slurs" in text and "Saved." in text
    assert "3rd: muted for 1 day" in text and "has no name, so its strikes are kept under rule2" in text
    assert read_names(path.parent / "names.txt") == []
    assert "BadName is banned" in text and "the banned name 'BadName'" in text and "BadName can be used again." in text
    assert "it has 'worseword' in it, the word list's worseword (slurs line)" in text


def test_d_switches_a_servers_mods_and_o_opens_everything(tmp_path: Path, run, monkeypatch) -> None:
    path = template_root(tmp_path)
    folder = path.parent / "SERVERS" / ALPHA
    mod = folder / "content" / "Mods" / "canyon"
    mod.mkdir(parents=True)
    (mod / "ModInfo.json").write_text(json.dumps({"Title": {"Neutral": "Canyon"}}), encoding="utf-8")
    menu, terminal = screen(path, ["1", "ENTER", "M", "ENTER", "1", "ENTER", "ESC", "ESC", "ESC"])
    run(menu.screen_settings())
    assert json.loads((folder / "content" / "disabled-mods.json").read_text(encoding="utf-8")) == \
        {"packages": ["Mods/canyon"]}
    assert "Canyon switched off." in text_of(terminal) and "mods on or off" in text_of(terminal)
    opened: list[Path] = []
    monkeypatch.setattr(desktop, "open_editor", lambda p: opened.append(p) or f"opened {p.name}")
    monkeypatch.setattr(desktop, "open_path", lambda p: opened.append(p) or f"opened {p}")
    for keys in (["1", "ENTER", "x"], ["2", "ENTER", "x"], ["7", "ENTER", "1", "ENTER", "x"],
                 ["7", "ENTER", "2", "ENTER", "x"]):
        menu, terminal = screen(path, keys)
        run(menu.screen_open())
    assert opened == [path.parent, path, folder, folder / "dedicated.toml"]
    assert "OPEN" in text_of(terminal) and "re-checks its files" in text_of(terminal)


def test_d_says_what_it_cant_read(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    (path.parent / "automod.txt").write_text("bad | 5x\n", encoding="utf-8")
    (path.parent / "commands.toml").unlink()
    menu, terminal = screen(path, ["A", "ENTER", "x", "C", "ENTER", "N", "ESC"])
    run(menu.screen_settings())
    text = text_of(terminal)
    assert "can't be read: automod.txt line 1" in text and "O opens it, to put it right by hand." in text
    assert "not there: every command has its default role" in text and "There's no commands.toml" in text
    assert not (path.parent / "commands.toml").exists()


def test_d_settings_toml_refuses_and_takes_out(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    hs.add_relay_user(path, "Alice", "alice-long-password", "moderator")
    keys = ["S", "ENTER",
            "1", "ENTER", "4", "ENTER", "N",  # dry_run off: it's off already
            "1", "ENTER", "4",  # backend: careful; attach is what it is
            "ESC",
            "3", "ENTER", "1", "ENTER", *"none", "ENTER",  # warn_seconds: none
            "2", "ENTER", *"soon", "ENTER", "x",  # after_game_seconds: not a number
            "ESC",
            *"12", "ENTER", "2", "ENTER", *"D:/MCC", "ENTER", "2", "ENTER", "-", "ENTER",  # mcc_folder, then out
            "1", "ENTER", *"9", "ENTER", "x",  # share: 9 isn't on the list
            "ESC",
            "7", "ENTER", "7", "ENTER", "-", "ENTER",  # exempt_roles: none
            "ESC",
            "8", "ENTER", "M", "ENTER",
            "A", "ENTER", *"Bob", "ENTER", *"alice-long-password", "ENTER", "1", "x",  # Alice's password: refused
            "1", "ENTER", "P", *"alice-new-password", "ENTER",  # a new password for Alice
            "1", "ENTER", "X", "Y",  # Alice taken out
            "ESC", "ESC", "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    data = read_toml(path)
    assert data["restarts"]["warn_seconds"] == [] and "mcc_folder" not in data["setup"]
    assert data["automod"]["exempt_roles"] == [] and "user" not in data["relay"]
    assert "That's what it is already." in text and "Careful: this changes where Huragok looks" in text
    assert "Not changed: a number." in text and "Not changed: the numbers in the list." in text
    assert "[setup] mcc_folder = D:/MCC, written" in text and "[setup] mcc_folder taken out of the file" in text
    assert "has that password" in text and "Alice's password changed." in text and "Alice taken out." in text


def test_d_commands_toml_made_owners_typed_and_the_cooldown(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    commands = path.parent / "commands.toml"
    commands.unlink()
    keys = ["C", "ENTER", "Y",  # made, as init writes it
            "1", "A", "ENTER", *OWNER, "ENTER", "ESC",  # an owner by player ID (no records yet)
            "1", "A", "ENTER", *"Alice", "ENTER", "x", "ESC",  # not a player ID
            "6", *"5", "ENTER", "6", *"lots", "ENTER",  # the cooldown, then not a number
            "5", "?", "ENTER",  # the prefix
            "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    permissions = load_permissions(commands)
    assert OWNER in permissions.members["owner"] and permissions.cooldown == 5 and permissions.prefix == "?"
    assert player_notes(commands)[OWNER].startswith("added as owner in Huragok's window on ")
    assert "No players in Huragok's records yet" in text and "is an owner now." in text
    assert "Not changed: Alice is not a player ID" in text and "Not changed: a number of seconds, 0 to 3600." in text
    assert "cooldown_seconds = 5 written." in text and "Commands start with ? now: !help reads ?help." in text


def test_d_word_list_changes_and_refusals(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    words = path.parent / "automod.txt"
    change_rule(words, None, "badword | 1h | | | rude")
    change_rule(words, None, "worstword | | | 0 | threats")
    keys = ["A", "ENTER",
            "1", "ENTER", "W", *"badword, rudeword", "ENTER", "Y",  # its words
            "1", "ENTER", "N", "BACKSPACE", "BACKSPACE", "BACKSPACE", "BACKSPACE", *"insults", "ENTER", "Y",  # its name
            "2", "ENTER", "X", "Y",  # threats taken out
            "A", "ENTER", *"slur", "ENTER", "1", *"insults", "ENTER", "ESC",  # a name taken already: asked again
            "A", "ENTER", *"spam", "ENTER", "5", *"soon", "ENTER", *"1h, 2h", "ENTER", "Y", *"x", "ENTER", *"1", "ENTER",
            *"1d", "ENTER", "Y", "1", "ENTER", "ENTER",  # not a time, not a number, too few for the ban; Enter: enough
            *"name", "ENTER", "ENTER", "N",  # a name !strike keeps; then none, and it isn't added
            "ESC", "N", "ENTER",
            "A", "ENTER", *"***", "ENTER", "x",  # nothing left of it: would catch everyone
            "T", "ENTER", *"Charlie", "ENTER", "x",
            "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    assert [line_of(r) for r in read_rules(words)] == ["badword, rudeword | 1h | | | insults"]
    assert "Strikes players have under rude stay under that name" in text and "Taken out." in text
    assert "A rule is called insults already" in text and "'soon' isn't a time" in text
    assert "A number, 0 or more." in text and "at least 2, so every kick and mute time comes first" in text
    assert "A number, 2 or more." in text and "Not name: !strike <player> name bans a player's name." in text
    assert "The line: spam | 1h, 2h | 1, 1d | 2" in text  # Enter: the least the ban can be
    assert "2nd: removed for 2 hours, then muted for 1 day" in text and "3rd: banned for good" in text
    assert "it would catch everyone" in text and "That name is fine." in text


def test_d_a_servers_playlists_custom_games_and_bans(tmp_path: Path, run) -> None:
    from testkit.setup import setup_root

    settings_path = setup_root(tmp_path)
    keys = ["1", "ENTER",
            "P", "ENTER", "ESC",  # its playlists: G's screen
            "G", "ENTER", "ESC",  # its custom games
            "B", "ENTER", "x",  # its bans: not running, so the ban list from its folder
            "M", "ENTER", "x",  # its mods: none yet
            "ESC", "ESC"]
    menu, terminal = screen(settings_path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    assert "Playlists" in text and "Custom games" in text and "isn't running: the ban list from its folder" in text
    assert "No mods in its content folder yet" in text
    # Without the server program, the screens that need it say so.
    path = template_root(tmp_path / "other")
    menu, terminal = screen(path, ["1", "ENTER", "P", "ENTER", "x", "ESC", "ESC"])
    run(menu.screen_settings())
    assert "project-reclaimer" in text_of(terminal).lower() or "program" in text_of(terminal)
