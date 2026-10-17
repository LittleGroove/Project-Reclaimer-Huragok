"""One manager per server: a small state machine, ticked once a second by the fleet.

Nothing in here waits. A five-minute countdown, a start, a stop and a retry are
phases that are looked at on every tick, so every server is looked after at once
and the window never freezes while one of them restarts.

  phase      meaning                                          leaves for
  idle       not running                                      launch, running (adopted), external
  launch     wanted; the start is being sent                  booting, backoff
  booting    started, waiting for RCON to sign in             running, backoff, faulted
  running    up                                               countdown, stopping, backoff
  countdown  warning players before a stop or restart         stopping, running (cancelled)
  stopping   stop sent, waiting for the process to go         idle, launch
  restarting the backend's own restart command is running     booting
  backoff    died; trying again at a set time                 launch, idle
  faulted    gave up; a person starts it again                launch
  external   running, but not started from here               idle
  offline    (attach) not reachable                           external

What must survive closing the supervisor is in records/servers/<server>.json:
whether the server is wanted running, whether it's faulted, how many starts in a
row failed, the automatic restarts of the last hour, and the daily restart's date.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .config import ServerConfig, Settings
from .events import Event, EventBus
from .lifecycle import Backend, LifecycleError, LifecycleUnavailable, port_in_use
from .rcon import RconClient, RconError
from .state import ServerState

if TYPE_CHECKING:
    from .tomledit import set_server_setting  # noqa: F401

log = logging.getLogger(__name__)

PHASES = ("idle", "launch", "booting", "running", "countdown", "stopping", "restarting", "backoff", "faulted",
          "external", "offline")
BUSY = {"launch", "booting", "countdown", "stopping", "restarting"}  # mid-flight: nothing else should be started
SETTLED = {"idle", "faulted", "offline", "external"}  # where `stop everything` is done with a server
UP = {"running", "countdown", "external"}
WHEN = ("now", "after_game", "when_empty")
AFTER_GAME_LIMIT = 1800.0  # a restart "after this game" waits at most this long
DAILY_WINDOW = 1800.0  # the daily restart only fires within this long after its time
INFERRED_ALIVE_SECONDS = 30.0  # without a process to watch: status.json this fresh means up


@dataclass
class ServerRecord:
    """What the supervisor remembers about a server between runs."""

    wanted: str = "stopped"  # running | stopped
    faulted: bool = False
    fault_reason: str = ""
    fail_count: int = 0
    restart_times: list[float] = field(default_factory=list)  # automatic restarts, Unix seconds
    daily_last_fired: str = ""  # "2026-10-16"
    last_start: float | None = None

    @classmethod
    def load(cls, path: Path) -> ServerRecord:
        record = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return record
        for key, value in (data.items() if isinstance(data, dict) else []):
            if hasattr(record, key):
                setattr(record, key, value)
        record.restart_times = [float(t) for t in record.restart_times if isinstance(t, (int, float))]
        return record

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=1), encoding="utf-8")
        os.replace(tmp, path)


@dataclass
class Countdown:
    reason: str
    coming_back: bool
    manual: bool
    by: str
    pending: list[float]  # rungs still to be said, largest first
    target: float | None  # when it happens
    step: str = "ladder"  # ladder | go
    warned: bool = False
    empty_since: float | None = None

    @property
    def label(self) -> str:
        return self.reason or ("manual restart" if self.coming_back else "manual stop")


@dataclass
class PlannedRestart:
    server: str
    when: str
    reason: str
    by: str
    requested: float = field(default_factory=time.time)
    countdown: float | None = None  # seconds of notice once it's due; None: the after_game setting


@dataclass
class Result:
    ok: bool
    message: str
    text: str = ""


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds}s"


def warning_text(seconds: float, label: str, coming_back: bool) -> str:
    seconds = int(seconds)
    if seconds >= 60:
        minutes = seconds // 60
        when = f"{minutes} minute" + ("" if minutes == 1 else "s")
    else:
        when = f"{seconds} second" + ("" if seconds == 1 else "s")
    what = "Server restarting" if coming_back else "Server shutting down"
    if coming_back and label and seconds > 60:
        return f"{what} in {when}: {label}."
    return f"{what} in {when}."


def daily_slot(spec: str, now: datetime) -> datetime | None:
    """Today's slot for 'HH:MM', or None when there's no daily restart."""
    if not spec:
        return None
    hours, minutes = spec.split(":")
    return now.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)


def next_daily(spec: str, now: datetime) -> datetime | None:
    slot = daily_slot(spec, now)
    if slot is None:
        return None
    return slot + timedelta(days=1) if slot <= now else slot


class ServerManager:
    def __init__(self, fleet: Fleet, server: ServerConfig, client: RconClient, state: ServerState) -> None:
        self.fleet = fleet
        self.server = server
        self.client = client
        self.state = state
        self.backend = fleet.backend
        self.settings = fleet.settings
        self.record_path = self.settings.records / "servers" / f"{server.slug}.json"
        self.record = ServerRecord.load(self.record_path)
        self.phase = "idle"
        self.phase_since = time.time()
        self.countdown: Countdown | None = None
        self.planned: PlannedRestart | None = None
        self.playing: tuple[Any, Any] | None = None  # (map, mode) of the game being played, for after_game
        self.coming_back = False  # a stop in progress leads to a start
        self.stop_label = ""
        self.stop_by = ""
        self.failing = False  # the stop in progress is a failed start being cleaned up
        self.task: asyncio.Task[None] | None = None  # the backend operation in flight
        self.task_error: str | None = None
        self.task_kind = ""
        self.backoff_until = 0.0
        self.started: float | None = None  # when this run began (Unix seconds)
        self.health = "healthy"  # the watchdog's word, for the dashboard
        self.notes: dict[str, float] = {}
        self.booted = False

    # What we know -------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.server.name

    def setting(self, key: str) -> Any:
        return self.settings.restart_setting(self.server, key)

    @property
    def alive(self) -> bool:
        known = self.backend.alive(self.server)
        if known is not None:
            return known
        age = self.state.status_age
        return self.state.connected or (age is not None and age < INFERRED_ALIVE_SECONDS)

    @property
    def uptime(self) -> float | None:
        return None if self.started is None else max(0.0, time.time() - self.started)

    @property
    def players(self) -> int:
        return len(self.state.players)

    def note(self, message: str, every: float = 3600, level: int = logging.WARNING) -> None:
        """A warning that won't repeat itself into uselessness."""
        now = time.monotonic()
        if now - self.notes.get(message, -every) < every:
            return
        if len(self.notes) > 200:
            self.notes.clear()
        self.notes[message] = now
        log.log(level, "[%s] %s", self.name, message)

    def _set_phase(self, phase: str) -> None:
        if phase != self.phase:
            log.debug("[%s] %s -> %s", self.name, self.phase, phase)
        self.phase = phase
        self.phase_since = time.time()

    def _save(self) -> None:
        self.record.save(self.record_path)

    # Booting the manager ----------------------------------------------------------------

    def boot(self) -> None:
        """Once, when the supervisor starts: this is what makes a reboot come back by itself."""
        if self.server.autostart:
            self.record.wanted, self.record.faulted, self.record.fault_reason, self.record.fail_count = \
                "running", False, "", 0
        elif not self.alive:
            self.record.wanted = "stopped"
        else:
            self.record.wanted = "running"
        self._save()
        self.booted = True

    # The tick -----------------------------------------------------------------------------

    def tick(self, now: float) -> None:
        if self.task is not None and self.task.done():
            task, self.task = self.task, None
            self.task_error = None
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    self.task_error = str(error) or type(error).__name__
        self.backend.drain(self.server)
        try:
            self._step(now)
        except Exception as error:  # noqa: BLE001 - one bad step must not stop the fleet
            self.note(f"manager error (server untouched): {type(error).__name__}: {error}", 60, logging.ERROR)
            log.debug("[%s] step failed", self.name, exc_info=True)

    def _step(self, now: float) -> None:
        alive = self.alive
        phase = self.phase
        if phase == "idle":
            self._step_idle(alive)
        elif phase == "launch":
            self._step_launch()
        elif phase == "booting":
            self._step_booting(now, alive)
        elif phase == "running":
            self._step_running(now, alive)
        elif phase == "countdown":
            self._step_countdown(now, alive)
        elif phase == "stopping":
            self._step_stopping(now, alive)
        elif phase == "restarting":
            self._step_restarting(now)
        elif phase == "backoff":
            self._step_backoff(now, alive)
        elif phase == "faulted":
            if alive:
                self._adopt()
        elif phase == "external":
            self._step_external(alive)
        elif phase == "offline":
            if alive:
                log.info("[%s] reachable", self.name)
                self._seen_up()

    def _seen_up(self) -> None:
        """The server is up: ours to look after when the backend can control it, otherwise just watched."""
        if self.backend.can_restart:
            self._adopt()
        else:
            self._set_phase("external")
            self.started = self.backend.started_at(self.server) or time.time()

    def _step_idle(self, alive: bool) -> None:
        if alive:
            self._seen_up()
            return
        if not self.backend.can_start:
            self._set_phase("offline")
            return
        if self.backend.sees_processes and port_in_use(self.server):
            self._set_phase("external")
            self.started = time.time()
            log.warning("[%s] already running, started some other way (something answers on RCON port %s). "
                        "Watching and moderating it; stop it and start it from here to let the supervisor "
                        "restart it", self.name, self.server.rcon_port)
            return
        if self.record.wanted == "running" and not self.record.faulted and not self.server.errors:
            self._set_phase("launch")

    def _step_launch(self) -> None:
        if self.record.wanted != "running":
            self._set_phase("idle")
            return
        if self.server.errors:
            self.note("not starting: " + "; ".join(self.server.errors))
            self._set_phase("idle")
            return
        self.started = time.time()
        self.record.last_start = self.started
        self._save()
        self.state.players.clear()
        self._run_task("start", self.backend.start(self.server))
        self._set_phase("booting")
        log.info("[%s] starting; allowing %s for it to answer", self.name, format_duration(self.setting("ready_seconds")))

    def _step_booting(self, now: float, alive: bool) -> None:
        if self.task is not None:
            return  # the start command is still running
        if self.task_error is not None:
            error, self.task_error = self.task_error, None
            self._fail(f"could not start: {error}")
            return
        if self.task_kind == "start":
            self.task_kind = ""
            self.client.wake()
        if self.backend.sees_processes and not alive:
            self._fail("the server exited while it was starting", show_output=True)
            return
        if self.state.connected or self.client.stopped_reason:
            self._ready(now)
            return
        if now - self.phase_since > float(self.setting("ready_seconds")):
            self._fail(f"the server never answered on RCON within {format_duration(self.setting('ready_seconds'))}",
                       kill=True, show_output=True)

    def _ready(self, now: float) -> None:
        self._set_phase("running")
        if self.started is None:
            self.started = self.backend.started_at(self.server) or now
        took = format_duration(now - (self.record.last_start or now))
        if self.client.stopped_reason:
            log.error("[%s] up, but its console can't be used: %s", self.name, self.client.stopped_reason)
        else:
            log.info("[%s] up and answering RCON after %s", self.name, took)
        if self.coming_back:
            self.coming_back = False
            self.fleet.bus.emit("server.restarted", self.name, reason=self.stop_label, by=self.stop_by)
        else:
            self.fleet.bus.emit("server.ready", self.name, pid=self.pid)

    def _step_running(self, now: float, alive: bool) -> None:
        if not alive:
            self._crashed()
            return
        uptime = self.uptime or 0.0
        if self.record.fail_count and uptime >= float(self.setting("good_run_seconds")):
            self.record.fail_count = 0
            self._save()
        if self.planned is not None and now - self.planned.requested > AFTER_GAME_LIMIT:
            log.info("[%s] the game didn't end within %s; restarting with the usual warnings",
                     self.name, format_duration(AFTER_GAME_LIMIT))
            self._trigger_plan(use_ladder=True)
            return
        if self.planned is not None and self.planned.when == "when_empty" and not self.players and self.state.connected:
            self._trigger_plan()
            return
        if uptime >= float(self.setting("min_uptime_seconds")) and self._daily_due(now):
            self._begin_countdown("daily restart", coming_back=True, manual=False, by=self.settings.name)

    def _daily_due(self, now: float) -> bool:
        spec = self.server.daily_restart if self.server.daily_restart is not None else self.setting("daily_restart")
        local = datetime.fromtimestamp(now)
        slot = daily_slot(spec, local)
        if slot is None:
            return False
        today = local.strftime("%Y-%m-%d")
        if self.record.daily_last_fired == today:
            return False
        # Only inside a short window after the slot, so a supervisor opened at noon
        # doesn't decide it missed 04:00 and restart everything.
        if slot <= local < slot + timedelta(seconds=DAILY_WINDOW):
            self.record.daily_last_fired = today
            self._save()
            return True
        return False

    def next_daily_text(self) -> str:
        spec = self.server.daily_restart if self.server.daily_restart is not None else self.setting("daily_restart")
        slot = next_daily(spec, datetime.now())
        if slot is None:
            return ""
        return f"daily restart {spec} (in {format_duration((slot - datetime.now()).total_seconds())})"

    def _step_countdown(self, now: float, alive: bool) -> None:
        countdown = self.countdown
        if countdown is None:
            self._set_phase("running")
            return
        if not alive:
            log.warning("[%s] the server exited on its own during the countdown", self.name)
            self.countdown = None
            self.coming_back = countdown.coming_back
            self._complete_stop(died=True)
            return
        if countdown.step == "ladder" and countdown.target is not None:
            remaining = countdown.target - now
            due = [r for r in countdown.pending if remaining <= r]
            if due:
                fire = min(due)
                countdown.pending = [r for r in countdown.pending if r < fire]
                self._say(warning_text(fire, countdown.label, countdown.coming_back))
                countdown.warned = True
            if remaining <= 0:
                countdown.step = "go"
            elif self.state.connected and not self.players:
                countdown.empty_since = countdown.empty_since or now
                if now - countdown.empty_since >= float(self.setting("empty_seconds")):
                    log.info("[%s] everyone left during the countdown; %s now", self.name,
                             "restarting" if countdown.coming_back else "stopping")
                    countdown.step = "go"
            else:
                countdown.empty_since = None
        if countdown.step == "go":
            self._begin_stop()

    def _step_stopping(self, now: float, alive: bool) -> None:
        if self.task is not None:
            return
        if self.task_error is not None:
            log.error("[%s] stopping failed: %s", self.name, self.task_error)
            self.task_error = None
        if alive and now - self.phase_since < float(getattr(self.backend, "stop_timeout", 30.0)) + 10:
            return  # the process (or, inferred, its RCON) hasn't gone yet
        if alive:
            self.note("still reachable after the stop; carrying on anyway", 60)
        self._complete_stop()

    def _step_restarting(self, now: float) -> None:
        if self.task is not None:
            return
        if self.task_error is not None:
            error, self.task_error = self.task_error, None
            self._fail(f"the restart command failed: {error}")
            return
        self.started = time.time()
        self.record.last_start = self.started
        self._save()
        self.client.wake()
        self._set_phase("booting")

    def _step_backoff(self, now: float, alive: bool) -> None:
        if alive:
            self._adopt()
            return
        if self.record.wanted != "running":
            self._set_phase("idle")
            return
        if now >= self.backoff_until:
            self._set_phase("launch")

    def _step_external(self, alive: bool) -> None:
        if alive:
            return
        if self.backend.sees_processes and port_in_use(self.server):
            return
        log.info("[%s] no longer running", self.name)
        self.started = None
        self._set_phase("idle" if self.backend.can_start else "offline")

    # Helpers for the steps -------------------------------------------------------------------

    def _run_task(self, kind: str, coroutine: Awaitable[None]) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task_kind = kind
        self.task_error = None
        self.task = asyncio.ensure_future(coroutine)

    def _say(self, text: str) -> None:
        """A chat line whose answer only matters if it went wrong."""
        if not self.client.connected:
            self.note(f"could not announce (RCON not connected): {text}", 60)
            return

        async def send() -> None:
            try:
                await self.client.command("say", text)
            except RconError as error:
                log.warning("[%s] could not announce (%s): %s", self.name, error, text)

        asyncio.ensure_future(send())

    def _adopt(self) -> None:
        """Someone else started it, or a closed supervisor did: take it from here. Nothing about it changes."""
        self.started = self.backend.started_at(self.server) or time.time()
        self.record.wanted, self.record.faulted, self.record.fault_reason = "running", False, ""
        self._save()
        self._set_phase("running")
        pid = self.pid
        log.info("[%s] already running%s (up %s); looking after it now", self.name,
                 f" as process {pid}" if pid else "", format_duration(self.uptime or 0))
        self.fleet.bus.emit("server.adopted", self.name, pid=pid)

    @property
    def pid(self) -> int | None:
        getter = getattr(self.backend, "pid", None)
        return getter(self.server) if getter else self.state.pid

    def _crashed(self) -> None:
        uptime = self.uptime or 0.0
        code = self.backend.exit_code(self.server)
        log.error("[%s] the server exited on its own after %s%s", self.name, format_duration(uptime),
                  f" (exit code {code})" if code is not None else "")
        for line in self.backend.last_console_lines(self.server, 4):
            log.error("[%s]   | %s", self.name, line)
        self.backend.forget(self.server)
        self.started = None
        self.state.players.clear()
        if self.record.wanted != "running":
            self._set_phase("idle")
            return
        if uptime >= float(self.setting("good_run_seconds")):
            self.record.fail_count = 0
        self._register_failure()

    def _fail(self, why: str, *, kill: bool = False, show_output: bool = False) -> None:
        log.error("[%s] %s", self.name, why)
        if show_output:
            for line in self.backend.last_console_lines(self.server, 6):
                log.error("[%s]   | %s", self.name, line)
        if kill and self.backend.sees_processes and self.alive:
            log.warning("[%s] ending the process: it never finished starting", self.name)
            self.failing = True
            self.coming_back = False
            self._run_task("stop", self.backend.stop(self.server, timeout=5))
            self._set_phase("stopping")
            return
        self.backend.forget(self.server)
        self.started = None
        self._register_failure()

    def _register_failure(self) -> None:
        """Crashes back off, and several in a row mean stop trying rather than thrash all night."""
        self.record.fail_count += 1
        self._save()
        limit = int(self.setting("max_failures"))
        fails = self.record.fail_count
        if fails >= limit:
            self._enter_faulted(f"{fails} starts in a row failed or died soon after starting.")
            return
        ladder = [float(d) for d in self.setting("backoff_seconds")] or [5.0]
        delay = ladder[min(fails - 1, len(ladder) - 1)]
        if delay > 0:
            log.warning("[%s] trying again in %s (failure %s of %s)", self.name, format_duration(delay), fails, limit)
            self.backoff_until = time.time() + delay
            self._set_phase("backoff")
        else:
            log.warning("[%s] trying again now (failure %s of %s)", self.name, fails, limit)
            self._set_phase("launch")

    def _enter_faulted(self, why: str) -> None:
        self.record.faulted, self.record.fault_reason = True, why
        self._save()
        path = self.settings.logs / f"faulted-{self.server.slug}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            launcher = os.environ.get("RECLAIMER_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")
            title = f"{self.name} · stopped trying at {time.strftime('%Y-%m-%d %H:%M:%S')}"
            console = self.settings.logs / f"console-{self.server.slug}.log"
            lines = [title, "─" * max(60, len(title)), "",
                     f"Why         {why}",
                     f"What to do  fix the cause, then start it again: S in the window, or {launcher} restart "
                     f"\"{self.name}\"",
                     f"Its folder  {self.server.folder}",
                     f"Its console {console}", "",
                     "──── The last lines of its console ────", "",
                     *self.backend.last_console_lines(self.server, 200)]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        log.error("[%s] STOPPED - THE SERVER WILL NOT COME BACK ON ITS OWN", self.name)
        log.error("[%s] %s", self.name, why)
        log.error("[%s] Details and the last lines of its output: %s. Fix the cause, then start it again (S).",
                  self.name, path)
        self.fleet.bus.emit("server.faulted", self.name, reason=why)
        self._set_phase("faulted")

    def _begin_countdown(self, reason: str, *, coming_back: bool, manual: bool, by: str, immediate: bool = False,
                         seconds: float | None = None) -> None:
        if seconds is None:
            rungs = sorted({float(s) for s in self.setting("warn_seconds") if float(s) > 0}, reverse=True)
        else:
            rungs = sorted({s for s in (float(seconds), 10.0) if 0 < s <= float(seconds)}, reverse=True)
        countdown = Countdown(reason, coming_back, manual, by, rungs, None)
        label = countdown.label
        verb = "restart" if coming_back else "stop"
        log.info("[%s] %s requested: %s", self.name, verb, label)
        if coming_back and not manual:
            cutoff = time.time() - 3600
            self.record.restart_times = [t for t in self.record.restart_times if t > cutoff] + [time.time()]
            self._save()
        if immediate:
            countdown.step = "go"
        elif self.state.connected and not self.players:
            log.info("[%s] skipping the countdown: nobody is connected", self.name)
            countdown.step = "go"
        elif not rungs:
            countdown.step = "go"
        else:
            if not self.state.connected:
                log.warning("[%s] the player count is unknown (RCON not connected), so running the full countdown "
                            "to be safe", self.name)
            countdown.target = time.time() + rungs[0]
        self.countdown = countdown
        self._set_phase("countdown")
        self.fleet.bus.emit("server.countdown", self.name, reason=label, coming_back=coming_back,
                            seconds=rungs[0] if countdown.step == "ladder" and rungs else 0, by=by)
        if countdown.step == "go":
            self._begin_stop()  # right away, not at the next tick

    def _begin_stop(self) -> None:
        countdown = self.countdown
        self.countdown = None
        coming_back = countdown.coming_back if countdown else self.coming_back
        label = countdown.label if countdown else self.stop_label
        self.coming_back = coming_back
        self.stop_label = label
        self.stop_by = countdown.by if countdown else self.settings.name
        if self.state.connected and self.players:
            self._say("Server is restarting now." if coming_back else "Server is shutting down now.")
        if coming_back:
            self.fleet.bus.emit("server.restarting", self.name, reason=label, by=self.stop_by)
        if coming_back and self.backend.restart_only:
            self._run_task("restart", self.backend.restart(self.server))
            self._set_phase("restarting")
            return
        log.info("[%s] %s: stopping the server", self.name, "restart" if coming_back else "stop")
        self._run_task("stop", self.backend.stop(self.server))
        self._set_phase("stopping")

    def _complete_stop(self, *, died: bool = False) -> None:
        self.started = None
        self.state.players.clear()
        if died:
            self.backend.forget(self.server)
        if self.failing:
            self.failing = False
            self._register_failure()
            return
        if self.coming_back and self.record.wanted == "running":
            cutoff = time.time() - 3600
            recent = [t for t in self.record.restart_times if t > cutoff]
            budget = int(self.setting("restarts_per_hour"))
            if len(recent) > budget:
                self._enter_faulted(f"{len(recent)} automatic restarts in one hour is over the budget of {budget}. "
                                    "Something is triggering in a loop; stopping here rather than thrashing.")
                return
            self._set_phase("launch")
            return
        self.coming_back = False
        if self.record.wanted != "stopped":
            self.record.wanted = "stopped"
            self._save()
        log.info("[%s] stopped%s", self.name, " (it exited on its own before it could be stopped cleanly)" if died else "")
        self._set_phase("idle")

    # Planned restarts (after this game, when empty) ----------------------------------------------

    def plan(self, when: str, reason: str, by: str, countdown: float | None) -> PlannedRestart:
        plan = PlannedRestart(self.name, when, reason, by, countdown=countdown)
        self.planned = plan
        self.record.wanted = "running"
        self._save()
        if self.state.phase == "playing" and self.playing is None:
            self.playing = (self.state.map, self.state.mode)
        log.info("[%s] restart planned (%s): %s", self.name, when.replace("_", " "), reason or "no reason given")
        self.fleet.bus.emit("server.restart_planned", self.name, when=when, reason=reason, by=by)
        if not self.players and self.state.connected:
            self._trigger_plan()
        elif when == "after_game" and self.state.phase == "finished":
            self._trigger_plan()
        elif when == "after_game":
            self._say(f"This server restarts after this game{': ' + reason if reason else '.'}")
        return plan

    def on_phase(self, event: Event) -> None:
        phase, game = event.get("phase"), (event.get("map"), event.get("mode"))
        if phase == "playing":
            self.playing = game
            return
        # The server can send `finished` for the next game before it starts (seen on
        # 0.9.18), which must not count: only the game that was being played ends it.
        if phase != "finished" or self.playing != game:
            return
        self.playing = None
        if self.planned is not None and self.planned.when == "after_game" and self.phase == "running":
            self._trigger_plan()

    def on_leave(self, event: Event) -> None:
        if self.planned is None or self.phase != "running":
            return
        others = [p for p in self.state.players.values() if p.number != event.get("number")]
        if not others:
            self._trigger_plan()

    def _trigger_plan(self, *, use_ladder: bool = False) -> None:
        plan, self.planned = self.planned, None
        if plan is None or self.phase != "running":
            return
        seconds = None if use_ladder else (plan.countdown if plan.countdown is not None
                                           else float(self.setting("after_game_seconds")))
        self._begin_countdown(plan.reason, coming_back=True, manual=False, by=plan.by,
                              immediate=seconds is not None and seconds <= 0, seconds=seconds)

    # What a person asks for ----------------------------------------------------------------------

    def _notice(self) -> None:
        """A request can't wait for the next tick: if the server is up and we haven't seen it yet, see it now."""
        if self.phase in ("idle", "backoff", "faulted", "offline") and self.alive:
            self._seen_up()

    def start(self) -> Result:
        self._notice()
        if not self.backend.can_start:
            return Result(False, f"backend = {self.backend.name} can't start servers")
        if self.server.errors:
            return Result(False, "not starting: " + "; ".join(self.server.errors))
        if self.phase == "external":
            return Result(False, "it's already running, started some other way")
        self.record.wanted, self.record.faulted, self.record.fault_reason, self.record.fail_count = \
            "running", False, "", 0
        self._save()
        if self.phase in ("idle", "faulted", "backoff", "offline"):
            log.info("[%s] start requested", self.name)
            self._set_phase("launch")
            return Result(True, "starting")
        if self.phase == "countdown" and self.countdown and not self.countdown.coming_back:
            return self.cancel()
        if self.phase == "stopping":
            self.coming_back = True
            return Result(True, "it will start again once it has stopped")
        return Result(True, "already running" if self.phase in UP else "already starting")

    def stop(self, *, immediate: bool = False, reason: str = "", manual: bool = True,
             by: str = "") -> Result:
        self._notice()
        if self.phase == "external" and self.backend.sees_processes:
            return Result(False, "it wasn't started from here, so the supervisor can't stop it; stop it the way it "
                                 "was started")
        if not self.backend.can_stop:
            return Result(False, f"backend = {self.backend.name} can't stop servers")
        self.planned = None
        self.record.wanted = "stopped"
        self._save()
        phase = self.phase
        if phase in ("running", "external"):
            self._begin_countdown(reason or "manual stop", coming_back=False, manual=manual, by=by or self.settings.name,
                                  immediate=immediate)
            return Result(True, "stopping now" if immediate or self.countdown is None or self.countdown.step == "go"
                          else "stopping (warning players first)")
        if phase == "countdown" and self.countdown:
            self.countdown.coming_back = False
            if immediate:
                self.countdown.step = "go"
            log.info("[%s] the countdown now ends in a stop", self.name)
            return Result(True, "the countdown now ends in a stop")
        if phase in ("booting", "launch"):
            self.coming_back = False
            self.stop_label = reason or "manual stop"
            log.info("[%s] stop requested while starting", self.name)
            self._run_task("stop", self.backend.stop(self.server))
            self._set_phase("stopping")
            return Result(True, "it was still starting; stopping it")
        if phase in ("stopping", "restarting"):
            self.coming_back = False
            return Result(True, "already stopping; it will stay stopped")
        if phase in ("backoff", "faulted"):
            self._set_phase("idle")
            return Result(True, "it will stay stopped")
        return Result(True, "it is not running")

    def restart(self, *, immediate: bool = False, reason: str = "", manual: bool = True, by: str = "",
                when: str = "now", countdown: float | None = None) -> Result:
        self._notice()
        if self.phase == "external" and self.backend.sees_processes:
            return Result(False, "it wasn't started from here, so the supervisor can't restart it; stop it the way "
                                 "it was started, then start it from here")
        if not self.backend.can_restart:
            return Result(False, f"backend = {self.backend.name} can't restart servers")
        if when not in WHEN:
            return Result(False, f"when must be one of {', '.join(WHEN)}")
        by = by or self.settings.name
        if when != "now":
            if self.phase not in ("running", "external"):
                return Result(False, f"it isn't running ({self.phase})")
            self.plan(when, reason, by, countdown)
            return Result(True, f"restart planned {when.replace('_', ' ')}")
        self.record.wanted = "running"
        self._save()
        phase = self.phase
        if phase in ("running", "external"):
            self._begin_countdown(reason or ("manual restart" if manual else ""), coming_back=True, manual=manual,
                                  by=by, immediate=immediate or (countdown is not None and countdown <= 0),
                                  seconds=countdown if countdown else None)
            return Result(True, "restarting now" if self.countdown is None or self.countdown.step == "go"
                          else "restarting (warning players first)")
        if phase == "countdown" and self.countdown:
            self.countdown.coming_back = True
            if immediate:
                self.countdown.step = "go"
            return Result(True, "the countdown now ends in a restart")
        if phase in ("stopping", "restarting"):
            self.coming_back = True
            return Result(True, "it will start again once it has stopped")
        if phase in ("booting", "launch"):
            return Result(False, "it is still starting")
        return self.start()

    def cancel(self) -> Result:
        if self.phase == "countdown" and self.countdown and self.countdown.step == "ladder":
            countdown, self.countdown = self.countdown, None
            if countdown.warned:
                self._say("The server restart has been cancelled." if countdown.coming_back
                          else "The server shutdown has been cancelled.")
            self.record.wanted = "running"
            self._save()
            log.info("[%s] countdown cancelled (%s)", self.name, countdown.label)
            self.fleet.bus.emit("server.countdown_cancelled", self.name, reason=countdown.label)
            self._set_phase("running")
            return Result(True, "cancelled")
        if self.planned is not None:
            plan, self.planned = self.planned, None
            log.info("[%s] planned restart cancelled (%s)", self.name, plan.reason)
            return Result(True, "the planned restart is cancelled")
        return Result(False, "there is no countdown to cancel")

    async def say(self, text: str) -> Result:
        text = text.strip()
        if not text:
            return Result(False, "nothing to say")
        if not self.client.connected:
            return Result(False, "its RCON isn't connected")
        try:
            reply = await self.client.command("say", text)
        except RconError as error:
            return Result(False, str(error))
        log.info("[%s] message to players: %s", self.name, text)
        return Result(reply.ok, "sent" if reply.ok else reply.text, reply.text)

    async def rcon(self, line: str, by: str | None = None) -> Result:
        line = line.strip()
        if not line:
            return Result(False, "nothing to send")
        if not self.client.connected:
            return Result(False, "its RCON isn't connected")
        try:
            reply = await self.client.command(line, by=by, timeout=15)
        except RconError as error:
            return Result(False, str(error))
        log.info("[%s] console command: %s", self.name, line)
        return Result(reply.ok, "ok" if reply.ok else "refused", reply.text)

    def set_autostart(self, value: bool) -> Result:
        from .tomledit import set_server_setting

        try:
            set_server_setting(self.settings.path, self.name, "autostart", value)
        except OSError as error:
            return Result(False, f"couldn't edit {self.settings.path.name}: {error}")
        self.server.autostart = value
        self.settings.server_overrides.setdefault(self.name.lower(), {"name": self.name})["autostart"] = value
        log.info("[%s] autostart turned %s", self.name, "ON" if value else "OFF")
        return Result(True, f"autostart {'ON' if value else 'off'}")

    # For the dashboard --------------------------------------------------------------------------

    def row(self, index: int, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        state = self.state
        phase = self.phase
        status, color, detail = "", "gray", ""
        up = format_duration(self.uptime) if self.uptime is not None else ""
        if phase == "idle":
            if self.server.errors:
                status, color = "CONFIG ERROR", "red"
            elif not self.backend.can_start:
                status, color = "not running", "gray"
            else:
                status, color = "stopped", "gray"
        elif phase == "launch":
            status, color = "STARTING", "cyan"
        elif phase == "booting":
            status, color = f"STARTING  {format_duration(now - self.phase_since)}", "cyan"
            if self.task is not None and not self.backend.sees_processes:
                detail = "running the start command"
        elif phase == "running":
            status, color = f"RUNNING  up {up}", "green"
            if not state.connected:
                age = now - self.phase_since
                if age > 30:
                    status, color = f"RUNNING  up {up} (RCON not connected)", "yellow"
            if self.health not in ("healthy", ""):
                color = "yellow"
        elif phase == "countdown" and self.countdown:
            verb = "RESTART" if self.countdown.coming_back else "STOP"
            if self.countdown.step == "go" or self.countdown.target is None:
                status = f"{verb} NOW"
            else:
                status = f"{verb} IN {format_duration(self.countdown.target - now)}"
            color = "yellow"
            detail = f"{verb.lower()} reason: {self.countdown.label}   [K] cancels it"
        elif phase == "stopping":
            status, color = ("RESTARTING - stopping" if self.coming_back else "STOPPING"), "yellow"
        elif phase == "restarting":
            status, color = "RESTARTING", "yellow"
        elif phase == "backoff":
            status, color = f"CRASHED - retry in {format_duration(self.backoff_until - now)}", "red"
        elif phase == "faulted":
            status, color, detail = "FAULTED - [S] to retry", "red", self.record.fault_reason
        elif phase == "external":
            if self.backend.can_start and self.backend.sees_processes:
                status, color = f"RUNNING  up {up} - not started from here", "yellow"
                detail = "stop it the way it was started, then start it here, to let the supervisor restart it"
            elif self.backend.can_start:
                status, color = f"RUNNING  up {up} - started elsewhere", "yellow"
            else:
                status, color = f"RUNNING  up {up} (watching)", "green"
        elif phase == "offline":
            status, color = "not reachable", "gray"
        for error in self.server.errors:
            detail = f"! {error}"
        if phase == "running" and not detail:
            bits = []
            if self.planned is not None:
                bits.append(f"restart planned {self.planned.when.replace('_', ' ')}: {self.planned.reason or 'no reason'}")
            daily = self.next_daily_text()
            if daily:
                bits.append(daily)
            if self.health not in ("healthy", ""):
                bits.append(f"watchdog: {self.health}")
            if state.rcon_latency_ms is not None and state.connected:
                bits.append(f"RCON {state.rcon_latency_ms:.0f} ms")
            if state.status_age is not None:
                bits.append(f"status {state.status_age:.0f} s old")
            detail = "  |  ".join(bits)
        players = ""
        names = ""
        if phase in UP or phase == "countdown":
            players = str(len(state.players)) + (f"/{state.max_players}" if state.max_players else "")
            names = ", ".join(p.name for p in sorted(state.players.values(), key=lambda p: p.number))
        game = ""
        if phase in UP or phase == "countdown":
            phase_word = {"no_game": "lobby:", "playing": "playing", "round_over": "round over:",
                          "between_rounds": "between rounds:", "finished": "finished:"}.get(state.phase or "",
                                                                                          (state.phase or ""))
            words = [phase_word, state.map or "", state.mode or ""]
            game = " ".join(w for w in words if w)
            if state.next_game:
                game = f"{game} → next {state.next_game}" if game else f"next {state.next_game}"
            elif state.playlist_vote:
                game = f"{game} (voting)" if game else "voting"
        return {
            "index": index, "name": self.name, "slug": self.server.slug, "port": self.server.game_port,
            "autostart": self.server.autostart, "phase": phase, "status": status, "color": color,
            "players": players, "names": names, "game": game, "detail": detail, "plays": self.server.plays,
            "next": state.next_game or "",
            "version": state.version or "", "uptime": up, "health": self.health,
            "connected": state.connected, "errors": list(self.server.errors),
            "can": {
                "start": self.backend.can_start and phase in ("idle", "faulted", "backoff", "offline"),
                "stop": self.backend.can_stop and (phase in ("running", "countdown", "booting", "launch", "backoff",
                                                             "faulted", "stopping", "restarting")
                                                   or (phase == "external" and not self.backend.sees_processes)),
                "restart": self.backend.can_restart and (phase in ("running", "countdown", "idle", "faulted",
                                                                   "backoff", "stopping")
                                                         or (phase == "external" and not self.backend.sees_processes)),
                "cancel": phase == "countdown" or self.planned is not None,
                "talk": phase in UP or phase == "countdown",
            },
        }


class Fleet:
    """Every server's manager, ticked together once a second; what a person (or a request file) can ask for."""

    def __init__(self, settings: Settings, backend: Backend, bus: EventBus, *,
                 on_all_stopped: Callable[[], None] | None = None,
                 on_request: Callable[[str, dict[str, Any]], Awaitable[Result]] | None = None) -> None:
        self.settings = settings
        self.backend = backend
        self.bus = bus
        self.managers: dict[str, ServerManager] = {}
        self.on_all_stopped = on_all_stopped
        self.on_request = on_request  # verbs the supervisor itself answers: stop_all, update...
        self.stopping_all = False
        self._awaiting: set[str] = set()  # servers a `stop everything` is waiting on
        self.live_path = settings.records / "live.json"
        self._live_written = 0.0
        self._requests_tasks: set[asyncio.Task[None]] = set()
        bus.subscribe("rcon.phase", self._on_phase)
        bus.subscribe("rcon.leave", self._on_leave)
        bus.subscribe("watchdog.health", self._on_health)

    # Servers -------------------------------------------------------------------------------

    def add(self, server: ServerConfig, client: RconClient, state: ServerState) -> ServerManager:
        manager = ServerManager(self, server, client, state)
        self.managers[server.name] = manager
        if any(m.booted for m in self.managers.values() if m is not manager):
            manager.boot()  # added while running: treat like the start
        return manager

    def remove(self, name: str) -> None:
        self.managers.pop(name, None)

    def find(self, ref: str) -> ServerManager | None:
        """By name (any case), #number or number, or data-folder name."""
        ref = ref.strip().lstrip("#")
        if ref.isdigit():
            index = int(ref)
            names = list(self.managers)
            return self.managers[names[index - 1]] if 0 < index <= len(names) else None
        for name, manager in self.managers.items():
            if name.lower() == ref.lower() or manager.server.slug == ref.lower():
                return manager
        return None

    def names(self) -> list[str]:
        return list(self.managers)

    @property
    def can_control(self) -> bool:
        return self.backend.can_restart

    # Running ---------------------------------------------------------------------------------

    def boot(self) -> None:
        self.backend.adopt()
        for manager in self.managers.values():
            manager.boot()

    async def run(self) -> None:
        self.boot()
        self.tick()
        while True:
            await asyncio.sleep(1)
            self.tick()

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for manager in list(self.managers.values()):
            manager.tick(now)
        if now - self._live_written >= 2:
            self._live_written = now
            self.write_live(now)
        self._read_requests()
        if self.stopping_all and all(self.managers[n].phase in SETTLED for n in self._awaiting if n in self.managers):
            self.stopping_all = False
            self._awaiting.clear()
            if self.on_all_stopped:
                self.on_all_stopped()

    def _on_phase(self, event: Event) -> None:
        manager = self.managers.get(event.server or "")
        if manager:
            manager.on_phase(event)

    def _on_leave(self, event: Event) -> None:
        manager = self.managers.get(event.server or "")
        if manager:
            manager.on_leave(event)

    def _on_health(self, event: Event) -> None:
        manager = self.managers.get(event.server or "")
        if manager:
            manager.health = str(event.get("level", "healthy"))

    # What's shown -----------------------------------------------------------------------------

    def rows(self, now: float | None = None) -> list[dict[str, Any]]:
        now = now or time.time()
        return [m.row(i, now) for i, m in enumerate(self.managers.values(), 1)]

    def write_live(self, now: float | None = None) -> None:
        from . import __version__

        now = now or time.time()
        data = {"updated": now, "pid": os.getpid(), "version": __version__, "backend": self.backend.name,
                "stopping_all": self.stopping_all, "servers": self.rows(now)}
        try:
            self.live_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.live_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.live_path)
        except OSError as error:
            log.debug("couldn't write %s: %s", self.live_path, error)

    # Requests -----------------------------------------------------------------------------------

    def stop_all(self, *, immediate: bool = False, reason: str = "the supervisor is stopping") -> list[tuple[str, Result]]:
        self.stopping_all = True
        results = []
        for manager in self.managers.values():
            if manager.phase in SETTLED:
                continue
            result = manager.stop(immediate=immediate, reason=reason)
            if result.ok:
                self._awaiting.add(manager.name)
            results.append((manager.name, result))
        return results

    def targets(self, ref: str) -> list[ServerManager]:
        if ref.strip().lower() in ("", "all", "*"):
            return list(self.managers.values())
        manager = self.find(ref)
        return [manager] if manager else []

    async def verb(self, verb: str, server: str, **options: Any) -> list[tuple[str, Result]]:
        """One request, for one server or all; the answers by server name."""
        if verb == "stop_all":
            return self.stop_all(immediate=bool(options.get("immediate")))
        if verb in ("update", "check_update", "close", "check_program_update", "update_program"):
            if self.on_request is None:
                return [("supervisor", Result(False, "not available"))]
            return [("supervisor", await self.on_request(verb, options))]
        targets = self.targets(server)
        if not targets:
            return [(server, Result(False, f"no server called '{server}'"))]
        results = []
        for manager in targets:
            results.append((manager.name, await self._apply(manager, verb, options)))
        return results

    async def _apply(self, manager: ServerManager, verb: str, options: dict[str, Any]) -> Result:
        immediate = bool(options.get("immediate"))
        reason = str(options.get("reason") or "")
        by = str(options.get("by") or "")
        if verb == "start":
            return manager.start()
        if verb == "stop":
            return manager.stop(immediate=immediate, reason=reason, by=by)
        if verb == "restart":
            when = str(options.get("when") or "now")
            return manager.restart(immediate=immediate, reason=reason, by=by, when=when)
        if verb == "cancel":
            return manager.cancel()
        if verb == "say":
            return await manager.say(str(options.get("text") or ""))
        if verb == "rcon":
            return await manager.rcon(str(options.get("text") or ""), by=by or None)
        if verb == "autostart":
            value = options.get("value")
            return manager.set_autostart(not manager.server.autostart if value is None else bool(value))
        return Result(False, f"unknown request '{verb}'")

    def _read_requests(self) -> None:
        from . import mailbox

        try:
            requests = mailbox.read(self.settings.records)
        except OSError:
            return
        for request in requests:
            task = asyncio.ensure_future(self._answer(request))
            self._requests_tasks.add(task)
            task.add_done_callback(self._requests_tasks.discard)

    async def _answer(self, request: Any) -> None:
        from . import mailbox

        try:
            results = await self.verb(request.verb, request.server, **request.options)
        except Exception as error:  # noqa: BLE001
            log.exception("request %s failed", request.verb)
            results = [(request.server, Result(False, f"{type(error).__name__}: {error}"))]
        ok = all(r.ok for _, r in results)
        message = "; ".join(f"{name}: {r.message}" for name, r in results)
        text = "\n".join(r.text for _, r in results if r.text)
        log.info("request from outside (%s %s): %s", request.verb, request.server, message)
        try:
            mailbox.reply(self.settings.records, request.id, ok, message, text)
        except OSError:
            pass


