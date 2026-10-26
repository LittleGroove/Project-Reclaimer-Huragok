"""Looking people up: who's on, and who someone is."""

from __future__ import annotations

import time

from ...core.part import command
from ..router import CommandCall
from ..targets import ago, plain
from .base import CommandsBase


class PlayerCommands(CommandsBase):
    @command("players", usage="[name]", help="Who's on, with #numbers and player IDs; !players nate lists those "
             "with nate in their name", role="moderator")
    async def players(self, call: CommandCall) -> None:
        players = sorted(call.state.players.values(), key=lambda p: p.number)
        if call.text.strip():  # a busy server has more players than one answer shows
            players = [p for p in players if plain(call.text) in plain(p.name)]
            if not players:
                await call.reply(f"Nobody on has '{call.text.strip()}' in their name.")
                return
        if not players:
            await call.reply("Nobody is on.")
            return
        lines = []
        now = time.time()
        for p in players:
            role = self.ctx.role_of(call.server, p)
            tags = [t for t in ((p.team or "").lower(), f"on {ago(now - p.joined)}", role if role != "player" else "",
                                "muted" if p.muted else "") if t]
            lines.append(f"#{p.number} {p.name} {p.short_id} ({', '.join(tags)})")
        limit = int(self.config.get("max_reply_lines", 8))
        if len(lines) > limit:
            lines = lines[:limit - 1] + [f"…and {len(lines) - limit + 1} more: {call.prefix}players <name> narrows it."]
        await call.reply("\n".join(lines))
