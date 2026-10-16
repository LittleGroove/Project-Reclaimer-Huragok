"""Read and check the supervisor's own folder, and find the servers from the host's dedicated.toml.

The supervisor's folder holds:
  settings.toml   how it runs (this module)
  commands.toml   who's who and who may use which command (permissions.py)
  automod.txt     the word list (parts/automod)
  records/        its database; logs/  its logs

We read dedicated.toml and each server's data/<server>/instance.json, but never
write them. instance.json is what the running server actually uses (environment
variables included), so it wins over dedicated.toml when both exist.
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
BACKENDS = ("attach", "windows", "docker", "command")
CORE_TABLES = {"rcon", "keepalive", "updates", "windows", "docker", "command", "server"}
PART_SWITCHES = {"enabled", "dry_run", "handler_timeout"}


class ConfigError(Exception):
    pass


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
    data_dir: Path
    game_port: int
    rcon_host: str
    rcon_port: int
    rcon_password: str
    service: str | None = None  # docker compose service, e.g. "server-1"
    admins: set[str] = field(default_factory=set)

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
class KeepaliveSettings:
    hang_seconds: float = 180.0
    check_seconds: float = 10.0
    act: bool = True


@dataclass
class PartSettings:
    """A part's section: [automod], [watchdog]…"""

    enabled: bool = False
    dry_run: bool = False  # record moderation actions as "would have…", never send them
    handler_timeout: float = 10.0
    options: dict[str, Any] = field(default_factory=dict)  # everything else in the section


@dataclass
class Settings:
    path: Path
    server_folder: Path
    dedicated_config: Path
    backend: str
    backend_options: dict[str, Any]
    servers: list[ServerConfig]
    permissions: Permissions = field(default_factory=Permissions)
    name: str = "Supervisor"
    database: Path | None = None
    log_file: Path | None = None
    dry_run: bool = False
    use_host_key: bool = True
    command_timeout: float = 10.0
    keepalive: KeepaliveSettings = field(default_factory=KeepaliveSettings)
    updates: UpdateSettings = field(default_factory=UpdateSettings)
    parts: dict[str, PartSettings] = field(default_factory=dict)  # every part, on or off

    @property
    def home(self) -> Path:
        """The supervisor's folder: settings.toml, commands.toml, automod.txt, records/, logs/."""
        return self.path.parent

    @property
    def records(self) -> Path:
        return self.home / "records"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    def server(self, name: str) -> ServerConfig:
        for server in self.servers:
            if server.name.lower() == name.lower():
                return server
        raise KeyError(name)


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


def read_dedicated(path: Path, overrides: dict[str, Table]) -> list[ServerConfig]:
    """Every enabled server in the host's dedicated.toml, with its RCON settings."""
    top = Table(_read_toml(path, ": set server_folder to the folder that holds it"), path.name)
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
        extra = overrides.get(name.lower(), Table({}))
        folder = extra.get("data", str, None)
        servers.append(ServerConfig(
            name=name,
            slug=slugify(name),
            data_dir=(base / folder) if folder else instances.get(name.lower(), data_dir / slugify(name)),
            game_port=port,
            rcon_host=extra.get("rcon_host", str, rcon.get("address", str, "127.0.0.1")),
            rcon_port=extra.get("rcon_port", int, entry.get("rcon_port", int, port)),
            rcon_password=entry.get("rcon_password", str, rcon.get("password", str, "")),
            service=extra.get("service", str, None),
            admins=admins | _player_ids(entry.strings("admins"), f"{entry.where} admins"),
        ))
    return servers


def load(path: str | os.PathLike[str]) -> Settings:
    path = Path(path).resolve()
    top = Table(_read_toml(path, ": start the supervisor once to make it"))
    base = path.parent

    folder = top.get("server_folder", str, "..")
    if not folder:
        raise ConfigError("server_folder: set it to the folder that holds the server's dedicated.toml")
    server_folder = (base / folder).resolve()
    dedicated_config = server_folder / top.get("dedicated_config", str, "dedicated.toml")
    if not dedicated_config.is_file():
        raise ConfigError(f"{dedicated_config} not found. Put the supervisor's folder inside your server folder "
                          f"(next to dedicated.toml), or set server_folder in {path.name}")

    backend = top.get("backend", str, "attach")
    if backend not in BACKENDS:
        raise ConfigError(f"backend: '{backend}' is not one of {', '.join(BACKENDS)}")

    overrides: dict[str, Table] = {}
    for number, block in enumerate(top.data.get("server", []), 1):
        entry = Table(block, f"[[server]] {number}")
        name = entry.get("name", str, "")
        if not name:
            raise ConfigError(f"{entry.where}: name the server as dedicated.toml does")
        overrides[name.lower()] = entry

    servers = read_dedicated(dedicated_config, overrides)
    known = {s.name.lower() for s in servers}
    if unknown := [t.data["name"] for n, t in overrides.items() if n not in known]:
        raise ConfigError(f"[[server]] '{unknown[0]}' is not an enabled server in {dedicated_config.name}")
    wanted = top.strings("servers")
    if wanted:
        missing = [n for n in wanted if n.lower() not in known]
        if missing:
            raise ConfigError(f"servers: '{missing[0]}' is not an enabled server in {dedicated_config.name}")
        servers = [s for s in servers if s.name.lower() in {n.lower() for n in wanted}]

    rcon = top.table("rcon")
    keep = top.table("keepalive")
    updates = top.table("updates")
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

    return Settings(
        path=path,
        server_folder=server_folder,
        dedicated_config=dedicated_config,
        backend=backend,
        backend_options=dict(top.table(backend).data),
        servers=servers,
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
        parts=parts,
    )


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
