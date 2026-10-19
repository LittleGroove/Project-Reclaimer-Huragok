"""The host's own files after an update: every release reads them as any version from 0.4.0 on wrote them, and
brings them up to date itself (migrate.py). tests/data/host-0.4.0/ holds the files 0.4.0's first start wrote."""

from __future__ import annotations

import json
import logging
import re
import shutil
import tomllib
from pathlib import Path

import pytest

from huragok import __version__, cli, migrate
from huragok.config import load
from huragok.permissions import all_commands, load_permissions
from huragok.tomledit import TomlEditError, add_table_key, comment_out_table_key, read_toml, replace_comments

HOST_0_4_0 = Path(__file__).resolve().parent / "data" / "host-0.4.0"
TEMPLATE = Path(__file__).resolve().parent.parent / "huragok" / "templates" / "settings.toml"
# Keys the template has that an older host's settings.toml may go without, each with why. A key added to the
# template must be added here or by a step in migrate.py: that's the rule that keeps a host's file in step.
NOT_ADDED: dict[tuple[str, ...], str] = {}


def host(tmp_path: Path, *, packages: bool = True) -> Path:
    """A root as 0.4.0 left it: its four files, and PACKAGES/ with 0.4.0's starters."""
    root = tmp_path / "huragok"
    root.mkdir()
    for path in HOST_0_4_0.iterdir():
        shutil.copy2(path, root / path.name)
    if packages:
        for name in migrate.STARTERS_0_4_0:
            (root / "PACKAGES" / name).mkdir(parents=True)
            (root / "PACKAGES" / name / "package.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    (root / "SERVERS").mkdir()
    return root / "settings.toml"


def up_to_date(settings_path: Path) -> list[str]:
    root = settings_path.parent
    return migrate.bring_up_to_date(settings_path, root / "records", root / "PACKAGES")


def active_keys(path: Path) -> set[tuple[str, ...]]:
    """Every (table, key) a TOML file sets, [[server]] blocks left out (they're the host's own servers)."""
    keys: set[tuple[str, ...]] = set()

    def walk(data: dict, prefix: tuple[str, ...]) -> None:
        for key, value in data.items():
            if key == "server" and not prefix:
                continue
            if isinstance(value, dict):
                walk(value, prefix + (key,))
            else:
                keys.add(prefix + (key,))

    walk(read_toml(path), ())
    return keys


def test_this_version_reads_0_4_0s_files_as_they_are(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    settings = load(settings_path)  # before anything is brought up to date: nothing in them is wrong or unknown
    assert settings.retired == [] and settings.notes == []  # retired keys and unknown ones alike
    assert settings.setup.download_game_files is True  # a setting 0.4.0 never had takes its default
    load_permissions(settings_path.parent / "commands.toml")


def test_0_4_0s_settings_are_brought_up_to_date_once(tmp_path: Path, caplog) -> None:
    settings_path = host(tmp_path)
    before = settings_path.read_text(encoding="utf-8")
    with caplog.at_level(logging.INFO):
        changed = up_to_date(settings_path)
    assert changed == ["settings.toml: [setup] download_game_files = true added (new in 0.4.1)",
                       "settings.toml: the comment above [setup] steamcmd says it gets Halo 3's files too"]
    assert f"Brought up to date for Huragok {__version__}" in caplog.text
    text = settings_path.read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")
    for line in migrate.STEAMCMD_COMMENT_0_4_1 + [f"# {c}" for c in migrate.DOWNLOAD_GAME_FILES_COMMENT]:
        assert line in text and line in template  # the same words a new host's file has
    setup = text[text.index("\n[setup]\n"):]
    assert setup.index("steam_username") < setup.index("download_game_files") < setup.index("auto_update = true")
    # The host's own values and every other line are as they were.
    assert [line for line in text.splitlines() if line not in before.splitlines()] == \
        migrate.STEAMCMD_COMMENT_0_4_1 + [f"# {c}" for c in migrate.DOWNLOAD_GAME_FILES_COMMENT] + \
        ["download_game_files = true"]
    backup = settings_path.parent / "records" / "previous-files" / __version__ / "settings.toml"
    assert backup.read_text(encoding="utf-8") == before
    record = json.loads((settings_path.parent / "records" / "files.json").read_text(encoding="utf-8"))
    assert record["version"] == __version__ and "0.4.1/download-game-files" in record["steps"]
    assert up_to_date(settings_path) == [] and settings_path.read_text(encoding="utf-8") == text  # done once
    settings = load(settings_path)
    assert settings.retired == [] and settings.notes == []


def test_every_template_key_reaches_an_older_hosts_file(tmp_path: Path) -> None:
    """The rule: a setting new in the template comes with a step that adds it to every host's file since 0.4.0 (or
    a reason in NOT_ADDED). A host's file never silently falls behind the template."""
    settings_path = host(tmp_path)
    up_to_date(settings_path)
    template = tmp_path / "template.toml"
    template.write_text(re.sub(r"\{(\w+)\}", "''", TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
    missing = active_keys(template) - active_keys(settings_path) - set(NOT_ADDED)
    assert not missing, f"new in the template, never added to an older host's settings.toml: {sorted(missing)}"


def test_a_file_from_before_0_4_0_loses_what_does_nothing(tmp_path: Path) -> None:
    """Hosts' files outlive the version that wrote them: settings retired or renamed before 0.4.0 are put right
    too, never left for `check` to name forever."""
    settings_path = host(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    text = text.replace("[process]\n", "[process]\ndisable_auto_update = true  # the old default\n", 1)
    text = re.sub(r'(?m)^repository = "LittleGroove/Project-Reclaimer-Huragok"',
                  'repository = "LittleGroove/Project-Reclaimer-Server-Supervisor"', text, count=1)
    settings_path.write_text(text, encoding="utf-8")
    old = load(settings_path)
    assert old.retired and old.notes  # what 0.4.0 could only name
    assert migrate.pending(settings_path, settings_path.parent / "records", settings_path.parent / "PACKAGES")[:2] == [
        "settings.toml: [process] disable_auto_update made a comment (it does nothing since 0.4.0)",
        "settings.toml: [updates] repository is LittleGroove/Project-Reclaimer-Huragok (the repository's name since "
        "0.4.0)"]
    assert settings_path.read_text(encoding="utf-8") == text  # a dry run changes nothing
    up_to_date(settings_path)
    after = settings_path.read_text(encoding="utf-8")
    assert "# disable_auto_update = true  # the old default" in after and "# Not read since 0.4.0" in after
    fresh = load(settings_path)
    assert fresh.retired == [] and fresh.notes == [] and fresh.updates.repository == migrate.NEW_REPOSITORY


def test_the_hosts_own_choices_are_left_as_they_made_them(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    text = text.replace('steam_username = ""\n', 'steam_username = ""\n# download_game_files = false\n', 1)
    text = text.replace("# SteamCMD, for Workshop items (the window's I key", "# My SteamCMD notes (the window's I key", 1)
    settings_path.write_text(text, encoding="utf-8")
    assert up_to_date(settings_path) == []  # their commented-out key stays theirs; their comment stays theirs
    assert settings_path.read_text(encoding="utf-8") == text
    assert load(settings_path).setup.download_game_files is True


def test_a_file_that_cant_be_read_is_never_touched(tmp_path: Path, caplog) -> None:
    settings_path = host(tmp_path)
    broken = settings_path.read_text(encoding="utf-8").replace("[setup]\n", "[setup\n", 1)
    settings_path.write_text(broken, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert up_to_date(settings_path) == []
    assert settings_path.read_text(encoding="utf-8") == broken and "tried again at the next start" in caplog.text
    record = json.loads((settings_path.parent / "records" / "files.json").read_text(encoding="utf-8"))
    assert "0.4.1/download-game-files" not in record["steps"]  # not done: tried again once the file is fixed
    pending = migrate.pending(settings_path, settings_path.parent / "records", settings_path.parent / "PACKAGES")
    assert any(line.startswith("can't yet: ") for line in pending)
    settings_path.write_text(broken.replace("[setup\n", "[setup]\n", 1), encoding="utf-8")
    assert "settings.toml: [setup] download_game_files = true added (new in 0.4.1)" in up_to_date(settings_path)


def test_new_commands_are_listed_under_their_default_role(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    commands = settings_path.parent / "commands.toml"
    text = commands.read_text(encoding="utf-8")
    without = re.sub(r'(?m)^\s*"whois",.*\n', "", text)
    assert without != text
    commands.write_text(without, encoding="utf-8")
    roles_before = load_permissions(commands)
    changed = up_to_date(settings_path)
    assert "commands.toml: !whois (new) listed under [moderator], its default" in changed
    data = tomllib.loads(commands.read_text(encoding="utf-8"))
    assert "whois" in data["moderator"]["commands"]
    whois = all_commands()["whois"]
    assert load_permissions(commands).role_for(whois) == roles_before.role_for(whois)  # nobody's rights change
    assert up_to_date(settings_path) == []
    # A command the host switched off stays off; one they moved stays where they put it.
    off = re.sub(r'(?m)^\s*"warn",.*\n', "", commands.read_text(encoding="utf-8")).replace(
        "[off]\ncommands = []", '[off]\ncommands = ["warn"]')
    commands.write_text(off, encoding="utf-8")
    assert up_to_date(settings_path) == [] and commands.read_text(encoding="utf-8") == off


def test_a_new_starter_package_arrives_once_and_a_removed_one_stays_removed(tmp_path: Path, monkeypatch) -> None:
    settings_path = host(tmp_path)
    packages = settings_path.parent / "PACKAGES"
    shutil.rmtree(packages / "infection")  # the host took it out
    assert up_to_date(settings_path)[-1:] != ["PACKAGES/infection: a new starter package"]
    assert not (packages / "infection").exists()
    # A release that brings a starter 0.4.0 didn't have:
    monkeypatch.setattr(migrate, "STARTERS_0_4_0", tuple(n for n in migrate.STARTERS_0_4_0 if n != "team-slayer"))
    shutil.rmtree(packages / "team-slayer")
    record = settings_path.parent / "records" / "files.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["starters"].remove("team-slayer")
    record.write_text(json.dumps(data), encoding="utf-8")
    assert "PACKAGES/team-slayer: a new starter package" in up_to_date(settings_path)
    assert (packages / "team-slayer" / "package.toml").is_file()
    shutil.rmtree(packages / "team-slayer")  # taken out again: never put back
    assert up_to_date(settings_path) == [] and not (packages / "team-slayer").exists()


def test_huragok_brings_the_files_up_to_date_as_it_starts(tmp_path: Path, monkeypatch) -> None:
    settings_path = host(tmp_path)
    args = cli.build_parser().parse_args(["-c", str(settings_path), "start"])
    settings = load(settings_path)
    fresh = cli._bring_files_up_to_date(args, settings)
    assert fresh is not settings and "download_game_files" in read_toml(settings_path)["setup"]

    def broken(*args: object) -> list[str]:
        raise RuntimeError("a bug in a step")

    monkeypatch.setattr(migrate, "bring_up_to_date", broken)
    assert cli._bring_files_up_to_date(args, fresh) is fresh  # never keeps Huragok from starting


def test_check_says_what_the_next_start_brings_up_to_date(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings_path = host(tmp_path)
    cli.main(["-c", str(settings_path), "check"])
    out = capsys.readouterr().out
    assert "Brought up to date at the next start" in out and "download_game_files = true added" in out
    assert "download_game_files" not in read_toml(settings_path)["setup"]  # check changes nothing


def test_the_edits_steps_make(tmp_path: Path) -> None:
    path = tmp_path / "x.toml"
    path.write_text('[a]\nkeep = 1\nold = [\n  "x",\n]  # why\n\n[b]\n# note\n', encoding="utf-8")
    assert comment_out_table_key(path, "a", "old", "Not read any more.\nSecond line.")
    assert path.read_text(encoding="utf-8") == ('[a]\nkeep = 1\n# Not read any more.\n# Second line.\n# old = [\n'
                                                '#   "x",\n# ]  # why\n\n[b]\n# note\n')
    assert not comment_out_table_key(path, "a", "old", "again")
    assert add_table_key(path, "a", "new", True, ["What it does."], after="keep")
    assert read_toml(path)["a"] == {"keep": 1, "new": True}
    assert path.read_text(encoding="utf-8").startswith("[a]\nkeep = 1\n# What it does.\nnew = true\n")
    assert not add_table_key(path, "a", "new", False)  # set already: left as it is
    assert add_table_key(path, "c", "made", "yes", ["A table it makes."])
    assert read_toml(path)["c"] == {"made": "yes"}
    path.write_text("[a]\n# new = false\n", encoding="utf-8")
    assert not add_table_key(path, "a", "new", True)  # the host's commented-out choice
    path.write_text("[a]\n  # one\n  # two\nx = 1\n", encoding="utf-8")
    assert replace_comments(path, ["# one", "# two"], ["# uno", "# dos", "# tres"])
    assert path.read_text(encoding="utf-8") == "[a]\n  # uno\n  # dos\n  # tres\nx = 1\n"
    assert not replace_comments(path, ["# one", "# two"], ["# x"])
    with pytest.raises(TomlEditError):
        replace_comments(path, ["# uno"], ["x = 2"])  # only comments are swapped
