"""Acting on players: warn, mute, kick, ban and their undoing. Every one is recorded with who and why.

Each declares the player it names (`player=`), so the router finds them first with every guard of targets.py: a
name with spaces, a name several players have (the caller picks by #number or @number), someone of the caller's
role or above.
"""

from __future__ import annotations

from ...core.part import command
from ..router import CommandCall, unquote
from .base import CommandsBase


class ModerationCommands(CommandsBase):
    @command("warn", usage="<player> [reason]", help="A private warning, recorded", role="moderator", player="on",
             acts=True)
    async def warn(self, call: CommandCall) -> None:
        assert call.target is not None
        action = await self.ctx.moderation.warn(call.server, call.target, unquote(call.rest), by=call.player.name)
        await self._done(call, action)

    @command("mute", usage="<player> [time] [reason]",
             help="Nobody hears their voice or reads their chat, e.g. !mute Bravo 30m spam; someone who left is "
                  "muted as they join", role="moderator", player="seen", acts=True, follows="time")
    async def mute(self, call: CommandCall) -> None:
        target = call.target
        assert target is not None
        duration, rest, _ = self._split_time(call.rest)
        reason = unquote(rest)
        if target.number:
            action = await self.ctx.moderation.mute(call.server, target, duration, reason, by=call.player.name)
            await self._done(call, action)
            return
        # Not on this server: the one picked, by player ID (the name they left with may be someone else's by now).
        # Moderation commands are each server's own: someone on another server is that server's to mute. On none:
        # on Vergil's list, muted as they next join (any server, with [automod] across_servers).
        across = bool(self.ctx.part_option("automod", "across_servers", True))
        elsewhere = next((server for server, state in self.ctx.servers.items()
                          if server != call.server and state.player_by_id(target.id)), None)
        if elsewhere:
            await call.reply(f"{target.name} is on {elsewhere}, not this server.")
            return
        if not self.ctx.running("automod"):
            await call.reply(f"{target.name} isn't on, and the server only mutes players who are: Vergil keeps mutes "
                             "for players who left, and automod is off.")
            return
        action = await self.ctx.moderation.mute(call.server, target, duration, reason, by=call.player.name,
                                                pending=True)
        if action.dry_run:
            await self._done(call, action)
            return
        await call.reply(f"{target.name} isn't on now: muted as they next join" + (", on any server" if across else
                                                                                  " this server")
                         + (f", for {duration} from now." if duration else ", until someone unmutes them."))

    @command("unmute", usage="<player | player ID>", help="Lift a mute, also for someone who left", role="moderator",
             player="seen", start=True, words_after=False)
    async def unmute(self, call: CommandCall) -> None:
        # Lifting is harmless, so a name's start will do; someone who left is unmuted by ID, as the server can.
        # Someone on another server is that server's, as for !mute: unmuted here, they'd stay muted there.
        assert call.target is not None
        if not call.target.number:
            elsewhere = next((server for server, state in self.ctx.servers.items()
                              if server != call.server and state.player_by_id(call.target.id)), None)
            if elsewhere:
                await call.reply(f"{call.target.name} is on {elsewhere}, not this server.")
                return
        await self._done(call, await self.ctx.moderation.unmute(call.server, call.target, by=call.player.name))

    @command("kick", usage="<player> [reason]", help="Remove them; the server keeps them out 10, 30, 60, then 120 minutes",
             role="moderator", player="on", acts=True)
    async def kick(self, call: CommandCall) -> None:
        assert call.target is not None
        action = await self.ctx.moderation.kick(call.server, call.target, unquote(call.rest), by=call.player.name)
        await self._done(call, action)

    @command("ban", usage="<player | player ID> [time | perm] [reason]",
             help="Ban their ID, address and device; no time means for good, e.g. !ban Bravo 7d cheating",
             role="admin", player="seen", acts=True, follows="time")
    async def ban(self, call: CommandCall) -> None:
        # Offline bans work by ID, and a name Vergil has seen finds theirs; rank counts either way.
        target = call.target
        assert target is not None
        role = self.ctx.highest_role(target)
        duration, rest, _ = self._split_time(call.rest)
        action = await self.ctx.moderation.ban(call.server, target, duration, unquote(rest), by=call.player.name)
        await self._done(call, action)
        if action.ok and not action.dry_run and role in ("member", "moderator", "admin"):  # roles.py takes it
            await call.reply(f"{target.name}'s {role} role goes with the ban.")
