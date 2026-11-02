"""discord.py's side of the bot, imported only in bot mode: the gateway (the connection, its events) and the interactions
(/huragok, and the setup's pickers in panels.py). What's posted goes through the outbox and rest.py instead; an
interaction's own answer goes straight back, since Discord wants it within 3 seconds.

Slash commands are registered only when the list changes (Discord allows 200 a day per Discord, and the watchdog restarts
Huragok whenever it must). With no `guild` in discord.toml, /huragok is there in every Discord the bot is in, so that
/huragok claim can say which one is its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands

from . import features, records, rest, tidy
from .config import add_owner, set_guild
from .outbox import ANSWER
from .ranks import Someone, rank_of
from .rest import INTENT_FLAGS
from .speakers import PERSONAS, message, say
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)
logging.getLogger("discord").setLevel(logging.WARNING)  # its own news is for its own debugging
logging.getLogger("discord.client").setLevel(logging.ERROR)  # "voice will NOT be supported": nothing here speaks

FATAL = (discord.LoginFailure, discord.PrivilegedIntentsRequired)
NOT_OWNER = ("Only the owners named in discord.toml can do this. Huragok's host can give you a code from Huragok's "
             "window (J) for `/huragok claim`.")
ANOTHER_DISCORD = "This bot belongs to another Discord."
CHECK_LIMIT = 3900  # characters in one card: Discord allows 4,096
CHECK_TOTAL = 5600  # in all the cards of one message: Discord allows 6,000
MOST_IN_A_MESSAGE = 2000  # Discord's
DEAD_PANEL_SECONDS = 2.0  # a panel still open answers within this, so anything that hasn't is one that's gone
COMMANDS_RETRY_SECONDS = 300.0  # when the bot isn't allowed to add its commands: until it's invited properly
COMMANDS_SETTLE_SECONDS = 2.0  # a burst of switches is one registration
# Measured, 31 October: five overwrites in 97 seconds were taken, the sixth told to wait 19 s, and after that about one every 30 s.
COMMANDS_BURST, COMMANDS_BURST_SECONDS = 4, 120.0


class SaysWhatWentWrong:
    """On a view or a form: what fails in one gets a word back, not "This interaction failed". discord.py only logs it."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, *_item: Any) -> None:
        log.error("A press in Discord failed: %s", error, exc_info=(type(error), error, error.__traceback__))
        try:
            await answer(interaction, "That didn't work. Huragok's host can see why in its log.")
        except (discord.HTTPException, RuntimeError):
            pass


def why_fatal(error: BaseException) -> str:
    if isinstance(error, discord.PrivilegedIntentsRequired):
        return ("Discord refused an intent the bot needs: switch on Message Content Intent and Server Members Intent in the developer "
                "portal (the Bot page, under Privileged Gateway Intents), as the features you turned on need them")
    return f"Discord refused the token: fix it in Huragok's window (J). ({error})"


def worth_trying_again(error: BaseException) -> bool:
    """A portal switch turned off while it ran: signing in again asks only for what's allowed. A bad token won't get better."""
    return isinstance(error, discord.PrivilegedIntentsRequired)


def elsewhere(part: Any, interaction: discord.Interaction) -> bool:
    """Someone in a Discord that isn't this bot's own."""
    return bool(part.file.guild) and str(interaction.guild_id) != part.file.guild


def part_of(interaction: discord.Interaction) -> Any:
    return interaction.client.part  # type: ignore[attr-defined]


def why_down(error: BaseException) -> str:
    """What to add to "can't reach Discord": what the network said, when it was the network that said it."""
    return f" ({error})" if isinstance(error, (OSError, aiohttp.ClientError, discord.DiscordException)) else ""


def someone_from(user: discord.abc.User) -> Someone:
    """Whoever used a command, as far as ranks go (Discord sends their roles with every interaction)."""
    roles = {str(role.id): role.name for role in getattr(user, "roles", [])}
    permissions = getattr(user, "guild_permissions", None)
    return Someone(str(user.id), roles, administrator=bool(permissions and permissions.administrator),
                   timed_out=bool(getattr(user, "is_timed_out", lambda: False)()))


