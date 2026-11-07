"""What a server can play, as one catalogue: every game's maps (Halo 3's, and from Project Reclaimer 0.10 Reach's,
Combat Evolved's and Halo 2's), map variants, game types, game variants, mod maps (hand-installed and Workshop) and the
Forge layouts mods ship.

Built from the server program's own `dedicated content` listing (so the names are the ones
playlists and `map = ` / `game = ` accept), the content folder's Mods (their ModInfo.json),
and the Workshop items in dedicated.toml. The window's pickers number everything so the
host chooses from what's there instead of typing names.

The listing's two shapes:
- 0.9: `Maps: map = "…" …` sections of quoted names (`"Assembly"  or "descent"`), `From Halo 3:` lines before each
  source's variants; from 0.9.20 Halo 3's own game variants only counted ("49 from Halo 3 (search to list them …").
- 0.10 (probed on 0.10.0, 2026-11-06): `Maps (map = "..."):`, `Halo: Reach's maps (map = "..."):` … sections with
  `  Assembly                 descent (ID 490)` lines; `Other games' mods' maps and campaign canvases`; `Game types`;
  and the variants counted (`Map variants and Forge saves (map = "..."): 595 (name words to list them)`) unless words
  are given. A search (`dedicated content <words>`) lists the names, base types or files holding the words (no
  wildcards: "*" finds nothing), and an empty word lists every one: `  2 Flag Slayer        Slayer (Halo: Reach
  matchmaking)`. No Halo 3 mod maps: `dedicated mods` names those.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.files import write_atomic
from ..games import bungie
from ..games.odst import Listing
from ..games.odst import parse_listing as parse_odst
from ..program.dedicated import Dedicated

SECTION = re.compile(r"^(Maps|Map variants|Game types|Game variants|Mod maps|Workshop maps)\b")
SOURCE = re.compile(r"^\s+From (?P<source>.+?):\s*$")
ITEM = re.compile(r'^\s+"(?P<name>[^"]+)"'
                  r'(?:\s+or\s+"(?P<stem>[^"]+)")?'
                  r'(?:\s+"(?P<display>[^"]+)")?'
                  r'(?:\s+(?P<note>[^"\s].*?))?\s*$')
# From 0.9.20 the listing counts Halo 3's own game variants without listing them ("49 from Halo 3 (search to list
# them: dedicated content <words>)"). A search lists the names holding its word (two words narrow it), and every
# name holds a vowel, so one search per vowel lists them all (probed on 0.9.21: 49 of 49).
COUNTED = re.compile(r"^\s+(?P<count>\d+) from .+ \(search to list them", re.MULTILINE)
SEARCH_WORDS = ("a", "e", "i", "o", "u")
KINDS = {"Maps": "map", "Map variants": "map variant", "Game types": "game type", "Game variants": "game variant",
         "Mod maps": "mod map", "Workshop maps": "mod map"}
LAYOUTS_FOLDER = "Project Reclaimer Layouts"

# 0.10's shape. Lines that aren't entries come between them ("Skipped content file …", "Replaced the default game
# mode …", "[odst] Halo 3: ODST installed in …"), so only the next heading ends a section.
HEADING = re.compile(r'^(?P<what>\S.*?) \((?:map|game) = "\.\.\."\):\s*(?:(?P<count>\d+) \(name words to list them\))?'
                     r"\s*$")
MAP_LINE = re.compile(r"^  (?P<title>.*?\S)\s+(?P<stem>\S+) \(ID -?\d+\)\s*$")  # title, file name, map ID
TYPE_LINE = re.compile(r"^  (?P<title>.*?\S)\s+(?P<stem>\S+)\s*$")  # a game type's title and file name; also
#                                                                       "Title · subtitle (Campaign Canvas) ref"
VARIANT_LINE = re.compile(r"^  (?P<rest>.*?\S) \((?P<source>[^()]*)\)\s*$")  # name (32 wide), base type, (source)
NAME_WIDTH = 32
CANVAS = "(Campaign Canvas)"
EVERYTHING = ""  # the search word 0.10 lists every variant for; 0.9 takes it as no word (checked on 0.9.18 and 0.9.21)
BASES = sorted({*bungie.HALO3_GAME_TYPES, *(t for g in bungie.GAMES.values() for t in g.game_types)}, key=len,
               reverse=True)


@dataclass
class Entry:
    name: str  # what players read
    reference: str  # what map = / game = and playlists take
    kind: str  # map, map variant, game type, game variant, mod map, campaign canvas
    source: str = ""  # "Halo 3", "your content folder", "mod HugeGrass", "Workshop 3820687243", "Halo: Reach"
    base: str = ""  # a variant's map, a game variant's game type
    note: str = ""
    game: str = ""  # a bungie.GAMES key ("halo3", "reach", "halo1", "halo2"); "" when not known
    copies: int = 1  # 0.10 lists a variant once per file, and MCC has several of one name (11 "Slayer DMRs")

    @property
    def label(self) -> str:
        bits = [self.name]
        if self.reference != self.name:
            bits.append(f"[{self.reference}]")
        if self.base:
            bits.append(f"· {self.base}")
        if self.source:
            bits.append(f"({self.source})")
        if self.copies > 1:
            bits.append(f"{self.copies} copies")
        return "  ".join(bits)


@dataclass
class Mod:
    folder: Path
    title: str
    maps: list[str] = field(default_factory=list)  # map titles from its multiplayer/*.json
    layouts: list[tuple[str, Path]] = field(default_factory=list)  # (name, file) Forge layouts it ships
    workshop_id: int | None = None
    engine: str = ""
    stems: list[str] = field(default_factory=list)  # each map's file name, beside maps

    @property
    def game(self) -> str:
        """The game it's for (a bungie.GAMES key, by its ModInfo Engine), or "" for one no Reclaimer plays."""
        game = bungie.game_of_engine(self.engine)
        return game.key if game else ""

    @property
    def label(self) -> str:
        bits = [self.title]
        if self.workshop_id:
            bits.append(f"Workshop {self.workshop_id}")
        if self.maps:
            bits.append(f"map{'s' if len(self.maps) > 1 else ''}: {', '.join(self.maps)}")
        if self.layouts:
            bits.append(f"{len(self.layouts)} Forge layout(s)")
        game = bungie.game_of_engine(self.engine)
        if game is None:
            bits.append(f"{self.engine}: not a game Reclaimer plays")
        elif game.key != "halo3":
            bits.append(f"a {game.title} mod")
        return " · ".join(bits)


