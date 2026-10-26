"""What a server can play, as one catalogue: Halo 3's maps, map variants, game types, game
variants, mod maps (hand-installed and Workshop) and the Forge layouts mods ship.

Built from the server program's own `dedicated content` listing (so the names are the ones
playlists and `map = ` / `game = ` accept), the content folder's Mods (their ModInfo.json),
and the Workshop items in dedicated.toml. The window's pickers number everything so the
host chooses from what's there instead of typing names.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
COUNTED = re.compile(r"^\s+\d+ from .+ \(search to list them", re.MULTILINE)
SEARCH_WORDS = ("a", "e", "i", "o", "u")
KINDS = {"Maps": "map", "Map variants": "map variant", "Game types": "game type", "Game variants": "game variant",
         "Mod maps": "mod map", "Workshop maps": "mod map"}
LAYOUTS_FOLDER = "Project Reclaimer Layouts"


@dataclass
class Entry:
    name: str  # what players read
    reference: str  # what map = / game = and playlists take
    kind: str  # map, map variant, game type, game variant, mod map
    source: str = ""  # "Halo 3", "your content folder", "mod HugeGrass", "Workshop 3820687243"
    base: str = ""  # a variant's map, a game variant's game type
    note: str = ""

    @property
    def label(self) -> str:
        bits = [self.name]
        if self.reference != self.name:
            bits.append(f"[{self.reference}]")
        if self.base:
            bits.append(f"· {self.base}")
        if self.source:
            bits.append(f"({self.source})")
        return "  ".join(bits)


@dataclass
class Mod:
    folder: Path
    title: str
    maps: list[str] = field(default_factory=list)  # map titles from its multiplayer/*.json
    layouts: list[tuple[str, Path]] = field(default_factory=list)  # (name, file) Forge layouts it ships
    workshop_id: int | None = None
    engine: str = ""

    @property
    def label(self) -> str:
        bits = [self.title]
        if self.workshop_id:
            bits.append(f"Workshop {self.workshop_id}")
        if self.maps:
            bits.append(f"map{'s' if len(self.maps) > 1 else ''}: {', '.join(self.maps)}")
        if self.layouts:
            bits.append(f"{len(self.layouts)} Forge layout(s)")
        if self.engine and self.engine.lower() != "halo3":
            bits.append(f"{self.engine}: not a Halo 3 mod")
        return " · ".join(bits)


@dataclass
class Catalogue:
    maps: list[Entry] = field(default_factory=list)
    map_variants: list[Entry] = field(default_factory=list)
    game_types: list[Entry] = field(default_factory=list)
    game_variants: list[Entry] = field(default_factory=list)
    mod_maps: list[Entry] = field(default_factory=list)
    mods: list[Mod] = field(default_factory=list)
    workshop_items: list[int] = field(default_factory=list)
    odst: Listing = field(default_factory=lambda: Listing([], [], []))  # Halo 3: ODST's, when its files are there
    raw: str = ""

    @property
    def all_maps(self) -> list[Entry]:
        """Everything a playlist's `map` can name: base maps, mod maps, then variants."""
        return [*self.maps, *self.mod_maps, *self.map_variants]

    @property
    def all_games(self) -> list[Entry]:
        return [*self.game_types, *self.game_variants]

    def find_map(self, text: str) -> Entry | None:
        return _find(self.all_maps, text)

    def find_game(self, text: str) -> Entry | None:
        return _find(self.all_games, text)

    def counts(self) -> str:
        odst = (f", Halo 3: ODST's {len(self.odst.missions)} missions and {len(self.odst.maps)} Firefight maps"
                if not self.odst.empty else "")
        return (f"{len(self.maps)} maps, {len(self.mod_maps)} mod maps, {len(self.map_variants)} map variants, "
                f"{len(self.game_types)} game types, {len(self.game_variants)} game variants, {len(self.mods)} mods, "
                f"{len(self.workshop_items)} Workshop items{odst}")


def _find(entries: list[Entry], text: str) -> Entry | None:
    text = text.strip().lower()
    for entry in entries:
        if text in (entry.name.lower(), entry.reference.lower()):
            return entry
    return None


def parse_listing(text: str) -> Catalogue:
    """`dedicated content`'s listing as a catalogue (mods and Workshop items are added by load_catalogue)."""
    catalogue = Catalogue(raw=text, odst=parse_odst(text))
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
        entry = Entry(name, reference, entry_kind, entry_source, base, note)
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
    """The variants the main listing only counted, from searches of it: each one once, in the order found."""
    for kind in ("game_variants", "map_variants"):
        entries = getattr(catalogue, kind)
        listed = {e.reference.lower() for e in entries}
        for result in results:
            if not result.ok:
                continue
            for entry in getattr(parse_listing(result.text), kind):
                if entry.reference.lower() not in listed:
                    entries.append(entry)
                    listed.add(entry.reference.lower())


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
        for relative in listed if isinstance(listed, list) else []:
            path = folder / str(relative).replace("\\", "/")
            try:
                map_data = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            except (OSError, ValueError):
                map_data = None
            maps.append((_neutral(map_data.get("Title")) if isinstance(map_data, dict) else None) or path.stem)
        layouts: list[tuple[str, Path]] = []
        for path in sorted((folder / LAYOUTS_FOLDER).glob("*.json")) if (folder / LAYOUTS_FOLDER).is_dir() else []:
            try:
                layout = json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
            except (OSError, ValueError):
                layout = None
            name = layout.get("name") if isinstance(layout, dict) else None
            layouts.append((name if isinstance(name, str) and name.strip() else path.stem, path))
        mods.append(Mod(folder, title, maps, layouts, workshop_id, str(data.get("Engine") or "")))
    return mods


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
    """Everything a server can play right now. Raises DedicatedError-like ValueError when the listing fails."""
    result = tool.content(folder)
    if not result.ok:
        raise ValueError(result.problem)
    catalogue = parse_listing(result.text)
    if COUNTED.search(result.text):
        add_searched(catalogue, [tool.content(folder, [word]) for word in SEARCH_WORDS])
    catalogue.mods = installed_mods(content_root)
    catalogue.workshop_items = workshop_items_of(config)
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
    if packages:
        path.write_text(json.dumps({"packages": packages}, indent=2) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return packages
