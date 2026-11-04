"""Huragok's roles in the Discord (0.6.1, the host's calls of 3 November 2026): rank roles, top roles and side roles,
given only by Huragok. One given by hand is taken off; a rank changes with !promote and !demote, and its role follows."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, MOD, FACULTY, staff
from test_bot_running import OTHER
from test_links import link_file, up
from test_standings import ENDED, report
from testkit.fake_discord import ADMIN_ROLE, MOD_ROLE, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import rolesync
from huragok.discordbot.config import set_switch
from huragok.servers.stats import read_game, store

SOMEONE = "9911"
TOP, RED = "5301", "5302"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.roles += [discord._role(TOP, "Top Slayer", 1, "0"), discord._role(RED, "Red Team", 1, "0")]
    discord.member_roles = {OTHER: [], SOMEONE: [ADMIN_ROLE, TOP, RED]}  # someone gave themselves the lot, by hand
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def roles_file(root: Path) -> Path:
    """Linking on, with SPR-Admin as [ranks] admin and SPR-Mod as moderator (link_file's)."""
    return link_file(root)


async def settled(part: Any, fake: FakeDiscord) -> None:
    """A look now, and its changes through the outbox."""
    before = len(fake.role_changes)
    asked = await rolesync.put_roles_right(part)
    await wait_for(lambda: len(fake.role_changes) >= before + asked, timeout=15)


def test_rank_roles_follow_huragoks_ranks_and_nothing_else(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "sync", True)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.links.use_code(part.links.new_code(MOD, "Mod"), OTHER, "Buck")
            await settled(part, fake)
            assert fake.member_roles[OTHER] == [MOD_ROLE]  # their rank in Huragok
            assert ADMIN_ROLE not in fake.member_roles[SOMEONE]  # given by hand: only Huragok gives them
            await rcon.join(1, "Faculty", FACULTY)
            await rcon.join(2, "Mod", MOD)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(FACULTY, "Faculty", "!demote Mod")
            await wait_for(lambda: MOD.lower() not in {pid.lower() for pid in part.ctx.permissions.members.get("moderator", set())},
                           timeout=15)
            await settled(part, fake)
            assert MOD_ROLE not in fake.member_roles[OTHER]  # /demote (here !demote): the role follows

    run(scenario())


def test_top_roles_are_worn_by_the_linked_leaders_alone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "top_roles", True)
        set_switch(path, ("roles",), "top", {"kills all": TOP})
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            store(huragok.storage, SERVER, read_game(report("g-1")), ENDED)
            await settled(part, fake)
            assert TOP not in fake.member_roles[OTHER] and TOP not in fake.member_roles[SOMEONE]  # the leader isn't linked
            part.links.use_code(part.links.new_code(ALICE, "Alpha"), OTHER, "Buck")
            await settled(part, fake)
            assert TOP in fake.member_roles[OTHER]  # linking claims it
            assert ADMIN_ROLE in fake.member_roles[SOMEONE]  # rank roles are off here: not Huragok's to touch

    run(scenario())


def test_a_side_has_its_role(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = roles_file(tmp_path)
        for key, value in (("enabled", True), ("roles", True), ("red_role", RED)):
            set_switch(path, ("sides",), key, value)
        async with running(tmp_path, commands=staff(f'[member]\nplayers = ["{ALICE}"]\n')) as (huragok, _, _):
            part = await up(huragok)
            part.links.use_code(part.links.new_code(ALICE, "Alpha"), OTHER, "Buck")
            from huragok.discordbot import sides

            sides.choose(part, ALICE, "red")
            await settled(part, fake)
            assert RED in fake.member_roles[OTHER] and RED not in fake.member_roles[SOMEONE]
            sides.choose(part, ALICE, "")
            await settled(part, fake)
            assert RED not in fake.member_roles[OTHER]

    run(scenario())


def test_faculty_of_the_bot_are_faculty_in_game_once_linked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The host's call, 3 November 2026: claiming the bot (/huragok claim) is the one way to faculty but writing
    commands.toml by hand. Their linked player is listed there as faculty, and wears the faculty role, never their game rank's."""
    from test_bot_running import answer_text
    from test_links import code_in_game
    from test_slash import and_yes
    from testkit.fake_discord import FACULTY_USER

    from huragok.core.commandsfile import load_permissions

    async def scenario() -> None:
        faculty_role = "5303"
        fake.roles.append(fake._role(faculty_role, "Faculty", 1, "0"))
        fake.member_roles[FACULTY_USER] = [ADMIN_ROLE]
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "sync", True)
        set_switch(path, ("roles",), "faculty", faculty_role)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            code = await code_in_game(huragok, rcon)  # Alice, a player in game, is the host
            opened, token = fake.command("link", user=FACULTY_USER, code=code)
            assert "Is **Alice** you?" in await answer_text(fake, token)  # faculty say whose it is first (audit 0.6.1, 3.3)
            assert ALICE not in load_permissions(tmp_path / "commands.toml").members["faculty"]
            assert "Linked" in str((await and_yes(fake, opened, token, label="Yes, that's me")).get("content"))
            await wait_for(lambda: ALICE in load_permissions(tmp_path / "commands.toml").members["faculty"], timeout=15)
            assert "faculty, as they claimed Huragok's Discord bot" in (tmp_path / "commands.toml").read_text(encoding="utf-8")
            await settled(part, fake)
            assert fake.member_roles[FACULTY_USER] == [faculty_role]  # the faculty's role, not admin's

    run(scenario())


def test_without_the_server_members_switch_nothing_is_touched(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "sync", True)
        fake.flags &= ~((1 << 14) | (1 << 15))
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert not rolesync.on(part, "roles")  # waiting, and the check says why
            await rolesync.put_roles_right(part)
            assert fake.role_changes == []

    run(scenario())
