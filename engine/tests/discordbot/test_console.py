"""The typed console: lines in a console channel run on a server, for those with a rank, with the same checks as anywhere else."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, MOD, OWNER
from test_bot_running import OTHER
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, OWNER_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.discordbot.config import add_owner, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.servers.state import ServerState

ADMIN_USER, MOD_USER, PROBATION_USER = "4441", "4442", "4443"
PROBATION_ROLE = "5103"
SECOND = "Second Server"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.roles.append(FakeDiscord._role(PROBATION_ROLE, "Probation", 1, "0"))
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, **console: Any) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    set_channel(path, "console", STAFF_CHANNEL, "console")
    set_switch(path, ("console",), "typed", True)
    for key, value in console.items():
        set_switch(path, ("console",), key, value)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]').replace("\nprobationary_moderator = []", f'\nprobationary_moderator = ["{PROBATION_ROLE}"]')
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any, *, second: bool = False) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    if second:
        huragok.superintendent.states[SECOND] = ServerState(SECOND)
    return part


def replies(fake: FakeDiscord, to: str | None = None) -> list[dict[str, Any]]:
    return [post for post in fake.posted if post.get("message_reference") and (to is None or str(post["message_reference"]["message_id"]) == to)]


async def reply_to(fake: FakeDiscord, line: str) -> dict[str, Any]:
    await wait_for(lambda: replies(fake, line), timeout=15)
    return replies(fake, line)[-1]


def buttons(post: dict[str, Any]) -> dict[str, str]:
    return {item["label"]: item["custom_id"] for row in post.get("components") or [] for item in row["components"] if item.get("label")}


async def press_until(fake: FakeDiscord, done: Any, custom_id: str, post: dict[str, Any], **who: Any) -> None:
    """A press as soon as the post's in the fake can get there before discord.py has its answer and knows the buttons, where a person would still be reading: so again, until it counts."""
    for _ in range(15):
        fake.press(custom_id, post, **who)
        try:
            await wait_for(done, timeout=1)
            return
        except AssertionError:
            pass
    raise AssertionError("the press never counted")


