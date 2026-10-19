"""The supervisor's parts, beyond keeping the servers up:

  commands   in-game commands; commands.toml says which role may use each (always on)
  automod    the word list, automod.txt: removes players for the words on it
  watchdog   notices a struggling server and restarts it at a good moment
  eventlog   every event as JSON, to see what the server sends

The parts with settings each have a section in settings.toml ([automod]…). To
add a part, write it in this folder and list it here; tests/test_parts.py checks
that no two parts claim the same chat command or settings section.
"""

from __future__ import annotations

from ..part import Part
from .automod import AutoMod
from .commands import Commands
from .eventlog import EventLog
from .watchdog import Watchdog

PARTS: dict[str, type[Part]] = {cls.name: cls for cls in (Commands, AutoMod, Watchdog, EventLog)}

__all__ = ["PARTS"]
