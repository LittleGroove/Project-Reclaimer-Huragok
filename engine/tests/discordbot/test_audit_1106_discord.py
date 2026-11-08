"""The audit of 6 November 2026, moderation from Discord (plans/bug-audit-2026-11-06.md, area 2)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO
from test_audit_2026_11 import FIRST, SCOPED_ROLE, SECOND, scoped, scoped_roles
from test_tickets import ADMIN_ROLE, MOD_ROLE, MOD_USER, STAFF, buttons_of, fake, form_of, posts_in, said_back, up  # noqa: F401
from test_word_list_reports import commands_file
from testkit.fake_discord import FakeDiscord
from testkit.running import SERVER, fleet, running, wait_for

STRIKE_ROW = ("INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, action, duration, kick, expires, dry_run, ok, waiting) "
              "VALUES (?, ?, ?, ?, ?, ?, 'chat', 'x', 'Vergil', 'mute', '10m', NULL, 0, 0, 1, 0)")


def names(fake: FakeDiscord) -> set[str]:
    return {command["name"] for commands in fake.commands.values() for command in commands}


def struck(huragok: Any, when: float, server: str, player_id: str, name: str, rule: str) -> None:
    huragok.storage.execute(STRIKE_ROW, (when, server, player_id, name, rule, rule))


def latest(fake: FakeDiscord, post: dict[str, Any]) -> dict[str, Any]:
    """A card as last edited: the fake keeps edits apart from what was posted."""
    edits = [edit for edit in fake.edits if edit["message"] == post["id"] and "components" in edit]
    return {**post, **edits[-1]} if edits else post


def standing(huragok: Any) -> dict[str, int]:
    return {str(row["grp"]): int(row["n"]) for row in huragok.storage.query("SELECT grp, COUNT(*) AS n FROM automod_strikes WHERE pardoned IS NULL GROUP BY grp")}


async def offered(fake: FakeDiscord, command: str, typed: str = "", **others: Any) -> list[dict[str, Any]]:
    _, token = fake.typing_command(command, "rule", typed, **others)
    await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
    return next(c for c in fake.callbacks if c.get("token") == token)["data"]["choices"]


def test_a_name_with_a_space_is_one_player_never_a_player_and_a_reason(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.1: `/kick player:"Bob Smyth"` with only Bob on kicked Bob, the reason "Smyth"."""

    async def scenario() -> None:
        commands_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await wait_for(lambda: "kick" in names(fake), timeout=15)
            await rcon.join(3, "Bob", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", user=MOD_USER, roles=(MOD_ROLE,), player="Bob Smyth")
            assert "Bob Smyth" in await said_back(fake, token)
            await asyncio.sleep(1)
            assert not rcon.named("kick") and not huragok.storage.query("SELECT 1 FROM actions WHERE kind = 'kick'")

    run(scenario())


def test_a_number_in_a_command_with_no_server_box_isnt_guessed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.2: /clearstrikes and /promote have no server box: `#3` was the first server's #3, not the one meant."""

    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text("badword | 1d | | | slurs\n", encoding="utf-8")
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await wait_for(lambda: "clearstrikes" in names(fake), timeout=15)
            await consoles[FIRST].join(3, "Alice", ALICE)
            await consoles[SECOND].join(3, "Bravo", BRAVO)
            await wait_for(lambda: huragok.superintendent.states[FIRST].player_by_id(ALICE) and huragok.superintendent.states[SECOND].player_by_id(BRAVO), timeout=10)
            for who, name in ((ALICE, "Alice"), (BRAVO, "Bravo")):
                struck(huragok, huragok.storage.query("SELECT strftime('%s','now') AS t")[0]["t"], SECOND, who, name, "slurs")
            for command in ("clearstrikes", "promote"):
                _, token = fake.command(command, player="#3")
                assert "different on each server" in await said_back(fake, token), command
            assert standing(huragok) == {"slurs": 2}

    run(scenario())


def test_pardon_strike_on_a_card_whose_list_was_taken_off_clears_only_that_list(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.3: the card's list taken off the word list: its name was read as the reason, and every strike on every list cleared."""

    async def scenario() -> None:
        commands_file(tmp_path)
        words = tmp_path / "automod.txt"
        words.write_text("badword | 1d | | | slurs\nmeanword | 10m, 1h, 1d | 3 | 5 | insults\n", encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            automod = huragok.runners["automod"].part
            for _ in range(2):
                struck(huragok, 1000.0, SERVER, ALICE, "Alice", "insults")
            await rcon.chat(ALICE, "Alice", "badword")
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            card = posts_in(fake, STAFF)[-1]
            words.write_text("meanword | 10m, 1h, 1d | 3 | 5 | insults\n", encoding="utf-8")
            await automod.reload_if_changed()
            await wait_for(lambda: "slurs" not in automod.by_name, timeout=20)
            _, opened = fake.press(buttons_of(card)["Pardon strike"], card)
            form = await form_of(fake, opened)
            assert form["components"][0].get("description") == "It clears every strike on slurs, and lifts what they did"
            edits = len(fake.original_edits)
            fake.fill_in(opened, card, {"Why": "misheard"})
            await wait_for(lambda: "slurs" not in standing(huragok), timeout=15)
            assert standing(huragok) == {"insults": 2}
            # The card is shown again after the pardon: ended before that, its press lost its session (CI's runner).
            await wait_for(lambda: len(fake.original_edits) > edits, timeout=15)

    run(scenario())


def test_a_card_that_heard_a_word_pardons_only_the_strikes_that_joined_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.4: a heard card offered Pardon strike for yesterday's strike, and cleared it."""

    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text("badword | 1d | | | slurs\nmeanword | 10m | | | insults\nrudeword | | | | rude\n", encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            struck(huragok, 1000.0, SERVER, ALICE, "Alice", "slurs")  # yesterday's
            await rcon.chat(ALICE, "Alice", "rudeword")
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            assert "Pardon strike" not in buttons_of(posts_in(fake, STAFF)[-1])
            await rcon.chat(ALICE, "Alice", "meanword")  # a strike joins the card
            await wait_for(lambda: "insults" in standing(huragok), timeout=15)
            await wait_for(lambda: "Pardon strike" in buttons_of(latest(fake, posts_in(fake, STAFF)[-1])), timeout=15)
            card = latest(fake, posts_in(fake, STAFF)[-1])
            _, opened = fake.press(buttons_of(card)["Pardon strike"], card)
            form = await form_of(fake, opened)
            assert form["components"][0].get("description") == "It clears every strike on insults, and lifts what they did"
            fake.fill_in(opened, card, {"Why": "a joke between friends"})
            await wait_for(lambda: "insults" not in standing(huragok), timeout=15)
            assert standing(huragok) == {"slurs": 1}

    run(scenario())


def test_name_and_all_are_offered_however_many_lists(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.9: with 25 lists, name and all were cut off the end."""

    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text("".join(f"word{n} | 1d | | | list{n:02}\n" for n in range(30)), encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            await wait_for(lambda: "strike" in names(fake), timeout=15)
            strike = [c["value"] for c in await offered(fake, "strike")]
            clear = [c["value"] for c in await offered(fake, "clearstrikes")]
            assert len(strike) == len(clear) == 25 and strike[-1] == "name" and clear[-1] == "all"
            assert [c["value"] for c in await offered(fake, "strike", "list29")] == ["list29"]

    run(scenario())


def test_changebanword_keeps_what_its_left_empty(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.6 and the host's call (6 November): a box left empty keeps that part, none clears it; all three empty is
    refused. It was the whole new ladder: `bans: 5` alone took the mutes and kicks away, and no box at all made the list
    only report, in one press."""

    async def scenario() -> None:
        commands_file(tmp_path)
        words = tmp_path / "automod.txt"
        words.write_text("badword | 1d, 3d | 2, 1h | 4 | slurs\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            await wait_for(lambda: "changebanword" in names(fake), timeout=15)
            _, token = fake.command("changebanword", listname="slurs")
            assert "Nothing to change" in await said_back(fake, token)
            assert words.read_text(encoding="utf-8") == "badword | 1d, 3d | 2, 1h | 4 | slurs\n"
            _, token = fake.command("changebanword", listname="slurs", bans="5")
            await said_back(fake, token)
            assert words.read_text(encoding="utf-8") == "badword | 1d, 3d | 2, 1h | 5 | slurs\n"
            _, token = fake.command("changebanword", listname="slur", kicks="none")
            await said_back(fake, token)
            assert words.read_text(encoding="utf-8") == "badword | 1d, 3d | | 5 | slurs\n"
            _, token = fake.command("changebanword", listname="slurs", mutes="none", bans="none")
            await said_back(fake, token)
            assert huragok.runners["automod"].part.by_name["slurs"].reports  # every part none: it only reports, as asked

    run(scenario())


def test_yes_pressed_twice_sends_it_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.7: a double click on "Are you sure?" ran the command twice."""

    async def scenario() -> None:
        commands_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await wait_for(lambda: "endgame" in names(fake), timeout=15)
            opened, token = fake.command("endgame", server=SERVER)
            await wait_for(lambda: any(c["token"] == token and c.get("type") == 4 for c in fake.callbacks), timeout=10)
            asked = next(c for c in fake.callbacks if c["token"] == token)["data"]
            yes = next(item["custom_id"] for row in asked["components"] for item in row["components"] if item["label"] == "Yes")
            fake.pick(opened, yes, 2, [])
            fake.pick(opened, yes, 2, [])
            await wait_for(lambda: rcon.named("endgame") or rcon.named("end_game"), timeout=15)
            await asyncio.sleep(2)
            assert len(rcon.named("endgame")) + len(rcon.named("end_game")) == 1

    run(scenario())


def test_strikes_offered_on_a_dry_run_server_count_as_the_strike_would(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.8: in dry run, the suggestion counted real strikes and the strike dry ones."""

    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text("badword | 1d, 3d | 2, 1h | 4 | slurs\n", encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ndry_run = true\ncooldown_seconds = 0\n") as (huragok, rcon, _):
            await up(huragok)
            await wait_for(lambda: "strike" in names(fake), timeout=15)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            for _ in range(2):
                await rcon.chat(ALICE, "Alice", "badword")
                await asyncio.sleep(0.3)
            await wait_for(lambda: len(huragok.storage.query("SELECT 1 FROM automod_strikes")) == 2, timeout=15)
            assert (await offered(fake, "strike", player=ALICE))[0]["name"] == "slurs · Strike 3: kicked for 1 hour"
            _, token = fake.command("strike", player=ALICE, rule="slurs")
            assert "slurs strike 3 of 4, kicked for 1 hour" in await said_back(fake, token)

    run(scenario())


def test_a_rule_box_that_isnt_a_list_is_refused_never_a_reason(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The known one, from Discord: /clearstrikes rule:slrus cleared every strike, with the reason "slrus". slur is slurs, as /banword takes it."""

    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text("badword | 1d | | | slurs\nmeanword | 10m | | | insults\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await wait_for(lambda: "clearstrikes" in names(fake), timeout=15)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            for rule in ("slurs", "insults"):
                struck(huragok, 1000.0, SERVER, ALICE, "Alice", rule)
            _, token = fake.command("clearstrikes", player=ALICE, rule="slrus", reason="misheard")
            said = await said_back(fake, token)
            assert "no list called slrus" in said and "Did you mean slurs?" in said
            _, token = fake.command("strike", player=ALICE, rule="slrus")
            assert "no list called slrus to strike on" in await said_back(fake, token)
            assert standing(huragok) == {"insults": 1, "slurs": 1}
            _, token = fake.command("clearstrikes", player=ALICE, rule="slur", reason="misheard")
            assert "1 strike cleared (slurs)" in await said_back(fake, token)
            assert standing(huragok) == {"insults": 1}

    run(scenario())


def test_the_cards_strike_on_a_list_taken_off_says_so_and_shows_no_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """2.10: the list taken off while the form was open: the refusal read the list's name as part of the player's, with their whole ID."""
    from test_tickets import strike as struck_card

    async def scenario() -> None:
        path = commands_file(tmp_path)
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('buttons = ["mute", "kick", "ban", "whois", "note"]', 'buttons = ["mute", "kick", "ban", "strike", "whois", "note"]'), encoding="utf-8")
        words = tmp_path / "automod.txt"
        words.write_text("badword | 1d | | | slurs\nmeanword | 10m | | | insults\n", encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (huragok, rcon, _):
            await up(huragok)
            card = await struck_card(huragok, rcon, fake)
            _, opened = fake.press(buttons_of(card)["Strike"], card, user=MOD_USER, roles=(MOD_ROLE,))
            await form_of(fake, opened)
            words.write_text("meanword | 10m | | | insults\n", encoding="utf-8")
            automod = huragok.runners["automod"].part
            await automod.reload_if_changed()
            await wait_for(lambda: "slurs" not in automod.by_name, timeout=20)
            before = len(huragok.storage.query("SELECT 1 FROM automod_strikes"))
            _, token = fake.fill_in(opened, card, {"Which rule": "slurs", "Why (they'll read this)": "said a bad name in voice"}, user=MOD_USER, roles=(MOD_ROLE,))
            said = await said_back(fake, token)
            assert "slurs list isn't one a strike can be given on now" in said and "insults" in said and "a" * 20 not in said and ALICE[:20] not in said
            assert len(huragok.storage.query("SELECT 1 FROM automod_strikes")) == before

    run(scenario())


def test_clearstrikes_with_only_a_reason_clears_everything(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """In game a reason for clearing every strike comes after `all` now (the host's call, 6 November): here the reason
    has a box of its own, so an empty rule box with a reason is all."""

    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text("badword | 1d | | | slurs\nmeanword | 10m | | | insults\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await wait_for(lambda: "clearstrikes" in names(fake), timeout=15)
            for rule in ("slurs", "insults"):
                struck(huragok, 1000.0, SERVER, ALICE, "Alice", rule)
            _, token = fake.command("clearstrikes", player=ALICE, reason="a misunderstanding")
            assert "2 strikes cleared" in await said_back(fake, token)
            assert standing(huragok) == {}

    run(scenario())


def test_a_strike_that_bans_from_a_moderator_goes_to_the_admins_and_a_one_server_rank_cant_remove(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host's calls (6 November): only Huragok and admins ban, so a moderator's /strike whose next step is the ban
    is a request for the admins, with its card; and a strike that would remove someone or ban reaches every server, so
    a rank held on one server can't give it (one that mutes, it can)."""
    from test_tickets import bot_file as tickets_file

    from huragok.discordbot import cards

    async def scenario() -> None:
        scoped(tickets_file(tmp_path))
        scoped_roles(fake)
        (tmp_path / "automod.txt").write_text("badword | 1d | | 1 | slurs\nmeanword | | 0, 1d | | kicks\n", encoding="utf-8")
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            fake_events: list[Any] = []
            huragok.bus.subscribe("commands.request", fake_events.append)
            await up(huragok)
            await wait_for(lambda: "strike" in names(fake), timeout=15)
            await consoles[FIRST].join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[FIRST].player_by_id(ALICE), timeout=10)
            # A one-server moderator: a mute is theirs to give; a kick for a day (every server's) isn't.
            _, token = fake.command("strike", user=MOD_USER, roles=(SCOPED_ROLE,), player=ALICE, rule="slurs", server=FIRST)
            assert "slurs strike 1 of 1, muted for 1 day" in await said_back(fake, token)
            _, token = fake.command("strike", user=MOD_USER, roles=(SCOPED_ROLE,), player=ALICE, rule="kicks", server=FIRST)
            assert "reaches every server" in await said_back(fake, token)
            # A moderator everywhere: the ban for good goes to the admins.
            _, token = fake.command("strike", user=MOD_USER, roles=(MOD_ROLE,), player=ALICE, rule="slurs")
            said = await said_back(fake, token)
            assert "Sent to the admins as #1" in said, said
            assert not consoles[FIRST].named("ban") and not consoles[SECOND].named("ban")
            asked = next(e for e in fake_events if e.name == "commands.request")
            card = cards.request(huragok.runners["discord"].part.file, asked)  # its card, where [feed.requests] is on
            assert "asks an admin, #1: !strike Alice slurs" in card.line and "the ban for good, which only an admin gives" in card.line

    run(scenario())
