"""Project Reclaimer 0.10: server.log's tagged lines, and players who join with no name (their network key as the ID).

The 0.9 lines are as the tests of logs.py have them (v0.9.18-0.9.21, from real servers); the 0.10 lines as the host's
0.10.0 test server and this PC's probe server (DEPLOY/probe-0.10) wrote them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import free_port, wait_for, write_server_folder, write_settings
from testkit.running import SERVER, running

from huragok.core.events import Event, EventBus
from huragok.core.settings import load
from huragok.core.storage import Storage
from huragok.players.commands.votekicks import _named
from huragok.servers.logs import FileWatcher, parse_log_line
from huragok.servers.state import NAMELESS_SECONDS, Player, ServerState, StateTracker
from huragok.servers.stats import live_id
from huragok.servers.watchdog import JOIN_GRACE, Watchdog
from huragok.vergil import Vergil

KEY = "0a1b2c3d" + "0" * 52 + "9e8f"  # a network key: 0.10's player ID
OTHER = "5d4c3b2a" + "1" * 52 + "7a6b"


# The parser: 0.9 as before ---------------------------------------------------------------------------------------

def test_the_09_lines_read_as_before() -> None:
    assert parse_log_line(f"00:00:02 Bravo connected from 203.0.113.4 (player ID {'D' * 64}, ping 31 ms).") == (
        "log.connected", {"name": "Bravo", "id": "d" * 64, "clock": "00:00:02"})
    assert parse_log_line("Community roster: 007 joined the server") == ("log.joined", {"name": "007"})
    assert parse_log_line("12:00:01 ODST: player Bravo joined the game in progress and spawns now.")[0] == "log.joined"
    assert parse_log_line("04:18:57 Playlist lobby voting opened: HugeGrass BR Fiesta Rotation")[1] == {
        "playlist": "HugeGrass BR Fiesta Rotation", "clock": "04:18:57"}
    assert parse_log_line("Playlist lobby voting opened: BTB (Big Team)")[1]["playlist"] == "BTB (Big Team)"  # 0.9: all
    assert parse_log_line("04:19:22 Playlist selected: BR Fiesta CTF on HugeGrass · Custom")[1]["kind"] == "Custom"
    assert parse_log_line("00:33:23 RCON: 127.0.0.1:64289 signed in (WebSocket, from the server's machine).")[1][
        "host_slot"] is True
    assert parse_log_line("20:01:59 RCON 127.0.0.1:60106 as Alice: say hi")[1] == {
        "address": "127.0.0.1:60106", "by": "Alice", "command": "say hi", "clock": "20:01:59"}
    assert parse_log_line("17:11:29 Stats reporting to http://203.0.113.1:8790; server key abc= (whitelist this)")[1][
        "url"] == "http://203.0.113.1:8790"
    assert parse_log_line("Game countdown started: 3 seconds (Playlist)")[1] == {"what": "started", "seconds": 3,
                                                                                 "kind": "Playlist"}
    assert parse_log_line("03:07:07 pjams could not call a vote: Charlie can't be vote-kicked on this server.")[0] == \
        "log.vote_refused"
    assert parse_log_line("04:12:09 Host roster lost 28 of 28 players in one update (0 now present)")[0] == \
        "log.roster_lost"
    assert parse_log_line("20:02:00 Refused Bravo from 203.0.113.4 (player ID " + "d" * 64 + "): Zed connected "
                          "from 203.0.113.9 (player ID " + "9" * 64 + ", ping 5 ms).") is None


# The parser: 0.10's tagged lines ---------------------------------------------------------------------------------

def test_a_player_getting_in_on_010() -> None:
    assert parse_log_line(f"[server] player #1 joined: {KEY.upper()} from 203.0.113.9:51234") == (
        "log.admitted", {"number": 1, "id": KEY, "address": "203.0.113.9:51234"})
    assert parse_log_line("[online] connected to 0x0123456789abcdef at 203.0.113.9:51234") == (
        "log.connected", {"handle": "0x0123456789abcdef", "address": "203.0.113.9:51234"})
    assert parse_log_line("[looks] 0x0123456789abcdef's player Bravo Two proved their identity") == (
        "log.identified", {"handle": "0x0123456789abcdef", "name": "Bravo Two"})
    assert parse_log_line("[server] player #1 (Bravo Two) left: closed by the peer (0)") == (
        "log.left", {"number": 1, "name": "Bravo Two", "reason": "closed by the peer (0)"})
    assert parse_log_line("[looks] 0x1's player 007 proved their identity")[1]["name"] == "007"  # a name, not 7
    # A visit for mods (or a bot's) is no player getting in: it says which connection was one.
    assert parse_log_line(f"[server] a visit (mods or a bot) from 203.0.113.9:51234: {KEY}") == (
        "log.visit", {"what": "mods or a bot", "address": "203.0.113.9:51234"})
    assert parse_log_line("[server] a visit (mods) from [2001:db8::7]:5000") == (
        "log.visit", {"what": "mods", "address": "[2001:db8::7]:5000"})
    assert parse_log_line(f"[chat] line Bravo: [server] a visit (mods or a bot) from 203.0.113.9:1: {KEY}") is None


def test_the_010_game_flow() -> None:
    assert parse_log_line("[server] playlist lobby voting opened: Race Tests (Race on Road Course, Death Race on "
                          "Road Course, Race on Grand Prix)") == (
        "log.ballot_opened", {"playlist": "Race Tests",
                              "choices": "Race on Road Course, Death Race on Road Course, Race on Grand Prix"})
    assert parse_log_line("[server] playlist lobby voting opened: Race Tests")[1] == {"playlist": "Race Tests"}
    assert parse_log_line("[server] playlist selected: Race on Road Course") == (
        "log.ballot_picked", {"picked": "Race on Road Course"})
    assert parse_log_line("[server] 1 player(s) in the lobby: Race on Road Course starts in 3 s") == (
        "log.countdown", {"players": 1, "game": "Race on Road Course", "seconds": 3, "what": "started"})
    assert parse_log_line("[server] every player left: ending the game") == (
        "log.early_end", {"text": "every player left: ending the game"})
    _, data = parse_log_line("[server] Network upload over the last 5 minutes: 0.0 MB/s to 1 players (8 KB/s each): "
                             "the game x")
    assert data["players"] == 1 and data["unit"] == "MB"
    assert parse_log_line("[server] Server falling behind: under 54 frames a second for 41 of the last 60 seconds "
                          "(48.2 on average, longest frame 212 ms) with 63 players.")[0] == "log.falling_behind"
    assert parse_log_line("[online] taking joins") == ("log.online", {})
    assert parse_log_line("[stats] reporting to http://203.0.113.1:45887 as abc=") == (
        "log.stats_reporting", {"url": "http://203.0.113.1:45887", "key": "abc="})
    for line in ("[server] getting Race on Road Course ready", "[server] next game: Race on Road Course",
                 "[server] starting Race on Road Course", "[server] game 1 begun: Race on Road Course with 1 player(s)",
                 "[server] back in the lobby", "[engine] game mode 'Race' loaded", "[server] RCON on TCP 127.0.0.1:46991",
                 '[server] "LG Test Server": 4 game(s), voted on for 20 s, starting with Death Race on Grand Prix'):
        assert parse_log_line(line) is None, line


def test_the_010_console_lines() -> None:
    assert parse_log_line("[rcon] 127.0.0.1:59748 signed in (WebSocket)") == (
        "log.rcon_signin", {"address": "127.0.0.1:59748", "how": "WebSocket", "host_slot": False})
    assert parse_log_line("[rcon] 127.0.0.1:59795: immune list") == (
        "log.rcon_command", {"address": "127.0.0.1:59795", "command": "immune list"})
    # What someone typed stays a command, however it reads.
    assert parse_log_line("[rcon] 127.0.0.1:1: signed in (WebSocket)")[0] == "log.rcon_command"
    assert parse_log_line("[rcon] 127.0.0.1:1: say [server] player #1 joined: " + KEY + " from 1.2.3.4:5")[0] == \
        "log.rcon_command"


def test_what_players_say_and_wrote_is_never_the_servers_own_line() -> None:
    forged = "Host roster lost 99 of 99 players in one update (0 now present)"
    assert parse_log_line(f"[chat] line Bravo: {forged}") is None
    assert parse_log_line(f"[chat] line Bravo: [server] player #1 joined: {KEY} from 1.2.3.4:5") is None
    assert parse_log_line("04:25:11 [all] Playlist selected: Slayer on Guardian") is None  # a player of that name
    assert parse_log_line("04:25:11 [team] Stats report: x") is None
    # 0.9's lines that start with a name can't be made out of a 0.10 line's reason.
    assert parse_log_line(f"[server] Zed connected from 203.0.113.9 (player ID {'9' * 64}, ping 5 ms).") is None
    assert parse_log_line("[server] Alice could not call a vote: Bravo can't be vote-kicked on this server.") is None
    assert parse_log_line(f"[server] refused Bravo from 203.0.113.4: {forged}") is None


def test_the_probe_servers_whole_log_says_only_what_it_should() -> None:
    log = Path(__file__).resolve().parents[3] / "DEPLOY" / "probe-0.10" / "data" / "probe-reach" / "server.log"
    if not log.exists():
        return  # this PC's probe; elsewhere there's nothing to read
    names = [parsed[0] for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
             if (parsed := parse_log_line(line))]
    assert set(names) <= {"log.mod_problem", "log.rcon_signin", "log.online"}
    assert "log.online" in names


def test_the_file_watcher_names_a_leaving_player_by_their_key(tmp_path: Path) -> None:
    data = write_server_folder(tmp_path / "server", free_port())
    server = load(write_settings(tmp_path, tmp_path / "server")).servers[0]
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("log.*", seen.append)
    watcher = FileWatcher(server, ServerState(server.name), bus)
    (data / "server.log").write_text("", encoding="utf-8")
    watcher.poll_log()
    with (data / "server.log").open("a", encoding="utf-8") as file:
        file.write(f"[server] player #3 joined: {KEY} from 203.0.113.9:51234\n"
                   "[online] connected to 0x0123456789abcdef at 203.0.113.9:51234\n"
                   "[looks] 0x0123456789abcdef's player Bravo proved their identity\n"
                   "[server] player #3 (Bravo) left: closed by the peer (0)\n"
                   "[server] player #4 () left: timed out\n")
    watcher.poll_log()
    assert [e.name for e in seen] == ["log.admitted", "log.connected", "log.identified", "log.left", "log.left"]
    assert seen[3]["id"] == KEY and seen[3]["name"] == "Bravo"
    assert "id" not in seen[4].data and seen[4]["name"] == ""


def test_the_last_lines_before_010_starts_again_are_read(tmp_path: Path) -> None:
    # 0.10 moves server.log to server.previous.log as it starts (0.9 rolled it to server.log.old at 20 MB).
    data = write_server_folder(tmp_path / "server", free_port())
    server = load(write_settings(tmp_path, tmp_path / "server")).servers[0]
    watcher = FileWatcher(server, ServerState(server.name), EventBus())
    log = data / "server.log"
    log.write_text("[host] Project Reclaimer 0.10.0 (rcl-engine)\n", encoding="utf-8")
    assert watcher.tail.read_lines() == []
    with log.open("a", encoding="utf-8") as file:
        file.write("[server] every player left: ending the game\n")
    log.rename(data / "server.previous.log")
    log.write_text("[host] Project Reclaimer 0.10.0 (rcl-engine)\n", encoding="utf-8")
    assert watcher.tail.read_lines() == ["[server] every player left: ending the game",
                                         "[host] Project Reclaimer 0.10.0 (rcl-engine)"]


# The watchdog's no_joins on 0.10 ----------------------------------------------------------------------------------

NOW = 1_800_000_000.0


def dog(state: ServerState) -> Watchdog:
    ctx = SimpleNamespace(name="watchdog", servers={state.name: state}, server=lambda name: state, pid=lambda name: None,
                          run_started=lambda name: None, restart_setting=lambda name, key: 900.0,
                          lifecycle=SimpleNamespace(busy=set(), planned={}, can_control=False),
                          log=logging.getLogger("part.watchdog"), emit=lambda *args, **data: None,
                          config=dict(Watchdog.defaults))
    watchdog = Watchdog(ctx)  # type: ignore[arg-type]
    watchdog.check_config()
    asyncio.run(watchdog.setup())
    return watchdog


def feed(watchdog: Watchdog, *events: Event) -> None:
    async def go() -> None:
        for event in events:
            handler = {"log.connected": watchdog.connected, "log.admitted": watchdog.joined,
                       "log.visit": watchdog.visited, "rcon.join": watchdog.joined, "rcon.leave": watchdog.left,
                       "log.left": watchdog.left}[event.name]
            await handler(event)

    asyncio.run(go())


def key(n: int) -> str:
    return f"{n:064x}"


def connected(n: int, at: float) -> Event:
    return Event("log.connected", "Alpha", {"handle": f"0x{n:016x}", "address": f"203.0.113.{n}:5000"}, at)


def let_in(n: int, at: float) -> Event:
    return Event("log.admitted", "Alpha", {"number": n, "id": key(n), "address": f"203.0.113.{n}:5000"}, at)


def console_join(n: int, at: float) -> Event:
    return Event("rcon.join", "Alpha", {"number": n, "name": "", "id": key(n), "ip": f"203.0.113.{n}"}, at)


def log_left(n: int, at: float) -> Event:
    return Event("log.left", "Alpha", {"number": n, "name": f"P{n}", "reason": "timed out", "id": key(n)}, at)


def test_a_dead_010_server_lets_players_in_and_drops_them() -> None:
    state = ServerState("Alpha", connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300, 250), 1):
        feed(watchdog, connected(n, NOW - ago), let_in(n, NOW - ago), console_join(n, NOW - ago + 1),
             log_left(n, NOW - ago + 18))
    assert "no_joins" in watchdog.findings("Alpha", NOW)
    # The console away, and only the log to go by: the same.
    state.connected = False
    assert "no_joins" in watchdog.findings("Alpha", NOW)


def test_players_who_connect_and_are_never_let_in_are_failed_joins() -> None:
    watchdog = dog(ServerState("Alpha", connected=True, max_players=16))
    feed(watchdog, *(connected(n, NOW - ago) for n, ago in enumerate((400, 350, 300), 1)))
    assert "no_joins" in watchdog.findings("Alpha", NOW)


def test_players_let_in_who_stay_have_joined_with_the_console_there_or_not() -> None:
    state = ServerState("Alpha", connected=True, max_players=16)
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connected(n, NOW - ago), let_in(n, NOW - ago), console_join(n, NOW - ago + 1))
    state.players = {n: Player(n, "", key(n)) for n in (1, 2, 3)}  # on, and maybe with no name yet
    assert "no_joins" not in watchdog.findings("Alpha", NOW)
    assert len(watchdog.admitted["Alpha"]) == 3  # the log's line and the console's join: one admission each
    # With the console away from the start, the log alone: a player not seen leaving is on.
    watchdog = dog(ServerState("Alpha", connected=False, max_players=16))
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connected(n, NOW - ago), let_in(n, NOW - ago))
    assert "no_joins" not in watchdog.findings("Alpha", NOW)
    # One who stayed past the grace and left still got in.
    watchdog = dog(ServerState("Alpha", connected=False, max_players=16))
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, connected(n, NOW - ago), let_in(n, NOW - ago))
    feed(watchdog, log_left(1, NOW - 400 + JOIN_GRACE + 30), log_left(2, NOW - 340), log_left(3, NOW - 290))
    assert "no_joins" not in watchdog.findings("Alpha", NOW)


def visit(n: int, at: float, port: int = 6000) -> list[Event]:
    """A game looking at the server's mods: its connection, then 0.10's visit line for it."""
    address = f"203.0.113.{n}:{port}"
    return [Event("log.connected", "Alpha", {"handle": f"0x{port:016x}", "address": address}, at),
            Event("log.visit", "Alpha", {"what": "mods or a bot", "address": address}, at)]


def from_log(*lines: tuple[float, str]) -> list[Event]:
    """server.log's lines as the watchdog gets them, each this many seconds before NOW."""
    events = []
    for ago, line in lines:
        parsed = parse_log_line(line)
        assert parsed, line
        events.append(Event(parsed[0], "Alpha", parsed[1], NOW - ago))
    return events


