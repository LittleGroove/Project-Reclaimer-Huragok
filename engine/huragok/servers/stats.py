"""stats: every finished game, as its server reported it, kept in Huragok's database: what players' stats and
leaderboards are counted from (0.6.1).

After each game a server sends its report to the stats service, which answers with each player's rank. statsrelay.py
stands in the middle (the host's call, 30 October 2026: it's ours as it leaves the machine), passes both on exactly as
sent, and hands them here. The report is the game's own record, by player ID: the map, mode and team scores, and for
each player their team, place, score and totals, headshots, best streak, time played, whether they quit, the game's own
medals ("double_kill": 2), their kills and deaths by weapon, and their kills and deaths against each other player.
What's taken out of it goes into these tables; the report itself isn't kept here:

  stats_games     one row a game: where, what, when, the team scores and who won
  stats_players   each player's part in each game, and how it went for them: win, loss, tie, or none
  stats_medals    each player's medals in each game
  stats_weapons   each player's kills, deaths, headshots, team kills and suicides by weapon in each game
  stats_versus    kills and deaths between two players, summed by month (nemesis, head to head)
  stats_official  each player's record on the stats service, as its last answer gave it: one game behind, as the
                  answer is the record from before the game just sent

Who won (the host's calls, 3 November 2026): in a team game, the team with the highest score, and a tie when two share
it; in free-for-all, first place only. A game with fewer than two sides in it counts for nobody ("none"). Only games
the server reports count: one the service would skip (a map's file name with capitals, fewer than two identified
players) isn't here, and neither is a player the server couldn't identify, as on the service.

statsrelay's copies are written, as tempdiscord.py always did, to logs/temp-discord/stats-reports/ (stand-ins for
player IDs, kept 30 days). As this starts it reads them back in (the backfill), so the stats begin with what's already
been played: each stand-in is turned back into the player ID of someone Huragok has seen join, with the key that made it.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.background import background
from ..core.events import Event
from ..core.part import Part, command, every, on
from ..core.text import TIME, describe_seconds, describe_time, seconds_of
from ..lore import SUPERINTENDENT
from .leaderboards import season_named
from .statsrelay import StatsCapture
from .tempdiscord import DAY_FILE, FOLDER, REPORTS, Scrubber, key_for

MATCHES = "/v1/matches"
GAME_ON = {"playing", "round_over", "between_rounds"}
MOST_KILLS = 50_000  # one game's, at most: a 60-minute Huge Battles game had 4,119
ASK_SECONDS = 5.0
KILLS_KEPT_DAYS = 14
KILL_SLACK = 3.0  # seconds either side of a report's start and end that its kills may fall in
WIN, LOSS, TIE, NONE = "win", "loss", "tie", "none"
FULL_ID = re.compile(r"[0-9a-f]{64}")
STAND_IN = re.compile(r"P-[0-9a-f]{10}")

# Numbers stand for the long keys (a game's 36-letter ID, a player's 64): a busy day is about 20,000 medal, weapon and
# versus rows, which with the keys written out came to 130 KB a game, over a gigabyte a year (the live servers'
# reports, 3 November 2026). stats_people holds each player's number; the rows that hang off one player's part in
# one game name it by its number, in tables kept by their key (WITHOUT ROWID).
SCHEMA = [
    """
    CREATE TABLE stats_people (number INTEGER PRIMARY KEY, player_id TEXT NOT NULL UNIQUE);
    CREATE TABLE stats_games (
        number INTEGER PRIMARY KEY, game_id TEXT NOT NULL UNIQUE, server TEXT NOT NULL, map TEXT NOT NULL,
        map_title TEXT NOT NULL, variant TEXT NOT NULL, mode TEXT NOT NULL, team_game INTEGER NOT NULL,
        ranked INTEGER NOT NULL, started REAL NOT NULL, ended REAL NOT NULL, red INTEGER, blue INTEGER,
        winner TEXT NOT NULL, players INTEGER NOT NULL, received REAL NOT NULL);
    CREATE INDEX stats_games_ended ON stats_games (ended);
    CREATE TABLE stats_players (
        number INTEGER PRIMARY KEY, game INTEGER NOT NULL, person INTEGER NOT NULL, name TEXT NOT NULL, team INTEGER,
        place INTEGER NOT NULL, score INTEGER NOT NULL, kills INTEGER NOT NULL, deaths INTEGER NOT NULL,
        assists INTEGER NOT NULL, betrayals INTEGER NOT NULL, suicides INTEGER NOT NULL, headshots INTEGER NOT NULL,
        best_streak INTEGER NOT NULL, seconds INTEGER NOT NULL, quit INTEGER NOT NULL, result TEXT NOT NULL,
        ended REAL NOT NULL, UNIQUE (game, person));
    CREATE INDEX stats_players_person ON stats_players (person, ended);
    CREATE INDEX stats_players_ended ON stats_players (ended);
    CREATE TABLE stats_medals (
        part INTEGER NOT NULL, medal TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY (part, medal)) WITHOUT ROWID;
    CREATE INDEX stats_medals_medal ON stats_medals (medal);
    CREATE TABLE stats_weapons (
        part INTEGER NOT NULL, weapon TEXT NOT NULL, kills INTEGER NOT NULL, deaths INTEGER NOT NULL,
        headshots INTEGER NOT NULL, betrayals INTEGER NOT NULL, suicides INTEGER NOT NULL,
        PRIMARY KEY (part, weapon)) WITHOUT ROWID;
    CREATE INDEX stats_weapons_weapon ON stats_weapons (weapon);
    CREATE TABLE stats_versus (
        person INTEGER NOT NULL, other INTEGER NOT NULL, month INTEGER NOT NULL, kills INTEGER NOT NULL,
        deaths INTEGER NOT NULL, PRIMARY KEY (person, other, month)) WITHOUT ROWID;
    CREATE TABLE stats_official (
        person INTEGER PRIMARY KEY, name TEXT NOT NULL, rank INTEGER, title TEXT NOT NULL, grade INTEGER,
        exp INTEGER, skill INTEGER, highest_skill INTEGER, games INTEGER, wins INTEGER, losses INTEGER, ties INTEGER,
        quits INTEGER, kills INTEGER, deaths INTEGER, assists INTEGER, betrayals INTEGER, suicides INTEGER,
        headshots INTEGER, best_streak INTEGER, seconds INTEGER, score INTEGER, first_seen REAL, last_seen REAL,
        as_of REAL NOT NULL);
    CREATE TABLE stats_read (file TEXT PRIMARY KEY, size INTEGER NOT NULL);
    CREATE TABLE stats_kills (game INTEGER PRIMARY KEY, kills BLOB NOT NULL, kept REAL NOT NULL);
    CREATE TABLE stats_seasons (
        number INTEGER PRIMARY KEY, name TEXT NOT NULL, started REAL NOT NULL, ends REAL, ended REAL,
        started_by TEXT NOT NULL, ended_by TEXT);
    """,
]
TOTALS = ("score", "kills", "deaths", "assists", "betrayals", "suicides", "headshots", "best_streak")
OFFICIAL = ("rank", "grade", "exp", "skill", "highest_skill", "games", "wins", "losses", "ties", "quits", "kills",
            "deaths", "assists", "betrayals", "suicides", "headshots", "best_streak", "time_played", "score")
BY_WEAPON = ("kills", "deaths", "headshots", "betrayals", "suicides")


LARGEST = 2 ** 63 - 1  # SQLite's: a number past it (a count gone round from 0) stopped the backfill for good (audit 0.6.1, 1.3)


def _int(value: Any) -> int:
    """A count as the report gives it: anything else is 0, and so is one no count could be."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
        return 0
    return int(value) if -LARGEST <= value <= LARGEST else 0


