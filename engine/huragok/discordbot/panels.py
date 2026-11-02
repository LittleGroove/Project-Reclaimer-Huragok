"""/huragok setup and /huragok features: the same steps as the window's J, with Discord's own pickers. Every panel is
private to the owner using it, and each change is written to discord.toml straight away (the part reloads it), so the
window, the file and Discord always agree."""

from __future__ import annotations

import json
import asyncio
import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

from . import cards, contact, features, kept, rest, tidy
from .client import NOT_OWNER, SaysWhatWentWrong, acting_as, answer, someone_from
from .config import add_owner, colour_value, colour_value_of, LOG, ping_value, pings_of, RANK_KEYS, remove_owner, set_channel, set_message, set_rank_roles, set_switch, TICKET
from .features import FEATURES, MARKS
from .ranks import rank_of
from .speakers import say
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

TIMEOUT = 600.0
FEATURES_PER_PAGE = 20  # a menu holds 25
SERVERS_PER_PAGE = 4
CHANNELS_PER_PAGE = 4  # a panel holds 5 rows; the last is for the buttons
RANK_TITLES = {"admin": "Admins (every server)", "moderator": "Moderators", "probationary-moderator": "Probationary moderators",
               "member": "Members"}
CHANNEL_KINDS = [discord.ChannelType.text, discord.ChannelType.news]


def _ids(values: Any) -> list[int]:
    return [int(value) for value in values if str(value).strip().isdecimal()]


def _defaults(values: Any, kind: discord.SelectDefaultValueType) -> list[discord.SelectDefaultValue]:
    return [discord.SelectDefaultValue(id=value, type=kind) for value in _ids(values)][:25]


class Panel(SaysWhatWentWrong, ui.View):
    """One screen of the setup: a card, and its controls."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(timeout=TIMEOUT)
        self.part = part
        self.shown_through: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Asked at every press: a panel left open outlives the owner who opened it, and each press makes it new again."""
        if rank_of(self.part.file, someone_from(interaction.user)) == "owner":
            self.shown_through = interaction  # the token it's taken down with lasts 15 minutes from here
            return True
        await answer(interaction, NOT_OWNER)
        return False

    def card(self) -> discord.Embed:
        raise NotImplementedError

    async def open(self, interaction: discord.Interaction) -> None:
        """As a new private message, or in place of the panel it was reached from."""
        self.shown_through = interaction
        self.part.panel_open[interaction.user.id] = self
        if interaction.response.is_done():  # it took a moment, so it was held open first
            await interaction.edit_original_response(embed=self.card(), view=self)
        elif interaction.type is discord.InteractionType.component:
            await interaction.response.edit_message(embed=self.card(), view=self)
        else:
            await interaction.response.send_message(embed=self.card(), view=self, ephemeral=True)

    async def on_timeout(self) -> None:
        """Nothing on it works any more, so it goes. Not if another's taken its place: replaced ones time out too, and this would delete that."""
        through = self.shown_through
        if through is not None and self.part.panel_open.get(through.user.id) is self:
            del self.part.panel_open[through.user.id]
            try:
                await through.delete_original_response()
            except discord.HTTPException:
                pass

    def nav(self, row: int = 4) -> None:
        back = ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=row)

        async def go_back(interaction: discord.Interaction) -> None:
            await Home(self.part).open(interaction)

        back.callback = go_back  # type: ignore[method-assign]
        self.add_item(back)


class Home(Panel):
    """What's left to do, in order, and everything else a press away: nobody should have to work out what comes next."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        self.steps = features.steps(part.file, part.facts)
        screens = {"features": Features, "channels": Channels, "ranks": Ranks, "told": WhoGetsTold}
        upcoming = next((step for step in self.steps if not step.done), None)
        if upcoming is not None:
            self.add_item(_Open(f"Next: {upcoming.label}", screens[upcoming.screen], part, row=0, style=discord.ButtonStyle.success))
        if not self.steps[0].done:
            self.add_item(_Recommended(part))
        self.add_item(_Check(part))
        if self.part.facts is not None and part.file.section("channels") and any(str(value).strip() for value in part.file.section("channels").values()):
            self.add_item(_Hello(part))
        link = _invite_link(part)
        if link and features.missing_permissions(part.file, part.facts):
            self.add_item(ui.Button(label="Invite link", url=link, row=0))
        for number, (label, screen) in enumerate((("Features", Features), ("Channels", Channels), ("Ranks", Ranks), ("Owners", Owners), ("Who gets told", WhoGetsTold), ("Messages", Messages), ("Tickets", Tickets), ("Commands", CommandsPanel),
                                                  ("Console", Console), ("Reports", ReportsPanel), ("Appeals", AppealsPanel), ("Contact", ContactPanel), ("Status", StatusPanel), ("Notes", NotesPanel))):
            self.add_item(_Open(label, screen, part, row=1 + number // 5))

    def card(self) -> discord.Embed:
        lines = [("✅ " if step.done else "⬜ ") + step.text for step in self.steps]
        return discord.Embed(title="Huragok's bot", description="\n".join([*lines, "", "Each change takes effect straight away."])[:4000], colour=discord.Colour.blurple())


def _invite_link(part: DiscordPart) -> str:
    client = part.client
    application = getattr(client, "application_id", None) if client is not None else None
    return rest.invite_url(str(application), features.invite_permissions(part.file), part.file.guild if part.facts is not None and part.facts.guild is not None else "") if application else ""


class _Open(ui.Button[Panel]):
    def __init__(self, label: str, screen: type[Panel], part: DiscordPart, *, row: int | None = None, style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(label=label, style=style, row=row)
        self.screen, self.part = screen, part

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.screen(self.part).open(interaction)


class _Recommended(ui.Button[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(label="Recommended", style=discord.ButtonStyle.primary, row=0)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        for key in features.RECOMMENDED:
            feature = features.BY_KEY[key]
            if not feature.is_on(self.part.file):
                set_switch(self.part.path, feature.table, feature.switch, feature.on_value)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what="the recommended features are on"))
        await Home(self.part).open(interaction)


class _Hello(ui.Button[Panel]):
    """Proof that each channel works, which also shows what each is for. It's gone again in a few minutes."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(label="Say hello in every channel", style=discord.ButtonStyle.secondary, row=0)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        part, lines = self.part, []
        lasting = tidy.minutes_for(part, "tests") * 60
        for name, reference in part.file.section("channels").items():
            if not str(reference).strip():
                continue
            channel = part.facts.find_channel(reference) if part.facts is not None else None
            if channel is None:
                lines.append(f"⚠️ **{name}**: the bot can't see that channel. Add its role in the channel's settings: Permissions")
                continue
            try:
                reply = await to_thread(rest.post_message, part.file.token, str(channel.get("id")), {"content": f"This is **{name}**: {features.CHANNELS.get(name, '')}.", "allowed_mentions": {"parse": []}})
                new = str(reply.data.get("id") or "") if isinstance(reply.data, dict) else ""
                if lasting and new:
                    tidy.later(part, str(channel.get("id")), new, lasting, "saying hello")
                lines.append(f"✅ **{name}**: <#{channel.get('id')}>" + (" (⚠️ @everyone can read it)" if name in features.PRIVATE_CHANNELS and part.facts.everyone_reads(channel) else ""))
            except rest.DiscordError as error:
                lines.append(f"⚠️ **{name}**: <#{channel.get('id')}> refused it: {error}")
        await answer(interaction, "\n".join(lines)[:1900] or "No channels are set yet.", lasts="looks")


