from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import ALICE, BRAVO, free_port, wait_for, write_server_folder, write_settings
from fake_rcon import PASSWORD, FakeRcon
from websockets.asyncio.server import serve

from huragok import rcon
from huragok.config import load
from huragok.events import Event, EventBus
from huragok.rcon import RconClient, RconUnavailable, Reply, is_lockout, one_shot, url_for
from huragok.state import Player, StateTracker


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


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """rcon's monotonic clock, moved by hand; its Unix time stays real."""
    clock = [1000.0]
    monkeypatch.setattr(rcon, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    return clock


def _refusals(seen: list[Event]) -> int:
    return sum(e.name == "rcon.auth_failed" for e in seen)


def test_a_refused_password_is_tried_again_only_when_that_cant_lock_the_machine_out(
        tmp_path: Path, run, monkeypatch, caplog) -> None:
    clock = _clock(monkeypatch)
    monkeypatch.setattr(rcon, "REFUSED_POLL", 0.02)

    async def scenario() -> None:
        async with FakeRcon(password="something else entirely") as fake:
            client, seen = _client(tmp_path, fake.port)
            looks: list[int] = []
            endpoint = client.server.rcon_endpoint
            client.server.rcon_endpoint = lambda: (looks.append(1), endpoint())[1]  # type: ignore[method-assign]
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: _refusals(seen) == 1)
            assert client.stopped_reason == "password refused: Wrong password." and not task.done()
            looked = len(looks)
            await wait_for(lambda: len(looks) >= looked + 3)  # it keeps looking at the files, not at the console
            assert len(fake.auths) == 1
            client.wake()  # the server started again: one more try
            await wait_for(lambda: _refusals(seen) == 2)
            client.wake()  # woken again for the same start: not a third try
            looked = len(looks)
            await wait_for(lambda: len(looks) >= looked + 3)
            assert len(fake.auths) == 2
            clock[0] += rcon.REFUSED_RETRY  # ten minutes on: any lockout has ended
            await wait_for(lambda: _refusals(seen) == 3)
            fake.password = PASSWORD  # the console's password set right at the server
            client.wake()
            await wait_for(lambda: client.connected)
            assert client.stopped_reason is None and len(fake.auths) == 4
            client.stop()
            await asyncio.wait_for(task, 3)

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[LG Test Server] RCON refused the password (Wrong password.). Huragok won't keep trying it" in text
    assert "If the password is right, another tool on this machine may have locked this address out" in text
    assert "the server started again; trying its console once more" in text
    assert "trying the console again, 10 minutes after it was refused" in text
    assert "RCON took the password again; the console is back" in text


def test_a_new_password_in_the_files_is_tried_at_once(tmp_path: Path, run, monkeypatch) -> None:
    monkeypatch.setattr(rcon, "REFUSED_POLL", 0.02)

    async def scenario() -> None:
        async with FakeRcon(password="the password the server really has") as fake:
            client, seen = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: _refusals(seen) == 1)
            # The restarted server wrote its instance.json, with the new password the host set.
            (client.server.data_dir / "instance.json").write_text(
                json.dumps({"rcon": {"password": fake.password}}), encoding="utf-8")
            await wait_for(lambda: client.connected)
            assert [a["password"] for a in fake.auths] == [PASSWORD, fake.password]
            assert client.stopped_reason is None
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


def test_a_lockout_is_told_apart_from_a_wrong_password(tmp_path: Path, run, monkeypatch, caplog) -> None:
    # 0.9.18 says "wrong password" either way; its log says when it locked an address out.
    assert is_lockout("Too many wrong passwords from your address; try again in 9 minutes.")
    assert not is_lockout("wrong password") and not is_lockout("Wrong password.")
    monkeypatch.setattr(rcon, "REFUSED_POLL", 0.02)
    lockout = {"address": "127.0.0.1:60204", "times": 5, "refused": "127.0.0.1", "minutes": 10}

    async def scenario() -> None:
        async with FakeRcon(password="something else entirely") as fake:
            client, seen = _client(tmp_path, fake.port)
            client.bus.emit("log.rcon_wrong_password", "LG Test Server", **lockout)  # another tool, just now
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: _refusals(seen) == 1)
            assert client.stopped_reason and "locked out" in client.stopped_reason
            assert next(e for e in seen if e.name == "rcon.auth_failed")["lockout"] is True
            client.stop()
            await asyncio.wait_for(task, 3)
        async with FakeRcon(password="something else entirely") as fake:
            client, seen = _client(tmp_path / "later", fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: _refusals(seen) == 1)
            assert client.stopped_reason == "password refused: Wrong password."
            client.bus.emit("log.rcon_wrong_password", "LG Test Server", **lockout)  # the log line comes after
            assert client.stopped_reason == "this machine is locked out of the console for 10 minutes"
            client.stop()
            await asyncio.wait_for(task, 3)

    with caplog.at_level(logging.INFO):
        run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert ("RCON turned Huragok away (Wrong password.): this machine's address is locked out after five wrong "
            "passwords from tools on it, right password or not. Huragok tries again in 10 minutes") in text
    assert ("the server locked 127.0.0.1 out of its console for 10 minutes after 5 wrong passwords (its log says "
            "so), so it refuses Huragok too") in text


