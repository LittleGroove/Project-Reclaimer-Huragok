"""self.ctx: what each part uses to see and act on the servers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from .moderation import ModerationView
from .state import Player, ServerState
from .storage import PartStore, PlayerRecord

if TYPE_CHECKING:
    from .core import Supervisor
    from .manager import Lifecycle
    from .permissions import Permissions
    from .rcon import RconClient


class PlayerHistory:
    """Every player the supervisor has seen, on any server on this machine."""

    def __init__(self, core: Supervisor) -> None:
        self._storage = core.storage

    def get(self, player_id: str) -> PlayerRecord | None:
        return self._storage.player(player_id)

    def find(self, text: str, limit: int = 10) -> list[PlayerRecord]:
        return self._storage.find_players(text, limit)


class PartContext:
    def __init__(self, core: Supervisor, *, name: str, config: dict[str, Any], dry_run: bool,
                 acts_as: str) -> None:
        self._core = core
        self.name = name
        self.config = config
        self.dry_run = dry_run
        self.acts_as = acts_as  # what players read: "muted by AutoMod"
        self.log = logging.getLogger(f"part.{name}")
        self.db = PartStore(core.storage, name)
        self.players = PlayerHistory(core)
        self.home = core.settings.home  # the supervisor's folder: settings.toml, commands.toml, automod.txt
        self.moderation = ModerationView(core.moderation, source=name, by=acts_as, dry_run=dry_run)

    # What's going on ------------------------------------------------------------

    @property
    def servers(self) -> Mapping[str, ServerState]:
        """Each server's live picture, by name."""
        return self._core.states

    def server(self, name: str) -> ServerState:
        return self._core.states[name]

    def plays_kind(self, server: str) -> str:
        """What kind of thing the server plays: playlist, map, variant, campaign, odst or default ("" if unknown)."""
        try:
            return self._core.settings.server(server).plays_kind
        except KeyError:
            return ""

    def role_of(self, server: str, player: Player) -> str:
        return self._core.roles.role_of(self._core.states[server], player)

    @property
    def permissions(self) -> Permissions:
        """commands.toml as it is in force now (it reloads when edited)."""
        return self._core.settings.permissions

    def reload_permissions(self) -> None:
        """commands.toml was just changed from here: in force now, not at the next look a couple of seconds on."""
        self._core.check_files(force=True)

    # Talking ----------------------------------------------------------------------

    async def say(self, server: str, text: str) -> None:
        """A [Server] line everyone on the server reads."""
        await self._core.clients[server].command("say", text)

    async def tell(self, server: str, player: Player | str, text: str) -> None:
        """A [Server] line only this player reads."""
        ref = player.id if isinstance(player, Player) else player
        await self._core.clients[server].command("tell", ref, text)

    def emit(self, name: str, server: str | None = None, **data: Any) -> None:
        """An event for the other parts, always named '<part>.<name>'."""
        if not name.startswith(self.name + "."):
            name = f"{self.name}.{name}"
        self._core.bus.emit(name, server, **data)

    # Acting ------------------------------------------------------------------------

    @property
    def lifecycle(self) -> Lifecycle:
        """Restart a server: now, after this game, or when it's empty."""
        return self._core.lifecycle

    def rcon(self, server: str) -> RconClient:
        """The raw console, for what the rest doesn't cover."""
        return self._core.clients[server]
