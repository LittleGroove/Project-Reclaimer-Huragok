"""Respawning a stuck player (the host's call, 27 October): !respawn for yourself, !respawn <player> for admins.

The server's console has no kill or respawn command, but an admin's move to another team respawns the player at once
(the host has seen it with the game's own admin menu). So !respawn moves them to another team and, once the server
says it has, straight back: they come back to life on their own team. That needs a team game with a round being
played: free-for-all has no other team to move to, and the server won't move a team's last player during a round (it
says so, and the caller reads why).

The two moves are a short exchange with the server (each move's outcome comes back as a `team` control event), so the
command only sends the first and the rest follows the answers, one respawn per server at a time. A move the server
doesn't answer within a few seconds is taken as done. Players read who respawned and why once they're back.

Not yet tried with players from the console (plans/respawn-command.md): whether the move back respawns them a second
time, and whether a move counts as a death. `!unstuck` is to be a host's own name for it (the pseudoname plan).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ...core.events import Event
from ...core.part import command, every, on
from ...lore import callout
from ...rcon import RconError
from ...servers.state import Player, ServerState
from ..roles import Roles
from ..router import CommandCall, unquote
from .base import CommandsBase

if TYPE_CHECKING:
    from ...core.context import PartContext

OWN_GAP = 60.0  # seconds between a player's own respawns (admins' aren't held): a respawn can also dodge a death
ANSWER_SECONDS = 6.0  # how long a move waits for the server's answer before it's taken as done
OTHERS = "admin"  # the role that respawns someone else
OPPOSITE = {"red": "blue", "blue": "red"}


@dataclass
class Respawn:
    """One under way on a server: moved away (step 1), then back (step 2), each waiting for the server's answer."""

    player: Player  # as they were when asked: ID, name, team
    home: str  # their own team, as the console takes it ("red")
    away: str  # the team they're moved to for a moment
    by: str  # who asked: the player themselves, or an admin
    own: bool
    reason: str
    call: CommandCall
    step: int = 1
    sent: float = field(default_factory=time.monotonic)


def _away(state: ServerState, player: Player, home: str) -> str:
    """Another team to move them to for a moment: the smallest one being played, else red's or blue's opposite."""
    sizes: dict[str, int] = {}
    for other in state.players.values():
        team = (other.team or "").lower()
        if team and team != home:
            sizes[team] = sizes.get(team, 0) + 1
    if sizes:
        return min(sizes, key=lambda team: (sizes[team], team))
    return OPPOSITE.get(home, "red")


