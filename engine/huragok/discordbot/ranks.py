"""Who someone in Discord is to Huragok: [ranks] maps Discord roles onto Huragok's ranks, and nothing counts until it's
there. A Discord's own admins aren't the servers' admins (discord_administrators decides what that permission counts
as). Owners are named by user ID, never a role: anyone with Manage Roles can hand out a role, but only the host can
write in discord.toml. What each rank may then do is commands.toml's job, in game and in Discord alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core.commandsfile import PROBATION, ROLES
from .config import RANK_KEYS, DiscordConfig


@dataclass(frozen=True)
class Someone:
    """A member of the Discord, as far as ranks go."""

    user_id: str
    roles: dict[str, str] = field(default_factory=dict)  # role ID -> name
    administrator: bool = False  # Discord's own Administrator permission
    timed_out: bool = False


def _named(ref: object, someone: Someone) -> bool:
    text = str(ref or "").strip()
    if not text:
        return False
    if text.isdecimal():
        return text in someone.roles
    name = text.removeprefix("@").lower()
    return any(role.lower() == name for role in someone.roles.values())


def _higher(one: str, other: str) -> str:
    return one if ROLES.index(one) >= ROLES.index(other) else other


def is_owner(config: DiscordConfig, user_id: str) -> bool:
    return str(user_id) in {str(owner).strip() for owner in config.section("ranks").get("owners", [])}


def rank_of(config: DiscordConfig, someone: Someone, server: str | None = None) -> str:
    """Their rank: the highest their roles give them; `server`: there, with the roles that only count on some servers
    (without it, only what holds on every server). "player" when nothing maps."""
    ranks = config.section("ranks")
    if is_owner(config, someone.user_id):
        return "owner"
    if someone.timed_out and ranks.get("timeout_suspends", True):
        return "player"
    found = "player"
    for rank, key in RANK_KEYS.items():
        if any(_named(ref, someone) for ref in ranks.get(key, [])):
            found = _higher(found, rank)
    for block in ranks.get("scoped") or []:
        if not isinstance(block, dict) or not _named(block.get("role"), someone):
            continue
        rank = str(block.get("rank", "")).strip().replace("_", "-")
        servers = block.get("servers") or []
        if rank in RANK_KEYS and (server is not None and (server in servers or "*" in servers) or "*" in servers):
            found = _higher(found, rank)
    if someone.administrator and ranks.get("discord_administrators") in ("moderator", "admin"):
        found = _higher(found, str(ranks["discord_administrators"]))
    return found


def can_act(rank: str) -> bool:
    """A probationary moderator's rank counts as moderator's for looking, but what they do is a request."""
    return ROLES.index(rank) >= ROLES.index(PROBATION)
