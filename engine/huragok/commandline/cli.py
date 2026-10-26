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
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from .. import __version__
from ..core import lock, mailbox
from ..core.serverfolders import SERVERS_FOLDER, find_server_folders
from ..core.settings import BACKENDS, COMMANDS_FILE, SETTINGS_FILE, Settings
from . import check, common, firstrun, launcher, logsetup, run
from .common import ALREADY_RUNNING, DEFAULT_CONFIG, FAILED, LAUNCHER, NOT_RUNNING, SET_UP, SETTINGS_PROBLEM
from .shell import console as out


# Running ------------------------------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    if args.child:
        return run.run_huragok(args)
    if not getattr(args, "hold", False):
        return _start(args)
    # A double-clicked window closes the moment this returns: whatever stopped it (a settings problem, an error
    # writing the files, a crash), keep it open to read why.
    try:
        return _start(args)
    except SystemExit as stop:
        if isinstance(stop.code, str):
            out.fail(stop.code)
        return common.hold(args, stop.code if isinstance(stop.code, int) else FAILED if stop.code else 0)
    except Exception:  # noqa: BLE001 - shown, then held
        traceback.print_exc()
        out.fail(f"Huragok stopped with the error above. `{LAUNCHER} check` may say more.")
        return common.hold(args, FAILED)


def _start(args: argparse.Namespace) -> int:
    path = Path(args.config).resolve()
    home = path.parent
    if not path.exists():
        out.banner()
        out.title(f"HURAGOK {__version__}: THE FIRST DROP", f"setting up in {home}")
        out.heading("Written")
        out.listing(firstrun.written_rows(firstrun.write_files(home, settings_path=path)), index=False, wrap=True)
        if not find_server_folders(home, home / SERVERS_FOLDER, None) and not (home / "dedicated.toml").exists() \
                and not (home.parent / "dedicated.toml").exists():
            out.heading("Server folders")
            for line in firstrun.layout_help(home):
                out.say(line)
        out.write()
        out.ok(f"Look these over, then start again: {LAUNCHER}{common.config_option(path)}")
        return common.hold(args, SET_UP)
    for line in firstrun.write_files(home, settings_path=path):  # put back any file that was deleted, with its defaults
        out.say(f"Wrote {line}")
    settings = launcher.load_for_start(args)
    if pid := lock.running_supervisor(settings):
        if common.want_menu(args):
            outcome = run.second_window(settings)
            if outcome != "takeover":
                return 0
            out.note("The supervising window closed; this one takes over.")
        else:
            out.fail(f"Huragok is already running from this folder (process {pid}). To stop it: {LAUNCHER} stop")
            return common.hold(args, ALREADY_RUNNING)
    return common.hold(args, launcher.start_loop(args, settings))


def cmd_update_program(args: argparse.Namespace, settings: Settings) -> int:
    """`update --program`: a new release of the server program, installed at a quiet moment."""
    from ..program.releases import download_program, install_program, latest_program_release
    from ..window.tui import is_terminal
    from ..core.web import UpdateError
    from ..updates.engine import summary

    when = "now" if args.now else "after_game"
    if pid := lock.running_supervisor(settings):
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
    from ..window.tui import is_terminal
    from ..core.web import UpdateError, same_version
    from ..updates.engine import installed_here, is_newer, latest_release, summary, update

    settings = common.load_settings(args.config)
    if args.program:
        return cmd_update_program(args, settings)
    try:
        release = latest_release(settings.updates.repository, beta=settings.updates.beta)
    except UpdateError as error:
        out.fail(f"Couldn't look for updates: {error}")
        return 1
    if release is None or not is_newer(release):
        out.ok(f"Huragok {__version__} is the newest version.")
        return 0
    out.write(f"Update available: Huragok {release.version}{' (a beta: [updates] beta is on)' if release.beta else ''} "
              f"(this is {__version__})", "white")
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
    if pid := lock.running_supervisor(settings):
        out.note(f"Asking Huragok (process {pid}) to install it…")
        # It answers once the download and its Python packages are in, which can take minutes.
        reply = _ask(settings, "update", wait=UPDATE_WAIT, why=f"asked by {LAUNCHER} update")
        if not reply.ok:
            out.fail(reply.message)
            return 1
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            time.sleep(1)
            running = str(lock.read(settings).get("version") or "")
            if same_version(running, release.version) and lock.running_supervisor(settings):
                out.ok(f"Updated to {release.version}, and running again.")
                return 0
        out.fail(f"Not done after {args.wait:.0f} s; its window or {common.log_name(settings)} says why.")
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
                                    f"{common.log_name(settings)} (or its window) says what happened.")
    return reply


