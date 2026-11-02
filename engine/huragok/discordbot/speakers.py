"""Who the bot speaks as, what it says, and how that reaches a channel.

The Superintendent speaks for the servers, Vergil for moderation, Huragok for updates and setup, each under its own name
and picture ([personas]), through a webhook the bot keeps in each channel; a webhook posts under any name. Without Manage
Webhooks there, the bot posts as itself. `voice = "plain"` or [personas] enabled = false drops the theme. Every text has a
key, and [text] rewords any of them. The Superintendent's words are canon only (lore.NOTICES): none is made up here.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, Callable

from .. import lore
from ..core.discordmoved import TEXTS as TEXTS_MOVED
from . import outbox as out
from . import rest
from .config import DiscordConfig
from .rest import Refused, Reply
from .workers import to_thread

log = logging.getLogger(__name__)

WEBHOOK_NAME = "Huragok"  # the bot's own webhook in each channel, found again by this name
SILENT = 4096  # Discord's flag: no notification
TOO_MANY_WEBHOOKS = 30007  # Discord allows 15 in a channel
NOT_THIS_KIND_OF_CHANNEL = 50024  # a thread has no webhooks of its own

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
# The feeds' words (cards.py): the same in both voices, as the lore voice is in each card's heading.
for _key, _words in {
    "moderation.kick": "{by} kicked {player}{lasting}{reason}", "moderation.ban": "{by} banned {player}{lasting}{reason}",
    "moderation.mute": "{by} muted {player}{lasting}{reason}", "moderation.unmute": "{by} unmuted {player}{reason}",
    "moderation.unban": "{by} unbanned {player}{reason}", "moderation.pardon": "{by} let {player} back{reason}",
    "moderation.warn": "{by} warned {player}{reason}", "moderation.failed": "Didn't work: {what}{why}",
    "moderation.case": "case", "moderation.when": "When", "moderation.pending": "As they next join: they aren't on now",
    "vergil.strike": "{player} got strike {number} for {rule}: {action}", "vergil.strike_by": "{by} gave {player} strike {number} for {rule}: {action}", "vergil.would": "Vergil's only watching: {player} would get strike {number} for {rule}: {action}",
    "vergil.said": "What they said", "vergil.why": "Why", "vergil.name_kick": "Kicked {player} for their name ({caught})",
    "vergil.name_banned": "{by} banned the name {name}", "vergil.name_allowed": "{by} allowed the name {name}",
    "vergil.words_banned": "{by} added {words} to {rule}", "vergil.word_allowed": "{by} allowed the word {word}",
    "vergil.rule_changed": "{by} changed {rule}: {line}", "vergil.pardon": "{by} took {strikes} off {player}{reason}",
    "vote.called": "{by} called a vote: {subject}", "vote.passed": "The vote passed: {subject}", "vote.failed": "The vote failed: {subject}",
    "vote.cancelled": "The vote was cancelled: {subject}",
    "alert.crashed": "The server stopped by itself: {why}. The Superintendent is starting it again",
    "alert.faulted": "It keeps failing: {why}", "alert.hung": "It's stopped answering",
    "alert.start_failed": "It wouldn't start: {why}", "alert.update_failed": "Huragok couldn't update to {version}: {why}",
    "alert.relay_lockout": "The console relay locked out {address}: too many wrong passwords",
    "alert.rcon_sign_in": "Huragok can't sign in to this server's console: {why}", "alert.health": "The watchdog says it's {level}: {reasons}",
    "alert.part_off": "{part} was switched off: {reason}", "ticket.now": "Now", "alert.tries_again": "It tries again {when}.", "alert.on_it": "{who} is on it", "alert.stopped_by": "Stopped by {who}", "alert.update_worked": "An update has worked since", "alert.part_back": "The Discord bot had been switched off for a while, and it's back. Nothing that happened meanwhile was posted: the log says why it went off", "alert.huragok_started": "Huragok {version} is running",
    "alert.recovered": "It's back after {minutes}", "alert.healthy_again": "The watchdog says it's healthy again", "alert.signed_in": "The console took its password",
    "server.started": "It's up", "server.adopted": "Huragok picked it up, already running", "server.stopped": "It's stopped",
    "server.restarted": "It restarted{reason}", "server.restart_planned": "A restart is planned {when}{reason}",
    "server.countdown": "{reason}: {when}", "server.now": "now", "server.program_updated": "The server program is now {version}",
    "server.workshop_updated": "Mod update installed: {items}", "server.repaired": "{text}",
    "game.started": "{what} · {players} playing", "game.ended": "{what}: over after {minutes} · {players} played",
    "join.first": "{player} joined for the first time", "join.joined": "{player} joined", "join.left": "{player} left",
    "join.refused": "{player} was turned away: {reason}", "join.new_name": "{player} was last seen as {old}",
    "join.back": "{player} is back after a long time", "content.workshop": "A new version of {item} is out, and waits for the next update",
    "moderation.record": "Their record", "moderation.said": "What they said before it", "record.none": "Nothing on record",
    "moderation.notes": "What staff have written", "watch.flagged": "{player} is on, flagged {flag}{why}", "watch.back": "{player} is back, {minutes} after being kicked ({by}{reason})",
    "watch.lookalike": "{player} has a name like {staff}'s, who is staff", "watch.quiet": "{player} is new here, with only {players} on",
    "watch.banned": "{player} joined, and is banned on {where}", "watch.cheat": "The server {action} {player}: {detail}",
    "calls.called": "{player} is calling for an admin{why}", "calls.recent": "The last of the chat",
    "seeding.wanted": "{server} has {players} on: come and fill it", "notify.players": "{server} has reached {players} players",
    "notify.game": "{mode} has started on {server}, on {map}",
    "button.mute": "Mute", "button.unmute": "Unmute", "button.kick": "Kick", "button.letback": "Let back", "button.ban": "Ban", "button.unban": "Unban",
    "button.whois": "Whois", "button.said": "Said", "button.note": "Add note", "button.thread": "Discuss", "button.acknowledge": "Acknowledge", "button.close": "Close",
    "contact.title": "Contact the staff", "contact.button": "Contact the staff", "contact.card": "Need the staff for something? This opens a private thread between you and them.",
    "contact.what": "Message", "contact.form_category": "What it's about", "contact.opened": "{member} needs the staff{category}", "contact.sent": "Your thread is here: {thread}",
    "contact.failed": "That couldn't be opened. Try again in a while, or ask a member of staff directly.", "contact.off": "That isn't switched on here.", "contact.already": "You have one open already: {thread}",
    "contact.closed": "{who} closed this.", "contact.transcript": "What was said in {ticket}", "contact.transcript_line": "[Transcript]({link})", "contact.no_transcript": "No transcript: the bot needs Message Content Intent to read one back",
    "appeals.made": "{member} appeals {what} on {player}", "appeals.about_ban": "the ban", "appeals.about_kick": "the kick", "appeals.about_mute": "the mute", "appeals.the_case": "The case",
    "appeals.case": "Case {number}: {what} by {by} {when}{why}",
    "appeals.lifted": "{who} accepted the appeal", "appeals.shortened": "{who} accepted part of it: {length} from now{why}", "appeals.denied": "{who} denied the appeal{why}", "appeals.no_dm": "They couldn't be told: they don't take DMs from here", "appeals.after_all": "{who} lifted it after all",
    "appeals.told_closed": "Your appeal in **{discord}** about {what} on {player} was closed by the staff: there's nothing more to do on it.",
    "appeals.told_lifted": "Your appeal in **{discord}** was accepted: {what} on {player} is lifted.", "appeals.told_shortened": "Your appeal in **{discord}** was partly accepted: {what} on {player} now ends {length} from now{why}", "appeals.told_denied": "Your appeal in **{discord}** about {what} on {player} was denied{why}",
    "appeals.half_done": "It's lifted, but the new time didn't go on, so nothing stands against them now. Close this, and use /ticket to act on them if they should still be kept off.",
    "reports.several": "More than one player goes by {player}. Pick the one you mean from the list if they're on, or tell a member of staff.",
    "appeals.form_title": "Your appeal", "appeals.sent": "Sent: the staff have it.", "appeals.will_hear": "You'll hear by DM once they've decided, as long as your DMs are open.", "appeals.nowhere": "That can't reach the staff just now. Try later, or ask a member of staff.", "appeals.off": "Appeals aren't switched on here.",
    "appeals.nothing": "Nothing that can be appealed stands against {who}. Your name has to be in full, as it is in game.", "appeals.already": "There's already an appeal with the staff for {player}, sent {when}. They'll answer that one first.", "appeals.already_staff": "It's {ticket}: decide it or close it there, and this clears.", "appeals.too_soon": "Your last appeal on this has been decided. You can appeal it again {when}.", "appeals.no_more": "The staff have turned this down, and aren't taking any more appeals on it.",
    "appeals.again_when": "they can appeal again {when}", "appeals.again_never": "no more appeals on this", "appeals.told_again": "You can appeal again {when}.", "appeals.told_never": "They aren't taking any more appeals on this.",
    "appeals.let_again": "{who} said they can appeal again", "appeals.told_let_again": "You can appeal again in **{discord}** about {what} on {player}.", "button.appealagain": "Let them appeal again",
    "button.lift": "Lift it", "button.shorten": "Shorten it",
    "reports.made": "{reporter} ({record}) reported {player}{on}{why}", "reports.message": "{reporter} ({record}) reported a message by {author} in {channel}{why}\n{link}",
    "reports.record": "{made}, {upheld} acted on", "reports.first": "their first report", "reports.the_message": "The message", "reports.no_text": "(no text)",
    "reports.many": "{people} different people have reported {player} within {minutes} minutes: {ticket}",
    "reports.thanks": "Thanks: the staff have it.", "reports.sent": "Sent.", "reports.already": "You've reported {player} already: the staff have it.", "reports.too_soon": "You reported someone a moment ago. You can report again {when}.",
    "reports.off": "That can't reach the staff just now. Try later, or ask a member of staff.", "reports.no_such_player": "No player called {player} has been seen here. Pick from the list, or check the spelling.",
    "reports.form_title": "Report this message", "reports.form_why": "Why",
    "reports.proof": "What {who} sent with {ticket}", "reports.proof_line": "{who} sent [{name}]({link})", "reports.proof_lost": "{who} sent a file that couldn't be kept",
    "ticket.dismissed": "{who} dismissed it", "button.dismiss": "Dismiss",
    "console.public": "This channel can be read by everyone, so the console doesn't run here: make it private, or allow it in `/huragok setup` → Console.",
    "console.one_line": "One line at a time: nothing was sent.",
    "console.which": "Which server? It's remembered after this. Or start a line with its name: `server1: players`.",
    "console.menu_title": "Console", "console.menu_hint": "Which server your lines go to",
    "console.menu": "Type here and it runs on your server: `{prefix}` for Huragok's commands, anything else for the server's console, each as your rank allows. Pick your server below, or start a line with its name: `server1: players`.",
    "console.channel_goes_to": "Everyone's lines go to **{server}** now.",
    "console.goes_to": "Your lines go to **{server}** now.",
    "console.not_staff_there": "You aren't staff on {server}.",
    "console.raw_for_moderators": "Console lines are for moderators and up here: start the line with `{prefix}` for Huragok's commands.",
    "console.not_for_you": "This is for those with a rank here.",
    "console.not_yours": "That's for whoever typed the line.",
    "console.sure": "Send {line} to **{server}**?",
    "console.sending": "Sending {line} to **{server}**.",
    "console.not_sent": "Not sent.",
    "console.nobody_answered": "Not sent: no answer in time.",
    "console.done": "Done.",
    "console.cleared": "#{channel} cleared: {count} messages from {who}, {first} to {last} UTC.",
    "console.last_cleared": "Last cleared {when}: [what was here]({link}).",
    "console.last_cleared_no_file": "Last cleared {when}.",
    "console.long": "It's long, so here's a file.",
    "console.cut_short": "(Cut short: the bot can't attach files here.)",
    "console.commands_only": "Only Huragok's commands here: start the line with `{prefix}`.",
    "console.raw_for_admins": "Console lines are for admins here: start the line with `{prefix}` for Huragok's commands.",
    "console.raw_for_owners": "Console lines are for owners here: start the line with `{prefix}` for Huragok's commands.",
    "audit.console": "{who} on {server}: {line}",
    "button.server": "Server", "audit.power": "{who} asked for {what} on {server}",
    "button.confirm": "Confirm", "button.deny": "Deny", "requests.asked": "{by} (on probation) asks, #{slot}: {what}", "console.menu_past_limit": "The first {most} servers: \"server name: line\" reaches any of them", "reports.added": "{who} adds: {why}", "reports.told_acted": "The staff acted on your report of {player}.", "reports.told_dismissed": "The staff looked at your report of {player}, and took it no further.", "reports.added_answer": "Added to your report.", "reports.too_many": "The staff have several of your reports already: give them a chance to get through those.", "reports.proof_link": "{who} added a file that's too big, or isn't a picture or a clip, to keep: [{name}]({link})", "contact.too_soon": "You can open another {when}.",
    "appeals.joined": "{who} appeals this too, with their code", "appeals.with_code": "with their code", "appeals.name_only": "with only a name", "appeals.still_stands": "{what} still stands.",
    "appeals.not_here": "What stands against them is {what}, and that can't be appealed here.", "appeals.code_needed": "That needs the code from your ban's message. Without one, Contact the staff.", "appeals.also": "Also standing",
    "ticket.request_outcome": "Request {number}: {what}", "ticket.request_told": "Your request {number}: {what}", "ticket.confirmed": "{who} confirmed it: {what}", "ticket.denied": "{who} denied it{why}",
    "ticket.opened": "{who} opened this ticket on {player}",
    "ticket.number": "ticket {number}", "ticket.history": "What's happened", "ticket.earlier": "…and {count} earlier", "ticket.discussion": "Discussion: {thread}",
    "ticket.about": "Who's this about?", "ticket.about_set": "{who} says this is about {player}", "ticket.noted": "{who} added a note", "ticket.discussing": "{who} started a discussion",
    "ticket.acknowledged": "{who} acknowledged this", "ticket.closed": "{who} closed this", "ticket.stale": "Nobody has touched this for {how_long}",
    "ticket.brought_back": "{who} brought this back", "ticket.moved": "That's back in play: [the ticket]({link}).", "ticket.joined": "{who} joined the discussion", "ticket.power": "{who} asked for {what}",
    "ticket.asked": "{who} asked for: {what}, as a request", "ticket.strike_pardoned": "{who} took {count} off{why}", "ticket.banned_everywhere": "{who} banned them on every server",
    "button.strike": "Strike", "button.warn": "Warn", "button.pardonstrike": "Pardon strike", "button.banall": "Ban everywhere",
    "audit.note.add": "{who} wrote a note on {player}", "audit.note.remove": "{who} took a note off {player}",
    "audit.flag.set": "{who} flagged {player} {flag}", "audit.flag.clear": "{who} took the {flag} flag off {player}",
    "audit.commands": "Who can use which command was changed", "audit.file": "The bot's settings were changed, in Huragok's window or by hand",
    "audit.claim": "{who} claimed the bot and is an owner now", "audit.setup": "{who}: {what}",
}.items():
    TEXTS[_key] = (_words, _words)
PERSONAS = ("superintendent", "vergil", "huragok")


class _Blank(dict):
    def __missing__(self, key: str) -> str:  # a placeholder the host misspelled stays readable, never an error
        return "{" + key + "}"


def say(config: DiscordConfig, key: str, **values: Any) -> str:
    """The text for `key`, in the file's voice, or as [text] rewords it, under the name it has now or one it had."""
    given = config.section("text")
    words = given.get(key)
    if not isinstance(words, str):
        words = next((given[old] for old, new in TEXTS_MOVED.items() if new == key and isinstance(given.get(old), str)), None)
    if not isinstance(words, str):
        lore_words, plain = TEXTS[key]
        words = plain if config.data["voice"] == "plain" else lore_words
    try:
        return words.format_map(_Blank(values))
    except (ValueError, IndexError, AttributeError, KeyError):
        return words


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
            ping_roles: tuple[str, ...] = (), ping_users: tuple[str, ...] = ()) -> dict[str, Any]:
    """A message's form. Nothing pings unless named: a player's name can be "@everyone"."""
    payload: dict[str, Any] = {"allowed_mentions": {"parse": [], "roles": list(ping_roles)}}
    if ping_users:
        payload["allowed_mentions"]["users"] = list(ping_users)
    if content:
        payload["content"] = content[:2000]
    if embeds:
        payload["embeds"] = embeds[:10]
    if silent:
        payload["flags"] = SILENT
    return payload


