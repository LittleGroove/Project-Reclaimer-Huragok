"""File health: what a server plays, every file it names, and what will probably go wrong, as warnings (0.7.0,
plans/server-files-audit/plan-0.7.0.md sections 4.1 and 5; huragok-code.md C.2). Read only: a server's files are
the host's. The screens (G, D) are built on this; the warnings catalogue hears its findings (content/playswatch.py).

One resolver for every name a playlist game, `map =`, `game =` or `variant =` gives, the way Project Reclaimer 0.10
resolves them (playlists-format.md section 3, verified on 0.10.8): a map is a stock map (title, file name or ID; Halo
3's first, then Reach's, CE's, Halo 2's), a mod map (`workshop:<item>/<map>`, "<title> · Custom", its title or file
name), or a Forge layout or map variant in content/Maps (by path or name); a game is a built-in game type (name or
key), a game mode in content/Game Modes (.json, .rmeg, .bin; by path or name) or one of MCC's variants in the game
folder. A co-op entry names a mission. Each answer: found (where), missing, found but the server will skip it (why),
or not known to Huragok (the program also reads this PC's Steam Workshop folder and MCC's own variants: its check
says for sure).

The server's own program has the last word (program/plays.py plays_verdict: `dedicated check` of a draft and
`dedicated playlist list`): its refusals are tied to their file and game, each paired with the "Skipped content file
…: reason" line of a file the game names. Then what it said while playing (servers/logs.py: a game skipped, B13).
Then Huragok's own cheap judgement of what the program lets through (README section 3), each with why:
- more than 23 mod maps or 16 mods in one playlist (the guide's limit; 24 from 9 mods passed check on 0.10.8);
- two mod maps with one map ID in one playlist (the server leaves one out of a session);
- a layout made on another version of its mod map (its base_map.id isn't the installed map's ID);
- a saved map past 9,999 objects (Project Reclaimer 0.10's limit);
- options or rules on a Combat Evolved or Halo 2 map (the program refuses them there: games/plays.py takes);
- a choice label the server's version refuses (games/halo3_options.py LABEL_CHANGES);
- a ballot picture that isn't in content/Images (the program doesn't look);
- two game mode files with the name a game uses (the program picks one, and which isn't known);
- `[modes]` lines: a game mode read without part of it.

Statuses, from best to worst: ok, unknown, check, missing, skip, skipped (seen while it played), refused.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.files import write_atomic
from ..core.tomledit import read_toml
from ..games import files as game_files
from ..games import odst
from ..games.bungie import GAMES, HALO3_GAME_TYPES, game_of_map
from ..games.gamemodes import (
    GAME_MODES, IMAGES, MAPS, OBJECTS_MOST, GameModeFile, LayoutFile, list_game_modes, list_layouts,
)
from ..games.halo3 import (
    MISSION_STARTS, MISSIONS, PlaylistError, coop_kind, entry_game, entry_label_fixes, game_details, game_label,
    load_playlist, playlist_files,
)
from ..games.plays import plays_block, plays_kind, plays_of, playlist_refs
from ..program.plays import Problem, Verdict
from ..program.settings import path_setting
from .catalogue import DisabledModsError, Mod, disabled_mods, installed_mods, mod_package
from .mccimport import content_root_of

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig

log = logging.getLogger(__name__)

STATUSES = ("ok", "unknown", "check", "missing", "skip", "skipped", "refused")  # from best to worst
MOD_MAPS_MOST, MODS_MOST = 23, 16  # the guide's limit for one playlist (0.10.8's check doesn't hold it)
IMAGE_MOST = 4 << 20  # a ballot picture, at most (the guide)
SOURCE = "plays"  # the warnings catalogue's source for what this finds
VERDICTS = "plays-checked.json"  # in records/: each server's last verdict of its own program
SKIPS = "games-skipped.json"  # in records/: the games each server said it couldn't play
SKIP_DAYS = 7  # how long a skip is remembered
SKIPS_MOST = 200  # per server
GAME_KEYS = {"slayer": "Slayer", "ctf": "Capture the Flag", "koth": "King of the Hill", "oddball": "Oddball",
             "vip": "VIP", "juggernaut": "Juggernaut", "territories": "Territories", "assault": "Assault",
             "infection": "Infection", "escalation": "Escalation Slayer", "forge": "Forge"}
WORKSHOP = "workshop:"


def worst(statuses: list[str]) -> str:
    return max(statuses, key=STATUSES.index, default="ok")


@dataclass(frozen=True)
class Ref:
    """One thing a game names, and whether the server has it."""

    role: str  # map, game, base_map, preview_image, mission
    written: str  # as the file writes it
    kind: str = ""  # stock map, mod map, layout, map variant, game type, game mode, MCC variant, picture, mission
    status: str = "found"  # found, missing, skip (found, but the server will skip it), unknown (not known to Huragok)
    where: str = ""  # where it's found: "Halo 3's maps", "mod HugeGrass SPR", "Maps/SPR/GroundWar.json"
    why: str = ""  # for missing, skip and unknown
    path: Path | None = None  # the file, when it's one in the content folder
    stem: str = ""  # a mod map's file name (for the mod-map counts and map IDs)
    mod: str = ""  # the mod it's from


@dataclass(frozen=True)
class SkipSeen:
    """A game the server said it couldn't play (servers/logs.py GAME_SKIPS, GAME_LINES), kept a week."""

    what: str  # retry, skipped, unplayable, none_playable, outside, load_failed, no_results
    game: str  # as the server named it ("Grand Prix on Daybreak")
    at: float  # when Huragok read it
    reason: str = ""
    times: int | None = None
    skip: str = ""  # how long it's skipped for ("30 minutes")
    active: bool = True  # still skipped (false once its skip ran out, or the server started again)

    def to_json(self) -> dict[str, Any]:
        return {"what": self.what, "game": self.game, "at": self.at, "reason": self.reason, "times": self.times,
                "skip": self.skip, "active": self.active}

    @classmethod
    def from_json(cls, data: Any) -> SkipSeen | None:
        if not isinstance(data, dict) or not isinstance(data.get("game"), str):
            return None
        times = data.get("times")
        return cls(str(data.get("what") or ""), data["game"], float(data.get("at") or 0.0), str(data.get("reason") or ""),
                   times if isinstance(times, int) else None, str(data.get("skip") or ""), bool(data.get("active")))