class _Check(ui.Button[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(label="Check", style=discord.ButtonStyle.secondary, row=0)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        from .client import findings_cards

        await interaction.response.defer(ephemeral=True, thinking=True)
        findings, facts = await to_thread(features.check, self.part.file)
        self.part.facts = facts or self.part.facts  # what it just read: Features says the same now, rather than in five minutes
        left = [step for step in features.steps(self.part.file, self.part.facts) if not step.done]
        await answer(interaction, embeds=findings_cards(findings, left), lasts="looks")


# Ranks ---------------------------------------------------------------------------------------------------------


class _RankSelect(ui.RoleSelect[Panel]):
    def __init__(self, part: DiscordPart, rank: str, key: str) -> None:
        super().__init__(placeholder=RANK_TITLES[rank], min_values=0, max_values=25,
                         default_values=_defaults(part.file.section("ranks").get(key, []),
                                                  discord.SelectDefaultValueType.role))
        self.part, self.rank, self.key = part, rank, key

    async def callback(self, interaction: discord.Interaction) -> None:
        wanted = [(str(role.id), role.name) for role in self.values]
        known = {str(role.id): role.name for role in interaction.guild.roles} if interaction.guild else {}
        set_rank_roles(self.part.path, self.key, wanted, guild_roles=known)
        log.info("%s set the %s roles in Discord: %s", interaction.user.name, self.rank,
                 ", ".join(name for _, name in wanted) or "none")
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"the {self.rank} roles are {', '.join(name for _, name in wanted) or 'none'}"))
        await Ranks(self.part).open(interaction)


class Ranks(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        for rank, key in reversed(RANK_KEYS.items()):
            self.add_item(_RankSelect(part, rank, key))
        self.nav()

    def card(self) -> discord.Embed:
        text = ("Which Discord roles count as which of Huragok's ranks. Nothing counts until it's here: a Discord's own "
                "admins aren't the servers' admins. What each rank can do is `commands.toml`'s job, in game and here "
                "alike. An admin role means admin on every server.")
        return discord.Embed(title="Ranks", description=text, colour=discord.Colour.blurple())


class _OwnerSelect(ui.UserSelect[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(placeholder="Owners", min_values=0, max_values=25,
                         default_values=_defaults(part.file.section("ranks").get("owners", []),
                                                  discord.SelectDefaultValueType.user))
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        wanted = {str(user.id): user.name for user in self.values}
        current = [str(owner).strip() for owner in self.part.file.section("ranks").get("owners", [])]
        if str(interaction.user.id) not in wanted:
            await interaction.response.send_message("You can't take yourself off here: another owner can, or the host in "
                                                    "discord.toml.", ephemeral=True)
            return
        for owner in current:
            if owner not in wanted:
                remove_owner(self.part.path, owner)
        for owner, name in wanted.items():
            if owner not in current:
                add_owner(self.part.path, owner, name)
        log.info("%s changed the bot's owners in Discord", interaction.user.name)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what="changed the bot's owners"))
        await Owners(self.part).open(interaction)


class Owners(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        self.add_item(_OwnerSelect(part))
        self.add_item(_SettingChoice(part, "ranks", "owners_in_game", "What they can do from Discord", [("From Discord they act as owners: any command, on anyone", "owner"), ("From Discord they act as admins", "admin")], part.file.section("ranks").get("owners_in_game") or "owner", Owners))
        self.nav()

    def card(self) -> discord.Embed:
        text = ("Owners can use `/huragok` and everything it changes. They're people, never a role: anyone with Manage Roles can hand out a role, but only an owner or the host can name an owner.\n\n"
                f"**From Discord they act as:** {self.part.file.section('ranks').get('owners_in_game') or 'owner'}, on every server. It gives them no rank in the game itself: there, commands.toml decides.")
        return discord.Embed(title="Owners", description=text, colour=discord.Colour.blurple())


# Channels ------------------------------------------------------------------------------------------------------


CHANNEL_ORDER = ("staff", "logs", "reports", "appeals", "alerts", "huragok-logs", "console", "status", "info", "games")  # most needed first; those for what isn't built yet stay out


class _ChannelSelect(ui.ChannelSelect[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(placeholder=f"{key}", channel_types=CHANNEL_KINDS, min_values=0, max_values=1,
                         default_values=_defaults([part.file.section("channels").get(key, "")],
                                                  discord.SelectDefaultValueType.channel))
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0] if self.values else None
        await interaction.response.defer()  # it's read again, so the answer's about now
        set_channel(self.part.path, self.key, str(picked.id) if picked else "", picked.name if picked else "")
        log.info("%s set the %s channel in Discord: %s", interaction.user.name, self.key, picked.name if picked else "none")
        await self.part.look_now()
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"the {self.key} channel is {picked.name if picked else 'none'}"))
        await Channels(self.part, self.page, said=_verdict(self.part, self.key, str(picked.id)) if picked else "").open(interaction)


def _verdict(part: DiscordPart, key: str, channel_id: str) -> str:
    """Whether what was just picked will work, and if not what to do: it's why they picked it."""
    facts = part.facts
    channel = facts.find_channel(channel_id) if facts is not None else None
    if channel is None:
        return f"⚠️ The bot can't see that channel. Add its role in the channel's settings: Permissions, then add a role or member."
    missing = [name for name in features.POSTS if not facts.has(name, channel)]
    if missing:
        return f"⚠️ The bot can't post in <#{channel_id}>: it needs {', '.join(features.PERMISSION_NAMES[name] for name in missing)} there."
    if key in features.PRIVATE_CHANNELS and facts.everyone_reads(channel) and not part.file.section("feeds").get("public"):
        return f"⚠️ @everyone can read <#{channel_id}>, so private things won't be posted there. Make it private, or allow it below."
    return f"✅ The bot can post in <#{channel_id}>."


class Channels(Panel):
    def __init__(self, part: DiscordPart, page: int = 0, *, said: str = "") -> None:
        super().__init__(part)
        self.page, self.said = page, said
        keys = [key for key in CHANNEL_ORDER if key in part.file.section("channels")] + [key for key, value in part.file.section("channels").items() if key not in CHANNEL_ORDER and str(value).strip()]  # what's not built yet stays out, unless it's been set
        self.pages = max(1, (len(keys) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
        for key in keys[page * CHANNELS_PER_PAGE:(page + 1) * CHANNELS_PER_PAGE]:
            self.add_item(_ChannelSelect(part, key, page))
        self.keys = keys[page * CHANNELS_PER_PAGE:(page + 1) * CHANNELS_PER_PAGE]
        self.nav()
        if page > 0:
            self.add_item(_ChannelPage(part, page - 1, "Previous"))
        if page + 1 < self.pages:
            self.add_item(_ChannelPage(part, page + 1, "Next"))
        if any(str(value).strip() for value in part.file.section("channels").values()):
            self.add_item(_UnsetButton(part, page))
        self.add_item(_SettingSwitch(part, "feeds", "public", "Allow private things where everyone can read", Channels))

    def card(self) -> discord.Embed:
        menus = "\n".join(f"{number}. **{key}**: {features.CHANNELS.get(key, '')}" for number, key in enumerate(self.keys, 1))
        text = (f"{self.said + chr(10) + chr(10) if self.said else ''}These menus, from the top:\n{menus}\n\nA small Discord only needs staff and logs: reports and alerts fall back on staff, appeals on reports, and huragok-logs on logs. The channel decides how a message looks: a ticket, a record or a post. A channel with private "
                "things (tickets, logs, the console) should be one @everyone can't read, and nobody should be able to type in status, info or games.")
        return discord.Embed(title=f"Channels ({self.page + 1} of {self.pages})", description=text,
                             colour=discord.Colour.blurple())


class _UnsetButton(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int) -> None:
        super().__init__(label="Unset one", style=discord.ButtonStyle.secondary, row=4)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await UnsetChannel(self.part, self.page).open(interaction)


class UnsetChannel(Panel):
    """Taking a channel off a name. A menu of the ones that are set, so it can't be missed."""

    def __init__(self, part: DiscordPart, page: int) -> None:
        super().__init__(part)
        self.page = page
        self.add_item(_UnsetSelect(part, page))
        back = ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=4)

        async def go_back(interaction: discord.Interaction) -> None:
            await Channels(part, page).open(interaction)

        back.callback = go_back  # type: ignore[method-assign]
        self.add_item(back)

    def card(self) -> discord.Embed:
        return discord.Embed(title="Unset a channel", description="Pick one, and it's no longer used for that. What goes there waits, or uses the channel it falls back on.", colour=discord.Colour.blurple())


class _UnsetSelect(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, page: int) -> None:
        options = [discord.SelectOption(label=key, description=features.CHANNELS.get(key, "")[:100] or None) for key, value in part.file.section("channels").items() if str(value).strip()]
        super().__init__(placeholder="Which one?", options=options[:25] or [discord.SelectOption(label="None are set")], disabled=not options)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        key = self.values[0]
        set_channel(self.part.path, key, "")
        log.info("%s unset the %s channel in Discord", interaction.user.name, key)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"the {key} channel is unset"))
        await Channels(self.part, self.page).open(interaction)


