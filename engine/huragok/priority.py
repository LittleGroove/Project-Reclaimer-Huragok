"""How much of the processor each server gets: the priority its processes run at ([process] priority).

The game runs a few processes deep (`dedicated run`, the `dedicated run` it starts, `local-server`, and
`probe-engine-worker`, the game itself), and the program starts new ones as it goes. Windows doesn't hand a
priority above normal on to the processes a process starts, so setting the first one isn't enough: the keeper sets
every process in the server's tree, and looks again every few seconds for new ones.

  high       ahead of everything that runs at normal priority (the default): browsers, Steam, updates, Huragok
  realtime   ahead of Windows' own work too (disk, network, input). Windows only gives it to a program started
             as administrator, and silently gives High otherwise: what each process got is read back, and said
  off        left as it starts (for a host who sets it some other way)

On Linux these are nice values (NICE); above normal needs root or CAP_SYS_NICE (Docker: --cap-add SYS_NICE).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

log = logging.getLogger(__name__)

PRIORITIES = ("realtime", "high", "above_normal", "normal", "below_normal", "off")
DEFAULT = "high"
LABELS = {"realtime": "Realtime", "high": "High", "above_normal": "Above normal", "normal": "Normal",
          "below_normal": "Below normal", "idle": "Idle"}
CLASSES = {"realtime": "REALTIME_PRIORITY_CLASS", "high": "HIGH_PRIORITY_CLASS",  # psutil's names, Windows only
           "above_normal": "ABOVE_NORMAL_PRIORITY_CLASS", "normal": "NORMAL_PRIORITY_CLASS",
           "below_normal": "BELOW_NORMAL_PRIORITY_CLASS", "idle": "IDLE_PRIORITY_CLASS"}
NICE = {"realtime": -20, "high": -10, "above_normal": -5, "normal": 0, "below_normal": 5}
RANK = {"idle": 0, "below_normal": 1, "normal": 2, "above_normal": 3, "high": 4, "realtime": 5}
EVERY = 5.0  # seconds between looks at a server's processes: a new game worker runs at normal until then


def value_for(level: str) -> int:
    """What psutil's nice() takes for a level: a priority class on Windows, a nice value elsewhere."""
    if os.name == "nt":
        return int(getattr(psutil, CLASSES[level]))
    return NICE[level]


def level_of(value: int) -> str:
    """The level a process runs at, from psutil's nice(): on Linux the nearest at or below it."""
    if os.name == "nt":
        return next((level for level, name in CLASSES.items() if int(getattr(psutil, name, -1)) == value), "normal")
    return next((level for level, nice in sorted(NICE.items(), key=lambda item: item[1]) if value <= nice), "idle")


@dataclass
class Outcome:
    """One look at a server's processes."""

    processes: int = 0  # how many there are
    changed: int = 0  # how many were set
    got: str = ""  # the lowest level any of them runs at now ("" when none could be read)
    denied: str = ""  # why one couldn't be set, when the system refused


def apply(pid: int, level: str) -> Outcome:
    """Set pid and every process under it to level, where it isn't already, and read back what each runs at."""
    outcome = Outcome()
    try:
        root = psutil.Process(pid)
        tree = [root, *root.children(recursive=True)]
    except psutil.Error:
        return outcome
    wanted = value_for(level)
    got: list[str] = []
    for process in tree:
        try:
            now = before = process.nice()
            if before != wanted:
                process.nice(wanted)
                now = process.nice()  # Windows gives High to a Realtime it won't allow, and says nothing
                outcome.changed += now != before
            got.append(level_of(now))
        except psutil.AccessDenied as error:
            outcome.denied = outcome.denied or (error.msg or "access denied")
        except psutil.Error:
            continue  # gone meanwhile
        outcome.processes += 1
    if got:
        outcome.got = min(got, key=lambda name: RANK.get(name, 2))
    return outcome


def short_of(level: str, outcome: Outcome) -> bool:
    """The processes run below the level asked for (Windows gives High to Realtime without administrator)."""
    return bool(outcome.got) and RANK.get(outcome.got, 2) < RANK[level]


def why_short(level: str, got: str) -> str:
    if os.name == "nt" and level == "realtime":
        return "Windows only gives Realtime to a program started as administrator"
    if os.name != "nt" and RANK[level] > RANK["normal"]:
        return "above normal needs root or CAP_SYS_NICE (in Docker, --cap-add SYS_NICE)"
    return f"the system kept it at {LABELS.get(got, got)}"