@dataclass
class GameHealth:
    """One game of a playlist (or what a server's own block plays), its names resolved."""

    number: int  # its place in its playlist, from 1; 0: the server's own `map`/`game`/`variant`/co-op block
    label: str  # "Team Slayer on Guardian", "Campaign: Sierra 117 (heroic)"
    kind: str  # multiplayer, campaign, odst
    details: list[str] = field(default_factory=list)  # weight, lobby sizes, teams, options… in words
    refs: list[Ref] = field(default_factory=list)
    status: str = "ok"
    reasons: list[str] = field(default_factory=list)  # why, worst first
    skips: list[SkipSeen] = field(default_factory=list)  # what the server said of it while playing
    data: dict[str, Any] = field(default_factory=dict, repr=False)  # the entry, as written
    names: set[str] = field(default_factory=set, repr=False)  # how the server may name it (folded): its skip lines

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def judge(self, status: str, reason: str) -> None:
        if STATUSES.index(status) > STATUSES.index(self.status):
            self.status = status
            self.reasons.insert(0, reason)
        elif reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class PlaylistHealth:
    """A playlist file of a server's: its games, whether the server plays it, and what's wrong."""

    path: Path
    relative: str  # "playlists/races.playlist.json"
    name: str = ""
    in_use: str = ""  # how the server plays it ("playlist =", "playlists = 1-8"); "" when it doesn't
    vote: str = ""  # "20 s", "15-30 s, follows the lobby size"
    players: str = ""  # its lobby sizes
    games: list[GameHealth] = field(default_factory=list)
    problem: str = ""  # Huragok can't read it: why (the program can't either)
    refused: list[str] = field(default_factory=list)  # the program's words about the whole file

    @property
    def status(self) -> str:
        if self.problem or self.refused:
            return "refused" if self.refused else "missing" if not self.path.is_file() else "skip"
        return worst([g.status for g in self.games])

    @property
    def reason(self) -> str:
        if self.problem or self.refused:
            return "; ".join([*self.refused, *([self.problem] if self.problem else [])])
        bad = [g for g in self.games if g.status not in ("ok", "unknown")]
        bad.sort(key=lambda g: -STATUSES.index(g.status))
        return "; ".join(f"game {g.number} ({g.label}): {g.reason}" for g in bad[:3]) + (
            f"; and {len(bad) - 3} more" if len(bad) > 3 else "")


@dataclass
class FileHealth:
    """A game mode or a Forge layout in the server's content folder, and what uses it."""

    path: Path
    relative: str  # "Game Modes/SPR/BR Fiesta CTF.json"
    name: str
    kind: str  # json, rmeg, bin (game modes); json, mvar (layouts)
    what: str  # game mode, layout
    details: str = ""  # "Capture the Flag · scripted mode (.rmeg)", "1,675 objects · on HugeGrass SPR"
    used_by: list[str] = field(default_factory=list)  # "playlists/races.playlist.json game 3", "variant ="
    in_use: bool = False  # what the server plays names it
    status: str = "ok"
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

    def judge(self, status: str, reason: str) -> None:
        if STATUSES.index(status) > STATUSES.index(self.status):
            self.status = status
            self.reasons.insert(0, reason)
        elif reason not in self.reasons:
            self.reasons.append(reason)


Finding = tuple[str, str, Any, dict[str, Any]]  # (rule, key, the file it's about, fields): Warnings.sync's


@dataclass
class ServerFiles:
    """Everything one server plays and has, for the screens: G's file health, its playlists, game modes and layouts."""

    server: str
    plays: str  # as the dashboard says it: "playlist races", "playlists by lobby size: small 1-8, big 9+"
    kind: str  # games/plays.py plays_kind
    setting: str  # what its own file says: 'playlist = "playlists/races.playlist.json"'
    from_defaults: bool = False  # the server's block names nothing to play: [defaults] does
    own: GameHealth | None = None  # what a `map`/`game`/`variant`/co-op block plays (None for playlists)
    playlists: list[PlaylistHealth] = field(default_factory=list)
    game_modes: list[FileHealth] = field(default_factory=list)
    layouts: list[FileHealth] = field(default_factory=list)
    verdict: Verdict | None = None  # its own program's last word (None: not asked yet)
    refused: list[str] = field(default_factory=list)  # the program's words about its block, not a playlist's game
    skips: list[SkipSeen] = field(default_factory=list)  # every game it said it couldn't play this past week
    findings: list[Finding] = field(default_factory=list)  # what the warnings catalogue hears (source "plays")
    config_path: Path | None = None  # its dedicated.toml

    @property
    def in_use(self) -> list[PlaylistHealth]:
        return [p for p in self.playlists if p.in_use]


# The content a server has -------------------------------------------------------------------------------------------

@dataclass
class ContentIndex:
    """What a server's folders hold, read once for a whole report."""

    folder: Path
    content: Path
    game: Path | None
    game_modes: list[GameModeFile]
    layouts: list[LayoutFile]
    mods: list[Mod]  # Halo 3's and the other games', switched-off ones too
    off: set[str]  # mod folders switched off (disabled-mods.json), as mod_package names them
    images: dict[str, Path]  # content/Images, by file name folded
    variants: dict[str, str]  # MCC's own game variants in the game folder: stem folded -> "Halo 3's" / "Reach's"
    _skipped: tuple[int, dict[str, str]] | None = None  # the verdict's left-out files by full path, for one verdict

    def mod_off(self, mod: Mod) -> bool:
        return mod_package(self.content, mod).casefold() in self.off

    def skipped(self, verdict: Verdict | None) -> dict[str, str]:
        """The files the program said it leaves out, by their full path (normalised): its reason for each."""
        if verdict is None:
            return {}
        if self._skipped is None or self._skipped[0] != id(verdict):
            found = {}
            for printed, reason in verdict.skipped.items():
                where = Path(printed) if Path(printed).is_absolute() else self.folder / printed
                found[_norm(where)] = reason
            self._skipped = (id(verdict), found)
        return self._skipped[1]


