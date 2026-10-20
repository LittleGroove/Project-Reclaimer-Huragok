"""Everything a host can change about a server, in one catalogue: what each setting is, how it's typed,
its default, where it lives and when it applies (Project Reclaimer 0.9.20, auto_reload on).

The window's D screen shows this catalogue in groups (joining, what it plays, gameplay, voice and chat,
safety, mods, updates, listing and network, the remote console, folders, and Huragok's own per-server
settings) with each value and where it comes from, writes a change in place (dedicated.toml keeps its
comments; Huragok's own go into settings.toml), and the server re-reads its file a couple of seconds
later: most settings apply at once, some from the next game or vote, some by a restart once the server is
empty (the server program does that itself), and a few when `dedicated` restarts, which D offers to do
at a good moment. The status card (V) reads the same catalogue for what a server is set to.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .odst import describe as describe_odst
from .permissions import PLAYER_ID
from .priority import DEFAULT as DEFAULT_PRIORITY, PRIORITIES
from .tomledit import change_server_settings, read_toml, remove_server_setting, set_server_setting, set_table_key, \
    set_top_key

if TYPE_CHECKING:
    from .config import Settings

APPLIES = {
    "now": "at once; players stay connected",
    "next game": "from the next game",
    "next vote": "from the next playlist vote",
    "when empty": "the server restarts by itself once nobody is on",
    "restart": "when `dedicated` restarts (the Superintendent offers to do it after the game)",
    "huragok": "at once: Huragok's own setting, kept in settings.toml",
}

GROUPS: list[tuple[str, str, str]] = [  # (key, title, what's in it)
    ("joining", "Joining", "password, hidden, player limits, ping limit, welcome lines"),
    ("plays", "What it plays", "a playlist, one map and game type, a saved custom game, campaign co-op or "
                               "Halo 3: ODST"),
    ("gameplay", "Gameplay", "friendly fire, team changing, the camera, votes"),
    ("voice", "Voice and chat", "voice channels, text chat, chat log"),
    ("safety", "Safety and admins", "anti-cheat, the VPN block and its lists, admins"),
    ("mods", "Mods and the Workshop", "sharing mods, character mods, Workshop items, SteamCMD"),
    ("updates", "Updates", "Project Reclaimer's own updates, Workshop update checks and installs"),
    ("network", "Listing and network", "names in the browser, port, address, UPnP, the discovery master, stats"),
    ("rcon", "Remote console", "the console's password and address, this server's own"),
    ("files", "Folders and files", "game files, content, data, the ban list, live reload"),
    ("huragok", "Huragok's own", "autostart, the daily restart (in settings.toml)"),
]
GROUP_TITLES = {key: title for key, title, _ in GROUPS}

WHERE = {"server": "this server", "folder": "every server in its folder", "rcon": "[rcon]", "master": "[master]",
         "workshop": "[workshop]", "vpn": "[vpn]", "huragok": "settings.toml", "plays": "this server"}
PLAYS_KEYS = ("playlist", "map", "game", "base_map", "variant", "campaign", "odst")  # one kind of these
CO_OP_MAX_PLAYERS = 16  # a campaign or ODST server holds no more: a higher max_players stops its start


@dataclass(frozen=True)
class Setting:
    key: str
    scope: str  # server ([[server]] block), folder (top of the file), rcon/master/workshop/vpn (that table),
    #             huragok (settings.toml's [[server]] block), plays (the special "what it plays" editor)
    kind: str  # bool, int, str, list, ids, lines (one is text, several a list), choice, path, time, urls, plays
    applies: str  # a key of APPLIES
    help: str
    group: str
    choices: tuple[str, ...] = ()
    low: int | None = None
    high: int | None = None
    default: Any = None  # the program's own value when nothing sets it (None: nothing, or depends)
    label: str = ""  # how the screen names it, when not the key
    since: str | None = None  # the first program version that has it (older ones refuse the whole file); None: all

    @property
    def where(self) -> str:
        return WHERE[self.scope]

    @property
    def name(self) -> str:
        if self.label:
            return self.label
        return self.key if self.scope in ("server", "folder", "huragok", "plays") else f"[{self.scope}] {self.key}"


def S(key: str, scope: str, kind: str, applies: str, help: str, group: str, **more: Any) -> Setting:  # noqa: N802
    return Setting(key, scope, kind, applies, help, group, **more)


SETTINGS: list[Setting] = [
    # Joining
    S("password", "server", "str", "now", "the join password; empty lets anyone join", "joining", default=""),
    S("hidden", "server", "bool", "when empty", "never listed; players join by address", "joining", default=False),
    S("max_players", "server", "int", "now", "players at once (campaign co-op and ODST at most 16)", "joining",
      low=1, high=127,
      default=16),
    S("min_players", "server", "int", "now", "wait in the lobby until this many join", "joining", low=1, high=127,
      default=1),
    S("max_ping", "server", "int", "now", "turn away players joining with a higher ping, ms; 0 takes everyone",
      "joining", low=0, high=1000, default=0),
    S("load_timeout", "server", "int", "now", "seconds a player may hold up the next game; 0 waits", "joining",
      low=0, high=600, default=30),
    S("welcome", "server", "lines", "now", "chat lines players read as they join (up to 5; {name}, {server})",
      "joining", default=""),
    S("enabled", "server", "bool", "when empty", "false stops this server once it's empty and keeps it off",
      "joining", default=True),
    # What it plays
    S("plays", "plays", "plays", "next vote", "a playlist (from the next vote), or one map and game type, a saved "
      "custom game, campaign co-op or Halo 3: ODST (switching between those restarts it once nobody is on)",
      "plays", label="what it plays"),
    # Gameplay
    S("friendly_fire", "server", "bool", "next game", "teammates can hurt each other (a campaign or ODST server "
      "restarts once nobody is on)", "gameplay", default=True),
    S("team_changing", "server", "choice", "next game", "how far players may change teams themselves", "gameplay",
      choices=("balanced", "game_type", "off"), default="balanced"),
    S("camera", "server", "choice", "next game", "how players see their own character on foot in multiplayer: "
      "first_person for everyone (third-person character mods too), player_choice (each switches with P) or "
      "third_person for everyone; campaign and ODST keep their own", "gameplay",
      choices=("first_person", "player_choice", "third_person"), default="first_person", since="0.9.20"),
    S("votes", "server", "bool", "now", "players may vote to kick, end the round or game, shuffle", "gameplay",
      default=True),
    S("trust_player_games", "server", "bool", "now", "campaign and ODST: when every player's game goes out of step, "
      "take one player's game instead of reloading all", "gameplay", default=True, since="0.9.18"),
    # Voice and chat
    S("voice", "server", "bool", "now", "voice chat at all", "voice", default=True),
    S("voice_global", "server", "bool", "now", "the V key: everyone hears", "voice", default=True),
    S("voice_team", "server", "bool", "now", "the B key: teammates hear", "voice", default=True),
    S("voice_proximity", "server", "bool", "now", "the N key: players nearby hear", "voice", default=True),
    S("text_chat", "server", "bool", "now", "text chat", "voice", default=True),
    S("chat_log", "server", "bool", "when empty", "keep the chat in data/<server>/chat", "voice", default=True),
    # Safety and admins
    S("anti_cheat", "server", "choice", "when empty", "reject impossible game data: enforce kicks, detect logs",
      "safety", choices=("enforce", "detect", "off"), default="enforce"),
    S("block_vpn", "server", "bool", "when empty", "refuse players on a VPN, proxy or hosting provider", "safety",
      default=True),
    S("lists", "vpn", "list", "restart", "the networks block_vpn refuses: https:// lists or files; \"default\" keeps "
      "the default lists beside your own", "safety", default=[]),
    S("admins", "folder", "list", "now", "player IDs who may kick and ban on every server here", "safety", default=[],
      label="admins (every server here)"),
    S("admins", "server", "list", "now", "extra player IDs who may kick and ban on this server only", "safety",
      default=[], label="admins (this server only)"),
    # Mods and the Workshop
    S("share_mods", "server", "bool", "now", "players without a mod download it from this server", "mods",
      default=False),
    S("share_rate", "server", "int", "now", "upload limit for shared mods, Mbit/s; 0 is none", "mods", low=0,
      high=10000, default=20),
    S("customization", "server", "list", "when empty", "character mods whose characters every game adds", "mods",
      default=[], since="0.9.18"),
    S("require_customization", "server", "bool", "when empty", "players must accept the character mods", "mods",
      default=False, since="0.9.18"),
    S("no_vanilla_characters", "server", "bool", "when empty", "everyone plays as a character mod's character",
      "mods", default=False, since="0.9.18"),
    S("items", "workshop", "ids", "now", "Workshop item IDs the servers download (64 at most); a running server "
      "fetches added ones itself", "mods", default=[]),
    S("steamcmd", "workshop", "str", "restart", "SteamCMD's program, for Workshop downloads (I sets it up)", "mods",
      default=""),
    S("username", "workshop", "str", "restart", "the Steam account SteamCMD is signed in with; it must own MCC",
      "mods", default=""),
    # Updates
    S("auto_update", "folder", "bool", "restart", "install new Project Reclaimer releases by itself (looked for at "
      "the start and hourly); the update restarts the servers at once, mid-game. Huragok checks it happened and "
      "installs the release after the game when it didn't; off: Huragok only tells you (U)", "updates",
      default=False),
    S("update_check_minutes", "workshop", "int", "restart", "minutes between the server's looks for newer Workshop "
      "mod versions (5 at least; 0 never); [workshop_updates] check_minutes keeps it for every server", "updates",
      low=0, high=10080, default=60),
    S("workshop_updates", "huragok", "choice", "huragok", "how new Workshop versions are installed: after the game, "
      "by the server once nobody's on, or by hand", "updates", choices=("after_game", "when_empty", "off")),
    # Listing and network
    S("host_name", "server", "str", "now", "shown beneath this server's name in the browser", "network",
      label="host_name (this server)"),
    S("host_name", "folder", "str", "now", "shown beneath each server's name in the browser", "network",
      default="Dedicated server", label="host_name (every server here)"),
    S("port", "server", "int", "when empty", "its UDP game port (and TCP console port)", "network", low=1024,
      high=65535),
    S("public_address", "folder", "str", "restart", "the address players connect to; empty detects it", "network",
      default=""),
    S("upnp", "folder", "bool", "when empty", "ask the router to forward the ports", "network", default=False),
    S("stats_server", "folder", "str", "when empty", "where finished games are reported; \"off\" nowhere; empty the "
      "public service", "network", default=""),
    S("enabled", "master", "bool", "restart", "run the discovery master the server browser asks", "network",
      default=True),
    S("port", "master", "int", "restart", "the master's TCP port", "network", low=1024, high=65535, default=49175),
    S("peers", "master", "urls", "restart", "more masters (https:// addresses) to report to and share listings with",
      "network", default=[]),
    S("origin", "master", "str", "restart", "the address browsers use when an HTTPS reverse proxy is in front",
      "network", default=""),
    # Remote console
    S("password", "rcon", "str", "when empty", "the console password (8-128 characters; Huragok's connection "
      "follows it)", "rcon", default=""),
    S("address", "rcon", "str", "when empty", "where the console listens: 127.0.0.1 this machine only", "rcon",
      default="127.0.0.1"),
    S("rcon_port", "server", "int", "when empty", "this server's console port, when not its game port", "rcon",
      low=1024, high=65535),
    S("rcon_password", "server", "str", "when empty", "this server's own console password (8-128 characters)", "rcon"),
    # Folders and files
    S("game", "folder", "path", "restart", "Halo 3's files (`dedicated` must restart; nothing is applied live)",
      "files", default="game"),
    S("content", "folder", "path", "restart", "maps, game types, mods and images (`dedicated` must restart)", "files",
      default="content"),
    S("data", "folder", "path", "restart", "logs, chat logs and live status, a folder per server", "files",
      default="data"),
    S("bans", "folder", "path", "when empty", "the ban list (a file several servers can share)", "files",
      default="bans.json"),
    S("auto_reload", "folder", "bool", "now", "apply edits to this file and the content while running", "files",
      default=True, since="0.9.18"),
    # Huragok's own
    S("autostart", "huragok", "bool", "huragok", "start it whenever Huragok starts (after a reboot too)", "huragok",
      default=True),
    S("daily_restart", "huragok", "time", "huragok", "a restart every day at this time (24-hour clock); empty: "
      "none for this server; - takes its own out, so [restarts] daily_restart applies", "huragok"),
    S("priority", "huragok", "choice", "huragok", "how much of the processor it gets before the rest of this PC: "
      "high puts it ahead of everything at normal; realtime ahead of Windows' own work too (needs Huragok run as "
      "administrator; a runaway server can starve the PC); off leaves it as it starts. - takes its own out, so "
      "[process] priority applies", "huragok", choices=PRIORITIES),
]

SERVER_SETTINGS = [s for s in SETTINGS if s.scope == "server"]
FOLDER_SETTINGS = [s for s in SETTINGS if s.scope in ("folder", "rcon", "master", "workshop", "vpn")]
PER_SERVER_DEFAULTS = {s.key for s in SERVER_SETTINGS if s.key not in ("password", "port", "enabled")}
MAX_WELCOME_LINES = 5


def group_settings(group: str) -> list[Setting]:
    return [s for s in SETTINGS if s.group == group]


def find(key: str, scope: str | None = None) -> Setting:
    return next(s for s in SETTINGS if s.key == key and (scope is None or s.scope == scope))


def _block(data: dict[str, Any], server_name: str) -> dict[str, Any]:
    return next((b for b in data.get("server", []) if str(b.get("name", "")).lower() == server_name.lower()), {})


def plays_kind(block: dict[str, Any]) -> str:
    """Which kind of thing a [[server]] block plays: playlist, campaign, odst, variant, map (one map and game type)
    or default (nothing set: Slayer on Guardian). One kind per server; this is the order the dashboard reads them."""
    for key in ("playlist", "odst", "campaign", "variant"):
        if key in block:
            return key
    return "map" if "map" in block or "game" in block else "default"


def plays_of(block: dict[str, Any]) -> str:
    """What a [[server]] block plays, as the dashboard says it: playlist slayer, Slayer on Guardian…"""
    if "playlist" in block:
        return f"playlist {Path(str(block['playlist'])).name.removesuffix('.playlist.json')}"
    if "odst" in block:
        return describe_odst(block["odst"])
    if "campaign" in block:
        campaign = block["campaign"] if isinstance(block["campaign"], dict) else {}
        mission = campaign.get("mission", "")
        title = MISSIONS.get(int(mission), "") if str(mission).isdigit() else ""
        return f"campaign {title or mission} ({campaign.get('difficulty', 'normal')})".replace("  ", " ")
    if "variant" in block:
        return f"variant {Path(str(block['variant'])).stem}"
    if "map" in block or "game" in block:
        return f"{block.get('game', 'Slayer')} on {block.get('map', 'Guardian')}"
    return "Slayer on Guardian"


MISSIONS = {3010: "Sierra 117", 3020: "Crow's Nest", 3030: "Tsavo Highway", 3040: "The Storm", 3050: "Floodgate",
            3070: "The Ark", 3100: "The Covenant", 3110: "Cortana", 3120: "Halo"}
# Each mission's highest start (`insertion`; 0 is the mission's start), as 0.9.18's `dedicated check` takes them
# with MCC's files (2026-10-18): one higher is "The selected insertion point is not available on this mission".
MISSION_STARTS = {3010: 2, 3020: 2, 3030: 1, 3040: 1, 3050: 1, 3070: 2, 3100: 2, 3110: 1, 3120: 1}
DIFFICULTIES = ("easy", "normal", "heroic", "legendary")
# What-it-plays keys and campaign modifiers older programs don't have (0.9.18 refuses each as an unknown field).
PLAYS_SINCE = {"odst": "0.9.19", "sprint": "0.9.19", "sprint_speed_percent": "0.9.19",
               "sprint_stamina_seconds": "0.9.19", "skulls": "0.9.19"}
HOST_NAME_MAX = 48  # "Choose a name of 1–48 characters without control characters"
HTTP_ORIGIN = re.compile(r"https?://[^\s/]+(/\S*)?")
DNS_NAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?")


def version_problem(key: str, since: str | None, program_version: str | None) -> str | None:
    """`key` needs Project Reclaimer `since`: why a server on `program_version` can't take it, or None (it can, or
    either version isn't known)."""
    from .gameoptions import version_tuple

    have, needs = version_tuple(program_version), version_tuple(since)
    if have is None or needs is None or have >= needs:
        return None
    return f"{key} needs Project Reclaimer {since} or newer (this server has {program_version})"


