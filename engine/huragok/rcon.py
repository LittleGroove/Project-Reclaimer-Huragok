"""One signed-in RCON WebSocket connection per server, shared by every part of the supervisor.

Rules from the server's RCON.md and our own tests:
- A server takes 4 signed-in tools, plus one spot kept for its own machine. We sign
  in on that spot with the key in data/<server>/rcon-host.key, so moderators keep all 4.
  A stale key still signs in, silently on a normal spot, so after signing in we read
  the key again and reconnect once if it changed (it is rewritten at every start).
- 5 wrong passwords in 10 minutes lock the address out: every tool on this machine,
  right password or not, until the oldest of them is 10 minutes old. So a refused
  password is tried again only when trying can't add up to a lockout: at once when
  the password in the files changes, once after the server starts again (a restart
  rewrites instance.json, and a server's lockouts don't outlive it), and otherwise
  once every 10 minutes (by then a lockout has ended). Never twice in a row for one
  start, so a server that keeps restarting can't make Huragok lock the machine out.
- A command is never sent twice: after a dropped connection it may already have run.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

from .core.background import background
from .core.errors import ServerError
from .core.events import Event, EventBus
from .core.serverfolders import ServerConfig

log = logging.getLogger(__name__)

PROTOCOL = 1  # the RCON protocol version this code was written for
MAX_MESSAGE = 16 * 1024  # the server's limit for one WebSocket message
BACKOFF = (1, 2, 5, 10, 30)
SECRET_COMMANDS = ("password",)  # console commands whose argument is a password (RCON.md: the join password)
REFUSED_RETRY = 600.0  # seconds before a refused password is tried again: a lockout lasts 10 minutes
REFUSED_POLL = 5.0  # how often a refused client looks for a new password in the files
REFUSED_PER_WINDOW = 2  # refusals in REFUSED_RETRY after which only the clock or a new password tries again
LOCKOUT_WORDS = ("too many", "locked", "refusing", "try again", "minutes")  # a lockout, as the refusal says it
# websockets 15+ reads the system's proxy settings for every connection; the console is never behind one.
NO_PROXY: dict[str, Any] = {"proxy": None} if "proxy" in inspect.signature(connect).parameters else {}


def url_for(host: str, port: int) -> str:
    """ws://host:port/, with an IPv6 address in brackets as URLs need it."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"ws://{host}:{port}/"


def is_lockout(error: str) -> bool:
    """Whether a refusal says the address is locked out, rather than that the password was wrong."""
    text = error.lower()
    return any(word in text for word in LOCKOUT_WORDS)


def masked(line: str) -> str:
    """A console line as logs and transcripts keep it: `password ********` for one that sets a password."""
    words = line.strip().split(maxsplit=1)  # any whitespace: a tab before the password hides nothing
    return f"{words[0]} ********" if len(words) == 2 and words[0].lower() in SECRET_COMMANDS else line


