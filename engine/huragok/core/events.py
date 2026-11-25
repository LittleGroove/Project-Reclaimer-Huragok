"""Events: everything that happens becomes one Event, published on the bus.

Names are grouped by where they come from:
  server.*      starting, started, stopped, crashed, hung, recovered, restart_planned…
  rcon.*        connected, disconnected, full, auth_failed, lagged, and one per RCON
                event: chat, join, leave, refused, kick, ban, unban, pardon, mute,
                unmute, vote, kill, control, phase, cheat, admin_added, admin_removed
  log.*         lines recognised in data/<server>/server.log
  state.*       the live picture's own: named (a player's name, known after they joined: 0.10)
  moderation.*  action (any warn/mute/kick/ban through the supervisor), seen (one it only saw the server announce)
  supervisor.*  the supervisor's own: started, stopping, part_disabled, part_enabled, commands_reloaded
  <part>.*      events a part emits for the others: automod.strike…
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    name: str
    server: str | None = None  # the server's name; None for supervisor-wide events
    data: dict[str, Any] = field(default_factory=dict)
    time: float = field(default_factory=time.time)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


def matches(pattern: str, name: str) -> bool:
    """'rcon.chat' matches itself, 'rcon.*' every rcon event, '*' everything."""
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return name.startswith(pattern[:-1])
    return pattern == name


Listener = Callable[[Event], None]


class EventBus:
    """Delivers each event to every listener whose pattern matches, in order.

    Listeners run inline and must not block: each part gets its own queue (see
    runner.py), so a slow part never holds up the RCON reader.

    Once bound to the running loop (bind()), an event published from another thread (a check run with to_thread that
    raises a warning) is handed to the loop and delivered there: parts' queues live on it, and a part's listener called
    from a worker thread failed ("no current event loop", the live servers, 23 November 2026). Unbound, or with the
    loop gone, every event is delivered inline as before.
    """

    def __init__(self) -> None:
        self._listeners: list[tuple[str, Listener]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop the listeners run on; called from it, as Huragok starts."""
        self._loop, self._loop_thread = loop, threading.get_ident()

    def subscribe(self, pattern: str, listener: Listener) -> Callable[[], None]:
        entry = (pattern, listener)
        self._listeners.append(entry)

        def unsubscribe() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)

        return unsubscribe

    def publish(self, event: Event) -> Event:
        loop = self._loop
        if loop is not None and threading.get_ident() != self._loop_thread and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._deliver, event)
                return event
            except RuntimeError:  # the loop closing as Huragok stops: delivered here, as before
                pass
        self._deliver(event)
        return event

    def _deliver(self, event: Event) -> None:
        for pattern, listener in list(self._listeners):
            if matches(pattern, event.name):
                try:
                    listener(event)
                except Exception:
                    log.exception("A listener for %s failed on %s", pattern, event.name)

    def emit(self, name: str, server: str | None = None, /, **data: Any) -> Event:
        # Positional-only: a log line's own `name` (the player's) or `server` is data, never the event's.
        return self.publish(Event(name, server, data))
