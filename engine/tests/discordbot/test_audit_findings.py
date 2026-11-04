"""What the audit of 31 October found, each kept fixed. A fleet with a stopped server first, as DEPLOY's list runs, and Discord's own 3 seconds."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ADMIN, ALICE, BRAVO, FACULTY
from test_a_real_fleet import _label, _select, press, saying_yes
from test_bot_running import OTHER, _components, answer_text
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, MOD_USER, REPORTS, STAFF, buttons_of, form_of, posts_in, said_back
from testkit.discord_rules import component_problems, modal_problems
from testkit.fake_discord import ADMIN_ROLE, BOT_ROLE, GUILD, MOD_ROLE, FACULTY_USER, PUBLIC_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.core.settings import RelayUser
from huragok.discordbot import cards, features
from huragok.discordbot.config import add_faculty, make_file, remove_faculty, set_channel, set_guild, set_mode, set_switch, set_token

STOPPED, FIRST, SECOND = "LG Backend Test", "LG Test Server", "Sim Slayer"
ADMIN_USER = "4441"
WHAT, WHY = "What happened?", "Why should it be lifted?"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.interaction_seconds = 3.0
    role = next(one for one in discord.roles if one["id"] == BOT_ROLE)
    role["permissions"] = str(int(role["permissions"]) | (1 << 34) | (1 << 36) | (1 << 38))
    private = [{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]
    discord.channels += [discord._channel(channel, name, overwrites=private) for channel, name in ((REPORTS, "reports"), (ALERTS, "alerts"), (LOGS, "logs"), (HURAGOK_LOGS, "huragok-logs"))]
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *switches: tuple[str, ...], text: str = "") -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_faculty(path, FACULTY_USER, "the host")
    for name, channel in (("staff", STAFF), ("reports", REPORTS), ("alerts", ALERTS), ("logs", LOGS), ("huragok-logs", HURAGOK_LOGS)):
        set_channel(path, name, channel, name)
    set_switch(path, ("commands",), "enabled", True)
    set_switch(path, ("feed", "moderation"), "enabled", True)
    for table in switches:
        set_switch(path, table, "enabled", True)
    written = path.read_text(encoding="utf-8")
    written = written.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    path.write_text(written + text, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


async def on(huragok: Any, console: Any, server: str, number: int = 3, name: str = "Alice", player_id: str = ALICE) -> None:
    await console.join(number, name, player_id)
    await wait_for(lambda: huragok.superintendent.states[server].player_by_id(player_id), timeout=10)


async def left(huragok: Any, console: Any, server: str, number: int = 3, player_id: str = ALICE) -> None:
    await console.leave(number)
    await wait_for(lambda: not huragok.superintendent.states[server].player_by_id(player_id), timeout=10)


async def card_for(fake: FakeDiscord, part: Any, server: str, name: str = "Alice", player_id: str = ALICE) -> dict[str, Any]:
    there = len(posts_in(fake, STAFF))
    part.tickets.open_by_hand("Host", player_id, name, server)
    await wait_for(lambda: len(posts_in(fake, STAFF)) > there, timeout=15)
    return posts_in(fake, STAFF)[-1]


async def fill(fake: FakeDiscord, label: str, post: dict[str, Any], answers: dict[str, str], **who: Any) -> str:
    _, token = fake.press(buttons_of(post)[label], post, **who)
    await form_of(fake, token)
    return fake.fill_in(token, post, answers, **who)[1]


async def appeal(fake: FakeDiscord, who: str = "Alice") -> dict[str, Any]:
    _, token = fake.command("appeal", user=OTHER, who=who)
    await form_of(fake, token)
    _, sent = fake.fill_in(token, {"channel": STAFF, "id": "1"}, {WHAT: "carried away", WHY: "won't again"}, user=OTHER)
    assert "the staff have it" in await answer_text(fake, sent)
    await wait_for(lambda: any("Lift it" in buttons_of(post) for post in posts_in(fake, REPORTS)), timeout=15)
    return next(post for post in posts_in(fake, REPORTS) if "Lift it" in buttons_of(post))


def dms(fake: FakeDiscord, user: str = OTHER) -> list[str]:
    return [str(post.get("content")) for post in fake.posted if post["channel"] == f"dm{user}"]


def refuse(console: Any, *commands: str) -> None:
    for command in commands:
        console.replies[command] = lambda _message: {"ok": False, "text": "the server says no"}


# Every panel, with every kind of message ------------------------------------------------------------------------------------


def test_every_panel_builds_and_fits(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Picking any of 25 kinds of message used to fail as the panel was built, before it could answer."""

    async def scenario() -> None:
        from huragok.discordbot import panels, serverpanel

        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,), down=(STOPPED,)) as (huragok, _):
            part = await up(huragok)
            made: list[Any] = [panels.Home(part), panels.Ranks(part), panels.FacultyPanel(part), panels.Tickets(part), panels.Console(part), panels.ReportsPanel(part), panels.AppealsPanel(part), panels.ContactPanel(part),
                               serverpanel.ServerPanel(part, FIRST), serverpanel._Confirm(part, FIRST, "restart")]
            for page in range(panels.Channels(part).pages):
                made.append(panels.Channels(part, page))
            for page in range(panels.Features(part).pages):
                made.append(panels.Features(part, page))
            for page in range(panels.Messages(part).pages):
                made.append(panels.Messages(part, page))
            made += [panels.OneMessage(part, key, 0) for key in part.file.section("messages")]
            set_switch(part.path, ("console",), "layout", "per-server")
            await part.reload(force=True)
            made += [panels.Console(part), panels.ServerChannels(part)]
            set_switch(part.path, ("leaderboards",), "enabled", True)  # 0.6.1's: every button there is
            set_switch(part.path, ("roles",), "top", {"kills season": "5001"})
            await part.reload(force=True)
            made += [panels.StatsPanel(part), panels.LeaderboardsPanel(part), panels.LeaderboardsPanel(part, "week"),
                     panels.StatsRolesPanel(part), panels.StatsRolesPanel(part, "kills season"), panels.SidesPanel(part)]
            for panel in made:
                assert not component_problems(panel.to_components()), type(panel).__name__
                panel.card()
                panel.stop()

    run(scenario())


