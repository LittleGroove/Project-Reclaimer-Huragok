"""What every command shares: the settings file, the launcher's name, and the exit codes the start loop (and a
script) tells the ends of a Huragok process apart by."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from ..core.settings import SETTINGS_FILE, Settings, launcher, load
from ..core.tables import ConfigError
from .shell import console as out


DEFAULT_CONFIG = os.environ.get("HURAGOK_CONFIG", SETTINGS_FILE)


LAUNCHER = launcher("huragok")  # start.cmd / start.sh say themselves; "huragok" is the installed command


# Exit codes. Each deliberate end has its own, so the start loop (and a script) can tell them from a crash: an
# uncaught exception is 1, a process killed by Windows or a signal some other number.
FAILED = 1  # it didn't work (and what Python itself exits with on an uncaught exception)


SETTINGS_PROBLEM = 2  # settings.toml (or commands.toml) has a mistake, or a name or option isn't known


SET_UP = 3  # files written, look them over and start again (start.cmd keeps the window open)


ALREADY_RUNNING = 4  # start: Huragok already runs from this folder


NOT_RUNNING = 5  # status: Huragok isn't running from this folder


RELAUNCH = 10  # exit code of the supervisor itself: a new version is installed, run it


DELIBERATE = (0, SET_UP, ALREADY_RUNNING)  # ends the start loop takes as they are


def load_settings(path: str) -> Settings:
    try:
        return load(path)
    except ConfigError as error:
        out.fail(f"Settings problem: {error}")
        raise SystemExit(SETTINGS_PROBLEM) from None


def log_name(settings: Settings) -> str:
    """Where the log is, as the host would find it: logs/huragok.log."""
    if settings.log_file is None:
        return "the log"
    try:
        return settings.log_file.relative_to(settings.home).as_posix()
    except ValueError:
        return str(settings.log_file)


def config_option(path: Path) -> str:
    """` -c mine.toml` when the settings file isn't the usual one, for the next command to type."""
    return "" if path.name == SETTINGS_FILE else f" -c {path.name}"


def hold(args: argparse.Namespace, code: int) -> int:
    """A double-clicked window closes when we exit: keep it open to read why it stopped."""
    from ..window.tui import is_terminal

    if getattr(args, "hold", False) and code not in (0, RELAUNCH) and is_terminal():
        try:
            input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


def want_menu(args: argparse.Namespace) -> bool:
    from ..window.tui import is_terminal

    return not getattr(args, "no_menu", False) and is_terminal()


def backend_for(settings: Settings, bus: Any, states: dict) -> Any:
    """The backend for a command; None (said why) when its section in settings.toml can't be used."""
    from ..servers.backends import make_backend

    try:
        return make_backend(settings, bus, states)
    except ConfigError as error:
        out.fail(f"Settings problem: {error}")
        return None
