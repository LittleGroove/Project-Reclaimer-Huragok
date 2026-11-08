"""The audit of 6 November 2026, Vergil's word list and strike ladder in game (plans/bug-audit-2026-11-06.md, area 1)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conftest import ADMIN, BRAVO, FACULTY, MOD, wait_for
from testkit.running import SERVER, everyone_joins, running, tells_to

from huragok.players.automod.rules import RuleError, change_rule

HEAD = "# words | mutes | kicks | ban | name\n"


def told(fake: Any, player_id: str) -> str:
    return " ".join(tells_to(fake, player_id))


def settings(tmp_path: Path, words: str) -> str:
    (tmp_path / "automod.txt").write_text(words, encoding="utf-8")
    return "[automod]\nenabled = true\ncooldown_seconds = 0\n"


def test_a_pardon_lets_back_someone_the_servers_kick_removed_before_a_mute(tmp_path: Path, run: Any) -> None:
    """1.1: `kmword | 1d | 0` is the server's kick, then muted a day once they're back. !pardonplayer lifted the mute and
    left them kicked (out 10 to 120 minutes, on every server). A mute for someone on no server waits the same way with
    nobody kicked: no pardon for that one."""

    async def scenario() -> None:
        words = HEAD + "kmword | 1d | 0 | | kickmute\nmuteword | 1d | | | mutes\n"
        async with running(tmp_path, settings(tmp_path, words)) as (huragok, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 5)
            await fake.chat(BRAVO, "Bravo", "kmword")
            await wait_for(lambda: fake.named("kick"))
            await fake.leave(4)  # the kick, as the server does it
            await wait_for(lambda: BRAVO not in {p.id for p in huragok.superintendent.states[SERVER].players.values()}, timeout=10)
            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo")
            await wait_for(lambda: "cleared" in told(fake, FACULTY))
            await wait_for(lambda: fake.named("pardon"), timeout=10)
            assert "can rejoin now" in told(fake, FACULTY), told(fake, FACULTY)
            # Struck while on no server: the mute waits for them, and nobody was kicked to let back.
            before = len(fake.named("pardon"))
            await fake.chat(MOD, "Mod", "!strike bravo mutes in voice")
            await wait_for(lambda: "mutes strike 1" in told(fake, MOD))
            row = huragok.storage.query("SELECT waiting FROM automod_strikes WHERE pardoned IS NULL")
            assert [r["waiting"] for r in row] == [1]
            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo")
            await wait_for(lambda: told(fake, FACULTY).count("cleared") == 2)
            await asyncio.sleep(1)
            assert len(fake.named("pardon")) == before

    run(scenario())


@pytest.mark.parametrize("typed", ["!banword #badtag slurs", "!banword #othertag | 1d | | | tags",
                                   "!banword hello, #badtag | 1d | | | tags"])
def test_a_word_starting_with_a_hash_is_refused_and_said_so(tmp_path: Path, run: Any, typed: str) -> None:
    """1.2: a line whose first word starts with # is a note: !banword crashed on it (no answer), and a # word anywhere
    catches nothing more than without it."""

    async def scenario() -> None:
        words = HEAD + "badword | 1d | | | slurs\n"
        async with running(tmp_path, settings(tmp_path, words)) as (_, fake, _seen):
            await everyone_joins(fake)
            await fake.chat(FACULTY, "Faculty", typed)
            # Its two lines come as two tells: wait for both.
            await wait_for(lambda: "Not added" in told(fake, FACULTY) and "starts with #" in told(fake, FACULTY)
                           and "badtag also catches #badtag" in told(fake, FACULTY), timeout=10)
            assert (tmp_path / "automod.txt").read_text(encoding="utf-8") == words

    run(scenario())


def test_taking_the_first_word_off_never_turns_a_line_into_a_note(tmp_path: Path) -> None:
    """1.3: `badword, #badtag | 1d, 7d | | 3 | slurs`, then !unbanword badword wrote `#badtag | …`: the whole slurs line
    gone, and the reply only said the word was taken off. change_rule refuses any line that wouldn't read as a rule."""
    path = tmp_path / "automod.txt"
    path.write_text(HEAD + "badword, #badtag | 1d, 7d | | 3 | slurs\n", encoding="utf-8")
    with pytest.raises(RuleError, match="a line starting with #badtag is a note, not a rule: leave the # off"):
        change_rule(path, 2, "#badtag | 1d, 7d | | 3 | slurs")
    with pytest.raises(RuleError, match="is a note"):
        change_rule(path, None, "#new | 1d | | | x")
    assert "badword, #badtag" in path.read_text(encoding="utf-8")


