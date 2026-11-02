"""What members asked to be told (/notify, in staff.py): a server reaching so many players, a game type starting. Kept in discord_alerts, posted with
only those members pinged, each no more than once per `cooldown_minutes`.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import cards, features
from .cards import Card

if TYPE_CHECKING:
    from .part import DiscordPart

SCHEMA_STEP = """
    CREATE TABLE discord_alerts (
        user_id TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL, server TEXT NOT NULL, last REAL NOT NULL,
        PRIMARY KEY (user_id, kind, value, server));
    """
PLAYERS, GAME = "players", "game"
MOST_PER_MEMBER = 5
PINGS_PER_POST = 50  # Discord allows 100 in a message's allowed_mentions; the rest go in the next


class Alerts:
    def __init__(self, part: DiscordPart, clock: Callable[[], float] = time.time) -> None:
        self.part = part
        self.clock = clock
        self.counts: dict[str, int] = {}  # server -> players on, last look. A server's first look crosses nothing: a restart pings nobody

    @property
    def on(self) -> bool:
        return features.BY_KEY["notify"].is_on(self.part.file)

    def add(self, user_id: str, kind: str, value: str, server: str) -> bool:
        """False if they already have as many as a member can."""
        db = self.part.ctx.db
        if len(self.of(user_id)) >= MOST_PER_MEMBER:
            return False
        db.execute("INSERT OR REPLACE INTO discord_alerts (user_id, kind, value, server, last) VALUES (?, ?, ?, ?, 0)", (user_id, kind, value, server))
        return True

    def of(self, user_id: str) -> list[Any]:
        return self.part.ctx.db.query("SELECT * FROM discord_alerts WHERE user_id = ? ORDER BY kind, value", (user_id,))

    def stop(self, user_id: str) -> int:
        return int(self.part.ctx.db.execute("DELETE FROM discord_alerts WHERE user_id = ?", (user_id,)).rowcount)

    def tick(self) -> None:
        """A server going from under someone's number to it or past it."""
        if not self.on:
            return
        for server, state in list(self.part.ctx.servers.items()):
            count = len(state.players)
            before = self.counts.get(server)
            self.counts[server] = count
            if before is None or count <= before:
                continue
            rows = self.part.ctx.db.query("SELECT * FROM discord_alerts WHERE kind = ? AND CAST(value AS INTEGER) > ? AND CAST(value AS INTEGER) <= ? AND server IN ('', ?)",
                                          (PLAYERS, before, count, server))
            self._tell(rows, cards.notify_players(self.part.file, server, count))

    def game_started(self, server: str, mode: str, game_map: str) -> None:
        if not self.on or not mode:
            return
        rows = [row for row in self.part.ctx.db.query("SELECT * FROM discord_alerts WHERE kind = ? AND server IN ('', ?)", (GAME, server))
                if str(row["value"]).casefold() in mode.casefold()]
        self._tell(rows, cards.notify_game(self.part.file, server, mode, game_map))

    def _tell(self, rows: list[Any], card: Card) -> None:
        now = self.clock()
        cooldown = float(self.part.file.section("notify").get("cooldown_minutes", 120) or 0) * 60
        due = [row for row in rows if now - float(row["last"]) >= cooldown]
        people = sorted({str(row["user_id"]) for row in due})
        if not people or not self.part.feeds.reaches(card):
            return
        for row in due:
            self.part.ctx.db.execute("UPDATE discord_alerts SET last = ? WHERE user_id = ? AND kind = ? AND value = ? AND server = ?",
                                     (now, row["user_id"], row["kind"], row["value"], row["server"]))
        for start in range(0, len(people), PINGS_PER_POST):
            self.part.feeds.deliver(dataclasses.replace(card, pings=tuple(people[start:start + PINGS_PER_POST])))
