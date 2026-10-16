"""Log and console lines, as v0.9.18 writes them (copied from a real test server)."""

from __future__ import annotations

from pathlib import Path

from reclaimer_supervisor.files import FileTail, parse_console_line, parse_log_line


def test_falling_behind() -> None:
    name, data = parse_log_line(
        "21:04:10 Server falling behind: under 60 frames a second for 41 of the last 60 seconds "
        "(48.2 on average, longest frame 212 ms) with 63 players.")
    assert name == "log.falling_behind"
    assert data["behind"] == 41 and data["window"] == 60 and data["longest_ms"] == 212 and data["players"] == 63
    assert data["clock"] == "21:04:10"


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
