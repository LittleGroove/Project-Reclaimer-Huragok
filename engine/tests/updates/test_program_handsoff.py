"""Project Reclaimer releases, hands off (0.7.0, plan-0.7.0 section 4.3): each server installs its own; Huragok looks
every 15 minutes, restarts the servers that update themselves after their game, tells players, and warns. It never
puts a program file in place. S2 and S2d (DEPLOY/sim-handsoff/RESULTS.md) are what these follow."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, ProgramLifecycle, wait_for
from testkit.manage import StubClient, program_release, self_updating_root, serve_program

from huragok.core.events import Event, EventBus
from huragok.core.settings import load
from huragok.servers.state import Player, ServerState
from huragok.updates import program as program_module
from huragok.updates.program import OLD_FILE_WARN, REASON, ProgramUpdateService


def service_on(root: Path, *, clients: dict[str, Any] | None = None, up: tuple[str, ...] = ("Alpha", "Beta"),
               bus: EventBus | None = None) -> tuple[ProgramUpdateService, ProgramLifecycle, EventBus]:
    settings = load(root / "settings.toml")
    lifecycle, bus = ProgramLifecycle(up=up), bus or EventBus()
    states = {s.name: ServerState(s.name) for s in settings.servers}
    service = ProgramUpdateService(settings, bus, lifecycle, states, clients or {})  # type: ignore[arg-type]
    return service, lifecycle, bus


# Answer 23: the server's own update is about to cut the game ------------------------------------------------------

def test_players_are_told_the_moment_the_server_says_it_is_downloading_a_release(tmp_path: Path, run, caplog) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    alpha, beta = StubClient(), StubClient()
    service, lifecycle, bus = service_on(root, clients={"Alpha": alpha, "Beta": beta})
    service.states["Alpha"].players[1] = Player(1, "Alice", ALICE)

    async def scenario() -> None:
        bus.emit("server.update_downloading", "Alpha", version="0.10.8")  # S2d: about 8 s before the stop
        bus.emit("server.update_downloading", "Beta", version="0.10.8")  # nobody on: nothing to say
        await wait_for(lambda: alpha.sent)
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.INFO):
        run(scenario())
    said = " ".join(args[0] for command, args, _ in alpha.sent if command == "say")
    assert said == ("[-.-] PARDON OUR DUST! This server is updating to Project Reclaimer 0.10.8 and restarts in a few "
                    "seconds. Update your game to rejoin.")
    assert beta.sent == []
    assert lifecycle.restarts == []  # Huragok restarts nothing for it: the server's own updater does
    assert "[Alpha] downloading Project Reclaimer 0.10.8 itself (auto_update): it restarts on it in a few seconds; " \
           "telling the 1 player(s) on" in caplog.text


def test_a_game_the_servers_own_update_cut_is_said_as_such(tmp_path: Path, run, caplog) -> None:
    """S2d: Huragok logged "its game ended" when the update had stopped the game 9 minutes 46 s in."""
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    service, _, bus = service_on(root)
    began = time.time() - (9 * 60 + 46)
    service._on_phase(Event("rcon.phase", "Alpha", {"phase": "playing"}, time=began))
    service._on_phase(Event("rcon.phase", "Alpha", {"phase": "round_over"}))  # a round isn't a new game
    service._on_phase(Event("rcon.phase", "Alpha", {"phase": "playing"}))

    with caplog.at_level(logging.WARNING):
        bus.emit("server.update_downloading", "Alpha", version="0.10.8")
        bus.emit("server.updating", "Alpha", version="0.10.8")
        bus.emit("server.updating", "Beta", version="0.10.8")  # in its lobby
    assert ("[Alpha] PARDON OUR DUST! Project Reclaimer installed a new release by itself (auto_update): it stopped "
            "its game 9 minutes in and is back in a few seconds (players must update their game to rejoin)") in caplog.text
    assert ("[Beta] PARDON OUR DUST! Project Reclaimer installed a new release by itself (auto_update): it is back in "
            "a few seconds") in caplog.text
    assert "its game ended" not in caplog.text


# Answer 24: Huragok's look restarts the self-updating servers after their game ------------------------------------

def test_a_release_huragok_sees_restarts_each_self_updating_server_after_its_game(tmp_path: Path, monkeypatch, run,
                                                                                caplog) -> None:
    root = self_updating_root(tmp_path, "Alpha", "Beta", body='backend = "process"\n')
    serve_program(monkeypatch, program_release("0.10.9", published=time.time() - 60), b"program 0.10.9")
    clients = {"Alpha": StubClient(), "Beta": StubClient()}
    for client in clients.values():
        client.version = "0.10.8"
    service, lifecycle, bus = service_on(root, clients=clients)

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.10.8")
        bus.emit("rcon.connected", "Beta", version="0.10.8")
        assert await service.check() is not None
        assert lifecycle.restarts == [(n, "after_game", REASON.format(version="0.10.9")) for n in ("Alpha", "Beta")]
        assert lifecycle.told["Alpha"] == ("[o_o] PARDON OUR DUST! Project Reclaimer 0.10.9 is out. This server restarts "
                                           "for it after this game, so the update lands sooner. Update your game to "
                                           "rejoin.")
        await service.check()
        assert len(lifecycle.restarts) == 2  # once per release
        # Back on it after the restart: nothing more. One that started after Huragok saw it looked itself as it
        # started, so a third server coming up behind is warned about, never restarted again.
        bus.emit("server.started", "Alpha")
        bus.emit("rcon.connected", "Alpha", version="0.10.9")
        service.restarting.pop("Beta")
        bus.emit("server.started", "Beta")
        assert await service.review() == []

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "Project Reclaimer 0.10.9 is out (running: Alpha 0.10.8, Beta 0.10.8): Alpha, Beta install it themselves " \
           "(auto_update), each restarted after its game to do so as it starts" in caplog.text
    assert lifecycle.jobs == {}


def test_huragok_looks_every_check_minutes(tmp_path: Path, monkeypatch, run) -> None:
    root = self_updating_root(tmp_path, body='backend = "process"\n[server_updates]\ncheck_minutes = 20\n')
    service, _, _ = service_on(root)
    slept: list[float] = []
    looks: list[int] = []
    real_sleep = asyncio.sleep

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await real_sleep(0)
        if len(looks) >= 2:
            raise asyncio.CancelledError

    async def check(**options: Any) -> None:
        looks.append(1)

    monkeypatch.setattr(program_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(service, "check", check)
    monkeypatch.setattr(service, "_review_loop", lambda: real_sleep(3600))

    async def scenario() -> None:
        try:
            await service.run()
        except asyncio.CancelledError:
            pass

    run(scenario())
    assert slept[:2] == [20, 20 * 60] and len(looks) == 2


def test_on_docker_a_release_is_only_a_warning(tmp_path: Path, monkeypatch, run, caplog) -> None:
    """Answer 36: the image is the update, and Huragok doesn't manage images yet."""
    root = self_updating_root(tmp_path, "Alpha", body='backend = "docker"\n[docker]\ncompose_file = "compose.yaml"\n')
    serve_program(monkeypatch, program_release("0.10.9", published=time.time() - 3 * 3600), b"program 0.10.9")
    service, lifecycle, bus = service_on(root)

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.10.8")
        bus.emit("rcon.connected", "Beta", version="0.10.8")
        await service.check()
        await service.review(now=time.time() + program_module.OVERDUE * 3)
        bus.emit("server.updating", "Alpha")  # updated itself after all: no restart onto a new file either
        bus.emit("rcon.connected", "Alpha", version="0.10.9")
        await asyncio.sleep(0.05)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert lifecycle.restarts == [] and lifecycle.jobs == {}
    [image] = service.warnings.of("Beta")
    assert image.rule.id == "release-image" and image.rule.discord
    assert image.text == ("A new image is out: Project Reclaimer 0.10.9, and this server runs 0.10.8. On Docker the "
                          "image is the update, so Huragok doesn't restart it for a release.")
    assert "on Docker a new image is the update" in caplog.text