class Lifecycle:
    """What parts use to restart servers: now (with a countdown), after this game, or when empty."""

    def __init__(self, fleet: Fleet) -> None:
        self.fleet = fleet

    @property
    def can_control(self) -> bool:
        return self.fleet.can_control

    @property
    def planned(self) -> dict[str, PlannedRestart]:
        return {name: m.planned for name, m in self.fleet.managers.items() if m.planned is not None}

    @property
    def busy(self) -> set[str]:
        return {name for name, m in self.fleet.managers.items() if m.phase in BUSY}

    def is_up(self, server: str) -> bool:
        manager = self.fleet.managers.get(server)
        return manager is not None and manager.phase in UP

    async def restart(self, server: str, *, when: str = "now", reason: str = "", by: str | None = None,
                      countdown: float | None = None) -> PlannedRestart:
        if not self.can_control:
            raise LifecycleUnavailable(f"backend = {self.fleet.backend.name} can't restart servers")
        if when not in WHEN:
            raise ValueError(f"when must be one of {', '.join(WHEN)}")
        manager = self.fleet.managers.get(server)
        if manager is None:
            raise LifecycleError(f"no server called {server}")
        by = by or self.fleet.settings.name
        result = manager.restart(immediate=countdown is not None and countdown <= 0, reason=reason, manual=False,
                                 by=by, when=when, countdown=countdown)
        if not result.ok:
            raise LifecycleUnavailable(result.message)
        return manager.planned or PlannedRestart(server, when, reason, by, countdown=countdown)

    def cancel(self, server: str) -> bool:
        manager = self.fleet.managers.get(server)
        return bool(manager and manager.cancel().ok)
