"""The dashboard and its screens, without a terminal: a fake keyboard, a fake controller, output captured."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import tempfile
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from huragok.manager import Result
from huragok.menu import Menu, render
from huragok.tui import Keyboard, RecentLog, Terminal, without_quick_edit


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
            "log_name": "logs/huragok.log"}
    lines = render(view, 100, 30)
    text = "\n".join(t for t, _ in lines)
    assert "HURAGOK 0.2.0   (supervising)" in text
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
        self.drained = 0
        for key in keys:
            self.queue.put_nowait(key)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def drain(self) -> None:
        self.drained += 1  # the script is every key the test presses, none pressed "meanwhile": keep them

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

    def _write(self, text: str, frame: bool = False) -> None:
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
    launcher = "start.cmd"

    def __init__(self, home: Path | None = None) -> None:
        # A folder of its own: the window writes logs/window.log there (never into a real install).
        if home is None:
            home = Path(tempfile.mkdtemp(prefix="huragok-window-"))
            weakref.finalize(self, shutil.rmtree, home, True)
        self.home = home
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
    assert terminal.frames and "HURAGOK" in terminal.frames[0][0][0]


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


def test_ctrl_c_asks_before_the_window_closes(run) -> None:
    controller = FakeController()
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, FakeKeyboard(["CTRL_C", "N", "CTRL_C", "Y"]))  # type: ignore[arg-type]
    assert run(menu.run()) == "quit" and controller.quit
    assert "".join(terminal.output).count("Close the window?") == 2  # the first one answered no


def test_one_ctrl_c_press_is_one_key(run) -> None:
    async def presses() -> list[str]:
        keyboard = Keyboard(asyncio.get_running_loop())
        keyboard.ctrl_c()
        keyboard.ctrl_c()  # the launcher passing the same press on
        await asyncio.sleep(0)
        return [keyboard.queue.get_nowait() for _ in range(keyboard.queue.qsize())]

    assert run(presses()) == ["CTRL_C"]


def test_a_frame_that_cannot_be_drawn_keeps_the_window(run) -> None:
    class Breaks(FakeController):
        broken = True

        def rows(self) -> list[dict[str, Any]]:
            if self.broken:
                self.broken = False
                raise ValueError("a bad row")
            return super().rows()

    controller = Breaks()
    terminal = FakeTerminal()
    assert run(Menu(controller, terminal, FakeKeyboard(["Q", "Y"])).run()) == "quit"  # type: ignore[arg-type]
    assert "couldn't be drawn" in terminal.frames[0][0][0]


def test_key_names() -> None:
    assert Keyboard.name_of("\r") == "ENTER" and Keyboard.name_of("\x1b") == "ESC"
    assert Keyboard.name_of("\x7f") == "BACKSPACE" and Keyboard.name_of("\x03") == "CTRL_C"
    assert Keyboard.name_of("s") == "s" and Keyboard.name_of("\x01") is None


def test_quick_edit_is_switched_off_and_nothing_else() -> None:
    assert without_quick_edit(0x01F7) == 0x01B7  # the classic console's usual input mode, QuickEdit on
    assert without_quick_edit(0x0007) == 0x0087  # extended flags on, or the change doesn't count


def test_a_second_window_shows_log_lines_the_way_the_first_shows_its_events(tmp_path: Path) -> None:
    from huragok.menu import RemoteController

    log = tmp_path / "huragok.log"
    log.write_text("2026-10-16 06:45:19,685 INFO    relay: [LG Test Server] console relay listening on 127.0.0.1:50176\n"
                   "2026-10-16 06:45:20,001 WARNING manager: [LG Test Server] trying again in 5 s (failure 1 of 5)\n"
                   "a line that isn't Huragok's format\n", encoding="utf-8")
    settings = type("S", (), {"home": tmp_path, "records": tmp_path, "log_file": log, "backend": "process"})()
    events = RemoteController(settings, lambda: None).events(10)  # type: ignore[arg-type]
    assert events[0] == (logging.INFO, "06:45:19 VRGL INFO    [LG Test Server] console relay listening on 127.0.0.1:50176")
    assert events[1][0] == logging.WARNING and events[1][1].startswith("06:45:20 SUPT WARNING [LG Test Server] trying again")
    assert events[2] == (logging.INFO, "a line that isn't Huragok's format")


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


# The console: never waited on, never fed what a player typed as commands, lined up by columns ------------------

SEQUENCES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # the window's own: colours, cursor moves, clearing


class PausedConsole(io.StringIO):
    """A classic console window with text selected in it: every write waits until the selection ends."""

    def __init__(self) -> None:
        super().__init__()
        self.selection_ends = threading.Event()

    def write(self, text: str) -> int:
        self.selection_ends.wait(10)
        return super().write(text)


def test_a_paused_console_never_holds_up_the_loop(run) -> None:
    console = PausedConsole()
    terminal = Terminal(console)
    terminal.size = lambda: (80, 24)  # type: ignore[method-assign]

    async def supervising() -> int:
        ticks = 0
        for n in range(30):  # the dashboard, drawn again and again while the window takes nothing
            terminal.draw([(f"frame {n}", None)])
            await asyncio.sleep(0.01)
            ticks += 1
        for n in range(5):  # then a screen's lines
            terminal.print(f"line {n}")
            ticks += 1
        return ticks

    started = time.monotonic()
    assert run(supervising()) == 35
    assert time.monotonic() - started < 3  # the loop went on: nothing waited for the console
    assert terminal.writer is not None and terminal.writer.pending() >= 2
    assert not terminal.flush(0.1)  # still held: flush says so rather than wait for ever
    console.selection_ends.set()
    assert terminal.flush(5)
    shown = console.getvalue()
    assert "frame 29" in shown and "frame 15" not in shown  # frames that waited: only the newest is drawn
    lines = [shown.index(f"line {n}") for n in range(5)]
    assert lines == sorted(lines) and shown.index("frame 29") < lines[0]  # the screen's lines, in order, after it


def test_what_a_paused_console_cannot_hold_is_dropped_and_said() -> None:
    console = PausedConsole()
    terminal = Terminal(console)
    assert terminal.writer is not None
    terminal.writer.LIMIT = 200
    for n in range(50):
        terminal.print(f"line {n:02}")
    console.selection_ends.set()
    assert terminal.flush(5)
    shown = console.getvalue()
    assert "were dropped while it took none" in shown and "logs/window.log has what the screens showed" in shown
    assert "line 49" in shown and "line 10" not in shown


def hostile_row() -> dict[str, Any]:
    return row("Evil\x1b]52;c;cm0gLXJmIC8=\x07Server", names="Bob\x1b[2J\x9b31m\r\x00Cy",
               game="playing\tSlayer")


def test_control_characters_from_players_and_servers_never_reach_the_terminal(run) -> None:
    terminal = FakeTerminal()
    terminal.transcript = io.StringIO()
    menu = Menu(FakeController(), terminal, FakeKeyboard([]))  # type: ignore[arg-type]
    chat = "12:00:00 Bob\x1b]0;owned\x07: hi\x1b[31m"
    menu.note(chat)
    menu.server_rows([hostile_row()])
    menu.held.push(f"    {chat}")
    menu.held.flush()
    printed = SEQUENCES.sub("", "".join(terminal.output))
    assert not re.search("[\x00-\x09\x0b-\x1f\x7f-\x9f]", printed)  # nothing but the window's own sequences
    assert "Bob?]0;owned?: hi?[31m" in printed and "Evil?]52;c;cm0gLXJmIC8=?Server" in printed
    assert "Bob?[2J?31m??Cy" in printed and "playing Slayer" in printed  # a tab is a space
    transcript = terminal.transcript.getvalue()
    assert "\x1b" not in transcript and "\x07" not in transcript and "Bob?]0;owned?: hi" in transcript
    # The dashboard too: a name in the table and a chat line among the events.
    frames = Terminal(io.StringIO(), threaded=False)
    frames.size = lambda: (100, 30)  # type: ignore[method-assign]
    view = {"version": "x", "supervising": True, "rows": [hostile_row()], "events": [(20, chat), (20, "two\nlines")]}
    frames.draw(render(view, 100, 30))
    drawn = SEQUENCES.sub("", frames.out.getvalue())
    assert not re.search("[\x00-\x09\x0b-\x1f\x7f-\x9f]", drawn.replace("\n", ""))
    assert "Evil?]52;c" in drawn and "two lines" in drawn


def drawn_lines(terminal: Terminal) -> list[str]:
    return SEQUENCES.sub("", terminal.out.getvalue()).split("\n")[:-1]


def test_wide_names_keep_one_row_per_line_and_the_columns_lined_up() -> None:
    from huragok.tui import cells, cut, format_listing, pad

    assert cells("サーバー東京") == 12 and cells("🎮gamer") == 7 and cells("e\u0301") == 1 and cells("plain") == 5
    assert cut("サーバー東京", 7) == "サーバ…" and cells(cut("サーバー東京", 7)) == 7 and pad("東京", 6) == "東京  "
    terminal = Terminal(io.StringIO(), threaded=False)
    terminal.size = lambda: (100, 30)  # type: ignore[method-assign]
    view = {"version": "x", "supervising": True, "when": "", "home": "H:/r", "header": [], "problems": [],
            "rows": [row("サーバー東京", names="ゆうき, 太郎, 🎮gamer", game="playing 守護者 Slayer " * 3)],
            "events": [(20, "12:00:00 VRGL INFO    [サーバー東京] ゆうき: こんにちは皆さん" * 2)]}
    terminal.draw(render(view, 100, 30))
    lines = drawn_lines(terminal)
    assert lines and all(cells(line) == 100 for line in lines)  # each exactly the window's width: none wraps
    table = next(line for line in lines if "サーバー" in line and "49176" in line)
    header = next(line for line in lines if "SERVER" in line and "PORT" in line)
    assert cells(table[:table.index("49176")]) == cells(header[:header.index("PORT")])  # PORT lines up
    block = format_listing([("サーバー東京", "running", "a"), ("Alpha", "stopped", "b")], 60)
    starts = [cells("".join(text for text, _ in parts[:3])) for parts in block]
    assert starts[0] == starts[1]  # the second column starts at the same place on both rows


def test_a_name_cut_inside_an_emoji_or_a_console_without_the_characters_still_gets_its_frame() -> None:
    for encoding in ("utf-8", "cp1252"):
        stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding)
        terminal = Terminal(stream, threaded=False)
        terminal.size = lambda: (80, 24)  # type: ignore[method-assign]
        terminal.draw([("Bob\ud83d cut inside an emoji · サーバー", None)])
        stream.flush()
        shown = stream.buffer.getvalue().decode(encoding, errors="replace")  # type: ignore[attr-defined]
        assert "Bob? cut inside an emoji" in shown  # the frame was written, not dropped


def test_a_second_window_keeps_the_last_good_live_file(tmp_path: Path) -> None:
    import json

    from huragok.menu import RemoteController

    settings = type("S", (), {"home": tmp_path, "records": tmp_path, "log_file": None, "backend": "process"})()
    remote = RemoteController(settings, lambda: 1234)  # type: ignore[arg-type]
    live = tmp_path / "live.json"

    def again() -> None:
        remote._live_read = 0.0  # as if a second had passed

    assert remote.problems() == ["no word from the supervising window yet (it writes records/live.json every "
                                 "second or two)"]  # not "hasn't updated for 1792375343 s"
    assert remote.rows() == [] and not remote.answering()
    live.write_text(json.dumps({"updated": time.time(), "servers": [row("Alpha"), "junk", {"no": "name"}]}),
                    encoding="utf-8")
    again()
    assert [r["name"] for r in remote.rows()] == ["Alpha"] and remote.problems() == []
    for bad in ("", "{not json", '{"updated": "soon", "servers": []}', "[1, 2]", '{"updated": true}'):
        live.write_text(bad, encoding="utf-8")
        again()
        assert [r["name"] for r in remote.rows()] == ["Alpha"] and remote.problems() == [], bad
    live.unlink()
    live.mkdir()  # can't be read (on Windows a file being replaced can't be opened for a moment either)
    again()
    assert [r["name"] for r in remote.rows()] == ["Alpha"] and remote.answering()
    live.rmdir()
    live.write_text(json.dumps({"updated": time.time() - 100, "servers": [row("Alpha")]}), encoding="utf-8")
    again()
    assert remote.rows() == [] and remote.problems()[0].startswith("the supervising window hasn't updated for 10")


def test_a_grave_is_typed_and_an_arrow_types_nothing() -> None:
    # Windows: msvcrt gave "\xe0" first for an arrow and for "à" alike; key events tell them apart.
    assert Keyboard.windows_char(True, "\xe0") == "à" and Keyboard.name_of("à") == "à"
    assert Keyboard.windows_char(True, "\x00") is None  # an arrow, a function key, Shift alone
    assert Keyboard.windows_char(False, "a") is None  # a key let go
    assert Keyboard.windows_char(True, "\r") == "\r" and Keyboard.windows_char(True, "\x1b") == "\x1b"


class BusyKeyboard(Keyboard):
    """Keys pressed while long work runs land in the queue (and are drained for real); the answers come after."""

    def __init__(self, answers: list[str]) -> None:
        self.queue = asyncio.Queue()  # type: ignore[assignment]
        self.answers = list(answers)

    async def read(self, timeout: float | None = None) -> str | None:
        await asyncio.sleep(0)
        if not self.queue.empty():
            return self.queue.get_nowait()
        return self.answers.pop(0) if self.answers else None


def test_keys_pressed_during_long_work_never_answer_the_next_question(run) -> None:
    keyboard = BusyKeyboard(["Y"])
    terminal = FakeTerminal()
    menu = Menu(FakeController(), terminal, keyboard)  # type: ignore[arg-type]

    async def scenario() -> tuple[str, bool]:
        loop = asyncio.get_running_loop()

        def copy_the_game_files() -> str:
            for key in ("ESC", "Q", "Y"):  # pressed while it ran
                loop.call_soon_threadsafe(keyboard.queue.put_nowait, key)
            time.sleep(0.1)
            return "copied"

        done = await menu.slow(copy_the_game_files)
        return done, await menu.prompt_yes_no("  Start it now?", False)

    assert run(scenario()) == ("copied", True)  # the Esc pressed meanwhile didn't say no
    assert "keys pressed meanwhile are ignored until it's done" in plain(terminal)


def test_a_second_window_waits_as_long_as_the_work_takes(tmp_path: Path, run, monkeypatch) -> None:
    import json

    from conftest import wait_for

    from huragok import mailbox
    from huragok import menu as menu_module
    from huragok.menu import RemoteController, remote_wait

    assert remote_wait("update_program") >= 600 and remote_wait("update") >= 300 and remote_wait("check_workshop") >= 120
    assert remote_wait("check_update") >= 60 and 20 <= remote_wait("rcon") <= 30 and remote_wait("start") >= 10
    settings = type("S", (), {"home": tmp_path, "records": tmp_path, "log_file": None, "backend": "process"})()
    remote = RemoteController(settings, lambda: 1)  # type: ignore[arg-type]
    monkeypatch.setitem(menu_module.REMOTE_WAITS, "check_update", 0.6)
    requests = tmp_path / "requests"

    def waiting() -> list[Path]:
        return [f for f in requests.glob("*.json") if not f.name.endswith(".reply.json")]

    [(_, result)] = run(remote.verb("check_update"))  # nobody picks it up: taken back
    assert not result.ok and "didn't take the request" in result.message and "nothing was done" in result.message
    assert not waiting()

    async def picked_up_then_quiet() -> Any:
        task = asyncio.ensure_future(remote.verb("check_update"))
        for file in await wait_for(waiting):
            file.unlink()  # the supervising window reads (and removes) it, and works on it
        return await task

    [(_, result)] = run(picked_up_then_quiet())
    assert not result.ok and "no answer yet after 1 s: it may still be doing it (the supervising window's events " \
                             "will say)" in result.message

    async def answered() -> Any:
        task = asyncio.ensure_future(remote.verb("rcon", "Alpha", text="players"))
        file = (await wait_for(waiting))[0]
        request = json.loads(file.read_text(encoding="utf-8"))
        file.unlink()
        assert request["options"] == {"text": "players"}
        mailbox.reply(tmp_path, request["id"], True, "ok", "3 players")
        return await task

    assert run(answered()) == [("Alpha", Result(True, "ok", "3 players"))]


def test_start_at_boot_is_switched_off_the_loop(run, monkeypatch) -> None:
    from huragok import desktop

    threads: list[bool] = []
    monkeypatch.setattr(desktop, "startup_enabled", lambda: False)
    monkeypatch.setattr(desktop, "enable_startup",  # on Linux it runs systemctl, which answers when it will
                        lambda home: threads.append(threading.current_thread() is threading.main_thread()) or "on")
    terminal = FakeTerminal()
    run(Menu(FakeController(), terminal, FakeKeyboard(["Y", "x"])).screen_startup())  # type: ignore[arg-type]
    assert threads == [False] and "  on" in "".join(terminal.output)


def test_the_window_writes_its_transcript_into_its_own_folder(run) -> None:
    controller = FakeController()  # T16: it used to be H:/reclaimer, so tests wrote into a real install's log
    assert controller.home != Path("H:/reclaimer") and controller.home.is_dir()
    menu = Menu(controller, FakeTerminal(), FakeKeyboard(["O", "Q", "Y"]))  # type: ignore[arg-type]
    menu.actions["O"] = lambda: asyncio.sleep(0)  # type: ignore[assignment]  # no folder opened on the desktop
    assert run(menu.run()) == "quit"
    assert "key O =====" in (controller.home / "logs" / "window.log").read_text(encoding="utf-8")
