"""Reports: a person telling staff about a player, with `!report` in game, /report in Discord, or a right-click on a message. It's one ticket for a player while that's open, however many report them: what
they said is added to it. Enough different people within a while pings staff (`threshold`). How often someone's reports get acted on shows beside their name, as staff learn who to believe.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..core.background import background
from ..core.events import Event
from ..lore import callout
from . import cards, rest, tidy
from .cards import Card
from .outbox import ALERT, ANSWER
from .speakers import message, say
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

SCHEMA_STEP = """
    CREATE TABLE discord_reports (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, reporter TEXT NOT NULL, reporter_name TEXT NOT NULL, player_id TEXT NOT NULL, player_name TEXT NOT NULL, why TEXT NOT NULL, source TEXT NOT NULL,
        ticket INTEGER NOT NULL, upheld INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX discord_reports_player ON discord_reports (player_id, time);
    CREATE INDEX discord_reports_reporter ON discord_reports (reporter, time);
    CREATE INDEX discord_reports_ticket ON discord_reports (ticket);
    """
log = logging.getLogger(__name__)

TAKEN, TOO_SOON, TOO_MANY, ALREADY, OFF = "taken", "too soon", "too many", "already", "off"
UPHOLDS = ("mute", "kick", "ban", "warn", "strike")  # what counts as staff agreeing
WHY_ROOM = 500


def _mention(reporter: str) -> str:
    """Who it really is, as a name in a Discord is anyone's to choose. It can't ping: nothing posted allows it."""
    return f" (<@{reporter.removeprefix('discord:')}>)" if reporter.startswith("discord:") else ""


class Reports:
    def __init__(self, part: DiscordPart, clock: Callable[[], float] = time.time) -> None:
        self.part = part
        self.clock = clock

    @property
    def config(self) -> Any:
        return self.part.file

    @property
    def db(self) -> Any:
        return self.part.ctx.db

    @property
    def options(self) -> dict[str, Any]:
        return self.config.section("reports")

    def takes(self, source: str) -> bool:
        return bool(self.options.get("enabled")) and self.config.mode == "bot" and source in (self.options.get("from") or [])

    def _too_soon(self, reporter: str) -> bool:
        wait = float(self.options.get("reporter_cooldown_seconds", 0) or 0)
        rows = self.db.query("SELECT time FROM discord_reports WHERE reporter = ? ORDER BY time DESC LIMIT 1", (reporter,))
        return bool(wait) and bool(rows) and self.clock() - float(rows[0]["time"]) < wait

    def next_allowed(self, reporter: str) -> float:
        wait = float(self.options.get("reporter_cooldown_seconds", 0) or 0)
        rows = self.db.query("SELECT time FROM discord_reports WHERE reporter = ? ORDER BY time DESC LIMIT 1", (reporter,))
        return float(rows[0]["time"]) + wait if rows else self.clock()

    def track_record(self, reporter: str) -> str:
        """How their reports have gone: staff's to weigh. Nothing for a first."""
        rows = self.db.query("SELECT COUNT(*) AS made, COALESCE(SUM(upheld), 0) AS upheld FROM discord_reports WHERE reporter = ?", (reporter,))
        made, upheld = int(rows[0]["made"]), int(rows[0]["upheld"])
        return say(self.config, "reports.record", made=cards.counted(made, "report"), upheld=upheld) if made else say(self.config, "reports.first")

    def make(self, reporter: str, reporter_name: str, player_id: str, player_name: str, server: str | None, why: str, source: str) -> tuple[str, int]:
        """A report of a player: (what became of it, its ticket). Reporting them again while it's open says nothing new."""
        player_id, why = player_id.lower(), why[:WHY_ROOM]
        if not self.takes(source):
            return OFF, 0
        problem = f"report:{player_id}"
        ongoing = self.part.tickets.ongoing(problem)
        if ongoing and self.db.query("SELECT 1 FROM discord_reports WHERE ticket = ? AND reporter = ?", (ongoing, reporter)):  # theirs is there: what they've added isn't lost, and isn't another report
            if why:
                self.part.tickets.add_line(ongoing, say(self.config, "reports.added", who=cards.clean(reporter_name), why=cards.clean(why)), touch=False)
                self.part.tickets.show(ongoing)
            return ALREADY, ongoing
        if self._too_soon(reporter):
            return TOO_SOON, 0
        most = int(self.options.get("most_at_once", 0) or 0)
        if most and self.db.query("SELECT COUNT(DISTINCT discord_reports.ticket) AS n FROM discord_reports JOIN discord_tickets ON discord_tickets.number = discord_reports.ticket WHERE reporter = ? AND discord_tickets.state IN ('open', 'active')", (reporter,))[0]["n"] >= most:
            return TOO_MANY, 0
        record = self.track_record(reporter)
        lines = self.part.said_by(player_id, int(self.options.get("evidence_lines", 0) or 0), self.clock())
        card = cards.reported(self.config, reporter_name, record, player_id, player_name, server, why, lines, problem, mention=_mention(reporter))
        number = self._open(card, "reports-made")
        if not number:
            return OFF, 0
        self.db.execute("INSERT INTO discord_reports (time, reporter, reporter_name, player_id, player_name, why, source, ticket) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self.clock(), reporter, reporter_name, player_id, player_name, why, source, number))
        self._enough_people(player_id, player_name, number)
        self.part.tell_staff_in_game(callout("request", f"{reporter_name} reported {player_name}" + (f" on {server}" if server else "") + (f": {why}" if why else ".")), skip=reporter)
        return TAKEN, number

    def message_reported(self, reporter: str, reporter_name: str, author: str, channel_name: str, link: str, quoted: str, why: str) -> tuple[str, int]:
        """A message in the Discord, not a player: one ticket for it, as its link's its only subject."""
        if not self.takes("discord"):
            return OFF, 0
        if self._too_soon(reporter):
            return TOO_SOON, 0
        problem = f"message:{link}"
        ongoing = self.part.tickets.ongoing(problem)
        if ongoing and self.db.query("SELECT 1 FROM discord_reports WHERE ticket = ? AND reporter = ?", (ongoing, reporter)):
            return ALREADY, ongoing
        card = cards.message_reported(self.config, reporter_name, self.track_record(reporter), author, channel_name, link, quoted, why, mention=_mention(reporter))
        card.problem = problem  # one message, one card, however many people report it
        number = self._open(card, "reports-message")
        if not number:
            return OFF, 0
        self.db.execute("INSERT INTO discord_reports (time, reporter, reporter_name, player_id, player_name, why, source, ticket) VALUES (?, ?, ?, '', ?, ?, 'message', ?)",
                        (self.clock(), reporter, reporter_name, author, why, number))
        return TAKEN, number

    def _open(self, card: Card, kind: str) -> int:
        """Its line in [messages] says where. With that switched off, it reaches nobody and says so."""
        return self.part.tickets.open_as(kind, card, "reports")

    def _enough_people(self, player_id: str, player_name: str, number: int) -> None:
        """So many different people at once is worth a ping: once, as it gets there."""
        needed = int(self.options.get("threshold", 0) or 0)
        line = self.config.section("messages").get("reports-many") or {}
        if needed <= 0 or not line.get("post", True):
            return
        since = self.clock() - float(self.options.get("threshold_minutes", 10) or 10) * 60
        rows = self.db.query("SELECT COUNT(DISTINCT reporter) AS people FROM discord_reports WHERE player_id = ? AND time >= ?", (player_id, since))
        people = int(rows[0]["people"])
        if people != needed or number in self.part.reports_pinged:
            return
        self.part.reports_pinged.add(number)
        ticket = self.part.tickets.get(number)
        mention, roles = self.part.feeds.ping_of(line)
        minutes = f"{float(self.options.get('threshold_minutes', 10) or 10):g}"
        self.part.tickets.add_line(number, cards.enough_reports(self.config, people, player_name, minutes, "").line.rstrip(": "), touch=False)
        self.part.tickets.show(number)
        text = cards.enough_reports(self.config, people, player_name, minutes, f"[{say(self.config, 'ticket.number', number=number)}]({self.part.tickets.link(ticket)})").line
        lasting = float(line.get("remove_after_minutes", 0) or 0) * 60  # it's on the card, for good. This is only to get someone's attention

        def posted(reply: Any) -> None:
            new = str(reply.data.get("id") or "") if isinstance(reply, rest.Reply) and isinstance(reply.data, dict) else ""
            if lasting and new:
                tidy.later(self.part, str(ticket["channel"]), new, lasting, "a ping about reports")

        self.part.poster.post(str(ticket["persona"]), message(mention + text, silent=not roles, ping_roles=roles), channel=str(ticket["channel"]), priority=ALERT, what="enough reports to ping about", tag=f"channel:{ticket['channel']}", on_done=posted)

    def upheld(self, number: int) -> None:
        """Someone acted on a report's ticket: everyone's report on it goes down as right, and they're told, once."""
        waiting = self.db.query("SELECT reporter, player_name FROM discord_reports WHERE ticket = ? AND upheld = 0", (number,))
        self.db.execute("UPDATE discord_reports SET upheld = 1 WHERE ticket = ?", (number,))
        if self.options.get("tell_reporter", "acted") in ("acted", "all"):
            self._tell(waiting, "reports.told_acted")

    def dismissed(self, number: int) -> None:
        """Nothing came of it. [reports] tell_reporter says whether they hear that."""
        if self.options.get("tell_reporter", "acted") == "all":
            self._tell(self.db.query("SELECT reporter, player_name FROM discord_reports WHERE ticket = ? AND upheld = 0", (number,)), "reports.told_dismissed")

    def _tell(self, rows: list[Any], key: str) -> None:
        """Whoever reported, however they did: on a server now, or as they next join; by DM from Discord, which they may have closed. Nobody else is ever told who reported whom."""
        config = self.config
        for row in rows:
            reporter, text = str(row["reporter"]), say(config, key, player=str(row["player_name"]))
            if reporter.startswith("discord:"):
                user = reporter.removeprefix("discord:")

                async def send(user: str = user, text: str = text) -> Any:
                    return await to_thread(rest.send_dm, config.token, user, {"content": text, "allowed_mentions": {"parse": []}})

                def refused(error: rest.DiscordError) -> bool:
                    log.info("Couldn't tell someone how their report went: %s", error)
                    return True

                self.part.outbox.put(send, priority=ANSWER, bucket="dm", what="telling someone how their report went", on_fail=refused)
                continue
            where = next(((server, player) for server, state in list(self.part.ctx.servers.items()) if (player := state.player_by_id(reporter))), None)
            if where is None:
                self.part.notice_for(reporter, callout("request", text))
            else:
                background(self.part.ctx.tell(where[0], where[1], callout("request", text)), "telling someone how their report went", log)

    def case(self, event: Event, pressed: int = 0) -> bool:
        """A moderator dealt with a reported player some other way, in game say: on the report's card, which counts as acted on, and it's still theirs to close. Whether there was one."""
        if str(event.get("kind")) not in UPHOLDS or event.get("dry_run") or not event.get("ok", True) or event.get("source") == "vote":  # a vote isn't staff agreeing
            return False
        player_id = str(event.get("target_id") or "").lower()
        number = self.part.tickets.ongoing(f"report:{player_id}")
        if not number or number == pressed:  # a press on the report's own card has said so itself
            return False
        card = cards.moderation(self.config, self.part.feeds._by_real_name(event))
        if card is not None:
            self.part.tickets.add_line(number, card.line)
        self.upheld(number)
        self.part.tickets.touched(number)
        self.part.tickets.show(number)
        return True


__all__ = ["ALREADY", "OFF", "Reports", "SCHEMA_STEP", "TAKEN", "TOO_MANY", "TOO_SOON", "UPHOLDS"]
