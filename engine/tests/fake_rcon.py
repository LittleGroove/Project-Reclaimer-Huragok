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
        self.admins_configured: list[str] = []  # dedicated.toml's admins: `admin remove` can't take them off
        self.immune: list[dict[str, Any]] = []  # vote-kick immune, added with RCON `immune add`: id, added_by
        self.immune_configured: list[str] = []  # dedicated.toml's vote_kick_immune
        self.has_immune = True  # False: a server program from before the immune list
        self.ban_list: list[dict[str, Any]] = []  # player bans from `ban <ID> …`, as `bans` lists them; `unban` lifts
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
                await self._announce(message)
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
            return {"ok": True, "text": "admins", "data": {"configured": list(self.admins_configured), "added": [
                {"id": a, "added_at": 0, "added_by": "RCON"} for a in self.admins]}}
        words = [str(w) for w in message.get("args") or str(message["command"]).split()[1:]]
        configured = len(words) == 2 and words[1].lower() in self.admins_configured
        if name == "admin" and words[:1] == ["remove"] and configured:
            return {"ok": True, "text": f"Removed {words[1]} from the console's admins; dedicated.toml still makes "
                                        "them one."}
        if name == "immune":
            return self._immune(message)
        if name == "players":
            return {"ok": True, "text": f"{len(self.players)} players",
                    "data": {"count": len(self.players), "max_players": 16, "players": self.players}}
        if name in ("ban", "unban", "bans"):
            self._bans(name, [str(w) for w in message.get("args") or str(message["command"]).split()[1:]],
                       message.get("by"))
            if name == "bans":
                return {"ok": True, "text": f"{len(self.ban_list)} bans",
                        "data": {"players": [dict(e) for e in self.ban_list], "ips": [], "devices": []}}
        return {"ok": True, "text": f"Done: {name} {' '.join(message.get('args', []))}".strip()}

    async def _announce(self, message: dict[str, Any]) -> None:
        """The events a real server sends after some commands: a ban, an admin added or removed."""
        name = str(message["command"]).split()[0]
        words = [str(w) for w in message.get("args") or str(message["command"]).split()[1:]]
        by = f"{message['by']} (RCON)" if message.get("by") else "RCON"
        if name == "ban" and words:
            target = words[0].lower()
            player = next((p for p in self.players if p["id"] == target or f"#{p['number']}" == target), None)
            await self.event("ban", id=player["id"] if player else target, name=player["name"] if player else "",
                             by=by, reason="", expires=0)
        elif name == "admin" and len(words) == 2 and words[0] in ("add", "remove"):
            target = words[1].lower()
            if words[0] == "add" and target not in self.admins:
                self.admins.append(target)
            elif words[0] == "remove" and target in self.admins:
                self.admins.remove(target)
            else:
                return
            for p in self.players:
                if p["id"] == target:
                    p["admin"] = words[0] == "add" or target in self.admins_configured
            await self.event("admin_added" if words[0] == "add" else "admin_removed", id=target, by=by)

    def _bans(self, name: str, words: list[str], by: str | None) -> None:
        """The ban list as far as tests need it: bans by player ID or #number, unbans by player ID."""
        if not words:
            return
        target = words[0].lower()
        if target.startswith("#"):
            target = next((str(p["id"]) for p in self.players if f"#{p['number']}" == target), target)
        if name == "unban":
            self.ban_list = [e for e in self.ban_list if e["id"] != target]
        elif name == "ban":
            timed = len(words) > 1 and words[1][:-1].isdigit() and words[1][-1:] in "smhdw"
            entry = {"id": target, "name": next((p["name"] for p in self.players if p["id"] == target), ""),
                     "reason": " ".join(words[2:] if timed else words[1:]),
                     "banned_by": f"{by} (RCON)" if by else "RCON", "banned_at": int(time.time())}
            if timed:
                unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[words[1][-1]]
                entry["expires"] = int(time.time()) + int(words[1][:-1]) * unit
            self.ban_list.append(entry)

    def _immune(self, message: dict[str, Any]) -> dict[str, Any]:
        """`immune [list | add <ID> | remove <ID>]`, answering as 0.9.20 does (players named by ID only)."""
        if not self.has_immune:
            return {"ok": False, "text": "Unknown command 'immune'; help lists them."}
        words = [str(w) for w in message.get("args") or str(message["command"]).split()[1:]]
        if not words or words[0] == "list":
            return {"ok": True, "text": "immune", "data": {"configured": list(self.immune_configured),
                                                            "added": [dict(e) for e in self.immune]}}
        target = words[1].lower()
        known = [e for e in self.immune if e["id"] == target]
        if words[0] == "add":
            if not known:
                self.immune.append({"id": target, "added_by": f"{message['by']} (RCON)" if message.get("by")
                                    else "RCON", "added_at": int(time.time())})
            return {"ok": True, "text": f"{target} can't be vote-kicked on any server sharing the ban list now."}
        if not known:
            return {"ok": False, "text": f"The console never made {target} vote-kick immune; immune lists who it did."}
        self.immune.remove(known[0])
        return {"ok": True, "text": f"{target} can be vote-kicked again on every server sharing the ban list."}

    def immune_ids(self) -> set[str]:
        return {e["id"] for e in self.immune}

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

    async def leave(self, number: int) -> None:
        """Player `number` leaves: gone from `players`, and the `leave` event."""
        gone = next((p for p in self.players if p["number"] == number), None)
        self.players = [p for p in self.players if p["number"] != number]
        await self.event("leave", number=number, name=gone["name"] if gone else "", id=gone["id"] if gone else "")

    async def drop(self) -> None:
        for ws in list(self.connections):
            await ws.close()
