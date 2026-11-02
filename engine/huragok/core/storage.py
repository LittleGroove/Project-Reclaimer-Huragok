"""One SQLite database per machine: players seen, the actions ledger, and each part's own tables.

Bans stay in each server's own bans.json, which the server owns. Huragok keeps a list of the bans it gives (and the
admins it makes) to put on every server's: the commands part's commands_every_server table.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

ID_START = re.compile(r"[0-9a-fA-F]{8,64}")


def _casefold(text: Any) -> Any:
    return text.casefold() if isinstance(text, str) else text

# Who's acting: a player's ID, or discord:<id>. Set around a command, so that nothing between it and what it writes down has to carry it. A display name isn't an identity: it can change, or be someone else's.
ACTING_ID: ContextVar[str] = ContextVar("huragok_acting_id", default="")


@contextmanager
def acting(who: str) -> Iterator[None]:
    token = ACTING_ID.set(who)
    try:
        yield
    finally:
        ACTING_ID.reset(token)


CORE_MIGRATIONS = [
    """
    CREATE TABLE players (
        id TEXT PRIMARY KEY, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        last_name TEXT NOT NULL, visits INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE player_names (
        id TEXT NOT NULL, name TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        PRIMARY KEY (id, name));
    CREATE TABLE player_ips (
        id TEXT NOT NULL, ip TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        PRIMARY KEY (id, ip));
    CREATE TABLE actions (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, server TEXT, kind TEXT NOT NULL,
        target_id TEXT, target_name TEXT, by TEXT NOT NULL, source TEXT NOT NULL,
        reason TEXT, duration TEXT, evidence TEXT, dry_run INTEGER NOT NULL, ok INTEGER NOT NULL,
        reply TEXT);
    CREATE INDEX actions_target ON actions (target_id, time);
    """,
    """
    CREATE TABLE ballots (
        time REAL NOT NULL, server TEXT NOT NULL, playlist TEXT NOT NULL, picked TEXT NOT NULL, plays TEXT);
    CREATE INDEX ballots_playlist ON ballots (playlist, server);
    """,
    """
    CREATE TABLE player_notes (
        number INTEGER PRIMARY KEY, player_id TEXT NOT NULL, text TEXT NOT NULL, by TEXT NOT NULL, time REAL NOT NULL, expires REAL);
    CREATE INDEX player_notes_player ON player_notes (player_id, time);
    CREATE TABLE player_flags (
        player_id TEXT NOT NULL, flag TEXT NOT NULL, why TEXT NOT NULL, by TEXT NOT NULL, time REAL NOT NULL,
        PRIMARY KEY (player_id, flag));
    """,
    """
    ALTER TABLE actions ADD COLUMN by_id TEXT;
    ALTER TABLE player_notes ADD COLUMN by_id TEXT;
    ALTER TABLE player_flags ADD COLUMN by_id TEXT;
    """,
    """
    ALTER TABLE player_names ADD COLUMN folded TEXT;
    UPDATE player_names SET folded = casefold(name);
    CREATE INDEX player_names_folded ON player_names (folded);
    """,
]


@dataclass
class PlayerRecord:
    id: str
    first_seen: float
    last_seen: float
    last_name: str
    visits: int
    names: list[str] = field(default_factory=list)
    ips: list[str] = field(default_factory=list)


class Storage:
    def __init__(self, path: Path | None = None) -> None:
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("casefold", 1, _casefold, deterministic=True)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS schema_versions (owner TEXT PRIMARY KEY, version INTEGER NOT NULL)")
        self.migrate("core", CORE_MIGRATIONS)
        self.db.execute("UPDATE player_names SET folded = casefold(name) WHERE folded IS NULL")  # what an older version wrote, if it was put back for a while

    def close(self) -> None:
        self.db.close()

    def migrate(self, owner: str, steps: Sequence[str]) -> int:
        """Apply the steps this database hasn't had yet. Never edit a released step: add one."""
        row = self.db.execute("SELECT version FROM schema_versions WHERE owner = ?", (owner,)).fetchone()
        version = row["version"] if row else 0
        for number, step in enumerate(steps[version:], version + 1):
            self.db.execute("BEGIN")
            try:
                for statement in step.split(";"):
                    if statement.strip():
                        self.db.execute(statement)
                self.db.execute("INSERT INTO schema_versions (owner, version) VALUES (?, ?) "
                                "ON CONFLICT (owner) DO UPDATE SET version = excluded.version", (owner, number))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return len(steps)

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        """Many rows, one commit: each commit waits on the disk, and this is on the event loop."""
        self.db.execute("BEGIN")
        try:
            self.db.executemany(sql, rows)
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    # Ballots --------------------------------------------------------------------

    def record_pick(self, server: str, playlist: str, picked: str, plays: str = "", when: float | None = None) -> None:
        """A game players picked on a playlist's ballot (`playlist` by the name its lobby shows)."""
        self.db.execute("INSERT INTO ballots (time, server, playlist, picked, plays) VALUES (?, ?, ?, ?, ?)",
                        (when or time.time(), server, playlist, picked, plays))

    def picks(self, playlist: str, server: str | None = None) -> list[tuple[str, int, float]]:
        """How often each game of a playlist was picked: (the game as the server named it, times, last time),
        most picked first; one server's, or every server's that plays a playlist of that name."""
        rows = self.db.execute(
            "SELECT picked, COUNT(*) AS times, MAX(time) AS last FROM ballots WHERE casefold(playlist) = ? "
            + ("AND server = ? " if server else "") + "GROUP BY casefold(picked) ORDER BY times DESC, last DESC",
            (_casefold(playlist), server) if server else (_casefold(playlist),)).fetchall()
        return [(str(r["picked"]), int(r["times"]), float(r["last"])) for r in rows]

    # Players --------------------------------------------------------------------

    def record_join(self, player_id: str, name: str, ip: str | None, when: float | None = None) -> float | None:
        """Returns when they were last seen before this; None the first time."""
        when = when or time.time()
        before = self.db.execute("SELECT last_seen FROM players WHERE id = ?", (player_id,)).fetchone()
        self.db.execute(
            "INSERT INTO players (id, first_seen, last_seen, last_name, visits) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen, last_name = excluded.last_name, "
            "visits = visits + 1", (player_id, when, when, name))
        self.db.execute(
            "INSERT INTO player_names (id, name, first_seen, last_seen, folded) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id, name) DO UPDATE SET last_seen = excluded.last_seen", (player_id, name, when, when, name.casefold()))
        if ip:
            self.db.execute(
                "INSERT INTO player_ips (id, ip, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (id, ip) DO UPDATE SET last_seen = excluded.last_seen", (player_id, ip, when, when))
        return float(before["last_seen"]) if before is not None else None

    def player(self, player_id: str) -> PlayerRecord | None:
        row = self.db.execute("SELECT * FROM players WHERE id = ?", (player_id.lower(),)).fetchone()
        if row is None:
            return None
        names = [r["name"] for r in self.query(
            "SELECT name FROM player_names WHERE id = ? ORDER BY last_seen DESC", (row["id"],))]
        ips = [r["ip"] for r in self.query(
            "SELECT ip FROM player_ips WHERE id = ? ORDER BY last_seen DESC", (row["id"],))]
        return PlayerRecord(row["id"], row["first_seen"], row["last_seen"], row["last_name"], row["visits"], names, ips)

    def find_players(self, text: str, limit: int = 10) -> list[PlayerRecord]:
        """By player ID (all of it, or its first 8 characters or more) or any name they have used, in any case.

        A shorter start of an ID is never enough: "3" or "cafe" would pick a stranger whose ID happens to
        begin with it. Names are compared casefolded in Python, since SQLite's lower() only folds A-Z.
        """
        text = text.strip()
        wanted = text.lower()
        by_id = self.query("SELECT id FROM players WHERE id >= ? AND id < ? ORDER BY last_seen DESC LIMIT ?", (wanted, wanted + "\uffff", limit)) if ID_START.fullmatch(text) else []
        by_name = self.query("SELECT p.id FROM player_names n JOIN players p ON p.id = n.id WHERE n.folded = ? GROUP BY p.id ORDER BY p.last_seen DESC LIMIT ?", (text.casefold(), limit))
        found = dict.fromkeys(str(row["id"]) for row in [*by_id, *by_name])
        return [r for r in (self.player(player_id) for player_id in list(found)[:limit]) if r]

    def players_named(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        """(ID, name) for whoever has gone by exactly this name, in any case: however many others have it in theirs."""
        rows = self.query("SELECT id, name FROM player_names WHERE folded = ? ORDER BY last_seen DESC LIMIT ?", (text.strip().casefold(), limit))
        return [(str(row["id"]), str(row["name"])) for row in rows]

    def players_like(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        """(ID, name) for each name with this in it, the latest first: for picking a player while typing."""
        pattern = "%" + re.sub(r"([\\%_])", r"\\\1", text.strip().casefold()) + "%"
        rows = self.query("SELECT id, name FROM player_names WHERE folded LIKE ? ESCAPE '\\' ORDER BY last_seen DESC LIMIT ?", (pattern, limit))
        return [(str(row["id"]), str(row["name"])) for row in rows]

    def add_note(self, player_id: str, text: str, by: str, *, expires: float | None = None, when: float | None = None) -> int:
        cursor = self.db.execute("INSERT INTO player_notes (player_id, text, by, time, expires, by_id) VALUES (?, ?, ?, ?, ?, ?)",
                                 (player_id.lower(), text, by, when or time.time(), expires, ACTING_ID.get() or None))
        return int(cursor.lastrowid or 0)

    def notes_of(self, player_id: str) -> list[sqlite3.Row]:
        """The latest first, without those past their time."""
        return self.query("SELECT * FROM player_notes WHERE player_id = ? AND (expires IS NULL OR expires > ?) ORDER BY time DESC, number DESC",
                          (player_id.lower(), time.time()))

    def drop_note(self, number: int) -> str | None:
        """Whose note it was; None if there's no such note."""
        rows = self.query("SELECT player_id FROM player_notes WHERE number = ?", (number,))
        if not rows:
            return None
        self.db.execute("DELETE FROM player_notes WHERE number = ?", (number,))
        return str(rows[0]["player_id"])

    def set_flag(self, player_id: str, flag: str, why: str, by: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO player_flags (player_id, flag, why, by, time, by_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (player_id.lower(), flag, why, by, time.time(), ACTING_ID.get() or None))

    def clear_flag(self, player_id: str, flag: str) -> bool:
        return self.db.execute("DELETE FROM player_flags WHERE player_id = ? AND flag = ?", (player_id.lower(), flag)).rowcount > 0

    def flags_of(self, player_id: str) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM player_flags WHERE player_id = ? ORDER BY time", (player_id.lower(),))

    def names_starting(self, word: str, limit: int = 200) -> list[str]:
        """Names of more than one word that players have used, starting with `word` (any case): so a command can
        tell where a name like "Nate Higgers" ends and the rest of the line begins. The most recent first."""
        pattern = re.sub(r"([\\%_])", r"\\\1", word.casefold()) + " %"
        rows = self.query("SELECT name FROM player_names WHERE folded LIKE ? ESCAPE '\\' "
                          "GROUP BY name ORDER BY MAX(last_seen) DESC LIMIT ?", (pattern, limit))
        return [str(row["name"]) for row in rows]

    # Actions --------------------------------------------------------------------

    def record_action(self, *, kind: str, server: str | None, target_id: str | None, target_name: str | None,
                      by: str, source: str, reason: str = "", duration: str | None = None,
                      evidence: str | None = None, dry_run: bool = False, ok: bool = True,
                      reply: str = "", when: float | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO actions (time, server, kind, target_id, target_name, by, source, reason, duration, "
            "evidence, dry_run, ok, reply, by_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (when or time.time(), server, kind, target_id, target_name, by, source, reason, duration,
             evidence, int(dry_run), int(ok), reply, ACTING_ID.get() or None))
        return int(cursor.lastrowid or 0)

    def actions_for(self, player_id: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM actions WHERE target_id = ? ORDER BY time DESC LIMIT ?",
                          (player_id.lower(), limit))


class PartStore:
    """A part's view of the database: its own tables, named with its prefix by convention."""

    def __init__(self, storage: Storage, part: str) -> None:
        self._storage = storage
        self.part = part

    def migrate(self, steps: Iterable[str]) -> int:
        return self._storage.migrate(self.part, list(steps))

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self._storage.execute(sql, params)

    def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self._storage.execute_many(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self._storage.query(sql, params)


def read_picks(path: Path | None, playlist: str) -> tuple[list[tuple[str, int, float]], float | None]:
    """What players picked on a playlist's ballots, read-only from Huragok's records (the window, a second one
    too): ([(the game as the server named it, times, last time)], most picked first; when the first was kept).
    ([], None) when nothing's kept yet or the records can't be read."""
    from urllib.request import pathname2url

    if path is None or not path.is_file():
        return [], None
    try:
        db = sqlite3.connect(f"file:{pathname2url(str(path))}?mode=ro", uri=True, timeout=2)
        db.create_function("casefold", 1, _casefold, deterministic=True)
        try:
            rows = db.execute("SELECT picked, COUNT(*), MAX(time) FROM ballots WHERE casefold(playlist) = ? "
                              "GROUP BY casefold(picked) ORDER BY COUNT(*) DESC, MAX(time) DESC",
                              (_casefold(playlist),)).fetchall()
            first = db.execute("SELECT MIN(time) FROM ballots WHERE casefold(playlist) = ?",
                               (_casefold(playlist),)).fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error:
        return [], None
    return [(str(r[0]), int(r[1]), float(r[2])) for r in rows], first


def recent_players(path: Path | None, limit: int = 15) -> list[tuple[str, str, float]]:
    """The players seen most lately, read-only from Huragok's records (the window lists them to pick from, so
    nobody has to type a player ID): (ID, the name they last used, when they were last seen). [] when there are
    no records yet or they can't be read."""
    from urllib.request import pathname2url

    if path is None or not path.is_file():
        return []
    try:
        db = sqlite3.connect(f"file:{pathname2url(str(path))}?mode=ro", uri=True, timeout=2)
        try:
            rows = db.execute("SELECT id, last_name, last_seen FROM players ORDER BY last_seen DESC LIMIT ?",
                              (limit,)).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return [(str(r[0]), str(r[1]), float(r[2])) for r in rows]


class PlayerHistory:
    """Every player Huragok has seen, on any server on this machine."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def get(self, player_id: str) -> PlayerRecord | None:
        return self._storage.player(player_id)

    def find(self, text: str, limit: int = 10) -> list[PlayerRecord]:
        return self._storage.find_players(text, limit)

    def names_starting(self, word: str) -> list[str]:
        """Names of more than one word seen here that start with `word`: where a typed name ends."""
        return self._storage.names_starting(word)

    def like(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        return self._storage.players_like(text, limit)

    def named(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        return self._storage.players_named(text, limit)

    def notes(self, player_id: str) -> list[sqlite3.Row]:
        return self._storage.notes_of(player_id)

    def add_note(self, player_id: str, text: str, by: str, *, expires: float | None = None) -> int:
        return self._storage.add_note(player_id, text, by, expires=expires)

    def drop_note(self, number: int) -> str | None:
        return self._storage.drop_note(number)

    def flags(self, player_id: str) -> list[sqlite3.Row]:
        return self._storage.flags_of(player_id)

    def set_flag(self, player_id: str, flag: str, why: str, by: str) -> None:
        self._storage.set_flag(player_id, flag, why, by)

    def clear_flag(self, player_id: str, flag: str) -> bool:
        return self._storage.clear_flag(player_id, flag)
