"""The host's call of 21 November 2026, after the scope audit: moderation is every server's or each server's ([automod]
across_servers), never split by who gave it, and a rank on one server is a rank on all of them. Only a vote's kick, the
server's own, and a server's own settings stay on one server. What players and staff read says how far it reaches."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from conftest import ADMIN, ALICE, BRAVO, FACULTY, MOD, staff, wait_for
from testkit.commands import OtherServer
from testkit.running import everyone_joins, running, tells_to

from huragok.servers.state import Player, ServerState

SERVER = "LG Test Server"
ZED = "9" * 64
WORDS = "badword | 1d | | | slurs\n"


def _other(sup, players: dict[int, Player] | None = None) -> OtherServer:
    other = OtherServer()
    sup.superintendent.clients["Other"] = other  # type: ignore[assignment]
    sup.superintendent.states["Other"] = ServerState("Other", connected=True, players=players or {})
    return other


def _gone(sup) -> None:
    sup.superintendent.states.pop("Other", None)
    sup.superintendent.clients.pop("Other", None)


@pytest.mark.parametrize("across", [True, False])
def test_a_rank_on_one_server_is_a_rank_on_every_server(tmp_path: Path, run, across: bool) -> None:
    """"Admin on one server? Admin everywhere": an admin on Other's own list is an admin typing on this server too. With
    across_servers off, a rank is each server's."""
    async def scenario() -> None:
        body = "" if across else "[automod]\nacross_servers = false\n"
        async with running(tmp_path, body) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = _other(sup)
            sup.superintendent.states["Other"].admins = {ALICE}
            await fake.chat(ALICE, "Alice", f"!ban {ZED} cheating")
            if across:
                await wait_for(lambda: [c for c in fake.named("ban") if c["args"][0] == ZED])
                await wait_for(lambda: any("banned from every server for good" in t for t in tells_to(fake, ALICE)))
            else:
                await wait_for(lambda: any(t == "!ban is for admins and up." for t in tells_to(fake, ALICE)))
                assert not [c for c in fake.named("ban") if c["args"][0] == ZED]
            assert other is not None
            _gone(sup)

    run(scenario())


def test_a_ban_on_one_servers_own_list_becomes_every_servers(tmp_path: Path, run) -> None:
    """A /ban from before 0.7.0 (on one server's own list, not on Huragok's) is every server's at the next look; one
    lifted on every server while that server was away comes off it instead, and a vote's stays that server's."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            other = _other(sup)
            other.banned = [ZED, "8" * 64, "7" * 64]
            other.details = {ZED: {"banned_by": "Grok", "banned_at": int(time.time()) - 60, "reason": "spam"},
                             "8" * 64: {"banned_by": "vote", "banned_at": int(time.time()) - 60},
                             "7" * 64: {"banned_by": "Bob", "banned_at": int(time.time()) - 600}}
            commands = sup.vergil.commands.commands["banall"].runner.part  # the commands part
            commands.add_every_server("ban", "7" * 64, name="Seven", reason="", by="Bob", server="Other", expires=None)
            commands.lift_every_server("ban", "7" * 64, "Mod")  # lifted everywhere, after it was given
            await commands._every_server_check("Other")
            assert [row["target"] for row in commands.every_server("ban")] == [ZED]
            await wait_for(lambda: [c for c in fake.named("ban") if c["args"][0] == ZED])  # on this server's list too
            assert ("7" * 64,) in other.named("unban") and ("8" * 64,) not in other.named("unban")
            assert not [c for c in fake.named("ban") if c["args"][0] == "8" * 64]
            _gone(sup)

    run(scenario())


def test_an_unban_lifts_every_ban_on_every_server(tmp_path: Path, run) -> None:
    """An unban lifts them everywhere: another server's own /ban too (up to 0.7.0 it stayed), and from a server that
    didn't have the ban at all (no unban event follows there: the scope audit, A10)."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            other = _other(sup)
            other.banned = [ZED]
            other.details = {ZED: {"banned_by": "Grok", "banned_at": int(time.time()) - 60}}
            fake.replies["unban"] = lambda message: {"ok": False, "text": "Nobody is banned by that."}
            await fake.chat(ADMIN, "Admin", f"!unban {ZED}")
            await wait_for(lambda: other.named("unban"))
            await wait_for(lambda: any("every other server is told to lift any ban" in t for t in tells_to(fake, ADMIN)))
            _gone(sup)

    run(scenario())


