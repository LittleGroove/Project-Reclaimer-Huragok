"""Huragok's notes in a host's files, brought up to date by every update, the host's own notes kept where they put
them (updates/notes.py). Small made-up files show each rule; the real ones show it on what hosts have."""

from __future__ import annotations

import json
import logging
import random
import tomllib
from pathlib import Path

import pytest

from testkit import ENGINE
from testkit.hostfiles import HOST_0_4_0, host, up_to_date

from huragok import __version__
from huragok.updates import hostfiles as migrate
from huragok.updates.notes import (
    COMMANDS, KINDS, NotesError, collect, load_history, merged, refresh, refresh_text, template_text,
)


def history_of(kind: str, *texts: str) -> dict:
    history: dict = {}
    for text in texts:
        merged(history, kind, collect(kind, text))
    return history


def brought_up(kind: str, text: str, old: str, new: str) -> str:
    """`text` (a host's file from `old`) as the version with `new` leaves it; a second time changes nothing."""
    history = history_of(kind, old, new)
    once = refresh_text(kind, text, template=new, history=history).text
    assert refresh_text(kind, once, template=new, history=history).text == once
    return once


# A list: automod.txt, names.txt, intervalmessages.txt -------------------------------------------------------------

OLD_LIST = """\
# The word list. KEEP IT CLEAN!
#
# words  The words.
# kicks  How long each kick lasts.
#        The last one repeats.
#
# Your rules go below.

"""
NEW_LIST = """\
# The word list. KEEP IT CLEAN!
#
# words  The words, separated by commas.
# kicks  How long each kick lasts.
#        The last one repeats until the mutes.
# mutes  How long each mute lasts.
#
# Your rules go below.

"""
NEW_TOP = NEW_LIST.rstrip("\n").split("\n")


def test_the_hosts_notes_stay_where_they_put_them() -> None:
    host_file = OLD_LIST.replace("# words  The words.\n", "# words  The words.\n# (we keep this list short)\n") + (
        "\n# ── my own lines ──\nbadword | | | 0 | slurs\n# below: the mild ones\nmild\n")
    assert brought_up("automod.txt", host_file, OLD_LIST, NEW_LIST) == "\n".join([
        "# The word list. KEEP IT CLEAN!",
        "#",
        "# words  The words, separated by commas.",
        "# (we keep this list short)",  # after the line it followed, in its new words
        "# kicks  How long each kick lasts.",
        "#        The last one repeats until the mutes.",
        "# mutes  How long each mute lasts.",
        "#",
        "# Your rules go below.",
        "",
        "",  # their spacing, not Huragok's and theirs added up
        "# ── my own lines ──",  # right above their rule, as it was
        "badword | | | 0 | slurs",
        "# below: the mild ones",  # among their rules: never touched
        "mild", ""])


def test_a_host_who_reworded_a_line_keeps_their_words() -> None:
    reworded = OLD_LIST.replace("How long each kick lasts.", "How long each kick keeps them out.")
    out = brought_up("automod.txt", reworded + "badword\n", OLD_LIST, NEW_LIST).split("\n")
    assert out[:6] == [*NEW_TOP[:3], "# kicks  How long each kick keeps them out.", *NEW_TOP[4:6]]
    assert refresh_text("automod.txt", reworded, template=NEW_LIST,
                        history=history_of("automod.txt", OLD_LIST, NEW_LIST)).reworded == 1


def test_notes_the_host_deleted_come_back() -> None:
    trimmed = OLD_LIST.replace("# words  The words.\n# kicks  How long each kick lasts.\n#        The last one "
                               "repeats.\n", "")
    assert brought_up("automod.txt", trimmed + "badword\n", OLD_LIST, NEW_LIST) == NEW_LIST + "badword\n"
    assert brought_up("automod.txt", "badword\n", OLD_LIST, NEW_LIST) == NEW_LIST + "badword\n"  # no notes at all
    assert brought_up("automod.txt", "", OLD_LIST, NEW_LIST) == NEW_LIST


