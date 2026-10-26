"""The terminal: colours, a frame redrawn in place, single keys as they're pressed, a log
handler that keeps recent lines for the dashboard, and the holding of lines that arrive
while a screen waits for a key. Windows 10+ and Linux, nothing to install.

The window is the supervisor (every server's care runs on its event loop), so nothing here may
make the loop wait for the console: what's shown goes out from a thread of its own (ConsoleWriter).
Text from players and servers is made safe before it's shown (clean), and lined up by the columns
it takes on the screen rather than by its length in characters (cells, cut, pad).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from typing import Any, Callable

Parts = list[tuple[str, str | None]]  # one line in several colours: [(text, colour), ...]

COLORS = {
    "default": "\x1b[0m", "white": "\x1b[97m", "gray": "\x1b[90m", "grey": "\x1b[90m", "red": "\x1b[91m",
    "yellow": "\x1b[93m", "green": "\x1b[92m", "cyan": "\x1b[96m", "magenta": "\x1b[95m",
    "darkyellow": "\x1b[33m", "blue": "\x1b[94m",
    # The three parts' own (lore.py): the Superintendent's signs, Vergil's camera eye, the Engineer's glow.
    "amber": "\x1b[38;5;214m", "sky": "\x1b[38;5;117m", "violet": "\x1b[38;5;141m",
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
    text = clean(text)
    code = COLORS.get(color or "", "")
    return f"{code}{text}{RESET}" if code else text


# Text from players and servers: made safe, and measured as the screen shows it ----------------------

# C0 and C1 control characters (all but the newline) and lone surrogates. A player's name or chat line, a
# server's reply or a console-log line carrying ESC or C1 sequences would otherwise run in the host's terminal,
# every frame: move the cursor, retitle the window, and in Windows Terminal OSC 52 even writes the clipboard. A
# lone surrogate (a name cut inside an emoji) can't be encoded, which used to cost the whole frame.
_UNSAFE = re.compile("[\x00-\x09\x0b-\x1f\x7f-\x9f\ud800-\udfff]")


def _safe(match: re.Match[str]) -> str:
    return " " if match.group() == "\t" else "?"


def clean(text: str, *, newlines: bool = True) -> str:
    """Text as it may reach the terminal: a tab becomes a space, any other control character (and a lone
    surrogate) a ?. Newlines stay, unless newlines=False (one line of a frame: they become spaces)."""
    if not newlines and "\n" in text:
        text = text.replace("\n", " ")
    return _UNSAFE.sub(_safe, text) if _UNSAFE.search(text) else text


def _columns(char: str) -> int:
    if unicodedata.combining(char) or unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0  # an accent on the letter before, a zero-width joiner, an emoji's variation selector
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1  # CJK and most emoji take two


def cells(text: str) -> int:
    """How many columns text takes on the screen (len() counts a CJK character or an emoji as one)."""
    if text.isascii():
        return len(text)
    return sum(_columns(char) for char in text)


def cut(text: str, width: int, mark: str = "…") -> str:
    """At most `width` columns: shortened, ending in `mark`, when it's wider."""
    if cells(text) <= width:
        return text
    room = width - cells(mark)
    if room < 0:
        return ""
    kept: list[str] = []
    used = 0
    for char in text:
        size = _columns(char)
        if used + size > room:
            break
        kept.append(char)
        used += size
    return "".join(kept) + mark


def pad(text: str, width: int) -> str:
    """Text and spaces up to `width` columns (str.ljust, by columns)."""
    return text + " " * max(0, width - cells(text))


def _emit(stream: Any, text: str) -> None:
    """Write and flush; a console that can't take a character gets a ? instead of losing the whole write."""
    try:
        stream.write(text)
    except UnicodeEncodeError:  # (a ValueError: caught first) a code page without these characters
        encoding = getattr(stream, "encoding", None) or "ascii"
        try:
            stream.write(text.encode(encoding, "replace").decode(encoding))
        except (OSError, ValueError, LookupError):
            pass
    except (OSError, ValueError):
        pass
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


