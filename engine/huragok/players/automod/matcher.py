"""Find listed words in chat, however they're disguised.

List each word once: these are caught too.
  capitals and accents        BADWORD, bädwörd
  digits and symbols          b4dw0rd, b@dword, b@dw()rd
  spacing and punctuation     b a d w o r d, b.a.d.w.o.r.d, b?a!d|w+o%r&d: up to four of
                              anything that isn't a letter or digit between two letters
  repeated letters            baaaadwoooord
  lookalike letters           Greek, Cyrillic and Armenian ones, small capitals (ɴᴀᴍᴇ), fullwidth,
                              styled, circled and boxed letters (core/text.py lookalike)
  invisible characters        zero-width spaces, tag characters, variation selectors
  plurals (whole words)       badwords, badwordz

Doubled letters must stay doubled: a word listed with "gg" needs at least two g's,
so a different word that only has one isn't caught. A listed word only matches
whole words (and their plurals) unless it has a *: "word*" also matches the start
of longer words, "*word*" anywhere. A line's -word entries are never caught.

A number is never taken for a word: with "ass" listed, "455 kills", "4:55 left"
and "respawn in 45s" aren't caught. A match of a listed word that has letters
needs at least as many letters as digits (so "a55" isn't caught either; "a5s" and
"@$$" are). A listed word that is all digits, like a hate number, is matched as
written.

Players' names are written run together, so the word list catches names one of
two ways ([automod] name_match):
  anywhere   the word anywhere in the name, as if listed *word*: hitler catches
             Hitler, DasHitler, xXhitlerXx. A short word catches more than meant
             (ass in Assassin) unless its line lists -assassin.
  words      the name taken apart where its capitals and digits change first, then
             whole words as in chat: DasHitler is "Das Hitler" and caught,
             dashitler isn't.
Either way a listed number is never caught inside a longer one: 88 isn't in 1988.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...core.text import lookalike
from .rules import Rule

# What may stand in for each letter.
STAND_INS = {
    "a": "a4@", "b": "b8", "e": "e3€", "g": "g96", "i": "i1!|", "l": "l1|", "o": "o0",
    "s": "s5$", "t": "t7+", "z": "z2",
}
PAIRS = {"o": r"\(\)"}  # stand-ins of two characters: () for o
SYMBOLS = set("".join(STAND_INS.values())) - set("abcdefghijklmnopqrstuvwxyz0123456789") | set("()")
SEPARATOR_LENGTH = 4  # characters at most between two letters
PLURAL_SEPARATOR = r"[\s.,\-_*'\"`~^:;/\\#=()\[\]{}<>]{0,3}"
PLURAL = rf"(?:{PLURAL_SEPARATOR}(?:[e3]{PLURAL_SEPARATOR})?[s5$z]+)?"
WORD_CHAR = "a-z0-9"
MAX_LENGTH = 600
NEVER = "(?!)"  # a pattern that matches nothing
NAME_MATCHES = ("anywhere", "words")  # how the word list catches a name: [automod] name_match


def normalise(text: str) -> str:
    """Lowercase, no accents or invisible characters, lookalikes made Latin (core/text.py), long runs cut to two."""
    text = lookalike(text[:MAX_LENGTH])
    # "baaaaaad" -> "baad": keeps the regex fast; patterns accept any run of a letter.
    return re.sub(r"(.)\1{2,}", r"\1\1", text)


def _letter(char: str) -> str:
    choices = "[" + re.escape(STAND_INS.get(char, char)) + "]"
    if char in PAIRS:
        choices = f"(?:{choices}|{PAIRS[char]})"
    return choices + "+"


def _separator(before: str, after: str) -> str:
    """What may come between two letters: up to four of anything that isn't a letter or digit, but not
    the stand-ins of these two letters (those are the letters), so the regex never has two readings."""
    own = sorted(set(STAND_INS.get(before, "") + STAND_INS.get(after, "")
                     + ("()" if "o" in (before, after) else "")) & SYMBOLS)
    return rf"(?:[^\w{re.escape(''.join(own))}]|_){{0,{SEPARATOR_LENGTH}}}"


def pattern(word: str, *, anywhere: bool = False) -> str:
    """The regex for one listed word or phrase; a * at either end lifts that end's word boundary, and `anywhere`
    lifts both (but a number stays a whole number)."""
    open_start, open_end = word.startswith("*") or anywhere, word.endswith("*") or anywhere
    # A phrase's spaces are covered by the separator allowed between any two letters.
    letters = [c for c in normalise(word.strip("*")).strip() if not c.isspace()]
    if not any(c.isalnum() for c in letters):
        return NEVER  # a word of nothing (rules.py refuses these) would match everywhere
    body = _letter(letters[0]) + "".join(_separator(a, b) + _letter(b) for a, b in zip(letters, letters[1:]))
    if anywhere and all(c.isdigit() for c in letters):
        return rf"(?<!\d){body}(?!\d)"  # 88 is in Sniper88 and xX88Xx, not in 1988
    start = "" if open_start else rf"(?<![{WORD_CHAR}])"
    end = "" if open_end else rf"{PLURAL}(?![{WORD_CHAR}])"
    return start + body + end


def apart(name: str) -> str:
    """A name's run-together words taken apart where its capitals and digits change: "DasHitler" -> "Das Hitler",
    "xXHitlerXx" -> "x X Hitler Xx", "Sniper1488" -> "Sniper 1488". A listed word still matches across the spaces
    this puts in (they're separators, like any other), so "HiTlEr" is still hitler."""
    out: list[str] = []
    for i, char in enumerate(name):
        before, after = name[i - 1] if i else "", name[i + 1] if i + 1 < len(name) else ""
        if before and (before.islower() and char.isupper()
                       or before.isupper() and char.isupper() and after.islower()
                       or before.isalpha() and char.isdigit() or before.isdigit() and char.isalpha()):
            out.append(" ")
        out.append(char)
    return "".join(out)


def _wordy(word: str) -> bool:
    """A listed word with more letters than digits: its matches need letters too, so a number isn't it."""
    normal = normalise(word.strip("*"))
    return sum(c.isalpha() for c in normal) > sum(c.isdigit() for c in normal)


def _number_like(text: str) -> bool:
    """More digits than letters: "455", "4:55", "45s" are numbers, not a disguised word."""
    return sum(c.isdigit() for c in text) > sum(c.isalpha() for c in text)


@dataclass(frozen=True)
class Hit:
    rule: Rule
    start: int  # where in the text as matched (normalised: lowercase, disguises undone)
    end: int
    word: str = ""  # the listed word that caught it, as the line lists it
    found: str = ""  # what it caught, as matched


class Matcher:
    def __init__(self, rules: list[Rule], *, names: str | None = None) -> None:
        """Chat, or with `names` ("anywhere" or "words", NAME_MATCHES) players' names."""
        if names not in (None, *NAME_MATCHES):
            raise ValueError(f"name_match: '{names}' isn't one of {', '.join(NAME_MATCHES)}")
        self.rules = rules
        self.names = names
        anywhere = names == "anywhere"
        # Per rule: one regex with a group per word (w0, w1…), which of them need letters, and the exceptions.
        self._compiled: list[tuple[Rule, re.Pattern[str], set[str], re.Pattern[str] | None]] = []
        for rule in rules:
            if not rule.words:
                continue
            words = re.compile("|".join(f"(?P<w{n}>{pattern(w, anywhere=anywhere)})" for n, w in enumerate(rule.words)))
            wordy = {f"w{n}" for n, w in enumerate(rule.words) if _wordy(w)}
            allow = re.compile("|".join(pattern(w, anywhere=anywhere) for w in rule.allow)) if rule.allow else None
            self._compiled.append((rule, words, wordy, allow))

    def find(self, text: str, *, from_position: int = 0) -> list[Hit]:
        """Each rule that matches, once, with where. `from_position` ignores matches ending before it."""
        normal = normalise(apart(text) if self.names == "words" else text)
        hits = []
        for rule, words, wordy, allow in self._compiled:
            allowed = [(m.start(), m.end()) for m in allow.finditer(normal)] if allow else []
            for match in words.finditer(normal):
                if match.end() <= from_position:
                    continue
                if match.lastgroup in wordy and _number_like(match.group()):
                    continue
                if any(start <= match.start() and match.end() <= end for start, end in allowed):
                    continue
                word = rule.words[int(str(match.lastgroup)[1:])] if match.lastgroup else ""
                hits.append(Hit(rule, match.start(), match.end(), word, match.group()))
                break
        return hits