def content_index(folder: Path, content: Path, game: Path | None) -> ContentIndex:
    mods = installed_mods(content)
    try:
        off = {p.strip().strip("/").replace("\\", "/").casefold() for p in disabled_mods(content)}
    except DisabledModsError:
        off = set()
    images: dict[str, Path] = {}
    if (content / IMAGES).is_dir():
        for path in (content / IMAGES).rglob("*"):
            if path.is_file():
                images.setdefault(path.name.casefold(), path)
                images.setdefault(path.relative_to(content / IMAGES).as_posix().casefold(), path)
    variants: dict[str, str] = {}
    for key, folder_name in (("halo3", "halo3"), ("reach", "haloreach")):
        for sub in ("game_variants", "hopper_game_variants"):
            where = game / folder_name / sub if game is not None else None
            if where is not None and where.is_dir():
                for path in where.glob("*.bin"):
                    variants.setdefault(path.stem.casefold(), f"{GAMES[key].title}'s own variants")
    return ContentIndex(folder, content, game, list_game_modes(content), list_layouts(content), mods, off, images,
                        variants)


def _norm(path: Path | str) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _in_content(text: str, root: str) -> str | None:
    """A name written as a path inside the content folder ("Maps/SPR/x.json", "content/Game Modes/x.rmeg"), as the
    path from the content folder; None for a plain name."""
    path = text.strip().replace("\\", "/")
    if path.casefold().startswith("content/"):
        path = path[len("content/"):]
    if path.casefold().startswith(root.casefold() + "/"):
        return path
    return None


# Resolving names -----------------------------------------------------------------------------------------------------

def _skipped_reason(index: ContentIndex, path: Path | None, verdict: Verdict | None) -> str | None:
    """The program's own reason for leaving a file out ("Skipped content file …: reason"), matched by path."""
    if path is None or verdict is None or not verdict.skipped:
        return None
    return index.skipped(verdict).get(_norm(path))


def _mod_maps(index: ContentIndex) -> list[tuple[Mod, str, str]]:
    """(mod, map title, map file name) for every mod map in content/Mods."""
    return [(mod, title, stem) for mod in index.mods for title, stem in zip(mod.maps, mod.stems)]


def resolve_map(text: Any, index: ContentIndex, verdict: Verdict | None = None, role: str = "map") -> Ref:
    """What a `map` (or `base_map`) names, as the program finds it."""
    written = str(text or "").strip()
    if not written:
        return Ref(role, "", status="missing", why="no map is named")
    folded = written.casefold()
    stock = game_of_map(written)
    if stock is not None:
        ref = Ref(role, written, "stock map", "found", f"{stock.title}'s maps")
        if stock.key != "halo3" and index.game is not None and stock.key in game_files.HAS \
                and not game_files.has(index.game, stock.key):
            return Ref(role, written, "stock map", "skip", ref.where,
                       f"{stock.title}'s files aren't in its game folder ({index.game}), so the server has no such map")
        return ref
    if folded.startswith(WORKSHOP):
        item, _, stem = written[len(WORKSHOP):].partition("/")
        for mod, title, map_stem in _mod_maps(index):
            if str(mod.workshop_id) == item.strip() and map_stem.casefold() == stem.strip().casefold():
                return _mod_ref(role, written, mod, title, map_stem, index)
        return Ref(role, written, "mod map", "unknown", why=f"Workshop item {item} isn't in its content/Mods: the server "
                   "fetches the items its [workshop] items lists, and reads this PC's Steam Workshop folder too")
    named = folded.removesuffix(" · custom")
    for mod, title, map_stem in _mod_maps(index):
        if (mod.game or "halo3") == "halo3" and named in (title.casefold(), map_stem.casefold()):
            return _mod_ref(role, written, mod, title, map_stem, index)
    other = entry_game({"map": written})
    if other is not None:  # another game's mod map, named `halo1_<stem>_<hex>` as the listing names it
        return Ref(role, written, "mod map", "unknown", why=f"{other.title}'s mod map: Huragok doesn't read those; the "
                   "server's own check does")
    inside = _in_content(written, MAPS)
    for layout in index.layouts:
        if (inside is not None and layout.relative.casefold() == inside.casefold()) or \
                (inside is None and folded in (layout.name.casefold(), layout.path.stem.casefold())):
            return _layout_ref(role, written, layout, index, verdict)
    if inside is not None:
        return Ref(role, written, "layout", "missing", why=f"content/{inside} isn't there")
    return Ref(role, written, "", "unknown", why="not a stock map, a mod in its content/Mods or a layout in its "
               "content/Maps (the server also reads this PC's Steam Workshop folder: its check says for sure)")


def _mod_ref(role: str, written: str, mod: Mod, title: str, stem: str, index: ContentIndex) -> Ref:
    where = f"mod {mod.title}" + (f" (Workshop {mod.workshop_id})" if mod.workshop_id else "")
    if index.mod_off(mod):
        return Ref(role, written, "mod map", "skip", where, f"{mod.title} is switched off (content/disabled-mods.json)",
                   mod.folder, stem, mod.title)
    return Ref(role, written, "mod map", "found", where, "", mod.folder, stem, mod.title)


def _layout_ref(role: str, written: str, layout: LayoutFile, index: ContentIndex, verdict: Verdict | None) -> Ref:
    kind = "map variant" if layout.kind == "mvar" else "layout"
    said = _skipped_reason(index, layout.path, verdict)
    if said:
        return Ref(role, written, kind, "skip", layout.relative, f"the server leaves {layout.relative} out: {said}",
                   layout.path)
    if layout.problem:
        return Ref(role, written, kind, "skip", layout.relative, f"{layout.relative}: {layout.problem}", layout.path)
    if layout.base_stem and game_of_map(layout.base_stem) is None and not any(
            stem.casefold() == layout.base_stem.casefold() for _, _, stem in _mod_maps(index)):
        title = layout.base_title or layout.base_stem
        return Ref(role, written, kind, "unknown", layout.relative,
                   f"made on {title} ({layout.base_stem}.map), which isn't in its content/Mods: it plays only if that "
                   "mod is in this PC's Steam Workshop folder", layout.path, layout.base_stem)
    stem = layout.base_stem if layout.base_stem and game_of_map(layout.base_stem) is None else ""
    mod = next((m.title for m, _, s in _mod_maps(index) if stem and s.casefold() == stem.casefold()), "")
    return Ref(role, written, kind, "found", layout.relative, "", layout.path, stem, mod)


