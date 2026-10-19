"""In-game commands: players type !name in game chat; who may use what is commands.toml.

- The game handles every line starting with / itself, so ours start with ! (any
  prefix but / can be set). Those lines are ordinary chat: everyone in the channel
  sees them. Replies go back privately with `tell`.
- Every part's commands share one set of names; a test checks they never clash.
- !yes and !no are the server's own votes and never arrive as chat.
- Who may run what comes from roles tied to player IDs, never names: anyone can
  use any name. The permissions are read on every line, so edits to
  commands.toml apply while running.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .events import Event, EventBus
from .part import CommandSpec
from .permissions import HELP, ROLES, TITLES, Roles
from .rcon import RconError
from .state import Player, ServerState

if TYPE_CHECKING:
    from .config import Settings
    from .context import PlayerHistory
    from .rcon import RconClient
    from .runner import PartRunner

log = logging.getLogger(__name__)

RESERVED = {"yes", "no"}  # the server counts these as votes
WORD = re.compile(r"[a-z][a-z0-9_-]{0,31}")
LINE_LIMIT = 120  # characters per private line; longer replies are split
NUMBER = re.compile(r"#?(\d{1,4})")  # a player's #number; the number alone means it too, as on the console
FULL_ID = re.compile(r"[0-9a-fA-F]{64}")
ID_START = re.compile(r"[0-9a-fA-F]{8,63}")  # the start of a player ID: !players shows the first 8


def parse_command(text: str, prefix: str) -> tuple[str, str] | None:
    """'!mute Bravo 30m' -> ('mute', 'Bravo 30m'), or None if it isn't a command."""
    if not text.startswith(prefix):
        return None
    word, _, remainder = text[len(prefix):].partition(" ")
    word = word.lower()
    if not WORD.fullmatch(word):
        return None
    return word, remainder.strip()


def split_words(text: str) -> list[str]:
    """Split like a shell, so "two words" stay together; tolerate a stray quote."""
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def wrap(text: str, limit: int = LINE_LIMIT) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        line = ""
        for word in paragraph.split(" "):
            if line and len(line) + 1 + len(word) > limit:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}" if line else word
        lines.append(line)
    return [l for l in lines if l.strip()]


def role_plural(role: str) -> str:
    """For players to read: 'moderators', 'everyone'."""
    return "everyone" if role == "player" else f"{role}s"


