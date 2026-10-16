"""Runs one part: its own queue, handler time limits, and switching it off if it keeps failing.

One part can't break the others or hold up the RCON reader: events are copied
into each part's queue and handled there, one at a time.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from .events import Event, EventBus
from .part import CommandSpec, Part

log = logging.getLogger(__name__)

Job = tuple[str, Callable[[], Awaitable[Any]]]


class PartRunner:
    def __init__(self, part: Part, bus: EventBus, *, queue_size: int = 1000, handler_timeout: float = 10.0,
                 max_failures: int = 5, failure_window: float = 600.0) -> None:
        self.part = part
        self.name = part.ctx.name
        self.bus = bus
        self.handler_timeout = handler_timeout
        self.max_failures = max_failures
        self.failure_window = failure_window
        self.enabled = True
        self.disabled_reason: str | None = None
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=queue_size)
        self._failures: deque[float] = deque()
        self._unsubscribe: list[Callable[[], None]] = []
        self._timers: list[asyncio.Task[None]] = []
        self._drained = asyncio.Event()  # run() has finished: the queue is done with
        self.handlers: list[tuple[str, Callable[[Event], Awaitable[Any]]]] = []
        self.commands: list[tuple[CommandSpec, Callable[..., Awaitable[Any]]]] = []
        self.intervals: list[tuple[float, Callable[[], Awaitable[Any]]]] = []
        for attribute in dir(type(part)):
            function = getattr(type(part), attribute, None)
            marks = getattr(function, "__dict__", {})
            if not marks:
                continue
            method = getattr(part, attribute)
            for pattern in marks.get("_part_on", []):
                self.handlers.append((pattern, method))
            if "_part_command" in marks:
                self.commands.append((marks["_part_command"], method))
            if "_part_every" in marks:
                self.intervals.append((marks["_part_every"], method))

    def submit(self, label: str, job: Callable[[], Awaitable[Any]]) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait((label, job))
        except asyncio.QueueFull:
            log.warning("%s is falling behind; dropped %s", self.name, label)

    def _deliver(self, method: Callable[[Event], Awaitable[Any]]) -> Callable[[Event], None]:
        def listener(event: Event) -> None:
            self.submit(f"{method.__name__}({event.name})", lambda: method(event))

        return listener

    async def run(self) -> None:
        try:
            await self._run()
        finally:
            self._drained.set()

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self.part.setup(), 30)
        except Exception as error:  # noqa: BLE001
            self.disable(f"setup failed: {type(error).__name__}: {error}")
            log.exception("%s: setup failed", self.name)
            return
        if not self.enabled:  # stopped while setting up
            return
        for pattern, method in self.handlers:
            self._unsubscribe.append(self.bus.subscribe(pattern, self._deliver(method)))
        for interval, method in self.intervals:
            self._timers.append(asyncio.ensure_future(self._timer(interval, method)))
        log.info("%s running", self.name)
        while True:
            label, job = await self.queue.get()
            if job is _noop:  # the end of the queue, from stop() or disable()
                return
            if self.disabled_reason is None:  # switched off for failing: what's left is dropped
                await self._run_job(label, job)

    async def _timer(self, interval: float, method: Callable[[], Awaitable[Any]]) -> None:
        while self.enabled:
            await asyncio.sleep(interval)
            self.submit(f"{method.__name__}()", method)

    async def _run_job(self, label: str, job: Callable[[], Awaitable[Any]]) -> None:
        try:
            await asyncio.wait_for(job(), self.handler_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._failed(label, f"took longer than {self.handler_timeout:.0f} s")
        except Exception as error:  # noqa: BLE001
            log.exception("%s: %s failed", self.name, label)
            self._failed(label, f"{type(error).__name__}: {error}")

    def _failed(self, label: str, why: str) -> None:
        now = time.monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self.failure_window:
            self._failures.popleft()
        log.warning("%s: %s %s", self.name, label, why)
        if len(self._failures) >= self.max_failures:
            self.disable(f"{len(self._failures)} failures in {self.failure_window / 60:.0f} minutes; last: {why}")

    def disable(self, reason: str) -> None:
        if not self.enabled:
            return
        self.enabled = False
        self.disabled_reason = reason
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        for timer in self._timers:
            timer.cancel()
        log.error("%s switched off: %s", self.name, reason)
        self.bus.emit("supervisor.part_disabled", None, part=self.name, reason=reason)
        self.submit_stop()

    def submit_stop(self) -> None:
        """Mark the end of the queue: run() handles what came before it, then returns."""
        try:
            self.queue.put_nowait(("stop", _noop))
        except asyncio.QueueFull:
            pass

    async def stop(self) -> None:
        was_enabled = self.enabled
        self.enabled = False
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        for timer in self._timers:
            timer.cancel()
        self.submit_stop()
        try:  # let what was already queued (e.g. supervisor.stopping) be handled before teardown
            await asyncio.wait_for(self._drained.wait(), 5)
        except TimeoutError:
            log.warning("%s: still busy after 5 s; stopping it anyway", self.name)
        if was_enabled or self.disabled_reason is None:
            try:
                await asyncio.wait_for(self.part.teardown(), 10)
            except Exception:  # noqa: BLE001
                log.exception("%s: teardown failed", self.name)


async def _noop() -> None:
    return None
