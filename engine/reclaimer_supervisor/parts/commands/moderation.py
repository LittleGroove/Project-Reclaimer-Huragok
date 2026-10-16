"""Acting on players: warn, mute, kick, ban and their undoing. Every one is recorded with who and why."""

from __future__ import annotations

from ...chat import CommandCall
from ...part import command
from ...state import Player
from .base import PLAYER_ID, CommandsBase


class ModerationCommands(CommandsBase):
    @command("warn", usage="<player> [reason]", help="A private warning, recorded", role="moderator")
    async def warn(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}warn <player> [reason]")
            return
        target = await self._target(call, call.args[0])
        if target:
            action = await self.ctx.moderation.warn(call.server, target, " ".join(call.args[1:]), by=call.player.name)
            await self._done(call, action)

    @command("mute", usage="<player> [time] [reason]",
             help="Nobody hears their voice or reads their chat, e.g. !mute Bravo 30m spam", role="moderator")
    async def mute(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}mute <player> [time like 30m] [reason]")
            return
        target = await self._target(call, call.args[0])
        if target:
            duration, rest, _ = self._split_time(call.args[1:])
            action = await self.ctx.moderation.mute(call.server, target, duration, " ".join(rest), by=call.player.name)
            await self._done(call, action)

    @command("unmute", usage="<player>", help="Lift a mute", role="moderator")
    async def unmute(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}unmute <player>")
            return
        target = await call.find_player(call.text)
        if target:
            await self._done(call, await self.ctx.moderation.unmute(call.server, target, by=call.player.name))

    @command("kick", usage="<player> [reason]", help="Remove them; the server keeps them out 10, 30, 60, then 120 minutes",
             role="moderator")
    async def kick(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}kick <player> [reason]")
            return
        target = await self._target(call, call.args[0])
        if target:
            action = await self.ctx.moderation.kick(call.server, target, " ".join(call.args[1:]), by=call.player.name)
            await self._done(call, action)

    @command("pardon", usage="<player | player ID | IP>", help="Let a kicked player back in now", role="moderator")
    async def pardon(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}pardon <name they were kicked under | player ID | IP>")
            return
        await self._done(call, await self.ctx.moderation.pardon(call.server, call.text, by=call.player.name))

    @command("ban", usage="<player | player ID> [time | perm] [reason]",
             help="Ban their ID, address and device; no time means for good, e.g. !ban Bravo 7d cheating",
             role="admin")
    async def ban(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}ban <player | player ID> [time like 7d, or perm] [reason]")
            return
        ref = call.args[0]
        target: Player | str | None
        if PLAYER_ID.fullmatch(ref):
            target = call.state.player_by_id(ref) or ref.lower()  # offline bans work by ID
        else:
            target = await call.find_player(ref)
        if target is None:
            return
        if isinstance(target, Player) and not self._outranks(call, target):
            await call.reply(f"{target.name} is a {self.ctx.role_of(call.server, target)}; you can't do that to them.")
            return
        duration, rest, _ = self._split_time(call.args[1:])
        action = await self.ctx.moderation.ban(call.server, target, duration, " ".join(rest), by=call.player.name)
        await self._done(call, action)

    @command("unban", usage="<name | player ID | IP>", help="Lift a ban with everything linked to it", role="admin")
    async def unban(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}unban <name | player ID | IP>")
            return
        await self._done(call, await self.ctx.moderation.unban(call.server, call.text, by=call.player.name))