# What someone writes isn't what's done --------------------------------------------------------------------------------------


@pytest.mark.parametrize("why", ["never listens to staff", "perm is what they deserve", "2h of camping"])
def test_a_kicks_reason_cant_change_what_it_does(tmp_path: Path, run: Any, fake: FakeDiscord, why: str) -> None:
    """A reason starting with "never" made a kick a permanent ban, and one starting "2h" gave it a time."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            post = await card_for(fake, part, FIRST)
            await fill(fake, "Kick", post, {"Why (they'll read this)": why}, user=FACULTY_USER)
            await wait_for(lambda: consoles[FIRST].named("kick"), timeout=15)
            assert consoles[FIRST].named("ban") == []
            rows = huragok.storage.query("SELECT kind, duration, reason FROM actions WHERE target_id = ?", (ALICE,))
            assert (rows[0]["kind"], rows[0]["duration"], rows[0]["reason"]) == ("kick", None, why)

    run(scenario())


def test_a_bans_reason_is_only_its_reason(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            post = await card_for(fake, part, FIRST)
            await fill(fake, "Ban", post, {"Why (they'll read this)": "2h of slurs"}, user=FACULTY_USER)
            await wait_for(lambda: consoles[FIRST].named("ban"), timeout=15)
            rows = huragok.storage.query("SELECT duration, reason FROM actions WHERE kind = 'ban' AND target_id = ?", (ALICE,))
            assert (rows[0]["duration"], rows[0]["reason"]) == (None, "2h of slurs")

    run(scenario())


# What counts as done is what the ledger says ----------------------------------------------------------------------------------


def test_a_failed_lift_leaves_the_appeal_open_and_says_nothing_to_them(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A failed unban closed the card and told the player it had been lifted."""

    async def scenario() -> None:
        bot_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[FIRST], FIRST, 1, "Faculty", FACULTY)
            await consoles[FIRST].chat(FACULTY, "Faculty", "!ban Alice perm slurs")
            await wait_for(lambda: part.tickets.standing(ALICE)["banned"], timeout=15)
            post = await appeal(fake)
            refuse(consoles[FIRST], "unban")
            number = int(buttons_of(post)["Lift it"].split(":")[2])
            _, token = fake.press(buttons_of(post)["Lift it"], post)
            await said_back(fake, token)
            assert part.appeals.of_ticket(number)["outcome"] == "" and not dms(fake)
            assert part.tickets.get(number)["state"] in ("open", "active")
            assert part.tickets.standing(ALICE)["banned"]

    run(scenario())


def test_shortening_when_the_new_time_doesnt_go_on_says_so_and_stays_open(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, ("appeals",))
        set_switch(path, ("appeals",), "for", ["ban", "kick"])
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[FIRST], FIRST, 1, "Faculty", FACULTY)
            await consoles[FIRST].chat(FACULTY, "Faculty", "!kick Alice 2h spam")
            await wait_for(lambda: part.tickets.standing(ALICE)["kicked"], timeout=15)
            post = await appeal(fake)
            number = int(buttons_of(post)["Lift it"].split(":")[2])
            sent = await fill(fake, "Shorten it", post, {"How long from now": "7d"}, user=MOD_USER, roles=(MOD_ROLE,))  # past what a moderator's kick can be
            assert "at most" in await said_back(fake, sent)  # said while nothing's been touched: it can't be half done
            assert part.appeals.of_ticket(number)["outcome"] == "" and not dms(fake) and part.tickets.standing(ALICE)["kicked"]

    run(scenario())


