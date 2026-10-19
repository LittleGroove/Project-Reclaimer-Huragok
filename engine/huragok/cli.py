"""Huragok's commands: start, status, stop, restart, say, rcon, watch, check, update, init.

Hosts run them through start.cmd (Windows) or start.sh (Linux) at the root; with no
command, those open the window (the dashboard), which is Huragok itself.
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
import traceback
from importlib import resources
from pathlib import Path
from typing import Any

import psutil

from . import __version__, mailbox
from .config import (BACKENDS, COMMANDS_FILE, SERVERS_FOLDER, SETTINGS_FILE, ConfigError, ServerConfig, Settings,
                     find_server_folders, load)
from .packages import PACKAGES_FOLDER
from .playlists import old_labels, playlist_files
from .shell import console as out

DEFAULT_CONFIG = os.environ.get("HURAGOK_CONFIG", SETTINGS_FILE)
LAUNCHER = os.environ.get("HURAGOK_LAUNCHER", "huragok")  # set by start.cmd / start.sh
# Exit codes. Each deliberate end has its own, so the start loop (and a script) can tell them from a crash: an
# uncaught exception is 1, a process killed by Windows or a signal some other number.
FAILED = 1  # it didn't work (and what Python itself exits with on an uncaught exception)
SETTINGS_PROBLEM = 2  # settings.toml (or commands.toml) has a mistake, or a name or option isn't known
SET_UP = 3  # files written, look them over and start again (start.cmd keeps the window open)
ALREADY_RUNNING = 4  # start: Huragok already runs from this folder
NOT_RUNNING = 5  # status: Huragok isn't running from this folder
RELAUNCH = 10  # exit code of the supervisor itself: a new version is installed, run it
DELIBERATE = (0, SET_UP, ALREADY_RUNNING)  # ends the start loop takes as they are
CRASH_WAITS = (5, 15, 30, 60, 300)  # seconds before starting Huragok again after each crash in a row; the last repeats
GOOD_RUN_SECONDS = 600  # a Huragok that ran this long was fine: the next crash waits the shortest time again
FAILED_START_SECONDS = 120  # a new version that ends within this long of starting didn't start: the old one goes back
SETTINGS_RETRY_SECONDS = 300  # with --no-menu, a settings problem is looked at again this often
LOCK_FILE = "huragok.pid"
OLD_LOCK_FILE = "supervisor.pid"  # 0.2.1's name for it
LAUNCHER_LOG = "logs/launcher.log"  # where the start loop writes when settings.toml can't be read
FAILED_UPDATE = "failed-update.json"  # in records/: a version that didn't start and was taken back out
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
    """huragok.log names the module that spoke without the package prefix: `manager:`, not
    `huragok.manager:`."""

    def format(self, record: logging.LogRecord) -> str:
        record.short = record.name.removeprefix("huragok.")
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
        from .tui import TaggedFormatter

        stream.setFormatter(TaggedFormatter())
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
        raise SystemExit(SETTINGS_PROBLEM) from None


def _log_name(settings: Settings) -> str:
    """Where the log is, as the host would find it: logs/huragok.log."""
    if settings.log_file is None:
        return "the log"
    try:
        return settings.log_file.relative_to(settings.home).as_posix()
    except ValueError:
        return str(settings.log_file)


def _template(name: str) -> str:
    return resources.files("huragok").joinpath(f"templates/{name}").read_text("utf-8")


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
            return "docker", {"compose_file": compose}, f"found {compose}: the Superintendent runs the containers"
    for folder in folders:
        if exe := find_executable(folder):
            return "process", {"executable": exe.name}, f"found {exe.name}: the Superintendent runs the servers"
    return "process", {}, "the Superintendent runs the servers: project-reclaimer.exe in each server folder"


def _toml_text(value: str) -> str:
    """A value for settings.toml as the host would type it: a 'literal string' when it can be one, so a Windows
    path's backslashes stay backslashes; otherwise an escaped "basic string"."""
    from .tomledit import _toml_string

    if "'" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return _toml_string(value)
    return f"'{value}'"