def resolve_game(text: Any, index: ContentIndex, verdict: Verdict | None = None) -> Ref:
    """What a `game` names, as the program finds it: a built-in game type first, then a game mode by path or name, then
    MCC's own variants."""
    written = str(text or "").strip() or "Slayer"
    folded = written.casefold()
    types = {t.casefold(): t for game in GAMES.values() for t in game.game_types} | \
        {t.casefold(): t for t in HALO3_GAME_TYPES} | GAME_KEYS
    if folded in types:
        return Ref("game", written, "game type", "found", "built in")
    inside = _in_content(written, GAME_MODES)
    if inside is not None:
        found = [m for m in index.game_modes if m.relative.casefold() == inside.casefold()]
        if not found:
            return Ref("game", written, "game mode", "missing", why=f"content/{inside} isn't there")
        return _mode_ref(written, found[0], index, verdict)
    found = [m for m in index.game_modes if folded in (m.name.casefold(), m.path.stem.casefold())]
    if found:
        named = [m for m in found if m.name.casefold() == folded] or found
        ref = _mode_ref(written, named[0], index, verdict)
        return ref
    if folded in index.variants:
        return Ref("game", written, "MCC variant", "found", index.variants[folded])
    return Ref("game", written, "", "unknown", why="not a built-in game type or a game mode in its content/Game Modes "
               "(the server also knows MCC's own variants: its check says for sure)")


def _mode_ref(written: str, mode: GameModeFile, index: ContentIndex, verdict: Verdict | None) -> Ref:
    said = _skipped_reason(index, mode.path, verdict)
    if said:
        return Ref("game", written, "game mode", "skip", mode.relative, f"the server leaves {mode.relative} out: {said}",
                   mode.path)
    if mode.problem:
        return Ref("game", written, "game mode", "skip", mode.relative, f"{mode.relative}: {mode.problem}", mode.path)
    return Ref("game", written, "game mode", "found", mode.relative, "", mode.path)


def resolve_image(text: Any, index: ContentIndex) -> Ref:
    written = str(text or "").strip()
    path = index.images.get(written.replace("\\", "/").casefold())
    if path is None:
        return Ref("preview_image", written, "picture", "missing",
                   why=f"content/{IMAGES} has no {written}: the ballot shows no picture (the program's check doesn't "
                       "look)")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > IMAGE_MOST:
        return Ref("preview_image", written, "picture", "skip", f"{IMAGES}/{path.name}",
                   f"{size / (1 << 20):.1f} MB, over the guide's 4 MiB for a ballot picture", path)
    return Ref("preview_image", written, "picture", "found", f"{IMAGES}/{path.name}", "", path)


def resolve_mission(entry: dict[str, Any], kind: str, index: ContentIndex, *, names: bool = False) -> Ref:
    """A co-op entry's mission: Halo 3's campaign or ODST's. A playlist's campaign entry takes the mission's number
    only (0.10.8); `names`: a server's own `campaign = { … }`, which takes its name too (0.10.7)."""
    setup = entry.get(kind) if isinstance(entry.get(kind), dict) else {}
    mission = setup.get("mission")
    written = str(mission)
    if kind == "campaign":
        if names and isinstance(mission, str):
            mission = next((n for n, t in MISSIONS.items() if t.casefold() == mission.strip().casefold()),
                           int(mission) if mission.strip().isdecimal() else mission)
        if not isinstance(mission, int) or isinstance(mission, bool):
            return Ref("mission", written, "mission", "skip", why="a playlist's campaign entry takes the mission's "
                       "number (3010 for Sierra 117), not its name (0.10.8)")
        if mission not in MISSIONS:
            return Ref("mission", written, "mission", "skip", why=f"Halo 3's campaign has no mission {mission}")
        start = setup.get("insertion", 0)
        if isinstance(start, int) and start > MISSION_STARTS.get(mission, 0):
            return Ref("mission", written, "mission", "skip", MISSIONS[mission],
                       f"{MISSIONS[mission]} has no start {start} (0 to {MISSION_STARTS[mission]})")
        if index.game is not None and not game_files.has(index.game, "campaign"):
            return Ref("mission", written, "mission", "skip", MISSIONS[mission],
                       f"Halo 3's campaign files aren't in its game folder ({index.game})")
        return Ref("mission", written, "mission", "found", f"Halo 3's campaign: {MISSIONS[mission]}")
    if odst.is_firefight(setup):
        place = next((t for m, i, t in odst.FIREFIGHT_MAPS if str(m) == str(mission)
                      and str(i) == str(setup.get("insertion"))), None)
        if place is None:
            return Ref("mission", written, "Firefight map", "skip", why="no ODST Firefight map is that mission and "
                       f"insertion ({mission}, {setup.get('insertion', 'none')}): the program says \"Unknown Firefight "
                       "map\"")
        found = Ref("mission", written, "Firefight map", "found", f"ODST Firefight: {place}")
    else:
        title = next((t for m, t, _ in odst.MISSIONS if str(m) == str(mission)), None)
        if title is None:
            return Ref("mission", written, "mission", "skip", why=f"ODST has no mission {mission}")
        found = Ref("mission", written, "mission", "found", f"ODST's campaign: {title}")
    if index.game is not None and not game_files.has(index.game, "odst"):
        return Ref("mission", written, found.kind, "skip", found.where,
                   f"Halo 3: ODST's files aren't in its game folder ({index.game})")
    return found


def resolve_game_entry(number: int, entry: dict[str, Any], index: ContentIndex,
                       verdict: Verdict | None = None) -> GameHealth:
    """One playlist game (or a server's own block), every name it gives resolved."""
    kind = coop_kind(entry)
    game = GameHealth(number, game_label(entry), kind, game_details(entry), data=entry)
    if kind != "multiplayer":
        game.refs.append(resolve_mission(entry, kind, index, names=number == 0))
    else:
        map_ref = resolve_map(entry.get("map", "Guardian"), index, verdict)
        game_ref = resolve_game(entry.get("game"), index, verdict)
        game.refs += [map_ref, game_ref]
        if entry.get("base_map"):
            game.refs.append(resolve_map(entry["base_map"], index, verdict, "base_map"))
        game.names = _names(entry, map_ref, game_ref, index)
    if entry.get("preview_image"):
        game.refs.append(resolve_image(entry["preview_image"], index))
    for ref in game.refs:
        if ref.status != "found":
            game.judge({"missing": "missing", "skip": "skip", "unknown": "unknown"}[ref.status],
                       f"{_role(ref.role)} {ref.written}: {ref.why}")
    return game


