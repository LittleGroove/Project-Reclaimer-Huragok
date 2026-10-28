"""The commands part's own tables, one migration step each, in order (a version before reads the ones it knows)."""

from __future__ import annotations

MIGRATIONS = [
    # 1 (0.5.0): every player's chat line, for !said (said.py).
    """
    CREATE TABLE commands_chat (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, server TEXT, player_id TEXT NOT NULL, name TEXT,
        channel TEXT, text TEXT NOT NULL);
    CREATE INDEX commands_chat_player ON commands_chat (player_id, time);
    """,
    # 2 (0.5.0): Huragok's own list, put on every server's ban list (across.py): its bans (! commands, Vergil, the
    # relay, a /ban made every server's with !banall) and the admins it made (!admin add). `target` is a player ID or
    # an address; `expires` None is for good; `lifted` when it was taken off.
    """
    CREATE TABLE commands_every_server (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL, name TEXT,
        reason TEXT, by TEXT, server TEXT, expires REAL, lifted REAL, lifted_by TEXT);
    CREATE INDEX commands_every_server_target ON commands_every_server (target);
    """,
    # 3 (0.5.0): the ban lists servers shared up to 0.4.4, each put on Huragok's list once (across.py: their bans
    # were every server's when given).
    """
    CREATE TABLE commands_shared_lists (path TEXT PRIMARY KEY, time REAL NOT NULL, bans INTEGER NOT NULL);
    """,
    # 4 (0.5.0): a setting a timed toggle changed (toggles.py: !togglevoice 10m), put back to `before` at `until`;
    # `had_own`: the server's block set it itself (else its own value comes out again, so [defaults] applies).
    """
    CREATE TABLE commands_timed_settings (
        server TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL, before INTEGER NOT NULL,
        had_own INTEGER NOT NULL, until REAL NOT NULL, length TEXT NOT NULL, by TEXT, time REAL NOT NULL,
        PRIMARY KEY (server, key));
    """,
    # 5 (0.5.0): a key the toggles put in a server's own block, and whether the block set it itself before the first
    # of them (toggles.py): one switched back by hand to what [defaults] gives comes out again, so a later change to
    # [defaults] reaches that server (the simulation's finding 7, 26 October).
    """
    CREATE TABLE commands_toggled (
        server TEXT NOT NULL, key TEXT NOT NULL, had_own INTEGER NOT NULL, time REAL NOT NULL,
        PRIMARY KEY (server, key));
    """,
    # 6 (0.5.1): a player kept from calling votes (novotes.py: !novote Bravo 10m), by player ID; `until` None is until
    # someone gives them back (!allowvote: `lifted`); `server` it was given on (all of them, with across_servers).
    """
    CREATE TABLE commands_no_votes (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, target TEXT NOT NULL, name TEXT, reason TEXT, by TEXT,
        server TEXT, until REAL, length TEXT, lifted REAL, lifted_by TEXT);
    CREATE INDEX commands_no_votes_target ON commands_no_votes (target);
    """,
]
