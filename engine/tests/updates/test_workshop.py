"""Workshop mod updates the 0.7.0 way: the server downloads and installs them itself; Huragok restarts a server whose
map in use holds one up (nobody on: `restart`; people on: a `say`, then `restart now`), confirms it, and warns. Servers
sharing a content folder are restarted together, or taken through the shutdown pattern (plan-0.7.0.md 4.4, S1b)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import free_port
from testkit.setup import make_server_folder
from testkit.workshop import ALPHA, DOWNLOADED, DOWNLOADING, HELD, ITEM, LOCKED, NEW, OLD, UPDATED, Clock, \
    FakeLifecycle, said, service_for, workshop_root

from huragok.core.settings import load
from huragok.core.tomledit import set_table_key, set_top_key
from huragok.steam import SteamItem
from huragok.updates import workshop
from huragok.updates.workshop import RECEIPT, installed_version, is_newer, staged_copies


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """No 3 s between telling the players and the restart, a shared update that looks every 10 ms, and a short look
    for stale copies once it's in."""
    monkeypatch.setattr(workshop, "SAY_SECONDS", 0.0)
    monkeypatch.setattr(workshop, "SHARED_TICK", 0.01)
    monkeypatch.setattr(workshop, "AFTER_SECONDS", 0.05)


def line(service: Any, text: str, server: str = ALPHA, item: int = ITEM) -> None:
    service.bus.emit("server.workshop", server, item=str(item), text=text)


async def settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0.01)


async def until(check: Callable[[], bool], seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never happened")


# Reading the version the way the program does (answer 17) -----------------------------------------------------------

def test_the_version_is_read_from_the_programs_own_receipt(tmp_path: Path) -> None:
    settings_path, folder = workshop_root(tmp_path)
    content = load(settings_path).server(ALPHA).content_dir
    copy = content / "Mods" / "Workshop" / str(ITEM)
    assert installed_version(content, ITEM) == {"item": ITEM, "manifest": OLD * 7, "updated": OLD}
    # The older receipt, which the program never reads, changed alone changes nothing (S1)
    (copy / "reclaimer-workshop-version.json").write_text(json.dumps({"item": ITEM, "manifest": 1, "updated": 1}))
    assert installed_version(content, ITEM)["updated"] == OLD
    receipt = json.loads((copy / RECEIPT).read_text(encoding="utf-8"))
    receipt["version"]["updated"] = 1740000000
    (copy / RECEIPT).write_text(json.dumps(receipt))
    assert installed_version(content, ITEM)["updated"] == 1740000000
    (copy / RECEIPT).write_text("{}")
    assert installed_version(content, ITEM) is None  # no version: not the program's install


def test_a_new_version_needs_both_a_new_manifest_and_a_newer_time() -> None:
    assert is_newer({"updated": OLD, "manifest": 1}, SteamItem(ITEM, "x", NEW, "2"))
    assert not is_newer({"updated": OLD, "manifest": 2}, SteamItem(ITEM, "x", NEW, "2"))  # the same files
    assert not is_newer({"updated": NEW, "manifest": 1}, SteamItem(ITEM, "x", NEW, "2"))  # not newer
    assert not is_newer({"updated": NEW + 9, "manifest": 1}, SteamItem(ITEM, "x", NEW, "2"))
    assert not is_newer({"updated": OLD}, SteamItem(ITEM, "x", None))
    assert is_newer({"updated": OLD, "manifest": 1933178121822970917}, SteamItem(ITEM, "x", NEW, "1933178121822970918"))
    assert not is_newer({"updated": OLD, "manifest": 1933178121822970917}, SteamItem(ITEM, "x", NEW, "1933178121822970917"))


# One server ---------------------------------------------------------------------------------------------------------

def test_nobody_on_the_failed_install_sets_off_a_restart_and_updated_confirms_it(tmp_path: Path, run, caplog) -> None:
    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)
    heard: list[Any] = []
    service.bus.subscribe("server.workshop_updated", heard.append)

    async def scenario() -> None:
        line(service, DOWNLOADING)
        line(service, DOWNLOADED)
        await settle()
        assert lifecycle.sent == []  # downloaded alone: a server whose game doesn't use the mod installs it itself
        assert "mod-update-downloading" in said(service) and "mod-update-players" in said(service)
        line(service, HELD)
        await settle()
        assert lifecycle.to(ALPHA) == ["restart"]  # nobody on: RCON restart, no say
        assert "downloaded, waiting for a restart: the map is in use" in said(service)["mod-update-waiting"]
        assert "at once (nobody is on)" in said(service)["mod-update-waiting"]
        line(service, HELD)  # said again before the restart took: not another restart
        await settle()
        assert lifecycle.to(ALPHA) == ["restart"]
        line(service, UPDATED)
        await settle()
        assert said(service) == {} and not service.pending

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert heard and heard[-1].get("items") == [ITEM]
    assert heard[-1].get("names")[0].endswith(" 16 Oct 2026 -> 18 Oct 2026")  # Discord's card names both
    assert "restarted its game (restart) to put it in" in caplog.text and "the restart put the new version" in caplog.text


