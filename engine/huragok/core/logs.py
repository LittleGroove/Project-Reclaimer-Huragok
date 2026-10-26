"""Huragok's log lines as the window and the plain log show them: each with the tag of the named part that spoke
(lore.py), and the last few hundred kept for the dashboard."""

from __future__ import annotations

import logging
from collections import deque
from typing import Callable

from ..lore import speaker


class TaggedFormatter(logging.Formatter):
    """`14:02:11 SUPT INFO    [Alpha] …`: the time, the tag of the part that spoke (lore.py), the level."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(tag)s %(levelname)-7s %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.tag = speaker(record.name).tag
        return super().format(record)


CONTINUED = " " * 22 + "↳ "  # a message's further lines, under its first: past the time, tag and level


class RecentLog(logging.Handler):
    """Keeps the last few hundred log lines for the dashboard, and echoes new ones while a screen is open."""

    def __init__(self, keep: int = 400) -> None:
        super().__init__()
        self.lines: deque[tuple[int, str]] = deque(maxlen=keep)
        self.echo: Callable[[int, str], None] | None = None
        self.setFormatter(TaggedFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            return
        # A message on several lines (a server's reply) becomes one entry per line, the rest indented
        # under the first, so every dashboard line keeps its time and level.
        first, *rest = text.splitlines() or [""]
        entries = [first, *(f"{CONTINUED}{line.strip()}" for line in rest if line.strip())]
        for entry in entries:
            self.lines.append((record.levelno, entry))
            if self.echo is not None:
                try:
                    self.echo(record.levelno, entry)
                except Exception:  # noqa: BLE001
                    pass

    def recent(self, count: int) -> list[tuple[int, str]]:
        return list(self.lines)[-count:]
