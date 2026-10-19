"""tomledit, the in-place TOML writer behind every change Huragok makes to a host's dedicated.toml and settings.toml:
each change either reads back as exactly that change, or is refused with the file left byte for byte as it was."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, Callable

import pytest
from fake_dedicated import TEMPLATE

from huragok import tomledit
from huragok.livesettings import write_plays
from huragok.tomledit import TomlEditError, change_server_settings, read_toml, remove_server_setting, \
    replace_server_blocks, set_server_setting, set_table_key, set_top_key


def _block(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((b for b in data.get("server", []) if str(b.get("name", "")).lower() == name.lower()), None)


def _top(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda data: data.__setitem__(key, value)


def _table(table: str, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda data: data.setdefault(table, {}).__setitem__(key, value)


def _server(name: str, key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def change(data: dict[str, Any]) -> None:
        block = _block(data, name)
        if block is None:
            data.setdefault("server", []).append({"name": name, key: value})
        else:
            block[key] = value
    return change


def _removed(name: str, key: str) -> Callable[[dict[str, Any]], None]:
    return lambda data: (_block(data, name) or {}).pop(key, None)


CAMPAIGN = {"mission": 3010, "difficulty": "heroic", "modifiers": {"sprint": True}}
OPS: list[tuple[str, Callable[[Path], Any], Callable[[dict[str, Any]], None]]] = [
    ("top game", lambda p: set_top_key(p, "game", "../../game"), _top("game", "../../game")),
    ("top quotes", lambda p: set_top_key(p, "host_name", 'He said "hi" \\ C:\\x'),
     _top("host_name", 'He said "hi" \\ C:\\x')),
    ("top list", lambda p: set_top_key(p, "admins", ["a" * 64]), _top("admins", ["a" * 64])),
    ("top bool", lambda p: set_top_key(p, "auto_update", True), _top("auto_update", True)),
    ("rcon password", lambda p: set_table_key(p, "rcon", "password", "pw#x'\"y"),
     _table("rcon", "password", "pw#x'\"y")),
    ("workshop items", lambda p: set_table_key(p, "workshop", "items", [1, 2, 3], before="master"),
     _table("workshop", "items", [1, 2, 3])),
    ("defaults max_ping", lambda p: set_table_key(p, "defaults", "max_ping", 150), _table("defaults", "max_ping", 150)),
    ("server int", lambda p: set_server_setting(p, "Alpha", "max_players", 12), _server("Alpha", "max_players", 12)),
    ("server password", lambda p: set_server_setting(p, "Alpha", "password", 'a"b#c'),
     _server("Alpha", "password", 'a"b#c')),
    ("server campaign", lambda p: set_server_setting(p, "Alpha", "campaign", CAMPAIGN),
     _server("Alpha", "campaign", CAMPAIGN)),
    ("server separators", lambda p: set_server_setting(p, "Alpha", "welcome", "Hi\u2028there\x85ok\x7f\u2029"),
     _server("Alpha", "welcome", "Hi\u2028there\x85ok\x7f\u2029")),
    ("new block", lambda p: set_server_setting(p, "Zed", "autostart", False), _server("Zed", "autostart", False)),
    ("remove playlist", lambda p: remove_server_setting(p, "Alpha", "playlist"), _removed("Alpha", "playlist")),
    ("rename", lambda p: set_server_setting(p, "Alpha", "name", "Alpha Two"), _server("Alpha", "name", "Alpha Two")),
]

REAL = TEMPLATE.replace("\r\n", "\n")
EDGE = {
    "the template": REAL + '\n[[server]]\nname = "Alpha"\nport = 1\nplaylist = "p"\n',
    "CRLF": (REAL + '\n[[server]]\nname = "Alpha"\nport = 1\n').replace("\n", "\r\n"),
    "no trailing newline": '[[server]]\nname = "Alpha"\nport = 1',
    "dotted top rcon": 'game = "g"\nrcon.password = "abcdefgh"\n\n[[server]]\nname = "Alpha"\nport = 1\n',
    "inline rcon": 'game = "g"\nrcon = { password = "abcdefgh" }\n\n[[server]]\nname = "Alpha"\nport = 1\n',
    "quoted table": 'game = "g"\n["rcon"]\npassword = "abcdefgh"\n\n[[server]]\nname = "Alpha"\nport = 1\n',
    "quoted key": 'game = "g"\n[rcon]\n"password" = "abcdefgh"\n\n[[server]]\nname = "Alpha"\nport = 1\n',
    "dotted campaign": '[[server]]\nname = "Alpha"\nport = 1\ncampaign.mission = 3010\n'
                       'campaign.difficulty = "heroic"\n',
    "server sub-table": '[[server]]\nname = "Alpha"\nport = 1\n\n[server.campaign]\nmission = 3010\n',
    "quoted name key": '[[server]]\n"name" = "Alpha"\nport = 1\n',
    "multi-line string": '[[server]]\nname = "Alpha"\nwelcome = """\n[LG] rules\nport = 5\n"""\nport = 1\n',
    "nested arrays": 'game = "g"\nweird = [\n  [1, 2],\n  [3],\n]\n[rcon]\npassword = "abcdefgh"\n'
                     '[[server]]\nname = "Alpha"\nport = 1\n',
    "commented key in an array": 'game = "g"\n[defaults]\ncustomization = [\n  "Armor Pack",\n  # max_ping = 5\n]\n'
                                 '[[server]]\nname = "Alpha"\nport = 1\n',
    "name not first": '[[server]]\nport = 1\nname = "Alpha"\nplaylist = "p"\n',
    "escaped name": '[[server]]\nname = "Al\\u0070ha"\nport = 1\n',
    "table after the blocks": '[[server]]\nname = "Alpha"\nport = 1\n\n[rcon]\npassword = "abcdefgh"\n',
    "array of tables": 'x = 1\n[relay]\nenabled = false\n[[relay.user]]\nname = "Al"\n'
                       '[[server]]\nname = "Alpha"\nport = 1\n',
    "tabs and indents": '[rcon]\n  password\t=\t"abcdefgh"\n[[server]]\n  name\t= "Alpha"\n  port = 1\n',
    "literal multi-line": "[[server]]\nname = 'Alpha'\nwelcome = '''\n[x]\n'''\nport = 1\n",
    "U+2028 in a comment": 'game = "g"  # note\u2028more\n[[server]]\nname = "Alpha"\nport = 1\n',
    "NEL in a value": 'host_name = "a\x85b"\n[[server]]\nname = "Alpha"\nport = 1\n',
    "empty": "",
    "commented example": '# hi\n# [workshop]\n# items = [1]\n',
}


@pytest.mark.parametrize("label", list(EDGE))
def test_every_change_is_exactly_that_change_or_refused(tmp_path: Path, label: str) -> None:
    text = EDGE[label]
    base = tomllib.loads(text)
    path = tmp_path / "dedicated.toml"
    for name, op, change in OPS:
        path.write_bytes(text.encode("utf-8"))
        try:
            op(path)
        except TomlEditError as error:
            assert path.read_bytes() == text.encode("utf-8"), f"{name}: refused but the file changed"
            assert "edit it by hand (E)" in str(error)
            continue
        wanted = copy.deepcopy(base)
        change(wanted)
        assert read_toml(path) == wanted, name
        set_top_key(path, "upnp", False)  # and the file stays editable
        assert read_toml(path)["upnp"] is False, name
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]


def test_forms_it_cant_edit_are_refused_without_showing_the_value(tmp_path: Path) -> None:
    path = tmp_path / "dedicated.toml"
    for text, op, shown in [
        ('rcon.password = "hunter2hunter2"\n', lambda: set_table_key(path, "rcon", "password", "newpassword"),
         "rcon.password = …"),
        ('rcon = { password = "hunter2hunter2" }\n', lambda: set_table_key(path, "rcon", "password", "newpassword"),
         "rcon = { … }"),
        ('[[server]]\nname = "Alpha"\ncampaign.mission = 3010\n',
         lambda: set_server_setting(path, "Alpha", "campaign", {"mission": 3020}), "campaign.mission = …"),
        ('[[server]]\nname = "Alpha"\n[server.campaign]\nmission = 3010\n',
         lambda: remove_server_setting(path, "Alpha", "campaign"), "[server.campaign] table"),
    ]:
        path.write_text(text, encoding="utf-8")
        before = path.read_bytes()
        with pytest.raises(TomlEditError) as raised:
            op()
        assert shown in str(raised.value)
        assert "hunter2" not in str(raised.value)
        assert "couldn't change" in str(raised.value) and "dedicated.toml" in str(raised.value)
        assert path.read_bytes() == before


def test_quoted_names_are_the_same_keys(tmp_path: Path) -> None:
    path = tmp_path / "dedicated.toml"
    path.write_text('["rcon"]\n"password" = "abcdefgh"\n\n[[server]]\n"name" = "Alpha"\nport = 1\n', encoding="utf-8")
    set_table_key(path, "rcon", "password", "ijklmnop")
    set_server_setting(path, "alpha", "port", 2)
    text = path.read_text(encoding="utf-8")
    assert text.count("\n[") == 1 and text.count("password") == 1  # in place: no second [rcon] or [[server]]
    assert '"password" = "ijklmnop"' in text
    assert read_toml(path) == {"rcon": {"password": "ijklmnop"}, "server": [{"name": "Alpha", "port": 2}]}


def test_lines_inside_multi_line_values_are_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "dedicated.toml"
    path.write_text('game = "g"\n[defaults]\ncustomization = [\n  "Armor Pack",\n  # max_ping = 5\n]\n\n'
                    '[[server]]\nname = "Alpha"\nwelcome = """\n[LG] rules\nport = 5\n"""\nport = 1\n',
                    encoding="utf-8")
    set_table_key(path, "defaults", "max_ping", 150)
    set_server_setting(path, "Alpha", "port", 2)
    text = path.read_text(encoding="utf-8")
    assert "  # max_ping = 5\n" in text and "[LG] rules\nport = 5\n" in text
    data = read_toml(path)
    assert data["defaults"] == {"customization": ["Armor Pack"], "max_ping": 150}
    assert data["server"][0] == {"name": "Alpha", "welcome": "[LG] rules\nport = 5\n", "port": 2}


def test_multi_line_strings_round_trip(tmp_path: Path) -> None:
    """[command] restart = '''…''' in settings.toml, and a multi-line welcome in a block."""
    path = tmp_path / "settings.toml"
    original = ("[command]\nrestart = '''\nsystemctl restart reclaimer\n[not a header]\n'''\n"
                'stop = """\nsystemctl stop \\\n  reclaimer\n"""  # why\ntimeout = 10\n\n'
                "[[server]]\nname = 'Alpha'\nwelcome = '''\nhi\n[x] = 1\n'''\nport = 1\n")
    path.write_text(original, encoding="utf-8")
    set_table_key(path, "command", "timeout", 20)
    assert path.read_text(encoding="utf-8") == original.replace("timeout = 10", "timeout = 20")
    set_table_key(path, "command", "restart", "systemctl restart other")
    set_server_setting(path, "Alpha", "max_players", 8)
    assert remove_server_setting(path, "Alpha", "welcome")
    data = read_toml(path)
    assert data["command"] == {"restart": "systemctl restart other", "stop": "systemctl stop reclaimer\n",
                               "timeout": 20}
    assert data["server"] == [{"name": "Alpha", "port": 1, "max_players": 8}]
    assert '"""  # why' in path.read_text(encoding="utf-8")


