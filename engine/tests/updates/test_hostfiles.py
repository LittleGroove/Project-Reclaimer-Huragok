"""The host's own files after an update: every release reads them as any version from 0.4.0 on wrote them, and
brings them up to date itself (migrate.py). tests/data/host-0.4.0/ holds the files 0.4.0's first start wrote."""

from __future__ import annotations

import json
from contextlib import closing
import logging
import re
import shutil
import tomllib
from types import SimpleNamespace
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
NEW_COMMANDS = {"report", "allowvote", "banall", "bannames", "bans", "banword", "calladmin", "changebanword", "confirm", "deny", "endseason", "kicks", "link", "mods", "mutes", "newseason", "togglesides",
                "novote", "novotes", "reports", "requests", "respawn", "restartserver", "rules", "said", "season", "side", "stats", "toggleglobal", "toggleproxy",
                "toggleteam", "togglevoice", "unbanword"}


# The top rank is faculty (0.6.1), once this version has stayed: 0.4.0's commands.toml has [faculty], its settings.toml
# exempt_roles names it.
FACULTY = ["commands.toml: [owner] is [faculty] now, everyone in it kept", "settings.toml: the rank owner is faculty now, where a setting names it"]
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
    # Retired keys and unknown ones alike: none but 0.6.0's restarts_per_hour, which the update takes out
    assert [line for line in settings.retired if "restarts_per_hour" not in line] == [] and len(settings.retired) == 1
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
        "discord.toml: new, for the Discord bot; it's off until you set it up (J in the window)",
        "settings.toml: [automod] moderators_kick_at_most = \"1d\" added: the longest kick with a time a moderator can give (new in 0.6.0)",
        "settings.toml: [workshop_updates] adopt = true added: a mod copied into a server's content by hand that names a "
        "Workshop item is kept up to date too (new in 0.6.0)",
        "settings.toml: [workshop_updates] layouts = true added: the Forge layouts mods ship are copied into "
        "content/Maps and kept in step with each new version (new in 0.6.0)",
        "settings.toml: [restarts] restarts_per_hour taken out: automatic restarts aren't held to a number an hour "
        "since 0.6.0",
        "settings.toml: [watchdog] memory = \"watch\": it only warns unless you turn it on (\"act\"), and counts growth "
        "past memory_mb once a run has settled (new in 0.6.0)",
        *(f"{name}: Huragok's notes as this version writes them"
          for name in ("settings.toml", "commands.toml", "names.txt")),
        "PACKAGES/every-halo: a new starter package",  # 0.6.2's, for Project Reclaimer 0.10
        AUTOMOD_ORDER, *FACULTY, *new_command_lines()]
    assert f"Brought up to date for Huragok {__version__}" in caplog.text
    text = settings_path.read_text(encoding="utf-8")
    template = TEMPLATE.read_text(encoding="utf-8")
    added = ([f"# {c}" for c in migrate.NAME_MATCH_COMMENT] + ['name_match = "anywhere"']
             + [f"# {c}" for c in migrate.ACROSS_COMMENT] + ["across_servers = true"]
             + [f"# {c}" for c in migrate.KICK_LIMIT_COMMENT] + ['moderators_kick_at_most = "1d"']
             + [f"# {c}" for c in migrate.ADOPT_COMMENT] + ["adopt = true"]
             + [f"# {c}" for c in migrate.LAYOUTS_COMMENT] + ["layouts = true"]
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
    text = text.replace("check_names = true ", '# name_match = "words"\n# across_servers = false\n# moderators_kick_at_most = "2d"\ncheck_names = true ',
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
                       for line in [AUTOMOD_ORDER, *FACULTY, *new_command_lines()]]
    assert load_permissions(commands).role_for(all_commands()["banword"]) == "admin"  # meanwhile: its default
    # Once this version has stayed up: the running Huragok lists them.
    assert migrate.bring_up_to_date(settings_path, records, packages, settled=True) == [AUTOMOD_ORDER, *FACULTY,
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
    assert '"banname",' in after.split("[admin]")[1].split("[faculty]")[0]  # into [admin], with what it does
    assert [line for line in before.splitlines() if line not in after.splitlines()] == [
        f'    "banname",      # {banname.help}']  # nothing else went
    backup = settings_path.parent / "records" / "previous-files" / __version__ / "commands.toml"
    assert backup.read_text(encoding="utf-8") == before
    # Done once: a host who puts it back under [moderator] keeps it there.
    commands.write_text(before, encoding="utf-8")
    assert not any("!banname " in line for line in up_to_date(settings_path))
    assert load_permissions(commands).role_for(banname) == "moderator"
    # A host who had moved it to another role keeps it there; one who switched it off keeps it off.
    settings_path = host(tmp_path / "faculty")
    commands = settings_path.parent / "commands.toml"
    remove_from_list(commands, "moderator", "commands", "banname")
    add_to_list(commands, "faculty", "commands", "banname")
    assert not any("!banname " in line for line in up_to_date(settings_path))
    assert load_permissions(commands).role_for(banname) == "faculty"
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
                                  "4th: kicked for 1 day", "5th: kicked for 2 days", "6th: kicked for 3 days",
                                  "7th to 10th: kicked for 3 days, then muted for good", "11th: banned for good"])
    changed = up_to_date(settings_path, settled=False)  # as the new version starts
    assert not any(line.startswith("automod.txt") for line in changed)
    assert words.read_text(encoding="utf-8") == old  # the version before still reads it as it did
    records, packages = root / "records", root / "PACKAGES"
    pending = migrate.pending(settings_path, records, packages)
    turned = (f"automod.txt: the mutes come first now ({PARTS}): the hate, slurs, rude and spam lines and line 49 "
              "rewritten so, the strikes that came before the mutes now before the kicks; Huragok's notes as this "
              "version writes them")
    later = " (2.5 minutes after it starts: the version before can't read it)"
    assert pending[:3] == [turned + later,
                           "automod.txt: the spam line: kick times 1d, 2d left out, the ban coming before them now"
                           + later,
                           "automod.txt: line 50 had nothing in its mutes, kicks and ban, the server's kick every time: a "
                           "0 in its kicks keeps it kicking, as a line with nothing in them only reports now (a ticket "
                           "for the staff on Discord)" + later]

    # Once it has stayed: the lines in the new layout, reading as they did meanwhile, and this version's notes.
    assert migrate.bring_up_to_date(settings_path, records, packages, settled=True)[:2] == [
        line.removesuffix(later) for line in pending[:2]]
    text = words.read_text(encoding="utf-8")
    assert not kicks_first(text) and text.startswith(template_text("automod.txt"))
    assert text[len(template_text("automod.txt")):].splitlines() == [
        "word | 4d, 5d, 6d, 6 | 3, 1d, 2d, 3d | 10 | hate", "slur | | 1h, 1d, 7d | 3 | slurs",
        "# my own note, kept where it is", "rude | 1d, 3d | | | rude", "spam | 1d | 2, 1h | 3 | spam",
        "broken | | 1x", "kickword | | 0"]
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


