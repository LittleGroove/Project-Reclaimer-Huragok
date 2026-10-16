"""Find listed words in chat, however they're disguised.

List each word once: these are caught too.
  capitals and accents        BADWORD, bädwörd
  digits and symbols          b4dw0rd, b@dword
  spacing and punctuation     b a d w o r d, b.a.d.w.o.r.d
  repeated letters            baaaadwoooord
  lookalike letters           Cyrillic а, е, о… in place of Latin
  invisible characters        zero-width spaces between letters
  plurals (whole words)       badwords, badwordz

Doubled letters must stay doubled: a word listed with "gg" needs at least two g's,
so a different word that only has one isn't caught. A listed word only matches
whole words (and their plurals) unless it has a *: "word*" also matches the start
of longer words, "*word*" anywhere. A line's -word entries are never caught.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .rules import Rule

INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠋-᠏​-‏"
                       "‪-‮⁠-⁯ㅤ︀-️﻿ﾠ]")
# Letters from other scripts that look like Latin ones.
CONFUSABLES = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g", "ո": "n",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ı": "i", "ł": "l", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe",
})
# What may stand in for each letter.
STAND_INS = {
    "a": "a4@", "b": "b8", "e": "e3€", "g": "g96", "i": "i1!|", "l": "l1|", "o": "o0",
    "s": "s5$", "t": "t7+", "z": "z2",
}
SEPARATOR = r"[\s.,\-_*'\"`~^:;/\\#=()\[\]{}<>]{0,3}"
PLURAL = rf"(?:{SEPARATOR}(?:[e3]{SEPARATOR})?[s5$z]+)?"
WORD_CHAR = "a-z0-9"
MAX_LENGTH = 600


def normalise(text: str) -> str:
    """Lowercase, no accents or invisible characters, lookalikes made Latin, long runs cut to two."""
    text = INVISIBLE.sub("", text[:MAX_LENGTH])
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.casefold().translate(CONFUSABLES)
    # "baaaaaad" -> "baad": keeps the regex fast; patterns accept any run of a letter.
    return re.sub(r"(.)\1{2,}", r"\1\1", text)


def _letter(char: str) -> str:
    stand_ins = STAND_INS.get(char)
    if stand_ins:
        return "[" + re.escape(stand_ins) + "]+"
    return re.escape(char) + "+"


def pattern(word: str) -> str:
    """The regex for one listed word or phrase; a * at either end lifts that end's word boundary."""
    open_start, open_end = word.startswith("*"), word.endswith("*")
    # A phrase's spaces are covered by the separator allowed between any two letters.
    body = SEPARATOR.join(_letter(c) for c in normalise(word.strip("*")).strip() if not c.isspace())
    start = "" if open_start else rf"(?<![{WORD_CHAR}])"
    end = "" if open_end else rf"{PLURAL}(?![{WORD_CHAR}])"
    return start + body + end


@dataclass(frozen=True)
class Hit:
    rule: Rule
    start: int
    end: int


class Matcher:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = rules
        self._compiled: list[tuple[Rule, re.Pattern[str], re.Pattern[str] | None]] = []
        for rule in rules:
            if not rule.words:
                continue
            words = re.compile("|".join(pattern(w) for w in rule.words))
            allow = re.compile("|".join(pattern(w) for w in rule.allow)) if rule.allow else None
            self._compiled.append((rule, words, allow))

    def find(self, text: str, *, from_position: int = 0) -> list[Hit]:
        """Each rule that matches, once, with where. `from_position` ignores matches ending before it."""
        normal = normalise(text)
        hits = []
        for rule, words, allow in self._compiled:
            allowed = [(m.start(), m.end()) for m in allow.finditer(normal)] if allow else []
            for match in words.finditer(normal):
                if match.end() <= from_position:
                    continue
                if any(start <= match.start() and match.end() <= end for start, end in allowed):
                    continue
                hits.append(Hit(rule, match.start(), match.end()))
                break
        return hits
