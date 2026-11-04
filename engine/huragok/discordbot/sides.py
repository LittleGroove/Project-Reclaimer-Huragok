"""Sides (0.6.1): a player's own team, Red or Blue, and Huragok putting them on it (the host's calls, 3 November 2026).

Members and up choose one: `!side red` in game, `/side` here (for a linked member, as their linked player). Choosing
switches it on for them; `!togglesides` (`/togglesides`) switches it off or on again, for them alone.

As a round of a team game is being played (the game starts, someone joins), each player with a side on, who's on the
other team, is moved there with the console's `team` command, with the checks !respawn makes so a move can't end the
game: a round being played, a team game, the teams as the server has them now, and never a team's last player (even a
moment with a team empty ends the game). Two who'd each rather be on the other's team swap, however even the teams
are, the move from the bigger team first; with one player on each team, they can't. Anyone else moves only while
the team they join has at most [sides] most_apart (2) more players than the one they leave, whoever joined first
first. A move that can't be made now is tried again as someone else joins, until they're on their side. Red against
Blue (the stats, by season) counts the games each colour wins.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..core.background import background
from ..core.commandsfile import ROLES
from ..servers.leaderboards import ROLE_MODES
from ..servers.state import Player

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

SCHEMA_STEP = """
    CREATE TABLE discord_sides (
        player_id TEXT PRIMARY KEY, side TEXT NOT NULL, forced INTEGER NOT NULL DEFAULT 1, time REAL NOT NULL);
    """
SIDES = ("red", "blue")
OTHER = {"red": "blue", "blue": "red"}
LOWEST = "member"  # the rank that chooses a side
TEAMS_TIMEOUT = 5.0
LOBBY = {"no_game", "voting"}  # before a game: moved there too, where nothing can end a game (the host's call, 3 November 2026)
MOVE_SECONDS = 6.0  # how long a move has to show in the teams, as !respawn waits for its answer
MOVE_LOOK_SECONDS = 0.5
MOVE_GAP_SECONDS = 2.5  # between one move and the next: the game waits about 2 s between a player's team changes (the host's call 22)
MOST_MOVES = 16  # a placement's moves at most: everyone it could ever need


def takes(part: DiscordPart) -> bool:
    return bool(part.file.section("sides").get("enabled")) and part.file.mode != "off"


def choose(part: DiscordPart, player_id: str, side: str) -> None:
    """Their side, and being put on it switched on; "" or "off": neither."""
    player_id = player_id.lower()
    part.roles_due = True  # a side's role follows (rolesync.py)
    if side in SIDES:
        part.ctx.db.execute("INSERT INTO discord_sides (player_id, side, forced, time) VALUES (?, ?, 1, ?) ON CONFLICT "
                            "(player_id) DO UPDATE SET side = excluded.side, forced = 1, time = excluded.time",
                            (player_id, side, time.time()))
    else:
        part.ctx.db.execute("DELETE FROM discord_sides WHERE player_id = ?", (player_id,))


def locked_to(part: DiscordPart, player_id: str) -> str:
    """The side they're held to until the season ends ([sides] lock_for_season: chosen while it runs), or ""."""
    from ..servers.leaderboards import current_season

    if not part.file.section("sides").get("lock_for_season"):
        return ""
    season = current_season(part.ctx.db)
    row = side_of(part, player_id)
    if season is None or row is None or float(row["time"]) < float(season["started"]):
        return ""
    return str(row["side"])


def held(part: DiscordPart, player_id: str) -> str:
    """The side they're forced onto until the season ends: [sides] lock_for_season with force_for_season (the host's
    call, 3 November 2026): !togglesides can't switch it off, and a switch of their own is put back. Without
    lock_for_season, nobody's held, whatever force_for_season says. "" for nobody."""
    return locked_to(part, player_id) if part.file.section("sides").get("force_for_season") else ""


def new_game(part: DiscordPart, server: str) -> None:
    """A game over or not begun: who switched team themselves in it is forgotten."""
    part.__dict__.setdefault("side_seen", {}).pop(server, None)
    part.__dict__.setdefault("side_switched", {}).pop(server, None)


