"""What a card offers is what can really be done, for whoever's pressing, now. Each of these was found by reading the cards against what they're for (plans/discord-audit/cards-and-buttons.md)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, MOD
from test_bot_running import OTHER, answer_text
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, PROBATION_ROLE, REPORTS, STAFF, bot_file, buttons_of, fake, fill, form_of, posts_in, row_of, strike, up  # noqa: F401
from testkit.fake_discord import ADMIN_ROLE, MOD_ROLE, OWNER_USER, FakeDiscord
from testkit.running import SERVER, fleet, running, wait_for

ADMIN = {"user": "4441", "roles": (ADMIN_ROLE,)}
MODERATOR = {"user": "4442", "roles": (MOD_ROLE,)}


def latest(fake: FakeDiscord, number: int, huragok: Any) -> dict[str, Any]:
    """The card as it stands now, however it was last changed."""
    ticket = row_of(huragok, number)
    form = huragok.runners["discord"].part.tickets.edit_form(ticket)
    return {**form, "id": ticket["message"], "channel": ticket["channel"]}


async def report(huragok: Any, fake: FakeDiscord, rcon: Any, *, who: str = OTHER) -> dict[str, Any]:
    await rcon.join(3, "Alice", ALICE)
    await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
    fake.command("report", user=who, player=ALICE, why="shouting")
    await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
    return posts_in(fake, REPORTS)[-1]


def reports_on(path: Path) -> Path:
    from huragok.discordbot.config import set_switch

    set_switch(path, ("reports",), "enabled", True)
    set_switch(path, ("appeals",), "enabled", True)
    set_switch(path, ("feed", "requests"), "enabled", True)
    set_switch(path, ("commands",), "enabled", True)
    return path


def test_a_report_can_be_muted_warned_and_struck(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text("slurs | 1h | 1d | perm\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await report(huragok, fake, rcon)
            assert {"Mute", "Kick", "Ban", "Strike", "Warn", "Whois", "Dismiss"} <= set(buttons_of(post))

    run(scenario())


def test_a_refusal_comes_before_the_form_not_after_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Ban"], post, **MODERATOR)  # a ban's for admins
            assert "for admins and up" in await answer_text(fake, token)
            assert not any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks)

    run(scenario())


def test_acting_on_a_card_says_nothing_unless_it_fails(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            token = await fill(fake, "Mute", post, {"How long": "1h", "Why (they'll read this)": "spam"})
            await wait_for(lambda: rcon.named("mute"), timeout=10)
            await wait_for(lambda: any("(spam)" in line["text"] for line in huragok.storage.query("SELECT text FROM discord_ticket_lines")), timeout=10)
            assert not [f for f in fake.followups if f["token"] == token]  # the card's what says so

    run(scenario())


def test_a_kick_on_someone_whos_left_asks_for_a_time_up_front(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press(buttons_of(post)["Kick"], post)
            box = (await form_of(fake, token))["components"][0]["component"]
            assert box["required"] is False and "10m" in box["placeholder"]
            await rcon.leave(3)
            await wait_for(lambda: not huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.press(buttons_of(post)["Kick"], post)
            box = (await form_of(fake, token))["components"][0]["component"]
            assert box["required"] is True and "left" in box["placeholder"]

    run(scenario())


def test_a_label_gone_stale_says_so_and_the_card_catches_up(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Muted on one card, and then Unmuted from there: another card, drawn before that, still says Mute until it's shown again."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            _, token = fake.press("huragok:t:1:letback", post)  # as a card drawn before they were let back still says
            assert "Nothing's keeping them out" in await answer_text(fake, token)
            assert not rcon.named("pardon")

    run(scenario())


def test_every_live_card_on_a_player_follows_how_they_stand(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            first = await strike(huragok, rcon, fake)
            await huragok.vergil.moderation.act("mute", SERVER, ALICE, by="Mod", source="commands", reason="spam")
            await wait_for(lambda: any(edit["message"] == first["id"] and "Unmute" in str(edit.get("components")) for edit in fake.edits), timeout=10)

    run(scenario())


def test_banned_on_one_server_and_on_another_still_has_buttons(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The card for "joined, and is banned elsewhere" exists to act on them: it used to offer only the opposite."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=("Red", "Blue")) as (huragok, consoles):
            await up(huragok)
            await huragok.vergil.moderation.act("ban", "Red", ALICE, by="Mod", source="in game", reason="cheating")
            await consoles["Blue"].join(3, "Alice", ALICE)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            await wait_for(lambda: any("banned-elsewhere" in str(row["kind"]) for row in huragok.storage.query("SELECT kind FROM discord_tickets")), timeout=10)
            row = next(row for row in huragok.storage.query("SELECT * FROM discord_tickets") if "banned-elsewhere" in str(row["kind"]))
            labels = buttons_of(latest(fake, int(row["number"]), huragok))
            assert {"Mute", "Kick", "Ban everywhere"} <= set(labels)

    run(scenario())


def test_a_wrong_strike_can_be_taken_back_from_its_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            assert "Pardon strike" in buttons_of(post)

    run(scenario())


def test_one_person_rejoining_is_one_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands", reason="spam")
            await wait_for(lambda: posts_in(fake, LOGS), timeout=15)
            for _ in range(3):
                await rcon.join(3, "Alice", ALICE)
                await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
                await rcon.leave(3)
                await wait_for(lambda: not huragok.superintendent.states[SERVER].players, timeout=10)
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'watch-back-after-kick'"), timeout=10)
            rows = huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'watch-back-after-kick'")
            assert len(rows) == 1
            await wait_for(lambda: len(huragok.storage.query("SELECT * FROM discord_ticket_lines WHERE ticket = ?", (rows[0]["number"],))) >= 2, timeout=10)

    run(scenario())


def test_sign_in_failures_are_one_card_that_closes_itself(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            for _ in range(3):
                huragok.bus.emit("rcon.auth_failed", SERVER, why="wrong password")
            await wait_for(lambda: posts_in(fake, ALERTS), timeout=15)
            rows = huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'alerts-rcon-sign-in'")
            assert len(rows) == 1 and rows[0]["problem"] == f"rcon:{SERVER}"
            huragok.bus.emit("rcon.connected", SERVER)
            await wait_for(lambda: huragok.storage.query("SELECT state FROM discord_tickets WHERE number = ?", (rows[0]["number"],))[0]["state"] == "closed", timeout=10)

    run(scenario())


def test_running_slowly_closes_itself_when_its_healthy_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            huragok.bus.emit("watchdog.health", SERVER, level="degraded", previous="healthy", reasons=["slow"])
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'alerts-slow'") or posts_in(fake, ALERTS), timeout=15)
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'alerts-slow'"), timeout=10)
            huragok.bus.emit("watchdog.health", SERVER, level="healthy", previous="degraded", reasons=[])
            await wait_for(lambda: huragok.storage.query("SELECT state FROM discord_tickets WHERE kind = 'alerts-slow'")[0]["state"] == "closed", timeout=10)

    run(scenario())


def test_an_alert_reopened_isnt_the_next_incident(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="boom")
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'alerts-crashed'"), timeout=15)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets")[0]["number"])
            part.tickets.problem_over(SERVER)
            assert not huragok.storage.query("SELECT problem FROM discord_tickets WHERE number = ?", (number,))[0]["problem"]

    run(scenario())


def test_a_second_press_of_close_does_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            part.tickets.close(1, "Mod")
            lines = len(part.tickets.lines_of(1))
            assert part.tickets.close(1, "Mod") is False
            assert len(part.tickets.lines_of(1)) == lines

    run(scenario())


def test_a_lost_move_isnt_left_behind(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A card moves by posting anew and deleting the old one. If the new post never arrives, the sweep tries again, and the old one's still known about."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            part.tickets.clock = lambda: __import__("time").time() + 601
            huragok.storage.execute("UPDATE discord_tickets SET message = '', replacing = ?, channel = ? WHERE number = 1", (f"{STAFF}:{post['id']}", HURAGOK_LOGS))
            part.tickets.sweep()
            await wait_for(lambda: not any(one["id"] == post["id"] for one in fake.posted), timeout=15)  # the old one's gone, however it was deleted
            assert row_of(huragok)["message"] and not row_of(huragok)["replacing"]

    run(scenario())


def test_automatic_tickets_finish_themselves_by_kind(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            part.tickets.clock = lambda: __import__("time").time() + 47 * 3600
            part.tickets.sweep()
            assert row_of(huragok)["state"] == "open"
            part.tickets.clock = lambda: __import__("time").time() + 49 * 3600
            part.tickets.sweep()
            assert row_of(huragok)["state"] == "stale"

    run(scenario())


def test_a_line_sent_to_its_own_channel_stays_what_it_was(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = reports_on(bot_file(tmp_path))
        fake.channels.append(fake._channel("4777", "appeals-here", overwrites=[{"id": fake.guild["id"], "type": 0, "allow": "0", "deny": str(1 << 10)}, {"id": "5009", "type": 0, "allow": str(1 << 10), "deny": "0"}]))
        from huragok.discordbot.config import set_message

        set_message(path, "reports-made", channel="#appeals-here")
        set_message(path, "alerts-crashed", channel="#appeals-here")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="boom")
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'alerts-crashed'"), timeout=15)
            row = huragok.storage.query("SELECT * FROM discord_tickets WHERE kind = 'alerts-crashed'")[0]
            assert row["state"] == "open" and row["channel"] == "4777"  # a ticket still, with Acknowledge

    run(scenario())


def test_an_owner_can_be_kept_to_admin_rank_in_game(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot.config import load, set_switch
        from huragok.discordbot.ranks import in_game

        path = bot_file(tmp_path)
        assert in_game(load(path), "owner") == "owner"
        set_switch(path, ("ranks",), "owners_in_game", "admin")
        assert in_game(load(path), "owner") == "admin"
        assert in_game(load(path), "moderator") == "moderator"

    run(scenario())
