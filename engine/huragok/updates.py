"""New versions of the supervisor, from its GitHub releases. Never the game server: that's its own updater.

An update replaces only the program: engine/ (but not engine/.venv), the start
scripts, docs/, README.md and LICENSE. The host's files (settings.toml,
commands.toml, automod.txt, records/, logs/) are never touched, and the program
files it replaces are kept in records/previous-<version on disk>/ (engine/<each
item but .venv>, docs/, start.cmd, start.sh, README.md, LICENSE, as they were).
records/last-update.json says which backup belongs to the version now installed,
and put_back() restores it.

Every move is a rename, never a copy: a folder that can't be renamed (a console
whose current folder is engine/huragok, an antivirus scan of the unpacked release)
stops the install with every rename undone, so a failure never leaves the program
half there. The result is checked (the version file must read the new version)
before it counts, and if the new version's packages can't be installed the old
files go back. A backup is never deleted or refilled.

Nothing is installed unless the host says so (U in the window, `update` typed in
the console without one, or `start.cmd update`), or has set [updates] auto_update,
which installs a new version as soon as it's found (at the start and every
check_hours). While one waits, the window's HURAGOK line says so (status_text), so
it isn't lost among the events. A version whose install failed, or that the start
loop took back out, isn't tried again by itself: only once a newer one is out, or
when asked.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import __version__

if TYPE_CHECKING:
    from .config import Settings
    from .events import Event, EventBus

log = logging.getLogger(__name__)

REPOSITORY = "LittleGroove/Project-Reclaimer-Huragok"
PROGRAM = ("engine", "docs", "start.cmd", "start.sh", "README.md", "LICENSE")  # what an update replaces
KEEP_IN_ENGINE = {".venv"}  # the installed packages stay; start.cmd / start.sh keep them up to date
MAX_DOWNLOAD = 50_000_000  # Huragok's own zip
MAX_PROGRAM_DOWNLOAD = 300_000_000  # the server program (about 25 MB in 2026)
VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)
LAST_UPDATE = "last-update.json"  # in records/: which backup the installed version replaced


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str  # "0.2.0"
    page: str  # the release's page on GitHub, with what changed
    notes: str
    download: str  # the zip to install
    digest: str | None = None  # "sha256:…" when GitHub gives one
    size: int | None = None  # the zip's size in bytes, as GitHub lists it


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.10.2' -> (0, 10, 2). Anything after the numbers (-beta…) is ignored."""
    numbers = re.match(r"v?(\d+(?:\.\d+)*)", text.strip())
    if not numbers:
        raise UpdateError(f"'{text}' isn't a version number")
    return tuple(int(n) for n in numbers.group(1).split("."))


