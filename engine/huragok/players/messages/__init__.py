"""messages: what Huragok says to the players by itself. For now, interval messages: intervalmessages.txt in
Huragok's folder for every server, and one in each server's folder for that server's own, one per line:

    30m | Join us on Discord: discord.gg/yourserver

The host sets them up (the files, or D → I in the window); there are no in-game commands for them yet.
"""

from .intervals import FILE
from .messages import Messages

__all__ = ["FILE", "Messages"]
