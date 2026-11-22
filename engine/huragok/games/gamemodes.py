"""A server's game modes and Forge layouts, read the way Project Reclaimer 0.10 writes them
(plans/server-files-audit/gamemodes-layouts-format.md, verified on 0.10.8). Read only: they're the host's files.

Game modes, anywhere under content/Game Modes:
- `.json`: a game type (`version`, `name`, `mode`, `rules`, `options`…; 0.9's scripted ones carry a `script`);
- `.rmeg` (also `.rvt`): a scripted game mode, a TOML header inside `--[=[ mode … ]=]` (the same fields as a `.json`
  game type: `name`, `mode`…) and a Megalo script. Without a header the program plays it as Slayer, named after its
  file; the header may stand anywhere in the file;
- `.bin`: an MCC game variant (BLF), named in the listing by its own title (Huragok, not reading it, names it by its
  file); one that isn't BLF is skipped by the program ("Expected an MCC BLF variant file").
Playlists, `game =` and `variant =` name a mode by its `name` or by its path inside the content folder.

Forge layouts (saved maps), anywhere under content/Maps: `.json` with a `map` block (its `name` is what playlists name;
`base_map` says the map it was made on: `id`, `stem`, `title`), and MCC's `.mvar` map variants. A layout's objects,
as Project Reclaimer 0.10 counts them against its 9,999 (projectreclaimer.dev/forge "Object limits"; counted the way
DEPLOY/HugeGrassFix/tools/layout_room.py does, checked on 0.10.7): the objects standing in `map.data`'s 640 records
and every entry of the `frozen` list.

Each file is read once while it stays the same size and time (a content folder can hold hundreds of layouts).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import struct
import threading
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GAME_MODES = "Game Modes"
MAPS = "Maps"
IMAGES = "Images"
BUNDLED = "Game Modes/Project Reclaimer"  # where the program writes the modes it ships (and puts back a missing one)
SCRIPTED = (".rmeg", ".rvt")
BLF = b"_blf"
BOM = bytes((0xEF, 0xBB, 0xBF))  # a UTF-8 byte-order mark
HEADER = re.compile(r"--\[=\[[ \t]*mode\b[^\n]*\n(?P<toml>.*?)\]=\]", re.DOTALL)
OBJECTS_MOST = 9999  # Project Reclaimer 0.10's Forge map limit, the map's own objects included
RECORDS, RECORD_SIZE, RECORD_COUNT = 0x130, 0x54, 640  # in map.data (57,504 bytes): the 640 object records
STANDING, DELETED = 0x1, 0x20  # a record's flags: an object there, and one deleted
FROZEN = b"FRZ1"  # the frozen list: the objects past the 640 records ("FRZ1", u32 count, …)


@dataclass(frozen=True)
class GameModeFile:
    """A game mode file in a content folder, as Huragok reads it."""

    path: Path
    relative: str  # inside the content folder: "Game Modes/SPR/BR Fiesta CTF.json"
    kind: str  # json, rmeg, bin
    name: str  # what playlists, `game =` and the lobby name it by
    mode: str  # the game type it plays ("Slayer", "CaptureTheFlag"…); "" for a .bin
    scripted: bool = False
    version: int | None = None
    problem: str = ""  # why it can't be read as it is ("" when it can): the program skips it too, its words say why

    @property
    def bundled(self) -> bool:
        """One the program ships and writes itself (Game Modes/Project Reclaimer): put back when deleted."""
        return self.relative.casefold().startswith(BUNDLED.casefold() + "/")

    @property
    def references(self) -> set[str]:
        """Every way a playlist, `game =` or `variant =` can name it, folded: its name, its path in content/ (with and
        without the extension), its file name and stem."""
        relative = self.relative
        return {text.casefold() for text in (self.name, relative, relative.rsplit(".", 1)[0], self.path.name,
                                             self.path.stem, f"content/{relative}") if text}


@dataclass(frozen=True)
class LayoutFile:
    """A Forge layout (saved map) or MCC map variant in a content folder."""

    path: Path
    relative: str  # "Maps/SPR/GroundWar.json"
    kind: str  # json, mvar
    name: str  # what playlists and `map =` name it by
    base_stem: str = ""  # base_map.stem: the map file it was made on ("hugegrass_spr", "guardian")
    base_title: str = ""
    base_id: int | None = None  # base_map.id: the map ID it was made on
    objects: int | None = None  # as the program counts them against OBJECTS_MOST; None: not counted
    version: int | None = None
    problem: str = ""

    @property
    def references(self) -> set[str]:
        relative = self.relative
        return {text.casefold() for text in (self.name, relative, relative.rsplit(".", 1)[0], self.path.name,
                                             self.path.stem, f"content/{relative}") if text}


_cache: dict[str, tuple[tuple[int, int], Any]] = {}
_lock = threading.Lock()


def _cached(path: Path, read: Any) -> Any:
    """read(path), remembered while the file keeps its size and time."""
    try:
        stat = path.stat()
    except OSError:
        return read(path)
    key, stamp = str(path), (stat.st_mtime_ns, stat.st_size)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    value = read(path)
    with _lock:
        if len(_cache) > 4096:
            _cache.clear()
        _cache[key] = (stamp, value)
    return value


def _relative(path: Path, content_dir: Path) -> str:
    try:
        return path.relative_to(content_dir).as_posix()
    except ValueError:
        return path.as_posix()


def _version(data: dict[str, Any]) -> int | None:
    value = data.get("version")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def rmeg_header(text: str) -> dict[str, Any] | None:
    """A .rmeg's header (`--[=[ mode` … `]=]`, TOML) as a table; None when it has none. Raises ValueError with why
    when the header can't be read."""
    match = HEADER.search(text)
    if match is None:
        return None
    try:
        return tomllib.loads(match.group("toml"))
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"its header can't be read: {error}") from None


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _json(path: Path) -> Any:
    """A game file's JSON. ValueError with why the program can't read it: a byte-order mark (what old Notepad adds)
    makes 0.10.8 skip the file ("expected value at line 1 column 1"), so it isn't forgiven here either."""
    raw = path.read_bytes()
    if raw.startswith(BOM):
        raise ValueError("it starts with a byte-order mark, which the program can't read (save it as UTF-8 without "
                         "one)")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise ValueError(f"it isn't UTF-8 text ({error.reason})") from None
    except ValueError as error:
        raise ValueError(f"it isn't JSON the program reads ({error})") from None