def _text(value: Any, longest: int = 120) -> str:
    return str(value)[:longest] if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


@dataclass
class PlayerPart:
    player_id: str
    name: str
    team: int | None
    place: int
    totals: dict[str, int]
    seconds: int
    quit: bool
    result: str = NONE
    medals: dict[str, int] = field(default_factory=dict)
    weapons: dict[str, dict[str, int]] = field(default_factory=dict)
    versus: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass
class Game:
    game_id: str
    map: str
    map_title: str
    variant: str
    mode: str
    team_game: bool
    ranked: bool
    started: float
    ended: float
    scores: dict[int, int]
    winner: str  # a team's number, "tie", or "" (free-for-all, or nobody)
    players: list[PlayerPart]


def live_id(text: str) -> str | None:
    """A player ID as a report going by gives it: a whole one, or nobody (audit 0.6.1, 1.11: a player with none was kept)."""
    return text.lower() if FULL_ID.fullmatch(text.lower()) else None


def read_game(report: Any, ids: Callable[[str], str | None] = live_id) -> Game | None:
    """What a report says of its game, or None if it isn't one. `ids` turns the report's player IDs into Huragok's
    (lower case; the backfill's stand-ins into the real ones): a player it gives None for is left out."""
    if not isinstance(report, Mapping) or not _text(report.get("game_id")) or not isinstance(report.get("players"), list):
        return None
    team_game = bool(report.get("team_game"))
    scores = {}
    for team, score in (report.get("team_scores") or {}).items() if isinstance(report.get("team_scores"), Mapping) else ():
        if str(team).isdecimal():
            scores[int(team)] = _int(score)
    players = []
    for given in report["players"]:
        if not isinstance(given, Mapping) or (player_id := ids(str(given.get("id") or ""))) is None:
            continue
        team = given.get("team")
        part = PlayerPart(player_id, _text(given.get("name"), 64), _int(team) if team_game and isinstance(team, (int, float)) else None,
                          _int(given.get("place")), {key: _int(given.get(key)) for key in TOTALS},
                          _int(given.get("time_played")), bool(given.get("quit")))
        for medal, count in (given.get("medals") or {}).items() if isinstance(given.get("medals"), Mapping) else ():
            if _int(count) > 0:
                part.medals[_text(medal, 64)] = _int(count)
        for weapon, counts in (given.get("weapons") or {}).items() if isinstance(given.get("weapons"), Mapping) else ():
            if isinstance(counts, Mapping) and any(_int(counts.get(key)) for key in BY_WEAPON):
                part.weapons[_text(weapon, 64)] = {key: _int(counts.get(key)) for key in BY_WEAPON}
        for other, counts in (given.get("versus") or {}).items() if isinstance(given.get("versus"), Mapping) else ():
            other_id = ids(str(other))
            if other_id and other_id != player_id and isinstance(counts, Mapping):
                part.versus[other_id] = (_int(counts.get("kills")), _int(counts.get("deaths")))
        players.append(part)
    game = Game(_text(report["game_id"], 64), _text(report.get("map"), 64), _text(report.get("map_title"), 120),
                _text(report.get("variant"), 120), _text(report.get("mode"), 64), team_game, bool(report.get("ranked")),
                float(_int(report.get("started_at"))), float(_int(report.get("ended_at"))), scores, "", players)
    decide(game)
    return game


