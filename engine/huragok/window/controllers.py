"""What the window talks to: the running Huragok itself (LocalController, in the supervising window), or the
files it keeps (RemoteController, a second window: records/live.json, logs/huragok.log, records/requests/). The
dashboard and its screens ask a Controller and never care which."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .. import lore
from ..core import desktop, mailbox
from ..core.logs import RecentLog
from ..core.requests import Result
from ..core.settings import LIVE_FILE, launcher
from ..lore import HURAGOK, SUPERINTENDENT, VERGIL
from ..servers.backends import memory_mb

if TYPE_CHECKING:
    from ..huragok import Huragok

log = logging.getLogger(__name__)


LIVE_STALE = 15.0


# A logs/huragok.log line: date, time (ms dropped), level, the module that spoke (shown as its part's tag), the message.
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d (?P<time>\d\d:\d\d:\d\d)(?:,\d+)? (?P<level>\w+)\s+(?:(?P<name>[\w.]+): )?"
                      r"(?P<message>.*)$")


Row = dict[str, Any]


def _relative(path: Path | None, home: Path) -> str:
    """logs/huragok.log, as the host would find it from the folder."""
    if path is None:
        return "no log file"
    try:
        return path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def automod_modes(part: Any, servers: list[str]) -> str:
    """Vergil on each server, for the header: " (on)" when every server has the same, else
    " (on: A, B; dry run: C; off: D)"."""
    by: dict[str, list[str]] = {}
    for server in servers:
        by.setdefault(part.mode(server).replace("_", " "), []).append(server)
    if not by:
        return " (dry run)" if part.ctx.dry_run else ""
    if len(by) == 1:
        return f" ({next(iter(by))})"
    return " (" + "; ".join(f"{mode}: {', '.join(names)}" for mode, names in by.items()) + ")"


class Controller(Protocol):
    supervising: bool
    home: Path
    launcher: str

    def header(self) -> list[str]: ...
    def rows(self) -> list[Row]: ...
    def events(self, count: int) -> list[tuple[int, str]]: ...
    def problems(self) -> list[str]: ...
    def finished(self) -> str | None: ...
    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]: ...
    def request_quit(self) -> None: ...
    def refresh_settings(self) -> Any: ...  # the Settings, with the server folders as they are now
    def discover(self) -> None: ...  # look for server folders now (a new one was just made)
    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]: ...  # a server's events
    def card(self, server: str) -> list[tuple[str, list[tuple[str, str, str | None]]]]: ...  # V: its status card


class LocalController:
    """The supervising window: acts directly on the fleet."""

    supervising = True

    def __init__(self, supervisor: Huragok, recent: RecentLog) -> None:
        self.supervisor = supervisor
        self.recent = recent
        self.home = supervisor.settings.home
        self.launcher = launcher()

    def refresh_settings(self) -> Any:
        return self.supervisor.settings  # kept current by the supervisor's own discovery

    def discover(self) -> None:
        self.supervisor.superintendent.discover()

    @property
    def log_name(self) -> str:
        return _relative(self.supervisor.settings.log_file, self.home)

    def header(self) -> list[str]:
        """What each part is running: `SUPT backend process · …  |  VRGL commands · automod (dry run)  |  HRGK …`."""
        settings = self.supervisor.settings
        where = settings.servers_folder.name if settings.servers_folder else str(settings.single_folder)
        bits: dict[lore.Persona, list[str]] = {SUPERINTENDENT: [f"backend {settings.backend}", f"servers in {where}/"],
                                               VERGIL: [], HURAGOK: []}
        for name, runner in self.supervisor.runners.items():
            part = settings.parts[name]
            label = name
            if not runner.enabled:
                label += " (off: failing)"
            elif name == "automod" and not settings.dry_run and hasattr(runner.part, "mode"):
                label += automod_modes(runner.part, [s.name for s in settings.servers])
            elif part.dry_run or settings.dry_run:
                label += " (dry run)"
            elif name == "watchdog":
                label += f" ({runner.part.config['mode']})"
            bits[lore.speaker(f"part.{name}")].append(label)
        consoles = self.supervisor.vergil.relay.connected()
        if consoles:
            bits[VERGIL].append("consoles: " + ", ".join(consoles))
        if update := self.supervisor.updates.status_text():  # stays here: the events scroll it away
            bits[HURAGOK].append(update)
        status = self.supervisor.program_updates.status()
        versions = sorted(set(status["running"].values()))
        themselves = len(status.get("self_updating") or [])
        updating = (f" · {themselves} of {len(settings.servers)} update themselves" if themselves else "")
        if versions:
            bits[HURAGOK].append("server program " + "/".join(versions)
                                 + (f" ({status['pending']} out: U)" if status["pending"] else "") + updating)
        elif status["pending"]:
            bits[HURAGOK].append(f"server program {status['pending']} is out (U){updating}")
        return [f"{persona.tag} " + " · ".join(said) for persona, said in bits.items() if said]

    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return self.supervisor.bus.subscribe("rcon.*", lambda event: listener(event) if event.server == server else None)

    def rows(self) -> list[Row]:
        return self.supervisor.superintendent.fleet.rows()

    def card(self, server: str) -> list[tuple[str, list[tuple[str, str, str | None]]]]:
        """V: the server's status card, with what only the supervising window knows."""
        from . import statuscard

        sup = self.supervisor
        config = sup.settings.server(server)
        manager = sup.superintendent.fleet.managers.get(server)
        row = manager.row(manager_index(sup.superintendent.fleet, server)) if manager else {}
        pending = sup.program_updates.pending
        extra = {"release": pending.version if pending else None,
                 "program_note": sup.program_updates.note(server),
                 "workshop": sup.workshop_updates.item_lines(config) if config.workshop_items else None,
                 "workshop_disk": statuscard.workshop_disk(config) if config.workshop_items else None,
                 "memory_mb": memory_mb(row.get("pid")), "startup": desktop.startup_enabled(),
                 "problems": list(sup.superintendent.backend.problems(config))}
        return statuscard.build(sup.settings, config, row, extra)

    def events(self, count: int) -> list[tuple[int, str]]:
        return self.recent.recent(count)

    def problems(self) -> list[str]:
        return list(self.supervisor.problems)

    def finished(self) -> str | None:
        return "stopped" if self.supervisor.stopping else None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        return await self.supervisor.superintendent.fleet.verb(verb, server, **options)

    def request_quit(self) -> None:
        self.supervisor.stop()


