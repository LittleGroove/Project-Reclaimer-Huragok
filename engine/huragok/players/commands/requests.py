"""Probationary moderators (0.5.3): moderators' commands, but what they do waits for a moderator or an admin.

SPR's staff meeting (29 October 2026): a rank between member and moderator, to bring new moderators in on
probation. `!promote` is still the way up (a moderator makes a member a probationary moderator, an admin makes one a
moderator: roles.py).

- Looking runs as it does for a moderator (router.LOOKING). Every other moderators' command that names a player is a
  request: the player found with every guard (as if it ran), kept (commands_requests) with the lowest number not
  waiting already, and then: the probationary moderator reads "Sent for review as #3", every moderator and admin on
  any server reads who asks what (privately), and the player named reads nothing.
- `!reports` lists them, every server's. `!confirm 3` runs it as the confirmer's own (they're who acts), on the
  server the player is on now, else the one it was asked on; `!confirm 3 strike slurs` (or `mute 1h…`) does that to
  the same player instead. `!deny 3 [why]` turns it down. Either frees the number.
- Nothing lapses: confirmed later, it still happens, its time running from the moment it's confirmed. For someone
  who has left: a mute is Vergil's, as they next join; a kick keeps them off every server for a kick's time
  (across.py, as Huragok's kicks do); a warning waits for their next join (commands_notices); a strike counts at
  once.
- The probationary moderator is told how each went, now or as they next join. `!whois` shows their tally; the
  ledger keeps "requested by" beside the confirmer's action.
- commands.toml lists a probationary moderator as a member (with a note) and commands_probation marks them: an
  older Huragok, if an update is put back, sees a member, never more (it refuses a section it doesn't know).
"""

from __future__ import annotations

import json
import time
from typing import Any

from ...core.commandsfile import PROBATION, ROLES
from ...core.events import Event
from ...core.part import command, on
from ...lore import callout
from ...rcon import RconError
from ...servers.state import Player
from ..router import CommandCall, Registered, unquote
from ..targets import ago
from .base import CommandsBase

AWAY_KICK_SECONDS = 600.0  # a kick confirmed while they're away: the server's first kick keeps someone out 10 minutes
NEEDS_THEM_ON = ("kick", "warn")  # the console can only do these to someone who's on: done Huragok's way otherwise
RECORDED = ("kick", "mute", "unmute", "warn", "pardon", "ban", "unban")  # what the ledger keeps (actions)


