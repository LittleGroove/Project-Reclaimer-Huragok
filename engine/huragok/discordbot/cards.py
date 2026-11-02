"""What each event says in Discord: pure functions from an event to a Card, so every card can be read in a test.

A card is `feed` and `kind`, which together name its line in [messages]: where it goes, its colour, whether it posts. Staff feeds show names, reasons and chat
lines; no card ever shows an address (`clean()`). Every time in a card is a Discord timestamp, which each reader's Discord counts down by itself. A card's words
are a text key, rewordable in [text]. Headings in the lore voice are the Superintendent's own words (lore.NOTICES).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.events import Event
from ..core.text import describe_seconds, TIME, describe_time
from ..lore import NOTICES
from ..players.ledger import shown_kind
from .config import DiscordConfig
from .speakers import say

ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|\b(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{0,4}\b|\b(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f:]*")
LINE_START = re.compile(r"^(\s*)(#{1,3}\s|-#\s|-\s|\d+\.\s)", re.M)  # what would make a heading or a list of a line, with nothing to show for it but the look
MAX_CARD = 5900
MAX_FOOTER = 1000
MAX_DESCRIPTION = 3800
MAX_FIELD_NAME = 256
MAX_FIELD = 1000


def no_addresses(value: Any) -> str:
    return ADDRESS.sub("(hidden)", str(value or ""))


def clean(value: Any, *, for_a_channel: bool = True) -> str:
    """Text from a player or a server as it may be posted: no addresses, ever, and no formatting of their own, so nothing they type can look like the bot's own heading or hide a link behind words. Not `for_a_channel`: an answer that can't ping anyone, where @1 has to stay copyable."""
    text = re.sub(r"([*_~`|>\\\[\]])", r"\\\1", no_addresses(value))
    text = LINE_START.sub(lambda found: f"{found[1]}\\{found[2]}", text)
    return text.replace("@", "@​") if for_a_channel else text


HURAGOK, PEOPLE = "huragok", "people"  # who a ticket is the doing of


@dataclass
class Card:
    """One thing that happened. `feed`-`kind` is its line in [messages]; the rest is how it reads."""

    feed: str
    kind: str
    line: str  # what happened, in one line: the compact style, and the card's own text
    heading: str = ""  # the card's title
    fields: list[tuple[str, str]] = field(default_factory=list)
    footer: str = ""
    server: str | None = None
    source: str = ""  # who did it, as [feed.moderation] sources names it
    merge: bool = False  # a burst of these becomes one card
    urgent: bool = False
    colour: str = ""  # one of its own, over its line's
    pings: tuple[str, ...] = ()  # members who asked to be told (/notify)
    subject: str = ""  # the player it's about, whom a card's buttons act on: with none, there's nothing to act on, and in a log channel it's a line
    subject_name: str = ""
    owner: str = HURAGOK  # whose doing: which log channel it ends up in
    decided: bool = False  # a person did this: it's on the record, not for anyone to answer
    problem: str = ""  # the server whose ongoing problem this is: one ticket, added to
    rule: str = ""  # the automod rule a strike was on, for pardoning it from its card

    @property
    def message(self) -> str:
        return f"{self.feed}-{self.kind}"


def heading(config: DiscordConfig, notice: str, plain: str) -> str:
    """What it is, at a glance, then the Superintendent's sign: staff work from what it says, and the theme stays."""
    return plain if config.data["voice"] == "plain" else f"{plain} · {NOTICES[notice]}"


def player(event: Event, id_key: str = "target_id", name_key: str = "target_name") -> str:
    name = clean(event.get(name_key) or "")
    if name:
        return f"**{name}**"
    player_id = str(event.get(id_key) or "")
    return f"`{player_id[:8]}…`" if player_id else "someone"


def _reason(event: Event) -> str:
    reason = clean(event.get("reason") or "")
    return f" ({reason})" if reason else ""