@dataclass
class Catalogue:
    maps: list[Entry] = field(default_factory=list)  # every game's stock maps (one with no game is Halo 3's)
    map_variants: list[Entry] = field(default_factory=list)
    game_types: list[Entry] = field(default_factory=list)
    game_variants: list[Entry] = field(default_factory=list)
    mod_maps: list[Entry] = field(default_factory=list)  # every game's
    mods: list[Mod] = field(default_factory=list)
    workshop_items: list[int] = field(default_factory=list)
    odst: Listing = field(default_factory=lambda: Listing([], [], []))  # Halo 3: ODST's, when its files are there
    raw: str = ""
    canvases: list[Entry] = field(default_factory=list)  # 0.10: campaign missions' areas as Forge canvases
    counted: dict[str, int] = field(default_factory=dict)  # "map variant"/"game variant": counted, not listed
    installed: dict[str, bool] = field(default_factory=dict)  # 0.10: {"reach": True, "odst": False, …}
    shape: str = "0.9"  # the listing's: "0.9", or "0.10" (every game's maps)

    @property
    def all_maps(self) -> list[Entry]:
        """Everything a playlist's `map` can name: base maps, mod maps, then variants."""
        return [*self.maps, *self.mod_maps, *self.map_variants]

    @property
    def all_games(self) -> list[Entry]:
        return [*self.game_types, *self.game_variants]

    def find_map(self, text: str) -> Entry | None:
        return _find(self.all_maps, text) or _find(self.canvases, text)

    def find_game(self, text: str) -> Entry | None:
        return _find(self.all_games, text)

    def maps_of(self, game: str) -> list[Entry]:
        """One game's stock maps, as listed."""
        return [e for e in self.maps if (e.game or "halo3") == game]

    def games_present(self) -> list[str]:
        """The games the listing shows maps for (stock or mod), Halo 3's first: bungie.GAMES keys."""
        have = {e.game or "halo3" for e in self.maps} | {e.game for e in self.mod_maps}
        return [key for key in bungie.MULTIPLAYER_GAMES if key in have]

    def counts(self) -> str:
        odst = (f", Halo 3: ODST's {len(self.odst.missions)} missions and {len(self.odst.maps)} Firefight maps"
                if not self.odst.empty else "")
        games = self.games_present()
        each = (f" ({', '.join(f'{bungie.GAMES[k].short} {len(self.maps_of(k))}' for k in games)})"
                if len(games) > 1 else "")
        canvases = f", {len(self.canvases)} campaign canvases" if self.canvases else ""

        def variants(kind: str, entries: list[Entry]) -> str:
            if not entries and self.counted.get(kind):
                return f"{self.counted[kind]} {kind}s (counted, not listed)"
            return f"{len(entries)} {kind}s"

        return (f"{len(self.maps)} maps{each}, {len(self.mod_maps)} mod maps, "
                f"{variants('map variant', self.map_variants)}{canvases}, {len(self.game_types)} game types, "
                f"{variants('game variant', self.game_variants)}, {len(self.mods)} mods, "
                f"{len(self.workshop_items)} Workshop items{odst}")


