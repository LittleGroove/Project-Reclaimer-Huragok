"""Reports: a person telling staff about a player. One card for a player while it's open, however many report them."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, MOD, OWNER
from test_bot_running import OTHER, answer_text
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, MOD_USER, REPORTS, STAFF, buttons_of, fill, form_of, lines_in, posts_in, said_back
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, OWNER_USER, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, tells_to, wait_for

from huragok.discordbot.config import add_owner, make_file, set_channel, set_guild, set_message, set_mode, set_switch, set_token

PING_ROLE = ADMIN_ROLE


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, **options: Any) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    for name, channel in (("staff", STAFF), ("reports", REPORTS), ("alerts", ALERTS), ("logs", LOGS), ("huragok-logs", HURAGOK_LOGS)):
        set_channel(path, name, channel, name)
    set_switch(path, ("reports",), "enabled", True)
    set_switch(path, ("reports",), "reporter_cooldown_seconds", 0)
    set_switch(path, ("feed", "moderation"), "enabled", True)
    for key, value in options.items():
        set_switch(path, ("reports",), key, value)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


async def on(huragok: Any, rcon: Any, *people: tuple[int, str, str]) -> None:
    for number, name, player_id in people:
        await rcon.join(number, name, player_id)
    await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) >= len(people), timeout=10)


ALICE_ON, BRAVO_ON, OWNER_ON = (3, "Alice", ALICE), (4, "Bravo", BRAVO), (1, "Owner", OWNER)


def description(post: dict[str, Any]) -> str:
    return str(post["embeds"][0]["description"])


def test_in_game_it_opens_a_card_for_staff_and_thanks_them(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(ALICE, "Alice", "come and fight me")
            await rcon.chat(BRAVO, "Bravo", "!report Alice camping the spawn")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            assert f"**Bravo** (their first report) reported **Alice** on {SERVER}: camping the spawn" in description(post)
            assert "come and fight me" in str(post["embeds"][0]["fields"])
            assert list(buttons_of(post)) == ["Mute", "Kick", "Ban", "Strike", "Warn", "Whois", "Discuss", "Dismiss"]
            await wait_for(lambda: tells_to(rcon, BRAVO), timeout=10)
            assert "the staff have it" in tells_to(rcon, BRAVO)[-1]

    run(scenario())


def test_staff_can_be_reported_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, OWNER_ON)
            await rcon.chat(ALICE, "Alice", "!report Owner abusing their powers")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            assert "reported **Owner**" in description(posts_in(fake, REPORTS)[0])

    run(scenario())


def test_not_yourself(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            await rcon.chat(ALICE, "Alice", "!report Alice")
            await wait_for(lambda: tells_to(rcon, ALICE), timeout=10)
            assert "yourself" in tells_to(rcon, ALICE)[-1] and not posts_in(fake, REPORTS)

    run(scenario())


def test_someone_who_just_left_can_be_reported(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.leave(3)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 1, timeout=10)
            await rcon.chat(BRAVO, "Bravo", "!report Alice was cheating")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)

    run(scenario())


def test_switched_off_it_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, enabled=False)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: tells_to(rcon, BRAVO), timeout=10)
            assert "Unknown command" in tells_to(rcon, BRAVO)[-1] and not fake.posted  # there's no such command while it can't reach anyone

    run(scenario())


def test_a_setting_says_where_reports_come_from(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, **{"from": ["discord"]})
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: tells_to(rcon, BRAVO), timeout=10)
            assert "Unknown command" in tells_to(rcon, BRAVO)[-1]

    run(scenario())


def test_their_message_line_can_be_off_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_message(path, "reports-made", post=False)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: tells_to(rcon, BRAVO), timeout=10)
            assert "can't reach anyone" in tells_to(rcon, BRAVO)[-1] and not posts_in(fake, REPORTS)

    run(scenario())


# One card ----------------------------------------------------------------------------------------------------------------


def test_another_persons_report_is_added_to_the_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON, OWNER_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice camping")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await rcon.chat(OWNER, "Owner", "!report Alice cheating")
            await wait_for(lambda: fake.edits, timeout=15)
            assert len(posts_in(fake, REPORTS)) == 1
            assert f"**Owner** (their first report) reported **Alice** on {SERVER}: cheating" in fake.edits[-1]["embeds"][0]["description"]

    run(scenario())


def test_the_same_person_again_is_added_to_their_report(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice camping")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await rcon.chat(BRAVO, "Bravo", "!report Alice camping!!")
            await wait_for(lambda: any("Added to your report" in said for said in tells_to(rcon, BRAVO)), timeout=10)
            assert len(posts_in(fake, REPORTS)) == 1 and "camping!!" in part.tickets.lines_of(1)[0]["text"]

    run(scenario())


def test_reporting_too_fast_waits(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, reporter_cooldown_seconds=60)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON, OWNER_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await rcon.chat(BRAVO, "Bravo", "!report Owner")
            await wait_for(lambda: any("moment ago" in said for said in tells_to(rcon, BRAVO)), timeout=10)
            assert len(posts_in(fake, REPORTS)) == 1

    run(scenario())


def test_enough_people_ping_staff_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, threshold=2)
        set_message(path, "reports-many", ping=PING_ROLE)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON, OWNER_ON, (2, "Mod", MOD))
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            assert not lines_in(fake, REPORTS)
            await rcon.chat(OWNER, "Owner", "!report Alice")
            await wait_for(lambda: lines_in(fake, REPORTS), timeout=15)
            ping = lines_in(fake, REPORTS)[0]
            assert f"<@&{PING_ROLE}>" in ping["content"] and "2 different people have reported **Alice**" in ping["content"]
            await rcon.chat(MOD, "Mod", "!report Alice")
            await wait_for(lambda: any("the staff have it" in said for said in tells_to(rcon, MOD)), timeout=10)
            assert len(lines_in(fake, REPORTS)) == 1

    run(scenario())


# What staff do with it ---------------------------------------------------------------------------------------------------


def test_dismissing_it_closes_it_to_logs_and_is_for_staff(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            _, token = fake.press(buttons_of(post)["Dismiss"], post, user=OTHER)
            assert "for staff" in await answer_text(fake, token)
            assert posts_in(fake, REPORTS)
            fake.press(buttons_of(post)["Dismiss"], post, user=MOD_USER, roles=(MOD_ROLE,))
            await wait_for(lambda: posts_in(fake, LOGS) and not posts_in(fake, REPORTS), timeout=15)
            assert "dismissed it" in description(posts_in(fake, LOGS)[0])

    run(scenario())


def test_acting_on_it_counts_for_everyone_who_reported(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            await fill(fake, "Mute", post, {"How long": "10m", "Why (they'll read this)": "spam"}, user=MOD_USER, roles=(MOD_ROLE,))
            await wait_for(lambda: rcon.named("mute"), timeout=15)
            await wait_for(lambda: part.reports.track_record(BRAVO) == "1 report, 1 acted on", timeout=15)
            assert "Close" in [row["label"] for form in [part.tickets.components(part.tickets.get(1))] for one in form for row in one["components"]]

    run(scenario())


def test_their_record_shows_on_their_next_report(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON, OWNER_ON)
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await rcon.chat(BRAVO, "Bravo", "!report Owner")
            await wait_for(lambda: len(posts_in(fake, REPORTS)) == 2, timeout=15)
            assert "**Bravo** (1 report, 0 acted on) reported **Owner**" in description(posts_in(fake, REPORTS)[1])

    run(scenario())


def test_dealing_with_them_in_game_goes_on_the_reports_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON, (2, "Mod", MOD))
            await rcon.chat(BRAVO, "Bravo", "!report Alice")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await rcon.chat(MOD, "Mod", "!mute Alice 10m spam")
            await wait_for(lambda: part.reports.track_record(BRAVO) == "1 report, 1 acted on", timeout=15)
            await wait_for(lambda: fake.edits, timeout=15)
            assert "Mod" in fake.edits[-1]["embeds"][0]["description"] and "Alice" in fake.edits[-1]["embeds"][0]["description"]
            assert not posts_in(fake, LOGS)  # one incident, one card

    run(scenario())


# From Discord --------------------------------------------------------------------------------------------------------------


def test_slash_report_for_someone_picked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            _, token = fake.command("report", user=OTHER, player=ALICE, why="camping")
            assert "the staff have it" in await said_back(fake, token)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            assert f"reported **Alice** on {SERVER}: camping" in description(posts_in(fake, REPORTS)[0])
            assert fake.followups[-1].get("flags") == 64  # seen by them alone

    run(scenario())


def test_a_name_typed_in_full_is_found_even_if_theyve_left(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            await rcon.leave(3)
            await wait_for(lambda: not huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("report", user=OTHER, player="alice")
            await said_back(fake, token)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            assert "reported **Alice**" in description(posts_in(fake, REPORTS)[0])

    run(scenario())


def test_part_of_a_name_doesnt_search_who_has_been_here(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            await rcon.leave(3)
            await wait_for(lambda: not huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("report", user=OTHER, player="ali")
            assert "has been seen here" in await answer_text(fake, token)
            assert not posts_in(fake, REPORTS)

    run(scenario())


def test_what_it_offers_is_only_whos_on_now(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await on(huragok, rcon, ALICE_ON, BRAVO_ON)
            await rcon.leave(4)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 1, timeout=10)
            _, token = fake.typing_command("report", "player", "", user=OTHER)
            await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
            offered = next(c for c in fake.callbacks if c.get("token") == token)["data"]["choices"]
            assert [choice["name"] for choice in offered] == ["Alice"]

    run(scenario())


def test_a_clip_with_it_is_kept_and_linked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            _, token = fake.command_with_file("report", "proof", "clip.txt", "the clip itself", user=OTHER, player=ALICE)
            await said_back(fake, token)
            await wait_for(lambda: [post for post in fake.posted if post.get("files") == ["the clip itself"]], timeout=15)
            kept = next(post for post in fake.posted if post.get("files"))
            assert kept["channel"] == LOGS  # where the card will end up, so it isn't left behind when the card goes
            await wait_for(lambda: part.tickets.lines_of(1), timeout=10)
            assert f"[clip.txt](https://discord.com/channels/{GUILD}/{LOGS}/{kept['id']})" in part.tickets.lines_of(1)[0]["text"]

    run(scenario())


def test_a_right_click_reports_a_message(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            target = fake.message_someone_wrote(STAFF, "come and fight me")
            _, token = fake.right_click("Report message", target, user=OTHER)
            await form_of(fake, token)
            _, sent = fake.fill_in(token, {"channel": STAFF, "id": target["id"]}, {"Why": "threats"}, user=OTHER)
            assert "the staff have it" in await answer_text(fake, sent)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            assert "reported a message by" in description(post) and "threats" in description(post) and "come and fight me" in str(post["embeds"][0]["fields"])
            assert list(buttons_of(post)) == ["Discuss", "Dismiss"]  # no player here to act on

    run(scenario())


def test_the_commands_are_there_only_when_theyre_wanted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, enabled=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            names = {command["name"] for command in fake.commands[GUILD]}
            assert not {"report", "Report message"} & names
            set_switch(path, ("reports",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: {"report", "Report message"} <= {command["name"] for command in fake.commands[GUILD]}, timeout=15)
            set_switch(path, ("reports",), "right_click", False)
            await part.reload(force=True)
            await wait_for(lambda: "Report message" not in {command["name"] for command in fake.commands[GUILD]}, timeout=15)
            assert "report" in {command["name"] for command in fake.commands[GUILD]}

    run(scenario())


def test_slash_commands_dont_offer_the_in_game_one_as_well(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, **{"from": ["game"]})
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            await wait_for(lambda: "mute" in {command["name"] for command in fake.commands.get(GUILD, [])}, timeout=15)
            assert "report" not in {command["name"] for command in fake.commands[GUILD]}

    run(scenario())


def test_a_list_of_buttons_with_a_name_it_doesnt_know_says_so(tmp_path: Path) -> None:
    """The first beta's lists held names like mute-30m: no Mute on a report, and nothing saying why."""
    from huragok.discordbot.config import load

    path = bot_file(tmp_path, buttons=["mute-30m", "kick"])
    assert any("mute-30m isn't one of" in warning for warning in load(path).warnings)
    assert not load(bot_file(tmp_path, buttons=["mute", "kick"])).warnings


def test_a_file_thats_not_a_picture_or_a_clip_is_only_pointed_at(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """It isn't read into memory or posted under the bot's name: anyone can run /report."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            _, token = fake.command_with_file("report", "proof", "installer.exe", "not a clip", user=OTHER, player=ALICE)
            await said_back(fake, token)
            await wait_for(lambda: part.tickets.lines_of(1), timeout=10)
            assert "installer.exe" in part.tickets.lines_of(1)[0]["text"]
            assert not [post for post in fake.posted if post.get("files")]

    run(scenario())

