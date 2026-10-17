"""The supervisor's commands: start, status, stop, restart, say, rcon, watch, check, update, init.

Hosts run them through start.cmd (Windows) or start.sh (Linux) at the root; with no
command, those open the window (the dashboard), which is the supervisor.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

import psutil

from . import __version__, mailbox
from .config import (BACKENDS, COMMANDS_FILE, SERVERS_FOLDER, SETTINGS_FILE, ConfigError, Settings,
                     find_server_folders, load)
from .packages import PACKAGES_FOLDER
from .shell import console as out

DEFAULT_CONFIG = os.environ.get("RECLAIMER_SUPERVISOR_CONFIG", SETTINGS_FILE)
LAUNCHER = os.environ.get("RECLAIMER_LAUNCHER", "reclaimer-supervisor")  # set by start.cmd / start.sh
SET_UP = 3  # exit code: files written, look them over and start again (start.cmd keeps the window open)
RELAUNCH = 10  # exit code of the supervisor itself: a new version is installed, run it
LOCK_FILE = "supervisor.pid"
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


class AlreadyRunning(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(f"process {pid}")
        self.pid = pid


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


class _FileFormatter(logging.Formatter):
    """supervisor.log names the part that spoke without the package prefix: `manager:`, not
    `reclaimer_supervisor.manager:`."""

    def format(self, record: logging.LogRecord) -> str:
        record.short = record.name.removeprefix("reclaimer_supervisor.")
        return super().format(record)


class _ConsoleHandler(logging.StreamHandler):
    """The plain log (--no-menu): warnings yellow and errors red when the window is a real console."""

    def __init__(self) -> None:
        super().__init__()
        from .tui import enable_vt

        try:
            self._colour = bool(self.stream.isatty()) and enable_vt() and not os.environ.get("NO_COLOR")
        except (AttributeError, ValueError):
            self._colour = False

    def format(self, record: logging.LogRecord) -> str:
        from .tui import paint

        text = super().format(record)
        if not self._colour or record.levelno < logging.WARNING:
            return text
        return paint(text, "red" if record.levelno >= logging.ERROR else "yellow")


def _setup_logging(settings: Settings | None, verbose: bool, *, console: bool = True,
                   recent: logging.Handler | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if console:
        stream = _ConsoleHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(stream)
    if recent is not None:
        root.addHandler(recent)
    if settings and settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file = logging.handlers.RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=5,
                                                    encoding="utf-8")
        file.setFormatter(_FileFormatter("%(asctime)s %(levelname)-7s %(short)s: %(message)s"))
        root.addHandler(file)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _load(path: str) -> Settings:
    try:
        return load(path)
    except ConfigError as error:
        out.fail(f"Settings problem: {error}")
        raise SystemExit(2) from None


def _template(name: str) -> str:
    return resources.files("reclaimer_supervisor").joinpath(f"templates/{name}").read_text("utf-8")


# Setting up ------------------------------------------------------------------------------

from .dedicated import find_executable  # noqa: E402  (the server program: project-reclaimer.exe, or the release's name)


def detect_layout(home: Path) -> tuple[str, str, list[Path]]:
    """(servers_folder, server_folder, the server folders found) for a first start in `home`."""
    servers = home / SERVERS_FOLDER
    found = find_server_folders(home, servers, None)
    if found:
        return SERVERS_FOLDER, "", found
    if (home / "dedicated.toml").is_file():  # unzipped into a server folder
        return SERVERS_FOLDER, ".", [home]
    if (home.parent / "dedicated.toml").is_file():  # the 0.1 layout: inside the server folder
        return SERVERS_FOLDER, "..", [home.parent]
    return SERVERS_FOLDER, "", []


def pick_backend(folders: list[Path]) -> tuple[str, dict[str, str], str]:
    """(backend, its settings, why) for what the server folders hold."""
    for folder in folders:
        if compose := next((name for name in COMPOSE_FILES if (folder / name).is_file()), None):
            return "docker", {"compose_file": compose}, f"found {compose}: the supervisor runs the containers"
    for folder in folders:
        if exe := find_executable(folder):
            return "process", {"executable": exe.name}, f"found {exe.name}: the supervisor runs the servers"
    return "process", {}, "the supervisor runs the servers: project-reclaimer.exe in each server folder"


def write_files(home: Path, server_folder: str | None = None, backend: str | None = None,
                force: bool = False) -> list[str]:
    """settings.toml, commands.toml and automod.txt, each only if it isn't there (or force). Returns what was written."""
    from .permissions import render_commands_toml

    home.mkdir(parents=True, exist_ok=True)
    written = []
    settings_path = home / SETTINGS_FILE
    if force or not settings_path.exists():
        servers_folder, single, folders = detect_layout(home)
        if server_folder is not None:
            single = server_folder
            folders = [Path(home / server_folder).resolve()]
        picked, options, why = pick_backend(folders)
        backend = backend or picked
        if not folders:
            (home / servers_folder).mkdir(exist_ok=True)
        text = (_template(SETTINGS_FILE)
                .replace("{{", "\x00").replace("}}", "\x01")  # doubled braces in the template are literal
                .replace("{servers_folder}", servers_folder)
                .replace("{server_folder}", single)
                .replace("{backend}", backend)
                .replace("{executable}", options.get("executable", "project-reclaimer.exe"))
                .replace("{compose_file}", options.get("compose_file", "compose.yaml"))
                .replace("\x00", "{").replace("\x01", "}"))
        settings_path.write_text(text, encoding="utf-8")
        where = (f"{len(folders)} server folder(s) found" if folders
                 else f"no server folders yet: put each one in {servers_folder}{os.sep}")
        written.append(f"{SETTINGS_FILE:<15} how it runs. {where}; backend {backend}"
                       + (f" ({why})" if backend == picked else ""))
    commands_path = home / COMMANDS_FILE
    if force or not commands_path.exists():
        commands_path.write_text(render_commands_toml(), encoding="utf-8")
        written.append(f"{COMMANDS_FILE:<15} who can use which in-game command. Put your player ID under [owner]")
    words = home / "automod.txt"
    if not words.exists():
        words.write_text(_template("automod.txt"), encoding="utf-8")
        written.append("automod.txt     automod's word list, empty for now (automod starts in dry run)")
    packages = home / PACKAGES_FOLDER
    if not packages.exists():
        from .packages import install_starters

        packages.mkdir(parents=True, exist_ok=True)
        starters = install_starters(packages)
        written.append(f"{PACKAGES_FOLDER + '/':<15} {len(starters)} starter packages for new servers "
                       "(N in the window, or `new`)")
    return written