def test_people_on_are_told_then_the_restart_drops_them(tmp_path: Path, run) -> None:
    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    lifecycle.counts[ALPHA] = 2
    service = service_for(settings_path, lifecycle)
    service.titles[ITEM] = "HugeGrass SPR"

    async def scenario() -> None:
        lifecycle.playing.add(ALPHA)
        line(service, HELD)  # mid-game (it never is: the server only tries between games): left to the game's end
        await settle()
        assert lifecycle.sent == []
        lifecycle.playing.discard(ALPHA)  # the game ended: its next try fails again
        line(service, HELD)
        await settle()

    run(scenario())
    assert lifecycle.to(ALPHA) == ["say [-.-] PARDON OUR DUST! Restarting for a mod update: the new version of "
                                   "HugeGrass SPR goes in now. Join again in a minute.", "restart now"]
    assert "now its game has ended (players are told first)" in said(service)["mod-update-waiting"]


def test_a_restart_that_didnt_take_is_tried_once_more_then_warned_about(tmp_path: Path, run) -> None:
    settings_path, _ = workshop_root(tmp_path)
    lifecycle, clock = FakeLifecycle(), Clock()
    service = service_for(settings_path, lifecycle, clock=clock)

    async def scenario() -> None:
        line(service, HELD)
        await settle()
        assert lifecycle.to(ALPHA) == ["restart"]
        clock.now += 1
        line(service, HELD)  # in flight as the restart went: not counted
        await settle()
        service.bus.emit("server.started", ALPHA, pid=4242)  # its new game worker
        line(service, HELD)  # still failing after the new worker started: the restart didn't take
        await settle()
        assert lifecycle.to(ALPHA) == ["restart", "restart"]
        clock.now += workshop.FENCE_SECONDS  # no new worker seen, but long enough
        line(service, HELD)
        await settle()
        line(service, HELD)

    run(scenario())
    assert lifecycle.to(ALPHA) == ["restart", "restart"]  # twice, then only a warning
    warning = said(service)["mod-update-stuck"]
    assert "2 restart(s) didn't put the new version in" in warning and "Access is denied" in warning
    assert "mod-update-waiting" not in said(service)
    assert service.warnings.of(ALPHA)[0].rule.discord  # the host must act: the staff channel hears it
    assert "STUCK" in "\n".join(service.item_lines(service.settings.server(ALPHA)))


