"""Staff tools: a case's evidence and record, the watch list, !calladmin and !mods, seeding pings."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, MOD, OWNER
from test_bot_running import OTHER, answer_text
from testkit.fake_discord import GUILD, OWNER_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, tells_to, wait_for

from huragok.discordbot.config import add_owner, make_file, set_channel, set_guild, set_message, set_mode, set_switch, set_token


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *feeds: str, body: str = "") -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    for name in ("staff", "info", "logs"):
        set_channel(path, name, STAFF_CHANNEL, "admin-log")
    for name in feeds:
        set_switch(path, ("feed", name), "enabled", True)
    if body:
        path.write_text(path.read_text(encoding="utf-8") + body, encoding="utf-8")
    return path


async def up(huragok: Any) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    return part


def cards_of(fake: FakeDiscord) -> list[dict[str, Any]]:
    return [card for post in fake.posted for card in post.get("embeds", [])]


def said(fake: FakeDiscord) -> str:
    """What's been posted, as cards or as one line."""
    return "\n".join([*(str(card.get("description")) for card in cards_of(fake)), *(str(post["content"]) for post in fake.posted if post.get("content"))])


# A case -----------------------------------------------------------------------------------------------------------


def test_a_case_shows_what_they_said_and_their_record(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "moderation")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.chat(ALICE, "Alice", "you are all bad")
            await rcon.chat(ALICE, "Alice", "really bad")
            await wait_for(lambda: len(huragok.storage.query("SELECT * FROM commands_chat")) == 2, timeout=10)
            await huragok.vergil.moderation.act("mute", SERVER, ALICE, by="Mod", source="commands", reason="rude")
            await wait_for(lambda: len(cards_of(fake)) == 1, timeout=15)
            fields = {field["name"]: field["value"] for field in cards_of(fake)[0]["fields"]}
            assert fields["What they said before it"].split("\n")[0].startswith("you are all bad") and fields["What they said before it"].split("\n")[1].startswith("really bad")
            assert fields["Their record"] == "1 mute"  # this one too
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands", reason="ruder")
            await wait_for(lambda: len(cards_of(fake)) == 2, timeout=15)
            fields = {field["name"]: field["value"] for field in cards_of(fake)[1]["fields"]}
            assert fields["Their record"] in ("1 mute, 1 kick", "1 kick, 1 mute")

    run(scenario())


