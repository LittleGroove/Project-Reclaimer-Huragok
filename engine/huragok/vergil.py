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
from .core.settings import RelayUser
from .players.relay import Relay
from .players.roles import Roles
from .players.router import CommandRouter
from .servers.state import Player
from .superintendent import Superintendent, SuperintendentContext

if TYPE_CHECKING:
    from .huragok import Huragok

log = logging.getLogger(__name__)

FILE_CHECK_SECONDS = 2.0  # commands.toml is looked at this often
UNNAMED_FOR = 3600.0  # a join with no name (0.10) waits this long at most for its name to be recorded with


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
        self.last_visit: dict[str, float | None] = {}  # player ID -> when they were here before this visit; None: first time
        self.unnamed: dict[str, tuple[str | None, float]] = {}  # player ID -> (address, when) of a join with no name
        bus.subscribe("rcon.join", self._remember_player)
        bus.subscribe("state.named", self._remember_name)
        bus.subscribe("rcon.leave", self._remember_name)
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
        if not event.get("id"):
            return
        player_id = str(event["id"]).lower()
        name = str(event.get("name") or "")
        if not name:  # 0.10 names nobody as they join: the live picture may know them already
            state = self.superintendent.states.get(event.server or "")
            player = state.player_by_id(player_id) if state is not None else None
            name = player.name if player is not None else ""
        if name:
            self.unnamed.pop(player_id, None)
            self.last_visit[player_id] = self.huragok.storage.record_join(player_id, name, event.get("ip"), event.time)
            return
        # Recorded once their name is known (state.named, or their leave naming them): a blank name is no name to keep.
        # When they were here before is known now.
        rows = self.huragok.storage.query("SELECT last_seen FROM players WHERE id = ?", (player_id,))
        self.last_visit[player_id] = float(rows[0]["last_seen"]) if rows else None
        self.unnamed = {key: value for key, value in self.unnamed.items() if event.time - value[1] < UNNAMED_FOR}
        self.unnamed[player_id] = (event.get("ip"), event.time)

    def _remember_name(self, event: Event) -> None:
        """A join that named nobody (0.10), recorded now its name is known: state.named, or a leave that names them.
        A leave that doesn't, ends the wait."""
        player_id = str(event.get("id") or "").lower()
        waiting = self.unnamed.pop(player_id, None)
        name = str(event.get("name") or "")
        if waiting is not None and name:
            self.huragok.storage.record_join(player_id, name, waiting[0], waiting[1])

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

    def last_visit(self, player_id: str) -> float | None:
        """When this player was here before the visit they just began; None the first time, or when it isn't known."""
        return self._vergil.last_visit.get(player_id.lower())

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

    @property
    def probation(self) -> set[str]:
        """Members on probation as moderators (the commands part keeps it: players/commands/requests.py)."""
        return self._vergil.roles.probation

    def on_request(self, handler: Any) -> None:
        """Where a probationary moderator's commands go instead of running (the router's on_request)."""
        self._vergil.commands.on_request = handler

    def game_commands(self) -> list[Any]:
        """Every in-game command and the role it needs: what Discord offers as slash commands."""
        return self._vergil.commands.every_command()

    async def run_line(self, name: str, text: str, **kwargs: Any) -> str | None:
        """A command typed somewhere other than in game, by someone with a rank."""
        return await self._vergil.commands.run_line(name, text, **kwargs)

    @property
    def command_prefix(self) -> str:
        return self._vergil.commands.prefix

    async def console_line(self, server: str, who: str, role: str, line: str) -> tuple[bool, str]:
        """A console line typed somewhere other than the server's console, by someone with a rank: everything the relay checks, and the reply as that rank may read it."""
        config = next((one for one in self._vergil.settings.servers if one.name == server), None)
        if config is None:
            return False, f"There's no server {server}."
        worked, text, _ = await self._vergil.relay.run_line(RelayUser(who, "", role), config, line)
        return worked, text

    def refuses(self, name: str, target: Player | None, **kwargs: Any) -> str:
        """Why a command would say no, asked before anyone fills in a form."""
        return self._vergil.commands.refuses(name, target, **kwargs)

    def _automod(self) -> Any:
        """Vergil's word list part, if it's on: whoever has !strike."""
        registered = self._vergil.commands.commands.get("strike")
        return registered.runner.part if registered is not None and registered.runner is not None else None

    def strike_rules(self) -> list[str]:
        """The word list's rules, which a strike can be given on: not those that only report."""
        part = self._automod()
        return [str(name) for name in part.strike_rules()] if part is not None else []

    def word_lists(self) -> list[tuple[str, str]]:
        """Every line of the word list by its name, with what its strikes do in a few words: for Discord to offer as
        it's typed."""
        part = self._automod()
        return list(part.word_lists()) if part is not None else []

    def listed_words(self) -> list[tuple[str, str]]:
        """Every word on the word list (`-word` too), with the line it's on."""
        part = self._automod()
        return [(word, rule.name) for rule in getattr(part, "rules", []) for word in [*rule.words, *(f"-{w}" for w in rule.allow)]]

    def banned_names(self) -> list[str]:
        """names.txt's names, the latest first."""
        part = self._automod()
        return [name.text for name in reversed(getattr(part, "names", []))]

    def name_unfit(self, name: str) -> bool:
        """A name not to show to everyone (a leaderboard, 0.6.1): one names.txt bans, or with a word of the word list
        in it, whether or not check_names kicks for those; a word of a line that only reports counts until the staff
        allow the name. False with Vergil off: there's no list to go by."""
        part = self._automod()
        if part is None:
            return False
        if part.name_matcher.find(name):
            return True
        hits = part.word_names.find(name)
        return any(not hit.rule.reports for hit in hits) or (bool(hits) and not part.name_allowed(name))

    def allow_name(self, name: str, by: str, server: str | None = None) -> bool:
        """The staff's answer to a name Vergil asked them about (a word of a line that only reports): it's fine. Whether
        it's new: False when it was allowed already, or Vergil's off."""
        part = self._automod()
        return bool(part.allow_name(name, by, server)) if part is not None else False

    def strike_removes(self, player_id: str, rule: str, server: str | None = None) -> bool:
        """Whether a strike on this rule would take them off the server now (a kick, a kick before a mute, the ban):
        what reaches every server."""
        part = self._automod()
        found = getattr(part, "by_name", {}).get(rule)
        if found is None:
            return False
        previous = part.active_strikes(player_id, found, dry=bool(server) and part.mode(server) == "dry_run")
        return found.step_for(previous).removes

    def next_strike(self, player_id: str, rule: str, server: str | None = None) -> str:
        """What a strike on this rule would do to them now, in words: "" if that can't be said. On a server in dry run,
        its strikes are counted apart, as the strike there would count them."""
        part = self._automod()
        found = getattr(part, "by_name", {}).get(rule)
        if found is None:
            return ""
        previous = part.active_strikes(player_id, found, dry=bool(server) and part.mode(server) == "dry_run")
        return f"Strike {previous + 1}: {found.step_for(previous).describe()}"

    def command_there(self, name: str) -> bool:
        return self._vergil.commands.command_there(name)

    def picks_through(self, caller: str) -> Any:
        return self._vergil.commands.picks_through(caller)

    def pick_of(self, caller: str, number: int) -> Any:
        return self._vergil.commands.pick_of(caller, number)

    async def ask_an_admin(self, call: Any, target: Player, words: list[str]) -> bool:
        """The command, held for an admin (or faculty) to confirm, as a probationary moderator's requests are: a
        moderator's strike whose next step is the ban for good. False when nothing takes requests (the commands part
        is off)."""
        router = self._vergil.commands
        registered = router.commands.get(call.name)
        if router.on_request is None or registered is None:
            return False
        await router.on_request(call, registered, target, words, needs="admin")
        return True

    async def run_as(self, name: str, target: Player | None, words: list[str], **kwargs: Any) -> str | None:
        """A command run for someone, on a server, as if they'd typed it there (a confirmed request, a button in Discord)."""
        return await self._vergil.commands.run_as(name, target, words, **kwargs)
