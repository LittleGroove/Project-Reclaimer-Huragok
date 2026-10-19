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

import difflib
import hashlib
import json
import os
import re
import string
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .livesettings import plays_kind, plays_of
from .permissions import PLAYER_ID, Permissions, PermissionsError, load_permissions

SETTINGS_FILE = "settings.toml"
COMMANDS_FILE = "commands.toml"
SERVERS_FOLDER = "SERVERS"
BACKENDS = ("attach", "process", "docker", "command")
BACKEND_ALIASES = {"windows": "process"}  # 0.1 called the process backend `windows`
CORE_TABLES = {"rcon", "keepalive", "updates", "restarts", "process", "windows", "docker", "command", "server",
               "setup", "relay", "server_updates", "workshop_updates"}
WORKSHOP_INSTALLS = ("after_game", "when_empty", "off")
WORKSHOP_MIN_MINUTES = 5  # the server program never looks more often than this
RELAY_ROLES = ("moderator", "admin", "owner")
PROGRAM_RELEASES = "ProjectReclaimer/project-reclaimer-releases"
IGNORED_PREFIXES = (".", "_")  # SERVERS/.something and SERVERS/_something are never servers (set aside, being made)
PART_SWITCHES = {"enabled", "dry_run", "handler_timeout"}
SERVER_KEYS = {"name", "autostart", "daily_restart", "service", "data", "rcon_host", "rcon_port", "warn_seconds",
               "min_uptime_seconds", "workshop_updates"}  # what a settings.toml [[server]] block can set
