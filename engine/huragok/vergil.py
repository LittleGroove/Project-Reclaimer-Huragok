"""Vergil: watches over the players.

The subroutine Dr. Endesha wrote to watch over his daughter Sadie, through New Mombasa's cameras and microphones
(lore.py). Here it watches over the players on every server. It is built on Huragok (the settings, the event bus,
the database) and on the Superintendent (the servers, their consoles and live picture), and owns:

    the ledger (players/ledger.py): every warn, mute, kick and ban, recorded with who and why
    roles (players/roles.py), and commands.toml (core/commandsfile.py) reloaded when edited
    the router (players/router.py): `!` commands in chat, who may use each, who they mean (players/targets.py)
    the relay (players/relay.py): the server's console for moderators elsewhere, gated by the same roles
    the outbox (players/outbox.py): each server's queue of what's passed on to it from another (a ban, an admin)
    its parts: automod (players/automod), the in-game commands (players/commands), interval messages
    (players/messages)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .core.background import background
from .core.commandsfile import Permissions, PermissionsError, reload_if_changed
from .core.events import Event
from .core.serverfolders import ServerConfig
from .core.storage import PlayerHistory
from .players.ledger import Moderation, ModerationView
from .players.outbox import Outbox
from .players.relay import Relay
from .players.roles import Roles
from .players.router import CommandRouter
from .servers.state import Player
from .superintendent import Superintendent, SuperintendentContext

if TYPE_CHECKING:
    from .huragok import Huragok

log = logging.getLogger(__name__)

FILE_CHECK_SECONDS = 2.0  # commands.toml is looked at this often


class Vergil:
    def __init__(self, huragok: Huragok, superintendent: Superintendent) -> None:
        self.huragok = huragok
        self.superintendent = superintendent
        settings, bus = huragok.settings, huragok.bus
        self.settings = settings
        self.bus = bus
        self.moderation = Moderation(superintendent.clients, superintendent.states, huragok.storage, bus,
                                     dry_run=settings.dry_run)
        self.roles = Roles(settings)
        self.commands = CommandRouter(settings, bus, superintendent.states, superintendent.clients, self.roles,
                                      PlayerHistory(huragok.storage))
        self.relay = Relay(settings, superintendent.clients, superintendent.states, bus)
        self.outbox = Outbox(superintendent.states)
        bus.subscribe("rcon.join", self._remember_player)
        superintendent.listen(self)

    # Servers coming and going -------------------------------------------------------------------------------

    def server_added(self, server: ServerConfig, *, running: bool) -> None:
        if running:
            background(self.relay.add_server(server), f"[{server.name}] its relay", log)

    def server_removed(self, name: str) -> None:
        self.relay.remove_server(name)

    def problems(self) -> list[str]:
        return list(self.relay.problems)

    # The players --------------------------------------------------------------------------------------------

    def _remember_player(self, event: Event) -> None:
        if event.get("id"):
            self.huragok.storage.record_join(str(event["id"]).lower(), str(event.get("name", "")), event.get("ip"),
                                             event.time)

    async def ban_everywhere(self, server: str, target: str, by: str) -> tuple[bool, str]:
        """One server's ban onto Huragok's own list, so every server has it (the window's B → E, `banall`)."""
        runner = self.huragok.runners.get("commands")
        if runner is None or not runner.enabled:
            return False, "the commands part isn't running, and it keeps Huragok's ban list"
        state = self.superintendent.states.get(server)
        if state is None or not state.connected:
            return False, f"{server} isn't connected: its ban list can't be read"
        return await runner.part.ban_everywhere(server, target, by or "the Superintendent")

    # commands.toml ----------------------------------------------------------------------------------------------

    def check_files(self, *, force: bool = False) -> None:
        """commands.toml reloads when edited; with force, at once (Huragok itself just changed it)."""
        try:
            fresh = reload_if_changed(self.settings.permissions, force=force)
        except PermissionsError as error:
            log.error("commands.toml not reloaded, keeping what was in force: %s", error)
            return
        if fresh is not None:
            self.settings.permissions = fresh
            log.info("commands.toml reloaded")
            for line in fresh.unknown:
                log.warning(line)
            self.bus.emit("supervisor.commands_reloaded")

    async def _watch_files(self) -> None:
        while True:
            await asyncio.sleep(FILE_CHECK_SECONDS)
            self.check_files()

    # Running ----------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.huragok.spawn("file checks", self._watch_files)
        self.huragok.spawn("console relay", self.relay.run)

    def context(self, **options: Any) -> VergilContext:
        return VergilContext(self.huragok, self.superintendent, self, **options)


class VergilContext(SuperintendentContext):
    """self.ctx for Vergil's parts: everything the Superintendent's parts have, and the players."""

    def __init__(self, huragok: Huragok, superintendent: Superintendent, vergil: Vergil, **options: Any) -> None:
        super().__init__(huragok, superintendent, **options)
        self._vergil = vergil
        self.players = PlayerHistory(huragok.storage)  # everyone seen on any server here
        self.moderation = ModerationView(vergil.moderation, source=self.name, by=self.acts_as, dry_run=self.dry_run)
        self.outbox = vergil.outbox  # each server's queue of what's passed on to it (players/outbox.py)

    def role_of(self, server: str, player: Player) -> str:
        return self._vergil.roles.role_of(self._superintendent.states[server], player)

    def highest_role(self, player: Player) -> str:
        """Their role on whichever server ranks them highest: what protects them from a ! command."""
        return self._vergil.roles.highest_role(list(self._superintendent.states.values()), player)

    @property
    def permissions(self) -> Permissions:
        """commands.toml as it is in force now (it reloads when edited)."""
        return self._vergil.settings.permissions

    def reload_permissions(self) -> None:
        """commands.toml was just changed from here: in force now, not at the next look a couple of seconds on."""
        self._vergil.check_files(force=True)
