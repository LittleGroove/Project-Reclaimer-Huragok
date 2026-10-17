"""Read and check the supervisor's own folder, and find the servers.

The supervisor's folder (the root) holds:
  start.cmd, start.sh   the one way to start it
  settings.toml   how it runs (this module)
  commands.toml   who's who and who may use which command (permissions.py)
  automod.txt     the word list (parts/automod)
  SERVERS/        one folder per Project Reclaimer server: what `dedicated init` makes
                  (dedicated.toml, project-reclaimer.exe, game/, data/, ...)
  records/        its database and what it remembers; logs/  its logs

Each server folder's dedicated.toml names that folder's servers ([[server]] blocks,
usually one). We read it and each server's data/<server>/instance.json, but never
write them. instance.json is what the running server actually uses (environment
variables included), so it wins over dedicated.toml when both exist.

The 0.1 layout, the supervisor's folder inside one server folder, still works:
server_folder = ".." names that folder.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .permissions import PLAYER_ID, Permissions, PermissionsError, load_permissions

SETTINGS_FILE = "settings.toml"
COMMANDS_FILE = "commands.toml"
SERVERS_FOLDER = "SERVERS"
BACKENDS = ("attach", "process", "docker", "command")
BACKEND_ALIASES = {"windows": "process"}  # 0.1 called the process backend `windows`
CORE_TABLES = {"rcon", "keepalive", "updates", "restarts", "process", "windows", "docker", "command", "server",
               "setup", "relay", "server_updates"}
RELAY_ROLES = ("moderator", "admin", "owner")
PROGRAM_RELEASES = "ProjectReclaimer/project-reclaimer-releases"
IGNORED_PREFIXES = (".", "_")  # SERVERS/.something and SERVERS/_something are never servers (set aside, being made)
PART_SWITCHES = {"enabled", "dry_run", "handler_timeout"}
CLOCK_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FolderProblem:
    """A server folder (or one server in it) left out until it's fixed; every other server runs as usual."""

    folder: Path | None  # None: settings.toml names a server that isn't there
    text: str

    def __str__(self) -> str:
        return self.text


