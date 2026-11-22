"""How far an action reaches, worked out in one place, so every card, reply and log line says the same.

[automod] across_servers is the one switch (on by default; the 0.7.0 update turns it on where it was off). On, moderation
is every server's: a kick, ban, unban, letting back (a pardon), mute or unmute reaches every server, whoever gave it:
Huragok's ! commands, Vergil, Discord, the console, the relay, the window, and the game's own / commands and scoreboard
(the host's call, 21 November 2026: "no more split actions on a per-server basis"). Off, each server's moderation is its
own. Either way a vote's kick and the server's own (a load timeout, a mute running out) are that server's alone: a vote
is players saying "leave this server". A server's own settings and commands (!togglevoice, !team, !respawn, restarts)
are always its own, and aren't moderation.

A rank works the same way: on, a rank someone holds on any server is theirs on every server ("admin on one server? admin
everywhere"); off, each server's.

What's the player's, not any server's, names no server: a strike, a warning, a note.
"""

from __future__ import annotations

from typing import Any

from .who import acting_name

EVERY_SERVER = "every server"
MODERATION = frozenset({"kick", "ban", "unban", "pardon", "mute", "unmute"})
NOT_A_PERSON = frozenset({"vote", "server", "time"})  # who acted, as the server names it, when nobody did: a vote, itself


def across(settings: Any) -> bool:
    """[automod] across_servers, from Huragok's settings: on unless it's set to false."""
    parts = getattr(settings, "parts", None) or {}
    part = parts.get("automod") if isinstance(parts, dict) else None
    return bool(part.options.get("across_servers", True)) if part is not None else True


def by_a_person(by: Any) -> bool:
    """Whether someone acted (an admin in game, the console, a ! command, Vergil), not a vote or the server itself."""
    text = acting_name(by).strip().lower()
    return bool(text) and text not in NOT_A_PERSON


def reaches_every_server(kind: Any, by: Any, across_servers: bool) -> bool:
    """Whether this action, given by `by`, reaches every server."""
    return across_servers and str(kind or "") in MODERATION and by_a_person(by)


def where_it_reaches(kind: Any, by: Any, server: str | None, across_servers: bool) -> str:
    """"every server", or the one server's name ("" when there's none to name)."""
    return EVERY_SERVER if reaches_every_server(kind, by, across_servers) else str(server or "")
