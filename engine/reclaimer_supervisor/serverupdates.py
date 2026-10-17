"""New releases of the server program (Project Reclaimer itself), installed at a quiet moment.

Players must be on exactly the server's version, and releases have come out daily,
so a server left behind empties out. The server's own `auto_update` restarts every
server the moment a release downloads, mid-game; the supervisor turns it off and
does this instead:

- looks at the releases on GitHub every few hours and compares with the version
  each running server reports on its console;
- when asked (U in the window, `update --program`, or [server_updates] install),
  downloads `project-reclaimer-dedicated-v<version>.exe`, checks it against the
  release's SHA256SUMS.txt, puts it in place of every copy in the server folders
  (and the root's), keeping the old ones in records/previous-program/, and
  restarts each running server after its game, or now.

A running program file can be renamed on Windows, not overwritten: the old file
is moved aside first, so the servers keep running until their restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .dedicated import find_executable
from .updates import UpdateError, _get, parse_version

if TYPE_CHECKING:
    from .config import Settings
    from .events import EventBus
    from .manager import Lifecycle
    from .rcon import RconClient
    from .state import ServerState

log = logging.getLogger(__name__)

RELEASES = "ProjectReclaimer/project-reclaimer-releases"
PROGRAM_PREFIX = "project-reclaimer-dedicated"
SUMS = "SHA256SUMS.txt"


@dataclass(frozen=True)
class ProgramRelease:
    version: str
    page: str
    notes: str
    exe_url: str
    exe_name: str
    sums_url: str | None = None


def latest_program_release(repository: str = RELEASES, timeout: float = 10.0) -> ProgramRelease | None:
    """The newest release with a dedicated server program in it, or None."""
    import json

    try:
        data = json.loads(_get(f"https://api.github.com/repos/{repository}/releases/latest", timeout))
    except UpdateError as error:
        if " 404 " in str(error):
            return None
        raise
    except ValueError:
        raise UpdateError("GitHub's answer wasn't JSON") from None
    assets = {str(a.get("name", "")): str(a.get("browser_download_url", "")) for a in data.get("assets") or []}
    exe = next((n for n in assets if n.startswith(PROGRAM_PREFIX) and n.endswith(".exe")), None)
    if exe is None:
        return None
    return ProgramRelease(
        version=str(data.get("tag_name") or "").lstrip("v"),
        page=str(data.get("html_url") or f"https://github.com/{repository}/releases"),
        notes=str(data.get("body") or ""),
        exe_url=assets[exe], exe_name=exe, sums_url=assets.get(SUMS))


def parse_sums(text: str) -> dict[str, str]:
    """SHA256SUMS.txt: `<hex>  <name>` (or `*<name>`) per line -> name -> hex."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            found[parts[1].lstrip("*").strip()] = parts[0].lower()
    return found


def download_program(release: ProgramRelease, folder: Path, timeout: float = 600.0) -> Path:
    """The program, verified against the release's checksums when it has them. Returns the file."""
    data = _get(release.exe_url, timeout, accept="application/octet-stream")
    if release.sums_url:
        sums = parse_sums(_get(release.sums_url, 30, accept="application/octet-stream").decode("utf-8", "replace"))
        expected = sums.get(release.exe_name)
        if expected is None:
            raise UpdateError(f"{SUMS} doesn't list {release.exe_name}; nothing was changed")
        if hashlib.sha256(data).hexdigest() != expected:
            raise UpdateError("the download doesn't match the release's checksum; nothing was changed")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / release.exe_name
    path.write_bytes(data)
    return path


def installed_programs(settings: Settings) -> list[Path]:
    """Every copy of the program the servers run: the root's, then each server folder's."""
    name = str(settings.backend_options.get("executable") or "project-reclaimer.exe") \
        if settings.backend == "process" else "project-reclaimer.exe"
    found: list[Path] = []
    for folder in [settings.home, *settings.server_folders]:
        program = find_executable(folder, name)
        if program is not None and program.resolve() not in [p.resolve() for p in found]:
            found.append(program)
    return found


def install_program(settings: Settings, downloaded: Path, version: str) -> list[Path]:
    """Put `downloaded` in place of every installed copy. The old ones go to records/previous-program/."""
    targets = installed_programs(settings)
    if not targets:
        raise UpdateError("no server program to replace: none in the server folders or at the root")
    backup = settings.records / "previous-program" / time.strftime("%Y-%m-%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    first: Path | None = None
    replaced: list[Path] = []
    for target in targets:
        keep = backup / target.parent.name / target.name
        keep.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(target), str(keep))  # a running program can be moved, not overwritten
        except OSError as error:
            raise UpdateError(f"couldn't move {target} aside: {error}") from None
        try:
            if first is None:
                shutil.copy2(downloaded, target)
                first = target
            else:
                try:
                    os.link(first, target)
                except OSError:
                    shutil.copy2(downloaded, target)
        except OSError as error:
            shutil.move(str(keep), str(target))
            raise UpdateError(f"couldn't put the new program at {target}: {error}") from None
        replaced.append(target)
    (backup / "version.txt").write_text(f"replaced by {version}\n", encoding="utf-8")
    log.info("Server program %s installed in %d place(s); the old files are in %s", version, len(replaced), backup)
    return replaced