class _ChannelPage(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await Channels(self.part, self.page).open(interaction)


# Tickets -------------------------------------------------------------------------------------------------------


WHEN_CLOSED = {"move": "When finished: move to its log channel", "stay": "When finished: stay where it is", "thread": "When finished: stay, with a thread of its own"}
STALE_DAYS = (0, 1, 2, 3, 7, 14, 30)
CLOSE_RANKS = ("moderator", "admin", "owner")


class Tickets(Panel):
    """How tickets behave, for every kind of card. Messages has what's different for one kind."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("tickets")
        self.add_item(_TicketChoice(part, "when_closed", "When a card's finished…", [(label, value) for value, label in WHEN_CLOSED.items()], str(options.get("when_closed"))))
        self.add_item(_TicketChoice(part, "stale_after_days", "Untouched, Huragok's are finished after…", [("Untouched automatic tickets stay open" if days == 0 else f"Untouched automatic tickets finish after {days} day{'s' if days != 1 else ''}", str(days)) for days in STALE_DAYS], str(options.get("stale_after_days"))))
        self.add_item(_TicketChoice(part, "close_rank", "Who can acknowledge or close…", [(f"Acknowledge or close: {rank}s and above" if rank != "owner" else "Acknowledge or close: owners", rank) for rank in CLOSE_RANKS], str(options.get("close_rank"))))
        self.nav()

    def card(self) -> discord.Embed:
        text = ("**Moving** sends a finished card to its log channel: `logs` for what people did or asked, `huragok-logs` for Vergil's and the Superintendent's. **Staying** leaves it where it is, in the closed "
                "colour. **A thread of its own** gives each card a thread from the start, which closing locks and hides (the bot needs Manage Threads). Acting on a finished card brings it back in every case.\n\n"
                "A log channel can be shared, by giving two names the same channel in Channels, or a kind of card can have its own, in Messages.")
        return discord.Embed(title="Tickets", description=text, colour=discord.Colour.blurple())


class _TicketChoice(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, key: str, placeholder: str, choices: list[tuple[str, str]], current: str) -> None:
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=[discord.SelectOption(label=label, value=value, default=value == current) for label, value in choices])
        self.part, self.key = part, key

    async def callback(self, interaction: discord.Interaction) -> None:
        value: Any = int(self.values[0]) if self.key == "stale_after_days" else self.values[0]
        set_switch(self.part.path, ("tickets",), self.key, value)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"tickets: {self.key} is {value}"))
        await Tickets(self.part).open(interaction)


# The typed console -----------------------------------------------------------------------------------------------


CONSOLE_LAYOUTS = {"picker": "One channel, and pick the server", "per-server": "A channel for each server"}
CONSOLE_LINES = {"owner": "Console lines: owners only", "admin": "Console lines: admins and owners", "moderator": "Console lines: moderators too", "off": "Console lines: nobody, only ! commands"}
CONSOLE_ANSWERS = {"reply+file": "Answers as a reply, a long one as a file", "reply+split": "Answers as a reply, a long one in pieces", "thread+file": "Answers in a thread, a long one as a file", "thread+split": "Answers in a thread, a long one in pieces"}
CONSOLE_OTHERS = {"ignore": "Lines from people without a rank: ignored", "delete": "Lines from people without a rank: deleted"}


class Console(Panel):
    """How typed lines work. The switch for it is in Features, as it needs a switch of Discord's too."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("console")
        layout = str(options.get("layout") or "picker")
        lines = str(options.get("raw_rank") or "admin")
        self.add_item(_ConsoleChoice(part, "layout", CONSOLE_LAYOUTS, layout))
        self.add_item(_ConsoleChoice(part, "lines", CONSOLE_LINES, lines))
        self.add_item(_ConsoleChoice(part, "answers", CONSOLE_ANSWERS, f"{options.get('answers') or 'reply'}+{options.get('long_answers') or 'file'}"))
        self.add_item(_ConsoleChoice(part, "ignore_others", CONSOLE_OTHERS, str(options.get("ignore_others") or "ignore")))
        self.nav()
        if layout == "per-server":
            server_channels = _Open("A channel for each server", ServerChannels, part)
            server_channels.row = 4
            self.add_item(server_channels)
        else:
            self.add_item(_ConsoleRemember(part))
        self.add_item(_ConsoleAsksFirst(part))
        self.add_item(_Open("Clearing and more", ConsoleMore, part))
        self.add_item(_FeatureSwitch(part, "console", Console))

    def card(self) -> discord.Embed:
        options = self.part.file.section("console")
        state = features.state_of(features.BY_KEY["console"], self.part.file, self.part.facts)
        text = (f"**Now:** {state[0]}{f' ({state[1]})' if state[1] else ''}.\n\nA line starting with `{self.part.ctx.command_prefix}` is one of Huragok's commands, as your rank allows in game. Anything else goes to the server's console, if your rank may use it. People without a rank aren't heard.\n\n"
                f"**Clearing:** {f'after {consoleclear_minutes(self.part)} quiet minutes' if consoleclear_minutes(self.part) else 'never'}. Clearing and more says why, and changes it.\n\n"
                f"**These ask first:** {', '.join(str(word) for word in options.get('confirm') or []) or 'nothing'}")
        return discord.Embed(title="Typed console", description=text, colour=discord.Colour.blurple())


CLEAR_AFTER = [("Clear after 5 quiet minutes", 5), ("Clear after 10 quiet minutes", 10), ("Clear after 30 quiet minutes", 30), ("Clear after an hour's quiet", 60), ("Clear after 6 quiet hours", 360), ("Clear it once a day at most", 1440), ("Never clear it", 0)]


def consoleclear_minutes(part: DiscordPart) -> str:
    from . import consoleclear

    minutes = consoleclear.after_minutes(part.file)
    return f"{minutes:g}" if minutes else ""


class ConsoleMore(Panel):
    """How the console keeps itself clear, and the rest that didn't fit on its panel."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("console")
        self.add_item(_SettingChoice(part, "console", "clear_after_minutes", "When it's cleared", CLEAR_AFTER, options.get("clear_after_minutes", 10), ConsoleMore))
        for row, (key, label) in enumerate((("transcript", "Keep what was said in the logs"), ("forget_server", "Clearing forgets each person's server"))):
            switch = _SettingSwitch(part, "console", key, label, ConsoleMore)
            switch.row = row + 1
            self.add_item(switch)
        switch = _SettingSwitch(part, "console", "server_prefix", "\"server1: kick 3\" names the server", ConsoleMore)
        switch.row = 3
        self.add_item(switch)
        self.add_item(_Open("Back", Console, part, row=4))
        self.add_item(_SettingSwitch(part, "console", "public", "Allow where everyone can read", ConsoleMore))

    def card(self) -> discord.Embed:
        config, facts = self.part.file, self.part.facts
        from . import consoleclear

        minutes = consoleclear.after_minutes(config)
        stopped = consoleclear.problems(config, facts) if facts is not None else []
        text = ("**Clearing:** once nothing's been said or pressed for that long, what was there goes to the logs as a file and the channel's emptied, all but the menu and anything pinned. "
                "It's ready to use again, and nothing's deleted unless the file got there.\n\n"
                + ("**Now:** it's never cleared." if not minutes else "**Now:** " + " ".join(stopped) if stopped else f"**Now:** after {minutes:g} quiet minutes."))
        return discord.Embed(title="Console: clearing and more", description=text, colour=discord.Colour.blurple())


MOST_PINGED = 25  # Discord's most in a menu


def _roles_pinged(part: DiscordPart, value: Any) -> list[dict[str, Any]]:
    """The roles this pings that are still there."""
    return [role for ping in pings_of(value) if part.facts is not None and (role := part.facts.find_role(ping))]


def _picked_as_roles(roles: list[dict[str, Any]]) -> list[discord.SelectDefaultValue]:
    return [discord.SelectDefaultValue(id=int(role["id"]), type=discord.SelectDefaultValueType.role) for role in roles]


def _pings_in_words(part: DiscordPart, value: Any) -> str:
    """Who it pings, as a card says it: a mention for each, or how the file says it if it's not found."""
    found = [f"<@&{ping}>" if ping.isdecimal() else f"<@&{role['id']}>" if (role := part.facts.find_role(ping) if part.facts is not None else None) else ping for ping in pings_of(value)]
    return ", ".join(found) or "nobody"


def _audit_words(roles: list[Any]) -> str:
    return "pings " + ", ".join(f"@{role.name}" for role in roles) if roles else "pings nobody"


class _TellSelect(ui.RoleSelect[Panel]):
    def __init__(self, part: DiscordPart, label: str, keys: tuple[str, ...], page: int) -> None:
        super().__init__(placeholder=label[:150], min_values=0, max_values=MOST_PINGED, default_values=_picked_as_roles(_roles_pinged(part, part.file.section("messages").get(keys[0]) or {})))
        self.part, self.keys, self.page, self.about = part, keys, page, label

    async def callback(self, interaction: discord.Interaction) -> None:
        for key in self.keys:
            set_message(self.part.path, key, ping=ping_value([str(role.id) for role in self.values]))
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.about}: {_audit_words(list(self.values))}"))
        await WhoGetsTold(self.part, self.page).open(interaction)


