"""Keeping channels clear. A ticket channel holds what needs acting on and a board holds itself, so what's only useful for a while goes when that's over.

Two kinds, as they behave differently. What only the asker can see can only be touched through its interaction's token, which dies in 15 minutes, so it's done from memory and a restart just leaves one for them to
dismiss (client.answer, and `answered` here). What everyone can see is kept in `discord_tidy`, so a restart loses nothing, and swept once a minute: a run of them as one bulk delete where the bot may, one at a
time where it may not. Refused here never stops a channel being posted to, and a message already gone is the job done. Nothing moves to a log channel: what's worth keeping is already on a card or in a line.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from . import rest
from .outbox import CHAT
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

SCHEMA_STEP = """
    CREATE TABLE discord_tidy (channel TEXT NOT NULL, message TEXT NOT NULL, due REAL NOT NULL, why TEXT NOT NULL DEFAULT '', PRIMARY KEY (channel, message));
    CREATE INDEX discord_tidy_due ON discord_tidy (due);
    """
MOST_AT_ONCE = 200  # gone through in one sweep, a minute apart
MOST_IN_A_BULK = 100  # Discord's
MOST_ALONE = 20  # one at a time, to a channel, in one sweep: older than bulk can reach, or without the permission
BULK_LIMIT_SECONDS = 13 * 86400 + 20 * 3600  # Discord refuses a bulk delete with anything over 14 days in it: this leaves room for clocks
LONGEST_PRIVATE_MINUTES = 14  # the token's good for 15
DISCORD_EPOCH_MS = 1420070400000


def time_of(snowflake: str) -> float:
    """When something with this ID was made: Discord's IDs start with their time."""
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000


def minutes_for(part: DiscordPart, lasts: str) -> float:
    """How long an answer of this kind stays: done, looks, public or tests. 0: until someone dismisses it."""
    minutes = float(part.file.section("answers").get({"done": "done_minutes", "looks": "looks_minutes", "public": "public_minutes", "tests": "tests_minutes"}[lasts], 0) or 0)
    return minutes if lasts in ("public", "tests") else min(minutes, LONGEST_PRIVATE_MINUTES)


def later(part: DiscordPart, channel: str, message: str, seconds: float, why: str = "") -> None:
    """This message goes after this long. It survives a restart."""
    if channel and message:
        part.ctx.db.execute("INSERT OR REPLACE INTO discord_tidy (channel, message, due, why) VALUES (?, ?, ?, ?)", (str(channel), str(message), part.status.clock() + seconds, why))


def later_many(part: DiscordPart, channel: str, messages: list[str], why: str = "") -> None:
    """All of these, as soon as they can go: one commit, as a clear can be hundreds."""
    now = part.status.clock()
    part.ctx.db.execute_many("INSERT OR REPLACE INTO discord_tidy (channel, message, due, why) VALUES (?, ?, ?, ?)", [(str(channel), str(message), now, why) for message in messages if message])


def answered(part: DiscordPart, interaction: Any, sent: Any, private: bool, lasts: str = "looks") -> None:
    """A slash command's answer: a private one gone from their screen after a while, a public one from the channel."""
    if private:
        seconds = minutes_for(part, lasts) * 60
        if seconds and hasattr(sent, "delete"):
            asyncio.ensure_future(sent.delete(delay=seconds))
        return
    minutes = minutes_for(part, "public")
    if minutes and getattr(sent, "id", None):
        later(part, str(interaction.channel_id), str(sent.id), minutes * 60, "a public answer")


async def notices(part: DiscordPart, channel: str, kinds: tuple[int, ...]) -> None:
    """What Discord just said in a channel about something the bot did ("Huragok pinned a message", "started a thread"): the latest few, and only its own. Without the means to, they stay: they do no harm."""
    config = part.file
    try:
        latest = await to_thread(rest.call, "GET", f"/channels/{channel}/messages?limit=5", config.token)
        me = str((part.client.user.id if part.client is not None and part.client.user is not None else "") or "")
        for message in latest or []:
            if message.get("type") in kinds and str((message.get("author") or {}).get("id")) == me:
                later(part, channel, str(message["id"]), 0.0, "Discord's notice")
    except rest.DiscordError as error:
        log.info("Couldn't look for Discord's own notice: %s", error)
    sweep(part)


