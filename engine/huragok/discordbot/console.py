"""The typed console: a line in a console channel runs on a server. One starting with `!` is a Huragok command, as in game, with the same rank; anything else is a console line, with everything the relay checks (players/relay.py).
Each channel is a server's own (`per-server`), or the one `channel` is for any of them (`picker`): a menu there says which, kept for each member, or a line can start with the server's name. Imported with client.py, in bot mode alone.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

from ..core.storage import acting
from ..players.relay import READ_ONLY, without_addresses
from ..players.router import LOOKING
from ..rcon import masked
from ..servers.state import Player
from . import cards, consoleclear, features, kept, tidy
from .client import SaysWhatWentWrong, acting_as, answer, someone_from
from .config import colour_value
from .ranks import Someone, as_they_may_see, can_act, game_rank, rank_of, rank_to_run, times_a_kick
from .speakers import say

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

PICK = "huragok:console:pick"  # the menu's: stateless, so one from before a restart still works
MENU = "console:menu"  # [discord_posts]: the menu's message, and what it says
MEMBER = "console:member:"  # + their Discord ID: the server their lines go to
CHANNEL = "console:channel"  # remember_server = "channel": the one everyone's go to
NOBODY = "player"
MOST_SHOWN = 1900  # in one message, with room for the server's name and a code block
MOST_SPLIT = 5  # messages of a long answer, with long_answers = "split": past that it's a file
SURE_SECONDS = 120.0  # how long "are you sure?" and "which server?" wait
SERVERS_ON_A_MENU = 25  # Discord's limit
PUBLIC_EVERY_SECONDS = 600.0
PUBLIC_LASTS_SECONDS = 60.0


def _squeezed(name: str) -> str:
    return "".join(letter for letter in name.casefold() if letter.isalnum())


def _is_channel(reference: str, channel: Any) -> bool:
    text = str(reference or "").strip()
    return bool(text) and (text == str(channel.id) or text.removeprefix("#").casefold() == str(getattr(channel, "name", "")).casefold())  # one the bot hasn't been told about has no name


def where_lines_go(part: DiscordPart, channel: Any) -> str | None:
    """None: this isn't a console channel. A server's name: it's that server's. "": the picker's, for any of them."""
    config = part.file
    options = config.section("console")
    if options.get("layout") == "per-server":
        for server, reference in dict(options.get("channels") or {}).items():
            if server in part.ctx.servers and _is_channel(config.channel_ref(reference), channel):
                return str(server)
    return "" if _is_channel(config.channel_ref(options.get("channel")), channel) else None  # per-server too: the way to a server without a channel


def named_on_the_line(part: DiscordPart, line: str) -> tuple[str, str]:
    """(the server it names, the line without that): "server1: kick 3". A colon that doesn't follow a server's name is part of the line."""
    word, colon, rest_of_line = line.partition(":")
    if colon:
        wanted = _squeezed(word)
        for server in part.ctx.servers:
            if _squeezed(server) == wanted:
                return str(server), rest_of_line.strip()
    return "", line


def _remember_key(part: DiscordPart, member: str) -> str:
    return CHANNEL if part.file.section("console").get("remember_server") == "channel" else MEMBER + member


def remembered(part: DiscordPart, member: str) -> str:
    server = part.status.stored(_remember_key(part, member))[1]
    return server if server in part.ctx.servers else ""


def _mine(part: DiscordPart, someone: Someone) -> bool:
    return any(rank_of(part.file, someone, str(server)) != NOBODY for server in part.ctx.servers) or rank_of(part.file, someone) != NOBODY


