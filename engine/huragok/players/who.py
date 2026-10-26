"""Who the server says acted, as it announces a kick, ban, mute or pardon.

    "Alice (RCON)", "RCON"   through the console: Huragok's ! commands, Vergil, the relay, a console tool (the name
                             is whoever acted through it)
    "Alice"                  an admin's own command in the game (/ban)
    "vote", "Server", "time" a vote; the server itself (a load timeout); a mute running out
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONSOLE = " (RCON)"  # what the server adds to a name that acted through the console


def through_console(by: Any) -> bool:
    """Whether the server says an action came through the console (Huragok's ! commands, Vergil, the relay): it names
    those "Alice (RCON)" or "RCON". An admin's own in game is just "Alice"; a vote's is "vote", the server's
    "Server"."""
    text = str(by or "").strip()
    return text == "RCON" or text.endswith("(RCON)")


def from_console(by: Any, servers: Mapping[str, Any], server: str | None) -> bool:
    """through_console, for an action the server announces as it happens: not when a player on that server is called
    all of `by`. Players pick their own names, and one called "Mallory (RCON)" whose own /ban read as the console's
    would have it put on every server, and the target's roles taken away."""
    text = str(by or "").strip()
    if not through_console(text):
        return False
    state = servers.get(server or "")
    players = getattr(state, "players", None) or {}
    return not any(str(getattr(player, "name", "")).strip() == text for player in players.values())


def acting_name(by: Any) -> str:
    """The name that acted, without the console's mark: "Alice (RCON)" is Alice."""
    return str(by or "").removesuffix(CONSOLE)


def who(by: Any) -> str:
    """Who acted, as players read it: "Alice", not "Alice (RCON)"; "a vote", "an admin"."""
    text = str(by or "").removesuffix(" (RCON)").strip()
    return {"": "an admin", "RCON": "the console", "vote": "a vote", "Server": "the server"}.get(text, text)
