"""discord.toml: its template is every setting there is; what it can't use is a warning, never a stop."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from huragok.core.tomledit import read_toml
from huragok.discordbot.config import (
    DiscordFileError, add_owner, clean_token, defaults, launcher_webhook, load, make_file, read, reload_if_changed,
    remove_owner, set_channel, set_guild, set_launcher_webhook, set_mode, set_rank_roles, set_switch, set_token,
    template_text,
)


def test_the_template_is_every_feature_off_and_no_secret() -> None:
    data = tomllib.loads(template_text())
    assert data["mode"] == "off" and data["token"] == "" and data["guild"] == ""
    assert data["feed"]["moderation"]["enabled"] is False and data["status"]["board"]["enabled"] is False
    assert data["ranks"]["owners"] == [] and data["ranks"]["discord_administrators"] == "nothing"
    assert data["link"]["nickname"] == "off" and data["console"]["typed"] is False
    assert data["moderation"]["enabled"] is False  # moderating the Discord: off by default, the host's call
    assert "FINAL DRAFT" not in template_text()


def test_no_file_is_every_default(tmp_path: Path) -> None:
    config = load(tmp_path / "discord.toml")
    assert config.mode == "off" and not config.error and not config.warnings and config.token == ""
    assert config.limit("status_seconds") == 120 and config.section("feed", "alerts")["persona"] == "superintendent"


def test_what_it_does_not_know_is_a_warning_never_a_stop(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\nmodee = 1\n[limits]\nstatus_secnds = 90\n[a_later_feature]\nx = 1\n', encoding="utf-8")
    config = load(path)
    assert config.mode == "bot" and not config.error
    joined = "\n".join(config.warnings)
    assert "modee isn't a setting" in joined and "[limits] status_secnds" in joined and "did you mean status_seconds" in joined
    assert "a_later_feature" in joined


def test_the_wrong_kind_of_value_falls_back_to_its_default(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[limits]\nstatus_seconds = "soon"\n[link]\nenabled = 1\n', encoding="utf-8")
    config = load(path)
    assert config.limit("status_seconds") == 120 and config.section("link")["enabled"] is False
    assert sum("default is used" in line for line in config.warnings) == 2


def test_an_owner_role_is_refused_with_the_reason(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[ranks]\nowner = ["@Boss"]\n', encoding="utf-8")
    assert any("owners are people, never a role" in line for line in load(path).warnings)


def test_every_timer_keeps_to_its_floor(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text("[limits]\nrename_minutes = 1\nchat_per_minute = 500\n[status.channel_names]\nupdate_minutes = 2\n"
                    "[status.presence]\nrotate_seconds = 1\n", encoding="utf-8")
    config = load(path)
    assert config.limit("rename_minutes") == 5 and config.limit("chat_per_minute") == 30
    assert config.section("status", "channel_names")["update_minutes"] == 5  # no lower than [limits], as raised
    assert config.section("status", "presence")["rotate_seconds"] == 120
    assert sum("can't go" in line for line in config.warnings) == 4


def test_the_hosts_own_keys_are_theirs(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[text]\n"test.vergil" = "mine"\n[community]\nself_roles = { Seeder = "@Seeder" }\n'
                    '[[ranks.scoped]]\nrole = "@A"\nrank = "admin"\nservers = ["S"]\n'
                    '[leaderboards.board.mine]\nstat = "kd"\n', encoding="utf-8")
    config = load(path)
    assert not config.warnings
    assert config.section("text")["test.vergil"] == "mine" and config.section("community")["self_roles"] == {"Seeder": "@Seeder"}
    assert config.section("ranks")["scoped"][0]["role"] == "@A"


def test_a_feed_has_the_feeds_defaults_beneath_it(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[feeds]\nsilent = false\n[feed.alerts]\nping = "@On Call"\n', encoding="utf-8")
    feed = load(path).feed("alerts")
    assert feed["ping"] == "@On Call" and feed["merge_seconds"] == 30 and feed["silent"] is False


def test_a_feeds_block_can_set_what_feeds_sets_for_all(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[feed.moderation]\npublic = true\nstyle = "compact"\nmerge_seconds = "soon"\n', encoding="utf-8")
    config = load(path)
    feed = config.feed("moderation")
    assert feed["public"] is True and feed["style"] == "compact" and feed["merge_seconds"] == 30
    assert config.warnings == ["discord.toml: [feed.moderation] merge_seconds should be a number; its default is used"]


def test_a_channel_means_the_shared_name_or_itself(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[channels]\nstaff = "123"\n', encoding="utf-8")
    config = load(path)
    assert config.channel_ref("staff") == "123" and config.channel_ref("#elsewhere") == "#elsewhere"
    assert config.channel_ref("alerts") == ""


def test_an_id_without_quotes_is_still_an_id(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text("guild = 123456\n", encoding="utf-8")
    config = load(path)
    assert config.guild == "123456" and not config.warnings


# The token -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ['  abc.def  ', 'Bot abc.def', '"abc.def"', 'bot  abc.def'])
def test_a_token_as_people_paste_it(typed: str) -> None:
    assert clean_token(typed) == "abc.def"


def test_the_token_can_live_in_a_variable_and_is_never_in_a_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\ntoken_variable = "MY_BOT"\n', encoding="utf-8")
    assert any("MY_BOT, which isn't set" in line for line in load(path).warnings)
    monkeypatch.setenv("MY_BOT", "secret.value")
    config = load(path)
    assert config.token == "secret.value" and not config.warnings
    assert "secret.value" not in repr(config)


def test_a_token_in_the_file_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('token = "from.file"\ntoken_variable = "MY_BOT"\n', encoding="utf-8")
    monkeypatch.setenv("MY_BOT", "from.variable")
    assert load(path).token == "from.file"


# A file that can't be used -------------------------------------------------------------------------------------


def test_a_broken_file_keeps_the_bot_off_at_the_start(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\n[broken', encoding="utf-8")
    config = load(path)
    assert config.mode == "off" and "isn't valid TOML" in config.error
    with pytest.raises(DiscordFileError):
        read(path)


def test_an_unknown_mode_is_a_mistake_not_a_guess(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bots"\n', encoding="utf-8")
    assert "it can be off, webhook, bot" in load(path).error


def test_a_reload_only_comes_when_the_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "off"\n', encoding="utf-8")
    config = load(path)
    assert reload_if_changed(config) is None
    assert reload_if_changed(config, force=True) is not None
    path.write_text('mode = "webhook"\n', encoding="utf-8")
    assert reload_if_changed(config).mode == "webhook"  # type: ignore[union-attr]
    path.write_text("[broken", encoding="utf-8")
    with pytest.raises(DiscordFileError):
        reload_if_changed(config)


# Changing it ---------------------------------------------------------------------------------------------------


def test_every_change_keeps_the_notes_and_only_changes_itself(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    assert make_file(path) is True and make_file(path) is False
    before = read_toml(path)
    set_mode(path, "bot")
    set_token(path, " Bot abc.def ")
    set_guild(path, "42", "My Discord")
    set_channel(path, "staff", "77", "admin-log")
    set_switch(path, ("feed", "moderation"), "enabled", True)
    set_switch(path, ("console",), "typed", True)
    set_launcher_webhook(path, " https://discord.com/api/webhooks/1/x ")
    after = read_toml(path)
    assert after["mode"] == "bot" and after["token"] == "abc.def" and after["guild"] == "42"
    assert after["channels"]["staff"] == "77" and after["feed"]["moderation"]["enabled"] is True
    assert after["console"]["typed"] is True and after["feed"]["alerts"]["launcher_webhook"] == "https://discord.com/api/webhooks/1/x"
    assert after["feed"]["votes"] == before["feed"]["votes"] and after["limits"] == before["limits"]
    text = path.read_text(encoding="utf-8")
    assert 'guild = "42"  # My Discord' in text and 'staff = "77"  # #admin-log' in text
    assert "# Who's who: Discord roles onto Huragok's ranks" in text
    assert "off | webhook (posts only, no bot) | bot (everything)" in text  # mode's own note stays
    set_channel(path, "staff", "", "")
    assert read_toml(path)["channels"]["staff"] == ""
    assert launcher_webhook(tmp_path) == "https://discord.com/api/webhooks/1/x"


def test_the_ranks_are_exactly_what_is_picked_each_once(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    make_file(path)
    text = path.read_text(encoding="utf-8").replace("admin = []", 'admin = ["@SPR-Admin", "99"]')
    path.write_text(text, encoding="utf-8")
    known = {"5": "SPR-Admin", "6": "Boss", "99": "Old"}
    set_rank_roles(path, "admin", [("5", "SPR-Admin"), ("6", "Boss")], guild_roles=known)
    assert sorted(read_toml(path)["ranks"]["admin"]) == ["5", "6"]  # a name becomes its ID; one not picked goes
    assert '"5",  # @SPR-Admin' in path.read_text(encoding="utf-8")
    set_rank_roles(path, "admin", [], guild_roles=known)
    assert read_toml(path)["ranks"]["admin"] == []


def test_owners_are_added_and_taken_off(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    assert add_owner(path, "111", "the host") is True
    assert add_owner(path, "111") is False
    assert '"111",  # the host' in path.read_text(encoding="utf-8")
    assert remove_owner(path, "111") is True and read_toml(path)["ranks"]["owners"] == []


def test_the_start_loops_webhook_never_fails_it(tmp_path: Path) -> None:
    assert launcher_webhook(tmp_path) == ""
    (tmp_path / "discord.toml").write_text("[broken", encoding="utf-8")
    assert launcher_webhook(tmp_path) == ""
    (tmp_path / "discord.toml").write_text("feed = 3\n", encoding="utf-8")
    assert launcher_webhook(tmp_path) == ""


def test_defaults_are_a_copy() -> None:
    defaults()["limits"]["status_seconds"] = 1
    assert defaults()["limits"]["status_seconds"] == 120