def sweep(part: DiscordPart) -> None:
    """What's due. Each channel gets its own place in the queue, so deleting never slows posting, and a job still waiting is replaced rather than stacked."""
    if part.facts is None:
        return
    now = part.status.clock()
    for row in part.ctx.db.query("SELECT DISTINCT channel FROM discord_tidy WHERE due <= ?", (now,)):  # one channel it can't reach, or a long backlog in one, doesn't hold the rest back
        channel = str(row["channel"])
        if channel not in part.tidy_paused:
            _channel(part, channel, [str(one["message"]) for one in part.ctx.db.query("SELECT message FROM discord_tidy WHERE channel = ? AND due <= ? ORDER BY due LIMIT ?", (channel, now, MOST_AT_ONCE))])


def _channel(part: DiscordPart, channel: str, messages: list[str]) -> None:
    facts = part.facts
    known = facts.find_channel(channel) if facts is not None else None
    allowed = bool(known) and bool(facts.has("manage_messages", known))
    now = part.status.clock()
    bulk = [message for message in messages if now - time_of(message) < BULK_LIMIT_SECONDS] if allowed and channel not in part.tidy_one_by_one else []
    alone = [message for message in messages if message not in bulk]
    if len(bulk) == 1:
        alone, bulk = [*alone, bulk[0]], []
    config, token, delivery = part.file, part.file.token, part.poster.delivery

    def done(gone: list[str]) -> Any:
        def forget(_reply: Any = None) -> None:
            for message in gone:
                part.ctx.db.execute("DELETE FROM discord_tidy WHERE channel = ? AND message = ?", (channel, message))
        return forget

    def refused(gone: list[str]) -> Any:
        def failed(error: rest.DiscordError) -> bool:
            if error.status == 404:  # already gone: the job's done
                done(gone)()
            elif error.status == 400 and len(gone) > 1:  # one of them can't go in a bulk: one at a time from now on
                part.tidy_one_by_one.add(channel)
            elif error.status == 403:
                part.tidy_paused.add(channel)  # until Discord's read again
                if channel not in part.tidy_said:
                    part.tidy_said.add(channel)
                    log.warning("Messages can't be tidied away in a channel: the bot needs Manage Messages there (%s)", error)
            else:
                log.info("Couldn't tidy a message away: %s", error)
            return True
        return failed

    if len(bulk) % MOST_IN_A_BULK == 1:  # Discord won't bulk-delete one
        alone, bulk = [*alone, bulk[-1]], bulk[:-1]
    for start in range(0, len(bulk), MOST_IN_A_BULK):
        batch = bulk[start:start + MOST_IN_A_BULK]

        async def send_bulk(batch: list[str] = batch) -> Any:
            return await to_thread(rest.bulk_delete, token, channel, batch)

        part.outbox.put(send_bulk, priority=CHAT, bucket=f"tidy:{channel}", key=f"tidy:{channel}:{start}", what="messages no longer wanted", on_done=done(batch), on_fail=refused(batch))
    for message in alone[:MOST_ALONE]:

        async def send_one(message: str = message) -> Any:
            if allowed:  # a person's line would only waste a try through the webhook first
                return await to_thread(rest.delete_message, token, channel, message)
            return await to_thread(delivery.delete, config, channel, message)

        part.outbox.put(send_one, priority=CHAT, bucket=f"tidy:{channel}", key=f"tidy:{channel}:{message}", what="a message no longer wanted", on_done=done([message]), on_fail=refused([message]))


PINNED, THREAD_STARTED = 6, 18  # Discord's own kinds of message
__all__ = ["PINNED", "SCHEMA_STEP", "THREAD_STARTED", "answered", "later", "minutes_for", "notices", "sweep", "time_of"]
