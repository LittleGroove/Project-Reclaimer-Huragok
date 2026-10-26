"""Work started to run by itself while the code that started it carries on: a reply in game, a restart the keepalive
asked for, an install, a ban passed on to another server.

asyncio keeps only a weak reference to a task, so one nobody holds can be collected before it ends, and its failure
is only logged as "Task exception was never retrieved", without saying what it was. background() keeps every such
task until it ends, logs a failure at once with what it was doing, and stop() cancels whatever is still running as
Huragok stops (before its database closes), instead of leaving it to asyncio's own clean-up at the very end.
Huragok's long-running tasks (each server's console, the parts' runners) are Huragok.spawn's: those restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

log = logging.getLogger(__name__)

STOP_SECONDS = 5.0  # what stop() waits for cancelled work to finish (a reply mid-send, a file being written)

_running: set[asyncio.Task[Any]] = set()


def background(work: Awaitable[Any], what: str, logger: logging.Logger | None = None) -> asyncio.Task[Any]:
    """Run `work` by itself, kept until it ends; a failure is logged at once as "<what> failed: …" (to `logger`, the
    caller's own, when given). Returns the task, for a caller that wants to wait for it or call it off."""
    task = asyncio.ensure_future(work)
    _running.add(task)

    def done(finished: asyncio.Task[Any]) -> None:
        _running.discard(finished)
        if finished.cancelled():
            return
        error = finished.exception()
        if error is not None:
            (logger or log).error("%s failed: %s", what, str(error) or type(error).__name__,
                                  exc_info=(type(error), error, error.__traceback__))

    task.add_done_callback(done)
    return task


def running() -> list[asyncio.Task[Any]]:
    """What's still running on this event loop."""
    loop = asyncio.get_running_loop()
    return [task for task in _running if task.get_loop() is loop and not task.done()]


async def stop(timeout: float = STOP_SECONDS) -> None:
    """Cancel everything still running on this event loop (but the task calling this) and wait, at most `timeout`
    seconds, for it to end."""
    current = asyncio.current_task()
    tasks = [task for task in running() if task is not current]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