def _role(role: str) -> str:
    return {"base_map": "base map", "preview_image": "picture"}.get(role, role)


def _names(entry: dict[str, Any], map_ref: Ref, game_ref: Ref, index: ContentIndex) -> set[str]:
    """How the server may name this game in its lines: "<its name, or its game's> on <its map's title>"."""
    maps = {str(entry.get("map") or "")}
    stock = game_of_map(map_ref.written)
    if stock is not None:
        maps |= {m.title for m in stock.maps if map_ref.written.casefold() in (m.title.casefold(), m.stem.casefold(),
                                                                               str(m.map_id))}
    if map_ref.path is not None:
        layout = next((l for l in index.layouts if l.path == map_ref.path), None)
        if layout is not None:
            maps.add(layout.name)
    if map_ref.stem:
        maps |= {title for _, title, stem in _mod_maps(index) if stem == map_ref.stem}
    games = {str(entry.get("name") or ""), str(entry.get("game") or "Slayer"),
             GAME_KEYS.get(str(entry.get("game") or "").casefold(), "")}
    if game_ref.path is not None:
        mode = next((m for m in index.game_modes if m.path == game_ref.path), None)
        if mode is not None:
            games.add(mode.name)
    return {f"{g} on {m}".casefold() for g in games if g for m in maps if m}


# One server's report ------------------------------------------------------------------------------------------------

def _block_of(data: dict[str, Any], name: str) -> dict[str, Any]:
    blocks = data.get("server", []) if isinstance(data.get("server"), list) else []
    return next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == name.lower()), {})


def _setting(block: dict[str, Any]) -> str:
    """What the block names to play, written as its file has it."""
    from ..core.tomledit import _toml_value

    keys = [k for k in ("playlist", "playlists", "variant", "map", "game", "base_map", "campaign", "odst", "reach")
            if k in block]
    if not keys:
        return "(nothing: Slayer on Guardian)"
    if "playlists" in keys and isinstance(block["playlists"], list):
        return f"playlists = [{len(block['playlists'])} entries]"
    return ", ".join(f"{k} = {_toml_value(block[k])}" for k in keys)


def server_files(server: ServerConfig, *, verdict: Verdict | None = None, skips: list[SkipSeen] | None = None,
                 version: str | None = None, config_name: str = "dedicated.toml") -> ServerFiles:
    """One server's report: what its own file says it plays, every playlist file it has (and the ones it names
    elsewhere), each game's names resolved, its game modes and layouts with what uses them, the program's last
    verdict and the games it said it couldn't play (`skips`), and every finding for the warnings catalogue. Reads
    files only (a content folder of hundreds of layouts: in a thread, never on the event loop). `version`: the
    program the server runs, for the labels it refuses."""
    try:
        data = read_toml(server.config_path)
    except (OSError, ValueError) as error:
        files = ServerFiles(server.name, server.plays, server.plays_kind, f"(its {server.config_path.name} can't be read)")
        files.refused.append(f"{server.config_path.name} can't be read: {error}")
        return files
    own_block = _block_of(data, server.name)
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else None
    block = plays_block(own_block, defaults)
    content = server.content_dir or content_root_of(server.folder, config_name)
    game = path_setting(server.folder, data, "game")
    index = content_index(server.folder, content, game)
    kind = plays_kind(block)
    files = ServerFiles(server.name, plays_of(block), kind, _setting(block), block is not own_block, verdict=verdict,
                        skips=list(skips or []), config_path=server.config_path)

    refs = playlist_refs(block)
    used = {_norm(ref.path(server.folder)): ref for ref in refs}
    paths: dict[str, Path] = {}
    for path in [*playlist_files(server.folder), *(ref.path(server.folder) for ref in refs)]:
        paths.setdefault(_norm(path), path)
    for path in paths.values():
        files.playlists.append(_playlist(path, server.folder, used.get(_norm(path)), index, verdict, version))
    if kind in ("map", "variant", "default", "campaign", "odst"):
        entry = dict(block)
        if kind == "variant":
            entry["game"] = str(block.get("variant") or "")
        files.own = resolve_game_entry(0, {k: v for k, v in entry.items() if k in (
            "map", "game", "base_map", "campaign", "odst")} or {"map": "Guardian", "game": "Slayer"}, index, verdict)
        files.own.label = plays_of(block)
    _program_words(files, server, verdict)
    _attach_skips(files, skips or [])
    _file_lists(files, index, verdict)
    files.findings = _findings(files, index, verdict, version)
    return files


def _playlist(path: Path, folder: Path, ref: Any, index: ContentIndex, verdict: Verdict | None,
              version: str | None) -> PlaylistHealth:
    try:
        relative = path.relative_to(folder).as_posix()
    except ValueError:
        relative = path.as_posix()
    how = ""
    if ref is not None:
        how = "playlist =" if ref.how == "playlist" else f"playlists = {ref.players or 'its own lobby sizes'}"
    report = PlaylistHealth(path, relative, path.name.removesuffix(".playlist.json"), how)
    try:
        playlist = load_playlist(path)
    except PlaylistError as error:
        report.problem = str(error)
        return report
    report.name, report.vote, report.players = playlist.name, playlist.vote, playlist.players
    for number, entry in enumerate(playlist.games, 1):
        report.games.append(resolve_game_entry(number, entry, index, verdict))
    return report


def _same_file(folder: Path, written: str | None, path: Path) -> bool:
    if not written:
        return False
    where = Path(written)
    return _norm(where if where.is_absolute() else folder / written) == _norm(path)


