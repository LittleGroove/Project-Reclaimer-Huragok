"""Discord's web API as rest.py reads it, what the bot may do where (facts.py), and who someone is (ranks.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from testkit.fake_discord import (
    ADMIN_ROLE, BOT_ROLE, GUILD, MOD_ROLE, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord,
)

from huragok.discordbot import rest
from huragok.discordbot.config import load, set_rank_roles
from huragok.discordbot.facts import ALL_PERMISSIONS, Facts, gather
from huragok.discordbot.ranks import Someone, can_act, is_owner, rank_of
from huragok.discordbot.rest import PERMISSIONS as P


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


# rest.py -------------------------------------------------------------------------------------------------------


def test_a_bad_token_is_said_and_never_repeated_in_the_error(fake: FakeDiscord) -> None:
    with pytest.raises(rest.BadToken) as caught:
        rest.bot_user("not.the.token")
    assert caught.value.status == 401 and "not.the.token" not in str(caught.value)


def test_slow_down_says_how_long_and_whom(fake: FakeDiscord) -> None:
    fake.refuse_next.append(("/users/@me", 429, {"message": "slow", "retry_after": 2.5, "global": False}, {}))
    with pytest.raises(rest.SlowDown) as caught:
        rest.bot_user(TOKEN)
    assert caught.value.retry_after == 2.5 and caught.value.everything is False
    fake.refuse_next.append(("/users/@me", 429, {"message": "slow", "retry_after": 9, "global": True}, {"Retry-After": "9"}))
    with pytest.raises(rest.SlowDown) as caught:
        rest.bot_user(TOKEN)
    assert caught.value.retry_after == 9 and caught.value.everything is True


def test_refusals_and_trouble_are_told_apart(fake: FakeDiscord) -> None:
    fake.refuse_next.append(("/users/@me", 403, {"message": "Missing Access", "code": 50001}, {}))
    with pytest.raises(rest.Refused) as refused:
        rest.bot_user(TOKEN)
    assert refused.value.code == 50001 and "Missing Access" in str(refused.value)
    fake.refuse_next.append(("/users/@me", 503, {"message": "down"}, {}))
    with pytest.raises(rest.Unreachable):
        rest.bot_user(TOKEN)


def test_nobody_home_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import free_port

    monkeypatch.setattr(rest, "API", f"http://127.0.0.1:{free_port()}/api/v10")
    with pytest.raises(rest.Unreachable):
        rest.bot_user(TOKEN)


@pytest.mark.parametrize("url", ["https://discord.com/api/webhooks/123/abc-DEF_1", "https://canary.discord.com/api/webhooks/1/x",
                                 "https://discordapp.com/api/v10/webhooks/1/x", "https://ptb.discord.com/api/webhooks/1/x/"])
def test_a_webhooks_address(url: str) -> None:
    assert rest.valid_webhook(url)


@pytest.mark.parametrize("url", ["http://discord.com/api/webhooks/1/x", "https://example.com/api/webhooks/1/x",
                                 "https://discord.com/api/webhooks/x/y", "not a url", ""])
def test_not_a_webhooks_address(url: str) -> None:
    assert not rest.valid_webhook(url)
    with pytest.raises(rest.Refused):
        rest.webhook_info(url)


def test_the_invite_asks_for_what_it_needs_and_where() -> None:
    url = rest.invite_url("77", 3072, "42")
    assert "client_id=77" in url and "permissions=3072" in url and "guild_id=42" in url
    assert "scope=bot+applications.commands" in url


def test_reply_says_how_much_of_the_limit_is_left(fake: FakeDiscord) -> None:
    reply = rest.post_message(TOKEN, PUBLIC_CHANNEL, {"content": "hi"})
    assert reply.remaining == 4 and reply.reset_after == 1.0 and reply.data["content"] == "hi"


# facts.py ------------------------------------------------------------------------------------------------------


def test_gather_reads_the_bot_and_its_discord(fake: FakeDiscord) -> None:
    facts = gather(TOKEN, GUILD)
    assert facts.user["username"] == "Huragok Test" and facts.guild["name"] == "Test Discord"
    assert facts.intent_on("message_content") and facts.intent_on("members") and not facts.intent_on("presence")
    assert [c["name"] for c in facts.message_channels()] == ["admin-log", "servers"]


def test_a_discord_the_bot_is_not_in_is_only_absent(fake: FakeDiscord) -> None:
    facts = gather(TOKEN, "9999")
    assert facts.guild is None and facts.roles == [] and facts.guilds


def test_roles_and_channels_by_id_or_name(fake: FakeDiscord) -> None:
    facts = gather(TOKEN, GUILD)
    assert facts.find_role(ADMIN_ROLE)["name"] == "SPR-Admin" == facts.find_role("@spr-admin")["name"]
    assert facts.find_channel(STAFF_CHANNEL)["name"] == "admin-log" == facts.find_channel("#ADMIN-LOG")["name"]
    assert facts.find_role("nothing") is None and facts.find_channel("") is None


def _guild(everyone: int = 0, own: int = 0, *, owner: str = "1") -> Facts:
    return Facts(user={"id": "9"}, guild={"id": "100", "owner_id": owner},
                 roles=[{"id": "100", "permissions": str(everyone), "position": 0},
                        {"id": "200", "permissions": str(own), "position": 5},
                        {"id": "300", "permissions": "0", "position": 9}],
                 member={"roles": ["200"]})


def test_permissions_add_up_from_everyone_and_the_bots_roles() -> None:
    facts = _guild(P["view"], P["send"])
    assert facts.has("view") and facts.has("send") and not facts.has("embed")


def test_administrator_and_ownership_are_everything() -> None:
    assert _guild(own=P["administrator"]).base_permissions() == ALL_PERMISSIONS
    assert _guild(owner="9").base_permissions() == ALL_PERMISSIONS
    channel = {"permission_overwrites": [{"id": "100", "deny": str(P["view"]), "allow": "0"}]}
    assert _guild(own=P["administrator"]).has("view", channel)  # no overwrite stops an administrator


def test_overwrites_go_everyone_then_roles_then_the_member() -> None:
    base = _guild(P["view"] | P["send"])
    denied_to_all = {"permission_overwrites": [{"id": "100", "deny": str(P["send"]), "allow": "0"}]}
    assert not base.has("send", denied_to_all)
    let_back_by_role = {"permission_overwrites": [{"id": "100", "deny": str(P["send"]), "allow": "0"},
                                                  {"id": "200", "deny": "0", "allow": str(P["send"])}]}
    assert base.has("send", let_back_by_role)
    denied_to_bot = {"permission_overwrites": [{"id": "200", "deny": "0", "allow": str(P["send"])},
                                               {"id": "9", "deny": str(P["send"]), "allow": "0"}]}
    assert not base.has("send", denied_to_bot)
    other_role = {"permission_overwrites": [{"id": "300", "deny": "0", "allow": str(P["embed"])}]}
    assert not base.has("embed", other_role)  # a role the bot hasn't got


def test_what_at_everyone_can_read(fake: FakeDiscord) -> None:
    facts = gather(TOKEN, GUILD)
    assert facts.everyone_reads(facts.find_channel(PUBLIC_CHANNEL))
    assert not facts.everyone_reads(facts.find_channel(STAFF_CHANNEL))
    assert facts.has("send", facts.find_channel(STAFF_CHANNEL))  # let in by its own role
    assert facts.top_position() == 4 and facts.find_role(BOT_ROLE)["managed"]


# ranks.py ------------------------------------------------------------------------------------------------------


def ranks_file(tmp_path: Path, body: str = "") -> Any:
    path = tmp_path / "discord.toml"
    path.write_text(body, encoding="utf-8")
    return load(path)


def test_nothing_counts_until_it_is_mapped(tmp_path: Path) -> None:
    config = ranks_file(tmp_path)
    assert rank_of(config, Someone("1", {"5": "Admin"}, administrator=True)) == "player"


def test_the_highest_mapped_role_counts_by_id_or_name(tmp_path: Path) -> None:
    config = ranks_file(tmp_path, f'[ranks]\nadmin = ["{ADMIN_ROLE}"]\nmoderator = ["@SPR-Mod"]\nmember = ["m"]\n'
                                  'probationary_moderator = ["p"]\n')
    assert rank_of(config, Someone("1", {MOD_ROLE: "spr-mod"})) == "moderator"
    assert rank_of(config, Someone("1", {MOD_ROLE: "SPR-Mod", ADMIN_ROLE: "x"})) == "admin"
    assert rank_of(config, Someone("1", {"8": "p"})) == "probationary-moderator"
    assert rank_of(config, Someone("1", {"8": "m"})) == "member"
    assert rank_of(config, Someone("1", {"8": "other"})) == "player"


def test_owners_are_named_never_given(tmp_path: Path) -> None:
    config = ranks_file(tmp_path, '[ranks]\nowners = ["111"]\nadmin = ["a"]\n')
    assert is_owner(config, "111") and rank_of(config, Someone("111")) == "owner"
    assert rank_of(config, Someone("222", {"1": "a"}, administrator=True)) == "admin"
    assert rank_of(config, Someone("222", {"1": "owners"})) == "player"


def test_discords_own_administrators_count_only_as_the_file_says(tmp_path: Path) -> None:
    config = ranks_file(tmp_path, '[ranks]\ndiscord_administrators = "moderator"\n')
    assert rank_of(config, Someone("1", administrator=True)) == "moderator"
    assert rank_of(config, Someone("1", administrator=False)) == "player"


def test_a_timeout_suspends_a_rank_but_not_an_owner(tmp_path: Path) -> None:
    config = ranks_file(tmp_path, '[ranks]\nadmin = ["a"]\nowners = ["111"]\n')
    assert rank_of(config, Someone("1", {"1": "a"}, timed_out=True)) == "player"
    assert rank_of(config, Someone("111", timed_out=True)) == "owner"
    config = ranks_file(tmp_path, '[ranks]\nadmin = ["a"]\ntimeout_suspends = false\n')
    assert rank_of(config, Someone("1", {"1": "a"}, timed_out=True)) == "admin"


def test_a_rank_on_some_servers_only(tmp_path: Path) -> None:
    config = ranks_file(tmp_path, '[[ranks.scoped]]\nrole = "@S1"\nrank = "admin"\nservers = ["SPR 1"]\n'
                                  '[[ranks.scoped]]\nrole = "@All"\nrank = "probationary_moderator"\nservers = ["*"]\n')
    someone = Someone("1", {"1": "S1"})
    assert rank_of(config, someone, "SPR 1") == "admin"
    assert rank_of(config, someone, "SPR 2") == "player" and rank_of(config, someone) == "player"
    assert rank_of(config, Someone("1", {"1": "All"})) == "probationary-moderator"


def test_who_can_act() -> None:
    assert can_act("probationary-moderator") and can_act("owner") and not can_act("member") and not can_act("player")


def test_ranks_can_be_set_from_a_real_role_list(tmp_path: Path, fake: FakeDiscord) -> None:
    path = tmp_path / "discord.toml"
    facts = gather(TOKEN, GUILD)
    set_rank_roles(path, "admin", [(ADMIN_ROLE, "SPR-Admin")], guild_roles={str(r["id"]): str(r["name"]) for r in facts.roles})
    assert rank_of(load(path), Someone("1", {ADMIN_ROLE: "renamed since"})) == "admin"
