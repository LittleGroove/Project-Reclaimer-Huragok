"""One player, every server: what Huragok does on one server reaches the others ([automod] across_servers, on by
default; mutes follow players through Vergil's list, in automod).

Every server keeps a ban list of its own (a /ban in game is that server's; repair.own_bans splits the shared one
servers had up to 0.4.4), and Huragok keeps its own list (commands_every_server): the bans it gives and the admins it
makes. Those are put on every server's list as they're given, again as each server's console connects and every ten
minutes (a server added since, or one whose list was reset), and on a player who gets in anyway: on every server but
the one it was given on, whose own list rules there. One taken off Huragok's list is taken off every server's, a
server that was down meanwhile as it connects again (only the entry Huragok put there: a /ban since stands). !banall
puts a ban from one server's list (a /ban) on Huragok's.

Only Huragok's own actions go across: its ! commands, Vergil, and anything else sent through the console (the relay,
the window's console). The server says which those are: it names who acted as "Alice (RCON)" (or "RCON"). The game's
own / commands and scoreboard buttons ("Alice", an admin in game) stay on the server they were used on, as do a
vote's kicks and the server's own: the host's call (24 October): / for one server, ! for all of them.

- A kick through Huragok keeps the player off every server until the kick's time is up: on another server now,
  they're kicked there too; joining another meanwhile, they're kicked as they join (and let straight back in, as
  Vergil's name kicks are, so the server's own kick count isn't run up), with where and why. A pardon through
  Huragok (!pardon on any server, the window's, the relay's) lets them back on every server.
- An admin made or unmade through the console some other way than !admin (the window, the relay) is every
  server's too, as !admin's are.
- A ban through Huragok reaches every server that keeps a ban list of its own: servers still sharing one list
  (until their next start) have it already. So does an unban. A ban or unban sent here isn't sent on again.
"""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from pathlib import Path
from typing import Any

from ...core.background import background
from ...core.events import Event
from ...core.part import command, every, on
from ...core.text import is_player_id
from ...program.settings import path_setting
from ...rcon import RconError, ban_list
from ...servers.state import Player
from ..outbox import ASK_SECONDS, results
from ..router import CommandCall
from ..targets import FULL_ID, ago, outranked, plain
from ..who import from_console, through_console, who
from .base import CommandsBase
from .lists import Kick

# How long something this part passed on to a server waits for that server's own event for it (its echo). An echo
# is matched to what was sent, one for one, not to a moment: a server frozen with its console still connected works
# through what it held once it thaws, and its events come late (the simulation, 26 October: 60 s late, past the 15 s
# this used to be, the ban was taken for a new one, put on every server again and an !unban given meanwhile undone).
# A server frozen past [keepalive] hang_seconds (180 s) is restarted, which drops what it held: 15 minutes is ample.
ECHO_SECONDS = 900.0
DECIDED_KEPT = 500  # events whose echo was decided: several handlers ask about one event, and get one answer
UNBAN_RETRY_SECONDS = 30.0  # an unban for a server whose list didn't answer: asked again this often…
UNBAN_RETRIES = 20  # …this many times (10 minutes), from its queue (behind whatever it holds)
SHARED_RECORD = "shared-ban-lists.json"  # records/: the shared lists repair.own_bans gave servers a copy of
LIFTS_KEPT = 30 * 86400.0  # a server down this long after a ban came off Huragok's list keeps it (then: !unban there)