def _game_mode(path: Path) -> tuple[str, str, str, bool, int | None, str] | None:
    """(kind, name, mode, scripted, version, problem) of a file in Game Modes; None: not a game mode file."""
    suffix = path.suffix.lower()
    if suffix in SCRIPTED:
        try:
            header = rmeg_header(_read_text(path))
        except OSError as error:
            return "rmeg", path.stem, "", True, None, f"can't be read ({error.strerror or error})"
        except ValueError as error:
            return "rmeg", path.stem, "", True, None, str(error)
        if header is None:  # the program plays it as Slayer, named after its file
            return "rmeg", path.stem, "Slayer", True, None, ""
        name = header.get("name")
        return ("rmeg", name.strip() if isinstance(name, str) and name.strip() else path.stem,
                str(header.get("mode") or "Slayer"), True, _version(header), "")
    if suffix == ".bin":
        try:
            with path.open("rb") as file:
                head = file.read(len(BLF))
        except OSError as error:
            return "bin", path.stem, "", False, None, f"can't be read ({error.strerror or error})"
        return "bin", path.stem, "", False, None, "" if head == BLF else "it isn't an MCC variant file (BLF)"
    if suffix != ".json":
        return None
    try:
        data = _json(path)
    except OSError as error:
        return "json", path.stem, "", False, None, f"can't be read ({error.strerror or error})"
    except ValueError as error:
        return "json", path.stem, "", False, None, str(error)
    if not isinstance(data, dict) or "mode" not in data:
        return "json", str(data.get("name") or path.stem) if isinstance(data, dict) else path.stem, "", False, \
            _version(data) if isinstance(data, dict) else None, "it isn't a game type (no mode)"
    name = data.get("name")
    return ("json", name.strip() if isinstance(name, str) and name.strip() else path.stem, str(data.get("mode")),
            bool(data.get("script")), _version(data), "")


def read_game_mode(path: Path, content_dir: Path) -> GameModeFile | None:
    """One file of Game Modes as a game mode; None for a file that isn't one (a README, a picture)."""
    found = _cached(path, _game_mode)
    if found is None:
        return None
    kind, name, mode, scripted, version, problem = found
    return GameModeFile(path, _relative(path, content_dir), kind, name, mode, scripted, version, problem)