def test_separators_and_control_characters_are_escaped(tmp_path: Path) -> None:
    for char in ("\x7f", "\x85", "\u2028", "\u2029", "\x00", "\x1b", "\x9f"):
        rendered = tomledit._toml_value(f"a{char}b")
        assert char not in rendered and tomllib.loads(f"v = {rendered}")["v"] == f"a{char}b"
    path = tmp_path / "dedicated.toml"
    path.write_text('host_name = "x"  # a note\u2028still the note\n[[server]]\nname = "Alpha"\n', encoding="utf-8")
    set_server_setting(path, "Alpha", "welcome", "Hi\u2028there\x7f")
    set_top_key(path, "game", "game")
    assert read_toml(path)["server"][0]["welcome"] == "Hi\u2028there\x7f"
    assert "a note\u2028still the note" in path.read_text(encoding="utf-8")


def test_renaming_a_block_that_isnt_there_never_appends_one(tmp_path: Path) -> None:
    """A retried rename (the first one got halfway) used to append `[[server]] name = "Alpha"` + a second name."""
    path = tmp_path / "dedicated.toml"
    path.write_text('[[server]]\nname = "Charlie"\nport = 1\n', encoding="utf-8")
    with pytest.raises(TomlEditError, match="no \\[\\[server\\]\\] block named Alpha"):
        set_server_setting(path, "Alpha", "name", "Charlie")
    assert path.read_text(encoding="utf-8") == '[[server]]\nname = "Charlie"\nport = 1\n'


