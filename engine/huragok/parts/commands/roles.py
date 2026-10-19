"""Who's who, changed in game: one role each, on a ladder, a step at a time.

    player -> member -> moderator -> admin -> owner

Everyone holds exactly one role. !promote moves a player one step up (out of the role they had), !demote one step
down: an admin becomes a moderator, a moderator a member, a member a player again. A player can be promoted up to
the role below the caller's own (moderators make members, admins make moderators, owners make admins) and demoted
only by someone above them. Owners are the host's: changed only in commands.toml. A ban takes every role away
(and since nobody acts on their own role or above, only an owner can ban an admin).

Members and moderators live in commands.toml, one line per player saying who they are and who changed it, the
rest of the file as the host wrote it. Admins are the server's own list (the console's `admin add` and `admin
remove`, kept in the ban list every server sharing it reads); an admin dedicated.toml names stays one until that
file says otherwise, and the reply says so.
"""

from __future__ import annotations

import asyncio
import time

from ...chat import CommandCall
from ...events import Event
from ...part import command, on
from ...permissions import ROLES, a_role
from ...rcon import RconError
from ...state import Player
from ...tomledit import add_to_list, remove_from_list
from .base import CommandsBase

IN_FILE = ("member", "moderator")  # the roles commands.toml lists that a promotion or demotion changes
STILL_CONFIGURED = "dedicated.toml still makes them one"  # the console's answer for an admin that file names
MARK = "Vergil"  # whose change it is when nobody typed it (a ban)


