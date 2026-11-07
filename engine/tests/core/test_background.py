"""Work run by itself (core/background.py): kept until it ends, a failure said at once with what it was, and called
off as Huragok stops."""

from __future__ import annotations

import asyncio
import logging

from huragok.core import background


def test_work_is_kept_until_it_ends_and_its_failure_says_what_it_was(run, caplog) -> None:
    async def fails() -> None:
        await asyncio.sleep(0)
        raise ValueError("the console said no")

    async def scenario() -> None:
        gate = asyncio.Event()
        waiting = background.background(gate.wait(), "waiting")
        assert waiting in background.running()  # held: asyncio alone keeps only a weak reference
        gate.set()
        await waiting
        assert waiting not in background.running()
        task = background.background(fails(), "[Alpha] the keepalive's restart", logging.getLogger("part.test"))
        await asyncio.wait([task])
        await asyncio.sleep(0)  # the done callback

    with caplog.at_level(logging.ERROR):
        run(scenario())
    [record] = [r for r in caplog.records if r.name == "part.test"]
    assert record.getMessage() == "[Alpha] the keepalive's restart failed: the console said no" and record.exc_info


def test_stop_calls_off_what_is_still_running_but_never_its_caller(run) -> None:
    async def scenario() -> None:
        slow = background.background(asyncio.sleep(3600), "a long wait")

        async def stopper() -> str:
            await background.stop(timeout=1)
            return "carried on"

        caller = background.background(stopper(), "Huragok stopping")
        assert await caller == "carried on"
        assert slow.cancelled() and not background.running()

    run(scenario())


def test_a_wait_ends_with_its_task_even_as_what_it_waits_for_comes() -> None:
    """The Linux check, 6 November 2026: asyncio.wait_for(event.wait(), t) on Python 3.11 swallowed a cancel that came
    as the event was set, so a loop waiting so (the Discord outbox, a console's reconnect) never ended, and Huragok's
    stop waited for it for good. wait_until is cancelled whatever comes with the cancel."""

    async def scenario() -> None:
        event = asyncio.Event()
        ended: list[str] = []

        async def loop() -> None:
            while True:
                await background.wait_until(event, 30)
                ended.append("woken")
                event.clear()

        for _ in range(50):
            task = asyncio.ensure_future(loop())
            await asyncio.sleep(0)  # waiting now
            event.set()
            task.cancel()
            try:
                await asyncio.wait_for(task, 2)
            except asyncio.CancelledError:
                pass
            assert task.cancelled()
        event.clear()
        assert await background.wait_until(event, 0.01) is False
        event.set()
        assert await background.wait_until(event, None) is True

    asyncio.run(scenario())