def test_full_console_is_reported_and_retried_later(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeRcon(full=True) as fake:
            client, seen = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: any(e.name == "rcon.full" for e in seen))
            client.wake()  # skip the 30 s wait: it does try again, unlike a refused password
            await wait_for(lambda: len(fake.auths) == 2)
            assert not task.done()
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


def test_a_session_over_a_minute_starts_the_backoff_again(tmp_path: Path, run, monkeypatch) -> None:
    clock = _clock(monkeypatch)
    monkeypatch.setattr(rcon, "BACKOFF", (0.01, 30))

    async def scenario() -> None:
        async with FakeRcon() as fake:
            client, seen = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            connects = lambda: sum(e.name == "rcon.connected" for e in seen)  # noqa: E731
            await wait_for(lambda: client.connected)
            await fake.drop()  # a short session: the next drop would wait the second step (30 s)…
            await wait_for(lambda: connects() == 2)
            clock[0] += 61  # …but this one lasts over a minute
            await fake.drop()
            await wait_for(lambda: connects() == 3, timeout=3)
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


def test_an_unexpected_error_doesnt_end_the_console(tmp_path: Path, run, monkeypatch, caplog) -> None:
    monkeypatch.setattr(rcon, "BACKOFF", (0.01,))

    async def scenario() -> None:
        async with FakeRcon() as fake:
            client, seen = _client(tmp_path, fake.port)
            real = client._session
            tries: list[int] = []

            async def odd() -> None:
                tries.append(1)
                if len(tries) <= 2:
                    raise asyncio.IncompleteReadError(b"", 4)
                await real()

            client._session = odd  # type: ignore[method-assign]
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            await fake.event("chat", channel="all", name="Alice", id=ALICE, text="gg")
            await fake.event("chat", channel="all", name="Alice", id=ALICE, text="gg", time="soon")
            for ws in list(fake.connections):
                await ws.send(json.dumps([1, 2]))  # JSON, but not a message
            await fake.event("chat", channel="all", name="Alice", id=ALICE, text="still here")
            await wait_for(lambda: any(e.get("text") == "still here" for e in seen))
            assert client.connected and sum(e.name == "rcon.connected" for e in seen) == 1
            client.stop()
            await asyncio.wait_for(task, 3)

    with caplog.at_level(logging.DEBUG):
        run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text.count("RCON connection failed unexpectedly (IncompleteReadError") == 1  # once per kind of error
    assert "RCON sent JSON that isn't a message: '[1, 2]'" in text


def test_an_ipv6_console_address_connects(tmp_path: Path, run) -> None:
    assert url_for("::1", 49176) == "ws://[::1]:49176/" and url_for("[::1]", 5) == "ws://[::1]:5/"
    assert url_for("127.0.0.1", 49176) == "ws://127.0.0.1:49176/"

    class OnIpv6(FakeRcon):
        async def __aenter__(self) -> FakeRcon:
            self._server = await serve(self._handle, "::1", 0, subprotocols=["reclaimer-rcon"])
            self.port = next(iter(self._server.sockets)).getsockname()[1]
            return self

    async def scenario() -> None:
        try:
            fake = await OnIpv6().__aenter__()
        except OSError:
            pytest.skip("no IPv6 loopback on this machine")
        try:
            client, _ = _client(tmp_path, fake.port)
            (client.server.data_dir / "instance.json").write_text(
                json.dumps({"rcon": {"address": "::1", "port": fake.port, "password": PASSWORD}}), encoding="utf-8")
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            assert (await one_shot(client.server, "players")).ok
            client.stop()
            await asyncio.wait_for(task, 3)
        finally:
            await fake.__aexit__(None, None, None)

    run(scenario())