def plays_problem(values: dict[str, Any], program_version: str | None) -> str | None:
    """What-it-plays values (odst, campaign modifiers) a server on `program_version` can't take, or None."""
    keys = [k for k in values if k in PLAYS_SINCE]
    for kind in ("campaign", "odst"):
        modifiers = values.get(kind, {}).get("modifiers") if isinstance(values.get(kind), dict) else None
        keys += [k for k in modifiers if k in PLAYS_SINCE] if isinstance(modifiers, dict) else []
    return next((p for k in keys if (p := version_problem(k, PLAYS_SINCE[k], program_version))), None)


def check_value(setting: Setting, value: Any, config: Path, server_name: str, settings: Settings | None = None,
                program_version: str | None = None) -> str | None:
    """Why the server program would refuse this value for this server, or None: the checks that look past the
    value itself (the other settings in its file, the ports other servers use, the program's version) and the
    formats the program checks as it starts. D runs it before writing, so a value the next start refuses is
    never reported as written."""
    if problem := version_problem(setting.key, setting.since, program_version):
        return problem
    if value is None:
        return None
    try:
        data = read_toml(config)
    except (OSError, ValueError):
        data = {}
    block = _block(data, server_name)
    key = setting.key
    if setting.scope == "server" and key in ("min_players", "max_players"):
        other = "max_players" if key == "min_players" else "min_players"
        low, high = (value, effective(data, other, server_name)) if key == "min_players" else \
            (effective(data, other, server_name), value)
        if isinstance(low, int) and isinstance(high, int) and low > high:
            return f"min_players ({low}) can't be more than max_players ({high})"
        if key == "max_players" and plays_kind(block) in ("campaign", "odst") and value > CO_OP_MAX_PLAYERS:
            return f"a campaign or ODST server holds {CO_OP_MAX_PLAYERS} players at most"
    if key == "host_name" and isinstance(value, str):
        if len(value) > HOST_NAME_MAX:
            return f"host_name is {len(value)} characters; the server program takes {HOST_NAME_MAX} at most"
        if any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in value):
            return "host_name can't have control characters"
    if setting.scope == "folder" and key == "stats_server" and value not in ("", "off") \
            and not HTTP_ORIGIN.fullmatch(str(value)):
        return 'stats_server must be an http:// or https:// address, "off", or empty for the public service'
    if setting.scope == "master" and key == "origin" and value and not HTTP_ORIGIN.fullmatch(str(value)):
        return "[master] origin must be an http:// or https:// address, such as https://master.example"
    if setting.scope == "folder" and key == "public_address" and value and not _ipv4(str(value)) \
            and not DNS_NAME.fullmatch(str(value)):
        return "public_address must be an IPv4 address or a DNS name, or empty to detect it"
    if setting.scope == "rcon" and key == "address" and not _ip(str(value)):
        return "[rcon] address must be an IP address: 127.0.0.1 (this machine only) or 0.0.0.0 (every network)"
    if setting.scope == "server" and key in ("port", "rcon_port") and isinstance(value, int):
        master = data.get("master") if isinstance(data.get("master"), dict) else {}
        if value == master.get("port", find("port", "master").default):
            return f"{value} is the discovery master's port ([master] port)"
        if settings is not None:
            from .newserver import used_ports

            if value in used_ports(settings, skip=(config, server_name)):
                return f"another server here (or a discovery master) uses port {value}"
    return None