def slugify(name: str) -> str:
    """The server's data folder name: 'LG Test Server' -> 'lg-test-server'."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class Table:
    """A TOML table with typed getters whose errors name the setting."""

    def __init__(self, data: dict[str, Any], where: str = "") -> None:
        self.data = data
        self.where = where

    def _name(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def get(self, key: str, kind: type, default: Any) -> Any:
        value = self.data.get(key, default)
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if value is not None and not isinstance(value, kind):
            raise ConfigError(f"{self._name(key)} must be {kind.__name__}, not {type(value).__name__}")
        return value

    def strings(self, key: str) -> list[str]:
        value = self.data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self._name(key)} must be a list of text")
        return [v.strip() for v in value if v.strip()]

    def numbers(self, key: str, default: list[float]) -> list[float]:
        value = self.data.get(key, default)
        if not isinstance(value, list) or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                 and v >= 0 for v in value):
            raise ConfigError(f"{self._name(key)} must be a list of seconds, like [300, 60, 30]")
        return [float(v) for v in value]

    def table(self, key: str) -> Table:
        value = self.data.get(key, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{self._name(key)} must be a table, like [{self._name(key)}]")
        return Table(value, self._name(key))


@dataclass(frozen=True)
class RconEndpoint:
    host: str
    port: int
    password: str


@dataclass
class ServerConfig:
    name: str
    slug: str
    folder: Path  # the server folder: dedicated.toml, project-reclaimer.exe, game/, data/
    config_path: Path  # its dedicated.toml
    data_dir: Path
    game_port: int
    rcon_host: str
    rcon_port: int
    rcon_password: str
    service: str | None = None  # docker compose service, e.g. "server-1"
    admins: set[str] = field(default_factory=set)
    autostart: bool = True  # started whenever the supervisor starts
    daily_restart: str | None = None  # "04:00"; None: the [restarts] setting
    plays: str = ""  # what its [[server]] block says it plays: "playlist slayer", "Slayer on Guardian"...
    errors: list[str] = field(default_factory=list)  # shown in red; a server with errors is never started

    @property
    def host_key_path(self) -> Path:
        return self.data_dir / "rcon-host.key"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "server.log"

    def rcon_endpoint(self) -> RconEndpoint | None:
        """Where and how to sign in, read fresh: a restart may have changed it."""
        host, port, password = self.rcon_host, self.rcon_port, self.rcon_password
        try:
            rcon = json.loads((self.data_dir / "instance.json").read_text(encoding="utf-8")).get("rcon") or {}
            host = rcon.get("address") or host
            port = int(rcon.get("port") or port)
            password = rcon.get("password") or password
        except (OSError, ValueError):
            pass
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if not password:
            return None
        return RconEndpoint(host, port, password)

    def host_key(self) -> str | None:
        try:
            return self.host_key_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None


@dataclass
class UpdateSettings:
    """[updates]: new versions of the supervisor, from GitHub."""

    check: bool = True
    check_hours: float = 6.0
    install_on_server_restart: bool = False  # otherwise only when the host says so
    repository: str = "LittleGroove/Project-Reclaimer-Server-Supervisor"


@dataclass
class SetupSettings:
    """[setup]: making new servers (the window's N key, `new` from the command line)."""

    mcc_folder: str | None = None  # Halo 3's files from here; None: not set ("" would be Steam's default folder)
    share: list[str] = field(default_factory=lambda: ["game", "content", "bans"])  # what new servers take from the root
    packages_folder: str = "PACKAGES"
    program: str = ""  # the server program for making and checking folders, when it isn't in a server folder or at the root
    steamcmd: str = ""  # SteamCMD, for Workshop items; empty: looked for in the usual places
    steam_username: str = ""  # the Steam account that owns MCC, signed in to SteamCMD once (never a password here)


@dataclass(frozen=True)
class RelayUser:
    name: str
    password: str
    role: str  # moderator, admin or owner


@dataclass
class RelaySettings:
    """[relay]: the console relay for moderators elsewhere (the official RCON client through the supervisor)."""

    enabled: bool = False
    address: str = "127.0.0.1"
    port_offset: int = 1000  # each server's relay listens at its game port plus this (WebSocket, the RCON client)
    source_port_offset: int = 2000  # and Source RCON tools at its game port plus this; 0 switches that off
    users: list[RelayUser] = field(default_factory=list)


@dataclass
class ProgramUpdateSettings:
    """[server_updates]: new releases of the server program, installed at a quiet moment."""

    check: bool = True
    check_hours: float = 6.0
    install: str = "ask"  # ask (only say so), after_game, now
    repository: str = PROGRAM_RELEASES


@dataclass
class KeepaliveSettings:
    hang_seconds: float = 180.0
    check_seconds: float = 10.0
    act: bool = True


@dataclass
class RestartSettings:
    """[restarts]: warnings, crashes, the daily restart. Any of them can be repeated per server."""

    warn_seconds: list[float] = field(default_factory=lambda: [300.0, 180.0, 60.0, 30.0])
    after_game_seconds: float = 15.0  # notice once a game has ended, for a restart "after this game"
    daily_restart: str = ""  # local "HH:MM"; empty: none
    min_uptime_seconds: float = 900.0  # a daily restart waits until the server has been up this long
    backoff_seconds: list[float] = field(default_factory=lambda: [5.0, 10.0, 30.0, 60.0, 300.0])
    good_run_seconds: float = 600.0  # dying sooner than this after a start counts as a failed start
    max_failures: int = 5  # failed starts in a row before FAULTED
    restarts_per_hour: int = 6  # more automatic restarts than this in an hour: FAULTED
    ready_seconds: float = 300.0  # a started server's RCON must answer within this
    empty_seconds: float = 10.0  # a countdown ends once nobody has been on for this long


@dataclass
class PartSettings:
    """A part's section: [automod], [watchdog]..."""

    enabled: bool = False
    dry_run: bool = False  # record moderation actions as "would have...", never send them
    handler_timeout: float = 10.0
    options: dict[str, Any] = field(default_factory=dict)  # everything else in the section


@dataclass
class Settings:
    path: Path
    servers_folder: Path | None  # SERVERS/: one server folder each
    single_folder: Path | None  # server_folder: one server folder somewhere else (the 0.1 layout)
    config_name: str  # dedicated.toml
    backend: str
    backend_options: dict[str, Any]
    servers: list[ServerConfig]
    server_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)  # [[server]] blocks, by lower name
    only: list[str] = field(default_factory=list)  # `servers = [...]`: lower names; empty: all
    permissions: Permissions = field(default_factory=Permissions)
    name: str = "Supervisor"
    database: Path | None = None
    log_file: Path | None = None
    dry_run: bool = False
    use_host_key: bool = True
    command_timeout: float = 10.0
    keepalive: KeepaliveSettings = field(default_factory=KeepaliveSettings)
    updates: UpdateSettings = field(default_factory=UpdateSettings)
    restarts: RestartSettings = field(default_factory=RestartSettings)
    setup: SetupSettings = field(default_factory=SetupSettings)
    relay: RelaySettings = field(default_factory=RelaySettings)
    server_updates: ProgramUpdateSettings = field(default_factory=ProgramUpdateSettings)
    parts: dict[str, PartSettings] = field(default_factory=dict)  # every part, on or off
    problems: list[FolderProblem] = field(default_factory=list)  # server folders left out, found when loading

    @property
    def home(self) -> Path:
        """The supervisor's folder: settings.toml, commands.toml, automod.txt, SERVERS/, records/, logs/."""
        return self.path.parent

    @property
    def records(self) -> Path:
        return self.home / "records"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def packages_folder(self) -> Path:
        return self.home / self.setup.packages_folder

    @property
    def server_folders(self) -> list[Path]:
        seen: list[Path] = []
        for server in self.servers:
            if server.folder not in seen:
                seen.append(server.folder)
        return seen

    def server(self, name: str) -> ServerConfig:
        for server in self.servers:
            if server.name.lower() == name.lower():
                return server
        raise KeyError(name)

    def restart_setting(self, server: ServerConfig, key: str) -> Any:
        """A [restarts] value, or the server's own from its [[server]] block."""
        override = self.server_overrides.get(server.name.lower(), {})
        if key in override:
            return override[key]
        return getattr(self.restarts, key)


