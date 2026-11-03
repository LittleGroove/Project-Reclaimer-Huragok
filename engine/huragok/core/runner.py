"""Runs one part: its own queues, handler time limits, and switching it off for a while if it keeps failing.

One part can't break the others or hold up the RCON reader: events are copied
into each part's queues and handled there. Each server has a lane of its own (its
events, and the commands typed on it), handled one at a time and in order; what
names no server (timers, Huragok's own events) has the part's shared lane. So a
server whose console stops answering while still connected (the "dead after game"
freeze) holds up only its own lane, not the part's work for every other server
(the host's call, 2 Nov 2026). One server's lane that keeps overrunning is paused for
COOL_DOWN, the part staying on for the rest; a part that keeps raising is switched off
whole, as before.

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
        self.queue_size = queue_size
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=queue_size)  # the shared lane: what names no server
        self.lanes: dict[str, asyncio.Queue[Job]] = {}  # each server's own
        self._lane_tasks: dict[str, asyncio.Task[None]] = {}
        self._lane_failures: dict[str, deque[float]] = {}
        self.paused: dict[str, float] = {}  # server -> monotonic: its lane failed, and is paused till then
        self._running = False  # set up and handling: a new lane gets its worker at once
        self._failures: deque[float] = deque()
        self._unsubscribe: list[Callable[[], None]] = []
        self._timers: list[asyncio.Task[None]] = []
        self._comeback: asyncio.Task[None] | None = None
        self._stopped = False  # stop() was called: run() ends at the marker
        self._drained = asyncio.Event()  # run() has finished: the queue is done with
        self.ready = asyncio.Event()  # set up and listening, or given up: what supervisor.started waits for
        self.set_up = False  # its setup() worked: its teardown() runs at the end, whatever happened since
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

    def submit(self, label: str, job: Callable[[], Awaitable[Any]], *, server: str | None = None) -> None:
        """`server`: the server it's about, whose lane it waits in; None (or ""), the shared lane."""
        if not self.enabled:
            return
        if server:
            if self.paused.get(server, 0.0) > time.monotonic():
                return  # its lane is paused for failing: dropped, as a part switched off drops what comes
            queue = self.lanes.get(server) or self._lane(server)
        else:
            queue = self.queue
        try:
            queue.put_nowait((label, job))
        except asyncio.QueueFull:
            log.warning("%s is falling behind%s; dropped %s", self.name, f" on {server}" if server else "", label)

    def _lane(self, server: str) -> asyncio.Queue[Job]:
        queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=self.queue_size)
        self.lanes[server] = queue
        if self._running:
            self._start_lane(server)
        return queue

    def _start_lane(self, server: str) -> None:
        if server not in self._lane_tasks:
            self._lane_tasks[server] = asyncio.ensure_future(self._lane_worker(server))

    async def _lane_worker(self, server: str) -> None:
        queue = self.lanes[server]
        while True:
            label, job = await queue.get()
            if job is _noop:
                if self._stopped or self.for_good:
                    return
                continue
            if self.disabled_reason is None and self.paused.get(server, 0.0) <= time.monotonic():
                await self._run_job(label, job, server)

    def queued(self) -> int:
        """Jobs waiting, in every lane."""
        return self.queue.qsize() + sum(queue.qsize() for queue in self.lanes.values())

    def _deliver(self, method: Callable[[Event], Awaitable[Any]]) -> Callable[[Event], None]:
        def listener(event: Event) -> None:
            self.submit(f"{method.__name__}({event.name})", lambda: method(event), server=event.server)

        return listener

    async def run(self) -> None:
        try:
            await self._run()
            lanes = list(self._lane_tasks.values())
            if lanes:  # each server's lane finishes what it has, as the shared one did
                await asyncio.wait(lanes, timeout=5)
        finally:
            self._running = False
            for task in self._lane_tasks.values():
                if not task.done():
                    task.cancel()
            self.ready.set()
            self._drained.set()

    async def _run(self) -> None:
        try:
            await asyncio.wait_for(self.part.setup(), 30)
        except Exception as error:  # noqa: BLE001
            self.disable(f"setup failed: {type(error).__name__}: {error}", for_good=True)
            log.exception("%s: setup failed", self.name)
            return
        self.set_up = True
        if not self.enabled:  # stopped while setting up
            return
        self._switch_on()
        self._running = True
        for server in list(self.lanes):  # commands that came before it was going
            self._start_lane(server)
        self.ready.set()
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

    async def _run_job(self, label: str, job: Callable[[], Awaitable[Any]], server: str | None = None) -> None:
        try:
            await asyncio.wait_for(job(), self.handler_timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:  # overran: on a server's lane, most likely its console frozen while connected
            self._failed(label, f"took longer than {self.handler_timeout:.0f} s", server)
        except ServerError as error:  # the server not answering (or not connected) isn't the part failing
            log.warning("%s: %s: the server didn't answer: %s", self.name, label, error)
        except Exception as error:  # noqa: BLE001
            log.exception("%s: %s failed", self.name, label)
            self._failed(label, f"{type(error).__name__}: {error}")  # a fault of the part's own: the whole part

    def _failed(self, label: str, why: str, server: str | None = None) -> None:
        now = time.monotonic()
        if server:  # one server's lane overrunning: paused, the part staying on for the others
            failures = self._lane_failures.setdefault(server, deque())
            failures.append(now)
            while failures and now - failures[0] > self.failure_window:
                failures.popleft()
            log.warning("%s: [%s] %s %s", self.name, server, label, why)
            if len(failures) >= self.max_failures:
                failures.clear()
                self.paused[server] = now + self.cool_down
                queue = self.lanes.get(server)
                while queue is not None and not queue.empty():  # what's waiting there is dropped
                    queue.get_nowait()
                log.error("%s paused on %s for %s: %d failures in %.0f minutes there (the other servers carry on); "
                          "last: %s", self.name, server, _minutes(self.cool_down), self.max_failures,
                          self.failure_window / 60, why)
                self.bus.emit("supervisor.part_paused", server, part=self.name, reason=why, seconds=self.cool_down)
            return
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
        """Mark the end of each lane: run() handles what came before it, then returns."""
        for queue in (self.queue, *self.lanes.values()):
            try:
                queue.put_nowait(("stop", _noop))
            except asyncio.QueueFull:
                pass

    async def stop(self) -> None:
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
        if self.set_up:  # a part in its cool-down after failing too: Discord's queued cards, its state file
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