def test_with_restart_off_it_only_warns_and_the_window_can_put_it_in(tmp_path: Path, run) -> None:
    settings_path, _ = workshop_root(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    settings_path.write_text(text + "\n[workshop_updates]\nrestart = false\n", encoding="utf-8")
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)

    async def scenario() -> None:
        line(service, HELD)
        await settle()
        assert lifecycle.sent == []
        assert "[workshop_updates] restart = false" in said(service)["mod-update-held"]
        assert "It stays waiting" in said(service)["mod-update-held"]
        assert service.held() == [ALPHA]
        ok, message = service.restart_now(ALPHA)  # U -> Workshop mods: a person asked
        await settle()
        assert ok and message == "restarting it to put Workshop item 3820687243 in"

    run(scenario())
    assert lifecycle.to(ALPHA) == ["restart"]
    assert service.restart_now("Ghost") == (False, "no server called Ghost")


def test_a_servers_own_off_leaves_it_out(tmp_path: Path, run) -> None:
    settings_path, _ = workshop_root(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    settings_path.write_text(text + f'\n[[server]]\nname = "{ALPHA}"\nworkshop_updates = "off"\n', encoding="utf-8")
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)

    async def scenario() -> None:
        line(service, HELD)
        await settle()

    run(scenario())
    assert lifecycle.sent == []
    assert "its own workshop_updates = \"off\" in settings.toml" in said(service)["mod-update-held"]


def test_the_lock_line_and_other_lines_set_nothing_off(tmp_path: Path, run) -> None:
    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)

    async def scenario() -> None:
        line(service, LOCKED)  # another server on the folder installing it this moment
        line(service, "installing the update failed (no space left on the disk); trying again")  # not the map
        line(service, "installed")
        await settle()

    run(scenario())
    assert lifecycle.sent == [] and not service.pending


def test_a_second_update_from_the_console_is_restarted_for_too(tmp_path: Path, run) -> None:
    """Each update its own episode: the first one's restart count never holds the next up (audit 2026-11, 8.7)."""
    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)

    async def scenario() -> None:
        for _ in range(3):
            line(service, HELD)
            await settle()
            line(service, UPDATED)
            await settle()

    run(scenario())
    assert lifecycle.to(ALPHA) == ["restart", "restart", "restart"]


def test_a_downloading_line_warns_and_the_staff_channel_hears_it_once(tmp_path: Path, run) -> None:
    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass SPR", NEW, "n")})
    heard: list[Any] = []
    service.bus.subscribe("server.workshop_update", heard.append)

    async def scenario() -> None:
        await service.check()  # Huragok's own look sees it first
        line(service, DOWNLOADING)
        line(service, DOWNLOADING)

    run(scenario())
    assert [e.get("title") for e in heard] == ["HugeGrass SPR"]
    assert said(service)["mod-update-downloading"] == (
        "Info: HugeGrass SPR has a new version on Steam (18 Oct 2026); this server has the old one (16 Oct 2026). "
        "Nothing to do: the server downloads it by itself (its [workshop] update_check_minutes says how often it "
        "looks) and puts it in between games.")
    assert "this PC's copy is a different version from the host's" in said(service)["mod-update-players"]
    assert service.waiting_lists() == {ALPHA: [ITEM]}
    assert "NEW VERSION ON STEAM (18 Oct 2026)" in "\n".join(service.report())


def test_steams_details_settle_it_once_the_new_version_is_in(tmp_path: Path, run) -> None:
    settings_path, folder = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass SPR", NEW, "n")})
    content = service.settings.server(ALPHA).content_dir

    async def scenario() -> None:
        assert await service.check(quiet=False) == {ALPHA: [ITEM]}
        receipt = content / "Mods" / "Workshop" / str(ITEM) / RECEIPT  # the server put it in by itself
        data = json.loads(receipt.read_text(encoding="utf-8"))
        data["version"] = {"item": ITEM, "manifest": "n", "updated": NEW}
        receipt.write_text(json.dumps(data))
        assert await service.check() == {}

    run(scenario())
    assert said(service) == {}