def _read_toml(path: Path, hint: str) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found{hint}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None


def _player_ids(values: list[str], where: str) -> set[str]:
    ids = {v.lower() for v in values}
    if bad := sorted(i for i in ids if not PLAYER_ID.fullmatch(i)):
        raise ConfigError(f"{where}: {bad[0]} is not a player ID (64 hexadecimal characters)")
    return ids


def _instances(data_dir: Path) -> dict[str, Path]:
    """Server name (lowercase) -> its data folder, from instance.json files."""
    found: dict[str, Path] = {}
    if data_dir.is_dir():
        for instance in data_dir.glob("*/instance.json"):
            try:
                name = json.loads(instance.read_text(encoding="utf-8")).get("name")
            except (OSError, ValueError):
                continue
            if isinstance(name, str):
                found[name.lower()] = instance.parent
    return found


def check_clock_time(value: str, where: str) -> str:
    """'04:00' -> '04:00'; anything else is an error. Empty means none."""
    value = value.strip()
    if not value:
        return ""
    match = CLOCK_TIME.match(value)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ConfigError(f"{where}: '{value}' isn't a time of day; use the 24-hour clock, like \"04:00\"")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def read_dedicated(path: Path, overrides: dict[str, dict[str, Any]]) -> list[ServerConfig]:
    """Every enabled server in one dedicated.toml, with its RCON settings."""
    top = Table(_read_toml(path, ""), path.name)
    base = path.parent
    data_dir = base / top.get("data", str, "data")
    rcon = top.table("rcon")
    defaults = top.table("defaults")
    admins = _player_ids(top.strings("admins"), f"{path.name} admins")
    instances = _instances(data_dir)

    blocks = top.data.get("server", [])
    if not isinstance(blocks, list) or not blocks:
        raise ConfigError(f"{path} has no [[server]] blocks")
    servers = []
    for number, block in enumerate(blocks, 1):
        entry = Table(block, f"{path.name} [[server]] {number}")
        name = entry.get("name", str, "")
        if not name:
            raise ConfigError(f"{entry.where}: has no name")
        if not entry.get("enabled", bool, defaults.get("enabled", bool, True)):
            continue
        port = entry.get("port", int, 0)
        extra = Table(overrides.get(name.lower(), {}), f"[[server]] {name}")
        folder = extra.get("data", str, None)
        daily = extra.get("daily_restart", str, None)
        if "playlist" in block:
            plays = f"playlist {Path(str(block['playlist'])).name.removesuffix('.playlist.json')}"
        elif "campaign" in block:
            mission = block["campaign"].get("mission", "") if isinstance(block["campaign"], dict) else ""
            plays = f"campaign {mission}".strip()
        elif "variant" in block:
            plays = f"variant {Path(str(block['variant'])).stem}"
        elif "map" in block or "game" in block:
            plays = f"{block.get('game', 'Slayer')} on {block.get('map', 'Guardian')}"
        else:
            plays = "Slayer on Guardian"
        server = ServerConfig(
            name=name,
            slug=slugify(name),
            folder=base,
            config_path=path,
            data_dir=(base / folder) if folder else instances.get(name.lower(), data_dir / slugify(name)),
            game_port=port,
            rcon_host=extra.get("rcon_host", str, rcon.get("address", str, "127.0.0.1")),
            rcon_port=extra.get("rcon_port", int, entry.get("rcon_port", int, port)),
            rcon_password=entry.get("rcon_password", str, rcon.get("password", str, "")),
            service=extra.get("service", str, None),
            admins=admins | _player_ids(entry.strings("admins"), f"{entry.where} admins"),
            autostart=extra.get("autostart", bool, True),
            daily_restart=check_clock_time(daily, f"[[server]] {name} daily_restart") if daily is not None else None,
            plays=plays,
        )
        if not server.rcon_password:
            server.errors.append(f"RCON is off in {path.name}: set [rcon] password. The supervisor needs it to "
                                 "see players, warn them and moderate")
        servers.append(server)
    return servers


