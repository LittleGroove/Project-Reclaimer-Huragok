"""The servers Huragok looks after: one folder each (SERVERS/<name>/, the folder `dedicated init` makes), its
dedicated.toml read into a ServerConfig, checked against the others (unique names, slugs and ports; RCON on). A
folder that can't be read is left out alone, with why (FolderProblem); the rest are looked after. This is each
server's own side; settings.toml (settings.py) is Huragok's, for every server.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..games.plays import plays_kind, plays_of
from .tables import ConfigError, Table, _read_toml, check_clock_time
from .text import STORED_ID
from .text import slug as text_slug

if TYPE_CHECKING:
    from .settings import Settings


SERVERS_FOLDER = "SERVERS"


IGNORED_PREFIXES = (".", "_")  # SERVERS/.something and SERVERS/_something are never servers (set aside, being made)
INSTANCE_SLACK = 120.0  # seconds: 0.9 writes instance.json and rcon-host.key within moments of each other at a start


@dataclass(frozen=True)
class FolderProblem:
    """A server folder (or one server in it) left out until it's fixed; every other server runs as usual."""

    folder: Path | None  # None: settings.toml names a server that isn't there
    text: str

    def __str__(self) -> str:
        return self.text


def slugify(name: str) -> str:
    """The name of the server's own files (its data folder, records/servers/<slug>.json, logs/console-<slug>.log):
    'LG Test Server' -> 'lg-test-server'. A name with no Latin letters or digits at all ('Сервер') gets
    'server-' and a short hash of the name, the same every time, instead of nothing."""
    return text_slug(name) or "server-" + hashlib.sha1(name.strip().lower().encode("utf-8", "replace")).hexdigest()[:8]


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
    plays_kind: str = ""  # playlist, map, variant, campaign, odst or default (games/plays.py)
    errors: list[str] = field(default_factory=list)  # shown in red; a server with errors is never started
    auto_update: bool = False  # dedicated.toml auto_update: the server program installs its own new releases
    content_dir: Path | None = None  # its content folder (maps, game types, content/Mods/Workshop/<item>)
    workshop_items: list[int] = field(default_factory=list)  # [workshop] items
    workshop_check_minutes: int | None = None  # [workshop] update_check_minutes; None: the program's 60
    workshop_updates: str | None = None  # settings.toml's [[server]] workshop_updates; None: [workshop_updates]
    priority: str | None = None  # settings.toml's [[server]] priority; None: [process] priority
    automod: str | None = None  # settings.toml's [[server]] automod (on, dry_run, off); None: [automod] dry_run
    # settings.toml's [[server]] rcon_host / rcon_port: where Huragok reaches the console from (Docker, another PC),
    # which wins over the address the server writes in its instance.json (audit 2026-11, 4.5)
    rcon_host_set: bool = False
    rcon_port_set: bool = False

    @property
    def host_key_path(self) -> Path:
        return self.data_dir / "rcon-host.key"

    @property
    def status_path(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "server.log"

    def instance_current(self) -> bool:
        """data/<server>/instance.json is the running server's: 0.9 writes it, with rcon-host.key, as it starts.
        0.10 writes no instance.json, so on a server updated from 0.9 it is what 0.9 last wrote: older than the key
        0.10 rewrites at every start, and its RCON password, port and address may be stale (a password changed in
        dedicated.toml since would be refused again and again, and five refusals lock this machine out)."""
        try:
            written = (self.data_dir / "instance.json").stat().st_mtime
        except OSError:
            return False
        try:
            key = self.host_key_path.stat().st_mtime
        except OSError:
            return True  # no key to tell by (a 0.9 server before 0.9.15's key): as before
        return written >= key - INSTANCE_SLACK

    def rcon_endpoint(self) -> RconEndpoint | None:
        """Where and how to sign in, read fresh: a restart may have changed it. On 0.9 the server's instance.json
        says (it is what the server runs with); on 0.10, and with a leftover 0.9 instance.json, dedicated.toml."""
        host, port, password = self.rcon_host, self.rcon_port, self.rcon_password
        try:
            if not self.instance_current():
                raise OSError("no instance.json written at this start")
            rcon = json.loads((self.data_dir / "instance.json").read_text(encoding="utf-8")).get("rcon") or {}
            host = host if self.rcon_host_set else rcon.get("address") or host
            port = port if self.rcon_port_set else int(rcon.get("port") or port)
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


def _player_ids(values: list[str], where: str) -> set[str]:
    ids = {v.lower() for v in values}
    if bad := sorted(i for i in ids if not STORED_ID.fullmatch(i)):
        raise ConfigError(f"{where}: {bad[0]} is not a player ID (64 hexadecimal characters)")
    return ids


def _instances(data_dir: Path) -> dict[str, Path]:
    """Server name (lowercase) -> its data folder, from instance.json files. Two folders naming one server (a copy:
    Explorer's "slayer - Copy" sorts after "slayer" and took over, its stale log and status read for the real ones,
    audit 2026-11, 4.6): the one named as the server's own (its slug) wins, else the one written last."""
    found: dict[str, tuple[bool, float, Path]] = {}
    if data_dir.is_dir():
        for instance in data_dir.glob("*/instance.json"):
            try:
                name = json.loads(instance.read_text(encoding="utf-8")).get("name")
                written = instance.stat().st_mtime
            except (OSError, ValueError):
                continue
            if isinstance(name, str):
                rank = (instance.parent.name.lower() == slugify(name), written, instance.parent)
                if name.lower() not in found or rank[:2] > found[name.lower()][:2]:
                    found[name.lower()] = rank
    return {name: folder for name, (_, _, folder) in found.items()}


def read_dedicated(path: Path, overrides: dict[str, dict[str, Any]]) -> list[ServerConfig]:
    """Every enabled server in one dedicated.toml, with its RCON settings."""
    top = Table(_read_toml(path, ""), path.name)
    base = path.parent
    data_dir = base / top.get("data", str, "data")
    rcon = top.table("rcon")
    defaults = top.table("defaults")
    admins = _player_ids(top.strings("admins"), f"{path.name} admins")
    workshop = top.table("workshop")
    raw_items = workshop.data.get("items", [])
    items = [int(i) for i in raw_items if isinstance(i, int) and not isinstance(i, bool)] \
        if isinstance(raw_items, list) else []
    check_minutes = workshop.data.get("update_check_minutes")
    content_dir = (base / top.get("content", str, "content")).resolve()
    auto_update = top.get("auto_update", bool, False)
    instances = _instances(data_dir)

    blocks = top.data.get("server", [])
    if not isinstance(blocks, list) or not blocks:
        raise ConfigError(f"{path} has no [[server]] blocks")
    servers = []
    for number, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            raise ConfigError(f"{path.name}: server {number} isn't a [[server]] block (two brackets on each side, "
                              "then name = \"...\" under it)")
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
        workshop_updates = extra.get("workshop_updates", str, None)  # checked as settings.toml is read
        priority = extra.get("priority", str, None)  # so is this
        automod = extra.get("automod", str, None)  # and this
        plays = plays_of(block)
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
            plays_kind=plays_kind(block),
            auto_update=auto_update,
            content_dir=content_dir,
            workshop_items=items,
            workshop_check_minutes=check_minutes if isinstance(check_minutes, int) and
            not isinstance(check_minutes, bool) else None,
            workshop_updates=workshop_updates,
            priority=priority,
            automod=automod,
            rcon_host_set="rcon_host" in extra.data,
            rcon_port_set="rcon_port" in extra.data,
        )
        if not server.rcon_password:
            server.errors.append(f"RCON is off in {path.name}: set [rcon] password. The Superintendent needs it to "
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
                 only: list[str], problems: list[FolderProblem] | None = None,
                 known: set[str] | None = None) -> list[ServerConfig]:
    """The servers in these folders, checked against each other: names, slugs and ports must be unique.

    With `problems`, a folder whose dedicated.toml can't be read, or a server named
    like one found before it, is left out and noted there, so one hand edit gone
    wrong never keeps the other servers from running. Without it, they raise.
    Two names with one slug ("LG Slayer", "LG-Slayer") would share the per-server
    files (records/servers/<slug>.json, logs/console-<slug>.log...), each one's
    console lines driving the other's restarts: the later one is left out, or the
    new one when `known` (lowercase names already running) has the other.
    """
    def left_out(folder: Path | None, text: str, outcome: str = "") -> None:
        if problems is None:
            raise ConfigError(text)
        problems.append(FolderProblem(folder, text + outcome))

    servers: list[ServerConfig] = []
    for folder in folders:
        try:
            servers.extend(read_dedicated(folder / config_name, overrides))
        except (ConfigError, OSError, ValueError, TypeError, AttributeError) as error:  # one folder, not every server
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
    slugs: dict[str, ServerConfig] = {}
    unique = []
    for server in servers:
        other = slugs.get(server.slug)
        if other is None:
            slugs[server.slug] = server
            unique.append(server)
            continue
        keep, drop = other, server
        if known and server.name.lower() in known and other.name.lower() not in known:
            keep, drop = server, other  # the one already running keeps its files
        slug = server.slug
        left_out(drop.folder, f"'{other.name}' (in {other.folder}) and '{server.name}' (in {server.folder}) would "
                 f"share their records and logs: Huragok names both {slug} (records/servers/{slug}.json, "
                 f"logs/console-{slug}.log). Rename one of them",
                 f"; '{drop.name}' is left out until then")
        if keep is server:
            unique[unique.index(other)] = slugs[slug] = server
    servers = unique
    ports: dict[int, ServerConfig] = {}
    for server in servers:
        for port in {p for p in (server.game_port, server.rcon_port) if p}:
            other = ports.get(port)
            if other is not None and other is not server:
                server.errors.append(f"port {port} is also used by {other.name}")
            ports.setdefault(port, server)
    return servers


def discover_servers(settings: Settings, problems: list[FolderProblem] | None = None) -> list[ServerConfig]:
    """The servers in the server folders right now, for picking up folders added while running."""
    folders = find_server_folders(settings.home, settings.servers_folder, settings.single_folder,
                                  settings.config_name)
    return read_servers(folders, settings.config_name, settings.server_overrides, settings.only, problems,
                        known={s.name.lower() for s in settings.servers})


def slug_taken(settings: Settings, name: str, *, ignore: str | None = None) -> ServerConfig | None:
    """The server whose per-server files a server called `name` would share (same slug), if any: for refusing a
    new server's name or a rename. `ignore` is the server being renamed."""
    slug = slugify(name)
    for server in settings.servers:
        if ignore is not None and server.name.lower() == ignore.lower():
            continue
        if server.slug == slug:
            return server
    return None