def _layout_help(home: Path) -> list[str]:
    return [
        f"Each server is one folder in {home / SERVERS_FOLDER}: the folder `dedicated init` makes, with",
        "dedicated.toml, project-reclaimer.exe and game/. For example:",
        f"  {home / SERVERS_FOLDER / 'Big Team Battle'}",
        f"  {home / SERVERS_FOLDER / 'Slayer'}",
        "Switch RCON on in each dedicated.toml ([rcon] password). Folders added later are picked up while running.",
    ]


def _written_rows(lines: list[str]) -> list[tuple[str, str]]:
    """write_files' lines (`name   what it is`) as (name, what) rows for a listing."""
    rows = []
    for line in lines:
        name, _, what = line.partition("  ")
        rows.append((name.strip(), what.strip()))
    return rows


def cmd_init(args: argparse.Namespace) -> int:
    home = Path(args.config).resolve().parent
    if (home / SETTINGS_FILE).exists() and not args.force:
        out.fail(f"{home / SETTINGS_FILE} already exists; --force replaces it (and {COMMANDS_FILE}).")
        return 1
    written = write_files(home, args.server_folder, args.backend, force=args.force)
    out.heading("Written", note=str(home))
    out.listing(_written_rows(written), index=False, wrap=True)
    out.write()
    out.say(f"Next: {LAUNCHER} check")
    return 0


# Running ------------------------------------------------------------------------------------

def _process_alive(pid: int, create_time: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - create_time) < 1
    except psutil.Error:
        return False


def _lock(settings: Settings) -> dict:
    try:
        lock = json.loads((settings.records / LOCK_FILE).read_text(encoding="utf-8"))
        return lock if isinstance(lock, dict) else {}
    except (OSError, ValueError):
        return {}


def running_supervisor(settings: Settings) -> int | None:
    """The process ID of a supervisor already running from this folder, if any."""
    lock = _lock(settings)
    try:
        pid, created = int(lock["pid"]), float(lock["create_time"])
    except (KeyError, TypeError, ValueError):
        return None
    return pid if pid != os.getpid() and _process_alive(pid, created) else None


def _take_lock(settings: Settings) -> Path:
    """Become the one supervisor of this folder, or raise AlreadyRunning. Two started at once can't both win."""
    path = settings.records / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    mine = json.dumps({"pid": os.getpid(), "create_time": psutil.Process().create_time(), "version": __version__})
    for _ in range(5):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if pid := running_supervisor(settings):
                raise AlreadyRunning(pid)
            try:
                path.unlink()  # left by a supervisor that's gone
            except OSError:
                pass
            time.sleep(0.05)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(mine)
        return path
    raise AlreadyRunning(running_supervisor(settings) or 0)


def _summary(settings: Settings) -> None:
    """What's about to run, for the window the host is looking at."""
    log = logging.getLogger("supervisor")
    parts = settings.parts
    automod = parts["automod"]
    where = settings.servers_folder if settings.servers_folder else settings.single_folder
    log.info("Folder %s · servers in %s", settings.home, where)
    on = ["commands"]
    if automod.enabled:
        on.append("automod" + (" (dry run)" if automod.dry_run or settings.dry_run else ""))
    if parts["watchdog"].enabled:
        on.append(f"watchdog ({parts['watchdog'].options.get('mode', 'watch')})")
    if parts["eventlog"].enabled:
        on.append("eventlog")
    log.info("Running: %s", ", ".join(on))
    owners = settings.permissions.members.get("owner", set())
    if not owners:
        log.warning("Nobody is owner yet: put your player ID under [owner] players in %s", COMMANDS_FILE)
    if not settings.servers:
        for line in _layout_help(settings.home):
            log.warning(line)


