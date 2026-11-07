"""Project Reclaimer 0.10 in Discord: a join names nobody (the name comes a few seconds later, as state.named), and the
server's log says no join address (its status.json does).

Join cards and the watch wait for the name, then go as 0.9's did at the join; never twice. One who leaves before
anyone knew it gets their join's cards then, by the name the leave gives or they were last seen under, else their short
ID. One whose name never comes, after NAME_WAIT_SECONDS, the same way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from conftest import MOD
from testkit.fake_discord import GUILD, FACULTY_USER, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.core.events import Event
from huragok.core.storage import PlayerRecord
from huragok.discordbot.config import add_faculty, load, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.discordbot.feeds import EVENTS, NAME_WAIT_SECONDS, Feeds
from huragok.discordbot.part import DiscordPart
from huragok.servers.state import Player, ServerState
from huragok.superintendent import Superintendent

KEY = "0a1b2c3d" + "0" * 52 + "9e8f"  # a network key: 0.10's player ID
SHORT = "`0a1b2c3d…`"
LOGS = "4009"


class Part:
    """As much of the part as the feeds use for joins: what's posted is kept."""

    def __init__(self, path: Path) -> None:
        self.file = load(path)
        self.facts = None
        self.real_names: dict[str, str] = {}
        self.status = SimpleNamespace(stored=lambda _key: ("", ""), forget=lambda _key: None, store=lambda *_a, **_k: None)
        self.posts: list[dict[str, Any]] = []
        self.poster = SimpleNamespace(post=lambda persona, form, **where: self.posts.append({**form, **where}))
        self.live: dict[tuple[str, str], str] = {}  # (server, ID) -> the name the live picture has now
        self.last: dict[str, str] = {}  # ID -> the name they were last seen under
        self.visits: dict[str, float | None] = {}
        self.staff: list[tuple[str, str]] = []
        self.banned: list[str] = []
        self.players: list[Any] = [object()]

    def name_now(self, server: str, player_id: str, *, or_last: bool = False) -> str:
        return self.live.get((server, player_id.lower()), "") or (self.last.get(player_id.lower(), "") if or_last else "")

    def name_of(self, player_id: str, given: str = "") -> str:
        return given or self.last.get(player_id, "") or player_id[:8]

    def players_on(self, server: str) -> list[Any]:
        return self.players

    def last_visit(self, player_id: str) -> float | None:
        return self.visits.get(player_id)

    def old_name(self, player_id: str, name: str, when: float) -> str:
        return ""

    def kicked_lately(self, player_id: str, seconds: float, now: float) -> Any:
        return None

    def banned_elsewhere(self, player_id: str, server: str) -> list[str]:
        return self.banned

    def staff_names(self) -> list[tuple[str, str]]:
        return self.staff

    def flags_of(self, player_id: str) -> list[Any]:
        return []


def feeds_for(tmp_path: Path) -> tuple[Feeds, Part]:
    """Joins (left too) and the watch on, one post each, every card in a channel known by its ID."""
    path = tmp_path / "discord.toml"
    path.write_text(f'mode = "bot"\n[channels]\nstaff = "{STAFF_CHANNEL}"\nlogs = "{LOGS}"\ngames = "{PUBLIC_CHANNEL}"\n'
                    "[feed.joins]\nenabled = true\nmerge_seconds = 0\n[feed.watch]\nenabled = true\n"
                    '[messages]\njoins-left = { post = true }\nwatch-first-time-quiet = { channel = "logs" }\n', encoding="utf-8")
    part = Part(path)
    feeds = Feeds(part)  # type: ignore[arg-type]
    part.tickets = SimpleNamespace(open=lambda card, options: feeds._send(card, {**options, "also": []}, one_line=False),
                                   take=lambda event: False, of_player=lambda player_id, leaving=0: None)
    return feeds, part


def said(part: Part) -> list[str]:
    return [str(post.get("content") or "") or str(post["embeds"][0].get("description")) for post in part.posts]


def joined(name: str = "", when: float | None = None) -> Event:
    data = {"number": 2, "name": name, "id": KEY, "ip": "203.0.113.9", "admin": False}
    return Event("rcon.join", "Alpha", data) if when is None else Event("rcon.join", "Alpha", data, when)


def named(name: str = "Bravo") -> Event:
    return Event("state.named", "Alpha", {"number": 2, "id": KEY, "name": name})