PROCESS_KEYS = {"executable", "command", "stop_timeout", "stop_servers_on_exit"}
# Every setting of settings.toml, by section ("" is above the first [section]). A key that isn't one does nothing,
# and the host who typed `[relay] enable = true` or appended `daily_restart = "04:00"` at the end (where it lands in
# the last section) thinks it does: `check` and the start name each one. The parts' sections come from the parts.
KNOWN_KEYS: dict[str, set[str]] = {
    "": {"servers_folder", "server_folder", "dedicated_config", "backend", "servers", "name", "dry_run", "database",
         "log_file"},
    "restarts": {"warn_seconds", "after_game_seconds", "daily_restart", "min_uptime_seconds", "backoff_seconds",
                 "faulted_retry_minutes",
                 "good_run_seconds", "max_failures", "restarts_per_hour", "ready_seconds", "empty_seconds"},
    "keepalive": {"hang_seconds", "check_seconds", "act"},
    "rcon": {"use_host_key", "command_timeout"},
    "updates": {"check", "check_hours", "auto_update", "install_on_server_restart", "repository"},
    "server_updates": {"check", "check_hours", "install", "repository", "follow", "fallback"},
    "workshop_updates": {"install", "check_minutes"},
    "setup": {"mcc_folder", "share", "packages_folder", "program", "auto_update", "steamcmd", "steam_username",
              "download_game_files"},
    "relay": {"enabled", "address", "port_offset", "source_port_offset", "user"},
    "relay.user": {"name", "password", "role"},
    "process": PROCESS_KEYS,
    "windows": PROCESS_KEYS,  # 0.1's name for [process]
    "docker": {"compose_file", "start", "stop", "restart", "timeout"},
    "command": {"start", "stop", "restart", "timeout"},
}
COMMAND_ACTIONS = ("start", "stop", "restart")
# What Huragok fills in: in [process] command, and in [command]/[docker] start, stop and restart (where the section's
# own other settings, such as compose_file, can be used too).
PROCESS_PLACEHOLDERS = ("executable", "config", "folder", "name")
COMMAND_PLACEHOLDERS = ("name", "slug", "service", "port", "folder", "config")
# The least that makes sense for the timing settings; less turns a check into a busy loop, restarts healthy servers
# (status.json is rewritten every 5 s), or makes every start or console command fail at once.
MINIMUMS = {
    ("keepalive", "check_seconds"): 1.0,
    ("keepalive", "hang_seconds"): 30.0,
    ("rcon", "command_timeout"): 1.0,
    ("restarts", "ready_seconds"): 30.0,
    ("restarts", "after_game_seconds"): 0.0,
    ("restarts", "min_uptime_seconds"): 0.0,
    ("restarts", "good_run_seconds"): 0.0,
    ("restarts", "empty_seconds"): 0.0,
}
MIN_HANDLER_TIMEOUT = 1.0
MIN_STOP_TIMEOUT = 1.0
# Settings that once did something and now do nothing: (section, key) -> what to do. A host's settings.toml outlives
# the version that wrote it, so `check` and the start of Huragok name each one still there.
RETIRED = {
    ("process", "disable_auto_update"): "each server's own auto_update (in its dedicated.toml, D in the window) is the "
                                        "only switch for Project Reclaimer updates now: delete the line",
}
ANY_VALUE = object()
# Values earlier versions wrote into settings.toml whose meaning or default has changed since: (section, key, the
# value as written, what it means now). They're legal choices, so they're named as notes, never as problems.
OLD_VALUES: list[tuple[str, str, Any, str]] = [
    ("watchdog", "mode", "watch",
     "is what 0.1 and 0.2.0 wrote: the watchdog only logs and restarts nothing. Since 0.2.1 the default is \"act\" "
     "(it restarts a dead or stuck server). Set mode = \"act\" if you want those restarts"),
    ("watchdog", "slow_frames", ANY_VALUE,
     "is 0.2.1's one switch for both frame cases. It's still read while slow and very_slow aren't set (\"act\" makes "
     "slow \"act\" and very_slow \"actnow\"), but those two are the settings now: replace the line with them"),
    ("setup", "share", ["game", "content", "bans"],
     "is what 0.2.0 and 0.2.1 wrote: new servers share one content/ folder. Since 0.4.0 the default is "
     "[\"game\", \"bans\"]: each new server keeps its own content, so its Workshop mods are updated after its own "
     "games. Take \"content\" out unless you want the shared folder"),
    ("server_updates", "check_hours", 6,
     "is what 0.2.0 and 0.2.1 wrote. Since 0.4.0 the default is 1, as often as the servers' own updaters look (the "
     "fallback for a server that didn't update itself counts on hourly looks). Set check_hours = 1"),
    ("updates", "install_on_server_restart", ANY_VALUE,
     "is auto_update's name before 0.4.2, and is read as auto_update while that isn't set: true installs a new "
     "version as soon as Huragok finds it, rather than at the next server restart (which a server that keeps "
     "failing to start never reaches). Huragok renames the line at its next start"),
    ("updates", "repository", "LittleGroove/Project-Reclaimer-Server-Supervisor",
     "is the old name of Huragok's repository; Huragok uses LittleGroove/Project-Reclaimer-Huragok in its place. "
     "Change the line to that"),
]
CLOCK_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
KIND_NAMES = {bool: "true or false", str: "text in quotes", int: "a whole number", float: "a number",
              list: "a list", dict: "a table"}
