"""Workshop mods kept managed: every Workshop mod a server loads is kept up to date, and its Forge layouts with it.

The server program updates only the items its dedicated.toml lists ([workshop] items), which it fetches into
content/Mods/Workshop/<item> with its receipts. Hosts copy mods in by hand too (from MCC's Workshop folder, or a
SteamCMD download): the program loads such a copy and names its maps by the item its ModInfo.json gives
(HostedModIds: workshop:<item>/<map>, as for its own copy), but never updates it. An item taken out of the list
leaves its folder behind, and the server loads that all the same (both tested 2 Nov 2026 on 0.9.23). So every
LOOK_SECONDS, on the files alone (Steam is asked only about a mod that's new to Huragok):

1. Adopt ([workshop_updates] adopt). A copy the program installed that isn't listed is listed again: no restart. A
   hand copy: the Workshop's own copy is fetched into a staging folder beside the content folder while the server
   runs (`dedicated mods` on a dedicated.toml of its own: 33 s for 1.1 GB, tested), then with the server stopped
   after its game (at once when nobody's on) the hand copy goes to `content set aside`, the fetched copy takes its
   place in Mods/Workshop/<item>, the item is listed, and the server starts on it. Playlists keep working: they name
   the item's maps the same way. Left as they are: a mod switched off (disabled-mods.json), one for another game, one
   Steam doesn't list publicly (an author's own hidden upload), one whose maps aren't the item's (made from it, like
   a host's own version of a map that kept the item's ID), one in a content folder several servers share (they'd all
   have to stop at once), and every one on a server whose Workshop updates are off.
2. Fetch. An item added to a server's [workshop] items is fetched at once, whoever added it: a stopped server's with
   `dedicated mods`; a running server fetches its own (auto_reload), or with auto_reload off it's staged and put in
   place for its next start (a new folder: nothing the server has open).
3. Layouts ([workshop_updates] layouts). The Forge layouts each mod ships go into content/Maps/<mod>/ and follow its
   new versions (catalogue.sync_layouts): at every look, after an update's job and after an adoption, before the
   server starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import steam
from ..core.background import background
from ..core.files import write_atomic
from ..core.jobs import StoppedJob
from ..core.tomledit import read_toml, set_table_key
from ..core.web import UpdateError
from ..lore import callout
from ..updates.workshop import RECEIPT, date_text, installed_version
from .catalogue import DISABLED_MODS, DisabledModsError, Mod, disabled_mods, installed_mods, is_disabled, \
    mod_package, set_aside, set_mod_disabled, sync_layouts
from .gamefiles import read_paths

if TYPE_CHECKING:
    from ..core.events import EventBus
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings
    from ..program.dedicated import Dedicated
    from ..servers.fleet import Lifecycle
    from ..updates.workshop import WorkshopUpdateService

log = logging.getLogger(__name__)

LOOK_SECONDS = 60.0
FIRST_LOOK = 45.0  # after the servers have started, and after the Workshop updates' first look at Steam
STAGING = ".huragok-workshop-staging"  # beside the content folder: same drive, so putting a copy in place is instant
RECORD = "workshop-mods.json"  # in records/: the layouts Huragok wrote, the mods it leaves as they are and why
JOB = "workshop-adopt"
FETCH_AGAIN = 600.0  # a fetch that failed is tried again after this long
MAX_TRIES = 3  # fetches of one item before Huragok stops trying it until it starts again
STEAM_AGAIN = 3600.0  # what Steam said about a hand copy's item is asked again after this long
STOPPED = ("idle", "faulted", "offline")  # nothing running and nothing starting
UP = ("running", "countdown", "external")
ADOPTED_KEPT = 50  # adoptions remembered in the record


@dataclass
class HandCopy:
    """A Workshop item's hand copies in one server's content folder, and the Workshop's own copy fetched for them."""

    item: int
    title: str
    folders: list[Path]
    staged: Path | None = None  # the fetched copy, in the staging folder; None: the program's own copy is there
    staging: Path | None = None  # its staging folder, removed once it's in place
    version: int | None = None  # the fetched copy's (Unix seconds)


@dataclass
class Found:
    """What one content folder holds: the program's own Workshop copies and the hand copies naming an item."""

    managed: dict[int, Mod] = field(default_factory=dict)
    hand: dict[int, list[Mod]] = field(default_factory=dict)
    mods: list[Mod] = field(default_factory=list)  # every mod switched on, for its layouts


