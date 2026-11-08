"""Linking (0.6.1): !link in game tells a code privately, /link here ties the player to the member, and the stats show
a linked player under their name here (the host's ask, 3 November 2026)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO
from test_bot_running import OTHER, answer_text
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, REPORTS, STAFF
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, FACULTY_USER, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.config import add_faculty, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.discordbot.links import CODES_AN_HOUR, LINKED, MEMBER_TAKEN, NO_SUCH_CODE, PLAYER_TAKEN, tidy_code

SOMEONE = "9911"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def link_file(root: Path, *, enabled: bool = True, **options: Any) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_faculty(path, FACULTY_USER, "the host")
    for name, channel in (("staff", STAFF), ("reports", REPORTS), ("alerts", ALERTS), ("logs", LOGS), ("huragok-logs", HURAGOK_LOGS)):
        set_channel(path, name, channel, name)
    set_switch(path, ("link",), "enabled", enabled)
    for key, value in options.items():
        set_switch(path, ("link",), key, value)
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]'),
                    encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


def told(rcon: Any, words: str) -> list[str]:
    return [str(command) for command in rcon.named("tell") if words in str(command)]


async def code_in_game(huragok: Any, rcon: Any, player_id: str = ALICE, name: str = "Alice") -> str:
    """The player on, typing !link: the code they're told."""
    if not huragok.superintendent.states[SERVER].player_by_id(player_id):
        await rcon.join(3 if player_id == ALICE else 4, name, player_id)
        await wait_for(lambda: huragok.superintendent.states[SERVER].player_by_id(player_id), timeout=10)
    before = len(told(rcon, "link code"))
    await rcon.chat(player_id, name, "!link")
    await wait_for(lambda: len(told(rcon, "link code")) > before, timeout=10)
    found = re.search(r"code is ([A-Z0-9]{3}-[A-Z0-9]{3})", told(rcon, "link code")[-1])
    assert found
    return found.group(1)


