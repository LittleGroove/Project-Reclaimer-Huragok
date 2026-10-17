"""Reclaimer Supervisor: one window that runs Project Reclaimer dedicated servers.
It starts them and keeps them up, removes players for words on a list (automod),
and gives in-game commands to the roles the host picks (commands.toml). Works the
same on Windows and Linux."""

__version__ = "0.2.1"

from .chat import CommandCall  # noqa: E402
from .events import Event  # noqa: E402
from .part import Part, command, every, on  # noqa: E402

__all__ = ["CommandCall", "Event", "Part", "__version__", "command", "every", "on"]
