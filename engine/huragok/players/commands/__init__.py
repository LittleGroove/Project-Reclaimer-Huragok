"""The in-game commands: what the server's console can do, for the roles the host picks.

Most hosts won't hand out the RCON password, and the server's own admins only get
kick and ban. These commands put the rest in game chat, and commands.toml says
who may use each one (core/commandsfile.py). Players read who acted ("Bravo was muted
by Alice"), every action is recorded with its reason, and nobody can act on
someone of their own role or above (owners excepted).

The commands are grouped by what they're for, one file each; this class joins
them into one part. Add a command to the file it fits, or start a new file for
a new group and add its class here. Commands that need another part's state
(automod's !strike, the watchdog's !health) live with that part. roles.py moves
players along the roles a step at a time (!promote, !demote; one role each, and
a ban takes it away). lists.py lists the bans and kicks, numbered for !unban and !pardon.
history.py puts every kick, mute and ban in the ledger, however it was given (! or / or the console), and !whois
reads a player's back as a rap sheet. said.py keeps every player's chat lines 30 days, for !said. across.py takes kicks and bans to every server
([automod] across_servers). votekicks.py has no commands: it keeps everyone
commands.toml lists safe from votes to kick. toggles.py switches a server's voice and chat (dedicated.toml),
for a while or for good.
Finding the player a command names, with its guards, is targets.py's.
"""

from __future__ import annotations

from ...lore import VERGIL
from .across import AcrossServers
from .history import HistoryCommands
from .lists import ListCommands
from .moderation import ModerationCommands
from .players import PlayerCommands
from .roles import RoleCommands
from .said import SaidCommands
from .schema import MIGRATIONS
from .server import ServerCommands
from .toggles import SettingToggles
from .votekicks import VoteKicks


class Commands(PlayerCommands, ModerationCommands, ListCommands, HistoryCommands, AcrossServers, SaidCommands,
               RoleCommands, ServerCommands, SettingToggles, VoteKicks):
    persona = VERGIL
    name = "commands"
    description = "In-game commands, each for the roles commands.toml gives it"
    acts_as = "Vergil"
    always_on = True

    async def setup(self) -> None:
        self.ctx.db.migrate(MIGRATIONS)  # the chat lines !said reads, Huragok's own list for every server
        await self._import_shared_lists()  # the bans servers shared up to 0.4.4, onto Huragok's list


__all__ = ["Commands"]