def _find(entries: list[Entry], text: str) -> Entry | None:
    text = text.strip().lower()
    for entry in entries:
        if text in (entry.name.lower(), entry.reference.lower()):
            return entry
    return None


def game_of_source(source: str) -> str:
    """The game a 0.10 variant is from, by its source: "Halo 3" and "Matchmaking" are Halo 3's, "Halo: Reach" and
    "Halo: Reach matchmaking" Reach's; "" for the content folder's ("Custom") and what isn't known."""
    text = source.strip().casefold()
    if text == "matchmaking":
        return "halo3"
    text = text.removesuffix(" matchmaking")
    return next((g.key for g in bungie.GAMES.values() if g.title.casefold() == text), "")


def _split_variant(rest: str) -> tuple[str, str]:
    """A 0.10 variant line's name and base type: the name is padded to 32 wide, and may hold two spaces itself
    ("Alembic  Gold") or start with some ("   Synapse")."""
    for base in BASES:
        name = rest[:-len(base)]
        if rest.endswith(base) and name.endswith(" ") and len(name) > NAME_WIDTH:
            return name.strip(), base
    if len(rest) > NAME_WIDTH + 1 and rest[NAME_WIDTH] == " ":
        return rest[:NAME_WIDTH].strip(), rest[NAME_WIDTH + 1:].strip()
    name, _, base = rest.rpartition(" ")
    return (name.strip(), base) if name.strip() else (rest.strip(), "")


def _section(what: str) -> tuple[str, str]:
    """(kind, game) for one of 0.10's headings."""
    game = next((g for g in bungie.GAMES.values() if what == g.section), None)
    if game is not None:
        return "map", game.key
    if what.startswith("Other games'"):
        return "other map", ""
    if what.startswith("Game types"):
        return "game type", "halo3"
    if what.startswith("Map variants"):
        return "map variant", ""
    if what.startswith("Game variants"):
        return "game variant", ""
    if what.endswith("'s maps"):
        return "map", ""  # a game this Huragok doesn't know yet: its maps go with the mod maps
    return "", ""


def _parse_010(text: str, catalogue: Catalogue) -> None:
    """0.10's shape into the catalogue. A variant listed more than once (one name, base and source) is one entry
    counting its copies: `game = ` takes the name, so they're one choice."""
    kind = game = ""
    stock = {key: {m.stem.casefold(): m for m in g.maps} for key, g in bungie.GAMES.items()}
    seen: dict[tuple[str, str, str, str], Entry] = {}
    for line in text.splitlines():
        heading = HEADING.match(line)
        if heading:
            kind, game = _section(heading.group("what"))
            if heading.group("count") and kind:
                catalogue.counted[kind] = int(heading.group("count"))
            continue
        if not kind or not line.startswith("  "):
            continue
        if kind == "map":
            match = MAP_LINE.match(line)
            if not match:
                continue
            title, stem = match.group("title"), match.group("stem")
            known = stock.get(game, {}).get(stem.casefold())
            owner = bungie.GAMES.get(game)
            if known is not None:
                catalogue.maps.append(Entry(title, stem, "map", "" if game == "halo3" else owner.title,
                                            note="Firefight" if known.firefight else "", game=game))
            else:  # a mod's map of that game (MCC's Workshop ones are listed with its own)
                catalogue.mod_maps.append(Entry(title, stem, "mod map", f"{owner.title} mod" if owner else "mod",
                                                game=game))
        elif kind == "other map":
            match = TYPE_LINE.match(line)
            if not match:
                continue
            words, reference = match.group("title"), match.group("stem")
            if words.endswith(CANVAS):
                catalogue.canvases.append(Entry(words[:-len(CANVAS)].strip(), reference, "campaign canvas"))
            else:  # "Scarif · The Battlefront Pack - Reach": the map, then its mod
                title, _, mod = words.partition(" · ")
                catalogue.mod_maps.append(Entry(title.strip(), reference, "mod map",
                                                f"mod {mod.strip()}" if mod.strip() else "mod"))
        elif kind == "game type":
            match = TYPE_LINE.match(line)
            if match:
                catalogue.game_types.append(Entry(match.group("title"), match.group("title"), "game type",
                                                  note=match.group("stem"), game=game))
        else:
            match = VARIANT_LINE.match(line)
            if not match:
                continue
            name, base = _split_variant(match.group("rest"))
            source = match.group("source").strip()
            key = (kind, name, base, source)
            if key in seen:
                seen[key].copies += 1
                continue
            seen[key] = Entry(name, name, kind, source, base, game=game_of_source(source))
            (catalogue.map_variants if kind == "map variant" else catalogue.game_variants).append(seen[key])


