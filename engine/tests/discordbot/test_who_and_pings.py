"""The who: a list offers only who a command can do anything to, saying how they stand; a whole player ID always works; what's posted names them, never by ID. And pings: as many roles as a line calls for."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO
from test_bot_running import OTHER, _components, answer_text
from test_contact import home, labels, picked
from test_slash import bot_file, up
from test_tickets import LOGS, REPORTS, posts_in
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import features
from huragok.discordbot.config import load, pings_of, set_channel, set_message, set_switch
from huragok.discordbot.facts import gather

CHARLIE = "f" * 64


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


async def choices(fake: FakeDiscord, command: str, typed: str = "", option: str = "player", **who: Any) -> list[dict[str, Any]]:
    _, token = fake.typing_command(command, option, typed, **who)
    await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
    return next(c for c in fake.callbacks if c.get("token") == token)["data"]["choices"]


async def names_offered(fake: FakeDiscord, command: str, typed: str = "") -> list[str]:
    return [c["name"].partition(" · ")[0] for c in await choices(fake, command, typed)]


async def seen(huragok: Any, rcon: Any) -> None:
    """Alice and Bravo have been here and gone, and Charlie's on."""
    for number, (name, player_id) in enumerate((("Alice", ALICE), ("Bravo", BRAVO)), 2):
        await rcon.join(number, name, player_id)
        await wait_for(lambda: huragok.storage.player(player_id) is not None, timeout=10)  # noqa: B023
        await rcon.leave(number)
    await rcon.join(4, "Charlie", CHARLIE)
    await wait_for(lambda: huragok.storage.player(CHARLIE) is not None and len(huragok.superintendent.states[SERVER].players) == 1, timeout=10)


async def ran(fake: FakeDiscord, command: str, **options: Any) -> str:
    """What it says, having said Yes if it asks first."""
    opened, token = fake.command(command, **options)
    await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=10)
    asked = next(c for c in fake.callbacks if c["token"] == token).get("data") or {}
    yes = next((item["custom_id"] for row in asked.get("components") or [] for item in row["components"] if item.get("label") == "Yes"), "")
    if yes:
        _, token = fake.pick(opened, yes, 2, [])
    return await answer_text(fake, token)


# What a list offers ------------------------------------------------------------------------------------------------------


