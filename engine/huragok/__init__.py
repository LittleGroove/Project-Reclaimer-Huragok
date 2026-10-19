"""Huragok, the Engineer: one window that runs Project Reclaimer dedicated servers.

Named for Halo 3: ODST's Engineer and the two minds it carried out of New Mombasa
(lore.py): the Superintendent starts the servers and keeps them up; Vergil watches
over the players, removing them for words on a list (automod) and giving in-game
commands to the roles the host picks (commands.toml). Works the same on Windows and
Linux."""

__version__ = "0.4.1"

from .chat import CommandCall  # noqa: E402
from .events import Event  # noqa: E402
from .part import Part, command, every, on  # noqa: E402

__all__ = ["CommandCall", "Event", "Part", "__version__", "command", "every", "on"]
