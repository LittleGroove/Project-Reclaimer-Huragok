"""Switching a server's voice from the game, admins' (the host's call, 25 October): !toggleglobal, !toggleteam and
!toggleproxy (one voice channel each: V, B, N), and !togglevoice (all voice, for a while).

No console command changes these, so each writes the key in the server's own [[server]] block of dedicated.toml
(never [defaults], which every server in the file reads), and the server applies it by itself within seconds
(auto_reload): no restart, nobody dropped. Only the server it's typed on.

A channel toggle stays, unless a time follows it (30s, 10m, 2h, 1d): then it switches back once that's up.
!togglevoice is for quelling voice spam: off always for 10 minutes, then back on by itself; typed again it's back on at
once. What's put back is kept in the database (step 4), so a restart of Huragok can't leave voice off for good: one
overdue as Huragok starts is put back at once. Anyone changing the setting meanwhile (another toggle, the window's D)
wins: a setting is only put back while it's still what the toggle made it.

Players read every change as a [Server] line, with how long, and again when it's put back. (No !togglechat: the
server's own lines are text chat, so with it off nobody could type the command to bring it back, nor read why.)
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ...core.events import Event
from ...core.part import command, every, on
from ...core.text import describe_seconds, describe_time, seconds_of
from ...core.tomledit import read_toml
from ...lore import callout
from ...program.settings import current, effective, find, reset, write
from ...rcon import RconError
from ..router import CommandCall
from ..targets import TIME
from .base import CommandsBase

if TYPE_CHECKING:
    from ...core.context import PartContext


@dataclass(frozen=True)
class Toggle:
    key: str  # in dedicated.toml
    label: str  # as players read it mid-sentence


TOGGLES = {
    "toggleglobal": Toggle("voice_global", "global voice (V)"),
    "toggleteam": Toggle("voice_team", "team voice (B)"),
    "toggleproxy": Toggle("voice_proximity", "proximity voice (N)"),
    "togglevoice": Toggle("voice", "voice chat"),
}
BY_KEY = {toggle.key: toggle for toggle in TOGGLES.values()}
CHANNELS = ("voice_global", "voice_team", "voice_proximity")
VOICE_OFF = "10m"  # how long !togglevoice turns voice off, always (the host's call: no other time)
RELOAD_ANSWER_SECONDS = 15.0  # the server re-reads its file a couple of seconds after a change; later is another edit's
RETRY_SECONDS = 60.0  # a setting that couldn't be put back is tried again after this long


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _state(on: bool) -> str:
    return "on" if on else "off"


class SettingToggles(CommandsBase):
    def __init__(self, ctx: PartContext) -> None:
        super().__init__(ctx)
        self._written: dict[str, tuple[CommandCall, float]] = {}  # server -> the toggle and when, for the answer

    @command("toggleglobal", usage="[on | off] [time]", help="Global voice (V, everyone hears) on or off on this "
             "server; with a time (10m, 2h) it switches back once that's up", role="admin")
    async def toggleglobal(self, call: CommandCall) -> None:
        await self._channel(call, TOGGLES["toggleglobal"])

    @command("toggleteam", usage="[on | off] [time]", help="Team voice (B, teammates hear) on or off on this server; "
             "with a time it switches back once that's up", role="admin")
    async def toggleteam(self, call: CommandCall) -> None:
        await self._channel(call, TOGGLES["toggleteam"])

    @command("toggleproxy", usage="[on | off] [time]", help="Proximity voice (N, players nearby hear) on or off on "
             "this server; with a time it switches back once that's up", role="admin")
    async def toggleproxy(self, call: CommandCall) -> None:
        await self._channel(call, TOGGLES["toggleproxy"])

    @command("togglevoice", help="All voice chat off on this server for 10 minutes, then back on by itself; typed "
             "again, back on now", role="admin")
    async def togglevoice(self, call: CommandCall) -> None:
        if call.args:
            await call.reply(f"Use: {call.prefix}togglevoice (voice off for {describe_time(VOICE_OFF)}, then back on "
                             "by itself; typed again, it's back on now)")
            return
        await self._change(call, TOGGLES["togglevoice"], lambda was: (not was, VOICE_OFF if was else None))

    # A change from the game -----------------------------------------------------------------

    async def _channel(self, call: CommandCall, toggle: Toggle) -> None:
        wanted, length = _words(call.args)
        if length == "":
            await call.reply(f"Use: {call.prefix}{call.name} [on | off] [time] (alone it switches over and stays; a "
                             "time such as 10m switches it back once that's up)")
            return
        await self._change(call, toggle, lambda was: ((not was) if wanted is None else wanted, length))

    async def _change(self, call: CommandCall, toggle: Toggle,
                      decide: Callable[[bool], tuple[bool, str | None]]) -> None:
        """Write what `decide(was)` says, (on, a time or None), and tell everyone; a time puts it back once up."""
        config = self.ctx.config_path(call.server)
        if config is None:
            await call.reply(f"Couldn't: Huragok doesn't know where {call.server}'s dedicated.toml is.")
            return
        try:
            data, was, had_own = await asyncio.to_thread(_read, config, call.server, toggle.key)
        except LookupError:  # writing would add a [[server]] block of its own, with no port: a file the server refuses
            await call.reply(f"Couldn't: {config.name} has no [[server]] block named {call.server} now.")
            return
        except (OSError, ValueError) as error:  # a file that can't be read, or isn't TOML
            await call.reply(f"Couldn't read {config.name}: {error}")
            return
        now, length = decide(was)
        timer = self._timer(call.server, toggle.key)
        if timer is not None and bool(timer["value"]) != was:  # changed since by someone else: not this timer's now
            self._drop_timer(call.server, toggle.key)
            timer = None
        if now == was and timer is None:
            await call.reply(f"{_capital(toggle.label)} is already {_state(now)} on {call.server}.")
            return
        # Whether the block set the key itself before the toggles first touched it (a key a toggle wrote isn't the
        # host's): switched back by hand to what [defaults] gives, a key only the toggles wrote comes out again.
        origin = self._toggled(call.server, toggle.key)
        own_before = bool(origin["had_own"]) if origin is not None else had_own
        take_out = not own_before and length is None and now == _inherited(data, call.server, toggle.key)
        # (Already so with a timer running: a time sets a new one, none makes it stay.)
        if now != was:
            try:
                await asyncio.to_thread(_take_out if take_out else _write, config, call.server, toggle.key, now)
                if take_out:
                    self._forget_toggled(call.server, toggle.key)
                elif origin is None:
                    self._remember_toggled(call.server, toggle.key, had_own)
            except LookupError:
                await call.reply(f"Couldn't: {config.name} has no [[server]] block named {call.server} now.")
                return
            except (OSError, ValueError) as error:  # a refused edit (TomlEditError) is an OSError
                await call.reply(f"Couldn't change {config.name}: {error}")
                return
            if data.get("auto_reload") is not False:  # on by default (0.9.18 on): the server answers
                self._written[call.server.lower()] = (call, time.monotonic())
        if length:
            keep = now == was and timer is not None  # a new time for the same change: put back what was before it
            self._set_timer(call.server, toggle.key, now, bool(timer["before"]) if keep else was,
                            bool(timer["had_own"]) if keep else own_before, length, call.player.name)
        else:
            self._drop_timer(call.server, toggle.key)
        spoken = f" for {describe_time(length)}" if length else ""
        self.log.info("[%s] %s turned %s %s%s (%s%s)", call.server, call.player.name, toggle.label, _state(now), spoken,
                      call.prefix, call.name)
        line = callout("voice on" if now else "voice off",
                       f"{call.player.name} turned {toggle.label} {_state(now)}{spoken}.")
        if not await self._say(call.server, line):
            await call.reply(line)  # the console wouldn't take a [Server] line: the admin reads it at least
        if notes := self._notes(call, data, toggle, now):
            await call.reply(" ".join(f"Note: {note}." for note in notes))

    def _notes(self, call: CommandCall, data: dict[str, Any], toggle: Toggle, now: bool) -> list[str]:
        """What else keeps players quiet, or keeps this from applying, for the admin who toggled."""
        notes = []
        server = call.server
        if toggle.key in CHANNELS:
            if not effective(data, "voice", server):
                back = self._timer(server, "voice")
                notes.append("voice chat is off on this server altogether (voice = false"
                             + (f", back on in {describe_seconds(back['until'] - time.time())}" if back else "")
                             + "), so nobody hears any key until it's on")
            elif not now and not any(effective(data, key, server) for key in CHANNELS if key != toggle.key):
                notes.append("V, B and N are all off now: nobody hears anyone")
        elif now and not any(effective(data, key, server) for key in CHANNELS):
            notes.append(f"V, B and N are all off, so nobody hears anyone yet: {call.prefix}toggleglobal, "
                         f"{call.prefix}toggleteam and {call.prefix}toggleproxy turn them on")
        if data.get("auto_reload") is False:
            notes.append(f"its file has auto_reload off, so this (and putting it back) applies at an owner's "
                         f"{call.prefix}reload or the next restart")
        return notes

    async def _say(self, server: str, line: str) -> bool:
        """A [Server] line everyone on the server reads; False when it's not connected or the console refused."""
        state = self.ctx.servers.get(server)
        if state is None or not state.connected:
            return False
        try:
            await self.ctx.say(server, line)
        except RconError as error:
            self.log.info("[%s] couldn't say %r: %s", server, line, error)
            return False
        return True

    # Putting it back -------------------------------------------------------------------------

    def _timer(self, server: str, key: str) -> sqlite3.Row | None:
        rows = self.ctx.db.query("SELECT * FROM commands_timed_settings WHERE server = ? AND key = ?", (server, key))
        return rows[0] if rows else None

    def _set_timer(self, server: str, key: str, value: bool, before: bool, had_own: bool, length: str,
                   by: str) -> None:
        self._drop_timer(server, key)
        now = time.time()
        self.ctx.db.execute("INSERT INTO commands_timed_settings (server, key, value, before, had_own, until, length, "
                            "by, time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (server, key, int(value), int(before), int(had_own), now + seconds_of(length),
                             describe_time(length), by, now))

    def _drop_timer(self, server: str, key: str) -> None:
        self.ctx.db.execute("DELETE FROM commands_timed_settings WHERE server = ? AND key = ?", (server, key))

    def _toggled(self, server: str, key: str) -> sqlite3.Row | None:
        rows = self.ctx.db.query("SELECT * FROM commands_toggled WHERE server = ? AND key = ?", (server, key))
        return rows[0] if rows else None

    def _remember_toggled(self, server: str, key: str, had_own: bool) -> None:
        self.ctx.db.execute("INSERT OR REPLACE INTO commands_toggled (server, key, had_own, time) VALUES (?, ?, ?, ?)",
                            (server, key, int(had_own), time.time()))

    def _forget_toggled(self, server: str, key: str) -> None:
        self.ctx.db.execute("DELETE FROM commands_toggled WHERE server = ? AND key = ?", (server, key))

    @every(seconds=2)
    async def toggles_due(self) -> None:
        """Put back what a timed toggle changed, once its time is up."""
        for row in self.ctx.db.query("SELECT * FROM commands_timed_settings WHERE until <= ? ORDER BY until",
                                     (time.time(),)):
            await self._put_back(row)

    async def _put_back(self, row: sqlite3.Row) -> None:
        server, key = str(row["server"]), str(row["key"])
        toggle = BY_KEY.get(key)
        config = self.ctx.config_path(server)
        if toggle is None or config is None:
            self.log.warning("[%s] %s's timer dropped: Huragok has no such server or setting now", server, key)
            self._drop_timer(server, key)
            return
        before = bool(row["before"])
        try:
            done = await asyncio.to_thread(_restore, config, server, key, bool(row["value"]), before,
                                           bool(row["had_own"]))
        except LookupError:
            self.log.warning("[%s] %s not put back: %s has no [[server]] block named it now", server, toggle.label,
                             config.name)
            self._drop_timer(server, key)
            return
        except (OSError, ValueError) as error:
            self.log.warning("[%s] couldn't put %s back %s (tried again in a minute): %s", server, toggle.label,
                             _state(before), error)
            self.ctx.db.execute("UPDATE commands_timed_settings SET until = ? WHERE server = ? AND key = ?",
                                (time.time() + RETRY_SECONDS, server, key))
            return
        self._drop_timer(server, key)
        if not done:
            self.log.info("[%s] %s was changed since %s's toggle: left as it is", server, toggle.label, row["by"])
            return
        try:
            own_now = (await asyncio.to_thread(_read, config, server, key))[2]
        except (LookupError, OSError, ValueError):
            own_now = True
        if not row["had_own"] and not own_now:
            self._forget_toggled(server, key)  # its own key came out again: as before any toggle
        # (Put back to a value [defaults] doesn't give, the key stays, and stays the toggles': the record stays.)
        self.log.info("[%s] %s back %s after %s (%s's toggle)", server, toggle.label, _state(before), row["length"],
                      row["by"])
        await self._say(server, callout("voice on" if before else "voice off", f"{_capital(toggle.label)} is "
                                        f"{'back on' if before else 'off again'} after {row['length']}."))

    # The server's answer -----------------------------------------------------------------------

    @on("rcon.control")
    async def toggle_reload_answer(self, event: Event) -> None:
        """The server re-read dedicated.toml after a toggle: tell the admin who toggled when it didn't take it."""
        if event.get("action") != "reload" or not event.server:
            return
        written = self._written.pop(event.server.lower(), None)
        if written is None or time.monotonic() - written[1] > RELOAD_ANSWER_SECONDS or event.get("ok", True):
            return
        why = " ".join(str(event.get("text") or "no reason given").split())[:200]
        await written[0].reply(f"{event.server} didn't apply it and keeps its settings: {why}. dedicated.toml needs "
                               "the host's fix.")


