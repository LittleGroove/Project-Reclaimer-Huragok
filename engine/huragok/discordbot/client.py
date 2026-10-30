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

from . import features, records
from .config import add_owner, set_guild
from .outbox import ANSWER
from .ranks import Someone, rank_of
from .rest import INTENT_FLAGS
from .speakers import PERSONAS, message, say

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
COMMANDS_RETRY_SECONDS = 300.0  # when the bot isn't allowed to add its commands: until it's invited properly


def why_fatal(error: BaseException) -> str:
    if isinstance(error, discord.PrivilegedIntentsRequired):
        return ("Discord refused an intent the bot needs: switch on Message Content and Server Members in the developer "
                "portal (the Bot page), as the features you turned on need them")
    return f"Discord refused the token: fix it in Huragok's window (J). ({error})"


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
        if "message_content" in wanted and flags & INTENT_FLAGS["message_content"]:
            intents.message_content = intents.guild_messages = True
        if "members" in wanted and flags & INTENT_FLAGS["members"]:
            intents.members = True
        super().__init__(intents=intents, max_ratelimit_timeout=float(part.file.limit("wait_seconds")),
                         member_cache_flags=discord.MemberCacheFlags.none(), chunk_guilds_at_startup=False)
        self.part = part
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(HuragokCommands(self))
        self.tree.on_error = self._command_failed  # type: ignore[method-assign]
        self.commands_lock = asyncio.Lock()
        self.commands_retry_at: float | None = None

    async def run_until_closed(self, token: str) -> None:
        async with self:
            await self.start(token)

    async def before_identify_hook(self, shard_id: int | None, *, initial: bool = False) -> None:
        self.part.count_login()  # raises past [limits] logins_per_day
        await super().before_identify_hook(shard_id, initial=initial)

    def say_ready(self) -> None:
        guild = self.get_guild(int(self.part.file.guild)) if self.part.file.guild.isdecimal() else None
        self.part.bot_ready(str(self.user), guild.name if guild else "")

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
        except discord.HTTPException as error:
            self.commands_retry_at = time.monotonic() + COMMANDS_RETRY_SECONDS
            log.warning("Couldn't register the slash commands: %s", error)
        else:
            if self.part.state == "waiting":
                self.say_ready()

    async def on_ready(self) -> None:
        self.say_ready()
        await self.try_commands()

    async def on_resumed(self) -> None:
        if self.user is not None and self.part.state == "signing in":
            self.say_ready()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.say_ready()
        await self.try_commands()

    async def on_disconnect(self) -> None:
        self.part.bot_gone()

    async def commands_changed(self) -> None:
        """Register what's new where it's new, and nothing else."""
        async with self.commands_lock:
            mine = self.part.file.guild
            where = [mine] if mine else [str(guild.id) for guild in self.guilds]
            known = records.registered(self.part.records)
            for guild_id in where:
                place = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=place)
                wanted = [command.to_dict(self.tree) for command in self.tree.get_commands(guild=place)]
                digest = hashlib.sha256(json.dumps(wanted, sort_keys=True).encode("utf-8")).hexdigest()
                if known.get(guild_id) != digest:
                    await self.tree.sync(guild=place)
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
            await answer(interaction, "That didn't work; Huragok's log says why.")
        except discord.HTTPException:
            pass


async def answer(interaction: discord.Interaction, text: str = "", **options: Any) -> None:
    """A private answer, whether the interaction was answered yet or not."""
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, **options)
    else:
        await interaction.response.send_message(text, ephemeral=True, **options)


def findings_cards(findings: list[features.Finding]) -> list[discord.Embed]:
    """The check as cards: what's wrong first."""
    lines = [f"{'✅' if f.good else '⚠️'} **{f.where}**: {f.text}"[:1000] for f in findings]
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
        if not records.use_code(self.part.records, code):
            await answer(interaction, "That code isn't right, or it ran out. Huragok's window (J) makes a new one.")
            return
        config = self.part.file
        guild = interaction.guild
        if not config.guild and guild is not None:
            set_guild(self.part.path, str(guild.id), guild.name)
        add_owner(self.part.path, str(interaction.user.id), interaction.user.name)
        log.info("%s claimed the bot from Discord: an owner now", interaction.user.name)
        await self.part.reload(force=True)
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
        findings, _ = await asyncio.to_thread(features.check, self.part.file)
        await interaction.followup.send(embeds=findings_cards(findings), ephemeral=True)

    @app_commands.command(description="Post a test here as each of the three who speak")
    async def test(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        config = self.part.file
        for persona in PERSONAS:
            self.part.poster.post(persona, message(say(config, f"test.{persona}")), channel=str(interaction.channel_id),
                                  priority=ANSWER, what=f"{persona}'s test")
        await answer(interaction, "Three test posts are on their way here.")

    @app_commands.command(description="Read discord.toml again now")
    async def reload(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        await self.part.reload(force=True)
        problem = self.part.problems()
        warnings = self.part.file.warnings
        lines = [*problem, *warnings] or ["No problems in it."]
        await answer(interaction, "discord.toml read again.\n" + "\n".join(f"- {line}" for line in lines)[:1800])
