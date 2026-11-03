"""Feeds: what happened, posted where it should be. Each event becomes a Card (cards.py); this decides whether it's wanted, where it goes and how often.

Every kind of message has its own line in [messages]: its channel, colour, ping, and whether it posts at all. A feed's `enabled` switches a whole group. The channel decides how it looks: in a ticket
channel it's a ticket (tickets.py), in a log channel a ticket filed away if it's about a player, or else one line, and anywhere else a post.

Nothing floods: the first of a burst posts at once, the rest of `merge_seconds` become one when it's over, with no edits and so no extra requests. An ongoing
problem on a server is one ticket, added to once per `repeat_minutes`, and when it's over its recovery says for how long. Performance alerts wait `grace_seconds` after a start. A
channel or webhook that's refused stops being posted to (`blocked`), with the reason on the dashboard, until Discord is read again or the file changes: 10,000
refusals in 10 minutes blocks this PC's address.
"""

from __future__ import annotations

import difflib
import logging
import time
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..core.events import Event
from ..players.ledger import is_housekeeping
from . import cards, features, tidy
from .cards import PEOPLE, Card
from .config import colour_value, defaults, DiscordConfig, FEED, LOG, pings_of, shape_of, TICKET
from .outbox import ALERT, MODERATION, STATUS
from .rest import Reply
from .speakers import message, say

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

PROBLEM_KINDS = {"crashed", "faulted", "hung", "start-failed", "slow", "watchdog-restart"}
OWN_PROBLEM_KINDS = {"rcon-sign-in": "rcon:"}  # over when something else says so: one card, not one per refusal
SLOW_KINDS = {"slow", "watchdog-restart"}  # what the grace period after a start holds back
PERSONAS = {"moderation": "vergil", "vergil": "vergil", "votes": "vergil", "alerts": "superintendent", "servers": "superintendent",
            "games": "superintendent", "joins": "superintendent", "content": "huragok", "audit": "huragok", "watch": "vergil", "calls": "vergil",
            "seeding": "superintendent", "notify": "superintendent", "requests": "vergil", "tickets": "vergil", "reports": "vergil", "appeals": "vergil", "contact": "vergil"}
LOOKALIKE = 0.85  # how like a staff member's a name has to be, to say so
LOOKS_THE_SAME = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "!": "l", "3": "e", "4": "a", "@": "a", "5": "s", "$": "s", "7": "t"})

# Every event a feed cares about, and who makes its card. The ones that need more than the event are methods below.
ABOUT_SOMEONE = (("target_id", "target_name"), ("player_id", "player_name"))
NAME_IS_THE_NEWS = {"automod.name"}
SIMPLE: dict[str, Callable[[DiscordConfig, Event], Card | None]] = {
    "rcon.cheat": cards.cheat,
    "commands.request": cards.request,
    "automod.strike": cards.strike,
    "automod.name": cards.vergil_line("name-kick", "vergil.name_kick", player="player_name", caught="caught_by"),
    "automod.heard": cards.heard,
    "automod.name_asked": cards.name_asked,
    "automod.name_banned": cards.vergil_line("name-ban", "vergil.name_banned", name="text"),
    "automod.name_allowed": cards.vergil_line("name-allowed", "vergil.name_allowed", name="text"),
    "automod.words_banned": cards.words_banned,
    "automod.word_allowed": cards.vergil_line("word-allowed", "vergil.word_allowed", word="word"),
    "automod.rule_changed": cards.vergil_line("rule-changed", "vergil.rule_changed", rule="rule", line="line"),
    "automod.pardon": cards.pardon,
    "rcon.vote": cards.vote,
    "server.crashed": cards.alert("crashed", "alert.crashed", why="why"),
    "server.faulted": cards.faulted,
    "server.hung": cards.alert("hung", "alert.hung"),
    "server.start_failed": cards.alert("start-failed", "alert.start_failed", why="why"),
    "supervisor.update_failed": cards.alert("update-failed", "alert.update_failed", version="version", why="why"),
    "supervisor.relay_lockout": cards.alert("relay-lockout", "alert.relay_lockout", "limits", address="who"),
    "rcon.auth_failed": cards.alert("rcon-sign-in", "alert.rcon_sign_in", why="error"),
    "watchdog.health": cards.health,
    "supervisor.part_disabled": cards.part_off,
    "supervisor.part_enabled": cards.part_back,
    "supervisor.started": cards.huragok_started,
    "server.ready": cards.server_card("started", "server.started"),
    "server.adopted": cards.server_card("adopted", "server.adopted"),
    "server.restarted": cards.server_restarted,
    "server.restart_planned": cards.restart_planned,
    "server.countdown": cards.countdown,
    "server.program_updated": cards.server_card("program-updated", "server.program_updated", version="version"),
    "server.workshop_updated": cards.workshop_updated,
    "server.repaired": cards.server_card("repaired", "server.repaired", text="text"),
    "server.workshop_update": cards.workshop_waiting,
    "byplayers.changed": cards.playlist_changed,
    "supervisor.commands_reloaded": lambda config, event: cards.audit("settings", say(config, "audit.commands"), config),
}
BACK_UP = {"server.ready", "server.adopted", "server.restarted", "server.recovered"}  # a server's problem is over
TAKES_NEWS_DOWN = {"server.restarted", "server.crashed", "server.stopped", "server.countdown_cancelled", "server.plan_cancelled"}  # a restart that's been and gone, or a call to come and fill a server that's down
NEWS_THAT_GOES = {"servers-restart-planned", "servers-countdown", "seeding-wanted"}
EVENTS = (*SIMPLE, "server.countdown_cancelled", "server.plan_cancelled", "commands.request_decided", "moderation.action", "moderation.seen", "server.stopped", "server.recovered", "rcon.phase", "rcon.join", "rcon.leave", "rcon.refused", "rcon.connected")
GALLERY_PER_POST = 10  # a message holds 10 cards


