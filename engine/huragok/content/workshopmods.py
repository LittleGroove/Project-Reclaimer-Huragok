"""Workshop mods as the files have them (0.7.0): what the servers' content folders hold that nothing keeps up to date,
or that an update left behind, said through the warnings catalogue (templates/warnings.toml, the mod- rules).
Nothing here writes into a server's folder: the server fetches and updates the items its dedicated.toml lists by
itself ([workshop] items, which are the host's to write), and Huragok no longer takes over hand copies, lists items,
switches mods off or keeps layouts in step (plan-0.7.0.md 4.4).

Every LOOK_SECONDS, on the files alone:

- a mod copied in by hand that names a Workshop item no server on its content folder lists (answer 18): the program
  never updates it (verified, S1 Q4); listed, the program fetches and updates it itself;
- a copy the program installed in Mods/Workshop/<item> whose item isn't listed any more: still loaded, never updated;
- a Workshop update's copy left in a content folder (.workshop-<item>-<pid>-<n>) by a server whose process is gone
  (answer 16, warned about only): a clean stop throws it away, a killed or crashed server leaves it, 1 GB and more;
- a mod's download kept in data/workshop-cache beside the installed copy, once an update has put it in (answer 20);
- the mods a server loads from this PC's own Steam Workshop folder that its content folder doesn't have (0.10 picks
  up every MCC Workshop mod on the PC, the host's own subscriptions too).

A mod's own Forge layouts reach content/Maps only when the host asks (layouts_by_hand: G → the server → 3 → L): S3
showed the server doesn't play them otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import steam
from ..core.files import write_atomic
from ..core.text import size_text
from ..games.bungie import GAMES, game_of_engine
from ..updates.workshop import RECEIPT, download_folder, is_installed, staged_copies
from .catalogue import DisabledModsError, Mod, disabled_mods, installed_mods, is_disabled, mod_package, sync_layouts
from .modcheck import plays_mods_of

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.serverwarnings import Warnings
    from ..core.settings import Settings
    from ..updates.workshop import WorkshopUpdateService

log = logging.getLogger(__name__)

LOOK_SECONDS = 60.0
FIRST_LOOK = 45.0  # after the servers have started
STEAM_AGAIN = 600.0  # this PC's Steam Workshop folder is read again after this long
RECORD = "workshop-mods.json"  # in records/: the layouts Huragok copied when asked, so a mod's next ones replace them
SOURCE = "workshop-files"  # what these warnings are kept under in the catalogue
SHOWN_MODS = 8  # names listed in the Steam folder's warning before "and N more"

Finding = tuple[str, str, "Path | str | None", dict[str, Any]]  # (rule, key, the file it's about, fields)


@dataclass
class Found:
    """What one content folder holds: the program's own Workshop copies and the hand copies naming an item."""

    managed: dict[int, Mod] = field(default_factory=dict)
    hand: dict[int, list[Mod]] = field(default_factory=dict)
    mods: list[Mod] = field(default_factory=list)  # every mod switched on


def is_managed(mod: Mod, content_root: Path) -> bool:
    """The program's own copy: content/Mods/Workshop/<item> with its receipt."""
    try:
        parts = mod.folder.relative_to(content_root).parts
    except ValueError:
        return False
    return (len(parts) == 3 and parts[0].casefold() == "mods" and parts[1].casefold() == "workshop"
            and parts[2].isdecimal() and is_installed(mod.folder))


def look_through(content_root: Path, version: str | None | Callable[[], str | None] = None) -> Found:
    """The mods in a content folder, sorted: the program's copies, hand copies naming an item, and every one switched
    on (disabled-mods.json, the host's, says which are off; one that can't be read counts no hand copies, since what
    the server leaves out can't be told). version: the server's program version, or how to learn it (asked only for
    a mod for another game than Halo 3); None: not known, taken as 0.10 or newer."""
    found = Found()
    try:
        disabled: list[str] | None = disabled_mods(content_root)
    except DisabledModsError:
        disabled = None
    known: list[str | None] = [] if callable(version) else [version]
    for mod in installed_mods(content_root):
        if disabled is not None and is_disabled(disabled, mod_package(content_root, mod)):
            continue
        found.mods.append(mod)
        game = game_of_engine(mod.engine)
        if game is None or game.key != "halo3":
            if not known:
                known.append(version())  # type: ignore[operator]
            if not plays_mods_of(game, known[0]):
                continue  # a game this program doesn't play
            if is_managed(mod, content_root):
                found.managed[int(mod.folder.name)] = mod
            continue  # a hand copy of another game's mod: 0.10's installer still says "targets an engine other than
            # Halo 3", so listing it couldn't help
        if is_managed(mod, content_root):
            found.managed[int(mod.folder.name)] = mod
        elif mod.workshop_id is not None and disabled is not None:
            found.hand.setdefault(mod.workshop_id, []).append(mod)
        elif mod.workshop_id is None and mod.folder.name.isdecimal() and disabled is not None and \
                mod.folder.parent.name.casefold() == "mods":
            found.hand.setdefault(int(mod.folder.name), []).append(mod)  # SteamCMD's folder copied in: named by its item
    return found


