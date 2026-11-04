"""Tickets: one incident, one card. It stays usable wherever it is, what's done on it updates it, and where it sits and its colour say how it stands."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, MOD
from test_bot_running import OTHER, _components, _opened, answer_text
from test_feeds import ON, feeds_for, kick
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, FACULTY_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.buttons import HOW_LONG, how_long
from huragok.core.tomledit import read_toml
from huragok.discordbot.config import add_faculty, colour_value, load, make_file, set_channel, set_guild, set_message, set_mode, set_switch, set_token

PROBATION_ROLE = "5103"
MOD_USER = "3334"
STAFF, REPORTS, ALERTS, LOGS, HURAGOK_LOGS = STAFF_CHANNEL, "4201", "4202", "4203", "4204"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.roles.append(FakeDiscord._role(PROBATION_ROLE, "SPR-Probation", 1, "0"))
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *, every_channel: bool = True, notes: bool = True, change: tuple[str, str] | None = None) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_faculty(path, FACULTY_USER, "the host")
    set_channel(path, "staff", STAFF, "tickets")
    set_channel(path, "logs", LOGS, "logs")
    if every_channel:
        for name, channel in (("reports", REPORTS), ("alerts", ALERTS), ("huragok-logs", HURAGOK_LOGS)):
            set_channel(path, name, channel, name)
    set_switch(path, ("notes",), "enabled", notes)
    for feed in ("moderation", "vergil", "watch", "calls", "alerts", "votes", "servers"):
        set_switch(path, ("feed", feed), "enabled", True)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    text = text.replace("probationary_moderator = []", f'probationary_moderator = ["{PROBATION_ROLE}"]')
    if change:
        assert change[0] in text
        text = text.replace(*change)
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


def buttons_of(form: dict[str, Any]) -> dict[str, str]:
    """What can be pressed: a greyed one only holds its place."""
    return {item["label"]: item["custom_id"] for row in form.get("components", []) for item in row["components"] if item["type"] == 2 and not item.get("disabled")}


def posts_in(fake: FakeDiscord, channel: str) -> list[dict[str, Any]]:
    """The cards in a channel; the one-liners there are lines_in's."""
    return [post for post in fake.posted if post["channel"] == channel and "embeds" in post]


def lines_in(fake: FakeDiscord, channel: str) -> list[dict[str, Any]]:
    return [post for post in fake.posted if post["channel"] == channel and "embeds" not in post]


def colour(path: Path, name: str) -> int:
    return colour_value(load(path), name)


async def strike(huragok: Any, rcon: Any, fake: FakeDiscord) -> dict[str, Any]:
    """Alice on, and struck by Vergil: its card."""
    await rcon.join(3, "Alice", ALICE)
    await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
    before = len(posts_in(fake, STAFF))
    huragok.bus.emit("automod.strike", SERVER, player_id=ALICE, player_name="Alice", rule="slurs", strike=1, action="mute 1d", source="chat", evidence="something bad", by="Vergil", dry_run=False, ok=True)
    await wait_for(lambda: len(posts_in(fake, STAFF)) > before, timeout=15)
    return posts_in(fake, STAFF)[-1]


async def own_kick(huragok: Any, rcon: Any, fake: FakeDiscord) -> dict[str, Any]:
    """A moderator's own kick, in game: its card."""
    await rcon.join(3, "Alice", ALICE)
    await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
    await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands", reason="spam")
    await wait_for(lambda: posts_in(fake, LOGS), timeout=15)
    return posts_in(fake, LOGS)[-1]


async def form_of(fake: FakeDiscord, token: str) -> dict[str, Any]:
    await wait_for(lambda: any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks), timeout=10)
    return next(c for c in fake.callbacks if c["token"] == token)["data"]


async def said_back(fake: FakeDiscord, token: str) -> str:
    """What they were told: as a follow-up, or straight off if it was refused before anything else."""

    def told() -> str | None:
        for followup in fake.followups:
            if followup["token"] == token:
                return str(followup["content"])
        for callback in fake.callbacks:
            if callback["token"] == token and callback.get("type") == 4:
                return str(callback["data"].get("content"))
        return None

    await wait_for(lambda: told() is not None, timeout=10)
    return str(told())


async def fill(fake: FakeDiscord, label: str, post: dict[str, Any], answers: dict[str, str], **who: Any) -> str:
    """Press a button that opens a form, and send the form: the token its answer comes back on."""
    _, token = fake.press(buttons_of(post)[label], post, **who)
    await form_of(fake, token)
    return fake.fill_in(token, post, answers, **who)[1]


def row_of(huragok: Any, number: int = 1) -> Any:
    return huragok.storage.query("SELECT * FROM discord_tickets WHERE number = ?", (number,))[0]


def edited(fake: FakeDiscord) -> dict[str, Any]:
    return fake.original_edits[-1]


