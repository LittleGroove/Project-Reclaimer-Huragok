"""Quick actions (0.7.0, answers 7 and 33): small writes into a server's own dedicated.toml that the host chooses.

Huragok never changes a server's files by itself. Where a warning needs it (no console, ports taken, a folder
`dedicated init` just made with two servers in it), it offers a quick action, and each one is two things:

- what it would write (a QuickAction): the file, and every line it adds, takes out or changes, each with a note
  saying why, made on a copy of the file so nothing is touched while the host looks;
- writing it (write), once the host says yes: a version of the file is kept first (core/versions.py, so it can be put
  back), then exactly the text shown is written. A file changed since it was shown isn't written over.

The window asks (window/screens/setup.py, quick_action); `start.cmd new` and `start.cmd steam lines` ask the same
way. The quick actions:

  one_server      keep the first [[server]] block, named as the host named the folder; the others go
  free_ports      a free game port for each server, and the folder's master off or on a free port
  add_console     the console: [rcon] password (or the server's own rcon_password), random, 24 characters; its own
                  rcon_port only when its game port's number is taken on TCP
  steamcmd_lines  [workshop] steamcmd and username: this PC's SteamCMD and the I key's account

"Free" means free on this PC (servers/ports.py: the system's listening TCP sockets and bound UDP ones) and not used
by another server Huragok knows (any folder in SERVERS/, disabled and left-out servers too, and each folder's
master while it's on).
"""

from __future__ import annotations

import difflib
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..core import versions
from ..core.files import read_text, write_atomic
from ..core.serverfolders import slugify
from ..core.tomledit import read_toml, remove_array_block, set_server_setting, set_table_key
from ..program.settings import every_block
from . import newserver
from .newserver import FIRST_PORT, MAX_PORT, generate_password

if TYPE_CHECKING:
    from ..core.settings import Settings

log = logging.getLogger(__name__)

HISTORY = "settings"  # core/versions.py's kind: records/settings/<folder's slug>/dedicated.toml/<when>/
PASSWORD_LENGTH = 24
KEY_LINE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$")


class QuickActionError(Exception):
    """A quick action that can't be offered or written as it is; nothing was changed."""


@dataclass(frozen=True)
class Line:
    """One line of what a quick action writes: + added, - taken out, ~ changed (the line as it will be)."""

    sign: str
    text: str  # as it's shown (a password cut short)
    note: str = ""


@dataclass
class QuickAction:
    """What a quick action would write, worked out on a copy of the file: shown, then written when the host says yes."""

    key: str  # one-server, free-ports, console, steamcmd
    title: str  # "Add the console"
    path: Path  # the file it writes (a new folder's is still in SERVERS/.new-<slug>/)
    owner: str  # whose history keeps the versions: the folder's slug, as the host will name it
    where: str  # the file as the host knows it: SERVERS\LG Test 2\dedicated.toml
    why: list[str]  # said first: what it's for
    before: str  # the file's text when this was worked out
    after: str  # the text it writes
    notes: dict[str, str] = field(default_factory=dict)  # a written line's text -> its note
    shown: dict[str, str] = field(default_factory=dict)  # a written line's text -> how it's shown (a password)
    then: str = "The server reads it at its next start."
    nothing: str = ""  # why there's nothing to write, when there isn't
    ask: str = "Write it?"  # the question the host answers (y/N)

    @property
    def needed(self) -> bool:
        return self.before != self.after

    def lines(self) -> list[Line]:
        """Every line it adds, takes out or changes; a key whose value changes is one line (~, "was …")."""
        old, new = self.before.split("\n"), self.after.split("\n")
        found: list[Line] = []
        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            gone, added = old[i1:i2], new[j1:j2]
            if tag == "replace" and len(gone) == len(added):
                pairs = list(zip(gone, added))
                if all(_key(a) and _key(a) == _key(b) for a, b in pairs):
                    for was, now in pairs:
                        was_value = KEY_LINE.match(was).group(2)  # type: ignore[union-attr]
                        default = f"was {was_value}" if was_value not in ('""', "''") else "was empty"
                        found.append(Line("~", self._shown(now), self.notes.get(now, default)))
                    continue
            found += [Line("-", line.strip()) for line in gone if line.strip()]
            found += [Line("+", self._shown(line), self.notes.get(line, "")) for line in added if line.strip()]
        return found

    def _shown(self, line: str) -> str:
        return self.shown.get(line, line).strip()

    def preview(self) -> list[tuple[str, str | None]]:
        """The whole of it as the window and the command line show it (mockup 5): (text, colour) lines."""
        if not self.needed:
            return [(self.nothing or "Nothing to write: its file has this already.", "gray")]
        lines = self.lines()
        out: list[tuple[str, str | None]] = [(text, "white") for text in self.why]
        out.append((f"This writes {len(lines)} line(s) in {self.where}:", "white"))
        width = max(len(line.text) for line in lines) + 2
        colours = {"+": "green", "-": "red", "~": "yellow"}
        out += [(f"  {line.sign} {line.text:<{width}}{line.note}".rstrip(), colours[line.sign]) for line in lines]
        out.append(("A copy of the file is kept first (D → a server → F → History puts it back).", "gray"))
        out.append((self.then, "gray"))
        return out


