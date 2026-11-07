"""Probationary moderators (0.5.3): moderators' commands, but what they do waits for a moderator or an admin.

a staff meeting (29 October 2026): a rank between member and moderator, to bring new moderators in on
probation. `!promote` is still the way up (a moderator makes a member a probationary moderator, an admin makes one a
moderator: roles.py).

- Looking runs as it does for a moderator (router.LOOKING). Every other moderators' command that names a player is a
  request: the player found with every guard (as if it ran), kept (commands_requests) with the lowest number not
  waiting already, and then: the probationary moderator reads "Sent for review as #3", every moderator and admin on
  any server reads who asks what (privately), and the player named reads nothing.
- `!requests` lists them, every server's. `!confirm 3` runs it as the confirmer's own (they're who acts), on the
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
from ..ledger import kick_reason
from ..roles import a_role
from ..router import CommandCall, Registered, role_plural, unquote
from ..targets import ago
from .base import CommandsBase

AWAY_KICK_SECONDS = 600.0  # a kick confirmed while they're away: the server's first kick keeps someone out 10 minutes
NEEDS_THEM_ON = ("kick", "warn")  # the console can only do these to someone who's on: done Huragok's way otherwise
RECORDED = ("kick", "mute", "unmute", "warn", "pardon", "ban", "unban")  # what the ledger keeps (actions)
MOST_WAITING = 5  # one probationary moderator's requests waiting at once (the host's call, 2 Nov 2026)
NO_LONGER = "they aren't a probationary moderator any more"


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
        """The requests waiting for a decision. One whose asker isn't a probationary moderator any more (demoted, a
        ban for good, taken out of commands.toml) is dropped as it's found: it stayed confirmable (the host's call,
        2 Nov 2026). Someone who asked from Discord holds their rank there, where the bot judges it."""
        rows = self.ctx.db.query("SELECT * FROM commands_requests WHERE status = 'open' ORDER BY slot")
        gone = {row["number"] for row in rows if not row["needs"] and not self._still_on_probation(str(row["by_id"]))}
        for row in rows:
            if row["number"] in gone:
                self._drop(row)
        return [row for row in rows if row["number"] not in gone]

    def _still_on_probation(self, player_id: str) -> bool:
        if player_id.startswith("discord:"):
            return True
        pid = player_id.lower()
        members = {str(member).lower() for member in self.ctx.permissions.members.get("member", set())}
        return pid in {str(p).lower() for p in self.ctx.probation} and pid in members

    def _drop(self, row: Any) -> None:
        self.ctx.db.execute("UPDATE commands_requests SET status = 'dropped', decided = ?, decided_by = ?, why = ? "
                            "WHERE number = ? AND status = 'open'", (time.time(), "Huragok", NO_LONGER, row["number"]))
        what = self._what(row["command"], row["target_name"] or str(row["target"])[:8], json.loads(row["words"]))
        self.ctx.log.info("[%s] #%d dropped (%s asked %s): %s", row["server"], row["slot"], row["by_name"], what, NO_LONGER)
        self.ctx.emit("request_decided", row["server"], number=row["number"], slot=row["slot"], status="dropped",
                      by="Huragok", by_id=row["by_id"], what=what, why=NO_LONGER)

    def _free_slot(self) -> int:
        taken = {row["slot"] for row in self._waiting()}
        slot = 1
        while slot in taken:
            slot += 1
        return slot

    def _what(self, command: str, name: str, words: list[str]) -> str:
        return " ".join([f"{self.ctx.permissions.prefix}{command}", name, *words]).replace('"', "")  # a reason comes in quotes from a box in Discord

    async def _request(self, call: CommandCall, registered: Registered, target: Player, rest: list[str],
                       needs: str = "") -> None:
        """A probationary moderator's command, kept for a moderator or an admin; or, with `needs`, a command only that
        rank or above confirms (a moderator's strike that bans: an admin's)."""
        theirs = [row for row in self._waiting() if str(row["by_id"]).lower() == call.player.id.lower()]
        if not needs and len(theirs) >= MOST_WAITING:  # each one is told to every staff member on every server (the host's call)
            await call.reply(f"You have {len(theirs)} requests waiting already: a moderator or admin decides one before "
                             f"you ask again ({self.ctx.permissions.prefix}requests lists them).")
            return
        slot = self._free_slot()
        name = registered.spec.name
        self.ctx.db.execute(
            "INSERT INTO commands_requests (slot, time, server, by_id, by_name, command, target, target_name, words, "
            "status, needs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (slot, time.time(), call.server, call.player.id, call.player.name, name, target.id, target.name,
             json.dumps(rest), needs))
        what = self._what(name, target.name, rest)
        prefix = self.ctx.permissions.prefix
        if needs:
            self.ctx.log.info("[%s] %s asks %s #%d: %s", call.server, call.player.name, a_role(needs), slot, what)
            await call.reply(f"Sent to the {role_plural(needs)} as #{slot}: {what}. It would ban {target.name} for good, "
                             f"and only {a_role(needs)} or above bans; nothing happens until one confirms it.")
        else:
            self.ctx.log.info("[%s] %s (probationary moderator) asks #%d: %s", call.server, call.player.name, slot, what)
            await call.reply(f"Sent for review as #{slot}: {what}. A moderator or admin confirms it; nothing happens "
                             "until then.")
        number = self.ctx.db.query("SELECT number FROM commands_requests WHERE slot = ? AND status = 'open' ORDER BY number DESC LIMIT 1", (slot,))[0]["number"]
        self.ctx.emit("request", call.server, number=number, slot=slot, by=call.player.name, what=what, target_id=target.id, target_name=target.name, needs=needs)
        if needs:
            await self._tell_staff(callout("request", f"{call.player.name} asks {a_role(needs)}, #{slot} on "
                                                      f"{call.server}: {what}, which bans them for good. {prefix}confirm "
                                                      f"{slot} or {prefix}deny {slot}"), skip=call.player.id, least=needs)
            return
        await self._tell_staff(callout("request", f"{call.player.name} (probationary moderator) asks, #{slot} on "
                                                  f"{call.server}: {what}. {prefix}confirm {slot} or {prefix}deny "
                                                  f"{slot}"), skip=call.player.id)

    async def _tell_staff(self, text: str, skip: str = "", least: str = "moderator") -> None:
        """Every moderator and admin on any server (or only those of rank `least` and up), privately."""
        for server, state in list(self.ctx.servers.items()):
            for player in list(state.players.values()):
                if player.id == skip or ROLES.index(self.ctx.highest_role(player)) < ROLES.index(least):
                    continue
                try:
                    await self.ctx.tell(server, player, text)
                except (RconError, KeyError) as error:
                    self.ctx.log.debug("[%s] couldn't tell %s of a request: %s", server, player.name, error)

    # Deciding -------------------------------------------------------------------------------------------------

    async def _picked(self, call: CommandCall) -> Any:
        """The waiting request the line's first word numbers (3 or #3), or None after saying why."""
        word = call.args[0].lstrip("#") if call.args else ""
        if not (word.isascii() and word.isdecimal()):  # "²" is a digit int() refuses: it crashed the command (5.12)
            await call.reply(f"Use: {call.prefix}{call.name} <number>. {call.prefix}requests lists them.")
            return None
        row = next((r for r in self._waiting() if r["slot"] == int(word)), None)
        if row is None:
            await call.reply(f"No request #{word} is waiting. {call.prefix}requests lists them.")
        return row

    @command("requests", help="The probationary moderators' requests waiting, on every server: !confirm or !deny each",
             role="moderator")
    async def requests(self, call: CommandCall) -> None:
        rows = self._waiting()
        if not rows:
            await call.reply("No requests are waiting.")
            return
        now = time.time()
        lines = [f"#{r['slot']} {r['server']}, {ago(now - r['time'])} ago: {r['by_name']} asks "
                 + (f"{a_role(r['needs'])} " if r["needs"] else "")
                 + f"{self._what(r['command'], r['target_name'] or r['target'][:8], json.loads(r['words']))}"
                 + (" (it bans them for good)" if r["needs"] else "")
                 for r in rows]
        await self._reply_text(call, f"{len(rows)} waiting:\n" + "\n".join(lines) + f"\n{call.prefix}confirm <number> "
                               f"[another command instead], {call.prefix}deny <number> [why]")

    def _requests_deciding(self) -> set[int]:
        """Requests being decided now: one at a time, or two presses of Confirm (or two moderators at once) both
        acted, a strike climbing the ladder twice (audit 2026-11, 6.10)."""
        if not hasattr(self, "_being_decided"):
            self._being_decided: set[int] = set()
        return self._being_decided

    @command("confirm", usage="<number> [command and its words, to do that instead]",
             help="Confirm a probationary moderator's request: it happens now, as yours (e.g. !confirm 3), or do "
                  "something else to that player instead (!confirm 3 strike slurs)", role="moderator")
    async def confirm(self, call: CommandCall) -> None:
        row = await self._picked(call)
        if row is None:
            return
        if row["by_id"] == call.player.id:
            await call.reply(f"That's your own request: {a_role(row['needs'])} or above confirms it." if row["needs"] else
                             "That's your own request: another moderator or an admin confirms it.")
            return
        if row["needs"] and call.role != "faculty" and ROLES.index(call.role) < ROLES.index(row["needs"]):
            await call.reply(f"#{row['slot']} takes {a_role(row['needs'])} or above: it bans {row['target_name'] or 'them'} "
                             "for good.")
            return
        if row["number"] in self._requests_deciding():
            await call.reply(f"#{row['slot']} is being decided already.")
            return
        self._requests_deciding().add(row["number"])
        try:
            await self._confirm(call, row)
        finally:
            self._requests_deciding().discard(row["number"])

    async def _confirm(self, call: CommandCall, row: Any) -> None:
        prefix = call.prefix
        if call.args[1:]:
            name, words = call.args[1].lower().removeprefix(prefix), call.args[2:]
        else:
            name, words = row["command"], json.loads(row["words"])
        target = Player(0, row["target_name"] or row["target"][:8], row["target"])
        # The server they're on now (the one to act on), else the one it was asked on.
        where = next((s for s, state in self.ctx.servers.items() if state.connected and state.player_by_id(target.id)), row["server"])
        if where not in self.ctx.servers or not self.ctx.servers[where].connected:  # stopped since: nothing can be done on it
            where = call.server
        live = self.ctx.server(where).player_by_id(target.id)
        heard: list[str] = []

        async def tell(_server: str, player_id: str, text: str) -> None:
            heard.append(text)
            await call._tell(call.server, player_id, text)  # wherever they are: in game, or Discord

        started = time.time()
        if live is None and name in NEEDS_THEM_ON:
            why = await self._while_away(call, row, name, target, words, where)
        else:
            why = await self.ctx.run_as(name, live or target, words, server=where, player=call.player,
                                        role=call.role, tell=tell)
        if why is None and not self._acted(name, target.id, started):
            # A strike that would ban, confirmed by a moderator: it's the admins' now (a request of its own), and this
            # one's done with.
            sent = self.ctx.db.query("SELECT slot FROM commands_requests WHERE needs != '' AND status = 'open' AND target = ? "
                                     "AND time >= ? ORDER BY number DESC LIMIT 1", (target.id, started - 1))
            if sent:
                heard.append(f"sent to the admins as #{sent[0]['slot']}")
            else:
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
        self.ctx.emit("request_decided", row["server"], number=row["number"], slot=row["slot"], status="confirmed", by=call.player.name, by_id=row["by_id"], what=done, why="")
        await self._tell_asker(row["number"])

    async def _while_away(self, call: CommandCall, row: Any, name: str, target: Player, words: list[str],
                          server: str) -> str | None:
        """A kick or a warning for someone who has left: kept from every server for a kick's time from now
        (across.py), or read as they next join. None when done; else why not."""
        if outranked := self._outranks(call, target):
            return outranked
        moderation = self.ctx.moderation
        evidence = f"requested by {row['by_name']} (#{row['slot']})"
        if name == "kick":
            duration, rest, forever = self._split_time(words)
            if forever and words[0].lower() == "never":  # the start of a reason, not "for good" (audit 5.6)
                duration, rest, forever = None, words, False
            if why := self._no_time(duration):
                return why
            if forever:  # a kick for good is a ban, and takes the rank a ban does
                return await self.ctx.run_as("ban", target, rest, server=server, player=call.player, role=call.role, tell=call._tell)
            if duration:
                if too_long := self.kick_too_long(call.role, duration):  # whoever confirms it is who gives it
                    return too_long
                await moderation.ban(server, target, duration, unquote(rest), told=kick_reason(duration, unquote(rest)), by=call.player.name, evidence=evidence)
                return None
        reason = unquote(words)
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
        if call.role == "faculty" or ROLES.index(role) < ROLES.index(call.role):
            return ""
        return f"{target.name} outranks you"

    def _acted(self, name: str, target_id: str, since: float) -> bool:
        """Whether what was confirmed left its mark: a ledger entry, a strike; other commands count as done."""
        if name in RECORDED:
            rows = self.ctx.db.query("SELECT COUNT(*) AS n FROM actions WHERE target_id = ? AND time >= ? AND ok = 1 AND dry_run = 0",
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
        if row["number"] in self._requests_deciding():  # being confirmed: that decides it
            await call.reply(f"#{row['slot']} is being decided already.")
            return
        why = unquote(call.args[1:])
        self.ctx.db.execute("UPDATE commands_requests SET status = 'denied', decided = ?, decided_by = ?, why = ? "
                            "WHERE number = ?", (time.time(), call.player.name, why, row["number"]))
        self.ctx.log.info("[%s] %s turned down #%d (%s asked %s)%s", call.server, call.player.name, row["slot"],
                          row["by_name"], row["command"], f": {why}" if why else "")
        await call.reply(f"Turned down #{row['slot']}.")
        self.ctx.emit("request_decided", row["server"], number=row["number"], slot=row["slot"], status="denied", by=call.player.name, by_id=row["by_id"], what=self._what(row["command"], row["target_name"] or row["target"][:8], json.loads(row["words"])), why=why)
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
