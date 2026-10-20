"""automod, with stand-in words ("badword", "rudeword", "kickword") in place of real slurs."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, wait_for
from test_supervisor import SERVER, everyone_joins, running, tells_to

from huragok.parts import PARTS
from huragok.parts.automod.automod import MIGRATIONS, check_messages
from huragok.parts.automod.matcher import Matcher, normalise
from huragok.parts.automod.rules import (Rule, RuleError, Step, describe_seconds, parse_line, parse_text, read_names,
                                         read_rules)
from huragok.parts.automod.rules import ladder as described
from huragok.rcon import Reply
from huragok.state import Player, ServerState
from huragok.storage import Storage

# The design: kicks with cascading times then a ban; mutes from the first strike and
# never a ban; and a word that only ever gets a kick.
WORDS = """\
# words           | kicks        | mutes        | ban | name
badword           | 1h, 1d, 7d   |              | 3   | slurs
rudeword          |              | 0, 1d, 3d    |     | rude
kickword
"""
ZED = "9" * 64


def rule(line: str):
    parsed = parse_line(line, 1)
    assert parsed is not None
    return parsed


def ladder(line: str, strikes: int = 6) -> list[str]:
    r = rule(line)
    return [str(r.step_for(n)) for n in range(strikes)]


# The word list ---------------------------------------------------------------------------

def test_kicks_with_cascading_times_then_a_ban() -> None:
    r = rule("badword, otherword | 1h, 1d, 7d | | 3 | slurs")
    assert r.words == ["badword", "otherword"] and r.name == "slurs" and not r.old_layout
    steps = [r.step_for(n) for n in range(6)]
    assert steps == [Step("kick", "1h"), Step("kick", "1d"), Step("kick", "7d"), Step("ban"), Step("ban"), Step("ban")]
    assert [s.describe() for s in steps[:4]] == ["removed for 1 hour", "removed for 1 day", "removed for 7 days",
                                                  "banned for good"]
    assert r.strike_text(2) == "strike 2 of 3" and r.strike_text(4) == "strike 4"


def test_the_hosts_examples() -> None:
    # Banned the first time.
    assert ladder("bad_word | | | 0 | slurs", 3) == ["ban", "ban", "ban"]
    # Muted the first time, for a day, then 3 days, then a week each time after; never banned.
    r = rule("bad_word | | 0, 1d, 3d, 7d | | transphobia")
    assert [str(r.step_for(n)) for n in range(6)] == ["mute 1d", "mute 3d", "mute 7d", "mute 7d", "mute 7d",
                                                       "mute 7d"]
    assert r.step_for(0).describe() == "muted for 1 day" and r.ban_after is None and r.strike_text(5) == "strike 5"


def test_kicks_then_mutes_then_the_ban() -> None:
    assert ladder("w | 1h | 1, 1d, 7d | 3 | x") == ["kick 1h", "mute 1d", "mute 7d", "ban", "ban", "ban"]
    assert ladder("w | | 2, 1d", 4) == ["kick", "kick", "mute 1d", "mute 1d"]  # no kick times: the server's kick
    assert ladder("w | | 0, 1d, perm", 3) == ["mute 1d", "mute perm", "mute perm"]
    assert rule("w | | 0, perm").step_for(0).describe() == "muted for good"
    assert ladder("w | | 0, 1d, perm | 4", 5) == ["mute 1d", "mute perm", "mute perm", "mute perm", "ban"]


def test_a_word_alone_is_a_plain_kick_forever() -> None:
    r = rule("kickword")
    assert {r.step_for(n) for n in range(50)} == {Step("kick")} and r.step_for(0).describe() == "kicked"
    assert r.ban_after is None and r.mute_after is None and r.name == ""


def test_the_0_2_layout_reads_as_it_always_did() -> None:
    # words | timers | chances | name: four parts, the last a name.
    r = rule("badword | 1h, 1d, 7d | 3 | slurs")
    assert r.old_layout and r.name == "slurs" and r.mute_after is None
    assert [str(r.step_for(n)) for n in range(5)] == ["kick 1h", "kick 1d", "kick 7d", "ban", "ban"]
    assert ladder("troon | | | transphobia", 3) == ["kick", "kick", "kick"]
    assert ladder("worstword | | 0 | threats", 1) == ["ban"]
    assert ladder("w | 1h, 1d | 4 | x") == ["kick 1h", "kick 1d", "kick 1d", "kick 1d", "ban", "ban"]
    # Three parts were words | timers | chances: now the third is the mutes, so it's a mistake, not a mute.
    with pytest.raises(RuleError, match="list how long each mute lasts.*now the 4th"):
        parse_line("w | 1h | 3", 1)


def test_other_shapes() -> None:
    assert ladder("w | 10m, 1h", 3) == ["kick 10m", "kick 1h", "kick 1h"]  # the last time repeats
    assert ladder("w | | | 2", 3) == ["kick", "kick", "ban"]


def test_kick_times_past_the_start_of_the_mutes_kick_then_mute() -> None:
    # The host's rule: out 1 day, 2 days… 15 days; from the 6th strike muted a day as well; banned on the 21st.
    times = ", ".join(f"{n}d" for n in range(1, 16))
    r = rule(f"w | {times} | 5, 1d | 20 | r")
    steps = [r.step_for(n) for n in range(22)]
    assert steps[4] == Step("kick", "5d") and steps[5] == Step("mute", "1d", "6d")
    assert steps[14] == Step("mute", "1d", "15d")
    assert steps[15] == steps[19] == Step("mute", "1d") and steps[20] == steps[21] == Step("ban")
    assert steps[5].describe() == "removed for 6 days, then muted for 1 day" and str(steps[5]) == "kick 6d + mute 1d"
    assert described(r)[4:6] == ["5th: removed for 5 days", "6th: removed for 6 days, then muted for 1 day"]
    assert described(r)[-2:] == ["16th to 20th: muted for 1 day", "21st: banned for good"]
    # Lines that worked before mean what they did: the last kick time repeats up to the mutes, never with them.
    assert ladder("w | 1h | 2, 1d", 4) == ["kick 1h", "kick 1h", "mute 1d", "mute 1d"]
    assert ladder("w | 1h, 1d | 1, 1d", 3) == ["kick 1h", "kick 1d + mute 1d", "mute 1d"]
    assert ladder("w | 1h | 0, 1d, perm", 3) == ["kick 1h + mute 1d", "mute perm", "mute perm"]
    assert rule("w | 1h | 0, perm").step_for(0).describe() == "removed for 1 hour, then muted for good"


def test_no_limit_on_the_counts() -> None:
    r = rule("w | | 100, 1d | 1000 | x")
    assert r.mute_after == 100 and r.ban_after == 1000 and r.strike_text(500) == "strike 500 of 1000"
    assert r.step_for(99) == Step("kick") and r.step_for(999) == Step("mute", "1d") and r.step_for(1000) == Step("ban")
    assert described(r) == ["1st to 100th: kicked", "101st to 1000th: muted for 1 day", "1001st: banned for good"]
    assert described(rule("w | | | 1000000000"))[-1] == "1000000001st: banned for good"  # worked out, not counted


def test_severity_order() -> None:
    order = [Step("kick"), Step("kick", "1h"), Step("kick", "7d"), Step("mute", "1h"), Step("mute", "1h", "1d"),
             Step("mute", "7d"), Step("mute"), Step("ban")]
    assert sorted(reversed(order), key=lambda s: s.severity) == order


@pytest.mark.parametrize("line, message", [
    ("w | 1h, 1d, 7d | | 2", "3 kick times but only 2 kicks before the ban"),
    ("w | 1h, 1d, 7d | 0, 1d | 2", "3 kick times but only 2 kicks before the ban"),  # with the mutes or not
    ("w | 1h, 1d, 7d | 2 | slurs", "3 timers but 2 chances"),  # the 0.2 layout says it the 0.2 way
    ("w | | 0 | x | y | z", "at most 5 parts"),
    ("w | 1 hour", "isn't a time"),
    ("w | | 0, soon", "isn't a time"),
    ("w | perm", "a kick can't be for good"),
    ("w | | 1d, 3d", "mutes start with how many strikes"),
    ("w | | 0", "list how long each mute lasts"),
    ("w | | 0, perm, 1d", "perm can only be the last"),
    ("w | | \u00b2, 1d", "mutes start with how many strikes"),  # ² is a digit to isdigit(), not to int()
    ("w | | | lots |", "the strikes before the ban must be a number"),
    ("w | | | \u00b2 |", "the strikes before the ban must be a number, 0 or more"),
    ("w | | 3, 1d | 2 |", "the mutes start after 3 strikes but the ban comes after 2"),
    ("w | | 1, 1d, 3d, 7d | 3 |", "3 mute times but only 2 mutes before the ban"),
    ("| 1h", "no words"),
    ("-onlyallowed", "no words"),
    ("w | | | | two words", "one short word"),
    ("w | | | | name", "can't be called 'name'"),
])
def test_mistakes_name_the_line(line: str, message: str) -> None:
    with pytest.raises(RuleError, match=f"line 1: .*{message}"):
        parse_line(line, 1)


def test_the_whole_file() -> None:
    rules = parse_text("# notes\n\n" + WORDS + "otherkick | 30m\n")
    assert [r.name for r in rules] == ["slurs", "rude", "rule3", "rule4"]  # unnamed lines still get a name
    assert rules[0].line == 4
    with pytest.raises(RuleError, match="two lines are named slurs"):
        parse_text("a | | | | slurs\nb | | | | slurs\n")


def test_check_points_out_the_old_layout(tmp_path: Path) -> None:
    (tmp_path / "automod.txt").write_text("badword | 1h | 3 | slurs\nrudeword | | 0, 1d | | rude\n", encoding="utf-8")
    warnings = PARTS["automod"].preflight({}, tmp_path)
    assert len(warnings) == 1 and "line 1: the 0.2 layout" in warnings[0] and "words | kicks | mutes" in warnings[0]


def test_how_long_is_left() -> None:
    assert describe_seconds(86399.5) == "23 hours 59 minutes"
    assert describe_seconds(2 * 86400 + 3 * 3600 + 7) == "2 days 3 hours"
    assert describe_seconds(45) == "45 seconds" and describe_seconds(125) == "2 minutes"
    assert describe_seconds(3600) == "1 hour" and describe_seconds(0.2) == "1 second"


# Matching ------------------------------------------------------------------------------

def caught(text: str, line: str) -> bool:
    return bool(Matcher([rule(line)]).find(text))


@pytest.mark.parametrize("text", [
    "badword", "BADWORD", "you badword lol", "b4dw0rd", "b@dw0rd", "b a d w o r d", "b.a.d.w.o.r.d",
    "b-a-d_w*o*r*d", "baaaaadwooooooord", "bаdwоrd",  # Cyrillic а and о
    "bad​word", "bädwörd", "badwords", "badwordz", "BADWORDS!!!", "b4dw0rd " * 20, "(badword)",
])
def test_disguises_are_caught(text: str) -> None:
    assert caught(text, "badword")


@pytest.mark.parametrize("text", ["notbadword", "badwordsmith", "good game", "badwor", ""])
def test_whole_words_only(text: str) -> None:
    assert not caught(text, "badword")


def test_a_split_word_is_still_the_word() -> None:
    # Splitting a word with a space is a common way around filters, so it's caught,
    # even when the halves are words of their own. List words where that can't happen.
    assert caught("bad word", "badword") and caught("ok bad word smith", "badword")


def test_doubled_letters_stay_doubled() -> None:
    assert caught("abbcd", "abbcd") and caught("abbbbbcd", "abbcd") and caught("a b b c d", "abbcd")
    assert not caught("abcd", "abbcd")  # one b: a different word


def test_stars_and_exceptions() -> None:
    assert caught("badwordsmith", "badword*") and not caught("notbadword", "badword*")
    assert caught("notbadwordatall", "*badword*")
    assert caught("so bad", "*bad*, -badminton")
    assert not caught("badminton tonight", "*bad*, -badminton")


def test_phrases() -> None:
    assert caught("just kill yourself", "kill yourself")
    assert caught("k i l l y o u r s e l f", "kill yourself") and caught("killyourself", "kill yourself")
    assert not caught("skill yourself up", "kill yourself")


def test_innocent_chat_is_left_alone() -> None:
    matcher = Matcher([rule("badword, abbcd, kickword, kill yourself")])
    for line in ["gg", "nice shot", "who's on red?", "bad wording there", "I killed you, sorry", "skill issue",
                 "abcd efg", "b a d", "brb", "lol " * 50]:
        assert not matcher.find(line), line


def test_long_lines_stay_fast() -> None:
    matcher = Matcher([rule("*badword*, *abbcd*")])
    started = time.perf_counter()
    matcher.find("b" * 5000 + "a" * 5000 + " ." * 2000)
    assert time.perf_counter() - started < 0.5


def test_normalise() -> None:
    assert normalise("ÀBÇ​dééé") == "abcdee"


# In a game -----------------------------------------------------------------------------

def setup_words(tmp_path: Path, names: str | None = None) -> str:
    (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
    if names is not None:
        (tmp_path / "names.txt").write_text(names, encoding="utf-8")
    return '[automod]\nenabled = true\ncooldown_seconds = 0\n'


def removals(fake) -> list[list[str]]:
    return [c["args"] for c in fake.commands if str(c["command"]) in ("ban", "kick")]


def mutes_of(fake, player_id: str) -> list[list[str]]:
    return [c["args"] for c in fake.named("mute") if c["args"][0] == player_id]


async def vergil_has_caught_up(fake) -> None:
    """Vergil handles events in order: once a joining "kickword" is kicked, what came before is done."""
    kicks = len(fake.named("kick"))
    await fake.join(15, "kickword", "f" * 64)
    await wait_for(lambda: len(fake.named("kick")) > kicks)


def test_the_ladder_in_a_game(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            for n in range(4):
                await fake.chat(BRAVO, "Bravo", "you b4dw0rd")
                await wait_for(lambda: len(removals(fake)) == n + 1)
            bans = fake.named("ban")
            assert [b["args"][1] for b in bans[:3]] == ["1h", "1d", "7d"]
            assert len(bans[3]["args"]) == 2  # no time: for good
            assert all(b["args"][0] == BRAVO and b["by"] == "Vergil" for b in bans)
            assert bans[0]["args"][2] == ("KEEP IT CLEAN! That isn't allowed here (slurs, strike 1 of 3): removed for 1 hour. "
                                          "Next time: removed for 1 day.")
            assert "banned for good" in bans[3]["args"][1] and "Next time" not in bans[3]["args"][1]
            assert "badword" not in " ".join(str(c) for c in fake.commands)  # the word is never repeated
            ledger = sup.storage.actions_for(BRAVO)
            assert len(ledger) == 4 and ledger[0]["evidence"] == "you b4dw0rd"

            for _ in range(3):  # a word alone on its line: a plain kick every time
                await fake.chat(ALICE, "Alice", "KICKWORD")
            await wait_for(lambda: len(fake.named("kick")) == 3)
            assert all(len(k["args"]) == 2 for k in fake.named("kick"))
            assert fake.named("kick")[0]["args"][1].startswith("KEEP IT CLEAN! That isn't allowed here (strike 1): kicked.")

    run(scenario())


def test_mutes_follow_the_ladder_and_last_through_a_rejoin(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            for n in range(3):
                await fake.chat(BRAVO, "Bravo", "rudeword")
                await wait_for(lambda: len(mutes_of(fake, BRAVO)) == n + 1)
            mutes = mutes_of(fake, BRAVO)
            assert [m[1] for m in mutes] == ["1d", "3d", "3d"]  # the last time repeats; never a kick or a ban
            assert mutes[0][2] == ("KEEP IT CLEAN! That isn't allowed here (rude, strike 1): muted for 1 day. "
                                   "Next time: muted for 3 days.")
            assert not removals(fake) and fake.named("mute")[0]["by"] == "Vergil"
            assert [e["action"] for e in seen if e.name == "automod.strike"] == ["mute 1d", "mute 3d", "mute 3d"]

            # The server forgets its mutes when it restarts: Vergil mutes them again as they join.
            await fake.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(mutes_of(fake, BRAVO)) == 4)
            again = mutes_of(fake, BRAVO)[3]
            assert re.fullmatch(r"\d+s", again[1]) and 3 * 86400 - 60 < int(again[1][:-1]) <= 3 * 86400
            assert again[2] == "KEEP IT CLEAN! You're still muted (rude): 2 days 23 hours left."
            assert len(sup.storage.actions_for(BRAVO)) == 4  # in the ledger too

            await fake.chat(BRAVO, "Bravo", "!strikes")
            await wait_for(lambda: any("Muted (rude): 2 days 23 hours left" in t for t in tells_to(fake, BRAVO)))
            assert any("rude: strike 3, next: muted for 3 days" in t for t in tells_to(fake, BRAVO))

            # A mute that ran out isn't someone lifting it; an !unmute is, and Vergil leaves it lifted.
            part = sup.runners["automod"].part
            await fake.event("unmute", name="Bravo", id=BRAVO, by="time")
            await vergil_has_caught_up(fake)
            assert part.active_mute(BRAVO)
            await fake.event("unmute", name="Bravo", id=BRAVO, by="Mod")
            await wait_for(lambda: not part.active_mute(BRAVO))
            await fake.join(4, "Bravo", BRAVO)
            await vergil_has_caught_up(fake)
            assert len(mutes_of(fake, BRAVO)) == 4

    run(scenario())


def test_a_strike_while_away_mutes_them_when_they_join(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", f"!strike {ZED} rude said it in voice last night")
            await wait_for(lambda: any("muted for 1 day (not on now: muted when they next join)" in t
                                       for t in tells_to(fake, MOD)))
            assert not fake.named("mute")
            await fake.join(6, "Zed", ZED)
            mute = await wait_for(lambda: mutes_of(fake, ZED))
            assert mute[0][2].startswith("KEEP IT CLEAN! You're still muted (rude): 23 hours 59 minutes left")

            # !pardonplayer lifts it, and Vergil doesn't put it back.
            await fake.chat(OWNER, "Owner", f"!pardonplayer {ZED}")
            await wait_for(lambda: fake.named("unmute"))
            assert fake.named("unmute")[0]["args"] == [ZED]
            await wait_for(lambda: any("1 strike cleared (rude); unmuted" in t for t in tells_to(fake, OWNER)))
            await fake.join(6, "Zed", ZED)
            await vergil_has_caught_up(fake)
            assert len(mutes_of(fake, ZED)) == 1

    run(scenario())


def test_a_mute_the_server_lost_while_huragok_was_off_comes_back(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", f"!strike {ZED} rude")
            await wait_for(lambda: any("muted when they next join" in t for t in tells_to(fake, MOD)))
            # On already, with no join seen (the server restarted while Huragok was closed).
            sup.states[SERVER].players[6] = Player(6, "Zed", ZED)
            part = sup.runners["automod"].part
            await part.check_names()
            assert len(mutes_of(fake, ZED)) == 1
            sup.states[SERVER].players[6].muted = True  # what the server's mute event does
            part.names_checked.clear()  # as after an edit to a list
            await part.check_names()
            assert len(mutes_of(fake, ZED)) == 1

    run(scenario())


def test_split_lines_and_exemptions(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(ALICE, "Alice", "you are a bad")
            await fake.chat(ALICE, "Alice", "word")
            ban = await wait_for(lambda: fake.named("ban"))
            assert ban[0]["args"][0] == ALICE
            await fake.chat(ADMIN, "Admin", "badword")  # admins are exempt
            await fake.chat(ALICE, "Alice", "kickword")
            await wait_for(lambda: fake.named("kick"))
            assert len(fake.named("ban")) == 1

    run(scenario())


def test_a_bad_name_is_kicked_every_join_until_it_changes(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.join(6, "xX BadWord Xx", ZED)
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"] == [ZED, "KEEP IT CLEAN! Change your name, it isn't allowed here (slurs). "
                                            "Join again with another one."]
            # Let straight back in (the server's kick would keep them out), with no strike and no ban.
            await wait_for(lambda: fake.named("pardon"))
            assert fake.named("pardon")[0]["args"] == [ZED]
            part = sup.runners["automod"].part
            assert not fake.named("ban") and part.active_strikes(ZED, part.by_name["slurs"]) == 0
            await fake.join(6, "xX BadWord Xx", ZED)  # the same name again: kicked again
            await wait_for(lambda: len(fake.named("kick")) == 2)
            await fake.join(6, "Zed", ZED)  # a new name: welcome
            await wait_for(lambda: (6, "Zed") in [(p.number, p.name) for p in sup.states[SERVER].players.values()])
            await part.check_names()
            assert len(fake.named("kick")) == 2 and not fake.named("ban")
            assert [e["caught_by"] for e in seen if e.name == "automod.name"] == ["automod.txt slurs"] * 2

    run(scenario())


def test_strike_name_bans_the_name_and_unbanname_lifts_it(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!strike bravo name offensive")
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"] == [BRAVO, "KEEP IT CLEAN! Change your name, it isn't allowed here (banned name). "
                                              "Join again with another one."]
            assert kick[0]["by"] == "Mod"
            await wait_for(lambda: any("The name Bravo is banned now: they're kicked" in t for t in tells_to(fake, MOD)))
            names = (tmp_path / "names.txt").read_text(encoding="utf-8")
            assert names.startswith("# Vergil's banned names") and re.search(r"# banned [\d-]+ by Mod: offensive\nBravo\n",
                                                                              names)
            assert any(e.name == "automod.name_banned" and e["text"] == "Bravo" for e in seen)

            # Anyone with it, disguised or not, is kicked as they join; no strike for it.
            await fake.join(6, "BR4VO", ZED)
            await wait_for(lambda: len(fake.named("kick")) == 2)
            assert fake.named("kick")[1]["args"][0] == ZED and not fake.named("ban")
            await fake.chat(MOD, "Mod", "!strike bravo name")  # already on the list: not written twice
            await wait_for(lambda: any("was already banned" in t for t in tells_to(fake, MOD)))
            assert (tmp_path / "names.txt").read_text(encoding="utf-8").count("\nBravo\n") == 1

            # Lifting it is for admins.
            await fake.chat(MOD, "Mod", "!unbanname Bravo")
            await wait_for(lambda: any("admins and up" in t for t in tells_to(fake, MOD)))
            await fake.chat(OWNER, "Owner", "!unbanname bravo")
            await wait_for(lambda: any("bravo can be used again" in t for t in tells_to(fake, OWNER)))
            names = (tmp_path / "names.txt").read_text(encoding="utf-8")
            assert "Bravo" not in names and "by Mod" not in names and names.startswith("# Vergil's banned names")
            kicks = len(fake.named("kick"))
            await fake.join(6, "Bravo", ZED)
            await wait_for(lambda: (6, "Bravo") in [(p.number, p.name) for p in sup.states[SERVER].players.values()])
            await sup.runners["automod"].part.check_names()
            assert len(fake.named("kick")) == kicks

            await fake.chat(OWNER, "Owner", "!unbanname Bravo")
            await wait_for(lambda: any("Bravo isn't a banned name" in t for t in tells_to(fake, OWNER)))
            await fake.chat(OWNER, "Owner", "!unbanname badword")
            await wait_for(lambda: any("still caught by automod.txt's slurs line" in t for t in tells_to(fake, OWNER)))
            await fake.chat(MOD, "Mod", "!strike owner name")
            await wait_for(lambda: any("you can't strike them" in t for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!strike bravo")
            await wait_for(lambda: any("or name, to ban their name" in t for t in tells_to(fake, MOD)))

    run(scenario())


def test_banname_bans_a_players_name_or_a_name_itself(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!banname #4")  # Bravo, by number: his name, and he's kicked
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"][0] == BRAVO and kick[0]["by"] == "Mod" and not fake.named("ban")
            await wait_for(lambda: any("The name Bravo is banned now" in t for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!banname xX Edgelord Xx")  # nobody on with it: the name itself
            await wait_for(lambda: any("The name xX Edgelord Xx is banned now" in t for t in tells_to(fake, MOD)))
            names = (tmp_path / "names.txt").read_text(encoding="utf-8")
            assert "\nBravo\n" in names and "\nxX Edgelord Xx\n" in names
            await fake.join(6, "xX Edgelord Xx", ZED)  # whoever joins with it is kicked
            await wait_for(lambda: any(c["args"][0] == ZED for c in fake.named("kick")))
            await fake.chat(MOD, "Mod", "!banname admin")  # the name of someone above them: no
            await wait_for(lambda: any("Admin is an admin; you can't ban their name." in t
                                       for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!banname")
            await wait_for(lambda: any("Use: !banname <#number | name>. Banned names:" in t
                                       for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!unbanname Bravo")  # lifting stays an admin's
            await wait_for(lambda: any("!unbanname is for admins and up" in t for t in tells_to(fake, MOD)))

    run(scenario())


def test_a_name_banned_in_the_file_kicks_whoever_is_on(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path, names="# notes\n")) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            part = sup.runners["automod"].part
            await part.check_names()
            assert not fake.named("kick")
            (tmp_path / "names.txt").write_text("# notes\n*lic*\n", encoding="utf-8")
            await part.reload_if_changed()
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"][0] == ALICE  # "Alice": *lic* is anywhere in a name
            await part.reload_if_changed()  # once is enough while the server takes them out
            assert len(fake.named("kick")) == 1

    run(scenario())


def test_strike_strikes_and_pardonplayer(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            # A moderator's strike, for something said in voice, follows the same ladder.
            await fake.chat(MOD, "Mod", "!strike bravo slurs said it in voice")
            ban = await wait_for(lambda: fake.named("ban"))
            assert ban[0]["args"][1] == "1h" and ban[0]["by"] == "Mod"
            assert ban[0]["args"][2].startswith("Mod gave you a strike (slurs, strike 1 of 3)")
            await fake.chat(MOD, "Mod", "!strike bravo slurs again")
            await wait_for(lambda: len(fake.named("ban")) == 2)
            assert fake.named("ban")[1]["args"][1] == "1d"
            await fake.chat(MOD, "Mod", "!strike alice rude")
            mute = await wait_for(lambda: mutes_of(fake, ALICE))
            assert mute[0][:2] == [ALICE, "1d"] and mute[0][2].startswith("Mod gave you a strike (rude, strike 1)")
            await wait_for(lambda: any("Alice: rude strike 1, muted for 1 day (done)" in t for t in tells_to(fake, MOD)))

            await fake.chat(BRAVO, "Bravo", "!strikes")
            await wait_for(lambda: any("strike 2 of 3" in t for t in tells_to(fake, BRAVO)))
            await fake.chat(ALICE, "Alice", "!strikes bravo")
            await wait_for(lambda: any("only see your own" in t for t in tells_to(fake, ALICE)))
            await fake.chat(MOD, "Mod", "!pardonplayer bravo")
            await wait_for(lambda: any("admins and up" in t for t in tells_to(fake, MOD)))

            await fake.chat(OWNER, "Owner", "!pardonplayer bravo all false positive")
            await wait_for(lambda: fake.named("unban"))
            assert fake.named("unban")[0]["args"] == [BRAVO] and not fake.named("unmute")
            await wait_for(lambda: any("2 strikes cleared" in t for t in tells_to(fake, OWNER)))
            await fake.chat(BRAVO, "Bravo", "badword")  # starts over at the first chance
            await wait_for(lambda: len(fake.named("ban")) == 3)
            assert fake.named("ban")[2]["args"][1] == "1h"
            assert any(e.name == "automod.pardon" for e in seen)

    run(scenario())


def test_dry_run_counts_without_acting(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path) + "dry_run = true\n") as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            for n in range(2):
                await fake.chat(BRAVO, "Bravo", "badword")
                await wait_for(lambda: sum(e.name == "automod.strike" for e in seen) == n + 1)
            await fake.chat(ALICE, "Alice", "rudeword")
            await wait_for(lambda: sum(e.name == "automod.strike" for e in seen) == 3)
            strikes = [e for e in seen if e.name == "automod.strike"]
            assert [s["action"] for s in strikes] == ["kick 1h", "kick 1d", "mute 1d"] and all(s["dry_run"] for s in strikes)
            await fake.join(6, "BadWord", ZED)
            await wait_for(lambda: any(e.name == "automod.name" for e in seen))
            await fake.chat(MOD, "Mod", "!strike bravo name")  # the list is still written; nobody is kicked
            await wait_for(lambda: any("dry run: nobody is kicked for it yet" in t for t in tells_to(fake, MOD)))
            assert "\nBravo\n" in (tmp_path / "names.txt").read_text(encoding="utf-8")
            await sup.runners["automod"].part.check_names()
            assert not fake.named("ban") and not fake.named("kick") and not fake.named("mute")
            assert not fake.named("pardon") and not tells_to(fake, BRAVO)  # not even told they were removed

    run(scenario())


def test_edits_apply_while_running(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            words = tmp_path / "automod.txt"
            words.write_text(WORDS + "newword\n", encoding="utf-8")
            await sup.runners["automod"].part.reload_if_changed()
            await fake.chat(ALICE, "Alice", "newword")
            await wait_for(lambda: fake.named("kick"))
            words.write_text(WORDS + "broken | soon\n", encoding="utf-8")
            await sup.runners["automod"].part.reload_if_changed()  # a mistake: the old rules stay
            await fake.chat(ALICE, "Alice", "newword")
            await wait_for(lambda: len(fake.named("kick")) == 2)

    run(scenario())


# What a word list can't hold ---------------------------------------------------------------

@pytest.mark.parametrize("invisible", [0x200B, 0x0301, 0x3164, 0xFE0F, 0xE0020, 0x2800, 0x115F])
def test_a_word_of_nothing_is_refused_with_its_line(invisible: int) -> None:
    # A list pasted from the web with a zero-width space between two commas would catch almost every line.
    with pytest.raises(RuleError, match=r"line 1: .*has no letters or digits.*would catch almost every line"):
        parse_line(f"badword, {chr(invisible)} | | | 0 | threats", 1)


def test_an_exception_of_nothing_is_dropped_and_a_word_of_nothing_never_matches() -> None:
    assert rule(f"badword, -{chr(0x200B)}").allow == []
    matcher = Matcher([Rule(name="x", label="x", words=[chr(0x200B), f"*{chr(0x0301)}*"])])  # if one got in
    for line in ["nice shot!", "ok.", "gg ", "who's on red?", ""]:
        assert not matcher.find(line), line


# Numbers, disguises, and what must stay innocent --------------------------------------------

@pytest.mark.parametrize("text", ["respawn in 45s", "455 kills", "4:55 left", "wait 4 55", "it was 4-5-5", "Team 455",
                                  "it costs 45$", "a55"])
def test_a_number_is_not_a_word(text: str) -> None:
    assert not caught(text, "ass")


@pytest.mark.parametrize("text", ["a5s", "@$$", "4ss", "a$$", "ASS", "a s s"])
def test_letters_with_stand_ins_still_are(text: str) -> None:
    assert caught(text, "ass")


def test_a_listed_number_is_matched_as_written() -> None:
    assert caught("1488", "1488") and caught("14 88", "1488") and not caught("21488", "1488")
    assert caught("88", "88") and not caught("880", "88")


@pytest.mark.parametrize("text", [
    "b?a?d?w?o?r?d", "b%a%d%w%o%r%d", "b&a&d&w&o&r&d", "b!a!d!w!o!r!d", "b|a|d|w|o|r|d", "b+a+d+w+o+r+d",
    "b@dw()rd", "b-.-.a-.-.d-.-.w-.-.o-.-.r-.-.d",
    f"b{chr(0xE0020)}adword",  # a tag character
    f"bad{chr(0xE0100)}word",  # the variation selector supplement
    f"b{chr(0x2800)}adword", f"bad{chr(0x2063)}word",  # a blank Braille pattern; an invisible separator
])
def test_more_disguises_are_caught(text: str) -> None:
    assert caught(text, "badword")


TYPICAL = ("ass, tit, cum, fag, gay, kys, rape, hoe, sex, nazi, retard, spic, cunt, dick, cock, slut, shit, piss, "
           "homo, dyke, twat, boobs, slag, kill yourself")
INNOCENT = ["class", "assist", "pass the flag", "Scunthorpe", "classic", "passed", "bass", "grass", "cockpit",
            "shitake", "therapist", "skill issue", "analysis", "document", "cucumber", "homosapien", "Sussex", "titan",
            "assume", "grape", "drape", "scum", "Dickens", "cocktail", "spice", "assassin", "glass cannon",
            "embarrassed", "respawn in 45s", "455 kills", "4:55 left", "Team 455", "80085", "717", "5149",
            "it costs 45$", "his stats are 4 5 5", "at 7:17", "gg wp", "nice shot!", "who's on red?", "ok.", "brb",
            "is the sniper up?", "I was so close", "as soon as", "it's a trap", "what? a sec", "hello, is it me",
            "lol " * 30, "bad wording there", "I killed you, sorry", "skill yourself up"]


def test_a_typical_list_leaves_everyday_chat_alone() -> None:
    matcher = Matcher([rule(TYPICAL)])
    caught_lines = [line for line in INNOCENT if matcher.find(line)]
    assert caught_lines == []


# Reading the files -------------------------------------------------------------------------

def bump(path: Path, seconds: float = 0) -> None:
    """A new modification time, as an editor's save gives one (writes this close together may share one)."""
    bump.count = getattr(bump, "count", 0) + 1  # type: ignore[attr-defined]
    when = time.time() + 10 * bump.count + seconds  # type: ignore[attr-defined]
    os.utime(path, (when, when))