class WhoGetsTold(Panel):
    """Everything's silent until someone says who to tell: that's the one thing a new Discord can't guess."""

    PER_PAGE = 4

    def __init__(self, part: DiscordPart, page: int = 0) -> None:
        super().__init__(part)
        self.page = page
        self.shown = features.TOLD[page * self.PER_PAGE:(page + 1) * self.PER_PAGE]
        for label, keys in self.shown:
            self.add_item(_TellSelect(part, label, keys, page))
        self.nav()
        for other, word in ((page - 1, "Previous"), (page + 1, "Next")):
            if 0 <= other < (len(features.TOLD) + self.PER_PAGE - 1) // self.PER_PAGE:
                button: ui.Button[Panel] = ui.Button(label=word, style=discord.ButtonStyle.secondary, row=4)

                async def go(interaction: discord.Interaction, other: int = other) -> None:
                    await WhoGetsTold(part, other).open(interaction)

                button.callback = go  # type: ignore[method-assign]
                self.add_item(button)

    def card(self) -> discord.Embed:
        lines = []
        for number, (label, keys) in enumerate(self.shown, 1):
            roles = _roles_pinged(self.part, self.part.file.section("messages").get(keys[0]) or {})
            lines.append(f"{number}. **{label}**: " + ("pings " + ", ".join(f"<@&{role['id']}>" for role in roles) if roles else "pings nobody"))
        return discord.Embed(title="Who gets told", description="\n".join(["These menus, from the top:", *lines, "", "A card goes where Channels and Messages say, but only a ping makes anyone's phone buzz. Everything else in Messages can have one too, in its line."]), colour=discord.Colour.blurple())


class _TableWords(ui.Button[Panel]):
    """A setting that's a table of names and values, one to a line as "name = value"."""

    def __init__(self, part: DiscordPart, section: tuple[str, ...], key: str, label: str, hint: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.part, self.section, self.key, self.hint = part, section, key, hint

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_TableForm(self.part, self.section, self.key, str(self.label), self.hint))


class _TableForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, section: tuple[str, ...], key: str, label: str, hint: str) -> None:
        super().__init__(title=label[:45])
        self.part, self.section, self.key = part, section, key
        now = part.file.section(*section).get(key) or {}
        self.words: ui.TextInput[Any] = ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=1000, placeholder=hint[:100], default="\n".join(f"{name} = {value}" for name, value in now.items())[:1000])
        self.add_item(ui.Label(text="One to a line: name = value", component=self.words))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if rank_of(self.part.file, someone_from(interaction.user)) != "owner":
            await answer(interaction, NOT_OWNER)
            return
        found = {name.strip(): value.strip() for name, _, value in (line.partition("=") for line in str(self.words.value).splitlines()) if name.strip() and value.strip()}
        set_switch(self.part.path, self.section, self.key, found)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{'.'.join(self.section)}: {self.key}"))
        await answer(interaction, "Saved.")


STAFF_SEES = [("Staff commands show to those with Manage Messages", "manage_messages"), ("Staff commands show to those with Kick Members", "kick_members"), ("Staff commands show to those with Moderate Members", "moderate_members"),
              ("Staff commands show to those with Manage Server", "manage_guild"), ("Staff commands show to everyone (rank still decides)", "")]
POWER_RANKS = [("The power buttons are for moderators and up", "moderator"), ("The power buttons are for admins and up", "admin"), ("The power buttons are for owners", "owner")]
POWER_CHOICES = [("Start", "start"), ("Stop", "stop"), ("Restart", "restart"), ("Restart after the game", "restart-after-game"), ("Restart when empty", "restart-when-empty"), ("Cancel what's planned", "cancel"), ("Check for updates", "updates")]


