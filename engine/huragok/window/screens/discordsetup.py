"""J: the Discord bot, set up a step at a time. Every step writes discord.toml, as /huragok setup in Discord and a hand
edit do; the running bot picks the file up within seconds. Discord is read through its web API (discordbot/rest.py), so
this works in a second window too, and whether or not the bot is connected."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...content.catalogue import parse_picks
from ...core import desktop
from ...discordbot import features, rest
from ...discordbot.config import (
    FILE, RANK_KEYS, DiscordConfig, add_owner, load, make_file, remove_owner, set_channel, set_guild,
    set_launcher_webhook, set_mode, set_rank_roles, set_switch, set_token,
)
from ...discordbot.facts import Facts, gather
from ...discordbot.records import CLAIM_MINUTES, new_code, read_state
from ...discordbot.rest import DiscordError
from ...discordbot.speakers import PERSONAS, TEXTS, Delivery, message, say, speaker
from .base import Screens

PORTAL = "https://discord.com/developers/applications"
BOT_STEPS = (
    f"1. Make the bot at {PORTAL}: New Application, then its Bot page. It's your own bot, under your own account.",
    "2. On the Bot page: Reset Token, and copy it. It's a password: anyone who has it can act as your bot.",
    "3. Also there: switch Public Bot off, so nobody else can add it to their Discord.",
    "4. Paste the token here. Huragok checks it, then gives you the link that adds the bot to your Discord.",
)


class DiscordScreens(Screens):
    def _discord_path(self) -> Path:
        return self._settings().home / FILE

    async def screen_discord(self) -> None:
        path = self._discord_path()
        make_file(path)
        while True:
            config = load(path)
            self.title("DISCORD", "the bot: every feature is off until you set it up; /huragok setup in Discord does the same")
            self._discord_summary(config)
            choice = await self.choose("Which", [
                ("M", "Mode and bot token", "off, webhook only, or the full bot"),
                ("W", "Which Discord", "the one the bot works in"),
                ("R", "Ranks", "Discord roles onto Huragok's ranks; owners"),
                ("N", "Channels", "the channels features share"),
                ("F", "Features", "each one's state, and its switch"),
                ("C", "Check", "everything the features that are on need"),
                ("T", "Test post", "as each of the three who speak"),
                ("K", "Claim code", "whoever types it in Discord becomes an owner"),
                ("L", "The start loop's webhook", "posts when Huragok is down, since the bot can't then"),
                ("X", "Texts", "every text the bot posts, rewordable in [text]"),
                ("O", "Open discord.toml", "the file itself"),
            ], columns=1)
            if choice is None:
                return
            if choice == "O":
                self.note(desktop.open_editor(path))
                continue
            steps = {"M": self._discord_mode, "W": self._discord_which, "R": self._discord_ranks, "N": self._discord_channels,
                     "F": self._discord_features, "C": self._discord_check, "T": self._discord_test,
                     "K": self._discord_claim, "L": self._discord_launcher, "X": self._discord_texts}
            try:
                await steps[choice](path, config)
            except DiscordError as error:
                self.note(str(error), "red")
            except OSError as error:  # a refused edit: TomlEditError says what to do
                self.note(f"Couldn't change {FILE}: {error}", "red")

    def _discord_summary(self, config: DiscordConfig) -> None:
        state = read_state(self._settings().records)
        rows: list[tuple[str, str]] = [("mode", config.mode)]
        if config.error:
            self.note(config.error, "red")
        running = state.get("state", "") if not state.get("stale") else "not running (Huragok isn't either)"
        if config.mode == "bot":
            rows.append(("token", "set" if config.token else "none yet"))
            rows.append(("the bot", " · ".join(str(part) for part in (running, state.get("bot"), state.get("guild")) if part)))
            if state.get("why") and not state.get("stale"):
                self.note(str(state["why"]), "yellow")
        rows.append(("Discord", config.guild or "none picked"))
        ranks = config.section("ranks")
        rows.append(("owners", str(len(ranks.get("owners", [])))))
        on = [feature for feature in features.FEATURES if feature.is_on(config) and feature.stage <= features.BUILT]
        rows.append(("features on", str(len(on))))
        self.listing(rows, index=False)
        for line in config.warnings:
            self.note(line, "yellow")

    # Mode and token ---------------------------------------------------------------------------------------------

    async def _discord_mode(self, path: Path, config: DiscordConfig) -> None:
        self.heading("Discord", "Mode")
        picked = await self.choose("Mode", [
            ("1", "Off", "nothing goes to Discord"),
            ("2", "Webhook only", "no bot: posts only, each feature with its channel's webhook address"),
            ("3", "The full bot", "everything: your own bot, its token kept in discord.toml"),
        ], columns=1)
        if picked is None:
            return
        if picked == "1":
            set_mode(path, "off")
            self.note("The bot is off.")
        elif picked == "2":
            set_mode(path, "webhook")
            self.note("Webhook only. In Discord: a channel's settings, Integrations, Webhooks, New Webhook, Copy URL.")
        else:
            await self._discord_token(path, config)

    async def _discord_token(self, path: Path, config: DiscordConfig) -> None:
        self.heading("Discord", "Your bot")
        for line in BOT_STEPS:
            self.note(line)
        typed = await self.prompt_line("  The bot's token (Enter keeps the one set)" if config.token else "  The bot's token",
                                       secret=True)
        if typed is None:
            return
        token = typed.strip() or config.token
        if not token:
            self.note("No token: nothing changed.", "yellow")
            return
        user = await self.slow(rest.bot_user, token, note="Asking Discord…")
        application = await self.slow(rest.application, token, note="Asking Discord…")
        if typed.strip():
            set_token(path, token)
        set_mode(path, "bot")
        self.note(f"Signed in as {user.get('username')}: the bot connects within a few seconds.", "green")
        if application.get("bot_public"):
            self.note("Public Bot is on: anyone can add it to their Discord. Switch it off on the portal's Bot page.", "yellow")
        permissions = features.invite_permissions(load(path))
        self.note("To add it to your Discord, open this link:")
        self.terminal.print(f"  {rest.invite_url(str(application.get('id')), permissions, config.guild)}", "cyan")

    # Which Discord ----------------------------------------------------------------------------------------------

    def _need_token(self, config: DiscordConfig) -> bool:
        if config.token:
            return True
        self.note("There's no bot token yet: M first.", "yellow")
        return False

    async def _discord_which(self, path: Path, config: DiscordConfig) -> None:
        if not self._need_token(config):
            return
        guilds = await self.slow(rest.bot_guilds, config.token, note="Asking Discord…")
        self.heading("Discord", "Which Discord")
        if not guilds:
            application = await self.slow(rest.application, config.token, note="Asking Discord…")
            self.note("The bot isn't in any Discord yet. Add it with this link, then come back:")
            self.terminal.print(f"  {rest.invite_url(str(application.get('id')), features.invite_permissions(config))}", "cyan")
            return
        self.listing([(str(g.get("name")), str(g.get("id")), "← now" if str(g.get("id")) == config.guild else "")
                      for g in guilds])
        answer = await self.prompt_line("  Which number")
        picks = parse_picks(answer or "", len(guilds))
        if not picks or len(picks) != 1:
            return
        guild = guilds[picks[0] - 1]
        set_guild(path, str(guild.get("id")), str(guild.get("name")))
        self.note(f"The bot works in {guild.get('name')} now.", "green")

    async def _discord_facts(self, config: DiscordConfig) -> Facts | None:
        if not self._need_token(config):
            return None
        facts = await self.slow(gather, config.token, config.guild, note="Asking Discord…")
        if facts.guild is None:
            self.note("Pick the bot's Discord first (W), and make sure the bot is in it.", "yellow")
            return None
        return facts

    # Ranks ------------------------------------------------------------------------------------------------------

    async def _discord_ranks(self, path: Path, config: DiscordConfig) -> None:
        facts = await self._discord_facts(config)
        if facts is None:
            return
        roles = [role for role in sorted(facts.roles, key=lambda r: -int(r.get("position") or 0))
                 if str(role.get("id")) != str((facts.guild or {}).get("id")) and not role.get("managed")]
        known = {str(role.get("id")): str(role.get("name")) for role in facts.roles}
        self.heading("Discord", "Ranks", note="Nothing counts until it's here: a Discord's own admins aren't the servers' "
                                              "admins. An admin role means admin on every server.")
        self.listing([(f"@{role.get('name')}",) for role in roles])
        ranks = config.section("ranks")
        for rank, key in reversed(RANK_KEYS.items()):
            current = [facts.find_role(ref) for ref in ranks.get(key, [])]
            names = ", ".join(f"@{role.get('name')}" for role in current if role) or "none"
            answer = await self.prompt_line(f"  {rank}: now {names}. Numbers (Enter keeps, 0 for none)")
            if answer is None:
                return
            if not answer.strip():
                continue
            picks = [] if answer.strip() == "0" else parse_picks(answer, len(roles))
            if picks is None:
                self.note("Those aren't numbers on the list: left as it was.", "yellow")
                continue
            chosen = [(str(roles[n - 1].get("id")), str(roles[n - 1].get("name"))) for n in picks]
            set_rank_roles(path, key, chosen, guild_roles=known)
        await self._discord_owners(path)

    async def _discord_owners(self, path: Path) -> None:
        owners = [str(owner).strip() for owner in load(path).section("ranks").get("owners", [])]
        self.heading("Discord", "Owners", note="People, never a role: they can use /huragok. Easiest: K makes a code, and "
                                               "they type /huragok claim <code>.")
        self.listing([(owner,) for owner in owners] or [("nobody yet",)], index=False)
        answer = await self.prompt_line("  A Discord user ID to add or take off (Enter for neither)")
        if not answer or not answer.strip():
            return
        user = answer.strip()
        if not user.isdecimal():
            self.note("That isn't a user ID (a long number: Copy User ID in Discord, with Developer Mode on).", "yellow")
        elif user in owners:
            remove_owner(path, user)
            self.note(f"{user} isn't an owner now.", "green")
        else:
            add_owner(path, user)
            self.note(f"{user} is an owner now.", "green")

    # Channels ---------------------------------------------------------------------------------------------------

    async def _discord_channels(self, path: Path, config: DiscordConfig) -> None:
        facts = await self._discord_facts(config)
        if facts is None:
            return
        channels = facts.message_channels()
        self.heading("Discord", "Channels", note="What features share: a feature whose channel says \"staff\" posts wherever "
                                                 "staff is here. One with private things shouldn't be one @everyone can read.")
        self.listing([(f"#{c.get('name')}", facts.category_of(c), "@everyone reads" if facts.everyone_reads(c) else "private")
                      for c in channels])
        for key, reference in config.section("channels").items():
            current = facts.find_channel(reference)
            shown = f"#{current.get('name')}" if current else str(reference or "none")
            answer = await self.prompt_line(f"  {key}: now {shown}. A number (Enter keeps, 0 for none)")
            if answer is None:
                return
            if not answer.strip():
                continue
            if answer.strip() == "0":
                set_channel(path, key, "")
                continue
            picks = parse_picks(answer, len(channels))
            if not picks or len(picks) != 1:
                self.note("That isn't one number on the list: left as it was.", "yellow")
                continue
            channel = channels[picks[0] - 1]
            set_channel(path, key, str(channel.get("id")), str(channel.get("name")))

    # Features ---------------------------------------------------------------------------------------------------

    async def _discord_features(self, path: Path, config: DiscordConfig) -> None:
        facts = None
        if config.mode == "bot" and config.token and config.guild:
            facts = await self.slow(gather, config.token, config.guild, note="Asking Discord…")
        while True:
            shown = [feature for feature in features.FEATURES if feature.stage <= features.BUILT]
            coming = len(features.FEATURES) - len(shown)
            self.heading("Discord", "Features")
            self.listing([(f"{features.MARKS[state]} {feature.title}", why)
                          for feature in shown for state, why in [features.state_of(feature, config, facts)]])
            if coming:
                self.note(f"{coming} more arrive in later betas of 0.6.0.")
            answer = await self.prompt_line("  Numbers to switch (Enter or Esc: back)")
            picks = parse_picks(answer or "", len(shown))
            if not picks:
                return
            for number in picks:
                feature = shown[number - 1]
                set_switch(path, feature.table, feature.switch, features.switched(feature, config))
            config = load(path)

    # The check, a test, the rest --------------------------------------------------------------------------------

    async def _discord_check(self, path: Path, config: DiscordConfig) -> None:
        findings, _ = await self.slow(features.check, config, note="Asking Discord…")
        self.heading("Discord", "Check")
        for finding in findings:
            self.terminal.print(f"  {'✓' if finding.good else '!'} {finding.where}: {finding.text}",
                                "green" if finding.good else "yellow")

    async def _discord_test(self, path: Path, config: DiscordConfig) -> None:
        if config.mode == "webhook":
            await self._discord_test_webhook(config)
            return
        facts = await self._discord_facts(config)
        if facts is None:
            return
        channels = facts.message_channels()
        self.heading("Discord", "Test post")
        self.listing([(f"#{c.get('name')}", facts.category_of(c)) for c in channels])
        answer = await self.prompt_line("  Which channel")
        picks = parse_picks(answer or "", len(channels))
        if not picks or len(picks) != 1:
            return
        channel = str(channels[picks[0] - 1].get("id"))
        delivery = Delivery()
        for persona in PERSONAS:
            await self.slow(delivery.to_channel, config, channel, persona, message(say(config, f"test.{persona}")),
                            note=f"Posting as {persona}…")
        how = ("through the bot's webhook there" if delivery.webhooks.get(channel)
               else "as the bot itself (it can't keep a webhook there: Manage Webhooks)")
        self.note(f"Three posts sent, {how}.", "green")

    async def _discord_test_webhook(self, config: DiscordConfig) -> None:
        typed = await self.prompt_line("  A webhook's address to post to")
        if not typed or not typed.strip():
            return
        for persona in PERSONAS:
            payload = {**message(say(config, f"test.{persona}")), **speaker(config, persona)}
            await self.slow(rest.execute_webhook, typed.strip(), payload, note=f"Posting as {persona}…")
        self.note("Three posts sent.", "green")

    async def _discord_claim(self, path: Path, config: DiscordConfig) -> None:
        code = new_code(self._settings().records)
        self.heading("Discord", "Claim code")
        self.note("In Discord, whoever should be an owner types:")
        self.terminal.print(f"  /huragok claim {code}", "cyan")
        self.note(f"It works once, for {CLAIM_MINUTES} minutes. With no Discord picked yet, it also makes that one the bot's.")

    async def _discord_launcher(self, path: Path, config: DiscordConfig) -> None:
        self.heading("Discord", "The start loop's webhook", note="The bot is down when Huragok is, so the start loop posts "
                                                                 "\"Huragok stopped\" through a webhook.")
        typed = await self.prompt_line("  The webhook's address (Enter keeps, 0 for none)")
        if not typed or not typed.strip():
            return
        if typed.strip() == "0":
            set_launcher_webhook(path, "")
            self.note("No webhook now.", "green")
            return
        info = await self.slow(rest.webhook_info, typed.strip(), note="Asking Discord…")
        set_launcher_webhook(path, typed.strip())
        self.note(f"It posts through '{info.get('name')}' now.", "green")

    async def _discord_texts(self, path: Path, config: DiscordConfig) -> None:
        self.heading("Discord", "Texts", note="To reword one: [text] <key> = \"your words\" in discord.toml; {braces} are "
                                              "filled in.")
        lines: list[Any] = []
        for key, (lore_words, plain) in TEXTS.items():
            lines.append((key, "white"))
            lines.append(f"    {(plain if config.data['voice'] == 'plain' else lore_words)}")
        await self._pages(lines)
