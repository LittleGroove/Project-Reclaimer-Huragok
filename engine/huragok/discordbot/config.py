"""discord.toml: what the host wants of the bot.

Its own file, so nothing before 0.6.0 ever reads it and a rollback is safe. Unlike settings.toml, a setting it doesn't
know is only a warning (a later version's file still works here), and a value of the wrong kind falls back to its
default with a warning. A file that can't be read at all keeps the bot off at the start; on a reload what was in force
stays and the problem is shown, as with commands.toml. The template, templates/discord.toml, is every setting there is:
what it holds is what's known, and what it says is the default. The token is never shown, logged or put in a repr.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ..core.files import write_atomic
from ..core.discordmoved import KEYS as MOVED_KEYS
from ..core.discordmoved import LINE_FIELDS
from ..core.discordmoved import TEXTS as MOVED_TEXTS
from ..core.tomledit import add_to_list, read_toml, remove_from_list, set_nested_key, set_table_key, set_top_key

FILE = "discord.toml"
TEMPLATE = "templates/discord.toml"
MODES = ("off", "webhook", "bot")
VOICES = ("lore", "plain")

# Tables whose keys are the host's own, on top of those the template writes as {} (per_server, names…).
FREE_FORM = {("text",), ("ranks", "scoped"), ("colours",), ("notes", "flags")}
OWN_ENTIRELY = {("notes", "flags")}  # what's written is the whole of it: one taken out stays out
# How a message looks follows from the channel it goes to. A channel named any other way is a feed: a post, as a card.
TICKET, LOG, FEED = "ticket", "log", "feed"
SHAPES = {"staff": TICKET, "reports": TICKET, "appeals": TICKET, "alerts": TICKET, "logs": LOG, "huragok-logs": LOG}
# A channel left unset uses another, so a small Discord can run on just staff and logs.
FALLBACKS = {"reports": "staff", "appeals": "reports", "alerts": "staff", "huragok-logs": "logs"}
OLD_NAMES = {"public": "status"}  # as a beta's file may still say


def pings_of(value: Any) -> list[str]:
    """The roles a line of [messages] pings, or that `value` is: one, as a name or an ID, or a list of them."""
    wanted = value.get("ping") if isinstance(value, dict) else value
    return list(dict.fromkeys(text for item in (wanted if isinstance(wanted, list) else [wanted]) if (text := str(item or "").strip())))


def ping_value(roles: list[str]) -> Any:
    """What goes in the file: one stays as plain as it always was."""
    return roles[0] if len(roles) == 1 else roles if roles else ""


def shape_of(channel: Any) -> str:
    """How a message sent to this channel looks. Named in [channels]: its kind; anything else, a post."""
    name = str(channel or "").strip()
    return SHAPES.get(OLD_NAMES.get(name, name), FEED)
DEFAULT_COLOUR = 0x95A5A6

# What no [limits] value goes under (or, for CEILINGS, over): Discord's limits, in the design's table.
FLOORS = {"status_seconds": 60, "rename_minutes": 5, "voice_status_minutes": 1, "presence_seconds": 30,
          "profile_minutes": 15, "wait_seconds": 5}
CEILINGS = {"logins_per_day": 900, "wait_seconds": 60}
# Each feature's own timer, and the [limits] value it can't go under.
TIMERS = {("status", "board", "update_seconds"): "status_seconds",
          ("status", "presence", "rotate_seconds"): "presence_seconds",
          ("status", "bio", "update_minutes"): "profile_minutes",
          ("status", "nickname", "update_minutes"): "profile_minutes",
          ("status", "channel_names", "update_minutes"): "rename_minutes",
          ("status", "topics", "update_minutes"): "rename_minutes",
          ("status", "voice_status", "update_minutes"): "voice_status_minutes"}


class DiscordFileError(ValueError):
    """discord.toml can't be used; the reason is written for the host."""


