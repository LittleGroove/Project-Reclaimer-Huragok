"""Lifting takes the rank of whoever gave it (the host's call, 2 Nov 2026): a moderator's !unmute, !allowvote or !pardon
of what an admin did is refused, and says who gave it; an admin's goes through. And the kicks list outlasts Huragok's
restart, as kicks with a time do."""

from __future__ import annotations

import time
from pathlib import Path

from conftest import BRAVO, wait_for
from testkit.running import SERVER, everyone_joins, running, tells_to

from huragok.core.storage import Storage
from huragok.players.commands.lists import KICKS_KEPT, KeptKicks, Kick


def test_an_admins_mute_takes_an_admin_to_lift(tmp_path: Path, run) -> None:
    from conftest import ADMIN, MOD

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!mute bravo 1h spam")
            await wait_for(lambda: fake.named("mute"))
            row = sup.storage.query("SELECT by_role FROM actions WHERE kind = 'mute' AND target_id = ?", (BRAVO,))[0]
            assert row["by_role"] == "admin"
            await fake.chat(MOD, "Mod", "!unmute bravo")
            await wait_for(lambda: any("Not unmuted: Admin gave that mute, and lifting it takes an admin or above."
                                       in t for t in tells_to(fake, MOD)))
            assert not fake.named("unmute")
            await fake.chat(ADMIN, "Admin", "!unmute bravo")
            await wait_for(lambda: fake.named("unmute"))

    run(scenario())


def test_an_admins_novote_takes_an_admin_to_give_back_or_change(tmp_path: Path, run) -> None:
    from conftest import ADMIN, MOD

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!novote bravo 1h spamming votes")
            await wait_for(lambda: sup.storage.query("SELECT 1 FROM commands_no_votes WHERE lifted IS NULL"))
            await fake.chat(MOD, "Mod", "!allowvote bravo")
            await wait_for(lambda: any("Not given back: Admin gave that !novote, and lifting it takes an admin or above."
                                       in t for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!novote bravo 5m")  # a new time would lift the admin's
            await wait_for(lambda: any("Bravo's votes are away already. Admin gave that !novote, and changing it takes"
                                       in t for t in tells_to(fake, MOD)))
            assert len(sup.storage.query("SELECT 1 FROM commands_no_votes WHERE lifted IS NULL")) == 1
            await fake.chat(ADMIN, "Admin", "!allowvote bravo")
            await wait_for(lambda: not sup.storage.query("SELECT 1 FROM commands_no_votes WHERE lifted IS NULL"))

    run(scenario())


def test_an_admins_kick_takes_an_admin_to_pardon(tmp_path: Path, run) -> None:
    from conftest import ADMIN, MOD

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!kick bravo cheating")
            await wait_for(lambda: fake.named("kick"))
            await fake.chat(MOD, "Mod", f"!pardon {BRAVO}")
            await wait_for(lambda: any("Not pardoned: Admin gave that kick, and lifting it takes an admin or above."
                                       in t for t in tells_to(fake, MOD)))
            assert not fake.named("pardon")

    run(scenario())


def test_the_kicks_list_outlasts_a_restart(tmp_path: Path) -> None:
    """A plain kick passed around the servers, and one waiting for someone away (a confirmed request), were kept in
    memory only: Huragok's restart let them straight back on every other server."""
    from huragok.players.commands.schema import MIGRATIONS

    path = tmp_path / "huragok.sqlite3"
    storage = Storage(path)
    storage.migrate("commands", MIGRATIONS)
    kicks = KeptKicks(storage)
    now = time.time()
    plain = Kick(now - 60, "Alpha", BRAVO, "Bravo", "Mod", "spam", 600, True)
    away = Kick(now - 30, "Alpha", "f" * 64, "Foxtrot", "Mod", "confirmed request", 3600, True, True)
    old = Kick(now - KICKS_KEPT - 10, "Alpha", "e" * 64, "Echo", "Mod", "", 600, True)
    for kick in (old, plain, away):
        kicks.append(kick)
    kicks.remove(plain)
    storage.close()
    again = Storage(path)
    again.migrate("commands", MIGRATIONS)
    back = list(KeptKicks(again))
    assert back == [away]  # the pardoned one gone, the old one too, the rest as it was
    again.close()
