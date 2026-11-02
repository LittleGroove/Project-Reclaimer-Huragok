"""A new host, from the first press of /huragok setup: it says what's left, each pick answers at once, and nothing that could only be set in the file stays that way."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE
from test_bot_running import OTHER, _components
from testkit.fake_discord import ADMIN_ROLE, BOT_ROLE, GUILD, MOD_ROLE, OWNER_USER, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import features
from huragok.discordbot.config import add_owner, load, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.discordbot.startover import start_over

LOGS = "4203"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.channels.append(discord._channel(LOGS, "logs", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}, {"id": BOT_ROLE, "type": 0, "allow": str(1 << 10), "deny": "0"}]))
    role = next(role for role in discord.roles if role["id"] == BOT_ROLE)
    role["permissions"] = str(int(role["permissions"]) | (1 << 34) | (1 << 36) | (1 << 38))  # Manage Threads, Create Private Threads, Send in Threads: what Contact needs, ticked
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bare_file(root: Path) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


async def panel(fake: FakeDiscord, token: str) -> dict[str, Any]:
    await wait_for(lambda: any(c.get("token") == token and c.get("type") in (4, 7) for c in fake.callbacks), timeout=15)
    return next(c for c in reversed(fake.callbacks) if c.get("token") == token)["data"]


def labels(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label"]: item for item in _components(form) if item.get("label")}


def test_home_says_whats_left_and_whats_next(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            _, token = fake.huragok("setup")
            form = await panel(fake, token)
            shown = form["embeds"][0]["description"]
            assert "⬜" in shown and "nothing's switched on" in shown and "Until then only owners" in shown and "nobody's phone buzzes" in shown
            buttons = labels(form)
            assert "Recommended" in buttons and any(label.startswith("Next:") for label in buttons) and "Who gets told" in buttons and "Commands" in buttons

    run(scenario())


def test_recommended_switches_on_what_staff_need_and_nothing_public(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            opened, token = fake.huragok("setup")
            form = await panel(fake, token)
            fake.pick(opened, labels(form)["Recommended"]["custom_id"], 2, [])
            await wait_for(lambda: features.BY_KEY["feed.moderation"].is_on(load(path)), timeout=10)
            config = load(path)
            on = {feature.key for feature in features.FEATURES if feature.is_on(config)}
            assert set(features.RECOMMENDED) <= on
            assert not {"status.board", "status.seeding", "feed.games", "contact"} & on

    run(scenario())


def test_a_channel_pick_says_at_once_if_it_works(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            from huragok.discordbot.panels import _verdict

            set_channel(path, "staff", PUBLIC_CHANNEL, "servers")
            await part.reload(force=True)
            assert "@everyone can read" in _verdict(part, "staff", PUBLIC_CHANNEL)
            assert "can post" in _verdict(part, "staff", STAFF_CHANNEL)
            assert "can't see" in _verdict(part, "staff", "9999")
            assert "can post" in _verdict(part, "status", PUBLIC_CHANNEL)  # it's for everyone: nothing wrong with that

    run(scenario())


def test_what_isnt_built_stays_out_of_the_way(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            from huragok.discordbot.panels import Channels, Features

            keys = Channels(part).keys
            assert keys[:2] == ["staff", "logs"]
            shown = Features(part).shown
            assert all(feature.stage <= features.BUILT for feature in shown)
            assert all(feature.key in features.WHAT for feature in shown)

    run(scenario())


def test_every_built_feature_says_what_it_does() -> None:
    assert not [feature.key for feature in features.FEATURES if feature.stage <= features.BUILT and feature.key not in features.WHAT]


def test_who_gets_told_sets_every_line_of_a_group(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            from huragok.discordbot.config import set_message

            assert not features.steps(part.file, part.facts)[4].done
            for key in dict(features.TOLD)["A server in trouble"]:
                set_message(path, key, ping=MOD_ROLE)
            await part.reload(force=True)
            assert features.steps(part.file, part.facts)[4].done
            assert part.feeds.ping_of(part.file.section("messages")["alerts-crashed"])[1] == (MOD_ROLE,)

    run(scenario())


def test_a_call_for_an_admin_is_never_silent(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    config = load(Path("no such file"))
    for key in ("calls-called", "appeals-made", "contact-opened", "reports-many"):
        assert config.section("messages")[key]["silent"] is False, key


def test_nothing_says_share_a_channel_on_the_defaults(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        set_channel(path, "staff", STAFF_CHANNEL, "tickets")
        set_channel(path, "logs", LOGS, "logs")
        for key in ("moderation", "servers", "alerts", "vergil"):
            set_switch(path, ("feed", key), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            found, _ = features.check(load(path))
            assert not [finding for finding in found if "give them a channel each" in finding.text]

    run(scenario())


def test_a_role_with_a_rank_that_cant_see_its_tickets_is_named(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        set_channel(path, "staff", STAFF_CHANNEL, "tickets")
        set_switch(path, ("commands",), "enabled", True)
        text = path.read_text(encoding="utf-8").replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
        path.write_text(text, encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            found, _ = features.check(load(path))
            said = " ".join(finding.text for finding in found if not finding.good)
            assert "can't see" in said and "won't see /mute" in said

    run(scenario())


def test_no_ranks_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            found, _ = features.check(load(path))
            assert any("only owners can press" in finding.text for finding in found if not finding.good)

    run(scenario())


def test_what_contact_needs_includes_somewhere_to_tell_staff(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        set_channel(path, "info", PUBLIC_CHANNEL, "servers")
        set_switch(path, ("contact",), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert "nowhere to tell staff" in features.why_waiting(features.BY_KEY["contact"], part.file, part.facts)
            set_channel(path, "reports", PUBLIC_CHANNEL, "servers")
            await part.reload(force=True)
            assert "can be read by @everyone" in features.why_waiting(features.BY_KEY["contact"], part.file, part.facts)
            set_channel(path, "reports", STAFF_CHANNEL, "tickets")
            await part.reload(force=True)
            assert features.why_waiting(features.BY_KEY["contact"], part.file, part.facts) == ""

    run(scenario())


def test_a_call_that_cant_reach_anyone_isnt_told_it_did(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        set_channel(path, "staff", PUBLIC_CHANNEL, "servers")
        set_switch(path, ("feed", "calls"), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            from huragok.discordbot import cards

            assert part.feeds.reaches(cards.called(part.file, SERVER, "Alice", "", [])) is False

    run(scenario())


def test_the_feed_public_switch_doesnt_open_the_console(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        set_channel(path, "console", PUBLIC_CHANNEL, "servers")
        set_switch(path, ("feeds",), "public", True)
        set_switch(path, ("console",), "typed", True)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            fake.message(PUBLIC_CHANNEL, "!mods", user=OWNER_USER)
            await wait_for(lambda: any(post.get("message_reference") for post in fake.posted), timeout=10)
            assert "can be read by everyone" in next(post for post in fake.posted if post.get("message_reference"))["content"]

    run(scenario())


def test_starting_over_keeps_the_token_and_what_staff_wrote(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bare_file(tmp_path)
        set_channel(path, "staff", STAFF_CHANNEL, "tickets")
        set_switch(path, ("feed", "alerts"), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
        database = tmp_path / "copy.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE discord_posts (key TEXT, message TEXT)")
        connection.execute("CREATE TABLE player_notes (text TEXT)")
        connection.execute("INSERT INTO discord_posts VALUES ('seeding:Server 1', '5')")
        connection.execute("INSERT INTO player_notes VALUES ('watch them')")
        connection.commit()
        connection.close()
        said = start_over(path, database)
        assert any("kept as" in line for line in said)
        config = load(path)
        assert config.token == TOKEN and config.mode == "bot" and not config.guild and not config.section("ranks")["owners"]
        assert not features.BY_KEY["feed.alerts"].is_on(config)
        connection = sqlite3.connect(database)
        assert connection.execute("SELECT COUNT(*) FROM discord_posts").fetchone()[0] == 0 and connection.execute("SELECT COUNT(*) FROM player_notes").fetchone()[0] == 1
        connection.close()
        assert list(tmp_path.glob("discord.before-start-over-*.toml"))

    run(scenario())


def test_every_panel_can_be_drawn(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Discord refuses a panel with more than it can hold, and only says when it's sent: so each is sent, on the strict kit."""

    async def scenario() -> None:
        bare_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            from huragok.discordbot import panels

            for screen in (panels.Home, panels.Features, panels.Channels, panels.Ranks, panels.Owners, panels.WhoGetsTold, panels.Messages, panels.Tickets, panels.CommandsPanel, panels.Console, panels.ConsoleMore,
                           panels.ReportsPanel, panels.AppealsPanel, panels.ContactPanel, panels.StatusPanel, panels.NotesPanel):
                view = screen(part)
                assert view.card().to_dict()["description"]
                rows = view.to_components()  # as it's sent: discord.py lays out what has no row of its own, and says if it can't
                assert rows, screen.__name__

    run(scenario())
