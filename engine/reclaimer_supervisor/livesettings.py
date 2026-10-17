"""A server's settings, changed while it runs: what each one is, how it's typed, and when
the server applies it (Project Reclaimer 0.9.18, auto_reload on).

The window's D screen shows this catalogue with the current values, writes the new
value into dedicated.toml in place (the comments stay), and the server re-reads
the file a couple of seconds later: most settings apply at once, some from the next
game or vote, some by a restart once the server is empty (the server's own
supervisor does that), and a few need `dedicated` itself restarted (R).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .tomledit import read_toml, set_server_setting, set_table_key, set_top_key

APPLIES = {
    "now": "at once; players stay connected",
    "next game": "from the next game",
    "next vote": "from the next playlist vote",
    "when empty": "the server restarts by itself once nobody is on",
    "restart": "needs this server restarted from here (R)",
}


@dataclass(frozen=True)
class Setting:
    key: str
    scope: str  # server: its [[server]] block; folder: top of the file; rcon / master: that table
    kind: str  # bool, int, str, list, choice
    applies: str  # a key of APPLIES
    help: str
    choices: tuple[str, ...] = ()
    low: int | None = None
    high: int | None = None

    @property
    def where(self) -> str:
        return {"server": "this server", "folder": "every server in its folder", "rcon": "[rcon]",
                "master": "[master]"}[self.scope]


SERVER_SETTINGS: list[Setting] = [
    Setting("password", "server", "str", "now", "the join password; empty lets anyone join"),
    Setting("max_players", "server", "int", "now", "players at once (campaign co-op at most 16)", low=1, high=127),
    Setting("min_players", "server", "int", "now", "wait in the lobby until this many join", low=1, high=127),
    Setting("max_ping", "server", "int", "now", "turn away players joining with a higher ping, ms; 0 takes everyone",
            low=0, high=1000),
    Setting("votes", "server", "bool", "now", "players may vote to kick, end the round or game, shuffle"),
    Setting("voice", "server", "bool", "now", "voice chat at all"),
    Setting("voice_global", "server", "bool", "now", "the V key: everyone hears"),
    Setting("voice_team", "server", "bool", "now", "the B key: teammates hear"),
    Setting("voice_proximity", "server", "bool", "now", "the N key: players nearby hear"),
    Setting("text_chat", "server", "bool", "now", "text chat"),
    Setting("welcome", "server", "list", "now", "chat lines players read as they join (up to 5; {name}, {server})"),
    Setting("share_mods", "server", "bool", "now", "players without a mod download it from this server"),
    Setting("share_rate", "server", "int", "now", "upload limit for shared mods, Mbit/s; 0 is none", low=0, high=10000),
    Setting("host_name", "server", "str", "now", "shown beneath the name in the server browser"),
    Setting("friendly_fire", "server", "bool", "next game", "teammates can hurt each other"),
    Setting("team_changing", "server", "choice", "next game", "how far players may change teams themselves",
            choices=("balanced", "game_type", "off")),
    Setting("playlist", "server", "str", "next vote", "the rotation file, e.g. playlists/infection.playlist.json "
                                                     "(G manages them)"),
    Setting("hidden", "server", "bool", "when empty", "never listed; players join by address"),
    Setting("port", "server", "int", "when empty", "its UDP game port (and TCP console port)", low=1024, high=65535),
    Setting("anti_cheat", "server", "choice", "when empty", "reject impossible game data: enforce kicks, detect logs",
            choices=("enforce", "detect", "off")),
    Setting("block_vpn", "server", "bool", "when empty", "refuse players on a VPN, proxy or hosting provider"),
    Setting("chat_log", "server", "bool", "when empty", "keep the chat in data/<server>/chat"),
    Setting("load_timeout", "server", "int", "when empty", "seconds a player may hold up the next game; 0 waits",
            low=0, high=600),
    Setting("customization", "server", "list", "when empty", "character mods whose characters every game adds"),
    Setting("require_customization", "server", "bool", "when empty", "players must accept the character mods"),
    Setting("no_vanilla_characters", "server", "bool", "when empty", "everyone plays as a character mod's character"),
    Setting("enabled", "server", "bool", "when empty", "false stops this server once it's empty and keeps it off"),
]

FOLDER_SETTINGS: list[Setting] = [
    Setting("host_name", "folder", "str", "now", "shown beneath each server's name in the browser"),
    Setting("admins", "folder", "list", "now", "player IDs who may kick and ban on every server here"),
    Setting("stats_server", "folder", "str", "when empty", "where finished games are reported; \"off\" nowhere; "
                                                            "empty the public service"),
    Setting("upnp", "folder", "bool", "when empty", "ask the router to forward the ports"),
    Setting("auto_reload", "folder", "bool", "now", "apply edits to this file and the content while running"),
    Setting("public_address", "folder", "str", "restart", "the address players connect to; empty detects it"),
    Setting("password", "rcon", "str", "when empty", "the console password (8-128 characters; the supervisor's "
                                                     "connection follows it)"),
    Setting("address", "rcon", "str", "when empty", "where the console listens: 127.0.0.1 this machine only"),
    Setting("enabled", "master", "bool", "restart", "run the discovery master the server browser asks"),
    Setting("port", "master", "int", "restart", "the master's TCP port", low=1024, high=65535),
]

PER_SERVER_DEFAULTS = {s.key for s in SERVER_SETTINGS if s.key not in ("password", "port", "playlist", "enabled")}


def current(data: dict[str, Any], setting: Setting, server_name: str) -> tuple[Any, str]:
    """(the value in force as the file has it, where it comes from): server block, [defaults], the file, or unset."""
    if setting.scope == "server":
        block = next((b for b in data.get("server", []) if str(b.get("name", "")).lower() == server_name.lower()), {})
        if setting.key in block:
            return block[setting.key], "this server"
        defaults = data.get("defaults", {})
        if isinstance(defaults, dict) and setting.key in defaults:
            return defaults[setting.key], "[defaults]"
        return None, "unset"
    if setting.scope == "folder":
        return (data[setting.key], "the file") if setting.key in data else (None, "unset")
    table = data.get(setting.scope, {})
    return (table[setting.key], f"[{setting.scope}]") if isinstance(table, dict) and setting.key in table \
        else (None, "unset")


def show(value: Any) -> str:
    if value is None:
        return "(unset)"
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(none)"
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
        return value
    if setting.kind == "choice":
        if text.lower() not in setting.choices:
            raise ValueError("one of " + ", ".join(setting.choices))
        return text.lower()
    if setting.kind == "list":
        return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    if setting.key == "password" and setting.scope == "rcon" and text and not 8 <= len(text) <= 128:
        raise ValueError("8 to 128 characters")
    return text


def write(config: Path, server_name: str, setting: Setting, value: Any) -> None:
    if setting.scope == "server":
        set_server_setting(config, server_name, setting.key, value)
    elif setting.scope == "folder":
        set_top_key(config, setting.key, value)
    else:
        set_table_key(config, setting.scope, setting.key, value)


def catalogue(config: Path, server_name: str) -> list[tuple[Setting, Any, str]]:
    """Every setting with its current value and where it comes from, server settings first."""
    data = read_toml(config)
    return [(s, *current(data, s, server_name)) for s in [*SERVER_SETTINGS, *FOLDER_SETTINGS]]