# Opening ---------------------------------------------------------------------------------------------------------------


def test_a_strike_is_one_card_with_everything_to_act_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            assert list(buttons_of(post)) == ["Mute", "Kick", "Ban", "Whois", "Add note", "Pardon strike", "Discuss", "Acknowledge"]
            assert post["embeds"][0]["color"] == colour(path, "gold")  # the strike's own: nobody has touched it
            assert "ticket 1" in post["embeds"][0]["footer"]["text"]
            # Vergil's mute, the action behind the strike, is on that card: it makes none of its own.
            await huragok.vergil.moderation.act("mute", SERVER, ALICE, by="Vergil", source="automod", duration="1d", reason="slurs")
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert len(posts_in(fake, STAFF)) == 1 and posts_in(fake, LOGS) == []

    run(scenario())


def test_a_second_strike_goes_on_the_first_ones_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Seven people saying listed words three times was 21 cards, each with eight buttons and its own Acknowledge."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            first = await strike(huragok, rcon, fake)
            huragok.bus.emit("automod.strike", SERVER, player_id=ALICE, player_name="Alice", rule="slurs", strike=2, action="mute 7d", source="chat", evidence="again")
            await wait_for(lambda: part.tickets.lines_of(1), timeout=15)
            assert len(posts_in(fake, STAFF)) == 1 and first["id"] == part.tickets.get(1)["message"]

    run(scenario())


def test_a_moderators_own_action_is_a_card_on_the_record(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await own_kick(huragok, rcon, fake)
            assert posts_in(fake, STAFF) == []
            assert post["embeds"][0]["color"] == colour(path, "orange")  # its own line's colour
            assert list(buttons_of(post)) == ["Mute", "Let back", "Ban", "Whois", "Add note", "Discuss"]  # nothing to acknowledge: it's closed already
            assert row_of(huragok)["state"] == "filed"

    run(scenario())


def test_a_moderators_own_strike_is_on_the_record_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            huragok.bus.emit("automod.strike", SERVER, player_id=ALICE, player_name="Alice", rule="slurs", strike=1, action="mute 1d", source="manual", evidence="said it in voice", by="Mod", manual=True, dry_run=False, ok=True)
            await wait_for(lambda: posts_in(fake, LOGS), timeout=15)
            assert posts_in(fake, STAFF) == [] and row_of(huragok)["owner"] == "people"

    run(scenario())


def test_what_has_nothing_to_act_on_is_one_line(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.event("vote", state="passed", subject="end the game", caller="Alice")
            huragok.bus.emit("server.restarted", SERVER, reason="the daily restart", by="the Superintendent")
            await wait_for(lambda: lines_in(fake, LOGS) and lines_in(fake, HURAGOK_LOGS), timeout=15)
            for post in (lines_in(fake, LOGS)[0], lines_in(fake, HURAGOK_LOGS)[0]):
                assert post["content"] and "components" not in post
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert huragok.storage.query("SELECT * FROM discord_tickets") == []

    run(scenario())


def test_where_a_channel_is_unset_it_falls_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, every_channel=False)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: posts_in(fake, LOGS), timeout=15)  # huragok-logs is unset: logs
            await rcon.chat(ALICE, "Alice", "!calladmin help")
            await wait_for(lambda: len(posts_in(fake, STAFF)) == 1, timeout=15)  # reports is unset: staff

    run(scenario())


def test_webhook_mode_has_no_tickets(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON + 'webhook = "https://discord.com/api/webhooks/1/x"\n', mode="webhook")
    feeds.handle(kick())
    assert part.posts[0]["webhook"] and "components" not in part.posts[0]


