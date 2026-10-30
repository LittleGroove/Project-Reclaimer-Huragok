"""tempdiscord: the temporary record for 0.6.0, and statsrelay: the copy of what a server sends the stats service."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO, wait_for
from testkit.running import SERVER, everyone_joins, running

from huragok.servers.logs import parse_log_line
from huragok.servers.statsrelay import StatsCapture, configured, last_reported_to, readable
from huragok.servers.tempdiscord import Scrubber, said

ENGINE = {"engine_id": None, "service_tag": "LG9", "score": 0, "kills": 0, "deaths": 0, "assists": 0,
          "betrayals": 0, "suicides": 0}


def lines(home: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for path in sorted((home / "logs" / "temp-discord").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


def test_scrubber_hides_addresses_and_devices_and_stands_in_for_ids() -> None:
    scrub = Scrubber(b"k" * 32)
    data = scrub.value({"id": ALICE, "ip": "203.0.113.4", "name": "Alice", "devices": [{"id": "f" * 64}],
                        "text": f"banned {BRAVO} at 203.0.113.9:11774 and 2001:db8::1, at 03:07:07"})
    assert set(data) == {"id", "name", "text"}
    assert data["id"] == scrub.stand_in(ALICE) == scrub.stand_in(ALICE.upper()) and data["id"].startswith("P-")
    assert data["text"] == f"banned {scrub.stand_in(BRAVO)} at (hidden) and (hidden), at 03:07:07"
    assert Scrubber(b"x" * 32).stand_in(ALICE) != data["id"]  # another machine's key, other stand-ins
    assert scrub.value({"players": {ALICE: [], "203.0.113.4": 1}}) == {"players": {scrub.stand_in(ALICE): [], "(hidden)": 1}}
    assert scrub.value({"evidence": "a slur", "reason": "spam"}, said=True) == {"reason": "spam"}
    assert said("gg ez") == "(5 characters)" and said("!Stats Nate") == "!stats (+5 characters)" and said("/k") == "/k"


def test_stats_and_game_lines_become_events() -> None:
    assert parse_log_line("02:21:53 Stats recorded: Cookie Monster is Gunnery Sergeant grade 1 (51 EXP, skill 22)") == (
        "log.stats_recorded", {"name": "Cookie Monster", "rank": "Gunnery Sergeant", "grade": 1, "exp": 51,
                               "skill": 22, "clock": "02:21:53"})
    assert parse_log_line("17:11:29 Stats reporting to http://203.0.113.1:8790; server key abc= (whitelist this "
                          "public key on the service)")[1]["url"] == "http://203.0.113.1:8790"
    assert parse_log_line("Stats report: 96 players on Hugegrass · Custom (BR) to http://x")[0] == "log.stats_report"
    for problem in ("Stats report skipped: fewer than two identified players.",
                    "Stats reporting skipped: Map stem must be lowercase letters, digits, _ or -",
                    "Stats report abandoned after repeated failures.", "Stats service refused the report (401)"):
        assert parse_log_line(problem)[0] == "log.stats_problem", problem
    assert parse_log_line("Game countdown started: 3 seconds (Playlist)")[1] == {"what": "started", "seconds": 3,
                                                                                 "kind": "Playlist"}
    assert parse_log_line("Game countdown finished")[1] == {"what": "finished"}
    assert parse_log_line("Early end: ended the game in round 1.")[1] == {"text": "ended the game in round 1"}
    assert parse_log_line("Team change made: You'll join Blue team when you next respawn.")[0] == "log.team_change"
    assert parse_log_line("Host shell received a native match report; player data discarded.")[0] == "log.match_report"
    assert parse_log_line("Shell player-time notification: slot=2, seconds=0.016666668")[1] == {
        "slot": 2, "seconds": 0.016666668}


def test_records_events_and_players_without_anything_private(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (_, fake, _):
            await everyone_joins(fake)
            for number, player in enumerate(fake.players):
                player.update(ENGINE, engine_id=str(number), team="Red" if number % 2 else "Blue")
            await wait_for(lambda: any(l["kind"] == "event" and l["event"] == "rcon.join" for l in lines(tmp_path)))
            await fake.chat(ALICE, "Alice", "you all suck")
            await fake.chat(ALICE, "Alice", "!stats Bravo")
            await fake.event("phase", phase="playing", map="Guardian", mode="Slayer")
            await wait_for(lambda: any(l["kind"] == "players" for l in lines(tmp_path)))
            await fake.event("kill", killer="3", victim="4", report=17)
            await fake.event("kill", killer="99", victim="3", report=0)  # an engine number not seen yet: look again
            await wait_for(lambda: [l for l in lines(tmp_path) if l["kind"] == "players"
                                    and "engine number" in l["why"]])
        written = lines(tmp_path)
        kinds = {l["kind"] for l in written}
        assert {"start", "event", "players", "status"} <= kinds
        start = written[0]
        assert start["kind"] == "start" and start["servers"][0]["server"] == SERVER
        chats = [l["data"]["text"] for l in written if l["kind"] == "event" and l["event"] == "rcon.chat"]
        assert chats == ["(12 characters)", "!stats (+6 characters)"]
        kill = next(l for l in written if l["kind"] == "event" and l["event"] == "rcon.kill")
        assert kill["data"] == {"killer": "3", "victim": "4", "report": 17} and isinstance(kill["time"], float)
        players = next(l for l in written if l["kind"] == "players")
        assert players["phase"] == "playing" and players["map"] == "Guardian"
        assert players["data"]["players"][2]["service_tag"] == "LG9" and players["data"]["players"][2]["kills"] == 0
        text = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "logs" / "temp-discord").rglob("*.jsonl"))
        assert not re.search(r"[0-9a-f]{64}", text) and "203.0.113" not in text and "you all suck" not in text
        assert (tmp_path / "records" / "temp-discord.key").exists()

    run(scenario())


def test_every_server_is_captured_once_it_says_where_it_reports(tmp_path: Path, run) -> None:
    """Nothing to set: a server's report is passed on and kept in stats-reports/ from the moment Huragok knows where
    it goes (here: the server says so in server.log as it starts), and its stats_server goes back as Huragok stops."""
    async def scenario() -> None:
        async with FakeService() as service:
            async with running(tmp_path) as (supervisor, _, _):
                config = tmp_path / "server" / "dedicated.toml"
                assert configured(config) == (False, None)  # nothing known yet: left as it is
                log = supervisor.settings.server(SERVER).log_path
                with log.open("a", encoding="utf-8") as file:
                    file.write(f"10:00:00 Stats reporting to {service.url}; server key k= (whitelist this public key "
                               "on the service)\n")
                url = await wait_for(lambda: (configured(config)[1] or "").startswith("http://127.0.0.1:")
                                     and configured(config)[1])
                report = json.dumps({"players": [{"id": BRAVO, "kills": 3}]}).encode()
                status, _ = await post(url, report)
                assert status == 201 and service.received[0][2] == report
                folder = tmp_path / "logs" / "temp-discord"
                kept = await wait_for(lambda: [json.loads(line) for path in (folder / "stats-reports").glob("*.jsonl")
                                               for line in path.read_text(encoding="utf-8").splitlines()])
                assert kept[0]["server"] == SERVER and kept[0]["answer"]["status"] == 201
                assert kept[0]["request"]["body"]["json"]["players"][0]["id"].startswith("P-")
                line = await wait_for(lambda: [l for l in lines(tmp_path) if l["kind"] == "stats_report"])
                assert line[0]["status"] == 201 and line[0]["file"].startswith("stats-reports/stats-reports-")
            assert configured(config) == (False, None)  # Huragok stopped: back as it was (it had no line)

    run(scenario())


def test_two_refusals_in_a_row_stop_it_for_that_server(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeService(status=401) as service:
            config = dedicated(tmp_path / "one", service.url)
            capture = StatsCapture(tmp_path / "records", lambda s, d: None, Scrubber(b"k" * 32))
            await capture.apply(["One"], {"One": (config, None)})
            url = configured(config)[1]
            assert (await post(url, b"{}"))[0] == 401 and configured(config)[1] == url  # once: could be the report
            assert (await post(url, b"{}"))[0] == 401  # twice in a row: Huragok steps aside
            assert configured(config) == (True, service.url) and "stopped for good" in capture.notes[-1]
            await capture.close()
            again = StatsCapture(tmp_path / "records", lambda s, d: None, Scrubber(b"k" * 32))
            await again.apply(["One"], {"One": (config, None)})
            assert configured(config) == (True, service.url) and not again.listeners  # and stays aside
            await again.close()

    run(scenario())


def test_two_huragoks_reach_for_different_ports(tmp_path: Path) -> None:
    one = StatsCapture(tmp_path / "a" / "records", lambda s, d: None, None, home=tmp_path / "a")
    two = StatsCapture(tmp_path / "b" / "records", lambda s, d: None, None, home=tmp_path / "b")
    assert one.first_port != two.first_port


def test_old_days_go(tmp_path: Path, run) -> None:
    folder = tmp_path / "logs" / "temp-discord"
    folder.mkdir(parents=True)
    old = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 40 * 86400))
    recent = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 2 * 86400))
    for day in (old, recent):
        (folder / f"temp-discord-{day}.jsonl").write_text("{}\n", encoding="utf-8")

    async def scenario() -> None:
        async with running(tmp_path):
            await wait_for(lambda: not (folder / f"temp-discord-{old}.jsonl").exists())
        assert (folder / f"temp-discord-{recent}.jsonl").exists()

    run(scenario())


# ── statsrelay ─────────────────────────────────────────────────────────────────────────────────────────────────────

class FakeService:
    """The stats service: takes POST /v1/matches and answers with a receipt."""

    def __init__(self, status: int = 201) -> None:
        self.received: list[tuple[str, dict[str, str], bytes]] = []
        self.server: Any = None
        self.status = status

    async def __aenter__(self) -> FakeService:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: Any) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in head[1:] if line)}
        body = await reader.readexactly(int(headers.get("content-length", 0)))
        self.received.append((head[0], headers, body))
        answer = json.dumps({"players": [{"id": ALICE, "rank": "Sergeant", "grade": 2}]}).encode()
        writer.write(f"HTTP/1.1 {self.status} {'Created' if self.status == 201 else 'Refused'}".encode()
                     + b"\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(answer)).encode() + b"\r\n\r\n" + answer)
        await writer.drain()
        writer.close()


async def post(url: str, body: bytes) -> tuple[int, bytes]:
    host, port = url.removeprefix("http://").split(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(b"POST /v1/matches HTTP/1.1\r\nHost: " + url.encode() + b"\r\nContent-Type: application/json\r\n"
                 b"X-Signature: sig==\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    await writer.drain()
    answer = await reader.read()
    writer.close()
    status = int(answer.split(b" ", 2)[1])
    return status, answer.split(b"\r\n\r\n", 1)[1]


def dedicated(folder: Path, stats: str | None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "dedicated.toml"
    line = f'stats_server = "{stats}"  # where finished games go\n' if stats is not None else ""
    path.write_text(f'# my server\ndata = "data"\n{line}\n[[server]]\nname = "One"\nport = 46176\n', encoding="utf-8")
    return path


def test_capture_passes_reports_on_unchanged_and_keeps_a_copy(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with FakeService() as service:
            config = dedicated(tmp_path / "one", service.url)
            kept: list[tuple[str, dict[str, Any]]] = []
            capture = StatsCapture(tmp_path / "records", lambda s, d: kept.append((s, d)), Scrubber(b"k" * 32))
            await capture.apply(["one"], {"One": (config, None)})
            _, url = configured(config)
            assert url and url.startswith("http://127.0.0.1:45")
            assert "# where finished games go" in config.read_text(encoding="utf-8")  # the host's note stays
            report = json.dumps({"players": [{"id": BRAVO, "kills": 12}], "map": "guardian"}).encode()
            status, answer = await post(url, report)
            assert status == 201 and json.loads(answer)["players"][0]["rank"] == "Sergeant"
            request_line, headers, body = service.received[0]
            assert request_line == "POST /v1/matches HTTP/1.1" and body == report  # byte for byte
            assert headers["x-signature"] == "sig==" and headers["content-type"] == "application/json"
            server, record = kept[0]
            assert server == "One" and record["answer"]["status"] == 201
            assert record["request"]["body"]["json"]["players"][0]["id"].startswith("P-")
            assert BRAVO not in json.dumps(record) and ALICE not in json.dumps(record)
            await capture.close()
            assert configured(config) == (True, service.url)  # back as it was
            # Next time it starts on the same port: a server still running finds it there.
            again = StatsCapture(tmp_path / "records", lambda s, d: None, Scrubber(b"k" * 32))
            await again.apply(["One"], {"One": (config, None)})
            assert configured(config)[1] == url
            await again.apply([], {"One": (config, None)})  # taken off the list
            assert configured(config) == (True, service.url)
            await again.close()

    run(scenario())


def test_capture_finds_the_service_in_the_log_and_leaves_off_alone(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        config = dedicated(tmp_path / "one", "")
        log = tmp_path / "one" / "server.log"
        capture = StatsCapture(tmp_path / "records", lambda s, d: None, Scrubber(b"k" * 32))
        await capture.apply(["One"], {"One": (config, log)})
        assert configured(config) == (True, "") and "waiting until it has started once" in capture.notes[-1]
        log.write_text("10:00:00 Stats reporting to http://203.0.113.1:8790; server key k= (whitelist this public "
                       "key on the service)\n", encoding="utf-8")
        assert last_reported_to(log, set()) == "http://203.0.113.1:8790"
        await capture.apply(["One"], {"One": (config, log)})
        assert configured(config)[1].startswith("http://127.0.0.1:")
        await capture.close()
        assert configured(config) == (True, "")
        off = dedicated(tmp_path / "two", "off")
        await capture.apply(["One"], {"One": (off, log)})
        assert configured(off) == (True, "off") and "reports nowhere" in capture.notes[-1]
        missing = dedicated(tmp_path / "three", None)
        await capture.apply(["One"], {"One": (missing, log)})
        await capture.close()
        assert configured(missing) == (False, None)  # it had no line: none is left behind

    run(scenario())


def test_capture_answers_502_when_the_service_cant_be_reached(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        config = dedicated(tmp_path / "one", "http://127.0.0.1:9")  # nothing listens there
        kept: list[tuple[str, dict[str, Any]]] = []
        capture = StatsCapture(tmp_path / "records", lambda s, d: kept.append((s, d)), Scrubber(b"k" * 32))
        await capture.apply(["One"], {"One": (config, None)})
        status, _ = await post(configured(config)[1], b"{}")
        assert status == 502 and kept[0][1]["answer"]["status"] == 502
        await capture.close()

    run(scenario())


def test_readable_bodies() -> None:
    assert readable(b'{"a": 1}', []) == {"json": {"a": 1}}
    assert readable(b"hello", []) == {"text": "hello"}
    assert readable(b"\xff\xfe", []) == {"base64": "//4="}