class ConsoleWriter:
    """Writes to the console from a thread of its own, so the event loop never waits for the window.

    A classic console window stops taking output while text is selected in it (Edit > Mark, the way to copy
    with QuickEdit off) or after the Pause key, and whatever writes to it waits until the selection ends. On the
    loop that wait would stop every server's supervision (it froze the live server program once, the same way).
    So output is handed to this thread instead: screens' text in order, and a dashboard frame still waiting its
    turn is replaced by the newer one (only the newest matters). Should a paused window hold back more than
    LIMIT characters, the oldest are dropped and the window says so when it takes output again; logs/window.log
    has what the screens showed in any case.
    """

    LIMIT = 1_000_000

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self._ready = threading.Condition()
        self._waiting: deque[tuple[str, bool]] = deque()  # (text, is a dashboard frame)
        self._size = 0
        self._busy = False  # a write is under way (it may be the one a paused window holds)
        self._dropped = 0
        self._thread: threading.Thread | None = None

    def put(self, text: str, *, frame: bool = False) -> None:
        if not text:
            return
        with self._ready:
            if frame and self._waiting and self._waiting[-1][1]:
                self._size -= len(self._waiting.pop()[0])  # a frame nobody saw: the new one replaces it
            self._waiting.append((text, frame))
            self._size += len(text)
            while self._size > self.LIMIT and len(self._waiting) > 1:
                self._size -= len(self._waiting.popleft()[0])
                self._dropped += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="console", daemon=True)
                self._thread.start()
                atexit.register(self.flush, 2.0)  # what's still waiting goes out before the process ends
            self._ready.notify_all()

    def _run(self) -> None:
        while True:
            with self._ready:
                while not self._waiting:
                    self._busy = False
                    self._ready.notify_all()  # for flush()
                    self._ready.wait()
                text, _ = self._waiting.popleft()
                self._size -= len(text)
                self._busy = True
                dropped, self._dropped = self._dropped, 0
            if dropped:
                _emit(self.stream, f"\n  ({dropped} piece(s) of this window's output were dropped while it took none: "
                                   "text was selected in it, or Pause was pressed. logs/window.log has what the "
                                   "screens showed.)\n")
            _emit(self.stream, text)

    def pending(self) -> int:
        with self._ready:
            return len(self._waiting) + (1 if self._busy else 0)

    def flush(self, timeout: float | None = None) -> bool:
        """Wait until everything handed over has been written; False when `timeout` passed first (the window
        still takes no output)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._ready:
            while self._waiting or self._busy:
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return False
                self._ready.wait(left)
        return True


class Terminal:
    """Draws a frame of lines in place, and plain lines when a screen is in progress."""

    transcript: Any = None  # a file that gets a plain copy of what a screen prints (logs/window.log)
    colour: bool = False  # the console understands escape sequences (set when a real one is opened)
    writer: ConsoleWriter | None = None  # the console's own thread (none: written straight out)

    def __init__(self, stream=None, *, threaded: bool = True) -> None:
        self.out = stream or sys.stdout
        self.colour = enable_vt()
        self._last_lines = 0
        self._last_width = 0
        self.transcript = None
        self.writer = ConsoleWriter(self.out) if threaded else None

    def note(self, text: str) -> None:
        """A line for the transcript only (what was typed at a prompt, which the screen echoed key by key)."""
        if self.transcript is not None:
            try:
                self.transcript.write(clean(text) + "\n")
                self.transcript.flush()
            except (OSError, ValueError):
                pass

    def size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((100, 30))
        return max(60, size.columns), max(12, size.lines)

    def _write(self, text: str, frame: bool = False) -> None:
        """Everything for the screen goes through here: to the console's thread, which writes it in order
        (frame=True: a dashboard frame, which a newer one may replace while it waits)."""
        if self.writer is not None:
            self.writer.put(text, frame=frame)
        else:
            _emit(self.out, text)

    def flush(self, timeout: float | None = 2.0) -> bool:
        """Wait (at most `timeout`) until what was written is on the screen: before the process ends, or before
        something else writes to the console directly."""
        return self.writer.flush(timeout) if self.writer is not None else True

    def backspace(self, columns: int = 1) -> None:
        """Take back what the cursor just passed over (a prompt's Backspace)."""
        self._write("\b" * columns + " " * columns + "\b" * columns)

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
            # Exactly one row each, by the columns the text takes: a wider line would wrap and scroll the frame.
            out.append(paint(pad(cut(clean(text, newlines=False), width), width), color) + "\x1b[K\n")
            shown += 1
        out.append("\x1b[J")
        self._last_lines = shown
        self._write("".join(out), frame=True)

    def print(self, text: str = "", color: str | None = None, end: str = "\n", *, record: bool = True) -> None:
        self.print_parts([(text, color)], end, record=record)

    def print_parts(self, parts: Parts, end: str = "\n", *, record: bool = True) -> None:
        """One line in several colours. record=False keeps it out of the transcript (a prompt drawn again)."""
        parts = [(clean(text), color) for text, color in parts]
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
        self.loop = loop or asyncio.get_running_loop()  # (get_event_loop() raises in 3.14 with none running)
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

    _last_ctrl_c = 0.0

    def ctrl_c(self, *_: object) -> None:
        """Ctrl+C as a key (the window's signal handler): Q's question, never the end of supervision. A terminal
        sends it to the launcher too, which passes it on, so a second one within a second is the same press."""
        now = time.monotonic()
        if now - self._last_ctrl_c >= 1.0:
            self._last_ctrl_c = now
            self._push("CTRL_C")

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

    @staticmethod
    def windows_char(key_down: bool, char: str) -> str | None:
        """What one Windows console key event typed: None for a key let go, or a key that types nothing (an
        arrow, a function key, Shift alone). msvcrt.getwch() gave '\\xe0' first for an arrow and for "à" alike
        (unshifted 0 on a French keyboard), and its second code can't be told apart, so keys are read as events."""
        if not key_down or not char or char == "\x00":
            return None
        return char

    def _read_windows(self) -> None:
        try:
            console = _ConsoleInput()
        except (OSError, AttributeError):  # no console of our own to read events from: msvcrt's way
            self._read_msvcrt()
            return
        try:
            while not self._stop.is_set():
                try:
                    events = console.read(0.1)
                except OSError:
                    return
                for down, typed in events:
                    char = self.windows_char(down, typed)
                    key = self.name_of(char) if char else None
                    if key:
                        self._push(key)
        finally:
            console.close()

    def _read_msvcrt(self) -> None:
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
        """Drop the keys pressed so far and not read (pressed while long work ran: never the next answer)."""
        while not self.queue.empty():
            self.queue.get_nowait()


class _ConsoleInput:
    """Windows: the console's key events (ReadConsoleInputW on CONIN$, as msvcrt reads them), each with the
    character it typed, if any. Raises OSError when there's no console to read."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class KeyEvent(ctypes.Structure):
            _fields_ = [("down", wintypes.BOOL), ("repeat", wintypes.WORD), ("key", wintypes.WORD),
                        ("scan", wintypes.WORD), ("char", wintypes.WCHAR), ("state", wintypes.DWORD)]

        class Event(ctypes.Union):
            _fields_ = [("key", KeyEvent), ("size", ctypes.c_byte * 16)]

        class Record(ctypes.Structure):
            _fields_ = [("kind", wintypes.WORD), ("event", Event)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Record), wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)  # read+write, shared, existing
        mode = wintypes.DWORD()
        if not handle or handle == wintypes.HANDLE(-1).value:
            raise OSError("no console input")
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.CloseHandle(handle)
            raise OSError("not a console")
        self._ctypes, self._kernel32, self._handle = ctypes, kernel32, handle
        self._records = (Record * 16)()
        self._count = wintypes.DWORD()

    def read(self, timeout: float) -> list[tuple[bool, str]]:
        """The key events (pressed?, character typed or '\\x00') that came within `timeout` seconds."""
        waited = self._kernel32.WaitForSingleObject(self._handle, int(timeout * 1000))
        if waited == 0x102:  # WAIT_TIMEOUT
            return []
        if waited != 0:
            raise OSError("the console's input can't be waited on")
        if not self._kernel32.ReadConsoleInputW(self._handle, self._records, len(self._records),
                                                self._ctypes.byref(self._count)):
            raise OSError("the console's input can't be read")
        return [(bool(record.event.key.down), record.event.key.char or "\x00")
                for record in self._records[: self._count.value] if record.kind == 1]  # KEY_EVENT

    def close(self) -> None:
        self._kernel32.CloseHandle(self._handle)


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
            (" " + "─" * max(3, width - cells(path) - len(indent) - 5), "gray")]


def _split_columns(text: str, width: int) -> list[str]:
    """Text in pieces of at most `width` columns (a wide character never split from itself)."""
    pieces: list[str] = []
    while cells(text) > width:
        head = cut(text, width, "")
        if not head:  # narrower than one wide character: it goes on a line of its own
            head = text[0]
        pieces.append(head)
        text = text[len(head):]
    return pieces + ([text] if text else [])


def wrap_cell(text: str, width: int) -> list[str]:
    """Lines of at most `width` columns, broken at spaces, or inside a long path after a `\\` or `/`."""
    pieces: list[str] = []
    for word in re.findall(r"\S+\s*", text):
        if cells(word.rstrip()) <= width:
            pieces.append(word)
            continue
        for part in re.findall(r"[^\\/]*[\\/]|[^\\/]+$", word):  # a path, segment by segment
            pieces.extend(_split_columns(part, width))  # a segment longer than the line is cut
    lines: list[str] = []
    line = ""
    for piece in pieces:
        if line and cells((line + piece).rstrip()) > width:
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
    widths = [min(widest, max(cells(r[i]) for r in rows)) for i in range(count)]
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
                if cells(cell) <= room:
                    text = cell
                elif wrap and room > 10:
                    text, *more = wrap_cell(cell, room) or [""]
                else:
                    text = cut(cell, max(1, room))
                parts.append((text, shade))
                break
            parts.append((pad(cut(cell, widths[i]), widths[i]) + "  ", shade))
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
