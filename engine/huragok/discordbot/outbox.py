"""One way out to Discord: everything posted, edited or renamed waits here, so it all keeps to Discord's limits.

Most urgent first (alerts, then answers, moderation, status, chat). A job with a `key` replaces the one still waiting
with the same key, so only the newest status edit is ever sent. Each `bucket` (a channel, a webhook) has its own pace:
`gap` seconds between jobs, longer when Discord says its limit is used up. Discord's "slow down" holds that bucket (or
everything, when it's about the whole bot) for as long as it says, and nothing is sent into it meanwhile; other channels
carry on. No answer from Discord is tried again a few times, further apart each time. A refusal is never tried again:
10,000 of those in 10 minutes blocks the PC's address, so `on_refused` hears of each one, and the feature waits. A wait longer
than `long_wait` ([limits] wait_seconds) is real and kept to, but `on_long_wait` hears of it: a channel never goes quiet unexplained.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core.background import wait_until
from .rest import BadToken, DiscordError, Reply, SlowDown, Unreachable

log = logging.getLogger(__name__)

ALERT, ANSWER, MODERATION, STATUS, CHAT = range(5)

MAX_WAITING = 2000  # past this, the least urgent goes
RETRY_WAITS = (5.0, 15.0, 30.0, 60.0, 120.0)  # after each time Discord can't be reached
KEEP_TRYING_SECONDS = 21600.0  # a job that's failed this long while nothing else got through goes: the 2,000 above keeps memory safe till then
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
    tag: str = field(default="", compare=False)  # the feature it's for: a refusal stops that feature
    tries: int = field(default=0, compare=False)
    failing_since: float = field(default=0.0, compare=False)
    expires: float | None = field(default=None, compare=False)  # only true until then: it isn't sent after that
    on_done: Callable[[Any], None] | None = field(default=None, compare=False)
    on_fail: Callable[[DiscordError], bool] | None = field(default=None, compare=False)  # True: it's dealt with


class Outbox:
    def __init__(self, *, on_refused: Callable[[Job, DiscordError], None] | None = None,
                 on_long_wait: Callable[[Job, float], None] | None = None,
                 long_wait: Callable[[], float] = lambda: 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.on_refused = on_refused
        self.on_long_wait = on_long_wait
        self.long_wait = long_wait
        self.clock = clock
        self.jobs: list[Job] = []
        self.last_worked = clock()
        self.failing = False  # Discord couldn't be reached, the last time anything tried
        self.free_at: dict[str, float] = {}
        self.paused_until = 0.0
        self.dropped = 0
        self._numbers = itertools.count()
        self._wake = asyncio.Event()

    @property
    def waiting(self) -> int:
        return len(self.jobs)

    def put(self, send: Callable[[], Awaitable[Any]], *, priority: int = STATUS, bucket: str = "", key: str | None = None,
            gap: float = DEFAULT_GAP, what: str = "", tag: str = "", on_done: Callable[[Any], None] | None = None,
            on_fail: Callable[[DiscordError], bool] | None = None, expires: float | None = None) -> None:
        if key is not None:
            for job in self.jobs:
                if job.key == key:  # the newest wins, in the place the first one waited
                    job.send, job.what, job.on_done, job.on_fail, job.gap, job.tag, job.expires = send, what, on_done, on_fail, gap, tag, expires
                    return
        self.jobs.append(Job(priority, next(self._numbers), send, bucket, key, gap, what, tag, on_done=on_done, on_fail=on_fail, expires=expires))
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
                await wait_until(self._wake, wait)  # a cancel always ends it (wait_for's, on 3.11, could be lost)
                continue
            await self.send_one(job)

    async def send_one(self, job: Job) -> None:
        self.jobs.remove(job)
        now = self.clock()
        if job.expires is not None and now > job.expires:  # "come and fill it", hours late, after an outage
            log.info("%s is out of date, so it isn't posted", job.what or "a post")
            return
        try:
            reply = await job.send()
        except SlowDown as error:
            log.warning("%s; %s waits", error, job.what or "a post")
            if error.retry_after > self.long_wait() and self.on_long_wait is not None:
                self.on_long_wait(job, error.retry_after)
            if error.everything:
                self.paused_until = now + error.retry_after
            else:
                self.free_at[job.bucket] = now + error.retry_after
            self._again(job)
            return
        except Unreachable as error:
            job.tries += 1
            job.failing_since = job.failing_since or now
            self.failing = True
            if job.tries > len(RETRY_WAITS):
                if self.last_worked > job.failing_since or now - job.failing_since > KEEP_TRYING_SECONDS:  # others are getting through, or it's been hours: it's this one
                    self.dropped += 1
                    log.warning("%s; gave up on %s", error, job.what or "a post")
                    try:
                        if job.on_fail is not None:
                            job.on_fail(error)
                    except Exception:  # noqa: BLE001 - it mustn't stop the rest
                        log.exception("Telling of a failure failed, for %s", job.what or "a post")
                    return
                job.number = next(self._numbers)  # it's nobody's fault while nothing's getting through, and others of its priority go first in case it's only this one
            wait = RETRY_WAITS[min(job.tries, len(RETRY_WAITS)) - 1]
            self.paused_until = now + wait
            log.info("%s; %s tried again in %.0f s", error, job.what or "a post", wait)
            self._again(job)
            return
        except DiscordError as error:
            try:
                if job.on_fail is not None and job.on_fail(error):
                    return
                level = logging.ERROR if isinstance(error, BadToken) else logging.WARNING
                log.log(level, "Discord refused %s: %s", job.what or "a post", error)
                if self.on_refused is not None:
                    self.on_refused(job, error)
            except Exception:  # noqa: BLE001 - what's told of a refusal failing mustn't stop the rest either
                log.exception("Telling of a refusal failed, for %s", job.what or "a post")
            return
        except Exception:  # noqa: BLE001 - one bad post must never stop the rest
            log.exception("%s couldn't be sent", job.what or "a post")
            return
        self.last_worked = self.clock()
        self.failing = False
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