def test_a_copy_to_a_partner_has_no_buttons(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        address = "https://discord.com/api/webhooks/1/x"
        bot_file(tmp_path, change=("[feed.moderation]\nenabled = true", f'[feed.moderation]\nenabled = true\nalso = ["{address}"]'))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            posted: list[dict[str, Any]] = []
            real = part.poster.post
            part.poster.post = lambda persona, form, **where: (posted.append({"form": form, **where}), real(persona, form, **where))[1] if where.get("channel") else posted.append({"form": form, **where})
            await own_kick(huragok, rcon, fake)
            copies = [entry for entry in posted if entry.get("webhook") == address]
            assert len(copies) == 1 and "components" not in copies[0]["form"]

    run(scenario())


# The buttons follow how they stand ------------------------------------------------------------------------------------------


def test_nothing_locks_mute_undo_kick_undo_ban_undo_on_one_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)

            await fill(fake, "Mute", post, {"How long": "2h", "Why (they'll read this)": "shouting"})
            await wait_for(lambda: fake.original_edits, timeout=10)
            assert "Unmute" in buttons_of(edited(fake)) and "Mute" not in buttons_of(edited(fake))

            fake.press(buttons_of(edited(fake))["Unmute"], post)
            await wait_for(lambda: len(fake.original_edits) == 2, timeout=10)
            assert "Mute" in buttons_of(edited(fake))

            await fill(fake, "Kick", post, {"How long": "2h", "Why (they'll read this)": "again"})
            await wait_for(lambda: len(fake.original_edits) == 3, timeout=10)
            assert "Let back" in buttons_of(edited(fake)) and "Kick" not in buttons_of(edited(fake))

            fake.press(buttons_of(edited(fake))["Let back"], post)
            await wait_for(lambda: len(fake.original_edits) == 4, timeout=10)
            assert "Kick" in buttons_of(edited(fake))

            await fill(fake, "Ban", post, {"Why (they'll read this)": "enough"})
            await wait_for(lambda: len(fake.original_edits) == 5, timeout=10)
            assert list(buttons_of(edited(fake))) == ["Unban", "Whois", "Add note", "Pardon strike", "Discuss", "Acknowledge"]

            fake.press(buttons_of(edited(fake))["Unban"], post)
            await wait_for(lambda: len(fake.original_edits) == 6, timeout=10)
            assert "Ban" in buttons_of(edited(fake))

            # One card all along, with all of it written on it, in order.
            assert len(posts_in(fake, STAFF)) == 1 and posts_in(fake, LOGS) == []
            story = edited(fake)["embeds"][0]["description"]
            assert story.index("muted") < story.index("unmuted") < story.index("kicked") < story.index(" let ") < story.index("banned") < story.index("unbanned **Alice**")

    run(scenario())


def test_an_action_on_a_card_touches_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            await fill(fake, "Mute", post, {"How long": "2h"})
            await wait_for(lambda: fake.original_edits, timeout=10)
            assert row_of(huragok)["state"] == "active"
            assert edited(fake)["embeds"][0]["color"] == colour(path, "blurple")

    run(scenario())


def test_looking_touches_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Whois"], post)
            assert "Alice" in await said_back(fake, token)
            assert row_of(huragok)["state"] == "open" and fake.original_edits == []

    run(scenario())


def test_a_note_touches_it_and_shows_on_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            await fill(fake, "Add note", post, {"The note": "said sorry after"})
            await wait_for(lambda: fake.original_edits, timeout=10)
            fields = {field["name"]: field["value"] for field in edited(fake)["embeds"][0]["fields"]}
            assert "said sorry after" in fields["What staff have written"]
            assert row_of(huragok)["state"] == "active"

    run(scenario())


def test_the_file_says_which_buttons_there_are(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=('buttons = ["mute", "kick", "ban", "whois", "note"]', 'buttons = ["said", "ban"]'))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            assert list(buttons_of(await strike(huragok, rcon, fake))) == ["Said", "Ban", "Pardon strike", "Discuss", "Acknowledge"]

    run(scenario())


# Where a card goes --------------------------------------------------------------------------------------------------------


def test_acknowledging_files_it_with_huragok_and_it_stays_usable(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: posts_in(fake, HURAGOK_LOGS), timeout=15)
            await wait_for(lambda: posts_in(fake, STAFF) == [], timeout=15)  # the old card's gone
            filed = posts_in(fake, HURAGOK_LOGS)[0]
            assert filed["embeds"][0]["color"] == colour(path, "green")
            assert "acknowledged this" in filed["embeds"][0]["description"]
            assert list(buttons_of(filed)) == ["Mute", "Kick", "Ban", "Whois", "Add note", "Pardon strike", "Discuss"]

    run(scenario())


def test_acting_on_a_filed_card_sends_it_back_to_be_acknowledged_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: posts_in(fake, HURAGOK_LOGS) and not posts_in(fake, STAFF), timeout=15)
            filed = posts_in(fake, HURAGOK_LOGS)[0]
            await fill(fake, "Kick", filed, {"How long": "2h", "Why (they'll read this)": "still at it"})
            await wait_for(lambda: posts_in(fake, STAFF) and not posts_in(fake, HURAGOK_LOGS), timeout=15)
            back = posts_in(fake, STAFF)[0]
            assert back["embeds"][0]["color"] == colour(path, "blurple") and "Acknowledge" in buttons_of(back)
            assert row_of(huragok)["state"] == "active" and "kicked" in back["embeds"][0]["description"]

    run(scenario())


