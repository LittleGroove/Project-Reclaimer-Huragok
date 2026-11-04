"""That what's been built is reached. Each of these passed with its fix taken away until this file: the timers were only ever called by hand in tests, and panels' helpers rather than the panels (plans/discord-audit/verify-fixes.md, section 5)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from conftest import ALICE
from test_audit_findings import FIRST, on, said_back, up  # noqa: F401
from test_audit_findings import bot_file as audit_file
from test_console_clear import a_line, console_file, later, fake as console_fake  # noqa: F401
from test_console import up as console_up
from test_tickets import ADMIN_ROLE, MOD_ROLE, bot_file, buttons_of, fake, fill, form_of, posts_in, row_of, strike  # noqa: F401
from testkit.fake_discord import PUBLIC_CHANNEL, STAFF_CHANNEL, FakeDiscord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.discordbot import consoleclear, tidy
from huragok.discordbot.config import load, set_switch
from huragok.discordbot.ranks import Someone, game_rank, rank_of

ADMIN = {"user": "4441", "roles": (ADMIN_ROLE,)}
MODERATOR = {"user": "4442", "roles": (MOD_ROLE,)}


def test_the_part_runs_the_clear_and_the_tidying_by_itself(tmp_path: Path, run: Any, fake: FakeDiscord, monkeypatch: Any) -> None:
    """Every test called these by hand, so taking them out of the part's own loop failed nothing."""
    calls: list[str] = []
    real_clear, real_tidy = consoleclear.sweep, tidy.sweep

    async def clear(part: Any) -> None:
        calls.append("clear")
        await real_clear(part)

    def sweep(part: Any) -> None:
        calls.append("tidy")
        real_tidy(part)

    async def scenario() -> None:
        monkeypatch.setattr(consoleclear, "sweep", clear)
        monkeypatch.setattr(tidy, "sweep", sweep)
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.ticket_sweep()
            await wait_for(lambda: "clear" in calls and "tidy" in calls, timeout=10)

    run(scenario())


def test_that_runs_on_a_timer() -> None:
    from huragok.discordbot.part import DiscordPart

    assert DiscordPart.ticket_sweep.__dict__.get("_part_every") == 60


def test_a_press_counts_as_someone_being_there(tmp_path: Path, run: Any, console_fake: FakeDiscord) -> None:
    """Pressing a menu in the console isn't a message, and a clear under someone's finger would lose what they were doing."""
    fake = console_fake

    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await console_up(huragok)
            await consoleclear.sweep(part)
            line, _answer = await a_line(part, fake)
            later(part, 11)
            consoleclear.touched(part, STAFF_CHANNEL)
            await consoleclear.sweep(part)
            await asyncio.sleep(0.2)
            assert line not in fake.deleted

    run(scenario())


def test_a_line_still_running_holds_a_clear_off_in_the_real_path(tmp_path: Path, run: Any, console_fake: FakeDiscord) -> None:
    fake = console_fake

    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await console_up(huragok)
            await consoleclear.sweep(part)
            hold = asyncio.Event()

            async def slow(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
                await hold.wait()
                return True, "done"

            part.ctx.console_line = slow
            line = fake.message(STAFF_CHANNEL, f"{SERVER}: maps", roles=(ADMIN_ROLE,), user="4441")
            await wait_for(lambda: part.console_running.get(STAFF_CHANNEL), timeout=10)
            later(part, 11)
            await consoleclear.sweep(part)
            await asyncio.sleep(0.2)
            assert line not in fake.deleted
            hold.set()
            await wait_for(lambda: not part.console_running.get(STAFF_CHANNEL), timeout=10)

    run(scenario())


def test_talk_with_no_command_holds_a_clear_off(tmp_path: Path, run: Any, console_fake: FakeDiscord) -> None:
    """console_touched only counts presses and lines: what's quiet is the channel's last message."""
    fake = console_fake

    async def scenario() -> None:
        console_file(tmp_path, ignore_prefix="//")
        async with running(tmp_path) as (huragok, _, _):
            part = await console_up(huragok)
            await consoleclear.sweep(part)
            line = fake.message(STAFF_CHANNEL, "//just talking", roles=(ADMIN_ROLE,), user="4441")
            part.console_touched.clear()
            await consoleclear.sweep(part)
            await asyncio.sleep(0.2)
            assert line not in fake.deleted

    run(scenario())


def test_a_refused_appeal_deny_doesnt_open_its_form(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = audit_file(tmp_path, ("appeals",))
        set_switch(path, ("tickets",), "close_rank", "admin")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Faculty", source="in game", reason="x")
            number = part.appeals.file("7777", "Alice", ALICE, "Alice", "ban", [("What happened?", "x")])
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            ticket = part.tickets.get(number)
            card = {**part.tickets.edit_form(ticket), "id": ticket["message"], "channel": ticket["channel"]}
            _, token = fake.press(buttons_of(card)["Deny"], card, **MODERATOR)
            assert "or above" in await said_back(fake, token)

    run(scenario())


def test_faculty_in_game_reaches_what_they_can_do(tmp_path: Path) -> None:
    """It was only tested as a function: nothing proved a command saw it."""
    path = tmp_path / "discord.toml"
    path.write_text('[ranks]\nfaculty = ["9"]\nfaculty_in_game = "admin"\n', encoding="utf-8")
    config = load(path)
    someone = Someone("9", {}, False, False)
    assert rank_of(config, someone) == "faculty" and game_rank(config, someone) == "admin"


def test_a_channel_event_gets_discord_read_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Fixing a permission in Discord shows at once: that's what these events are for."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await wait_for(lambda: part.looking == 0, timeout=15)
            looked = part._looked
            await asyncio.sleep(0.01)
            await part.client.on_guild_channel_update(None, None)
            assert part._looked > looked or part.looking or part._look_again
            await part.client.on_guild_role_update(None, None)
            await wait_for(lambda: part.looking == 0 and not part._look_again, timeout=15)

    run(scenario())
