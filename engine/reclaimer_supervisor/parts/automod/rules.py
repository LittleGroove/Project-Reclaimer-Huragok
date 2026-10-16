"""automod's word list: one text file, one rule per line.

    # words                 | timers       | chances | name
    badword, otherword      | 1h, 1d, 7d   | 3       | slurs
    kickword

words    separated by commas. `word*` also catches it at the start of longer
         words, `*word*` anywhere inside one. `-word` is never caught by that line.
timers   how long each offence keeps the player out, in order (30m, 1h, 1d, 2w);
         the last repeats. Empty: the server's own kick (out 10, 30, 60, then 120
         minutes). A kick with a timer is a timed ban underneath, since the
         server's kick times are fixed.
chances  how many times they're removed and let back; the next time is a ban for
         good. Empty: never banned. 0: zero tolerance. At most one timer per chance.
name     what players read, and what moderators type in !strike. Optional.

So "badword, otherword | 1h, 1d, 7d | 3 | slurs" is 1st: out 1 hour, 2nd: out 1 day,
3rd: out 1 week, 4th: banned. "kickword" alone is a plain kick every time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

UNITS = {"s": ("second", 1), "m": ("minute", 60), "h": ("hour", 3600), "d": ("day", 86400), "w": ("week", 604800)}
TIME = re.compile(r"(\d+)([smhdw])")
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
MAX_CHANCES = 20


class RuleError(ValueError):
    pass


def describe_time(time: str) -> str:
    number, unit = TIME.fullmatch(time).groups()  # type: ignore[union-attr]
    return f"{number} {UNITS[unit][0]}" + ("" if number == "1" else "s")


@dataclass(frozen=True)
class Step:
    """What one offence does: a kick (for `duration`, or the server's plain kick) or a ban for good."""

    action: str  # kick | ban
    duration: str | None = None

    @property
    def seconds(self) -> int | None:
        if self.duration is None:
            return None
        number, unit = TIME.fullmatch(self.duration).groups()  # type: ignore[union-attr]
        return int(number) * UNITS[unit][1]

    @property
    def severity(self) -> tuple[int, int]:
        return (2, 0) if self.action == "ban" else (1, self.seconds or 0)

    def describe(self) -> str:
        if self.action == "ban":
            return "banned for good"
        return f"removed for {describe_time(self.duration)}" if self.duration else "kicked"

    def __str__(self) -> str:
        return self.action + (f" {self.duration}" if self.duration else "")


@dataclass
class Rule:
    name: str
    label: str  # what players read; empty when the line has no name
    words: list[str]
    allow: list[str] = field(default_factory=list)
    timers: list[str] = field(default_factory=list)
    chances: int | None = None  # None: never banned
    line: int = 0

    def step_for(self, previous: int) -> Step:
        """The step for a player's next offence, given how many still count."""
        if self.chances is not None and previous >= self.chances:
            return Step("ban")
        return Step("kick", self.timers[min(previous, len(self.timers) - 1)] if self.timers else None)

    def strike_text(self, number: int) -> str:
        if self.chances and number <= self.chances:
            return f"strike {number} of {self.chances}"
        return f"strike {number}"


def parse_line(text: str, number: int) -> Rule | None:
    """One line of the word list -> a Rule, or None for blank lines and # notes."""
    text = text.strip()
    if not text or text.startswith("#"):
        return None
    where = f"line {number}"
    fields = [f.strip() for f in text.split("|")]
    if len(fields) > 4:
        raise RuleError(f"{where}: at most 4 parts: words | timers | chances | name")
    fields += [""] * (4 - len(fields))
    entries = [w.strip() for w in fields[0].split(",") if w.strip()]
    words = [w for w in entries if not w.startswith("-")]
    allow = [w[1:].strip() for w in entries if w.startswith("-") and w[1:].strip()]
    if not words or any(not w.strip("*").strip() for w in words):
        raise RuleError(f"{where}: no words before the first |")

    timers = [t.strip().lower() for t in fields[1].split(",") if t.strip()]
    for timer in timers:
        if not TIME.fullmatch(timer):
            raise RuleError(f"{where}: '{timer}' isn't a time; use a number and s, m, h, d or w, like 30m or 7d")
    chances = None
    if fields[2]:
        if not fields[2].isdigit() or int(fields[2]) > MAX_CHANCES:
            raise RuleError(f"{where}: chances must be a number from 0 to {MAX_CHANCES}, or empty for never banned")
        chances = int(fields[2])
        if len(timers) > chances:
            raise RuleError(f"{where}: {len(timers)} timers but {chances} chances; one timer per chance at most")
    name = fields[3].lower()
    if name and not NAME.fullmatch(name):
        raise RuleError(f"{where}: the name '{fields[3]}' should be one short word: letters, digits, - or _")
    return Rule(name=name, label=name, words=words, allow=allow, timers=timers, chances=chances, line=number)


def parse_text(text: str) -> list[Rule]:
    rules = [r for r in (parse_line(line, n) for n, line in enumerate(text.splitlines(), 1)) if r]
    for number, rule in enumerate(rules, 1):
        if not rule.name:
            rule.name = f"rule{number}"  # unnamed: !strike still needs something to type
    names = [r.name for r in rules]
    if duplicate := next((n for n in names if names.count(n) > 1), None):
        raise RuleError(f"two lines are named {duplicate}")
    return rules


def read_rules(path: Path) -> list[Rule]:
    try:
        return parse_text(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise RuleError(f"{path} not found: starting the supervisor writes one, or set words in [automod]") from None
    except RuleError as error:
        raise RuleError(f"{path.name} {error}") from None
