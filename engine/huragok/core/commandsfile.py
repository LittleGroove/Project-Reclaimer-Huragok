"""Who's who, and who may use which in-game command: commands.toml.

    prefix = "!"
    cooldown_seconds = 3

    [everyone]
    commands = ["help", "strikes"]

    [member]                   # no commands of their own yet; no vote can kick them
    players = ["<player ID>"]
    commands = []

    [moderator]
    players = ["<player ID>"]
    commands = ["players", "mute", "kick"]

    [admin]                    # the server's own admins; no players list here
    commands = ["ban", "map"]

    [owner]
    players = ["<player ID>"]
    commands = ["admin", "reload"]

    [off]
    commands = ["say"]

Each command sits in the list of the lowest role that may use it; every role
below it in the file can use it too. A command in no list keeps its default
role. Roles follow player IDs, never names: anyone can use any name. Nobody
from member up can be vote-kicked (players/commands/votekicks.py).
"""

from __future__ import annotations

import difflib
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .files import mtime
from .part import CommandSpec
from .text import STORED_ID



ROLES = ("player", "member", "moderator", "admin", "owner")  # lowest to highest


SECTIONS = {"everyone": "player", "member": "member", "moderator": "moderator", "admin": "admin", "owner": "owner"}


TITLES = {v: k for k, v in SECTIONS.items()}  # role -> its section: player -> everyone


LISTED = ("owner", "moderator", "member")  # the roles whose players are listed here; admins are the server's own
HELP = CommandSpec("help", "The commands you can use, or how to use one", "[command]")


class PermissionsError(Exception):
    pass


@dataclass
class Permissions:
    members: dict[str, set[str]] = field(default_factory=lambda: {role: set() for role in LISTED})
    roles: dict[str, str] = field(default_factory=dict)  # command -> lowest role allowed, where the file says
    off: set[str] = field(default_factory=set)
    prefix: str = "!"
    cooldown: float = 3.0
    path: Path | None = None
    mtime: float = 0.0
    digest: str = ""  # of the file as read: a change within one tick of the clock keeps the time but not this
    sections: set[str] = field(default_factory=set)  # the sections the file has
    unknown: list[str] = field(default_factory=list)  # commands listed that this version doesn't have, each said why

    def doubles(self, admins: Iterable[str] = ()) -> dict[str, list[str]]:
        """Players listed under more than one role (admins: the servers' own): the highest is the one that counts,
        and the rest only confuse. One role each is the rule (!promote and !demote keep it)."""
        found: dict[str, list[str]] = {}
        for role in ("owner", "admin", "moderator", "member"):
            for player_id in (set(admins) if role == "admin" else self.members.get(role, set())):
                found.setdefault(player_id.lower(), []).append(role)
        return {player_id: roles for player_id, roles in found.items() if len(roles) > 1}

    def role_for(self, spec: CommandSpec) -> str:
        return self.roles.get(spec.name, spec.role)

    def listed(self) -> set[str]:
        """Every player ID listed here, members, moderators and owners: nobody can vote-kick them."""
        return set().union(*self.members.values())


def all_commands() -> dict[str, CommandSpec]:
    """Every command the supervisor has, by name, whichever parts are switched on."""
    from .part import commands_of
    from ..parts import PARTS  # here, not at the top: the parts import this module

    found = {HELP.name: HELP}
    for cls in PARTS.values():
        for spec in commands_of(cls):
            found[spec.name] = spec
    return found


def _version() -> str:
    from .. import __version__

    return __version__


