from __future__ import annotations

import asyncio
import logging
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, wait_for, write_server_folder, write_settings

from huragok.core.events import Event, EventBus, matches
from huragok.core.part import Part, commands_of, on
from huragok.core.runner import PartRunner, handler_time_limit
from huragok.core.settings import CORE_TABLES, PART_SWITCHES, load
from huragok.parts import PARTS
from huragok.players.roles import Roles
from huragok.players.router import RESERVED, CommandRouter
from huragok.rcon import RconTimeout


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
    write_server_folder(tmp_path / "server", free_port())
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
    write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server", '[eventlog]\nenabled = true\nfolder = "x"'))
    assert settings.parts["eventlog"].enabled and settings.parts["eventlog"].options == {"folder": "x"}
    assert settings.parts["commands"].enabled  # no section, can't be switched off
    assert settings.parts["automod"].enabled and settings.parts["watchdog"].enabled  # on unless switched off
    off = load(write_settings(tmp_path, tmp_path / "server", "[automod]\nenabled = false"))
    assert not off.parts["automod"].enabled


@pytest.mark.parametrize("name", [n for n, cls in PARTS.items() if not cls.always_on])
def test_default_settings_pass_preflight(name: str, tmp_path: Path) -> None:
    if name == "automod":  # with the word list `init` writes: valid, but nothing in it yet
        template = resources.files("huragok").joinpath("templates/automod.txt").read_text("utf-8")
        (tmp_path / "automod.txt").write_text(template, encoding="utf-8")
        assert PARTS[name].preflight({}, tmp_path) == ["automod.txt has no words yet"]
        return
    assert PARTS[name].preflight({}, tmp_path) == []


# The runner ----------------------------------------------------------------------------------------------

class Ctx:
    """What a part's runner needs of its context."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.config: dict[str, Any] = {}
        self.log = logging.getLogger(f"part.{name}")


class Flaky(Part):
    name = "flaky"
    description = "Fails while it's told to"

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.failing = True
        self.handled: list[str] = []

    @on("rcon.chat")
    async def chat(self, event: Event) -> None:
        if self.failing:
            raise RuntimeError("oops")
        self.handled.append(event["text"])


class SlowServer(Part):
    name = "slow"
    description = "Its server never answers"

    @on("rcon.chat")
    async def chat(self, event: Event) -> None:
        raise RconTimeout("no reply within 10 s")


class BrokenSetup(Part):
    name = "broken"
    description = "Can't set itself up"

    async def setup(self) -> None:
        raise RuntimeError("no database")


def test_a_part_switched_off_for_failing_is_on_again_after_a_while(run, caplog) -> None:
    """Switched off for good, every `!` command answered "Unknown command" until Huragok restarted."""
    bus = EventBus()
    part = Flaky(Ctx("flaky"))
    runner = PartRunner(part, bus, cool_down=1.0)
    seen: list[str] = []
    bus.subscribe("supervisor.*", lambda e: seen.append(e.name))

    async def scenario() -> None:
        task = asyncio.ensure_future(runner.run())
        # Listening only once its setup is done: lines sent before that reach nobody (CI's Windows is slow to it).
        await wait_for(lambda: runner.enabled and runner._unsubscribe)
        for n in range(5):
            bus.emit("rcon.chat", "Alpha", text=str(n))
        # The event, not the state: a runner slow to look (CI's Windows) could miss a short spell off.
        await wait_for(lambda: "supervisor.part_disabled" in seen)
        assert not runner.enabled
        part.failing = False
        bus.emit("rcon.chat", "Alpha", text="while it's off")  # not handled
        await wait_for(lambda: runner.enabled, 5)
        bus.emit("rcon.chat", "Alpha", text="back")
        await wait_for(lambda: part.handled)
        assert part.handled == ["back"] and runner.disabled_reason is None
        await runner.stop()
        await task

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert seen == ["supervisor.part_disabled", "supervisor.part_enabled"]
    assert "flaky switched off for 1 s: 5 failures in 10 minutes" in caplog.text
    assert "flaky on again after 1 s off" in caplog.text


def test_a_part_whose_setup_fails_stays_off_and_a_silent_server_isnt_the_part_failing(run) -> None:
    bus = EventBus()
    broken = PartRunner(BrokenSetup(Ctx("broken")), bus, cool_down=0.05)
    slow = PartRunner(SlowServer(Ctx("slow")), bus, cool_down=0.05)

    async def scenario() -> None:
        await broken.run()  # returns: it never got going
        task = asyncio.ensure_future(slow.run())
        await wait_for(lambda: slow.enabled and slow._unsubscribe)  # listening (its setup done)
        for n in range(6):
            bus.emit("rcon.chat", "Alpha", text=str(n))
        await wait_for(lambda: slow.queue.empty())
        await asyncio.sleep(0.2)
        assert not broken.enabled and broken.for_good and "setup failed" in (broken.disabled_reason or "")
        assert slow.enabled and slow.disabled_reason is None  # the server not answering, six times: still on
        await slow.stop()
        await task

    run(scenario())


def test_a_handler_gets_longer_than_an_rcon_command() -> None:
    """Both were 10 s, and the handler's limit, started first, ran out first: a slow server counted as the part
    taking too long."""
    assert handler_time_limit(10, 10) == 15
    assert handler_time_limit(30, 10) == 30
    assert handler_time_limit(10, 20) == 25
