"""What the commands print, in the window's own look: a title, `─── headings ───`, numbered lists in
aligned columns with the description in gray, green for done and yellow for a problem.

Colours only when the output is a real console (and NO_COLOR isn't set); into a pipe, a file or the
tests the same lines come out plain. Problems go to stderr, as they always did.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from ..window.tui import Parts, clean, enable_vt, format_listing, heading_parts, paint

WIDE = 110


class Console:
    def __init__(self) -> None:
        self._vt: bool | None = None

    @property
    def colour(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        try:
            if not sys.stdout.isatty():
                return False
        except (AttributeError, ValueError):
            return False
        if self._vt is None:
            self._vt = enable_vt()
        return self._vt

    @property
    def width(self) -> int:
        return min(WIDE, max(60, shutil.get_terminal_size((100, 30)).columns) - 1)

    # Lines ---------------------------------------------------------------------------------

    def parts(self, parts: Parts, *, err: bool = False) -> None:
        # Coloured or not, text from players and servers (a name, a chat line, a console line in `watch`) never
        # carries a control character to the terminal: paint() cleans it, and so does clean() without colour.
        text = "".join(paint(t, c) if self.colour else clean(t) for t, c in parts)
        try:
            print(text, file=sys.stderr if err else sys.stdout, flush=True)
        except BrokenPipeError:
            # `status | head`: the reader has gone; stop quietly instead of a traceback at exit.
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            except OSError:
                pass
            raise SystemExit(0) from None

    def write(self, text: str = "", colour: str | None = None, *, err: bool = False) -> None:
        self.parts([(text, colour)], err=err)

    def banner(self) -> None:
        """HURAGOK in big letters, in the Engineer's colour, with the ODSTs' motto beneath."""
        from ..lore import BANNER, HURAGOK, MOTTO

        for row in BANNER:
            self.write(f" {row}", HURAGOK.colour)
        self.write(f" the Engineer for Project Reclaimer servers · {MOTTO}", "gray")
        self.write()

    def title(self, text: str, note: str | None = None) -> None:
        self.write(f" {text}", "white")
        if note:
            self.write(f" {note}", "gray")
        self.write(" " + "─" * (self.width - 1), "gray")

    def heading(self, *crumbs: str, note: str | None = None) -> None:
        self.write()
        self.parts(heading_parts(" › ".join(c for c in crumbs if c), self.width))
        if note:
            self.note(note)

    def note(self, text: str) -> None:
        self.write(f"  {text}", "gray")

    def say(self, text: str) -> None:
        self.write(f"  {text}")

    def ok(self, text: str) -> None:
        self.write(f"  {text}", "green")

    def warn(self, text: str) -> None:
        self.write(f"  {text}", "yellow")

    def fail(self, text: str) -> None:
        """A problem, on stderr."""
        self.write(text, "red", err=True)

    def listing(self, rows: list[tuple[str, ...]], **options: Any) -> None:
        for parts in format_listing(rows, self.width, **options):
            self.parts(parts)

    def program(self, text: str) -> None:
        """The server program's own output, line by line: its Error lines red, problems yellow, good news green."""
        for line in text.rstrip().splitlines():
            lower = line.lower()
            if lower.startswith("error"):
                colour: str | None = "red"
            elif "problem" in lower or "skipped" in lower or "cannot" in lower or "missing" in lower:
                colour = "yellow"
            elif "everything is ready" in lower or "every game is installed" in lower or ": ready" in lower \
                    or ": installed" in lower:
                colour = "green"
            else:
                colour = None
            self.write(line, colour)

    def fields(self, rows: list[tuple[str, str]], **options: Any) -> None:
        """Key/value pairs: the key in gray, the value plain, wrapped onto more lines when long."""
        options.setdefault("wrap", True)
        self.listing(rows, index=False, tones=["gray", None], **options)


console = Console()
