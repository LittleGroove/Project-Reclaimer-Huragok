"""Whatever's pressed or typed says what really happened, and goes where the thing really is. Each of these was found by an audit on 1 November (plans/discord-audit/new-code.md, working-days.md, verify-fixes.md), and each goes through the real press or the real line."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, MOD
from test_audit_findings import ADMIN_USER, FIRST, SECOND, buttons_of, on, said_back, up  # noqa: F401
from test_audit_findings import bot_file as audit_file
from test_cards_do_what_they_say import report, reports_on
from test_tickets import PROBATION_ROLE, REPORTS, STAFF, bot_file, fake, fill, posts_in, row_of, strike  # noqa: F401
from testkit.fake_discord import ADMIN_ROLE, MOD_ROLE, FakeDiscord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.discordbot.config import load, set_channel, set_message, set_switch
from huragok.players.targets import Candidate, Picks

ADMIN = {"user": "4441", "roles": (ADMIN_ROLE,)}
MODERATOR = {"user": "4442", "roles": (MOD_ROLE,)}
OTHER_MODERATOR = {"user": "4444", "roles": (MOD_ROLE,)}
PROBATION = {"user": "4443", "roles": (PROBATION_ROLE,)}
WORD_LIST = "badword | 1h | 3 | | slurs\n"
THE_REASON = "Why (they'll read this)"


def card_of(huragok: Any, number: int) -> dict[str, Any]:
    part = huragok.runners["discord"].part
    ticket = part.tickets.get(number)
    return {**part.tickets.edit_form(ticket), "id": ticket["message"], "channel": ticket["channel"]}


def lines_on(huragok: Any, number: int) -> list[str]:
    return [str(line["text"]) for line in huragok.runners["discord"].part.tickets.lines_of(number)]


def number_of(huragok: Any, kind: str) -> int:
    return int(huragok.storage.query("SELECT number FROM discord_tickets WHERE kind = ? ORDER BY number DESC", (kind,))[0]["number"])


# Ban everywhere ---------------------------------------------------------------------------------------------------------


async def banned_on_first_joined_on_second(huragok: Any, consoles: Any, part: Any) -> int:
    huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Mod", source="in game", reason="cheating")
    await consoles[SECOND].join(3, "Alice", ALICE)
    await wait_for(lambda: huragok.superintendent.states[SECOND].player_by_id(ALICE), timeout=10)
    await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'watch-banned-elsewhere'"), timeout=15)
    number = number_of(huragok, "watch-banned-elsewhere")
    await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
    return number


def test_ban_everywhere_runs_where_the_ban_is_and_does_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("feed", "watch"))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            part = await up(huragok)
            consoles[FIRST].ban_list.append({"id": ALICE, "name": "Alice", "reason": "cheating", "banned_by": "Mod", "banned_at": int(time.time())})
            number = await banned_on_first_joined_on_second(huragok, consoles, part)
            card = card_of(huragok, number)
            fake.press(buttons_of(card)["Ban everywhere"], card, user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: any("every server" in line for line in lines_on(huragok, number)), timeout=15)
            assert huragok.storage.query("SELECT 1 FROM commands_every_server WHERE kind = 'ban' AND target = ? AND lifted IS NULL", (ALICE,))

    run(scenario())


def test_ban_everywhere_doesnt_claim_what_didnt_happen(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The record says a ban, and no server's list has it any more."""
    async def scenario() -> None:
        audit_file(tmp_path, ("feed", "watch"))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            part = await up(huragok)
            number = await banned_on_first_joined_on_second(huragok, consoles, part)
            card = card_of(huragok, number)
            _, token = fake.press(buttons_of(card)["Ban everywhere"], card, user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: any(f["token"] == token for f in fake.followups), timeout=15)
            assert not any("every server" in line for line in lines_on(huragok, number))
            assert not huragok.storage.query("SELECT 1 FROM commands_every_server WHERE kind = 'ban' AND target = ?", (ALICE,))

    run(scenario())


# Lift it ------------------------------------------------------------------------------------------------------------------