class ProbationRequests(CommandsBase):
    def _requests_start(self) -> None:
        """At the start: who's on probation (Huragok's database) for the roles, and the router's requests here."""
        probation = self.ctx.probation
        probation.clear()
        probation.update(row["target"] for row in self.ctx.db.query("SELECT target FROM commands_probation"))
        self.ctx.on_request(self._request)

    # The rank -------------------------------------------------------------------------------------------------

    def _probation_mark(self, player_id: str, name: str, by: str) -> None:
        self.ctx.db.execute("INSERT OR REPLACE INTO commands_probation (target, name, time, by) VALUES (?, ?, ?, ?)",
                            (player_id, name, time.time(), by))
        self.ctx.probation.add(player_id)

    def _probation_unmark(self, player_id: str) -> None:
        self.ctx.db.execute("DELETE FROM commands_probation WHERE target = ?", (player_id,))
        self.ctx.probation.discard(player_id)

    # Asking ---------------------------------------------------------------------------------------------------

    def _waiting(self) -> list[Any]:
        return self.ctx.db.query("SELECT * FROM commands_requests WHERE status = 'open' ORDER BY slot")

    def _free_slot(self) -> int:
        taken = {row["slot"] for row in self._waiting()}
        slot = 1
        while slot in taken:
            slot += 1
        return slot

    def _what(self, command: str, name: str, words: list[str]) -> str:
        return " ".join([f"{self.ctx.permissions.prefix}{command}", name, *words])

    async def _request(self, call: CommandCall, registered: Registered, target: Player, rest: list[str]) -> None:
        slot = self._free_slot()
        name = registered.spec.name
        self.ctx.db.execute(
            "INSERT INTO commands_requests (slot, time, server, by_id, by_name, command, target, target_name, words, "
            "status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')",
            (slot, time.time(), call.server, call.player.id, call.player.name, name, target.id, target.name,
             json.dumps(rest)))
        what = self._what(name, target.name, rest)
        self.ctx.log.info("[%s] %s (probationary moderator) asks #%d: %s", call.server, call.player.name, slot, what)
        await call.reply(f"Sent for review as #{slot}: {what}. A moderator or admin confirms it; nothing happens "
                         "until then.")
        prefix = self.ctx.permissions.prefix
        await self._tell_staff(callout("request", f"{call.player.name} (probationary moderator) asks, #{slot} on "
                                                  f"{call.server}: {what}. {prefix}confirm {slot} or {prefix}deny "
                                                  f"{slot}"), skip=call.player.id)

    async def _tell_staff(self, text: str, skip: str = "") -> None:
        """Every moderator and admin on any server, privately."""
        for server, state in list(self.ctx.servers.items()):
            for player in list(state.players.values()):
                if player.id == skip or ROLES.index(self.ctx.highest_role(player)) < ROLES.index("moderator"):
                    continue
                try:
                    await self.ctx.tell(server, player, text)
                except (RconError, KeyError) as error:
                    self.ctx.log.debug("[%s] couldn't tell %s of a request: %s", server, player.name, error)

    # Deciding -------------------------------------------------------------------------------------------------

    async def _picked(self, call: CommandCall) -> Any:
        """The waiting request the line's first word numbers (3 or #3), or None after saying why."""
        word = call.args[0].lstrip("#") if call.args else ""
        if not word.isdigit():
            await call.reply(f"Use: {call.prefix}{call.name} <number>. {call.prefix}reports lists them.")
            return None
        row = next((r for r in self._waiting() if r["slot"] == int(word)), None)
        if row is None:
            await call.reply(f"No request #{word} is waiting. {call.prefix}reports lists them.")
        return row

    @command("reports", help="The probationary moderators' requests waiting, on every server: !confirm or !deny each",
             role="moderator")
    async def reports(self, call: CommandCall) -> None:
        rows = self._waiting()
        if not rows:
            await call.reply("No requests are waiting.")
            return
        now = time.time()
        lines = [f"#{r['slot']} {r['server']}, {ago(now - r['time'])} ago: {r['by_name']} asks "
                 f"{self._what(r['command'], r['target_name'] or r['target'][:8], json.loads(r['words']))}"
                 for r in rows]
        await self._reply_text(call, f"{len(rows)} waiting:\n" + "\n".join(lines) + f"\n{call.prefix}confirm <number> "
                               f"[another command instead], {call.prefix}deny <number> [why]")

    @command("confirm", usage="<number> [command and its words, to do that instead]",
             help="Confirm a probationary moderator's request: it happens now, as yours (e.g. !confirm 3), or do "
                  "something else to that player instead (!confirm 3 strike slurs)", role="moderator")
    async def confirm(self, call: CommandCall) -> None:
        row = await self._picked(call)
        if row is None:
            return
        if row["by_id"] == call.player.id:
            await call.reply("That's your own request: another moderator or an admin confirms it.")
            return
        prefix = call.prefix
        if call.args[1:]:
            name, words = call.args[1].lower().removeprefix(prefix), call.args[2:]
        else:
            name, words = row["command"], json.loads(row["words"])
        target = Player(0, row["target_name"] or row["target"][:8], row["target"])
        # The server they're on now (the one to act on), else the one it was asked on.
        where = next((s for s, state in self.ctx.servers.items() if state.player_by_id(target.id)), row["server"])
        if where not in self.ctx.servers:
            where = call.server
        live = self.ctx.server(where).player_by_id(target.id)
        heard: list[str] = []

        async def tell(_server: str, player_id: str, text: str) -> None:
            heard.append(text)
            await self.ctx.tell(call.server, player_id, text)

        started = time.time()
        if live is None and name in NEEDS_THEM_ON:
            why = await self._while_away(call, row, name, target, words, where)
        else:
            why = await self.ctx.run_as(name, live or target, words, server=where, player=call.player,
                                        role=call.role, tell=tell)
        if why is None and not self._acted(name, target.id, started):
            why = "nothing was done (see above)"
        if why:
            await call.reply(f"#{row['slot']} is still waiting: {why}.")
            return
        done = self._what(name, target.name, words)
        self._mark_requested(target.id, started, row)
        self.ctx.db.execute("UPDATE commands_requests SET status = 'confirmed', decided = ?, decided_by = ?, done = ?, "
                            "outcome = ? WHERE number = ?",
                            (time.time(), call.player.name, done, " / ".join(heard), row["number"]))
        self.ctx.log.info("[%s] %s confirmed #%d (%s asked %s): %s", call.server, call.player.name, row["slot"],
                          row["by_name"], self._what(row["command"], target.name, json.loads(row["words"])), done)
        await call.reply(f"Confirmed #{row['slot']}" + ("" if live else f" ({target.name} isn't on: from now, as "
                                                                        "they come back)") + ".")
        await self._tell_asker(row["number"])

    async def _while_away(self, call: CommandCall, row: Any, name: str, target: Player, words: list[str],
                          server: str) -> str | None:
        """A kick or a warning for someone who has left: kept from every server for a kick's time from now
        (across.py), or read as they next join. None when done; else why not."""
        if outranked := self._outranks(call, target):
            return outranked
        reason = unquote(words)
        moderation = self.ctx.moderation
        evidence = f"requested by {row['by_name']} (#{row['slot']})"
        if name == "kick":
            action = await moderation.kick(server, target, reason, by=call.player.name, pending=True,
                                           evidence=evidence)
            if not action.dry_run:
                self._kick_while_away(server, target, call.player.name, reason, AWAY_KICK_SECONDS)
            return None
        action = await moderation.warn(server, target, reason, by=call.player.name, pending=True, evidence=evidence)
        if not action.dry_run:
            text = callout("warn", f"Warning from {call.player.name}: {reason}" if reason else
                           f"Warning from {call.player.name}.")
            self.ctx.db.execute("INSERT INTO commands_notices (time, target, text) VALUES (?, ?, ?)",
                                (time.time(), target.id, text))
        return None

    def _outranks(self, call: CommandCall, target: Player) -> str:
        role = self.ctx.highest_role(target)
        if call.role == "owner" or ROLES.index(role) < ROLES.index(call.role):
            return ""
        return f"{target.name} outranks you"

    def _acted(self, name: str, target_id: str, since: float) -> bool:
        """Whether what was confirmed left its mark: a ledger entry, a strike; other commands count as done."""
        if name in RECORDED:
            rows = self.ctx.db.query("SELECT COUNT(*) AS n FROM actions WHERE target_id = ? AND time >= ?",
                                     (target_id, since - 1))
            return bool(rows and rows[0]["n"])
        if name == "strike":
            try:
                rows = self.ctx.db.query("SELECT COUNT(*) AS n FROM automod_strikes WHERE player_id = ? AND time >= ?",
                                         (target_id, since - 1))
            except Exception:  # noqa: BLE001 - automod's table isn't there (automod never ran): nothing to read
                return True
            return bool(rows and rows[0]["n"])
        return True

    def _mark_requested(self, target_id: str, since: float, row: Any) -> None:
        """The ledger keeps who asked, beside the confirmer's action."""
        self.ctx.db.execute("UPDATE actions SET evidence = ? WHERE target_id = ? AND time >= ? AND evidence IS NULL",
                            (f"requested by {row['by_name']} (#{row['slot']})", target_id, since - 1))

    @command("deny", usage="<number> [why]", help="Turn down a probationary moderator's request (they're told why)",
             role="moderator")
    async def deny(self, call: CommandCall) -> None:
        row = await self._picked(call)
        if row is None:
            return
        why = unquote(call.args[1:])
        self.ctx.db.execute("UPDATE commands_requests SET status = 'denied', decided = ?, decided_by = ?, why = ? "
                            "WHERE number = ?", (time.time(), call.player.name, why, row["number"]))
        self.ctx.log.info("[%s] %s turned down #%d (%s asked %s)%s", call.server, call.player.name, row["slot"],
                          row["by_name"], row["command"], f": {why}" if why else "")
        await call.reply(f"Turned down #{row['slot']}.")
        await self._tell_asker(row["number"])

    # Telling the probationary moderator -----------------------------------------------------------------------

    def _outcome(self, row: Any) -> str:
        asked = self._what(row["command"], row["target_name"] or row["target"][:8], json.loads(row["words"]))
        if row["status"] == "confirmed":
            instead = f" ({row['done']} instead)" if row["done"] and row["done"] != asked else ""
            return callout("request confirmed", f"{row['decided_by']} confirmed your request #{row['slot']}, "
                                                f"{asked}{instead}.")
        return callout("request denied", f"{row['decided_by']} turned down your request #{row['slot']}, {asked}"
                                         + (f": {row['why']}" if row["why"] else "."))

    async def _tell_asker(self, number: int) -> None:
        row = next(iter(self.ctx.db.query("SELECT * FROM commands_requests WHERE number = ?", (number,))), None)
        if row is None:
            return
        for server, state in list(self.ctx.servers.items()):
            player = state.player_by_id(row["by_id"])
            if player is None:
                continue
            try:
                await self.ctx.tell(server, player, self._outcome(row))
            except (RconError, KeyError):
                return
            self.ctx.db.execute("UPDATE commands_requests SET told = 1 WHERE number = ?", (number,))
            return

    @on("rcon.join")
    async def _requests_joined(self, event: Event) -> None:
        """As someone joins: how their requests went while they were away, and a warning confirmed meanwhile."""
        player_id = str(event.get("id") or "").lower()
        if not player_id or not event.server:
            return
        player = self.ctx.server(event.server).player_by_id(player_id)
        if player is None:
            return
        lines = [self._outcome(r) for r in self.ctx.db.query(
            "SELECT * FROM commands_requests WHERE by_id = ? AND status != 'open' AND told = 0", (player_id,))]
        notices = self.ctx.db.query("SELECT * FROM commands_notices WHERE target = ? AND delivered IS NULL",
                                    (player_id,))
        lines += [n["text"] for n in notices]
        try:
            for line in lines:
                await self.ctx.tell(event.server, player, line)
        except RconError:
            return
        self.ctx.db.execute("UPDATE commands_requests SET told = 1 WHERE by_id = ? AND status != 'open'", (player_id,))
        if notices:
            self.ctx.db.execute("UPDATE commands_notices SET delivered = ? WHERE target = ? AND delivered IS NULL",
                                (time.time(), player_id))

    # The record -----------------------------------------------------------------------------------------------

    def probation_record(self, player_id: str) -> str:
        """A probationary moderator's requests, for !whois: "" for someone who never asked."""
        rows = self.ctx.db.query("SELECT status, COUNT(*) AS n FROM commands_requests WHERE by_id = ? GROUP BY status",
                                 (player_id,))
        counts = {r["status"]: r["n"] for r in rows}
        if not counts:
            return ""
        total = sum(counts.values())
        return (f"Requests as a probationary moderator: {total} ({counts.get('confirmed', 0)} confirmed, "
                f"{counts.get('denied', 0)} turned down, {counts.get('open', 0)} waiting)")


__all__ = ["ProbationRequests"]