def season_ended(part: DiscordPart) -> int:
    """A season's over: with [sides] reset_at_season_end, everyone chooses again. How many sides went."""
    if not part.file.section("sides").get("reset_at_season_end"):
        return 0
    gone = part.ctx.db.execute("DELETE FROM discord_sides").rowcount
    part.roles_due = True
    if gone:
        log.info("The season's over: %d side%s cleared, to be chosen again", gone, "" if gone == 1 else "s")
    return gone


def toggle(part: DiscordPart, player_id: str) -> bool | None:
    """Being put on their side, switched the other way: whether it's on now; None if they haven't chosen one."""
    row = side_of(part, player_id)
    if row is None:
        return None
    part.ctx.db.execute("UPDATE discord_sides SET forced = ? WHERE player_id = ?", (0 if row["forced"] else 1, player_id.lower()))
    return not bool(row["forced"])


def side_of(part: DiscordPart, player_id: str) -> Any:
    rows = part.ctx.db.query("SELECT * FROM discord_sides WHERE player_id = ?", (player_id.lower(),))
    return rows[0] if rows else None


def may_choose(part: DiscordPart, player_id: str) -> bool:
    return ROLES.index(part.ctx.highest_role(Player(0, "", player_id.lower()))) >= ROLES.index(LOWEST)


def moves(teams: dict[str, str], wanted: list[tuple[str, str]], most_apart: int, *, lobby: bool = False) -> list[tuple[str, str]]:
    """(player ID, the team to move them to), in the order to send them. `teams`: everyone on now, by team; `wanted`:
    (player ID, their side), whoever joined first first. No move leaves a team empty, even for a moment, in the
    `lobby` too: teams lock at the countdown, and a swap's second half refused there started a game with a team empty
    (second audit, 8.8; the host's call 7, "keep the team balanced")."""
    least = 2
    count = {side: sum(1 for team in teams.values() if team == side) for side in SIDES}
    to = {side: [player for player, chose in wanted if chose == side and teams.get(player) == OTHER[side]] for side in SIDES}
    found: list[tuple[str, str]] = []
    while to["red"] and to["blue"]:  # a swap leaves the numbers as they were: the move from the bigger team first,
        # which keeps the teams within one of each other between its two moves whenever any order can (the host's call 22)
        first = "red" if count["blue"] >= count["red"] else "blue"  # the side the first mover joins
        if count[OTHER[first]] < least:
            break  # one each: either move first would empty a team
        found += [(to[first].pop(0), first), (to[OTHER[first]].pop(0), OTHER[first])]
    for side in SIDES:
        for player in to[side]:
            leaving = OTHER[side]
            if count[leaving] >= least and (count[side] + 1) - (count[leaving] - 1) <= most_apart:
                found.append((player, side))
                count[side], count[leaving] = count[side] + 1, count[leaving] - 1
    return found


async def _teams(client: Any) -> dict[str, str] | None:
    """{player ID: team} as the server has them now, or None when it doesn't say."""
    try:
        reply = await client.command("players", timeout=TEAMS_TIMEOUT)  # as !respawn reads them
    except Exception as error:  # noqa: BLE001 - nobody's moved; it's tried again as someone joins or leaves
        log.debug("couldn't read the teams: %s", error)
        return None
    rows = (reply.data or {}).get("players") if isinstance(reply.data, dict) else None
    if not isinstance(rows, list):
        return None
    return {str(row["id"]).lower(): str(row.get("team") or "").lower() for row in rows if isinstance(row, dict) and row.get("id")}