class CommandsPanel(Panel):
    """The slash commands, and who sees them. Rank is what decides whether one works: this is only who's shown it."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("commands")
        self.add_item(_SettingChoice(part, "commands", "staff_permission", "Who's shown staff commands", STAFF_SEES, options.get("staff_permission", ""), CommandsPanel))
        self.add_item(_SettingChoice(part, "commands", "power_rank", "Who gets the power buttons", POWER_RANKS, options.get("power_rank") or "admin", CommandsPanel))
        self.add_item(_SettingChoice(part, "commands", "power_buttons", "Which power buttons there are", POWER_CHOICES, options.get("power_buttons") or [], CommandsPanel, several=True))
        self.nav()
        self.add_item(_SettingWords(part, "commands", "public", "Answers everyone sees"))
        self.add_item(_SettingWords(part, "commands", "hidden", "Commands not offered"))
        self.add_item(_TableWords(part, ("commands",), "names", "Rename", "whois = lookup"))
        self.add_item(_SettingWords(part, "commands", "shown_as", "How staff are named"))

    def card(self) -> discord.Embed:
        options = self.part.file.section("commands")
        state = features.state_of(features.BY_KEY["commands"], self.part.file, self.part.facts)
        text = (f"**Now:** {state[0]}{f' ({state[1]})' if state[1] else ''}. Switch it in Features.\n\nEvery in-game command as a slash command, with a box for each thing it takes. Their rank decides what works, as in game; answers only the asker sees, unless listed here.\n\n"
                f"**Answers everyone sees:** {', '.join(str(one) for one in options.get('public') or []) or 'none'}\n**Not offered:** {', '.join(str(one) for one in options.get('hidden') or []) or 'none'}\n"
                f"**Renamed:** {', '.join(f'{name} to {value}' for name, value in (options.get('names') or {}).items()) or 'none of yours'}\n**Staff are named:** {options.get('shown_as') or '{name}'} (their name in this Discord)\n\nA new list of commands can take Discord a moment to show. Ctrl+R brings it sooner.")
        return discord.Embed(title="Commands", description=text, colour=discord.Colour.blurple())


BOARD_SHOW = [("Its state", "state"), ("The map", "map"), ("The mode", "mode"), ("Players", "players"), ("What's next", "next"), ("How long it's been up", "uptime"), ("The playlist", "playlist"), ("Votes", "votes"), ("The version", "version")]


class StatusPanel(Panel):
    """What the world sees of the servers."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        board, seeding = part.file.section("status", "board"), part.file.section("status", "seeding")
        self.add_item(_NestedChoice(part, ("status", "board"), "layout", "How the board's laid out", [("The board: a card for each server", "per-server"), ("The board: one card for all of them", "one")], board.get("layout") or "per-server", StatusPanel))
        self.add_item(_NestedChoice(part, ("status", "board"), "show", "What a card shows", BOARD_SHOW, board.get("show") or [], StatusPanel, several=True))
        self.add_item(_NestedChoice(part, ("status", "board"), "update_seconds", "How often it updates", [("The board updates every minute", 60), ("The board updates every 2 minutes", 120), ("The board updates every 5 minutes", 300), ("The board updates every 15 minutes", 900)], board.get("update_seconds", 120), StatusPanel))
        self.add_item(_NestedChoice(part, ("status", "seeding"), "seed_at", "When it asks for players", [("Ask for players at 1 to 3 on", [1, 3]), ("Ask for players at 1 to 5 on", [1, 5]), ("Ask for players at 2 to 6 on", [2, 6]), ("Ask for players at 4 to 8 on", [4, 8])], seeding.get("seed_at") or [1, 3], StatusPanel))
        self.nav()
        self.add_item(_FeatureSwitch(part, "status.board", StatusPanel, named="The board"))
        self.add_item(_FeatureSwitch(part, "status.seeding", StatusPanel, named="Asking for players"))
        self.add_item(_FeatureSwitch(part, "status.presence", StatusPanel, named="The status line"))
        self.add_item(_NestedWords(part, ("status", "presence"), "lines", "The status line's words"))

    def card(self) -> discord.Embed:
        lines = [f"{MARKS[features.state_of(features.BY_KEY[key], self.part.file, self.part.facts)[0]]} {features.BY_KEY[key].title}: {features.WHAT.get(key, '')}" for key in ("status.board", "status.seeding", "status.presence", "status.bio", "status.nickname")]
        return discord.Embed(title="Status", description="\n".join([*lines, "", "These menus, from the top: the board's layout, what a card shows, how often it updates, when it asks for players. Words can use {players_total}, {servers_up}, {server}, {map}, {mode}."]), colour=discord.Colour.blurple())


class NotesPanel(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        notes = part.file.section("notes")
        self.add_item(_SettingChoice(part, "notes", "expire_days", "How long a note lasts", [("Notes last for good", 0), ("Notes last 30 days", 30), ("Notes last 90 days", 90), ("Notes last a year", 365)], notes.get("expire_days", 0), NotesPanel))
        self.add_item(_SettingChoice(part, "notes", "note_lines", "How many a card shows", [("A card shows no notes", 0), ("A card shows the last note", 1), ("A card shows the last 3 notes", 3), ("A card shows the last 5 notes", 5)], notes.get("note_lines", 3), NotesPanel))
        self.nav()
        self.add_item(_FeatureSwitch(part, "notes", NotesPanel))
        self.add_item(_TableWords(part, ("notes",), "flags", "The flags", "regular = blue"))
        self.add_item(_SettingWords(part, "notes", "watch_flags", "Flags that say when they join"))

    def card(self) -> discord.Embed:
        notes = self.part.file.section("notes")
        state = features.state_of(features.BY_KEY["notes"], self.part.file, self.part.facts)
        return discord.Embed(title="Notes and flags", description=f"**Now:** {state[0]}{f' ({state[1]})' if state[1] else ''}.\n\n/note and /flag are what staff write down about a player, and it's shown on their cards.\n\n**Flags:** {', '.join(notes.get('flags') or {}) or 'none'}\n**Say when they join:** {', '.join(str(one) for one in notes.get('watch_flags') or []) or 'none'}", colour=discord.Colour.blurple())


class _NestedChoice(ui.Select[Panel]):
    """As _SettingChoice, for a table inside a table."""

    def __init__(self, part: DiscordPart, table: tuple[str, ...], key: str, placeholder: str, choices: list[tuple[str, Any]], current: Any, screen: type[Panel], *, several: bool = False) -> None:
        wanted = [json.dumps(value) for value in current] if several else [json.dumps(current)]
        options = [discord.SelectOption(label=label, value=json.dumps(value), default=json.dumps(value) in wanted) for label, value in choices]
        if not several and json.dumps(current) not in {json.dumps(value) for _, value in choices}:
            options.append(discord.SelectOption(label=f"As the file says: {current}"[:100], value=json.dumps(current), default=True))
        super().__init__(placeholder=placeholder, min_values=1, max_values=len(options) if several else 1, options=options)
        self.part, self.table, self.key, self.screen, self.several = part, table, key, screen, several

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = [json.loads(value) for value in self.values]
        set_switch(self.part.path, self.table, self.key, picked if self.several else picked[0])
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{'.'.join(self.table)}: {self.key} is {picked if self.several else picked[0]}"))
        await self.screen(self.part).open(interaction)


class _NestedWords(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, table: tuple[str, ...], key: str, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.part, self.table, self.key = part, table, key

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_NestedWordsForm(self.part, self.table, self.key, str(self.label)))


class _NestedWordsForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, table: tuple[str, ...], key: str, label: str) -> None:
        super().__init__(title=label[:45])
        self.part, self.table, self.key = part, table, key
        self.words: ui.TextInput[Any] = ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=1000, default="\n".join(str(one) for one in part.file.section(*table).get(key) or [])[:1000])
        self.add_item(ui.Label(text="One to a line", component=self.words))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if rank_of(self.part.file, someone_from(interaction.user)) != "owner":
            await answer(interaction, NOT_OWNER)
            return
        set_switch(self.part.path, self.table, self.key, [line.strip() for line in str(self.words.value).splitlines() if line.strip()])
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{'.'.join(self.table)}: {self.key}"))
        await answer(interaction, "Saved.")


class _ConsoleChoice(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, key: str, choices: dict[str, str], current: str) -> None:
        super().__init__(options=[discord.SelectOption(label=label, value=value, default=value == current) for value, label in choices.items()], min_values=1, max_values=1)
        self.part, self.key = part, key

    async def callback(self, interaction: discord.Interaction) -> None:
        value, path = self.values[0], self.part.path
        if self.key == "lines":
            set_switch(path, ("console",), "raw_rank", value)
        elif self.key == "answers":
            set_switch(path, ("console",), "answers", value.partition("+")[0])
            set_switch(path, ("console",), "long_answers", value.partition("+")[2])
        else:
            set_switch(path, ("console",), self.key, value)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"the console: {self.key} is {value}"))
        await Console(self.part).open(interaction)