def test_the_safety_net_refuses_a_wrong_result_and_a_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "dedicated.toml"
    path.write_text('game = "g"\n', encoding="utf-8")
    edit = tomledit._Edit(path, "game")
    edit.wanted["game"] = "h"
    edit.lines = ['game = "h"', "stray = 1"]  # what a bug in the line handling would do
    with pytest.raises(TomlEditError, match="other values would change too \\(stray\\)"):
        edit.save()
    edit.lines = ['game = "h"', "[x"]
    with pytest.raises(TomlEditError, match="wouldn't be valid TOML"):
        edit.save()
    assert path.read_text(encoding="utf-8") == 'game = "g"\n'
    path.write_text('game = "g"\ngame = "h"\n', encoding="utf-8")
    with pytest.raises(TomlEditError, match="isn't valid TOML now"):
        set_top_key(path, "game", "x")
    path.write_bytes(b'host_name = "caf\xe9"\n')  # saved as ANSI by an old editor
    with pytest.raises(TomlEditError, match="isn't UTF-8"):
        set_top_key(path, "game", "x")


def test_a_failed_replace_leaves_the_file_and_no_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "dedicated.toml"
    path.write_text('game = "g"\n', encoding="utf-8")

    def locked(src: Any, dst: Any) -> None:
        raise PermissionError(13, "The process cannot access the file", str(dst))

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        set_top_key(path, "game", "x")
    assert path.read_text(encoding="utf-8") == 'game = "g"\n'
    assert [p.name for p in tmp_path.iterdir()] == ["dedicated.toml"]