def test_a_line_with_nothing_in_it_goes_on_kicking_after_the_update(tmp_path: Path) -> None:
    """Up to 0.6.0 a line with nothing in its mutes, kicks and ban was the server's kick every time; now such a line
    only reports (the host's design, 2 November). The host's lines like that get the 0 that keeps them kicking, written
    in where they had it, the rest of the line as they wrote it; the version before reads them the same. A line the
    host writes later with nothing in it only reports."""
    from huragok.players.automod.rules import ladder, parse_line, read_rules_lenient

    settings_path = host(tmp_path)
    words = settings_path.parent / "automod.txt"
    lines = ["troon                   |              |                |     | transphobia", "grimbo", "solo |",
             "spam | | | 3 | spam", "w | | | oldname", "rude | 1d, 3d | 2 | | rude", "broken | 1x", "| | | | voice"]
    words.write_text(template_text("automod.txt") + "\n".join(lines) + "\n", encoding="utf-8")
    untouched = ("spam", "rude")
    before = [(r.name, ladder(r)) for r in read_rules_lenient(words)[0] if r.name in untouched]
    changed = up_to_date(settings_path)
    first = len(template_text("automod.txt").splitlines()) + 1  # by their line numbers in the file, as the host sees them
    assert (f"automod.txt: the transphobia and voice lines and lines {first + 1} and {first + 2} had nothing in their mutes, kicks and ban, "
            "the server's kick every time: a 0 in their kicks keeps them kicking, as a line with nothing in them only "
            "reports now (a ticket for the staff on Discord)") in changed
    text = words.read_text(encoding="utf-8")
    assert text[len(template_text("automod.txt")):].splitlines() == [
        "troon                   |              | 0              |     | transphobia", "grimbo | | 0", "solo | | 0",
        "spam | | | 3 | spam", "w | | | oldname", "rude | 1d, 3d | 2 | | rude", "broken | 1x", "| | 0 | | voice"]
    rules = read_rules_lenient(words)[0]
    assert not any(r.reports for r in rules)
    assert {r.name: ladder(r) for r in rules if r.name in ("transphobia", "rule2", "rule3", "voice", "oldname")} == {
        name: ["every time: kicked"] for name in ("transphobia", "rule2", "rule3", "voice", "oldname")}  # oldname: the 0.2 layout's, as ever
    assert [(r.name, ladder(r)) for r in rules if r.name in untouched] == before
    assert not parse_line("w | | 0", 1).reports  # and the version before reads a 0 there as the same kick
    assert not any(line.startswith("automod.txt") for line in up_to_date(settings_path))  # once
    words.write_text(text + "askword | | | | ask\n", encoding="utf-8")  # one the host adds now: theirs, and it reports
    assert not any(line.startswith("automod.txt") for line in up_to_date(settings_path))
    assert read_rules_lenient(words)[0][-1].reports


