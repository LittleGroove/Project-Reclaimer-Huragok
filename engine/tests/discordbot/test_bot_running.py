"""The bot running against a fake Discord: signing in, /huragok, the setup's panels, discord.toml live."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterator

import pytest

from testkit.fake_discord import (
    ADMIN_ROLE, GUILD, OWNER_USER, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord,
)
from testkit.running import running, wait_for

from huragok.core.tomledit import read_toml
from huragok.discordbot import records
from huragok.discordbot.config import add_owner, make_file, set_guild, set_mode, set_token

OTHER = "3333"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path, *, guild: bool = True, owner: bool = True, token: str = TOKEN) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, token)
    set_mode(path, "bot")
    if guild:
        set_guild(path, GUILD, "Test Discord")
    if owner:
        add_owner(path, OWNER_USER, "the host")
    return path


def part_of(huragok: Any) -> Any:
    return huragok.runners["discord"].part


async def online(huragok: Any) -> Any:
    part = part_of(huragok)
    await wait_for(lambda: part.state == "online", timeout=20)
    return part


async def answered(fake: FakeDiscord, token: str) -> dict[str, Any]:
    """The answer to an interaction: a follow-up's, or its own callback's form."""
    def found() -> dict[str, Any] | None:
        for followup in fake.followups:
            if followup["token"] == token:
                return followup
        for callback in fake.callbacks:
            if callback["token"] == token and callback.get("type") != 5:
                return callback.get("data") or {}
        return None

    await wait_for(lambda: found() is not None, timeout=15)
    return found() or {}


async def answer_text(fake: FakeDiscord, token: str) -> str:
    return str((await answered(fake, token)).get("content"))


def test_a_new_file_changes_nothing_and_connects_nowhere(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            assert part.state == "off" and part.client is None
            assert not (tmp_path / "discord.toml").exists()  # only the first start or an update writes it
            await asyncio.sleep(0)
        assert fake.identifies == [] and fake.requests == []

    run(scenario())


def test_the_bot_signs_in_says_who_it_is_and_registers_its_commands_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            assert part.bot_name.startswith("Huragok Test") and part.guild_name == "Test Discord"
            state = records.read_state(tmp_path / "records")
            assert state["state"] == "online" and state["mode"] == "bot"
            await wait_for(lambda: fake.registrations == 1)
            names = [command["name"] for command in fake.commands[GUILD]]
            assert names == ["huragok"]
            subcommands = {option["name"] for option in fake.commands[GUILD][0]["options"]}
            assert subcommands == {"claim", "setup", "features", "check", "test", "reload"}
        assert records.logins_today(tmp_path / "records") == 1
        assert fake.identifies[0]["intents"] & (1 << 15) == 0  # nothing asks for Message Content yet
        # The next start: the same list, so nothing is registered (200 a day), though it signs in again.
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            await asyncio.sleep(0.2)
        assert fake.registrations == 1 and len(fake.identifies) == 2

    run(scenario())


def test_the_token_goes_nowhere_but_discord(tmp_path: Path, run: Any, fake: FakeDiscord, caplog: Any) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            assert TOKEN not in repr(part.file)
        assert TOKEN not in (tmp_path / "records" / records.STATE_FILE).read_text(encoding="utf-8")
        assert TOKEN not in caplog.text

    run(scenario())


def test_a_refused_token_stops_it_without_spending_a_login_and_a_good_one_brings_it_back(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, token="wrong.token")
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            await wait_for(lambda: part.state == "waiting" and "refused the token" in part.why, timeout=15)
            assert fake.identifies == []
            await wait_for(lambda: any("refused the token" in line for line in huragok.problems))
            set_token(path, TOKEN)
            await wait_for(lambda: part.state == "online", timeout=20)
            await wait_for(lambda: not any("refused the token" in line for line in huragok.problems))

    run(scenario())


def test_a_name_in_the_file_becomes_its_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        path.write_text(path.read_text(encoding="utf-8").replace('staff = ""', 'staff = "#admin-log"'), encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            await wait_for(lambda: part.file.section("channels")["staff"] == STAFF_CHANNEL, timeout=15)

    run(scenario())


def test_invited_without_the_commands_scope_it_says_so_and_tries_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        fake.refuse_next.append(("/commands", 403, {"message": "Missing Access", "code": 50001}, {}))
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            await wait_for(lambda: part.state == "waiting" and "applications.commands" in part.why, timeout=20)
            await wait_for(lambda: any("applications.commands" in line for line in huragok.problems))
            assert part.client.commands_retry_at is not None
            part.client.commands_retry_at = 0.0  # time has passed; and it's been invited properly
            await wait_for(lambda: part.state == "online", timeout=15)
            assert fake.registrations == 1 and part.client.commands_retry_at is None

    run(scenario())


def test_joining_its_discord_later_brings_it_up(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path, guild=False)
        fake.in_guilds = []
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            assert fake.registrations == 0
            fake.in_guilds = [GUILD]
            fake.dispatch("GUILD_CREATE", {
                **fake.guild, "roles": fake.roles, "channels": fake.channels, "members": [], "threads": [], "presences": [],
                "voice_states": [], "stage_instances": [], "guild_scheduled_events": [], "soundboard_sounds": [],
                "member_count": 3, "large": False, "joined_at": "2026-10-30T00:00:00+00:00"})  # no "unavailable": a new one
            await wait_for(lambda: fake.registrations == 1, timeout=15)
            assert part.state == "online"

    run(scenario())


def test_past_the_logins_a_day_it_stays_off(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        for _ in range(100):
            records.count_login(tmp_path / "records")
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            await wait_for(lambda: part.state == "waiting" and "logins_per_day" in part.why, timeout=15)
            assert fake.identifies == []

    run(scenario())


def test_going_to_webhook_mode_signs_the_bot_out(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            set_mode(path, "webhook")
            await wait_for(lambda: part.state == "webhook", timeout=15)
            assert part.client is None

    run(scenario())


def test_a_broken_file_on_a_reload_keeps_what_is_in_force_and_says_so(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            good = path.read_text(encoding="utf-8")
            path.write_text(good + "\n[broken\n", encoding="utf-8")
            await wait_for(lambda: any("not reloaded" in line for line in huragok.problems), timeout=15)
            assert part.state == "online" and part.client is not None and part.file.mode == "bot"
            path.write_text(good, encoding="utf-8")
            await wait_for(lambda: not any("not reloaded" in line for line in huragok.problems), timeout=15)

    run(scenario())


def test_a_broken_file_at_the_start_leaves_the_bot_off_and_the_servers_alone(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        (tmp_path / "discord.toml").write_text("mode = [", encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            assert part.state == "off" and "isn't valid TOML" in part.why
            assert any("Discord bot off" in line for line in huragok.problems)
            assert huragok.runners["discord"].enabled

    run(scenario())


# /huragok ------------------------------------------------------------------------------------------------------


def test_claim_makes_an_owner_once_and_picks_the_discord(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path, guild=False, owner=False)
        async with running(tmp_path) as (huragok, _, _):
            part = await online(huragok)
            await wait_for(lambda: GUILD in fake.commands)  # with no Discord picked, it's where the bot is
            _, token = fake.huragok("setup", user=OTHER)
            assert "Only the owners" in await answer_text(fake, token)
            code = records.new_code(tmp_path / "records")
            _, token = fake.huragok("claim", user=OTHER, code="WRONG-CODE")
            assert "isn't right" in await answer_text(fake, token)
            _, token = fake.huragok("claim", user=OTHER, code=code.lower())
            assert "You're an owner" in await answer_text(fake, token)
            data = read_toml(path)
            assert data["ranks"]["owners"] == [OTHER] and data["guild"] == GUILD
            assert part.file.guild == GUILD
            text = path.read_text(encoding="utf-8")
            assert f'guild = "{GUILD}"  # Test Discord' in text
            _, token = fake.huragok("claim", user="4444", code=code)  # once
            assert "isn't right" in await answer_text(fake, token)
            _, token = fake.huragok("reload", user=OTHER)
            assert "read again" in await answer_text(fake, token)

    run(scenario())


def test_another_discord_gets_nothing_from_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            _, token = fake.huragok("check", guild="7777")
            assert "another Discord" in await answer_text(fake, token)

    run(scenario())


def test_a_role_alone_is_never_an_owner(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        text = path.read_text(encoding="utf-8").replace("admin = []", f'admin = ["{ADMIN_ROLE}"]')
        path.write_text(text, encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            _, token = fake.huragok("setup", user=OTHER, roles=(ADMIN_ROLE,))
            assert "Only the owners" in await answer_text(fake, token)

    run(scenario())


def test_the_check_says_what_is_wrong_first(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        fake.bot_public = True
        path.write_text(path.read_text(encoding="utf-8").replace('staff = ""', 'staff = "no-such-channel"'), encoding="utf-8")
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            _, token = fake.huragok("check")
            card = (await answered(fake, token))["embeds"][0]
            assert "to fix" in card["title"]
            description = card["description"]
            assert "Public Bot" in description and "no-such-channel" in description
            assert description.index("⚠️") < description.index("✅")

    run(scenario())


def test_the_test_posts_as_each_persona_through_the_bots_webhook(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            _, token = fake.huragok("test", channel=STAFF_CHANNEL)
            assert "on their way" in await answer_text(fake, token)
            await wait_for(lambda: len(fake.posted) == 3, timeout=15)
            assert {post["username"] for post in fake.posted} == {"Superintendent", "Vergil", "Huragok"}
            assert all(post["via"] == "webhook" and post["channel"] == STAFF_CHANNEL for post in fake.posted)
            assert all(post["allowed_mentions"]["parse"] == [] for post in fake.posted)
            assert len(fake.webhooks) == 1  # made once, for that channel

    run(scenario())


def test_without_manage_webhooks_it_posts_as_itself(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            fake.refuse_next.append(("/webhooks", 403, {"message": "Missing Permissions", "code": 50013}, {}))
            _, token = fake.huragok("test")
            await answer_text(fake, token)
            await wait_for(lambda: len(fake.posted) == 3, timeout=15)
            assert all(post["via"] == "bot" for post in fake.posted)
            assert fake.posted[0]["content"].startswith("**")

    run(scenario())


# The panels ----------------------------------------------------------------------------------------------------


def _components(form: dict[str, Any]) -> list[dict[str, Any]]:
    return [component for row in form.get("components", []) for component in row.get("components", [])]


async def _opened(fake: FakeDiscord, command: str) -> tuple[str, dict[str, Any]]:
    opened, token = fake.huragok(command)
    return opened, await answered(fake, token)


def _role_resolved(role_id: str, name: str) -> dict[str, Any]:
    return {"roles": {role_id: FakeDiscord._role(role_id, name, 3, "0")}}


def test_setup_writes_what_is_picked_to_the_file_comments_kept(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            opened, form = await _opened(fake, "setup")
            assert form["embeds"][0]["title"] == "Huragok's bot"
            labels = {c.get("label"): c["custom_id"] for c in _components(form) if c.get("label")}
            assert set(labels) == {"Ranks", "Owners", "Channels", "Features", "Check"}
            before = len(fake.callbacks)
            fake.pick(opened, labels["Ranks"], 2, [])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            ranks_form = fake.callbacks[-1]["data"]
            assert ranks_form["embeds"][0]["title"] == "Ranks"
            selects = [c for c in _components(ranks_form) if c["type"] == 6]
            admin = next(c for c in selects if c["placeholder"].startswith("Admins"))
            before = len(fake.callbacks)
            fake.pick(opened, admin["custom_id"], 6, [ADMIN_ROLE], resolved=_role_resolved(ADMIN_ROLE, "SPR-Admin"))
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert read_toml(path)["ranks"]["admin"] == [ADMIN_ROLE]
            text = path.read_text(encoding="utf-8")
            assert f'"{ADMIN_ROLE}",  # @SPR-Admin' in text
            assert "# Who's who: Discord roles onto Huragok's ranks" in text
            # What's picked is shown picked the next time.
            again = next(c for c in _components(fake.callbacks[-1]["data"]) if c.get("placeholder", "").startswith("Admins"))
            assert again["default_values"] == [{"id": int(ADMIN_ROLE), "type": "role"}]

    run(scenario())


def test_the_features_panel_switches_what_is_built_and_says_what_is_coming(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            await online(huragok)
            opened, form = await _opened(fake, "features")
            assert "🔜" in form["embeds"][0]["description"]
            menu = next(c for c in _components(form) if c["type"] == 3)
            before = len(fake.callbacks)
            fake.pick(opened, menu["custom_id"], 3, ["feed.moderation"])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert "later beta" in fake.callbacks[-1]["data"]["content"]
            assert read_toml(path)["feed"]["moderation"]["enabled"] is False
            before = len(fake.callbacks)
            fake.pick(opened, menu["custom_id"], 3, ["personas"])
            await wait_for(lambda: len(fake.callbacks) > before, timeout=15)
            assert read_toml(path)["personas"]["enabled"] is False

    run(scenario())


