"""Every in-game command as a slash command: made from the commands, and running just as they do in game."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from conftest import ALICE, BRAVO, MOD
from test_bot_running import OTHER
from testkit.fake_discord import ADMIN_ROLE, GUILD, MOD_ROLE, OWNER_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.discordbot.config import add_owner, make_file, set_channel, set_guild, set_mode, set_switch, set_token
from huragok.core.commandsfile import all_commands
from huragok.discordbot.slash import NOT_FROM_DISCORD, description_of, plan_of

PROBATION_ROLE = "5103"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.roles.append(FakeDiscord._role(PROBATION_ROLE, "SPR-Probation", 1, "0"))
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *, on: bool = True, change: tuple[str, str] | None = None) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    set_channel(path, "logs", STAFF_CHANNEL, "logs")
    set_switch(path, ("commands",), "enabled", on)
    text = path.read_text(encoding="utf-8")
    text = text.replace("\nadmin = []", f'\nadmin = ["{ADMIN_ROLE}"]').replace("\nmoderator = []", f'\nmoderator = ["{MOD_ROLE}"]')
    text = text.replace("probationary_moderator = []", f'probationary_moderator = ["{PROBATION_ROLE}"]')
    if change:
        assert change[0] in text
        text = text.replace(*change)
    path.write_text(text, encoding="utf-8")
    return path


async def up(huragok: Any, fake: FakeDiscord) -> Any:
    part = huragok.runners["discord"].part
    await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
    await wait_for(lambda: GUILD in fake.commands, timeout=15)
    return part


def registered(fake: FakeDiscord) -> dict[str, dict[str, Any]]:
    return {command["name"]: command for command in fake.commands[GUILD]}


async def said(fake: FakeDiscord, token: str) -> dict[str, Any]:
    """What they were told: as a follow-up, or straight off if it was refused before anything was shown."""

    def told() -> dict[str, Any] | None:
        for followup in fake.followups:
            if followup["token"] == token:
                return followup
        for callback in fake.callbacks:
            if callback["token"] == token and callback.get("type") == 4:
                return callback["data"]
        return None

    await wait_for(lambda: told() is not None, timeout=15)
    answer = told()
    assert answer is not None
    return answer


async def and_yes(fake: FakeDiscord, opened: str, token: str, **who: Any) -> dict[str, Any]:
    """It asks first, as the console does: Yes, and what came of it."""
    await wait_for(lambda: any(c["token"] == token and c.get("type") == 4 for c in fake.callbacks), timeout=10)
    asked = next(c for c in fake.callbacks if c["token"] == token)["data"]
    yes = next(item["custom_id"] for row in asked["components"] for item in row["components"] if item["label"] == "Yes")
    _, token = fake.pick(opened, yes, 2, [], **who)
    await wait_for(lambda: any(f["token"] == token for f in fake.followups), timeout=15)
    return next(f for f in fake.followups if f["token"] == token)


def test_they_come_and_go_with_the_switch(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, on=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await up(huragok, fake)
            assert set(registered(fake)) == {"huragok"}
            set_switch(path, ("commands",), "enabled", True)
            await part.reload(force=True)
            await wait_for(lambda: "mute" in registered(fake), timeout=15)
            assert {"kick", "ban", "whois", "bans", "restartserver"} <= set(registered(fake))
            assert not (NOT_FROM_DISCORD - {"help", "report"}) & set(registered(fake))  # those two are Discord's own there
            assert len(registered(fake)) <= 100  # Discord's limit
            set_switch(path, ("commands",), "enabled", False)
            await part.reload(force=True)
            await wait_for(lambda: set(registered(fake)) == {"huragok"}, timeout=15)

    run(scenario())


def test_a_command_has_the_options_it_takes(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, fake)
            commands = registered(fake)
            options = {option["name"]: option for option in commands["mute"]["options"]}
            assert list(options) == ["player", "time", "reason", "server"]
            assert options["player"]["required"] is True and options["player"]["autocomplete"] is True
            assert options["time"]["required"] is False and options["time"]["autocomplete"] is True
            assert options["reason"]["required"] is False and not options["reason"].get("autocomplete")
            assert [option["name"] for option in commands["whois"]["options"]][:1] == ["player"]
            assert next(option for option in commands["players"]["options"] if option["name"] == "name").get("required", False) is False  # !players [name]
            assert next(option for option in commands["say"]["options"] if option["name"] == "text")["required"] is True  # !say <text>
            assert [option["name"] for option in commands["endgame"].get("options", [])] == ["server"]
            assert not commands["mods"].get("options")  # it's about the whole of Huragok
            assert [choice["value"] for choice in next(option for option in commands["team"]["options"] if option["name"] == "team")["choices"]][:2] == ["red", "blue"]
            assert next(option for option in commands["said"]["options"] if option["name"] == "lines")["type"] == 4

    run(scenario())


def test_every_command_gets_boxes_that_mean_something() -> None:
    """No raw usage for a description, nothing cut mid-word, and nothing that isn't a choice offered as one."""
    for spec in all_commands().values():
        assert len(description_of(spec)) <= 100, spec.name
        plan = plan_of(spec)
        assert [option.required for option in plan] == sorted((option.required for option in plan), reverse=True), spec.name
        for option in plan:
            assert not set("<>[]|") & set(option.description) or option.name == "words", (spec.name, option)
    made = {name: {option.name: option for option in plan_of(spec)} for name, spec in all_commands().items()}
    assert not made["pardonplayer"]["rule"].choices and not made["strike"]["rule"].choices  # these are a rule's name, not the words "rule" and "all"
    assert not made["rules"]["search"].choices
    assert made["strike"]["rule"].required and made["strike"]["reason"].kind == "reason"
    assert made["respawn"]["player"].required  # from Discord there's no "me"
    assert made["load"]["map"].kind == made["load"]["mode"].kind == "one"
    assert made["maxping"]["limit"].name == "limit"