def unavailable(level: str) -> str:
    """Why this Huragok can't give a server that level ("" when it can): for `check`, and the window's settings."""
    if level not in RANK:
        return ""
    if os.name == "nt":
        if level != "realtime":
            return ""
        try:
            import ctypes

            admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return ""
        return "" if admin else ("Huragok isn't running as administrator, so Windows gives the servers High instead "
                                 "(start it as administrator for Realtime)")
    if RANK[level] <= RANK["normal"] or os.geteuid() == 0 or _has_sys_nice():
        return ""
    return ("above normal needs root or CAP_SYS_NICE (in Docker, --cap-add SYS_NICE), which Huragok doesn't have "
            "here, so the servers stay at normal")


def _has_sys_nice() -> bool:
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("CapEff:"):
                return bool(int(line.split()[1], 16) >> 23 & 1)  # CAP_SYS_NICE is capability 23
    except (OSError, ValueError, IndexError):
        pass
    return False


class PriorityKeeper:
    """Keeps each server's processes at its priority. keep() is called every tick; at most every EVERY seconds per
    server, the look runs in a thread (walking a process tree takes Windows about 10 ms, which the event loop, and
    every server's RCON with it, would otherwise wait for). What's logged is said once per run of the server."""

    def __init__(self) -> None:
        self._due: dict[str, float] = {}
        self._busy: set[str] = set()
        self._told: dict[str, tuple[int, str, str]] = {}  # server -> (its pid, the level, what was said)
        self.now: dict[str, tuple[str, str]] = {}  # server -> (the level asked for, the lowest level it runs at)

    def keep(self, name: str, pid: int | None, level: str) -> None:
        if not pid or level == "off" or level not in RANK:
            self.now.pop(name, None)
            return
        clock = time.monotonic()
        if name in self._busy or clock < self._due.get(name, float("-inf")):
            return
        self._due[name] = clock + EVERY
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, apply, pid, level)
        except RuntimeError:  # no event loop (a command), or it's closing: look now
            self._report(name, pid, level, apply(pid, level))
            return
        self._busy.add(name)

        def done(finished: asyncio.Future[Outcome]) -> None:
            self._busy.discard(name)
            if finished.cancelled():
                return
            if error := finished.exception():
                log.debug("[%s] looking at its priority failed: %s", name, error)
                return
            self._report(name, pid, level, finished.result())

        future.add_done_callback(done)

    def forget(self, name: str) -> None:
        """The server stopped: its next run is looked at as soon as it starts, and said again."""
        self._due.pop(name, None)
        self._told.pop(name, None)
        self.now.pop(name, None)

    def text(self, name: str) -> str:
        """For the window: "High priority", or what it runs at when that's less than asked."""
        if name not in self.now:
            return ""
        level, got = self.now[name]
        if not got:
            return f"priority not set ({LABELS[level]} asked)"
        if got == level:
            return f"{LABELS[level]} priority"
        return f"{LABELS.get(got, got)} priority ({LABELS[level]} asked)"

    def _report(self, name: str, pid: int, level: str, outcome: Outcome) -> None:
        if not outcome.processes:
            return
        self.now[name] = (level, outcome.got)
        if outcome.denied and not outcome.got:
            said = f"couldn't set its priority to {LABELS[level]}: {outcome.denied}"
            if os.name != "nt" and RANK[level] > RANK["normal"]:
                said += f"; {why_short(level, 'normal')}"
        elif short_of(level, outcome):
            said = (f"runs at {LABELS.get(outcome.got, outcome.got)} priority, not {LABELS[level]}: "
                    f"{why_short(level, outcome.got)}")
        elif outcome.changed:
            said = f"its processes run at {LABELS[level]} priority"
        else:
            return  # already as asked (picked up after Huragok's own restart): nothing to say
        if self._told.get(name) == (pid, level, said):
            if outcome.changed:
                log.debug("[%s] %s new process(es) set to %s priority", name, outcome.changed, LABELS[level])
            return
        self._told[name] = (pid, level, said)
        if said.startswith("its processes"):
            log.info("[%s] %s", name, said)
        else:
            log.warning("[%s] %s", name, said)
