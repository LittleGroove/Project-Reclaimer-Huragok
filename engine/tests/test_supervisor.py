"""The whole supervisor against a fake server: its parts, commands, moderation, restarts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, staff, wait_for, write_server_folder, write_settings
from fake_rcon import FakeRcon

from reclaimer_supervisor.config import load
from reclaimer_supervisor.core import Supervisor
from reclaimer_supervisor.events import Event
from reclaimer_supervisor.part import Part, on
from reclaimer_supervisor.parts import PARTS

SERVER = "LG Test Server"


@asynccontextmanager
async def running(tmp_path: Path, body: str = "", **folder: Any) -> AsyncIterator[tuple[Supervisor, FakeRcon, list[Event]]]:
    async with FakeRcon() as fake:
        write_server_folder(tmp_path / "server", fake.port, **folder)
        settings = load(write_settings(tmp_path, tmp_path / "server", body, commands=staff()))
        supervisor = Supervisor(settings)
        seen: list[Event] = []
        supervisor.bus.subscribe("*", seen.append)
        task = asyncio.ensure_future(supervisor.run())
        try:
            await wait_for(lambda: supervisor.states[SERVER].connected)
            yield supervisor, fake, seen
        finally:
            supervisor.stop()
            await asyncio.wait_for(task, 10)


def tells_to(fake: FakeRcon, player_id: str) -> list[str]:
    return [c["args"][1] for c in fake.named("tell") if c.get("args", [None])[0] == player_id]


async def everyone_joins(fake: FakeRcon) -> None:
    for number, (name, pid) in enumerate([("Owner", OWNER), ("Mod", MOD), ("Alice", ALICE), ("Bravo", BRAVO)], 1):
        await fake.join(number, name, pid)
    await fake.join(5, "Admin", ADMIN, admin=True)


def test_commands_mute_by_a_moderator(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (supervisor, fake, seen):
            await everyone_joins(fake)
            await wait_for(lambda: len(supervisor.states[SERVER].players) == 5)
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
            await wait_for(lambda: len(supervisor.states[SERVER].players) == 6)
            await fake.chat(OWNER, "Owner", "!kick alice")
            await wait_for(lambda: any("could be #3 Alice, #6 Alice" in t for t in tells_to(fake, OWNER)))
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
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
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


def _marker_backend(tmp_path: Path) -> str:
    marker = (tmp_path / "restarts.txt").as_posix()
    python = Path(sys.executable).as_posix()
    command = f'"{python}" -c "open(r\'{marker}\', \'a\').write(\'{{slug}}\\n\')"'
    return f"backend = \"command\"\n[command]\nrestart = '''{command}'''\n"


def test_restart_after_the_game_that_was_played(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, _marker_backend(tmp_path)) as (sup, fake, seen):
            await fake.join(1, "Alice", ALICE)
            await wait_for(lambda: sup.states[SERVER].players)
            await fake.event("phase", phase="playing", map="Valhalla", mode="Slayer")
            await wait_for(lambda: sup.states[SERVER].phase == "playing")
            await sup.lifecycle.restart(SERVER, when="after_game", reason="testing")
            await wait_for(lambda: any("after this game" in c.get("args", [""])[0] for c in fake.named("say")))
            # Seen on 0.9.18: `finished` for the next game before it starts. Not the end of ours.
            await fake.event("phase", phase="finished", map="The Pit", mode="Slayer")
            await asyncio.sleep(0.3)
            assert not (tmp_path / "restarts.txt").exists()
            await fake.event("phase", phase="finished", map="Valhalla", mode="Slayer")
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())
            assert (tmp_path / "restarts.txt").read_text().strip() == "lg-test-server"
            await wait_for(lambda: any(e.name == "server.restarted" for e in seen))

    run(scenario())


def test_watchdog_restarts_after_a_mass_drop(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        body = _marker_backend(tmp_path) + '[watchdog]\nmode = "act"\n'
        async with running(tmp_path, body) as (sup, _, seen):
            sup.bus.emit("log.roster_lost", SERVER, lost=3, of=20, present=17)  # a few players: not a collapse
            await asyncio.sleep(0.3)
            assert not any(e.name == "server.restart_planned" for e in seen)
            sup.bus.emit("log.roster_lost", SERVER, lost=28, of=28, present=0, details="{}")
            planned = await wait_for(lambda: next((e for e in seen if e.name == "server.restart_planned"), None))
            assert planned["when"] == "now" and planned["by"] == "Watchdog" and "dropped" in planned["reason"]
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())

    run(scenario())


def test_keepalive_restarts_a_hung_server(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        body = _marker_backend(tmp_path) + "[keepalive]\nhang_seconds = 1\ncheck_seconds = 0.2\n"
        async with running(tmp_path, body, status_age=600) as (sup, fake, seen):
            await wait_for(lambda: sup.states[SERVER].last_rcon_ok)  # the first refresh is done
            sup.states[SERVER].last_rcon_ok = time.monotonic() - 60  # RCON quiet too
            sup.watchers[0].poll_status()
            hung = await wait_for(lambda: next((e for e in seen if e.name == "server.hung"), None))
            assert hung.server == SERVER
            await wait_for(lambda: (tmp_path / "restarts.txt").exists())

    run(scenario())


def test_attach_mode_cannot_restart(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, _, _):
            assert not sup.lifecycle.can_control
            try:
                await sup.lifecycle.restart(SERVER)
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
            await wait_for(lambda: len(sup.states[SERVER].players) == 5)
            await fake.chat(ALICE, "Alice", "!players")
            await wait_for(lambda: any("moderators and up" in t for t in tells_to(fake, ALICE)))
            path = tmp_path / "commands.toml"
            path.write_text(staff(f'[everyone]\ncommands = ["players"]\n[off]\ncommands = ["kick"]'), encoding="utf-8")
            os.utime(path, (time.time() + 5, time.time() + 5))
            sup.check_files()
            await fake.chat(ALICE, "Alice", "!players")
            await wait_for(lambda: any("#4 Bravo" in t for t in tells_to(fake, ALICE)))
            await fake.chat(OWNER, "Owner", "!kick Bravo")
            await wait_for(lambda: any("Unknown command !kick" in t for t in tells_to(fake, OWNER)))

            path.write_text("[everyone\n", encoding="utf-8")  # a mistake: what was in force stays
            os.utime(path, (time.time() + 10, time.time() + 10))
            sup.check_files()
            assert "kick" in sup.settings.permissions.off

    run(scenario())


def test_a_stop_request_stops_everything(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            write_server_folder(tmp_path / "server", fake.port)
            supervisor = Supervisor(load(write_settings(tmp_path, tmp_path / "server", commands=staff())))
            task = asyncio.ensure_future(supervisor.run())
            await wait_for(lambda: supervisor.states[SERVER].connected)
            (tmp_path / "records").mkdir(exist_ok=True)
            (tmp_path / "records" / "stop-request").write_text("now", encoding="utf-8")
            await asyncio.wait_for(task, 10)  # the file check sees it within a few seconds
            assert supervisor.stop_servers and not (tmp_path / "records" / "stop-request").exists()

    run(scenario())