def write_files(home: Path, server_folder: str | None = None, backend: str | None = None,
                force: bool = False, settings_path: Path | None = None) -> list[str]:
    """settings.toml (or the settings file named with -c), commands.toml, automod.txt and names.txt, each only if
    it isn't there (or force; never the word lists). Returns what was written."""
    import tomllib

    from .permissions import render_commands_toml

    home.mkdir(parents=True, exist_ok=True)
    written = []
    settings_path = settings_path or home / SETTINGS_FILE
    if force or not settings_path.exists():
        servers_folder, single, folders = detect_layout(home)
        if server_folder is not None:
            single = server_folder
            folders = [Path(home / server_folder).resolve()]
        picked, options, why = pick_backend(folders)
        backend = backend or picked
        if not folders:
            (home / servers_folder).mkdir(exist_ok=True)
        values = {"servers_folder": servers_folder, "server_folder": single, "backend": backend,
                  "executable": options.get("executable", "project-reclaimer.exe"),
                  "compose_file": options.get("compose_file", "compose.yaml")}
        text = _template(SETTINGS_FILE).replace("{{", "\x00").replace("}}", "\x01")  # doubled braces are literal
        for key, value in values.items():
            text = text.replace("{" + key + "}", _toml_text(value))
        text = text.replace("\x00", "{").replace("\x01", "}")
        try:  # what's written must read back as written: a path is the likeliest thing to go wrong
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{settings_path.name} as it would be written can't be read ({error}); "
                              "nothing was written") from None
        if (data.get("servers_folder"), data.get("server_folder")) != (servers_folder, single):
            raise ConfigError(f"{settings_path.name} as it would be written doesn't say the folders given; "
                              "nothing was written")
        settings_path.write_text(text, encoding="utf-8")
        where = (f"{len(folders)} server folder(s) found" if folders
                 else f"no server folders yet: put each one in {servers_folder}{os.sep}")
        written.append(f"{settings_path.name:<15} how it runs. {where}; backend {backend}"
                       + (f" ({why})" if backend == picked else ""))
    commands_path = home / COMMANDS_FILE
    if force or not commands_path.exists():
        commands_path.write_text(render_commands_toml(), encoding="utf-8")
        written.append(f"{COMMANDS_FILE:<15} who can use which in-game command. Put your player ID under [owner]")
    words = home / "automod.txt"
    if not words.exists():
        words.write_text(_template("automod.txt"), encoding="utf-8")
        written.append("automod.txt     Vergil's word list (automod), empty for now; it starts in dry run")
    names = home / "names.txt"
    if not names.exists():
        names.write_text(_template("names.txt"), encoding="utf-8")
        written.append("names.txt       Vergil's banned names: kicked as they join until they change it")
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
    path = Path(args.config).resolve()
    home = path.parent
    if path.exists() and not args.force:
        out.fail(f"{path} already exists; --force replaces it (and {COMMANDS_FILE}).")
        return FAILED
    try:
        written = write_files(home, args.server_folder, args.backend, force=args.force, settings_path=path)
    except ConfigError as error:
        out.fail(str(error))
        return SETTINGS_PROBLEM
    out.heading("Written", note=str(home))
    out.listing(_written_rows(written), index=False, wrap=True)
    out.write()
    try:
        load(path)
    except ConfigError as error:
        out.fail(f"Settings problem in what was written: {error}")
        return SETTINGS_PROBLEM
    out.say(f"Next: {LAUNCHER}{_config_option(path)} check")
    return 0


def _config_option(path: Path) -> str:
    """` -c mine.toml` when the settings file isn't the usual one, for the next command to type."""
    return "" if path.name == SETTINGS_FILE else f" -c {path.name}"


# Running ------------------------------------------------------------------------------------

def _process_alive(pid: int, create_time: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - create_time) < 1
    except psutil.Error:
        return False


def _lock(settings: Settings, name: str = LOCK_FILE) -> dict:
    try:
        lock = json.loads((settings.records / name).read_text(encoding="utf-8"))
        return lock if isinstance(lock, dict) else {}
    except (OSError, ValueError):
        return {}


def _live_pid(lock: Any) -> int | None:
    """The process a lock names, if that very process is still running (a reused process ID doesn't count)."""
    try:
        pid, created = int(lock["pid"]), float(lock["create_time"])
    except (KeyError, TypeError, ValueError):
        return None
    return pid if _process_alive(pid, created) else None


def running_supervisor(settings: Settings) -> int | None:
    """The process ID of a Huragok already running from this folder, if any: records/huragok.pid, or 0.2.1's
    records/supervisor.pid (its window may still be open beside a newer copy). While Huragok itself starts again
    (an update, a crash) the lock names its start loop, which holds the folder for it."""
    for name in (LOCK_FILE, OLD_LOCK_FILE):
        pid = _live_pid(_lock(settings, name))
        if pid is not None and pid != os.getpid():
            return pid
    return None


def _lock_text(**extra: Any) -> str:
    return json.dumps({"pid": os.getpid(), "create_time": psutil.Process().create_time(), "version": __version__,
                       **extra})


def _create_lock(path: Path, text: str) -> bool:
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


def _replace_lock(path: Path, text: str) -> bool:
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


def _take_lock(settings: Settings, *, launcher: bool = False) -> Path:
    """Become the one Huragok of this folder, or raise AlreadyRunning. Two started at once can't both win.

    launcher: the start loop's own claim, held while Huragok itself starts again (an update, a crash) so that
    nothing else takes the folder meanwhile. The Huragok it starts takes the lock over from it, and hands it back
    as it ends.
    """
    path = settings.records / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if (old := _live_pid(_lock(settings, OLD_LOCK_FILE))) is not None and old != os.getpid():
        raise AlreadyRunning(old)  # a 0.2.1 window, still open
    text = _lock_text(launcher=True) if launcher else _lock_text()
    for _ in range(50):
        if _create_lock(path, text):
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
            pid = _live_pid(lock)
            if pid == os.getpid():
                return path  # ours already: the start loop, after Huragok handed it back
            if pid is not None:
                if lock.get("launcher") and not launcher and _started_by(pid):
                    # The start loop that started this process holds it for us: take it over, to hand back.
                    _replace_lock(path, _lock_text(launched_by=lock))
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


def _started_by(pid: int) -> bool:
    """`pid` started this process: its parent, or its grandparent (on Windows a venv's python.exe is a small
    launcher that starts the real Python as its own child)."""
    try:
        return pid in [parent.pid for parent in psutil.Process().parents()[:3]]
    except psutil.Error:
        return pid == os.getppid()


def _release_lock(path: Path) -> None:
    """Give the lock up, if it's still ours: back to the start loop that lent it (Huragok is starting again), or
    gone."""
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(lock, dict) or lock.get("pid") != os.getpid():
        return
    lent = lock.get("launched_by")
    if isinstance(lent, dict) and _live_pid(lent) is not None and _replace_lock(path, json.dumps(lent)):
        return
    path.unlink(missing_ok=True)