def test_a_look_at_the_servers_mods_is_no_try_to_get_in() -> None:
    # 7 Nov 2026, 22:53 on the LG Test Server: six players mid-game, and three looks at the server (two of them one
    # player's) restarted it, as "3 players connected in 5 minutes and none got in".
    state = ServerState("Alpha", connected=True, max_players=16)
    state.players = {n: Player(n, f"P{n}", key(n)) for n in range(10, 16)}
    watchdog = dog(state)
    feed(watchdog, *visit(1, NOW - 330), *visit(2, NOW - 186), *visit(2, NOW - 180, 6001))
    assert "no_joins" not in watchdog.findings("Alpha", NOW)
    assert not watchdog.connects["Alpha"]
    # A visit takes away its own connection only: the player's own one after it still tries to get in.
    watchdog = dog(state)
    for n, ago in enumerate((400, 350, 300), 1):
        feed(watchdog, *visit(n, NOW - ago), connected(n, NOW - ago + 1))
    assert watchdog.findings("Alpha", NOW)["no_joins"] == "3 players tried to join in 5 minutes and none got in"
    # A visit line with no connection of its own just before (another address, or too long ago) takes none.
    watchdog = dog(state)
    feed(watchdog, *(connected(n, NOW - ago) for n, ago in enumerate((400, 350, 300), 1)))
    feed(watchdog, Event("log.visit", "Alpha", {"what": "mods", "address": "203.0.113.1:5000"}, NOW - 200),
         Event("log.visit", "Alpha", {"what": "mods", "address": "198.51.100.1:5000"}, NOW - 300))
    assert len(watchdog.connects["Alpha"]) == 3 and "no_joins" in watchdog.findings("Alpha", NOW)


