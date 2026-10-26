"""Who's been kicked and banned, as lists to act on: !bans and !kicks number their entries @1, @2…, so !unban @2 and
!pardon @1 lift exactly the one picked, never another player who shares the name. !unban and !pardon given a name
look in the same lists, and ask when it's on more than one entry.

The kicks are every kick the servers announce (`rcon.kick`: from the console, the relay, an admin in game, a vote),
kept while the server keeps them out (at most two hours) and since Huragok started; a pardon takes one off.

What the servers announce goes in the ledger too (history.py), for !whois.
"""

from __future__ import annotations

import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from ...core.events import Event
from ...core.part import command, on
from ...rcon import ban_list
from ..router import CommandCall
from ..targets import FULL_ID, PICK, Candidate, ago, ask, offer, plain
from ..who import from_console, who
from .base import CommandsBase

KICKS_KEPT = 7200  # seconds: the server's longest kick block
LISTED = 10  # entries in one !bans or !kicks answer


@dataclass(frozen=True)
class Kick:
    time: float
    server: str
    id: str
    name: str
    by: str
    reason: str
    seconds: float  # how long the server keeps them out
    through_console: bool = False  # a ! command's, Vergil's, the relay's (the server names who "Alice (RCON)")

    def candidate(self, now: float, here: str = "") -> Candidate:
        left = self.time + self.seconds - now
        how = "" if self.through_console or self.by in ("a vote", "the server") else " in game"
        where = f" on {self.server}" if here and self.server != here else ""
        note = f"kicked {ago(now - self.time)} ago{where} by {self.by}{how}" + (f": {self.reason}" if self.reason
                                                                                 else "")
        note += f"; out {ago(left)} more" if left > 0 else "; can come back"
        return Candidate(self.id, self.name, note=note)


def ban_note(entry: dict[str, Any], now: float) -> str:
    at = entry.get("banned_at")
    at = at / 1000 if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 1e11 else at
    note = "banned" + (f" {ago(now - at)} ago" if isinstance(at, (int, float)) and at else "")
    note += f" by {who(entry.get('banned_by'))}" if entry.get("banned_by") else ""
    note += f": {entry['reason']}" if entry.get("reason") else ""
    expires = entry.get("expires")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
        expires = expires / 1000 if expires > 1e11 else expires
        note += f"; {ago(expires - now)} left" if expires > now else "; running out"
    return note