def _ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip())
    except ValueError:
        return False
    return True


def _ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text.strip())
    except ValueError:
        return False
    return True


def current(data: dict[str, Any], setting: Setting, server_name: str,
            settings: Settings | None = None) -> tuple[Any, str]:
    """(the value in force as the files have it, where it comes from): this server, [defaults], the file, a table,
    settings.toml, the program's default, or unset."""
    if setting.scope == "plays":
        return plays_of(_block(data, server_name)), "this server"
    if setting.scope == "server":
        block = _block(data, server_name)
        if setting.key in block:
            return block[setting.key], "this server"
        defaults = data.get("defaults", {})
        if isinstance(defaults, dict) and setting.key in defaults:
            return defaults[setting.key], "[defaults]"
        if setting.key == "host_name" and "host_name" in data:
            return data["host_name"], "the file"
        if setting.key == "rcon_port":
            return block.get("port"), "its game port"
        if setting.key == "rcon_password":
            return None, "[rcon] password"
    elif setting.scope == "folder":
        if setting.key in data:
            return data[setting.key], "the file"
    elif setting.scope == "huragok":
        if settings is None:
            return None, "unset"
        override = settings.server_overrides.get(server_name.lower(), {})
        if setting.key in override:
            return override[setting.key], "settings.toml"
        if setting.key == "daily_restart":
            return settings.restarts.daily_restart, "[restarts]"
        if setting.key == "workshop_updates":
            return settings.workshop_updates.install, "[workshop_updates]"
        if setting.key == "priority":
            return (str(settings.backend_options.get("priority", DEFAULT_PRIORITY))
                    if settings.backend == "process" else "off"), "[process]"
    else:
        table = data.get(setting.scope, {})
        if isinstance(table, dict) and setting.key in table:
            return table[setting.key], f"[{setting.scope}]"
    if setting.default is not None:
        return setting.default, "default"
    return None, "unset"