def _files(folder: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Every file with one of the suffixes under `folder`, sorted, hidden ones (.name) and Huragok's drafts left out."""
    if not folder.is_dir():
        return []
    found = []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in names:
            if name.startswith(".") or not name.lower().endswith(suffixes):
                continue
            found.append(Path(root) / name)
    return sorted(found, key=lambda p: str(p).casefold())


def list_game_modes(content_dir: Path | None) -> list[GameModeFile]:
    """Every game mode in a content folder's Game Modes (and its subfolders): .json, .rmeg/.rvt and .bin."""
    if content_dir is None:
        return []
    found = []
    for path in _files(content_dir / GAME_MODES, (".json", ".bin", *SCRIPTED)):
        mode = read_game_mode(path, content_dir)
        if mode is not None:
            found.append(mode)
    return found


def layout_objects(data: dict[str, Any]) -> int | None:
    """A saved map's objects as Project Reclaimer 0.10 counts them: those standing in map.data's 640 records and every
    entry of its frozen list. None when its data can't be read (the program says why it skips it)."""
    block = data.get("map")
    if not isinstance(block, dict) or not isinstance(block.get("data"), str):
        return None
    try:
        raw = base64.b64decode(block["data"], validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(raw) < RECORDS + RECORD_SIZE * RECORD_COUNT:
        return None
    standing = 0
    for number in range(RECORD_COUNT):
        (flags,) = struct.unpack_from("<H", raw, RECORDS + RECORD_SIZE * number)
        if flags & STANDING and not flags & DELETED:
            standing += 1
    frozen = 0
    if isinstance(block.get("frozen"), str) and block["frozen"]:
        try:
            listed = base64.b64decode(block["frozen"], validate=False)
        except (binascii.Error, ValueError):
            return None
        if listed[:4] != FROZEN or len(listed) < 8:
            return None
        (frozen,) = struct.unpack_from("<I", listed, 4)
    return standing + frozen


def _layout(path: Path) -> tuple[str, str, str, str, int | None, int | None, int | None, str] | None:
    """(kind, name, base stem, base title, base id, objects, version, problem) of a file in Maps; None: not one."""
    suffix = path.suffix.lower()
    if suffix == ".mvar":
        return "mvar", path.stem, "", "", None, None, None, ""
    if suffix != ".json":
        return None
    try:
        data = _json(path)
    except OSError as error:
        return "json", path.stem, "", "", None, None, None, f"can't be read ({error.strerror or error})"
    except ValueError as error:
        return "json", path.stem, "", "", None, None, None, str(error)
    if not isinstance(data, dict):
        return "json", path.stem, "", "", None, None, None, "it isn't a saved map"
    name = data.get("name")
    name = name.strip() if isinstance(name, str) and name.strip() else path.stem
    base = data.get("base_map") if isinstance(data.get("base_map"), dict) else {}
    stem = base.get("stem") if isinstance(base.get("stem"), str) else ""
    title = base.get("title") if isinstance(base.get("title"), str) else ""
    made_on = base.get("id") if isinstance(base.get("id"), int) and not isinstance(base.get("id"), bool) else None
    problem = "" if isinstance(data.get("map"), dict) else "it has no map block (a saved map's objects)"
    return "json", name, stem, title, made_on, layout_objects(data), _version(data), problem


def read_layout(path: Path, content_dir: Path) -> LayoutFile | None:
    found = _cached(path, _layout)
    if found is None:
        return None
    kind, name, stem, title, made_on, objects, version, problem = found
    return LayoutFile(path, _relative(path, content_dir), kind, name, stem, title, made_on, objects, version, problem)


def list_layouts(content_dir: Path | None) -> list[LayoutFile]:
    """Every Forge layout (.json) and map variant (.mvar) in a content folder's Maps, subfolders too."""
    if content_dir is None:
        return []
    found = []
    for path in _files(content_dir / MAPS, (".json", ".mvar")):
        layout = read_layout(path, content_dir)
        if layout is not None:
            found.append(layout)
    return found


__all__ = ["BUNDLED", "GAME_MODES", "GameModeFile", "IMAGES", "LayoutFile", "MAPS", "OBJECTS_MOST", "SCRIPTED",
           "layout_objects", "list_game_modes", "list_layouts", "read_game_mode", "read_layout", "rmeg_header"]
