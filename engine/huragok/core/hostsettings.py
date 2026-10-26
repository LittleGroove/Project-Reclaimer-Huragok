"""Huragok's own settings.toml in one catalogue: every setting, how it's typed, its default and what it does.

The window's D screen shows it in groups (the Superintendent's: which servers and how they're run, restarts, the
watchdog, the keepalive, the event log; Vergil's: automod and the moderators' console relay; Huragok's: its own
updates, the server program's, Workshop updates, new servers and SteamCMD, its folders and files), with each
value and whether it's the file's or the default. A change is made on a copy of the file first, and the copy is
read the way Huragok reads settings.toml as it starts (every part's own check too): a change that would stop
Huragok starting is refused with the reason, and the file is left as it was. A change that passes replaces the
file in one step, comments and layout kept. Edits to settings.toml apply when Huragok starts again; a server's
own Huragok settings (autostart, its daily restart…) are in that server's settings, where they apply at once.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .priority import PRIORITIES
from .priority import DEFAULT as DEFAULT_PRIORITY
from .tomledit import read_toml, remove_table_key, remove_top_key, set_table_key, set_top_key

APPLIES = "when Huragok starts again (the servers keep running meanwhile)"
WATCHDOG_ACTIONS = ("watch", "act", "actnow")
BACKENDS = ("process", "docker", "command", "attach")
SHARE = ("game", "content")
ROLES = ("player", "member", "moderator", "admin", "owner")
RELAY_ROLES = ("moderator", "admin", "owner")

GROUPS: list[tuple[str, str, str, str]] = [  # (key, whose: SUPT/VRGL/HRGK, title, what's in it)
    ("general", "SUPT", "Which servers, and who starts them", "backend, servers, the name players read, dry run"),
    ("running", "SUPT", "How the servers are run", "the backend's own settings, and Huragok's console connection"),
    ("restarts", "SUPT", "Restarts", "warnings, the daily restart, backoff after a crash, FAULTED"),
    ("watchdog", "SUPT", "The watchdog", "dead, nobody gets in, stuck, slow, memory: watch, act or actnow"),
    ("keepalive", "SUPT", "The keepalive", "a server that stopped responding at all"),
    ("eventlog", "SUPT", "The event log", "every event as a line of JSON"),
    ("automod", "VRGL", "Automod", "on or off, dry run, its files, who's exempt, timing"),
    ("relay", "VRGL", "Moderators' console (relay)", "the relay, and each moderator's name, password and role"),
    ("updates", "HRGK", "Huragok's updates", "looking for new versions of Huragok, and installing them"),
    ("server_updates", "HRGK", "Server program updates", "new Project Reclaimer releases"),
    ("workshop_updates", "HRGK", "Workshop updates", "new versions of the servers' Workshop mods"),
    ("setup", "HRGK", "New servers and SteamCMD", "what new servers share, packages, the program, SteamCMD"),
    ("files", "HRGK", "Folders and files", "where the server folders, records and log are"),
]
GROUP_TITLES = {key: title for key, _, title, _ in GROUPS}


@dataclass(frozen=True)
class HostSetting:
    section: str  # its [table]; "" is the top of the file
    key: str
    kind: str  # bool, int, float, str, choice, time, numbers, strings, words, roles, share, path
    help: str
    group: str
    default: Any = None
    choices: tuple[str, ...] = ()
    low: float | None = None
    high: float | None = None
    backend: str | None = None  # shown only when this is the backend
    optional: bool = False  # leaving the line out means something of its own: "take it out" is offered

    @property
    def name(self) -> str:
        return self.key

    @property
    def where(self) -> str:
        return f"[{self.section}] {self.key}" if self.section else self.key


def H(section: str, key: str, kind: str, help: str, group: str, **more: Any) -> HostSetting:  # noqa: N802
    return HostSetting(section, key, kind, help, group, **more)


SETTINGS: list[HostSetting] = [
    # Which servers, and who starts them
    H("", "backend", "choice", "who starts the servers: process (the Superintendent runs each server's program), "
      "docker (docker compose), command (your own commands, such as systemd units) or attach (something else "
      "does; Huragok watches and moderates but can't restart anything)", "general", choices=BACKENDS,
      default="process"),
    H("", "servers", "strings", "which servers, by name as in their dedicated.toml; empty: every enabled server "
      "found", "general", default=[]),
    H("", "name", "str", "the name players read when the Superintendent acts (\"restarting…\")", "general",
      default="Superintendent"),
    H("", "dry_run", "bool", "record every moderation action as \"would have…\" without doing it", "general",
      default=False),
    # How the servers are run
    H("process", "executable", "str", "the server program, in each server's folder", "running",
      default="project-reclaimer.exe", backend="process"),
    H("process", "command", "words", "how it's run, as the whole command ({executable}, {config}, {folder} and "
      "{name} are filled in); empty: the program itself on Windows, wine and the program on Linux", "running",
      default=[], backend="process"),
    H("process", "stop_timeout", "float", "seconds to wait for a clean stop before ending the process", "running",
      default=30, low=1, high=3600, backend="process"),
    H("process", "priority", "choice", "how much of the processor the servers get before the rest of this PC: "
      "high (ahead of everything at normal), realtime (ahead of Windows' own disk, network and input work too: needs "
      "Huragok run as administrator, and a runaway server can starve the PC), above_normal, normal, below_normal, or "
      "off (left as it starts). One server can have its own, on its page", "running", choices=PRIORITIES,
      default=DEFAULT_PRIORITY, backend="process"),
    H("process", "stop_servers_on_exit", "bool", "closing Huragok stops the servers too (off: they keep running, "
      "and starting Huragok again picks them up)", "running", default=False, backend="process"),
    H("docker", "compose_file", "str", "the compose file, in each server's folder", "running",
      default="compose.yaml", backend="docker"),
    H("command", "start", "str", "the command that starts a server ({name}, {slug}, {service}, {port}, {folder}, "
      "{config} are filled in)", "running", default="", backend="command"),
    H("command", "stop", "str", "the command that stops a server", "running", default="", backend="command"),
    H("command", "restart", "str", "the command that restarts a server", "running", default="", backend="command"),
    H("rcon", "use_host_key", "bool", "sign in on the console spot each server keeps for its own machine, so "
      "moderators keep all 4 normal spots (needs Huragok on the same machine)", "running", default=True),
    H("rcon", "command_timeout", "float", "seconds a console command may take", "running", default=10, low=1,
      high=600),
    # Restarts
    H("restarts", "warn_seconds", "numbers", "chat warnings before a planned stop or restart, this many seconds "
      "before it, in turn", "restarts", default=[300, 180, 60, 30]),
    H("restarts", "after_game_seconds", "float", "a restart \"after this game\": seconds of notice once the game "
      "has ended", "restarts", default=15, low=0, high=3600),
    H("restarts", "daily_restart", "time", "restart every server every day at this time (24-hour clock); empty: "
      "never. One server's own time is in its settings", "restarts", default=""),
    H("restarts", "min_uptime_seconds", "float", "a daily restart waits until the server has been up this long",
      "restarts", default=900, low=0, high=86400),
    H("restarts", "backoff_seconds", "numbers", "a server that died: wait this long before each new start, in "
      "turn (the last repeats)", "restarts", default=[5, 10, 30, 60, 300]),
    H("restarts", "good_run_seconds", "float", "a server that dies within this long of starting counts as a failed "
      "start", "restarts", default=600, low=0, high=86400),
    H("restarts", "max_failures", "int", "this many failed starts in a row: FAULTED (the Superintendent stops "
      "trying for now)", "restarts", default=5, low=1, high=100),
    H("restarts", "restarts_per_hour", "int", "more automatic restarts than this in one hour means something is "
      "looping: FAULTED", "restarts", default=6, low=1, high=100),
    H("restarts", "faulted_retry_minutes", "numbers", "a FAULTED server is tried once more after this many minutes, "
      "in turn (the last repeats); empty leaves it for you to start (S)", "restarts", default=[30, 60], low=1),
    H("restarts", "ready_seconds", "float", "a started server must answer on its console within this long, or it's "
      "a failed start", "restarts", default=300, low=30, high=3600),
    H("restarts", "empty_seconds", "float", "a countdown ends once nobody has been on for this long", "restarts",
      default=10, low=0, high=3600),
    # The watchdog
    H("watchdog", "enabled", "bool", "the watchdog looks at every server each minute", "watchdog", default=True),
    H("watchdog", "mode", "choice", "act: each case does what it says below; watch: every case only watches (a dry "
      "run, to see what it would do)", "watchdog", choices=("act", "watch"), default="act"),
    H("watchdog", "report_minutes", "int", "a health line in the log this often; 0: never", "watchdog", default=15,
      low=0, high=1440),
    H("watchdog", "dead", "choice", "every player dropped at once: watch (log it), act (restart after the game) or "
      "actnow (restart right away)", "watchdog", choices=WATCHDOG_ACTIONS, default="actnow"),
    H("watchdog", "drop_players", "int", "it counts as dead when at least this many went at once…", "watchdog",
      default=8, low=0, high=128),
    H("watchdog", "drop_share", "float", "…at least this share of those on (0 to 1)…", "watchdog", default=0.5,
      low=0, high=1),
    H("watchdog", "drop_left", "int", "…and no more than this many were left", "watchdog", default=0, low=0,
      high=128),
    H("watchdog", "no_joins", "choice", "players keep connecting and nobody gets in, while there's room",
      "watchdog", choices=WATCHDOG_ACTIONS, default="actnow"),
    H("watchdog", "no_joins_connections", "int", "this many connections (0: never checked)…", "watchdog", default=3,
      low=0, high=1000),
    H("watchdog", "no_joins_minutes", "int", "…in this many minutes, and none of them joined", "watchdog", default=5,
      low=0, high=1440),
    H("watchdog", "stuck", "choice", "on the scoreboard after a game, players waiting", "watchdog",
      choices=WATCHDOG_ACTIONS, default="actnow"),
    H("watchdog", "stuck_minutes", "int", "this long counts as stuck (0: never checked)", "watchdog", default=3,
      low=0, high=1440),
    H("watchdog", "slow", "choice", "the server falling behind for a good share of several minutes (a restart "
      "doesn't make a full server faster)", "watchdog", choices=WATCHDOG_ACTIONS, default="watch"),
    H("watchdog", "slow_share", "float", "a minute is slow when this share of it was (0 to 1)…", "watchdog",
      default=0.25, low=0, high=1),
    H("watchdog", "slow_minutes", "int", "…and this many minutes were slow…", "watchdog", default=3, low=0,
      high=1440),
    H("watchdog", "slow_window_minutes", "int", "…out of the last this many", "watchdog", default=5, low=0,
      high=1440),
    H("watchdog", "very_slow", "choice", "falling behind nearly all of every minute, minutes on end", "watchdog",
      choices=WATCHDOG_ACTIONS, default="watch"),
    H("watchdog", "very_slow_share", "float", "this share of each minute (0 to 1)…", "watchdog", default=0.9, low=0,
      high=1),
    H("watchdog", "very_slow_minutes", "int", "…this many minutes in a row", "watchdog", default=3, low=0, high=1440),
    H("watchdog", "memory", "choice", "the server program using more memory than memory_mb", "watchdog",
      choices=WATCHDOG_ACTIONS, default="act"),
    H("watchdog", "memory_mb", "int", "the most memory a server may use, MB; 0: never checked", "watchdog",
      default=0, low=0, high=1048576),
    # The keepalive
    H("keepalive", "hang_seconds", "float", "a server has stopped responding when its status.json isn't rewritten "
      "(normally every 5 seconds) and its console is silent, both for this long", "keepalive", default=180, low=30,
      high=3600),
    H("keepalive", "check_seconds", "float", "how often it looks", "keepalive", default=10, low=1, high=600),
    H("keepalive", "act", "bool", "restart a server that stopped responding (off: only report it)", "keepalive",
      default=True),
    # The event log
    H("eventlog", "enabled", "bool", "every event as a line of JSON, one file per UTC day (player IPs are in it: "
      "keep the folder private)", "eventlog", default=False),
    H("eventlog", "folder", "path", "where the files go", "eventlog", default="logs/events"),
    # Automod
    H("automod", "enabled", "bool", "Vergil acts on the words in its word list said in chat, and on banned names",
      "automod", default=True),
    H("automod", "dry_run", "bool", "only log what it would do (the window's VRGL lines); off: it acts", "automod",
      default=False),
    H("automod", "words", "path", "the word list", "automod", default="automod.txt"),
    H("automod", "names", "path", "the banned names (!strike <player> name adds one)", "automod",
      default="names.txt"),
    H("automod", "forget_after_days", "float", "strikes older than this stop counting; 0: never", "automod",
      default=0, low=0, high=36500),
    H("automod", "name", "str", "the name players read (\"removed by Vergil\")", "automod", default="Vergil"),
    H("automod", "exempt_roles", "roles", "never acted on (roles as in commands.toml; player is everyone)",
      "automod", default=["admin", "owner"]),
    H("automod", "check_names", "bool", "kick names with a word from the word list in them (the banned names always "
      "apply)", "automod", default=True),
    H("automod", "name_match", "choice", "how a word from the word list catches a name: anywhere (anywhere in it: "
      "hitler catches DasHitler and xXhitlerXx, and a short word catches more than you mean unless its line lists "
      "the exception, like -classic) or words (only as a word of its own, the name taken apart at its capitals and "
      "digits: DasHitler, not dashitler)", "automod", choices=("anywhere", "words"), default="anywhere"),
    H("automod", "across_servers", "bool", "Huragok's moderation (! commands, Vergil) reaches every server: a mute "
      "follows the player to any server, a kick keeps them off every server until its time is up, a ban reaches "
      "servers with a ban list of their own. The game's own /kick and /ban, and votes, stay on their server (they're "
      "only written on the player's record); false: Huragok's stay on theirs too. Each server's own Vergil (on, dry "
      "run, off) is on its page", "automod", default=True),
    H("automod", "cooldown_seconds", "float", "lines this close together count as one strike", "automod",
      default=10, low=0, high=3600),
    H("automod", "split_seconds", "float", "a word split over lines this close together is caught", "automod",
      default=10, low=0, high=3600),
    # Moderators' console
    H("relay", "enabled", "bool", "moderators elsewhere connect to Huragok instead of a server, each with a password "
      "and name of their own; only the commands their role allows pass", "relay", default=False),
    H("relay", "address", "str", "where the relay listens: keep 127.0.0.1 and reach it through an SSH tunnel or a "
      "VPN (it isn't encrypted)", "relay", default="127.0.0.1"),
    H("relay", "port_offset", "int", "each server's relay is at its game port plus this, for the RCON client "
      "(49176 -> 50176)", "relay", default=1000, low=1, high=60000),
    H("relay", "source_port_offset", "int", "and at its game port plus this for Source RCON tools; 0 switches "
      "that off", "relay", default=2000, low=0, high=60000),
    # Huragok's updates
    H("updates", "check", "bool", "look for new versions of Huragok when it starts and every check_hours", "updates",
      default=True),
    H("updates", "check_hours", "float", "hours between looks", "updates", default=1, low=1, high=720),
    H("updates", "auto_update", "bool", "a new version installs itself as soon as it's found (the servers keep "
      "running); off: it waits for U", "updates", default=False),
    H("updates", "beta", "bool", "beta releases too (GitHub pre-releases), to try what's coming before it's a full "
      "release; a beta can have bugs the full release won't. Off: full releases only (turning it off doesn't go "
      "back: a beta stays until a full release passes it)", "updates", default=False),
    H("updates", "repository", "str", "where Huragok's releases come from, owner/name on GitHub", "updates",
      default="LittleGroove/Project-Reclaimer-Huragok"),
    # Server program updates
    H("server_updates", "check", "bool", "look for new Project Reclaimer releases", "server_updates", default=True),
    H("server_updates", "check_hours", "float", "hours between looks (hourly, as the servers' own updaters do)",
      "server_updates", default=1, low=1, high=720),
    H("server_updates", "install", "choice", "for servers with auto_update off: ask (only say so; U installs), "
      "after_game (install and restart each after its game) or now", "server_updates",
      choices=("ask", "after_game", "now"), default="ask"),
    H("server_updates", "repository", "str", "where the server program's releases come from, owner/name on GitHub",
      "server_updates", default="ProjectReclaimer/project-reclaimer-releases"),
    H("server_updates", "follow", "bool", "once one server has updated itself, restart the others that update "
      "themselves after their game, so every server is on the new version within a game", "server_updates",
      default=True),
    H("server_updates", "fallback", "bool", "a server with auto_update on that didn't update itself gets the release "
      "from Huragok after its game (off: only say so)", "server_updates", default=True),
    # Workshop updates
    H("workshop_updates", "install", "choice", "after_game (stop the server when the game ends, swap the new version "
      "in, start it), when_empty (the server's own way, once nobody is on) or off (by hand)", "workshop_updates",
      choices=("after_game", "when_empty", "off"), default="after_game"),
    H("workshop_updates", "check_minutes", "int", "minutes between looks at Steam (5 at least; 0 leaves each server's "
      "own setting alone and Huragok doesn't look)", "workshop_updates", default=5, low=0, high=10080),
    # New servers and SteamCMD
    H("setup", "share", "share", "what new servers take from this folder instead of a copy of their own: game "
      "(Halo 3's files), content (one content folder for every server). Never the ban list: every server keeps its "
      "own, and Huragok puts its own bans on every one", "setup", default=["game"]),
    H("setup", "mcc_folder", "path", "your MCC folder, for Halo 3's files when no server has them yet; empty: "
      "Steam's own folder; left out: linked from a server that has them", "setup", optional=True),
    H("setup", "packages_folder", "path", "where the packages are", "setup", default="PACKAGES"),
    H("setup", "program", "path", "the server program for what isn't one server's (making new folders, checking "
      "custom games); empty: the newest copy here", "setup", default=""),
    H("setup", "steamcmd", "path", "SteamCMD's program (I sets it up); empty: looked for in steamcmd/ here, "
      "C:\\steamcmd and PATH", "setup", default=""),
    H("setup", "steam_username", "str", "the Steam account SteamCMD signs in with; it must own MCC (never a password "
      "here)", "setup", default=""),
    H("setup", "download_game_files", "bool", "a server missing Halo 3's files gets them by itself (copied from MCC, "
      "or downloaded with SteamCMD)", "setup", default=True),
    H("setup", "auto_update", "bool", "new servers install new Project Reclaimer releases themselves (auto_update "
      "in their dedicated.toml)", "setup", default=True),
    # Folders and files
    H("", "servers_folder", "path", "where the server folders are, one folder per server", "files",
      default="SERVERS"),
    H("", "server_folder", "path", "one server folder somewhere else, on top of those (\"..\" is the folder this one "
      "sits in)", "files", default=""),
    H("", "dedicated_config", "str", "the name of each server folder's configuration file", "files",
      default="dedicated.toml"),
    H("", "database", "path", "Huragok's records: every player seen, every action with who and why", "files",
      default="records/huragok.sqlite3"),
    H("", "log_file", "path", "Huragok's log", "files", default="logs/huragok.log"),
]


class HostSettingsError(ValueError):
    """A change refused: the reason, as the host reads it."""


def find(section: str, key: str) -> HostSetting:
    return next(s for s in SETTINGS if s.section == section and s.key == key)


def current(data: dict[str, Any], setting: HostSetting) -> tuple[Any, str]:
    """(the value in force as the file has it, "the file" or "default"); (None, "left out") for an optional one."""
    table = data if not setting.section else data.get(setting.section, {})
    if setting.section == "process" and "process" not in data and isinstance(data.get("windows"), dict):
        table = data["windows"]  # 0.1's name for [process]
    if isinstance(table, dict) and setting.key in table:
        return table[setting.key], "the file"
    if setting.optional:
        return None, "left out"
    return setting.default, "default"


def backend_of(data: dict[str, Any]) -> str:
    backend = str(data.get("backend") or "process")
    return "process" if backend == "windows" else backend


def catalogue(path: Path) -> list[tuple[HostSetting, Any, str]]:
    """Every setting that applies with the file's backend, with its value and where it comes from. Raises OSError
    or ValueError (the file can't be read)."""
    data = read_toml(path)
    backend = backend_of(data)
    return [(s, *current(data, s)) for s in SETTINGS if s.backend is None or s.backend == backend]


def summary(entries: list[tuple[HostSetting, Any, str]], group: str, limit: int = 4) -> str:
    picked = [(s, v) for s, v, _ in entries if s.group == group][:limit]
    return " · ".join(f"{s.key} {show(s, v)}" for s, v in picked)


def show(setting: HostSetting, value: Any) -> str:
    """A value as the screens show it."""
    if value is None:
        return "(left out)"
    if isinstance(value, bool):
        return "on" if value else "off"
    if setting.kind == "words" and isinstance(value, list):
        return " ".join(_quoted(str(v)) for v in value) or "(none)"
    if isinstance(value, list):
        return ", ".join(_number(v) if isinstance(v, (int, float)) else str(v) for v in value) or "(none)"
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str) and not value:
        return '""'
    return str(value)


