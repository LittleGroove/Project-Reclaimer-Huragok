"""Contact staff, /case, /status, and the setup's panels for how people reach the staff."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, OWNER
from test_bot_running import OTHER, _components, answer_text
from test_tickets import ALERTS, HURAGOK_LOGS, LOGS, MOD_USER, REPORTS, STAFF, buttons_of, form_of, posts_in
from testkit.fake_discord import ADMIN_ROLE, BOT_ROLE, GUILD, MOD_ROLE, OWNER_USER, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot import features
from huragok.discordbot.config import add_owner, load, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.discordbot.contact import OPEN, time_of

INFO = "4209"
ROLE = "5107"


def can_make_threads(discord: FakeDiscord) -> None:
    """The bot's role with what this feature needs, as its invite link asks."""
    role = next(one for one in discord.roles if one["id"] == BOT_ROLE)
    role["permissions"] = str(int(role["permissions"]) | (1 << 34) | (1 << 36) | (1 << 38))


def the_staffs_channels(discord: FakeDiscord) -> None:
    private = [{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]
    discord.channels += [discord._channel(channel, name, overwrites=private) for channel, name in ((REPORTS, "reports"), (LOGS, "logs"), (ALERTS, "alerts"), (HURAGOK_LOGS, "huragok-logs"))]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.channels.append(discord._channel(INFO, "info"))
    the_staffs_channels(discord)
    discord.roles.append(FakeDiscord._role(ROLE, "Staff", 1, "0"))
    can_make_threads(discord)
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, **options: Any) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    for name, channel in (("staff", STAFF), ("reports", REPORTS), ("alerts", ALERTS), ("logs", LOGS), ("huragok-logs", HURAGOK_LOGS), ("info", INFO)):
        set_channel(path, name, channel, name)
    set_switch(path, ("contact",), "enabled", True)
    for key, value in options.items():
        set_switch(path, ("contact",), key, value)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


def the_button(fake: FakeDiscord) -> dict[str, Any] | None:
    return next((post for post in fake.posted if any(item.get("custom_id") == OPEN for row in post.get("components") or [] for item in row["components"])), None)


async def card_up(part: Any, fake: FakeDiscord) -> dict[str, Any]:
    await part.status_tick()
    await wait_for(lambda: the_button(fake), timeout=15)
    return the_button(fake)  # type: ignore[return-value]


async def opened(part: Any, fake: FakeDiscord, *, user: str = OTHER, text: str = "Someone's been cheating") -> str:
    """Someone pressing the button and filling in the form: their thread."""
    post = await card_up(part, fake)
    _, token = fake.press(OPEN, post, user=user)
    await form_of(fake, token)
    _, sent = fake.fill_in(token, post, {"What it's about": "Question", "Message": text}, user=user)
    said = await answer_text(fake, sent)
    assert "Your thread is here" in said
    return said.split("<#")[1].split(">")[0]


def in_thread(fake: FakeDiscord, thread: str) -> list[dict[str, Any]]:
    return [post for post in fake.posted if post["channel"] == thread]


