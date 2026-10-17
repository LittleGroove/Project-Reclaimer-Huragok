"""The terminal: colours, a frame redrawn in place, single keys as they're pressed, a log
handler that keeps recent lines for the dashboard, and the holding of lines that arrive
while a screen waits for a key. Windows 10+ and Linux, nothing to install.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

Parts = list[tuple[str, str | None]]  # one line in several colours: [(text, colour), ...]

COLORS = {
    "default": "\x1b[0m", "white": "\x1b[97m", "gray": "\x1b[90m", "grey": "\x1b[90m", "red": "\x1b[91m",
    "yellow": "\x1b[93m", "green": "\x1b[92m", "cyan": "\x1b[96m", "magenta": "\x1b[95m",
    "darkyellow": "\x1b[33m", "blue": "\x1b[94m",
}
RESET = "\x1b[0m"


def is_terminal() -> bool:
    """A real window with a keyboard, not a service, a pipe or a scheduled task."""
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except (ValueError, OSError):
        return False


def enable_vt() -> bool:
    """Make the Windows console understand colours and cursor moves (always true elsewhere)."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (OSError, AttributeError):
        return False


QUICK_EDIT = 0x0040  # ENABLE_QUICK_EDIT_MODE
EXTENDED_FLAGS = 0x0080  # ENABLE_EXTENDED_FLAGS: without it, the change to QuickEdit doesn't count


def without_quick_edit(mode: int) -> int:
    return (mode & ~QUICK_EDIT) | EXTENDED_FLAGS


