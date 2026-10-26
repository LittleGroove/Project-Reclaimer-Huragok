"""Runs one part: its own queue, handler time limits, and switching it off for a while if it keeps failing.

One part can't break the others or hold up the RCON reader: events are copied
into each part's queue and handled there, one at a time.

A part that keeps failing is switched off for COOL_DOWN, then on again: what
failed may have been passing (a server answering slowly), and a part off for good
would leave every `!` command answering "Unknown command" until Huragok
restarted. One whose setup fails stays off. A server that doesn't answer isn't the
part failing (an RconError is only logged), and a handler always gets longer than
an RCON command's own time limit, so a slow server times out there first.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from .errors import ServerError
from .events import Event, EventBus
from .part import CommandSpec, Part

log = logging.getLogger(__name__)

Job = tuple[str, Callable[[], Awaitable[Any]]]

COOL_DOWN = 600.0  # a part switched off for failing is on again after this long
OVER_RCON = 5.0  # a handler's time limit is at least the RCON command time limit plus this


def handler_time_limit(handler_timeout: float, command_timeout: float) -> float:
    """The time a handler gets: what [<part>] handler_timeout says, but always more than one RCON command's own
    limit ([rcon] command_timeout), so a server slow to answer is an RCON timeout (the handler's to deal with), not
    the part taking too long (which counts towards switching it off)."""
    return max(float(handler_timeout), float(command_timeout) + OVER_RCON)


class PartRunner:
    def __init__(self, part: Part, bus: EventBus, *, queue_size: int = 1000, handler_timeout: float = 10.0,
                 max_failures: int = 5, failure_window: float = 600.0, cool_down: float = COOL_DOWN) -> None:
        self.part = part
        self.name = part.ctx.name
        self.bus = bus
        self.handler_timeout = handler_timeout
        self.max_failures = max_failures
        self.failure_window = failure_window
        self.cool_down = cool_down
        self.enabled = True
        self.disabled_reason: str | None = None
        self.for_good = False  # switched off until Huragok restarts (its setup failed)
        self.back_at: float | None = None  # monotonic: when a part switched off for failing is on again
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=queue_size)
        self._failures: deque[float] = deque()
        self._unsubscribe: list[Callable[[], None]] = []
        self._timers: list[asyncio.Task[None]] = []
        self._comeback: asyncio.Task[None] | None = None
        self._stopped = False  # stop() was called: run() ends at the marker
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
            self.disable(f"setup failed: {type(error).__name__}: {error}", for_good=True)
            log.exception("%s: setup failed", self.name)
            return
        if not self.enabled:  # stopped while setting up
            return
        self._switch_on()
        log.info("%s running", self.name)
        while True:
            label, job = await self.queue.get()
            if job is _noop:  # the end of the queue, from stop(); or disable()'s, which only marks the drop
                if self._stopped or self.for_good:
                    return
                continue
            if self.disabled_reason is None:  # switched off for failing: what's left is dropped
                await self._run_job(label, job)

    def _switch_on(self) -> None:
        for pattern, method in self.handlers:
            self._unsubscribe.append(self.bus.subscribe(pattern, self._deliver(method)))
        for interval, method in self.intervals:
            self._timers.append(asyncio.ensure_future(self._timer(interval, method)))

    def _switch_off(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

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
        except ServerError as error:  # the server not answering (or not connected) isn't the part failing
            log.warning("%s: %s: the server didn't answer: %s", self.name, label, error)
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

    def disable(self, reason: str, *, for_good: bool = False) -> None:
        """Switch the part off: for good (its setup failed), or for cool_down, after which it's on again."""
        if not self.enabled:
            return
        self.enabled = False
        self.disabled_reason = reason
        self.for_good = for_good
        self._switch_off()
        if for_good or self._stopped:
            log.error("%s switched off: %s", self.name, reason)
        else:
            self.back_at = time.monotonic() + self.cool_down
            log.error("%s switched off for %s: %s", self.name, _minutes(self.cool_down), reason)
            self._comeback = asyncio.ensure_future(self._come_back())
        self.bus.emit("supervisor.part_disabled", None, part=self.name, reason=reason, for_good=for_good,
                      seconds=None if for_good else self.cool_down)
        self.submit_stop()

    async def _come_back(self) -> None:
        await asyncio.sleep(self.cool_down)
        if self._stopped or self.for_good:
            return
        reason = self.disabled_reason
        self._failures.clear()
        self.disabled_reason = None
        self.back_at = None
        self.enabled = True
        self._switch_on()
        log.info("%s on again after %s off (it was switched off for: %s)", self.name, _minutes(self.cool_down),
                 reason)
        self.bus.emit("supervisor.part_enabled", None, part=self.name)

    def submit_stop(self) -> None:
        """Mark the end of the queue: run() handles what came before it, then returns."""
        try:
            self.queue.put_nowait(("stop", _noop))
        except asyncio.QueueFull:
            pass

    async def stop(self) -> None:
        was_enabled = self.enabled
        self.enabled = False
        self._stopped = True
        if self._comeback is not None:
            self._comeback.cancel()
        self._switch_off()
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


def _minutes(seconds: float) -> str:
    if seconds >= 60:
        minutes = round(seconds / 60)
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    return f"{seconds:.0f} s"
