"""Interval messages: intervalmessages.txt in Huragok's folder (every server) and in each server folder (its own),
one message per line, each said to everyone on the server again and again after its own time."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from conftest import ALICE, wait_for
from testkit.hostfiles import host, up_to_date
from testkit.running import SERVER, running, says
from testkit.screens import screen, template_root, text_of
from testkit.setup import ALPHA

from huragok.commandline import cli, firstrun
from huragok.players.messages import Messages
from huragok.players.messages.intervals import (
    FILE, MIN_SECONDS, IntervalMessage, MessageError, add_message, describe_every, message_line, overview, own_file,
    parse_text, read_messages, remove_message, seconds_every, template, write_template,
)
from huragok.rcon import RconError
from huragok.servers.state import Player, ServerState
from huragok.updates import hostfiles as migrate

MINUTE = 60


# The file ------------------------------------------------------------------------------------------------------

def test_a_line_is_how_often_then_the_message() -> None:
    messages, problems = parse_text("# a note\n\n30m | Join us on Discord: discord.gg/x\n  1h30m|Rules | read them  \n"
                                    "90s | Ninety seconds\n1H | capitals\n")
    assert problems == []
    assert [(m.every, m.seconds, m.text, m.line) for m in messages] == [
        ("30m", 1800, "Join us on Discord: discord.gg/x", 3), ("1h30m", 5400, "Rules | read them", 4),
        ("90s", 90, "Ninety seconds", 5), ("1h", 3600, "capitals", 6)]
    assert [seconds_every(t) for t in ("1m", "2h", "1d", "1 h 30 m")] == [60, 7200, 86400, 5400]
    assert describe_every(5400) == "1 hour 30 minutes" and describe_every(60) == "1 minute"
    assert describe_every(90061) == "1 day 1 hour 1 minute 1 second"


def test_a_line_that_cant_be_read_is_left_out_and_named() -> None:
    messages, problems = parse_text("30m | fine\nno bar here\n30m |\nsoon | what\n30s | too often\n0m | never\n")
    assert [m.text for m in messages] == ["fine"]
    assert problems == [
        "line 2 has no |: write it as how often | the message (30m | Join us on Discord)",
        "line 3 has no message after the |: write it as how often | the message",
        "line 4: 'soon' isn't a time: 30s, 10m, 1h, 1d, or put together, 1h30m",
        "line 5: every 30 seconds is too often: once a minute at most",
        "line 6: every 0 seconds is too often: once a minute at most"]
    assert MIN_SECONDS == 60


def test_the_files_as_huragok_writes_them_have_notes_and_no_messages(tmp_path: Path) -> None:
    for kind in ("every", "server"):
        assert parse_text(template(kind)) == ([], [])
        assert "how often | the message" in template(kind) and "Once a minute at most" in template(kind)
    path = tmp_path / FILE
    assert write_template(path) and not write_template(path, "server")  # one that's there is never touched
    assert "on every server" in path.read_text(encoding="utf-8")


def test_reading_a_file_as_windows_editors_save_it(tmp_path: Path) -> None:
    path = tmp_path / FILE
    assert read_messages(path) == ([], [])  # no file: no messages
    path.write_bytes("30m | Ça va? Join us\r\nbad\r\n".encode("utf-16"))  # Notepad's "Unicode"
    messages, problems = read_messages(path)
    assert [m.text for m in messages] == ["Ça va? Join us"] and problems[0].startswith(f"{FILE} line 2 has no |")
    path.write_bytes("30m | caf\xe9\n".encode("latin-1"))
    with pytest.raises(MessageError, match="isn't saved as UTF-8"):
        read_messages(path)


def test_messages_are_added_and_taken_off(tmp_path: Path) -> None:
    path = tmp_path / "SERVERS" / ALPHA / FILE
    path.parent.mkdir(parents=True)
    added = add_message(path, "45M", "  Bring   friends  ", "server")  # no file yet: written with its notes first
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# This server's own interval messages") and text.endswith("\n45m | Bring friends\n")
    assert (added.every, added.seconds, added.text) == ("45m", 2700, "Bring friends")
    second = add_message(path, "1h", "Rules")
    messages, _ = read_messages(path)
    assert messages == [added, second]
    with pytest.raises(MessageError, match="too often"):
        add_message(path, "10s", "spam")
    with pytest.raises(MessageError, match="no message"):
        message_line("5m", "   ")
    assert remove_message(path, added)
    assert [m.text for m in read_messages(path)[0]] == ["Rules"]
    assert not remove_message(path, added)  # gone already: its line is something else now
    assert not remove_message(path, IntervalMessage("1h", 3600, "Rules", 1))  # line 1 is a note
    assert not remove_message(tmp_path / "missing.txt", second)


def test_a_server_whose_folder_is_huragoks_has_no_file_of_its_own(tmp_path: Path) -> None:
    assert own_file(tmp_path / "server", tmp_path) == tmp_path / "server" / FILE
    assert own_file(tmp_path, tmp_path) is None and own_file(None, tmp_path) is None


# Saying them ------------------------------------------------------------------------------------------------------

class FakeContext:
    """What the part uses of Huragok: the servers (each with a folder), and say."""

    def __init__(self, home: Path, *servers: str) -> None:
        self.home = home
        self.config: dict[str, Any] = {}
        self.log = logging.getLogger("part.messages")
        self.folders = {name: home / "SERVERS" / name for name in servers}
        for folder in self.folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        self.servers = {name: ServerState(name, connected=True) for name in servers}
        self.said: list[tuple[str, str]] = []
        self.broken: set[str] = set()

    def server_option(self, server: str, key: str) -> Any:
        return self.folders.get(server) if key == "folder" else None

    async def say(self, server: str, text: str) -> None:
        if server in self.broken:
            raise RconError("no answer")
        self.said.append((server, text))

    def on(self, server: str, *names: str) -> None:
        self.servers[server].players = {n: Player(n, name, str(n) * 64) for n, name in enumerate(names, 1)}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def made(ctx: FakeContext) -> tuple[Messages, Clock]:
    part = Messages(ctx)  # type: ignore[arg-type]
    part.check_config()
    clock = Clock()
    part.clock = clock
    await part.setup()
    return part, clock


async def at(part: Messages, clock: Clock, minutes: float) -> None:
    clock.now = 1000.0 + minutes * MINUTE
    await part.tick()


def write(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def said(ctx: FakeContext) -> list[str]:
    texts = [text for _, text in ctx.said]
    ctx.said.clear()
    return texts


def test_messages_with_the_same_time_take_turns(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha")
        write(tmp_path / FILE, "30m | Discord", "30m | Rules", "1h | Vote")
        ctx.on("Alpha", "Alice")
        part, clock = await made(ctx)
        await at(part, clock, 0)  # the clocks start: nothing yet
        assert said(ctx) == []
        await at(part, clock, 14.9)
        assert said(ctx) == []
        await at(part, clock, 15)
        assert said(ctx) == ["Discord"]
        await at(part, clock, 30)
        assert said(ctx) == ["Rules"]
        await at(part, clock, 45)
        assert said(ctx) == ["Discord"]
        await at(part, clock, 60)
        assert said(ctx) == ["Rules", "Vote"]  # due together: said in the file's order

    run(scenario())


def test_one_due_while_nobody_is_on_or_the_server_is_down_waits_for_its_next_time(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha")
        write(tmp_path / FILE, "10m | Discord")
        part, clock = await made(ctx)
        await at(part, clock, 0)
        await at(part, clock, 10)  # nobody on
        ctx.on("Alpha", "Alice")
        await at(part, clock, 11)
        assert said(ctx) == []  # not saved up for whoever joins
        await at(part, clock, 20)
        assert said(ctx) == ["Discord"]
        ctx.servers["Alpha"].connected = False  # restarting
        await at(part, clock, 30)
        ctx.servers["Alpha"].connected = True
        await at(part, clock, 31)
        assert said(ctx) == []
        await at(part, clock, 75)  # Huragok busy for a long while: once, not once for each time missed
        assert said(ctx) == ["Discord"]
        await at(part, clock, 79)
        assert said(ctx) == []
        await at(part, clock, 80)  # still on its times: 10, 20 … 80
        assert said(ctx) == ["Discord"]

    run(scenario())


def test_a_servers_clocks_start_when_it_is_first_seen_up(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha")
        write(tmp_path / FILE, "10m | Discord")
        ctx.on("Alpha", "Alice")
        ctx.servers["Alpha"].connected = False
        part, clock = await made(ctx)
        await at(part, clock, 0)
        await at(part, clock, 25)  # up now
        ctx.servers["Alpha"].connected = True
        await at(part, clock, 26)
        await at(part, clock, 35)
        assert said(ctx) == []
        await at(part, clock, 36)
        assert said(ctx) == ["Discord"]

    run(scenario())


def test_each_server_says_the_ones_for_every_server_and_its_own(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha", "Bravo")
        write(tmp_path / FILE, "30m | Discord")
        write(ctx.folders["Alpha"] / FILE, "30m | Alpha plays BTB all night")
        ctx.on("Alpha", "Alice")
        ctx.on("Bravo", "Bob")
        part, clock = await made(ctx)
        await at(part, clock, 0)
        await at(part, clock, 15)
        assert ctx.said == [("Alpha", "Discord")]  # the two take turns on Alpha
        ctx.said.clear()
        await at(part, clock, 30)
        assert ctx.said == [("Alpha", "Alpha plays BTB all night"), ("Bravo", "Discord")]

    run(scenario())


def test_edits_apply_and_a_message_that_was_there_keeps_its_time(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha")
        path = tmp_path / FILE
        write(path, "30m | Discord")
        ctx.on("Alpha", "Alice")
        part, clock = await made(ctx)
        assert "interval messages: 1 for every server" in caplog.text
        await at(part, clock, 0)
        await at(part, clock, 10)
        write(path, "# a note above it now", "30m | Discord", "20m | Vote for the next map")
        part.files[path].mtime = -1  # as if saved again (a file's time may not move within a test)
        await at(part, clock, 11)
        assert f"{FILE} reloaded: 2 interval messages" in caplog.text
        await at(part, clock, 30)
        assert said(ctx) == ["Discord"]  # still at 30: the edit didn't start its clock again
        await at(part, clock, 31)
        assert said(ctx) == ["Vote for the next map"]  # a new one's clock started as it was added (11 + 20)
        write(path, "this line has no bar", "15m | Discord")
        part.files[path].mtime = -1
        await at(part, clock, 32)
        assert f"{FILE} line 1 has no |" in caplog.text and "(left out; the others are said)" in caplog.text
        await at(part, clock, 47)
        assert said(ctx) == ["Discord"]  # its time changed: a new message, from when it changed

    run(scenario())


def test_a_long_message_goes_out_as_lines_and_a_server_not_answering_waits(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha", "Bravo")
        long = " ".join(["word"] * 50)  # 249 characters
        write(tmp_path / FILE, f"10m | {long}")
        ctx.on("Alpha", "Alice")
        ctx.on("Bravo", "Bob")
        ctx.broken.add("Alpha")
        part, clock = await made(ctx)
        await at(part, clock, 0)
        await at(part, clock, 10)
        assert [server for server, _ in ctx.said] == ["Bravo", "Bravo", "Bravo"]
        assert " ".join(text for _, text in ctx.said) == long and all(len(t) <= 120 for _, t in ctx.said)
        assert "[Alpha] couldn't say an interval message (every 10 minutes): no answer" in caplog.text

    run(scenario())


def test_a_file_that_cant_be_read_keeps_what_it_had(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        ctx = FakeContext(tmp_path, "Alpha")
        path = ctx.folders["Alpha"] / FILE
        write(path, "10m | Alpha's own")
        ctx.on("Alpha", "Alice")
        part, clock = await made(ctx)
        await at(part, clock, 0)
        path.write_bytes("10m | caf\xe9\n".encode("latin-1"))
        part.files[path].mtime = -1
        await at(part, clock, 10)
        assert said(ctx) == ["Alpha's own"]
        assert f"[Alpha] {FILE} not read, keeping its messages as they were" in caplog.text
        assert "save it as UTF-8" in caplog.text
        path.unlink()
        await at(part, clock, 11)
        assert f"[Alpha] {FILE} reloaded: 0 interval messages" in caplog.text
        await at(part, clock, 20)
        assert said(ctx) == []

    run(scenario())


def test_huragok_says_them_on_a_running_server(tmp_path: Path, run) -> None:
    write(tmp_path / FILE, "1m | Join us on Discord")
    (tmp_path / "server").mkdir()
    write(tmp_path / "server" / FILE, "1m | This server's own")

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            part = sup.runners["messages"].part
            clock = Clock()
            part.clock = clock
            part.plans.clear()  # in case its own timer went first, on the real clock
            await fake.join(1, "Alice", ALICE)
            await wait_for(lambda: sup.superintendent.states[SERVER].players)
            await part.tick()
            assert says(fake) == []
            clock.now += 31
            await part.tick()
            clock.now += 30
            await part.tick()
            assert says(fake) == ["Join us on Discord", "This server's own"]
            assert sup.runners["messages"].enabled

    run(scenario())


# Brought in for hosts, init and new servers, and `check` ---------------------------------------------------------

def test_a_host_from_before_gets_the_files_once_and_keeps_its_own(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    root = settings_path.parent
    for name in ("Alpha", "Bravo", "_set aside"):
        (root / "SERVERS" / name).mkdir()
        (root / "SERVERS" / name / "dedicated.toml").write_text("", encoding="utf-8")
    (root / "SERVERS" / "not a server").mkdir()
    write(root / "SERVERS" / "Bravo" / FILE, "1h | Bravo's own")
    pending = migrate.pending(settings_path, root / "records", root / "PACKAGES")
    assert f"{FILE}: interval messages (for every server), written with how to set them up; none yet" in pending
    assert f"Alpha/{FILE}: interval messages (its own), written with how to set them up; none yet" in pending
    changed = up_to_date(settings_path)
    assert [line for line in changed if FILE in line] == [
        f"{FILE}: interval messages (for every server), written with how to set them up; none yet",
        f"Alpha/{FILE}: interval messages (its own), written with how to set them up; none yet"]
    assert (root / FILE).read_text(encoding="utf-8") == template("every")
    assert (root / "SERVERS" / "Alpha" / FILE).read_text(encoding="utf-8") == template("server")
    assert (root / "SERVERS" / "Bravo" / FILE).read_text(encoding="utf-8") == "1h | Bravo's own\n"
    assert not (root / "SERVERS" / "_set aside" / FILE).exists() and not (root / "SERVERS" / "not a server" / FILE).exists()
    (root / FILE).unlink()  # the host took it out: it stays out
    assert [line for line in up_to_date(settings_path) if FILE in line] == []
    assert not (root / FILE).exists()


def test_check_lists_the_messages_and_the_lines_left_out(tmp_path: Path, capsys) -> None:
    path = template_root(tmp_path)
    root = path.parent
    write(root / FILE, "30m | Discord", "1h | Rules", "oops")
    write(root / "SERVERS" / ALPHA / FILE, "45m | Alpha's own", "5s | too soon")
    cli.main(["-c", str(path), "check"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert "Interval messages" in text and "2: every 30 minutes, every 1 hour" in text
    assert "1: every 45 minutes" in text
    assert f"{FILE} line 3 has no |" in text and f"{ALPHA}: {FILE} line 2: every 5 seconds is too often" in text
    rows, problems = overview(root, [(ALPHA, root / "SERVERS" / ALPHA), ("Bravo", root / "SERVERS" / "Bravo")])
    assert rows == [("every server", "2: every 30 minutes, every 1 hour"), (ALPHA, "1: every 45 minutes"),
                    ("Bravo", "none of its own")]
    assert len(problems) == 2


def test_init_writes_the_one_for_every_server(tmp_path: Path) -> None:
    written = firstrun.write_files(tmp_path / "root", backend="attach")
    assert any(line.startswith(f"{FILE}  interval messages every server says") for line in written)
    assert (tmp_path / "root" / FILE).read_text(encoding="utf-8") == template("every")


# The window: D → I, and a server's page → I ---------------------------------------------------------------------

def test_d_adds_and_takes_off_interval_messages(tmp_path: Path, run) -> None:
    path = template_root(tmp_path)
    root = path.parent
    keys = ["I", "ENTER",
            "A", "ENTER", "4", *"Join us on Discord: discord.gg/x", "ENTER",  # every 30 minutes
            "A", "ENTER", "T", *"1h30m", "ENTER", *"Rules: be nice", "ENTER",
            "A", "ENTER", "T", *"10s", "ENTER", "x",  # too often: refused before the message is asked for
            "1", "ENTER", "Y",  # Discord taken off
            "ESC",
            "1", "ENTER", "I", "ENTER",  # Alpha's page: its own
            "A", "ENTER", "6", *"Alpha only", "ENTER",
            "ESC", "ESC", "ESC"]
    menu, terminal = screen(path, keys)
    run(menu.screen_settings())
    text = text_of(terminal)
    assert [(m.every, m.text) for m in read_messages(root / FILE)[0]] == [("1h30m", "Rules: be nice")]
    assert [(m.every, m.text) for m in read_messages(root / "SERVERS" / ALPHA / FILE)[0]] == [("1h", "Alpha only")]
    assert (root / "SERVERS" / ALPHA / FILE).read_text(encoding="utf-8").startswith("# This server's own")
    assert "Added: every 30 minutes. It's first said within 30 minutes, while someone's on." in text
    assert "Not changed: every 10 seconds is too often" in text and "Taken off: it isn't said any more." in text
    assert "It says the ones for every server too (I in D's list): 1: every 1 hour 30 minutes." in text
    assert "Interval messages" in text and "its own; it says the ones for every server too" in text


def test_d_and_o_show_a_file_with_lines_left_out(tmp_path: Path, run, monkeypatch) -> None:
    from huragok.core import desktop

    path = template_root(tmp_path)
    write(path.parent / FILE, "30m | Discord", "oops")
    menu, terminal = screen(path, ["I", "ENTER", "ESC", "ESC"])
    run(menu.screen_settings())
    text = text_of(terminal)
    assert "1: every 30 minutes · 1 line left out" in text
    assert f"{FILE} line 2 has no |" in text and "left out, O opens the file to put it right" in text
    opened: list[Path] = []
    monkeypatch.setattr(desktop, "open_editor", lambda p: opened.append(p) or f"opened {p.name}")
    write(path.parent / "SERVERS" / ALPHA / FILE, "1h | Alpha's own")
    for keys in (["6", "ENTER", "x"], ["7", "ENTER", "3", "ENTER", "x"]):
        menu, terminal = screen(path, keys)
        run(menu.screen_open())
    assert opened == [path.parent / FILE, path.parent / "SERVERS" / ALPHA / FILE]
