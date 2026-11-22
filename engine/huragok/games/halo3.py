"""Halo 3: its playlist files (read, never written: they're the host's), its campaign's missions and difficulties,
and its 27 multiplayer maps.

A playlist (`playlists/<name>.playlist.json`, version 2) is a rotation players vote on: 3 to 128 games before
Project Reclaimer 0.10, more from it (voted on from three up), each a map and a game type (plus a display name, a
weight, rules and options). From 0.10 one playlist may mix Halo 3's maps with Reach's, Combat Evolved's and Halo 2's
(entry_game); CE's and Halo 2's play only their own game types, and the program refuses `options` and `rules` on
them. Huragok makes and edits none of them (0.7.0): it lists and shows them, keeps their versions (core/versions.py),
copies one to another server when the host asks, and warns of a choice label the server's program refuses
(old_labels).

Everything 0.10 writes in one is read and never refused (playlists-format.md, verified on 0.10.8): co-op entries
(`{"campaign": {"mission": 3010}}`, `{"odst": {"mode": "firefight", "mission": 5000, "insertion": 7}}`, shown as
"Campaign: Sierra 117" and "ODST Firefight: Crater (Night)"), `teams`, `players` and `recommended_players`, and
curves or formulas (`{"points": [[2, 15], [16, 25]]}`, `{"formula": "3 + p/8"}`) in place of `vote_seconds`, a weight
or an option, kept as written and shown as following the lobby size.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bungie import GAMES, Game, game_of_map
from .halo3_options import LABEL_CHANGES, NEWEST


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


def entry_game(game: dict[str, Any]) -> Game | None:
    """The game whose map a playlist game names: its stock map's (by title, file name or ID), else by MCC's file
    name for Combat Evolved's and Halo 2's maps, their mods' too (`halo1_bloodgulch_6381b55e`, as 0.10's `dedicated
    content` lists them); None: a Halo 3 mod map, a saved map, a typo (Halo 3's, as far as Huragok knows)."""
    name = str(game.get("map") or "").strip()
    found = game_of_map(name)
    if found is not None:
        return found
    for key in ("halo1", "halo2"):
        if name.casefold().startswith(f"{key}_"):
            return GAMES[key]
    return None


def own_rules_only(game: dict[str, Any]) -> bool:
    """A Combat Evolved or Halo 2 game: it plays only its own game types, and the program refuses `options` and
    `rules` on it, so Halo 3's option labels aren't looked for in it."""
    found = entry_game(game)
    return found is not None and found.own_rules_only


class PlaylistError(Exception):
    pass


FOLLOWS = "follows the lobby size"  # a curve or formula in place of a number (0.10.1 on)


def is_curve(value: Any) -> bool:
    """A value that follows the lobby size (0.10.1's vote_seconds, 0.10.2's weights and options): a table of
    `points` or a `formula` (with `interpolation`, `min`, `max`), kept as written; the program reads it."""
    return isinstance(value, dict) and ("points" in value or "formula" in value)


def value_text(value: Any, unit: str = "") -> str:
    """A number as written ("30 s"), or a curve as the lobby-size range its points give ("15-30 s, follows the lobby
    size"), or just that it follows the lobby size (a formula: the program works it out)."""
    if not is_curve(value):
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        return f"{value}{unit}" if number else str(value)
    points = value.get("points")
    numbers = [p[1] for p in points if isinstance(p, list) and len(p) == 2 and isinstance(p[1], (int, float))
               and not isinstance(p[1], bool)] if isinstance(points, list) else []
    if numbers and "formula" not in value:
        low, high = min(numbers), max(numbers)
        shown = f"{low:g}{unit}" if low == high else f"{low:g}-{high:g}{unit}"
        return f"{shown}, {FOLLOWS}"
    return FOLLOWS


def coop_kind(game: dict[str, Any]) -> str:
    """A playlist game's kind: campaign (`{"campaign": {...}}`), odst (`{"odst": {...}}`, a mission or Firefight), or
    multiplayer (a map and a game type), 0.10's three."""
    if isinstance(game.get("campaign"), dict) or "campaign" in game:
        return "campaign"
    if isinstance(game.get("odst"), dict) or "odst" in game:
        return "odst"
    return "multiplayer"


def game_label(game: dict[str, Any]) -> str:
    """A playlist game in a few words: "Team Slayer on Guardian", "Campaign: Sierra 117 (heroic)", "ODST Firefight:
    Crater (Night)", "ODST campaign: Tayari Plaza". Only what the file says; the program says what it makes of it."""
    from .odst import is_firefight, map_title, mission_title

    kind = coop_kind(game)
    if kind == "campaign":
        setup = game["campaign"] if isinstance(game.get("campaign"), dict) else {}
        mission = setup.get("mission", "?")
        title = MISSIONS.get(mission, str(mission)) if isinstance(mission, int) and not isinstance(mission, bool) \
            else str(mission)
        start = setup.get("insertion")
        bits = [str(setup.get("difficulty") or "normal")] + ([f"start {start}"] if start not in (None, 0, "0") else [])
        return f"Campaign: {title} ({', '.join(bits)})"
    if kind == "odst":
        setup = game["odst"] if isinstance(game.get("odst"), dict) else {}
        difficulty = str(setup.get("difficulty") or "normal")
        if is_firefight(setup):
            return f"ODST Firefight: {map_title(setup.get('mission'), setup.get('insertion'))} ({difficulty})"
        return f"ODST campaign: {mission_title(setup.get('mission', '?'))} ({difficulty})"
    label = f"{game.get('name') or game.get('game', 'Slayer')} on {game.get('map', '?')}"
    if game.get("name") and game.get("game"):
        label += f" ({game['game']})"
    return label


def game_details(game: dict[str, Any]) -> list[str]:
    """What else a playlist game says, in a few words each: its game where it isn't Halo 3, weight, lobby sizes,
    teams, base map, rules and options (and whether one follows the lobby size), picture."""
    bits: list[str] = []
    other = entry_game(game) if coop_kind(game) == "multiplayer" else None
    if other is not None and other.key != "halo3":
        bits.append(other.short)
    weight = game.get("weight", 1)
    if is_curve(weight):
        bits.append(f"weight {FOLLOWS}")
    elif weight != 1:
        # As it's written: "two" or 1.5 broke every playlist screen (int() raised), and the program's check says
        # what's wrong with it (audit 2026-11, 7.9)
        bits.append(f"weight {weight}")
    if players := game.get("players"):
        bits.append(f"players {players}")
    if recommended := game.get("recommended_players"):
        bits.append(f"best at {recommended}")
    if isinstance(game.get("teams"), bool):
        bits.append("teams on" if game["teams"] else "teams off")
    if game.get("base_map"):
        bits.append(f"on mod map {game['base_map']}")
    for key in ("rules", "options", "world"):
        value = game.get(key)
        if value:
            curved = isinstance(value, dict) and any(is_curve(v) for v in value.values())
            bits.append(f"{key} (some follow the lobby size)" if curved else key)
    if game.get("anti_cheat") is False:
        bits.append("anti-cheat off")
    if game.get("voice_team_only"):
        bits.append("team voice only")
    if game.get("preview_image"):
        bits.append(f"picture {game['preview_image']}")
    return bits


@dataclass
class Playlist:
    """A playlist file as it's written, read without judging it: 0.10's co-op entries, curves and formulas for
    `vote_seconds`, weights and options, `teams`, `players` and `recommended_players` are kept as they are. Only a
    file that isn't a playlist at all (no `games` list) is refused; the program's own check says the rest."""

    path: Path
    name: str
    vote_seconds: Any = 20  # whole seconds, or a curve that follows the lobby size (0.10.1), as written
    games: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # the file's other keys, as they are
    vote_given: bool = True  # the file says its vote_seconds (else the program's default, 20 s)

    @classmethod
    def from_data(cls, path: Path, data: Any) -> Playlist:
        """A playlist file's contents (a kept version's too) as a Playlist at `path`. Raises PlaylistError only for
        something that isn't a playlist at all."""
        if not isinstance(data, dict) or not isinstance(data.get("games", []), list):
            raise PlaylistError(f"{path.name} isn't a playlist")
        games = [g for g in data.get("games", []) if isinstance(g, dict)]
        extra = {k: v for k, v in data.items() if k not in ("version", "name", "vote_seconds", "games")}
        given = data.get("vote_seconds") is not None
        return cls(path, str(data.get("name") or path.stem.removesuffix(".playlist")),
                   data["vote_seconds"] if given else 20, games, extra, given)

    @property
    def relative(self) -> str:
        return f"playlists/{self.path.name}"

    @property
    def vote(self) -> str:
        """Its vote time in words: "20 s", "15-30 s, follows the lobby size"."""
        return value_text(self.vote_seconds, " s")

    @property
    def players(self) -> str:
        """The lobby sizes it's for ("1-8", "12+"), "" when it doesn't say."""
        value = self.extra.get("players")
        return "" if value is None or isinstance(value, bool) else str(value)

    @property
    def recommended(self) -> str:
        value = self.extra.get("recommended_players")
        return "" if value is None or isinstance(value, bool) else str(value)

    def labels(self) -> list[str]:
        return [" · ".join([game_label(game), *game_details(game)]) for game in self.games]

    def data(self) -> dict[str, Any]:
        vote = {"vote_seconds": self.vote_seconds} if self.vote_given else {}
        return {**self.extra, "version": 2, "name": self.name, **vote, "games": self.games}


GAME_PARTS = re.compile(r"(?:^|;\s*)game (\d+):\s*")  # 0.10: "<file>: game 2: why; game 6: why"


def game_problems(text: str) -> dict[int, str] | None:
    """0.10's way of naming a playlist's games that can't be played, on one line: "game 2: no multiplayer map
    "Atlantiss" is installed; …; game 6: Guardian Slayer is listed twice (…)" -> {2: "no multiplayer map …", 6: …};
    None when the text doesn't start with a game."""
    if not GAME_PARTS.match(text):
        return None
    parts = GAME_PARTS.split(text)
    return {int(number): why.strip() for number, why in zip(parts[1::2], parts[2::2])}


def problems_from_show(text: str) -> dict[int, str]:
    """What `dedicated playlist show` says can't be played: game number -> its PROBLEM line (0.9), or its part of
    the one line 0.10 says them on ("playlists/x.playlist.json: game 2: …; game 6: …")."""
    found: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        on_one_line = re.match(r"^\S+\.json:\s*(game \d+:.*)$", line.strip())
        if on_one_line:
            found.update(game_problems(on_one_line.group(1)) or {})
            continue
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
    `options` that a program of this version refuses. Only option values count, and only for options that have that
    label: a game named "Vanilla characters" isn't one. Read only: the file is the host's to change."""
    fixes = []
    games = data.get("games") if isinstance(data, dict) else None
    for game in games if isinstance(games, list) else []:
        if isinstance(game, dict) and own_rules_only(game):
            continue  # CE's and Halo 2's: not Halo 3's options, left as written
        options = game.get("options") if isinstance(game, dict) else None
        for key, value in options.items() if isinstance(options, dict) else []:
            for change in LABEL_CHANGES:
                taken = change.taken(program_version)
                if taken is None or not isinstance(value, str) or not change.applies_to(key):
                    continue
                if value.strip().lower() == (change.old if taken == change.new else change.new).lower():
                    fixes.append((key, value, taken))
    return fixes


def entry_label_fixes(game: dict[str, Any], program_version: str | None = NEWEST) -> list[tuple[str, str, str]]:
    """label_fixes for one playlist game (a co-op entry has none)."""
    return _label_fixes({"games": [game]}, program_version)


def label_fixes(path: Path, program_version: str | None = NEWEST) -> list[tuple[str, str, str]]:
    """(option key, the label as written, the label the program takes) for each choice in a playlist file that a
    program of this version refuses; [] when the file can't be read. Without a version, as the newest program calls
    them; None (not known) names only the labels no version takes."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _label_fixes(data, program_version)


def old_labels(path: Path, program_version: str | None = NEWEST) -> list[str]:
    """The choice labels in a playlist file that a program of this version refuses (it calls them otherwise), as
    label_fixes finds them: what `check` warns of."""
    return [written for _, written, _ in label_fixes(path, program_version)]


def playlist_files(server_folder: Path) -> list[Path]:
    folder = server_folder / "playlists"
    return sorted(folder.glob("*.playlist.json")) if folder.is_dir() else []
