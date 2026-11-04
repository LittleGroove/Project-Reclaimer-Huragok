"""The Discord bot on what a real host has: several servers, some of them stopped, and someone pressing things quickly. Each test here is something that went wrong in a real Discord first."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, FACULTY
from test_bot_running import OTHER, _components, answer_text
from test_console import press_until, reply_to
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, REPORTS, STAFF, buttons_of, posts_in
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, FACULTY_USER, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import fleet, wait_for

from huragok.discordbot.config import add_faculty, load, make_file, set_channel, set_guild, set_mode, set_switch, set_token

STOPPED, FIRST, SECOND = "LG Backend Test", "LG Test Server", "Sim Slayer"  # as DEPLOY's list runs: the stopped one comes first


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.interaction_seconds = 3.0  # Discord's own
    private = [{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]
    discord.channels += [discord._channel(channel, name, overwrites=private) for channel, name in ((REPORTS, "reports"), (ALERTS, "alerts"), (LOGS, "logs"), (HURAGOK_LOGS, "huragok-logs"))]
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *switches: tuple[str, ...]) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_faculty(path, FACULTY_USER, "the host")
    for name, channel in (("staff", STAFF), ("reports", REPORTS), ("alerts", ALERTS), ("logs", LOGS), ("huragok-logs", HURAGOK_LOGS)):
        set_channel(path, name, channel, name)
    set_switch(path, ("commands",), "enabled", True)
    set_switch(path, ("feed", "moderation"), "enabled", True)
    for table in switches:
        set_switch(path, table, "enabled", True)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


async def saying_yes(fake: FakeDiscord, opened: str, token: str, **who: Any) -> str:
    """It asks first, as the console does, for what's on that list. The Yes's own token."""
    await wait_for(lambda: any(c["token"] == token and c.get("type") == 4 for c in fake.callbacks), timeout=10)
    asked = next(c for c in fake.callbacks if c["token"] == token)["data"]
    return fake.pick(opened, next(item["custom_id"] for item in _components(asked) if item.get("label") == "Yes"), 2, [], **who)[1]


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


async def seen_then_gone(huragok: Any, console: Any, server: str, number: int = 3, name: str = "Alice", player_id: str = ALICE) -> None:
    await console.join(number, name, player_id)
    await wait_for(lambda: huragok.superintendent.states[server].player_by_id(player_id), timeout=10)
    await console.leave(number)
    await wait_for(lambda: not huragok.superintendent.states[server].player_by_id(player_id), timeout=10)


# Which server --------------------------------------------------------------------------------------------------------------


def test_banning_someone_who_left_goes_to_a_server_thats_up(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Seen 31 October: `/ban` went to the first server in the list, which was stopped, and failed."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND), down=(STOPPED,)) as (huragok, consoles):
            await up(huragok)
            assert list(huragok.superintendent.states)[0] == STOPPED
            await seen_then_gone(huragok, consoles[SECOND], SECOND)
            opened, token = fake.command("ban", player=ALICE, time="perm", reason="testing")
            await saying_yes(fake, opened, token)
            await wait_for(lambda: consoles[FIRST].named("ban") or consoles[SECOND].named("ban"), timeout=15)
            rows = huragok.storage.query("SELECT server, ok, reply FROM actions WHERE kind = 'ban' AND target_id = ? AND (evidence IS NULL OR evidence NOT LIKE 'kicked on %')", (ALICE,))
            assert rows and rows[0]["ok"] and rows[0]["server"] != STOPPED, [dict(row) for row in rows]
            await wait_for(lambda: consoles[FIRST].named("ban") and consoles[SECOND].named("ban"), timeout=15)  # and it's on every server that's up

    run(scenario())


def test_looking_someone_up_doesnt_care_whats_stopped(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,), down=(STOPPED,)) as (huragok, consoles):
            await up(huragok)
            await seen_then_gone(huragok, consoles[FIRST], FIRST)
            _, token = fake.command("whois", player=ALICE)
            assert "Alice" in await answer_text(fake, token)

    run(scenario())


def test_a_cards_button_when_the_server_on_the_card_has_stopped(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,), down=(STOPPED,)) as (huragok, consoles):
            part = await up(huragok)
            await seen_then_gone(huragok, consoles[FIRST], FIRST)
            part.tickets.open_by_hand("Host", ALICE, "Alice", STOPPED)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            post = posts_in(fake, STAFF)[0]
            _, token = fake.press(buttons_of(post)["Ban"], post)
            await wait_for(lambda: any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks), timeout=10)
            fake.fill_in(token, post, {"Why (they'll read this)": "testing"})
            await wait_for(lambda: consoles[FIRST].named("ban"), timeout=15)

    run(scenario())


def test_with_nothing_up_it_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,), down=(STOPPED,)) as (huragok, consoles):
            part = await up(huragok)
            await seen_then_gone(huragok, consoles[FIRST], FIRST)
            huragok.superintendent.states[FIRST].connected = False
            assert part.where_to_run(ALICE) == STOPPED  # the first: looking still works
            assert part.where_to_run(ALICE, FIRST) == FIRST

    run(scenario())


def test_where_to_run_goes_where_the_player_is(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND), down=(STOPPED,)) as (huragok, consoles):
            part = await up(huragok)
            await consoles[SECOND].join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SECOND].player_by_id(ALICE), timeout=10)
            assert part.where_to_run(ALICE, FIRST) == SECOND
            assert part.where_to_run(BRAVO, FIRST) == FIRST
            assert part.where_to_run(BRAVO, STOPPED) == FIRST
            assert part.where_to_run(BRAVO) == FIRST

    run(scenario())


# Pressing things quickly --------------------------------------------------------------------------------------------------


async def features_panel(fake: FakeDiscord, wanted: str) -> tuple[str, dict[str, Any]]:
    """/huragok setup, to the page of Features that has this one."""
    opened, token = fake.huragok("setup")
    await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=15)
    form = next(c for c in fake.callbacks if c["token"] == token)["data"]
    form = await press(fake, opened, _label(form, "Features"), 2)
    for _ in range(12):
        if any(option["value"] == wanted for option in _select(form)["options"]):
            return opened, form
        form = await press(fake, opened, _label(form, "Next"), 2)
    raise AssertionError(f"{wanted} isn't on any page of Features")


def _label(form: dict[str, Any], label: str) -> str:
    return next(item["custom_id"] for item in _components(form) if item.get("label") == label)


def _select(form: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in _components(form) if item.get("type") == 3)


async def press(fake: FakeDiscord, opened: str, custom_id: str, kind: int, values: list[str] | None = None) -> dict[str, Any]:
    before = len(fake.callbacks)
    fake.pick(opened, custom_id, kind, values or [])
    await wait_for(lambda: len(fake.callbacks) > before, timeout=4)  # past Discord's 3 seconds, it's refused
    return fake.callbacks[-1]["data"]


def test_switching_features_quickly_is_answered_every_time_and_registered_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Seen 31 October: one registration a switch hit Discord's limit, and the wait outlasted the 3 seconds a press has."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            before = fake.registrations
            opened, form = await features_panel(fake, "reports")
            for key in ("reports", "reports", "reports", "reports", "reports", "reports", "reports", "reports", "reports"):
                form = await press(fake, opened, _select(form)["custom_id"], 3, [key])
            await wait_for(lambda: "report" in {command["name"] for command in fake.commands[GUILD]}, timeout=15)
            assert fake.registrations == before + 1

    run(scenario())


def test_a_press_doesnt_wait_when_discord_says_to_wait(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            opened, form = await features_panel(fake, "reports")
            fake.slow_down("/commands", 20.0)
            await press(fake, opened, _select(form)["custom_id"], 3, ["reports"])
            assert load(path).section("reports")["enabled"] is True

    run(scenario())


def test_many_changes_over_time_stay_under_discords_limit(tmp_path: Path, run: Any, fake: FakeDiscord, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each one settled, one after another, as someone working through the setup does. Past a few, the next one waits its turn."""
    from huragok.discordbot import client

    monkeypatch.setattr(client, "COMMANDS_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(client, "COMMANDS_BURST", 2)
    monkeypatch.setattr(client, "COMMANDS_BURST_SECONDS", 1.5)

    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            fake.registration_tokens = 100.0  # this measures ours, not the fake's
            when: list[float] = []
            for turn in range(5):
                sent = fake.registrations
                set_switch(path, ("reports",), "enabled", turn % 2 == 0)
                await part.reload(force=True)
                await wait_for(lambda: fake.registrations > sent, timeout=15)
                when.append(time.monotonic())
            assert all(when[n + 2] - when[n] >= 1.0 for n in range(len(when) - 2)), when

    run(scenario())


def test_a_switch_that_needs_a_new_sign_in_gets_its_answer_first(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "console", STAFF, "console")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            signed_in = len(fake.identifies)
            opened, form = await features_panel(fake, "console")
            await press(fake, opened, _select(form)["custom_id"], 3, ["console"])
            await wait_for(lambda: len(fake.identifies) == signed_in + 1, timeout=15)
            await wait_for(lambda: part.state == "online" and part.facts is not None, timeout=15)
            assert fake.identifies[-1]["intents"] & (1 << 15)

    run(scenario())


def test_two_quick_switches_that_need_a_sign_in_are_one_sign_in(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "console", STAFF, "console")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            signed_in = len(fake.identifies)
            set_switch(path, ("console",), "typed", True)
            await part.reload(force=True)
            set_switch(path, ("console",), "typed", False)
            await part.reload(force=True)
            set_switch(path, ("console",), "typed", True)
            await part.reload(force=True)
            await wait_for(lambda: len(fake.identifies) == signed_in + 1, timeout=15)
            await wait_for(lambda: part.state == "online", timeout=15)

    run(scenario())


# The whole of Stage 3, as the host's list has it --------------------------------------------------------------------------


def test_stage_3_from_end_to_end_on_a_fleet(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """What the host is asked to try, in one go, with a stopped server first in the list. Nothing here may log an error (the fixture in conftest), or be refused by the fake."""

    async def scenario() -> None:
        path = bot_file(tmp_path, ("reports",), ("appeals",), ("feed", "requests"), ("feed", "alerts"))
        set_channel(path, "console", STAFF, "console")
        set_switch(path, ("console",), "typed", True)
        set_switch(path, ("reports",), "reporter_cooldown_seconds", 0)
        async with fleet(tmp_path, up=(FIRST, SECOND), down=(STOPPED,)) as (huragok, consoles):
            part = await up(huragok)
            await wait_for(lambda: "appeal" in {command["name"] for command in fake.commands.get(GUILD, [])}, timeout=20)
            await consoles[SECOND].join(1, "Faculty", FACULTY)
            await consoles[SECOND].join(3, "Alice", ALICE)
            await consoles[SECOND].join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SECOND].players) == 3, timeout=10)

            # Commands, with and without a player on
            _, token = fake.command("players", server=SECOND)
            assert "Alice" in await answer_text(fake, token)
            _, token = fake.command("status")
            await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=5)
            _, token = fake.command("status")
            await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=5)
            _, token = fake.command("server", name=SECOND)
            await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=5)

            # A report, and a mute on its card
            await consoles[SECOND].chat(BRAVO, "Bravo", "!report Alice camping")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            card = posts_in(fake, REPORTS)[0]
            _, token = fake.press(buttons_of(card)["Mute"], card)
            await wait_for(lambda: any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks), timeout=5)
            fake.fill_in(token, card, {"How long": "10m", "Why (they'll read this)": "camping"})
            await wait_for(lambda: consoles[SECOND].named("mute"), timeout=15)
            await wait_for(lambda: part.reports.track_record(BRAVO) == "1 report, 1 acted on", timeout=15)

            # A ban, typed in the console, then an appeal, and lifting it
            line = fake.message(STAFF, f"{SECOND}: !ban Alice perm slurs")
            asked = await reply_to(fake, line)  # a ban's one of those that ask first
            await press_until(fake, lambda: consoles[SECOND].named("ban"), buttons_of(asked)["Yes"], asked)
            await wait_for(lambda: part.tickets.standing(ALICE)["banned"], timeout=15)
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            await wait_for(lambda: any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks), timeout=5)
            fake.fill_in(token, {"channel": STAFF, "id": "1"}, {"What happened?": "carried away", "Why should it be lifted?": "won't again"}, user=OTHER)
            await wait_for(lambda: len(posts_in(fake, REPORTS)) >= 2, timeout=15)
            appeal = next(post for post in posts_in(fake, REPORTS) if "Lift it" in buttons_of(post))
            fake.press(buttons_of(appeal)["Lift it"], appeal)
            await wait_for(lambda: consoles[SECOND].named("unban"), timeout=15)
            await wait_for(lambda: any(post["channel"] == f"dm{OTHER}" for post in fake.posted), timeout=15)
            assert not part.tickets.standing(ALICE)["banned"]

    run(scenario())


def test_a_stopped_server_isnt_called_healthy(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A stopped server read "healthy · RCON 1 ms" for eight hours: what the watchdog last saw, said as if it were now. /health reads the same words."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,), down=(STOPPED,)) as (huragok, consoles):
            await up(huragok)
            watchdog = huragok.runners["watchdog"].part
            state = huragok.superintendent.states[FIRST]
            await wait_for(lambda: state.rcon_latency_ms is not None, timeout=15)
            assert watchdog.summary(FIRST).startswith("healthy") and "RCON" in watchdog.summary(FIRST)
            assert "healthy" not in watchdog.summary(STOPPED)
            _, token = fake.command("health", server=STOPPED)
            shown = await answer_text(fake, token)
            assert "healthy" not in shown and " ms" not in shown
            await consoles[FIRST].drop()  # its console going away, as when it stops
            await wait_for(lambda: not state.connected, timeout=15)
            assert state.rcon_latency_ms is None
            assert "healthy" not in watchdog.summary(FIRST) and " ms" not in watchdog.summary(FIRST) and "players" not in watchdog.summary(FIRST)
            row = next(row for row in huragok.superintendent.fleet.rows() if row["name"] == FIRST)
            assert row["rcon_ms"] is None

    run(scenario())