def test_the_console_never_goes_through_a_proxy(tmp_path: Path, run, monkeypatch) -> None:
    nowhere = f"http://127.0.0.1:{free_port()}"  # websockets 15+ would send ws:// through this
    for name in ("WS_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, nowhere)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    async def scenario() -> None:
        async with FakeRcon() as fake:
            client, _ = _client(tmp_path, fake.port)
            task = asyncio.ensure_future(client.run())
            await wait_for(lambda: client.connected)
            assert (await one_shot(client.server, "status")).ok
            client.stop()
            await asyncio.wait_for(task, 3)

    run(scenario())


# The live picture the console keeps (state.py) ----------------------------------------------------------------

class Held:
    """A console whose replies wait until the test lets them go, as a slow round trip."""

    connected = True

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.asked: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def command(self, command: str, *args: str, **options: Any) -> Reply:
        self.asked.append(command)
        if command in self.gates:
            await self.gates[command].wait()
        return Reply(True, "", self.replies.get(command), {})


def test_events_during_a_refresh_win_over_its_older_replies(run) -> None:
    # The replies were made before these events happened: the 30 s refresh mustn't undo them.
    bus = EventBus()
    client = Held({"status": {"phase": "playing", "map": "Guardian", "mode": "Slayer", "max_players": 16},
                   "players": {"players": [{"number": 3, "name": "Alice", "id": ALICE}], "max_players": 16},
                   "admin": {"configured": [], "added": []}})
    tracker = StateTracker(bus, {"Alpha": client})  # type: ignore[dict-item]
    state = tracker.states["Alpha"]

    async def scenario() -> None:
        client.gates = {"status": asyncio.Event(), "players": asyncio.Event()}
        refresh = asyncio.ensure_future(tracker.refresh("Alpha"))
        await wait_for(lambda: "status" in client.asked)
        bus.publish(Event("rcon.phase", "Alpha", {"phase": "finished", "map": "Guardian", "mode": "Slayer"},
                          1_800_000_000.0))
        client.gates["status"].set()
        await wait_for(lambda: "players" in client.asked)
        assert state.max_players == 16  # the status reply's other facts are applied as it comes
        bus.publish(Event("rcon.join", "Alpha", {"number": 4, "name": "Bravo", "id": BRAVO}))
        bus.publish(Event("rcon.leave", "Alpha", {"number": 3, "name": "Alice", "id": ALICE}))
        client.gates["players"].set()
        await refresh

    run(scenario())
    assert state.phase == "finished" and state.phase_since == 1_800_000_000.0
    assert [(p.number, p.name) for p in state.players.values()] == [(4, "Bravo")]
    # With nothing happening meanwhile, the replies are the picture.
    client.gates = {}
    run(tracker.refresh("Alpha"))
    assert state.phase == "playing" and [p.number for p in state.players.values()] == [3]


def test_a_lagged_console_is_read_again_at_once(run) -> None:
    bus = EventBus()
    client = Held({"status": {"phase": "playing"}, "players": {"players": []}, "admin": {}})
    tracker = StateTracker(bus, {"Alpha": client}, refresh_seconds=3600)  # type: ignore[dict-item]

    async def scenario() -> None:
        task = asyncio.ensure_future(tracker.run())
        await asyncio.sleep(0.05)
        assert client.asked == []  # nothing due for an hour
        bus.emit("rcon.lagged", "Alpha", missed=12)
        await wait_for(lambda: "status" in client.asked)
        task.cancel()

    run(scenario())


def test_admins_added_and_removed_on_the_console(run) -> None:
    bus = EventBus()
    tracker = StateTracker(bus, {"Alpha": SimpleNamespace(connected=False)})  # type: ignore[dict-item]
    state = tracker.states["Alpha"]
    state.players[3] = Player(3, "Alice", ALICE)
    state.admins = set()
    bus.emit("rcon.admin_added", "Alpha", id=ALICE.upper())
    assert state.players[3].admin and state.admins == {ALICE}
    bus.emit("rcon.admin_removed", "Alpha", id=ALICE)
    assert not state.players[3].admin and state.admins == set()
    bus.emit("rcon.admin_added", "Alpha", id=BRAVO)  # not on: only the list changes
    assert state.admins == {BRAVO}


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
