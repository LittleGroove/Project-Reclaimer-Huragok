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

from .dedicated import Dedicated

SECTION = re.compile(r"^(Maps|Map variants|Game types|Game variants|Mod maps|Workshop maps)\b")
SOURCE = re.compile(r"^\s+From (?P<source>.+?):\s*$")
ITEM = re.compile(r'^\s+"(?P<name>[^"]+)"'
                  r'(?:\s+or\s+"(?P<stem>[^"]+)")?'
                  r'(?:\s+"(?P<display>[^"]+)")?'
                  r'(?:\s+(?P<note>[^"\s].*?))?\s*$')
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
        return (f"{len(self.maps)} maps, {len(self.mod_maps)} mod maps, {len(self.map_variants)} map variants, "
                f"{len(self.game_types)} game types, {len(self.game_variants)} game variants, {len(self.mods)} mods, "
                f"{len(self.workshop_items)} Workshop items")


def _find(entries: list[Entry], text: str) -> Entry | None:
    text = text.strip().lower()
    for entry in entries:
        if text in (entry.name.lower(), entry.reference.lower()):
            return entry
    return None


def parse_listing(text: str) -> Catalogue:
    """`dedicated content`'s listing as a catalogue (mods and Workshop items are added by load_catalogue)."""
    catalogue = Catalogue(raw=text)
    kind = ""
    source = ""
    for line in text.splitlines():
        header = SECTION.match(line)
        if header:
            kind = KINDS[header.group(1)]
            source = ""
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


def installed_mods(content_root: Path) -> list[Mod]:
    """The mods in content/Mods (and Mods/Workshop/<id>): their titles, maps and shipped Forge layouts."""
    mods: list[Mod] = []
    root = content_root / "Mods"
    if not root.is_dir():
        return mods
    for info in sorted(root.rglob("ModInfo.json")):
        folder = info.parent
        try:
            data = json.loads(info.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        title = str((data.get("Title") or {}).get("Neutral") or folder.name)
        ids = (data.get("ModIdentifier") or {}).get("HostedModIds") or {}
        workshop_id = ids.get("SteamWorkshopId")
        if workshop_id is None and folder.parent.name == "Workshop" and folder.name.isdigit():
            workshop_id = int(folder.name)
        maps: list[str] = []
        for relative in (data.get("GameModContents") or {}).get("MultiplayerMaps") or []:
            path = folder / str(relative).replace("\\", "/")
            try:
                map_data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                maps.append(str((map_data.get("Title") or {}).get("Neutral") or path.stem))
            except (OSError, ValueError):
                maps.append(path.stem)
        layouts: list[tuple[str, Path]] = []
        for path in sorted((folder / LAYOUTS_FOLDER).glob("*.json")) if (folder / LAYOUTS_FOLDER).is_dir() else []:
            try:
                name = str(json.loads(path.read_text(encoding="utf-8", errors="replace")).get("name") or path.stem)
            except (OSError, ValueError):
                name = path.stem
            layouts.append((name, path))
        mods.append(Mod(folder, title, maps, layouts, int(workshop_id) if workshop_id else None,
                        str(data.get("Engine") or "")))
    return mods


def workshop_items_of(config: Path) -> list[int]:
    from .tomledit import read_toml

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
    catalogue.mods = installed_mods(content_root)
    catalogue.workshop_items = workshop_items_of(config)
    return catalogue


def install_layouts(mod: Mod, content_root: Path, *, overwrite: bool = False) -> list[Path]:
    """Copy a mod's shipped Forge layouts into content/Maps/<mod title>/ (the server lists them as saved maps)."""
    import shutil

    target = content_root / "Maps" / re.sub(r'[<>:"/\\|?*]', "-", mod.title).strip() or "layouts"
    copied: list[Path] = []
    for name, path in mod.layouts:
        safe = re.sub(r'[<>:"/\\|?*]', "-", name).strip() or path.stem
        destination = target / f"{safe}.json"
        if destination.exists() and not overwrite:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
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
        if "-" in part and all(p.strip().isdigit() for p in part.split("-", 1)):
            low, high = (int(p) for p in part.split("-", 1))
            if low < 1 or high > count or low > high:
                return None
            picked.extend(range(low, high + 1))
        elif part.isdigit() and 1 <= int(part) <= count:
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
