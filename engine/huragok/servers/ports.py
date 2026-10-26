"""Whether something listens on a server's ports: the system's listening sockets (psutil), read at most once a
second for every stopped server, so no check holds up the loop; a connect, in a thread, when the table can't be
read. A stopped server never starts onto a port another program holds."""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import psutil

from ..core.background import background
from ..core.serverfolders import ServerConfig


log = logging.getLogger(__name__)


LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _rcon_address(server: ServerConfig) -> tuple[str, int]:
    endpoint = server.rcon_endpoint()
    return (endpoint.host, endpoint.port) if endpoint else ("127.0.0.1", server.rcon_port)


def listening_ports() -> frozenset[int] | None:
    """The TCP ports something on this machine listens on, from the system's own table of sockets: about a
    millisecond for every server at once. None when the system won't say (macOS without root)."""
    try:
        return frozenset(c.laddr.port for c in psutil.net_connections("tcp")
                         if c.status == psutil.CONN_LISTEN and c.laddr)
    except (psutil.Error, OSError):
        return None


def _connects(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_in_use(server: ServerConfig, timeout: float = 0.5) -> bool:
    """Something listens on the server's RCON port (the same number as its game port).

    Blocking, for the command line. The system's socket table answers at once; a connect is only tried when the
    table can't say (unreadable, or the server's RCON is on another machine), and a connect to a closed port takes
    Windows two seconds to refuse. The managers use PortWatch, which never waits."""
    host, port = _rcon_address(server)
    if not port:
        return False
    ports = listening_ports()
    if ports is not None and (port in ports or host in LOOPBACK):
        return port in ports
    return _connects(host, port, timeout)


class PortWatch:
    """Whether a stopped server's RCON port is taken (by a copy started some other way), for the managers' every
    tick, without ever holding the event loop.

    Asked by connecting, each stopped server held the loop for the whole time Windows takes to refuse a connect to
    a closed port: three stopped servers stalled every other server's RCON, the parts and the window by 1.5 s a
    second. The system's table of listening sockets answers for all of them at once in a millisecond, looked up at
    most once a second. Where it can't be read, a connect runs in a thread and its answer is kept for a while; until
    the first one is in, the answer is None (not known yet)."""

    LOOK_EVERY = 1.0
    PROBE_EVERY = 15.0

    def __init__(self) -> None:
        self._ports: frozenset[int] | None = None
        self._looked = float("-inf")
        self._probed: dict[tuple[str, int], tuple[float, bool]] = {}  # (host, port) -> (when, answer)
        self._probing: set[tuple[str, int]] = set()

    def in_use(self, server: ServerConfig) -> bool | None:
        host, port = _rcon_address(server)
        if not port:
            return False
        now = time.monotonic()
        if now - self._looked >= self.LOOK_EVERY:
            self._looked = now
            self._ports = listening_ports()
        if self._ports is not None:
            return port in self._ports  # the process backend's servers run here, whatever address they listen on
        key = (host, port)
        known = self._probed.get(key)
        if (known is None or now - known[0] >= self.PROBE_EVERY) and key not in self._probing:
            self._probing.add(key)
            background(self._probe(key), f"looking at port {key[1]}", log)
        return None if known is None else known[1]

    async def _probe(self, key: tuple[str, int]) -> None:
        try:
            answer = await asyncio.to_thread(_connects, key[0], key[1], 0.5)
        finally:
            self._probing.discard(key)
        self._probed[key] = (time.monotonic(), answer)
