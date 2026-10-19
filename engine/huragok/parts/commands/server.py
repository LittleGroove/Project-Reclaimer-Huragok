"""Running the server: the console's own commands, passed through as the player who typed them.

There's no !password: commands are public chat, so everyone would read it.
"""

from __future__ import annotations

from ...chat import CommandCall
from ...part import command
from .base import CommandsBase


class ServerCommands(CommandsBase):
    @command("say", usage="<text>", help="A [Server] line for everyone", role="admin")
    async def say(self, call: CommandCall) -> None:
        if call.text:
            await self._rcon(call, "say", call.text)

    # Games ------------------------------------------------------------------------------

    @command("map", usage="<map>", help="Load a map with the current rules", role="admin")
    async def map(self, call: CommandCall) -> None:
        await self._line(call, "map", needs_words=True)

    @command("mode", usage="<mode>", help="Load a mode on the current map", role="admin")
    async def mode(self, call: CommandCall) -> None:
        await self._line(call, "mode", needs_words=True)

    @command("load", usage="<map> <mode>", help="End this game and load a map and mode", role="admin")
    async def load(self, call: CommandCall) -> None:
        await self._line(call, "load", needs_words=True)

    @command("nextmap", usage="[<map> [<mode>]]", help="Show or pick the next game", role="admin")
    async def nextmap(self, call: CommandCall) -> None:
        await self._line(call, "nextmap")

    @command("endgame", help="End the game with the scores as they stand", role="admin")
    async def endgame(self, call: CommandCall) -> None:
        await self._rcon(call, "endgame")

    @command("endround", help="End the round as if its time ran out", role="admin")
    async def endround(self, call: CommandCall) -> None:
        await self._rcon(call, "endround")

    # Teams ------------------------------------------------------------------------------

    @command("shuffle", help="Shuffle and balance the teams", role="admin")
    async def shuffle(self, call: CommandCall) -> None:
        await self._rcon(call, "shuffle")

    @command("team", usage="<player> <red|blue|green|orange|purple|gold|brown|pink>", help="Move a player",
             role="admin")
    async def team(self, call: CommandCall) -> None:
        if len(call.args) < 2:
            await call.reply(f"Use: {call.prefix}team <player> <team color>")
            return
        target = await call.find_player(" ".join(call.args[:-1]))
        if target:
            await self._rcon(call, "team", target.id, call.args[-1])

    @command("teamcount", usage="[2-64]", help="Show teams, or spread players over this many", role="admin")
    async def teamcount(self, call: CommandCall) -> None:
        await self._line(call, "teamcount")

    # Votes ------------------------------------------------------------------------------

    @command("startvote", usage="<endround|endgame|shuffle|kick|playlist> [player]", help="Call a vote",
             role="admin")
    async def startvote(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}startvote <endround|endgame|shuffle|kick|playlist> [player]")
            return
        if call.args[0].lower() == "kick" and len(call.args) > 1:
            target = await call.find_player(" ".join(call.args[1:]))
            if target:
                await self._rcon(call, "startvote", "kick", target.id)
            return
        await self._line(call, "startvote")

    @command("cancelvote", help="End the vote under way with no result", role="admin")
    async def cancelvote(self, call: CommandCall) -> None:
        await self._rcon(call, "cancelvote")

    @command("passvote", help="Pass the vote under way", role="admin")
    async def passvote(self, call: CommandCall) -> None:
        await self._rcon(call, "passvote")

    # The server itself -------------------------------------------------------------------

    @command("maxping", usage="[<milliseconds> | off]", help="Show or set the ping limit for joining", role="admin")
    async def maxping(self, call: CommandCall) -> None:
        await self._line(call, "maxping")

    @command("servername", usage="[<name>]", help="Show or change the server's name until it restarts",
             role="admin")
    async def servername(self, call: CommandCall) -> None:
        await self._line(call, "servername")

    @command("reload", help="Apply edits to dedicated.toml now", role="owner")
    async def reload(self, call: CommandCall) -> None:
        await self._rcon(call, "reload")
