"""New releases of the server program (Project Reclaimer itself), and which release a program file is.

Players must be on exactly the server's version, and releases come out often, so a server left behind empties out.
Each server's `auto_update` (its dedicated.toml) is the one switch, and since 0.7.0 the server alone installs releases:
Huragok never downloads a program or puts one in place (plan-0.7.0 section 4.3). With auto_update on, the program looks
at GitHub as it starts and hourly, checks the download against the release's SHA-256 list, and restarts on the new
version; off, it stays on the version it is (a server pinned on purpose).

What Huragok does about it is updates/program.py's: it looks at GitHub itself (latest_program_release), tells which
release each server runs (a program's own version isn't always its release: running_release), and restarts servers
after their game so they install a release as they start.

The newest release is whatever GitHub says is the latest, even when that goes back (a release pulled for a fault).
On Linux a release from 0.10 on has a program of Linux's own for each machine
(`project-reclaimer-dedicated-v<version>-linux-x86_64.tar.gz`, or -aarch64), and on an Apple Silicon Mac one of
macOS's own (`-macos-aarch64.zip`); the checksum GitHub lists for those is the archive's, not the program's. Windows
always has the .exe.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core import web
from ..core.files import write_atomic
from ..core.web import MAX_PROGRAM_DOWNLOAD, UpdateError, parse_version
from .dedicated import cached_version, peek_version, server_program

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings

log = logging.getLogger(__name__)

RELEASES = "ProjectReclaimer/project-reclaimer-releases"
PROGRAM_PREFIX = "project-reclaimer-dedicated"
SUMS = "SHA256SUMS.txt"
ARCHIVE = ".tar.gz"  # Linux's own program comes in one (0.10 on)
MAC_ARCHIVE = ".zip"  # macOS's own program (0.10 on, Apple Silicon only)
MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json", **options: Any) -> bytes:
    """web.fetch with the server program's own size limit (a program is far bigger than Huragok's zip)."""
    options.setdefault("limit", MAX_PROGRAM_DOWNLOAD)
    return web.fetch(url, timeout, accept, **options)


@dataclass(frozen=True)
class ProgramRelease:
    version: str
    page: str
    notes: str
    exe_url: str  # the program's download: the .exe, or from 0.10 Linux's .tar.gz or macOS's .zip
    exe_name: str
    sums_url: str | None = None
    published: float | None = None  # Unix seconds, GitHub's published_at
    exe_size: int | None = None  # the program's size in bytes, as GitHub lists it
    exe_sha256: str | None = None  # the download's SHA-256, as GitHub lists it ("digest"), lower-case hex


def _published(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def native_download() -> str | None:
    """How a release names this machine's own program: "-linux-x86_64.tar.gz" (or -aarch64) on Linux, and
    "-macos-aarch64.zip" on an Apple Silicon Mac. None on Windows (the .exe, always), on an Intel Mac (Project
    Reclaimer has no program for it) and on any other system or machine."""
    if os.name == "nt":
        return None
    machine = MACHINES.get(platform.machine().lower())
    if sys.platform == "darwin":
        return f"-macos-aarch64{MAC_ARCHIVE}" if machine == "aarch64" else None
    if not sys.platform.startswith("linux"):
        return None
    return f"-linux-{machine}{ARCHIVE}" if machine else None


def is_archive(path: Path | str) -> bool:
    """A download that holds the program (Linux's .tar.gz, macOS's .zip) rather than being it (the .exe)."""
    return str(path).lower().endswith((ARCHIVE, MAC_ARCHIVE))


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
    listed = [a for a in data.get("assets") or [] if isinstance(a, dict)]
    assets = {str(a.get("name", "")): str(a.get("browser_download_url", "")) for a in listed}
    exe = next((n for n in assets if n.startswith(PROGRAM_PREFIX) and n.endswith(".exe")), None)
    ending = native_download()
    if ending:  # Linux or a Mac: its own program when the release has one (0.10 on), else the .exe (0.9's)
        exe = next((n for n in assets if n.startswith(PROGRAM_PREFIX) and n.endswith(ending)), exe)
    if exe is None:
        return None
    size = next((a.get("size") for a in listed if str(a.get("name", "")) == exe), None)
    digest = str(next((a.get("digest") for a in listed if str(a.get("name", "")) == exe), None) or "")
    digest = digest[len("sha256:"):].lower() if digest.lower().startswith("sha256:") else ""
    return ProgramRelease(
        version=str(data.get("tag_name") or "").lstrip("v"),
        page=str(data.get("html_url") or f"https://github.com/{repository}/releases"),
        notes=str(data.get("body") or ""),
        exe_url=assets[exe], exe_name=exe, sums_url=assets.get(SUMS),
        published=_published(data.get("published_at")),
        exe_size=size if isinstance(size, int) and size > 0 else None,
        exe_sha256=digest if _is_digest(digest) else None)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# Which release a program file is -------------------------------------------------------------------------------
#
# A program's `--version`, and the version its console gives as Huragok signs in, is the version it was built as,
# which isn't always its release: Project Reclaimer 0.10.1's program says "project-reclaimer 0.10.0 (rcl-engine)"
# (its own updater goes by the release's tag, v0.10.1, built in beside it, so it doesn't update again). So Huragok
# goes by the file: its SHA-256 against the releases' own checksums (GitHub lists one for each download). Each
# release Huragok looks at or downloads, and each a server says it put in place itself, is kept in
# records/program-releases.json: {"releases": {sha256: release}, "says": {sha256: what that file's --version says}}.
# A server's console is taken for its file's release only when it said what that file says: otherwise the process
# running isn't that file (one put in place while it ran, its restart still to come).

IDENTITIES = "program-releases.json"

# Releases whose program gives another version than its release's, known before Huragok has asked GitHub:
# sha256 -> (release, what its --version and console say).
MISLABELLED = {
    "56aa513e298b6b2bda0f74acbfcc6ec1a1c2311a3b83a1038d457e5173b1ac24": ("0.10.1", "0.10.0"),  # the .exe
    "555c4fe81f44e406e97b7fc5a25d47cdc2940ad665df4fad0dd4bb2fe5126aac": ("0.10.1", "0.10.0"),  # linux-x86_64's
}

_HASHES: dict[tuple[str, int, int], str] = {}
_KEPT: dict[str, tuple[int, dict[str, str], dict[str, str]]] = {}  # records file -> (its time, releases, says)
_IDENTITIES_LOCK = threading.Lock()


def _is_digest(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def cached_hash(path: Path) -> str | None:
    """file_hash, once per file as it is (its size and time; a new file is hashed again). None: it can't be read."""
    try:
        info = path.stat()
    except OSError:
        return None
    key = (str(path), info.st_mtime_ns, info.st_size)
    if key not in _HASHES:
        try:
            _HASHES[key] = file_hash(path)
        except OSError:
            return None
    return _HASHES[key]