# The feeds ------------------------------------------------------------------------------------------------------------

def test_a_nameless_join_is_carded_once_its_name_comes_and_only_once(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    feeds.handle(joined())
    assert part.posts == [] and ("Alpha", KEY) in feeds.unnamed  # nothing with no name in it
    feeds.handle(named())
    assert said(part) == ["Alpha · **Bravo** joined for the first time", "Alpha · **Bravo** is new here, with only 1 on"]
    assert SHORT not in str(part.posts) and not feeds.unnamed
    feeds.handle(named())  # said again (another refresh): posted already
    assert len(part.posts) == 2


def test_a_name_the_live_picture_has_already_goes_at_once(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    part.live[("Alpha", KEY)] = "Bravo"  # `players` named them before the part got to the join
    feeds.handle(joined())
    assert said(part)[0] == "Alpha · **Bravo** joined for the first time" and not feeds.unnamed
    feeds.handle(named())  # nothing waited for it
    assert len(part.posts) == 2


def test_a_staff_lookalike_is_caught_by_the_name_that_came_later(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    part.staff = [(MOD, "Moderator")]
    part.visits[KEY] = 1.0  # been before: no "new here", and their join isn't posted by default
    feeds.handle(joined())
    assert part.posts == []
    feeds.handle(named("Moderat0r"))
    assert said(part) == ["**Moderat0r** has a name like Moderator's, who is staff"]


def test_09s_join_with_its_name_is_as_it_was(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    feeds.handle(joined("Bravo"))
    assert said(part)[0] == "Alpha · **Bravo** joined for the first time" and not feeds.unnamed


def test_gone_before_the_name_came_their_join_goes_then_by_what_is_known(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    part.banned = ["Elsewhere"]
    # The leave names them: their join's cards first, by that name, and no "new here" for someone gone.
    feeds.handle(joined())
    feeds.handle(Event("rcon.leave", "Alpha", {"number": 2, "name": "Bravo", "id": KEY}))
    assert said(part) == ["Alpha · **Bravo** joined for the first time", "**Bravo** joined, and is banned on Elsewhere",
                          "Alpha · **Bravo** left"]
    # It doesn't, but they've been here before: the name they were last seen under.
    part.posts.clear()
    part.last[KEY] = "Charlie"
    feeds.handle(joined())
    feeds.handle(Event("rcon.leave", "Alpha", {"number": 2, "name": "", "id": KEY}))
    assert said(part) == ["Alpha · **Charlie** joined for the first time", "**Charlie** joined, and is banned on Elsewhere",
                          "Alpha · **Charlie** left"]
    # Nobody knows them: their short ID, as any card does.
    part.posts.clear()
    part.last.clear()
    feeds.handle(joined())
    feeds.handle(Event("rcon.leave", "Alpha", {"number": 2, "name": "", "id": KEY}))
    assert said(part) == [f"Alpha · {SHORT} joined for the first time", f"{SHORT} joined, and is banned on Elsewhere",
                          f"Alpha · {SHORT} left"]
    assert not feeds.unnamed


def test_a_name_that_never_comes_is_waited_for_a_minute(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    now = 1_000_000.0
    feeds.clock = lambda: now
    feeds.handle(joined(when=now))
    now += NAME_WAIT_SECONDS - 1
    feeds.flush()
    assert part.posts == []
    now += 1
    feeds.flush()
    assert said(part) == [f"Alpha · {SHORT} joined for the first time", f"Alpha · {SHORT} is new here, with only 1 on"]
    feeds.handle(named())  # too late: posted already
    assert len(part.posts) == 2 and not feeds.unnamed


def test_a_refusal_or_leave_with_no_name_goes_by_the_last_one(tmp_path: Path) -> None:
    feeds, part = feeds_for(tmp_path)
    part.last[KEY] = "Charlie"
    feeds.handle(Event("rcon.refused", "Alpha", {"id": KEY, "name": "", "reason": "banned"}))
    feeds.handle(Event("rcon.leave", "Alpha", {"number": 3, "name": "", "id": KEY}))
    assert said(part) == ["Alpha · **Charlie** was turned away: banned", "Alpha · **Charlie** left"]


def test_the_part_hears_state_named() -> None:
    assert "state.named" in EVENTS


# The part: the name a player goes by now --------------------------------------------------------------------------

def test_the_name_now_is_the_live_pictures_else_the_last_seen() -> None:
    state = ServerState("Alpha", players={2: Player(2, "", KEY), 3: Player(3, "Delta", "e" * 64)})
    records = {KEY: PlayerRecord(KEY, 1.0, 2.0, "Charlie", 1, ["Charlie"], [])}
    part = SimpleNamespace(ctx=SimpleNamespace(servers={"Alpha": state}, players=SimpleNamespace(get=records.get)))
    name_now = DiscordPart.name_now.__get__(part)
    assert name_now("Alpha", KEY) == ""  # on, with no name yet
    assert name_now("Alpha", KEY, or_last=True) == "Charlie"
    assert name_now("Alpha", "E" * 64) == "Delta" and name_now("Beta", "e" * 64) == ""
    state.players[2].name = "Bravo"
    assert name_now("Alpha", KEY) == "Bravo" and name_now("Alpha", "f" * 64, or_last=True) == ""


# The join address ---------------------------------------------------------------------------------------------------

def test_010s_join_address_comes_from_status_json(tmp_path: Path) -> None:
    (tmp_path / "status.json").write_text('{"name": "Probe Reach", "address": "127.0.0.1:46991", "players": []}', encoding="utf-8")
    state = ServerState("Alpha")
    watcher = SimpleNamespace(server=SimpleNamespace(status_path=tmp_path / "status.json"))
    superintendent = SimpleNamespace(states={"Alpha": state}, watchers={"Alpha": watcher})
    note = Superintendent._note_address.__get__(superintendent)
    note(Event("log.online", "Alpha", {}))  # 0.10's "[online] taking joins"
    assert state.address == "127.0.0.1:46991"
    # 0.9's log says it: as it says.
    note(Event("log.online", "Alpha", {"address": "203.0.113.7:49176"}))
    assert state.address == "203.0.113.7:49176"
    # The console signing in reads the file again (a file from the run before, a server picked up already running).
    note(Event("rcon.connected", "Alpha", {"version": "0.10.0"}))
    assert state.address == "127.0.0.1:46991"
    # No file, or none in it: what was known stays.
    (tmp_path / "status.json").write_text('{"name": "Probe Reach"}', encoding="utf-8")
    note(Event("rcon.connected", "Alpha", {}))
    (tmp_path / "status.json").unlink()
    note(Event("log.online", "Alpha", {}))
    note(Event("log.online", "Nobody", {}))
    assert state.address == "127.0.0.1:46991"


# The whole way: a 0.10 join, its name proved in the log, on the fake Discord -----------------------------------------

@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_faculty(path, FACULTY_USER, "the host")
    for name in ("staff", "info", "logs"):
        set_channel(path, name, STAFF_CHANNEL, "admin-log")
    set_switch(path, ("feed", "watch"), "enabled", True)
    return path


def everything_said(fake: FakeDiscord) -> str:
    cards = [str(card.get("description")) for post in fake.posted for card in post.get("embeds", [])]
    return "\n".join([*cards, *(str(post["content"]) for post in fake.posted if post.get("content"))])


def test_a_010_join_is_carded_by_the_name_proved_after_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = huragok.runners["discord"].part
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            await rcon.join(1, "Moderator", MOD)  # staff, known by that name
            await wait_for(lambda: huragok.storage.player(MOD) is not None, timeout=10)
            rcon.players.append({"number": 2, "name": "", "id": KEY, "ip": "203.0.113.2", "admin": False, "muted": False, "team": None})
            await rcon.event("join", number=2, name="", id=KEY, ip="203.0.113.2", admin=False)
            await wait_for(lambda: 2 in huragok.superintendent.states[SERVER].players, timeout=10)
            await wait_for(lambda: (SERVER, KEY) in part.feeds.unnamed, timeout=10)
            assert SHORT not in everything_said(fake)
            rcon.players[-1]["name"] = "Moderat0r"
            huragok.bus.emit("log.identified", SERVER, handle="0x0123456789abcdef", name="Moderat0r")
            await wait_for(lambda: "**Moderat0r** has a name like Moderator's" in everything_said(fake), timeout=15)
            await wait_for(lambda: "**Moderat0r** is new here" in everything_said(fake), timeout=15)
            await asyncio.sleep(0.2)
            assert SHORT not in everything_said(fake) and not part.feeds.unnamed

    run(scenario())