def test_a_failed_press_on_a_report_isnt_it_being_acted_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, ("reports",))
        set_switch(path, ("reports",), "reporter_cooldown_seconds", 0)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[FIRST], FIRST, 4, "Bravo", BRAVO)
            await consoles[FIRST].chat(BRAVO, "Bravo", "!report Alice camping")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            refuse(consoles[FIRST], "kick")
            sent = await fill(fake, "Kick", post, {"Why (they'll read this)": "camping"})
            await said_back(fake, sent)
            assert part.reports.track_record(BRAVO) == "1 report, 0 acted on"
            assert part.tickets.get(1)["state"] == "open"  # still Dismiss: nothing's been done

    run(scenario())


def test_a_press_that_failed_leaves_the_next_thing_they_do_its_own_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Within 30 seconds of a press, whatever else that person did to that player made no card at all."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            post = await card_for(fake, part, FIRST)
            await fill(fake, "Mute", post, {"How long": "10m", "Why (they'll read this)": "first"}, user=FACULTY_USER)
            await wait_for(lambda: consoles[FIRST].named("mute") and not part.tickets.expecting, timeout=15)
            _, token = fake.command("kick", player=ALICE, reason="second")
            await said_back(fake, token)
            await wait_for(lambda: any("second" in str(card["embeds"][0]) for card in posts_in(fake, LOGS)), timeout=15)

    run(scenario())


def test_a_confirmed_request_that_failed_isnt_marked_confirmed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, ("feed", "requests"))
        probation = "5103"
        fake.roles.append(FakeDiscord._role(probation, "Probation", 1, "0"))
        path = tmp_path / "discord.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("probationary_moderator = []", f'probationary_moderator = ["{probation}"]'), encoding="utf-8")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            fake.command("kick", user=OTHER, roles=(probation,), player=ALICE, reason="spam")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            refuse(consoles[FIRST], "kick")
            _, token = fake.press(buttons_of(post)["Confirm"], post, user=MOD_USER, roles=(MOD_ROLE,))
            await said_back(fake, token)
            rows = huragok.storage.query("SELECT status FROM commands_requests")
            assert rows[0]["status"] != "confirmed"

    run(scenario())


# Which server, when it isn't obvious ------------------------------------------------------------------------------------------


