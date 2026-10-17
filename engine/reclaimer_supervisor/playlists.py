"""Playlist files: reading, building from picked maps and game types, editing entries, saving.

A playlist (`playlists/<name>.playlist.json`, version 2) is a rotation players vote on: 3 to
128 games, each a map and a game type (plus a display name, a weight, rules and options).
The server program's `dedicated playlist new` makes every map with every game type; the
window's builder lets the host pick which pairs, then checks the file with
`dedicated playlist show`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_GAMES, MAX_GAMES = 3, 128


class PlaylistError(Exception):
    pass


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "playlist"


@dataclass
class Playlist:
    path: Path
    name: str
    vote_seconds: int = 20
    games: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # keys we don't edit, kept as they were

    @property
    def relative(self) -> str:
        return f"playlists/{self.path.name}"

    def labels(self) -> list[str]:
        out = []
        for game in self.games:
            label = f"{game.get('name') or game.get('game', '?')} on {game.get('map', '?')}"
            if game.get("name") and game.get("game"):
                label += f" ({game['game']})"
            bits = [label]
            if int(game.get("weight", 1)) != 1:
                bits.append(f"weight {game['weight']}")
            if game.get("base_map"):
                bits.append(f"on mod map {game['base_map']}")
            for key in ("rules", "options", "world"):
                if game.get(key):
                    bits.append(key)
            if game.get("anti_cheat") is False:
                bits.append("anti-cheat off")
            if game.get("voice_team_only"):
                bits.append("team voice only")
            out.append(" · ".join(bits))
        return out

    def add(self, map_ref: str, game_ref: str, *, name: str | None = None, weight: int = 1) -> bool:
        """Add a pair unless the same pair (same name) is there. Returns whether it was added."""
        for game in self.games:
            if game.get("map") == map_ref and game.get("game") == game_ref and game.get("name") == name:
                return False
        entry: dict[str, Any] = {"map": map_ref, "game": game_ref}
        if name:
            entry["name"] = name
        if weight != 1:
            entry["weight"] = weight
        self.games.append(entry)
        return True

    def remove(self, numbers: list[int]) -> int:
        keep = [g for i, g in enumerate(self.games, 1) if i not in numbers]
        removed = len(self.games) - len(keep)
        self.games = keep
        return removed

    def set_weight(self, number: int, weight: int) -> None:
        if not 1 <= number <= len(self.games):
            raise PlaylistError(f"no game {number}")
        if not 1 <= weight <= 100:
            raise PlaylistError("weight is 1 to 100")
        game = self.games[number - 1]
        if weight == 1:
            game.pop("weight", None)
        else:
            game["weight"] = weight

    def problems(self) -> list[str]:
        found = []
        if len(self.games) < MIN_GAMES:
            found.append(f"a playlist needs {MIN_GAMES} or more games (this one has {len(self.games)})")
        if len(self.games) > MAX_GAMES:
            found.append(f"a playlist takes at most {MAX_GAMES} games")
        if not 5 <= self.vote_seconds <= 120:
            found.append("vote_seconds is 5 to 120")
        return found

    def data(self) -> dict[str, Any]:
        return {**self.extra, "version": 2, "name": self.name, "vote_seconds": self.vote_seconds, "games": self.games}

    def save(self) -> Path:
        if problems := self.problems():
            raise PlaylistError("; ".join(problems))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.path


def problems_from_show(text: str) -> dict[int, str]:
    """What `dedicated playlist show` says can't be played: game number -> its PROBLEM line."""
    found: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        numbered = re.match(r"^\s*(\d+)\.\s", line)
        if numbered:
            current = int(numbered.group(1))
            continue
        problem = re.match(r"^\s*PROBLEM:\s*(.+?)\s*$", line)
        if problem and current is not None:
            found[current] = problem.group(1)
    return found


def load_playlist(path: Path) -> Playlist:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlaylistError(f"{path} isn't there") from None
    except (OSError, ValueError) as error:
        raise PlaylistError(f"{path.name}: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("games", []), list):
        raise PlaylistError(f"{path.name} isn't a playlist")
    games = [g for g in data.get("games", []) if isinstance(g, dict)]
    extra = {k: v for k, v in data.items() if k not in ("version", "name", "vote_seconds", "games")}
    return Playlist(path, str(data.get("name") or path.stem.removesuffix(".playlist")),
                    int(data.get("vote_seconds") or 20), games, extra)


def playlist_files(server_folder: Path) -> list[Path]:
    folder = server_folder / "playlists"
    return sorted(folder.glob("*.playlist.json")) if folder.is_dir() else []


def new_playlist(server_folder: Path, name: str, *, vote_seconds: int = 20) -> Playlist:
    path = server_folder / "playlists" / f"{slug(name)}.playlist.json"
    return Playlist(path, name, vote_seconds)


def resolve(server_folder: Path, ref: str) -> Path:
    """A playlist by file, path, or its name in playlists/ ("infection" -> playlists/infection.playlist.json)."""
    path = server_folder / ref
    if path.is_file():
        return path
    for candidate in (server_folder / "playlists" / f"{ref}.playlist.json", server_folder / "playlists" / ref,
                      server_folder / "playlists" / f"{slug(ref)}.playlist.json"):
        if candidate.is_file():
            return candidate
    for file in playlist_files(server_folder):
        try:
            if load_playlist(file).name.lower() == ref.lower():
                return file
        except PlaylistError:
            continue
    return path
