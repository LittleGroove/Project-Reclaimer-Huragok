"""Workshop items installed the program's way, and the servers a Workshop update asks things of, written down."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from testkit.setup import setup_root

from huragok.core.events import EventBus
from huragok.core.requests import Result
from huragok.core.serverwarnings import Warnings, load_rules
from huragok.core.settings import load
from huragok.core.tomledit import set_table_key
from huragok.program.dedicated import tool_for
from huragok.steam import SteamItem
from huragok.updates.workshop import WorkshopUpdateService


ITEM = 3820687243


OLD, NEW = 1792148310, 1792300975  # 16 Oct and 18 Oct 2026: the real item's two versions


ALPHA = "Alpha Server"

# The program's own words (S1, S1b), as servers/logs.py hands them on: the text after "[workshop] <item>: "
DOWNLOADING = "downloading the version of 18 Oct 2026 (installed: 16 Oct 2026)"
DOWNLOADED = "a new version was downloaded"
HELD = ("installing the update failed (cannot move S:\\SERVERS\\Alpha\\content\\Mods/Workshop\\3820687243 aside: "
        "Access is denied. (os error 5)); trying again")
LOCKED = ("installing the update failed (Cannot lock Workshop installation (S:\\shared\\content\\workshop-install.lock): "
          "The file exists. (os error 80). If a previous installer crashed, remove this lock file after confirming it "
          "has stopped.); trying again")
UPDATED = "updated to the version of 18 Oct 2026 between games; 1 server(s) play it from their next game"


class FakeLifecycle:
    """Each server's phase, players and game as a test sets them; what the update asks of them, in order."""

    def __init__(self, phase: str = "running") -> None:
        self.default = phase
        self.phases: dict[str, str] = {}
        self.counts: dict[str, int] = {}  # players on, by server
        self.playing: set[str] = set()  # servers with a game being played
        self.flags: dict[str, dict[str, str]] = {}
        self.sent: list[tuple[str, str]] = []  # (server, what): restart, restart now, quit, start, say <text>
        self.refuse: set[str] = set()  # servers whose console refuses a restart

    def phase(self, server: str) -> str:
        return self.phases.get(server, self.default)

    def is_up(self, server: str) -> bool:
        return self.phase(server) in ("running", "countdown", "external")

    def is_external(self, server: str) -> bool:
        return self.phase(server) == "external"

    def is_stopped(self, server: str) -> bool:
        return self.phase(server) in ("idle", "faulted", "backoff")

    def empty(self, server: str) -> bool:
        return not self.counts.get(server)

    def in_game(self, server: str) -> bool:
        return server in self.playing

    def players(self, server: str) -> int:
        return self.counts.get(server, 0)

    def set_flag(self, server: str, who: str, text: str | None) -> None:
        if text:
            self.flags.setdefault(server, {})[who] = text
        else:
            self.flags.get(server, {}).pop(who, None)

    async def say(self, server: str, text: str) -> Result:
        self.sent.append((server, f"say {text}"))
        return Result(True, "sent")

    async def restart_worker(self, server: str, *, now: bool = False, reason: str = "", by: Any = None) -> Result:
        if server in self.refuse or not self.is_up(server):
            return Result(False, "its console isn't connected")
        self.sent.append((server, "restart now" if now else "restart"))
        return Result(True, "restarting its game")

    def quit(self, server: str, *, reason: str = "", by: Any = None) -> Result:
        if self.phase(server) != "running":
            return Result(False, "it wasn't started from here, so the Superintendent doesn't stop it")
        self.sent.append((server, "quit"))
        self.phases[server] = "idle"
        return Result(True, "stopping")

    def start(self, server: str) -> Result:
        self.sent.append((server, "start"))
        self.phases[server] = "running"
        return Result(True, "starting")

    def to(self, server: str) -> list[str]:
        return [what for name, what in self.sent if name == server]


class Clock:
    """Huragok's clock for the update, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def workshop_root(tmp_path: Path, steam: int = OLD) -> tuple[Path, Path]:
    """Alpha with item ITEM installed (by the fake `dedicated mods`) at version `steam`."""
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): steam}), encoding="utf-8")
    result = tool_for(load(settings_path)).mods(folder)
    assert result.ok and "ready (version of 16 Oct 2026)" in result.text, result.text
    return settings_path, folder


def service_for(settings_path: Path, lifecycle: FakeLifecycle, steam: dict[int, SteamItem] | None = None,
                clock: Clock | None = None) -> WorkshopUpdateService:
    settings = load(settings_path)
    found = steam or {}
    return WorkshopUpdateService(settings, EventBus(), lifecycle,  # type: ignore[arg-type]
                                 warnings=Warnings(load_rules(), settings.records),
                                 fetch=lambda items: {i: found[i] for i in items if i in found},
                                 **({"clock": clock} if clock is not None else {}))


def said(service: WorkshopUpdateService, server: str = ALPHA) -> dict[str, str]:
    """The server's warnings now: rule -> its line."""
    assert service.warnings is not None
    return {w.rule.id: w.line() for w in service.warnings.of(server)}