def _kept(settings: Settings) -> tuple[dict[str, str], dict[str, str]]:
    """(releases, says) as records/program-releases.json has them."""
    path = settings.records / IDENTITIES
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}, {}
    cached = _KEPT.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1], cached[2]
    found: dict[str, str] = {}
    says: dict[str, str] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for digest, version in dict(data.get("releases") or {}).items():
            if isinstance(digest, str) and _is_digest(digest.lower()) and isinstance(version, str) \
                    and readable(version):
                found[digest.lower()] = version
        for digest, label in dict(data.get("says") or {}).items():
            if isinstance(digest, str) and digest.lower() in found and isinstance(label, str) and label:
                says[digest.lower()] = label
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # unreadable: as if empty, and the next release Huragok sees writes it again
    _KEPT[str(path)] = (stamp, found, says)
    return found, says


def known_releases(settings: Settings) -> dict[str, str]:
    """sha256 -> release: the ones built in, and the ones kept in records/."""
    return {**{digest: release for digest, (release, _) in MISLABELLED.items()}, **_kept(settings)[0]}


def known_says(settings: Settings) -> dict[str, str]:
    """sha256 -> what that release's program says it is (--version, its console), where Huragok knows."""
    return {**{digest: said for digest, (_, said) in MISLABELLED.items()}, **_kept(settings)[1]}


