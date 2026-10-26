"""What players said: every chat line a player sends on any server, kept 30 days, so a moderator who arrives late can
see why everyone's upset (!said <player>), and a line someone reported can be looked at again. The text only:
voice never reaches Huragok.
"""

from __future__ import annotations

import time

from ...core.events import Event
from ...core.part import command, every, on
from ..router import CommandCall
from ..targets import ago
from .base import CommandsBase

KEEP_DAYS = 30
SHOWN, MOST = 5, 15  # lines !said shows, and at most


class SaidCommands(CommandsBase):
    @on("rcon.chat")
    async def _said_record(self, event: Event) -> None:
        if event.get("channel") == "server" or not event.get("id") or not event.server:
            return
        self.ctx.db.execute(
            "INSERT INTO commands_chat (time, server, player_id, name, channel, text) VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), event.server, str(event["id"]).lower(), str(event.get("name") or ""),
             str(event.get("channel") or ""), str(event.get("text") or "")[:600]))

    @every(hours=1)
    async def _said_forget(self) -> None:
        self.ctx.db.execute("DELETE FROM commands_chat WHERE time < ?", (time.time() - KEEP_DAYS * 86400,))

    def _said_count(self, word: str) -> bool:
        return word.isascii() and word.isdecimal()

    @command("said", usage="<player> [lines]", help="The last lines a player typed in chat, on any server (kept 30 "
             "days)", role="moderator", player="seen", start=True, follows="_said_count")
    async def said(self, call: CommandCall) -> None:
        target = call.target
        assert target is not None
        count = min(int(call.rest[0]), MOST) if call.rest and self._said_count(call.rest[0]) else SHOWN
        rows = self.ctx.db.query("SELECT * FROM commands_chat WHERE player_id = ? ORDER BY time DESC LIMIT ?",
                                 (target.id, count))
        if not rows:
            await call.reply(f"{target.name} hasn't typed anything in chat here (in the last {KEEP_DAYS} days).")
            return
        now = time.time()
        lines = [f"{ago(now - r['time'])} ago" + (f" on {r['server']}" if r["server"] != call.server else "")
                 + (" (team)" if r["channel"] == "team" else "") + f" {r['name']}: {r['text']}" for r in reversed(rows)]
        await call.reply("\n".join(lines))
