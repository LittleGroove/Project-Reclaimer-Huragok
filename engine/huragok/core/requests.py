"""What a request to Huragok gets back: from the window, start.cmd (through records/requests/), the console without a
window, or the relay. The fleet answers a server's own (start, stop, restart, say…), Huragok the rest (update,
banall, game_files…)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Result:
    ok: bool
    message: str
    text: str = ""
