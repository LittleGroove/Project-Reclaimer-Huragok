"""The watchdog's cases from the inputs it really gets: log events, the console's events, the server's state."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import free_port, wait_for, write_server_folder, write_settings
from test_manager import SERVER as MANAGED, cycle, world  # noqa: F401 - world is a fixture

from huragok.config import load
from huragok.events import Event, EventBus
from huragok.files import FileWatcher
from huragok.manager import Lifecycle as ManagerLifecycle
from huragok.parts.watchdog import JOIN_GRACE, Watchdog
from huragok.runner import PartRunner
from huragok.state import Player, ServerState

SERVER = "Alpha"
NOW = 1_800_000_000.0


def pid(n: int) -> str:
    return f"{n:064x}"


class Recorder:
    """The lifecycle as the watchdog uses it, recording what it asks for."""

    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.planned: dict[str, Any] = {}
        self.restarts: list[tuple[str, str, float | None, str]] = []

    async def restart(self, server: str, *, when: str, countdown: float | None, reason: str, by: str) -> Any:
        self.restarts.append((server, when, countdown, reason))
        self.planned[server] = SimpleNamespace(when=when)
        return self.planned[server]


def dog(state: ServerState, lifecycle: Any = None, **config: Any) -> Watchdog:
    ctx = SimpleNamespace(name="watchdog", servers={state.name: state}, server=lambda name: state,
                          lifecycle=lifecycle or Recorder(), log=logging.getLogger("part.watchdog"),
                          emit=lambda *args, **data: None, config={**Watchdog.defaults, **config})
    watchdog = Watchdog(ctx)  # type: ignore[arg-type]
    watchdog.check_config()
    asyncio.run(watchdog.setup())
    return watchdog


def feed(watchdog: Watchdog, *events: Event) -> None:
    async def go() -> None:
        for event in events:
            handler = {"log.connected": watchdog.connected, "log.joined": watchdog.joined,
                       "rcon.join": watchdog.joined, "rcon.leave": watchdog.left}[event.name]
            await handler(event)

    asyncio.run(go())


def connection(n: int, at: float) -> Event:
    return Event("log.connected", SERVER, {"name": f"P{n}", "id": pid(n)}, at)


def admitted(n: int, at: float) -> Event:
    return Event("rcon.join", SERVER, {"number": n, "name": f"P{n}", "id": pid(n), "ip": "203.0.113.9"}, at)


def left(n: int, at: float) -> Event:
    return Event("rcon.leave", SERVER, {"number": n, "name": f"P{n}", "id": pid(n)}, at)


def on_server(state: ServerState, *numbers: int) -> None:
    state.players = {n: Player(n, f"P{n}", pid(n)) for n in numbers}


# no_joins and the console's join event (H1) ----------------------------------------------------------------------

def test_a_dead_server_lets_players_in_and_drops_them_so_nobody_joins() -> None:
    # As on the live log's dead stretches: "X connected from …", the console's join, "X left the server." ~18 s later.
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300, 250), 1):
        feed(watchdog, connection(n, NOW - ago), admitted(n, NOW - ago), left(n, NOW - ago + 18))
    assert "no_joins" in watchdog.findings(SERVER, NOW)


def test_players_the_console_let_in_who_stay_have_joined() -> None:
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connection(n, NOW - ago), admitted(n, NOW - ago))
    on_server(state, 1, 2, 3)
    assert "no_joins" not in watchdog.findings(SERVER, NOW)
    # One who left after more than the grace still got in.
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connection(n, NOW - ago), admitted(n, NOW - ago))
    feed(watchdog, left(1, NOW - 400 + JOIN_GRACE + 30), left(2, NOW - 340), left(3, NOW - 290))
    assert "no_joins" not in watchdog.findings(SERVER, NOW)


def test_a_late_join_counts_from_the_players_own_connection() -> None:
    # If the console's join came only once the map had loaded, a player let in 60 s after connecting who is still
    # on has joined, though their join is younger than the grace.
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((170, 160, 150), 1):
        feed(watchdog, connection(n, NOW - ago), admitted(n, NOW - ago + 60))
    on_server(state, 1, 2, 3)
    assert "no_joins" not in watchdog.findings(SERVER, NOW)


def test_a_leave_the_console_missed_or_a_second_admission_ends_the_first() -> None:
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connection(n, NOW - ago), admitted(n, NOW - ago))
    on_server(state)  # the refresh's player list: none of them is on, though no leave came
    assert "no_joins" in watchdog.findings(SERVER, NOW)
    # The same player let in again (reconnecting to a dead server) with no leave between: the first stay ended
    # then, even with the console gone since (a player not seen leaving counts as on while it's away).
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    feed(watchdog, connection(1, NOW - 400), admitted(1, NOW - 400), connection(1, NOW - 390), admitted(1, NOW - 390),
         left(1, NOW - 372), connection(2, NOW - 350), admitted(2, NOW - 350), left(2, NOW - 332),
         connection(3, NOW - 300))
    state.connected = False
    assert "no_joins" in watchdog.findings(SERVER, NOW)


def test_the_logs_roster_line_is_a_join_at_once() -> None:
    state = ServerState(SERVER, connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connection(n, NOW - ago))
    assert "no_joins" in watchdog.findings(SERVER, NOW)
    feed(watchdog, Event("log.joined", SERVER, {"name": "P1"}, NOW - 5))
    assert "no_joins" not in watchdog.findings(SERVER, NOW)


# Judging only on what the console says now (H4) ------------------------------------------------------------------

def test_stuck_after_a_game_from_the_servers_state() -> None:
    state = ServerState(SERVER, connected=True, phase="finished", phase_since=NOW - 4 * 60, max_players=16)
    on_server(state, *range(1, 17))
    watchdog = dog(state)
    assert watchdog.findings(SERVER, NOW)["stuck"] == "stuck after a game for 4 minutes"
    assert "stuck" not in watchdog.findings(SERVER, NOW - 2 * 60)  # 2 minutes in: under stuck_minutes
    state.players = {}
    assert "stuck" not in watchdog.findings(SERVER, NOW)  # nobody waiting
    # The console dropped while the scoreboard was up: that's the last thing it saw, not what's on now.
    on_server(state, *range(1, 17))
    state.connected = False
    assert "stuck" not in watchdog.findings(SERVER, NOW)


def test_a_full_roster_frozen_with_the_console_away_doesnt_explain_no_joins() -> None:
    state = ServerState(SERVER, connected=True, phase="playing", max_players=16)
    on_server(state, *range(1, 17))
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 20):
        feed(watchdog, connection(n, NOW - ago))
    assert "no_joins" not in watchdog.findings(SERVER, NOW)  # full: turning players away is right
    state.connected = False
    assert "no_joins" in watchdog.findings(SERVER, NOW)


# The minute timer, and how it acts ---------------------------------------------------------------------------------

def test_the_minute_timer_measures_memory_and_acts(run, monkeypatch) -> None:
    monkeypatch.setitem(Watchdog.evaluate.__dict__, "_part_every", 0.02)
    monkeypatch.setattr(Watchdog, "_memory_mb", staticmethod(lambda process: 4096.0 if process == 4242 else None))
    state = ServerState(SERVER, connected=True, pid=4242)
    lifecycle = Recorder()
    watchdog = dog(state, lifecycle, memory_mb=1000)

    async def scenario() -> None:
        runner = PartRunner(watchdog, EventBus())
        task = asyncio.ensure_future(runner.run())
        try:
            await wait_for(lambda: lifecycle.restarts)
        finally:
            await runner.stop()
            task.cancel()

    run(scenario())
    assert watchdog.memory[SERVER] == 4096.0 and watchdog.level[SERVER] == "degraded"
    assert lifecycle.restarts[0] == (SERVER, "after_game", None, "the server is using too much memory")


def test_it_doesnt_restart_twice_and_only_actnow_replaces_a_plan(run) -> None:
    state = ServerState(SERVER, connected=True)
    lifecycle = Recorder()
    watchdog = dog(state, lifecycle)

    async def scenario() -> None:
        lifecycle.busy.add(SERVER)
        await watchdog._act(SERVER, {"dead": "dropped 28 of 28 players at once"})
        assert lifecycle.restarts == []  # already stopping or restarting
        lifecycle.busy.clear()
        lifecycle.planned[SERVER] = SimpleNamespace(when="after_game")
        await watchdog._act(SERVER, {"memory": "using 4096 MB, over 1000 MB"})
        assert lifecycle.restarts == []  # after the game is already planned
        await watchdog._act(SERVER, {"no_joins": "3 players connected in 5 minutes and none got in"})
        assert lifecycle.restarts == [(SERVER, "now", 30.0, "nobody can join")]  # actnow replaces it
        await watchdog._act(SERVER, {"dead": "dropped 28 of 28 players at once"})
        assert len(lifecycle.restarts) == 1  # now is already planned

    run(scenario())


def test_a_death_seen_during_a_countdown_that_was_called_off_still_restarts_it(run) -> None:
    state = ServerState(SERVER, connected=True)
    lifecycle = Recorder()
    watchdog = dog(state, lifecycle)

    async def scenario() -> None:
        lifecycle.busy.add(SERVER)  # a daily-restart countdown under way
        await watchdog.roster_lost(Event("log.roster_lost", SERVER, {"lost": 28, "of": 28, "present": 0}))
        assert lifecycle.restarts == [] and "dead" in watchdog.found[SERVER]
        await watchdog.evaluate()  # still counting down: still nothing
        assert lifecycle.restarts == []
        lifecycle.busy.clear()  # K: the countdown is called off, and the server is still dead
        await watchdog.evaluate()
        assert lifecycle.restarts and lifecycle.restarts[0][:2] == (SERVER, "now")
        await watchdog.evaluate()
        assert len(lifecycle.restarts) == 1  # acted on once, not every minute

    run(scenario())


def test_four_dead_servers_in_an_hour_fault_it_with_three_restarts(world, run) -> None:  # noqa: F811
    clock, settings, backend, fleet, manager, client, state, seen = world  # restarts_per_hour = 3
    watchdog = dog(state, ManagerLifecycle(fleet))

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running"
        for _ in range(4):
            await watchdog.roster_lost(Event("log.roster_lost", MANAGED, {"lost": 28, "of": 28, "present": 0}))
            for _ in range(5):
                await cycle(manager, clock, 1)
                if manager.phase in ("running", "faulted"):
                    break
            await watchdog.fresh_start(Event("server.restarted", MANAGED))
        assert manager.phase == "faulted" and "over the budget of 3" in manager.record.fault_reason

    run(scenario())
    assert backend.starts == [MANAGED] * 4  # the first start and three restarts, not a fourth


# From the server's log file to a restart -------------------------------------------------------------------------

def test_a_roster_loss_in_the_log_file_reaches_the_watchdog(tmp_path: Path, run) -> None:
    data = write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    server = settings.servers[0]
    bus = EventBus()
    state = ServerState(server.name, connected=True)
    lifecycle = Recorder()
    watchdog = dog(state, lifecycle)
    watcher = FileWatcher(server, state, bus)
    log_file = data / "server.log"
    log_file.write_text("04:12:00 Online: players join at 203.0.113.7:49176.\n", encoding="utf-8")

    async def scenario() -> None:
        runner = PartRunner(watchdog, bus)
        task = asyncio.ensure_future(runner.run())
        try:
            await wait_for(lambda: runner.enabled and runner._unsubscribe)
            watcher.poll_log()  # the first look: it was there already, so it starts at the end
            with log_file.open("a", encoding="utf-8") as file:
                file.write("04:12:09 Host roster lost 28 of 28 players in one update (0 now present): {}\n")
            watcher.poll_log()
            await wait_for(lambda: lifecycle.restarts)
        finally:
            await runner.stop()
            task.cancel()

    run(scenario())
    assert lifecycle.restarts == [(server.name, "now", 0, "the server dropped its players")]


def test_a_say_that_reads_like_a_roster_loss_restarts_nothing(tmp_path: Path, run) -> None:
    data = write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("log.*", lambda event: seen.append(event.name))
    watcher = FileWatcher(settings.servers[0], ServerState(settings.servers[0].name), bus)
    (data / "server.log").write_text("", encoding="utf-8")
    watcher.poll_log()
    forged = "Host roster lost 99 of 99 players in one update (0 now present): gg"
    with (data / "server.log").open("a", encoding="utf-8") as file:
        file.write(f"20:01:58 RCON 127.0.0.1:60106 as Alice: say {forged}\n")
        file.write(f"20:01:59 RCON 127.0.0.1:60106: say {forged}\n")
        file.write("20:02:00 Refused Bravo from 203.0.113.4 (player ID " + "d" * 64 + "): You are banned from this "
                   f"server. Reason: {forged}\n")
    watcher.poll_log()
    assert seen == ["log.rcon_command", "log.rcon_command"]


def test_a_player_connecting_reaches_the_bus_and_the_lines_after_them_too(tmp_path: Path) -> None:
    # The live 0.4.3 host: the line's `name` (the player's) clashed with emit()'s own, so every connect killed the
    # file watcher and lost the lines read with it.
    data = write_server_folder(tmp_path / "server", free_port())
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    bus = EventBus()
    seen: list[tuple[str, str | None, Any]] = []
    bus.subscribe("log.*", lambda event: seen.append((event.name, event.server, event.get("name"))))
    server = settings.servers[0]
    watcher = FileWatcher(server, ServerState(server.name), bus)
    (data / "server.log").write_text("", encoding="utf-8")
    watcher.poll_log()
    with (data / "server.log").open("a", encoding="utf-8") as file:
        file.write(f"17:12:11 Bravo connected from 203.0.113.4 (player ID {'d' * 64}, ping 31 ms).\n")
        file.write("17:12:30 Community roster: Bravo joined the server\n")
        file.write("17:12:31 Host roster lost 1 of 1 player in one update (0 now present)\n")
    watcher.poll_log()
    assert seen == [("log.connected", server.name, "Bravo"), ("log.joined", server.name, "Bravo"),
                    ("log.roster_lost", server.name, None)]