def decide(game: Game) -> None:
    """Who won, and how it went for each player (the host's calls, 3 November 2026): a team game's highest score wins,
    and two sharing it tie; in free-for-all only first place wins. Fewer than two sides in it: nobody."""
    if game.team_game:
        sides = {part.team for part in game.players if part.team is not None}
        playing = {team: game.scores.get(team, 0) for team in sides}
        if len(playing) < 2:
            return
        best = max(playing.values())
        top = [team for team, score in playing.items() if score == best]
        game.winner = str(top[0]) if len(top) == 1 else TIE
        for part in game.players:
            part.result = (NONE if part.team is None else WIN if game.winner == str(part.team)
                           else TIE if part.team in top else LOSS)
        return
    if len(game.players) < 2:
        return
    for part in game.players:
        part.result = WIN if part.place == 1 else LOSS


def month_of(when: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(when))


def person(db: Any, player_id: str) -> int:
    """The player's number in the stats, given the first time it's wanted."""
    db.execute("INSERT OR IGNORE INTO stats_people (player_id) VALUES (?)", (player_id,))
    return int(db.query("SELECT number FROM stats_people WHERE player_id = ?", (player_id,))[0]["number"])


def number_of(db: Any, player_id: str) -> int | None:
    """The player's number in the stats, if they have one: None if they've no game in them."""
    rows = db.query("SELECT number FROM stats_people WHERE player_id = ?", (player_id.lower(),))
    return int(rows[0]["number"]) if rows else None


