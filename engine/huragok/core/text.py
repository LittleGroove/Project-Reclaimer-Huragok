"""Text every part of Huragok writes the same way."""

from __future__ import annotations

import re
import unicodedata


UNITS = {"s": ("second", 1), "m": ("minute", 60), "h": ("hour", 3600), "d": ("day", 86400), "w": ("week", 604800)}


TIME = re.compile(r"(\d+)([smhdw])")


def slug(name: str, empty: str = "") -> str:
    """A name as a file's: lower case, each run of anything but a-z and 0-9 one hyphen ('LG Test Server' ->
    'lg-test-server'); `empty` when nothing is left of it."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or empty


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


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def size_text(size: int, places: int = 0) -> str:
    """A size as the host reads it: '1.2 GB', '120 MB', '81 KB', '500 bytes'. `places` decimals below a gigabyte
    (a map file's '20.3 KB'); a gigabyte always has one."""
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= scale:
            return f"{size / scale:.{max(places, 1) if unit == 'GB' else places}f} {unit}"
    return f"{size} bytes"


def one_line(text: str) -> str:
    """A message the server sent on several lines, as one log line (the window shows one line per entry)."""
    return " | ".join(part.strip() for part in text.splitlines() if part.strip())


PLAYER_ID = re.compile(r"[0-9a-fA-F]{64}")  # a player ID as a server sends it or someone types it: 64 hex digits
STORED_ID = re.compile(r"[0-9a-f]{64}")  # as Huragok's files keep them, and dedicated.toml's admins must be


def is_player_id(text: str) -> bool:
    """A whole player ID (either case), not a name or the start of one."""
    return bool(PLAYER_ID.fullmatch(text))


# Text as it looks: what the word list and player lookups compare (players/automod/matcher.py, players/targets.py).
# Taken out first: format characters (zero-width spaces and joiners, direction marks, tag characters), combining and
# enclosing marks (accents, variation selectors), unassigned code points, and the fillers that show as nothing (Hangul
# fillers, the blank Braille pattern). NFKD before that already turns fullwidth (Ａ), styled (𝐀 𝑨 𝓐), circled (ⓐ),
# squared (🄰), superscript and subscript letters into plain ones.
HIDDEN = {"Cf", "Mn", "Me", "Cn"}
FILLERS = re.compile("[" + "".join(map(chr, (0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800))) + "]")
# Capitals of other scripts that look like Latin ones, read before lower case: Greek Ν is N (its lower case, ν, looks
# like v), Η is H (η looks like n).
CAPITAL_LOOKALIKES = str.maketrans({
    # Greek
    "Α": "a", "Β": "b", "Ε": "e", "Ζ": "z", "Η": "h", "Ι": "i", "Κ": "k", "Μ": "m", "Ν": "n", "Ο": "o", "Ρ": "p",
    "Τ": "t", "Υ": "y", "Χ": "x", "Ϲ": "c", "Ϳ": "j",
    # Cyrillic
    "А": "a", "В": "b", "Е": "e", "Ѕ": "s", "І": "i", "Ї": "i", "Ј": "j", "К": "k", "М": "m", "Н": "h", "О": "o",
    "Р": "p", "С": "c", "Т": "t", "У": "y", "Х": "x", "Ԛ": "q", "Ԝ": "w", "Ү": "y", "Һ": "h", "Ӏ": "l", "Ԁ": "d",
})
# Lower case letters that look like Latin ones, read after it.
LOOKALIKES = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ӏ": "l",
    "ү": "y",
    # Greek
    "α": "a", "β": "b", "γ": "y", "ε": "e", "η": "n", "ι": "i", "κ": "k", "μ": "u", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "ω": "w", "ϲ": "c", "ϳ": "j",
    # Armenian
    "ո": "n", "օ": "o", "ս": "u", "հ": "h",
    # small capitals, as "fancy text" makers write them: ɴᴀᴍᴇ
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k",
    "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ꞯ": "q", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v",
    "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
    # Latin letters of other shapes
    "ɡ": "g", "ɑ": "a", "ɩ": "i", "ʋ": "v", "ı": "i", "ł": "l", "ø": "o", "đ": "d", "ħ": "h", "ŧ": "t",
    "ß": "ss", "æ": "ae", "œ": "oe",
    # letters in boxes that NFKD leaves: 🅐 (negative circled), 🅰 (negative squared), 🇦 (regional indicators)
    **{chr(start + n): letter for start in (0x1F150, 0x1F170, 0x1F1E6)
       for n, letter in enumerate("abcdefghijklmnopqrstuvwxyz")},
})


def lookalike(text: str) -> str:
    """Text as it looks, for comparing: lower case, no accents or invisible characters, and letters of other scripts
    and styles (Greek, Cyrillic, small capitals, fullwidth, styled, circled) as the Latin letters they look like:
    "ΒΑΝΗΑΜΜΕR", "ʙᴀɴʜᴀᴍᴍᴇʀ" and "ｂａｎｈａｍｍｅｒ" are all "banhammer". Spaces and everything else stay as they are."""
    text = FILLERS.sub("", text)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) not in HIDDEN)
    return text.translate(CAPITAL_LOOKALIKES).casefold().translate(LOOKALIKES)
