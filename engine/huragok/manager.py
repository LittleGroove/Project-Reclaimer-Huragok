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
  stopping   stop sent, waiting for the process to go         idle, launch, running (it couldn't be stopped)
  preparing  stopped; jobs that need it stopped are running   booting, idle (a stop calls them off)
             (a Workshop mod update swapped in), then it starts
  restarting the backend's own restart command is running     booting, stopping (a stop asked meanwhile)
  backoff    died; trying again at a set time                 launch, idle
  faulted    stopped trying for now; one more try after a      launch
             cool-down (faulted_retry_minutes), or S
  external   running, but not started from here               idle
  offline    (attach) not reachable                           external

What must survive closing the supervisor is in records/servers/<server>.json:
whether the server is wanted running, whether it's faulted and when it tries again,
how many starts in a row failed, the automatic restarts of the last hour, and the
daily restart's date.

How long something has taken (a start, a stop, a backoff, a countdown, a plan's
wait) is measured on the monotonic clock: the clock being set at boot can move
wall-clock time by minutes, which would fail every start in progress or stretch
a backoff. Wall-clock time is kept for what is stored or is a time of day.
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
from .lifecycle import Backend, LifecycleError, LifecycleUnavailable, PortWatch
from .lore import NOTICES, SIGN_OFF
from .odst import console_refusal
from .playlists import playlist_files, rename_old_labels
from .rcon import RconClient, RconError, masked
from .state import ServerState

if TYPE_CHECKING:
    from .tomledit import set_server_setting  # noqa: F401

log = logging.getLogger(__name__)

PHASES = ("idle", "launch", "preparing", "booting", "running", "countdown", "stopping", "restarting", "backoff",
          "faulted",
          "external", "offline")
BUSY = {"launch", "preparing", "booting", "countdown", "stopping", "restarting"}  # mid-flight: nothing else started
SETTLED = {"idle", "faulted", "offline", "external"}  # where `stop everything` is done with a server
UP = {"running", "countdown", "external"}
DOWN = {"idle", "faulted", "backoff", "offline"}  # not running (one in backoff tries again by itself)
WHEN = ("now", "after_game", "when_empty")
AFTER_GAME_LIMIT = 1800.0  # a restart "after this game" waits at most this long
DAILY_WINDOW = 1800.0  # the daily restart only fires within this long after its time
JOB_LIMIT = 1200.0  # a job before a start (a Workshop swap) gets this long; then the server starts anyway
INFERRED_ALIVE_SECONDS = 30.0  # without a process to watch: status.json this fresh means up
HISTORY_DAYS = 7  # what records/servers/<server>.json remembers of starts, restarts, crashes
HISTORY_MAX = 60


@dataclass
class ServerRecord:
    """What the supervisor remembers about a server between runs."""

    wanted: str = "stopped"  # running | stopped
    faulted: bool = False
    fault_reason: str = ""
    fail_count: int = 0
    retry_at: float | None = None  # FAULTED: when it tries once more, Unix seconds (None: it waits for S)
    retries: int = 0  # tries after FAULTED since the last run that lasted: picks the next cool-down
    restart_times: list[float] = field(default_factory=list)  # automatic restarts, Unix seconds
    daily_last_fired: str = ""  # "2026-10-16"
    last_start: float | None = None
    history: list[list[Any]] = field(default_factory=list)  # [when, kind, what]: start, restart, stop, crash, faulted

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
        record.history = [list(h) for h in record.history if isinstance(h, list) and len(h) == 3]
        return record

    def remember(self, kind: str, text: str, now: float | None = None) -> None:
        now = now or time.time()
        cutoff = now - HISTORY_DAYS * 86400
        self.history = [h for h in self.history if float(h[0]) > cutoff][-(HISTORY_MAX - 1):] + [[now, kind, text]]

    def recent(self, seconds: float = 86400, kinds: tuple[str, ...] = ("restart", "crash")) -> list[list[Any]]:
        cutoff = time.time() - seconds
        return [h for h in self.history if float(h[0]) > cutoff and h[1] in kinds]

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
    target: float | None  # when it happens (monotonic)
    step: str = "ladder"  # ladder | go
    warned: bool = False
    empty_since: float | None = None  # monotonic

    @property
    def label(self) -> str:
        return self.reason or ("manual restart" if self.coming_back else "manual stop")


@dataclass
class PlannedRestart:
    server: str
    when: str
    reason: str
    by: str
    requested: float = field(default_factory=time.time)  # when it was asked for (Unix seconds)
    countdown: float | None = None  # seconds of notice once it's due; None: the after_game setting
    since: float = field(default_factory=time.monotonic)  # the same, monotonic: how long it has waited


@dataclass
class StoppedJob:
    """Work that needs the server stopped (swapping in a Workshop mod's new version): it runs at the next start,
    between the stop and the start of a restart. Returns what it did, for the log."""

    key: str  # one job per key: a newer one replaces it
    label: str  # what the dashboard shows while it runs
    run: Callable[[], Awaitable[str]]


@dataclass
class Result:
    ok: bool
    message: str
    text: str = ""


def since_start(lines: list[str]) -> list[str]:
    """A console's lines from the last start on (the "──── … started by …" line Huragok writes at each one)."""
    starts = [n for n, line in enumerate(lines) if line.startswith("────") and " started by " in line]
    return lines[starts[-1] + 1:] if starts else lines


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
        return f"{NOTICES['calm']} {what} in {when}: {label}."
    return f"{NOTICES['calm']} {what} in {when}."


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
        self.phase_since = time.monotonic()
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
        self.backoff_until = 0.0  # monotonic
        self.stop_failed = ""  # why the last stop left the process running ("" when it didn't)
        self.revive = False  # the next start follows a death or a failed start: see Backend.revive
        self.started: float | None = None  # when this run began (Unix seconds)
        self.health = "healthy"  # the watchdog's word, for the dashboard
        self.health_reasons: list[str] = []  # what the watchdog saw
        self.notes: dict[str, float] = {}
        self.booted = False
        self.jobs: list[StoppedJob] = []  # run at the next start, while it's stopped
        self.job_running: str = ""  # the label of the one running now
        self.flags: dict[str, str] = {}  # short notes for the dashboard's detail line, by who set them

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
        self.phase_since = time.monotonic()

    def _save(self) -> None:
        self.record.save(self.record_path)

    # Booting the manager ----------------------------------------------------------------

    def boot(self) -> None:
        """Once, when the supervisor starts: this is what makes a reboot come back by itself."""
        if self.server.autostart:
            self.record.wanted, self.record.faulted, self.record.fault_reason, self.record.fail_count = \
                "running", False, "", 0
            self.record.retry_at, self.record.retries = None, 0
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
        elif phase == "preparing":
            self._step_preparing()
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
            else:
                self._step_faulted()
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
        if self.backend.sees_processes:
            taken = self.fleet.ports.in_use(self.server)
            if taken:
                self._set_phase("external")
                self.started = time.time()
                log.warning("[%s] already running, started some other way (something answers on RCON port %s). "
                            "Watching and moderating it; stop it and start it from here to let the "
                            "Superintendent restart it", self.name, self.server.rcon_port)
                return
            if taken is None:
                return  # not known yet (it's being found out in the background): never start onto a taken port
        if self.record.wanted == "running" and not self.record.faulted and not self.server.errors:
            self._set_phase("launch")

    def _step_launch(self) -> None:
        if not self._still_wanted():
            return
        if self.jobs and not self.alive:
            # The jobs waiting now run, and then it starts whatever they do: a job added while they run (one
            # re-adding itself after failing) waits for the next start instead of keeping it stopped for good.
            jobs, self.jobs = self.jobs, []
            self._run_task("prepare", self._run_jobs(jobs))
            self._set_phase("preparing")
            return
        self._launch()

    def _still_wanted(self) -> bool:
        if self.record.wanted != "running":
            self._set_phase("idle")
            return False
        if self.server.errors:
            self.note("not starting: " + "; ".join(self.server.errors))
            self._set_phase("idle")
            return False
        return True

    def _launch(self) -> None:
        self._run_ended("is starting again")
        self.started = time.time()
        self.record.last_start = self.started
        self._save()
        self.state.players.clear()
        start = self.backend.revive(self.server) if self.revive else self.backend.start(self.server)
        self.revive = False
        playlists = playlist_files(self.server.folder)
        self._run_task("start", self._labels_then(start, playlists) if playlists else start)
        self._set_phase("booting")
        log.info("[%s] starting; allowing %s for it to answer", self.name, format_duration(self.setting("ready_seconds")))

    async def _labels_then(self, start: Awaitable[None], playlists: list[Path]) -> None:
        """The playlists' labels first, then the start. In a thread: asking the program its version runs it (through
        wine on Linux, seconds), and nothing may hold the loop."""
        try:
            await asyncio.to_thread(self._rename_old_labels, playlists)
        except asyncio.CancelledError:
            close = getattr(start, "close", None)
            if close is not None:
                close()  # never started
            raise
        except Exception:  # noqa: BLE001 - the labels must never keep the server from starting
            log.exception("[%s] couldn't check its playlists' labels; starting it anyway", self.name)
        await start

    def _rename_old_labels(self, playlists: list[Path]) -> None:
        """Game option labels a server program doesn't know (0.9.19 renamed some, so an older one wants the old
        names) stop it starting with a playlist that names them: write them as the program about to run calls them."""
        version = self._program_version()
        for path in playlists:
            try:
                renamed = rename_old_labels(path, version)
            except OSError as error:
                log.warning("[%s] couldn't update %s's renamed labels: %s", self.name, path.name, error)
                continue
            if renamed:
                log.info("[%s] %s: %s, as %s calls them (other names stop it starting)", self.name, path.name,
                         ", ".join(f"'{old}' is now '{new}'" for old, new in renamed),
                         f"Project Reclaimer {version}" if version else "every version of Project Reclaimer")

    def _program_version(self) -> str | None:
        """The version of the program about to run: its file's own (`--version`, asked once per file), else what its
        console said on its last run; None when neither is known."""
        from .serverupdates import program_of, program_version, readable

        program = program_of(self.settings, self.server)
        version = program_version(self.settings, program) if program is not None else None
        if readable(version):
            return version
        return self.state.version if readable(self.state.version) else None

    def _step_preparing(self) -> None:
        if self.task is not None:
            return
        if self.task_error is not None:
            log.error("[%s] a job before the start failed: %s", self.name, self.task_error)
            self.task_error = None
        if self._still_wanted():
            self._launch()  # always on to the start, never back to the jobs

    async def _run_jobs(self, jobs: list[StoppedJob]) -> None:
        left = list(jobs)
        try:
            while left:
                job = left[0]
                self.job_running = job.label
                log.info("[%s] while it's stopped: %s", self.name, job.label)
                try:
                    said = await asyncio.wait_for(job.run(), JOB_LIMIT)
                except TimeoutError:
                    log.error("[%s] %s took longer than %s; starting it anyway", self.name, job.label,
                              format_duration(JOB_LIMIT))
                except Exception:  # noqa: BLE001 - a job going wrong must never keep the server down
                    log.exception("[%s] %s went wrong; starting it anyway", self.name, job.label)
                else:
                    if said:
                        log.info("[%s] %s", self.name, said)
                left.pop(0)
        except asyncio.CancelledError:
            # Stopped while they ran: what didn't finish runs at the next start, unless a newer one replaced it.
            newer = {j.key for j in self.jobs}
            self.jobs = [j for j in left if j.key not in newer] + self.jobs
            raise
        finally:
            self.job_running = ""

    def add_job(self, job: StoppedJob) -> None:
        """Run `job` at the next start (a newer one with the same key replaces it)."""
        self.jobs = [j for j in self.jobs if j.key != job.key] + [job]

    def drop_job(self, key: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.key != key]
        return len(self.jobs) != before

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
        if time.monotonic() - self.phase_since > float(self.setting("ready_seconds")):
            self._fail(f"the server never answered on RCON within {format_duration(self.setting('ready_seconds'))}",
                       kill=True, show_output=True)

    def _ready(self, now: float) -> None:
        self._set_phase("running")
        self.revive = False
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
        if (self.record.fail_count or self.record.retries) and uptime >= float(self.setting("good_run_seconds")):
            if self.record.retries:
                log.info("[%s] back to normal after FAULTED: up %s", self.name, format_duration(uptime))
            self.record.fail_count, self.record.retries = 0, 0
            self._save()
        if self.planned is not None and time.monotonic() - self.planned.since > AFTER_GAME_LIMIT:
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
        # doesn't decide it missed 04:00 and restart everything. Compared as instants, not
        # clock readings: on the night the clocks go forward, a slot inside the skipped hour
        # (02:30) is never read off the clock, but its instant still comes.
        at = slot.timestamp()
        if at <= now < at + DAILY_WINDOW:
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
            mono = time.monotonic()
            remaining = countdown.target - mono
            due = [r for r in countdown.pending if remaining <= r]
            if due:
                fire = min(due)
                countdown.pending = [r for r in countdown.pending if r < fire]
                self._say(warning_text(fire, countdown.label, countdown.coming_back))
                countdown.warned = True
            if remaining <= 0:
                countdown.step = "go"
            elif self.state.connected and not self.players:
                countdown.empty_since = countdown.empty_since if countdown.empty_since is not None else mono
                if mono - countdown.empty_since >= float(self.setting("empty_seconds")):
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
        error, self.task_error = self.task_error, None
        if error is not None:
            log.error("[%s] stopping failed: %s", self.name, error)
        if alive and self.backend.sees_processes:
            # The backend sees the process, so this is certain: it's still running. Calling it stopped would
            # have it picked up again as running at the next tick, the stop (or the restart) silently undone.
            self._stop_failed(error or "its process is still running after the stop")
            return
        if alive and time.monotonic() - self.phase_since < float(getattr(self.backend, "stop_timeout", 30.0)) + 10:
            return  # inferred from its RCON and status.json, which take a while to go quiet
        if alive:
            self.note("still reachable after the stop; carrying on anyway", 60)
        self._complete_stop()

    def _stop_failed(self, why: str) -> None:
        """The stop didn't end the process: it's still up and still ours, so it's running, and nothing pretends
        otherwise (a `stop everything` is told, and a restart reports nothing restarted)."""
        what = "restart" if self.coming_back else "stop"
        pid = self.pid
        self.stop_failed = why
        self.coming_back = self.failing = False
        self.countdown = None
        log.error("[%s] couldn't stop it: %s. It's still running, so the %s didn't happen. End %s by hand "
                  "(Task Manager, or kill), then start it here", self.name, why, what,
                  f"process {pid}" if pid else "its process")
        self.flags["stop"] = f"! couldn't stop it: {why}"[:120]
        self.record.remember("stop", f"couldn't stop it: {why}")
        self._save()
        if self.started is None:
            self.started = self.backend.started_at(self.server) or time.time()
        self._set_phase("running")

    def _step_restarting(self, now: float) -> None:
        if self.task is not None:
            return
        if self.task_error is not None:
            error, self.task_error = self.task_error, None
            self._fail(f"the restart command failed: {error}")
            return
        self._run_ended("is starting again")
        self.started = time.time()
        self.record.last_start = self.started
        self._save()
        self.client.wake()
        if self.record.wanted != "running" and self.backend.can_stop:
            # A stop asked for while the restart command ran: it can't be interrupted, so it's honoured now.
            log.info("[%s] restarted, and a stop was asked for meanwhile: stopping it", self.name)
            self.coming_back = False
            self.stop_label = self.stop_label or "manual stop"
            self._run_task("stop", self.backend.stop(self.server))
            self._set_phase("stopping")
            return
        self._set_phase("booting")

    def _step_backoff(self, now: float, alive: bool) -> None:
        if alive:
            self._adopt()
            return
        if self.record.wanted != "running":
            self._set_phase("idle")
            return
        if time.monotonic() >= self.backoff_until:
            self._set_phase("launch")

    def _step_external(self, alive: bool) -> None:
        if alive:
            return
        if self.backend.sees_processes and self.fleet.ports.in_use(self.server) is not False:
            return  # its port is still taken (or that isn't known yet)
        log.info("[%s] no longer running", self.name)
        self.started = None
        self._run_ended("stopped")
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
        if self.backend.sees_processes:
            log.error("[%s] the server exited on its own after %s%s", self.name, format_duration(uptime),
                      f" (exit code {code})" if code is not None else "")
        else:  # no process to watch: its console and status.json went quiet, so it died or hung
            log.error("[%s] the server stopped answering after %s (RCON gone and status.json not written for %s): "
                      "it died or hung", self.name, format_duration(uptime), format_duration(INFERRED_ALIVE_SECONDS))
        self.record.remember("crash", f"exited after {format_duration(uptime)}"
                             + (f" (exit code {code})" if code is not None else ""))
        for line in self.backend.last_console_lines(self.server, 4):
            log.error("[%s]   | %s", self.name, line)
        self.backend.forget(self.server)
        self.started = None
        self.state.players.clear()
        self._run_ended("exited")
        self.revive = not self.backend.sees_processes
        if self.record.wanted != "running":
            self._set_phase("idle")
            return
        if uptime >= float(self.setting("good_run_seconds")):
            self.record.fail_count, self.record.retries = 0, 0
        self._register_failure()

    def _fail(self, why: str, *, kill: bool = False, show_output: bool = False) -> None:
        log.error("[%s] %s", self.name, why)
        lines = self.backend.last_console_lines(self.server, 40) if show_output else []
        for line in lines[-6:]:
            log.error("[%s]   | %s", self.name, line)
        self.fleet.bus.emit("server.start_failed", self.name, why=why, lines=since_start(lines))
        self._run_ended("didn't start")
        self.revive = not self.backend.sees_processes
        # Without a process to watch, one that never answered and writes no status.json may still be running, hung:
        # its stop command runs. (One whose status.json is fresh is up, only its console doesn't answer: left be.)
        stoppable = self.alive if self.backend.sees_processes else self.backend.can_stop and not self.alive
        if kill and stoppable:
            log.warning("[%s] %s: it never finished starting", self.name, "ending the process"
                        if self.backend.sees_processes else "running its stop command")
            self.failing = True
            self.coming_back = False
            self._run_task("stop", self.backend.stop(self.server, timeout=5))
            self._set_phase("stopping")
            return
        self.backend.forget(self.server)
        self.started = None
        self._register_failure()

    def _run_ended(self, how: str) -> None:
        """A run of the server is over (or a new one is starting): what belonged to that run goes with it. A restart
        planned for after its game has in effect happened (a daily restart, a crash, a stop), so it mustn't restart
        the next run; nor may that run's first lobby count as the old game ending. Jobs are kept: they run at the
        next start, whatever brings it."""
        self.playing = None
        self.stop_failed = ""
        self.flags.pop("stop", None)
        if self.planned is not None:
            plan, self.planned = self.planned, None
            log.info("[%s] the restart planned %s (%s) is dropped: the server %s", self.name,
                     plan.when.replace("_", " "), plan.reason or "no reason given", how)

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
            self.backoff_until = time.monotonic() + delay
            self._set_phase("backoff")
        else:
            log.warning("[%s] trying again now (failure %s of %s)", self.name, fails, limit)
            self._set_phase("launch")

    def _retry_delay(self) -> float | None:
        """Seconds until the next try after FAULTED (the cool-downs in turn, the last repeating), or None: off."""
        minutes = [float(m) for m in self.setting("faulted_retry_minutes") or []]
        return minutes[min(self.record.retries, len(minutes) - 1)] * 60 if minutes else None

    def _retry_text(self) -> str:
        """When it tries again, as the window and the log say it: "at 03:41 (in 29m)"."""
        at = self.record.retry_at
        if at is None:
            return ""
        return f"at {time.strftime('%H:%M', time.localtime(at))} (in {format_duration(max(0.0, at - time.time()))})"

    def _enter_faulted(self, why: str) -> None:
        delay = self._retry_delay()
        self.record.faulted, self.record.fault_reason = True, why
        self.record.retry_at = time.time() + delay if delay is not None else None
        self.record.remember("faulted", why)
        self._save()
        path = self.settings.logs / f"faulted-{self.server.slug}.txt"
        launcher = os.environ.get("HURAGOK_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")
        then = self._retry_delay_after_this()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            title = f"{self.name} · stopped trying at {time.strftime('%Y-%m-%d %H:%M:%S')}"
            console = self.settings.logs / f"console-{self.server.slug}.log"
            if self.record.retry_at is not None:
                todo = [f"Next try    {self._retry_text()}, then every {format_duration(then)} until a run lasts "
                        f"{format_duration(float(self.setting('good_run_seconds')))}",
                        f"What to do  fix the cause if the lines below show one; S in the window (or {launcher} "
                        f"restart \"{self.name}\") starts it at once"]
            else:
                todo = [f"What to do  fix the cause, then start it again: S in the window, or {launcher} restart "
                        f"\"{self.name}\""]
            lines = [title, "─" * max(60, len(title)), "",
                     f"Why         {why}",
                     *todo,
                     f"Its folder  {self.server.folder}",
                     f"Its console {console}", "",
                     "──── The last lines of its console ────", "",
                     *self.backend.last_console_lines(self.server, 200)]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        if self.record.retry_at is not None:
            log.error("[%s] STOPPED TRYING FOR NOW - it tries once more %s", self.name, self._retry_text())
        else:
            log.error("[%s] STOPPED - THE SERVER WILL NOT COME BACK ON ITS OWN ([restarts] faulted_retry_minutes "
                      "= [])", self.name)
        log.error("[%s] %s", self.name, why)
        log.error("[%s] Details and the last lines of its output: %s. Fix the cause if they show one; S starts it "
                  "at once.", self.name, path)
        self.fleet.bus.emit("server.faulted", self.name, reason=why, retry_at=self.record.retry_at)
        self._set_phase("faulted")

    def _retry_delay_after_this(self) -> float:
        """The cool-down after the coming try, should that fail too (for saying "then every …")."""
        minutes = [float(m) for m in self.setting("faulted_retry_minutes") or []] or [0.0]
        return minutes[min(self.record.retries + 1, len(minutes) - 1)] * 60

    def _step_faulted(self) -> None:
        """FAULTED is a pause: after its cool-down, one more try. A failure brings it straight back here with the
        next cool-down; a run that lasts good_run_seconds is back to normal (_step_running)."""
        at = self.record.retry_at
        if at is None or time.time() < at:
            return
        if self.record.wanted != "running" or not self.backend.can_start or self.server.errors:
            return  # stopped by a person since, or it can't be started from here: it waits, as it is
        self.record.retries += 1
        self.record.retry_at = None
        reason = self.record.fault_reason
        self.record.faulted, self.record.fault_reason = False, ""
        # One try, not a new ladder: a failure goes straight back to FAULTED (and waits the next cool-down).
        self.record.fail_count = max(int(self.setting("max_failures")) - 1, 0)
        self.record.remember("start", f"tried again after FAULTED (try {self.record.retries}): {reason}")
        self._save()
        log.warning("[%s] trying again after FAULTED (try %s): %s", self.name, self.record.retries, reason)
        self.fleet.bus.emit("server.fault_retry", self.name, attempt=self.record.retries, reason=reason)
        self._set_phase("launch")

    def _begin_countdown(self, reason: str, *, coming_back: bool, manual: bool, by: str, immediate: bool = False,
                         seconds: float | None = None) -> None:
        if seconds is None:
            rungs = sorted({float(s) for s in self.setting("warn_seconds") if float(s) > 0}, reverse=True)
        else:
            rungs = sorted({s for s in (float(seconds), 10.0) if 0 < s <= float(seconds)}, reverse=True)
        countdown = Countdown(reason, coming_back, manual, by, rungs, None)
        label = countdown.label
        self.stop_failed = ""  # a new try: a `stop everything` waits for this one
        self.flags.pop("stop", None)
        verb = "restart" if coming_back else "stop"
        log.info("[%s] %s requested: %s", self.name, verb, label)
        self.record.remember(verb, f"{label} (by {by})" if by and by != self.settings.name else label)
        self._save()
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
            countdown.target = time.monotonic() + rungs[0]
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
        self.stop_failed = ""
        self.flags.pop("stop", None)
        if coming_back and self.backend.restart_only:
            over = self._over_budget()
            if over and self.record.wanted == "running":
                # The restart command can't be stopped half way, and with no stop command there's no stopping it
                # either: the one thing to do is not restart it again this hour.
                log.error("[%s] %s Not restarting it: it keeps running as it is", self.name, over)
                self.coming_back = False
                self.record.remember("restart", "not restarted: over the hourly budget")
                self._save()
                self._set_phase("running")
                return
        if self.state.connected and self.players:
            self._say(f"{NOTICES['dust']} " + ("Server is restarting now." if coming_back
                                                else "Server is shutting down now."))
        if coming_back:
            self.fleet.bus.emit("server.restarting", self.name, reason=label, by=self.stop_by)
        if coming_back and self.backend.restart_only:
            self._run_task("restart", self.backend.restart(self.server))
            self._set_phase("restarting")
            return
        log.info("[%s] %s: stopping the server", self.name, "restart" if coming_back else "stop")
        self._run_task("stop", self.backend.stop(self.server))
        self._set_phase("stopping")

    def _over_budget(self) -> str:
        """Why one more restart would be too many this hour ("" when it wouldn't)."""
        cutoff = time.time() - 3600
        recent = [t for t in self.record.restart_times if t > cutoff]
        budget = int(self.setting("restarts_per_hour"))
        if len(recent) <= budget:
            return ""
        return (f"{len(recent)} automatic restarts in one hour is over the budget of {budget}. Something is "
                "triggering in a loop; stopping here rather than thrashing.")

    def _complete_stop(self, *, died: bool = False) -> None:
        self.started = None
        self.state.players.clear()
        self._run_ended("stopped")
        if died:
            self.backend.forget(self.server)
        self.revive = died and not self.backend.sees_processes
        if self.failing:
            self.failing = False
            self.revive = not self.backend.sees_processes
            self._register_failure()
            return
        if self.coming_back and self.record.wanted == "running":
            over = self._over_budget()
            if over:
                self._enter_faulted(over)
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
        plan = PlannedRestart(self.name, when, reason, by, requested=time.time(), countdown=countdown,
                              since=time.monotonic())
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
            self._say(f"{NOTICES['dust']} This server restarts after this game{': ' + reason if reason else '.'}")
        return plan

    def on_phase(self, event: Event) -> None:
        phase, game = event.get("phase"), (event.get("map"), event.get("mode"))
        if phase == "playing":
            self.playing = game
            return
        # The server can send `finished` for the next game before it starts (seen on
        # 0.9.18), which must not count: only the game that was being played ends it.
        # An ODST server (0.9.19) goes from its game straight back to a new lobby.
        ended = phase == "finished" and self.playing == game or phase == "no_game" and self.playing is not None
        if not ended:
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
        self.record.retry_at, self.record.retries = None, 0
        self._save()
        if self.phase in ("idle", "faulted", "backoff", "offline"):
            log.info("[%s] start requested", self.name)
            self.record.remember("start", "started from here")
            self._save()
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
            return Result(False, "it wasn't started from here, so the Superintendent can't stop it; stop it the way it "
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
        if phase == "preparing":
            # Stopped already, running the jobs before its start: those are called off (what they hadn't finished
            # runs at its next start) and it stays stopped.
            label = self.job_running or "the jobs before its start"
            if self.task is not None:
                self.task.cancel()
            log.info("[%s] stop requested while it was stopped for %s: called off; it stays stopped", self.name,
                     label)
            self._set_phase("idle")
            return Result(True, f"it was stopped for {label}; that's called off and it stays stopped")
        if phase == "restarting":
            self.coming_back = False
            return Result(True, "its restart command is running; it's stopped once that's done")
        if phase == "stopping":
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
            return Result(False, "it wasn't started from here, so the Superintendent can't restart it; stop it the way "
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
        if self.planned is not None and self.phase in ("running", "external", "countdown"):
            log.info("[%s] the restart planned %s is now a restart now", self.name, self.planned.when.replace("_", " "))
            self.planned = None  # one restart now, not another after the next game
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
        if phase in ("booting", "launch", "preparing"):
            return Result(False, "it is still starting")
        return self.start()

    def cancel(self) -> Result:
        if self.phase == "countdown" and self.countdown and self.countdown.step == "ladder":
            countdown, self.countdown = self.countdown, None
            if countdown.warned:
                self._say(("The server restart has been cancelled." if countdown.coming_back
                           else "The server shutdown has been cancelled.") + f" {SIGN_OFF}")
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
        if self.server.plays_kind == "odst" and (refusal := console_refusal(line)):
            log.info("[%s] console command not sent (an ODST server plays what its file names): %s", self.name, masked(line))
            return Result(False, "refused", refusal)
        if not self.client.connected:
            return Result(False, "its RCON isn't connected")
        try:
            reply = await self.client.command(line, by=by, timeout=15)
        except RconError as error:
            return Result(False, str(error))
        log.info("[%s] console command: %s", self.name, masked(line))
        return Result(reply.ok, "ok" if reply.ok else "refused", reply.text)

    def set_autostart(self, value: bool) -> Result:
        result = self.set_setting("autostart", value)
        if result.ok:
            log.info("[%s] autostart turned %s", self.name, "ON" if value else "OFF")
            return Result(True, f"autostart {'ON' if value else 'off'}")
        return result

    def set_setting(self, key: str, value: Any) -> Result:
        """One of Huragok's own settings for this server, in its settings.toml [[server]] block (None takes it out,
        so the general setting applies again). In force at once."""
        from .config import WORKSHOP_INSTALLS, ConfigError, check_clock_time
        from .tomledit import remove_server_setting, set_server_setting

        try:
            if key == "daily_restart" and value is not None:
                value = check_clock_time(str(value), "daily_restart")
            elif key == "workshop_updates" and value is not None and value not in WORKSHOP_INSTALLS:
                raise ConfigError(f"workshop_updates: one of {', '.join(WORKSHOP_INSTALLS)}")
            elif key == "autostart":
                value = bool(value) if value is not None else True
            elif key not in ("daily_restart", "workshop_updates"):
                raise ConfigError(f"{key} isn't one of Huragok's per-server settings")
        except ConfigError as error:
            return Result(False, str(error))
        try:
            if value is None:
                remove_server_setting(self.settings.path, self.name, key)
            else:
                set_server_setting(self.settings.path, self.name, key, value)
        except OSError as error:
            return Result(False, f"couldn't edit {self.settings.path.name}: {error}")
        override = self.settings.server_overrides.setdefault(self.name.lower(), {"name": self.name})
        if value is None:
            override.pop(key, None)
        else:
            override[key] = value
        setattr(self.server, key, value if value is not None or key == "autostart" else None)
        log.info("[%s] %s %s in %s", self.name, key, f"= {value}" if value is not None else "back to the default",
                 self.settings.path.name)
        return Result(True, f"{key} {'= ' + str(value) if value is not None else 'back to the default'}")

    # For the dashboard --------------------------------------------------------------------------

    def row(self, index: int, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        mono = time.monotonic()  # what the phase, backoff and countdown times are measured on
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
        elif phase == "preparing":
            status, color = f"PREPARING  {format_duration(mono - self.phase_since)}", "cyan"
            detail = f"before it starts: {self.job_running or 'jobs'}"
        elif phase == "booting":
            status, color = f"STARTING  {format_duration(mono - self.phase_since)}", "cyan"
            if self.task is not None and not self.backend.sees_processes:
                detail = "running the start command"
        elif phase == "running":
            status, color = f"RUNNING  up {up}", "green"
            if not state.connected:
                age = mono - self.phase_since
                if age > 30:
                    status, color = f"RUNNING  up {up} (RCON not connected)", "yellow"
            if self.health not in ("healthy", ""):
                color = "yellow"
        elif phase == "countdown" and self.countdown:
            verb = "RESTART" if self.countdown.coming_back else "STOP"
            if self.countdown.step == "go" or self.countdown.target is None:
                status = f"{verb} NOW"
            else:
                status = f"{verb} IN {format_duration(self.countdown.target - mono)}"
            color = "yellow"
            detail = f"{verb.lower()} reason: {self.countdown.label}   [K] cancels it"
        elif phase == "stopping":
            status, color = ("RESTARTING - stopping" if self.coming_back else "STOPPING"), "yellow"
        elif phase == "restarting":
            status, color = "RESTARTING", "yellow"
        elif phase == "backoff":
            status, color = f"CRASHED - retry in {format_duration(self.backoff_until - mono)}", "red"
        elif phase == "faulted":
            if self.record.retry_at is not None and self.record.wanted == "running":
                status = f"FAULTED - tries again in {format_duration(max(0.0, self.record.retry_at - time.time()))}"
                detail = f"{self.record.fault_reason}   next try {self._retry_text()}; [S] starts it now"
            else:
                status, detail = "FAULTED - [S] to retry", self.record.fault_reason
            color = "red"
        elif phase == "external":
            if self.backend.can_start and self.backend.sees_processes:
                status, color = f"RUNNING  up {up} - not started from here", "yellow"
                detail = "stop it the way it was started, then start it here, to let the Superintendent restart it"
            elif self.backend.can_start:
                status, color = f"RUNNING  up {up} - started elsewhere", "yellow"
            else:
                status, color = f"RUNNING  up {up} (watching)", "green"
        elif phase == "offline":
            status, color = "not reachable", "gray"
        for error in self.server.errors:
            detail = f"! {error}"
        urgent = [f for f in self.flags.values() if f.startswith("!")]  # first, so the line shows red
        quiet = [f for f in self.flags.values() if not f.startswith("!")]
        if phase in ("idle", "backoff") and not detail and self.flags:
            detail = "  |  ".join(urgent + quiet)
        if phase == "running" and not detail:
            bits = list(urgent)
            if self.planned is not None:
                bits.append(f"restart planned {self.planned.when.replace('_', ' ')}: {self.planned.reason or 'no reason'}")
            daily = self.next_daily_text()
            if daily:
                bits.append(daily)
            if self.health not in ("healthy", ""):
                bits.append(f"watchdog: {self.health}" + (f" ({'; '.join(self.health_reasons)})"
                                                          if self.health_reasons else ""))
            restarts = self.record.recent()
            if restarts:
                bits.append(f"{len(restarts)} restart(s)/crash(es) in 24 h")
            bits.extend(quiet)
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
            "health_reasons": list(self.health_reasons), "flags": list(self.flags.values()),
            "history": [list(h) for h in self.record.history[-12:]], "address": state.address or "",
            "pid": self.pid, "planned": (f"{self.planned.when.replace('_', ' ')}: {self.planned.reason or 'no reason'}"
                                         if self.planned is not None else ""),
            "daily": self.next_daily_text(), "fail_count": self.record.fail_count,
            "fault_reason": self.record.fault_reason if self.record.faulted else "",
            "retry_at": self.record.retry_at if self.record.faulted else None,  # FAULTED: its next try (Unix s)
            "rcon_ms": round(state.rcon_latency_ms) if state.rcon_latency_ms is not None else None,
            "status_age": round(state.status_age) if state.status_age is not None else None,
            "jobs": [job.label for job in self.jobs],
            "can": {
                "start": self.backend.can_start and phase in ("idle", "faulted", "backoff", "offline"),
                "stop": self.backend.can_stop and (phase in ("running", "countdown", "booting", "launch", "backoff",
                                                             "faulted", "stopping", "restarting", "preparing")
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
        self._live_written = float("-inf")  # monotonic
        self._requests_tasks: set[asyncio.Task[None]] = set()
        self.ports = PortWatch()  # whether a stopped server's port is taken, without holding the loop
        self.booted = False  # boot() has run: once per Fleet, however often run() is restarted
        bus.subscribe("rcon.phase", self._on_phase)
        bus.subscribe("rcon.leave", self._on_leave)
        bus.subscribe("watchdog.health", self._on_health)

    # Servers -------------------------------------------------------------------------------

    def add(self, server: ServerConfig, client: RconClient, state: ServerState) -> ServerManager:
        manager = ServerManager(self, server, client, state)
        self.managers[server.name] = manager
        if self.booted:
            manager.boot()  # added while running: treat like the start
        return manager

    def remove(self, name: str) -> None:
        self.managers.pop(name, None)

    def find(self, ref: str) -> ServerManager | None:
        """By name (any case), then data-folder name, then list position (#2, or 2 when no server is called 2)."""
        ref = ref.strip()
        for name, manager in self.managers.items():
            if name.lower() == ref.lower():
                return manager
        for manager in self.managers.values():
            if manager.server.slug == ref.lower():
                return manager
        number = ref[1:] if ref.startswith("#") else ref
        if number.isascii() and number.isdigit():  # "²" is a digit to Python, not a position
            index = int(number)
            names = list(self.managers)
            return self.managers[names[index - 1]] if 0 < index <= len(names) else None
        return None

    def names(self) -> list[str]:
        return list(self.managers)

    @property
    def can_control(self) -> bool:
        return self.backend.can_restart

    # Running ---------------------------------------------------------------------------------

    def boot(self) -> None:
        self.booted = True
        self.backend.adopt()
        for manager in self.managers.values():
            manager.boot()

    async def run(self) -> None:
        # Restarted after a failure, it carries on: booting again would forget what each server was doing (a restart
        # in flight would become a stop, a non-autostart server in backoff would stay down).
        if not self.booted:
            self.boot()
        self.tick()
        while True:
            await asyncio.sleep(1)
            self.tick()

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for manager in list(self.managers.values()):
            manager.tick(now)
        mono = time.monotonic()  # not the wall clock: set back an hour, live.json would stop for an hour
        if mono - self._live_written >= 2:
            self._live_written = mono
            self.write_live(now)
        try:
            self._read_requests()
        except Exception:  # noqa: BLE001 - a request must never stop the servers being looked after
            log.exception("reading the requests from outside failed")
        if self.stopping_all and all(self.managers[n].phase in SETTLED or self.managers[n].stop_failed
                                     for n in self._awaiting if n in self.managers):
            self.stopping_all = False
            self._awaiting.clear()
            if self.on_all_stopped:
                self.on_all_stopped()

    def unstoppable(self) -> list[str]:
        """Servers whose last stop left their process running."""
        return [name for name, m in self.managers.items() if m.stop_failed]

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
            manager.health_reasons = [str(r) for r in event.get("reasons") or []]

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

    def stop_all(self, *, immediate: bool = False, reason: str = "Huragok is stopping") -> list[tuple[str, Result]]:
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

    @staticmethod
    def means_all(ref: str) -> bool:
        return ref.strip().lower() in ("", "all", "*")

    def targets(self, ref: str) -> list[ServerManager]:
        if self.means_all(ref):
            return list(self.managers.values())
        manager = self.find(ref)
        return [manager] if manager else []

    async def verb(self, verb: str, server: str, **options: Any) -> list[tuple[str, Result]]:
        """One request, for one server or all; the answers by server name."""
        if verb == "stop_all":
            return self.stop_all(immediate=bool(options.get("immediate")))
        if verb in ("update", "check_update", "close", "check_program_update", "update_program", "check_workshop",
                    "update_workshop"):
            if self.on_request is None:
                return [("Huragok", Result(False, "not available"))]
            return [("Huragok", await self.on_request(verb, options))]
        targets = self.targets(server)
        if not targets:
            return [(server, Result(False, f"no server called '{server}'"))]
        results = []
        for manager in targets:
            if verb == "restart" and self.means_all(server):
                manager._notice()
            if verb == "restart" and self.means_all(server) and manager.phase in DOWN:
                # Restarting every server restarts the ones that are up: a stopped or faulted one stays as it is
                # (naming it restarts it, which starts it).
                results.append((manager.name, Result(True, "not running, so not restarted")))
                continue
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
        if verb == "server_setting":
            return manager.set_setting(str(options.get("key") or ""), options.get("value"))
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

    def add_job(self, server: str, job: StoppedJob) -> None:
        """Work that needs the server stopped, run at its next start (a restart's, or a person's)."""
        manager = self.fleet.managers.get(server)
        if manager is None:
            raise LifecycleError(f"no server called {server}")
        manager.add_job(job)

    def drop_job(self, server: str, key: str) -> bool:
        manager = self.fleet.managers.get(server)
        return bool(manager and manager.drop_job(key))

    def jobs(self, server: str) -> list[StoppedJob]:
        manager = self.fleet.managers.get(server)
        return list(manager.jobs) if manager else []

    def set_flag(self, server: str, who: str, text: str | None) -> None:
        """A short note on the server's dashboard line (None takes it away)."""
        manager = self.fleet.managers.get(server)
        if manager is None:
            return
        if text:
            manager.flags[who] = text
        else:
            manager.flags.pop(who, None)

    def phase(self, server: str) -> str | None:
        manager = self.fleet.managers.get(server)
        return manager.phase if manager else None

    def players(self, server: str) -> int:
        manager = self.fleet.managers.get(server)
        return manager.players if manager else 0
