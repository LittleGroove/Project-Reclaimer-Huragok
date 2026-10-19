"""Every warn, mute, kick and ban goes through here, whoever asks: a part, a
moderator's chat command, later a Discord button. One place means one ledger,
one dry-run switch, and players always reading who acted ("muted by Alice").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .events import EventBus
from .rcon import RconClient, RconError
from .state import Player, ServerState
from .storage import Storage

log = logging.getLogger(__name__)

KINDS = ("warn", "mute", "unmute", "kick", "ban", "unban", "pardon")


@dataclass(frozen=True)
class Action:
    kind: str
    server: str
    target_id: str | None
    target_name: str | None
    by: str
    source: str
    reason: str
    duration: str | None
    dry_run: bool
    ok: bool
    text: str

    def describe(self) -> str:
        what = f"{self.kind} {self.target_name or self.target_id}"
        if self.duration:
            what += f" for {self.duration}"
        if self.reason:
            what += f" ({self.reason})"
        return ("would " if self.dry_run else "") + what


class Moderation:
    def __init__(self, clients: dict[str, RconClient], states: dict[str, ServerState], storage: Storage,
                 bus: EventBus, *, dry_run: bool = False) -> None:
        self.clients = clients
        self.states = states
        self.storage = storage
        self.bus = bus
        self.dry_run = dry_run

    def _target(self, server: str, target: Player | str) -> tuple[str, str | None, str | None]:
        """(what to send, player ID, name). A connected player is named by ID: never ambiguous."""
        if isinstance(target, Player):
            return target.id, target.id, target.name
        state = self.states.get(server)
        player = state.player_by_id(target) if state else None
        if player:
            return player.id, player.id, player.name
        return target, (target.lower() if len(target) == 64 else None), None

    async def act(self, kind: str, server: str, target: Player | str, *, by: str, source: str,
                  reason: str = "", duration: str | None = None, evidence: str | None = None,
                  dry_run: bool = False) -> Action:
        if kind not in KINDS:
            raise ValueError(f"unknown action {kind}")
        ref, target_id, name = self._target(server, target)
        dry = dry_run or self.dry_run
        ok, text = True, ""
        if dry:
            text = "dry run: not sent"
        else:
            client = self.clients[server]
            try:
                if kind == "warn":
                    reply = await client.command("tell", ref, f"Warning from {by}: {reason}" if reason
                                                 else f"Warning from {by}.")
                else:
                    args = [ref]
                    if kind in ("mute", "ban") and duration:
                        args.append(duration)
                    if kind in ("mute", "kick", "ban") and reason:
                        args.append(reason)
                    reply = await client.command(kind, *args, by=by)
                ok, text = reply.ok, reply.text
            except RconError as error:
                ok, text = False, str(error)
        action = Action(kind, server, target_id, name, by, source, reason, duration, dry, ok, text)
        self.storage.record_action(kind=kind, server=server, target_id=target_id, target_name=name, by=by,
                                   source=source, reason=reason, duration=duration, evidence=evidence,
                                   dry_run=dry, ok=ok, reply=text)
        log.info("[%s] %s by %s via %s: %s", server, action.describe(), by, source, "ok" if ok else f"failed: {text}")
        self.bus.emit("moderation.action", server, kind=kind, target_id=target_id, target_name=name, by=by,
                      source=source, reason=reason, duration=duration, evidence=evidence, dry_run=dry,
                      ok=ok, text=text)
        return action


class ModerationView:
    """What one part gets: actions carry its name, and its own dry-run setting."""

    def __init__(self, service: Moderation, *, source: str, by: str, dry_run: bool) -> None:
        self._service = service
        self.source = source
        self.by = by
        self.dry_run = dry_run

    async def act(self, kind: str, server: str, target: Player | str, *, by: str | None = None,
                  dry_run: bool = False, **kwargs: Any) -> Action:
        return await self._service.act(kind, server, target, by=by or self.by, source=self.source,
                                       dry_run=self.dry_run or dry_run, **kwargs)

    async def warn(self, server: str, target: Player | str, reason: str = "", **kw: Any) -> Action:
        return await self.act("warn", server, target, reason=reason, **kw)

    async def mute(self, server: str, target: Player | str, duration: str | None = None, reason: str = "",
                   **kw: Any) -> Action:
        return await self.act("mute", server, target, duration=duration, reason=reason, **kw)

    async def unmute(self, server: str, target: Player | str, **kw: Any) -> Action:
        return await self.act("unmute", server, target, **kw)

    async def kick(self, server: str, target: Player | str, reason: str = "", **kw: Any) -> Action:
        return await self.act("kick", server, target, reason=reason, **kw)

    async def ban(self, server: str, target: Player | str, duration: str | None = None, reason: str = "",
                  **kw: Any) -> Action:
        return await self.act("ban", server, target, duration=duration, reason=reason, **kw)

    async def unban(self, server: str, target: str, **kw: Any) -> Action:
        return await self.act("unban", server, target, **kw)

    async def pardon(self, server: str, target: Player | str, **kw: Any) -> Action:
        return await self.act("pardon", server, target, **kw)