def _program_words(files: ServerFiles, server: ServerConfig, verdict: Verdict | None) -> None:
    """The program's own refusals, each on its file and game, its "Skipped content file" reason paired in."""
    if verdict is None:
        return
    by_path = {_norm(p.path): p for p in files.playlists}

    def place(problem: Problem, in_use: bool) -> None:
        playlist = next((p for p in by_path.values() if _same_file(server.folder, problem.file, p.path)), None)
        if playlist is None and problem.file is None and problem.game and len(files.in_use) == 1:
            playlist = files.in_use[0]  # 0.9 names the game without its file: the one playlist it plays
        if playlist is None:
            if in_use and problem.text not in files.refused:
                files.refused.append(problem.text)
            return
        game = next((g for g in playlist.games if g.number == problem.game), None) if problem.game else None
        words = problem.text.removeprefix(f"Game {problem.game}:").strip() if problem.game else problem.text
        if game is None:
            if words not in playlist.refused:
                playlist.refused.append(words)
            return
        also = [f"{r.written} is left out: {r.why.split(': ', 1)[-1]}" for r in game.refs
                if r.status == "skip" and r.why.startswith("the server leaves")]
        game.judge("refused", words + (f" ({'; '.join(also)})" if also else ""))

    for problem in verdict.refusals:
        place(problem, True)
    for file, problems in verdict.listed.items():
        for problem in problems:
            place(Problem(problem.text, problem.game, problem.file or file), False)


def _attach_skips(files: ServerFiles, skips: list[SkipSeen]) -> None:
    """The games the server said it couldn't play, on the games they name (by the server's own words for them)."""
    games = [g for p in files.in_use for g in p.games] + ([files.own] if files.own else [])
    for skip in skips:
        folded = skip.game.casefold()
        game = next((g for g in games if folded in g.names or folded == g.label.casefold()), None)
        if game is None:
            continue
        game.skips.append(skip)
        if skip.active and skip.what in ("skipped", "unplayable", "no_results"):
            game.judge("skipped", _skip_words(skip))


def _skip_words(skip: SkipSeen) -> str:
    when = time.strftime("%H:%M", time.localtime(skip.at)) if skip.at else ""
    if skip.what == "skipped":
        return f"skipped for {skip.skip or 'a while'}: its map failed to load ({skip.reason}, {when})"
    if skip.what == "unplayable":
        return f"skipped from now on: it can't be played ({skip.reason}, {when})"
    if skip.what == "no_results":
        return f"passed over: it ended with no results {skip.times} times in a row ({when})"
    return f"{skip.what} ({skip.reason}, {when})"


def _file_lists(files: ServerFiles, index: ContentIndex, verdict: Verdict | None) -> None:
    """Its game modes and layouts, each with what uses it (from the games resolved) and what's wrong with it."""
    uses: dict[str, list[tuple[str, bool]]] = {}
    for playlist in files.playlists:
        for game in playlist.games:
            for ref in game.refs:
                if ref.path is not None and ref.kind in ("game mode", "layout", "map variant"):
                    uses.setdefault(_norm(ref.path), []).append((f"{playlist.relative} game {game.number}",
                                                                 bool(playlist.in_use)))
    if files.own is not None:
        for ref in files.own.refs:
            if ref.path is not None:
                uses.setdefault(_norm(ref.path), []).append((files.setting, True))
    for mode in index.game_modes:
        details = _mode_details(mode)
        report = FileHealth(mode.path, mode.relative, mode.name, mode.kind, "game mode", details)
        _judge_file(report, mode.problem, uses, index, verdict)
        files.game_modes.append(report)
    for layout in index.layouts:
        bits = [f"{layout.objects:,} objects" if layout.objects is not None else ""]
        if layout.base_stem:
            bits.append(f"on {layout.base_title or layout.base_stem}")
        report = FileHealth(layout.path, layout.relative, layout.name, layout.kind,
                            "map variant" if layout.kind == "mvar" else "layout", " · ".join(b for b in bits if b))
        _judge_file(report, layout.problem, uses, index, verdict)
        if layout.objects is not None and layout.objects > OBJECTS_MOST:
            report.judge("check", f"{layout.objects:,} objects, over Project Reclaimer 0.10's {OBJECTS_MOST:,}")
        files.layouts.append(report)


def _mode_details(mode: GameModeFile) -> str:
    from ..games.halo3_options import mode_name

    if mode.kind == "bin":
        return "MCC game variant (.bin)"
    bits = [mode_name(mode.mode)] if mode.mode else []
    if mode.kind == "rmeg":
        bits.append("scripted mode (.rmeg)")
    elif mode.scripted:
        bits.append("scripted")
    if mode.bundled:
        bits.append("ships with the program")
    return " · ".join(bits)


def _judge_file(report: FileHealth, problem: str, uses: dict[str, list[tuple[str, bool]]], index: ContentIndex,
                verdict: Verdict | None) -> None:
    for text, in_use in uses.get(_norm(report.path), []):
        report.used_by.append(text + ("" if in_use else " (not played now)"))
        report.in_use = report.in_use or in_use
    said = _skipped_reason(index, report.path, verdict)
    if said:
        report.judge("skip", f"the server leaves it out: {said}")
    elif problem:
        report.judge("skip", problem)


# The findings, for the warnings catalogue ---------------------------------------------------------------------------

def _where(playlist: PlaylistHealth | None, game: GameHealth) -> str:
    if playlist is None:
        return f"what it plays ({game.label})"
    return f"{playlist.relative}, game {game.number} ({game.label})"


