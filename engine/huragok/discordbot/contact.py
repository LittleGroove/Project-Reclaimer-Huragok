"""Contact staff: a button in a channel everyone can read opens a private thread between someone and the staff, for what doesn't fit a form. Only they, whoever `thread_role` names, and anyone who presses Discuss on the card
can see it (and those with Manage Threads, which Discord lets see them all). Staff get a ticket in `reports` as usual, with no player to act on. Closing it locks the thread and keeps a transcript. The bot needs Manage
Threads itself, since only that opens a locked thread again when a closed card comes back (seen on the real thing: 50001 without it, though it can lock its own without).
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from . import cards, features, kept, rest
from .config import colour_value, pings_of
from .speakers import say
from .tickets import LIVE
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

OPEN = "huragok:contact:open"
CARD = "contact:card"
SWEEP_SECONDS = 3600.0
DISCORD_EPOCH_MS = 1420070400000
PAGES = 10  # of 100 messages, in a transcript
SAID = (0, 19)  # what kinds of message are someone saying something: plainly, or as a reply


def time_of(snowflake: str) -> float:
    """When something with this ID was made: Discord's IDs start with their time."""
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000


class Contact:
    def __init__(self, part: DiscordPart, clock: Callable[[], float] = time.time) -> None:
        self.part = part
        self.clock = clock
        self.swept = clock()
        self.closing: set[int] = set()  # being closed now: Close pressed twice kept two transcripts (audit 2026-11, 6.13)

    @property
    def config(self) -> Any:
        return self.part.file

    @property
    def db(self) -> Any:
        return self.part.ctx.db

    @property
    def options(self) -> dict[str, Any]:
        return self.config.section("contact")

    def on(self) -> bool:
        return features.state_of(features.BY_KEY["contact"], self.config, self.part.facts)[0] == features.ON

    def keep_card(self) -> None:
        """The button, while it's wanted. With Discord not read, or the feature only waiting, what's there stays."""
        now = features.state_of(features.BY_KEY["contact"], self.config, self.part.facts)[0]
        if self.part.facts is None or now == features.WAITING:
            return
        channel = kept.channel_of(self.part, self.options.get("channel")) if now == features.ON else ""
        if not channel:
            kept.take_down(self.part, CARD)
            return
        config = self.config
        kept.keep(self.part, CARD, channel, {"embeds": [{"title": say(config, "contact.title"), "description": say(config, "contact.card"), "color": colour_value(config, "blurple")}],
                                             "components": [{"type": 1, "components": [{"type": 2, "style": 1, "label": say(config, "contact.button")[:80], "custom_id": OPEN}]}], "allowed_mentions": {"parse": []}}, pin=True)

    def open_one(self, member: str) -> Any:
        number = self.part.tickets.ongoing(f"contact:{member}")
        return self.part.tickets.get(number) if number else None

    def next_allowed(self, member: str) -> float:
        """When they can open another (0: now): each one is a thread, a card and a ping."""
        wait = float(self.options.get("cooldown_minutes", 0) or 0) * 60
        last = self.db.query("SELECT MAX(opened) AS last FROM discord_tickets WHERE problem = ?", (f"contact:{member}",))[0]["last"]
        allowed = float(last) + wait if wait and last is not None else 0.0
        return allowed if allowed > self.part.status.clock() else 0.0

    def _roles(self) -> list[str]:
        """The roles brought into each thread, if any. Being named in a private thread is what lets them see it."""
        found = []
        for ping in pings_of(self.options.get("thread_role")):
            role = self.part.facts.find_role(ping) if self.part.facts is not None else None
            found.append(str(role.get("id")) if role else ping if ping.isdecimal() else "")
        return [role for role in found if role]

    async def open(self, member: str, member_name: str, category: str, text: str) -> str:
        """A private thread, and a ticket for staff: the thread's ID, or "" if it couldn't be made. It blocks on Discord, as they're waiting to be told where to go."""
        config = self.config
        token = config.token
        line = config.section("messages").get("contact-opened") or {}  # what's looked at before making a thread nobody would hear of
        channel = kept.channel_of(self.part, self.options.get("channel"))
        staff_channel = self.part.feeds.channel_id({"channel": line.get("channel") or "reports"})
        if not channel or not line.get("post", True) or not staff_channel or f"channel:{staff_channel}" in self.part.feeds.blocked:  # staff wouldn't hear: no thread nobody's watching
            return ""
        name = f"{category or say(config, 'contact.title')}: {member_name}"[:100]
        try:
            thread = await to_thread(rest.start_private_thread, token, channel, name)
            await to_thread(rest.add_thread_member, token, str(thread["id"]), member)
        except rest.DiscordError as error:
            log.warning("Couldn't open a private thread for Contact staff: %s", error)
            return ""
        thread_id = str(thread["id"])
        card = cards.contacted(config, member_name, category, text, f"contact:{member}", mention=f" (<@{member}>)")
        number = self.part.tickets.open_as("contact-opened", card, "reports")
        self.db.execute("UPDATE discord_tickets SET thread = ? WHERE number = ?", (thread_id, number))
        self.part.tickets.show(number)
        roles = self._roles()
        opening = {"content": " ".join([f"<@{member}>", *(f"<@&{role}>" for role in roles)]), "embeds": [{"title": category or say(config, "contact.title"), "description": cards.clean(text)[:cards.MAX_DESCRIPTION],
                                                                                                                               "color": colour_value(config, "blurple")}],
                   "allowed_mentions": {"parse": [], "users": [member], "roles": roles},
                   "components": [{"type": 1, "components": [{"type": 2, "style": 3, "label": say(config, "button.close")[:80], "custom_id": f"huragok:t:{number}:leave"}]}]}
        try:
            await to_thread(rest.post_message, token, thread_id, opening)
        except rest.DiscordError as error:
            log.warning("Couldn't post in a Contact staff thread: %s", error)
        return thread_id

    def _messages(self, thread: str) -> list[dict[str, Any]]:
        """Every message in it, oldest first. It blocks."""
        found: list[dict[str, Any]] = []
        before = ""
        for _ in range(PAGES):
            page = rest.call("GET", f"/channels/{thread}/messages?limit=100" + (f"&before={before}" if before else ""), self.config.token)
            if not page:
                break
            found += page
            before = str(page[-1]["id"])
        return list(reversed(found))

    def transcript(self, messages: list[dict[str, Any]]) -> str:
        """What people said: not Discord's own notices, like someone being added. What's in an embed counts: that's where the opening message says what they asked."""
        lines = []
        for message in messages:
            if message.get("type", 0) not in SAID:
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time_of(str(message["id"]))))
            author = (message.get("author") or {}).get("username") or "?"
            words = " ".join([str(message.get("content") or ""), *[f"{embed.get('title') or ''}: {embed.get('description') or ''}" for embed in message.get("embeds") or []]]).strip()
            files = " ".join(str(file.get("url") or "") for file in message.get("attachments") or [])
            lines.append(f"[{when}] {author}: {words} {files}".rstrip())
        return "\n".join(lines) + "\n"

    async def close(self, number: int, who: str) -> None:
        """They're done: a transcript for the record, a word in the thread, and it's locked. The card's finished as any other."""
        ticket = self.part.tickets.get(number)
        if ticket is None or number in self.closing or ticket["state"] not in LIVE:  # before the first wait: a second press finds it taken
            return
        self.closing.add(number)
        try:
            thread, token = str(ticket["thread"]), self.config.token
            tickets = self.part.tickets
            if thread:
                if self.options.get("transcript", True):
                    await self._keep_transcript(number, ticket, thread)
                try:
                    await to_thread(rest.post_message, token, thread, {"content": say(self.config, "contact.closed", who=cards.clean(who)), "allowed_mentions": {"parse": []}})
                    await to_thread(rest.close_thread, token, thread, True)
                except rest.DiscordError as error:
                    log.info("Couldn't close a Contact staff thread: %s", error)
            tickets.close(number, who)
        finally:
            self.closing.discard(number)

    async def _keep_transcript(self, number: int, ticket: Any, thread: str) -> None:
        client = self.part.client
        if client is None or not client.intents.message_content:  # its words read back as nothing
            self.part.tickets.add_line(number, say(self.config, "contact.no_transcript"), touch=False)
            return
        import discord

        try:
            text = self.transcript(await to_thread(self._messages, thread))
            channel = client.get_partial_messageable(int(self.part.tickets.keeps_in(ticket)), guild_id=int(self.config.guild))
            sent = await channel.send(say(self.config, "contact.transcript", ticket=say(self.config, "ticket.number", number=number)), file=discord.File(io.BytesIO(text.encode("utf-8")), f"ticket-{number}.txt"), allowed_mentions=discord.AllowedMentions.none())
            self.part.tickets.add_line(number, say(self.config, "contact.transcript_line", link=sent.jump_url), touch=False)
        except (rest.DiscordError, discord.DiscordException) as error:  # a rate limit too: closing goes on either way
            log.info("Couldn't keep ticket %s's transcript: %s", number, error)
            self.part.tickets.add_line(number, say(self.config, "contact.no_transcript"), touch=False)

    async def sweep(self) -> None:
        """Hourly: threads nobody's written in for `close_after_days` are closed, as they'd otherwise sit on the staff's list for good."""
        days = float(self.options.get("close_after_days", 0) or 0)
        if not days or self.clock() - self.swept < SWEEP_SECONDS:
            return
        self.swept = self.clock()
        for row in self.db.query("SELECT number, thread FROM discord_tickets WHERE problem LIKE 'contact:%' AND state IN ('open', 'active') AND touched <= ? AND thread != ''", (self.clock() - days * 86400,)):
            try:
                channel = await to_thread(rest.call, "GET", f"/channels/{row['thread']}", self.config.token)
            except rest.DiscordError as error:
                if error.status == 404:  # deleted: there's nothing to wait for
                    self.part.tickets.close(int(row["number"]), "Huragok")
                else:
                    log.info("Couldn't look at a Contact staff thread: %s", error)
                continue
            last = str(channel.get("last_message_id") or row["thread"])
            if self.clock() - time_of(last) >= days * 86400:
                await self.close(int(row["number"]), f"Nobody's said anything for {days:g} days, so Huragok")


__all__ = ["CARD", "OPEN", "Contact", "time_of"]
