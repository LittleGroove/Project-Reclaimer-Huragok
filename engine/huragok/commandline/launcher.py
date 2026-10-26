"""The start loop: what start.cmd and start.sh run. It runs Huragok in a process of its own and starts it again when
it ends unexpectedly, starts a newly installed version, and puts the version before back when a new one doesn't
start. It never touches a server, and stays on the code it started with whatever an update puts on disk.

    exit RELAUNCH (10)   a new version is installed: start it
    a new version ends within FAILED_START_SECONDS: put back the files the update replaced, start the old one
    a settings problem   the window shows it and stops; with --no-menu it's tried again every 5 minutes
    any other end        a crash: start Huragok again after CRASH_WAITS, forever
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..core import lock, mailbox
from ..core.files import mtime
from ..core.lock import AlreadyRunning
from ..core.settings import Settings, load
from ..core.tables import ConfigError
from . import common
from .common import ALREADY_RUNNING, DELIBERATE, FAILED, LAUNCHER, RELAUNCH, SETTINGS_PROBLEM
from .shell import console as out


CRASH_WAITS = (5, 15, 30, 60, 300)  # seconds before starting Huragok again after each crash in a row; the last repeats


GOOD_RUN_SECONDS = 600  # a Huragok that ran this long was fine: the next crash waits the shortest time again


FAILED_START_SECONDS = 120  # a new version that ends within this long of starting didn't start: the old one goes back


SETTINGS_RETRY_SECONDS = 300  # with --no-menu, a settings problem is looked at again this often


LAUNCHER_LOG = "logs/launcher.log"  # where the start loop writes when settings.toml can't be read


FAILED_UPDATE = "failed-update.json"  # in records/: a version that didn't start and was taken back out


def load_for_start(args: argparse.Namespace) -> Settings:
    """settings.toml, for starting. A mistake in it ends the start with the problem shown (--hold keeps the window
    open to read it). With --no-menu (a task at boot) nobody may be looking, and nobody would restart the task
    once the file is fixed: so it says so in logs/launcher.log and tries again every 5 minutes, or as soon as the
    file changes."""
    path = Path(args.config).resolve()
    while True:
        try:
            return load(path)
        except ConfigError as error:
            out.fail(f"Settings problem: {error}")
            if not getattr(args, "no_menu", False):
                raise SystemExit(SETTINGS_PROBLEM) from None
            minutes = SETTINGS_RETRY_SECONDS // 60
            _launcher_log(None, path.parent, "ERROR", f"Settings problem: {error}. Huragok tries again in {minutes} "
                                                      f"minutes, or as soon as {path.name} changes")
            out.note(f"Fix {path.name}: Huragok tries again in {minutes} minutes, or as soon as the file changes.")
            before = mtime(path)
            try:
                _pause(SETTINGS_RETRY_SECONDS, wake=lambda: mtime(path) != before)
            except KeyboardInterrupt:
                raise SystemExit(SETTINGS_PROBLEM) from None


def start_loop(args: argparse.Namespace, settings: Settings) -> int:
    """Runs Huragok in a process of its own, and again when it has to, so it comes back by itself.

    - exit RELAUNCH: an update has installed a new version; start it.
    - the new version ends within FAILED_START_SECONDS of starting (it crashed, or its stricter settings check
      refused the file): put back the files the update replaced (records/last-update.json names their backup,
      records/previous-<version>/), and start that version.
    - any other end that isn't deliberate (DELIBERATE) is a crash: start it again after CRASH_WAITS, forever.
    - a settings problem: the window shows it and stops; with --no-menu (a task at boot) it's tried again every
      5 minutes, or as soon as the file changes.
    The servers are never touched from here, and a stop signal passed on to Huragok ends this loop too.

    This process stays on the code it started with, whatever an update puts on disk, so it does as little as
    possible and has what it needs (the updates module's layout) loaded before the first Huragok starts.
    """
    from ..updates import engine  # noqa: F401 - loaded now, while the files on disk are still this version's
    from ..window import tui  # noqa: F401

    home = settings.home
    command = [sys.executable, "-m", "huragok", "-c", str(settings.path), "start", "--child"]
    if args.verbose:
        command.append("--verbose")
    if args.no_menu:
        command.append("--no-menu")
    menu = common.want_menu(args)
    child: subprocess.Popen[bytes] | None = None
    stopping = False

    def on_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        # In the window Ctrl+C is a key that asks first (see _run_supervisor), not a stop.
        if signum != signal.SIGINT or not menu:
            stopping = True
        if os.name != "nt" and child is not None and child.poll() is None:
            child.send_signal(signum)

    if os.name == "nt":
        # Ctrl+C, Ctrl+Break and closing the window reach every process in the window, Huragok included; it
        # stops by itself and this one waits for it.
        signal.signal(signal.SIGINT, on_signal)
        signal.signal(signal.SIGBREAK, on_signal)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, on_signal)
    try:
        held = lock.take(settings, launcher=True)
    except AlreadyRunning as error:
        out.fail(f"Huragok is already running from this folder (process {error.pid}). To stop it: {LAUNCHER} stop")
        return ALREADY_RUNNING
    crashes = 0
    updated_from: str | None = None  # the version an update replaced, while the new one proves it starts
    code = 0
    try:
        while True:
            version = _version_on_disk(home)
            started = _clock()
            try:
                child = subprocess.Popen(command)
                code = child.wait()
            except OSError as error:
                _say(settings, "ERROR", f"Couldn't start Huragok: {error}")
                code = FAILED
            ran = _clock() - started
            child = None
            if stopping or code in DELIBERATE:
                return code
            try:
                lock.take(settings, launcher=True)  # hold the folder while Huragok starts again
            except AlreadyRunning as error:
                _say(settings, "WARNING", f"Another Huragok (process {error.pid}) runs from this folder now; this "
                                          "one leaves it to that.")
                return code
            if code == RELAUNCH:
                updated_from = version or __version__
                crashes = 0
                new = _version_on_disk(home)
                _say(settings, "INFO", f"Starting the new version{f' ({new})' if new else ''}…")
                continue
            if updated_from is not None and ran < FAILED_START_SECONDS:
                if _put_back(settings, updated_from, version, code, ran):
                    updated_from = None
                    continue
            else:
                updated_from = None
            if code == SETTINGS_PROBLEM:
                if not args.no_menu:
                    return code  # the window shows the problem (and --hold keeps it open)
                minutes = SETTINGS_RETRY_SECONDS // 60
                _say(settings, "ERROR", f"Huragok stopped: a settings problem ({LAUNCHER} check shows it). Fix "
                                        f"{settings.path.name}: it tries again in {minutes} minutes, or as soon as "
                                        "the file changes.")
                before = mtime(settings.path)
                # Nothing here could answer start.cmd stop or status meanwhile, so the folder is let go: they see
                # Huragok isn't running and act by themselves (stop stops the servers itself).
                lock.release(held)
                if not _pause(SETTINGS_RETRY_SECONDS, stopped=lambda: stopping,
                              wake=lambda: mtime(settings.path) != before):
                    return code
                try:
                    lock.take(settings, launcher=True)
                except AlreadyRunning as error:
                    _say(settings, "WARNING", f"Another Huragok (process {error.pid}) runs from this folder now; "
                                              "this one leaves it to that.")
                    return code
                continue
            else:
                if ran >= GOOD_RUN_SECONDS:
                    crashes = 0
                wait = CRASH_WAITS[min(crashes, len(CRASH_WAITS) - 1)]
                crashes += 1
                _say(settings, "ERROR", f"Huragok stopped unexpectedly ({_exit_text(code)}); starting it again in "
                                        f"{wait:.0f} s. {common.log_name(settings)} says why.")
                ended = time.time()  # someone asking since (start.cmd stop, close): start it now to answer
                wake = lambda: _requests_waiting(settings.records, since=ended)  # noqa: E731
            if not _pause(wait, stopped=lambda: stopping, wake=wake):
                return code
    finally:
        lock.release(held)


def _put_back(settings: Settings, previous: str, failed: str | None, code: int, ran: float) -> bool:
    """The version just installed ended right after it started: put `previous` back. True when it's back."""
    failed = failed or "the new version"
    _say(settings, "ERROR", f"Huragok {failed} stopped {ran:.0f} s after it started ({_exit_text(code)}), just "
                            f"after an update: putting {previous} back.")
    try:
        back = _restore_previous(settings.home, previous, failed)
    except RestoreError as error:
        _say(settings, "ERROR", f"Couldn't put Huragok {previous} back: {error}. Starting {failed} again after a "
                                "wait, and trying again if it fails.")
        return False
    _mark_failed_update(settings, failed, back, code)
    _say(settings, "WARNING", f"Huragok {back} is back in place of {failed} ({common.log_name(settings)} says why {failed} "
                              f"didn't start). Starting {back}.")
    return True


class RestoreError(Exception):
    pass


def _clock() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _pause(seconds: float, *, stopped: Any = lambda: False, wake: Any = lambda: False) -> bool:
    """Wait, a second at a time. False when `stopped()` (a stop signal came); `wake()` ends the wait early."""
    deadline = _clock() + seconds
    while (left := deadline - _clock()) > 0:
        if stopped():
            return False
        if wake():
            return True
        _sleep(min(1.0, left))
    return not stopped()


def _requests_waiting(records: Path, since: float) -> bool:
    """A request in records/requests/ made since `since` (start.cmd stop, a second window): the restarted Huragok
    will answer it. One left from before the crash doesn't count: it would end every wait at once."""
    try:
        return any(not p.name.endswith(".reply.json") and mtime(p) > since
                   for p in (records / mailbox.FOLDER).glob("*.json"))
    except OSError:
        return False


def _exit_text(code: int) -> str:
    """An exit code as the host can look it up: Windows' own codes in hexadecimal, a Linux signal by number."""
    if code < 0:
        return f"ended by signal {-code}"
    if code > 0xFFFF:
        return f"exit code {code:#010x}"
    return f"exit code {code}"


def _launcher_log(settings: Settings | None, home: Path, level: str, message: str) -> None:
    """A line in Huragok's log as the start loop sees it (logs/launcher.log when settings.toml can't be read)."""
    path = settings.log_file if settings is not None and settings.log_file else home / LAUNCHER_LOG
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + f",{int(now * 1000) % 1000:03d}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(f"{stamp} {level:<7} launcher: {message}\n")
    except OSError:
        pass


def _say(settings: Settings, level: str, message: str) -> None:
    """What the start loop does, in the window and in the log."""
    from ..window.tui import is_terminal

    if is_terminal() and out.colour:
        try:
            sys.stdout.write("\x1b[?25h")  # a Huragok that crashed may have left the cursor hidden
        except (OSError, ValueError):
            pass
    (out.note if level == "INFO" else out.warn if level == "WARNING" else out.fail)(message)
    _launcher_log(settings, settings.home, level, message)


def _version_on_disk(home: Path) -> str | None:
    """The version in home/engine now (an update changes it under this process), when this copy runs from there."""
    from ..updates import engine

    if not engine.installed_here(home):
        return None
    try:
        return engine.version_in(home)
    except (OSError, engine.UpdateError):
        return None


def _restore_previous(home: Path, version: str, failed: str) -> str:
    """Put back what the latest update replaced (records/last-update.json names its backup, records/previous-<V>/),
    in place of `failed`, which didn't start. Returns the version back on disk. The updates module's own put_back
    does it, loaded by this process before the update: renames only, all or nothing."""
    from ..updates import engine

    installed = engine.last_update(home)
    if installed is None:
        raise RestoreError(f"records/{engine.LAST_UPDATE} doesn't say which files the update replaced, so the "
                           f"backup of {version} isn't known")
    if installed.version != failed and failed != "the new version":
        raise RestoreError(f"records/{engine.LAST_UPDATE} is about {installed.version}, not {failed}; nothing was "
                           "changed")
    try:
        return engine.put_back(home, installed)
    except (engine.UpdateError, OSError) as error:
        raise RestoreError(str(error)) from None


def _mark_failed_update(settings: Settings, failed: str, previous: str, code: int) -> None:
    """records/failed-update.json: the version that didn't start, so it isn't simply installed again."""
    try:
        (settings.records / FAILED_UPDATE).write_text(json.dumps({
            "version": failed, "restored": previous, "exit_code": code, "time": time.time()}), encoding="utf-8")
    except OSError:
        pass
