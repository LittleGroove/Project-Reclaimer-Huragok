"""Making a server folder: `dedicated init`, the package, the root's shared folders, its
dedicated.toml, then `dedicated check`.

    SERVERS/<name>/            the new folder: dedicated.toml, playlists/, data/
    game/  content/  bans.json at the root, shared by every server made here ([setup] share):
                               Halo 3's files once, one content folder, one ban list

The folder is built as SERVERS/.new-<name>/ (folders starting with a dot are never
picked up) and renamed when it's done, so a half-made server is never started. If
its check fails, the folder stays, with autostart off and the problems reported, for
the host to fix.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings, slug_taken, slugify
from .contentimport import find_mcc
from .dedicated import Dedicated, ToolResult, find_executable
from .odst import FILES_HINT
from .packages import Package, apply_package, server_values
from .tomledit import read_toml, remove_server_setting, replace_server_blocks, set_server_setting, \
    set_table_key, set_top_key

FIRST_PORT = 49176
SHAREABLE = ("game", "content", "bans")
COPIED_FROM_EXISTING = ("host_name", "stats_server", "public_address", "upnp")
MAX_NAME = 48  # the server program's limit for a [[server]] name
MIN_PORT, MAX_PORT = 1024, 65535
MAX_PLAYERS, CO_OP_PLAYERS = 127, 16
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
Say = Callable[[str], None]


class SetupError(Exception):
    pass


@dataclass
class NewServer:
    name: str
    port: int = 0  # 0: the next free one
    package: Package | None = None
    hidden: bool = False
    password: str = ""  # join password; empty lets anyone join
    max_players: int | None = None
    share: set[str] = field(default_factory=lambda: set(SHAREABLE))  # what comes from the root
    mcc_folder: str | None = None  # Halo 3's files from here ("" for Steam's default) when none can be shared or linked
    maps: list[str] | None = None  # with mcc_folder: only these maps
    link_from: Path | None = None  # a server folder whose game/ is hard-linked, or a game folder (same drive: no
    #                                space used); with neither this nor mcc_folder, the first game folder here
    host_name: str | None = None
    rcon_password: str = ""  # empty: made up
    welcome: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # more [[server]] keys (map, game, voice...)
    auto_update: bool | None = None  # installs new Project Reclaimer releases itself; None: [setup] auto_update


@dataclass
class Outcome:
    folder: Path
    name: str
    port: int
    rcon_password: str
    check: ToolResult | None
    ready: bool
    notes: list[str] = field(default_factory=list)


def valid_name(name: str) -> str | None:
    """Why a name can't be a server's (and its folder's), or None. The server program takes names of at most
    MAX_NAME characters; control and format characters (DEL, U+2028, zero-width ones) would end up raw in
    dedicated.toml and in the browser."""
    name = name.strip()
    if not name:
        return "the name is empty"
    if name.startswith((".", "_")):
        return "a name starting with . or _ is a folder Huragok ignores"
    if re.search(r'[<>:"/\\|?*]', name) or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in name):
        return "the name has a character a folder name can't have (or an invisible one)"
    if name.endswith((" ", ".")) or len(name) > MAX_NAME:
        return f"the name must be at most {MAX_NAME} characters and not end with a space or a dot"
    if name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
        return f"{name.split('.')[0].strip()} is a name Windows keeps for a device: it can't be a folder's"
    return None


def every_block(settings: Settings) -> list[tuple[Path, dict[str, Any], dict[str, Any]]]:
    """(its folder, the [[server]] block, its whole file) for every block in every server folder here: disabled
    servers and folders Huragok left out (a broken file, a clashing name) too, which settings.servers doesn't
    hold. A file that can't be read is skipped."""
    from .config import find_server_folders

    found = []
    for folder in find_server_folders(settings.home, settings.servers_folder, settings.single_folder,
                                      settings.config_name):
        try:
            data = read_toml(folder / settings.config_name)
        except (OSError, ValueError):
            continue
        blocks = data.get("server")
        found += [(folder, block, data) for block in blocks if isinstance(block, dict)] \
            if isinstance(blocks, list) else []
    return found


def _port(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def used_ports(settings: Settings, skip: tuple[Path, str] | None = None) -> set[int]:
    """Every game and console port a server here uses, and each folder's master port while it's on: the running
    servers' and the disabled or left-out ones' too. `skip` (its config file, its name) leaves one server out."""
    left_out = (skip[0].resolve(), skip[1].lower()) if skip else None
    ports: set[int] = set()
    for server in settings.servers:
        if (server.config_path.resolve(), server.name.lower()) != left_out:
            ports |= {p for p in (server.game_port, server.rcon_port) if p}
    blocks = every_block(settings)
    for folder, block, _ in blocks:
        if ((folder / settings.config_name).resolve(), str(block.get("name", "")).lower()) != left_out:
            ports |= {p for p in (_port(block.get("port")), _port(block.get("rcon_port"))) if p}
    for data in {id(data): data for _, _, data in blocks}.values():
        master = data.get("master")
        if isinstance(master, dict) and master.get("enabled", True) is not False and _port(master.get("port")):
            ports.add(master["port"])
    return ports


def taken_names(settings: Settings) -> set[str]:
    """Every server name here, lowercase: disabled and left-out servers' too."""
    return {s.name.lower() for s in settings.servers} | {
        str(block.get("name", "")).lower() for _, block, _ in every_block(settings) if block.get("name")}


def port_free(port: int) -> bool:
    """Nothing listens on TCP port (RCON) and UDP port (the game) can be bound right now."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", port))
    except OSError:
        return False
    return True


def next_free_port(settings: Settings, start: int = FIRST_PORT) -> int:
    taken = used_ports(settings) | {49175}
    port = max([start, *(p + 1 for p in taken if start <= p < MAX_PORT)])
    while port in taken or not port_free(port):
        port += 1
        if port > MAX_PORT:
            raise SetupError(f"no free port from {start} to {MAX_PORT}: give the server one (--port)")
    return port


def check_port(port: Any) -> str | None:
    """Why a game port can't be used (its console listens on it too), or None."""
    if not isinstance(port, int) or isinstance(port, bool) or not MIN_PORT <= port <= MAX_PORT:
        return f"the port must be a whole number from {MIN_PORT} to {MAX_PORT}"
    return None


def check_players(players: Any, *, co_op: bool = False) -> str | None:
    """Why a max_players can't be used, or None: 1-127, at most 16 for campaign co-op and ODST."""
    high = CO_OP_PLAYERS if co_op else MAX_PLAYERS
    if not isinstance(players, int) or isinstance(players, bool) or not 1 <= players <= high:
        return f"max_players must be from 1 to {high}" + (" (campaign co-op and ODST hold 16)" if co_op else "")
    return None


def generate_password(length: int = 32) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def relative_posix(target: Path, folder: Path) -> str:
    """target as a path relative to folder, with forward slashes (what dedicated.toml takes on both systems)."""
    try:
        return Path(os.path.relpath(target, folder)).as_posix()
    except ValueError:  # another drive on Windows: no relative path exists
        return target.resolve().as_posix()


def link_tree(source: Path, target: Path, say: Say | None = None) -> tuple[int, int]:
    """Every file of source under target: hard links where the drive allows (no space used), copies elsewhere."""
    linked = copied = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        destination = target / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        try:
            os.link(path, destination)
            linked += 1
        except OSError:
            shutil.copy2(path, destination)
            copied += 1
    if say:
        say(f"game files: {linked} hard-linked, {copied} copied from {source}")
    return linked, copied


def has_game_files(game: Path) -> bool:
    return (game / "halo3" / "halo3.dll").is_file() and (game / "halo3" / "maps" / "shared.map").is_file()


def has_odst_files(game: Path) -> bool:
    """Halo 3: ODST's engine and maps, which an ODST server needs beside Halo 3's (0.9.19)."""
    return (game / "halo3odst" / "halo3odst.dll").is_file() and (game / "halo3odst" / "maps" / "shared.map").is_file()


def add_odst_files(tool: Dedicated, game: Path, mcc_folder: str, stage: Path, say: Say) -> tuple[int, int]:
    """ODST's files from MCC into a game folder that has Halo 3's (the root's shared one too): `dedicated init
    --maps odst` copies them into a scratch folder, then they're linked in beside what's there, which stays as it
    is. (linked, copied) files; raises SetupError when the program couldn't copy them."""
    if stage.exists():
        shutil.rmtree(stage)
    say(f"copying Halo 3: ODST's files from {mcc_folder or 'Steam'} with dedicated init --maps odst "
        "(about 3.5 GB; hard links on the same drive)")
    try:
        result = tool.init(stage, mcc=mcc_folder, maps=["odst"])
        if not result.ok or not has_odst_files(stage / "game"):
            raise SetupError(f"dedicated init couldn't copy ODST's files: {result.problem or 'none arrived'}")
        linked, copied = link_tree(stage / "game", game)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    say(f"Halo 3: ODST's files are in {game}: {linked} hard-linked, {copied} copied")
    return linked, copied


def needs_odst(spec_server: dict[str, Any]) -> bool:
    return "odst" in spec_server


def odst_mcc(hint: str | None) -> Path | None:
    """The MCC folder to copy ODST's files from: the hint ([setup] mcc_folder) or one found on this PC, when it has
    Halo 3: ODST installed (it's a DLC: Steam, MCC, Properties, DLC)."""
    mcc = find_mcc(hint)
    return mcc if mcc is not None and (mcc / "halo3odst" / "halo3odst.dll").is_file() else None


def game_folders(settings: Settings) -> list[Path]:
    """Every folder with Halo 3's files on this root: the shared one first, then the servers' own."""
    found: list[Path] = []
    candidates = [settings.home / "game"]
    for folder in settings.server_folders:
        try:
            game = str(read_toml(folder / settings.config_name).get("game") or "game")
        except (OSError, ValueError):
            game = "game"
        candidates.append((folder / game).resolve())
    for game in candidates:
        if has_game_files(game) and game not in found:
            found.append(game)
    return found


def existing_top_keys(settings: Settings) -> dict[str, Any]:
    """Settings worth copying from a server folder that exists (the same machine, the same host)."""
    for folder in settings.server_folders:
        try:
            data = read_toml(folder / settings.config_name)
        except (OSError, ValueError):
            continue
        return {k: data[k] for k in COPIED_FROM_EXISTING if k in data}
    return {}


def shared_paths(settings: Settings, share: set[str]) -> dict[str, Path]:
    home = settings.home
    paths = {"game": home / "game", "content": home / "content", "bans": home / "bans.json"}
    return {k: v for k, v in paths.items() if k in share}


def ensure_shared(settings: Settings, spec: NewServer, tool: Dedicated, say: Say) -> dict[str, Path]:
    """The root's shared folders, made if missing. Returns what the new server points at."""
    paths = shared_paths(settings, spec.share)
    for key, path in paths.items():
        if key == "content" and not path.is_dir():
            for sub in ("Maps", "Game Modes", "Images", "Mods"):
                (path / sub).mkdir(parents=True, exist_ok=True)
            say(f"made the shared content folder {path}")
        elif key == "bans" and not path.is_file():
            path.write_text(json.dumps({"players": [], "ips": []}, indent=2) + "\n", encoding="utf-8")
            say(f"made the shared ban list {path}")
        elif key == "game" and not has_game_files(path):
            make_shared_game(settings, spec, tool, path, say)
    return paths


def make_shared_game(settings: Settings, spec: NewServer, tool: Dedicated, target: Path, say: Say) -> None:
    """Halo 3's files at the root: linked from a server that has them, or copied from MCC with `dedicated init`."""
    sources = [spec.link_from / "game"] if spec.link_from else []
    sources += [g for g in game_folders(settings) if g != target.resolve()]
    source = next((s for s in sources if has_game_files(s) and not is_mcc_install(s)), None)
    if source is not None:
        say(f"sharing Halo 3's files: linking {source} into {target} (no space used on the same drive)")
        link_tree(source, target, say)
        return
    if spec.mcc_folder is None:
        raise SetupError(f"no Halo 3 files to share yet: name your MCC folder ([setup] mcc_folder in "
                         f"{settings.path.name}, or --from), or put a server that has them in SERVERS/ first")
    stage = settings.records / "new-server-stage"
    if stage.exists():
        shutil.rmtree(stage)
    say(f"copying Halo 3's files from {spec.mcc_folder or 'Steam'} with dedicated init (hard links on the same drive)")
    result = tool.init(stage, mcc=spec.mcc_folder, maps=spec.maps)
    if not result.ok or not has_game_files(stage / "game"):
        shutil.rmtree(stage, ignore_errors=True)
        raise SetupError(f"dedicated init couldn't copy the game files: {result.problem}")
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(stage / "game"), str(target))
    shutil.rmtree(stage, ignore_errors=True)
    say(f"Halo 3's files are at {target}")


def is_mcc_install(game: Path) -> bool:
    """MCC's own folder (a server's `game` may name it): linking it whole would link every game MCC has."""
    return (game / "mcclauncher.exe").is_file() or (game / "MCC").is_dir() and (game / "halo1").is_dir()


def game_link_source(settings: Settings) -> Path | None:
    """The game folder a new server that doesn't share the root's links Halo 3's files from: the first here that
    has them (the root's, then each server's own, as its file names it; never MCC's own folder). None: there's
    none (they're copied from MCC). The window's N can pass it as link_from."""
    return next((game for game in game_folders(settings) if not is_mcc_install(game)), None)


def game_to_link(settings: Settings, spec: NewServer) -> Path | None:
    """Where a server that doesn't share the root's game folder links Halo 3's files from: link_from (a server
    folder with a game/, or a game folder) when it has them; with no link_from and no MCC to copy from, the first
    game folder here (game_link_source). None: `dedicated init` copies them from MCC, or there are none."""
    if spec.link_from is not None:
        return next((game for game in (spec.link_from / "game", spec.link_from)
                     if has_game_files(game) and not is_mcc_install(game)), None)
    return game_link_source(settings) if spec.mcc_folder is None else None


def plays_co_op(spec: NewServer) -> bool:
    server = {**(spec.package.server if spec.package else {}), **spec.extra}
    return "campaign" in server or "odst" in server


def create_server(settings: Settings, spec: NewServer, tool: Dedicated, say: Say = print) -> Outcome:
    """Make SERVERS/<name>/ and check it. Raises SetupError when nothing could be made; a build that fails part
    way leaves nothing behind (its staging folder is removed)."""
    name = spec.name.strip()
    if problem := valid_name(name):
        raise SetupError(problem)
    if settings.servers_folder is None:
        raise SetupError(f"{settings.path.name} has no servers_folder: new servers need SERVERS/")
    if name.lower() in taken_names(settings):
        raise SetupError(f"a server called {name} exists already (disabled and left-out servers count too)")
    if clash := slug_taken(settings, name):
        raise SetupError(f"{name} and {clash.name} would share their records and logs (both are "
                         f"{clash.slug} to Huragok): pick a name that differs in more than punctuation")
    final = settings.servers_folder / name
    if final.exists():
        raise SetupError(f"{final} exists already")
    if spec.port and (problem := check_port(spec.port)):
        raise SetupError(problem)
    if spec.max_players is not None and (problem := check_players(spec.max_players, co_op=plays_co_op(spec))):
        raise SetupError(problem)
    port = spec.port or next_free_port(settings)
    if port in used_ports(settings):
        raise SetupError(f"port {port} is used by another server here (or a discovery master)")
    if not port_free(port):
        raise SetupError(f"something on this machine already listens on port {port}")
    if spec.package is not None and spec.package.needs_campaign and not spec.maps:
        say("this package needs the campaign missions: copy them with dedicated init --maps campaign if missing")
    staging = settings.servers_folder / f".new-{slugify(name)}"
    if staging.exists():
        shutil.rmtree(staging)
    shared = ensure_shared(settings, spec, tool, say) if spec.share else {}
    autostart_off = False
    try:
        rcon_password, check, notes = _build(settings, spec, tool, say, name, port, staging, final, shared)
        ready = check.ok
        if not ready:  # before the folder gets its real name: discovery would start it
            notes.append(f"check found problems; autostart is off until they're fixed: {check.problem}")
            try:
                set_server_setting(settings.path, name, "autostart", False)
                autostart_off = True
            except OSError as error:
                notes.append(f"couldn't write autostart = false to {settings.path.name}: {error}")
        os.replace(staging, final)
    except BaseException as error:
        shutil.rmtree(staging, ignore_errors=True)
        if autostart_off:
            try:
                remove_server_setting(settings.path, name, "autostart")
            except OSError:
                pass
        if isinstance(error, SetupError) or not isinstance(error, Exception):
            raise
        raise SetupError(f"couldn't make {name}: {error}; nothing was left half-made") from error
    check.text = check.text.replace(str(staging), str(final)).replace(staging.name, final.name)
    return Outcome(final, name, port, rcon_password, check, ready, notes)


def _build(settings: Settings, spec: NewServer, tool: Dedicated, say: Say, name: str, port: int, staging: Path,
           final: Path, shared: dict[str, Path]) -> tuple[str, ToolResult, list[str]]:
    """Everything that goes into the staging folder, then `dedicated check`: (its console password, the check,
    notes for the host)."""
    notes: list[str] = []
    wants_odst = needs_odst(spec.extra) or spec.package is not None and (spec.package.needs_odst
                                                                       or needs_odst(spec.package.server))
    link_game = game_to_link(settings, spec) if "game" not in shared else None
    own_game_from_mcc = "game" not in shared and link_game is None and spec.mcc_folder is not None
    say(f"making {final.name} with dedicated init")
    result = tool.init(staging, mcc=spec.mcc_folder if own_game_from_mcc else None,
                       maps=spec.maps if own_game_from_mcc else None)
    if not result.ok:
        raise SetupError(f"dedicated init failed: {result.problem}")
    config = staging / settings.config_name
    if not config.is_file():
        raise SetupError(f"dedicated init made no {settings.config_name} in {staging}")
    if link_game is not None:
        link_tree(link_game, staging / "game", say)
    auto_update = settings.setup.auto_update if spec.auto_update is None else spec.auto_update
    # A server that updates itself replaces the program file it runs: that file must be its own, never the
    # root's copy every server shares.
    if find_executable(staging) is None and (auto_update or find_executable(settings.home) is None):
        program = staging / tool.executable.name  # the program that built it, as a hard link (no space) or a copy
        try:
            os.link(tool.executable, program)
        except OSError:
            shutil.copy2(tool.executable, program)
        say(f"the server program: {program.name}, linked from {tool.executable}")
    set_top_key(config, "auto_update", auto_update)
    say("it installs new Project Reclaimer releases itself (auto_update; the update restarts it at once, and "
        "Huragok steps in when one doesn't arrive)" if auto_update
        else "Project Reclaimer updates: by Huragok (U), as auto_update is off")

    for key, path in shared.items():
        set_top_key(config, key, relative_posix(path, staging))
        own = staging / ("bans.json" if key == "bans" else key)
        if own.is_dir():
            shutil.rmtree(own, ignore_errors=True)
        elif own.is_file():
            own.unlink()
    say("shared from the root: " + ", ".join(shared) if shared else "its own game, content and ban list")

    for key, value in existing_top_keys(settings).items():
        if key == "host_name" and spec.host_name:
            continue
        set_top_key(config, key, value)
    if spec.host_name:
        set_top_key(config, "host_name", spec.host_name)
    rcon_password = spec.rcon_password or generate_password()
    set_table_key(config, "rcon", "password", rcon_password)

    package = spec.package
    content_root = shared.get("content") or staging / "content"
    block: dict[str, Any] = {"name": name, "port": port}
    if package is not None:
        block.update(server_values(package, staging, content_root))
        for key, value in package.defaults.items():
            set_table_key(config, "defaults", key, value)
    elif not any(k in spec.extra for k in ("playlist", "map", "game", "variant", "campaign", "odst")):
        block["playlist"] = "playlists/slayer.playlist.json"  # the example rotation `dedicated init` writes
    if spec.hidden:
        block["hidden"] = True
    if spec.password:
        block["password"] = spec.password
    if spec.max_players:
        block["max_players"] = spec.max_players
    if spec.welcome:
        block["welcome"] = list(spec.welcome)
    block.update(spec.extra)
    replace_server_blocks(config, [block])

    if settings.setup.steamcmd and settings.setup.steam_username:  # the I key's sign-in, for its Workshop items
        set_table_key(config, "workshop", "steamcmd", settings.setup.steamcmd, before="master")
        set_table_key(config, "workshop", "username", settings.setup.steam_username, before="master")
        say(f"Workshop items come through SteamCMD as {settings.setup.steam_username}")
    workshop_items: list[int] = []
    if package is not None:
        kept: list[str] = []
        for copied in apply_package(package, staging, content_root, kept):
            say(f"copied {copied} from the package")
        for path in kept:
            notes.append(f"{path}: kept the one already in {content_root} (other servers may use it); the "
                         "package's own differs")
        workshop_items = list(package.workshop_items)
    if workshop_items:
        set_table_key(config, "workshop", "items", workshop_items, before="master")
        say(f"installing {len(workshop_items)} Workshop item(s) with dedicated mods (this can take a while)")
        mods = tool.mods(staging)
        for line in mods.lines[:20]:
            say(f"  {line}")
        if not mods.ok:
            notes.append(f"Workshop items not installed: {mods.problem}")

    if wants_odst:
        game = shared.get("game") or staging / "game"
        mcc = odst_mcc(spec.mcc_folder)
        if has_odst_files(game):
            say(f"Halo 3: ODST's files: already in {game}")
        elif mcc is None:
            notes.append(f"it plays Halo 3: ODST, whose files aren't in {game}, and no MCC with ODST installed was "
                         "found on this PC ([setup] mcc_folder names it; ODST is a DLC: Steam, MCC, Properties, DLC). "
                         f"D -> What it plays adds them once it's there, or run {FILES_HINT}")
        else:
            try:
                add_odst_files(tool, game, str(mcc), settings.records / "odst-stage", say)
            except (SetupError, OSError) as error:
                notes.append(f"Halo 3: ODST's files weren't added: {error}")

    say("checking it with dedicated check")
    return rcon_password, tool.check(staging), notes