def test_staffs_only_show_to_those_with_the_permission(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, fake)
            commands = registered(fake)
            assert int(commands["mute"]["default_member_permissions"]) == 1 << 13  # Manage Messages
            assert commands["mods"].get("default_member_permissions") is None  # for everyone

    run(scenario())


def test_names_and_hidden_are_settings(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=("names = {}                     # rename any", 'names = { whois = "lookup" }  # rename any'))
        (tmp_path / "automod.txt").write_text("insults | 10m | 1h | perm\n", encoding="utf-8")  # some of them are the word list's
        path = tmp_path / "discord.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("hidden = []                    # never registered", 'hidden = ["endgame"]'), encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, fake)
            commands = registered(fake)
            assert "lookup" in commands and "whois" not in commands and "endgame" not in commands
            assert {"requests", "bannedwords", "bannednames", "letback", "clearstrikes", "reloadserver"} <= set(commands)  # where the game's own name would mislead
            assert not {"reports", "rules", "bannames", "pardon", "pardonplayer", "reload", "servers", "strikes"} & set(commands)

    run(scenario())


def test_a_mute_from_discord_is_the_games_own_mute(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("mute", player=ALICE, time="30m", reason="shouting in voice")
            await said(fake, token)
            assert rcon.named("mute")[0]["args"][:2] == [ALICE, "30m"]
            row = huragok.storage.query("SELECT * FROM actions WHERE kind = 'mute'")[0]
            assert row["by"] == f"user{OWNER_USER}" and row["reason"] == "shouting in voice"

    run(scenario())


def test_a_player_can_be_typed_by_name_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", player="Alice", reason="spam")
            await said(fake, token)
            assert rcon.named("kick")[0]["args"][0] == ALICE

    run(scenario())


def test_the_rank_is_the_games(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", user=OTHER, player=ALICE)  # no rank at all
            assert "for moderators and up" in (await said(fake, token))["content"]
            _, token = fake.command("ban", user=OTHER, roles=(MOD_ROLE,), player=ALICE)  # a moderator, where it's admins'
            assert "for admins and up" in (await said(fake, token))["content"]
            assert rcon.named("kick") == [] and rcon.named("ban") == []
            opened, token = fake.command("ban", user=OTHER, roles=(ADMIN_ROLE,), player=ALICE, reason="cheating")
            await and_yes(fake, opened, token, user=OTHER, roles=(ADMIN_ROLE,))
            assert rcon.named("ban")

    run(scenario())


def test_nobody_acts_on_their_own_rank_or_above(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(2, "Mod", MOD)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", user=OTHER, roles=(MOD_ROLE,), player=MOD)
            assert "can't" in (await said(fake, token))["content"]
            assert rcon.named("kick") == []

    run(scenario())


def test_on_probation_its_a_request(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE, reason="spam")
            answer = await said(fake, token)
            assert rcon.named("kick") == []
            assert "#1" in answer["content"] or "request" in answer["content"].lower()
            assert huragok.storage.query("SELECT * FROM commands_requests")
            _, token = fake.command("whois", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE)  # looking is looking
            assert "Alice" in (await said(fake, token))["content"]

    run(scenario())


def test_which_server_is_worked_out_or_asked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            _, token = fake.command("endgame", server="Nowhere")
            assert "no server called Nowhere" in (await said(fake, token))["content"]
            opened, token = fake.command("endgame")  # there's only the one
            await and_yes(fake, opened, token)
            assert rcon.named("endgame")

    run(scenario())


def test_answers_are_private_unless_the_setting_says(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=("public = []                    # commands whose answer", 'public = ["kick"]              # commands whose answer'))
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            for private in ("whois", "players", "mods"):
                _, token = fake.command(private, **({"player": ALICE} if private == "whois" else {}))
                assert (await said(fake, token)).get("flags") == 64, private
            _, token = fake.command("kick", player=ALICE)
            assert not (await said(fake, token)).get("flags")
            await wait_for(lambda: huragok.storage.query("SELECT * FROM discord_tidy"), timeout=5)  # and it doesn't stay in the channel for ever
            row = huragok.storage.query("SELECT * FROM discord_tidy")[0]
            assert 590 < row["due"] - part.status.clock() <= 600

    run(scenario())


def test_a_reason_is_only_ever_a_reason(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Two boxes, so what's in one can't be read as the other: the words a reason starts with don't change what the command does."""

    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("mute", player=ALICE, reason="30m of shouting")
            await said(fake, token)
            row = huragok.storage.query("SELECT * FROM actions WHERE kind = 'mute'")[0]
            assert row["reason"] == "30m of shouting" and not row["duration"]
            opened, token = fake.command("ban", player=ALICE, reason="7d of cheating, she said \"never\"")
            await and_yes(fake, opened, token)
            row = huragok.storage.query("SELECT * FROM actions WHERE kind = 'ban'")[0]
            assert not row["duration"] and row["reason"] == "7d of cheating, she said 'never'"

    run(scenario())


def test_a_time_it_cant_read_stops_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", player=ALICE, time="a while", reason="spam")
            answer = await said(fake, token)
            assert "Nothing was done" in answer["content"] and answer["flags"] == 64
            assert rcon.named("kick") == [] and rcon.named("ban") == []
            _, token = fake.command("kick", player=ALICE, time="2 hours", reason="spam")  # as anyone might write it
            await wait_for(lambda: huragok.storage.query("SELECT duration FROM actions WHERE kind = 'ban'"), timeout=10)
            assert huragok.storage.query("SELECT duration FROM actions WHERE kind = 'ban'")[0]["duration"] == "2h"

    run(scenario())


def test_a_refusal_is_private_even_on_a_public_command(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, change=("public = []                    # commands whose answer", 'public = ["kick"]              # commands whose answer'))
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("kick", user=OTHER, player=ALICE)
            answer = await said(fake, token)
            assert answer["flags"] == 64 and "/kick is for moderators and up" in answer["content"]
            assert not rcon.named("kick")

    run(scenario())


def test_what_the_console_asks_first_its_asked_here_too(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            opened, token = fake.command("endgame")
            await wait_for(lambda: any(c["token"] == token and c.get("type") == 4 for c in fake.callbacks), timeout=10)
            asked = next(c for c in fake.callbacks if c["token"] == token)["data"]
            assert asked["flags"] == 64 and "endgame" in asked["content"] and not rcon.named("endgame")
            buttons = [item for row in asked["components"] for item in row["components"]]
            fake.pick(opened, next(button for button in buttons if button["label"] == "Yes")["custom_id"], 2, [])
            await wait_for(lambda: rcon.named("endgame"), timeout=10)

    run(scenario())


def test_a_list_isnt_cut_short_for_game_chat(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            for number in range(1, 15):
                await rcon.join(number, f"Player{number}", f"{number:032x}")
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 14, timeout=10)
            _, token = fake.command("players")
            answer = await said(fake, token)
            assert "Player14" in answer["content"] and "more:" not in answer["content"]
            assert "\n" in answer["content"]

    run(scenario())


def test_a_long_answer_is_a_file_with_no_address_in_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot import cards

        assert cards.no_addresses("seen from 203.0.113.9:11774 then") == "seen from (hidden) then"
        assert "​" not in cards.clean("#3 @1", for_a_channel=False)
        assert "​" in cards.clean("@everyone")

    run(scenario())


def test_a_toggle_says_what_it_did(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            _, token = fake.command("toggleglobal", state="off", time="10m")
            answer = await said(fake, token)
            assert "lobal" in answer["content"] and "Done." not in answer["content"]

    run(scenario())


def test_one_answer_for_each_server_when_none_is_named(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=("Red", "Blue")) as (huragok, consoles):
            await up(huragok, fake)
            await consoles["Red"].join(3, "Alice", ALICE)
            await consoles["Blue"].join(4, "Bravo", BRAVO)
            await wait_for(lambda: huragok.superintendent.states["Blue"].players and huragok.superintendent.states["Red"].players, timeout=10)
            _, token = fake.command("players")
            shown = (await said(fake, token))["content"]
            assert "**Red**" in shown and "**Blue**" in shown and "Alice" in shown and "Bravo" in shown
            _, token = fake.command("players", server="Red")
            shown = (await said(fake, token))["content"]
            assert "Alice" in shown and "Bravo" not in shown
            _, token = fake.command("endgame")  # this one's a change: it isn't guessed, or done to all of them
            answer = await said(fake, token)
            assert "Which server" in answer["content"] and not consoles["Red"].named("endgame") and not consoles["Blue"].named("endgame")

    run(scenario())


def test_a_name_on_two_servers_isnt_guessed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=("Red", "Blue")) as (huragok, consoles):
            await up(huragok, fake)
            await consoles["Red"].join(3, "Bob", ALICE)
            await consoles["Blue"].join(4, "Bob", BRAVO)
            await wait_for(lambda: huragok.superintendent.states["Blue"].players and huragok.superintendent.states["Red"].players, timeout=10)
            _, token = fake.command("kick", player="Bob")
            assert "More than one" in (await said(fake, token))["content"]
            assert not consoles["Red"].named("kick") and not consoles["Blue"].named("kick")
            _, token = fake.command("kick", player=BRAVO)  # picked from the list: it goes where they are
            await said(fake, token)
            assert consoles["Blue"].named("kick") and not consoles["Red"].named("kick")

    run(scenario())


def test_an_unmute_goes_where_they_are_now(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with fleet(tmp_path, up=("Red", "Blue")) as (huragok, consoles):
            await up(huragok, fake)
            await consoles["Red"].join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states["Red"].players, timeout=10)
            _, token = fake.command("mute", player=ALICE, reason="spam")
            await said(fake, token)
            await consoles["Red"].leave(3)
            await consoles["Blue"].join(4, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states["Blue"].players, timeout=10)
            _, token = fake.command("unmute", player=ALICE)
            await said(fake, token)
            assert consoles["Blue"].named("unmute")

    run(scenario())


def test_help_lists_what_their_rank_can_use(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await up(huragok, fake)
            _, token = fake.command("help", user=OTHER)
            everyone = (await said(fake, token))["content"]
            assert "`/mods`" in everyone and "`/kick`" not in everyone
            _, token = fake.command("help", user=OTHER, roles=(MOD_ROLE,))
            moderator = (await said(fake, token))["content"]
            assert "`/kick`" in moderator and "`/ban`" not in moderator
            _, token = fake.command("help", command="/mute")
            one = (await said(fake, token))["content"]
            assert "/mute <player> [time] [reason]" in one

    run(scenario())


def test_a_banned_name_is_the_name_even_when_picked_from_the_list(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        (tmp_path / "automod.txt").write_text("insults | 10m | 1h | perm\n", encoding="utf-8")
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.command("banname", player=ALICE)
            await said(fake, token)
            found = [path for path in tmp_path.rglob("names.txt") if "Alice" in path.read_text(encoding="utf-8")]
            assert found and ALICE not in found[0].read_text(encoding="utf-8")

    run(scenario())


def test_an_answer_cant_ping(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "@everyone", BRAVO)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            _, token = fake.command("players")
            answer = await said(fake, token)
            assert answer["allowed_mentions"]["parse"] == []  # shown as written, so a name can be read and copied, and it can't ping

    run(scenario())


def test_only_staff_are_offered_players(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok, fake)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)

            async def choices(token: str) -> list[dict[str, Any]]:
                await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
                return next(c for c in fake.callbacks if c.get("token") == token)["data"]["choices"]

            _, token = fake.typing_command("mute", "player", "lic")
            assert [c["value"] for c in await choices(token)] == [ALICE]
            _, token = fake.typing_command("mute", "player", "lic", user=OTHER)
            assert await choices(token) == []

    run(scenario())


def test_a_command_switched_off_in_commands_toml_isnt_offered(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        commands = 'prefix = "!"\n\n[moderator]\nplayers = []\ncommands = ["kick"]\n\n[off]\ncommands = ["mute"]\n'
        async with running(tmp_path, commands=commands) as (huragok, _, _):
            await up(huragok, fake)
            assert "mute" not in registered(fake) and "kick" in registered(fake)

    run(scenario())
