"""What every in-game command shares: replies, results, passing lines to the console. Finding the player a command
names, with its guards, is targets.py's (a command declares it: @command(…, player=…))."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.part import Part
from ...games.plays import console_refusal
from ...rcon import RconError
from ..ledger import Action
from ..relay import CONTROL
from ..router import CommandCall
from ..targets import FOREVER, TIME

if TYPE_CHECKING:
    from collections import deque

    from ...core.events import Event
    from ..targets import Candidate
    from .lists import Kick

MAX_REPLY_LINES = 8


class CommandsBase(Part):
    defaults = {"max_reply_lines": MAX_REPLY_LINES}

    async def _reply_text(self, call: CommandCall, text: str) -> None:
        lines = [l for l in text.splitlines() if l.strip()]
        limit = int(self.config.get("max_reply_lines", MAX_REPLY_LINES))
        if len(lines) > limit:
            lines = lines[:limit] + [f"…and {len(lines) - limit} more lines"]
        await call.reply("\n".join(lines) or "Done.")

    async def _done(self, call: CommandCall, action: Action) -> None:
        if action.dry_run:
            await call.reply(f"Dry run: would {action.kind} {action.target_name or action.target_id}.")
        elif action.ok:
            await call.reply(action.text or "Done.")
        else:
            await call.reply(f"Couldn't: {action.text}")

    async def _rcon(self, call: CommandCall, line: str, *args: str) -> None:
        """Pass a command to the console as the caller, and show them the reply."""
        state = self.ctx.servers.get(call.server)
        if refusal := console_refusal(self.ctx.plays_kind(call.server), " ".join([line, *args]),
                                      state.version if state is not None else None):
            await call.reply(refusal)  # an ODST server before 0.9.21 plays what its file names (0.9.20 queued a map)
            return
        try:
            reply = await self.ctx.rcon(call.server).command(line, *args, by=call.player.name)
        except RconError as error:
            await call.reply(f"Couldn't: {error}")
            return
        await self._reply_text(call, reply.text if reply.ok else f"Couldn't: {reply.text}")

    async def _line(self, call: CommandCall, name: str, *, needs_words: bool = False) -> None:
        """The console command of the same name, with the caller's words after it."""
        if needs_words and not call.text:
            await call.reply(f"Use: {call.prefix}{name} {self._usage(name)}")
            return
        if CONTROL.search(call.text):  # as the relay refuses them: a second command could hide behind a new line
            await call.reply("One command, on one line: new lines, tabs and other control characters can't go "
                             "to the console.")
            return
        await self._rcon(call, f"{name} {call.text}".strip())

    def _usage(self, name: str) -> str:
        method = getattr(type(self), name, None)
        spec = getattr(method, "__dict__", {}).get("_part_command")
        return spec.usage if spec else ""

    @staticmethod
    def _split_time(words: list[str]) -> tuple[str | None, list[str], bool]:
        """['30m', 'being', 'rude'] -> ('30m', ['being', 'rude'], False); 'perm' -> forever."""
        if words and TIME.fullmatch(words[0]):
            return words[0].lower(), words[1:], False
        if words and words[0].lower() in FOREVER:
            return None, words[1:], True
        return None, words, False

    # What one group of commands asks of another: declared here, defined by the group named. Commands (__init__.py)
    # is every group, so each call reaches the real one; a group's own methods stay its own.

    # AcrossServers (across.py)

    def _across(self) -> bool:
        raise NotImplementedError("AcrossServers's")

    def _across_echo(self, server: str, kind: str, target: str, event: Event | None = None) -> bool:
        raise NotImplementedError("AcrossServers's")

    def _across_mark(self, server: str, kind: str, target: str) -> None:
        raise NotImplementedError("AcrossServers's")

    def _across_unmark(self, server: str, kind: str, target: str) -> None:
        raise NotImplementedError("AcrossServers's")

    async def _banned_elsewhere(self, server: str, target: str) -> tuple[list[str], list[str]]:
        """The other servers' own bans of this target (a /ban there): "Other by LilGremlin (/ban: spam)", and the
        servers whose list didn't answer in time."""
        raise NotImplementedError("AcrossServers's")

    async def _admin_on(self, server: str, verb: str, target: str, by: str, origin: str) -> bool:
        """`admin add|remove <ID>` passed on to `server` (a job of its queue): how it went."""
        raise NotImplementedError("AcrossServers's")

    async def _pardon_elsewhere(self, server: str, target: str, by: str) -> list[str]:
        """The kicks through Huragok on other servers keeping this player (ID, or the name they were kicked under) off
        `server` too: taken off the list, and pardoned where they were given (each server's queue). Those servers."""
        raise NotImplementedError("AcrossServers's")

    def every_server(self, kind: str, target: str | None = None) -> list[Any]:
        """What's on Huragok's list and in force (not taken off, not run out): its bans, or the admins it made."""
        raise NotImplementedError("AcrossServers's")

    def add_every_server(self, kind: str, target: str, *, name: str, reason: str, by: str, server: str,
                         expires: float | None) -> bool:
        """On Huragok's list. One in force already is kept (a later end, or none, lengthens it): False."""
        raise NotImplementedError("AcrossServers's")

    def lift_every_server(self, kind: str, target: str, by: str) -> int:
        """Off Huragok's list (by player ID or address, or the name it was given under): how many."""
        raise NotImplementedError("AcrossServers's")

    def _own_lists(self, server: str) -> list[str]:
        """The other servers, connected now, whose ban list isn't this one's (one that isn't connected gets Huragok's
        list as it connects: _every_server_check)."""
        raise NotImplementedError("AcrossServers's")

    async def _import_shared_lists(self) -> None:
        """The host's call (25 October): bans from before 0.5.0 were every server's when given (the servers shared one
        list), so each shared list's bans go on Huragok's list, once per list: a server added later gets them, and
        !bans says "every server". One taken off Huragok's list later stays off (never imported again)."""
        raise NotImplementedError("AcrossServers's")

    # NoVotes (novotes.py)

    def _no_vote_now(self, player_id: str, now: float) -> str:
        """For !whois's Now: "can't call votes, 8m more (by Mod: spam)", or ""."""
        raise NotImplementedError("NoVotes's")

    # ListCommands (lists.py)

    async def _read_bans(self, server: str) -> tuple[list[dict[str, Any]] | None, str]:
        """A server's ban list, newest first (_ban_data), or None and why it couldn't be read."""
        raise NotImplementedError("ListCommands's")

    async def _ban_data(self, call: CommandCall) -> list[dict[str, Any]] | None:
        """This server's ban list as the console gives it, newest first: each player, and each address banned on its own
        (an address linked to a player's ban goes with theirs). None after telling the caller it couldn't be read."""
        raise NotImplementedError("ListCommands's")

    async def _ban_entries(self, call: CommandCall) -> list[Candidate] | None:
        """This server's bans as a list to pick from, each saying whether it's on every server (Huragok's list) or this
        one's alone (a /ban, until !banall)."""
        raise NotImplementedError("ListCommands's")

    async def _listed(self, call: CommandCall, text: str, entries_of: Any) -> str:
        """What to send for `text`: an @number's entry (its player ID, or an address), the one entry with that name, or
        the text itself for the console to match (an ID, an address, a name on none of the entries). "" after asking
        the caller to pick (several entries with that name) or telling them why not."""
        raise NotImplementedError("ListCommands's")

    def _kicks_list(self) -> deque[Kick]:
        raise NotImplementedError("ListCommands's")

    def recent_kicks(self, server: str | None = None) -> list[Kick]:
        """Kicks still keeping someone out (or within the last two hours), newest first."""
        raise NotImplementedError("ListCommands's")