def _words(args: list[str]) -> tuple[bool | None, str | None]:
    """[on | off] [time], either order: (on, off or None: switch it over; the time or None). "" for the time: the
    words aren't that."""
    wanted: bool | None = None
    length: str | None = None
    for word in (a.lower() for a in args):
        if word in ("on", "off") and wanted is None:
            wanted = word == "on"
        elif TIME.fullmatch(word) and length is None and seconds_of(word) > 0:
            length = word
        else:
            return None, ""
    return wanted, length


def _block_named(data: dict[str, Any], server: str) -> dict[str, Any]:
    blocks = data.get("server") if isinstance(data.get("server"), list) else []
    block = next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == server.lower()), None)
    if block is None:
        raise LookupError(server)
    return block


def _read(config: Path, server: str, key: str) -> tuple[dict[str, Any], bool, bool]:
    """(the file, the setting in force for this server, whether its own block sets it). LookupError: no block of that
    name in the file."""
    data = read_toml(config)
    block = _block_named(data, server)
    return data, bool(current(data, find(key, "server"), server)[0]), key in block


def _write(config: Path, server: str, key: str, value: bool) -> None:
    _block_named(read_toml(config), server)  # never a new block (tomledit adds one for a name it can't find)
    write(config, server, find(key, "server"), value)


