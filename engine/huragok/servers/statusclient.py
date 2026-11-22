"""A server with no console (no [rcon] password in its dedicated.toml): its status.json stands in for the console.

0.7.0 (plan-0.7.0.md answer 7, 4.6): a server without a console is still started and kept on, and Huragok warns,
naming the lines to add. Without a console Huragok can't tell players anything, moderate, or ask the server
anything; it can still read what the server writes every 2 seconds: whether it's up (status.json rewritten by its
running process), its phase, map, mode and players. This client takes RconClient's place for such a server:

- connected while status.json is fresh, written by a process that's alive;
- what changes in it becomes the console's own events (rcon.phase, rcon.join, rcon.leave), so the manager's "after
  this game", "nobody on" and the state's picture work as they do with a console;
- `status` and `players` are answered from the file; everything else is refused as the console being off
  (RconUnavailable), which every caller already handles.

status.json's `state` uses the console's phase words, except `lobby` for the console's `no_game`; its `phase` is the
server's own (lobby, vote, countdown, loading, in_game, post_game), used when `state` is missing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import psutil

from ..core.background import wait_until
from ..core.events import EventBus
from ..core.serverfolders import ServerConfig
from ..rcon import RconUnavailable, Reply
from .logs import read_status

log = logging.getLogger(__name__)

POLL_SECONDS = 2.0  # 0.10 rewrites status.json every 2 s
FRESH_SECONDS = 15.0  # written this recently, by a process still running: the server is up
NO_CONSOLE = "it has no console: there's no [rcon] password in its dedicated.toml"
STATE_PHASES = {"lobby": "no_game"}  # status.json's state, in the console's words
SERVER_PHASES = {"in_game": "playing", "post_game": "finished"}  # its own phase, when there's no state


def console_phase(status: dict[str, Any]) -> str:
    """The phase as the console's `phase` events name it."""
    state = status.get("state")
    if isinstance(state, str) and state:
        return STATE_PHASES.get(state, state)
    return SERVER_PHASES.get(str(status.get("phase") or ""), "no_game")


def _players(status: dict[str, Any]) -> dict[int, dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for entry in status.get("players") or []:
        if isinstance(entry, dict):
            try:
                found[int(entry.get("number", 0))] = entry
            except (TypeError, ValueError):
                continue
    return found


class StatusFileClient:
    """RconClient's place for a server with no console. `no_console` tells the manager not to try telling players."""

    no_console = True

    def __init__(self, server: ServerConfig, bus: EventBus, *, poll_seconds: float = POLL_SECONDS,
                 fresh_seconds: float = FRESH_SECONDS) -> None:
        self.server = server
        self.bus = bus
        self.poll_seconds = poll_seconds
        self.fresh_seconds = fresh_seconds
        self.version: str | None = None
        self.protocol: int | None = None
        self.host_slot = False
        self.stopped_reason: str | None = None  # the console isn't refused, there's none: the manager waits on `connected`
        self._connected = False
        self._status: dict[str, Any] | None = None
        self._phase: str | None = None
        self._ids: dict[int, str] = {}  # player number -> ID, as last seen
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        while not self._stop.is_set():
            self.poll()
            await wait_until(self._wake, self.poll_seconds)
            self._wake.clear()
        self._lost()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _fresh(self, status: dict[str, Any] | None, now: float) -> bool:
        if status is None:
            return False
        updated = status.get("updated")
        if not isinstance(updated, (int, float)) or now - float(updated) > self.fresh_seconds:
            return False
        pid = status.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                return psutil.pid_exists(pid)
            except (psutil.Error, OSError):
                return True
        return True

    def _lost(self) -> None:
        if self._connected:
            self._connected = False
            self._ids.clear()
            self._phase = None
            self.bus.emit("rcon.disconnected", self.server.name)

    def poll(self, now: float | None = None) -> None:
        """Read status.json once and say what changed, as the console would have."""
        status = read_status(self.server.status_path)
        if not self._fresh(status, time.time() if now is None else now):
            self._lost()
            return
        assert status is not None
        self._status = status
        players = _players(status)
        if not self._connected:
            # Whoever is on already is read by the state's refresh (`players`, answered from the file), not joined.
            self._connected = True
            self._phase = console_phase(status)
            self._ids = {number: str(p.get("id") or "").lower() for number, p in players.items()}
            health = status.get("health")
            version = health.get("version") if isinstance(health, dict) else None
            self.version = version if isinstance(version, str) else None
            log.info("[%s] watched through its status.json: %s", self.server.name, NO_CONSOLE)
            self.bus.emit("rcon.connected", self.server.name, version=self.version, console=False)
            return
        phase = console_phase(status)
        if phase != self._phase:
            self._phase = phase
            self.bus.emit("rcon.phase", self.server.name, phase=phase, map=status.get("map"), mode=status.get("mode"))
        for number in sorted(set(self._ids) - set(players)):
            self.bus.emit("rcon.leave", self.server.name, number=number, id=self._ids.pop(number))
        for number, entry in sorted(players.items()):
            player_id = str(entry.get("id") or "").lower()
            if self._ids.get(number) != player_id:
                if number in self._ids:  # someone else has the number now: the one before left
                    self.bus.emit("rcon.leave", self.server.name, number=number, id=self._ids[number])
                self._ids[number] = player_id
                self.bus.emit("rcon.join", self.server.name, number=number, id=player_id,
                              name=str(entry.get("name") or ""), admin=bool(entry.get("admin")))

    async def command(self, command: str, *args: str, by: str | None = None,
                      timeout: float | None = None) -> Reply:
        word = command.split()[0].lower() if command.strip() else ""
        if self._connected and self._status is not None:
            if word == "status":
                return Reply(True, "", dict(self._status), {})
            if word == "players":
                return Reply(True, "", {"players": list(_players(self._status).values()),
                                        "max_players": self._status.get("max_players")}, {})
        raise RconUnavailable(NO_CONSOLE)