# Warnings ---------------------------------------------------------------------------------------------------------

def test_a_server_pinned_by_its_own_file_is_said_once_calmly(tmp_path: Path, monkeypatch, run, caplog) -> None:
    """LG Firefight: auto_update = false on purpose (0.9.23 pinned). One line, for your information, never the staff
    channel, never a restart."""
    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    serve_program(monkeypatch, program_release("0.10.9"), b"program 0.10.9")
    service, lifecycle, bus = service_on(root)
    raised: list[Event] = []
    bus.subscribe("warning.raised", raised.append)

    async def scenario() -> None:
        bus.emit("rcon.connected", "Beta", version="0.9.23")
        for _ in range(3):
            await service.check()
            await service.review(now=time.time() + 86400)
        serve_program(monkeypatch, program_release("0.10.10"), b"program 0.10.10")  # the next one: still not again
        await service.check()

    with caplog.at_level(logging.INFO):
        run(scenario())
    [pinned] = service.warnings.of("Beta")
    assert pinned.rule.id == "release-pinned" and pinned.kind == "info" and not pinned.rule.discord
    assert pinned.text.startswith("Pinned to Project Reclaimer 0.9.23 by its own file (auto_update = false)")
    assert [e.get("rule") for e in raised if e.server == "Beta"] == ["release-pinned"]
    assert caplog.text.count("Pinned to Project Reclaimer 0.9.23") == 1
    assert ("Beta", "after_game", REASON.format(version="0.10.9")) not in lifecycle.restarts


