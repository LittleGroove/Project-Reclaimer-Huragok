"""Votes to kick: no vote can kick anyone commands.toml lists, or the server's own admins.

The server never lets a vote kick its admins, and keeps a list of other players no vote can
kick (its console's `immune`, kept in its ban list: each server's own since 0.5.0, so Vergil
keeps every server's). Vergil keeps that list in step with commands.toml: every member, moderator and
owner is on it, put there as the server connects, when commands.toml changes, and every few
minutes in case someone took them off; whoever is taken out of commands.toml comes off it
again. Only the entries Vergil added ("Vergil (RCON)") are ever taken off: the host's own,
added on the console or in dedicated.toml's vote_kick_immune, stay.

A vote to kick one of them is refused as it's called. The caller reads the server's "Alice
can't be vote-kicked on this server.", and Vergil, reading that in server.log, tells them why.
On a server program without the list, or before the list is in step, a vote that does get
called is cancelled at once, and everyone reads why.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from ...core.events import Event
from ...core.part import every, on
from ...lore import callout
from ...rcon import RconError
from ...servers.state import Player, ServerState
from .base import CommandsBase

if TYPE_CHECKING:
    from ...core.context import PartContext

MARK = "Vergil"  # the `by` of every entry Vergil adds; the list records it as "Vergil (RCON)"
OURS = f"{MARK} (RCON)"
TIMEOUT = 3.0  # seconds for each console command, so a struggling server can't hold the part up
BUDGET = 6.0  # seconds one pass may take, within the part's 10 s per job; what's left waits for the next
TELL_GAP = 30.0  # seconds between explanations to the same caller
IMMUNE = re.compile(r"(?P<name>.+) can't be vote-kicked on this server\.?")
SUFFIX = re.compile(r"(?P<name>.+) \([0-9A-Za-z]{2,6}\)")  # "Player (A7B)": how votes tell same-named players apart
ARTICLE = {"member": "a member", "moderator": "a moderator", "admin": "an admin", "owner": "an owner"}


def _named(state: ServerState, name: str) -> list[Player]:
    """The connected players a vote's name could mean: the exact name, else the name without its suffix."""
    folded = name.strip().casefold()
    exact = [p for p in state.players.values() if p.name.casefold() == folded]
    if exact or not (match := SUFFIX.fullmatch(name.strip())):
        return exact
    base = match["name"].casefold()
    return [p for p in state.players.values() if p.name.casefold() == base]


def _entries(data: object) -> tuple[dict[str, str], set[str]]:
    """`immune list`'s data -> ({player ID: added_by} the console added, {IDs dedicated.toml names})."""
    data = data if isinstance(data, dict) else {}
    added = {str(e["id"]).lower(): str(e.get("added_by") or "") for e in data.get("added") or []
             if isinstance(e, dict) and e.get("id")}
    configured = {str(e.get("id") if isinstance(e, dict) else e).lower() for e in data.get("configured") or []}
    return added, configured


