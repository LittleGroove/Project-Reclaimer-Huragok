"""Halo 3: its playlist files (reading, building from picked maps and game types, editing entries, saving), its
campaign's missions and difficulties, and its 27 multiplayer maps.

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

from ..core.files import write_atomic
from ..core.text import slug
from .halo3_options import LABEL_CHANGES, NEWEST, choices_as_numbers


HALO3_MAPS = {"armory", "bunkerworld", "chill", "chillout", "construct", "cyberdyne", "deadlock", "descent", "docks",
              "fortress", "ghosttown", "guardian", "isolation", "lockout", "midship", "riverworld", "s3d_edge",
              "s3d_turf", "s3d_waterfall", "salvation", "sandbox", "shrine", "sidewinder", "snowbound", "spacecamp",
              "warehouse", "zanzibar"}


MISSIONS = {3010: "Sierra 117", 3020: "Crow's Nest", 3030: "Tsavo Highway", 3040: "The Storm", 3050: "Floodgate",
            3070: "The Ark", 3100: "The Covenant", 3110: "Cortana", 3120: "Halo"}


# Each mission's highest start (`insertion`; 0 is the mission's start), as 0.9.18's `dedicated check` takes them
# with MCC's files (2026-10-18): one higher is "The selected insertion point is not available on this mission".
MISSION_STARTS = {3010: 2, 3020: 2, 3030: 1, 3040: 1, 3050: 1, 3070: 2, 3100: 2, 3110: 1, 3120: 1}


DIFFICULTIES = ("easy", "normal", "heroic", "legendary")


MIN_GAMES, MAX_GAMES = 3, 128


class PlaylistError(Exception):
    pass


@dataclass
class Playlist:
    path: Path
    name: str
    vote_seconds: int = 20
    games: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # keys we don't edit, kept as they were

    @classmethod
    def from_data(cls, path: Path, data: Any) -> Playlist:
        """A playlist file's contents (a kept version's too) as a Playlist at `path`. Raises PlaylistError."""
        if not isinstance(data, dict) or not isinstance(data.get("games", []), list):
            raise PlaylistError(f"{path.name} isn't a playlist")
        games = [g for g in data.get("games", []) if isinstance(g, dict)]
        extra = {k: v for k, v in data.items() if k not in ("version", "name", "vote_seconds", "games")}
        return cls(path, str(data.get("name") or path.stem.removesuffix(".playlist")),
                   int(data.get("vote_seconds") or 20), games, extra)

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

    def duplicates(self) -> dict[int, int]:
        """Games that repeat an earlier one, numbered from 1: {game: the earlier game}. The program's rule: the same
        map and game with the same rules, options and world are one game, whatever each is called; raising its
        weight plays it more often. (It also counts a custom game named by name and by file as the same, which
        only the program can tell: check_plays asks it.)"""
        seen: dict[str, int] = {}
        found: dict[int, int] = {}
        for number, game in enumerate(self.games, 1):
            copy = json.loads(json.dumps(game))  # a deep copy: choices_as_numbers rewrites its options in place
            choices_as_numbers(copy)
            key = json.dumps([str(copy.get("map", "")).casefold(), str(copy.get("game", "")).casefold(),
                              str(copy.get("base_map", "")).casefold(), copy.get("rules") or {},
                              copy.get("options") or {}, copy.get("world") or {}], sort_keys=True)
            if key in seen:
                found[number] = seen[key]
            else:
                seen[key] = number
        return found

    def number_choices(self) -> None:
        """Every option choice as its number, as Huragok writes playlists (labels only on screen)."""
        for game in self.games:
            choices_as_numbers(game)

    def save(self) -> Path:
        if problems := self.problems():
            raise PlaylistError("; ".join(problems))
        self.number_choices()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Whole or not at all: the server's auto_reload reads it as it changes.
        write_atomic(self.path, json.dumps(self.data(), indent=2, ensure_ascii=False) + "\n")
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
    return Playlist.from_data(path, data)


def _label_fixes(data: Any, program_version: str | None) -> list[tuple[str, str, str]]:
    """(option key, the label as written, the label the program takes) for every choice in the playlist's games'
    `options` that a program of this version refuses; `data` gets the labels it takes. Only option values change,
    and only for options that have that label: a game named "Vanilla characters" stays so named."""
    fixes = []
    games = data.get("games") if isinstance(data, dict) else None
    for game in games if isinstance(games, list) else []:
        options = game.get("options") if isinstance(game, dict) else None
        for key, value in list(options.items()) if isinstance(options, dict) else []:
            for change in LABEL_CHANGES:
                taken = change.taken(program_version)
                if taken is None or not isinstance(value, str) or not change.applies_to(key):
                    continue
                if value.strip().lower() == (change.old if taken == change.new else change.new).lower():
                    options[key] = taken
                    fixes.append((key, value, taken))
    return fixes


def old_labels(path: Path, program_version: str | None = NEWEST) -> list[str]:
    """The choice labels in a playlist file that a program of this version refuses (it calls them otherwise).
    Without a version, as the newest program calls them (what `check` warns of); None (not known) names only the
    labels no version takes."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [written for _, written, _ in _label_fixes(data, program_version)]


