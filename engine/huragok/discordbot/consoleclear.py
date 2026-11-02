"""The console channel put back as it started. Nothing said for `clear_after_minutes`: what was there goes to the logs as a file, and it's all removed but the menu (and anything pinned), ready for next time.

Nothing can be moved in Discord, and only a transcript keeps who said what and when. So the file comes first, and if it can't be posted nothing's touched: nothing's ever lost, and the worst a failure in the middle can do
is post a transcript twice. The deleting is tidy.py's, so a restart picks it up where it was. Without Manage Messages nothing happens at all: clearing only the bot's replies would leave questions with no answers.
It's checked for once a minute by one request a channel, and only reads messages when one of them has been quiet long enough. Imported with the bot alone.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
from typing import TYPE_CHECKING, Any

from ..rcon import masked
from . import cards, features, rest, tidy
from .config import DiscordConfig
from .facts import Facts
from .ranks import as_they_may_see
from .speakers import say
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

KEPT = "console:kept:"  # + the channel: the newest message that's been dealt with. What's above it hasn't
CLEARED = "console:cleared"  # the picker channel's last clear: its message in the logs, and when
SMALLEST_MINUTES = 5.0  # longer than anything that's waiting for an answer in there (console.SURE_SECONDS)
PAGES = 5  # of 100, at once: a long backlog carries on next minute, with no gaps
RETRY_SECONDS = 600.0  # after the transcript couldn't be posted, or Discord wouldn't say
SAID = (0, 19, 20, 23)  # what people and the bot said: not Discord's own notices
STAYS = (1, 2, 3, 4, 5, 21)  # Discord's notices that can't be deleted
LARGEST_FILE = 7_000_000  # bytes: the smallest limit Discord has is 8 MiB, and it's a file of words


def after_minutes(config: DiscordConfig) -> float:
    """How long it stays quiet before it's cleared. 0: never."""
    minutes = float(config.section("console").get("clear_after_minutes", 0) or 0)
    return max(minutes, SMALLEST_MINUTES) if minutes > 0 else 0.0


def channels(config: DiscordConfig, facts: Facts) -> list[dict[str, Any]]:
    """Each console channel Discord has: the picker's, and on per-server each server's."""
    options = config.section("console")
    references = [options.get("channel")]
    if options.get("layout") == "per-server":
        references += list(dict(options.get("channels") or {}).values())
    found: dict[str, dict[str, Any]] = {}
    for reference in references:
        channel = facts.find_channel(config.channel_ref(reference))
        if channel:
            found[str(channel.get("id"))] = channel
    return list(found.values())


def why_not(config: DiscordConfig, facts: Facts, channel: dict[str, Any]) -> str:
    """Why this channel can't be cleared, in words for the host. "" if it can. What's checked first is what could lose something."""
    named = config.section("channels")
    sharing = [(name, name) for name in named if name != "console"] + [(config.section("contact").get("channel"), "Contact"), (config.section("status", "board").get("channel"), "the status board")]
    sharing += [(line.get("closes_to"), f"{key}'s closed cards") for key, line in config.section("messages").items() if isinstance(line, dict) and str(line.get("closes_to") or "").strip() not in ("", "move", "stay", "thread")]
    sharing += [(line.get("channel"), key) for key, line in config.section("messages").items() if isinstance(line, dict) and line.get("post", True) and line.get("channel") not in set(named) - {"console"}]  # a line sent somewhere of its own
    other_jobs = sorted({job for reference, job in sharing if reference and (found := facts.find_channel(config.channel_ref(reference))) and str(found.get("id")) == str(channel.get("id"))})
    if other_jobs:
        return f"it's also the channel for {', '.join(other_jobs)}, and clearing it would take those with it: give the console its own"
    missing = [label for label, permission in (("View Channel", "view"), ("Read Message History", "history"), ("Manage Messages", "manage_messages")) if not facts.has(permission, channel)]
    if missing:
        return f"the bot needs {' and '.join(missing)} in #{channel.get('name')} (tick them on its role, or on that channel)"
    if config.section("console").get("transcript", True):
        line = config.section("messages").get("console-cleared") or {}
        target = facts.find_channel(config.channel_ref(line.get("channel")))
        if not line.get("post", True) or not target:
            return "there's nowhere to keep what was said: pick a channel for console-cleared, or set [console] transcript = false"
        if facts.everyone_reads(target) and not config.section("feeds").get("public"):
            return f"what was said would be kept in #{target.get('name')}, which anyone can read: make it private, or pick another for console-cleared"
        if str(target.get("id")) == str(channel.get("id")):
            return "console-cleared points at the console itself: pick another channel for it"
        if not facts.has("attach", target):
            return f"the bot needs Attach Files in #{target.get('name')} to keep what was said there"
    return ""


