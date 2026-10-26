"""Roles: who a player is to Huragok, from player (everyone) up to owner.

owner, moderator and member come from commands.toml (commandfile.py), the same on every server; admin only from
each server itself (its console's `admin` list, or its dedicated.toml until it answers). A player's rank against a
`!` command is their highest role on any server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from ..core.commandsfile import ROLES

if TYPE_CHECKING:
    from ..servers.state import Player, ServerState


def a_role(role: str) -> str:
    """'an admin', 'a moderator': for players to read."""
    return ("an " if role[:1] in ("a", "e", "i", "o", "u") else "a ") + role


class Roles:
    """owner, moderator and member come from commands.toml; admin only from the server itself."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings  # reads settings.permissions each time: commands.toml reloads while running

    def role_of(self, state: ServerState | None, player: Player) -> str:
        members, pid = self.settings.permissions.members, player.id.lower()
        if pid in members.get("owner", set()):
            return "owner"
        if player.admin or pid in self.server_admins(state):
            return "admin"
        if pid in members.get("moderator", set()):
            return "moderator"
        if pid in members.get("member", set()):
            return "member"
        return "player"

    def highest_role(self, states: Iterable[ServerState], player: Player) -> str:
        """Their role on whichever server ranks them highest (admins are each server's own): what a ! command, which
        reaches every server, mustn't outrank (the host's call, 25 October)."""
        from ..servers.state import Player

        best = self.role_of(None, player)
        for state in states:
            role = self.role_of(state, state.player_by_id(player.id) or Player(0, player.name, player.id))
            if ROLES.index(role) > ROLES.index(best):
                best = role
        return best

    def server_admins(self, state: ServerState | None) -> set[str]:
        """The server's live list (RCON `admin`), or dedicated.toml's until the server answers."""
        if state is None:
            return set()
        if state.admins is not None:
            return state.admins
        try:
            return self.settings.server(state.name).admins
        except KeyError:
            return set()

    @staticmethod
    def allows(role: str, needed: str) -> bool:
        return ROLES.index(role) >= ROLES.index(needed)