def test_a_number_alone_isnt_guessed_when_several_servers_are_up(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """`/ban #4` used to ban whoever was #4 on the first server up."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND), down=(STOPPED,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST, 4)
            await on(huragok, consoles[SECOND], SECOND, 4, "Bravo", BRAVO)
            _, token = fake.command("ban", player="#4", time="perm", reason="spam")
            assert "different on each server" in await said_back(fake, token)
            assert consoles[FIRST].named("ban") == [] and consoles[SECOND].named("ban") == []
            opened, token = fake.command("ban", player="#4", time="perm", reason="spam", server=SECOND)
            await saying_yes(fake, opened, token)
            await wait_for(lambda: consoles[SECOND].named("ban"), timeout=15)
            assert huragok.storage.query("SELECT target_id FROM actions WHERE kind = 'ban'")[0]["target_id"] == BRAVO

    run(scenario())


def test_two_people_with_a_name_on_different_servers_arent_guessed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST, 3, "Bob", ALICE)
            await on(huragok, consoles[SECOND], SECOND, 3, "Bob", BRAVO)
            _, token = fake.command("kick", player="Bob", reason="spam")
            assert "More than one player" in await said_back(fake, token)
            assert consoles[FIRST].named("kick") == [] and consoles[SECOND].named("kick") == []

    run(scenario())


def test_lifting_goes_where_it_was_given(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A /ban in game is that server's alone: an unban sent to another server wouldn't lift it."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[SECOND], SECOND)
            huragok.storage.record_action(kind="ban", server=FIRST, target_id=ALICE, target_name="Alice", by="Faculty", source="in game", reason="slurs")
            _, token = fake.command("unban", player=ALICE)
            await said_back(fake, token)
            await wait_for(lambda: consoles[FIRST].named("unban"), timeout=15)

    run(scenario())


def test_a_hung_servers_picture_isnt_trusted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            assert part.where_to_run(ALICE) == FIRST
            huragok.superintendent.states[FIRST].connected = False
            assert part.where_to_run(ALICE) == SECOND
            huragok.superintendent.states[FIRST].connected = True

    run(scenario())


def test_a_report_of_a_name_two_people_share_isnt_guessed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, ("reports",))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST, 3, "Bob", ALICE)
            await on(huragok, consoles[SECOND], SECOND, 3, "Bob", BRAVO)
            _, token = fake.command("report", user=OTHER, player="Bob")
            assert "More than one player" in await answer_text(fake, token)
            assert not posts_in(fake, REPORTS)

    run(scenario())


def test_picking_between_players_shows_a_plain_player_only_names(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, ("reports",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST, 3, "Nate Higgers", ALICE)
            await on(huragok, consoles[FIRST], FIRST, 4, "Nate Higgins", BRAVO)
            await on(huragok, consoles[FIRST], FIRST, 5, "Viewer", "e" * 64)
            await consoles[FIRST].chat("e" * 64, "Viewer", "!report Nate")
            await wait_for(lambda: consoles[FIRST].named("tell"), timeout=10)
            told = str(consoles[FIRST].named("tell")[-1])
            assert ALICE[:8] not in told and BRAVO[:8] not in told

    run(scenario())


# Rank -------------------------------------------------------------------------------------------------------------------------

SCOPED = f'\n[[ranks.scoped]]\nrole = "5200"\nrank = "moderator"\nservers = ["{FIRST}"]\n'


def test_a_rank_for_one_server_counts_on_that_server(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """It only counted in the typed console: on commands and cards they were nobody."""

    async def scenario() -> None:
        bot_file(tmp_path, text=SCOPED)
        fake.roles.append(FakeDiscord._role("5200", "First's mods", 1, "0"))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[SECOND], SECOND, 4, "Bravo", BRAVO)
            _, token = fake.command("kick", user=OTHER, roles=("5200",), player=ALICE, reason="spam")
            await said_back(fake, token)
            await wait_for(lambda: consoles[FIRST].named("kick"), timeout=15)
            _, token = fake.command("kick", user=OTHER, roles=("5200",), player=BRAVO, reason="spam", server=SECOND)
            assert "and up" in await said_back(fake, token)
            assert consoles[SECOND].named("kick") == []
            post = await card_for(fake, part, FIRST, "Bravo", BRAVO)
            await on(huragok, consoles[FIRST], FIRST, 5, "Bravo", BRAVO)
            await fill(fake, "Mute", post, {"How long": "10m", "Why (they'll read this)": "x"}, user=OTHER, roles=("5200",))
            await wait_for(lambda: consoles[FIRST].named("mute"), timeout=15)
            other = await card_for(fake, part, SECOND, "Bravo", BRAVO)
            _, token = fake.press(buttons_of(other)["Whois"], other, user=OTHER, roles=("5200",))
            assert "isn't one you're staff on" in await answer_text(fake, token)

    run(scenario())


def test_faculty_taken_off_cant_go_on_using_an_open_panel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            opened, token = fake.huragok("setup")
            await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=15)
            form = next(c for c in fake.callbacks if c["token"] == token)["data"]
            assert remove_faculty(path, FACULTY_USER)
            await part.reload(force=True)
            custom_id = _label(form, "Reports")
            before = len(fake.callbacks)
            fake.pick(opened, custom_id, 2, [])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=5)
            assert "Only the faculty" in str(fake.callbacks[-1]["data"].get("content"))

    run(scenario())


# Answers ------------------------------------------------------------------------------------------------------------------------


def test_asking_about_updates_gets_its_answer_however_long_that_takes(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """GitHub can take seconds; Discord gives a press three."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)

            async def slowly(_verb: str, _options: dict[str, Any]) -> Any:
                await asyncio.sleep(3.5)
                from types import SimpleNamespace

                return SimpleNamespace(ok=True, message="All up to date.")

            part.ctx.request = slowly
            opened, token = fake.command("server", name=FIRST)
            await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=5)
            form = next(c for c in fake.callbacks if c["token"] == token)["data"]
            before = len(fake.original_edits)
            fake.pick(opened, _label(form, "Check for updates"), 2, [])  # it only asks, so it doesn't ask first
            await wait_for(lambda: len(fake.original_edits) > before, timeout=10)
            assert "All up to date." in str(fake.original_edits[-1])

    run(scenario())


def test_a_panel_thats_gone_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            opened, _ = fake.huragok("setup")
            before = len(fake.callbacks)
            fake.pick(opened, "0123456789abcdef", 2, [])
            await wait_for(lambda: len(fake.callbacks) > before + 1, timeout=8)
            assert "has closed" in str(fake.callbacks[-1]["data"].get("content"))

    run(scenario())


@pytest.mark.discord_errors
def test_something_failing_in_a_panel_gets_a_word_back(tmp_path: Path, run: Any, fake: FakeDiscord, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        from huragok.discordbot import panels

        def broken(*_args: Any, **_options: Any) -> None:
            raise PermissionError("the file's locked")

        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            await up(huragok)
            opened, token = fake.huragok("setup")
            await wait_for(lambda: any(c["token"] == token for c in fake.callbacks), timeout=15)
            form = next(c for c in fake.callbacks if c["token"] == token)["data"]
            screen = await press(fake, opened, _label(form, "Reports"), 2)
            monkeypatch.setattr(panels, "set_switch", broken)
            before = len(fake.callbacks)
            fake.pick(opened, _select(screen)["custom_id"], 3, [_select(screen)["options"][0]["value"]])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=5)
            assert "didn't work" in str(fake.callbacks[-1]["data"].get("content"))

    run(scenario())