def test_a_rule_put_among_huragoks_notes_ends_up_below_them() -> None:
    split = OLD_LIST.replace("# words  The words.\n", "# words  The words.\nbadword | | | 0 | slurs\n") + "mild\n"
    assert brought_up("automod.txt", split, OLD_LIST, NEW_LIST) == NEW_LIST + "badword | | | 0 | slurs\n\nmild\n"


def test_a_line_is_huragoks_only_word_for_word() -> None:
    """A host's note that only looks like one of Huragok's, below the notes, is theirs: kept."""
    text = OLD_LIST + "badword\n# Your rules\n#\nmild\n"
    assert brought_up("automod.txt", text, OLD_LIST, NEW_LIST) == NEW_LIST + "badword\n# Your rules\n#\nmild\n"


# A TOML file: settings.toml, commands.toml ------------------------------------------------------------------------

OLD_TOML = """\
# Huragok: how it runs.

# Where the servers are.
servers = "SERVERS"
mode = "act"                 # "watch": only report

[process]
# How it's run.
# command = ["wine", "{executable}"]
command = []
# start = "systemctl start x"
stop_timeout = 30
"""
NEW_TOML = OLD_TOML.replace("# Where the servers are.", "# Where the server folders are.").replace(
    '# "watch": only report', '# "watch": only report, never act').replace(
    "stop_timeout = 30", "stop_timeout = 30           # seconds")


def test_a_settings_file_keeps_the_hosts_notes_values_and_choices() -> None:
    text = OLD_TOML.replace('servers = "SERVERS"', '# moved to D: in 2025\nservers = \'D:/Servers\'').replace(
        'mode = "act"                 # "watch": only report', 'mode = "watch"   # we only watch for now').replace(
        '# start = "systemctl start x"', 'start = "systemctl start mine"')
    assert brought_up("settings.toml", text, OLD_TOML, NEW_TOML) == "\n".join([
        "# Huragok: how it runs.",
        "",
        "# Where the server folders are.",
        "# moved to D: in 2025",  # right above the setting, as it was
        "servers = 'D:/Servers'",
        'mode = "watch"   # we only watch for now',  # their own note at the end of the line
        "",
        "[process]",
        "# How it's run.",
        '# command = ["wine", "{executable}"]',  # an example for a setting a new host's file sets too: kept
        "command = []",
        'start = "systemctl start mine"',  # the example they switched on isn't put back above it
        "stop_timeout = 30           # seconds",  # a new note at the end of a line, lined up as a new host's
        ""])


def test_a_setting_the_host_made_a_comment_keeps_its_notes() -> None:
    text = OLD_TOML.replace('servers = "SERVERS"', '# servers = "SERVERS"  # the default is fine')
    out = brought_up("settings.toml", text, OLD_TOML, NEW_TOML)
    assert '# Where the server folders are.\n# servers = "SERVERS"  # the default is fine\nmode = "act"' in out
    assert tomllib.loads(out) == tomllib.loads(text)


def test_an_end_of_line_note_brought_up_to_date() -> None:
    out = brought_up("settings.toml", OLD_TOML, OLD_TOML, NEW_TOML)
    assert out == NEW_TOML  # a file just as the old version wrote it: just as the new one does


