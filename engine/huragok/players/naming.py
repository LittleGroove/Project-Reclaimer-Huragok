"""Who a player ID is, for a tag (core/tags.py): the name they go by on the server they're on now, else the last name
they were seen under, and that server. Every server's live picture and the players Huragok has seen are both here, so
anything holding them (a command, the Discord bot, the window) writes a player the same way."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..core.tags import tag
from ..core.text import is_player_id
from ..servers.state import Player

if TYPE_CHECKING:
    from ..core.storage import PlayerHistory
    from ..servers.state import ServerState

__all__ = ["Names"]


class Names:
    """Names and servers by player ID, from every server's picture and the players seen."""

    def __init__(self, states: Mapping[str, ServerState] | None, history: PlayerHistory | None) -> None:
        self.states = states or {}
        self.history = history

    def on(self, player_id: str) -> tuple[str, Player | None]:
        """The server they're on now and them as it sees them; ("", None) when they're on none. A server whose console
        is down still has its last picture: they may have left it since, so it counts only while it's connected, or
        when no connected one has them."""
        player_id = str(player_id or "").lower()
        if not is_player_id(player_id):
            return "", None
        stale: tuple[str, Player | None] = ("", None)
        for server, state in self.states.items():
            player = state.player_by_id(player_id)
            if player is None:
                continue
            if getattr(state, "connected", True):
                return str(server), player
            stale = stale if stale[0] else (str(server), player)
        return stale

    def name(self, player_id: str, given: str = "") -> str:
        """The name they go by: on a server now, the one it has (once 0.10 has named them); else the last one seen;
        else `given` (what a record says)."""
        _, player = self.on(player_id)
        if player is not None and player.name:
            return player.name
        record = self.history.get(str(player_id).lower()) if self.history is not None and is_player_id(str(player_id)) else None
        if record is not None and record.last_name:
            return str(record.last_name)
        return given

    def tag(self, who: Player | str, given: str = "", *, handle: str = "", server: str | None = None,
            whole: bool = False, code: bool = False) -> str:
        """A player written the one way (core/tags.py), from a Player or a player ID. `server`: where they are, if the
        caller knows better; None looks it up; "" leaves it off. `given`: the name a record has, used when nothing
        newer is known (a name banned as text, an address)."""
        player_id = who.id if isinstance(who, Player) else str(who or "")
        if isinstance(who, Player) and not given:
            given = who.name
        where, _ = self.on(player_id) if server is None else (server, None)
        return tag(self.name(player_id, given) if is_player_id(player_id) else given, player_id, handle=handle,
                   server=where, whole=whole, code=code)

    def public(self, who: Player | str, given: str = "") -> str:
        """For everyone on a server to read (a [Server] line, a line told to a player): with their #number on the server
        they're on, which anyone there can use; never an @number, which is one reader's own."""
        player_id = who.id if isinstance(who, Player) else str(who or "")
        where, live = self.on(player_id)
        return self.tag(who, given, handle=f"#{live.number}" if live is not None and live.number else "", server=where)

    def describe(self, who: Any, given: str = "") -> str:
        """For the log: the whole ID, so one search ties a log line to the ban lists and the records."""
        return self.tag(who, given, whole=True)
