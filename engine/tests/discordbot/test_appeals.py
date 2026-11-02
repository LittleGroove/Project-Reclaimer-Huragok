"""Appeals: someone asking staff to lift what's against them, and being told how it went."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, OWNER
from test_bot_running import OTHER, answer_text
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, MOD_USER, REPORTS, STAFF, buttons_of, fill, form_of, posts_in
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, OWNER_USER, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.config import add_owner, make_file, set_channel, set_guild, set_message, set_mode, set_switch, set_token

WHAT, WHY = "What happened?", "Why should it be lifted?"


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
    set_switch(path, ("appeals",), "enabled", True)
    set_switch(path, ("feed", "moderation"), "enabled", True)
    for key, value in options.items():
        set_switch(path, ("appeals",), key, value)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


async def banned(huragok: Any, rcon: Any, fake: FakeDiscord, *, how: str = "!ban Alice perm slurs in voice", kind: str = "ban") -> None:
    """Alice on, and dealt with by a moderator in game."""
    await rcon.join(3, "Alice", ALICE)
    await rcon.join(1, "Owner", OWNER)
    await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
    await rcon.chat(ALICE, "Alice", "get out of my spawn")
    await rcon.chat(OWNER, "Owner", how)
    await wait_for(lambda: rcon.named(kind), timeout=15)
    await wait_for(lambda: posts_in(fake, LOGS), timeout=15)


async def appeal(fake: FakeDiscord, who: str = "Alice", *, user: str = OTHER) -> dict[str, Any]:
    """Someone's appeal, through to its card."""
    _, token = fake.command("appeal", user=user, who=who)
    await form_of(fake, token)
    _, sent = fake.fill_in(token, {"channel": STAFF, "id": "1"}, {WHAT: "I got carried away", WHY: "It won't happen again"}, user=user)
    assert "the staff have it" in await answer_text(fake, sent)
    await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
    return posts_in(fake, REPORTS)[0]


def dms(fake: FakeDiscord, user: str = OTHER) -> list[str]:
    return [str(post.get("content")) for post in fake.posted if post["channel"] == f"dm{user}"]


