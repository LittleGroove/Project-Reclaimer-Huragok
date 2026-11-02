"""The console channel put back as it started once it's been quiet: what was there kept in the logs, and only what's safe to remove removed."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from test_console import ADMIN_USER, SECOND, bot_file, reply_to, up as console_up
from test_tickets import LOGS
from testkit.fake_discord import ADMIN_ROLE, BOT_ROLE, DISCORD_EPOCH_MS, GUILD, STAFF_CHANNEL, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import consoleclear, features, tidy
from huragok.discordbot.config import load, set_channel
from huragok.discordbot.console import MEMBER, MENU, keep_menu

CONSOLE = STAFF_CHANNEL
ADMIN = {"user": ADMIN_USER, "roles": (ADMIN_ROLE,)}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.channels.append({**discord._channel(LOGS, "logs"), "permission_overwrites": [{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]})  # private, as a host's would be: a transcript won't go anywhere else
    role = next(role for role in discord.roles if role["id"] == BOT_ROLE)
    role["permissions"] = str(int(role["permissions"]) | (1 << 13) | (1 << 34))  # Manage Messages and Manage Threads, as a host who wants this gives it
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


async def up(huragok: Any, **how: Any) -> Any:
    """Up, and its first look at the channel: what was there before it was the console's is never touched, so what's cleared is what's said after that."""
    part = await console_up(huragok, **how)
    await consoleclear.sweep(part)
    return part


def console_file(root: Path, **console: Any) -> Path:
    path = bot_file(root, **console)
    set_channel(path, "logs", LOGS, "logs")
    return path


def later(part: Any, minutes: float) -> None:
    part.status.clock = lambda: time.time() + minutes * 60


def logged(fake: FakeDiscord) -> list[dict[str, Any]]:
    return [post for post in fake.posted if post["channel"] == LOGS and post.get("files")]


def old_id(days: float) -> str:
    return str((int((time.time() - days * 86400) * 1000) - DISCORD_EPOCH_MS) << 22)


async def a_line(part: Any, fake: FakeDiscord, text: str = "!mods") -> tuple[str, str]:
    line = fake.message(CONSOLE, f"{SERVER}: {text}", **ADMIN)
    answer = (await reply_to(fake, line))["id"]
    await wait_for(lambda: not part.console_running.get(CONSOLE), timeout=10)
    return line, answer


def test_a_quiet_console_goes_to_the_logs_and_the_menu_stays(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, second=True)
            keep_menu(part)
            await wait_for(lambda: part.status.stored(MENU)[0], timeout=10)
            menu = part.status.stored(MENU)[0]
            line, answer = await a_line(part, fake)
            part.status.store(MEMBER + ADMIN_USER, value=SECOND)

            await consoleclear.sweep(part)  # it's only just been said
            assert line not in fake.deleted and not logged(fake)

            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted and answer in fake.deleted, timeout=10)
            kept = logged(fake)
            assert len(kept) == 1
            assert "1 messages" not in kept[0]["content"] and "2 messages" in kept[0]["content"] and "user" + ADMIN_USER in kept[0]["content"]
            assert "!mods" in kept[0]["files"][0] and "replying to" in kept[0]["files"][0]
            assert menu not in fake.deleted and any(post["id"] == menu for post in fake.posted)

            await wait_for(lambda: any(edit["message"] == menu and "Last cleared" in edit["embeds"][0]["description"] for edit in fake.edits), timeout=10)
            assert not part.status.stored(MEMBER + ADMIN_USER)[1]  # their next line asks which server again

            reads = len(fake.message_reads)
            await consoleclear.sweep(part)  # nothing new: nothing's read, and no second file
            assert len(fake.message_reads) == reads and len(logged(fake)) == 1

    run(scenario())


def test_what_comes_after_a_clear_is_left_alone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted, timeout=10)
            part.status.clock = time.time  # and someone comes back
            second, answer = await a_line(part, fake, "!mods again")
            await consoleclear.sweep(part)
            assert second not in fake.deleted and answer not in fake.deleted and len(logged(fake)) == 1
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: second in fake.deleted, timeout=10)
            assert len(logged(fake)) == 2 and "!mods again" in logged(fake)[1]["files"][0] and "!mods\n" not in logged(fake)[1]["files"][0]

    run(scenario())


