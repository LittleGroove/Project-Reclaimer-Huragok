"""Workshop items with a newer version on Steam, and the lifecycle a Workshop update hands its job to."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from testkit.setup import setup_root

from huragok.core.events import EventBus
from huragok.core.jobs import StoppedJob
from huragok.core.settings import load
from huragok.core.tomledit import set_table_key
from huragok.program.dedicated import tool_for
from huragok.steam import SteamItem
from huragok.updates.workshop import WorkshopUpdateService


ITEM = 3820687243


OLD, NEW = 1792148310, 1792300975  # 16 Oct and 18 Oct 2026: the real item's two versions


ALPHA = "Alpha Server"


class FakeLifecycle:
    def __init__(self, phase: str = "running") -> None:
        self.state = phase
        self.jobs: dict[str, list[StoppedJob]] = {}
        self.flags: dict[str, dict[str, str]] = {}
        self.restarts: list[tuple[str, str, str]] = []

    def add_job(self, server: str, job: StoppedJob) -> None:
        self.jobs[server] = [j for j in self.jobs.get(server, []) if j.key != job.key] + [job]

    def drop_job(self, server: str, key: str) -> bool:
        before = len(self.jobs.get(server, []))
        self.jobs[server] = [j for j in self.jobs.get(server, []) if j.key != key]
        return len(self.jobs[server]) != before

    def set_flag(self, server: str, who: str, text: str | None) -> None:
        if text:
            self.flags.setdefault(server, {})[who] = text
        else:
            self.flags.get(server, {}).pop(who, None)

    def phase(self, server: str) -> str:
        return self.state

    async def restart(self, server: str, *, when: str, reason: str, by: str | None = None, countdown=None) -> Any:
        self.restarts.append((server, when, reason))


def workshop_root(tmp_path: Path, steam: int = OLD) -> tuple[Path, Path]:
    """Alpha with item ITEM installed (by the fake `dedicated mods`) at version `steam`."""
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    (folder / "fake-steam.json").write_text(json.dumps({str(ITEM): steam}), encoding="utf-8")
    result = tool_for(load(settings_path)).mods(folder)
    assert result.ok and "ready (version of 16 Oct 2026)" in result.text, result.text
    return settings_path, folder


def service_for(settings_path: Path, lifecycle: FakeLifecycle, steam: dict[int, SteamItem]) -> WorkshopUpdateService:
    settings = load(settings_path)
    return WorkshopUpdateService(settings, EventBus(), lifecycle, tool=lambda: tool_for(settings),  # type: ignore[arg-type]
                                 fetch=lambda items: {i: steam[i] for i in items if i in steam})
