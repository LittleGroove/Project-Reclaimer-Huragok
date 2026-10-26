"""The keepalive: notices a server that has stopped responding (its status.json and its console both quiet), and
restarts it when the backend can. A slow server that still answers is the watchdog's business (watchdog.py)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..core.background import background
from ..core.events import EventBus
from ..core.settings import KeepaliveSettings
from .backends import LifecycleError, LifecycleUnavailable
from .state import ServerState

if TYPE_CHECKING:
    from .fleet import Lifecycle

log = logging.getLogger(__name__)


class Keepalive:
    """Notices a server that has stopped responding, and restarts it when the backend can.

    Hung means both: status.json not rewritten (normally every 5 s) and RCON not
    answering, each for hang_seconds. Two independent signals keep false alarms
    rare; a slow server that still answers is the watchdog's business.

    Each run of a server is judged from when it came up: the files and replies of
    the run before don't count against it, and one that freezes as it starts is
    restarted hang_seconds later (the live server froze twice in a row that way:
    76 minutes, then 2 s into the next start). It acts once per run, again after
    RETRY if that run is still hung; the per-hour restart budget stops a loop.
    """

    RETRY = 600.0  # the same run still hung this long after its restart was asked for: ask again

    def __init__(self, settings: KeepaliveSettings, lifecycle: Lifecycle, states: dict[str, ServerState],
                 bus: EventBus) -> None:
        self.settings = settings
        self.lifecycle = lifecycle
        self.states = states
        self.bus = bus
        self.hung: set[str] = set()
        self._up_since: dict[str, float] = {}  # monotonic time this run was first seen up
        self._acted: dict[str, tuple[float, float]] = {}  # (the run's up_since, when it acted)

    def is_hung(self, state: ServerState, up_for: float | None = None) -> bool:
        age = state.status_age
        quiet = None if state.last_rcon_ok is None else time.monotonic() - state.last_rcon_ok
        if up_for is not None:  # nothing from before this run counts
            answered = quiet is not None and quiet < up_for  # RCON answered during this run
            age = None if age is None else min(age, up_for)
            quiet = up_for if quiet is None else min(quiet, up_for)
            if age is None and answered:
                # No status.json yet (a new server's first run): one that freezes before writing it would never
                # count as hung. Its console answered this run, so the run's age stands in for the file's. (A
                # console that never answered gives no signal at all: that alone mustn't restart it in a loop.)
                age = up_for
        limit = self.settings.hang_seconds
        return age is not None and age > limit and (quiet is None or quiet > limit)

    def check(self) -> None:
        now = time.monotonic()
        for name, state in list(self.states.items()):
            if name in self.lifecycle.busy or not self.lifecycle.is_up(name):
                self._up_since.pop(name, None)
                continue
            since = self._up_since.setdefault(name, now)
            if self.is_hung(state, now - since):
                if name not in self.hung:
                    self.hung.add(name)
                    log.error("[%s] not responding: status.json last written %.0f s ago, RCON quiet",
                              name, state.status_age or 0)
                    self.bus.emit("server.hung", name, status_age=state.status_age)
                self._maybe_act(name, since)
            elif name in self.hung:
                self.hung.discard(name)
                log.info("[%s] responding again", name)
                self.bus.emit("server.recovered", name)

    def _maybe_act(self, name: str, since: float) -> None:
        if not (self.settings.act and self.lifecycle.can_control):
            return
        run, acted = self._acted.get(name, (None, 0.0))
        if run == since and time.monotonic() - acted < self.RETRY:
            return
        self._acted[name] = (since, time.monotonic())
        background(self._restart(name), f"[{name}] the keepalive's restart", log)

    async def _restart(self, name: str) -> None:
        try:
            await self.lifecycle.restart(name, when="now", reason="not responding", countdown=0)
        except (LifecycleUnavailable, LifecycleError) as error:
            log.warning("[%s] can't restart it: %s", name, error)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.settings.check_seconds)
            self.check()