async def _move(part: DiscordPart, server: str, client: Any, player_id: str, team: str, lobby: bool = False) -> bool:
    """One move, sent and seen through: True once the server lists them on that team. A move's answer only says it's
    on its way; what came of it shows in the teams (audit 0.6.1, 4.1: a swap whose first half didn't happen still sent
    the second, and a team could be left empty)."""
    from ..games.plays import console_refusal

    state = part.ctx.servers.get(server)
    if state is None or not (state.phase == "playing" or state.phase in LOBBY) or (state.phase in LOBBY) != lobby:
        return False  # looked at again before each move (4.4), and in the phase its plan was made for (8.8)
    if console_refusal(part.ctx.plays_kind(server), f"team {player_id} {team}", state.version):  # an ODST console that can't (4.9)
        return False
    mine = time.monotonic() + TEAMS_TIMEOUT + 2 * MOVE_SECONDS  # !respawn waits meanwhile, and the other way round (4.3)
    state.team_moving_until = max(state.team_moving_until, mine)  # the whole move and its watch, not only its start (8.7)
    try:
        reply = await client.command("team", player_id, team, by="Huragok")
        if not getattr(reply, "ok", True):
            log.debug("[%s] a move to %s was refused: %s", server, team, getattr(reply, "text", ""))
            _record(part, server, player_id, team, "refused", str(getattr(reply, "text", "")))
            return False
        deadline = time.monotonic() + MOVE_SECONDS
        while time.monotonic() < deadline:
            teams = await _teams(client)
            if teams is not None and player_id not in teams:
                _record(part, server, player_id, team, "gone", "")
                return False  # gone
            if teams is not None and teams.get(player_id) == team:
                part.__dict__.setdefault("side_seen", {}).setdefault(server, {})[player_id] = team  # ours: not a switch of theirs
                await _tell_moved(part, server, player_id, team)
                _record(part, server, player_id, team, "landed", "")
                return True
            await asyncio.sleep(MOVE_LOOK_SECONDS)
        _record(part, server, player_id, team, "didn't land", "")
        return False
    except Exception as error:  # noqa: BLE001 - this one stays where it is; tried again as someone joins or leaves
        log.debug("[%s] couldn't move a player to %s: %s", server, team, error)
        return False
    finally:
        if state.team_moving_until == mine:  # its own mark: a respawn's set meanwhile stays (8.7)
            state.team_moving_until = 0.0


def _record(part: DiscordPart, server: str, player_id: str, team: str, outcome: str, text: str) -> None:
    """A move and how it went, as an event, for the temp-discord record: sides gets tried on the live servers, and the
    game's own refusals are what's to learn (the host, 4 November 2026)."""
    state = part.ctx.servers.get(server)
    player = state.player_by_id(player_id) if state is not None else None
    part.ctx.emit("side_move", server, player_id=player_id, player=player.name if player else "", team=team,
                  outcome=outcome, text=text[:200], phase=state.phase if state is not None else "")


async def _tell_moved(part: DiscordPart, server: str, player_id: str, team: str) -> None:
    """Told, privately, once they're on their side; a move that didn't happen says nothing, so nobody's chat fills
    with tries (the host's call, 3 November 2026)."""
    from ..lore import callout

    state = part.ctx.servers.get(server)
    player = state.player_by_id(player_id) if state is not None else None
    if player is None:
        return
    prefix = getattr(part.ctx.permissions, "prefix", "!") or "!"
    try:
        await part.ctx.tell(server, player, callout("sided", f"You're on {team.title()}, the side you chose: {prefix}togglesides stops it."))
    except Exception as error:  # noqa: BLE001 - they're moved: only the word of it is lost
        log.debug("[%s] couldn't tell %s they were moved: %s", server, player.name, error)


def _wanted(part: DiscordPart, server: str) -> list[tuple[str, str]]:
    """(player ID, their side) of everyone on with a side on, members and up, whoever joined first first (4.6: the
    list Huragok keeps is in the server's order after each refresh)."""
    state = part.ctx.servers.get(server)
    if state is None:
        return []
    chosen = {str(row["player_id"]): str(row["side"]) for row in part.ctx.db.query(
        "SELECT player_id, side, forced FROM discord_sides WHERE side IN ('red', 'blue')")
        if row["forced"] or held(part, str(row["player_id"]))}
    on = sorted((player for player in state.players.values() if player.id.lower() in chosen), key=lambda player: player.joined)
    return [(player.id.lower(), chosen[player.id.lower()]) for player in on
            if ROLES.index(part.ctx.role_of(server, player)) >= ROLES.index(LOWEST)]


