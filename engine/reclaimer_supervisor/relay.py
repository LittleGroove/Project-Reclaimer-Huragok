"""The console relay: moderators elsewhere use the official RCON client (or any tool speaking the
server's console protocols) through the supervisor, each with a password and name of their own.

For each server the supervisor listens at the server's game port plus [relay] port_offset
(49176 -> 50176) speaking the server's own WebSocket protocol (RCON.md, protocol 1):

    {"type": "auth", "password": "…"}                      -> {"type": "auth", "ok": true, "server": …, "version": …}
    {"type": "command", "id": 7, "command": "kick #3 x"}  -> {"type": "reply", "id": 7, "ok": …, "text": …, "data": …}
    events, as the server sends them                       -> {"type": "event", "event": "chat", …}

and at the game port plus source_port_offset (49176 -> 51176) speaking Source RCON
(Valve's: sign in with the password, send a command, read the text), for the many
tools and panels that speak it. Source RCON has no events.

The password picks the moderator ([[relay.user]]). Only the commands their role allows
pass (the same lists as commands.toml: a moderator can mute, not ban), the server
reads who acted (`by`), every command is logged, one upstream spot serves everyone,
and a wrong password locks nobody out of the server (five in ten minutes lock that
address out of the relay). Not encrypted: keep it on 127.0.0.1 behind an SSH tunnel
or a VPN, like the server's own console.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .events import Event
from .permissions import ROLES, Permissions, all_commands
from .rcon import PROTOCOL, RconError

if TYPE_CHECKING:
    from .config import RelayUser, ServerConfig, Settings
    from .events import EventBus
    from .rcon import RconClient
    from .state import ServerState

log = logging.getLogger(__name__)

LOCKOUT_TRIES, LOCKOUT_SECONDS = 5, 600
READ_ONLY = {"status", "players", "maps", "modes", "vote", "bans", "vpn", "help", "admins"}
QUERIES = {"nextmap", "servername", "maxping", "teamcount", "admin", "password"}  # read-only when typed alone
FIXED = {"tell": "moderator", "banip": "admin", "vpnallow": "admin", "vpnrevoke": "admin", "password": "owner",
         "reload": "owner", "admin": "owner"}

# Source RCON packet types (Valve's protocol)
SOURCE_AUTH, SOURCE_AUTH_RESPONSE, SOURCE_EXEC, SOURCE_RESPONSE = 3, 2, 2, 0
SOURCE_MAX_BODY = 4096
SOURCE_MAX_PACKET = 4096 + 10


def _why(error: BaseException) -> str:
    """An OS error as the host reads it: a port already taken says so, not two nested error numbers."""
    text = str(error)
    if getattr(error, "errno", None) in (errno.EADDRINUSE, 10048) or "10048" in text \
            or "address already in use" in text.lower() or "only one usage of each socket address" in text.lower():
        return "the port is in use (another program, or another supervisor, listens there)"
    return text


def required_role(line: str, permissions: Permissions) -> str:
    """The role a console line needs: commands.toml's role where the command is one of ours, else a fixed one."""
    words = line.strip().split()
    if not words:
        return "moderator"
    word = words[0].lower()
    if word in READ_ONLY or (word in QUERIES and len(words) == 1):
        return "moderator"
    spec = all_commands().get(word)
    if spec is not None and word not in FIXED:
        role = permissions.role_for(spec)
        return role if role != "player" else "moderator"
    return FIXED.get(word, "owner")


def allows(role: str, needed: str) -> bool:
    return ROLES.index(role) >= ROLES.index(needed)


@dataclass
class Session:
    user: RelayUser
    server: str
    address: str
    close: Callable[[], Any]
    protocol: str = "websocket"
    since: float = field(default_factory=time.time)
    commands: int = 0