def store(db: Any, server: str, game: Game, received: float) -> bool:
    """The game into the tables, all of it or none. False: it's there already (a report sent again after a failure)."""
    if db.query("SELECT 1 FROM stats_games WHERE game_id = ?", (game.game_id,)):
        return False
    ended = game.ended or received
    month = int(month_of(ended).replace("-", ""))
    with db.transaction():
        number = db.execute(
            "INSERT INTO stats_games (game_id, server, map, map_title, variant, mode, team_game, ranked, started, ended, "
            "red, blue, winner, players, received) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (game.game_id, server, game.map, game.map_title, game.variant, game.mode, int(game.team_game),
             int(game.ranked), game.started, ended, game.scores.get(0) if game.team_game else None,
             game.scores.get(1) if game.team_game else None, game.winner, len(game.players), received)).lastrowid
        numbers = {part.player_id: person(db, part.player_id) for part in game.players}
        for part in game.players:
            cursor = db.execute(
                "INSERT OR IGNORE INTO stats_players (game, person, name, team, place, score, kills, deaths, assists, "
                "betrayals, suicides, headshots, best_streak, seconds, quit, result, ended) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (number, numbers[part.player_id], part.name, part.team, part.place,
                 *(part.totals[key] for key in TOTALS), part.seconds, int(part.quit), part.result, ended))
            if not cursor.rowcount:
                continue  # the same player twice in one report: the first stands
            row = cursor.lastrowid
            for medal, count in part.medals.items():
                db.execute("INSERT OR IGNORE INTO stats_medals (part, medal, count) VALUES (?, ?, ?)", (row, medal, count))
            for weapon, counts in part.weapons.items():
                db.execute("INSERT OR IGNORE INTO stats_weapons (part, weapon, kills, deaths, headshots, betrayals, "
                           "suicides) VALUES (?, ?, ?, ?, ?, ?, ?)", (row, weapon, *(counts[key] for key in BY_WEAPON)))
            for other, (kills, deaths) in part.versus.items():
                db.execute("INSERT INTO stats_versus (person, other, month, kills, deaths) VALUES (?, ?, ?, ?, ?) "
                           "ON CONFLICT (person, other, month) DO UPDATE SET kills = kills + excluded.kills, "
                           "deaths = deaths + excluded.deaths",
                           (numbers[part.player_id], numbers.get(other) or person(db, other), month, kills, deaths))
    return True


def store_official(db: Any, answer: Any, as_of: float, ids: Callable[[str], str | None] = str.lower) -> int:
    """Each player's record as the service's answer gives it, where it's newer than what's kept: how many."""
    players = answer.get("players") if isinstance(answer, Mapping) else None
    if not isinstance(players, list):
        return 0
    kept = 0
    with db.transaction():
        for given in players:
            if not isinstance(given, Mapping) or (player_id := ids(str(given.get("id") or ""))) is None:
                continue
            values = [_int(given.get(key)) for key in OFFICIAL]
            cursor = db.execute(
                "INSERT INTO stats_official (person, name, rank, title, grade, exp, skill, highest_skill, games, wins, "
                "losses, ties, quits, kills, deaths, assists, betrayals, suicides, headshots, best_streak, seconds, score, "
                "first_seen, last_seen, as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (person) DO UPDATE SET name = excluded.name, rank = excluded.rank, title = excluded.title, "
                "grade = excluded.grade, exp = excluded.exp, skill = excluded.skill, highest_skill = excluded.highest_skill, "
                "games = excluded.games, wins = excluded.wins, losses = excluded.losses, ties = excluded.ties, "
                "quits = excluded.quits, kills = excluded.kills, deaths = excluded.deaths, assists = excluded.assists, "
                "betrayals = excluded.betrayals, suicides = excluded.suicides, headshots = excluded.headshots, "
                "best_streak = excluded.best_streak, seconds = excluded.seconds, score = excluded.score, "
                "first_seen = excluded.first_seen, last_seen = excluded.last_seen, as_of = excluded.as_of "
                "WHERE excluded.as_of > stats_official.as_of",
                (person(db, player_id), _text(given.get("name"), 64), values[0], _text(given.get("title"), 64), *values[1:],
                 float(_int(given.get("first_seen"))) or None, float(_int(given.get("last_seen"))) or None, as_of))
            kept += cursor.rowcount
    return kept


