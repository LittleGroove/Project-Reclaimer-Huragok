"""The window without a console: a scripted keyboard, a terminal that keeps what it prints, a controller."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import Any

from huragok.core.requests import Result
from huragok.window.tui import Keyboard, Terminal


def row(name: str, index: int = 1, **over: Any) -> dict[str, Any]:
    base = {"index": index, "name": name, "slug": name.lower(), "port": 49176, "autostart": True, "phase": "running",
            "status": "RUNNING  up 2h 14m", "color": "green", "players": "3/16", "names": "Alice, Bob, Cy",
            "game": "playing Guardian Slayer", "detail": "daily restart 04:00 (in 3h)", "version": "0.9.18",
            "uptime": "2h 14m", "health": "healthy", "connected": True, "errors": [],
            "can": {"start": False, "stop": True, "restart": True, "cancel": False, "talk": True}}
    base.update(over)
    return base


class FakeKeyboard(Keyboard):
    def __init__(self, keys: list[str]) -> None:
        self.queue = asyncio.Queue()  # type: ignore[assignment]
        self.drained = 0
        for key in keys:
            self.queue.put_nowait(key)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def drain(self) -> None:
        self.drained += 1  # the script is every key the test presses, none pressed "meanwhile": keep them

    async def read(self, timeout: float | None = None) -> str | None:
        if timeout is not None and timeout < 1:  # a screen polling for Esc while a command runs: nobody presses it
            await asyncio.sleep(min(timeout, 0.05))
            return None
        await asyncio.sleep(0)
        if self.queue.empty():
            return None
        return self.queue.get_nowait()


class FakeTerminal(Terminal):
    def __init__(self) -> None:
        self.output: list[str] = []
        self.frames: list[list[tuple[str, str | None]]] = []

    def size(self) -> tuple[int, int]:
        return 100, 30

    def _write(self, text: str, frame: bool = False) -> None:
        self.output.append(text)

    def draw(self, lines: list[tuple[str, str | None]]) -> None:
        self.frames.append(lines)

    def hide_cursor(self) -> None:
        pass

    def show_cursor(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def reset(self) -> None:
        pass


class FakeController:
    supervising = True
    launcher = "start.cmd"

    def __init__(self, home: Path | None = None) -> None:
        # A folder of its own: the window writes logs/window.log there (never into a real install).
        if home is None:
            home = Path(tempfile.mkdtemp(prefix="huragok-window-"))
            weakref.finalize(self, shutil.rmtree, home, True)
        self.home = home
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.quit = False
        self._rows = [row("Alpha Server"), row("Beta Server", 2, phase="idle", status="stopped", color="gray",
                                               players="", names="", game="",
                                               can={"start": True, "stop": False, "restart": True, "cancel": False,
                                                    "talk": False})]

    def header(self) -> list[str]:
        return ["backend process"]

    def rows(self) -> list[dict[str, Any]]:
        return self._rows

    def events(self, count: int) -> list[tuple[int, str]]:
        return []

    def problems(self) -> list[str]:
        return []

    def finished(self) -> str | None:
        return None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        if verb == "rcon":
            return [(server, Result(True, "ok", "3 players\nAlice"))]
        return [(server, Result(True, "done"))]

    def request_quit(self) -> None:
        self.quit = True

    def watch(self, server: str, listener: Any) -> Any:
        return lambda: None