class Relay:
    def __init__(self, settings: Settings, clients: dict[str, RconClient], states: dict[str, ServerState],
                 bus: EventBus) -> None:
        self.settings = settings
        self.config = settings.relay
        self.clients = clients
        self.states = states
        self.bus = bus
        self.listeners: dict[str, Any] = {}
        self.source_listeners: dict[str, asyncio.AbstractServer] = {}
        self.sessions: dict[str, list[Session]] = {}
        self.failures: dict[str, list[float]] = {}
        self.problems: list[str] = []

    # Listening ---------------------------------------------------------------------------------

    def port_for(self, server: ServerConfig) -> int:
        return server.game_port + self.config.port_offset

    def source_port_for(self, server: ServerConfig) -> int | None:
        return server.game_port + self.config.source_port_offset if self.config.source_port_offset else None

    async def run(self) -> None:
        if not self.config.enabled:
            return
        if not self.config.users:
            log.warning("[relay] enabled, but no [[relay.user]] is listed: nobody can sign in")
        for server in list(self.settings.servers):
            await self.add_server(server)
        try:
            await asyncio.Event().wait()
        finally:
            for name in list(self.listeners) + list(self.source_listeners):
                self.remove_server(name)

    async def add_server(self, server: ServerConfig) -> None:
        if not self.config.enabled or server.name in self.listeners:
            return
        port = self.port_for(server)
        try:
            if not 1 <= port <= 65535:
                raise OSError(f"port {port} is out of range (game port {server.game_port} + port_offset)")
            listener = await serve(lambda ws, s=server: self._handle(s, ws), self.config.address, port,
                                   subprotocols=["reclaimer-rcon"], max_size=1 << 20)
        except (OSError, OverflowError, ValueError) as error:
            problem = f"{server.name}: the console relay can't listen on {self.config.address}:{port}: {_why(error)}"
            log.error("[relay] %s", problem)
            self.problems.append(problem)
            return
        self.listeners[server.name] = listener
        log.info("[%s] console relay listening on %s:%s (moderators connect the RCON client there)",
                 server.name, self.config.address, port)
        source_port = self.source_port_for(server)
        if source_port:
            try:
                if not 1 <= source_port <= 65535:
                    raise OSError(f"port {source_port} is out of range (game port {server.game_port} + "
                                  "source_port_offset)")
                self.source_listeners[server.name] = await asyncio.start_server(
                    lambda reader, writer, s=server: self._source(s, reader, writer), self.config.address, source_port)
                log.info("[%s] Source RCON relay listening on %s:%s", server.name, self.config.address, source_port)
            except (OSError, OverflowError, ValueError) as error:
                problem = (f"{server.name}: the Source RCON relay can't listen on {self.config.address}:{source_port}: "
                           f"{_why(error)}")
                log.error("[relay] %s", problem)
                self.problems.append(problem)

    def remove_server(self, name: str) -> None:
        listener = self.listeners.pop(name, None)
        if listener is not None:
            listener.close()
        source = self.source_listeners.pop(name, None)
        if source is not None:
            source.close()
        for session in list(self.sessions.get(name, [])):
            try:
                result = session.close()
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001
                pass

    def connected(self) -> list[str]:
        """Who's signed in, as "Alice (LG Test Server)"."""
        return [f"{s.user.name} ({s.server})" for sessions in self.sessions.values() for s in sessions]

    # Shared by both protocols -------------------------------------------------------------------

    def _locked(self, address: str) -> bool:
        now = time.time()
        recent = [t for t in self.failures.get(address, []) if now - t < LOCKOUT_SECONDS]
        self.failures[address] = recent
        return len(recent) >= LOCKOUT_TRIES

    def _user_for(self, password: str) -> RelayUser | None:
        return next((u for u in self.config.users if u.password == password), None)

    def _sign_in(self, server: ServerConfig, address: str, password: str, protocol: str,
                 close: Callable[[], Any]) -> tuple[Session | None, str]:
        """(the session, or None and why) for a password from an address."""
        if self._locked(address):
            return None, "Too many wrong passwords from your address; try again in ten minutes."
        user = self._user_for(password)
        if user is None:
            self.failures.setdefault(address, []).append(time.time())
            log.warning("[%s] console relay: wrong password from %s (%s)", server.name, address, protocol)
            return None, "Wrong password."
        session = Session(user, server.name, address, close, protocol)
        self.sessions.setdefault(server.name, []).append(session)
        log.info("[%s] console: %s (%s) signed in from %s through the relay (%s)", server.name, user.name, user.role,
                 address, protocol)
        return session, ""

    def _sign_out(self, session: Session) -> None:
        sessions = self.sessions.get(session.server, [])
        if session in sessions:
            sessions.remove(session)
        log.info("[%s] console: %s signed out (%d command(s))", session.server, session.user.name, session.commands)

    async def _run_command(self, session: Session, server: ServerConfig, command: str,
                           args: list[str] | None = None) -> tuple[bool, str, Any]:
        """(ok, text, data) for a command from a signed-in moderator, after the role check."""
        user = session.user
        line = " ".join([command, *(str(a) for a in args)]) if args else command
        needed = required_role(line, self.settings.permissions)
        if not allows(user.role, needed):
            log.info("[%s] console: %s (%s) may not run: %s (needs %s)", server.name, user.name, user.role, line, needed)
            word = line.split()[0] if line.split() else ""
            return False, f"'{word}' needs the {needed} role; you're {user.role}. Ask the host to change commands.toml.", None
        client = self.clients.get(server.name)
        if client is None or not client.connected:
            return False, f"{server.name}'s console isn't connected right now.", None
        session.commands += 1
        log.info("[%s] console: %s (%s): %s", server.name, user.name, user.role, line)
        try:
            if args:
                reply = await client.command(command, *(str(a) for a in args), by=user.name, timeout=15)
            else:
                reply = await client.command(command, by=user.name, timeout=15)
        except RconError as error:
            return False, str(error), None
        return reply.ok, reply.text, reply.data

    # WebSocket ---------------------------------------------------------------------------------

    async def _send(self, ws: ServerConnection, message: dict[str, Any]) -> None:
        await ws.send(json.dumps(message, ensure_ascii=False))

    async def _handle(self, server: ServerConfig, ws: ServerConnection) -> None:
        address = str((ws.remote_address or ("?",))[0])
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), 10))
        except (TimeoutError, ValueError, ConnectionClosed):
            return
        if not isinstance(first, dict) or first.get("type") != "auth":
            await self._send(ws, {"type": "error", "text": "Sign in first: {\"type\": \"auth\", \"password\": …}"})
            return
        session, why = self._sign_in(server, address, str(first.get("password") or ""), "websocket", ws.close)
        if session is None:
            await self._send(ws, {"type": "auth", "ok": False, "error": why})
            return
        user = session.user
        client = self.clients.get(server.name)
        await self._send(ws, {"type": "auth", "ok": True, "protocol": PROTOCOL, "server": server.name,
                              "version": (client.version if client else None) or "",
                              "relay": self.settings.name, "role": user.role})
        loop = asyncio.get_running_loop()

        def forward(event: Event) -> None:
            if event.server != server.name:
                return
            message = self._event_message(event, user)
            if message is not None:
                loop.create_task(self._send_quietly(ws, message))

        unsubscribe = self.bus.subscribe("rcon.*", forward)
        try:
            async for raw in ws:
                await self._message(session, server, ws, raw)
        except ConnectionClosed:
            pass
        finally:
            unsubscribe()
            self._sign_out(session)

    async def _send_quietly(self, ws: ServerConnection, message: dict[str, Any]) -> None:
        try:
            await self._send(ws, message)
        except ConnectionClosed:
            pass

    def _event_message(self, event: Event, user: RelayUser) -> dict[str, Any] | None:
        name = event.name.removeprefix("rcon.")
        if name in ("connected", "full", "auth_failed", "unknown"):
            return None
        if name == "disconnected":
            return {"type": "event", "event": "control", "time": int(event.time), "action": "relay", "ok": False,
                    "text": "the supervisor's connection to the server's console dropped; commands wait until it's back"}
        if name == "lagged":
            return {"type": "lagged", "missed": event.get("missed")}
        message: dict[str, Any] = {"type": "event", "event": name, "time": int(event.time)}
        for key, value in event.data.items():
            if key == "ip" and not allows(user.role, "admin"):
                continue  # moderators don't get players' addresses
            message[key] = value
        return message

    async def _message(self, session: Session, server: ServerConfig, ws: ServerConnection, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            await self._send(ws, {"type": "error", "text": "Not JSON."})
            return
        if not isinstance(message, dict) or message.get("type") != "command":
            await self._send(ws, {"type": "error", "text": "Not a command: send {\"type\": \"command\", …}."})
            return
        request_id = message.get("id")
        args = message.get("args")
        command = str(message.get("command") or "").strip()
        ok, text, data = await self._run_command(session, server, command,
                                                 [str(a) for a in args] if isinstance(args, list) and args else None)
        answer: dict[str, Any] = {"type": "reply", "id": request_id, "ok": ok, "text": text}
        if data is not None:
            answer["data"] = data
        await self._send(ws, answer)

    # Source RCON ------------------------------------------------------------------------------

    @staticmethod
    def _source_packet(packet_id: int, kind: int, body: str) -> bytes:
        payload = struct.pack("<ii", packet_id, kind) + body.encode("utf-8", "replace") + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    @staticmethod
    async def _read_source_packet(reader: asyncio.StreamReader) -> tuple[int, int, str] | None:
        try:
            head = await reader.readexactly(4)
            (size,) = struct.unpack("<i", head)
            if not 10 <= size <= SOURCE_MAX_PACKET:
                return None
            payload = await reader.readexactly(size)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None
        packet_id, kind = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", "replace") if len(payload) >= 10 else ""
        return packet_id, kind, body

    async def _source(self, server: ServerConfig, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?",)
        address = str(peer[0])

        def write(packet_id: int, kind: int, body: str = "") -> None:
            writer.write(self._source_packet(packet_id, kind, body))

        session: Session | None = None
        try:
            try:
                first = await asyncio.wait_for(self._read_source_packet(reader), 10)
            except TimeoutError:
                first = None
            if first is None or first[1] != SOURCE_AUTH:
                return
            packet_id, _, password = first
            session, why = self._sign_in(server, address, password, "source rcon", writer.close)
            write(packet_id, SOURCE_RESPONSE)  # as Source servers do: an empty response, then the result
            if session is None:
                write(-1, SOURCE_AUTH_RESPONSE, why)
                await writer.drain()
                return
            write(packet_id, SOURCE_AUTH_RESPONSE)
            await writer.drain()
            while True:
                packet = await self._read_source_packet(reader)
                if packet is None:
                    return
                packet_id, kind, body = packet
                if kind == SOURCE_EXEC:
                    ok, text, _ = await self._run_command(session, server, body.strip())
                    text = text if ok or text else "refused"
                    chunks = [text[i:i + SOURCE_MAX_BODY] for i in range(0, len(text), SOURCE_MAX_BODY)] or [""]
                    for chunk in chunks:
                        write(packet_id, SOURCE_RESPONSE, chunk)
                elif kind == SOURCE_RESPONSE:
                    write(packet_id, SOURCE_RESPONSE)  # the end-of-reply marker clients send: echoed back
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            if session is not None:
                self._sign_out(session)
            try:
                writer.close()
            except OSError:
                pass
