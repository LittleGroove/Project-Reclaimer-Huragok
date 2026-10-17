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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings, slugify
from .dedicated import Dedicated, ToolResult, find_executable
from .packages import Package, apply_package
from .tomledit import read_toml, replace_server_blocks, set_server_setting, set_table_key, set_top_key

FIRST_PORT = 49176
SHAREABLE = ("game", "content", "bans")
COPIED_FROM_EXISTING = ("host_name", "stats_server", "public_address", "upnp")
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
    link_from: Path | None = None  # a server folder whose game/ is hard-linked (same drive: no space used)
    host_name: str | None = None
    rcon_password: str = ""  # empty: made up
    welcome: list[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # more [[server]] keys (map, game, voice...)


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
    """Why a name can't be a server folder's, or None."""
    name = name.strip()
    if not name:
        return "the name is empty"
    if name.startswith((".", "_")):
        return "a name starting with . or _ is a folder the supervisor ignores"
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        return "the name has a character a folder name can't have"
    if name.endswith((" ", ".")) or len(name) > 64:
        return "the name must be at most 64 characters and not end with a space or a dot"
    return None


def used_ports(settings: Settings) -> set[int]:
    return {p for s in settings.servers for p in (s.game_port, s.rcon_port) if p}


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
    port = max([start, *(p + 1 for p in taken if p >= start)])
    while port in taken or not port_free(port):
        port += 1
    return port


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
    source = next((s for s in sources if has_game_files(s)), None)
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


def create_server(settings: Settings, spec: NewServer, tool: Dedicated, say: Say = print) -> Outcome:
    """Make SERVERS/<name>/ and check it. Raises SetupError when nothing could be made."""
    name = spec.name.strip()
    if problem := valid_name(name):
        raise SetupError(problem)
    if settings.servers_folder is None:
        raise SetupError(f"{settings.path.name} has no servers_folder: new servers need SERVERS/")
    if any(s.name.lower() == name.lower() for s in settings.servers):
        raise SetupError(f"a server called {name} exists already")
    final = settings.servers_folder / name
    if final.exists():
        raise SetupError(f"{final} exists already")
    port = spec.port or next_free_port(settings)
    if port in used_ports(settings):
        raise SetupError(f"port {port} is used by another server here")
    if not port_free(port):
        raise SetupError(f"something on this machine already listens on port {port}")
    if spec.package is not None and spec.package.needs_campaign and not spec.maps:
        say("this package needs the campaign missions: copy them with dedicated init --maps campaign if missing")
    staging = settings.servers_folder / f".new-{slugify(name)}"
    if staging.exists():
        shutil.rmtree(staging)
    notes: list[str] = []
    shared = ensure_shared(settings, spec, tool, say) if spec.share else {}

    own_game_from_mcc = "game" not in shared and spec.link_from is None and spec.mcc_folder is not None
    say(f"making {final.name} with dedicated init")
    result = tool.init(staging, mcc=spec.mcc_folder if own_game_from_mcc else None,
                       maps=spec.maps if own_game_from_mcc else None)
    if not result.ok:
        shutil.rmtree(staging, ignore_errors=True)
        raise SetupError(f"dedicated init failed: {result.problem}")
    config = staging / settings.config_name
    if not config.is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise SetupError(f"dedicated init made no {settings.config_name} in {staging}")
    if "game" not in shared and spec.link_from is not None:
        link_tree(spec.link_from / "game", staging / "game", say)
    if find_executable(staging) is None and find_executable(settings.home) is None:
        program = staging / tool.executable.name  # the program that built it, as a hard link (no space) or a copy
        try:
            os.link(tool.executable, program)
        except OSError:
            shutil.copy2(tool.executable, program)
        say(f"the server program: {program.name}, linked from {tool.executable}")

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
    block: dict[str, Any] = {"name": name, "port": port}
    if package is not None:
        block.update(package.server)
        for key, value in package.defaults.items():
            set_table_key(config, "defaults", key, value)
    elif not any(k in spec.extra for k in ("playlist", "map", "game", "variant", "campaign")):
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

    content_root = shared.get("content") or staging / "content"
    workshop_items: list[int] = []
    if package is not None:
        for copied in apply_package(package, staging, content_root):
            say(f"copied {copied} from the package")
        workshop_items = list(package.workshop_items)
    if workshop_items:
        set_table_key(config, "workshop", "items", workshop_items, before="master")
        say(f"installing {len(workshop_items)} Workshop item(s) with dedicated mods (this can take a while)")
        mods = tool.mods(staging)
        for line in mods.lines[:20]:
            say(f"  {line}")
        if not mods.ok:
            notes.append(f"Workshop items not installed: {mods.problem}")

    say("checking it with dedicated check")
    check = tool.check(staging)
    ready = check.ok
    os.replace(staging, final)
    check.text = check.text.replace(str(staging), str(final)).replace(staging.name, final.name)
    if not ready:
        notes.append(f"check found problems; autostart is off until they're fixed: {check.problem}")
        try:
            set_server_setting(settings.path, name, "autostart", False)
        except OSError as error:
            notes.append(f"couldn't write autostart = false to {settings.path.name}: {error}")
    return Outcome(final, name, port, rcon_password, check, ready, notes)