def _inherited(data: dict[str, Any], server: str, key: str) -> bool:
    """What the server gets for `key` without its own block's value: [defaults]'s, else the program's own."""
    blocks = [({k: v for k, v in b.items() if k != key}
               if isinstance(b, dict) and str(b.get("name", "")).lower() == server.lower() else b)
              for b in (data.get("server") if isinstance(data.get("server"), list) else [])]
    return bool(current({**data, "server": blocks}, find(key, "server"), server)[0])


def _take_out(config: Path, server: str, key: str, value: bool) -> None:
    """The block's own key out, so [defaults] applies again; written after all if that wouldn't give `value`."""
    _block_named(read_toml(config), server)
    setting = find(key, "server")
    reset(config, server, setting)
    if _read(config, server, key)[1] != value:
        write(config, server, setting, value)


def _restore(config: Path, server: str, key: str, value: bool, before: bool, had_own: bool) -> bool:
    """Put a setting back as it was before a timed toggle. False: it isn't what the toggle made it any more (someone
    changed it since), so it's left alone. A block that didn't set it itself has its own value taken out again, so
    [defaults] applies, unless that would give the wrong value."""
    _, now, _ = _read(config, server, key)
    if now != value:
        return False
    setting = find(key, "server")
    if had_own:
        write(config, server, setting, before)
        return True
    reset(config, server, setting)
    if _read(config, server, key)[1] != before:
        write(config, server, setting, before)
    return True