def _summary(settings: Settings) -> None:
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
        from .parts import PARTS

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
    if not getattr(args, "hold", False):
        return _start(args)
    # A double-clicked window closes the moment this returns: whatever stopped it (a settings problem, an error
    # writing the files, a crash), keep it open to read why.
    try:
        return _start(args)
    except SystemExit as stop:
        if isinstance(stop.code, str):
            out.fail(stop.code)
        return _hold(args, stop.code if isinstance(stop.code, int) else FAILED if stop.code else 0)
    except Exception:  # noqa: BLE001 - shown, then held
        traceback.print_exc()
        out.fail(f"Huragok stopped with the error above. `{LAUNCHER} check` may say more.")
        return _hold(args, FAILED)


def _start(args: argparse.Namespace) -> int:
    path = Path(args.config).resolve()
    home = path.parent
    if not path.exists():
        out.banner()
        out.title(f"HURAGOK {__version__}: THE FIRST DROP", f"setting up in {home}")
        out.heading("Written")
        out.listing(_written_rows(write_files(home, settings_path=path)), index=False, wrap=True)
        if not find_server_folders(home, home / SERVERS_FOLDER, None) and not (home / "dedicated.toml").exists() \
                and not (home.parent / "dedicated.toml").exists():
            out.heading("Server folders")
            for line in _layout_help(home):
                out.say(line)
        out.write()
        out.ok(f"Look these over, then start again: {LAUNCHER}{_config_option(path)}")
        return _hold(args, SET_UP)
    for line in write_files(home, settings_path=path):  # put back any file that was deleted, with its defaults
        out.say(f"Wrote {line}")
    settings = _load_for_start(args)
    if pid := running_supervisor(settings):
        if _want_menu(args):
            outcome = _remote_window(settings)
            if outcome != "takeover":
                return 0
            out.note("The supervising window closed; this one takes over.")
        else:
            out.fail(f"Huragok is already running from this folder (process {pid}). To stop it: {LAUNCHER} stop")
            return _hold(args, ALREADY_RUNNING)
    return _hold(args, _start_loop(args, settings))


def _load_for_start(args: argparse.Namespace) -> Settings:
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
            before = _mtime(path)
            try:
                _pause(SETTINGS_RETRY_SECONDS, wake=lambda: _mtime(path) != before)
            except KeyboardInterrupt:
                raise SystemExit(SETTINGS_PROBLEM) from None


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


def _start_loop(args: argparse.Namespace, settings: Settings) -> int:
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
    from . import tui, updates  # noqa: F401 - loaded now, while the files on disk are still this version's

    home = settings.home
    command = [sys.executable, "-m", "huragok", "-c", str(settings.path), "start", "--child"]
    if args.verbose:
        command.append("--verbose")
    if args.no_menu:
        command.append("--no-menu")
    menu = _want_menu(args)
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
        lock = _take_lock(settings, launcher=True)
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
                _take_lock(settings, launcher=True)  # hold the folder while Huragok starts again
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
                before = _mtime(settings.path)
                # Nothing here could answer start.cmd stop or status meanwhile, so the folder is let go: they see
                # Huragok isn't running and act by themselves (stop stops the servers itself).
                _release_lock(lock)
                if not _pause(SETTINGS_RETRY_SECONDS, stopped=lambda: stopping,
                              wake=lambda: _mtime(settings.path) != before):
                    return code
                try:
                    _take_lock(settings, launcher=True)
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
                                        f"{wait:.0f} s. {_log_name(settings)} says why.")
                ended = time.time()  # someone asking since (start.cmd stop, close): start it now to answer
                wake = lambda: _requests_waiting(settings.records, since=ended)  # noqa: E731
            if not _pause(wait, stopped=lambda: stopping, wake=wake):
                return code
    finally:
        _release_lock(lock)


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
    _say(settings, "WARNING", f"Huragok {back} is back in place of {failed} ({_log_name(settings)} says why {failed} "
                              f"didn't start). Starting {back}.")
    return True


class RestoreError(Exception):
    pass


