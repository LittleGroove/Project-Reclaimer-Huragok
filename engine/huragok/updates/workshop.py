"""Workshop mod updates: the servers do them; Huragok restarts a server whose map in use holds one up, and warns
(0.7.0, plan-0.7.0.md 4.4; simulations S1 and S1b, DEPLOY/sim-handsoff).

A server looks for new versions of the items its dedicated.toml lists ([workshop] items) 6 s after it starts and then
every update_check_minutes, downloads one during a game and installs it between games, by itself. Except over a mod's
map it has open: its game process keeps the map open from the first time it plays it, and the console then says
"installing the update failed (cannot move … aside: Access is denied …); trying again" every 5 s, for good, with
players on or not. A restart of its game worker (RCON `restart`) frees it: the program's supervisor, which keeps the
download, installs it in the second before the new worker opens the map (S1b, 7 of 7). So with [workshop_updates]
restart (on unless set; a server's own workshop_updates = "off" leaves it out) Huragok only restarts:

- What sets it off: that line. It comes between games: at once on an empty server, at the game's end on a busy one,
  exactly when to act. "A new version was downloaded" alone isn't enough: a server whose game doesn't use the mod
  installs it straight after.
- Nobody on: RCON `restart`. People on: a `say` first, about 3 s, then `restart now`; they rejoin as they first joined
  (answer 22). No SteamCMD, nothing moved, no `dedicated mods`, nothing written into the server's files.
- Confirmed by "updated to the version … between games". If the failures carry on, once more, then a warning.
- Servers sharing a content folder (one folder, or folders whose `content` is the same path) can't install while any
  of them has the map open, and only one of them downloads at a time (a lock in the content folder): SharedUpdate.
- A server Huragok didn't start (phase external) is only ever restarted over its console, never stopped, started or
  killed (answer 30). Its console isn't Huragok's to read, so its held-up update is seen in its content folder: the
  program's waiting copy (.workshop-<item>-<pid>-<n>, made by the program's supervisor, process <pid>) still there
  while it's between games.

The installed version is read the way the program reads it: reclaimer-installed.json's "version" (item, manifest,
updated). The program counts a new version only when the manifest differs and the time is newer (S1, answer 17);
reclaimer-workshop-version.json, written beside it, is never read by the program.

Huragok also looks at Steam itself every LOOK_SECONDS (public details, no sign-in), for the window and for a server
whose console isn't read; and says what it finds through the warnings catalogue (templates/warnings.toml, the rules
whose ids start mod-update-).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..core.background import background
from ..core.events import Event
from ..core.text import format_duration
from ..core.web import UpdateError
from ..lore import callout
from ..steam import MCC_APP, SteamItem, workshop_details

if TYPE_CHECKING:
    from ..core.events import EventBus
    from ..core.serverfolders import ServerConfig
    from ..core.serverwarnings import Warnings
    from ..core.settings import Settings
    from ..servers.fleet import Lifecycle

log = logging.getLogger(__name__)

RECEIPT = "reclaimer-installed.json"  # the program's own receipt: its "version" is what the program goes by
OLD_RECEIPT = "reclaimer-workshop-version.json"  # written beside it with the same values, never read by the program
FLAG = "workshop"  # the dashboard line's note
SIGN_IN_FLAG = "steamcmd"
SIGN_IN_FAILED = re.compile(r"signing in as \S+ failed|sign in to steamcmd again|no key for mcc", re.IGNORECASE)
SOURCE = "workshop-updates"  # what these warnings are kept under in the catalogue

FIRST_LOOK = 30.0  # after the servers have started and their consoles answer
LOOK_SECONDS = 900.0  # Huragok's own look at Steam
EXTERNAL_LOOK = 10.0  # a server Huragok didn't start: its content folder looked at this often
QUIET = 3600.0  # a failing look at Steam, or a restart that couldn't be sent, is logged again after this long
SAY_SECONDS = 3.0  # between telling the players and `restart now`
FENCE_SECONDS = 20.0  # a failure this long after a restart is the restart not taking (when no new worker was seen)
MAX_RESTARTS = 2  # restarts for one update before Huragok only warns
SHARED_TICK = 1.0  # how often a shared folder's update looks at its servers
HOLDER_WAIT = 120.0  # the last server up, restarted for the update: this long for "updated to the version"
RESTART_WAIT = 120.0  # every server restarted together: this long for "updated to the version"
DOWNLOAD_WAIT = 1800.0  # the servers started again: this long for one to download the update afresh
AFTER_SECONDS = 60.0  # once it's in: a server still failing this long after is retrying a stale copy (S1b f)

DOWNLOADING = re.compile(r"downloading the version of (?P<newest>.+?) \(installed: (?P<installed>[^)]+)\)", re.I)
UPDATED = re.compile(r"updated to the version of (?P<version>.+?) between games", re.I)
FAILED = re.compile(r"installing the update failed \((?P<reason>.*)\)(?:; trying again)?", re.I)
STAGED = re.compile(r"^\.workshop-(?P<item>\d+)-(?P<pid>\d+)-(?P<n>[\w.-]+)$")  # the program's copy waiting to go in

# Rules whose warnings follow one item's update on one server, cleared together once it's in
UPDATE_RULES = ("mod-update-downloading", "mod-update-players", "mod-update-waiting", "mod-update-held",
                "mod-update-held-shared", "mod-update-external", "mod-update-stuck", "mod-update-group")

# What Huragok 0.6 left beside a content folder when it swapped mod copies itself: only cleaned up now (U → clean up,
# content/upkeep.py), and kept in step when a server's content is renamed (content/serverops.py).
ASIDE = ".huragok-workshop-previous"
MARKER = ".moving.json"  # in the aside folder while a swap had copies moved aside
ASIDE_NAME = re.compile(r"-(?P<item>\d+)-\d{8}-\d{6}(?P<failed>-failed)?$")


def aside_folders(content_dir: Path) -> Path:
    return content_dir.parent / ASIDE


def kept_by_markers(aside_root: Path) -> set[Path]:
    """The old copies an unfinished 0.6 swap's marker names: never offered for deletion."""
    kept: set[Path] = set()
    for marker in aside_root.glob(f"*{MARKER}"):
        try:
            moves = json.loads(marker.read_text(encoding="utf-8")).get("moves", [])
            kept.update(Path(aside) for _, aside in moves)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return kept