def template_text() -> str:
    return resources.files("huragok").joinpath(TEMPLATE).read_text(encoding="utf-8")


_defaults: dict[str, Any] | None = None


def defaults() -> dict[str, Any]:
    """Every setting there is, with its default: a copy, for the caller to keep."""
    global _defaults
    if _defaults is None:
        _defaults = tomllib.loads(template_text())
    return copy.deepcopy(_defaults)


def _shown(where: tuple[str, ...]) -> str:
    return f"[{'.'.join(where)}] " if where else ""


def _same_kind(default: Any, value: Any, key: str = "") -> bool:
    if key in ("ping", "thread_role"):  # a role, or a list of them
        return isinstance(value, (str, list))
    if key == "servers" and not isinstance(default, dict):  # "*", or a list of them ([feed.servers] is a feed of that name)
        return isinstance(value, (str, list))
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def _kind(value: Any) -> str:
    return ("true or false" if isinstance(value, bool) else "a number" if isinstance(value, (int, float))
            else "text" if isinstance(value, str) else "a list" if isinstance(value, list) else "a table")


LINE_EXTRAS = ("webhook", "closes_to", "as", "silent", "remove_after_minutes", "stale_after_hours")  # what a line of [messages] can say besides what its template line has


def _merge(known: dict[str, Any], given: dict[str, Any], where: tuple[str, ...], warnings: list[str]) -> dict[str, Any]:
    merged = dict(known)
    for key, value in given.items():
        here = (*where, key)
        if key not in known and len(where) == 2 and where[0] == "feed" and key in (shared := defaults()["feeds"]):
            if _same_kind(shared[key], value, key):  # a feed's block can change anything [feeds] sets for them all
                merged[key] = value
            else:
                warnings.append(f"{FILE}: {_shown(where)}{key} should be {_kind(shared[key])}; its default is used")
            continue
        if key not in known:
            if where in FREE_FORM or here in FREE_FORM or (len(where) == 2 and where[0] == "messages" and key in LINE_EXTRAS):
                merged[key] = value
            elif where[:1] == ("messages",) and len(where) == 1 and isinstance(value, dict):
                merged[key] = value  # a line of a message this version doesn't know: a later version's, no harm
            elif (where, key) in MOVED_KEYS:
                home, new_key = MOVED_KEYS[(where, key)]
                warnings.append(f"{FILE}: {_shown(where)}{key} is {_shown(home)}{new_key} now, so it's ignored")
            elif len(where) == 2 and where[0] == "messages" and key in LINE_FIELDS:
                warnings.append(f"{FILE}: {_shown(where)}{key} is {LINE_FIELDS[key]} now, so it's ignored")
            elif here == ("ranks", "owner"):
                warnings.append(f"{FILE}: [ranks] owner is ignored: owners are people, never a role; put their Discord "
                                "user IDs in [ranks] owners")
            else:
                near = difflib.get_close_matches(key, list(known), 1)
                warnings.append(f"{FILE}: {_shown(where)}{key} isn't a setting this version knows"
                                + (f" (did you mean {near[0]}?)" if near else "") + "; ignored")
            continue
        default = known[key]
        if isinstance(default, str) and isinstance(value, int) and not isinstance(value, bool):
            value = str(value)  # an ID written without quotes
        if not _same_kind(default, value, key):
            warnings.append(f"{FILE}: {_shown(where)}{key} should be {_kind(default)}; its default is used")
        elif isinstance(default, dict):
            merged[key] = _merge(default, value, here, warnings) if default and here not in OWN_ENTIRELY else value
        else:
            merged[key] = value
    return merged


