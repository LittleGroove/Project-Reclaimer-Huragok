"""Setting servers up from the command line: `new`, `package`, `content`, `playlist`, `workshop`, `dedicated`.

They work without the window (files and the server program's own commands), so a host
running at boot with no dashboard has them too. A new server appears in a running
window within seconds and is started from here when one is running.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable

from ..content.newserver import NewServer, SetupError, create_server
from ..content.packages import PackageError, find_package, list_packages, save_package
from ..core import lock, mailbox
from ..core.serverfolders import ServerConfig
from ..core.settings import Settings
from ..core.text import slug
from ..core.tomledit import read_toml, set_table_key
from ..games import files as game_files
from ..lore import MOTTO
from ..program.dedicated import SLOW, Dedicated, DedicatedError, ToolResult, parse_playlists, tool_for
from .shell import console as out

ITEM_ID = re.compile(r"(?:id=)?(\d{6,})")


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


def parse_item_ids(words: list[str]) -> list[int]:
    """Workshop item IDs from numbers, or Workshop URLs (…?id=3014096841), in any mix."""
    found: list[int] = []
    for word in words:
        for part in word.replace(",", " ").split():
            match = ITEM_ID.search(part)
            if match and int(match.group(1)) not in found:
                found.append(int(match.group(1)))
    return found


# new ------------------------------------------------------------------------------------------

def cmd_new(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    tool = _tool(settings)
    package = None
    if args.package:
        package = find_package(settings.packages_folder, args.package)
        if package is None:
            names = ", ".join(p.key for p in list_packages(settings.packages_folder)) or "none"
            out.fail(f"No package called {args.package} in {settings.packages_folder}. Packages: {names}")
            return 2
        if package.name.endswith("(broken)"):
            out.fail(f"Package {package.key} is broken: {package.description}")
            return 2
    share = set() if args.own else set(settings.setup.share)
    if args.share is not None:
        share = {s.strip().lower() for s in args.share.split(",") if s.strip()}
    extra: dict[str, Any] = {}
    if args.map:
        extra["map"] = args.map
    if args.game:
        extra["game"] = args.game
    spec = NewServer(
        name=args.name, port=args.port or 0, package=package, hidden=args.hidden, password=args.password or "",
        max_players=args.max_players, share=share,
        mcc_folder=args.mcc if args.mcc is not None else settings.setup.mcc_folder,
        maps=[m.strip() for m in args.maps.split(",")] if args.maps else None,
        link_from=Path(args.link_from).resolve() if args.link_from else None,
        host_name=args.host_name, welcome=[args.welcome] if args.welcome else None, extra=extra,
        auto_update=False if args.no_auto_update else None)
    out.heading(args.name, note=f"UDP {spec.port or 'the next free port'} · "
                                 f"{package.summary() if package else 'the example rotation'}")
    try:
        outcome = create_server(settings, spec, tool, say=out.note)
    except SetupError as error:
        out.fail(f"Not made: {error}")
        return 1
    out.write()
    if outcome.ready:
        out.ok(f"Made {outcome.folder}. {MOTTO}")
    else:
        out.warn(f"Made {outcome.folder}, but it isn't ready:")
    out.note(f"server {outcome.name} · UDP {outcome.port} · RCON password set in its {settings.config_name}")
    if outcome.check is not None and not outcome.ready:
        for line in outcome.check.lines[-12:]:
            out.say(line)
    for note in outcome.notes:
        out.warn(note)
    if not outcome.ready:
        out.fail(f"Fix the problems, then check it again: {launcher} dedicated --server \"{outcome.name}\" check")
        return 1
    if args.no_start:
        return 0

    if lock.running_supervisor(settings):
        out.note("Huragok picks it up within 10 s, and the Superintendent starts it…")
        for _ in range(8):
            reply = mailbox.send(settings.records, "start", outcome.name, wait=10)
            if reply.ok or "no server called" not in reply.message:
                (out.ok if reply.ok else out.warn)(reply.message)
                break
            time.sleep(3)
    else:
        out.note(f"Start Huragok to run it: {launcher}")
    return 0


# package ----------------------------------------------------------------------------------------

def cmd_package(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    folder = settings.packages_folder
    if args.action == "list":
        packages = list_packages(folder)
        if not packages:
            out.warn(f"No packages in {folder}. The starter packages are written on the first start; "
                     f"save a server as one: {launcher} package save <server> <name>")
            return 0
        out.heading("Packages", note=f"{folder} · a package is a playlist, settings and content a new server "
                                     f"starts with ({launcher} new <name> --package <package>)")
        out.listing([(package.key, package.name, package.description + (f" · {package.summary()}" if package.description
                                                                        else "") or package.summary())
                     for package in packages], wrap=True)
        return 0
    if args.action == "save":
        server = _server(settings, args.server)
        name = args.name or server.name
        dest = folder / slug(name, "package")
        try:
            package = save_package(server.folder, server.name, dest, config_name=settings.config_name, name=name,
                                   description=args.description or f"Saved from {server.name}")
        except PackageError as error:
            out.fail(f"Not saved: {error}")
            return 1
        out.ok(f"Saved {package.folder}: {package.summary()}")
        for path in package.playlists:
            out.note(f"playlists/{path.name}")
        for path in package.content_files:
            out.note(f"content/{path.relative_to(package.folder / 'content').as_posix()}")
        return 0
    # show: the parser allows list, save and show only
    ref = args.name or args.server or ""
    package = find_package(folder, ref)
    if package is None:
        out.fail(f"No package called {ref or '(none named)'}")
        return 2
    out.heading(package.name, note=str(package.folder))
    rows: list[tuple[str, str]] = [("about", package.description), ("plays", package.plays)]
    rows += [(f"[server] {key}", str(value)) for key, value in package.server.items()]
    if package.workshop_items:
        rows.append(("Workshop items:", ", ".join(str(i) for i in package.workshop_items)))
    rows += [("file", f"playlists/{path.name}") for path in package.playlists]
    rows += [("file", f"content/{path.relative_to(package.folder / 'content').as_posix()}")
             for path in package.content_files]
    out.fields(rows)
    return 0


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
            out.note(f"None yet: G -> Custom games in the window makes one ({launcher}).")
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
        for line in go.describe(preset.mode, preset.options, preset.rules, preset.world) or \
                ["(plays as its game type has it)"]:
            out.say(line)
        return 0
    problems = preset.problems()
    with tempfile.TemporaryDirectory() as folder:
        result = _tool(settings).mode_check(preset.check_copy(Path(folder)))
    for problem in problems:
        out.warn(f"PROBLEM  {problem}")
    if result.ok and not problems:
        out.ok(f"{preset.name}: valid ({result.lines[-1] if result.lines else 'mode-check'})")
        return 0
    out.fail(f"{preset.name}: {result.problem if not result.ok else 'see the problems above'}")
    return 1


def cmd_playlist(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    server = _server(settings, args.server)
    tool = _tool(settings)
    action = args.action or "list"
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
    if action == "show":
        if not args.name:
            out.fail("Name the playlist: its file, or its name in playlists/")
            return 2
        return _show(tool.playlist_show(server.folder, args.name))
    if action == "new":
        if not (args.name and args.maps and args.games):
            out.fail("playlist new needs a name, --maps and --games")
            return 2
        result = tool.playlist_new(server.folder, args.name, args.maps.split(","), args.games.split(","),
                                   vote_seconds=args.vote_seconds, replace=args.replace)
        return _show(result)
    # use: the parser allows list, show, new and use only
    if not args.name:
        out.fail("Name the playlist file to use, e.g. playlists/infection.playlist.json")
        return 2
    path = args.name if args.name.endswith(".json") else f"playlists/{args.name}.playlist.json"
    if not (server.folder / path).is_file():
        out.fail(f"{server.folder / path} isn't there")
        return 2
    from ..program.plays import applies_when, check_plays, write_plays

    check = check_plays(tool, server, {"playlist": path})
    if check.ok and not check.asked:
        out.warn(f"Not checked by the server program: {check.unasked}")
    if not check.ok:
        out.fail(f"Not used: the server program won't play {path} as it is:")
        for problem in check.problems:
            out.warn(f"  {problem.text}")
        return 1
    try:
        write_plays(server.config_path, server.name, {"playlist": path})  # and its map, game, campaign… out
    except OSError as error:
        out.fail(f"Couldn't write {server.config_path.name}: {error}")
        return 1
    out.ok(f"{server.name} now plays {path}: with auto_reload on, {applies_when(server.plays_kind, 'playlist')} (the "
           "window's events say when it's applied)")
    return 0


def workshop_items(server: ServerConfig) -> list[int]:
    try:
        workshop = read_toml(server.config_path).get("workshop", {})
    except (OSError, ValueError):
        return []
    items = workshop.get("items", []) if isinstance(workshop, dict) else []
    return [int(i) for i in items if isinstance(i, int) and not isinstance(i, bool)]


def set_workshop_items(server: ServerConfig, items: list[int]) -> None:
    set_table_key(server.config_path, "workshop", "items", items, before="master")


def cmd_workshop(args: argparse.Namespace, load: Callable[[str], Settings], launcher: str) -> int:
    settings = load(args.config)
    server = _server(settings, args.server)
    action = args.action or "list"
    items = workshop_items(server)
    if action == "list":
        out.heading(server.name, "Workshop items")
        if not items:
            out.warn(f"{server.name} has no Workshop items ([workshop] items in its {settings.config_name}). "
                     f"Add some: {launcher} workshop --server \"{server.name}\" add <item ID or URL>…")
            return 0
        out.say(f"[workshop] items = {items}")
        out.note("Installed mod maps, as the server lists them (dedicated mods):")
        result = _tool(settings).mods(server.folder)
        out.program(textwrap.indent(result.text.rstrip(), "  "))
        return 0 if result.ok else 1
    if action in ("add", "remove"):
        ids = parse_item_ids(args.ids or [])
        if not ids:
            out.fail("Give Workshop item IDs (the number after id= in the item's address)")
            return 2
        new = [i for i in items if i not in ids] if action == "remove" else items + [i for i in ids if i not in items]
        set_workshop_items(server, new)
        out.ok(f"{server.name}'s Workshop items: {', '.join(str(i) for i in new) or 'none'}")
        out.note("A change to [workshop] takes effect when the server's `dedicated` restarts "
                 f"({launcher} restart \"{server.name}\"); install the items first: "
                 f"{launcher} workshop --server \"{server.name}\" install")
        return 0
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
        written = steam.configure(settings, status.executable, username)
        (out.ok if ok else out.warn)(f"[workshop] steamcmd and username written into {len(written)} server folder(s)"
                                     + (" and the account is signed in" if ok else "")
                                     + "; a server's [workshop] takes effect when its `dedicated` restarts "
                                     f"({launcher} restart <server>).")
        return 0 if ok else 1
    if action == "files":
        return _steam_files(settings, args, launcher)
    # path: the parser allows status, install, login, files and path only
    if status.executable is None:
        out.fail("SteamCMD isn't installed")
        return 1
    out.note(steam.add_to_path(status.executable.parent))
    return 0


def _steam_files(settings: Settings, args: argparse.Namespace, launcher: str) -> int:
    """`steam files [halo3|campaign|odst] [--download]`: one kind of game files into the shared game/, copied from
    MCC when it's on this PC, else (or with --download) downloaded with SteamCMD. Asked for by a person, so a
    download happens whatever [setup] download_game_files says."""
    from ..content import gamefiles
    from .. import steam as steam

    kind = (args.account or "halo3").lower()
    if kind not in game_files.DEPOTS:
        out.fail(f"'{kind}' isn't one of {', '.join(game_files.DEPOTS)}: {launcher} steam files [halo3|campaign|odst]")
        return 2
    out.heading("Halo 3's files")
    for label, text, colour in gamefiles.game_status(settings):
        (out.ok if colour == "green" else out.warn if colour == "yellow" else out.say)(f"{label:<13} {text}")
    target, why = gamefiles.shared_target(settings, kind)
    if target is None:
        out.fail(why)
        return 1
    found, no_download = gamefiles.sources(settings, kind, downloads=True)
    if args.download:
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
    for server in settings.servers:  # a server whose game folder has no Halo 3 now uses this one
        for line in gamefiles.quick_repairs(settings, server):
            out.say(f"{server.name}: {line}")
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

    new = commands.add_parser("new", help="Make a new server folder in SERVERS/ (from a package), check it, start it")
    new.add_argument("name", help="the server's name (its folder's name too)")
    new.add_argument("--package", "-p", help="a package from PACKAGES/ (its name, folder or number in `package list`)")
    new.add_argument("--port", type=int, help="its UDP game port (default: the next free one)")
    new.add_argument("--hidden", action="store_true", help="not listed in the server browser")
    new.add_argument("--password", help="the join password")
    new.add_argument("--max-players", type=int, help="players at once (1-127)")
    new.add_argument("--map", help="one map for every game (instead of a package or the example playlist)")
    new.add_argument("--game", help="the game type with --map")
    new.add_argument("--welcome", help="the chat line players read as they join")
    new.add_argument("--host-name", help="shown beneath the server's name in the browser")
    new.add_argument("--share", help="what it takes from the root: game,content (default: [setup] share; the ban "
                                     "list is always its own)")
    new.add_argument("--own", action="store_true", help="its own game folder, content and ban list")
    new.add_argument("--from", dest="mcc", nargs="?", const="", help="copy Halo 3's files from this MCC folder "
                     "(no folder: Steam's default) when none can be shared or linked")
    new.add_argument("--maps", help="with --from: only these maps, e.g. \"Guardian,Valhalla\"")
    new.add_argument("--link-from", help="a server folder whose game/ is hard-linked into the new one")
    new.add_argument("--no-start", action="store_true", help="don't start it")
    new.add_argument("--no-auto-update", action="store_true",
                     help="don't let it install new Project Reclaimer releases itself (default: [setup] auto_update)")
    new.set_defaults(func=bind(cmd_new))

    package = commands.add_parser("package", help="The packages in PACKAGES/: list, show, save a server as one")
    package.add_argument("action", nargs="?", default="list", choices=["list", "show", "save"])
    package.add_argument("server", nargs="?", help="save: the server to save")
    package.add_argument("name", nargs="?", help="save: the package's name; show: the package")
    package.add_argument("--description", help="save: a line about it")
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

    playlist = commands.add_parser("playlist", help="A server's playlists: list, show, new, use")
    playlist.add_argument("-s", "--server", help="the server, when there are several")
    playlist.add_argument("action", nargs="?", default="list", choices=["list", "show", "new", "use"])
    playlist.add_argument("name", nargs="?", help="show/use: the playlist; new: its name")
    playlist.add_argument("--maps", help="new: maps, separated by commas")
    playlist.add_argument("--games", help="new: game types or variants, separated by commas")
    playlist.add_argument("--vote-seconds", type=int, help="new: seconds players have to vote (5-120)")
    playlist.add_argument("--replace", action="store_true", help="new: overwrite a playlist of the same name")
    playlist.set_defaults(func=bind(cmd_playlist))

    variant = commands.add_parser("variant", help="A server's saved custom games (game variants): list, show, check")
    variant.add_argument("-s", "--server", help="the server, when there are several")
    variant.add_argument("action", nargs="?", default="list", choices=["list", "show", "check"])
    variant.add_argument("name", nargs="?", help="show/check: the custom game, by name or file")
    variant.set_defaults(func=bind(cmd_variant))

    workshop = commands.add_parser("workshop", help="A server's Steam Workshop items: list, add, remove, install")
    workshop.add_argument("-s", "--server", help="the server, when there are several")
    workshop.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove", "install"])
    workshop.add_argument("ids", nargs="*", help="item IDs or Workshop addresses")
    workshop.set_defaults(func=bind(cmd_workshop))

    steam = commands.add_parser("steam", help="SteamCMD: status, install, login <account>, files [halo3|campaign|odst], "
                                              "path")
    steam.add_argument("action", nargs="?", default="status", choices=["status", "install", "login", "files", "path"])
    steam.add_argument("account", nargs="?", help="login: the Steam account that owns MCC; files: halo3 (the default), "
                                                  "campaign or odst")
    steam.add_argument("--download", action="store_true", help="files: download with SteamCMD even when MCC is here")
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
