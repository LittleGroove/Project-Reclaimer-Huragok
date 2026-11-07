"""New releases of the server program (Project Reclaimer itself).

Players must be on exactly the server's version, and releases have come out daily,
so a server left behind empties out. Each server's `auto_update` (its dedicated.toml)
is the one switch:

- **On: the server updates itself, and Huragok makes sure it did.** The program
  looks at GitHub as it starts and hourly, checks the download against the release's
  SHA-256 list, and restarts its game server on the new version at once, mid-game.
  The `dedicated` process Huragok holds stays up through it (tested 2026-10-18,
  0.9.15 -> 0.9.18: back in 3 s). Huragok looks at GitHub as often (hourly) and
  watches each of these servers:
  - it says when one updated itself, and with [server_updates] follow restarts the
    others after their game, so they pick it up as they start instead of within the
    hour;
  - after an update the `dedicated` it holds still runs from the old file
    (project-reclaimer.exe.old), so once nobody is on it is restarted on the new one
    and the old file removed;
  - **when the update didn't happen** (the console says "Automatic update failed", or
    the server is still on an older version OVERDUE minutes after the release came
    out and it had its look), Huragok installs it instead, with [server_updates]
    fallback: download, check, and swap the file while the server is stopped after
    its game;
  - **when its program is older than its dedicated.toml** it can't even start (the
    program reads its settings before it looks for releases: 0.9.15 stops at 0.9.20's
    `auto_reload`), so it can't update itself either. A start that fails on a setting
    the program doesn't know is checked against the program's `--version`: older than
    the newest release, and the newest is put in place before the next try; already
    the newest, and the log calls that line a mistake.
- **Off: Huragok tells you.** It compares the release with the version each running
  server reports; when asked (U, `update --program`, or [server_updates] install) it
  downloads `project-reclaimer-dedicated-v<version>.exe`, checks it against
  SHA256SUMS.txt, puts it in place of those servers' copies (the old ones kept in
  records/previous-program/) and restarts each after its game, or now.

A running program file can be renamed on Windows, not overwritten: the new file is
written next to it first (<name>.new), the old file is moved aside, then the new one
renamed into place, so a crash never leaves half a program; when one copy of several
can't be replaced, the ones already replaced are put back.

On Linux a release from 0.10 on has a program of Linux's own for each machine
(`project-reclaimer-dedicated-v<version>-linux-x86_64.tar.gz`, or -aarch64), which runs
without wine: that is what's downloaded and checked, its `project-reclaimer` unpacked
(that one file, by that name) into a temporary folder beside where it goes, and put in
place as above; 0.9's Windows program there goes with the old files. A release without
one (0.9) is the .exe, as before. Windows always gets the .exe.

The newest release is whatever GitHub says is the latest, even when that goes back (a
release pulled for a fault): its download, take-overs and jobs are dropped. A swap that
keeps failing (an antivirus holding the file, say) is tried again after 1, 4 and 12
hours, then left with a line on the dashboard until someone presses U.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import platform
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterator

from ..core import web
from ..core.web import MAX_PROGRAM_DOWNLOAD, UpdateError, parse_version
from .dedicated import NATIVE, cached_version, find_executable, native_name, server_program, windows_program

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings

log = logging.getLogger(__name__)

RELEASES = "ProjectReclaimer/project-reclaimer-releases"
PROGRAM_PREFIX = "project-reclaimer-dedicated"
SUMS = "SHA256SUMS.txt"
ARCHIVE = ".tar.gz"  # Linux's own program comes in one (0.10 on)
MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
MAX_UNPACKED = 1 << 30  # the program unpacked (85 MB in 0.10.0)


def _get(url: str, timeout: float, accept: str = "application/vnd.github+json", **options: Any) -> bytes:
    """web.fetch with the server program's own size limit (a program is far bigger than Huragok's zip)."""
    options.setdefault("limit", MAX_PROGRAM_DOWNLOAD)
    return web.fetch(url, timeout, accept, **options)


@dataclass(frozen=True)
class ProgramRelease:
    version: str
    page: str
    notes: str
    exe_url: str  # the program's download: the .exe, or on Linux from 0.10 its .tar.gz
    exe_name: str
    sums_url: str | None = None
    published: float | None = None  # Unix seconds, GitHub's published_at
    exe_size: int | None = None  # the program's size in bytes, as GitHub lists it


def _published(text: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def linux_download() -> str | None:
    """How a release names its Linux program for this machine ("-linux-x86_64.tar.gz"); None on Windows (the .exe,
    always) and on any other system or machine."""
    if os.name == "nt" or not sys.platform.startswith("linux"):
        return None
    machine = MACHINES.get(platform.machine().lower())
    return f"-linux-{machine}{ARCHIVE}" if machine else None


def is_archive(path: Path | str) -> bool:
    return str(path).lower().endswith(ARCHIVE)


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
    ending = linux_download()
    if ending:  # Linux: its own program when the release has one (0.10 on), else the .exe through wine (0.9)
        exe = next((n for n in assets if n.startswith(PROGRAM_PREFIX) and n.endswith(ending)), exe)
    if exe is None:
        return None
    size = next((a.get("size") for a in listed if str(a.get("name", "")) == exe), None)
    return ProgramRelease(
        version=str(data.get("tag_name") or "").lstrip("v"),
        page=str(data.get("html_url") or f"https://github.com/{repository}/releases"),
        notes=str(data.get("body") or ""),
        exe_url=assets[exe], exe_name=exe, sums_url=assets.get(SUMS),
        published=_published(data.get("published_at")),
        exe_size=size if isinstance(size, int) and size > 0 else None)


def parse_sums(text: str) -> dict[str, str]:
    """SHA256SUMS.txt: `<hex>  <name>` (or `*<name>`) per line -> name -> hex."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            found[parts[1].lstrip("*").strip()] = parts[0].lower()
    return found


