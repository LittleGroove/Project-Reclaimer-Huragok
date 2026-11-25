"""The bus and worker threads: a check run with to_thread that raises a warning publishes from that thread, and a part's
listener (its queue lives on the loop) failed there with "no current event loop" (the live servers, 23 November
2026). Bound to the running loop, the bus hands such an event to the loop."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from conftest import wait_for

from huragok.core.events import Event, EventBus


def test_an_event_from_a_worker_thread_is_delivered_on_the_loop(run: Any) -> None:
    async def scenario() -> None:
        bus = EventBus()
        bus.bind(asyncio.get_running_loop())
        loop_thread = threading.get_ident()
        heard: list[tuple[str, int]] = []

        def part_like(event: Event) -> None:  # as runner.py's listener does: a task on the loop
            asyncio.ensure_future(asyncio.sleep(0))
            heard.append((event.name, threading.get_ident()))

        bus.subscribe("warning.*", part_like)
        await asyncio.to_thread(bus.emit, "warning.raised", "Alpha", id="plays-skipped")
        await wait_for(lambda: heard)
        assert heard == [("warning.raised", loop_thread)]
        bus.emit("warning.cleared", "Alpha")  # on the loop's own thread: at once, as before
        assert heard[-1] == ("warning.cleared", loop_thread)

    run(scenario())


def test_unbound_it_delivers_inline_as_before() -> None:
    bus = EventBus()
    heard: list[int] = []
    bus.subscribe("*", lambda event: heard.append(threading.get_ident()))
    worker = threading.Thread(target=bus.emit, args=("check.done",))
    worker.start()
    worker.join()
    assert heard == [worker.ident]
