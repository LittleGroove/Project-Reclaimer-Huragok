"""Every warn, mute, kick and ban goes through here, whoever asks: a part, a
moderator's chat command, a press in Discord. One place means one ledger,
one dry-run switch, and players always reading who acted ("muted by Alice").
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from ..core.events import EventBus
from ..core.storage import ACTING_ID, Storage
from ..core.text import describe_time, is_player_id
from ..lore import callout
from ..rcon import RconClient, RconError
from ..servers.state import Player, ServerState

log = logging.getLogger(__name__)

KINDS = ("warn", "mute", "unmute", "kick", "ban", "unban", "pardon")
# Evidence on what isn't someone's decision, so not on a rap sheet or a card: a mute put back, a kick passed on to another server.
HOUSEKEEPING = ("still muted:", "kicked on ")


def is_housekeeping(evidence: Any) -> bool:
    return str(evidence or "").startswith(HOUSEKEEPING)


KICK_LIFTED = "their kick, lifted early"  # the reason on the unban behind letting someone back from a kick with a time


def shown_kind(kind: Any, duration: Any, reason: Any = "") -> str:
    """What to call an action. A ban is for good; one with a time is a kick that long (the host's call, 30 October). Underneath it's the server's timed ban,
    which refuses them at the door without letting them in, and says how long is left and why: "You are banned from this server for another 6 days. Reason: …".
    So letting them back early is a pardon, whatever it took."""
    if kind == "unban" and reason == KICK_LIFTED:
        return "pardon"
    return "kick" if kind == "ban" and duration else str(kind)


def kick_reason(duration: str, reason: str) -> str:
    """What a kick with a time gives the server as the ban's reason: the player reads it as they're removed and every time they try to join."""
    return f"Kicked for {describe_time(duration)}" + (f": {reason}" if reason else ".")


TIME_ONLY = re.compile(r"\d+\s*[a-zA-Z]+")


def plain_reason(text: Any) -> str:
    """What a kick was for, without what kick_reason put in front of it for the player: for lists, which say it's a kick and how long for themselves."""
    return re.sub(r"^Kicked for [^:.]+[:.]\s*", "", str(text or ""))


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
        self.ban_note: Callable[[str, str | None], str] | None = None  # what a ban's message adds (Discord: how to appeal)
        self.clients = clients
        self.states = states
        self.storage = storage
        self.bus = bus
        self.dry_run = dry_run
        # (server, kind, player ID) -> when Huragok sent it: the server announces each action back, and one Huragok
        # sent is in the ledger already (the announcement can arrive before the console's reply).
        self._sent: dict[tuple[str, str, str], float] = {}
        self._housekeeping: dict[tuple[str, str], float] = {}  # (server, player ID) -> when a pardon that's no one's decision went out

    def housekeeping_pardon(self, server: str, target: str, seconds: float = 60.0) -> bool:
        """Whether the server's announcement of a pardon is Huragok tidying up, not someone letting a player back."""
        sent = self._housekeeping.get((server, target.lower()))
        return sent is not None and time.monotonic() - sent < seconds

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
        if not is_player_id(target):
            return target, None, None
        known = self.storage.player(target.lower())  # someone who's left, by the name they had
        return target, target.lower(), known.last_name if known else None

    async def act(self, kind: str, server: str, target: Player | str, *, by: str, source: str,
                  reason: str = "", duration: str | None = None, evidence: str | None = None,
                  dry_run: bool = False, pending: bool = False, told: str = "") -> Action:
        """`pending`: for someone who isn't on, nothing goes to the console; it's recorded (and announced) for
        whoever acts on it as they join (Vergil, for a mute). `told`: what the player reads, if not the reason."""
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
            if kind == "pardon" and is_housekeeping(evidence):
                self._housekeeping[(server, (target_id or ref).lower())] = now
            client = self.clients[server]
            try:
                if kind == "warn":
                    reply = await client.command("tell", ref, callout("warn", f"Warning from {by}: {reason}" if reason
                                                                      else f"Warning from {by}."))
                else:
                    args = [ref]
                    if kind in ("mute", "ban") and duration:
                        args.append(duration)
                    said = told or reason
                    if kind == "ban" and target_id and self.ban_note is not None and (note := self.ban_note(target_id, duration)):
                        joined = said.rstrip()
                        said = f"{joined}{'' if not joined or joined[-1] in '.!?:' else '.'} {note}".strip()  # it's read at the door, as a sentence
                    if not duration and kind in ("mute", "ban") and TIME_ONLY.fullmatch(said.strip()):
                        said = f"{said.strip()}."  # alone it would be how long, and a ban for good would end in 30 minutes
                    if kind in ("mute", "kick", "ban") and said:
                        args.append(said)
                    reply = await client.command(kind, *args, by=by)
                ok, text = reply.ok, reply.text
            except RconError as error:
                ok, text = False, str(error)
        action = Action(kind, server, target_id, name, by, source, reason, duration, dry, ok, text)
        case = self.storage.record_action(kind=kind, server=server, target_id=target_id, target_name=name, by=by,
                                          source=source, reason=reason, duration=duration, evidence=evidence,
                                          dry_run=dry, ok=ok, reply=text)
        log.info("[%s] %s by %s via %s: %s", server, action.describe(), by, source, "ok" if ok else f"failed: {text}")
        self.bus.emit("moderation.action", server, kind=kind, target_id=target_id, target_name=name, by=by, by_id=ACTING_ID.get(),
                      source=source, reason=reason, duration=duration, evidence=evidence, dry_run=dry,
                      ok=ok, text=text, pending=pending and not dry, case=case)
        return action


    def record_seen(self, kind: str, server: str, target_id: str | None, target_name: str | None, *, by: str,
                    source: str, reason: str = "", duration: str | None = None) -> None:
        """An action Huragok didn't send but saw the server announce (an admin's own /kick or /ban in game, a vote's
        kick): in the ledger for the player's history (!whois). Its event is `moderation.seen`, not `.action`: automod and
        the commands act on that one, and nobody acts on this; the Discord feed shows both."""
        case = self.storage.record_action(kind=kind, server=server, target_id=target_id, target_name=target_name, by=by,
                                          source=source, reason=reason, duration=duration, evidence=None, dry_run=False,
                                          ok=True, reply="seen on the server")
        self.bus.emit("moderation.seen", server, kind=kind, target_id=target_id, target_name=target_name, by=by, by_id=ACTING_ID.get(), source=source,
                      reason=reason, duration=duration, evidence=None, dry_run=False, ok=True, text="", pending=False, case=case)
        log.info("[%s] %s %s by %s (%s)%s", server, kind, target_name or target_id, by, source,
                 f": {reason}" if reason else "")


class ModerationView:
    def adds_to_bans(self, note: Callable[[str, str | None], str]) -> None:
        """What a ban's message adds, given who it's for and how long."""
        self._service.ban_note = note

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

    def housekeeping_pardon(self, server: str, target: str) -> bool:
        return self._service.housekeeping_pardon(server, target)

    def sent_lately(self, server: str, kind: str, target_id: str) -> bool:
        return self._service.sent_lately(server, kind, target_id)

    def record_seen(self, kind: str, server: str, target_id: str | None, target_name: str | None, *, by: str,
                    source: str, reason: str = "", duration: str | None = None) -> None:
        self._service.record_seen(kind, server, target_id, target_name, by=by, source=source, reason=reason,
                                  duration=duration)
