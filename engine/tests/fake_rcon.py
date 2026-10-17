"""A stand-in for a server's WebSocket RCON, speaking the same messages as v0.9.18."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.server import ServerConnection, serve

PASSWORD = "correct horse battery staple"


class FakeRcon:
    def __init__(self, password: str = PASSWORD, *, full: bool = False,
                 rotate_key: Path | None = None, port: int = 0) -> None:
        self.password = password
        self.full = full
        self.rotate_key = rotate_key  # rewrite this key file during the first sign-in
        self.auths: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.replies: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {}  # None: no reply
        self.players: list[dict[str, Any]] = []
        self.admins: list[str] = []  # added with RCON `admin add`
        self.status: dict[str, Any] = {"name": "LG Test Server", "phase": "no_game", "map": "Guardian",
                                       "mode": "Slayer", "players": 0, "max_players": 16}
        self.connections: set[ServerConnection] = set()
        self.port = port  # 0: any free port
        self._server: Any = None

    async def __aenter__(self) -> FakeRcon:
        self._server = await serve(self._handle, "127.0.0.1", self.port, subprotocols=["reclaimer-rcon"])
        self.port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._server.close()
        await self._server.wait_closed()

    def signed_in(self) -> int:
        return len(self.connections)

    async def _handle(self, ws: ServerConnection) -> None:
        auth = json.loads(await ws.recv())
        self.auths.append(auth)
        if auth.get("password") != self.password:
            await ws.send(json.dumps({"type": "auth", "ok": False, "error": "Wrong password."}))
            return
        if self.full:
            await ws.send(json.dumps({"type": "auth", "ok": False, "full": True,
                                      "error": "The remote console is full: 4 tools are connected."}))
            return
        if self.rotate_key is not None:
            self.rotate_key.write_text("f" * 64, encoding="utf-8")
            self.rotate_key = None
        await ws.send(json.dumps({"type": "auth", "ok": True, "protocol": 1, "server": "LG Test Server",
                                  "version": "0.9.18"}))
        self.connections.add(ws)
        try:
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") != "command":
                    await ws.send(json.dumps({"type": "error", "text": "Not a command"}))
                    continue
                self.commands.append(message)
                reply = self._reply(message)
                if reply is not None:
                    await ws.send(json.dumps({"type": "reply", "id": message.get("id"), **reply}))
        except Exception:  # noqa: BLE001 - a dropped client is fine
            pass
        finally:
            self.connections.discard(ws)

    def _reply(self, message: dict[str, Any]) -> dict[str, Any] | None:
        name = str(message["command"]).split()[0]
        if name in self.replies:
            return self.replies[name](message)
        if name == "status":
            return {"ok": True, "text": "status", "data": {**self.status, "players": len(self.players)}}
        if name == "admin" and not message.get("args"):
            return {"ok": True, "text": "admins", "data": {"configured": [], "added": [
                {"id": a, "added_at": 0, "added_by": "RCON"} for a in self.admins]}}
        if name == "players":
            return {"ok": True, "text": f"{len(self.players)} players",
                    "data": {"count": len(self.players), "max_players": 16, "players": self.players}}
        return {"ok": True, "text": f"Done: {name} {' '.join(message.get('args', []))}".strip()}

    def named(self, name: str) -> list[dict[str, Any]]:
        """Commands received with this name."""
        return [c for c in self.commands if str(c["command"]).split()[0] == name]

    async def event(self, event: str, **data: Any) -> None:
        message = json.dumps({"type": "event", "event": event, "time": int(time.time()), **data})
        for ws in list(self.connections):
            await ws.send(message)

    async def join(self, number: int, name: str, player_id: str, *, admin: bool = False) -> None:
        self.players.append({"number": number, "name": name, "id": player_id, "ip": "203.0.113.%d" % number,
                             "admin": admin, "muted": False, "team": None})
        await self.event("join", number=number, name=name, id=player_id, ip="203.0.113.%d" % number, admin=admin)

    async def chat(self, player_id: str, name: str, text: str, channel: str = "all") -> None:
        await self.event("chat", channel=channel, name=name, id=player_id, text=text)

    async def drop(self) -> None:
        for ws in list(self.connections):
            await ws.close()
