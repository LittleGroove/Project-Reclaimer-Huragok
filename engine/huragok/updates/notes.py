"""Huragok's notes in the host's files, as this version writes them, at every update (from 0.5.0).

settings.toml, commands.toml, automod.txt, names.txt and the intervalmessages.txt files are written once, by the
version that made them, with notes (# lines) saying how each works; before 0.5.0 an update never changed them, but
for a few notes in settings.toml with a step of their own each. Now the first start of every version puts
Huragok's notes in as this version has them, and keeps every note the host wrote, where they wrote it.

Huragok's own lines are known word for word: templates/notes-history.json lists every note line each version since
0.4.0 wrote in each file, by the thing it was written above (tools/notes_history.py adds a version's; a test fails
when this version writes a note it doesn't list). Any other note line is the host's.

Every note belongs to the thing below it. In a TOML file that's a setting, a table, or a setting the host made a
comment (`# check_hours = 6`); in a list (automod.txt, names.txt, intervalmessages.txt), the list itself: its notes
run from the top of the file to its first line that isn't a note. For each thing:
- Huragok's notes are put in as this version has them, its blank lines too.
- Each of the host's notes stays after the same line of Huragok's it followed (that line's new wording, if it
  changed; after the whole paragraph when theirs stood apart from it), or at the top or the bottom of the notes
  where it was: one right above a setting stays right above it.
- A line of the host's very like one of Huragok's (difflib's ratio REWORDED) is their rewording of it: it takes
  the place of the line of this version's most like it, which isn't brought up to date (the host's call, 26 Oct).
- Huragok's notes the host deleted come back (the host's call); those above a setting the host took out go with
  it. An example the host switched on (`start = …` for `# start = …`) isn't put back above it.
- A note at the end of a setting's line is brought up to date the same way, and so is the note beside each command
  in commands.toml, lined up as a new host's file has them. A host's own note there stays.
In a list, a line of Huragok's notes found below the first line that isn't a note (the host put a rule among
them) goes: the notes at the top are whole again.

Nothing that's read changes: a TOML file must read the same afterwards (tomllib), and in a list every line that
isn't a note is kept, word for word and in order; otherwise nothing is written (NotesError).
"""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

HISTORY = "templates/notes-history.json"
TEMPLATES = {"settings.toml": "templates/settings.toml", "discord.toml": "templates/discord.toml",
             "automod.txt": "templates/automod.txt",
             "names.txt": "templates/names.txt", "intervalmessages.txt": "templates/intervalmessages.txt",
             "intervalmessages-server.txt": "templates/intervalmessages-server.txt"}
COMMANDS = "commands.toml"  # written from the commands themselves (render_commands_toml), not from a template
KINDS = ("settings.toml", COMMANDS, "discord.toml", "automod.txt", "names.txt", "intervalmessages.txt",
         "intervalmessages-server.txt")
TOML_KINDS = ("settings.toml", COMMANDS, "discord.toml")
START, END = "start", "end"  # a list's notes; a TOML file's after its last setting
DISTINCT = 20  # a line of Huragok's this long is its own wherever it sits; a shorter one only above its own thing
REWORDED = 0.8  # a host's line at least this like one of Huragok's is their rewording of it
LIKE = 0.6  # ...and takes the place of this version's line most like it, if one is at least this like
SHORTEST = 12  # shorter lines are never taken for a rewording
SPACER = re.compile(r"#\s*")  # a bare `#`: layout between paragraphs
COMMENTED_HEADER = re.compile(r"^\s*#\s*\[")  # `# [[server]]`: what follows is an example table
ENTRY = re.compile(r"""^(?P<indent>\s*)(?P<entry>(?P<q>["'])(?P<name>[A-Za-z0-9_-]+)(?P=q)\s*,?)\s*(?:#(?P<note>.*))?$""")


class NotesError(ValueError):
    """A file whose notes couldn't be brought up to date: it can't be read, or the result would read differently."""


@dataclass
class Refreshed:
    """A file with this version's notes: its text, and how many of the host's notes and rewordings it kept."""

    text: str
    kept: int = 0
    reworded: int = 0


@dataclass
class _Unit:
    """A line of the file, or a setting whose value runs over several."""

    lines: list[str]
    kind: str  # "note", "blank" or "thing" (anything a reader reads)
    anchor: str | None = None  # a TOML thing's name: "updates|check_hours", "[updates]"; None: the host's own
    table: str = ""  # TOML: the table it's in
    key: bool = False  # a TOML setting's line (not a table's)

    @property
    def text(self) -> str:
        return self.lines[0].strip()