# Reading what's installed ---------------------------------------------------------------------------------------------

def date_text(stamp: int | None) -> str:
    return time.strftime("%d %b %Y", time.gmtime(stamp)) if stamp else "?"  # UTC, as the program says it


def installed_version(content_dir: Path | None, item: int) -> dict[str, Any] | None:
    """The version the program goes by, as it wrote it while installing the item: reclaimer-installed.json's
    "version", {item, manifest, updated}. None: not installed (or a copy the program didn't install)."""
    if content_dir is None:
        return None
    try:
        data = json.loads((content_dir / "Mods" / "Workshop" / str(item) / RECEIPT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, dict) or not isinstance(version.get("updated"), int) or isinstance(version["updated"],
                                                                                                    bool):
        return None
    return version


def is_installed(folder: Path) -> bool:
    """A copy the program installed: Mods/Workshop/<item> with its receipt (either one: 0.9's program wrote only the
    older)."""
    return (folder / RECEIPT).is_file() or (folder / OLD_RECEIPT).is_file()


def is_newer(installed: dict[str, Any], steam: SteamItem) -> bool:
    """Whether the program takes Steam's version for a new one: the manifest differs AND it's newer (S1: either alone
    isn't an update)."""
    if steam.updated is None:
        return False
    if str(installed.get("manifest", "")) == str(steam.manifest or ""):
        return False
    return steam.updated > int(installed["updated"])


@dataclass(frozen=True)
class Staged:
    """A copy the program downloaded, waiting in the content folder to go in: .workshop-<item>-<pid>-<n>."""

    path: Path
    item: int
    pid: int  # the program's supervisor that made it


def staged_copies(content_dir: Path | None) -> list[Staged]:
    found: list[Staged] = []
    if content_dir is None:
        return found
    try:
        entries = list(content_dir.iterdir())
    except OSError:
        return found
    for path in entries:
        match = STAGED.match(path.name)
        if match and path.is_dir():
            found.append(Staged(path, int(match["item"]), int(match["pid"])))
    return found


def download_folder(server: ServerConfig, item: int) -> Path:
    """Where the server folder's SteamCMD keeps its download of the item (data/workshop-cache/…): a second copy beside
    the installed one (answer 20)."""
    return (server.data_dir.parent / "workshop-cache" / "steamapps" / "workshop" / "content" / str(MCC_APP)
            / str(item))


def failure_reason(text: str) -> str:
    match = FAILED.search(text)
    return match["reason"].strip() if match else ""


def restart_line(names: str) -> str:
    """What players read just before a restart that puts a mod's new version in."""
    return callout("now", f"Restarting for a mod update: the new version of {names} goes in now. Join again in a "
                          "minute.")


def stop_line(names: str) -> str:
    """What players read before their server stops for an update shared with other servers (the shutdown pattern)."""
    return callout("now", f"Stopping for a mod update: this server is back once the new version of {names} is in. "
                          "Join again in a few minutes.")


def _mod_title(content_dir: Path | None, item: int) -> str:
    """The installed copy's own title (its ModInfo.json), or ""."""
    if content_dir is None:
        return ""
    try:
        data = json.loads((content_dir / "Mods" / "Workshop" / str(item) / "ModInfo.json")
                          .read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return ""
    title = data.get("Title") if isinstance(data, dict) else None
    if isinstance(title, dict):
        title = title.get("Neutral")
    return title.strip() if isinstance(title, str) else ""


# One server's update ----------------------------------------------------------------------------------------------

@dataclass
class Pending:
    """A server's downloaded mod update that its map in use holds up, and Huragok's restarts for it."""

    server: str
    since: float
    items: dict[int, str] = field(default_factory=dict)  # item -> its name
    reason: str = ""  # the server's own words ("cannot move … aside: Access is denied …")
    restarts: int = 0
    sent: float | None = None  # when the last restart was sent (Huragok's clock)
    fenced: bool = False  # its new game worker has started since (its console said so): a failure now is real
    busy: bool = False  # a restart being sent
    stuck: bool = False  # MAX_RESTARTS didn't put it in: only warned about now
    said: float = float("-inf")  # a restart that couldn't be sent, last logged

    @property
    def names(self) -> str:
        return ", ".join(self.items.values())


# Servers sharing a content folder ---------------------------------------------------------------------------------

@dataclass
class SharedUpdate:
    """One shared content folder's update (answer 15, S1b; plan 4.4 with changes 1 and 2, OK'd 22 November).

    Nobody on any of them: RCON `restart` to all at the same moment (one install, all back in 12 s). Otherwise the
    shutdown pattern:
      1. stop (RCON `quit`) the ones with nobody on, so nobody joins them while the others finish;
      2. as each busy one's game ends, tell its players and stop it too;
      3. change 1: when only one is left up and it's the one holding the download, restart it instead (at once when
         empty, at its game's end when busy): the update goes in, then the others start already up to date;
      4. otherwise, once the last is down, start them all: their start-up look downloads the update again;
      5. change 2: once one says "a new version was downloaded", restart them all at the same moment (not as soon as
         they're up: the download finishes after that), unless one says "updated to the version" first.
    Afterwards a server still failing to install is retrying a stale copy (two downloads): `quit` and start it again.
    A `quit` throws a server's download away; a restart keeps it."""

    content: Path
    members: list[str]
    items: dict[int, str]
    holders: set[str]  # those holding a download: said they downloaded it (or failed to install it), not stopped since
    began: float
    step: str = "stopping"  # all-together, stopping, holder, starting, confirming, after, done
    since: float = 0.0  # when this step began
    stopped: list[str] = field(default_factory=list)  # stopped by Huragok for it: started again later
    started_again: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)  # restarted by the last restart
    restarted_at: float | None = None
    restarts: int = 0
    downloaded_at: float | None = None  # a member said it downloaded the update (or failed to install it)
    updated_at: float | None = None  # a member said "updated to the version"
    failing: dict[str, float] = field(default_factory=dict)  # member -> its last "installing the update failed"
    worker_started: dict[str, float] = field(default_factory=dict)  # member -> its new game worker (console)
    requit: set[str] = field(default_factory=set)  # stopped again afterwards for a stale copy
    to_start: list[str] = field(default_factory=list)  # those, to start once they're down
    refused: set[str] = field(default_factory=set)  # couldn't be stopped (said once)

    @property
    def names(self) -> str:
        return ", ".join(self.items.values())

    def failed_since(self, member: str, at: float | None) -> bool:
        """It said it couldn't install after the restart at `at` took: its new worker started first, or long enough
        has gone by."""
        failed = self.failing.get(member)
        if failed is None or at is None or failed <= at:
            return False
        return self.worker_started.get(member, float("-inf")) > at or failed - at >= FENCE_SECONDS


class WorkshopUpdateService:
    def __init__(self, settings: Settings, bus: EventBus, lifecycle: Lifecycle, *, warnings: Warnings | None = None,
                 fetch: Callable[[list[int]], dict[int, SteamItem]] = workshop_details,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.bus = bus
        self.lifecycle = lifecycle
        self.warnings = warnings
        self.fetch = fetch
        self.clock = clock
        self.pending: dict[str, Pending] = {}  # server -> its update held up by the map
        self.shared: dict[Path, SharedUpdate] = {}  # content folder -> its servers' update under way
        self.titles: dict[int, str] = {}
        self.steam: dict[int, SteamItem] = {}  # Steam's last answer
        self.newer: dict[tuple[str, int], tuple[str, str]] = {}  # (server, item) -> (Steam's, installed): not in yet
        self.installed_here: set[tuple[Path, int]] = set()  # (server folder, item) put in while Huragok ran
        self.announced: set[tuple[str, int, str]] = set()  # (server, item, version) said in the staff channel
        self.last_check: float | None = None
        self.last_error = ""
        self._error_logged = float("-inf")
        self._sign_in_logged: dict[str, float] = {}
        self._seen_staged: dict[tuple[str, int], int] = {}  # a server Huragok didn't start: looks its copy waited
        bus.subscribe("server.workshop", self._on_console)
        bus.subscribe("server.started", self._on_worker)

    # Settings and servers ------------------------------------------------------------------------------------------

    def restart_on(self, server: ServerConfig) -> bool:
        """[workshop_updates] restart, or the server's own workshop_updates in its [[server]] block."""
        return self.settings.of(server).workshop_restart

    def _server(self, name: str | None) -> ServerConfig | None:
        return next((s for s in self.settings.servers if s.name == name), None)

    def group_of(self, server: ServerConfig) -> list[ServerConfig]:
        """The servers using its content folder (itself among them): one installed copy of each mod for all."""
        if server.content_dir is None:
            return [server]
        return [s for s in self.settings.servers if s.content_dir == server.content_dir] or [server]

    def name_of(self, server: ServerConfig | None, item: int) -> str:
        """The mod's name: Steam's title, else its installed copy's, else its item number."""
        title = self.titles.get(item) or (_mod_title(server.content_dir, item) if server is not None else "")
        if title:
            self.titles.setdefault(item, title)
        return title or f"Workshop item {item}"

    # Warnings -----------------------------------------------------------------------------------------------------

    def _warn(self, server: str, rule: str, item: int, about: Path | None = None, **fields: Any) -> None:
        if self.warnings is not None:
            self.warnings.set(server, rule, str(item), about=about, source=SOURCE, item=item, **fields)

    def _unwarn(self, server: str, rule: str, item: int) -> None:
        if self.warnings is not None:
            self.warnings.clear(server, rule, str(item))

    def _settle(self, server: str, item: int) -> None:
        """The item's new version is in (or there's none): what was said about its update goes."""
        for rule in UPDATE_RULES:
            self._unwarn(server, rule, item)
        self.newer.pop((server, item), None)
        self._seen_staged.pop((server, item), None)
        pending = self.pending.get(server)
        if pending is not None:
            pending.items.pop(item, None)
            if not pending.items:
                del self.pending[server]
                self.lifecycle.set_flag(server, FLAG, None)

    # What the servers say -------------------------------------------------------------------------------------------

    def _on_console(self, event: Event) -> None:
        """A server's [workshop] lines (servers/logs.py): what it downloads, installs, and can't install."""
        server = self._server(event.server)
        if server is None:
            return
        text = str(event.get("text") or "")
        if SIGN_IN_FAILED.search(text):
            self._sign_in_failed(server, text)
        try:
            item = int(event.get("item"))
        except (TypeError, ValueError):
            return
        self.line(server, item, text)

    def line(self, server: ServerConfig, item: int, text: str) -> None:
        lower = text.lower()
        if match := DOWNLOADING.search(text):
            self._downloading(server, item, match["newest"].strip(), match["installed"].strip())
        elif lower.startswith("a new version was downloaded"):
            self._downloaded(server, item)
        elif lower.startswith("installing the update failed"):
            self._failed(server, item, text)
        elif lower.startswith("updated to the version"):
            self._updated(server, item, text)
        elif "installed copy is the version steam serves" in lower:
            for member in self.group_of(server):
                self._settle(member.name, item)
        elif lower.startswith(("ready", "installed")):
            self.lifecycle.set_flag(server.name, SIGN_IN_FLAG, None)

    def _on_worker(self, event: Event) -> None:
        """A server's new game worker started (its console's `on port …`): a restart sent before it has taken."""
        name = event.server or ""
        pending = self.pending.get(name)
        if pending is not None and pending.sent is not None:
            pending.fenced = True
        for shared in self.shared.values():
            if name in shared.members:
                shared.worker_started[name] = self.clock()

    def _sign_in_failed(self, server: ServerConfig, text: str) -> None:
        self.lifecycle.set_flag(server.name, SIGN_IN_FLAG, "! SteamCMD sign-in failed: press I to sign in again")
        if self.clock() - self._sign_in_logged.get(server.name, -QUIET) > QUIET:
            self._sign_in_logged[server.name] = self.clock()
            log.error("[%s] SteamCMD can't sign in (%s). Press I in the window to sign in again: until then the server "
                      "can't download its Workshop mods or their new versions", server.name, text.strip()[:160])

    def _downloading(self, server: ServerConfig, item: int, newest: str, installed: str) -> None:
        """A new version on Steam, the server getting it: it plays the old one meanwhile, and players who have the new
        one can't join (answer 19). Every server sharing its content folder has the old one too."""
        name = self.name_of(server, item)
        for member in self.group_of(server):
            self.newer[(member.name, item)] = (newest, installed)
            self._warn(member.name, "mod-update-downloading", item, mod=name, newest=newest, installed=installed)
            self._warn(member.name, "mod-update-players", item, mod=name)
        self._announce(server, item, newest)

    def _announce(self, server: ServerConfig, item: int, newest: str) -> None:
        if (server.name, item, newest) in self.announced:
            return
        self.announced.add((server.name, item, newest))
        log.info("[%s] %s has a new version on Steam (%s); the server gets it by itself", server.name,
                 self.name_of(server, item), newest)
        self.bus.emit("server.workshop_update", server.name, item=item, title=self.titles.get(item, ""),
                      newest=newest)

    def _downloaded(self, server: ServerConfig, item: int) -> None:
        """It has the new version now; a server whose game doesn't use the mod installs it straight after (S1)."""
        shared = self.shared.get(server.content_dir) if server.content_dir is not None else None
        if shared is not None:
            shared.holders.add(server.name)
            shared.downloaded_at = self.clock()

    def _failed(self, server: ServerConfig, item: int, text: str) -> None:
        """"installing the update failed (cannot move … aside …)": the map in use holds the update up. Between games:
        at once with nobody on, at the game's end with people on (S1b)."""
        lower = text.lower()
        if "cannot lock" in lower:
            return  # another server on the folder is installing it this moment: its "updated" says when it's in
        if "aside" not in lower:
            return  # another fault (the disk…), which a restart doesn't mend: content/mods.py says it with its why
        name = self.name_of(server, item)
        reason = failure_reason(text)
        shared = self.shared.get(server.content_dir) if server.content_dir is not None else None
        if shared is not None:
            shared.failing[server.name] = self.clock()
            shared.items.setdefault(item, name)
            if server.name not in shared.stopped:
                shared.holders.add(server.name)  # it holds a download
            if shared.step == "starting":
                shared.downloaded_at = self.clock()  # step 5's signal as much as "a new version was downloaded"
            return
        if len(self.group_of(server)) > 1:
            self._shared_failed(server, item, name, reason)
            return
        pending = self.pending.get(server.name)
        if pending is None:
            pending = self.pending[server.name] = Pending(server.name, self.clock())
        pending.items[item] = name
        pending.reason = reason or pending.reason
        if pending.stuck or pending.busy:
            return
        if pending.sent is not None:
            if not pending.fenced and self.clock() - pending.sent < FENCE_SECONDS:
                return  # said before the restart took
            if pending.restarts >= MAX_RESTARTS:
                self._stuck(server, pending)
                return
        if not self.restart_on(server):
            for each, title in pending.items.items():
                self._warn(server.name, "mod-update-held", each, mod=title, where=self._why_off(server))
            self.lifecycle.set_flag(server.name, FLAG, f"! mod update waiting: {pending.names}: its map is in use")
            return
        for each, title in pending.items.items():
            self._warn(server.name, "mod-update-waiting", each, mod=title,
                       when="at once (nobody is on)" if self.lifecycle.empty(server.name) else
                       "now its game has ended (players are told first)")
        self.lifecycle.set_flag(server.name, FLAG, f"mod update: restarting to put {pending.names} in")
        self._spawn(self._restart_one(server, pending), f"[{server.name}] restarting to put a mod update in")

    def _why_off(self, server: ServerConfig) -> str:
        return ("its own workshop_updates = \"off\" in settings.toml"
                if self.settings.of(server).own("workshop_updates") == "off" else "[workshop_updates] restart = false")

    def _spawn(self, work: Any, what: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # an event with no loop running (some tests): nothing can be sent
            work.close()
            return
        background(work, what, log)

    async def _restart_one(self, server: ServerConfig, pending: Pending) -> None:
        pending.busy = True
        try:
            name = server.name
            if not self.lifecycle.is_up(name):
                return
            if self.lifecycle.in_game(name) and not self.lifecycle.empty(name):
                return  # mid-game: the server only tries between games, and says so again then
            now = not self.lifecycle.empty(name)
            if now:
                await self.lifecycle.say(name, restart_line(pending.names))
                await asyncio.sleep(SAY_SECONDS)
            result = await self.lifecycle.restart_worker(name, now=now, reason=f"putting in the new version of "
                                                                                 f"{pending.names}")
            if not result.ok:
                if self.clock() - pending.said > QUIET:
                    pending.said = self.clock()
                    log.warning("[%s] couldn't restart it to put the new version of %s in (%s); tried again at its next "
                                "try to install it", name, pending.names, result.message)
                return
            pending.restarts += 1
            pending.sent, pending.fenced = self.clock(), False
            log.info("[%s] the new version of %s was downloaded but can't go in while the map is in use: restarted "
                     "its game (%s) to put it in", name, pending.names, "restart now" if now else "restart")
        finally:
            pending.busy = False

    def _stuck(self, server: ServerConfig, pending: Pending) -> None:
        pending.stuck = True
        folder = server.content_dir / "Mods" / "Workshop" if server.content_dir is not None else server.folder
        for item, title in pending.items.items():
            self._warn(server.name, "mod-update-stuck", item, mod=title, restarts=pending.restarts,
                       reason=pending.reason or "the map is in use", folder=str(folder))
            self._unwarn(server.name, "mod-update-waiting", item)
        log.error("[%s] %d restart(s) didn't put the new version of %s in: the server still can't install it (%s). "
                  "Huragok stops trying; the warning says what to do", server.name, pending.restarts, pending.names,
                  pending.reason or "the map is in use")
        self.lifecycle.set_flag(server.name, FLAG, f"! mod update stuck: {pending.names}")

    def _updated(self, server: ServerConfig, item: int, text: str) -> None:
        """"updated to the version … between games": in, for every server on the content folder."""
        name = self.name_of(server, item)
        match = UPDATED.search(text)
        version = match["version"].strip() if match else ""
        before = self.newer.get((server.name, item), ("", ""))[1]
        pending = self.pending.get(server.name)
        restarted = pending is not None and item in pending.items and pending.restarts > 0
        group = self.group_of(server)
        for member in group:
            self._settle(member.name, item)
            self.installed_here.add((member.folder, item))
        shared = self.shared.get(server.content_dir) if server.content_dir is not None else None
        if shared is not None:
            shared.updated_at = self.clock()
            shared.items.setdefault(item, name)
        if restarted:
            log.info("[%s] the restart put the new version of %s in", server.name, name)
        elif shared is None:
            log.info("[%s] the server put the new version of %s in itself", server.name, name)
        names = f"{name} {before} -> {version}" if before and version else name
        self.bus.emit("server.workshop_updated", server.name, items=[item], names=[names])

    # Servers sharing a content folder ---------------------------------------------------------------------------------

    def _shared_failed(self, server: ServerConfig, item: int, name: str, reason: str) -> None:
        """The server holding the download can't install it while any server on the folder has the map open."""
        group = self.group_of(server)
        up = [s for s in group if self.lifecycle.is_up(s.name)]
        if server.name not in [s.name for s in up]:
            return
        if not self.restart_on(server):
            self._warn(server.name, "mod-update-held", item, mod=name, where=self._why_off(server))
            return
        if off := [s.name for s in up if not self.restart_on(s)]:
            # Answer 29: one "off" holds the group (none can install while it has the map open); only warned about
            self._warn(server.name, "mod-update-held-shared", item, mod=name, holder=", ".join(off))
            return
        empty = all(self.lifecycle.empty(s.name) for s in up)
        external = [s.name for s in up if self.lifecycle.is_external(s.name)]
        if not empty and external:
            # Answer 30: never stopped from here; they're restarted together once nobody's on any of them
            self._warn(server.name, "mod-update-external", item, mod=name, external=", ".join(external))
            return
        for rule in ("mod-update-held", "mod-update-held-shared", "mod-update-external"):
            self._unwarn(server.name, rule, item)
        now = self.clock()
        shared = SharedUpdate(server.content_dir, [s.name for s in group], {item: name}, {server.name}, now,  # type: ignore[arg-type]
                              step="all-together" if empty else "stopping", since=now, failing={server.name: now})
        self.shared[shared.content] = shared
        log.info("[%s] the new version of %s was downloaded but can't go in while the servers sharing its content "
                 "folder (%s) have the map open: %s", server.name, name, ", ".join(shared.members),
                 "restarting them all at once (nobody is on)" if empty else
                 "stopping each as it empties or its game ends, then restarting them for it")
        self._spawn(self._run_shared(shared), f"[{server.name}] a mod update for the servers sharing its content")

    async def _run_shared(self, shared: SharedUpdate) -> None:
        try:
            while shared.step != "done":
                await self._shared_step(shared)
                if shared.step != "done":
                    await asyncio.sleep(SHARED_TICK)
        finally:
            self.shared.pop(shared.content, None)
            for member in shared.members:
                for item in shared.items:
                    self._unwarn(member, "mod-update-group", item)
                self.lifecycle.set_flag(member, FLAG, None)

    async def _shared_step(self, shared: SharedUpdate) -> None:
        now = self.clock()
        up = [m for m in shared.members if self.lifecycle.is_up(m)]
        if shared.step == "all-together":
            await self._restart_all(shared, up)
            shared.step, shared.since = "confirming", now
        elif shared.step == "stopping":
            await self._stopping(shared, up, now)
        elif shared.step == "holder":
            holder = shared.restarted[0] if shared.restarted else ""
            if shared.updated_at is not None:
                self._start_stopped(shared)  # they start already up to date (S1b k)
                shared.step, shared.since = "after", now
            elif shared.failed_since(holder, shared.restarted_at) and shared.restarts < MAX_RESTARTS:
                await self._restart_member(shared, holder)  # once more
            elif shared.failed_since(holder, shared.restarted_at) or now - shared.since > HOLDER_WAIT:
                self._shared_stuck(shared, [holder])
                self._start_stopped(shared)
                shared.step = "done"
        elif shared.step == "starting":
            if shared.updated_at is not None and shared.updated_at >= shared.since:
                shared.step, shared.since = "after", now  # a start won the race: nothing more to do (S1b d)
            elif shared.downloaded_at is not None and shared.downloaded_at >= shared.since:
                if all(self.lifecycle.is_up(m) or self.lifecycle.is_stopped(m) for m in shared.started_again):
                    await self._restart_all(shared, up)
                    shared.step, shared.since = "confirming", now
            elif now - shared.since > DOWNLOAD_WAIT:
                log.warning("[%s] none of the servers sharing %s downloaded the new version of %s again within %s; "
                            "their next look at Steam does", ", ".join(shared.members), shared.content, shared.names,
                            format_duration(DOWNLOAD_WAIT))
                shared.step = "done"
        elif shared.step == "confirming":
            if shared.updated_at is not None and shared.updated_at >= (shared.restarted_at or 0):
                shared.step, shared.since = "after", now
            else:
                failing = [m for m in shared.restarted if shared.failed_since(m, shared.restarted_at)]
                if failing and shared.restarts < MAX_RESTARTS:
                    await self._restart_all(shared, up)  # once more
                elif failing or now - shared.since > RESTART_WAIT:
                    self._shared_stuck(shared, failing or shared.restarted)
                    shared.step = "done"
        elif shared.step == "after":
            self._after(shared, now)

    async def _stopping(self, shared: SharedUpdate, up: list[str], now: float) -> None:
        if shared.updated_at is not None:  # it went in another way: whoever's stopped is started again
            self._start_stopped(shared)
            shared.step, shared.since = "after", now
            return
        # One Huragok stopped and a person started again is up again: stopped again like the rest
        shared.stopped = [m for m in shared.stopped if not self.lifecycle.is_up(m)]
        if len(up) == 1 and up[0] in shared.holders:
            holder = up[0]
            if self.lifecycle.in_game(holder) and not self.lifecycle.empty(holder):
                self._shared_waiting(shared, [holder], now)
                return
            self._clear_waiting(shared)
            await self._restart_member(shared, holder)  # change 1: it puts the update in, the others start after
            shared.step, shared.since = "holder", now
            return
        busy = []
        for member in up:
            if self.lifecycle.empty(member):
                self._stop(shared, member)
            elif not self.lifecycle.in_game(member):
                await self.lifecycle.say(member, stop_line(shared.names))
                await asyncio.sleep(SAY_SECONDS)
                self._stop(shared, member)
            else:
                busy.append(member)
        if busy:
            self._shared_waiting(shared, busy, now)
            return
        if any(self.lifecycle.is_up(m) for m in shared.members) or \
                any(not self.lifecycle.is_stopped(m) for m in shared.stopped):
            return  # one stopping still, or one just restarted (change 1 next time)
        self._clear_waiting(shared)
        self._start_stopped(shared)
        shared.step, shared.since = "starting", now

    def _stop(self, shared: SharedUpdate, member: str) -> None:
        result = self.lifecycle.quit(member, reason=f"stopped to put in the new version of {shared.names} (its "
                                                    "content folder is shared)")
        if result.ok:
            shared.stopped.append(member)
            shared.holders.discard(member)  # a quit throws its download away (S1b)
        elif member not in shared.refused:
            shared.refused.add(member)
            log.warning("[%s] couldn't stop it for the update of %s (%s): tried again each second", member,
                        shared.names, result.message)

    def _start_stopped(self, shared: SharedUpdate) -> None:
        for member in shared.stopped:
            result = self.lifecycle.start(member)
            if not result.ok:
                log.warning("[%s] couldn't start it again after the update of %s: %s", member, shared.names,
                            result.message)
        shared.started_again, shared.stopped = list(shared.stopped), []

    async def _restart_member(self, shared: SharedUpdate, member: str) -> None:
        now = not self.lifecycle.empty(member)
        if now:
            await self.lifecycle.say(member, restart_line(shared.names))
            await asyncio.sleep(SAY_SECONDS)
        result = await self.lifecycle.restart_worker(member, now=now, reason=f"putting in the new version of "
                                                                             f"{shared.names}")
        if not result.ok:
            log.warning("[%s] couldn't restart its game for the update of %s: %s", member, shared.names,
                        result.message)
        shared.restarted, shared.restarted_at = [member], self.clock()
        shared.restarts += 1

    async def _restart_all(self, shared: SharedUpdate, up: list[str]) -> None:
        """Every server up on the folder restarted at the same moment: told first where people are on."""
        busy = [m for m in up if not self.lifecycle.empty(m)]
        if busy:
            await asyncio.gather(*(self.lifecycle.say(m, restart_line(shared.names)) for m in busy))
            await asyncio.sleep(SAY_SECONDS)
        results = await asyncio.gather(*(self.lifecycle.restart_worker(
            m, now=m in busy, reason=f"putting in the new version of {shared.names}") for m in up))
        for member, result in zip(up, results):
            if not result.ok:
                log.warning("[%s] couldn't restart its game for the update of %s: %s", member, shared.names,
                            result.message)
        shared.restarted, shared.restarted_at = list(up), self.clock()
        shared.restarts += 1
        log.info("[%s] restarted together to put in the new version of %s", ", ".join(up) or "none",
                 shared.names)

    def _after(self, shared: SharedUpdate, now: float) -> None:
        """Afterwards: a server still failing to install is retrying a stale copy of its own (only after two
        downloads, S1b f). A restart keeps that copy; a `quit` and a start throw it away."""
        since = shared.updated_at if shared.updated_at is not None else shared.since
        for member, at in shared.failing.items():
            if at > since + 2 and member not in shared.requit and self.lifecycle.is_up(member):
                if self.lifecycle.is_external(member):
                    self._shared_stuck(shared, [member])
                    shared.requit.add(member)
                    continue
                log.info("[%s] it still tries to install an older download of %s: stopping it and starting it again",
                         member, shared.names)
                if self.lifecycle.quit(member, reason=f"throwing away a stale download of {shared.names}").ok:
                    shared.to_start.append(member)
                shared.requit.add(member)
        for member in list(shared.to_start):
            if self.lifecycle.is_stopped(member):
                self.lifecycle.start(member)
                shared.to_start.remove(member)
        if now - shared.since >= AFTER_SECONDS and not shared.to_start:
            shared.step = "done"

    def _shared_waiting(self, shared: SharedUpdate, busy: list[str], now: float) -> None:
        """Answer 38: which server's game holds the group up, and for how long, in the window and the staff
        channel."""
        waited = format_duration(now - shared.began)
        stopped = ", ".join(shared.stopped) or "none yet"
        for member in busy:
            for item, name in shared.items.items():
                self._warn(member, "mod-update-group", item, mod=name, stopped=stopped, waited=waited)
        for member in shared.stopped:
            self.lifecycle.set_flag(member, FLAG, f"stopped for the update of {shared.names}: waiting for "
                                                  f"{', '.join(busy)}'s game ({waited})")

    def _clear_waiting(self, shared: SharedUpdate) -> None:
        for member in shared.members:
            for item in shared.items:
                self._unwarn(member, "mod-update-group", item)

    def _shared_stuck(self, shared: SharedUpdate, members: list[str]) -> None:
        folder = shared.content / "Mods" / "Workshop"
        for member in members or shared.members[:1]:
            for item, name in shared.items.items():
                self._warn(member, "mod-update-stuck", item, mod=name, restarts=shared.restarts,
                           reason="the servers sharing its content folder still have the map open",
                           folder=str(folder))

    # Servers Huragok didn't start -------------------------------------------------------------------------------------

    def look_external(self) -> dict[str, tuple[int | None, set[int]]]:
        """Each server Huragok didn't start, with Workshop items: its program's supervisor and the items whose
        downloaded copy waits in its content folder (made by that supervisor). In a thread."""
        from ..servers.server import supervisor_running

        found: dict[str, tuple[int | None, set[int]]] = {}
        for server in list(self.settings.servers):
            if not server.workshop_items or server.content_dir is None or not self.lifecycle.is_external(server.name):
                continue
            supervisor = supervisor_running(server)
            waiting = {c.item for c in staged_copies(server.content_dir) if supervisor is None or c.pid == supervisor}
            found[server.name] = (supervisor, waiting)
        return found

    def act_external(self, found: dict[str, tuple[int | None, set[int]]]) -> None:
        """Its copy still waiting on two looks in a row, between games: the map holds it up (the program installs one
        within a second when it can). Gone again after a restart: it's in."""
        for name, (_, waiting) in found.items():
            server = self._server(name)
            if server is None:
                continue
            for item in server.workshop_items:
                key = (name, item)
                if item in waiting:
                    if self.lifecycle.in_game(name):
                        continue
                    seen = self._seen_staged[key] = self._seen_staged.get(key, 0) + 1
                    if seen >= 2:
                        self._failed(server, item, "installing the update failed (its downloaded copy waits in the "
                                                   "content folder: cannot move the installed copy aside while the "
                                                   "map is in use)")
                elif self._seen_staged.pop(key, 0):
                    pending = self.pending.get(name)
                    shared = self.shared.get(server.content_dir) if server.content_dir is not None else None
                    if pending is not None and item in pending.items or shared is not None and item in shared.items:
                        self._updated(server, item, "")  # its copy went in with the restart
                    elif (installed := installed_version(server.content_dir, item)) is not None and \
                            (steam := self.steam.get(item)) is not None and not is_newer(installed, steam):
                        self._settle(name, item)  # it put the copy in itself

    # Huragok's own look at Steam ------------------------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.sleep(FIRST_LOOK)
        next_look = float("-inf")
        while True:
            if self.clock() >= next_look:
                await self.check()
                next_look = self.clock() + LOOK_SECONDS
            self.act_external(await asyncio.to_thread(self.look_external))
            await asyncio.sleep(EXTERNAL_LOOK)

    async def check(self, *, quiet: bool = True) -> dict[str, list[int]]:
        """Steam's details of every item the servers list, against what each has installed: a newer version is
        warned about (the server gets it by itself). Returns the servers with a new version not in yet."""
        items = sorted({i for s in self.settings.servers for i in s.workshop_items})
        if not items:
            return {}
        try:
            details = await asyncio.to_thread(self.fetch, items)
        except UpdateError as error:
            self.last_error = str(error)
            if not quiet or self.clock() - self._error_logged > QUIET:
                self._error_logged = self.clock()
                log.log(logging.WARNING if not quiet else logging.INFO, "Couldn't look for new Workshop versions: %s",
                        error)
            return self.waiting_lists()
        self.last_check, self.last_error = time.time(), ""
        self.steam.update(details)
        for item, found in details.items():
            if found.title:
                self.titles[item] = found.title
        for server in self.settings.servers:
            for item in server.workshop_items:
                installed = installed_version(server.content_dir, item)
                steam = details.get(item)
                if installed is None or steam is None:
                    continue  # not installed yet (the server fetches it as it starts), or Steam doesn't list it
                if is_newer(installed, steam):
                    if (server.name, item) not in self.newer:
                        self._downloading(server, item, date_text(steam.updated), date_text(installed["updated"]))
                elif server.name not in self.pending or item not in self.pending[server.name].items:
                    self._settle(server.name, item)
        waiting = self.waiting_lists()
        if not quiet:
            log.info("Workshop: %d item(s) looked at, %s", len(items),
                     f"new versions not in yet on {', '.join(waiting)}" if waiting else "every server has the newest")
        return waiting

    # For the window ---------------------------------------------------------------------------------------------------

    def waiting_lists(self) -> dict[str, list[int]]:
        """Servers with a new version not in yet: Steam has it, or it's downloaded and held up."""
        found: dict[str, set[int]] = {}
        for (server, item) in self.newer:
            found.setdefault(server, set()).add(item)
        for server, pending in self.pending.items():
            found.setdefault(server, set()).update(pending.items)
        return {server: sorted(items) for server, items in found.items() if items}

    def held(self) -> list[str]:
        """The servers whose downloaded update the map holds up, and that Huragok isn't restarting by itself now."""
        return [name for name, pending in self.pending.items() if pending.items]

    def state_of(self, server: ServerConfig, item: int) -> str:
        pending = self.pending.get(server.name)
        if pending is not None and item in pending.items:
            if pending.stuck:
                return f"DOWNLOADED, STUCK: {pending.restarts} restart(s) didn't put it in"
            if not self.restart_on(server):
                return "DOWNLOADED, WAITING FOR A RESTART (the map is in use; restart is off)"
            return "DOWNLOADED, RESTARTING TO PUT IT IN"
        shared = self.shared.get(server.content_dir) if server.content_dir is not None else None
        if shared is not None and item in shared.items:
            return f"DOWNLOADED, PUTTING IT IN ON EVERY SERVER SHARING ITS CONTENT ({shared.step})"
        if (server.name, item) in self.newer:
            newest, _ = self.newer[(server.name, item)]
            return f"NEW VERSION ON STEAM ({newest}): the server gets it by itself"
        return ""

    def item_lines(self, server: ServerConfig) -> list[str]:
        """One server's items: installed version, Steam's newest, and where its update is."""
        lines = []
        for item in server.workshop_items:
            installed = installed_version(server.content_dir, item)
            steam = self.steam.get(item)
            title = self.titles.get(item) or (steam.title if steam else "") or _mod_title(server.content_dir, item)
            state = "not installed yet" if installed is None else f"installed {date_text(installed['updated'])}"
            if steam and steam.updated:
                state += f" · Steam {date_text(steam.updated)}"
            if waiting := self.state_of(server, item):
                state += f" · {waiting}"
            lines.append(f"{item} {title}".rstrip() + f"   {state}")
        return lines

    def report(self) -> list[str]:
        """Every server's Workshop items: installed version, Steam's, and what happens next."""
        lines = []
        for server in self.settings.servers:
            if not server.workshop_items:
                continue
            lines.append(f"{server.name} · {len(server.workshop_items)} item(s) · "
                         f"{self.settings.of(server).workshop_text()}")
            lines.extend(f"  {line}" for line in self.item_lines(server))
        if self.last_error:
            lines.append(f"Last look at Steam failed: {self.last_error}")
        elif self.last_check:
            lines.append(f"Last look at Steam: {time.strftime('%H:%M', time.localtime(self.last_check))}")
        return lines or ["No server lists Workshop items ([workshop] items in its dedicated.toml)."]

    def restart_now(self, name: str) -> tuple[bool, str]:
        """The window's "put it in now": a server whose downloaded update its map holds up (restart off, or stuck),
        restarted as Huragok would have, the person having asked."""
        server = self._server(name)
        if server is None:
            return False, f"no server called {name}"
        pending = self.pending.get(name)
        if pending is None or not pending.items:
            return False, "no downloaded update waits on it: the server puts each in between games itself"
        if pending.busy:
            return True, "already restarting for it"
        pending.stuck, pending.restarts, pending.sent = False, 0, None
        self._spawn(self._restart_one(server, pending), f"[{name}] restarting to put a mod update in (asked)")
        return True, f"restarting it to put {pending.names} in" + (
            "" if self.lifecycle.empty(name) else " (players are told, then dropped: they join again)")


__all__ = ["OLD_RECEIPT", "RECEIPT", "Pending", "SharedUpdate", "Staged", "WorkshopUpdateService", "date_text",
           "download_folder", "installed_version", "is_installed", "is_newer", "staged_copies"]
