"""The byplayers part (servers/byplayers.py): a server's playlist changed by how many players are on, once the count
has held for the wait, only what its program can play, and only while it's set up, on, running and playing a playlist."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from huragok.core.tomledit import read_toml
from huragok.games import byplayers as bp
from huragok.program.plays import Problem, PlaysCheck
from huragok.servers.byplayers import REFUSED_AGAIN, ByPlayers
from huragok.servers.state import Player, ServerState

DEDICATED = """\
auto_reload = true

[[server]]
name = "Alpha"
port = 46300
playlist = "playlists/slayer.playlist.json"
"""


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeContext:
    """What the part uses of the Superintendent: the servers, their folders and files, the program's check, events."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.config: dict[str, Any] = {}
        self.log = logging.getLogger("part.byplayers")
        self.servers = {"Alpha": ServerState("Alpha", connected=True, last_rcon_ok=1.0)}
        self.events: list[tuple[str, str | None, dict[str, Any]]] = []
        self.checked: list[dict[str, Any]] = []
        self.refuse: set[str] = set()

    started: float | None = None  # the run's start: None, not known (nothing held back for it)

    def server_option(self, server: str, key: str) -> Any:
        return self.folder if key == "folder" and server == "Alpha" else None

    def run_started(self, server: str) -> float | None:
        return self.started

    def config_path(self, server: str) -> Path | None:
        return self.folder / "dedicated.toml" if server == "Alpha" else None

    def check_plays(self, server: str, values: dict[str, Any]) -> PlaysCheck:
        self.checked.append(values)
        if values["playlist"] in self.refuse:
            return PlaysCheck([Problem("Game 2: no map called Nowhere")])
        return PlaysCheck()

    def emit(self, name: str, server: str | None = None, /, **data: Any) -> None:
        self.events.append((name, server, data))

    def on(self, count: int) -> None:
        self.servers["Alpha"].players = {n: Player(n, f"P{n}", f"{n:064x}") for n in range(1, count + 1)}


def made(tmp_path: Path, setup: bp.Setup | None = None, dedicated: str = DEDICATED) -> tuple[ByPlayers, FakeContext, Clock]:
    for name, title in (("slayer", "Slayer"), ("small", "Small Slayer"), ("medium", "Medium"), ("big", "Big Team")):
        (tmp_path / "playlists").mkdir(exist_ok=True)
        (tmp_path / "playlists" / f"{name}.playlist.json").write_text(json.dumps({
            "version": 1, "name": title, "games": [{"map": "Guardian", "game": "Slayer"}] * 3}), encoding="utf-8")
    (tmp_path / "dedicated.toml").write_text(dedicated, encoding="utf-8")
    if setup is not None:
        bp.save(tmp_path, setup)
    ctx = FakeContext(tmp_path)
    part = ByPlayers(ctx)  # type: ignore[arg-type]
    part.check_config()
    clock = Clock()
    part.clock = clock
    return part, ctx, clock


def three(on: bool = True, wait: int = 30) -> bp.Setup:
    return bp.Setup("Alpha", on, "playlists/slayer.playlist.json", wait, [
        bp.Step("playlists/small.playlist.json", 10), bp.Step("playlists/medium.playlist.json", 50),
        bp.Step("playlists/big.playlist.json")])


def playing(tmp_path: Path) -> str:
    return read_toml(tmp_path / "dedicated.toml")["server"][0].get("playlist", "")


async def after(part: ByPlayers, clock: Clock, seconds: float) -> None:
    clock.now += seconds
    await part.tick()
    await part.settled()


def test_the_playlist_changes_once_the_count_has_held_for_the_wait(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three())
        ctx.on(34)
        await after(part, clock, 0)
        await after(part, clock, 29)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []
        await after(part, clock, 1)
        assert playing(tmp_path) == "playlists/medium.playlist.json"
        assert ctx.checked == [{"playlist": "playlists/medium.playlist.json"}]  # its program said it can play it first
        (name, server, data), = ctx.events
        assert (name, server) == ("changed", "Alpha")
        assert data | {"low": 11} == {"players": 34, "playlist": "playlists/medium.playlist.json", "name": "Medium",
                                      "step": 2, "steps": 3, "low": 11, "high": 50, "range": "11 to 50 players",
                                      "was": "playlists/slayer.playlist.json", "was_name": "Slayer"}
        await after(part, clock, 60)
        assert len(ctx.events) == 1  # it plays the right one: nothing more to do
        text = (tmp_path / "dedicated.toml").read_text(encoding="utf-8")
        assert "auto_reload = true" in text and "port = 46300" in text  # only what it plays changed

    run(scenario())