def download_program(release: ProgramRelease, folder: Path, timeout: float = 600.0) -> Path:
    """The program, verified against the release's checksums when it has them (and its size, when GitHub lists
    it: a download cut short is never put in place). Returns the file."""
    data = _get(release.exe_url, timeout, accept="application/octet-stream",
                **({"size": release.exe_size} if release.exe_size else {}))
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


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


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


def installed_programs(settings: Settings, *, skip_self_updating: bool = False) -> list[Path]:
    """Every copy of the program the servers run: the root's, then each server folder's. With skip_self_updating,
    not the copies only servers that update themselves run."""
    name = program_name(settings)
    skipped = self_updating(settings) if skip_self_updating else set()
    folders = [settings.home, *(folder for folder in settings.server_folders
                                if any(s.folder == folder and s.name not in skipped for s in settings.servers))]
    found: list[Path] = []
    for folder in folders:
        program = find_executable(folder, name)
        if program is not None and program.resolve() not in [p.resolve() for p in found]:
            found.append(program)
    return found


def as_new_as(settings: Settings, path: Path, version: str) -> str | None:
    """The copy's own version when it is `version` or newer (a pre-release, say), so putting `version` in its
    place would gain nothing or go back; also what it said when that isn't a version this can read (unknown is
    never taken for older). None when it is older, or doesn't answer `--version` at all (an old program)."""
    theirs = program_version(settings, path)
    if theirs is None:
        return None
    if not readable(theirs):
        return theirs
    return theirs if parse_version(theirs) >= parse_version(version) else None


def unpack_program(archive: Path, folder: Path) -> Path:
    """Linux's own program from a release's .tar.gz, written to folder/project-reclaimer: that one file (a plain
    file called project-reclaimer, the outermost), by that name, never at a path the archive gives (no ../, no
    links). Returns it, runnable."""
    import tarfile

    folder.mkdir(parents=True, exist_ok=True)
    target = folder / NATIVE
    try:
        with tarfile.open(archive, "r:gz") as tar:
            found = [m for m in tar.getmembers() if m.isreg() and PurePosixPath(m.name).name == NATIVE]
            if not found:
                raise UpdateError(f"{archive.name} has no {NATIVE} in it; nothing was changed")
            member = min(found, key=lambda m: len(PurePosixPath(m.name).parts))
            if member.size > MAX_UNPACKED:
                raise UpdateError(f"{archive.name}'s {NATIVE} is {member.size} bytes, more than a program can be; "
                                  "nothing was changed")
            source = tar.extractfile(member)
            if source is None:
                raise UpdateError(f"{archive.name}'s {NATIVE} can't be read; nothing was changed")
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
    except (tarfile.TarError, EOFError) as error:
        raise UpdateError(f"{archive.name} can't be unpacked ({error}); nothing was changed") from None
    target.chmod(0o755)
    return target


