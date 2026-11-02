"""An outage, a slow Discord, a channel deleted: what's kept, what's said, and that moderation doesn't wait on any of it. From the failures audit of 1 November (plans/discord-audit/failures-scale.md)."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from conftest import ALICE
from test_audit_findings import FIRST, on, up  # noqa: F401
from test_audit_findings import bot_file as audit_file
from test_tickets import bot_file, fake, strike  # noqa: F401
from testkit.fake_discord import FakeDiscord
from testkit.running import fleet, running, wait_for

from huragok.discordbot.config import DiscordFileError, load, read, reload_if_changed
from huragok.discordbot.outbox import Outbox, RETRY_WAITS
from huragok.discordbot.rest import Refused, Unreachable, send
from huragok.discordbot.workers import to_thread


def test_discords_calls_are_on_threads_of_their_own(run: Any) -> None:
    """The usual pool stops and kills servers. A burst of slow reads mustn't queue in front of that."""
    async def scenario() -> None:
        ours = await to_thread(lambda: threading.current_thread().name)
        theirs = await asyncio.to_thread(lambda: threading.current_thread().name)
        assert ours.startswith("discord") and not theirs.startswith("discord")

    run(scenario())


def test_a_burst_of_events_is_one_read_and_one_after_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            await wait_for(lambda: part.looking == 0, timeout=15)
            before = len(fake.requests)
            for _ in range(30):
                part.look_at_discord()
            await wait_for(lambda: part.looking == 0 and not part._look_again, timeout=20)
            await asyncio.sleep(0.2)
            assert len(fake.requests) - before <= 16  # two reads, of about seven requests: it was 210

    run(scenario())


def test_an_outage_doesnt_throw_away_the_most_urgent(run: Any) -> None:
    now = [100.0]
    outbox = Outbox(clock=lambda: now[0])
    failed: list[str] = []

    async def down() -> None:
        raise Unreachable("couldn't reach Discord")

    async def scenario() -> None:
        outbox.put(down, what="an alert", on_fail=lambda error: failed.append("told") or True)
        for _ in range(len(RETRY_WAITS) * 4):
            job = outbox.jobs[0]
            await outbox.send_one(job)
            now[0] += 200.0
        assert outbox.jobs and not failed and outbox.dropped == 0
        assert outbox.failing

    run(scenario())


def test_a_post_only_it_cant_send_is_given_up_on_and_says_so(run: Any) -> None:
    now = [100.0]
    outbox = Outbox(clock=lambda: now[0])
    failed: list[str] = []

    async def down() -> None:
        raise Unreachable("Discord is having trouble")

    async def fine() -> Any:
        return None

    async def scenario() -> None:
        outbox.put(down, what="a post", on_fail=lambda error: failed.append("told") or True)
        for _ in range(len(RETRY_WAITS)):
            await outbox.send_one(outbox.jobs[0])
            now[0] += 200.0
        outbox.put(fine, what="another")
        fresh = next(job for job in outbox.jobs if job.what == "another")
        await outbox.send_one(fresh)
        now[0] += 1.0
        await outbox.send_one(outbox.jobs[0])
        assert failed == ["told"] and not outbox.jobs

    run(scenario())


def test_what_was_only_true_for_a_while_isnt_posted_hours_late(run: Any) -> None:
    now = [100.0]
    outbox = Outbox(clock=lambda: now[0])
    sent: list[str] = []

    async def post() -> None:
        sent.append("posted")

    async def scenario() -> None:
        outbox.put(post, what="come and fill it", expires=150.0)
        now[0] = 200.0
        await outbox.send_one(outbox.jobs[0])
        assert not sent

    run(scenario())


def test_cloudflares_answer_isnt_discords_refusal(monkeypatch: Any) -> None:
    import io
    import urllib.error

    from huragok.discordbot import rest

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError("https://discord.com/api", 403, "Forbidden", {}, io.BytesIO(b"<html>error 1015</html>"))  # type: ignore[arg-type]

    monkeypatch.setattr(rest.urllib.request, "urlopen", refused) if hasattr(rest, "urllib") else None
    try:
        send("GET", "https://discord.com/api/v10/users/@me", token="x")
    except Unreachable:
        return
    except Refused as error:
        raise AssertionError("an HTML 403 was taken for Discord refusing") from error
    raise AssertionError("it should have raised")


def test_an_empty_file_doesnt_switch_a_running_bot_off(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\ntoken = "x"\n', encoding="utf-8")
    running_now = read(path)
    path.write_text("", encoding="utf-8")
    try:
        reload_if_changed(running_now)
    except DiscordFileError as error:
        assert "mode" in str(error)
    else:
        raise AssertionError("it read as off")
    assert load(path).mode == "off"  # at the start, it is what it says
    path.write_text('mode = "off"\n', encoding="utf-8")
    assert reload_if_changed(running_now).mode == "off"  # said in so many words, it's meant


def test_the_dashboard_says_what_was_silent(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            part.state, part._signing_in_since = "signing in", time.monotonic() - 500
            assert any("signing in" in line for line in part.problems())
            part.state = "online"
            part.facts.guild = None
            assert any("isn't in its Discord" in line for line in part.problems())

    run(scenario())


def test_a_part_switched_off_is_on_the_dashboard(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            huragok.runners["discord"].disable("5 failures in 10 minutes")
            await wait_for(lambda: any("switched off" in line for line in huragok.problems), timeout=5)

    run(scenario())


def test_a_stray_refusal_of_the_token_doesnt_stay_for_good(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            part.set_state("waiting", "Discord refused the token: fix it in Huragok's window (J). (401)")
            part.look_at_discord()
            await wait_for(lambda: part.state != "waiting", timeout=15)

    run(scenario())


def test_exact_names_are_found_however_many_have_them_in_theirs(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from huragok.discordbot.asking import who_is_meant

    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.storage.record_join(ALICE, "Bob", None, when=time.time() - 3600)
            for number in range(30):
                huragok.storage.record_join(f"{number:064x}", f"Bobby{number}", None)
            assert who_is_meant(part, "Bob")[0] == ALICE
            assert huragok.storage.find_players("bob")[0].id == ALICE

    run(scenario())


def test_an_outcome_of_lost_kept_post_is_tried_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Written down before it was sent, an edit lost in an outage was never tried again."""
    from huragok.discordbot import kept

    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            form = {"content": "one", "allowed_mentions": {"parse": []}}
            channel = fake.channels[0]["id"]
            kept.keep(part, "test:kept", channel, form)
            await wait_for(lambda: part.status.stored("test:kept")[0], timeout=10)
            before = part.status.stored("test:kept")[1]
            part.outbox.paused_until = part.outbox.clock() + 60  # nothing's going anywhere
            kept.keep(part, "test:kept", channel, {**form, "content": "two"})
            assert part.status.stored("test:kept")[1] == before  # not till it's been sent
            part.outbox.paused_until = 0.0
            part.outbox._wake.set()
            await wait_for(lambda: part.status.stored("test:kept")[1] != before, timeout=10)

    run(scenario())
