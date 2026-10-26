"""The whole supervisor against a fake server: its parts, commands, moderation, restarts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, MOD, OWNER, staff, wait_for, write_server_folder, write_settings
from testkit.fake_rcon import FakeRcon
from testkit.running import SERVER, everyone_joins, running, says, tells_to

from huragok.core import mailbox
from huragok.core.events import Event
from huragok.core.part import Part, on
from huragok.core.settings import load
from huragok.huragok import Huragok
from huragok.parts import PARTS


def test_commands_mute_by_a_moderator(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (supervisor, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(supervisor.superintendent.states[SERVER].players) == 5)
            await fake.chat(MOD, "Mod", '!mute bravo 30m "being rude"')
            mute = await wait_for(lambda: fake.named("mute"))
            assert mute[0]["args"] == [BRAVO, "30m", "being rude"] and mute[0]["by"] == "Mod"
            await wait_for(lambda: tells_to(fake, MOD))
            action = next(e for e in seen if e.name == "moderation.action")
            assert action["kind"] == "mute" and action["source"] == "commands" and not action["dry_run"]
            row = supervisor.storage.actions_for(BRAVO)[0]
            assert (row["kind"], row["by"], row["reason"], row["duration"]) == ("mute", "Mod", "being rude", "30m")
            assert supervisor.storage.player(BRAVO).last_name == "Bravo"

            # A moderator can't mute an admin, and a player can't mute anyone.
            await fake.chat(MOD, "Mod", "!mute Admin")
            await wait_for(lambda: any("can't do that" in t for t in tells_to(fake, MOD)))
            await fake.chat(ALICE, "Alice", "!mute Bravo")
            await wait_for(lambda: any("moderators and up" in t for t in tells_to(fake, ALICE)))
            assert len(fake.named("mute")) == 1

    run(scenario())


def test_commands_ambiguous_names_and_admin_commands(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (supervisor, fake, _):
            await everyone_joins(fake)
            await fake.join(6, "Alice", "9" * 64)
            await wait_for(lambda: len(supervisor.superintendent.states[SERVER].players) == 6)
            await fake.chat(OWNER, "Owner", "!kick alice")
            # Two players with one name: each with what tells them apart (how long they've been on, the start of
            # their player ID), and the #number to use.
            await wait_for(lambda: any(re.fullmatch(r"'Alice' could be #3 Alice \(on \d+s, cccccccc\); #6 Alice "
                                                    r"\(on \d+s, 99999999\)\. Use the #number\.", t)
                                       for t in tells_to(fake, OWNER)))
            assert not fake.named("kick")
            await fake.chat(OWNER, "Owner", "!map The Pit")
            assert (await wait_for(lambda: fake.named("map")))[0]["command"] == "map The Pit"
            await fake.chat(MOD, "Mod", "!map Guardian")  # admins only
            await wait_for(lambda: any("admins and up" in t for t in tells_to(fake, MOD)))
            await fake.chat(OWNER, "Owner", f"!ban {'8' * 64} 7d alt account")  # offline, by ID
            ban = await wait_for(lambda: fake.named("ban"))
            assert ban[0]["args"] == ["8" * 64, "7d", "alt account"]

    run(scenario())


def test_dry_run_records_without_sending(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, 'dry_run = true') as (sup, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(OWNER, "Owner", "!kick Bravo spam")
            await wait_for(lambda: any("Dry run" in t for t in tells_to(fake, OWNER)))
            assert not fake.named("kick")
            assert sup.storage.actions_for(BRAVO)[0]["dry_run"] == 1

    run(scenario())


class Broken(Part):
    name = "broken"
    description = "Fails at everything"

    @on("rcon.chat")
    async def chat(self, event: Event) -> None:
        raise RuntimeError("oops")


def test_a_broken_part_is_switched_off_alone(tmp_path: Path, run, monkeypatch) -> None:
    monkeypatch.setitem(PARTS, "broken", Broken)

    async def scenario() -> None:
        async with running(tmp_path, '[broken]\nenabled = true') as (sup, fake, seen):
            await everyone_joins(fake)
            for _ in range(5):
                await fake.chat(ALICE, "Alice", "hello")
            off = await wait_for(lambda: next((e for e in seen if e.name == "supervisor.part_disabled"), None))
            assert off["part"] == "broken" and "failures" in off["reason"]
            assert sup.runners["commands"].enabled
            await fake.chat(OWNER, "Owner", "!players")
            await wait_for(lambda: any("#4 Bravo" in t for t in tells_to(fake, OWNER)))

    run(scenario())


async def through_rcon(fake: FakeRcon, seen: list[Event]) -> None:
    """Everything the fake server sent before now has been dealt with: a marker sent after it has come through."""
    marker = f"marker {len(seen)} {time.monotonic()}"
    await fake.chat(ALICE, "Alice", marker)
    await wait_for(lambda: any(e.name == "rcon.chat" and e.get("text") == marker for e in seen))


async def handled(runner: Any) -> None:
    """Everything queued for this part before now has been handled (its queue runs one job at a time, in order)."""
    done = asyncio.Event()

    async def mark() -> None:
        done.set()

    runner.submit("marker", mark)
    await asyncio.wait_for(done.wait(), 5)


def _marker_backend(tmp_path: Path) -> str:
    marker = (tmp_path / "restarts.txt").as_posix()
    python = Path(sys.executable).as_posix()
    command = f'"{python}" -c "open(r\'{marker}\', \'a\').write(\'{{slug}}\\n\')"'
    return f"backend = \"command\"\n[command]\nrestart = '''{command}'''\n"


def test_restart_after_the_game_that_was_played(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        body = _marker_backend(tmp_path) + "[restarts]\nafter_game_seconds = 1\n"
        async with running(tmp_path, body) as (sup, fake, seen):
            await fake.join(1, "Alice", ALICE)
            await wait_for(lambda: sup.superintendent.states[SERVER].players)
            await fake.event("phase", phase="playing", map="Valhalla", mode="Slayer")
            await wait_for(lambda: sup.superintendent.states[SERVER].phase == "playing")
            await sup.superintendent.lifecycle.restart(SERVER, when="after_game", reason="testing")
            assert sup.superintendent.fleet.managers[SERVER].phase == "running"  # the server is up: noticed at once
            await wait_for(lambda: any("after this game" in s for s in says(fake)))
            # Seen on 0.9.18: `finished` for the next game before it starts. Not the end of ours.
            await fake.event("phase", phase="finished", map="The Pit", mode="Slayer")
            await through_rcon(fake, seen)  # the phase event has been dealt with
            assert not (tmp_path / "restarts.txt").exists() and sup.superintendent.lifecycle.planned
            await fake.event("phase", phase="finished", map="Valhalla", mode="Slayer")
            await wait_for(lambda: any("restarting in 1 second" in s for s in says(fake)))
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())
            assert (tmp_path / "restarts.txt").read_text().strip() == "lg-test-server"
            await wait_for(lambda: any(e.name == "server.restarting" for e in seen))
            await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "running")  # RCON still answers: up again
            assert any(e.name == "server.restarted" for e in seen)

    run(scenario())


def test_watchdog_restarts_after_a_mass_drop(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        body = _marker_backend(tmp_path)  # the watchdog acts by default
        async with running(tmp_path, body) as (sup, _, seen):
            # A full server is slow all game: by default that restarts nothing (shown as critical, only logged).
            watchdog = sup.runners["watchdog"].part
            for _ in range(3):
                sup.bus.emit("log.falling_behind", SERVER, fps=60, behind=60, window=60, longest_ms=133, players=127)
            await wait_for(lambda: len(watchdog.samples.get(SERVER, ())) == 3)
            await watchdog._update(SERVER)
            assert watchdog.level[SERVER] == "critical"
            sup.bus.emit("log.roster_lost", SERVER, lost=3, of=20, present=17)  # a few players: not a collapse
            # The live server's mid-game drop: most went, three stayed, and they all came back by themselves.
            sup.bus.emit("log.roster_lost", SERVER, lost=70, of=73, present=3)
            await handled(sup.runners["watchdog"])
            assert not any(e.name in ("server.restart_planned", "server.restarting") for e in seen)
            sup.bus.emit("log.roster_lost", SERVER, lost=28, of=28, present=0, details="{}")
            planned = await wait_for(lambda: next((e for e in seen if e.name == "server.restarting"), None))
            assert planned["by"] == "Watchdog" and "dropped" in planned["reason"]
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())

    run(scenario())


def test_watchdog_restarts_a_server_nobody_can_join(tmp_path: Path, run, caplog) -> None:
    async def scenario() -> None:
        async with running(tmp_path, _marker_backend(tmp_path)) as (sup, _, seen):
            watchdog = sup.runners["watchdog"].part
            now = time.time()
            for ago in (400, 300, 200):  # three connected, each over two minutes ago, and nobody joined
                await watchdog.connected(Event("log.connected", SERVER, {"name": "x"}, now - ago))
            await watchdog._update(SERVER)
            restarting = await wait_for(lambda: next((e for e in seen if e.name == "server.restarting"), None))
            assert restarting["reason"] == "nobody can join"
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())

    with caplog.at_level(logging.WARNING):
        run(scenario())
    # actnow is the Superintendent's emergency, in its own words
    assert any(r.name == "part.watchdog" and "METROPOLITAN EMERGENCY! Restarting it now: nobody can join" in r.getMessage()
               for r in caplog.records)


def test_watchdog_cases_follow_their_own_settings(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        body = _marker_backend(tmp_path) + '[watchdog]\ndead = "watch"\nmemory = "act"\nmemory_mb = 1000\n'
        async with running(tmp_path, body) as (sup, _, seen):
            watchdog = sup.runners["watchdog"].part
            sup.bus.emit("log.roster_lost", SERVER, lost=28, of=28, present=0)  # dead, but dead is watch
            await wait_for(lambda: watchdog.level[SERVER] == "critical")
            await handled(sup.runners["watchdog"])
            assert not any(e.name in ("server.restart_planned", "server.restarting") for e in seen)
            watchdog.memory[SERVER] = 4096.0  # as measured; act: after the game, and nobody is playing
            await watchdog._update(SERVER)
            planned = await wait_for(lambda: next((e for e in seen if e.name == "server.restart_planned"), None))
            assert planned["when"] == "after_game" and planned["reason"] == "the server is using too much memory"
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())

    run(scenario())


def test_keepalive_restarts_a_hung_server(tmp_path: Path, run) -> None:
    def quick(settings: Any) -> None:  # below what settings.toml allows
        settings.keepalive.hang_seconds, settings.keepalive.check_seconds = 1.0, 0.2

    async def scenario() -> None:
        async with running(tmp_path, _marker_backend(tmp_path), status_age=600, adjust=quick) as (sup, fake, seen):
            await wait_for(lambda: sup.superintendent.states[SERVER].last_rcon_ok)  # the first refresh is done
            await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "running")
            sup.superintendent.states[SERVER].last_rcon_ok = time.monotonic() - 60  # RCON quiet too
            sup.superintendent.watchers[SERVER].poll_status()
            hung = await wait_for(lambda: next((e for e in seen if e.name == "server.hung"), None))
            assert hung.server == SERVER
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())

    run(scenario())


def test_attach_mode_cannot_restart(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, _, _):
            assert not sup.superintendent.lifecycle.can_control
            await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "external")
            row = sup.superintendent.fleet.rows()[0]
            assert "watching" in row["status"] and not row["can"]["restart"]
            try:
                await sup.superintendent.lifecycle.restart(SERVER)
            except Exception as error:  # noqa: BLE001
                assert "attach" in str(error)
            else:
                raise AssertionError("attach mode restarted something")

    run(scenario())


def test_eventlog_writes_json_lines(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, '[eventlog]\nenabled = true') as (_, fake, _):
            await fake.chat(ALICE, "Alice", "gg ✓")
            folder = tmp_path / "logs" / "events"
            lines = await wait_for(lambda: [json.loads(l) for f in folder.glob("*.jsonl")
                                            for l in f.read_text(encoding="utf-8").splitlines()
                                            if '"rcon.chat"' in l])
            assert lines[0]["text"] == "gg ✓" and lines[0]["server"] == SERVER
        # Stopping lets each part finish what was queued before tearing it down.
        written = "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "logs" / "events").glob("*.jsonl"))
        assert '"supervisor.stopping"' in written

    run(scenario())


def test_commands_toml_edits_apply_while_running(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.chat(ALICE, "Alice", "!players")
            await wait_for(lambda: any("moderators and up" in t for t in tells_to(fake, ALICE)))
            path = tmp_path / "commands.toml"
            path.write_text(staff(f'[everyone]\ncommands = ["players"]\n[off]\ncommands = ["kick"]'), encoding="utf-8")
            os.utime(path, (time.time() + 5, time.time() + 5))
            sup.vergil.check_files()
            await fake.chat(ALICE, "Alice", "!players")
            await wait_for(lambda: any("#4 Bravo" in t for t in tells_to(fake, ALICE)))
            await fake.chat(OWNER, "Owner", "!kick Bravo")
            await wait_for(lambda: any("Unknown command !kick" in t for t in tells_to(fake, OWNER)))

            path.write_text("[everyone\n", encoding="utf-8")  # a mistake: what was in force stays
            os.utime(path, (time.time() + 10, time.time() + 10))
            sup.vergil.check_files()
            assert "kick" in sup.settings.permissions.off

    run(scenario())


def test_a_stop_request_from_outside_stops_everything(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            write_server_folder(tmp_path / "server", fake.port)
            settings = load(write_settings(tmp_path, tmp_path / "server", _marker_backend(tmp_path), commands=staff()))
            supervisor = Huragok(settings)
            task = asyncio.ensure_future(supervisor.run())
            await wait_for(lambda: supervisor.superintendent.states[SERVER].connected)
            await wait_for(lambda: supervisor.superintendent.fleet.managers[SERVER].phase == "running")
            request = mailbox.post(tmp_path / "records", "stop_all", immediate=True)
            await asyncio.wait_for(task, 15)  # the fleet reads it within a second; no stop command: nothing to wait for
            reply = mailbox.take_reply(tmp_path / "records", request)
            assert reply is not None and "can't stop" in reply.message  # this backend has only `restart`
            assert supervisor.stopping and supervisor.stop_servers is False

    run(scenario())


def test_requests_from_outside_are_answered(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await fake.join(1, "Alice", ALICE)
            await wait_for(lambda: sup.superintendent.states[SERVER].players)
            await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "external")
            request = mailbox.post(tmp_path / "records", "say", SERVER, text="hello there")
            reply = await wait_for(lambda: mailbox.take_reply(tmp_path / "records", request))
            assert reply.ok and "hello there" in says(fake)
            request = mailbox.post(tmp_path / "records", "rcon", SERVER, text="players")
            reply = await wait_for(lambda: mailbox.take_reply(tmp_path / "records", request))
            assert reply.ok and "1 players" in reply.text
            request = mailbox.post(tmp_path / "records", "restart", "Nope")
            reply = await wait_for(lambda: mailbox.take_reply(tmp_path / "records", request))
            assert not reply.ok and "no server called" in reply.message
            live = json.loads((tmp_path / "records" / "live.json").read_text(encoding="utf-8"))
            assert live["servers"][0]["name"] == SERVER and live["servers"][0]["players"].startswith("1")

    run(scenario())


def test_close_from_outside_leaves_the_servers_running(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            write_server_folder(tmp_path / "server", fake.port)
            supervisor = Huragok(load(write_settings(tmp_path, tmp_path / "server", commands=staff())))
            task = asyncio.ensure_future(supervisor.run())
            await wait_for(lambda: supervisor.superintendent.states[SERVER].connected)
            request = mailbox.post(tmp_path / "records", "close")
            await asyncio.wait_for(task, 15)
            reply = mailbox.take_reply(tmp_path / "records", request)
            assert reply is not None and reply.ok and "keep running" in reply.message
            assert supervisor.stop_servers is False and fake.signed_in() == 0  # our connection closed, nothing stopped

    run(scenario())


def test_a_server_folder_added_while_running_is_picked_up(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake, FakeRcon() as second:
            root = tmp_path / "reclaimer"
            write_server_folder(root / "SERVERS" / SERVER, fake.port)
            settings = load(write_settings(root, None, commands=staff()))
            supervisor = Huragok(settings)
            task = asyncio.ensure_future(supervisor.run())
            try:
                await wait_for(lambda: supervisor.superintendent.states[SERVER].connected)
                write_server_folder(root / "SERVERS" / "Second", second.port, name="Second")
                supervisor.superintendent.discover()
                assert "Second" in supervisor.superintendent.fleet.managers and "Second" in supervisor.superintendent.states
                await wait_for(lambda: supervisor.superintendent.states["Second"].connected)
                assert [r["name"] for r in supervisor.superintendent.fleet.rows()] == [SERVER, "Second"]
            finally:
                supervisor.stop()
                await asyncio.wait_for(task, 10)

    run(scenario())


def test_a_restart_planned_for_when_its_empty_happens_as_the_last_player_leaves(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, _marker_backend(tmp_path)) as (sup, fake, seen):
            await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "running")
            await fake.join(1, "Alice", ALICE)
            await fake.join(2, "Bravo", BRAVO)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 2)
            await sup.superintendent.lifecycle.restart(SERVER, when="when_empty", reason="testing")
            assert sup.superintendent.lifecycle.planned
            await fake.leave(1)
            await through_rcon(fake, seen)
            assert sup.superintendent.lifecycle.planned and not (tmp_path / "restarts.txt").exists()  # Bravo is still on
            await fake.leave(2)
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())
            assert any(e.name == "server.restarting" for e in seen) and not sup.superintendent.lifecycle.planned

    run(scenario())


def test_a_new_version_waits_for_a_restart_in_progress(tmp_path: Path, run, monkeypatch, caplog) -> None:
    """Huragok restarting on its new version straight away lost what the servers were doing: a countdown players
    had been warned of, a planned restart, the jobs before one."""
    from huragok import huragok as core

    monkeypatch.setattr(core, "RELAUNCH_POLL", 0.05)

    async def planned(sup: Huragok, fake: FakeRcon) -> None:
        await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "running")
        await fake.join(1, "Alice", ALICE)
        await wait_for(lambda: sup.superintendent.states[SERVER].players)
        await sup.superintendent.lifecycle.restart(SERVER, when="after_game", reason="testing")
        sup.relaunch_now()
        await asyncio.sleep(0.2)
        assert not sup.stopping and not sup.relaunch

    async def scenario() -> None:
        async with running(tmp_path / "a", _marker_backend(tmp_path)) as (sup, fake, _):
            await planned(sup, fake)
            assert sup.superintendent.lifecycle.cancel(SERVER)  # nothing in progress now
            await wait_for(lambda: sup.stopping)
            assert sup.relaunch and sup.stop_servers is False
        monkeypatch.setattr(core, "RELAUNCH_WAIT", 0.4)
        async with running(tmp_path / "b", _marker_backend(tmp_path)) as (sup, fake, _):
            await planned(sup, fake)
            await wait_for(lambda: sup.stopping)  # not for ever
            assert sup.relaunch

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert f"Huragok's new version is installed; it restarts on it once {SERVER} is done" in caplog.text
    assert f"Huragok restarts on its new version now, after waiting 0 minutes: {SERVER} is still busy" in caplog.text


def test_the_fleet_restarted_after_a_failure_carries_on_without_booting_again(tmp_path: Path, run,
                                                                              monkeypatch) -> None:
    from huragok import huragok as core

    monkeypatch.setattr(core, "RESTART_SECONDS", 0.05)

    async def scenario() -> None:
        async with FakeRcon() as fake:
            write_server_folder(tmp_path / "server", fake.port)
            settings = load(write_settings(tmp_path, tmp_path / "server", _marker_backend(tmp_path),
                                           commands=staff()))
            sup = Huragok(settings)
            boots: list[int] = []
            ticks: list[int] = []
            real_boot, real_tick = sup.superintendent.fleet.boot, sup.superintendent.fleet.tick

            def boot() -> None:
                boots.append(1)
                real_boot()

            def tick(now: float | None = None) -> None:
                ticks.append(1)
                if len(ticks) == 1:
                    raise RuntimeError("one bad tick")
                real_tick(now)

            sup.superintendent.fleet.boot, sup.superintendent.fleet.tick = boot, tick  # type: ignore[method-assign]
            task = asyncio.ensure_future(sup.run())
            try:
                await wait_for(lambda: sup.superintendent.fleet.managers[SERVER].phase == "running", 10)
                assert boots == [1] and len(ticks) > 1
                # A part's handler gets longer than one RCON command ([rcon] command_timeout, 10 s).
                assert sup.runners["commands"].handler_timeout == 15
            finally:
                sup.stop()
                await asyncio.wait_for(task, 10)

    run(scenario())


def test_a_ballots_pick_is_kept_and_shown(tmp_path: Path) -> None:
    """Decision 7: the Superintendent keeps each game players pick, by the playlist its ballot was for; the row both
    windows read says what was picked, and how many of its own rules were applied."""
    from conftest import free_port

    write_server_folder(tmp_path / "server", free_port())
    supervisor = Huragok(load(write_settings(tmp_path, tmp_path / "server", commands=staff())))
    state = supervisor.superintendent.states[SERVER]
    bus = supervisor.bus
    bus.emit("log.ballot_picked", SERVER, picked="Slayer on Guardian")  # no ballot seen opening: not counted
    bus.emit("log.ballot_opened", SERVER, playlist="Team Slayer")
    bus.emit("log.ballot_picked", SERVER, picked="Oddball on The Pit", kind="Custom")
    bus.emit("log.game_overrides", SERVER, count=4, mode="Oddball")
    assert (state.ballot_playlist, state.picked, state.picked_overrides) == ("Team Slayer", "Oddball on The Pit", 4)
    assert [p[:2] for p in supervisor.storage.picks("Team Slayer")] == [("Oddball on The Pit", 1)]
    row = supervisor.superintendent.fleet.rows()[0]
    assert row["picked"] == "Oddball on The Pit" and row["overrides"] == 4
