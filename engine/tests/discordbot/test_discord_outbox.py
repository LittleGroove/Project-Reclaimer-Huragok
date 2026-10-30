"""One way out to Discord: most urgent first, the newest of a kind, and never into a "slow down"."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest

from huragok.discordbot.outbox import ALERT, ANSWER, CHAT, MAX_WAITING, MODERATION, RETRY_WAITS, STATUS, Outbox
from huragok.discordbot.rest import BadToken, Refused, Reply, SlowDown, Unreachable


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def sender(sent: list[str], name: str, result: Any = None) -> Callable[[], Any]:
    async def send() -> Any:
        sent.append(name)
        if isinstance(result, Exception):
            raise result
        return result

    return send


async def flush(outbox: Outbox) -> None:
    """Everything free to go, now, in order."""
    while True:
        job, _ = outbox._next(outbox.clock())
        if job is None:
            return
        await outbox.send_one(job)


def test_the_most_urgent_goes_first_then_oldest_first(run: Any) -> None:
    async def scenario() -> list[str]:
        outbox, sent = Outbox(clock=Clock()), []
        for priority, name, bucket in [(CHAT, "chat", "a"), (STATUS, "status", "b"), (MODERATION, "mod", "c"),
                                       (ANSWER, "answer", "d"), (ALERT, "alert", "e"), (ALERT, "alert 2", "f")]:
            outbox.put(sender(sent, name), priority=priority, bucket=bucket)
        await flush(outbox)
        return sent

    assert run(scenario()) == ["alert", "alert 2", "answer", "mod", "status", "chat"]


def test_only_the_newest_status_edit_goes(run: Any) -> None:
    async def scenario() -> list[str]:
        outbox, sent = Outbox(clock=Clock()), []
        outbox.put(sender(sent, "first"), key="board", bucket="a")
        outbox.put(sender(sent, "other"), key="other board", bucket="b")
        outbox.put(sender(sent, "newest"), key="board", bucket="a")
        await flush(outbox)
        return sent

    assert run(scenario()) == ["newest", "other"]


def test_a_channel_keeps_its_pace_and_others_carry_on(run: Any) -> None:
    async def scenario() -> None:
        clock = Clock()
        outbox, sent = Outbox(clock=clock), []
        outbox.put(sender(sent, "a1"), bucket="a", gap=5.0)
        outbox.put(sender(sent, "a2"), bucket="a", gap=5.0)
        outbox.put(sender(sent, "b1"), bucket="b", gap=5.0)
        await flush(outbox)
        assert sent == ["a1", "b1"] and outbox.waiting == 1
        assert outbox._next(clock())[1] == pytest.approx(5.0)  # how long until it can go
        clock.now += 5.0
        await flush(outbox)
        assert sent == ["a1", "b1", "a2"]

    run(scenario())


def test_discords_own_count_can_lengthen_the_gap(run: Any) -> None:
    async def scenario() -> None:
        clock = Clock()
        outbox, sent = Outbox(clock=clock), []
        outbox.put(sender(sent, "a1", Reply({}, remaining=0, reset_after=8.0)), bucket="a", gap=1.0)
        outbox.put(sender(sent, "a2"), bucket="a")
        await flush(outbox)
        assert sent == ["a1"]
        clock.now += 7.0
        await flush(outbox)
        assert sent == ["a1"]
        clock.now += 1.0
        await flush(outbox)
        assert sent == ["a1", "a2"]

    run(scenario())


def test_a_slow_down_holds_that_channel_only_and_the_post_waits(run: Any) -> None:
    async def scenario() -> None:
        clock = Clock()
        outbox, sent = Outbox(clock=clock), []
        outbox.put(sender(sent, "a", SlowDown("slow", retry_after=30.0, everything=False)), bucket="a")
        outbox.put(sender(sent, "b"), bucket="b")
        await flush(outbox)
        assert sent == ["a", "b"] and outbox.waiting == 1  # never asked again meanwhile
        clock.now += 29.0
        await flush(outbox)
        assert sent == ["a", "b"]
        clock.now += 1.0
        await flush(outbox)
        assert sent == ["a", "b", "a"]

    run(scenario())


def test_a_slow_down_for_the_whole_bot_holds_everything(run: Any) -> None:
    async def scenario() -> None:
        clock = Clock()
        outbox, sent = Outbox(clock=clock), []
        async def once() -> None:
            sent.append("a")
            if sent.count("a") == 1:
                raise SlowDown("slow", retry_after=10.0, everything=True)

        outbox.put(once, bucket="a", priority=ALERT)
        outbox.put(sender(sent, "b"), bucket="b")
        await flush(outbox)
        assert sent == ["a"] and outbox.waiting == 2
        clock.now += 10.0
        await flush(outbox)
        assert sent == ["a", "a", "b"]

    run(scenario())


def test_a_newer_one_replaces_a_slowed_one(run: Any) -> None:
    async def scenario() -> None:
        clock = Clock()
        outbox, sent = Outbox(clock=clock), []
        job_sent: list[str] = []

        async def slow() -> None:
            job_sent.append("old")
            outbox.put(sender(sent, "new"), key="board", bucket="a")  # came while it was going
            raise SlowDown("slow", retry_after=1.0, everything=False)

        outbox.put(slow, key="board", bucket="a")
        await flush(outbox)
        clock.now += 1.0
        await flush(outbox)
        assert job_sent == ["old"] and sent == ["new"]

    run(scenario())


def test_a_refusal_is_never_tried_again_and_is_heard(run: Any) -> None:
    async def scenario() -> None:
        heard: list[str] = []
        outbox, sent = Outbox(clock=Clock(), on_refused=lambda job, error: heard.append(f"{job.what}: {error}")), []
        outbox.put(sender(sent, "a", Refused("Missing Access (403)", status=403)), bucket="a", what="a card")
        outbox.put(sender(sent, "b", BadToken("no", status=401)), bucket="b", what="another")
        await flush(outbox)
        assert sent == ["a", "b"] and outbox.waiting == 0
        assert heard == ["a card: Missing Access (403)", "another: no"]

    run(scenario())


def test_no_answer_is_tried_again_further_apart_then_given_up(run: Any) -> None:
    async def scenario() -> None:
        clock = Clock()
        outbox, sent = Outbox(clock=clock), []
        outbox.put(sender(sent, "a", Unreachable("no")), bucket="a")
        for wait in RETRY_WAITS:
            await flush(outbox)
            assert outbox.waiting == 1 and outbox.paused_until - clock.now == wait
            clock.now += wait
        await flush(outbox)
        assert outbox.waiting == 0 and outbox.dropped == 1 and len(sent) == len(RETRY_WAITS) + 1

    run(scenario())


def test_one_bad_post_never_stops_the_rest(run: Any) -> None:
    async def scenario() -> None:
        outbox, sent = Outbox(clock=Clock()), []
        outbox.put(sender(sent, "a", ValueError("bad")), bucket="a")
        outbox.put(sender(sent, "b"), bucket="b")
        await flush(outbox)
        assert sent == ["a", "b"]

    run(scenario())


def test_what_follows_a_post_hears_its_answer(run: Any) -> None:
    async def scenario() -> None:
        heard: list[Any] = []
        outbox = Outbox(clock=Clock())
        outbox.put(sender([], "a", Reply({"id": "5"})), bucket="a", on_done=lambda reply: heard.append(reply.data["id"]))
        await flush(outbox)
        assert heard == ["5"]

    run(scenario())


def test_too_much_waiting_loses_the_least_urgent(run: Any) -> None:
    async def scenario() -> None:
        outbox = Outbox(clock=Clock())
        outbox.put(sender([], "alert"), priority=ALERT, what="alert")
        for number in range(MAX_WAITING):
            outbox.put(sender([], "chat"), priority=CHAT, what=f"chat {number}")
        assert outbox.waiting == MAX_WAITING and outbox.dropped == 1
        assert any(job.what == "alert" for job in outbox.jobs)
        assert not any(job.what == f"chat {MAX_WAITING - 1}" for job in outbox.jobs)

    run(scenario())


def test_it_runs_by_itself_and_wakes_for_new_work(run: Any) -> None:
    async def scenario() -> None:
        outbox, sent = Outbox(), []
        task = asyncio.ensure_future(outbox.run())
        await asyncio.sleep(0)
        outbox.put(sender(sent, "a"), bucket="a", gap=0.0)
        for _ in range(500):
            if sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert sent == ["a"]

    run(scenario())


def test_stopping_sends_what_it_can_in_the_time_it_has(run: Any) -> None:
    async def scenario() -> None:
        outbox, sent = Outbox(), []
        outbox.put(sender(sent, "a1"), bucket="a", gap=600.0)
        outbox.put(sender(sent, "a2"), bucket="a", gap=600.0)
        outbox.put(sender(sent, "b"), bucket="b", gap=0.0)
        await outbox.drain(5.0)
        assert sent == ["a1", "b"] and outbox.waiting == 1  # a2's channel isn't free: it isn't pushed through

    run(scenario())