def _explain(server: str, error: BaseException) -> str:
    """A connection problem in plain words: a server that's off refuses its console port."""
    if isinstance(error, (ConnectionRefusedError, ConnectionResetError)) or "1225" in str(error) \
            or "refused" in str(error).lower():
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
    settings = common.load_settings(args.config)
    pid = lock.running_supervisor(settings)
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
            if lock.running_supervisor(settings) is None:
                out.ok("Stopped.")
                return 0
            time.sleep(0.5)
        out.fail(f"Still running after {args.wait:.0f} s; its window or {common.log_name(settings)} says why.")
        return 1

    from ..core.events import EventBus
    from ..servers.backends import LifecycleError, LifecycleUnavailable
    from ..servers.state import ServerState

    try:
        servers = [settings.server(name) for name in args.servers]
    except KeyError as error:
        out.fail(f"No server named {error}. Servers: {', '.join(s.name for s in settings.servers) or 'none'}")
        return SETTINGS_PROBLEM
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    backend = common.backend_for(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
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


def cmd_close(args: argparse.Namespace) -> int:
    """Close the running Huragok; the servers keep running (what Q does in the window)."""
    settings = common.load_settings(args.config)
    pid = lock.running_supervisor(settings)
    if not pid:
        out.note("Huragok isn't running from this folder.")
        return 0
    reply = _ask(settings, "close")
    out.say(reply.message)
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if lock.running_supervisor(settings) is None:
            out.ok("Closed; the servers keep running. Start it again to pick them up.")
            return 0
        time.sleep(0.5)
    out.fail(f"Still running after {args.wait:.0f} s.")
    return 1


def cmd_restart(args: argparse.Namespace) -> int:
    settings = common.load_settings(args.config)
    if not lock.running_supervisor(settings):
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
    settings = common.load_settings(args.config)
    text = " ".join(args.words).strip()
    if not text:
        out.fail("Nothing to say.")
        return 2
    every = args.server.lower() == "all"
    if lock.running_supervisor(settings):
        # One server after another, each with its own console's time to answer.
        wait = SAY_WAIT * max(1, len(settings.servers)) + ASK_WAIT if every else SAY_WAIT + ASK_WAIT
        reply = _ask(settings, "say", args.server, wait=wait, text=text)
        _print_reply(reply)
        return 0 if reply.ok else 1
    from ..rcon import RconError, one_shot

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
    from ..servers.ports import port_in_use

    settings = common.load_settings(args.config)
    pid = lock.running_supervisor(settings)
    live: dict = {}
    try:
        live = json.loads((settings.live_file).read_text(encoding="utf-8"))
        live = live if isinstance(live, dict) else {}
    except (OSError, ValueError):
        pass
    if pid:  # the lock says it runs; live.json only says how recently it wrote what it sees
        from ..window.dashboard import table_lines

        from ..lore import mood

        try:
            age = time.time() - float(live.get("updated") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        out.title(f"HURAGOK {live.get('version') or lock.read(settings).get('version') or ''}",
                  f"running (process {pid}) · backend {live.get('backend') or settings.backend} · {settings.home}")
        if lock.read(settings).get("launcher"):
            out.warn(f"Huragok is starting again (an update, or after it stopped unexpectedly): its start loop, "
                     f"process {pid}, holds the folder meanwhile. {common.log_name(settings)} says why.")
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
        for line in firstrun.layout_help(settings.home):
            out.say(line)
    return NOT_RUNNING


LIVE_STALE_SECONDS = 15
LOG_WORDS = ("huragok", "supervisor", "superintendent", "vergil", "events", "log")  # `watch` these: Huragok's log


def cmd_watch(args: argparse.Namespace) -> int:
    from .watch import follow

    settings = common.load_settings(args.config)
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
        console = settings.console_log(server.slug)
        path = server.log_path if args.server_log or not console.exists() else console
    try:
        follow(path, once=args.once)
    except KeyboardInterrupt:
        pass
    return 0


# Looking ---------------------------------------------------------------------------------------


def cmd_rcon(args: argparse.Namespace) -> int:
    from ..rcon import RconError, one_shot

    settings = common.load_settings(args.config)
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
    if lock.running_supervisor(settings):  # through Huragok's own connection: no console spot used
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
    from ..core.winproc import send_ctrl_break

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
        ("steam [status|install|login <account>|files [halo3|campaign|odst]|path]",
         "SteamCMD: Workshop items, and Halo 3's files (copied from MCC here, or downloaded)"),
        ("dedicated [-s server] <command…>", "any of the server program's own commands, e.g. dedicated check"),
        ("init [--force]", "write settings.toml, commands.toml, automod.txt, names.txt and intervalmessages.txt (the "
                           "first start does this)"),
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
    from ..window.tui import format_listing, heading_parts

    width = out.width
    from ..lore import BANNER, HURAGOK, MOTTO

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

    checking = commands.add_parser("check", help="Check the settings, servers and commands without starting")
    checking.add_argument("--set-aside", action="store_true",
                          help="move content files the servers can't read out of their content folders")
    checking.set_defaults(func=check.cmd_check)

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

    from .setup import add_parsers

    add_parsers(commands, common.load_settings, LAUNCHER)

    init = commands.add_parser("init", help=f"Write {SETTINGS_FILE}, {COMMANDS_FILE}, automod.txt, names.txt and "
                                            "intervalmessages.txt (start does this the first time)")
    init.add_argument("server_folder", nargs="?", help="one server folder (default: SERVERS/ next to the settings)")
    init.add_argument("--backend", choices=BACKENDS, help="default: picked from what's in the server folders")
    init.add_argument("--force", action="store_true", help=f"replace {SETTINGS_FILE} and {COMMANDS_FILE}")
    init.set_defaults(func=firstrun.cmd_init)

    ctrl_break = commands.add_parser("_ctrl-break")  # internal: see winproc.py
    ctrl_break.add_argument("pid", type=int)
    ctrl_break.set_defaults(func=cmd_ctrl_break)
    return parser


def main(argv: list[str] | None = None) -> int:
    logsetup.utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)