def parse_listing(text: str) -> Catalogue:
    """`dedicated content`'s listing as a catalogue (mods and Workshop items are added by load_catalogue), from
    either shape."""
    catalogue = Catalogue(raw=text, odst=parse_odst(text), installed=bungie.installed_in_text(text))
    if any(HEADING.match(line) for line in text.splitlines()):
        catalogue.shape = "0.10"
        _parse_010(text, catalogue)
        return catalogue
    counted = sum(int(m.group("count")) for m in COUNTED.finditer(text))
    if counted:
        catalogue.counted["game variant"] = counted
    kind = ""
    source = ""
    for line in text.splitlines():
        header = SECTION.match(line)
        if header:
            kind = KINDS[header.group(1)]
            source = ""
            continue
        if line and not line[0].isspace():
            kind = ""  # a section this doesn't read (Halo 3: ODST's, from 0.9.19), or the closing hint
            continue
        origin = SOURCE.match(line)
        if origin:
            source = origin.group("source")
            continue
        match = ITEM.match(line)
        if not match or not kind:
            continue
        name = match.group("display") or match.group("name")
        reference = match.group("stem") or match.group("name")
        note = (match.group("note") or "").strip()
        entry_kind, base, entry_source = kind, "", source
        if note.startswith("mod ·") or note.startswith("mod map"):
            entry_kind = "mod map"
            entry_source = "mod " + note.split("·", 1)[1].strip() if "·" in note else "mod"
        elif "·" in note:  # "map variant · Foundry", or just "· Foundry" after a quoted display name
            base = note.split("·", 1)[1].strip()
        elif note.startswith("(") and note.endswith(")"):
            entry_source = note.strip("()")
        entry = Entry(name, reference, entry_kind, entry_source, base, note, game="halo3")  # 0.9 plays only Halo 3
        {"map": catalogue.maps, "map variant": catalogue.map_variants, "game type": catalogue.game_types,
         "game variant": catalogue.game_variants, "mod map": catalogue.mod_maps}[entry_kind].append(entry)
    return catalogue


BLF = b"_blf"  # how every Halo 3 and MCC variant file (.bin, .mvar) starts
SET_ASIDE = "content set aside"  # beside a content folder: what was moved out of it, nothing deleted


def unreadable_variants(content_root: Path) -> list[Path]:
    """Variant files (.bin, .mvar) in a content folder that aren't MCC's BLF format: the server skips each at every
    start, saying "Expected an MCC BLF variant file". Mods/ is the mod checks' (modcheck.py)."""
    found: list[Path] = []
    if not content_root.is_dir():
        return found
    for path in sorted(content_root.rglob("*"), key=lambda p: str(p).lower()):
        # Mods/ in any case: on Windows "mods" is the same folder, and moving a mod's files out breaks the mod.
        if path.suffix.lower() not in (".bin", ".mvar") or path.relative_to(content_root).parts[0].casefold() == "mods":
            continue
        try:
            with path.open("rb") as file:
                head = file.read(len(BLF))
        except OSError:
            continue  # a folder named like a file, or one in use: not judged
        if head != BLF:
            found.append(path)
    return found


