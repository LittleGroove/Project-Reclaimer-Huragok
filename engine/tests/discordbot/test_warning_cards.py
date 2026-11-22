"""Huragok's warnings in the staff channel (0.7.0, plan section 5): only those whose rule says the host must act on
them (a file the server refuses, a game skipped, an update waiting), said once as each is raised, in the alerts channel."""

from __future__ import annotations

from huragok.core.events import Event
from huragok.discordbot import cards
from huragok.discordbot.config import DiscordConfig
from huragok.discordbot.feeds import SIMPLE


def _config() -> DiscordConfig:
    config = DiscordConfig()
    config.data["voice"] = "plain"
    return config


def _raised(kind: str, discord: bool) -> Event:
    return Event("warning.raised", "LG Races", {"id": "plays-refused:big", "rule": "plays-refused", "kind": kind,
                                                "text": "The server refuses playlists\\big.playlist.json: game 4 names "
                                                        "More Vehicles, which isn't installed.",
                                                "fix": "Fix the playlist; it keeps playing the old one meanwhile.",
                                                "discord": discord})


def test_a_warning_for_the_staff_is_an_alert_with_what_to_do() -> None:
    card = cards.warning(_config(), _raised("refused", True))
    assert card is not None and card.feed == "alerts" and card.kind == "warning-refused" and card.urgent
    assert "game 4 names More Vehicles" in card.line and "Fix the playlist" in card.line
    assert card.server == "LG Races"


def test_one_only_for_the_window_says_nothing_in_discord() -> None:
    assert cards.warning(_config(), _raised("check", False)) is None


def test_the_bot_hears_warnings_raised() -> None:
    assert SIMPLE["warning.raised"] is cards.warning
