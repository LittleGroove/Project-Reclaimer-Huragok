"""A post that stays: one message Huragok keeps in a channel and keeps right (the console's menu, the Contact staff button). It's posted once, edited when what it says changes, posted again if someone deletes it, and
taken down when it isn't wanted. What it says and where are kept in `discord_posts`, so a restart changes nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from ..core.background import background
from . import rest, tidy
from .outbox import ANSWER, STATUS
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

RETRY_SECONDS = 600.0  # after a post that didn't get through
LOOK_SECONDS = 600.0  # how often to see that one's still there: nothing else would say someone deleted it


def keep(part: DiscordPart, key: str, channel: str, form: dict[str, Any], *, pin: bool = False) -> None:
    """Make it so. It's only worth calling now and then: nothing goes to Discord unless something's changed."""
    status, config, delivery = part.status, part.file, part.poster.delivery
    message, said = status.stored(key)
    old_channel = str(json.loads(said)[0]) if said else ""
    now_says = json.dumps([channel, form], sort_keys=True)
    if said == now_says and message and status.clock() - part.kept_tried.get(key, 0.0) >= LOOK_SECONDS:
        _still_there(part, key, channel, message)
    if said == now_says and (message or status.clock() - part.kept_tried.get(key, 0.0) < RETRY_SECONDS):
        return
    if message and old_channel == channel:
        if part.kept_sending.get(key) == now_says and status.clock() - part.kept_tried.get(key, 0.0) < RETRY_SECONDS:
            return  # on its way
        part.kept_tried[key] = status.clock()
        part.kept_sending[key] = now_says

        def edited(_reply: Any) -> None:  # written down once it's so: an edit lost in an outage or a stop is tried again
            status.store(key, value=now_says)
            part.kept_sending.pop(key, None)

        async def send() -> Any:
            return await to_thread(delivery.edit, config, channel, message, form)

        def failed(error: rest.DiscordError) -> bool:
            """Someone deleted it: a new one next time."""
            if error.status == 404:
                status.forget(key)
                return True
            return False

        part.outbox.put(send, priority=ANSWER, bucket=f"channel:{channel}", key=key, what=f"the post {key}", on_done=edited, on_fail=failed)
        return

    part.kept_tried[key] = status.clock()
    status.store(key, value=now_says)
    if message:
        _delete(part, old_channel, message)
    status.store(key, message="")

    def stored(reply: Any) -> None:
        new = str(reply.data.get("id") or "") if isinstance(reply, rest.Reply) and isinstance(reply.data, dict) else ""
        if new:
            status.store(key, message=new)
            if pin:
                _pin(part, channel, new)

    async def send_new() -> Any:
        return await to_thread(delivery.to_channel, config, channel, "superintendent", form)

    # Keyed, so a later post of it replaces one still waiting: while Discord's API was down, one more copy queued every
    # RETRY_SECONDS, and every one was posted (and pinned) once it was back (audit 2026-11, 6.3)
    form = dict(form)  # this job's own: its nonce stays for each of its tries
    part.outbox.put(send_new, priority=ANSWER, bucket=f"channel:{channel}", key=f"kept:{key}", what=f"the post {key}",
                    on_done=stored)


def _still_there(part: DiscordPart, key: str, channel: str, message: str) -> None:
    status, token = part.status, part.file.token
    part.kept_tried[key] = status.clock()

    async def send() -> Any:
        return await to_thread(rest.call, "GET", f"/channels/{channel}/messages/{message}", token)

    def failed(error: rest.DiscordError) -> bool:
        if error.status == 404:
            status.forget(key)
            return True
        return False

    part.outbox.put(send, priority=STATUS, bucket=f"channel:{channel}", key=f"look:{key}", what=f"seeing that the post {key} is there", on_fail=failed)


def take_down(part: DiscordPart, key: str) -> None:
    message, said = part.status.stored(key)
    if message or said:
        part.status.forget(key)
    if message and said:
        _delete(part, str(json.loads(said)[0]), message)


def channel_of(part: DiscordPart, reference: Any) -> str:
    """A channel's ID, from a [channels] name or the channel itself; "" if Discord has no such channel."""
    named = part.file.channel_ref(reference)
    found = part.facts.find_channel(named) if part.facts is not None and named else None
    return str(found.get("id")) if found else named if named.isdecimal() else ""


def _delete(part: DiscordPart, channel: str, message: str) -> None:
    config, delivery = part.file, part.poster.delivery

    async def send() -> Any:
        return await to_thread(delivery.delete, config, channel, message)

    part.outbox.put(send, priority=ANSWER, bucket=f"channel:{channel}", what="a post no longer wanted", on_fail=lambda _error: True)


def _pin(part: DiscordPart, channel: str, message: str) -> None:
    """So it stays at hand as lines go by. Without the permission it's still there, further up."""
    token = part.file.token

    async def send() -> Any:
        return await to_thread(rest.pin_message, token, channel, message)

    def refused(error: rest.DiscordError) -> bool:
        log.info("Couldn't pin a post (the bot needs Pin Messages there): %s", error)
        return True

    def pinned(_reply: Any) -> None:
        background(tidy.notices(part, channel, (tidy.PINNED,)), "taking the pin's notice away", log)

    part.outbox.put(send, priority=ANSWER, bucket=f"pins:{channel}", what="pinning a post", on_done=pinned, on_fail=refused)


__all__ = ["channel_of", "keep", "take_down"]