def test_a_moderators_card_comes_back_to_staff_and_closes_to_logs(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await own_kick(huragok, rcon, fake)
            await fill(fake, "Add note", post, {"The note": "not the first time"})
            await wait_for(lambda: posts_in(fake, STAFF) and not posts_in(fake, LOGS), timeout=15)
            back = posts_in(fake, STAFF)[0]
            assert "Close" in buttons_of(back)
            fake.press(buttons_of(back)["Close"], back)
            await wait_for(lambda: posts_in(fake, LOGS) and not posts_in(fake, STAFF), timeout=15)
            assert posts_in(fake, LOGS)[0]["embeds"][0]["color"] == colour(path, "green")
            assert "closed this" in posts_in(fake, LOGS)[0]["embeds"][0]["description"]

    run(scenario())


def test_nobody_touching_vergils_for_a_week_files_it_as_stale(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            part.tickets.sweep()
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert posts_in(fake, HURAGOK_LOGS) == []  # not yet
            huragok.storage.execute("UPDATE discord_tickets SET touched = touched - 8 * 86400")
            part.tickets.sweep()
            await wait_for(lambda: posts_in(fake, HURAGOK_LOGS) and not posts_in(fake, STAFF), timeout=15)
            filed = posts_in(fake, HURAGOK_LOGS)[0]
            assert filed["embeds"][0]["color"] == colour(path, "slate") and "Nobody has touched this for 2 days" in filed["embeds"][0]["description"]
            assert "Mute" in buttons_of(filed) and row_of(huragok)["state"] == "stale"

    run(scenario())


def test_stale_can_be_switched_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=("stale_after_days = 2", "stale_after_days = 0"))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            huragok.storage.execute("UPDATE discord_tickets SET touched = touched - 800 * 86400")
            part.tickets.sweep()
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert row_of(huragok)["state"] == "open"

    run(scenario())


def test_a_persons_request_waits_until_someone_closes_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(ALICE, "Alice", "!calladmin someone is team killing")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            assert list(buttons_of(post)) == ["Discuss", "Close"]
            huragok.storage.execute("UPDATE discord_tickets SET touched = touched - 8 * 3600")  # today's call is still today's. After a day it's finished itself, by its line
            part.tickets.sweep()
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert row_of(huragok)["state"] == "open"
            fake.press(buttons_of(post)["Close"], post)
            await wait_for(lambda: posts_in(fake, LOGS) and not posts_in(fake, REPORTS), timeout=15)  # people's go to logs

    run(scenario())


def test_saying_who_a_call_is_about_gives_it_the_buttons(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(ALICE, "Alice", "!calladmin team killing")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            menu = next(item for row in post["components"] for item in row["components"] if item["type"] == 3)
            assert {option["label"].split(" · ")[0] for option in menu["options"]} == {"Alice", "Bravo"} and all("#" in option["label"] for option in menu["options"])
            fake.choose(menu["custom_id"], post, [BRAVO])
            await wait_for(lambda: fake.original_edits, timeout=10)
            assert "Mute" in buttons_of(edited(fake)) and "says this is about **Bravo**" in edited(fake)["embeds"][0]["description"]
            assert row_of(huragok)["player_id"] == BRAVO

    run(scenario())


def test_a_problem_is_one_ticket_until_its_over(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, change=("repeat_minutes = 30", "repeat_minutes = 0"))
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="exit code 3")
            await wait_for(lambda: posts_in(fake, ALERTS), timeout=15)
            post = posts_in(fake, ALERTS)[0]
            assert list(buttons_of(post)) == ["Server", "Discuss", "Acknowledge"]
            huragok.bus.emit("server.hung", SERVER)
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_ticket_lines"), timeout=15)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert len(posts_in(fake, ALERTS)) == 1
            huragok.bus.emit("server.recovered", SERVER)
            await wait_for(lambda: posts_in(fake, HURAGOK_LOGS) and not posts_in(fake, ALERTS), timeout=15)
            over = posts_in(fake, HURAGOK_LOGS)[0]
            assert over["embeds"][0]["color"] == colour(path, "green") and "back" in over["embeds"][0]["description"].lower()

    run(scenario())


def test_a_card_someone_deleted_comes_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.posted[:] = [other for other in fake.posted if other["id"] != post["id"]]
            part.tickets.add_line(1, "something new")
            part.tickets.show(1)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            assert "something new" in posts_in(fake, STAFF)[0]["embeds"][0]["description"]

    run(scenario())


def test_an_edit_refused_for_now_catches_up_and_never_posts_another(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.refuse_next.append((f"/messages/{post['id']}", 400, {"message": "Maximum number of edits to messages older than 1 hour reached. Try again later", "code": 30046}, {}))
            part.tickets.add_line(1, "a line it can't show yet")
            part.tickets.show(1)
            await wait_for(lambda: part.tickets.dirty == {1}, timeout=15)
            assert len(posts_in(fake, STAFF)) == 1
            part.tickets.sweep()
            await wait_for(lambda: any("a line it can't show yet" in str(edit) for edit in fake.edits), timeout=15)
            assert len(posts_in(fake, STAFF)) == 1 and not part.tickets.dirty

    run(scenario())


def test_a_button_from_before_a_restart_still_works(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        kept = tmp_path / "kept.sqlite3"

        def adjust(settings: Any) -> None:
            settings.database = kept

        async with running(tmp_path, adjust=adjust) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
        async with running(tmp_path, adjust=adjust) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await fill(fake, "Kick", post, {"How long": "2h"})
            await wait_for(lambda: huragok.storage.query("SELECT * FROM actions WHERE kind = 'ban'"), timeout=10)

    run(scenario())


def test_a_card_that_gets_long_keeps_the_latest_and_fits(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            for number in range(120):
                part.tickets.add_line(1, f"line number {number} " + "x" * 200, when=1000.0 + number)
            form = part.tickets.embed(row_of(huragok))
            total = len(form["description"]) + len(form.get("title", "")) + sum(len(f["name"]) + len(f["value"]) for f in form.get("fields", [])) + len(form["footer"]["text"])
            assert total <= 6000 and len(form["description"]) <= 4096
            assert "line number 119" in form["description"] and "line number 0 " not in form["description"] and "earlier" in form["description"]

    run(scenario())


def test_colours_are_settings(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, change=('colours = { active = "blurple"', 'colours = { active = "#123456"'))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            await fill(fake, "Mute", post, {"How long": "2h"})
            await wait_for(lambda: fake.original_edits, timeout=10)
            assert edited(fake)["embeds"][0]["color"] == 0x123456
            assert load(path).section("tickets")["colours"]["stale"] == "slate"

    run(scenario())


# What happens to a finished card is the host's to choose ----------------------------------------------------------------


def test_a_finished_card_can_stay_where_it_is(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, change=('when_closed = "move"', 'when_closed = "stay"'))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: row_of(huragok)["state"] == "closed" and fake.edits, timeout=15)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert posts_in(fake, HURAGOK_LOGS) == [] and posts_in(fake, STAFF)[0]["id"] == post["id"]
            assert fake.edits[-1]["embeds"][0]["color"] == colour(path, "green") and "Acknowledge" not in buttons_of(fake.edits[-1])
            await fill(fake, "Mute", post, {"How long": "2h"})  # it's still usable, and comes back in place
            await wait_for(lambda: row_of(huragok)["state"] == "active" and fake.original_edits, timeout=15)
            assert posts_in(fake, STAFF)[0]["id"] == post["id"] and "Acknowledge" in buttons_of(edited(fake))

    run(scenario())


def test_going_stale_stays_put_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, change=('when_closed = "move"', 'when_closed = "stay"'))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            huragok.storage.execute("UPDATE discord_tickets SET touched = touched - 8 * 86400")
            part.tickets.sweep()
            await wait_for(lambda: row_of(huragok)["state"] == "stale" and fake.edits, timeout=15)
            assert posts_in(fake, HURAGOK_LOGS) == [] and fake.edits[-1]["embeds"][0]["color"] == colour(path, "slate")

    run(scenario())


def test_a_card_can_be_its_own_thread(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=('when_closed = "move"', 'when_closed = "thread"'))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            await wait_for(lambda: post["id"] in fake.threads and row_of(huragok)["thread"] == post["id"], timeout=15)
            await wait_for(lambda: any(f"<#{post['id']}>" in str(edit) for edit in fake.edits), timeout=15)  # the card says where it is
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: any(edit.get("archived") is True and edit.get("locked") is True and edit["channel"] == post["id"] for edit in fake.channel_edits), timeout=15)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert posts_in(fake, HURAGOK_LOGS) == [] and row_of(huragok)["state"] == "closed"
            await fill(fake, "Mute", post, {"How long": "2h"})
            await wait_for(lambda: any(edit.get("archived") is False and edit["channel"] == post["id"] for edit in fake.channel_edits), timeout=15)  # acting on it brings the thread back
            assert row_of(huragok)["state"] == "active"

    run(scenario())


def test_a_thread_it_cant_make_doesnt_stop_the_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=('when_closed = "move"', 'when_closed = "thread"'))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            fake.threads_refused = True
            post = await strike(huragok, rcon, fake)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=15)
            assert post["id"] not in fake.threads and not part.feeds.blocked  # refused a thread isn't refused the channel

    run(scenario())


