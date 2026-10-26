"""The lock: one Huragok per folder, records/huragok.pid.

It names the process that holds the folder ({pid, create_time, version}), made in one step so two Huragoks
started at once can't both win, and checked against the process's start time so a reused process ID never counts.
While Huragok itself starts again (an update, a crash) its start loop holds it (`launcher`), and the Huragok it
starts takes it over and hands it back as it ends (`launched_by`). Every version since 0.4.0 reads and writes it the
same way: a 0.4.x start loop supervises later versions until its window is closed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil

from .. import __version__

if TYPE_CHECKING:
    from .settings import Settings


LOCK_FILE = "huragok.pid"


OLD_LOCK_FILE = "supervisor.pid"  # 0.2.1's name for it


class AlreadyRunning(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(f"process {pid}")
        self.pid = pid


def process_alive(pid: int, create_time: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - create_time) < 1
    except psutil.Error:
        return False


def read(settings: Settings, name: str = LOCK_FILE) -> dict:
    try:
        lock = json.loads((settings.records / name).read_text(encoding="utf-8"))
        return lock if isinstance(lock, dict) else {}
    except (OSError, ValueError):
        return {}


def live_pid(lock: Any) -> int | None:
    """The process a lock names, if that very process is still running (a reused process ID doesn't count)."""
    try:
        pid, created = int(lock["pid"]), float(lock["create_time"])
    except (KeyError, TypeError, ValueError):
        return None
    return pid if process_alive(pid, created) else None


def running_supervisor(settings: Settings) -> int | None:
    """The process ID of a Huragok already running from this folder, if any: records/huragok.pid, or 0.2.1's
    records/supervisor.pid (its window may still be open beside a newer copy). While Huragok itself starts again
    (an update, a crash) the lock names its start loop, which holds the folder for it."""
    for name in (LOCK_FILE, OLD_LOCK_FILE):
        pid = live_pid(read(settings, name))
        if pid is not None and pid != os.getpid():
            return pid
    return None


def _text(**extra: Any) -> str:
    return json.dumps({"pid": os.getpid(), "create_time": psutil.Process().create_time(), "version": __version__,
                       **extra})


def _create(path: Path, text: str) -> bool:
    """Make the lock with its content in one step; False when there's one already. It's written to a file of its
    own and hard-linked into place, so nobody can read it empty and take it for one left behind."""
    temp = path.with_name(f"{path.name}.{os.getpid()}-{time.monotonic_ns()}.tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        os.link(temp, path)
        return True
    except FileExistsError:
        return False
    except OSError:  # no hard links on this drive (FAT, some network folders): create it alone, then fill it
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        return True
    finally:
        temp.unlink(missing_ok=True)


def _replace(path: Path, text: str) -> bool:
    """Rewrite a lock we hold, in one step (a reader never sees it half written)."""
    temp = path.with_name(f"{path.name}.{os.getpid()}-{time.monotonic_ns()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        for _ in range(40):
            try:
                os.replace(temp, path)
                return True
            except PermissionError:  # Windows: someone is reading it this moment
                time.sleep(0.05)
    except OSError:
        pass
    temp.unlink(missing_ok=True)
    return False


def take(settings: Settings, *, launcher: bool = False) -> Path:
    """Become the one Huragok of this folder, or raise AlreadyRunning. Two started at once can't both win.

    launcher: the start loop's own claim, held while Huragok itself starts again (an update, a crash) so that
    nothing else takes the folder meanwhile. The Huragok it starts takes the lock over from it, and hands it back
    as it ends.
    """
    path = settings.records / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if (old := live_pid(read(settings, OLD_LOCK_FILE))) is not None and old != os.getpid():
        raise AlreadyRunning(old)  # a 0.2.1 window, still open
    text = _text(launcher=True) if launcher else _text()
    for _ in range(50):
        if _create(path, text):
            return path
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            time.sleep(0.05)
            continue
        try:
            lock = json.loads(raw)
        except ValueError:
            lock = None
        if not isinstance(lock, dict) or not lock:
            # Empty or half written: on a drive without hard links, someone is making it this moment.
            try:
                young = time.time() - path.stat().st_mtime < 10
            except OSError:
                young = False
            if young:
                time.sleep(0.05)
                continue
        else:
            pid = live_pid(lock)
            if pid == os.getpid():
                return path  # ours already: the start loop, after Huragok handed it back
            if pid is not None:
                if lock.get("launcher") and not launcher and started_by(pid):
                    # The start loop that started this process holds it for us: take it over, to hand back.
                    _replace(path, _text(launched_by=lock))
                    return path
                raise AlreadyRunning(pid)
        # Left by a Huragok that's gone. Take it away under a name of our own, then make sure what was taken is
        # what was judged: another starter may have put a fresh one there in between, and it goes back.
        aside = path.with_name(f"{path.name}.{os.getpid()}.stale")
        try:
            os.replace(path, aside)
        except FileNotFoundError:
            continue
        except OSError:
            time.sleep(0.05)
            continue
        try:
            taken = aside.read_bytes()
        except OSError:
            taken = raw
        if taken != raw:
            try:
                os.link(aside, path)
            except FileExistsError:
                pass  # a third starter got there first; its claim stands
            except OSError:
                if not path.exists():
                    os.replace(aside, path)
        aside.unlink(missing_ok=True)
    raise AlreadyRunning(running_supervisor(settings) or 0)


def started_by(pid: int) -> bool:
    """`pid` started this process: its parent, or its grandparent (on Windows a venv's python.exe is a small
    launcher that starts the real Python as its own child)."""
    try:
        return pid in [parent.pid for parent in psutil.Process().parents()[:3]]
    except psutil.Error:
        return pid == os.getppid()


def release(path: Path) -> None:
    """Give the lock up, if it's still ours: back to the start loop that lent it (Huragok is starting again), or
    gone."""
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(lock, dict) or lock.get("pid") != os.getpid():
        return
    lent = lock.get("launched_by")
    if isinstance(lent, dict) and live_pid(lent) is not None and _replace(path, json.dumps(lent)):
        return
    path.unlink(missing_ok=True)
