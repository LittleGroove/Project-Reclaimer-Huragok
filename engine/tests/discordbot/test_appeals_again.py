"""After an appeal's decided: whether they can ask again, and what someone's told when they can't. It's whoever appealed who waits, for that player: another account can still ask, unless it came with the code, which only the player has. And appeals' own channel."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO
from test_appeals import appeal, banned, bot_file, up
from test_bot_running import OTHER, answer_text
from test_tickets import MOD_USER, REPORTS, buttons_of, fill, form_of, posts_in
from testkit.fake_discord import ADMIN_ROLE, GUILD, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.config import set_channel

AGAIN = "Can they appeal this again?"
WHY_TOLD = "Why (they'll read this)"
ADMIN = {"user": MOD_USER, "roles": (ADMIN_ROLE,)}
STRANGER = "5555"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def dms(fake: FakeDiscord, user: str = OTHER) -> list[str]:
    return [str(post.get("content")) for post in fake.posted if post["channel"] == f"dm{user}"]


def card_of(part: Any, number: int) -> dict[str, Any]:
    ticket = part.tickets.get(number)
    return {**part.tickets.edit_form(ticket), "id": ticket["message"], "channel": ticket["channel"]}


async def appealed(huragok: Any, rcon: Any, fake: FakeDiscord) -> tuple[Any, dict[str, Any]]:
    part = await up(huragok)
    await banned(huragok, rcon, fake)
    await appeal(fake)
    post = next(post for post in posts_in(fake, REPORTS) if "Lift it" in buttons_of(post))
    return part, post


async def denied(part: Any, fake: FakeDiscord, post: dict[str, Any], **answers: str) -> int:
    number = int(part.ctx.db.query("SELECT ticket FROM discord_appeals ORDER BY number DESC LIMIT 1")[0]["ticket"])
    await fill(fake, "Deny", post, {WHY_TOLD: "not the first time", **answers}, **ADMIN)
    await wait_for(lambda: part.tickets.get(number)["state"] not in ("open", "active"), timeout=10)
    await wait_for(lambda: dms(fake), timeout=10)
    return number


def test_no_more_appeals_is_for_whoever_appealed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host's: that account can't appeal that player again, another could. A name is anyone's to type: a stranger appealing someone's ban mustn't lock them out."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            number = await denied(part, fake, post, **{AGAIN: "never"})
            assert "no more appeals on this" in part.tickets.lines_of(number)[-1]["text"]
            assert "any more appeals" in dms(fake)[-1] and "not the first time" in dms(fake)[-1]
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            assert "aren't taking any more" in await answer_text(fake, token)
            _, token = fake.command("appeal", user=STRANGER, who="Alice")
            await form_of(fake, token)  # another account: the form, to say what they've got to say

    run(scenario())


def test_with_the_code_its_the_player_who_waits(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Only the player has their code: a second account with it is them."""
    async def scenario() -> None:
        bot_file(tmp_path, in_reason="Appeal: /appeal {code}")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            code = part.appeals.code_of(ALICE)
            await appeal(fake, code)
            post = next(post for post in posts_in(fake, REPORTS) if "Lift it" in buttons_of(post))
            await denied(part, fake, post, **{AGAIN: "never"})
            _, token = fake.command("appeal", user=STRANGER, who=code)
            assert "aren't taking any more" in await answer_text(fake, token)
            assert part.appeals.no_more(STRANGER, ALICE, "ban") and not part.appeals.no_more("7777", BRAVO, "ban")

    run(scenario())