def _findings(files: ServerFiles, index: ContentIndex, verdict: Verdict | None, version: str | None) -> list[Finding]:
    found: list[Finding] = []
    config = files.config_path

    def add(rule: str, key: str, about: Any, /, **fields: Any) -> None:
        found.append((rule, key, about, fields))

    # The program's own words
    for text in files.refused:
        add("plays-refused", f"server:{text[:60]}", config, what=f"its {config.name if config else 'settings'}", why=text,
            effect=_effect(True))
    for playlist in files.playlists:
        for text in playlist.refused:
            rule = "plays-refused" if playlist.in_use else "plays-refused-unused"
            add(rule, f"{playlist.relative}:{text[:60]}", playlist.path, what=playlist.relative, why=text,
                effect=_effect(bool(playlist.in_use)))
        for game in playlist.games:
            if game.status == "refused":
                rule = "plays-refused" if playlist.in_use else "plays-refused-unused"
                add(rule, f"{playlist.relative}#{game.number}", playlist.path, what=_where(playlist, game),
                    why=game.reasons[0], effect=_effect(bool(playlist.in_use)))
    if verdict is not None and not verdict.asked and verdict.unasked:
        add("plays-not-checked", "", None, why=verdict.unasked)

    # What it said while it played, each game it still skips (matched to a game of its playlist or not)
    for skip in files.skips:
        if skip.active:
            found.append(_skip_finding(skip))

    # Huragok's own judgement, for what the server plays
    named = {_norm(r.path) for p in files.in_use for g in p.games for r in g.refs if r.path is not None}
    games = [(p, g) for p in files.in_use for g in p.games] + ([(None, files.own)] if files.own else [])
    for playlist, game in games:
        about = playlist.path if playlist is not None else None
        where = _where(playlist, game)
        for ref in game.refs:
            if ref.role == "preview_image" and ref.status != "found":
                add("plays-image", f"{where}:{ref.written}", about, where=where, image=ref.written, why=ref.why)
            if ref.role == "game" and ref.status == "found" and ref.kind == "game mode":
                same = [m for m in index.game_modes if m.name.casefold() == ref.written.casefold()]
                if len(same) > 1:
                    add("plays-same-name", f"{where}:{ref.written}", about, where=where, name=ref.written,
                        count=len(same), files=", ".join(m.relative for m in same))
            if ref.role == "map" and ref.written.casefold().startswith(WORKSHOP):
                add("plays-workshop-names", ref.written, about, where=where, name=ref.written)
        entry = game.data
        other = entry_game(entry) if game.kind == "multiplayer" else None
        if other is not None and other.own_rules_only and (entry.get("options") or entry.get("rules")):
            keys = ", ".join(k for k in ("options", "rules") if entry.get(k))
            add("plays-options-own-rules", where, about, where=where, keys=keys, map=entry.get("map"), game=other.title)
        if game.kind == "multiplayer" and version:
            for key, label, taken in entry_label_fixes(entry, version):
                add("plays-label-refused", f"{where}:{key}", about, where=where, option=key, label=label, taken=taken,
                    version=version)
    for playlist in files.in_use:
        stems: dict[str, str] = {}
        for game in playlist.games:
            for ref in game.refs:
                if ref.stem and ref.role in ("map", "base_map"):
                    stems.setdefault(ref.stem.casefold(), ref.mod or ref.stem)
        mods = set(stems.values())
        if len(stems) > MOD_MAPS_MOST or len(mods) > MODS_MOST:
            add("plays-too-many-mod-maps", playlist.relative, playlist.path, file=playlist.relative, maps=len(stems),
                mods=len(mods))
        by_id: dict[int, list[str]] = {}
        for stem in stems:
            map_id = _map_id(index, stem)
            if map_id:
                by_id.setdefault(map_id, []).append(stem)
        for map_id, clash in by_id.items():
            if len(clash) > 1:
                add("plays-map-id-clash", f"{playlist.relative}:{map_id}", playlist.path, file=playlist.relative,
                    maps=" and ".join(f"{s}.map" for s in sorted(clash)), id=map_id)
    for layout in files.layouts:
        if _norm(layout.path) not in named and not layout.in_use:
            continue
        data = next((l for l in index.layouts if l.path == layout.path), None)
        if data is None:
            continue
        if data.objects is not None and data.objects > OBJECTS_MOST:
            add("plays-layout-objects", layout.relative, layout.path, layout=layout.relative, objects=f"{data.objects:,}",
                most=f"{OBJECTS_MOST:,}")
        if data.base_stem and data.base_id:
            now = _map_id(index, data.base_stem.casefold())
            mod = next((m.title for m, _, s in _mod_maps(index) if s.casefold() == data.base_stem.casefold()), "")
            if now and now != data.base_id:
                add("plays-layout-other-version", layout.relative, layout.path, layout=layout.relative,
                    stem=data.base_stem, made=data.base_id, now=now, mod=mod or data.base_stem)
    if verdict is not None:
        for path, note in verdict.modes:
            add("plays-mode-note", f"{path}:{note[:60]}", None, file=_short(path, index), note=note)
        used = {_norm(r.path) for p in files.playlists for g in p.games for r in g.refs if r.path is not None}
        for printed, reason in verdict.skipped.items():
            where = Path(printed) if Path(printed).is_absolute() else index.folder / printed
            if _inside(where, index.content) and _norm(where) not in used:
                add("plays-file-left-out", _short(printed, index), where, file=_short(printed, index), reason=reason)
    return found


def _effect(in_use: bool) -> str:
    if in_use:
        return ("It keeps playing what it played before, no other change to its dedicated.toml applies until this is "
                "fixed, and stopped, it won't start.")
    return "Nothing plays that file now, so nothing changes for players; as it is, it can't be played."


def _skip_finding(skip: SkipSeen) -> Finding:
    at = time.strftime("%H:%M", time.localtime(skip.at)) if skip.at else "?"
    rule = {"skipped": "plays-game-skipped", "unplayable": "plays-game-unplayable", "retry": "plays-game-retried",
            "load_failed": "plays-game-load-failed", "none_playable": "plays-nothing-playable",
            "outside": "plays-console-game-failed", "no_results": "plays-game-passed-over"}.get(skip.what, "")
    key = "" if skip.what == "none_playable" else skip.game.casefold()
    return (rule, key, None, {"game": skip.game, "reason": skip.reason or "no reason given", "at": at,
                              "times": skip.times if skip.times is not None else "?", "skip": skip.skip or "a while"})


def _short(printed: str, index: ContentIndex) -> str:
    where = Path(printed) if Path(printed).is_absolute() else index.folder / printed
    try:
        return "content/" + where.resolve().relative_to(index.content.resolve()).as_posix()
    except (OSError, ValueError):
        return printed


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.resolve().is_relative_to(folder.resolve())
    except OSError:
        return False


