"""Importing MCC's variants, Source RCON through the relay, and the next game on the dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from conftest import free_port, free_ports, staff, wait_for, write_root
from test_manage import StubClient
from test_setup import TEMPLATE_COMMAND, make_server_folder, setup_root

from huragok import cli, desktop
from huragok.config import load
from huragok.contentimport import content_root_of, find_mcc, import_content, importable
from huragok.dedicated import Dedicated, parse_content
from huragok.events import EventBus
from huragok.relay import SOURCE_AUTH, SOURCE_AUTH_RESPONSE, SOURCE_EXEC, SOURCE_RESPONSE, Relay
from huragok.state import ServerState, StateTracker, describe_game

FAKE = Path(__file__).resolve().parent / "fake_dedicated.py"


def fake_mcc(folder: Path) -> Path:
    halo3 = folder / "halo3"
    for sub, files in (("hopper_map_variants", ["mlg_guardian_8.mvar", "team_hardcore_pit.mvar", "infection_zanzibar.mvar"]),
                       ("hopper_game_variants", ["mlg_ts_v8.bin", "team_hardcore_010.bin", "infection_alpha.bin"]),
                       ("map_variants", ["h3_hardcoreGuardian.mvar"]), ("game_variants", ["slayer-0_010.bin"])):
        (halo3 / sub).mkdir(parents=True, exist_ok=True)
        for name in files:
            (halo3 / sub / name).write_bytes(b"variant " + name.encode())
    (halo3 / "halo3.dll").write_bytes(b"dll")
    return folder


# Importing variants -----------------------------------------------------------------------------

def test_importing_mccs_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from huragok import contentimport

    mcc = fake_mcc(tmp_path / "MCC")
    assert find_mcc(str(mcc)) == mcc
    assert find_mcc(str(tmp_path / "nowhere")) is None  # the hint is wrong, and Steam has no MCC (conftest)
    library = tmp_path / "Library"
    fake_mcc(library / "steamapps" / "common" / contentimport.MCC_FOLDER)
    monkeypatch.setattr(contentimport, "_steam_roots", lambda: [library])
    assert find_mcc(str(tmp_path / "nowhere")) == library / "steamapps" / "common" / contentimport.MCC_FOLDER  # Steam's
    items = importable(mcc)
    assert sorted(i.name for i in items if i.target == "Maps") == ["h3_hardcoreGuardian", "infection_zanzibar",
                                                                     "mlg_guardian_8", "team_hardcore_pit"]
    assert sorted(i.name for i in items if i.target == "Game Modes") == ["infection_alpha", "mlg_ts_v8",
                                                                           "slayer-0_010", "team_hardcore_010"]
    assert [i.name for i in importable(mcc, ["hardcore"])] == ["team_hardcore_pit", "h3_hardcoreGuardian",
                                                               "team_hardcore_010"]
    content = tmp_path / "content"
    copied, skipped = import_content(importable(mcc, ["mlg"]), content)
    assert [p.relative_to(content).as_posix() for p in copied] == ["Maps/From MCC/mlg_guardian_8.mvar",
                                                                    "Game Modes/From MCC/mlg_ts_v8.bin"]
    assert import_content(importable(mcc, ["mlg"]), content) == ([], 2)  # already there
    assert len(import_content(importable(mcc, ["mlg"]), content, overwrite=True)[0]) == 2
    # Any folder, or one file, with saved .json maps and game types sorted by what they hold.
    other = tmp_path / "downloads"
    other.mkdir()
    (other / "snipers.json").write_text(json.dumps({"version": 1, "name": "Snipers", "mode": "Slayer"}), encoding="utf-8")
    (other / "fort.json").write_text(json.dumps({"map": "Sandbox", "placements": []}), encoding="utf-8")
    (other / "notes.json").write_text("[1, 2]", encoding="utf-8")
    (other / "a.mvar").write_bytes(b"x")
    assert {(i.name, i.target) for i in importable(other)} == {("snipers", "Game Modes"), ("fort", "Maps"), ("a", "Maps")}
    assert [(i.name, i.target) for i in importable(other / "snipers.json")] == [("snipers", "Game Modes")]


def test_import_through_the_command_and_the_fake_lists_them(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings_path = setup_root(tmp_path)
    mcc = fake_mcc(tmp_path / "MCC")
    assert cli.main(["-c", str(settings_path), "content", "-s", "Alpha Server", "--import", "--from", str(mcc), "mlg"]) == 0
    out = capsys.readouterr().out
    assert "Copied 2 file(s)" in out and "Maps/From MCC/mlg_guardian_8.mvar" in out
    root = content_root_of(settings_path.parent / "SERVERS" / "Alpha Server")
    assert (root / "Maps" / "From MCC" / "mlg_guardian_8.mvar").is_file()
    assert cli.main(["-c", str(settings_path), "content", "-s", "Alpha Server", "mlg_guardian"]) == 0
    assert "mlg_guardian_8" in capsys.readouterr().out  # the (fake) program lists it as a map variant
    assert cli.main(["-c", str(settings_path), "content", "-s", "Alpha Server", "--import", "--from",
                     str(tmp_path / "missing")]) == 2
    tool = Dedicated(FAKE, template=TEMPLATE_COMMAND)
    made = tool.playlist_new(settings_path.parent / "SERVERS" / "Alpha Server", "MLG", ["mlg_guardian_8", "Guardian"],
                             ["Slayer", "team_slayer"])
    assert made.ok, made.text  # an imported variant works in a playlist


# Source RCON through the relay ----------------------------------------------------------------

def source_packet(packet_id: int, kind: int, body: str = "") -> bytes:
    payload = struct.pack("<ii", packet_id, kind) + body.encode() + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    (size,) = struct.unpack("<i", await reader.readexactly(4))
    payload = await reader.readexactly(size)
    packet_id, kind = struct.unpack("<ii", payload[:8])
    return packet_id, kind, payload[8:-2].decode()


def test_source_rcon_through_the_relay(tmp_path: Path, run) -> None:
    root = tmp_path / "reclaimer"
    port, ws_port, source_port = free_ports(3)
    ws_offset, source_offset = ws_port - port, source_port - port  # the relays listen at game port + offset
    body = (f'backend = "attach"\n[relay]\nenabled = true\nport_offset = {ws_offset}\nsource_port_offset = {source_offset}\n'
            '[[relay.user]]\nname = "Alice"\npassword = "alicealice"\nrole = "moderator"\n')
    settings = load(write_root(root, [("Alpha", port)], body, staff()))
    assert settings.relay.source_port_offset == source_offset
    client = StubClient()
    relay = Relay(settings, {"Alpha": client}, {"Alpha": ServerState("Alpha")}, EventBus())  # type: ignore[arg-type]

    async def scenario() -> None:
        task = asyncio.ensure_future(relay.run())
        await wait_for(lambda: "Alpha" in relay.source_listeners)
        reader, writer = await asyncio.open_connection("127.0.0.1", port + source_offset)
        writer.write(source_packet(7, SOURCE_AUTH, "nope"))
        await writer.drain()
        assert (await read_packet(reader))[1] == SOURCE_RESPONSE
        assert (await read_packet(reader))[:2] == (-1, SOURCE_AUTH_RESPONSE)  # refused: id -1, as Source servers do
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", port + source_offset)
        writer.write(source_packet(3, SOURCE_AUTH, "alicealice"))
        await writer.drain()
        assert (await read_packet(reader))[:2] == (3, SOURCE_RESPONSE)
        assert (await read_packet(reader))[:2] == (3, SOURCE_AUTH_RESPONSE)
        assert relay.connected() == ["Alice (Alpha)"]
        writer.write(source_packet(4, SOURCE_EXEC, "players") + source_packet(5, SOURCE_RESPONSE))
        await writer.drain()
        assert await read_packet(reader) == (4, SOURCE_RESPONSE, "1/16 players")
        assert await read_packet(reader) == (5, SOURCE_RESPONSE, "")  # the end marker, echoed
        assert client.sent[-1] == ("players", (), "Alice")
        writer.write(source_packet(6, SOURCE_EXEC, "ban #3 7d"))
        await writer.drain()
        packet = await read_packet(reader)
        assert packet[0] == 6 and "needs the admin role" in packet[2] and len(client.sent) == 1
        writer.close()
        await wait_for(lambda: not relay.connected())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())


# The next game on the dashboard ---------------------------------------------------------------

def test_the_next_game_from_status() -> None:
    assert describe_game(None) is None and describe_game({"map": "Valhalla", "mode": "Slayer"}) == "Valhalla Slayer"
    assert describe_game({"map": "The Pit", "game": "BR Starts", "mode": "Slayer"}) == "The Pit BR Starts"
    assert describe_game("Guardian Slayer") == "Guardian Slayer"
    state = ServerState("Alpha")
    StateTracker._apply_status(state, {"phase": "playing", "map": "Guardian", "mode": "Slayer", "max_players": 16,
                                       "next": {"map": "Valhalla", "mode": "Slayer"}, "playlist_vote": None})
    assert state.next_game == "Valhalla Slayer" and state.playlist_vote is None
    StateTracker._apply_status(state, {"phase": "no_game", "next": None, "playlist_vote": {"options": []}})
    assert state.next_game is None and state.playlist_vote == {"options": []}


# Starting at login ---------------------------------------------------------------------------------------------

def test_the_startup_item_works_whatever_the_folder_is_called(tmp_path: Path, run) -> None:
    """cmd.exe reads a batch file in the console's code page, not UTF-8: Huragok in C:/Users/José/... didn't start
    at login ("The system cannot find the path specified"), and a % in the path was read as a variable. The unit on
    Linux broke on a space in the path."""
    home = tmp_path / "José 100% Huragok"
    text = desktop.windows_startup_script(home).decode("utf-8")
    assert text.startswith("@echo off\r\nchcp 65001 >nul\r\n")
    assert f'/D "{tmp_path}{os.sep}José 100%% Huragok"' in text
    unit = desktop.linux_startup_unit(PurePosixPath("/home/jo sé/50% off"))  # type: ignore[arg-type]
    assert 'ExecStart="/home/jo sé/50%% off/start.sh" start --no-menu\n' in unit
    assert "WorkingDirectory=/home/jo sé/50%% off\n" in unit
    assert desktop.systemd_quote('a "b" $HOME \\ c') == '"a \\"b\\" $$HOME \\\\ c"'
    if os.name == "nt":  # what cmd.exe makes of a script written that way
        home.mkdir()
        (home / "start.cmd").write_text("@echo off\r\n", encoding="utf-8")
        target = desktop.batch_text(str(home / "start.cmd"))
        probe = tmp_path / "probe.cmd"
        probe.write_bytes(("@echo off\r\nchcp 65001 >nul\r\n"
                           f'if exist "{target}" (echo FOUND) else (echo MISSING)\r\n').encode("utf-8"))
        result = subprocess.run(["cmd", "/c", str(probe)], capture_output=True, text=True, timeout=30)
        assert "FOUND" in result.stdout, result.stdout + result.stderr
    # From the window: off the event loop.
    assert asyncio.iscoroutinefunction(desktop.enable_startup_async)
    assert asyncio.iscoroutinefunction(desktop.disable_startup_async)