@dataclass
class Burst:
    """A burst of one message in one place: the first posted, the rest waiting for the window to end."""

    until: float
    waiting: list[Card] = field(default_factory=list)


@dataclass
class Problem:
    since: float
    posted: dict[str, float] = field(default_factory=dict)


def skeleton(name: str) -> str:
    """What a name looks like at a glance: no accents, capitals or spacing, and 0 the same as o, 1 and I the same as l."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return "".join(c for c in plain.casefold().translate(LOOKS_THE_SAME) if not c.isspace() and c not in "_-.")


class Feeds:
    def __init__(self, part: DiscordPart, *, clock: Callable[[], float] = time.time) -> None:
        self.part = part
        self.clock = clock
        self.said_public: set[Any] = set()
        self.bursts: dict[tuple[str, str | None], Burst] = {}
        self.problems: dict[str, Problem] = {}
        self.started: dict[str, float] = {}  # server -> when it last came up: the grace period
        self.games: dict[str, float] = {}  # server -> when its game started: a `finished` without one is the next game's
        self.blocked: dict[str, str] = {}  # where it's posted -> why Discord refused it

    @property
    def config(self) -> DiscordConfig:
        return self.part.file

    # What's happened -----------------------------------------------------------------------------------------------

    def handle(self, event: Event) -> None:
        if self.config.mode == "off":
            return
        card = self.card_for(event)
        if card is not None:
            self.deliver(card)
        if event.name == "rcon.join":
            for found in self._watch(event):
                self.deliver(found)
        if event.name in BACK_UP:
            back = self._recovered(event)
            if back is not None:
                self.deliver(back)

    def _by_name_now(self, event: Event) -> Event:
        """Whoever a card's about goes by the name they were last seen under: not the one they did it under, nor the ID someone typed. A name that's itself what happened stays."""
        if event.name in NAME_IS_THE_NEWS:
            return event
        changes = {name_key: now for id_key, name_key in ABOUT_SOMEONE if (player_id := str(event.get(id_key) or "")) and (now := self.part.name_of(player_id, str(event.get(name_key) or ""))) != event.get(name_key)}
        return Event(event.name, event.server, {**event.data, **changes}, event.time) if changes else event

    def card_for(self, event: Event) -> Card | None:
        config = self.config
        event = self._by_name_now(event)
        name = event.name
        if name == "rcon.connected":
            self.part.tickets.signed_in(event.server or "")
            return None
        if name == "supervisor.started":
            self.part.tickets.update_worked()
        if name in TAKES_NEWS_DOWN:
            self._news_over(event.server or "")
            if name == "server.countdown_cancelled":
                return None
        if name == "watchdog.health" and event.get("level") == "healthy":
            self.part.tickets.slow_over(event.server or "")
            problem = self.problems.get(event.server or "")
            if problem is not None:  # it's over: slow again is news, not a repeat, and this was never downtime for a start to say it's back from
                for kind in SLOW_KINDS:
                    problem.posted.pop(kind, None)
                if not problem.posted:
                    self.problems.pop(event.server or "", None)
        if name in SIMPLE:
            card = SIMPLE[name](config, event)
            if card is not None and (name == "automod.heard" or (name == "automod.strike" and not event.get("dry_run"))):
                return self._strike(card, event)
            if name in ("automod.name_banned", "automod.name_allowed") and event.get("key"):
                self._name_answered(event)
            return card
        if name in ("moderation.action", "moderation.seen"):
            return self._case(event)
        if name == "commands.request_decided":
            self.part.tickets.request_decided(event)
            return None
        if name == "server.stopped":  # one that wasn't expected is server.crashed
            if event.get("expected") is False:
                return None
            if event.get("by") or event.get("manual"):
                self.part.tickets.stopped_by_someone(event.server or "", str(event.get("by") or "someone"))
            return cards.server_card("stopped", "server.stopped")(config, event)
        if name == "rcon.phase":
            return self._phase(event)
        if name in ("rcon.join", "rcon.leave", "rcon.refused"):
            return self._join(event)
        return None

    def _by_real_name(self, event: Event) -> Event:
        """On a card, someone acting from Discord goes by their name there. `shown_as` is for what players read."""
        real = self.part.real_names.get(str(event.get("by_id") or ""))
        return Event(event.name, event.server, {**event.data, "by": real}, event.time) if real else event

    def _news_over(self, server: str) -> None:
        """What was said in `info` about this server that isn't so any more goes, rather than stay until its time's up."""
        for kind in NEWS_THAT_GOES:
            key = f"news:{kind}:{server}"
            message, channel = self.part.status.stored(key)
            if message and channel:
                tidy.later(self.part, channel, message, 0.0, "news that's over")
                self.part.status.forget(key)
        tidy.sweep(self.part)

    def _strike(self, card: Card, event: Event) -> Card | None:
        """A strike on someone who's been reported is an answer to that report, whoever's it was, so the report's card says so and nobody strikes them again for the same thing. A moderator's, from a card or typed, is that card's.
        Vergil's has its card as well, as it's the word list and not the report it's about: one live card per player, as an evening's worth of strikes shouldn't be a channel's worth of cards."""
        player_id = str(event.get("player_id") or "").lower()
        tickets = self.part.tickets
        reported = tickets.ongoing(f"report:{player_id}") if player_id else 0
        if not event.get("manual"):
            if reported:
                tickets.add_line(reported, card.line, when=event.time, touch=False)
                tickets.show(reported)
            if player_id:
                card.problem = f"strike:{player_id}"
            return card
        card.owner, card.decided = PEOPLE, True
        pressed = tickets.take(Event("moderation.action", event.server, {"target_id": player_id, "by_id": event.get("by_id")}))
        number = pressed or reported
        if not number:
            return card
        written = card.fields[0][1] if card.fields else ""
        tickets.add_line(number, card.line + (f": {written}" if written else ""), when=event.time)
        tickets.touched(number)
        tickets.show(number)
        self.part.reports.upheld(number)
        return None

    def _name_answered(self, event: Event) -> None:
        """A name Vergil asked about, banned or allowed however it was (its card's buttons, /banname, in game): its card is done."""
        tickets = self.part.tickets
        if number := tickets.ongoing(str(event["key"])):
            who = self.part.real_names.get(str(event.get("by_id") or "")) or str(event.get("by") or "someone")
            tickets.close(number, who, key="ticket.name_banned" if event.name == "automod.name_banned" else "ticket.name_allowed")

    def _case(self, event: Event) -> Card | None:
        """A person's moderation, as a card of its own with what the player said before it. Not for what's no one's decision; not for Vergil's, whose strike is the card; and not for
        what's pressed on a ticket, which is that ticket's."""
        if is_housekeeping(event.get("evidence")) or event.get("source") == "automod":
            return None
        pressed = self.part.tickets.take(event)
        self.part.tickets.of_player(str(event.get("target_id") or ""), leaving=pressed)
        on_a_report, on_an_appeal = self.part.reports.case(event, pressed), self.part.appeals.case(event, pressed)  # both: one act can answer a report and an appeal
        if pressed or on_a_report or on_an_appeal:  # that card's, as it's the one incident
            return None
        card = cards.moderation(self.config, self._by_real_name(event))
        if card is None:
            return None
        options = self.config.section("tickets")
        if not options.get("case_numbers", True):
            card.footer = ""
        player_id = str(event.get("target_id") or "")
        lines = int(options.get("evidence_lines", 0) or 0)
        if player_id and lines:
            said = self.part.said_by(player_id, lines, event.time)
            if said:
                card.fields.append((say(self.config, "moderation.said"), "\n".join(cards.clean(line) for line in said)[:cards.MAX_FIELD]))
        return card

    def _watch(self, event: Event) -> list[Card]:
        """What about someone joining is worth staff knowing."""
        config = self.config
        options = config.section("feed", "watch")
        player_id = str(event.get("id") or "").lower()
        if not player_id or not features.BY_KEY["feed.watch"].is_on(config):
            return []
        found: list[Card] = []
        if features.BY_KEY["notes"].is_on(config):
            notes = config.section("notes")
            colours = notes.get("flags") or {}
            for row in self.part.flags_of(player_id):
                if row["flag"] in (notes.get("watch_flags") or []):
                    found.append(cards.flagged(config, event, str(row["flag"]), str(row["why"]), str(colours.get(row["flag"]) or "")))
        kicked = self.part.kicked_lately(player_id, float(options.get("back_after_kick_minutes", 15) or 0) * 60, event.time)
        if kicked is not None:
            found.append(cards.watch(config, event, "back-after-kick", "watch.back", minutes=cards.minutes_words(event.time - float(kicked["time"])), by=cards.clean(kicked["by"]),
                                     reason=f": {cards.clean(kicked['reason'])}" if kicked["reason"] else ""))
        name = str(event.get("name") or "")
        staff = self._lookalike(player_id, name)
        if staff:
            found.append(cards.watch(config, event, "staff-lookalike", "watch.lookalike", staff=cards.clean(staff)))
        if self.part.last_visit(player_id) is None and len(self.part.players_on(event.server or "")) <= int(options.get("quiet_players", 2) or 0):
            found.append(cards.watch(config, event, "first-time-quiet", "watch.quiet", players=len(self.part.players_on(event.server or ""))))
        elsewhere = self.part.banned_elsewhere(player_id, event.server or "")
        if elsewhere:
            found.append(cards.watch(config, event, "banned-elsewhere", "watch.banned", where=cards.clean(", ".join(elsewhere))))
        return found

    def _lookalike(self, player_id: str, name: str) -> str:
        """A staff member's name this is close to, or the same as, when it isn't them. Names can't be trusted: IDs can."""
        wanted = skeleton(name)
        if not wanted:
            return ""
        for staff_id, staff_name in self.part.staff_names():
            if staff_id != player_id and difflib.SequenceMatcher(None, wanted, skeleton(staff_name)).ratio() >= LOOKALIKE:
                return staff_name
        return ""

    def _recovered(self, event: Event) -> Card | None:
        problem = self.problems.pop(event.server or "", None)
        if self.part.tickets.problem_over(event.server or ""):  # it says so on its own card
            return None
        if problem is None or not self.config.section("feed", "alerts").get("recovery_summary", True):
            return None
        card = cards.alert("recovered", "alert.recovered", "dust")(self.config, event)
        card.line = say(self.config, "alert.recovered", minutes=cards.minutes_words(self.clock() - problem.since))
        return card

    def _phase(self, event: Event) -> Card | None:
        server, phase = event.server or "", str(event.get("phase") or "")
        players = len(self.part.players_on(server))
        if phase == "playing":
            self.games[server] = self.clock()
            return cards.game_started(self.config, event, players) if players >= self._min_players() else None
        if phase == "finished" and server in self.games:
            minutes = (self.clock() - self.games.pop(server)) / 60
            return cards.game_ended(self.config, event, players, minutes) if players >= self._min_players() else None
        return None

    def _min_players(self) -> int:
        return int(self.config.section("feed", "games").get("min_players", 0) or 0)

    def _join(self, event: Event) -> Card | None:
        """Vergil has recorded the join by now, so it's Vergil who says when they were last here."""
        if event.name == "rcon.refused":
            return cards.join(self.config, event, "refused")
        if event.name == "rcon.leave":
            return cards.join(self.config, event, "left")
        player_id = str(event.get("id") or "")
        before = self.part.last_visit(player_id)
        if before is None:
            return cards.join(self.config, event, "first")
        old = self.part.old_name(player_id, str(event.get("name") or ""), event.time)
        if old:
            return cards.join(self.config, event, "new-name", name=old)
        days = float(self.config.section("feed", "joins").get("returning_days", 30) or 30)
        if event.time - before >= days * 86400:
            return cards.join(self.config, event, "back")
        return cards.join(self.config, event, "joined")

    # Where it goes -------------------------------------------------------------------------------------------------

    def wanted(self, card: Card) -> dict[str, Any] | None:
        """The feed's settings, if it's on and this message of it is wanted."""
        feature = features.feature_of_feed(card.feed)
        if feature is None or feature.stage > features.BUILT or not feature.is_on(self.config):
            return None
        line = self.config.section("messages").get(card.message)
        if not isinstance(line, dict) or not line.get("post", True):
            return None
        options = {**self.config.data["feeds"], **self.config.section(*feature.table)}
        servers = options.get("servers", "*")
        if card.server and servers != "*" and "*" not in servers and card.server not in servers:
            return None
        sources = options.get("sources")
        if isinstance(sources, list) and "*" not in sources and card.source and card.source not in sources:
            return None
        return {**options, **line}

    def deliver(self, card: Card) -> None:
        options = self.wanted(card)
        if options is None or not self._let_through(card, options):
            return
        feature = features.feature_of_feed(card.feed)
        if feature is not None and feature.staff and not options.get("webhook") and self.reads_by_everyone(options.get("channel"), allowed=bool(options.get("public"))):
            return
        if card.decided and self.shape_for(card.message, options) == TICKET:  # a person's doing is on the record, wherever its line says tickets of its kind go
            options = {**options, "channel": "logs"}
        shape = self.shape_for(card.message, options)
        if self.config.mode == "bot" and not str(options.get("webhook") or "").strip() and (shape == TICKET or (shape == LOG and card.subject)):
            if card.feed == "alerts" and card.server and card.kind in PROBLEM_KINDS:
                card.problem = card.server
            elif card.feed == "alerts" and card.server and card.kind in OWN_PROBLEM_KINDS:
                card.problem = f"{OWN_PROBLEM_KINDS[card.kind]}{card.server}"
            elif card.feed == "alerts" and card.kind == "update-failed":
                card.problem = "update"  # one card however many tries, and it closes when one works
            self.part.tickets.open(card, options)
            self._copies(card, options, self._form(card, options, one_line=False))
            return
        if card.merge:
            key = (card.message, card.server)
            now = self.clock()
            burst = self.bursts.get(key)
            if burst is not None and now < burst.until:
                burst.waiting.append(card)
                return
            if burst is not None and burst.waiting:  # its window ended unseen: what waited goes first
                self._send(cards.merged(burst.waiting), options)
            self.bursts[key] = Burst(now + float(options.get("merge_seconds", 30) or 0))
        self._send(card, options)

    def shape_for(self, kind: str, options: dict[str, Any]) -> str:
        """What this kind of message is, wherever it's sent. A channel [channels] names says so; one of its own keeps what this line is by default, so moving a kind of card to its own channel doesn't turn it into something else.
        `as` on its line overrules either."""
        told = str(options.get("as") or "").strip().lower()
        if told in (TICKET, LOG):
            return told
        if told == "post":
            return FEED
        channel = options.get("channel")
        shape = shape_of(channel)
        if shape == FEED and str(channel or "").strip() not in self.config.section("channels"):
            usual = (defaults()["messages"].get(kind) or {}).get("channel")
            return shape_of(usual) if usual else FEED
        return shape

    def everyone_reads(self, reference: Any) -> bool:
        """Only what's known counts: with Discord not read yet, it's no."""
        facts = self.part.facts
        channel = facts.find_channel(self.config.channel_ref(reference)) if facts is not None and facts.guild is not None else None
        return channel is not None and facts.everyone_reads(channel)

    def reads_by_everyone(self, reference: Any, *, allowed: bool = False) -> bool:
        """A channel anyone can read, with nothing saying that's wanted: private things don't go there. Only what's known counts: with Discord not read yet, it goes."""
        facts = self.part.facts
        channel = facts.find_channel(self.config.channel_ref(reference)) if facts is not None and facts.guild is not None else None
        if channel is None or allowed or self.config.section("feeds").get("public") or not facts.everyone_reads(channel):
            return False
        if channel.get("id") not in self.said_public:
            self.said_public.add(channel.get("id"))
            log.warning("Private things aren't posted in #%s, as @everyone can read it: make it private, or set [feeds] public = true", channel.get("name"))
        return True

    def reaches(self, card: Card) -> bool:
        """Whether a card would have somewhere to go: it's wanted, and its channel or webhook isn't one Discord refused."""
        options = self.wanted(card)
        if options is None:
            return False
        webhook = str(options.get("webhook") or "").strip()
        if webhook or self.config.mode == "webhook":
            return bool(webhook) and f"webhook:{webhook}" not in self.blocked
        channel = self.channel_id(options)
        feature = features.BY_KEY.get(f"feed.{card.feed}")
        if feature is not None and feature.staff and self.reads_by_everyone(options.get("channel"), allowed=bool(options.get("public"))):
            return False  # it would be held back: nobody should be told it's reached anyone
        return bool(channel) and f"channel:{channel}" not in self.blocked

    def flush(self, *, everything: bool = False) -> None:
        """Bursts whose window is over (all of them, as Huragok stops), as one card each."""
        now = self.clock()
        for key, burst in list(self.bursts.items()):
            if everything or now >= burst.until:
                if burst.waiting:
                    card = cards.merged(burst.waiting)
                    options = self.wanted(card)
                    if options is not None:
                        self._send(card, options)
                del self.bursts[key]

    def _let_through(self, card: Card, options: dict[str, Any]) -> bool:
        """An ongoing problem is posted once in `repeat_minutes`, and performance alerts wait out a start."""
        if card.feed != "alerts" or not card.server or card.kind not in PROBLEM_KINDS:
            return True
        now = self.clock()
        if card.kind in SLOW_KINDS and now - self.started.get(card.server, 0.0) < float(options.get("grace_seconds", 0) or 0):
            return False
        problem = self.problems.setdefault(card.server, Problem(now))
        last = problem.posted.get(card.kind)
        if last is not None and now - last < float(options.get("repeat_minutes", 30) or 0) * 60:
            return False
        problem.posted[card.kind] = now
        return True

    def ping_of(self, options: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        """The roles this message pings, if any."""
        facts = self.part.facts
        found: list[str] = []
        for ping in pings_of(options):
            role = facts.find_role(ping) if facts is not None else None
            role_id = str(role.get("id")) if role else ping if ping.isdecimal() else ""
            if role_id and role_id not in found:
                found.append(role_id)
        return "".join(f"<@&{role_id}> " for role_id in found), tuple(found)

    def channel_id(self, options: dict[str, Any]) -> str:
        reference = self.config.channel_ref(options.get("channel"))
        facts = self.part.facts
        channel = facts.find_channel(reference) if facts is not None else None
        return str(channel.get("id")) if channel else reference if reference.isdecimal() else ""

    def persona_of(self, card: Card, options: dict[str, Any]) -> str:
        return str(options.get("persona") or PERSONAS.get(card.feed, "huragok"))

    def _form(self, card: Card, options: dict[str, Any], *, one_line: bool) -> dict[str, Any]:
        mention, roles = self.ping_of(options)
        mention += "".join(f"<@{user}> " for user in card.pings)
        silent = bool(options.get("silent", True)) and not roles and not card.pings
        if one_line:
            return message(mention + cards.compact(card), silent=silent, ping_roles=roles, ping_users=card.pings)
        return message(mention, embeds=[cards.embed(card, colour_value(self.config, card.colour or options.get("colour")))], silent=silent, ping_roles=roles, ping_users=card.pings)

    def _send(self, card: Card, options: dict[str, Any], *, one_line: bool | None = None) -> None:
        """A post. In a log channel it's one line, as there's nothing on it to act on."""
        config = self.config
        if one_line is None:
            one_line = options.get("style") == "compact" or self.shape_for(card.message, options) == LOG
        form = self._form(card, options, one_line=one_line)
        persona = self.persona_of(card, options)
        priority = ALERT if card.urgent else MODERATION if card.feed in ("moderation", "vergil", "votes") else STATUS
        what = f"{card.message}"
        webhook = str(options.get("webhook") or "").strip()
        if webhook or config.mode == "webhook":
            if webhook and f"webhook:{webhook}" not in self.blocked:
                self.part.poster.post(persona, form, webhook=webhook, priority=priority, what=what, tag=f"webhook:{webhook}")
        else:
            channel = self.channel_id(options)
            if channel and f"channel:{channel}" not in self.blocked:
                lasting = float(options.get("remove_after_minutes", 0) or 0) * 60  # only true for a while, so it doesn't stay in a channel people read

                def posted(reply: Any, channel: str = channel) -> None:
                    new = str(reply.data.get("id") or "") if isinstance(reply, Reply) and isinstance(reply.data, dict) else ""
                    if lasting and new:
                        tidy.later(self.part, channel, new, lasting, card.message)
                    if new and card.message in NEWS_THAT_GOES and card.server:
                        self.part.status.store(f"news:{card.message}:{card.server}", message=new, value=channel)

                self.part.poster.post(persona, form, channel=channel, priority=priority, what=what, tag=f"channel:{channel}", on_done=posted, lasts=lasting)
        self._copies(card, options, form)

    def _copies(self, card: Card, options: dict[str, Any], form: dict[str, Any]) -> None:
        """To a partner's Discord: what's posted, without buttons, which only work from the bot's own posts."""
        for address in options.get("also") or []:
            address = str(address).strip()
            if address and f"webhook:{address}" not in self.blocked:
                self.part.poster.post(self.persona_of(card, options), form, webhook=address, priority=ALERT if card.urgent else MODERATION if card.feed in ("moderation", "vergil", "votes") else STATUS,
                                      what=f"{card.message}, a copy", tag=f"webhook:{address}")

    # Refusals ------------------------------------------------------------------------------------------------------

    def refused(self, tag: str, why: str) -> None:
        if tag and tag not in self.blocked:
            self.blocked[tag] = why
            what = "A channel stops being posted to" if tag.startswith(("channel:", "webhook:")) else "A channel's name, topic or voice status stops being changed"
            log.warning("%s: Discord refused it (%s). `/huragok check` or J says what's wrong", what, why)

    def unblock(self) -> None:
        """Discord was read again, or the file changed: worth one more try."""
        self.blocked.clear()

    def blocked_lines(self) -> list[str]:
        """For the dashboard. Never a webhook's address: that's a password."""
        lines = []
        for tag, why in self.blocked.items():
            kind, _, place = tag.partition(":")
            facts = self.part.facts
            channel = facts.find_channel(place) if facts is not None and kind != "webhook" else None
            where = f"#{channel.get('name')}" if channel else "a channel" if kind != "webhook" else "a webhook"
            doing = {"rename": "renaming", "voice": "the voice status of"}.get(kind, "posts to")
            lines.append(f"Discord refused {doing} {where}: {why}")
        return lines

    def server_up(self, server: str) -> None:
        self.started[server] = self.clock()

    # The gallery ---------------------------------------------------------------------------------------------------

    def gallery(self) -> list[list[dict[str, Any]]]:
        """Every message there is, in its colour, with where it goes: cards in tens, for /huragok gallery."""
        config = self.config
        posts: list[list[dict[str, Any]]] = [[]]
        made = {card.message: card for card in sample_cards(config)}
        for key, line in config.section("messages").items():
            if not isinstance(line, dict):
                continue
            card = made.get(key)
            state = "" if line.get("post", True) else " · off"
            reference = str(line.get("channel") or "")
            shown = f"#{self._channel_name(reference)} ({shape_of(reference)})"
            form = cards.embed(Card(card.feed if card else "", card.kind if card else "", card.line if card else key, f"{key} → {shown}{state}"),
                               colour_value(config, line.get("colour")))
            form["footer"] = {"text": str(line.get("colour") or "")}
            if len(posts[-1]) == GALLERY_PER_POST:
                posts.append([])
            posts[-1].append(form)
        return [post for post in posts if post]

    def _channel_name(self, reference: str) -> str:
        facts = self.part.facts
        channel = facts.find_channel(self.config.channel_ref(reference)) if facts is not None else None
        return str(channel.get("name")) if channel else reference


def sample_cards(config: DiscordConfig) -> list[Card]:
    """One of every card, for the gallery, and so a test can tell every line in [messages] has one."""
    alice = {"target_id": "a" * 64, "target_name": "Alice", "by": "Mod", "source": "commands", "reason": "spam", "duration": None,
             "dry_run": False, "ok": True, "text": "", "case": 1432}
    made: list[Card | None] = []
    for kind in cards.VERBS:
        made.append(cards.moderation(config, Event("moderation.action", "Server 1", {**alice, "kind": kind, "duration": "1h" if kind == "mute" else None})))
    strike = {"player_id": "a" * 64, "player_name": "Alice", "rule": "insults", "strike": 2, "action": "mute 10m", "evidence": "what they said", "ok": True}
    made.append(cards.strike(config, Event("automod.strike", "Server 1", {**strike, "dry_run": False})))
    made.append(cards.strike(config, Event("automod.strike", "Server 1", {**strike, "dry_run": True})))
    vergil = {"player_name": "Alice", "player_id": "a" * 64, "caught_by": "a banned word", "text": "BadName", "by": "Admin", "word": "word", "words": ["word"],
              "rule": "insults", "line": "word | 10m | 1 | 7d", "strikes": 2, "reason": "a second chance"}
    made.append(cards.heard(config, Event("automod.heard", "Server 1", {**strike, "dry_run": False})))
    made.append(cards.name_asked(config, Event("automod.name_asked", "Server 1", {"player_id": "b" * 64, "player_name": "Bravo", "rule": "insults", "key": "name:bravo"})))
    made += [SIMPLE[name](config, Event(name, "Server 1", vergil)) for name in ("automod.name", "automod.name_banned", "automod.name_allowed", "automod.words_banned",
                                                                                 "automod.word_allowed", "automod.rule_changed", "automod.pardon")]
    made += [cards.vote(config, Event("rcon.vote", "Server 1", {"state": state, "subject": "kick Bravo", "caller": "Alice"})) for state in cards.VOTE_STATES]
    alerts = {"why": "exit code 3", "reason": "5 starts in a row failed", "version": "0.6.0", "who": "an address", "error": "wrong password", "part": "automod",
              "level": "unwell", "previous": "healthy", "reasons": ["the frame rate is low"]}
    made += [SIMPLE[name](config, Event(name, "Server 1", alerts)) for name in ("server.crashed", "server.faulted", "server.hung", "server.start_failed", "supervisor.update_failed",
                                                                                 "supervisor.relay_lockout", "rcon.auth_failed", "watchdog.health", "supervisor.part_disabled", "supervisor.part_enabled",
                                                                                 "supervisor.started")]
    made.append(cards.health(config, Event("watchdog.health", "Server 1", {**alerts, "level": "restart"})))
    recovered = cards.alert("recovered", "alert.recovered", "dust")(config, Event("server.recovered", "Server 1", {}))
    recovered.line = say(config, "alert.recovered", minutes="4")
    made.append(recovered)
    news = {"reason": "the daily restart", "by": "the Superintendent", "when": "after this game", "seconds": 300, "version": "0.9.24", "items": ["Hugegrass"], "text": "Halo 3's files were put back",
            "item": "Hugegrass"}
    made += [SIMPLE[name](config, Event(name, "Server 1", news)) for name in ("server.ready", "server.adopted", "server.restarted", "server.restart_planned", "server.countdown",
                                                                               "server.program_updated", "server.workshop_updated", "server.repaired", "server.workshop_update")]
    made.append(cards.server_card("stopped", "server.stopped")(config, Event("server.stopped", "Server 1", {})))
    game = Event("rcon.phase", "Server 1", {"map": "Guardian", "mode": "Slayer"})
    made += [cards.game_started(config, game, 12), cards.game_ended(config, game, 12, 15.0)]
    made.append(cards.playlist_changed(config, Event("byplayers.changed", "Server 1", {
        "players": 34, "name": "Big Team Battle", "range": "11 to 50 players", "was_name": "Small Slayer"})))
    person = Event("rcon.join", "Server 1", {"id": "a" * 64, "name": "Alice", "reason": "banned"})
    made += [cards.join(config, person, kind, name="OldName") for kind in cards.JOIN_TEXTS]
    made += [cards.flagged(config, person, "watch", "seen spawn killing"),
             cards.watch(config, person, "back-after-kick", "watch.back", minutes="4 minutes", by="Mod", reason=": spam"),
             cards.watch(config, person, "staff-lookalike", "watch.lookalike", staff="Alicé"),
             cards.watch(config, person, "first-time-quiet", "watch.quiet", players=1),
             cards.watch(config, person, "banned-elsewhere", "watch.banned", where="Server 2"),
             cards.cheat(config, Event("rcon.cheat", "Server 1", {"id": "a" * 64, "name": "Alice", "action": "kicked", "detail": "invalid game data (ref 4F2A)"})),
             cards.called(config, "Server 1", "Alice", "someone's team killing", ["Bravo: stop it", "Charlie: lol"]),
             cards.seeding(config, "Server 1", 2), cards.notify_players(config, "Server 1", 12),
             cards.request(config, Event("commands.request", "Server 1", {"by": "Rookie", "what": "kick Alice spam", "target_id": "a" * 64, "target_name": "Alice", "number": 1})),
             cards.opened_by_hand(config, "Mod", "a" * 64, "Alice", "Server 1")]
    made += [cards.audit("notes", say(config, "audit.flag.set", who="Mod", player="Alice", flag="watch"), config),
             cards.audit("power", say(config, "audit.power", who="Admin", what="restart now", server="Server 1"), config),
             cards.reported(config, "Bravo", say(config, "reports.first"), "", "Alice", "Server 1", "camping the spawn", ["get out of my spawn"], ""),
             cards.enough_reports(config, 3, "Alice", "10", say(config, "ticket.number", number=12)),
             cards.message_reported(config, "Bravo", say(config, "reports.first"), "Alice", "general", "https://discord.com/channels/", "come and fight me", ""),
             cards.appeal(config, "Alice (Discord)", "ban", "", "Alice", "Server 1", say(config, "appeals.case", number=1432, what="ban", by="Mod", when="<t:0:R>", why=": slurs in voice"), ["get out of my spawn"], [("What happened?", "I got carried away"), ("Why should the ban be lifted?", "It won't happen again")]),
             cards.contacted(config, "Alice", "Question", "How do I get on the server list?", ""),
             cards.audit("console", say(config, "audit.console", who="Admin", server="Server 1", line="players"), config)]
    made += [cards.audit("settings", say(config, "audit.file"), config), cards.audit("setup", say(config, "audit.setup", who="Host", what="the staff channel is #admin-log"), config)]
    made.append(Card("console", "cleared", say(config, "console.cleared", channel="console", count=41, who="Alice, Bravo", first="20:02:11", last="20:41:07"), "Console"))
    return [card for card in made if card is not None]


__all__ = ["EVENTS", "Feeds", "sample_cards"]
