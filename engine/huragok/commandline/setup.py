"""Setting servers up from the command line: `new`, `repair`, `content`, `playlist`, `workshop`, `steam`,
`dedicated` (and `package`, gone in 0.7.0, which says where it went).

They work without the window (files and the server program's own commands), so a host
running at boot with no dashboard has them too. A new server appears in a running
window within seconds and is started from here when one is running.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable

from ..content import quickactions
from ..content.newserver import SetupError, create_server, finish
from ..core import lock
from ..core.serverfolders import ServerConfig, slugify
from ..core.settings import Settings
from ..core.text import slug
from ..core.tomledit import read_toml, set_table_key
from ..games import bungie
from ..games import files as game_files
from ..games.files import KINDS
from ..lore import MOTTO
from ..program.dedicated import SLOW, Dedicated, DedicatedError, ToolResult, parse_playlists, tool_for
from .shell import console as out


def _tool(settings: Settings) -> Dedicated:
    try:
        return tool_for(settings)
    except DedicatedError as error:
        out.fail(f"Problem: {error}")
        raise SystemExit(2) from None


def _server(settings: Settings, name: str | None) -> ServerConfig:
    if name:
        try:
            return settings.server(name)
        except KeyError:
            out.fail(f"No server named {name}. Servers: {', '.join(s.name for s in settings.servers) or 'none'}")
            raise SystemExit(2) from None
    if len(settings.servers) == 1:
        return settings.servers[0]
    out.fail("Several servers: name one with --server" if settings.servers else "No servers yet: make one with `new`")
    raise SystemExit(2)


def _show(result: ToolResult) -> int:
    out.program(result.text)
    return 0 if result.ok else 1


# new ------------------------------------------------------------------------------------------

# What `new` took before 0.7.0 (a package, shared folders, a port, settings written into the new file): still taken
# for one release, to say where they went.
GONE_NEW_OPTIONS: list[tuple[str, ...]] = [
    ("--package", "-p"), ("--port",), ("--hidden",), ("--password",), ("--max-players",), ("--map",), ("--game",),
    ("--welcome",), ("--host-name",), ("--share",), ("--own",), ("--from",), ("--maps",), ("--games",),
    ("--link-from",), ("--no-auto-update",)]


def _gone_dest(option: tuple[str, ...]) -> str:
    return f"gone_{option[0].lstrip('-').replace('-', '_')}"


def _shown(settings: Settings, server: ServerConfig) -> str:
    try:
        return str(server.config_path.resolve().relative_to(settings.home.resolve()))
    except ValueError:
        return str(server.config_path)


def _quick_action(settings: Settings, name: str, action: quickactions.QuickAction, yes: bool, launcher: str,
                  again: str) -> int:
    """A quick action as the window shows it (mockup 5): what it writes first, then written with --yes, or when the
    host says yes at a terminal. 0: written, or nothing to write, or the host said no; 1: not written."""
    from ..window.tui import is_terminal

    out.heading(name, "Quick action", action.title)
    for text, colour in action.preview():
        out.write(f"  {text}", None if colour == "white" else colour)
    if not action.needed:
        return 0
    if not yes:
        if not is_terminal():
            out.warn(f"Not written: nothing here can answer yes. To write it: {again}")
            return 1
        try:
            answer = input(f"  {action.ask} [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out.note("Nothing was written.")
            return 0
    try:
        quickactions.write(settings, action)
    except (quickactions.QuickActionError, OSError) as error:
        out.fail(f"Not written: {error}")
        return 1
    out.ok(f"Written in {action.where}; the version before it is kept in its history.")
    return 0


def cmd_new(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    if gone := [option[0] for option in GONE_NEW_OPTIONS if getattr(args, _gone_dest(option), None) is not None]:
        out.fail(f"{gone[0]} is gone: since Huragok 0.7.0 `new` makes a bare server, the program's own `dedicated init`"
                 " and nothing more (no package, no shared folders, nothing written into its dedicated.toml). Set what "
                 "you want in the new folder's dedicated.toml by hand afterwards; its quick actions (each shown first) "
                 f"are --one-server, --free-ports and --console; its game files: {launcher} repair, or I in the window")
        return 2
    tool = _tool(settings)
    out.heading(args.name, note="the program's own dedicated init, and nothing more")
    try:
        outcome = create_server(settings, args.name, tool, say=out.note)
    except SetupError as error:
        out.fail(f"Not made: {error}")
        return 1
    steps = ["one-server", "free-ports", "console"] if args.all else [
        step for step, chosen in (("one-server", args.one_server), ("free-ports", args.free_ports),
                                  ("console", args.console)) if chosen]
    folder = None
    try:
        code = _new_actions(settings, outcome, steps, args, launcher)
    finally:
        try:
            folder = finish(outcome)
        except SetupError as error:
            out.fail(str(error))
    if folder is None:
        return 1
    out.write()
    out.ok(f"Made {folder}. {MOTTO}")
    if not steps:
        out.note(f"Its quick actions, each shown before it's written: {launcher} new <name> --one-server --free-ports "
                 "--console (or --all) as a folder is made; for this one now, the window (D → Q).")
    out.note("Huragok picks it up within seconds and starts it once its ports are free on this PC."
             if lock.running_supervisor(settings) else f"Start Huragok to run it: {launcher}")
    return code


def _new_actions(settings: Settings, outcome: Any, steps: list[str], args: argparse.Namespace, launcher: str) -> int:
    """What the new folder holds (mockup 6), then each quick action asked for, in turn."""
    from ..content.newserver import read_folder

    servers, master = read_folder(outcome.config)
    ports = [port for _, port in servers if port]
    out.say(f"Made with the program's own `dedicated init`"
            + (f" (Project Reclaimer {outcome.version})" if outcome.version else "") + f": {len(servers)} server(s)"
            + (f", on port(s) {', '.join(str(p) for p in ports)}" if ports else "")
            + (f", with its master on {master}" if master else ", with its master off"))
    taken = [(port, whose) for port, whose in quickactions.taken_ports(settings, outcome.config, ports)]
    if taken:
        out.warn(f"{', '.join(str(p) for p, _ in taken)} in use on this PC"
                 + (f" ({', '.join(w for _, w in taken if w)})" if any(w for _, w in taken) else "")
                 + ": it won't start until that changes (--free-ports)")
    where, owner = outcome.shown, slugify(outcome.name)
    again = f"{launcher} new <another name> {' '.join('--' + s for s in steps)} --yes"
    code = 0
    for step in steps:
        try:
            if step == "one-server":
                action = quickactions.one_server(outcome.config, outcome.name, where=where, owner=owner)
            elif step == "free-ports":
                action = quickactions.free_ports(settings, outcome.config, master=args.master, where=where, owner=owner)
            else:
                action = quickactions.add_console(settings, outcome.config, where=where, owner=owner)
        except quickactions.QuickActionError as error:
            out.warn(f"Not offered: {error}")
            code = 1
            continue
        code = max(code, _quick_action(settings, outcome.name, action, args.yes, launcher, again))
    return code


# repair ---------------------------------------------------------------------------------------------------------

def cmd_repair(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    """What each server's folder lacks (content/gamefiles.py's repairs), shown, then put right when the host says so
    (--yes, or y at a terminal); a version of each dedicated.toml kept before its first change."""
    from ..content import gamefiles
    from ..window.tui import is_terminal

    settings = load(args.config)
    servers = [_server(settings, name) for name in args.servers] if args.servers else list(settings.servers)
    if not servers:
        out.warn("No servers to look at.")
        return 0
    plans = []
    for server in servers:
        plan = gamefiles.repairs(settings, server, lambda server=server: tool_for(settings, server), out.note)
        out.heading(server.name, "Repair")
        if not plan:
            out.ok("Nothing to put right: its folder has what it needs, as far as Huragok can tell.")
            continue
        for number, repair in enumerate(plan, 1):
            (out.say if repair.run else out.warn)(f"{number}. {repair.text}"
                                                  + ("" if repair.run else " (can't be done from here)"))
        if doable := [repair for repair in plan if repair.run is not None]:
            plans.append((server, doable))
    if not plans:
        return 0
    if not args.yes:
        if not is_terminal():
            out.warn(f"Nothing was changed. To put these right: {launcher} repair "
                     + " ".join(f'"{s.name}"' for s, _ in plans) + " --yes")
            return 1
        try:
            answer = input("  Put these right? A copy of each dedicated.toml is kept first [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            out.note("Nothing was changed.")
            return 0
    code = 0
    for server, plan in plans:
        out.heading(server.name, "Repairing")
        _, failed = gamefiles.run_repairs(settings, server, plan, out.note)
        for line in failed:
            out.warn(f"Not done: {line}")
            code = 1
    if lock.running_supervisor(settings):
        out.note("Huragok sees the changes within seconds; a running server takes a changed path at its next start.")
    return code


# package: gone ----------------------------------------------------------------------------------------------------

def cmd_package(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    out.fail(f"package is gone: Huragok 0.7.0 has no packages. A new server is the program's own dedicated init "
             f"({launcher} new <name>), and a playlist goes from one server to another in the window (G → 1 → C). "
             "PACKAGES/ is left as it is; the starter packages are kept in engine/tools/archive/starter-packages.")
    return 2


# content, playlist, workshop, dedicated --------------------------------------------------------

def cmd_content(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    server = _server(settings, args.server)
    if args.do_import:
        from ..content.mccimport import content_root_of, import_content, importable
        from ..steam import find_mcc

        source = Path(args.source) if args.source else find_mcc(settings.setup.mcc_folder)
        if source is None or not source.exists():
            out.fail("Name where the variants come from with --from: your MCC folder (or any folder or file with "
                     ".mvar, .bin or saved .json maps and game types)")
            return 2
        items = importable(source, args.words)
        if not items:
            out.warn(f"Nothing to import from {source}" + (f" matching {' '.join(args.words)}" if args.words else ""))
            return 1
        root = content_root_of(server.folder, settings.config_name)
        copied, skipped = import_content(items, root, overwrite=args.overwrite)
        out.ok(f"Copied {len(copied)} file(s) into {root}" + (f"; {skipped} already there" if skipped else ""))
        for path in copied[:30]:
            out.note(path.relative_to(root).as_posix())
        if len(copied) > 30:
            out.note(f"... {len(copied) - 30} more")
        out.note(f"They're listed by name now: {launcher} content -s \"{server.name}\" <words>")
        return 0
    result = _tool(settings).content(server.folder, args.words)
    sections = ("Maps:", "Map variants:", "Game types:", "Game variants:", "Mod maps:", "Workshop maps:",
                "Halo 3: ODST")
    for line in result.text.rstrip().splitlines():
        out.write(line, "white" if line.startswith(sections) else None)
    return 0 if result.ok else 1


def cmd_variant(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    """`variant list | show <name> | check <name>`: a server's saved custom games (the window's G -> Custom games)."""
    import tempfile

    from ..games import halo3_options as go

    settings = load(args.config)
    server = _server(settings, args.server)
    presets = go.list_presets(server.content_dir)
    if args.action == "list":
        out.heading(f"{server.name} · custom games", note=str((server.content_dir or server.folder) / "Game Modes"))
        if not presets:
            out.note("None: custom games are made by hand (or imported from MCC: content --import) into its "
                     "content folder's Game Modes.")
            return 0
        out.listing([(p.name, p.mode_label, p.relative) for p in presets], index=False)
        return 0
    if not args.name:
        out.fail(f"Name the custom game: {launcher} variant {args.action} \"<name>\"")
        return 2
    wanted = args.name.lower()
    info = next((p for p in presets if wanted in (p.name.lower(), p.path.stem.lower(), p.relative.lower())), None)
    if info is None:
        out.fail(f"No custom game called {args.name}; {launcher} variant list shows them")
        return 1
    try:
        preset = go.load_preset(info.path)
    except go.PresetError as error:
        out.fail(str(error))
        return 1
    if args.action == "show":
        out.heading(preset.name, note=f"{info.mode_label} · {info.relative}")
        for line in go.describe(preset.options, preset.rules, preset.world) or ["(plays as its game type has it)"]:
            out.say(line)
        return 0
    with tempfile.TemporaryDirectory() as folder:  # the server program's own check, of a copy
        try:
            result = _tool(settings).mode_check(preset.check_copy(Path(folder)))
        except go.PresetError as error:  # an MCC .bin variant: mode-check doesn't read those
            out.note(f"{preset.name}: {error}")
            return 0
    problem = go.mode_check_problem(result)  # 0.10.8 answers 0 for a bad file too: its words say
    if problem is None:
        out.ok(f"{preset.name}: valid ({result.lines[-1] if result.lines else 'mode-check'})")
        return 0
    out.fail(f"{preset.name}: {problem}")
    return 1


def cmd_playlist(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    server = _server(settings, args.server)
    action = args.action or "list"
    if action in ("new", "use"):  # gone in 0.7.0: said where they went, for one release
        out.fail(f"playlist {action} is gone: Huragok no longer makes playlists or changes what a server plays. Make "
                 f"or change a playlist by hand in {server.folder / 'playlists'}, and name it in the server's own "
                 f"[[server]] block (playlist = \"playlists/<file>.playlist.json\") in {server.config_path}; the "
                 f"server takes it by itself (auto_reload). {launcher} playlist show <file> shows it as the server "
                 "reads it.")
        return 2
    tool = _tool(settings)
    if action == "list":
        result = tool.playlists(server.folder)
        infos = parse_playlists(result.text) if result.ok else []
        if not infos:
            return _show(result)
        out.heading(server.name, "Playlists", note=f"{server.name} plays {server.plays}")
        rows = []
        for info in infos:
            title = getattr(info, "name", "")
            about = (f'"{title}" · ' if title else "") + f"{info.games} games · vote {info.vote_seconds} s" \
                if info.games else ""  # one the program can't read: just why
            if info.played_by and info.played_by != "no server":
                about += f"   ◄ played by {info.played_by}"
            if info.problem:
                about += ("   " if about else "") + f"PROBLEM: {info.problem}"
            rows.append((info.file.removeprefix("playlists/"), about))
        out.listing(rows, wrap=True)
        return 0
    if not args.name:  # show
        out.fail("Name the playlist: its file, or its name in playlists/")
        return 2
    return _show(tool.playlist_show(server.folder, args.name))


def workshop_items(server: ServerConfig) -> list[int]:
    try:
        workshop = read_toml(server.config_path).get("workshop", {})
    except (OSError, ValueError):
        return []
    items = workshop.get("items", []) if isinstance(workshop, dict) else []
    return [int(i) for i in items if isinstance(i, int) and not isinstance(i, bool)]


def cmd_workshop(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    server = _server(settings, args.server)
    action = args.action or "list"
    items = workshop_items(server)
    if action == "list":
        out.heading(server.name, "Workshop items")
        if not items:
            out.warn(f"{server.name} has no Workshop items: list them in [workshop] items in its "
                     f"{settings.config_name} (items = [<item ID>, ...]); the server fetches them itself")
            return 0
        out.say(f"[workshop] items = {items}")
        out.note("Installed mod maps, as the server lists them (dedicated mods):")
        result = _tool(settings).mods(server.folder)
        out.program(textwrap.indent(result.text.rstrip(), "  "))
        return 0 if result.ok else 1
    if action in ("add", "remove"):
        # 0.7.0: a server's files are the host's; Huragok no longer writes [workshop] items
        out.fail(f"Huragok doesn't change a server's Workshop items any more: write them in [workshop] items in "
                 f"{server.config_path} (items = [<item ID>, ...]). The server fetches what it lists and keeps it up to "
                 f"date itself; to fetch now with the server stopped: {launcher} workshop --server \"{server.name}\" "
                 "install")
        return 2
    return _show(_tool(settings).mods(server.folder))  # install: the parser allows no other action


def cmd_steam(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    from .. import steam

    settings = load(args.config)
    action = args.action or "status"
    status = steam.status(settings)
    if action == "status":
        out.heading("SteamCMD for the Workshop")
        (out.ok if status.ready else out.warn)(status.summary())
        if status.accounts:
            out.note(f"accounts SteamCMD remembers: {', '.join(status.accounts)}")
        if not status.ready:
            out.note(f"Set it up: {launcher} steam install [--standard] ; {launcher} steam login <account>")
        return 0 if status.ready else 1
    if action == "install":
        if status.executable is not None and not args.force:
            out.say(f"SteamCMD is already at {status.executable} ({status.where}); --force installs another copy")
            return 0
        target = Path(args.into) if args.into else (
            (steam.STANDARD_WINDOWS if sys.platform == "win32" else Path.home() / "steamcmd") if args.standard
            else settings.home / "steamcmd")
        try:
            executable = steam.install(target, say=out.note)
        except steam.SteamSetupError as error:
            out.fail(f"Not installed: {error}")
            return 1
        out.ok(f"Installed {executable}. Next: {launcher} steam login <your Steam account>")
        if args.path:
            out.note(steam.add_to_path(executable.parent))
        return 0
    if action == "login":
        if status.executable is None:
            out.fail(f"SteamCMD isn't installed: {launcher} steam install")
            return 1
        username = args.account or status.username
        if not username:
            out.fail("Name the Steam account (it must own MCC): steam login <account>")
            return 2
        if problem := steam.check_username(username):
            out.fail(f"Not signed in: {problem}")
            return 2
        ok, detail = steam.signed_in(status.executable, username)
        if not ok:
            out.say(steam.login_window(status.executable, username))
            out.note(f"Then run this again to check and to tell the servers: {launcher} steam login {username}")
        steam.configure(settings, status.executable, username)
        (out.ok if ok else out.warn)("[setup] steamcmd and steam_username written in settings.toml"
                                     + (" and the account is signed in" if ok else "") + ". A server's own [workshop] "
                                     f"lines, one server at a time and shown first: {launcher} steam lines <server>")
        return 0 if ok else 1
    if action == "files":
        return _steam_files(settings, args, launcher)
    if action == "lines":
        server = _server(settings, args.account)
        try:
            action_ = quickactions.steamcmd_lines(settings, server.config_path, where=_shown(settings, server))
        except quickactions.QuickActionError as error:
            out.fail(f"Not offered: {error}")
            return 1
        return _quick_action(settings, server.name, action_, args.yes, launcher,
                             f"{launcher} steam lines \"{server.name}\" --yes")
    # path: the parser allows status, install, login, files, lines and path only
    if status.executable is None:
        out.fail("SteamCMD isn't installed")
        return 1
    out.note(steam.add_to_path(status.executable.parent))
    return 0


def _steam_files(settings: Settings, args: argparse.Namespace, launcher: str) -> int:
    """`steam files [halo3|campaign|odst|reach|reach-campaign|halo1|halo2] [--download]`: one kind of game files
    into the shared game/, copied from MCC when it's on this PC, else (or with --download) downloaded with SteamCMD
    (Halo 3's, the campaign's and ODST's only: Reach's, CE's and Halo 2's come from MCC alone for now). Asked for
    by a person, so a download happens whatever [setup] download_game_files says."""
    from ..content import gamefiles
    from .. import steam as steam

    kind = (args.account or "halo3").lower()
    if kind not in game_files.KINDS:
        out.fail(f"'{kind}' isn't one of {', '.join(game_files.KINDS)}: {launcher} steam files [{'|'.join(KINDS)}]")
        return 2
    out.heading("Game files")
    version = gamefiles.root_version(settings)
    for label, text, colour in gamefiles.game_status(settings, version):
        (out.ok if colour == "green" else out.warn if colour == "yellow" else out.say)(f"{label:<13} {text}")
    if not game_files.plays_on(kind, version):
        out.fail(f"{game_files.LABELS[kind]} need Project Reclaimer {bungie.GAMES[game_files.GAME_OF_KIND[kind]].since}"
                 f" or later; the program here is {version}: put the newest in place (a server with auto_update on "
                 "installs it as it starts)")
        return 1
    target, why = gamefiles.shared_target(settings, kind)
    if target is None:
        out.fail(why)
        return 1
    found, no_download = gamefiles.sources(settings, kind, downloads=True)
    if kind not in game_files.DEPOTS:
        if args.download or not found:
            out.fail(f"No way to get {game_files.LABELS[kind]} here: {no_download}")
            return 1
    elif args.download:
        found = [source for source in found if source.how == "steamcmd"]
    if not found:
        why = no_download or "SteamCMD can download them, but --download leaves MCC out and there's no SteamCMD way"
        out.fail(f"No way to get {game_files.LABELS[kind]} here: "
                 + ("" if args.download else "MCC isn't on this PC (Steam's records don't list it), and ")
                 + f"{why}. Set SteamCMD up: {launcher} steam install ; {launcher} steam login <account>")
        return 1
    source = found[0]
    try:
        said = gamefiles.fetch(settings, kind, source, target, lambda: _tool(settings), out.note)
    except (OSError, steam.SteamSetupError) as error:
        out.fail(f"Not got: {error}")
        return 1
    out.ok(said)
    for server in settings.servers:  # a server whose own game folder has no Halo 3: said, never changed by itself
        paths = gamefiles.read_paths(server.folder, settings.config_name)
        if paths is not None and gamefiles.game_path_candidate(settings, paths, server.plays_kind) is not None:
            out.note(f"{server.name}: its game folder ({paths.game_raw}) has no Halo 3 files; "
                     f"{launcher} repair \"{server.name}\" points it at one here that has them (shown first)")
    return 0


def cmd_dedicated(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    server = _server(settings, args.server)
    if not args.words:
        out.fail(f"Name a dedicated command, e.g. {launcher} dedicated check")
        return 2
    tool = _tool(settings)
    result = tool.run_sync(server.folder, *args.words, timeout=SLOW, config=settings.config_name)
    out.program(result.text)
    return result.code


# The parsers ---------------------------------------------------------------------------------

def add_parsers(commands: Any, load: Callable[[str], Settings], launcher: str) -> None:
    def bind(function: Callable[..., int]) -> Callable[[argparse.Namespace], int]:
        return lambda args: function(args, load, launcher)

    new = commands.add_parser("new", help="Make a new server folder in SERVERS/ with the program's own `dedicated "
                                           "init`, and nothing more; then its quick actions, each shown first")
    new.add_argument("name", help="the new folder's name (and its server's, with --one-server)")
    new.add_argument("--one-server", action="store_true", help="quick action: keep the first [[server]] block, "
                                                                "named as the folder, and take out the others")
    new.add_argument("--free-ports", action="store_true", help="quick action: a free game port for each server, and "
                                                                "its master off (or --master free)")
    new.add_argument("--master", choices=["off", "free"], default="off",
                     help="with --free-ports: the folder's master off (default), or on a free port")
    new.add_argument("--console", action="store_true", help="quick action: the console, a random [rcon] password")
    new.add_argument("--all", action="store_true", help="all three quick actions, in turn")
    new.add_argument("-y", "--yes", action="store_true", help="write the quick actions without asking (each is still "
                                                                "shown)")
    for old in GONE_NEW_OPTIONS:  # gone in 0.7.0: still taken, to say where they went (one release)
        new.add_argument(*old, dest=f"gone_{old[0].lstrip('-').replace('-', '_')}", nargs="?", const=True,
                         default=None, help=argparse.SUPPRESS)
    new.add_argument("--no-start", action="store_true", help=argparse.SUPPRESS)  # kept for scripts: it never starts
    new.set_defaults(func=bind(cmd_new))

    repair = commands.add_parser("repair", help="What a server's folder lacks (its game files, a game folder or "
                                                "ban list it names that isn't here, SteamCMD lines), shown, then put "
                                                "right when you say yes")
    repair.add_argument("servers", nargs="*", help="the servers (default: every one)")
    repair.add_argument("-y", "--yes", action="store_true", help="put them right without asking (each is still shown)")
    repair.set_defaults(func=bind(cmd_repair))

    package = commands.add_parser("package", help=argparse.SUPPRESS)  # gone in 0.7.0: says where it went
    package.add_argument("words", nargs=argparse.REMAINDER)
    package.set_defaults(func=bind(cmd_package))

    content = commands.add_parser("content", help="The maps, map variants, game types and variants a server can play "
                                                  "(`content odst`: Halo 3: ODST's missions, Firefight maps and game "
                                                  "types); --import copies MCC's own variants (or a folder's) into "
                                                  "its content")
    content.add_argument("-s", "--server", help="the server, when there are several")
    content.add_argument("--import", dest="do_import", action="store_true",
                         help="copy map and game variants into the server's content folder instead of listing")
    content.add_argument("--from", dest="source", help="with --import: your MCC folder, or any folder or file "
                                                       "(default: MCC found on this PC, or [setup] mcc_folder)")
    content.add_argument("--overwrite", action="store_true", help="with --import: replace files already there")
    content.add_argument("words", nargs="*", help="only names containing every word")
    content.set_defaults(func=bind(cmd_content))

    playlist = commands.add_parser("playlist", help="A server's playlists: list, show")
    playlist.add_argument("-s", "--server", help="the server, when there are several")
    # new and use are gone (0.7.0): still taken, to say where they went
    playlist.add_argument("action", nargs="?", default="list", choices=["list", "show", "new", "use"])
    playlist.add_argument("name", nargs="?", help="show: the playlist, its file or its name in playlists/")
    playlist.set_defaults(func=bind(cmd_playlist))

    variant = commands.add_parser("variant", help="A server's saved custom games (game variants): list, show, check")
    variant.add_argument("-s", "--server", help="the server, when there are several")
    variant.add_argument("action", nargs="?", default="list", choices=["list", "show", "check"])
    variant.add_argument("name", nargs="?", help="show/check: the custom game, by name or file")
    variant.set_defaults(func=bind(cmd_variant))

    workshop = commands.add_parser("workshop", help="A server's Steam Workshop items: list, install (fetch them now)")
    workshop.add_argument("-s", "--server", help="the server, when there are several")
    workshop.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "install"])
    workshop.add_argument("ids", nargs="*", help="item IDs or Workshop addresses")
    workshop.set_defaults(func=bind(cmd_workshop))

    steam = commands.add_parser("steam", help="SteamCMD: status, install, login <account>, files [halo3|campaign|odst|"
                                              "reach|reach-campaign|halo1|halo2], lines <server>, path")
    steam.add_argument("action", nargs="?", default="status",
                       choices=["status", "install", "login", "files", "lines", "path"])
    steam.add_argument("account", nargs="?", help="login: the Steam account that owns MCC; files: halo3 (the default), "
                                                  "campaign, odst, or from Project Reclaimer 0.10 reach, "
                                                  "reach-campaign, halo1 or halo2 (those from MCC only); lines: the "
                                                  "server whose [workshop] steamcmd and username are written")
    steam.add_argument("-y", "--yes", action="store_true", help="lines: write them without asking (still shown)")
    steam.add_argument("--download", action="store_true", help="files: download with SteamCMD even when MCC is here "
                                                               "(Halo 3's, the campaign's and ODST's)")
    steam.add_argument("--standard", action="store_true", help="install: into SteamCMD's standard folder, not the root's")
    steam.add_argument("--into", help="install: into this folder")
    steam.add_argument("--force", action="store_true", help="install: even when one was found")
    steam.add_argument("--path", action="store_true", help="install: also put it on your PATH")
    steam.set_defaults(func=bind(cmd_steam))

    dedicated = commands.add_parser("dedicated", help="Run any of the server program's own `dedicated` commands "
                                                      "in a server's folder, e.g. `dedicated check`")
    dedicated.add_argument("-s", "--server", help="the server, when there are several")
    dedicated.add_argument("words", nargs=argparse.REMAINDER, help="the command and its words")
    dedicated.set_defaults(func=bind(cmd_dedicated))