def test_a_console_line_runs_and_its_answer_comes_back_as_a_reply(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            rcon.replies["maps"] = lambda _message: {"ok": True, "text": "Guardian\nHigh Ground"}
            line = fake.message(STAFF_CHANNEL, "maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            post = await reply_to(fake, line)
            assert rcon.named("maps")
            assert post["content"] == "```\nGuardian\nHigh Ground\n```"
            assert post["allowed_mentions"]["parse"] == []

    run(scenario())


def test_a_bang_is_a_huragok_command_with_your_rank(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            line = fake.message(STAFF_CHANNEL, "!kick Alice spam", roles=(MOD_ROLE,), user=MOD_USER)
            await reply_to(fake, line)
            assert rcon.named("kick") or rcon.named("ban")
            line = fake.message(STAFF_CHANNEL, "!say hello", roles=(MOD_ROLE,), user=MOD_USER)
            assert "and up" in (await reply_to(fake, line))["content"]

    run(scenario())


def test_on_probation_a_line_is_a_request(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            line = fake.message(STAFF_CHANNEL, "!kick Alice spam", roles=(PROBATION_ROLE,), user=PROBATION_USER)
            await reply_to(fake, line)
            assert rcon.named("kick") == [] and rcon.named("ban") == []
            line = fake.message(STAFF_CHANNEL, "kick 3", roles=(PROBATION_ROLE,), user=PROBATION_USER)
            await reply_to(fake, line)
            assert rcon.named("kick") == []

    run(scenario())


def test_people_without_a_rank_arent_heard(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            fake.message(STAFF_CHANNEL, "kick 3", user=OTHER)
            fake.message(STAFF_CHANNEL, "!kick Alice", user=OTHER)
            line = fake.message(STAFF_CHANNEL, "!rules", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            await reply_to(fake, line)  # the admin's is answered, so the others' have been read by now
            assert rcon.named("kick") == [] and len(replies(fake)) == 1 and not fake.deleted

    run(scenario())


def test_or_their_lines_can_be_deleted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, ignore_others="delete")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "kick 3", user=OTHER)
            await wait_for(lambda: line in fake.deleted, timeout=15)
            assert not replies(fake)

    run(scenario())


def test_elsewhere_nothing_is_a_line(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            fake.message("9999", "kick 3", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            fake.message(STAFF_CHANNEL, "kick 4", roles=(ADMIN_ROLE,), user=ADMIN_USER, bot=True)
            fake.message(STAFF_CHANNEL, "kick 5", roles=(ADMIN_ROLE,), user=ADMIN_USER, webhook=True)
            line = fake.message(STAFF_CHANNEL, "!rules", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            await reply_to(fake, line)
            assert rcon.named("kick") == []

    run(scenario())


def test_raw_lines_are_for_admins_unless_the_host_says(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            line = fake.message(STAFF_CHANNEL, "maps", roles=(MOD_ROLE,), user=MOD_USER)
            assert "for admins" in (await reply_to(fake, line))["content"] and rcon.named("maps") == []
            set_switch(path, ("console",), "raw_rank", "moderator")
            await part.reload(force=True)
            line = fake.message(STAFF_CHANNEL, "maps", roles=(MOD_ROLE,), user=MOD_USER)
            await reply_to(fake, line)
            assert rcon.named("maps")

    run(scenario())


def test_raw_can_be_off_for_everyone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, raw_rank="off")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "maps")
            assert "`!`" in (await reply_to(fake, line))["content"] and rcon.named("maps") == []

    run(scenario())


def test_the_relays_checks_hold_a_line_to_its_rank(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "reload", roles=(ADMIN_ROLE,), user=ADMIN_USER)  # owner's alone, and one that asks first
            asked = await reply_to(fake, line)
            await press_until(fake, lambda: any("needs the owner role" in str(post.get("content")) for post in replies(fake, line)), buttons(asked)["Yes"], asked, user=ADMIN_USER, roles=(ADMIN_ROLE,))
            assert rcon.named("reload") == []
            await rcon.join(1, "Owner", OWNER)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            line = fake.message(STAFF_CHANNEL, f"kick {OWNER}", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            assert "nobody acts on their own role or above" in (await reply_to(fake, line))["content"] and rcon.named("kick") == []

    run(scenario())


def test_moderators_see_the_start_of_an_id_unless_the_host_says(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("console",), "raw_rank", "moderator")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            rcon.replies["maps"] = lambda _message: {"ok": True, "text": f"last played by {ALICE}"}
            line = fake.message(STAFF_CHANNEL, "maps", roles=(MOD_ROLE,), user=MOD_USER)
            said = (await reply_to(fake, line))["content"]
            assert ALICE not in said and ALICE[:8] in said
            line = fake.message(STAFF_CHANNEL, "maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            assert ALICE not in (await reply_to(fake, line))["content"]  # moderators read this channel too, whoever asked
            set_switch(path, ("ranks",), "moderators_see_ids", True)
            await part.reload(force=True)
            line = fake.message(STAFF_CHANNEL, "maps", roles=(MOD_ROLE,), user=MOD_USER)
            assert ALICE in (await reply_to(fake, line))["content"]

    run(scenario())


def test_one_line_at_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "say hello\nkick 3", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            assert "One line" in (await reply_to(fake, line))["content"] and rcon.named("kick") == [] and rcon.named("say") == []

    run(scenario())


def test_what_can_do_harm_asks_first_and_only_whoever_typed_it_can_answer(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "endround")
            asked = await reply_to(fake, line)
            assert "endround" in asked["content"] and rcon.named("endround") == []
            sure = buttons(asked)
            answered = len(fake.callbacks)
            await press_until(fake, lambda: len(fake.callbacks) > answered, sure["Yes"], asked, user=MOD_USER, roles=(MOD_ROLE,))
            assert rcon.named("endround") == []
            answered = len(fake.callbacks)
            await press_until(fake, lambda: len(fake.callbacks) > answered, sure["No"], asked)
            assert rcon.named("endround") == []
            line = fake.message(STAFF_CHANNEL, "endround")
            asked = await reply_to(fake, line)
            await press_until(fake, lambda: rcon.named("endround"), buttons(asked)["Yes"], asked)

    run(scenario())


def test_every_line_is_on_the_record_with_secrets_hidden(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, confirm=[])
        set_channel(path, "logs", "7777", "logs")
        set_switch(path, ("feed", "audit"), "enabled", True)
        set_switch(path, ("feeds",), "merge_seconds", 0)  # a burst of them is one line, otherwise
        fake.channels.append(fake._channel("7777", "logs", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "say hello", roles=(ADMIN_ROLE,), user=ADMIN_USER)  # what only looks isn't worth a line
            await reply_to(fake, line)
            await wait_for(lambda: any("say hello" in str(post) for post in fake.posted if post["channel"] == "7777"), timeout=15)
            secret = fake.message(STAFF_CHANNEL, "password hunter2")
            await wait_for(lambda: rcon.named("password"), timeout=15)
            await wait_for(lambda: sum(post["channel"] == "7777" for post in fake.posted) >= 2, timeout=15)
            await wait_for(lambda: secret in fake.deleted, timeout=15)
            assert not any("hunter2" in json.dumps(post) for post in fake.posted)

    run(scenario())


# Which server ------------------------------------------------------------------------------------------------------------


def test_with_one_server_there_is_nothing_to_ask(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await huragok.runners["discord"].part.status_tick()
            line = fake.message(STAFF_CHANNEL, "maps")
            await reply_to(fake, line)
            assert rcon.named("maps") and not fake.pins

    run(scenario())


def test_with_several_it_asks_once_then_remembers(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, second=True)
            line = fake.message(STAFF_CHANNEL, "maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            asked = await reply_to(fake, line)
            assert rcon.named("maps") == [] and asked["components"][0]["components"][0]["type"] == 3
            fake.choose(asked["components"][0]["components"][0]["custom_id"], asked, [SERVER], user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: rcon.named("maps"), timeout=15)
            before = len(rcon.named("maps"))
            line = fake.message(STAFF_CHANNEL, "maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            assert SERVER in (await reply_to(fake, line))["content"]  # the server's name on it: this channel's for any of them
            assert len(rcon.named("maps")) == before + 1

    run(scenario())


def test_a_line_can_name_its_server_however_its_spaced(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, second=True)
            line = fake.message(STAFF_CHANNEL, "lgtestserver: maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            await reply_to(fake, line)
            assert rcon.named("maps")

    run(scenario())


def test_a_colon_after_anything_else_is_part_of_the_line(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "say note: back soon", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            await reply_to(fake, line)
            assert "note: back soon" in json.dumps(rcon.named("say")[-1])

    run(scenario())


def test_a_server_of_its_own_needs_no_asking(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, layout="per-server", channel="", channels={SERVER: "6661", SECOND: "6662"})
        async with running(tmp_path) as (huragok, rcon, _):
            hidden = [{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]
            fake.channels.extend([fake._channel("6661", "one", overwrites=hidden), fake._channel("6662", "two", overwrites=hidden)])
            part = await up(huragok, second=True)
            line = fake.message("6661", "maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            post = await reply_to(fake, line)
            assert rcon.named("maps") and SERVER not in post["content"]
            await part.status_tick()
            assert not fake.pins  # no menu: no channel's for picking

    run(scenario())


# The menu ----------------------------------------------------------------------------------------------------------------


def menus(fake: FakeDiscord) -> list[dict[str, Any]]:
    return [post for post in fake.posted if any(item.get("custom_id") == "huragok:console:pick" for row in post.get("components") or [] for item in row["components"])]


def test_the_menu_is_posted_pinned_follows_the_servers_and_goes_when_its_not_wanted(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, second=True)
            await part.status_tick()
            await wait_for(lambda: menus(fake) and fake.pins, timeout=15)
            assert [item["value"] for item in menus(fake)[0]["components"][0]["components"][0]["options"]] == [SERVER, SECOND]
            await part.status_tick()
            assert len(menus(fake)) == 1  # kept, not posted again
            huragok.superintendent.states["Third"] = ServerState("Third")
            await part.status_tick()
            await wait_for(lambda: fake.edits, timeout=15)
            assert "Third" in json.dumps(fake.edits[-1]) and len(menus(fake)) == 1
            set_switch(path, ("console",), "typed", False)
            await part.reload(force=True)
            await wait_for(lambda: part.state == "online", timeout=20)
            await part.status_tick()
            await wait_for(lambda: not menus(fake), timeout=15)

    run(scenario())


def test_a_menu_someone_deleted_comes_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, second=True)
            await part.status_tick()
            await wait_for(lambda: menus(fake) and part.status.stored("console:menu")[0], timeout=15)
            fake.posted[:] = [post for post in fake.posted if post not in menus(fake)]  # as Discord would have it
            await part.status_tick()
            assert not menus(fake)  # it isn't asked every time: that's a request every 15 seconds
            part.kept_tried["console:menu"] -= 1000
            await part.status_tick()
            await wait_for(lambda: not part.status.stored("console:menu")[1], timeout=15)
            await part.status_tick()
            await wait_for(lambda: menus(fake), timeout=15)

    run(scenario())


def test_the_menu_keeps_each_persons_own_choice(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok, second=True)
            await part.status_tick()
            await wait_for(lambda: menus(fake), timeout=15)
            menu = menus(fake)[0]
            fake.choose("huragok:console:pick", menu, [SERVER], user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: part.status.stored(f"console:member:{ADMIN_USER}")[1] == SERVER, timeout=15)
            line = fake.message(STAFF_CHANNEL, "maps", roles=(ADMIN_ROLE,), user=ADMIN_USER)
            await reply_to(fake, line)
            assert rcon.named("maps")
            line = fake.message(STAFF_CHANNEL, "maps", user=OWNER_USER)  # who hasn't picked is still asked
            assert (await reply_to(fake, line))["components"]

    run(scenario())


def test_the_menu_is_only_for_those_with_a_rank(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, second=True)
            await part.status_tick()
            await wait_for(lambda: menus(fake), timeout=15)
            fake.choose("huragok:console:pick", menus(fake)[0], [SERVER], user=OTHER)
            await wait_for(lambda: fake.callbacks, timeout=15)
            assert part.status.stored(f"console:member:{OTHER}")[1] == ""

    run(scenario())


def test_one_server_for_everyone_is_a_setting(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, remember_server="channel")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok, second=True)
            await part.status_tick()
            await wait_for(lambda: menus(fake), timeout=15)
            fake.choose("huragok:console:pick", menus(fake)[0], [SERVER], user=ADMIN_USER, roles=(ADMIN_ROLE,))
            await wait_for(lambda: part.status.stored("console:channel")[1] == SERVER, timeout=15)
            line = fake.message(STAFF_CHANNEL, "maps", user=OWNER_USER)  # someone else's goes there too
            await reply_to(fake, line)
            assert rcon.named("maps")
            await part.status_tick()
            await wait_for(lambda: fake.edits, timeout=15)
            assert SERVER in json.dumps(fake.edits[-1]["embeds"])  # and the menu says so

    run(scenario())


def test_one_server_for_everyone_is_staffs_to_change(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, remember_server="channel")
        text = path.read_text(encoding="utf-8").replace("\nmember = []", f'\nmember = ["{PROBATION_ROLE}"]').replace(f'\nprobationary_moderator = ["{PROBATION_ROLE}"]', "\nprobationary_moderator = []")
        path.write_text(text, encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, second=True)
            await part.status_tick()
            await wait_for(lambda: menus(fake), timeout=15)
            fake.choose("huragok:console:pick", menus(fake)[0], [SERVER], user=PROBATION_USER, roles=(PROBATION_ROLE,))
            await wait_for(lambda: fake.callbacks, timeout=15)
            assert part.status.stored("console:channel")[1] == ""

    run(scenario())


# How answers come back ---------------------------------------------------------------------------------------------------


def long_maps(_message: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "text": "\n".join(f"Map number {n}" for n in range(300))}


def test_a_long_answer_is_a_file(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            rcon.replies["maps"] = long_maps
            line = fake.message(STAFF_CHANNEL, "maps")
            post = await reply_to(fake, line)
            assert "Map number 299" in post["files"][0]

    run(scenario())


def test_or_several_messages(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, long_answers="split")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            rcon.replies["maps"] = long_maps
            line = fake.message(STAFF_CHANNEL, "maps")
            await wait_for(lambda: len(replies(fake, line)) >= 3, timeout=15)
            said = "".join(post["content"] for post in replies(fake, line))
            assert "Map number 0" in said and "Map number 299" in said and all(len(post["content"]) <= 2000 for post in replies(fake, line))

    run(scenario())


def test_answers_can_go_in_a_thread(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, answers="thread")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            line = fake.message(STAFF_CHANNEL, "maps")
            await wait_for(lambda: line in fake.threads and any(post["channel"] == line for post in fake.posted), timeout=15)
            assert rcon.named("maps") and fake.threads[line] == "maps" and not replies(fake, line)

    run(scenario())


# Without Message Content ------------------------------------------------------------------------------------------------


def test_without_message_content_it_says_what_it_needs_and_nothing_runs(tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from huragok.discordbot import features

    discord = FakeDiscord()
    discord.flags = 0  # as the portal is until someone switches it on
    discord.start()
    use_fake_discord(discord, monkeypatch)
    try:
        async def scenario() -> None:
            bot_file(tmp_path)
            async with running(tmp_path) as (huragok, rcon, _):
                part = await up(huragok)
                state, why = features.state_of(features.BY_KEY["console"], part.file, part.facts)
                assert state == features.WAITING and "Message Content" in why
                assert not discord.identifies[-1]["intents"] & (1 << 15)
                discord.message(STAFF_CHANNEL, "maps")
                await part.status_tick()
                assert rcon.named("maps") == [] and not discord.posted

        run(scenario())
    finally:
        discord.stop()


def test_turning_it_on_in_the_portal_is_picked_up_without_a_restart(tmp_path: Path, run: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    discord = FakeDiscord()
    discord.flags = 0
    discord.start()
    use_fake_discord(discord, monkeypatch)
    try:
        async def scenario() -> None:
            bot_file(tmp_path)
            async with running(tmp_path) as (huragok, rcon, _):
                part = await up(huragok)
                signed_in = len(discord.identifies)
                await part.look()
                assert len(discord.identifies) == signed_in  # nothing's changed: a login costs
                discord.flags = 1 << 18
                part.look_at_discord()
                await wait_for(lambda: part.looking == 0 and part.facts is not None and part.facts.intent_on("message_content"), timeout=10)
                await part.look()
                await wait_for(lambda: len(discord.identifies) == signed_in + 1 and part.state == "online", timeout=20)
                await wait_for(lambda: part.facts is not None and part.looking == 0, timeout=20)
                line = discord.message(STAFF_CHANNEL, "maps")
                await reply_to(discord, line)
                assert rcon.named("maps")
                await part.look()
                assert len(discord.identifies) == signed_in + 1  # once

        run(scenario())
    finally:
        discord.stop()


def test_the_setting_that_needs_it_asks_for_it_at_sign_in(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            assert fake.identifies[-1]["intents"] & (1 << 15) and fake.identifies[-1]["intents"] & (1 << 9)

    run(scenario())