def test_a_player_trying_again_and_again_is_one_player() -> None:
    watchdog = dog(ServerState("Alpha", connected=True, max_players=16))
    feed(watchdog, *(Event("log.connected", "Alpha", {"handle": f"0x{n:016x}", "address": f"203.0.113.1:{5000 + n}"},
                           NOW - 400 + n * 30) for n in range(5)))
    assert "no_joins" not in watchdog.findings("Alpha", NOW)
    feed(watchdog, connected(2, NOW - 300), connected(3, NOW - 250))
    assert watchdog.findings("Alpha", NOW)["no_joins"] == "3 players tried to join in 5 minutes and none got in"
    # 0.9's line names the player: the same ID is the same player, from any address.
    watchdog = dog(ServerState("Alpha", connected=True, max_players=16))
    feed(watchdog, *(Event("log.connected", "Alpha", {"name": "Bravo", "id": KEY}, NOW - ago) for ago in (400, 300, 200)))
    assert "no_joins" not in watchdog.findings("Alpha", NOW)


def test_the_three_restarts_of_7_november_were_players_getting_in() -> None:
    """The LG Test Server, 7 Nov 2026, from its server.log and Huragok's record of the events (times kept, addresses,
    handles and keys made up): each of these restarted it as "nobody can join"."""
    # 22:57: a fresh run. Four looks at the mods, then the host joined and was on, 99 s before the check.
    state = ServerState("Alpha", connected=True, max_players=16)
    watchdog = dog(state)
    feed(watchdog, *from_log(
        (171, "[online] connected to 0x00000000000000a1 at 198.51.100.20:50329"),
        (171, "[server] a visit (mods or a bot) from 198.51.100.20:50329: " + "a" * 64),
        (161, "[online] connected to 0x00000000000000a2 at 198.51.100.21:61769"),
        (161, "[server] a visit (mods or a bot) from 198.51.100.21:61769: " + "b" * 64),
        (153, "[online] connected to 0x00000000000000a3 at 198.51.100.20:50248"),
        (153, "[server] a visit (mods or a bot) from 198.51.100.20:50248: " + "c" * 64),
        (138, "[online] connected to 0x00000000000000a4 at 198.51.100.20:50430"),
        (138, "[server] a visit (mods or a bot) from 198.51.100.20:50430: " + "d" * 64),
        (99, "[online] connected to 0x00000000000000a5 at 198.51.100.21:44330"),
        (99, f"[server] player #1 joined: {KEY} from 198.51.100.21:44330")))
    state.players = {1: Player(1, "Host", KEY)}
    assert "no_joins" not in watchdog.findings("Alpha", NOW)
    # 23:09: four players in a race. One player looked at the mods three times, joined, and timed out 47 s later.
    watchdog = dog(state)
    feed(watchdog, *from_log(
        (408, "[online] connected to 0x00000000000000a6 at 198.51.100.30:51812"),
        (408, "[server] a visit (mods or a bot) from 198.51.100.30:51812: " + "e" * 64),
        (400, "[online] connected to 0x00000000000000a7 at 198.51.100.30:51818"),
        (400, "[server] a visit (mods or a bot) from 198.51.100.30:51818: " + "f" * 64),
        (336, "[online] connected to 0x00000000000000a8 at 198.51.100.30:65072"),
        (336, "[server] a visit (mods or a bot) from 198.51.100.30:65072: " + "1" * 64),
        (327, "[online] connected to 0x00000000000000a9 at 198.51.100.30:44330"),
        (327, f"[server] player #7 joined: {OTHER} from 198.51.100.30:44330"),
        (280, "[server] player #7 (Luigi) left: timed out")))
    assert "no_joins" not in watchdog.findings("Alpha", NOW)
    assert [c.who for c in watchdog.connects["Alpha"]] == ["198.51.100.30"]  # his own connection: one try


