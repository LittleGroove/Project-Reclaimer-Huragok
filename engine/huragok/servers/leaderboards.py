"""leaderboards: every board and every player's card, as queries over what stats.py kept (0.6.1).

Every leaderboard is a query (the host's call, 29 October 2026): a counted thing, over a period, somewhere. The thing is
one of STATS ("kills", "win_rate"…), a medal ("medal:overkill") or a weapon ("weapon:sniper_rifle"); the period one of
PERIODS; somewhere a server, a map or a mode, or everywhere. A new board is a new query, never new counting: what's on it
is only ever what the servers reported (stats.py). Rates (K/D, win rate…) need MIN_GAMES so one lucky game doesn't top
them. Who's on a board is by player ID, so a name change carries their record with it; the name shown is the one they
last played under, which the Discord bot turns into their name there if they've linked (links.py).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PERIODS = {"all": None, "season": None, "month": 30 * 86400, "week": 7 * 86400, "day": 86400}  # a season: since the one running began
MIN_GAMES = 5  # before a rate counts
MORE_TIED = 25  # how many more a board reads for those tied with its last place
RARITY_SECONDS = 3600.0  # how long how often each medal's been earned is kept before it's counted again
ROLE_MODES = ("infection", "juggernaut")  # modes where a team is what you are, not a side: never Red against Blue
ROWS = 10


@dataclass(frozen=True)
class Stat:
    title: str
    sql: str  # over stats_players p (and a joined table t, for medals and weapons)
    rate: bool = False  # needs MIN_GAMES
    shown: str = "{:,.0f}"


STATS: dict[str, Stat] = {
    "kills": Stat("Kills", "SUM(p.kills)"),
    "deaths": Stat("Deaths", "SUM(p.deaths)"),
    "assists": Stat("Assists", "SUM(p.assists)"),
    "headshots": Stat("Headshots", "SUM(p.headshots)"),
    "wins": Stat("Wins", "SUM(p.result = 'win')"),
    "games": Stat("Games", "COUNT(*)"),
    "playtime": Stat("Time played", "SUM(p.seconds)", shown="hours"),
    "best_streak": Stat("Best streak", "MAX(p.best_streak)"),
    "score": Stat("Score", "SUM(p.score)"),
    "kd": Stat("K/D", "CAST(SUM(p.kills) AS REAL) / MAX(1, SUM(p.deaths))", rate=True, shown="{:.2f}"),
    "kda": Stat("KDA", "CAST(SUM(p.kills) + SUM(p.assists) AS REAL) / MAX(1, SUM(p.deaths))", rate=True, shown="{:.2f}"),
    "win_rate": Stat("Win rate", "CAST(SUM(p.result = 'win') AS REAL) / MAX(1, SUM(p.result IN ('win', 'loss', 'tie')))",
                     rate=True, shown="percent"),
    "kills_per_game": Stat("Kills a game", "CAST(SUM(p.kills) AS REAL) / COUNT(*)", rate=True, shown="{:.1f}"),
    "betrayals": Stat("Friendly fire", "SUM(p.betrayals)"),
    "suicides": Stat("Suicides", "SUM(p.suicides)"),
}
NAME = re.compile(r"[a-z0-9_]{1,64}")
# Halo 3's ladders, lowest first: a card shows the best of each a player has earned.
MULTI_KILLS = ("double_kill", "triple_kill", "overkill", "killtacular", "killtrocity", "killimanjaro", "killtastrophe",
               "killpocalypse", "killionaire")
SPREES = ("killing_spree", "killing_frenzy", "running_riot", "rampage", "untouchable", "invincible", "kills_in_a_row_35",
          "kills_in_a_row_40")
# What nearly every game hands out: not what a card's medals are for.
EVERYDAY = {"kill", "assist", "headshot", "vehicle_kill", "vehicle_destroyed", "vehicle_destroy_assist", "avenger",
            "revenge", "comeback_kill", "reload_this", "protector", "close_call", "snapshot", "first_strike",
            "last_strike", "flag_taken", "flag_returned", *MULTI_KILLS, *SPREES}


def words_of(name: str) -> str:
    """A medal or weapon as people say it: "kill_from_the_grave" is "Kill from the grave"."""
    text = name.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else name


@dataclass(frozen=True)
class Board:
    stat: str = "kills"
    period: str = "all"
    server: str = ""
    map: str = ""
    mode: str = ""
    rows: int = ROWS

    @property
    def title(self) -> str:
        kind, _, name = self.stat.partition(":")
        what = (f"{words_of(name)} medals" if kind == "medal" else f"{words_of(name)} kills" if kind == "weapon"
                else STATS[self.stat].title if self.stat in STATS else self.stat)
        when = {"all": "", "season": " · this season", "month": " · last 30 days", "week": " · last 7 days",
                "day": " · last 24 hours"}.get(self.period, "")  # the last 24 hours, not the host's day (audit 0.6.1, 2.7)
        where = " · ".join(part for part in (self.server, self.map, self.mode) if part)
        return what + when + (f" · {where}" if where else "")


@dataclass
class Row:
    place: int
    person: int
    player_id: str
    name: str
    value: float
    games: int
    shown: str = ""


def known(stat: str) -> bool:
    kind, _, name = stat.partition(":")
    return stat in STATS or (kind in ("medal", "weapon") and bool(NAME.fullmatch(name)))


def last_season(db: Any) -> Any:
    """The season that ended last, or None: what a season board shows between seasons (the host's call, 3 November 2026)."""
    rows = db.query("SELECT * FROM stats_seasons WHERE ended IS NOT NULL ORDER BY number DESC LIMIT 1")
    return rows[0] if rows else None


def season_numbered(db: Any, number: int) -> Any:
    rows = db.query("SELECT * FROM stats_seasons WHERE number = ?", (int(number),))
    return rows[0] if rows else None


def season_shown(db: Any) -> Any:
    """The season a season board is of: the one running, else the one just ended; None before the first."""
    return current_season(db) or last_season(db)


def season_named(number: Any, name: Any) -> str:
    """"Season 3", or "Season 3 (Winter)" when it has a name of its own: "Season 1 (Season 1)" said it twice (audit 0.6.1, 5.10)."""
    named = str(name or "").strip()
    return f"Season {number}" + (f" ({named})" if named and named.casefold() != f"season {number}".casefold() else "")


def current_season(db: Any) -> Any:
    """The season running now (faculty started it with !newseason), or None."""
    rows = db.query("SELECT * FROM stats_seasons WHERE ended IS NULL ORDER BY number DESC LIMIT 1")
    return rows[0] if rows else None


def since(db: Any, period: str, now: float) -> float | None:
    """When a period starts: None for all of it. With no season running, a season board has nobody on it: it showed
    all time, titled "this season", and the top roles went to the all-time leaders as a season ended (audit 0.6.1, 2.2)."""
    if period == "season":
        season = current_season(db)
        return float(season["started"]) if season is not None else now + 1.0
    seconds = PERIODS.get(period)
    return now - seconds if seconds else None


def red_blue(db: Any, start: float | None = None, end: float | None = None) -> dict[str, int]:
    """Red against Blue: team games won by each, and tied, between two times (the host's call, 29 October 2026). Only
    Red against Blue games: none with a third team, nor a mode where a team is what you are (Infection, Juggernaut:
    their armour colour, the host's call, 3 November 2026)."""
    modes = ", ".join("?" for _ in ROLE_MODES)
    row = db.query("SELECT SUM(winner = '0') AS red, SUM(winner = '1') AS blue, SUM(winner = 'tie') AS ties, COUNT(*) AS games "
                   f"FROM stats_games g WHERE team_game = 1 AND winner != '' AND ended >= ? AND ended < ? AND casefold(mode) NOT IN ({modes}) "
                   "AND NOT EXISTS (SELECT 1 FROM stats_players p WHERE p.game = g.number AND p.team > 1)",
                   (start or 0.0, end if end is not None else 1e18, *ROLE_MODES))[0]
    return {key: int(row[key] or 0) for key in ("red", "blue", "ties", "games")}


def _where(board: Board, now: float, db: Any = None) -> tuple[str, list[Any]]:
    clauses, values = [], []
    start = since(db, board.period, now) if db is not None else (now - PERIODS[board.period] if PERIODS.get(board.period) else None)
    if start is not None:
        clauses.append("p.ended >= ?")
        values.append(start)
    for column, wanted in (("g.server", board.server), ("g.map", board.map), ("g.mode", board.mode)):
        if wanted:
            clauses.append(f"casefold({column}) = casefold(?)" if column != "g.map" else
                           "(casefold(g.map) = casefold(?) OR casefold(g.map_title) = casefold(?))")
            values += [wanted] if column != "g.map" else [wanted, wanted]
    return (" AND ".join(clauses) or "1"), values


def shown_value(stat: str, value: float) -> str:
    style = STATS[stat].shown if stat in STATS else "{:,.0f}"
    if style == "hours":
        hours = value / 3600
        return f"{hours:,.1f} h" if hours < 100 else f"{hours:,.0f} h"
    if style == "percent":
        return f"{value * 100:.0f}%"
    return style.format(value)


def board(db: Any, wanted: Board, *, now: float | None = None, skip: set[str] | None = None,
          min_games: int = MIN_GAMES, between: tuple[float, float] | None = None, ties: bool = False) -> list[Row]:
    """The board's top rows, best first. `skip`: player IDs not to show (those who asked not to be on boards), whose
    places go to the next. `between`: games that ended in this window instead of the board's period (a season over).
    A tie shares the place: two first, then third (the host's call, 3 November 2026). `ties`: everyone tied with the
    last row comes too, past `rows` (a top role is worn by everyone at the top).

    Summed from the players' rows alone, grouped as they're read rather than through the person index, and the games
    joined only for a server, map or mode: on a year of a busy host's games an all-time board took 14.7 s that way, and
    0.84 s this way (audit 0.6.1, 2.1). Ties go to the fewer games (the more, on a rate: 50 games without a death
    beat 5), then to whoever's been in the stats longest, the same at every look (2.8)."""
    if not known(wanted.stat):
        raise ValueError(f"no such stat: {wanted.stat}")
    now = time.time() if now is None else now
    if between is None and wanted.period == "season" and current_season(db) is None:  # between seasons: the last one's
        last = last_season(db)
        if last is None:
            return []
        between = (float(last["started"]), float(last["ended"]))
    if between is not None:
        where, values = _where(Board(wanted.stat, "all", wanted.server, wanted.map, wanted.mode, wanted.rows), now, db)
        where, values = f"{where} AND p.ended >= ? AND p.ended < ?", [*values, *between]
    else:
        where, values = _where(wanted, now, db)
    by_period = between is not None or since(db, wanted.period, now) is not None
    kind, _, name = wanted.stat.partition(":")
    join = "CROSS JOIN" if by_period else "JOIN"  # over a period, from its players' rows: a medal's every row ever was read (2.5)
    if kind == "medal":
        value, joined, values = "SUM(t.count)", f"{join} stats_medals t ON t.part = p.number AND t.medal = ?", [name, *values]
    elif kind == "weapon":
        value, joined, values = "SUM(t.kills)", f"{join} stats_weapons t ON t.part = p.number AND t.weapon = ?", [name, *values]
    else:
        value, joined = STATS[wanted.stat].sql, ""
    rate = wanted.stat in STATS and STATS[wanted.stat].rate
    games = "COUNT(DISTINCT p.game)" if joined else "COUNT(*)"
    limit = wanted.rows + len(skip or ()) + (MORE_TIED if ties else 0)
    counted = "SUM(p.result IN ('win', 'loss', 'tie'))" if wanted.stat == "win_rate" else "COUNT(*)"  # games it's a rate of (2.9)
    least = f" AND {counted} >= {max(1, min(10_000, int(min_games)))}" if rate else ""
    games_joined = "JOIN stats_games g ON g.number = p.game" if any((wanted.server, wanted.map, wanted.mode)) else ""
    order = "DESC" if rate else "ASC"
    sql = (
        f"SELECT s.person, people.player_id, s.value, s.games FROM ("
        f"SELECT p.person AS person, {value} AS value, {games} AS games FROM stats_players p {joined} {games_joined} "
        f"WHERE {where} GROUP BY +p.person HAVING value > 0{least} ORDER BY value DESC, games {order}, p.person LIMIT ?"
        f") s JOIN stats_people people ON people.number = s.person ORDER BY s.value DESC, s.games {order}, s.person")
    rows = db.query(sql, (*values, limit))
    while ties and len(rows) == limit and len(rows) > wanted.rows and _shown_as(wanted.stat, rows[-1]["value"]) == _shown_as(wanted.stat, rows[wanted.rows - 1]["value"]):
        limit *= 10  # still tied at the last read: everyone tied wears a top role (second audit, 7.5)
        rows = db.query(sql, (*values, limit))
    found: list[Row] = []
    for row in rows:
        if skip and str(row["player_id"]) in skip:
            continue
        value = float(row["value"])
        tied = bool(found) and shown_value(wanted.stat, value) == found[-1].shown  # a tie is what players see: 2.00 and 2.00
        if len(found) >= wanted.rows and not (ties and tied):
            break
        found.append(Row(found[-1].place if tied else len(found) + 1, int(row["person"]), str(row["player_id"]),
                         last_name(db, int(row["person"])), value, int(row["games"]), shown_value(wanted.stat, value)))
    return found


def _shown_as(stat: str, value: Any) -> str:
    return shown_value(stat, float(value or 0))


def last_name(db: Any, person: int) -> str:
    """The name they last played under: in the stats, or as Huragok last saw them join."""
    rows = db.query("SELECT name FROM stats_players WHERE person = ? ORDER BY ended DESC LIMIT 1", (person,))
    if rows:
        return str(rows[0]["name"])
    rows = db.query("SELECT last_name FROM players JOIN stats_people ON players.id = stats_people.player_id "
                    "WHERE stats_people.number = ?", (person,))
    return str(rows[0]["last_name"]) if rows else "?"


@dataclass
class Card:
    player_id: str
    name: str
    games: int = 0
    wins: int = 0
    losses: int = 0
    ties: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    headshots: int = 0
    best_streak: int = 0
    seconds: int = 0
    medals: list[tuple[str, int]] = field(default_factory=list)  # the most earned first, not counting EVERYDAY's
    best_multi_kill: tuple[str, int] | None = None  # the highest of MULTI_KILLS earned, and how often
    best_spree: tuple[str, int] | None = None
    weapon: tuple[str, int] | None = None  # most kills with
    nemesis: tuple[str, str, int] | None = None  # (their ID, their name, kills on this player)
    victim: tuple[str, str, int] | None = None
    official: Any = None  # the service's record, or None
    first: float = 0.0
    last: float = 0.0

    @property
    def kd(self) -> float:
        return self.kills / max(1, self.deaths)

    @property
    def win_rate(self) -> float:
        return self.wins / max(1, self.wins + self.losses + self.ties)


def card(db: Any, player_id: str, *, medals: int = 3) -> Card | None:
    """A player's record, all time: None if they've no game in the stats."""
    found = db.query("SELECT number FROM stats_people WHERE player_id IN (?, ?)", (player_id, player_id.lower()))
    if not found:
        return None
    person = int(found[0]["number"])
    totals = db.query(
        "SELECT COUNT(*) AS games, SUM(result = 'win') AS wins, SUM(result = 'loss') AS losses, SUM(result = 'tie') AS ties, "
        "SUM(kills) AS kills, SUM(deaths) AS deaths, SUM(assists) AS assists, SUM(headshots) AS headshots, "
        "MAX(best_streak) AS best_streak, SUM(seconds) AS seconds, MIN(ended) AS first, MAX(ended) AS last "
        "FROM stats_players WHERE person = ?", (person,))[0]
    if not totals["games"]:
        return None
    result = Card(player_id.lower(), last_name(db, person), **{key: int(totals[key] or 0) for key in (
        "games", "wins", "losses", "ties", "kills", "deaths", "assists", "headshots", "best_streak", "seconds")},
        first=float(totals["first"] or 0), last=float(totals["last"] or 0))
    earned = {str(row["medal"]): int(row["n"]) for row in db.query(
        "SELECT medal, SUM(count) AS n FROM stats_medals JOIN stats_players p ON p.number = part WHERE p.person = ? "
        "GROUP BY medal", (person,))}
    seen = rarity(db)  # the rarest first: a Killionaire over a thousand rocket kills (the host's call, 3 November 2026)
    result.medals = sorted(((medal, count) for medal, count in earned.items() if medal not in EVERYDAY),
                           key=lambda item: (seen.get(item[0], 0), -item[1], item[0]))[:medals]
    for ladder, attribute in ((MULTI_KILLS, "best_multi_kill"), (SPREES, "best_spree")):
        best = next((medal for medal in reversed(ladder) if earned.get(medal)), None)
        setattr(result, attribute, (best, earned[best]) if best else None)
    weapon = db.query("SELECT weapon, SUM(stats_weapons.kills) AS n FROM stats_weapons JOIN stats_players p ON p.number = part "
                      "WHERE p.person = ? GROUP BY weapon HAVING n > 0 ORDER BY n DESC LIMIT 1", (person,))
    result.weapon = (str(weapon[0]["weapon"]), int(weapon[0]["n"])) if weapon else None
    for column, attribute in (("deaths", "nemesis"), ("kills", "victim")):
        rows = db.query(f"SELECT other, SUM({column}) AS n FROM stats_versus WHERE person = ? GROUP BY other "
                        f"HAVING n > 0 ORDER BY n DESC LIMIT 1", (person,))
        if rows:
            other = int(rows[0]["other"])
            other_id = db.query("SELECT player_id FROM stats_people WHERE number = ?", (other,))[0]["player_id"]
            setattr(result, attribute, (str(other_id), last_name(db, other), int(rows[0]["n"])))
    official = db.query("SELECT * FROM stats_official WHERE person = ?", (person,))
    result.official = official[0] if official else None
    return result


def in_game_line(card: Card) -> str:
    """!stats in game: short, as game chat wraps."""
    line = (f"{card.name}: {card.games} game{'s' if card.games != 1 else ''}, {card.wins} won ({card.win_rate * 100:.0f}%), K/D {card.kd:.2f}, "
            f"best streak {card.best_streak}")
    if card.weapon:
        line += f", most kills with the {words_of(card.weapon[0]).lower()}"
    if card.official is not None and str(card.official["title"] or ""):
        line += f". Rank: {card.official['title']}"
    return line + "."



def game_summary(db: Any, game_id: str, top: int = 3) -> dict[str, Any] | None:
    """One game, for its Carnage Report: where, what, the score, the best of it, and every medal earned in it."""
    games = db.query("SELECT * FROM stats_games WHERE game_id = ?", (game_id,))
    if not games:
        return None
    game = games[0]
    people = ("SELECT people.player_id, p.name, p.team, p.place, p.score, p.kills, p.deaths, p.best_streak, p.number "
              "FROM stats_players p JOIN stats_people people ON people.number = p.person WHERE p.game = ?")
    everyone = [dict(row) for row in db.query(people + " ORDER BY p.place, p.score DESC, p.kills DESC", (game["number"],))]
    streak = max(everyone, key=lambda row: row["best_streak"], default=None)
    medals = [dict(row) for row in db.query(
        "SELECT people.player_id, p.name, m.medal, m.count FROM stats_medals m JOIN stats_players p ON p.number = m.part "
        "JOIN stats_people people ON people.number = p.person WHERE p.game = ?", (game["number"],))]
    return {"game": dict(game), "top": everyone[:top], "players": len(everyone),
            "best_streak": streak if streak and streak["best_streak"] > 0 else None, "medals": medals}


def _each(table: str, column: str) -> str:
    """Every value of an indexed column, a step through the index each: a few dozen medals in millions of rows read in a
    millisecond, where DISTINCT read every row at each keystroke (audit 0.6.1, 2.4)."""
    return (f"WITH RECURSIVE seen(name) AS (SELECT MIN({column}) FROM {table} UNION ALL SELECT (SELECT MIN({column}) "
            f"FROM {table} WHERE {column} > seen.name) FROM seen WHERE seen.name IS NOT NULL) SELECT name FROM seen")


def choices(db: Any, kind: str, typed: str = "", most: int = 25) -> list[str]:
    """What's been seen of a kind, for suggestions as someone types: medals, weapons, maps, modes or servers."""
    sql = {"medal": _each("stats_medals", "medal"),
           "weapon": _each("stats_weapons", "weapon"),
           "map": "SELECT DISTINCT map_title AS name FROM stats_games",
           "mode": "SELECT DISTINCT mode AS name FROM stats_games",
           "server": "SELECT DISTINCT server AS name FROM stats_games"}[kind]
    names = sorted(str(row["name"]) for row in db.query(sql) if row["name"])
    wanted = typed.casefold().replace(" ", "_" if kind in ("medal", "weapon") else " ")
    return [name for name in names if wanted in name.casefold()][:most]


_RARITY: dict[str, Any] = {"at": float("-inf"), "seen": {}}


def rarity(db: Any = None) -> dict[str, int]:
    """How often each medal's been earned on these servers, as last counted (count_rarity, the stats part's hourly
    job): the fewer, the rarer. Never counted here: that read every medal row inside a card, 16 s on a busy year
    (second audit, 7.4). {} until it's first counted."""
    return _RARITY["seen"]


def _medal_counts(db: Any) -> dict[str, int]:
    return {str(row["medal"]): int(row["n"] or 0) for row in db.query("SELECT medal, SUM(count) AS n FROM stats_medals GROUP BY medal")}


async def count_rarity(db: Any) -> dict[str, int]:
    """How often each medal's been earned, counted in a thread (read_soon), and kept for rarity()."""
    _RARITY["seen"] = await read_soon(db, _medal_counts)
    _RARITY["at"] = time.monotonic()
    return _RARITY["seen"]


class Reading:
    """Huragok's records, read-only, on a connection of its own: for a thread, while the event loop goes on."""

    def __init__(self, path: Path) -> None:
        from urllib.request import pathname2url

        from ..core.storage import _casefold

        self.db = sqlite3.connect(f"file:{pathname2url(str(path))}?mode=ro", uri=True, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("casefold", 1, _casefold, deterministic=True)

    def query(self, sql: str, params: Any = ()) -> list[Any]:
        return self.db.execute(sql, params).fetchall()

    def close(self) -> None:
        self.db.close()


async def read_soon(db: Any, reader: Any, *args: Any, **options: Any) -> Any:
    """reader(db, ...), in a thread on a connection of its own: a board or a card over a busy year takes most of a
    second, which held up the servers' consoles, the stats relay and Discord while it ran (audit 0.6.1, 2.1). In the
    loop for a database with no file (tests)."""
    path = getattr(db, "path", None)
    if path is None:
        return reader(db, *args, **options)

    def read() -> Any:
        with closing(Reading(path)) as reading:
            return reader(reading, *args, **options)

    return await asyncio.to_thread(read)


async def board_soon(db: Any, wanted: Board, **options: Any) -> list[Row]:
    """board(), read in a thread (read_soon)."""
    return await read_soon(db, board, wanted, **options)


__all__ = ["EVERYDAY", "MULTI_KILLS", "SPREES", "Board", "Card", "MIN_GAMES", "current_season", "red_blue", "since", "PERIODS", "ROWS", "Row", "STATS", "Stat", "board", "card", "choices", "known",
           "count_rarity", "in_game_line", "last_name", "last_season", "rarity", "season_named", "season_numbered", "season_shown", "shown_value", "words_of", "Reading", "board_soon", "read_soon"]
