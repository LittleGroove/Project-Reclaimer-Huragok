"""One SQLite database per machine: players seen, the actions ledger, and each part's own tables.

Bans stay in the server's bans.json, which the server owns; we never keep a copy.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ID_START = re.compile(r"[0-9a-fA-F]{8,64}")


def _casefold(text: Any) -> Any:
    return text.casefold() if isinstance(text, str) else text

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

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.db.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, params).fetchall()

    # Players --------------------------------------------------------------------

    def record_join(self, player_id: str, name: str, ip: str | None, when: float | None = None) -> None:
        when = when or time.time()
        self.db.execute(
            "INSERT INTO players (id, first_seen, last_seen, last_name, visits) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen, last_name = excluded.last_name, "
            "visits = visits + 1", (player_id, when, when, name))
        self.db.execute(
            "INSERT INTO player_names (id, name, first_seen, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (id, name) DO UPDATE SET last_seen = excluded.last_seen", (player_id, name, when, when))
        if ip:
            self.db.execute(
                "INSERT INTO player_ips (id, ip, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (id, ip) DO UPDATE SET last_seen = excluded.last_seen", (player_id, ip, when, when))

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
        by_id = ID_START.fullmatch(text) is not None
        rows = self.query(
            "SELECT DISTINCT p.id FROM players p LEFT JOIN player_names n ON n.id = p.id "
            "WHERE (? AND p.id LIKE ?) OR casefold(n.name) = ? ORDER BY p.last_seen DESC LIMIT ?",
            (by_id, text.lower() + "%", text.casefold(), limit))
        return [r for r in (self.player(row["id"]) for row in rows) if r]

    # Actions --------------------------------------------------------------------

    def record_action(self, *, kind: str, server: str | None, target_id: str | None, target_name: str | None,
                      by: str, source: str, reason: str = "", duration: str | None = None,
                      evidence: str | None = None, dry_run: bool = False, ok: bool = True,
                      reply: str = "", when: float | None = None) -> int:
        cursor = self.db.execute(
            "INSERT INTO actions (time, server, kind, target_id, target_name, by, source, reason, duration, "
            "evidence, dry_run, ok, reply) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (when or time.time(), server, kind, target_id, target_name, by, source, reason, duration,
             evidence, int(dry_run), int(ok), reply))
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

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self._storage.query(sql, params)


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