def is_managed(mod: Mod, content_root: Path) -> bool:
    """The program's own copy: content/Mods/Workshop/<item> with its receipt."""
    try:
        parts = mod.folder.relative_to(content_root).parts
    except ValueError:
        return False
    return (len(parts) == 3 and parts[0].casefold() == "mods" and parts[1].casefold() == "workshop"
            and parts[2].isdecimal() and (mod.folder / RECEIPT).is_file())


def look_through(content_root: Path) -> Found:
    """The mods in a content folder, sorted: the program's copies, hand copies naming an item, and every one switched
    on. A disabled-mods.json that can't be read switches nothing off here, and no hand copy is taken over: what the
    server leaves out can't be told."""
    found = Found()
    try:
        disabled: list[str] | None = disabled_mods(content_root)
    except DisabledModsError:
        disabled = None
    for mod in installed_mods(content_root):
        if disabled is not None and is_disabled(disabled, mod_package(content_root, mod)):
            continue
        found.mods.append(mod)
        if mod.engine and mod.engine.casefold() != "halo3":
            continue
        if is_managed(mod, content_root):
            found.managed[int(mod.folder.name)] = mod
        elif mod.workshop_id is not None and disabled is not None:
            found.hand.setdefault(mod.workshop_id, []).append(mod)
        elif mod.workshop_id is None and mod.folder.name.isdecimal() and disabled is not None and \
                mod.folder.parent.name.casefold() == "mods":
            found.hand.setdefault(int(mod.folder.name), []).append(mod)  # SteamCMD's folder copied in: named by its item
    return found


def map_files(folder: Path) -> set[str]:
    maps = folder / "maps"
    return {p.stem.casefold() for p in maps.glob("*.map")} if maps.is_dir() else set()


def same_mod(hand: list[Path], fetched: Path) -> bool:
    """The hand copies are the item, maybe an older version: every map they have, the item has too. A mod made from
    an item that kept its ID has maps of its own."""
    fetched_maps = map_files(fetched)
    return all(map_files(folder) <= fetched_maps for folder in hand)


def toml_text(value: str) -> str:
    return json.dumps(value)  # a TOML basic string: JSON's escapes are TOML's