def remember_release(settings: Settings, digest: str, version: str, says: str | None = None) -> bool:
    """Keep `digest` as release `version`'s program file (and what it says it is, when known). True when that
    adds something; never raises."""
    digest = (digest or "").lower()
    if not _is_digest(digest) or not readable(version):
        return False
    says = says if says and readable(says) else None
    with _IDENTITIES_LOCK:
        kept, kept_says = _kept(settings)
        if kept.get(digest) == version and (says is None or kept_says.get(digest) == says):
            return False
        kept = {**kept, digest: version}
        if kept_says.get(digest) and kept.get(digest) != version:
            kept_says = {k: v for k, v in kept_says.items() if k != digest}  # another release now: said nothing yet
        kept_says = {**kept_says, **({digest: says} if says else {})}
        path = settings.records / IDENTITIES
        order = sorted(kept, key=lambda each: (parse_version(kept[each]), each))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, json.dumps({"releases": {k: kept[k] for k in order},
                                           "says": {k: kept_says[k] for k in order if k in kept_says}},
                                          indent=2) + "\n")
        except (OSError, UpdateError) as error:
            log.info("Couldn't keep which release a program file is in %s: %s", path, error)
            return False
        _KEPT.pop(str(path), None)
    return True


def remember_file(settings: Settings, path: Path, version: str, says: str | None = None) -> bool:
    """Keep the program file at `path` as release `version`'s. True when that adds something; never raises."""
    digest = cached_hash(path)
    return remember_release(settings, digest, version, says) if digest else False


def learn_says(settings: Settings, path: Path) -> str | None:
    """For a program file Huragok knows the release of but not what it says it is: its --version, asked once and
    kept. Runs the program (a thread's work, never the event loop's). Returns what it says, or None."""
    digest = cached_hash(path)
    release = known_releases(settings).get(digest) if digest else None
    if digest is None or release is None:
        return None
    if digest in known_says(settings):
        return known_says(settings)[digest]
    said = program_version(settings, path)
    if said and readable(said):
        remember_release(settings, digest, release, said)
        return said
    return None


def release_of(settings: Settings, path: Path | None) -> str | None:
    """The release a program file is, by its checksum; None when Huragok doesn't know that file."""
    digest = cached_hash(path) if path is not None else None
    return known_releases(settings).get(digest) if digest else None


def file_version(settings: Settings, path: Path) -> str | None:
    """Which release a program file is: by its checksum when Huragok knows it, else as its `--version` says (which,
    for 0.10.1, is 0.10.0)."""
    return release_of(settings, path) or program_version(settings, path)


def running_release(settings: Settings, path: Path | None, said: str) -> str:
    """The release a server runs whose console said `said` as Huragok signed in: its program file's release when
    Huragok knows the file and that file says exactly that (0.10.1's program says 0.10.0). Otherwise `said`: the file
    isn't known, Huragok hasn't heard what it says yet, or it says something else, so the process running isn't that
    file (one put in place while it ran, its restart still to come). Never runs the program."""
    digest = cached_hash(path) if path is not None else None
    release = known_releases(settings).get(digest) if digest else None
    if release is None or release == said:
        return said
    template = [str(w) for w in settings.backend_options.get("command") or []] \
        if settings.backend == "process" else []
    says = known_says(settings).get(digest) or peek_version(path, template)  # type: ignore[arg-type]
    return release if says == said else said


def program_name(settings: Settings) -> str:
    return str(settings.backend_options.get("executable") or "project-reclaimer.exe") \
        if settings.backend == "process" else "project-reclaimer.exe"


def program_of(settings: Settings, server: ServerConfig) -> Path | None:
    """The program file a server runs: its own folder's, or the one at the root (as the process backend picks)."""
    return server_program(server.folder, settings.home, program_name(settings))


def program_version(settings: Settings, path: Path) -> str | None:
    """A program file's own version, as `--version` says it (through wine on Linux, or [process] command); asked
    once per file as it is. None: it doesn't answer; something that isn't a version: it answered with that."""
    template = [str(w) for w in settings.backend_options.get("command") or []] \
        if settings.backend == "process" else []
    return cached_version(path, template)


def readable(version: str | None) -> bool:
    try:
        parse_version(str(version))
        return version is not None
    except UpdateError:
        return False


def self_updating(settings: Settings) -> set[str]:
    """The servers whose own auto_update installs new releases (auto_update = true in their dedicated.toml)."""
    return {s.name for s in settings.servers if s.auto_update}


def remove_old_files(folder: Path) -> list[Path]:
    """The project-reclaimer.exe.old a self-update leaves, once nothing runs it (the server is stopped)."""
    removed = []
    for old in folder.glob("*.exe.old"):
        try:
            old.unlink()
            removed.append(old)
        except OSError:
            pass  # still running from it: the next stop
    return removed