def test_what_a_case_shows_can_be_turned_down(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "moderation")
        text = path.read_text(encoding="utf-8").replace("evidence_lines = 5             # their chat", "evidence_lines = 0             # their chat")
        text = text.replace("record = true ", "record = false").replace("case_numbers = true", "case_numbers = false")
        path.write_text(text, encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands")
            await wait_for(lambda: len(cards_of(fake)) == 1, timeout=15)
            card = cards_of(fake)[0]
            assert "fields" not in card and "case" not in card["footer"]["text"]

    run(scenario())


# The watch list ------------------------------------------------------------------------------------------------------


def test_someone_back_soon_after_a_kick(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands", reason="spam")
            before = len(cards_of(fake))
            await rcon.join(4, "Alice", ALICE)
            await wait_for(lambda: len(cards_of(fake)) > before, timeout=15)
            assert "**Alice** is back, under a minute after being kicked (Mod: spam)" in said(fake)

    run(scenario())


def test_back_after_a_kick_counts_from_when_the_block_ends(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            kick = lambda **more: huragok.storage.record_action(kind="kick", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", **more)  # noqa: E731
            window = 15 * 60
            start = 1_000_000.0
            kick(when=start)
            assert part.kicked_lately(ALICE, window, start + 5 * 60) is not None  # still blocked: on another server
            assert part.kicked_lately(ALICE, window, start + 24 * 60) is not None  # 14 minutes after the 10
            assert part.kicked_lately(ALICE, window, start + 26 * 60) is None
            kick(when=start + 60 * 60)  # the second in a day: 30 minutes
            assert part.kicked_lately(ALICE, window, start + 60 * 60 + 44 * 60) is not None
            assert part.kicked_lately(ALICE, window, start + 60 * 60 + 46 * 60) is None
            kick(when=start + 60 * 60 + 2 * 86400)  # after two days without one, the count has started again
            assert part.kicked_lately(ALICE, window, start + 60 * 60 + 2 * 86400 + 26 * 60) is None

    run(scenario())


def test_a_pardon_means_theyre_welcome_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            start = 1_000_000.0
            huragok.storage.record_action(kind="kick", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", when=start)
            huragok.storage.record_action(kind="pardon", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", when=start + 60)
            assert part.kicked_lately(ALICE, 900, start + 120) is None

    run(scenario())


def test_a_kick_passed_on_from_another_server_isnt_a_second_kick(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            start = 1_000_000.0
            huragok.storage.record_action(kind="kick", server="Elsewhere", target_id=ALICE, target_name="Alice", by="Mod", source="commands", when=start)
            huragok.storage.record_action(kind="kick", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", evidence="kicked on Elsewhere", when=start + 60)
            huragok.storage.record_action(kind="pardon", server=SERVER, target_id=ALICE, target_name="Alice", by="Mod", source="commands", evidence="kicked on Elsewhere", when=start + 60)
            assert part.kicked_lately(ALICE, 900, start + 24 * 60) is not None  # the first's block still counts, and only as one kick
            assert part.kicked_lately(ALICE, 900, start + 26 * 60) is None

    run(scenario())


def test_a_name_like_a_staff_members(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(1, "Moderator", MOD)  # now known by that name
            await wait_for(lambda: huragok.storage.player(MOD) is not None, timeout=10)
            before = len(cards_of(fake))
            await rcon.join(2, "Moderat0r", BRAVO)
            await wait_for(lambda: "has a name like Moderator's" in said(fake), timeout=15)
            assert len(cards_of(fake)) > before
            # Their own name isn't like themself.
            mods = [text for text in said(fake).split("\n") if text.startswith("**Moderator**") and "name like" in text]
            assert mods == []

    run(scenario())


@pytest.mark.parametrize("name", ["Moderator", "MODERATOR", "M0derator", "Modérator", "Mod erator", "Moderat0r_", "Moderatol"])
def test_what_looks_the_same_at_a_glance(name: str) -> None:
    from huragok.discordbot.feeds import LOOKALIKE, skeleton

    assert difflib.SequenceMatcher(None, skeleton(name), skeleton("Moderator")).ratio() >= LOOKALIKE


def test_a_different_name_isnt_a_lookalike() -> None:
    from huragok.discordbot.feeds import LOOKALIKE, skeleton

    assert difflib.SequenceMatcher(None, skeleton("Bravo"), skeleton("Moderator")).ratio() < LOOKALIKE


def test_someone_new_on_a_quiet_server(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: "**Alice** is new here" in said(fake), timeout=15)

    run(scenario())


def test_someone_banned_on_another_server(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            huragok.storage.record_action(kind="ban", server="Elsewhere", target_id=ALICE, target_name="Alice", by="Admin", source="in game")
            assert part.banned_elsewhere(ALICE, SERVER) == ["Elsewhere"]
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: "**Alice** joined, and is banned on Elsewhere" in said(fake), timeout=15)
            huragok.storage.record_action(kind="unban", server="Elsewhere", target_id=ALICE, target_name="Alice", by="Admin", source="in game")
            assert part.banned_elsewhere(ALICE, SERVER) == []

    run(scenario())


def test_the_anti_cheat_acting(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.event("cheat", id=ALICE, name="Alice", category="invalid_game_data", detail="bad game data (ref 4F2A)", action="kicked")
            await wait_for(lambda: "The server kicked **Alice**: bad game data (ref 4F2A)" in said(fake), timeout=15)

    run(scenario())


def test_the_watch_list_can_be_narrowed_a_line_at_a_time(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "watch", body='\n')
        set_message(tmp_path / "discord.toml", "watch-first-time-quiet", post=False)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert "is new here" not in said(fake)

    run(scenario())


# Calling ---------------------------------------------------------------------------------------------------------------


def test_calladmin_tells_staff_with_the_chat_and_keeps_to_its_cooldown(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "calls")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await rcon.chat(BRAVO, "Bravo", "ha ha")
            await wait_for(lambda: len(huragok.storage.query("SELECT * FROM commands_chat")) == 1, timeout=10)
            await rcon.chat(ALICE, "Alice", "!calladmin Bravo is team killing")
            await wait_for(lambda: len(cards_of(fake)) == 1, timeout=15)
            card = cards_of(fake)[0]
            assert card["description"] == "**Alice** is calling for an admin: Bravo is team killing"
            assert "Bravo: ha ha" in card["fields"][0]["value"]
            await wait_for(lambda: any("staff have been told" in line for line in tells_to(rcon, ALICE)), timeout=10)
            await rcon.chat(ALICE, "Alice", "!calladmin again")
            await wait_for(lambda: any("can't reach anyone" in line for line in tells_to(rcon, ALICE)), timeout=10)
            assert len(cards_of(fake)) == 1

    run(scenario())


def test_with_no_discord_there_are_no_discord_commands(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A host who never set the bot up: players aren't offered what would reach nobody, and !help doesn't list it."""
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, rcon, _):  # no discord.toml: the bot is off
            await rcon.join(3, "Alice", ALICE)
            for line in ("!calladmin help", "!report Bravo", "!mods"):
                before = len(tells_to(rcon, ALICE))
                await rcon.chat(ALICE, "Alice", line)
                await wait_for(lambda: len(tells_to(rcon, ALICE)) > before, timeout=10)
                assert "Unknown command" in tells_to(rcon, ALICE)[-1], line
            before = len(tells_to(rcon, ALICE))
            await rcon.chat(ALICE, "Alice", "!help")
            await wait_for(lambda: len(tells_to(rcon, ALICE)) > before, timeout=10)
            told = " ".join(tells_to(rcon, ALICE)[before:])
            assert "!help" in told and not any(word in told for word in ("calladmin", "!report", "!mods"))
            assert fake.requests == []

    run(scenario())


def test_a_call_doesnt_claim_to_have_reached_a_refused_channel(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "calls")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.feeds.blocked[f"channel:{STAFF_CHANNEL}"] = "Discord refused it"
            await rcon.join(3, "Alice", ALICE)
            await rcon.chat(ALICE, "Alice", "!calladmin help")
            await wait_for(lambda: any("can't reach anyone" in line for line in tells_to(rcon, ALICE)), timeout=10)
            assert cards_of(fake) == []

    run(scenario())


def test_a_word_in_chat_calls_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, "calls")
        path.write_text(path.read_text(encoding="utf-8").replace("words = []                     # chat words", 'words = ["admin pls"]          # chat words'), encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.chat(ALICE, "Alice", "ADMIN PLS he is hacking")
            await wait_for(lambda: "**Alice** is calling for an admin" in said(fake), timeout=15)

    run(scenario())


def test_mods_says_who_is_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, "calls")
        async with running(tmp_path) as (huragok, rcon, _):
            await rcon.join(3, "Alice", ALICE)
            await rcon.chat(ALICE, "Alice", "!mods")
            await wait_for(lambda: any("No staff are in a game just now: !calladmin reaches them" in line for line in tells_to(rcon, ALICE)), timeout=10)
            await rcon.join(1, "Mod", MOD)
            await rcon.join(2, "Owner", OWNER)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 3, timeout=10)
            await rcon.chat(ALICE, "Alice", "!mods")
            await wait_for(lambda: any(f"Mod ({SERVER}), Owner ({SERVER})" in line for line in tells_to(rcon, ALICE)), timeout=10)

    run(scenario())


# Seeding ---------------------------------------------------------------------------------------------------------------


def test_a_server_with_a_few_on_asks_once_per_cooldown(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("status", "seeding"), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await wait_for(lambda: huragok.superintendent.fleet.rows()[0]["phase"] in ("running", "external"), timeout=15)
            await part.status.tick()
            assert cards_of(fake) == []  # nobody on: nothing to fill
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            await part.status.tick()
            await wait_for(lambda: len(cards_of(fake)) == 1, timeout=15)
            assert cards_of(fake)[0]["description"] == f"{SERVER} has 1 on: come and fill it"
            await part.status.tick()
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert len(cards_of(fake)) == 1

    run(scenario())


def test_the_cooldown_outlives_a_restart(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("status", "seeding"), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await wait_for(lambda: huragok.superintendent.fleet.rows()[0]["phase"] in ("running", "external"), timeout=15)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            part.status.store(f"seeding:{SERVER}", value=str(part.status.clock() - 60))  # a minute ago, before a restart
            await part.status.tick()
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert cards_of(fake) == []
            part.status.store(f"seeding:{SERVER}", value=str(part.status.clock() - 121 * 60))
            await part.status.tick()
            await wait_for(lambda: len(cards_of(fake)) == 1, timeout=15)

    run(scenario())


# /notify --------------------------------------------------------------------------------------------------------------


def alerts_file(root: Path) -> Path:
    path = bot_file(root)
    set_switch(path, ("status", "seeding"), "enabled", True)
    set_switch(path, ("notify",), "enabled", True)
    set_message(path, "seeding-wanted", post=False)  # the seeding post has its own tests
    return path


def pings_of(fake: FakeDiscord) -> list[dict[str, Any]]:
    return [post for post in fake.posted if post.get("allowed_mentions", {}).get("users")]


def test_notify_is_there_only_when_its_switched_on(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("status", "seeding"), "enabled", True)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok)
            await wait_for(lambda: GUILD in fake.commands)
            assert {c["name"] for c in fake.commands[GUILD]} == {"huragok"}
            set_switch(path, ("notify",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: {c["name"] for c in fake.commands[GUILD]} == {"huragok", "notify"}, timeout=15)

    run(scenario())


def test_anyone_can_ask_list_and_stop_up_to_a_limit(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        alerts_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok)
            _, token = fake.slash("notify", "players", user=OTHER, count=8)
            assert "when a server reaches 8 players" in await answer_text(fake, token)
            _, token = fake.slash("notify", "game", user=OTHER, mode="Slayer", server=SERVER)
            assert f"when Slayer starts on {SERVER}" in await answer_text(fake, token)
            _, token = fake.slash("notify", "game", user=OTHER, mode="Slayer", server="Nowhere")
            assert "no server called Nowhere" in await answer_text(fake, token)
            _, token = fake.slash("notify", "list", user=OTHER)
            said = await answer_text(fake, token)
            assert "Any server reaching 8 players" in said and f"Slayer starting on {SERVER}" in said
            for count in (9, 10, 11):
                _, token = fake.slash("notify", "players", user=OTHER, count=count)
                await answer_text(fake, token)
            _, token = fake.slash("notify", "players", user=OTHER, count=12)
            assert "the most you can have" in await answer_text(fake, token)
            _, token = fake.slash("notify", "stop", user=OTHER)
            assert "Stopped 5" in await answer_text(fake, token)

    run(scenario())


def test_reaching_a_number_pings_who_asked_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        alerts_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.alerts.add(OTHER, "players", "2", "")
            part.alerts.add(OWNER_USER, "players", "2", SERVER)
            part.alerts.add("3999", "players", "3", "")
            part.alerts.add("3998", "players", "2", "Elsewhere")
            part.alerts.tick()  # its first look crosses nothing: a restart pings nobody
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 1, timeout=10)
            part.alerts.tick()
            assert pings_of(fake) == []
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            part.alerts.tick()
            await wait_for(lambda: len(pings_of(fake)) == 1, timeout=15)
            post = pings_of(fake)[0]
            assert sorted(post["allowed_mentions"]["users"]) == sorted([OTHER, OWNER_USER])
            assert f"<@{OTHER}>" in post["content"] and "flags" not in post  # a ping that's silent isn't a ping
            assert post["embeds"][0]["description"] == f"{SERVER} has reached 2 players"
            await rcon.leave(4)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 1, timeout=10)
            part.alerts.tick()
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            part.alerts.tick()  # back up within the cooldown
            await wait_for(lambda: part.outbox.waiting == 0, timeout=10)
            assert len(pings_of(fake)) == 1

    run(scenario())


def test_a_game_starting_pings_who_asked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        alerts_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            part.alerts.add(OTHER, "game", "slayer", "")
            part.alerts.add("3999", "game", "Infection", "")
            await rcon.event("phase", phase="playing", map="Guardian", mode="Team Slayer")
            await wait_for(lambda: len(pings_of(fake)) == 1, timeout=15)
            post = pings_of(fake)[0]
            assert post["allowed_mentions"]["users"] == [OTHER]
            assert post["embeds"][0]["description"] == f"Team Slayer has started on {SERVER}, on Guardian"

    run(scenario())


def test_every_line_has_a_colour_in_the_palette(tmp_path: Path) -> None:
    from huragok.discordbot.config import load

    config = load(tmp_path / "none")
    palette = config.section("colours")
    assert [key for key, line in config.section("messages").items() if line.get("colour") not in palette] == []


def test_every_new_line_is_in_the_file(tmp_path: Path) -> None:
    from huragok.discordbot.config import load
    from huragok.discordbot.feeds import sample_cards

    config = load(tmp_path / "none")
    assert not config.warnings
    assert {card.message for card in sample_cards(config)} == set(config.section("messages"))
