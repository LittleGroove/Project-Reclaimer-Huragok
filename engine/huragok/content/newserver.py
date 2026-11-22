"""A new server folder (0.7.0, answer 6): the folder in SERVERS/, the program's own `dedicated init` in it, and
nothing more. No package, no folders shared from Huragok's, no console password, no auto_update, no SteamCMD lines:
what `init` writes is what the server gets.

0.10.8's `init` makes two servers in the folder, on 49176 and 49177, with its master on 49175 (simulation S4), and on
most hosts those ports are taken. So the quick actions follow (content/quickactions.py: one server only, free ports,
the console), each shown before it's written, and only when the host says yes.

The folder is made as SERVERS/.new-<name>/ (a folder starting with a dot is never picked up) and given its name once
the host is done with its quick actions (finish), so Huragok never starts a server half made, nor one whose quick
actions are still being chosen. A server with no copy of the program in its folder runs the one in Huragok's; when
there's none there either, the program that made it is linked in (copied where the drive can't link), or it couldn't
run at all.

Also here, for the host's own tools: the names a server can have, whether a port is free, getting Halo 3: ODST's
files from MCC (C's check offers it), and the game folders Huragok's folder has.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import socket
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.serverfolders import slug_taken, slugify
from ..core.settings import Settings
from ..core.tomledit import read_toml
from ..games.files import has_game_files, has_odst_files
from ..program.dedicated import Dedicated, find_executable
from ..program.settings import every_block
from ..steam import find_mcc

FIRST_PORT = 49176  # where a free port is looked for first: init's own first port
MAX_NAME = 48  # the server program's limit for a [[server]] name
MIN_PORT, MAX_PORT = 1024, 65535
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}
STAGING = ".new-"  # SERVERS/.new-<slug>: a folder being made
Say = Callable[[str], None]


class SetupError(Exception):
    pass


@dataclass
class Outcome:
    """A folder `dedicated init` made, waiting in SERVERS/.new-<slug>/ for the host's quick actions."""

    name: str  # the folder's name, as the host gave it
    folder: Path  # where it goes: SERVERS/<name>
    staging: Path  # where it is until finish(): SERVERS/.new-<slug>
    config_name: str
    servers: list[tuple[str, int | None]] = field(default_factory=list)  # init's [[server]] blocks: name, game port
    master: int | None = None  # init's master port while it's on; None: off
    version: str | None = None  # the program that made it, as its --version says
    program: Path | None = None  # the program linked in (there's none in Huragok's folder), or None

    @property
    def config(self) -> Path:
        """Its dedicated.toml where it is now (the staging folder until finish())."""
        return (self.staging if self.staging.is_dir() else self.folder) / self.config_name

    @property
    def shown(self) -> str:
        """Its dedicated.toml as the host will find it: SERVERS\\<name>\\dedicated.toml."""
        return str(Path(self.folder.parent.name) / self.folder.name / self.config_name)

    @property
    def ports(self) -> list[int]:
        """Every port init gave it: its servers' game ports, then its master's."""
        return [port for _, port in self.servers if port] + ([self.master] if self.master else [])


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


def taken_names(settings: Settings) -> set[str]:
    """Every server name here, lowercase: disabled and left-out servers' too."""
    return {s.name.lower() for s in settings.servers} | {
        str(block.get("name", "")).lower() for _, block, _ in every_block(settings) if block.get("name")}


def port_free(port: int) -> bool:
    """Nothing listens on TCP port (RCON) and UDP port (the game) can be bound right now. For when the system's own
    table of sockets can't be read (content/quickactions.py reads that first)."""
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


def check_port(port: Any) -> str | None:
    """Why a game port can't be used (its console listens on it too), or None."""
    if not isinstance(port, int) or isinstance(port, bool) or not MIN_PORT <= port <= MAX_PORT:
        return f"the port must be a whole number from {MIN_PORT} to {MAX_PORT}"
    return None


def generate_password(length: int = 24) -> str:
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


