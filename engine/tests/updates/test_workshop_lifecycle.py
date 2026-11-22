"""A Workshop mod update held up by its map, through the real fleet: the server's console line sets off a restart of
its game over RCON (its process stays up), and for servers sharing a content folder the shutdown pattern stops them
with RCON `quit` and starts them again. The other tests hand everything to a fake lifecycle; these run the managers
the way a host's Huragok does."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from conftest import ALICE, free_port
from testkit.fleet import RESTARTS, Clock, FakeBackend, FakeClient
from testkit.setup import make_server_folder, setup_root
from testkit.workshop import ALPHA, HELD, ITEM, OLD, UPDATED

from huragok.core.events import Event, EventBus
from huragok.core.serverwarnings import Warnings, load_rules
from huragok.core.settings import load
from huragok.core.tomledit import set_table_key, set_top_key
from huragok.program.dedicated import tool_for
from huragok.servers import server as manager_module
from huragok.servers.fleet import Fleet, Lifecycle
from huragok.servers.state import Player, ServerState
from huragok.updates import workshop
from huragok.updates.workshop import WorkshopUpdateService, installed_version

BETA = "Beta"


class QuittingClient(FakeClient):
    """`quit` ends the program, as the real one does in under a second (S1b)."""

    def __init__(self, state: ServerState, backend: FakeBackend, name: str) -> None:
        super().__init__(state)
        self.backend, self.name = backend, name

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Any:
        if command == "quit":
            self.backend.up.discard(self.name)
            self.state.connected = False
        return await super().command(command, *args, by=by, timeout=timeout)


class Host:
    """Alpha (and Beta on Alpha's content folder, with `shared`), the item installed by the fake program, run by the
    real fleet on a fake backend, the managers' clock moved by hand."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shared: bool = False) -> None:
        monkeypatch.setattr(workshop, "SAY_SECONDS", 0.0)
        monkeypatch.setattr(workshop, "SHARED_TICK", 0.01)
        monkeypatch.setattr(workshop, "AFTER_SECONDS", 0.05)
        self.clock = Clock(1_800_000_000.0)
        monkeypatch.setattr(manager_module, "time", self.clock)
        settings_path = setup_root(tmp_path, extra=RESTARTS)
        folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
        set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
        (folder / "fake-steam.json").write_text(f'{{"{ITEM}": {OLD}}}', encoding="utf-8")
        assert tool_for(load(settings_path)).mods(folder).ok
        names = [ALPHA]
        if shared:
            beta = tmp_path / "reclaimer" / "SERVERS" / BETA
            make_server_folder(beta, BETA, free_port())
            set_top_key(beta / "dedicated.toml", "content", f"../{ALPHA}/content")
            names.append(BETA)
        self.settings = load(settings_path)
        self.bus = EventBus()
        self.states = {name: ServerState(name) for name in names}
        self.backend = FakeBackend(self.settings, self.bus, self.states)
        self.fleet = Fleet(self.settings, self.backend, self.bus)
        self.clients = {name: QuittingClient(self.states[name], self.backend, name) for name in names}
        self.managers = {name: self.fleet.add(self.settings.server(name), self.clients[name], self.states[name])
                         for name in names}  # type: ignore[arg-type]
        self.lifecycle = Lifecycle(self.fleet)
        self.warnings = Warnings(load_rules(), self.settings.records)
        self.updates = WorkshopUpdateService(self.settings, self.bus, self.lifecycle, warnings=self.warnings,
                                             fetch=lambda items: {})

    async def tick(self, advance: float = 0) -> None:
        self.clock.now += advance
        for _ in range(3):
            for manager in self.managers.values():
                manager.tick(self.clock.now)
            await asyncio.sleep(0.01)

    async def up(self) -> None:
        self.fleet.boot()
        for state in self.states.values():
            state.connected = True
        for _ in range(100):
            await self.tick(1)
            if all(m.phase == "running" for m in self.managers.values()):
                return
            for name, manager in self.managers.items():
                if manager.phase == "booting":
                    self.states[name].connected = True
        raise AssertionError({n: m.phase for n, m in self.managers.items()})

    def sent(self, name: str) -> list[str]:
        return [command if not args else f"{command} {' '.join(args)}" for command, args in self.clients[name].sent]

    def line(self, text: str, name: str = ALPHA) -> None:
        self.bus.emit("server.workshop", name, item=str(ITEM), text=text)

    def player(self, name: str = ALPHA) -> None:
        self.states[name].players[1] = Player(1, "Alice", ALICE)

    def game(self, phase: str, name: str = ALPHA) -> None:
        self.states[name].phase = phase
        self.managers[name].on_phase(Event("rcon.phase", name, {"phase": phase, "map": "m", "mode": "Slayer"}))

    async def until(self, check: Any, ticks: int = 300) -> None:
        for _ in range(ticks):
            if check():
                return
            await self.tick(1)
        raise AssertionError({n: (m.phase, self.sent(n)) for n, m in self.managers.items()})


def test_nobody_on_its_game_is_restarted_over_the_console_and_the_process_stays(tmp_path: Path, monkeypatch, run) -> None:
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        starts = len(host.backend.starts)
        host.line(HELD)
        await host.until(lambda: host.sent(ALPHA) == ["restart"])
        assert host.managers[ALPHA].phase == "running" and len(host.backend.starts) == starts
        assert not host.backend.stops  # never stopped, never killed
        assert {w.rule.id for w in host.warnings.of(ALPHA)} == {"mod-update-waiting"}
        host.line(UPDATED)
        await host.tick(1)
        assert host.warnings.of(ALPHA) == []

    run(scenario())
    assert installed_version(host.settings.server(ALPHA).content_dir, ITEM)["updated"] == OLD  # nothing moved by Huragok


def test_people_on_the_line_at_the_games_end_tells_them_then_restart_now(tmp_path: Path, monkeypatch, run) -> None:
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        host.player()
        host.game("playing")
        host.game("finished")  # the game ends: the server's install attempt fails then (S1b b)
        host.line(HELD)
        await host.until(lambda: host.sent(ALPHA)[-1:] == ["restart now"])

    run(scenario())
    assert host.sent(ALPHA)[0].startswith("say [-.-] PARDON OUR DUST! Restarting for a mod update")
    assert host.managers[ALPHA].phase == "running"


def test_sharing_a_content_folder_the_last_one_up_holding_it_is_restarted(tmp_path: Path, monkeypatch, run) -> None:
    """Change 1 through the real managers: empty Beta stopped with `quit`, Alpha (holding the download, players on)
    restarted at its game's end, then Beta started again."""
    host = Host(tmp_path, monkeypatch, shared=True)

    async def scenario() -> None:
        await host.up()
        host.player(ALPHA)
        host.game("playing", ALPHA)
        host.line(HELD, ALPHA)
        await host.until(lambda: host.managers[BETA].phase == "idle")
        assert host.sent(BETA) == ["quit"] and host.managers[BETA].record.wanted == "stopped"
        assert "waits for this server's game to end" in host.warnings.of(ALPHA)[0].text
        host.game("finished", ALPHA)
        await host.until(lambda: host.sent(ALPHA)[-1:] == ["restart now"])
        assert host.managers[BETA].phase == "idle"  # started only once it's in
        host.line(UPDATED, ALPHA)
        await host.until(lambda: host.managers[BETA].phase in ("booting", "running"))
        host.states[BETA].connected = True
        await host.until(lambda: host.managers[BETA].phase == "running")
        await host.until(lambda: not host.updates.shared)

    run(scenario())
    assert host.warnings.of(ALPHA) == [] and host.warnings.of(BETA) == []
    assert "quit" not in host.sent(ALPHA)