def test_a_pardon_names_a_list_as_banword_does_or_one_taken_off(tmp_path: Path, run: Any) -> None:
    """1.4: only a list's exact name counted, so `slur` (which !banword takes for slurs), or the name of a list taken off
    the word list, was the reason, and every strike they had was cleared."""

    async def scenario() -> None:
        words = HEAD + "badword | 1d, 7d | | | slurs\nrudeword | 1d, 3d | | | rude\n"
        async with running(tmp_path, settings(tmp_path, words)) as (huragok, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 5)
            huragok.storage.execute("INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, action, duration, "
                                    "kick, expires, dry_run, ok, waiting) VALUES (1000, ?, ?, 'Bravo', 'spam', 'spam', 'chat', 'x', 'Vergil', 'mute', '1d', NULL, 0, 0, 1, 0)",
                                    (SERVER, BRAVO))  # a list since taken off
            await fake.chat(MOD, "Mod", "!strike bravo slurs a")
            await asyncio.sleep(3.5)  # past the 3 s between one player's commands
            await fake.chat(MOD, "Mod", "!strike bravo rude b")
            await wait_for(lambda: told(fake, MOD).count("strike 1") == 2)

            def standing() -> list[str]:
                return sorted(r["grp"] for r in huragok.storage.query("SELECT grp FROM automod_strikes WHERE pardoned IS NULL"))

            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo slur misheard")
            await wait_for(lambda: "cleared" in told(fake, FACULTY))
            assert "1 strike cleared (slurs)" in told(fake, FACULTY) and standing() == ["rude", "spam"]
            await asyncio.sleep(3.5)
            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo spam an old list")
            await wait_for(lambda: told(fake, FACULTY).count("cleared") == 2)
            assert standing() == ["rude"]

    run(scenario())


def test_a_pardon_word_that_isnt_a_list_is_refused_and_a_near_miss_asked_about(tmp_path: Path, run: Any) -> None:
    """The host's call (6 November): a word after the player that isn't a list, nor all, is refused, never the reason
    (a typo cleared every strike); one close to a list's name asks "did you mean". A reason for clearing everything
    comes after all."""

    async def scenario() -> None:
        words = HEAD + "badword | 1d, 7d | | | slurs\nrudeword | 1d, 3d | | | rude\n"
        async with running(tmp_path, settings(tmp_path, words)) as (huragok, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!strike bravo slurs a")
            await asyncio.sleep(3.5)
            await fake.chat(MOD, "Mod", "!strike bravo rude b")
            await wait_for(lambda: told(fake, MOD).count("strike 1") == 2)

            def standing() -> int:
                return len(huragok.storage.query("SELECT 1 FROM automod_strikes WHERE pardoned IS NULL"))

            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo slrus misheard")
            await wait_for(lambda: "no list called slrus" in told(fake, FACULTY))
            assert "Did you mean slurs?" in told(fake, FACULTY) and standing() == 2
            await asyncio.sleep(3.5)
            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo he apologised")
            await wait_for(lambda: "no list called he" in told(fake, FACULTY))
            assert "pardonplayer Bravo all <why> clears every strike" in told(fake, FACULTY) and standing() == 2
            await asyncio.sleep(3.5)
            await fake.chat(FACULTY, "Faculty", "!pardonplayer bravo all he apologised")
            await wait_for(lambda: "2 strikes cleared" in told(fake, FACULTY))
            assert standing() == 0

    run(scenario())


def test_a_moderators_strike_that_would_ban_goes_to_the_admins(tmp_path: Path, run: Any) -> None:
    """The host's call (6 November): only Huragok and admins ban. A moderator's strike whose next step is the ban for
    good is held for an admin to confirm (or deny): the admins may well ban them anyway. A moderator can't confirm it."""

    async def scenario() -> None:
        words = HEAD + "badword | 1d | | 1 | slurs\n"
        async with running(tmp_path, settings(tmp_path, words)) as (huragok, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!strike bravo slurs in voice")
            await wait_for(lambda: "slurs strike 1 of 1, muted for 1 day" in told(fake, MOD))
            await asyncio.sleep(3.5)
            await fake.chat(MOD, "Mod", "!strike bravo slurs again")
            await wait_for(lambda: "Sent to the admins as #1" in told(fake, MOD))
            assert not fake.named("ban") and len(huragok.storage.query("SELECT 1 FROM automod_strikes")) == 1
            await wait_for(lambda: "asks an admin, #1" in told(fake, ADMIN))  # the admins are told after the moderator
            assert "asks an admin" not in told(fake, MOD).replace("Sent to the admins", "")
            assert any(e.name == "commands.request" and e.get("needs") == "admin" for e in seen)
            await asyncio.sleep(3.5)
            await fake.chat(MOD, "Mod", "!requests")
            await wait_for(lambda: "(it bans them for good)" in told(fake, MOD))
            await asyncio.sleep(3.5)
            await fake.chat(MOD, "Mod", "!confirm 1")
            await wait_for(lambda: "That's your own request: an admin or above confirms it." in told(fake, MOD))
            assert not fake.named("ban")
            await fake.chat(ADMIN, "Admin", "!confirm 1")
            await wait_for(lambda: fake.named("ban"), timeout=10)
            assert "Confirmed #1" in told(fake, ADMIN)
            assert len(huragok.storage.query("SELECT 1 FROM automod_strikes WHERE action = 'ban'")) == 1
            # An admin's own strike that bans just bans.
            huragok.storage.execute("UPDATE automod_strikes SET pardoned = 1")
            await asyncio.sleep(3.5)
            await fake.chat(ADMIN, "Admin", "!strike alice slurs a")
            await asyncio.sleep(3.5)
            await fake.chat(ADMIN, "Admin", "!strike alice slurs b")
            await wait_for(lambda: len(fake.named("ban")) == 2, timeout=10)

    run(scenario())