BACKSLASH_HINT = ("Windows paths: write them in 'single quotes' (like 'C:\\steamcmd\\steamcmd.exe') or with "
                  "forward slashes (C:/steamcmd/steamcmd.exe); inside \"double quotes\" a \\ starts an escape")


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
    """The name of the server's own files (its data folder, records/servers/<slug>.json, logs/console-<slug>.log):
    'LG Test Server' -> 'lg-test-server'. A name with no Latin letters or digits at all ('Сервер') gets
    'server-' and a short hash of the name, the same every time, instead of nothing."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if slug:
        return slug
    return "server-" + hashlib.sha1(name.strip().lower().encode("utf-8", "replace")).hexdigest()[:8]


def _shown(value: Any) -> str:
    """A value as it reads in settings.toml: "no", 300, true."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "a table"
    return str(value)


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
        if value is not None and (not isinstance(value, kind) or (kind is int and isinstance(value, bool))):
            raise ConfigError(f"{self._name(key)} must be {KIND_NAMES.get(kind, kind.__name__)}, not {_shown(value)}")
        return value

    def at_least(self, key: str, default: float, minimum: float) -> float:
        """A number of seconds (or the like) that makes no sense below `minimum`."""
        value = self.get(key, float, default)
        if value < minimum:
            raise ConfigError(f"{self._name(key)} must be at least {minimum:g}, not {value:g}")
        return value

    def path(self, key: str, default: str | None) -> str | None:
        """A file or folder name. A control character in one is a Windows path typed into "double quotes", where
        \\n, \\t or \\b silently became a line break, a tab or a backspace."""
        value = self.get(key, str, default)
        if value and (bad := next((c for c in value if ord(c) < 0x20 or ord(c) == 0x7F), None)):
            raise ConfigError(f"{self._name(key)} has a control character in it ({json.dumps(bad)[1:-1]}), so the "
                              f"path isn't what you typed. {BACKSLASH_HINT}")
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
    plays_kind: str = ""  # playlist, map, variant, campaign, odst or default (livesettings.plays_kind)
    errors: list[str] = field(default_factory=list)  # shown in red; a server with errors is never started
    auto_update: bool = False  # dedicated.toml auto_update: the server program installs its own new releases
    content_dir: Path | None = None  # its content folder (maps, game types, content/Mods/Workshop/<item>)
    workshop_items: list[int] = field(default_factory=list)  # [workshop] items
    workshop_check_minutes: int | None = None  # [workshop] update_check_minutes; None: the program's 60
    workshop_updates: str | None = None  # settings.toml's [[server]] workshop_updates; None: [workshop_updates]

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
    """[updates]: new versions of Huragok, from GitHub."""

    check: bool = True
    check_hours: float = 6.0
    auto_update: bool = False  # true: a new version installs itself once found; false: the host says when (U)
    repository: str = "LittleGroove/Project-Reclaimer-Huragok"


RENAMED_REPOSITORIES = {  # a settings.toml written before the rename names the old address
    "LittleGroove/Project-Reclaimer-Server-Supervisor": UpdateSettings.repository,
}


@dataclass
class SetupSettings:
    """[setup]: making new servers (the window's N key, `new` from the command line)."""

    mcc_folder: str | None = None  # Halo 3's files from here; None: not set ("" would be Steam's default folder)
    share: list[str] = field(default_factory=lambda: ["game", "bans"])  # what new servers take from the root
    packages_folder: str = "PACKAGES"
    program: str = ""  # the server program for making and checking folders, when it isn't in a server folder or at the root
    auto_update: bool = True  # new servers install new Project Reclaimer releases themselves (dedicated.toml auto_update)
    steamcmd: str = ""  # SteamCMD, for Workshop items and Halo 3's files; empty: looked for in the usual places
    steam_username: str = ""  # the Steam account that owns MCC, signed in to SteamCMD once (never a password here)
    download_game_files: bool = True  # a server missing Halo 3's files may download them with SteamCMD by itself


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
    check_hours: float = 1.0  # as often as the program's own auto_update looks
    install: str = "ask"  # ask (only say so), after_game, now: for servers with auto_update off
    repository: str = PROGRAM_RELEASES
    follow: bool = True  # one server updated itself: restart the other self-updating ones after their game
    fallback: bool = True  # a self-updating server whose own update didn't happen gets it from Huragok after its game


@dataclass
class WorkshopUpdateSettings:
    """[workshop_updates]: new versions of the servers' Workshop mods, installed when a game ends."""

    install: str = "after_game"  # after_game, when_empty (the server's own way), off
    check_minutes: int = 5  # Huragok's look at Steam, and each server's update_check_minutes; 0: leave both alone


@dataclass
class KeepaliveSettings:
    hang_seconds: float = 180.0
    check_seconds: float = 10.0
    act: bool = True