class WorkshopKeeper:
    def __init__(self, settings: Settings, bus: EventBus, lifecycle: Lifecycle, updates: WorkshopUpdateService,
                 tool: Callable[[], Dedicated] | None = None) -> None:
        self.settings = settings
        self.config = settings.workshop_updates
        self.bus = bus
        self.lifecycle = lifecycle
        self.updates = updates
        self.tool = tool if tool is not None else updates.tool
        self.answers: dict[int, tuple[float, steam.SteamItem | None]] = {}  # item -> (when asked, Steam's; None: unlisted)
        self.adopting: dict[str, dict[int, HandCopy]] = {}  # server -> item -> staged, waiting for its stop
        self.tries: dict[tuple[str, int], tuple[float, int]] = {}  # (server, item) -> (last try, tries)
        self.said: set[tuple[Any, ...]] = set()  # what's been logged once
        self.fetching: set[str] = set()  # servers a fetch runs for now
        self.looking: set[str] = set()  # servers being looked at now
        self.lock = threading.Lock()  # the record file: the window's L writes it too
        self._fetch_lock: asyncio.Lock | None = None  # one fetch at a time: SteamCMD's sign-ins stay few
        updates.after_install = self.layouts_for
        self.clear_staging()

    # The record ----------------------------------------------------------------------------------------------

    @property
    def record_path(self) -> Path:
        return self.settings.records / RECORD

    def read_record(self) -> dict[str, Any]:
        try:
            data = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_record(self, data: dict[str, Any]) -> None:
        try:
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.record_path, json.dumps(data, indent=1, sort_keys=True) + "\n")
        except OSError as error:
            log.warning("Couldn't write %s (%s): the layouts Huragok wrote are told apart from yours again next time",
                        self.record_path.name, error)

    # Looking ------------------------------------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.sleep(FIRST_LOOK)
        while True:
            await self.look()
            await asyncio.sleep(LOOK_SECONDS)

    async def look(self) -> None:
        """Layouts, items to list, items to fetch and hand copies to take over, server by server."""
        if self.config.layouts:
            await asyncio.to_thread(self.sync_layouts)
        for server in list(self.settings.servers):
            await self.look_at(server)

    async def look_at(self, server: ServerConfig) -> None:
        if server.content_dir is None or server.name in self.looking:
            return
        self.looking.add(server.name)
        try:
            found = await asyncio.to_thread(look_through, server.content_dir)
            if self.config.adopt and self._updated(server):
                await asyncio.to_thread(self.list_unlisted, server, found)
            await self.fetch_missing(server)
            if self.config.adopt and self._updated(server):
                await self.adopt(server, found)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one server's mods mustn't stop the others being looked at
            log.exception("[%s] couldn't look at its Workshop mods", server.name)
        finally:
            self.looking.discard(server.name)

    def look_now(self, name: str) -> tuple[bool, str]:
        """The window's A: the server's new items fetched now rather than at the next look."""
        server = next((s for s in self.settings.servers if s.name == name), None)
        if server is None:
            return False, f"no server called {name}"
        for item in server.workshop_items:
            self.tries.pop((server.name, item), None)  # a person added it: tried again however often it failed
        background(self.look_at(server), f"[{name}] looking at its Workshop mods", log)
        return True, "Huragok fetches what's new now; the log says when it's in"

    def _updated(self, server: ServerConfig) -> bool:
        return self.settings.of(server).workshop_updates != "off"

    def _once(self, key: tuple[Any, ...], level: int, text: str, *args: Any) -> None:
        if key not in self.said:
            self.said.add(key)
            log.log(level, text, *args)

    def _name(self, item: int, mods: list[Mod] | None = None) -> str:
        title = self.updates.titles.get(item) or (mods[0].title if mods else "")
        return f"{title} (Workshop {item})" if title else f"Workshop {item}"

    # 1. Listing the program's own copies -----------------------------------------------------------------------

    def list_unlisted(self, server: ServerConfig, found: Found) -> list[int]:
        """The program's copies in the server's content folder that its [workshop] items don't list: the server loads
        them all the same, so they're listed and kept up to date. In a thread."""
        unlisted = sorted(item for item in found.managed if item not in server.workshop_items)
        if not unlisted:
            return []
        items = [*server.workshop_items, *unlisted]
        try:
            set_table_key(server.config_path, "workshop", "items", items, before="master")
        except (OSError, ValueError) as error:
            self._once(("unlisted", server.name, tuple(unlisted)), logging.WARNING,
                       "[%s] couldn't list Workshop %s in %s (%s)", server.name, ", ".join(map(str, unlisted)),
                       server.config_path.name, error)
            return []
        self._set_items(server, items)
        self.updates.write_check_minutes()  # the server looks for new versions as often as Huragok
        for item in unlisted:
            log.info("[%s] %s is in its content folder (Mods/Workshop/%d) but wasn't in [workshop] items: the server "
                     "loads it all the same, so it's listed now and kept up to date. To stop using it: G -> the server "
                     "-> Workshop and mods -> R", server.name, self._name(item, [found.managed[item]]), item)
        return unlisted

    def _set_items(self, server: ServerConfig, items: list[int]) -> None:
        for each in self.settings.servers:  # the discovery loop reads it again within seconds; until then, this
            if each.config_path == server.config_path:
                each.workshop_items = list(items)

    # 2. Fetching what's been added ---------------------------------------------------------------------------

    async def fetch_missing(self, server: ServerConfig) -> list[int]:
        """The listed items with no copy in the content folder, fetched now: returns those fetched."""
        assert server.content_dir is not None
        workshop = server.content_dir / "Mods" / "Workshop"
        missing = [item for item in server.workshop_items if not (workshop / str(item)).exists()
                   and item not in self.adopting.get(server.name, {})]
        if not missing or server.name in self.fetching or (server.content_dir / "workshop-install.lock").exists():
            return []  # nothing to fetch, or a fetch (this one's, the program's, the window's) is under way
        missing = [item for item in missing if self._may_try(server, item)]
        phase = self.lifecycle.phase(server.name)
        if not missing or phase is None:
            return []
        if phase in UP and self._auto_reload(server):
            self._once(("itself", server.name, tuple(missing)), logging.INFO,
                       "[%s] %s added: the running server fetches it itself (auto_reload)", server.name,
                       ", ".join(self._name(i) for i in missing))
            return []
        if phase not in UP and phase not in STOPPED:
            return []  # starting or stopping: its start fetches it
        if blocked := steam.cannot_fetch(server.config_path):
            self._once(("blocked", server.name), logging.WARNING, "[%s] %s can't be fetched here: %s. Press I to set "
                       "up SteamCMD", server.name, ", ".join(self._name(i) for i in missing), blocked)
            return []
        for item in missing:
            self._count_try(server, item)
        self.fetching.add(server.name)
        try:
            if phase in STOPPED:
                fetched = await self._fetch_here(server, missing)
            else:  # running with auto_reload off: fetched beside it and put in place for its next start
                fetched = await self._fetch_beside(server, missing)
        finally:
            self.fetching.discard(server.name)
        if fetched:
            await asyncio.to_thread(self.layouts_for, server)
        return fetched

    def _may_try(self, server: ServerConfig, item: int) -> bool:
        last, tries = self.tries.get((server.name, item), (0.0, 0))
        if tries >= MAX_TRIES:
            self._once(("gave up", server.name, item), logging.ERROR, "[%s] %s couldn't be fetched in %d tries; it's "
                       "fetched as the server starts, or G -> the server -> Workshop and mods -> I", server.name,
                       self._name(item), MAX_TRIES)
            return False
        return not tries or time.monotonic() - last >= FETCH_AGAIN

    def _count_try(self, server: ServerConfig, item: int) -> None:
        _, tries = self.tries.get((server.name, item), (0.0, 0))
        self.tries[(server.name, item)] = (time.monotonic(), tries + 1)

    def _auto_reload(self, server: ServerConfig) -> bool:
        try:
            return read_toml(server.config_path).get("auto_reload") is not False  # on by default
        except (OSError, ValueError):
            return True

    def _fetch_lock_now(self) -> asyncio.Lock:
        if self._fetch_lock is None:
            self._fetch_lock = asyncio.Lock()
        return self._fetch_lock

    async def _fetch_here(self, server: ServerConfig, items: list[int]) -> list[int]:
        """`dedicated mods` on the stopped server itself."""
        assert server.content_dir is not None
        names = ", ".join(self._name(i) for i in items)
        log.info("[%s] fetching %s now (just added), so it's ready as the server starts", server.name, names)
        async with self._fetch_lock_now():
            try:
                tool = await asyncio.to_thread(self.tool)
                result = await asyncio.to_thread(tool.mods, server.folder)
            except Exception as error:  # noqa: BLE001 - DedicatedError: tried again later
                log.warning("[%s] couldn't fetch %s: %s", server.name, names, error)
                return []
        fetched = [i for i in items if installed_version(server.content_dir, i) is not None]
        self._said_fetched(server, items, fetched, result.problem if not result.ok else "no new copy")
        return fetched

    async def _fetch_beside(self, server: ServerConfig, items: list[int]) -> list[int]:
        """Fetched in a staging folder while the server runs, then moved into its content folder: a new folder, which
        the server hasn't open, read at its next start."""
        assert server.content_dir is not None
        staged, problem = await self._stage(server, items)
        fetched = []
        for item, (folder, _) in staged.items():
            live = server.content_dir / "Mods" / "Workshop" / str(item)
            try:
                if not live.exists():
                    live.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(folder, live)
                fetched.append(item)
            except OSError as error:
                problem = f"couldn't move it into {live.parent} ({error})"
        self._clear(staged)
        self._said_fetched(server, items, fetched, problem, later=True)
        return fetched

    def _said_fetched(self, server: ServerConfig, items: list[int], fetched: list[int], problem: str, *,
                      later: bool = False) -> None:
        for item in fetched:
            self.tries.pop((server.name, item), None)
            version = installed_version(server.content_dir, item)
            log.info("[%s] %s fetched (version of %s)%s", server.name, self._name(item),
                     date_text(version["updated"] if version else None),
                     ": the server uses it from its next start" if later else "")
        for item in items:
            if item not in fetched:
                tries = self.tries.get((server.name, item), (0.0, 0))[1]
                log.warning("[%s] %s couldn't be fetched: %s (try %d of %d%s)", server.name, self._name(item),
                            problem, tries, MAX_TRIES, f"; again in {FETCH_AGAIN / 60:.0f} minutes"
                            if tries < MAX_TRIES else "")

    # Staging: a fetch beside a running server -------------------------------------------------------------------

    def staging_root(self, content_root: Path) -> Path:
        return content_root.parent / STAGING

    def clear_staging(self) -> None:
        """Staging folders left from Huragok's last run (what they held isn't known now: fetched again)."""
        for root in {s.content_dir.parent / STAGING for s in self.settings.servers if s.content_dir is not None}:
            if root.is_dir():
                shutil.rmtree(root, ignore_errors=True)

    async def _stage(self, server: ServerConfig, items: list[int]) -> tuple[dict[int, tuple[Path, Path]], str]:
        """Items fetched by `dedicated mods` on a dedicated.toml of their own beside the content folder, with the
        server's game and SteamCMD. ({item: (its fetched folder, the staging folder)}, why the others weren't)."""
        assert server.content_dir is not None
        paths = read_paths(server.folder, self.settings.config_name)
        try:
            workshop = read_toml(server.config_path).get("workshop") or {}
        except (OSError, ValueError) as error:
            return {}, f"its {server.config_path.name} can't be read ({error})"
        if paths is None or not isinstance(workshop, dict):
            return {}, f"its {server.config_path.name} can't be read"
        if workshop.get("provisioner"):
            return {}, "its items come from a SteamCMD service ([workshop] provisioner), which only fetches for it"
        root = self.staging_root(server.content_dir) / f"{server.slug}-{time.strftime('%Y%m%d-%H%M%S')}"
        lines = [f"game = {toml_text(str(paths.game))}", 'content = "content"', 'data = "data"', "", "[workshop]",
                 f"items = [{', '.join(str(i) for i in items)}]"]
        steamcmd, username = steam.workshop_settings(server.config_path)
        if steamcmd:
            executable = Path(steamcmd) if Path(steamcmd).is_absolute() else (server.folder / steamcmd).resolve()
            lines.append(f"steamcmd = {toml_text(str(executable))}")
        if username:
            lines.append(f"username = {toml_text(username)}")
        lines += ["", "[[server]]", 'name = "Huragok staging"', f"port = {server.game_port}"]
        stop = threading.Event()
        async with self._fetch_lock_now():
            try:
                root.mkdir(parents=True, exist_ok=True)
                (root / self.settings.config_name).write_text("\n".join(lines) + "\n", encoding="utf-8")
                tool = await asyncio.to_thread(self.tool)
                fetch = asyncio.ensure_future(asyncio.to_thread(tool.mods, root, stop=stop))
                try:
                    result = await asyncio.shield(fetch)
                except asyncio.CancelledError:
                    stop.set()  # Huragok is closing: the fetch ends, its folder goes
                    await asyncio.wait({fetch}, timeout=15)
                    shutil.rmtree(root, ignore_errors=True)
                    raise
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - OSError, DedicatedError
                shutil.rmtree(root, ignore_errors=True)
                return {}, str(error)
        staged = {item: (root / "content" / "Mods" / "Workshop" / str(item), root) for item in items
                  if (root / "content" / "Mods" / "Workshop" / str(item) / RECEIPT).is_file()}
        if not staged:
            shutil.rmtree(root, ignore_errors=True)
        return staged, result.problem if not result.ok else "no copy came back"

    def _clear(self, staged: dict[int, tuple[Path, Path]]) -> None:
        for root in {root for _, root in staged.values()}:
            shutil.rmtree(root, ignore_errors=True)

    # 3. Taking over hand copies -------------------------------------------------------------------------------

    async def _answer(self, item: int) -> steam.SteamItem | None | str:
        """Steam's public details of the item, asked at most every STEAM_AGAIN; None: Steam doesn't list it; a
        text: Steam couldn't be asked."""
        asked, answer = self.answers.get(item, (-STEAM_AGAIN, None))
        if time.monotonic() - asked < STEAM_AGAIN:
            return answer
        try:
            found = await asyncio.to_thread(self.updates.fetch, [item])
        except UpdateError as error:
            return str(error)
        answer = found.get(item)
        self.answers[item] = (time.monotonic(), answer)
        if answer is not None and answer.title:
            self.updates.titles[item] = answer.title
        return answer

    def _left_key(self, server: ServerConfig, item: int, mods: list[Mod]) -> str:
        assert server.content_dir is not None
        stamps = []
        for mod in mods:
            try:
                stamps.append(f"{mod.folder.name}:{(mod.folder / 'ModInfo.json').stat().st_mtime_ns}")
            except OSError:
                stamps.append(mod.folder.name)
        return f"{server.content_dir}|{item}|{'|'.join(sorted(stamps))}"

    async def adopt(self, server: ServerConfig, found: Found) -> list[int]:
        """Each item with hand copies: the Workshop's copy fetched beside the server, then swapped in while it's
        stopped (a job, with a restart after the game). Returns the items now waiting for that."""
        assert server.content_dir is not None
        if not found.hand:
            return []
        if shared := self.updates.shared_with(server):
            for item, mods in found.hand.items():
                self._once(("shared", server.name, item), logging.INFO,
                           "[%s] %s is a hand copy (%s) in a content folder %s use too: it's left as it is, as they'd "
                           "all have to stop for it. To have it kept up to date: add %d to their [workshop] items and "
                           "move the hand copy out while they're stopped", server.name, self._name(item, mods),
                           ", ".join(self._relative(server, m.folder) for m in mods), ", ".join(shared), item)
            return []
        waiting = self.adopting.setdefault(server.name, {})
        left = self.read_record().get("left", {})
        new: list[int] = []
        for item, mods in sorted(found.hand.items()):
            if item in waiting or server.name in self.fetching:
                continue
            key = self._left_key(server, item, mods)
            if key in left:
                continue  # looked at before and left as it is; a change to its ModInfo.json looks again
            answer = await self._answer(item)
            if isinstance(answer, str):
                self._once(("steam", server.name, item), logging.INFO, "[%s] %s: Steam couldn't be asked about it "
                           "(%s); asked again later", server.name, self._name(item, mods), answer)
                continue
            if answer is None or (answer.app is not None and answer.app != steam.MCC_APP):
                why = ("Steam doesn't list Workshop item %d publicly (hidden, private or removed)" % item
                       if answer is None else "Workshop item %d isn't an MCC mod" % item)
                self._once(("unlisted item", server.name, item), logging.INFO, "[%s] %s is left a hand-installed mod "
                           "(%s): %s", server.name, mods[0].title, ", ".join(self._relative(server, m.folder)
                                                                             for m in mods), why)
                continue
            if item in found.managed or installed_version(server.content_dir, item) is not None:
                # The program's own copy is there (or was fetched since): only the hand copies go.
                waiting[item] = HandCopy(item, answer.title or mods[0].title, [m.folder for m in mods])
                new.append(item)
                continue
            if not self._may_try(server, item):
                continue
            if blocked := steam.cannot_fetch(server.config_path):
                self._once(("blocked", server.name), logging.WARNING, "[%s] %s can't be fetched here to take it "
                           "over: %s. Press I to set up SteamCMD", server.name, self._name(item, mods), blocked)
                continue
            self._count_try(server, item)
            log.info("[%s] %s is a hand copy (%s) that nothing keeps up to date: fetching the Workshop's own copy "
                     "while the server runs, to take its place", server.name, self._name(item, mods),
                     ", ".join(self._relative(server, m.folder) for m in mods))
            self.fetching.add(server.name)
            try:
                staged, problem = await self._stage(server, [item])
            finally:
                self.fetching.discard(server.name)
            if item not in staged:
                tries = self.tries[(server.name, item)][1]
                log.warning("[%s] couldn't fetch %s to take it over: %s (try %d of %d%s)", server.name,
                            self._name(item, mods), problem, tries, MAX_TRIES,
                            f"; again in {FETCH_AGAIN / 60:.0f} minutes" if tries < MAX_TRIES else "")
                continue
            folder, root = staged[item]
            if not same_mod([m.folder for m in mods], folder):
                self._clear(staged)
                why = (f"its maps ({', '.join(sorted(set().union(*(map_files(m.folder) for m in mods))))}) aren't the "
                       f"item's ({', '.join(sorted(map_files(folder))) or 'none'}): a mod of its own made from it")
                self._leave(key, why)
                log.warning("[%s] %s is left as it is: %s names Workshop item %d, but %s", server.name,
                            mods[0].title, ", ".join(self._relative(server, m.folder) for m in mods), item, why)
                continue
            self.tries.pop((server.name, item), None)
            version = installed_version(root / "content", item)
            waiting[item] = HandCopy(item, answer.title or mods[0].title, [m.folder for m in mods], folder, root,
                                     version["updated"] if version else None)
            new.append(item)
        if new:
            await self._schedule(server)
        return new

    def _relative(self, server: ServerConfig, folder: Path) -> str:
        try:
            return folder.relative_to(server.content_dir).as_posix() if server.content_dir else str(folder)
        except ValueError:
            return str(folder)

    def _leave(self, key: str, why: str) -> None:
        with self.lock:
            data = self.read_record()
            data.setdefault("left", {})[key] = why
            self.write_record(data)

    async def _schedule(self, server: ServerConfig) -> None:
        """The job that swaps the copies, and its restart: after the game (at once with nobody on), or when the server
        empties ([workshop_updates] install = "when_empty"). A stopped server's are swapped now."""
        waiting = self.adopting.get(server.name, {})
        if not waiting:
            return
        names = ", ".join(w.title or f"Workshop {w.item}" for w in waiting.values())
        phase = self.lifecycle.phase(server.name)
        if phase in STOPPED:
            await asyncio.to_thread(self.swap, server)
            return
        when = "when_empty" if self.settings.of(server).workshop_install == "when_empty" else "after_game"
        self.lifecycle.add_job(server.name, StoppedJob(JOB, "putting the Workshop's copies of its hand-copied mods "
                                                            "in place", lambda: asyncio.to_thread(self.swap, server)))
        self.lifecycle.set_flag(server.name, JOB, f"taking over {names}: " + (
            "as it next starts" if phase != "running" else "once nobody's on" if when == "when_empty"
            else "after this game"))
        if phase != "running":
            return  # it swaps them as it next starts
        tell = callout("mod update", f"This server restarts after this game to bring {names} up to date.")
        background(self._plan(server.name, when, f"putting the Workshop's own copy of {names} in place of a hand "
                                                 "copy, to keep it up to date", tell),
                   f"[{server.name}] planning the restart that takes over its hand-copied mods", log)

    async def _plan(self, name: str, when: str, reason: str, tell: str) -> None:
        try:
            await self.lifecycle.restart(name, when=when, reason=reason, by=self.settings.name, tell=tell)
        except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError: done at its next start
            log.info("[%s] couldn't plan a restart to take over its hand-copied mods (%s); done at its next start",
                     name, error)

    def swap(self, server: ServerConfig) -> str:
        """The job, with the server stopped: each item's hand copies to `content set aside`, the fetched copy in their
        place, the item listed. One that can't be moved is put back as it was. In a thread."""
        assert server.content_dir is not None
        waiting = self.adopting.pop(server.name, {})
        self.lifecycle.set_flag(server.name, JOB, None)
        done, failed = [], []
        items = list(server.workshop_items)
        for item, hand in waiting.items():
            live = server.content_dir / "Mods" / "Workshop" / str(item)
            pairs, kept = self._set_aside(server, [f for f in hand.folders if f.exists()])
            if kept:
                self._put_back(server, pairs)
                self._count_try(server, item)  # fetched again after FETCH_AGAIN, MAX_TRIES times at most
                failed.append(f"{hand.title} ({'; '.join(kept)})")
                continue
            if hand.staged is not None and not (live / RECEIPT).is_file():
                try:
                    live.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(hand.staged, live)
                except OSError as error:
                    self._put_back(server, pairs)
                    self._count_try(server, item)
                    failed.append(f"{hand.title} (couldn't put the fetched copy in place: {error})")
                    continue
            moved = [target for _, target in pairs]
            if item not in items:
                items.append(item)
            version = installed_version(server.content_dir, item)
            done.append((item, hand, moved, version["updated"] if version else hand.version))
        for hand in waiting.values():
            if hand.staging is not None:
                shutil.rmtree(hand.staging, ignore_errors=True)
        if done and items != server.workshop_items:
            try:
                set_table_key(server.config_path, "workshop", "items", items, before="master")
                self._set_items(server, items)
                self.updates.write_check_minutes()
            except (OSError, ValueError) as error:  # the copy is in place: the next look lists it
                log.warning("[%s] couldn't list %s in %s (%s); listed at the next look", server.name,
                            ", ".join(str(i) for i, *_ in done), server.config_path.name, error)
        for item, hand, moved, version in done:
            log.info("[%s] %s (Workshop %d) is kept up to date now: the Workshop's own copy (version of %s) is in "
                     "Mods/Workshop/%d and listed in [workshop] items; the hand copy is kept in %s", server.name,
                     hand.title, item, date_text(version), item, ", ".join(str(p) for p in moved) or "(none left)")
        if done:
            self._remember(server, done)
            self.layouts_for(server)
            self.bus.emit("server.workshop_adopted", server.name, items=[item for item, *_ in done])
        for text in failed:
            log.warning("[%s] couldn't take over %s: it's left as it was, tried again in %d minutes", server.name,
                        text, FETCH_AGAIN / 60)
        if not done:
            return "no hand-copied Workshop mod taken over" + (f": {'; '.join(failed)}" if failed else "")
        return "kept up to date from now on: " + ", ".join(f"{h.title} ({date_text(v)})" for _, h, _, v in done)

    def _set_aside(self, server: ServerConfig, folders: list[Path]) -> tuple[list[tuple[Path, Path]], list[str]]:
        """Each folder to `content set aside`, one at a time: ([(folder, where it went)], those that couldn't go)."""
        assert server.content_dir is not None
        pairs: list[tuple[Path, Path]] = []
        kept: list[str] = []
        for folder in folders:
            moved, left = set_aside(server.content_dir, [folder])
            pairs += [(folder, target) for target in moved]
            kept += left
        return pairs, kept

    def _put_back(self, server: ServerConfig, pairs: list[tuple[Path, Path]]) -> None:
        for folder, target in pairs:
            try:
                os.replace(target, folder)
            except OSError as error:
                log.error("[%s] couldn't put %s back (%s): it is in %s", server.name, folder, error, target)

    def _remember(self, server: ServerConfig, done: list[tuple[int, HandCopy, list[Path], int | None]]) -> None:
        with self.lock:
            data = self.read_record()
            adopted = data.setdefault("adopted", [])
            for item, hand, moved, version in done:
                adopted.append({"server": server.name, "item": item, "title": hand.title, "version": version,
                                "hand copies kept in": [str(p) for p in moved], "when": time.strftime("%Y-%m-%d %H:%M")})
            data["adopted"] = adopted[-ADOPTED_KEPT:]
            self.write_record(data)

    # Layouts ---------------------------------------------------------------------------------------------------

    def sync_layouts(self, roots: list[tuple[Path, list[str]]] | None = None) -> list[tuple[str, int]]:
        """Every content folder's mods' Forge layouts in content/Maps, as their versions have them, logged; the lines
        are returned too. In a thread."""
        if roots is None:
            seen: dict[Path, list[str]] = {}
            for server in self.settings.servers:
                if server.content_dir is not None:
                    seen.setdefault(server.content_dir, []).append(server.name)
            roots = list(seen.items())
        lines: list[tuple[str, int]] = []
        with self.lock:
            data = self.read_record()
            written_all = data.setdefault("layouts", {})
            before = json.dumps(written_all, sort_keys=True)
            for root, names in roots:
                lines += self._sync_root(root, ", ".join(names), written_all.setdefault(str(root), {}))
            for root in [r for r, files in written_all.items() if not files]:
                del written_all[root]
            if json.dumps(written_all, sort_keys=True) != before:
                self.write_record(data)
        for line, level in lines:
            log.log(level, "%s", line)
        return lines

    def layouts_for(self, server: ServerConfig) -> list[tuple[str, int]]:
        """One server's content folder's layouts, after a new version is in and before it starts. In a thread."""
        if not self.config.layouts or server.content_dir is None:
            return []
        return self.sync_layouts([(server.content_dir, [server.name])])

    def _sync_root(self, root: Path, label: str, written: dict[str, str]) -> list[tuple[str, int]]:
        found = look_through(root)
        # The program's own copy first: a hand copy of the same mod (on its way out) writes nothing over it.
        mods = sorted((m for m in found.mods if m.layouts), key=lambda m: (not is_managed(m, root), str(m.folder)))
        lines: list[tuple[str, int]] = []
        handled: set[str] = set()
        for mod in mods:
            result = sync_layouts(mod, root, written, skip=handled)
            parts = [f"{len(result.copied)} copied" if result.copied else "",
                     f"{len(result.updated)} brought up to date" if result.updated else "",
                     f"{len(result.replaced)} replaced (the old one kept in content set aside)" if result.replaced
                     else ""]
            if result.changed:
                folder = Path(result.copied[0] if result.copied else result.updated[0] if result.updated
                              else result.replaced[0]).parent.as_posix()
                lines.append((f"[{label}] Forge layouts from {mod.title}: {', '.join(p for p in parts if p)} in "
                              f"{folder}", logging.INFO))
            for key in result.kept:
                if ("kept", str(root), key) not in self.said:
                    self.said.add(("kept", str(root), key))
                    lines.append((f"[{label}] {key} was changed here, so {mod.title}'s own version isn't copied over "
                                  "it (delete it to get the mod's)", logging.INFO))
        return lines