def test_its_a_card_with_the_case_and_their_answers(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            text = str(post["embeds"][0])
            assert "appeals the ban on **Alice**" in text and "I got carried away" in text and "It won't happen again" in text
            assert "by Owner" in text and "slurs in voice" in text and "get out of my spawn" in text
            assert list(buttons_of(post)) == ["Lift it", "Shorten it", "Deny", "Whois", "Discuss", "Close"]

    run(scenario())


def test_lifting_it_unbans_tells_them_and_closes_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            fake.press(buttons_of(post)["Lift it"], post, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: rcon.named("unban"), timeout=15)
            await wait_for(lambda: dms(fake), timeout=15)
            assert "is lifted" in dms(fake)[0]
            await wait_for(lambda: not posts_in(fake, REPORTS) and any("accepted the appeal" in post["embeds"][0]["description"] for post in posts_in(fake, LOGS)), timeout=15)

    run(scenario())


def test_shortening_a_ban_makes_it_a_kick_with_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            await fill(fake, "Shorten it", post, {"How long from now": "7 days", "Why (they'll read this)": "first time"}, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: rcon.named("unban") and dms(fake), timeout=15)
            assert "7 days from now" in dms(fake)[0] and "first time" in dms(fake)[0]
            assert any(str(call.get("args", [None, None])[1:2]) == "['7d']" or "7d" in str(call) for call in rcon.named("ban")[1:])
            assert {key: value for key, value in part.tickets.standing(ALICE).items() if key != "on"} == {"muted": False, "kicked": True, "banned": False}

    run(scenario())


def test_shortening_needs_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            sent = await fill(fake, "Shorten it", post, {"How long from now": "perm"}, user=MOD_USER, roles=(ADMIN_ROLE,))
            assert "Nothing was done" in await answer_text(fake, sent)
            assert rcon.named("unban") == [] and posts_in(fake, REPORTS)

    run(scenario())


def test_denying_it_says_why_and_lifts_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            await fill(fake, "Deny", post, {"Why (they'll read this)": "not the first time"}, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: dms(fake), timeout=15)
            assert "denied: not the first time" in dms(fake)[0] and rcon.named("unban") == []
            assert part.tickets.standing(ALICE)["banned"]
            await wait_for(lambda: not posts_in(fake, REPORTS), timeout=15)

    run(scenario())


def test_closing_it_tells_nobody_and_it_cant_be_decided_after(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            number = int(buttons_of(post)["Close"].split(":")[2])
            fake.press(buttons_of(post)["Close"], post, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: part.appeals.of_ticket(number)["outcome"] == "closed", timeout=15)
            await wait_for(lambda: not posts_in(fake, REPORTS), timeout=15)
            await wait_for(lambda: dms(fake), timeout=10)  # they were told they'd hear
            assert "closed by the staff" in dms(fake)[0] and part.tickets.standing(ALICE)["banned"]
            part.appeals.decide(number, "lifted", "Someone")
            assert part.appeals.of_ticket(number)["outcome"] == "closed" and len(dms(fake)) == 1

    run(scenario())


def test_denying_needs_a_reason(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            _, token = fake.press(buttons_of(post)["Deny"], post, user=MOD_USER, roles=(ADMIN_ROLE,))
            form = await form_of(fake, token)
            assert form["components"][0]["component"]["required"] is True

    run(scenario())


def test_only_staff_can_decide(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            for label in ("Lift it", "Shorten it", "Deny", "Close"):
                _, token = fake.press(buttons_of(post)[label], post, user=OTHER)
                assert "for staff" in await answer_text(fake, token)
            assert rcon.named("unban") == [] and not dms(fake)

    run(scenario())


def test_lifting_it_in_game_decides_it_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            await appeal(fake)
            await rcon.chat(OWNER, "Owner", f"!unban {ALICE}")
            await wait_for(lambda: dms(fake), timeout=15)
            assert "is lifted" in dms(fake)[0]
            await wait_for(lambda: not posts_in(fake, REPORTS), timeout=15)

    run(scenario())


def test_if_they_cant_be_told_the_card_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        fake.dms_closed.add(OTHER)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            number = int(buttons_of(post)["Lift it"].split(":")[2])
            fake.press(buttons_of(post)["Lift it"], post, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: any("couldn't be told" in row["text"] for row in part.tickets.lines_of(number)), timeout=15)

    run(scenario())


def test_telling_them_can_be_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, tell_outcome="no")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            fake.press(buttons_of(post)["Lift it"], post, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: not posts_in(fake, REPORTS), timeout=15)
            assert not dms(fake)

    run(scenario())


# Who can appeal what -------------------------------------------------------------------------------------------------------


def test_nothing_against_them_nothing_to_appeal(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            assert "can be appealed" in await answer_text(fake, token)

    run(scenario())


def test_only_what_the_setting_says_can_be_appealed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake, how="!mute Alice 1d spam", kind="mute")
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            assert "can't be appealed" in await answer_text(fake, token)
            set_switch(path, ("appeals",), "for", ["ban", "mute"])
            await part.reload(force=True)
            post = await appeal(fake)
            assert "appeals the mute on **Alice**" in post["embeds"][0]["description"]
            fake.press(buttons_of(post)["Lift it"], post, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: rcon.named("unmute"), timeout=15)

    run(scenario())


def test_the_code_from_their_ban_finds_them_without_a_name(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake, part.appeals.code_of(ALICE).lower())
            assert "appeals the ban on **Alice**" in post["embeds"][0]["description"]

    run(scenario())


def test_a_setting_puts_the_code_in_the_bans_message(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, in_reason="Appeal: /appeal {code}")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            assert f"Appeal: /appeal {part.appeals.code_of(ALICE)}" in str(rcon.named("ban")[-1])
            assert "Appeal" not in str(huragok.storage.query("SELECT reason FROM actions WHERE kind = 'ban'")[0]["reason"])  # staff's record has the reason alone

    run(scenario())


def test_only_one_at_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            await appeal(fake)
            _, token = fake.command("appeal", user="9911", who="Alice")
            assert "already an appeal" in await answer_text(fake, token)

    run(scenario())


def test_after_a_decision_they_wait_unless_something_new_is_against_them(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            post = await appeal(fake)
            await fill(fake, "Deny", post, {"Why (they'll read this)": "no"}, user=MOD_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: not posts_in(fake, REPORTS), timeout=15)
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            assert "again <t:" in await answer_text(fake, token)
            assert part.appeals.next_allowed(OTHER, ALICE)
            huragok.storage.db.execute("UPDATE discord_appeals SET time = time - 1000")
            huragok.storage.record_action(kind="ban", server=SERVER, target_id=ALICE, target_name="Alice", by="Owner", source="commands", reason="again", duration=None, evidence=None, dry_run=False, ok=True, reply="")
            assert part.appeals.next_allowed(OTHER, ALICE) == 0.0

    run(scenario())


def test_their_message_line_can_be_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_message(path, "appeals-made", post=False)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            await form_of(fake, token)
            _, sent = fake.fill_in(token, {"channel": STAFF, "id": "1"}, {WHAT: "a", WHY: "b"}, user=OTHER)
            assert "can't reach the staff" in await answer_text(fake, sent)

    run(scenario())


def test_the_command_is_only_there_when_wanted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, enabled=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            assert "appeal" not in {command["name"] for command in fake.commands[GUILD]}
            set_switch(path, ("appeals",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: "appeal" in {command["name"] for command in fake.commands[GUILD]}, timeout=15)

    run(scenario())