@dataclass
class RestartSettings:
    """[restarts]: warnings, crashes, the daily restart. warn_seconds, daily_restart and min_uptime_seconds can be
    set again per server, in its settings.toml [[server]] block (SERVER_KEYS)."""

    warn_seconds: list[float] = field(default_factory=lambda: [300.0, 180.0, 60.0, 30.0])
    after_game_seconds: float = 15.0  # notice once a game has ended, for a restart "after this game"
    daily_restart: str = ""  # local "HH:MM"; empty: none
    min_uptime_seconds: float = 900.0  # a daily restart waits until the server has been up this long
    backoff_seconds: list[float] = field(default_factory=lambda: [5.0, 10.0, 30.0, 60.0, 300.0])
    good_run_seconds: float = 600.0  # dying sooner than this after a start counts as a failed start
    max_failures: int = 5  # failed starts in a row before FAULTED
    restarts_per_hour: int = 6  # more automatic restarts than this in an hour: FAULTED
    # FAULTED is a pause, not the end: one more try after each of these many minutes in turn (the last repeats),
    # until a run lasts good_run_seconds. Empty: it waits for a person (S), as before 0.4.0.
    faulted_retry_minutes: list[float] = field(default_factory=lambda: [30.0, 60.0])
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
    name: str = "Superintendent"
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
    workshop_updates: WorkshopUpdateSettings = field(default_factory=WorkshopUpdateSettings)
    parts: dict[str, PartSettings] = field(default_factory=dict)  # every part, on or off
    problems: list[FolderProblem] = field(default_factory=list)  # server folders left out, found when loading
    # settings.toml lines that do nothing: RETIRED settings, and keys that aren't settings (misspelt, or under the
    # wrong [section]). Shown by `check`, logged at the start and listed in the window's problems.
    retired: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # OLD_VALUES found: legal, but not what they may seem

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
        """A server by its name (any case); else by its slug (lg-test-server); else by its number in the list (2,
        or #2), as the window numbers them. A server really named "2" always wins over the second server."""
        wanted = name.strip()
        for server in self.servers:
            if server.name.lower() == wanted.lower():
                return server
        for server in self.servers:
            if server.slug == wanted.lower():
                return server
        number = wanted.removeprefix("#")
        if number.isascii() and number.isdigit() and 1 <= int(number) <= len(self.servers):
            return self.servers[int(number) - 1]
        raise KeyError(name)

    def workshop_install(self, server: ServerConfig) -> str:
        """How this server's Workshop updates are installed: its own [[server]] workshop_updates, or the default."""
        return server.workshop_updates or self.workshop_updates.install

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
        text = str(error)
        hint = f". {BACKSLASH_HINT}" if "Unescaped '\\'" in text or "hex value" in text.lower() else ""
        raise ConfigError(f"{path}: {text}{hint}") from None


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


