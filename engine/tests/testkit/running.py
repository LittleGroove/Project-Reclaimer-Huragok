"""Huragok running for real against a fake server's console: who was told what, who said what."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, staff, wait_for, write_server_folder, write_settings
from testkit.fake_rcon import FakeRcon

from huragok.core.events import Event
from huragok.core.settings import load
from huragok.huragok import Huragok


SERVER = "LG Test Server"


@asynccontextmanager
async def running(tmp_path: Path, body: str = "", *, commands: str | None = None,
                  prepare: Callable[[FakeRcon], None] | None = None,
                  adjust: Callable[[Any], None] | None = None,
                  **folder: Any) -> AsyncIterator[tuple[Huragok, FakeRcon, list[Event]]]:
    """Huragok on one fake server, with OWNER and MOD listed (or `commands`); `prepare` sets the server up first,
    `adjust` the settings (to times below what settings.toml allows, so a test runs in seconds)."""
    async with FakeRcon() as fake:
        if prepare:
            prepare(fake)
        write_server_folder(tmp_path / "server", fake.port, **folder)
        settings = load(write_settings(tmp_path, tmp_path / "server", body,
                                       commands=staff() if commands is None else commands))
        if adjust:
            adjust(settings)
        supervisor = Huragok(settings)
        seen: list[Event] = []
        supervisor.bus.subscribe("*", seen.append)
        task = asyncio.ensure_future(supervisor.run())
        try:
            await wait_for(lambda: supervisor.superintendent.states[SERVER].connected)
            yield supervisor, fake, seen
        finally:
            supervisor.stop()
            await asyncio.wait_for(task, 10)


def tells_to(fake: FakeRcon, player_id: str) -> list[str]:
    return [c["args"][1] for c in fake.named("tell") if c.get("args", [None])[0] == player_id]


def says(fake: FakeRcon) -> list[str]:
    return [c["args"][0] for c in fake.named("say") if c.get("args")]


async def everyone_joins(fake: FakeRcon) -> None:
    for number, (name, pid) in enumerate([("Owner", OWNER), ("Mod", MOD), ("Alice", ALICE), ("Bravo", BRAVO)], 1):
        await fake.join(number, name, pid)
    await fake.join(5, "Admin", ADMIN, admin=True)
