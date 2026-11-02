"""The fake's rules for what Discord refuses: they catch what they're meant to, and every form the bot makes passes them."""

from __future__ import annotations

from pathlib import Path

import pytest

from testkit.discord_rules import command_problems, component_problems, embed_problems, message_problems, modal_problems

from huragok.discordbot.config import load
from huragok.discordbot.feeds import sample_cards
from huragok.discordbot import cards


def row(*items: dict) -> dict:
    return {"type": 1, "components": list(items)}


def button(label: str = "Go", custom_id: str = "a") -> dict:
    return {"type": 2, "style": 1, "label": label, "custom_id": custom_id}


def select(*options: dict, custom_id: str = "s", **more: object) -> dict:
    return {"type": 3, "custom_id": custom_id, "options": list(options), **more}


def option(value: str = "v", label: str = "l") -> dict:
    return {"label": label, "value": value}


@pytest.mark.parametrize(("form", "word"), [
    ([row(button(custom_id=str(n))) for n in range(6)], "rows"),
    ([row(*[button(custom_id=str(n)) for n in range(6)])], "1 to 5"),
    ([row(button(label="x" * 81))], "label"),
    ([row(button(custom_id="x" * 101))], "custom_id"),
    ([row(button(label=""))], "label"),
    ([row(button(), button())], "custom_id"),
    ([row(select(option()), button())], "a row to itself"),
    ([row(select())], "1 to 25"),
    ([row(select(*[option(str(n)) for n in range(26)]))], "1 to 25"),
    ([row(select(option("x" * 101)))], "value"),
    ([row(select(option(label="x" * 101)))], "label"),
    ([row(select(option("a"), option("a")))], "same value"),
    ([row(select(option(), placeholder="x" * 151))], "placeholder"),
    ([row(select({**option("a"), "default": True}, {**option("b"), "default": True}))], "by default"),
    ([row(select(option(), min_values=3, max_values=2))], "min_values"),
])
def test_components_it_catches(form: list[dict], word: str) -> None:
    assert any(word in problem for problem in component_problems(form))


def test_good_components_pass() -> None:
    assert component_problems([row(button(custom_id="a"), button(custom_id="b")), row(select(option("a"), option("b"), placeholder="pick"))]) == []


@pytest.mark.parametrize(("embed", "word"), [
    ({"title": "x" * 257}, "title"), ({"description": "x" * 4097}, "description"), ({"footer": {"text": "x" * 2049}}, "footer"),
    ({"fields": [{"name": "", "value": "v"}]}, "name"), ({"fields": [{"name": "n", "value": ""}]}, "value"), ({"fields": [{"name": "n", "value": "x" * 1025}]}, "value"),
    ({"fields": [{"name": "n", "value": "v"}] * 26}, "fields"), ({"description": "x" * 4000, "fields": [{"name": "n", "value": "x" * 1024}] * 3}, "6000"),
])
def test_embeds_it_catches(embed: dict, word: str) -> None:
    assert any(word in problem for problem in embed_problems([embed]))


def test_messages_it_catches() -> None:
    assert message_problems({})  # nothing to send
    assert message_problems({"content": "x" * 2001})
    assert message_problems({"content": "x", "flags": 64})  # only an answer can be private
    assert not message_problems({"content": "x", "flags": 64}, may_be_ephemeral=True)
    assert message_problems({"content": "x", "allowed_mentions": {"parse": ["roles"], "roles": ["1"]}})
    assert not message_problems({"content": "x", "allowed_mentions": {"parse": [], "roles": ["1"]}})
    assert not message_problems({"embeds": [{"title": "x"}], "flags": 4096})


def test_forms_it_catches() -> None:
    box = {"type": 4, "custom_id": "b", "style": 1}
    label = {"type": 18, "label": "L", "component": box}
    assert modal_problems({"title": "x" * 46, "custom_id": "m", "components": [label]})
    assert modal_problems({"title": "t", "custom_id": "m", "components": []})
    assert modal_problems({"title": "t", "custom_id": "m", "components": [label] * 6})
    assert modal_problems({"title": "t", "custom_id": "m", "components": [{**label, "label": "x" * 46}]})
    assert modal_problems({"title": "t", "custom_id": "m", "components": [{"type": 18, "label": "L", "component": {**box, "value": "x" * 20, "max_length": 10}}]})
    assert not modal_problems({"title": "t", "custom_id": "m", "components": [label]})


def test_commands_it_catches() -> None:
    good = {"name": "kick", "description": "d", "type": 1}
    assert not command_problems([good])
    assert command_problems([{**good, "name": "Kick"}])
    assert command_problems([{**good, "name": "has space"}])
    assert command_problems([{**good, "description": ""}])
    assert command_problems([{**good, "description": "x" * 101}])
    assert command_problems([good, good])
    assert command_problems([{**good, "options": [{"name": "a", "description": "d", "type": 3}, {"name": "b", "description": "d", "type": 3, "required": True}]}])
    assert command_problems([{**good, "name": str(n)} for n in range(101)])
    assert not command_problems([{"name": "Report message", "type": 3}])
    assert command_problems([{"name": "x" * 33, "type": 3}])


def test_every_kind_of_card_the_bot_posts_passes(tmp_path: Path) -> None:
    config = load(tmp_path / "none")
    for card in sample_cards(config):
        assert not embed_problems([cards.embed(card, 0)]), card.message


def test_a_card_with_everything_at_its_longest_passes(tmp_path: Path) -> None:
    config = load(tmp_path / "none")
    long = "x" * 5000
    made = [cards.reported(config, long, "first", "id", long, long, long, [long] * 10, "p"), cards.message_reported(config, long, "first", long, long, "https://discord.com/x", long, long),
            cards.appeal(config, long, "ban", "id", long, long, long, [long] * 10, [(long, long)] * 5), cards.contacted(config, long, long, long, "p"), cards.called(config, long, long, long, [long] * 10)]
    for card in made:
        assert not embed_problems([cards.embed(card, 0)]), card.message
