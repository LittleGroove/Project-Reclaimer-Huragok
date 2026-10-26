"""The server program's own commands, run for a server folder (program/dedicated.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import free_port, wait_for
from testkit.setup import fake_tool, make_server_folder


def test_streaming_a_command_and_cancelling_it(tmp_path: Path, run) -> None:
    folder = tmp_path / "Slayer"
    make_server_folder(folder, "Slayer", free_port())
    tool = fake_tool()
    seen: list[str] = []

    async def scenario() -> None:
        result = await tool.stream(folder, "content", "guardian", on_line=seen.append, timeout=60)
        assert result.ok and any("Guardian" in line for line in seen) and "Guardian" in result.text
        seen.clear()
        cancel = asyncio.Event()
        task = asyncio.ensure_future(tool.stream(folder, "slow", "40", on_line=seen.append, cancel=cancel, timeout=60))
        await wait_for(lambda: len(seen) >= 2 or task.done(), 30)
        assert not task.done(), task.result()
        cancel.set()
        result = await asyncio.wait_for(task, 15)
        assert not result.ok and result.code == 130 and "(cancelled)" in result.text and len(seen) < 10
        result = await tool.stream(folder, "slow", "40", on_line=seen.append, timeout=1.2)
        assert result.code == 124 and "took longer than" in result.text

    run(scenario())


def test_a_playlist_the_program_cant_read_is_listed_with_why() -> None:
    from huragok.program.dedicated import parse_playlists

    infos = parse_playlists('playlists/slayer.playlist.json · "Slayer" · 4 games · vote 20 s · played by Alpha\n'
                            "playlists/broken.playlist.json · played by no server · cannot be read: line 3: x\n")
    assert [(i.file, i.games, i.played_by) for i in infos] == [("playlists/slayer.playlist.json", 4, "Alpha"),
                                                               ("playlists/broken.playlist.json", 0, "no server")]
    assert infos[1].problem == "cannot be read: line 3: x" and infos[0].problem == ""

