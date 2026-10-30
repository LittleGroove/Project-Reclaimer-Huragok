"""The part that runs the bot: reads discord.toml (and again whenever it changes), runs the outbox, and in bot mode keeps
the connection to Discord up. Always on: its switch is discord.toml's `mode`, and a mistake there or a Discord that says
no only ever turns the bot off, with the reason on the dashboard; the servers carry on.

Before signing in it asks Discord's web API about the token and the portal's switches, so a bad token or a missing switch
never costs one of the 1,000 logins a day. Past [limits] logins_per_day it stops until tomorrow and says so.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from ..core.background import background
from ..core.part import Part, every
from ..core.settings import launcher
from ..lore import HURAGOK
from . import features, records, rest
from .config import FILE, DiscordConfig, DiscordFileError, load, reload_if_changed
from .facts import Facts, gather
from .outbox import Job, Outbox
from .rest import BadToken, DiscordError
from .speakers import Poster, unknown_texts

CONNECT_WAITS = (15, 30, 60, 120, 300)  # seconds between tries when Discord can't be reached
STATE_SECONDS = 30.0  # the state file says it's still true this often, for a second window
STOP_DRAIN_SECONDS = 3.0



class TooManyLogins(Exception):
    """Past [limits] logins_per_day: Discord resets the token at 1,000."""


class DiscordPart(Part):
    persona = HURAGOK
    name = "discord"
    description = "The Discord bot: discord.toml says what it does (J in the window, or /huragok setup)"
    always_on = True  # its switch is discord.toml's mode

    file: DiscordConfig
    client: Any = None
    facts: Facts | None = None  # what Discord said last: panels show it, the check always asks again
    state = "off"
    why = ""
    bot_name = ""
    guild_name = ""
    _token = ""  # the token the running bot signed in with
    _intents: frozenset[str] = frozenset()
    _bot_task: asyncio.Task[None] | None = None
    _state_written = 0.0
    _reload_problem = ""

    async def setup(self) -> None:
        self.home = Path(self.ctx.home)
        self.path = self.home / FILE
        self.records = self.home / "records"
        self.file = load(self.path)
        self.outbox = Outbox(on_refused=self._refused)
        self.poster = Poster(lambda: self.file, self.outbox)
        self.ctx.spawn("Discord's outbox", self.outbox.run)
        self._say_warnings(self.file)
        if self.file.error:
            self.log.error("The Discord bot stays off: %s", self.file.error)
        await self._apply(previous=None)

    def _say_warnings(self, config: DiscordConfig) -> None:
        for line in [*config.warnings, *unknown_texts(config)]:
            self.log.warning(line)

    # discord.toml, live -------------------------------------------------------------------------------------

    @every(seconds=2)
    async def look(self) -> None:
        await self.reload()
        client = self.client
        if client is not None and client.commands_retry_at is not None and time.monotonic() >= client.commands_retry_at:
            await client.try_commands()
        if time.monotonic() - self._state_written >= STATE_SECONDS:
            self.write_state()

    async def reload(self, *, force: bool = False) -> bool:
        """discord.toml again if it changed (always, with force). True: a new one is in force."""
        try:
            fresh = reload_if_changed(self.file, force=force)
        except DiscordFileError as error:
            if str(error) != self._reload_problem:
                self._reload_problem = str(error)
                self.log.error("discord.toml not reloaded, keeping what was in force: %s", error)
                self.ctx.refresh_problems()
            return False
        if self._reload_problem:  # good again, even if that's just as it was
            self._reload_problem = ""
            self.ctx.refresh_problems()
        if fresh is None:
            return False
        previous, self.file = self.file, fresh
        self.log.info("discord.toml reloaded")
        self._say_warnings(fresh)
        await self._apply(previous)
        return True

    async def _apply(self, previous: DiscordConfig | None) -> None:
        """Make what runs what the file says: the bot only restarts for what needs it (a login costs)."""
        config = self.file
        token = config.token if config.mode == "bot" else ""
        intents = frozenset(features.needed_intents(config))
        if token != self._token or (token and intents != self._intents):
            await self._stop_bot()
            self.poster.delivery.forget()
            self.facts = None
            self._token, self._intents = token, intents
            if token:
                self._bot_task = asyncio.ensure_future(self._run_bot(token))
        elif self.client is not None and previous is not None and config.guild != previous.guild:
            self.poster.delivery.forget()
            self.client.say_ready()
            await self.client.try_commands()
        if config.error:
            self.set_state("off", config.error)
        elif config.mode == "off":
            self.set_state("off", "")
        elif config.mode == "webhook":
            self.set_state("webhook", "")
        elif not token:
            self.set_state("waiting", "mode is bot, but there's no token")
        else:
            self.write_state()
            self.look_at_discord()
        self.ctx.refresh_problems()

    def look_at_discord(self) -> None:
        """What's in the Discord now, for the panels: in the background, and a failure only leaves the last look."""
        config = self.file

        async def look() -> None:
            try:
                self.facts = await asyncio.to_thread(gather, config.token, config.guild)
                changed = await asyncio.to_thread(features.names_to_ids, config, self.facts)
            except DiscordError as error:
                self.log.debug("Couldn't read Discord: %s", error)
            except OSError as error:  # a refused edit: the names still work
                self.log.warning("Couldn't put IDs in discord.toml: %s", error)
            else:
                if changed:
                    self.log.info("discord.toml: IDs put in place of names: %s", ", ".join(changed))

        background(look(), "reading Discord", self.log)

    # The state ----------------------------------------------------------------------------------------------

    def set_state(self, state: str, why: str = "") -> None:
        changed = (state, why) != (self.state, self.why)
        self.state, self.why = state, why
        if changed and why:
            (self.log.warning if state == "waiting" else self.log.info)("The Discord bot: %s", why)
        self.write_state()
        if changed:
            self.ctx.refresh_problems()

    def write_state(self) -> None:
        self._state_written = time.monotonic()
        try:
            records.write_state(self.records, mode=self.file.mode, state=self.state, why=self.why, bot=self.bot_name,
                                guild=self.guild_name, waiting=self.outbox.waiting)
        except OSError as error:
            self.log.debug("Couldn't write the bot's state: %s", error)

    def problems(self) -> list[str]:
        """What the dashboard shows in red."""
        lines: list[str] = []
        if not hasattr(self, "file"):  # not set up yet
            return lines
        if self.file.error:
            lines.append(f"Discord bot off: {self.file.error}")
        elif self._reload_problem:
            lines.append(f"discord.toml not reloaded: {self._reload_problem}")
        elif self.state == "waiting" and self.why:
            lines.append(f"Discord bot: {self.why}")
        return lines

    def _refused(self, job: Job, error: DiscordError) -> None:
        if isinstance(error, BadToken):
            self.set_state("waiting", f"Discord refused the token: fix it in Huragok's window (J). ({error})")

    # The bot ------------------------------------------------------------------------------------------------

    async def _run_bot(self, token: str) -> None:
        try:
            from . import client as gateway  # only in bot mode: webhook mode runs without discord.py
        except ImportError as error:
            self.set_state("waiting", f"discord.py isn't installed ({error}): start Huragok with {launcher()} again, which "
                                 "installs what's missing")
            return
        tries = 0
        while True:
            used = records.logins_today(self.records)
            allowed = int(self.file.limit("logins_per_day"))
            if used >= allowed:
                self.set_state("waiting", f"the bot signed in {used} times today ([limits] logins_per_day): it stays off "
                                     "until tomorrow, because Discord resets a token at 1,000. Something keeps "
                                     "restarting it")
                await asyncio.sleep(3600)
                continue
            self.set_state("signing in")
            try:
                application = await asyncio.to_thread(rest.application, token)
                self.client = gateway.Bot(self, frozenset(features.needed_intents(self.file)), application)
                await self.client.run_until_closed(token)
                return
            except asyncio.CancelledError:
                raise
            except BadToken as error:
                self.set_state("waiting", f"Discord refused the token: fix it in Huragok's window (J). ({error})")
                return
            except TooManyLogins as error:
                self.set_state("waiting", str(error))
            except gateway.FATAL as error:
                self.set_state("waiting", gateway.why_fatal(error))
                return
            except Exception as error:  # noqa: BLE001 - no way to Discord: the bot tries again, nothing else notices
                self.log.debug("The bot's connection failed", exc_info=True)
                self.set_state("waiting", f"can't reach Discord just now{gateway.why_down(error)}; trying again")
            finally:
                await self._close_client()
            await asyncio.sleep(CONNECT_WAITS[min(tries, len(CONNECT_WAITS) - 1)])
            tries += 1

    async def _close_client(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                self.log.debug("Closing the bot's connection failed", exc_info=True)

    async def _stop_bot(self) -> None:
        task, self._bot_task = self._bot_task, None
        await self._close_client()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.bot_name = self.guild_name = ""

    def bot_ready(self, name: str, guild_name: str) -> None:
        self.bot_name, self.guild_name = name, guild_name
        self.set_state("online", "")
        self.look_at_discord()

    def bot_gone(self) -> None:
        if self.state == "online":
            self.set_state("signing in", "")

    def count_login(self) -> None:
        """Called before each login to the gateway (client.py)."""
        used = records.logins_today(self.records)
        allowed = int(self.file.limit("logins_per_day"))
        if used >= allowed:
            raise TooManyLogins(f"the bot signed in {used} times today ([limits] logins_per_day): it stays off until "
                                "tomorrow, because Discord resets a token at 1,000. Something keeps restarting it")
        records.count_login(self.records)

    async def teardown(self) -> None:
        await self._stop_bot()
        await self.outbox.drain(STOP_DRAIN_SECONDS)
        self.state = "stopped"
        self.write_state()


__all__ = ["DiscordPart", "TooManyLogins"]