def find_server_folders(home: Path, servers_folder: Path | None, single_folder: Path | None,
                        config_name: str = "dedicated.toml") -> list[Path]:
    """Every folder holding a dedicated.toml: SERVERS/*, and server_folder if set."""
    found: list[Path] = []
    if single_folder is not None and (single_folder / config_name).is_file():
        found.append(single_folder.resolve())
    if servers_folder is not None and servers_folder.is_dir():
        for child in sorted(servers_folder.iterdir(), key=lambda p: p.name.lower()):
            if child.name.startswith(IGNORED_PREFIXES):
                continue
            if child.is_dir() and (child / config_name).is_file() and child.resolve() not in found:
                found.append(child.resolve())
    return found


def read_servers(folders: list[Path], config_name: str, overrides: dict[str, dict[str, Any]],
                 only: list[str], problems: list[FolderProblem] | None = None) -> list[ServerConfig]:
    """The servers in these folders, checked against each other: names and ports must be unique.

    With `problems`, a folder whose dedicated.toml can't be read, or a server named
    like one found before it, is left out and noted there, so one hand edit gone
    wrong never keeps the other servers from running. Without it, they raise.
    """
    def left_out(folder: Path | None, text: str, outcome: str = "") -> None:
        if problems is None:
            raise ConfigError(text)
        problems.append(FolderProblem(folder, text + outcome))

    servers: list[ServerConfig] = []
    for folder in folders:
        try:
            servers.extend(read_dedicated(folder / config_name, overrides))
        except ConfigError as error:
            text = str(error) if str(folder) in str(error) else f"{folder / config_name}: {error}"
            left_out(folder, text, ". Its servers are left out until that's fixed")
    if only:
        known = {s.name.lower() for s in servers}
        for name in [n for n in only if n.lower() not in known]:
            left_out(None, f"servers: '{name}' is not an enabled server in any server folder")
        servers = [s for s in servers if s.name.lower() in {n.lower() for n in only}]
    seen: dict[str, ServerConfig] = {}
    unique: list[ServerConfig] = []
    for server in servers:
        key = server.name.lower()
        other = seen.get(key)
        if other is not None:
            keep, drop = other, server
            if server.folder.name.lower() == key and other.folder.name.lower() != key:
                keep, drop = server, other  # the folder named after the server is the real one, the other a copy
            left_out(drop.folder, f"two servers are called '{server.name}': in {other.folder} and {server.folder}. "
                     "Server names must be unique across the SERVERS folder",
                     f"; the one in {drop.folder} is left out until one is renamed")
            unique[unique.index(other)] = seen[key] = keep
            continue
        seen[key] = server
        unique.append(server)
    servers = unique
    ports: dict[int, ServerConfig] = {}
    for server in servers:
        for port in {p for p in (server.game_port, server.rcon_port) if p}:
            other = ports.get(port)
            if other is not None and other is not server:
                server.errors.append(f"port {port} is also used by {other.name}")
            ports.setdefault(port, server)
    return servers


