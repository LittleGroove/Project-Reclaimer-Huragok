"""Appeals: someone asking staff to lift what's against them, with /appeal, giving their name in game, their player ID or the code their ban's message told them. It's a ticket in `appeals` (`reports` until that's set) with the case and their answers, and
staff lift it, shorten it, or turn it down, saying whether they can ask again; they're told by DM, as someone without a staff role can't see a thread under a staff channel. A ban shortened is a kick with a time, as anything with a time is.

A code is random, kept once it's been given out in a ban's message, and stays the same for that player, so only they can have read it. A name alone is anyone's to type, so staff are told which it was, and an appeal
with only a name doesn't count against the player it names. What anyone says either way is staff's to weigh.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..core.events import Event
from ..core.text import TIME, describe_time
from ..players.ledger import shown_kind
from ..players.targets import FULL_ID
from . import cards, rest
from .config import defaults
from .outbox import ANSWER
from .speakers import say
from .tickets import CLOSED
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

SCHEMA_STEP = """
    CREATE TABLE discord_appeals (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, member TEXT NOT NULL, member_name TEXT NOT NULL, player_id TEXT NOT NULL, player_name TEXT NOT NULL, about TEXT NOT NULL, ticket INTEGER NOT NULL,
        outcome TEXT NOT NULL DEFAULT '', decided REAL);
    CREATE INDEX discord_appeals_member ON discord_appeals (member, time);
    CREATE INDEX discord_appeals_player ON discord_appeals (player_id, time);
    CREATE INDEX discord_appeals_ticket ON discord_appeals (ticket);
    """
CODES_STEP = """
    ALTER TABLE discord_appeals ADD COLUMN proved INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE discord_appeal_codes (code TEXT PRIMARY KEY, player_id TEXT NOT NULL UNIQUE, time REAL NOT NULL);
    """  # whether someone came with their code, or only a name; and each player's code, kept once it's been given out in a ban's message
AGAIN_STEP = """
    ALTER TABLE discord_appeals ADD COLUMN again REAL;
    """  # what whoever denied it said of another: when they can (0: any time), or NEVER. Nothing: [appeals] cooldown_days
NEVER = -1.0
USUAL, NO_MORE = "usual", "never"
LIFTED, SHORTENED, DENIED, CLOSED_AS_IT_IS = "lifted", "shortened", "denied", "closed"
NOT_HOUSEKEEPING = "(evidence IS NULL OR (evidence NOT LIKE 'still muted:%' AND evidence NOT LIKE 'kicked on %'))"  # players.ledger's, as SQL
LIFTS = ("unban", "pardon", "unmute")
LIFTED_BY = {"ban": ("unban",), "kick": ("pardon", "unban"), "mute": ("unmute",)}
CODE = re.compile(r"[0-9A-Fa-f]{6}")
MOST_QUESTIONS = 5  # Discord's limit on a form


class Appeals:
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
        return self.config.section("appeals")

    def takes(self) -> bool:
        return bool(self.options.get("enabled")) and self.config.mode == "bot"

    def code_of(self, player_id: str) -> str:
        """Their code, made the first time it's asked for."""
        player_id = player_id.lower()
        rows = self.db.query("SELECT code FROM discord_appeal_codes WHERE player_id = ?", (player_id,))
        if rows:
            return str(rows[0]["code"])
        while True:
            code = secrets.token_hex(3).upper()
            if not self.db.query("SELECT 1 FROM discord_appeal_codes WHERE code = ?", (code,)):
                self.db.execute("INSERT INTO discord_appeal_codes (code, player_id, time) VALUES (?, ?, ?)", (code, player_id, self.clock()))
                return code

    def codes_used(self) -> bool:
        return "{code}" in str(self.options.get("in_reason") or "")

    def what_else_stands(self, player_id: str) -> str:
        """What's against them that this Discord doesn't take appeals on: what to say instead of "nothing"."""
        now = self.part.tickets.standing(player_id)
        return next((kind for kind, on in (("ban", now["banned"]), ("kick", now["kicked"]), ("mute", now["muted"])) if on), "")

    def what_stands(self, player_id: str) -> str:
        """What's against them that can be appealed here, the heaviest first; "" for nothing."""
        now = self.part.tickets.standing(player_id)
        allowed = self.options.get("for") or ["ban"]
        return next((kind for kind, on in (("ban", now["banned"]), ("kick", now["kicked"]), ("mute", now["muted"])) if on and kind in allowed), "")

    def find_code(self, typed: str) -> tuple[str, str]:
        """(their ID, their name) for a code, which is only theirs to have read; ("", "") for anything else."""
        wanted = typed.strip()
        if not CODE.fullmatch(wanted):
            return "", ""
        rows = self.db.query("SELECT player_id FROM discord_appeal_codes WHERE code = ?", (wanted.upper(),))
        if not rows:
            return "", ""
        player_id = str(rows[0]["player_id"])
        return player_id, self.part.name_of(player_id)

    def find(self, typed: str) -> tuple[str, str]:
        """(their ID, their name) for a code, a player ID, or a name in full, which can be one they've gone by. Of several with a name, the one with something to appeal."""
        wanted = typed.strip()
        found = self.find_code(wanted)
        if found[0]:
            return found
        if FULL_ID.fullmatch(wanted):
            record = self.part.ctx.players.get(wanted.lower())
            return (wanted.lower(), str(record.last_name)) if record is not None else ("", "")
        named = self.part.ctx.players.named(wanted)
        return next((one for one in named if self.what_stands(one[0])), named[0] if named else ("", ""))

    def open_one(self, player_id: str) -> int:
        """Their appeal that's still waiting. One that's been decided and brought back to deal with something else isn't: they can appeal again, when the wait's up."""
        number = self.part.tickets.ongoing(f"appeal:{player_id.lower()}")
        appeal = self.of_ticket(number) if number else None
        return number if appeal is None or not appeal["outcome"] else 0

    def next_allowed(self, member: str, player_id: str, about: str = "") -> float:
        """When they can appeal this again (0: now). Something new against them starts it over. What it waits on is whoever appealed, for that player, whether whoever denied it picked the wait or it's the usual one: another account can still ask. Only what came with the code counts against the player, as only they have it: anyone can type a name, and a stranger's appeal shouldn't lock them out."""
        ruling = self._ruling(member, player_id, about)
        if ruling is not None:
            return float(ruling) if float(ruling) > self.clock() else 0.0
        days = float(self.options.get("cooldown_days", 0) or 0)
        rows = self.db.query("SELECT MAX(COALESCE(decided, time)) AS last FROM discord_appeals WHERE ((member = ? AND player_id = ?) OR (player_id = ? AND proved = 1)) AND outcome IS NOT ? AND (? = '' OR about = ?)", (member, player_id.lower(), player_id.lower(), CLOSED_AS_IT_IS, about, about))  # a Shorten's own kick comes before it's decided
        last = rows[0]["last"] if rows else None
        if not days or last is None:
            return 0.0
        newest = self.db.query("SELECT MAX(time) AS newest FROM actions WHERE target_id = ? AND kind IN ('ban', 'kick', 'mute') AND ok = 1 AND dry_run = 0 AND " + NOT_HOUSEKEEPING, (player_id.lower(),))[0]["newest"]
        if newest is not None and float(newest) > float(last):
            return 0.0
        allowed = float(last) + days * 86400
        return allowed if allowed > self.clock() else 0.0

    def _ruling(self, member: str, player_id: str, about: str = "") -> float | None:
        """What was said of another appeal when the last one this person made, or one with the code, was decided, if anything was, and nothing's come since."""
        rows = self.db.query("SELECT again, COALESCE(decided, time) AS at FROM discord_appeals WHERE player_id = ? AND (member = ? OR proved = 1) AND outcome != '' AND outcome != ? AND (? = '' OR about = ?) ORDER BY COALESCE(decided, time) DESC, number DESC LIMIT 1", (player_id.lower(), member, CLOSED_AS_IT_IS, about, about))
        if not rows or rows[0]["again"] is None:
            return None
        newest = self.db.query("SELECT MAX(time) AS newest FROM actions WHERE target_id = ? AND kind IN ('ban', 'kick', 'mute') AND ok = 1 AND dry_run = 0 AND " + NOT_HOUSEKEEPING, (player_id.lower(),))[0]["newest"]
        return None if newest is not None and float(newest) > float(rows[0]["at"]) else float(rows[0]["again"])

    def no_more(self, member: str, player_id: str, about: str = "") -> bool:
        """Whoever denied this person's last said that's the end of it."""
        return self._ruling(member, player_id, about) == NEVER

    def again_choices(self) -> list[tuple[str, str]]:
        """What Deny offers: (what it's called, what it says)."""
        usual = float(self.options.get("cooldown_days", 0) or 0)
        return [(USUAL, f"Yes, after the usual {usual:g} day{'s' if usual != 1 else ''}" if usual else "Yes, any time"), *[(str(days), f"Yes, after {days} day{'s' if days != 1 else ''}") for days in (1, 7, 30, 90) if days != usual], (NO_MORE, "No more appeals on this")]

    def again_from(self, choice: str) -> float | None:
        """What Deny's choice means, to be kept."""
        if choice == NO_MORE:
            return NEVER
        return self.clock() + float(choice) * 86400 if choice.isdecimal() else None

    def still_kept_out(self, number: int) -> bool:
        """Whether this card's appeal was denied, with a wait that's still going or no more to come: what Let them appeal again is for."""
        appeal = self.of_ticket(number)
        if appeal is None or appeal["outcome"] != DENIED:
            return False
        if appeal["again"] is not None:
            return float(appeal["again"]) == NEVER or float(appeal["again"]) > self.clock()
        return float(appeal["decided"] or 0) + float(self.options.get("cooldown_days", 0) or 0) * 86400 > self.clock()

    def let_again(self, number: int, who: str) -> None:
        """Someone's changed their mind about the wait. They hear, as they've been told otherwise."""
        appeal = self.of_ticket(number)
        if appeal is None or appeal["outcome"] != DENIED:
            return
        self.db.execute("UPDATE discord_appeals SET again = 0 WHERE ticket = ? AND outcome = ?", (number, DENIED))
        self.part.tickets.add_line(number, say(self.config, "appeals.let_again", who=cards.clean(who)))
        self._tell(number, str(appeal["member"]), "let_again", "", "")

    def _next_time_words(self, again: float | None, decided: float) -> tuple[str, str]:
        """What a denial says of another appeal: on the card, and to them."""
        if again == NEVER:
            return say(self.config, "appeals.again_never"), say(self.config, "appeals.told_never")
        when = again if again is not None else decided + float(self.options.get("cooldown_days", 0) or 0) * 86400
        if when <= decided:
            return "", ""
        stamp = f"<t:{int(when)}:R>"
        return say(self.config, "appeals.again_when", when=stamp), say(self.config, "appeals.told_again", when=stamp)

    def questions(self) -> list[str]:
        """What the form asks. Discord refuses a form with nothing on it, so emptying the list brings the usual ones back."""
        asked = [str(question).strip() for question in self.options.get("questions") or [] if str(question).strip()][:MOST_QUESTIONS]
        return asked or [str(question) for question in defaults()["appeals"]["questions"]]

    def file(self, member: str, member_name: str, player_id: str, player_name: str, about: str, answers: list[tuple[str, str]], *, proved: bool = False) -> int:
        """Their appeal, as a ticket: its number, or 0 if it has nowhere to go. A decided appeal's card that's been brought back is an ordinary card by now, so this is a new one. An undecided one from someone with only a name is
        joined, what they said and all, by someone with the code: it's theirs now, and they're who hears how it went."""
        player_id = player_id.lower()
        problem = f"appeal:{player_id}"
        ongoing = self.part.tickets.ongoing(problem)
        if ongoing and not self.open_one(player_id):
            self.db.execute("UPDATE discord_tickets SET problem = '' WHERE number = ?", (ongoing,))
        elif ongoing:
            self.part.tickets.add_line(ongoing, say(self.config, "appeals.joined", who=cards.clean(member_name)))
            for question, said in answers:
                self.part.tickets.add_line(ongoing, f"**{cards.clean(question)}** {cards.clean(said)}"[:290], touch=False)
            self.db.execute("INSERT INTO discord_appeals (time, member, member_name, player_id, player_name, about, ticket, proved) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (self.clock(), member, member_name, player_id, player_name, about, ongoing, int(proved)))
            self.part.tickets.show(ongoing)
            return ongoing
        rows = self.db.query("SELECT * FROM actions WHERE target_id = ? AND kind = ? AND ok = 1 AND dry_run = 0 AND " + NOT_HOUSEKEEPING + " ORDER BY number DESC LIMIT 1", (player_id, "ban" if about == "kick" else about))
        rows = rows or self.db.query("SELECT * FROM actions WHERE target_id = ? AND kind IN ('ban', 'kick', 'mute') AND ok = 1 AND dry_run = 0 AND " + NOT_HOUSEKEEPING + " ORDER BY number DESC LIMIT 1", (player_id,))
        case, server, said = "", None, []
        if rows:
            row = rows[0]
            case = say(self.config, "appeals.case", number=row["number"], what=shown_kind(row["kind"], row["duration"], row["reason"]), by=cards.clean(str(row["by"] or "")), when=f"<t:{int(row['time'])}:R>",
                       why=f": {cards.clean(row['reason'])}" if row["reason"] else "")
            server = str(row["server"] or "") or None
            said = self.part.said_by(player_id, int(self.config.section("tickets").get("evidence_lines", 0) or 0), float(row["time"]))
        also = [kind for kind in ("ban", "kick", "mute") if kind != about and self.part.tickets.standing(player_id)[{"ban": "banned", "kick": "kicked", "mute": "muted"}[kind]]]
        came_with = say(self.config, "appeals.with_code") if proved else say(self.config, "appeals.name_only") if self.codes_used() else ""
        number = self.part.tickets.open_as("appeals-made", cards.appeal(self.config, member_name, about, player_id, player_name, server, case, said, answers, came_with=came_with, also=also, mention=f" (<@{member}>)" if member.isdecimal() else ""), "appeals")
        if number:
            self.db.execute("INSERT INTO discord_appeals (time, member, member_name, player_id, player_name, about, ticket, proved) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (self.clock(), member, member_name, player_id, player_name, about, number, int(proved)))
        return number

    def of_ticket(self, number: int) -> Any:
        rows = self.db.query("SELECT * FROM discord_appeals WHERE ticket = ? ORDER BY number DESC LIMIT 1", (number,))
        return rows[0] if rows else None

    def decide(self, number: int, outcome: str, who: str, *, why: str = "", length: str = "", again: float | None = None) -> None:
        """It's decided: on its card, which is finished, and they're told. `again`: when a denied one can be appealed again, as again_from gives it."""
        appeal = self.of_ticket(number)
        if appeal is None or appeal["outcome"]:
            return
        now = self.clock()
        self.db.execute("UPDATE discord_appeals SET outcome = ?, decided = ?, again = ? WHERE ticket = ? AND outcome = ''", (outcome, now, again, number))  # everyone who appealed on this card: each is waited on as their own
        reason = f": {cards.clean(why)}" if why else ""
        on_card, to_them = self._next_time_words(again, now) if outcome == DENIED else ("", "")
        self.part.tickets.add_line(number, " · ".join(words for words in (say(self.config, f"appeals.{outcome}", who=cards.clean(who), why=reason, length=cards.clean(length)), on_card) if words))
        self._tell(number, str(appeal["member"]), outcome, reason, length, to_them)
        self.part.tickets.finish(number, CLOSED)

    def closed(self, number: int, who: str = "") -> None:
        """Closed with no decision: staff did something else about it, and nothing more's wanted. They were told they'd hear, so they do. It isn't held against them: they can appeal again straight off."""
        appeal = self.of_ticket(number)
        if appeal is not None and not appeal["outcome"]:
            self.db.execute("UPDATE discord_appeals SET outcome = ?, decided = ? WHERE ticket = ? AND outcome = ''", (CLOSED_AS_IT_IS, self.clock(), number))
            self._tell(number, str(appeal["member"]), "closed", "", "")

    def _tell(self, number: int, member: str, outcome: str, why: str, length: str, then: str = "") -> None:
        """By DM, which they may have closed: staff then know they haven't heard."""
        if self.options.get("tell_outcome", "dm") != "dm" or not member:
            return
        config = self.config
        appeal = self.of_ticket(number)
        still = self.what_else_stands(str(appeal["player_id"])) if appeal is not None and outcome == LIFTED else ""
        text = say(config, f"appeals.told_{outcome}", why=why, length=describe_time(length) if TIME.fullmatch(length or "") else length, discord=cards.clean(self.part.guild_name or "the Discord"),
                   player=cards.clean(appeal["player_name"] if appeal is not None else ""), what=say(config, f"appeals.about_{appeal['about']}") if appeal is not None else "it")
        if still:
            text += " " + say(config, "appeals.still_stands", what=say(config, f"appeals.about_{still}"))
        if then:
            text += " " + then

        async def send() -> Any:
            return await to_thread(rest.send_dm, config.token, member, {"content": text, "allowed_mentions": {"parse": []}})

        def refused(error: rest.DiscordError) -> bool:
            log.info("Couldn't tell someone how their appeal went: %s", error)
            self.part.tickets.add_line(number, say(config, "appeals.no_dm"), touch=False)
            self.part.tickets.show(number)
            return True

        self.part.outbox.put(send, priority=ANSWER, bucket="dm", what="telling someone how their appeal went", on_fail=refused)

    def case(self, event: Event, pressed: int = 0) -> bool:
        """What they appealed was lifted some other way, in game say: that decides it. Whether it did."""
        if str(event.get("kind")) not in LIFTS or event.get("dry_run") or not event.get("ok", True):
            return False
        player_id = str(event.get("target_id") or "").lower()
        number = self.open_one(player_id)
        who = str(event.get("by") or "")
        if not number:
            self._second_thoughts(player_id, pressed, who, str(event.get("kind")))
            return False
        if number == pressed or self.what_stands(player_id):  # a press on the appeal's own card decides it itself
            return False
        self.decide(number, LIFTED, who)
        return True

    def _second_thoughts(self, player_id: str, pressed: int, who: str, lifted: str) -> None:
        """They were denied, and then it's lifted after all: they'd otherwise go on believing it stands. Only what they appealed: their mute coming off says nothing about their ban."""
        rows = self.db.query("SELECT * FROM discord_appeals WHERE player_id = ? AND outcome = ? ORDER BY number DESC LIMIT 1", (player_id, DENIED))
        if not rows or self.what_stands(player_id) or lifted not in LIFTED_BY.get(str(rows[0]["about"]), ()):
            return
        row = rows[0]
        self.db.execute("UPDATE discord_appeals SET outcome = ?, decided = ? WHERE number = ?", (LIFTED, self.clock(), row["number"]))
        self.part.tickets.add_line(int(row["ticket"]), say(self.config, "appeals.after_all", who=cards.clean(who)), touch=False)
        self.part.tickets.show(int(row["ticket"]))
        self._tell(int(row["ticket"]), str(row["member"]), LIFTED, "", "")

    def in_the_reason(self, target_id: str, duration: str | None) -> str:
        """What a ban's message adds, for whoever reads it trying to get back in. It's asked as the ban goes out."""
        text = str(self.options.get("in_reason") or "").strip()
        if not text or not self.takes() or ("kick" if duration else "ban") not in (self.options.get("for") or ["ban"]):
            return ""
        return text.replace("{code}", self.code_of(target_id)) if "{code}" in text else text

    def strike_comes_off(self, player_id: str, who: str) -> None:
        """An appeal accepted against what Vergil did: the latest strike stops counting, or the next word starts where this one left off. [appeals] lift_clears_strike says whether."""
        if not self.options.get("lift_clears_strike", True):
            return
        rows = self.db.query("SELECT source FROM actions WHERE target_id = ? AND kind IN ('ban', 'mute') AND ok = 1 AND dry_run = 0 AND " + NOT_HOUSEKEEPING + " ORDER BY number DESC LIMIT 1", (player_id.lower(),))
        if not rows or rows[0]["source"] != "automod":
            return
        try:
            self.db.execute("UPDATE automod_strikes SET pardoned = ?, pardoned_by = ? WHERE number = (SELECT MAX(number) FROM automod_strikes WHERE player_id = ? AND pardoned IS NULL)", (self.clock(), who, player_id.lower()))
        except sqlite3.Error as error:  # Vergil's off, and always has been
            log.info("Couldn't take a strike off: %s", error)


__all__ = ["AGAIN_STEP", "CODES_STEP", "DENIED", "LIFTED", "MOST_QUESTIONS", "NEVER", "SCHEMA_STEP", "SHORTENED", "Appeals"]