def _digest(path: Path) -> str:
    """What the file holds, in short. Windows moves file times in steps of about 16 ms, so two saves in one step (two
    !promote at once) leave the time as it was: the time alone would miss the second."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def load_permissions(path: Path, known: dict[str, CommandSpec] | None = None) -> Permissions:
    """Read commands.toml. A missing file means every command keeps its default and nobody is listed."""
    known = all_commands() if known is None else known
    result = Permissions(path=path, mtime=mtime(path), digest=_digest(path))
    if not path.exists():
        return result
    name = path.name
    try:
        with path.open("rb") as file:
            data: dict[str, Any] = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PermissionsError(f"{name}: {error}") from None

    if stray := sorted(set(data) - {"prefix", "cooldown_seconds", "off", *SECTIONS}):
        raise PermissionsError(f"{name}: [{stray[0]}] isn't a role; the roles are "
                               f"{', '.join(SECTIONS)}, and [off] for commands nobody can use")
    prefix = data.get("prefix", "!")
    if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or any(c.isspace() for c in prefix):
        raise PermissionsError(f"{name}: prefix: the game keeps lines starting with / for itself; use something "
                               "like !")
    cooldown = data.get("cooldown_seconds", 3)
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or cooldown < 0:
        raise PermissionsError(f"{name}: cooldown_seconds must be a number of seconds")
    result.prefix, result.cooldown = prefix, float(cooldown)
    result.sections = set(data)

    aliases = {alias: spec.name for spec in known.values() for alias in spec.aliases}
    listed_in: dict[str, str] = {}
    for section in (*SECTIONS, "off"):
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise PermissionsError(f"{name}: {section} must be a section, like [{section}]")
        if stray := sorted(set(table) - {"commands", "players"}):
            raise PermissionsError(f"[{section}] {stray[0]}: a role has players and commands")
        if "players" in table and section not in LISTED:
            if section == "admin":
                raise PermissionsError("[admin] players: admins are the server's own, so there's never a second "
                                       "list to keep in step. Add them to admins in dedicated.toml, or in game "
                                       "with !admin add")
            raise PermissionsError(f"[{section}] players: only [member], [moderator] and [owner] list players")
        if section in LISTED:
            result.members[section] = _player_ids(table.get("players", []), f"[{section}] players")
        for command in _strings(table.get("commands", []), f"[{section}] commands"):
            command = command.lower().removeprefix(prefix)
            if command in aliases:
                raise PermissionsError(f"[{section}] commands: {command} is short for {aliases[command]}; "
                                       f"list {aliases[command]}")
            if command not in known:
                # Left out, not refused: a newer Huragok's command stays listed when an update is put back (the
                # version before must read what the new one wrote), and a typo mustn't keep Huragok from starting.
                close = difflib.get_close_matches(command, known, n=1)
                result.unknown.append(f"{name} [{section}]: there's no {prefix}{command} in Huragok {_version()}, so "
                                      "that line does nothing" + (f" (did you mean {close[0]}?)" if close else
                                                                  " (from a newer version, perhaps)"))
                continue
            if section == "off":  # off wins, wherever else the command is listed
                result.off.add(command)
                continue
            if command in listed_in:
                raise PermissionsError(f"{command} is listed under both [{listed_in[command]}] and [{section}]; "
                                       "list each command once, under the lowest role that may use it")
            listed_in[command] = section
            result.roles[command] = SECTIONS[section]
    if "help" in result.off:
        raise PermissionsError("[off] help: !help can't be switched off; it only lists what each player may use")
    return result


def _strings(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PermissionsError(f"{where} must be a list of text, like [\"one\", \"two\"]")
    return [v.strip() for v in value if v.strip()]


def _player_ids(value: Any, where: str) -> set[str]:
    given = _strings(value, where)
    if bad := [v for v in given if not STORED_ID.fullmatch(v.lower())]:
        raise PermissionsError(f"{where}: {bad[0]} is not a player ID (64 letters and digits; players find theirs "
                               "under Settings > Online services, and the console's `players` shows everyone's)")
    return {v.lower() for v in given}


def _blank(path: Path) -> bool:
    """Missing, or nothing but spaces: what some editors leave for a moment while they save."""
    try:
        return not path.read_bytes().strip()
    except FileNotFoundError:
        return True
    except OSError:
        return False  # can't be read (locked): load_permissions says so


def reload_if_changed(current: Permissions, *, force: bool = False) -> Permissions | None:
    """A fresh copy when the file changed since `current` was read (its time, or else what it holds), or always
    with force (Huragok just wrote it: !promote), else None. Raises PermissionsError.

    Only a file that reads replaces what's in force. One gone missing or empty while running is a save in
    progress (or a mistake), not "nobody is listed": it's reported once, the old roles stay, and the file
    is read again as soon as it changes."""
    if current.path is None:
        return None
    if not force and mtime(current.path) == current.mtime and _digest(current.path) == current.digest:
        return None
    if _blank(current.path) and (current.sections or any(current.members.values()) or current.roles or current.off):
        current.mtime, current.digest = mtime(current.path), _digest(current.path)
        raise PermissionsError(f"{current.path.name} is missing or empty (an editor saving it, perhaps); it's read "
                               "again once it has something in it")
    try:
        return load_permissions(current.path)
    except PermissionsError:
        current.mtime, current.digest = mtime(current.path), _digest(current.path)  # a broken edit: said once
        raise


# The file `init` writes ------------------------------------------------------------------

HEADER = """\
# Who can use which in-game command. Players type them in game chat:
#     !mute Bravo 30m spam
# Everyone in that chat sees the line, and the answer comes back privately.
# Edits apply within a few seconds; if one has a mistake, the log says what and
# the file from before stays in force.
#
# The roles, each able to use its own commands and those of every role above it:
#   everyone   every player
#   member     the players you list below: regulars, friends, clan. No commands
#              of their own yet; what they get is that no vote can kick them.
#   moderator  the players you list below. The game itself doesn't know about
#              moderators, so what they can do is exactly what you list here.
#   admin      the server's own admins: admins in dedicated.toml, or added in game.
#              They aren't listed here, so there's never a second list to keep in
#              step.
#   owner      the players you list below (you): everything.
#
# One role each, on a ladder: player, member, moderator, admin, owner. In game,
# !promote <player> moves someone one step up (up to the role below your own) and
# !demote one step down (only someone below you): moderators make members, admins
# make moderators, owners make admins. A !ban takes every role away. Each change
# gets a line of its own below, with who they are and who changed it; your own
# comments in the lists stay as you wrote them. Owners change only here.
#
# Players are listed by player ID, never by name (anyone can use any name). It's
# 64 letters and digits; players find theirs in game under Settings > Online
# services. `start.cmd rcon players` (Linux: ./start.sh rcon players) shows
# everyone's on the server.
#
# To change who can use a command, move it to another role's list. To switch it
# off, put it in [off]. A command in no list keeps the role it has here. Nobody
# can use a command on someone of their own role or above (owners excepted).
#
# No vote can kick a member, moderator, admin or owner: Vergil puts everyone
# listed here on each server's vote-kick immune list (the console's `immune`),
# and takes them off again when you take them out of this file.