def load(path: str | os.PathLike[str]) -> Settings:
    path = Path(path).resolve()
    top = Table(_read_toml(path, ": start the supervisor once to make it"))
    base = path.parent

    servers_folder_name = top.get("servers_folder", str, SERVERS_FOLDER)
    servers_folder = (base / servers_folder_name).resolve() if servers_folder_name else None
    single_name = top.get("server_folder", str, "")
    single_folder = (base / single_name).resolve() if single_name else None
    config_name = top.get("dedicated_config", str, "dedicated.toml")
    if single_folder is not None and not (single_folder / config_name).is_file():
        raise ConfigError(f"{single_folder / config_name} not found: server_folder should name a server folder "
                          f"(the one holding {config_name}), or be left empty to use {servers_folder_name}/")

    backend = top.get("backend", str, "process")
    backend = BACKEND_ALIASES.get(backend, backend)
    if backend not in BACKENDS:
        raise ConfigError(f"backend: '{backend}' is not one of {', '.join(BACKENDS)}")
    backend_options = dict(top.table(backend).data)
    if backend == "process":  # the 0.1 name for its section still counts
        backend_options = {**top.table("windows").data, **backend_options}

    overrides: dict[str, dict[str, Any]] = {}
    for number, block in enumerate(top.data.get("server", []), 1):
        entry = Table(block, f"[[server]] {number}")
        name = entry.get("name", str, "")
        if not name:
            raise ConfigError(f"{entry.where}: name the server as its dedicated.toml does")
        if stray := sorted(set(entry.data) - {"name", "autostart", "daily_restart", "service", "data", "rcon_host",
                                              "rcon_port", "warn_seconds", "min_uptime_seconds"}):
            raise ConfigError(f"[[server]] {name}: '{stray[0]}' isn't a setting a server can have here")
        overrides[name.lower()] = dict(entry.data)

    only = top.strings("servers")
    folders = find_server_folders(base, servers_folder, single_folder, config_name)
    problems: list[FolderProblem] = []
    servers = read_servers(folders, config_name, overrides, only, problems)
    known = {s.name.lower() for s in servers}
    if folders:  # with no folders at all, the first start's hints are the useful message
        # A folder taken away (or left out above) must not keep the rest from starting.
        for name in [overrides[n]["name"] for n in overrides if n not in known]:
            problems.append(FolderProblem(None, f"[[server]] '{name}' in {path.name} is not an enabled server in any "
                                                "server folder, so its settings there do nothing"))

    rcon = top.table("rcon")
    keep = top.table("keepalive")
    updates = top.table("updates")
    restarts = top.table("restarts")
    setup = top.table("setup")
    share = [s.lower() for s in setup.strings("share")] if "share" in setup.data else ["game", "content", "bans"]
    if unknown_share := [s for s in share if s not in ("game", "content", "bans")]:
        raise ConfigError(f"setup.share: '{unknown_share[0]}' isn't one of game, content, bans")
    relay = top.table("relay")
    users: list[RelayUser] = []
    raw_users = relay.data.get("user", [])
    if not isinstance(raw_users, list):
        raise ConfigError("[relay]: list moderators as [[relay.user]] blocks, each with name, password and role")
    for number, raw in enumerate(raw_users, 1):
        entry = Table(raw if isinstance(raw, dict) else {}, f"[[relay.user]] {number}")
        name = entry.get("name", str, "").strip()
        password = entry.get("password", str, "")
        role = entry.get("role", str, "moderator").strip().lower()
        if not name:
            raise ConfigError(f"{entry.where}: has no name")
        if not 8 <= len(password) <= 128:
            raise ConfigError(f"[[relay.user]] {name}: password must be 8 to 128 characters")
        if any(u.password == password for u in users):
            raise ConfigError(f"[[relay.user]] {name}: another user has the same password, and the password is "
                              "what tells users apart")
        if role not in RELAY_ROLES:
            raise ConfigError(f"[[relay.user]] {name}: role must be one of {', '.join(RELAY_ROLES)}")
        users.append(RelayUser(name, password, role))
    program_updates = top.table("server_updates")
    install = program_updates.get("install", str, "ask").strip().lower()
    if install not in ("ask", "after_game", "now"):
        raise ConfigError("server_updates.install: must be ask, after_game or now")
    program_repository = program_updates.get("repository", str, PROGRAM_RELEASES)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", program_repository):
        raise ConfigError(f"server_updates.repository: '{program_repository}' should look like owner/name")
    repository = updates.get("repository", str, UpdateSettings.repository)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ConfigError(f"updates.repository: '{repository}' should look like owner/name, as in its GitHub address")
    if moved := sorted(k for k in ("roles", "commands") if k in top.data):
        raise ConfigError(f"[{moved[0]}]: who's who and who may use which command are set in {COMMANDS_FILE}")
    parts = _parts(top)
    try:
        permissions = load_permissions(base / COMMANDS_FILE)
    except PermissionsError as error:
        raise ConfigError(str(error)) from None

    def optional_path(key: str, default: str) -> Path | None:
        value = top.get(key, str, default)
        return base / value if value else None

    max_failures = restarts.get("max_failures", int, 5)
    per_hour = restarts.get("restarts_per_hour", int, 6)
    if max_failures < 1 or per_hour < 1:
        raise ConfigError("restarts: max_failures and restarts_per_hour must be 1 or more")

    return Settings(
        path=path,
        servers_folder=servers_folder,
        single_folder=single_folder,
        config_name=config_name,
        backend=backend,
        backend_options=backend_options,
        servers=servers,
        server_overrides=overrides,
        only=only,
        permissions=permissions,
        name=top.get("name", str, "Supervisor"),
        database=optional_path("database", "records/supervisor.sqlite3"),
        log_file=optional_path("log_file", "logs/supervisor.log"),
        dry_run=top.get("dry_run", bool, False),
        use_host_key=rcon.get("use_host_key", bool, True),
        command_timeout=rcon.get("command_timeout", float, 10.0),
        keepalive=KeepaliveSettings(
            hang_seconds=keep.get("hang_seconds", float, 180.0),
            check_seconds=keep.get("check_seconds", float, 10.0),
            act=keep.get("act", bool, True),
        ),
        updates=UpdateSettings(
            check=updates.get("check", bool, True),
            check_hours=max(1.0, updates.get("check_hours", float, 6.0)),
            install_on_server_restart=updates.get("install_on_server_restart", bool, False),
            repository=repository,
        ),
        restarts=RestartSettings(
            warn_seconds=restarts.numbers("warn_seconds", [300, 180, 60, 30]),
            after_game_seconds=restarts.get("after_game_seconds", float, 15.0),
            daily_restart=check_clock_time(restarts.get("daily_restart", str, ""), "restarts.daily_restart"),
            min_uptime_seconds=restarts.get("min_uptime_seconds", float, 900.0),
            backoff_seconds=restarts.numbers("backoff_seconds", [5, 10, 30, 60, 300]) or [5.0],
            good_run_seconds=restarts.get("good_run_seconds", float, 600.0),
            max_failures=max_failures,
            restarts_per_hour=per_hour,
            ready_seconds=restarts.get("ready_seconds", float, 300.0),
            empty_seconds=restarts.get("empty_seconds", float, 10.0),
        ),
        setup=SetupSettings(
            mcc_folder=setup.get("mcc_folder", str, None),
            share=share,
            packages_folder=setup.get("packages_folder", str, "PACKAGES") or "PACKAGES",
            program=setup.get("program", str, ""),
            steamcmd=setup.get("steamcmd", str, ""),
            steam_username=setup.get("steam_username", str, ""),
        ),
        relay=RelaySettings(
            enabled=relay.get("enabled", bool, False),
            address=relay.get("address", str, "127.0.0.1") or "127.0.0.1",
            port_offset=relay.get("port_offset", int, 1000),
            source_port_offset=relay.get("source_port_offset", int, 2000),
            users=users,
        ),
        server_updates=ProgramUpdateSettings(
            check=program_updates.get("check", bool, True),
            check_hours=max(1.0, program_updates.get("check_hours", float, 6.0)),
            install=install,
            repository=program_repository,
        ),
        parts=parts,
        problems=problems,
    )


