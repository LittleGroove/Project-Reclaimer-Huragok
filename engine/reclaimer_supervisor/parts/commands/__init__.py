"""The in-game commands: what the server's console can do, for the roles the host picks.

Most hosts won't hand out the RCON password, and the server's own admins only get
kick and ban. These commands put the rest in game chat, and commands.toml says
who may use each one (permissions.py). Players read who acted ("Bravo was muted
by Alice"), every action is recorded with its reason, and nobody can act on
someone of their own role or above (owners excepted).

The commands are grouped by what they're for, one file each; this class joins
them into one part. Add a command to the file it fits, or start a new file for
a new group and add its class here. Commands that need another part's state
(automod's !strike, the watchdog's !health) live with that part.
"""

from __future__ import annotations

from .moderation import ModerationCommands
from .players import PlayerCommands
from .server import ServerCommands


class Commands(PlayerCommands, ModerationCommands, ServerCommands):
    name = "commands"
    description = "In-game commands, each for the roles commands.toml gives it"
    acts_as = "Supervisor"
    always_on = True


__all__ = ["Commands"]