def test_an_address_without_its_port() -> None:
    from huragok.servers.watchdog import _host
    assert _host("203.0.113.9:51234") == "203.0.113.9"
    assert _host("[2001:db8::1]:5000") == "2001:db8::1"
    assert _host("2001:db8::1") == "2001:db8::1"  # no port to take off
    assert _host("203.0.113.9") == "203.0.113.9" and _host("") == ""


# Names that come after the join -----------------------------------------------------------------------------------

def test_a_join_with_no_name_keeps_the_name_players_gave() -> None:
    bus = EventBus()
    tracker = StateTracker(bus, {})
    state = tracker.add("Alpha", SimpleNamespace(connected=False))  # type: ignore[arg-type]
    named: list[Event] = []
    bus.subscribe("state.named", named.append)
    tracker._apply_players(state, {"players": [{"number": 1, "name": "Bravo", "id": KEY}]})
    bus.emit("rcon.join", "Alpha", number=1, name="", id=KEY, ip="203.0.113.9")
    assert state.players[1].name == "Bravo" and not tracker._unnamed["Alpha"] and not named
    # Someone else under that number: their own (no) name.
    bus.emit("rcon.join", "Alpha", number=1, name="", id=OTHER, ip="203.0.113.8")
    assert state.players[1].name == "" and tracker._unnamed["Alpha"] == {OTHER}
    assert tracker._wait("Alpha") == NAMELESS_SECONDS  # asked again soon
    bus.emit("rcon.leave", "Alpha", number=1, name="", id=OTHER)
    assert not tracker._unnamed["Alpha"] and tracker._wait("Alpha") == tracker.refresh_seconds


