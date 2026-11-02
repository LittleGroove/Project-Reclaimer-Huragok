"""Discord's calls, on threads of their own. The event loop's usual pool also stops and kills servers, reads their ports and sets their priority: a slow Discord with a burst of events behind it mustn't make any of that wait."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

T = TypeVar("T")
WORKERS = 4  # the outbox sends one at a time, so this only ever serves that, a look at Discord and whoever's waiting on an answer
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="discord")


async def to_thread(function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """As asyncio.to_thread, on Discord's own threads."""
    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(_pool, partial(context.run, function, *args, **kwargs))


__all__ = ["to_thread"]