def test_a_kind_of_card_can_have_a_log_channel_of_its_own(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_message(path, "vergil-strike", closes_to="4300")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: posts_in(fake, "4300") and not posts_in(fake, STAFF), timeout=15)
            assert posts_in(fake, HURAGOK_LOGS) == [] and not load(path).warnings

    run(scenario())


def test_a_kind_of_card_can_share_a_log_channel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_message(path, "vergil-strike", closes_to="logs")  # with what people did, rather than in huragok-logs
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: posts_in(fake, LOGS) and not posts_in(fake, STAFF), timeout=15)

    run(scenario())


def test_a_kind_of_card_can_stay_while_the_rest_move(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_message(path, "vergil-strike", closes_to="stay")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.press(buttons_of(post)["Acknowledge"], post)
            await wait_for(lambda: row_of(huragok)["state"] == "closed", timeout=15)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert posts_in(fake, STAFF)[0]["id"] == post["id"] and posts_in(fake, HURAGOK_LOGS) == []

    run(scenario())


def test_a_problem_thats_over_stays_too_when_told_to(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=('when_closed = "move"', 'when_closed = "stay"'))
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="exit code 3")
            await wait_for(lambda: posts_in(fake, ALERTS), timeout=15)
            huragok.bus.emit("server.recovered", SERVER)
            await wait_for(lambda: row_of(huragok)["state"] == "closed", timeout=15)
            assert posts_in(fake, HURAGOK_LOGS) == [] and len(posts_in(fake, ALERTS)) == 1

    run(scenario())