def display(setting: Setting, value: Any) -> str:
    """A value as the screens show it: a password only says whether it's set (the window's transcript is a file)."""
    if "password" in setting.key and value:
        return f"set ({len(str(value))} characters)"
    return show(value)


def show(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in value.items())
    if isinstance(value, str) and not value:
        return '""'
    return str(value)


def parse(setting: Setting, text: str) -> Any:
    """What the host typed, as the value the file takes. Raises ValueError with the reason."""
    text = text.strip()
    if setting.kind == "bool":
        if text.lower() in ("y", "yes", "on", "true", "1"):
            return True
        if text.lower() in ("n", "no", "off", "false", "0"):
            return False
        raise ValueError("answer on or off")
    if setting.kind == "int":
        if not text.lstrip("-").isdigit():
            raise ValueError("a whole number")
        value = int(text)
        if setting.low is not None and value < setting.low or setting.high is not None and value > setting.high:
            raise ValueError(f"between {setting.low} and {setting.high}")
        if setting.key == "update_check_minutes" and 0 < value < 5:
            raise ValueError("5 at least (the server program never looks more often), or 0")
        return value
    if setting.kind == "choice":
        if text.lower() not in setting.choices:
            raise ValueError("one of " + ", ".join(setting.choices))
        return text.lower()
    if setting.kind == "lines":
        lines = [part.strip() for part in text.split("|") if part.strip()]
        if len(lines) > MAX_WELCOME_LINES:
            raise ValueError(f"{MAX_WELCOME_LINES} lines at most")
        return lines[0] if len(lines) == 1 else lines if lines else ""
    if setting.kind == "time":
        if not text:
            return ""
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("a time of day on the 24-hour clock, like 04:00")
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    if setting.kind == "ids":
        ids = []
        for part in re.split(r"[\s,;]+", text):
            found = re.search(r"(?:id=)?(\d{6,})", part)
            if part and not found:
                raise ValueError(f"{part[:30]} isn't a Workshop item ID or address")
            if found and int(found.group(1)) not in ids:
                ids.append(int(found.group(1)))
        if len(ids) > 64:
            raise ValueError("64 items at most")
        return ids
    if setting.kind in ("list", "urls"):
        values = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
        if setting.key == "admins" and (bad := [v for v in values if not PLAYER_ID.fullmatch(v.lower())]):
            raise ValueError(f"{bad[0][:20]} is not a player ID (64 hexadecimal characters, from the game's "
                             "Settings > Online services > Your player ID)")
        if setting.kind == "urls" and (bad := [v for v in values if not v.startswith(("https://", "http://"))]):
            raise ValueError(f"{bad[0][:30]} isn't an https:// address")
        return [v.lower() for v in values] if setting.key == "admins" else values
    console_password = setting.scope == "rcon" and setting.key == "password" or setting.key == "rcon_password"
    if console_password and text and not 8 <= len(text) <= 128:
        raise ValueError("8 to 128 characters")
    if setting.key == "password" and len(text.encode("utf-8")) > 128:
        raise ValueError("128 bytes at most")
    return text