class _ConsoleRemember(ui.Button[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        self.channel_wide = part.file.section("console").get("remember_server") == "channel"
        super().__init__(label="The menu: one server for everyone" if self.channel_wide else "The menu: each person's own server", style=discord.ButtonStyle.secondary, row=4)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        set_switch(self.part.path, ("console",), "remember_server", "member" if self.channel_wide else "channel")
        await self.part.reload(force=True)
        await Console(self.part).open(interaction)


class _ConsoleAsksFirst(ui.Button[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(label="What asks first", style=discord.ButtonStyle.secondary, row=4)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_AsksFirstForm(self.part))


class _AsksFirstForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(title="What asks first")
        self.part = part
        self.words: ui.TextInput[Any] = ui.TextInput(label="Commands, with spaces between", style=discord.TextStyle.paragraph, required=False, max_length=1000,
                                                     default=" ".join(str(word) for word in part.file.section("console").get("confirm") or [])[:1000])
        self.add_item(self.words)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if rank_of(self.part.file, someone_from(interaction.user)) != "owner":
            await answer(interaction, NOT_OWNER)
            return
        wanted = [word.strip().lstrip("!").lower() for word in str(self.words.value).replace(",", " ").split() if word.strip().lstrip("!")]
        set_switch(self.part.path, ("console",), "confirm", wanted)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what="what the console asks first"))
        await interaction.response.send_message(f"These ask first now: {', '.join(wanted) or 'nothing'}.", ephemeral=True)


class ServerChannels(Panel):
    """Each server's own console channel, four to a page."""

    def __init__(self, part: DiscordPart, page: int = 0) -> None:
        super().__init__(part)
        servers = [str(name) for name in part.ctx.servers]
        self.page, self.pages = page, max(1, (len(servers) + SERVERS_PER_PAGE - 1) // SERVERS_PER_PAGE)
        self.shown = servers[page * SERVERS_PER_PAGE:(page + 1) * SERVERS_PER_PAGE]
        for server in self.shown:
            self.add_item(_ServerChannel(part, server, page))
        self.nav()
        for number, label in ((page - 1, "Previous"), (page + 1, "Next")):
            if 0 <= number < self.pages:
                self.add_item(_ServerChannelsPage(part, number, label))
        if part.file.section("console").get("channels"):
            self.add_item(_ForgetServerChannels(part))

    def card(self) -> discord.Embed:
        have = dict(self.part.file.section("console").get("channels") or {})
        lines = [f"**{name}:** {f'<#{have[name]}>' if str(have.get(name) or '').isdecimal() else have.get(name) or 'none'}" for name in self.shown]
        return discord.Embed(title=f"A channel for each server ({self.page + 1} of {self.pages})", description="\n".join(lines) + "\n\nA server without one is reached from the picker's channel, if that's set in Channels.", colour=discord.Colour.blurple())


class _ServerChannel(ui.ChannelSelect[Panel]):
    def __init__(self, part: DiscordPart, server: str, page: int) -> None:
        super().__init__(placeholder=server[:150], channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.part, self.server, self.page = part, server, page

    async def callback(self, interaction: discord.Interaction) -> None:
        have = dict(self.part.file.section("console").get("channels") or {})
        set_switch(self.part.path, ("console",), "channels", {**have, self.server: str(self.values[0].id)})
        await self.part.reload(force=True)
        await ServerChannels(self.part, self.page).open(interaction)


class _ServerChannelsPage(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await ServerChannels(self.part, self.page).open(interaction)


class _ForgetServerChannels(ui.Button[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(label="Clear them all", style=discord.ButtonStyle.danger, row=4)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        set_switch(self.part.path, ("console",), "channels", {})
        await self.part.reload(force=True)
        await ServerChannels(self.part).open(interaction)


# Reports, appeals, contact ---------------------------------------------------------------------------------------


class _SettingChoice(ui.Select[Panel]):
    """One setting, picked from a few: its value is written, and the panel shown again."""

    def __init__(self, part: DiscordPart, section: str, key: str, placeholder: str, choices: list[tuple[str, Any]], current: Any, screen: type[Panel], *, several: bool = False) -> None:
        wanted = [str(value) for value in current] if several else [str(current)]
        super().__init__(placeholder=placeholder, min_values=1, max_values=len(choices) if several else 1,
                         options=[discord.SelectOption(label=label, value=json.dumps(value), default=(value if several else str(value)) in wanted) for label, value in choices])
        self.part, self.section, self.key, self.screen, self.several = part, section, key, screen, several

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = [json.loads(value) for value in self.values]
        value = [one for many in picked for one in (many if isinstance(many, list) else [many])] if self.several else picked[0]
        set_switch(self.part.path, (self.section,), self.key, value)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.section}: {self.key} is {value}"))
        await self.screen(self.part).open(interaction)


class _SettingSwitch(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, section: str, key: str, label: str, screen: type[Panel]) -> None:
        self.now = bool(part.file.section(section).get(key))
        super().__init__(label=f"{label}: {'yes' if self.now else 'no'}"[:80], style=discord.ButtonStyle.secondary, row=4)
        self.part, self.section, self.key, self.screen = part, section, key, screen

    async def callback(self, interaction: discord.Interaction) -> None:
        set_switch(self.part.path, (self.section,), self.key, not self.now)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.section}: {self.key} is {'off' if self.now else 'on'}"))  # it may have just opened something to everyone
        await self.screen(self.part).open(interaction)


class _FeatureSwitch(ui.Button[Panel]):
    """On or off, from the feature's own panel: it needn't be looked for in Features."""

    def __init__(self, part: DiscordPart, key: str, screen: type[Panel], *, named: str = "") -> None:
        self.feature = features.BY_KEY[key]
        now = self.feature.is_on(part.file)
        super().__init__(label=f"{named}: on" if named and now else f"{named}: off" if named else "Switch it off" if now else "Switch it on", style=discord.ButtonStyle.danger if now else discord.ButtonStyle.success, row=4)
        self.part, self.screen = part, screen

    async def callback(self, interaction: discord.Interaction) -> None:
        turned = features.switched(self.feature, self.part.file)
        set_switch(self.part.path, self.feature.table, self.feature.switch, turned)
        await self.part.reload(force=True)
        if self.feature.key == "contact":
            self.part.contact.keep_card()  # so the panel can say where it is, rather than in a moment
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.feature.title} is {'off' if turned == self.feature.off_value else 'on'}"))
        await self.screen(self.part).open(interaction)


class _SettingWords(ui.Button[Panel]):
    """Opens a form for a setting that's words, or a list of them, one to a line."""

    def __init__(self, part: DiscordPart, section: str, key: str, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.part, self.section, self.key = part, section, key

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_WordsForm(self.part, self.section, self.key, str(self.label)))


class _WordsForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, section: str, key: str, label: str) -> None:
        super().__init__(title=label[:45])
        self.part, self.section, self.key = part, section, key
        now = part.file.section(section).get(key)
        self.is_list = isinstance(now, list)
        self.words: ui.TextInput[Any] = ui.TextInput(label=("One to a line" if self.is_list else label)[:45], style=discord.TextStyle.paragraph, required=False, max_length=1000,
                                                     default=("\n".join(str(one) for one in now) if self.is_list else str(now or ""))[:1000])
        self.add_item(self.words)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if rank_of(self.part.file, someone_from(interaction.user)) != "owner":
            await answer(interaction, NOT_OWNER)
            return
        typed = str(self.words.value).strip()
        set_switch(self.part.path, (self.section,), self.key, [line.strip() for line in typed.splitlines() if line.strip()] if self.is_list else typed)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.section}: {self.key}"))
        await interaction.response.send_message("Saved.", ephemeral=True)


class ReportsPanel(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("reports")
        self.add_item(_SettingChoice(part, "reports", "from", "Where reports come from…", [("In game", "game"), ("In Discord", "discord")], options.get("from") or [], ReportsPanel, several=True))
        self.add_item(_SettingChoice(part, "reports", "threshold", "Ping staff once this many different people report someone…",
                                     [("Never ping staff about reports", 0), *[(f"Ping staff at {people} people reporting", people) for people in (2, 3, 5, 10)]], options.get("threshold", 0), ReportsPanel))
        self.add_item(_SettingChoice(part, "reports", "threshold_minutes", "…within…", [(f"…within {minutes} minutes", minutes) for minutes in (5, 10, 30, 60)], options.get("threshold_minutes", 10), ReportsPanel))
        self.add_item(_SettingChoice(part, "reports", "reporter_cooldown_seconds", "One person can report once in…", [("No limit on reports", 0), ("One report each per minute", 60), ("One report each per 5 minutes", 300), ("One report each per hour", 3600)], options.get("reporter_cooldown_seconds", 60), ReportsPanel))
        self.nav()
        self.add_item(_FeatureSwitch(part, "reports", ReportsPanel))
        self.add_item(_SettingSwitch(part, "reports", "thank_reporter", "Thank them", ReportsPanel))

    def card(self) -> discord.Embed:
        state = features.state_of(features.BY_KEY["reports"], self.part.file, self.part.facts)
        return discord.Embed(title="Reports", colour=discord.Colour.blurple(),
                             description=f"**Now:** {state[0]}{f' ({state[1]})' if state[1] else ''}.\n\nEach player has one card in `reports` while it's open, whoever else reports them. Who's pinged is on Who gets told.")


class AppealsPanel(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("appeals")
        self.add_item(_SettingChoice(part, "appeals", "for", "What can be appealed…", [("Can appeal: bans", "ban"), ("Can appeal: kicks with a time", "kick"), ("Can appeal: mutes", "mute")], options.get("for") or [], AppealsPanel, several=True))
        self.add_item(_SettingChoice(part, "appeals", "cooldown_days", "The usual wait before they can appeal again…", [("Usually: appeal again any time", 0), *[(f"Usually: appeal again after {days} day{'s' if days != 1 else ''}", days) for days in (1, 7, 30)]], options.get("cooldown_days", 7), AppealsPanel))
        self.add_item(_SettingChoice(part, "appeals", "tell_outcome", "Telling them how it went…", [("Tell them how it went by DM", "dm"), ("Don't tell them how it went", "no")], options.get("tell_outcome", "dm"), AppealsPanel))
        self.nav()
        self.add_item(_FeatureSwitch(part, "appeals", AppealsPanel))
        self.add_item(_SettingWords(part, "appeals", "questions", "The questions"))
        self.add_item(_SettingWords(part, "appeals", "in_reason", "Added to a ban's message"))

    def card(self) -> discord.Embed:
        state = features.state_of(features.BY_KEY["appeals"], self.part.file, self.part.facts)
        return discord.Embed(title="Appeals", colour=discord.Colour.blurple(),
                             description=f"**Now:** {state[0]}{f' ({state[1]})' if state[1] else ''}.\n\nWhat's added to a ban's message tells a player how to appeal, and `{{code}}` in it finds them without their name: `Appeal on discord.gg/yours: /appeal {{code}}`.")


class ContactPanel(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        options = part.file.section("contact")
        self.add_item(_ContactChannel(part))
        self.add_item(_ContactRole(part))
        self.add_item(_SettingChoice(part, "contact", "close_after_days", "With nothing said, a thread closes after…", [("Quiet threads stay open", 0), *[(f"Quiet threads close after {days} day{'s' if days != 1 else ''}", days) for days in (1, 3, 7, 14, 30)]], options.get("close_after_days", 7), ContactPanel))
        self.nav()
        self.add_item(_FeatureSwitch(part, "contact", ContactPanel))
        self.add_item(_SettingSwitch(part, "contact", "transcript", "Keep a transcript", ContactPanel))
        self.add_item(_SettingWords(part, "contact", "categories", "The categories"))

    def card(self) -> discord.Embed:
        config = self.part.file
        state = features.state_of(features.BY_KEY["contact"], config, self.part.facts)
        options = config.section("contact")
        channel = kept.channel_of(self.part, options.get("channel"))
        message = self.part.status.stored(contact.CARD)[0]
        where = f"https://discord.com/channels/{config.guild}/{channel}/{message}" if channel and message else f"<#{channel}>, once it's switched on" if channel else "nowhere yet: pick a channel below"
        said = (f"**Now:** {state[0]}{f' ({state[1]})' if state[1] else ''}.\n**The button:** {where}\n**Brought into each thread:** {_pings_in_words(self.part, options.get('thread_role'))}\n\n"
                "A button in a channel everyone can read opens a private thread with the staff, and staff get a card in `reports`. For people to write in their thread, they need Send Messages in Threads in that channel, and the bot needs Create Private Threads and Manage Threads.")
        return discord.Embed(title="Contact staff", colour=discord.Colour.blurple(), description=said)


class _ContactChannel(ui.ChannelSelect[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(placeholder="Where the button goes…", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        set_switch(self.part.path, ("contact",), "channel", str(self.values[0].id))
        await self.part.reload(force=True)
        self.part.contact.keep_card()
        await ContactPanel(self.part).open(interaction)


class _ContactRole(ui.RoleSelect[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(placeholder="The roles brought into each thread…", min_values=0, max_values=MOST_PINGED, default_values=_picked_as_roles(_roles_pinged(part, part.file.section("contact").get("thread_role"))))
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        set_switch(self.part.path, ("contact",), "thread_role", ping_value([str(role.id) for role in self.values]))
        await self.part.reload(force=True)
        await ContactPanel(self.part).open(interaction)


# Messages ------------------------------------------------------------------------------------------------------


class Messages(Panel):
    """Every kind of message, a page at a time: pick one to change where it goes, its colour, or whether it posts."""

    def __init__(self, part: DiscordPart, page: int = 0) -> None:
        super().__init__(part)
        self.page = page
        keys = list(part.file.section("messages"))
        self.pages = max(1, (len(keys) + FEATURES_PER_PAGE - 1) // FEATURES_PER_PAGE)
        self.shown = keys[page * FEATURES_PER_PAGE:(page + 1) * FEATURES_PER_PAGE]
        self.add_item(_MessageSelect(part, self.shown, page))
        self.nav(row=1)
        if page > 0:
            self.add_item(_MessagesPage(part, page - 1, "Previous"))
        if page + 1 < self.pages:
            self.add_item(_MessagesPage(part, page + 1, "Next"))

    def card(self) -> discord.Embed:
        lines = []
        messages = self.part.file.section("messages")
        for key in self.shown:
            line = messages.get(key) or {}
            state = "" if line.get("post", True) else " · **off**"
            lines.append(f"`{key}` → {line.get('channel')} · {line.get('colour')}{state}")
        text = "\n".join(lines) + "\n\n`/huragok gallery` shows them all in their colours."
        return discord.Embed(title=f"Messages ({self.page + 1} of {self.pages})", description=text[:4000], colour=discord.Colour.blurple())


class _MessageSelect(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, keys: list[str], page: int) -> None:
        super().__init__(placeholder="Pick a message to change", options=[discord.SelectOption(label=key[:100], value=key) for key in keys],
                         min_values=1, max_values=1)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await OneMessage(self.part, self.values[0], self.page).open(interaction)


class _MessagesPage(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int, label: str, *, row: int = 1) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=row)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await Messages(self.part, self.page).open(interaction)


class OneMessage(Panel):
    """One message: its channel, its colour, on or off."""

    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(part)
        self.key, self.page = key, page
        self.line = part.file.section("messages").get(key) or {}
        self.add_item(_MessageChannel(part, key, page))
        self.add_item(_MessageColour(part, key, page, str(self.line.get("colour") or "")))
        self.add_item(_MessagePing(part, key, page))
        self.is_ticket = part.feeds.shape_for(key, self.line) == TICKET
        if self.is_ticket:
            self.add_item(_MessageCloses(part, key, page, str(self.line.get("closes_to") or "")))
        self.add_item(_MessageSwitch(part, key, page, bool(self.line.get("post", True))))
        self.add_item(_MessagesPage(part, page, "Back", row=4))
        self.add_item(_MessageAnyColour(part, key, page))
        if self.is_ticket:
            self.add_item(_ClosesChannelOpen(part, key, page))

    def card(self) -> discord.Embed:
        colours = self.part.file.data["colours"]
        name = str(self.line.get("colour") or "")
        value = colour_value(self.part.file, name)
        state = "It posts." if self.line.get("post", True) else "It's switched off."
        closes = f"\n**When it's finished:** {self.line.get('closes_to') or 'as Tickets says'}" if self.is_ticket else ""
        kind = {TICKET: "a ticket, with buttons, whichever channel it's in", LOG: "a record in a log channel"}.get(self.part.feeds.shape_for(self.key, self.line), "a post")
        text = (f"**It's:** {kind}\n**Channel:** {self.line.get('channel')}\n**Colour:** {name} ({colours.get(name, name)})\n**Pings:** {_pings_in_words(self.part, self.line)}{closes}\n{state}\n\n"
                "These menus, from the top: its channel, its colour, who it pings" + (", when it's finished. " if self.is_ticket else ". ") + "This card is in its colour.")
        return discord.Embed(title=self.key, description=text, colour=value)


class _MessageChannel(ui.ChannelSelect[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(placeholder="Send it to a channel…", channel_types=CHANNEL_KINDS, min_values=1, max_values=1)
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        names = {str(value).strip(): name for name, value in self.part.file.section("channels").items() if str(value).strip()}
        set_message(self.part.path, self.key, channel=names.get(str(picked.id), f"#{picked.name}"))
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.key} goes to #{picked.name}"))
        await OneMessage(self.part, self.key, self.page).open(interaction)


class _MessagePing(ui.RoleSelect[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(placeholder="Roles to ping when it's posted…", min_values=0, max_values=MOST_PINGED, default_values=_picked_as_roles(_roles_pinged(part, part.file.section("messages").get(key) or {})))
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        set_message(self.part.path, self.key, ping=ping_value([str(role.id) for role in self.values]))
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.key} {_audit_words(list(self.values))}"))
        await OneMessage(self.part, self.key, self.page).open(interaction)


class _MessageAnyColour(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(label="Any colour", style=discord.ButtonStyle.secondary, row=4)
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_ColourForm(self.part, self.key, self.page))


class _ColourForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(title="Any colour")
        self.part, self.key, self.page = part, key, page
        self.colour: ui.TextInput[Any] = ui.TextInput(max_length=40, placeholder="#FF8800, or a name from [colours]")
        self.add_item(ui.Label(text="The colour", component=self.colour))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        typed = str(self.colour.value).strip()
        if colour_value_of(self.part.file.data["colours"], typed) is None:
            await answer(interaction, f"\"{cards.clean(typed)}\" isn't a colour: try #FF8800.")
            return
        set_message(self.part.path, self.key, colour=typed)
        await self.part.reload(force=True)
        await OneMessage(self.part, self.key, self.page).open(interaction)


class _ClosesChannelOpen(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(label="A channel of its own when finished", style=discord.ButtonStyle.secondary, row=4)
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await _ClosesScreen(self.part, self.key, self.page).open(interaction)


class _ClosesScreen(Panel):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(part)
        self.key = key
        self.add_item(_MessageClosesChannel(part, key, page))
        back = ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=4)

        async def go_back(interaction: discord.Interaction) -> None:
            await OneMessage(part, key, page).open(interaction)

        back.callback = go_back  # type: ignore[method-assign]
        self.add_item(back)

    def card(self) -> discord.Embed:
        return discord.Embed(title=self.key, description="Pick the channel this kind of card goes to when it's finished. Who can read it isn't checked for you: a channel for staff should be private.", colour=discord.Colour.blurple())


class _MessageCloses(ui.Select[Panel]):
    """Where this kind of card goes when it's finished."""

    def __init__(self, part: DiscordPart, key: str, page: int, current: str) -> None:
        choices = [("As Tickets says", ""), ("To logs", "logs"), ("To huragok-logs", "huragok-logs"), ("Stay where it is", "stay"), ("Stay, as its own thread", "thread")]
        if current and current not in {value for _, value in choices}:
            choices.append((f"To {current}"[:100], current))
        super().__init__(placeholder="When it's finished…", min_values=1, max_values=1,
                         options=[discord.SelectOption(label=label, value=value or "default", default=value == current) for label, value in choices])
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        wanted = "" if self.values[0] == "default" else self.values[0]
        set_message(self.part.path, self.key, closes_to=wanted)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.key} when it's finished: {wanted or 'as Tickets says'}"))
        await OneMessage(self.part, self.key, self.page).open(interaction)


class _MessageClosesChannel(ui.ChannelSelect[Panel]):
    """A log channel of its own, for this kind of card alone."""

    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(placeholder="…or to a channel of its own", channel_types=CHANNEL_KINDS, min_values=1, max_values=1)
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        set_message(self.part.path, self.key, closes_to=str(picked.id))
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.key} when it's finished goes to #{picked.name}"))
        await OneMessage(self.part, self.key, self.page).open(interaction)


class _MessageColour(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int, current: str) -> None:
        names = list(part.file.data["colours"])
        # A menu holds 25: this one's colour, then the rest in the wheel's order, thinned evenly to fit. Any colour's a button away.
        evenly = [names[number * len(names) // 24] for number in range(24)] if len(names) > 24 else names
        chosen = [current] if current in names else []
        chosen += [name for name in evenly if name != current][:25 - len(chosen)]
        super().__init__(placeholder="Pick a colour…", min_values=1, max_values=1,
                         options=[discord.SelectOption(label=name, value=name, default=name == current) for name in chosen])
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        set_message(self.part.path, self.key, colour=self.values[0])
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.key} is {self.values[0]}"))
        await OneMessage(self.part, self.key, self.page).open(interaction)


class _MessageSwitch(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int, posting: bool) -> None:
        super().__init__(label="Switch it off" if posting else "Switch it on", style=discord.ButtonStyle.danger if posting else discord.ButtonStyle.success, row=4)
        self.part, self.key, self.page, self.posting = part, key, page, posting

    async def callback(self, interaction: discord.Interaction) -> None:
        set_message(self.part.path, self.key, post=not self.posting)
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{self.key} is {'off' if self.posting else 'on'}"))
        await OneMessage(self.part, self.key, self.page).open(interaction)


# Features ------------------------------------------------------------------------------------------------------


class _FeatureSelect(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, shown: list[features.Feature], page: int) -> None:
        options = [discord.SelectOption(label=feature.title[:100], value=feature.key, description=features.WHAT.get(feature.key, "")[:100] or None,
                                        emoji=MARKS[features.state_of(feature, part.file, part.facts)[0]])
                   for feature in shown]
        super().__init__(placeholder="Switch a feature on or off", options=options, min_values=1, max_values=1)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        feature = features.BY_KEY[self.values[0]]
        if feature.stage > features.BUILT:
            await interaction.response.send_message(f"**{feature.title}** arrives in a later version.", ephemeral=True)
            return
        config = self.part.file
        turned = features.switched(feature, config)
        set_switch(self.part.path, feature.table, feature.switch, turned)
        log.info("%s switched %s %s in Discord", interaction.user.name, feature.key, "off" if turned == feature.off_value else "on")
        await self.part.reload(force=True)
        self.part.audit("setup", say(self.part.file, "audit.setup", who=acting_as(self.part, interaction.user), what=f"{feature.title} is {'off' if turned == feature.off_value else 'on'}"))
        await Features(self.part, self.page).open(interaction)


class Features(Panel):
    def __init__(self, part: DiscordPart, page: int = 0) -> None:
        super().__init__(part)
        self.page = page
        built = [feature for feature in FEATURES if feature.stage <= features.BUILT]  # what isn't there yet is only in the way
        self.pages = max(1, (len(built) + FEATURES_PER_PAGE - 1) // FEATURES_PER_PAGE)
        self.shown = built[page * FEATURES_PER_PAGE:(page + 1) * FEATURES_PER_PAGE]
        self.add_item(_FeatureSelect(part, self.shown, page))
        self.nav(row=1)
        if page > 0:
            self.add_item(_FeaturePage(part, page - 1, "Previous"))
        if page + 1 < self.pages:
            self.add_item(_FeaturePage(part, page + 1, "Next"))

    def card(self) -> discord.Embed:
        lines = []
        for feature in self.shown:
            state, why = features.state_of(feature, self.part.file, self.part.facts)
            lines.append(f"{MARKS[state]} **{feature.title}**: {features.WHAT.get(feature.key, '')}" + (f"\n  ⚠️ {why}" if why and state == features.WAITING else ""))
        return discord.Embed(title=f"Features ({self.page + 1} of {self.pages})", description="\n".join(lines)[:4000],
                             colour=discord.Colour.blurple())


class _FeaturePage(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=1)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await Features(self.part, self.page).open(interaction)