def _apply_limits(data: dict[str, Any], warnings: list[str]) -> None:
    limits = data["limits"]
    for key, floor in FLOORS.items():
        if limits[key] < floor:
            warnings.append(f"{FILE}: [limits] {key} can't go under {floor}; {floor} is used")
            limits[key] = floor
    for key, ceiling in CEILINGS.items():
        if limits[key] > ceiling:
            warnings.append(f"{FILE}: [limits] {key} can't go over {ceiling}; {ceiling} is used")
            limits[key] = ceiling
    for key, line in data["messages"].items():
        if isinstance(line, dict) and line.get("colour") is not None and colour_value_of(data["colours"], line.get("colour")) is None:
            warnings.append(f"{FILE}: [messages] {key}: there's no colour '{line.get('colour')}': a name from [colours], or \"#RRGGBB\"")
    for parts, limit in TIMERS.items():
        table = data
        for part in parts[:-1]:
            table = table[part]
        if table[parts[-1]] < limits[limit]:
            warnings.append(f"{FILE}: {_shown(parts[:-1])}{parts[-1]} can't go under {limits[limit]} ([limits] {limit}); "
                            f"{limits[limit]} is used")
            table[parts[-1]] = limits[limit]


def colour_value_of(colours: dict[str, Any], name: Any) -> int | None:
    """A colour's number: a name from [colours], or "#RRGGBB". None when it's neither."""
    text = str(colours.get(str(name), name) or "").strip()
    match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", text)
    return int(match.group(1), 16) if match else None


def colour_value(config: DiscordConfig, name: Any) -> int:
    value = colour_value_of(config.data["colours"], name)
    return DEFAULT_COLOUR if value is None else value


def clean_token(text: str) -> str:
    """A token as people paste it: spaces and a "Bot " in front are no part of it."""
    token = text.strip().strip('"').strip()
    return token[4:].strip() if token.lower().startswith("bot ") else token


@dataclass
class DiscordConfig:
    path: Path | None = None
    data: dict[str, Any] = field(default_factory=defaults, repr=False)
    token: str = field(default="", repr=False)  # the bot's, wherever it came from
    error: str = ""  # why the file can't be used (the bot is off)
    warnings: list[str] = field(default_factory=list)
    digest: str = ""
    says_mode: bool = True  # whether the file names a mode. One that doesn't is empty, or being written, or gone

    @property
    def mode(self) -> str:
        return "off" if self.error else str(self.data["mode"])

    @property
    def guild(self) -> str:
        return str(self.data["guild"]).strip()

    def section(self, *parts: str) -> dict[str, Any]:
        """A table of the file, every default filled in."""
        table: Any = self.data
        for part in parts:
            table = table.get(part, {}) if isinstance(table, dict) else {}
        return table if isinstance(table, dict) else {}

    def limit(self, key: str) -> Any:
        return self.data["limits"][key]

    def feed(self, name: str) -> dict[str, Any]:
        """A feed's settings: [feeds] under its own [feed.<name>]."""
        return {**self.data["feeds"], **self.section("feed", name)}

    def channel_ref(self, value: Any) -> str:
        """What a feature's `channel` means: the name in [channels] ("staff"), or the channel itself."""
        ref = str(value or "").strip()
        ref = OLD_NAMES.get(ref, ref)
        named = self.data["channels"].get(ref)
        if named is None:
            return ref
        return str(named).strip() or (self.channel_ref(FALLBACKS[ref]) if ref in FALLBACKS else "")


# The lists whose items have to be names the code knows: one it doesn't is otherwise just quietly nothing.
BUTTONS = ("mute", "kick", "ban", "strike", "warn", "whois", "said", "note")
NAMES = {("tickets", "buttons"): BUTTONS, ("reports", "buttons"): BUTTONS, ("appeals", "buttons"): ("lift", "shorten", "deny", "close", "refuse"),
         ("reports", "from"): ("game", "discord"), ("appeals", "for"): ("ban", "kick", "mute"), ("commands", "power_buttons"): ("start", "stop", "restart", "restart-after-game", "restart-when-empty", "cancel", "updates"),
         ("status", "board", "show"): ("state", "map", "mode", "players", "next", "uptime", "playlist", "votes", "version"), ("feed", "moderation", "sources"): ("*", "!", "/", "vergil", "vote", "console", "relay", "window", "discord")}
