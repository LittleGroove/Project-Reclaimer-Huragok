"""/server: a server's power buttons, for whoever may use them, seen by them alone, each asking first and on the record."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from test_bot_running import OTHER, _components, answer_text
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, OWNER_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.discordbot.config import add_owner, make_file, set_channel, set_guild, set_mode, set_switch, set_token


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
    add_owner(path, OWNER_USER, "the host")
    set_channel(path, "logs", STAFF_CHANNEL, "logs")
    set_channel(path, "alerts", STAFF_CHANNEL, "alerts")
    set_switch(path, ("commands",), "enabled", True)
    set_switch(path, ("feed", "audit"), "enabled", True)
    set_switch(path, ("feed", "alerts"), "enabled", True)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


EVERYTHING = {"start": True, "stop": True, "restart": True, "cancel": True, "talk": True}


class Manager:
    """As much of a server's manager as the panel uses: what it was asked is kept."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict[str, Any]]] = []
        self.ok = True

    def _result(self, name: str, **details: Any) -> SimpleNamespace:
        self.asked.append((name, details))
        return SimpleNamespace(ok=self.ok, message=f"{name} done" if self.ok else "it's busy")

    def start(self) -> SimpleNamespace:
        return self._result("start")

    def stop(self, **details: Any) -> SimpleNamespace:
        return self._result("stop", **details)

    def restart(self, **details: Any) -> SimpleNamespace:
        return self._result("restart", **details)

    def cancel(self) -> SimpleNamespace:
        return self._result("cancel")


async def up(huragok: Any, manager: Manager | None = None) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    if manager is not None:
        part.ctx.server_manager = lambda name: manager if name == SERVER else None
        real = part.status.rows
        part.status.rows = lambda: [{**row, "phase": "running", "can": dict(EVERYTHING)} for row in real()]  # whichever button a test wants is one it can press
    return part


async def panel_of(fake: FakeDiscord, opened: str, token: str) -> dict[str, Any]:
    await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=15)
    return next(c for c in reversed(fake.callbacks) if c.get("token") == token)["data"]


def buttons(form: dict[str, Any]) -> dict[str, str]:
    return {item["label"]: item["custom_id"] for item in _components(form) if item.get("label")}


async def press(fake: FakeDiscord, opened: str, custom_id: str, **who: Any) -> dict[str, Any]:
    before = len(fake.callbacks)
    fake.pick(opened, custom_id, 2, [], **who)
    await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
    return fake.callbacks[-1]["data"]


async def answered_after_yes(fake: FakeDiscord, opened: str, ask: dict[str, Any]) -> dict[str, Any]:
    """Yes takes as long as it takes, so the answer is held open first, and it comes as an edit."""
    before = len(fake.original_edits)
    fake.pick(opened, buttons(ask)["Yes"], 2, [])
    await wait_for(lambda: len(fake.original_edits) > before, timeout=15)
    return fake.original_edits[-1]


def test_the_panel_is_private_and_has_the_power_buttons(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, Manager())
            opened, token = fake.command("server")
            form = await panel_of(fake, opened, token)
            assert form["flags"] == 64
            assert list(buttons(form)) == ["Start", "Stop", "Restart", "Restart after the game", "Restart when empty", "Cancel what's planned", "Check for updates", "Refresh"]

    run(scenario())


@pytest.mark.parametrize(("label", "called", "details"), [("Restart", "restart", {"when": "now"}), ("Restart after the game", "restart", {"when": "after_game"}),
                                                          ("Restart when empty", "restart", {"when": "when_empty"}), ("Stop", "stop", {}), ("Start", "start", {}), ("Cancel what's planned", "cancel", {})])
def test_each_asks_first_then_does_it_and_its_on_the_record(tmp_path: Path, run: Any, fake: FakeDiscord, label: str, called: str, details: dict[str, Any]) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        manager = Manager()
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, manager)
            opened, token = fake.command("server")
            form = await panel_of(fake, opened, token)
            ask = await press(fake, opened, buttons(form)[label])
            assert manager.asked == [] and "?" in ask["embeds"][0]["description"]  # it asks first
            done = await answered_after_yes(fake, opened, ask)
            assert manager.asked[0][0] == called and all(manager.asked[0][1][key] == value for key, value in details.items())
            assert "done" in str(done["embeds"][0])
            await wait_for(lambda: any("asked for" in str(post.get("content")) for post in fake.posted), timeout=15)

    run(scenario())


def test_saying_no_does_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        manager = Manager()
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, manager)
            opened, token = fake.command("server")
            ask = await press(fake, opened, buttons(await panel_of(fake, opened, token))["Stop"])
            back = await press(fake, opened, buttons(ask)["No"])
            assert manager.asked == [] and "Stop" in buttons(back)

    run(scenario())


def test_a_refusal_says_so_and_isnt_on_the_record(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        manager = Manager()
        manager.ok = False
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, manager)
            opened, token = fake.command("server")
            ask = await press(fake, opened, buttons(await panel_of(fake, opened, token))["Restart"])
            done = await answered_after_yes(fake, opened, ask)
            assert "Couldn't: it's busy" in str(done["embeds"][0])
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert not any("asked for" in str(post.get("content")) for post in fake.posted)

    run(scenario())


