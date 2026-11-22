"""How a player is written, everywhere Huragok names one: in game, in Discord, in the window and in its log.

    Nate Higgers 1a2b3c4d (#4) [HugeGrass]

- The name they go by: their name on the server they're on, else the last one seen.
- The first 8 characters of their player ID. Anyone can type it back (`!kick 1a2b3c4d`), on any server, and it never
  runs out the way a #number or an @number does. The log has the whole ID, so one search ties it to the ban lists.
- The handle: in a confirmation, the #number or @number that was typed; in a list, the one to type. #4 is someone on
  the server the reader is on, @2 an entry of the reader's own list (players/targets.py). Left off where nobody types
  it back: a Discord card, a log line.
- The server they're on, in brackets, when they're on one: where the player is, never where a command ran.

Read it from the right: the server in [ ], the handle in ( ), then 8 hex digits; what's left is the name, however it's
written. Names are free text, with brackets, numbers or a # in some, so the fixed part goes at the end.

The host's call, 21 November 2026 (plans/player-identity-audit): one way to write a player, wherever one is named.
What's stored stays plain (a name, an ID); the tag is made when it's shown.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from .text import is_player_id

__all__ = ["NO_NAME", "SHORT", "named_then", "short_id", "shown_name", "tag"]

SHORT = 8  # characters of a player ID a tag shows, and the fewest that name a player when typed
NO_NAME = "(no name yet)"  # Project Reclaimer 0.10 names a player a few seconds after they join


def short_id(player_id: Any) -> str:
    """The start of a player ID, as tags show it: "1a2b3c4d"."""
    return str(player_id or "").strip().lower()[:SHORT]


def shown_name(name: Any) -> str:
    """A name on one line: no line breaks, tabs or other control characters, which would break a chat line, a log
    line or a list's columns."""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in str(name or ""))
    return " ".join(text.split())


def tag(name: Any, player_id: Any = "", *, handle: str = "", server: str = "", whole: bool = False,
        code: bool = False) -> str:
    """A player as Huragok writes one: "Nate Higgers 1a2b3c4d (#4) [HugeGrass]". `handle`: "#4" or "@2", or "" where
    nobody types it back. `server`: the one they're on, "" for none. `whole`: the whole 64-character ID (the log).
    `code`: the ID in a code box, for Discord (a name for Discord is cleaned before it comes here).

    Something that isn't a player (an address, a name banned as text) has no ID to show: it's written as it is."""
    player_id = str(player_id or "").strip().lower()
    known = is_player_id(player_id)
    parts = [shown_name(name) or (NO_NAME if known else "someone")]
    if known:
        shown = player_id if whole else player_id[:SHORT]
        parts.append(f"`{shown}`" if code else shown)
    if handle:
        parts.append(f"({handle})")
    if server:
        parts.append(f"[{server}]")
    return " ".join(parts)


def named_then(then: Any, now: Any) -> str:
    """The name something was done under, when it isn't the name they go by now ("banned as Alpha"); "" when it's the
    same, in any case, or isn't known."""
    then_shown, now_shown = shown_name(then), shown_name(now)
    return then_shown if then_shown and then_shown.casefold() != now_shown.casefold() else ""
