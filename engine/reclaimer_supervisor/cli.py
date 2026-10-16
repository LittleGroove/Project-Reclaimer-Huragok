"""The supervisor's commands: start, check, stop, update, rcon, init.

Hosts run them through start.cmd (Windows) or start.sh (Linux) in the
supervisor's folder; with no command, those start it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__
from .config import BACKENDS, COMMANDS_FILE, SETTINGS_FILE, ConfigError, Settings, load

DEFAULT_CONFIG = os.environ.get("RECLAIMER_SUPERVISOR_CONFIG", SETTINGS_FILE)
LAUNCHER = os.environ.get("RECLAIMER_LAUNCHER", "reclaimer-supervisor")  # set by start.cmd / start.sh
SET_UP = 3  # exit code: files written, look them over and start again (start.cmd keeps the window open)
RELAUNCH = 10  # exit code of the supervisor itself: a new version is installed, run it
LOCK_FILE = "supervisor.pid"


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _setup_logging(settings: Settings | None, verbose: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if settings and settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file = logging.handlers.RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=5,
                                                    encoding="utf-8")
        file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(file)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _load(path: str) -> Settings:
    try:
        return load(path)
    except ConfigError as error:
        print(f"Settings problem: {error}", file=sys.stderr)
        raise SystemExit(2) from None


def _template(name: str) -> str:
    return resources.files("reclaimer_supervisor").joinpath(f"templates/{name}").read_text("utf-8")


# Setting up ------------------------------------------------------------------------------

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def find_server_folder(home: Path, given: str | None = None) -> Path | None:
    """The folder holding dedicated.toml: the one given, or the one the supervisor's folder sits in (or is)."""
    candidates = [Path(given)] if given else [home.parent, home]
    for folder in candidates:
        if (folder / "dedicated.toml").is_file():
            return folder.resolve()
    return None


def find_executable(folder: Path) -> Path | None:
    """The server program: project-reclaimer.exe, or the release's own name for it."""
    if (folder / "project-reclaimer.exe").is_file():
        return folder / "project-reclaimer.exe"
    found = sorted(folder.glob("project-reclaimer-dedicated*.exe"))
    return found[-1] if found else None


def pick_backend(folder: Path | None) -> tuple[str, dict[str, str], str]:
    """(backend, its settings, why) for what's in the server folder."""
    if folder is not None and os.name == "nt":
        if exe := find_executable(folder):
            return "windows", {"executable": exe.name}, f"found {exe.name}: the supervisor starts the servers"
        return "attach", {}, "no project-reclaimer.exe in the server folder, so it only watches and moderates"
    if folder is not None:
        if compose := next((name for name in COMPOSE_FILES if (folder / name).is_file()), None):
            return "docker", {"compose_file": compose}, f"found {compose}: the supervisor runs the containers"
        return "attach", {}, "no compose.yaml in the server folder, so it only watches and moderates"
    return "attach", {}, "no server folder found yet"


def _relative(folder: Path, home: Path) -> str:
    if folder == home.parent.resolve():
        return ".."
    if folder == home.resolve():
        return "."
    return folder.as_posix()


def write_files(home: Path, server_folder: str | None = None, backend: str | None = None,
                force: bool = False) -> list[str]:
    """settings.toml, commands.toml and automod.txt, each only if it isn't there (or force). Returns what was written."""
    from .permissions import render_commands_toml

    home.mkdir(parents=True, exist_ok=True)
    written = []
    settings_path = home / SETTINGS_FILE
    if force or not settings_path.exists():
        folder = find_server_folder(home, server_folder)
        picked, options, why = pick_backend(folder)
        backend = backend or picked
        if folder is None:
            where = server_folder or ".."
            print(f"  No dedicated.toml found {'in ' + where if server_folder else 'next to this folder'}. "
                  f"Put this folder inside your server folder, or set server_folder in {SETTINGS_FILE}.")
        else:
            where = _relative(folder, home)
        text = (_template(SETTINGS_FILE)
                .replace("{server_folder}", where)
                .replace("{backend}", backend)
                .replace("{executable}", options.get("executable", "project-reclaimer.exe"))
                .replace("{compose_file}", options.get("compose_file", "compose.yaml")))
        settings_path.write_text(text, encoding="utf-8")
        written.append(f"{SETTINGS_FILE:<15} how it runs. Servers in {folder or where}; backend {backend}"
                       + (f" ({why})" if backend == picked else ""))
    commands_path = home / COMMANDS_FILE
    if force or not commands_path.exists():
        commands_path.write_text(render_commands_toml(), encoding="utf-8")
        written.append(f"{COMMANDS_FILE:<15} who can use which in-game command. Put your player ID under [owner]")
    words = home / "automod.txt"
    if not words.exists():
        words.write_text(_template("automod.txt"), encoding="utf-8")
        written.append("automod.txt     automod's word list, empty for now (automod starts in dry run)")
    return written


