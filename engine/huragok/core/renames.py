"""A server renamed in its own dedicated.toml stays the same server to Huragok (0.7.0, answers 32 and 39).

How a rename is told from a server taken out and another added: a name gone from a dedicated.toml and a new name in
the same file, on the same game port, or, when the port changed too, in the same place among the file's [[server]]
blocks.

What follows it, all of it Huragok's own (the server's folder is the host's and isn't touched; the D -> N rename tool
does that, when the host asks):

- its [[server]] block in settings.toml (autostart, daily restart, priority, Vergil...);
- its records under its slug: records/servers/<slug>.json (wanted running, FAULTED, the daily restart's date), its
  console log, its FAULTED note, its playlist history;
- every row of Huragok's database that names it (stats, leaderboards, ballots, chat, kicks, timed toggles, actions,
  Discord alerts): the same server's history carries on under the new name.

Lines already written to logs, and cards already posted to Discord, keep the name they had then. The stats relay finds
its own address in the renamed server's file and carries on by itself (servers/statsrelay.py `_renamed`).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Any, Callable

from .serverfolders import ServerConfig

if TYPE_CHECKING:
    from .settings import Settings

log = logging.getLogger(__name__)

SERVER_COLUMNS = ("server", "on_server")  # the columns of Huragok's tables that hold a server's name


def pair_renames(gone: list[ServerConfig], appeared: list[ServerConfig]) -> list[tuple[ServerConfig, ServerConfig]]:
    """(old, new) for each server that was renamed: gone from a dedicated.toml while a new name appeared in it, on the
    same game port, else in the same place among its [[server]] blocks."""
    pairs: list[tuple[ServerConfig, ServerConfig]] = []
    left = list(appeared)
    for old in gone:
        same_file = [s for s in left if _same_file(s, old)]
        match = next((s for s in same_file if s.game_port and s.game_port == old.game_port), None)
        if match is None:
            match = next((s for s in same_file if old.block and s.block == old.block
                          and not any(o.game_port == s.game_port for o in gone if o is not old)), None)
        if match is not None:
            pairs.append((old, match))
            left.remove(match)
    return pairs


def _same_file(a: ServerConfig, b: ServerConfig) -> bool:
    try:
        return a.config_path.resolve() == b.config_path.resolve()
    except OSError:
        return a.config_path == b.config_path


def follow_rename(settings: Settings, storage: Any, old: ServerConfig, new: ServerConfig,
                  say: Callable[[str], None] = log.info) -> list[str]:
    """Carry what Huragok keeps for `old` over to `new` (see the top). Returns what it did; each step that can't be
    done is said and skipped, never left half-done in silence."""
    done: list[str] = []
    if old.name.lower() in settings.server_overrides:
        from .tomledit import set_server_setting

        try:
            set_server_setting(settings.path, old.name, "name", new.name)
            settings.server_overrides[new.name.lower()] = {**settings.server_overrides.pop(old.name.lower()),
                                                           "name": new.name}
            done.append(f"its block in {settings.path.name}")
        except OSError as error:  # a TomlEditError is an OSError
            say(f"[{new.name}] its block in {settings.path.name} stays under {old.name}: {error}")
    if old.slug != new.slug:
        moves = [(settings.record_file(old.slug), settings.record_file(new.slug)),
                 (settings.console_log(old.slug), settings.console_log(new.slug)),
                 (settings.faulted_file(old.slug), settings.faulted_file(new.slug)),
                 (settings.records / "playlists" / old.slug, settings.records / "playlists" / new.slug)]
        for source, target in moves:
            if not source.exists() or target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                done.append(f"{source.name} -> {target.name}")
            except OSError as error:
                say(f"[{new.name}] {source} stays where it is: {error}")
    if storage is not None:
        rows = rename_in_database(storage, old.name, new.name)
        if rows:
            done.append(f"{rows} row(s) of its history")
    return done


def rename_in_database(storage: Any, old: str, new: str) -> int:
    """Every row of Huragok's tables naming the server `old` names `new` instead. Returns how many."""
    changed = 0
    try:
        tables = [row[0] for row in storage.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            columns = {row[1] for row in storage.query(f'PRAGMA table_info("{table}")')}
            for column in SERVER_COLUMNS:
                if column in columns:
                    cursor = storage.execute(f'UPDATE OR IGNORE "{table}" SET "{column}" = ? WHERE "{column}" = ?', (new, old))
                    changed += max(cursor.rowcount, 0)
    except sqlite3.Error as error:
        log.warning("[%s] its history in the database stays under %s: %s", new, old, error)
    return changed