# The backfill: the copies already kept --------------------------------------------------------------------------------

def report_files(folder: Path) -> list[Path]:
    """The stats reports' daily files, oldest first, gzipped or not."""
    return sorted((path for path in folder.glob("stats-reports-*.jsonl*") if DAY_FILE.fullmatch(path.name)),
                  key=lambda path: path.name)


def read_copies(path: Path) -> list[tuple[str, float, Any, int, Any]]:
    """(server, when, report, the answer's status, the answer) for each game's report in one day's file: what the
    backfill takes in. Read off the event loop: a busy server's month is many megabytes."""
    found = []
    try:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as file:
            for line in file:
                try:
                    copy = json.loads(line)
                    request = copy.get("request") if isinstance(copy, dict) else None
                    if not isinstance(request, dict) or request.get("path") != MATCHES:
                        continue
                    report = ((request.get("body") or {}).get("json") or {}).get("report")
                    answer = copy.get("answer") or {}
                    found.append((str(copy.get("server") or ""), float(_int(copy.get("at"))), report,
                                  _int(answer.get("status")), (answer.get("body") or {}).get("json")))
                except (ValueError, TypeError, AttributeError):
                    continue  # a line cut short as Huragok stopped, or one that isn't a copy: the rest are read (audit 0.6.1, 1.3)
    except (OSError, EOFError, ValueError):
        pass  # a gzip cut short: what was read stands
    return found


def real_ids(scrub: Scrubber, known: Iterable[str]) -> Callable[[str], str | None]:
    """The copies' player IDs back as Huragok knows them: a stand-in for someone seen joining, or a whole ID (the
    copies before 0.6.0 kept those as they were where the service keyed its answer by them)."""
    back = {scrub.stand_in(player_id): player_id.lower() for player_id in known}

    def real(found: str) -> str | None:
        if STAND_IN.fullmatch(found):
            return back.get(found)
        return found.lower() if FULL_ID.fullmatch(found.lower()) else None

    return real


