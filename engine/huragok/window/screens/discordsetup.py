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
    FILE, RANK_KEYS, DiscordConfig, add_faculty, load, make_file, remove_faculty, set_channel, set_guild,
    set_launcher_webhook, set_message, set_mode, set_rank_roles, set_switch, set_token, set_webhook,
)
from ...discordbot.facts import Facts, gather
from ...discordbot.records import CLAIM_MINUTES, new_code, read_state
from ...discordbot.rest import DiscordError
from ...discordbot.speakers import PERSONAS, TEXTS, Delivery, message, say, speaker
from .base import Screens

PORTAL = "https://discord.com/developers/applications"
BOT_STEPS = (
    "The whole guide is docs/discord.md. In short:",
    f"1. Make the bot at {PORTAL}: New Application. It's your own bot, under your own account.",
    "2. On its Installation page: set Install Link to None, and save (Discord won't let it go private otherwise).",
    "3. On its Bot page: switch Public Bot off, so nobody else can add it to their Discord, and save.",
    "4. Also there: Reset Token, and copy it. It's a password: anyone who has it can act as your bot.",
    "5. Paste the token here. Huragok checks it, then gives you the link that adds the bot to your Discord.",
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
                ("R", "Ranks", "Discord roles onto Huragok's ranks; faculty"),
                ("N", "Channels", "the channels features share"),
                ("F", "Features", "each one's state, and its switch"),
                ("E", "Every message", "where each kind goes, its colour, on or off"),
                ("I", "Tickets", "what happens to a finished card, and who can close one"),
                ("Y", "Typed console", "where lines are typed, who can, how answers come back"),
                ("P", "Reports, appeals, Contact staff", "how people reach the staff"),
                ("C", "Check", "everything the features that are on need"),
                ("T", "Test post", "as each of the three who speak"),
                ("K", "Claim code", "whoever types it in Discord joins the faculty"),
                ("V", "Invite link", "to add the bot, or give it what's now switched on"),
                ("Z", "Start over", "forget this Discord and every card: the token stays"),
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
                     "F": self._discord_features, "E": self._discord_messages, "I": self._discord_tickets, "Y": self._discord_console, "P": self._discord_people, "C": self._discord_check, "T": self._discord_test,
                     "K": self._discord_claim, "L": self._discord_launcher, "X": self._discord_texts, "V": self._discord_invite, "Z": self._discord_start_over}
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
        rows.append(("faculty", str(len(ranks.get("faculty", [])))))
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
                                       secret=True, waits=True)
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
            self.note("Public Bot is on: anyone can add it to their Discord. On the portal: Installation, Install Link to None; "
                      "then Bot, Public Bot off.", "yellow")
        await self._discord_invite(path, load(path))
        self.note("Next: K gives a code, for /huragok claim in your Discord. /huragok setup takes it from there.")

    async def _discord_invite(self, path: Path, config: DiscordConfig) -> None:
        """Adding the bot, or giving it what's now switched on. Locked to a Discord only if the bot's in it: one it's left, or an old ID, is a link that can't work."""
        if not self._need_token(config):
            return
        application = await self.slow(rest.application, config.token, note="Asking Discord…")
        guilds = await self.slow(rest.bot_guilds, config.token, note="Asking Discord…")
        inside = config.guild if any(str(guild.get("id")) == config.guild for guild in guilds) else ""
        self.note("To add it to your Discord, open this link. If it's there already, open it anyway: don't remove the bot first, as Discord deletes its commands and webhooks with it.")
        self.terminal.print(f"  {rest.invite_url(str(application.get('id')), features.invite_permissions(config), inside)}", "cyan")

    async def _discord_start_over(self, path: Path, config: DiscordConfig) -> None:
        """A clean slate, for a Discord that's been wiped or a test from the top. The token stays, and so do players' notes: they're about the players, not this Discord."""
        self.heading("Discord", "Start over")
        self.note("This forgets the Discord, its faculty, channels, ranks and every switch, and every card, ticket, report and appeal the bot knows of. The token and what staff have written about players stay. discord.toml is backed up first.", "yellow")
        typed = await self.prompt_line("  Type yes to start over")
        if (typed or "").strip().lower() != "yes":
            self.note("Nothing changed.")
            return
        from ...discordbot.startover import start_over

        for line in start_over(path, self._settings().database):
            self.note(line)
        self.note("Next: M, 3 gives the link to add the bot, then K for the claim code.", "green")

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
        await self._discord_faculty(path)

    async def _discord_faculty(self, path: Path) -> None:
        faculty = [str(faculty).strip() for faculty in load(path).section("ranks").get("faculty", [])]
        self.heading("Discord", "Faculty", note="People, never a role: they can use /huragok. Easiest: K makes a code, and "
                                               "they type /huragok claim <code>.")
        self.listing([(faculty,) for faculty in faculty] or [("nobody yet",)], index=False)
        answer = await self.prompt_line("  A Discord user ID to add or take off (Enter for neither)")
        if not answer or not answer.strip():
            return
        user = answer.strip()
        if not user.isdecimal():
            self.note("That isn't a user ID (a long number: Copy User ID in Discord, with Developer Mode on).", "yellow")
        elif user in faculty:
            remove_faculty(path, user)
            self.note(f"{user} isn't faculty now.", "green")
        else:
            add_faculty(path, user)
            self.note(f"{user} is faculty now.", "green")

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
            self.note(f"{key}: {features.CHANNELS.get(key, '')}")
            answer = await self.prompt_line(f"  {key}: now {shown}. A number (Enter keeps, 0 unsets it)")
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
                self.note(f"{coming} more arrive in later versions.")
            answer = await self.prompt_line("  Numbers to switch (Enter or Esc: back)")
            picks = parse_picks(answer or "", len(shown))
            if not picks:
                return
            for number in picks:
                feature = shown[number - 1]
                turned = features.switched(feature, config)
                if config.mode == "webhook" and feature.channel and turned != feature.off_value:
                    typed = await self.prompt_line(f"  {feature.title}: its channel's webhook address (Enter to skip)", secret=True)
                    if not typed or not typed.strip():
                        continue
                    info = await self.slow(rest.webhook_info, typed.strip(), note="Asking Discord…")
                    set_webhook(path, feature.table, typed.strip())
                    self.note(f"It posts through '{info.get('name')}'.", "green")
                set_switch(path, feature.table, feature.switch, turned)
            config = load(path)

    # Every message ----------------------------------------------------------------------------------------------

    async def _discord_messages(self, path: Path, config: DiscordConfig) -> None:
        while True:
            messages = config.section("messages")
            keys = list(messages)
            self.heading("Discord", "Every message", note="/huragok gallery in Discord shows them all in their colours")
            self.listing([(key, str(messages[key].get("channel")), str(messages[key].get("colour")), "" if messages[key].get("post", True) else "off")
                          for key in keys])
            answer = await self.prompt_line("  Which number (Enter or Esc: back)")
            picks = parse_picks(answer or "", len(keys))
            if not picks or len(picks) != 1:
                return
            await self._discord_one_message(path, config, keys[picks[0] - 1])
            config = load(path)

    async def _discord_one_message(self, path: Path, config: DiscordConfig, key: str) -> None:
        line = config.section("messages")[key]
        choice = await self.choose(key, [
            ("C", "Its channel", str(line.get("channel"))),
            ("L", "Its colour", str(line.get("colour"))),
            ("G", "When it's finished", str(line.get("closes_to") or "as Tickets says")),
            ("P", "Switch it off" if line.get("post", True) else "Switch it on", ""),
        ], columns=1)
        if choice == "P":
            set_message(path, key, post=not line.get("post", True))
        elif choice == "C":
            names = list(config.section("channels"))
            self.listing([(name, str(config.section("channels")[name]) or "not set") for name in names])
            answer = await self.prompt_line("  A number, or a channel's name (#my-channel)")
            if answer and answer.strip():
                picks = parse_picks(answer, len(names)) if answer.strip()[0].isdigit() else None
                set_message(path, key, channel=names[picks[0] - 1] if picks else answer.strip())
        elif choice == "G":
            answer = await self.prompt_line("  logs, huragok-logs, #its-own-channel, stay or thread (0: as Tickets says)")
            if answer and answer.strip():
                set_message(path, key, closes_to="" if answer.strip() == "0" else answer.strip())
        elif choice == "L":
            names = list(config.data["colours"])
            self.listing([(name, str(config.data["colours"][name])) for name in names])
            answer = await self.prompt_line("  A number, or #RRGGBB")
            if answer and answer.strip():
                picks = parse_picks(answer, len(names)) if answer.strip()[0].isdigit() else None
                set_message(path, key, colour=names[picks[0] - 1] if picks else answer.strip())

    # Tickets ----------------------------------------------------------------------------------------------------

    async def _discord_tickets(self, path: Path, config: DiscordConfig) -> None:
        while True:
            options = config.section("tickets")
            choice = await self.choose("Tickets", [
                ("W", "When a card's finished", f"now: {options.get('when_closed')}. move: to a log channel; stay: where it is; thread: where it is, with a thread of its own"),
                ("S", "Untouched, Huragok's are finished after", f"now: {options.get('stale_after_days')} days (0: never)"),
                ("R", "Who can acknowledge or close", f"now: {options.get('close_rank')} and above"),
            ], columns=1)
            if choice is None:
                return
            if choice == "W":
                answer = await self.prompt_line("  move, stay or thread")
                if answer and answer.strip().lower() in ("move", "stay", "thread"):
                    set_switch(path, ("tickets",), "when_closed", answer.strip().lower())
            elif choice == "S":
                answer = await self.prompt_line("  How many days (0: never)")
                if answer and answer.strip().isdecimal():
                    set_switch(path, ("tickets",), "stale_after_days", int(answer.strip()))
            elif choice == "R":
                answer = await self.prompt_line("  moderator, admin or faculty")
                if answer and answer.strip().lower() in ("moderator", "admin", "faculty", "faculty"):
                    set_switch(path, ("tickets",), "close_rank", answer.strip().lower())
            config = load(path)

    # The typed console ------------------------------------------------------------------------------------------

    async def _discord_console(self, path: Path, config: DiscordConfig) -> None:
        while True:
            options = config.section("console")
            choice = await self.choose("Typed console", [
                ("T", "Switched on", f"now: {'yes' if options.get('typed') else 'no'}. It needs Message Content Intent, on the portal's Bot page"),
                ("L", "Where lines are typed", f"now: {options.get('layout')}. picker: one channel, pick the server; per-server: a channel each"),
                ("S", "A channel for a server", f"now: {len(options.get('channels') or {})} set. For the per-server layout"),
                ("A", "Who can type console lines", f"now: {options.get('raw_rank')}"),
                ("B", "How answers come back", f"now: {options.get('answers')}, long ones as a {options.get('long_answers')}"),
                ("F", "What asks first", "now: " + (", ".join(str(word) for word in options.get("confirm") or []) or "nothing")),
                ("G", "Lines from people without a rank", f"now: {options.get('ignore_others')}"),
                ("C", "When it clears itself", f"now: after {options.get('clear_after_minutes')} quiet minutes (0: never). It needs Manage Messages there"),
                ("K", "Keep what was said", f"now: {'yes, in the logs as a file' if options.get('transcript', True) else 'no'}"),
            ], columns=1)
            if choice is None:
                return
            if choice == "T":
                set_switch(path, ("console",), "typed", not options.get("typed"))
            elif choice == "L":
                answer = await self.prompt_line("  picker or per-server")
                if answer and answer.strip().lower() in ("picker", "per-server"):
                    set_switch(path, ("console",), "layout", answer.strip().lower())
            elif choice == "S":
                server = await self.prompt_line("  Which server (its name)")
                channel = await self.prompt_line("  Its channel: an ID or #name (0: none)") if server and server.strip() else None
                if channel:
                    have = {key: value for key, value in dict(options.get("channels") or {}).items() if key != server.strip()}
                    set_switch(path, ("console",), "channels", have if channel.strip() == "0" else {**have, server.strip(): channel.strip()})
            elif choice == "A":
                answer = await self.prompt_line("  moderator, admin, faculty or off")
                if answer and answer.strip().lower() in ("moderator", "admin", "faculty", "faculty", "off"):
                    set_switch(path, ("console",), "raw_rank", answer.strip().lower())
            elif choice == "B":
                answer = await self.prompt_line("  reply or thread, then file or split: reply file")
                words = answer.strip().lower().split() if answer else []
                if len(words) == 2 and words[0] in ("reply", "thread") and words[1] in ("file", "split"):
                    set_switch(path, ("console",), "answers", words[0])
                    set_switch(path, ("console",), "long_answers", words[1])
            elif choice == "F":
                answer = await self.prompt_line("  Commands, with spaces between (0: nothing asks first)")
                if answer:
                    set_switch(path, ("console",), "confirm", [] if answer.strip() == "0" else [word.lstrip("!").lower() for word in answer.replace(",", " ").split()])
            elif choice == "G":
                answer = await self.prompt_line("  ignore or delete")
                if answer and answer.strip().lower() in ("ignore", "delete"):
                    set_switch(path, ("console",), "ignore_others", answer.strip().lower())
            elif choice == "C":
                answer = await self.prompt_line("  Quiet minutes before it clears (at least 5; 0: never)")
                if answer and answer.strip().isdecimal():
                    set_switch(path, ("console",), "clear_after_minutes", int(answer.strip()))
            elif choice == "K":
                set_switch(path, ("console",), "transcript", not options.get("transcript", True))
            config = load(path)

    # How people reach the staff ---------------------------------------------------------------------------------

    async def _discord_people(self, path: Path, config: DiscordConfig) -> None:
        settings = [("A", "reports", "from", "Reports come from (game, discord)"), ("B", "reports", "threshold", "Ping staff when this many people report someone (0: never)"), ("C", "reports", "threshold_minutes", "...within this many minutes"),
                    ("D", "reports", "reporter_cooldown_seconds", "One person can report once in this many seconds"), ("E", "appeals", "for", "What can be appealed (ban, kick, mute)"), ("F", "appeals", "cooldown_days", "Days before they can appeal again"),
                    ("G", "appeals", "questions", "The questions, separated by |"), ("H", "appeals", "tell_outcome", "Telling them how it went (dm, no)"), ("I", "appeals", "in_reason", "Added to a ban's message: {code} finds them"),
                    ("K", "contact", "channel", "Contact staff's button goes in (a [channels] name, an ID or #name)"), ("L", "contact", "categories", "The categories, separated by |"), ("M", "contact", "thread_role", "The role brought into each thread"),
                    ("N", "contact", "close_after_days", "Days with nothing said before a thread closes (0: never)")]
        while True:
            choice = await self.choose("Reports, appeals, Contact staff", [(key, title, f"now: {config.section(section).get(name)}") for key, section, name, title in settings], columns=1)
            if choice is None:
                return
            _, section, name, title = next(one for one in settings if one[0] == choice)
            answer = await self.prompt_line(f"  {title}")
            if answer is not None and answer.strip():
                now = config.section(section).get(name)
                typed = answer.strip()
                value: Any = typed
                if isinstance(now, bool):
                    value = typed.lower() in ("yes", "true", "on")
                elif isinstance(now, int):
                    value = int(typed) if typed.isdecimal() else now
                elif isinstance(now, list):
                    value = [one.strip() for one in typed.replace(",", "|").split("|") if one.strip()] if name != "questions" else [one.strip() for one in typed.split("|") if one.strip()]
                set_switch(path, (section,), name, value)
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
        typed = await self.prompt_line("  A webhook's address to post to", secret=True)
        if not typed or not typed.strip():
            return
        for persona in PERSONAS:
            payload = {**message(say(config, f"test.{persona}")), **speaker(config, persona)}
            await self.slow(rest.execute_webhook, typed.strip(), payload, note=f"Posting as {persona}…")
        self.note("Three posts sent.", "green")

    async def _discord_claim(self, path: Path, config: DiscordConfig) -> None:
        code = new_code(self._settings().records)
        self.heading("Discord", "Claim code")
        self.note("In Discord, whoever should be faculty types:")
        self.terminal.print(f"  /huragok claim {code}", "cyan")
        self.note(f"It works once, for {CLAIM_MINUTES} minutes. With no Discord picked yet, it also makes that one the bot's.")

    async def _discord_launcher(self, path: Path, config: DiscordConfig) -> None:
        self.heading("Discord", "The start loop's webhook", note="The bot is down when Huragok is, so the start loop posts "
                                                                 "\"Huragok stopped\" through a webhook.")
        typed = await self.prompt_line("  The webhook's address (Enter keeps, 0 for none)", secret=True)
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
        self.heading("Discord", "Texts", note="To reword one: \"<key>\" = \"your words\" under [text] in discord.toml; {braces} are "
                                              "filled in.")
        lines: list[Any] = []
        for key, (lore_words, plain) in TEXTS.items():
            lines.append((key, "white"))
            lines.append(f"    {(plain if config.data['voice'] == 'plain' else lore_words)}")
        await self._pages(lines)
