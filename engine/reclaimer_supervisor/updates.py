"""New versions of the supervisor, from its GitHub releases. Never the game server: that's its own updater.

An update replaces only the program: engine/ (but not engine/.venv), the start
scripts, docs/, README.md and LICENSE. The host's files (settings.toml,
commands.toml, automod.txt, records/, logs/) are never touched, and the program
files it replaces are kept in records/previous-<version>/.

Nothing is installed unless the host says so (`update` in the supervisor's
window, or `start.cmd update`), or has set [updates] install_on_server_restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import __version__

if TYPE_CHECKING:
    from .config import Settings
    from .events import Event, EventBus

log = logging.getLogger(__name__)

REPOSITORY = "LittleGroove/Project-Reclaimer-Server-Supervisor"
PROGRAM = ("engine", "docs", "start.cmd", "start.sh", "README.md", "LICENSE")  # what an update replaces
KEEP_IN_ENGINE = {".venv"}  # the installed packages stay; start.cmd / start.sh keep them up to date
MAX_DOWNLOAD = 50_000_000
VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str  # "0.2.0"
    page: str  # the release's page on GitHub, with what changed
    notes: str
    download: str  # the zip to install
    digest: str | None = None  # "sha256:…" when GitHub gives one


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.10.2' -> (0, 10, 2). Anything after the numbers (-beta…) is ignored."""
    numbers = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not numbers:
        raise UpdateError(f"'{text}' isn't a version number")
    return tuple(int(n) for n in numbers.group(1).split("."))


def is_newer(release: Release, current: str = __version__) -> bool:
    return parse_version(release.version) > parse_version(current)


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": f"reclaimer-supervisor/{__version__}", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_DOWNLOAD + 1)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub answered {error.code} {error.reason} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise UpdateError(f"couldn't reach GitHub: {reason}") from None
    if len(data) > MAX_DOWNLOAD:
        raise UpdateError(f"{url} is larger than {MAX_DOWNLOAD // 1_000_000} MB")
    return data


def latest_release(repository: str = REPOSITORY, timeout: float = 10.0) -> Release | None:
    """The newest published release (drafts and pre-releases left out), or None if there's none yet."""
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{repository}/releases/latest", timeout))
    except UpdateError as error:
        if " 404 " in str(error):
            return None
        raise
    except ValueError:
        raise UpdateError("GitHub's answer wasn't JSON") from None
    tag = str(data.get("tag_name") or "")
    assets = [a for a in data.get("assets") or [] if str(a.get("name", "")).endswith(".zip")]
    ours = next((a for a in assets if str(a["name"]).startswith("reclaimer-supervisor")), assets[0] if assets else None)
    return Release(
        version=tag.lstrip("v"),
        page=str(data.get("html_url") or f"https://github.com/{repository}/releases"),
        notes=str(data.get("body") or ""),
        download=str(ours["browser_download_url"]) if ours else str(data.get("zipball_url") or ""),
        digest=(ours or {}).get("digest"),
    )


def download(release: Release, folder: Path, timeout: float = 120.0) -> Path:
    if not release.download:
        raise UpdateError(f"release {release.version} has nothing to download")
    data = _get(release.download, timeout, accept="application/octet-stream")
    if release.digest and release.digest.startswith("sha256:"):
        if hashlib.sha256(data).hexdigest() != release.digest.removeprefix("sha256:"):
            raise UpdateError("the download doesn't match GitHub's checksum; nothing was changed")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"reclaimer-supervisor-{release.version}.zip"
    path.write_bytes(data)
    return path


def program_root(extracted: Path) -> Path:
    """The folder in an unpacked release that holds engine/ and the start scripts."""
    for init in sorted(extracted.glob("**/engine/reclaimer_supervisor/__init__.py"), key=lambda p: len(p.parts)):
        return init.parents[2]
    raise UpdateError("the download has no engine/reclaimer_supervisor in it")


def version_in(root: Path) -> str:
    text = (root / "engine" / "reclaimer_supervisor" / "__init__.py").read_text(encoding="utf-8")
    found = VERSION_LINE.search(text)
    if not found:
        raise UpdateError("the download doesn't say which version it is")
    return found.group(1)


def installed_here(home: Path) -> bool:
    """True when this copy runs from home/engine, as start.cmd / start.sh run it; only then can it update itself."""
    return Path(__file__).resolve().parents[1] == (home / "engine").resolve()


def install(archive: Path, home: Path, expected: str | None = None) -> str:
    """Put the release in `archive` in place of the program in `home`. Returns its version.

    The replaced files go to records/previous-<this version>/. If anything goes
    wrong half way, what was moved is moved back.
    """
    records = home / "records"
    staging = records / "update"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(staging)  # strips absolute paths and ".." from member names
    except zipfile.BadZipFile:
        raise UpdateError("the download isn't a zip file") from None
    root = program_root(staging)
    version = version_in(root)
    if expected and parse_version(version) != parse_version(expected):
        raise UpdateError(f"the download says it's version {version}, not {expected}; nothing was changed")

    backup = records / f"previous-{__version__}"
    shutil.rmtree(backup, ignore_errors=True)
    (backup / "engine").mkdir(parents=True)
    moved: list[tuple[Path, Path]] = []  # (from, to), to undo in reverse

    def move(source: Path, target: Path) -> None:
        shutil.move(str(source), str(target))
        moved.append((source, target))

    try:
        engine = home / "engine"
        engine.mkdir(exist_ok=True)
        for item in list(engine.iterdir()):
            if item.name not in KEEP_IN_ENGINE:
                move(item, backup / "engine" / item.name)
        for item in (root / "engine").iterdir():
            if item.name not in KEEP_IN_ENGINE:
                move(item, engine / item.name)
        for name in PROGRAM[1:]:
            if (home / name).exists():
                move(home / name, backup / name)
            if (root / name).exists():
                move(root / name, home / name)
    except OSError as error:
        for source, target in reversed(moved):
            try:
                shutil.move(str(target), str(source))
            except OSError:
                log.exception("couldn't put %s back; it's in %s", source, target)
        raise UpdateError(f"couldn't put the new files in place ({error}); the old ones are back") from None
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if os.name != "nt" and (home / "start.sh").exists():
        (home / "start.sh").chmod(0o755)
    return version