class Stats(Part):
    persona = SUPERINTENDENT
    name = "stats"
    description = "Every finished game, from its server's report to the stats service: what players' stats count"
    always_on = True  # nothing to set: the servers report their games anyway
    capture: StatsCapture | None = None

    async def setup(self) -> None:
        self.ctx.db.migrate(SCHEMA)
        home = Path(self.ctx.home)
        self.records = home / "records"
        self.folder = home / FOLDER / REPORTS
        self.folder.mkdir(parents=True, exist_ok=True)
        self.scrub = Scrubber(key_for(self.records))
        # Before anything listens: a report retried as Huragok starts comes in at once (audit 0.6.1, 1.7).
        self.kills: dict[str, list[tuple[float, str, str, Any]]] = {}  # each server's game on now: (when, killer, victim, report)
        self.before: dict[str, list[tuple[float, str, str, Any]]] = {}  # the game before's, until its report comes
        self.engines: dict[str, dict[str, tuple[str, str]]] = {}  # engine number -> (player ID, name), per server
        self.asked: dict[str, float] = {}
        self.was: dict[str, str] = {}  # each server's last phase
        self.capture = StatsCapture(self.records, self.copy, self.scrub, home=home, on_report=self.take)
        await self.capture.apply(list(self.ctx.servers), {name: self._paths(name) for name in self.ctx.servers})
        self._notes()
        background(self.backfill(), what="stats: reading in the reports already kept")
        background(self.count_medals(), what="stats: counting how often each medal's been earned")

    # Each game's kills, from the console: who killed whom, when and with what (weapons.py), kept with the game once its
    # report comes, for its Carnage Report's kill feed (the host's call, 3 November 2026: a feed of every kill would
    # be more than Discord lets a channel take; a game's own list, to look at, isn't).

    @on("rcon.phase")
    async def phase(self, event: Event) -> None:
        server, phase = event.server or "", str(event.get("phase") or "")
        if phase == "playing" and self.was.get(server) not in GAME_ON:  # a new game, not a new round of it
            self.before[server], self.kills[server] = self.kills.get(server, []), []  # its report may yet come
            background(self._who(server), what="stats: who's who for the kill feed")
        self.was[server] = phase

    @on("rcon.kill")
    async def kill(self, event: Event) -> None:
        server = event.server or ""
        kills = self.kills.setdefault(server, [])
        if len(kills) >= MOST_KILLS:
            return
        killer, victim = event.get("killer"), event.get("victim")
        kills.append((time.time(), "" if killer is None else str(killer), "" if victim is None else str(victim), event.get("report")))
        known = self.engines.get(server, {})
        if any(engine and engine not in known for engine in (kills[-1][1], kills[-1][2])):
            background(self._who(server), what="stats: who's who for the kill feed")

    async def _who(self, server: str) -> None:
        """Engine numbers to players, from `players` (read-only: never in server.log), at most every ASK_SECONDS."""
        if time.monotonic() - self.asked.get(server, float("-inf")) < ASK_SECONDS:
            return
        self.asked[server] = time.monotonic()
        try:
            client = self.ctx.rcon(server)
            if not client.connected:
                return
            reply = await client.command("players")
        except Exception as error:  # noqa: BLE001 - the kill feed names fewer players, that's all
            self.log.debug("[%s] couldn't read who's who: %s", server, error)
            return
        data = reply.data if isinstance(reply.data, dict) else {}
        known = self.engines.setdefault(server, {})
        rows = [row for row in [*(data.get("players") or []), *(data.get("guest_players") or [])] if isinstance(row, dict)]
        rows += [guest for row in list(rows) for guest in row.get("guest_players") or [] if isinstance(guest, dict)]  # each player's guests (1.8)
        for row in rows:
            if row.get("engine_id") is not None:
                known[str(row["engine_id"])] = (str(row.get("id") or "").lower(), str(row.get("name") or ""))

    def _keep_kills(self, server: str, game: Game) -> None:
        """The game's kills, with names, kept with it (gzipped JSON: a busy game has thousands). Its own, by when they
        were: a report that came after the next game began took that game's first kills, and lost its own (audit
        0.6.1, 1.4)."""
        pool = [*self.before.pop(server, []), *self.kills.get(server, [])]
        end = (game.ended or time.time()) + KILL_SLACK
        kills = [kill for kill in pool if kill[0] <= end and (not game.started or kill[0] >= game.started - KILL_SLACK)]
        self.kills[server] = [kill for kill in self.kills.get(server, []) if kill[0] > end]
        if not kills:
            return
        known = self.engines.get(server, {})
        start = game.started or kills[0][0]
        rows = [[round(max(0.0, when - start)), *known.get(killer, ("", "")), *known.get(victim, ("", "")), report]
                if killer or victim else None for when, killer, victim, report in kills]
        number = self.ctx.db.query("SELECT number FROM stats_games WHERE game_id = ?", (game.game_id,))
        if number:
            kept = [row for row in rows if row]
            self.ctx.db.execute("INSERT OR REPLACE INTO stats_kills (game, kills, kept) VALUES (?, ?, ?)",
                                (int(number[0]["number"]), gzip.compress(json.dumps(kept).encode("utf-8")), time.time()))
            # The Kill feed as kept, for the temp-discord record (the host, 4 November 2026: checked against real games
            # after the update): each kill with the weapon as the feed says it.
            from .weapons import how

            self.ctx.emit("kills", server, game_id=game.game_id, number=int(number[0]["number"]), count=len(kept),
                          kills=[[*row, how(row[5]).words()] for row in kept])

    @every(hours=6)
    async def forget_kills(self) -> None:
        self.ctx.db.execute("DELETE FROM stats_kills WHERE kept < ?", (time.time() - KILLS_KEPT_DAYS * 86400,))

    def _paths(self, name: str) -> tuple[Path | None, Path | None]:
        return self.ctx.config_path(name), self.ctx.server_option(name, "log_path")

    def _notes(self) -> None:
        if self.capture is None:
            return
        for line in self.capture.notes:
            self.log.info("%s", line)
            self.ctx.emit("capture", None, text=line)
        self.capture.notes.clear()

    @on("log.stats_reporting")
    async def reporting(self, event: Event) -> None:
        """Where a server reports, as it starts: one Huragok isn't standing in the middle of yet (new, or its server.log
        didn't say before) it is from now on, from its next start."""
        if self.capture is None or event.server is None or event.server not in self.ctx.servers:
            return
        await self.capture.learned(event.server, str(event.get("url", "")), *self._paths(event.server))
        self._notes()

    def take(self, server: str, path: str, sent: Any, status: int, answer: Any) -> None:
        """A game's report as it went by, and the service's answer."""
        if path.split("?", 1)[0] != MATCHES or not isinstance(sent, Mapping):
            return
        now = time.time()
        game = read_game(sent.get("report"))
        if game is not None and store(self.ctx.db, server, game, now):
            self._keep_kills(server, game)
            self.ctx.emit("game", server, game_id=game.game_id, map=game.map_title or game.map, mode=game.mode,
                          players=len(game.players), winner=game.winner)
        if 200 <= status < 300:
            store_official(self.ctx.db, answer, now)

    def copy(self, server: str, data: dict[str, Any]) -> None:
        """A report and the service's answer, with stand-ins for IDs, into the day's file of copies."""
        now = time.time()
        name = f"stats-reports-{time.strftime('%Y-%m-%d', time.gmtime(now))}.jsonl"
        with (self.folder / name).open("a", encoding="utf-8") as file:
            file.write(json.dumps({"at": round(now, 3), "server": server, **data}, ensure_ascii=False, default=str) + "\n")
        answer = data.get("answer") or {}
        self.ctx.emit("reported", server, file=f"{REPORTS}/{name}", status=answer.get("status"),
                      bytes=(data.get("request") or {}).get("bytes"))
        self._notes()

    async def backfill(self) -> int:
        """The copies already kept, read in once each (a file that's grown since is read again: a game already in is
        left as it is). How many games came in."""
        db = self.ctx.db
        seen = {str(row["file"]): int(row["size"]) for row in db.query("SELECT file, size FROM stats_read")}
        known = [str(row["id"]) for row in db.query("SELECT id FROM players")]
        ids = real_ids(self.scrub, known)
        added = 0
        for path in report_files(self.folder):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if seen.get(path.name) == size:
                continue
            for server, when, report, status, answer in await asyncio.to_thread(read_copies, path):
                game = read_game(report, ids)
                if game is not None and store(db, server, game, when):
                    added += 1
                if 200 <= status < 300:
                    store_official(db, answer, when, ids)
                await asyncio.sleep(0)  # a month of games doesn't hold up the servers
            db.execute("INSERT INTO stats_read (file, size) VALUES (?, ?) ON CONFLICT (file) DO UPDATE SET size = excluded.size",
                       (path.name, size))
        if added:
            self.log.info("Read in %d game%s from the reports already kept", added, "" if added == 1 else "s")
        return added

    @command("stats", help="Your record on these servers, or another player's, from every game reported",
             usage="[player]", role="player", start=True)
    async def stats(self, call: Any) -> None:
        from .leaderboards import card, in_game_line, read_soon

        if call.text.strip():
            player = await call.find(call.text.strip(), scope="seen", start=True)
            if player is None:
                return
        else:
            player = call.player
        found = await read_soon(self.ctx.db, card, player.id)  # in a thread: a long record takes a moment
        if found is None:
            await call.reply(f"No reported games for {player.name} yet: a game counts once its server reports it.")
            return
        await call.reply(in_game_line(found))

    # Seasons (the host's calls, 3 November 2026): only faculty start or end one. One with no length runs until
    # faculty end it, and that's how one starts unless a length is given.

    async def _season_words(self, season: Any, now: float) -> str:
        from .leaderboards import read_soon, red_blue

        score = await read_soon(self.ctx.db, red_blue, float(season["started"]), float(season["ended"]) if season["ended"] else None)
        ends = (f", ends in {describe_seconds(float(season['ends']) - now)}" if season["ends"] and not season["ended"]
                else "" if season["ended"] else ", until faculty end it")
        return (f"{season_named(season['number'], season['name'])}, {describe_seconds(now - float(season['started']))} old{ends}: "
                f"Red {score['red']}, Blue {score['blue']}" + (f", {score['ties']} tied" if score["ties"] else ""))

    @command("season", help="The season running now, and Red against Blue in it; or a past season's, by its number",
             usage="[number]", role="player")
    async def season(self, call: Any) -> None:
        from .leaderboards import current_season, last_season, red_blue, season_numbered

        if call.text.strip():  # a past season (the host's call, 3 November 2026)
            asked = call.text.strip().lstrip("#")
            season = season_numbered(self.ctx.db, int(asked)) if asked.isdecimal() and len(asked) <= 6 else None  # 7.7
            await call.reply(await self._season_words(season, time.time()) + "." if season is not None else
                             f"There's no season {call.text.strip()}: {call.prefix}season alone says the one running.")
            return
        season = current_season(self.ctx.db) or last_season(self.ctx.db)  # between seasons, the one just ended (the host's call 12)
        if season is None:
            from .leaderboards import read_soon

            score = await read_soon(self.ctx.db, red_blue)  # in a thread (9.12)
            await call.reply(f"No season running: faculty starts one with {call.prefix}newseason. All time: "
                             f"Red {score['red']}, Blue {score['blue']}.")
            return
        await call.reply(await self._season_words(season, time.time()) + ".")

    @command("newseason", help="End the season running, if any, and start the next: for good, or for a time",
             usage="[name] [length, e.g. 30d]", role="faculty")
    async def new_season(self, call: Any) -> None:
        words = call.text.split()
        length = words.pop() if words and TIME.fullmatch(words[-1].lower()) else ""
        now = time.time()
        ended = await self.end_season(call.player.name, now)
        number = int(self.ctx.db.query("SELECT COALESCE(MAX(number), 0) + 1 AS n FROM stats_seasons")[0]["n"])
        name = " ".join(words)[:40] or f"Season {number}"
        ends = now + seconds_of(length.lower()) if length else None
        self.ctx.db.execute("INSERT INTO stats_seasons (number, name, started, ends, started_by) VALUES (?, ?, ?, ?, ?)",
                            (number, name, now, ends, call.player.name))
        self.ctx.emit("season", None, action="started", number=number, name=name, ends=ends, by=call.player.name)
        self.log.info("%s started season %d (%s)%s", call.player.name, number, name,
                      f", for {describe_time(length.lower())}" if length else ", until faculty end it")
        await call.reply((f"Season {ended['number']} is over. " if ended else "") + f"{season_named(number, name)} has begun"
                         + (f", for {describe_time(length.lower())}." if length else f": it runs until faculty type {call.prefix}endseason."))

    @command("endseason", help="End the season running now", role="faculty")
    async def end_season_command(self, call: Any) -> None:
        ended = await self.end_season(call.player.name, time.time())
        await call.reply(f"{season_named(ended['number'], ended['name'])} is over." if ended else "No season is running.")

    async def end_season(self, by: str, now: float) -> Any:
        """The season running now, ended: what it was, or None if none was. Its score read in a thread (7.3)."""
        from .leaderboards import current_season, read_soon, red_blue

        season = current_season(self.ctx.db)
        if season is None:
            return None
        self.ctx.db.execute("UPDATE stats_seasons SET ended = ?, ended_by = ? WHERE number = ?", (now, by, season["number"]))
        score = await read_soon(self.ctx.db, red_blue, float(season["started"]), now)
        self.ctx.emit("season", None, action="ended", number=int(season["number"]), name=str(season["name"]), by=by,
                      started=float(season["started"]), ended=now, **score)
        self.log.info("Season %d (%s) ended (%s): Red %d, Blue %d", season["number"], season["name"], by,
                      score["red"], score["blue"])
        return season

    @every(minutes=1)
    async def season_ends(self) -> None:
        """A season given a length ends by itself when it's up."""
        from .leaderboards import current_season

        season = current_season(self.ctx.db)
        if season is not None and season["ends"] and float(season["ends"]) <= time.time():
            await self.end_season("its time ran out", float(season["ends"]))  # at its time, not this look's (audit 0.6.1, 1.12)

    @every(hours=1)
    async def count_medals(self) -> None:
        """How often each medal's been earned, for a card's rarest first (leaderboards.count_rarity): an hour at a time, in a thread."""
        from .leaderboards import count_rarity

        await count_rarity(self.ctx.db)

    async def teardown(self) -> None:
        if self.capture is not None:
            await self.capture.close()


__all__ = ["LOSS", "NONE", "SCHEMA", "TIE", "WIN", "Game", "PlayerPart", "Stats", "decide", "read_copies",
           "read_game", "real_ids", "store", "store_official"]