class RemoteController:
    """A second window: the dashboard from records/live.json, requests through the mailbox."""

    supervising = False

    def __init__(self, settings: Any, is_running: Callable[[], int | None]) -> None:
        self.settings = settings
        self.home = settings.home
        self.records = settings.records
        self.launcher = launcher()
        self.is_running = is_running
        self._live: dict[str, Any] = {}
        self._live_read = 0.0

    def refresh_settings(self) -> Any:
        from ..core.tables import ConfigError
        from ..core.settings import load

        try:
            self.settings = load(self.settings.path)
        except ConfigError as error:
            log.warning("settings not re-read: %s", error)
        return self.settings

    def discover(self) -> None:
        pass  # the supervising window finds new folders by itself within 10 s

    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return lambda: None  # a second window has no connection of its own; the supervising window shows events

    def _read_live(self) -> dict[str, Any]:
        """records/live.json as the supervising window last wrote it. A read that fails (the file missing for a
        moment, or Windows refusing it while the writer replaces it) or a file that isn't what it should be keeps
        the last good copy: one bad read must not flash "no servers" across the dashboard."""
        if time.monotonic() - self._live_read < 0.9:
            return self._live
        self._live_read = time.monotonic()
        try:
            data = json.loads((self.records / LIVE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._live
        if isinstance(data, dict) and _stamp(data.get("updated")) is not None:
            servers = data.get("servers")
            data["servers"] = [r for r in servers if isinstance(r, dict) and isinstance(r.get("name"), str)] \
                if isinstance(servers, list) else []
            self._live = data
        return self._live

    def _age(self) -> float | None:
        """Seconds since the supervising window last wrote live.json; None when no good copy was read yet."""
        updated = _stamp(self._read_live().get("updated"))
        return None if updated is None else time.time() - updated

    @property
    def log_name(self) -> str:
        return _relative(self.settings.log_file, self.home)

    def answering(self) -> bool:
        age = self._age()
        return age is not None and age <= LIVE_STALE

    def header(self) -> list[str]:
        live = self._read_live()
        bits = [f"{SUPERINTENDENT.tag} backend {live.get('backend', self.settings.backend)}"]
        pid = self.is_running()
        if pid:
            update = str(live.get("huragok_update") or "")
            bits.append(f"{HURAGOK.tag} supervising window: process {pid}" + (f" · {update}" if update else ""))
        return bits

    def rows(self) -> list[Row]:
        if not self.answering():
            return []
        return list(self._read_live().get("servers") or [])

    def card(self, server: str) -> list[tuple[str, list[tuple[str, str, str | None]]]]:
        """V from a second window: the card from the files and the supervising window's live row."""
        from . import statuscard

        config = self.settings.server(server)
        row = next((r for r in self.rows() if r.get("name") == server), {})
        return statuscard.build(self.settings, config, row, {
            "workshop_disk": statuscard.workshop_disk(config) if config.workshop_items else None,
            "startup": desktop.startup_enabled()})

    def problems(self) -> list[str]:
        age = self._age()
        if age is None:
            return ["no word from the supervising window yet (it writes records/live.json every second or two)"]
        if age > LIVE_STALE:
            return [f"the supervising window hasn't updated for {age:.0f} s"]
        return []

    def events(self, count: int) -> list[tuple[int, str]]:
        path = self.settings.log_file
        if not path or not path.exists():
            return []
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(max(0, size - 16 * 1024))
                lines = file.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for line in [l for l in lines if l.strip()][-count:]:
            level = logging.ERROR if " ERROR " in line or " CRITICAL " in line else \
                logging.WARNING if " WARNING " in line else logging.INFO
            match = LOG_LINE.match(line)
            if match:  # the file's line shown as the supervising window shows its own: time, tag, level, message
                tag = lore.speaker(match.group("name") or "").tag
                line = f"{match.group('time')} {tag} {match.group('level'):<7} {match.group('message')}"
            out.append((level, line))
        return out

    def finished(self) -> str | None:
        return None if self.is_running() else "takeover"

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        """A request through the mailbox, answered by the supervising window. The wait is longer than the work:
        a download or a Steam check can take minutes."""
        wait = remote_wait(verb)
        request_id = await asyncio.to_thread(mailbox.post, self.records, verb, server, **options)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            reply = await asyncio.to_thread(mailbox.take_reply, self.records, request_id)
            if reply is not None:
                return [(server, Result(reply.ok, reply.message, reply.text))]
            await asyncio.sleep(0.25)
        request = mailbox.folder(self.records) / f"{request_id}.json"
        if request.exists():  # never picked up: taken back, so it doesn't happen later by surprise
            request.unlink(missing_ok=True)
            return [(server, Result(False, f"the supervising window didn't take the request in {wait:.0f} s "
                                           "(is it still running?); nothing was done"))]
        return [(server, Result(False, f"no answer yet after {wait:.0f} s: it may still be doing it (the "
                                       "supervising window's events will say)"))]

    def request_quit(self) -> None:
        pass


# How long a second window waits for the supervising window's answer, by request: longer than the work takes.
REMOTE_WAITS = {"update": 600.0, "update_program": 900.0, "update_workshop": 600.0, "check_update": 180.0,
                "check_program_update": 180.0, "check_workshop": 300.0, "rcon": 30.0, "say": 30.0,
                "game_files": 60.0, "game_files_retry": 30.0, "banall": 60.0}


def remote_wait(verb: str) -> float:
    return REMOTE_WAITS.get(verb, 15.0)


def _stamp(value: Any) -> float | None:
    """live.json's `updated` as a number of seconds; None when it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def manager_index(fleet: Any, name: str) -> int:
    return list(fleet.managers).index(name) + 1 if name in fleet.managers else 0


