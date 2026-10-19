"""A live picture of each server: players, phase, map and mode, kept by events.

RCON events keep it current; a `players` + `status` call on connecting, after a
`lagged`, and every 30 s corrects any drift and doubles as a health check. Each
reply is applied as it comes, and an event that arrives while a reply is on its
way wins over it: the reply may have been made before the event happened.

While RCON is disconnected the picture stays as it was last seen (the window shows
it), but `connected` is False: nothing should be judged on it then.
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
    next_game: str | None = None  # a `nextmap` override, from `status`'s `next`: "Valhalla Slayer"
    playlist_vote: Any = None  # the playlist ballot under way, as `status` gives it, or None
    players: dict[int, Player] = field(default_factory=dict)
    last_rcon_ok: float | None = None  # monotonic time of the last good reply
    rcon_latency_ms: float | None = None
    status_updated: float | None = None  # status.json's own `updated` (Unix seconds)
    status_seen: float | None = None  # when we last read status.json (Unix seconds)
    pid: int | None = None  # the server's process, when the backend knows it
    admins: set[str] | None = None  # the server's admins (RCON `admin`), None until it answers
    address: str | None = None  # where players join, as the server says it as it starts ("players join at …")
    last_reload: tuple[bool, str, float] | None = None  # (ok, what it said, when) of the last settings reload

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
    """Keeps every ServerState current from RCON events and periodic refreshes. Servers can be added while running."""

    def __init__(self, bus: EventBus, clients: dict[str, RconClient], refresh_seconds: float = 30.0) -> None:
        self.bus = bus
        self.clients = clients
        self.refresh_seconds = refresh_seconds
        self.states: dict[str, ServerState] = {}
        self._refresh_now: dict[str, asyncio.Event] = {}
        self._loops: dict[str, asyncio.Task[None]] | None = None  # None until run()
        self._meanwhile: dict[str, list[Event]] = {}  # join, leave and phase events while a refresh is under way
        for name, client in list(clients.items()):
            self.add(name, client)
        bus.subscribe("rcon.*", self._on_event)

    def add(self, name: str, client: RconClient) -> ServerState:
        self.clients[name] = client
        state = self.states.get(name) or ServerState(name)
        self.states[name] = state
        self._refresh_now.setdefault(name, asyncio.Event())
        if self._loops is not None and name not in self._loops:
            self._loops[name] = asyncio.ensure_future(self._refresh_loop(name))
        return state

    def remove(self, name: str) -> None:
        if self._loops is not None and (task := self._loops.pop(name, None)):
            task.cancel()
        self.states.pop(name, None)
        self._refresh_now.pop(name, None)
        self.clients.pop(name, None)

    def _on_event(self, event: Event) -> None:
        state = self.states.get(event.server or "")
        if state is None:
            return
        name, data = event.name, event.data
        if name in ("rcon.join", "rcon.leave", "rcon.phase") and state.name in self._meanwhile:
            self._meanwhile[state.name].append(event)
        if name == "rcon.connected":
            state.connected, state.version = True, data.get("version")
            self._refresh_now[state.name].set()
        elif name == "rcon.disconnected":
            state.connected = False
        elif name == "rcon.lagged":
            self._refresh_now[state.name].set()
        elif name in ("rcon.join", "rcon.leave"):
            self._apply_player_event(state, event)
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
        self._loops = {name: asyncio.ensure_future(self._refresh_loop(name)) for name in self.clients}
        try:
            await asyncio.Event().wait()
        finally:
            for task in self._loops.values():
                task.cancel()
            self._loops = None

    async def _refresh_loop(self, name: str) -> None:
        wake = self._refresh_now[name]
        while True:
            try:
                await asyncio.wait_for(wake.wait(), self.refresh_seconds)
            except TimeoutError:
                pass
            wake.clear()
            client = self.clients.get(name)
            if client is not None and client.connected:
                try:
                    await self.refresh(name)
                except Exception as error:  # noqa: BLE001 - the keepalive's RCON signal depends on this loop living
                    log.warning("[%s] couldn't read the server's state: %s: %s", name, type(error).__name__, error)

    async def refresh(self, name: str) -> None:
        client, state = self.clients[name], self.states[name]
        meanwhile = self._meanwhile[name] = []
        try:
            started = time.monotonic()
            status = await client.command("status")
            state.rcon_latency_ms = (time.monotonic() - started) * 1000
            state.last_rcon_ok = time.monotonic()
            if isinstance(status.data, dict):
                # A phase event since we asked is newer than the reply: keep it (and when it started).
                self._apply_status(state, status.data, phase=not any(e.name == "rcon.phase" for e in meanwhile))
            meanwhile.clear()
            players = await client.command("players")
            state.last_rcon_ok = time.monotonic()
            if isinstance(players.data, dict):
                self._apply_players(state, players.data)
                for event in meanwhile:  # joins and leaves since we asked happened after the reply was made
                    if event.name != "rcon.phase":
                        self._apply_player_event(state, event)
            admins = await client.command("admin")
            state.last_rcon_ok = time.monotonic()
            if admins.ok and isinstance(admins.data, dict):
                state.admins = self.admin_ids(admins.data)
        except RconError as error:
            log.debug("[%s] refresh failed: %s", name, error)
        finally:
            if self._meanwhile.get(name) is meanwhile:
                del self._meanwhile[name]

    @staticmethod
    def _apply_player_event(state: ServerState, event: Event) -> None:
        data = event.data
        if event.name == "rcon.join":
            number = int(data.get("number", 0))
            state.players[number] = Player(number, str(data.get("name", "")), str(data.get("id", "")).lower(),
                                           data.get("ip"), bool(data.get("admin")))
        elif event.name == "rcon.leave":
            state.players.pop(int(data.get("number", -1)), None)

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
    def _apply_status(state: ServerState, data: dict[str, Any], phase: bool = True) -> None:
        if phase:
            if data.get("phase") and data["phase"] != state.phase:
                state.phase, state.phase_since = data["phase"], time.time()
            state.map, state.mode = data.get("map", state.map), data.get("mode", state.mode)
        state.max_players = data.get("max_players", state.max_players)
        state.next_game = describe_game(data.get("next"))
        state.playlist_vote = data.get("playlist_vote") or None

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


def describe_game(value: Any) -> str | None:
    """A game as `status`/`nextmap` describe one ({"map": …, "mode": …, "game": …}, or text) -> "Valhalla Slayer"."""
    if not value:
        return None
    if isinstance(value, dict):
        words = [str(value.get("map") or ""), str(value.get("game") or value.get("mode") or "")]
        return " ".join(w for w in words if w) or None
    return str(value)