def set_aside(content_root: Path, paths: list[Path]) -> tuple[list[Path], list[str]]:
    """Files moved out of a content folder into `content set aside` beside it, each at the same path inside it
    (a name taken there gets a number): nothing is deleted, and the server no longer meets them at its start.
    (where each went, each that couldn't be moved and why): one in use or gone since stays, the others still move."""
    moved: list[Path] = []
    kept: list[str] = []
    for path in paths:
        target = content_root.parent / SET_ASIDE / path.relative_to(content_root)
        number = 2
        while target.exists():
            target = target.with_name(f"{path.stem} ({number}){path.suffix}")
            number += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
        except OSError as error:
            kept.append(f"{path.relative_to(content_root).as_posix()} ({error.strerror or error})")
            continue
        moved.append(target)
    return moved, kept


def add_searched(catalogue: Catalogue, results: list[Any]) -> None:
    """The variants the main listing only counted, from searches of it: each one once, in the order found (0.9 by
    its file or name; 0.10, whose names repeat, by name, base and source)."""
    def key(entry: Entry) -> tuple[str, ...]:
        if catalogue.shape == "0.10":
            return entry.reference.lower(), entry.base.lower(), entry.source.lower()
        return (entry.reference.lower(),)

    for kind, counted in (("game_variants", "game variant"), ("map_variants", "map variant")):
        entries = getattr(catalogue, kind)
        listed = {key(e) for e in entries}
        for result in results:
            if not result.ok:
                continue
            catalogue.counted.pop(counted, None)
            for entry in getattr(parse_listing(result.text), kind):
                if key(entry) not in listed:
                    entries.append(entry)
                    listed.add(key(entry))


def installed_mods(content_root: Path) -> list[Mod]:
    """The mods in content/Mods (and Mods/Workshop/<id>): their titles, maps and shipped Forge layouts."""
    mods: list[Mod] = []
    root = content_root / "Mods"
    if not root.is_dir():
        return mods
    for info in sorted(root.rglob("ModInfo.json")):
        folder = info.parent
        try:
            data = json.loads(info.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):  # not a ModInfo the game reads either: skipped, the other mods listed
            continue
        title = _neutral(data.get("Title")) or folder.name
        identifier = data.get("ModIdentifier")
        ids = identifier.get("HostedModIds") if isinstance(identifier, dict) else None
        workshop_id = _number(ids.get("SteamWorkshopId")) if isinstance(ids, dict) else None
        if workshop_id is None and folder.parent.name == "Workshop" and folder.name.isdecimal():
            workshop_id = int(folder.name)
        contents = data.get("GameModContents")
        listed = contents.get("MultiplayerMaps") if isinstance(contents, dict) else None
        maps: list[str] = []
        stems: list[str] = []
        for relative in listed if isinstance(listed, list) else []:
            path = folder / str(relative).replace("\\", "/")
            try:
                map_data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            except (OSError, ValueError):
                map_data = None
            maps.append((_neutral(map_data.get("Title")) if isinstance(map_data, dict) else None) or path.stem)
            scenario = map_data.get("ScenarioFile") if isinstance(map_data, dict) else None  # levels/multi/x/<stem>
            stem = str(scenario).replace("\\", "/").rstrip("/").rpartition("/")[2] if isinstance(scenario, str) else ""
            stems.append(stem.strip() or path.stem)
        layouts: list[tuple[str, Path]] = []
        for path in sorted((folder / LAYOUTS_FOLDER).glob("*.json")) if (folder / LAYOUTS_FOLDER).is_dir() else []:
            try:
                layout = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            except (OSError, ValueError):
                layout = None
            name = layout.get("name") if isinstance(layout, dict) else None
            layouts.append((name if isinstance(name, str) and name.strip() else path.stem, path))
        mods.append(Mod(folder, title, maps, layouts, workshop_id, str(data.get("Engine") or ""), stems))
    return mods