NOT_THE_AUTHOR = 50005  # Discord: "Cannot edit a message authored by another user"


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
        return rest.post_message(config.token, channel, _once(plain))

    def edit(self, config: DiscordConfig, channel: str, message_id: str, payload: dict[str, Any]) -> Reply:
        """A message this bot posted in this channel. Only whoever posted it can edit it, and that may have been its webhook or itself, whatever voice it speaks in now. Neither (the webhook that posted it
        went with the bot being removed): it's gone, as far as anyone asking goes, so it's posted again."""
        address = self._webhook_of(channel, config.token)

        def as_webhook() -> Reply:
            return rest.edit_webhook_message(address, message_id, payload)

        def as_itself() -> Reply:
            return rest.edit_message(config.token, channel, message_id, payload)

        ways = [as_itself] if not address else [as_webhook, as_itself] if speaker(config, "superintendent") else [as_itself, as_webhook]
        last: Refused | None = None
        for way in ways:
            try:
                return way()
            except Refused as error:
                if error.status != 404 and error.code != NOT_THE_AUTHOR:
                    raise
                last = error
        raise Refused(str(last), status=404, code=last.code if last else 0) from None

    def delete(self, config: DiscordConfig, channel: str, message_id: str) -> Reply:
        """A message this bot posted in this channel, through its webhook there, or as itself."""
        address = self._webhook_of(channel, config.token)  # whatever the voice is now: it may have been posted under another
        if address:
            try:
                return rest.delete_webhook_message(address, message_id)
            except Refused as error:
                if error.status not in (403, 404):
                    raise
        return rest.delete_message(config.token, channel, message_id)

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
            if error.status != 403 and error.code not in (TOO_MANY_WEBHOOKS, NOT_THIS_KIND_OF_CHANNEL):
                raise
            log.info("The bot can't keep a webhook in channel %s (%s): it posts as itself there", channel, error)
            self.no_webhook.add(channel)
            return ""
        address = _address(ours)
        if address:
            self.webhooks[channel] = address
        return address


def _once(payload: dict[str, Any]) -> dict[str, Any]:
    """With Discord asked to take it once: a post it answers too late for is tried again, and this stops a second copy. It's kept on what's being sent, so each try says the same."""
    if "nonce" not in payload:
        payload["nonce"] = secrets.token_hex(12)
        payload["enforce_nonce"] = True
    return payload


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
             priority: int = out.STATUS, key: str | None = None, what: str = "", tag: str = "",
             on_done: Callable[[Any], None] | None = None, lasts: float = 0.0) -> None:
        if webhook:
            bucket = f"webhook:{webhook[-12:]}"

            async def send() -> Reply:
                shown = speaker(self.config(), persona)
                return await to_thread(rest.execute_webhook, webhook, {**payload, **shown})
        else:
            bucket = f"channel:{channel}"

            async def send() -> Reply:
                return await to_thread(self.delivery.to_channel, self.config(), channel, persona, payload)

        self.outbox.put(send, priority=priority, bucket=bucket, key=key, what=what or f"a post to a {bucket.split(':')[0]}",
                        tag=tag, on_done=on_done, expires=self.outbox.clock() + lasts if lasts else None)
