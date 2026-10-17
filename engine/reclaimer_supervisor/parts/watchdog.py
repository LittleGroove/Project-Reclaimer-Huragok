"""watchdog: notice a server getting worse, and restart it at a good moment.

The core's keepalive handles a server that has stopped; this handles one that
runs badly. Each minute it sorts every server into:

  healthy
  degraded  under 60 frames a second for a good share of several recent minutes:
            restart after this game
  critical  barely running for minutes on end, stuck on the scoreboard after a
            game with players waiting, or every player dropped at once ("Host
            roster lost 28 of 28 players in one update (0 now present)"; seen on
            the main server ~38 s after a game ended, after which it lets nobody
            back in): restart now. A drop that leaves players on is only logged:
            the one seen mid-game (70 of 73) recovered by itself.

The thresholds are first guesses. Leave mode = "watch" (the default) for a few
nights: it logs what it would do, so the numbers can be tuned before it acts.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from ..chat import CommandCall
from ..events import Event
from ..part import Part, command, every, on
from ..lifecycle import LifecycleUnavailable


@dataclass
class Sample:
    time: float
    share: float  # of the minute spent under 60 fps
    longest_ms: int
    players: int


class Watchdog(Part):
    name = "watchdog"
    description = "Notices a struggling server and, in act mode, restarts it at the end of a game"
    enabled_by_default = True
    defaults = {
        "mode": "watch",            # watch | act
        "report_minutes": 15,       # a health line in the log this often (0: never)
        "window_minutes": 5,
        "degraded_share": 0.25,     # a minute counts as bad when this share of it was slow…
        "degraded_minutes": 3,      # …and this many of the window's minutes were bad
        "critical_share": 0.9,
        "critical_minutes": 3,      # this many minutes in a row at critical_share
        "stuck_end_minutes": 10,    # on the scoreboard this long with players: stuck
        "drop_players": 8,          # a roster loss counts when at least this many players went…
        "drop_share": 0.5,          # …and at least this share of those who were on…
        "drop_left": 0,             # …and no more than this many were left
    }

    def check_config(self) -> None:
        if self.config["mode"] not in ("watch", "act"):
            raise ValueError("mode must be watch or act")

    async def setup(self) -> None:
        self.samples: dict[str, deque[Sample]] = {name: deque(maxlen=120) for name in self.ctx.servers}
        self.upload: dict[str, Event] = {}
        self.level: dict[str, str] = {name: "healthy" for name in self.ctx.servers}
        self.reasons: dict[str, list[str]] = {name: [] for name in self.ctx.servers}
        self.drops: dict[str, Event] = {}
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
        self.drops.pop(event.server or "", None)

    def assess(self, server: str, now: float | None = None) -> tuple[str, list[str]]:
        now = now or time.time()
        cfg = self.config
        state = self.ctx.server(server)
        samples = self.samples.get(server, deque())
        recent = [s for s in samples if now - s.time <= cfg["window_minutes"] * 60 + 30]
        bad = [s for s in recent if s.share >= cfg["degraded_share"]]
        last = list(samples)[-int(cfg["critical_minutes"]):]
        reasons: list[str] = []
        critical = (len(last) >= cfg["critical_minutes"]
                    and all(s.share >= cfg["critical_share"] for s in last)
                    and now - last[0].time <= cfg["critical_minutes"] * 60 + 60)
        if critical:
            reasons.append(f"under 60 fps for {cfg['critical_minutes']} minutes running")
        if (state.phase == "finished" and state.phase_since and state.players
                and now - state.phase_since > cfg["stuck_end_minutes"] * 60):
            critical = True
            reasons.append(f"stuck after a game for {(now - state.phase_since) / 60:.0f} minutes")
        drop = self.drops.get(server)
        if drop and now - drop.time <= cfg["window_minutes"] * 60:
            critical = True
            reasons.append(f"dropped {drop.get('lost')} of {drop.get('of')} players at once")
        if critical:
            return "critical", reasons
        if len(bad) >= cfg["degraded_minutes"]:
            worst = max(s.longest_ms for s in bad)
            return "degraded", [f"slow in {len(bad)} of the last {cfg['window_minutes']} minutes "
                                f"(longest frame {worst} ms)"]
        return "healthy", []

    @every(seconds=60)
    async def evaluate(self) -> None:
        for server in self.ctx.servers:
            await self._update(server)
        minutes = float(self.config["report_minutes"])
        if minutes and time.monotonic() - self.last_report >= minutes * 60:
            self.last_report = time.monotonic()
            for server in self.ctx.servers:
                self.log.info("[%s] %s", server, self.summary(server))

    async def _update(self, server: str) -> None:
        level, reasons = self.assess(server)
        previous = self.level.get(server, "healthy")
        self.level[server], self.reasons[server] = level, reasons
        if level != previous:
            self.log.warning("[%s] %s → %s%s", server, previous, level, f": {'; '.join(reasons)}" if reasons else "")
            self.ctx.emit("health", server, level=level, previous=previous, reasons=reasons)
            if level != "healthy":
                await self._act(server, level, reasons)

    async def _act(self, server: str, level: str, reasons: list[str]) -> None:
        stuck = any("stuck" in r for r in reasons)
        dropped = any("dropped" in r for r in reasons)
        when = "now" if level == "critical" else "after_game"
        # Nobody to warn when they were stuck, or when the drop left nobody behind.
        countdown = 0 if stuck or (dropped and not self.drops[server].get("present")) else 30
        if self.config["mode"] != "act":
            self.log.info("[%s] would restart (%s) — mode is watch", server, when.replace("_", " "))
            return
        if server in self.ctx.lifecycle.planned or server in self.ctx.lifecycle.busy:
            return
        try:
            await self.ctx.lifecycle.restart(server, when=when, countdown=countdown,
                                             reason="the game is stuck" if stuck else
                                             "the server dropped its players" if dropped else
                                             "the server is struggling",
                                             by="Watchdog")
        except LifecycleUnavailable as error:
            self.log.warning("[%s] can't restart: %s", server, error)

    def summary(self, server: str) -> str:
        state = self.ctx.server(server)
        parts = [self.level.get(server, "healthy"),
                 f"{len(state.players)} players",
                 f"{state.phase or 'phase unknown'} {state.map or ''}".strip()]
        if state.rcon_latency_ms is not None:
            parts.append(f"RCON {state.rcon_latency_ms:.0f} ms")
        if state.status_age is not None:
            parts.append(f"status {state.status_age:.0f} s old")
        recent = [s for s in self.samples.get(server, []) if time.time() - s.time <= 3600]
        parts.append(f"slow minutes in the last hour: {len(recent)}")
        memory = self._memory_mb(state.pid)
        if memory:
            parts.append(f"memory {memory:.0f} MB")
        upload = self.upload.get(server)
        if upload:
            parts.append(f"upload {upload.get('rate')} {upload.get('unit')}/s")
        if self.reasons.get(server):
            parts.append("; ".join(self.reasons[server]))
        return " · ".join(parts)

    @staticmethod
    def _memory_mb(pid: int | None) -> float | None:
        if not pid:
            return None
        try:
            import psutil

            process = psutil.Process(pid)
            return sum(p.memory_info().rss for p in [process, *process.children(recursive=True)]) / 1e6
        except Exception:  # noqa: BLE001 - the process may have just gone
            return None

    @command("health", help="How this server is running", role="moderator")
    async def health(self, call: CommandCall) -> None:
        await call.reply(self.summary(call.server))
