"""Notes and flags on a player: /note and /flag, who may, what they show on, the commands coming and going with the switch."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, FACULTY
from test_bot_running import OTHER, answer_text
from testkit.fake_discord import GUILD, MOD_ROLE, FACULTY_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, tells_to, wait_for

from huragok.core.storage import Storage
from huragok.discordbot.config import add_faculty, load, make_file, set_channel, set_guild, set_mode, set_switch, set_token

PROBATION_ROLE = "5103"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.roles.append(FakeDiscord._role(PROBATION_ROLE, "SPR-Probation", 1, "0"))
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *, notes: bool = True, watch: bool = True, moderation: bool = False) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_faculty(path, FACULTY_USER, "the host")
    for name in ("staff", "logs"):
        set_channel(path, name, STAFF_CHANNEL, "admin-log")
    set_switch(path, ("notes",), "enabled", notes)
    set_switch(path, ("feed", "watch"), "enabled", watch)
    set_switch(path, ("feed", "moderation"), "enabled", moderation)
    text = path.read_text(encoding="utf-8").replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]').replace("probationary_moderator = []", f'probationary_moderator = ["{PROBATION_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


def cards_of(fake: FakeDiscord) -> list[dict[str, Any]]:
    return [card for post in fake.posted for card in post.get("embeds", [])]


# Keeping them --------------------------------------------------------------------------------------------------------


def test_notes_last_until_their_time_and_go_with_their_number(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite3")
    first = storage.add_note(ALICE, "seen spawn killing", "Mod")
    second = storage.add_note(ALICE.upper(), "said sorry", "Admin")
    storage.add_note(ALICE, "long gone", "Mod", expires=time.time() - 1)
    storage.add_note(BRAVO, "someone else's", "Mod")
    assert [row["number"] for row in storage.notes_of(ALICE)] == [second, first]
    assert storage.drop_note(first) == ALICE
    assert storage.drop_note(first) is None
    assert [row["text"] for row in storage.notes_of(ALICE)] == ["said sorry"]
    storage.close()


def test_a_flag_is_once_on_a_player_and_comes_off(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "db.sqlite3")
    storage.set_flag(ALICE, "watch", "first", "Mod")
    storage.set_flag(ALICE, "watch", "second", "Admin")
    assert [(row["flag"], row["why"], row["by"]) for row in storage.flags_of(ALICE)] == [("watch", "second", "Admin")]
    assert storage.clear_flag(ALICE, "watch") and not storage.clear_flag(ALICE, "watch")
    storage.close()


def test_a_hosts_own_flags_arent_a_warning(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    make_file(path)
    text = path.read_text(encoding="utf-8").replace('trusted = "green",', 'trusted = "green", "team killer" = "#112233",')
    path.write_text(text, encoding="utf-8")
    config = load(path)
    assert not config.warnings and config.section("notes")["flags"]["team killer"] == "#112233"


# The commands --------------------------------------------------------------------------------------------------------


def test_the_commands_come_and_go_with_the_switch(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, notes=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await wait_for(lambda: GUILD in fake.commands)
            assert {c["name"] for c in fake.commands[GUILD]} == {"huragok"}
            set_switch(path, ("notes",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: {c["name"] for c in fake.commands[GUILD]} == {"huragok", "note", "flag"}, timeout=15)
            set_switch(path, ("notes",), "enabled", False)
            await part.reload(force=True)
            await wait_for(lambda: {c["name"] for c in fake.commands[GUILD]} == {"huragok"}, timeout=15)

    run(scenario())


def test_a_note_written_read_and_taken_away(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.slash("note", "add", player=ALICE, text="seen spawn killing")
            assert "Note #1 on **Alice**" in await answer_text(fake, token)
            _, token = fake.slash("note", "list", player="Alice")  # typed, not picked: fine when only one has gone by it
            said = await answer_text(fake, token)
            assert "#1 " in said and "seen spawn killing" in said
            _, token = fake.slash("note", "remove", number=1)
            assert "gone" in await answer_text(fake, token)
            _, token = fake.slash("note", "list", player=ALICE)
            assert "nothing written" in await answer_text(fake, token)

    run(scenario())


def test_a_note_can_run_out(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.slash("note", "add", player=ALICE, text="for now", days=7)
            assert "for 7 days" in await answer_text(fake, token)
            row = huragok.storage.notes_of(ALICE)[0]
            assert 6.9 * 86400 < row["expires"] - time.time() < 7.1 * 86400

    run(scenario())


def test_who_may_do_what(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.slash("note", "add", user=OTHER, player=ALICE, text="nope")
            assert "for staff" in await answer_text(fake, token)
            assert huragok.storage.notes_of(ALICE) == []
            _, token = fake.slash("note", "add", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE, text="on probation, and watching")
            assert "Note #1" in await answer_text(fake, token)
            _, token = fake.slash("note", "remove", user=OTHER, roles=(PROBATION_ROLE,), number=1)
            assert "takes a moderator" in await answer_text(fake, token)
            _, token = fake.slash("flag", "set", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE, flag="watch")
            assert "takes a moderator" in await answer_text(fake, token)
            _, token = fake.slash("flag", "set", user=OTHER, roles=(MOD_ROLE,), player=ALICE, flag="watch", why="spawn killing")
            assert "flagged **watch**" in await answer_text(fake, token)
            _, token = fake.slash("note", "remove", user=OTHER, roles=(MOD_ROLE,), number=1)
            assert "gone" in await answer_text(fake, token)

    run(scenario())


def test_only_staff_get_suggestions_of_who_has_played(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)

            async def choices(token: str) -> list[dict[str, Any]]:
                await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
                return next(c for c in fake.callbacks if c.get("token") == token)["data"]["choices"]

            _, token = fake.typing("note", "add", "player", "lic")
            assert [c["value"] for c in await choices(token)] == [ALICE]
            _, token = fake.typing("note", "add", "player", "lic", user=OTHER)
            assert await choices(token) == []
            _, token = fake.typing("flag", "set", "flag", "voi", player=ALICE)
            assert [c["value"] for c in await choices(token)] == ["voice abuser"]

    run(scenario())


def test_a_flag_that_isnt_on_the_list_is_refused(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.slash("flag", "set", player=ALICE, flag="made up")
            assert "isn't a flag here" in await answer_text(fake, token)
            assert huragok.storage.flags_of(ALICE) == []

    run(scenario())


def test_two_players_with_one_name_have_to_be_picked_between(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Sam", ALICE)
            await rcon.join(4, "Sam", BRAVO)
            await wait_for(lambda: huragok.storage.player(BRAVO) is not None, timeout=10)
            _, token = fake.slash("note", "add", player="Sam", text="which one")
            assert "2 players have gone by Sam" in await answer_text(fake, token)
            _, token = fake.slash("note", "add", player="Nobody", text="who")
            assert "Nobody here has gone by" in await answer_text(fake, token)
            assert huragok.storage.notes_of(ALICE) == huragok.storage.notes_of(BRAVO) == []

    run(scenario())


# Where they show ------------------------------------------------------------------------------------------------------


def test_a_flagged_player_joining_tells_staff_in_the_flags_colour(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            huragok.storage.set_flag(ALICE, "voice abuser", "shouting", "Mod")
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: any("flagged" in str(card.get("description")) for card in cards_of(fake)), timeout=15)
            card = next(card for card in cards_of(fake) if "flagged" in str(card.get("description")))
            assert card["description"] == "**Alice** is on, flagged **voice abuser**: shouting"
            assert card["color"] == 0xE67E22  # orange, from [notes] flags

    run(scenario())


def test_a_flag_not_in_watch_flags_stays_quiet(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            huragok.storage.set_flag(ALICE, "regular", "", "Mod")
            huragok.storage.record_join(ALICE, "Alice", "198.51.100.3")  # known: not someone new on a quiet server
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert not any("flagged" in str(card.get("description")) for card in cards_of(fake))

    run(scenario())


def test_a_case_shows_the_flags_and_notes(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, moderation=True, watch=False)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            huragok.storage.set_flag(ALICE, "watch", "spawn killing", "Mod")
            for number in range(5):
                huragok.storage.add_note(ALICE, f"note {number}", "Mod", when=1000.0 + number)
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands", reason="spam")
            await wait_for(lambda: len(cards_of(fake)) == 1, timeout=15)
            fields = {field["name"]: field["value"] for field in cards_of(fake)[0]["fields"]}
            assert fields["What staff have written"] == "Flagged **watch**: spawn killing\nnote 4 (Mod)\nnote 3 (Mod)\nnote 2 (Mod)"

    run(scenario())


def test_whois_shows_them_in_game(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):  # the bot off: what's written still shows
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(1, "Faculty", FACULTY)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            huragok.storage.set_flag(ALICE, "watch", "spawn killing", "Mod (Discord)")
            huragok.storage.add_note(ALICE, "said sorry", "Admin (Discord)")
            await rcon.chat(FACULTY, "Faculty", "!whois Alice")
            await wait_for(lambda: any("Note by Admin (Discord): said sorry" in line for line in tells_to(rcon, FACULTY)), timeout=10)
            assert any("Flagged watch by Mod (Discord): spawn killing" in line for line in tells_to(rcon, FACULTY))

    run(scenario())


def test_a_change_is_on_the_audit_feed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, watch=False)
        set_switch(path, ("feed", "audit"), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.slash("flag", "set", player=ALICE, flag="watch")
            await answer_text(fake, token)
            await wait_for(lambda: any("flagged Alice watch" in str(post.get("content")) for post in fake.posted), timeout=15)

    run(scenario())