def test_steam_down_is_said_and_nothing_changes(tmp_path: Path, run, caplog) -> None:
    from huragok.core.web import UpdateError

    settings_path, _ = workshop_root(tmp_path)
    service = service_for(settings_path, FakeLifecycle())

    def down(items: list[int]) -> dict[int, SteamItem]:
        raise UpdateError("couldn't reach Steam: timed out")

    service.fetch = down
    with caplog.at_level(logging.INFO):
        assert run(service.check(quiet=False)) == {}
    assert service.last_error == "couldn't reach Steam: timed out" and "Couldn't look for new Workshop versions" in caplog.text
    assert "Last look at Steam failed: couldn't reach Steam: timed out" in service.report()


def test_a_failed_steamcmd_sign_in_is_flagged(tmp_path: Path, run, caplog) -> None:
    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)
    with caplog.at_level(logging.ERROR):
        line(service, "signing in as host failed: sign in to SteamCMD again")
    assert "press I" in lifecycle.flags[ALPHA]["steamcmd"] and "SteamCMD can't sign in" in caplog.text
    line(service, "ready (version of 18 Oct 2026)")
    assert "steamcmd" not in lifecycle.flags[ALPHA]


# A server Huragok didn't start (answer 30) -------------------------------------------------------------------------

def test_a_server_huragok_didnt_start_is_restarted_over_its_console(tmp_path: Path, run, monkeypatch) -> None:
    from huragok.servers import server as manager_module

    settings_path, _ = workshop_root(tmp_path)
    lifecycle = FakeLifecycle("external")
    service = service_for(settings_path, lifecycle)
    content = service.settings.server(ALPHA).content_dir
    monkeypatch.setattr(manager_module, "supervisor_running", lambda server, ours=None: 31092)
    waiting = content / f".workshop-{ITEM}-31092-1"
    waiting.mkdir()
    (content / f".workshop-{ITEM}-999-1").mkdir()  # another program's (a dead server's): not this one's
    assert sorted((c.item, c.pid) for c in staged_copies(content)) == [(ITEM, 999), (ITEM, 31092)]

    async def scenario() -> None:
        lifecycle.playing.add(ALPHA)
        service.act_external(service.look_external())  # waiting during a game: as it should
        service.act_external(service.look_external())
        assert lifecycle.sent == []
        lifecycle.playing.discard(ALPHA)
        service.act_external(service.look_external())  # between games, once: it may be going in this second
        assert lifecycle.sent == []
        service.act_external(service.look_external())  # still there: the map holds it up
        await settle()
        assert lifecycle.to(ALPHA) == ["restart"]  # only ever over its console
        waiting.rmdir()  # it went in as its game restarted
        service.act_external(service.look_external())

    run(scenario())
    assert "quit" not in lifecycle.to(ALPHA) and said(service) == {}


# Servers sharing a content folder (answer 15) ----------------------------------------------------------------------

def shared_root(tmp_path: Path, names: tuple[str, ...] = ("Beta", "Gamma")) -> Path:
    """Alpha with the item installed, and servers in folders of their own whose content is Alpha's."""
    settings_path, _ = workshop_root(tmp_path)
    for name in names:
        folder = tmp_path / "reclaimer" / "SERVERS" / name
        make_server_folder(folder, name, free_port())
        set_top_key(folder / "dedicated.toml", "content", f"../{ALPHA}/content")
        set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    return settings_path


def test_sharing_servers_all_empty_are_restarted_together(tmp_path: Path, run) -> None:
    lifecycle = FakeLifecycle()
    service = service_for(shared_root(tmp_path), lifecycle)

    async def scenario() -> None:
        line(service, HELD, "Beta")  # the one holding the download
        await until(lambda: len(lifecycle.sent) == 3)
        line(service, UPDATED, "Beta")
        await until(lambda: not service.shared)

    run(scenario())
    assert sorted(lifecycle.sent) == [(ALPHA, "restart"), ("Beta", "restart"), ("Gamma", "restart")]
    for name in (ALPHA, "Beta", "Gamma"):
        assert said(service, name) == {}


