"""Work that needs a server stopped (a Workshop mod's new version swapped in, the server program replaced): the
Superintendent runs it at the server's next start, between the stop and the start of a restart."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class StoppedJob:
    """Work that needs the server stopped (swapping in a Workshop mod's new version): it runs at the next start,
    between the stop and the start of a restart. Returns what it did, for the log."""

    key: str  # one job per key: a newer one replaces it
    label: str  # what the dashboard shows while it runs
    run: Callable[[], Awaitable[str]]