class RconError(ServerError):
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
        self.stopped_reason: str | None = None  # set while the console can't be used (its password refused)
        self._ws: ClientConnection | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._password: str | None = None  # the password the last sign-in tried
        self._refusals: deque[float] = deque()  # when (monotonic) recent sign-ins were refused
        self._lockout_seen: float | None = None  # when (monotonic) the server's log last said it locked an address out
        self._unexpected: set[str] = set()  # error types already logged with their traceback
        bus.subscribe("log.rcon_wrong_password", self._on_wrong_password)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def run(self) -> None:
        """Stay connected until stop(), reconnecting with backoff; a refused password waits (see the top)."""
        failures = 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except RconAuthError as error:
                refused = self._password
                self._refused(str(error))
                await self._wait_after_refusal(refused)
                failures = 0
                continue
            except RconFull as error:
                log.warning("[%s] RCON console is full: %s", self.server.name, error)
                self.bus.emit("rcon.full", self.server.name, error=str(error))
                failures = len(BACKOFF) - 1
            except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake, WebSocketException,
                    ValueError) as error:
                log.debug("[%s] RCON connection: %s: %s", self.server.name, type(error).__name__, error)
            except Exception as error:  # noqa: BLE001 - one odd message must not end the console for good
                kind = type(error).__name__
                if kind not in self._unexpected:
                    self._unexpected.add(kind)
                    log.warning("[%s] RCON connection failed unexpectedly (%s: %s); reconnecting",
                                self.server.name, kind, error, exc_info=True)
                else:
                    log.debug("[%s] RCON connection: %s: %s", self.server.name, kind, error)
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

    def _on_wrong_password(self, event: Event) -> None:
        """The server's log said a tool gave a wrong password; at the fifth it locks the tool's address out."""
        if event.server != self.server.name or not event.get("minutes") or self._stop.is_set():
            return
        self._lockout_seen = time.monotonic()
        if self.stopped_reason:
            self.stopped_reason = f"this machine is locked out of the console for {event.get('minutes')} minutes"
            log.error("[%s] the server locked %s out of its console for %s minutes after %s wrong passwords (its log "
                      "says so), so it refuses Huragok too, right password or not. Huragok tries again once the "
                      "lockout is over.", self.server.name, event.get("refused") or event.get("address"),
                      event.get("minutes"), event.get("times") or 5)

    def _refused(self, error: str) -> None:
        now = time.monotonic()
        self._refusals.append(now)
        while self._refusals and now - self._refusals[0] >= REFUSED_RETRY:
            self._refusals.popleft()
        name = self.server.name
        locked = is_lockout(error) or (self._lockout_seen is not None and now - self._lockout_seen < REFUSED_RETRY)
        if locked:
            self.stopped_reason = f"this machine is locked out of the console: {error}"
            log.error("[%s] RCON turned Huragok away (%s): this machine's address is locked out after five wrong "
                      "passwords from tools on it, right password or not. Huragok tries again in 10 minutes, when "
                      "the lockout has ended, or as soon as the server restarts. Until then it can't moderate this "
                      "server or warn its players.", name, error)
        else:
            self.stopped_reason = f"password refused: {error}"
            log.error("[%s] RCON refused the password (%s). Huragok won't keep trying it: five wrong passwords lock "
                      "out every tool on this machine. It tries again as soon as data/%s/instance.json names a "
                      "new one (the server writes it as it starts: a new password in dedicated.toml counts from "
                      "the server's next start), once after the server restarts, and every 10 minutes. "
                      "If the password is right, another tool on this machine may have locked this address out "
                      "with wrong ones.", name, error, self.server.slug)
        self.bus.emit("rcon.auth_failed", name, error=str(error), lockout=locked)

    async def _wait_after_refusal(self, refused: str | None) -> None:
        """Until trying again can't add up to a lockout: a new password, the server starting again, 10 minutes."""
        self._wake.clear()
        since = time.monotonic()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), REFUSED_POLL)
            except TimeoutError:
                pass
            woken = self._wake.is_set()  # also a wake that came as the wait ran out
            self._wake.clear()
            if self._stop.is_set():
                return
            endpoint = self.server.rcon_endpoint()
            if endpoint is not None and endpoint.password != refused:
                log.info("[%s] RCON password changed; signing in with the new one", self.server.name)
                return
            now = time.monotonic()
            recent = [t for t in self._refusals if now - t < REFUSED_RETRY]
            if woken and len(recent) < REFUSED_PER_WINDOW:
                log.info("[%s] the server started again; trying its console once more", self.server.name)
                return
            if now - since >= REFUSED_RETRY:
                log.info("[%s] trying the console again, 10 minutes after it was refused", self.server.name)
                return

    def wake(self) -> None:
        """Try to connect now instead of waiting out the backoff (the server just started)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._ws is not None:
            background(self._ws.close(), f"closing {self.server.name}'s console connection", log)

    async def _session(self) -> None:
        endpoint = self.server.rcon_endpoint()
        if endpoint is None:
            raise ValueError("no RCON password: set [rcon] password in dedicated.toml")
        key = self.server.host_key() if self.use_host_key else None
        async with connect(url_for(endpoint.host, endpoint.port), subprotocols=["reclaimer-rcon"], open_timeout=5,
                           max_size=1 << 20, ping_interval=20, ping_timeout=20, **NO_PROXY) as ws:
            auth: dict[str, Any] = {"type": "auth", "password": endpoint.password}
            if key:
                auth["host_key"] = key
            self._password = endpoint.password
            await ws.send(json.dumps(auth))
            reply = json.loads(await asyncio.wait_for(ws.recv(), 10))
            if not isinstance(reply, dict):
                raise ValueError(f"the server answered the sign-in with {reply!r:.80}")
            if not reply.get("ok"):
                if reply.get("full"):
                    raise RconFull(reply.get("error", "full"))
                raise RconAuthError(reply.get("error", "refused"))
            if self.stopped_reason:
                log.info("[%s] RCON took the password again; the console is back", self.server.name)
            self.stopped_reason = None
            self._refusals.clear()
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
                log.warning("[%s] RCON protocol %s (this Huragok knows %s); some messages may "
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
        if not isinstance(message, dict):
            log.warning("[%s] RCON sent JSON that isn't a message: %r", self.server.name, raw[:200])
            return
        kind = message.get("type")
        if kind == "reply":
            future = self._pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(message)
        elif kind == "event":
            data = {k: v for k, v in message.items() if k not in ("type", "event", "time")}
            name = str(message.get("event", "unknown"))
            when = message.get("time")
            self.bus.publish(Event(f"rcon.{name}", self.server.name, data,
                                   float(when) if isinstance(when, (int, float)) and when else time.time()))
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
    holds the one kept for this machine. A connection that breaks, a handshake something else on the port answers,
    or a message that isn't JSON is an RconError, as RconClient takes them: `say` to every server goes on to the
    next one."""
    endpoint = server.rcon_endpoint()
    if endpoint is None:
        raise RconError(f"{server.name} has no RCON password: set [rcon] password in dedicated.toml")
    try:
        return await _one_shot(endpoint, command, args, by, timeout)
    except (WebSocketException, ValueError) as error:  # ConnectionClosed and InvalidHandshake among them
        raise RconError(f"the console connection failed ({type(error).__name__}: {error})") from None


async def _one_shot(endpoint: Any, command: str, args: tuple[str, ...], by: str | None, timeout: float) -> Reply:
    async with connect(url_for(endpoint.host, endpoint.port), subprotocols=["reclaimer-rcon"], open_timeout=5,
                       max_size=1 << 20, **NO_PROXY) as ws:
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


async def ban_list(client: RconClient, *, strict: bool = False) -> tuple[dict[str, list[dict[str, Any]]] | None, str]:
    """The server's ban list, as its console's `bans` gives it: {"players": [...], "ips": [...]}, every entry a dict
    (a player's has `id`, an address's `ip`; both `banned_by`, `reason`, `banned_at`, `group`...). None and why when
    it couldn't be read; `strict`: also when the answer has no list of players (lifting a ban mustn't take that for
    "not banned")."""
    try:
        reply = await client.command("bans")
    except RconError as error:
        return None, str(error)
    if not reply.ok or not isinstance(reply.data, dict):
        return None, reply.text
    if strict and not isinstance(reply.data.get("players"), list):
        return None, reply.text or "the answer has no list of players"
    return {kind: [e for e in reply.data.get(kind) or [] if isinstance(e, dict)] for kind in ("players", "ips")}, ""