def test_word_lists_saved_by_windows_editors_are_read(tmp_path: Path) -> None:
    path = tmp_path / "automod.txt"
    path.write_text("badword | | | 0 | slurs\n", encoding="utf-16")  # Notepad's "Unicode"
    assert [r.name for r in read_rules(path)] == ["slurs"]
    path.write_text("badword | | | 0 | slurs\n", encoding="utf-8-sig")
    assert [r.name for r in read_rules(path)] == ["slurs"]
    path.write_bytes(f"p{chr(0xE9)}d{chr(0xE9)} | | | 0 | slurs\n".encode("cp1252"))  # ANSI
    with pytest.raises(RuleError, match="automod.txt isn't saved as UTF-8 .*save it as UTF-8 in your editor"):
        read_rules(path)
    path.write_bytes("badword\n".encode("utf-16-le"))  # no BOM
    with pytest.raises(RuleError, match="UTF-16 without a BOM"):
        read_rules(path)
    names = tmp_path / "names.txt"
    names.write_text("# notes\nBravo\n", encoding="utf-16")
    assert [n.text for n in read_names(names)] == ["Bravo"]


def words_of(part: Any) -> list[str]:
    return [w for r in part.rules for w in r.words]


def test_a_list_in_another_encoding_or_held_by_an_editor_keeps_the_rules(tmp_path: Path, run, caplog,
                                                                          monkeypatch) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            part = sup.runners["automod"].part
            words = tmp_path / "automod.txt"
            words.write_bytes((WORDS + f"p{chr(0xE9)}d{chr(0xE9)}\n").encode("cp1252"))
            bump(words)
            for _ in range(3):
                await part.reload_if_changed()
            assert caplog.text.count("isn't saved as UTF-8") == 1 and sup.runners["automod"].enabled
            assert "badword" in words_of(part)
            words.write_text(WORDS + "newword\n", encoding="utf-16")
            bump(words)
            await part.reload_if_changed()
            assert "newword" in words_of(part)

            # An editor holding the file while it saves: said once, tried again until it reads.
            real, held = Path.read_bytes, [True]

            def read_bytes(self: Path) -> bytes:
                if self.name == "automod.txt" and held[0]:
                    raise PermissionError(13, "Permission denied", str(self))
                return real(self)

            monkeypatch.setattr(Path, "read_bytes", read_bytes)
            words.write_text(WORDS + "otherword\n", encoding="utf-8")
            bump(words)
            for _ in range(3):
                await part.reload_if_changed()
            assert caplog.text.count("automod.txt can't be read right now (Permission denied)") == 1
            assert "otherword" not in words_of(part) and "newword" in words_of(part)
            held[0] = False
            await part.reload_if_changed()  # the same modification time: read, since it never was
            assert "otherword" in words_of(part) and sup.runners["automod"].enabled

    run(scenario())