def test_the_shutdown_pattern_waits_for_the_new_download_before_its_last_restart(tmp_path: Path, run) -> None:
    """S1b (d): players on Alpha and Beta, Gamma empty, Gamma holding the download: Gamma stopped (its download thrown
    away), Alpha and Beta stopped as their games end, all started, and restarted together only once one of them
    has downloaded it again (change 2)."""
    lifecycle = FakeLifecycle()
    lifecycle.counts.update({ALPHA: 3, "Beta": 1})
    lifecycle.playing.update({ALPHA, "Beta"})
    service = service_for(shared_root(tmp_path), lifecycle)
    service.titles[ITEM] = "Albatross Battle"

    async def scenario() -> None:
        line(service, HELD, "Gamma")
        await until(lambda: lifecycle.to("Gamma") == ["quit"])  # 1. the empty one stopped
        await until(lambda: "mod-update-group" in said(service, ALPHA))
        warning = said(service, ALPHA)["mod-update-group"]  # answer 38: which game holds it up, and for how long
        assert "waits for this server's game to end" in warning and "(Gamma)" in warning and "so far" in warning
        assert service.warnings.of(ALPHA)[0].rule.discord
        assert "waiting for Alpha Server, Beta's game" in lifecycle.flags["Gamma"]["workshop"]
        lifecycle.playing.discard("Beta")  # 2. Beta's game ends: told, then stopped
        await until(lambda: lifecycle.to("Beta")[-1:] == ["quit"])
        assert lifecycle.to("Beta")[0].startswith("say [-.-] PARDON OUR DUST! Stopping for a mod update: this server "
                                                  "is back once the new version of Albatross Battle is in.")
        assert lifecycle.to(ALPHA) == []
        lifecycle.playing.discard(ALPHA)  # the last game ends
        await until(lambda: [what for _, what in lifecycle.sent].count("start") == 3)  # 4. all started
        assert said(service, ALPHA) == {}  # nothing holds it up now
        lifecycle.counts.update({ALPHA: 1, "Beta": 0})  # one of Alpha's players rejoined
        await settle(10)
        assert not [what for _, what in lifecycle.sent if what.startswith("restart")]  # not as soon as they're up
        line(service, DOWNLOADED, ALPHA)  # 5. the download is in
        await until(lambda: ("Gamma", "restart") in lifecycle.sent)
        line(service, UPDATED, ALPHA)
        await until(lambda: not service.shared)

    run(scenario())
    assert lifecycle.to(ALPHA)[-2:] == ["say [-.-] PARDON OUR DUST! Restarting for a mod update: the new version of "
                                        "Albatross Battle goes in now. Join again in a minute.", "restart now"]
    assert lifecycle.to("Beta")[-2:] == ["start", "restart"] and lifecycle.to("Gamma") == ["quit", "start", "restart"]


def test_the_last_one_up_holding_the_download_is_restarted_instead_of_stopped(tmp_path: Path, run) -> None:
    """S1b (k), change 1: one download, one drop."""
    lifecycle = FakeLifecycle()
    lifecycle.counts["Beta"] = 2
    lifecycle.playing.add("Beta")
    service = service_for(shared_root(tmp_path), lifecycle)

    async def scenario() -> None:
        line(service, HELD, "Beta")
        await until(lambda: lifecycle.to(ALPHA) == ["quit"] and lifecycle.to("Gamma") == ["quit"])
        assert lifecycle.to("Beta") == []
        lifecycle.playing.discard("Beta")  # its game ends
        await until(lambda: lifecycle.to("Beta")[-1:] == ["restart now"])
        assert lifecycle.to(ALPHA) == ["quit"]  # not started before it's in
        line(service, UPDATED, "Beta")
        await until(lambda: lifecycle.to(ALPHA) == ["quit", "start"] and lifecycle.to("Gamma") == ["quit", "start"])

    run(scenario())
    assert lifecycle.to("Beta")[0].startswith("say [-.-] PARDON OUR DUST! Restarting for a mod update")
    assert "quit" not in lifecycle.to("Beta")