@dataclass
class CommandCall:
    """One use of a command: who, where, and the words after its name."""

    server: str
    player: Player
    role: str
    name: str  # as registered, even when an alias was typed
    args: list[str]
    text: str  # everything after the name, as typed
    channel: str
    state: ServerState
    _tell: Callable[[str, str, str], Awaitable[Any]] = field(repr=False)
    prefix: str = "!"

    async def reply(self, text: str) -> None:
        """Private lines to the caller. One the console can't deliver (slow, or away) is logged and the rest
        dropped: a reply that didn't arrive isn't the command failing, so it never counts against its part."""
        for line in wrap(text):
            try:
                await self._tell(self.server, self.player.id, line)
            except RconError as error:
                log.info("[%s] couldn't tell %s the reply to %s%s: %s", self.server, self.player.name, self.prefix,
                         self.name, error)
                return

    async def usage(self, spec: CommandSpec) -> None:
        await self.reply(f"Use: {self.prefix}{spec.name} {spec.usage}".rstrip())

    async def find_player(self, ref: str) -> Player | None:
        """The one connected player `ref` means, or None after telling the caller why."""
        matches = self.state.find(ref)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            await self.reply(f"No player matches '{ref}'. Use their #number from {self.prefix}players.")
        else:
            listed = ", ".join(f"#{p.number} {p.name}" for p in sorted(matches, key=lambda p: p.number)[:6])
            await self.reply(f"'{ref}' could be {listed}. Use the #number.")
        return None

    async def find_target(self, ref: str, *, history: PlayerHistory | None = None, offline: bool = False,
                          prefix: bool = False) -> Player | None:
        """The one player `ref` means, for a command that acts on them, or None after telling the caller why.

        In order: their #number (or the number alone), their player ID, their exact name on this server, a
        name `history` has seen them use, the first 8 or more characters of their ID. With `offline`, someone
        who isn't on comes back as a Player with number 0 (by ID); without, they're "not on now". The start
        of a name alone acts on nobody unless `prefix` is set: when the one meant has left, it lands on
        someone else (Bob left, Bobby is on), so the caller reads "did you mean Bobby (#4)?" instead.
        """
        ref = ref.strip()
        if not ref:
            await self.reply(f"Name a player: their #number from {self.prefix}players, their name or player ID.")
            return None
        number = NUMBER.fullmatch(ref)
        if number:
            player = self.state.players.get(int(number[1]))
            if player is not None:
                return player
            if ref.startswith("#"):
                await self.reply(f"Nobody is #{number[1]} on this server now. {self.prefix}players lists them.")
                return None
            # The number alone could still be someone's name.
        if FULL_ID.fullmatch(ref):
            player_id = ref.lower()
            if player := self.state.player_by_id(player_id):
                return player
            record = history.get(player_id) if history is not None else None
            if offline:
                return Player(0, record.last_name if record else player_id[:8], player_id)
            await self.reply(f"{record.last_name if record else player_id[:8]} isn't on this server now.")
            return None
        folded = ref.casefold()
        here = sorted((p for p in self.state.players.values() if p.name.casefold() == folded), key=lambda p: p.number)
        if len(here) == 1:
            return here[0]
        if here:
            await self.reply(f"'{ref}' could be {', '.join(f'#{p.number} {p.name}' for p in here[:6])}. "
                             "Use the #number.")
            return None
        records = history.find(ref) if history is not None else []
        seen = [r for r in records if any(n.casefold() == folded for n in [r.last_name, *r.names])]
        if not seen and ID_START.fullmatch(ref):
            start = ref.lower()
            on = [p for p in self.state.players.values() if p.id.startswith(start)]
            if len(on) == 1:
                return on[0]
            seen = [r for r in records if r.id.startswith(start)]
        if len(seen) == 1:
            record = seen[0]
            if player := self.state.player_by_id(record.id):
                return player
            if offline:
                return Player(0, record.last_name, record.id)
            await self.reply(f"{record.last_name} isn't on this server now.")
            return None
        if seen:
            await self.reply(f"'{ref}' could be " + ", ".join(f"{r.last_name} {r.id[:8]}" for r in seen[:5])
                             + ". Use the player ID.")
            return None
        starts = sorted((p for p in self.state.players.values() if p.name.casefold().startswith(folded)),
                        key=lambda p: p.number)
        if prefix and len(starts) == 1:
            return starts[0]
        if starts:
            listed = ", ".join(f"{p.name} (#{p.number})" for p in starts[:6])
            hint = f"#{starts[0].number}" if len(starts) == 1 else "the #number"
            await self.reply(f"Nobody here is called '{ref}'. Did you mean {listed}? Use {hint}.")
            return None
        if number:
            await self.reply(f"Nobody is #{number[1]} on this server now. {self.prefix}players lists them.")
        elif history is not None:
            await self.reply(f"Nobody called '{ref}' is on or has been seen. Use their #number or player ID.")
        else:
            await self.reply(f"No player matches '{ref}'. Use their #number from {self.prefix}players.")
        return None


@dataclass
class Registered:
    spec: CommandSpec
    runner: PartRunner | None  # None: the router's own (!help)
    method: Callable[[CommandCall], Awaitable[Any]]