# One thing out of a few. A wrong one is replaced with its default, so what reads it never has to wonder: and it can't be read as something it isn't, like a rank nobody has, or a rank everyone does.
STAFF_RANKS = ("moderator", "admin", "owner")
STAFF_PERMISSIONS = ("", "manage_messages", "kick_members", "ban_members", "moderate_members", "manage_guild", "manage_roles", "administrator")
CHOICES = {("tickets", "when_closed"): ("move", "stay", "thread"), ("tickets", "close_rank"): STAFF_RANKS, ("commands", "power_rank"): STAFF_RANKS, ("commands", "staff_permission"): STAFF_PERMISSIONS,
           ("ranks", "discord_administrators"): ("nothing", "moderator", "admin"), ("ranks", "owners_in_game"): ("owner", "admin"), ("feeds", "style"): ("card", "compact"), ("status", "board", "layout"): ("per-server", "one"), ("status", "board", "player_list"): ("names", "off"),
           ("status", "presence", "activity"): ("playing", "listening", "watching", "competing", "custom"), ("status", "presence", "dot"): ("health", "online"), ("console", "layout"): ("per-server", "picker"),
           ("console", "remember_server"): ("member", "channel"), ("console", "answers"): ("reply", "thread"), ("console", "long_answers"): ("file", "split"), ("console", "ignore_others"): ("ignore", "delete"), ("console", "audit"): ("changes", "everything"), ("console", "raw_rank"): ("moderator", "admin", "owner", "off"), ("appeals", "tell_outcome"): ("dm", "no"), ("reports", "tell_reporter"): ("acted", "all", "no")}
LINE_SHAPES = ("ticket", "log", "post")
PERSONAS_KNOWN = ("superintendent", "vergil", "huragok")


def _table(data: dict[str, Any], parts: tuple[str, ...]) -> dict[str, Any]:
    for part in parts:
        data = data.get(part, {}) if isinstance(data.get(part, {}), dict) else {}
    return data


def _check_names(data: dict[str, Any], warnings: list[str]) -> None:
    for (*table, key), known in NAMES.items():
        here = _table(data, tuple(table))
        wrong = [str(name) for name in here.get(key) or [] if str(name) not in known]
        if wrong:  # left in, it would mean something: a list of sources with a wrong one in it still leaves out everything else
            warnings.append(f"{FILE}: {_shown(tuple(table))}{key}: {', '.join(wrong)} isn't one of {', '.join(known)}, so it's ignored")
            here[key] = [name for name in here[key] if str(name) in known] or _table(defaults(), tuple(table))[key]
    made = defaults()
    for (*table, key), known in CHOICES.items():
        here = _table(data, tuple(table))
        if key in here and str(here[key]) not in known:
            usual = _table(made, tuple(table))[key]
            warnings.append(f"{FILE}: {_shown(tuple(table))}{key} can't be \"{here[key]}\": it's one of {', '.join(one or 'empty' for one in known)}. \"{usual}\" is used")
            here[key] = usual
    _check_the_rest(data, warnings, made)