@dataclass
class _Tally:
    kept: int = 0
    reworded: int = 0


# What the files hold ------------------------------------------------------------------------------------------------

def _lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _units(kind: str, lines: list[str]) -> list[_Unit]:
    if kind in TOML_KINDS:
        return _toml_units(lines)
    return [_Unit([line], "blank" if not line.strip() else "note" if line.strip().startswith("#") else "thing")
            for line in lines]


def _toml_units(lines: list[str]) -> list[_Unit]:
    from ..core.tomledit import line_kinds

    kinds = line_kinds(lines)
    units: list[_Unit] = []
    table = ""
    number = 0
    while number < len(lines):
        entry, line = kinds[number], lines[number]
        if entry.kind in ("blank", "comment"):
            units.append(_Unit([line], "blank" if entry.kind == "blank" else "note", table=table))
        elif entry.kind == "header":
            if entry.parts is None:  # a header TOML can't read: nothing under it is one of Huragok's things
                table = f"?{number}"
                units.append(_Unit([line], "thing", table=table))
            else:
                table = ".".join(entry.parts)
                units.append(_Unit([line], "thing", f"[[{table}]]" if entry.array else f"[{table}]", table))
        elif entry.kind == "key":
            units.append(_Unit(lines[number:entry.end + 1], "thing", f"{table}|{'.'.join(entry.parts or ())}", table,
                               key=True))
            number = entry.end
        else:
            units.append(_Unit([line], "thing", table=table))
        number += 1
    return units


def _segments(units: list[_Unit], anchors: set[str] | None = None) -> list[tuple[list[_Unit], _Unit | None]]:
    """The notes above each thing that is one of `anchors` (None: every named thing): (what's above it, the thing),
    the last with None for what comes after the last one. Each name counts once, where it's first."""
    out: list[tuple[list[_Unit], _Unit | None]] = []
    seen: set[str] = set()
    above: list[_Unit] = []
    for unit in units:
        named = unit.kind == "thing" and unit.anchor is not None and unit.anchor not in seen
        if named and (anchors is None or unit.anchor in anchors):
            assert unit.anchor is not None
            seen.add(unit.anchor)
            out.append((above, unit))
            above = []
        else:
            above.append(unit)
    out.append((above, None))
    return out


def _anchor(unit: _Unit | None) -> str:
    return END if unit is None or unit.anchor is None else unit.anchor


def _inline(unit: _Unit) -> tuple[str, str]:
    """A one-line setting's line: (what TOML reads, its own comment)."""
    from ..core.tomledit import line_kinds, split_comment

    line = unit.lines[0]
    if len(unit.lines) != 1 or not unit.key:
        return line, ""
    return split_comment(line, line_kinds([line])[0].at)


def collect(kind: str, text: str) -> dict[str, dict[str, list[str]]]:
    """The notes a version writes in a file, as notes-history.json keeps them: "notes" (each thing's lines, as written
    above it), "inline" (a setting's own note) and, for commands.toml, "commands" (the note beside each command)."""
    units = _units(kind, _lines(text))
    found: dict[str, dict[str, list[str]]] = {"notes": {}, "inline": {}}
    if kind not in TOML_KINDS:
        top = [u.text for u in _list_top(units)[0] if u.kind == "note"]
        if top:
            found["notes"][START] = top
        return found
    for above, thing in _segments(units):
        lines = [u.text for u in above if u.kind == "note"]
        if lines:
            found["notes"][_anchor(thing)] = lines
        if thing is not None and (comment := _inline(thing)[1].strip()):
            found["inline"][_anchor(thing)] = [comment]
    if kind == COMMANDS:
        found["commands"] = {}
        for unit in units:
            for line in unit.lines:
                if (match := ENTRY.match(line)) and (note := (match.group("note") or "").strip()):
                    found["commands"].setdefault(match.group("name").lower(), [note])
    return found


def merged(history: dict[str, Any], kind: str, found: dict[str, dict[str, list[str]]]) -> bool:
    """`found` added to the history (which only ever grows); whether it gained a line."""
    grew = False
    mine = history.setdefault(kind, {})
    for part, things in found.items():
        known = mine.setdefault(part, {})
        for thing, lines in things.items():
            listed = known.setdefault(thing, [])
            for line in lines:
                if line not in listed:
                    listed.append(line)
                    grew = True
    return grew


