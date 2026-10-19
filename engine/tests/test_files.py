"""Log and console lines, as v0.9.18 writes them (copied from a real test server)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from huragok.files import FileTail, open_shared, parse_console_line, parse_log_line, read_status


def test_falling_behind() -> None:
    name, data = parse_log_line(
        "21:04:10 Server falling behind: under 54 frames a second for 41 of the last 60 seconds "
        "(48.2 on average, longest frame 212 ms) with 63 players.")
    assert name == "log.falling_behind"
    assert data["behind"] == 41 and data["window"] == 60 and data["longest_ms"] == 212 and data["players"] == 63
    assert data["clock"] == "21:04:10"


def test_a_name_of_digits_stays_a_name() -> None:
    name, data = parse_log_line("Community roster: 007 joined the server")
    assert name == "log.joined" and data["name"] == "007"  # not the number 7: votes and joins match it by name
    name, data = parse_log_line("1337 could not call a vote: 007 can't be vote-kicked on this server.")
    assert name == "log.vote_refused" and data["caller"] == "1337"
    name, data = parse_log_line("Server falling behind: under 54 frames a second for 41 of the last 60 seconds "
                                "(48.2 on average, longest frame 212 ms) with 63 players.")
    assert data["fps"] == 54 and data["players"] == 63  # numbers are still numbers


def test_upload() -> None:
    name, data = parse_log_line("Network upload over the last 5 minutes: 5.2 MB/s to 64 players (81 KB/s each): x")
    assert name == "log.upload" and data["rate"] == 5.2 and data["unit"] == "MB" and data["players"] == 64


def test_roster_lost() -> None:
    # From the main server, at the end of a game.
    details = ('{"launch_state":11,"lifecycle":1,"peers":"00000000000000000000000000000001",'
               '"players":"00000000000000000000000000000000","results":{"finalized":true,"finished":true,'
               '"players":37},"session_mode":1,"session_state":6,"start_error":13,"start_status":7}')
    name, data = parse_log_line(f"04:12:09 Host roster lost 28 of 28 players in one update (0 now present): {details}")
    assert name == "log.roster_lost"
    assert data["lost"] == 28 and data["of"] == 28 and data["present"] == 0 and data["details"] == details


def test_rcon_sign_ins() -> None:
    name, data = parse_log_line("00:33:23 RCON: 127.0.0.1:64289 signed in (WebSocket, from the server's machine).")
    assert name == "log.rcon_signin" and data["host_slot"] is True and data["address"] == "127.0.0.1:64289"
    _, data = parse_log_line("00:33:31 RCON: 127.0.0.1:64292 signed in (WebSocket).")
    assert data["host_slot"] is False
    name, data = parse_log_line("23:52:44 RCON: 127.0.0.1:55148 was turned away: 4 tools are connected.")
    assert name == "log.rcon_refused" and data["reason"] == "4 tools are connected"


def test_reload_and_noise() -> None:
    name, data = parse_log_line("23:53:53 Reload result: Configuration reloaded. Now: max_players 16 → 12.")
    assert name == "log.reload" and "max_players 16 → 12" in data["text"]
    assert parse_log_line("23:51:38 Public admission rejected: aborted by peer: the application …") is None
    assert parse_log_line("23:52:15 Forge library unavailable: the engine refused it (error 6).") is None


def test_console_lines() -> None:
    name, data = parse_console_line(
        "23:56:23 [LG Test Server] started (process 24264); players join at 203.0.113.7:49176.")
    assert name == "server.started" and data["pid"] == 24264 and data["server"] == "LG Test Server"
    name, data = parse_console_line(
        "23:54:21 [LG Test Server] stopped: Engine probe worker failed: exit code: 1; restarting in 5 s. "
        "See H:\\x\\data\\lg-test-server\\server.log.")
    assert name == "server.crashed" and data["kind"] == "engine_exit" and data["wait"] == "5 s"
    name, data = parse_console_line(
        "[Big Team Battle] stopped: its game stopped responding for five minutes; restarting in 5 s. See data.")
    assert data["kind"] == "hang"
    assert parse_console_line("23:53:53 Configuration reloaded.")[0] == "server.reloaded"
    assert parse_console_line("All servers stopped.")[0] == "server.all_stopped"
    assert parse_console_line("[LG Test Server] RCON listening on TCP 127.0.0.1:49176.") is None


def test_tail_follows_appends_partial_lines_and_rollover(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    old = tmp_path / "server.log.old"
    log.write_bytes(b"before we started\n")
    tail = FileTail(log, old)
    assert tail.read_lines() == []  # starts at the end
    with log.open("ab") as f:
        f.write("one\ntw".encode())
    assert tail.read_lines() == ["one"]
    with log.open("ab") as f:
        f.write("o ✓\r\n".encode())
    assert tail.read_lines() == ["two ✓"]
    with log.open("ab") as f:
        f.write(b"last line of the old file\n")
    log.rename(old)
    log.write_bytes(b"first of the new\n")
    assert tail.read_lines() == ["last line of the old file", "first of the new"]


def test_tail_survives_missing_and_truncated(tmp_path: Path) -> None:
    log = tmp_path / "server.log"
    tail = FileTail(log, from_start=True)
    assert tail.read_lines() == []
    log.write_bytes(b"a\nb\n")
    assert tail.read_lines() == ["a", "b"]
    log.write_bytes(b"c\n")  # shorter: truncated
    assert tail.read_lines() == ["c"]


ONLINE = "00:00:01 Online: players join at 203.0.113.7:49176."
CONNECTED = f"00:00:02 Bravo connected from 203.0.113.4 (player ID {'D' * 64}, ping 31 ms)."


def test_a_log_that_appears_after_the_first_look_is_read_from_its_start(tmp_path: Path) -> None:
    # A new server's first start: its server.log didn't exist when Huragok started.
    log = tmp_path / "server.log"
    tail = FileTail(log, tmp_path / "server.log.old")
    assert tail.read_lines() == []
    log.write_text(f"{ONLINE}\n{CONNECTED}\n", encoding="utf-8")
    assert tail.read_lines() == [ONLINE, CONNECTED]
    assert parse_log_line(CONNECTED) == ("log.connected", {"name": "Bravo", "id": "d" * 64, "clock": "00:00:02"})


def test_a_line_cut_by_a_rollover_or_truncation_is_never_glued_to_the_next(tmp_path: Path) -> None:
    log, old = tmp_path / "server.log", tmp_path / "server.log.old"
    log.write_bytes(b"")
    tail = FileTail(log, old)
    tail.read_lines()
    with log.open("ab") as f:
        f.write(b"00:00:04 Host roster lost 20 of 20 players in one upd")
    assert tail.read_lines() == []  # half a line waits
    with log.open("ab") as f:
        f.write(b"ate (0 now present)")  # the old file's last line, without a newline
    log.rename(old)
    log.write_bytes(b"00:00:05 first of the new\n")
    assert tail.read_lines() == ["00:00:04 Host roster lost 20 of 20 players in one update (0 now present)",
                                 "00:00:05 first of the new"]
    with log.open("ab") as f:
        f.write(b"abc")
    assert tail.read_lines() == []
    log.write_bytes(b"x\n")  # truncated, with "abc" pending: it's dropped, not read as "abcx"
    assert tail.read_lines() == ["x"]


def test_status_json_caught_mid_write_or_odd_is_skipped(tmp_path: Path) -> None:
    status = tmp_path / "status.json"
    assert read_status(status) is None  # not there
    status.write_text('{"updated": 17906', encoding="utf-8")  # half written
    assert read_status(status) is None
    status.write_bytes(b"\xff\xfe not json")
    assert read_status(status) is None
    status.write_text("[1, 2]", encoding="utf-8")  # JSON, but not the server's object
    assert read_status(status) is None
    status.write_text('{"updated": 1790600227, "players": []}', encoding="utf-8")
    assert read_status(status) == {"updated": 1790600227, "players": []}


@pytest.mark.skipif(os.name != "nt", reason="only Windows stops a rename while a file is open")
def test_reading_never_stops_the_server_renaming_its_log(tmp_path: Path) -> None:
    log, old = tmp_path / "server.log", tmp_path / "server.log.old"
    log.write_bytes(b"one\n")
    with open_shared(log) as file:
        log.rename(old)  # what the server does at 20 MB; Python's own open() makes this fail on Windows
        assert file.read() == b"one\n"
    assert old.exists() and not log.exists()


@pytest.mark.skipif(os.name != "nt", reason="the ctypes route is Windows' only")
def test_shared_opening_falls_back_to_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from huragok import files

    def broken(path: Path) -> Any:
        raise AttributeError("no CreateFileW here")

    monkeypatch.setattr(files, "_open_windows", broken)
    (tmp_path / "a.txt").write_bytes(b"ok")
    with files.open_shared(tmp_path / "a.txt") as file:
        assert file.read() == b"ok"
    with pytest.raises(FileNotFoundError):
        files.open_shared(tmp_path / "missing.txt")


# What someone typed, echoed into server.log, must never read as the server's own line (H2) ------------------------

FORGED_LOSS = "Host roster lost 99 of 99 players in one update (0 now present): gg"
FORGED_CONNECT = f"Zed connected from 203.0.113.9 (player ID {'9' * 64}, ping 5 ms)."


def test_echoed_commands_and_reasons_are_not_health_lines() -> None:
    name, data = parse_log_line(f"20:01:58 RCON 127.0.0.1:60106 as Alice: say {FORGED_LOSS}")
    assert name == "log.rcon_command" and data["by"] == "Alice" and data["command"] == f"say {FORGED_LOSS}"
    name, data = parse_log_line(f"20:01:58 RCON 127.0.0.1:60106: say {FORGED_LOSS}")  # 0.9.18 without a `by`
    assert name == "log.rcon_command" and data == {"address": "127.0.0.1:60106", "command": f"say {FORGED_LOSS}",
                                                   "clock": "20:01:58"}
    reason = f"You are banned from this server. Reason: {FORGED_LOSS}"
    for line in (f"20:02:00 Refused Bravo from 203.0.113.4 (player ID {'d' * 64}): {reason}",
                 f"20:02:00 Public admission rejected: Preflight: {reason}",
                 f"20:02:01 Alice (RCON) kicked Bravo (player ID {'d' * 64}). Reason: {FORGED_CONNECT}",
                 f"20:02:02 RCON banned Bravo (player ID {'d' * 64}, address 203.0.113.4). Reason: {FORGED_CONNECT}",
                 f"20:02:03 Refused Bravo from 203.0.113.4 (player ID {'d' * 64}): {FORGED_CONNECT}",
                 f"20:02:04 Vote 3 passed: {FORGED_LOSS}"):
        assert parse_log_line(line) is None, line
    name, _ = parse_log_line(f"20:02:05 RCON 127.0.0.1:60106: say {FORGED_CONNECT}")
    assert name == "log.rcon_command"


def test_the_servers_own_lines_still_read_as_before() -> None:
    assert parse_log_line(f"21:15:44 {FORGED_LOSS}")[0] == "log.roster_lost"
    assert parse_log_line(f"00:00:02 {FORGED_CONNECT}")[0] == "log.connected"
    assert parse_log_line(f"17:12:11 Bravo connected from 127.0.0.1 (player ID {'a' * 64}, ping 0 ms), an admin.")[
        1]["name"] == "Bravo"
    assert parse_log_line("19:16:25 RCON listening on TCP 127.0.0.1:49176 (Source RCON and WebSocket).") is None
    assert parse_log_line("21:15:05 RCON ended the game.") is None
    assert parse_log_line("17:11:29 Setting up Online: detecting your address and configuring router ports…") is None
    assert parse_log_line("17:56:03 A vote kicked Bravo (player ID " + "d" * 64 + ").") is None


def test_wrong_passwords_and_a_lockout_in_the_log() -> None:
    assert parse_log_line("10:00:01 RCON: 127.0.0.1:60200 gave a wrong password.") == (
        "log.rcon_wrong_password", {"address": "127.0.0.1:60200", "clock": "10:00:01"})
    name, data = parse_log_line("10:00:05 RCON: 127.0.0.1:60204 gave a wrong password 5 times; refusing 127.0.0.1 "
                                "for 10 minutes.")
    assert name == "log.rcon_wrong_password"
    assert data["times"] == 5 and data["refused"] == "127.0.0.1" and data["minutes"] == 10