class RoleCommands(CommandsBase):
    @command("promote", usage="<player>", help="One step up the roles: player, member, moderator, admin (up to the "
             "role below yours)", role="moderator")
    async def promote(self, call: CommandCall) -> None:
        target = await self._role_target(call, "promote")
        if target is None:
            return
        now = self.ctx.role_of(call.server, target)
        who = self._role_name(target)
        if now in ("admin", "owner"):
            await call.reply(f"{who} is {a_role(now)}: as high as a promotion goes" + (" (owners are listed in "
                             "commands.toml)." if now == "admin" else "."))
            return
        new = ROLES[ROLES.index(now) + 1]
        if ROLES.index(call.role) <= ROLES.index(new):
            await call.reply(f"{who} would be {a_role(new)}, and you can promote players only to the role below "
                             f"yours ({a_role(ROLES[ROLES.index(call.role) - 1])} at most).")
            return
        if await self._role_move(call, target, now, new, "promoted"):
            await call.reply(f"{who} is {a_role(new)} now" + (f" (was {a_role(now)})." if now != "player" else "."))
            await self._role_tell(call, target, f"{call.player.name} made you {a_role(new)} here.")

    @command("demote", usage="<player>", help="One step down the roles: an admin becomes a moderator, a moderator a "
             "member, a member a player", role="moderator")
    async def demote(self, call: CommandCall) -> None:
        target = await self._role_target(call, "demote")
        if target is None:
            return
        await self._role_demote(call, target)

    @command("admin", usage="[list | add <player> | remove <player>]",
             help="The admins (the server's own list, shared by every server using the ban list): add makes a player "
                  "one, remove makes them a moderator", role="owner")
    async def admin(self, call: CommandCall) -> None:
        verb = call.args[0].lower() if call.args else "list"
        if verb not in ("add", "remove") or len(call.args) < 2:
            await self._line(call, "admin")  # the list, as the server keeps it
            return
        target = await call.find_target(" ".join(call.args[1:]), history=self.ctx.players, offline=True)
        if target is None:
            return
        now, who = self.ctx.role_of(call.server, target), self._role_name(target)
        if verb == "remove":
            if now != "admin":
                await call.reply(f"{who} isn't an admin" + (f" (they're {a_role(now)})." if now != "player" else "."))
                return
            await self._role_demote(call, target)
            return
        if now in ("admin", "owner"):
            await call.reply(f"{who} is {a_role(now)} already.")
            return
        if await self._role_move(call, target, now, "admin", "made an admin"):
            await call.reply(f"{who} is an admin now, on every server sharing the ban list"
                             + (f" (was {a_role(now)})." if now != "player" else "."))
            await self._role_tell(call, target, f"{call.player.name} made you an admin here.")

    @command("roles", help="Who has which role: owners, admins, moderators, members", role="moderator")
    async def roles(self, call: CommandCall) -> None:
        members = self.ctx.permissions.members
        admins = self._role_admins(call.server)
        lines = []
        for role, ids in (("owner", members.get("owner", set())), ("admin", admins),
                          ("moderator", members.get("moderator", set())), ("member", members.get("member", set()))):
            names = [self._role_seen(i) for i in sorted(ids)]
            lines.append(f"{role}s ({len(names)}): {', '.join(names) if names else 'none'}")
        await self._reply_text(call, "\n".join(lines))

    @on("rcon.ban")
    async def banned(self, event: Event) -> None:
        """A ban takes every role away: the role goes with the right to play, whoever banned them."""
        player_id = str(event.get("id") or "").lower()
        if not player_id or not event.server:
            return
        target = Player(0, str(event.get("name") or self._role_seen(player_id)), player_id)
        now = self.ctx.role_of(event.server, target)
        if now == "player":
            return
        if now == "owner":
            self.ctx.log.warning("[%s] %s (an owner) was banned; owners are changed only in commands.toml",
                                 event.server, target.name)
            return
        lost, note = await self._role_strip(event.server, target, now, by=MARK)
        if lost:
            self.ctx.log.info("[%s] %s was banned, and their %s role went with it%s", event.server, target.name,
                              now, f" ({note})" if note else "")

    # Moving a player along the ladder ------------------------------------------------------------------------

    async def _role_target(self, call: CommandCall, verb: str) -> Player | None:
        if not call.args:
            await call.reply(f"Use: {call.prefix}{verb} <player>: their #number, player ID, or a name Vergil has seen")
            return None
        # Someone who isn't on is found by player ID or a name Vergil has seen them use; never a name's start.
        return await call.find_target(call.text, history=self.ctx.players, offline=True)

    async def _role_demote(self, call: CommandCall, target: Player) -> None:
        now, who = self.ctx.role_of(call.server, target), self._role_name(target)
        if now == "player":
            await call.reply(f"{who} has no role to lose.")
            return
        if now == "owner":
            await call.reply(f"{who} is an owner: owners are changed only in commands.toml.")
            return
        if ROLES.index(call.role) <= ROLES.index(now):
            await call.reply(f"{who} is {a_role(now)}; you can't demote them.")
            return
        new = ROLES[ROLES.index(now) - 1]
        if await self._role_move(call, target, now, new, "demoted"):
            await call.reply(f"{who} is {a_role(new)} now (was {a_role(now)})." if new != "player"
                             else f"{who} has no role now (was {a_role(now)}).")

    async def _role_move(self, call: CommandCall, target: Player, old: str, new: str, how: str) -> bool:
        """Take `target` from `old` to `new`, so they hold that one role alone. False: it couldn't, and the caller
        was told (nothing half-changed is left behind)."""
        path = self.ctx.permissions.path or self.ctx.home / "commands.toml"
        who = self._role_name(target)
        note = f"{who}: {how} by {call.player.name} on {time.strftime('%Y-%m-%d')}"
        added = made_admin = False
        try:
            if new in IN_FILE:
                added = await asyncio.to_thread(add_to_list, path, new, "players", target.id, note)
            elif new == "admin":
                await self._role_admin(call.server, "add", target.id, call.player.name)
                made_admin = True
            for section in IN_FILE:
                if section != new and target.id in self.ctx.permissions.members.get(section, set()):
                    await asyncio.to_thread(remove_from_list, path, section, "players", target.id)
            warning = ""
            if old == "admin":
                warning = await self._role_admin(call.server, "remove", target.id, call.player.name)
        except (OSError, RconError) as error:
            # Put back what was done: never two roles, never one lost half way.
            try:
                if added:
                    await asyncio.to_thread(remove_from_list, path, new, "players", target.id)
                if made_admin:
                    await self._role_admin(call.server, "remove", target.id, call.player.name)
            except (OSError, RconError):
                pass
            self.ctx.reload_permissions()
            await call.reply(f"Couldn't: {error}. {who} stays {a_role(old)}." if old != "player"
                             else f"Couldn't: {error}.")
            return False
        self.ctx.reload_permissions()
        self.ctx.log.info("[%s] %s %s %s: %s -> %s", call.server, call.player.name, how, who, old, new)
        if warning:
            await call.reply(warning)
        return True

    async def _role_strip(self, server: str, target: Player, now: str, *, by: str) -> tuple[bool, str]:
        """Every role but owner off `target` (a ban). (Anything lost, and a note when an admin stays one.)"""
        path = self.ctx.permissions.path or self.ctx.home / "commands.toml"
        lost, note = False, ""
        for section in IN_FILE:
            if target.id in self.ctx.permissions.members.get(section, set()):
                try:
                    lost = await asyncio.to_thread(remove_from_list, path, section, "players", target.id) or lost
                except OSError as error:
                    self.ctx.log.error("[%s] %s's %s role couldn't be taken off: %s", server, target.name, section,
                                       error)
        if now == "admin":
            try:
                note = await self._role_admin(server, "remove", target.id, by)
                lost = True
            except RconError as error:
                self.ctx.log.error("[%s] %s is still an admin: %s", server, target.name, error)
        self.ctx.reload_permissions()
        return lost, note

    async def _role_admin(self, server: str, verb: str, player_id: str, by: str) -> str:
        """`admin add|remove <ID>` on the server's console. A warning to pass on ("" for none); RconError when the
        console refused it or didn't answer."""
        reply = await self.ctx.rcon(server).command("admin", verb, player_id, by=by)
        if not reply.ok:
            raise RconError(reply.text or f"the console refused admin {verb}")
        if STILL_CONFIGURED in (reply.text or ""):
            return (f"dedicated.toml's admins still name them, so the server still treats them as an admin: take "
                    f"them out there (D -> Safety and admins) to finish it.")
        return ""

    # Saying who ---------------------------------------------------------------------------------------------

    def _role_name(self, target: Player) -> str:
        return target.name or self._role_seen(target.id)

    def _role_seen(self, player_id: str) -> str:
        record = self.ctx.players.get(player_id)
        return f"{record.last_name} ({player_id[:8]})" if record else player_id[:8]

    def _role_admins(self, server: str) -> set[str]:
        state = self.ctx.server(server)
        return set(state.admins) if state.admins is not None else {p.id for p in state.players.values() if p.admin}

    async def _role_tell(self, call: CommandCall, target: Player, text: str) -> None:
        if not target.number:  # not on this server now
            return
        try:
            await self.ctx.tell(call.server, target, text)
        except RconError:  # a greeting that doesn't arrive changes nothing
            self.ctx.log.debug("[%s] couldn't tell %s", call.server, target.name)
