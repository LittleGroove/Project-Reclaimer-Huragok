"""The window on a root to clean up and check."""

from __future__ import annotations

from typing import Any

from testkit.setup import StrictKeyboard
from testkit.window import FakeTerminal

from huragok.core.logs import RecentLog
from huragok.window.dashboard import Menu


def menu_on(controller: Any, keys: list[str]) -> tuple[Menu, FakeTerminal]:
    terminal = FakeTerminal()
    return Menu(controller, terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]
