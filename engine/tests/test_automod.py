"""automod, with stand-in words ("badword", "kickword") in place of real slurs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, wait_for
from test_supervisor import SERVER, everyone_joins, running, tells_to

from reclaimer_supervisor.parts.automod.matcher import Matcher, normalise
from reclaimer_supervisor.parts.automod.rules import RuleError, Step, parse_line, parse_text

# The design: three chances with cascading timers then a ban, and a word that only
# ever gets a kick.
WORDS = """\
# words           | timers       | chances | name
badword           | 1h, 1d, 7d   | 3       | slurs
kickword
"""


def rule(line: str):
    parsed = parse_line(line, 1)
    assert parsed is not None
    return parsed


# The word list ---------------------------------------------------------------------------

def test_three_chances_with_cascading_timers_then_a_ban() -> None:
    r = rule("badword, otherword | 1h, 1d, 7d | 3 | slurs")
    assert r.words == ["badword", "otherword"] and r.name == "slurs"
    steps = [r.step_for(n) for n in range(6)]
    assert steps == [Step("kick", "1h"), Step("kick", "1d"), Step("kick", "7d"), Step("ban"), Step("ban"), Step("ban")]
    assert [s.describe() for s in steps[:4]] == ["removed for 1 hour", "removed for 1 day", "removed for 7 days",
                                                  "banned for good"]
    assert r.strike_text(2) == "strike 2 of 3" and r.strike_text(4) == "strike 4"


def test_a_word_alone_is_a_plain_kick_forever() -> None:
    r = rule("kickword")
    assert {r.step_for(n) for n in range(50)} == {Step("kick")} and r.step_for(0).describe() == "kicked"
    assert r.chances is None and r.name == ""


def test_other_shapes() -> None:
    assert [str(rule("w | 1h, 1d | 4").step_for(n)) for n in range(6)] == \
        ["kick 1h", "kick 1d", "kick 1d", "kick 1d", "ban", "ban"]  # the last timer repeats
    assert [str(rule("w | 10m, 1h").step_for(n)) for n in range(3)] == ["kick 10m", "kick 1h", "kick 1h"]
    assert str(rule("w | | 0").step_for(0)) == "ban"  # banned the first time
    assert [str(rule("w | | 2").step_for(n)) for n in range(3)] == ["kick", "kick", "ban"]


def test_severity_order() -> None:
    order = [Step("kick"), Step("kick", "1h"), Step("kick", "7d"), Step("ban")]
    assert sorted(order, key=lambda s: s.severity) == order


@pytest.mark.parametrize("line, message", [
    ("w | 1h, 1d, 7d | 2", "3 timers but 2 chances"),
    ("w | | 0 | x | y", "at most 4 parts"),
    ("w | 1 hour", "isn't a time"),
    ("w | | lots", "chances must be a number"),
    ("w | | 99", "chances must be a number"),
    ("| 1h", "no words"),
    ("-onlyallowed", "no words"),
    ("w | | | two words", "one short word"),
])
def test_mistakes_name_the_line(line: str, message: str) -> None:
    with pytest.raises(RuleError, match=f"line 1: .*{message}"):
        parse_line(line, 1)


def test_the_whole_file() -> None:
    rules = parse_text("# notes\n\n" + WORDS + "otherkick | 30m\n")
    assert [r.name for r in rules] == ["slurs", "rule2", "rule3"]  # unnamed lines still get a name for !strike
    assert rules[0].line == 4
    with pytest.raises(RuleError, match="two lines are named slurs"):
        parse_text("a | | | slurs\nb | | | slurs\n")


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

def setup_words(tmp_path: Path) -> str:
    (tmp_path / "automod.txt").write_text(WORDS, encoding="utf-8")
    return '[automod]\nenabled = true\ncooldown_seconds = 0\n'


def removals(fake) -> list[list[str]]:
    return [c["args"] for c in fake.commands if str(c["command"]) in ("ban", "kick")]


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
            assert all(b["args"][0] == BRAVO and b["by"] == "AutoMod" for b in bans)
            assert bans[0]["args"][2] == ("That isn't allowed here (slurs, strike 1 of 3): removed for 1 hour. "
                                          "Next time: removed for 1 day.")
            assert "banned for good" in bans[3]["args"][1] and "Next time" not in bans[3]["args"][1]
            assert "badword" not in " ".join(str(c) for c in fake.commands)  # the word is never repeated
            ledger = sup.storage.actions_for(BRAVO)
            assert len(ledger) == 4 and ledger[0]["evidence"] == "you b4dw0rd"

            for _ in range(3):  # a word alone on its line: a plain kick every time
                await fake.chat(ALICE, "Alice", "KICKWORD")
            await wait_for(lambda: len(fake.named("kick")) == 3)
            assert all(len(k["args"]) == 2 for k in fake.named("kick"))
            assert fake.named("kick")[0]["args"][1].startswith("That isn't allowed here (strike 1): kicked.")

    run(scenario())


def test_split_lines_names_and_exemptions(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, setup_words(tmp_path)) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(ALICE, "Alice", "you are a bad")
            await fake.chat(ALICE, "Alice", "word")
            ban = await wait_for(lambda: fake.named("ban"))
            assert ban[0]["args"][0] == ALICE
            await fake.chat(ADMIN, "Admin", "badword")  # admins are exempt
            await fake.join(9, "BadWord", "9" * 64)  # names are checked on join
            await wait_for(lambda: len(fake.named("ban")) == 2)
            assert fake.named("ban")[1]["args"][0] == "9" * 64
            assert fake.named("ban")[1]["args"][2].startswith("Change your name")

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

            await fake.chat(BRAVO, "Bravo", "!strikes")
            await wait_for(lambda: any("strike 2 of 3" in t for t in tells_to(fake, BRAVO)))
            await fake.chat(ALICE, "Alice", "!strikes bravo")
            await wait_for(lambda: any("only see your own" in t for t in tells_to(fake, ALICE)))
            await fake.chat(MOD, "Mod", "!pardonplayer bravo")
            await wait_for(lambda: any("admins and up" in t for t in tells_to(fake, MOD)))

            await fake.chat(OWNER, "Owner", "!pardonplayer bravo all false positive")
            await wait_for(lambda: fake.named("unban"))
            assert fake.named("unban")[0]["args"] == [BRAVO]
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
            for n in range(2):
                await fake.chat(BRAVO, "Bravo", "badword")
                await wait_for(lambda: sum(e.name == "automod.strike" for e in seen) == n + 1)
            strikes = [e for e in seen if e.name == "automod.strike"]
            assert [s["action"] for s in strikes] == ["kick 1h", "kick 1d"] and all(s["dry_run"] for s in strikes)
            assert not fake.named("ban") and not fake.named("kick")

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
