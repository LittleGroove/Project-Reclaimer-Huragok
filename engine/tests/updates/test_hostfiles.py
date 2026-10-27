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

from testkit import ENGINE
from testkit.hostfiles import HOST_0_4_0, host, up_to_date

from huragok import __version__
from huragok.commandline import cli
from huragok.commandline import launcher as launcher_module
from huragok.commandline import run as run_module
from huragok.core.commandsfile import all_commands, load_permissions
from huragok.core.settings import load
from huragok.core.tomledit import (
    TomlEditError, add_table_key, add_to_list, comment_out_table_key, read_toml, remove_from_list, replace_comments,
)
from huragok.updates import hostfiles as migrate
from huragok.updates.notes import template_text

TEMPLATE = ENGINE / "huragok" / "templates" / "settings.toml"
# Keys the template has that an older host's settings.toml may go without, each with why. A key added to the
# template must be added here or by a step in migrate.py: that's the rule that keeps a host's file in step.
NOT_ADDED: dict[tuple[str, ...], str] = {}


# The commands 0.4.0 to 0.4.4 had: anything else listed in commands.toml stopped them starting.
COMMANDS_0_4_0 = {c for section in tomllib.loads((HOST_0_4_0 / "commands.toml").read_text(encoding="utf-8")).values()
                  if isinstance(section, dict) for c in section.get("commands", [])}


# In-game commands since 0.4.0, each listed in a host's commands.toml under its default role once the new version
# has stayed up.
NEW_COMMANDS = {"banall", "bannames", "bans", "banword", "changebanword", "kicks", "mutes", "restartserver", "rules",
                "said", "toggleglobal", "toggleproxy", "toggleteam", "togglevoice",
                "unbanword"}


# The word list in the new layout (the mutes first), once this version has stayed: 0.4.0's has notes and no lines.
AUTOMOD_ORDER = "automod.txt: Huragok's notes as this version writes them, the mutes first (words | mutes | kicks | ban | name)"


def new_command_lines() -> list[str]:
    from huragok.core.commandsfile import TITLES

    specs = all_commands()
    assert set(specs) - COMMANDS_0_4_0 - {"help"} == NEW_COMMANDS
    return [f"commands.toml: !{name} (new) listed under [{TITLES[specs[name].role]}], its default"
            for name in sorted(NEW_COMMANDS)]


def listed_commands(path: Path) -> set[str]:
    return {c for section in tomllib.loads(path.read_text(encoding="utf-8")).values() if isinstance(section, dict)
            for c in section.get("commands", [])}


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
    assert settings.retired == []  # retired keys and unknown ones alike
    # Only a key renamed since, still read under its old name until the start renames it (0.4.2):
    assert len(settings.notes) == 1 and "install_on_server_restart = false is auto_update's name" in settings.notes[0]
    assert settings.setup.download_game_files is True  # a setting 0.4.0 never had takes its default
    load_permissions(settings_path.parent / "commands.toml")


