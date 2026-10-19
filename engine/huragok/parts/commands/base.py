"""What every in-game command shares: finding the target, checking rank, passing lines to the console."""

from __future__ import annotations

import re

from ...chat import CommandCall
from ...moderation import Action
from ...odst import console_refusal
from ...part import Part
from ...permissions import ROLES, a_role
from ...rcon import RconError
from ...state import Player

TIME = re.compile(r"\d+[smhdw]", re.IGNORECASE)
FOREVER = {"perm", "permanent", "forever", "never"}
PLAYER_ID = re.compile(r"[0-9a-fA-F]{64}")
MAX_REPLY_LINES = 8


class CommandsBase(Part):
    defaults = {"max_reply_lines": MAX_REPLY_LINES}

    async def _reply_text(self, call: CommandCall, text: str) -> None:
        lines = [l for l in text.splitlines() if l.strip()]
        limit = int(self.config.get("max_reply_lines", MAX_REPLY_LINES))
        if len(lines) > limit:
            lines = lines[:limit] + [f"…and {len(lines) - limit} more lines"]
        await call.reply("\n".join(lines) or "Done.")

    def _outranks(self, call: CommandCall, target: Player) -> bool:
        if call.role == "owner":
            return True
        return ROLES.index(self.ctx.role_of(call.server, target)) < ROLES.index(call.role)

    async def _target(self, call: CommandCall, ref: str, *, offline: bool = False) -> Player | None:
        """Who a warn, mute, kick or ban is for: #number, player ID, or exact name (never just its start), and
        someone below the caller. With `offline`, someone who isn't on too (by ID: a Player with number 0);
        roles follow the ID, so rank counts for them as well."""
        player = await call.find_target(ref, history=self.ctx.players, offline=offline)
        if player and not self._outranks(call, player):
            await call.reply(f"{player.name} is {a_role(self.ctx.role_of(call.server, player))}; you can't do that to "
                             "them.")
            return None
        return player

    async def _done(self, call: CommandCall, action: Action) -> None:
        if action.dry_run:
            await call.reply(f"Dry run: would {action.kind} {action.target_name or action.target_id}.")
        elif action.ok:
            await call.reply(action.text or "Done.")
        else:
            await call.reply(f"Couldn't: {action.text}")

    async def _rcon(self, call: CommandCall, line: str, *args: str) -> None:
        """Pass a command to the console as the caller, and show them the reply."""
        if self.ctx.plays_kind(call.server) == "odst" and (refusal := console_refusal(" ".join([line, *args]))):
            await call.reply(refusal)  # an ODST server plays what its file names (0.9.20 would still queue a map)
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