def load(path: str | os.PathLike[str]) -> Settings:
    path = Path(path).resolve()
    top = Table(_read_toml(path, ": start Huragok once to make it"))
    base = path.parent

    servers_folder_name = top.path("servers_folder", SERVERS_FOLDER)
    servers_folder = (base / servers_folder_name).resolve() if servers_folder_name else None
    single_name = top.path("server_folder", "")
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
    check_backend_options(backend, backend_options)

    overrides = _server_overrides(top)
    only = top.strings("servers")
    folders = find_server_folders(base, servers_folder, single_folder, config_name)
    problems: list[FolderProblem] = []
    servers = read_servers(folders, config_name, overrides, only, problems)
    known = {s.name.lower() for s in servers}
    chosen = {n.lower() for n in only}
    if folders:  # with no folders at all, the first start's hints are the useful message
        # A folder taken away (or left out above) must not keep the rest from starting. A block for a server that
        # `servers = [...]` leaves out on purpose is fine as it is.
        for name in [overrides[n]["name"] for n in overrides if n not in known and (not chosen or n in chosen)]:
            problems.append(FolderProblem(None, f"[[server]] '{name}' in {path.name} is not an enabled server in any "
                                                "server folder, so its settings there do nothing"))

    rcon = top.table("rcon")
    keep = top.table("keepalive")
    updates = top.table("updates")
    restarts = top.table("restarts")
    setup = top.table("setup")
    share = [s.lower() for s in setup.strings("share")] if "share" in setup.data else ["game", "bans"]
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
    workshop_updates = top.table("workshop_updates")
    workshop_install = workshop_updates.get("install", str, "after_game").strip().lower()
    if workshop_install not in WORKSHOP_INSTALLS:
        raise ConfigError(f"workshop_updates.install: must be one of {', '.join(WORKSHOP_INSTALLS)}")
    workshop_minutes = workshop_updates.get("check_minutes", int, 5)
    if workshop_minutes and not WORKSHOP_MIN_MINUTES <= workshop_minutes <= 10080:
        raise ConfigError(f"workshop_updates.check_minutes: {WORKSHOP_MIN_MINUTES} to 10080 (the server program "
                          "never looks more often than every 5 minutes), or 0 to leave each server's own")
    program_repository = program_updates.get("repository", str, PROGRAM_RELEASES)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", program_repository):
        raise ConfigError(f"server_updates.repository: '{program_repository}' should look like owner/name")
    repository = updates.get("repository", str, UpdateSettings.repository)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ConfigError(f"updates.repository: '{repository}' should look like owner/name, as in its GitHub address")
    repository = RENAMED_REPOSITORIES.get(repository, repository)
    if moved := sorted(k for k in ("roles", "commands") if k in top.data):
        raise ConfigError(f"[{moved[0]}]: who's who and who may use which command are set in {COMMANDS_FILE}")
    parts = _parts(top)
    try:
        permissions = load_permissions(base / COMMANDS_FILE)
    except PermissionsError as error:
        raise ConfigError(str(error)) from None

    def optional_path(key: str, default: str) -> Path | None:
        value = top.path(key, default)
        return base / value if value else None

    def seconds(table: Table, key: str, default: float) -> float:
        """A timing setting, held to its MINIMUMS."""
        minimum = MINIMUMS.get((table.where, key))
        return table.get(key, float, default) if minimum is None else table.at_least(key, default, minimum)

    def retry_minutes(table: Table) -> list[float]:
        """[restarts] faulted_retry_minutes: whole minutes, 1 or more each (a retry every few seconds is the very
        thrashing FAULTED stops); an empty list switches the retries off."""
        value = table.data.get("faulted_retry_minutes", [30, 60])
        if not isinstance(value, list) or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1
                                                  for v in value):
            raise ConfigError("restarts.faulted_retry_minutes must be a list of minutes, each 1 or more, like "
                              "[30, 60] (the last repeats; [] waits for a person)")
        return [float(v) for v in value]

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
        name=top.get("name", str, "Superintendent"),
        database=optional_path("database", "records/huragok.sqlite3"),
        log_file=optional_path("log_file", "logs/huragok.log"),
        dry_run=top.get("dry_run", bool, False),
        use_host_key=rcon.get("use_host_key", bool, True),
        command_timeout=seconds(rcon, "command_timeout", 10.0),
        keepalive=KeepaliveSettings(
            hang_seconds=seconds(keep, "hang_seconds", 180.0),
            check_seconds=seconds(keep, "check_seconds", 10.0),
            act=keep.get("act", bool, True),
        ),
        updates=UpdateSettings(
            check=updates.get("check", bool, True),
            check_hours=max(1.0, updates.get("check_hours", float, 6.0)),
            # 0.4.0 and 0.4.1 called it install_on_server_restart: read as auto_update until the start renames it
            auto_update=updates.get("auto_update", bool, updates.get("install_on_server_restart", bool, False)),
            repository=repository,
        ),
        restarts=RestartSettings(
            warn_seconds=restarts.numbers("warn_seconds", [300, 180, 60, 30]),
            after_game_seconds=seconds(restarts, "after_game_seconds", 15.0),
            daily_restart=check_clock_time(restarts.get("daily_restart", str, ""), "restarts.daily_restart"),
            min_uptime_seconds=seconds(restarts, "min_uptime_seconds", 900.0),
            backoff_seconds=restarts.numbers("backoff_seconds", [5, 10, 30, 60, 300]) or [5.0],
            good_run_seconds=seconds(restarts, "good_run_seconds", 600.0),
            max_failures=max_failures,
            restarts_per_hour=per_hour,
            faulted_retry_minutes=retry_minutes(restarts),
            ready_seconds=seconds(restarts, "ready_seconds", 300.0),
            empty_seconds=seconds(restarts, "empty_seconds", 10.0),
        ),
        setup=SetupSettings(
            mcc_folder=setup.path("mcc_folder", None),
            share=share,
            packages_folder=setup.path("packages_folder", "PACKAGES") or "PACKAGES",
            program=setup.path("program", "") or "",
            steamcmd=setup.path("steamcmd", "") or "",
            steam_username=setup.get("steam_username", str, ""),
            auto_update=setup.get("auto_update", bool, True),
            download_game_files=setup.get("download_game_files", bool, True),
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
            check_hours=max(1.0, program_updates.get("check_hours", float, 1.0)),
            install=install,
            repository=program_repository,
            follow=program_updates.get("follow", bool, True),
            fallback=program_updates.get("fallback", bool, True),
        ),
        workshop_updates=WorkshopUpdateSettings(install=workshop_install, check_minutes=workshop_minutes),
        parts=parts,
        problems=problems,
        retired=retired_settings(top) + unknown_settings(top),
        notes=old_values(top),
    )


