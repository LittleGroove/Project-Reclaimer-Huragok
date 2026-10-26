"""Each server's queue of what's passed on to it (players/outbox.py): one at a time and spaced on each server, a slow
server holding up only itself, a disconnected one skipped, and results said within a time."""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

from huragok.players import outbox as outbox_module
from huragok.players.outbox import Outbox, results


def servers(**connected: bool) -> dict[str, SimpleNamespace]:
    return {name: SimpleNamespace(connected=on) for name, on in connected.items()}


def test_a_slow_server_holds_up_only_its_own_queue(run, monkeypatch) -> None:
    monkeypatch.setattr(outbox_module, "SPACING", 0.01)
    done: list[str] = []

    async def job(name: str, seconds: float) -> bool:
        await asyncio.sleep(seconds)
        done.append(name)
        return True

    async def scenario() -> None:
        box = Outbox(servers(Alpha=True, Frozen=True))
        started = time.monotonic()
        frozen = box.send("Frozen", "a ban", lambda: job("frozen 1", 1.0))
        box.send("Frozen", "another ban", lambda: job("frozen 2", 0.0))
        quick = [box.send("Alpha", f"ban {n}", lambda n=n: job(f"alpha {n}", 0.0)) for n in range(3)]
        await asyncio.wait(quick)
        assert time.monotonic() - started < 0.5 and done == ["alpha 0", "alpha 1", "alpha 2"]  # in order, not waiting
        assert box.waiting("Frozen") == 1  # the second waits behind the first, on that server alone
        went, failed, waiting = await results({"Alpha": quick[0], "Frozen": frozen}, 0.1)
        assert (went, failed, waiting) == (["Alpha"], [], ["Frozen"])  # said within its time; the queue carries on
        await frozen
        await asyncio.sleep(0.05)
        assert done[-2:] == ["frozen 1", "frozen 2"] and not box.workers

    run(scenario())


def test_a_server_not_connected_is_skipped_a_job_that_runs_too_long_given_up_and_one_key_waits_once(
        run, monkeypatch, caplog) -> None:
    monkeypatch.setattr(outbox_module, "SPACING", 0.0)
    monkeypatch.setattr(outbox_module, "JOB_SECONDS", 0.1)

    async def scenario() -> None:
        box = Outbox(servers(Away=False, Stuck=True))
        assert await box.send("Away", "a ban", lambda: asyncio.sleep(0, result=True)) is None  # put right on connect
        assert await box.send("Stuck", "a ban", lambda: asyncio.sleep(5, result=True)) is None  # given up
        first = box.send("Stuck", "Huragok's list", lambda: asyncio.sleep(0.05, result=True), key="check")
        second = box.send("Stuck", "Huragok's list", lambda: asyncio.sleep(0, result=True), key="check")
        assert second is first and box.waiting("Stuck") == 1  # a sweep doesn't stack another on one still waiting
        assert await first is True

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "[Away] not connected" in caplog.text and "[Stuck] a ban took over 0.1 s" in caplog.text
