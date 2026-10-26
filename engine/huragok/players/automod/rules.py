"""Vergil's word list: one text file, one rule per line, in five parts.

    # words                 | kicks        | mutes            | ban | name
    badword, otherword      | 1h, 1d       | 2, 1d, 7d        | 5   | slurs
    worstword               |              |                  | 0   | threats
    rudeword                |              | 0, 1d, 3d, 7d    |     | rude
    meanword                |              | 5, 1d, 3d, 7d, 8 |     | mean
    kickword

Each strike moves a player one step down the line's ladder: kicks first, then
mutes, then the ban. Kick times listed past the start of the mutes overlap them:
those strikes remove the player for that long, then mute them once they're back.

words    separated by commas. `word*` also catches it at the start of longer
         words, `*word*` anywhere inside one. `-word` is never caught by that line.
kicks    how long each kick keeps the player out, in order (30m, 1h, 1d, 2w); the
         last repeats until the mutes. Empty: the server's own kick (out 10, 30,
         60, then 120 minutes). A kick with a time is a timed ban underneath,
         since the server's kick times are fixed.
mutes    how many strikes come before the mutes start (0: muted the first time),
         then how long each mute lasts, in order; the last repeats, and `perm`
         (last only) is muted for good. A number last, with no s, m, h, d or w, is
         how many strikes come before muted for good, as the ban counts them: the
         last time repeats until then. Empty: never muted. A muted player stays in
         the game; nobody hears their voice or reads their chat.
ban      how many strikes come before the ban for good (0: banned the first
         time). Empty: never banned.
name     what players read, and what moderators type in !strike. Optional.

So "badword, otherword | 1h, 1d | 2, 1d, 7d | 5 | slurs" is 1st: out 1 hour,
2nd: out 1 day, 3rd: muted 1 day, 4th and 5th: muted 7 days, 6th: banned.
"w | 1h, 1d, 2d | 1, 1d | 4" is 1st: out 1 hour, 2nd: out 1 day then muted 1 day,
3rd: out 2 days then muted 1 day, 4th: muted 1 day, 5th: banned.
"meanword | | 5, 1d, 3d, 7d, 8" is 1st to 5th: kicked, 6th: muted 1 day, 7th: muted
3 days, 8th: muted 7 days, 9th and after: muted for good, never banned.
"kickword" alone is a plain kick every time. There's no limit on the counts or
on how many times a line lists, only that every time comes before the ban.

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
PARTS = "words | kicks | mutes | ban | name"


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    """What one strike does: a kick (for `duration`, or the server's plain kick), a mute
    (for `duration`, or for good; after a timed `kick` when one comes with it), or a ban for good."""

    action: str  # kick | mute | ban
    duration: str | None = None
    kick: str | None = None  # a mute's: removed for this long first, the mute starting once they're back

    @property
    def seconds(self) -> int | None:
        return None if self.duration is None else seconds_of(self.duration)

    @property
    def kick_seconds(self) -> int:
        return 0 if self.kick is None else seconds_of(self.kick)

    @property
    def severity(self) -> tuple[int, float]:
        """Heavier steps sort later: a kick, then a mute, then the ban, each longer beating shorter."""
        if self.action == "ban":
            return (3, 0)
        if self.action == "mute":
            return (2, float("inf") if self.duration is None else (self.seconds or 0) + self.kick_seconds)
        return (1, self.seconds or 0)

    def describe(self) -> str:
        if self.action == "ban":
            return "banned for good"
        if self.action == "mute":
            muted = f"muted for {describe_time(self.duration)}" if self.duration else "muted for good"
            return f"removed for {describe_time(self.kick)}, then {muted}" if self.kick else muted
        return f"removed for {describe_time(self.duration)}" if self.duration else "kicked"

    def __str__(self) -> str:
        if self.action == "mute":
            return (f"kick {self.kick} + " if self.kick else "") + f"mute {self.duration or FOREVER}"
        return self.action + (f" {self.duration}" if self.duration else "")


@dataclass
class Rule:
    name: str
    label: str  # what players read; empty when the line has no name
    words: list[str]
    allow: list[str] = field(default_factory=list)
    kicks: list[str] = field(default_factory=list)
    mute_after: int | None = None  # strikes before the mutes start; None: never muted
    mutes: list[str] = field(default_factory=list)  # each mute's time; "perm" last for good
    mute_forever_after: int | None = None  # strikes before muted for good (the mutes' last number); None: never
    ban_after: int | None = None  # strikes before the ban; None: never banned
    line: int = 0
    old_layout: bool = False  # written as "words | timers | chances | name"

    def step_for(self, previous: int) -> Step:
        """The step for a player's next strike, given how many still count."""
        if self.ban_after is not None and previous >= self.ban_after:
            return Step("ban")
        if self.mute_after is not None and previous >= self.mute_after:
            if self.mute_forever_after is not None and previous >= self.mute_forever_after:
                time = FOREVER
            else:
                time = self.mutes[min(previous - self.mute_after, len(self.mutes) - 1)]
            # A kick time listed for this strike still kicks, the mute after it; the last doesn't repeat here.
            kick = self.kicks[previous] if previous < len(self.kicks) else None
            if _timed(time) is not None:
                return Step("mute", None if time == FOREVER else time, _timed(kick))
            # A mute of 0m mutes nobody (and the server may read 0 as no end): that strike kicks, as the line does
            # without mutes. `check` names the line (zero_times).
        return Step("kick", _timed(self.kicks[min(previous, len(self.kicks) - 1)]) if self.kicks else None)

    def zero_times(self) -> list[str]:
        """The line's times of 0 (0m, 0h), which every version reads but which do nothing a host means: a kick of 0m
        is the server's plain kick, a mute of 0m a kick."""
        return [time for time in (*self.kicks, *self.mutes) if time != FOREVER and seconds_of(time) == 0]

    def changes(self) -> list[int]:
        """The strikes (counted from 0) where what a strike does may change: past the last, it never does."""
        points = set(range(len(self.kicks) + 1))
        if self.mute_after is not None:
            points.update(range(self.mute_after, self.mute_after + len(self.mutes) + 1))
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


def parse_line(text: str, number: int) -> Rule | None:
    """One line of the word list -> a Rule, or None for blank lines and # notes."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    where = f"line {number}"
    fields = [f.strip() for f in text.split("|")]
    if len(fields) > 5:
        raise RuleError(f"{where}: at most 5 parts: {PARTS}")
    # The 0.2 layout: four parts, the last a name (in five parts the fourth is a number).
    old = len(fields) == 4 and bool(fields[3]) and not is_count(fields[3])
    if old:
        fields = [fields[0], fields[1], "", fields[2], fields[3]]
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

    kicks = _times(fields[1], where)
    if FOREVER in kicks:
        raise RuleError(f"{where}: a kick can't be for good; that's the ban, in the 4th part")

    mute_after, mutes, forever_after = None, [], None
    if fields[2]:
        first, _, rest = fields[2].partition(",")
        if not is_count(first.strip()):
            raise RuleError(f"{where}: mutes start with how many strikes come first (0: muted the first time), "
                            f"then how long each mute lasts, like 0, 1d, 3d, 7d")
        mute_after = _count(first.strip(), where, "the strikes before the mutes")
        entries = [t.strip() for t in rest.split(",") if t.strip()]
        if entries and is_count(entries[-1]):
            forever_after = int(entries.pop())  # a number last: the strikes before muted for good
        if count := next((t for t in entries if is_count(t)), None):
            raise RuleError(f"{where}: '{count}' in mutes has no s, m, h, d or w: a number alone, the strikes "
                            f"before muted for good, can only be last, like {mute_after}, 1d, 3d, 7d, 8")
        mutes = _times(", ".join(entries), where)
        if not mutes and forever_after is not None:
            raise RuleError(f"{where}: between the {mute_after} and the {forever_after} in mutes, list how long "
                            f"each mute lasts, like {mute_after}, 1d, 3d, 7d, {forever_after}")
        if not mutes:
            raise RuleError(f"{where}: after the {mute_after} in mutes, list how long each mute lasts, like "
                            f"{mute_after}, 1d, 3d, 7d (perm: for good). In the 0.2 layout the 3rd part was the "
                            f"chances before a ban; that is now the 4th: words | kicks | | ban | name")
        if FOREVER in mutes[:-1]:
            raise RuleError(f"{where}: perm can only be the last mute; nothing comes after for good")
        if forever_after is not None:
            if FOREVER in mutes:
                raise RuleError(f"{where}: perm and the {forever_after} after it both say when the mutes are for "
                                f"good; keep one")
            if forever_after <= mute_after:
                raise RuleError(f"{where}: the mutes start after {mute_after} strikes but are for good after "
                                f"{forever_after}, so no mute time would be used; the last number counts every "
                                f"strike, as the ban does: {mute_after + len(mutes)} or more here")
            if len(mutes) > forever_after - mute_after:
                raise RuleError(f"{where}: {_many(len(mutes), 'mute time')} but only "
                                f"{_many(forever_after - mute_after, 'mute')} before muted for good; one time per "
                                f"mute at most ({mute_after + len(mutes)} or more)")

    ban_after = None
    if fields[3]:
        ban_after = _count(fields[3], where, "chances" if old else "the strikes before the ban")
        if mute_after is not None and mute_after >= ban_after:
            raise RuleError(f"{where}: the mutes start after {mute_after} strikes but the ban comes after "
                            f"{ban_after}, so they never would")
        if forever_after is not None and forever_after >= ban_after:
            raise RuleError(f"{where}: the mutes are for good after {forever_after} strikes but the ban comes after "
                            f"{ban_after}, so they never would be")
        if mute_after is not None and len(mutes) > ban_after - mute_after:
            raise RuleError(f"{where}: {_many(len(mutes), 'mute time')} but only "
                            f"{_many(ban_after - mute_after, 'mute')} before the ban; one time per mute at most")

    # Kick times may run past the start of the mutes (those strikes kick, then mute), but not past the ban.
    if ban_after is not None and len(kicks) > ban_after:
        if old:
            raise RuleError(f"{where}: {len(kicks)} timers but {ban_after} chances; one timer per chance at most")
        raise RuleError(f"{where}: {_many(len(kicks), 'kick time')} but only {_many(ban_after, 'kick')} "
                        f"before the ban; one time per kick at most")

    name = fields[4].lower()
    if name and not NAME.fullmatch(name):
        raise RuleError(f"{where}: the name '{fields[4]}' should be one short word: letters, digits, - or _")
    if name == NAME_STRIKE:
        raise RuleError(f"{where}: a line can't be called '{NAME_STRIKE}': !strike <player> {NAME_STRIKE} "
                        f"bans a player's name")
    return Rule(name=name, label=name, words=words, allow=allow, kicks=kicks, mute_after=mute_after,
                mutes=mutes, mute_forever_after=forever_after, ban_after=ban_after, line=number, old_layout=old)


def lines_of(text: str) -> list[str]:
    """A word list's or names.txt's lines, as change_rule() and remove_name() count them: split at line ends only.
    str.splitlines() also splits at U+2028, U+0085, form feeds and the like, which a pasted name or note can hold: a
    note's tail would become a live rule, and an edit would land on the wrong line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_text(text: str) -> list[Rule]:
    rules = [r for r in (parse_line(line, n) for n, line in enumerate(lines_of(text), 1)) if r]
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
    for number, line in enumerate(lines, 1):
        try:
            rule = parse_line(line, number)
        except RuleError as error:
            place += 1
            left.append(LeftOut(number, line, str(error)))
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
    return [r for r in rules if not r.label and (r.mute_after is not None or r.ban_after is not None
                                                  or len(set(r.kicks)) > 1)]


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
    ["1st and 2nd: kicked", "3rd to 9th: muted for 1 day", "10th and after: muted for 7 days"]."""
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


def least_ban(kicks: list[str], mute_after: int | None, mutes: list[str], forever_after: int | None = None) -> int:
    """The fewest strikes before the ban that leave room for every kick and mute time, and for muted for good."""
    return max(len(kicks), 0 if mute_after is None else mute_after + len(mutes),
               0 if forever_after is None else forever_after + 1)


def least_forever(mute_after: int, mutes: list[str]) -> int:
    """The fewest strikes before muted for good that leave room for every mute time."""
    return mute_after + len(mutes)


def rule_line(words: list[str], kicks: list[str], mute_after: int | None, mutes: list[str],
              forever_after: int | None, ban_after: int | None, name: str) -> str:
    """A rule as its line in the word list. With a name it's written in all five parts (four parts ending in a
    name read as the 0.2 layout); without one, the empty parts at the end are left off. `forever_after` goes last
    in the mutes (the strikes before muted for good)."""
    forever = [] if forever_after is None else [str(forever_after)]
    parts = [", ".join(words), ", ".join(kicks),
             "" if mute_after is None else ", ".join([str(mute_after), *mutes, *forever]),
             "" if ban_after is None else str(ban_after), name]
    while not name and len(parts) > 1 and not parts[-1]:
        parts.pop()
    return parts[0] + "".join(f" | {part}" if part else " |" for part in parts[1:])


def line_of(rule: Rule) -> str:
    return rule_line([*rule.words, *(f"-{w}" for w in rule.allow)], rule.kicks, rule.mute_after, rule.mutes,
                     rule.mute_forever_after, rule.ban_after, rule.label)


def change_rule(path: Path, line: int | None, text: str | None) -> list[Rule]:
    """Put `text` in place of the rule on `line` (None: added at the end); a text of None takes that line out.
    The whole list is read again before it's written, so a change that would break it raises RuleError and the
    file stays as it was. The rules as they are now."""
    try:
        lines = lines_of(read_text(path, RuleError))
    except FileNotFoundError:
        from importlib import resources

        lines = lines_of(resources.files("huragok").joinpath("templates/automod.txt").read_text("utf-8"))
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
