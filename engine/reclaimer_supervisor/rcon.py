"""One signed-in RCON WebSocket connection per server, shared by every part of the supervisor.

Rules from the server's RCON.md and our own tests:
- A server takes 4 signed-in tools, plus one spot kept for its own machine. We sign
  in on that spot with the key in data/<server>/rcon-host.key, so moderators keep all 4.
  A stale key still signs in, silently on a normal spot, so after signing in we read
  the key again and reconnect once if it changed (it is rewritten at every start).
- 5 wrong passwords in 10 minutes lock the address out: every tool on this machine.
  A refused password is never retried.
- A command is never sent twice: after a dropped connection it may already have run.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .config import ServerConfig
from .events import Event, EventBus

log = logging.getLogger(__name__)

PROTOCOL = 1  # the RCON protocol version this code was written for
MAX_MESSAGE = 16 * 1024  # the server's limit for one WebSocket message
BACKOFF = (1, 2, 5, 10, 30)


class RconError(Exception):
    pass


class RconUnavailable(RconError):
    """Not connected right now; the command was not sent."""


class RconTimeout(RconError):
    """Sent, but no reply in time. It may still have run: don't resend it."""


class RconAuthError(RconError):
    """The server refused the password."""


class RconFull(RconError):
    """Every spot on the console is taken."""


@dataclass(frozen=True)
class Reply:
    ok: bool
    text: str
    data: Any
    raw: dict[str, Any]


