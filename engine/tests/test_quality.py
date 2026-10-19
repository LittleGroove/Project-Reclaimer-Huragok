"""Commands you can watch and cancel, log lines that keep their shape, and the server's Workshop lines."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from conftest import free_port, wait_for
from test_setup import fake_tool, make_server_folder

from huragok.core import one_line
from huragok.files import CONSOLE_PATTERNS, parse_console_line
from huragok.tui import RecentLog


def test_streaming_a_command_and_cancelling_it(tmp_path: Path, run) -> None:
    folder = tmp_path / "Slayer"
    make_server_folder(folder, "Slayer", free_port())
    tool = fake_tool()
    seen: list[str] = []

    async def scenario() -> None:
        result = await tool.stream(folder, "content", "guardian", on_line=seen.append, timeout=60)
        assert result.ok and any("Guardian" in line for line in seen) and "Guardian" in result.text
        seen.clear()
        cancel = asyncio.Event()
        task = asyncio.ensure_future(tool.stream(folder, "slow", "40", on_line=seen.append, cancel=cancel, timeout=60))
        await wait_for(lambda: len(seen) >= 2 or task.done(), 30)
        assert not task.done(), task.result()
        cancel.set()
        result = await asyncio.wait_for(task, 15)
        assert not result.ok and result.code == 130 and "(cancelled)" in result.text and len(seen) < 10
        result = await tool.stream(folder, "slow", "40", on_line=seen.append, timeout=1.2)
        assert result.code == 124 and "took longer than" in result.text

    run(scenario())


def test_log_lines_keep_their_time_and_level() -> None:
    recent = RecentLog()
    logger = logging.getLogger("test.quality")
    logger.addHandler(recent)
    logger.setLevel(logging.INFO)
    echoed: list[tuple[int, str]] = []
    recent.echo = lambda level, text: echoed.append((level, text))
    try:
        logger.warning("[Slayer] settings reloaded: Configuration reloaded.\nRestart `dedicated` to apply [workshop].\n")
    finally:
        logger.removeHandler(recent)
    lines = recent.recent(5)
    assert len(lines) == 2 and lines[0][1].endswith("settings reloaded: Configuration reloaded.")
    assert lines[1][1].strip().startswith("↳ Restart `dedicated`") and lines[1][0] == logging.WARNING
    assert echoed == lines
    assert one_line("Configuration reloaded.\n  Restart `dedicated` to apply [workshop] update checks.\n") == \
        "Configuration reloaded. | Restart `dedicated` to apply [workshop] update checks."


def test_lines_are_coloured_by_what_they_say_everywhere(tmp_path: Path, monkeypatch) -> None:
    """The dashboard's events, the held lines in a screen and `watch` share one set of rules."""
    import io
    import sys

    from huragok.menu import render
    from huragok.tui import content_colour, event_colour
    from huragok.watch import follow, parts_for

    assert content_colour("[Alpha] started `dedicated` (process 5)") == "green"
    assert content_colour("[Alpha] + Alice joined (#2)") == "cyan"
    assert content_colour("[Alpha] restart requested: daily restart") == "magenta"
    assert content_colour("Error: 2 of 5 games cannot be played") == "red"
    assert content_colour("[Alpha] Skipped map package x: no .mapinfo") == "yellow"
    assert content_colour("[Alpha] healthy · 0 players · RCON 1 ms") is None
    assert event_colour(logging.WARNING, "all fine") == "yellow" and event_colour(logging.INFO, "plain") == "gray"
    view = {"version": "0.2.0", "supervising": True, "when": "", "home": "", "header": [], "problems": [], "rows": [],
            "startup": None, "log_name": "x", "events": [(20, "10:00:00 INFO    [Alpha] started `dedicated` (process 5)"),
                                                          (30, "10:00:01 WARNING careful"),
                                                          (20, "10:00:02 INFO    [Alpha] + Bob joined (#1)")]}
    colours = dict(render(view, 100, 30))
    assert colours[next(t for t in colours if "started `dedicated`" in t)] == "green"
    assert colours[next(t for t in colours if "careful" in t)] == "yellow"
    assert colours[next(t for t in colours if "Bob joined" in t)] == "cyan"
    # watch: a huragok.log line keeps a gray time and level; a session marker is white; appended lines follow
    parts = parts_for("2026-10-16 06:02:29,655 INFO    rcon: [Alpha] RCON connected (server 0.9.18)")
    assert parts[0] == ("2026-10-16 06:02:29 ", "gray") and parts[1][1] == "gray" and parts[-1][1] == "green"
    assert parts_for("──── 2026-10-16 12:04:59 UTC · started by the Superintendent · x ────")[0][1] == "white"
    assert parts_for("12:04:40 [Alpha] restarting for the new version")[1][1] == "magenta"
    path = tmp_path / "console.log"
    path.write_text("first line\n", encoding="utf-8")
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    looks = {"n": 0}

    def stop() -> bool:
        looks["n"] += 1
        if looks["n"] == 2:
            with path.open("a", encoding="utf-8") as file:
                file.write("[Alpha] Bob joined (#3)\n")
        return looks["n"] > 4

    follow(path, poll=0.02, stop=stop)
    text = buffer.getvalue()
    assert "Following console.log" in text and "first line" in text and "Bob joined" in text
    assert text.index("first line") < text.index("Bob joined")


def test_the_servers_workshop_lines_become_events() -> None:
    names = {name for name, _ in CONSOLE_PATTERNS}
    assert {"server.workshop", "server.map_skipped"} <= names
    event = parse_console_line("Workshop 2984061723: acquiring from Steam")
    assert event is not None and event[0] == "server.workshop" and event[1]["item"] == 2984061723  # digits become numbers
    assert event[1]["text"] == "acquiring from Steam"
    event = parse_console_line("Error: Workshop 2984061723: MCC's Steam runtime was not found. On a headless server "
                               "configure [workshop] steamcmd and username after signing into SteamCMD once.")
    assert event is not None and event[0] == "server.workshop" and event[1]["text"].startswith("MCC's Steam runtime")
    event = parse_console_line(r"Skipped map package H:\x\content\Mods\Workshop\3820687243\maps\fixed.map: "
                               "Map needs matching .mapinfo metadata")
    assert event is not None and event[0] == "server.map_skipped" and event[1]["reason"] == "Map needs matching .mapinfo metadata"
    assert parse_console_line("11:08:37 Checking the configuration (its files changed)…") is None