def test_the_button_is_kept_where_the_setting_says(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            post = await card_up(part, fake)
            assert post["channel"] == INFO
            await part.status_tick()
            assert sum(the_button(fake) is one for one in fake.posted) == 1
            set_switch(path, ("contact",), "enabled", False)
            await part.reload(force=True)
            await part.status_tick()
            await wait_for(lambda: the_button(fake) is None, timeout=15)

    run(scenario())


def test_it_opens_a_private_thread_with_them_in_it_and_a_ticket_for_staff(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            made = fake.private_threads[thread]
            assert made["type"] == 12 and made["invitable"] is False and made["parent"] == INFO and made["members"] == [OTHER]
            assert f"<@{OTHER}>" in in_thread(fake, thread)[0]["content"] and "cheating" in str(in_thread(fake, thread)[0]["embeds"])
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            card = posts_in(fake, REPORTS)[0]
            assert "needs the staff (Question)" in card["embeds"][0]["description"] and "cheating" in str(card["embeds"][0]["fields"])
            assert list(buttons_of(card)) == ["Discuss", "Close"]  # no player to act on

    run(scenario())


def test_the_role_named_is_brought_in(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, thread_role=ROLE)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            first = in_thread(fake, thread)[0]
            assert f"<@&{ROLE}>" in first["content"] and first["allowed_mentions"]["roles"] == [ROLE]

    run(scenario())


def test_several_roles_are_all_brought_in(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, thread_role=[ROLE, ADMIN_ROLE])
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            assert not part.file.warnings
            thread = await opened(part, fake)
            first = in_thread(fake, thread)[0]
            assert f"<@&{ROLE}>" in first["content"] and f"<@&{ADMIN_ROLE}>" in first["content"] and set(first["allowed_mentions"]["roles"]) == {ROLE, ADMIN_ROLE}

    run(scenario())


def test_one_at_a_time_for_each_person(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            _, token = fake.press(OPEN, the_button(fake), user=OTHER)  # type: ignore[arg-type]
            assert f"<#{thread}>" in await answer_text(fake, token)
            assert len(fake.private_threads) == 1
            await opened(part, fake, user="9912")  # someone else can
            assert len(fake.private_threads) == 2

    run(scenario())


def test_discuss_brings_staff_into_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            card = posts_in(fake, REPORTS)[0]
            _, token = fake.press(buttons_of(card)["Discuss"], card, user=MOD_USER, roles=(MOD_ROLE,))
            await wait_for(lambda: MOD_USER in fake.private_threads[thread]["members"], timeout=15)
            assert thread in await answer_text(fake, token)

    run(scenario())


def test_closing_it_locks_the_thread_keeps_a_transcript_and_files_the_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            card = posts_in(fake, REPORTS)[0]
            fake.press(buttons_of(card)["Close"], card, user=MOD_USER, roles=(MOD_ROLE,))
            await wait_for(lambda: any(edit["channel"] == thread and edit.get("locked") for edit in fake.channel_edits), timeout=15)
            kept = next(post for post in fake.posted if post.get("files"))
            assert "cheating" in str(in_thread(fake, thread)[0]["embeds"]) and "closed this" in in_thread(fake, thread)[-1]["content"]
            assert kept["files"][0].startswith("[")
            await wait_for(lambda: posts_in(fake, LOGS), timeout=15)
            assert "Transcript" in posts_in(fake, LOGS)[0]["embeds"][0]["description"]

    run(scenario())


def test_whoever_opened_it_can_close_it_and_nobody_else_who_isnt_staff(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            first = in_thread(fake, thread)[0]
            close = buttons_of(first)["Close"]
            _, token = fake.press(close, first, user="9913")
            assert "whoever opened" in await answer_text(fake, token)
            fake.press(close, first, user=OTHER)
            await wait_for(lambda: any(edit["channel"] == thread and edit.get("locked") for edit in fake.channel_edits), timeout=15)

    run(scenario())


def test_without_message_content_it_says_theres_no_transcript(tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = FakeDiscord()
    discord.flags = 0
    discord.channels.append(discord._channel(INFO, "info"))
    the_staffs_channels(discord)
    can_make_threads(discord)
    discord.start()
    use_fake_discord(discord, monkeypatch)
    try:
        async def scenario() -> None:
            bot_file(tmp_path)
            async with running(tmp_path) as (huragok, _, _):
                part = await up(huragok)
                await opened(part, discord)
                await wait_for(lambda: posts_in(discord, REPORTS), timeout=15)
                card = posts_in(discord, REPORTS)[0]
                discord.press(buttons_of(card)["Close"], card, user=MOD_USER, roles=(MOD_ROLE,))
                await wait_for(lambda: posts_in(discord, LOGS), timeout=15)
                assert "No transcript" in posts_in(discord, LOGS)[0]["embeds"][0]["description"]

        run(scenario())
    finally:
        discord.stop()


def test_a_thread_with_nothing_said_closes_after_a_while(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, close_after_days=1, transcript=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            thread = await opened(part, fake)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            await part.contact.sweep()
            assert not fake.channel_edits  # it hasn't been an hour
            part.contact.swept = 0.0
            await part.contact.sweep()
            assert not fake.channel_edits  # nor a day
            part.contact.clock = lambda: time.time() + 2 * 86400
            part.contact.swept = 0.0
            await part.contact.sweep()
            await wait_for(lambda: any(edit["channel"] == thread for edit in fake.channel_edits), timeout=15)

    run(scenario())


def test_with_nowhere_for_its_card_to_go_it_opens_no_thread(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "reports", "", "")
        set_channel(path, "staff", "", "")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await part.status_tick()
            assert features.state_of(features.BY_KEY["contact"], part.file, part.facts)[0] == features.WAITING  # no button that opens a thread nobody hears of
            assert the_button(fake) is None and not fake.private_threads

    run(scenario())


def test_the_check_says_when_people_cant_write_back(tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = FakeDiscord()
    channel = discord._channel(INFO, "info", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 38)}])
    discord.channels.append(channel)
    can_make_threads(discord)
    discord.start()
    use_fake_discord(discord, monkeypatch)
    try:
        path = bot_file(tmp_path)
        findings, _ = features.check(load(path))
        assert any(finding.where == "Contact staff" and not finding.good and "Send Messages in Threads" in finding.text for finding in findings)
        channel["permission_overwrites"] = [{"id": GUILD, "type": 0, "allow": str((1 << 38) | (1 << 10)), "deny": "0"}]
        findings, _ = features.check(load(path))
        assert not any("write back" in finding.text for finding in findings)
    finally:
        discord.stop()


def test_a_transcript_is_what_people_said(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            said = part.contact.transcript([{"id": "1000000000000000000", "type": 1, "author": {"username": "Huragok"}, "content": ""},
                                            {"id": "1000000000000000001", "type": 0, "author": {"username": "Alice"}, "content": "hello", "attachments": [{"url": "https://cdn/clip.mp4"}]},
                                            {"id": "1000000000000000002", "type": 19, "author": {"username": "Mod"}, "content": "hi there"}])
            assert said.count("\n") == 2 and "Alice: hello https://cdn/clip.mp4" in said and "Mod: hi there" in said and "Huragok" not in said

    run(scenario())


def test_ids_tell_the_time() -> None:
    assert abs(time_of("1000000000000000000") - 1658490000) < 86400 * 2


def test_without_the_permissions_it_says_what_it_needs_and_stays_quiet(tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = FakeDiscord()
    discord.channels.append(discord._channel(INFO, "info"))
    the_staffs_channels(discord)
    discord.start()
    use_fake_discord(discord, monkeypatch)
    try:
        async def scenario() -> None:
            bot_file(tmp_path)
            async with running(tmp_path) as (huragok, _, _):
                part = await up(huragok)
                state, why = features.state_of(features.BY_KEY["contact"], part.file, part.facts)
                assert state == features.WAITING and "Create Private Threads" in why
                await part.status_tick()
                assert the_button(discord) is None

        run(scenario())
    finally:
        discord.stop()


def test_the_intent_is_asked_for_only_if_a_transcript_is_wanted(tmp_path: Path) -> None:
    path = bot_file(tmp_path)
    assert "message_content" in features.needed_intents(load(path))
    set_switch(path, ("contact",), "transcript", False)
    assert "message_content" not in features.needed_intents(load(path))


# /status and /case ------------------------------------------------------------------------------------------------------


def test_status_is_for_anyone_and_can_name_a_server(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            _, token = fake.command("status", user=OTHER, server=SERVER)
            await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
            form = next(c for c in fake.callbacks if c.get("token") == token)["data"]
            assert form["flags"] == 64 and SERVER in str(form["embeds"][0])
            _, token = fake.command("status", user=OTHER, server="nothing like it")
            assert "no server called" in await answer_text(fake, token)

    run(scenario())


def test_case_shows_staff_what_happened(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            number = huragok.storage.record_action(kind="ban", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod (Discord)", source="commands", reason="slurs in voice", duration="7d", evidence=None, dry_run=False, ok=True, reply="")
            _, token = fake.command("case", user=OTHER, number=number)
            assert "for staff" in await answer_text(fake, token)
            _, token = fake.command("case", user=MOD_USER, roles=(MOD_ROLE,), number=number)
            await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
            form = next(c for c in fake.callbacks if c.get("token") == token)["data"]
            text = str(form["embeds"][0])
            assert form["flags"] == 64 and "**kick** of **Alice**" in text and "by Mod <t:" in text and "slurs in voice" in text and "7 days" in text
            _, token = fake.command("case", user=MOD_USER, roles=(MOD_ROLE,), number=number + 99)
            assert "no case" in await answer_text(fake, token)

    run(scenario())


# The panels ------------------------------------------------------------------------------------------------------------


async def home(fake: FakeDiscord) -> tuple[str, dict[str, Any]]:
    opened_at, token = fake.huragok("setup")
    await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=15)
    return opened_at, next(c for c in reversed(fake.callbacks) if c.get("token") == token)["data"]


async def picked(fake: FakeDiscord, opened_at: str, custom_id: str, kind: int = 2, values: list[str] | None = None) -> dict[str, Any]:
    before = len(fake.callbacks)
    fake.pick(opened_at, custom_id, kind, values or [])
    await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
    return fake.callbacks[-1]["data"]


def labels(form: dict[str, Any]) -> dict[str, str]:
    return {item["label"]: item["custom_id"] for item in _components(form) if item.get("label") and item.get("custom_id")}  # a link has no ID


def menus(form: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in _components(form) if item.get("type") == 3]


@pytest.mark.parametrize(("button", "section", "key", "placeholder", "label", "written"), [
    ("Reports", "reports", "threshold", "Ping staff", "Ping staff at 5 people reporting", 5), ("Reports", "reports", "reporter_cooldown_seconds", "One person", "One report each per hour", 3600),
    ("Appeals", "appeals", "cooldown_days", "The usual wait", "Usually: appeal again after 30 days", 30), ("Appeals", "appeals", "tell_outcome", "Telling them", "Don't tell them how it went", "no"),
    ("Contact", "contact", "close_after_days", "With nothing", "Quiet threads close after 14 days", 14)])
def test_the_panels_write_what_is_picked(tmp_path: Path, run: Any, fake: FakeDiscord, button: str, section: str, key: str, placeholder: str, label: str, written: Any) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)[button])
            menu = next(one for one in menus(screen) if one["placeholder"].startswith(placeholder))
            value = next(option["value"] for option in menu["options"] if option["label"] == label)
            await picked(fake, opened_at, menu["custom_id"], 3, [value])
            assert load(path).section(section)[key] == written and part.file.section(section)[key] == written

    run(scenario())


def test_what_can_be_appealed_is_several_at_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)["Appeals"])
            menu = next(one for one in menus(screen) if one["placeholder"].startswith("What can"))
            assert menu["max_values"] == 3
            values = [option["value"] for option in menu["options"] if option["label"] in ("Can appeal: bans", "Can appeal: mutes")]
            await picked(fake, opened_at, menu["custom_id"], 3, values)
            assert load(path).section("appeals")["for"] == ["ban", "mute"]

    run(scenario())


def test_a_switch_on_a_panel_flips(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)["Reports"])
            await picked(fake, opened_at, labels(screen)["Thank them: yes"])
            assert load(path).section("reports")["thank_reporter"] is False

    run(scenario())


def test_questions_are_one_to_a_line(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)["Appeals"])
            modal = await picked(fake, opened_at, labels(screen)["The questions"])
            assert "What happened?" in str(modal)
            assert modal["components"]

    run(scenario())


def test_the_panel_says_where_the_button_is_and_can_switch_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """"What contact button? Where?" It was off, its channel wasn't set, and nothing said so."""

    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("contact",), "enabled", False)
        set_channel(path, "info", "", "")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)["Contact"])
            assert "nowhere yet" in screen["embeds"][0]["description"]
            screen = await picked(fake, opened_at, labels(screen)["Switch it on"])
            assert load(path).section("contact")["enabled"] is True and "Switch it off" in labels(screen)

    run(scenario())


