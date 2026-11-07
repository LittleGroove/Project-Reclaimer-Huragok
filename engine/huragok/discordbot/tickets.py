"""Tickets: one incident, one card. The card with the buttons is the one to keep coming back to: what's done on it updates it, and it becomes the record. It never spawns another.

A card moves as what it's about changes, which on Discord means posting it anew and deleting the old one (nothing can move a message):

    open       nobody has touched it: in `staff`, `reports` or `alerts`, in its own message's colour
    active     someone has acted on it, and nobody has said that's enough yet: where it opened, in the `active` colour
    stale      Vergil's or the Superintendent's, untouched for `stale_after_days`: in `huragok-logs`, in the `stale` colour
    closed     someone acknowledged it (or its problem is over): in its log channel, in the `closed` colour
    filed      a person's own action, decided already: in `logs` from the start, in its own message's colour

A card in a log channel keeps its buttons: acting on it sends it back to where tickets are worked on, needing acknowledging again. Who it belongs to decides which log: `people`'s (a moderator's
action, a call for an admin) go to `logs`, `huragok`'s (Vergil's and the Superintendent's) to `huragok-logs`, so each is a feed of its own. A Discord's owner can have it otherwise: `[tickets] when_closed`
keeps finished cards where they are, or gives each a thread from the start, which closing locks and hides; and a kind of card's line in [messages] can say `closes_to` a channel of its own.

The buttons follow how the player stands now (the ledger's word), so nothing locks: Mute or Unmute, Kick or Let back, Ban or Unban. What's pressed is in buttons.py. Discord may refuse an edit to a card
over an hour old for a while (an unpublished cap, error 30046): what was done still stands, the card catches up later, and a second card is never posted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..core.commandsfile import ROLES
from ..core.events import Event
from ..core.text import TIME, describe_seconds, seconds_of
from . import cards, features, rest
from .cards import HURAGOK, PEOPLE, Card
from .config import FEED, LOG, TICKET, colour_value, shape_of
from .outbox import ALERT, ANSWER, MODERATION, STATUS
from .speakers import message, say
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

SCHEMA_STEP = """
    CREATE TABLE discord_tickets (
        number INTEGER PRIMARY KEY, opened REAL NOT NULL, touched REAL NOT NULL, kind TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL,
        player_id TEXT NOT NULL, player_name TEXT NOT NULL, server TEXT NOT NULL, problem TEXT NOT NULL, card TEXT NOT NULL, persona TEXT NOT NULL,
        open_in TEXT NOT NULL, log_in TEXT NOT NULL, place TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL, thread TEXT NOT NULL);
    CREATE INDEX discord_tickets_state ON discord_tickets (owner, state, touched);
    CREATE TABLE discord_ticket_lines (ticket INTEGER NOT NULL, time REAL NOT NULL, text TEXT NOT NULL);
    CREATE INDEX discord_ticket_lines_ticket ON discord_ticket_lines (ticket, time);
    """
REPLACING_STEP = """
    ALTER TABLE discord_tickets ADD COLUMN replacing TEXT NOT NULL DEFAULT '';
    """  # the old card a move hasn't taken down yet: "channel:message", so a lost move can't leave one up for good
NUMBERED_STEP = """
    CREATE TABLE discord_tickets_numbered (
        number INTEGER PRIMARY KEY AUTOINCREMENT, opened REAL NOT NULL, touched REAL NOT NULL, kind TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL,
        player_id TEXT NOT NULL, player_name TEXT NOT NULL, server TEXT NOT NULL, problem TEXT NOT NULL, card TEXT NOT NULL, persona TEXT NOT NULL,
        open_in TEXT NOT NULL, log_in TEXT NOT NULL, place TEXT NOT NULL, channel TEXT NOT NULL, message TEXT NOT NULL, thread TEXT NOT NULL, replacing TEXT NOT NULL DEFAULT '');
    INSERT INTO discord_tickets_numbered SELECT * FROM discord_tickets;
    DROP TABLE discord_tickets;
    ALTER TABLE discord_tickets_numbered RENAME TO discord_tickets;
    CREATE INDEX discord_tickets_state ON discord_tickets (owner, state, touched);
    CREATE INDEX discord_tickets_problem ON discord_tickets (problem, state);
    CREATE INDEX discord_tickets_player ON discord_tickets (player_id, state);
    CREATE INDEX discord_tickets_touched ON discord_tickets (state, touched);
    CREATE INDEX discord_tickets_unposted ON discord_tickets (number) WHERE message = '';
    """  # Start over empties the table, and old cards can still be on the screen: a number is never used twice, so their buttons can't reach someone else's ticket
OPEN, ACTIVE, STALE, CLOSED, FILED = "open", "active", "stale", "closed", "filed"
LIVE = (OPEN, ACTIVE)
MOVE, STAY, THREAD = "move", "stay", "thread"  # what [tickets] when_closed can say
THREAD_ALREADY = 160004
TOO_MANY_EDITS = 30046
CARD_ROOM = 5500  # Discord: 6,000 characters across a card
LINE_ROOM = 300  # one line of what's happened
NEVER_POSTED_SECONDS = 600.0
SWEEP_MOVES = 10  # cards moved in one sweep, a minute apart: posting a card and deleting one
REPOST_AT_MOST = 21600.0  # a card that won't post is tried again this often, once it's been failing a while
EXPECT_SECONDS = 30.0
BUTTON_STYLES = {"appealagain": 1, "lift": 3, "shorten": 1, "dismiss": 2, "mute": 1, "unmute": 3, "kick": 4, "letback": 3, "ban": 4, "unban": 3, "banall": 4, "acknowledge": 3, "close": 3, "confirm": 3, "deny": 4, "strike": 1, "warn": 1, "pardonstrike": 3, "banthename": 4, "allowname": 3}  # 1 blurple, 2 grey, 3 green, 4 red
# What must be answered, whatever channel someone's sent it to: it's a question, so it can't be just a post.
QUESTIONS = ("reports-", "appeals-", "requests-", "contact-", "tickets-", "calls-", "vergil-name-asked")
NAME_ASKED = "vergil-name-asked"  # a name with a word of a list that only reports: its two answers are its only buttons
BUTTONS_PER_ROW = 5
CARD_OPENS_AFTER_SECONDS = 10  # a strike's card opens this long after the strike at most


class Tickets:
    def __init__(self, part: DiscordPart, clock: Callable[[], float] = time.time) -> None:
        self.part = part
        self.clock = clock
        self.expecting: dict[tuple[str, str], tuple[int, float, int]] = {}  # (player, who pressed) -> the ticket, until when, how many acts: theirs, not cards of their own
        self.dirty: set[int] = set()  # cards Discord wouldn't edit just now: tried again by the sweep
        self.warned: dict[tuple[int, str], float] = {}  # (ticket, who pressed) -> when they were told someone else got there first
        self.reposts: dict[int, tuple[int, float]] = {}  # ticket -> how many times its card has been posted again, and when it's next worth trying

    @property
    def config(self) -> Any:
        return self.part.file

    @property
    def db(self) -> Any:
        return self.part.ctx.db

    def get(self, number: int) -> Any:
        rows = self.db.query("SELECT * FROM discord_tickets WHERE number = ?", (number,))
        return rows[0] if rows else None

    def lines_of(self, number: int) -> list[Any]:
        return self.db.query("SELECT * FROM discord_ticket_lines WHERE ticket = ? ORDER BY time, rowid", (number,))

    # Opening -------------------------------------------------------------------------------------------------------

    def link(self, ticket: Any) -> str:
        return f"https://discord.com/channels/{self.config.guild}/{ticket['channel']}/{ticket['message']}"

    def rule_of(self, ticket: Any) -> str:
        """The automod rule a strike's card is about."""
        return str(json.loads(ticket["card"]).get("rule") or "")

    def of_player(self, player_id: str, *, leaving: int = 0) -> None:
        """Something's changed how they stand: each live card on them says so, as its buttons follow that. A card in a log channel catches up when it's next pressed."""
        for row in self.db.query("SELECT number FROM discord_tickets WHERE player_id = ? AND state IN (?, ?) LIMIT 10", (player_id.lower(), OPEN, ACTIVE)):
            if int(row["number"]) != leaving:
                self.show(int(row["number"]))

    def open(self, card: Card, options: dict[str, Any]) -> int:
        """A new incident gets a new card; an ongoing one is added to (a problem on a server, reports of a player). Its number, or 0 if it has nowhere to go."""
        if card.problem and (ongoing := self.ongoing(card.problem)):
            self.add_line(ongoing, card.line, touch=False)
            if card.message == "alerts-faulted":  # worse than the card says: this is the moment a person's needed
                self._got_worse(ongoing, card, options)
            if card.rule and self.rule_of(self.get(ongoing)) != card.rule:  # strikes on more than one rule: its Pardon strike clears the lot
                kept = json.loads(self.get(ongoing)["card"])
                kept["rule"] = ""
                self.db.execute("UPDATE discord_tickets SET card = ? WHERE number = ?", (json.dumps(kept), ongoing))
            self.show(ongoing)
            return ongoing
        place = str(options.get("channel") or "")
        if self.part.feeds.reads_by_everyone(place, allowed=bool(options.get("public"))):
            return 0
        shape = self.part.feeds.shape_for(card.message, options)
        channel = self.part.feeds.channel_id({"channel": place})
        if not channel:
            return 0
        waiting = f"channel:{channel}" in self.part.feeds.blocked  # kept, for the sweep to post once it can: someone's been told staff have it, or a repeat's being held back because it was said
        wanted_answer = card.message.startswith(QUESTIONS)
        state, open_in = (OPEN, place) if shape == TICKET or wanted_answer else (FILED, "staff")
        log_in = place if shape == LOG else "logs" if card.owner == PEOPLE else "huragok-logs"
        now = self.clock()
        stored = json.dumps({"feed": card.feed, "kind": card.kind, "line": card.line, "heading": card.heading, "fields": card.fields, "footer": card.footer, "server": card.server,
                             "source": card.source, "colour": card.colour, "rule": card.rule})
        cursor = self.db.execute("INSERT INTO discord_tickets (opened, touched, kind, owner, state, player_id, player_name, server, problem, card, persona, open_in, log_in, place, channel, message, thread) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', '')",
                                 (now, now, card.message, card.owner, state, card.subject, card.subject_name, card.server or "", card.problem, stored,
                                  self.part.feeds.persona_of(card, options), open_in, log_in, place, channel))
        ticket = self.get(int(cursor.lastrowid or 0))
        mention, roles = self.part.feeds.ping_of(options)
        silent = bool(options.get("silent", True)) and not roles
        form = message(mention, embeds=[self.embed(ticket)], silent=silent, ping_roles=roles)
        form["components"] = self.components(ticket)
        if waiting:
            return int(ticket["number"])
        wants_thread = state == OPEN and self._closing(ticket)[0] == THREAD and not card.problem.startswith("contact:")  # Contact has one of its own, a private one
        self._post(ticket, form, ALERT if card.urgent else MODERATION, then=(lambda posted: self._start_thread(int(ticket["number"]), posted)) if wants_thread else None)
        return int(ticket["number"])

    def _got_worse(self, number: int, card: Card, options: dict[str, Any]) -> None:
        """The top of the card says what it is now, and its own ping goes, as the first post's was for something the Superintendent was dealing with."""
        ticket = self.get(number)
        kept = json.loads(ticket["card"])
        kept["line"], kept["heading"] = card.line, card.heading
        self.db.execute("UPDATE discord_tickets SET card = ?, kind = ? WHERE number = ?", (json.dumps(kept), card.message, number))
        mention, roles = self.part.feeds.ping_of(options)
        if roles and ticket["message"]:
            text = f"{mention}{card.line} {say(self.config, 'ticket.number', number=number)}: {self.link(ticket)}"
            self.part.poster.post(str(ticket["persona"]), message(text[:1900], silent=False, ping_roles=roles), channel=str(ticket["channel"]), priority=ALERT, what="a server that's got worse", tag=f"channel:{ticket['channel']}")

    def ongoing(self, problem: str) -> int:
        """The live ticket for this one incident: 0 if there isn't one."""
        rows = self.db.query("SELECT number FROM discord_tickets WHERE problem = ? AND state IN (?, ?) ORDER BY number DESC LIMIT 1", (problem, OPEN, ACTIVE))
        return int(rows[0]["number"]) if rows else 0

    def open_as(self, kind: str, card: Card, usual_channel: str) -> int:
        """For a kind of card that isn't a feed's: its line in [messages] says where, and whether at all. Its number, or 0 if it reaches nobody."""
        line = self.config.section("messages").get(kind) or {}
        if not line.get("post", True):
            return 0
        return self.open(card, {"channel": line.get("channel") or usual_channel, "ping": line.get("ping") or "", "silent": bool(line.get("silent", True))})

    def _closing(self, ticket: Any) -> tuple[str, str]:
        """What happens to this card when it's finished, and where to if it moves. Its own line in [messages] over [tickets]."""
        line = self.config.section("messages").get(ticket["kind"])
        own = str(line.get("closes_to") or "").strip() if isinstance(line, dict) else ""
        if own in (STAY, THREAD):
            return own, ""
        if own:
            return MOVE, own
        how = str(self.config.section("tickets").get("when_closed") or MOVE)
        return (how, "") if how in (STAY, THREAD) else (MOVE, str(ticket["log_in"]))

    def keeps_in(self, ticket: Any) -> str:
        """The channel for what goes with this card and stays: where it'll end up, so nothing's left behind in a channel that's meant to empty."""
        how, place = self._closing(ticket)
        channel = self.part.feeds.channel_id({"channel": place}) if how == MOVE and not self.part.feeds.reads_by_everyone(place) else ""  # who reported whom, and what was said, isn't for anyone to read
        return channel or str(ticket["channel"])

    def _start_thread(self, number: int, message_id: str) -> None:
        ticket = self.get(number)
        if ticket is None or not message_id:
            return
        config, channel = self.config, str(ticket["channel"])
        name = f"{say(config, 'ticket.number', number=number)}: {ticket['player_name'] or ticket['kind']}"[:100]

        async def send() -> Any:
            return await to_thread(rest.start_thread, config.token, channel, message_id, name)

        def started(_: Any) -> None:
            self.db.execute("UPDATE discord_tickets SET thread = ? WHERE number = ?", (message_id, number))
            self.show(number)

        def failed(error: rest.DiscordError) -> bool:
            if error.code == THREAD_ALREADY:
                started(None)
            else:
                log.info("Couldn't make ticket %s its thread: %s", number, error)  # the card works without it, and Discuss can try again
            return True

        self.part.outbox.put(send, priority=STATUS, bucket=f"channel:{channel}", what=f"ticket {number}'s thread", on_done=started, on_fail=failed)

    def _close_thread(self, ticket: Any, closed: bool) -> None:
        """Locked and hidden as it finishes, and back if someone acts on the card again. The bot needs Manage Threads: without it the thread only stays as it is."""
        config, thread = self.config, str(ticket["thread"])
        if not thread:
            return

        async def send() -> Any:
            return await to_thread(rest.close_thread, config.token, thread, closed)

        def failed(error: rest.DiscordError) -> bool:
            log.info("Couldn't %s a ticket's thread: %s", "close" if closed else "reopen", error)
            return True

        self.part.outbox.put(send, priority=STATUS, bucket=f"channel:{ticket['channel']}", what="a ticket's thread", on_fail=failed)

    def _post(self, ticket: Any, form: dict[str, Any], priority: int, *, then: Callable[[str], None] | None = None) -> None:
        number = int(ticket["number"])
        channel = str(ticket["channel"])

        def posted(reply: Any) -> None:
            data = reply.data if isinstance(reply, rest.Reply) else None
            new = str(data.get("id") or "") if isinstance(data, dict) else ""
            now = self.get(number)
            if new and now is not None and (str(now["channel"]) != channel or (now["message"] and str(now["message"]) != new)):  # it moved while this was on its way, or was posted again: one card, not two with live buttons
                self._delete(channel, new)
                return
            if new:
                self.db.execute("UPDATE discord_tickets SET message = ?, replacing = '' WHERE number = ?", (new, number))
                self.reposts.pop(number, None)
            old = str(now["replacing"]) if now is not None else ""
            if old:
                self._delete(*old.split(":", 1))
            if then is not None:
                then(new)

        self.part.poster.post(str(ticket["persona"]), form, channel=str(ticket["channel"]), priority=priority, key=f"post:{number}:{channel}", what=f"ticket {number}", tag=f"channel:{ticket['channel']}", on_done=posted,
                              adopt=f"huragok:t:{number}:")  # its buttons': one Discord took but answered too late for is found, not posted twice

    def _delete(self, channel: str, message_id: str) -> None:
        delivery = self.part.poster.delivery
        config = self.config

        async def send() -> Any:
            return await to_thread(delivery.delete, config, channel, message_id)

        def failed(error: rest.DiscordError) -> bool:
            log.info("Couldn't delete a ticket's old card: %s", error)  # it's still there, and its buttons still work
            return True

        self.part.outbox.put(send, priority=STATUS, bucket=f"channel:{channel}", what="a ticket's old card", on_fail=failed)

    # What's happened -------------------------------------------------------------------------------------------------

    def add_line(self, number: int, text: str, *, when: float | None = None, touch: bool = True) -> None:
        now = when or self.clock()
        self.db.execute("INSERT INTO discord_ticket_lines (ticket, time, text) VALUES (?, ?, ?)", (number, now, text[:LINE_ROOM]))
        if touch:
            self.db.execute("UPDATE discord_tickets SET touched = ? WHERE number = ?", (now, number))

    def expect(self, player_id: str, by_id: str, number: int) -> None:
        """What this person does to this player in the next moment is this ticket's, so it makes no card of its own. One press, one act: anything after that is a card as usual."""
        key = (player_id.lower(), by_id)
        waiting = self.expecting[key][2] if key in self.expecting and self.clock() <= self.expecting[key][1] else 0
        self.expecting[key] = (number, self.clock() + EXPECT_SECONDS, waiting + 1)

    def not_expected(self, player_id: str, by_id: str) -> None:
        """The press did nothing: nothing's coming."""
        key = (player_id.lower(), by_id)
        if key in self.expecting:
            number, until, waiting = self.expecting[key]
            if waiting > 1:
                self.expecting[key] = (number, until, waiting - 1)
            else:
                del self.expecting[key]

    def take(self, event: Event) -> int:
        """The ticket a moderation event belongs to, if it's what's being pressed: 0 if not. The press itself writes the line, from the ledger, as this may come after it's done."""
        key = (str(event.get("target_id") or "").lower(), str(event.get("by_id") or ""))
        expected = self.expecting.get(key)
        if expected is None:
            return 0
        self.not_expected(*key)
        return expected[0] if self.clock() <= expected[1] else 0

    def touched(self, number: int, *, then: Callable[[str], None] | None = None) -> bool:
        """Someone acted on it, so it needs acknowledging, again if it had been: back where tickets are worked on. True if it moved, and the old card is on its way out."""
        ticket = self.get(number)
        if ticket is None:
            return False
        if ticket["state"] in LIVE:
            self.db.execute("UPDATE discord_tickets SET state = ?, touched = ? WHERE number = ?", (ACTIVE, self.clock(), number))
            return False
        self.db.execute("UPDATE discord_tickets SET touched = ? WHERE number = ?", (self.clock(), number))
        self._close_thread(ticket, False)  # a thread of its own, or one someone started: it's back in use
        return self._move(number, str(ticket["open_in"]) if ticket["state"] == FILED or self._closing(ticket)[0] == MOVE else str(ticket["place"]), ACTIVE, then=then)

    def close(self, number: int, who: str, *, key: str = "") -> bool:
        """Someone says enough's been done. True if it moved."""
        ticket = self.get(number)
        if ticket is None or ticket["state"] not in LIVE:  # pressed twice, or from an old copy
            return False
        key = key or ("ticket.acknowledged" if ticket["owner"] == HURAGOK else "ticket.closed")
        self.add_line(number, say(self.config, key, who=cards.clean(who)))
        return self.finish(number, CLOSED)

    def finish(self, number: int, state: str) -> bool:
        """Closed or gone stale: to its log channel, or where it is, as the file says. True if it moved."""
        ticket = self.get(number)
        how, place = self._closing(ticket)
        if str(ticket["kind"]).startswith("alerts-"):  # the incident's over: this one's a record now, and whatever comes next is a new one
            self.db.execute("UPDATE discord_tickets SET problem = '' WHERE number = ?", (number,))
        if how == MOVE:
            self._close_thread(ticket, True)  # a Discuss thread under it would otherwise stay open with nothing to do
            return self._move(number, place, state)
        self.db.execute("UPDATE discord_tickets SET state = ? WHERE number = ?", (state, number))
        if how == THREAD:
            self._close_thread(ticket, True)
        self.show(number)
        return False

    def still_wrong(self, ticket: Any) -> bool:
        """It's about a server that's still down: there's nothing to file yet."""
        if str(ticket["kind"]) not in ("alerts-crashed", "alerts-faulted", "alerts-hung", "alerts-start-failed") or not ticket["server"] or ticket["state"] not in LIVE:
            return False
        row = next((row for row in self.part.status.rows() if row.get("name") == ticket["server"]), None)
        return row is not None and str(row.get("phase")) not in ("running", "external")

    def stopped_by_someone(self, server: str, who: str) -> None:
        """Stopped for the night: what was wrong with it isn't wrong now, and the card shouldn't go on saying it keeps failing."""
        for row in self.db.query("SELECT number FROM discord_tickets WHERE problem = ? AND state IN (?, ?)", (server, OPEN, ACTIVE)):
            self.add_line(row["number"], say(self.config, "alert.stopped_by", who=cards.clean(who)))
            self.finish(row["number"], CLOSED)

    def update_worked(self) -> None:
        for row in self.db.query("SELECT number FROM discord_tickets WHERE problem = 'update' AND state IN (?, ?)", (OPEN, ACTIVE)):
            self.add_line(row["number"], say(self.config, "alert.update_worked"))
            self.finish(row["number"], CLOSED)

    def slow_over(self, server: str) -> None:
        """The watchdog says it's healthy again: a ticket that's only about it running slowly is finished. One about a crash waits for its own news."""
        for row in self.db.query("SELECT number FROM discord_tickets WHERE problem = ? AND kind IN ('alerts-slow', 'alerts-watchdog-restart') AND state IN (?, ?)", (server, OPEN, ACTIVE)):
            self.add_line(row["number"], say(self.config, "alert.healthy_again"))
            self.finish(row["number"], CLOSED)

    def signed_in(self, server: str) -> None:
        """The console took its password after all."""
        for row in self.db.query("SELECT number FROM discord_tickets WHERE problem = ? AND state IN (?, ?)", (f"rcon:{server}", OPEN, ACTIVE)):
            self.add_line(row["number"], say(self.config, "alert.signed_in"))
            self.finish(row["number"], CLOSED)

    def problem_over(self, server: str) -> bool:
        """The server's back: its open problems are over, and are on the record. Whether there were any."""
        rows = self.db.query("SELECT number, opened FROM discord_tickets WHERE problem = ? AND state IN (?, ?)", (server, OPEN, ACTIVE))
        for row in rows:
            self.add_line(row["number"], say(self.config, "alert.recovered", minutes=cards.minutes_words(self.clock() - float(row["opened"]))))
            self.finish(row["number"], CLOSED)
        return bool(rows)

    def request_decided(self, event: Event) -> None:
        """A probationary moderator's request, decided here or in game: on its card, which is finished; on the card they asked from, as that's where someone would look; and to them, if they asked from Discord."""
        who = cards.clean(str(event.get("by") or ""))
        why = cards.clean(event.get("why"))
        said = say(self.config, "ticket.confirmed", who=who, what=cards.clean(event.get("what"))) if event.get("status") == "confirmed" else say(self.config, "ticket.denied", who=who, why=f": {why}" if why else "")
        for row in self.db.query("SELECT number FROM discord_tickets WHERE problem = ? AND state IN (?, ?)", (f"request:{event.get('number')}", OPEN, ACTIVE)):
            self.add_line(row["number"], said)
            self.finish(row["number"], CLOSED)
        origin = self.part.card_it_came_from(int(event.get("number") or 0))
        if origin and self.get(origin) is not None:
            self.add_line(origin, say(self.config, "ticket.request_outcome", number=event.get("slot") or event.get("number") or "", what=said), touch=False)
            self.show(origin)
        self._tell_asker(event, said)

    def _tell_asker(self, event: Event, said: str) -> None:
        """Someone on probation who asked from Discord can't be told in game. By DM, which they may have closed: then it's only on the card."""
        asker = str(event.get("by_id") or "")
        if not asker.startswith("discord:"):
            return
        config, user = self.config, asker.removeprefix("discord:")
        text = say(config, "ticket.request_told", number=event.get("slot") or "", what=said)

        async def send() -> Any:
            return await to_thread(rest.send_dm, config.token, user, {"content": text, "allowed_mentions": {"parse": []}})

        def refused(error: rest.DiscordError) -> bool:
            log.info("Couldn't tell a probationary moderator how their request went: %s", error)
            return True

        self.part.outbox.put(send, priority=ANSWER, bucket="dm", what="telling someone how their request went", on_fail=refused)

    def open_by_hand(self, who: str, player_id: str, name: str, server: str) -> int:
        """/ticket: the buttons for anyone, any time. Its line in [messages] says where, and it isn't anyone's feed to switch off."""
        return self.open_as("tickets-opened", cards.opened_by_hand(self.config, who, player_id, name, server), "staff")

    def sweep(self) -> None:
        """Vergil's and the Superintendent's that nobody's touched go to their log, as most need nothing; and cards Discord wouldn't edit are tried again."""
        for number in list(self.dirty):
            self.dirty.discard(number)
            self.show(number)
        now, posted = self.clock(), 0
        for row in self.db.query("SELECT number, place, channel FROM discord_tickets WHERE message = '' AND touched <= ? ORDER BY number", (now - NEVER_POSTED_SECONDS,)):
            # Its post was refused, or lost in an outage or a stop: nobody has seen it, and whoever comes next would be told it's in hand. An old card of it goes as this one's up. Where its channel is now: it may have been deleted and made again.
            number = int(row["number"])
            tries, due = self.reposts.get(number, (0, 0.0))
            channel = self.part.feeds.channel_id({"channel": row["place"]}) or str(row["channel"])
            if now < due or f"channel:{channel}" in self.part.feeds.blocked:
                continue
            self.reposts[number] = (tries + 1, now + min(NEVER_POSTED_SECONDS * 2 ** tries, REPOST_AT_MOST))  # a card Discord will never take shouldn't be tried every minute for ever
            self._post_again(number)
            posted += 1
            if posted >= SWEEP_MOVES:
                break
        days = float(self.config.section("tickets").get("stale_after_days", 0) or 0)
        if days <= 0:  # never means never, whatever a line says
            return
        moved = 0
        lines = self.config.section("messages")
        for row in self.db.query("SELECT number, kind, owner, touched, state, server FROM discord_tickets WHERE state IN (?, ?) ORDER BY touched", (OPEN, ACTIVE)):
            line = lines.get(row["kind"])
            own = line.get("stale_after_hours") if isinstance(line, dict) else None
            hours = float(own) if own is not None else days * 24 if row["owner"] == HURAGOK else 0.0  # people's wait for someone, unless their line says
            if str(row["kind"]).startswith("reports-") and row["state"] == ACTIVE and self._acted_on_report(int(row["number"])):  # someone's dealt with it, and nobody closes those by habit
                hours = float(self.config.section("reports").get("acted_after_hours", 24) or 0) or hours
            if not hours or self.clock() - float(row["touched"]) < hours * 3600 or self.still_wrong(row):  # with a server still down, it's not over because nobody's looked
                continue
            self.add_line(row["number"], say(self.config, "ticket.stale", how_long=describe_seconds(hours * 3600)), touch=False)
            self.finish(row["number"], STALE)
            moved += 1
            if moved >= SWEEP_MOVES:
                break

    def _move(self, number: int, place: str, state: str, *, then: Callable[[str], None] | None = None) -> bool:
        """To another channel, in another state: a new card, and the old one deleted once that's up. In place if there's nowhere to go, or it's the same channel. True if it moved."""
        ticket = self.get(number)
        channel = self.part.feeds.channel_id({"channel": place})
        if place == ticket["log_in"] and self.part.feeds.reads_by_everyone(place):  # its usual log, and anyone can read that: it stays where it is. A `closes_to` is someone's choice, and its panel says it isn't checked
            channel = ""
        if not channel or channel == ticket["channel"]:
            self.db.execute("UPDATE discord_tickets SET state = ?, place = ? WHERE number = ?", (state, place if channel else ticket["place"], number))
            self.show(number)
            if then is not None and ticket["message"]:
                then(str(ticket["message"]))
            return False
        old = f"{ticket['channel']}:{ticket['message']}" if ticket["message"] else str(ticket["replacing"])
        self.db.execute("UPDATE discord_tickets SET state = ?, place = ?, channel = ?, message = '', replacing = ? WHERE number = ?", (state, place, channel, old, number))
        moved = self.get(number)
        form = message("", embeds=[self.embed(moved)])
        form["components"] = self.components(moved)
        self._post(moved, form, MODERATION, then=then)
        return True

    def show(self, number: int) -> None:
        """The card as it stands now, edited in place."""
        ticket = self.get(number)
        if ticket is None:
            return
        if not ticket["message"]:  # still on its way
            self.dirty.add(number)
            return
        delivery = self.part.poster.delivery
        config, channel, message_id = self.config, str(ticket["channel"]), str(ticket["message"])
        form = self.edit_form(ticket)

        async def send() -> Any:
            return await to_thread(delivery.edit, config, channel, message_id, form)

        def failed(error: rest.DiscordError) -> bool:
            if error.code == TOO_MANY_EDITS:  # "try again later", and what's on the card is only behind
                self.dirty.add(number)
                return True
            if error.status == 404:  # someone deleted the card, or it can't be edited now (the webhook that posted it has gone): the ticket goes on
                self.db.execute("UPDATE discord_tickets SET replacing = ? WHERE number = ?", (f"{channel}:{message_id}", number))  # the latter leaves it up, buttons and all, unless the bot may tidy it
                self._post_again(number)
                return True
            return False

        self.part.outbox.put(send, priority=MODERATION, bucket=f"channel:{channel}", key=f"ticket:{number}", what=f"ticket {number}", tag=f"channel:{channel}", on_fail=failed)

    def _post_again(self, number: int) -> None:
        ticket = self.get(number)
        channel = self.part.feeds.channel_id({"channel": ticket["place"]}) or str(ticket["channel"])
        self.db.execute("UPDATE discord_tickets SET message = '', channel = ? WHERE number = ?", (channel, number))
        fresh = self.get(number)
        again = message("", embeds=[self.embed(fresh)])
        again["components"] = self.components(fresh)
        self._post(fresh, again, MODERATION)

    # The card ----------------------------------------------------------------------------------------------------------

    def edit_form(self, ticket: Any) -> dict[str, Any]:
        return {"embeds": [self.embed(ticket)], "components": self.components(ticket), "allowed_mentions": {"parse": []}}

    def embed(self, ticket: Any) -> dict[str, Any]:
        config = self.config
        kept = json.loads(ticket["card"])
        fields = [tuple(field) for field in kept.get("fields") or []]
        options = config.section("tickets")
        player_id = str(ticket["player_id"])
        if player_id and ticket["state"] in LIVE and (now := self._now_words(player_id)):
            fields.append((say(config, "ticket.now"), now))
        if player_id and options.get("record", True):
            fields.append((say(config, "moderation.record"), self.part.record_of(player_id) or say(config, "record.none")))
        if player_id and features.BY_KEY["notes"].is_on(config):
            noted = self.part.noted(player_id, int(config.section("notes").get("note_lines", 3) or 0))
            if noted:
                fields.append((say(config, "moderation.notes"), noted[:cards.MAX_FIELD]))
        footer = " · ".join(part for part in (say(config, "ticket.number", number=ticket["number"]), kept.get("footer") or "") if part)
        taken = sum(len(name) + min(len(value), cards.MAX_FIELD) for name, value in fields) + len(footer) + len(str(kept.get("heading") or "")) + len(str(kept.get("line") or ""))
        card = Card(kept.get("feed") or "", kept.get("kind") or "", self._story(ticket, kept.get("line") or "", CARD_ROOM - taken), kept.get("heading") or "", fields, footer, kept.get("server"), kept.get("source") or "")
        return cards.embed(card, self._colour(ticket, kept))

    def _now_words(self, player_id: str) -> str:
        """Where they are and how they stand: the footer is only where it began, and they may have moved on."""
        standing = self.standing(player_id)
        where = [f"on {cards.clean(name)} as #{player.number}" for name, state in self.part.ctx.servers.items() if state.connected and (player := state.player_by_id(player_id.lower()))]
        return " · ".join([*where, *[word for word, on in (("muted", standing["muted"]), ("kicked", standing["kicked"]), ("banned", standing["banned"])) if on]])

    def _story(self, ticket: Any, line: str, room: int) -> str:
        """Its own line, then what's happened, the latest kept if it doesn't all fit: !whois has everything."""
        config = self.config
        head = line
        if ticket["thread"]:
            head += "\n" + say(config, "ticket.discussion", thread=f"<#{ticket['thread']}>")
        rows = self.lines_of(int(ticket["number"]))
        if not rows:
            return head
        room = min(room, cards.MAX_DESCRIPTION) - len(head) - 40
        shown: list[str] = []
        for row in reversed(rows):
            text = f"<t:{int(row['time'])}:R> {row['text']}"
            if len(text) + 1 > room:
                break
            room -= len(text) + 1
            shown.insert(0, text)
        earlier = len(rows) - len(shown)
        return "\n".join([head, "", f"**{say(config, 'ticket.history')}**", *([say(config, "ticket.earlier", count=earlier)] if earlier else []), *shown])

    def _colour(self, ticket: Any, kept: dict[str, Any]) -> int:
        config = self.config
        named = (config.section("tickets").get("colours") or {}).get(ticket["state"])
        line = config.section("messages").get(ticket["kind"]) or {}
        return colour_value(config, named or kept.get("colour") or line.get("colour"))

    def components(self, ticket: Any) -> list[dict[str, Any]]:
        config = self.config
        options = config.section("tickets")
        number = int(ticket["number"])
        names: list[str] = []
        greyed: set[str] = set()  # still there, so what's beside it doesn't move, and can't be pressed
        after: list[str] = []
        player_id = str(ticket["player_id"])
        live = ticket["state"] in LIVE
        asking = str(ticket["problem"]).startswith("request:") and live and self._undecided_request(ticket)  # decided already, and brought back: an ordinary card
        appealing = str(ticket["problem"]).startswith("appeal:") and live and self._undecided_appeal(number)
        contact = str(ticket["problem"]).startswith("contact:")
        if str(ticket["kind"]) == NAME_ASKED:  # either answer closes it, and that's all there is to it
            buttons = [{"type": 2, "style": BUTTON_STYLES[name], "label": say(config, f"button.{name}")[:80], "custom_id": f"huragok:t:{number}:{name}"} for name in ("banthename", "allowname")]
            return [{"type": 1, "components": buttons}] if live else []
        if asking:
            names += ["confirm", "deny"]
        if appealing:
            names += list(dict.fromkeys("deny" if str(name) == "refuse" else str(name) for name in config.section("appeals").get("buttons") or [] if str(name) in ("lift", "shorten", "deny", "refuse")))
        if player_id:
            now = self.standing(player_id)
            kind_of_its_own = config.section("reports").get("buttons") if str(ticket["kind"]).startswith("reports-") else None
            on_a_server = now["on"]
            for wanted in [str(name) for name in kind_of_its_own or options.get("buttons") or []]:
                if (asking or appealing) and wanted in ("mute", "kick", "ban", "strike", "warn"):  # it's a question, with answers of its own
                    continue
                if wanted == "mute":
                    if not on_a_server and now["banned"] and not now["muted"]:  # banned, and gone: there's nobody to mute. On another server, there's still someone to deal with
                        greyed.add("mute")
                    names.append("unmute" if now["muted"] else "mute")
                elif wanted == "kick" and on_a_server:
                    names.append("kick")
                    after += ["letback"] if now["kicked"] else []  # kept off another server, and on this one
                elif wanted == "kick":
                    if now["banned"]:
                        greyed.add("kick")
                    names.append("letback" if now["kicked"] and not now["banned"] else "kick")
                elif wanted == "ban":
                    names.append("unban" if now["banned"] else "ban")
                elif wanted == "warn" and on_a_server and not (asking or appealing):
                    names.append("warn")  # it's said to them in game, so they have to be there
                elif wanted == "strike" or wanted in ("whois", "said") or (wanted == "note" and features.BY_KEY["notes"].is_on(config)):
                    names.append(wanted)
            names += after
            if not asking and not appealing and (str(ticket["kind"]) == "vergil-strike" or (str(ticket["kind"]) == "vergil-heard" and self.struck_on_card(ticket))):
                names.append("pardonstrike")  # a strike Vergil got wrong. Unmuting alone leaves it counting
            if str(ticket["kind"]) == "watch-banned-elsewhere":
                names.append("banall")  # usually what this card is for
        if str(ticket["kind"]).startswith("alerts-") and ticket["server"]:
            names.append("server")  # the power buttons, for whoever may use them, seen by them alone
        if not live and str(ticket["problem"]).startswith("appeal:") and self.part.appeals.still_kept_out(number):
            names.append("appealagain")
        if options.get("threads", True) or contact:  # a Contact thread is private: this is the staff's only way in
            names.append("thread")
        if live and str(ticket["kind"]).startswith("reports-") and not self._acted_on_report(number):
            names.append("dismiss")  # nothing to it. Once someone's acted on it, it's a Close
        elif live and not asking:  # an appeal too: nothing more might be wanted, and it can always be finished
            names.append("acknowledge" if ticket["owner"] == HURAGOK else "close")
        buttons = [{"type": 2, "style": BUTTON_STYLES.get(name, 2), "label": say(config, f"button.{name}")[:80], "custom_id": f"huragok:t:{number}:{name}", **({"disabled": True} if name in greyed else {})} for name in dict.fromkeys(names)]  # a name twice in the file is one button
        rows = [{"type": 1, "components": buttons[start:start + BUTTONS_PER_ROW]} for start in range(0, len(buttons), BUTTONS_PER_ROW)]
        if ticket["kind"] == "calls-called" and live:  # it stays, as the first pick can be wrong: its names are told apart by number and ID
            state = self.part.ctx.servers.get(str(ticket["server"]))
            players = list(state.players.values())[:25] if state is not None else []
            if players:
                rows.insert(0, {"type": 1, "components": [{"type": 3, "custom_id": f"huragok:t:{number}:about", "placeholder": say(config, "ticket.about")[:150],
                                                           "options": [{"label": f"{player.name} · #{player.number} · {str(player.id)[:8]}"[:100], "value": self.part.opaque(str(player.id)), "default": str(player.id) == player_id} for player in players]}]})
        return rows

    def _undecided_request(self, ticket: Any) -> bool:
        try:
            rows = self.db.query("SELECT status FROM commands_requests WHERE number = ?", (str(ticket["problem"]).removeprefix("request:"),))
        except sqlite3.Error:
            return False
        return bool(rows) and rows[0]["status"] == "open"

    def _undecided_appeal(self, number: int) -> bool:
        rows = self.db.query("SELECT outcome FROM discord_appeals WHERE ticket = ? ORDER BY number DESC LIMIT 1", (number,))
        return not rows or not rows[0]["outcome"]

    def struck_on_card(self, ticket: Any) -> list[str]:
        """The lists of the strikes that joined this card, still standing: a card that only heard a word has nothing to take off until a strike joins it. A strike from before the card isn't
        its to pardon (audit 6 November: Pardon strike on a heard word cleared yesterday's strike). The strike a card opens with comes a moment before the card."""
        try:
            rows = self.db.query("SELECT DISTINCT grp FROM automod_strikes WHERE player_id = ? AND pardoned IS NULL AND time >= ? ORDER BY grp",
                                 (str(ticket["player_id"] or "").lower(), float(ticket["opened"]) - CARD_OPENS_AFTER_SECONDS))
        except sqlite3.Error:  # Vergil's never run
            return []
        return [str(row["grp"]) for row in rows]

    def _acted_on_report(self, number: int) -> bool:
        return bool(self.db.query("SELECT 1 FROM discord_reports WHERE ticket = ? AND upheld = 1 LIMIT 1", (number,)))

    def standing(self, player_id: str) -> dict[str, bool]:
        """How they stand now, by the ledger's last word on each. What a button says is this, so nothing locks and nothing's out of date for long."""
        rows = self.db.query("SELECT kind, duration, time, evidence, server FROM actions WHERE target_id = ? AND (ok = 1 OR kind = 'unmute') AND dry_run = 0 AND kind IN ('mute', 'unmute', 'kick', 'pardon', 'ban', 'unban') ORDER BY number DESC LIMIT 60",
                             (player_id.lower(),))  # an unmute the server said no to has still come off Vergil's list, and nobody can do more
        now = self.clock()
        def running(row: Any, otherwise: float) -> bool:
            duration = str(row["duration"] or "")
            return now < float(row["time"]) + (seconds_of(duration) if TIME.fullmatch(duration) else otherwise)

        muting = next((row for row in rows if row["kind"] in ("mute", "unmute")), None)
        muted = muting is not None and muting["kind"] == "mute" and (not muting["duration"] or running(muting, float("inf")))
        removing = next((row for row in rows if row["kind"] in ("kick", "pardon", "ban", "unban") and not str(row["evidence"] or "").startswith("kicked on ")), None)
        banned = bool(self.part.banned_on(player_id))  # on any server: a kick on one, or an unban on another, doesn't lift it
        kicked = removing is not None and removing["kind"] in ("ban", "kick") and bool(removing["duration"] or removing["kind"] == "kick") and self.part.kicked_lately(player_id.lower(), 0.0, now) is not None
        if removing is None:  # no word on them in the ledger: the bans brought over from older lists, or given while Huragok was off, are only on its own list
            try:
                listed = self.db.query("SELECT expires FROM commands_every_server WHERE kind = 'ban' AND target = ? AND lifted IS NULL AND (expires IS NULL OR expires > ?) ORDER BY number DESC LIMIT 1", (player_id.lower(), now))
            except sqlite3.Error:
                listed = []
            if listed:
                banned, kicked = listed[0]["expires"] is None, listed[0]["expires"] is not None
        put_out_of = str(removing["server"] or "") if removing is not None and (banned or kicked) else ""  # still listed there for a moment, on their way out
        on = any(state.connected and state.player_by_id(player_id.lower()) for name, state in self.part.ctx.servers.items() if name != put_out_of)
        return {"muted": muted, "kicked": kicked, "banned": banned, "on": on}

    def can_close(self, rank: str) -> bool:
        needed = str(self.config.section("tickets").get("close_rank") or "moderator")
        return rank in ROLES and needed in ROLES and ROLES.index(rank) >= ROLES.index(needed)


__all__ = ["ACTIVE", "CLOSED", "FEED", "FILED", "LOG", "OPEN", "SCHEMA_STEP", "STALE", "TICKET", "Tickets"]