def check_path(setting: Setting, folder: Path, text: str) -> str | None:
    """A folder or file setting must name something there (relative to the server folder): the problem, or None."""
    if setting.kind != "path":
        return None
    target = (folder / text).resolve()
    if setting.key == "bans":
        return None if target.parent.is_dir() else f"{target.parent} isn't there"
    if setting.key == "game" and not (target / "halo3").is_dir():
        return f"{target} has no halo3 folder: not Halo 3's files"
    if setting.key in ("content", "data") and not target.is_dir():
        return f"{target} isn't a folder"
    return None


def can_reset(setting: Setting, where: str) -> bool:
    """A value this server sets itself can be taken out, so [defaults] (or the program's own default) applies."""
    if setting.scope == "huragok":
        return where == "settings.toml" and setting.key != "autostart"
    return setting.scope == "server" and where == "this server" and setting.key not in ("port", "enabled")


def reset(config: Path, server_name: str, setting: Setting) -> bool:
    """Take this server's own value out of its [[server]] block. False: it had none."""
    return remove_server_setting(config, server_name, setting.key)


def write(config: Path, server_name: str, setting: Setting, value: Any) -> None:
    if setting.scope in ("huragok", "plays"):
        raise ValueError(f"{setting.key} isn't written to {config.name}")
    if setting.scope == "server":
        set_server_setting(config, server_name, setting.key, value)
    elif setting.scope == "folder":
        set_top_key(config, setting.key, value)
    else:
        set_table_key(config, setting.scope, setting.key, value, before="master" if setting.scope != "master" else None)


