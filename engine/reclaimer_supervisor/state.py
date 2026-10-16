"""A live picture of each server: players, phase, map and mode, kept by events.

RCON events keep it current; a `players` + `status` call on connecting, after a
`lagged`, and every 30 s corrects any drift and doubles as a health check.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .events import Event, EventBus
from .rcon import RconClient, RconError

log = logging.getLogger(__name__)

PLAYER_ID = re.compile(r"[0-9a-fA-F]{64}")


@dataclass
class Player:
    number: int
    name: str
    id: str
    ip: str | None = None
    admin: bool = False
    muted: bool = False
    team: str | None = None
    joined: float = field(default_factory=time.time)

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def __str__(self) -> str:
        return f"{self.name} (#{self.number})"


@dataclass
class ServerState:
    name: str
    connected: bool = False
    version: str | None = None
    phase: str | None = None
    phase_since: float | None = None
    map: str | None = None
    mode: str | None = None
    max_players: int | None = None
    players: dict[int, Player] = field(default_factory=dict)
    last_rcon_ok: float | None = None  # monotonic time of the last good reply
    rcon_latency_ms: float | None = None
    status_updated: float | None = None  # status.json's own `updated` (Unix seconds)
    status_seen: float | None = None  # when we last read status.json (Unix seconds)
    pid: int | None = None  # the server's process, when the backend knows it
    admins: set[str] | None = None  # the server's admins (RCON `admin`), None until it answers

    @property
    def status_age(self) -> float | None:
        """Seconds since the server last rewrote status.json (every 5 s when healthy)."""
        return None if self.status_updated is None else max(0.0, time.time() - self.status_updated)

    def player_by_id(self, player_id: str) -> Player | None:
        player_id = player_id.lower()
        return next((p for p in self.players.values() if p.id == player_id), None)

    def find(self, ref: str) -> list[Player]:
        """Players a reference could mean: #number, player ID, exact name, then name start.

        Names can't be trusted to pick a player (one night's log had 30 players
        called "Player"), so anything matching more than one is returned as such
        and the caller asks for a #number instead.
        """
        ref = ref.strip()
        if ref.startswith("#") and ref[1:].isdigit():
            player = self.players.get(int(ref[1:]))
            return [player] if player else []
        if PLAYER_ID.fullmatch(ref):
            player = self.player_by_id(ref)
            return [player] if player else []
        folded = ref.casefold()
        exact = [p for p in self.players.values() if p.name.casefold() == folded]
        if exact:
            return exact
        return [p for p in self.players.values() if p.name.casefold().startswith(folded)]


class StateTracker:
    """Keeps every ServerState current from RCON events and periodic refreshes."""

    def __init__(self, bus: EventBus, clients: dict[str, RconClient], refresh_seconds: float = 30.0) -> None:
        self.bus = bus
        self.clients = clients
        self.refresh_seconds = refresh_seconds
        self.states = {name: ServerState(name) for name in clients}
        self._refresh_now = {name: asyncio.Event() for name in clients}
        bus.subscribe("rcon.*", self._on_event)

    def _on_event(self, event: Event) -> None:
        state = self.states.get(event.server or "")
        if state is None:
            return
        name, data = event.name, event.data
        if name == "rcon.connected":
            state.connected, state.version = True, data.get("version")
            self._refresh_now[state.name].set()
        elif name == "rcon.disconnected":
            state.connected = False
        elif name == "rcon.lagged":
            self._refresh_now[state.name].set()
        elif name == "rcon.join":
            number = int(data.get("number", 0))
            state.players[number] = Player(number, str(data.get("name", "")), str(data.get("id", "")).lower(),
                                           data.get("ip"), bool(data.get("admin")))
        elif name == "rcon.leave":
            state.players.pop(int(data.get("number", -1)), None)
        elif name in ("rcon.mute", "rcon.unmute"):
            player = state.player_by_id(str(data.get("id", "")))
            if player:
                player.muted = name == "rcon.mute"
        elif name == "rcon.admin_added" or name == "rcon.admin_removed":
            player_id = str(data.get("id", "")).lower()
            added = name == "rcon.admin_added"
            player = state.player_by_id(player_id)
            if player:
                player.admin = added
            if state.admins is not None and player_id:
                (state.admins.add if added else state.admins.discard)(player_id)
        elif name == "rcon.control" and data.get("action") == "reload":
            self._refresh_now[state.name].set()  # dedicated.toml changed: its admins may have too
        elif name == "rcon.phase":
            if data.get("phase") != state.phase:
                state.phase, state.phase_since = data.get("phase"), event.time
            state.map, state.mode = data.get("map", state.map), data.get("mode", state.mode)

    async def run(self) -> None:
        await asyncio.gather(*(self._refresh_loop(name) for name in self.clients))

    async def _refresh_loop(self, name: str) -> None:
        wake = self._refresh_now[name]
        while True:
            try:
                await asyncio.wait_for(wake.wait(), self.refresh_seconds)
            except TimeoutError:
                pass
            wake.clear()
            if self.clients[name].connected:
                await self.refresh(name)

    async def refresh(self, name: str) -> None:
        client, state = self.clients[name], self.states[name]
        try:
            started = time.monotonic()
            status = await client.command("status")
            state.rcon_latency_ms = (time.monotonic() - started) * 1000
            players = await client.command("players")
            admins = await client.command("admin")
        except RconError as error:
            log.debug("[%s] refresh failed: %s", name, error)
            return
        state.last_rcon_ok = time.monotonic()
        if isinstance(status.data, dict):
            self._apply_status(state, status.data)
        if isinstance(players.data, dict):
            self._apply_players(state, players.data)
        if admins.ok and isinstance(admins.data, dict):
            state.admins = self.admin_ids(admins.data)

    @staticmethod
    def admin_ids(data: dict[str, Any]) -> set[str]:
        """Player IDs from RCON `admin`: {"configured": [...], "added": [{"id": …}, …]}."""
        ids = set()
        for entry in [*(data.get("configured") or []), *(data.get("added") or [])]:
            value = entry.get("id") if isinstance(entry, dict) else entry
            if isinstance(value, str) and PLAYER_ID.fullmatch(value):
                ids.add(value.lower())
        return ids

    @staticmethod
    def _apply_status(state: ServerState, data: dict[str, Any]) -> None:
        if data.get("phase") and data["phase"] != state.phase:
            state.phase, state.phase_since = data["phase"], time.time()
        state.map, state.mode = data.get("map", state.map), data.get("mode", state.mode)
        state.max_players = data.get("max_players", state.max_players)

    @staticmethod
    def _apply_players(state: ServerState, data: dict[str, Any]) -> None:
        fresh: dict[int, Player] = {}
        for entry in data.get("players") or []:
            number = int(entry.get("number", 0))
            old = state.players.get(number)
            fresh[number] = Player(
                number=number,
                name=str(entry.get("name", "")),
                id=str(entry.get("id", "")).lower(),
                ip=entry.get("ip"),
                admin=bool(entry.get("admin")),
                muted=bool(entry.get("muted")),
                team=entry.get("team"),
                joined=old.joined if old and old.id == str(entry.get("id", "")).lower() else time.time(),
            )
        state.players = fresh
        state.max_players = data.get("max_players", state.max_players)