class CommandRouter:
    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState],
                 clients: dict[str, RconClient], roles: Roles) -> None:
        self.settings = settings
        self.states = states
        self.clients = clients
        self.roles = roles
        self.commands: dict[str, Registered] = {}
        self.aliases: dict[str, str] = {}
        self._last_use: dict[tuple[str, str], float] = {}
        self.register(HELP, None, self._help)
        bus.subscribe("rcon.chat", self._on_chat)

    @property
    def prefix(self) -> str:
        return self.settings.permissions.prefix

    def register(self, spec: CommandSpec, runner: PartRunner | None,
                 method: Callable[[CommandCall], Awaitable[Any]]) -> None:
        owner = runner.name if runner else "Huragok"
        for word in (spec.name, *spec.aliases):
            if not WORD.fullmatch(word):
                raise ValueError(f"{owner}: '{word}' isn't a usable command name")
            if word in RESERVED:
                raise ValueError(f"{owner}: !{word} is the server's own vote")
            if word in self.commands or word in self.aliases:
                taken = self.commands.get(self.aliases.get(word, word))
                by = taken.runner.name if taken and taken.runner else "Huragok"
                raise ValueError(f"{owner}: !{word} is already {by}'s")
        if spec.role not in ROLES:
            raise ValueError(f"{owner}: !{spec.name} role must be one of {', '.join(ROLES)}")
        self.commands[spec.name] = Registered(spec, runner, method)
        for alias in spec.aliases:
            self.aliases[alias] = spec.name

    def unregister_part(self, runner: PartRunner) -> None:
        for name in [n for n, r in self.commands.items() if r.runner is runner]:
            spec = self.commands.pop(name).spec
            for alias in spec.aliases:
                self.aliases.pop(alias, None)

    def role_needed(self, registered: Registered) -> str:
        return self.settings.permissions.role_for(registered.spec)

    def _available(self, registered: Registered) -> bool:
        if registered.spec.name in self.settings.permissions.off:
            return False
        return registered.runner is None or registered.runner.enabled

    def _usable_by(self, registered: Registered, role: str) -> bool:
        return self._available(registered) and self.roles.allows(role, self.role_needed(registered))

    def parse(self, text: str) -> tuple[str, str] | None:
        """'!mute Bravo 30m' -> ('mute', 'Bravo 30m'), or None if it isn't a command."""
        return parse_command(text, self.prefix)

    async def _tell(self, server: str, player_id: str, text: str) -> None:
        await self.clients[server].command("tell", player_id, text)

    def _on_chat(self, event: Event) -> None:
        if event.get("channel") == "server" or not event.get("id"):
            return
        parsed = self.parse(str(event.get("text", "")))
        if parsed is None or event.server not in self.states:
            return
        word, rest = parsed
        state = self.states[event.server]
        player = state.player_by_id(str(event["id"])) or Player(0, str(event.get("name", "")), str(event["id"]).lower())
        call = CommandCall(event.server, player, self.roles.role_of(state, player), self.aliases.get(word, word),
                           split_words(rest), rest, str(event.get("channel", "")), state, self._tell, self.prefix)
        registered = self.commands.get(call.name)
        if registered is None or not self._available(registered):
            self._reply_soon(call, f"Unknown command {self.prefix}{word}. Type {self.prefix}help for commands.")
            return
        needed = self.role_needed(registered)
        if not self.roles.allows(call.role, needed):
            self._reply_soon(call, f"{self.prefix}{call.name} is for {role_plural(needed)} and up.")
            return
        spec_cooldown = registered.spec.cooldown
        cooldown = spec_cooldown if spec_cooldown is not None else self.settings.permissions.cooldown
        key = (player.id, call.name)
        now = time.monotonic()
        if call.role not in ("admin", "owner") and now - self._last_use.get(key, -1e9) < cooldown:
            return
        self._last_use[key] = now
        log.info("[%s] %s (%s) ran %s%s %s", call.server, player.name, TITLES[call.role], self.prefix, call.name, rest)
        if registered.runner is None:
            asyncio.ensure_future(self._safe(registered.method(call)))
        else:
            registered.runner.submit(f"!{call.name}", lambda: registered.method(call))

    def _reply_soon(self, call: CommandCall, text: str) -> None:
        asyncio.ensure_future(self._safe(call.reply(text)))

    @staticmethod
    async def _safe(awaitable: Awaitable[Any]) -> None:
        try:
            await awaitable
        except Exception:  # noqa: BLE001
            log.exception("command reply failed")

    async def _help(self, call: CommandCall) -> None:
        if call.args:
            word = call.args[0].lstrip(self.prefix).lower()
            registered = self.commands.get(self.aliases.get(word, word))
            if registered and self._usable_by(registered, call.role):
                spec = registered.spec
                await call.reply(f"{self.prefix}{spec.name} {spec.usage}".rstrip() + (f": {spec.help}" if spec.help else ""))
            else:
                await call.reply(f"No command {self.prefix}{word} for you. Type {self.prefix}help.")
            return
        usable = sorted(n for n, r in self.commands.items() if self._usable_by(r, call.role))
        await call.reply("Commands: " + ", ".join(self.prefix + n for n in usable)
                         + f". {self.prefix}help <command> explains one.")
