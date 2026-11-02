"""What's said to one person goes by itself, and what they ask to read stays as long as it can. And the small commands that help someone see what's what."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ALICE
from test_bot_running import OTHER, _components, answer_text
from test_tickets import LOGS, MOD_ROLE, STAFF, bot_file, fake, posts_in, strike, up  # noqa: F401
from testkit.fake_discord import FakeDiscord
from testkit.running import running, wait_for

from huragok.discordbot import tidy
from huragok.discordbot.config import load, set_switch


def test_the_lengths_are_settings_and_a_private_one_cant_outlive_its_token(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert tidy.minutes_for(part, "done") == 10 and tidy.minutes_for(part, "looks") == 14
            set_switch(path, ("answers",), "looks_minutes", 60)
            await part.reload(force=True)
            assert tidy.minutes_for(part, "looks") == tidy.LONGEST_PRIVATE_MINUTES  # Discord won't let the bot touch it after 15
            set_switch(path, ("answers",), "public_minutes", 60)
            await part.reload(force=True)
            assert tidy.minutes_for(part, "public") == 60

    run(scenario())


def test_an_answer_goes_from_their_screen(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("answers",), "done_minutes", 0.01)
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            _, token = fake.command("kick", user=OTHER, player=ALICE)  # refused, and that's all it says
            assert "for moderators" in await answer_text(fake, token)
            await wait_for(lambda: any(one == token for one, _ in fake.answers_deleted), timeout=10)

    run(scenario())


def test_nothing_goes_if_its_set_to_stay(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("answers",), "done_minutes", 0)
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert tidy.minutes_for(part, "done") == 0
            _, token = fake.command("kick", user=OTHER, player=ALICE)
            await answer_text(fake, token)
            assert not fake.answers_deleted

    run(scenario())


def test_tickets_lists_whats_waiting_with_links(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            _, token = fake.command("tickets", user=OTHER)
            assert "for staff" in await answer_text(fake, token)
            _, token = fake.command("tickets", user=OTHER, roles=(MOD_ROLE,))
            assert "Nothing's waiting" in await answer_text(fake, token)
            post = await strike(huragok, rcon, fake)
            _, token = fake.command("tickets", user=OTHER, roles=(MOD_ROLE,))
            shown = await answer_text(fake, token)
            assert post["id"] in shown and "Alice" in shown and "strike" in shown

    run(scenario())


def test_flags_can_be_listed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            huragok.storage.set_flag(ALICE, "watch", "spawn killing", "Mod")
            _, token = fake.slash("flag", "list", user=OTHER, roles=(MOD_ROLE,), player=ALICE)
            shown = await answer_text(fake, token)
            assert "watch" in shown and "spawn killing" in shown

    run(scenario())


def test_saying_hello_shows_each_channel_works_and_goes_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            opened, token = fake.huragok("setup")
            await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=15)
            form = next(c for c in fake.callbacks if c.get("token") == token)["data"]
            hello = next(item["custom_id"] for item in _components(form) if item.get("label") == "Say hello in every channel")
            _, token = fake.pick(opened, hello, 2, [])
            await wait_for(lambda: any(f["token"] == token for f in fake.followups), timeout=15)
            said = next(f for f in fake.followups if f["token"] == token)["content"]
            assert "**staff**" in said and "**logs**" in said
            assert any("This is **staff**" in str(post.get("content")) for post in fake.posted)
            assert huragok.storage.query("SELECT * FROM discord_tidy")  # they go by themselves
            assert load(path).token  # nothing about it changes the file
            assert part.file.guild

    run(scenario())


def test_the_gallery_doesnt_stay_in_the_channel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            before = len(posts_in(fake, LOGS))
            _, token = fake.huragok("gallery")
            await wait_for(lambda: any(f["token"] == token and "That's every message" in str(f.get("content")) for f in fake.followups), timeout=30)
            assert len(posts_in(fake, LOGS)) == before

    run(scenario())
