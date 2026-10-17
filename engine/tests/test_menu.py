"""The dashboard and its screens, without a terminal: a fake keyboard, a fake controller, output captured."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from reclaimer_supervisor.manager import Result
from reclaimer_supervisor.menu import Menu, render
from reclaimer_supervisor.tui import Keyboard, RecentLog, Terminal, without_quick_edit


def row(name: str, index: int = 1, **over: Any) -> dict[str, Any]:
    base = {"index": index, "name": name, "slug": name.lower(), "port": 49176, "autostart": True, "phase": "running",
            "status": "RUNNING  up 2h 14m", "color": "green", "players": "3/16", "names": "Alice, Bob, Cy",
            "game": "playing Guardian Slayer", "detail": "daily restart 04:00 (in 3h)", "version": "0.9.18",
            "uptime": "2h 14m", "health": "healthy", "connected": True, "errors": [],
            "can": {"start": False, "stop": True, "restart": True, "cancel": False, "talk": True}}
    base.update(over)
    return base


def test_render_fits_the_width_and_shows_every_server() -> None:
    view = {"version": "0.2.0", "supervising": True, "when": "Fri 16 Oct 2026  00:15:17", "home": "H:/reclaimer",
            "header": ["backend process", "servers in SERVERS/", "running: commands, automod (dry run)"],
            "problems": ["Beta: project-reclaimer.exe is not in H:/reclaimer/SERVERS/Beta"],
            "rows": [row("Alpha Server"), row("Beta Server", 2, phase="idle", status="stopped", color="gray",
                                             players="", names="", game="", detail="", autostart=False)],
            "startup": False, "events": [(20, "00:15:01 INFO    hello"), (30, "00:15:02 WARNING careful")],
            "log_name": "logs/supervisor.log"}
    lines = render(view, 100, 30)
    text = "\n".join(t for t, _ in lines)
    assert "RECLAIMER SUPERVISOR 0.2.0   (supervising)" in text
    assert "Beta: project-reclaimer.exe is not in" in text
    assert "Alpha Server" in text and "Beta Server" in text and "3/16 Alice, Bo..." in text  # cut to its column
    assert "daily restart 04:00" in text and "stopped" in text
    assert "[S] Start" in text and "Start at boot: OFF" in text and "careful" in text
    header = next(t for t, _ in lines if t.lstrip().startswith("#"))
    assert header.split() == ["#", "SERVER", "PORT", "AUTO", "STATUS", "PLAYERS", "GAME"]
    colours = {t: c for t, c in lines}
    assert colours[next(t for t in colours if "Alpha Server" in t)] == "green"
    assert colours[next(t for t in colours if "careful" in t)] == "yellow"
    short = render(view, 100, 14)  # a short window: fewer events (the terminal clips the rest)
    assert len([t for t, _ in short if "INFO" in t or "WARNING" in t]) <= 4 and len(short) < len(lines) + 1

    empty = render({**view, "rows": [], "supervising": False}, 80, 30)
    text = "\n".join(t for t, _ in empty)
    assert "no servers yet" in text and "WINDOW ONLY" in text


class FakeKeyboard(Keyboard):
    def __init__(self, keys: list[str]) -> None:
        self.queue = asyncio.Queue()  # type: ignore[assignment]
        for key in keys:
            self.queue.put_nowait(key)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    async def read(self, timeout: float | None = None) -> str | None:
        if timeout is not None and timeout < 1:  # a screen polling for Esc while a command runs: nobody presses it
            await asyncio.sleep(min(timeout, 0.05))
            return None
        await asyncio.sleep(0)
        if self.queue.empty():
            return None
        return self.queue.get_nowait()


class FakeTerminal(Terminal):
    def __init__(self) -> None:
        self.output: list[str] = []
        self.frames: list[list[tuple[str, str | None]]] = []

    def size(self) -> tuple[int, int]:
        return 100, 30

    def _write(self, text: str) -> None:
        self.output.append(text)

    def draw(self, lines: list[tuple[str, str | None]]) -> None:
        self.frames.append(lines)

    def hide_cursor(self) -> None:
        pass

    def show_cursor(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def reset(self) -> None:
        pass


class FakeController:
    supervising = True
    home = Path("H:/reclaimer")
    launcher = "start.cmd"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.quit = False
        self._rows = [row("Alpha Server"), row("Beta Server", 2, phase="idle", status="stopped", color="gray",
                                               players="", names="", game="",
                                               can={"start": True, "stop": False, "restart": True, "cancel": False,
                                                    "talk": False})]

    def header(self) -> list[str]:
        return ["backend process"]

    def rows(self) -> list[dict[str, Any]]:
        return self._rows

    def events(self, count: int) -> list[tuple[int, str]]:
        return []

    def problems(self) -> list[str]:
        return []

    def finished(self) -> str | None:
        return None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        if verb == "rcon":
            return [(server, Result(True, "ok", "3 players\nAlice"))]
        return [(server, Result(True, "done"))]

    def request_quit(self) -> None:
        self.quit = True

    def watch(self, server: str, listener: Any) -> Any:
        return lambda: None


def test_keys_drive_the_screens(run) -> None:
    controller = FakeController()
    # S: start Beta (the only one that can start), 2, Enter; a key ends the "press any key" wait.
    # X: stop Alpha with players on: pick 1, then N (now). M: message all. A: admin console on Alpha, one command.
    # Q then Y: quit.
    keys = ["S", "2", "ENTER", "x",
            "X", "1", "ENTER", "N", "x",
            "M", "A", "ENTER", *"hi all", "ENTER", "x",
            "A", "1", "ENTER", "ENTER", *"players", "ENTER", "ESC",
            "R", "1", "ENTER", "W", *"maintenance", "ENTER", "x",
            "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, FakeKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    outcome = run(menu.run())
    assert outcome == "quit" and controller.quit
    assert controller.calls == [
        ("start", "Beta Server", {}),
        ("stop", "Alpha Server", {"immediate": True}),
        ("say", "Alpha Server", {"text": "hi all"}),
        ("rcon", "Alpha Server", {"text": "players", "by": ""}),
        ("restart", "Alpha Server", {"immediate": False, "reason": "maintenance"}),
    ]
    printed = "".join(terminal.output)
    assert "Alpha Server: done" in printed and "3 players" in printed and "These keep running: Alpha Server" in printed
    assert terminal.frames and "RECLAIMER SUPERVISOR" in terminal.frames[0][0][0]


def test_prompts_cancel_and_time_out(run) -> None:
    controller = FakeController()
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, FakeKeyboard(["ESC"]))  # type: ignore[arg-type]
    assert run(menu.prompt_line("name")) is None
    menu = Menu(controller, terminal, FakeKeyboard([]))  # type: ignore[arg-type]
    assert run(menu.prompt_line("name")) is None  # nothing typed: the (fake) timeout
    menu = Menu(controller, terminal, FakeKeyboard(["a", "b", "BACKSPACE", "c", "ENTER"]))  # type: ignore[arg-type]
    assert run(menu.prompt_line("name")) == "ac"
    menu = Menu(controller, terminal, FakeKeyboard(["ENTER"]))  # type: ignore[arg-type]
    assert run(menu.prompt_yes_no("sure?", True)) is True


def test_key_names() -> None:
    assert Keyboard.name_of("\r") == "ENTER" and Keyboard.name_of("\x1b") == "ESC"
    assert Keyboard.name_of("\x7f") == "BACKSPACE" and Keyboard.name_of("\x03") == "CTRL_C"
    assert Keyboard.name_of("s") == "s" and Keyboard.name_of("\x01") is None


def test_quick_edit_is_switched_off_and_nothing_else() -> None:
    assert without_quick_edit(0x01F7) == 0x01B7  # the classic console's usual input mode, QuickEdit on
    assert without_quick_edit(0x0007) == 0x0087  # extended flags on, or the change doesn't count


def test_a_second_window_shows_log_lines_the_way_the_first_shows_its_events(tmp_path: Path) -> None:
    from reclaimer_supervisor.menu import RemoteController

    log = tmp_path / "supervisor.log"
    log.write_text("2026-10-16 06:45:19,685 INFO    relay: [LG Test Server] console relay listening on 127.0.0.1:50176\n"
                   "2026-10-16 06:45:20,001 WARNING manager: [LG Test Server] trying again in 5 s (failure 1 of 5)\n"
                   "a line that isn't the supervisor's format\n", encoding="utf-8")
    settings = type("S", (), {"home": tmp_path, "records": tmp_path, "log_file": log, "backend": "process"})()
    events = RemoteController(settings, lambda: None).events(10)  # type: ignore[arg-type]
    assert events[0] == (logging.INFO, "06:45:19 INFO    [LG Test Server] console relay listening on 127.0.0.1:50176")
    assert events[1][0] == logging.WARNING and events[1][1].startswith("06:45:20 WARNING [LG Test Server] trying again")
    assert events[2] == (logging.INFO, "a line that isn't the supervisor's format")


def plain(terminal: FakeTerminal) -> str:
    """What was written, without the colour codes."""
    import re

    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", "".join(terminal.output))


class InterruptedKeyboard(FakeKeyboard):
    """Before the first key, a log line arrives while the question waits, and the quiet moment passes."""

    def __init__(self, keys: list[str], logger: logging.Logger) -> None:
        super().__init__(keys)
        self.logger = logger
        self.interrupted = False

    async def read(self, timeout: float | None = None) -> str | None:
        if not self.interrupted:
            self.interrupted = True
            self.logger.info("[Alpha Server] Workshop 2984061723: installed")
            await asyncio.sleep(0.2)
        return await super().read(timeout)


def test_lines_arriving_during_a_question_are_held_then_shown_with_the_question_again(run) -> None:
    recent = RecentLog()
    logger = logging.getLogger("test.menu.held")
    logger.addHandler(recent)
    logger.setLevel(logging.INFO)
    terminal = FakeTerminal()
    menu = Menu(FakeController(), terminal, InterruptedKeyboard(["3", "ENTER"], logger), recent)  # type: ignore[arg-type]
    menu.held.quiet = 0.05

    async def scenario() -> str | None:
        menu.held.attach(asyncio.get_running_loop())
        menu._echo(True)
        try:
            return await menu.prompt_line("  Which?")
        finally:
            menu._echo(False)
            menu.held.detach()
            logger.removeHandler(recent)

    assert run(scenario()) == "3"
    text = plain(terminal)
    first = text.index("Which?: ")
    held = text.index("Workshop 2984061723: installed")
    again = text.index("Which?: ", first + 1)
    assert first < held < again < text.index("3", again)  # the question, the line, the question again, the answer
    assert text.count("Workshop 2984061723") == 1  # shown once, not on every key


def test_a_menu_block_is_laid_out_in_columns_and_drawn_again_after_held_lines(run) -> None:
    recent = RecentLog()
    logger = logging.getLogger("test.menu.block")
    logger.addHandler(recent)
    logger.setLevel(logging.INFO)
    terminal = FakeTerminal()
    menu = Menu(FakeController(), terminal, InterruptedKeyboard(["2"], logger), recent)  # type: ignore[arg-type]
    menu.held.quiet = 0.05
    options = [("1", "Browse: maps, variants, game types, mods"), ("2", "Playlists: list, build, edit, use"),
               ("3", "Workshop items and mods"), ("4", "Check it (dedicated check)")]

    async def scenario() -> str | None:
        menu.held.attach(asyncio.get_running_loop())
        menu._echo(True)
        try:
            return await menu.choose("Which?", options)
        finally:
            menu._echo(False)
            menu.held.detach()
            logger.removeHandler(recent)

    assert run(scenario()) == "2"
    text = plain(terminal)
    lines = text.splitlines()
    first_row = next(line for line in lines if "[1] Browse" in line)
    assert "[2] Playlists" in first_row  # two columns: they fit side by side at 100 columns
    assert [line for line in lines if "[3] Workshop" in line and "[4] Check" in line]
    assert text.count("[1] Browse") == 2 and text.count("Esc back") == 2  # drawn, then drawn again after the log line
    assert text.index("Workshop 2984061723") < text.rindex("[1] Browse")
    narrow = FakeTerminal()
    narrow.size = lambda: (60, 30)  # type: ignore[method-assign]
    menu = Menu(FakeController(), narrow, FakeKeyboard(["4"]), RecentLog())  # type: ignore[arg-type]
    assert run(menu.choose("Which?", options)) == "4"
    assert not [line for line in plain(narrow).splitlines() if "[1] Browse" in line and "[2] Playlists" in line]