def _map_id(index: ContentIndex, stem: str) -> int | None:
    """The map ID a mod map's file declares (its scenario's), read once while the file stays the same."""
    from .modcheck import map_verdict, maps_folder

    for mod, _, map_stem in _mod_maps(index):
        if map_stem.casefold() != stem.casefold() or index.mod_off(mod):
            continue
        for folder in (maps_folder(mod.folder), mod.folder):
            path = folder / f"{map_stem}.map" if folder is not None else None
            if path is not None and path.is_file():
                try:
                    return map_verdict(path).map_id or None
                except OSError:
                    return None
    return None


def skip_finding(skip: SkipSeen) -> Finding:
    """A game the server said it couldn't play, as a finding for the warnings catalogue."""
    return _skip_finding(skip)


# Who uses a file (B14) ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Use:
    server: str
    how: str  # "playlist =", "playlists/races.playlist.json game 3", "variant ="
    in_use: bool  # what the server plays now (False: a playlist file of its it doesn't play)

    @property
    def text(self) -> str:
        return f"{self.server} ({self.how})" + ("" if self.in_use else ", not played now")


def who_uses(servers: list[Any], path: Path, *, config_name: str = "dedicated.toml") -> list[Use]:
    """Every server that names this file: a playlist file by `playlist =` or `playlists`, a game mode or layout by any
    game of any playlist file it has (played or not), `variant =`, `game =`, `map =` or `base_map`, a picture by a
    game's `preview_image`; co-op entries name none. One resolver for all of it (server_files)."""
    target = _norm(path)
    found: list[Use] = []
    for server in servers:
        try:
            report = server_files(server, config_name=config_name)
        except OSError:
            continue
        for playlist in report.playlists:
            if _norm(playlist.path) == target and playlist.in_use:
                found.append(Use(server.name, playlist.in_use, True))
            for game in playlist.games:
                if any(r.path is not None and _norm(r.path) == target for r in game.refs):
                    found.append(Use(server.name, f"{playlist.relative} game {game.number}", bool(playlist.in_use)))
        if report.own is not None and any(r.path is not None and _norm(r.path) == target for r in report.own.refs):
            found.append(Use(server.name, report.setting, True))
    return found


# What Huragok keeps between runs ------------------------------------------------------------------------------------

def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_verdict(records: Path | None, server: str) -> Verdict | None:
    """The server's own program's last word on what it plays, as the running Huragok kept it (None: not asked yet)."""
    if records is None:
        return None
    return Verdict.from_json(_read(records / VERDICTS).get(server))


def keep_verdict(records: Path | None, server: str, verdict: Verdict) -> None:
    if records is None:
        return
    data = _read(records / VERDICTS)
    data[server] = verdict.to_json()
    try:
        records.mkdir(parents=True, exist_ok=True)
        write_atomic(records / VERDICTS, json.dumps(data, indent=1, sort_keys=True) + "\n")
    except OSError as error:
        log.debug("couldn't keep %s's verdict: %s", server, error)


PASSING_MINUTES = 30.0  # a load that failed and was tried again, or a console game that failed: said this long
LASTING = {"skipped", "unplayable", "none_playable", "no_results"}  # until the skip ends or the server starts again


def skip_seconds(text: str) -> float | None:
    """'30 minutes' -> 1800; None for what this can't read."""
    import re

    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|second|min|minute|h|hour)s?\b", text or "", re.IGNORECASE)
    if match is None:
        return None
    unit = match.group(2).lower()
    return float(match.group(1)) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)


def still_active(skip: SkipSeen, now: float) -> bool:
    """Whether a game the server said it couldn't play is still so: a skip for a while until that while (and five
    minutes) has gone by, one for good until the server starts again (the watch makes those inactive), a passing one
    (a retry, a failed load, a console game) for PASSING_MINUTES."""
    if not skip.active:
        return False
    if skip.what == "skipped":
        seconds = skip_seconds(skip.skip)
        return seconds is None or now <= skip.at + seconds + 300
    if skip.what in LASTING:
        return True
    return now <= skip.at + PASSING_MINUTES * 60


def current_skips(skips: list[SkipSeen], now: float | None = None) -> list[SkipSeen]:
    """The skips with those that have passed made inactive."""
    now = time.time() if now is None else now
    return [s if still_active(s, now) or not s.active else
            SkipSeen(s.what, s.game, s.at, s.reason, s.times, s.skip, False) for s in skips]


def report_for(settings: Any, server: ServerConfig) -> ServerFiles:
    """One server's report as any window can make it: from its files, and what the running Huragok kept of its own
    program's verdict and the games it said it couldn't play (records/). Files only: in a thread."""
    verdict = load_verdict(settings.records, server.name)
    return server_files(server, verdict=verdict, skips=current_skips(load_skips(settings.records, server.name)),
                        version=verdict.version if verdict is not None and verdict.version else None,
                        config_name=settings.config_name)


def load_skips(records: Path | None, server: str) -> list[SkipSeen]:
    """The games the server said it couldn't play this past week, oldest first."""
    if records is None:
        return []
    items = _read(records / SKIPS).get(server)
    found = [SkipSeen.from_json(item) for item in items] if isinstance(items, list) else []
    return [s for s in found if s is not None]


def keep_skips(records: Path | None, server: str, skips: list[SkipSeen], now: float | None = None) -> list[SkipSeen]:
    """The server's skips kept (the last week's, SKIPS_MOST at most); the list as kept."""
    now = time.time() if now is None else now
    kept = [s for s in skips if s.at >= now - SKIP_DAYS * 86400][-SKIPS_MOST:]
    if records is not None:
        data = _read(records / SKIPS)
        data[server] = [s.to_json() for s in kept]
        try:
            records.mkdir(parents=True, exist_ok=True)
            write_atomic(records / SKIPS, json.dumps(data, indent=1, sort_keys=True) + "\n")
        except OSError as error:
            log.debug("couldn't keep %s's skipped games: %s", server, error)
    return kept


__all__ = ["ContentIndex", "FileHealth", "Finding", "GameHealth", "PlaylistHealth", "Ref", "SOURCE", "STATUSES",
           "ServerFiles", "SkipSeen", "Use", "content_index", "current_skips", "keep_skips", "keep_verdict",
           "load_skips", "load_verdict", "report_for", "resolve_game", "resolve_game_entry", "resolve_image",
           "resolve_map", "resolve_mission", "server_files", "skip_finding", "skip_seconds", "still_active", "who_uses",
           "worst"]
