from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
from conftest import write_server_folder, write_settings

from reclaimer_supervisor.chat import RESERVED, CommandRouter
from reclaimer_supervisor.config import CORE_TABLES, PART_SWITCHES, load
from reclaimer_supervisor.events import EventBus, matches
from reclaimer_supervisor.part import Part, commands_of
from reclaimer_supervisor.parts import PARTS
from reclaimer_supervisor.permissions import Roles


def test_event_patterns() -> None:
    assert matches("rcon.chat", "rcon.chat") and not matches("rcon.chat", "rcon.chatter")
    assert matches("rcon.*", "rcon.join") and not matches("rcon.*", "log.upload")
    assert matches("*", "anything")


def test_a_failing_listener_does_not_stop_the_others() -> None:
    bus, seen = EventBus(), []
    bus.subscribe("x", lambda e: 1 / 0)
    unsubscribe = bus.subscribe("x", seen.append)
    bus.emit("x", None, n=1)
    unsubscribe()
    bus.emit("x", None, n=2)
    assert [e["n"] for e in seen] == [1]


def test_every_part_is_described_and_named_for_its_section() -> None:
    for name, cls in PARTS.items():
        assert issubclass(cls, Part) and cls.name == name and cls.description, name
        assert name not in CORE_TABLES, f"[{name}] would collide with a core section"
        assert not set(cls.defaults) & PART_SWITCHES, f"{name} defaults use a reserved setting"
        assert name not in ("everyone", "moderator", "admin", "owner", "off"), f"{name} would read as a role"


def test_no_two_parts_claim_the_same_command(tmp_path: Path) -> None:
    """All parts share one set of chat commands: a clash fails here, not on a host's server."""
    write_server_folder(tmp_path / "server", 49176)
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    router = CommandRouter(settings, EventBus(), {}, {}, Roles(settings))
    owners: dict[str, str] = {}
    for name, cls in PARTS.items():
        for spec in commands_of(cls):
            assert spec.help, f"!{spec.name} ({name}) needs help text: it's what commands.toml and !help show"
            for word in (spec.name, *spec.aliases):
                assert word not in RESERVED, f"!{word} ({name}) is the server's own vote"
                assert word not in owners, f"!{word} is claimed by both {owners[word]} and {name}"
                owners[word] = name

            async def handler(call: object) -> None:
                pass

            router.register(spec, None, handler)  # raises on any clash, including with !help
    assert "help" not in owners


def test_parts_with_settings_and_the_commands_which_are_always_on(tmp_path: Path) -> None:
    write_server_folder(tmp_path / "server", 49176)
    settings = load(write_settings(tmp_path, tmp_path / "server", '[eventlog]\nenabled = true\nfolder = "x"'))
    assert settings.parts["eventlog"].enabled and settings.parts["eventlog"].options == {"folder": "x"}
    assert settings.parts["commands"].enabled  # no section, can't be switched off
    assert settings.parts["automod"].enabled and settings.parts["watchdog"].enabled  # on unless switched off
    off = load(write_settings(tmp_path, tmp_path / "server", "[automod]\nenabled = false"))
    assert not off.parts["automod"].enabled


@pytest.mark.parametrize("name", [n for n, cls in PARTS.items() if not cls.always_on])
def test_default_settings_pass_preflight(name: str, tmp_path: Path) -> None:
    if name == "automod":  # with the word list `init` writes: valid, but nothing in it yet
        template = resources.files("reclaimer_supervisor").joinpath("templates/automod.txt").read_text("utf-8")
        (tmp_path / "automod.txt").write_text(template, encoding="utf-8")
        assert PARTS[name].preflight({}, tmp_path) == ["automod.txt has no words yet"]
        return
    assert PARTS[name].preflight({}, tmp_path) == []
