"""An action sent is an action recorded: a handler cancelled at its time limit (a slow server, a ban's save tried
again) left the ban standing with no ledger row, no `moderation.action` (so no Discord case) and a mute not kept across
restarts (audit 2026-11)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import BRAVO

from huragok.core.events import EventBus
from huragok.core.storage import Storage
from huragok.players.ledger import Moderation
from huragok.servers.state import Player, ServerState


class SlowServer:
    """A console that takes its time to answer."""

    async def command(self, *words: Any, by: str = "") -> Any:
        await asyncio.sleep(0.3)
        return SimpleNamespace(ok=True, text="Banned Bravo.")


def test_a_ban_sent_as_its_handler_runs_out_of_time_is_still_recorded(tmp_path: Path, run) -> None:
    storage = Storage(tmp_path / "huragok.sqlite3")
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("moderation.action", lambda event: seen.append(event["kind"]))
    state = ServerState("Alpha", connected=True)
    state.players[3] = Player(3, "Bravo", BRAVO)
    moderation = Moderation({"Alpha": SlowServer()}, {"Alpha": state}, storage, bus)  # type: ignore[dict-item]

    async def scenario() -> None:
        with pytest.raises(TimeoutError):  # the handler's time limit
            await asyncio.wait_for(moderation.act("ban", "Alpha", state.players[3], by="Mod", source="commands",
                                                  reason="cheating"), 0.05)
        await asyncio.sleep(0.6)  # the console answers after all

    try:
        run(scenario())
        rows = storage.actions_for(BRAVO)
        assert [row["kind"] for row in rows] == ["ban"] and seen == ["ban"]
    finally:
        storage.close()