def test_0_4_0s_settings_are_brought_up_to_date_once(tmp_path: Path, caplog) -> None:
    settings_path = host(tmp_path)
    before = settings_path.read_text(encoding="utf-8")
    with caplog.at_level(logging.INFO):
        changed = up_to_date(settings_path)
    assert changed == [
        "settings.toml: [setup] download_game_files = true added (new in 0.4.1)",
        "settings.toml: the comment above [setup] steamcmd says it gets Halo 3's files too",
        "settings.toml: [updates] install_on_server_restart is auto_update now (= false)",
        "settings.toml: [updates] auto_update = false added: true installs new versions of Huragok by themselves "
        "(new in 0.4.2)",
        "settings.toml: the comment above [updates] says the window keeps saying a new version is out",
        "settings.toml: [process] priority = \"high\" added: the servers run at High priority, ahead of everything "
        "else on this PC that runs at normal (new in 0.4.4)",
        "settings.toml: [automod] name_match = \"anywhere\" added: a word from automod.txt anywhere in a player's "
        "name gets them kicked until they change it, DasHitler with hitler listed (new in 0.5.0)",
        "settings.toml: [automod] across_servers = true added: a mute, kick or ban on one server reaches the others "
        "(new in 0.5.0)",
        "settings.toml: [setup] share = ['game'] (\"bans\" taken out): every server keeps a ban list of its own from "
        "0.5.0; the ones sharing one get a copy of it as each next starts",
        "commands.toml: !banname moved from [moderator] to [admin]: banning names, like the new !banword, is for "
        "admins from 0.5.0 (put it back under [moderator] to keep it theirs)",
        "intervalmessages.txt: interval messages (for every server), written with how to set them up; none yet",
        "settings.toml: [updates] beta = false added: true offers beta releases of Huragok too, before they're "
        "full releases (new in 0.5.0)",
        *(f"{name}: Huragok's notes as this version writes them"
          for name in ("settings.toml", "commands.toml", "names.txt")),
        AUTOMOD_ORDER, *new_command_lines()]
    assert f"Brought up to date for Huragok {__version__}" in caplog.text
    text = settings_path.read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")
    added = ([f"# {c}" for c in migrate.NAME_MATCH_COMMENT] + ['name_match = "anywhere"']
             + [f"# {c}" for c in migrate.ACROSS_COMMENT] + ["across_servers = true"]
             + migrate.UPDATES_COMMENT_0_4_2 + [f"# {c}" for c in migrate.AUTO_UPDATE_COMMENT] + ["auto_update = false"]
             + [f"# {c}" for c in migrate.BETA_COMMENT] + ["beta = false"]
             + [c for c in migrate.SHARE_COMMENT_0_5_0 if c not in migrate.SHARE_COMMENT_0_4_0] + ['share = ["game"]']
             + migrate.STEAMCMD_COMMENT_0_4_1 + [f"# {c}" for c in migrate.DOWNLOAD_GAME_FILES_COMMENT]
             + ["download_game_files = true"] + [f"# {c}" for c in migrate.PRIORITY_COMMENT] + ['priority = "high"'])
    for line in added:
        assert line in template.splitlines()  # the same words a new host's file has
    setup = text[text.index("\n[setup]\n"):]
    assert setup.index("steam_username") < setup.index("download_game_files") < setup.index("auto_update = true")
    process = text[text.index("\n[process]\n"):text.index("\n[docker]\n")]
    assert process.rstrip().endswith("\nstop_servers_on_exit = false\n" + "\n".join(
        f"# {c}" for c in migrate.PRIORITY_COMMENT) + '\npriority = "high"')  # where a new host's file has it
    # The file is now the one a new host's init writes, line for line, but for the host's own values (check_hours
    # stays 6: 1 is new hosts' default). Every note is this version's.
    new_host = template_text("settings.toml").splitlines()
    assert len(text.splitlines()) == len(new_host)
    assert [(a, b) for a, b in zip(new_host, text.splitlines()) if a != b] == [
        ("servers_folder = ''", "servers_folder = 'SERVERS'"), ("backend = ''", "backend = 'process'"),
        ("check_hours = 1", "check_hours = 6"), ("executable = ''", "executable = 'project-reclaimer.exe'"),
        ("compose_file = ''", "compose_file = 'compose.yaml'")]
    root = settings_path.parent
    for name in ("automod.txt", "names.txt", "intervalmessages.txt"):
        assert (root / name).read_text(encoding="utf-8") == template_text(name)
    commands = (root / "commands.toml").read_text(encoding="utf-8").splitlines()
    assert sorted(commands) == sorted(template_text("commands.toml").splitlines())  # the new commands come last
    backup = settings_path.parent / "records" / "previous-files" / __version__ / "settings.toml"
    assert backup.read_text(encoding="utf-8") == before
    record = json.loads((settings_path.parent / "records" / "files.json").read_text(encoding="utf-8"))
    assert record["version"] == __version__ and "0.4.1/download-game-files" in record["steps"]
    assert up_to_date(settings_path) == [] and settings_path.read_text(encoding="utf-8") == text  # done once
    settings = load(settings_path)
    assert settings.retired == [] and settings.notes == []


