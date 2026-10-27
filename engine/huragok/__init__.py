"""Huragok, the Engineer: one window that runs Project Reclaimer dedicated servers.

Named for Halo 3: ODST's Engineer and the two minds it carried out of New Mombasa
(lore.py): the Superintendent starts the servers and keeps them up; Vergil watches
over the players, removing them for words on a list (automod) and giving in-game
commands to the roles the host picks (commands.toml). Works the same on Windows and
Linux."""

__version__ = "0.5.1"

# Unzipped over an older engine by hand (an update swaps the whole folder; a hand install should delete it first):
# 0.4.x's parts/ package would be imported in place of parts.py, a mix of two versions. Said plainly instead.
from pathlib import Path as _Path  # noqa: E402

if (_Path(__file__).with_name("parts") / "__init__.py").is_file():
    raise ImportError(f"{_Path(__file__).parent} still holds files of an older Huragok (parts/, core.py and more): "
                      "it was unzipped over the old one. Delete the engine folder (your settings, servers and records "
                      "aren't in it), copy the new one's engine folder in its place, and start again.")

from .players.router import CommandCall  # noqa: E402
from .core.events import Event  # noqa: E402
from .core.part import Part, command, every, on  # noqa: E402

__all__ = ["CommandCall", "Event", "Part", "__version__", "command", "every", "on"]
