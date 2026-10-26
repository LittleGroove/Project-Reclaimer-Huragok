"""self.ctx: what a part uses to see and act, growing with the named part it belongs to (lore.py).

    PartContext            every part: its settings, the database, events, its log, Huragok's folder
      SuperintendentContext   (superintendent.py) + the servers: their live picture, talking to them, restarting
        VergilContext         (vergil.py) + the players: who they are, their roles, commands.toml, the ledger

A part gets the context of its `persona`: the watchdog the Superintendent's, automod and the commands Vergil's, so
Vergil's parts see everything the Superintendent's do, and more.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .storage import PartStore

if TYPE_CHECKING:
    from ..huragok import Huragok


class PartContext:
    def __init__(self, huragok: Huragok, *, name: str, config: dict[str, Any], dry_run: bool, acts_as: str) -> None:
        self._huragok = huragok
        self.name = name
        self.config = config
        self.dry_run = dry_run
        self.acts_as = acts_as  # what players read: "muted by AutoMod"
        self.log = logging.getLogger(f"part.{name}")
        self.db = PartStore(huragok.storage, name)
        self.home: Path = huragok.settings.home  # Huragok's folder: settings.toml, commands.toml, automod.txt

    @property
    def everything_dry(self) -> bool:
        """settings.toml's own dry_run (above every section): no moderation action is sent anywhere, whatever a part
        or a server says (the ledger holds every one back)."""
        return bool(self._huragok.settings.dry_run)

    def part_option(self, part: str, key: str, default: Any = None) -> Any:
        """A setting of another part's section in settings.toml ([automod] across_servers, say), or `default`."""
        settings = self._huragok.settings.parts.get(part)
        return settings.options.get(key, default) if settings is not None else default

    def running(self, part: str) -> bool:
        """Whether a part is switched on and running now (automod keeps the mute list, say)."""
        runner = self._huragok.runners.get(part)
        return runner is not None and runner.enabled

    def emit(self, name: str, server: str | None = None, /, **data: Any) -> None:
        """An event for the other parts, always named '<part>.<name>'."""
        if not name.startswith(self.name + "."):
            name = f"{self.name}.{name}"
        self._huragok.bus.emit(name, server, **data)
