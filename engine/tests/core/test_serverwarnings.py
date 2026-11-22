"""Huragok's warnings (0.7.0, plan section 5): words as data, said once, cleared when gone, dismissed until the file
changes, and one line for a server newer than every rule was checked on."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from huragok.core.events import EventBus
from huragok.core.serverwarnings import CHECKED_UP_TO, RuleError, Warnings, load_rules, version_key

RULES = """
[[rule]]
id = "checked-up-to"
kind = "info"
text = "Checked up to {checked}; it runs {version}."
checked = "0.10.9"

[[rule]]
id = "refused"
kind = "refused"
text = "The server refuses {file}: {why}."
fix = "Fix {file}."
checked = "0.10.8"
discord = true

[[rule]]
id = "guess"
kind = "check"
text = "Game {game} looks wrong."
checked = "0.10.9"
"""


def _warnings(tmp_path: Path, bus: EventBus | None = None) -> Warnings:
    return Warnings(load_rules(RULES), tmp_path / "records", bus, clock=lambda: 1000.0)


def test_the_shipped_rules_load_and_every_one_was_checked() -> None:
    rules = load_rules()
    assert CHECKED_UP_TO in rules and "no-console" in rules
    assert all(rule.checked for rule in rules.values())


def test_a_rule_that_cant_be_used_is_refused_with_why() -> None:
    with pytest.raises(RuleError, match="needs an id, a kind"):
        load_rules('[[rule]]\nid = "x"\nkind = "loud"\ntext = "y"\n')
    with pytest.raises(RuleError, match="two rules are called x"):
        load_rules('[[rule]]\nid = "x"\nkind = "info"\ntext = "a"\n[[rule]]\nid = "x"\nkind = "info"\ntext = "b"\n')


def test_a_warning_is_said_once_kept_up_to_date_and_cleared(tmp_path: Path) -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("warning.*", lambda event: seen.append(f"{event.name} {event.get('id')} {event.get('discord')}"))
    warnings = _warnings(tmp_path, bus)
    first = warnings.set("Alpha", "refused", "big", about="big.json", why="game 4", file="big")
    assert first is not None and first.line() == "Refused: The server refuses big: game 4. Fix big."
    warnings.set("Alpha", "refused", "big", about="big.json", why="game 4", file="big")
    assert seen == ["warning.raised refused:big True"]  # said once
    warnings.set("Alpha", "refused", "big", about="big.json", why="game 5", file="big")
    assert warnings.of("Alpha")[0].text == "The server refuses big: game 5."
    assert warnings.clear("Alpha", "refused", "big") and seen[-1] == "warning.cleared refused:big True"
    assert warnings.of("Alpha") == [] and not warnings.clear("Alpha", "refused", "big")
    assert warnings.set("Alpha", "nonsense") is None


def test_a_checker_hands_over_everything_it_found_and_what_it_didnt_is_cleared(tmp_path: Path) -> None:
    warnings = _warnings(tmp_path)
    warnings.sync("Alpha", "plays", [("guess", "6", None, {"game": 6}), ("guess", "7", None, {"game": 7})])
    warnings.set("Alpha", "guess", "9", source="other", game=9)
    warnings.sync("Alpha", "plays", [("guess", "7", None, {"game": 7})])
    assert [w.id for w in warnings.of("Alpha")] == ["guess:7", "guess:9"]  # another checker's stays


def test_refused_comes_first_and_the_summary_counts(tmp_path: Path) -> None:
    warnings = _warnings(tmp_path)
    warnings.set("Alpha", "guess", "6", game=6)
    warnings.set("Alpha", "refused", "big", why="x", file="big")
    assert [w.kind for w in warnings.of("Alpha")] == ["refused", "check"]
    assert warnings.summary("Alpha") == "1 refused · 1 to check" and warnings.summary("Bravo") == ""


def test_a_dismissed_warning_stays_hidden_until_its_file_changes(tmp_path: Path) -> None:
    playlist = tmp_path / "big.playlist.json"
    playlist.write_text("{}", encoding="utf-8")
    warnings = _warnings(tmp_path)
    warnings.set("Alpha", "refused", "big", about=playlist, why="x", file="big")
    warnings.set("Alpha", "guess", "6", game=6)  # about no file: dismissed for good
    assert warnings.dismiss("Alpha", "refused:big") and warnings.dismiss("Alpha", "guess:6")
    assert warnings.of("Alpha") == [] and len(warnings.of("Alpha", dismissed=True)) == 2
    again = _warnings(tmp_path)  # kept across a restart of Huragok
    again.set("Alpha", "refused", "big", about=playlist, why="x", file="big")
    assert again.of("Alpha") == []
    playlist.write_text('{"games": []}', encoding="utf-8")
    os.utime(playlist, ns=(1, 1))
    assert [w.id for w in again.of("Alpha")] == ["refused:big"]  # the host changed it: shown again
    assert again.undismiss("Alpha") == 2 and len(again.of("Alpha")) == 1
    assert not again.dismiss("Alpha", "nothing")


def test_a_server_newer_than_every_rule_gets_one_line(tmp_path: Path) -> None:
    warnings = _warnings(tmp_path)
    assert warnings.newest_checked == "0.10.9"
    warnings.version("Alpha", "0.10.9")
    assert warnings.of("Alpha") == []
    warnings.version("Alpha", "0.10.10")
    assert [w.text for w in warnings.of("Alpha")] == ["Checked up to 0.10.9; it runs 0.10.10."]
    warnings.version("Alpha", "0.10.9")
    assert warnings.of("Alpha") == []
    assert version_key("0.10.10") > version_key("0.10.9") > version_key("0.9.23") and version_key(None) == ()


def test_a_renamed_server_keeps_its_warnings_and_dismissals(tmp_path: Path) -> None:
    warnings = _warnings(tmp_path)
    warnings.set("Old", "guess", "6", game=6)
    warnings.set("Old", "guess", "7", game=7)
    warnings.dismiss("Old", "guess:7")
    warnings.rename("Old", "New")
    assert [w.id for w in warnings.of("New")] == ["guess:6"] and warnings.of("Old") == []
    assert warnings.of("New")[0].server == "New"
    warnings.forget("New")
    assert warnings.of("New") == []