def add_mod_maps(catalogue: Catalogue, content_root: Path) -> None:
    """0.10's listing has no Halo 3 mod maps (`dedicated mods` names them): the content folder's Halo 3 mods' maps,
    named as 0.10's `dedicated mods` and 0.9's listing name them: workshop:<item>/<map>, or "<map title> · Custom"
    for a mod with no Workshop item. Switched-off mods (disabled-mods.json) and maps already listed are left out."""
    try:
        packages = disabled_mods(content_root)
    except DisabledModsError:
        packages = []
    listed = {e.reference.casefold() for e in catalogue.mod_maps}
    for mod in catalogue.mods:
        if mod.game != "halo3" or is_disabled(packages, mod_package(content_root, mod)):
            continue
        for title, stem in zip(mod.maps, mod.stems):
            reference = f"workshop:{mod.workshop_id}/{stem}" if mod.workshop_id else f"{title} · Custom"
            if reference.casefold() not in listed:
                listed.add(reference.casefold())
                catalogue.mod_maps.append(Entry(title, reference, "mod map", f"mod {mod.title}", game="halo3"))


def _neutral(value: Any) -> str | None:
    """A ModInfo text: {"Neutral": "…"} (or a plain string, as some mods write it)."""
    if isinstance(value, dict):
        value = value.get("Neutral")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value or None
    return int(value) if isinstance(value, str) and value.strip().isdecimal() else None


def workshop_items_of(config: Path) -> list[int]:
    from ..core.tomledit import read_toml

    try:
        workshop = read_toml(config).get("workshop", {})
    except (OSError, ValueError):
        return []
    items = workshop.get("items", []) if isinstance(workshop, dict) else []
    return [int(i) for i in items if isinstance(i, int) and not isinstance(i, bool)]


def load_catalogue(tool: Dedicated, folder: Path, content_root: Path, config: Path) -> Catalogue:
    """Everything a server can play right now. Raises DedicatedError-like ValueError when the listing fails.

    One call with an empty search word: 0.10 then lists every variant its plain listing only counts (1,353 on
    this PC, in about 3 s), and 0.9 gives its plain listing, whose counted Halo 3 variants one search per vowel
    lists (0.9.20 on: 49 of 49). A program that took no word at all would be asked again without it."""
    result = tool.content(folder, [EVERYTHING])
    if not result.ok and getattr(result, "code", 1) not in (124, 130):  # not when it timed out or was stopped
        result = tool.content(folder)
    if not result.ok:
        raise ValueError(result.problem)
    catalogue = parse_listing(result.text)
    if catalogue.counted:
        add_searched(catalogue, [tool.content(folder, [word]) for word in SEARCH_WORDS])
    catalogue.mods = installed_mods(content_root)
    catalogue.workshop_items = workshop_items_of(config)
    if catalogue.shape == "0.10":
        add_mod_maps(catalogue, content_root)
    return catalogue


def install_layouts(mod: Mod, content_root: Path, *, overwrite: bool = False) -> list[Path]:
    """Copy a mod's shipped Forge layouts into content/Maps/<mod title>/ (the server lists them as saved maps).
    A title or layout name that isn't a file name ("..", "CON", "a/b") never puts a file outside that folder."""
    from .mccimport import copy_replacing, safe_file_name

    target = content_root / "Maps" / safe_file_name(mod.title, "layouts")
    copied: list[Path] = []
    for name, path in mod.layouts:
        destination = target / f"{safe_file_name(name, safe_file_name(path.stem, 'layout'))}.json"
        if destination.exists() and not overwrite:
            continue
        copy_replacing(path, destination)
        copied.append(destination)
    return copied


@dataclass
class LayoutSync:
    """What sync_layouts did, by file under the content folder."""

    copied: list[str] = field(default_factory=list)  # new
    updated: list[str] = field(default_factory=list)  # the mod's new version over Huragok's last copy
    replaced: list[str] = field(default_factory=list)  # there before Huragok kept track, and different: set aside
    kept: list[str] = field(default_factory=list)  # changed here by hand: left as it is

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.updated or self.replaced)


def file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def layout_target(mod: Mod, content_root: Path, name: str, path: Path) -> Path:
    from .mccimport import safe_file_name

    return (content_root / "Maps" / safe_file_name(mod.title, "layouts")
            / f"{safe_file_name(name, safe_file_name(path.stem, 'layout'))}.json")


