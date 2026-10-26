"""What each part of the supervisor is made of: a subclass of Part, and decorators for its handlers.

The supervisor is one program; automod, the in-game commands, the watchdog and
the event log are its parts. Each lives in the folder of the named part it
belongs to (servers/, players/) and is listed in parts.py. A part with settings has its own section in
settings.toml:

    [greeter]
    enabled = true
    invite = "discord.gg/…"

    class Greeter(Part):
        name = "greeter"
        description = "Welcomes players and answers !discord"
        defaults = {"invite": ""}

        @on("rcon.join")
        async def welcome(self, event):
            await self.ctx.tell(event.server, event["id"], f"Hi {event['name']}!")

        @command("discord", help="Our Discord invite")
        async def discord(self, call):
            await call.reply(self.config["invite"])

        @every(minutes=10)
        async def remind(self):
            for server in self.ctx.servers:
                await self.ctx.say(server, "Be nice!")

Who may use each command is the host's choice, in commands.toml; `role` here is
only the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, TypeVar

from ..lore import HURAGOK, Persona

if TYPE_CHECKING:
    from .context import PartContext

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str = ""
    usage: str = ""  # the words after the name, e.g. "<player> [time] [reason]"
    role: str = "player"  # the lowest role allowed unless commands.toml says otherwise
    aliases: tuple[str, ...] = ()
    cooldown: float | None = None  # seconds per player; None uses cooldown_seconds in commands.toml
    # The player it names first, found by the router before it runs, with every guard (targets.py): "on" (on this
    # server), "seen" (someone who left too, by player ID); "" for a command that names nobody, or finds them itself.
    player: str = ""
    acts: bool = False  # it acts against them: nobody of the caller's role or above
    start: bool = False  # it only looks or lifts: the start of a name will do, when one player on has it
    follows: str = ""  # what may come right after the name: "time", "team", or the name of a method of the part
    words_after: bool = True  # False: nothing follows the player, so everything typed is their name (!whois)


def on(*patterns: str) -> Callable[[F], F]:
    """Run this method for every event matching a pattern: 'rcon.chat', 'rcon.*', '*'."""

    def mark(method: F) -> F:
        method.__dict__.setdefault("_part_on", []).extend(patterns)
        return method

    return mark


def command(name: str, *, help: str = "", usage: str = "", role: str = "player",
            aliases: tuple[str, ...] | list[str] = (), cooldown: float | None = None, player: str = "",
            acts: bool = False, start: bool = False, follows: str = "", words_after: bool = True) -> Callable[[F], F]:
    """A chat command: players type !name (the prefix is set in commands.toml). With `player`, the player the line
    names first is found before it runs (call.target, and the words after them call.rest): see targets.py."""

    def mark(method: F) -> F:
        method.__dict__["_part_command"] = CommandSpec(name.lower(), help, usage, role,
                                                       tuple(a.lower() for a in aliases), cooldown, player, acts,
                                                       start, follows, words_after)
        return method

    return mark


def every(seconds: float = 0, *, minutes: float = 0, hours: float = 0) -> Callable[[F], F]:
    """Run this method on a timer, first after one interval."""
    interval = seconds + minutes * 60 + hours * 3600
    if interval <= 0:
        raise ValueError("every() needs a positive interval")

    def mark(method: F) -> F:
        method.__dict__["_part_every"] = interval
        return method

    return mark


def commands_of(cls: type[Part]) -> list[CommandSpec]:
    """The chat commands a part defines."""
    specs = [getattr(getattr(cls, a, None), "__dict__", {}).get("_part_command") for a in dir(cls)]
    return [s for s in specs if s]


class Part:
    """Subclass this; the supervisor makes one instance of each part that is on.

    `name` is its section in settings.toml. `defaults` fills in settings the
    host leaves out. check_config() runs before setup(): raise ValueError to
    switch this part off with your message, without affecting anything else.
    """

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    acts_as: ClassVar[str] = ""  # what players read when it acts ("muted by AutoMod"); `name` setting overrides
    defaults: ClassVar[dict[str, Any]] = {}
    always_on: ClassVar[bool] = False  # no section in settings.toml, can't be switched off (the commands)
    enabled_by_default: ClassVar[bool] = False
    # The named part it belongs to (lore.py): its self.ctx is that part's (superintendent.py, vergil.py).
    persona: ClassVar[Persona] = HURAGOK
    old_options: ClassVar[frozenset[str]] = frozenset()  # settings an older version had, still read

    def __init__(self, ctx: PartContext) -> None:
        self.ctx = ctx
        self.config = ctx.config
        self.log = ctx.log

    @classmethod
    def preflight(cls, config: dict[str, Any], home: Path) -> list[str]:
        """Check settings and files without running, for `check`.

        `config` is the part's section of settings.toml (defaults not yet
        applied) and `home` the supervisor's folder. Return warnings; raise
        ValueError for errors.
        """
        return []

    def check_config(self) -> None:
        pass

    async def setup(self) -> None:
        pass

    async def teardown(self) -> None:
        pass