def test_a_new_hosts_own_word_list_is_never_taken_for_one_from_before(tmp_path: Path) -> None:
    """The playlists thread's audit (2 November): a first start only writes the files, so the second ran every step,
    and a new host's own list in today's layout, with no note naming its parts, got the old layout's note and then its
    mutes and kicks swapped for good (7d lost); a line that only reports would have been made a kick. A word list
    Huragok writes itself is this version's: its steps are done."""
    from huragok.commandline import firstrun
    from huragok.players.automod.rules import kicks_first, parse_text

    assert set(migrate.WORD_LIST_STEPS) <= {step.key for step in migrate.STEPS}
    settings_path = tmp_path / "settings.toml"
    firstrun.write_files(tmp_path, settings_path=settings_path)  # the first start
    words = tmp_path / "automod.txt"
    mine = "badword, otherword | 1d, 7d | 2, 1h | 3 | slurs\ntheword | | | | badwords\n"
    words.write_text(mine, encoding="utf-8")
    changed = up_to_date(settings_path)  # the second start, and once it's settled
    assert [line for line in changed if line.startswith("automod.txt")] == ["automod.txt: Huragok's notes as this version writes them"]
    text = words.read_text(encoding="utf-8")
    assert not kicks_first(text) and [line for line in text.splitlines() if not line.startswith("#") and line.strip()] == mine.splitlines()
    rules = parse_text(text)
    assert rules[0].mutes == ["1d", "7d"] and rules[0].kicks == ["1h"] and rules[1].reports
    done = json.loads((tmp_path / "records" / "files.json").read_text(encoding="utf-8"))["steps"]
    assert "0.6.0/discord-file" in done or (tmp_path / "discord.toml").exists()  # the rest ran as ever
    firstrun.write_files(tmp_path, settings_path=settings_path)  # every start after: nothing more
    assert json.loads((tmp_path / "records" / "files.json").read_text(encoding="utf-8"))["steps"] == done


