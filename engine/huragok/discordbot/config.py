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
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from ..core.files import write_atomic
from ..core.tomledit import add_to_list, read_toml, remove_from_list, set_nested_key, set_table_key, set_top_key

FILE = "discord.toml"
TEMPLATE = "templates/discord.toml"
MODES = ("off", "webhook", "bot")
VOICES = ("lore", "plain")

# Tables whose keys are the host's own, on top of those the template writes as {} (per_server, names…).
FREE_FORM = {("text",), ("ranks", "scoped"), ("leaderboards", "board")}

# What no [limits] value goes under (or, for CEILINGS, over): Discord's limits, in the design's table.
FLOORS = {"status_seconds": 60, "rename_minutes": 5, "voice_status_minutes": 1, "presence_seconds": 30,
          "profile_minutes": 15, "roles_check_minutes": 5, "boards_minutes": 5, "chat_batch_seconds": 2,
          "wait_seconds": 5}
CEILINGS = {"roles_per_second": 1, "chat_per_minute": 30, "logins_per_day": 900, "wait_seconds": 60}
# Each feature's own timer, and the [limits] value it can't go under.
TIMERS = {("status", "board", "update_seconds"): "status_seconds",
          ("status", "presence", "rotate_seconds"): "presence_seconds",
          ("status", "bio", "update_minutes"): "profile_minutes",
          ("status", "nickname", "update_minutes"): "profile_minutes",
          ("status", "channel_names", "update_minutes"): "rename_minutes",
          ("status", "topics", "update_minutes"): "rename_minutes",
          ("status", "voice_status", "update_minutes"): "voice_status_minutes",
          ("roles", "check_minutes"): "roles_check_minutes",
          ("leaderboards", "update_minutes"): "boards_minutes",
          ("chat", "batch_seconds"): "chat_batch_seconds"}


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


def _same_kind(default: Any, value: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


def _kind(value: Any) -> str:
    return ("true or false" if isinstance(value, bool) else "a number" if isinstance(value, (int, float))
            else "text" if isinstance(value, str) else "a list" if isinstance(value, list) else "a table")


def _merge(known: dict[str, Any], given: dict[str, Any], where: tuple[str, ...], warnings: list[str]) -> dict[str, Any]:
    merged = dict(known)
    for key, value in given.items():
        here = (*where, key)
        if key not in known and len(where) == 2 and where[0] == "feed" and key in (shared := defaults()["feeds"]):
            if _same_kind(shared[key], value):  # a feed's block can change anything [feeds] sets for them all
                merged[key] = value
            else:
                warnings.append(f"{FILE}: {_shown(where)}{key} should be {_kind(shared[key])}; its default is used")
            continue
        if key not in known:
            if where in FREE_FORM or here in FREE_FORM:
                merged[key] = value
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
        if not _same_kind(default, value):
            warnings.append(f"{FILE}: {_shown(where)}{key} should be {_kind(default)}; its default is used")
        elif isinstance(default, dict):
            merged[key] = _merge(default, value, here, warnings) if default else value
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
    for parts, limit in TIMERS.items():
        table = data
        for part in parts[:-1]:
            table = table[part]
        if table[parts[-1]] < limits[limit]:
            warnings.append(f"{FILE}: {_shown(parts[:-1])}{parts[-1]} can't go under {limits[limit]} ([limits] {limit}); "
                            f"{limits[limit]} is used")
            table[parts[-1]] = limits[limit]


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
        named = self.data["channels"].get(ref)
        return str(named).strip() if named is not None else ref


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
        return config
    try:
        given = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise DiscordFileError(f"{path.name} can't be read: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise DiscordFileError(f"{path.name} isn't valid TOML: {error}") from None
    config.data = _merge(defaults(), given, (), config.warnings)
    _apply_limits(config.data, config.warnings)
    if config.data["mode"] not in MODES:
        raise DiscordFileError(f"{path.name}: mode is '{config.data['mode']}'; it can be {', '.join(MODES)}")
    if config.data["voice"] not in VOICES:
        config.warnings.append(f"{path.name}: voice is '{config.data['voice']}'; it can be {', '.join(VOICES)}; lore "
                               "is used")
        config.data["voice"] = "lore"
    config.token = clean_token(str(config.data["token"]))
    variable = str(config.data["token_variable"]).strip()
    if variable and not config.token:
        config.token = clean_token(os.environ.get(variable, ""))
        if not config.token:
            config.warnings.append(f"{path.name}: token_variable names {variable}, which isn't set")
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
    return read(current.path)


def make_file(path: Path) -> bool:
    """A new discord.toml, every feature off. False: there's one already."""
    if path.exists():
        return False
    write_atomic(path, template_text())
    return True


# Changing the file: every door (the window, /huragok, a hand edit) ends in the same file, comments kept. ---------


def set_mode(path: Path, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode can be {', '.join(MODES)}")
    make_file(path)
    set_top_key(path, "mode", mode)


def set_token(path: Path, token: str) -> None:
    make_file(path)
    set_top_key(path, "token", clean_token(token))
    if os.name != "nt":  # it's a password now: this file is the host's alone
        os.chmod(path, 0o600)


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


def set_launcher_webhook(path: Path, url: str) -> None:
    make_file(path)
    set_nested_key(path, ("feed", "alerts"), "launcher_webhook", url.strip())


def launcher_webhook(home: Path) -> str:
    """[feed.alerts] launcher_webhook, for the start loop (it has no bot, and must never fail over this)."""
    try:
        data = tomllib.loads((home / FILE).read_text(encoding="utf-8"))
        return str(data.get("feed", {}).get("alerts", {}).get("launcher_webhook") or "").strip()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, AttributeError):
        return ""
