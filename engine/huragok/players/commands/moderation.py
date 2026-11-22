"""Acting on players: warn, mute, kick, ban and their undoing. Every one is recorded with who and why.

Each declares the player it names (`player=`), so the router finds them first with every guard of targets.py: a
name with spaces, a name several players have (the caller picks by #number or @number), someone of the caller's
role or above.

With [automod] across_servers on (the default), a mute, kick or ban is every server's, so it's given wherever the
player is: on this server, on another (it's sent there), or on none (as they next join, or by their player ID). The
host's call, 21 November 2026: "if they get muted one place, they get muted everywhere for that timer" (up to then,
someone on another server was that server's to act on). With it off, each server's moderation is its own, and a
command acts on players on the server it's typed on.
"""

from __future__ import annotations

from typing import Any

from ...core.commandsfile import PROBATION, ROLES
from ...core.part import command
from ...core.text import TIME, describe_time, seconds_of
from ...servers.state import Player
from ..ledger import kick_reason
from ..roles import role_name
from ..router import CommandCall, unquote
from .base import CommandsBase


class ModerationCommands(CommandsBase):
    def _elsewhere(self, call: CommandCall, target: Player) -> tuple[str, Player] | None:
        """The other server this player is on now, as that server sees them, or None."""
        for server, state in self.ctx.servers.items():
            if server != call.server and (there := state.player_by_id(target.id)):
                return server, there
        return None

    async def _not_here(self, call: CommandCall, target: Player, where: str) -> None:
        """With across_servers off, someone on another server is that server's to act on."""
        await call.reply(f"{call.tag(target)} is on another server: with [automod] across_servers off, each "
                         "server's moderation is its own.")

    @command("warn", usage="<player> [reason]", help="A private warning, recorded", role="moderator", player="on",
             acts=True)
    async def warn(self, call: CommandCall) -> None:
        assert call.target is not None
        action = await self.ctx.moderation.warn(call.server, call.target, unquote(call.rest), by=call.player.name)
        await self._done(call, action)

    @command("mute", usage="<player> [time] [reason]",
             help="Nobody hears their voice or reads their chat, on every server, e.g. !mute Bravo 30m spam; someone "
                  "who left is muted as they join", role="moderator", player="seen", acts=True, follows="time")
    async def mute(self, call: CommandCall) -> None:
        target = call.target
        assert target is not None
        duration, rest, _ = self._split_time(call.rest)
        if why := self._no_time(duration):
            await call.reply(why)
            return
        reason = unquote(rest)
        if target.number:
            action = await self.ctx.moderation.mute(call.server, target, duration, reason, by=call.player.name)
            await self._done(call, action)
            return
        # Not on this server: the one picked, by player ID (the name they left with may be someone else's by now).
        if found := self._elsewhere(call, target):
            where, there = found
            if not self._across():
                await self._not_here(call, target, where)
                return
            action = await self.ctx.moderation.mute(where, there, duration, reason, by=call.player.name)
            await self._done(call, action)
            return
        if not self.ctx.running("automod"):
            await call.reply(f"{call.tag(target)} isn't on, and the server only mutes players who are: Vergil keeps "
                             "mutes for players who left, and automod is off.")
            return
        action = await self.ctx.moderation.mute(call.server, target, duration, reason, by=call.player.name,
                                                pending=True)
        if action.dry_run:
            await self._done(call, action)
            return
        await call.reply(f"{call.tag(target)} isn't on now: muted on {self.reach_word()} as they next join"
                         + (f", for {describe_time(duration)} from now." if duration else ", until someone unmutes them."))

    @command("unmute", usage="<player | player ID>", help="Lift a mute, on every server, also for someone who left",
             role="moderator", player="seen", start=True, words_after=False)
    async def unmute(self, call: CommandCall) -> None:
        # Lifting is harmless, so a name's start will do; someone who left is unmuted by ID, as the server can.
        target = call.target
        assert target is not None
        where = call.server
        if not target.number and (found := self._elsewhere(call, target)):
            if not self._across():
                await self._not_here(call, target, found[0])
                return
            where, target = found  # unmuted where they are: Vergil's list lets go of it everywhere
        if why := self.lift_outranked(call, target.id, "mute", "mute"):
            await call.reply(f"Not unmuted: {why}")
            return
        await self._done(call, await self.ctx.moderation.unmute(where, target, by=call.player.name))

    @command("kick", usage="<player> [time] [reason]",
             help="Remove them from every server: kept out 10, 30, 60, then 120 minutes, or for the time you give, told "
                  "how long, e.g. !kick Bravo 7d cheating; perm is a ban", role="moderator", player="any", acts=True,
             follows="time")
    async def kick(self, call: CommandCall) -> None:
        target = call.target
        assert target is not None
        duration, rest, forever = self._split_time(call.rest)
        if forever and call.rest[0].lower() == "never":
            # "!kick Bravo never camp the spawn": the start of a reason, not "for good" (perm, permanent or forever
            # are); it banned for good, roles and all (audit 2026-11, 5.6)
            duration, rest, forever = None, call.rest, False
        if why := self._no_time(duration):
            await call.reply(why)
            return
        where, found = call.server, target
        if not target.number:
            elsewhere = self._elsewhere(call, target)
            if not self._across():
                if elsewhere:
                    await self._not_here(call, target, elsewhere[0])
                else:
                    await call.reply(f"{call.tag(target)} isn't on this server now.")
                return
            if elsewhere:
                where, found = elsewhere
            elif not duration and not forever:  # the server's plain kick needs them on
                await call.reply(f"{call.tag(target)} isn't on any server now, so there's nobody to kick: a time keeps them "
                                 f"off every server as they join (the same {call.prefix}kick, with a time after them: 30m).")
                return
        if forever:  # a kick for good is a ban, and takes the rank a ban does
            why = await self.ctx.run_as("ban", target, rest, server=call.server, player=call.player, role=call.role, tell=call._tell,
                                        picked=call.picked.get(target.id, ""))
            if why:
                await call.reply(f"A kick for good is a ban: {why}")
            return
        if duration:
            await self._kick_for(call, found, duration, unquote(rest), where)
            return
        await self._done(call, await self.ctx.moderation.kick(where, found, unquote(rest), by=call.player.name))

    async def _kick_for(self, call: CommandCall, target: Any, duration: str, reason: str, where: str = "") -> None:
        """Off every server for this long. A moderator's has a limit ([automod] moderators_kick_at_most): a long one is a ban in all but name."""
        if too_long := self.kick_too_long(call.role, duration):
            await call.reply(too_long)
            return
        action = await self.ctx.moderation.ban(where or call.server, target, duration, reason, told=kick_reason(duration, reason, self._across()), by=call.player.name)
        if action.ok and not action.dry_run:
            await call.reply(f"{self.acted_on(call, action)} is kicked off {self.reach_word()} for {describe_time(duration)}: the server tells "
                             "them how long is left each time they try to join.")
        else:
            await self._done(call, action)

    @command("ban", usage="<player | player ID> [time | perm] [reason]",
             help="Ban their ID, address and device from every server; no time means for good, and a time makes it a kick that long, e.g. !ban Bravo 7d cheating",
             role="admin", player="seen", acts=True, follows="time")
    async def ban(self, call: CommandCall) -> None:
        # Offline bans work by ID, and a name Vergil has seen finds theirs; rank counts either way.
        target = call.target
        assert target is not None
        role = self.ctx.highest_role(target)
        duration, rest, _ = self._split_time(call.rest)
        if why := self._no_time(duration):
            await call.reply(why)
            return
        action = await self.ctx.moderation.ban(call.server, target, duration, unquote(rest), by=call.player.name)
        await self._done(call, action)
        if action.ok and not action.dry_run and not duration and role in ("member", PROBATION, "moderator", "admin"):  # roles.py takes it
            await call.reply(f"Their {role_name(role)} role goes with the ban.")