prefix = "!"            # what commands start with ("/" lines are the game's own)
cooldown_seconds = 3    # between uses of a command, per player; admins and owners wait for nothing
"""

PLAYERS_EXAMPLE = '    # "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",  # who this is\n'


def entry_width(specs: Iterable[CommandSpec] | None = None) -> int:
    """How wide a command's entry is written (`"changebanword",` and the spaces after it), so their notes line up."""
    return max((len(s.name) for s in (all_commands().values() if specs is None else specs)), default=0) + 3


def render_commands_toml(specs: Iterable[CommandSpec] | None = None) -> str:
    """commands.toml with every command under its default role, each with what it does."""
    specs = sorted(all_commands().values() if specs is None else specs, key=lambda s: s.name)
    width = entry_width(specs)
    out = [HEADER]
    for role in ROLES:
        section = TITLES[role]
        out.append(f"\n[{section}]\n")
        if section in LISTED:
            out.append(f"players = [\n{PLAYERS_EXAMPLE}]\n")
        mine = [s for s in specs if s.role == role]
        if not mine:
            out.append("commands = []\n")
            continue
        out.append("commands = [\n")
        for spec in mine:
            entry = f'"{spec.name}",'
            out.append(f"    {entry:<{width}} # {spec.help}\n" if spec.help else f"    {entry}\n")
        out.append("]\n")
    out.append("\n[off]\ncommands = []\n")
    return "".join(out)


# Changing commands.toml from the window (D) ----------------------------------------------------------------------

ENTRY = re.compile(r"""^\s*["']([0-9A-Fa-f]{64})["']\s*,?\s*(?:#\s*(?P<note>.*?))?\s*$""")


