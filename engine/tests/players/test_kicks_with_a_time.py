"""A ban is for good; anything with a time is a kick. Underneath it's the server's timed ban, which refuses them at the door and says how long is left and why; everywhere else
it's a kick. And what's passed on to other servers, or is Huragok tidying up, is one line on the record."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conftest import ALICE, MOD, OWNER
from testkit.running import SERVER, running, tells_to, wait_for

from huragok.players.ledger import is_housekeeping, kick_reason, shown_kind


def rows_of(huragok: Any, kind: str) -> list[Any]:
    return huragok.storage.query("SELECT * FROM actions WHERE kind = ? AND target_id = ? ORDER BY number", (kind, ALICE))


async def on(huragok: Any, rcon: Any, *people: tuple[int, str, str]) -> None:
    for number, name, player_id in people:
        await rcon.join(number, name, player_id)
    await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) >= len(people), timeout=10)


def test_what_its_called() -> None:
    assert shown_kind("ban", "7d") == "kick" and shown_kind("ban", "604800s") == "kick"
    assert shown_kind("ban", None) == "ban" and shown_kind("ban", "") == "ban"
    assert shown_kind("kick", None) == "kick" and shown_kind("mute", "1d") == "mute"


def test_what_the_player_reads() -> None:
    assert kick_reason("7d", "team killing") == "Kicked for 7 days: team killing"
    assert kick_reason("1h", "") == "Kicked for 1 hour."


def test_a_kick_with_a_time_is_the_servers_timed_ban_with_the_reason_saying_so(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (3, "Alice", ALICE))
            await rcon.chat(OWNER, "Owner", "!kick Alice 7d team killing")
            await wait_for(lambda: rcon.named("ban"), timeout=10)
            assert rcon.named("ban")[0]["args"] == [ALICE, "7d", "Kicked for 7 days: team killing"]
            assert rcon.named("kick") == []
            row = rows_of(huragok, "ban")[0]
            assert row["duration"] == "7d" and row["reason"] == "team killing"  # the record has the reason alone
            await wait_for(lambda: any("is kicked for 7 days" in line for line in tells_to(rcon, OWNER)), timeout=10)

    run(scenario())


def test_whois_calls_it_a_kick(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (3, "Alice", ALICE))
            await rcon.chat(OWNER, "Owner", "!kick Alice 7d team killing")
            await wait_for(lambda: rows_of(huragok, "ban"), timeout=10)
            await rcon.chat(OWNER, "Owner", f"!whois {ALICE}")
            await wait_for(lambda: any("Record:" in line for line in tells_to(rcon, OWNER)), timeout=10)
            said = "\n".join(tells_to(rcon, OWNER))
            assert "Record: 1 kick" in said and ": kick 7d" in said and "!kick" in said
            assert "1 ban" not in said

    run(scenario())


def test_kicks_lists_it_and_pardon_lifts_it_at_a_pardons_rank(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (2, "Mod", MOD), (3, "Alice", ALICE))
            await rcon.chat(MOD, "Mod", "!kick Alice 12h spam")
            await wait_for(lambda: rcon.named("ban"), timeout=10)
            await rcon.leave(3)
            await wait_for(lambda: huragok.runners["commands"].part.every_server("ban", ALICE), timeout=10)
            await rcon.chat(MOD, "Mod", "!kicks")
            await wait_for(lambda: any("@1" in line and "Alice" in line for line in tells_to(rcon, MOD)), timeout=10)
            await rcon.chat(MOD, "Mod", f"!pardon {ALICE}")  # a moderator can't !unban: this is a kick
            await wait_for(lambda: rcon.named("unban"), timeout=10)
            assert rcon.named("unban")[0]["args"] == [ALICE]
            assert rows_of(huragok, "unban")[0]["reason"] == "their kick, lifted early"

    run(scenario())


def test_pardon_doesnt_lift_a_ban(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (2, "Mod", MOD), (3, "Alice", ALICE))
            await rcon.chat(OWNER, "Owner", "!ban Alice cheating")
            await wait_for(lambda: rows_of(huragok, "ban"), timeout=10)
            await rcon.chat(MOD, "Mod", f"!pardon {ALICE}")
            await wait_for(lambda: rcon.named("pardon"), timeout=10)  # the console's own pardon, which lifts kicks alone
            assert rcon.named("unban") == []

    run(scenario())


def test_a_kick_with_a_time_that_has_run_out_isnt_lifted(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            part = huragok.runners["commands"].part
            huragok.storage.record_action(kind="ban", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", duration="1h", when=1000.0)
            assert not part._kick_running(ALICE)
            huragok.storage.record_action(kind="ban", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", duration="1h")
            assert part._kick_running(ALICE)
            huragok.storage.record_action(kind="unban", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands")
            assert not part._kick_running(ALICE)

    run(scenario())


def test_a_kick_with_a_time_leaves_their_role(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (2, "Mod", MOD))
            await rcon.chat(OWNER, "Owner", "!kick Mod 1d spam")
            await wait_for(lambda: rcon.named("ban"), timeout=10)
            await wait_for(lambda: any("is kicked for 1 day" in line for line in tells_to(rcon, OWNER)), timeout=10)
            await asyncio.sleep(0.3)  # time for the server's announcement of it to have been heard
            assert MOD in huragok.settings.permissions.members["moderator"]
            assert not any("role goes with" in line for line in tells_to(rcon, OWNER))

    run(scenario())


def test_a_kick_for_good_is_a_ban_and_takes_a_bans_rank(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (2, "Mod", MOD), (3, "Alice", ALICE))
            await rcon.chat(MOD, "Mod", "!kick Alice perm cheating")
            await wait_for(lambda: any("for admins and up" in line for line in tells_to(rcon, MOD)), timeout=10)
            assert rcon.named("ban") == [] and rcon.named("kick") == []
            await rcon.chat(OWNER, "Owner", "!kick Alice perm cheating")
            await wait_for(lambda: rcon.named("ban"), timeout=10)
            assert rcon.named("ban")[0]["args"] == [ALICE, "cheating"] and rcon.named("kick") == []
            assert rows_of(huragok, "ban")[0]["duration"] is None

    run(scenario())


@pytest.mark.parametrize(("length", "allowed"), [("1d", True), ("2d", False), ("24h", True), ("25h", False)])
def test_a_moderators_kick_has_a_limit(tmp_path: Path, run: Any, length: str, allowed: bool) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (2, "Mod", MOD), (3, "Alice", ALICE))
            await rcon.chat(MOD, "Mod", f"!kick Alice {length} spam")
            if allowed:
                await wait_for(lambda: rcon.named("ban"), timeout=10)
            else:
                await wait_for(lambda: any("at most here" in line for line in tells_to(rcon, MOD)), timeout=10)
                assert rcon.named("ban") == []

    run(scenario())


def test_the_limit_is_a_setting(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path, '[automod]\nenabled = true\nmoderators_kick_at_most = "7d"\n') as (huragok, rcon, _):
            await on(huragok, rcon, (2, "Mod", MOD), (3, "Alice", ALICE))
            await rcon.chat(MOD, "Mod", "!kick Alice 7d spam")
            await wait_for(lambda: rcon.named("ban"), timeout=10)

    run(scenario())


def test_a_limit_it_cant_read_is_still_a_limit(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path, '[automod]\nenabled = true\nmoderators_kick_at_most = "a while"\n') as (huragok, rcon, _):
            await on(huragok, rcon, (2, "Mod", MOD), (3, "Alice", ALICE))
            await rcon.chat(MOD, "Mod", "!kick Alice 7d spam")
            await wait_for(lambda: any("1 day at most here" in line for line in tells_to(rcon, MOD)), timeout=10)
            assert rcon.named("ban") == []

    run(scenario())


def test_an_owner_has_no_limit(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (3, "Alice", ALICE))
            await rcon.chat(OWNER, "Owner", "!kick Alice 52w spam")
            await wait_for(lambda: rcon.named("ban"), timeout=10)

    run(scenario())


def test_a_kick_without_a_time_is_the_servers_own(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (3, "Alice", ALICE))
            await rcon.chat(OWNER, "Owner", "!kick Alice spam")
            await wait_for(lambda: rcon.named("kick"), timeout=10)
            assert rcon.named("kick")[0]["args"][-1] == "spam" and rcon.named("ban") == []

    run(scenario())


# One line on the record ----------------------------------------------------------------------------------------------------


def test_an_unban_passed_on_from_another_server_isnt_a_new_action(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            commands = huragok.runners["commands"].part
            commands._across_mark(SERVER, "unban", ALICE)
            await rcon.event("unban", target=ALICE, by="Mod (RCON)")
            await wait_for(lambda: not commands._across_echo(SERVER, "unban", ALICE), timeout=10)
            assert rows_of(huragok, "unban") == []

    run(scenario())


def test_an_unban_only_the_server_saw_has_their_name(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (3, "Alice", ALICE))
            await rcon.leave(3)
            await rcon.event("unban", target=ALICE, by="Admin")
            await wait_for(lambda: rows_of(huragok, "unban"), timeout=10)
            assert rows_of(huragok, "unban")[0]["target_name"] == "Alice"

    run(scenario())


def test_an_unban_by_id_knows_who_it_was(tmp_path: Path, run: Any) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (1, "Owner", OWNER), (3, "Alice", ALICE))
            await rcon.leave(3)
            await rcon.chat(OWNER, "Owner", f"!unban {ALICE}")
            await wait_for(lambda: rows_of(huragok, "unban"), timeout=10)
            assert rows_of(huragok, "unban")[0]["target_name"] == "Alice"

    run(scenario())


def test_what_isnt_a_decision_is_known_by_its_evidence() -> None:
    assert is_housekeeping("still muted: slurs") and is_housekeeping("kicked on Server 2")
    assert not is_housekeeping(None) and not is_housekeeping("") and not is_housekeeping("requested by Mod (#1)")


def test_the_pardon_after_a_passed_on_kick_doesnt_lift_the_kick(tmp_path: Path, run: Any) -> None:
    """Heard back from the server, it used to count as someone's pardon, so a kick kept them off only until their first try on another server."""
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):
            await on(huragok, rcon, (3, "Alice", ALICE))
            commands = huragok.runners["commands"].part
            await rcon.event("kick", id=ALICE, name="Alice", by="Mod (RCON)", reason="spam", seconds=600)
            await wait_for(lambda: commands.recent_kicks(SERVER), timeout=10)
            await huragok.vergil.moderation.act("pardon", SERVER, ALICE, by="Mod", source="commands", evidence="kicked on Other")
            await rcon.event("pardon", id=ALICE, by="Mod (RCON)")
            await rcon.event("mute", id=ALICE, name="Alice", by="time")  # a later event, so the pardon's been handled
            assert commands.recent_kicks(SERVER)

    run(scenario())
