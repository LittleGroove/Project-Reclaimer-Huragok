"""A player's record: every kick, mute, ban and warning, however it was given, in Huragok's ledger (`actions`), and
!whois reading it back as a whole rap sheet.

Into the ledger, with the server and who acted:
- Huragok's own as it sends them: a moderator's ! command ("commands"), Vergil's ("automod").
- What the servers announce that Huragok didn't send: an admin's own /kick, /ban (and /mute, if the game gets one) in
  game ("in game"), a vote's kick ("vote"), and anything sent through the console some other way: the relay, the
  window's console, a console tool ("console"). Huragok's own come back as announcements too ("Alice (RCON)"): one
  it sent a moment ago isn't written twice (Moderation.sent_lately).
Huragok acts only on its own; the rest are history (the host's call, 24 October: / is that server's).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from typing import Any

from ...core.events import Event
from ...core.part import command, on
from ...core.text import is_player_id
from ...servers.state import Player
from ..router import CommandCall
from ..targets import ago, ask, named
from ..who import acting_name, through_console, who
from .base import CommandsBase

KINDS_SHOWN = ("ban", "kick", "mute", "warn")  # what a record counts; lifts (unban, unmute, pardon) are listed only
RECENT, MOST = 5, 15  # actions !whois lists, and at most
READ_SECONDS = 4.0  # !whois waits this long at most for a server's ban list
STRIKE_IN = re.compile(r"\(([^()]*strike \d+[^()]*)\)")  # "(slurs, strike 1 of 3)" in Vergil's message


def source_of(by: str, acts_as: str) -> str | None:
    """Where an announced action came from, by who the server says acted: None for one that's no one's act (a
    mute running out, a load timeout)."""
    if by in ("time", "Server", ""):
        return None
    if by == "vote":
        return "vote"
    if through_console(by):
        return "automod" if acting_name(by) == acts_as else "console"
    return "in game"


def _seconds(value: Any) -> float | None:
    """An event's `expires` as seconds from now (Unix seconds or milliseconds, or seconds already); None: no end."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    value = value / 1000 if value > 1e11 else value
    return value - time.time() if value > 1e9 else float(value)


def label(row: Any) -> str:
    """How it was given: !kick, /ban, Vergil, the console ("" for a vote's: "by a vote" says it)."""
    source, kind = row["source"], row["kind"]
    return {"commands": f"!{kind}", "in game": f"/{kind}", "automod": "Vergil", "vote": "",
            "console": f"console {kind}"}.get(source, f"{source} {kind}")


def shown_time(duration: Any) -> str:
    text = str(duration or "")
    if text.endswith("s") and text[:-1].isdecimal():
        return ago(int(text[:-1]))
    return text


