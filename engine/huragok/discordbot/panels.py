"""/huragok setup and /huragok features: the same steps as the window's J, with Discord's own pickers. Every panel is
private to the owner using it, and each change is written to discord.toml straight away (the part reloads it), so the
window, the file and Discord always agree."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

from . import features
from .config import RANK_KEYS, add_owner, remove_owner, set_channel, set_rank_roles, set_switch
from .features import FEATURES, MARKS

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

TIMEOUT = 600.0
FEATURES_PER_PAGE = 20  # a menu holds 25
CHANNELS_PER_PAGE = 4  # a panel holds 5 rows; the last is for the buttons
RANK_TITLES = {"admin": "Admins (every server)", "moderator": "Moderators", "probationary-moderator": "Probationary moderators",
               "member": "Members"}
CHANNEL_KINDS = [discord.ChannelType.text, discord.ChannelType.news]


def _ids(values: Any) -> list[int]:
    return [int(value) for value in values if str(value).strip().isdecimal()]


def _defaults(values: Any, kind: discord.SelectDefaultValueType) -> list[discord.SelectDefaultValue]:
    return [discord.SelectDefaultValue(id=value, type=kind) for value in _ids(values)][:25]


class Panel(ui.View):
    """One screen of the setup: a card, and its controls."""

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(timeout=TIMEOUT)
        self.part = part

    def card(self) -> discord.Embed:
        raise NotImplementedError

    async def open(self, interaction: discord.Interaction) -> None:
        """As a new private message, or in place of the panel it was reached from."""
        if interaction.type is discord.InteractionType.component:
            await interaction.response.edit_message(embed=self.card(), view=self)
        else:
            await interaction.response.send_message(embed=self.card(), view=self, ephemeral=True)

    async def _reload(self) -> None:
        await self.part.reload(force=True)

    def nav(self, row: int = 4) -> None:
        back = ui.Button(label="Back", style=discord.ButtonStyle.secondary, row=row)

        async def go_back(interaction: discord.Interaction) -> None:
            await Home(self.part).open(interaction)

        back.callback = go_back  # type: ignore[method-assign]
        self.add_item(back)


class Home(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        for label, screen in (("Ranks", Ranks), ("Owners", Owners), ("Channels", Channels), ("Features", Features)):
            self.add_item(_Open(label, screen, part))
        self.add_item(_Check(part))

    def card(self) -> discord.Embed:
        config = self.part.file
        ranks = config.section("ranks")
        states = [features.state_of(feature, config, self.part.facts)[0] for feature in FEATURES]
        channels = sum(1 for value in config.section("channels").values() if str(value).strip())
        lines = [f"**Mode:** {config.mode}",
                 f"**Owners:** {len(ranks.get('owners', []))}",
                 "**Ranks:** " + ", ".join(f"{rank} {len(ranks.get(key, []))}" for rank, key in RANK_KEYS.items()),
                 f"**Channels named:** {channels} of {len(config.section('channels'))}",
                 f"**Features switched on:** {sum(1 for f in FEATURES if f.is_on(config))} "
                 f"(of {len(states) - states.count(features.COMING)} in this version)",
                 "", "Pick a step below. Each change is written to discord.toml straight away."]
        return discord.Embed(title="Huragok's bot", description="\n".join(lines), colour=discord.Colour.blurple())


class _Open(ui.Button[Panel]):
    def __init__(self, label: str, screen: type[Panel], part: DiscordPart) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.screen, self.part = screen, part

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.screen(self.part).open(interaction)


class _Check(ui.Button[Panel]):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(label="Check", style=discord.ButtonStyle.success)
        self.part = part

    async def callback(self, interaction: discord.Interaction) -> None:
        from .client import findings_cards

        await interaction.response.defer(ephemeral=True, thinking=True)
        findings, _ = await asyncio.to_thread(features.check, self.part.file)
        await interaction.followup.send(embeds=findings_cards(findings), ephemeral=True)


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
        await Owners(self.part).open(interaction)


class Owners(Panel):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        self.add_item(_OwnerSelect(part))
        self.nav()

    def card(self) -> discord.Embed:
        text = ("Owners can use `/huragok` and everything it changes. They're people, never a role: anyone with Manage "
                "Roles can hand out a role, but only an owner or the host can name an owner. This gives owner rights on "
                "the bot only: owner rank in game is never given from Discord.")
        return discord.Embed(title="Owners", description=text, colour=discord.Colour.blurple())


# Channels ------------------------------------------------------------------------------------------------------


class _ChannelSelect(ui.ChannelSelect[Panel]):
    def __init__(self, part: DiscordPart, key: str, page: int) -> None:
        super().__init__(placeholder=f"{key}", channel_types=CHANNEL_KINDS, min_values=0, max_values=1,
                         default_values=_defaults([part.file.section("channels").get(key, "")],
                                                  discord.SelectDefaultValueType.channel))
        self.part, self.key, self.page = part, key, page

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0] if self.values else None
        set_channel(self.part.path, self.key, str(picked.id) if picked else "", picked.name if picked else "")
        log.info("%s set the %s channel in Discord: %s", interaction.user.name, self.key, picked.name if picked else "none")
        await self.part.reload(force=True)
        await Channels(self.part, self.page).open(interaction)


class Channels(Panel):
    def __init__(self, part: DiscordPart, page: int = 0) -> None:
        super().__init__(part)
        self.page = page
        keys = list(part.file.section("channels"))
        self.pages = max(1, (len(keys) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
        for key in keys[page * CHANNELS_PER_PAGE:(page + 1) * CHANNELS_PER_PAGE]:
            self.add_item(_ChannelSelect(part, key, page))
        self.nav()
        if page > 0:
            self.add_item(_ChannelPage(part, page - 1, "Previous"))
        if page + 1 < self.pages:
            self.add_item(_ChannelPage(part, page + 1, "Next"))

    def card(self) -> discord.Embed:
        text = ("The channels features can share: a feature whose `channel` says \"staff\" posts wherever **staff** is "
                "here. Leave out what you won't use. A channel with private things (staff, alerts, console) should be "
                "one @everyone can't read.")
        return discord.Embed(title=f"Channels ({self.page + 1} of {self.pages})", description=text,
                             colour=discord.Colour.blurple())


class _ChannelPage(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=4)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await Channels(self.part, self.page).open(interaction)


# Features ------------------------------------------------------------------------------------------------------


class _FeatureSelect(ui.Select[Panel]):
    def __init__(self, part: DiscordPart, shown: list[features.Feature], page: int) -> None:
        options = [discord.SelectOption(label=feature.title[:100], value=feature.key,
                                        emoji=MARKS[features.state_of(feature, part.file, part.facts)[0]])
                   for feature in shown]
        super().__init__(placeholder="Switch a feature on or off", options=options, min_values=1, max_values=1)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        feature = features.BY_KEY[self.values[0]]
        if feature.stage > features.BUILT:
            await interaction.response.send_message(f"**{feature.title}** arrives in a later beta.", ephemeral=True)
            return
        config = self.part.file
        turned = features.switched(feature, config)
        set_switch(self.part.path, feature.table, feature.switch, turned)
        log.info("%s switched %s %s in Discord", interaction.user.name, feature.key, "off" if turned == feature.off_value else "on")
        await self.part.reload(force=True)
        await Features(self.part, self.page).open(interaction)


class Features(Panel):
    def __init__(self, part: DiscordPart, page: int = 0) -> None:
        super().__init__(part)
        self.page = page
        self.pages = max(1, (len(FEATURES) + FEATURES_PER_PAGE - 1) // FEATURES_PER_PAGE)
        self.shown = list(FEATURES[page * FEATURES_PER_PAGE:(page + 1) * FEATURES_PER_PAGE])
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
            lines.append(f"{MARKS[state]} {feature.title}" + (f": {why}" if why and state == features.WAITING else ""))
        return discord.Embed(title=f"Features ({self.page + 1} of {self.pages})", description="\n".join(lines)[:4000],
                             colour=discord.Colour.blurple())


class _FeaturePage(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, page: int, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=1)
        self.part, self.page = part, page

    async def callback(self, interaction: discord.Interaction) -> None:
        await Features(self.part, self.page).open(interaction)
