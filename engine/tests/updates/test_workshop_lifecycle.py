"""A Workshop mod's new version through the real lifecycle: noticed while players are on, planned for the end of the
game, swapped in while the server is stopped, and the server back on the new version. The other tests hand the job to a
fake lifecycle; these run it the way a host's Huragok does."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from conftest import ALICE
from testkit.fleet import RESTARTS, Clock, FakeBackend, FakeClient
from testkit.mods import a_map, a_mod
from testkit.setup import setup_root
from testkit.workshop import ALPHA, ITEM, NEW, OLD

from huragok.content.workshopmods import WorkshopKeeper
from huragok.core.events import Event, EventBus
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_table_key
from huragok.program.dedicated import tool_for
from huragok.servers import server as manager_module
from huragok.servers.fleet import Fleet, Lifecycle
from huragok.servers.state import Player, ServerState
from huragok.steam import MCC_APP, SteamItem
from huragok.updates.workshop import RECEIPT, WorkshopUpdateService, installed_version

TITLE = "HugeGrass Rebalance"
MAP, MODE = f"workshop:{ITEM}/fake{ITEM}", "CaptureTheFlag"


class Host:
    """Alpha with the fake program, run by the real fleet on a fake backend, its clock moved by hand."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, listed: bool = True) -> None:
        self.clock = Clock(1_800_000_000.0)
        monkeypatch.setattr(manager_module, "time", self.clock)
        settings_path = setup_root(tmp_path, extra=RESTARTS)
        self.folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
        self.steam(OLD)
        if listed:
            set_table_key(self.folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
            result = tool_for(load(settings_path)).mods(self.folder)
            assert result.ok, result.text
        else:
            a_mod(self.folder / "content" / "Mods" / "HugeGrass", {f"fake{ITEM}": a_map()}, title=TITLE, workshop=ITEM)
        self.settings = load(settings_path)
        self.bus = EventBus()
        self.state = ServerState(ALPHA)
        self.backend = FakeBackend(self.settings, self.bus, {ALPHA: self.state})
        self.fleet = Fleet(self.settings, self.backend, self.bus)
        self.client = FakeClient(self.state)
        self.manager = self.fleet.add(self.settings.server(ALPHA), self.client, self.state)  # type: ignore[arg-type]
        self.lifecycle = Lifecycle(self.fleet)
        self.updates = WorkshopUpdateService(self.settings, self.bus, self.lifecycle,
                                             tool=lambda: tool_for(self.settings), fetch=self.fetch)
        self.keeper = WorkshopKeeper(self.settings, self.bus, self.lifecycle, self.updates)
        self.server = self.settings.server(ALPHA)

    def steam(self, version: int) -> None:
        """What Steam serves: its details and what the fake `dedicated mods` fetches."""
        self.newest = version
        (self.folder / "fake-steam.json").write_text(json.dumps({str(ITEM): version}), encoding="utf-8")

    def fetch(self, items: list[int]) -> dict[int, SteamItem]:
        return {ITEM: SteamItem(ITEM, TITLE, self.newest, str(self.newest), MCC_APP)} if ITEM in items else {}

    def installed(self) -> int | None:
        found = installed_version(self.server.content_dir, ITEM)
        return found["updated"] if found else None

    async def tick(self, advance: float = 0) -> None:
        self.clock.now += advance
        for _ in range(3):
            self.manager.tick(self.clock.now)
            await asyncio.sleep(0.01)

    async def up(self) -> None:
        self.fleet.boot()
        self.state.connected = True
        for _ in range(200):
            await self.tick()
            if self.manager.phase == "running":
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"not up: {self.manager.phase}")

    def playing(self, phase: str = "playing") -> None:
        self.state.phase, self.state.map, self.state.mode = phase, MAP, MODE
        self.manager.on_phase(Event("rcon.phase", ALPHA, {"phase": phase, "map": MAP, "mode": MODE}))

    async def until_running_again(self, seconds: float = 30) -> None:
        """Through the countdown, the stop, the jobs (the fake program runs in a thread) and the start."""
        starts = len(self.backend.starts)
        for _ in range(int(seconds / 0.05)):
            await self.tick(1)
            if len(self.backend.starts) > starts:
                self.state.connected = True
                await self.tick(1)
                if self.manager.phase == "running":
                    return
            await asyncio.sleep(0.04)
        raise AssertionError(f"not running again: {self.manager.phase}, starts {self.backend.starts}")


def test_a_busy_servers_update_goes_in_when_its_game_ends(tmp_path: Path, monkeypatch, run) -> None:
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        host.steam(NEW)
        await host.updates.check()
        await asyncio.sleep(0.05)  # the plan is made in the background
        assert host.manager.planned is not None and host.manager.planned.when == "after_game"
        assert any("restarts after this game to put it in" in said for said in host.client.says())
        assert [j.key for j in host.manager.jobs] == ["workshop"]
        assert host.installed() == OLD and host.backend.stops == []  # nobody's game is cut short
        host.playing("finished")
        await host.until_running_again()
        assert host.backend.stops == [ALPHA] and host.installed() == NEW
        assert not host.updates.waiting and host.manager.jobs == [] and host.manager.planned is None

    run(scenario())


