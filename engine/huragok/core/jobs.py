"""Work that needs a server stopped (the program's old file removed after its update): the Superintendent runs it
at the server's next start, between the stop and the start of a restart."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class StoppedJob:
    """Work that needs the server stopped (removing the program's old file): it runs at the next start,
    between the stop and the start of a restart. Returns what it did, for the log."""

    key: str  # one job per key: a newer one replaces it
    label: str  # what the dashboard shows while it runs
    run: Callable[[], Awaitable[str]]
