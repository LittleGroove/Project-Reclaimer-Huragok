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

MCC is found only where the host or Steam says it is (find_mcc): [setup] mcc_folder, then Steam's own records:
where Steam is (the registry's SteamPath on Windows, Steam's ~/.steam/steam link on Linux), the libraries its
libraryfolders.vdf lists with the games each holds, and the folder MCC's appmanifest_976730.acf names. Nothing
else on the PC is looked at: no drive is searched and no folder guessed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

MCC_APP = "976730"  # MCC on Steam
MCC_FOLDER = "Halo The Master Chief Collection"  # the folder Steam installs it in (its appmanifest says so)
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
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


def _steam_roots() -> list[Path]:
    """Where Steam is, as Steam itself records it: on Windows the registry's SteamPath (and the InstallPath its
    installer writes), and only when neither is there, Steam's own install folders; on Linux the ~/.steam links
    Steam keeps, its data folder and Flatpak's. Each once, and only one that's there."""
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg
        except ImportError:
            winreg = None  # type: ignore[assignment]
        for hive, key, value in (("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
                                 ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            if winreg is None:
                break
            try:
                with winreg.OpenKey(getattr(winreg, hive), key) as handle:
                    roots.append(Path(str(winreg.QueryValueEx(handle, value)[0])))
            except OSError:
                continue
        if not roots:
            roots += [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
    else:
        roots += [Path(p).expanduser() for p in ("~/.steam/steam", "~/.steam/root", "~/.local/share/Steam",
                                                  "~/.steam/debian-installation",
                                                  "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")]
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
            if key not in seen and root.is_dir():
                seen.add(key)
                found.append(root)
        except OSError:
            continue
    return found


def read_vdf(path: Path) -> dict[str, Any]:
    """Steam's KeyValues text (libraryfolders.vdf, appmanifest_*.acf) as nested dicts; {} when it can't be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    root: dict[str, Any] = {}
    stack = [root]
    key: str | None = None
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        token, brace = match.group(1), match.group(2)
        if brace == "{":
            child: dict[str, Any] = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = re.sub(r"\\(.)", r"\1", token)
        else:
            stack[-1][key] = re.sub(r"\\(.)", r"\1", token)
            key = None
    return root


def steam_libraries() -> Iterator[tuple[Path, dict[str, Any] | None]]:
    """Each Steam library, as Steam lists them (steamapps/libraryfolders.vdf), with the games Steam says are in it
    (None from the old file, which doesn't say). Steam's own folder first."""
    seen: set[str] = set()
    for root in _steam_roots():
        listed: list[tuple[Path, dict[str, Any] | None]] = []
        for where in ("steamapps", "config"):  # Steam keeps the list in steamapps/, older Steam in config/
            data = read_vdf(root / where / "libraryfolders.vdf")
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for number, entry in (folders.items() if isinstance(folders, dict) else []):
                if not str(number).isdigit():
                    continue
                if isinstance(entry, dict) and entry.get("path"):
                    apps = entry.get("apps")
                    listed.append((Path(str(entry["path"])), apps if isinstance(apps, dict) else None))
                elif isinstance(entry, str) and entry:  # before 2021: "1" "D:\\SteamLibrary"
                    listed.append((Path(entry), None))
        listed.append((root, None))  # Steam's own folder is a library, listed or not
        for library, apps in listed:
            key = str(library).lower().replace("/", "\\").rstrip("\\")
            if key in seen:
                continue
            seen.add(key)
            yield library, apps


def mcc_in(library: Path) -> Path | None:
    """The folder MCC's own manifest in this library names (appmanifest_976730.acf), or None: not installed here."""
    state = read_vdf(library / "steamapps" / f"appmanifest_{MCC_APP}.acf").get("AppState")
    folder = state.get("installdir") if isinstance(state, dict) else None
    return library / "steamapps" / "common" / str(folder) if folder else None


def _mcc_candidates(hint: str | None) -> Iterator[Path]:
    if hint and clean_path(hint):
        yield Path(clean_path(hint))
    for library, apps in steam_libraries():
        if apps is not None and MCC_APP not in apps:
            continue  # Steam says MCC isn't in this library
        found = mcc_in(library)
        if found is not None:
            yield found


def find_mcc(hint: str | None = None) -> Path | None:
    """Where MCC is: the hint ([setup] mcc_folder), else where Steam's own records say it's installed. The first
    that has halo3/ wins; nothing past it is looked at, and nothing Steam doesn't name is looked at at all."""
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