def lasting_words(duration: str) -> str:
    """A length, in words, however it was written: as typed, or in seconds as the ledger writes one seen on the server."""
    if duration.endswith("s") and duration[:-1].isdecimal():
        return describe_seconds(int(duration[:-1]))
    return describe_time(duration) if TIME.fullmatch(duration) else clean(duration)


def _lasting(event: Event) -> str:
    duration = str(event.get("duration") or "")
    return f" for {lasting_words(duration)}" if duration else ""


def counted(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


def _because(reason: Any) -> str:
    text = clean(reason or "")
    return f" ({text})" if text else ""


WHEN_WORDS = {"after_game": "after this game", "when_empty": "once everyone has left"}


# Moderation ---------------------------------------------------------------------------------------------------------

SOURCE_NAMES = {"commands": "!", "in game": "/", "automod": "vergil", "vote": "vote", "console": "console", "relay": "relay",
                "window": "window"}
MODERATION_NOTICES = {"kick": "crime", "ban": "crime", "mute": "clean", "warn": "clean"}
VERBS = {kind: f"moderation.{kind}" for kind in ("kick", "ban", "mute", "unmute", "unban", "pardon", "warn")}


def moderation(config: DiscordConfig, event: Event) -> Card | None:
    kind = shown_kind(event.get("kind") or "", event.get("duration"), event.get("reason"))
    if kind not in VERBS or event.get("dry_run"):  # what Vergil would have done is vergil-would's
        return None
    values = dict(by=clean(event.get("by") or "someone"), player=player(event), lasting=_lasting(event), reason=_reason(event))
    line = say(config, VERBS[kind], **values)
    if not event.get("ok", True):
        why = clean(event.get("text") or "")
        line = say(config, "moderation.failed", what=line, why=f" ({why})" if why else "")
    case = event.get("case")
    fields: list[tuple[str, str]] = []
    if event.get("pending"):
        fields.append((say(config, "moderation.when"), say(config, "moderation.pending")))
    return Card("moderation", kind, line, heading(config, MODERATION_NOTICES.get(kind, "citizens"), kind.title()), fields,
                footer=f"{say(config, 'moderation.case')} {case}" if case else "", server=event.server,
                source="discord" if str(event.get("by_id") or "").startswith("discord:") else SOURCE_NAMES.get(str(event.get("source") or ""), str(event.get("source") or "")), merge=True,
                subject=str(event.get("target_id") or ""), subject_name=str(event.get("target_name") or ""), owner=PEOPLE, decided=True)


# Vergil -------------------------------------------------------------------------------------------------------------


def strike(config: DiscordConfig, event: Event) -> Card:
    dry = bool(event.get("dry_run"))
    values = dict(player=player(event, "player_id", "player_name"), rule=clean(event.get("rule")), number=event.get("strike"),
                  action=clean(event.get("action")))
    manual = bool(event.get("manual"))
    by = clean(event.get("by") or "")
    evidence = str(event.get("evidence") or "")
    note = evidence.partition(": ")[2] if manual and ": " in evidence else "" if manual else evidence  # a moderator's says who, and it's on the line
    fields = [(say(config, "vergil.why" if manual else "vergil.said"), clean(note)[:MAX_FIELD])] if note else []
    key = "vergil.would" if dry else "vergil.strike_by" if manual and by else "vergil.strike"
    line = say(config, key, by=by, **values)
    if not dry and not event.get("ok", True):
        line = say(config, "moderation.failed", what=line, why="")
    return Card("vergil", "would" if dry else "strike", line,
                heading(config, "hero", "Would have" if dry else "Strike"), fields, server=event.server, merge=dry,
                subject="" if dry else str(event.get("player_id") or ""), subject_name=str(event.get("player_name") or ""), rule=str(event.get("rule") or ""))


def vergil_line(kind: str, key: str, **names: str) -> Callable[[DiscordConfig, Event], Card]:
    def build(config: DiscordConfig, event: Event) -> Card:
        values = {name: clean(event.get(source)) for name, source in names.items()}
        values["by"] = clean(event.get("by") or "someone")
        about = kind == "name-kick"  # the rest are changes to Vergil's lists, with nobody to act on
        return Card("vergil", kind, say(config, key, **values), heading(config, "citizens", kind.replace("-", " ").title()), server=event.server,
                    subject=str(event.get("player_id") or "") if about else "", subject_name=str(event.get("player_name") or "") if about else "", owner=HURAGOK if about else PEOPLE)

    return build


def words_banned(config: DiscordConfig, event: Event) -> Card:
    words = ", ".join(f"`{clean(word)}`" for word in event.get("words") or [])
    return Card("vergil", "word-ban", say(config, "vergil.words_banned", by=clean(event.get("by")), words=words, rule=clean(event.get("rule"))),
                heading(config, "citizens", "Words banned"), server=event.server)


def pardon(config: DiscordConfig, event: Event) -> Card:
    return Card("vergil", "pardon", say(config, "vergil.pardon", by=clean(event.get("by")), strikes=counted(int(event.get("strikes") or 0), "strike"),
                                        player=player(event, "player_id", "player_name"), reason=_reason(event)),
                heading(config, "citizens", "Pardon"), server=event.server)


# Votes --------------------------------------------------------------------------------------------------------------

VOTE_STATES = ("called", "passed", "failed", "cancelled")


def vote(config: DiscordConfig, event: Event) -> Card | None:
    state = str(event.get("state") or "")
    if state not in VOTE_STATES:
        return None
    return Card("votes", state, say(config, f"vote.{state}", subject=clean(event.get("subject")), by=clean(event.get("caller") or event.get("by") or "someone")),
                heading(config, "citizens", f"Vote {state}"), server=event.server)


# Alerts -------------------------------------------------------------------------------------------------------------


def alert(kind: str, key: str, notice: str = "emergency", **names: str) -> Callable[[DiscordConfig, Event], Card]:
    def build(config: DiscordConfig, event: Event) -> Card:
        values = {name: clean(event.get(source))[:MAX_FIELD] for name, source in names.items()}
        return Card("alerts", kind, say(config, key, **values), heading(config, notice, kind.replace("-", " ").title()), server=event.server, urgent=True)

    return build


def health(config: DiscordConfig, event: Event) -> Card | None:
    """The watchdog's level changing: worse is an alert; back to healthy is what the recovery says."""
    level, previous = str(event.get("level") or ""), str(event.get("previous") or "")
    if level == "healthy" or level == previous:
        return None
    reasons = "; ".join(clean(reason) for reason in event.get("reasons") or [])
    return Card("alerts", "watchdog-restart" if level in ("critical", "restart") else "slow", say(config, "alert.health", level=clean(level), reasons=reasons),
                heading(config, "emergency", "Watchdog"), server=event.server, urgent=True)


def faulted(config: DiscordConfig, event: Event) -> Card:
    card = alert("faulted", "alert.faulted", why="reason")(config, event)
    retry = event.get("retry_at")
    if retry:
        card.line += " " + say(config, "alert.tries_again", when=f"<t:{int(float(retry))}:R>")
    return card


def part_back(config: DiscordConfig, event: Event) -> Card | None:
    """The Discord bot's own: it can't say it's going off, as it's off by then. Coming back it can say it's been away, and what that means for these channels."""
    return Card("alerts", "part-off", say(config, "alert.part_back"), heading(config, "emergency", "Part Off"), urgent=True) if event.get("part") == "discord" else None


def part_off(config: DiscordConfig, event: Event) -> Card:
    return Card("alerts", "part-off", say(config, "alert.part_off", part=clean(event.get("part")), reason=clean(event.get("reason"))),
                heading(config, "emergency", "Part off"), urgent=True)


def huragok_started(config: DiscordConfig, event: Event) -> Card:
    return Card("alerts", "huragok-started", say(config, "alert.huragok_started", version=clean(event.get("version"))), heading(config, "dust", "Huragok started"))


# Servers ------------------------------------------------------------------------------------------------------------


def server_card(kind: str, key: str, **names: str) -> Callable[[DiscordConfig, Event], Card]:
    def build(config: DiscordConfig, event: Event) -> Card:
        values = {name: clean(event.get(source) or "") for name, source in names.items()}
        return Card("servers", kind, say(config, key, **values), heading(config, "dust", kind.replace("-", " ").title()), server=event.server)

    return build


def server_restarted(config: DiscordConfig, event: Event) -> Card:
    """The reason already says who, for what someone asked for."""
    return Card("servers", "restarted", say(config, "server.restarted", reason=_because(event.get("reason"))), heading(config, "dust", "Restarted"), server=event.server)


def restart_planned(config: DiscordConfig, event: Event) -> Card:
    when = str(event.get("when") or "")
    reason = clean(event.get("reason") or "")
    return Card("servers", "restart-planned", say(config, "server.restart_planned", when=WHEN_WORDS.get(when, clean(when)), reason=f": {reason}" if reason else ""), heading(config, "construction", "Restart planned"), server=event.server)


def countdown(config: DiscordConfig, event: Event) -> Card:
    seconds = event.get("seconds") or 0
    when = f"<t:{int(event.time + seconds)}:R>" if seconds else say(config, "server.now")
    return Card("servers", "countdown", say(config, "server.countdown", reason=clean(event.get("reason")) or "A restart", when=when),
                heading(config, "construction", "Countdown"), server=event.server)


def workshop_updated(config: DiscordConfig, event: Event) -> Card:
    items = ", ".join(clean(item) for item in event.get("items") or [])
    return Card("servers", "workshop-updated", say(config, "server.workshop_updated", items=items), heading(config, "dust", "Workshop"), server=event.server)


# Games --------------------------------------------------------------------------------------------------------------


def _game_what(event: Event) -> str:
    return " · ".join(part for part in (clean(event.get("map")), clean(event.get("mode"))) if part) or "A game"


def game_started(config: DiscordConfig, event: Event, players: int) -> Card:
    return Card("games", "started", say(config, "game.started", what=_game_what(event), players=players),
                heading(config, "last train", "Game on"), server=event.server)


def game_ended(config: DiscordConfig, event: Event, players: int, minutes: float) -> Card:
    return Card("games", "ended", say(config, "game.ended", what=_game_what(event), players=players, minutes=minutes_words(minutes * 60)),
                heading(config, "come again", "Game over"), server=event.server)


# Joins --------------------------------------------------------------------------------------------------------------

JOIN_TEXTS = {"first": "join.first", "joined": "join.joined", "left": "join.left", "refused": "join.refused", "new-name": "join.new_name", "back": "join.back"}


def join(config: DiscordConfig, event: Event, kind: str, *, name: str = "") -> Card:
    return Card("joins", kind, say(config, JOIN_TEXTS[kind], player=player(event, "id", "name"), reason=clean(event.get("reason")), old=clean(name)),
                heading(config, "welcome", "Joins"), server=event.server, merge=True)


# The watch list, calls and seeding ------------------------------------------------------------------------------------


def minutes_words(seconds: float) -> str:
    return "under a minute" if seconds < 60 else describe_seconds(seconds)


def watch(config: DiscordConfig, event: Event, kind: str, key: str, **values: Any) -> Card:
    subject = "" if kind == "first-time-quiet" else str(event.get("id") or "").lower()  # someone new is only for the record
    return Card("watch", kind, say(config, key, player=player(event, "id", "name"), **values), heading(config, "hero", "Keep an eye out"), server=event.server,
                subject=subject, subject_name=str(event.get("name") or ""), problem=f"watch:{kind}:{subject}" if subject else "")  # joining again is the same story, on the same card


def flagged(config: DiscordConfig, event: Event, flag: str, why: str, colour: str = "") -> Card:
    """In the flag's own colour, when it has one."""
    card = watch(config, event, "flagged-joined", "watch.flagged", flag=f"**{clean(flag)}**", why=f": {clean(why)}" if why else "")
    card.colour = colour
    return card


def cheat(config: DiscordConfig, event: Event) -> Card:
    return Card("watch", "anti-cheat", say(config, "watch.cheat", player=player(event, "id", "name"), action=clean(event.get("action")), detail=clean(event.get("detail"))),
                heading(config, "crime", "Anti-cheat"), server=event.server, subject=str(event.get("id") or "").lower(), subject_name=str(event.get("name") or ""))


def called(config: DiscordConfig, server: str, name: str, why: str, recent: list[str]) -> Card:
    fields = [(say(config, "calls.recent"), "\n".join(recent)[:MAX_FIELD])] if recent else []
    return Card("calls", "called", say(config, "calls.called", player=f"**{clean(name)}**", why=f": {clean(why)}" if why else ""),
                heading(config, "hero", "A call for an admin"), fields, server=server, urgent=True, owner=PEOPLE)


def seeding(config: DiscordConfig, server: str, players: int) -> Card:
    return Card("seeding", "wanted", say(config, "seeding.wanted", server=clean(server), players=players), heading(config, "citizens", "Come and play"), server=server)


def request(config: DiscordConfig, event: Event) -> Card:
    return Card("requests", "asked", say(config, "requests.asked", by=clean(event.get("by")), what=clean(event.get("what")), slot=event.get("slot") or event.get("number")), heading(config, "citizens", "A request"), server=event.server,
                subject=str(event.get("target_id") or "").lower(), subject_name=str(event.get("target_name") or ""), owner=PEOPLE, problem=f"request:{event.get('number')}")


def reported(config: DiscordConfig, reporter: str, record: str, player_id: str, player_name: str, server: str | None, why: str, said: list[str], problem: str, mention: str = "") -> Card:
    fields = [(say(config, "moderation.said"), "\n".join(clean(line) for line in said)[:MAX_FIELD])] if said else []
    return Card("reports", "made", say(config, "reports.made", reporter=f"**{clean(reporter)}**{mention}", record=record, on=f" on {clean(server)}" if server else "", player=f"**{clean(player_name)}**", why=f": {clean(why)}" if why else ""), heading(config, "citizens", "A report"), fields,
                server=server, subject=player_id, subject_name=player_name, owner=PEOPLE, problem=problem)


def enough_reports(config: DiscordConfig, people: int, player: str, minutes: str, ticket: str) -> Card:
    return Card("reports", "many", say(config, "reports.many", people=people, player=f"**{clean(player)}**", minutes=minutes, ticket=ticket), heading(config, "hero", "A report"), owner=PEOPLE)


def message_reported(config: DiscordConfig, reporter: str, record: str, author: str, channel: str, link: str, quoted: str, why: str, mention: str = "") -> Card:
    fields = [(say(config, "reports.the_message"), clean(quoted)[:MAX_FIELD] or say(config, "reports.no_text"))]
    return Card("reports", "message", say(config, "reports.message", reporter=f"**{clean(reporter)}**{mention}", record=record, author=f"**{clean(author)}**", channel=f"#{clean(channel)}", link=link, why=f": {clean(why)}" if why else ""),
                heading(config, "citizens", "A report"), fields, owner=PEOPLE)


def appeal(config: DiscordConfig, member: str, about: str, player_id: str, player_name: str, server: str | None, case: str, said: list[str], answers: list[tuple[str, str]], *, came_with: str = "", also: list[str] | None = None, mention: str = "") -> Card:
    fields = [(question[:MAX_FIELD_NAME], clean(words)[:MAX_FIELD]) for question, words in answers if words]
    if case:
        fields.append((say(config, "appeals.the_case"), case[:MAX_FIELD]))
    if also:
        fields.append((say(config, "appeals.also"), ", ".join(say(config, f"appeals.about_{kind}") for kind in also)))
    if said:
        fields.append((say(config, "moderation.said"), "\n".join(clean(line) for line in said)[:MAX_FIELD]))
    return Card("appeals", "made", say(config, "appeals.made", member=f"**{clean(member)}**{mention}", what=say(config, f"appeals.about_{about}"), player=f"**{clean(player_name)}**") + (f" {came_with}" if came_with else ""), heading(config, "citizens", "An appeal"), fields,
                server=server, subject=player_id, subject_name=player_name, owner=PEOPLE, problem=f"appeal:{player_id}")


def contacted(config: DiscordConfig, member: str, category: str, text: str, problem: str, mention: str = "") -> Card:
    return Card("contact", "opened", say(config, "contact.opened", member=f"**{clean(member)}**{mention}", category=f" ({clean(category)})" if category else ""), heading(config, "citizens", "Someone needs the staff"),
                [(say(config, "contact.what"), clean(text)[:MAX_FIELD])] if text else [], owner=PEOPLE, problem=problem)


def opened_by_hand(config: DiscordConfig, who: str, player_id: str, name: str, server: str) -> Card:
    return Card("tickets", "opened", say(config, "ticket.opened", who=clean(who), player=f"**{clean(name)}**"), heading(config, "citizens", "A ticket"), server=server or None, subject=player_id, subject_name=name, owner=PEOPLE)


def notify_players(config: DiscordConfig, server: str, players: int) -> Card:
    return Card("notify", "told", say(config, "notify.players", server=clean(server), players=players), heading(config, "citizens", "You asked to be told"), server=server)


def notify_game(config: DiscordConfig, server: str, mode: str, game_map: str) -> Card:
    return Card("notify", "told", say(config, "notify.game", server=clean(server), mode=clean(mode), map=clean(game_map)), heading(config, "citizens", "You asked to be told"), server=server)


# Content and audit --------------------------------------------------------------------------------------------------


def workshop_waiting(config: DiscordConfig, event: Event) -> Card:
    return Card("content", "workshop", say(config, "content.workshop", item=clean(event.get("item"))), heading(config, "construction", "Workshop"), server=event.server)


def audit(kind: str, text: str, config: DiscordConfig, server: str | None = None) -> Card:
    return Card("audit", kind, clean(text), heading(config, "citizens", "Audit"), server=server)


# The form Discord takes ---------------------------------------------------------------------------------------------


def compact(card: Card) -> str:
    where = f"{clean(card.server)} · " if card.server else ""
    return f"{where}{card.line}" + (f" · {card.footer}" if card.footer else "")


def embed(card: Card, colour: int) -> dict[str, Any]:
    """A card, as Discord's embed."""
    footer = " · ".join(part for part in (clean(card.server) if card.server else "", card.footer) if part)[:MAX_FOOTER]
    heading = card.heading[:240]
    room = MAX_CARD - len(heading) - len(footer)  # Discord refuses a card over 6,000 characters in all, so the line goes first and what's after it gets what's left
    line = card.line[:min(MAX_DESCRIPTION, room)]
    room -= len(line)
    form: dict[str, Any] = {"description": line, "color": colour}
    if heading:
        form["title"] = heading
    fields = []
    for name, value in card.fields[:10]:
        name, value = name[:240], value[:MAX_FIELD] or "-"
        if room <= len(name):
            break
        value = value[:room - len(name)]
        if not name or not value:
            break
        room -= len(name) + len(value)
        fields.append({"name": name, "value": value, "inline": False})
    if fields:
        form["fields"] = fields
    if footer:
        form["footer"] = {"text": footer}
    return form


def merged(cards: list[Card]) -> Card:
    """A burst, as one card: the lines of all of them."""
    first = cards[0]
    lines = [card.line for card in cards]
    shown = lines[:30]
    text = "\n".join(shown) + (f"\n…and {len(lines) - len(shown)} more" if len(lines) > len(shown) else "")
    return Card(first.feed, first.kind, text, first.heading, server=first.server, source=first.source)