def is_newer(release: Release, current: str = __version__) -> bool:
    return parse_version(release.version) > parse_version(current)


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json", *, limit: int = MAX_DOWNLOAD,
         size: int | None = None) -> bytes:
    """GitHub's answer, whole: one cut short (fewer bytes than it said, or than `size`) is an UpdateError."""
    request = urllib.request.Request(url, headers={
        "Accept": accept, "User-Agent": f"huragok/{__version__}", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            promised = str(response.headers.get("Content-Length") or "").strip()
            data = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"GitHub answered {error.code} {error.reason} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise UpdateError(f"couldn't reach GitHub: {reason}") from None
    except http.client.HTTPException as error:  # IncompleteRead: the connection broke mid-answer
        raise UpdateError(f"the download from {url} broke off ({type(error).__name__}); nothing was changed") from None
    if len(data) > limit:
        raise UpdateError(f"{url} is larger than {limit // 1_000_000} MB")
    expected = size if size is not None else int(promised) if promised.isdigit() else None
    if expected is not None and len(data) != expected:
        raise UpdateError(f"the download from {url} stopped after {len(data)} of {expected} bytes; nothing was "
                          "changed")
    return data


def latest_release(repository: str = REPOSITORY, timeout: float = 10.0) -> Release | None:
    """The newest published release (drafts and pre-releases left out), or None if there's none yet. A release
    whose tag isn't a version number is an UpdateError."""
    try:
        data = json.loads(_get(f"https://api.github.com/repos/{repository}/releases/latest", timeout))
    except UpdateError as error:
        if " 404 " in str(error):
            return None
        raise
    except ValueError:
        raise UpdateError("GitHub's answer wasn't JSON") from None
    tag = str(data.get("tag_name") or "")
    try:
        parse_version(tag)
    except UpdateError:
        raise UpdateError(f"the newest release's tag ({tag or 'none'}) isn't a version number") from None
    assets = [a for a in data.get("assets") or [] if str(a.get("name", "")).endswith(".zip")]
    ours = next((a for a in assets if str(a["name"]).startswith("huragok")), assets[0] if assets else None)
    size = (ours or {}).get("size")
    return Release(
        version=tag.lstrip("v"),
        page=str(data.get("html_url") or f"https://github.com/{repository}/releases"),
        notes=str(data.get("body") or ""),
        download=str(ours["browser_download_url"]) if ours else str(data.get("zipball_url") or ""),
        digest=(ours or {}).get("digest"),
        size=size if isinstance(size, int) and size > 0 else None,
    )


def download(release: Release, folder: Path, timeout: float = 120.0) -> Path:
    if not release.download:
        raise UpdateError(f"release {release.version} has nothing to download")
    data = _get(release.download, timeout, accept="application/octet-stream",
                **({"size": release.size} if release.size else {}))
    if release.digest and release.digest.startswith("sha256:"):
        if hashlib.sha256(data).hexdigest() != release.digest.removeprefix("sha256:"):
            raise UpdateError("the download doesn't match GitHub's checksum; nothing was changed")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"huragok-{release.version}.zip"
    path.write_bytes(data)
    return path


def program_root(extracted: Path) -> Path:
    """The folder in an unpacked release that holds engine/ and the start scripts."""
    for init in sorted(extracted.glob("**/engine/huragok/__init__.py"), key=lambda p: len(p.parts)):
        return init.parents[2]
    raise UpdateError("the download has no engine/huragok in it")


def version_in(root: Path) -> str:
    text = (root / "engine" / "huragok" / "__init__.py").read_text(encoding="utf-8")
    found = VERSION_LINE.search(text)
    if not found:
        raise UpdateError("the download doesn't say which version it is")
    return found.group(1)


def installed_here(home: Path) -> bool:
    """True when this copy runs from home/engine, as start.cmd / start.sh run it; only then can it update itself."""
    return Path(__file__).resolve().parents[1] == (home / "engine").resolve()


@dataclass
class Installed:
    """What install() did: the version now in place, the one it replaced and where those files are kept."""

    version: str
    replaced: str
    backup: Path
    added: list[str] = field(default_factory=list)  # paths (relative to home) the replaced version didn't have


def version_on_disk(home: Path) -> str:
    """The version whose files are in home/engine now (after a failed relaunch it may not be the one running)."""
    try:
        return version_in(home)
    except (OSError, UpdateError):
        return __version__


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _unique(path: Path) -> Path:
    candidate, number = path, 2
    while _exists(candidate):
        candidate = path.with_name(f"{path.name}-{number}")
        number += 1
    return candidate


class _Renames:
    """Renames only (never copy-then-delete, which can stop half way), each one undone in reverse on a failure."""

    def __init__(self) -> None:
        self.done: list[tuple[Path, Path]] = []

    def __call__(self, source: Path, target: Path) -> None:
        if _exists(target):
            raise OSError(f"{target} is in the way")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, target)
        self.done.append((source, target))

    def undo(self) -> list[str]:
        """Every rename reversed; returns what couldn't be put back (empty when all went back)."""
        left: list[str] = []
        for source, target in reversed(self.done):
            try:
                if _exists(source):
                    raise OSError(f"{source} is in the way")
                os.rename(target, source)
            except OSError as error:
                left.append(f"{source} (it is in {target}: {error})")
                log.error("couldn't put %s back; it is in %s (%s)", source, target, error)
        self.done.clear()
        return left


def _program_items(home: Path, root: Path) -> list[tuple[Path, Path | None]]:
    """(the file or folder in home, the release's one or None) for everything an update replaces."""
    engine, new_engine = home / "engine", root / "engine"
    names = {p.name for p in engine.iterdir()} if engine.is_dir() else set()
    names |= {p.name for p in new_engine.iterdir()} if new_engine.is_dir() else set()
    items = [(engine / name, new_engine / name if _exists(new_engine / name) else None)
             for name in sorted(names - KEEP_IN_ENGINE)]
    items += [(home / name, root / name if _exists(root / name) else None) for name in PROGRAM[1:]]
    return items


def _swap_in(home: Path, root: Path, backup: Path, version: str) -> list[str]:
    """Each program file or folder to backup/, the release's in its place, two renames per item; checked after.
    Raises UpdateError with every rename undone (or saying what couldn't be)."""
    rename = _Renames()
    added: list[str] = []
    try:
        (home / "engine").mkdir(exist_ok=True)
        for live, new in _program_items(home, root):
            relative = live.relative_to(home)
            if _exists(live):
                rename(live, backup / relative)
            elif new is not None:
                added.append(relative.as_posix())
            if new is not None:
                rename(new, live)
        installed = version_in(home)
        if parse_version(installed) != parse_version(version):
            raise UpdateError(f"after the swap engine/huragok says {installed}, not {version}")
    except (OSError, UpdateError) as error:
        left = rename.undo()
        why = str(error)
        if isinstance(error, PermissionError) or "WinError 5" in why or "WinError 32" in why:
            why += (" (a window whose current folder is in engine, an editor with a file open, or an antivirus scan "
                    "can hold a folder: close it and try again)")
        if left:
            raise UpdateError(f"couldn't put the new files in place ({why}), and not everything could be put back: "
                              f"{'; '.join(left)}. Huragok's own files are in {backup}: copy them back into {home} "
                              "before starting it") from None
        raise UpdateError(f"couldn't put the new files in place ({why}); nothing was changed") from None
    return added


def install(archive: Path, home: Path, expected: str | None = None) -> str:
    """Put the release in `archive` in place of the program in `home`. Returns its version.

    The replaced files go to records/previous-<the version on disk>/ (one already there is
    renamed with its date, never deleted). Only renames are used, and if one fails every one
    made is undone, so it is all or nothing.
    """
    return install_release(archive, home, expected).version


def install_release(archive: Path, home: Path, expected: str | None = None) -> Installed:
    records = home / "records"
    staging = records / "update"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        try:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(staging)  # strips absolute paths and ".." from member names
        except zipfile.BadZipFile:
            raise UpdateError("the download isn't a zip file") from None
        root = program_root(staging)
        version = version_in(root)
        if expected and parse_version(version) != parse_version(expected):
            raise UpdateError(f"the download says it's version {version}, not {expected}; nothing was changed")
        replaced = version_on_disk(home)
        backup = records / f"previous-{replaced}"
        older: Path | None = None
        if _exists(backup):  # an earlier install over this same version: kept, under its date
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(backup.stat().st_mtime))
            older = _unique(records / f"previous-{replaced}-{stamp}")
            try:
                os.rename(backup, older)
            except OSError:
                older, backup = None, _unique(records / f"previous-{replaced}-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            added = _swap_in(home, root, backup, version)
        except UpdateError:
            if _empty_tree(backup):
                shutil.rmtree(backup, ignore_errors=True)
            if older is not None and not _exists(backup):
                try:
                    os.rename(older, backup)
                except OSError:
                    pass
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if os.name != "nt" and (home / "start.sh").exists():
        (home / "start.sh").chmod(0o755)
    done = Installed(version, replaced, backup, added)
    try:
        (records / LAST_UPDATE).write_text(json.dumps({
            "version": version, "replaced": replaced, "backup": backup.name, "added": added,
            "installed": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        log.warning("couldn't write records/%s (%s); the replaced files are in %s", LAST_UPDATE, error, backup)
    return done


def _empty_tree(folder: Path) -> bool:
    """A folder holding nothing but empty folders (a backup an undone install left)."""
    try:
        return folder.is_dir() and not any(p.is_file() or p.is_symlink() for p in folder.rglob("*"))
    except OSError:
        return False


def last_update(home: Path) -> Installed | None:
    """What records/last-update.json says the latest install replaced, or None."""
    try:
        data = json.loads((home / "records" / LAST_UPDATE).read_text(encoding="utf-8"))
        return Installed(str(data["version"]), str(data["replaced"]), home / "records" / str(data["backup"]),
                         [str(p) for p in data.get("added") or []])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def put_back(home: Path, installed: Installed) -> str:
    """The files an install replaced (its backup folder) back in place of what it put there; what it added is
    removed. For a new version that can't install its packages, or doesn't start. Returns the version back."""
    backup = installed.backup
    if not (backup / "engine").is_dir():
        raise UpdateError(f"{backup} doesn't hold Huragok's files; nothing was changed")
    undone = _unique(home / "records" / f"undone-{installed.version}")
    rename = _Renames()
    try:
        names = sorted({f"engine/{p.name}" for p in (backup / "engine").iterdir()}
                       | {n for n in PROGRAM[1:] if _exists(backup / n)} | set(installed.added))
        for name in names:
            live, kept = home / name, backup / name
            if _exists(live):
                rename(live, undone / name)
            if _exists(kept):
                rename(kept, live)
        back = version_in(home)
    except (OSError, UpdateError) as error:
        left = rename.undo()
        raise UpdateError(f"couldn't put {backup.name} back ({error})" + (
            f"; not everything could be undone either: {'; '.join(left)}" if left else "; nothing was changed")) \
            from None
    shutil.rmtree(undone, ignore_errors=True)
    if _empty_tree(backup):
        shutil.rmtree(backup, ignore_errors=True)
    pointer = home / "records" / LAST_UPDATE
    if (found := last_update(home)) is not None and found.backup == backup:
        pointer.unlink(missing_ok=True)
    return back


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
        installed = install_release(archive, home, release.version)
    finally:
        archive.unlink(missing_ok=True)
    try:
        refresh_packages(home)
    except (UpdateError, OSError, subprocess.SubprocessError) as error:
        try:
            put_back(home, installed)
        except UpdateError as undo:
            raise UpdateError(f"{error}; and the old version couldn't be put back: {undo}") from None
        raise UpdateError(f"{error}; the old version's files are back in place") from None
    return installed.version


def summary(notes: str, lines: int = 6) -> list[str]:
    """The first few lines of a release's notes, for the console."""
    kept = [l.rstrip() for l in notes.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    return kept[:lines] + (["…"] if len(kept) > lines else [])


class UpdateService:
    """Looks for a new version now and then, says so, and installs it when asked, or as soon as it's found with
    [updates] auto_update.

    Asked means: U in the window, `update` typed in the console without one, or `start.cmd update`. Installing
    ends with `relaunch()`: the supervisor stops (leaving the servers running) and its start loop runs the new
    version, which picks the servers up again. 0.4.1 and earlier installed by itself only when a server restarted
    (install_on_server_restart), which a server that keeps failing to start never does.
    """

    def __init__(self, settings: Settings, bus: EventBus, relaunch: Callable[[], None]) -> None:
        self.settings = settings
        self.config = settings.updates
        self.relaunch = relaunch
        self.pending: Release | None = None
        self.announced: str | None = None
        self.installing = False
        self.can_install = installed_here(settings.home)
        self.last_error = ""  # why the last look at GitHub didn't get an answer ("" when it did)
        self.failed: str | None = None  # a version whose install failed: not tried again by itself
        self.last_failure = ""
        self._remember_failed_start()

    def _remember_failed_start(self) -> None:
        """records/failed-update.json, from the start loop: a version that was installed but didn't start, so the
        loop put this one back. It counts as failed, so a server restarting doesn't install it again (and again);
        a person (update) or a newer release still can. Once that version itself runs, the note goes."""
        path = self.settings.records / "failed-update.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        version = str(data.get("version") or "") if isinstance(data, dict) else ""
        if not version or version == __version__:
            path.unlink(missing_ok=True)
            return
        self.failed = version
        self.last_failure = f"it was installed but didn't start (exit code {data.get('exit_code')}), so {__version__} " \
                            "was put back"
        log.error("Huragok %s %s. It isn't installed again by itself; to try it again: U in the window (or run: %s "
                  "update)", version, self.last_failure, self.launcher)

    @property
    def launcher(self) -> str:
        return os.environ.get("HURAGOK_LAUNCHER", "huragok")

    async def run(self) -> None:
        if not self.config.check:
            log.info("Not looking for updates ([updates] check = false)")
            return
        await asyncio.sleep(3)  # after the start-up lines
        while True:
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - one bad look must not end the looking
                log.exception("Looking for a new Huragok went wrong; it looks again in %g hour(s)",
                              self.config.check_hours)
            await asyncio.sleep(self.config.check_hours * 3600)

    async def check(self, *, quiet: bool = True) -> Release | None:
        try:
            release = await asyncio.to_thread(latest_release, self.config.repository)
            newer = release is not None and is_newer(release)
        except UpdateError as error:
            self.last_error = str(error)
            log.log(logging.INFO if quiet else logging.WARNING, "Couldn't look for updates: %s", error)
            return None
        self.last_error = ""
        if release is None or not newer:
            self.pending = None
            if not quiet:
                log.info("Huragok %s is the newest version", __version__)
            return None
        self.pending = release
        if not quiet or self.announced != release.version:
            self.announce(release)
            self.announced = release.version
        if self.config.auto_update and self.can_install and not self.installing and self.failed != release.version:
            asyncio.ensure_future(self.install("auto_update is on", automatic=True))
        return release

    def status_text(self) -> str:
        """For the window's HURAGOK line while a new version waits (it stays there, where the events scroll by)."""
        release = self.pending
        if release is None:
            return ""
        if self.installing:
            return f"installing Huragok {release.version}"
        if self.failed == release.version:
            return f"Huragok {release.version} didn't install: U tries again"
        if not self.can_install:
            return f"Huragok {release.version} is out (this copy can't update itself)"
        return f"Huragok {release.version} is out (U)"

    def announce(self, release: Release) -> None:
        log.warning("Update available: Huragok %s (this is %s)", release.version, __version__)
        for line in summary(release.notes):
            log.warning("    %s", line)
        log.warning("  What's new: %s", release.page)
        if not self.can_install:
            log.warning("  This copy doesn't run from its own folder's engine/, so it can't update itself; "
                        "download the new version")
        elif self.config.auto_update and self.failed != release.version:
            log.warning("  auto_update is on: it installs now. Your servers keep running while it updates")
        else:
            log.warning("  To install it: U in the window (or run: %s update; without the window, type update "
                        "here). Your servers keep running while it updates. To have new versions install "
                        "themselves: [updates] auto_update = true", self.launcher)

    async def install(self, why: str, *, automatic: bool = False) -> bool:
        """Download, check and install the newest release, then relaunch. automatic (a server restarting):
        not for a version that failed before."""
        if self.installing:
            return False
        if not self.can_install:
            log.error("This copy can't update itself: it doesn't run from %s", self.settings.home / "engine")
            return False
        release = self.pending or await self.check(quiet=False)
        if release is None or (automatic and self.failed == release.version):
            return False
        self.installing = True
        log.warning("Updating to %s (%s). The servers keep running", release.version, why)
        try:
            version = await asyncio.to_thread(update, self.settings.home, release)
        except (UpdateError, OSError, subprocess.SubprocessError) as error:
            self.failed, self.last_failure = release.version, str(error)
            log.error("Update failed, still on %s: %s", __version__, error)
            if self.config.auto_update:
                log.warning("  Huragok doesn't try %s again by itself; it tries a newer release when one is out. "
                            "To try %s again: U in the window (or run: %s update)", release.version,
                            release.version, self.launcher)
            self.installing = False
            return False
        self.failed, self.last_failure = None, ""
        log.warning("Installed %s; starting it", version)
        self.relaunch()
        return True