def test_a_busy_server_that_never_empties_for_its_new_program_file_is_warned_about(tmp_path: Path, run) -> None:
    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    service, lifecycle, bus = service_on(root)
    lifecycle.planned["Alpha"] = type("Plan", (), {"when": "when_empty", "reason": "x"})()  # it waits for an empty

    async def scenario() -> None:
        bus.emit("rcon.connected", "Alpha", version="0.10.7")
        bus.emit("server.updating", "Alpha", version="0.10.8")
        bus.emit("rcon.connected", "Alpha", version="0.10.8")
        await service.review()
        assert service.warnings.of("Alpha") == []  # not yet: it may empty any minute
        await service.review(now=time.time() + OLD_FILE_WARN + 1)
        [busy] = service.warnings.of("Alpha")
        assert busy.rule.id == "release-old-file" and not busy.rule.discord
        assert busy.text.startswith("It updated itself to Project Reclaimer 0.10.8 at ")
        assert "still runs from the old file (project-reclaimer.exe.old)" in busy.text
        assert service.note("Alpha") == "its program still runs from the old file: it restarts onto the new one once " \
                                        "nobody is on"
        (root / "SERVERS" / "Alpha" / "project-reclaimer.exe.old").write_bytes(b"0.10.7")
        said = await lifecycle.jobs["Alpha"]["program-old"].run()  # it emptied at last: stopped, started again
        assert said == "removed project-reclaimer.exe.old (the program from before its update)"
        assert service.warnings.of("Alpha") == [] and service.old_file == {}
        await service.review(now=time.time() + OLD_FILE_WARN * 5)
        assert service.warnings.of("Alpha") == []

    run(scenario())
    assert not (root / "SERVERS" / "Alpha" / "project-reclaimer.exe.old").exists()


def test_a_server_huragok_cant_restart_gets_no_busy_warning(tmp_path: Path, run) -> None:
    """One started some other way (its restart refused): not "busy", just not Huragok's to restart."""
    root = self_updating_root(tmp_path, "Alpha", body='backend = "process"\n')
    service, lifecycle, bus = service_on(root)

    async def refused(server: str, **options: Any) -> Any:
        raise program_module.RconError("it wasn't started from here, so the Superintendent can't restart it")

    lifecycle.restart = refused  # type: ignore[method-assign]

    async def scenario() -> None:
        bus.emit("server.updating", "Alpha", version="0.10.8")
        bus.emit("rcon.connected", "Alpha", version="0.10.8")
        await wait_for(lambda: "Alpha" not in service.old_file)
        await service.review(now=time.time() + OLD_FILE_WARN * 2)

    run(scenario())
    assert service.warnings.of("Alpha") == []


def test_the_warnings_are_in_the_catalogue_with_their_version() -> None:
    from huragok.core.serverwarnings import load_rules

    rules = load_rules()
    for rule_id, kind, discord in (("release-pinned", "info", False), ("release-overdue", "seen", True),
                                   ("release-old-file", "check", False), ("release-image", "check", True)):
        rule = rules[rule_id]
        assert (rule.kind, rule.discord, rule.area, rule.checked) == (kind, discord, "updates", "0.10.9"), rule_id