def _check_the_rest(data: dict[str, Any], warnings: list[str], made: dict[str, Any]) -> None:
    """What needs more than a list: whole numbers in order, colours, who speaks, a Discord's ID, a line's own fields."""
    guild = str(data.get("guild") or "").strip()
    if guild and not guild.isdecimal():
        warnings.append(f"{FILE}: guild has to be a Discord's ID: use W in Huragok's window to pick it. What's there is ignored")  # it isn't said back: a token pasted a line too low would be in the log
        data["guild"] = ""
    seeding = data["status"]["seeding"]
    seed = seeding.get("seed_at")
    if not (isinstance(seed, list) and len(seed) == 2 and all(isinstance(one, int) and not isinstance(one, bool) for one in seed) and 0 <= seed[0] <= seed[1]):
        warnings.append(f"{FILE}: [status.seeding] seed_at has to be two whole numbers, lowest first, like [1, 3]; the usual is used")
        seeding["seed_at"] = made["status"]["seeding"]["seed_at"]
    colours = data["colours"]
    for where, values in ((("tickets", "colours"), data["tickets"].get("colours")), (("status", "board", "colours"), data["status"]["board"].get("colours")), (("notes", "flags"), data["notes"].get("flags"))):
        for name, value in (values or {}).items():
            if colour_value_of(colours, value) is None:
                warnings.append(f"{FILE}: {_shown(where)}{name}: there's no colour '{value}': a name from [colours], or \"#RRGGBB\"")
    for name, value in colours.items():
        if colour_value_of({}, value) is None:
            warnings.append(f"{FILE}: [colours] {name}: \"{value}\" isn't a colour: it should look like \"#RRGGBB\"")
    # `servers = "Server 1"`, written as the template's own "*" is: one server, as a list. Left a string, the board
    # sorted it letter by letter and crashed every tick (switching the bot off, again and again), and a feed matched
    # it as a part of a name (audit 2026-11, 6.2).
    for table in [data.get("feeds"), data["status"].get("board"), *(data.get("feed") or {}).values()]:
        if isinstance(table, dict) and isinstance(table.get("servers"), str) and table["servers"].strip() != "*":
            table["servers"] = [table["servers"].strip()] if table["servers"].strip() else "*"
    feeds = data.get("feed") or {}
    for name, feed in feeds.items():
        persona = feed.get("persona") if isinstance(feed, dict) else ""
        if persona and str(persona) not in PERSONAS_KNOWN:
            warnings.append(f"{FILE}: [feed.{name}] persona: \"{persona}\" isn't one of {', '.join(PERSONAS_KNOWN)}, so it's ignored")
            feed["persona"] = ""
    calls = feeds.get("calls")
    if isinstance(calls, dict) and any(not str(word).strip() for word in calls.get("words") or []):
        warnings.append(f"{FILE}: [feed.calls] words: an empty one would call on every line said, so it's left out")
        calls["words"] = [word for word in calls.get("words") or [] if str(word).strip()]
    scoped = data["ranks"].get("scoped") or []
    if not isinstance(scoped, list):
        warnings.append(f"{FILE}: [ranks] scoped should be blocks of [[ranks.scoped]], so it's ignored")
        data["ranks"]["scoped"] = scoped = []
    for block in scoped:
        if not isinstance(block, dict):
            warnings.append(f"{FILE}: [[ranks.scoped]] should be a block with role, rank and servers, so \"{block}\" is ignored")
            continue
        if str(block.get("rank", "")).strip().replace("_", "-") not in RANK_KEYS:
            warnings.append(f"{FILE}: [[ranks.scoped]]: rank \"{block.get('rank')}\" isn't a rank: one of {', '.join(RANK_KEYS)}")
        servers = block.get("servers")
        if isinstance(servers, str):
            block["servers"] = [servers]
        elif not isinstance(servers, list):
            warnings.append(f"{FILE}: [[ranks.scoped]]: servers should be a list of their names, so that block counts on no server")
            block["servers"] = []
    known_lines = made["messages"]
    for key, line in data["messages"].items():
        if isinstance(line, dict):
            if str(line.get("as") or "") not in ("", *LINE_SHAPES):
                warnings.append(f"{FILE}: [messages] {key}: as is one of {', '.join(LINE_SHAPES)}, so \"{line.get('as')}\" is ignored")
            for field in ("stale_after_hours", "remove_after_minutes"):  # every minute, or every post of it, these get worked out: a word here mustn't stop that
                if field in line and (isinstance(line[field], bool) or not isinstance(line[field], (int, float))):
                    warnings.append(f"{FILE}: [messages] {key}: {field} is a number, so \"{line[field]}\" is ignored")
                    del line[field]
            if "closes_to" in line and not isinstance(line["closes_to"], str):
                line["closes_to"] = str(line["closes_to"])
            if key not in known_lines and (near := difflib.get_close_matches(key, list(known_lines), 1)):
                warnings.append(f"{FILE}: [messages] {key} isn't a message this version has (did you mean {near[0]}?)")
    power = data["commands"].get("hidden") or []
    if any(str(name).startswith(("!", "/")) for name in power):
        warnings.append(f"{FILE}: [commands] hidden: name a command without its ! or /")


