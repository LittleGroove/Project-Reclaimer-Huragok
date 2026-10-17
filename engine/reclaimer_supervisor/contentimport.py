"""Importing map and game variants into a server's content folder: from MCC's own files, a folder, or a file.

Halo: The Master Chief Collection ships every matchmaking variant 343 made for Halo 3 in
`halo3/hopper_map_variants/*.mvar` (320 of them: MLG, Team Hardcore, Infection, Grifball…)
and `halo3/hopper_game_variants/*.bin` (334), plus `halo3/map_variants` and
`halo3/game_variants`. A dedicated server lists only what's in its content folder, so
this copies them there: `.mvar` to content/Maps, `.bin` to content/Game Modes, and
saved `.json` maps and game types to the folder they belong in. The server picks maps
and game types up while running (0.9.18); `dedicated content` lists them by name.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

MCC_FOLDER = "Halo The Master Chief Collection"
STEAM_DEFAULT = Path(r"C:\Program Files (x86)\Steam\steamapps\common") / MCC_FOLDER
MCC_SOURCES = {  # folder under halo3/ -> content subfolder
    "hopper_map_variants": "Maps", "map_variants": "Maps",
    "hopper_game_variants": "Game Modes", "game_variants": "Game Modes",
}
KINDS = {".mvar": "Maps", ".bin": "Game Modes"}


@dataclass(frozen=True)
class Importable:
    source: Path
    target: str  # content subfolder: Maps or Game Modes

    @property
    def name(self) -> str:
        return self.source.stem


def find_mcc(hint: str | None = None) -> Path | None:
    """Where MCC is: the hint, Steam's default folder, or a Steam library on any drive."""
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint))
    candidates.append(STEAM_DEFAULT)
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            for library in (f"{letter}:\\SteamLibrary", f"{letter}:\\Steam", f"{letter}:\\Games\\Steam",
                            f"{letter}:\\Program Files (x86)\\Steam", f"{letter}:\\Program Files\\Steam"):
                candidates.append(Path(library) / "steamapps" / "common" / MCC_FOLDER)
    else:
        for library in ("~/.steam/steam", "~/.local/share/Steam", "~/.var/app/com.valvesoftware.Steam/.local/share/Steam"):
            candidates.append(Path(library).expanduser() / "steamapps" / "common" / MCC_FOLDER)
    for path in candidates:
        if (path / "halo3").is_dir():
            return path
    return None


def _json_kind(path: Path) -> str | None:
    """A saved .json: a game type (has "mode"/"rules") goes to Game Modes, a saved map to Maps."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "mode" in data or "rules" in data or "script" in data:
        return "Game Modes"
    if "map" in data or "placements" in data or "objects" in data:
        return "Maps"
    return None


def importable(source: Path, words: list[str] | None = None) -> list[Importable]:
    """What a source holds: MCC's variant folders, any folder's .mvar/.bin/.json files, or one file."""
    words = [w.lower() for w in (words or []) if w.strip()]
    found: list[Importable] = []

    def add(path: Path, target: str) -> None:
        if words and not all(w in path.stem.lower() for w in words):
            return
        found.append(Importable(path, target))

    if source.is_file():
        target = KINDS.get(source.suffix.lower()) or (_json_kind(source) if source.suffix.lower() == ".json" else None)
        if target:
            add(source, target)
        return found
    if (source / "halo3").is_dir():  # an MCC install, or a server's game folder with the optional variants
        for folder, target in MCC_SOURCES.items():
            for path in sorted((source / "halo3" / folder).glob("*")):
                if path.suffix.lower() in KINDS and KINDS[path.suffix.lower()] == target:
                    add(path, target)
        return found
    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        if path.suffix.lower() in KINDS:
            add(path, KINDS[path.suffix.lower()])
        elif path.suffix.lower() == ".json" and path.name != "package.toml":
            target = _json_kind(path)
            if target and "Mods" not in path.parts:
                add(path, target)
    return found


def import_content(items: list[Importable], content_root: Path, *, overwrite: bool = False,
                   subfolder: str = "From MCC") -> tuple[list[Path], int]:
    """Copy the items into content_root/<Maps|Game Modes>/<subfolder>/. Returns (copied, skipped as already there)."""
    copied: list[Path] = []
    skipped = 0
    for item in items:
        target = content_root / item.target / subfolder / item.source.name if subfolder else \
            content_root / item.target / item.source.name
        if target.exists() and not overwrite:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.source, target)
        copied.append(target)
    return copied, skipped


def content_root_of(server_folder: Path, config_name: str = "dedicated.toml") -> Path:
    from .tomledit import read_toml

    try:
        content = str(read_toml(server_folder / config_name).get("content") or "content")
    except (OSError, ValueError):
        content = "content"
    return (server_folder / content).resolve()
