"""Every feature's state, and the conflicts table of plans/discord-bot-design.md."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Iterator

import pytest

from testkit.fake_discord import GUILD, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord

from huragok.discordbot import features, rest
from huragok.discordbot.config import load, template_text
from huragok.discordbot.facts import gather
from huragok.discordbot.features import BY_KEY, COMING, FEATURES, OFF, ON, WAITING, check, state_of


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


@pytest.fixture
def all_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(features, "BUILT", 7)


def bot(tmp_path: Path, body: str = "", *, mode: str = "bot", guild: str = GUILD) -> Any:
    path = tmp_path / "discord.toml"
    path.write_text(f'mode = "{mode}"\ntoken = "{TOKEN}"\nguild = "{guild}"\n{body}', encoding="utf-8")
    return load(path)


def _switches(table: dict[str, Any], where: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for key, value in table.items():
        if isinstance(value, dict):
            found |= _switches(value, (*where, key))
        elif key == "enabled":
            found.add(where)
    return found


def test_every_switch_in_the_template_is_a_feature() -> None:
    data = tomllib.loads(template_text())
    assert _switches(data) - {feature.table for feature in FEATURES} == set()


def test_every_feature_has_a_real_switch_in_the_template() -> None:
    data = tomllib.loads(template_text())
    for feature in FEATURES:
        table: Any = data
        for part in feature.table:
            table = table[part]
        assert feature.switch in table, feature.key
        assert table[feature.switch] == feature.off_value or feature.key == "personas", feature.key


def test_keys_are_unique_and_only_personas_starts_on() -> None:
    assert len(BY_KEY) == len(FEATURES)
    config = load(Path("no such file"))
    assert [feature.key for feature in FEATURES if feature.is_on(config)] == ["personas"]


def test_what_is_not_built_is_coming_whatever_its_switch_says(tmp_path: Path) -> None:
    config = bot(tmp_path, "[feed.moderation]\nenabled = true\n")
    assert state_of(BY_KEY["feed.moderation"], config, None)[0] == COMING
    assert state_of(BY_KEY["personas"], config, None)[0] != COMING


def test_off_means_off(tmp_path: Path, all_built: None) -> None:
    config = bot(tmp_path)
    assert state_of(BY_KEY["feed.moderation"], config, None) == (OFF, "")


def test_webhook_mode_can_only_post(tmp_path: Path, all_built: None) -> None:
    config = bot(tmp_path, '[feed.moderation]\nenabled = true\n[link]\nenabled = true\n', mode="webhook")
    state, why = state_of(BY_KEY["link"], config, None)
    assert state == WAITING and "needs the bot" in why
    state, why = state_of(BY_KEY["feed.moderation"], config, None)
    assert state == WAITING and "webhook address" in why
    config = bot(tmp_path, '[feed.moderation]\nenabled = true\nwebhook = "https://discord.com/api/webhooks/1/x"\n', mode="webhook")
    assert state_of(BY_KEY["feed.moderation"], config, None) == (ON, "")


def test_a_channel_a_staff_feed_needs(tmp_path: Path, all_built: None, fake: FakeDiscord) -> None:
    facts = gather(TOKEN, GUILD)
    feed = BY_KEY["feed.moderation"]
    config = bot(tmp_path, "[feed.moderation]\nenabled = true\n")
    state, why = state_of(feed, config, facts)
    assert state == WAITING and "[channels] staff is empty" in why
    config = bot(tmp_path, '[channels]\nstaff = "nowhere"\n[feed.moderation]\nenabled = true\n')
    assert "no channel 'nowhere'" in state_of(feed, config, facts)[1]
    config = bot(tmp_path, f'[channels]\nstaff = "{PUBLIC_CHANNEL}"\n[feed.moderation]\nenabled = true\n')
    state, why = state_of(feed, config, facts)
    assert state == WAITING and "@everyone" in why and "public = true" in why
    config = bot(tmp_path, f'[channels]\nstaff = "{PUBLIC_CHANNEL}"\n[feed.moderation]\nenabled = true\npublic = true\n')
    assert state_of(feed, config, facts)[0] == ON
    config = bot(tmp_path, f'[channels]\nstaff = "{STAFF_CHANNEL}"\n[feed.moderation]\nenabled = true\n')
    assert state_of(feed, config, facts) == (ON, "")


def test_a_channel_the_bot_cant_post_in(tmp_path: Path, all_built: None, fake: FakeDiscord) -> None:
    fake.channels[0]["permission_overwrites"] = [{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)}]
    facts = gather(TOKEN, GUILD)
    assert facts.has("send")
    config = bot(tmp_path, f'[channels]\nstaff = "{STAFF_CHANNEL}"\n[feed.moderation]\nenabled = true\n')
    state, why = state_of(BY_KEY["feed.moderation"], config, facts)
    assert state == WAITING and "can't post in #admin-log" in why and "View Channel" in why


def test_the_portals_switches(tmp_path: Path, all_built: None, fake: FakeDiscord) -> None:
    fake.flags = 0
    facts = gather(TOKEN, GUILD)
    config = bot(tmp_path, "[console]\ntyped = true\n")
    state, why = state_of(BY_KEY["console"], config, facts)
    assert state == WAITING and "Message Content switch is off" in why
    config = bot(tmp_path, "[chat]\nto_game = true\n")
    assert "Message Content" in state_of(BY_KEY["chat.to_game"], config, facts)[1]
    fake.flags = 1 << 19  # the "limited" form of the switch counts too
    assert state_of(BY_KEY["console"], bot(tmp_path, "[console]\ntyped = true\n"), gather(TOKEN, GUILD))[0] == ON


def test_role_sync_needs_linking_and_manage_roles(tmp_path: Path, all_built: None, fake: FakeDiscord) -> None:
    facts = gather(TOKEN, GUILD)
    config = bot(tmp_path, "[roles]\nsync = true\n")
    assert "needs linking" in state_of(BY_KEY["roles"], config, facts)[1]
    config = bot(tmp_path, "[roles]\nsync = true\n[link]\nenabled = true\n")
    assert state_of(BY_KEY["roles"], config, facts)[0] == ON
    next(r for r in fake.roles if r["name"] == "Huragok")["permissions"] = str(1 << 11)
    state, why = state_of(BY_KEY["roles"], config, gather(TOKEN, GUILD))
    assert state == WAITING and "Manage Roles" in why


def test_a_switch_that_is_not_true_or_false(tmp_path: Path, all_built: None) -> None:
    feature = BY_KEY["community.awards"]
    config = bot(tmp_path, '[community]\nawards = "daily"\n')
    assert feature.is_on(config) and features.switched(feature, config) == "off"
    config = bot(tmp_path)
    assert not feature.is_on(config) and features.switched(feature, config) == "weekly"


def test_the_bot_asks_only_for_the_intents_what_is_on_wants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert features.needed_intents(bot(tmp_path, "[console]\ntyped = true\n")) == set()  # not built yet
    monkeypatch.setattr(features, "BUILT", 7)
    assert features.needed_intents(bot(tmp_path)) == set()
    assert features.needed_intents(bot(tmp_path, "[console]\ntyped = true\n")) == {"message_content"}


def test_the_invite_asks_for_exactly_what_is_on(tmp_path: Path) -> None:
    base = features.invite_permissions(bot(tmp_path))
    assert base & rest.PERMISSIONS["send"] and base & rest.PERMISSIONS["manage_webhooks"]
    assert not base & rest.PERMISSIONS["manage_roles"] and not base & rest.PERMISSIONS["administrator"]
    more = features.invite_permissions(bot(tmp_path, "[roles]\nsync = true\n"))
    assert more & rest.PERMISSIONS["manage_roles"]


def test_names_become_ids_once_discord_can_say_which(tmp_path: Path, fake: FakeDiscord) -> None:
    config = bot(tmp_path, '[ranks]\nadmin = ["@spr-admin", "@Nobody"]\n[channels]\nstaff = "#admin-log"\npublic = "nowhere"\n'
                           '[feed.games]\nchannel = "servers"\n[feed.votes]\nchannel = "staff"\n')
    facts = gather(TOKEN, GUILD)
    assert len(features.names_to_ids(config, facts)) == 3
    again = load(config.path)
    assert again.section("channels")["staff"] == STAFF_CHANNEL and again.section("channels")["public"] == "nowhere"
    assert sorted(again.section("ranks")["admin"]) == sorted(["@Nobody", "5001"])  # one it can't find stays as typed
    assert again.section("feed", "games")["channel"] == PUBLIC_CHANNEL and again.section("feed", "votes")["channel"] == "staff"
    text = config.path.read_text(encoding="utf-8")
    assert f'staff = "{STAFF_CHANNEL}"  # #admin-log' in text and '"5001",  # @SPR-Admin' in text
    assert features.names_to_ids(again, facts) == []  # nothing left to do: the file settles


# The check -----------------------------------------------------------------------------------------------------


def _said(findings: list[features.Finding]) -> str:
    return "\n".join(f"{'ok' if f.good else 'BAD'} {f.where}: {f.text}" for f in findings)


def test_the_check_on_a_good_setup(tmp_path: Path, fake: FakeDiscord) -> None:
    config = bot(tmp_path, f'[ranks]\nowners = ["3000"]\nadmin = ["@SPR-Admin"]\n[channels]\nstaff = "{STAFF_CHANNEL}"\n')
    findings, facts = check(config)
    assert facts is not None and all(f.good for f in findings), _said(findings)
    said = _said(findings)
    assert "signed in as Huragok Test" in said and "Test Discord" in said and "@SPR-Admin" in said and "#admin-log" in said


def test_the_check_says_what_to_fix_first(tmp_path: Path, fake: FakeDiscord) -> None:
    fake.bot_public = True
    config = bot(tmp_path, '[ranks]\nadmin = ["@Nobody"]\nowners = ["@Boss"]\n[channels]\nstaff = "nowhere"\n')
    findings, _ = check(config)
    said = _said(findings)
    assert "Public Bot" in said and "no role '@Nobody'" in said and "no channel 'nowhere'" in said
    assert "'@Boss' isn't a Discord user ID" in said
    assert not findings[0].good and findings[-1].good
    assert [f.good for f in findings] == sorted(f.good for f in findings)


def test_the_check_with_no_owner_points_at_the_claim(tmp_path: Path, fake: FakeDiscord) -> None:
    findings, _ = check(bot(tmp_path))
    assert "/huragok claim" in _said(findings)


def test_the_check_without_a_token_or_a_good_one(tmp_path: Path, fake: FakeDiscord) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\n', encoding="utf-8")
    assert "there's no token" in _said(check(load(path))[0])
    path.write_text('mode = "bot"\ntoken = "wrong"\n', encoding="utf-8")
    findings, facts = check(load(path))
    assert facts is None and "refused the token" in _said(findings)


def test_the_check_with_a_discord_not_picked_or_not_joined(tmp_path: Path, fake: FakeDiscord) -> None:
    assert "none picked yet" in _said(check(bot(tmp_path, guild=""))[0])
    assert "isn't in Discord 9999" in _said(check(bot(tmp_path, guild="9999"))[0])


def test_the_check_tries_each_webhook(tmp_path: Path, fake: FakeDiscord) -> None:
    good = rest.create_webhook(TOKEN, STAFF_CHANNEL, "Up")["url"]
    config = bot(tmp_path, f'[feed.alerts]\nlauncher_webhook = "{good}"\n', mode="webhook")
    assert "its webhook 'Up' works" in _said(check(config)[0])
    config = bot(tmp_path, f'[feed.alerts]\nlauncher_webhook = "{good}0"\n', mode="webhook")
    assert "its webhook doesn't work" in _said(check(config)[0])


def test_the_check_on_a_broken_file_says_only_that(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text("[broken", encoding="utf-8")
    findings, _ = check(load(path))
    assert len(findings) == 1 and "isn't valid TOML" in findings[0].text


def test_one_channel_one_audience(tmp_path: Path, all_built: None, fake: FakeDiscord) -> None:
    config = bot(tmp_path, f'[channels]\nstaff = "{STAFF_CHANNEL}"\ngames = "{STAFF_CHANNEL}"\n'
                           "[feed.moderation]\nenabled = true\n[feed.games]\nenabled = true\n")
    said = _said(check(config)[0])
    assert "share #admin-log" in said and "one is private and the other public" in said


def test_the_personas_need_manage_webhooks_to_show(tmp_path: Path, fake: FakeDiscord) -> None:
    next(r for r in fake.roles if r["name"] == "Huragok")["permissions"] = str((1 << 10) | (1 << 11) | (1 << 14))
    said = _said(check(bot(tmp_path))[0])
    assert "BAD Posting as the Superintendent" in said and "Manage Webhooks" in said