def switch_workshop_copies(server: ServerConfig, items: list[int], *, off: bool,
                           config_name: str = "dedicated.toml") -> list[str]:
    """Removed items' copies switched off in disabled-mods.json (R): the server loads a folder in content/Mods/Workshop
    whether it's listed or not, and Huragok lists one it finds there again; an item added again (A) switched back on.
    What was done, as lines for the host."""
    from .mccimport import content_root_of

    root = server.content_dir or content_root_of(server.folder, config_name)
    try:
        packages = disabled_mods(root)
    except DisabledModsError as error:
        return [f"{error}: switch the removed items' copies off by hand"] if off else []
    lines = []
    for item in items:
        package = f"Mods/Workshop/{item}"
        if not (root / package).is_dir() or is_disabled(packages, package) == off:
            continue
        try:
            packages = set_mod_disabled(root, package, off)
        except (OSError, DisabledModsError) as error:
            lines.append(f"Couldn't switch {package} {'off' if off else 'on'} in {DISABLED_MODS} ({error})")
            continue
        lines.append(f"{package} switched {'off' if off else 'back on'} in {DISABLED_MODS}"
                     + (": its files stay, but the server stops loading them, and Huragok doesn't list it again"
                        if off else ""))
    return lines


def layouts_by_hand(settings: Settings, root: Path, mod: Mod) -> list[str]:
    """The window's L, with or without Huragok running: the mod's layouts into content/Maps, remembered as Huragok's
    (so its next versions are copied over them)."""
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


__all__ = ["HandCopy", "WorkshopKeeper", "layouts_by_hand", "look_through", "same_mod", "switch_workshop_copies"]