async def heard(part: DiscordPart, message: discord.Message) -> None:
    """A message in a channel the bot can see, by anyone. Only lines in a console channel, by someone with a rank, are ever acted on."""
    config = part.file
    options = config.section("console")
    if not options.get("typed") or message.author.bot or message.webhook_id is not None or message.guild is None or str(message.guild.id) != config.guild:
        return
    here = where_lines_go(part, message.channel)
    line = message.content.strip()
    if here is None or not line:
        return
    someone = someone_from(message.author)
    if not options.get("public") and part.feeds.everyone_reads(str(message.channel.id)):
        channel = str(message.channel.id)
        if _mine(part, someone) and part.status.clock() - part.console_public_said.get(channel, 0.0) >= PUBLIC_EVERY_SECONDS:  # the check says so too: this is for whoever's typing, and it isn't for repeating
            part.console_public_said[channel] = part.status.clock()
            sent = await message.reply(say(config, "console.public"), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            tidy.later(part, channel, str(sent.id), PUBLIC_LASTS_SECONDS, "the console saying it's public")
        return
    if not _mine(part, someone):
        if options.get("ignore_others") == "delete":
            try:
                await message.delete()
            except discord.HTTPException as error:
                log.info("Couldn't delete a line in the console channel: %s", error)
        return
    if str(options.get("ignore_prefix") or "") and line.startswith(str(options["ignore_prefix"])):  # staff talking to each other
        return
    if "\n" in line:
        await _reply(message, say(config, "console.one_line"))
        return
    server, line = named_on_the_line(part, line) if options.get("server_prefix", True) else ("", line)
    if server and not line:  # "server2:" alone: that's where their lines go from now
        part.status.store(_remember_key(part, str(message.author.id)), value=server)
        await _reply(message, say(config, "console.goes_to", server=cards.clean(server)))
        return
    server = server or here or remembered(part, str(message.author.id))
    servers = list(part.ctx.servers)
    if not server and len(servers) == 1:
        server = str(servers[0])
    if not server:
        if not part.ctx.servers:
            await _reply(message, "There are no servers to run that on.")
            return
        view = _Which(part, message, line)
        view.asked = await message.reply(say(config, "console.which"), view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        return
    await _ask_or_run(part, message, server, line, someone)


def _rank_for_line(part: DiscordPart, someone: Someone, server: str, line: str) -> str:
    """A ! command that reaches every server takes the rank they have on all of them. A console line is the server's own, so it's their rank there."""
    prefix = part.ctx.command_prefix
    if line.startswith(prefix):
        command, _, rest = line.removeprefix(prefix).partition(" ")
        return rank_to_run(part.file, someone, part.ctx.servers, server, command.lower(), timed=times_a_kick(rest.split()))
    return game_rank(part.file, someone, server)


async def _ask_or_run(part: DiscordPart, message: discord.Message, server: str, line: str, someone: Someone) -> None:
    """Whether they may is settled before anyone's asked if they're sure: being asked, saying Yes, and then being refused is worse than either."""
    prefix = part.ctx.command_prefix
    word = line.removeprefix(prefix).partition(" ")[0].lower()
    rank = _rank_for_line(part, someone, server, line)
    if rank == NOBODY:
        try:
            await _reply(message, say(part.file, "console.not_staff_there", server=cards.clean(server)))
        finally:
            await _gone_if_secret(message, line)
        return
    if line.startswith(prefix):
        why = cards.clean(part.ctx.refuses(word, None, server=server, player=Player(0, acting_as(part, message.author), f"discord:{message.author.id}"), role=rank))  # it names what they typed
    else:
        why = _no_raw_lines(part, rank)
    if why:
        try:
            await _reply(message, why)
        finally:
            await _gone_if_secret(message, line)
        return
    if word in {str(name).lower() for name in part.file.section("console").get("confirm") or []}:
        view = _Sure(part, message, server, line)
        try:
            view.asked = await message.reply(say(part.file, "console.sure", line=_code(masked(line)), server=cards.clean(server)), view=view, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        finally:
            await _gone_if_secret(message, line)  # not after they've said Yes: it shouldn't sit there while they decide
    else:
        await _run(part, message, server, line, someone)


async def _run(part: DiscordPart, message: discord.Message, server: str, line: str, someone: Someone) -> None:
    """A line, and while it runs its channel isn't cleared: the answer needs its line to reply to."""
    channel = str(message.channel.id)
    part.console_running[channel] = part.console_running.get(channel, 0) + 1
    try:
        with acting(f"discord:{message.author.id}"):
            await _running(part, message, server, line, someone)
    finally:
        part.console_running[channel] -= 1
        consoleclear.touched(part, channel)


async def _running(part: DiscordPart, message: discord.Message, server: str, line: str, someone: Someone) -> None:
    config = part.file
    rank = _rank_for_line(part, someone, server, line)
    who = acting_as(part, message.author)
    prefix = part.ctx.command_prefix
    raw = not line.startswith(prefix)
    if raw:
        if why := _no_raw_lines(part, rank):
            await _reply(message, why)
            return
        worked, text = await part.ctx.console_line(server, who, rank, line)
    else:
        heard_back: list[str] = []

        async def tell(_server: str, _player_id: str, said: str) -> None:
            heard_back.append(said)

        name, _, words = line[len(prefix):].partition(" ")
        player = Player(0, who, f"discord:{message.author.id}")
        refused = await part.ctx.run_line(name.lower(), words.strip(), server=server, player=player, role=rank, tell=tell)
        worked, text = refused is None, refused or "\n".join(heard_back)
    if worked:
        changes_only = config.section("console").get("audit", "changes") == "changes"
        if not changes_only or (name_of_line(part, line) not in LOOKING and name_of_line(part, line) not in READ_ONLY):
            part.audit("console", say(config, "audit.console", who=who, server=server, line=masked(line)), merge=True)
        if raw and line.partition(" ")[0].lower() == "pardon":
            _pardon_on_record(part, server, line.partition(" ")[2].strip(), who)
    fit_to_read = without_addresses(as_they_may_see(config, "moderator", (text if raw else part.named_in(text)) or say(config, "console.done")), None)[0]  # everyone with a rank reads this channel, so everyone's answer is what the lowest may see: an address is for the window or the relay
    try:
        await _answer(part, message, server, line, fit_to_read, raw=raw and worked)
    finally:
        await _gone_if_secret(message, line)


async def _gone_if_secret(message: discord.Message, line: str) -> None:
    """A password, sitting in a channel: it goes, whatever else came of the line. Without the permission to, the host hears why."""
    if masked(line) == line:
        return
    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.HTTPException as error:
        log.warning("A password typed in the console is still there, as the bot couldn't delete it: it needs Manage Messages there (%s)", error)


def name_of_line(part: DiscordPart, line: str) -> str:
    return line.removeprefix(part.ctx.command_prefix).partition(" ")[0].lower()


def _pardon_on_record(part: DiscordPart, server: str, ref: str, by: str) -> None:
    """The server says nothing when someone's pardoned, as it does for the rest, so what's typed here wouldn't reach the ledger, and their card would go on saying they're kicked."""
    state = part.ctx.servers.get(server)
    wanted = ref.strip("\"'").casefold()
    found = [(str(record.id), str(record.last_name)) for record in part.ctx.players.find(wanted)] if wanted else []
    if len(found) == 1:
        part.ctx.moderation.record_seen("pardon", server, found[0][0], found[0][1], by=by, source="console")
    elif state is not None:
        log.info("A pardon typed for %s isn't on anyone's record: it doesn't name one player", ref)


def _no_raw_lines(part: DiscordPart, rank: str) -> str:
    """Why this rank can't type a console line here. The relay has its own word on each line after this."""
    wanted = str(part.file.section("console").get("raw_rank") or "admin")
    if wanted == "off":
        return say(part.file, "console.commands_only", prefix=part.ctx.command_prefix)
    if rank in {"moderator": ("moderator", "admin", "owner"), "admin": ("admin", "owner"), "owner": ("owner",)}[wanted]:
        return ""
    return say(part.file, {"moderator": "console.raw_for_moderators", "admin": "console.raw_for_admins", "owner": "console.raw_for_owners"}[wanted], prefix=part.ctx.command_prefix)


def _code(text: str) -> str:
    return "`" + str(text).replace("`", "'")[:300] + "`"


async def _reply(message: discord.Message, text: str, **more: Any) -> None:
    await message.channel.send(text, reference=message.to_reference(fail_if_not_exists=False), mention_author=False, allowed_mentions=discord.AllowedMentions.none(), **more)  # it may have gone already, if it held a password


def _pieces(text: str, size: int) -> list[str]:
    """A long answer, cut at the ends of lines where it can be."""
    pieces: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        pieces.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return [*pieces, text]


async def _answer(part: DiscordPart, message: discord.Message, server: str, line: str, text: str, *, raw: bool) -> None:
    """The answer: where the setting says, as a console's output if it's that, never a ping. A long one is a file, or several messages."""
    options = part.file.section("console")
    quiet = discord.AllowedMentions.none()
    thread: discord.Thread | None = None
    if options.get("answers") == "thread":
        try:
            thread = await message.create_thread(name=masked(line)[:90] or "Answer")
        except discord.HTTPException as error:
            log.info("Couldn't start a thread for a console line, so it's a reply: %s", error)

    async def send(content: str, **more: Any) -> None:
        if thread is not None:
            await thread.send(content, allowed_mentions=quiet, **more)
        else:
            await message.channel.send(content, reference=message.to_reference(fail_if_not_exists=False), mention_author=False, allowed_mentions=quiet, **more)

    head = f"**{cards.clean(server)}**\n" if where_lines_go(part, message.channel) != server and len(part.ctx.servers) > 1 else ""
    shown = text if raw else cards.clean(text)
    if len(shown) <= MOST_SHOWN:
        await send(head + _block(shown, raw))
        return
    pieces = _pieces(shown, MOST_SHOWN)
    if options.get("long_answers") == "split" and len(pieces) <= MOST_SPLIT:
        for number, piece in enumerate(pieces):
            await send((head if number == 0 else "") + _block(piece, raw))
        return
    try:
        await send(head + say(part.file, "console.long"), file=discord.File(io.BytesIO(text.encode("utf-8")), "answer.txt"))
    except discord.Forbidden:
        await send(head + _block(pieces[0], raw) + "\n" + say(part.file, "console.cut_short"))


def _block(text: str, raw: bool) -> str:
    return "```\n" + text.replace("```", "'''") + "\n```" if raw else text


class _Sure(SaysWhatWentWrong, ui.View):
    """Are you sure? For whoever typed the line alone, and it goes nowhere until they say."""

    def __init__(self, part: DiscordPart, message: discord.Message, server: str, line: str) -> None:
        super().__init__(timeout=SURE_SECONDS)
        self.part, self.message, self.server, self.line = part, message, server, line
        self.asked: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        consoleclear.touched(self.part, self.message.channel.id)
        if interaction.user.id == self.message.author.id:
            return True
        await answer(interaction, say(self.part.file, "console.not_yours"))
        return False

    @ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _button: ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content=say(self.part.file, "console.sending", line=_code(masked(self.line)), server=cards.clean(self.server)), view=None)
        await _run(self.part, self.message, self.server, self.line, someone_from(interaction.user))

    @ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content=say(self.part.file, "console.not_sent"), view=None)

    async def on_timeout(self) -> None:
        if self.asked is not None:
            try:
                await self.asked.edit(content=say(self.part.file, "console.nobody_answered"), view=None)
            except discord.HTTPException:
                pass


class _Which(SaysWhatWentWrong, ui.View):
    """A line, and nothing yet says which server: asked once, then remembered."""

    def __init__(self, part: DiscordPart, message: discord.Message, line: str) -> None:
        super().__init__(timeout=SURE_SECONDS)
        self.part, self.message, self.line = part, message, line
        self.add_item(_WhichSelect(part))
        self.asked: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        consoleclear.touched(self.part, self.message.channel.id)
        if interaction.user.id == self.message.author.id:
            return True
        await answer(interaction, say(self.part.file, "console.not_yours"))
        return False

    async def on_timeout(self) -> None:
        if self.asked is not None:
            try:
                await self.asked.edit(content=say(self.part.file, "console.nobody_answered"), view=None)
            except discord.HTTPException:
                pass


def _menu_hint(part: DiscordPart) -> str:
    """What the menu says. A menu holds 25: past that it says how to reach the rest, as they'd otherwise just not be there."""
    return (say(part.file, "console.menu_past_limit", most=SERVERS_ON_A_MENU) if len(part.ctx.servers) > SERVERS_ON_A_MENU else say(part.file, "console.menu_hint"))[:150]


class _WhichSelect(ui.Select[_Which]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(placeholder=_menu_hint(part), options=[discord.SelectOption(label=str(name)[:100], value=str(name)[:100]) for name in list(part.ctx.servers)[:SERVERS_ON_A_MENU]])

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view.stop()
        server = self.values[0]
        part = view.part
        part.status.store(_remember_key(part, str(interaction.user.id)), value=server)
        await interaction.response.edit_message(content=say(part.file, "console.goes_to", server=cards.clean(server)), view=None)
        await _ask_or_run(part, view.message, server, view.line, someone_from(interaction.user))


async def picked(part: DiscordPart, interaction: discord.Interaction) -> None:
    """The menu at the top of the channel."""
    consoleclear.touched(part, interaction.channel_id)
    server = str(((interaction.data or {}).get("values") or [""])[0])
    someone = someone_from(interaction.user)
    for_everyone = part.file.section("console").get("remember_server") == "channel"
    if not _mine(part, someone) or (for_everyone and not any(can_act(rank_of(part.file, someone, str(name))) for name in part.ctx.servers)):
        await answer(interaction, say(part.file, "console.not_for_you"))
    elif server not in part.ctx.servers:
        await answer(interaction, f"There's no server called {cards.clean(server)} now.")
    else:
        part.status.store(_remember_key(part, str(interaction.user.id)), value=server)
        await answer(interaction, say(part.file, "console.goes_to", server=cards.clean(server)))


def _menu_form(part: DiscordPart, chosen: str) -> dict[str, Any]:
    config = part.file
    text = say(config, "console.menu", prefix=part.ctx.command_prefix)
    if chosen:
        text += "\n\n" + say(config, "console.channel_goes_to", server=cards.clean(chosen))
    link, when = part.status.stored(consoleclear.CLEARED)
    if when:
        text += "\n\n" + say(config, "console.last_cleared" if link else "console.last_cleared_no_file", when=f"<t:{int(float(when))}:R>", link=link)
    choices = [{"label": str(name)[:100], "value": str(name)[:100], "default": str(name) == chosen} for name in list(part.ctx.servers)[:SERVERS_ON_A_MENU]]
    return {"embeds": [{"title": say(config, "console.menu_title"), "description": text, "color": colour_value(config, "blurple")}],
            "components": [{"type": 1, "components": [{"type": 3, "custom_id": PICK, "placeholder": say(config, "console.menu_hint")[:150], "options": choices}]}],
            "allowed_mentions": {"parse": []}}


def keep_menu(part: DiscordPart) -> None:
    """The picker channel's menu: there while there's more than one server to pick from, right when they change, gone when it's not wanted. Called now and then."""
    config = part.file
    now = features.state_of(features.BY_KEY["console"], config, part.facts)[0]
    if part.facts is None or now == features.WAITING:  # not known, or not for long: what's there stays
        return
    channel = kept.channel_of(part, config.section("console").get("channel")) if now == features.ON else ""
    if not channel or len(part.ctx.servers) < 2:
        kept.take_down(part, MENU)
        return
    chosen = part.status.stored(CHANNEL)[1] if config.section("console").get("remember_server") == "channel" else ""
    kept.keep(part, MENU, channel, _menu_form(part, chosen), pin=True)
