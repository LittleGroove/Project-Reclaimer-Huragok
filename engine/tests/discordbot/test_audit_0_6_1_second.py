"""The second audit before 0.6.1 (plans/bug-audit-0.6.1.md), area 8: sides, roles, links."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import ALICE
from test_bot_running import OTHER, answer_text
from test_links import link_file, up
from test_roles import fake  # noqa: F401 (the fixture)
from testkit.fake_discord import ADMIN_ROLE, FACULTY_USER, FakeDiscord
from testkit.running import running

from huragok.core.commandsfile import add_player, load_permissions
from huragok.discordbot import sides
from huragok.discordbot.rolesync import RankRole, wanted_rank_roles
from huragok.discordbot.standings import counted

MOD_EVERYWHERE, ADMIN_HERE = "7001", "7002"


def test_a_role_for_one_server_is_worn_with_the_one_for_every_server() -> None:
    """8.1: everyone's moderator and one server's admin wore only that server's role: nobody everywhere else."""
    part = SimpleNamespace(ctx=SimpleNamespace(servers={"Server 1": None, "Server 2": None},
                                               role_of=lambda server, player: "admin" if server == "Server 1" else "moderator"))
    roles = [RankRole(MOD_EVERYWHERE, "moderator", ("*",)), RankRole(ADMIN_HERE, "admin", ("Server 1",))]
    assert wanted_rank_roles(part, ALICE, roles) == {MOD_EVERYWHERE, ADMIN_HERE}


def test_a_rejoin_isnt_a_switch_and_most_apart_is_kept_in_range() -> None:
    """8.4: a player gone and back on the other team was taken for one who'd switched themselves. 8.10: inf stopped it."""
    part = SimpleNamespace(file=SimpleNamespace(section=lambda *_: {}))
    sides._not_switched(part, "S", {ALICE: "red"}, [(ALICE, "red")])  # seen on Red
    sides._not_switched(part, "S", {}, [])  # gone
    assert sides._not_switched(part, "S", {ALICE: "blue"}, [(ALICE, "red")]) == [(ALICE, "red")]  # back on Blue: moved
    assert counted(float("inf"), 2, 0, 64) == 2 and counted(float("nan"), 2, 0, 64) == 2


def test_an_unlink_gives_back_the_rank_a_claim_took_and_two_by_one_name_are_asked_apart(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """8.3: a moderator whose code faculty linked, unlinked to put it right, was left a player. 8.5: /unlink player:
    took the first of two links by one name."""
    async def scenario() -> None:
        link_file(tmp_path)
        commands = tmp_path / "commands.toml"
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            add_player(commands, "moderator", ALICE, "Alice: a moderator")
            part.ctx.reload_permissions()
            part.links.use_code(part.links.new_code(ALICE, "Alice"), FACULTY_USER, "the host")
            assert await part.links.faculty_claimed(FACULTY_USER) == "Alice"
            assert ALICE in load_permissions(commands).members["faculty"]
            assert await part.links.faculty_unclaimed(part.links.unlink(FACULTY_USER))
            assert ALICE in load_permissions(commands).members["moderator"]
            part.links.use_code(part.links.new_code("e" * 64, "Twin"), "8001", "One")
            part.links.use_code(part.links.new_code("f" * 64, "Twin"), "8002", "Two")
            _, token = fake.command("unlink", user=OTHER, roles=(ADMIN_ROLE,), player="Twin")
            assert "More than one link goes by" in await answer_text(fake, token)

    run(scenario())
