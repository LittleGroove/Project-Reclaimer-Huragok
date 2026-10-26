"""Workshop mods kept up to date: a new version is swapped in when a game ends.

The server program updates the Workshop items it lists by itself, but only once nobody
is on a server that uses the mod. A busy server may never get there, and players who
already have the new version can't join it ("only updating the server helps"). With
[workshop_updates] install = "after_game" the Superintendent does it at the end of a
game instead:

1. Notice. Every check_minutes it asks Steam's public Workshop details (no sign-in)
   for each item the servers list, and compares them with the version each server
   installed (content/Mods/Workshop/<item>/reclaimer-workshop-version.json). The
   server's console saying "Workshop <id>: update downloaded; it installs once …
   no players" counts too.
2. Plan a restart after the game (players are told why), with a job that runs while
   the server is stopped.
3. The job moves the installed copy aside and runs `dedicated mods`, which fetches the
   newest version. Tested on 2026-10-18: 38 s for a 2 GB map whose old version
   SteamCMD still had, while `dedicated mods` over the old copy changes nothing. Then
   it checks the new receipt (it must be newer than the old one: a fetch that brought
   back the same version didn't update anything) and the server starts. If the fetch
   fails (an expired SteamCMD sign-in, Steam down), the old copy goes back and the
   server starts on it. The job never runs again before that start: the item stays
   waiting and is tried after a later game (the next look at Steam plans it), up to
   MAX_TRIES times for one version; then Huragok stops trying until a newer version
   comes out or a person asks (U -> Workshop mods). That also keeps SteamCMD's
   sign-ins well under Steam's limits.

A job cut short (Huragok closed or crashed mid-fetch) must never leave a server without
its mod: before anything is moved a marker lists what goes where, the old copies go
back on a cancel, and at Huragok's next start (and before the next job) any marker
left over puts them back. Clean up never offers an old copy whose item is missing.

check_minutes is also written into each server's [workshop] update_check_minutes, so
the server itself downloads the new version during the game and the swap is quick.
A content folder several servers share is left to the program's own way (it swaps the
item for every server using it at once), as is install = "when_empty".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import steam
from ..core.background import background
from ..core.events import Event
from ..core.jobs import StoppedJob
from ..core.web import UpdateError
from ..steam import SteamItem, workshop_details

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings
    from ..program.dedicated import Dedicated
    from ..core.events import EventBus
    from ..servers.fleet import Lifecycle

log = logging.getLogger(__name__)

RECEIPT = "reclaimer-workshop-version.json"
ASIDE = ".huragok-workshop-previous"  # next to the content folder: same drive, so moving a 2 GB map is instant
JOB = "workshop"
FLAG = "workshop"
SIGN_IN_FLAG = "steamcmd"
SIGN_IN_FAILED = re.compile(r"signing in as \S+ failed|sign in to steamcmd again|no key for mcc", re.IGNORECASE)
QUIET_ERRORS = 3600.0  # a failing look at Steam is logged again after this long
MAX_TRIES = 3  # fetches of one version that didn't install before Huragok stops trying it
MARKER = ".moving.json"  # in the aside folder while a job has copies moved aside
ASIDE_NAME = re.compile(r"-(?P<item>\d+)-\d{8}-\d{6}(?P<failed>-failed)?$")


@dataclass
class Waiting:
    """A newer version of one item than the server has installed."""

    item: int
    title: str
    installed: int | None  # the installed copy's version (Unix seconds)
    newest: int | None  # Steam's (None: only the server's console said so)
    downloaded: bool = False  # the server said it has it already
    since: float = 0.0

    @property
    def label(self) -> str:
        name = self.title or f"Workshop {self.item}"
        return f"{name} ({date_text(self.newest)})" if self.newest else name


def date_text(stamp: int | None) -> str:
    return time.strftime("%d %b %Y", time.gmtime(stamp)) if stamp else "?"  # UTC, as the program says it


def installed_version(content_dir: Path | None, item: int) -> dict[str, Any] | None:
    """The receipt the server program writes as it installs an item: {item, manifest, updated}. None: not installed."""
    if content_dir is None:
        return None
    try:
        data = json.loads((content_dir / "Mods" / "Workshop" / str(item) / RECEIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("updated"), int):
        return None
    return data


def is_newer(installed: dict[str, Any], steam: SteamItem) -> bool:
    if steam.updated is None:
        return False
    if steam.manifest and str(installed.get("manifest", "")) == steam.manifest:
        return False  # the same files, whatever the dates say
    return steam.updated > int(installed["updated"])


def got_newer(before: int | None, new: dict[str, Any] | None, waiting: Waiting) -> bool:
    """A fetch updated the item: its new receipt is newer than the old one (or as new as Steam's)."""
    if new is None:
        return False
    if before is None:
        return True
    updated = int(new["updated"])
    return updated > before or (waiting.newest is not None and updated >= waiting.newest)


def aside_folders(content_dir: Path) -> Path:
    return content_dir.parent / ASIDE


def kept_by_markers(aside_root: Path) -> set[Path]:
    """The old copies an unfinished job's marker names: never offered for deletion."""
    kept: set[Path] = set()
    for marker in aside_root.glob(f"*{MARKER}"):
        try:
            moves = json.loads(marker.read_text(encoding="utf-8")).get("moves", [])
            kept.update(Path(aside) for _, aside in moves)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return kept


class WorkshopUpdateService:
    def __init__(self, settings: Settings, bus: EventBus, lifecycle: Lifecycle,
                 tool: Callable[[], Dedicated] | None = None,
                 fetch: Callable[[list[int]], dict[int, SteamItem]] = workshop_details) -> None:
        self.settings = settings
        self.config = settings.workshop_updates
        self.bus = bus
        self.lifecycle = lifecycle
        self.tool = tool
        self.fetch = fetch
        self.waiting: dict[str, dict[int, Waiting]] = {}  # server -> item -> what's waiting
        self.titles: dict[int, str] = {}
        self.steam: dict[int, SteamItem] = {}  # the last answer
        self.asked: dict[tuple[str, int], int | str] = {}  # (server, item) -> the version a restart was planned for
        self.done: dict[tuple[str, int], int | None] = {}  # (server, item) -> Steam's version when it was swapped in
        self.failures: dict[tuple[str, int], tuple[int | str, int]] = {}  # (server, item) -> (version, fetches failed)
        self.installing: set[str] = set()  # servers whose job is running right now
        self.last_check: float | None = None
        self.last_error = ""
        self._error_logged = 0.0
        self._sign_in_logged: dict[str, float] = {}
        self._blocked_logged: dict[str, float] = {}  # server -> when "can't be fetched here" was last logged
        bus.subscribe("server.workshop", self._on_console)
        self.recover()  # a job cut short last time: its old copies back before any server starts

    # Settings ----------------------------------------------------------------------------------

    def install_mode(self, server: ServerConfig) -> str:
        return self.settings.of(server).workshop_install  # after_game on a shared folder: the program's, when empty

    def shared_with(self, server: ServerConfig) -> list[str]:
        return self.settings.of(server).shares_content_with()

    def write_check_minutes(self) -> list[str]:
        """Each server with Workshop items looks for new versions every check_minutes ([workshop] in its file)."""
        from ..core.tomledit import set_table_key

        minutes = self.config.check_minutes
        written = []
        if not minutes:
            return written
        for server in self.settings.servers:
            if not server.workshop_items or self.settings.of(server).workshop_updates == "off" or \
                    server.workshop_check_minutes == minutes:
                continue
            try:
                set_table_key(server.config_path, "workshop", "update_check_minutes", minutes, before="master")
            except OSError as error:
                log.warning("[%s] couldn't write update_check_minutes into %s: %s", server.name,
                            server.config_path.name, error)
                continue
            for other in self.settings.servers:
                if other.config_path == server.config_path:
                    other.workshop_check_minutes = minutes
            written.append(server.name)
            log.info("[%s] the server looks for new Workshop versions every %d min from its next restart "
                     "([workshop] update_check_minutes in %s)", server.name, minutes, server.config_path.name)
        return written

    # Noticing ----------------------------------------------------------------------------------

    async def run(self) -> None:
        minutes = self.config.check_minutes
        if not minutes:
            log.info("Not looking for new Workshop versions ([workshop_updates] check_minutes = 0); the servers "
                     "still say when they download one")
            return
        await asyncio.sleep(30)  # after the servers have started and their consoles answer
        self.write_check_minutes()
        while True:
            await self.check()
            await asyncio.sleep(minutes * 60)

    async def check(self, *, quiet: bool = True) -> dict[str, list[Waiting]]:
        """Ask Steam about every item the servers list; plan what's newer. Returns what's waiting, by server."""
        servers = [s for s in self.settings.servers if s.workshop_items and self.settings.of(s).workshop_updates != "off"]
        items = sorted({i for s in servers for i in s.workshop_items})
        if not items:
            return {}
        try:
            details = await asyncio.to_thread(self.fetch, items)
        except UpdateError as error:
            self.last_error = str(error)
            if not quiet or time.monotonic() - self._error_logged > QUIET_ERRORS:
                self._error_logged = time.monotonic()
                log.log(logging.WARNING if not quiet else logging.INFO,
                        "Couldn't look for new Workshop versions: %s", error)
            return self.waiting_lists()
        self.last_check, self.last_error = time.time(), ""
        self.steam.update(details)
        for item, found in details.items():
            if found.title:
                self.titles[item] = found.title
        for server in servers:
            for item in server.workshop_items:
                installed = installed_version(server.content_dir, item)
                steam = details.get(item)
                if installed is None or steam is None:
                    continue  # not installed yet (the server fetches it as it starts), or Steam doesn't list it
                if is_newer(installed, steam) and self.done.get((server.name, item), -1) != steam.updated:
                    self._found(server, item, installed["updated"], steam.updated)
                else:
                    self._settled(server, item)
        if not quiet:
            waiting = sum(len(w) for w in self.waiting.values())
            log.info("Workshop: %d item(s) looked at, %s", len(items),
                     f"{waiting} new version(s) waiting" if waiting else "every server has the newest")
        return self.waiting_lists()

    def _on_console(self, event: Event) -> None:
        """What the server says about its items: a downloaded update, one it installed itself, a failed sign-in."""
        server = self._server(event.server)
        text = str(event.get("text") or "")
        if server is None:
            return
        if SIGN_IN_FAILED.search(text):
            self._sign_in_failed(server, text)
        try:
            item = int(event.get("item"))
        except (TypeError, ValueError):
            return
        lower = text.lower()
        if "update downloaded" in lower:
            installed = installed_version(server.content_dir, item)
            self._found(server, item, installed["updated"] if installed else None, None, downloaded=True)
        elif lower.startswith("updated") or "installing the update" in lower:
            if self._settled(server, item):
                log.info("[%s] the server installed the new version of %s itself (nobody was on)", server.name,
                         self.titles.get(item) or f"Workshop {item}")
        elif lower.startswith(("ready", "installed")):
            self.lifecycle.set_flag(server.name, SIGN_IN_FLAG, None)

    def _server(self, name: str | None) -> ServerConfig | None:
        return next((s for s in self.settings.servers if s.name == name), None)

    def cannot_fetch(self, server: ServerConfig) -> str | None:
        """Why the server's SteamCMD can't fetch the new version here (steam.cannot_fetch), logged once an hour."""
        blocked = steam.cannot_fetch(server.config_path)
        if blocked and time.monotonic() - self._blocked_logged.get(server.name, -QUIET_ERRORS) > QUIET_ERRORS:
            self._blocked_logged[server.name] = time.monotonic()
            log.warning("[%s] a new version of its Workshop mods waits, but it can't be fetched here: %s. No restart "
                        "until it can: press I to set up SteamCMD", server.name, blocked)
        return blocked

    def _sign_in_failed(self, server: ServerConfig, text: str) -> None:
        self.lifecycle.set_flag(server.name, SIGN_IN_FLAG, "! SteamCMD sign-in failed: press I to sign in again")
        if time.monotonic() - self._sign_in_logged.get(server.name, -QUIET_ERRORS) > QUIET_ERRORS:
            self._sign_in_logged[server.name] = time.monotonic()
            log.error("[%s] SteamCMD can't sign in (%s). Press I in the window to sign in again: until then Workshop "
                      "mods can't be fetched or updated", server.name, text.strip()[:160])

    def _found(self, server: ServerConfig, item: int, installed: int | None, newest: int | None, *,
               downloaded: bool = False) -> None:
        waiting = self.waiting.setdefault(server.name, {})
        known = waiting.get(item)
        if known is None:
            known = waiting[item] = Waiting(item, self.titles.get(item, ""), installed, newest, downloaded, time.time())
            log.warning("[%s] a new version of %s is out (installed: %s)", server.name, known.label,
                        date_text(installed))
            self.bus.emit("server.workshop_update", server.name, item=item, newest=newest, installed=installed)
        else:
            known.newest = newest or known.newest
            known.downloaded = known.downloaded or downloaded
            known.title = known.title or self.titles.get(item, "")
        self._act(server)

    def _settled(self, server: ServerConfig, item: int) -> bool:
        """The server has the newest version now. True: something was waiting."""
        waiting = self.waiting.get(server.name, {})
        was = waiting.pop(item, None)
        self.failures.pop((server.name, item), None)
        if not waiting:
            self.waiting.pop(server.name, None)
            self.lifecycle.drop_job(server.name, JOB)
        self._show(server)
        return was is not None

    # Acting ------------------------------------------------------------------------------------

    def _show(self, server: ServerConfig, how: str = "") -> None:
        waiting = self.waiting.get(server.name)
        if not waiting:
            self.lifecycle.set_flag(server.name, FLAG, None)
            return
        names = ", ".join(w.label for w in waiting.values())
        self.lifecycle.set_flag(server.name, FLAG, f"mod update waiting: {names}{'; ' + how if how else ''}")

    def _act(self, server: ServerConfig) -> None:
        mode = self.install_mode(server)
        waiting = self.waiting.get(server.name, {})
        if mode == "off":
            self._show(server, "Workshop updates are off")
            return
        if mode == "when_empty":
            shared = self.shared_with(server)
            self._show(server, "it installs once nobody's on" + (f" (content shared with {', '.join(shared)})"
                                                                 if shared else ""))
            return
        tryable = [w for w in waiting.values() if not self.gave_up(server.name, w)]
        if not tryable:
            self.lifecycle.drop_job(server.name, JOB)
            self.lifecycle.set_flag(server.name, FLAG, f"! {', '.join(w.label for w in waiting.values())}: the new "
                                                       f"version didn't install ({MAX_TRIES} tries): U -> Workshop "
                                                       "mods to try again")
            return
        if blocked := self.cannot_fetch(server):
            # A restart now would only put the old copy back: no restart and nothing told to players until it can.
            self.lifecycle.drop_job(server.name, JOB)
            self.lifecycle.set_flag(server.name, FLAG, f"! mod update waiting: {', '.join(w.label for w in tryable)}; "
                                                       f"it can't be fetched: {blocked}: press I")
            return
        if server.name not in self.installing:  # never added while it runs: the server must start after it
            self.lifecycle.add_job(server.name, StoppedJob(JOB, "installing new Workshop versions",
                                                           lambda: self.install(server)))
        fresh = [w for w in tryable if self.asked.get((server.name, w.item)) != (w.newest or "console")]
        phase = self.lifecycle.phase(server.name)
        if phase in ("running", "countdown"):
            self._show(server, "installs after this game")
        else:
            self._show(server, "installs as it starts")
        if not fresh or phase != "running":
            return
        for w in fresh:
            self.asked[(server.name, w.item)] = w.newest or "console"
        names = ", ".join(w.title or f"Workshop {w.item}" for w in fresh)
        background(self._plan(server.name, f"installing a new version of {names}"),
                   f"[{server.name}] planning its Workshop update", log)

    async def _plan(self, name: str, reason: str) -> None:
        try:
            await self.lifecycle.restart(name, when="after_game", reason=reason, by=self.settings.name)
        except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError: it installs at its next start
            log.info("[%s] couldn't plan a restart for the Workshop update (%s); it installs at its next start",
                     name, error)

    # Tries --------------------------------------------------------------------------------------

    def gave_up(self, server: str, waiting: Waiting) -> bool:
        """MAX_TRIES fetches of this version didn't install it: left until a newer one, or a person asks."""
        version, tries = self.failures.get((server, waiting.item), (None, 0))
        return version == (waiting.newest or "console") and tries >= MAX_TRIES

    def _count_failure(self, server: str, waiting: Waiting) -> int:
        key = waiting.newest or "console"
        version, tries = self.failures.get((server, waiting.item), (key, 0))
        tries = tries + 1 if version == key else 1
        self.failures[(server, waiting.item)] = (key, tries)
        return tries

    # Installing ------------------------------------------------------------------------------------

    def recover(self, server: ServerConfig | None = None) -> list[str]:
        """Old copies a job moved aside and never put back (Huragok closed or crashed mid-fetch), back in place;
        a fetched copy that has its receipt is kept. Returns what was put back."""
        restored: list[str] = []
        for each in [server] if server is not None else list(self.settings.servers):
            if each.content_dir is None or each.name in self.installing:
                continue
            for marker in sorted(aside_folders(each.content_dir).glob(f"{each.slug}-*{MARKER}")):
                try:
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    moves = [(Path(live), Path(aside)) for live, aside in data.get("moves", [])]
                except (OSError, ValueError, AttributeError, TypeError) as error:
                    log.warning("[%s] couldn't read %s (%s); the old copies it names are left in %s", each.name,
                                marker.name, error, marker.parent)
                    continue
                left = False
                for live, aside in moves:
                    if not aside.is_dir() or (live / RECEIPT).is_file():
                        continue  # put back already, or the fetch got as far as a whole new copy
                    try:
                        if live.exists():
                            os.replace(live, aside.with_name(aside.name + "-failed"))  # a fetch cut short
                        os.replace(aside, live)
                        restored.append(live.name)
                        log.warning("[%s] Workshop %s: its old copy is back in place (a Workshop update was cut short "
                                    "before it finished)", each.name, live.name)
                    except OSError as error:
                        left = True
                        log.error("[%s] couldn't put Workshop %s's old copy back (%s): it is in %s; move it to %s",
                                  each.name, live.name, error, aside, live)
                if not left:
                    marker.unlink(missing_ok=True)
        return restored

    async def install(self, server: ServerConfig) -> str:
        """The job, with the server stopped: each waiting item's old copy aside, `dedicated mods`, check, start.

        It never adds itself again: an item that didn't install stays waiting for a later game."""
        self.recover(server)
        if shared := self.shared_with(server):
            return f"left to the server program: its content folder is shared with {', '.join(shared)}"
        waiting = {item: w for item, w in self.waiting.get(server.name, {}).items() if not self.gave_up(server.name, w)}
        if not waiting or server.content_dir is None:
            return "no Workshop update waiting any more"
        if self.tool is None:
            return "can't install Workshop updates: the server program wasn't found"
        try:
            tool = await asyncio.to_thread(self.tool)  # may ask each program copy its version: not on the loop
        except Exception as error:  # noqa: BLE001 - DedicatedError
            return f"can't install Workshop updates: {error}"
        self.installing.add(server.name)
        try:
            return await self._install(server, waiting, tool)
        finally:
            self.installing.discard(server.name)

    async def _install(self, server: ServerConfig, waiting: dict[int, Waiting], tool: Any) -> str:
        assert server.content_dir is not None
        workshop = server.content_dir / "Mods" / "Workshop"
        lock = server.content_dir / "workshop-install.lock"
        if lock.exists():
            lock.unlink(missing_ok=True)  # stopped, and nothing else uses this content: a crashed install's
            log.info("[%s] removed a leftover %s (a Workshop install that crashed)", server.name, lock.name)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        aside_root = aside_folders(server.content_dir)
        planned = {item: aside_root / f"{server.slug}-{item}-{stamp}" for item in waiting
                   if (workshop / str(item)).is_dir()}
        if not planned:
            return "no installed copy to replace"
        marker = aside_root / f"{server.slug}-{stamp}{MARKER}"
        try:
            aside_root.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"server": server.name, "moves": [
                [str(workshop / str(item)), str(target)] for item, target in planned.items()]}), encoding="utf-8")
        except OSError as error:
            return f"Workshop not updated: couldn't write {marker.name} ({error}); nothing was moved"
        moved: dict[int, tuple[Path, int | None]] = {}
        for item, target in planned.items():
            old = installed_version(server.content_dir, item)
            try:
                os.replace(workshop / str(item), target)
            except OSError as error:
                log.warning("[%s] couldn't move Workshop %s aside (%s); leaving it as it is", server.name, item, error)
                continue
            moved[item] = (target, old["updated"] if old else None)
        if not moved:
            marker.unlink(missing_ok=True)
            return "no installed copy to replace"
        stop = threading.Event()
        fetch = asyncio.ensure_future(asyncio.to_thread(tool.mods, server.folder, stop=stop))
        try:
            result = await asyncio.shield(fetch)
        except asyncio.CancelledError:
            # Huragok is closing (or relaunching): end the fetch, put every old copy back, then go.
            stop.set()
            try:
                await asyncio.wait({fetch}, timeout=15)
            except BaseException:  # noqa: BLE001 - cancelled again: the copies still go back
                pass
            for item, (aside, _) in moved.items():
                self._put_back(server, item, aside, workshop / str(item), remove_failed=False)
            if not any(aside.is_dir() for aside, _ in moved.values()):
                marker.unlink(missing_ok=True)
            raise
        if SIGN_IN_FAILED.search(result.text):
            self._sign_in_failed(server, next((l for l in result.lines if SIGN_IN_FAILED.search(l)), result.problem))
        done, kept, same, stopped = [], [], [], []
        for item, (aside, before) in moved.items():
            new = installed_version(server.content_dir, item)
            w = waiting[item]
            name = w.title or f"Workshop {item}"
            if result.ok and got_newer(before, new, w):
                assert new is not None
                done.append(f"{name} {date_text(before)} -> {date_text(new['updated'])}")
                self.done[(server.name, item)] = w.newest  # swapped once; Steam saying so again won't loop
                self.waiting.get(server.name, {}).pop(item, None)
                self.failures.pop((server.name, item), None)
                await asyncio.to_thread(shutil.rmtree, aside, True)
                continue
            if result.ok and new is not None:
                same.append(f"{name} ({date_text(new['updated'])})")
            if await asyncio.to_thread(self._put_back, server, item, aside, workshop / str(item)):
                kept.append(name)
            if self._count_failure(server.name, w) >= MAX_TRIES:
                stopped.append(name)
            self.asked.pop((server.name, item), None)  # the next look at Steam plans another try after a game
        if not any(aside.is_dir() for aside, _ in moved.values()):
            marker.unlink(missing_ok=True)
        if not self.waiting.get(server.name):
            self.waiting.pop(server.name, None)
        if kept or stopped or same:
            why = result.problem if not result.ok else \
                f"the fetch brought back the version it had: {', '.join(same)}" if same else "no new copy"
            if stopped:
                log.error("[%s] %s didn't install after %d tries (%s); Huragok stops trying that version until a "
                          "newer one comes out. To try again: U -> Workshop mods", server.name, ", ".join(stopped),
                          MAX_TRIES, why)
            else:
                log.warning("[%s] the new Workshop version(s) didn't install (%s); %s kept, tried again after a later "
                            "game (the next look at Steam plans it)", server.name, why, ", ".join(kept) or "nothing")
        if stopped:
            self._act(server)  # its line says it stopped trying
        else:
            self._show(server, "tried again after a later game" if kept else "")
        if done:
            self.lifecycle.set_flag(server.name, SIGN_IN_FLAG, None)
            self.bus.emit("server.workshop_updated", server.name, items=sorted(i for i in moved if i not in
                                                                               self.waiting.get(server.name, {})))
            return "Workshop updated: " + "; ".join(done)
        return "Workshop not updated; the old version(s) kept"

    def _put_back(self, server: ServerConfig, item: int, aside: Path, folder: Path, *,
                  remove_failed: bool = True) -> bool:
        """The old copy back in place, so the server starts on what it had. A partial new copy is moved off first."""
        try:
            if folder.exists():
                failed = aside.with_name(aside.name + "-failed")
                os.replace(folder, failed)
                if remove_failed:
                    shutil.rmtree(failed, ignore_errors=True)
            os.replace(aside, folder)
            return True
        except OSError as error:
            log.error("[%s] couldn't put Workshop %s's old copy back (%s): it is in %s; move it to %s", server.name,
                      item, error, aside, folder)
            return False

    # For the window ----------------------------------------------------------------------------

    def waiting_lists(self) -> dict[str, list[Waiting]]:
        return {name: list(items.values()) for name, items in self.waiting.items() if items}

    def item_lines(self, server: ServerConfig) -> list[str]:
        """One server's items: installed version, Steam's newest, and whether a new one is waiting."""
        lines = []
        for item in server.workshop_items:
            installed = installed_version(server.content_dir, item)
            steam = self.steam.get(item)
            title = self.titles.get(item) or (steam.title if steam else "") or ""
            state = "not installed yet" if installed is None else f"installed {date_text(installed['updated'])}"
            if steam and steam.updated:
                state += f" · Steam {date_text(steam.updated)}"
            waiting = self.waiting.get(server.name, {}).get(item)
            if waiting:
                state += " · NEW VERSION WAITING" + (" (downloaded)" if waiting.downloaded else "")
                if self.gave_up(server.name, waiting):
                    state += f" · didn't install in {MAX_TRIES} tries; installing it here tries again"
            lines.append(f"{item} {title}".rstrip() + f"   {state}")
        return lines

    def installable(self) -> list[str]:
        """The servers with a new version waiting that Huragok installs: not those sharing their content folder
        (the server program swaps a shared item for all of them once nobody's on)."""
        return [name for name in self.waiting_lists()
                if (server := self._server(name)) is not None and not self.shared_with(server)]

    def report(self) -> list[str]:
        """Every server's Workshop items: installed version, Steam's, and what happens next."""
        lines = []
        for server in self.settings.servers:
            if not server.workshop_items:
                continue
            mode = self.install_mode(server)
            lines.append(f"{server.name} · {len(server.workshop_items)} item(s) · "
                         + {"after_game": "new versions installed after the game",
                            "when_empty": "new versions installed by the server once nobody's on",
                            "off": "Workshop updates off"}[mode])
            lines.extend(f"  {line}" for line in self.item_lines(server))
        if self.last_error:
            lines.append(f"Last look at Steam failed: {self.last_error}")
        elif self.last_check:
            lines.append(f"Last look at Steam: {time.strftime('%H:%M', time.localtime(self.last_check))}")
        return lines or ["No server lists Workshop items ([workshop] items in its dedicated.toml; G -> Workshop)."]

    async def install_now(self, name: str, when: str) -> tuple[bool, str]:
        """The window's 'install the waiting versions': after the game, or now (a restart with the usual warning)."""
        server = self._server(name)
        if server is None:
            return False, f"no server called {name}"
        if shared := self.shared_with(server):
            return False, (f"its content folder is shared with {', '.join(shared)}, so the server program installs "
                           "the new version for all of them once nobody's on")
        if not self.waiting.get(name):
            return False, "nothing is waiting: the server has the newest version of every item"
        if blocked := self.cannot_fetch(server):
            return False, f"it can't be fetched: {blocked}: press I to set up SteamCMD for it"
        for item in self.waiting[name]:
            self.failures.pop((name, item), None)  # a person asked: tried again, however often it failed
        self.lifecycle.add_job(name, StoppedJob(JOB, "installing new Workshop versions", lambda: self.install(server)))
        try:
            await self.lifecycle.restart(name, when=when, reason="installing new Workshop versions",
                                         by=self.settings.name)
        except Exception as error:  # noqa: BLE001
            return True, f"it installs at its next start ({error})"
        return True, f"installing {'after this game' if when == 'after_game' else 'now'}"
