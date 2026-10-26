"""The messages part: interval messages, said to everyone on a server again and again, each after its own time.

Huragok's folder has an intervalmessages.txt for every server, and each server folder one of its own; a server
says both (intervals.py reads them). Each message's clock starts when Huragok first sees the server up, and a
message comes due once a time after that. Messages with the same time take turns: n of them every 30 minutes go
out 30/n minutes apart, each every 30 minutes. One that comes due while nobody's on, or while the server is down,
is left out until its next time: nobody joins to a pile of them. Edits apply within a few seconds; a message that
was there before an edit keeps its time, a new one starts its clock then.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ...core.part import Part, every
from ...lore import VERGIL
from ...rcon import RconError
from ..router import wrap
from .intervals import FILE, IntervalMessage, MessageError, describe_every, own_file, read_messages

TICK_SECONDS = 5


@dataclass
class _Read:
    """One file as last read."""

    mtime: float | None  # None: not there
    messages: list[IntervalMessage]
    problems: list[str]


@dataclass
class _Due:
    message: IntervalMessage
    due: float  # the clock's time it's next said
    everywhere: bool  # from the file for every server (False: the server's own)


class Messages(Part):
    persona = VERGIL
    name = "messages"
    description = "Interval messages, from intervalmessages.txt: for every server, and each server's own"
    always_on = True

    def check_config(self) -> None:
        self.clock = time.monotonic
        self.every_path = self.ctx.home / FILE
        self.files: dict[Path, _Read] = {}
        self.plans: dict[str, list[_Due]] = {}  # each server seen up: its messages, each with when it's next due

    async def setup(self) -> None:
        own = {server: len(self._read(path, server).messages) for server in self.ctx.servers
               if (path := self.own_path(server))}
        every_server = len(self._read(self.every_path).messages)
        self.log.info("interval messages: %s for every server%s", every_server,
                      "".join(f" · {server}: {count} of its own" for server, count in own.items() if count))

    def own_path(self, server: str) -> Path | None:
        """The server's own intervalmessages.txt (in its folder), or None for one with no folder of its own here."""
        folder = self.ctx.server_option(server, "folder")
        return own_file(folder, self.ctx.home) if isinstance(folder, Path) else None

    def _read(self, path: Path, server: str | None = None) -> _Read:
        """The file's messages, read again only when it's changed. One that can't be read right now (an editor
        holding it as it saves) keeps what it had; its problems are logged once, as it's read."""
        try:
            mtime: float | None = path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        except OSError:
            return self.files.get(path) or _Read(None, [], [])
        known = self.files.get(path)
        if known is not None and known.mtime == mtime:
            return known
        where = f"[{server}] " if server else ""
        try:
            messages, problems = read_messages(path) if mtime is not None else ([], [])
        except MessageError as error:
            self.log.error("%s%s not read, keeping its messages as they were: %s", where, path.name, error)
            read = _Read(mtime, known.messages if known else [], [str(error)])
            self.files[path] = read
            return read
        except OSError as error:
            if known is None or known.problems != [str(error)]:
                self.log.warning("%s%s can't be read right now (%s); trying again", where, path.name,
                                 error.strerror or error)
            read = _Read(known.mtime if known else None, known.messages if known else [], [str(error)])
            self.files[path] = read
            return read
        for problem in problems:
            self.log.warning("%s%s (left out; the others are said)", where, problem)
        if known is not None:
            self.log.info("%s%s reloaded: %s interval message%s", where, path.name, len(messages),
                          "" if len(messages) == 1 else "s")
        read = _Read(mtime, messages, problems)
        self.files[path] = read
        return read

    def messages_for(self, server: str) -> list[tuple[IntervalMessage, bool]]:
        """What the server says: the messages for every server, then its own; (message, for every server)."""
        own = self.own_path(server)
        return ([(m, True) for m in self._read(self.every_path).messages]
                + ([(m, False) for m in self._read(own, server).messages] if own else []))

    def _plan(self, old: list[_Due], messages: list[tuple[IntervalMessage, bool]], now: float) -> list[_Due]:
        """Each message with when it's next said: one that was there keeps its time; a new one is first said a part
        of its time from now, by its turn among the messages with the same time (two every 30m: 15 and 30 minutes)."""
        kept: dict[tuple[int, str], list[float]] = {}
        for due in old:
            kept.setdefault((due.message.seconds, due.message.text), []).append(due.due)
        sizes: dict[int, int] = {}
        for message, _ in messages:
            sizes[message.seconds] = sizes.get(message.seconds, 0) + 1
        turns: dict[int, int] = {}
        plan = []
        for message, everywhere in messages:
            turn = turns[message.seconds] = turns.get(message.seconds, 0) + 1
            times = kept.get((message.seconds, message.text))
            due = times.pop(0) if times else now + message.seconds * turn / sizes[message.seconds]
            plan.append(_Due(message, due, everywhere))
        return plan

    @every(seconds=TICK_SECONDS)
    async def tick(self) -> None:
        now = self.clock()
        for server in [s for s in self.plans if s not in self.ctx.servers]:
            del self.plans[server]
        for server, state in list(self.ctx.servers.items()):
            if server not in self.plans and not state.connected:
                continue  # its clocks start when it's first seen up
            messages = self.messages_for(server)
            old = self.plans.get(server, [])
            if [(d.message, d.everywhere) for d in old] != messages:
                old = self.plans[server] = self._plan(old, messages, now)
            due = [d for d in old if d.due <= now]
            for entry in due:
                while entry.due <= now:  # its next time; one missed while nobody was on isn't made up for
                    entry.due += entry.message.seconds
            if not due or not state.connected or not state.players:
                continue
            await self._say(server, [d.message for d in due])

    async def _say(self, server: str, messages: list[IntervalMessage]) -> None:
        for message in messages:
            try:
                for line in wrap(message.text):
                    await self.ctx.say(server, line)
            except RconError as error:  # the server not answering: the rest wait for their next time
                self.log.info("[%s] couldn't say an interval message (every %s): %s", server,
                              describe_every(message.seconds), error)
                return
            self.log.debug("[%s] interval message (every %s): %s", server, describe_every(message.seconds),
                           message.text)


__all__ = ["Messages", "TICK_SECONDS"]
