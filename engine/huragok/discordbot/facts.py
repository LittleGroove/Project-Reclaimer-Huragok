"""What Discord says is true right now: the bot, its Discord, the roles and channels there, and what the bot and
@everyone can do in each channel. Read with rest.py, so the window and /huragok check see the same thing.

References in discord.toml are an ID ("1234567890") or a name ("#admin-log", "@SPR-Admin", "admin-log"); find_role() and
find_channel() take either.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import rest
from .rest import PERMISSIONS

ALL_PERMISSIONS = (1 << 64) - 1
TEXT_KINDS = {0, 5, 15, 16}  # text, announcement, forum, media: where a message can go
CATEGORY = 4
VOICE_KINDS = {2, 13}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class Facts:
    user: dict[str, Any] = field(default_factory=dict)  # the bot
    application: dict[str, Any] = field(default_factory=dict)
    guilds: list[dict[str, Any]] = field(default_factory=list)  # every Discord it's in
    guild: dict[str, Any] | None = None  # the one in discord.toml
    roles: list[dict[str, Any]] = field(default_factory=list)
    channels: list[dict[str, Any]] = field(default_factory=list)
    member: dict[str, Any] | None = None  # the bot, in that Discord

    # The portal's switches ---------------------------------------------------------------------------------

    def intent_on(self, name: str) -> bool:
        """Whether the developer portal lets the bot have this privileged intent."""
        return bool(_int(self.application.get("flags")) & rest.INTENT_FLAGS[name])

    # Finding things ----------------------------------------------------------------------------------------

    def find_role(self, ref: Any) -> dict[str, Any] | None:
        text = str(ref or "").strip()
        if not text:
            return None
        if text.isdecimal():
            return next((r for r in self.roles if str(r.get("id")) == text), None)
        name = text.removeprefix("@").lower()
        return next((r for r in self.roles if str(r.get("name", "")).lower() == name), None)

    def find_channel(self, ref: Any) -> dict[str, Any] | None:
        text = str(ref or "").strip()
        if not text:
            return None
        if text.isdecimal():
            return next((c for c in self.channels if str(c.get("id")) == text), None)
        name = text.removeprefix("#").lower()
        return next((c for c in self.channels if str(c.get("name", "")).lower() == name
                     and c.get("type") != CATEGORY), None)

    def message_channels(self) -> list[dict[str, Any]]:
        """Channels a message can go to, in Discord's order: each category's together, those in none first."""
        return self.sorted_channels(TEXT_KINDS)

    def sorted_channels(self, kinds: set[int]) -> list[dict[str, Any]]:
        by_id = {str(c.get("id")): c for c in self.channels}

        def place(channel: dict[str, Any]) -> tuple[int, int, int]:
            parent = by_id.get(str(channel.get("parent_id") or ""))
            return (_int(parent.get("position")) + 1 if parent else 0, _int(channel.get("position")),
                    _int(channel.get("id")))

        return sorted((c for c in self.channels if c.get("type") in kinds), key=place)

    def category_of(self, channel: dict[str, Any]) -> str:
        parent = str(channel.get("parent_id") or "")
        return next((str(c.get("name")) for c in self.channels if str(c.get("id")) == parent), "")

    # Permissions, worked out as Discord's documents say ------------------------------------------------------

    @property
    def everyone(self) -> dict[str, Any]:
        guild_id = str((self.guild or {}).get("id"))
        return next((r for r in self.roles if str(r.get("id")) == guild_id), {})

    def _member_role_ids(self) -> list[str]:
        return [str(r) for r in (self.member or {}).get("roles", [])]

    def base_permissions(self) -> int:
        """What the bot may do in the Discord as a whole."""
        if self.guild is None or self.member is None:
            return 0
        if str(self.guild.get("owner_id")) == str(self.user.get("id")):
            return ALL_PERMISSIONS
        allowed = _int(self.everyone.get("permissions"))
        for role in self.roles:
            if str(role.get("id")) in self._member_role_ids():
                allowed |= _int(role.get("permissions"))
        return ALL_PERMISSIONS if allowed & PERMISSIONS["administrator"] else allowed

    def permissions_in(self, channel: dict[str, Any]) -> int:
        """What the bot may do in this channel: the Discord's, then @everyone's overwrite, its roles', its own."""
        allowed = self.base_permissions()
        if allowed & PERMISSIONS["administrator"]:
            return ALL_PERMISSIONS
        overwrites = {str(o.get("id")): o for o in channel.get("permission_overwrites") or []}
        guild_id = str((self.guild or {}).get("id"))
        own = overwrites.get(guild_id)
        if own:
            allowed = (allowed & ~_int(own.get("deny"))) | _int(own.get("allow"))
        deny = allow = 0
        for role in self._member_role_ids():
            if role in overwrites:
                deny |= _int(overwrites[role].get("deny"))
                allow |= _int(overwrites[role].get("allow"))
        allowed = (allowed & ~deny) | allow
        mine = overwrites.get(str(self.user.get("id")))
        if mine:
            allowed = (allowed & ~_int(mine.get("deny"))) | _int(mine.get("allow"))
        return allowed

    def has(self, permission: str, channel: dict[str, Any] | None = None) -> bool:
        allowed = self.permissions_in(channel) if channel is not None else self.base_permissions()
        return bool(allowed & PERMISSIONS[permission])

    def everyone_reads(self, channel: dict[str, Any]) -> bool:
        """Whether a plain member of the Discord can read this channel: what a staff feature must never post in."""
        allowed = _int(self.everyone.get("permissions"))
        if allowed & PERMISSIONS["administrator"]:
            return True
        guild_id = str((self.guild or {}).get("id"))
        for overwrite in channel.get("permission_overwrites") or []:
            if str(overwrite.get("id")) == guild_id:
                allowed = (allowed & ~_int(overwrite.get("deny"))) | _int(overwrite.get("allow"))
        return bool(allowed & PERMISSIONS["view"])

    def top_position(self) -> int:
        """The bot's highest role: it can only manage roles below this one."""
        ids = set(self._member_role_ids())
        return max((_int(r.get("position")) for r in self.roles if str(r.get("id")) in ids), default=0)


def gather(token: str, guild_id: str = "") -> Facts:
    """Everything, from Discord, now. Raises rest.DiscordError when it can't be had; a Discord the bot isn't in simply
    leaves `guild` None."""
    facts = Facts(user=rest.bot_user(token), application=rest.application(token), guilds=rest.bot_guilds(token))
    if not guild_id or not any(str(g.get("id")) == guild_id for g in facts.guilds):
        return facts
    facts.guild = rest.guild(token, guild_id)
    facts.roles = rest.guild_roles(token, guild_id)
    facts.channels = rest.guild_channels(token, guild_id)
    facts.member = rest.guild_member(token, guild_id, str(facts.user.get("id")))
    return facts