class ListCommands(CommandsBase):
    # The bans ------------------------------------------------------------------------------------------------------

    async def _ban_data(self, call: CommandCall) -> list[dict[str, Any]] | None:
        """This server's ban list as the console gives it, newest first: each player, and each address banned on its
        own (an address linked to a player's ban goes with theirs). None after telling the caller it couldn't be
        read."""
        entries, error = await self._read_bans(call.server)
        if entries is None:
            await call.reply(f"The ban list couldn't be read: {error}")
        return entries

    async def _read_bans(self, server: str) -> tuple[list[dict[str, Any]] | None, str]:
        """A server's ban list, newest first (_ban_data), or None and why it couldn't be read."""
        data, why = await ban_list(self.ctx.rcon(server))
        if data is None:
            return None, why
        players = [e for e in data["players"] if e.get("id")]
        linked = {e.get("group") for e in players if e.get("group") is not None}
        ips = [e for e in data["ips"] if e.get("ip") and (e.get("group") is None or e.get("group") not in linked)]

        def at(entry: dict[str, Any]) -> float:
            value = entry.get("banned_at")
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

        return sorted(players + ips, key=at, reverse=True), ""

    def _on_every_server(self) -> set[str]:
        """The player IDs and addresses on Huragok's own list (the bans it puts on every server's); none while
        [automod] across_servers is off (then nothing is put on another server)."""
        if not self.ctx.part_option("automod", "across_servers", True):
            return set()
        try:
            rows = self.ctx.db.query("SELECT target FROM commands_every_server WHERE kind = 'ban' AND lifted IS NULL "
                                     "AND (expires IS NULL OR expires > ?)", (time.time(),))
        except sqlite3.Error:
            return set()
        return {str(r["target"]) for r in rows}

    async def _ban_entries(self, call: CommandCall) -> list[Candidate] | None:
        """This server's bans as a list to pick from, each saying whether it's on every server (Huragok's list) or
        this one's alone (a /ban, until !banall)."""
        data = await self._ban_data(call)
        if data is None:
            return None
        now, everywhere = time.time(), self._on_every_server()

        def where(entry: dict[str, Any]) -> str:
            return "; every server" if str(entry.get("id") or entry.get("ip") or "").lower() in everywhere \
                else "; this server"

        return [Candidate(str(e["id"]).lower(), str(e.get("name") or str(e["id"])[:8]), note=ban_note(e, now) + where(e))
                if e.get("id") else Candidate("", str(e["ip"]), note=ban_note(e, now) + where(e), target=str(e["ip"]))
                for e in data]

    @command("bans", usage="[name]", help="The bans in force, newest first, with who and why; @numbers for !unban",
             role="admin")
    async def bans(self, call: CommandCall) -> None:
        entries = await self._ban_entries(call)
        if entries is None:
            return
        if call.text.strip():
            entries = [c for c in entries if plain(call.text) in plain(c.name)]
        if not entries:
            await call.reply("Nobody is banned" + (f" with '{call.text.strip()}' in their name." if call.text.strip()
                                                   else " on this server's list."))
            return
        lines = await offer(call, entries[:LISTED])
        more = (f"…and {len(entries) - LISTED} more: {call.prefix}bans <name> narrows it."
                if len(entries) > LISTED else "")
        await call.reply("\n".join([*lines, more, f"{call.prefix}unban @number lifts one; {call.prefix}banall @number "
                                                  "puts one on every server."]))

    @command("unban", usage="<@number | name | player ID | IP>", help="Lift a ban with everything linked to it",
             role="admin")
    async def unban(self, call: CommandCall) -> None:
        text = call.text.strip()
        if not text:
            await call.reply(f"Use: {call.prefix}unban <@number from {call.prefix}bans | name | player ID | IP>")
            return
        target = await self._listed(call, text, self._ban_entries)
        if not target:
            return
        action = await self.ctx.moderation.unban(call.server, target, by=call.player.name)
        await self._done(call, action)
        # Huragok's ban comes off every server; another server's own /ban stays that server's: said where.
        if action.dry_run:
            return
        still, silent = await self._banned_elsewhere(call.server, target)
        if still:
            await call.reply("Still banned on " + "; ".join(still) + f": a /ban is that server's own; {call.prefix}"
                             "unban on that server lifts it.")
        if silent:
            await call.reply(f"{', '.join(silent)} didn't answer, so a /ban of their own there isn't known (Huragok's "
                             "own ban comes off there as each answers).")

    async def _listed(self, call: CommandCall, text: str, entries_of: Any) -> str:
        """What to send for `text`: an @number's entry (its player ID, or an address), the one entry with that name,
        or the text itself for the console to match (an ID, an address, a name on none of the entries). "" after
        asking the caller to pick (several entries with that name) or telling them why not."""
        if pick := PICK.fullmatch(text):
            got = call.picks.get(call.player.id, int(pick[1])) if call.picks is not None else "No list to pick from."
            if isinstance(got, str):
                await call.reply(got)
                return ""
            return got.target or got.id
        if FULL_ID.fullmatch(text):
            return text.lower()
        entries = await entries_of(call)
        named = [c for c in entries or [] if plain(c.name) == plain(text)]
        if len(named) > 1:
            await ask(call, f"'{text}'", named)
            return ""
        return (named[0].target or named[0].id) if named else text

    # The kicks -----------------------------------------------------------------------------------------------------

    def _kicks_list(self) -> deque[Kick]:
        if not hasattr(self, "_kicks_seen"):
            self._kicks_seen: deque[Kick] = deque(maxlen=500)
        return self._kicks_seen

    @on("rcon.kick")
    async def _kicks_record(self, event: Event) -> None:
        player_id = str(event.get("id") or "").lower()
        if not player_id or not event.server:
            return
        if self._across_echo(event.server, "kick", player_id):
            return  # a kick on another server, passed on here (across.py): the one there is the one that counts
        seconds = event.get("seconds")
        seconds = float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else 600.0
        by = str(event.get("by") or "").strip()
        console = from_console(by, self.ctx.servers, event.server)
        name = str(event.get("name") or player_id[:8])
        self._kicks_list().append(Kick(time.time(), event.server, player_id, name, who(by),
                                       str(event.get("reason") or ""), seconds, console))

    @on("moderation.action")
    async def _kicks_pardoned(self, event: Event) -> None:
        """A pardon lets them back: off the list (Vergil's name kicks are pardoned at once, so they never stay)."""
        if event.get("kind") != "pardon" or event.get("dry_run") or not event.get("ok"):
            return
        target = str(event.get("target_id") or "").lower()
        kicks = self._kicks_list()
        for kick in [k for k in kicks if k.server == event.server and target and k.id == target]:
            kicks.remove(kick)

    def recent_kicks(self, server: str | None = None) -> list[Kick]:
        """Kicks still keeping someone out (or within the last two hours), newest first."""
        now = time.time()
        return [k for k in reversed(self._kicks_list())
                if now - k.time < KICKS_KEPT and (server is None or k.server == server)]

    async def _kick_entries(self, call: CommandCall) -> list[Candidate]:
        """This server's kicks, and the ones through Huragok on another server keeping them off this one too."""
        now = time.time()
        across = bool(self.ctx.part_option("automod", "across_servers", True))
        elsewhere = [k for k in self.recent_kicks() if across and k.server != call.server and k.through_console
                     and k.time + k.seconds > now]
        return [k.candidate(now, call.server) for k in self.recent_kicks(call.server) + elsewhere]

    @command("kicks", help="Who's been kicked lately (here, or off every server by a ! kick), with who and why; "
                           "@numbers for !pardon", role="moderator")
    async def kicks(self, call: CommandCall) -> None:
        entries = await self._kick_entries(call)
        if not entries:
            await call.reply("Nobody has been kicked here, or is kept off it by a kick on another server, in the last "
                             "two hours (since Huragok started).")
            return
        lines = await offer(call, entries[:LISTED])
        await call.reply("\n".join([*lines, f"{call.prefix}pardon @number lets one back in now."]))

    @command("pardon", usage="<@number | name | player ID | IP>",
             help="Let a kicked player back in now (a ! kick's: on every server)", role="moderator")
    async def pardon(self, call: CommandCall) -> None:
        text = call.text.strip()
        if not text:
            await call.reply(f"Use: {call.prefix}pardon <@number from {call.prefix}kicks | name they were kicked "
                             "under | player ID | IP>")
            return
        target = await self._listed(call, text, self._kick_entries)
        if not target:
            return
        # A kick through Huragok keeps them off every server: pardoned where it was given, wherever this is typed.
        lifted = await self._pardon_elsewhere(call.server, target, call.player.name)
        now = time.time()
        here = any(k.time + k.seconds > now and (k.id == target.lower() or plain(k.name) == plain(target))
                   for k in self.recent_kicks(call.server))
        if lifted and not here:
            await call.reply(f"Pardoned on {', '.join(lifted)}, where the kick was given: they can join every server "
                             "again.")
            return
        await self._done(call, await self.ctx.moderation.pardon(call.server, target, by=call.player.name))
        if lifted:
            await call.reply(f"Also pardoned on {', '.join(lifted)}, where a kick kept them off every server.")
