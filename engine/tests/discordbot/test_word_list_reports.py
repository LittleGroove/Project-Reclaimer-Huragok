"""The word list from Discord, and its lists that only report (the host's design, 2 November).

A line with nothing in its mutes, kicks and ban (`theword | | | | badwords`) does nothing in game: each time, a ticket for
the staff, who decide, as how a word's said matters more than the word. A name with one of its words isn't kicked: a
ticket asks whether to ban the name or allow it, and staff in that game are told, to them alone. The word list's commands
take a box for each part, filled in any order, and the lists come whole, over as many messages as it takes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, MOD
from test_tickets import LOGS, STAFF, bot_file, buttons_of, fake, posts_in, row_of, said_back, up  # noqa: F401
from testkit.fake_discord import ADMIN_ROLE, MOD_ROLE, FakeDiscord
from testkit.running import SERVER, running, tells_to, wait_for

from huragok.discordbot.config import set_switch
from huragok.discordbot.slash import MOST_SHOWN, pages

WORDS = "badword | | 1h, 1d | 3 | slurs\nrudeword, -rudewordfan | | | | rude\n"
EPHEMERAL = 64


def commands_file(root: Path) -> Path:
    path = bot_file(root)
    set_switch(path, ("commands",), "enabled", True)
    return path


def answers(fake: FakeDiscord, token: str) -> list[dict[str, Any]]:
    return [followup for followup in fake.followups if followup["token"] == token]


def nothing_in_game(rcon: Any) -> bool:
    return not any(rcon.named(kind) for kind in ("kick", "ban", "mute", "say"))


# In game ------------------------------------------------------------------------------------------------------------


def test_a_word_on_a_list_that_only_reports_is_a_ticket_and_nothing_else(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (huragok, rcon, seen):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await rcon.chat(ALICE, "Alice", "what a rudeword thing to say")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-heard'"), timeout=15)
            assert nothing_in_game(rcon) and not tells_to(rcon, ALICE)  # nobody in game knows
            assert not huragok.storage.query("SELECT 1 FROM automod_strikes")  # and it's no strike
            heard = [event for event in seen if event.name == "automod.heard"]
            assert heard and heard[0]["rule"] == "rude" and heard[0]["evidence"] == "what a rudeword thing to say"
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            card = posts_in(fake, STAFF)[-1]
            text = str(card["embeds"][0])
            assert "only reports" in text and "what a rudeword thing to say" in text
            pressable = buttons_of(card)
            assert {"Mute", "Kick", "Ban", "Whois"} <= set(pressable) and "Pardon strike" not in pressable  # nothing to pardon
            # Said again, and a strike beside it: lines on the one live card a player has.
            await rcon.chat(ALICE, "Alice", "rudeword")
            await wait_for(lambda: len([event for event in seen if event.name == "automod.heard"]) == 2, timeout=15)
            await rcon.chat(ALICE, "Alice", "badword")
            await wait_for(lambda: rcon.named("ban"), timeout=15)  # the slurs line's 1st: out 1 hour
            await wait_for(lambda: len(huragok.storage.query("SELECT 1 FROM discord_ticket_lines")) >= 2, timeout=15)
            assert len(huragok.storage.query("SELECT 1 FROM discord_tickets WHERE player_id = ?", (ALICE,))) == 1

    run(scenario())


def test_a_strike_on_a_list_that_only_reports_does_nothing_and_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await rcon.join(2, "Mod", MOD)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(MOD, "Mod", "!strike Alice rude")
            await wait_for(lambda: any("only reports" in text for text in tells_to(rcon, MOD)), timeout=15)
            assert nothing_in_game(rcon) and not huragok.storage.query("SELECT 1 FROM automod_strikes")
            assert huragok.runners["automod"].part.strike_rules() == ["slurs"]  # what the Strike button offers

    run(scenario())


def test_a_name_with_a_word_that_only_reports_asks_the_staff(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Nobody's kicked. A ticket asks: Ban the name, or Allow name, and either closes it. Staff on that server are told, to
    them alone; the player isn't."""
    async def scenario() -> None:
        bot_file(tmp_path)
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, seen):
            await up(huragok)
            await rcon.join(2, "Mod", MOD)
            await rcon.join(4, "xXRudewordXx", BRAVO)
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-name-asked'"), timeout=15)
            # In lines game chat takes: the last of them too, which a slow runner (CI's Windows) hadn't had yet
            await wait_for(lambda: "bans the name" in " ".join(tells_to(rcon, MOD)), timeout=15)
            told = " ".join(tells_to(rcon, MOD))
            assert "xXRudewordXx joined with a name" in told and "!banname #4 bans the name" in told
            assert not rcon.named("kick") and not tells_to(rcon, BRAVO) and not rcon.named("say")
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            card = posts_in(fake, STAFF)[-1]
            assert list(buttons_of(card)) == ["Ban the name", "Allow name"]  # those two, and nothing else
            # One card a name, however often they come and go.
            await rcon.leave(4)
            await rcon.join(4, "xXRudewordXx", BRAVO)
            await huragok.runners["automod"].part.check_names()
            assert len(huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-name-asked'")) == 1
            # A moderator can't answer it: banning a name is for admins, as /banname is.
            _, token = fake.press(buttons_of(card)["Ban the name"], card, user="3334", roles=(MOD_ROLE,))
            assert "admins" in await said_back(fake, token)
            assert row_of(huragok)["state"] in ("open", "active")
            # An admin bans it: in names.txt, they're kicked, and the card's done.
            _, token = fake.press(buttons_of(card)["Ban the name"], card, user="3335", roles=(ADMIN_ROLE,))
            names = tmp_path / "names.txt"
            await wait_for(lambda: names.exists() and "xXRudewordXx" in names.read_text(encoding="utf-8"), timeout=15)
            await wait_for(lambda: any(kick["args"][0] == BRAVO for kick in rcon.named("kick")), timeout=15)
            await wait_for(lambda: row_of(huragok)["state"] == "closed", timeout=15)
            assert any("banned the name" in str(line["text"]) for line in huragok.storage.query("SELECT text FROM discord_ticket_lines"))

    run(scenario())


def test_a_name_the_staff_allow_isnt_asked_about_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(4, "RudewordRanger", BRAVO)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            card = posts_in(fake, STAFF)[-1]
            fake.press(buttons_of(card)["Allow name"], card)
            await wait_for(lambda: row_of(huragok)["state"] == "closed", timeout=15)
            assert huragok.storage.query("SELECT name, by FROM automod_names_allowed")[0]["name"] == "RudewordRanger"
            automod = huragok.runners["automod"].part
            automod.names_asked.clear()  # as after a restart
            await rcon.leave(4)
            await rcon.join(4, "RudewordRanger", BRAVO)
            await automod.check_names()
            assert len(huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-name-asked'")) == 1
            assert not rcon.named("kick")

    run(scenario())


def test_a_name_banned_another_way_closes_its_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(4, "Rudeword", BRAVO)
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE kind = 'vergil-name-asked'"), timeout=15)
            await rcon.join(1, "Faculty", "a" * 64)
            await rcon.chat("a" * 64, "Faculty", "!banname Rudeword")
            await wait_for(lambda: row_of(huragok)["state"] == "closed", timeout=15)

    run(scenario())


# From Discord -------------------------------------------------------------------------------------------------------


def test_banword_has_a_box_for_each_part_in_any_order(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        commands_file(tmp_path)
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            await wait_for(lambda: "banword" in {command["name"] for commands in fake.commands.values() for command in commands}, timeout=15)
            commands = {command["name"]: command for commands in fake.commands.values() for command in commands}
            assert [option["name"] for option in commands["banword"]["options"]] == ["words", "mutes", "kicks", "bans", "listname"]
            assert [option["name"] for option in commands["changebanword"]["options"]] == ["listname", "mutes", "kicks", "bans"]
            assert [option["name"] for option in commands["unbanword"]["options"]] == ["words", "listname"]
            assert {"bannedwords", "bannednames"} <= set(commands) and not {"wordlist", "bannames", "rules"} & set(commands)
            words = tmp_path / "automod.txt"
            # The host's example, the boxes in another order.
            _, token = fake.command("banword", listname="badwords", bans="4", words="hello, friend", kicks="3d, 4d", mutes="1d, 3d, 2")
            assert "Added to automod.txt line 3 (badwords): hello, friend" in await said_back(fake, token)
            assert "hello, friend | 1d, 3d, 2 | 2, 3d, 4d | 4 | badwords" in words.read_text(encoding="utf-8")  # the kicks once every mute time is used: written so
            # A list with nothing in the steps only reports.
            _, token = fake.command("banword", words="theword", listname="watchwords")
            assert "a ticket for the staff, nothing in game" in await said_back(fake, token)
            assert "theword | | | | watchwords" in words.read_text(encoding="utf-8")
            # Words added to a list there, by its name with no steps (slur is slurs).
            _, token = fake.command("banword", words="otherword", listname="slur")
            assert "(slurs): otherword" in await said_back(fake, token)
            # No list: which, and nothing's written.
            before = words.read_text(encoding="utf-8")
            _, token = fake.command("banword", words="newword")
            said = await said_back(fake, token)
            assert said.startswith("Which list?") and "slurs, rude, badwords, watchwords" in said and words.read_text(encoding="utf-8") == before
            _, token = fake.command("banword", words="a | b", listname="x")
            assert "can't have a | in it" in await said_back(fake, token)
            # New steps, and a list's words off, and a whole list after asking.
            _, token = fake.command("changebanword", kicks="0", listname="watchwords")
            assert "The watchwords line now: every time: kicked" in await said_back(fake, token)
            _, token = fake.command("unbanword", words="hello, -rudewordfan")
            assert "Taken off automod.txt: -rudewordfan (rude), hello (badwords)" in await said_back(fake, token)
            opened, token = fake.command("unbanword", listname="badwords")
            await wait_for(lambda: any(c["token"] == token and c.get("type") == 4 for c in fake.callbacks), timeout=10)
            assert "badwords" in words.read_text(encoding="utf-8")  # asked first
            asked = next(c for c in fake.callbacks if c["token"] == token)["data"]
            yes = next(item["custom_id"] for row in asked["components"] for item in row["components"] if item["label"] == "Yes")
            fake.pick(opened, yes, 2, [])
            await wait_for(lambda: "badwords" not in words.read_text(encoding="utf-8"), timeout=15)
            assert "friend" not in words.read_text(encoding="utf-8")

    run(scenario())


def test_the_lists_come_whole_and_private_over_as_many_messages_as_it_takes(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        commands_file(tmp_path)
        many = [f"longwordnumber{n:04}" for n in range(400)]
        (tmp_path / "automod.txt").write_text(WORDS + ", ".join(many) + " | 1d | | | many\n", encoding="utf-8")
        (tmp_path / "names.txt").write_text("".join(f"Banned Name {n:03}\n" for n in range(300)), encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            await wait_for(lambda: "bannedwords" in {command["name"] for commands in fake.commands.values() for command in commands}, timeout=15)
            _, token = fake.command("bannedwords")
            await wait_for(lambda: len(answers(fake, token)) > 3, timeout=30)
            await wait_for(lambda: "kicked as they join" in str(answers(fake, token)[-1].get("content")), timeout=30)
            sent = answers(fake, token)
            assert all(len(str(one["content"])) <= 2000 and int(one.get("flags") or 0) & EPHEMERAL for one in sent)
            text = "\n".join(str(one["content"]) for one in sent)
            assert all(word in text for word in many) and "rudeword, -rudewordfan" in text and "Only reports" in text
            assert "1st: kicked for 1 hour" in text and not any(one.get("attachments") for one in sent)  # never a file
            _, token = fake.command("bannednames")
            await wait_for(lambda: answers(fake, token) and "unbanname @number" in "\n".join(str(one["content"]) for one in answers(fake, token)), timeout=30)
            text = "\n".join(str(one["content"]) for one in answers(fake, token))
            assert all(f"Banned Name {n:03}" in text for n in range(300)) and "300 banned names" in text

    run(scenario())


def test_pages_break_between_lines_and_long_lines_between_words() -> None:
    text = "head\n" + ", ".join(f"word{n}" for n in range(1000)) + "\nlast line"
    made = pages(text)
    assert all(len(page) <= MOST_SHOWN for page in made) and made[0] == "head"
    assert "".join(made).replace("\n", "").replace("  ", " ").count("word") == 1000 and made[-1].endswith("last line")
    assert pages("short") == ["short"]
