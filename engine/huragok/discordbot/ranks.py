"""Who someone in Discord is to Huragok: [ranks] maps Discord roles onto Huragok's ranks, and nothing counts until it's
there. A Discord's own admins aren't the servers' admins (discord_administrators decides what that permission counts
as). Faculty are named by user ID, never a role: anyone with Manage Roles can hand out a role, but only the host can
write in discord.toml. What each rank may then do is commands.toml's job, in game and in Discord alike.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.commandsfile import PROBATION, ROLES
from .config import RANK_KEYS, DiscordConfig


WHOLE_ID = re.compile(r"\b[0-9a-fA-F]{64}\b")


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


def as_they_may_see(config: DiscordConfig, rank: str, text: str, *, everyone: bool = False) -> str:
    """An answer for someone of this rank: below admin, unless `moderators_see_ids` says otherwise, a player's whole ID is cut to its start, which is enough to tell two apart. Names and #numbers are how moderators work.
    `everyone`: an answer the whole channel reads (`[commands] public`), cut whoever asked: an admin's public answer
    showed whole IDs to everyone there (audit 2026-11, 6.16)."""
    if not everyone and (rank in ("admin", "faculty") or config.section("ranks").get("moderators_see_ids")):
        return text
    return WHOLE_ID.sub(lambda found: found[0][:8], text)


def is_faculty(config: DiscordConfig, user_id: str) -> bool:
    return str(user_id) in {str(one).strip() for one in config.section("ranks").get("faculty", [])}


def rank_of(config: DiscordConfig, someone: Someone, server: str | None = None) -> str:
    """Their rank: the highest their roles give them; `server`: there, with the roles that only count on some servers
    (without it, only what holds on every server). "player" when nothing maps."""
    ranks = config.section("ranks")
    if is_faculty(config, someone.user_id):
        return "faculty"
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
        servers = [servers] if isinstance(servers, str) else servers  # "Server 10" isn't in "Server 1"
        if rank in RANK_KEYS and (server is not None and (server in servers or "*" in servers) or "*" in servers):
            found = _higher(found, rank)
    if someone.administrator and ranks.get("discord_administrators") in ("moderator", "admin"):
        found = _higher(found, str(ranks["discord_administrators"]))
    return found


def in_game(config: DiscordConfig, rank: str) -> str:
    """What a rank is when a command runs from Discord: faculty of the bot acts as faculty, unless the file keeps them to admin."""
    return str(config.section("ranks").get("faculty_in_game") or "faculty") if rank == "faculty" else rank


def game_rank(config: DiscordConfig, someone: Someone, server: str | None = None) -> str:
    return in_game(config, rank_of(config, someone, server))


def highest_rank(config: DiscordConfig, someone: Someone, servers: Iterable[Any]) -> str:
    """Their rank at its highest, on any of these servers: for what isn't about one server, as someone scoped to one is still staff."""
    found = rank_of(config, someone)
    for server in servers:
        found = _higher(found, rank_of(config, someone, str(server)))
    return found


# What reaches further than the server it's run on, so it takes the rank someone has on all of them. A kick with a time does too.
REACHES_EVERY_SERVER = {"ban", "banall", "unban", "promote", "demote", "admin", "banname", "unbanname", "banword", "unbanword", "changebanword", "pardonplayer", "confirm", "deny"}


FOREVER_WORDS = {"perm", "permanent", "forever"}  # what makes a kick a ban for good ("never" starts a reason)


def times_a_kick(words: Iterable[str]) -> bool:
    """Whether a kick's words could give it a time, or make it a ban for good: then it reaches every server, and takes
    the rank held on all of them. Any word that reads as one counts (a name can have spaces in it, so which word is
    the time isn't known before the router reads the line): refusing a one-server moderator a plain kick whose reason
    says "30m" is the safe way to be wrong. Read from the time box alone, a time typed in the player box, or in the
    console, reached every server on a one-server rank (audit 2026-11, 6.8)."""
    from ..core.text import TIME

    return any(TIME.fullmatch(word.lower()) or word.lower() in FOREVER_WORDS for word in words)


def rank_to_run(config: DiscordConfig, someone: Someone, servers: Iterable[Any], server: str | None, command: str, *, timed: bool = False) -> str:
    """Their rank, for this command on this server. `timed`: a kick with a time, or a strike whose next step removes
    them or bans (the host's call, 6 November), which reach every server."""
    return lowest_rank(config, someone, servers) if command in REACHES_EVERY_SERVER or (command in ("kick", "strike") and timed) else game_rank(config, someone, server)


def lowest_rank(config: DiscordConfig, someone: Someone, servers: Iterable[Any]) -> str:
    """Their rank at its lowest, on any of these servers: for what reaches them all, as a rank on one server isn't a rank on the rest. Faculty's holds everywhere, and rank_of says so."""
    names = [str(server) for server in servers]
    if not names:
        return in_game(config, rank_of(config, someone))
    return in_game(config, min((rank_of(config, someone, name) for name in names), key=ROLES.index))


def can_act(rank: str) -> bool:
    """A probationary moderator's rank counts as moderator's for looking, but what they do is a request."""
    return ROLES.index(rank) >= ROLES.index(PROBATION)