def test_what_it_plays_is_one_write(tmp_path: Path) -> None:
    """write_plays: every other kind out and the new one in, or (one part refused) nothing changed at all."""
    path = tmp_path / "dedicated.toml"
    path.write_text('[[server]]\nname = "Alpha"\nport = 1\nmap = "Guardian"\ngame = "Slayer"\n', encoding="utf-8")
    write_plays(path, "Alpha", {"playlist": "playlists/slayer.playlist.json"})
    assert read_toml(path)["server"] == [{"name": "Alpha", "port": 1, "playlist": "playlists/slayer.playlist.json"}]
    broken = '[[server]]\nname = "Alpha"\nport = 1\nmap = "Guardian"\ncampaign.mission = 3010\n'
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(TomlEditError, match="couldn't change what it plays for Alpha"):
        write_plays(path, "Alpha", {"playlist": "playlists/slayer.playlist.json"})
    assert path.read_text(encoding="utf-8") == broken
    change_server_settings(path, "Zed", {"port": 5, "map": "Pit"})
    assert read_toml(path)["server"][1] == {"name": "Zed", "port": 5, "map": "Pit"}


def test_replacing_the_blocks_takes_their_sub_tables_too(tmp_path: Path) -> None:
    path = tmp_path / "dedicated.toml"
    path.write_text('# the servers\n[[server]]\nname = "Alpha"\n[server.campaign]\nmission = 3010\n\n'
                    '[[server]]\nname = "Bravo"\n', encoding="utf-8")
    replace_server_blocks(path, [{"name": "New", "port": 3, "campaign": {"mission": 3020}}])
    assert read_toml(path) == {"server": [{"name": "New", "port": 3, "campaign": {"mission": 3020}}]}
    assert path.read_text(encoding="utf-8").startswith("# the servers\n")