def test_a_staff_strike_is_never_a_servers_vergil_setting(tmp_path: Path, run) -> None:
    """Vergil off on the server it's typed on, or the one Discord picked: a moderator's strike still counts and mutes. The
    setting is about what Vergil catches by itself (the host's call, 21 November 2026, H2)."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        body = f'[automod]\nenabled = true\n[[server]]\nname = "{SERVER}"\nautomod = "off"\n'
        async with running(tmp_path, body) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!strike bravo slurs said it in voice")
            mute = (await wait_for(lambda: fake.named("mute")))[0]
            assert mute["args"][:2] == [BRAVO, "1d"] and "muted for 1 day on every server" in mute["args"][2]
            await wait_for(lambda: any(t.startswith("Bravo dddddddd") and ": slurs strike 1, muted for 1 day (done)" in t
                                       for t in tells_to(fake, MOD)))
            strikes = sup.storage.db.execute("SELECT dry_run FROM automod_strikes").fetchall() if hasattr(sup.storage, "db") else None
            assert strikes is None or all(row[0] == 0 for row in strikes)

    run(scenario())


def test_lifting_a_timed_kick_takes_its_givers_rank(tmp_path: Path, run) -> None:
    """A kick with a time is the server's timed ban underneath: a moderator can't let back someone an admin kicked for an
    hour (the scope audit, A4: only plain kicks were looked at)."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ADMIN, "Admin", "!kick bravo 1h griefing")
            await wait_for(lambda: [c for c in fake.named("ban") if c["args"][0] == BRAVO])
            await fake.chat(MOD, "Mod", f"!pardon {BRAVO}")
            await wait_for(lambda: any(t.startswith("Not let back: Admin gave that kick, and lifting it takes an admin")
                                       for t in tells_to(fake, MOD)))
            assert not fake.named("unban")

    run(scenario())


def test_a_kick_with_a_time_says_every_server_to_them_and_the_staff(tmp_path: Path, run) -> None:
    """What the player reads says how far it goes, and so does the reply (the host's call, 21 November 2026, H5)."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!kick bravo 30m spawn camping")
            ban = (await wait_for(lambda: [c for c in fake.named("ban") if c["args"][0] == BRAVO]))[0]
            assert ban["args"][2].startswith("Kicked off every server for 30 minutes: spawn camping")
            await wait_for(lambda: any(t.startswith("Bravo dddddddd") and "is kicked off every server for 30 minutes" in t
                                       for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!mute alice 10m spam")
            mute = (await wait_for(lambda: fake.named("mute")))[0]
            assert mute["args"][2] == "spam (every server)"

    run(scenario())


def test_kick_and_mute_someone_on_another_server_or_none(tmp_path: Path, run) -> None:
    """In game, !kick and !mute act wherever the player is: on another server they're sent there, on none a kick needs a
    time (the host's call, 21 November 2026, H4)."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            other = _other(sup, {3: Player(3, "Zed", ZED)})
            await fake.chat(MOD, "Mod", f"!kick {ZED} griefing")
            assert (await wait_for(lambda: other.named("kick")))[0][0] == ZED
            sup.superintendent.states["Other"].players.clear()
            await fake.chat(MOD, "Mod", f"!kick {ZED}")
            await wait_for(lambda: any("isn't on any server now, so there's nobody to kick" in t for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", f"!kick {ZED} 1h")
            await wait_for(lambda: [c for c in fake.named("ban") if c["args"][0] == ZED])
            _gone(sup)

    run(scenario())


def test_the_update_turns_moderation_every_servers_once(tmp_path: Path) -> None:
    """A host who had across_servers off gets it on with 0.7.0 (the host's call: "defaulted, and changed on the update"),
    once: the step is done by its key, so turning it off again holds."""
    from huragok.updates.hostfiles import context_for, every_server_moderation

    settings = tmp_path / "settings.toml"
    settings.write_text('[automod]\nenabled = true\nacross_servers = false   # mine\n', encoding="utf-8")
    ctx = context_for(settings, tmp_path / "records", tmp_path / "PACKAGES")
    said = every_server_moderation(ctx)
    assert said and "across_servers = true" in said[0]
    assert "across_servers = true" in settings.read_text(encoding="utf-8")
    assert every_server_moderation(ctx) == []  # on already: nothing to do
    settings.write_text('[automod]\nenabled = true\n', encoding="utf-8")
    assert every_server_moderation(ctx) == []  # no line: the default is on