class RespawnCommands(CommandsBase):
    def __init__(self, ctx: PartContext) -> None:
        super().__init__(ctx)
        self._respawns: dict[str, Respawn] = {}  # server -> the respawn under way there
        self._respawned: dict[str, float] = {}  # player ID -> when they last respawned themselves

    @command("respawn", usage="[player] [reason]",
             help="Stuck? Respawn yourself (a team game, during a round); admins name a player to respawn them",
             role="player")
    async def respawn(self, call: CommandCall) -> None:
        text = call.text.strip()
        state = self.ctx.servers.get(call.server)
        if state is None:
            return
        target: Player | None = call.player
        reason = unquote(text.split())
        if text and Roles.allows(call.role, OTHERS):  # an admin's words name a player (anyone else's: a reason)
            target, rest = await call.player_and_rest(text, acts=True)
            if target is None:
                return
            reason = unquote(rest)
        assert target is not None
        own = target.id == call.player.id
        player = state.player_by_id(target.id)
        if player is None:
            await call.reply(f"{target.name} isn't on this server now.")
            return
        who = "you" if own else player.name
        now = time.monotonic()
        if own and not Roles.allows(call.role, OTHERS):
            wait = OWN_GAP - (now - self._respawned.get(player.id, -OWN_GAP))
            if wait > 0:
                await call.reply(f"You can respawn again in {int(wait) + 1} s.")
                return
        if state.phase != "playing":
            await call.reply(f"There's no round being played to respawn {who} in.")
            return
        if not player.team:
            await call.reply("This game has no teams, so there's no way to respawn anyone: the server has no kill "
                             "command, and a respawn is a move to another team and back.")
            return
        under_way = self._respawns.get(call.server)
        if under_way is not None and now - under_way.sent < 2 * ANSWER_SECONDS:
            await call.reply("Another respawn is under way here: try again in a moment.")
            return
        home = player.team.lower()
        respawn = Respawn(player, home, _away(state, player, home), call.player.name, own, reason, call)
        if not await self._respawn_move(call.server, respawn, respawn.away):
            return
        self._respawns[call.server] = respawn
        if own:
            self._respawned = {pid: t for pid, t in self._respawned.items() if now - t < OWN_GAP}
            self._respawned[player.id] = now
        self.log.info("[%s] %s asked to respawn %s: moving them to %s and back to %s", call.server, call.player.name,
                      "themselves" if own else player.name, respawn.away, home)

    async def _respawn_move(self, server: str, respawn: Respawn, team: str) -> bool:
        """`team <ID> <team>` sent; False after telling the caller why not."""
        try:
            reply = await self.ctx.rcon(server).command("team", respawn.player.id, team, by=respawn.by)
        except RconError as error:
            reply_text, ok = str(error), False
        else:
            reply_text, ok = reply.text, reply.ok
        if ok:
            respawn.sent = time.monotonic()
            return True
        if respawn.step == 1:
            await respawn.call.reply(f"Couldn't respawn {'you' if respawn.own else respawn.player.name}: {reply_text}")
        else:
            await self._respawn_left_away(server, respawn, reply_text)
        return False

    # The server's answers ----------------------------------------------------------------------

    @on("rcon.control")
    async def respawn_answer(self, event: Event) -> None:
        """A move's outcome: the first one done, the move back; the move back done, everyone reads it."""
        server = event.server or ""
        respawn = self._respawns.get(server)
        if event.get("action") != "team" or respawn is None:
            return
        why = " ".join(str(event.get("text") or "no reason given").split())[:200]
        if not event.get("ok", True):
            self._respawns.pop(server, None)
            if respawn.step == 1:
                await respawn.call.reply(f"Couldn't respawn {'you' if respawn.own else respawn.player.name}: {why}")
            else:
                await self._respawn_left_away(server, respawn, why)
            return
        await self._respawn_next(server, respawn)

    @every(seconds=2)
    async def respawns_unanswered(self) -> None:
        """A move the server didn't answer in time is taken as done."""
        now = time.monotonic()
        for server, respawn in list(self._respawns.items()):
            if now - respawn.sent >= ANSWER_SECONDS:
                self.log.info("[%s] no answer to moving %s to %s: taken as done", server, respawn.player.name,
                              respawn.away if respawn.step == 1 else respawn.home)
                await self._respawn_next(server, respawn)

    async def _respawn_next(self, server: str, respawn: Respawn) -> None:
        if respawn.step == 1:
            respawn.step = 2
            if not await self._respawn_move(server, respawn, respawn.home):
                self._respawns.pop(server, None)
            return
        self._respawns.pop(server, None)
        self.log.info("[%s] %s respawned (%s)", server, respawn.player.name,
                      "their own !respawn" if respawn.own else f"by {respawn.by}")
        team = respawn.home.capitalize()
        why = f": {respawn.reason}" if respawn.reason else ""
        line = callout("respawn", f"{respawn.player.name} respawned on {team} team{why}." if respawn.own else
                       f"{respawn.by} respawned {respawn.player.name} on {team} team{why}.")
        try:
            await self.ctx.say(server, line)
        except RconError:
            await respawn.call.reply(line)

    async def _respawn_left_away(self, server: str, respawn: Respawn, why: str) -> None:
        """The move back was refused: they're on the other team now, and the caller reads how to put them back."""
        self.log.warning("[%s] %s was left on %s: the move back to %s was refused: %s", server, respawn.player.name,
                         respawn.away, respawn.home, why)
        await respawn.call.reply(f"{'You' if respawn.own else respawn.player.name} respawned, but on "
                                 f"{respawn.away.capitalize()} team: the server wouldn't move "
                                 f"{'you' if respawn.own else 'them'} back ({why}).")
