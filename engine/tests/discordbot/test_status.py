"""The servers at a glance: the board edited in place, the bot's own status line, bio and nickname, channel names, topics and voice status."""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE
from testkit.fake_discord import GUILD, OWNER_USER, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.core.tomledit import set_nested_key
from huragok.discordbot import features
from huragok.discordbot.config import add_owner, load, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.discordbot.status import UNCHANGED_EVERY, Status, condition, fill


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *switches: str) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    set_channel(path, "status", PUBLIC_CHANNEL, "servers")
    for name in switches:
        set_switch(path, ("status", name), "enabled", True)
    return path


def part_of(huragok: Any) -> Any:
    return huragok.runners["discord"].part


async def up(huragok: Any) -> Any:
    part = part_of(huragok)
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    await wait_for(lambda: huragok.superintendent.fleet.rows()[0]["phase"] in ("running", "external"), timeout=15)
    return part


# Pure --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("phase, players, limit, how", [("running", 1, 16, "up"), ("running", 16, 16, "busy"), ("external", 0, None, "up"), ("countdown", 2, 16, "up"),
                                                         ("booting", 0, 16, "restarting"), ("idle", 0, 16, "stopped"), ("faulted", 0, 16, "down")])
def test_how_a_server_is(phase: str, players: int, limit: int | None, how: str) -> None:
    from types import SimpleNamespace

    assert condition({"phase": phase}, SimpleNamespace(max_players=limit, players=dict.fromkeys(range(players)))) == how


def test_a_template_never_fails_and_has_no_gaps() -> None:
    assert fill("{map} · {mode} on {server}", {"map": "Guardian", "mode": "Slayer", "server": "A"}) == "Guardian · Slayer on A"
    assert fill("{nothing_yet} Spartans", {}) == "Spartans"
    assert fill("broken {", {}) == "broken {"


def test_every_status_feature_is_built(tmp_path: Path) -> None:
    built = {f.key for f in features.FEATURES if f.stage <= features.BUILT}
    assert {"status.board", "status.presence", "status.bio", "status.nickname", "status.channel_names", "status.topics", "status.voice_status", "status.seeding"} <= built


# The board, for real on the fake --------------------------------------------------------------------------------------


def test_the_board_posts_once_and_edits_from_then_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            card = fake.posted[0]["embeds"][0]
            assert card["title"] == SERVER and "Online" in card["description"] and "updated <t:" in card["description"]
            assert fake.posted[0]["username"] == "Superintendent" and fake.posted[0]["channel"] == PUBLIC_CHANNEL
            assert card["color"] == 0x57F287  # green, by [status.board] colours
            # Nothing new: nothing sent.
            part.status.due.clear()
            await part.status.tick()
            await asyncio.sleep(0.3)
            assert fake.edits == []
            # A player joins: the same message says so.
            await rcon.join(1, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.edits) == 1, timeout=15)
            edit = fake.edits[0]
            assert edit["message"] == fake.posted[0]["id"] and len(fake.posted) == 1
            assert any(f["name"] == "On now" and "Alice" in f["value"] for f in edit["embeds"][0]["fields"])
            # Its "updated" is kept true every so often, even with nothing new.
            part.status.seen_at[f"board:{SERVER}"] -= UNCHANGED_EVERY + 1
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.edits) == 2, timeout=15)

    run(scenario())


def test_a_restart_edits_the_same_message(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        kept = tmp_path / "kept.sqlite3"

        def on_a_file(settings: Any) -> None:  # tests' databases are otherwise in memory
            settings.database = kept

        async with running(tmp_path, adjust=on_a_file) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=10)
        async with running(tmp_path, adjust=on_a_file) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.edits) == 1, timeout=15)
            assert len(fake.posted) == 1 and fake.edits[0]["message"] == fake.posted[0]["id"]

    run(scenario())