def _clock() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


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
        return any(not p.name.endswith(".reply.json") and _mtime(p) > since
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
    from .tui import is_terminal

    if is_terminal() and out.colour:
        try:
            sys.stdout.write("\x1b[?25h")  # a Huragok that crashed may have left the cursor hidden
        except (OSError, ValueError):
            pass
    (out.note if level == "INFO" else out.warn if level == "WARNING" else out.fail)(message)
    _launcher_log(settings, settings.home, level, message)


def _version_on_disk(home: Path) -> str | None:
    """The version in home/engine now (an update changes it under this process), when this copy runs from there."""
    from . import updates

    if not updates.installed_here(home):
        return None
    try:
        return updates.version_in(home)
    except (OSError, updates.UpdateError):
        return None


def _restore_previous(home: Path, version: str, failed: str) -> str:
    """Put back what the latest update replaced (records/last-update.json names its backup, records/previous-<V>/),
    in place of `failed`, which didn't start. Returns the version back on disk. The updates module's own put_back
    does it, loaded by this process before the update: renames only, all or nothing."""
    from . import updates

    installed = updates.last_update(home)
    if installed is None:
        raise RestoreError(f"records/{updates.LAST_UPDATE} doesn't say which files the update replaced, so the "
                           f"backup of {version} isn't known")
    if installed.version != failed and failed != "the new version":
        raise RestoreError(f"records/{updates.LAST_UPDATE} is about {installed.version}, not {failed}; nothing was "
                           "changed")
    try:
        return updates.put_back(home, installed)
    except (updates.UpdateError, OSError) as error:
        raise RestoreError(str(error)) from None


def _mark_failed_update(settings: Settings, failed: str, previous: str, code: int) -> None:
    """records/failed-update.json: the version that didn't start, so it isn't simply installed again."""
    try:
        (settings.records / FAILED_UPDATE).write_text(json.dumps({
            "version": failed, "restored": previous, "exit_code": code, "time": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _run_supervisor(args: argparse.Namespace) -> int:
    from .core import Supervisor
    from .tui import RecentLog, disable_quick_edit

    settings = _load(args.config)
    disable_quick_edit()  # a click in the window must never pause Huragok (and its servers' care)
    menu_mode = _want_menu(args)
    recent = RecentLog() if menu_mode else None
    _setup_logging(settings, args.verbose, console=not menu_mode, recent=recent)
    try:
        lock = _take_lock(settings)
    except AlreadyRunning as error:
        out.fail(f"Huragok is already running from this folder (process {error.pid}).")
        return ALREADY_RUNNING
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
            # In the window Ctrl+C is a key that asks first (Q), never a signal that ends every server's care.
            if os.name == "nt":
                signal.signal(signal.SIGINT, keyboard.ctrl_c)
            else:
                loop.add_signal_handler(signal.SIGINT, keyboard.ctrl_c)
                signal.signal(signal.SIGTSTP, signal.SIG_IGN)  # Ctrl+Z would freeze the supervisor
            menu = Menu(LocalController(supervisor, recent), Terminal(), keyboard, recent)  # type: ignore[arg-type]
        try:
            await supervisor.run(menu)
        finally:
            relaunch = supervisor.relaunch
            if keyboard is not None:
                keyboard.stop()

    try:
        _summary(settings)
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - the start loop starts it again; the log must say why it stopped
        logging.getLogger("huragok").critical("Huragok stopped with an error", exc_info=True)
        if menu_mode:  # the log isn't on the screen in the window
            traceback.print_exc()
        return FAILED
    finally:
        _release_lock(lock)  # back to the start loop, if it lent it
    return RELAUNCH if relaunch else 0


def cmd_update_program(args: argparse.Namespace, settings: Settings) -> int:
    """`update --program`: a new release of the server program, installed at a quiet moment."""
    from .serverupdates import download_program, install_program, latest_program_release
    from .tui import is_terminal
    from .updates import UpdateError, summary

    when = "now" if args.now else "after_game"
    if pid := running_supervisor(settings):
        reply = _ask(settings, "check_program_update", wait=60)
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
        out.note(f"Asking Huragok (process {pid}) to install it…")
        reply = _ask(settings, "update_program", wait=UPDATE_WAIT, when=when, why=f"asked by {LAUNCHER} update")
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
    out.note("Huragok isn't running, so the servers' versions aren't known; the file is put in place without restarts.")
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
    out.ok(f"Server program {release.version} installed. Start Huragok to run the servers on it: {LAUNCHER}")
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
        out.ok(f"Huragok {__version__} is the newest version.")
        return 0
    out.write(f"Update available: Huragok {release.version} (this is {__version__})", "white")
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
        out.note(f"Asking Huragok (process {pid}) to install it…")
        # It answers once the download and its Python packages are in, which can take minutes.
        reply = _ask(settings, "update", wait=UPDATE_WAIT, why=f"asked by {LAUNCHER} update")
        if not reply.ok:
            out.fail(reply.message)
            return 1
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            time.sleep(1)
            if _lock(settings).get("version") == release.version and running_supervisor(settings):
                out.ok(f"Updated to {release.version}, and running again.")
                return 0
        out.fail(f"Not done after {args.wait:.0f} s; its window or {_log_name(settings)} says why.")
        return 1
    out.note("Installing…")
    try:
        version = update(settings.home, release)
    except (UpdateError, OSError, subprocess.SubprocessError) as error:
        out.fail(f"Update failed, still on {__version__}: {error}")
        return 1
    out.ok(f"Updated to {version}. Start it as usual: {LAUNCHER}")
    return 0


# Talking to a running Huragok ---------------------------------------------------------------------

UPDATE_WAIT = 900.0  # an update's answer comes after the download and pip (pip alone may take 10 minutes)
RCON_WAIT = 30.0  # Huragok's own console command waits 15 s, plus picking the request up
SAY_WAIT = 10.0  # per server: `say all` speaks on one server after another
ASK_WAIT = 15.0


def _ask(settings: Settings, verb: str, server: str = "all", wait: float = ASK_WAIT, **options: Any) -> mailbox.Reply:
    """A request to the running Huragok. Its answer not coming in time doesn't mean it isn't doing it."""
    started = time.monotonic()
    reply = mailbox.send(settings.records, verb, server, wait=wait, **options)
    if not reply.ok and time.monotonic() - started >= wait:
        return mailbox.Reply(False, f"No answer from Huragok after {wait:.0f} s. It may still be doing it: "
                                    f"{_log_name(settings)} (or its window) says what happened.")
    return reply


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
    """Stop servers (warning players unless --now). With no server named: every server, then Huragok."""
    settings = _load(args.config)
    pid = running_supervisor(settings)
    if pid:
        if args.servers:
            ok = True
            for name in args.servers:
                reply = _ask(settings, "stop", name, immediate=args.now)
                _print_reply(reply)
                ok = ok and reply.ok
            return 0 if ok else 1
        reply = _ask(settings, "stop_all", immediate=args.now)
        out.say(f"Asked Huragok (process {pid}) to stop every server"
                f"{' now' if args.now else ', warning players first'}, then itself: {reply.message or 'ok'}")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            if running_supervisor(settings) is None:
                out.ok("Stopped.")
                return 0
            time.sleep(0.5)
        out.fail(f"Still running after {args.wait:.0f} s; its window or {_log_name(settings)} says why.")
        return 1

    from .events import EventBus
    from .lifecycle import LifecycleError, LifecycleUnavailable
    from .state import ServerState

    try:
        servers = [settings.server(name) for name in args.servers]
    except KeyError as error:
        out.fail(f"No server named {error}. Servers: {', '.join(s.name for s in settings.servers) or 'none'}")
        return SETTINGS_PROBLEM
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    backend = _make_backend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    if backend is None:
        return SETTINGS_PROBLEM
    not_running: list[str] = []
    try:
        if servers:
            backend.adopt()
            stopped = []
            for server in servers:
                if backend.alive(server) is False:  # the backend sees processes, and this one isn't ours, running
                    not_running.append(server.name)
                    continue
                asyncio.run(backend.stop(server))
                stopped.append(server.name)
        else:
            stopped = asyncio.run(backend.stop_all())
    except (LifecycleError, LifecycleUnavailable) as error:
        out.fail(str(error))
        return 1
    if stopped:
        out.ok(f"Stopped: {', '.join(stopped)}.")
    if not_running:
        out.note(f"Not running (or not started by Huragok, which stops only the servers it started): "
                 f"{', '.join(not_running)}.")
    if not stopped and not not_running:
        out.ok("No servers of ours were running.")
    return 0


def _make_backend(settings: Settings, bus: Any, states: dict) -> Any:
    """The backend for a command; None (said why) when its section in settings.toml can't be used."""
    from .lifecycle import make_backend

    try:
        return make_backend(settings, bus, states)
    except ConfigError as error:
        out.fail(f"Settings problem: {error}")
        return None


def cmd_close(args: argparse.Namespace) -> int:
    """Close the running Huragok; the servers keep running (what Q does in the window)."""
    settings = _load(args.config)
    pid = running_supervisor(settings)
    if not pid:
        out.note("Huragok isn't running from this folder.")
        return 0
    reply = _ask(settings, "close")
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
        out.fail(f"Huragok isn't running from this folder. A restart needs the Superintendent to bring the server back "
                 f"and look after it: start {LAUNCHER}, then restart from its window (R) or run this again.")
        return 1
    ok = True
    when = "after_game" if args.after_game else "now"
    for name in args.servers or ["all"]:
        reply = _ask(settings, "restart", name, immediate=args.now, reason=args.reason or "", when=when)
        _print_reply(reply)
        ok = ok and reply.ok
    return 0 if ok else 1


def cmd_say(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    text = " ".join(args.words).strip()
    if not text:
        out.fail("Nothing to say.")
        return 2
    every = args.server.lower() == "all"
    if running_supervisor(settings):
        # One server after another, each with its own console's time to answer.
        wait = SAY_WAIT * max(1, len(settings.servers)) + ASK_WAIT if every else SAY_WAIT + ASK_WAIT
        reply = _ask(settings, "say", args.server, wait=wait, text=text)
        _print_reply(reply)
        return 0 if reply.ok else 1
    from .rcon import RconError, one_shot

    try:
        targets = settings.servers if every else [settings.server(args.server)]
    except KeyError:
        out.fail(f"No server named {args.server}. Servers: {', '.join(s.name for s in settings.servers) or 'none'}")
        return SETTINGS_PROBLEM
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
        live = live if isinstance(live, dict) else {}
    except (OSError, ValueError):
        pass
    if pid:  # the lock says it runs; live.json only says how recently it wrote what it sees
        from .menu import table_lines

        from .lore import mood

        try:
            age = time.time() - float(live.get("updated") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        out.title(f"HURAGOK {live.get('version') or _lock(settings).get('version') or ''}",
                  f"running (process {pid}) · backend {live.get('backend') or settings.backend} · {settings.home}")
        if _lock(settings).get("launcher"):
            out.warn(f"Huragok is starting again (an update, or after it stopped unexpectedly): its start loop, "
                     f"process {pid}, holds the folder meanwhile. {_log_name(settings)} says why.")
        elif age >= LIVE_STALE_SECONDS:
            how_old = "missing" if age == float("inf") else f"{age:.0f} s old"
            out.warn(f"Running, but its status file (records/live.json) is {how_old}: it may be busy, or starting "
                     "up." + (" What it showed last:" if live.get("servers") else ""))
        if live.get("servers"):
            face = mood(live.get("servers") or [])
            out.write(f" {face.line()}", face.colour)
            for text, colour in table_lines(live.get("servers") or [], out.width):
                out.write(text, colour)
        return 0
    out.title(f"HURAGOK {__version__}", f"not running · {settings.home}")
    out.warn("Huragok isn't running from this folder, so the Superintendent isn't watching the servers.")
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
    return NOT_RUNNING


LIVE_STALE_SECONDS = 15
LOG_WORDS = ("huragok", "supervisor", "superintendent", "vergil", "events", "log")  # `watch` these: Huragok's log


def cmd_watch(args: argparse.Namespace) -> int:
    from .watch import follow

    settings = _load(args.config)
    what = (args.what or "").strip().strip('"')
    server = None
    if what:
        try:
            server = settings.server(what)  # a server really called "Vergil" is that server
        except KeyError:
            if what.lower() not in LOG_WORDS:
                out.fail(f"No server named {what}. Servers: {', '.join(s.name for s in settings.servers) or 'none'}")
                return SETTINGS_PROBLEM
    if server is None:
        path = settings.log_file
        if path is None:
            out.fail("Huragok keeps no log file (log_file is empty).")
            return 1
    else:
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
    from .part import commands_of
    from .parts import PARTS
    from .permissions import ROLES, TITLES, all_commands
    from .state import ServerState

    settings = _load(args.config)
    ok = True
    warned = bool(settings.retired or settings.notes)
    out.title("CHECK", f"Huragok {__version__}{_update_note(settings)}")
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
    backend = _make_backend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    if backend is None:
        return SETTINGS_PROBLEM
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
        problems = [error for error in server.errors if "RCON" not in error] + _backend_problems(backend, server)
        if endpoint:
            rows.append(("RCON", f"{endpoint.host}:{endpoint.port} · password set · host key "
                                 f"{'found' if server.host_key() else 'not found (server not started?)'}"))
        else:
            rows.append(("RCON", "off"))
            problems.insert(0, "RCON is off: set [rcon] password in dedicated.toml")
        rows.append(("updates", updates_text(settings, server)))
        if status_age is None:
            rows.append(("running", "unknown (no status.json)"))
        else:
            rows.append(("running", f"{'yes' if status_age < 30 else 'no'} (status.json written {status_age:.0f} s ago)"))
        out.fields(rows)
        for problem in problems:
            out.warn(f"PROBLEM  {problem}")
            ok = False
        for line in _label_warnings(settings, server):
            out.warn(line)
    if settings.problems:
        out.heading("Left out")
        for problem in settings.problems:
            out.warn(f"PROBLEM  {problem}")
        ok = False
    if settings.retired:
        out.heading("Settings that do nothing")
        for line in settings.retired:
            out.warn(line)
    if settings.notes:
        out.heading("Values from an older version")
        for line in settings.notes:
            out.warn(line)

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
            warnings = cls.preflight(part.options, settings.home)
        except ValueError as error:
            notes.append((f"PROBLEM  [{name}] {error}", True))
            continue
        # The keys that aren't settings are named above, with what they were probably meant to be.
        notes.extend((f"{name}: {warning}", False) for warning in warnings if not _named_above(name, warning))
        if problem := _part_config_problem(settings, name, cls):
            notes.append((f"PROBLEM  [{name}] {problem} ({name} wouldn't start)", True))
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
           "member": f" ({len(permissions.members['member'])} listed)",
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
    out.note("Each role can also use the commands listed above it. No vote can kick a member or anyone above.")
    if not permissions.members["owner"]:
        out.warn(f"warning: nobody is owner yet: put your player ID under [owner] players in {COMMANDS_FILE}")
    for player_id, roles in permissions.doubles(a.lower() for a in admins).items():
        out.warn(f"warning: {player_id[:8]}… is listed as {' and '.join(roles)}: one role each, and {roles[0]} is "
                 f"the one that counts. Take the other line out of {COMMANDS_FILE} (or dedicated.toml's admins), "
                 "or !demote / !promote them in game, which keeps one")
    if permissions.path and permissions.path.exists() and "member" not in permissions.sections:
        out.note(f"{COMMANDS_FILE} is from before members: add [member] with players = [\"<player ID>\", …] to "
                 "keep regulars safe from votes to kick")
    out.write()
    if ok and warned:
        out.ok(f"Ready, but read the settings warnings above: {SETTINGS_FILE} may not say what you meant.")
    elif ok:
        out.ok("Everything looks ready.")
    else:
        out.warn("Fix the problems above, then check again.")
    return 0 if ok else 1


def _label_warnings(settings: Settings, server: ServerConfig) -> list[str]:
    """Choice labels in the server's playlists that its own program refuses. Which those are depends on its version
    (0.9.19 renamed "Vanilla characters" to "Halo 3": 0.9.18 refuses "Halo 3"), asked of the program file only when
    a playlist has one of the labels at all."""
    from .gameoptions import NEWEST

    files = [path for path in playlist_files(server.folder) if old_labels(path, NEWEST) or old_labels(path, "0")]
    if not files:
        return []
    version = _program_version(settings, server)
    found = []
    for path in files:
        if not (labels := sorted(set(old_labels(path, version)))):
            continue
        named = f"{path.name} names {', '.join(repr(label) for label in labels)}"
        if version:
            found.append(f"{named}, which Project Reclaimer {version} (this server's program) calls otherwise: the "
                         "Superintendent writes them as its program calls them as it starts the server")
        else:
            found.append(f"{named}, which no version of Project Reclaimer takes: the Superintendent writes the name "
                         "it takes as it starts the server")
    return found


def _program_version(settings: Settings, server: ServerConfig) -> str | None:
    """The version of the program file the server runs, as its `--version` says (asked once per file); None when
    it isn't known."""
    from .serverupdates import program_of, program_version, readable

    program = program_of(settings, server)
    version = program_version(settings, program) if program is not None else None
    return version if readable(version) else None


def _backend_problems(backend: Any, server: ServerConfig) -> list[str]:
    """What would stop the backend starting this server (a [process] command starting with {executable} is
    judged on the program it names: the backend does that itself)."""
    return list(backend.problems(server))


def _named_above(part: str, warning: str) -> bool:
    """A part's own "isn't a setting" warning for a key that check already named with what it was meant to be."""
    return part == "watchdog" and warning.endswith("isn't a watchdog setting, so it does nothing")


def _part_config_problem(settings: Settings, name: str, cls: Any) -> str | None:
    """What stops a part at the start (its check_config), without starting it: the part made as the
    Superintendent makes it, on a stand-in for the running Huragok."""
    from types import SimpleNamespace

    config = {**cls.defaults, **settings.parts[name].options}
    ctx = SimpleNamespace(name=name, config=config, home=settings.home, dry_run=True,
                          acts_as=str(config.get("name") or cls.acts_as or name.title()),
                          log=logging.getLogger(f"part.{name}"))
    try:
        cls(ctx).check_config()
    except ValueError as error:
        return str(error)
    except Exception as error:  # noqa: BLE001 - a part that needs the running Huragok to check itself
        logging.getLogger(__name__).debug("%s's check_config needs more than check gives it: %s", name, error)
    return None


def updates_text(settings: Settings, server: ServerConfig) -> str:
    """How a server keeps up: Project Reclaimer itself or by Huragok, and its Workshop mods."""
    text = "Project Reclaimer " + ("installs its own releases (auto_update)" + (
        "; Huragok steps in when one doesn't arrive" if settings.server_updates.fallback and
        settings.backend == "process" else "") if server.auto_update else "updated by Huragok (U)")
    if not server.workshop_items:
        return text
    mode = settings.workshop_install(server)
    shared = [s.name for s in settings.servers if s is not server and s.content_dir == server.content_dir]
    how = {"after_game": "new versions installed after the game",
           "when_empty": "new versions installed by the server once nobody's on",
           "off": "Workshop updates off"}[mode]
    if mode == "after_game" and shared:
        how = f"new versions installed by the server once nobody's on (content shared with {', '.join(shared)})"
    return f"{text} · {len(server.workshop_items)} Workshop item(s): {how}"


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
            server = settings.server(args.server)  # its name, its slug, or its number in the list
        except KeyError:
            out.fail(f"No server named {args.server}. Servers: "
                     f"{', '.join(s.name for s in settings.servers) or 'none'}")
            return 2
    elif len(settings.servers) == 1:
        server = settings.servers[0]
    else:
        out.fail("Several servers: name one with --server")
        return 2
    line = " ".join(args.words)
    if running_supervisor(settings):  # through Huragok's own connection: no console spot used
        reply = _ask(settings, "rcon", server.name, wait=RCON_WAIT, text=line, by=args.by or "")
        out.write(reply.text or reply.message)  # the server's own words: written without control characters
        return 0 if reply.ok else 1
    try:
        reply = asyncio.run(one_shot(server, line, by=args.by))
    except (RconError, OSError, TimeoutError) as error:
        out.fail(_explain(server.name, error))
        return 1
    out.write(reply.text)
    return 0 if reply.ok else 1


def cmd_ctrl_break(args: argparse.Namespace) -> int:
    from .winproc import send_ctrl_break

    return send_ctrl_break(args.pid)


HELP_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Superintendent · keeps the servers running", [
        ("status", "what each server is doing"),
        ("stop [server…] [--now]", "stop servers; none named: all of them, then Huragok"),
        ("restart [server…] [--now|--after-game]", "restart servers (Huragok must be running); none named: every "
                                                    "server that's running. Players read a countdown unless --now "
                                                    "(--reason \"text\")"),
        ("watch [server|huragok]", "follow a server's console output, or Huragok's log, here"),
    ]),
    ("Vergil · watches over the players", [
        ("say <server|all> <text>", "a [Server] line in the chat"),
        ("rcon [-s server] <command>", "one console command through Huragok's connection (--by name)"),
    ]),
    ("Huragok · builds and mends", [
        ("check", "check the settings, servers and commands without starting anything"),
        ("new <name> [--package name] …", "a new server folder in SERVERS/, from a package, checked and started"),
        ("package [list|show|save] …", "the packages in PACKAGES/; save <server> [name] keeps a server as one"),
        ("content [-s server] [words]", "what a server can play; --import [--from folder] copies MCC's variants in"),
        ("playlist [-s server] [list|show|new|use]", "a server's playlists"),
        ("variant [-s server] [list|show|check] [name]", "a server's saved custom games: what each changes, checked "
                                                         "by the server program"),
        ("workshop [-s server] [list|add|remove|install]", "a server's Workshop items"),
        ("steam [status|install|login <account>|path]", "SteamCMD for Workshop items"),
        ("dedicated [-s server] <command…>", "any of the server program's own commands, e.g. dedicated check"),
        ("init [--force]", "write settings.toml, commands.toml, automod.txt and names.txt (the first start does this)"),
    ]),
    ("Program", [
        ("start [--no-menu] [--hold]", "open the window; --no-menu is a plain log with typed commands (a task at boot)"),
        ("close", "close Huragok; the servers keep running"),
        ("update [--check] [--yes]", "a new version of Huragok from GitHub, installed if you say yes"),
        ("update --program [--now]", "a new release of the server program, put in place of every copy"),
    ]),
]


def help_lines() -> list[list[tuple[str, str | None]]]:
    """The help as lines with colours: the commands grouped as the dashboard's keys are."""
    from .tui import format_listing, heading_parts

    width = out.width
    from .lore import BANNER, HURAGOK, MOTTO

    lines: list[list[tuple[str, str | None]]] = [[(f" {row}", HURAGOK.colour)] for row in BANNER]
    lines += [
        [(f" HURAGOK {__version__}", "white"), (f" · the Engineer for Project Reclaimer servers · {MOTTO}", "gray")],
        [(" the Superintendent keeps your servers running; Vergil watches over the players, with automod and "
          "in-game commands for the roles you pick", "gray")],
        [(" " + "─" * (width - 1), "gray")],
        [(f"  {LAUNCHER}", "white"), ("  with no command: open the window (the dashboard), which is Huragok itself",
                                      None)],
        [(f"  {LAUNCHER} <command> …", "white"), ("  one of these; each command's own --help lists its options", None)],
    ]
    for group, commands in HELP_GROUPS:
        lines.append([("", None)])
        lines.append(heading_parts(group, width))
        lines.extend(format_listing(list(commands), width, index=False, wrap=True, widest=48))
    lines.append([("", None)])
    lines.append([(f"  -c <file> uses another settings file (default: {SETTINGS_FILE} next to the launcher, or "
                   "$HURAGOK_CONFIG); the other files sit beside it. A server can be named by its name, its file "
                   "name (lg-test-server) or its number in the list.", "gray")])
    lines.append([(f"  Exit codes: 0 done · {FAILED} it didn't work · {SETTINGS_PROBLEM} a settings problem, or a "
                   f"name it doesn't know · {SET_UP} first start: files written · {ALREADY_RUNNING} start: already "
                   f"running · {NOT_RUNNING} status: not running.", "gray")])
    return lines


def print_help() -> None:
    for parts in help_lines():
        out.parts(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=LAUNCHER,
        description="Huragok, the Engineer for Project Reclaimer servers: the Superintendent keeps them running "
                    "and Vergil watches over the players, from one window.")
    parser.print_help = lambda file=None: print_help()  # type: ignore[method-assign]  the grouped help above
    parser.format_help = lambda: "\n".join("".join(t for t, _ in parts) for parts in help_lines()) + "\n"  # type: ignore[method-assign]
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                        help=f"its settings file (default: {SETTINGS_FILE} here, or $HURAGOK_CONFIG); "
                             "the other files sit next to it")
    commands = parser.add_subparsers(dest="command", metavar="command")

    start = commands.add_parser("start", aliases=["run"], help="Open the window: the dashboard that runs the servers")
    start.add_argument("-v", "--verbose", action="store_true", help="log debugging detail")
    start.add_argument("--hold", action="store_true", help="keep the window open if it stops with a problem")
    start.add_argument("--no-menu", action="store_true", help="no dashboard: a plain log, with typed commands")
    start.add_argument("--child", action="store_true", help=argparse.SUPPRESS)  # Huragok itself; see _start_loop
    start.set_defaults(func=cmd_start)

    status = commands.add_parser("status", help="What each server is doing")
    status.set_defaults(func=cmd_status)

    check = commands.add_parser("check", help="Check the settings, servers and commands without starting")
    check.set_defaults(func=cmd_check)

    stop = commands.add_parser("stop", help="Stop servers (all of them and Huragok when none is named)")
    stop.add_argument("servers", nargs="*", help="server names; none: every server, then Huragok")
    stop.add_argument("--now", action="store_true", help="no countdown for the players")
    stop.add_argument("--wait", type=float, default=600, help="seconds to wait (default: %(default)s)")
    stop.set_defaults(func=cmd_stop)

    close = commands.add_parser("close", help="Close Huragok; the servers keep running")
    close.add_argument("--wait", type=float, default=30, help="seconds to wait (default: %(default)s)")
    close.set_defaults(func=cmd_close)

    restart = commands.add_parser("restart", help="Restart servers (needs Huragok running)")
    restart.add_argument("servers", nargs="*", help="server names; none: every server that's running")
    restart.add_argument("--now", action="store_true", help="no countdown for the players")
    restart.add_argument("--after-game", action="store_true", help="once the game being played ends")
    restart.add_argument("--reason", help="what players read")
    restart.set_defaults(func=cmd_restart)

    say = commands.add_parser("say", help="A [Server] chat line on a server (or all)")
    say.add_argument("server", help="a server's name, or all")
    say.add_argument("words", nargs=argparse.REMAINDER)
    say.set_defaults(func=cmd_say)

    update = commands.add_parser("update", help="Look for a new version on GitHub, and install it if you say yes: "
                                                "Huragok's, or with --program the server program's")
    update.add_argument("--check", action="store_true", help="only say whether there's a new version")
    update.add_argument("-y", "--yes", action="store_true", help="install it without asking")
    update.add_argument("--program", action="store_true",
                        help="the server program (Project Reclaimer): put in place of every copy, each running server "
                             "restarted after its game")
    update.add_argument("--now", action="store_true", help="with --program: restart the servers at once, mid-game")
    update.add_argument("--wait", type=float, default=300, help="seconds to wait for a running Huragok to update")
    update.set_defaults(func=cmd_update)

    rcon = commands.add_parser("rcon", help="Run one console command on a server, e.g. `rcon players`")
    rcon.add_argument("-s", "--server", help="the server, by name, when there are several")
    rcon.add_argument("--by", help="the name players read as who acted")
    rcon.add_argument("words", nargs=argparse.REMAINDER)
    rcon.set_defaults(func=cmd_rcon)

    watch = commands.add_parser("watch", help="Follow a server's console output, or Huragok's log")
    watch.add_argument("what", nargs="?", help="a server's name, or huragok")
    watch.add_argument("--server-log", action="store_true", help="the server's own data/<server>/server.log")
    watch.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    watch.set_defaults(func=cmd_watch)

    from .setupcli import add_parsers

    add_parsers(commands, _load, LAUNCHER)

    init = commands.add_parser("init", help=f"Write {SETTINGS_FILE}, {COMMANDS_FILE}, automod.txt and "
                                            "names.txt (start does this the first time)")
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