def test_a_code_told_in_game_links_them_here(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            code = await code_in_game(huragok, rcon)

            def everything() -> str:  # game chat wraps a long line: its parts are tells of their own, one by one
                return " ".join(str(command) for command in rcon.named("tell"))

            await wait_for(lambda: "Only you can see this" in everything() and "/link " + code in everything(),
                           timeout=10)
            _, token = fake.command("link", user=OTHER, code=code.lower().replace("-", " "))  # however they type it
            assert "Linked: **Alice** is you" in await answer_text(fake, token)
            assert part.links.of_player(ALICE)["member"] == OTHER
            await wait_for(lambda: told(rcon, "stats show under that name"), timeout=10)  # and told in game
            _, token = fake.command("link", user=SOMEONE, code=code)
            assert "isn't one, or it's been used" in await answer_text(fake, token)  # once
            await rcon.chat(ALICE, "Alice", "!link")
            await wait_for(lambda: told(rcon, "on Discord already"), timeout=10)

    run(scenario())


def test_one_player_one_member(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            links = part.links
            assert links.use_code(links.new_code(ALICE, "Alice"), OTHER, "Buck") == (LINKED, ALICE, "Alice")
            assert links.use_code(links.new_code(BRAVO, "Bravo"), OTHER, "Buck")[0] == MEMBER_TAKEN
            code = links.new_code(ALICE, "Alice")  # someone else with a code for a linked player (it can't be asked in game)
            assert links.use_code(code, SOMEONE, "Stranger")[0] == PLAYER_TAKEN
            assert links.of_member(SOMEONE) is None

    run(scenario())


def test_a_code_runs_out_and_only_so_many_are_given(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        link_file(tmp_path, code_minutes=5)
        async with running(tmp_path) as (huragok, rcon, _):
            links = (await up(huragok)).links
            now = links.clock()
            first = links.new_code(ALICE, "Alice")
            second = links.new_code(ALICE, "Alice")
            assert links.use_code(first, OTHER, "Buck")[0] == NO_SUCH_CODE  # a new one replaces it
            links.clock = lambda: now + 6 * 60
            assert links.use_code(second, OTHER, "Buck")[0] == NO_SUCH_CODE  # past code_minutes
            links.clock = lambda: now
            for _ in range(CODES_AN_HOUR - 2):
                assert links.new_code(ALICE, "Alice")
            assert links.new_code(ALICE, "Alice") == ""
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].player_by_id(ALICE), timeout=10)
            await rcon.chat(ALICE, "Alice", "!link")
            await wait_for(lambda: told(rcon, "three codes in an hour"), timeout=10)
            links.clock = lambda: now + 3601
            assert links.new_code(ALICE, "Alice")
            assert tidy_code(" k7p-42q ") == "K7P42Q"

    run(scenario())


def test_the_stats_show_them_as_they_choose(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            links = part.links
            assert links.shown_name(ALICE, "Alice") == "Alice"  # not linked: the game's name
            links.use_code(links.new_code(ALICE, "Alice"), OTHER, "Buck")
            assert links.shown_name(ALICE, "Alice") == "Buck"  # [link] names = "discord", the host's call
            for how in ("both", "game"):
                _, token = fake.command("link", user=OTHER, show=how)
                await answer_text(fake, token)
                here = links.of_member(OTHER)["member_name"]  # as Discord says it is now: /link sees it again
                assert links.shown_name(ALICE, "Alice") == {"both": f"{here} (Alice)", "game": "Alice"}[how], how
            _, token = fake.command("link", user=OTHER)
            assert "You're linked to **Alice**" in await answer_text(fake, token) and "your name in game" in await answer_text(fake, token)
            assert not links.show(OTHER, "hidden") or links.how_shown(links.of_member(OTHER)) != "hidden"  # nobody's off the boards
            links.seen(OTHER, "Buck the Great")
            links.show(OTHER, "")
            assert links.shown_name(ALICE, "Alice") == "Buck the Great"  # their name here as it is now

    run(scenario())


def test_unlinking_puts_the_game_name_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.links.use_code(part.links.new_code(ALICE, "Alice"), OTHER, "Buck")
            _, token = fake.command("unlink", user=OTHER)
            assert "Unlinked from **Alice**" in await answer_text(fake, token)
            assert part.links.of_player(ALICE) is None and part.links.shown_name(ALICE, "Alice") == "Alice"
            _, token = fake.command("unlink", user=OTHER)
            assert "aren't linked" in await answer_text(fake, token)
            _, token = fake.command("link", user=OTHER)
            assert "Type **!link**" in await answer_text(fake, token)

    run(scenario())


def test_only_staff_unlink_when_the_setting_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        link_file(tmp_path, unlink="staff")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.links.use_code(part.links.new_code(ALICE, "Alice"), OTHER, "Buck")
            _, token = fake.command("unlink", user=OTHER)
            assert "Only the staff undo links" in await answer_text(fake, token)
            assert part.links.of_player(ALICE) is not None
            part.links.use_code(part.links.new_code(BRAVO, "Bravo"), FACULTY_USER, "the host")
            _, token = fake.command("unlink", user=FACULTY_USER)  # faculty is staff
            assert "Unlinked from **Bravo**" in await answer_text(fake, token)

    run(scenario())


def test_the_commands_are_only_there_when_wanted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = link_file(tmp_path, enabled=False)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            assert not {"link", "unlink"} & {command["name"] for command in fake.commands[GUILD]}
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].player_by_id(ALICE), timeout=10)
            await rcon.chat(ALICE, "Alice", "!link")
            await wait_for(lambda: rcon.named("tell"), timeout=10)
            assert not told(rcon, "link code")  # no such command while it's off
            set_switch(path, ("link",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: {"link", "unlink"} <= {command["name"] for command in fake.commands[GUILD]}, timeout=15)

    run(scenario())