def test_once_its_set_the_panel_links_to_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await card_up(part, fake)
            await wait_for(lambda: part.status.stored("contact:card")[0], timeout=10)
            opened_at, form = await home(fake)
            screen = await picked(fake, opened_at, labels(form)["Contact"])
            assert f"https://discord.com/channels/{GUILD}/{INFO}/" in screen["embeds"][0]["description"]

    run(scenario())


def test_a_second_form_doesnt_make_a_second_thread(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Pressed on a second device, or before the first form's answer came. Each had its own thread, and the first was lost to staff."""
    async def scenario() -> None:
        bot_file(tmp_path, cooldown_minutes=0)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            post = await card_up(part, fake)
            _, one = fake.press(OPEN, post, user=OTHER)
            _, two = fake.press(OPEN, post, user=OTHER)
            await form_of(fake, one)
            await form_of(fake, two)
            _, first = fake.fill_in(one, post, {"What it's about": "Question", "Message": "first"}, user=OTHER)
            await answer_text(fake, first)
            _, second = fake.fill_in(two, post, {"What it's about": "Question", "Message": "second"}, user=OTHER)
            assert "<#" in await answer_text(fake, second)  # where theirs is
            assert len(fake.private_threads) == 1

    run(scenario())


def test_contact_cant_be_opened_again_straight_away(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Each is a thread, a card and a ping, and it could be closed and opened again as fast as it could be pressed."""
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            post = await card_up(part, fake)
            await opened(part, fake)
            number = part.tickets.ongoing(f"contact:{OTHER}")
            await part.contact.close(number, "them")
            _, token = fake.press(OPEN, post, user=OTHER)
            assert "another" in await answer_text(fake, token)
            assert not any(callback["token"] == token and callback.get("type") == 9 for callback in fake.callbacks)

    run(scenario())