def cmd_init(args: argparse.Namespace) -> int:
    home = Path(args.config).resolve().parent
    if (home / SETTINGS_FILE).exists() and not args.force:
        print(f"{home / SETTINGS_FILE} already exists; --force replaces it (and {COMMANDS_FILE}).", file=sys.stderr)
        return 1
    written = write_files(home, args.server_folder, args.backend, force=args.force)
    print(f"Wrote, in {home}:")
    for line in written:
        print(f"  {line}")
    print(f"Next: {LAUNCHER} check")
    return 0


# Running ------------------------------------------------------------------------------------

def _process_alive(pid: int, create_time: float) -> bool:
    import psutil

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
    import psutil

    path = settings.records / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "create_time": psutil.Process().create_time(),
                                "version": __version__}), encoding="utf-8")
    return path


def _summary(settings: Settings) -> None:
    """What's about to run, for the window the host is looking at."""
    log = logging.getLogger("supervisor")
    parts = settings.parts
    automod = parts["automod"]
    log.info("Folder %s · servers from %s", settings.home, settings.dedicated_config)
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


def _hold(args: argparse.Namespace, code: int) -> int:
    """A double-clicked window closes when we exit: keep it open to read why it stopped."""
    from .console import interactive

    if getattr(args, "hold", False) and code not in (0, RELAUNCH) and interactive():
        try:
            input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


def cmd_start(args: argparse.Namespace) -> int:
    if args.child:
        return _run_supervisor(args)
    home = Path(args.config).resolve().parent
    if not (home / SETTINGS_FILE).exists():
        print(f"Setting up Reclaimer Supervisor in {home}")
        for line in write_files(home):
            print(f"  wrote {line}")
        print(f"Look these over, then start again: {LAUNCHER}")
        return _hold(args, SET_UP)
    for line in write_files(home):  # put back any file that was deleted, with its defaults
        print(f"Wrote {line}")
    settings = _load(args.config)
    if pid := running_supervisor(settings):
        print(f"The supervisor is already running from this folder (process {pid}). To stop it: {LAUNCHER} stop",
              file=sys.stderr)
        return _hold(args, 1)
    return _hold(args, _start_loop(args))


def _start_loop(args: argparse.Namespace) -> int:
    """Runs the supervisor in a process of its own, and again whenever an update has installed a new version.

    This process stays on the old code, so it does as little as possible: start the
    supervisor, pass on signals, and start it again when it exits with RELAUNCH.
    """
    command = [sys.executable, "-m", "reclaimer_supervisor", "-c", str(Path(args.config).resolve()), "start", "--child"]
    if args.verbose:
        command.append("--verbose")
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
        print("Starting the new version…", flush=True)


def _run_supervisor(args: argparse.Namespace) -> int:
    from .console import interactive
    from .core import Supervisor

    settings = _load(args.config)
    if pid := running_supervisor(settings):
        print(f"The supervisor is already running from this folder (process {pid}).", file=sys.stderr)
        return 1
    _setup_logging(settings, args.verbose)
    lock = _take_lock(settings)
    _summary(settings)
    relaunch = False

    async def main() -> None:
        nonlocal relaunch
        supervisor = Supervisor(settings, interactive=interactive())
        loop = asyncio.get_running_loop()
        if os.name == "nt":
            # Ctrl+C reaches us through asyncio.run; Ctrl+Break and closing the window here.
            signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(supervisor.stop))
        else:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.add_signal_handler(sig, supervisor.stop)
        try:
            await supervisor.run()
        finally:
            relaunch = supervisor.relaunch

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        lock.unlink(missing_ok=True)
    return RELAUNCH if relaunch else 0


