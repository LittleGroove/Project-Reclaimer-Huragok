"""docs/discord.md, the guide hosts follow to make their bot: everything it names has to be there."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from huragok.discordbot import features, rest
from huragok.discordbot.config import defaults, load
from huragok.window.screens import discordsetup

GUIDE = Path(__file__).resolve().parents[3] / "docs" / "discord.md"
TEXT = GUIDE.read_text(encoding="utf-8")
CHANNELS = TEXT[TEXT.index("| Name in Huragok |"):TEXT.index("**A private channel:**")]


def test_it_names_every_slash_command_and_no_other() -> None:
    pytest.importorskip("discord")
    from huragok.discordbot.client import HuragokCommands

    have = {name for name, value in vars(HuragokCommands).items() if type(value).__name__ == "Command"}
    assert have == {"claim", "setup", "features", "check", "test", "gallery", "reload"}
    assert set(re.findall(r"/huragok (\w+)", TEXT)) == have


def test_it_names_every_key_of_the_window_and_no_other() -> None:
    source = inspect.getsource(discordsetup.DiscordScreens.screen_discord)
    keys = set(re.findall(r'\("([A-Z])", "', source))
    assert len(keys) == 17
    table = TEXT[TEXT.index("In the window: J, then"):TEXT.index("A **feature** is")]
    assert set(re.findall(r"^\| \*\*([A-Z])\*\* \|", table, re.M)) == keys


def test_it_lists_every_permission_the_invite_asks_for() -> None:
    bits = features.invite_permissions(load(Path("no such file")))
    asked = {name for name, bit in rest.PERMISSIONS.items() if bits & bit}
    words = {"view": "View Channels", "send": "Send Messages", "embed": "Embed Links", "attach": "Attach Files",
             "history": "Read Message History", "manage_webhooks": "Manage Webhooks"}
    assert asked == set(words)
    table = TEXT[TEXT.index("| Permission |"):TEXT.index("Discord gives the bot a role")]
    assert all(f"| {word} |" in table for word in words.values())


def test_it_lists_every_shared_channel() -> None:
    assert set(re.findall(r"^\| `([\w-]+)` \|", CHANNELS, re.M)) == set(defaults()["channels"])


def test_what_it_says_about_a_channels_privacy_is_what_huragok_enforces() -> None:
    private = set(re.findall(r"^\| `([\w-]+)` \|[^|]*\| \*\*staff only\*\*", CHANNELS, re.M))
    config = load(Path("no such file"))
    for feature in features.FEATURES:
        if feature.channel and feature.staff:
            assert {name for name, _ in features.channels_of(feature, config)} <= private, feature.key


def test_it_says_where_messages_are_set() -> None:
    assert "[messages]" in TEXT and "[colours]" in TEXT and "post = false" in TEXT


def test_it_has_no_ones_own_names_and_no_address() -> None:
    assert "SPR" not in TEXT and "DEPLOY" not in TEXT and "LilGremlin" not in TEXT
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", TEXT)


def test_the_window_points_to_it_in_the_order_discord_needs() -> None:
    steps = "\n".join(discordsetup.BOT_STEPS)
    assert "docs/discord.md" in steps
    assert steps.index("Install Link") < steps.index("Public Bot")  # Discord refuses the other order
    assert TEXT.index("**Install Link** to **None**") < TEXT.index("Switch **Public Bot** off")