def retired_settings(top: Table) -> list[str]:
    """The RETIRED settings still in settings.toml, each with what to do about it, named under the section they're
    in (0.1 wrote [windows] for what is [process] now)."""
    found = []
    for (section, key), advice in RETIRED.items():
        tables = [section, *(old for old, new in BACKEND_ALIASES.items() if new == section)]
        for name in tables:
            if key in top.table(name).data:
                found.append(f"{SETTINGS_FILE} [{name}] {key} does nothing any more: {advice}")
    return found


def _known_keys() -> dict[str, set[str]]:
    """KNOWN_KEYS with the parts' sections: their switches and the settings their defaults name."""
    from .parts import PARTS
    from .parts.watchdog import OLD_NAMES

    known = dict(KNOWN_KEYS)
    for name, cls in PARTS.items():
        if not cls.always_on:
            known[name] = PART_SWITCHES | set(cls.defaults) | (OLD_NAMES if name == "watchdog" else set())
    return known


def unknown_settings(top: Table) -> list[str]:
    """Keys of settings.toml that aren't settings, each with the setting it was probably meant to be: a close
    spelling in the same section, or the same key in the section it belongs to."""
    known = _known_keys()
    retired = {(section, key) for section, key in RETIRED} | {
        (old, key) for section, key in RETIRED for old, new in BACKEND_ALIASES.items() if new == section}

    def where(section: str) -> str:
        return f"[[{section}]]" if section == "relay.user" else f"[{section}]" if section else \
            "(above the first [section])"

    def place(section: str) -> str:
        return f"under {where(section)}" if section else "at the top, above the first [section]"

    def advice(section: str, key: str) -> str:
        homes = [s for s, keys in known.items() if key in keys and s not in (section, "windows")]
        if homes:
            places = [place(h) for h in homes[:3]]
            text = places[0] if len(places) == 1 else ", ".join(places[:-1]) + " or " + places[-1]
            return f": it belongs {text}" + (" (a line belongs to the [section] heading above it)" if section else "")
        close = difflib.get_close_matches(key, sorted(known.get(section, set())), n=1, cutoff=0.6)
        return f" (did you mean {close[0]}?)" if close else ""

    found = []

    def look(section: str, data: dict[str, Any], skip: set[str] | None = None) -> None:
        for key in data:
            if key in known.get(section, set()) or key in (skip or ()) or (section, key) in retired:
                continue
            named = f"{where(section)} {key}" if section else f"{key} {where(section)}"
            found.append(f"{SETTINGS_FILE} {named} isn't a setting, so it does nothing{advice(section, key)}")

    look("", {k: v for k, v in top.data.items() if not isinstance(v, dict) and k != "server"})
    for section in known:
        data = top.data.get(section)
        if section in ("", "relay.user") or not isinstance(data, dict):
            continue
        skip: set[str] = set()
        if section in ("command", "docker"):  # its other settings can be filled into its commands
            skip = {k for k in data if any(k in _fields(str(data.get(a) or "")) for a in COMMAND_ACTIONS)}
        look(section, data, skip)
    for raw in top.table("relay").data.get("user", []) if isinstance(top.data.get("relay"), dict) else []:
        if isinstance(raw, dict):
            look("relay.user", raw)
    return found