class VoteKicks(CommandsBase):
    def __init__(self, ctx: PartContext) -> None:
        super().__init__(ctx)
        self._no_list: set[str] = set()  # servers whose program has no immune list
        self._failed: set[tuple[str, str]] = set()  # (server, ID) the server wouldn't add, reported once
        self._dry: dict[str, tuple[list[str], list[str]]] = {}  # what a dry run last said it would do
        self._told: dict[tuple[str, str], float] = {}  # (server, caller ID) -> when Vergil last explained

    # Keeping the list in step -------------------------------------------------------------

    @on("rcon.connected")
    async def immune_on_connect(self, event: Event) -> None:
        if event.server:
            await self._sync(event.server, time.monotonic() + BUDGET)

    @on("supervisor.commands_reloaded")
    async def immune_on_reload(self, event: Event) -> None:
        await self._sync_all()

    @every(minutes=5)
    async def immune_check(self) -> None:
        await self._sync_all()

    async def _sync_all(self) -> None:
        deadline = time.monotonic() + BUDGET
        for name, state in list(self.ctx.servers.items()):
            if state.connected and time.monotonic() < deadline:  # one at a time (some may still share a list)
                await self._sync(name, deadline)

    async def _sync(self, server: str, deadline: float) -> None:
        """Everyone commands.toml lists onto this server's immune list; off it, whom Vergil put there and it no
        longer lists."""
        state = self.ctx.servers.get(server)
        if state is None or not state.connected:
            return
        client = self.ctx.rcon(server)
        try:
            reply = await client.command("immune", "list", timeout=TIMEOUT)
        except RconError as error:
            self.log.debug("[%s] vote-kick immune list not read: %s", server, error)
            return
        if not reply.ok:
            if reply.text.lower().startswith("unknown command"):
                if server not in self._no_list:
                    self._no_list.add(server)
                    self.log.warning("[%s] this server program has no vote-kick immune list, so Vergil cancels "
                                     "votes to kick members, moderators and owners as they're called. A newer "
                                     "server program refuses them outright", server)
            else:
                self.log.warning("[%s] couldn't read the vote-kick immune list: %s", server, reply.text)
            return
        self._no_list.discard(server)
        wanted = self.ctx.permissions.listed()
        added, configured = _entries(reply.data)
        to_add = sorted(wanted - set(added) - configured)
        to_remove = sorted(pid for pid, by in added.items() if by == OURS and pid not in wanted)
        if not to_add and not to_remove:
            self._dry.pop(server, None)
            return
        if self.ctx.dry_run:
            if self._dry.get(server) != (to_add, to_remove):
                self._dry[server] = (to_add, to_remove)
                self.log.info("[%s] Dry run: would make %s vote-kick immune%s", server, self._who(to_add) or "nobody",
                              f" and take {self._who(to_remove)} off the list" if to_remove else "")
            return
        made, freed = [], []
        for pid in to_add:
            if time.monotonic() >= deadline:
                break
            try:
                reply = await client.command("immune", "add", pid, by=MARK, timeout=TIMEOUT)
            except RconError as error:
                self.log.debug("[%s] immune add stopped: %s", server, error)
                break
            if reply.ok:
                made.append(pid)
                self._failed.discard((server, pid))
            elif (server, pid) not in self._failed:
                self._failed.add((server, pid))
                self.log.warning("[%s] couldn't make %s vote-kick immune: %s", server, self._who([pid]), reply.text)
        for pid in to_remove:
            if time.monotonic() >= deadline:
                break
            try:
                reply = await client.command("immune", "remove", pid, by=MARK, timeout=TIMEOUT)
            except RconError as error:
                self.log.debug("[%s] immune remove stopped: %s", server, error)
                break
            if reply.ok:  # not ok: another server sharing the list took them off first
                freed.append(pid)
        if made:
            self.log.info("[%s] %s can't be vote-kicked now (listed in commands.toml)", server, self._who(made))
        if freed:
            self.log.info("[%s] %s can be vote-kicked again (no longer in commands.toml)", server, self._who(freed))

    def _who(self, ids: list[str]) -> str:
        """'Alice, Bravo and 3 more', by the names Vergil has seen them under, else their IDs' start."""
        names = []
        for pid in ids:
            record = self.ctx.players.get(pid)
            names.append(record.last_name if record and record.last_name else pid[:8])
        if len(names) > 3:
            names = [*names[:3], f"{len(names) - 3} more"]
        return f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1 else "".join(names)

    # A vote on one of them -----------------------------------------------------------------

    @on("log.vote_refused")
    async def vote_refused(self, event: Event) -> None:
        """The server turned down a vote to kick someone immune: say so on the dashboard, and tell the caller why."""
        match = IMMUNE.fullmatch(str(event.get("reason") or "").strip())
        server = event.server or ""
        state = self.ctx.servers.get(server)
        if match is None or state is None:
            return
        caller, name = str(event.get("caller") or "?"), match["name"]
        targets = _named(state, name)
        role = self.ctx.role_of(server, targets[0]) if len(targets) == 1 else "player"
        self.log.info("[%s] %s tried to vote-kick %s%s; the server refused", server, caller, name,
                      f", {ARTICLE[role]} here" if role in ARTICLE else "")
        callers = _named(state, caller)
        if role not in ARTICLE or len(callers) != 1:
            return
        key, now = (server, callers[0].id), time.monotonic()
        if now - self._told.get(key, -TELL_GAP) < TELL_GAP:
            return
        self._told = {k: t for k, t in self._told.items() if now - t < TELL_GAP}
        self._told[key] = now
        try:
            await self.ctx.tell(server, callers[0], callout("immune", f"{targets[0].name} is {ARTICLE[role]} here, "
                                                                      "so no vote can kick them. If they're breaking "
                                                                      "the rules, tell the host."))
        except RconError:
            pass

    @on("rcon.vote")
    async def vote_called(self, event: Event) -> None:
        """A vote to kick someone immune that the server let through: cancel it, and say why."""
        subject = str(event.get("subject") or "")
        server = event.server or ""
        state = self.ctx.servers.get(server)
        if event.get("state") != "called" or not subject.lower().startswith("kick ") or state is None:
            return
        targets = _named(state, subject[5:])
        roles = [self.ctx.role_of(server, p) for p in targets]
        if not targets or any(role not in ARTICLE for role in roles):  # nobody, or someone it could rightly mean
            return
        target, role, caller = targets[0], roles[0], event.get("caller") or "someone"
        if self.ctx.dry_run:
            self.log.info("[%s] Dry run: would cancel %s's vote to kick %s, %s here", server, caller, target.name,
                          ARTICLE[role])
            return
        try:
            reply = await self.ctx.rcon(server).command("cancelvote", by=MARK, timeout=TIMEOUT)
        except RconError as error:
            self.log.warning("[%s] couldn't cancel the vote to kick %s: %s", server, target.name, error)
            return
        if not reply.ok:
            self.log.warning("[%s] couldn't cancel the vote to kick %s: %s", server, target.name, reply.text)
            return
        self.log.warning("[%s] cancelled %s's vote to kick %s, %s here", server, caller, target.name, ARTICLE[role])
        try:
            await self.ctx.say(server, callout("immune", f"{target.name} is {ARTICLE[role]} here: no vote can kick "
                                                         "them."))
        except RconError:
            pass
        await self._sync(server, time.monotonic() + BUDGET)  # the list wasn't in step, or the server has none
