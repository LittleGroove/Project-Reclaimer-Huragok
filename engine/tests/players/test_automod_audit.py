"""Vergil's findings from the audit of November 2026 (rows 5.3, 5.4, 5.5, 5.10 and 5.13), each with what it does now."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import ALICE, BRAVO, wait_for
from testkit.running import SERVER, everyone_joins, running

from huragok.players.automod.matcher import Matcher
from huragok.players.automod.rules import RuleError, change_rule, parse_line, read_rules_lenient

WORDS = """\
# words           | mutes        | kicks        | ban | name
badword           |              | 1h, 1d, 7d   | 3   | slurs
askword           |              |              |     | ask
quietword         | 10m          | 0            |     | quiet
"""


def removals(fake) -> list[list[str]]:
    return [c["args"] for c in fake.commands if str(c["command"]) in ("ban", "kick", "mute")]


def stand_in_report(sup) -> list[str]:
    """!report as the Discord part has it while it takes reports: what it was given."""
    taken: list[str] = []

    async def report(call) -> None:
        taken.append(call.text)
        await call.reply("Thanks: the staff have it on Discord.")

    registered = sup.vergil.commands.commands["report"]
    registered.spec, registered.method, registered.runner = replace(registered.spec, when=""), report, None
    return taken


def test_a_report_quoting_a_word_goes_unstruck_once_a_minute_not_every_line(tmp_path: Path, run) -> None:
    """Audit 2026-11, 5.3: while Discord took reports, every line starting !report skipped the word list, refused or
    naming nobody, though everyone reads it. Now one a minute quoting a word goes unstruck; the next is chat."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            taken = stand_in_report(sup)
            await fake.chat(ALICE, "Alice", "!report Bravo he called me a badword")
            await wait_for(lambda: taken)
            await asyncio.sleep(0.5)
            assert not removals(fake)  # the reporter isn't struck for what they're reporting
            await fake.chat(ALICE, "Alice", "!report Bravo you badword badword")
            await wait_for(lambda: removals(fake), timeout=10)  # the second within the minute: struck as chat is
            assert removals(fake)[0][0] == ALICE
            await fake.chat(BRAVO, "Bravo", "!report")  # names nobody, quotes nothing: nothing to do
            automod = sup.runners["automod"].part
            automod.quoted.clear()
            assert automod._telling_the_staff(automod._player(SERVER, BRAVO, "Bravo"), "!report Alice badword", time.time())
            assert not automod._telling_the_staff(automod._player(SERVER, BRAVO, "Bravo"), "!report", time.time())

    run(scenario())