def folder_size(path: Path) -> int:
    total = 0
    try:
        for file in path.rglob("*"):
            try:
                if file.is_file():
                    total += file.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def program_running(pid: int) -> bool:
    """A Project Reclaimer process with this number is running (a number reused by another program doesn't count)."""
    import psutil

    try:
        process = psutil.Process(pid)
        names = [process.name()]
        try:  # under Wine the process is wine's: the program's own file is the first word of its command line
            names += process.cmdline()[:1]
        except (psutil.AccessDenied, OSError):
            pass
    except (psutil.Error, OSError):
        return False
    return any("reclaimer" in Path(name.replace("\\", "/")).name.lower() for name in names if name)


def steam_folder_mods(folder: Path | None) -> dict[int, str]:
    """The mods in this PC's own Steam Workshop folder for MCC: item -> its title (ModInfo.json), or its number."""
    found: dict[int, str] = {}
    if folder is None:
        return found
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdecimal() or not entry.is_dir():
            continue
        title = ""
        try:
            data = json.loads((entry / "ModInfo.json").read_text(encoding="utf-8-sig", errors="replace"))
            raw = data.get("Title") if isinstance(data, dict) else None
            raw = raw.get("Neutral") if isinstance(raw, dict) else raw
            title = raw.strip() if isinstance(raw, str) else ""
        except (OSError, ValueError):
            pass
        found[int(entry.name)] = title or f"Workshop item {entry.name}"
    return found