async def place(part: DiscordPart, server: str) -> int:
    """Everyone on this server with a side on, onto it where it's safe and the teams allow: one move at a time, each
    seen through and the next worked out from the teams as they are then. How many moves were made. One placement
    a server at a time (audit 0.6.1, 4.2: joins close together sent the same moves twice); one asked for meanwhile
    runs after it."""
    if not takes(part):
        return 0
    if server in part.placing:
        part.placing[server] = True
        return 0
    part.placing[server] = False
    made = 0
    try:
        made = await _place(part, server)
    finally:
        again = part.placing.pop(server, False)
    if made:
        log.info("[%s] %d player%s moved to the side they chose", server, made, "" if made == 1 else "s")
    if again:
        soon(part, server)
    return made


async def _place(part: DiscordPart, server: str) -> int:
    made = 0
    for _ in range(2 * MOST_MOVES):  # it ends: each pass makes a move or stops
        state = part.ctx.servers.get(server)
        if state is None or not (state.phase == "playing" or state.phase in LOBBY):  # as !respawn: a round; or the lobby
            return made
        if (state.mode or "").strip().casefold() in ROLE_MODES:  # a team is what you are there, not a side (10)
            return made
        lobby = state.phase in LOBBY
        if time.monotonic() < state.team_moving_until:  # a !respawn under way: after it
            soon(part, server, delay=MOVE_SECONDS)
            return made
        wanted = _wanted(part, server)
        if not wanted:
            return made
        client = part.ctx.rcon(server)
        if not client.connected:
            return made
        teams = await _teams(client)
        if teams is None or not any(team in SIDES for team in teams.values()):
            return made  # not a team game
        if any(team and team not in SIDES for team in teams.values()):
            return made  # a third team: not Red against Blue (the host's call, 3 November 2026)
        state = part.ctx.servers.get(server)
        if state is None or not (state.phase == "playing" or state.phase in LOBBY):
            return made
        lobby = state.phase in LOBBY  # as the teams were read: a lobby's plan isn't sent in the round (8.8)
        wanted = _not_switched(part, server, teams, wanted)
        from .standings import counted

        most_apart = counted(part.file.section("sides").get("most_apart", 2), 2, 0, 64)  # an inf or nan stopped them all (8.10)
        plan = moves(teams, wanted, most_apart, lobby=lobby)
        if not plan:
            return made
        first = plan[0]
        if made:
            await asyncio.sleep(MOVE_GAP_SECONDS)  # the game's own wait between changes
        if not await _move(part, server, client, *first, lobby=lobby):
            return made
        made += 1
        if len(plan) > 1 and plan[1][1] != first[1]:  # the other half of a swap, whatever the balance: never emptying a team
            partner, side = plan[1]
            teams = await _teams(client)
            if teams is not None and teams.get(partner) == OTHER[side] and sum(1 for team in teams.values() if team == OTHER[side]) >= 2:
                await asyncio.sleep(MOVE_GAP_SECONDS)
                if await _move(part, server, client, partner, side, lobby=lobby):
                    made += 1
    return made


def _not_switched(part: DiscordPart, server: str, teams: dict[str, str], wanted: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Those who haven't switched team themselves this game: one seen on their side and now on the other did, and
    isn't moved back until the next game, unless their side's forced (held). Each moved back at every join was a
    respawn (the host's call, 3 November 2026)."""
    seen = part.__dict__.setdefault("side_seen", {}).setdefault(server, {})
    switched = part.__dict__.setdefault("side_switched", {}).setdefault(server, set())
    for player_id, side in wanted:
        if seen.get(player_id) == side and teams.get(player_id) == OTHER[side]:
            switched.add(player_id)
    for player_id in [one for one in seen if one not in teams]:  # gone: a rejoin on the other team isn't a switch (8.4)
        del seen[player_id]
    for player_id, team in teams.items():
        seen[player_id] = team
    return [(player_id, side) for player_id, side in wanted if player_id not in switched or held(part, player_id)]


def soon(part: DiscordPart, server: str, *, delay: float = 0.0) -> None:
    async def later() -> None:
        if delay:
            await asyncio.sleep(delay)
        await place(part, server)

    background(later(), "putting players on the side they chose", log)


__all__ = ["LOBBY", "SCHEMA_STEP", "SIDES", "choose", "held", "new_game", "locked_to", "season_ended", "may_choose", "moves", "place", "side_of", "soon", "takes", "toggle"]