def test_a_host_who_had_huragok_install_itself_keeps_it(tmp_path: Path) -> None:
    """0.4.2 renamed [updates] install_on_server_restart auto_update: true carries over (and installs as soon as a
    version is found now), with a comment where the old line was; a value the host wrote either way wins."""
    settings_path = host(tmp_path)
    text = settings_path.read_text(encoding="utf-8").replace("install_on_server_restart = false",
                                                              "install_on_server_restart = true", 1)
    settings_path.write_text(text, encoding="utf-8")
    assert load(settings_path).updates.auto_update  # read as auto_update before the start renames it
    assert "settings.toml: [updates] install_on_server_restart is auto_update now (= true)" in up_to_date(settings_path)
    after = settings_path.read_text(encoding="utf-8")
    assert "\nauto_update = true\n" in after and "# install_on_server_restart = true" in after
    assert "# Renamed auto_update in 0.4.2" in after
    fresh = load(settings_path)
    assert fresh.updates.auto_update and fresh.notes == [] and fresh.retired == []
    # The host set auto_update themselves, beside the old line: theirs stays, the old line still goes.
    settings_path = host(tmp_path / "both")
    text = settings_path.read_text(encoding="utf-8").replace("install_on_server_restart = false",
                                                              "install_on_server_restart = true\nauto_update = false", 1)
    settings_path.write_text(text, encoding="utf-8")
    assert "settings.toml: [updates] install_on_server_restart is auto_update now (auto_update was set already)" in \
        up_to_date(settings_path)
    assert not load(settings_path).updates.auto_update
    assert read_toml(settings_path)["updates"].get("install_on_server_restart") is None


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
    """A setting the host made a comment stays one (no step adds it back), with this version's note above it; a note
    of Huragok's they reworded stays as they wrote it, the rest of that note this version's."""
    settings_path = host(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    text = text.replace('steam_username = ""\n', 'steam_username = ""\n# download_game_files = false\n', 1)
    text = text.replace("# SteamCMD, for Workshop items (the window's I key", "# My SteamCMD notes (the window's I key", 1)
    text = text.replace("stop_servers_on_exit = false\n", 'stop_servers_on_exit = false\n# priority = "normal"\n', 1)
    text = text.replace("check_names = true ", '# name_match = "words"\n# across_servers = false\ncheck_names = true ',
                        1)
    settings_path.write_text(text, encoding="utf-8")
    changed = up_to_date(settings_path)
    # Their commented-out keys and their comment are theirs (share losing "bans" is every host's: a ban list of its own
    # for every server isn't a choice).
    assert not any(part in line for line in changed for part in ("[setup]", "[process]", "[automod]")
                   if "share" not in line)
    after = settings_path.read_text(encoding="utf-8")
    steamcmd = "\n".join(["# My SteamCMD notes (the window's I key sets this up: finds or installs",
                          *migrate.STEAMCMD_COMMENT_0_4_1[1:], "# steamcmd = "])
    assert steamcmd in after  # their wording in place of the line it rewords; the rest brought up to date
    assert migrate.STEAMCMD_COMMENT_0_4_1[0] not in after
    for comment, line in ((migrate.DOWNLOAD_GAME_FILES_COMMENT, "# download_game_files = false"),
                          (migrate.PRIORITY_COMMENT, '# priority = "normal"'),
                          (migrate.NAME_MATCH_COMMENT, '# name_match = "words"'),
                          (migrate.ACROSS_COMMENT, "# across_servers = false")):
        assert "\n".join([*(f"# {c}" for c in comment), line]) in after  # what it is, above their choice
    assert "name_match" not in read_toml(settings_path)["automod"]  # the default, anywhere, applies
    assert "across_servers" not in read_toml(settings_path)["automod"]
    assert load(settings_path).setup.download_game_files is True
    assert "priority" not in read_toml(settings_path)["process"]  # the default, High, applies
    # The same for [updates]: their own line in Huragok's note stays, after it; their commented-out auto_update and
    # beta stay commented out, each with its note.
    settings_path = host(tmp_path / "updates")
    text = settings_path.read_text(encoding="utf-8")
    text = text.replace("# It never installs one unless you say so", "# My own note: it never installs one", 1)
    text = text.replace("install_on_server_restart = false\n", "# auto_update = true\n# beta = true\n", 1)
    text = text.replace("# true: install a new version by itself the next time a server restarts.\n", "", 1)
    settings_path.write_text(text, encoding="utf-8")
    assert not any("[updates]" in line for line in up_to_date(settings_path))
    after = settings_path.read_text(encoding="utf-8")
    table = after[after.index("\n[updates]\n"):after.index("\n[server_updates]\n")]
    assert table == "\n".join(["", "[updates]", *migrate.UPDATES_COMMENT_0_4_2,
                               "# My own note: it never installs one: when one is out, the window says so;",
                               "check = true", "check_hours = 6", *(f"# {c}" for c in migrate.AUTO_UPDATE_COMMENT),
                               "# auto_update = true", *(f"# {c}" for c in migrate.BETA_COMMENT), "# beta = true",
                               'repository = "LittleGroove/Project-Reclaimer-Huragok"', ""])
    assert "auto_update" not in read_toml(settings_path)["updates"] and "beta" not in read_toml(settings_path)["updates"]


def test_a_file_that_cant_be_read_is_never_touched(tmp_path: Path, caplog) -> None:
    settings_path = host(tmp_path)
    broken = settings_path.read_text(encoding="utf-8").replace("[setup]\n", "[setup\n", 1)
    settings_path.write_text(broken, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert not [line for line in up_to_date(settings_path) if line.startswith("settings.toml")]
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
    up_to_date(settings_path)
    text = commands.read_text(encoding="utf-8")
    without = re.sub(r'(?m)^\s*"whois",.*\n', "", text)
    assert without != text
    commands.write_text(without, encoding="utf-8")
    roles_before = load_permissions(commands)
    assert up_to_date(settings_path, settled=False) == []  # not as Huragok starts: see the test below
    changed = up_to_date(settings_path)
    assert changed == ["commands.toml: !whois (new) listed under [moderator], its default"]
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
    fresh = run_module.bring_files_up_to_date(args, settings)
    assert fresh is not settings and "download_game_files" in read_toml(settings_path)["setup"]

    def broken(*args: object) -> list[str]:
        raise RuntimeError("a bug in a step")

    monkeypatch.setattr(migrate, "bring_up_to_date", broken)
    assert run_module.bring_files_up_to_date(args, fresh) is fresh  # never keeps Huragok from starting


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


def test_a_new_command_waits_until_the_new_version_has_stayed(tmp_path: Path, monkeypatch) -> None:
    """The start loop that put this version in place is the old version's code: it puts the old version back if this
    one ends within its FAILED_START_SECONDS, and 0.4.0 to 0.4.4 refused to start with a command they don't have in
    commands.toml. So a new command is only listed once this version has been up longer than that."""
    assert migrate.SETTLED_SECONDS > launcher_module.FAILED_START_SECONDS
    settings_path = host(tmp_path)
    commands = settings_path.parent / "commands.toml"
    changed = up_to_date(settings_path, settled=False)  # as 0.5.0 starts
    assert not any("(new)" in line for line in changed)
    assert listed_commands(commands) <= COMMANDS_0_4_0  # what the version put back can read
    records, packages = settings_path.parent / "records", settings_path.parent / "PACKAGES"
    pending = migrate.pending(settings_path, records, packages)
    assert pending == [line + " (2.5 minutes after it starts: the version before can't read it)"
                       for line in [AUTOMOD_ORDER, *new_command_lines()]]
    assert load_permissions(commands).role_for(all_commands()["banword"]) == "admin"  # meanwhile: its default
    # Once this version has stayed up: the running Huragok lists them.
    assert migrate.bring_up_to_date(settings_path, records, packages, settled=True) == [AUTOMOD_ORDER,
                                                                                        *new_command_lines()]
    data = tomllib.loads(commands.read_text(encoding="utf-8"))
    assert {"banword", "unbanword", "banname"} <= set(data["admin"]["commands"])
    assert migrate.pending(settings_path, records, packages) == []


def test_the_running_huragok_lists_new_commands_once_it_has_stayed(tmp_path: Path, run, monkeypatch) -> None:
    from testkit.running import running

    monkeypatch.setattr(migrate, "SETTLED_SECONDS", 0.2)

    async def scenario() -> None:
        async with running(tmp_path, commands='[admin]\ncommands = ["ban"]\n') as (sup, _, _):
            from conftest import wait_for

            await wait_for(lambda: "banword" in sup.settings.permissions.roles)  # written, and read again
            assert sup.settings.permissions.roles["unbanword"] == "admin"

    run(scenario())


def test_banname_moves_to_the_admins_once(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    commands = settings_path.parent / "commands.toml"
    before = commands.read_text(encoding="utf-8")
    banname = all_commands()["banname"]
    assert load_permissions(commands).role_for(banname) == "moderator"  # 0.4.0's default
    assert any("commands.toml: !banname moved from [moderator] to [admin]" in line
               for line in up_to_date(settings_path, settled=False, notes=False))
    after = commands.read_text(encoding="utf-8")
    assert load_permissions(commands).role_for(banname) == "admin"
    assert f'    "banname",      # {banname.help}' not in after.split("[admin]")[0]  # out of [moderator]
    assert '"banname",' in after.split("[admin]")[1].split("[owner]")[0]  # into [admin], with what it does
    assert [line for line in before.splitlines() if line not in after.splitlines()] == [
        f'    "banname",      # {banname.help}']  # nothing else went
    backup = settings_path.parent / "records" / "previous-files" / __version__ / "commands.toml"
    assert backup.read_text(encoding="utf-8") == before
    # Done once: a host who puts it back under [moderator] keeps it there.
    commands.write_text(before, encoding="utf-8")
    assert not any("!banname " in line for line in up_to_date(settings_path))
    assert load_permissions(commands).role_for(banname) == "moderator"
    # A host who had moved it to another role keeps it there; one who switched it off keeps it off.
    settings_path = host(tmp_path / "owner")
    commands = settings_path.parent / "commands.toml"
    remove_from_list(commands, "moderator", "commands", "banname")
    add_to_list(commands, "owner", "commands", "banname")
    assert not any("!banname " in line for line in up_to_date(settings_path))
    assert load_permissions(commands).role_for(banname) == "owner"
    settings_path = host(tmp_path / "off")
    commands = settings_path.parent / "commands.toml"
    add_to_list(commands, "off", "commands", "banname")  # as D -> C switches one off: its role's line stays
    up_to_date(settings_path)
    assert "banname" in load_permissions(commands).off


# The word list with the mutes first (the host's call, 27 October) ------------------------------------------------

OLD_LINES = """\
word | 1d, 2d, 3d | 3, 4d, 5d, 6d, 6 | 10 | hate
slur | 1h, 1d, 7d | | 3 | slurs
# my own note, kept where it is
rude | | 0, 1d, 3d | | rude
spam | 1h, 1d, 2d | 2, 1d | 3 | spam
broken | 1x
kickword
"""


def test_a_word_list_from_before_gets_the_mutes_first_once_the_new_version_has_stayed(tmp_path: Path) -> None:
    """A host's word list as 0.4.0 to 0.5.0 wrote it (the kicks first): left as it is at the start, so the version
    before reads it as it did should the start loop put it back; read the new way meanwhile; rewritten with the
    mutes first, and this version's notes, once this version has stayed up."""
    from huragok.players.automod.rules import PARTS, kicks_first, ladder, read_rules_lenient

    settings_path = host(tmp_path)
    root = settings_path.parent
    words = root / "automod.txt"
    old = (HOST_0_4_0 / "automod.txt").read_text(encoding="utf-8") + OLD_LINES
    words.write_text(old, encoding="utf-8")
    before = [(r.name, ladder(r)) for r in read_rules_lenient(words)[0]]
    assert before[0] == ("hate", ["1st: muted for 4 days", "2nd: muted for 5 days", "3rd: muted for 6 days",
                                  "4th: removed for 1 day", "5th: removed for 2 days", "6th: removed for 3 days",
                                  "7th to 10th: removed for 3 days, then muted for good", "11th: banned for good"])
    changed = up_to_date(settings_path, settled=False)  # as the new version starts
    assert not any(line.startswith("automod.txt") for line in changed)
    assert words.read_text(encoding="utf-8") == old  # the version before still reads it as it did
    records, packages = root / "records", root / "PACKAGES"
    pending = migrate.pending(settings_path, records, packages)
    turned = (f"automod.txt: the mutes come first now ({PARTS}): the hate, slurs, rude and spam lines and line 49 "
              "rewritten so, the strikes that came before the mutes now before the kicks; Huragok's notes as this "
              "version writes them")
    later = " (2.5 minutes after it starts: the version before can't read it)"
    assert pending[:2] == [turned + later,
                           "automod.txt: the spam line: kick times 1d, 2d left out, the ban coming before them now"
                           + later]

    # Once it has stayed: the lines in the new layout, reading as they did meanwhile, and this version's notes.
    assert migrate.bring_up_to_date(settings_path, records, packages, settled=True)[:2] == [
        line.removesuffix(later) for line in pending[:2]]
    text = words.read_text(encoding="utf-8")
    assert not kicks_first(text) and text.startswith(template_text("automod.txt"))
    assert text[len(template_text("automod.txt")):].splitlines() == [
        "word | 4d, 5d, 6d, 6 | 3, 1d, 2d, 3d | 10 | hate", "slur | | 1h, 1d, 7d | 3 | slurs",
        "# my own note, kept where it is", "rude | 1d, 3d | | | rude", "spam | 1d | 2, 1h | 3 | spam",
        "broken | | 1x", "kickword"]
    assert [(r.name, ladder(r)) for r in read_rules_lenient(words)[0]] == before
    kept = records / "previous-files" / __version__ / "automod.txt"
    assert kept.read_text(encoding="utf-8") == old
    assert "0.5.1/automod-order" in json.loads((records / "files.json").read_text(encoding="utf-8"))["steps"]
    # Done once: the host's edits after it stay as they make them.
    assert not migrate.bring_up_to_date(settings_path, records, packages, settled=True)
    assert words.read_text(encoding="utf-8") == text


def test_a_word_list_with_no_note_naming_its_parts_says_its_old_layout_first(tmp_path: Path) -> None:
    """A host who took Huragok's notes out of 0.4.x's word list: a note naming its parts the old way goes at the top
    as the new version starts, so it's read as written until it's rewritten."""
    from huragok.players.automod.rules import kicks_first, read_rules_lenient

    settings_path = host(tmp_path)
    words = settings_path.parent / "automod.txt"
    words.write_text(OLD_LINES, encoding="utf-8")
    changed = up_to_date(settings_path, settled=False)
    assert "automod.txt: a note naming its parts as they were up to 0.5.0 (words | kicks | mutes | ban | name) put " \
           "at the top, so its lines are read as they were written until they're rewritten with the mutes first" in changed
    assert words.read_text(encoding="utf-8") == "#   words | kicks | mutes | ban | name\n" + OLD_LINES
    assert kicks_first(words.read_text(encoding="utf-8"))
    assert read_rules_lenient(words)[0][3].mutes == ["1d"]  # spam: 1d, after the 2 strikes before the kicks
    up_to_date(settings_path)
    assert not kicks_first(words.read_text(encoding="utf-8"))
    assert "rude | 1d, 3d | | | rude" in words.read_text(encoding="utf-8").splitlines()


def test_a_word_list_in_the_new_layout_is_left_as_it_is(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    words = settings_path.parent / "automod.txt"
    words.write_text(template_text("automod.txt") + "rude | 1d, 3d | 2 | | rude\n", encoding="utf-8")
    before = words.read_text(encoding="utf-8")
    assert not any(line.startswith("automod.txt") for line in up_to_date(settings_path))
    assert words.read_text(encoding="utf-8") == before