def disable_quick_edit() -> bool:
    """Windows: a click in the classic console window starts a selection, and whatever writes to the window
    waits until Enter or Esc; the supervisor would stop looking after its servers meanwhile. Turns that off for
    this window (Windows Terminal never waits anyway). True when it's off (always elsewhere)."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # no console: a scheduled task, a pipe
        return bool(kernel32.SetConsoleMode(handle, without_quick_edit(mode.value)))
    except (OSError, AttributeError):
        return False


def paint(text: str, color: str | None) -> str:
    code = COLORS.get(color or "", "")
    return f"{code}{text}{RESET}" if code else text


class Terminal:
    """Draws a frame of lines in place, and plain lines when a screen is in progress."""

    transcript: Any = None  # a file that gets a plain copy of what a screen prints (logs/window.log)
    colour: bool = False  # the console understands escape sequences (set when a real one is opened)

    def __init__(self, stream=None) -> None:
        self.out = stream or sys.stdout
        self.colour = enable_vt()
        self._last_lines = 0
        self._last_width = 0
        self.transcript = None

    def note(self, text: str) -> None:
        """A line for the transcript only (what was typed at a prompt, which the screen echoed key by key)."""
        if self.transcript is not None:
            try:
                self.transcript.write(text + "\n")
                self.transcript.flush()
            except (OSError, ValueError):
                pass

    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((100, 30))
        return max(60, size.columns), max(12, size.lines)

    def _write(self, text: str) -> None:
        try:
            self.out.write(text)
            self.out.flush()
        except (OSError, ValueError):
            pass

    def hide_cursor(self) -> None:
        self._write("\x1b[?25l")

    def show_cursor(self) -> None:
        self._write("\x1b[?25h")

    def clear(self) -> None:
        self._write("\x1b[2J\x1b[H")
        self._last_lines = 0

    def reset(self) -> None:
        """The next frame starts from a cleared screen (after a screen printed over it)."""
        self._last_width = 0

    def draw(self, lines: list[tuple[str, str | None]]) -> None:
        width, height = self.size()
        if width != self._last_width:
            self.clear()
            self._last_width = width
        out = ["\x1b[H"]
        shown = 0
        for text, color in lines[: height - 1]:
            text = text.replace("\t", " ")
            if len(text) > width:
                text = text[: width - 1] + "…"
            out.append(paint(text.ljust(width), color) + "\x1b[K\n")
            shown += 1
        out.append("\x1b[J")
        self._last_lines = shown
        self._write("".join(out))

    def print(self, text: str = "", color: str | None = None, end: str = "\n", *, record: bool = True) -> None:
        self.print_parts([(text, color)], end, record=record)

    def print_parts(self, parts: Parts, end: str = "\n", *, record: bool = True) -> None:
        """One line in several colours. record=False keeps it out of the transcript (a prompt drawn again)."""
        plain = "".join(text for text, _ in parts)
        if record and self.transcript is not None and (plain or end == "\n"):
            try:
                self.transcript.write(plain + end)
                self.transcript.flush()
            except (OSError, ValueError):
                pass
        self._write("".join(paint(text, color) for text, color in parts) + end)

    def erase_prompt(self, columns: int) -> None:
        """Wipe the line the cursor is on (and the rows above it that a long prompt wrapped onto)."""
        if not self.colour:
            self._write("\n")
            return
        width, _ = self.size()
        rows = max(1, math.ceil(max(1, columns) / max(1, width)))
        self._write("\r" + (f"\x1b[{rows - 1}A" if rows > 1 else "") + "\x1b[J")


class Keyboard:
    """Keys as they're pressed, from a thread, as names: a character, or ENTER, ESC, BACKSPACE, TAB, CTRL_C."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.loop = loop or asyncio.get_event_loop()
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._saved: object = None

    def start(self) -> None:
        if self._thread is not None:
            return
        if os.name != "nt":
            try:
                import termios
                import tty

                fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except (OSError, ValueError, ImportError):
                self._saved = None
        self._thread = threading.Thread(target=self._read_windows if os.name == "nt" else self._read_posix,
                                        name="keyboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if os.name != "nt" and self._saved is not None:
            try:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            except (OSError, ValueError, ImportError):
                pass
            self._saved = None

    def _push(self, key: str) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, key)

    @staticmethod
    def name_of(char: str) -> str | None:
        if char in ("\r", "\n"):
            return "ENTER"
        if char == "\x1b":
            return "ESC"
        if char in ("\x7f", "\x08"):
            return "BACKSPACE"
        if char == "\x03":
            return "CTRL_C"
        if char == "\t":
            return "TAB"
        if char.isprintable():
            return char
        return None

    def _read_windows(self) -> None:
        import msvcrt

        while not self._stop.is_set():
            try:
                if not msvcrt.kbhit():
                    time.sleep(0.03)
                    continue
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):  # a function or arrow key: two reads, ignored
                    msvcrt.getwch()
                    continue
            except (OSError, ValueError):
                return
            key = self.name_of(char)
            if key:
                self._push(key)

    def _read_posix(self) -> None:
        import select

        fd = sys.stdin.fileno()
        pending = ""
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if not ready:
                    if pending == "\x1b":
                        self._push("ESC")
                        pending = ""
                    continue
                data = os.read(fd, 64).decode("utf-8", errors="ignore")
            except (OSError, ValueError):
                return
            if not data:
                return
            text = pending + data
            pending = ""
            i = 0
            while i < len(text):
                char = text[i]
                if char == "\x1b":
                    if i + 1 >= len(text):
                        pending = "\x1b"  # a lone Escape, or the start of a sequence: decided on the next read
                        break
                    if text[i + 1] in "[O":  # an escape sequence (arrows, function keys): skip it
                        j = i + 2
                        while j < len(text) and not text[j].isalpha() and text[j] != "~":
                            j += 1
                        i = j + 1
                        continue
                    self._push("ESC")
                    i += 1
                    continue
                key = self.name_of(char)
                if key:
                    self._push(key)
                i += 1

    async def read(self, timeout: float | None = None) -> str | None:
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None

    def drain(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()


class RecentLog(logging.Handler):
    """Keeps the last few hundred log lines for the dashboard, and echoes new ones while a screen is open."""

    def __init__(self, keep: int = 400) -> None:
        super().__init__()
        self.lines: deque[tuple[int, str]] = deque(maxlen=keep)
        self.echo: Callable[[int, str], None] | None = None
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            return
        # A message on several lines (a server's reply) becomes one entry per line, the rest indented
        # under the first, so every dashboard line keeps its time and level.
        first, *rest = text.splitlines() or [""]
        entries = [first, *(f"                 ↳ {line.strip()}" for line in rest if line.strip())]
        for entry in entries:
            self.lines.append((record.levelno, entry))
            if self.echo is not None:
                try:
                    self.echo(record.levelno, entry)
                except Exception:  # noqa: BLE001
                    pass

    def recent(self, count: int) -> list[tuple[int, str]]:
        return list(self.lines)[-count:]


def level_colour(level: int) -> str:
    if level >= logging.ERROR:
        return "red"
    if level >= logging.WARNING:
        return "yellow"
    return "gray"


# What a line says decides its colour: the dashboard's events, held lines and `watch` share these rules -------

CONTENT_RULES = [
    (re.compile(r"\bERROR\b|CRITICAL|Traceback|FAULTED|^Error:|WILL NOT COME BACK"), "red"),
    (re.compile(r"crashed|exited on its own|cannot be played|refused it \(error", re.IGNORECASE), "red"),
    (re.compile(r"\bWARNING\b|\bWARN\b|refused|rejected|Skipped|unavailable|cannot find|could not|unreachable|"
                r"not found|failed|PROBLEM|out of range|NOT reloaded|nobody is owner", re.IGNORECASE), "yellow"),
    (re.compile(r"RCON connected|up and answering|started \(process|started `dedicated`|All servers stopped|"
                r"looking after it now|reloaded|reload applied|Online: players join|Workshop \d+: (ready|installed)|"
                r"Everything is ready|RCON listening|now plays", re.IGNORECASE), "green"),
    (re.compile(r"restart|countdown|stopping|shutting down|request|ended the game|closing", re.IGNORECASE), "magenta"),
    (re.compile(r"joined|left the server|\bleft$|chat|kick|\bban|mute|strike|connected from|Workshop \d+:|"
                r"Checking the configuration|RCON \S+ as ", re.IGNORECASE), "cyan"),
]


def content_colour(text: str) -> str | None:
    """The colour a line earns by what it says; None for an ordinary line."""
    for pattern, colour in CONTENT_RULES:
        if pattern.search(text):
            return colour
    return None


def event_colour(level: int, text: str) -> str:
    """A dashboard event's colour: its level when that's a warning or worse, otherwise what it says, else gray."""
    if level >= logging.WARNING:
        return level_colour(level)
    return content_colour(text) or "gray"


# The shared look: the window's screens and the commands print lists and headings the same way ----------

def heading_parts(path: str, width: int, *, indent: str = "  ") -> Parts:
    """A section's start as a rule with the path in it: `  ─── Alpha › Playlists ───────`."""
    return [(f"{indent}─── ", "gray"), (path, "white"),
            (" " + "─" * max(3, width - len(path) - len(indent) - 5), "gray")]


def wrap_cell(text: str, width: int) -> list[str]:
    """Lines of at most `width`, broken at spaces, or inside a long path after a `\\` or `/`."""
    pieces: list[str] = []
    for word in re.findall(r"\S+\s*", text):
        if len(word.rstrip()) <= width:
            pieces.append(word)
            continue
        for part in re.findall(r"[^\\/]*[\\/]|[^\\/]+$", word):  # a path, segment by segment
            while len(part) > width:  # a segment longer than the line: cut it
                pieces.append(part[:width])
                part = part[width:]
            if part:
                pieces.append(part)
    lines: list[str] = []
    line = ""
    for piece in pieces:
        if line and len((line + piece).rstrip()) > width:
            lines.append(line.rstrip())
            line = ""
        line += piece
    if line.rstrip():
        lines.append(line.rstrip())
    return lines


def format_listing(rows: list[tuple[str, ...]], width: int, *, colours: list[str | None] | None = None,
                   index: bool = True, keys: list[str] | None = None, indent: str = "   ", wrap: bool = False,
                   tones: list[str | None] | None = None, widest: int = 40) -> list[Parts]:
    """Rows as aligned columns, numbered `[ 1]` or keyed `[B]`.

    The first column is bright (or the row's colour), the rest gray, unless `tones` names a colour per
    column. The last column is cut to the width, or wrapped onto more lines (wrap=True).
    """
    if not rows:
        return []
    count = max(len(r) for r in rows)
    rows = [tuple(r) + ("",) * (count - len(r)) for r in rows]
    widths = [min(widest, max(len(r[i]) for r in rows)) for i in range(count)]
    labels = keys or ([str(n) for n in range(1, len(rows) + 1)] if index else [""] * len(rows))
    label_width = max(len(label) for label in labels)
    block: list[Parts] = []
    for n, row in enumerate(rows):
        parts: Parts = [(indent, None)]
        if labels[n]:
            parts.append((f"[{labels[n]:>{label_width}}] ", None))
        colour = colours[n] if colours and n < len(colours) else None
        room = width - len(indent) - (label_width + 3 if labels[n] else 0)
        more: list[str] = []
        for i, cell in enumerate(row):
            if tones is not None and i < len(tones):
                shade = tones[i]
            else:
                shade = colour if i == 0 else "gray"
            if i == count - 1 or not any(row[i + 1:]):  # the last column with anything in it
                if len(cell) <= room:
                    text = cell
                elif wrap and room > 10:
                    text, *more = wrap_cell(cell, room) or [""]
                else:
                    text = cell[: max(0, room - 1)] + "…"
                parts.append((text, shade))
                break
            text = cell if len(cell) <= widths[i] else cell[: widths[i] - 1] + "…"
            parts.append((text.ljust(widths[i]) + "  ", shade))
            room -= widths[i] + 2
        block.append(parts)
        lead = " " * (width - room)
        block.extend([(lead + piece, shade)] for piece in more)
    return block


class Interruptions:
    """Lines from elsewhere (the log, a server's events) that arrive while a screen is open.

    Printed the moment they come they land inside whatever question is on the screen, and the
    host can't tell whether it still waits for them. So they're held, and shown together once
    they stop coming (a quiet second), or after a few seconds when they keep coming; when a
    question was waiting, its menu block and the question itself are drawn again beneath them,
    with whatever was typed so far. push() may be called from any thread.
    """

    def __init__(self, terminal: Terminal, *, quiet: float = 1.0, longest: float = 4.0) -> None:
        self.terminal = terminal
        self.quiet = quiet
        self.longest = longest
        self.loop: asyncio.AbstractEventLoop | None = None
        self._held: list[Parts] = []
        self._first = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._redraw: Callable[[], None] | None = None  # draws the waiting question again
        self._columns: Callable[[], int] | None = None  # how wide the question is on the screen right now
        self._lock = threading.Lock()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def detach(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.loop = None

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._held)

    def push(self, text: str, colour: str | None = None) -> None:
        self.push_parts([(text, colour)])

    def push_parts(self, parts: Parts) -> None:
        with self._lock:
            if not self._held:
                self._first = time.monotonic()
            self._held.append(parts)
        loop = self.loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._arm)
        except RuntimeError:
            pass  # the loop is closed: the lines come out with the next flush, if any

    def _arm(self) -> None:
        if self.loop is None:
            return
        if self._timer is not None:
            self._timer.cancel()
        due = min(self.quiet, max(0.0, self.longest - (time.monotonic() - self._first)))
        self._timer = self.loop.call_later(due, self.flush)

    def waiting(self, redraw: Callable[[], None], columns: Callable[[], int]) -> None:
        """A question is on the screen: how to draw it again, and how wide it is."""
        self._redraw, self._columns = redraw, columns

    def done(self) -> None:
        self._redraw = self._columns = None

    def flush(self) -> None:
        """Show what's held, and draw the waiting question again beneath it."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        with self._lock:
            lines, self._held = self._held, []
        if not lines:
            return
        redraw, columns = self._redraw, self._columns
        try:  # from a timer: a slip here must never cost the screen its lines
            if redraw is not None and columns is not None:
                self.terminal.erase_prompt(columns())
            for parts in lines:
                self.terminal.print_parts(parts)
            if redraw is not None:
                self.terminal.print(record=False)
                redraw()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("held lines not shown")
