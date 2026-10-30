"""In-game commands: players type !name in game chat; who may use what is commands.toml.

- The game handles every line starting with / itself, so ours start with ! (any
  prefix but / can be set). Those lines are ordinary chat: everyone in the channel
  sees them. Replies go back privately with `tell`.
- Every part's commands share one set of names; a test checks they never clash.
- !yes and !no are the server's own votes and never arrive as chat.
- Who may run what comes from roles tied to player IDs, never names: anyone can
  use any name. The permissions are read on every line, so edits to
  commands.toml apply while running.
- A probationary moderator (0.5.3) uses moderators' commands: looking ones (LOOKING) as
  they are, the others, which name a player, as a request a moderator or admin confirms
  (on_request: players/commands/requests.py), which then runs as theirs (run_as).
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..core.background import background
from ..core.commandsfile import HELP, PROBATION, ROLES, TITLES
from ..core.events import Event, EventBus
from ..core.part import CommandSpec
from ..rcon import RconError
from ..servers.state import Player, ServerState
from .roles import Roles, role_name
from .targets import QUOTES, SCOPES, Picks, find, is_team, is_time, outranked, player_and_rest

if TYPE_CHECKING:
    from ..core.settings import Settings
    from ..core.storage import PlayerHistory
    from ..rcon import RconClient
    from ..core.runner import PartRunner

log = logging.getLogger(__name__)

RESERVED = {"yes", "no"}  # the server counts these as votes
WORD = re.compile(r"[a-z][a-z0-9_-]{0,31}")
LINE_LIMIT = 120  # characters per private line; longer replies are split
# What a probationary moderator runs as a moderator does: looking, never acting. Every other moderators' command that
# names a player is a request (requests.py); one that names nobody isn't theirs until they're a moderator.
LOOKING = {"players", "whois", "said", "kicks", "mutes", "rules", "roles", "health", "bannames", "novotes", "reports",
           "strikes", "help"}
# Where the player a command names ends, for a command that finds its own (!strike: a rule's name comes next).
REQUEST_FOLLOWS = {"strike": "_strike_word"}


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


def unquote(words: list[str]) -> str:
    """A reason or note as typed, without the quotes around it when it was written in some: "being rude"."""
    text = " ".join(words).strip()
    if len(text) > 1 and text[0] in QUOTES and text[-1] == QUOTES[text[0]]:
        return text[1:-1].strip()
    return text


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
    return "everyone" if role == "player" else f"{role_name(role)}s"


@dataclass
class CommandCall:
    """One use of a command: who, where, and the words after its name. A command that names a player has them as
    `target` (and the words after them as `rest`) when it declares `player` (targets.py)."""

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
    states: dict[str, ServerState] | None = None  # every server's, to say where someone else is
    history: PlayerHistory | None = None  # players seen here: names that have left, by ID
    role_of: Callable[[Player], str] | None = None  # a player's rank: the highest they hold on any server
    picks: Picks | None = None  # each caller's last list: @1, @2…
    target: Player | None = None  # the player a command declaring `player` names
    rest: list[str] = field(default_factory=list)  # the words after them

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

    async def find(self, ref: str, *, scope: str = "on", start: bool = False) -> Player | None:
        """The one player `ref` means (targets.find), or None after telling the caller why."""
        return await find(self, ref, scope=scope, start=start)

    async def player_and_rest(self, text: str | None = None, *, scope: str = "on", acts: bool = False,
                              start: bool = False,
                              follows: Callable[[str], bool] | None = None) -> tuple[Player | None, list[str]]:
        """The player the line (or `text`) names first, however many words their name has, and the words after
        them (targets.player_and_rest)."""
        return await player_and_rest(self, text, scope=scope, acts=acts, start=start, follows=follows)


@dataclass
class Registered:
    spec: CommandSpec
    runner: PartRunner | None  # None: the router's own (!help)
    method: Callable[[CommandCall], Awaitable[Any]]


class CommandRouter:
    def __init__(self, settings: Settings, bus: EventBus, states: dict[str, ServerState],
                 clients: dict[str, RconClient], roles: Roles, history: PlayerHistory | None = None) -> None:
        self.settings = settings
        self.states = states
        self.clients = clients
        self.roles = roles
        self.history = history
        self.picks = Picks()
        self.commands: dict[str, Registered] = {}
        self.aliases: dict[str, str] = {}
        self._last_use: dict[tuple[str, str], float] = {}
        # A probationary moderator's request: (call, the command, its player, the words after them) -> the part that
        # keeps them (players/commands/requests.py). None: the rank isn't in use, and their commands are refused.
        self.on_request: Callable[[CommandCall, Registered, Player, list[str]], Awaitable[None]] | None = None
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
        if spec.player and spec.player not in SCOPES:
            raise ValueError(f"{owner}: !{spec.name} player must be one of {', '.join(SCOPES)}")
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
                           split_words(rest), rest, str(event.get("channel", "")), state, self._tell, self.prefix,
                           states=self.states, history=self.history,
                           role_of=lambda target: self.roles.highest_role(list(self.states.values()), target),
                           picks=self.picks)
        registered = self.commands.get(call.name)
        if registered is None or not self._available(registered):
            self._reply_soon(call, f"Unknown command {self.prefix}{word}. Type {self.prefix}help for commands.")
            return
        needed = self.role_needed(registered)
        request = False
        if call.role == PROBATION and needed == "moderator" and call.name not in LOOKING:
            if not (registered.spec.player or call.name in REQUEST_FOLLOWS) or self.on_request is None:
                self._reply_soon(call, f"{self.prefix}{call.name} is for moderators; on probation, the commands that "
                                       "name a player are requests.")
                return
            request = True
        elif not (self.roles.allows(call.role, needed) or (call.role == PROBATION and needed == "moderator")):
            self._reply_soon(call, f"{self.prefix}{call.name} is for {role_plural(needed)} and up.")
            return
        spec_cooldown = registered.spec.cooldown
        cooldown = spec_cooldown if spec_cooldown is not None else self.settings.permissions.cooldown
        key = (player.id, call.name)
        now = time.monotonic()
        if call.role not in ("admin", "owner") and now - self._last_use.get(key, -1e9) < cooldown:
            return
        self._last_use[key] = now
        log.info("[%s] %s (%s) %s %s%s %s", call.server, player.name, TITLES[call.role],
                 "asked for" if request else "ran", self.prefix, call.name, rest)
        if request:
            work = lambda: self._request(registered, call)  # noqa: E731
            if registered.runner is None:
                background(self._safe(work()), f"a request for {self.prefix}{call.name} on {call.server}", log)
            else:
                registered.runner.submit(f"!{call.name} (a request)", work)
            return
        if registered.runner is None:
            background(self._safe(self._run(registered, call)), f"{self.prefix}{call.name} on {call.server}", log)
        else:
            registered.runner.submit(f"!{call.name}", lambda: self._run(registered, call))

    async def _run(self, registered: Registered, call: CommandCall) -> None:
        """A command that declares `player` gets them found first, with every guard (targets.py): no name, a name
        several players have, someone of the caller's role or above… answer the caller, and the command doesn't run."""
        spec = registered.spec
        if spec.player:
            if not call.text.strip():
                await call.usage(spec)
                return
            if spec.words_after:
                target, rest = await call.player_and_rest(scope=spec.player, acts=spec.acts, start=spec.start,
                                                          follows=self._follows(registered))
            else:  # all of it is the name
                target, rest = await call.find(call.text, scope=spec.player, start=spec.start), []
                if target is not None and spec.acts and (why := outranked(call, target)):
                    await call.reply(why)
                    return
            if target is None:
                return
            call.target, call.rest = target, rest
        await registered.method(call)

    async def _request(self, registered: Registered, call: CommandCall) -> None:
        """A probationary moderator's command: the player it names found with every guard (as if it ran), and then a
        request instead of the action."""
        spec = registered.spec
        if not call.text.strip():
            await call.usage(spec)
            return
        follows = self._follows(registered)
        if follows is None and call.name in REQUEST_FOLLOWS:
            part = registered.runner.part if registered.runner else None
            follows = getattr(part, REQUEST_FOLLOWS[call.name], None)
        if spec.player and not spec.words_after:
            target, rest = await call.find(call.text, scope="seen", start=spec.start), []
            if target is not None and (why := outranked(call, target)):
                await call.reply(why)
                return
        else:
            # Someone who left can be asked about too: the request may be confirmed after they've gone.
            target, rest = await call.player_and_rest(scope="seen", acts=True, start=spec.start, follows=follows)
        if target is None or self.on_request is None:
            return
        await self.on_request(call, registered, target, rest)

    async def run_as(self, name: str, target: Player, words: list[str], *, server: str, player: Player, role: str,
                     tell: Callable[[str, str, str], Awaitable[Any]]) -> str | None:
        """Run `name` on `target` for `player` (`role`), on `server`, as if they'd typed it there: a confirmed
        request (requests.py). Their replies go through `tell`. None when it ran; else why it couldn't."""
        registered = self.commands.get(self.aliases.get(name, name))
        if registered is None or not self._available(registered):
            return f"there's no {self.prefix}{name} to run"
        needed = self.role_needed(registered)
        if not self.roles.allows(role, needed):
            return f"{self.prefix}{name} is for {role_plural(needed)} and up"
        state = self.states.get(server)
        if state is None:
            return f"there's no server {server}"
        call = CommandCall(server, player, role, registered.spec.name, [target.id, *words],
                           " ".join([target.id, *words]), "requests", state, tell, self.prefix, states=self.states,
                           history=self.history,
                           role_of=lambda who: self.roles.highest_role(list(self.states.values()), who),
                           picks=self.picks)
        if why := outranked(call, target):
            return why
        if registered.spec.player:
            call.target, call.rest = target, list(words)
        await registered.method(call)
        return None

    @staticmethod
    def _follows(registered: Registered) -> Callable[[str], bool] | None:
        """What may come right after the player's name: "time", "team", or the name of a method of the part."""
        follows = registered.spec.follows
        if not follows:
            return None
        if follows == "time":
            return is_time
        if follows == "team":
            return is_team
        part = registered.runner.part if registered.runner else None
        method = getattr(part, follows, None)
        return method if callable(method) else None

    def _reply_soon(self, call: CommandCall, text: str) -> None:
        background(self._safe(call.reply(text)), f"a reply on {call.server}", log)

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