def problems(config: DiscordConfig, facts: Facts) -> list[str]:
    """What the setup check says about it: only what stops it, as it's quiet when it's working."""
    if not after_minutes(config):
        return []
    return [f"Clearing the console won't happen: {why}." for channel in channels(config, facts) if (why := why_not(config, facts, channel))]


def touched(part: DiscordPart, channel: Any) -> None:
    """Someone pressed something in it: that's not a message, and it counts as well."""
    part.console_touched[str(channel)] = part.status.clock()


async def sweep(part: DiscordPart) -> None:
    """Once a minute, each console channel that's on its own clock. One at a time, as there's no hurry."""
    config, facts = part.file, part.facts
    minutes = after_minutes(config)
    if not minutes or facts is None or part.state != "online" or features.state_of(features.BY_KEY["console"], config, facts)[0] != features.ON or part.client is None or not part.client.intents.message_content:
        return  # on and working: a console waiting on a channel anyone can read ignores what's said there, and so does this
    for channel in channels(config, facts):
        channel_id = str(channel.get("id"))
        if part.status.clock() < part.console_retry.get(channel_id, 0.0):
            continue
        if why := why_not(config, facts, channel):
            if channel_id not in part.console_said:
                part.console_said.add(channel_id)
                log.warning("The console isn't being cleared: %s", why)
            continue
        part.console_said.discard(channel_id)
        try:
            await _clear(part, channel_id, str(channel.get("name") or ""), minutes * 60)
        except rest.DiscordError as error:
            part.console_retry[channel_id] = part.status.clock() + RETRY_SECONDS
            log.info("Couldn't look at the console channel to clear it: %s", error)


async def _clear(part: DiscordPart, channel: str, name: str, quiet: float) -> None:
    config = part.file
    now = part.status.clock()
    if part.console_running.get(channel) or now - part.console_touched.get(channel, 0.0) < quiet:
        return
    last = str((await to_thread(rest.call, "GET", f"/channels/{channel}", config.token)).get("last_message_id") or "")
    mark = part.status.stored(KEPT + channel)[1]
    if not mark:  # the first look at this channel: what's in it was there before it was the console's, which may be anything. Only what's said from now on is ever touched
        part.status.store(KEPT + channel, value=last or "0")
        return
    if not last or int(last) <= int(mark) or now - tidy.time_of(last) < quiet:
        return
    messages = [message for message in await to_thread(_read, config.token, channel, mark) if int(message["id"]) <= int(last)]  # what's said while this reads wasn't quiet: it's for next time
    if not messages:
        part.status.store(KEPT + channel, value=last)
        return
    menu = part.status.stored("console:menu")[0]
    going = [message for message in messages if str(message["id"]) != menu and not message.get("pinned") and message.get("type", 0) not in STAYS]
    threads = [str(message["thread"]["id"]) for message in going if message.get("thread")]
    said_in: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        said_in[thread] = await to_thread(_read, config.token, thread, "")
        if said_in[thread] and now - tidy.time_of(str(said_in[thread][-1]["id"])) < quiet:
            return  # still being talked in: it isn't quiet
    if _read_blank(going):
        part.console_retry[channel] = now + RETRY_SECONDS
        log.warning("The console isn't being cleared: Discord is giving the bot blank messages, so there'd be no transcript. Turn Message Content Intent back on, on the developer portal's Bot page")
        return
    worth_keeping = [message for message in going if message.get("type", 0) in SAID]
    link = ""
    if worth_keeping and config.section("console").get("transcript", True):
        link = await _keep(part, name, worth_keeping, said_in)
        if not link:
            part.console_retry[channel] = now + RETRY_SECONDS
            return
    newest = str(messages[-1]["id"])
    tidy.later_many(part, channel, [str(message["id"]) for message in going], "the console's been cleared")  # first, in one go: a crash before the mark means a transcript twice, and never a message left for good
    part.status.store(KEPT + channel, value=newest)
    tidy.sweep(part)
    for thread in threads:
        await _end_thread(part, channel, thread, keep=len(said_in[thread]) >= PAGES * 100)
    if worth_keeping:
        _new_start(part, channel, link)