def old_values(top: Table) -> list[str]:
    """OLD_VALUES found in settings.toml, each with what it means now."""
    found = []
    for section, key, value, meaning in OLD_VALUES:
        data = top.data.get(section)
        if not isinstance(data, dict) or key not in data:
            continue
        written = data[key]
        if value is ANY_VALUE or written == value:
            shown = _shown(written) if not isinstance(written, list) else \
                "[" + ", ".join(_shown(v) for v in written) + "]"
            found.append(f"{SETTINGS_FILE} [{section}] {key} = {shown} {meaning}")
    return found


def _fields(template: str) -> list[str]:
    """The {placeholders} in a command; ValueError for a brace that isn't one."""
    names = []
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None:
            names.append(re.split(r"[.\[]", field_name, maxsplit=1)[0])
    return names


def _check_placeholders(template: str, allowed: set[str], where: str) -> None:
    try:
        names = _fields(template)
    except ValueError as error:
        raise ConfigError(f"{where}: {error}; write {{{{ and }}}} for a brace itself") from None
    for name in names:
        if name not in allowed:
            shown = ", ".join("{" + a + "}" for a in sorted(allowed))
            raise ConfigError(f"{where}: {{{name}}} isn't something Huragok fills in. It fills in {shown} "
                              "(write {{ and }} for a brace itself)")


def check_backend_options(backend: str, options: dict[str, Any]) -> None:
    """The backend's section, checked as settings.toml is read: a wrong type there would otherwise stop
    `check`, `stop` and the start with a traceback, and a placeholder Huragok doesn't fill in would fail every
    server start."""
    if backend == "process":
        table = Table(options, "process")
        table.path("executable", "")
        command = options.get("command", [])
        if not isinstance(command, list) or not all(isinstance(word, str) for word in command):
            raise ConfigError('process.command must be a list of words in quotes, like ["wine", "{executable}", '
                              '"dedicated", "run", "--config", "{config}"], or [] for the usual way')
        for word in command:
            _check_placeholders(word, set(PROCESS_PLACEHOLDERS), "[process] command")
        table.at_least("stop_timeout", 30.0, MIN_STOP_TIMEOUT)
        table.get("stop_servers_on_exit", bool, False)
    elif backend in ("command", "docker"):
        table = Table(options, backend)
        for action in COMMAND_ACTIONS:
            table.get(action, str, "")
        table.at_least("timeout", 120.0, MIN_STOP_TIMEOUT)
        if backend == "docker":
            table.path("compose_file", "compose.yaml")
        extra = {k for k, v in options.items() if k not in COMMAND_ACTIONS and isinstance(v, (str, int, float))
                 and not isinstance(v, bool)}
        allowed = set(COMMAND_PLACEHOLDERS) | extra | ({"compose_file"} if backend == "docker" else set())
        for action in COMMAND_ACTIONS:
            if options.get(action):
                _check_placeholders(options[action], allowed, f"[{backend}] {action}")