class HistoryCommands(CommandsBase):
    # Into the ledger ---------------------------------------------------------------------------------------------

    def _history_seen(self, event: Event, kind: str, *, target: str = "", name: str = "", reason: str = "",
                      duration: str | None = None) -> None:
        by = str(event.get("by") or "").strip()
        source = source_of(by, self.ctx.acts_as)
        if source is None or source == "automod" or not event.server or not target:
            return  # Vergil's own go in as Vergil sends them
        if source == "console" and self.ctx.moderation.sent_lately(event.server, kind, target):
            return  # a ! command's, in the ledger already
        self.ctx.moderation.record_seen(kind, event.server, target if is_player_id(target) else None, name or target[:8],
                                        by=who(by), source=source, reason=reason, duration=duration)

    @on("rcon.kick")
    async def _history_kick(self, event: Event) -> None:
        player_id = str(event.get("id") or "").lower()
        if self._across_echo(event.server, "kick", player_id):
            return  # passed on from another server: that one's on the record
        seconds = event.get("seconds")
        self._history_seen(event, "kick", target=player_id, name=str(event.get("name") or ""),
                           reason=str(event.get("reason") or ""),
                           duration=f"{round(seconds)}s" if isinstance(seconds, (int, float))
                           and not isinstance(seconds, bool) and seconds > 0 else None)

    @on("rcon.ban")
    async def _history_ban(self, event: Event) -> None:
        target = str(event.get("id") or event.get("ip") or "").lower()
        if self._across_echo(event.server, "ban", target):
            return
        seconds = _seconds(event.get("expires"))
        self._history_seen(event, "ban", target=target, name=str(event.get("name") or event.get("ip") or ""),
                           reason=str(event.get("reason") or ""),
                           duration=f"{max(1, round(seconds))}s" if seconds is not None else None)

    @on("rcon.mute")
    async def _history_mute(self, event: Event) -> None:
        seconds = _seconds(event.get("expires"))
        self._history_seen(event, "mute", target=str(event.get("id") or "").lower(), name=str(event.get("name") or ""),
                           reason=str(event.get("reason") or ""),
                           duration=f"{max(1, round(seconds))}s" if seconds is not None else None)

    @on("rcon.unmute")
    async def _history_unmute(self, event: Event) -> None:
        self._history_seen(event, "unmute", target=str(event.get("id") or "").lower(),
                           name=str(event.get("name") or ""))

    @on("rcon.unban")
    async def _history_unban(self, event: Event) -> None:
        target = str(event.get("target") or "").strip()
        self._history_seen(event, "unban", target=target.lower() if is_player_id(target) else target, name=target)

    # The rap sheet ------------------------------------------------------------------------------------------------

    def _whois_count(self, word: str) -> bool:
        return word.isascii() and word.isdecimal()

    def _query(self, sql: str, params: tuple[Any, ...]) -> list[Any]:
        """Another part's table (automod's), when it has one: none yet is nothing to show."""
        try:
            return self.ctx.db.query(sql, params)
        except sqlite3.Error:
            return []

    @command("whois", usage="<player | player ID> [lines]",
             help="A player's record: names, where they are, mutes, kicks, bans and warnings on every server, by ! or "
                  "/ and by whom, and their strikes", role="moderator")
    async def whois(self, call: CommandCall) -> None:
        # All of it is the name, but for a number of lines at its end: "!whois nate hig" is Nate Higgers by its start.
        # Someone who left too, by a name Vergil has seen them use: the one to look up is often gone already. Looking
        # is harmless, so a name's start will do when only one player on has it.
        name, _, last = call.text.strip().rpartition(" ")
        count = int(last) if name.strip() and self._whois_count(last) else 0
        if not call.text.strip():
            await call.reply(f"Use: {call.prefix}whois <player | player ID> [lines]")
            return
        if count and (whole := named(call, call.text.strip())):
            # "Player 2" is someone: the number is part of the name, unless "Player" is someone too, and then the
            # line could mean either: asked, as between two players with one name.
            if shorter := [c for c in named(call, name) if c.id not in {w.id for w in whole}]:
                await ask(call, f"'{call.text.strip()}'", whole + shorter)
                await call.reply(f"A number after the player is how many lines: {call.prefix}whois #number {last}.")
                return
            count = 0
        target = await call.find(name if count else call.text, scope="seen", start=True)
        if target is None:
            return
        player_id = target.id
        record = self.ctx.players.get(player_id)
        if record is None:
            await call.reply(f"{player_id[:8]}: never seen here.")
            return
        shown = min(count, MOST) if count else RECENT
        now = time.time()
        # Not someone's decision: a mute put back, a kick passed on to another server, the let-back-in after a name
        # kick.
        rows = [r for r in self.ctx.db.query(
            "SELECT * FROM actions WHERE target_id = ? AND dry_run = 0 AND ok = 1 ORDER BY time DESC", (player_id,))
            if not str(r["evidence"] or "").startswith(("still muted:", "kicked on "))
            and not (r["source"] == "automod" and r["kind"] == "pardon")]

        lines = [self._whois_who(call, record, player_id)]
        if len(record.names) > 1:
            lines.append("Also called: " + ", ".join(record.names[1:8]))
        if now_line := await self._whois_now(player_id, now):
            lines.append("Now: " + now_line)
        counted = [r for r in rows if r["kind"] in KINDS_SHOWN]
        if counted:
            kinds = ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind in KINDS_SHOWN
                              if (n := sum(r["kind"] == kind for r in counted)))
            ways: dict[str, int] = {}
            for row in counted:
                way = {"commands": "by !", "in game": "by /", "automod": "by Vergil", "vote": "by vote",
                       "console": "by console"}.get(row["source"], f"by {row['source']}")
                ways[way] = ways.get(way, 0) + 1
            lines.append(f"Record: {kinds} ({', '.join(f'{n} {way}' for way, n in ways.items())})")
        else:
            lines.append("Record: nothing: never kicked, muted, banned or warned here.")
        if strikes := self._whois_strikes(player_id):
            lines.append("Strikes: " + strikes)
        for row in rows[:shown]:
            when = time.strftime("%m-%d %H:%M", time.localtime(row["time"]))
            reason = str(row["reason"] or "")
            if row["source"] == "automod":  # its message to the player: the strike in it, or what it acted on
                found = STRIKE_IN.search(reason)
                reason = found.group(1) if found else str(row["evidence"] or reason)[:80]
            length = shown_time(row["duration"])
            how = label(row)
            lines.append(f"{when} {row['server']}: {row['kind']}" + (f" {length}" if length else "")
                         + f" by {row['by']}" + (f" ({how})" if how else "") + (f": {reason}" if reason else ""))
        if len(rows) > shown:
            lines.append(f"…{len(rows) - shown} older: {call.prefix}whois {record.last_name} {min(len(rows), MOST)}")
        await call.reply("\n".join(lines))

    def _whois_who(self, call: CommandCall, record: Any, player_id: str) -> str:
        where = next((f"on {server} as #{p.number}" for server, state in self.ctx.servers.items()
                      if (p := state.player_by_id(player_id))), f"last here {ago(time.time() - record.last_seen)} ago")
        role = self.ctx.role_of(call.server, Player(0, record.last_name, player_id))
        since = time.strftime("%Y-%m-%d", time.localtime(record.first_seen))
        return (f"{record.last_name} ({player_id[:8]}): {where}" + (f", {role}" if role != "player" else "")
                + f" · {record.visits} visit{'s' if record.visits != 1 else ''} since {since}")

    async def _whois_now(self, player_id: str, now: float) -> str:
        """What's on them now, on every server: a mute on Vergil's list, a kick keeping them out, their bans."""
        bits: list[str] = []
        mutes = self._query(
            "SELECT expires, by, reason FROM automod_mutes WHERE player_id = ? AND lifted IS NULL AND (expires IS NULL "
            "OR expires > ?) UNION ALL SELECT expires, by, rule FROM automod_strikes WHERE player_id = ? AND action = "
            "'mute' AND pardoned IS NULL AND lifted IS NULL AND dry_run = 0 AND (expires IS NULL OR expires > ?)",
            (player_id, now, player_id, now))
        if mutes:
            mute = max(mutes, key=lambda r: float("inf") if r["expires"] is None else r["expires"])
            left = "for good" if mute["expires"] is None else f"{ago(mute['expires'] - now)} left"
            bits.append(f"muted {left} (by {mute['by']}" + (f": {mute[2]}" if mute[2] else "") + ")")
        kicked: set[str] = set()
        for kick in self.recent_kicks():  # newest first: the one keeping them out of each server
            if kick.id != player_id or kick.time + kick.seconds <= now or kick.server in kicked:
                continue
            kicked.add(kick.server)
            left = ago(kick.time + kick.seconds - now)
            if kick.through_console and self._across():
                bits.append(f"kicked from {kick.server} by {kick.by}, off every server {left} more")
            else:
                how = "" if kick.by in ("a vote", "the server") else "/kick "
                bits.append(f"kicked from {kick.server}, out {left} more ({how}by {kick.by})")
        bits += await self._whois_bans(player_id, now)
        return " · ".join(bits)

    async def _whois_bans(self, player_id: str, now: float) -> list[str]:
        """Their bans as the servers have them: one on Huragok's list (every server's), and each server's own (a /ban
        there), read from the servers' lists; a server whose list can't be read now, from the record."""
        def left(expires: Any) -> str:
            if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
                return "for good"
            expires = expires / 1000 if expires > 1e11 else expires
            return f"{ago(expires - now)} left"

        bits: list[str] = []
        everywhere = self.every_server("ban", player_id)[-1:] if self._across() else []
        for row in everywhere:
            bits.append(f"banned on every server, {left(row['expires'])} (by {row['by']}"
                        + (f": {row['reason']}" if row["reason"] else "") + ")")

        async def read(server: str) -> tuple[str, list[dict[str, Any]] | None]:
            try:
                entries, _ = await asyncio.wait_for(self._read_bans(server), READ_SECONDS)
            except asyncio.TimeoutError:
                entries = None
            return server, entries

        connected = [server for server, state in self.ctx.servers.items() if state.connected]
        unread = [server for server in self.ctx.servers if server not in connected]
        same: dict[tuple[str, str, str], list[str]] = {}  # one ban on several servers' lists, said once
        for server, entries in await asyncio.gather(*(read(server) for server in connected)):
            if entries is None:
                unread.append(server)
                continue
            for entry in entries:
                if str(entry.get("id") or "").lower() != player_id:
                    continue
                if everywhere and through_console(entry.get("banned_by")):
                    continue  # Huragok's: said above
                how = "" if through_console(entry.get("banned_by")) else "/ban "
                key = (left(entry.get("expires")), f"{how}by {who(entry.get('banned_by'))}", str(entry.get("reason") or ""))
                same.setdefault(key, []).append(server)
        for (time_left, by, reason), servers in same.items():
            bits.append(f"banned on {', '.join(servers)}, {time_left} ({by}" + (f": {reason}" if reason else "") + ")")
        for server in sorted(unread):  # stopped, or its list didn't answer: what the record says was last done there
            rows = self.ctx.db.query("SELECT * FROM actions WHERE target_id = ? AND server = ? AND kind IN ('ban', "
                                     "'unban') AND ok = 1 AND dry_run = 0 ORDER BY time DESC LIMIT 1",
                                     (player_id, server))
            if not rows or rows[0]["kind"] != "ban" or (everywhere and rows[0]["source"] != "in game"):
                continue
            ban = rows[0]
            length = shown_time(ban["duration"])
            seconds = int(str(ban["duration"])[:-1]) if str(ban["duration"] or "").endswith("s") and \
                str(ban["duration"])[:-1].isdecimal() else None
            if length and seconds is not None and ban["time"] + seconds <= now:
                continue
            time_left = "for good" if not length else (f"{ago(ban['time'] + seconds - now)} left" if seconds
                                                       else f"for {length}")
            how = "/ban " if ban["source"] == "in game" else ""
            bits.append(f"banned on {server}, {time_left} ({how}by {ban['by']}"
                        + (f": {ban['reason']}" if ban["reason"] else "") + f"; from the record: {server}'s list "
                        "can't be read now)")
        return bits

    def _whois_strikes(self, player_id: str) -> str:
        forget = float(self.ctx.part_option("automod", "forget_after_days", 0) or 0)
        since = time.time() - forget * 86400 if forget else 0
        rows = self._query("SELECT grp, COUNT(*) AS n FROM automod_strikes WHERE player_id = ? AND pardoned IS NULL "
                           "AND dry_run = 0 AND time >= ? GROUP BY grp ORDER BY grp", (player_id, since))
        return ", ".join(f"{r['grp']} {r['n']}" for r in rows)