def test_a_long_answer_is_measured_as_it_goes_out(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from types import SimpleNamespace

        from huragok.discordbot.slash import MOST_SHOWN, _say

        sent: list[dict[str, Any]] = []

        async def send(content: str = "", **options: Any) -> None:
            sent.append({"content": content, **options})

        interaction = SimpleNamespace(followup=SimpleNamespace(send=send))
        await _say(SimpleNamespace(file=SimpleNamespace(section=lambda _name: {})), interaction, cards.clean("*" * (MOST_SHOWN - 10)), True)  # each grows a backslash, so it can't stay a message
        assert "file" in sent[-1] and len(sent[-1]["content"]) <= 2000

    run(scenario())


# Host's settings can't break a form ---------------------------------------------------------------------------------------------


def test_emptying_the_questions_doesnt_kill_appeal(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, ("appeals",))
        set_switch(path, ("appeals",), "questions", [])
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[FIRST], FIRST, 1, "Faculty", FACULTY)
            await consoles[FIRST].chat(FACULTY, "Faculty", "!ban Alice perm slurs")
            await wait_for(lambda: part.tickets.standing(ALICE)["banned"], timeout=15)
            _, token = fake.command("appeal", user=OTHER, who="Alice")
            assert not modal_problems(await form_of(fake, token))

    run(scenario())