def write_plays(config: Path, server_name: str, values: dict[str, Any]) -> None:
    """What it plays: the new keys in, every other kind's keys out (a server plays one kind of thing), in one
    write, so a change that can't be made safely leaves the file as it was rather than playing nothing."""
    change_server_settings(config, server_name, values, PLAYS_KEYS, what="what it plays")


def catalogue(config: Path, server_name: str, settings: Settings | None = None) -> list[tuple[Setting, Any, str]]:
    """Every setting with its value and where it comes from, in group order (Huragok's own only with settings)."""
    data = read_toml(config)
    return [(s, *current(data, s, server_name, settings)) for s in SETTINGS
            if s.scope != "huragok" or settings is not None]


def summary(entries: list[tuple[Setting, Any, str]], group: str, limit: int = 4) -> str:
    """A group's most telling values in one line, for the list of groups."""
    picked = [(s, v) for s, v, _ in entries if s.group == group][:limit]
    return " · ".join(f"{s.key} {display(s, v)}" for s, v in picked)


def effective(data: dict[str, Any], key: str, server_name: str, scope: str = "server") -> Any:
    """One setting's value in force (the program's default when nothing sets it)."""
    return current(data, find(key, scope), server_name)[0]


__all__ = ["APPLIES", "GROUPS", "SETTINGS", "SERVER_SETTINGS", "FOLDER_SETTINGS", "MISSIONS", "DIFFICULTIES",
           "Setting", "catalogue", "current", "parse", "show", "display", "write", "write_plays", "can_reset", "reset",
           "group_settings", "find", "plays_of", "plays_kind", "effective", "check_path", "summary",
           "CO_OP_MAX_PLAYERS"]