def rename_old_labels(path: Path, program_version: str | None = NEWEST) -> list[tuple[str, str]]:
    """Each choice label in a playlist file written as the server's own program calls it: [(as written, now)], []
    when nothing changed. `program_version` is that program's version: 0.9.19 or newer takes "Halo 3", older
    "Vanilla characters"; None (not known) changes only labels no version takes ("On with firebombs"). Called
    without it, as the newest program calls them (as before versions were known; keep that only for a server whose
    program is the newest).

    The rest of the file stays exactly as it was (when the targeted change can't be shown to give the same data,
    the file is written whole, indented); the new file replaces the old one whole (a hard-linked copy elsewhere
    keeps its own)."""
    try:
        with path.open(encoding="utf-8", newline="") as file:
            text = file.read()
        data = json.loads(text)
    except (OSError, ValueError):
        return []
    fixes = _label_fixes(data, program_version)
    if not fixes:
        return []
    fixed = text
    for key, written, taken in dict.fromkeys(fixes):
        pattern = re.compile("(" + re.escape(json.dumps(key, ensure_ascii=False)) + r"\s*:\s*)"
                             + re.escape(json.dumps(written, ensure_ascii=False)))
        fixed = pattern.sub(lambda match, taken=taken: match.group(1) + json.dumps(taken, ensure_ascii=False), fixed)
    try:
        exact = json.loads(fixed) == data
    except ValueError:
        exact = False
    if not exact:
        fixed = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    write_atomic(path, fixed, newline="")  # the file's own line ends, as they were
    return [(written, taken) for _, written, taken in fixes]


def playlist_files(server_folder: Path) -> list[Path]:
    folder = server_folder / "playlists"
    return sorted(folder.glob("*.playlist.json")) if folder.is_dir() else []


def new_playlist(server_folder: Path, name: str, *, vote_seconds: int = 20) -> Playlist:
    path = server_folder / "playlists" / f"{slug(name, 'playlist')}.playlist.json"
    return Playlist(path, name, vote_seconds)


def resolve(server_folder: Path, ref: str) -> Path:
    """A playlist by file, path, or its name in playlists/ ("infection" -> playlists/infection.playlist.json)."""
    path = server_folder / ref
    if path.is_file():
        return path
    for candidate in (server_folder / "playlists" / f"{ref}.playlist.json", server_folder / "playlists" / ref,
                      server_folder / "playlists" / f"{slug(ref, 'playlist')}.playlist.json"):
        if candidate.is_file():
            return candidate
    for file in playlist_files(server_folder):
        try:
            if load_playlist(file).name.lower() == ref.lower():
                return file
        except PlaylistError:
            continue
    return path