class ProgramUpdateService:
    """Looks for new server releases, says so, and installs them when asked (or by itself, per [server_updates])."""

    def __init__(self, settings: Settings, bus: EventBus, lifecycle: Lifecycle, states: dict[str, ServerState],
                 clients: dict[str, RconClient]) -> None:
        self.settings = settings
        self.config = settings.server_updates
        self.bus = bus
        self.lifecycle = lifecycle
        self.states = states
        self.clients = clients
        self.pending: ProgramRelease | None = None
        self.announced: str | None = None
        self.installing = False
        self.last_installed: str | None = None

    def running_versions(self) -> dict[str, str]:
        return {name: client.version for name, client in self.clients.items() if client.version}

    def oldest_running(self) -> str | None:
        versions = list(self.running_versions().values())
        return min(versions, key=parse_version) if versions else None

    def is_newer(self, release: ProgramRelease) -> bool | None:
        """True/False against the oldest running server; None when no server has said its version."""
        oldest = self.oldest_running()
        if oldest is None:
            return None
        return parse_version(release.version) > parse_version(oldest)

    async def run(self) -> None:
        if not self.config.check:
            log.info("Not looking for server program updates ([server_updates] check = false)")
            return
        await asyncio.sleep(20)  # the servers' consoles answer first, so their versions are known
        while True:
            await self.check()
            await asyncio.sleep(self.config.check_hours * 3600)

    async def check(self, *, quiet: bool = True) -> ProgramRelease | None:
        try:
            release = await asyncio.to_thread(latest_program_release, self.config.repository)
        except UpdateError as error:
            log.log(logging.INFO if quiet else logging.WARNING, "Couldn't look for server program updates: %s", error)
            return None
        if release is None:
            if not quiet:
                log.info("No server program release found at %s", self.config.repository)
            return None
        newer = self.is_newer(release)
        if newer is False or (newer is None and self.last_installed and
                              parse_version(release.version) <= parse_version(self.last_installed)):
            self.pending = None
            if not quiet:
                log.info("The servers are on the newest server program (%s)", release.version)
            return None
        self.pending = release
        if not quiet or self.announced != release.version:
            self.announce(release, newer)
            self.announced = release.version
            if self.config.install in ("after_game", "now") and newer:
                asyncio.ensure_future(self.install(when=self.config.install, why="[server_updates] install"))
        return release

    def announce(self, release: ProgramRelease, newer: bool | None) -> None:
        running = ", ".join(f"{n} {v}" for n, v in sorted(self.running_versions().items())) or "no server running"
        if newer:
            log.warning("Server program update available: Project Reclaimer %s (running: %s). Players must be on "
                        "the server's exact version. Press U in the window, or: update --program",
                        release.version, running)
        else:
            log.info("Newest server program release: %s (running: %s)", release.version, running)

    async def install(self, *, when: str = "after_game", why: str = "asked for") -> tuple[bool, str]:
        """Download, verify, put in place, restart each running server (after its game, or now)."""
        if self.installing:
            return False, "an install is already under way"
        release = self.pending or await self.check(quiet=False)
        if release is None:
            return False, "no newer server program is known"
        self.installing = True
        try:
            log.warning("Installing server program %s (%s)", release.version, why)
            try:
                path = await asyncio.to_thread(download_program, release, self.settings.records)
                replaced = await asyncio.to_thread(install_program, self.settings, path, release.version)
            except (UpdateError, OSError) as error:
                log.error("Server program update failed: %s", error)
                return False, f"not installed: {error}"
            finally:
                try:
                    (self.settings.records / release.exe_name).unlink(missing_ok=True)
                except OSError:
                    pass
            self.last_installed = release.version
            self.pending = None
            restarted, left = [], []
            for name in list(self.states):
                if not self.lifecycle.is_up(name):
                    continue
                try:
                    await self.lifecycle.restart(name, when=when, reason=f"updating to {release.version}",
                                                 by=self.settings.name)
                    restarted.append(name)
                except Exception as error:  # noqa: BLE001 - LifecycleUnavailable, LifecycleError
                    left.append(f"{name}: {error}")
            message = (f"server program {release.version} installed in {len(replaced)} place(s); "
                       + (f"restarting {', '.join(restarted)} {when.replace('_', ' ')}" if restarted
                          else "no server was running; they start on it next time"))
            if left:
                message += "; not restarted: " + "; ".join(left)
            log.warning(message)
            self.bus.emit("server.program_updated", None, version=release.version, restarted=restarted)
            return True, message
        finally:
            self.installing = False

    def status(self) -> dict[str, Any]:
        return {"pending": self.pending.version if self.pending else None, "running": self.running_versions(),
                "installing": self.installing, "last_installed": self.last_installed}