def _server_overrides(top: Table) -> dict[str, dict[str, Any]]:
    """settings.toml's [[server]] blocks, by lowercase name, each value checked here so a mistake names
    settings.toml (and not the server's dedicated.toml, which only seems to leave the server out)."""
    raw = top.data.get("server", [])
    if not isinstance(raw, list) or not all(isinstance(block, dict) for block in raw):
        raise ConfigError(f"{SETTINGS_FILE}: settings for one server go in a [[server]] block (two brackets on "
                          "each side), one block per server, starting with name = \"<its name in dedicated.toml>\"")
    overrides: dict[str, dict[str, Any]] = {}
    for number, block in enumerate(raw, 1):
        name = Table(block, f"{SETTINGS_FILE} [[server]] {number}").get("name", str, "")
        if not name:
            raise ConfigError(f"{SETTINGS_FILE} [[server]] {number}: name the server as its dedicated.toml does")
        if stray := sorted(set(block) - SERVER_KEYS):
            close = difflib.get_close_matches(stray[0], sorted(SERVER_KEYS), n=1, cutoff=0.6)
            raise ConfigError(f"[[server]] {name}: '{stray[0]}' isn't a setting a server can have here"
                              + (f" (did you mean {close[0]}?)" if close else ""))
        entry = Table(block, f"{SETTINGS_FILE} [[server]] {name}")
        override = dict(block)
        for key, kind in (("autostart", bool), ("daily_restart", str), ("service", str), ("rcon_host", str),
                          ("rcon_port", int), ("workshop_updates", str)):
            entry.get(key, kind, None)
        entry.path("data", None)
        if "daily_restart" in block:  # "5:00" read as "05:00", as [restarts] daily_restart is
            override["daily_restart"] = check_clock_time(block["daily_restart"],
                                                         f"{SETTINGS_FILE} [[server]] {name} daily_restart")
        if block.get("workshop_updates", "after_game") not in WORKSHOP_INSTALLS:
            raise ConfigError(f"[[server]] {name} workshop_updates: must be one of {', '.join(WORKSHOP_INSTALLS)}")
        if "rcon_port" in block and not 1 <= block["rcon_port"] <= 65535:
            raise ConfigError(f"{SETTINGS_FILE} [[server]] {name}.rcon_port must be a port number, 1 to 65535")
        if "warn_seconds" in block:  # as [restarts] reads them: the manager counts down with these
            override["warn_seconds"] = entry.numbers("warn_seconds", [])
        if "min_uptime_seconds" in block:
            override["min_uptime_seconds"] = entry.at_least("min_uptime_seconds", 0.0, 0.0)
        overrides[name.lower()] = override
    return overrides


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


def _parts(top: Table) -> dict[str, PartSettings]:
    """One section per part that has settings; a section that isn't one is a mistake worth naming."""
    from .parts import PARTS  # here, not at the top: the parts import this module

    sections = {k: v for k, v in top.data.items() if isinstance(v, dict) and k not in CORE_TABLES}
    optional = [name for name, cls in PARTS.items() if not cls.always_on]
    if unknown := sorted(set(sections) - set(optional)):
        close = difflib.get_close_matches(unknown[0], sorted((CORE_TABLES - {"windows", "server"}) | set(optional)),
                                          n=1, cutoff=0.6)
        raise ConfigError(f"[{unknown[0]}] isn't a section of {SETTINGS_FILE}"
                          + (f" (did you mean [{close[0]}]?)" if close else "")
                          + f"; the parts with settings are {', '.join(optional)}")
    result = {}
    for name, cls in PARTS.items():
        section = Table(sections.get(name, {}), name)
        result[name] = PartSettings(
            enabled=cls.always_on or section.get("enabled", bool, cls.enabled_by_default),
            dry_run=section.get("dry_run", bool, False),
            handler_timeout=section.at_least("handler_timeout", 10.0, MIN_HANDLER_TIMEOUT),
            options={k: v for k, v in section.data.items() if k not in PART_SWITCHES},
        )
    return result