def add_odst_files(tool: Dedicated, game: Path, mcc_folder: str, stage: Path, say: Say) -> tuple[int, int]:
    """ODST's files from MCC into a game folder that has Halo 3's, when the host asks (C's check): `dedicated init
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


def odst_mcc(hint: str | None) -> Path | None:
    """The MCC folder to copy ODST's files from: the hint ([setup] mcc_folder) or one found on this PC, when it has
    Halo 3: ODST installed (it's a DLC: Steam, MCC, Properties, DLC)."""
    mcc = find_mcc(hint)
    return mcc if mcc is not None and (mcc / "halo3odst" / "halo3odst.dll").is_file() else None


def game_folders(settings: Settings) -> list[Path]:
    """Every folder with Halo 3's files on this root: Huragok's own game/ first, then the servers' own."""
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


# A new server -----------------------------------------------------------------------------------------------------

def create_server(settings: Settings, name: str, tool: Dedicated, say: Say = print) -> Outcome:
    """SERVERS/.new-<slug>/ made with the program's own `dedicated init`, and nothing else written (finish() gives it
    its name). Raises SetupError, with nothing left behind, when it can't be made."""
    name = name.strip()
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
    staging = settings.servers_folder / f"{STAGING}{slugify(name)}"
    if staging.exists():  # left by a window closed half way: never picked up, nobody's
        shutil.rmtree(staging)
    say(f"making {name} with dedicated init")
    program = None
    try:
        result = tool.init(staging)
        if not result.ok:
            raise SetupError(f"dedicated init failed: {result.problem}")
        config = staging / settings.config_name
        if not config.is_file():
            raise SetupError(f"dedicated init made no {settings.config_name} in {staging}")
        if find_executable(staging) is None and find_executable(settings.home) is None:
            program = staging / tool.executable.name  # the program that made it, as a hard link (no space) or a copy
            try:
                os.link(tool.executable, program)
            except OSError:
                shutil.copy2(tool.executable, program)
            say(f"the server program: {program.name}, linked from {tool.executable} (Huragok's folder has none)")
        data = read_toml(config)
    except BaseException as error:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(error, SetupError) or not isinstance(error, Exception):
            raise
        raise SetupError(f"couldn't make {name}: {error}; nothing was left half-made") from error
    from .gamefiles import program_version_of

    servers, master = _servers_in(data)
    return Outcome(name, final, staging, settings.config_name, servers, master, program_version_of(tool), program)


def finish(outcome: Outcome) -> Path:
    """The folder given its name: from here Huragok picks it up (within seconds while it runs) and starts it once its
    ports are free. Raises SetupError when it can't be renamed (it stays where it is, never picked up)."""
    if not outcome.staging.is_dir():
        return outcome.folder
    if outcome.folder.exists():
        raise SetupError(f"{outcome.folder} exists now: the new folder stays as {outcome.staging.name}; rename it by "
                         "hand once the other is out of the way")
    try:
        os.replace(outcome.staging, outcome.folder)
    except OSError as error:
        raise SetupError(f"{outcome.staging.name} couldn't be renamed {outcome.folder.name} ({error.strerror or error}):"
                         " a program may have a file in it open; close it and rename the folder by hand") from None
    return outcome.folder


def read_folder(config: Path) -> tuple[list[tuple[str, int | None]], int | None]:
    """A folder's servers as its dedicated.toml has them now, (name, game port) each, and its master's port while it's
    on (None: off). Raises SetupError when the file can't be read."""
    try:
        return _servers_in(read_toml(config))
    except (OSError, ValueError) as error:
        raise SetupError(f"{config} can't be read: {error}") from None


def _servers_in(data: dict[str, Any]) -> tuple[list[tuple[str, int | None]], int | None]:
    blocks = [b for b in data.get("server", []) if isinstance(b, dict)] if isinstance(data.get("server"), list) else []
    master = data.get("master") if isinstance(data.get("master"), dict) else {}
    return ([(str(b.get("name") or ""), _port(b.get("port"))) for b in blocks],
            _port(master.get("port")) if master.get("enabled", True) is not False else None)


def _port(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