def _number(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def _quoted(word: str) -> str:
    return f'"{word}"' if not word or any(c.isspace() for c in word) else word


def hint(setting: HostSetting) -> str:
    """How to type a new value, for the question."""
    if setting.kind in ("int", "float"):
        whole = "a whole number" if setting.kind == "int" else "a number"
        if setting.low is not None and setting.high is not None:
            return f"{whole}, {_number(setting.low)} to {_number(setting.high)}"
        return whole
    return {"numbers": "numbers separated by commas, like 300, 60, 30; empty for none",
            "strings": "names separated by commas; empty for none",
            "words": "the command's words separated by spaces, \"quoted\" when one has a space; empty for none",
            "time": "HH:MM on the 24-hour clock, empty for none",
            "path": "a file or folder, from Huragok's folder or in full",
            "str": "text"}.get(setting.kind, "text")


def parse(setting: HostSetting, text: str) -> Any:
    """What the host typed, as the value the file takes. Raises HostSettingsError with the reason."""
    text = text.strip()
    kind = setting.kind
    if kind == "bool":
        if text.lower() in ("y", "yes", "on", "true", "1"):
            return True
        if text.lower() in ("n", "no", "off", "false", "0"):
            return False
        raise HostSettingsError("answer on or off")
    if kind in ("int", "float"):
        try:
            value: float = int(text) if kind == "int" else float(text)
        except ValueError:
            raise HostSettingsError("a whole number" if kind == "int" else "a number") from None
        if not math.isfinite(value):  # inf, nan, 1e400: no setting means those, and int() of one raises
            raise HostSettingsError("a number")
        if kind == "float" and value == int(value) and abs(value) < 1e15:
            value = int(value)  # 15 rather than 15.0 in the file
        if setting.low is not None and value < setting.low or setting.high is not None and value > setting.high:
            raise HostSettingsError(f"between {_number(setting.low)} and {_number(setting.high)}")
        if setting.key == "check_minutes" and 0 < value < 5:
            raise HostSettingsError("5 at least (the server program never looks more often), or 0")
        return value
    if kind == "choice":
        if text.lower() not in setting.choices:
            raise HostSettingsError("one of " + ", ".join(setting.choices))
        return text.lower()
    if kind == "time":
        if not text:
            return ""
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise HostSettingsError("a time of day on the 24-hour clock, like 04:00")
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    if kind == "numbers":
        values: list[float] = []
        for part in [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]:
            try:
                number = float(part)
            except ValueError:
                raise HostSettingsError(f"{part[:20]} isn't a number") from None
            if not math.isfinite(number):
                raise HostSettingsError(f"{part[:20]} isn't a number")
            low = setting.low if setting.low is not None else 0
            if number < low:
                raise HostSettingsError(f"each {_number(low)} or more")
            values.append(int(number) if number == int(number) else number)
        return values
    if kind == "strings":
        return [part.strip() for part in text.split(",") if part.strip()]
    if kind == "words":
        try:
            words = shlex.split(text, posix=False)
        except ValueError as error:
            raise HostSettingsError(f"the quotes don't pair up ({error})") from None
        return [w[1:-1] if len(w) >= 2 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]
    if kind == "path" and any(ord(c) < 0x20 for c in text):
        raise HostSettingsError("a file or folder name can't have control characters")
    return text


def write(path: Path, setting: HostSetting, value: Any) -> None:
    """Set it, checked first (see apply). None takes the line out (an optional setting's own meaning)."""
    if value is None:
        apply(path, lambda copy: _remove(copy, setting), f"{setting.where} taken out")
        return

    def change(copy: Path) -> None:
        if setting.section:
            set_table_key(copy, setting.section, setting.key, value)
        else:
            set_top_key(copy, setting.key, value)

    apply(path, change, f"{setting.where} = {show(setting, value)}")


def _remove(path: Path, setting: HostSetting) -> None:
    if setting.section:
        remove_table_key(path, setting.section, setting.key)
    else:
        remove_top_key(path, setting.key)


# Checking a change before it's made ------------------------------------------------------------

def problems(path: Path, shown_as: str | None = None) -> list[str]:
    """What would stop Huragok starting with this settings.toml: the file as config.load reads it, then each part
    that's on, as `check` checks them. `shown_as` names the file in the messages (a copy is checked as the real
    file)."""
    from .tables import ConfigError
    from .settings import load
    from ..parts import PARTS, config_problem

    def named(text: str) -> str:
        return text.replace(str(path), shown_as).replace(path.name, Path(shown_as).name) if shown_as else text

    try:
        settings = load(path)
    except ConfigError as error:
        return [named(str(error))]
    found: list[str] = []
    for name, cls in PARTS.items():
        part = settings.parts[name]
        if cls.always_on or not part.enabled:
            continue
        try:
            cls.preflight(part.options, settings.home)
        except ValueError as error:
            found.append(f"[{name}] {named(str(error))}")
            continue
        if problem := config_problem(settings, name, cls):
            found.append(f"[{name}] {named(problem)}")
    return found


def apply(path: Path, change: Callable[[Path], None], what: str) -> None:
    """Make `change` on a copy of settings.toml beside it, check the copy as Huragok would read it, and only then
    put it in place of the file (in one step). A problem the change brings is a HostSettingsError and the file is
    left as it was; one the file had already isn't this change's, and doesn't stop it."""
    copy = path.with_name(f".{path.name}.{os.getpid()}.check")
    try:
        shutil.copy2(path, copy)
        try:
            change(copy)
        except OSError as error:  # tomledit's refusal names the file it was given: name the real one
            raise type(error)(str(error).replace(copy.name, path.name)) from None
        before = problems(path)
        new = [p for p in problems(copy, shown_as=str(path)) if p not in before]
        if new:
            raise HostSettingsError(f"not changed ({what}): {new[0]}")
        os.replace(copy, path)
    finally:
        copy.unlink(missing_ok=True)


# Moderators' console: the [[relay.user]] blocks ------------------------------------------------

@dataclass(frozen=True)
class RelayUser:
    name: str
    role: str
    password_length: int


def relay_users(path: Path) -> list[RelayUser]:
    users = read_toml(path).get("relay", {}).get("user", [])
    return [RelayUser(str(u.get("name", "")), str(u.get("role", "moderator")), len(str(u.get("password", ""))))
            for u in users if isinstance(u, dict)] if isinstance(users, list) else []


def check_relay_user(path: Path, name: str, password: str, role: str, *, replacing: str | None = None) -> str | None:
    """Why this moderator can't be written, or None: the rules config.load holds the blocks to."""
    if not name.strip():
        return "a moderator needs a name"
    if not 8 <= len(password) <= 128:
        return "the password must be 8 to 128 characters"
    if role not in RELAY_ROLES:
        return f"the role is one of {', '.join(RELAY_ROLES)}"
    users = read_toml(path).get("relay", {}).get("user", [])
    for user in users if isinstance(users, list) else []:
        other = str(user.get("name", "")) if isinstance(user, dict) else ""
        if replacing is not None and other.lower() == replacing.lower():
            continue
        if other.lower() == name.strip().lower():
            return f"there's a moderator named {other} already"
        if isinstance(user, dict) and user.get("password") == password:
            return "another moderator has that password, and the password is what tells them apart"
    return None


def add_relay_user(path: Path, name: str, password: str, role: str) -> None:
    from .tomledit import add_array_block

    if problem := check_relay_user(path, name, password, role):
        raise HostSettingsError(problem)
    apply(path, lambda copy: add_array_block(copy, ("relay", "user"),
                                             {"name": name.strip(), "password": password, "role": role}),
          f"moderator {name.strip()} added")


def change_relay_user(path: Path, name: str, **values: Any) -> None:
    from .tomledit import set_array_block_key

    def change(copy: Path) -> None:
        for key, value in values.items():
            set_array_block_key(copy, ("relay", "user"), name, key, value)

    apply(path, change, f"moderator {name} changed")


def remove_relay_user(path: Path, name: str) -> None:
    from .tomledit import remove_array_block

    apply(path, lambda copy: remove_array_block(copy, ("relay", "user"), name), f"moderator {name} taken out")


__all__ = ["APPLIES", "GROUPS", "GROUP_TITLES", "SETTINGS", "HostSetting", "HostSettingsError", "RelayUser",
           "add_relay_user", "apply", "backend_of", "catalogue", "change_relay_user", "check_relay_user", "current",
           "find", "hint", "parse", "problems", "relay_users", "remove_relay_user", "show", "summary", "write"]