def test_a_lift_only_offers_who_it_could_lift_and_says_how_they_stand(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            assert await names_offered(fake, "unban") == []
            assert await names_offered(fake, "letback") == []  # /pardon's name in Discord
            assert await names_offered(fake, "unmute") == []
            await ran(fake, "ban", player=ALICE, reason="cheating")
            await wait_for(lambda: huragok.runners["discord"].part.tickets.standing(ALICE)["banned"], timeout=10)
            await ran(fake, "mute", player=CHARLIE, time="1h")
            await wait_for(lambda: huragok.runners["discord"].part.tickets.standing(CHARLIE)["muted"], timeout=10)
            assert await names_offered(fake, "unban") == ["Alice"]
            assert await names_offered(fake, "letback") == ["Alice"]
            assert await names_offered(fake, "unmute") == ["Charlie"]
            assert "banned" in (await choices(fake, "unban"))[0]["name"]
            assert "muted" in (await choices(fake, "unmute"))[0]["name"]
            assert await names_offered(fake, "unban", "lic") == ["Alice"] and await names_offered(fake, "unban", "bra") == []

    run(scenario())


def test_acting_on_someone_leaves_out_who_it_already_stands_for(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            assert set(await names_offered(fake, "ban")) == {"Alice", "Bravo", "Charlie"}
            assert (await names_offered(fake, "ban"))[0] == "Charlie"  # who's on, first
            await ran(fake, "ban", player=ALICE, reason="x")
            await wait_for(lambda: huragok.runners["discord"].part.tickets.standing(ALICE)["banned"], timeout=10)
            assert set(await names_offered(fake, "ban")) == {"Bravo", "Charlie"}
            await ran(fake, "mute", player=CHARLIE, time="1h")
            await wait_for(lambda: huragok.runners["discord"].part.tickets.standing(CHARLIE)["muted"], timeout=10)
            assert set(await names_offered(fake, "mute")) == {"Alice", "Bravo"}  # Alice is banned: a mute's still there to give
            kicks = await choices(fake, "kick")
            assert kicks[0]["name"].startswith("Charlie") and "muted" in kicks[0]["name"] and SERVER in kicks[0]["name"]  # still in the list, and it says so

    run(scenario())


def test_what_only_works_in_a_game_only_offers_whos_in_one(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            assert await names_offered(fake, "warn") == ["Charlie"]

    run(scenario())


def test_votes_and_ranks_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            assert await names_offered(fake, "allowvote") == []
            await ran(fake, "novote", player=BRAVO, time="1h")
            await wait_for(lambda: huragok.storage.db.execute("SELECT COUNT(*) FROM commands_no_votes").fetchone()[0], timeout=10)
            assert await names_offered(fake, "allowvote") == ["Bravo"]
            assert "Bravo" not in await names_offered(fake, "novote")
            assert await names_offered(fake, "demote") == [] or all(name not in ("Alice", "Bravo", "Charlie") for name in await names_offered(fake, "demote"))
            await ran(fake, "promote", player=ALICE)
            await wait_for(lambda: ALICE in huragok.settings.permissions.members.get("member", set()), timeout=10)
            assert "Alice" in await names_offered(fake, "demote")

    run(scenario())


def test_two_of_a_name_are_told_apart_and_one_isnt_shown_an_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            assert ALICE[:8] not in (await choices(fake, "whois", "Alice"))[0]["name"]
            await rcon.join(5, "Alice", BRAVO)
            await wait_for(lambda: huragok.storage.player(BRAVO).last_name == "Alice", timeout=10)
            await rcon.leave(5)
            offered = await choices(fake, "whois", "Alice")
            assert len(offered) == 2 and {ALICE[:8], BRAVO[:8]} == {c["name"].rpartition(" · ")[2] for c in offered}

    run(scenario())


def test_someone_found_by_an_old_name_reads_under_the_one_they_use_now(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(2, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            await rcon.leave(2)
            await rcon.join(2, "Alicia", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE).last_name == "Alicia", timeout=10)
            await rcon.leave(2)
            offered = (await choices(fake, "whois", "Alice"))[0]
            assert offered["name"].startswith("Alicia (was Alice)") and offered["value"] == ALICE

    run(scenario())


# A whole player ID ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("command,options", [("whois", {}), ("said", {}), ("ban", {"reason": "x"}), ("mute", {"time": "1h"}), ("novote", {"time": "1h"}), ("kick", {"time": "1h"})])
def test_a_whole_id_does_for_someone_whos_left(tmp_path: Path, run: Any, fake: FakeDiscord, command: str, options: dict[str, str]) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            said = await ran(fake, command, player=ALICE, **options)
            if command != "said":
                assert "Alice" in said, said
            assert ALICE not in said and "isn't" not in said.partition("Alice")[0], said

    run(scenario())


def test_a_whole_id_lifts_and_is_answered_for_in_the_list(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok, fake)
            await seen(huragok, rcon)
            offered = (await choices(fake, "unban", ALICE))[0]  # not banned, but it's who they mean: the list says who that is
            assert offered["name"].startswith("Alice") and "banned" not in offered["name"]
            await ran(fake, "ban", player=ALICE, reason="x")
            await wait_for(lambda: part.tickets.standing(ALICE)["banned"], timeout=10)
            offered = (await choices(fake, "unban", ALICE))[0]
            assert offered["name"].startswith("Alice") and "banned" in offered["name"]
            await ran(fake, "unban", player=ALICE)
            await wait_for(lambda: not part.tickets.standing(ALICE)["banned"], timeout=10)
            nobody = (await choices(fake, "whois", "9" * 64))[0]
            assert nobody["name"] == "That player ID"

    run(scenario())


# Names on what's posted --------------------------------------------------------------------------------------------------


def test_a_card_uses_the_name_they_were_last_seen_under(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A moderator with only an ID, for someone who's since changed their name: nobody reading the card knows either the ID or the old name."""
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "logs", LOGS, "logs")
        fake.channels.append(fake._channel(LOGS, "logs", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]))
        set_switch(path, ("feed", "moderation"), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(2, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            await rcon.leave(2)
            await rcon.join(2, "Alicia", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE).last_name == "Alicia", timeout=10)
            await rcon.leave(2)
            await ran(fake, "ban", player=ALICE, reason="cheating")
            await wait_for(lambda: posts_in(fake, LOGS), timeout=15)
            shown = str(posts_in(fake, LOGS)[0]["embeds"])
            assert "Alicia" in shown and ALICE[:8] not in shown

    run(scenario())


# Pings -------------------------------------------------------------------------------------------------------------------


def test_a_line_can_ping_several_roles(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "reports", REPORTS, "reports")
        fake.channels.append(fake._channel(REPORTS, "reports", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]))
        set_switch(path, ("feed", "calls"), "enabled", True)
        set_message(path, "calls-called", ping=[MOD_ROLE, ADMIN_ROLE])
        assert not load(path).warnings
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 1, timeout=10)
            await rcon.chat(ALICE, "Alice", "!calladmin someone's cheating")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            assert f"<@&{MOD_ROLE}>" in post["content"] and f"<@&{ADMIN_ROLE}>" in post["content"]
            assert set(post["allowed_mentions"]["roles"]) == {MOD_ROLE, ADMIN_ROLE}
            assert not post.get("flags")  # a ping's meant to be heard

    run(scenario())


def test_one_role_is_still_written_plainly() -> None:
    from huragok.discordbot.config import ping_value

    assert ping_value(["1"]) == "1" and ping_value(["1", "2"]) == ["1", "2"] and ping_value([]) == ""
    assert pings_of({"ping": ["1", "2", "1", ""]}) == ["1", "2"] and pings_of({"ping": "@On Call"}) == ["@On Call"] and pings_of({"ping": ""}) == [] and pings_of({}) == []


def test_a_deleted_role_among_several_gets_a_finding(tmp_path: Path, fake: FakeDiscord) -> None:
    path = bot_file(tmp_path)
    set_message(path, "calls-called", ping=[MOD_ROLE, "9999"])
    config = load(path)
    said = [finding.text for finding in features._pings(config, gather(config.token, config.guild)) if finding.where == "Message calls-called"]
    assert said and "any more" in said[0]


def test_nobody_outside_staff_sees_the_list(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await seen(huragok, rcon)
            assert await choices(fake, "ban", user=OTHER) == []

    run(scenario())


# The setup's menus ---------------------------------------------------------------------------------------------------------


def _roles(*ids: str) -> dict[str, Any]:
    return {"roles": {one: {"id": one, "name": f"role{one}", "permissions": "0", "position": 1, "color": 0, "hoist": False, "managed": False, "mentionable": True} for one in ids}}


async def told_panel(fake: FakeDiscord) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """The setup's Who gets told, and its first menu."""
    opened_at, form = await home(fake)
    screen = await picked(fake, opened_at, labels(form)["Who gets told"])
    menu = next(item for item in _components(screen) if item.get("type") == 6)
    return opened_at, screen, menu


def test_who_gets_told_takes_several_roles_and_says_them(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, fake)
            opened_at, _, menu = await told_panel(fake)
            assert menu["min_values"] == 0 and menu["max_values"] == 25
            before = len(fake.callbacks)
            fake.pick(opened_at, menu["custom_id"], 6, [MOD_ROLE, ADMIN_ROLE], resolved=_roles(MOD_ROLE, ADMIN_ROLE))
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            for key in ("alerts-crashed", "alerts-faulted", "alerts-hung", "alerts-start-failed"):
                assert load(path).section("messages")[key]["ping"] == [MOD_ROLE, ADMIN_ROLE]
            assert not load(path).warnings
            shown = str(fake.callbacks[-1]["data"]["embeds"])
            assert f"<@&{MOD_ROLE}>" in shown and f"<@&{ADMIN_ROLE}>" in shown
            again = next(item for item in _components(fake.callbacks[-1]["data"]) if item.get("type") == 6)
            assert {str(default["id"]) for default in again["default_values"]} == {MOD_ROLE, ADMIN_ROLE}  # it opens as it was left
            before = len(fake.callbacks)
            fake.pick(opened_at, again["custom_id"], 6, [ADMIN_ROLE], resolved=_roles(ADMIN_ROLE))
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert load(path).section("messages")["alerts-crashed"]["ping"] == ADMIN_ROLE
            again = next(item for item in _components(fake.callbacks[-1]["data"]) if item.get("type") == 6)
            before = len(fake.callbacks)
            fake.pick(opened_at, again["custom_id"], 6, [], resolved=None)
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert load(path).section("messages")["alerts-crashed"]["ping"] == ""
            assert part.file.warnings == []

    run(scenario())


def test_channels_in_the_setup_offer_every_one_there_is(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host's: everything can be pointed at a channel of any name. Nothing's a name it has to have."""
    from huragok.discordbot.config import defaults
    from huragok.discordbot.panels import CHANNEL_ORDER

    assert set(defaults()["channels"]) == set(CHANNEL_ORDER)
    assert "appeals" in CHANNEL_ORDER and "games" in CHANNEL_ORDER

    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, fake)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)["Channels"])
            offered: list[str] = []
            for _ in range(5):
                offered += [item["placeholder"] for item in _components(screen) if item.get("type") == 8]
                if "Next" not in labels(screen):
                    break
                screen = await picked(fake, opened_at, labels(screen)["Next"])
            assert set(offered) >= set(CHANNEL_ORDER), offered

    run(scenario())