class WorkshopFiles:
    def __init__(self, settings: Settings, warnings: Warnings | None, updates: WorkshopUpdateService | None = None, *,
                 steam_folder: Callable[[], Path | None] = steam.workshop_content,
                 version_of: Callable[[ServerConfig], str | None] | None = None) -> None:
        self.settings = settings
        self.warnings = warnings
        self.updates = updates
        self.steam_folder = steam_folder
        self.version_of = version_of  # the server's program version (None: not known, taken as 0.10 or newer)
        self._steam: tuple[float, Path | None, dict[int, str]] = (float("-inf"), None, {})
        self._sizes: dict[Path, int] = {}  # a leftover's size, measured once

    async def run(self) -> None:
        await asyncio.sleep(FIRST_LOOK)
        while True:
            self.say(await asyncio.to_thread(self.look))
            await asyncio.sleep(LOOK_SECONDS)

    def say(self, found: dict[str, list[Finding]]) -> None:
        """Into the catalogue: what's found now, and what was found before and isn't any more cleared."""
        if self.warnings is None:
            return
        for server, findings in found.items():
            self.warnings.sync(server, SOURCE, findings)

    def _steam_mods(self) -> tuple[Path | None, dict[int, str]]:
        when, folder, mods = self._steam
        if time.monotonic() - when >= STEAM_AGAIN:
            try:
                folder = self.steam_folder()
            except OSError:
                folder = None
            mods = steam_folder_mods(folder)
            self._steam = (time.monotonic(), folder, mods)
        return folder, mods

    def look(self) -> dict[str, list[Finding]]:
        """Every server's findings, by server. In a thread: it reads the content folders."""
        servers = list(self.settings.servers)
        found: dict[str, list[Finding]] = {s.name: [] for s in servers}
        roots: dict[Path, list[ServerConfig]] = {}
        for server in servers:
            if server.content_dir is not None:
                roots.setdefault(server.content_dir, []).append(server)
        steam_folder, steam_mods = self._steam_mods()
        for root, using in roots.items():
            try:
                self._look_at(root, using, found, steam_folder, steam_mods)
            except Exception as error:  # noqa: BLE001 - one content folder mustn't stop the others being looked at
                log.warning("[%s] couldn't look at its Workshop mods: %s", ", ".join(s.name for s in using), error)
        self._downloads_kept(servers, found)
        return found

    def _look_at(self, root: Path, using: list[ServerConfig], found: dict[str, list[Finding]],
                 steam_folder: Path | None, steam_mods: dict[int, str]) -> None:
        listed = {item for s in using for item in s.workshop_items}  # any server on the folder updates the copy
        contents = look_through(root)
        for item, mods in sorted(contents.hand.items()):
            if item in listed:
                continue
            for mod in mods:
                where = self._relative(root, mod.folder)
                for server in using:
                    found[server.name].append(("mod-hand-copy", f"{item}:{where}", mod.folder / "ModInfo.json",
                                               {"mod": mod.title, "item": item, "folder": where}))
        for item, mod in sorted(contents.managed.items()):
            if item not in listed:
                for server in using:
                    found[server.name].append(("mod-unlisted", str(item), mod.folder / RECEIPT, {
                        "mod": mod.title, "item": item, "folder": self._relative(root, mod.folder)}))
        for copy in staged_copies(root):
            if program_running(copy.pid):
                continue
            if copy.path not in self._sizes:
                self._sizes[copy.path] = folder_size(copy.path)
            found[using[0].name].append(("mod-leftover", copy.path.name, None, {
                "folder": str(copy.path), "size": size_text(self._sizes[copy.path]), "pid": copy.pid}))
        if steam_mods:
            here = set(contents.managed) | set(contents.hand) | {m.workshop_id for m in contents.mods if m.workshop_id}
            here |= listed | self._installed_items(root)
            only = {item: title for item, title in steam_mods.items() if item not in here}
            if only:
                names = sorted(only.values(), key=str.casefold)
                shown = ", ".join(names[:SHOWN_MODS]) + (f" and {len(names) - SHOWN_MODS} more"
                                                         if len(names) > SHOWN_MODS else "")
                for server in using:
                    version = self.version_of(server) if self.version_of is not None else None
                    if GAMES["reach"].plays_on(version):  # 0.10 or newer (or not known)
                        found[server.name].append(("mod-steam-folder", "", None, {
                            "count": len(only), "folder": str(steam_folder), "mods": shown}))

    def _downloads_kept(self, servers: list[ServerConfig], found: dict[str, list[Finding]]) -> None:
        """Answer 20: once an update has put a mod in, its download stays in the server folder's workshop-cache."""
        if self.updates is None:
            return
        seen: set[Path] = set()
        for folder, item in sorted(self.updates.installed_here):
            first = next((s for s in servers if s.folder == folder), None)
            if first is None:
                continue
            path = download_folder(first, item)
            if path in seen or not path.is_dir():
                continue
            seen.add(path)
            found[first.name].append(("mod-download-kept", str(item), None, {
                "mod": self.updates.name_of(first, item), "item": item,
                "folder": self._relative(first.folder, path), "size": size_text(folder_size(path))}))

    @staticmethod
    def _installed_items(root: Path) -> set[int]:
        """The items the program installed in Mods/Workshop (by folder, whatever their ModInfo.json says)."""
        try:
            return {int(p.name) for p in (root / "Mods" / "Workshop").iterdir() if p.name.isdecimal() and p.is_dir()}
        except OSError:
            return set()

    @staticmethod
    def _relative(root: Path, folder: Path) -> str:
        try:
            return folder.relative_to(root).as_posix()
        except ValueError:
            return str(folder)


def layouts_by_hand(settings: Settings, root: Path, mod: Mod) -> list[str]:
    """The window's L, with or without Huragok running: the mod's layouts into content/Maps, remembered as Huragok's
    (so the mod's next versions replace them when L is pressed again). Only when the host asks: S3 showed the server
    doesn't play a mod's own layouts otherwise."""
    path = settings.records / RECORD
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    written = data.setdefault("layouts", {}).setdefault(str(root), {})
    result = sync_layouts(mod, root, written)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(data, indent=1, sort_keys=True) + "\n")
    except OSError:
        pass  # the files are in; Huragok tells its own from the host's by content next time
    return [*result.copied, *result.updated, *result.replaced]


__all__ = ["Found", "WorkshopFiles", "folder_size", "is_managed", "layouts_by_hand", "look_through",
           "program_running", "steam_folder_mods"]
