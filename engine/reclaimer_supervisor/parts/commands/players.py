"""Looking people up: who's on, and who someone is."""

from __future__ import annotations

import time

from ...chat import CommandCall
from ...part import command
from .base import PLAYER_ID, CommandsBase


class PlayerCommands(CommandsBase):
    @command("players", help="Who's on, with #numbers and player IDs", role="moderator")
    async def players(self, call: CommandCall) -> None:
        players = sorted(call.state.players.values(), key=lambda p: p.number)
        if not players:
            await call.reply("Nobody is on.")
            return
        lines = []
        for p in players:
            role = self.ctx.role_of(call.server, p)
            tags = [t for t in (role if role != "player" else "", "muted" if p.muted else "") if t]
            lines.append(f"#{p.number} {p.name} {p.short_id}" + (f" ({', '.join(tags)})" if tags else ""))
        await self._reply_text(call, "\n".join(lines))

    @command("whois", usage="<player | player ID>", help="A player's other names, visits and past actions",
             role="moderator")
    async def whois(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}whois <player | player ID>")
            return
        ref = call.text
        if PLAYER_ID.fullmatch(ref):
            player_id = ref.lower()
        else:
            player = await call.find_player(ref)
            if player is None:
                return
            player_id = player.id
        record = self.ctx.players.get(player_id)
        if record is None:
            await call.reply(f"{player_id[:8]}: never seen here.")
            return
        actions = self.ctx.db.query(
            "SELECT kind, by, reason, time FROM actions WHERE target_id = ? AND dry_run = 0 ORDER BY time DESC LIMIT 3",
            (player_id,))
        since = time.strftime("%Y-%m-%d", time.gmtime(record.first_seen))
        lines = [f"{record.last_name} ({record.id[:8]}): {record.visits} visits since {since}"]
        if len(record.names) > 1:
            lines.append("Also called: " + ", ".join(record.names[1:6]))
        for row in actions:
            when = time.strftime("%m-%d", time.gmtime(row["time"]))
            lines.append(f"{when} {row['kind']} by {row['by']}" + (f": {row['reason']}" if row["reason"] else ""))
        await self._reply_text(call, "\n".join(lines))
