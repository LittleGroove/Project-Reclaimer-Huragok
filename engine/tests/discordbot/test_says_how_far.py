"""The scope audit (20 November 2026) and the host's call on it (21 November): a card says how far what it's about
reaches, never only where it began. ChadSlayer's strike from Discord read "SPR | 24/7 HugeGrass" in its footer when it
was his, and no server's."""

from __future__ import annotations

from huragok.core.events import Event
from huragok.discordbot import cards
from huragok.discordbot.config import DiscordConfig

CHAD = "c" * 64


def _config() -> DiscordConfig:
    config = DiscordConfig()
    config.data["voice"] = "plain"
    return config


def _footer(card: cards.Card) -> str:
    return str(cards.embed(card, 0).get("footer", {}).get("text", ""))


def test_a_moderation_card_says_every_server_or_that_server_only() -> None:
    config = _config()
    kick = cards.moderation(config, Event("moderation.action", "SPR | Minigames", {
        "kind": "kick", "target_id": CHAD, "target_name": "ChadSlayer", "by": "the console", "reason": "slurs", "ok": True,
        "case": 415, "reach": "every server"}))
    assert kick is not None and _footer(kick) == "Every server · case 415"
    assert cards.compact(kick).startswith("Every server · ")
    vote = cards.moderation(config, Event("moderation.seen", "Minigames", {
        "kind": "kick", "target_id": CHAD, "target_name": "ChadSlayer", "by": "a vote", "ok": True, "case": 416,
        "reach": "server", "source": "vote"}))
    assert vote is not None and _footer(vote) == "Minigames only · case 416"
    old = cards.moderation(config, Event("moderation.action", "Minigames", {  # an event from before 0.7.0: as it was
        "kind": "mute", "target_id": CHAD, "target_name": "ChadSlayer", "by": "Mod", "ok": True, "case": 3}))
    assert old is not None and _footer(old) == "Minigames · case 3"
    letback = cards.moderation(config, Event("moderation.action", "Minigames", {
        "kind": "pardon", "target_id": CHAD, "target_name": "ChadSlayer", "by": "Mod", "ok": True, "reach": "every server"}))
    assert letback is not None and letback.heading == "Let back"


def test_a_strike_is_the_players_and_says_what_it_did_and_how_far() -> None:
    config = _config()
    card = cards.strike(config, Event("automod.strike", "SPR | 24/7 HugeGrass", {
        "player_id": CHAD, "player_name": "ChadSlayer", "rule": "slurs", "strike": 1, "action": "mute 7d",
        "by": "LittleGroove", "manual": True, "ok": True, "evidence": "LittleGroove", "pending": True, "away": False,
        "of": "strike 1 of 2", "does": "muted for 7 days", "across": True}))
    assert card.line == ("LittleGroove gave **ChadSlayer** `cccccccc` strike 1 of 2 for slurs: muted for 7 days on every server, "
                         "from when they next join")
    assert _footer(card) == ""  # no server: it's his
    nobody = cards.strike(config, Event("automod.strike", "A", {
        "player_id": CHAD, "player_name": "ChadSlayer", "rule": "spam", "strike": 1, "action": "kick", "by": "Mod",
        "manual": True, "ok": True, "evidence": "Mod", "pending": False, "away": True, "of": "strike 1",
        "does": "kicked", "across": True}))
    assert not nobody.line.startswith("Didn't work") and "nobody was kicked: the strike counts" in nobody.line
    caught = cards.strike(config, Event("automod.strike", "A", {  # Vergil's own, before 0.7.0: as it was
        "player_id": CHAD, "player_name": "Chad", "rule": "slurs", "strike": 1, "action": "mute 7d", "ok": True}))
    assert caught.line == "**Chad** `cccccccc` got strike 1 for slurs: mute 7d" and _footer(caught) == "A"
    here = cards.strike(config, Event("automod.strike", "A", {
        "player_id": CHAD, "player_name": "Chad", "rule": "slurs", "strike": 1, "action": "mute 7d", "by": "Mod",
        "manual": True, "ok": True, "of": "strike 1", "does": "muted for 7 days", "across": False}))
    assert here.line.endswith("muted for 7 days on A") and _footer(here) == "A only"  # across_servers off


def test_the_word_lists_and_banned_names_are_every_servers() -> None:
    config = _config()
    words = cards.words_banned(config, Event("automod.words_banned", "A", {"by": "Mod", "words": ["x"], "rule": "slurs"}))
    assert _footer(words) == "Every server"
    name = cards.vergil_line("name-ban", "vergil.name_banned", name="text")(config, Event("automod.name_banned", "A", {"by": "Mod", "text": "Bad"}))
    assert _footer(name) == "Every server"
    cleared = cards.pardon(config, Event("automod.pardon", "A", {"by": "Faculty", "strikes": 2, "player_id": CHAD, "player_name": "Chad"}))
    assert cleared.heading == "Strikes cleared" and _footer(cleared) == ""


def test_a_burst_merged_keeps_its_reach_only_when_its_cards_agree() -> None:
    one = cards.Card("moderation", "kick", "a", server="A", reach=cards.EVERY)
    two = cards.Card("moderation", "kick", "b", server="A", reach=cards.EVERY)
    assert cards.merged([one, two]).reach == cards.EVERY
    three = cards.Card("moderation", "kick", "c", server="A", reach=cards.ONLY)
    assert cards.merged([one, three]).reach == ""