def test_commands_notes_follow_what_each_command_does() -> None:
    history = load_history()
    old_mute = "Nobody hears their voice or reads their chat, e.g. !mute Bravo 30m spam"
    assert old_mute in history[COMMANDS]["commands"]["mute"]  # what 0.4.0 to 0.4.4 wrote beside !mute
    player = "0123456789abcdef" * 4
    text = template_text(COMMANDS).replace(
        f'    "mute",          # {template_mute()}', f'    "mute",  # {old_mute}').replace(
        f'    "warn",          # {template_note("warn")}', '    "warn",  # we use this a lot').replace(
        f'    "kick",          # {template_note("kick")}', '    "kick",').replace(
        "[moderator]\nplayers = [\n", f'[moderator]\nplayers = [\n    "{player}",  # Bravo: promoted by Alice\n')
    out = refresh_text(COMMANDS, text)
    assert out.text.count(f'    "mute",          # {template_mute()}\n') == 1  # this version's, lined up
    assert '    "warn",  # we use this a lot\n' in out.text  # the host's own
    assert f'    "kick",          # {template_note("kick")}\n' in out.text  # put back
    assert f'    "{player}",  # Bravo: promoted by Alice\n' in out.text  # a note about a player: never touched
    assert refresh_text(COMMANDS, out.text).text == out.text


def template_note(name: str) -> str:
    from huragok.core.commandsfile import all_commands

    return all_commands()[name].help


def template_mute() -> str:
    return template_note("mute")


# The real files ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", KINDS)
def test_a_new_hosts_files_have_this_versions_notes_already(kind: str) -> None:
    text = template_text(kind)
    assert refresh(kind, text).text == text


@pytest.mark.parametrize("kind", KINDS)
def test_every_note_this_version_writes_is_in_the_history(kind: str) -> None:
    """The next version can only tell this one's notes from a host's if the history lists them: after changing a
    note, run  python tools/notes_history.py  (it adds this version's)."""
    history = load_history().get(kind, {})
    for part, things in collect(kind, template_text(kind)).items():
        for thing, lines in things.items():
            missing = [line for line in lines if line not in history.get(part, {}).get(thing, [])]
            assert not missing, f"{kind} {part} {thing}: not in notes-history.json (python tools/notes_history.py)"


def test_the_notes_steps_write_are_in_the_history() -> None:
    """Notes that steps put in settings.toml with the settings they add are Huragok's too."""
    every = {line for lines in load_history()["settings.toml"]["notes"].values() for line in lines}
    for name in dir(migrate):
        value = getattr(migrate, name)
        if name.endswith("_COMMENT") or "_COMMENT_" in name:
            lines = [line if line.startswith("#") else f"# {line}" for line in value]
            assert all(line.strip() in every for line in lines), name


def test_the_history_only_grows() -> None:
    data = json.loads((ENGINE / "huragok" / "templates" / "notes-history.json").read_text(encoding="utf-8"))
    assert data["versions"][:5] == ["0.4.0", "0.4.1", "0.4.2", "0.4.3", "0.4.4"] and __version__ in data["versions"]


def host_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("# host note ")]


@pytest.mark.parametrize("seed", range(25))
def test_whatever_the_host_wrote_and_wherever_nothing_read_changes(seed: int) -> None:
    """A host's 0.4.0 files with notes of their own at random places, settings switched on and off, values changed:
    what's read is the same afterwards, every note of theirs is there once and in its order, a second time changes
    nothing."""
    chance = random.Random(seed)
    for name, kind in (("settings.toml", "settings.toml"), ("commands.toml", COMMANDS), ("automod.txt", "automod.txt"),
                       ("names.txt", "names.txt")):
        lines = (HOST_0_4_0 / name).read_text(encoding="utf-8").split("\n")
        if kind == "automod.txt":
            lines[chance.randrange(len(lines)):0] = ["badword | 1h | 0, 1d | 3 | slurs"]
        for number in range(chance.randint(1, 6)):
            lines.insert(chance.randrange(len(lines) + 1), f"# host note {number}")
        text = "\n".join(lines)
        try:
            tomllib.loads(text) if kind in ("settings.toml", COMMANDS) else None
        except tomllib.TOMLDecodeError:
            continue  # a note put inside a multi-line string: not a file a host could have
        once = refresh_text(kind, text).text
        assert host_lines(once) == host_lines(text), (name, seed)
        assert refresh_text(kind, once).text == once, (name, seed)


# The step that runs it --------------------------------------------------------------------------------------------