def _read(token: str, channel: str, after: str) -> list[dict[str, Any]]:
    """What's been said since `after`, oldest first. It blocks."""
    found: list[dict[str, Any]] = []
    for _ in range(PAGES):
        page = rest.call("GET", f"/channels/{channel}/messages?limit=100&after={after or 0}", token)
        if not page:
            break
        page = sorted(page, key=lambda message: int(message["id"]))
        found += page
        after = str(page[-1]["id"])
        if len(page) < 100:
            break
    return found


def _read_blank(messages: list[dict[str, Any]]) -> bool:
    """Everything people said reads as nothing: the portal's switch was turned off since the bot last looked."""
    people = [message for message in messages if message.get("type", 0) in SAID and not (message.get("author") or {}).get("bot") and not message.get("webhook_id")]
    return bool(people) and not any(message.get("content") or message.get("attachments") or message.get("embeds") or message.get("sticker_items") for message in people)


def _when(snowflake: str) -> str:
    return time.strftime("%H:%M:%S", time.gmtime(tidy.time_of(snowflake)))


def _author(message: dict[str, Any]) -> str:
    author = message.get("author") or {}
    return str(author.get("global_name") or author.get("username") or "?")


def _words(config: DiscordConfig, message: dict[str, Any]) -> list[str]:
    """What a message said, as it may be read in the logs: no password, no address, and only what a moderator may see, whoever asked."""
    lines: list[str] = []
    content = str(message.get("content") or "")
    if content:
        server, colon, rest_of_line = content.partition(":")
        whole = masked(content)  # "password ab:cd" has a colon in it too
        safe = whole if whole != content else f"{server}: {masked(rest_of_line.strip())}" if colon else content  # as "server1: password" names a server first. Only ever hides too much
        lines += cards.no_addresses(as_they_may_see(config, "moderator", safe)).split("\n")
    for embed in message.get("embeds") or []:
        lines += [cards.no_addresses(as_they_may_see(config, "moderator", str(value))) for value in (embed.get("title"), embed.get("description")) if value]
    for file in message.get("attachments") or []:
        lines.append(f"(a file: {file.get('filename')}, {file.get('size', 0)} bytes. Its link stops working after a while)")
    return lines


def transcript(config: DiscordConfig, name: str, messages: list[dict[str, Any]], said_in: dict[str, list[dict[str, Any]]]) -> str:
    """Who said what and when, each line under the one it answers."""
    who = {str((message.get("author") or {}).get("id")): _author(message) for message in [*messages, *[inner for thread in said_in.values() for inner in thread]] if message.get("author")}
    day = time.strftime("%Y-%m-%d", time.gmtime(tidy.time_of(str(messages[0]["id"]))))
    out = [f"#{name}, from {day} {_when(str(messages[0]['id']))} to {_when(str(messages[-1]['id']))} UTC. {len(messages)} messages.",
           "Who: " + "; ".join(f"{label} = {person}" for person, label in who.items()), ""]

    def add(message: dict[str, Any], indent: str = "") -> None:
        reply_to = (message.get("referenced_message") or {}).get("author") or {}
        lead = f"{indent}[{_when(str(message['id']))}] {_author(message)}" + (f", replying to {reply_to.get('global_name') or reply_to.get('username')}" if reply_to else "") + ":"
        words = _words(config, message)
        out.append(f"{lead} {words[0]}" if words else lead)
        out.extend(f"{indent}    {line}" for line in words[1:])

    for message in messages:
        if message.get("type", 0) not in SAID:
            continue
        add(message)
        thread = str((message.get("thread") or {}).get("id") or "")
        if said_in.get(thread):
            out.append("    in its thread:")
            for inner in said_in[thread]:
                if inner.get("type", 0) in SAID:
                    add(inner, "    ")
    return "\n".join(out) + "\n"