def test_the_setup_has_all_of_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            opened, form = await _opened(fake, "setup")
            labels = {c.get("label"): c["custom_id"] for c in _components(form) if c.get("custom_id") and c.get("label")}
            before = len(fake.callbacks)
            fake.pick(opened, labels["Tickets"], 2, [])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            page = fake.callbacks[-1]["data"]
            assert page["embeds"][0]["title"] == "Tickets"
            when = next(c for c in _components(page) if c["type"] == 3 and c["placeholder"].startswith("When a card"))
            before = len(fake.callbacks)
            fake.pick(opened, when["custom_id"], 3, ["thread"])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert read_toml(path)["tickets"]["when_closed"] == "thread"
            page = fake.callbacks[-1]["data"]
            stale = next(c for c in _components(page) if c["type"] == 3 and c["placeholder"].startswith("Untouched"))
            before = len(fake.callbacks)
            fake.pick(opened, stale["custom_id"], 3, ["14"])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert read_toml(path)["tickets"]["stale_after_days"] == 14

    run(scenario())


def test_the_invite_asks_for_manage_threads_only_when_cards_are_threads(tmp_path: Path) -> None:
    from huragok.discordbot import features, rest

    path = bot_file(tmp_path)
    assert not features.invite_permissions(load(path)) & rest.PERMISSIONS["manage_threads"]
    set_switch(path, ("tickets",), "when_closed", "thread")
    assert features.invite_permissions(load(path)) & rest.PERMISSIONS["manage_threads"]
    set_switch(path, ("tickets",), "when_closed", "move")
    set_message(path, "vergil-strike", closes_to="thread")
    assert features.invite_permissions(load(path)) & rest.PERMISSIONS["manage_threads"]


# Probationary moderators' requests -----------------------------------------------------------------------------------------


async def asked(huragok: Any, rcon: Any, fake: FakeDiscord) -> dict[str, Any]:
    """A probationary moderator asking, with a slash command, to kick Alice: its card."""
    await rcon.join(3, "Alice", ALICE)
    await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
    fake.command("kick", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE, reason="spam")
    await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
    return posts_in(fake, REPORTS)[0]


def requests_file(root: Path) -> Path:
    path = bot_file(root)
    set_switch(path, ("feed", "requests"), "enabled", True)
    set_switch(path, ("commands",), "enabled", True)
    return path