def player_notes(path: Path) -> dict[str, str]:
    """Each listed player ID's note, as written beside it (who they are, who changed it); "" without one."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    return {m.group(1).lower(): m.group("note") or "" for m in (ENTRY.match(line) for line in lines) if m}


def _checked(path: Path, change: Any, what: str) -> None:
    """Make the change on a copy beside the file, read the copy as Huragok reads commands.toml, and only then put
    it in place (in one step). A PermissionsError says why it wasn't, and the file is as it was."""
    import os
    import shutil

    copy = path.with_name(f".{path.name}.{os.getpid()}.check")
    try:
        shutil.copy2(path, copy)
        try:
            change(copy)
        except OSError as error:  # tomledit's refusal names the file it was given: name the real one
            raise type(error)(str(error).replace(copy.name, path.name)) from None
        try:
            load_permissions(copy)
        except PermissionsError as error:
            raise PermissionsError(f"not changed ({what}): {str(error).replace(copy.name, path.name)}") from None
        os.replace(copy, path)
    finally:
        copy.unlink(missing_ok=True)


def make_commands_toml(path: Path) -> None:
    """commands.toml as `init` writes it: every command under its default role (for a folder without one)."""
    path.write_text(render_commands_toml(), encoding="utf-8")


def add_player(path: Path, role: str, player_id: str, note: str) -> list[str]:
    """List a player under member, moderator or owner, out of any other role listed here (one role each). The
    roles they were taken out of."""
    from .tomledit import add_to_list, remove_from_list

    given = player_id.strip()
    player_id = given.lower()
    if role not in LISTED:
        raise PermissionsError(f"only {', '.join(LISTED)} list players here (admins are the servers' own)")
    if not STORED_ID.fullmatch(player_id):
        raise PermissionsError(f"{given[:20]} is not a player ID (64 letters and digits; players find theirs "
                               "under Settings > Online services)")
    members = load_permissions(path).members
    moved = [other for other in LISTED if other != role and player_id in members.get(other, set())]

    def change(copy: Path) -> None:
        for other in moved:
            remove_from_list(copy, other, "players", player_id)
        add_to_list(copy, role, "players", player_id, note)

    _checked(path, change, f"{player_id[:8]} as {role}")
    return moved


def remove_player(path: Path, role: str, player_id: str) -> bool:
    """Take a player out of a role's list. False: they weren't in it."""
    from .tomledit import remove_from_list

    removed: list[bool] = []
    _checked(path, lambda copy: removed.append(remove_from_list(copy, role, "players", player_id.lower())),
             f"{player_id[:8]} out of {role}")
    return bool(removed and removed[0])


def _listed_as(path: Path, section: str, command: str, prefix: str) -> list[str]:
    """How a command is written in a section's list ("mute", "!mute"), each way it is."""
    from .tomledit import read_toml

    table = read_toml(path).get(section, {})
    values = table.get("commands", []) if isinstance(table, dict) else []
    return [v for v in values if isinstance(v, str) and v.strip().lower().removeprefix(prefix) == command]


def set_command_role(path: Path, command: str, role: str) -> None:
    """The lowest role that may use a command: its line moved to that role's list (with what it does). Whether
    it's switched off ([off]) stays as it was."""
    from .tomledit import add_to_list, remove_from_list

    spec = all_commands().get(command)
    if spec is None:
        raise PermissionsError(f"there's no command {command}")
    if role not in ROLES:
        raise PermissionsError(f"the roles are {', '.join(ROLES)}")
    prefix = load_permissions(path).prefix

    def change(copy: Path) -> None:
        for section in SECTIONS:
            for written in _listed_as(copy, section, command, prefix):
                remove_from_list(copy, section, "commands", written)
        add_to_list(copy, TITLES[role], "commands", command, spec.help, width=entry_width())

    _checked(path, change, f"{command} for {role}")


def set_command_off(path: Path, command: str, off: bool) -> None:
    """Switch a command off ([off]: nobody can use it) or on again (out of [off]: its role's own)."""
    from .tomledit import add_to_list, remove_from_list

    if command not in all_commands():
        raise PermissionsError(f"there's no command {command}")
    prefix = load_permissions(path).prefix

    def change(copy: Path) -> None:
        if off:
            add_to_list(copy, "off", "commands", command)
            return
        for written in _listed_as(copy, "off", command, prefix):
            remove_from_list(copy, "off", "commands", written)

    _checked(path, change, f"{command} {'off' if off else 'on'}")


def set_top(path: Path, key: str, value: Any) -> None:
    """prefix or cooldown_seconds."""
    from .tomledit import set_top_key

    _checked(path, lambda copy: set_top_key(copy, key, value), f"{key} = {value}")
