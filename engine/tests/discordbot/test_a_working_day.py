"""An evening's moderating, for the people doing it: who's told, what they can see, whether the card's still true. Each was found by walking a real day through the bot (plans/discord-audit/working-days.md)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, MOD
from test_audit_findings import FIRST, on, said_back, up  # noqa: F401
from test_audit_findings import bot_file as audit_file
from test_bot_running import OTHER
from test_cards_do_what_they_say import report, reports_on
from test_tickets import ALERTS, REPORTS, bot_file, buttons_of, fake, fill, posts_in  # noqa: F401
from testkit.fake_discord import ADMIN_ROLE, MOD_ROLE, FakeDiscord
from testkit.running import SERVER, fleet, running, tells_to, wait_for

from huragok.core.events import Event
from huragok.discordbot.config import load, set_message, set_switch
from huragok.players.automod.rules import parse_line

ADMIN = {"user": "4441", "roles": (ADMIN_ROLE,)}
MODERATOR = {"user": "4442", "roles": (MOD_ROLE,)}
WORD_LIST = "badword | 1h | 3 | | slurs\n"


def card_of(huragok: Any, number: int) -> dict[str, Any]:
    part = huragok.runners["discord"].part
    ticket = part.tickets.get(number)
    return {**part.tickets.edit_form(ticket), "id": ticket["message"], "channel": ticket["channel"]}


def test_moderators_in_game_hear_of_a_report(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """They're the only staff who can hear what's being reported, and they could act in seconds."""
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(4, "Mod", MOD)
            await report(huragok, fake, rcon)
            await wait_for(lambda: any("reported Alice" in line and "shouting" in line for line in tells_to(rcon, MOD)), timeout=15)

    run(scenario())


def test_a_setting_stops_that(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = reports_on(bot_file(tmp_path))
        set_switch(path, ("reports",), "tell_in_game", False)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(4, "Mod", MOD)
            await report(huragok, fake, rcon)
            await asyncio.sleep(0.5)
            assert not any("reported Alice" in line for line in tells_to(rcon, MOD))

    run(scenario())


def test_one_report_makes_a_sound(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """On a small server, voice abuse gets one or two reports."""
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await report(huragok, fake, rcon)
            assert not int(post.get("flags") or 0) & 4096

    run(scenario())


def test_reports_lists_whats_open_in_game(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(4, "Mod", MOD)
            await report(huragok, fake, rcon)
            await rcon.chat(MOD, "Mod", "!reports")
            await wait_for(lambda: any("A report on Alice" in line for line in tells_to(rcon, MOD)), timeout=15)

    run(scenario())


def test_old_chat_isnt_shown_as_what_they_just_said(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            now = time.time()
            add = "INSERT INTO commands_chat (time, server, player_id, name, text) VALUES (?, ?, ?, ?, ?)"
            huragok.storage.execute(add, (now - 86400 * 20, FIRST, ALICE, "Alice", "three weeks ago"))
            huragok.storage.execute(add, (now - 300, FIRST, ALICE, "Alice", "five minutes ago"))
            said = part.said_by(ALICE, 5, now)
            assert len(said) == 1 and said[0].startswith("five minutes ago") and "5 minutes" in said[0]
            assert len(part.recent_chat(FIRST, 5)) == 1

    run(scenario())


def test_a_clip_sent_second_is_kept(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """It's the only evidence of a voice there can be, and they'd been told the staff had it."""
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await report(huragok, fake, rcon)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind = 'reports-made'")[0]["number"])
            _, token = fake.command_with_file("report", "proof", "clip.mp4", "the clip", user=OTHER, player=ALICE)
            assert "Added" in await said_back(fake, token)
            await wait_for(lambda: any("clip.mp4" in str(line["text"]) for line in part.tickets.lines_of(number)), timeout=15)
            _, token = fake.command("report", user=OTHER, player=ALICE, why="doing it to the new kid too")
            assert "Added" in await said_back(fake, token)
            assert any("the new kid" in str(line["text"]) for line in part.tickets.lines_of(number))

    run(scenario())


def test_faulted_changes_the_top_of_the_card_and_pings(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_message(path, "alerts-faulted", ping="5101")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="exit code 3")
            await wait_for(lambda: posts_in(fake, ALERTS), timeout=15)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind LIKE 'alerts-%'")[0]["number"])
            retry = time.time() + 600
            huragok.bus.emit("server.faulted", SERVER, reason="5 starts in a row failed", retry_at=retry)
            await wait_for(lambda: "faulted" in str(part.tickets.get(number)["kind"]), timeout=15)
            kept = json.loads(part.tickets.get(number)["card"])
            assert "keeps failing" in kept["line"] and f"<t:{int(retry)}:R>" in kept["line"]
            await wait_for(lambda: any("<@&5101>" in str(post.get("content")) for post in fake.posted), timeout=15)

    run(scenario())


def test_acknowledging_a_server_thats_still_down_doesnt_file_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The next admin would see a clean channel with a server down."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="exit code 3")
            await wait_for(lambda: posts_in(fake, ALERTS), timeout=15)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind LIKE 'alerts-%'")[0]["number"])
            part.status.rows = lambda: [{"name": SERVER, "phase": "faulted"}]
            card = card_of(huragok, number)
            fake.press(buttons_of(card)["Acknowledge"], card, **ADMIN)
            await wait_for(lambda: any("is on it" in str(line["text"]) for line in part.tickets.lines_of(number)), timeout=15)
            assert part.tickets.get(number)["state"] in ("open", "active")

    run(scenario())


def test_stopping_it_for_the_night_closes_its_trouble(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            huragok.bus.emit("server.crashed", SERVER, why="exit code 3")
            await wait_for(lambda: posts_in(fake, ALERTS), timeout=15)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind LIKE 'alerts-%'")[0]["number"])
            huragok.bus.emit("server.stopped", SERVER, expected=True, by="Host")
            await wait_for(lambda: part.tickets.get(number)["state"] == "closed", timeout=15)
            assert any("Stopped by Host" in str(line["text"]) for line in part.tickets.lines_of(number))

    run(scenario())


def test_a_failed_update_closes_when_one_works(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            huragok.bus.emit("supervisor.update_failed", None, version="9.9", why="no network")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'alerts-update-failed'"), timeout=15)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind = 'alerts-update-failed'")[0]["number"])
            huragok.bus.emit("supervisor.started", None, version="9.9", parts=[])
            await wait_for(lambda: part.tickets.get(number)["state"] == "closed", timeout=15)

    run(scenario())


def test_tickets_carries_a_shift_change(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await report(huragok, fake, rcon)
            for number in range(3):
                part.tickets.open_by_hand("Mod", f"{number:064x}", f"P{number}", SERVER)
            _, token = fake.command("tickets", **MODERATOR)
            told = await said_back(fake, token)
            first = told.split(chr(10))[1]
            assert "report" in first and "Alice" in first and SERVER in first  # what someone's waiting on comes first
            for number in range(30, 60):
                part.tickets.open_by_hand("Mod", f"{number:064x}", f"P{number}", SERVER)
            _, token = fake.command("tickets", **MODERATOR)
            assert "it's a file" in await said_back(fake, token)  # it used to be cut off, with reports hidden by what sorted first

    run(scenario())


def test_a_card_says_where_they_are_now(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            number = part.tickets.open_by_hand("Mod", ALICE, "Alice", FIRST)
            fields = part.tickets.embed(part.tickets.get(number)).get("fields") or []
            now = next(field for field in fields if field["name"] == "Now")
            assert f"on {FIRST}" in now["value"]

    run(scenario())


def test_the_reporter_hears_when_staff_act(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await report(huragok, fake, rcon)
            await fill(fake, "Mute", post, {"How long": "10m", "Why (they'll read this)": "x"}, **ADMIN)
            await wait_for(lambda: any(str(p["channel"]).startswith("dm") and "acted on your report of Alice" in str(p.get("content")) for p in fake.posted), timeout=15)

    run(scenario())


def test_a_reporter_in_game_hears_too_and_only_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(BRAVO, "Bravo", "!report Alice shouting")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_reports"), timeout=15)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind = 'reports-made'")[0]["number"])
            part.reports.upheld(number)
            part.reports.upheld(number)
            await wait_for(lambda: any("acted on your report" in line for line in tells_to(rcon, BRAVO)), timeout=15)
            await asyncio.sleep(0.3)
            assert sum("acted on your report" in line for line in tells_to(rcon, BRAVO)) == 1

    run(scenario())


def test_a_muted_player_is_told_their_commands_wont_work(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Muted chat never arrives, so nothing answers, and they've no way to know why."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text("badword | 1h, 1d | | | slurs\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await rcon.chat(ALICE, "Alice", "badword")
            await wait_for(lambda: rcon.named("mute"), timeout=15)
            assert "commands won't work while you're muted" in str(rcon.named("mute")[-1])

    run(scenario())


def test_a_moderator_can_see_a_server_they_cant_power(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("commands",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            _, token = fake.command("server", user="4442", roles=(MOD_ROLE,))
            await wait_for(lambda: any(callback["token"] == token for callback in fake.callbacks), timeout=10)
            answered = next(callback for callback in fake.callbacks if callback["token"] == token)["data"]
            labels = [item.get("label") for row in answered.get("components") or [] for item in row["components"]]
            assert answered.get("embeds") and "Refresh" in labels and "Stop" not in labels and "Restart" not in labels

    run(scenario())


def test_a_ladder_with_no_words_for_voice(tmp_path: Path) -> None:
    rule = parse_line("| 1d, 7d | 2, 1h | 3 | voice", 1)
    assert rule is not None and rule.name == "voice" and not rule.words
    from huragok.players.automod.rules import RuleError

    try:
        parse_line("| 1d, 7d | 2, 1h | 3 |", 1)
    except RuleError:
        return
    raise AssertionError("no words and no name is a mistake")


def test_the_board_says_stopped_and_stays_up_for_a_countdown() -> None:
    from huragok.discordbot.status import condition

    assert condition({"phase": "idle"}, None) == "stopped"
    assert condition({"phase": "countdown"}, None) == "up"
    assert condition({"phase": "crashed"}, None) == "down"


def test_seeding_goes_when_the_server_does(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """"Come and fill it", for half an hour, about a server that's crashed."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.status.store("news:seeding-wanted:" + SERVER, message="9998", value="4001")
            part.feeds.handle(Event("server.crashed", SERVER, {"why": "x"}))
            assert huragok.storage.query("SELECT 1 FROM discord_tidy WHERE message = '9998'") or "9998" in fake.deleted
            assert not part.status.stored("news:seeding-wanted:" + SERVER)[0]

    run(scenario())


def test_an_acted_on_report_finishes_itself(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await report(huragok, fake, rcon)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind = 'reports-made'")[0]["number"])
            part.reports.upheld(number)
            part.tickets.touched(number)
            part.ctx.db.execute("UPDATE discord_tickets SET touched = ? WHERE number = ?", (time.time() - 25 * 3600, number))
            part.tickets.sweep()
            assert part.tickets.get(number)["state"] == "stale"

    run(scenario())


def test_the_settings_file_changing_is_said(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    import os

    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            assert not huragok._settings_waiting()
            later = time.time() + 60
            os.utime(huragok.settings.path, (later, later))
            assert "takes effect when Huragok starts again" in huragok._settings_waiting()[0]

    run(scenario())
