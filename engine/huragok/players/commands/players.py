"""Looking people up: who's on, and who someone is."""

from __future__ import annotations

import time

from ...core.part import command
from ...servers.state import Player
from ..router import CommandCall
from ..targets import ago, plain
from .base import CommandsBase

EVERY = {"all", "every", "everywhere"}  # !players all: every server's


class PlayerCommands(CommandsBase):
    @command("players", usage="[name]", help="Who's on, with #numbers and player IDs; !players nate lists those "
             "with nate in their name; !players <server> another server's, !players all every server's, with "
             "@numbers to act on them", role="moderator")
    async def players(self, call: CommandCall) -> None:
        text = call.text.strip()
        servers = self._servers_named(call, text)
        if servers:
            text = ""
        else:
            servers = [call.server]
        found: list[tuple[str, Player]] = []
        for server in servers:
            state = self.ctx.servers.get(server)
            found += [(server, p) for p in sorted(state.players.values(), key=lambda p: p.number)] if state else []
        if text:  # a busy server has more players than one answer shows
            found = [(server, p) for server, p in found if plain(text) in plain(p.name)]
            if not found:
                await call.reply(f"Nobody on has '{text}' in their name.")
                return
        if not found:
            await call.reply("Nobody is on." if servers == [call.server] else f"Nobody is on {', '.join(servers)}.")
            return
        lines = []
        now = time.time()
        for server, p in found:
            role = self.ctx.role_of(server, p)
            tags = [t for t in ((p.team or "").lower(), f"on {ago(now - p.joined)}", role if role != "player" else "",
                                "muted" if p.muted else "") if t]
            lines.append(call.tag(p) + (f": {', '.join(tags)}" if tags else ""))
        limit = int(self.config.get("max_reply_lines", 8))
        if len(lines) > limit and call.channel != "discord":  # a limit for game chat: Discord takes it whole
            lines = lines[:limit - 1] + [f"…and {len(lines) - limit + 1} more: {call.prefix}players <name> narrows it."]
        await call.reply("\n".join(lines))

    def _servers_named(self, call: CommandCall, text: str) -> list[str]:
        """The servers `text` means: "all" for every one, or one's name in any case; [] when it's a name to look for.
        Someone on another server is listed with an @number (the caller's own), so they can be acted on from here."""
        if not text:
            return []
        if text.casefold() in EVERY:
            return [call.server, *(server for server in self.ctx.servers if server != call.server)]
        return [server for server in self.ctx.servers if server.casefold() == text.casefold()]
