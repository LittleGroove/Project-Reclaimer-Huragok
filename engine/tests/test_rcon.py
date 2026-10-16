from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import wait_for, write_server_folder, write_settings
from fake_rcon import FakeRcon

from reclaimer_supervisor.config import load
from reclaimer_supervisor.events import Event, EventBus
from reclaimer_supervisor.rcon import RconClient, RconUnavailable, one_shot


def _client(tmp_path: Path, port: int, host_key: str | None = "k" * 64) -> tuple[RconClient, list[Event]]:
    write_server_folder(tmp_path / "server", port, host_key=host_key)
    settings = load(write_settings(tmp_path, tmp_path / "server"))
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("*", seen.append)
    return RconClient(settings.servers[0], bus, command_timeout=2), seen


def test_signs_in_on_the_host_spot_runs_commands_and_relays_events(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            client, seen = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            assert fake.auths[0]["host_key"] == "k" * 64
            connected = next(e for e in seen if e.name == "rcon.connected")
            assert connected.data == {"version": "0.9.18", "protocol": 1, "host_slot": True}

            reply = await client.command("kick", "#3", "camping all game", by="Alice")
            assert reply.ok and reply.text == "Done: kick #3 camping all game"
            assert fake.commands[-1] == {"type": "command", "id": 1, "command": "kick",
                                         "args": ["#3", "camping all game"], "by": "Alice"}

            await fake.chat("c" * 64, "Alice", "gg")
            chat = await wait_for(lambda: next((e for e in seen if e.name == "rcon.chat"), None))
            assert chat.server == "LG Test Server" and chat["text"] == "gg" and chat["channel"] == "all"
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


def test_wrong_password_is_tried_once_and_never_again(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon(password="something else entirely") as fake:
            client, seen = _client(tmp_path, fake.port)
            await asyncio.wait_for(client.run(), 5)  # returns: gave up for good
            assert len(fake.auths) == 1
            assert [e.name for e in seen] == ["rcon.auth_failed"]
            assert client.stopped_reason and "password" in client.stopped_reason

    run(scenario())


def test_full_console_is_reported_and_retried_later(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon(full=True) as fake:
            client, seen = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: any(e.name == "rcon.full" for e in seen))
            await asyncio.sleep(0.3)
            assert not task.done()  # still trying, unlike a wrong password
            client.stop()
            task.cancel()

    run(scenario())


def test_a_drop_fails_pending_commands_without_resending_and_reconnects(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            fake.replies["mute"] = lambda m: None  # never answers
            client, seen = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            pending = asyncio.ensure_future(client.command("mute", "#2"))
            await wait_for(lambda: fake.named("mute"))
            await fake.drop()
            with pytest.raises(RconUnavailable):
                await pending
            await wait_for(lambda: sum(e.name == "rcon.connected" for e in seen) == 2, timeout=6)
            assert len(fake.named("mute")) == 1  # not sent again after the reconnect
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


def test_new_host_key_during_sign_in_means_sign_in_again(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        key_file = tmp_path / "server" / "data" / "lg-test-server" / "rcon-host.key"
        async with FakeRcon() as fake:
            client, _ = _client(tmp_path, fake.port)
            fake.rotate_key = key_file
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected, timeout=6)
            assert [a["host_key"] for a in fake.auths] == ["k" * 64, "f" * 64]
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


def test_not_connected_raises(tmp_path: Path, run) -> None:
    client, _ = _client(tmp_path, 1)
    with pytest.raises(RconUnavailable):
        run(client.command("status"))


def test_one_shot_uses_a_normal_spot(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            client, _ = _client(tmp_path, fake.port)
            reply = await one_shot(client.server, "players")
            assert reply.ok and "host_key" not in fake.auths[0]

    run(scenario())


def test_moves_to_the_host_spot_when_the_key_appears(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon() as fake:
            client, seen = _client(tmp_path, fake.port, host_key=None)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            assert "host_key" not in fake.auths[0] and not client.host_slot
            client.server.host_key_path.write_text("k" * 64, encoding="utf-8")  # the server wrote it
            await wait_for(lambda: client.host_slot, timeout=8)
            assert fake.auths[-1]["host_key"] == "k" * 64
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())