def test_only_the_rank_set_can(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, Manager())
            opened, token = fake.command("server", user=OTHER, roles=(MOD_ROLE,))
            form = await panel_of(fake, opened, token)  # they can see why it's down, and tell players: the buttons are for the rank that's set
            assert "Stop" not in buttons(form) and "Refresh" in buttons(form)
            opened, token = fake.command("server", user=OTHER, roles=(ADMIN_ROLE,))
            assert "Stop" in buttons(await panel_of(fake, opened, token))

    run(scenario())


def test_an_alert_has_a_way_to_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, Manager())
            huragok.bus.emit("server.crashed", SERVER, why="exit code 3")
            await wait_for(lambda: fake.posted, timeout=15)
            post = fake.posted[0]
            assert {item["label"]: item["custom_id"] for row in post["components"] for item in row["components"]}["Server"].endswith(":server")
            custom_id = next(item["custom_id"] for row in post["components"] for item in row["components"] if item["label"] == "Server")
            _, token = fake.press(custom_id, post, user=OTHER, roles=(MOD_ROLE,))
            form = await panel_of(fake, "", token)
            assert "Restart" not in buttons(form) and "Refresh" in buttons(form)  # a moderator can see why it's down, not restart it
            _, token = fake.press(custom_id, post)
            form = await panel_of(fake, "", token)
            assert form["flags"] == 64 and "Restart" in buttons(form)  # seen by them alone

    run(scenario())


def test_status_is_for_anyone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            _, token = fake.command("status", user=OTHER)
            await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=15)
            form = next(c for c in fake.callbacks if c.get("token") == token)["data"]
            assert form["flags"] == 64 and SERVER in str(form["embeds"][0])

    run(scenario())


@pytest.mark.parametrize(("phase", "can", "wanted"), [("idle", {"start": True, "restart": True}, ["Start", "Restart", "Check for updates", "Refresh"]),
                                                      ("running", {"stop": True, "restart": True}, ["Stop", "Restart", "Restart after the game", "Restart when empty", "Check for updates", "Refresh"]),
                                                      ("countdown", {"stop": True, "restart": True, "cancel": True}, ["Stop", "Restart", "Cancel what's planned", "Check for updates", "Refresh"]),
                                                      ("external", {}, ["Check for updates", "Refresh"])])
def test_the_buttons_are_what_can_be_done_now(tmp_path: Path, run: Any, fake: FakeDiscord, phase: str, can: dict[str, bool], wanted: list[str]) -> None:
    """Start on a running server only ever said "already running", and went on the record as if it had done something."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, Manager())
            real = part.ctx.server_rows
            part.status.rows = lambda: [{**row, "phase": phase, "can": can} for row in real()]
            opened, token = fake.command("server")
            assert list(buttons(await panel_of(fake, opened, token))) == wanted

    run(scenario())


def test_the_card_says_whats_going_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, Manager())
            real = part.ctx.server_rows
            part.status.rows = lambda: [{**row, "phase": "faulted", "status": "Faulted", "fault_reason": "it keeps crashing", "retry_at": 2000000000.0, "planned": "", "can": {"start": True}} for row in real()]
            opened, token = fake.command("server")
            shown = str((await panel_of(fake, opened, token))["embeds"][0]["fields"])
            assert "Faulted" in shown and "it keeps crashing" in shown and "<t:2000000000:R>" in shown

    run(scenario())


def test_a_press_after_things_have_moved_on_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        manager = Manager()
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, manager)
            opened, token = fake.command("server")
            form = await panel_of(fake, opened, token)
            real = part.ctx.server_rows
            part.status.rows = lambda: [{**row, "phase": "idle", "can": {"start": True}} for row in real()]
            shown = await press(fake, opened, buttons(form)["Stop"])
            assert "doesn't apply now" in str(shown["embeds"][0]) and list(buttons(shown))[0] == "Start"
            assert manager.asked == []

    run(scenario())


def test_checking_for_updates_doesnt_ask_first(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, Manager())
            asked: list[str] = []

            async def request(name: str, _payload: Any) -> SimpleNamespace:
                asked.append(name)
                return SimpleNamespace(ok=True, message=f"{name} says no")

            part.ctx.request = request
            opened, token = fake.command("server")
            form = await panel_of(fake, opened, token)
            before = len(fake.original_edits)
            fake.pick(opened, buttons(form)["Check for updates"], 2, [])
            await wait_for(lambda: len(fake.original_edits) > before, timeout=15)
            assert asked == ["check_update", "check_program_update"]

    run(scenario())


def test_saying_why_is_what_players_read(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        manager = Manager()
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, manager)
            opened, token = fake.command("server")
            ask = await press(fake, opened, buttons(await panel_of(fake, opened, token))["Restart"])
            assert "Yes, and say why" in buttons(ask)

    run(scenario())


def test_several_servers_and_no_name_is_a_list_to_pick_from(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=("Red", "Blue")) as (huragok, _):
            await up(huragok)
            opened, token = fake.command("server")
            form = await panel_of(fake, opened, token)
            options = [option["value"] for row in form["components"] for item in row["components"] for option in item.get("options", [])]
            assert sorted(options) == ["Blue", "Red"]

    run(scenario())