def test_the_names_file_in_utf16_is_kept_and_an_unreadable_one_is_left_alone(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path, names="# notes\n")) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            names = tmp_path / "names.txt"
            names.write_text("# notes\nOldName\n", encoding="utf-16")
            await fake.chat(MOD, "Mod", "!strike bravo name")
            await wait_for(lambda: any("The name Bravo is banned now" in t for t in tells_to(fake, MOD)))
            assert names.read_text(encoding="utf-8").splitlines()[:2] == ["# notes", "OldName"]
            names.write_bytes(f"# notes\nRen{chr(0xE9)}\n".encode("cp1252"))
            await fake.chat(MOD, "Mod", "!strike alice name")
            await wait_for(lambda: "The name Alice isn't banned: names.txt can't be changed (names.txt isn't saved "
                           "as UTF-8" in told(fake, MOD))
            assert sup.runners["automod"].enabled

    run(scenario())


# Commands and who they're for ----------------------------------------------------------------

def test_a_moderator_quoting_the_word_in_a_command_isnt_struck(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", "!strike bravo slurs he said badword in voice")
            await wait_for(lambda: fake.named("ban"))
            await fake.chat(MOD, "Mod", "!mute alice 10m for saying badword")
            await wait_for(lambda: mutes_of(fake, ALICE))
            await fake.chat(ALICE, "Alice", "!help badword")  # a player's command line is still chat
            await wait_for(lambda: len(fake.named("ban")) == 2)
            await fake.chat(MOD, "Mod", "!nosuchcommand badword")  # not a command they may use: chat too
            await wait_for(lambda: len(fake.named("ban")) == 3)
            assert [b["args"][0] for b in fake.named("ban")] == [BRAVO, ALICE, MOD]

    run(scenario())


def test_who_a_strike_is_for(tmp_path: Path, run) -> None:
    stranger, cafe, bob, bobby = "3" + "0" * 63, "cafe" + "1" * 60, "b0b" + "2" * 61, "6" * 64

    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            for player_id, name in ((stranger, "Stranger"), (cafe, "Cafe Owner"), (bob, "Bob")):
                sup.storage.record_join(player_id, name, None)  # seen before, not on now
            await everyone_joins(fake)  # Owner #1, Mod #2, Alice #3, Bravo #4, Admin #5
            await fake.join(6, "Bobby", bobby)
            await wait_for(lambda: len(sup.states[SERVER].players) == 6)
            part = sup.runners["automod"].part
            rude = part.by_name["rude"]

            await fake.chat(MOD, "Mod", "!strike 3 rude")  # the console's #3: Alice, not an ID starting with 3
            await wait_for(lambda: mutes_of(fake, ALICE))
            await fake.chat(MOD, "Mod", "!strike cafe rude")  # an ID's start that short picks nobody
            await wait_for(lambda: any("Nobody called 'cafe' is on or has been seen" in t for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!strike cafe1111 rude")  # eight characters or more: theirs
            await wait_for(lambda: any(t.startswith("Cafe Owner: rude strike 1, muted for 1 day (not on now")
                                       for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!strike bob rude")  # Bob left and Bobby is on: Bob's name, seen, wins
            await wait_for(lambda: any(t.startswith("Bob: rude strike 1") for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!strike bobb rude")  # just the start of Bobby's name: asked, not acted on
            await wait_for(lambda: any("Nobody here is called 'bobb'. Did you mean Bobby (#6)? Use #6." in t
                                       for t in tells_to(fake, MOD)))
            await fake.chat(MOD, "Mod", "!strike #9 rude")
            await wait_for(lambda: any("Nobody is #9 on this server now" in t for t in tells_to(fake, MOD)))
            assert [part.active_strikes(p, rude) for p in (ALICE, stranger, cafe, bob, bobby)] == [1, 0, 1, 1, 0]
            assert not mutes_of(fake, bobby)

            await fake.chat(MOD, "Mod", "!strikes bobb")  # looking, too, takes the whole name
            await wait_for(lambda: any("Did you mean Bobby (#6)?" in t for t in tells_to(fake, MOD)[-1:]))
            await fake.chat(MOD, "Mod", "!strikes bob")
            await wait_for(lambda: any(t.startswith("Bob: rude: strike 1") for t in tells_to(fake, MOD)))

    run(scenario())


def test_a_strike_lands_on_the_server_they_are_on(tmp_path: Path, run) -> None:
    class Other:
        connected = True

        def __init__(self) -> None:
            self.sent: list[tuple[str, tuple[str, ...], str | None]] = []

        async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
            self.sent.append((command, args, by))
            return Reply(True, f"Done: {command}", None, {})

    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            other = Other()
            sup.clients["Other"] = other  # type: ignore[assignment]
            sup.states["Other"] = ServerState("Other", connected=True, players={2: Player(2, "Zed", ZED)})
            try:
                part = sup.runners["automod"].part
                await fake.chat(MOD, "Mod", f"!strike {ZED} rule3")  # kickword's line: a plain kick, where Zed is
                await wait_for(lambda: other.sent)
                assert other.sent[0][0] == "kick" and other.sent[0][1][0] == ZED and other.sent[0][2] == "Mod"
                assert not fake.named("kick")
                del sup.states["Other"].players[2]  # on no server now: nobody to kick, and the strike counts
                await fake.chat(MOD, "Mod", f"!strike {ZED} rule3")
                await wait_for(lambda: any("not on now, so nobody was kicked; the strike counts" in t
                                           for t in tells_to(fake, MOD)))
                assert part.active_strikes(ZED, part.by_name["rule3"]) == 2 and len(other.sent) == 1
                assert not fake.named("kick")
            finally:
                sup.states.pop("Other", None)
                sup.clients.pop("Other", None)

    run(scenario())


# Mutes that come back -------------------------------------------------------------------------

def test_mutes_come_back_after_a_restart_unseen_and_after_a_shorter_mute(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            part = sup.runners["automod"].part
            bravo = sup.states[SERVER].players[4]
            await fake.chat(BRAVO, "Bravo", "rudeword")  # rude: muted for a day
            await wait_for(lambda: len(mutes_of(fake, BRAVO)) == 1)
            # The server has the mute before Vergil has its answer and writes the strike down: wait for that too.
            await wait_for(lambda: part.active_mute(BRAVO) is not None)
            bravo.muted = True  # as the server's mute event sets it
            assert (BRAVO, "Bravo") in part.names_checked

            # The server restarted while the console was away; Bravo is back, unmuted, with no join seen.
            bravo.muted = False
            await part.check_names()
            assert len(mutes_of(fake, BRAVO)) == 2
            await part.check_names()  # once is enough while the server's mute event is on its way
            assert len(mutes_of(fake, BRAVO)) == 2
            sup.bus.emit("rcon.connected", SERVER, version="0.9.18")  # a reconnect: everyone looked at again
            await wait_for(lambda: not part.names_checked and not part.mutes_restored)
            await part.check_names()
            assert len(mutes_of(fake, BRAVO)) == 3

            # A moderator's shorter mute replaced Vergil's; when it runs out, Vergil's is put back.
            await fake.chat(MOD, "Mod", "!mute bravo 30s")
            await wait_for(lambda: len(mutes_of(fake, BRAVO)) == 4)
            part.mutes_restored[BRAVO] = (time.time() - 10, bravo.joined)
            await fake.event("unmute", name="Bravo", id=BRAVO, by="time")
            await wait_for(lambda: len(mutes_of(fake, BRAVO)) == 5)
            again = mutes_of(fake, BRAVO)[4]
            assert again[1].endswith("s") and int(again[1][:-1]) > 86000 and again[2].startswith("KEEP IT CLEAN!")

            # Vergil's own mute ending as the server's does isn't put back.
            part.ctx.db.execute("UPDATE automod_strikes SET expires = ? WHERE player_id = ?", (time.time() + 20, BRAVO))
            part.mutes_restored[BRAVO] = (time.time() - 10, bravo.joined)
            await fake.event("unmute", name="Bravo", id=BRAVO, by="time")
            await vergil_has_caught_up(fake)
            assert len(mutes_of(fake, BRAVO)) == 5

    run(scenario())


def test_unmute_reaches_a_player_who_left_and_ends_vergils_mute(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            part = sup.runners["automod"].part
            await fake.chat(BRAVO, "Bravo", "rudeword")
            await wait_for(lambda: mutes_of(fake, BRAVO))
            await fake.event("leave", number=4, name="Bravo", id=BRAVO)
            await wait_for(lambda: 4 not in sup.states[SERVER].players)
            await fake.chat(MOD, "Mod", "!unmute bravo")  # gone, but seen: unmuted by ID
            await wait_for(lambda: fake.named("unmute"))
            assert fake.named("unmute")[0]["args"] == [BRAVO] and fake.named("unmute")[0]["by"] == "Mod"
            await wait_for(lambda: not part.active_mute(BRAVO))
            await fake.join(4, "Bravo", BRAVO)
            await vergil_has_caught_up(fake)
            assert len(mutes_of(fake, BRAVO)) == 1

    run(scenario())


# !pardonplayer and the server's ban list -----------------------------------------------------

def told(fake: Any, player_id: str) -> str:
    """Everything this player was told, as one text: a long reply arrives as several lines."""
    return " ".join(tells_to(fake, player_id))


def add_strike(part: Any, player_id: str, *, action: str = "ban", duration: str | None = None, ok: bool = True,
               when: float | None = None, by: str = "Vergil", rule_name: str = "slurs") -> None:
    when = time.time() if when is None else when
    expires = when + 3600 if duration else None
    part.ctx.db.execute(
        "INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, action, "
        "duration, expires, dry_run, ok) VALUES (?, ?, ?, ?, ?, ?, 'chat', 'x', ?, ?, ?, ?, 0, ?)",
        (when, SERVER, player_id, "x", rule_name, rule_name, by, action, duration, expires, int(ok)))


def test_pardonplayer_lifts_only_the_bans_automod_gave(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            part = sup.runners["automod"].part
            await fake.chat(BRAVO, "Bravo", "badword")  # slurs: out 1 hour, a timed ban
            await wait_for(lambda: fake.ban_list)
            assert fake.ban_list[0]["banned_by"] == "Vergil (RCON)"
            fake.ban_list.append({"id": BRAVO, "name": "Bravo", "reason": "cheating", "banned_by": "Owner (RCON)",
                                  "banned_at": int(time.time()) - 86400})
            await fake.chat(ADMIN, "Admin", "!pardonplayer bravo")
            await wait_for(lambda: any(t.startswith("Pardoned Bravo") for t in tells_to(fake, ADMIN)))
            assert not fake.named("unban") and len(fake.ban_list) == 2
            assert "ban not lifted: Owner banned them too (cheating), and the server's unban lifts every ban on them; " \
                   "!unban lifts both" in told(fake, ADMIN)

            # Automod's ban that timed out (the server may have run it): it's in the list, so it's lifted.
            add_strike(part, ALICE, ok=False)
            fake.ban_list.append({"id": ALICE, "name": "Alice", "reason": "x", "banned_by": "Vergil (RCON)",
                                  "banned_at": int(time.time())})
            await fake.chat(ADMIN, "Admin", "!pardonplayer alice")
            await wait_for(lambda: fake.named("unban"))
            assert fake.named("unban")[0]["args"] == [ALICE]
            await wait_for(lambda: any("1 strike cleared (slurs); ban lifted." in t for t in tells_to(fake, ADMIN)))

            # Someone else's ban alone: kept, and said so.
            add_strike(part, ZED, when=time.time() - 7 * 86400)
            fake.ban_list.append({"id": ZED, "name": "Zed", "reason": "griefing", "banned_by": "Owner (RCON)",
                                  "banned_at": int(time.time())})
            await fake.chat(ADMIN, "Admin", f"!pardonplayer {ZED}")
            await wait_for(lambda: "Owner's ban stays (griefing): it isn't automod's; !unban lifts it"
                           in told(fake, ADMIN))
            assert len(fake.named("unban")) == 1

            # The ban list can't be read: nothing is lifted.
            add_strike(part, MOD)
            fake.replies["bans"] = lambda m: {"ok": False, "text": "The ban list can't be read."}
            await fake.chat(OWNER, "Owner", "!pardonplayer mod")
            await wait_for(lambda: "ban not lifted: the ban list couldn't be read (The ban list can't be read.)"
                           in told(fake, OWNER))
            assert len(fake.named("unban")) == 1

    run(scenario())


def test_a_kick_with_a_mute_removes_them_then_mutes_them_once_back(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        (tmp_path / "automod.txt").write_text("badword | 1h, 2h | 1, 1d | | slurs\nrudeword | 30m | 0, 1d | | rude\n",
                                              encoding="utf-8")
        async with running(tmp_path, '[automod]\nenabled = true\ncooldown_seconds = 0\n') as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            part = sup.runners["automod"].part
            for n in range(2):
                await fake.chat(BRAVO, "Bravo", "badword")
                await wait_for(lambda: len(fake.named("ban")) == n + 1)
            first, second = (b["args"] for b in fake.named("ban"))
            assert first[:2] == [BRAVO, "1h"] and second[:2] == [BRAVO, "2h"]
            assert second[2] == ("KEEP IT CLEAN! That isn't allowed here (slurs, strike 2): removed for 2 hours, then "
                                 "muted for 1 day. Next time: muted for 1 day.")
            assert [e["action"] for e in seen if e.name == "automod.strike"] == ["kick 1h", "kick 2h + mute 1d"]
            await fake.leave(4)  # the server takes them out
            await wait_for(lambda: all(p.id != BRAVO for p in sup.states[SERVER].players.values()))
            row = part.active_mute(BRAVO)
            assert row["kick"] == "2h" and row["duration"] == "1d"
            assert abs(row["expires"] - (row["time"] + 2 * 3600 + 86400)) < 1  # the day starts once they're back

            # Back once the 2 hours are up: muted for the day, not for what's left of a day from the strike.
            part.ctx.db.execute("UPDATE automod_strikes SET time = time - 7200, expires = expires - 7200 "
                                "WHERE kick IS NOT NULL")
            before = len(mutes_of(fake, BRAVO))
            await fake.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(mutes_of(fake, BRAVO)) > before)
            again = mutes_of(fake, BRAVO)[-1]
            assert 86400 - 60 < int(again[1][:-1]) <= 86400
            assert again[2].startswith("KEEP IT CLEAN! You're still muted (slurs): 23 hours 59 minutes")

            # !pardonplayer lifts both: the removal, and the mute waiting for them.
            await fake.chat(ALICE, "Alice", "rudeword")  # rude's 1st: out 30 minutes, then muted a day
            await wait_for(lambda: len(fake.named("ban")) == 3)
            assert fake.named("ban")[2]["args"][:2] == [ALICE, "30m"]
            await fake.leave(3)
            await wait_for(lambda: all(p.id != ALICE for p in sup.states[SERVER].players.values()))
            await fake.chat(ADMIN, "Admin", "!pardonplayer alice")
            await wait_for(lambda: "1 strike cleared (rude); ban lifted, unmuted." in told(fake, ADMIN))
            assert fake.named("unban")[0]["args"] == [ALICE] and fake.named("unmute")[-1]["args"] == [ALICE]
            assert not part.active_mute(ALICE) and all(e["id"] != ALICE for e in fake.ban_list)

    run(scenario())


# Messages, unnamed lines, cooldowns, forgetting --------------------------------------------

def test_a_brace_in_a_custom_message_is_found_before_it_breaks_a_strike(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"messages: chat can't be filled in .*write \{\{ or \}\} for a brace"):
        check_messages({"chat": "Clean it up :{ {action}"})
    with pytest.raises(ValueError, match="messages: muted can't be filled in"):
        check_messages({"muted": "{0} left"})
    assert check_messages({"chat": "{{rules}} {detail}"})["chat"] == "{{rules}} {detail}"
    (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
    with pytest.raises(ValueError, match="messages: name can't be filled in"):
        PARTS["automod"].preflight({"messages": {"name": "bad }"}}, tmp_path)  # so `check` reports it


def test_a_message_that_fails_at_run_time_falls_back_to_the_default(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            sup.runners["automod"].part.messages["chat"] = "oops {"
            await fake.chat(ALICE, "Alice", "kickword")
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"][1].startswith("KEEP IT CLEAN! That isn't allowed here")
            assert "messages: chat can't be filled in" in caplog.text and sup.runners["automod"].enabled

    run(scenario())


UNNAMED = "kickword\nbadword | 1h, 1d | | 3\nrudeword | | 0, 1d | | rude\nother | 10m\n"
UNNAMED_WARNING = ("automod.txt line 2 has no name, so its strikes are kept under rule2, and they move to another line "
                   "if a line is added above it: name it (the 5th part, words | kicks | mutes | ban | name)")


def test_check_and_the_start_warn_about_unnamed_lines_whose_strikes_add_up(tmp_path: Path, run, caplog) -> None:
    (tmp_path / "automod.txt").write_text(UNNAMED, encoding="utf-8")
    assert PARTS["automod"].preflight({}, tmp_path) == [UNNAMED_WARNING]
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, "[automod]\nenabled = true\n") as (sup, fake, _):
            await wait_for(lambda: UNNAMED_WARNING in caplog.text)
            assert caplog.text.count("has no name") == 1

    run(scenario())


def test_lines_close_together_are_one_strike_by_default(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        setup_words(tmp_path)
        async with running(tmp_path, "[automod]\nenabled = true\n") as (sup, fake, _):  # cooldown_seconds: 10
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(BRAVO, "Bravo", "badword")
            await fake.chat(BRAVO, "Bravo", "BADWORD!!")
            await wait_for(lambda: fake.named("ban"))
            await vergil_has_caught_up(fake)
            part = sup.runners["automod"].part
            assert len(fake.named("ban")) == 1 and part.active_strikes(BRAVO, part.by_name["slurs"]) == 1

    run(scenario())


def test_forget_after_days_starts_the_ladder_again(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path) + "forget_after_days = 1\n") as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            part = sup.runners["automod"].part
            add_strike(part, BRAVO, action="kick", duration="1h", when=time.time() - 2 * 86400)
            add_strike(part, ALICE, action="kick", duration="1h", when=time.time() - 3600)
            await fake.chat(BRAVO, "Bravo", "badword")  # two days ago: forgotten, so the first step again
            await fake.chat(ALICE, "Alice", "badword")  # an hour ago: still counts, so the second
            await wait_for(lambda: len(fake.named("ban")) == 2)
            assert {b["args"][0]: b["args"][1] for b in fake.named("ban")} == {BRAVO: "1h", ALICE: "1d"}

    run(scenario())


def test_a_database_from_0_2_gets_the_lifted_columns_and_keeps_its_strikes(tmp_path: Path) -> None:
    path = tmp_path / "huragok.sqlite"
    old = Storage(path)
    old.migrate("automod", MIGRATIONS[:1])  # as 0.2.0 left it
    old.execute("INSERT INTO automod_strikes (time, player_id, rule, grp, source, by, action, dry_run, ok) "
                "VALUES (1, ?, 'slurs', 'slurs', 'chat', 'Vergil', 'mute', 0, 1)", (BRAVO,))
    old.close()
    storage = Storage(path)
    storage.migrate("automod", MIGRATIONS)
    rows = storage.query("SELECT * FROM automod_strikes")
    assert len(rows) == 1 and rows[0]["player_id"] == BRAVO and rows[0]["lifted"] is None and rows[0]["kick"] is None
    assert storage.query("SELECT version FROM schema_versions WHERE owner = 'automod'")[0]["version"] == 3
    storage.close()


def test_a_database_from_0_4_still_reads_after_a_roll_back(tmp_path: Path) -> None:
    """The kick column (a mute that came with a kick) is added to 0.4's database, and a release that doesn't know
    it (the start loop putting the previous one back) skips the step it hasn't got and goes on writing strikes."""
    path = tmp_path / "huragok.sqlite"
    old = Storage(path)
    old.migrate("automod", MIGRATIONS[:2])  # as 0.4.0 to 0.4.3 left it
    old.close()
    storage = Storage(path)
    storage.migrate("automod", MIGRATIONS)
    storage.execute("INSERT INTO automod_strikes (time, player_id, rule, grp, source, by, action, duration, kick, "
                    "dry_run, ok) VALUES (1, ?, 'slurs', 'slurs', 'chat', 'Vergil', 'mute', '1d', '2h', 0, 1)",
                    (BRAVO,))
    storage.close()
    back = Storage(path)
    back.migrate("automod", MIGRATIONS[:2])
    back.execute("INSERT INTO automod_strikes (time, player_id, rule, grp, source, by, action, dry_run, ok) "
                 "VALUES (2, ?, 'slurs', 'slurs', 'chat', 'Vergil', 'kick', 0, 1)", (BRAVO,))
    assert [r["kick"] for r in back.query("SELECT * FROM automod_strikes ORDER BY time")] == ["2h", None]
    back.close()