def refresh_packages(home: Path) -> None:
    """Install what the new engine needs, as start.cmd / start.sh would at their next start."""
    engine = home / "engine"
    wanted, marker = engine / "requirements.txt", engine / ".venv" / "requirements.txt"
    if not (engine / ".venv").is_dir() or not wanted.exists():
        return
    if marker.exists() and marker.read_bytes() == wanted.read_bytes():
        return
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet",
                             "-r", str(wanted)], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise UpdateError(f"installing the new version's Python packages failed: {result.stderr.strip()[-500:]}")
    shutil.copyfile(wanted, marker)


def update(home: Path, release: Release) -> str:
    """Download, check and install `release`; then its packages. Returns the version now installed."""
    if not installed_here(home):
        raise UpdateError(f"this copy doesn't run from {home / 'engine'} (start.cmd / start.sh do that), so it "
                          "can't update itself; download the new version instead")
    archive = download(release, home / "records")
    try:
        version = install(archive, home, release.version)
    finally:
        archive.unlink(missing_ok=True)
    refresh_packages(home)
    return version


def summary(notes: str, lines: int = 6) -> list[str]:
    """The first few lines of a release's notes, for the console."""
    kept = [l.rstrip() for l in notes.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return kept[:lines] + (["…"] if len(kept) > lines else [])


class UpdateService:
    """Looks for a new version now and then, says so in the console, and installs it when asked.

    Asked means: `update` typed in the supervisor's window, `start.cmd update`, or,
    with install_on_server_restart, a server restarting. Installing ends with
    `relaunch()`: the supervisor stops (leaving the servers running) and its
    start loop runs the new version, which picks the servers up again.
    """

    def __init__(self, settings: Settings, bus: EventBus, relaunch: Callable[[], None]) -> None:
        self.settings = settings
        self.config = settings.updates
        self.relaunch = relaunch
        self.pending: Release | None = None
        self.announced: str | None = None
        self.installing = False
        self.can_install = installed_here(settings.home)
        bus.subscribe("server.restarted", self._on_server_restart)
        bus.subscribe("log.online", self._on_server_restart)

    @property
    def launcher(self) -> str:
        return os.environ.get("RECLAIMER_LAUNCHER", "reclaimer-supervisor")

    async def run(self) -> None:
        if not self.config.check:
            log.info("Not looking for updates ([updates] check = false)")
            return
        await asyncio.sleep(3)  # after the start-up lines
        while True:
            await self.check()
            await asyncio.sleep(self.config.check_hours * 3600)

    async def check(self, *, quiet: bool = True) -> Release | None:
        try:
            release = await asyncio.to_thread(latest_release, self.config.repository)
        except UpdateError as error:
            log.log(logging.INFO if quiet else logging.WARNING, "Couldn't look for updates: %s", error)
            return None
        if release is None or not is_newer(release):
            self.pending = None
            if not quiet:
                log.info("Reclaimer Supervisor %s is the newest version", __version__)
            return None
        self.pending = release
        if not quiet or self.announced != release.version:
            self.announce(release)
            self.announced = release.version
        return release

    def announce(self, release: Release) -> None:
        log.warning("Update available: Reclaimer Supervisor %s (this is %s)", release.version, __version__)
        for line in summary(release.notes):
            log.warning("    %s", line)
        log.warning("  What's new: %s", release.page)
        if not self.can_install:
            log.warning("  This copy doesn't run from its own folder's engine/, so it can't update itself; "
                        "download the new version")
        elif self.config.install_on_server_restart:
            log.warning("  It installs itself the next time a server restarts. To install it now, type update "
                        "here and press Enter")
        else:
            log.warning("  To install it, type update here and press Enter (or run: %s update). Your servers keep "
                        "running while it updates", self.launcher)

    def _on_server_restart(self, event: Event) -> None:
        if self.pending and self.config.install_on_server_restart and not self.installing:
            asyncio.ensure_future(self.install(f"{event.server or 'a server'} restarted"))

    async def install(self, why: str) -> bool:
        if self.installing:
            return False
        if not self.can_install:
            log.error("This copy can't update itself: it doesn't run from %s", self.settings.home / "engine")
            return False
        release = self.pending or await self.check(quiet=False)
        if release is None:
            return False
        self.installing = True
        log.warning("Updating to %s (%s). The servers keep running", release.version, why)
        try:
            version = await asyncio.to_thread(update, self.settings.home, release)
        except (UpdateError, OSError, subprocess.SubprocessError) as error:
            log.error("Update failed, still on %s: %s", __version__, error)
            self.installing = False
            return False
        log.warning("Installed %s; starting it", version)
        self.relaunch()
        return True