def test_lifting_one_of_two_bans_doesnt_tell_them_its_over(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            part = await up(huragok)
            for server in (FIRST, SECOND):
                consoles[server].ban_list.append({"id": ALICE, "name": "Alice", "reason": "cheating", "banned_by": "Mod", "banned_at": int(time.time())})
                huragok.vergil.moderation.record_seen("ban", server, ALICE, "Alice", by="Mod", source="in game", reason="cheating")
            number = part.appeals.file("7777", "Alice", ALICE, "Alice", "ban", [("What happened?", "x")])
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            card = card_of(huragok, number)
            _, token = fake.press(buttons_of(card)["Lift it"], card, user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: any(f["token"] == token and "Still banned" in str(f.get("content")) for f in fake.followups), timeout=15)
            assert huragok.storage.query("SELECT outcome FROM discord_appeals")[0]["outcome"] == ""
            assert not [post for post in fake.posted if str(post["channel"]).startswith("dm")]
            assert part.tickets.standing(ALICE)["banned"]  # so the card can't offer to ban them
            assert any("Still banned" in line for line in lines_on(huragok, number))
            card = card_of(huragok, number)  # and the second press lifts the other, and it's over
            fake.press(buttons_of(card)["Lift it"], card, user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: huragok.storage.query("SELECT outcome FROM discord_appeals")[0]["outcome"] == "lifted", timeout=15)
            assert not part.tickets.standing(ALICE)["banned"]

    run(scenario())


def test_a_kick_on_one_server_doesnt_hide_a_ban_on_another(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Banned on one, then kicked from another: the last word overall was a kick, and the ban's still there."""
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, _):
            part = await up(huragok)
            moderation = huragok.vergil.moderation
            moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Mod", source="in game", reason="x")
            moderation.record_seen("kick", SECOND, ALICE, "Alice", by="Mod", source="in game", reason="y")
            assert part.tickets.standing(ALICE)["banned"]
            assert part.banned_on(ALICE) == [FIRST]

    run(scenario())


def test_a_command_unban_doesnt_lift_a_servers_own_ban_in_the_record(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, _):
            part = await up(huragok)
            moderation = huragok.vergil.moderation
            moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Mod", source="in game", reason="own")
            huragok.storage.record_action(kind="ban", server=SECOND, target_id=ALICE, target_name="Alice", by="Mod", source="commands", reason="every")
            huragok.storage.record_action(kind="unban", server=SECOND, target_id=ALICE, target_name="Alice", by="Mod", source="commands")
            assert part.banned_on(ALICE) == [FIRST]
            assert part.where_it_lives(ALICE, ("ban",)) == FIRST

    run(scenario())


def test_a_lift_goes_where_that_thing_was_given(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Banned on one server and muted on another: an Unban went to wherever the last of them was."""
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, _):
            part = await up(huragok)
            moderation = huragok.vergil.moderation
            moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Mod", source="in game", reason="x")
            moderation.record_seen("mute", SECOND, ALICE, "Alice", by="Mod", source="in game", reason="y")
            assert part.where_it_lives(ALICE, ("ban",)) == FIRST
            assert part.where_it_lives(ALICE, ("mute",)) == SECOND

    run(scenario())


# Strike ---------------------------------------------------------------------------------------------------------------------


def test_a_strike_that_did_nothing_says_so_and_changes_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await report(huragok, fake, rcon)
            number = number_of(huragok, "reports-made")
            huragok.runners["automod"].part.mode = lambda server: "off"
            token = await fill(fake, "Strike", post, {"Which rule": "slurs", THE_REASON: "voice"}, **ADMIN)
            assert await said_back(fake, token)
            assert not huragok.storage.query("SELECT 1 FROM automod_strikes")
            assert huragok.storage.query("SELECT upheld FROM discord_reports")[0]["upheld"] == 0
            assert row_of(huragok, number)["state"] == "open"
            assert not part.tickets.expecting  # so the next real act on them still gets its card

    run(scenario())


def test_on_probation_a_strike_is_a_request(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await report(huragok, fake, rcon)
            number = number_of(huragok, "reports-made")
            token = await fill(fake, "Strike", post, {"Which rule": "slurs", THE_REASON: "voice"}, **PROBATION)
            assert "and up" not in await said_back(fake, token)
            assert huragok.storage.query("SELECT 1 FROM commands_requests WHERE command = 'strike'")
            assert not huragok.storage.query("SELECT 1 FROM automod_strikes")
            assert any("asked for" in line for line in lines_on(huragok, number))

    run(scenario())


def test_a_strike_typed_in_game_reaches_the_report_it_answers(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Otherwise the next moderator reads a report nobody's touched, and strikes them again for the same thing."""
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await report(huragok, fake, rcon)
            number = number_of(huragok, "reports-made")
            await rcon.join(4, "Mod", MOD)
            await wait_for(lambda: huragok.superintendent.states[SERVER].player_by_id(MOD), timeout=10)
            await rcon.chat(MOD, "Mod", "!strike Alice slurs slurring in voice")
            await wait_for(lambda: any("strike" in line for line in lines_on(huragok, number)), timeout=15)
            assert any("slurring in voice" in line for line in lines_on(huragok, number))
            assert huragok.storage.query("SELECT upheld FROM discord_reports")[0]["upheld"] == 1
            assert row_of(huragok, number)["state"] == "active"
            assert not huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind LIKE 'vergil-%'")  # no second card for the one incident

    run(scenario())


def test_what_a_moderator_writes_on_a_strike_goes_where_it_says(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await report(huragok, fake, rcon)
            number = number_of(huragok, "reports-made")
            await fill(fake, "Strike", post, {"Which rule": "slurs", THE_REASON: "slurring in voice"}, **ADMIN)
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM automod_strikes"), timeout=15)
            await wait_for(lambda: any("slurring in voice" in line for line in lines_on(huragok, number)), timeout=15)  # on the card
            await wait_for(lambda: any("slurring in voice" in str(call) for call in rcon.named("mute")), timeout=15)  # and what they read
            row = huragok.storage.query("SELECT by_id FROM actions WHERE target_id = ? AND kind = 'mute'", (ALICE,))[0]
            assert str(row["by_id"]).startswith("discord:")  # and the record knows it wasn't Vergil's own

    run(scenario())


def test_one_live_card_per_player_for_strikes(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Seven people saying words three times was 21 cards. Later strikes are lines on theirs, and Acknowledge clears the lot."""
    async def scenario() -> None:
        bot_file(tmp_path)
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await rcon.chat(ALICE, "Alice", "badword")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-strike'"), timeout=15)
            automod = huragok.runners["automod"].part
            automod.last_strike.clear()
            await rcon.chat(ALICE, "Alice", "badword")
            await wait_for(lambda: len(huragok.storage.query("SELECT 1 FROM automod_strikes")) >= 2, timeout=15)
            await wait_for(lambda: part.tickets.lines_of(number_of(huragok, "vergil-strike")), timeout=15)
            assert len(huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-strike'")) == 1

    run(scenario())


def test_a_rule_keeps_its_name_for_pardon_strike(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Written safely for a card it read hate\\_speech, which matched nothing, and Pardon strike cleared every rule instead."""
    from huragok.core.events import Event
    from huragok.discordbot import cards

    config = load(tmp_path / "none.toml")
    card = cards.strike(config, Event("automod.strike", SERVER, {"player_id": ALICE, "player_name": "Alice", "rule": "hate_speech", "strike": 1, "action": "mute 1h", "by": "Vergil", "evidence": "x"}))
    assert card.rule == "hate_speech"


# Two people on one card ------------------------------------------------------------------------------------------------------


def test_someone_elses_ban_a_moment_ago_stops_a_lift_going_through(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """One presses Ban, the other reaches for Mute, and the button there is now something else. Asked once, and pressed again it's meant."""
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            number = part.tickets.open_by_hand("Mod", ALICE, "Alice", FIRST)
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            huragok.storage.record_action(kind="mute", server=FIRST, target_id=ALICE, target_name="Alice", by="First Mod", source="commands", duration="7d", reason="x")
            card = card_of(huragok, number)
            sent = len(consoles[FIRST].named("unmute"))
            _, token = fake.press(buttons_of(card)["Unmute"], card, user="4441", roles=(ADMIN_ROLE,))
            told = await said_back(fake, token)
            assert "First Mod muted them a moment ago" in told and "Nothing was done" in told
            assert len(consoles[FIRST].named("unmute")) == sent
            fake.press(buttons_of(card)["Unmute"], card, user="4441", roles=(ADMIN_ROLE,))
            await wait_for(lambda: len(consoles[FIRST].named("unmute")) > sent, timeout=15)

    run(scenario())


def test_buttons_stay_where_they_were(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Before: Mute, Kick, Ban. After a ban with them gone it was Unban first, under the finger of whoever was reaching for Mute."""
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            number = part.tickets.open_by_hand("Mod", ALICE, "Alice", FIRST)
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            before = [button["label"] for row in card_of(huragok, number)["components"] for button in row["components"] if button.get("type") == 2]
            huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Mod", source="in game", reason="x")
            part.tickets.show(number)
            buttons = [button for row in card_of(huragok, number)["components"] for button in row["components"] if button.get("type") == 2]
            after = [button["label"] for button in buttons]
            assert after.index("Unban") == before.index("Ban")
            assert after.index("Mute") == before.index("Mute")
            assert next(button for button in buttons if button["label"] == "Mute").get("disabled") is True
            assert next(button for button in buttons if button["label"] == "Kick").get("disabled") is True

    run(scenario())


# Lists ---------------------------------------------------------------------------------------------------------------------------


def test_each_servers_list_numbers_on_with_nobody_twice() -> None:
    picks = Picks()
    alice, eve = Candidate(ALICE, "Alice"), Candidate(BRAVO, "Eve")
    with picks.adding("me"):
        assert picks.offer("me", [alice]) == [(1, alice)]
        assert picks.offer("me", [alice, eve]) == [(2, eve)]
    assert picks.get("me", 1) is alice and picks.get("me", 2) is eve
    assert picks.offer("me", [eve]) == [(1, eve)]  # a new list, on its own, starts again


def test_letback_at_a_number_goes_to_the_server_it_was_given_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """/kicks with no server, two up: @1 meant the last server's first, and went to the first server."""
    from huragok.discordbot.slash import _server_for
    from huragok.players.commands.lists import ListCommands  # noqa: F401 - the command's there

    async def scenario() -> None:
        audit_file(tmp_path, ("commands",))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, _):
            part = await up(huragok)
            huragok.storage.record_action(kind="ban", server=SECOND, target_id=BRAVO, target_name="Eve", by="Mod", source="commands", duration="1h")
            picks = huragok.vergil.commands.picks
            picks.offer("discord:1", [Candidate(BRAVO, "Eve")])
            spec = next(known for known, _role in part.ctx.game_commands() if known.name == "pardon")
            assert _server_for(part, spec, "", "@1", "discord:1") == ([SECOND], "")

    run(scenario())


def test_whois_and_bans_call_a_kick_a_kick(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from huragok.players.commands.lists import ban_note

    now = time.time()
    timed = ban_note({"id": ALICE, "reason": "Kicked for 1 hour: spam", "banned_by": "Mod", "banned_at": now, "expires": now + 3600}, now)
    forever = ban_note({"id": ALICE, "reason": "cheating", "banned_by": "Mod", "banned_at": now}, now)
    assert timed.startswith("kicked") and ": spam" in timed and "Kicked for" not in timed
    assert forever.startswith("banned") and ": cheating" in forever


# Probation, and limits -------------------------------------------------------------------------------------------------------------


def test_a_confirmed_kick_keeps_to_whoever_confirmed_its_limit(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Asked for on someone who's left, confirmed by a moderator: it went past their 1-day limit."""
    async def scenario() -> None:
        audit_file(tmp_path, ("feed", "requests"), ("commands",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            commands = huragok.vergil.commands
            console = consoles[FIRST]
            await console.join(9, "Mod", MOD)
            await wait_for(lambda: huragok.superintendent.states[FIRST].player_by_id(MOD), timeout=10)
            from huragok.servers.state import Player

            said: list[str] = []

            async def tell(_server: str, _who: str, text: str) -> None:
                said.append(text)

            part = huragok.runners["commands"].part
            row = {"slot": 1, "by_name": "Prob"}
            why = await part._while_away(type("Call", (), {"player": Player(9, "Mod", MOD), "role": "moderator", "_tell": tell, "call": None})(), row, "kick", Player(0, "Bravo", BRAVO), ["30d", "griefing"], FIRST)
            assert why and "at most" in why
            assert not huragok.storage.query("SELECT 1 FROM actions WHERE target_id = ? AND kind = 'ban'", (BRAVO,))
            assert commands is not None

    run(scenario())


def test_on_probation_looking_works_in_the_typed_console(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from test_console import bot_file as console_file
    from test_console import reply_to
    from testkit.fake_discord import STAFF_CHANNEL

    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = huragok.runners["discord"].part
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            line = fake.message(STAFF_CHANNEL, "!players", roles=(PROBATION_ROLE,), user="4443")
            assert "and up" not in (await reply_to(fake, line))["content"]

    run(scenario())


def test_a_moderators_shorten_on_a_ban_is_refused_before_the_form(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Faculty", source="in game", reason="x")
            number = part.appeals.file("7777", "Alice", ALICE, "Alice", "ban", [("What happened?", "x")])
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            card = card_of(huragok, number)
            _, token = fake.press(buttons_of(card)["Shorten it"], card, user="4442", roles=(MOD_ROLE,))
            assert "and up" in await said_back(fake, token)
            assert not any(c["token"] == token and c.get("type") == 9 for c in fake.callbacks)  # no form

    run(scenario())


# Quoting a word ------------------------------------------------------------------------------------------------------------------


def test_someone_quoting_the_word_in_a_report_isnt_struck(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        reports_on(bot_file(tmp_path))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(BRAVO, "Bravo", "!report Alice she keeps calling me a badword")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_reports"), timeout=15)
            await asyncio.sleep(0.5)
            assert not huragok.storage.query("SELECT 1 FROM automod_strikes")

    run(scenario())


def test_with_no_discord_a_report_line_is_just_chat(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The exemption's for telling staff. With nobody to tell, !report <word> mustn't be a way round the word list."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await rcon.chat(BRAVO, "Bravo", "!report Alice badword")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM automod_strikes"), timeout=15)

    run(scenario())


# Delivery ----------------------------------------------------------------------------------------------------------------------------


def test_the_sweep_doesnt_stack_a_card_while_the_first_is_waiting(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets ORDER BY number DESC LIMIT 1")[0]["number"])
            part.ctx.db.execute("UPDATE discord_tickets SET message = '', touched = ? WHERE number = ?", (time.time() - 3600, number))
            part.outbox.free_at[f"channel:{STAFF}"] = time.monotonic() + 3.0
            for _ in range(5):
                part.tickets.reposts.clear()
                part.tickets.sweep()
            assert len([job for job in part.outbox.jobs if job.what == f"ticket {number}"]) == 1

    run(scenario())


def test_a_card_that_wont_post_isnt_tried_every_minute(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets ORDER BY number DESC LIMIT 1")[0]["number"])
            part.ctx.db.execute("UPDATE discord_tickets SET message = '', touched = ? WHERE number = ?", (time.time() - 3600, number))
            part.outbox.free_at[f"channel:{STAFF}"] = time.monotonic() + 3.0
            part.tickets.sweep()
            first = part.tickets.reposts[number]
            part.tickets.sweep()
            assert part.tickets.reposts[number] == first  # not due yet

    run(scenario())


def test_a_channel_made_again_gets_its_cards_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Deleted and made again, and picked in Channels: the row kept the old channel's ID, for good."""
    async def scenario() -> None:
        path = bot_file(tmp_path)
        fake.channels.append(fake._channel("7900", "staff-again"))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await strike(huragok, rcon, fake)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets ORDER BY number DESC LIMIT 1")[0]["number"])
            set_channel(path, "staff", "7900", "staff-again")
            await part.reload(force=True)
            part.ctx.db.execute("UPDATE discord_tickets SET message = '', touched = ? WHERE number = ?", (time.time() - 3600, number))
            part.tickets.sweep()
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=15)
            assert str(part.tickets.get(number)["channel"]) == "7900"
            assert posts_in(fake, "7900")

    run(scenario())


def test_a_refused_channel_keeps_the_ticket_for_later(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Someone's been told staff have it, so it can't just be lost: the sweep posts it when it can."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.feeds.blocked[f"channel:{STAFF}"] = "Missing Access"
            number = part.tickets.open_by_hand("Mod", ALICE, "Alice", SERVER)
            assert number and part.tickets.get(number)["message"] == ""
            part.feeds.blocked.clear()
            part.ctx.db.execute("UPDATE discord_tickets SET touched = ? WHERE number = ?", (time.time() - 3600, number))
            part.tickets.sweep()
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=15)

    run(scenario())


def test_a_feeds_own_public_lets_its_tickets_through(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """!calladmin read that the staff had been told, and nothing was posted."""
    from testkit.fake_discord import PUBLIC_CHANNEL

    async def scenario() -> None:
        path = audit_file(tmp_path, ("feed", "calls"))
        set_channel(path, "reports", PUBLIC_CHANNEL, "servers")
        set_switch(path, ("feed", "calls"), "public", True)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            assert part._call(FIRST, ALICE, "Alice", "help") is True
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'calls-called'"), timeout=10)

    run(scenario())


def test_slow_again_after_being_healthy_is_news(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from huragok.core.events import Event

    async def scenario() -> None:
        audit_file(tmp_path, ("feed", "alerts"))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            feeds = part.feeds
            feeds.started[FIRST] = 0.0
            slow = Event("watchdog.health", FIRST, {"level": "slow", "reason": "ticks"})
            healthy = Event("watchdog.health", FIRST, {"level": "healthy"})
            feeds.handle(slow)
            assert FIRST in feeds.problems
            feeds.handle(healthy)
            assert FIRST not in feeds.problems  # so a later start doesn't say it's "back", and slow again gets posted

    run(scenario())


# Appeals -------------------------------------------------------------------------------------------------------------------------------


def test_a_stranger_with_only_a_name_cant_lock_the_player_out(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = audit_file(tmp_path, ("appeals",))
        set_switch(path, ("appeals",), "in_reason", "Appeal: /appeal {code}")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Faculty", source="in game", reason="x")
            number = part.appeals.file("7777", "Stranger", ALICE, "Alice", "ban", [("What happened?", "junk")])
            assert "with only a name" in str(part.tickets.get(number)["card"])
            part.appeals.decide(number, "denied", "Mod", why="no")
            assert part.appeals.next_allowed("7777", ALICE)  # they wait
            assert not part.appeals.next_allowed("8888", ALICE)  # anyone else doesn't
            again = part.appeals.file("8888", "Alice", ALICE, "Alice", "ban", [("What happened?", "it's me")], proved=True)
            assert again != number and "with their code" in str(part.tickets.get(again)["card"])
            part.appeals.decide(again, "denied", "Mod", why="no")
            assert part.appeals.next_allowed("9999", ALICE)  # with the code, it counts for the player

    run(scenario())


def test_a_code_is_kept_and_only_finds_who_it_was_given_to(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            code = part.appeals.code_of(ALICE)
            assert part.appeals.code_of(ALICE) == code and part.appeals.code_of(BRAVO) != code
            assert part.appeals.find_code(code.lower())[0] == ALICE
            assert part.appeals.find_code("000000")[0] == "" or code == "000000"

    run(scenario())


def test_someone_with_the_code_joins_a_strangers_appeal_and_is_who_hears(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Faculty", source="in game", reason="x")
            number = part.appeals.file("7777", "Stranger", ALICE, "Alice", "ban", [("What happened?", "junk")])
            assert part.appeals.file("8888", "Alice", ALICE, "Alice", "ban", [("What happened?", "it's me")], proved=True) == number
            assert any("it's me" in line for line in lines_on(huragok, number))
            assert str(part.appeals.of_ticket(number)["member"]) == "8888"

    run(scenario())


def test_a_new_appeal_on_a_decided_card_is_a_new_card_with_its_words(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Brought back to deal with something else, it was joined, and what they wrote was lost while they read "the staff have it"."""
    async def scenario() -> None:
        audit_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.vergil.moderation.record_seen("ban", FIRST, ALICE, "Alice", by="Faculty", source="in game", reason="x")
            first = part.appeals.file("7777", "Alice", ALICE, "Alice", "ban", [("What happened?", "one")])
            part.appeals.decide(first, "denied", "Mod", why="no")
            part.tickets.touched(first)  # someone acts on the card, so it's back
            second = part.appeals.file("7777", "Alice", ALICE, "Alice", "ban", [("What happened?", "two")])
            assert second != first
            assert "two" in str(part.tickets.get(second)["card"])

    run(scenario())


def test_an_accepted_appeal_takes_vergils_strike_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("appeals",))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.storage.record_action(kind="mute", server=FIRST, target_id=ALICE, target_name="Alice", by="Vergil", source="automod", reason="x")
            huragok.storage.execute("INSERT INTO automod_strikes (time, server, player_id, rule, grp, source, by, action, dry_run, ok) VALUES (?, ?, ?, 'r', 'r', 'chat', 'Vergil', 'mute', 0, 1)", (time.time(), FIRST, ALICE))
            part.appeals.strike_comes_off(ALICE, "Mod")
            assert huragok.storage.query("SELECT pardoned_by FROM automod_strikes")[0]["pardoned_by"] == "Mod"

    run(scenario())


# Slash commands and the audit --------------------------------------------------------------------------------------------------------------


def test_a_refused_command_isnt_on_the_audit_log(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("feed", "audit"), ("commands",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            _, token = fake.command("kick", user="4442", roles=(MOD_ROLE,), player="Nobodyatall", reason="spam", server=FIRST)
            await said_back(fake, token)
            await asyncio.sleep(0.5)
            assert not any("Nobodyatall" in str(post.get("content")) + str(post.get("embeds")) for post in fake.posted)

    run(scenario())


def test_the_audit_log_cuts_a_players_whole_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("feed", "audit"), ("commands",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            _, token = fake.command("allowvote", user="4441", roles=(ADMIN_ROLE,), player=ALICE, server=FIRST)
            await said_back(fake, token)
            await wait_for(lambda: any(ALICE[:8] in str(post.get("content")) + str(post.get("embeds")) for post in fake.posted), timeout=15)
            assert not any(ALICE in str(post.get("content")) + str(post.get("embeds")) for post in fake.posted)

    run(scenario())


def test_answers_name_commands_as_they_are_here(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from huragok.discordbot.slash import _as_named_here

    async def scenario() -> None:
        audit_file(tmp_path, ("commands",))
        (tmp_path / "automod.txt").write_text(WORD_LIST, encoding="utf-8")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            said = _as_named_here(part, "/pardon @number lets one back in now. /rules <line> shows one. https://example.com/pardon stays")
            assert "/letback @number" in said and "/bannedwords <line>" in said and "example.com/pardon" in said

    run(scenario())


def test_restartserver_has_no_for_good(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("commands",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            _, token = fake.command("restartserver", user=ADMIN_USER, roles=(ADMIN_ROLE,), time="perm", server=FIRST)
            assert "Couldn't read" in await said_back(fake, token)

    run(scenario())


def test_public_and_hidden_take_either_name(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = audit_file(tmp_path, ("commands",))
        set_switch(path, ("commands",), "hidden", ["bannedwords"])
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            from huragok.discordbot.slash import commands_for

            assert "bannedwords" not in {command.name for command in commands_for(part)}

    run(scenario())


# The file -------------------------------------------------------------------------------------------------------------------------------------


def test_a_word_where_a_number_goes_is_said_and_left_out(tmp_path: Path) -> None:
    """Every other time in the product is written like "30m", so this is an easy slip: and each sweep then raised, until the part switched itself off."""
    path = tmp_path / "discord.toml"
    path.write_text('[messages]\nvergil-strike = { channel = "staff", colour = "gold", post = true, ping = "", stale_after_hours = "12h", remove_after_minutes = "30m", closes_to = 5 }\n', encoding="utf-8")
    config = load(path)
    line = config.section("messages")["vergil-strike"]
    assert "stale_after_hours" not in line and "remove_after_minutes" not in line and line["closes_to"] == "5"
    assert sum("is a number" in said for said in config.warnings) == 2


def test_a_scoped_rank_written_wrong_cant_stop_anything(tmp_path: Path) -> None:
    from huragok.discordbot.ranks import Someone, rank_of

    path = tmp_path / "discord.toml"
    path.write_text('[[ranks.scoped]]\nrole = "5"\nrank = "admin"\nservers = 3\n[[ranks.scoped]]\nrole = "6"\nrank = "admin"\nservers = "One"\n', encoding="utf-8")
    config = load(path)
    assert any("servers should be a list" in said for said in config.warnings)
    assert config.section("ranks")["scoped"][1]["servers"] == ["One"]
    assert rank_of(config, Someone("1", {"5": "x", "6": "y"}, False, False), "One") == "admin"
    assert rank_of(config, Someone("1", {"5": "x"}, False, False), "One") == "player"
    other = tmp_path / "other.toml"
    other.write_text("[ranks]\nscoped = 5\n", encoding="utf-8")
    assert any("[ranks] scoped" in said for said in load(other).warnings)


def test_a_wrong_name_in_a_list_is_left_out_as_it_says(tmp_path: Path) -> None:
    """A list of sources with a wrong one in it used to stay as it was, and leave out every card."""
    path = tmp_path / "discord.toml"
    path.write_text('[feed.moderation]\nsources = ["nonsense"]\n', encoding="utf-8")
    config = load(path)
    assert config.section("feed", "moderation")["sources"] == ["*"]
    assert any("nonsense" in said for said in config.warnings)
    path.write_text('[feed.moderation]\nsources = ["discord", "nonsense"]\n', encoding="utf-8")
    assert load(path).section("feed", "moderation")["sources"] == ["discord"]


def test_a_press_from_discord_is_a_source_of_its_own(tmp_path: Path) -> None:
    from huragok.core.events import Event
    from huragok.discordbot import cards

    config = load(tmp_path / "none.toml")
    typed = cards.moderation(config, Event("moderation.action", SERVER, {"kind": "kick", "target_id": ALICE, "target_name": "Alice", "by": "Mod", "by_id": MOD, "source": "commands", "ok": True}))
    pressed = cards.moderation(config, Event("moderation.action", SERVER, {"kind": "kick", "target_id": ALICE, "target_name": "Alice", "by": "Mod", "by_id": "discord:1", "source": "commands", "ok": True}))
    assert typed is not None and pressed is not None and typed.source == "!" and pressed.source == "discord"


def test_who_acted_is_on_the_record_by_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A display name can change, or be someone else's. With shown_as naming nobody, nothing else could say who it was."""
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("commands",), "shown_as", "a moderator")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            post = await strike(huragok, rcon, fake)
            await fill(fake, "Kick", post, {"How long": "1h", THE_REASON: "spam"}, **ADMIN)
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM actions WHERE by = 'a moderator'"), timeout=15)
            row = huragok.storage.query("SELECT by_id FROM actions WHERE by = 'a moderator'")[0]
            assert row["by_id"] == "discord:4441"

    run(scenario())


def test_start_over_keeps_numbers_going_and_copes_with_a_broken_file(tmp_path: Path) -> None:
    from huragok.core.storage import PartStore, Storage
    from huragok.discordbot.startover import start_over
    from huragok.discordbot.status import SCHEMA

    database = tmp_path / "huragok.sqlite3"
    add = ("INSERT INTO discord_tickets (opened, touched, kind, owner, state, player_id, player_name, server, problem, card, persona, open_in, log_in, place, channel, message, thread) "
           "VALUES (0, 0, 'k', 'people', 'open', 'a', 'A', 'S', '', '{}', 'vergil', 'staff', 'logs', 'staff', '1', '', '')")
    db = Storage(database)
    PartStore(db, "discord").migrate(SCHEMA)
    first = db.execute(add).lastrowid
    db.close()
    broken = tmp_path / "discord.toml"
    broken.write_text("this isn't [ toml", encoding="utf-8")
    said = start_over(broken, database)
    assert load(broken).error == ""  # the likeliest reason for wanting it
    assert any("rows" in line for line in said)
    db = Storage(database)
    assert db.execute(add).lastrowid > first  # an old card's buttons can't reach someone else's ticket
    db.close()


def test_a_press_on_a_card_that_isnt_the_tickets_is_refused(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Two Huragoks on one token, each with its own ticket 5: the press mustn't act on the wrong one's player."""
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            number = part.tickets.open_by_hand("Mod", ALICE, "Alice", FIRST)
            await wait_for(lambda: part.tickets.get(number)["message"], timeout=10)
            card = card_of(huragok, number)
            elsewhere = {**card, "id": "424242"}
            _, token = fake.press(buttons_of(card)["Whois"], elsewhere, user=ADMIN_USER, roles=(ADMIN_ROLE,))
            assert "isn't the one" in await said_back(fake, token)

    run(scenario())


def test_set_message_is_still_how_a_line_changes(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    set_message(path, "notify-told", ping="123")
    assert load(path).section("messages")["notify-told"]["ping"] == "123"
