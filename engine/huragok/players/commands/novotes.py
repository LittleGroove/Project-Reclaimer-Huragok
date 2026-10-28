"""Calling votes, taken away from a player who spams them (the host's call, 27 October): !novote, !allowvote, !novotes.

The server has no way to stop one player calling votes (its only limits are its own waits: a minute after a vote of
theirs failed, three after a vote to kick), so Vergil cancels a vote such a player calls the moment the server reports
it, and tells them why. They can still answer everyone else's votes (F5, F6): only calling them is taken away, the
host's call ("not their ability to vote on something that someone else started").

`!novote <player> [time] [reason]`: for that long (30s, 10m, 2h, 1d, 2w), or with no time (or perm) until someone
gives it back with !allowvote. It follows the player ID, so a rejoin or a new name keeps it, and it's kept in the
database (step 6) through Huragok's restarts. Someone who left is picked as for !mute (by @number) and has it from
when they next join. Like Huragok's other moderation it reaches every server ([automod] across_servers), or only the
one it was given on.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

from ...core.events import Event
from ...core.part import command, on
from ...core.text import describe_seconds, describe_time, seconds_of
from ...lore import callout
from ...rcon import RconError
from ..router import CommandCall, unquote
from ..targets import Candidate, ago, offer
from .base import CommandsBase
from .votekicks import MARK, TIMEOUT, _named

if TYPE_CHECKING:
    from ...core.context import PartContext

TELL_GAP = 30.0  # seconds between explanations to the same player, however many votes they call
LISTED = 10  # entries in one !novotes answer
FOR_GOOD = "until a moderator gives them back"


def _away_for(row: sqlite3.Row, now: float) -> str:
    """How much longer a !novote keeps someone from calling votes: 'for another 8 minutes', or FOR_GOOD."""
    return FOR_GOOD if row["until"] is None else f"for another {describe_seconds(row['until'] - now)}"


class NoVotes(CommandsBase):
    def __init__(self, ctx: PartContext) -> None:
        super().__init__(ctx)
        self._told_novote: dict[str, float] = {}  # player ID -> when Vergil last told them why their vote went

    # The commands ------------------------------------------------------------------------------

    @command("novote", usage="<player> [time | perm] [reason]",
             help="They can't call votes: any vote they call is cancelled at once (they can still vote on others'), "
                  "e.g. !novote Bravo 10m spamming votes; no time means until !allowvote", role="moderator",
             player="seen", acts=True, follows="time")
    async def novote(self, call: CommandCall) -> None:
        target = call.target
        assert target is not None
        length, rest, _ = self._split_time(call.rest)
        if length is not None and seconds_of(length) <= 0:
            await call.reply(f"Use: {call.prefix}novote <player> [time | perm] [reason]: a time such as 10m or 2h, or "
                             "none for until someone gives their votes back.")
            return
        reason = unquote(rest)
        was = self._no_vote_row(target.id, call.server)
        self._lift_no_votes(target.id, call.server, by=call.player.name)  # a new time replaces the old one
        now = time.time()
        self.ctx.db.execute("INSERT INTO commands_no_votes (time, target, name, reason, by, server, until, length) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (now, target.id, target.name, reason, call.player.name, call.server,
                             now + seconds_of(length) if length else None, describe_time(length) if length else None))
        spoken = f"for {describe_time(length)}" if length else FOR_GOOD
        self.log.info("[%s] %s took %s's votes away %s%s (%snovote)", call.server, call.player.name, target.name,
                      spoken, f": {reason}" if reason else "", call.prefix)
        before = f"{target.name}'s votes were already away ({_away_for(was, now)}); now {spoken}." if was else ""
        if not target.number:
            where = "" if self._across() else " here"
            await call.reply(f"{target.name} isn't on now: any vote they call{where} once they're back is cancelled, "
                             + (f"for {describe_time(length)} from now." if length else f"{FOR_GOOD}.")
                             + (f" {before}" if before else ""))
            return
        line = callout("no votes", f"{call.player.name} took {target.name}'s votes away {spoken}"
                       + (f": {reason}" if reason else "") + ". Any vote they call is cancelled.")
        if self.ctx.dry_run:  # nothing is cancelled, so nobody is told it will be
            await call.reply(f"Dry run: their votes are only logged, not cancelled. Players would read: {line}")
        else:
            try:
                await self.ctx.say(call.server, line)
            except RconError:
                await call.reply(line)  # the console wouldn't take a [Server] line: the moderator reads it at least
        if before:
            await call.reply(before)

    @command("allowvote", usage="<player | @number>", help="Give a player their votes back after !novote",
             role="moderator", player="seen", start=True, words_after=False)
    async def allowvote(self, call: CommandCall) -> None:
        target = call.target
        assert target is not None
        if self._no_vote_row(target.id, call.server) is None:
            await call.reply(f"{target.name} can call votes already.")
            return
        self._lift_no_votes(target.id, call.server, by=call.player.name)
        self.log.info("[%s] %s gave %s's votes back (%sallowvote)", call.server, call.player.name, target.name,
                      call.prefix)
        await call.reply(f"{target.name} can call votes again.")
        if target.number:
            try:
                await self.ctx.tell(call.server, target, callout("votes back", f"{call.player.name} gave your votes "
                                                                               "back: you can call votes again."))
            except RconError:
                pass

    @command("novotes", help="Who can't call votes now (!novote), with who, why and how long; @numbers for "
                             "!allowvote", role="moderator")
    async def novotes(self, call: CommandCall) -> None:
        now = time.time()
        state = self.ctx.servers.get(call.server)
        entries = []
        for row in self._no_vote_rows(call.server, now)[:LISTED]:
            player = state.player_by_id(str(row["target"])) if state is not None else None
            note = (f"votes away {_away_for(row, now)}, by {row['by']} {ago(now - row['time'])} ago"
                    + (f" on {row['server']}" if row["server"] != call.server else "")
                    + (f": {row['reason']}" if row["reason"] else ""))
            entries.append(Candidate(str(row["target"]), player.name if player else str(row["name"] or "?"),
                                     number=player.number if player else 0, note=note))
        if not entries:
            await call.reply(f"Everyone here can call votes: nobody's votes are away ({call.prefix}novote "
                             "<player> [time]).")
            return
        lines = await offer(call, entries)
        await call.reply("\n".join([*lines, f"{call.prefix}allowvote @number gives one their votes back."]))

    # A vote one of them calls ------------------------------------------------------------------

    @on("rcon.vote")
    async def no_vote_called(self, event: Event) -> None:
        """A vote called by a player whose votes are away: cancelled at once, and they're told why."""
        server = event.server or ""
        state = self.ctx.servers.get(server)
        if event.get("state") != "called" or state is None:
            return
        caller = str(event.get("caller") or "")
        callers = _named(state, caller) if caller else []
        if not callers:  # the console's (an admin's startvote), or someone gone already
            return
        rows = [self._no_vote_row(p.id, server) for p in callers]
        if any(row is None for row in rows):  # someone sharing the name could have called it: never theirs to lose
            return
        player, row = callers[0], rows[0]
        assert row is not None
        subject = str(event.get("subject") or "") or "do something"
        if self.ctx.dry_run:
            self.log.info("[%s] Dry run: would cancel %s's vote to %s (their votes are away)", server, player.name,
                          subject)
            return
        try:
            reply = await self.ctx.rcon(server).command("cancelvote", by=MARK, timeout=TIMEOUT)
        except RconError as error:
            self.log.warning("[%s] couldn't cancel %s's vote to %s: %s", server, player.name, subject, error)
            return
        if not reply.ok:  # "No vote is under way": it ended already (another cancel, or the server's own)
            self.log.info("[%s] %s's vote to %s wasn't cancelled: %s", server, player.name, subject, reply.text)
            return
        self.log.warning("[%s] cancelled %s's vote to %s: their votes are away %s (by %s)", server, player.name,
                         subject, _away_for(row, time.time()), row["by"])
        now = time.monotonic()
        if now - self._told_novote.get(player.id, -TELL_GAP) < TELL_GAP:
            return
        self._told_novote = {k: t for k, t in self._told_novote.items() if now - t < TELL_GAP}
        self._told_novote[player.id] = now
        try:
            await self.ctx.tell(server, player, callout("no votes", f"Your vote was cancelled: {row['by']} took your "
                                                                    f"votes away {_away_for(row, time.time())}"
                                                                    + (f" ({row['reason']})" if row["reason"] else "")
                                                                    + ". You can still vote on other players' votes."))
        except RconError:
            pass

    # The list ----------------------------------------------------------------------------------

    def _no_vote_rows(self, server: str, now: float) -> list[sqlite3.Row]:
        """Whose votes are away on `server` now (every server's, with [automod] across_servers), newest first."""
        rows = self.ctx.db.query("SELECT * FROM commands_no_votes WHERE lifted IS NULL AND (until IS NULL OR "
                                 "until > ?) ORDER BY time DESC", (now,))
        across = self._across()
        return [r for r in rows if across or r["server"] == server]

    def _no_vote_row(self, player_id: str, server: str) -> sqlite3.Row | None:
        """The newest !novote keeping this player from calling votes on `server` now, or None."""
        player_id = player_id.lower()
        return next((r for r in self._no_vote_rows(server, time.time()) if str(r["target"]).lower() == player_id),
                    None)

    def _lift_no_votes(self, player_id: str, server: str, *, by: str) -> None:
        """Every !novote keeping this player from calling votes on `server` comes off."""
        now = time.time()
        for row in self._no_vote_rows(server, now):
            if str(row["target"]).lower() == player_id.lower():
                self.ctx.db.execute("UPDATE commands_no_votes SET lifted = ?, lifted_by = ? WHERE number = ?",
                                    (now, by, row["number"]))

    def _no_vote_now(self, player_id: str, now: float) -> str:
        """For !whois's Now: "can't call votes, 8m more (by Mod: spam)", or ""."""
        row = next((r for r in self.ctx.db.query("SELECT * FROM commands_no_votes WHERE lifted IS NULL AND (until IS "
                                                 "NULL OR until > ?) ORDER BY time DESC", (now,))
                    if str(r["target"]).lower() == player_id.lower()), None)
        if row is None:
            return ""
        left = "for good" if row["until"] is None else f"{ago(row['until'] - now)} more"
        where = "" if self._across() else f" on {row['server']}"
        why = f": {row['reason']}" if row["reason"] else ""
        return f"can't call votes{where}, {left} (by {row['by']}{why})"
