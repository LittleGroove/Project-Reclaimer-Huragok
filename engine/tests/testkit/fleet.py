"""A fleet on a clock moved by hand: one server's manager, a backend and a console that do what they're told."""

from __future__ import annotations

import asyncio
import time as real_time
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, write_root

from huragok.core.events import EventBus
from huragok.core.settings import load
from huragok.servers import server as manager_module
from huragok.servers.backends import Backend, LifecycleError
from huragok.servers.fleet import Fleet
from huragok.servers.state import ServerState


SERVER = "LG Test Server"


class Clock:
    """time.time() and time.monotonic() under our control; everything else is the real module's.

    Moving `now` on moves both clocks (moving it back moves only the wall clock: the monotonic one never goes
    back); step() moves the wall clock alone, as the clock being set does."""

    def __init__(self, now: float) -> None:
        self._now = now
        self.mono = 1000.0

    @property
    def now(self) -> float:
        return self._now

    @now.setter
    def now(self, value: float) -> None:
        self.mono += max(0.0, value - self._now)
        self._now = value

    def step(self, seconds: float) -> None:
        self._now += seconds

    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self.mono

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


class FakeBackend(Backend):
    name = "fake"
    can_start = can_stop = can_restart = True
    sees_processes = True

    def __init__(self, settings: Any, bus: EventBus, states: dict[str, ServerState]) -> None:
        super().__init__(settings, bus, states)
        self.up: set[str] = set()
        self.starts: list[str] = []
        self.stops: list[str] = []
        self.fail_start = False
        self.started_times: dict[str, float] = {}

    async def start(self, server: Any) -> None:
        if self.fail_start:
            raise LifecycleError("the program is missing")
        self.up.add(server.name)
        self.starts.append(server.name)
        self.started_times[server.name] = real_time.time()

    async def stop(self, server: Any, timeout: float | None = None) -> None:
        self.stops.append(server.name)
        self.up.discard(server.name)

    def alive(self, server: Any) -> bool:
        return server.name in self.up

    def started_at(self, server: Any) -> float | None:
        return self.started_times.get(server.name)

    def forget(self, server: Any) -> None:
        self.up.discard(server.name)


class FakeClient:
    def __init__(self, state: ServerState) -> None:
        self.state = state  # connected follows the server's state, as the real client's does
        self.stopped_reason: str | None = None
        self.sent: list[tuple[str, tuple[str, ...]]] = []
        self.woken = 0

    @property
    def connected(self) -> bool:
        return self.state.connected

    def wake(self) -> None:
        self.woken += 1

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Any:
        self.sent.append((command, args))

        class Reply:
            ok = True
            text = f"{command} ok"

        return Reply()

    def says(self) -> list[str]:
        return [args[0] for command, args in self.sent if command == "say"]


RESTARTS = ("[restarts]\nwarn_seconds = [10, 5, 2]\nbackoff_seconds = [5, 60]\nmax_failures = 2\n"
            "good_run_seconds = 600\nrestarts_per_hour = 3\nready_seconds = 30\nempty_seconds = 4\n"
            "after_game_seconds = 3\n")


def build_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str = RESTARTS,
                backend_type: type[Backend] | None = None) -> tuple:
    clock = Clock(1_800_000_000.0)
    monkeypatch.setattr(manager_module, "time", clock)
    root = tmp_path / "reclaimer"
    settings = load(write_root(root, [(SERVER, free_port())], body))
    bus = EventBus()
    state = ServerState(SERVER)
    backend = (backend_type or FakeBackend)(settings, bus, {SERVER: state})
    fleet = Fleet(settings, backend, bus)
    client = FakeClient(state)
    manager = fleet.add(settings.servers[0], client, state)  # type: ignore[arg-type]
    seen: list[str] = []
    bus.subscribe("server.*", lambda e: seen.append(e.name))
    return clock, settings, backend, fleet, manager, client, state, seen


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_world(tmp_path, monkeypatch)


async def cycle(manager: Any, clock: Clock, advance: float = 0) -> None:
    """A few ticks at one moment, with time moved on first, letting the backend's tasks finish between them."""
    clock.now += advance
    for _ in range(3):
        manager.tick(clock.now)
        await asyncio.sleep(0.01)