def test_once_a_version_with_a_copy_of_each_file_and_the_log_says_so(tmp_path: Path, caplog) -> None:
    settings_path = host(tmp_path)
    root = settings_path.parent
    (root / "SERVERS" / "Slayer").mkdir()
    (root / "SERVERS" / "Slayer" / "dedicated.toml").write_text("", encoding="utf-8")
    up_to_date(settings_path, notes=False)
    words = root / "automod.txt"
    before = words.read_text(encoding="utf-8")
    own = root / "SERVERS" / "Slayer" / "intervalmessages.txt"
    own.write_text("# Slayer's messages\n30m | Slayer only\n", encoding="utf-8")
    records, packages = root / "records", root / "PACKAGES"
    pending = migrate.pending(settings_path, records, packages)
    assert "automod.txt: Huragok's notes as this version writes them" in pending
    assert "Slayer/intervalmessages.txt: Huragok's notes as this version writes them (your 1 own note kept where " \
           "you put it)" in pending
    assert words.read_text(encoding="utf-8") == before  # `check` changes nothing
    with caplog.at_level(logging.INFO):
        changed = migrate.bring_up_to_date(settings_path, records, packages)
    assert set(pending) == set(changed) and f"Brought up to date for Huragok {__version__}: automod.txt" in caplog.text
    assert words.read_text(encoding="utf-8") == template_text("automod.txt")
    assert own.read_text(encoding="utf-8") == template_text("intervalmessages-server.txt") + (
        "# Slayer's messages\n30m | Slayer only\n")
    kept = records / "previous-files" / __version__
    assert (kept / "automod.txt").read_text(encoding="utf-8") == before
    assert (kept / "Slayer" / "intervalmessages.txt").read_text(encoding="utf-8") == (
        "# Slayer's messages\n30m | Slayer only\n")
    assert f"{__version__}/notes" in json.loads((records / "files.json").read_text(encoding="utf-8"))["steps"]
    # Done for this version: the host's edits after it stay as they make them.
    words.write_text("badword\n", encoding="utf-8")
    assert migrate.bring_up_to_date(settings_path, records, packages) == []
    assert words.read_text(encoding="utf-8") == "badword\n"


def test_a_file_that_cant_be_read_is_left_and_tried_again(tmp_path: Path, caplog) -> None:
    settings_path = host(tmp_path)
    root = settings_path.parent
    up_to_date(settings_path, notes=False)
    names = root / "names.txt"
    names.write_bytes("caf\u00e9\n".encode("cp1252"))  # ANSI
    with caplog.at_level(logging.INFO):
        changed = migrate.bring_up_to_date(settings_path, root / "records", root / "PACKAGES")
    assert changed == []  # the step didn't finish…
    assert "names.txt isn't saved as UTF-8" in caplog.text and "tried again at the next start" in caplog.text
    assert "Brought up to date for Huragok 0.5.0: automod.txt: Huragok's notes" in caplog.text  # …but did the others
    assert names.read_bytes() == "caf\u00e9\n".encode("cp1252")
    names.write_text("cafe\n", encoding="utf-8")
    assert migrate.bring_up_to_date(settings_path, root / "records", root / "PACKAGES") == [
        "names.txt: Huragok's notes as this version writes them"]


def test_a_result_that_would_read_differently_is_never_written(monkeypatch) -> None:
    """The last check before writing, for a fault in the merging: a setting's value, or a rule, that changed."""
    from huragok.updates import notes

    monkeypatch.setattr(notes, "refresh", lambda kind, text, **given: notes.Refreshed(text.replace("1", "2")))
    with pytest.raises(NotesError, match="would read differently"):
        refresh_text("settings.toml", "# A.\na = 1\n")
    with pytest.raises(NotesError, match="would read differently"):
        refresh_text("automod.txt", "# A.\nword | 1h\n")
    assert refresh_text("automod.txt", "# A 1.\nword | 2h\n").text == "# A 2.\nword | 2h\n"  # notes alone: fine
