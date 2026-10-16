"""Events: everything that happens becomes one Event, published on the bus.

Names are grouped by where they come from:
  server.*      starting, started, stopped, crashed, hung, recovered, restart_planned…
  rcon.*        connected, disconnected, full, auth_failed, lagged, and one per RCON
                event: chat, join, leave, refused, kick, ban, unban, pardon, mute,
                unmute, vote, kill, control, phase, cheat, admin_added, admin_removed
  log.*         lines recognised in data/<server>/server.log
  moderation.*  action (any warn/mute/kick/ban through the supervisor)
  supervisor.*  the supervisor's own: started, stopping, part_disabled
  <part>.*      events a part emits for the others: automod.strike…
"""

from __future__ import annotations

import logging
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
    """

    def __init__(self) -> None:
        self._listeners: list[tuple[str, Listener]] = []

    def subscribe(self, pattern: str, listener: Listener) -> Callable[[], None]:
        entry = (pattern, listener)
        self._listeners.append(entry)

        def unsubscribe() -> None:
            if entry in self._listeners:
                self._listeners.remove(entry)

        return unsubscribe

    def publish(self, event: Event) -> Event:
        for pattern, listener in list(self._listeners):
            if matches(pattern, event.name):
                try:
                    listener(event)
                except Exception:
                    log.exception("A listener for %s failed on %s", pattern, event.name)
        return event

    def emit(self, name: str, server: str | None = None, **data: Any) -> Event:
        return self.publish(Event(name, server, data))
