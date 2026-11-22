"""Playlists by player count, retired in 0.7.0: what's left is reading a host's old playlists-by-players.toml and
writing its steps as the server's own `playlists` setting, for the host to paste in. Nothing here writes.

Huragok's switcher (0.5.x-0.6.x, the window's G -> P) wrote `playlist = …` into a server's dedicated.toml as the
player count moved. From Project Reclaimer 0.10.1 the server does that itself, with no reload, from its own
[[server]] block (plans/server-files-audit/playlists-format.md 4.2):

    playlists = [
      { file = "playlists/small.playlist.json", players = "1-10" },
      { file = "playlists/big.playlist.json", players = "11+" },
    ]

1 to 16 entries, each a file and the lobby sizes it's for ("8", "8-20", "12+": 1 to 128 players). It takes the place
of `playlist = …` (the server refuses both in one block). The old file, beside a server's dedicated.toml, is left
where it is:

    [[server]]
    name = "LG Test Server"
    on = true
    [[server.step]]
    playlist = "playlists/small.playlist.json"
    up_to = 10                                    # the most players it's for; the last step has none
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILE = "playlists-by-players.toml"
MOST_PLAYLISTS = 16  # what the server's `playlists` takes
MOST_PLAYERS = 128  # the most players a lobby size names


@dataclass
class Native:
    """One server's old steps as its own `playlists` setting."""

    server: str
    on: bool  # whether Huragok was switching for it (off: it played the playlist its file names)
    lines: list[str] = field(default_factory=list)  # the TOML to paste into its [[server]] block
    notes: list[str] = field(default_factory=list)  # what didn't carry over, and why


def _players(low: int, high: int | None) -> str | None:
    """A step's range (fewest, most; None: no cap) as a lobby size the server takes, or None when no lobby has it."""
    low = max(low, 1)  # a lobby has a player in it
    if low > MOST_PLAYERS:
        return None
    if high is None:
        return f"{low}+"
    high = min(high, MOST_PLAYERS)
    if high < low:
        return None
    return str(low) if high == low else f"{low}-{high}"


def _block(folder: Path, server: str) -> dict[str, Any] | None:
    """The server's [[server]] block in the folder's old file; None when there's no file or no block for it. Raises
    ValueError when the file can't be read."""
    try:
        data = tomllib.loads((folder / FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{FILE} can't be read: {error}") from None
    blocks = data.get("server")
    for block in blocks if isinstance(blocks, list) else []:
        if isinstance(block, dict) and str(block.get("name") or "").strip().lower() == server.lower():
            return block
    return None


def native_playlists(folder: Path, server: str) -> Native | None:
    """The server's steps in its folder's playlists-by-players.toml, written as its own `playlists` setting; None when
    the file has no block for it. Raises ValueError when the file can't be read."""
    block = _block(folder, server)
    if block is None:
        return None
    native = Native(server, block.get("on") is True)
    steps = block.get("step") if isinstance(block.get("step"), list) else []
    entries: list[str] = []
    low = 0
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("playlist"), str) or not step["playlist"]:
            native.notes.append(f"step {number} names no playlist file: left out")
            continue
        up_to = step.get("up_to")
        high = up_to if isinstance(up_to, int) and not isinstance(up_to, bool) else None
        players = _players(low, high)
        if players is None:
            native.notes.append(f"{step['playlist']} (step {number}) is for no lobby the server has (1 to "
                                f"{MOST_PLAYERS} players): left out")
        else:
            entries.append(f"  {{ file = {json.dumps(step['playlist'], ensure_ascii=False)}, players = "
                           f"{json.dumps(players)} }},")
        if high is None:
            if number < len(steps):
                native.notes.append(f"the steps after step {number} never played (it has no cap): left out")
            break
        low = high + 1
    if len(entries) > MOST_PLAYLISTS:
        native.notes.append(f"the server takes {MOST_PLAYLISTS} playlists at most: the last "
                            f"{len(entries) - MOST_PLAYLISTS} are left out")
        entries = entries[:MOST_PLAYLISTS]
    if entries:
        native.lines = ["playlists = [", *entries, "]"]
    return native


def retired_warning(folder: Path, server: str) -> list[str]:
    """What a server whose folder has an old playlists-by-players.toml block is told, once per start and on its card:
    that Huragok no longer switches playlists, and the lines that have the server do it itself. [] when it has no
    block. The first line is the warning; the rest are the lines to paste and what didn't carry over."""
    try:
        native = native_playlists(folder, server)
    except ValueError as error:
        return [f"Huragok no longer switches playlists by player count (0.7.0), and {error}: the server does it itself "
                "from Project Reclaimer 0.10.1, with `playlists = [...]` in its [[server]] block"]
    if native is None:
        return []
    if not native.lines:
        return [f"Huragok no longer switches playlists by player count (0.7.0); {FILE} has nothing for this server "
                "the server's own `playlists` setting can take", *native.notes]
    state = "was on" if native.on else "was off"
    return [f"Huragok no longer switches playlists by player count (0.7.0; it {state} for this server). The server "
            "does it itself from Project Reclaimer 0.10.1: in its [[server]] block in dedicated.toml, put these lines "
            f"in place of `playlist = ...` ({FILE} is left as it is, and no longer read)",
            *native.lines, *native.notes]


__all__ = ["FILE", "MOST_PLAYERS", "MOST_PLAYLISTS", "Native", "native_playlists", "retired_warning"]
