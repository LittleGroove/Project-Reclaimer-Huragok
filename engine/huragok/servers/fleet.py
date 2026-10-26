"""The fleet: every server's manager (server.py), ticked together once a second, and what a person, a request file
or a part can ask of them.

    Fleet       ticks each ServerManager, writes records/live.json for a second window, reads records/requests/
                (start.cmd stop, restart, say…) and answers them, and runs the window's verbs
    Lifecycle   the thin view parts get (ctx.lifecycle): restart now, after this game or when empty; cancel; the jobs
                that need a server stopped; the dashboard's flags
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from ..core.events import Event, EventBus
from ..core.files import write_atomic
from ..core.jobs import StoppedJob
from ..core.requests import Result
from ..core.serverfolders import ServerConfig
from ..core.settings import Settings
from ..rcon import RconClient
from .backends import Backend, LifecycleError, LifecycleUnavailable
from .ports import PortWatch
from .server import BUSY, DOWN, SETTLED, UP, WHEN, PlannedRestart, ServerManager
from .state import ServerState

log = logging.getLogger(__name__)


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
        self.live_path = settings.live_file
        self._live_written = float("-inf")  # monotonic
        self._requests_tasks: set[asyncio.Task[None]] = set()
        self.ports = PortWatch()  # whether a stopped server's port is taken, without holding the loop
        # More for records/live.json, from the supervisor: what the second window shows beyond the servers.
        self.live_extra: Callable[[], dict[str, Any]] | None = None
        # What a server lacks before it starts (repair.GameFiles.prelaunch): why it waits, or None.
        self.prelaunch: Callable[[ServerConfig], str | None] | None = None
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
        if number.isascii() and number.isdecimal():  # "²" is a digit to Python, not a position
            index = int(number)
            names = list(self.managers)
            return self.managers[names[index - 1]] if 0 < index <= len(names) else None
        return None

    def names(self) -> list[str]:
        return list(self.managers)

    def release(self) -> None:
        """Game files arrived (or getting them failed): every server waiting for them looks again now, with a fresh
        count of failed starts (the ones that failed for want of the files don't count against it)."""
        for manager in self.managers.values():
            if manager.waiting_kind == "files":
                manager.waiting_checked = float("-inf")
                if manager.record.fail_count:
                    manager.record.fail_count = 0
                    manager._save()

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
        from .. import __version__

        now = now or time.time()
        data = {"updated": now, "pid": os.getpid(), "version": __version__, "backend": self.backend.name,
                "stopping_all": self.stopping_all, "servers": self.rows(now)}
        if self.live_extra is not None:
            try:
                data.update(self.live_extra())
            except Exception:  # noqa: BLE001 - the servers' rows are what matters: written without the extras
                log.debug("couldn't add the extras to %s", self.live_path, exc_info=True)
        try:
            self.live_path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.live_path, json.dumps(data))
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
                    "update_workshop", "game_files", "game_files_retry", "banall"):
            if self.on_request is None:
                return [("Huragok", Result(False, "not available"))]
            if verb == "banall":  # Huragok's own ban list, kept by the commands part: one server's ban onto it
                return [(server, await self.on_request(verb, {**options, "server": server}))]
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
        from ..core import mailbox

        try:
            requests = mailbox.read(self.settings.records)
        except OSError:
            return
        for request in requests:
            task = asyncio.ensure_future(self._answer(request))
            self._requests_tasks.add(task)
            task.add_done_callback(self._requests_tasks.discard)

    async def _answer(self, request: Any) -> None:
        from ..core import mailbox

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

    def is_stopped(self, server: str) -> bool:
        """Stopped (by a person, or with everything else), faulted, or waiting out a failed start: nothing automatic
        starts it then. Not "offline" (one that can't be reached), nor before the fleet has looked at its servers."""
        manager = self.fleet.managers.get(server)
        return self.fleet.booted and manager is not None and manager.phase in DOWN - {"offline"}

    async def restart(self, server: str, *, when: str = "now", reason: str = "", by: str | None = None,
                      countdown: float | None = None, manual: bool = False) -> PlannedRestart:
        """`manual`: a person asked (an admin's !restartserver), as the window's R: it isn't counted against
        [restarts] restarts_per_hour, the budget that stops automatic restarts looping."""
        if not self.can_control:
            raise LifecycleUnavailable(f"backend = {self.fleet.backend.name} can't restart servers")
        if when not in WHEN:
            raise ValueError(f"when must be one of {', '.join(WHEN)}")
        manager = self.fleet.managers.get(server)
        if manager is None:
            raise LifecycleError(f"no server called {server}")
        by = by or self.fleet.settings.name
        result = manager.restart(immediate=countdown is not None and countdown <= 0, reason=reason, manual=manual,
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
