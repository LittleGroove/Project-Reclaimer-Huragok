"""A second server's console, for the commands that reach every server."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from huragok.rcon import RconTimeout, Reply


class OtherServer:
    """A second server's console, as far as these tests need one: every command sent, by whom."""

    connected = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[str, ...], str | None]] = []
        self.banned: list[str] = []  # its own ban list: player IDs (and addresses)
        self.details: dict[str, dict[str, Any]] = {}  # an entry's banned_by, reason, expires, as `bans` lists them
        self.admins: list[str] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args, by))
        if command == "bans":
            return Reply(True, f"{len(self.banned)} bans", {"players": [{"id": i, **self.details.get(i, {})}
                                                                         for i in self.banned], "ips": []}, {})
        if command == "ban" and args:
            self.banned.append(args[0].lower())
            self.details[args[0].lower()] = {"banned_by": f"{by} (RCON)" if by else "RCON",
                                             "banned_at": int(time.time())}
        elif command == "unban" and args:
            self.banned = [i for i in self.banned if i != args[0].lower()]
        elif command == "admin" and len(args) == 2:
            (self.admins.append if args[0] == "add" else self.admins.remove)(args[1].lower())
        return Reply(True, f"Done: {command}", None, {})

    def named(self, name: str) -> list[tuple[str, ...]]:
        return [args for command, args, _ in self.sent if command == name]


class FrozenServer(OtherServer):
    """A server frozen with its console still connected (a live failure mode): every command waits `seconds` and
    then times out, as RconClient's would."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = seconds

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args, by))
        await asyncio.sleep(self.seconds)
        raise RconTimeout(f"{command}: no answer in {self.seconds} s")