# One player in a role's list, in or out (commands.toml) ---------------------------------------------------------

A_ID, B_ID, C_ID = "a" * 64, "b" * 64, "c" * 64


def test_a_player_goes_in_on_a_line_of_their_own_and_the_hosts_comments_stay(tmp_path: Path) -> None:
    from huragok.permissions import render_commands_toml

    path = tmp_path / "commands.toml"
    path.write_text(render_commands_toml(), encoding="utf-8")  # the file a first start writes
    before = path.read_text(encoding="utf-8")
    assert tomledit.add_to_list(path, "member", "players", A_ID, "Alice: added by Mod on 2026-10-19")
    assert tomledit.add_to_list(path, "member", "players", B_ID, "Bravo")
    text = path.read_text(encoding="utf-8")
    assert f'    "{A_ID}",  # Alice: added by Mod on 2026-10-19\n    "{B_ID}",  # Bravo\n]' in text
    assert "# who this is" in text  # the example line the host may have filled in stays
    assert read_toml(path)["member"]["players"] == [A_ID, B_ID]
    assert len(text.splitlines()) == len(before.splitlines()) + 2  # two lines in, nothing else
    assert not tomledit.add_to_list(path, "member", "players", A_ID.upper())  # there already: nothing changes
    assert tomledit.remove_from_list(path, "member", "players", A_ID)
    text = path.read_text(encoding="utf-8")
    assert "Alice" not in text and f'"{B_ID}",  # Bravo' in text and read_toml(path)["member"]["players"] == [B_ID]
    assert not tomledit.remove_from_list(path, "member", "players", A_ID)


def test_a_last_entry_without_its_comma_gets_one(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    path.write_text(f'[moderator]\nplayers = [\n    "{A_ID}"  # Alice, no comma\n    # someone later\n]\n',
                    encoding="utf-8")
    tomledit.add_to_list(path, "moderator", "players", B_ID, "Bravo")
    assert f'"{A_ID}",  # Alice, no comma' in path.read_text(encoding="utf-8")
    assert read_toml(path)["moderator"]["players"] == [A_ID, B_ID]


@pytest.mark.parametrize("start, then", [
    (f'[member]\nplayers = []  # regulars\n', [C_ID]),
    (f'[member]\nplayers = ["{A_ID}", "{B_ID}"]\n', [A_ID, B_ID, C_ID]),
    (f'prefix = "!"\n[member]\ncommands = []\n', [C_ID]),  # the table, no list yet
    (f'prefix = "!"\n', [C_ID]),  # no table at all
])
def test_lists_written_any_way_take_one_more(tmp_path: Path, start: str, then: list[str]) -> None:
    path = tmp_path / "commands.toml"
    path.write_text(start, encoding="utf-8")
    assert tomledit.add_to_list(path, "member", "players", C_ID, "Cy")
    data = read_toml(path)
    assert data["member"]["players"] == then and f'"{C_ID}",  # Cy' in path.read_text(encoding="utf-8")
    assert data.get("prefix", "!") == "!" and ("commands" not in start or data["member"]["commands"] == [])
    if "# regulars" in start:
        assert "# regulars" in path.read_text(encoding="utf-8")  # the line's own comment stays
    assert tomledit.remove_from_list(path, "member", "players", C_ID)
    assert read_toml(path)["member"]["players"] == then[:-1]


def test_an_entry_sharing_its_line_is_left_for_the_host(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    original = f'[member]\nplayers = [\n    "{A_ID}", "{B_ID}",\n]\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(TomlEditError, match="isn't on a line of its own"):
        tomledit.remove_from_list(path, "member", "players", A_ID)
    assert path.read_text(encoding="utf-8") == original  # untouched
