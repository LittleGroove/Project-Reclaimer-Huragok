"""Workshop mods kept up to date: noticed (Steam's details, the server's console), planned after the game,
swapped in while the server is stopped, and put back when the fetch fails."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from conftest import free_port
from test_setup import make_server_folder, setup_root

from huragok.config import load
from huragok.dedicated import tool_for
from huragok.events import Event, EventBus
from huragok.manager import StoppedJob
from huragok.tomledit import read_toml, set_table_key
from huragok.workshopupdates import SteamItem, WorkshopUpdateService, installed_version, is_newer

ITEM = 3820687243
OLD, NEW = 1792148310, 1792300975  # 16 Oct and 18 Oct 2026: the real item's two versions
ALPHA = "Alpha Server"


class FakeLifecycle:
    def __init__(self, phase: str = "running") -> None:
        self.state = phase
        self.jobs: dict[str, list[StoppedJob]] = {}
        self.flags: dict[str, dict[str, str]] = {}
        self.restarts: list[tuple[str, str, str]] = []

    def add_job(self, server: str, job: StoppedJob) -> None:
        self.jobs[server] = [j for j in self.jobs.get(server, []) if j.key != job.key] + [job]

    def drop_job(self, server: str, key: str) -> bool:
        before = len(self.jobs.get(server, []))
        self.jobs[server] = [j for j in self.jobs.get(server, []) if j.key != key]
        return len(self.jobs[server]) != before

    def set_flag(self, server: str, who: str, text: str | None) -> None:
        if text:
            self.flags.setdefault(server, {})[who] = text
        else:
            self.flags.get(server, {}).pop(who, None)

    def phase(self, server: str) -> str:
        return self.state

    async def restart(self, server: str, *, when: str, reason: str, by: str | None = None, countdown=None) -> Any:
        self.restarts.append((server, when, reason))


def workshop_root(tmp_path: Path, steam: int = OLD) -> tuple[Path, Path]:
    """Alpha with item ITEM installed (by the fake `dedicated mods`) at version `steam`."""
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): steam}), encoding="utf-8")
    result = tool_for(load(settings_path)).mods(folder)
    assert result.ok and "ready (version of 16 Oct 2026)" in result.text, result.text
    return settings_path, folder


def service_for(settings_path: Path, lifecycle: FakeLifecycle, steam: dict[int, SteamItem]) -> WorkshopUpdateService:
    settings = load(settings_path)
    return WorkshopUpdateService(settings, EventBus(), lifecycle, tool=lambda: tool_for(settings),  # type: ignore[arg-type]
                                 fetch=lambda items: {i: steam[i] for i in items if i in steam})


def test_comparing_versions() -> None:
    assert is_newer({"updated": OLD, "manifest": 1}, SteamItem(ITEM, "x", NEW, "2"))
    assert not is_newer({"updated": NEW, "manifest": 2}, SteamItem(ITEM, "x", NEW, "2"))
    assert not is_newer({"updated": OLD, "manifest": 2}, SteamItem(ITEM, "x", NEW, "2"))  # the same files
    assert not is_newer({"updated": OLD}, SteamItem(ITEM, "x", None))


def test_a_new_version_is_planned_after_the_game_and_swapped_in(tmp_path: Path, run, caplog) -> None:
    settings_path, folder = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass Rebalance", NEW, "n")})
    server = service.settings.server(ALPHA)
    assert installed_version(server.content_dir, ITEM)["updated"] == OLD

    async def scenario() -> None:
        waiting = await service.check(quiet=False)
        await asyncio.sleep(0)
        assert [w.item for w in waiting[ALPHA]] == [ITEM]
        assert lifecycle.restarts == [(ALPHA, "after_game", "installing a new version of HugeGrass Rebalance")]
        assert [j.key for j in lifecycle.jobs[ALPHA]] == ["workshop"]
        assert "mod update waiting: HugeGrass Rebalance" in lifecycle.flags[ALPHA]["workshop"]
        await service.check()  # asked again: no second plan for the same version
        assert len(lifecycle.restarts) == 1
        # Steam serves the new version now; the job runs with the server stopped.
        (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): NEW}), encoding="utf-8")
        said = await lifecycle.jobs[ALPHA][0].run()
        assert said.startswith("Workshop updated: HugeGrass Rebalance") and "18 Oct 2026" in said
        assert installed_version(server.content_dir, ITEM)["updated"] == NEW
        assert not list((server.content_dir.parent / ".huragok-workshop-previous").iterdir())  # the old copy gone
        assert not service.waiting and "workshop" not in lifecycle.flags.get(ALPHA, {})

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "a new version of HugeGrass Rebalance (18 Oct 2026) is out (installed: 16 Oct 2026)" in caplog.text


def test_a_failed_fetch_keeps_the_old_version(tmp_path: Path, run, caplog) -> None:
    settings_path, folder = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass Rebalance", NEW, "n")})
    server = service.settings.server(ALPHA)
    (folder / "fake_steam_down").write_text("", encoding="utf-8")  # SteamCMD's sign-in has expired

    async def scenario() -> None:
        await service.check()
        lifecycle.jobs.clear()  # the manager takes the jobs as it runs them
        said = await service.install(server)
        assert said == "Workshop not updated; the old version(s) kept"
        assert installed_version(server.content_dir, ITEM)["updated"] == OLD  # back where it was
        assert not lifecycle.jobs.get(ALPHA)  # never added again during this start: the server starts on the old one
        assert "press I" in lifecycle.flags[ALPHA]["steamcmd"]
        assert "tried again after a later game" in lifecycle.flags[ALPHA]["workshop"]
        assert service.waiting[ALPHA][ITEM]  # still waiting, for a later try

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert "SteamCMD can't sign in" in caplog.text and "tried again after a later game" in caplog.text


def test_what_the_server_says_about_its_items(tmp_path: Path, run) -> None:
    settings_path, folder = workshop_root(tmp_path)
    lifecycle = FakeLifecycle(phase="idle")
    service = service_for(settings_path, lifecycle, {})

    async def scenario() -> None:
        service.bus.emit("server.workshop", ALPHA, item=str(ITEM),
                         text="update downloaded; it installs once Alpha Server has no players")
        await asyncio.sleep(0)
        assert service.waiting[ALPHA][ITEM].downloaded and lifecycle.jobs[ALPHA]
        assert lifecycle.restarts == []  # stopped: it installs as it starts
        assert "installs as it starts" in lifecycle.flags[ALPHA]["workshop"]
        service.bus.emit("server.workshop", ALPHA, item=str(ITEM), text="updated")  # it emptied and did it itself
        assert not service.waiting and not lifecycle.jobs[ALPHA]
        service.bus.emit("server.workshop", ALPHA, item=str(ITEM),
                         text="signing in as host failed: sign in to SteamCMD again")
        assert "press I" in lifecycle.flags[ALPHA]["steamcmd"]
        service.bus.emit("server.workshop", ALPHA, item=str(ITEM), text="ready (version of 18 Oct 2026)")
        assert "steamcmd" not in lifecycle.flags[ALPHA]

    run(scenario())


def test_shared_content_and_off_are_left_alone(tmp_path: Path, run) -> None:
    settings_path, folder = workshop_root(tmp_path)
    second = tmp_path / "reclaimer" / "SERVERS" / "Beta"
    make_server_folder(second, "Beta", free_port())
    set_table_key(second / "dedicated.toml", "rcon", "password", "secretsecret")
    from huragok.tomledit import set_top_key

    set_top_key(second / "dedicated.toml", "content", f"../{ALPHA}/content")  # Alpha's content, items and all
    set_table_key(second / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})
    assert service.shared_with(service.settings.server(ALPHA)) == ["Beta"]

    async def scenario() -> None:
        await service.check()
        assert lifecycle.restarts == [] and not lifecycle.jobs  # the program swaps a shared item for both at once
        assert "content shared with Beta" in lifecycle.flags[ALPHA]["workshop"]

    run(scenario())
    off = load(settings_path)
    off.workshop_updates.install = "off"
    service = WorkshopUpdateService(off, EventBus(), FakeLifecycle(), fetch=lambda items: {})  # type: ignore[arg-type]
    assert run(service.check()) == {}  # nothing even asked


def test_the_servers_look_as_often_as_huragok(tmp_path: Path) -> None:
    settings_path, folder = workshop_root(tmp_path)
    service = service_for(settings_path, FakeLifecycle(), {})
    assert service.write_check_minutes() == [ALPHA]
    data = read_toml(folder / "dedicated.toml")
    assert data["workshop"]["update_check_minutes"] == 5 and data["workshop"]["items"] == [ITEM]
    assert service.write_check_minutes() == []  # once
    report = "\n".join(service.report())
    assert f"{ITEM}" in report and "installed 16 Oct 2026" in report and "after the game" in report


# A fetch that fails, brings back the same version, or is cut short ---------------------------------

def test_a_failed_fetch_never_keeps_the_server_from_starting(tmp_path: Path, run) -> None:
    """The real manager: the job runs once, the old copy goes back and the server starts on it."""
    import time

    from test_manager import FakeBackend, FakeClient

    from huragok.manager import Fleet, Lifecycle
    from huragok.state import ServerState

    settings_path, folder = workshop_root(tmp_path)
    settings = load(settings_path)
    bus = EventBus()
    state = ServerState(ALPHA)
    backend = FakeBackend(settings, bus, {ALPHA: state})
    fleet = Fleet(settings, backend, bus)
    manager = fleet.add(settings.servers[0], FakeClient(state), state)  # type: ignore[arg-type]
    manager.record.wanted = "running"
    service = WorkshopUpdateService(settings, bus, Lifecycle(fleet), tool=lambda: tool_for(settings),
                                    fetch=lambda items: {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})
    runs: list[str] = []
    real_install = service.install

    async def counted(server: Any) -> str:
        said = await real_install(server)
        runs.append(said)
        return said

    service.install = counted  # type: ignore[method-assign]
    (folder / "fake_steam_down").write_text("", encoding="utf-8")  # SteamCMD's sign-in has expired

    async def scenario() -> None:
        await service.check()  # the server is stopped: "installs as it starts"
        assert [j.key for j in manager.jobs] == ["workshop"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not backend.starts:
            manager.tick(time.time())
            await asyncio.sleep(0.05)

    run(scenario())
    assert backend.starts == [ALPHA] and runs == ["Workshop not updated; the old version(s) kept"]
    assert installed_version(settings.servers[0].content_dir, ITEM)["updated"] == OLD
    assert service.waiting[ALPHA][ITEM] and not manager.jobs  # still waiting, for a later game


def test_a_version_that_keeps_failing_is_tried_three_times_then_left(tmp_path: Path, run, caplog) -> None:
    settings_path, folder = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})
    server = service.settings.server(ALPHA)
    (folder / "fake_steam_down").write_text("", encoding="utf-8")

    async def scenario() -> None:
        for attempt in range(3):  # each look at Steam plans one more try after a game
            await service.check()
            await asyncio.sleep(0)
            assert [j.key for j in lifecycle.jobs.get(ALPHA, [])] == ["workshop"], attempt
            job, lifecycle.jobs[ALPHA] = lifecycle.jobs[ALPHA][0], []
            assert await job.run() == "Workshop not updated; the old version(s) kept"
        assert len(lifecycle.restarts) == 3
        await service.check()
        await asyncio.sleep(0)
        assert not lifecycle.jobs.get(ALPHA) and len(lifecycle.restarts) == 3  # stopped trying that version
        assert "didn't install (3 tries): U -> Workshop mods to try again" in lifecycle.flags[ALPHA]["workshop"]
        assert "didn't install in 3 tries; installing it here tries again" in "\n".join(service.item_lines(server))
        # A person asks (U -> Workshop mods): tried again, and this time Steam answers.
        (folder / "fake_steam_down").unlink()
        (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): NEW}), encoding="utf-8")
        ok, _ = await service.install_now(ALPHA, "after_game")
        assert ok and (await lifecycle.jobs[ALPHA][0].run()).startswith("Workshop updated: HugeGrass")
        assert not service.failures

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert caplog.text.count("tried again after a later game") == 2
    assert "HugeGrass didn't install after 3 tries" in caplog.text and "To try again: U -> Workshop mods" in caplog.text


def test_a_fetch_that_brought_back_the_same_version_isnt_an_update(tmp_path: Path, run, caplog) -> None:
    settings_path, folder = workshop_root(tmp_path)  # installed 16 Oct; the fake SteamCMD still serves 16 Oct
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})
    server = service.settings.server(ALPHA)

    async def scenario() -> str:
        await service.check()
        await asyncio.sleep(0)
        return await lifecycle.jobs[ALPHA][0].run()

    with caplog.at_level(logging.INFO):
        said = run(scenario())
    assert said == "Workshop not updated; the old version(s) kept"
    assert installed_version(server.content_dir, ITEM)["updated"] == OLD
    assert service.waiting[ALPHA][ITEM] and service.failures[(ALPHA, ITEM)] == (NEW, 1) and not service.done
    assert "the fetch brought back the version it had: HugeGrass (16 Oct 2026)" in caplog.text


class SlowTool:
    """`dedicated mods` that starts a new copy, then takes its time; stop ends it."""

    def __init__(self, live: Path) -> None:
        self.live = live
        self.started = False
        self.stopped = False

    def mods(self, folder: Path, *, stop: Any = None) -> Any:
        import time

        from huragok.dedicated import ToolResult

        (self.live / "maps").mkdir(parents=True, exist_ok=True)  # half a new copy, no receipt yet
        self.started = True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                self.stopped = True
                return ToolResult(False, 130, "mods was stopped before it finished")
            time.sleep(0.02)
        return ToolResult(False, 2, "Error: too slow")


def test_a_job_cut_short_puts_the_old_copy_back(tmp_path: Path, run) -> None:
    import pytest
    from conftest import wait_for

    settings_path, folder = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})
    server = service.settings.server(ALPHA)
    live = server.content_dir / "Mods" / "Workshop" / str(ITEM)
    tool = SlowTool(live)
    service.tool = lambda: tool  # type: ignore[assignment,return-value]

    async def scenario() -> None:
        await service.check()
        job = asyncio.ensure_future(lifecycle.jobs[ALPHA][0].run())
        await wait_for(lambda: tool.started)
        job.cancel()  # what asyncio.run does to the manager's job when Huragok closes or relaunches
        with pytest.raises(asyncio.CancelledError):
            await job

    run(scenario())
    assert tool.stopped  # `dedicated mods` was ended, not left running
    assert installed_version(server.content_dir, ITEM)["updated"] == OLD  # the server starts with its mod
    aside = server.content_dir.parent / ".huragok-workshop-previous"
    assert not list(aside.glob("*.moving.json")) and [p.name for p in aside.iterdir()][0].endswith("-failed")


def test_a_job_cut_short_by_a_crash_is_put_right_at_the_next_start(tmp_path: Path) -> None:
    import os

    from huragok import upkeep

    settings_path, folder = workshop_root(tmp_path)
    settings = load(settings_path)
    server = settings.server(ALPHA)
    live = server.content_dir / "Mods" / "Workshop" / str(ITEM)
    aside_root = server.content_dir.parent / ".huragok-workshop-previous"
    aside = aside_root / f"{server.slug}-{ITEM}-20261018-120000"
    aside_root.mkdir()
    marker = aside_root / f"{server.slug}-20261018-120000.moving.json"
    marker.write_text(json.dumps({"server": ALPHA, "moves": [[str(live), str(aside)]]}), encoding="utf-8")
    os.replace(live, aside)
    (live / "maps").mkdir(parents=True)  # the fetch got this far when Huragok went down: no receipt
    assert not [l for l in upkeep.cleanable(settings) if l.path == aside]  # the only good copy: never offered
    marker.unlink()
    assert not [l for l in upkeep.cleanable(settings) if l.path == aside]  # its item isn't whole in the live folder
    marker.write_text(json.dumps({"server": ALPHA, "moves": [[str(live), str(aside)]]}), encoding="utf-8")
    service_for(settings_path, FakeLifecycle(), {})  # Huragok starts again
    assert installed_version(server.content_dir, ITEM)["updated"] == OLD and not marker.exists()
    offered = [l.path.name for l in upkeep.cleanable(settings) if ".huragok-workshop-previous" in l.label]
    assert offered == [f"{aside.name}-failed"]  # the half-fetched copy; the old one is back in place
    (aside_root / f"{server.slug}-{ITEM}-20261017-120000").mkdir()  # an old copy whose item is installed
    assert len([l for l in upkeep.cleanable(settings) if ".huragok-workshop-previous" in l.label]) == 2


def test_shared_content_is_never_installed_from_here(tmp_path: Path, run) -> None:
    from huragok.tomledit import set_top_key

    settings_path, folder = workshop_root(tmp_path)
    second = tmp_path / "reclaimer" / "SERVERS" / "Beta"
    make_server_folder(second, "Beta", free_port())
    set_table_key(second / "dedicated.toml", "rcon", "password", "secretsecret")
    set_top_key(second / "dedicated.toml", "content", f"../{ALPHA}/content")  # Beta uses Alpha's content
    set_table_key(second / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")})

    async def scenario() -> None:
        await service.check()
        assert "Beta" in service.waiting_lists() and service.installable() == []
        ok, message = await service.install_now("Beta", "after_game")
        assert not ok and message.startswith("its content folder is shared with Alpha Server")
        assert lifecycle.restarts == [] and not lifecycle.jobs
        said = await service.install(service.settings.server("Beta"))
        assert said == "left to the server program: its content folder is shared with Alpha Server"

    run(scenario())
    assert installed_version(service.settings.server(ALPHA).content_dir, ITEM)["updated"] == OLD  # untouched
    # U -> Workshop mods: not offered for install, and says why.
    from huragok.core import Supervisor

    supervisor = Supervisor(load(settings_path))
    supervisor.workshop_updates.fetch = lambda items: {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")}
    try:
        result = run(supervisor._request("check_workshop", {}))
        assert result.ok and "waiting" not in result.message  # the window asks "install when?" only on "waiting"
        assert result.message == ("nothing for Huragok to install; Alpha Server, Beta share their content folder, so "
                                  "the server program installs theirs once nobody's on")
        assert run(supervisor._request("update_workshop", {})).message == "no new Workshop version is waiting"
    finally:
        supervisor.storage.close()