def render(kind: str, text: str) -> str:
    """A template as `init` writes it, the host's values aside: settings.toml's {values} filled, {{ }} made single."""
    if kind != "settings.toml":
        return text
    text = text.replace("{{", "\x00").replace("}}", "\x01")
    text = re.sub(r"\{(\w+)\}", "''", text)
    return text.replace("\x00", "{").replace("\x01", "}")


def template_text(kind: str) -> str:
    """The file as this version's `init` writes it."""
    if kind == COMMANDS:
        from ..core.commandsfile import render_commands_toml

        return render_commands_toml()
    return render(kind, resources.files("huragok").joinpath(TEMPLATES[kind]).read_text("utf-8"))


@lru_cache(maxsize=1)
def _shipped_history() -> str:
    try:
        return resources.files("huragok").joinpath(HISTORY).read_text("utf-8")
    except (FileNotFoundError, OSError):
        return "{}"


def load_history() -> dict[str, Any]:
    data = json.loads(_shipped_history())
    return data.get("files", {}) if isinstance(data, dict) else {}


# Bringing a file's notes up to date ---------------------------------------------------------------------------------

def _like(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _nearest(tags: list[str], index: int, step: int) -> str | None:
    index += step
    while 0 <= index < len(tags):
        if tags[index] not in ("B", "#"):
            return tags[index]
        index += step
    return None


def _paragraph_end(gap: list[str], index: int) -> int:
    """The last line of the paragraph `index` is in (paragraphs end at a blank line or a bare #)."""
    if index < 0 or not gap[index].strip() or SPACER.fullmatch(gap[index].strip()):
        return index
    while index + 1 < len(gap) and gap[index + 1].strip() and not SPACER.fullmatch(gap[index + 1].strip()):
        index += 1
    return index


def _close_blanks(out: list[tuple[str, str]]) -> list[str]:
    """Blank lines where the host's lines meet Huragok's: as many as the side with more, not both added up."""
    lines: list[str] = []
    index = 0
    while index < len(out):
        if out[index][0].strip():
            lines.append(out[index][0])
            index += 1
            continue
        end = index
        while end < len(out) and not out[end][0].strip():
            end += 1
        sources = [source for _, source in out[index:end]]
        if len(set(sources)) > 1:
            lines.extend([""] * max(sources.count("G"), sources.count("H")))
        else:
            lines.extend(line for line, _ in out[index:end])
        index = end
    return lines


def _merge(above: list[_Unit], gap: list[str], ours: set[str], pool: list[str], drop: set[int],
           tally: _Tally) -> list[str]:
    """The lines above one thing: this version's notes (`gap`), with the host's lines from `above` where they were.
    `ours`: the lines that are Huragok's here; `pool`: the lines a host's might be a rewording of; `drop`: lines of
    `gap` left out (examples the host switched on)."""
    texts = [u.text for u in above]
    tags = ["D" if u.kind == "thing" else "B" if u.kind == "blank" else "#" if SPACER.fullmatch(u.text)
            else "O" if u.text in ours else "T" for u in above]
    gap_text = [line.strip() for line in gap]
    # A host's rewording of one of Huragok's lines takes the place of this version's line most like it.
    replaced: dict[int, str] = {}
    targets: dict[int, int] = {}
    for index, tag in enumerate(tags):
        if tag != "T" or len(texts[index]) < SHORTEST or not any(_like(texts[index], h) >= REWORDED for h in pool):
            continue
        best, score = None, LIKE
        for number, text in enumerate(gap_text):
            if number in replaced or number in drop or not text or SPACER.fullmatch(text):
                continue
            if (like := _like(texts[index], text)) >= score:
                best, score = number, like
        if best is not None:
            replaced[best] = above[index].lines[0]
            targets[index] = best
            tags[index] = "E"
            tally.reworded += 1
    # A bare # is Huragok's layout beside its own lines, the host's among theirs.
    for index, tag in enumerate(tags):
        if tag == "#":
            near = (_nearest(tags, index, -1), _nearest(tags, index, 1))
            tags[index] = "O" if near == (None, None) or any(t in ("O", "E") for t in near) else "T"
    # Where each of Huragok's lines in the file is now in this version's notes.
    mine = [index for index, tag in enumerate(tags) if tag == "O"]
    filled = [number for number, text in enumerate(gap_text) if text]
    where: dict[int, int] = dict(targets)
    matcher = difflib.SequenceMatcher(None, [texts[i] for i in mine], [gap_text[j] for j in filled], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "insert":
            continue
        last = -1
        for k in range(i1, i2):
            if op == "equal":
                last = filled[j1 + k - i1]
            elif op == "replace" and k == i2 - 1:
                last = filled[j2 - 1]  # the last line of a rewritten passage: the passage's end
            elif op == "replace":  # within a rewritten passage: after the paragraph of the line most like it
                options = filled[j1:j2]
                found = max(options, key=lambda j: (_like(texts[mine[k]], gap_text[j]), j))
                last = max(min(_paragraph_end(gap, found), filled[j2 - 1]), last)
            else:  # gone from this version: where the line before it is
                last = filled[j1 - 1] if j1 > 0 else -1
            where[mine[k]] = last
    # The host's lines, in runs, each with where it goes.
    first = next((i for i, tag in enumerate(tags) if tag in ("O", "E")), None)
    tops: list[list[str]] = []
    bottoms: list[list[str]] = []
    middles: dict[int, list[list[str]]] = {}
    at = -1
    index = 0
    while index < len(tags):
        if tags[index] in ("O", "E"):
            index += 1
            continue
        start = index
        while index < len(tags) and tags[index] not in ("O", "E"):
            index += 1
        if not any(tags[i] in ("T", "D") for i in range(start, index)):
            continue  # Huragok's own blank lines: this version's instead
        tally.kept += sum(1 for i in range(start, index) if tags[i] == "T")
        lines = [line for unit in above[start:index] for line in unit.lines]
        if first is None or index == len(tags):
            bottoms.append(lines)
        elif start < first:
            tops.append(lines)
        else:
            position = where.get(start - 1, -1)
            apart = tags[start] == "B" or tags[index - 1] == "B" or SPACER.fullmatch(texts[index])
            if apart:
                position = _paragraph_end(gap, position)
            at = max(at, position)
            if at < 0:
                tops.append(lines)
            else:
                middles.setdefault(at, []).append(lines)
    out: list[tuple[str, str]] = []
    if not filled:  # no notes of Huragok's here, only its blank lines: the host's lines, or those blank lines
        hosts = [*tops, *(lines for number in sorted(middles) for lines in middles[number]), *bottoms]
        if not hosts:
            return list(gap)
        return [line for lines in hosts for line in lines]
    filled_first, filled_last = filled[0], filled[-1]
    if not tops:
        out += [(line, "G") for line in gap[:filled_first]]
    for lines in tops:
        out += [(line, "H") for line in lines]
    for number in range(filled_first, filled_last + 1):
        if number not in drop:
            out.append((replaced.get(number, gap[number]), "H" if number in replaced else "G"))
        for lines in middles.get(number, []):
            out += [(line, "H") for line in lines]
    if not bottoms:
        out += [(line, "G") for line in gap[filled_last + 1:]]
    for lines in bottoms:
        out += [(line, "H") for line in lines]
    return _close_blanks(out)


def _list_top(units: list[_Unit]) -> tuple[list[_Unit], list[_Unit]]:
    """A list's notes at the top (to its first line that isn't a note), and the rest."""
    first = next((i for i, u in enumerate(units) if u.kind == "thing"), len(units))
    return units[:first], units[first:]


def _known(history: dict[str, Any], part: str) -> dict[str, list[str]]:
    found = history.get(part)
    return found if isinstance(found, dict) else {}


def _refresh_list(kind: str, units: list[_Unit], template: list[_Unit], history: dict[str, Any],
                  tally: _Tally) -> list[str]:
    known = _known(history, "notes")
    gap = [u.lines[0] for u in _list_top(template)[0]]
    ours = {*known.get(START, []), *(line.strip() for line in gap)}
    top, rest = _list_top(units)
    out = _merge(top, gap, ours, sorted(ours), set(), tally)
    # Huragok's lines below the first line that isn't a note (a rule put among them) go: the top has them all.
    distinct = {line for line in ours if len(line) >= DISTINCT}
    gone = [u.kind == "note" and u.text in distinct for u in rest]
    for index, unit in enumerate(rest):
        if unit.kind == "note" and SPACER.fullmatch(unit.text):
            near = [i for i in (index - 1, index + 1) if 0 <= i < len(rest) and rest[i].kind != "blank"]
            gone[index] = bool(near) and all(gone[i] for i in near)
    if not any(gone):
        return out + [u.lines[0] for u in rest]
    return out + _close_runs(rest, gone)


def _close_runs(rest: list[_Unit], gone: list[bool]) -> list[str]:
    """The lines left once Huragok's are out, the blank lines either side of what went made one run, as long as the
    longer of the two."""
    out: list[str] = []
    index = 0
    while index < len(rest):
        if gone[index]:
            index += 1
            continue
        if rest[index].kind != "blank":
            out.append(rest[index].lines[0])
            index += 1
            continue
        runs: list[int] = []
        count = 0
        while index < len(rest) and (gone[index] or rest[index].kind == "blank"):
            if gone[index]:
                if count:
                    runs.append(count)
                count = 0
            else:
                count += 1
            index += 1
        runs.append(count)
        out.extend([""] * max(runs))
    return out


def _refresh_toml(kind: str, units: list[_Unit], template: list[_Unit], history: dict[str, Any],
                  tally: _Tally) -> list[str]:
    from ..core.tomledit import COMMENTED_KEY

    known = _known(history, "notes")
    things = {u.anchor for u in template if u.kind == "thing" and u.anchor is not None}
    real = {u.anchor for u in units if u.kind == "thing" and u.anchor is not None}
    every = {line for lines in known.values() for line in lines} | {u.text for u in template if u.kind == "note"}
    distinct = {line for line in every if len(line) >= DISTINCT}
    # A setting the host made a comment (`# check_hours = 6`) keeps its notes above it.
    for unit in units:
        match = COMMENTED_KEY.match(unit.lines[0]) if unit.kind == "note" else None
        anchor = f"{unit.table}|{match.group(1)}" if match else None
        if anchor in things and anchor not in real and unit.text not in every:
            unit.kind, unit.anchor, unit.key = "thing", anchor, False
            real.add(anchor)
    gaps = {_anchor(thing): above for above, thing in _segments(template)}
    out: list[str] = []
    for above, thing in _segments(units, things):
        name = _anchor(thing)
        gap_units = gaps.get(name, [])
        gap = [u.lines[0] for u in gap_units]
        mine = {*known.get(name, []), *(line.strip() for line in gap)}
        ours = mine | {u.text for u in above if u.kind == "note" and u.text in distinct}
        out += _merge(above, gap, ours, sorted(mine), _switched_on(gap_units, real, things), tally)
        if thing is not None:
            out += _thing_lines(kind, thing, template_thing=_template_thing(template, name), history=history,
                                tally=tally)
    return out


def _switched_on(gap: list[_Unit], real: set[str], things: set[str]) -> set[int]:
    """Examples in this version's notes (`# start = "systemctl …"`) for a setting the host has switched on (and that a
    new host's file doesn't set itself): not put back above it."""
    from ..core.tomledit import COMMENTED_KEY

    drop: set[int] = set()
    example_table = False
    for number, unit in enumerate(gap):
        if COMMENTED_HEADER.match(unit.lines[0]):
            example_table = True  # `# [[server]]` and what follows: an example of a whole table, always kept
            continue
        match = COMMENTED_KEY.match(unit.lines[0])
        anchor = f"{unit.table}|{match.group(1)}" if match else None
        if anchor and not example_table and anchor in real and anchor not in things:
            drop.add(number)
    return drop


def _template_thing(template: list[_Unit], name: str) -> _Unit | None:
    return next((u for u in template if u.kind == "thing" and u.anchor == name), None)


def _thing_lines(kind: str, thing: _Unit, *, template_thing: _Unit | None, history: dict[str, Any],
                 tally: _Tally) -> list[str]:
    """A setting's own lines, its note at the end brought up to date; in commands.toml, the note beside each command."""
    lines = list(thing.lines)
    if template_thing is not None and thing.key and len(lines) == 1:
        lines[0] = _inline_note(thing, template_thing, _known(history, "inline").get(thing.anchor or "", []), tally)
    if kind == COMMANDS and thing.key:
        lines = [_command_note(line, _known(history, "commands"), tally) for line in lines]
    return lines


def _inline_note(thing: _Unit, template_thing: _Unit, known: list[str], tally: _Tally) -> str:
    line = thing.lines[0]
    code, comment = _inline(thing)
    template_comment = _inline(template_thing)[1]
    theirs, wanted = comment.strip(), template_comment.strip()
    if theirs == wanted:
        return line
    if theirs and theirs not in known:
        if any(_like(theirs, note) >= REWORDED for note in [*known, wanted]):
            tally.reworded += 1
        return line  # the host's own note, or their rewording of Huragok's
    if not wanted:
        return code
    column = len(template_thing.lines[0]) - len(template_comment)
    return (code.ljust(column) if len(code) < column else code + "  ") + wanted


@lru_cache(maxsize=1)
def _command_help() -> tuple[dict[str, str], int]:
    from ..core.commandsfile import all_commands

    specs = all_commands()
    return {name.lower(): spec.help for name, spec in specs.items()}, max((len(n) for n in specs), default=0) + 3


def _command_note(line: str, known: dict[str, list[str]], tally: _Tally) -> str:
    match = ENTRY.match(line)
    if not match:
        return line
    helps, width = _command_help()
    name = match.group("name").lower()
    wanted = helps.get(name)
    if not wanted:
        return line  # not a command this version has (a player ID, a command gone since): left as it is
    theirs = (match.group("note") or "").strip()
    if theirs and theirs != wanted and theirs not in known.get(name, []):
        if any(_like(theirs, note) >= REWORDED for note in [*known.get(name, []), wanted]):
            tally.reworded += 1
        return line
    entry = match.group("entry").replace(" ", "")
    return f"{match.group('indent')}{entry:<{width}} # {wanted}"


def refresh(kind: str, text: str, *, template: str | None = None,
            history: dict[str, Any] | None = None) -> Refreshed:
    """`text` (a file of `kind`, one of KINDS) with this version's notes, the host's kept where they were."""
    history = (load_history() if history is None else history).get(kind, {})
    template_units = _units(kind, _lines(template_text(kind) if template is None else template))
    units = _units(kind, _lines(text))
    tally = _Tally()
    if kind in TOML_KINDS:
        out = _refresh_toml(kind, units, template_units, history, tally)
    else:
        out = _refresh_list(kind, units, template_units, history, tally)
    return Refreshed("\n".join(out) + "\n" if out else "", tally.kept, tally.reworded)


def _data(kind: str, text: str) -> Any:
    """What a reader reads in the file: a TOML file's values; a list's lines that aren't notes, in order."""
    if kind in TOML_KINDS:
        return tomllib.loads(text)
    return [line for line in _lines(text) if line.strip() and not line.strip().startswith("#")]


def read(path: Path, kind: str) -> str:
    """The file's text as its reader takes it. Raises NotesError (it can't be read) or OSError."""
    from ..core.files import read_text

    if kind in TOML_KINDS:
        try:
            text = path.read_text(encoding="utf-8")
            tomllib.loads(text)
        except UnicodeDecodeError:
            raise NotesError("it isn't UTF-8 text") from None
        except tomllib.TOMLDecodeError as error:
            raise NotesError(f"it isn't valid TOML ({error})") from None
        return text
    return read_text(path, NotesError)


def refresh_text(kind: str, text: str, **given: Any) -> Refreshed:
    """refresh(), checked: a result that would read differently (a fault in this module) raises NotesError."""
    result = refresh(kind, text, **given)
    if _data(kind, result.text) != _data(kind, text):
        raise NotesError("its settings would read differently afterwards, so it was left as it is")
    return result


@dataclass
class Target:
    """A file whose notes an update brings up to date."""

    path: Path
    kind: str  # one of KINDS
    label: str  # how the log names it: "settings.toml", "Slayer/intervalmessages.txt"
    found: list[str] = field(default_factory=list)


def describe(label: str, result: Refreshed) -> str:
    """The log's line for a file brought up to date."""
    extras = []
    if result.kept:
        extras.append(f"your {result.kept} own note{'s' if result.kept != 1 else ''} kept where you put "
                      f"{'them' if result.kept != 1 else 'it'}")
    if result.reworded:
        extras.append(f"{result.reworded} of its notes you reworded kept as you wrote "
                      f"{'them' if result.reworded != 1 else 'it'}")
    return f"{label}: Huragok's notes as this version writes them" + (f" ({'; '.join(extras)})" if extras else "")


__all__ = ["KINDS", "NotesError", "Refreshed", "Target", "collect", "describe", "load_history", "merged", "read",
           "refresh", "refresh_text", "render", "template_text"]
