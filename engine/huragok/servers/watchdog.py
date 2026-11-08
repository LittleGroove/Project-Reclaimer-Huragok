"""watchdog: restart a server that has died, stopped letting players in or got stuck; notice one running slowly.

The core's keepalive handles a server that has stopped answering; this handles one
that answers but is broken, or runs badly. Each minute it looks for these cases:

  dead       every player dropped at once ("Host roster lost 112 of 112 players in
             one update (0 now present)"): seen on the live server ~38 s after a
             game ended, after which it let nobody back in. A drop that leaves
             players on doesn't count (drop_left): the one seen mid-game (70 of 73)
             recovered by itself.
  no_joins   different players keep trying to get in and none of them does, while there's room.
             On the live log (7,652 connections) this happened only while the server
             was dead, also on versions that don't say "Host roster lost": one sat
             dead that way for six hours. Joining waits for the map to load, so a
             connection gets JOIN_GRACE to finish. A join is the log's roster line
             (or an ODST server's own), or a player the console's join event
             admitted who was still on JOIN_GRACE after connecting: that event
             comes as the player is let in, and a dead server keeps letting players
             in and dropping each about 20 s later (none of 300 such connections on
             the live log's dead stretches lasted 80 s). On 0.10 a connection is
             the log's "[online] connected to 0x… at …" (a player's machine), and
             there's no roster line: a join is a player let in who stayed, the
             console's join event or the log's own "[server] player #N joined: …"
             (one admission when both say it), with the log's "[server] player #N
             (…) left" as a leave, so it's judged with the console away too.
             Only real tries count, by player: a player trying again and again
             counts once (by their player ID on 0.9, their machine's address on
             0.10), and 0.10's "[server] a visit (mods or a bot) from …" makes
             the connection just made from that address no try at all. Every
             game looks at the server's mods that way before it joins, and a
             look from the server list stops there: counted as tries, a busy
             server's visits restarted the LG Test Server three times in 16
             minutes (7 Nov 2026), each time with players getting in.
  stuck      on the scoreboard after a game with players waiting, for stuck_minutes.
             When healthy, the live server had its next game loaded and running
             within 109 s of a game ending (51 game ends; 10 s of that is the load).
             Judged only while the console is connected: the phase and players
             are the console's, and frozen while it's away.
  slow       falling behind (the server's own "under N frames a second" lines, 54 on
             0.9.18-0.9.20) for a good share of several minutes
  very_slow  falling behind nearly all the time, minutes on end
  memory     the server program using more than memory_mb, having grown past it: what the run used
             once settled was under it (a limit below what a fresh server uses restarted it after every game)

Each case is watch (log it and show it in the window), act (restart after the game;
right away when no game is on or nobody is playing) or actnow (restart right away,
warning the players NOW_WARNING seconds first when they can be warned). mode =
"watch" makes every case watch. By default dead, no_joins and stuck are actnow;
memory (off until memory_mb is set), slow and very_slow watch: a full server is
slow anyway, and a restart doesn't make it faster. The memory and frame cases count only
once a run has settled ([restarts] min_uptime_seconds): a fresh server loads its maps. Replayed against the live
server's log (27 starts, 100+ players on a 2 GB map), the frame rules would have
restarted it mid-game 372 times and faulted it in 22 different hours. With the
next export added (46 starts), the dead rule fired exactly on its 13 post-game
deaths and nowhere else; four drops that left players on kept letting them in.

A server that freezes outright (no status.json, no RCON) is the core's keepalive's.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.events import Event
from ..core.part import Part, command, every, on
from ..lore import NOTICES, SUPERINTENDENT
from .backends import LifecycleUnavailable, memory_mb

if TYPE_CHECKING:
    from ..players.router import CommandCall

ACTIONS = ("watch", "act", "actnow")  # in order of strength
CASES = ("dead", "no_joins", "stuck", "slow", "very_slow", "memory")
CRITICAL = {"dead", "no_joins", "stuck", "very_slow"}  # shown as critical; the others as degraded
REASONS = {  # what players read when the server restarts for it
    "dead": "the server dropped its players", "no_joins": "nobody can join", "stuck": "the game is stuck",
    "very_slow": "the server is struggling", "slow": "the server is struggling",
    "memory": "the server is using too much memory",
}
NUMBERS = ("report_minutes", "drop_players", "drop_share", "drop_left", "no_joins_connections", "no_joins_minutes",
           "stuck_minutes", "slow_share", "slow_minutes", "slow_window_minutes", "very_slow_share",
           "very_slow_minutes", "memory_mb")
SHARES = ("drop_share", "slow_share", "very_slow_share")
OLD_NAMES = {"slow_frames"}  # 0.2.1's single switch for both frame cases, still read
JOIN_GRACE = 120.0  # seconds a connection gets to become a join: loading a 2 GB map takes a while
CONNECT_SLACK = 5.0  # the console's join event may come a moment before the log's line for the same connection
VISIT_SLACK = 10.0  # 0.10's visit line comes right after the connection it's about
DROP_MEMORY = 300.0  # a roster loss counts as dead for this long
NOW_WARNING = 30.0  # seconds of warning before an actnow restart, when there's anyone to warn


@dataclass
class Sample:
    time: float
    share: float  # of the minute spent falling behind (the server's "under N frames a second")
    longest_ms: int
    players: int


@dataclass
class Connection:
    """A player's machine connecting: 0.9's "X connected from …" (their player ID) or 0.10's "connected to 0x… at …"
    (their address, with the port)."""
    time: float
    id: str = ""
    address: str = ""

    @property
    def who(self) -> str:
        """The player trying, whichever connection it is: their ID, else their machine's address without the port
        (0.10's game looks at the mods from one port and joins from another)."""
        return self.id or _host(self.address)


def _host(address: str) -> str:
    """An address without its port: 203.0.113.9:51234 -> 203.0.113.9, [2001:db8::1]:5 -> 2001:db8::1."""
    if address.startswith("["):
        return address[1:].partition("]")[0]
    host, colon, port = address.rpartition(":")
    return host if colon and ":" not in host and port.isdigit() else address


@dataclass
class Admission:
    """A player the console's join event let in, and when they left (their leave, or being let in again)."""
    time: float
    number: int | None
    id: str
    left: float | None = None

    def same(self, number: int | None, player_id: str) -> bool:
        return (self.number is not None and self.number == number) or bool(self.id and self.id == player_id)


def _number(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


STOPPED_WORDS = {"idle": "stopped", "faulted": "faulted: it won't start again by itself", "backoff": "waiting to try starting again"}


class Watchdog(Part):
    persona = SUPERINTENDENT
    old_options = frozenset(OLD_NAMES)
    name = "watchdog"
    description = "Restarts a server that died, lets nobody in or got stuck after a game; notices a slow one"
    enabled_by_default = True
    defaults: dict[str, Any] = {
        "mode": "act",                # act | watch: watch makes every case below watch
        "report_minutes": 15,         # a health line in the log this often (0: never)
        "dead": "actnow",             # every player dropped at once
        "drop_players": 8,            # a roster loss counts when at least this many players went…
        "drop_share": 0.5,            # …and at least this share of those who were on…
        "drop_left": 0,               # …and no more than this many were left
        "no_joins": "actnow",         # players keep connecting, nobody gets in
        "no_joins_connections": 3,    # this many different players trying (0: never checked)…
        "no_joins_minutes": 5,        # …in this many minutes, and none of them joined
        "stuck": "actnow",            # on the scoreboard after a game, players waiting
        "stuck_minutes": 3,           # this long (0: never checked)
        "slow": None,                 # watch, unless slow_frames (0.2.1) says act
        "slow_share": 0.25,           # a minute is slow when this share of it was falling behind…
        "slow_minutes": 3,            # …and this many of the last slow_window_minutes were slow
        "slow_window_minutes": 5,
        "very_slow": None,            # watch, unless slow_frames (0.2.1) says act: then actnow
        "very_slow_share": 0.9,       # this share of each minute falling behind…
        "very_slow_minutes": 3,       # …this many minutes in a row
        "memory": "watch",            # the server program using more than memory_mb (the host, 2 Nov 2026: warn unless turned on)
        "memory_mb": 0,               # 0: never checked
    }

    # Settings ------------------------------------------------------------------------------

    @staticmethod
    def resolve(config: dict[str, Any]) -> dict[str, Any]:
        """The frame cases from 0.2.1's slow_frames when they aren't set themselves."""
        config = dict(config)
        old = config.get("slow_frames")
        if config.get("slow") is None:
            config["slow"] = "act" if old == "act" else "watch"
        if config.get("very_slow") is None:
            config["very_slow"] = "actnow" if old == "act" else "watch"
        return config

    @staticmethod
    def mistakes(config: dict[str, Any]) -> list[str]:
        found = []
        if config.get("mode") not in ("act", "watch"):
            found.append('mode must be "act" or "watch"')
        for case in CASES:
            if config.get(case) not in ACTIONS:
                found.append(f'{case} must be "watch", "act" or "actnow"')
        if config.get("slow_frames") not in (None, "watch", "act"):
            found.append('slow_frames must be "watch" or "act"')
        for key in NUMBERS:
            value = config.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                found.append(f"{key} must be a number, 0 or more")
            elif key in SHARES and value > 1:
                found.append(f"{key} is a share of a minute or of the players: 0 to 1")
        return found

    @classmethod
    def preflight(cls, config: dict[str, Any], home: Path) -> list[str]:
        if mistakes := cls.mistakes(cls.resolve({**cls.defaults, **config})):
            raise ValueError("; ".join(mistakes))
        return [f"'{key}' isn't a watchdog setting, so it does nothing"
                for key in sorted(set(config) - set(cls.defaults) - OLD_NAMES)]

    def check_config(self) -> None:
        self.config.update(self.resolve(self.config))
        if mistakes := self.mistakes(self.config):
            raise ValueError("; ".join(mistakes))

    # What it hears -------------------------------------------------------------------------

    async def setup(self) -> None:
        self.samples: dict[str, deque[Sample]] = {}
        self.upload: dict[str, Event] = {}
        self.drops: dict[str, Event] = {}
        self.connects: dict[str, deque[Connection]] = {}  # tries to get in (not 0.10's visits)
        self.joins: dict[str, deque[float]] = {}  # the log's roster lines (and ODST's)
        self.admitted: dict[str, deque[Admission]] = {}  # the console's join events (and 0.10's log lines for them)
        self.memory: dict[str, float] = {}
        # What each run used once settled: the memory case is growth past memory_mb from there, not a limit below what
        # a fresh server uses (that restarted it after every game, the host's call 2 Nov 2026)
        self.memory_base: dict[str, float] = {}
        self.found: dict[str, dict[str, str]] = {}  # the cases that apply now: case -> what was seen
        self.held: dict[str, set[str]] = {}  # cases that came up while the server was busy: looked at again
        self.level: dict[str, str] = {name: "healthy" for name in self.ctx.servers}
        self.last_report = time.monotonic()

    @on("log.falling_behind")
    async def falling_behind(self, event: Event) -> None:
        window = max(1, int(event.get("window", 60)))
        self.samples.setdefault(event.server or "", deque(maxlen=120)).append(
            Sample(event.time, int(event.get("behind", 0)) / window, int(event.get("longest_ms", 0)),
                   int(event.get("players", 0))))

    @on("log.upload")
    async def upload(self, event: Event) -> None:
        self.upload[event.server or ""] = event

    @on("log.connected")
    async def connected(self, event: Event) -> None:
        self.connects.setdefault(event.server or "", deque(maxlen=500)).append(
            Connection(event.time, str(event.get("id") or "").lower(), str(event.get("address") or "")))

    @on("log.visit")
    async def visited(self, event: Event) -> None:
        """0.10: the connection just made from this address was a look at the server's mods (or a bot's), no try."""
        address = str(event.get("address") or "")
        connects = self.connects.get(event.server or "")
        if not address or not connects:
            return
        for connection in reversed(connects):
            if connection.address == address and abs(event.time - connection.time) <= VISIT_SLACK:
                connects.remove(connection)
                return

    @on("log.joined", "rcon.join", "log.admitted")
    async def joined(self, event: Event) -> None:
        """A player got in (the log's roster line, or ODST's own), or the server let one in (the console's join event,
        the one an ODST server is sure to send whatever its log says, or 0.10's log line for it): that one counts once
        they've stayed (findings)."""
        server = event.server or ""
        if event.name == "log.joined":
            self.joins.setdefault(server, deque(maxlen=500)).append(event.time)
            return
        number, player_id = _number(event.get("number")), str(event.get("id") or "").lower()
        admitted = self.admitted.setdefault(server, deque(maxlen=500))
        for earlier in admitted:
            if earlier.left is None and earlier.same(number, player_id):
                if (abs(event.time - earlier.time) <= CONNECT_SLACK and earlier.number == number
                        and earlier.id == player_id):
                    return  # the same admission: the console's join event and 0.10's log line both say it
                earlier.left = event.time  # let in again: whatever they were on before has ended
        admitted.append(Admission(event.time, number, player_id))

    @on("rcon.leave", "log.left")
    async def left(self, event: Event) -> None:
        number, player_id = _number(event.get("number")), str(event.get("id") or "").lower()
        for admission in reversed(self.admitted.get(event.server or "", ())):
            if admission.left is None and admission.same(number, player_id):
                admission.left = event.time
                return

    @on("log.roster_lost")
    async def roster_lost(self, event: Event) -> None:
        server = event.server or ""
        lost, of = int(event.get("lost", 0)), int(event.get("of", 0))
        self.log.warning("[%s] the server dropped %d of %d players at once (%s now present)%s", server, lost, of,
                         event.get("present", "?"), f": {event.get('details')}" if event.get("details") else "")
        if lost < self.config["drop_players"] or lost < of * self.config["drop_share"]:
            return
        if int(event.get("present", 0)) > self.config["drop_left"]:
            self.log.info("[%s] players are still on, so it's left to recover by itself", server)
            return
        self.drops[server] = event
        await self._update(server)

    @on("server.restarted", "server.started")
    async def fresh_start(self, event: Event) -> None:
        """A new run of the server: what the old one did says nothing about it."""
        server = event.server or ""
        for history in (self.drops, self.samples, self.connects, self.joins, self.admitted, self.memory, self.found,
                        self.held, self.memory_base):
            history.pop(server, None)

    def _settled(self, server: str) -> bool:
        """Whether the server's run has been up [restarts] min_uptime_seconds: before that its memory and frames are
        a fresh server's, loading its maps. A run whose start isn't known counts as settled."""
        started = self.ctx.run_started(server)
        if started is None:
            return True
        return time.time() - started >= float(self.ctx.restart_setting(server, "min_uptime_seconds") or 0)

    # Deciding ------------------------------------------------------------------------------

    def findings(self, server: str, now: float | None = None) -> dict[str, str]:
        """The cases that apply to the server right now: case -> what was seen."""
        now = now or time.time()
        cfg = self.config
        state = self.ctx.server(server)
        found: dict[str, str] = {}
        drop = self.drops.get(server)
        if drop and now - drop.time <= DROP_MEMORY:
            found["dead"] = f"dropped {drop.get('lost')} of {drop.get('of')} players at once"
        if cfg["no_joins_connections"]:
            start = now - JOIN_GRACE - cfg["no_joins_minutes"] * 60
            tried = [c for c in self.connects.get(server, ()) if start <= c.time <= now - JOIN_GRACE]
            # Each player once, however often they tried; a connection that names nobody counts by itself.
            players = {c.who or f"#{n}" for n, c in enumerate(tried)}
            got_in = (any(t >= start for t in self.joins.get(server, ()))
                      or self._stayed(server, state, start, now))
            # A full server turns players away, so a full roster explains it; one frozen with the console away doesn't.
            room = not state.connected or not state.players_known or state.max_players is None                 or len(state.players) < state.max_players
            if len(players) >= cfg["no_joins_connections"] and not got_in and room:
                found["no_joins"] = (f"{len(players)} players tried to join in {cfg['no_joins_minutes']:g} minutes and "
                                     "none got in")
        # Judged on a picture read since the console connected: back after it dropped, its old "finished" and old
        # players restarted a server at once, with no warning (audit 2026-11, 8.2). Its players are read after its
        # status, in the same refresh, so known players mean a fresh phase too.
        if (cfg["stuck_minutes"] and state.connected and state.players_known and state.phase == "finished"
                and state.phase_since and state.players and now - state.phase_since > cfg["stuck_minutes"] * 60):
            found["stuck"] = f"stuck after a game for {(now - state.phase_since) / 60:.0f} minutes"
        settled = self._settled(server)
        samples = list(self.samples.get(server, ())) if settled else []  # a fresh run's frames don't count
        last = samples[-int(cfg["very_slow_minutes"]):] if cfg["very_slow_minutes"] else []
        if (last and len(last) >= cfg["very_slow_minutes"] and all(s.share >= cfg["very_slow_share"] for s in last)
                and now - last[0].time <= cfg["very_slow_minutes"] * 60 + 60):
            found["very_slow"] = f"falling behind for {cfg['very_slow_minutes']:g} minutes running"
        recent = [s for s in samples if now - s.time <= cfg["slow_window_minutes"] * 60 + 30]
        slow = [s for s in recent if s.share >= cfg["slow_share"]]
        if cfg["slow_minutes"] and len(slow) >= cfg["slow_minutes"]:
            found["slow"] = (f"slow in {len(slow)} of the last {cfg['slow_window_minutes']:g} minutes "
                             f"(longest frame {max(s.longest_ms for s in slow)} ms)")
        memory, settled_at = self.memory.get(server), self.memory_base.get(server)
        if (cfg["memory_mb"] and settled and memory and settled_at is not None
                and settled_at <= cfg["memory_mb"] < memory):
            found["memory"] = f"using {memory:.0f} MB, over {cfg['memory_mb']:g} MB (it used {settled_at:.0f} MB settled)"
        return found

    def _stayed(self, server: str, state: Any, start: float, now: float) -> bool:
        """Whether a player the console let in since `start` was still on JOIN_GRACE after they connected.

        Measured from their own "connected from" line (the same player ID, after their last admission) when the log
        has one, else from the admission itself. Still on: no leave by then, and, with the console connected, still
        in its player list (a leave can be missed). With the console away, a player not seen leaving counts.
        """
        connects = self.connects.get(server, ())
        last: dict[str, float] = {}  # player ID -> their previous admission
        for admission in self.admitted.get(server, ()):
            before = last.get(admission.id)
            if admission.id:
                last[admission.id] = admission.time
            if admission.time < start:
                continue
            base = admission.time
            if admission.id:
                linked = [c.time for c in connects if c.id == admission.id
                          and (before is None or c.time > before + CONNECT_SLACK)
                          and c.time <= admission.time + CONNECT_SLACK]
                if linked:
                    base = max(min(base, linked[-1]), admission.time - JOIN_GRACE)
            until = base + JOIN_GRACE
            if now < until:
                continue  # too soon to say
            if admission.left is not None:
                if admission.left >= until:
                    return True
            elif not state.connected or self._on(state, admission):
                return True
        return False

    @staticmethod
    def _on(state: Any, admission: Admission) -> bool:
        player = state.players.get(admission.number) if admission.number is not None else None
        if player is not None and (not admission.id or player.id == admission.id):
            return True
        return bool(admission.id) and state.player_by_id(admission.id) is not None

    @staticmethod
    def level_of(found: dict[str, str]) -> str:
        return "critical" if CRITICAL & set(found) else "degraded" if found else "healthy"

    @property
    def reasons(self) -> dict[str, list[str]]:
        return {server: list(found.values()) for server, found in self.found.items()}

    @every(seconds=60)
    async def evaluate(self) -> None:
        for server in self.ctx.servers:
            if self.config["memory_mb"]:
                memory = self._memory_mb(self._pid(server))
                if memory:
                    self.memory[server] = memory
                    if server not in self.memory_base and self._settled(server):
                        self.memory_base[server] = memory
                        if memory > self.config["memory_mb"]:
                            self.log.warning("[%s] uses %.0f MB once settled, over [watchdog] memory_mb (%g): the "
                                             "memory case doesn't count for this run. Set memory_mb above what a "
                                             "fresh server uses", server, memory, self.config["memory_mb"])
            await self._update(server)
        minutes = float(self.config["report_minutes"])
        if minutes and time.monotonic() - self.last_report >= minutes * 60:
            self.last_report = time.monotonic()
            for server in self.ctx.servers:
                self.log.info("[%s] %s", server, self.summary(server))

    async def _update(self, server: str) -> None:
        found = self.findings(server)
        level, previous = self.level_of(found), self.level.get(server, "healthy")
        # Only cases that weren't there a minute ago act: a full server is very_slow all game, and dying then
        # must still count.
        new = {case: reason for case, reason in found.items() if case not in self.found.get(server, {})}
        # A case that came up while the server was busy (a countdown, a start) gets another look each minute
        # while it lasts: the countdown may be called off, and the server is still dead.
        again = {case: found[case] for case in self.held.pop(server, set()) if case in found and case not in new}
        self.found[server], self.level[server] = found, level
        if level != previous:
            self.log.warning("[%s] %s → %s%s", server, previous, level,
                             f": {'; '.join(found.values())}" if found else "")
            self.ctx.emit("health", server, level=level, previous=previous, reasons=list(found.values()))
        elif new:
            self.log.warning("[%s] still %s, and now also: %s", server, level, "; ".join(new.values()))
        if new or again:
            if not await self._act(server, {**again, **new}):
                self.held[server] = {*again, *new}

    async def _act(self, server: str, new: dict[str, str]) -> bool:
        """Restart for these cases, as their settings say. False: the server was busy (a countdown, a stop or a
        start under way), so nothing was decided and they're looked at again next minute."""
        return await self._decide(server, new)

    async def _decide(self, server: str, new: dict[str, str]) -> bool:
        cfg = self.config
        strongest = max((cfg[case] for case in new), key=ACTIONS.index)
        when = "now" if strongest == "actnow" else "after_game"
        if strongest == "watch":
            settings = ", ".join(f'{case} = "watch"' for case in new)
            self.log.info("[%s] not restarted, as [watchdog] %s", server, settings)
            return True
        if cfg["mode"] != "act":
            self.log.info("[%s] would restart (%s) — [watchdog] mode = \"watch\"", server, when.replace("_", " "))
            return True
        lifecycle = self.ctx.lifecycle
        if server in lifecycle.busy:
            return False  # mid-flight (a countdown, a stop, a start): looked at again next minute
        if lifecycle.can_control and lifecycle.is_stopped(server):
            # Stopped by a person or with everything else, waiting out a failed start, or faulted: a restart now would
            # start it again, undo that stop and hold a shutdown up. The case goes with the run it was about.
            self.log.info("[%s] not restarted: it isn't running any more", server)
            return True
        planned = lifecycle.planned.get(server)
        if planned is not None and (when != "now" or planned.when == "now"):
            return True  # already planned; only actnow is worth changing a plan for
        acting = [case for case in CASES if case in new and cfg[case] != "watch"]
        drop = self.drops.get(server)
        # Nobody to warn when the server dropped everyone, or when they're stuck on the scoreboard.
        nobody = ("dead" in acting and drop is not None and not drop.get("present")) or "stuck" in acting
        if when == "now":
            self.log.warning("[%s] %s Restarting it now: %s", server, NOTICES["emergency"], REASONS[acting[0]])
        try:
            await lifecycle.restart(server, when=when, countdown=(0 if nobody else NOW_WARNING) if when == "now"
                                    else None, reason=REASONS[acting[0]], by="Watchdog")
        except LifecycleUnavailable as error:
            self.log.warning("[%s] can't restart: %s", server, error)
        return True

    # Saying how it is ---------------------------------------------------------------------

    def summary(self, server: str) -> str:
        state = self.ctx.server(server)
        lifecycle = self.ctx.lifecycle
        if lifecycle.can_control and lifecycle.is_stopped(server):  # what it last saw is no use: it isn't running
            return STOPPED_WORDS.get(str(lifecycle.phase(server)), "stopped")
        level = self.level.get(server, "healthy")
        if not state.connected:  # its last players and game are out of date, and "healthy" isn't known
            return " · ".join([level if level != "healthy" else "not answering", "RCON not connected", *self.found.get(server, {}).values()])
        parts = [level,
                 f"{len(state.players)} players",
                 f"{state.phase or 'phase unknown'} {state.map or ''}".strip()]
        if state.rcon_latency_ms is not None:
            parts.append(f"RCON {state.rcon_latency_ms:.0f} ms")
        if state.status_age is not None:
            parts.append(f"status {state.status_age:.0f} s old")
        recent = [s for s in self.samples.get(server, []) if time.time() - s.time <= 3600]
        parts.append(f"slow minutes in the last hour: {len(recent)}")
        memory = self._memory_mb(self._pid(server))
        if memory:
            parts.append(f"memory {memory:.0f} MB")
        upload = self.upload.get(server)
        if upload:
            parts.append(f"upload {upload.get('rate')} {upload.get('unit')}/s")
        if self.found.get(server):
            parts.append("; ".join(self.found[server].values()))
        return " · ".join(parts)

    def _pid(self, server: str) -> int | None:
        """The server's process: the one the backend started (its game runs under it), or the game's own."""
        return self.ctx.pid(server)

    _memory_mb = staticmethod(memory_mb)  # a test stands in for it

    @command("health", help="How this server is running", role="moderator")
    async def health(self, call: CommandCall) -> None:
        await call.reply(self.summary(call.server))
