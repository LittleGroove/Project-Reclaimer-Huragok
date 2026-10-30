"""Probationary moderators (requests.py): moderators' commands, but what they do waits for a moderator or an admin."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, wait_for
from testkit.running import SERVER, everyone_joins, running, tells_to

PROBATION = "probationary-moderator"


async def on_probation(sup: Any, fake: Any) -> None:
    """Everyone on; Alice made a probationary moderator by a moderator (two promotions)."""
    await everyone_joins(fake)
    await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
    alice = sup.superintendent.states[SERVER].players[3]
    for _ in range(2):
        await fake.chat(MOD, "Mod", "!promote alice")
        await asyncio.sleep(0.2)
    await wait_for(lambda: sup.vergil.roles.role_of(sup.superintendent.states[SERVER], alice) == PROBATION)


def heard(fake: Any, who: str, text: str) -> Any:
    return wait_for(lambda: [t for t in tells_to(fake, who) if text in t])


def test_a_request_waits_for_a_moderator_and_happens_as_theirs(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_probation(sup, fake)
            await fake.chat(ALICE, "Alice", "!players")  # looking works as for a moderator
            await heard(fake, ALICE, "#4 Bravo")
            await fake.chat(ALICE, "Alice", "!kick bravo spamming")
            await heard(fake, ALICE, "Sent for review as #1: !kick Bravo spamming.")
            staff_line = ("[o_o] BE A HERO! REPORT VIOLATORS! Alice (probationary moderator) asks, #1 on "
                          f"{SERVER}: !kick Bravo spamming. !confirm 1 or !deny 1")
            for who in (MOD, ADMIN, OWNER):
                await heard(fake, who, staff_line)
            await asyncio.sleep(0.3)
            assert not fake.named("kick") and not tells_to(fake, BRAVO)[-1:] == [staff_line]
            assert not any("Alice" in t for t in tells_to(fake, BRAVO))  # the player named reads nothing
            await fake.chat(ALICE, "Alice", "!mute bravo 30m rude")
            await heard(fake, ALICE, "Sent for review as #2: !mute Bravo 30m rude.")
            await fake.chat(ALICE, "Alice", "!confirm 1")  # never their own (nor anyone's)
            await heard(fake, ALICE, "!confirm is for moderators")
            await fake.chat(MOD, "Mod", "!reports")
            await heard(fake, MOD, "Alice asks !mute Bravo 30m rude")
            await fake.chat(MOD, "Mod", "!confirm 1")
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"][0] == BRAVO and kick[0]["by"] == "Mod"  # the confirmer's own kick
            await heard(fake, MOD, "Confirmed #1.")
            await heard(fake, ALICE, "[^_^] YOUR TAX DOLLARS AT WORK! Mod confirmed your request #1, !kick Bravo "
                                     "spamming.")
            row = sup.storage.actions_for(BRAVO)[0]
            assert row["kind"] == "kick" and row["by"] == "Mod" and row["evidence"] == "requested by Alice (#1)"
            # #1 is free again: the next request takes it.
            await fake.chat(ALICE, "Alice", "!warn bravo watch it")
            await heard(fake, ALICE, "Sent for review as #1: !warn Bravo watch it.")
            await fake.chat(MOD, "Mod", "!deny 2 too harsh")
            await heard(fake, ALICE, "Mod turned down your request #2, !mute Bravo 30m rude: too harsh")
            assert not fake.named("mute")
            # Another action instead: a mute for an hour, not the warning asked for.
            await fake.chat(ADMIN, "Admin", "!confirm 1 mute 1h language")
            mute = await wait_for(lambda: fake.named("mute"))
            assert mute[0]["args"][:2] == [BRAVO, "1h"] and mute[0]["by"] == "Admin"
            await heard(fake, ALICE, "Admin confirmed your request #1, !warn Bravo watch it (!mute Bravo 1h language "
                                     "instead).")
            await fake.chat(MOD, "Mod", "!reports")
            await heard(fake, MOD, "No requests are waiting.")
            await fake.chat(MOD, "Mod", "!whois alice")
            await heard(fake, MOD, "Requests as a probationary moderator: 3 (2 confirmed, 1 turned down, 0 waiting)")

    run(scenario())


def test_no_lapsing_a_player_who_left_is_dealt_with_as_they_come_back(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_probation(sup, fake)
            await fake.chat(ALICE, "Alice", "!kick bravo griefing")
            await heard(fake, ALICE, "Sent for review as #1")
            await fake.chat(ALICE, "Alice", "!warn bravo no spawn killing")
            await heard(fake, ALICE, "Sent for review as #2")
            await fake.leave(4)
            await wait_for(lambda: 4 not in sup.superintendent.states[SERVER].players)
            await fake.chat(MOD, "Mod", "!confirm 1")
            await heard(fake, MOD, "Confirmed #1 (Bravo isn't on: from now, as they come back).")
            await fake.chat(MOD, "Mod", "!confirm 2")
            await heard(fake, MOD, "Confirmed #2")
            assert not fake.named("kick")  # nobody to kick yet
            kicks = sup.storage.actions_for(BRAVO)
            assert {r["kind"] for r in kicks} == {"kick", "warn"} and all("requested by Alice" in r["evidence"]
                                                                          for r in kicks)
            await fake.join(6, "Bravo", BRAVO)  # back within the kick's time: kicked as they join
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"][0] == BRAVO and "Kicked from" in kick[0]["args"][1]
            await heard(fake, BRAVO, "[o_o] OBEY POSTED LIMITS! Warning from Mod: no spawn killing")

    run(scenario())


def test_the_probationary_moderator_hears_how_it_went_when_they_come_back(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_probation(sup, fake)
            await fake.chat(ALICE, "Alice", "!mute bravo 10m")
            await heard(fake, ALICE, "Sent for review as #1")
            await fake.leave(3)
            await wait_for(lambda: 3 not in sup.superintendent.states[SERVER].players)
            await fake.chat(MOD, "Mod", "!deny 1 not needed")
            await heard(fake, MOD, "Turned down #1.")
            await fake.join(7, "Alice", ALICE)
            await heard(fake, ALICE, "Mod turned down your request #1, !mute Bravo 10m: not needed")

    run(scenario())


def test_the_rank_guards_and_a_promotion_by_request(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_probation(sup, fake)
            await fake.chat(ALICE, "Alice", "!kick mod")  # never someone of their rank or above
            await heard(fake, ALICE, "Mod is a moderator; you can't do that to them.")
            await fake.chat(ALICE, "Alice", "!promote bravo")
            await heard(fake, ALICE, "Sent for review as #1: !promote Bravo.")
            bravo = sup.superintendent.states[SERVER].players[4]
            assert sup.vergil.roles.role_of(sup.superintendent.states[SERVER], bravo) == "player"
            await fake.chat(MOD, "Mod", "!confirm 1")
            await wait_for(lambda: sup.vergil.roles.role_of(sup.superintendent.states[SERVER], bravo) == "member")
            await fake.chat(MOD, "Mod", "!roles")
            await heard(fake, MOD, "probationary moderators (1): Alice")
            # Kept in Huragok's database (read back at every start), and commands.toml lists her as a member.
            assert [r["target"] for r in sup.storage.query("SELECT target FROM commands_probation")] == [ALICE]
            assert ALICE in sup.settings.permissions.members["member"]

    run(scenario())
