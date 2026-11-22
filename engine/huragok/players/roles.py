"""Roles: who a player is to Huragok, from player (everyone) up to faculty.

faculty, moderator and member come from commands.toml (commandfile.py), the same on every server; admin only from
each server itself (its console's `admin` list, or its dedicated.toml until it answers). A probationary moderator
(0.5.3) is a member commands.toml lists whom Huragok's database marks on probation (`probation`, which the commands
part keeps: players/commands/requests.py).

With [automod] across_servers on (the default), a rank is everyone's on every server: an admin on one server is an
admin on all of them, for what they do and for what's done to them (the host's call, 21 November 2026: "admin on one
server? admin everywhere"). With it off, what someone may do is their rank on the server they're on; what protects
them from a command is still their highest anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from ..core.commandsfile import PROBATION, ROLES
from .reach import across

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ..servers.state import Player, ServerState


def role_name(role: str) -> str:
    """A role as players read it: 'probationary moderator', 'faculty'."""
    from ..core.commandsfile import rank_word

    return rank_word(role)


def a_role(role: str) -> str:
    """'an admin', 'a moderator', 'faculty': for players to read."""
    if role == "faculty":
        return role_name(role)
    return ("an " if role[:1] in ("a", "e", "i", "o", "u") else "a ") + role_name(role)


class Roles:
    """faculty, moderator and member come from commands.toml; admin only from the server itself."""

    def __init__(self, settings: Any, states: Mapping[str, ServerState] | None = None) -> None:
        self.settings = settings  # reads settings.permissions each time: commands.toml reloads while running
        self.probation: set[str] = set()  # members on probation as moderators (requests.py keeps it)
        self.states = states  # every server, as Huragok has them: a rank on one is a rank on all (across_servers)

    def everywhere(self) -> bool:
        """Whether a rank on one server is a rank on all of them: [automod] across_servers."""
        return self.states is not None and across(self.settings)

    def role_of(self, state: ServerState | None, player: Player) -> str:
        """Their role: on every server, their highest anywhere (across_servers); else on this server."""
        if self.everywhere() and self.states is not None:
            return self.highest_role(list(self.states.values()), player)
        return self.role_on(state, player)

    def role_on(self, state: ServerState | None, player: Player) -> str:
        """Their role on this one server (an admin is on its list), whatever across_servers says."""
        members, pid = self.settings.permissions.members, player.id.lower()
        if pid in members.get("faculty", set()):
            return "faculty"
        if player.admin or pid in self.server_admins(state):
            return "admin"
        if pid in members.get("moderator", set()):
            return "moderator"
        if pid in members.get("member", set()):
            return PROBATION if pid in self.probation else "member"
        return "player"

    def highest_role(self, states: Iterable[ServerState], player: Player) -> str:
        """Their role on whichever server ranks them highest (admins are each server's own lists): what a command
        mustn't outrank (the host's call, 25 October), and, with across_servers, their rank everywhere."""
        from ..servers.state import Player

        best = self.role_on(None, player)
        for state in states:
            role = self.role_on(state, state.player_by_id(player.id) or Player(0, player.name, player.id))
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