def _hold(args: argparse.Namespace, code: int) -> int:
    """A double-clicked window closes when we exit: keep it open to read why it stopped."""
    from .tui import is_terminal

    if getattr(args, "hold", False) and code not in (0, RELAUNCH) and is_terminal():
        try:
            input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


def _want_menu(args: argparse.Namespace) -> bool:
    from .tui import is_terminal

    return not getattr(args, "no_menu", False) and is_terminal()


def cmd_start(args: argparse.Namespace) -> int:
    if args.child:
        return _run_supervisor(args)
    home = Path(args.config).resolve().parent
    if not (home / SETTINGS_FILE).exists():
        out.title(f"RECLAIMER SUPERVISOR {__version__}: THE FIRST START", f"setting up in {home}")
        out.heading("Written")
        out.listing(_written_rows(write_files(home)), index=False, wrap=True)
        if not find_server_folders(home, home / SERVERS_FOLDER, None) and not (home / "dedicated.toml").exists() \
                and not (home.parent / "dedicated.toml").exists():
            out.heading("Server folders")
            for line in _layout_help(home):
                out.say(line)
        out.write()
        out.ok(f"Look these over, then start again: {LAUNCHER}")
        return _hold(args, SET_UP)
    for line in write_files(home):  # put back any file that was deleted, with its defaults
        out.say(f"Wrote {line}")
    settings = _load(args.config)
    if pid := running_supervisor(settings):
        if _want_menu(args):
            outcome = _remote_window(settings)
            if outcome != "takeover":
                return 0
            out.note("The supervising window closed; this one takes over.")
        else:
            out.fail(f"The supervisor is already running from this folder (process {pid}). To stop it: {LAUNCHER} stop")
            return _hold(args, 1)
    return _hold(args, _start_loop(args))


def _remote_window(settings: Settings) -> str:
    """A second window: the same dashboard, keys handed to the supervising one. Returns why it ended."""
    from .menu import Menu, RemoteController
    from .tui import Keyboard, Terminal

    async def main() -> str:
        keyboard = Keyboard(asyncio.get_running_loop())
        keyboard.start()
        try:
            menu = Menu(RemoteController(settings, lambda: running_supervisor(settings)), Terminal(), keyboard)
            return await menu.run()
        finally:
            keyboard.stop()

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        return "quit"


def _start_loop(args: argparse.Namespace) -> int:
    """Runs the supervisor in a process of its own, and again whenever an update has installed a new version.

    This process stays on the old code, so it does as little as possible: start the
    supervisor, pass on signals, and start it again when it exits with RELAUNCH.
    """
    command = [sys.executable, "-m", "reclaimer_supervisor", "-c", str(Path(args.config).resolve()), "start", "--child"]
    if args.verbose:
        command.append("--verbose")
    if args.no_menu:
        command.append("--no-menu")
    child: subprocess.Popen[bytes] | None = None

    def forward(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    if os.name == "nt":
        # Ctrl+C, Ctrl+Break and closing the window reach every process in the window, the
        # supervisor included; it stops by itself and this one waits for it.
        signal.signal(signal.SIGINT, lambda *_: None)
        signal.signal(signal.SIGBREAK, lambda *_: None)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, forward)
    while True:
        child = subprocess.Popen(command)
        code = child.wait()
        if code != RELAUNCH:
            return code
        out.note("Starting the new version…")


def _run_supervisor(args: argparse.Namespace) -> int:
    from .core import Supervisor
    from .tui import RecentLog, disable_quick_edit

    settings = _load(args.config)
    disable_quick_edit()  # a click in the window must never pause the supervisor (and its servers' care)
    menu_mode = _want_menu(args)
    recent = RecentLog() if menu_mode else None
    _setup_logging(settings, args.verbose, console=not menu_mode, recent=recent)
    try:
        lock = _take_lock(settings)
    except AlreadyRunning as error:
        out.fail(f"The supervisor is already running from this folder (process {error.pid}).")
        return 1
    _summary(settings)
    relaunch = False

    async def main() -> None:
        nonlocal relaunch
        supervisor = Supervisor(settings, interactive=not menu_mode and sys.stdin is not None and sys.stdin.isatty(),
                                recent=recent)
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
            from .menu import LocalController, Menu
            from .tui import Keyboard, Terminal

            keyboard = Keyboard(loop)
            keyboard.start()
            menu = Menu(LocalController(supervisor, recent), Terminal(), keyboard, recent)  # type: ignore[arg-type]
        try:
            await supervisor.run(menu)
        finally:
            relaunch = supervisor.relaunch
            if keyboard is not None:
                keyboard.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        lock.unlink(missing_ok=True)
    return RELAUNCH if relaunch else 0