def cmd_update(args: argparse.Namespace) -> int:
    """Look for a new version on GitHub and, once the host says yes, install it."""
    from .console import interactive
    from .core import UPDATE_REQUEST
    from .updates import UpdateError, installed_here, is_newer, latest_release, summary, update

    settings = _load(args.config)
    try:
        release = latest_release(settings.updates.repository)
    except UpdateError as error:
        print(f"Couldn't look for updates: {error}", file=sys.stderr)
        return 1
    if release is None or not is_newer(release):
        print(f"Reclaimer Supervisor {__version__} is the newest version.")
        return 0
    print(f"Update available: Reclaimer Supervisor {release.version} (this is {__version__})")
    for line in summary(release.notes):
        print(f"    {line}")
    print(f"  What's new: {release.page}")
    if args.check:
        return 0
    if not installed_here(settings.home):
        print(f"This copy doesn't run from {settings.home / 'engine'}, so it can't update itself; download the new "
              "version instead.", file=sys.stderr)
        return 1
    if not args.yes:
        if not interactive():
            print(f"To install it: {LAUNCHER} update --yes")
            return 1
        try:
            answer = input(f"Install {release.version} now? Your servers keep running. [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Not installed.")
            return 0
    if pid := running_supervisor(settings):
        (settings.records / UPDATE_REQUEST).write_text(release.version, encoding="utf-8")
        print(f"Asked the running supervisor (process {pid}) to install it…")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            time.sleep(1)
            if _lock(settings).get("version") == release.version and running_supervisor(settings):
                print(f"Updated to {release.version}, and running again.")
                return 0
        print(f"Not done after {args.wait:.0f} s; its window or logs/supervisor.log says why.", file=sys.stderr)
        return 1
    print("Installing…")
    try:
        version = update(settings.home, release)
    except (UpdateError, OSError, subprocess.SubprocessError) as error:
        print(f"Update failed, still on {__version__}: {error}", file=sys.stderr)
        return 1
    print(f"Updated to {version}. Start it as usual: {LAUNCHER}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop the supervisor running from this folder and the servers it runs; or, if none is, just the servers."""
    from .core import STOP_REQUEST

    settings = _load(args.config)
    if pid := running_supervisor(settings):
        settings.records.mkdir(parents=True, exist_ok=True)
        (settings.records / STOP_REQUEST).write_text(str(time.time()), encoding="utf-8")
        print(f"Asked the supervisor (process {pid}) to stop, with the servers it runs…")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            if running_supervisor(settings) is None:
                print("Stopped.")
                return 0
            time.sleep(0.5)
        print(f"Still running after {args.wait:.0f} s; its window or logs/supervisor.log says why.", file=sys.stderr)
        return 1

    from .events import EventBus
    from .lifecycle import LifecycleError, LifecycleUnavailable, make_backend
    from .state import ServerState

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    backend = make_backend(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    try:
        stopped = asyncio.run(backend.stop_all())
    except (LifecycleError, LifecycleUnavailable) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Stopped: {', '.join(stopped)}." if stopped else "No servers of ours were running.")
    return 0


# Looking ---------------------------------------------------------------------------------------

def cmd_check(args: argparse.Namespace) -> int:
    from .part import commands_of
    from .parts import PARTS
    from .permissions import ROLES, TITLES, all_commands

    settings = _load(args.config)
    ok = True
    print(f"Version        {__version__}{_update_note(settings)}")
    print(f"Folder         {settings.home}")
    print(f"Server folder  {settings.server_folder}")
    print(f"Backend        {settings.backend}")
    if pid := running_supervisor(settings):
        print(f"Running        yes (process {pid})")
    for server in settings.servers:
        endpoint = server.rcon_endpoint()
        status_age = None
        try:
            updated = json.loads(server.status_path.read_text(encoding="utf-8")).get("updated")
            status_age = time.time() - float(updated)
        except (OSError, ValueError, TypeError):
            pass
        print(f"Server         {server.name}")
        print(f"  data         {server.data_dir}{'' if server.data_dir.is_dir() else '  (not there yet)'}")
        if endpoint:
            print(f"  RCON         {endpoint.host}:{endpoint.port} · password set · host key "
                  f"{'found' if server.host_key() else 'not found (server not started?)'}")
        else:
            print("  RCON         OFF: set [rcon] password in dedicated.toml")
            ok = False
        if status_age is None:
            print("  running      unknown (no status.json)")
        else:
            print(f"  running      {'yes' if status_age < 30 else 'no'} (status.json written {status_age:.0f} s ago)")

    print("Parts")
    for name, cls in PARTS.items():
        part = settings.parts[name]
        if cls.always_on:
            continue
        state = ("on" + (", dry run" if part.dry_run or settings.dry_run else "")) if part.enabled else "off"
        print(f"  {name:<10} {state:<12} {cls.description}")
        if not part.enabled:
            continue
        try:
            for warning in cls.preflight(part.options, settings.home):
                print(f"  {'':<10} warning: {warning}")
        except ValueError as error:
            print(f"  {'':<10} PROBLEM: [{name}] {error}")
            ok = False

    permissions = settings.permissions
    prefix = permissions.prefix
    print(f"Commands ({COMMANDS_FILE}{'' if permissions.path and permissions.path.exists() else ', missing: defaults'})")
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
    for role in ROLES:
        names = " ".join(prefix + n + ("*" if n in unavailable else "") for n in sorted(by_role[role]))
        print(f"  {TITLES[role]}{who[role]}: {names or '-'}")
    if permissions.off:
        print(f"  off: {' '.join(prefix + n for n in sorted(permissions.off))}")
    if unavailable & {n for names in by_role.values() for n in names}:
        print("  * its part is switched off")
    if not permissions.members["owner"]:
        print(f"  warning: nobody is owner yet: put your player ID under [owner] players in {COMMANDS_FILE}")
    print("  Each role can also use the commands listed above it.")
    print("Everything looks ready." if ok else "Fix the problems above, then check again.")
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
        print(f"Name a console command, e.g. {LAUNCHER} rcon players", file=sys.stderr)
        return 2
    if args.server:
        try:
            server = settings.server(args.server)
        except KeyError:
            print(f"No server named {args.server}", file=sys.stderr)
            return 2
    elif len(settings.servers) == 1:
        server = settings.servers[0]
    else:
        print("Several servers: pick one with --server", file=sys.stderr)
        return 2
    try:
        reply = asyncio.run(one_shot(server, " ".join(args.words), by=args.by))
    except (RconError, OSError, TimeoutError) as error:
        print(f"{server.name}: {error}", file=sys.stderr)
        return 1
    print(reply.text)
    return 0 if reply.ok else 1


def cmd_ctrl_break(args: argparse.Namespace) -> int:
    from .winproc import send_ctrl_break

    return send_ctrl_break(args.pid)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=LAUNCHER,
        description="Reclaimer Supervisor: starts your Project Reclaimer servers and keeps them up, with automod "
                    "and in-game commands for the roles you pick.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                        help=f"its settings file (default: {SETTINGS_FILE} here, or $RECLAIMER_SUPERVISOR_CONFIG); "
                             "the other files sit next to it")
    commands = parser.add_subparsers(dest="command", metavar="command")

    start = commands.add_parser("start", aliases=["run"], help="Start the supervisor (and the servers it runs)")
    start.add_argument("-v", "--verbose", action="store_true", help="log debugging detail")
    start.add_argument("--hold", action="store_true", help="keep the window open if it stops with a problem")
    start.add_argument("--child", action="store_true", help=argparse.SUPPRESS)  # the supervisor itself; see _start_loop
    start.set_defaults(func=cmd_start)

    check = commands.add_parser("check", help="Check the settings, servers and commands without starting")
    check.set_defaults(func=cmd_check)

    stop = commands.add_parser("stop", help="Stop the supervisor and the servers it runs")
    stop.add_argument("--wait", type=float, default=120, help="seconds to wait (default: %(default)s)")
    stop.set_defaults(func=cmd_stop)

    update = commands.add_parser("update", help="Look for a new version on GitHub, and install it if you say yes")
    update.add_argument("--check", action="store_true", help="only say whether there's a new version")
    update.add_argument("-y", "--yes", action="store_true", help="install it without asking")
    update.add_argument("--wait", type=float, default=300, help="seconds to wait for a running supervisor to update")
    update.set_defaults(func=cmd_update)

    rcon = commands.add_parser("rcon", help="Run one console command on a server, e.g. `rcon players`")
    rcon.add_argument("-s", "--server", help="the server, by name, when there are several")
    rcon.add_argument("--by", help="the name players read as who acted")
    rcon.add_argument("words", nargs=argparse.REMAINDER)
    rcon.set_defaults(func=cmd_rcon)

    init = commands.add_parser("init", help=f"Write {SETTINGS_FILE}, {COMMANDS_FILE} and automod.txt "
                                            "(start does this the first time)")
    init.add_argument("server_folder", nargs="?", help="the folder holding dedicated.toml (default: found)")
    init.add_argument("--backend", choices=BACKENDS, help="default: picked from what's in the server folder")
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
