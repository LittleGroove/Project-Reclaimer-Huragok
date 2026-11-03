"""Huragok closing: what a long piece of work in a thread looks at, so it ends the program it runs and returns rather
than holding the process open after Huragok is done (a SteamCMD download of hours, `dedicated init` copying from MCC, a
Workshop fetch). asyncio.run waits for every such thread before the process can end; meanwhile nothing is watched, the
lock stays held and the start loop can't start the next version (audit 2026-11, 2.1).

A count, not a flag: each piece of work notes it as it starts (`mark`) and stops once it has moved on (`closed_since`).
A flag set at one close would stay set for whatever runs after it in the same process (the next Huragok, a test).
"""

from __future__ import annotations

_closes = 0


def mark() -> int:
    """Now, for closed_since."""
    return _closes


def close() -> None:
    """Huragok is closing: every piece of work started before now stops."""
    global _closes
    _closes += 1


def closed_since(started: int) -> bool:
    """Whether Huragok has closed since `started` (a mark)."""
    return _closes != started


__all__ = ["close", "closed_since", "mark"]