def test_a_word_that_only_reports_doesnt_hold_up_a_strike(tmp_path: Path, run) -> None:
    """Audit 2026-11, 5.4: a report-only word set the strike's cooldown (10 s by default), so the slurs after it in
    those 10 s were nothing: no strike, no ticket. Reports have their timer, strikes theirs."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\n") as (sup, fake, seen):  # cooldown_seconds: 10
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ALICE, "Alice", "askword")
            await wait_for(lambda: [e for e in seen if e.name == "automod.heard"])
            await fake.chat(ALICE, "Alice", "askword again")  # a burst of reports is still one
            await fake.chat(ALICE, "Alice", "you badword")
            await wait_for(lambda: [e for e in seen if e.name == "automod.strike"], timeout=10)
            assert len([e for e in seen if e.name == "automod.heard"]) == 1
            assert removals(fake) and removals(fake)[0][0] == ALICE

    run(scenario())


@pytest.mark.parametrize(("text", "caught"), [
    ("badword1", True), ("1badword", True), ("kys2", True), ("b4dw0rd1", True), ("badwords1", True), ("kys 2", True),
    ("badword_1", True), ("4ss4ss1n", False), ("x1badword", False), ("badword1x", False), ("glass", False),
    ("class2", False), ("455 kills", False), ("a55", False), ("respawn in 45s", False), ("mike1988", False),
    ("88", True), ("sniper88", False),
])
def test_digits_glued_to_a_word_dont_hide_it(text: str, caught: bool) -> None:
    """Audit 2026-11, 5.5: the word's edges counted digits as letters, so badword1, 1badword and kys2 got through
    (kys 2 and badword_1 didn't). A digit at the edge of a word that's mostly letters is an edge now; digits joined to
    more letters still make a longer word, and a number is still never a word."""
    matcher = Matcher([parse_line("badword, kys, ass, 88 | 1d | | | x", 1)])
    assert bool(matcher.find(text)) is caught, text


def test_a_mute_after_the_servers_own_kick_starts_when_theyre_back(tmp_path: Path, run) -> None:
    """Audit 2026-11, 5.10: a "kick, then mute" with the server's own kick counted the mute from the strike, and the
    server keeps a kicked player out 10 to 120 minutes, so a 10 minute mute was over before they were back. Its time
    starts when they are; so does one given while they're on no server."""
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(BRAVO, "Bravo", "quietword")
            await wait_for(lambda: any(kick["args"][0] == BRAVO for kick in fake.named("kick")))
            db = sup.storage
            row = db.query("SELECT * FROM automod_strikes WHERE player_id = ?", (BRAVO,))[0]
            assert row["action"] == "mute" and row["duration"] == "10m" and row["waiting"] == 1
            # The server kept them out half an hour: the strike was then.
            db.execute("UPDATE automod_strikes SET time = time - 1800, expires = expires - 1800 WHERE number = ?", (row["number"],))
            automod = sup.runners["automod"].part
            assert automod.active_mute(BRAVO, waiting=True) is not None and automod.active_mute(BRAVO) is None
            assert "muted 10m once they're back" in await sup.runners["commands"].part._whois_now(BRAVO, time.time())  # !whois says so too
            await fake.leave(4)
            await fake.join(4, "Bravo", BRAVO)
            await wait_for(lambda: [m for m in fake.named("mute") if m["args"][0] == BRAVO], timeout=10)
            mute = [m for m in fake.named("mute") if m["args"][0] == BRAVO][-1]
            assert 590 <= int(str(mute["args"][1]).rstrip("s")) <= 600  # the whole 10 minutes, from now
            row = db.query("SELECT * FROM automod_strikes WHERE player_id = ?", (BRAVO,))[0]
            assert row["waiting"] == 0 and row["expires"] > time.time() + 500

    run(scenario())


def test_an_unmute_while_theyre_away_ends_a_mute_waiting_for_them(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
        async with running(tmp_path, "[automod]\nenabled = true\ncooldown_seconds = 0\n") as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(BRAVO, "Bravo", "quietword")
            await wait_for(lambda: any(kick["args"][0] == BRAVO for kick in fake.named("kick")))
            sup.storage.execute("UPDATE automod_strikes SET time = time - 1800, expires = expires - 1800")
            automod = sup.runners["automod"].part
            automod._lift(SERVER, BRAVO, "Bravo", "Mod")
            await fake.leave(4)
            await fake.join(4, "Bravo", BRAVO)
            await asyncio.sleep(1.0)
            assert not [m for m in fake.named("mute") if m["args"][0] == BRAVO]

    run(scenario())


def test_a_line_vergil_runs_without_doesnt_stop_changes_to_the_others(tmp_path: Path) -> None:
    """Audit 2026-11, 5.13: change_rule read the whole list strictly, so one line the lenient reading leaves out made
    every !banword, !unbanword and !changebanword fail with that line's error. A change fails only on what it brings."""
    path = tmp_path / "automod.txt"
    path.write_text("badword | | 1h | 3 | slurs\nbroken | 1x\nrude | 1d\n", encoding="utf-8")
    rules = change_rule(path, None, "newword | | | | watch")  # !banword's new line
    assert [r.name for r in rules] == ["slurs", "rule3", "watch"]
    change_rule(path, 1, "badword, otherword | | 1h | 3 | slurs")  # !banword adding to a line
    change_rule(path, 3, None)  # !unbanword's last word of a line
    assert [r.name for r in read_rules_lenient(path)[0]] == ["slurs", "watch"]
    assert "broken | 1x" in path.read_text(encoding="utf-8")  # still left out, and still said, as it was
    before = path.read_text(encoding="utf-8")
    with pytest.raises(RuleError, match="'2x' isn't a time"):
        change_rule(path, None, "another | 2x")  # what a change brings still stops it
    with pytest.raises(RuleError, match="is named slurs already"):
        change_rule(path, None, "dupe | 1d | | | slurs")
    assert path.read_text(encoding="utf-8") == before