@contextmanager
def _program_file(downloaded: Path, beside: Path) -> Iterator[Path]:
    """The program to put in place: the download itself (an .exe), or Linux's own unpacked from a .tar.gz into a
    temporary folder in `beside` (where it goes: the same drive), which goes afterwards."""
    if not is_archive(downloaded):
        yield downloaded
        return
    folder = beside / f".huragok-unpacked-{time.time_ns()}"
    try:
        yield unpack_program(downloaded, folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _target_for(settings: Settings, target: Path, program: Path) -> Path:
    """Where `program` goes for the copy at `target`: there, or for Linux's own program in place of 0.9's Windows one,
    beside it under its own name (project-reclaimer); the Windows one goes with the old files."""
    if program.suffix.lower() == ".exe" or not windows_program(target):
        return target
    return target.with_name(native_name(program_name(settings)))


def _staged(target: Path) -> Path:
    return target.with_name(target.name + ".new")


def _put_in_place(source: Path, target: Path, keep: Path | None, *, link_from: Path | None = None,
                  replaces: Path | None = None) -> None:
    """`source` at `target`, never half written: copied (or hard-linked) to <target>.new first, the old file moved
    to `keep` (a running program can be renamed, not overwritten), then <target>.new renamed into place. On an
    error the old file is back and the .new one gone. replaces: the old file when it isn't at `target` (0.9's
    Windows program, which Linux's own takes over from)."""
    replaced = replaces or target
    staged = _staged(target)
    staged.unlink(missing_ok=True)
    try:
        if link_from is not None:
            try:
                os.link(link_from, staged)
            except OSError:
                shutil.copy2(source, staged)
        else:
            shutil.copy2(source, staged)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    _clear_old_asides(target)
    old: Path | None = None  # where the old file is now, to put it back on an error
    try:
        if keep is not None and replaced.exists():
            old = _move_aside(replaced, keep)
        os.replace(staged, target)
    except OSError:
        staged.unlink(missing_ok=True)
        if old is not None and not replaced.exists():
            _move_back(old, replaced)
        raise


def _other_drive(error: OSError) -> bool:
    """A rename refused because it would cross drives (Windows) or file systems (Linux)."""
    return error.errno == errno.EXDEV or getattr(error, "winerror", None) == 17  # ERROR_NOT_SAME_DEVICE


def _move_aside(target: Path, keep: Path) -> Path:
    """The old program moved to `keep`. With the servers on another drive than Huragok (SERVERS on D:, Huragok on C:)
    a rename can't get there, and a running program can only be renamed, never copied away and deleted: it's renamed
    beside itself, copied to `keep`, and that renamed file removed when nothing runs it any more (now, or at the next
    update). Before, every program update of those servers failed (audit 2026-11, 3.3). Returns where it is now."""
    keep.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(target, keep)
        return keep
    except OSError as error:
        if not _other_drive(error):
            raise
    aside = target.with_name(f".{target.name}.previous-{time.time_ns()}")
    os.replace(target, aside)
    try:
        shutil.copy2(aside, keep)
    except OSError:
        os.replace(aside, target)
        raise
    try:
        aside.unlink()
    except OSError:
        return aside  # a running server's program: it goes at the next update (_clear_old_asides)
    return keep


def _move_back(old: Path, target: Path) -> None:
    try:
        os.replace(old, target)
    except OSError as error:
        if not _other_drive(error):
            raise
        shutil.copy2(old, target)


def _clear_old_asides(target: Path) -> None:
    """Old programs left beside a server's by _move_aside, which a server was running then."""
    for leftover in target.parent.glob(f".{target.name}.previous-*"):
        try:
            leftover.unlink()
        except OSError:
            pass


def install_program(settings: Settings, downloaded: Path, version: str) -> list[Path]:
    """Put `downloaded` in place of every copy Huragok looks after (not those of servers that update themselves),
    but never over one that is that version or newer already. The old ones go to records/previous-program/.
    All or nothing: when one copy can't be replaced, the ones already done are put back."""
    targets = installed_programs(settings, skip_self_updating=True)
    if not targets:
        raise UpdateError("no server program to replace: none in the server folders or at the root")
    kept = {target: theirs for target in targets if (theirs := as_new_as(settings, target, version))}
    for target, theirs in kept.items():
        if not readable(theirs):
            log.info("Left %s as it is: it answers --version with '%s', which isn't a version Huragok can compare "
                     "with %s", target, theirs, version)
        else:
            log.info("Left %s as it is: it is Project Reclaimer %s%s", target, theirs,
                     " already" if parse_version(theirs) == parse_version(version) else f", newer than {version}")
    targets = [target for target in targets if target not in kept]
    if not targets:
        raise UpdateError(f"every copy is Project Reclaimer {version} or newer already; nothing was changed")
    backup = settings.records / "previous-program" / time.strftime("%Y-%m-%d-%H%M%S")
    first: Path | None = None
    replaced: list[tuple[Path, Path, Path]] = []  # (where the new one is, the old file's place, where it is now)
    try:
        with _program_file(downloaded, targets[0].parent) as program:
            for target in targets:
                new = _target_for(settings, target, program)
                keep = backup / target.parent.name / target.name
                try:
                    _put_in_place(program, new, keep, link_from=first, replaces=target)
                except OSError as error:
                    left = []
                    if not replaced and backup.is_dir() and not any(p.is_file() for p in backup.rglob("*")):
                        shutil.rmtree(backup, ignore_errors=True)  # nothing was set aside: no empty set for Clean up
                    for done, place, old in reversed(replaced):  # all or nothing: the copies already replaced go back
                        try:
                            _move_back(old, place)  # copied back when it's on another drive: a rename can't get there (audit 2026-11, 7.8)
                            if done != place:
                                done.unlink(missing_ok=True)  # Linux's own program, where a Windows one was
                        except OSError as undo:
                            left.append(f"{done} (its old file is in {old}: {undo})")
                    if left:
                        raise UpdateError(f"couldn't put the new program at {new} ({error}); and these copies are "
                                          f"{version} now, the others still the old one: {'; '.join(left)}") from None
                    raise UpdateError(f"couldn't put the new program at {new}: {error}; nothing was changed") from None
                first = first or new
                replaced.append((new, target, keep))
    except OSError as error:  # unpacking it
        raise UpdateError(f"couldn't unpack the new program: {error}; nothing was changed") from None
    (backup / "version.txt").write_text(f"replaced by {version}\n", encoding="utf-8")
    log.info("Server program %s installed in %d place(s); the old files are in %s", version, len(replaced), backup)
    return [new for new, _, _ in replaced]


def swap_program(settings: Settings, server: ServerConfig, downloaded: Path, version: str) -> str:
    """With the server stopped: put `downloaded` in place of the program file it runs (the old one kept in
    records/previous-program/). A root copy other servers run too stays as it is: the server gets its own."""
    name = program_name(settings)
    target = program_of(settings, server)
    with _program_file(downloaded, server.folder) as program:
        if target is not None and target.is_file() and file_hash(target) == file_hash(program):
            return f"its program file is Project Reclaimer {version} already"
        if target is None or (target.parent != server.folder and any(
                other is not server and program_of(settings, other) == target for other in settings.servers)):
            # its own copy from now on, found before the root's
            target = server.folder / (name if program.suffix.lower() == ".exe" else native_name(name))
        new = _target_for(settings, target, program)
        backup = settings.records / "previous-program" / time.strftime("%Y-%m-%d-%H%M%S") / server.folder.name
        had = target.exists()
        try:
            _put_in_place(program, new, backup / target.name if had else None, replaces=target)
        except OSError as error:
            raise UpdateError(f"couldn't put the new program at {new}: {error}; the old one is as it was") from None
        target = new
    if had:
        (backup / "version.txt").write_text(f"replaced by {version}\n", encoding="utf-8")
    remove_old_files(server.folder)
    return f"Project Reclaimer {version} put in place at {target}" + (f"; the old one is in {backup}" if had else "")


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


