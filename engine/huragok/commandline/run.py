"""Huragok running in this process (`start --child`, what the start loop starts): the host's files brought up to
date, the lock taken, then Huragok with its window, or the plain log and typed commands with --no-menu. A second
window (Huragok already runs from this folder) is the same dashboard, its keys handed to the running one."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import traceback

from ..core import lock
from ..core.lock import AlreadyRunning
from ..core.settings import COMMANDS_FILE, Settings, load
from ..core.tables import ConfigError
from . import common, firstrun, logsetup
from .common import ALREADY_RUNNING, FAILED, RELAUNCH
from .shell import console as out


def summary(settings: Settings) -> None:
    """What's about to run, for the window the host is looking at."""
    log = logging.getLogger("superintendent")
    parts = settings.parts
    automod = parts["automod"]
    where = settings.servers_folder if settings.servers_folder else settings.single_folder
    log.info("Folder %s · servers in %s", settings.home, where)
    on = ["commands"]
    if automod.enabled:
        on.append("automod" + (" (dry run)" if automod.dry_run or settings.dry_run else ""))
    if parts["watchdog"].enabled:
        from ..parts import PARTS

        on.append(f"watchdog ({parts['watchdog'].options.get('mode', PARTS['watchdog'].defaults['mode'])})")
    if parts["eventlog"].enabled:
        on.append("eventlog")
    log.info("Running: %s", ", ".join(on))
    owners = settings.permissions.members.get("owner", set())
    if not owners:
        log.warning("Nobody is owner yet: put your player ID under [owner] players in %s", COMMANDS_FILE)
    for line in settings.notes:  # old values: legal, but maybe not what the host thinks they are
        log.warning(line)
    if not settings.servers:
        for line in firstrun.layout_help(settings.home):
            log.warning(line)


def second_window(settings: Settings) -> str:
    """A second window: the same dashboard, keys handed to the supervising one. Returns why it ended."""
    from ..window.dashboard import Menu
    from ..window.controllers import RemoteController
    from ..window.tui import Keyboard, Terminal

    async def main() -> str:
        keyboard = Keyboard(asyncio.get_running_loop())
        keyboard.start()
        try:
            menu = Menu(RemoteController(settings, lambda: lock.running_supervisor(settings)), Terminal(), keyboard)
            return await menu.run()
        finally:
            keyboard.stop()

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return "quit"


def run_huragok(args: argparse.Namespace) -> int:
    from ..huragok import Huragok
    from ..core.logs import RecentLog
    from ..window.tui import disable_quick_edit

    settings = common.load_settings(args.config)
    disable_quick_edit()  # a click in the window must never pause Huragok (and its servers' care)
    menu_mode = common.want_menu(args)
    recent = RecentLog() if menu_mode else None
    logsetup.setup(settings, args.verbose, console=not menu_mode, recent=recent)
    try:
        held = lock.take(settings)
    except AlreadyRunning as error:
        out.fail(f"Huragok is already running from this folder (process {error.pid}).")
        return ALREADY_RUNNING
    settings = bring_files_up_to_date(args, settings)
    relaunch = False

    async def main() -> None:
        nonlocal relaunch
        supervisor = Huragok(settings, recent=recent)
        loop = asyncio.get_running_loop()
        if os.name == "nt":
            # Ctrl+C reaches us through asyncio.run; Ctrl+Break and closing the window here.
            signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(supervisor.stop))
        else:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.add_signal_handler(sig, supervisor.stop)
        menu = None
        keyboard = None
        if menu_mode:
            from ..window.controllers import LocalController
            from ..window.dashboard import Menu
            from ..window.tui import Keyboard, Terminal

            keyboard = Keyboard(loop)
            keyboard.start()
            # In the window Ctrl+C is a key that asks first (Q), never a signal that ends every server's care.
            if os.name == "nt":
                signal.signal(signal.SIGINT, keyboard.ctrl_c)
            else:
                loop.add_signal_handler(signal.SIGINT, keyboard.ctrl_c)
                signal.signal(signal.SIGTSTP, signal.SIG_IGN)  # Ctrl+Z would freeze the supervisor
            menu = Menu(LocalController(supervisor, recent), Terminal(), keyboard, recent)  # type: ignore[arg-type]
        try:
            typed = None
            if menu is None and sys.stdin is not None and sys.stdin.isatty():
                from .console import TypedCommands

                typed = TypedCommands(supervisor)
            await supervisor.run(menu, console=typed)
        finally:
            relaunch = supervisor.relaunch
            if keyboard is not None:
                keyboard.stop()

    try:
        summary(settings)
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - the start loop starts it again; the log must say why it stopped
        logging.getLogger("huragok").critical("Huragok stopped with an error", exc_info=True)
        if menu_mode:  # the log isn't on the screen in the window
            traceback.print_exc()
        return FAILED
    finally:
        lock.release(held)  # back to the start loop, if it lent it
    return RELAUNCH if relaunch else 0


def bring_files_up_to_date(args: argparse.Namespace, settings: Settings) -> Settings:
    """The host's files in this version's form (updates/hostfiles.py), under the lock and before anything looks after a
    server: settings.toml read again when a step changed it. Nothing here may keep Huragok from starting."""
    from ..updates.hostfiles import bring_up_to_date

    try:
        changed = bring_up_to_date(settings.path, settings.records, settings.packages_folder)
    except Exception:  # noqa: BLE001 - the files as they are still work: this version reads every older form
        logging.getLogger("huragok").exception("Couldn't bring the files up to date; carrying on with them as they are")
        return settings
    if not changed:
        return settings
    try:
        return load(settings.path)
    except ConfigError as error:  # a step's write is checked as it's made, so this would be a bug: keep what ran
        logging.getLogger("huragok").error("settings.toml couldn't be read again after it was brought up to date "
                                           "(%s); carrying on with it as it was read before", error)
        return settings