def _beta_discord_file(tmp_path: Path) -> Path:
    """discord.toml as 0.6.0-beta.1's update wrote it, as far as this step reads it."""
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\n\n[channels]\nstaff = "1"\npublic = "2"\nappeals = "3"\nlogs = "4"\n\n[status.board]\nenabled = true\nchannel = "public"\n\n[messages]\n'
                    'moderation-kick = { channel = "staff", colour = "orange", post = true, ping = "" }\n'
                    'moderation-ban = { channel = "alerts", colour = "crimson", post = true, ping = "" }\n'
                    'seeding-wanted = { channel = "public", colour = "chartreuse", post = true, ping = "@Seeder" }\n'
                    'servers-countdown = { channel = "alerts", colour = "amber", post = true, ping = "" }\n'
                    'games-started = { channel = "games", colour = "green", post = true, ping = "" }\n'
                    'vergil-strike = { channel = "staff", colour = "gold", post = true, ping = "" }\n', encoding="utf-8")
    return path


def _run_step(tmp_path: Path, *, dry: bool = False) -> list[str]:
    from types import SimpleNamespace

    from huragok.updates.hostfiles import discord_channels

    return discord_channels(SimpleNamespace(home=tmp_path, dry=dry, backup=lambda path: None))


def test_a_betas_discord_file_moves_to_the_new_channels(tmp_path: Path) -> None:
    path = _beta_discord_file(tmp_path)
    said = _run_step(tmp_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["channels"]["status"] == "2" and "public" not in data["channels"]
    assert data["channels"]["appeals"] == "3"  # a channel of their own again: nothing that was set moves
    assert data["status"]["board"]["channel"] == "status"
    assert data["messages"]["moderation-kick"]["channel"] == "logs"
    assert data["messages"]["seeding-wanted"]["channel"] == "info" and data["messages"]["seeding-wanted"]["ping"] == "@Seeder"  # only where it goes
    assert data["messages"]["servers-countdown"] == {"channel": "info", "colour": "amber", "post": False, "ping": ""}
    assert data["messages"]["games-started"]["post"] is False
    assert any("status now" in line for line in said) and any("lines moved" in line for line in said)


def test_a_line_the_host_pointed_elsewhere_stays_theirs(tmp_path: Path) -> None:
    path = _beta_discord_file(tmp_path)
    _run_step(tmp_path)
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["messages"]["moderation-ban"]["channel"] == "alerts"
    assert data["messages"]["vergil-strike"]["channel"] == "staff"


def test_moving_channels_only_says_so_when_it_is_only_asked(tmp_path: Path) -> None:
    path = _beta_discord_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert _run_step(tmp_path, dry=True)
    assert path.read_text(encoding="utf-8") == before


def test_a_file_already_on_the_new_channels_is_left_alone(tmp_path: Path) -> None:
    from huragok.updates.notes import template_text

    path = tmp_path / "discord.toml"
    path.write_text(template_text("discord.toml"), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    assert _run_step(tmp_path) == []
    assert path.read_text(encoding="utf-8") == before


def test_the_first_betas_lists_of_buttons_are_brought_up_to_date(tmp_path: Path) -> None:
    """A list still as the beta wrote it: its names mean nothing now, and whole buttons were missing."""
    from huragok.discordbot.config import load
    from huragok.updates.hostfiles import OLD_BUTTONS, discord_buttons

    path = tmp_path / "discord.toml"
    path.write_text('mode = "off"\n\n[reports]\nenabled = true\nbuttons = ["mute-30m", "kick", "ban-1d", "ban-7d", "ban", "whois", "dismiss"]\n\n[appeals]\nbuttons = ["unban", "shorten", "deny"]\n', encoding="utf-8")
    ctx = SimpleNamespace(home=tmp_path, dry=False, backup=lambda _path: None)
    assert len(discord_buttons(ctx)) == 2  # type: ignore[arg-type]
    config = load(path)
    assert config.section("reports")["buttons"] == OLD_BUTTONS["reports"][1] and config.section("appeals")["buttons"] == OLD_BUTTONS["appeals"][1]
    assert config.section("reports")["enabled"] is True and not config.warnings
    assert discord_buttons(ctx) == []  # type: ignore[arg-type]


def test_a_list_the_host_changed_is_theirs(tmp_path: Path) -> None:
    from huragok.updates.hostfiles import discord_buttons

    path = tmp_path / "discord.toml"
    path.write_text('[reports]\nbuttons = ["kick"]\n', encoding="utf-8")
    assert discord_buttons(SimpleNamespace(home=tmp_path, dry=False, backup=lambda _path: None)) == []  # type: ignore[arg-type]
    assert 'buttons = ["kick"]' in path.read_text(encoding="utf-8")


def test_a_restart_budget_of_the_hosts_own_becomes_a_comment(tmp_path: Path) -> None:
    """restarts_per_hour does nothing from 0.6.0: Huragok's own 6 is taken out (above), and a number the host set is
    made a comment saying why, so they still see it. Huragok's old note above it goes either way."""
    from huragok.core.tomledit import set_table_key

    settings_path = host(tmp_path)
    set_table_key(settings_path, "restarts", "restarts_per_hour", 10)
    changed = up_to_date(settings_path)
    assert "settings.toml: [restarts] restarts_per_hour made a comment: automatic restarts aren't held to a number an "\
           "hour since 0.6.0" in changed
    text = settings_path.read_text(encoding="utf-8")
    assert "\n# restarts_per_hour = 10\n" in text and "# Not read since 0.6.0: automatic restarts" in text
    assert migrate.RESTART_BUDGET_OLD_NOTE not in text
    assert load(settings_path).retired == []


def test_the_top_rank_is_faculty_in_every_file(tmp_path: Path) -> None:
    """The host's call, 3 November 2026: "owner" is "faculty" wherever it's written, in the files and in the records.
    Settled, as the version before knows only the old name; this one reads both until it's gone."""
    import sqlite3

    settings_path = host(tmp_path)  # 0.4.0's files: [owner] in commands.toml, "owner" in exempt_roles
    commands = settings_path.parent / "commands.toml"
    commands.write_text(commands.read_text(encoding="utf-8").replace(
        "[owner]\nplayers = [\n", "[owner]\nplayers = [\n    \"" + "a" * 64 + "\",  # the host\n"), encoding="utf-8")
    assert load_permissions(commands).members["faculty"] == {"a" * 64}  # [owner] still reads
    discord = settings_path.parent / "discord.toml"
    discord.write_text('mode = "off"\n[ranks]\nowners = ["3000"]\nowners_in_game = "owner"\n[tickets]\nclose_rank = "owner"\n', encoding="utf-8")
    database = settings_path.parent / "records" / "huragok.sqlite3"
    database.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE actions (number INTEGER PRIMARY KEY, by_role TEXT)")
        db.executemany("INSERT INTO actions (by_role) VALUES (?)", [("owner",), ("admin",), (None,)])
        db.commit()
    changed = up_to_date(settings_path, settled=False)
    assert "[owner]" in commands.read_text(encoding="utf-8")  # not until this version has stayed: the one before reads [owner]
    changed = up_to_date(settings_path)
    text = commands.read_text(encoding="utf-8")
    assert "[faculty]" in text and "[owner]" not in text and "# the host" in text
    assert load_permissions(commands).members["faculty"] == {"a" * 64}
    assert '"faculty"' in settings_path.read_text(encoding="utf-8") and load(settings_path).parts["automod"].options["exempt_roles"] == ["admin", "faculty"]
    data = tomllib.loads(discord.read_text(encoding="utf-8"))
    assert data["ranks"]["faculty"] == ["3000"] and data["ranks"]["faculty_in_game"] == "faculty" and data["tickets"]["close_rank"] == "faculty"
    assert "owners" not in data["ranks"] and "owners_in_game" not in data["ranks"]
    with closing(sqlite3.connect(database)) as db:
        assert [row[0] for row in db.execute("SELECT by_role FROM actions ORDER BY number")] == ["faculty", "admin", None]
    assert any("[owner] is [faculty]" in line for line in changed)
