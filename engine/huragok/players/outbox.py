"""Every server's queue of what Huragok passes on to it from another server: a ban or unban given there, an admin
made there, a kick that keeps a player off every server, a pardon, and Huragok's own list put back on a server as it
connects (players/commands/across.py, roles.py).

Before, the handler that gave the ban went to each other server in turn and waited for it. A server that was frozen
but still connected made each wait out the console's timeout (10 s by default), so a ban with two such servers ran
past the commands part's time limit (15 s) and counted as a failure of the part, and five of those in ten minutes
switch the in-game commands off. Now each server has a queue, worked through in the background one job at a time and
never faster than SPACING apart (a sweep putting many bans on a server that was reset doesn't flood its console): a
frozen server holds up only its own queue, and the command that gave the ban answers at once. A caller that wants
to say how it went waits for the results a few seconds at most (results()), and the queue carries on after that.

A server that isn't connected when its turn comes is skipped, and so is a job that runs past JOB_SECONDS: Huragok's
list (its bans and the admins it made) is put on every server again as it connects and every ten minutes, so a ban
or an admin is never lost; a kick or a pardon is about who's on now, and a player kicked elsewhere is still kicked as
they join.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.background import background

log = logging.getLogger(__name__)

SPACING = 0.25  # seconds between two jobs on one server
JOB_SECONDS = 60.0  # one job's limit: a few console commands, each with its own timeout
MOST = 200  # jobs waiting for one server; more, and the oldest go (the next look at Huragok's list puts it right)
ASK_SECONDS = 6.0  # what a command waits for other servers' answers: well inside a part's own time limit (15 s)


@dataclass
class Job:
    what: str  # for the log: "Bravo's ban (by Admin on Alpha)"
    work: Callable[[], Awaitable[Any]]
    done: asyncio.Future[Any]
    key: str | None = None  # one job of a key waits at a time (Huragok's list, put on a server)
    queued: float = field(default_factory=time.monotonic)


class Outbox:
    def __init__(self, servers: Mapping[str, Any]) -> None:
        self.servers = servers  # name -> its live picture (ServerState): whether its console is connected
        self.queues: dict[str, deque[Job]] = {}
        self.workers: dict[str, asyncio.Task[Any]] = {}

    def send(self, server: str, what: str, work: Callable[[], Awaitable[Any]], *,
             key: str | None = None) -> asyncio.Future[Any]:
        """`work` queued for `server`. Returns the future of its result: what `work` returned, or None when it
        failed, ran too long, or was skipped (the server wasn't connected). With `key`, a job of that key already
        waiting there is the one (its future is returned): Huragok's list put on a server once, not once a sweep."""
        queue = self.queues.setdefault(server, deque())
        if key is not None and (waiting := next((job for job in queue if job.key == key), None)) is not None:
            return waiting.done
        job = Job(what, work, asyncio.get_running_loop().create_future(), key)
        if len(queue) >= MOST:
            dropped = queue.popleft()
            _settle(dropped.done, None)
            log.warning("[%s] %d things already wait to be passed on to it: %s dropped (Huragok's list is put on it "
                        "again as it connects and every ten minutes)", server, MOST, dropped.what)
        queue.append(job)
        if server not in self.workers:
            self.workers[server] = background(self._work(server), f"[{server}] passing things on", log)
        return job.done

    async def drain(self, timeout: float = 10.0) -> None:
        """Until every queue is empty and its last job done (`timeout` at most): for tests, and for a caller that must
        know what it sent has gone."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self.workers and asyncio.get_running_loop().time() < deadline:
            await asyncio.wait(list(self.workers.values()), timeout=max(0.0, deadline - asyncio.get_running_loop().time()))

    def waiting(self, server: str | None = None) -> int:
        """How many jobs wait (for one server, or all)."""
        queues = [self.queues.get(server, deque())] if server is not None else list(self.queues.values())
        return sum(len(queue) for queue in queues)

    async def _work(self, server: str) -> None:
        queue = self.queues[server]
        try:
            while queue:
                job = queue.popleft()
                result = None
                state = self.servers.get(server)
                if state is None or not getattr(state, "connected", False):
                    log.info("[%s] not connected: %s isn't sent (Huragok's list is put on it as it connects)", server,
                             job.what)
                else:
                    try:
                        result = await asyncio.wait_for(job.work(), JOB_SECONDS)
                    except TimeoutError:
                        log.warning("[%s] %s took over %g s and was given up (its console isn't answering?)",
                                    server, job.what, JOB_SECONDS)
                    except Exception as error:  # noqa: BLE001 - one job mustn't stop the server's queue
                        log.warning("[%s] %s didn't reach it: %s", server, job.what, error)
                _settle(job.done, result)
                if queue:
                    await asyncio.sleep(SPACING)
        finally:
            # No await between the queue found empty and this: a job sent from now on starts a new worker.
            self.workers.pop(server, None)
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():  # Huragok stopping
                for job in queue:
                    job.done.cancel()
                queue.clear()


def _settle(future: asyncio.Future[Any], result: Any) -> None:
    if not future.done():
        future.set_result(result)


async def results(futures: Mapping[str, asyncio.Future[Any]], seconds: float) -> tuple[list[str], list[str], list[str]]:
    """What came of jobs sent to several servers, waiting `seconds` at most (the queues carry on after): the servers
    it went well on (a true result), those it didn't, and those still waiting their turn."""
    if futures:
        await asyncio.wait(list(futures.values()), timeout=seconds)
    went, failed, waiting = [], [], []
    for server, future in futures.items():
        if not future.done():
            waiting.append(server)
        elif not future.cancelled() and future.result():
            went.append(server)
        else:
            failed.append(server)
    return went, failed, waiting
