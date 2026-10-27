"""Vergil's word list: one text file, one rule per line, in five parts.

    # words                 | mutes            | kicks        | ban | name
    badword, otherword      | 1d, 7d           | 2, 1h, 1d    | 5   | slurs
    worstword               |                  |              | 0   | threats
    rudeword                | 1d, 3d, 7d       |              |     | rude
    meanword                | 1d, 3d, 7d, 8    | 5            |     | mean
    kickword

Each strike moves a player one step down the line's ladder: mutes first, then
kicks, then the ban. Mute times listed past the start of the kicks overlap them:
those strikes remove the player, then mute them once they're back.

words    separated by commas. `word*` also catches it at the start of longer
         words, `*word*` anywhere inside one. `-word` is never caught by that line.
mutes    how long each mute lasts, in order from the first strike; the last
         repeats until the kicks, and `perm` (last only) is muted for good. A
         number last, with no s, m, h, d or w, is how many strikes come before
         muted for good, as the ban counts them: the last time repeats until
         then, and every strike after it mutes for good, kicks or not. Empty:
         never muted. A muted player stays in the game; nobody hears their voice
         or reads their chat.
kicks    how many strikes come before the kicks (left out: once every mute time
         is used; 0: kicked from the first strike), then how long each kick keeps
         the player out, in order (30m, 1h, 1d, 2w); the last repeats until the
         ban. No times: the server's own kick (out 10, 30, 60, then 120 minutes).
         Empty: never kicked, or with no mutes either, the server's kick every
         time. A kick with a time is a timed ban underneath, since the server's
         kick times are fixed.
ban      how many strikes come before the ban for good (0: banned the first
         time). Empty: never banned.
name     what players read, and what moderators type in !strike. Optional.

So "badword, otherword | 1d, 7d | 2, 1h, 1d | 5 | slurs" is 1st: muted 1 day,
2nd: muted 7 days, 3rd: out 1 hour, 4th and 5th: out 1 day, 6th: banned.
"w | 1d, 3d, 7d | 1, 1h | 4" is 1st: muted 1 day, 2nd: out 1 hour then muted
3 days, 3rd: out 1 hour then muted 7 days, 4th: out 1 hour, 5th: banned.
"meanword | 1d, 3d, 7d, 8 | 5" is 1st: muted 1 day, 2nd: muted 3 days, 3rd to
5th: muted 7 days, 6th to 8th: kicked, 9th and after: kicked, then muted for
good, never banned. "kickword" alone is a plain kick every time. There's no
limit on the counts or on how many times a line lists, only that every time
comes before the ban.

Up to 0.5.0 the kicks came first: "words | kicks | mutes | ban | name", the mutes
starting with how many strikes came before them. A word list whose notes say that
layout (and none says this one) is read by turning each line into this layout,
as from_kicks_first() does; a step of updates/hostfiles.py rewrites the file so.
The 0.2 layout, "words | timers | chances | name" (no mutes), is still read the
same way it always was: a line in four parts whose last part is a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...core.files import read_text
from ...core.text import TIME, describe_time, seconds_of

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
FOREVER = "perm"
NAME_STRIKE = "name"  # `!strike <player> name` bans their name, so no line may be called that
PARTS = "words | mutes | kicks | ban | name"
KICKS_FIRST_PARTS = "words | kicks | mutes | ban | name"  # the layout up to 0.5.0
HEADER = f"#   {PARTS}"  # the note that says a word list's layout, as the template writes it
# A note naming the parts: which layout the list is in. The 0.2 layout's says its kicks came first too.
_PARTS_NOTE = re.compile(r"#\s*words\s*\|(.*)")
_KICKS_FIRST_NOTES = ("kicks|mutes|ban|name", "timers|chances|name")
_THIS_LAYOUT_NOTE = "mutes|kicks|ban|name"


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    """What one strike does: a kick (for `duration`, or the server's plain kick), a mute (for `duration`, or for
    good; after a timed `kick`, or the server's plain kick when `plain_kick`, if one comes with it), or a ban for
    good."""

    action: str  # kick | mute | ban
    duration: str | None = None
    kick: str | None = None  # a mute's: removed for this long first, the mute starting once they're back
    plain_kick: bool = False  # a mute's: the server's own kick first, the mute kept for when they're back

    @property
    def seconds(self) -> int | None:
        return None if self.duration is None else seconds_of(self.duration)

    @property
    def kick_seconds(self) -> int:
        return 0 if self.kick is None else seconds_of(self.kick)

    @property
    def removes(self) -> bool:
        """Whether it takes them off the server: a kick, a mute with a kick first, the ban."""
        return self.action != "mute" or self.kick is not None or self.plain_kick

    @property
    def severity(self) -> tuple[int, float, float]:
        """Heavier steps sort later: a mute, then a kick (a mute after it or not), then the ban, each longer beating
        shorter."""
        if self.action == "ban":
            return (3, 0, 0)
        muted = 0.0 if self.action != "mute" else float("inf") if self.duration is None else float(self.seconds or 0)
        if self.removes:
            return (2, self.kick_seconds if self.action == "mute" else self.seconds or 0, muted)
        return (1, muted, 0)

    def describe(self) -> str:
        if self.action == "ban":
            return "banned for good"
        if self.action == "mute":
            muted = f"muted for {describe_time(self.duration)}" if self.duration else "muted for good"
            if self.kick:
                return f"removed for {describe_time(self.kick)}, then {muted}"
            return f"kicked, then {muted}" if self.plain_kick else muted
        return f"removed for {describe_time(self.duration)}" if self.duration else "kicked"

    def __str__(self) -> str:
        if self.action == "mute":
            first = f"kick {self.kick} + " if self.kick else "kick + " if self.plain_kick else ""
            return first + f"mute {self.duration or FOREVER}"
        return self.action + (f" {self.duration}" if self.duration else "")


@dataclass
class Rule:
    name: str
    label: str  # what players read; empty when the line has no name
    words: list[str]
    allow: list[str] = field(default_factory=list)
    mutes: list[str] = field(default_factory=list)  # each mute's time from the first strike; "perm" last for good
    mute_forever_after: int | None = None  # strikes before muted for good (the mutes' last number); None: never
    kick_after: int | None = 0  # strikes before the kicks start; None: never kicked (a line with mutes only)
    kicks: list[str] = field(default_factory=list)  # each kick's time; none: the server's plain kick
    ban_after: int | None = None  # strikes before the ban; None: never banned
    line: int = 0
    old_layout: bool = False  # written as "words | timers | chances | name"

    def step_for(self, previous: int) -> Step:
        """The step for a player's next strike, given how many still count."""
        if self.ban_after is not None and previous >= self.ban_after:
            return Step("ban")
        kicking = self.kick_after is not None and previous >= self.kick_after
        kick = None
        if kicking and self.kicks:
            kick = _timed(self.kicks[min(previous - (self.kick_after or 0), len(self.kicks) - 1)])
        mute = None  # this strike's mute: a time, or perm
        if self.mute_forever_after is not None and previous >= self.mute_forever_after:
            mute = FOREVER
        elif previous < len(self.mutes):
            mute = self.mutes[previous]  # a mute time listed for this strike mutes, the kicks begun or not
        elif self.mutes and not kicking:
            mute = self.mutes[-1]  # the last repeats until the kicks
        if mute is not None and _timed(mute) is not None:
            duration = None if mute == FOREVER else mute
            return Step("mute", duration, kick, plain_kick=kick is None) if kicking else Step("mute", duration)
        if kicking:
            return Step("kick", kick)
        # A mute of 0m mutes nobody (and the server may read 0 as no end): that strike kicks, as the line does
        # without mutes. `check` names the line (zero_times).
        return Step("kick", _timed(self.kicks[min(previous, len(self.kicks) - 1)]) if self.kicks else None)

    def zero_times(self) -> list[str]:
        """The line's times of 0 (0m, 0h), which every version reads but which do nothing a host means: a kick of 0m
        is the server's plain kick, a mute of 0m a kick."""
        return [time for time in (*self.mutes, *self.kicks) if time != FOREVER and seconds_of(time) == 0]

    def changes(self) -> list[int]:
        """The strikes (counted from 0) where what a strike does may change: past the last, it never does."""
        points = set(range(len(self.mutes) + 1))
        if self.kick_after is not None:
            points.update(range(self.kick_after, self.kick_after + len(self.kicks) + 1))
        if self.mute_forever_after is not None:
            points.add(self.mute_forever_after)
        if self.ban_after is not None:
            points.add(self.ban_after)
        return sorted(points)

    def strike_text(self, number: int) -> str:
        """"strike 2 of 3": toward the ban, or toward muted for good on a line that never bans."""
        last = self.ban_after if self.ban_after is not None else self.mute_forever_after
        if last and number <= last:
            return f"strike {number} of {last}"
        return f"strike {number}"


def _many(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _has_text(word: str) -> bool:
    """Whether a listed word has a letter or digit left once matching has taken out what it ignores."""
    from .matcher import normalise  # here: the matcher imports this module

    return any(c.isalnum() for c in normalise(word.strip("*")))


def _codes(text: str) -> str:
    return " ".join(f"U+{ord(c):04X}" for c in text[:6])


def is_count(text: str) -> bool:
    """0, 1, 2…: digits alone (not ² or ½, which isdigit() lets through and int() can't read)."""
    return text.isascii() and text.isdecimal()


def _count(text: str, where: str, what: str) -> int:
    if not is_count(text):
        raise RuleError(f"{where}: {what} must be a number, 0 or more")
    return int(text)


def _timed(time: str | None) -> str | None:
    """A kick's time, or None for a time of 0 (0m): the server's plain kick, never a ban sent for "0m", which the
    server may not read as no time at all. Every version has read 0 as a time, so a word list with one still loads."""
    return None if time is None or (time != FOREVER and seconds_of(time) == 0) else time


def _times(text: str, where: str) -> list[str]:
    times = [t.strip().lower() for t in text.split(",") if t.strip()]
    for time in times:
        if time != FOREVER and not TIME.fullmatch(time):
            raise RuleError(f"{where}: '{time}' isn't a time; use a number and s, m, h, d or w, like 30m or 7d")
    return times


def _entries(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _is_old_layout(fields: list[str]) -> bool:
    """The 0.2 layout: four parts, the last a name (in five parts the fourth is a number)."""
    return len(fields) == 4 and bool(fields[3]) and not is_count(fields[3])


def parse_line(text: str, number: int) -> Rule | None:
    """One line of the word list -> a Rule, or None for blank lines and # notes."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    where = f"line {number}"
    fields = [f.strip() for f in text.split("|")]
    if len(fields) > 5:
        raise RuleError(f"{where}: at most 5 parts: {PARTS}")
    old = _is_old_layout(fields)
    if old:  # its timers are kicks, and it has no mutes: it reads the same whichever comes first
        fields = [fields[0], "", fields[1], fields[2], fields[3]]
    fields += [""] * (5 - len(fields))
    entries = [w.strip() for w in fields[0].split(",") if w.strip()]
    words = [w for w in entries if not w.startswith("-")]
    allow = [w[1:].strip() for w in entries if w.startswith("-") and _has_text(w[1:])]
    if not words or any(not w.strip("*").strip() for w in words):
        raise RuleError(f"{where}: no words before the first |")
    if blank := next((w for w in words if not _has_text(w)), None):
        # A zero-width space or a lone accent pasted from the web: matched as nothing, it would catch
        # almost every line of chat.
        raise RuleError(f"{where}: '{blank}' ({_codes(blank)}) has no letters or digits once invisible "
                        "characters and accents are taken out, so it would catch almost every line; delete it")

    entries = _entries(fields[1])
    forever_after = int(entries.pop()) if entries and is_count(entries[-1]) else None  # the strikes before for good
    if count := next((t for t in entries if is_count(t)), None):
        if entries[0] == count:  # the layout up to 0.5.0, whose mutes started with the strikes before them
            raise RuleError(f"{where}: '{count}' in mutes has no s, m, h, d or w: the mutes come first now, from the "
                            f"first strike, so how many strikes come before the kicks goes first in the kicks: "
                            f"{PARTS}")
        raise RuleError(f"{where}: '{count}' in mutes has no s, m, h, d or w: a number alone, the strikes before "
                        f"muted for good, can only be last, like 1d, 3d, 7d, 8")
    mutes = _times(", ".join(entries), where)
    if not mutes and forever_after is not None:
        raise RuleError(f"{where}: before the {forever_after} in mutes, list how long each mute lasts, like 1d, 3d, "
                        f"7d, {forever_after}")
    if FOREVER in mutes[:-1]:
        raise RuleError(f"{where}: perm can only be the last mute; nothing comes after for good")
    if forever_after is not None:
        if FOREVER in mutes:
            raise RuleError(f"{where}: perm and the {forever_after} after it both say when the mutes are for good; "
                            f"keep one")
        if len(mutes) > forever_after:
            raise RuleError(f"{where}: {_many(len(mutes), 'mute time')} but only {_many(forever_after, 'mute')} "
                            f"before muted for good; one time per mute at most (the last number counts every strike, "
                            f"as the ban does: {len(mutes)} or more here)")

    entries = _entries(fields[2])
    kick_after = int(entries.pop(0)) if entries and is_count(entries[0]) else None  # the strikes before the kicks
    if count := next((t for t in entries if is_count(t)), None):
        raise RuleError(f"{where}: '{count}' in kicks has no s, m, h, d or w: a number alone, the strikes before the "
                        f"kicks, can only be first, like 3, 1h, 1d")
    kicks = _times(", ".join(entries), where)
    if FOREVER in kicks:
        raise RuleError(f"{where}: a kick can't be for good; that's the ban, in the 4th part")
    if not mutes:
        if kick_after:
            raise RuleError(f"{where}: the kicks start after {_many(kick_after, 'strike')}, but no mutes come before "
                            f"them: list the mutes first ({PARTS}), or take out the {kick_after}")
        kick_after = 0  # no mutes: every strike kicks
    elif kick_after is None and fields[2]:
        kick_after = len(mutes)  # once every mute time is used

    ban_after = None
    if fields[3]:
        ban_after = _count(fields[3], where, "chances" if old else "the strikes before the ban")
        if len(mutes) > ban_after:
            raise RuleError(f"{where}: {_many(len(mutes), 'mute time')} but only {_many(ban_after, 'mute')} before "
                            f"the ban; one time per mute at most")
        if forever_after is not None and forever_after >= ban_after:
            raise RuleError(f"{where}: the mutes are for good after {forever_after} strikes but the ban comes after "
                            f"{ban_after}, so they never would be")
        if mutes and kick_after is not None and kick_after >= ban_after:
            raise RuleError(f"{where}: the kicks start after {kick_after} strikes but the ban comes after "
                            f"{ban_after}, so they never would")
        # Mute times may run past the start of the kicks (those strikes kick, then mute), but not past the ban.
        if kick_after is not None and kick_after + len(kicks) > ban_after:
            if old:
                raise RuleError(f"{where}: {len(kicks)} timers but {ban_after} chances; one timer per chance at most")
            raise RuleError(f"{where}: {_many(len(kicks), 'kick time')} but only "
                            f"{_many(ban_after - kick_after, 'kick')} before the ban; one time per kick at most")

    name = fields[4].lower()
    if name and not NAME.fullmatch(name):
        raise RuleError(f"{where}: the name '{fields[4]}' should be one short word: letters, digits, - or _")
    if name == NAME_STRIKE:
        raise RuleError(f"{where}: a line can't be called '{NAME_STRIKE}': !strike <player> {NAME_STRIKE} "
                        f"bans a player's name")
    return Rule(name=name, label=name, words=words, allow=allow, mutes=mutes, mute_forever_after=forever_after,
                kick_after=kick_after, kicks=kicks, ban_after=ban_after, line=number, old_layout=old)


# The layout up to 0.5.0: the kicks first ------------------------------------------------------------------------

def _parts_notes(text: str) -> list[bool]:
    """Each note of a word list that names its parts, in order: True for one of the layout up to 0.5.0, False for
    this version's."""
    found = []
    for line in lines_of(text):
        if match := _PARTS_NOTE.fullmatch(line.strip()):
            parts = "".join(match[1].lower().split())
            if parts == _THIS_LAYOUT_NOTE or parts in _KICKS_FIRST_NOTES:
                found.append(parts != _THIS_LAYOUT_NOTE)
    return found


def kicks_first(text: str) -> bool:
    """Whether a word list is in the layout up to 0.5.0, the kicks before the mutes: a note of it names the parts
    that way (`#   words | kicks | mutes | ban | name`, or the 0.2 layout's), and none names them this version's way.
    A list with no such note is in this version's layout: every list since 0.3.0 was written with one, an update
    puts back one the host took out, and one from before this version that had none is given its own
    (updates/hostfiles.py) before it's read."""
    found = _parts_notes(text)
    return bool(found) and all(found)


def says_layout(text: str) -> bool:
    """Whether a note of a word list names its parts, either way."""
    return bool(_parts_notes(text))


def from_kicks_first(text: str) -> str:
    """A line of the layout up to 0.5.0 (words | kicks | mutes | ban | name) in this one's (words | mutes | kicks |
    ban | name), the host's call (27 October): the mute times come first, the strikes before muted for good still
    last among them; the strikes that came before the mutes now come before the kicks, then the kick times. A line
    that only muted (0, and no kick times) still only mutes, and one that only kicked still only kicks; one in the 0.2
    layout, or with neither, reads the same either way and is left as it is. Kick times that would now come after the
    ban are left out: no strike reaches them. A line that didn't read is turned the same way, part by part, and keeps
    its place in the count (an unnamed line below it keeps its rule<n>, and its strikes)."""
    return _turned(text)[0]


def _turned(text: str) -> tuple[str, list[str]]:
    """from_kicks_first(), and the kick times it left out."""
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return text, []
    fields = [f.strip() for f in stripped.split("|")]
    if len(fields) < 2 or len(fields) > 5 or _is_old_layout(fields):
        return text, []
    fields += [""] * (5 - len(fields))
    if not fields[1] and not fields[2]:
        return text, []  # neither kicks nor mutes: it reads the same either way
    kicks, mutes = _entries(fields[1]), _entries(fields[2])
    before = mutes.pop(0) if mutes and is_count(mutes[0]) else None  # the strikes before the mutes
    if not mutes and before is not None:
        mutes, before = [before], None  # a count with no times: still a line that doesn't read
    cut: list[str] = []
    if before is not None and (kicks or before != "0"):
        if is_count(fields[3]) and int(before) < int(fields[3]):
            reached = int(fields[3]) - int(before)  # past the ban: never reached now
            kicks, cut = kicks[:reached], kicks[reached:]
        kicks = [before, *kicks]
    fields[1], fields[2] = ", ".join(mutes), ", ".join(kicks)
    while len(fields) > 1 and not fields[-1]:
        fields.pop()
    if fields[-1] and len(fields) == 4 and not is_count(fields[3]):
        fields.append("")  # four parts ending in a name read as the 0.2 layout
    return fields[0] + "".join(f" | {part}" if part else " |" for part in fields[1:]), cut


def _this_layout(text: str) -> list[str]:
    """A word list's lines in this version's layout."""
    lines = lines_of(text)
    return [from_kicks_first(line) for line in lines] if kicks_first(text) else lines


@dataclass
class Turned:
    """A word list in this version's layout, as the update writes it."""

    text: str
    turned: list[int] = field(default_factory=list)  # the lines whose parts changed places
    cut: dict[int, list[str]] = field(default_factory=dict)  # kick times left out, by line: after the ban now


def in_this_layout(text: str) -> Turned:
    """A word list of the layout up to 0.5.0 in this version's, line for line (from_kicks_first()), the notes naming
    its parts the old way naming them this version's way. It reads as the list did: parse_lenient() reads one of the
    old layout so. A list in this layout already comes back as it is."""
    if not kicks_first(text):
        return Turned(text)
    result = Turned("")
    out: list[str] = []
    for number, line in enumerate(lines_of(text), 1):
        new, cut = _turned(line)
        if match := _PARTS_NOTE.fullmatch(line.strip()):
            if "".join(match[1].lower().split()) in _KICKS_FIRST_NOTES:
                new = HEADER
        elif new != line:
            result.turned.append(number)
        if cut:
            result.cut[number] = cut
        out.append(new)
    result.text = "\n".join(out) + "\n"
    return result


def lines_of(text: str) -> list[str]:
    """A word list's or names.txt's lines, as change_rule() and remove_name() count them: split at line ends only.
    str.splitlines() also splits at U+2028, U+0085, form feeds and the like, which a pasted name or note can hold: a
    note's tail would become a live rule, and an edit would land on the wrong line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_text(text: str) -> list[Rule]:
    rules = [r for r in (parse_line(line, n) for n, line in enumerate(_this_layout(text), 1)) if r]
    for number, rule in enumerate(rules, 1):
        if not rule.name:
            rule.name = f"rule{number}"  # unnamed: !strike still needs something to type
    names = [r.name for r in rules]
    if duplicate := next((n for n in names if names.count(n) > 1), None):
        raise RuleError(f"two lines are named {duplicate}")
    return rules


@dataclass(frozen=True)
class LeftOut:
    """A line Vergil runs without: it doesn't read, or it takes a name a line above it has."""

    line: int
    text: str
    why: str


def parse_lenient(text: str) -> tuple[list[Rule], list[LeftOut]]:
    """The lines that read, and those left out with why. One mistake used to stop the whole list, and with it all of
    Vergil until Huragok restarted, even once the file was fixed (the simulation's finding 2, 26 October: a host's
    new line named like one already there). A left-out line keeps its place in the count, so an unnamed line below it
    keeps its rule<n> and its strikes; of two lines named alike, the first keeps the name."""
    lines = lines_of(text)
    rules: list[Rule] = []
    left: list[LeftOut] = []
    place = 0
    for number, line in enumerate(_this_layout(text), 1):
        try:
            rule = parse_line(line, number)
        except RuleError as error:
            place += 1
            left.append(LeftOut(number, lines[number - 1], str(error)))
            continue
        if rule is None:
            continue
        place += 1
        if not rule.name:
            rule.name = f"rule{place}"
        rules.append(rule)
    named: dict[str, int] = {}
    kept: list[Rule] = []
    for rule in rules:
        if rule.name in named:
            left.append(LeftOut(rule.line, lines[rule.line - 1],
                                f"line {rule.line}: line {named[rule.name]} is named {rule.name} already (one name a "
                                "line: give this one another)"))
            continue
        named[rule.name] = rule.line
        kept.append(rule)
    return kept, sorted(left, key=lambda item: item.line)


def read_rules_lenient(path: Path) -> tuple[list[Rule], list[LeftOut]]:
    try:
        return parse_lenient(read_text(path, RuleError))
    except FileNotFoundError:
        raise RuleError(f"{path} not found: starting Huragok writes one, or set words in [automod]") from None


def unnamed_ladders(rules: list[Rule]) -> list[Rule]:
    """Lines with no name whose strikes change what happens next. Their strikes are kept under the name the
    line's place gives it (rule2), so a line added above one moves everyone's strikes to another line."""
    return [r for r in rules if not r.label and len({r.step_for(point) for point in r.changes()}) > 1]


def describe_unnamed(rule: Rule, file: str) -> str:
    return (f"{file} line {rule.line} has no name, so its strikes are kept under {rule.name}, and they move to "
            f"another line if a line is added above it: name it (the 5th part, {PARTS})")


def read_rules(path: Path) -> list[Rule]:
    try:
        return parse_text(read_text(path, RuleError))
    except FileNotFoundError:
        raise RuleError(f"{path} not found: starting Huragok writes one, or set words in [automod]") from None
    except RuleError as error:
        text = str(error)
        raise RuleError(text if text.startswith(path.name) else f"{path.name} {text}") from None


# Banned names --------------------------------------------------------------------------

@dataclass(frozen=True)
class BannedName:
    text: str  # as written: a player's name, or with a * at either end
    line: int


def read_names(path: Path) -> list[BannedName]:
    """names.txt: one banned name per line; # starts a note. No file: no banned names."""
    try:
        text = read_text(path, RuleError)
    except FileNotFoundError:
        return []
    return [BannedName(line.strip(), number) for number, line in enumerate(lines_of(text), 1)
            if line.strip() and not line.strip().startswith("#")]


# Changing the files from the window (D), and from the game -------------------------------------

def ordinal(number: int) -> str:
    """1st, 2nd, 3rd, 4th… 11th, 12th, 13th, 21st."""
    if number % 100 in (11, 12, 13):
        return f"{number}th"
    return f"{number}" + {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def ladder(rule: Rule) -> list[str]:
    """What each strike does, in order, until it stops changing, strikes in a row that do the same as one:
    ["1st: muted for 1 day", "2nd to 9th: muted for 7 days", "10th and after: removed for 1 day"]."""
    runs: list[tuple[int, Step]] = []  # (its first strike, counted from 0; what that strike and the run's others do)
    for point in rule.changes():
        step = rule.step_for(point)
        if not runs or runs[-1][1] != step:
            runs.append((point, step))
    out: list[str] = []
    for (first, step), following in zip(runs, [*(start for start, _ in runs[1:]), None]):
        if following is None:  # the same every time from here on
            if step.action == "ban":  # banned for good: no strike comes after it
                out.append(f"{ordinal(first + 1)}: {step.describe()}")
            else:
                out.append(f"every time: {step.describe()}" if first == 0 else
                           f"{ordinal(first + 1)} and after: {step.describe()}")
        elif following - first == 1:
            out.append(f"{ordinal(first + 1)}: {step.describe()}")
        else:
            joined = " and " if following - first == 2 else " to "
            out.append(f"{ordinal(first + 1)}{joined}{ordinal(following)}: {step.describe()}")
    return out


def least_ban(mutes: list[str], forever_after: int | None, kick_after: int | None, kicks: list[str]) -> int:
    """The fewest strikes before the ban that leave room for every mute and kick time, for muted for good, and for
    the kicks to start."""
    kicked = 0 if kick_after is None else kick_after + len(kicks) + (1 if mutes and not kicks else 0)
    return max(len(mutes), kicked, 0 if forever_after is None else forever_after + 1)


def least_forever(mutes: list[str]) -> int:
    """The fewest strikes before muted for good that leave room for every mute time."""
    return len(mutes)


def rule_line(words: list[str], mutes: list[str], forever_after: int | None, kick_after: int | None,
              kicks: list[str], ban_after: int | None, name: str) -> str:
    """A rule as its line in the word list. With a name it's written in all five parts (four parts ending in a
    name read as the 0.2 layout); without one, the empty parts at the end are left off. `forever_after` goes last
    in the mutes (the strikes before muted for good); `kick_after` first in the kicks, on a line with mutes (one
    without starts kicking at once)."""
    forever = [] if forever_after is None else [str(forever_after)]
    start = [] if not mutes or kick_after is None else [str(kick_after)]
    parts = [", ".join(words), ", ".join([*mutes, *forever]),
             "" if mutes and kick_after is None else ", ".join([*start, *kicks]),
             "" if ban_after is None else str(ban_after), name]
    while not name and len(parts) > 1 and not parts[-1]:
        parts.pop()
    return parts[0] + "".join(f" | {part}" if part else " |" for part in parts[1:])


def line_of(rule: Rule) -> str:
    return rule_line([*rule.words, *(f"-{w}" for w in rule.allow)], rule.mutes, rule.mute_forever_after,
                     rule.kick_after, rule.kicks, rule.ban_after, rule.label)


def change_rule(path: Path, line: int | None, text: str | None) -> list[Rule]:
    """Put `text` in place of the rule on `line` (None: added at the end); a text of None takes that line out.
    The whole list is read again before it's written, so a change that would break it raises RuleError and the
    file stays as it was. The rules as they are now."""
    try:
        lines = lines_of(read_text(path, RuleError))
    except FileNotFoundError:
        from importlib import resources

        lines = lines_of(resources.files("huragok").joinpath("templates/automod.txt").read_text("utf-8"))
    if kicks_first("\n".join(lines)):
        # Written in this layout, a line would read the old way in a file of the old layout; the file is rewritten
        # in this one a couple of minutes after Huragok starts (updates/hostfiles.py), once the version before it
        # can't be put back.
        raise RuleError(f"{path.name} is still in the layout before this version ({KICKS_FIRST_PARTS}); Huragok "
                        f"rewrites it in this one ({PARTS}) a couple of minutes after it starts: try again then")
    if line is None:
        if text is None:
            raise RuleError("nothing to add")
        lines.append(text)
    elif not 1 <= line <= len(lines) or parse_line(lines[line - 1], line) is None:
        raise RuleError(f"line {line} isn't a rule any more (the file changed): look again")
    elif text is None:
        del lines[line - 1]
    else:
        lines[line - 1] = text
    if text is not None and (rule := parse_line(text, line or len(lines))) is not None and rule.zero_times():
        raise RuleError(f"{rule.zero_times()[0]} is no time at all: write how long (10m, 1h, 7d), or perm for good")
    new = "\n".join(lines) + "\n"
    rules = parse_text(new)
    path.write_text(new, encoding="utf-8")
    return rules


def add_name(path: Path, name: str, by: str, note: str = "") -> None:
    """Add a banned name to names.txt with a line above it saying when and by whom (written back as UTF-8,
    whatever it was saved as). Raises RuleError or OSError when the file can't be read or written."""
    from datetime import datetime
    from importlib import resources

    try:
        text = read_text(path, RuleError)
    except FileNotFoundError:
        text = resources.files("huragok").joinpath("templates/names.txt").read_text("utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    when = datetime.now().strftime("%Y-%m-%d")
    text += f"# banned {when} by {by}" + (f": {note}" if note else "") + f"\n{name}\n"
    path.write_text(text, encoding="utf-8")


def remove_name(path: Path, name: str, same: Any) -> int:
    """Take every line banning `name` out of names.txt (`same(line, name)` says which), with the note above each
    that add_name wrote. How many went. Raises RuleError or OSError."""
    try:
        lines = lines_of(read_text(path, RuleError))
    except FileNotFoundError:
        return 0
    keep: list[str] = []
    removed = 0
    for line in lines:
        if line.strip() and not line.strip().startswith("#") and same(line, name):
            removed += 1
            if keep and keep[-1].startswith("# banned "):
                keep.pop()  # the note written above it
            continue
        keep.append(line)
    if removed:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return removed
