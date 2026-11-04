"""What's out in the world and can't quietly change. A released schema step has run on hosts' databases; a button's custom_id is on cards already posted; a slash command's boxes are what people have learned, and they come from usage strings, so editing one renames a box.

Each of these fails with what to do instead. If the change is meant, and nothing's been released with the old one: python tests/architecture/freeze.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from huragok.core.storage import CORE_MIGRATIONS
from huragok.discordbot import buttons, console, contact, speakers
from huragok.discordbot.status import SCHEMA
from huragok.discordbot.tickets import BUTTON_STYLES
from huragok.players.automod.automod import MIGRATIONS as AUTOMOD_MIGRATIONS
from huragok.players.commands.schema import MIGRATIONS as COMMANDS_MIGRATIONS

FROZEN = json.loads((Path(__file__).parent / "frozen.json").read_text(encoding="utf-8"))
STEPS = {"core": CORE_MIGRATIONS, "automod": AUTOMOD_MIGRATIONS, "commands": COMMANDS_MIGRATIONS, "discord": SCHEMA}
# Everything a card or a post in Discord can send back. Cards stay up for good, so each of these has to mean something for good.
PRESSES = ("appealagain", "mute", "unmute", "kick", "letback", "ban", "unban", "banall", "strike", "pardonstrike", "warn", "whois", "said", "note", "thread", "about", "server",
           "lift", "shorten", "confirm", "deny", "refuse", "acknowledge", "close", "dismiss", "leave", "banthename", "allowname")


def _hash(step: str) -> str:
    return hashlib.sha256(step.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("faculty", sorted(STEPS))
def test_a_released_schema_step_is_never_edited(faculty: str) -> None:
    """It's run on hosts' databases, and won't run again: a change has to be a new step at the end."""
    now = [_hash(step) for step in STEPS[faculty]]
    frozen = FROZEN["schema"][faculty]
    assert now[:len(frozen)] == frozen, f"A step of {faculty}'s has changed. Never edit a released step: add one at the end"


def test_every_press_a_card_can_send_still_means_something() -> None:
    source = Path(buttons.__file__).read_text(encoding="utf-8")
    for action in PRESSES:
        assert f'"{action}"' in source or f'{action}"' in source, f"huragok:t:<n>:{action} is on cards already posted: it needs to go on working"


def test_the_card_buttons_listed_are_all_known_presses() -> None:
    assert set(BUTTON_STYLES) <= set(PRESSES)


def test_the_addresses_on_old_posts_stay_the_same() -> None:
    """A card can only be edited through the webhook that posted it, so the bot has to find its own again by name."""
    assert speakers.WEBHOOK_NAME == "Huragok"
    assert console.PICK == "huragok:console:pick" and contact.OPEN == "huragok:contact:open"


def test_slash_commands_keep_their_names_and_boxes() -> None:
    """Options come from usage strings, so rewording one renames a box, and what people have learned stops working."""
    from huragok.core.commandsfile import all_commands
    from huragok.discordbot.slash import NOT_FROM_DISCORD, plan_of

    now = {spec.name: [option.name for option in plan_of(spec)] for spec in all_commands().values() if spec.name not in NOT_FROM_DISCORD}
    for name, boxes in FROZEN["slash"].items():
        assert name in now, f"/{name} has gone: it's what people have learned. Keep it, or say so in the notes and run tests/architecture/freeze.py"
        assert now[name] == boxes, f"/{name}'s boxes have changed from {boxes} to {now[name]}: a usage string was reworded?"