def test_a_press_or_a_line_still_running_means_its_not_quiet(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            later(part, 11)
            consoleclear.touched(part, CONSOLE)
            await consoleclear.sweep(part)
            assert not fake.message_reads
            part.console_touched.clear()
            part.console_running[CONSOLE] = 1
            await consoleclear.sweep(part)
            assert not fake.message_reads and line not in fake.deleted
            part.console_running[CONSOLE] = 0
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted, timeout=10)

    run(scenario())


def test_without_manage_messages_nothing_happens_and_it_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        role = next(role for role in fake.roles if role["id"] == BOT_ROLE)
        role["permissions"] = str(int(role["permissions"]) & ~(1 << 13))
        path = console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, answer = await a_line(part, fake)
            later(part, 11)
            await consoleclear.sweep(part)
            assert not fake.message_reads and not logged(fake) and line not in fake.deleted and answer not in fake.deleted
            problems = consoleclear.problems(part.file, part.facts)
            assert len(problems) == 1 and "Manage Messages" in problems[0]
            found, _facts = features.check(load(path))
            assert any("Manage Messages" in finding.text and not finding.good for finding in found)
            assert features.invite_permissions(load(path)) & (1 << 13)

    run(scenario())


def test_nothings_touched_unless_the_file_got_there(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, answer = await a_line(part, fake)
            later(part, 11)
            fake.refuse_next.append((f"/channels/{LOGS}/messages", 403, {"message": "Missing Access", "code": 50001}, {}))
            await consoleclear.sweep(part)
            assert line not in fake.deleted and answer not in fake.deleted and not huragok.storage.query("SELECT * FROM discord_tidy")
            assert part.status.stored(consoleclear.KEPT + CONSOLE)[1] == "0"  # no further on than its first look
            reads = len(fake.message_reads)
            await consoleclear.sweep(part)  # it isn't tried again every minute
            assert len(fake.message_reads) == reads
            later(part, 22)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted, timeout=10)
            assert len(logged(fake)) == 1

    run(scenario())


def test_what_should_stay_stays(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            pinned = fake.next_id()
            fake.typed.append({"id": pinned, "channel": CONSOLE, "content": "the rules for this channel"})
            fake.pins.append(pinned)
            notice = fake.next_id()
            fake.typed.append({"id": notice, "channel": CONSOLE, "content": "", "type": 21})  # Discord's, and can't be deleted
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted, timeout=10)
            assert pinned not in fake.deleted and notice not in fake.deleted
            assert "the rules for this channel" not in logged(fake)[0]["files"][0]

    run(scenario())


@pytest.mark.parametrize("job", ["staff", "reports"])
def test_a_channel_with_another_job_is_never_cleared(tmp_path: Path, run: Any, fake: FakeDiscord, job: str) -> None:
    async def scenario() -> None:
        set_channel(console_file(tmp_path), job, CONSOLE, job)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            later(part, 11)
            await consoleclear.sweep(part)
            assert not fake.message_reads and line not in fake.deleted
            assert any(job in problem for problem in consoleclear.problems(part.file, part.facts))

    run(scenario())


def test_nowhere_to_keep_it_means_not_cleared_unless_youve_said_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)  # no logs channel
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            later(part, 11)
            await consoleclear.sweep(part)
            assert line not in fake.deleted and "nowhere to keep" in consoleclear.problems(part.file, part.facts)[0]

    run(scenario())


def test_it_can_just_be_cleared(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, transcript=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted, timeout=10)
            assert not logged(fake)

    run(scenario())


def test_it_can_be_switched_off_and_has_a_floor(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = console_file(tmp_path, clear_after_minutes=0)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake)
            later(part, 60 * 24)
            await consoleclear.sweep(part)
            assert not fake.message_reads and line not in fake.deleted
        text = path.read_text(encoding="utf-8").replace("clear_after_minutes = 0", "clear_after_minutes = 1")
        path.write_text(text, encoding="utf-8")
        assert consoleclear.after_minutes(load(path)) == consoleclear.SMALLEST_MINUTES  # past anything waiting for an answer in there

    run(scenario())