def _key(line: str) -> str | None:
    match = KEY_LINE.match(line)
    return match.group(1) if match and not line.lstrip().startswith(("#", "[")) else None


def owner_of(path: Path) -> str:
    """Whose history keeps a dedicated.toml's versions: its folder's slug."""
    return slugify(path.parent.name)


def history(settings: Settings, path: Path) -> list[versions.Version]:
    """Every version of a server folder's dedicated.toml kept before and after a quick action, newest first (for D →
    a server → F → History)."""
    return versions.versions(settings.records, HISTORY, owner_of(path), path.name)


def write(settings: Settings, action: QuickAction) -> versions.Version | None:
    """The quick action written, as shown: a version of the file kept first (when it can't be, nothing is written),
    then the text shown, then that kept too. None when there was nothing to write. Raises QuickActionError (nothing
    written) when the file changed since it was shown, and OSError when it can't be read or written."""
    if not action.needed:
        return None
    if read_text(action.path) != action.before:
        raise QuickActionError(f"{action.where} changed since this was shown: nothing was written; look again")
    versions.keep_current(settings.records, HISTORY, action.owner, action.path)
    write_atomic(action.path, action.after)
    try:
        kept = versions.keep(settings.records, HISTORY, action.owner, action.path, f"quick action: {action.title}")
    except OSError as error:
        log.warning("%s: written, but the new version couldn't be kept in its history: %s", action.where, error)
        kept = None
    log.info("%s: %s, as the host chose (%d line(s))", action.where, action.title, len(action.lines()))
    return kept


# Working one out ---------------------------------------------------------------------------------------------------

def _worked_out(path: Path, edits: Callable[[Path], None]) -> tuple[str, str]:
    """(the file's text now, its text with the edits made): made on a copy, the file itself untouched. Raises
    QuickActionError for a file that can't be read or changed safely (tomledit's refusals)."""
    try:
        before = read_text(path)
    except (OSError, ValueError) as error:
        raise QuickActionError(f"{path} can't be read: {error}") from None
    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / path.name
        copy.write_text(before, encoding="utf-8")
        try:
            edits(copy)
        except OSError as error:  # a TomlEditError: written in a form tomledit won't guess at
            raise QuickActionError(str(error).replace(str(copy), str(path))) from None
        return before, read_text(copy)