async def _keep(part: DiscordPart, name: str, messages: list[dict[str, Any]], said_in: dict[str, list[dict[str, Any]]]) -> str:
    """The transcript in the logs. Its message's link, or "" if it couldn't be posted, and then nothing's touched."""
    import discord

    config = part.file
    target = part.feeds.channel_id(config.section("messages").get("console-cleared") or {})
    text = transcript(config, name, messages, said_in)
    people = sorted({_author(message) for message in messages if not (message.get("author") or {}).get("bot") and not message.get("webhook_id")})
    day = time.strftime("%Y-%m-%d-%H%M", time.gmtime(tidy.time_of(str(messages[-1]["id"]))))
    pieces = [text[start:start + LARGEST_FILE] for start in range(0, len(text), LARGEST_FILE)]
    try:
        channel = part.client.get_partial_messageable(int(target), guild_id=int(config.guild))
        first = None
        for number, piece in enumerate(pieces):
            line = say(config, "console.cleared", channel=cards.clean(name), count=len(messages), who=cards.clean(", ".join(people)) or "the bot", first=_when(str(messages[0]["id"])), last=_when(str(messages[-1]["id"])))
            sent = await channel.send(line if number == 0 else "", file=discord.File(io.BytesIO(piece.encode("utf-8")), f"console-{day}{f'-{number + 1}' if number else ''}.txt"), allowed_mentions=discord.AllowedMentions.none(), silent=True)
            first = first or sent
    except (discord.DiscordException, ValueError, OSError) as error:
        log.warning("The console wasn't cleared, as what was said in it couldn't be kept: %s", error)
        return ""
    return str(first.jump_url) if first is not None else ""


async def _end_thread(part: DiscordPart, channel: str, thread: str, *, keep: bool = False) -> None:
    """An answer's thread goes with its line. Without the permission to delete it, it's hidden: the bot started it. `keep`: there was more in it than got read, so it's hidden, and what's in it isn't lost."""
    config, facts = part.file, part.facts
    known = facts.find_channel(channel) if facts is not None else None
    try:
        if known and facts.has("manage_threads", known) and not keep:
            await to_thread(rest.call, "DELETE", f"/channels/{thread}", config.token)
        else:
            await to_thread(rest.close_thread, config.token, thread, True)
    except rest.DiscordError as error:
        log.info("Couldn't take a console thread away: %s", error)


def _new_start(part: DiscordPart, channel: str, link: str) -> None:
    """Ready to be used again: the menu says where it all went, and a server picked for what was just cleared has to be picked again, so a line after a long quiet doesn't land on one nobody remembers choosing."""
    from .console import MEMBER, keep_menu

    options = part.file.section("console")
    picker = part.facts.find_channel(part.file.channel_ref(options.get("channel"))) if part.facts is not None else None
    if not picker or str(picker.get("id")) != channel:
        return
    part.status.store(CLEARED, message=link, value=str(part.status.clock()))
    if options.get("forget_server", True):
        part.ctx.db.execute("DELETE FROM discord_posts WHERE key LIKE ?", (MEMBER + "%",))
    keep_menu(part)


__all__ = ["CLEARED", "KEPT", "after_minutes", "channels", "problems", "sweep", "touched", "transcript", "why_not"]