def sync_layouts(mod: Mod, content_root: Path, written: dict[str, str], *, skip: set[str] | None = None) -> LayoutSync:
    """A mod's shipped Forge layouts into content/Maps/<mod title>/ as its version has them, where install_layouts
    copies them once. `written` (brought up to date here) holds what Huragok put there, by file under the content
    folder, as each file's hash: one changed since was changed by hand and is left; one a person deleted stays
    deleted; one that was there before Huragok kept track and differs (an older version, copied with L) is replaced,
    the old copy moved to `content set aside`. `skip`: files another mod of the same name saw to already."""
    from .mccimport import copy_replacing

    result = LayoutSync()
    for name, path in mod.layouts:
        destination = layout_target(mod, content_root, name, path)
        key = destination.relative_to(content_root).as_posix()
        if skip is not None:
            if key in skip:
                continue
            skip.add(key)
        try:
            new = file_hash(path)
            current = file_hash(destination) if destination.is_file() else None
        except OSError:
            continue  # one in use or gone since: looked at again next time
        known = written.get(key)
        if current == new:
            written[key] = new
            continue
        if current is None and known is not None:
            continue  # deleted by hand: stays deleted
        if current is not None and known is not None and current != known:
            result.kept.append(key)
            continue
        if current is not None and known is None:
            moved, _ = set_aside(content_root, [destination])
            if not moved:
                result.kept.append(key)
                continue
            done = result.replaced
        else:
            done = result.updated if current is not None else result.copied
        try:
            copy_replacing(path, destination)
        except OSError:
            continue  # tried again next time
        written[key] = new
        done.append(key)
    return result


# Picking from numbered lists -----------------------------------------------------------------------

def parse_picks(text: str, count: int) -> list[int] | None:
    """"1,3 5-8" -> [1, 3, 5, 6, 7, 8] (1-based, within count); "A" or "all" -> everything; None when nonsense."""
    text = text.strip()
    if not text:
        return []
    if text.lower() in ("a", "all", "*"):
        return list(range(1, count + 1))
    picked: list[int] = []
    for part in text.replace(",", " ").split():
        if "-" in part and all(p.strip().isdecimal() for p in part.split("-", 1)):
            low, high = (int(p) for p in part.split("-", 1))
            if low < 1 or high > count or low > high:
                return None
            picked.extend(range(low, high + 1))
        elif part.isdecimal() and 1 <= int(part) <= count:
            picked.append(int(part))
        else:
            return None
    seen: list[int] = []
    for number in picked:
        if number not in seen:
            seen.append(number)
    return seen


def describe_games(games: list[dict[str, Any]], limit: int = 6) -> str:
    """"Team Slayer on Guardian, CTF on Valhalla, and 12 more" for a playlist's entries."""
    names = [f"{g.get('name') or g.get('game', '?')} on {g.get('map', '?')}" for g in games if isinstance(g, dict)]
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f", and {len(names) - limit} more"


# Mods kept but not used: content/disabled-mods.json ------------------------------------------

DISABLED_MODS = "disabled-mods.json"  # {"packages": ["Mods/canyon"]}: mods the server keeps on disk but doesn't use


class DisabledModsError(ValueError):
    pass


def disabled_mods(content_root: Path) -> list[str]:
    """The mod folders switched off, as written (relative to the content folder). Raises DisabledModsError when
    the file isn't in the shape the server program reads, so it's never written over."""
    path = content_root / DISABLED_MODS
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise DisabledModsError(f"{DISABLED_MODS} can't be read ({error})") from None
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list) or not all(isinstance(p, str) for p in packages) or set(data) - {"packages"}:
        raise DisabledModsError(f'{DISABLED_MODS} isn\'t written as {{"packages": ["Mods/<folder>", …]}}')
    return packages


def mod_package(content_root: Path, mod: Mod) -> str:
    """How disabled-mods.json names a mod: its folder from the content folder, with forward slashes."""
    try:
        return mod.folder.resolve().relative_to(content_root.resolve()).as_posix()
    except ValueError:
        return mod.folder.as_posix()


def is_disabled(packages: list[str], package: str) -> bool:
    return any(p.strip().strip("/").replace("\\", "/").lower() == package.lower() for p in packages)


def set_mod_disabled(content_root: Path, package: str, off: bool) -> list[str]:
    """Switch one mod off (listed in disabled-mods.json) or on again; the file goes once nothing is listed. The
    list as it is now. Raises DisabledModsError or OSError."""
    packages = [p for p in disabled_mods(content_root) if not is_disabled([p], package)]
    if off:
        packages.append(package)
    path = content_root / DISABLED_MODS
    if packages:  # replaced, not written through: a server's own content made by hard links shares nothing (7.1)
        write_atomic(path, json.dumps({"packages": packages}, indent=2) + "\n")
    else:
        path.unlink(missing_ok=True)
    return packages