def test_a_deleted_board_is_posted_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=10)
            fake.posted.clear()  # someone deleted it
            await rcon.join(1, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: part.status.rebuild, timeout=15)  # the whole list again, to keep it in order
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            assert f"channel:{PUBLIC_CHANNEL}" not in part.feeds.blocked

    run(scenario())


def test_one_edited_too_often_is_posted_fresh_at_most_hourly(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=15)
            fake.refuse_next.append(("/messages/", 400, {"message": "This message cannot be edited due to announcement rate limits.", "code": 30046}, {}))
            await rcon.join(1, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: part.status.rebuild, timeout=15)
            assert f"board:{SERVER}" in part.status.reposted

    run(scenario())


def test_a_server_taken_away_takes_its_card_with_it_and_the_list_is_posted_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=15)
            first = fake.posted[0]["id"]
            # As if a second server had been there, and has since gone.
            gone = fake.posted[0]["channel"]
            fake.posted.append({"id": "9000", "channel": gone, "via": "webhook", "embeds": [{"description": "Gone"}]})
            part.status.store("board:Gone", message="9000")
            part.status.store("board:list", value=json.dumps({"keys": ["board:Gone", f"board:{SERVER}"], "where": PUBLIC_CHANNEL}))
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1 and fake.posted[0]["id"] not in ("9000", first), timeout=15)
            assert part.status.stored("board:Gone") == ("", "")
            assert json.loads(part.status.stored("board:list")[1])["keys"] == [f"board:{SERVER}"]

    run(scenario())


def test_nothing_to_show_never_wipes_the_board(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=15)
            part.status.rows = lambda: []
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert len(fake.posted) == 1 and part.status.stored(f"board:{SERVER}")[0]

    run(scenario())


def test_cards_from_before_the_list_was_kept_are_taken_as_they_are(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=15)
            part.status.forget("board:list")
            before = fake.posted[0]["id"]
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: part.status.stored("board:list")[1], timeout=10)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert [post["id"] for post in fake.posted] == [before]

    run(scenario())


def test_the_join_button_goes_to_the_games_own_page(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "board")
        set_nested_key(path, ("status", "board"), "join_address", {SERVER: "198.51.100.7:11774"})
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            [row] = fake.posted[0]["components"]
            [button] = row["components"]
            assert button["style"] == 5 and button["label"] == "Join"
            assert button["url"] == "https://projectreclaimer.dev/join?server=198.51.100.7:11774"

    run(scenario())


def test_one_message_for_every_server(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "board")
        set_nested_key(path, ("status", "board"), "layout", "one")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            assert SERVER in fake.posted[0]["embeds"][0]["description"] and fake.posted[0]["components"] == []

    run(scenario())


def test_a_player_name_cant_ping_from_the_board(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.join(1, "@everyone", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            assert fake.posted[0]["allowed_mentions"] == {"parse": []}
            assert "@everyone" not in str(fake.posted[0]["embeds"])

    run(scenario())


def test_webhook_mode_keeps_a_board_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from huragok.discordbot import rest

    async def scenario() -> None:
        address = rest.create_webhook(TOKEN, PUBLIC_CHANNEL, "Mine")["url"]
        path = bot_file(tmp_path, "board")
        set_mode(path, "webhook")
        set_nested_key(path, ("status", "board"), "webhook", address)
        async with running(tmp_path) as (huragok, rcon, _):
            part = part_of(huragok)
            await wait_for(lambda: part.state == "webhook", timeout=15)
            await wait_for(lambda: huragok.superintendent.fleet.rows()[0]["phase"] in ("running", "external"), timeout=15)  # a server that's down shows no players
            await part.status.tick()
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            assert fake.posted[0]["username"] == "Superintendent" and fake.unversioned == 0
            await wait_for(lambda: part.status.stored(f"board:{SERVER}")[0], timeout=10)
            await rcon.join(1, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.edits) == 1, timeout=15)
            assert fake.identifies == []

    run(scenario())


# The bot itself -------------------------------------------------------------------------------------------------------


def test_the_status_line_rotates_and_says_how_the_servers_are(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "presence")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: fake.presences, timeout=15)
            first = fake.presences[-1]
            assert first["status"] == "online" and first["activities"][0]["type"] == 3  # watching
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.presences) >= 2, timeout=15)
            assert fake.presences[-1]["activities"][0]["name"] != first["activities"][0]["name"]

    run(scenario())