def test_two_categories_the_same_dont_kill_the_contact_button(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot.contact import OPEN

        path = bot_file(tmp_path)
        info = "4209"
        fake.channels.append(fake._channel(info, "info"))
        set_channel(path, "info", info, "info")
        set_switch(path, ("contact",), "enabled", True)
        set_switch(path, ("contact",), "categories", ["Question", "Question", "  ", "x" * 150, "x" * 160])
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            await part.status_tick()
            await wait_for(lambda: any(item.get("custom_id") == OPEN for post in fake.posted for row in post.get("components") or [] for item in row["components"]), timeout=15)
            post = next(post for post in fake.posted if post["channel"] == info)
            _, token = fake.press(OPEN, post, user=OTHER)
            assert not modal_problems(await form_of(fake, token))

    run(scenario())


def test_ticket_buttons_named_twice_are_one_button(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("tickets",), "buttons", ["whois", "whois", "note"])
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await card_for(fake, part, FIRST)  # the fake refuses two with one custom_id

    run(scenario())


# What the record says --------------------------------------------------------------------------------------------------------


def test_a_ban_only_on_huragoks_own_list_can_be_appealed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Bans brought over from the older lists aren't in the ledger, and /appeal said there was nothing to appeal."""

    async def scenario() -> None:
        bot_file(tmp_path, ("appeals",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await left(huragok, consoles[FIRST], FIRST)
            huragok.storage.db.execute("INSERT INTO commands_every_server (time, kind, target, name, reason, by, server) VALUES (?, 'ban', ?, 'Alice', 'old', 'Old', ?)", (1.0, ALICE, FIRST))
            assert part.tickets.standing(ALICE)["banned"]
            assert part.appeals.what_stands(ALICE) == "ban"

    run(scenario())


def test_an_unmute_the_server_refused_still_counts_as_off_the_list(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            huragok.storage.record_action(kind="mute", server=FIRST, target_id=ALICE, target_name="Alice", by="Mod", source="commands")
            assert part.tickets.standing(ALICE)["muted"]
            huragok.storage.record_action(kind="unmute", server=FIRST, target_id=ALICE, target_name="Alice", by="Mod", source="commands", ok=False, reply="nobody by that name is muted")
            assert not part.tickets.standing(ALICE)["muted"]

    run(scenario())


def test_banall_of_a_timed_ban_stays_a_kick(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[FIRST], FIRST, 1, "Faculty", FACULTY)
            consoles[FIRST].ban_list.append({"id": ALICE, "name": "Alice", "reason": "spam", "banned_by": "Faculty", "banned_at": int(time.time()), "expires": int(time.time()) + 7200})  # as someone's /ban in game leaves it
            await consoles[FIRST].chat(FACULTY, "Faculty", f"!banall {ALICE}")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM actions WHERE reason LIKE 'every server%'"), timeout=15)
            assert {key: value for key, value in part.tickets.standing(ALICE).items() if key != "on"} == {"muted": False, "kicked": True, "banned": False}

    run(scenario())


def test_a_pardon_typed_in_the_console_reaches_the_record(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The server says nothing when someone's pardoned, so their card went on saying they were kicked."""

    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "console", STAFF, "console")
        set_switch(path, ("console",), "typed", True)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await on(huragok, consoles[FIRST], FIRST, 1, "Faculty", FACULTY)
            await consoles[FIRST].chat(FACULTY, "Faculty", "!kick Alice spam")
            await wait_for(lambda: part.tickets.standing(ALICE)["kicked"], timeout=15)
            fake.message(STAFF, "pardon Alice")
            await wait_for(lambda: consoles[FIRST].named("pardon"), timeout=15)
            await wait_for(lambda: not part.tickets.standing(ALICE)["kicked"], timeout=15)

    run(scenario())


def test_an_unban_by_a_command_means_no_banned_elsewhere(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, _):
            part = await up(huragok)
            huragok.storage.record_action(kind="ban", server=SECOND, target_id=ALICE, target_name="Alice", by="Mod", source="commands")
            assert part.banned_elsewhere(ALICE, FIRST) == [SECOND]
            huragok.storage.record_action(kind="unban", server=FIRST, target_id=ALICE, target_name="Alice", by="Mod", source="commands")
            assert part.banned_elsewhere(ALICE, FIRST) == []

    run(scenario())


def test_an_admin_cant_reach_faculty_by_their_address(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST, 1, "Faculty", FACULTY)
            relay = huragok.vergil.relay
            admin = RelayUser("someone", "", "admin")
            address = huragok.superintendent.states[FIRST].player_by_id(FACULTY).ip
            assert address
            assert "you can't ban" in relay._outranked(admin, FIRST, f"ban {address.partition(':')[0]}", None)
            assert "you can't ban" in relay._outranked(admin, FIRST, f"ban {address.partition(':')[0].rsplit('.', 1)[0]}.0/24", None)
            assert relay._outranked(admin, FIRST, "ban 198.51.100.7", None) == ""
            assert relay._outranked(RelayUser("host", "", "faculty"), FIRST, f"ban {address.partition(':')[0]}", None) == ""

    run(scenario())


# The bot's own lifecycle --------------------------------------------------------------------------------------------------------


def test_one_switch_is_one_sign_in_however_often_the_part_looks(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Looking during the pause signed in as well, and two at once could leave two bots connected, each doing everything."""

    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "console", STAFF, "console")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            signed_in = len(fake.identifies)
            set_switch(path, ("console",), "typed", True)
            await part.reload(force=True)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                await part.look()
                await asyncio.sleep(0.1)
            await wait_for(lambda: part.state == "online", timeout=15)
            await asyncio.sleep(0.5)
            assert len(fake.identifies) == signed_in + 1
            assert sum(not socket.closed for socket in fake._sockets) == 1

    run(scenario())


def test_a_switch_the_portal_has_off_costs_no_sign_in(tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = FakeDiscord()
    discord.flags = 0
    discord.start()
    use_fake_discord(discord, monkeypatch)
    try:
        async def scenario() -> None:
            path = bot_file(tmp_path)
            set_channel(path, "console", STAFF, "console")
            async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
                part = await up(huragok)
                signed_in = len(discord.identifies)
                set_switch(path, ("console",), "typed", True)
                await part.reload(force=True)
                await asyncio.sleep(2.5)
                assert len(discord.identifies) == signed_in

        run(scenario())
    finally:
        discord.stop()


def test_a_change_while_commands_are_being_registered_isnt_lost(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            await wait_for(lambda: fake.commands.get(GUILD), timeout=15)
            client = part.client
            real = client.try_commands
            calls = 0

            async def slowly() -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    client.commands_soon()  # as a second switch would, while the first is at Discord
                await real()

            client.try_commands = slowly
            client.commands_soon()
            await wait_for(lambda: calls == 2, timeout=15)

    run(scenario())


def test_discord_going_away_isnt_huragok_failing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Showing the bot's status with no connection raised, five of those in ten minutes switched the whole part off."""

    async def scenario() -> None:
        path = bot_file(tmp_path, ("status", "presence"))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            client = part.client
            real, client.ws = client.ws, None
            assert await client.show_presence("watching", "a line", "online") is False
            await part.status.presence()  # not a failure, and it says it again when it's back
            assert "presence" not in part.status.shown
            client.ws = real
            assert await client.show_presence("watching", "a line", "online") is True

    run(scenario())


# Private things --------------------------------------------------------------------------------------------------------------------


def test_a_card_doesnt_go_where_everyone_can_read(tmp_path: Path, run: Any, fake: FakeDiscord, caplog: pytest.LogCaptureFixture) -> None:
    """The check said "make it private", and posted it anyway."""

    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "staff", PUBLIC_CHANNEL, "public")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            assert part.tickets.open_by_hand("Host", ALICE, "Alice", FIRST) == 0
            assert not posts_in(fake, PUBLIC_CHANNEL)
            set_switch(path, ("feeds",), "public", True)
            await part.reload(force=True)
            assert part.tickets.open_by_hand("Host", ALICE, "Alice", FIRST) == 1
            await wait_for(lambda: posts_in(fake, PUBLIC_CHANNEL), timeout=15)

    run(scenario())


def test_with_discord_not_read_yet_it_doesnt_hold_things_up(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "staff", PUBLIC_CHANNEL, "public")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            part.facts = None
            assert not part.feeds.reads_by_everyone("staff")

    run(scenario())


def test_the_console_doesnt_run_where_everyone_can_read_unless_its_allowed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "console", PUBLIC_CHANNEL, "public")
        set_switch(path, ("console",), "typed", True)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            line = fake.message(PUBLIC_CHANNEL, "maps")
            await wait_for(lambda: any(str((post.get("message_reference") or {}).get("message_id")) == line for post in fake.posted), timeout=15)
            assert consoles[FIRST].named("maps") == []
            assert "can be read by everyone" in fake.posted[-1]["content"]
            fake.message(PUBLIC_CHANNEL, "maps", user=OTHER)
            set_switch(path, ("console",), "public", True)
            await part.reload(force=True)
            fake.message(PUBLIC_CHANNEL, "maps")
            await wait_for(lambda: consoles[FIRST].named("maps"), timeout=15)

    run(scenario())


# Cards after the webhook's gone, or in another voice --------------------------------------------------------------------------------


def test_after_the_bot_is_removed_and_added_back_a_card_is_posted_again_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            post = await card_for(fake, part, FIRST)
            await wait_for(lambda: part.tickets.get(1)["message"], timeout=10)
            fake.remove_the_bot()
            fake.add_the_bot()
            await wait_for(lambda: not part.poster.delivery.webhooks, timeout=15)
            part.tickets.add_line(1, "something happened")
            part.tickets.show(1)
            await wait_for(lambda: len(posts_in(fake, STAFF)) == 2, timeout=15)
            assert part.tickets.get(1)["message"] == posts_in(fake, STAFF)[1]["id"]
            assert posts_in(fake, STAFF)[0]["id"] == post["id"]  # the old one stays: the bot can't touch it without Manage Messages
            part.tickets.show(1)
            await wait_for(lambda: fake.edits, timeout=15)  # and the new one edits as any other
            assert len(posts_in(fake, STAFF)) == 2

    run(scenario())


def test_changing_the_voice_doesnt_strand_the_cards_already_up(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Edits went to the bot's own name, Discord refused one it hadn't written, and the whole channel stopped being posted to."""

    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            await card_for(fake, part, FIRST)
            await wait_for(lambda: part.tickets.get(1)["message"], timeout=10)
            assert posts_in(fake, STAFF)[0]["via"] == "webhook"
            set_switch(path, ("personas",), "enabled", False)
            await part.reload(force=True)
            part.tickets.show(1)
            await wait_for(lambda: fake.edits, timeout=15)
            assert len(posts_in(fake, STAFF)) == 1 and not part.feeds.blocked

    run(scenario())


def test_contact_keeps_its_private_thread_when_cards_have_threads(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot.contact import OPEN

        path = bot_file(tmp_path)
        info = "4209"
        fake.channels.append(fake._channel(info, "info"))
        set_channel(path, "info", info, "info")
        set_switch(path, ("contact",), "enabled", True)
        set_switch(path, ("tickets",), "when_closed", "thread")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            await part.status_tick()
            await wait_for(lambda: any(post["channel"] == info for post in fake.posted), timeout=15)
            post = next(post for post in fake.posted if post["channel"] == info)
            _, token = fake.press(OPEN, post, user=OTHER)
            await form_of(fake, token)
            _, sent = fake.fill_in(token, post, {"What kind of thing": "Question", "What's it about?": "hello"}, user=OTHER)
            said = await answer_text(fake, sent)
            private = said.split("<#")[1].split(">")[0]
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await asyncio.sleep(0.5)
            assert str(part.tickets.get(1)["thread"]) == private and not fake.threads

    run(scenario())


# What's kept, what's retried ---------------------------------------------------------------------------------------------------------


def test_a_kept_post_stays_while_things_are_only_unknown(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot.contact import CARD

        path = bot_file(tmp_path)
        info = "4209"
        fake.channels.append(fake._channel(info, "info"))
        set_channel(path, "info", info, "info")
        set_switch(path, ("contact",), "enabled", True)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            part.contact.keep_card()
            await wait_for(lambda: part.status.stored(CARD)[0], timeout=15)
            facts, part.facts = part.facts, None
            part.contact.keep_card()
            part.facts = facts
            assert part.status.stored(CARD)[0]
            set_switch(path, ("contact",), "enabled", False)
            await part.reload(force=True)
            part.contact.keep_card()
            assert not part.status.stored(CARD)[0]  # switched off is another thing

    run(scenario())


def test_a_ticket_whose_post_was_refused_gets_another_go(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Nobody saw it, and whoever reported next was told it was in hand."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            fake.refuse_next.append(("/webhooks/", 400, {"message": "no", "code": 0}, {}))
            part.tickets.open_by_hand("Host", ALICE, "Alice", FIRST)
            await wait_for(lambda: not part.outbox.waiting, timeout=15)
            await asyncio.sleep(0.3)
            assert not posts_in(fake, STAFF) and part.tickets.get(1)["message"] == ""
            part.tickets.sweep()
            assert not posts_in(fake, STAFF)  # not yet: it may only be slow
            part.tickets.clock = lambda: time.time() + 700
            part.tickets.sweep()
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)
            await wait_for(lambda: part.tickets.get(1)["message"], timeout=10)

    run(scenario())


def test_one_bad_card_doesnt_stop_a_whole_channel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            fake.refuse_next.append(("/webhooks/", 400, {"message": "no", "code": 0}, {}))
            part.tickets.open_by_hand("Host", ALICE, "Alice", FIRST)
            await wait_for(lambda: not part.outbox.waiting, timeout=15)
            await asyncio.sleep(0.3)
            assert not part.feeds.blocked
            assert part.tickets.open_by_hand("Host", BRAVO, "Bravo", FIRST)
            await wait_for(lambda: posts_in(fake, STAFF), timeout=15)

    run(scenario())


def test_a_wrong_webhook_address_isnt_a_bad_token(fake: FakeDiscord) -> None:
    from huragok.discordbot import rest

    hook = f"{fake.base}/webhooks/1/wrong"
    fake.refuse_next.append(("/webhooks/", 401, {"message": "Invalid Webhook Token", "code": 50027}, {}))
    with pytest.raises(rest.Refused) as refused:
        rest.send("POST", hook, body={"content": "x"})
    assert refused.value.status == 401 and not isinstance(refused.value, rest.BadToken)


def test_a_status_boards_dead_webhook_stops_being_asked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        address = f"{fake.base}/webhooks/77/gone"
        path = bot_file(tmp_path, ("status", "board"))
        from huragok.discordbot.config import set_webhook

        set_webhook(path, ("status", "board"), address)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            fake.refuse_next.append(("/webhooks/77", 404, {"message": "Unknown Webhook", "code": 10015}, {}))
            part.status.board()
            await wait_for(lambda: part.feeds.blocked, timeout=15)
            asked = len(fake.requests)
            part.status.board()
            await asyncio.sleep(0.5)
            assert not [request for request in fake.requests[asked:] if "/webhooks/77" in request[1]]

    run(scenario())


def test_uptime_alone_isnt_worth_an_edit(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A card differed on every tick for its first day: 30 edits an hour a server, for nothing."""

    async def scenario() -> None:
        path = bot_file(tmp_path, ("status", "board"))
        set_channel(path, "status", STAFF, "status")
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            part.status.board()
            await wait_for(lambda: part.status.stored(f"board:{FIRST}")[0], timeout=15)
            form = {"title": "x", "description": "Up 3 minutes"}
            part.status.shown[f"board:{FIRST}"] = repr(([form], [])).replace("3 minutes", "")
            part.status.seen_at[f"board:{FIRST}"] = part.status.clock()
            edits = len(fake.edits)
            part.status._show(f"board:{FIRST}", [{"title": "x", "description": "Up 4 minutes"}], [], not_news="4 minutes")
            await asyncio.sleep(0.3)
            assert len(fake.edits) == edits

    run(scenario())


def test_every_setting_is_read_by_something() -> None:
    """A setting that does nothing tells a host they've changed something when they haven't. It has to be read by name somewhere. What isn't built yet doesn't count until its stage."""
    import re
    import tomllib

    import huragok
    home = Path(huragok.__file__).parent
    template = tomllib.loads((home / "templates" / "discord.toml").read_text(encoding="utf-8"))
    source = "\n".join(path.read_text(encoding="utf-8") for path in (home / "discordbot").glob("*.py"))
    free_form = {"messages", "colours", "text", "channels", "notes.flags"}  # each is read by what it's called in the file, not by a name in the code

    def unread(table: dict[str, Any], where: str) -> list[str]:
        found: list[str] = []
        for key, value in table.items():
            here = f"{where}.{key}" if where else key
            if here in free_form:
                continue
            if isinstance(value, dict) and value:
                found += unread(value, here)
            elif not re.search(rf"""["']{re.escape(key)}["']""", source):
                found.append(here)
        return found

    assert unread(template, "") == []

