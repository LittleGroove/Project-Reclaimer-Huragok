"""The servers' mods, watched while Huragok runs: each mod's health (modcheck.py) as Huragok starts, every
MOD_CHECK_SECONDS and after a Workshop install, and what a running server says about its mods and Workshop items
(servers/logs.py turns those lines into events), each with what it means and what to do."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..core.events import Event, EventBus
from ..core.text import one_line
from ..games import halo3_mapfile
from . import modcheck
from .gamefiles import read_paths

if TYPE_CHECKING:
    from ..core.settings import Settings

log = logging.getLogger(__name__)

MOD_CHECK_SECONDS = 600.0  # the servers' mods are looked at again this often (mods copied in by hand)
MOD_SETTLE_SECONDS = 5.0  # after a Workshop install, before its mods are looked at

# What a server's mod and content lines (servers/logs.py MOD_LINES) mean, and what to do: said once a start under each.
MOD_ADVICE = {
    "content": "a map or game variant file the server can't read is left out; the file's own problem (fix or delete "
               "it)",
    "characters": "players see Halo 3's own characters in that mod's place: check the server's customization "
                  "setting and the mod (its customization.json)",
    "package": "players can't get that mod from this server (too big to share, or a file it may not share): those "
               "who lack it can't join its games",
    "not_shared": "players who lack those mods can't join their games: turn on share_mods in the server's settings, "
                  "or use Workshop mods with their own ModInfo.json",
    "too_big": "nobody can join: the game needs fewer character mods or mod maps' objects",
    "packs": "players who lack those mods need the whole mod to join (no smaller pack could be made)",
    "map_slots": "a session holds 23 mod maps (Halo 3 lists 50 maps, 27 its own): fewer mod maps in the playlist, or a "
                 "restart frees the slots",
    "map_id": "a mod map left out: its map ID clashes with another's, or it declares none; check the mods (the "
              "Workshop screen says which)",
    "forge": "Forge offers fewer objects: those maps' objects are left out of the Forge library",
}

# Workshop lines from the console that say a fetch or install failed: (words in the line, what to do).
WORKSHOP_TROUBLE = [
    ("is incomplete", "files are missing from the installed copy: fetch it again (G → the server → 3 "
                      "Workshop items and mods → I)"),
    ("without an installation receipt", "a folder copied in by hand is in its place: move "
                                        "content/Mods/Workshop/<item> aside, then fetch it"),
    ("names a different workshop item", "its ModInfo.json names another item: the item's own packaging, ask its "
                                        "author"),
    ("engine other than halo 3", "it's a mod for another game in MCC: remove it from the server's Workshop items"),
    ("no mcc mod package", "it isn't a Halo 3 mod: remove it from the server's Workshop items"),
    ("no map files", "it has no maps: remove it from the server's Workshop items, or ask its author"),
    ("invalid file name", "the item's own packaging: ask its author"),
    ("exceeds installation limits", "the item is bigger than the server installs: ask its author"),
    ("filesystem link", "the item's own packaging: ask its author"),
    ("unsupported directory", "the item's own packaging: ask its author"),
    ("no key for mcc", "SteamCMD's account doesn't own MCC: sign in with one that does (I, SteamCMD)"),
    ("anonymous", "SteamCMD needs a signed-in account that owns MCC (I, SteamCMD)"),
    ("signing in as", "SteamCMD's sign-in expired: sign in again (I, SteamCMD)"),
    ("steam does not list it", "the item was removed from the Workshop or made private: the installed copy stays"),
    ("timed out", "Steam or SteamCMD didn't answer: it's tried again later"),
    ("steamcmd stopped", "SteamCMD stopped part way: it's tried again later; its log is in data/workshop-cache"),
    ("did not download", "SteamCMD didn't download it: it's tried again later; its log is in data/workshop-cache"),
    ("update failed", "the new version didn't install; the installed copy stays"),
    ("update check failed", "Steam couldn't be asked about new versions: it's tried again later"),
    ("cannot put the new version in place", "the new version couldn't replace the old (a file in use?): it's tried "
                                            "again later"),
]


class ModWatch:
    def __init__(self, settings: Settings, bus: EventBus,
                 spawn: Callable[..., Any]) -> None:
        self.settings = settings
        self._spawn = spawn  # Huragok's: a background task that can't take the rest down
        self._said: dict[str, set[str]] = {}  # per server, the mod lines said since its start
        self._reports: dict[str, tuple[Any, ...]] = {}  # per mod folder, its report as last logged
        self._changed: asyncio.Event | None = None  # set on a Workshop install: the mods are looked at again
        bus.subscribe("server.workshop", self._note_workshop)
        bus.subscribe("server.map_skipped", self._note_workshop)
        bus.subscribe("log.mod_problem", self._note_mod_problem)
        bus.subscribe("server.starting", self._forget_said)

    # What the servers say ---------------------------------------------------------------------------------------

    def _note_workshop(self, event: Event) -> None:
        """What the server says about its Workshop items, so the window shows the fetch and its outcome."""
        if event.name == "server.map_skipped":
            path = Path(str(event.get("path") or ""))
            reason = str(event.get("reason") or "")
            log.warning("[%s] a mod map can't be used: %s (%s)", event.server, path.name, reason)
            self._explain_skip(event.server or "", path, reason)
            return
        text = str(event.get("text") or "")
        lower = text.lower()
        advice = next((what for words, what in WORKSHOP_TROUBLE if words in lower), None)
        failed = advice is not None or lower.startswith(("error", "failed", "could not", "cannot"))
        log.log(logging.WARNING if failed else logging.INFO, "[%s] Workshop %s: %s", event.server, event.get("item"),
                one_line(text))
        if advice:
            log.warning("[%s]   why: %s", event.server, advice.replace("<item>", str(event.get("item"))))
        if lower.startswith(("installed", "ready")) and self._changed is not None:
            self._changed.set()  # its files changed: the mods are looked at again

    def _note_mod_problem(self, event: Event) -> None:
        """A mod or content file the server couldn't use (servers/logs.py MOD_LINES): said once a start, with what it
        means. The program says some of these again every couple of minutes."""
        text = str(event.get("text") or "")
        said = self._said.setdefault(event.server or "", set())
        if not text or text in said:
            return
        said.add(text)
        log.warning("[%s] %s", event.server, one_line(text))
        if advice := MOD_ADVICE.get(str(event.get("kind") or "")):
            log.warning("[%s]   why: %s", event.server, advice)

    def _forget_said(self, event: Event) -> None:
        self._said.pop(event.server or "", None)

    def _explain_skip(self, server: str, path: Path, reason: str) -> None:
        """Which of the server's checks the map failed and what to do (games/halo3_mapfile.py), on a line of its own:
        the server's reason alone ("its tag index is damaged") covers many faults. The map is read in a thread."""
        try:
            updates = self.settings.of(self.settings.server(server)).workshop_updates != "off"
        except KeyError:
            updates = False

        def say(text: str) -> None:
            log.warning("[%s]   why: %s", server, text)

        try:
            asyncio.get_running_loop()
        except RuntimeError:  # an event published with no loop running (tests): read it here
            say(halo3_mapfile.explain(path, reason, updates=updates))
            return

        async def explain() -> None:
            say(await asyncio.to_thread(halo3_mapfile.explain, path, reason, updates=updates))

        self._spawn(f"[{server}] reading {path.name}", explain, restart=False)

    # Each mod's health ------------------------------------------------------------------------------------------

    async def run(self) -> None:
        """Every server's mods looked at as Huragok starts, every MOD_CHECK_SECONDS and after a Workshop install:
        what changed is logged, each mod good to go or with what's wrong."""
        self._changed = asyncio.Event()
        while True:
            for line, level in await asyncio.to_thread(self.changes):
                log.log(level, "%s", line)
            try:
                await asyncio.wait_for(self._changed.wait(), MOD_CHECK_SECONDS)
                await asyncio.sleep(MOD_SETTLE_SECONDS)  # the install's last files, and a burst of lines, settle first
            except asyncio.TimeoutError:
                pass
            self._changed.clear()

    def changes(self) -> list[tuple[str, int]]:
        """The mods whose report changed since it was last logged, as log lines: (text, level). In a thread."""
        lines: list[tuple[str, int]] = []
        seen: set[str] = set()
        for root, names, game in self.content_roots():
            label = ", ".join(names)
            try:
                reports = modcheck.check_mods(root, game)
            except Exception as error:  # noqa: BLE001 - one content folder mustn't stop the others being looked at
                lines.append((f"[{label}] couldn't look at its mods: {error}", logging.WARNING))
                seen.update(key for key in self._reports if Path(key).is_relative_to(root))
                continue
            for report in reports:
                key = str(report.folder)
                seen.add(key)
                if self._reports.get(key) == report.fingerprint():
                    continue
                self._reports[key] = report.fingerprint()
                bad = not report.good and not report.disabled
                lines.append((f"[{label}] mod {report.summary()}", logging.WARNING if bad else logging.INFO))
                for finding in report.findings if not report.disabled else []:
                    lines.append((f"[{label}]   {finding.level}: {finding.text}",
                                  logging.INFO if finding.level == modcheck.NOTE else logging.WARNING))
        for key in set(self._reports) - seen:  # removed: said again if it comes back
            del self._reports[key]
        return lines

    def content_roots(self) -> list[tuple[Path, list[str], Path | None]]:
        """Each content folder the servers use, the servers using it and their game folder."""
        roots: dict[Path, tuple[list[str], Path | None]] = {}
        for server in self.settings.servers:
            paths = read_paths(server.folder, self.settings.config_name)
            root = server.content_dir or (paths.content if paths else None)
            if root is None:
                continue
            names, game = roots.setdefault(root, ([], paths.game if paths else None))
            names.append(server.name)
        return [(root, names, game) for root, (names, game) in roots.items()]


__all__ = ["MOD_ADVICE", "ModWatch", "WORKSHOP_TROUBLE"]