def unknown_texts(given: dict[str, Any]) -> list[str]:
    """[text] keys that reword nothing."""
    from .speakers import TEXTS

    return [key for key in given if key not in TEXTS and key not in MOVED_TEXTS]


def _flat(table: dict[str, Any], before: str = "") -> dict[str, Any]:
    """[text] with its keys whole: ticket.closed = "x" unquoted is a table inside a table to TOML, and means just what "ticket.closed" = "x" does."""
    flat: dict[str, Any] = {}
    for key, value in table.items():
        if isinstance(value, dict):
            flat.update(_flat(value, f"{before}{key}."))
        else:
            flat[f"{before}{key}"] = value
    return flat


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def read(path: Path) -> DiscordConfig:
    """discord.toml as it is now. Raises DiscordFileError when it can't be read: the caller decides what that means
    (load() below, or keep what's in force)."""
    config = DiscordConfig(path=path, digest=_digest(path))
    if not path.exists():
        config.says_mode = False
        return config
    try:
        given = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise DiscordFileError(f"{path.name} can't be read: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise DiscordFileError(f"{path.name} isn't valid TOML: {error}") from None
    config.says_mode = "mode" in given
    config.data = _merge(defaults(), given, (), config.warnings)
    config.data["text"] = _flat(config.data["text"])
    config.warnings += [f"{FILE}: [text] {key} isn't a text of this version; it does nothing" for key in unknown_texts(config.data["text"])]
    _apply_limits(config.data, config.warnings)
    _check_names(config.data, config.warnings)
    if config.data["mode"] not in MODES:
        raise DiscordFileError(f"{path.name}: mode can be {', '.join(MODES)}, and it's something else")
    if config.data["voice"] not in VOICES:
        config.warnings.append(f"{path.name}: voice is '{config.data['voice']}'; it can be {', '.join(VOICES)}; lore "
                               "is used")
        config.data["voice"] = "lore"
    config.token = clean_token(str(config.data["token"]))
    variable = str(config.data["token_variable"]).strip()
    if variable and not config.token:
        config.token = clean_token(os.environ.get(variable, ""))
        if not config.token:
            config.warnings.append(f"{path.name}: token_variable names a variable that isn't set" if len(variable) < 40 and "." not in variable else f"{path.name}: token_variable should be the name of a variable, and that doesn't look like one: did the token go a line too low?")
    return config


def load(path: Path) -> DiscordConfig:
    """For starting: a file that can't be read leaves the bot off and says why; the servers carry on."""
    try:
        return read(path)
    except DiscordFileError as error:
        return DiscordConfig(path=path, error=str(error), digest=_digest(path))


def reload_if_changed(current: DiscordConfig, *, force: bool = False) -> DiscordConfig | None:
    """The file again if it changed; DiscordFileError if it did and can't be used (what's in force stays)."""
    assert current.path is not None
    if not force and _digest(current.path) == current.digest:
        return None
    fresh = read(current.path)
    if current.mode != "off" and not fresh.says_mode:  # an empty file reads as every default, which is the bot off: and that's more often an editor halfway through saving than anyone's choice. Off is `mode = "off"`
        raise DiscordFileError(f"{current.path.name} is gone or doesn't say a mode, so what's in force stays. To switch the bot off: mode = \"off\"")
    return fresh


def _private(path: Path) -> None:
    """This file can hold a password, and webhook addresses that are as good as one: it's the host's alone."""
    if os.name != "nt" and path.exists():
        os.chmod(path, 0o600)


def make_file(path: Path) -> bool:
    """A new discord.toml, every feature off. False: there's one already."""
    if path.exists():
        return False
    write_atomic(path, template_text())
    _private(path)
    return True


# Changing the file: every door (the window, /huragok, a hand edit) ends in the same file, comments kept. ---------


def set_mode(path: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode can be {', '.join(MODES)}")
    make_file(path)
    set_top_key(path, "mode", mode)


def set_token(path: Path, token: str) -> None:
    make_file(path)
    _private(path)  # first, so it's never there to read with the token in it
    set_top_key(path, "token", clean_token(token))


def set_guild(path: Path, guild: str, name: str = "") -> None:
    make_file(path)
    set_top_key(path, "guild", guild, note=name)


def set_channel(path: Path, key: str, channel: str, name: str = "") -> None:
    """[channels] <key>: its ID, with its name as a note."""
    make_file(path)
    set_table_key(path, "channels", key, channel, note=f"#{name}" if name else "")


RANK_KEYS = {"member": "member", "probationary-moderator": "probationary_moderator", "moderator": "moderator",
             "admin": "admin"}


def set_rank_roles(path: Path, key: str, wanted: list[tuple[str, str]], *,
                   guild_roles: dict[str, str] | None = None) -> None:
    """[ranks] <key>: exactly these (role ID, name), each on its own line with the name as a note. Entries written by
    name are matched through `guild_roles` (ID -> name), so one wanted still is written once, as its ID."""
    make_file(path)
    current = read_toml(path).get("ranks", {}).get(key, [])
    names = {name.lower(): role for role, name in (guild_roles or {}).items()}
    keep = {role for role, _ in wanted}
    for entry in current if isinstance(current, list) else []:
        text = str(entry).strip()
        role = names.get(text.lstrip("@").lower(), text)
        if role not in keep or role != text:
            remove_from_list(path, "ranks", key, str(entry))
    for role, name in wanted:
        add_to_list(path, "ranks", key, role, f"@{name}" if name else "")


def add_owner(path: Path, user_id: str, note: str = "") -> bool:
    make_file(path)
    return add_to_list(path, "ranks", "owners", user_id, note)


def remove_owner(path: Path, user_id: str) -> bool:
    return remove_from_list(path, "ranks", "owners", user_id)


def set_switch(path: Path, section: tuple[str, ...], key: str, value: bool) -> None:
    """A feature's own on/off, in its own table."""
    make_file(path)
    if len(section) == 1:
        set_table_key(path, section[0], key, value)
    else:
        set_nested_key(path, section, key, value)


def set_webhook(path: Path, section: tuple[str, ...], url: str) -> None:
    make_file(path)
    _private(path)
    set_nested_key(path, section, "webhook", url.strip())


def set_message(path: Path, key: str, **changes: Any) -> None:
    """One line of [messages] changed; the rest of it as it was."""
    make_file(path)
    known = defaults()["messages"].get(key, {})
    line = {**known, **read_toml(path).get("messages", {}).get(key, {}), **changes}
    set_table_key(path, "messages", key, line)


def set_launcher_webhook(path: Path, url: str) -> None:
    make_file(path)
    _private(path)
    set_nested_key(path, ("feed", "alerts"), "launcher_webhook", url.strip())


def launcher_webhook(home: Path) -> str:
    """[feed.alerts] launcher_webhook, for the start loop (it has no bot, and must never fail over this)."""
    try:
        data = tomllib.loads((home / FILE).read_text(encoding="utf-8"))
        return str(data.get("feed", {}).get("alerts", {}).get("launcher_webhook") or "").strip()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, AttributeError):
        return ""