def test_a_count_that_moves_back_before_the_wait_is_up_changes_nothing(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three())
        ctx.on(9)
        await after(part, clock, 0)
        await after(part, clock, 60)
        assert playing(tmp_path) == "playlists/small.playlist.json"
        ctx.on(11)  # one more joins...
        await after(part, clock, 0)
        await after(part, clock, 20)
        ctx.on(10)  # ...and leaves before the wait is up
        await after(part, clock, 0)
        await after(part, clock, 20)
        ctx.on(11)  # back again: the wait starts over
        await after(part, clock, 0)
        await after(part, clock, 29)
        assert playing(tmp_path) == "playlists/small.playlist.json"
        await after(part, clock, 1)
        assert playing(tmp_path) == "playlists/medium.playlist.json"
        assert [e[2]["playlist"] for e in ctx.events] == ["playlists/small.playlist.json", "playlists/medium.playlist.json"]

    run(scenario())


def test_no_wait_changes_it_at_once_and_a_count_past_every_cap_plays_the_last(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        ctx.on(200)
        await after(part, clock, 0)
        assert playing(tmp_path) == "playlists/big.playlist.json"
        assert ctx.events[0][2]["range"] == "51 players and up"

    run(scenario())


def test_a_playlist_its_program_refuses_isnt_written_and_is_tried_again_later(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        ctx.refuse.add("playlists/big.playlist.json")
        ctx.on(60)
        await after(part, clock, 0)
        await after(part, clock, 5)
        assert playing(tmp_path) == "playlists/slayer.playlist.json"
        assert [e[0] for e in ctx.events] == ["refused"] and "no map called Nowhere" in ctx.events[0][2]["why"]
        assert len(ctx.checked) == 1  # not asked again every few seconds
        ctx.refuse.clear()  # put right
        await after(part, clock, REFUSED_AGAIN)
        assert playing(tmp_path) == "playlists/big.playlist.json"
        assert [e[0] for e in ctx.events] == ["refused", "changed"]

    run(scenario())


def test_a_playlist_file_gone_is_refused_without_asking_the_program(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        (tmp_path / "playlists" / "big.playlist.json").unlink()
        ctx.on(60)
        await after(part, clock, 0)
        assert playing(tmp_path) == "playlists/slayer.playlist.json"
        assert ctx.checked == [] and ctx.events[0][0] == "refused" and "isn't there" in ctx.events[0][2]["why"]

    run(scenario())


def test_nothing_changes_while_its_off_not_set_up_or_the_count_isnt_known(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        for setup in (None, three(on=False)):
            part, ctx, clock = made(tmp_path, setup)
            ctx.on(60)
            await after(part, clock, 0)
            await after(part, clock, 600)
            assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []
            (tmp_path / bp.FILE).unlink(missing_ok=True)
        part, ctx, clock = made(tmp_path, three(wait=0))
        ctx.on(60)
        ctx.servers["Alpha"].connected = False  # restarting: its players aren't known
        await after(part, clock, 0)
        ctx.servers["Alpha"] = ServerState("Alpha", connected=True)  # signed in, nothing read yet
        await after(part, clock, 5)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []
        state = ctx.servers["Alpha"]
        state.last_rcon_ok, state.players_known = 2.0, False  # its status is in, its players not yet: 0 isn't 0
        await after(part, clock, 0)
        await after(part, clock, 60)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []
        ctx.on(60)  # its players read
        state.players_known = True
        await after(part, clock, 0)
        assert playing(tmp_path) == "playlists/big.playlist.json"

    run(scenario())


def test_a_server_playing_something_else_or_with_auto_reload_off_is_left_alone(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        one_map = DEDICATED.replace('playlist = "playlists/slayer.playlist.json"', 'map = "Guardian"\ngame = "Slayer"')
        part, ctx, clock = made(tmp_path, three(wait=0), one_map)
        ctx.on(60)
        await after(part, clock, 0)
        assert "map = \"Guardian\"" in (tmp_path / "dedicated.toml").read_text(encoding="utf-8")
        assert ctx.events == []
        part, ctx, clock = made(tmp_path, three(wait=0), DEDICATED.replace("auto_reload = true", "auto_reload = false"))
        ctx.on(60)
        await after(part, clock, 0)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []

    run(scenario())


def test_an_edit_to_the_setup_applies_without_a_restart(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(on=False, wait=0))
        ctx.on(5)
        await after(part, clock, 0)
        assert ctx.events == []
        setup = three(wait=0)
        bp.save(tmp_path, setup)
        import os
        os.utime(tmp_path / bp.FILE, (clock.now + 10, clock.now + 10))  # a new modified time, however quick the test
        await after(part, clock, 5)
        assert playing(tmp_path) == "playlists/small.playlist.json"

    run(scenario())


def test_a_slow_check_runs_apart_from_the_tick_and_one_at_a_time(tmp_path: Path, run) -> None:
    """The program's check ran inside the tick's 15 s handler limit: a slow one was cancelled, the next tick started
    another, and the part was switched off (audit 2026-11, 8.4)."""
    import asyncio
    import threading

    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        gate = threading.Event()
        real = ctx.check_plays

        def slow(server: str, values: dict[str, Any]) -> PlaysCheck:
            gate.wait(10)
            return real(server, values)

        ctx.check_plays = slow  # type: ignore[method-assign]
        ctx.on(60)
        clock.now += 1
        await part.tick()  # returns at once: the check runs on
        await part.tick()
        await part.tick()
        assert "Alpha" in part.changing and ctx.checked == []
        gate.set()
        await part.settled()
        assert len(ctx.checked) == 1 and playing(tmp_path) == "playlists/big.playlist.json"

    run(scenario())


def test_a_write_that_keeps_failing_is_said_once_and_tried_again_later(tmp_path: Path, run, monkeypatch) -> None:
    """A write that failed every time ran the check and logged a warning every 5 s, for good (audit 2026-11, 8.5)."""
    from huragok.servers import byplayers as module

    def refused(*args: Any, **kwargs: Any) -> None:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(module, "write_plays", refused)

    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        ctx.on(60)
        for _ in range(20):
            await after(part, clock, 5)
        assert len(ctx.checked) == 1 and [e[0] for e in ctx.events] == ["refused"]
        assert "couldn't be written" in ctx.events[0][2]["why"]
        await after(part, clock, REFUSED_AGAIN)
        assert len(ctx.checked) == 2

    run(scenario())


def test_turned_off_while_its_check_ran_it_writes_nothing(tmp_path: Path, run) -> None:
    """Turned off in the window (the default put back) during the check, its pick still went over the default
    (audit 2026-11, 8.9)."""
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        real = ctx.check_plays

        def meanwhile(server: str, values: dict[str, Any]) -> PlaysCheck:
            bp.save(tmp_path, three(on=False, wait=0))
            import os
            os.utime(tmp_path / bp.FILE, (clock.now + 99, clock.now + 99))
            return real(server, values)

        ctx.check_plays = meanwhile  # type: ignore[method-assign]
        ctx.on(60)
        await after(part, clock, 1)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []

    run(scenario())


def test_a_playlist_whose_vote_seconds_isnt_a_number_is_refused_not_a_traceback(tmp_path: Path, run, caplog) -> None:
    """`vote_seconds = "30s"` raised out of the tick every 5 s (audit 2026-11, 8.6)."""
    import json
    import logging

    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        (tmp_path / "playlists" / "big.playlist.json").write_text(json.dumps(
            {"name": "Big", "vote_seconds": "30s", "games": [{"map": "Guardian", "game": "Slayer"}] * 3}),
            encoding="utf-8")
        ctx.on(60)
        await after(part, clock, 1)
        assert [e[0] for e in ctx.events] == ["refused"] and "vote_seconds" in ctx.events[0][2]["why"]

    with caplog.at_level(logging.ERROR):
        run(scenario())
    assert "Traceback" not in caplog.text and "couldn't pick" not in caplog.text


def test_turned_off_by_hand_it_puts_the_default_back(tmp_path: Path, run) -> None:
    """Off means the default, however it's turned off: `on = false` written by hand left the last pick playing; only
    the window's O put the default back (audit 2026-11)."""
    import os

    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(wait=0))
        ctx.on(60)
        await after(part, clock, 1)
        assert playing(tmp_path) == "playlists/big.playlist.json"
        bp.save(tmp_path, three(on=False, wait=0))  # by hand
        os.utime(tmp_path / bp.FILE, (clock.now + 50, clock.now + 50))
        await after(part, clock, 5)
        assert playing(tmp_path) == "playlists/slayer.playlist.json"  # the default
        await after(part, clock, 60)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.checked[-1] == {
            "playlist": "playlists/slayer.playlist.json"}

    run(scenario())


# The host's calls, 2 Nov 2026: a margin, the end of the game, the first 10 minutes ----------------------------------


def test_back_down_only_a_margin_below_where_the_bigger_one_starts(tmp_path: Path, run) -> None:
    """With small up to 10 and a margin of 2: up at 11, back down at 9. A count at the line doesn't swap it."""
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three(), DEDICATED.replace("slayer.playlist.json", "medium.playlist.json"))
        assert three().margin == bp.MARGIN == 2
        ctx.on(10)  # inside small's range, but only one below medium's start (11)
        await after(part, clock, 0)
        await after(part, clock, 60)
        assert playing(tmp_path) == "playlists/medium.playlist.json" and ctx.events == []
        ctx.on(9)  # two below: back down
        await after(part, clock, 0)
        await after(part, clock, 30)
        assert playing(tmp_path) == "playlists/small.playlist.json"
        ctx.on(11)  # up: at once (after the wait)
        await after(part, clock, 0)
        await after(part, clock, 30)
        assert playing(tmp_path) == "playlists/medium.playlist.json"

    run(scenario())


def test_during_a_game_the_change_waits_for_its_end_and_the_count_to_hold(tmp_path: Path, run) -> None:
    """Made at the end of the game, and only if the count still wants it then: it has to hold until the next vote."""
    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three())
        state = ctx.servers["Alpha"]
        state.phase = "playing"
        ctx.on(34)
        await after(part, clock, 0)
        await after(part, clock, 300)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.checked == []  # held through the game
        state.phase = "finished"  # the game's over: the vote is next
        await after(part, clock, 5)
        assert playing(tmp_path) == "playlists/medium.playlist.json"
        # Another game: the count goes up for a while, and back before the game ends. Nothing changes.
        state.phase = "playing"
        ctx.on(60)
        await after(part, clock, 0)
        await after(part, clock, 120)
        ctx.on(34)
        await after(part, clock, 5)
        state.phase = "finished"
        await after(part, clock, 5)
        assert playing(tmp_path) == "playlists/medium.playlist.json" and len(ctx.events) == 1

    run(scenario())


def test_nothing_changes_in_the_first_ten_minutes_of_a_run(tmp_path: Path, run) -> None:
    """After a restart the count starts at 0 and climbs as players come back: the smallest playlist went in first."""
    import time

    from huragok.servers.byplayers import AFTER_START_SECONDS

    async def scenario() -> None:
        part, ctx, clock = made(tmp_path, three())
        ctx.started = time.time() - 60  # restarted a minute ago
        ctx.on(0)
        await after(part, clock, 0)
        await after(part, clock, 300)
        assert playing(tmp_path) == "playlists/slayer.playlist.json" and ctx.events == []
        ctx.started = time.time() - AFTER_START_SECONDS - 1
        await after(part, clock, 0)
        await after(part, clock, 30)
        assert playing(tmp_path) == "playlists/small.playlist.json"

    run(scenario())