class RconClient:
    def __init__(self, server: ServerConfig, bus: EventBus, *, use_host_key: bool = True,
                 command_timeout: float = 10.0) -> None:
        self.server = server
        self.bus = bus
        self.use_host_key = use_host_key
        self.command_timeout = command_timeout
        self.version: str | None = None
        self.protocol: int | None = None
        self.host_slot = False
        self.stopped_reason: str | None = None  # set when we gave up for good
        self._ws: ClientConnection | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def run(self) -> None:
        """Stay connected until stop(), reconnecting with backoff."""
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._session()
            except RconAuthError as error:
                self.stopped_reason = f"password refused: {error}"
                log.critical("[%s] RCON refused the password (%s). Not retrying: five wrong "
                             "passwords lock out every tool on this machine. Fix the password "
                             "and restart the supervisor.", self.server.name, error)
                self.bus.emit("rcon.auth_failed", self.server.name, error=str(error))
                return
            except RconFull as error:
                log.warning("[%s] RCON console is full: %s", self.server.name, error)
                self.bus.emit("rcon.full", self.server.name, error=str(error))
                failures = len(BACKOFF) - 1
            except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, ValueError) as error:
                log.debug("[%s] RCON connection: %s: %s", self.server.name, type(error).__name__, error)
            if time.monotonic() - started > 60:
                failures = 0
            if self._stop.is_set():
                break
            delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
            failures += 1
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass
            self._wake.clear()

    def wake(self) -> None:
        """Try to connect now instead of waiting out the backoff (the server just started)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._ws is not None:
            asyncio.ensure_future(self._ws.close())

    async def _session(self) -> None:
        endpoint = self.server.rcon_endpoint()
        if endpoint is None:
            raise ValueError("no RCON password: set [rcon] password in dedicated.toml")
        key = self.server.host_key() if self.use_host_key else None
        url = f"ws://{endpoint.host}:{endpoint.port}/"
        async with connect(url, subprotocols=["reclaimer-rcon"], open_timeout=5,
                           max_size=1 << 20, ping_interval=20, ping_timeout=20) as ws:
            auth: dict[str, Any] = {"type": "auth", "password": endpoint.password}
            if key:
                auth["host_key"] = key
            await ws.send(json.dumps(auth))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if not reply.get("ok"):
                if reply.get("full"):
                    raise RconFull(reply.get("error", "full"))
                raise RconAuthError(reply.get("error", "refused"))
            if self.use_host_key and self.server.host_key() != key:
                # The server (re)started and wrote its key while we signed in: we are on
                # a normal spot. Sign in again with the new key.
                log.info("[%s] RCON host key changed while signing in; reconnecting", self.server.name)
                return
            self._ws = ws
            self.version = reply.get("version")
            self.protocol = reply.get("protocol")
            self.host_slot = bool(key)
            if self.protocol != PROTOCOL:
                log.warning("[%s] RCON protocol %s (this supervisor knows %s); some messages may "
                            "be misread", self.server.name, self.protocol, PROTOCOL)
            log.info("[%s] RCON connected (server %s)", self.server.name, self.version)
            self.bus.emit("rcon.connected", self.server.name, version=self.version,
                          protocol=self.protocol, host_slot=self.host_slot)
            # Signed in before a just-started server wrote its key: move to the kept spot
            # as soon as the key appears, so we don't hold one of the moderators' spots.
            upgrade = (asyncio.ensure_future(self._await_host_key(ws))
                       if self.use_host_key and not key else None)
            try:
                async for raw in ws:
                    self._dispatch(raw)
            finally:
                if upgrade:
                    upgrade.cancel()
                self._ws = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(RconUnavailable("the connection dropped"))
                self._pending.clear()
                log.info("[%s] RCON disconnected", self.server.name)
                self.bus.emit("rcon.disconnected", self.server.name)

    async def _await_host_key(self, ws: ClientConnection, poll: float = 2.0, give_up: float = 120.0) -> None:
        waited = 0.0
        while waited < give_up:
            await asyncio.sleep(poll)
            waited += poll
            if self.server.host_key():
                log.info("[%s] RCON host key appeared; moving to the spot kept for this machine",
                         self.server.name)
                await ws.close()
                return

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            log.warning("[%s] RCON sent something that isn't JSON: %r", self.server.name, raw[:200])
            return
        kind = message.get("type")
        if kind == "reply":
            future = self._pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(message)
        elif kind == "event":
            data = {k: v for k, v in message.items() if k not in ("type", "event", "time")}
            name = str(message.get("event", "unknown"))
            self.bus.publish(Event(f"rcon.{name}", self.server.name, data,
                                   float(message.get("time") or time.time())))
        elif kind == "lagged":
            log.warning("[%s] RCON: fell behind and missed %s events", self.server.name, message.get("missed"))
            self.bus.emit("rcon.lagged", self.server.name, missed=message.get("missed"))
        elif kind == "error":
            log.warning("[%s] RCON error: %s", self.server.name, message.get("text"))
        else:
            # A message type this version doesn't know: pass it on rather than drop it.
            self.bus.emit("rcon.unknown", self.server.name, message=message)

    async def command(self, command: str, *args: str, by: str | None = None,
                      timeout: float | None = None) -> Reply:
        """Run one command. With args, `command` is the name alone and args its words."""
        ws = self._ws
        if ws is None:
            raise RconUnavailable(f"{self.server.name} isn't connected")
        request: dict[str, Any] = {"type": "command", "id": next(self._ids), "command": command}
        if args:
            request["args"] = [str(a) for a in args]
        if by:
            request["by"] = by
        encoded = json.dumps(request, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > MAX_MESSAGE:
            raise ValueError("command too long for one RCON message (16 KiB)")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request["id"]] = future
        try:
            await ws.send(encoded)
        except ConnectionClosed:
            self._pending.pop(request["id"], None)
            raise RconUnavailable(f"{self.server.name} disconnected") from None
        try:
            reply = await asyncio.wait_for(future, timeout or self.command_timeout)
        except TimeoutError:
            self._pending.pop(request["id"], None)
            raise RconTimeout(f"no reply to '{command}' from {self.server.name}") from None
        return Reply(bool(reply.get("ok")), str(reply.get("text", "")), reply.get("data"), reply)


async def one_shot(server: ServerConfig, command: str, *args: str, by: str | None = None,
                   timeout: float = 10.0) -> Reply:
    """Sign in, run one command, sign out. Uses a normal spot: a running supervisor
    holds the one kept for this machine."""
    endpoint = server.rcon_endpoint()
    if endpoint is None:
        raise RconError(f"{server.name} has no RCON password: set [rcon] password in dedicated.toml")
    url = f"ws://{endpoint.host}:{endpoint.port}/"
    async with connect(url, subprotocols=["reclaimer-rcon"], open_timeout=5, max_size=1 << 20) as ws:
        await ws.send(json.dumps({"type": "auth", "password": endpoint.password}))
        auth = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if not auth.get("ok"):
            raise (RconFull if auth.get("full") else RconAuthError)(auth.get("error", "refused"))
        request: dict[str, Any] = {"type": "command", "id": 1, "command": command}
        if args:
            request["args"] = list(args)
        if by:
            request["by"] = by
        await ws.send(json.dumps(request, ensure_ascii=False))
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if message.get("type") == "reply" and message.get("id") == 1:
                return Reply(bool(message.get("ok")), str(message.get("text", "")), message.get("data"), message)
            if message.get("type") == "error":
                raise RconError(str(message.get("text")))