def test_a_request_is_a_ticket_with_confirm_and_deny(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await asked(huragok, rcon, fake)
            assert list(buttons_of(post)) == ["Confirm", "Deny", "Whois", "Add note", "Discuss"]
            assert "(on probation) asks, #1: !kick Alice spam" in post["embeds"][0]["description"]
            assert rcon.named("kick") == []  # nothing happens until someone says so

    run(scenario())


def test_confirming_does_it_says_so_and_closes_it_to_logs(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await asked(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Confirm"], post, user=MOD_USER, roles=(MOD_ROLE,))
            await wait_for(lambda: rcon.named("kick"), timeout=15)  # the card says so: nothing more's said to them
            await wait_for(lambda: posts_in(fake, LOGS) and not posts_in(fake, REPORTS), timeout=15)
            closed = next(card for card in posts_in(fake, LOGS) if "(on probation) asks" in card["embeds"][0]["description"])
            assert closed["embeds"][0]["color"] == colour(path, "green") and "confirmed it: !kick Alice spam" in closed["embeds"][0]["description"]
            assert "Confirm" not in buttons_of(closed)
            assert len(posts_in(fake, LOGS)) == 1  # the kick it did is that card's, not one of its own

    run(scenario())


def test_denying_asks_why_tells_the_card_and_does_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await asked(huragok, rcon, fake)
            sent = await fill(fake, "Deny", post, {"Why, for whoever asked": "not enough to go on"}, user=MOD_USER, roles=(MOD_ROLE,))
            await wait_for(lambda: posts_in(fake, LOGS) and not posts_in(fake, REPORTS), timeout=15)
            assert not [f for f in fake.followups if f["token"] == sent]
            assert "denied it: not enough to go on" in posts_in(fake, LOGS)[0]["embeds"][0]["description"]
            assert rcon.named("kick") == []

    run(scenario())


def test_deciding_in_game_closes_the_card_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await asked(huragok, rcon, fake)
            await rcon.join(2, "Mod", MOD)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(MOD, "Mod", "!deny 1 no")
            await wait_for(lambda: not posts_in(fake, REPORTS), timeout=15)
            assert "Mod denied it: no" in posts_in(fake, LOGS)[0]["embeds"][0]["description"]

    run(scenario())


def test_only_a_moderator_can_decide(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await asked(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Confirm"], post, user=MOD_USER, roles=(PROBATION_ROLE,))
            assert "for moderators" in await said_back(fake, token)
            assert rcon.named("kick") == [] and posts_in(fake, REPORTS)

    run(scenario())


def test_your_own_request_is_for_someone_else_to_confirm(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await asked(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Confirm"], post, user=OTHER, roles=(MOD_ROLE,))  # promoted since, say
            assert "your own request" in await said_back(fake, token)
            assert rcon.named("kick") == []

    run(scenario())


def test_slash_ticket_gives_the_buttons_for_anyone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.command("ticket", player=ALICE)
            assert "on its way" in await answer_text(fake, token)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            post = posts_in(fake, STAFF)[0]
            assert list(buttons_of(post)) == ["Mute", "Kick", "Ban", "Whois", "Add note", "Discuss", "Close"]
            _, token = fake.command("ticket", user=OTHER, player=ALICE)
            assert "for moderators and up" in await answer_text(fake, token)

    run(scenario())


# Who may ---------------------------------------------------------------------------------------------------------------


def test_someone_without_a_rank_cant_press(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Acknowledge"], post, user=OTHER)
            assert "for staff" in await answer_text(fake, token)
            assert row_of(huragok)["state"] == "open"

    run(scenario())


def test_a_moderator_cant_ban_where_the_game_says_admins(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Ban"], post, user=OTHER, roles=(MOD_ROLE,))
            assert "for admins and up" in await said_back(fake, token)  # at the press, before anyone's written why
            assert not any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks)
            assert rcon.named("ban") == [] and row_of(huragok)["state"] == "open"

    run(scenario())


def test_nobody_acts_on_their_own_rank_or_above(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(2, "Mod", MOD)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            huragok.bus.emit("automod.strike", SERVER, player_id=MOD, player_name="Mod", rule="slurs", strike=1, action="mute 1d", source="chat", evidence="bad", by="Vergil", dry_run=False, ok=True)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            post = posts_in(fake, STAFF)[0]
            _, token = fake.press(buttons_of(post)["Kick"], post, user=OTHER, roles=(MOD_ROLE,))
            assert "can't" in await said_back(fake, token)

    run(scenario())


def test_a_probationary_moderator_can_look_and_write_but_not_act_or_close(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            who = {"user": OTHER, "roles": (PROBATION_ROLE,)}
            sent = await fill(fake, "Mute", post, {"How long": "2h"}, **who)
            assert "Sent for review" in await said_back(fake, sent)  # theirs is a request, as in game
            assert rcon.named("mute") == [] and huragok.storage.query("SELECT * FROM commands_requests")
            _, token = fake.press(buttons_of(post)["Acknowledge"], post, **who)
            assert "takes a moderator" in await answer_text(fake, token)
            _, token = fake.press(buttons_of(post)["Whois"], post, **who)
            assert "Alice" in await said_back(fake, token)
            assert row_of(huragok)["state"] == "open"
            await fill(fake, "Add note", post, {"The note": "watching this one"}, **who)
            await wait_for(lambda: huragok.storage.notes_of(ALICE), timeout=10)

    run(scenario())


# What the forms take -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("typed", "length"), [("2h", "2h"), ("2 hours", "2h"), ("7 DAYS", "7d"), ("1 week", "1w"), ("90 mins", "90m"), ("", ""), ("perm", "perm"), ("forever", "perm"), ("permanent", "perm")])
def test_what_how_long_understands(typed: str, length: str) -> None:
    assert how_long(typed) == length


@pytest.mark.parametrize("typed", ["soon", "7 months", "0d", "7 fortnights"])
def test_what_how_long_doesnt(typed: str) -> None:
    assert how_long(typed) is None


def test_a_forms_hint_fits_on_its_line() -> None:
    """Discord cuts a placeholder at about 40 characters on a small window, and the host couldn't read the end of a longer one."""
    assert all(len(hint) <= 36 for hint, _ in HOW_LONG.values())


def test_a_time_it_cant_read_does_nothing_and_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            sent = await fill(fake, "Mute", post, {"How long": "soon"})
            assert "Nothing was done" in await answer_text(fake, sent)
            assert rcon.named("mute") == [] and row_of(huragok)["state"] == "open"

    run(scenario())


def test_a_ban_is_for_good_and_asks_only_why(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Ban"], post)
            assert [label["label"] for label in (await form_of(fake, token))["components"]] == ["Why (they'll read this)"]
            fake.fill_in(token, post, {"Why (they'll read this)": "slurs in voice"})
            await wait_for(lambda: rcon.named("ban"), timeout=10)
            assert rcon.named("ban")[0]["args"] == [ALICE, "slurs in voice"]

    run(scenario())


def test_a_kick_with_a_time_is_on_the_card_as_a_kick(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            await fill(fake, "Kick", post, {"How long": "7d", "Why (they'll read this)": "team killing"})
            await wait_for(lambda: fake.original_edits, timeout=10)
            assert rcon.named("ban")[0]["args"] == [ALICE, "7d", "Kicked for 7 days: team killing"]
            assert f"user{FACULTY_USER} kicked **Alice** for 7 days (team killing)" in edited(fake)["embeds"][0]["description"]
            assert "Let back" in buttons_of(edited(fake))

    run(scenario())


def test_a_kick_with_no_time_needs_them_to_be_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            await rcon.leave(3)
            await wait_for(lambda: not huragok.superintendent.states[SERVER].players, timeout=10)
            sent = await fill(fake, "Kick", post, {"Why (they'll read this)": "go"})
            assert "isn't on a server" in await said_back(fake, sent)
            assert rcon.named("kick") == []

    run(scenario())


def test_a_moderators_kick_from_a_card_keeps_to_the_limit(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            sent = await fill(fake, "Kick", post, {"How long": "7d"}, user=OTHER, roles=(MOD_ROLE,))
            assert "at most here" in await said_back(fake, sent)
            assert rcon.named("ban") == []

    run(scenario())


# Talking ---------------------------------------------------------------------------------------------------------------


def test_discuss_makes_one_thread_and_touches_the_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Discuss"], post)
            assert await said_back(fake, token) == f"<#{post['id']}>"
            assert "ticket 1: Alice" == fake.threads[post["id"]]
            assert row_of(huragok)["state"] == "active"
            _, token = fake.press(buttons_of(post)["Discuss"], post)  # someone else, a moment later: the same thread
            assert await said_back(fake, token) == f"<#{post['id']}>"
            assert len(fake.threads) == 1
            await wait_for(lambda: part.outbox.waiting == 0 and fake.edits, timeout=10)
            assert f"<#{post['id']}>" in str(fake.edits[-1])  # the card links to it from now on

    run(scenario())


def test_discuss_on_a_filed_card_brings_it_back_and_starts_there(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await own_kick(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Discuss"], post)
            answer = await said_back(fake, token)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            back = posts_in(fake, STAFF)[0]
            assert answer == f"<#{back['id']}>" and back["id"] in fake.threads

    run(scenario())


def test_discuss_says_what_the_bot_is_missing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            fake.threads_refused = True
            _, token = fake.press(buttons_of(post)["Discuss"], post)
            assert "Create Public Threads" in await said_back(fake, token)

    run(scenario())


# Old and unknown ---------------------------------------------------------------------------------------------------------


def test_a_button_from_an_older_test_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press("huragok:undo:305", post)
            assert "earlier version" in await answer_text(fake, token)

    run(scenario())


def test_a_button_this_version_doesnt_know_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press("huragok:t:1:a-later-button", post)
            assert "isn't one this version knows" in await answer_text(fake, token)

    run(scenario())


def test_how_they_stand_comes_from_the_ledger(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            record = lambda kind, **more: huragok.storage.record_action(kind=kind, server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", **more)  # noqa: E731
            standing = lambda: part.tickets.standing(ALICE)  # noqa: E731
            assert {key: value for key, value in standing().items() if key != "on"} == {"muted": False, "kicked": False, "banned": False}
            record("mute", duration="1h")
            assert standing()["muted"]
            record("unmute")
            assert not standing()["muted"]
            record("mute", duration="1h", when=time.time() - 7200)
            assert not standing()["muted"]  # it ran out
            record("mute")
            assert standing()["muted"]  # for good
            record("ban", duration="2h")
            assert standing()["kicked"] and not standing()["banned"]  # a ban with a time is a kick
            record("unban")
            assert not standing()["kicked"]
            record("kick")
            assert standing()["kicked"]
            record("pardon")
            assert not standing()["kicked"]
            record("kick", evidence="kicked on Other")  # passed on: not a kick of its own
            assert not standing()["kicked"]
            record("ban")
            assert standing()["banned"] and not standing()["kicked"]

    run(scenario())
