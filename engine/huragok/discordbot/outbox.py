"""One way out to Discord: everything posted, edited or renamed waits here, so it all keeps to Discord's limits.

Most urgent first (alerts, then answers, moderation, status, chat). A job with a `key` replaces the one still waiting
with the same key, so only the newest status edit is ever sent. Each `bucket` (a channel, a webhook) has its own pace:
`gap` seconds between jobs, longer when Discord says its limit is used up. Discord's "slow down" holds that bucket (or
everything, when it's about the whole bot) for as long as it says, and nothing is sent into it meanwhile; other channels
carry on. No answer from Discord is tried again a few times, further apart each time. A refusal is never tried again:
10,000 of those in 10 minutes blocks the PC's address, so `on_refused` hears of each one, and the feature waits.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .rest import BadToken, DiscordError, Reply, SlowDown, Unreachable

log = logging.getLogger(__name__)

ALERT, ANSWER, MODERATION, STATUS, CHAT = range(5)

MAX_WAITING = 2000  # past this, the least urgent goes
RETRY_WAITS = (5.0, 15.0, 30.0, 60.0, 120.0)  # after each time Discord can't be reached
DEFAULT_GAP = 1.0  # Discord: about 5 messages per 5 s per channel


@dataclass(order=True)
class Job:
    priority: int
    number: int
    send: Callable[[], Awaitable[Any]] = field(compare=False)
    bucket: str = field(default="", compare=False)
    key: str | None = field(default=None, compare=False)
    gap: float = field(default=DEFAULT_GAP, compare=False)
    what: str = field(default="", compare=False)
    tries: int = field(default=0, compare=False)
    on_done: Callable[[Any], None] | None = field(default=None, compare=False)


class Outbox:
    def __init__(self, *, on_refused: Callable[[Job, DiscordError], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.on_refused = on_refused
        self.clock = clock
        self.jobs: list[Job] = []
        self.free_at: dict[str, float] = {}
        self.paused_until = 0.0
        self.dropped = 0
        self._numbers = itertools.count()
        self._wake = asyncio.Event()

    @property
    def waiting(self) -> int:
        return len(self.jobs)

    def put(self, send: Callable[[], Awaitable[Any]], *, priority: int = STATUS, bucket: str = "", key: str | None = None,
            gap: float = DEFAULT_GAP, what: str = "", on_done: Callable[[Any], None] | None = None) -> None:
        if key is not None:
            for job in self.jobs:
                if job.key == key:  # the newest wins, in the place the first one waited
                    job.send, job.what, job.on_done, job.gap = send, what, on_done, gap
                    return
        self.jobs.append(Job(priority, next(self._numbers), send, bucket, key, gap, what, on_done=on_done))
        if len(self.jobs) > MAX_WAITING:
            last = max(self.jobs)
            self.jobs.remove(last)
            self.dropped += 1
            log.warning("Too much is waiting to go to Discord; dropped %s", last.what or "something")
        self._wake.set()

    def _next(self, now: float) -> tuple[Job | None, float | None]:
        """The most urgent job whose bucket is free, and otherwise how long until one is."""
        if now < self.paused_until:
            return None, self.paused_until - now if self.jobs else None
        job = min((j for j in self.jobs if self.free_at.get(j.bucket, 0.0) <= now), default=None)
        if job is not None:
            return job, None
        if not self.jobs:
            return None, None
        return None, min(self.free_at.get(j.bucket, 0.0) for j in self.jobs) - now

    async def run(self) -> None:
        """Until cancelled."""
        while True:
            job, wait = self._next(self.clock())
            if job is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), wait)
                except TimeoutError:
                    pass
                continue
            await self.send_one(job)

    async def send_one(self, job: Job) -> None:
        self.jobs.remove(job)
        now = self.clock()
        try:
            reply = await job.send()
        except SlowDown as error:
            log.warning("%s; %s waits", error, job.what or "a post")
            if error.everything:
                self.paused_until = now + error.retry_after
            else:
                self.free_at[job.bucket] = now + error.retry_after
            self._again(job)
            return
        except Unreachable as error:
            job.tries += 1
            if job.tries > len(RETRY_WAITS):
                self.dropped += 1
                log.warning("%s; gave up on %s", error, job.what or "a post")
                return
            self.paused_until = now + RETRY_WAITS[job.tries - 1]
            log.info("%s; %s tried again in %.0f s", error, job.what or "a post", RETRY_WAITS[job.tries - 1])
            self._again(job)
            return
        except DiscordError as error:
            level = logging.ERROR if isinstance(error, BadToken) else logging.WARNING
            log.log(level, "Discord refused %s: %s", job.what or "a post", error)
            if self.on_refused is not None:
                self.on_refused(job, error)
            return
        except Exception:  # noqa: BLE001 - one bad post must never stop the rest
            log.exception("%s couldn't be sent", job.what or "a post")
            return
        gap = job.gap
        if isinstance(reply, Reply) and reply.remaining == 0 and reply.reset_after:
            gap = max(gap, reply.reset_after)
        self.free_at[job.bucket] = now + gap
        if job.on_done is not None:
            try:
                job.on_done(reply)
            except Exception:  # noqa: BLE001
                log.exception("what follows %s failed", job.what or "a post")

    def _again(self, job: Job) -> None:
        """Back in its place; unless a newer one with its key came meanwhile, which is what's wanted now."""
        if job.key is None or not any(other.key == job.key for other in self.jobs):
            self.jobs.append(job)
        self._wake.set()

    async def drain(self, seconds: float) -> None:
        """As Huragok stops: what's waiting gets this long, and only what's free to go."""
        deadline = self.clock() + seconds
        while self.jobs and self.clock() < deadline:
            job, _ = self._next(self.clock())
            if job is None:
                break
            try:
                await asyncio.wait_for(self.send_one(job), max(0.1, deadline - self.clock()))
            except TimeoutError:
                break