def cmd_update_program(args: argparse.Namespace, settings: Settings) -> int:
    """`update --program`: a new release of the server program, installed at a quiet moment."""
    from .serverupdates import download_program, install_program, latest_program_release
    from .tui import is_terminal
    from .updates import UpdateError, summary

    when = "now" if args.now else "after_game"
    if pid := running_supervisor(settings):
        reply = mailbox.send(settings.records, "check_program_update", wait=60)
        (out.ok if reply.ok else out.warn)(reply.message)
        for line in reply.text.splitlines():
            out.say(f"  {line}")
        if args.check or not reply.ok or " is out " not in reply.message:
            return 0 if reply.ok else 1
        if not args.yes:
            if not is_terminal():
                out.note(f"To install it: {LAUNCHER} update --program --yes [--now]")
                return 1
            try:
                answer = input(f"Install it and restart each running server {when.replace('_', ' ')}? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                out.note("Not installed.")
                return 0
        out.note(f"Asking the running supervisor (process {pid}) to install it…")
        reply = mailbox.send(settings.records, "update_program", wait=900, when=when, why=f"asked by {LAUNCHER} update")
        (out.ok if reply.ok else out.warn)(reply.message)
        return 0 if reply.ok else 1
    try:
        release = latest_program_release(settings.server_updates.repository)
    except UpdateError as error:
        out.fail(f"Couldn't look for server program releases: {error}")
        return 1
    if release is None:
        out.warn("No server program release found.")
        return 1
    out.write(f"Newest server program: Project Reclaimer {release.version} ({release.page})", "white")
    for line in summary(release.notes):
        out.say(f"  {line}")
    out.note("No supervisor is running, so the servers' versions aren't known; the file is put in place without restarts.")
    if args.check:
        return 0
    if not args.yes:
        if not is_terminal():
            out.note(f"To install it: {LAUNCHER} update --program --yes")
            return 1
        try:
            answer = input(f"Put {release.version} in place of every copy of the server program? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out.note("Not installed.")
            return 0
    try:
        path = download_program(release, settings.records)
        try:
            replaced = install_program(settings, path, release.version)
        finally:
            path.unlink(missing_ok=True)
    except (UpdateError, OSError) as error:
        out.fail(f"Not installed: {error}")
        return 1
    for target in replaced:
        out.note(f"replaced {target}")
    out.ok(f"Server program {release.version} installed. Start the supervisor to run the servers on it: {LAUNCHER}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Look for a new version on GitHub and, once the host says yes, install it."""
    from .tui import is_terminal
    from .updates import UpdateError, installed_here, is_newer, latest_release, summary, update

    settings = _load(args.config)
    if args.program:
        return cmd_update_program(args, settings)
    try:
        release = latest_release(settings.updates.repository)
    except UpdateError as error:
        out.fail(f"Couldn't look for updates: {error}")
        return 1
    if release is None or not is_newer(release):
        out.ok(f"Reclaimer Supervisor {__version__} is the newest version.")
        return 0
    out.write(f"Update available: Reclaimer Supervisor {release.version} (this is {__version__})", "white")
    for line in summary(release.notes):
        out.say(f"  {line}")
    out.note(f"What's new: {release.page}")
    if args.check:
        return 0
    if not installed_here(settings.home):
        out.fail(f"This copy doesn't run from {settings.home / 'engine'}, so it can't update itself; download the new "
                 "version instead.")
        return 1
    if not args.yes:
        if not is_terminal():
            out.note(f"To install it: {LAUNCHER} update --yes")
            return 1
        try:
            answer = input(f"Install {release.version} now? Your servers keep running. [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out.note("Not installed.")
            return 0
    if pid := running_supervisor(settings):
        out.note(f"Asking the running supervisor (process {pid}) to install it…")
        reply = mailbox.send(settings.records, "update", wait=60, why=f"asked by {LAUNCHER} update")
        if not reply.ok:
            out.fail(reply.message)
            return 1
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            time.sleep(1)
            if _lock(settings).get("version") == release.version and running_supervisor(settings):
                out.ok(f"Updated to {release.version}, and running again.")
                return 0
        out.fail(f"Not done after {args.wait:.0f} s; its window or logs/supervisor.log says why.")
        return 1
    out.note("Installing…")
    try:
        version = update(settings.home, release)
    except (UpdateError, OSError, subprocess.SubprocessError) as error:
        out.fail(f"Update failed, still on {__version__}: {error}")
        return 1
    out.ok(f"Updated to {version}. Start it as usual: {LAUNCHER}")
    return 0


# Talking to a running supervisor ------------------------------------------------------------------

def _explain(server: str, error: BaseException) -> str:
    """A connection problem in plain words: a server that's off refuses its console port."""
    if isinstance(error, (ConnectionRefusedError, ConnectionResetError)) or "1225" in str(error)             or "refused" in str(error).lower():
        return f"{server} isn't running (nothing answers on its console port)"
    if isinstance(error, TimeoutError):
        return f"{server}: its console didn't answer in time"
    return f"{server}: {error}"


def _print_reply(reply: mailbox.Reply) -> None:
    (out.ok if reply.ok else out.warn)(reply.message)
    for line in (reply.text or "").splitlines():
        out.say(line)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop servers (warning players unless --now). With no server named: every server and the supervisor."""
    settings = _load(args.config)
    pid = running_supervisor(settings)
    if pid:
        if args.servers:
            ok = True
            for name in args.servers:
                reply = mailbox.send(settings.records, "stop", name, immediate=args.now)
                _print_reply(reply)
                ok = ok and reply.ok
            return 0 if ok else 1
        reply = mailbox.send(settings.records, "stop_all", immediate=args.now)
        out.say(f"Asked the supervisor (process {pid}) to stop every server"
                f"{' now' if args.now else ', warning players first'}, then itself: {reply.message or 'ok'}")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            if running_supervisor(settings) is None:
                out.ok("Stopped.")
                return 0
            time.sleep(0.5)
        out.fail(f"Still running after {args.wait:.0f} s; its window or logs/supervisor.log says why.")
        return 1

    from .events import EventBus
    from .lifecycle import LifecycleError, LifecycleUnavailable, make_backend
    from .state import ServerState

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    backend = make_backend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    try:
        if args.servers:
            backend.adopt()
            stopped = []
            for name in args.servers:
                server = settings.server(name)
                asyncio.run(backend.stop(server))
                stopped.append(server.name)
        else:
            stopped = asyncio.run(backend.stop_all())
    except KeyError as error:
        out.fail(f"No server named {error}")
        return 2
    except (LifecycleError, LifecycleUnavailable) as error:
        out.fail(str(error))
        return 1
    out.ok(f"Stopped: {', '.join(stopped)}." if stopped else "No servers of ours were running.")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    """Close the running supervisor; the servers keep running (what Q does in the window)."""
    settings = _load(args.config)
    pid = running_supervisor(settings)
    if not pid:
        out.note("No supervisor is running from this folder.")
        return 0
    reply = mailbox.send(settings.records, "close")
    out.say(reply.message)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if running_supervisor(settings) is None:
            out.ok("Closed; the servers keep running. Start it again to pick them up.")
            return 0
        time.sleep(0.5)
    out.fail(f"Still running after {args.wait:.0f} s.")
    return 1


def cmd_restart(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    if not running_supervisor(settings):
        out.fail(f"No supervisor is running from this folder. A restart needs one to bring the server back and look "
                 f"after it: start {LAUNCHER}, then restart from its window (R) or run this again.")
        return 1
    ok = True
    when = "after_game" if args.after_game else "now"
    for name in args.servers or ["all"]:
        reply = mailbox.send(settings.records, "restart", name, immediate=args.now, reason=args.reason or "", when=when)
        _print_reply(reply)
        ok = ok and reply.ok
    return 0 if ok else 1


def cmd_say(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    text = " ".join(args.words).strip()
    if not text:
        out.fail("Nothing to say.")
        return 2
    if running_supervisor(settings):
        reply = mailbox.send(settings.records, "say", args.server, text=text)
        _print_reply(reply)
        return 0 if reply.ok else 1
    from .rcon import RconError, one_shot

    targets = settings.servers if args.server.lower() == "all" else [settings.server(args.server)]
    code = 0
    for server in targets:
        try:
            reply = asyncio.run(one_shot(server, "say", text))
            (out.ok if reply.ok else out.warn)(f"{server.name}: {'sent' if reply.ok else reply.text}")
        except (RconError, OSError, TimeoutError) as error:
            out.fail(_explain(server.name, error))
            code = 1
    return code


def cmd_status(args: argparse.Namespace) -> int:
    from .lifecycle import port_in_use

    settings = _load(args.config)
    pid = running_supervisor(settings)
    live: dict = {}
    try:
        live = json.loads((settings.records / "live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    fresh = pid and time.time() - float(live.get("updated") or 0) < 15
    if fresh:
        from .menu import table_lines

        out.title(f"RECLAIMER SUPERVISOR {live.get('version', '')}",
                  f"running (process {pid}) · backend {live.get('backend', '')} · {settings.home}")
        for text, colour in table_lines(live.get("servers") or [], out.width):
            out.write(text, colour)
        return 0
    out.title(f"RECLAIMER SUPERVISOR {__version__}", f"not running · {settings.home}")
    out.warn("No supervisor is running from this folder, so nothing is watching the servers.")
    rows: list[tuple[str, ...]] = []
    colours: list[str | None] = []
    for server in settings.servers:
        running = port_in_use(server)
        rows.append((server.name, str(server.game_port), "RUNNING - nobody is watching it" if running else "stopped",
                     str(server.folder)))
        colours.append("yellow" if running else None)
    out.listing(rows, colours=colours, wrap=True)
    if not settings.servers:
        out.heading("Server folders")
        for line in _layout_help(settings.home):
            out.say(line)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .watch import follow

    settings = _load(args.config)
    what = (args.what or "").strip()
    if not what or what.lower() in ("supervisor", "events", "log"):
        path = settings.log_file
        if path is None:
            out.fail("The supervisor keeps no log file (log_file is empty).")
            return 1
    else:
        try:
            server = settings.server(what.strip('"'))
        except KeyError:
            out.fail(f"No server named {what}. Servers: {', '.join(s.name for s in settings.servers)}")
            return 2
        console = settings.logs / f"console-{server.slug}.log"
        path = server.log_path if args.server_log or not console.exists() else console
    try:
        follow(path, once=args.once)
    except KeyboardInterrupt:
        pass
    return 0


# Looking ---------------------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    from .events import EventBus
    from .lifecycle import make_backend
    from .part import commands_of
    from .parts import PARTS
    from .permissions import ROLES, TITLES, all_commands
    from .state import ServerState

    settings = _load(args.config)
    ok = True
    out.title("CHECK", f"Reclaimer Supervisor {__version__}{_update_note(settings)}")
    fields: list[tuple[str, str]] = [("Folder", str(settings.home))]
    if settings.servers_folder:
        fields.append(("Servers", f"{settings.servers_folder}"
                                  f"{'' if settings.servers_folder.is_dir() else '  (not there yet)'}"))
    if settings.single_folder:
        fields.append(("Server folder", str(settings.single_folder)))
    fields.append(("Backend", settings.backend))
    if pid := running_supervisor(settings):
        fields.append(("Running", f"yes (process {pid})"))
    out.fields(fields)
    backend = make_backend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    if not settings.servers:
        out.heading("Servers")
        out.warn("none found")
        for line in _layout_help(settings.home):
            out.say(line)
        ok = False
    for server in settings.servers:
        endpoint = server.rcon_endpoint()
        status_age = None
        try:
            updated = json.loads(server.status_path.read_text(encoding="utf-8")).get("updated")
            status_age = time.time() - float(updated)
        except (OSError, ValueError, TypeError):
            pass
        out.heading(server.name)
        rows: list[tuple[str, str]] = [
            ("folder", str(server.folder)),
            ("data", f"{server.data_dir}{'' if server.data_dir.is_dir() else '  (not there yet)'}"),
            ("port", f"{server.game_port} · autostart {'yes' if server.autostart else 'no'}"
                     + (f" · daily restart {server.daily_restart}" if server.daily_restart else "")),
        ]
        problems = [error for error in server.errors if "RCON" not in error] + list(backend.problems(server))
        if endpoint:
            rows.append(("RCON", f"{endpoint.host}:{endpoint.port} · password set · host key "
                                 f"{'found' if server.host_key() else 'not found (server not started?)'}"))
        else:
            rows.append(("RCON", "off"))
            problems.insert(0, "RCON is off: set [rcon] password in dedicated.toml")
        if status_age is None:
            rows.append(("running", "unknown (no status.json)"))
        else:
            rows.append(("running", f"{'yes' if status_age < 30 else 'no'} (status.json written {status_age:.0f} s ago)"))
        out.fields(rows)
        for problem in problems:
            out.warn(f"PROBLEM  {problem}")
            ok = False
    if settings.problems:
        out.heading("Left out")
        for problem in settings.problems:
            out.warn(f"PROBLEM  {problem}")
        ok = False

    out.heading("Parts")
    rows = []
    notes: list[tuple[str, bool]] = []  # (line, is a problem)
    for name, cls in PARTS.items():
        part = settings.parts[name]
        if cls.always_on:
            continue
        state = ("on" + (", dry run" if part.dry_run or settings.dry_run else "")) if part.enabled else "off"
        rows.append((name, state, cls.description))
        if not part.enabled:
            continue
        try:
            notes.extend((f"{name}: {warning}", False) for warning in cls.preflight(part.options, settings.home))
        except ValueError as error:
            notes.append((f"PROBLEM  [{name}] {error}", True))
    out.listing(rows, index=False, wrap=True)
    for line, problem in notes:
        out.warn(line)
        ok = ok and not problem

    permissions = settings.permissions
    prefix = permissions.prefix
    out.heading(f"Commands ({COMMANDS_FILE}{'' if permissions.path and permissions.path.exists() else ', missing: defaults'})")
    unavailable = {spec.name for name, cls in PARTS.items() if not settings.parts[name].enabled
                   for spec in commands_of(cls)}
    by_role: dict[str, list[str]] = {role: [] for role in ROLES}
    for spec in all_commands().values():
        if spec.name not in permissions.off:
            by_role[permissions.role_for(spec)].append(spec.name)
    admins = {a for s in settings.servers for a in s.admins}
    who = {"player": "",
           "moderator": f" ({len(permissions.members['moderator'])} listed)",
           "admin": f" (the server's own; {len(admins)} in dedicated.toml)",
           "owner": f" ({len(permissions.members['owner'])} listed)"}
    role_rows = []
    for role in ROLES:
        names = " ".join(prefix + n + ("*" if n in unavailable else "") for n in sorted(by_role[role]))
        role_rows.append((f"{TITLES[role]}{who[role]}", names or "-"))
    if permissions.off:
        role_rows.append(("off", " ".join(prefix + n for n in sorted(permissions.off))))
    out.listing(role_rows, index=False, wrap=True, widest=48)
    if unavailable & {n for names in by_role.values() for n in names}:
        out.note("* its part is switched off")
    out.note("Each role can also use the commands listed above it.")
    if not permissions.members["owner"]:
        out.warn(f"warning: nobody is owner yet: put your player ID under [owner] players in {COMMANDS_FILE}")
    out.write()
    if ok:
        out.ok("Everything looks ready.")
    else:
        out.warn("Fix the problems above, then check again.")
    return 0 if ok else 1


def _update_note(settings: Settings) -> str:
    if not settings.updates.check:
        return " (not looking for updates)"
    from .updates import UpdateError, is_newer, latest_release

    try:
        release = latest_release(settings.updates.repository, timeout=5)
    except UpdateError as error:
        return f" (couldn't look for updates: {error})"
    if release is not None and is_newer(release):
        return f" · {release.version} is out: {LAUNCHER} update"
    return " (the newest)"


def cmd_rcon(args: argparse.Namespace) -> int:
    from .rcon import RconError, one_shot

    settings = _load(args.config)
    if not args.words:
        out.fail(f"Name a console command, e.g. {LAUNCHER} rcon players")
        return 2
    if args.server:
        try:
            server = settings.server(args.server)
        except KeyError:
            out.fail(f"No server named {args.server}")
            return 2
    elif len(settings.servers) == 1:
        server = settings.servers[0]
    else:
        out.fail("Several servers: name one with --server")
        return 2
    line = " ".join(args.words)
    if running_supervisor(settings):  # through the supervisor's own connection: no console spot used
        reply = mailbox.send(settings.records, "rcon", server.name, text=line, by=args.by or "")
        print(reply.text or reply.message)
        return 0 if reply.ok else 1
    try:
        reply = asyncio.run(one_shot(server, line, by=args.by))
    except (RconError, OSError, TimeoutError) as error:
        out.fail(_explain(server.name, error))
        return 1
    print(reply.text)
    return 0 if reply.ok else 1


def cmd_ctrl_break(args: argparse.Namespace) -> int:
    from .winproc import send_ctrl_break

    return send_ctrl_break(args.pid)


HELP_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Servers", [
        ("status", "what each server is doing"),
        ("stop [server…] [--now]", "stop servers; none named: all of them, then the supervisor"),
        ("restart [server…] [--now|--after-game]", "restart servers through the running supervisor; players read a "
                                                    "countdown unless --now (--reason \"text\")"),
        ("say <server|all> <text>", "a [Server] line in the chat"),
    ]),
    ("Live", [
        ("rcon [-s server] <command>", "one console command through the running supervisor's connection "
                                       "(--by name)"),
        ("watch [server|supervisor]", "follow a server's console output, or the supervisor's log, here"),
        ("check", "check the settings, servers and commands without starting anything"),
    ]),
    ("Set up", [
        ("new <name> [--package name] …", "a new server folder in SERVERS/, from a package, checked and started"),
        ("package [list|show|save] …", "the packages in PACKAGES/; save <server> [name] keeps a server as one"),
        ("content [-s server] [words]", "what a server can play; --import [--from folder] copies MCC's variants in"),
        ("playlist [-s server] [list|show|new|use]", "a server's playlists"),
        ("workshop [-s server] [list|add|remove|install]", "a server's Workshop items"),
        ("steam [status|install|login <account>|path]", "SteamCMD for Workshop items"),
        ("dedicated [-s server] <command…>", "any of the server program's own commands, e.g. dedicated check"),
        ("init [--force]", "write settings.toml, commands.toml and automod.txt (the first start does this)"),
    ]),
    ("Program", [
        ("start [--no-menu] [--hold]", "open the window; --no-menu is a plain log with typed commands (a task at boot)"),
        ("close", "close the running supervisor; the servers keep running"),
        ("update [--check] [--yes]", "a new version of the supervisor from GitHub, installed if you say yes"),
        ("update --program [--now]", "a new release of the server program, put in place of every copy"),
    ]),
]


def help_lines() -> list[list[tuple[str, str | None]]]:
    """The help as lines with colours: the commands grouped as the dashboard's keys are."""
    from .tui import format_listing, heading_parts

    width = out.width
    lines: list[list[tuple[str, str | None]]] = [
        [(f" RECLAIMER SUPERVISOR {__version__}", "white")],
        [(" runs your Project Reclaimer servers from one window and keeps them up, with automod and in-game "
          "commands for the roles you pick", "gray")],
        [(" " + "─" * (width - 1), "gray")],
        [(f"  {LAUNCHER}", "white"), ("  with no command: open the window (the dashboard), which is the supervisor",
                                      None)],
        [(f"  {LAUNCHER} <command> …", "white"), ("  one of these; each command's own --help lists its options", None)],
    ]
    for group, commands in HELP_GROUPS:
        lines.append([("", None)])
        lines.append(heading_parts(group, width))
        lines.extend(format_listing(list(commands), width, index=False, wrap=True, widest=48))
    lines.append([("", None)])
    lines.append([(f"  -c <file> uses another settings file (default: {SETTINGS_FILE} next to the launcher, or "
                   "$RECLAIMER_SUPERVISOR_CONFIG); the other files sit beside it.", "gray")])
    return lines


def print_help() -> None:
    for parts in help_lines():
        out.parts(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=LAUNCHER,
        description="Reclaimer Supervisor: runs your Project Reclaimer servers from one window and keeps them up, "
                    "with automod and in-game commands for the roles you pick.")
    parser.print_help = lambda file=None: print_help()  # type: ignore[method-assign]  the grouped help above
    parser.format_help = lambda: "\n".join("".join(t for t, _ in parts) for parts in help_lines()) + "\n"  # type: ignore[method-assign]
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                        help=f"its settings file (default: {SETTINGS_FILE} here, or $RECLAIMER_SUPERVISOR_CONFIG); "
                             "the other files sit next to it")
    commands = parser.add_subparsers(dest="command", metavar="command")

    start = commands.add_parser("start", aliases=["run"], help="Open the window: the dashboard that runs the servers")
    start.add_argument("-v", "--verbose", action="store_true", help="log debugging detail")
    start.add_argument("--hold", action="store_true", help="keep the window open if it stops with a problem")
    start.add_argument("--no-menu", action="store_true", help="no dashboard: a plain log, with typed commands")
    start.add_argument("--child", action="store_true", help=argparse.SUPPRESS)  # the supervisor itself; see _start_loop
    start.set_defaults(func=cmd_start)

    status = commands.add_parser("status", help="What each server is doing")
    status.set_defaults(func=cmd_status)

    check = commands.add_parser("check", help="Check the settings, servers and commands without starting")
    check.set_defaults(func=cmd_check)

    stop = commands.add_parser("stop", help="Stop servers (all of them and the supervisor when none is named)")
    stop.add_argument("servers", nargs="*", help="server names; none: every server, then the supervisor")
    stop.add_argument("--now", action="store_true", help="no countdown for the players")
    stop.add_argument("--wait", type=float, default=600, help="seconds to wait (default: %(default)s)")
    stop.set_defaults(func=cmd_stop)

    close = commands.add_parser("close", help="Close the running supervisor; the servers keep running")
    close.add_argument("--wait", type=float, default=30, help="seconds to wait (default: %(default)s)")
    close.set_defaults(func=cmd_close)

    restart = commands.add_parser("restart", help="Restart servers (needs the supervisor running)")
    restart.add_argument("servers", nargs="*", help="server names; none: every server")
    restart.add_argument("--now", action="store_true", help="no countdown for the players")
    restart.add_argument("--after-game", action="store_true", help="once the game being played ends")
    restart.add_argument("--reason", help="what players read")
    restart.set_defaults(func=cmd_restart)

    say = commands.add_parser("say", help="A [Server] chat line on a server (or all)")
    say.add_argument("server", help="a server's name, or all")
    say.add_argument("words", nargs=argparse.REMAINDER)
    say.set_defaults(func=cmd_say)

    update = commands.add_parser("update", help="Look for a new version on GitHub, and install it if you say yes: "
                                                "the supervisor's, or with --program the server program's")
    update.add_argument("--check", action="store_true", help="only say whether there's a new version")
    update.add_argument("-y", "--yes", action="store_true", help="install it without asking")
    update.add_argument("--program", action="store_true",
                        help="the server program (Project Reclaimer): put in place of every copy, each running server "
                             "restarted after its game")
    update.add_argument("--now", action="store_true", help="with --program: restart the servers at once, mid-game")
    update.add_argument("--wait", type=float, default=300, help="seconds to wait for a running supervisor to update")
    update.set_defaults(func=cmd_update)

    rcon = commands.add_parser("rcon", help="Run one console command on a server, e.g. `rcon players`")
    rcon.add_argument("-s", "--server", help="the server, by name, when there are several")
    rcon.add_argument("--by", help="the name players read as who acted")
    rcon.add_argument("words", nargs=argparse.REMAINDER)
    rcon.set_defaults(func=cmd_rcon)

    watch = commands.add_parser("watch", help="Follow a server's console output, or the supervisor's log")
    watch.add_argument("what", nargs="?", help="a server's name, or supervisor")
    watch.add_argument("--server-log", action="store_true", help="the server's own data/<server>/server.log")
    watch.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    watch.set_defaults(func=cmd_watch)

    from .setupcli import add_parsers

    add_parsers(commands, _load, LAUNCHER)

    init = commands.add_parser("init", help=f"Write {SETTINGS_FILE}, {COMMANDS_FILE} and automod.txt "
                                            "(start does this the first time)")
    init.add_argument("server_folder", nargs="?", help="one server folder (default: SERVERS/ next to the settings)")
    init.add_argument("--backend", choices=BACKENDS, help="default: picked from what's in the server folders")
    init.add_argument("--force", action="store_true", help=f"replace {SETTINGS_FILE} and {COMMANDS_FILE}")
    init.set_defaults(func=cmd_init)

    ctrl_break = commands.add_parser("_ctrl-break")  # internal: see winproc.py
    ctrl_break.add_argument("pid", type=int)
    ctrl_break.set_defaults(func=cmd_ctrl_break)
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)