class AcrossServers(CommandsBase):
    def _across(self) -> bool:
        return bool(self.ctx.part_option("automod", "across_servers", True))

    def _across_sent(self) -> dict[tuple[str, str, str], list[float]]:
        """(server, kind, player ID or address) -> when this part sent each still waiting for its echo there."""
        if not hasattr(self, "_across_echoes"):
            self._across_echoes: dict[tuple[str, str, str], list[float]] = {}
        return self._across_echoes

    def _across_decided(self) -> dict[tuple[Any, ...], bool]:
        if not hasattr(self, "_across_decisions"):
            self._across_decisions: dict[tuple[Any, ...], bool] = {}
        return self._across_decisions

    def _across_echo(self, server: str, kind: str, target: str, event: Event | None = None) -> bool:
        """Whether `event` is `server`'s own answer to something this part passed on to it. The first event for a
        pass-on uses it up, so a later one (someone banning the same player there again) is never taken for one;
        every handler asking about the same event gets the same answer. Without an event: whether something passed
        on there still waits for its answer."""
        key = (server, kind, target.lower())
        if event is not None:
            decided = self._across_decided().get((*key, event.name, event.time))
            if decided is not None:
                return decided
        now = time.monotonic()
        waiting = self._across_sent().get(key)
        if waiting:
            waiting[:] = [t for t in waiting if now - t < ECHO_SECONDS]
        echo = bool(waiting)
        if event is not None:
            if echo and waiting:
                waiting.pop(0)
            decisions = self._across_decided()
            decisions[(*key, event.name, event.time)] = echo
            while len(decisions) > DECIDED_KEPT:
                decisions.pop(next(iter(decisions)))
        return echo

    def _across_mark(self, server: str, kind: str, target: str) -> None:
        self._across_sent().setdefault((server, kind, target.lower()), []).append(time.monotonic())

    def _across_unmark(self, server: str, kind: str, target: str) -> None:
        """What was passed on was refused (the server answered no): no echo will come for it."""
        waiting = self._across_sent().get((server, kind, target.lower()))
        if waiting:
            waiting.pop()

    # Kicks -----------------------------------------------------------------------------------------------------

    def _across_block(self, player_id: str, server: str) -> Any:
        """The kick on another server still keeping this player out, or None."""
        now = time.time()
        return next((k for k in self.recent_kicks() if k.id == player_id and k.server != server
                     and k.through_console and k.time + k.seconds > now
                     and not self._across_echo(k.server, "kick", k.id)), None)

    async def _across_kick(self, server: str, player: Any, kick: Any) -> None:
        left = ago(kick.time + kick.seconds - time.time())
        reason = (f"Kicked from {kick.server} by {kick.by}" + (f" ({kick.reason})" if kick.reason else "")
                  + f": you can join again in {left}.")
        self._across_mark(server, "kick", player.id)
        moderation = self.ctx.moderation
        action = await moderation.kick(server, player, reason, by=kick.by, evidence=f"kicked on {kick.server}",
                                       live=True)
        if action.ok and not action.dry_run:
            await moderation.pardon(server, player.id, by=kick.by, live=True,  # their block is the other server's
                                    evidence=f"kicked on {kick.server}")
        self.ctx.log.info("[%s] %s kicked here too: kicked on %s by %s, %s left%s", server, player.name, kick.server,
                          kick.by, left, "" if action.ok else f" (FAILED: {action.text})")

    @on("rcon.kick")
    async def _across_kicked(self, event: Event) -> None:
        """A kick on one server: off every other one they're on now."""
        player_id = str(event.get("id") or "").lower()
        if not player_id or not event.server or not self._across() or self._across_echo(event.server, "kick", player_id, event):
            return
        if not from_console(event.get("by"), self.ctx.servers, event.server):
            return  # the game's own /kick, a vote's, the server's: this server's alone
        seconds = event.get("seconds")
        kick = Kick(time.time(), event.server, player_id, str(event.get("name") or player_id[:8]), who(event.get("by")),
                    str(event.get("reason") or ""),
                    float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else 600.0,
                    True)
        for server, state in list(self.ctx.servers.items()):
            if server != event.server and (player := state.player_by_id(player_id)):
                self.ctx.outbox.send(server, f"{player.name}'s kick (given on {event.server})",
                                     lambda server=server, player=player: self._across_kick(server, player, kick))

    @on("rcon.join")
    async def _across_joined(self, event: Event) -> None:
        player_id = str(event.get("id") or "").lower()
        if not player_id or not event.server or not self._across():
            return
        if (kick := self._across_block(player_id, event.server)) is None:
            return
        player = self.ctx.server(event.server).player_by_id(player_id)
        if player is not None and self.ctx.role_of(event.server, player) not in ("admin", "owner"):
            await self._across_kick(event.server, player, kick)

    # Huragok's own list: its bans (and the admins it made) on every server's ---------------------------------------

    def every_server(self, kind: str, target: str | None = None) -> list[Any]:
        """What's on Huragok's list and in force (not taken off, not run out): its bans, or the admins it made."""
        now = time.time()
        sql = ("SELECT * FROM commands_every_server WHERE kind = ? AND lifted IS NULL AND (expires IS NULL OR "
               "expires > ?)")
        params: list[Any] = [kind, now]
        if target is not None:
            sql += " AND target = ?"
            params.append(target.lower())
        return self.ctx.db.query(sql + " ORDER BY time", params)

    def add_every_server(self, kind: str, target: str, *, name: str, reason: str, by: str, server: str,
                         expires: float | None) -> bool:
        """On Huragok's list. One in force already is kept (a later end, or none, lengthens it): False."""
        target = target.lower()
        if existing := self.every_server(kind, target):
            row = existing[-1]
            if row["expires"] is not None and (expires is None or expires > row["expires"]):
                self.ctx.db.execute("UPDATE commands_every_server SET expires = ? WHERE number = ?",
                                    (expires, row["number"]))
            return False
        self.ctx.db.execute(
            "INSERT INTO commands_every_server (time, kind, target, name, reason, by, server, expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (time.time(), kind, target, name, reason, by, server, expires))
        return True

    def lift_every_server(self, kind: str, target: str, by: str) -> int:
        """Off Huragok's list (by player ID or address, or the name it was given under): how many."""
        return self.ctx.db.execute(
            "UPDATE commands_every_server SET lifted = ?, lifted_by = ? WHERE kind = ? AND lifted IS NULL AND "
            "(target = ? OR casefold(name) = ?)", (time.time(), by, kind, target.lower(), target.casefold())).rowcount

    def _ban_list(self, server: str) -> Path | None:
        """The ban list file a server uses (dedicated.toml's bans, beside it), resolved: two servers with the same
        one share their bans (until the next start gives each its own, repair.own_bans)."""
        try:
            config = self.ctx.server_option(server, "config_path")
            folder = self.ctx.server_option(server, "folder")
            if config is None or folder is None:
                return None
            data = tomllib.loads(Path(config).read_text(encoding="utf-8"))
            return path_setting(Path(folder), data, "bans")
        except (OSError, ValueError):
            return None

    def _own_lists(self, server: str) -> list[str]:
        """The other servers, connected now, whose ban list isn't this one's (one that isn't connected gets Huragok's
        list as it connects: _every_server_check)."""
        here = self._ban_list(server)
        return [other for other, state in self.ctx.servers.items() if other != server and state.connected
                and (here is None or self._ban_list(other) != here)]

    def _lifted_lately(self, kind: str) -> dict[str, float]:
        """Taken off Huragok's list within LIFTS_KEPT and not back on it: target -> when it came off."""
        rows = self.ctx.db.query("SELECT target, MAX(lifted) AS lifted FROM commands_every_server WHERE kind = ? AND "
                                 "lifted > ? GROUP BY target", (kind, time.time() - LIFTS_KEPT))
        on = {row["target"] for row in self.every_server(kind)}
        return {row["target"]: float(row["lifted"]) for row in rows if row["target"] not in on}

    async def _put_ban(self, server: str, row: Any) -> bool:
        """One ban of Huragok's list onto a server's own, for the time it has left, as its giver gave it."""
        args = [row["target"]]
        if row["expires"] is not None:
            left = row["expires"] - time.time()
            if left <= 0:
                return False
            args.append(f"{max(1, round(left))}s")
        args.append(row["reason"] or (f"banned on {row['server']}" if row["server"] else "banned before 0.5.0"))
        self._across_mark(server, "ban", row["target"])
        try:
            reply = await self.ctx.rcon(server).command("ban", *args, by=row["by"] or None)
            ok, text = reply.ok, reply.text
            if not ok:
                self._across_unmark(server, "ban", row["target"])  # refused: no echo comes
        except RconError as error:  # no answer: a server frozen with its console up may still do it, later
            ok, text = False, str(error)
        self.ctx.log.info("[%s] %s's ban (by %s on %s) put on this server's list%s", server, row["name"] or
                          row["target"][:8], row["by"], row["server"] or "a shared list, before 0.5.0",
                          "" if ok else f": FAILED: {text}")
        return ok

    @on("rcon.ban")
    async def _across_banned(self, event: Event) -> None:
        """A ban through Huragok (a ! command, Vergil, the relay): on Huragok's list, and every server's."""
        target = str(event.get("id") or event.get("ip") or "").lower()
        if not target or not event.server or not self._across() or self._across_echo(event.server, "ban", target, event):
            return
        if not from_console(event.get("by"), self.ctx.servers, event.server):
            return  # the game's own /ban: this server's alone (!banall makes it every server's)
        expires = event.get("expires")
        until: float | None = None
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            expires = expires / 1000 if expires > 1e11 else expires
            until = float(expires) if expires > 1e9 else time.time() + float(expires)
            if until <= time.time():
                return
        self.add_every_server("ban", target, name=str(event.get("name") or event.get("ip") or target[:8]),
                              reason=str(event.get("reason") or ""), by=who(event.get("by")), server=event.server,
                              expires=until)
        row = self.every_server("ban", target)[-1]
        for server in self._own_lists(event.server):
            self.ctx.outbox.send(server, f"{row['name'] or target[:8]}'s ban (given on {event.server})",
                                 lambda server=server: self._put_ban(server, row))

    @on("rcon.unban")
    async def _across_unbanned(self, event: Event) -> None:
        """An unban through Huragok lifts Huragok's ban everywhere: off its list, and off every server's. Another
        server's own /ban of them stays (the host's call, 25 October: !unban says where)."""
        target = str(event.get("target") or "").strip()
        if not target or not event.server or self._across_echo(event.server, "unban", target, event):
            return
        if not from_console(event.get("by"), self.ctx.servers, event.server):
            return
        by = who(event.get("by"))
        self.lift_every_server("ban", target, by)
        if not self._across():
            return
        for server in self._own_lists(event.server):
            self.ctx.outbox.send(server, f"the unban of {target} (given on {event.server})",
                                 lambda server=server: self._unban_on(server, target, event.server, by))

    async def _unban_on(self, server: str, target: str, origin: str, by: str, tries: int = 0) -> bool:
        """An unban through Huragok on `origin`, passed on to `server`: only a ban Huragok put there. A list that
        doesn't answer is asked again from the server's queue (behind whatever it holds) until it does: a server
        frozen with its console up works through a ban it held once it thaws, after the unban was given, and that
        ban mustn't stand (the simulation, 26 October). Asking stops once Huragok bans them again meanwhile."""
        entries, why = await self._read_bans(server)
        if entries is None:
            if tries < UNBAN_RETRIES and self.ctx.server(server).connected:
                self.ctx.log.info("[%s] its ban list didn't answer (%s): the unban of %s is tried again in %.0f s",
                                  server, why, target, UNBAN_RETRY_SECONDS)

                async def again() -> None:
                    await asyncio.sleep(UNBAN_RETRY_SECONDS)
                    if self.every_server("ban", target):
                        return  # banned again through Huragok since: that ban stands
                    self.ctx.outbox.send(server, f"the unban of {target} (given on {origin}), again",
                                         lambda: self._unban_on(server, target, origin, by, tries + 1))

                background(again(), f"[{server}] the unban of {target} asked again", self.ctx.log)
            return False
        mine = [e for e in entries if self._names(e, target) and through_console(e.get("banned_by"))]
        if not mine:
            return False  # not banned there, or a /ban there (that server's)
        self._across_mark(server, "unban", target)
        try:
            reply = await self.ctx.rcon(server).command("unban", target, by=by)
            ok, text = reply.ok, reply.text
            if not ok:
                self._across_unmark(server, "unban", target)
        except RconError as error:
            ok, text = False, str(error)
        self.ctx.log.info("[%s] unban of %s on %s by %s passed on here%s", server, target, origin, by,
                          "" if ok else f": {text}")
        return ok

    @staticmethod
    def _names(entry: dict[str, Any], target: str) -> bool:
        """Whether a ban list's entry is for this target: a player ID, an address, or the name it's under."""
        target = target.strip()
        return str(entry.get("id") or entry.get("ip") or "").lower() == target.lower() \
            or (bool(entry.get("name")) and plain(str(entry["name"])) == plain(target))

    async def _banned_elsewhere(self, server: str, target: str) -> tuple[list[str], list[str]]:
        """The other servers' own bans of this target (a /ban there): "Other by LilGremlin (/ban: spam)", and the
        servers whose list didn't answer. The lists are read side by side, ASK_SECONDS at most: one that doesn't
        answer by then isn't waited on."""
        said: list[str] = []
        silent: list[str] = []
        others = [other for other, state in self.ctx.servers.items() if other != server and state.connected]
        reads = {other: asyncio.ensure_future(self._read_bans(other)) for other in others}
        if reads:
            await asyncio.wait(list(reads.values()), timeout=ASK_SECONDS)
        for other, read in reads.items():
            if not read.done() or read.cancelled() or read.exception() is not None:
                read.cancel()
                silent.append(other)
                continue
            entries, _ = read.result()
            if entries is None:  # it answered that it couldn't, or its console timed out: unknown all the same
                silent.append(other)
                continue
            for entry in entries:
                if self._names(entry, target) and not through_console(entry.get("banned_by")):
                    reason = str(entry.get("reason") or "")
                    said.append(f"{other} by {who(entry.get('banned_by'))} (/ban" + (f": {reason}" if reason else "")
                                + ")")
        return said, silent

    # Pardons: a kick through Huragok is every server's, and so is its pardon -----------------------------------

    async def _pardon_elsewhere(self, server: str, target: str, by: str) -> list[str]:
        """The kicks through Huragok on other servers keeping this player (ID, or the name they were kicked under)
        off `server` too: pardoned where they were given and taken off the list. Those servers."""
        if not self._across():
            return []
        now, wanted = time.time(), target.strip()
        kicks = [k for k in self.recent_kicks() if k.server != server and k.through_console
                 and k.time + k.seconds > now and (k.id == wanted.lower() or plain(k.name) == plain(wanted))]
        listed = self._kicks_list()
        done: list[str] = []
        for kick in kicks:
            if kick in listed:
                listed.remove(kick)  # the block on every server ends, whatever the server it came from answers
            if kick.server in done:
                continue
            done.append(kick.server)
            self.ctx.outbox.send(kick.server, f"{kick.name}'s pardon (given on {server})",
                                 lambda kick=kick: self._pardon_on(kick, server, by))
        return done

    async def _pardon_on(self, kick: Any, origin: str, by: str) -> bool:
        self._across_mark(kick.server, "pardon", kick.id)
        action = await self.ctx.moderation.pardon(kick.server, kick.id, by=by)
        self.ctx.log.info("[%s] %s's kick pardoned here too (pardoned by %s on %s)%s", kick.server, kick.name, by,
                          origin, "" if action.ok else f": {action.text}")
        return action.ok

    @on("rcon.pardon")
    async def _across_pardoned(self, event: Event) -> None:
        """A pardon the server announces: off the kick list there; one through Huragok (the window's, the relay's)
        lets them back on every server, as !pardon does."""
        target = str(event.get("id") or event.get("target") or "").strip()
        if not target or not event.server or self._across_echo(event.server, "pardon", target, event):
            return
        listed = self._kicks_list()
        for kick in [k for k in listed if k.server == event.server
                     and (k.id == target.lower() or plain(k.name) == plain(target))]:
            listed.remove(kick)
        if from_console(event.get("by"), self.ctx.servers, event.server):
            await self._pardon_elsewhere(event.server, target, who(event.get("by")))

    # Admins made through the console: every server's, as !admin's are ------------------------------------------

    @on("rcon.admin_added")
    async def _across_admin_added(self, event: Event) -> None:
        await self._across_admin(event, "add")

    @on("rcon.admin_removed")
    async def _across_admin_removed(self, event: Event) -> None:
        await self._across_admin(event, "remove")

    async def _across_admin(self, event: Event, verb: str) -> None:
        """An admin made or unmade through the console some other way than !admin (the window's B screen, the
        relay, a console tool): on Huragok's list or off it, and so on every server's. !admin's own come back as
        events too, marked as sent (roles.py)."""
        target = str(event.get("id") or event.get("target") or "").lower()
        if not FULL_ID.fullmatch(target) or not event.server or not self._across() \
                or not from_console(event.get("by"), self.ctx.servers, event.server) \
                or self._across_echo(event.server, "admin", target, event):
            return
        by = who(event.get("by"))
        if verb == "add":
            seen = self.ctx.players.get(target)
            if not self.add_every_server("admin", target, name=seen.last_name if seen else target[:8], reason="",
                                         by=by, server=event.server, expires=None):
                return
            others = self._own_lists(event.server)
        else:
            self.lift_every_server("admin", target, by)
            others = [o for o in self._own_lists(event.server) if target in (self.ctx.server(o).admins or set())]
        for other in others:
            self.ctx.outbox.send(other, f"admin {verb} {target[:8]} (on {event.server})",
                                 lambda other=other: self._admin_on(other, verb, target, by, event.server))

    async def _admin_on(self, server: str, verb: str, target: str, by: str, origin: str) -> bool:
        """`admin add|remove <ID>` passed on to `server` (the queue's job): how it went."""
        self._across_mark(server, "admin", target)
        try:
            reply = await self.ctx.rcon(server).command("admin", verb, target, by=by)
            ok, text = reply.ok, reply.text
            if not ok:
                self._across_unmark(server, "admin", target)
        except RconError as error:
            ok, text = False, str(error)
        self.ctx.log.info("[%s] admin %s %s (by %s on %s) passed on here%s", server, verb, target[:8], by, origin,
                          "" if ok else f": {text}")
        return ok

    @on("rcon.connected")
    async def _every_server_connected(self, event: Event) -> None:
        if event.server:
            self._check_soon(event.server)

    @every(minutes=10)
    async def _every_server_sweep(self) -> None:
        await self._import_shared_lists()  # one split since (or across_servers turned on)
        for server, state in list(self.ctx.servers.items()):
            if state.connected:
                self._check_soon(server)

    def _check_soon(self, server: str) -> None:
        """_every_server_check in the server's own queue: once at a time (a sweep doesn't stack another on one still
        waiting), and a server slow to answer holds up nothing else."""
        self.ctx.outbox.send(server, "Huragok's list checked against its own", lambda: self._every_server_check(server),
                             key="every-server-check")

    async def _every_server_check(self, server: str) -> None:
        """A server's own list against Huragok's: a ban or an admin it lacks (a server added since, or one that was
        down or whose list was reset) is put on it; one Huragok's list let go of while it was down is taken off."""
        if not self._across():
            return
        bans, admins = self.every_server("ban"), self.every_server("admin")
        unbanned, unmade = self._lifted_lately("ban"), self._lifted_lately("admin")
        if bans or unbanned:
            await self._every_server_bans(server, bans, unbanned)
        state = self.ctx.server(server)
        if admins and state.admins is not None:
            for row in admins:
                if row["target"] not in state.admins:
                    try:
                        self._across_mark(server, "admin", row["target"])
                        await self.ctx.rcon(server).command("admin", "add", row["target"], by=row["by"] or None)
                        self.ctx.log.info("[%s] %s made an admin here too (Huragok's list: made by %s)", server,
                                          row["name"] or row["target"][:8], row["by"])
                    except RconError as error:
                        self.ctx.log.warning("[%s] couldn't make %s an admin here: %s", server, row["target"][:8],
                                             error)
        if unmade and state.admins and unmade.keys() & state.admins:
            await self._every_server_unmade(server, unmade)

    async def _every_server_bans(self, server: str, bans: list[Any], unbanned: dict[str, float]) -> None:
        data, _ = await ban_list(self.ctx.rcon(server))
        if data is None:
            return
        have: dict[str, dict[str, Any]] = {}
        for entry in [*data["players"], *data["ips"]]:
            if target := str(entry.get("id") or entry.get("ip") or "").lower():
                have[target] = entry
        for row in bans:
            if row["target"] not in have and row["server"] != server:  # where it was given, its own list rules
                await self._put_ban(server, row)
        for target, lifted in unbanned.items():
            entry = have.get(target)
            at = entry.get("banned_at") if entry else None
            at = at / 1000 if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 1e11 else at
            # Only the entry Huragok put there (through the console, before it came off): a /ban since stands.
            if entry is None or not through_console(entry.get("banned_by")) or not isinstance(at, (int, float)) \
                    or at > lifted:
                continue
            self._across_mark(server, "unban", target)
            try:
                done = await self.ctx.rcon(server).command("unban", target)
                ok, text = done.ok, done.text
            except RconError as error:
                ok, text = False, str(error)
            self.ctx.log.info("[%s] %s's ban, taken off Huragok's list while this server was away, taken off here%s",
                              server, entry.get("name") or target[:8], "" if ok else f": FAILED: {text}")

    async def _every_server_unmade(self, server: str, unmade: dict[str, float]) -> None:
        """An admin Huragok made, unmade while this server was away: unmade here (one the console added since, or
        dedicated.toml's, stays)."""
        try:
            reply = await self.ctx.rcon(server).command("admin")
        except RconError:
            return
        data = reply.data if reply.ok and isinstance(reply.data, dict) else None
        for entry in (data or {}).get("added") or []:
            if not isinstance(entry, dict):
                continue
            target, at = str(entry.get("id") or "").lower(), entry.get("added_at")
            at = at / 1000 if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 1e11 else at
            if target not in unmade or not through_console(entry.get("added_by")) or not isinstance(at, (int, float)) \
                    or at > unmade[target]:
                continue
            try:
                self._across_mark(server, "admin", target)
                await self.ctx.rcon(server).command("admin", "remove", target)
                self.ctx.log.info("[%s] %s is no longer an admin here either (taken off Huragok's list while this "
                                  "server was away)", server, target[:8])
            except RconError as error:
                self.ctx.log.warning("[%s] couldn't take %s off this server's admins: %s", server, target[:8], error)

    # Bans from before 0.5.0: the servers' shared list, every server's when given -----------------------------

    def _shared_lists(self) -> list[Path]:
        """Ban lists servers shared up to 0.4.4: the root's bans.json (what [setup] share gave new servers), one a
        server still reads from outside its folder, and those repair.own_bans split (records/shared-ban-lists.json)."""
        found = [self.ctx.home / "bans.json"]
        for server in self.ctx.servers:
            folder = self.ctx.server_option(server, "folder")
            path = self._ban_list(server)
            if path is not None and folder is not None and path.parent != Path(folder).resolve():
                found.append(path)
        try:
            recorded = json.loads((self.ctx.home / "records" / SHARED_RECORD).read_text(encoding="utf-8"))
            found += [Path(p) for p in recorded if isinstance(p, str)] if isinstance(recorded, list) else []
        except (OSError, ValueError):
            pass
        unique: dict[str, Path] = {}
        for path in found:
            try:
                unique.setdefault(str(path.resolve()), path.resolve())
            except OSError:
                continue
        return list(unique.values())

    async def _import_shared_lists(self) -> None:
        """The host's call (25 October): bans from before 0.5.0 were every server's when given (the servers shared
        one list), so each shared list's bans go on Huragok's list, once per list: a server added later gets them,
        and !bans says "every server". One taken off Huragok's list later stays off (never imported again)."""
        if not self._across():
            return
        done = {row["path"] for row in self.ctx.db.query("SELECT path FROM commands_shared_lists")}
        for path in self._shared_lists():
            if str(path) in done:
                continue
            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8") if path.is_file() else ""
                data = json.loads(text) if text.strip() else None
            except (OSError, ValueError) as error:
                self.ctx.log.warning("The ban list %s couldn't be read for Huragok's list: %s", path, error)
                continue
            if data is None:
                continue  # not there (a host that never shared): looked at again next time
            count = self._import_bans(data if isinstance(data, dict) else {}, path)
            self.ctx.db.execute("INSERT INTO commands_shared_lists (path, time, bans) VALUES (?, ?, ?)",
                                (str(path), time.time(), count))
            self.ctx.log.info("%d ban(s) from the ban list servers shared before 0.5.0 (%s) put on Huragok's list: "
                              "every server keeps them", count, path)

    def _import_bans(self, data: dict[str, Any], path: Path) -> int:
        now, count = time.time(), 0
        players = [e for e in data.get("players") or [] if isinstance(e, dict) and e.get("id")]
        linked = {e.get("group") for e in players if e.get("group") is not None}
        ips = [e for e in data.get("ips") or [] if isinstance(e, dict) and e.get("ip")
               and (e.get("group") is None or e.get("group") not in linked)]  # one linked to a player's goes with it
        for entry in players + ips:
            expires = entry.get("expires")
            until = None
            if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
                until = expires / 1000 if expires > 1e11 else float(expires)
                if until <= now:
                    continue
            target = str(entry.get("id") or entry.get("ip")).lower()
            name = str(entry.get("name") or entry.get("ip") or target[:8])
            by = who(entry.get("banned_by")) if entry.get("banned_by") else "an admin"
            if self.add_every_server("ban", target, name=name, reason=str(entry.get("reason") or ""), by=by,
                                     server="", expires=until):
                count += 1
        return count

    @on("rcon.join")
    async def _every_server_joined(self, event: Event) -> None:
        """Someone on Huragok's list who got in anyway (this server's list didn't have it yet): banned here now."""
        player_id = str(event.get("id") or "").lower()
        if not player_id or not event.server or not self._across():
            return
        rows = self.every_server("ban", player_id) or (self.every_server("ban", str(event.get("ip") or ""))
                                                       if event.get("ip") else [])
        # Not where it was given: that server's own list rules there (an unban made there some other way stands).
        rows = [r for r in rows if r["server"] != event.server]
        if rows and not self._across_echo(event.server, "ban", player_id):
            await self._put_ban(event.server, rows[-1])

    # !banall: a server's /ban made every server's ----------------------------------------------------------------

    @command("banall", usage="<@number | player | player ID>",
             help="Put a ban from this server's list on every server's (a /ban made like a !ban): @number from !bans",
             role="admin")
    async def banall(self, call: CommandCall) -> None:
        text = call.text.strip()
        if not text:
            await call.reply(f"Use: {call.prefix}banall <@number from {call.prefix}bans | player | player ID>: puts "
                             "that ban on every server's list.")
            return
        if not self._across():
            await call.reply("Huragok's bans stay on their own server ([automod] across_servers is off, in the "
                             "window): there's no every-server list to put it on.")
            return
        entries = await self._ban_data(call)
        if entries is None:
            return
        target = await self._listed(call, text, self._ban_entries)
        if not target:
            return
        entry = next((e for e in entries if str(e.get("id") or e.get("ip") or "").lower() == target.lower()), None)
        if entry is None:  # a player named, not an entry: theirs, if this list has one
            player = await call.find(text, scope="seen")
            if player is None:
                return
            entry = next((e for e in entries if str(e.get("id") or "").lower() == player.id), None)
            if entry is None:
                await call.reply(f"{player.name} isn't banned on this server: {call.prefix}ban bans them on every "
                                 "server.")
                return
        listed_id = str(entry.get("id") or "").lower()
        if is_player_id(listed_id) and (why := outranked(call, Player(0, str(entry.get("name") or listed_id[:8]),
                                                                      listed_id))):
            await call.reply(why)  # as !ban: a /ban on an owner or a fellow admin isn't spread by an admin
            return
        await call.reply(await self._ban_everywhere(call.server, entry, call.player.name))

    async def _ban_everywhere(self, server: str, entry: dict[str, Any], by: str) -> str:
        """One entry of a server's own ban list onto Huragok's, and so every server's: what to tell whoever asked."""
        target = str(entry.get("id") or entry.get("ip")).lower()
        name = str(entry.get("name") or entry.get("ip") or target[:8])
        if self.every_server("ban", target):
            return f"{name}'s ban is on every server already."
        expires = entry.get("expires")
        until = None
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            until = expires / 1000 if expires > 1e11 else float(expires)
        if until is not None and until <= time.time():
            return f"{name}'s ban has run out: there's nothing to put on every server."
        reason = str(entry.get("reason") or "")
        given = who(entry.get("banned_by")) if entry.get("banned_by") else by
        self.add_every_server("ban", target, name=name, reason=reason, by=given, server=server, expires=until)
        row = self.every_server("ban", target)[-1]
        jobs = {other: self.ctx.outbox.send(other, f"{name}'s ban (!banall on {server})",
                                            lambda other=other: self._put_ban(other, row))
                for other in self._own_lists(server)}
        sent, failed, waiting = await results(jobs, ASK_SECONDS)  # the queues carry on past this
        self.ctx.log.info("[%s] %s made %s's ban (by %s) every server's: %s", server, by, name, given,
                          ", ".join(sent) or "no other server has a list of its own yet"
                          if not (failed or waiting) else f"on {', '.join(sent) or 'none yet'}; "
                          f"still to come: {', '.join(failed + waiting)}")
        self.ctx.moderation.record_seen("ban", server, target if is_player_id(target) else None, name, by=by,
                                        source="commands", reason=f"every server: {reason}" if reason else "every server")
        left = "for good" if until is None else f"{ago(until - time.time())} left"
        later = failed + waiting
        return (f"{name}'s ban (by {given}" + (f": {reason}" if reason else "") + f", {left}) is on every server "
                + ("now" if not later else f"as each answers ({', '.join(later)} not yet)")
                + ", and stays on Huragok's list for new ones.")

    async def ban_everywhere(self, server: str, text: str, by: str) -> tuple[bool, str]:
        """The window's B screen: a ban on a server's own list (its player ID, address, or the name it's under) onto
        every server's, as !banall does."""
        text = text.strip()
        if not self._across():
            return False, ("Huragok's bans stay on their own server ([automod] across_servers is off): there's no "
                           "every-server list to put it on.")
        entries, error = await self._read_bans(server)
        if entries is None:
            return False, f"{server}'s ban list couldn't be read: {error}"
        found = [e for e in entries if str(e.get("id") or e.get("ip") or "").lower() == text.lower()]
        if not found:
            found = [e for e in entries if str(e.get("name") or "").casefold() == text.casefold()]
        if not found and len(text) >= 4:  # the start of an ID, as the list shows them
            found = [e for e in entries if str(e.get("id") or "").lower().startswith(text.lower())]
        if not found:
            return False, f"Nothing on {server}'s ban list is '{text}': L lists it (a player ID, an address or a name)."
        if len(found) > 1:
            listed = "; ".join(f"{e.get('name') or '?'} {str(e.get('id') or e.get('ip'))[:8]}" for e in found[:6])
            return False, f"'{text}' could be {listed}: use the player ID."
        return True, await self._ban_everywhere(server, found[0], by)