def test_bio_and_nickname_only_when_they_change(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "bio", "nickname")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.member_edits) == 2, timeout=15)
            sent = {key: value for edit in fake.member_edits for key, value in edit.items()}
            assert "1 servers up" in sent["bio"] and sent["nick"].startswith("Huragok")
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.member_edits) == 2, timeout=15)

    run(scenario())


def test_channel_names_and_topics_go_as_one_change_and_only_when_changed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "channel_names", "topics")
        channels = {PUBLIC_CHANNEL: {"server": SERVER, "template": "{state_emoji} {players} on"}}
        set_nested_key(path, ("status", "channel_names"), "channels", channels)
        set_nested_key(path, ("status", "topics"), "channels", {PUBLIC_CHANNEL: "{servers_up} of {servers} up"})
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: len(fake.channel_edits) == 1, timeout=15)
            edit = fake.channel_edits[0]
            assert edit["channel"] == PUBLIC_CHANNEL and edit["name"] == "🟢 0 on" and edit["topic"] == "1 of 1 up"
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.channel_edits) == 1, timeout=15)
            # Discord allows 2 in 10 minutes: a change now waits for the channel's own pace, and only the newest goes.
            await rcon.join(1, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            part.status.due.clear()
            await part.status.tick()
            await wait_for(lambda: len(fake.channel_edits) == 1 and part.outbox.waiting == 1, timeout=15)

    run(scenario())


def test_voice_channel_status(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "voice_status")
        set_nested_key(path, ("status", "voice_status"), "channels", {PUBLIC_CHANNEL: {"server": SERVER, "template": "{players} on {server}"}})
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status.tick()
            await wait_for(lambda: fake.voice, timeout=15)
            assert fake.voice == [{"channel": PUBLIC_CHANNEL, "status": f"0 on {SERVER}"}]

    run(scenario())


def test_a_refused_rename_stops_and_is_on_the_dashboard_and_posting_goes_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "topics")
        set_nested_key(path, ("status", "topics"), "channels", {STAFF_CHANNEL: "{servers_up} up"})
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            fake.refuse_next.append((f"/channels/{STAFF_CHANNEL}", 403, {"message": "Missing Permissions", "code": 50013}, {}))
            await part.status.tick()
            await wait_for(lambda: f"rename:{STAFF_CHANNEL}" in part.feeds.blocked, timeout=15)
            await wait_for(lambda: any("refused renaming" in line for line in huragok.problems), timeout=15)
            assert f"channel:{STAFF_CHANNEL}" not in part.feeds.blocked

    run(scenario())


def test_nothing_is_sent_with_mode_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "board", "presence", "bio")
        set_mode(path, "off")
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            await part.status.tick()
            await asyncio.sleep(0.2)
            assert fake.posted == [] and fake.requests == []

    run(scenario())


def test_loads_clean(tmp_path: Path) -> None:
    assert load(tmp_path / "none").section("status", "board")["layout"]
    assert isinstance(Status, type)


@pytest.mark.parametrize(("phases", "dot"), [(["running", "running"], "online"), (["running", "idle"], "online"), (["running", "faulted"], "idle"), (["running", "backoff", "idle"], "idle"),
                                             (["faulted", "offline"], "dnd"), (["faulted", "idle"], "dnd"), (["idle", "idle"], "idle"), ([], "idle"), (["external"], "online")])
def test_the_dot_is_about_what_is_meant_to_be_up(phases: list[str], dot: str) -> None:
    from huragok.discordbot.status import health_dot

    assert health_dot([{"phase": phase} for phase in phases]) == dot

