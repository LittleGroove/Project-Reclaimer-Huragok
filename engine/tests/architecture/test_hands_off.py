"""Huragok keeps its hands off a server's folder (0.7.0, plans/server-files-audit/plan-0.7.0.md 1 and 7.7).

A server's folder is the host's: dedicated.toml, playlists/, content/, game/. Huragok writes into it only for
moderation (through the console), for the stats relay while it's on (the stats_server line, put back as it stops),
for what the host asks (a quick action, a file tool), and its own per-server intervalmessages.txt. This runs Huragok
on a server folder made the way `dedicated init` makes one, through its start, the folder rescans, the warnings and
its stop, and finds every file just as it was (simulation S5's check, as a test).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from conftest import staff, wait_for, write_server_folder, write_settings
from testkit.fake_rcon import FakeRcon

from huragok.core.settings import load
from huragok.huragok import Huragok

ALLOWED = {"intervalmessages.txt"}  # Huragok's own, in the server's folder: messages for that server only


def _snapshot(folder: Path) -> dict[str, str]:
    """Every file under the folder, by its path from there, with a hash of its bytes; folders as "<folder>"."""
    found: dict[str, str] = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if path.is_dir():
            found[relative] = "<folder>"
        else:
            found[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changes = [f"new: {p}" for p in after if p not in before and p.split("/")[0] not in ALLOWED]
    changes += [f"gone: {p}" for p in before if p not in after]
    changes += [f"changed: {p}" for p in before if p in after and before[p] != after[p] and p not in ALLOWED]
    return sorted(changes)


def test_huragok_changes_nothing_in_a_servers_folder(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            root = tmp_path / "reclaimer"
            folder = root / "SERVERS" / "Dropped In"
            write_server_folder(folder, fake.port, name="Dropped In")
            (folder / "playlists").mkdir()
            (folder / "playlists" / "mine.playlist.json").write_text(
                '{"version": 2, "games": [{"map": "Guardian", "game": "Slayer"}]}\n', encoding="utf-8")
            text = (folder / "dedicated.toml").read_text(encoding="utf-8")
            (folder / "dedicated.toml").write_text('stats_server = "http://127.0.0.1:9"\n' + text, encoding="utf-8")
            before = _snapshot(folder)
            supervisor = Huragok(load(write_settings(root, None, commands=staff())))
            task = asyncio.ensure_future(supervisor.run())
            try:
                sup = supervisor.superintendent
                await wait_for(lambda: "Dropped In" in sup.states and sup.states["Dropped In"].connected)
                for _ in range(3):  # the rescans, the warnings, the files looked at
                    sup.discover()
                    sup.crash_loops()
                    await asyncio.sleep(0.5)
            finally:
                supervisor.stop()
                await asyncio.wait_for(task, 15)
            assert _changes(before, _snapshot(folder)) == []

    run(scenario())


def test_the_check_itself_sees_a_change(tmp_path: Path) -> None:
    """The comparison above would notice a file written, added or taken away."""
    folder = tmp_path / "server"
    folder.mkdir()
    (folder / "dedicated.toml").write_text("a = 1\n", encoding="utf-8")
    before = _snapshot(folder)
    (folder / "dedicated.toml").write_text("a = 2\n", encoding="utf-8")
    (folder / "bans.json").write_text("[]", encoding="utf-8")
    (folder / "intervalmessages.txt").write_text("hello\n", encoding="utf-8")
    assert _changes(before, _snapshot(folder)) == ["changed: dedicated.toml", "new: bans.json"]
