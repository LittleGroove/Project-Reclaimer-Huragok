"""Importing map and game variants into a server's content folder: from MCC's own files, a folder, or a file.

Halo: The Master Chief Collection ships every matchmaking variant 343 made for Halo 3 in
`halo3/hopper_map_variants/*.mvar` (320 of them: MLG, Team Hardcore, Infection, Grifball…)
and `halo3/hopper_game_variants/*.bin` (334), plus `halo3/map_variants` and
`halo3/game_variants`. A dedicated server lists only what's in its content folder, so
this copies them there: `.mvar` to content/Maps, `.bin` to content/Game Modes, and
saved `.json` maps and game types to the folder they belong in. The server picks maps
and game types up while running (0.9.18); `dedicated content` lists them by name.

A server's own content folder can be hard links to the root's shared files (serverops.own_content), so every
write into a content folder goes through copy_replacing: a new file moved into place, never an overwrite of the
linked one, which would change the shared copy every other server plays.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MCC_FOLDER = "Halo The Master Chief Collection"
STEAM_DEFAULT = Path(r"C:\Program Files (x86)\Steam\steamapps\common") / MCC_FOLDER
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
DRIVE_FIXED = 3  # GetDriveTypeW: a local disk (not removable, optical, network or RAM)
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


def clean_path(text: str) -> str:
    """A path as typed or pasted: spaces and the quotes Explorer's "Copy as path" adds taken off."""
    text = str(text).strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def safe_file_name(text: str, fallback: str) -> str:
    """A name usable as one file or folder name on Windows and Linux: no separators or characters Windows refuses,
    not "." or "..", not a device name (CON, NUL…), no trailing dot or space; `fallback` when nothing's left."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "-", str(text)).strip().strip(".").strip()
    if not name or name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        return fallback
    return name


def copy_replacing(source: Path, target: Path) -> None:
    """Copy a file into place as a new file (a temp beside it, then os.replace). A target that's a hard link to
    the root's shared content gets a file of its own; writing over it in place would change the shared one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.huragok-new")
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _fixed_drives() -> list[str]:
    """This PC's local disks (C:, D:…), asked of Windows without touching any drive: an empty DVD drive, a
    disconnected network drive or a card reader isn't probed (each can stall for seconds)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mask = kernel32.GetLogicalDrives()
        return [f"{letter}:\\" for number, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
                if mask >> number & 1 and kernel32.GetDriveTypeW(f"{letter}:\\") == DRIVE_FIXED]
    except (AttributeError, OSError):
        return []


def _steam_roots() -> list[Path]:
    """Where Steam itself is: the registry's SteamPath, then the usual folders."""
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                roots.append(Path(str(winreg.QueryValueEx(key, "SteamPath")[0])))
        except OSError:
            pass
        roots += [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
    else:
        roots += [Path(p).expanduser() for p in ("~/.steam/steam", "~/.local/share/Steam",
                                                  "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")]
    return roots


def _steam_libraries() -> Iterator[Path]:
    """Steam's library folders as Steam lists them (steamapps/libraryfolders.vdf), each Steam root first."""
    for root in _steam_roots():
        yield root
        try:
            text = (root / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            yield Path(match.group(1).replace("\\\\", "\\"))


def _mcc_candidates(hint: str | None) -> Iterator[Path]:
    if hint and clean_path(hint):
        yield Path(clean_path(hint))
    for library in _steam_libraries():
        yield library / "steamapps" / "common" / MCC_FOLDER
    yield STEAM_DEFAULT
    if os.name == "nt":
        for drive in _fixed_drives():
            for library in ("SteamLibrary", "Steam", "Games\\Steam", "Program Files (x86)\\Steam",
                            "Program Files\\Steam"):
                yield Path(drive) / library / "steamapps" / "common" / MCC_FOLDER


def find_mcc(hint: str | None = None) -> Path | None:
    """Where MCC is: the hint, the Steam libraries Steam lists, Steam's default folder, or a Steam library on one
    of this PC's local disks. The first that has halo3/ wins; nothing past it is looked at."""
    seen: set[str] = set()
    for path in _mcc_candidates(hint):
        if str(path).lower() in seen:
            continue
        seen.add(str(path).lower())
        try:
            if (path / "halo3").is_dir():
                return path
        except OSError:
            continue
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
    source = Path(clean_path(str(source)))
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
        copy_replacing(item.source, target)
        copied.append(target)
    return copied, skipped


def content_root_of(server_folder: Path, config_name: str = "dedicated.toml") -> Path:
    from .tomledit import read_toml

    try:
        content = str(read_toml(server_folder / config_name).get("content") or "content")
    except (OSError, ValueError):
        content = "content"
    return (server_folder / content).resolve()
