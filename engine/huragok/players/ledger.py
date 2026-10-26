"""Every warn, mute, kick and ban goes through here, whoever asks: a part, a
moderator's chat command, later a Discord button. One place means one ledger,
one dry-run switch, and players always reading who acted ("muted by Alice").
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..core.events import EventBus
from ..core.storage import Storage
from ..core.text import is_player_id
from ..rcon import RconClient, RconError
from ..servers.state import Player, ServerState

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
        # (server, kind, player ID) -> when Huragok sent it: the server announces each action back, and one Huragok
        # sent is in the ledger already (the announcement can arrive before the console's reply).
        self._sent: dict[tuple[str, str, str], float] = {}

    def sent_lately(self, server: str, kind: str, target_id: str, seconds: float = 30.0) -> bool:
        """Whether Huragok itself sent this action a moment ago (so the server's announcement of it isn't another)."""
        sent = self._sent.get((server, kind, target_id.lower()))
        return sent is not None and time.monotonic() - sent < seconds

    def _target(self, server: str, target: Player | str) -> tuple[str, str | None, str | None]:
        """(what to send, player ID, name). A connected player is named by ID: never ambiguous."""
        if isinstance(target, Player):
            return target.id, target.id, target.name
        state = self.states.get(server)
        player = state.player_by_id(target) if state else None
        if player:
            return player.id, player.id, player.name
        return target, (target.lower() if is_player_id(target) else None), None

    async def act(self, kind: str, server: str, target: Player | str, *, by: str, source: str,
                  reason: str = "", duration: str | None = None, evidence: str | None = None,
                  dry_run: bool = False, pending: bool = False) -> Action:
        """`pending`: for someone who isn't on, nothing goes to the console; it's recorded (and announced) for
        whoever acts on it as they join (Vergil, for a mute)."""
        if kind not in KINDS:
            raise ValueError(f"unknown action {kind}")
        ref, target_id, name = self._target(server, target)
        dry = dry_run or self.dry_run
        ok, text = True, ""
        if dry:
            text = "dry run: not sent"
        elif pending:
            text = "not on now: as they next join"
        else:
            now = time.monotonic()
            self._sent = {key: when for key, when in self._sent.items() if now - when < 60}
            self._sent[(server, kind, (target_id or ref).lower())] = now
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
                      ok=ok, text=text, pending=pending and not dry)
        return action


    def record_seen(self, kind: str, server: str, target_id: str | None, target_name: str | None, *, by: str,
                    source: str, reason: str = "", duration: str | None = None) -> None:
        """An action Huragok didn't send but saw the server announce (an admin's own /kick or /ban in game, a vote's
        kick): in the ledger for the player's history (!whois), and nothing else. No event: nobody acts on it."""
        self.storage.record_action(kind=kind, server=server, target_id=target_id, target_name=target_name, by=by,
                                   source=source, reason=reason, duration=duration, evidence=None, dry_run=False,
                                   ok=True, reply="seen on the server")
        log.info("[%s] %s %s by %s (%s)%s", server, kind, target_name or target_id, by, source,
                 f": {reason}" if reason else "")


class ModerationView:
    """What one part gets: actions carry its name, and its own dry-run setting."""

    def __init__(self, service: Moderation, *, source: str, by: str, dry_run: bool) -> None:
        self._service = service
        self.source = source
        self.by = by
        self.dry_run = dry_run

    async def act(self, kind: str, server: str, target: Player | str, *, by: str | None = None,
                  dry_run: bool = False, live: bool = False, **kwargs: Any) -> Action:
        """`live`: sent even when this part is in dry run, for putting back what a person did (a moderator's
        mute the server forgot). Huragok's own dry_run still holds."""
        return await self._service.act(kind, server, target, by=by or self.by, source=self.source,
                                       dry_run=(self.dry_run and not live) or dry_run, **kwargs)

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

    def sent_lately(self, server: str, kind: str, target_id: str) -> bool:
        return self._service.sent_lately(server, kind, target_id)

    def record_seen(self, kind: str, server: str, target_id: str | None, target_name: str | None, *, by: str,
                    source: str, reason: str = "", duration: str | None = None) -> None:
        self._service.record_seen(kind, server, target_id, target_name, by=by, source=source, reason=reason,
                                  duration=duration)
