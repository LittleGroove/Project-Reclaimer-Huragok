"""Who the bot speaks as, what it says, and how that reaches a channel.

The Superintendent speaks for the servers, Vergil for moderation, Huragok for updates and setup, each under its own name
and picture ([personas]), through a webhook the bot keeps in each channel; a webhook posts under any name. Without Manage
Webhooks there, the bot posts as itself. `voice = "plain"` or [personas] enabled = false drops the theme. Every text has a
key, and [text] rewords any of them. The Superintendent's words are canon only (lore.NOTICES): none is made up here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .. import lore
from . import outbox as out
from . import rest
from .config import DiscordConfig
from .rest import Refused, Reply

log = logging.getLogger(__name__)

WEBHOOK_NAME = "Huragok"  # the bot's own webhook in each channel, found again by this name
SILENT = 4096  # Discord's flag: no notification
TOO_MANY_WEBHOOKS = 30007  # Discord allows 15 in a channel

# key -> (the lore voice's words, plain words)
TEXTS: dict[str, tuple[str, str]] = {
    "test.superintendent": (f"**{lore.NOTICES['welcome']}**\nA test from Huragok: the servers' news looks like this.",
                            "A test from Huragok: the servers' news looks like this."),
    "test.vergil": (f"**{lore.NOTICES['hero']}**\nA test from Huragok: moderation looks like this.",
                    "A test from Huragok: moderation looks like this."),
    "test.huragok": ("A test from Huragok: updates and setup look like this.",
                     "A test from Huragok: updates and setup look like this."),
    "launcher.stopped": (f"**{lore.NOTICES['emergency']}**\nHuragok stopped unexpectedly ({{why}}); starting it again in "
                         "{wait} s. The bot is offline until it's back. The servers keep running.",
                         "Huragok stopped unexpectedly ({why}); starting it again in {wait} s. The bot is offline "
                         "until it's back. The servers keep running."),
}
PERSONAS = ("superintendent", "vergil", "huragok")


class _Blank(dict):
    def __missing__(self, key: str) -> str:  # a placeholder the host misspelled stays readable, never an error
        return "{" + key + "}"


def say(config: DiscordConfig, key: str, **values: Any) -> str:
    """The text for `key`, in the file's voice, or as [text] rewords it."""
    words = config.section("text").get(key)
    if not isinstance(words, str):
        lore_words, plain = TEXTS[key]
        words = plain if config.data["voice"] == "plain" else lore_words
    try:
        return words.format_map(_Blank(values))
    except (ValueError, IndexError, AttributeError, KeyError):
        return words


def unknown_texts(config: DiscordConfig) -> list[str]:
    """[text] keys that reword nothing."""
    return [f"discord.toml: [text] {key} isn't a text of this version; it does nothing" for key in config.section("text")
            if key not in TEXTS]


def speaker(config: DiscordConfig, persona: str) -> dict[str, str]:
    """What goes in a webhook's post so it shows this persona; {} for no theme."""
    personas = config.section("personas")
    if config.data["voice"] == "plain" or not personas.get("enabled", True):
        return {}
    entry = personas.get(persona)
    if not isinstance(entry, dict):
        return {}
    shown = {"username": str(entry.get("name") or persona.title())}
    if str(entry.get("picture") or "").strip():
        shown["avatar_url"] = str(entry["picture"]).strip()
    return shown


def message(content: str = "", *, embeds: list[dict[str, Any]] | None = None, silent: bool = True,
            ping_roles: tuple[str, ...] = ()) -> dict[str, Any]:
    """A message's form. Nothing pings unless named: a player's name can be "@everyone"."""
    payload: dict[str, Any] = {"allowed_mentions": {"parse": [], "roles": list(ping_roles)}}
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]
    if silent:
        payload["flags"] = SILENT
    return payload


class Delivery:
    """How a message reaches a channel: the bot's own webhook there (made the first time), or the bot itself. It blocks:
    the outbox runs it in a thread, one at a time; the window runs its own."""

    def __init__(self) -> None:
        self.webhooks: dict[str, str] = {}  # channel -> its webhook's address, as found this run
        self.no_webhook: set[str] = set()  # channels the bot can't make one in

    def forget(self) -> None:
        """Another token, or another Discord: what was found isn't the new bot's."""
        self.webhooks.clear()
        self.no_webhook.clear()

    def to_channel(self, config: DiscordConfig, channel: str, persona: str, payload: dict[str, Any]) -> Reply:
        shown = speaker(config, persona)
        if shown and (address := self._webhook_of(channel, config.token)):
            try:
                return rest.execute_webhook(address, {**payload, **shown})
            except Refused as error:
                if error.status != 404:
                    raise
                self.webhooks.pop(channel, None)  # someone deleted it: the bot speaks for itself this once
        plain = dict(payload)
        if shown and plain.get("content"):
            plain["content"] = f"**{shown['username']}**: {plain['content']}"[:2000]
        return rest.post_message(config.token, channel, plain)

    def _webhook_of(self, channel: str, token: str) -> str:
        """The bot's webhook in this channel; "" when it isn't allowed one."""
        if channel in self.webhooks:
            return self.webhooks[channel]
        if channel in self.no_webhook:
            return ""
        try:
            hooks = rest.channel_webhooks(token, channel)
            ours = next((hook for hook in hooks if hook.get("name") == WEBHOOK_NAME and _address(hook)), None)
            if ours is None:
                ours = rest.create_webhook(token, channel, WEBHOOK_NAME)
        except Refused as error:
            if error.status != 403 and error.code != TOO_MANY_WEBHOOKS:
                raise
            log.info("The bot can't keep a webhook in channel %s (%s): it posts as itself there", channel, error)
            self.no_webhook.add(channel)
            return ""
        address = _address(ours)
        if address:
            self.webhooks[channel] = address
        return address


def _address(hook: dict[str, Any]) -> str:
    """A webhook's address: Discord's documents only promise its ID and token (the token only for the bot's own)."""
    if hook.get("url"):
        return str(hook["url"])
    return f"{rest.API}/webhooks/{hook.get('id')}/{hook['token']}" if hook.get("token") else ""


class Poster:
    """Everything posted, through the outbox: to a webhook's address (webhook mode, or a partner's Discord), or a
    channel."""

    def __init__(self, config: Callable[[], DiscordConfig], outbox: out.Outbox) -> None:
        self.config = config
        self.outbox = outbox
        self.delivery = Delivery()

    def post(self, persona: str, payload: dict[str, Any], *, channel: str = "", webhook: str = "",
             priority: int = out.STATUS, key: str | None = None, what: str = "",
             on_done: Callable[[Any], None] | None = None) -> None:
        if webhook:
            bucket = f"webhook:{webhook[-12:]}"

            async def send() -> Reply:
                shown = speaker(self.config(), persona)
                return await asyncio.to_thread(rest.execute_webhook, webhook, {**payload, **shown})
        else:
            bucket = f"channel:{channel}"

            async def send() -> Reply:
                return await asyncio.to_thread(self.delivery.to_channel, self.config(), channel, persona, payload)

        self.outbox.put(send, priority=priority, bucket=bucket, key=key, what=what or f"a post to a {bucket.split(':')[0]}",
                        on_done=on_done)
