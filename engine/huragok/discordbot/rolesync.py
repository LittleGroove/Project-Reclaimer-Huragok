"""Roles Huragok gives in the Discord (0.6.1): rank roles, the leaderboards' top roles and the sides' roles. Only Huragok
gives them (the host's calls, 3 November 2026): one given or taken by hand is put back as it should be at the next look,
every [roles] check_minutes and soon after anything that changes who should wear one (a rank, a link, a side chosen, a
game counted, discord.toml). Each look reads every member (Server Members Intent, on the developer portal's Bot page)
and gives or takes what differs; it needs Manage Roles, and the bot's own role above the ones it gives.

  Rank roles ([roles] sync): a linked member wears the role [ranks] maps to the rank Huragok gives them: faculty
    ([roles] faculty), admin, moderator, probationary moderator, member; a player, none. A rank whose role isn't set wears
    the highest one below it that is. A role a mapping limits to some servers is worn only by someone with that rank on
    all of them: one server's own admin isn't every server's. Ranks change with !promote and !demote, in game or as
    /promote and /demote here; never by giving a role.
  Top roles ([roles] top_roles, top, top_count): a role for a leaderboard ("kills season" = "@Top Slayer"), worn by the
    linked players among its top top_count. An unlinked leader doesn't wear it: linking is how it's claimed.
  Side roles ([sides] roles, red_role, blue_role): a linked member who chose a side (!side, /side) wears its role, so
    a Discord can open a category to each side.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.commandsfile import ROLES
from ..servers import leaderboards
from ..servers.state import Player
from . import features, rest
from .config import RANK_KEYS
from .outbox import CHAT
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

PAGE = 1000  # Discord's most members a page
REASON = "Huragok"
MOST_CHANGES = 200  # a look asks for no more: the outbox keeps its room for everything else, and the next look, once
# these have gone, carries on (audit 0.6.1, 3.4: a first look on a big Discord filled the outbox, and other posts were dropped)
PAGE_TRIES = 3  # a member page Discord asks to wait for, tried again after the wait


@dataclass(frozen=True)
class RankRole:
    role: str  # its ID here
    rank: str
    servers: tuple[str, ...]  # "*": every server


def _role_id(part: DiscordPart, ref: Any) -> str:
    found = part.facts.find_role(ref) if part.facts is not None and str(ref or "").strip() else None
    return str(found["id"]) if found else ""


def on(part: DiscordPart, key: str) -> bool:
    return part.facts is not None and features.state_of(features.BY_KEY[key], part.file, part.facts)[0] == features.ON


def rank_roles(part: DiscordPart) -> list[RankRole]:
    """Every role [ranks] maps to a rank, with the servers it counts on, and [roles] faculty."""
    ranks = part.file.section("ranks")
    found = []
    for rank, key in RANK_KEYS.items():
        for ref in ranks.get(key) or []:
            if role := _role_id(part, ref):
                found.append(RankRole(role, rank, ("*",)))
    for block in ranks.get("scoped") or []:
        if isinstance(block, dict) and (role := _role_id(part, block.get("role"))):
            rank = str(block.get("rank", "")).strip().replace("_", "-")
            servers = block.get("servers") or []
            if rank in RANK_KEYS:
                found.append(RankRole(role, rank, tuple([servers] if isinstance(servers, str) else servers)))
    if role := _role_id(part, part.file.section("roles").get("faculty")):
        found.append(RankRole(role, "faculty", ("*",)))
    return found


def rank_on(part: DiscordPart, player_id: str, servers: tuple[str, ...]) -> str:
    """Their rank on every one of these servers: the lowest of them (a server's own admin isn't another's)."""
    names = list(part.ctx.servers) if "*" in servers else [name for name in servers if name in part.ctx.servers]
    player = Player(0, "", player_id.lower())
    if not names:  # its servers are gone (renamed, removed): it counts nowhere (audit 0.6.1, 3.10)
        return "player"
    return min((part.ctx.role_of(name, player) for name in names), key=ROLES.index)


def wanted_rank_role(part: DiscordPart, player_id: str, roles: list[RankRole], *, faculty: bool = False) -> str:
    """The one rank role this player wears: the highest one their rank reaches, where it counts. "" for none. `faculty`:
    faculty of this bot (/huragok claim), faculty whatever commands.toml says yet (links.faculty_claimed lists them)."""
    best: RankRole | None = None
    for one in roles:
        if faculty or ROLES.index(rank_on(part, player_id, one.servers)) >= ROLES.index(one.rank):
            if best is None or ROLES.index(one.rank) > ROLES.index(best.rank):
                best = one
    return best.role if best is not None else ""


def wanted_rank_roles(part: DiscordPart, player_id: str, roles: list[RankRole], *, faculty: bool = False) -> set[str]:
    """The rank roles this player wears: the highest one that counts on every server their rank reaches, and any role
    for some servers alone ranked above it there. One rank role each, as Discord's own order gives the lower ones'
    (the host's call, 3 November 2026), holds within a scope; a server's own admin who's everyone's moderator wore only
    that server's role, and was nobody everywhere else (second audit, 8.1)."""
    everywhere = [one for one in roles if "*" in one.servers]
    best = wanted_rank_role(part, player_id, everywhere, faculty=faculty)
    floor = max((ROLES.index(one.rank) for one in everywhere if one.role == best), default=-1)
    found = {best} if best else set()
    for one in roles:
        if "*" not in one.servers and ROLES.index(one.rank) > floor and (
                faculty or ROLES.index(rank_on(part, player_id, one.servers)) >= ROLES.index(one.rank)):
            found.add(one.role)
    return found


async def top_holders(part: DiscordPart, links: dict[str, str]) -> dict[str, set[str]]:
    """Each top role: the members who should wear it (the linked players among its board's top places). The boards
    read in a thread (leaderboards.board_soon)."""
    from . import standings

    options = part.file.section("roles")
    count = standings.counted(options.get("top_count", 1), 1, 1, 25)
    member_of = {player: member for member, player in links.items()}
    found: dict[str, set[str]] = {}
    for entry, ref in (options.get("top") or {}).items():
        role, board = _role_id(part, ref), standings.board_wanted(entry)
        if not role or board is None:
            continue
        rows = await leaderboards.board_soon(part.ctx.db, leaderboards.Board(board.stat, board.period, rows=count), ties=True,
                                             min_games=standings._min_games(part))
        found.setdefault(role, set()).update(member_of[row.player_id] for row in rows if row.player_id in member_of)
    return found


def side_holders(part: DiscordPart, links: dict[str, str]) -> dict[str, set[str]]:
    """Each side's role: the linked members who chose that side."""
    options = part.file.section("sides")
    roles = {side: _role_id(part, options.get(f"{side}_role")) for side in ("red", "blue")}
    member_of = {player: member for member, player in links.items()}
    found: dict[str, set[str]] = {role: set() for role in roles.values() if role}
    for row in part.ctx.db.query("SELECT player_id, side FROM discord_sides"):
        role = roles.get(str(row["side"]))
        if role and str(row["player_id"]) in member_of:
            found[role].add(member_of[str(row["player_id"])])
    return found


def _change(part: DiscordPart, member: str, role: str, give: bool, why: str) -> None:
    config = part.file

    async def send() -> Any:
        return await to_thread(rest.send, "PUT" if give else "DELETE",
                               f"{rest.API}/guilds/{config.guild}/members/{member}/roles/{role}",
                               token=config.token, reason=f"{REASON}: {why}")

    def refused(error: rest.DiscordError) -> bool:
        log.warning("Couldn't %s a role (%s): %s", "give" if give else "take", why, error)
        found = part.facts.find_role(role) if part.facts is not None else None  # for the temp-discord record: no user IDs
        part.ctx.emit("role_refused", None, role=str(found["name"]) if found else role, give=give, why=why, error=str(error)[:200])
        return True

    # The least urgent of all: whatever else is waiting goes first, and a role is what the outbox drops if it must.
    part.outbox.put(send, priority=CHAT, bucket=BUCKET, key=f"role:{member}:{role}",
                    what=f"{'giving' if give else 'taking'} a role ({why})", on_fail=refused)


BUCKET = "roles"


def changes_waiting(part: DiscordPart) -> bool:
    """Whether the last look's changes are still on their way: the next look waits for them."""
    return any(job.bucket == BUCKET for job in part.outbox.jobs)


async def all_members(part: DiscordPart) -> list[dict[str, Any]] | None:
    """Every member here, a page at a time; None when Discord won't say (no Server Members Intent)."""
    found: list[dict[str, Any]] = []
    after = "0"
    tries = 0
    while True:
        try:
            page = await to_thread(rest.call, "GET", f"/guilds/{part.file.guild}/members?limit={PAGE}&after={after}", part.file.token)
        except rest.SlowDown as error:  # waited for, a few times, rather than the look given up (audit 0.6.1, 3.11)
            tries += 1
            if tries > PAGE_TRIES:
                log.info("Couldn't read the members to put Huragok's roles right: %s", error)
                return None
            await asyncio.sleep(min(60.0, max(0.5, error.retry_after)))
            continue
        except rest.DiscordError as error:
            log.info("Couldn't read the members to put Huragok's roles right: %s", error)
            return None
        if not isinstance(page, list) or not page:
            return found
        found += page
        if len(page) < PAGE:
            return found
        after = str(page[-1].get("user", {}).get("id") or "0")


def _links(part: DiscordPart) -> dict[str, str]:
    return {str(row["member"]): str(row["player_id"]) for row in part.ctx.db.query("SELECT member, player_id FROM discord_links")}


def faculty_members(part: DiscordPart) -> list[str]:
    """Faculty of this bot, by Discord user ID ([ranks] faculty)."""
    return [str(one).strip() for one in part.file.section("ranks").get("faculty") or [] if str(one).strip()]


def wanted_roles(part: DiscordPart, links: dict[str, str], *, tops: dict[str, set[str]] | None = None) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Each role Huragok gives and who should wear it (and why, for Discord's audit log). `tops`: the top roles'
    wearers (top_holders), which take reading the boards."""
    ranks = rank_roles(part) if on(part, "roles") else []
    managed: dict[str, set[str]] = {}  # role -> who should wear it
    why: dict[str, str] = {}
    if ranks:
        for one in ranks:
            managed.setdefault(one.role, set())
            why[one.role] = "their rank in Huragok"
        from .ranks import is_faculty

        for member, player in links.items():
            for role in wanted_rank_roles(part, player, ranks, faculty=is_faculty(part.file, member)):
                managed[role].add(member)
        for member in faculty_members(part):  # faculty here by their user ID, linked or not
            for role in wanted_rank_roles(part, "", ranks, faculty=True) if member not in links else ():
                managed[role].add(member)
    for role, members in (tops or {}).items():
        managed.setdefault(role, set()).update(members)
        why.setdefault(role, "the top of its leaderboard")
    for role, members in (side_holders(part, links) if on(part, "side_roles") else {}).items():
        managed.setdefault(role, set()).update(members)
        why.setdefault(role, "the side they chose")
    return managed, why


def due_key(part: DiscordPart) -> tuple[Any, ...]:
    """Who should wear the rank and side roles, and which roles the top ones are, worked out without asking Discord: a
    look is due as soon as it differs from the last look's. A rank changed on a server's own admin list, by probation,
    in discord.toml or by a switch starts one as surely as !promote does (audit 0.6.1, 3.12)."""
    managed, _ = wanted_roles(part, _links(part))
    top = part.file.section("roles").get("top") or {}
    return (tuple(sorted((role, tuple(sorted(members))) for role, members in managed.items())), on(part, "top_roles"),
            tuple(sorted((str(key), str(value)) for key, value in top.items())) if isinstance(top, dict) else ())


def counted_roles(part: DiscordPart, member: str, roles: dict[str, str]) -> dict[str, str]:
    """A member's roles as their rank here reads them. With rank roles on, they're Huragok's: a rank role counts only
    on whoever Huragok gives it to, so one given by hand counts for nothing, even before the next look takes it off
    (audit 0.6.1, 3.2: a moderator who could give roles gave themselves the admin role and /promoted their own player).
    Any other role named like a rank role is left out too, as [ranks] can name roles by name."""
    if on(part, "roles"):
        ranks = rank_roles(part)
        names = {str(role.get("name", "")).lower() for one in ranks if part.facts is not None and (role := part.facts.find_role(one.role))}
        part.__dict__["rank_roles_seen"] = (ranks, names)  # for while Discord can't be read
    elif part.facts is None and part.file.section("roles").get("sync") and part.__dict__.get("rank_roles_seen"):
        ranks, names = part.rank_roles_seen  # switched on, Discord not read yet (a sign-in): as last seen, never open (8.2)
    else:
        return roles
    if not ranks:
        return roles
    from .ranks import is_faculty

    found = part.ctx.db.query("SELECT player_id FROM discord_links WHERE member = ?", (str(member),))
    faculty = is_faculty(part.file, member)
    wanted = wanted_rank_roles(part, str(found[0]["player_id"]) if found else "", ranks, faculty=faculty) if found or faculty else set()
    ids = {one.role for one in ranks}
    return {role: name for role, name in roles.items() if role in wanted or (role not in ids and str(name).lower() not in names)}


def role_ids(part: DiscordPart) -> set[str]:
    """Every role Huragok gives, now."""
    managed, _ = wanted_roles(part, {})
    top = part.file.section("roles").get("top") or {}
    return set(managed) | {role for ref in (top.values() if isinstance(top, dict) else []) if (role := _role_id(part, ref))}


async def put_roles_right(part: DiscordPart) -> int:
    """Every role Huragok gives, on whoever should wear it and on nobody else: how many changes were asked for."""
    links = _links(part)
    managed, why = wanted_roles(part, links, tops=await top_holders(part, links) if on(part, "top_roles") else None)
    if not managed:
        return 0
    members = await all_members(part)
    if members is None:
        return 0
    asked = 0
    taken: dict[str, list[str]] = {}  # a rank role -> who it came off, said in the audit feed
    for member in members:
        user = member.get("user") or {}
        member_id = str(user.get("id") or "")
        if not member_id or user.get("bot"):
            continue
        have = {str(role) for role in member.get("roles") or []}
        for role, wearers in managed.items():
            give = member_id in wearers
            if give == (role in have):
                continue
            if asked >= MOST_CHANGES:
                part.roles_due = True  # the rest at the next look, once these have gone
                log.info("Huragok's roles: %d changes asked for, and more at the next look", asked)
                _say_taken(part, taken)
                return asked
            _change(part, member_id, role, give, why[role] if give or member_id in links else "only Huragok gives this role")
            if not give and why.get(role) == "their rank in Huragok":
                taken.setdefault(role, []).append(str(member.get("nick") or user.get("global_name") or user.get("username") or member_id))
            asked += 1
    if asked:
        log.info("Huragok's roles: %d change%s asked for", asked, "" if asked == 1 else "s")
    _say_taken(part, taken)
    _record_look(part, asked, managed, taken, len(members))
    return asked


def _record_look(part: DiscordPart, asked: int, managed: dict[str, set[str]], taken: dict[str, list[str]], members: int) -> None:
    """A look, as an event, for the temp-discord record: how many changes, each role's wearers, what came off (role
    names and counts, never anyone's user ID). Role sync gets tried on the live Discord (the host, 4 November 2026)."""
    def name(role: str) -> str:
        found = part.facts.find_role(role) if part.facts is not None else None
        return str(found["name"]) if found else role

    part.ctx.emit("roles_look", None, members=members, changes=asked, wearers={name(role): len(who) for role, who in managed.items()},
                  taken={name(role): len(who) for role, who in taken.items()})


MOST_NAMED = 10  # members named a role, in the audit line; the rest counted


def _say_taken(part: DiscordPart, taken: dict[str, list[str]]) -> None:
    """Who a rank role came off, in the audit feed: the look goes on by itself, and this says what it did (the host's
    call, 3 November 2026: say who lost what, no waiting, no button). Their rank's Huragok's: linking, and a rank given
    with !promote or /promote, give it back."""
    for role, names in taken.items():
        found = part.facts.find_role(role) if part.facts is not None else None
        named = ", ".join(names[:MOST_NAMED]) + (f" and {len(names) - MOST_NAMED} more" if len(names) > MOST_NAMED else "")
        part.audit("link", f"Huragok took @{found['name'] if found else role} off {named}: their rank in Huragok isn't that"
                           " (not linked, or not that rank). A rank role comes with linking and the rank: !promote or /promote")


__all__ = ["RankRole", "all_members", "changes_waiting", "counted_roles", "due_key", "faculty_members", "put_roles_right",
           "rank_on", "rank_roles", "role_ids", "side_holders", "top_holders", "wanted_rank_role", "wanted_rank_roles", "wanted_roles"]
