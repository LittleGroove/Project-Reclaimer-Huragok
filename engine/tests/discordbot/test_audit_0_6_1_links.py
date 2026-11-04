"""The audit before 0.6.1 (plans/bug-audit-0.6.1.md): linking, role sync and claiming (area 3)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import ALICE, MOD
from test_bot_running import OTHER, answer_text
from test_links import code_in_game, link_file, up
from test_roles import SOMEONE, fake, roles_file, settled  # noqa: F401 (fake: the fixture)
from testkit.fake_discord import ADMIN_ROLE, BOT_ROLE, FACULTY_USER, GUILD, MOD_ROLE, FakeDiscord
from testkit.running import running, wait_for

from huragok.core.commandsfile import add_player, load_permissions
from huragok.discordbot import features, rolesync
from huragok.discordbot.client import someone_from
from huragok.discordbot.config import load, set_switch
from huragok.discordbot.links import NO_SUCH_CODE, TOO_MANY_WRONG
from huragok.discordbot.outbox import CHAT
from huragok.discordbot.ranks import is_faculty, rank_of


def member(part: Any, user: str, *roles: str) -> Any:
    """A member of the fake Discord as discord.py hands one to a command."""
    named = [SimpleNamespace(id=int(role), name=str(part.facts.find_role(role)["name"])) for role in roles]
    return SimpleNamespace(id=int(user), roles=named, guild=SimpleNamespace(id=int(GUILD)), guild_permissions=None,
                           is_timed_out=lambda: False)


def waiting(part: Any, key: str) -> str:
    return features.why_waiting(features.BY_KEY[key], part.file, part.facts)


def test_rank_roles_wait_while_linking_is_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.1: switched on with linking off, every mapped rank role came off everyone, and Discord staff powers with it."""
    async def scenario() -> None:
        path = link_file(tmp_path, enabled=False)
        set_switch(path, ("roles",), "sync", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert "linking is off" in waiting(part, "roles") and not rolesync.on(part, "roles")
            assert await rolesync.put_roles_right(part) == 0
            assert ADMIN_ROLE in fake.member_roles[SOMEONE]

    run(scenario())


def test_a_rank_role_given_by_hand_counts_for_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.2: a role given by hand counted in Discord until the next look: long enough to /promote yourself."""
    async def scenario() -> None:
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "sync", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert rolesync.on(part, "roles")
            assert rank_of(part.file, someone_from(member(part, SOMEONE, ADMIN_ROLE))) == "player"  # not linked: it's nobody's
            part.links.use_code(part.links.new_code(MOD, "Mod"), OTHER, "Buck")
            assert rank_of(part.file, someone_from(member(part, OTHER, MOD_ROLE, ADMIN_ROLE))) == "moderator"  # their rank, not the role they took
            set_switch(path, ("roles",), "sync", False)
            await part.reload(force=True)
            assert rank_of(part.file, someone_from(member(part, SOMEONE, ADMIN_ROLE))) == "admin"  # sync off: roles are the host's, as before

    run(scenario())


def test_faculty_say_whose_code_it_is_and_unlinking_takes_it_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.3: faculty typing someone else's code made that player faculty in game for good."""
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            code = await code_in_game(huragok, rcon)
            _, token = fake.command("link", user=FACULTY_USER, code=code)
            assert "Is **Alice** you?" in await answer_text(fake, token)
            assert part.links.of_member(FACULTY_USER) is None  # nothing done until they say
            part.links.use_code(code, FACULTY_USER, "the host")
            assert await part.links.faculty_claimed(FACULTY_USER) == "Alice"
            commands = tmp_path / "commands.toml"
            assert ALICE in load_permissions(commands).members["faculty"]
            gone = part.links.unlink(FACULTY_USER)
            assert await part.links.faculty_unclaimed(gone)
            assert ALICE not in load_permissions(commands).members["faculty"]
            add_player(commands, "faculty", ALICE, "Alice: the host's own")  # written by hand: never taken
            part.links.use_code(part.links.new_code(ALICE, "Alice"), FACULTY_USER, "the host")
            assert not await part.links.faculty_unclaimed(part.links.unlink(FACULTY_USER))
            assert ALICE in load_permissions(commands).members["faculty"]

    run(scenario())


def test_faculty_kept_to_admin_in_game_arent_made_faculty_by_a_link(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """[ranks] faculty_in_game = "admin" keeps faculty of the bot to admin in game: a link doesn't undo that."""
    async def scenario() -> None:
        path = link_file(tmp_path)
        set_switch(path, ("ranks",), "faculty_in_game", "admin")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            part.links.use_code(part.links.new_code(ALICE, "Alice"), FACULTY_USER, "the host")
            assert await part.links.faculty_claimed(FACULTY_USER) == ""
            assert ALICE not in load_permissions(tmp_path / "commands.toml").members["faculty"]

    run(scenario())


def test_a_big_look_asks_for_a_few_hundred_changes_at_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.4: a first look on a big Discord filled the outbox past 2,000, and other posts were dropped with the changes."""
    async def scenario() -> None:
        for number in range(450):
            fake.member_roles[str(20000 + number)] = [ADMIN_ROLE]
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "sync", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            part.roles_running, part.roles_due = True, False  # no tick's look meanwhile: this one alone
            assert await rolesync.put_roles_right(part) == rolesync.MOST_CHANGES
            assert part.roles_due and rolesync.changes_waiting(part)  # the rest at the next look, once these have gone
            jobs = [job for job in part.outbox.jobs if job.bucket == rolesync.BUCKET]
            assert jobs and all(job.priority == CHAT for job in jobs)  # the least urgent: posts go first
            assert part.outbox.dropped == 0

    run(scenario())


def test_a_role_with_two_jobs_or_one_the_bot_cant_give_waits(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.5, 3.8: a top role that's a rank role made the leader staff; one role on two ranks gave moderators an
    admin's; an integration's role was refused at every look."""
    async def scenario() -> None:
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "top_roles", True)
        set_switch(path, ("roles",), "top", {"kills season": ADMIN_ROLE})
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert "is a rank role" in waiting(part, "top_roles")
            set_switch(path, ("roles",), "top", {"kills season": BOT_ROLE})
            await part.reload(force=True)
            assert "belongs to an integration" in waiting(part, "top_roles")
            set_switch(path, ("roles",), "sync", True)
            set_switch(path, ("ranks",), "moderator", [MOD_ROLE, ADMIN_ROLE])
            await part.reload(force=True)
            assert "more than one rank" in waiting(part, "roles")

    run(scenario())


def test_unlinking_someone_else_takes_an_admin_everywhere_and_never_a_higher_rank(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.6, 3.7: one server's admin could unlink anyone, faculty too; and a member who'd left could never be unlinked."""
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            part.links.use_code(part.links.new_code(ALICE, "Alice"), FACULTY_USER, "the host")
            part.links.use_code(part.links.new_code(MOD, "Mod"), "777001", "Gone")  # a member who's left the Discord
            _, token = fake.command("unlink", user=OTHER, roles=(ADMIN_ROLE,), player="Alice")
            assert "your rank or above" in await answer_text(fake, token)
            assert part.links.of_player(ALICE) is not None
            _, token = fake.command("unlink", user=OTHER, roles=(MOD_ROLE,), player="Mod")
            assert "for admins" in await answer_text(fake, token)
            _, token = fake.command("unlink", user=OTHER, roles=(ADMIN_ROLE,), player=MOD)
            assert "Unlinked" in await answer_text(fake, token)
            assert part.links.of_player(MOD) is None

    run(scenario())


def test_wrong_codes_are_limited(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.13: nothing stopped someone trying codes until one was a player's."""
    async def scenario() -> None:
        link_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            for _ in range(5):
                assert part.links.use_code("ZZZ-ZZZ", OTHER, "Buck")[0] == NO_SUCH_CODE
            assert part.links.use_code(part.links.new_code(ALICE, "Alice"), OTHER, "Buck")[0] == TOO_MANY_WRONG
            assert part.links.of_member(OTHER) is None

    run(scenario())


def test_old_owners_and_new_faculty_both_count(tmp_path: Path) -> None:
    """3.9: a 0.6.0 file's owners won over faculty until the update, so a claim meanwhile was lost."""
    path = tmp_path / "discord.toml"
    path.write_text('[ranks]\nowners = ["111"]\nfaculty = ["222"]\n', encoding="utf-8")
    config = load(path)
    assert is_faculty(config, "111") and is_faculty(config, "222")


def test_rank_roles_scope_and_faculty_by_their_user_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """3.10: a role whose servers are all gone counted with the highest rank anywhere. And faculty of the bot wear the
    faculty role whether they've linked or not: they're faculty here by their user ID."""
    async def scenario() -> None:
        faculty_role = "5303"
        fake.roles.append(fake._role(faculty_role, "Faculty", 1, "0"))
        fake.member_roles[FACULTY_USER] = []
        path = roles_file(tmp_path)
        set_switch(path, ("roles",), "sync", True)
        set_switch(path, ("roles",), "faculty", faculty_role)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert rolesync.rank_on(part, ALICE, ("A server that's gone",)) == "player"
            before = rolesync.due_key(part)
            part.links.use_code(part.links.new_code(MOD, "Mod"), OTHER, "Buck")
            assert rolesync.due_key(part) != before  # 3.12: a change of who should wear what starts a look
            await settled(part, fake)
            assert fake.member_roles[FACULTY_USER] == [faculty_role]

    run(scenario())