def installed_by_the_server(host: Host) -> None:
    """The program fetched and installed the new version itself: its receipt is the new one, and its console says so."""
    receipt = host.server.content_dir / "Mods" / "Workshop" / str(ITEM) / RECEIPT
    receipt.write_text(json.dumps({"item": ITEM, "manifest": str(NEW), "updated": NEW}), encoding="utf-8")
    host.bus.emit("server.workshop", ALPHA, item=ITEM, text="updated to the version of 18 Oct 2026")


def test_an_update_the_server_installed_itself_calls_off_the_restart(tmp_path: Path, monkeypatch, run, caplog) -> None:
    """The program installs a new version itself while the game doesn't use the mod (seen on FullDeployTest, 3 Nov
    2026): the restart planned for it is called off, not run after the game (or after half an hour) for nothing."""
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        host.steam(NEW)
        await host.updates.check()
        await asyncio.sleep(0.05)
        assert host.manager.planned is not None
        installed_by_the_server(host)
        assert host.manager.jobs == [] and host.manager.planned is None
        host.playing("finished")
        await host.tick(10)
        assert host.manager.phase == "running" and host.backend.stops == []
        await host.updates.check()  # Steam says the same: nothing planned again
        await asyncio.sleep(0.05)
        assert host.manager.planned is None and not host.updates.waiting

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "the restart after this game is called off: the server has the new version already" in caplog.text


def test_a_restart_planned_for_something_else_isnt_called_off(tmp_path: Path, monkeypatch, run) -> None:
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        host.steam(NEW)
        await host.updates.check()
        await asyncio.sleep(0.05)
        await host.lifecycle.restart(ALPHA, when="after_game", reason="the server is struggling")  # replaces it
        installed_by_the_server(host)
        assert host.manager.planned is not None and host.manager.planned.reason == "the server is struggling"
        host.playing("finished")
        await host.until_running_again()
        assert host.backend.stops == [ALPHA]

    run(scenario())


def test_a_hand_copy_is_taken_over_then_kept_up_to_date_after_games(tmp_path: Path, monkeypatch, run) -> None:
    """Copied into content/Mods by hand, never listed: taken over after one game, a later version in after another."""
    host = Host(tmp_path, monkeypatch, listed=False)

    async def scenario() -> None:
        await host.up()
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        await host.keeper.look_at(host.server)
        await asyncio.sleep(0.05)
        assert host.manager.planned is not None and [j.key for j in host.manager.jobs] == ["workshop-adopt"]
        host.playing("finished")
        await host.until_running_again()
        assert host.installed() == OLD
        assert read_toml(host.folder / "dedicated.toml")["workshop"]["items"] == [ITEM]
        assert not (host.folder / "content" / "Mods" / "HugeGrass").exists()
        # A new version later, players on again.
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        host.steam(NEW)
        await host.updates.check()
        await asyncio.sleep(0.05)
        assert host.manager.planned is not None and [j.key for j in host.manager.jobs] == ["workshop"]
        host.playing("finished")
        await host.until_running_again()
        assert host.installed() == NEW

    run(scenario())


# The audit of 6 November (plans/bug-audit-2026-11-06.md, area 3) -----------------------------------------------------

@pytest.mark.parametrize("by, reason", [("Alice", "asked for by an admin"), ("Watchdog", "it stopped answering after a game")])
def test_a_restart_planned_for_something_else_isnt_called_off_with_the_mod_update(tmp_path: Path, monkeypatch, run, by: str, reason: str) -> None:
    """3.1: the mod update planned over an admin's (or the watchdog's) restart after the game; the server put the mod in
    itself, the workshop's plan was called off, and the restart asked for never happened."""
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        await host.lifecycle.restart(ALPHA, when="after_game", reason=reason, by=by, manual=by != "Watchdog")
        host.steam(NEW)
        await host.updates.check()
        await asyncio.sleep(0.05)
        assert host.manager.planned is not None and host.manager.planned.reason == reason  # the job rides on it
        installed_by_the_server(host)
        assert host.manager.planned is not None and host.manager.planned.reason == reason
        host.playing("finished")
        await host.tick(10)
        assert host.backend.stops

    run(scenario())


def test_both_console_lines_in_one_read_plan_no_restart(tmp_path: Path, monkeypatch, run) -> None:
    """3.2: "update downloaded" and "updated" read together: the update settled before its plan was made in the
    background, and the plan was made anyway: players told, and a restart for nothing (the 3 November bug)."""
    host = Host(tmp_path, monkeypatch)

    async def scenario() -> None:
        await host.up()
        host.state.players[1] = Player(1, "Alice", ALICE)
        host.playing()
        receipt = host.server.content_dir / "Mods" / "Workshop" / str(ITEM) / RECEIPT
        receipt.write_text(json.dumps({"item": ITEM, "manifest": str(NEW), "updated": NEW}), encoding="utf-8")
        host.bus.emit("server.workshop", ALPHA, item=str(ITEM), text="update downloaded; it installs once nobody's on")
        host.bus.emit("server.workshop", ALPHA, item=str(ITEM), text="updated to the version of 18 Oct 2026")
        await asyncio.sleep(0.05)
        assert host.manager.planned is None
        host.playing("finished")
        await host.tick(10)
        assert not host.backend.stops

    run(scenario())