def discover_servers(settings: Settings, problems: list[FolderProblem] | None = None) -> list[ServerConfig]:
    """The servers in the server folders right now, for picking up folders added while running."""
    folders = find_server_folders(settings.home, settings.servers_folder, settings.single_folder,
                                  settings.config_name)
    return read_servers(folders, settings.config_name, settings.server_overrides, settings.only, problems)


def _parts(top: Table) -> dict[str, PartSettings]:
    """One section per part that has settings; a section that isn't one is a mistake worth naming."""
    from .parts import PARTS  # here, not at the top: the parts import this module

    sections = {k: v for k, v in top.data.items() if isinstance(v, dict) and k not in CORE_TABLES}
    optional = [name for name, cls in PARTS.items() if not cls.always_on]
    if unknown := sorted(set(sections) - set(optional)):
        raise ConfigError(f"[{unknown[0]}] isn't a section of {SETTINGS_FILE}; the parts with settings are "
                          f"{', '.join(optional)}")
    result = {}
    for name, cls in PARTS.items():
        section = Table(sections.get(name, {}), name)
        result[name] = PartSettings(
            enabled=cls.always_on or section.get("enabled", bool, cls.enabled_by_default),
            dry_run=section.get("dry_run", bool, False),
            handler_timeout=section.get("handler_timeout", float, 10.0),
            options={k: v for k, v in section.data.items() if k not in PART_SWITCHES},
        )
    return result