def _blocks(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        data = read_toml(path)
    except (OSError, ValueError) as error:
        raise QuickActionError(f"{path} can't be read: {error}") from None
    blocks = data.get("server")
    return data, [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []


def _port(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _action(key: str, title: str, path: Path, where: str | None, owner: str | None) -> dict[str, Any]:
    return {"key": key, "title": title, "path": path, "where": where or str(path),
            "owner": owner or owner_of(path)}


# Ports -------------------------------------------------------------------------------------------------------------

def ports_in_use() -> set[int] | None:
    """Every port something on this PC holds now: listening on TCP, or bound on UDP (servers/ports.py, the system's own
    tables). None when the system won't say."""
    from ..servers.ports import bound_udp_ports, listening_ports

    tcp, udp = listening_ports(), bound_udp_ports()
    return None if tcp is None or udp is None else set(tcp) | set(udp)


def known_ports(settings: Settings, *, skip: Path | None = None) -> dict[int, str]:
    """Every port another server Huragok knows uses, with whose it is: game and console ports (disabled and left-out
    servers' too) and each folder's master while it's on. `skip`: a dedicated.toml whose own ports don't count."""
    skipped = skip.resolve() if skip is not None else None
    found: dict[int, str] = {}
    for server in settings.servers:
        if server.config_path.resolve() != skipped:
            for port in (server.game_port, server.rcon_port):
                if port:
                    found.setdefault(port, server.name)
    files: dict[Path, dict[str, Any]] = {}
    for folder, block, data in every_block(settings):
        config = (folder / settings.config_name).resolve()
        if config == skipped:
            continue
        files[folder] = data
        for port in (_port(block.get("port")), _port(block.get("rcon_port"))):
            if port:
                found.setdefault(port, str(block.get("name") or folder.name))
    for folder, data in files.items():
        master = data.get("master")
        if isinstance(master, dict) and master.get("enabled", True) is not False and _port(master.get("port")):
            found.setdefault(master["port"], f"{folder.name}'s master")
    return found


@dataclass
class Ports:
    """What's taken where a quick action looks for ports: on this PC and by the servers Huragok knows."""

    known: dict[int, str]  # port -> whose (another server's, a master's)
    in_use: set[int] | None  # every port held on this PC now; None: the system won't say (each is probed instead)
    picked: set[int] = field(default_factory=set)  # given out by this quick action already

    def taken(self, port: int | None) -> str | None:
        """Why a port can't be had, or None: "used by LG Forge", "in use on this PC"."""
        if port is None:
            return "none set"
        if port in self.picked:
            return "given to another server here"
        if port in self.known:
            return f"used by {self.known[port]}"
        held = port in self.in_use if self.in_use is not None else not newserver.port_free(port)
        return "in use on this PC" if held else None

    def free(self, start: int = FIRST_PORT) -> int:
        port = start
        while port <= MAX_PORT:
            if self.taken(port) is None:
                self.picked.add(port)
                return port
            port += 1
        raise QuickActionError(f"no free port from {start} to {MAX_PORT}")

    def keep(self, port: int) -> None:
        self.picked.add(port)


def look_at_ports(settings: Settings, path: Path, in_use: set[int] | None = None) -> Ports:
    """The ports taken, for a quick action on the dedicated.toml at `path` (its own don't count)."""
    return Ports(known_ports(settings, skip=path), ports_in_use() if in_use is None else in_use)


def taken_ports(settings: Settings, path: Path, ports: list[int],
                in_use: set[int] | None = None) -> list[tuple[int, str]]:
    """Which of a folder's ports are taken, each with whose it is when Huragok knows ("" when it's something else on
    this PC): [(49176, "LG Forge"), (49175, "")]."""
    looked = look_at_ports(settings, path, in_use)
    return [(port, looked.known.get(port, "")) for port in ports if looked.taken(port) is not None]


# The quick actions -------------------------------------------------------------------------------------------------

def one_server(path: Path, name: str, *, where: str | None = None, owner: str | None = None) -> QuickAction:
    """Keep the first [[server]] block, named `name` (the folder's name, as the host gave it); every other goes."""
    _, blocks = _blocks(path)
    if not blocks:
        raise QuickActionError(f"{where or path} has no [[server]] blocks")
    names = [str(b.get("name") or "") for b in blocks]
    if len({n.lower() for n in names}) != len(names):
        raise QuickActionError(f"two [[server]] blocks in {where or path} have the same name: change it by hand")
    first, others = names[0], names[1:]

    def edits(copy: Path) -> None:
        for other in others:
            remove_array_block(copy, ("server",), other)
        if first != name:
            set_server_setting(copy, first, "name", name, add=False)

    before, after = _worked_out(path, edits)
    gone = ", ".join(f'"{n}"' for n in others)
    why = [f"It holds {len(blocks)} servers; this keeps the first as \"{name}\"" + (f" and takes out {gone}." if others
                                                                                  else ".")]
    action = QuickAction(**_action("one-server", "One server only", path, where, owner), why=why, before=before,
                         after=after, notes={f"name = {_quoted(name)}": f"was \"{first}\""} if first != name else {},
                         nothing="It's one server already, named as its folder.", ask="Keep one server?")
    return action


def free_ports(settings: Settings, path: Path, *, master: str = "off", where: str | None = None,
               owner: str | None = None, in_use: set[int] | None = None) -> QuickAction:
    """A free game port for each server whose own is taken (on this PC, or by another server Huragok knows), and the
    folder's master `master`: "off" (enabled = false; the project's public masters still list its servers) or "free"
    (kept on, on a free port when its own is taken)."""
    if master not in ("off", "free"):
        raise ValueError(f"master is 'off' or 'free', not {master!r}")
    data, blocks = _blocks(path)
    ports = look_at_ports(settings, path, in_use)
    changes: list[tuple[str, int | None, int, str]] = []  # (server, its port, the new one, why)
    for block in blocks:
        port, why = _port(block.get("port")), None
        why = ports.taken(port)
        if why is None and port is not None:
            ports.keep(port)
            continue
        changes.append((str(block.get("name") or ""), port, ports.free(), why or "none set"))
    table = data.get("master") if isinstance(data.get("master"), dict) else None
    master_on = table is not None and table.get("enabled", True) is not False
    master_port = _port(table.get("port")) if table is not None else None
    new_master: int | None = None
    if master_on and master == "free" and (taken := ports.taken(master_port)) is not None:
        new_master = ports.free()
    notes: dict[str, str] = {}

    def edits(copy: Path) -> None:
        for name, _, new, _ in changes:
            set_server_setting(copy, name, "port", new, add=False)
        if master_on and master == "off":
            set_table_key(copy, "master", "enabled", False)
        elif new_master is not None:
            set_table_key(copy, "master", "port", new_master)

    for name, old, new, why in changes:
        notes[f"port = {new}"] = f"free on this PC: {name}'s game port (and its console's); {old or 'none'} was {why}"
    why = ["Each server needs a game port of its own (UDP, and TCP for its console), free on this PC."]
    if master_on and master == "off":
        notes["enabled = false"] = (f"its own master (port {master_port}) off: the project's public masters still list "
                                    "its servers")
        why.append("Its master is only needed when this PC lists servers for players on its network; one master per "
                   "PC does it.")
    elif new_master is not None:
        notes[f"port = {new_master}"] = f"free on this PC: its master's (was {master_port}, {taken})"
    before, after = _worked_out(path, edits)
    return QuickAction(**_action("free-ports", "Free ports", path, where, owner), why=why, before=before, after=after,
                       notes=notes, then="The server takes its new port at its next start.", ask="Change them?",
                       nothing="Its ports are free on this PC already" + (", and its master is off." if not master_on
                                                                          else "."))


def add_console(settings: Settings, path: Path, server: str | None = None, *, where: str | None = None,
                owner: str | None = None, in_use: set[int] | None = None, password: str | None = None) -> QuickAction:
    """The console (Source RCON) for `server` (None: every server in the file without one): a random password of
    PASSWORD_LENGTH characters under [rcon], for every server in the folder, when none of them has a password of its
    own; else in the server's own block (rcon_password). The console listens on TCP at its game port's number; when
    that's taken, its own rcon_port, free on this PC."""
    data, blocks = _blocks(path)
    if server is not None:
        blocks_for = [b for b in blocks if str(b.get("name") or "").lower() == server.lower()]
        if not blocks_for:
            raise QuickActionError(f"{where or path} has no [[server]] block named {server} now")
    else:
        blocks_for = blocks
    rcon = data.get("rcon") if isinstance(data.get("rcon"), dict) else {}
    shared = str(rcon.get("password") or "")
    lacking = [] if shared else [b for b in blocks_for if not b.get("rcon_password")]
    own_ones = any(b.get("rcon_password") for b in blocks)
    password = password or generate_password(PASSWORD_LENGTH)
    ports = look_at_ports(settings, path, in_use)
    for block in blocks:  # the others' console ports stay theirs
        if all(block is not other for other in lacking) and (
                port := _port(block.get("rcon_port")) or _port(block.get("port"))):
            ports.keep(port)
    console_ports: list[tuple[str, int, int | None]] = []  # (server, the port it listens on, its own rcon_port)
    for block in lacking:
        name = str(block.get("name") or "")
        port = _port(block.get("rcon_port")) or _port(block.get("port"))
        if port is not None and ports.taken(port) is None:
            ports.keep(port)
            console_ports.append((name, port, None))
        else:
            new = ports.free()
            console_ports.append((name, new, new))

    def edits(copy: Path) -> None:
        if not lacking:
            return
        if not own_ones:
            set_table_key(copy, "rcon", "password", password)
        else:
            for block in lacking:
                set_server_setting(copy, str(block.get("name") or ""), "rcon_password", password, add=False)
        for name, _, own in console_ports:
            if own is not None:
                set_server_setting(copy, name, "rcon_port", own, add=False)

    before, after = _worked_out(path, edits)
    key = "password" if not own_ones else "rcon_password"
    line = f"{key} = {_quoted(password)}"
    every = "every server in the folder" if not own_ones and len(blocks) > 1 else None
    notes = {line: f"random, {len(password)} characters, shown in full in the file"
                   + (f"; the console for {every}" if every else "")}
    for name, port, own in console_ports:
        if own is not None:
            notes[f"rcon_port = {own}"] = f"free on this PC: {name}'s console (its game port's number is taken)"
    listens = "; ".join(f"{name}'s on TCP {port}" + (" (its game port's number)" if own is None else "")
                        for name, port, own in console_ports)
    why = ["Without its console Huragok can't see players, warn them or moderate.",
           f"Its console listens on this PC only ([rcon] address), {listens}, free on this PC." if listens else ""]
    action = QuickAction(**_action("console", "Add the console", path, where, owner), why=[w for w in why if w],
                         before=before, after=after, notes=notes, shown={line: f"{key} = \"{password[:4]}…\""},
                         nothing="It has its console already ([rcon] password, or its own rcon_password).")
    action.ask = "Add them?" if len(action.lines()) > 1 else "Add it?"
    return action


def steamcmd_lines(settings: Settings, path: Path, *, executable: Path | None = None, username: str | None = None,
                   where: str | None = None, owner: str | None = None) -> QuickAction:
    """[workshop] steamcmd and username for one server folder: this PC's SteamCMD and the I key's account ([setup]
    steamcmd and steam_username), so its Workshop items come through SteamCMD signed in as an account that owns MCC.
    Raises QuickActionError when SteamCMD isn't set up here."""
    from .. import steam

    if executable is None:
        executable, _ = steam.find_steamcmd(settings.home, settings.setup.steamcmd)
    if executable is None:
        raise QuickActionError("SteamCMD isn't set up on this PC: I → 1 sets it up first")
    username = settings.setup.steam_username if username is None else username

    def edits(copy: Path) -> None:
        set_table_key(copy, "workshop", "steamcmd", str(executable), before="master")
        if username:
            set_table_key(copy, "workshop", "username", username, before="master")

    before, after = _worked_out(path, edits)
    notes = {f"steamcmd = {_quoted(str(executable))}": "SteamCMD on this PC",
             f"username = {_quoted(username)}": "the account I signed in (it owns MCC; never a password)"}
    why = ["The server fetches its Workshop items with SteamCMD, signed in as an account that owns MCC (an anonymous "
           "sign-in can't fetch MCC's items)."]
    return QuickAction(**_action("steamcmd", "SteamCMD lines", path, where, owner), why=why, before=before, after=after,
                       notes=notes, then="The server reads [workshop] when its `dedicated` restarts (R).",
                       nothing="Its [workshop] lines name this PC's SteamCMD and account already.",
                       ask="Write them?")


def _quoted(text: str) -> str:
    from ..core.tomledit import _toml_value

    return _toml_value(text)


__all__ = ["HISTORY", "PASSWORD_LENGTH", "Line", "Ports", "QuickAction", "QuickActionError", "add_console",
           "free_ports", "history", "known_ports", "look_at_ports", "one_server", "owner_of", "ports_in_use",
           "steamcmd_lines", "taken_ports", "write"]
