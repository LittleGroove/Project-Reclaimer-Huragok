"""Every version of a host's file that Huragok keeps, with where it came from: a playlist before and after each
change Huragok makes to it, and a change made by hand, found the next time Huragok looks.

    records/<kind>/<owner>/<file name>/<YYYYMMDD-HHMMSS>[-n]/<file name>   the file as it was
                                                              /about.json   when, how it was made, where from

`kind` is "playlists", `owner` a server's slug. Each version is a folder of its own, so the window and a second
window keeping versions at once never write the same file; nothing here is thrown away by itself (they're small).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .files import write_atomic

ABOUT = "about.json"
FIRST = "as Huragok first found it"
BY_HAND = "changed outside Huragok"


@dataclass(frozen=True)
class Version:
    copy: Path  # the kept file
    time: float
    how: str  # "saved in G", "labels written for 0.9.18 as it started", "changed outside Huragok", "from package x"
    source: str = ""  # where it came from: another server, a package, a version put back
    sha256: str = ""
    draft: bool = False  # never written to the file (a draft left unsaved): not what the file was

    @property
    def stamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.time))


def _folder(records: Path, kind: str, owner: str, name: str) -> Path:
    return records / kind / owner / name


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def versions(records: Path, kind: str, owner: str, name: str) -> list[Version]:
    """The kept versions of one file, newest first. A version whose about.json can't be read is left out."""
    folder = _folder(records, kind, owner, name)
    found: list[Version] = []
    if not folder.is_dir():
        return found
    for entry in folder.iterdir():
        try:
            about = json.loads((entry / ABOUT).read_text(encoding="utf-8"))
            found.append(Version(entry / name, float(about["time"]), str(about.get("how") or ""),
                                 str(about.get("from") or ""), str(about.get("sha256") or ""),
                                 bool(about.get("draft"))))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(found, key=lambda v: (v.time, v.copy.parent.name), reverse=True)


def keep(records: Path, kind: str, owner: str, path: Path, how: str, source: str = "", *,
         draft: bool = False) -> Version | None:
    """The file as it is now, kept with how it was made, unless the newest kept version of the file is the same
    (None then, and when there's no file). A draft is compared with the newest draft. Raises OSError when it can't
    be written."""
    digest = _digest(path)
    if digest is None:
        return None
    kept = [v for v in versions(records, kind, owner, path.name) if v.draft == draft]
    if kept and kept[0].sha256 == digest:
        return None
    folder = _folder(records, kind, owner, path.name)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    folder.mkdir(parents=True, exist_ok=True)
    target, number = folder / stamp, 2
    while True:
        try:
            target.mkdir()  # taken in one step: two windows keeping a version in the same second get two folders
            break
        except FileExistsError:
            target, number = folder / f"{stamp}-{number}", number + 1
    shutil.copy2(path, target / path.name)
    write_atomic(target / ABOUT, json.dumps({"time": now, "how": how, "from": source, "sha256": digest,
                                             "file": str(path), **({"draft": True} if draft else {})},
                                            indent=2) + "\n")
    return Version(target / path.name, now, how, source, digest, draft)


def seen(records: Path, kind: str, owner: str, path: Path) -> Version | None:
    """Before Huragok changes a file, and when it lists them: the file as it is, kept when it isn't the newest
    version kept (the first time Huragok sees it, or after someone changed it by hand). Never raises."""
    try:
        how = BY_HAND if any(not v.draft for v in versions(records, kind, owner, path.name)) else FIRST
        return keep(records, kind, owner, path, how)
    except OSError:
        return None


def keep_text(records: Path, kind: str, owner: str, name: str, text: str, how: str, source: str = "") -> Version | None:
    """Text kept as a version of the file `name` that was never written there (a draft left unsaved)."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / name
        path.write_text(text, encoding="utf-8")
        return keep(records, kind, owner, path, how, source, draft=True)


def read(version: Version) -> str:
    """A kept version's text. Raises OSError."""
    return version.copy.read_text(encoding="utf-8")


__all__ = ["ABOUT", "FIRST", "BY_HAND", "Version", "versions", "keep", "seen", "keep_text", "read"]
