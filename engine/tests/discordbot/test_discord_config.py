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
    assert data["console"]["typed"] is False
    assert "FINAL DRAFT" not in template_text()


def test_a_new_file_reads_with_nothing_to_say(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    make_file(path)
    config = load(path)
    assert config.warnings == [] and not config.error


def test_the_feed_called_servers_is_a_feed(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[feed.servers]\nenabled = true\nservers = ["A"]\n', encoding="utf-8")
    config = load(path)
    assert config.warnings == [] and config.section("feed", "servers")["enabled"] is True
    assert config.feed("servers")["servers"] == ["A"]


def test_no_file_is_every_default(tmp_path: Path) -> None:
    config = load(tmp_path / "discord.toml")
    assert config.mode == "off" and not config.error and not config.warnings and config.token == ""
    assert config.limit("status_seconds") == 60 and config.section("feed", "alerts")["persona"] == "superintendent"


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
    path.write_text('[limits]\nstatus_seconds = "soon"\n[reports]\nenabled = 1\n', encoding="utf-8")
    config = load(path)
    assert config.limit("status_seconds") == 60 and config.section("reports")["enabled"] is False
    assert sum("default is used" in line for line in config.warnings) == 2


def test_an_owner_role_is_refused_with_the_reason(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[ranks]\nowner = ["@Boss"]\n', encoding="utf-8")
    assert any("owners are people, never a role" in line for line in load(path).warnings)


def test_every_timer_keeps_to_its_floor(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text("[limits]\nrename_minutes = 1\nlogins_per_day = 5000\n[status.channel_names]\nupdate_minutes = 2\n"
                    "[status.presence]\nrotate_seconds = 1\n", encoding="utf-8")
    config = load(path)
    assert config.limit("rename_minutes") == 5 and config.limit("logins_per_day") == 900
    assert config.section("status", "channel_names")["update_minutes"] == 5  # no lower than [limits], as raised
    assert config.section("status", "presence")["rotate_seconds"] == 30
    assert sum("can't go" in line for line in config.warnings) == 4


def test_the_hosts_own_keys_are_theirs(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[text]\n"test.vergil" = "mine"\n[colours]\nmine = "#123456"\n'
                    '[[ranks.scoped]]\nrole = "@A"\nrank = "admin"\nservers = ["S"]\n', encoding="utf-8")
    config = load(path)
    assert not config.warnings
    assert config.section("text")["test.vergil"] == "mine" and config.section("colours")["mine"] == "#123456"
    assert config.section("ranks")["scoped"][0]["role"] == "@A"


def test_a_feed_has_the_feeds_defaults_beneath_it(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[feeds]\nstyle = "compact"\n[feed.votes]\nsilent = false\n', encoding="utf-8")
    feed = load(path).feed("votes")
    assert feed["style"] == "compact" and feed["merge_seconds"] == 30 and feed["silent"] is False
    assert load(path).feed("audit")["silent"] is True


def test_a_feeds_block_can_set_what_feeds_sets_for_all(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[feed.moderation]\npublic = true\nstyle = "compact"\nmerge_seconds = "soon"\n', encoding="utf-8")
    config = load(path)
    feed = config.feed("moderation")
    assert feed["public"] is True and feed["style"] == "compact" and feed["merge_seconds"] == 30
    assert config.warnings == ["discord.toml: [feed.moderation] merge_seconds should be a number; its default is used"]


def test_servers_can_be_every_server_or_a_list(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[feeds]\nservers = ["A"]\n[feed.alerts]\nservers = ["B", "C"]\n[feed.votes]\nservers = "*"\n[status.board]\nservers = ["D"]\n'
                    "[feed.games]\nservers = 3\n", encoding="utf-8")
    config = load(path)
    assert config.feed("alerts")["servers"] == ["B", "C"] and config.feed("votes")["servers"] == "*"
    assert config.feed("audit")["servers"] == ["A"] and config.section("status", "board")["servers"] == ["D"]
    assert config.feed("games")["servers"] == ["A"]
    assert config.warnings == ["discord.toml: [feed.games] servers should be text; its default is used"]


def test_a_channel_means_the_shared_name_or_itself(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[channels]\nstaff = "123"\n', encoding="utf-8")
    config = load(path)
    assert config.channel_ref("staff") == "123" and config.channel_ref("#elsewhere") == "#elsewhere"
    assert config.channel_ref("logs") == ""


def test_a_channel_left_unset_falls_back_and_old_names_still_mean_something(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[channels]\nstaff = "1"\nlogs = "2"\nstatus = "3"\n', encoding="utf-8")
    config = load(path)
    assert config.channel_ref("reports") == "1" and config.channel_ref("alerts") == "1"  # tickets, in staff
    assert config.channel_ref("huragok-logs") == "2"  # records, in logs
    assert config.channel_ref("public") == "3" and config.channel_ref("appeals") == "1"  # as a beta's lines may say
    assert not config.warnings


def test_what_shape_a_channel_gives_a_message() -> None:
    from huragok.discordbot.config import FEED, LOG, TICKET, shape_of

    assert [shape_of(name) for name in ("staff", "reports", "alerts")] == [TICKET] * 3
    assert [shape_of(name) for name in ("logs", "huragok-logs")] == [LOG] * 2
    assert [shape_of(name) for name in ("info", "games", "status", "#anywhere", "123", "")] == [FEED] * 6


def test_an_old_name_written_by_hand_says_what_its_called_now(tmp_path: Path) -> None:
    """An update moves what's in a file: this is someone copying an old post, or a file put back by hand."""
    path = tmp_path / "discord.toml"
    path.write_text('[contact]\nping = "1"\n[console]\nraw = false\n[messages]\nalerts-crashed = { channel = "alerts", colour = "red", post = true, ping = "", remove_after = 5 }\n', encoding="utf-8")
    config = load(path)
    said = "\n".join(config.warnings)
    assert "[contact] ping is [contact] thread_role now" in said and "[console] raw is [console] raw_rank now" in said and "remove_after is remove_after_minutes now" in said
    assert "remove_after" not in config.section("messages")["alerts-crashed"]


def test_what_was_only_ever_planned_is_just_not_a_setting(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text("[link]\nenabled = true\n[feed.kills]\nenabled = true\n", encoding="utf-8")
    said = "\n".join(load(path).warnings)
    assert "link isn't a setting this version knows" in said and "kills isn't a setting this version knows" in said


def test_a_reworded_text_works_quoted_or_not(tmp_path: Path) -> None:
    """The notes used to read as if this worked, and it quietly did nothing."""
    from huragok.discordbot.speakers import say

    path = tmp_path / "discord.toml"
    path.write_text('[text]\nticket.closed = "plain"\n"ticket.number" = "quoted"\n', encoding="utf-8")
    config = load(path)
    assert config.warnings == []
    assert say(config, "ticket.closed") == "plain" and say(config, "ticket.number", number=1) == "quoted"


def test_a_text_under_a_name_it_used_to_have_still_counts(tmp_path: Path) -> None:
    from huragok.discordbot.speakers import say

    path = tmp_path / "discord.toml"
    path.write_text('[text]\n"seeding.players" = "{server}: {players}"\n', encoding="utf-8")
    config = load(path)
    assert config.warnings == [] and say(config, "notify.players", server="S", players=3) == "S: 3"


def test_a_text_that_rewords_nothing_reaches_the_check(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[text]\n"no.such" = "x"\n', encoding="utf-8")
    assert any("[text] no.such isn't a text of this version" in line for line in load(path).warnings)


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
    assert any("names a variable that isn't set" in line for line in load(path).warnings)
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
    assert "mode can be off, webhook, bot" in load(path).error


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
    assert defaults()["limits"]["status_seconds"] == 60