def test_a_lot_goes_in_bulk_and_the_old_one_at_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            old = [old_id(30 - n) for n in range(3)]
            for message in old:
                fake.typed.append({"id": message, "channel": CONSOLE, "content": "a long time ago", "author": {"id": ADMIN_USER, "username": "admin"}})
            recent = [fake.next_id() for _ in range(150)]
            for message in recent:
                fake.typed.append({"id": message, "channel": CONSOLE, "content": "not long ago", "author": {"id": ADMIN_USER, "username": "admin"}})
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: not [post for post in fake.typed if post["channel"] == CONSOLE], timeout=20)
            await wait_for(lambda: not huragok.storage.query("SELECT * FROM discord_tidy"), timeout=10)
            bulk = [path for method, path in fake.requests if method == "POST" and path.endswith("bulk-delete")]
            assert 1 <= len(bulk) <= 2  # the old ones can't be in one, and the fake refuses if they were
            assert set(old) <= set(fake.deleted)

    run(scenario())


def test_a_long_backlog_carries_on_with_no_gaps(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            monkeypatched = consoleclear.PAGES
            consoleclear.PAGES = 1
            try:
                everything = [fake.next_id() for _ in range(130)]
                for number, message in enumerate(everything):
                    fake.typed.append({"id": message, "channel": CONSOLE, "content": f"line {number}", "author": {"id": ADMIN_USER, "username": "admin"}})
                later(part, 11)
                await consoleclear.sweep(part)
                await wait_for(lambda: len(logged(fake)) == 1, timeout=10)
                assert "line 99\n" in logged(fake)[0]["files"][0] and "line 100\n" not in logged(fake)[0]["files"][0]
                await consoleclear.sweep(part)
                await wait_for(lambda: len(logged(fake)) == 2, timeout=10)
                assert "line 100\n" in logged(fake)[1]["files"][0] and "line 129\n" in logged(fake)[1]["files"][0]
            finally:
                consoleclear.PAGES = monkeypatched

    run(scenario())


def test_an_answers_thread_goes_with_its_line(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread, line = fake.next_id(), fake.next_id()
            fake.typed.append({"id": line, "channel": CONSOLE, "content": "!whois Alice", "author": {"id": ADMIN_USER, "username": "admin"}, "thread": {"id": thread}})
            fake.typed.append({"id": fake.next_id(), "channel": thread, "content": "Alice: nothing on record", "author": {"id": "1", "username": "Huragok", "bot": True}})
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted and thread in fake.threads_deleted, timeout=10)
            assert "    in its thread:" in logged(fake)[0]["files"][0] and "nothing on record" in logged(fake)[0]["files"][0]

    run(scenario())


def test_a_thread_still_being_talked_in_stops_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread, line = fake.next_id(), old_id(1)
            fake.typed.append({"id": line, "channel": CONSOLE, "content": "!whois Alice", "author": {"id": ADMIN_USER, "username": "admin"}, "thread": {"id": thread}})
            fake.typed.append({"id": fake.next_id(), "channel": thread, "content": "still on it", "author": {"id": ADMIN_USER, "username": "admin"}})
            await consoleclear.sweep(part)
            assert line not in fake.deleted and not logged(fake)

    run(scenario())


def test_blank_messages_arent_kept_as_a_blank_transcript(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Discord gives blank text once the portal's switch is off: clearing then would lose it all."""

    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line = fake.next_id()
            fake.typed.append({"id": line, "channel": CONSOLE, "content": "", "author": {"id": ADMIN_USER, "username": "admin"}})
            later(part, 11)
            await consoleclear.sweep(part)
            assert line not in fake.deleted and not logged(fake)

    run(scenario())


def test_a_password_goes_at_once_and_never_reaches_the_logs(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path, confirm=[])
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            line, _ = await a_line(part, fake, "password hunter2")
            await wait_for(lambda: line in fake.deleted, timeout=10)
            said = consoleclear.transcript(part.file, "console", [{"id": fake.next_id(), "type": 0, "content": f"{SERVER}: password hunter2", "author": {"id": ADMIN_USER, "username": "admin"}},
                                                                  {"id": fake.next_id(), "type": 0, "content": "password hunter2", "author": {"id": ADMIN_USER, "username": "admin"}},
                                                                  {"id": fake.next_id(), "type": 0, "content": "seen from 203.0.113.9:11774", "author": {"id": "1", "username": "Huragok", "bot": True}}], {})
            assert "hunter2" not in said and "203.0.113.9" not in said

    run(scenario())


def test_tidying_refused_never_loops_and_gone_is_done(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            gone, refused = fake.next_id(), fake.next_id()
            fake.refuse_next.append((f"messages/{gone}", 404, {"message": "Unknown Message", "code": 10008}, {}))
            tidy.later(part, CONSOLE, gone, 0.0)
            tidy.sweep(part)
            await wait_for(lambda: not huragok.storage.query("SELECT * FROM discord_tidy"), timeout=10)
            fake.refuse_next.append((f"messages/{refused}", 403, {"message": "Missing Permissions", "code": 50013}, {}))
            tidy.later(part, CONSOLE, refused, 0.0)
            tidy.sweep(part)
            await wait_for(lambda: CONSOLE in part.tidy_paused, timeout=10)
            before = len(fake.requests)
            tidy.sweep(part)  # not asked again until Discord's been read again
            assert not [path for method, path in fake.requests[before:] if method == "DELETE"]

    run(scenario())


def test_an_answer_a_while_later_is_gone_from_the_channel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            message = fake.next_id()
            fake.typed.append({"id": message, "channel": CONSOLE, "content": "a public answer"})
            tidy.later(part, CONSOLE, message, 600.0)
            tidy.sweep(part)
            assert message not in fake.deleted
            later(part, 11)
            tidy.sweep(part)
            await wait_for(lambda: message in fake.deleted, timeout=10)

    run(scenario())


def test_what_was_there_before_it_was_the_console_is_never_touched(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """An owner points the console at a channel that already has years in it."""
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await console_up(huragok)
            history = fake.message(CONSOLE, "//from long ago", user="7001")
            await consoleclear.sweep(part)  # its first look
            line, _answer = await a_line(part, fake)
            later(part, 11)
            await consoleclear.sweep(part)
            await wait_for(lambda: line in fake.deleted, timeout=15)
            assert history not in fake.deleted

    run(scenario())


def test_a_console_waiting_on_a_public_channel_isnt_cleared(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """It ignores what's said there, as it's not private. It was still deleting it."""
    from testkit.fake_discord import PUBLIC_CHANNEL

    async def scenario() -> None:
        path = console_file(tmp_path)
        set_channel(path, "console", PUBLIC_CHANNEL, "servers")
        async with running(tmp_path) as (huragok, _, _):
            part = await console_up(huragok)
            await consoleclear.sweep(part)
            someone = fake.message(PUBLIC_CHANNEL, "just talking", user="7001")
            later(part, 11)
            await consoleclear.sweep(part)
            await asyncio.sleep(0.3)
            assert someone not in fake.deleted

    run(scenario())


def test_a_transcript_doesnt_go_where_anyone_can_read_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from testkit.fake_discord import PUBLIC_CHANNEL

    async def scenario() -> None:
        path = console_file(tmp_path)
        set_channel(path, "logs", PUBLIC_CHANNEL, "servers")
        async with running(tmp_path) as (huragok, _, _):
            part = await console_up(huragok)
            channel = part.facts.find_channel(CONSOLE)
            assert "anyone can read" in consoleclear.why_not(part.file, part.facts, channel)

    run(scenario())


def test_a_line_pointed_at_the_console_by_name_stops_the_clear(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Picking the console's channel for a kind of card writes channel = "console": a clear deleted the live cards."""
    from huragok.discordbot.config import set_message

    async def scenario() -> None:
        path = console_file(tmp_path)
        set_message(path, "tickets-opened", channel="console")
        async with running(tmp_path) as (huragok, _, _):
            part = await console_up(huragok)
            channel = part.facts.find_channel(CONSOLE)
            assert "tickets-opened" in consoleclear.why_not(part.file, part.facts, channel)

    run(scenario())


def test_a_password_with_a_colon_in_it_isnt_in_the_transcript() -> None:
    from huragok.discordbot.config import load as load_config

    config = load_config(Path("no such file"))
    said = consoleclear._words(config, {"content": "password hun:ter2"})
    assert "hun" not in " ".join(said) and "ter2" not in " ".join(said)


def test_stickers_arent_mistaken_for_message_content_being_off() -> None:
    assert not consoleclear._read_blank([{"type": 0, "content": "", "author": {"id": "1"}, "sticker_items": [{"id": "1"}]}])
    assert consoleclear._read_blank([{"type": 0, "content": "", "author": {"id": "1"}}])