class Console:
    """`status`, `players` and `admin` as a 0.10 server answers: names once they're proved."""

    connected = True

    def __init__(self) -> None:
        self.players: list[dict[str, Any]] = []
        self.asked = 0

    async def command(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "players":
            self.asked += 1
            return SimpleNamespace(ok=True, data={"players": [dict(p) for p in self.players], "max_players": 16})
        return SimpleNamespace(ok=True, data={})


def test_the_name_proved_in_the_log_is_asked_for_and_said_once(run) -> None:
    async def scenario() -> None:
        bus = EventBus()
        console = Console()
        tracker = StateTracker(bus, {"Alpha": console}, refresh_seconds=3600)  # type: ignore[dict-item]
        task = asyncio.ensure_future(tracker.run())
        named: list[Event] = []
        bus.subscribe("state.named", named.append)
        try:
            await asyncio.sleep(0)
            state = tracker.states["Alpha"]
            console.players.append({"number": 1, "name": "", "id": KEY})
            bus.emit("rcon.join", "Alpha", number=1, name="", id=KEY, ip="203.0.113.9")
            assert tracker._unnamed["Alpha"] == {KEY}
            console.players[0]["name"] = "Bravo"
            bus.emit("log.identified", "Alpha", handle="0x0123456789abcdef", name="Bravo")  # asks now
            await wait_for(lambda: named)
            assert state.players[1].name == "Bravo" and console.asked == 1
            assert [(e["number"], e["id"], e["name"]) for e in named] == [(1, KEY, "Bravo")]
            await tracker.refresh("Alpha")
            assert len(named) == 1  # once
            bus.emit("log.identified", "Alpha", handle="0x2", name="Zed")  # nobody waits: nothing asked
            await asyncio.sleep(0.05)
            assert console.asked == 2
        finally:
            task.cancel()

    run(scenario())


def test_vergil_records_a_join_once_its_name_is_known(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "huragok.db")
    try:
        state = ServerState("Alpha")
        vergil = SimpleNamespace(huragok=SimpleNamespace(storage=storage),
                                 superintendent=SimpleNamespace(states={"Alpha": state}), last_visit={}, unnamed={})
        storage.record_join(OTHER, "Alpha", "203.0.113.1", 1000.0)
        remember = Vergil._remember_player.__get__(vergil)
        name_it = Vergil._remember_name.__get__(vergil)
        remember(Event("rcon.join", "Alpha", {"number": 1, "name": "", "id": KEY, "ip": "203.0.113.9"}, 2000.0))
        assert storage.player(KEY) is None and vergil.last_visit[KEY] is None  # no blank name kept; first time
        name_it(Event("state.named", "Alpha", {"number": 1, "id": KEY, "name": "Bravo"}))
        record = storage.player(KEY)
        assert record is not None and record.last_name == "Bravo" and record.names == ["Bravo"]
        assert record.last_seen == 2000.0 and record.ips == ["203.0.113.9"]
        # Back again: when they were here before is known at the join, the name a moment later.
        remember(Event("rcon.join", "Alpha", {"number": 1, "name": "", "id": KEY, "ip": "203.0.113.9"}, 3000.0))
        assert vergil.last_visit[KEY] == 2000.0 and storage.player(KEY).visits == 1
        name_it(Event("rcon.leave", "Alpha", {"number": 1, "name": "Bravo", "id": KEY}))  # left before: named there
        assert storage.player(KEY).visits == 2 and storage.player(KEY).last_seen == 3000.0
        # A join the picture already has a name for is recorded with it; 0.9's, with theirs.
        state.players[2] = Player(2, "Alpha", OTHER)
        remember(Event("rcon.join", "Alpha", {"number": 2, "name": "", "id": OTHER, "ip": "203.0.113.1"}, 4000.0))
        assert vergil.last_visit[OTHER] == 1000.0 and storage.player(OTHER).visits == 2 and not vergil.unnamed
        remember(Event("rcon.join", "Alpha", {"number": 3, "name": "Zed", "id": "9" * 64, "ip": "203.0.113.3"}, 5000.0))
        assert storage.player("9" * 64).last_name == "Zed"
        # Left before anyone knew their name: nothing blank kept.
        remember(Event("rcon.join", "Alpha", {"number": 4, "name": "", "id": "8" * 64, "ip": "203.0.113.4"}, 6000.0))
        name_it(Event("rcon.leave", "Alpha", {"number": 4, "name": "", "id": "8" * 64}))
        assert storage.player("8" * 64) is None and not vergil.unnamed
    finally:
        storage.close()


def test_a_banned_name_known_only_after_the_join_is_kicked(tmp_path: Path, run) -> None:
    (tmp_path / "automod.txt").write_text("badword | | 1h | 3 | slurs\n", encoding="utf-8")

    async def scenario() -> None:
        async with running(tmp_path, "[automod]\nenabled = true\n") as (sup, fake, seen):
            # 0.10: the console's join names nobody; `players` has the name once they've proved who they are.
            fake.players.append({"number": 6, "name": "", "id": KEY, "ip": "203.0.113.6", "admin": False,
                                 "muted": False, "team": None})
            await fake.event("join", number=6, name="", id=KEY, ip="203.0.113.6", admin=False)
            await wait_for(lambda: 6 in sup.superintendent.states[SERVER].players)
            await asyncio.sleep(0.2)
            assert not fake.named("kick")
            fake.players[-1]["name"] = "xX BadWord Xx"
            sup.bus.emit("log.identified", SERVER, handle="0x0123456789abcdef", name="xX BadWord Xx")
            kick = await wait_for(lambda: fake.named("kick"))
            assert kick[0]["args"][0] == KEY and "(banned name)" in kick[0]["args"][1]
            assert [e["name"] for e in seen if e.name == "state.named"] == ["xX BadWord Xx"]
            record = sup.storage.player(KEY)
            assert record is not None and record.last_name == "xX BadWord Xx" and "" not in record.names

    run(scenario())


# Player IDs that are network keys ---------------------------------------------------------------------------------

def test_network_keys_pass_where_player_ids_do() -> None:
    assert live_id(KEY.upper()) == KEY
    state = ServerState("Alpha", players={1: Player(1, "", KEY), 2: Player(2, "Bravo", OTHER, service_tag="LG9")})
    assert _named(state, "") == [] and _named(state, "  ") == []  # nobody's name isn't a nameless player's
    assert _named(state, "Bravo (504f)") == [state.players[2]] and state.find(KEY) == [state.players[1]]