class Bot(discord.Client):
    def __init__(self, part: DiscordPart, wanted: frozenset[str], application: dict[str, Any]) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # A switch that's off in the portal is the feature's to report (it waits); asking anyway refuses the login.
        flags = int(application.get("flags") or 0)
        self.asked_for = {intent for intent in wanted if flags & INTENT_FLAGS[intent]}
        if "message_content" in self.asked_for:
            intents.message_content = intents.guild_messages = True
        if "members" in self.asked_for:
            intents.members = True
        super().__init__(intents=intents, max_ratelimit_timeout=float(part.file.limit("wait_seconds")),
                         member_cache_flags=discord.MemberCacheFlags.none(), chunk_guilds_at_startup=False)
        self.part = part
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(HuragokCommands(self))
        self.tree.on_error = self._command_failed  # type: ignore[method-assign]
        self.commands_lock = asyncio.Lock()
        self.game_names: list[str] = []  # the slash commands made from in-game ones, as they stand
        self.commands_retry_at: float | None = None
        self.commands_due = 0.0
        self.commands_task: asyncio.Task[None] | None = None
        self.commands_sent: list[float] = []  # when they were last registered

    async def run_until_closed(self, token: str) -> None:
        async with self:
            await self.start(token)

    async def before_identify_hook(self, shard_id: int | None, *, initial: bool = False) -> None:
        self.part.count_login()  # raises past [limits] logins_per_day
        await super().before_identify_hook(shard_id, initial=initial)

    async def show_presence(self, activity: str, line: str, dot: str) -> bool:
        """The bot's status line and dot (status.py). A custom one has no "watching" in front. Whether it got there."""
        kinds = {"playing": discord.ActivityType.playing, "listening": discord.ActivityType.listening, "watching": discord.ActivityType.watching,
                 "competing": discord.ActivityType.competing}
        shown = discord.CustomActivity(name=line) if activity == "custom" else discord.Activity(type=kinds.get(activity, discord.ActivityType.watching), name=line)
        if self.ws is None or not self.is_ready():
            return False  # between connections: it's said again when it's back
        try:
            await self.change_presence(activity=shown, status=discord.Status(dot))
        except (discord.ConnectionClosed, OSError, aiohttp.ClientError) as error:  # Discord going away isn't Huragok failing
            log.debug("Couldn't show the bot's status: %s", error)
            return False
        return True

    def say_ready(self) -> None:
        guild = self.get_guild(int(self.part.file.guild)) if self.part.file.guild.isdecimal() else None
        self.part.bot_ready(str(self.user), guild.name if guild else "")

    def commands_soon(self) -> None:
        """The list has changed: registered in a moment, not now. A press can't wait on it, as Discord wants an answer within 3 seconds and registering can be told to wait half a minute."""
        self.commands_due = time.monotonic() + COMMANDS_SETTLE_SECONDS
        if self.commands_task is None or self.commands_task.done():
            self.commands_task = asyncio.ensure_future(self._commands_when_due())

    def _next_allowed(self) -> float:
        self.commands_sent = [when for when in self.commands_sent if time.monotonic() - when < COMMANDS_BURST_SECONDS]
        return self.commands_sent[-COMMANDS_BURST] + COMMANDS_BURST_SECONDS if len(self.commands_sent) >= COMMANDS_BURST else 0.0

    async def _commands_when_due(self) -> None:
        while True:
            while (wait := max(self.commands_due, self._next_allowed()) - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            if not self.is_ready():
                return  # signing in has it to do anyway
            due = self.commands_due
            await self.try_commands()
            if self.commands_due == due:  # nothing else changed while that was going on
                return

    async def close(self) -> None:
        if self.commands_task is not None:
            self.commands_task.cancel()
        await super().close()

    async def try_commands(self) -> None:
        """The commands registered; a bot invited without them tries again now and then, until it's invited properly
        (Discord tells nobody when that happens)."""
        self.commands_retry_at = None
        try:
            await self.commands_changed()
        except discord.Forbidden:
            self.commands_retry_at = time.monotonic() + COMMANDS_RETRY_SECONDS
            self.part.set_state("waiting", "the bot isn't allowed to add its commands in its Discord: invite it (again) "
                                           "with the link in Huragok's window (J), which asks for applications.commands")
        except (discord.HTTPException, discord.RateLimited) as error:
            self.commands_retry_at = time.monotonic() + COMMANDS_RETRY_SECONDS
            log.warning("Couldn't register the slash commands: %s", error)
        except (RuntimeError, aiohttp.ClientError):
            if not self.is_closed():  # the bot closing as this went: it isn't wanted any more
                raise
        else:
            if self.part.state == "waiting":
                self.say_ready()

    async def on_ready(self) -> None:
        self.say_ready()
        await self.try_commands()

    async def on_resumed(self) -> None:
        if self.user is not None and self.part.state == "signing in":
            self.say_ready()

    # What's fixed in Discord shows here at once, rather than up to five minutes later, while the host sits wondering. Asked to look: it comes to one read however many of these arrive.
    async def on_guild_channel_create(self, _channel: Any) -> None:
        self.part.look_at_discord()

    async def on_guild_channel_delete(self, _channel: Any) -> None:
        self.part.look_at_discord()

    async def on_guild_channel_update(self, _before: Any, _after: Any) -> None:
        self.part.look_at_discord()

    async def on_guild_role_create(self, _role: Any) -> None:
        self.part.look_at_discord()

    async def on_guild_role_delete(self, _role: Any) -> None:
        self.part.look_at_discord()

    async def on_guild_role_update(self, _before: Any, _after: Any) -> None:
        self.part.look_at_discord()

    async def on_member_update(self, _before: discord.Member, after: discord.Member) -> None:
        if self.user is not None and after.id == self.user.id:  # its own roles
            self.part.look_at_discord()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Added, or added back: being removed took its commands and webhooks, and maybe changed what it may do."""
        self.part.poster.delivery.forget()
        self.part.look_at_discord()
        self.say_ready()
        await self.try_commands()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.warning("The bot was removed from %s. Discord deleted its commands and webhooks with it: its commands come back if it's added again, but cards already posted can't be edited after that", guild.name)
        self.part.poster.delivery.forget()
        self.part.look_at_discord()
        self.say_ready()

    async def on_disconnect(self) -> None:
        self.part.bot_gone()

    async def on_message(self, message: discord.Message) -> None:
        """Only with the typed console on is there anything to hear: without its switch the bot doesn't ask for messages at all."""
        from .console import heard

        try:
            await heard(self.part, message)
        except Exception as error:
            if self.is_closed():  # it was still going when the bot closed: there's nothing left to finish it with
                log.warning("A line typed in the console channel was cut off as the bot closed (%s). If it held a password, delete it by hand.", error)
                return
            log.exception("A line typed in the console channel failed")

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """A button on a card: stateless, so one from before a restart still works. The setup's panels have views of their own."""
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if interaction.type is discord.InteractionType.modal_submit:  # one still open answers by itself. One from before a sign-in, or left over its time, has nobody to: they should know it didn't go
            await asyncio.sleep(DEAD_PANEL_SECONDS)
            if not interaction.response.is_done():
                try:
                    await answer(interaction, "That form had closed, so it wasn't sent. Nothing was done: it'll need filling in again.")
                except discord.HTTPException:
                    pass
            return
        if interaction.type is not discord.InteractionType.component:
            return
        if not custom_id.startswith("huragok:"):  # a panel's: gone if the bot's signed in again since, or it's been left over its time, and nobody would answer
            await asyncio.sleep(DEAD_PANEL_SECONDS)
            if not interaction.response.is_done():
                try:
                    await answer(interaction, "This panel has closed. `/huragok setup` or `/server` opens a new one.")
                except discord.HTTPException:
                    pass
            return
        from .buttons import pressed
        from .asking import contact_pressed
        from .console import PICK, picked
        from .contact import OPEN

        try:
            if custom_id == PICK:
                await picked(self.part, interaction)
            elif custom_id == OPEN:
                await contact_pressed(self.part, interaction)
            else:
                await pressed(self.part, interaction, custom_id)
        except Exception as error:
            if self.is_closed():
                log.warning("A button pressed as the bot closed was cut off (%s): %s", custom_id, error)
                return
            log.exception("A button failed: %s", custom_id)
            try:
                await answer(interaction, "That didn't work. Huragok's host can see why in its log.")
            except discord.HTTPException:
                pass

    def follow_switches(self) -> None:
        """The commands whose features are on, and only those."""
        from .staff import FlagCommands, NoteCommands, NotifyCommands

        wanted = features.slash_commands(self.part.file)
        for name, group in (("note", NoteCommands), ("flag", FlagCommands), ("notify", NotifyCommands)):
            there = self.tree.get_command(name) is not None
            if name in wanted and not there:
                self.tree.add_command(group(self))
            elif name not in wanted and there:
                self.tree.remove_command(name)
        from .slash import commands_for
        from .serverpanel import server_panel, status_command
        from .staff import case_command, help_command, open_ticket, tickets_command

        from .asking import appeal_command, report_command, report_message

        for name, one in (("ticket", open_ticket), ("server", server_panel), ("report", report_command), ("appeal", appeal_command), ("case", case_command), ("status", status_command), ("help", help_command), ("tickets", tickets_command)):
            there = self.tree.get_command(name) is not None
            if name in wanted and not there:
                self.tree.add_command(one)
            elif name not in wanted and there:
                self.tree.remove_command(name)
        right_click = self.tree.get_command("Report message", type=discord.AppCommandType.message) is not None
        if "report-message" in wanted and not right_click:
            self.tree.add_command(report_message)
        elif "report-message" not in wanted and right_click:
            self.tree.remove_command("Report message", type=discord.AppCommandType.message)
        for name in self.game_names:
            self.tree.remove_command(name)
        self.game_names = []
        needed = str(self.part.file.section("commands").get("staff_permission") or "")
        for name in ("note", "flag", "ticket", "case", "server", "tickets"):  # as the game's staff commands do. Rank's what decides, whatever shows
            command = self.tree.get_command(name)
            if command is not None:
                command.default_permissions = discord.Permissions(**{needed: True}) if needed in discord.Permissions.VALID_FLAGS else None
        if "game" in wanted:
            for command in commands_for(self.part):
                self.tree.add_command(command)
                self.game_names.append(command.name)

    async def _commands_there(self, place: discord.Object) -> set[str] | None:
        """What Discord has now, since it deletes them all when the bot's removed, and says nothing. Reading is free of the 200 a day that registering costs. None: it wouldn't say just now.
        Refused outright, the bot's not been invited properly, which try_commands says."""
        try:
            return {command.name for command in await self.tree.fetch_commands(guild=place)}
        except discord.Forbidden:
            raise
        except discord.HTTPException as error:
            log.info("Couldn't read the slash commands back: %s", error)
            return None

    async def commands_changed(self) -> None:
        """Register what's new where it's new, and nothing else."""
        async with self.commands_lock:
            self.follow_switches()
            mine = self.part.file.guild
            where = [mine] if mine else [str(guild.id) for guild in self.guilds]
            known = records.registered(self.part.records)
            for guild_id in where:
                place = discord.Object(id=int(guild_id))
                self.tree.clear_commands(guild=place)  # a copy keeps what's since been taken away
                self.tree.copy_global_to(guild=place)
                wanted = [command.to_dict(self.tree) for command in self.tree.get_commands(guild=place)]
                digest = hashlib.sha256(json.dumps(wanted, sort_keys=True).encode("utf-8")).hexdigest()
                there = await self._commands_there(place)
                if known.get(guild_id) != digest or (there is not None and there != {str(command["name"]) for command in wanted}):
                    await self.tree.sync(guild=place)
                    self.commands_sent.append(time.monotonic())
                    known[guild_id] = digest
                    log.info("Slash commands registered in Discord %s", guild_id)
            for guild_id in [g for g in known if mine and g != mine]:  # the bot moved on: it leaves none behind
                place = discord.Object(id=int(guild_id))
                self.tree.clear_commands(guild=place)
                try:
                    await self.tree.sync(guild=place)
                except discord.HTTPException as error:
                    log.info("Couldn't take the commands out of Discord %s: %s", guild_id, error)
                del known[guild_id]
            records.save_registered(self.part.records, known)

    async def _command_failed(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        log.error("A Discord command failed: %s", error, exc_info=(type(error), error, error.__traceback__))
        try:
            await answer(interaction, "That didn't work. Huragok's host can see why in its log.")
        except discord.HTTPException:
            pass


RESERVED_NAMES = {"vergil", "superintendent", "huragok", "automod", "console", "server"}


def named(user: Any) -> str:
    """Who someone is, in this Discord: on cards, and whenever they're speaking for themselves. `[commands] shown_as` is for what players read."""
    name = str(getattr(user, "display_name", "") or getattr(user, "name", "") or "someone")
    return "".join(letter for letter in name if letter.isprintable()).strip()[:64] or str(getattr(user, "name", "someone"))[:64]


def acting_as(part: Any, user: Any) -> str:
    """How someone acting from Discord is named to players, and in what the game records: `[commands] shown_as`. Their name in this Discord, unless it says otherwise. Staff read their real one: `named`."""
    name = str(getattr(user, "display_name", "") or getattr(user, "name", "") or "someone")
    name = "".join(letter for letter in name if letter.isprintable()).strip() or str(getattr(user, "name", "someone"))
    if name.casefold() in RESERVED_NAMES:  # what Vergil's acts go by: a line under that name would read as its own, with no record and no card
        name = f"{name} (Discord)"
    return str(part.file.section("commands").get("shown_as") or "{name}").replace("{name}", name)[:64]


async def answer(interaction: discord.Interaction, text: str = "", *, lasts: str = "done", **options: Any) -> None:
    """A private answer, whether the interaction was answered yet or not. It goes from their screen after a while (`lasts`: done, or looks for what they asked to read), so nobody has a column of these.
    Always by its own ID when it's a follow-up: on a button, the "original" is the card."""
    from . import tidy

    text = text[:MOST_IN_A_MESSAGE]  # what someone typed may be echoed in it
    seconds = tidy.minutes_for(part_of(interaction), lasts) * 60
    if interaction.response.is_done():
        sent = await interaction.followup.send(text, ephemeral=True, wait=True, **options)
        if seconds:
            await sent.delete(delay=seconds)
    else:
        await interaction.response.send_message(text, ephemeral=True, delete_after=seconds or None, **options)


def findings_cards(findings: list[features.Finding], left: list[features.Step] | None = None) -> list[discord.Embed]:
    """The check as cards: what's wrong first, then what's left to set up. Nothing's wrong isn't the same as being ready."""
    lines = [f"{'✅' if f.good else '⚠️'} **{f.where}**: {f.text}"[:1000] for f in findings]
    if left:
        lines = [f"⬜ {step.text}"[:1000] for step in left] + lines
    cards: list[discord.Embed] = []
    text = ""
    used = 0
    for number, line in enumerate(lines):
        if used + len(line) > CHECK_TOTAL:
            text += f"…and {len(lines) - number} more: the window's check (J) lists everything.\n"
            break
        if len(text) + len(line) + 1 > CHECK_LIMIT:
            cards.append(discord.Embed(description=text))
            text = ""
        text += line + "\n"
        used += len(line) + 1
    if text or not cards:
        cards.append(discord.Embed(description=text or "Nothing to check."))
    bad = sum(1 for f in findings if not f.good)
    cards[0].title = "Everything checks out" if not bad else f"{bad} thing{'s' if bad != 1 else ''} to fix"
    cards[0].colour = discord.Colour.green() if not bad else discord.Colour.gold()
    return cards


class HuragokCommands(app_commands.Group):
    """/huragok: setting the bot up from inside Discord, for the owners named in discord.toml. Every answer is private."""

    def __init__(self, bot: Bot) -> None:
        super().__init__(name="huragok", description="Set up and check Huragok's bot")  # only ever in Discords, by sync
        self.bot = bot

    @property
    def part(self) -> DiscordPart:
        return self.bot.part

    async def _refused(self, interaction: discord.Interaction, *, owner: bool = True) -> bool:
        """Says why not, when this Discord or this person may not."""
        mine = self.part.file.guild
        if mine and str(interaction.guild_id) != mine:
            await answer(interaction, ANOTHER_DISCORD)
            return True
        if owner and rank_of(self.part.file, someone_from(interaction.user)) != "owner":
            await answer(interaction, NOT_OWNER)
            return True
        return False

    @app_commands.command(description="Become an owner, with the code from Huragok's window (J)")
    @app_commands.describe(code="The code Huragok's window shows")
    async def claim(self, interaction: discord.Interaction, code: str) -> None:
        if await self._refused(interaction, owner=False):
            return
        if not records.use_code(self.part.records, code, str(interaction.user.id)):
            await answer(interaction, "That code isn't right, or it ran out. Huragok's window (J) makes a new one.")
            return
        config = self.part.file
        guild = interaction.guild
        if not config.guild and guild is not None:
            set_guild(self.part.path, str(guild.id), guild.name)
        add_owner(self.part.path, str(interaction.user.id), interaction.user.name)
        log.info("%s claimed the bot from Discord: an owner now", interaction.user.name)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.claim", who=interaction.user.name))
        await answer(interaction, "You're an owner of this bot now. `/huragok setup` is next.")

    @app_commands.command(description="Set the bot up: ranks, channels, features")
    async def setup(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        from .panels import Home

        await Home(self.part).open(interaction)

    @app_commands.command(description="Every feature and its state, with a switch for each")
    async def features(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        from .panels import Features

        await Features(self.part).open(interaction)

    @app_commands.command(description="Check everything the features that are on need")
    async def check(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        findings, facts = await to_thread(features.check, self.part.file)
        self.part.facts = facts or self.part.facts  # it's what it just read: what's on, here, says the same
        left = [step for step in features.steps(self.part.file, self.part.facts) if not step.done]
        await answer(interaction, embeds=findings_cards(findings, left), lasts="looks")

    @app_commands.command(description="Post a test here as the Superintendent, Vergil and Huragok")
    async def test(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        config = self.part.file
        lasting = tidy.minutes_for(self.part, "tests") * 60
        channel = str(interaction.channel_id)

        def goes_later(reply: Any) -> None:
            new = str(reply.data.get("id") or "") if isinstance(reply, rest.Reply) and isinstance(reply.data, dict) else ""
            if lasting and new:
                tidy.later(self.part, channel, new, lasting, "a test post")

        for persona in PERSONAS:
            self.part.poster.post(persona, message(say(config, f"test.{persona}")), channel=channel, priority=ANSWER, what=f"{persona}'s test", on_done=goes_later)
        await answer(interaction, "Three test posts are on their way here." + (f" They go after {lasting / 60:g} minutes." if lasting else ""))

    @app_commands.command(description="Show you every message, in its colour, with where it goes")
    async def gallery(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        for embeds in self.part.feeds.gallery():
            await interaction.followup.send(embeds=[discord.Embed.from_dict(embed) for embed in embeds], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await answer(interaction, "That's every message. `/huragok setup` → Messages changes them.", lasts="looks")

    @app_commands.command(description="Read discord.toml again now")
    async def reload(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        await self.part.reload(force=True)
        problem = self.part.problems()
        warnings = self.part.file.warnings
        lines = [*problem, *warnings] or ["No problems in it."]
        await answer(interaction, "discord.toml read again.\n" + "\n".join(f"- {line}" for line in lines)[:1800], lasts="looks")
