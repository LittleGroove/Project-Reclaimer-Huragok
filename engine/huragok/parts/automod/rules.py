"""Vergil's word list: one text file, one rule per line, in five parts.

    # words                 | kicks        | mutes          | ban | name
    badword, otherword      | 1h, 1d       | 2, 1d, 7d      | 5   | slurs
    worstword               |              |                | 0   | threats
    rudeword                |              | 0, 1d, 3d, 7d  |     | rude
    kickword

Each strike moves a player one step down the line's ladder: kicks first, then
mutes, then the ban.

words    separated by commas. `word*` also catches it at the start of longer
         words, `*word*` anywhere inside one. `-word` is never caught by that line.
kicks    how long each kick keeps the player out, in order (30m, 1h, 1d, 2w); the
         last repeats. Empty: the server's own kick (out 10, 30, 60, then 120
         minutes). A kick with a time is a timed ban underneath, since the
         server's kick times are fixed.
mutes    how many strikes come before the mutes start (0: muted the first time),
         then how long each mute lasts, in order; the last repeats, and `perm`
         (last only) is muted for good. Empty: never muted. A muted player stays
         in the game; nobody hears their voice or reads their chat.
ban      how many strikes come before the ban for good (0: banned the first
         time). Empty: never banned.
name     what players read, and what moderators type in !strike. Optional.

So "badword, otherword | 1h, 1d | 2, 1d, 7d | 5 | slurs" is 1st: out 1 hour,
2nd: out 1 day, 3rd: muted 1 day, 4th and 5th: muted 7 days, 6th: banned.
"kickword" alone is a plain kick every time.

The 0.2 layout, "words | timers | chances | name" (no mutes), is still read the
same way it always was: a line in four parts whose last part is a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UNITS = {"s": ("second", 1), "m": ("minute", 60), "h": ("hour", 3600), "d": ("day", 86400), "w": ("week", 604800)}
TIME = re.compile(r"(\d+)([smhdw])")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
FOREVER = "perm"
NAME_STRIKE = "name"  # `!strike <player> name` bans their name, so no line may be called that
MAX_STRIKES = 20
PARTS = "words | kicks | mutes | ban | name"


class RuleError(ValueError):
    pass


def seconds_of(time: str) -> int:
    number, unit = TIME.fullmatch(time).groups()  # type: ignore[union-attr]
    return int(number) * UNITS[unit][1]


def describe_time(time: str) -> str:
    number, unit = TIME.fullmatch(time).groups()  # type: ignore[union-attr]
    return f"{number} {UNITS[unit][0]}" + ("" if number == "1" else "s")


def describe_seconds(seconds: float) -> str:
    """How long is left, in its two largest units: "2 days 3 hours", "45 minutes"."""
    left = max(1, int(seconds))
    parts = []
    for unit in ("d", "h", "m", "s"):
        word, size = UNITS[unit]
        if left >= size:
            count, left = divmod(left, size)
            parts.append(f"{count} {word}" + ("" if count == 1 else "s"))
        if len(parts) == 2 or (parts and unit == "m"):
            break
    return " ".join(parts)


@dataclass(frozen=True)
class Step:
    """What one strike does: a kick (for `duration`, or the server's plain kick), a mute
    (for `duration`, or for good), or a ban for good."""

    action: str  # kick | mute | ban
    duration: str | None = None

    @property
    def seconds(self) -> int | None:
        return None if self.duration is None else seconds_of(self.duration)

    @property
    def severity(self) -> tuple[int, float]:
        """Heavier steps sort later: a kick, then a mute, then the ban, each longer beating shorter."""
        if self.action == "ban":
            return (3, 0)
        if self.action == "mute":
            return (2, float("inf") if self.duration is None else self.seconds or 0)
        return (1, self.seconds or 0)

    def describe(self) -> str:
        if self.action == "ban":
            return "banned for good"
        if self.action == "mute":
            return f"muted for {describe_time(self.duration)}" if self.duration else "muted for good"
        return f"removed for {describe_time(self.duration)}" if self.duration else "kicked"

    def __str__(self) -> str:
        if self.action == "mute":
            return f"mute {self.duration or FOREVER}"
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
    ban_after: int | None = None  # strikes before the ban; None: never banned
    line: int = 0
    old_layout: bool = False  # written as "words | timers | chances | name"

    def step_for(self, previous: int) -> Step:
        """The step for a player's next strike, given how many still count."""
        if self.ban_after is not None and previous >= self.ban_after:
            return Step("ban")
        if self.mute_after is not None and previous >= self.mute_after:
            time = self.mutes[min(previous - self.mute_after, len(self.mutes) - 1)]
            return Step("mute", None if time == FOREVER else time)
        return Step("kick", self.kicks[min(previous, len(self.kicks) - 1)] if self.kicks else None)

    def strike_text(self, number: int) -> str:
        if self.ban_after and number <= self.ban_after:
            return f"strike {number} of {self.ban_after}"
        return f"strike {number}"


def _many(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _has_text(word: str) -> bool:
    """Whether a listed word has a letter or digit left once matching has taken out what it ignores."""
    from .matcher import normalise  # here: the matcher imports this module

    return any(c.isalnum() for c in normalise(word.strip("*")))


def _codes(text: str) -> str:
    return " ".join(f"U+{ord(c):04X}" for c in text[:6])


def _count(text: str, where: str, what: str) -> int:
    if not text.isdigit() or int(text) > MAX_STRIKES:
        raise RuleError(f"{where}: {what} must be a number from 0 to {MAX_STRIKES}")
    return int(text)


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
    old = len(fields) == 4 and bool(fields[3]) and not fields[3].isdigit()
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

    mute_after, mutes = None, []
    if fields[2]:
        first, _, rest = fields[2].partition(",")
        if not first.strip().isdigit():
            raise RuleError(f"{where}: mutes start with how many strikes come first (0: muted the first time), "
                            f"then how long each mute lasts, like 0, 1d, 3d, 7d")
        mute_after = _count(first.strip(), where, "the strikes before the mutes")
        mutes = _times(rest, where)
        if not mutes:
            raise RuleError(f"{where}: after the {mute_after} in mutes, list how long each mute lasts, like "
                            f"{mute_after}, 1d, 3d, 7d (perm: for good). In the 0.2 layout the 3rd part was the "
                            f"chances before a ban; that is now the 4th: words | kicks | | ban | name")
        if FOREVER in mutes[:-1]:
            raise RuleError(f"{where}: perm can only be the last mute; nothing comes after for good")

    ban_after = None
    if fields[3]:
        ban_after = _count(fields[3], where, "chances" if old else "the strikes before the ban")
        if mute_after is not None and mute_after >= ban_after:
            raise RuleError(f"{where}: the mutes start after {mute_after} strikes but the ban comes after "
                            f"{ban_after}, so they never would")
        if mute_after is not None and len(mutes) > ban_after - mute_after:
            raise RuleError(f"{where}: {_many(len(mutes), 'mute time')} but only "
                            f"{_many(ban_after - mute_after, 'mute')} before the ban; one time per mute at most")

    stops = [n for n in (mute_after, ban_after) if n is not None]
    if stops and len(kicks) > min(stops):
        before = "the mutes" if mute_after is not None and mute_after == min(stops) else "the ban"
        if old:
            raise RuleError(f"{where}: {len(kicks)} timers but {ban_after} chances; one timer per chance at most")
        raise RuleError(f"{where}: {_many(len(kicks), 'kick time')} but only {_many(min(stops), 'kick')} "
                        f"before {before}; one time per kick at most")

    name = fields[4].lower()
    if name and not NAME.fullmatch(name):
        raise RuleError(f"{where}: the name '{fields[4]}' should be one short word: letters, digits, - or _")
    if name == NAME_STRIKE:
        raise RuleError(f"{where}: a line can't be called '{NAME_STRIKE}': !strike <player> {NAME_STRIKE} "
                        f"bans a player's name")
    return Rule(name=name, label=name, words=words, allow=allow, kicks=kicks, mute_after=mute_after,
                mutes=mutes, ban_after=ban_after, line=number, old_layout=old)


def parse_text(text: str) -> list[Rule]:
    rules = [r for r in (parse_line(line, n) for n, line in enumerate(text.splitlines(), 1)) if r]
    for number, rule in enumerate(rules, 1):
        if not rule.name:
            rule.name = f"rule{number}"  # unnamed: !strike still needs something to type
    names = [r.name for r in rules]
    if duplicate := next((n for n in names if names.count(n) > 1), None):
        raise RuleError(f"two lines are named {duplicate}")
    return rules


def unnamed_ladders(rules: list[Rule]) -> list[Rule]:
    """Lines with no name whose strikes change what happens next. Their strikes are kept under the name the
    line's place gives it (rule2), so a line added above one moves everyone's strikes to another line."""
    return [r for r in rules if not r.label and (r.mute_after is not None or r.ban_after is not None
                                                  or len(set(r.kicks)) > 1)]


def describe_unnamed(rule: Rule, file: str) -> str:
    return (f"{file} line {rule.line} has no name, so its strikes are kept under {rule.name}, and they move to "
            f"another line if a line is added above it: name it (the 5th part, {PARTS})")


def read_text(path: Path) -> str:
    """A word list as Windows editors save it: UTF-8 with or without a BOM, or Notepad's "Unicode" (UTF-16
    with a BOM). Anything else (ANSI) is a RuleError that says to save it as UTF-8. OSError passes through."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError:
            raise RuleError(f"{path.name} starts like UTF-16 but isn't: save it as UTF-8") from None
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise RuleError(f"{path.name} isn't saved as UTF-8 (byte {error.start + 1} is {data[error.start]:#04x}; "
                            "ANSI, perhaps): save it as UTF-8 in your editor") from None
        if "\x00" in text:
            raise RuleError(f"{path.name} looks like UTF-16 without a BOM: save it as UTF-8 in your editor")
    return text.replace("\r\n", "\n").replace("\r", "\n")  # as read_text() reads them: Windows line ends too


def read_rules(path: Path) -> list[Rule]:
    try:
        return parse_text(read_text(path))
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
        text = read_text(path)
    except FileNotFoundError:
        return []
    return [BannedName(line.strip(), number) for number, line in enumerate(text.splitlines(), 1)
            if line.strip() and not line.strip().startswith("#")]


# Changing the files from the window (D), and from the game -------------------------------------

def ordinal(number: int) -> str:
    """1st, 2nd, 3rd, 4th… 11th, 12th, 13th, 21st."""
    if number % 100 in (11, 12, 13):
        return f"{number}th"
    return f"{number}" + {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")


def ladder(rule: Rule) -> list[str]:
    """What each strike does, in order, until it stops changing: ["1st: kicked", "2nd and after: muted for 1 day"]."""
    steps = [rule.step_for(previous) for previous in range(MAX_STRIKES + 2)]
    out: list[str] = []
    for number, step in enumerate(steps):
        if all(later == step for later in steps[number:]):  # the same every time from here on
            if step.action == "ban":  # banned for good: no strike comes after it
                out.append(f"{ordinal(number + 1)}: {step.describe()}")
            elif number == 0:
                out.append(f"every time: {step.describe()}")
            else:
                out.append(f"{ordinal(number + 1)} and after: {step.describe()}")
            break
        out.append(f"{ordinal(number + 1)}: {step.describe()}")
    return out


def rule_line(words: list[str], kicks: list[str], mute_after: int | None, mutes: list[str], ban_after: int | None,
              name: str) -> str:
    """A rule as its line in the word list. With a name it's written in all five parts (four parts ending in a
    name read as the 0.2 layout); without one, the empty parts at the end are left off."""
    parts = [", ".join(words), ", ".join(kicks), "" if mute_after is None else ", ".join([str(mute_after), *mutes]),
             "" if ban_after is None else str(ban_after), name]
    while not name and len(parts) > 1 and not parts[-1]:
        parts.pop()
    return parts[0] + "".join(f" | {part}" if part else " |" for part in parts[1:])


def line_of(rule: Rule) -> str:
    return rule_line([*rule.words, *(f"-{w}" for w in rule.allow)], rule.kicks, rule.mute_after, rule.mutes,
                     rule.ban_after, rule.label)


def change_rule(path: Path, line: int | None, text: str | None) -> list[Rule]:
    """Put `text` in place of the rule on `line` (None: added at the end); a text of None takes that line out.
    The whole list is read again before it's written, so a change that would break it raises RuleError and the
    file stays as it was. The rules as they are now."""
    try:
        lines = read_text(path).split("\n")
    except FileNotFoundError:
        from importlib import resources

        lines = resources.files("huragok").joinpath("templates/automod.txt").read_text("utf-8").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
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
        text = read_text(path)
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
        lines = read_text(path).splitlines()
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