def test_one_off_server_holds_the_group_and_is_named(tmp_path: Path, run) -> None:
    settings_path = shared_root(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    settings_path.write_text(text + '\n[[server]]\nname = "Gamma"\nworkshop_updates = "off"\n', encoding="utf-8")
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle)

    async def scenario() -> None:
        line(service, HELD, "Beta")
        await settle()

    run(scenario())
    assert lifecycle.sent == [] and not service.shared
    warning = said(service, "Beta")["mod-update-held-shared"]
    assert "Gamma has workshop_updates = \"off\"" in warning and "It stays waiting" in warning


def test_a_server_huragok_didnt_start_gets_a_warning_instead_of_the_pattern(tmp_path: Path, run) -> None:
    lifecycle = FakeLifecycle()
    lifecycle.phases["Gamma"] = "external"
    lifecycle.counts[ALPHA] = 1
    service = service_for(shared_root(tmp_path), lifecycle)

    async def scenario() -> None:
        line(service, HELD, "Beta")
        await settle()
        assert lifecycle.sent == [] and not service.shared
        assert "Gamma wasn't started by Huragok" in said(service, "Beta")["mod-update-external"]
        lifecycle.counts[ALPHA] = 0  # nobody on any of them now: restarted together, Gamma over its console too
        line(service, HELD, "Beta")
        await until(lambda: len(lifecycle.sent) == 3)

    run(scenario())
    assert sorted(lifecycle.sent) == [(ALPHA, "restart"), ("Beta", "restart"), ("Gamma", "restart")]
    assert "mod-update-external" not in said(service, "Beta")


def test_a_server_still_retrying_a_stale_copy_is_stopped_and_started_again(tmp_path: Path, run, monkeypatch) -> None:
    """S1b (f): two downloads; after the update one keeps trying to install its own, now stale, copy."""
    monkeypatch.setattr(workshop, "AFTER_SECONDS", 30.0)
    lifecycle, clock = FakeLifecycle(), Clock()
    service = service_for(shared_root(tmp_path, ("Beta",)), lifecycle, clock=clock)

    async def scenario() -> None:
        line(service, HELD, "Beta")
        await until(lambda: len(lifecycle.sent) == 2)
        clock.now += 5
        line(service, UPDATED, "Beta")
        await until(lambda: service.shared and next(iter(service.shared.values())).step == "after")
        clock.now += 5
        line(service, LOCKED, ALPHA)  # the loser: first the lock, then its stale copy
        line(service, HELD, ALPHA)
        await until(lambda: lifecycle.to(ALPHA)[-2:] == ["quit", "start"])
        clock.now += workshop.AFTER_SECONDS
        await until(lambda: not service.shared)

    run(scenario())
    assert lifecycle.to("Beta") == ["restart"]


# U -> Workshop mods, through Huragok ------------------------------------------------------------------------------

def test_the_window_asks_huragok_what_waits_and_to_put_it_in(tmp_path: Path, run) -> None:
    from huragok.huragok import Huragok

    settings_path, _ = workshop_root(tmp_path)
    supervisor = Huragok(load(settings_path))
    supervisor.workshop_updates.fetch = lambda items: {ITEM: SteamItem(ITEM, "HugeGrass", NEW, "n")}
    try:
        result = run(supervisor.request("check_workshop", {}))
        assert result.ok and result.message == f"new versions not in yet on {ALPHA}"
        assert "waiting for a restart" not in result.message  # nothing for the window to offer
        assert f"{ITEM} HugeGrass" in result.text and "NEW VERSION ON STEAM" in result.text
        assert run(supervisor.request("update_workshop", {})).message == \
            "no downloaded Workshop update is waiting for a restart"
    finally:
        supervisor.storage.close()