def test_everyone_on_a_card_waits_as_their_own(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Someone with only a name, joined by someone with the code: the decision's on both."""
    async def scenario() -> None:
        bot_file(tmp_path, in_reason="Appeal: /appeal {code}")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await banned(huragok, rcon, fake)
            await appeal(fake, "Alice", user=STRANGER)
            await appeal(fake, part.appeals.code_of(ALICE))
            post = next(post for post in posts_in(fake, REPORTS) if "Lift it" in buttons_of(post))
            await denied(part, fake, post, **{AGAIN: "30"})
            assert part.appeals.next_allowed(STRANGER, ALICE, "ban") > time.time() + 29 * 86400
            assert part.appeals.next_allowed("7777", ALICE, "ban") > time.time() + 29 * 86400  # it came with the code: the player's

    run(scenario())


def test_a_wait_someone_picked_is_the_wait_for_anyone_asking(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            number = await denied(part, fake, post, **{AGAIN: "30"})
            allowed = part.appeals.next_allowed(OTHER, ALICE, "ban")
            assert time.time() + 29 * 86400 < allowed < time.time() + 31 * 86400
            assert not part.appeals.next_allowed(STRANGER, ALICE, "ban")  # only whoever appealed waits, with only a name
            stamp = f"<t:{int(allowed)}:R>"
            assert "they can appeal again" in part.tickets.lines_of(number)[-1]["text"] and stamp in part.tickets.lines_of(number)[-1]["text"]
            assert stamp in dms(fake)[-1]
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            assert stamp in await answer_text(fake, token)

    run(scenario())


def test_leaving_the_form_as_it_comes_is_the_usual_wait(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Deny"], post, **ADMIN)
            form = await form_of(fake, token)
            menu = next(label["component"] for label in form["components"] if label["label"] == AGAIN)
            usual = [option for option in menu["options"] if option.get("default")]
            assert len(usual) == 1 and "the usual 7 days" in usual[0]["label"]
            values = {option["value"] for option in menu["options"]}
            assert {"30", "never"} <= values and "7" not in values  # the usual isn't there twice
            number = int(part.ctx.db.query("SELECT ticket FROM discord_appeals")[0]["ticket"])
            fake.fill_in(token, post, {WHY_TOLD: "no"}, **ADMIN)
            await wait_for(lambda: part.tickets.get(number)["state"] not in ("open", "active"), timeout=10)
            assert part.appeals.of_ticket(number)["again"] is None
            assert part.appeals.next_allowed(OTHER, ALICE, "ban") > time.time() + 6 * 86400
            assert not part.appeals.next_allowed(STRANGER, ALICE, "ban")  # only a name, under the usual wait: a stranger's appeal doesn't lock them out

    run(scenario())


def test_something_new_against_them_can_still_be_appealed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            await denied(part, fake, post, **{AGAIN: "never"})
            assert part.appeals.no_more(OTHER, ALICE, "ban")
            huragok.vergil.moderation.record_seen("ban", SERVER, ALICE, "Alice", by="Owner", source="in game", reason="again")
            assert not part.appeals.no_more(OTHER, ALICE, "ban") and not part.appeals.next_allowed(OTHER, ALICE, "ban")

    run(scenario())


def test_let_them_appeal_again_undoes_it_and_tells_them(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            number = await denied(part, fake, post, **{AGAIN: "never"})
            told = len(dms(fake))
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            card = card_of(part, number)
            fake.press(buttons_of(card)["Let them appeal again"], card, **ADMIN)
            await wait_for(lambda: not part.appeals.no_more(OTHER, ALICE, "ban"), timeout=10)
            assert not part.appeals.next_allowed(OTHER, ALICE, "ban")
            await wait_for(lambda: len(dms(fake)) > told, timeout=10)
            assert "You can appeal again" in dms(fake)[-1]
            assert "Let them appeal again" not in str(part.tickets.edit_form(part.tickets.get(number)))

    run(scenario())


def test_a_lifted_appeal_has_nothing_to_let_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            number = int(part.ctx.db.query("SELECT ticket FROM discord_appeals")[0]["ticket"])
            fake.press(buttons_of(post)["Lift it"], post, **ADMIN)
            await wait_for(lambda: part.tickets.get(number)["state"] not in ("open", "active"), timeout=15)
            assert "Let them appeal again" not in str(part.tickets.edit_form(part.tickets.get(number)))

    run(scenario())


def test_already_says_when_and_shows_staff_the_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """It read as a fault to someone with a card up from days ago, as it said nothing about which."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await appealed(huragok, rcon, fake)
            _, token = fake.command("appeal", user=STRANGER, who="Alice")
            said = await answer_text(fake, token)
            assert "already an appeal" in said and "<t:" in said and "discord.com/channels" not in said
            _, token = fake.command("appeal", who="Alice", **ADMIN)
            assert "discord.com/channels" in await answer_text(fake, token)

    run(scenario())


def test_a_player_id_will_do_for_someone_whos_forgotten_their_old_name(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await banned(huragok, rcon, fake)
            _, token = fake.command("appeal", user=OTHER, who=ALICE)
            await form_of(fake, token)  # the form, not "nothing stands against them"
            _, token = fake.command("appeal", user=OTHER, who="f" * 64)
            assert "Nothing that can be appealed" in await answer_text(fake, token)

    run(scenario())


def test_appeals_go_to_their_own_channel_once_its_set_and_reports_until_then(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part, post = await appealed(huragok, rcon, fake)
            assert post["channel"] == REPORTS
            fake.channels.append(fake._channel("7788", "spr-appeals", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]))
            set_channel(path, "appeals", "7788", "spr-appeals")
            await part.look_now()
            await part.reload(force=True)
            part.appeals.file("9999", "Bravo", BRAVO, "Bravo", "ban", [("What happened?", "x")])
            await wait_for(lambda: posts_in(fake, "7788"), timeout=10)
            assert "Lift it" in buttons_of(posts_in(fake, "7788")[0])  # a ticket, with its buttons, whatever it's called

    run(scenario())
