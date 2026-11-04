"""discord.toml as a release wrote it, brought up to date by the real steps, in their real order: it should end up as a new one would be, but for what its host chose, and every line of it should still be editable. One frozen copy per release."""

from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

from testkit.hostfiles import host, up_to_date

from huragok import __version__
from huragok.discordbot.config import defaults, load, set_message
from huragok.updates import hostfiles as migrate
from huragok.updates.hostfiles import LEAVE_ALONE
from huragok.updates.notes import template_text

BETA_1 = Path(__file__).parent / "discord-0.6.0-beta.1.toml"


def from_beta(tmp_path: Path, *edits: tuple[str, str]) -> Path:
    """A host's discord.toml as the first beta wrote it, with what they changed."""
    settings = host(tmp_path)
    path = settings.parent / "discord.toml"
    text = BETA_1.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    return path


def from_today(tmp_path: Path, old: str, new: str) -> Path:
    settings = host(tmp_path)
    path = settings.parent / "discord.toml"
    text = template_text("discord.toml")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def update(path: Path) -> list[str]:
    """What every step of this version said about this file."""
    return [line for line in up_to_date(path.parent / "settings.toml") if line.startswith("discord.toml")]


def keys_of(table: dict[str, Any], where: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for key, value in table.items():
        if isinstance(value, dict) and value and (where, key) not in LEAVE_ALONE and where != ("messages",):
            found |= keys_of(value, (*where, key))
        else:
            found.add((*where, key))
    return found


def read(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_every_step_on_this_file_is_in_the_real_list() -> None:
    """A step nobody listed would never run, and these tests run the list."""
    keys = [step.key for step in migrate.STEPS]
    assert {"0.6.0/discord-channels", "0.6.0/discord-buttons", "0.6.0/discord-retired", "0.6.0/discord-defaults", "0.6.0/discord-one-line", "0.6.0/discord-unbuilt", "discord-moved", "discord-lines"} <= set(keys)
    assert keys.index("0.6.0/discord-retired") < keys.index("discord-moved") < keys.index("discord-lines") < next(number for number, key in enumerate(keys) if key.endswith("/notes"))
    assert keys.index("0.6.0/discord-one-line") < keys.index("discord-moved")  # it can only move what it can read


def test_the_first_beta_comes_up_with_no_warnings(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    update(path)
    assert load(path).warnings == []


def test_the_first_beta_comes_up_with_every_setting_in_the_file(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    update(path)
    assert not {key for key in keys_of(defaults()) if len(key) > 1} - keys_of(read(path))


def test_nothing_dead_is_left_in_it(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    update(path)
    assert not keys_of(read(path)) - keys_of(defaults())


def test_every_line_of_messages_is_still_on_one_line_and_can_be_changed(tmp_path: Path) -> None:
    """Seen 1 November: an update wrote each line as a table of its own, and the panels then couldn't change any of them."""
    path = from_beta(tmp_path)
    update(path)
    assert not re.findall(r"^\[messages\.", path.read_text(encoding="utf-8"), re.M)
    for key in defaults()["messages"]:
        set_message(path, key, ping="123")
        assert read(path)["messages"][key]["ping"] == "123"
    assert load(path).warnings == []


def test_it_ends_with_every_note_a_new_file_has(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    update(path)
    have = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    assert not {line.strip() for line in template_text("discord.toml").splitlines() if line.strip().startswith("#")} - have


def test_a_file_that_was_broken_by_that_is_put_right(tmp_path: Path) -> None:
    """On DEPLOY, and anyone's who ran a test build: a line that came as a table. Same values, one line."""
    text = template_text("discord.toml")
    line = next(one for one in text.splitlines() if one.startswith("console-cleared"))
    settings = host(tmp_path)
    path = settings.parent / "discord.toml"
    path.write_text(text.replace(line + "\n", "").rstrip() + '\n\n[messages.console-cleared]\nchannel = "logs"\ncolour = "red"\npost = true\nping = "@Mods"\n', encoding="utf-8")
    assert any("back on one line" in said for said in update(path))
    written = path.read_text(encoding="utf-8")
    assert "[messages.console-cleared]" not in written
    assert read(path)["messages"]["console-cleared"] == {"channel": "logs", "colour": "red", "post": True, "ping": "@Mods"}
    set_message(path, "console-cleared", ping="")
    assert load(path).warnings == []


def test_a_second_update_does_nothing(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    update(path)
    before = path.read_text(encoding="utf-8")
    assert update(path) == []
    assert path.read_text(encoding="utf-8") == before


def test_only_asking_changes_nothing(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    before = path.read_text(encoding="utf-8")
    settings = path.parent / "settings.toml"
    assert any("discord.toml" in line for line in migrate.pending(settings, path.parent / "records", path.parent / "PACKAGES"))
    assert path.read_text(encoding="utf-8") == before


def test_the_token_isnt_left_in_a_copy_of_the_file(tmp_path: Path) -> None:
    """An update keeps the file as it was, and a password shouldn't be sat in a folder of old copies."""
    token = "MTIz.abc.def"
    hook = "https://discord.com/api/webhooks/1234567890/secret-part"
    path = from_beta(tmp_path, ('token = ""', f'token = "{token}"'), ('webhook = ""', f'webhook = "{hook}"'))
    update(path)
    assert read(path)["token"] == token  # the file itself keeps them
    copy = path.parent / "records" / migrate.BACKUPS / __version__ / "discord.toml"
    written = copy.read_text(encoding="utf-8")
    assert token not in written and "secret-part" not in written
    assert 'token = ""' in written and "close_after_days" in written  # the rest of it, as it was


def test_a_token_an_older_copy_kept_comes_out_of_it(tmp_path: Path) -> None:
    path = from_beta(tmp_path)
    copy = path.parent / "records" / migrate.BACKUPS / "0.6.0-beta.1" / "discord.toml"
    copy.parent.mkdir(parents=True)
    hook = "https://discord.com/api/webhooks/1/secret-part"
    copy.write_text(f'[bot]\ntoken = "a.real.token"\nguild = "5"\n\n[webhooks]\nlogs = "{hook}"\n', encoding="utf-8")
    update(path)
    left = copy.read_text(encoding="utf-8")
    assert "a.real.token" not in left and "secret-part" not in left and 'guild = "5"' in left


def test_several_roles_on_a_ping_stay_as_they_are(tmp_path: Path) -> None:
    path = from_today(tmp_path, 'calls-called             = { channel = "reports", colour = "yellow", post = true, ping = ""', 'calls-called             = { channel = "reports", colour = "yellow", post = true, ping = ["1", "2"]')
    update(path)
    assert read(path)["messages"]["calls-called"]["ping"] == ["1", "2"] and not load(path).warnings


def test_appeals_get_their_own_channel_and_nothing_moves_until_its_set(tmp_path: Path) -> None:
    path = from_today(tmp_path, 'appeals-made             = { channel = "appeals"', 'appeals-made             = { channel = "reports"')
    path.write_text(path.read_text(encoding="utf-8").replace('appeals = ""                   # people appealing what\'s against them. Empty: reports\n', ""), encoding="utf-8")
    update(path)
    data = read(path)
    assert data["messages"]["appeals-made"]["channel"] == "appeals" and data["channels"]["appeals"] == ""
    assert load(path).channel_ref("appeals") == load(path).channel_ref("reports")


def test_what_someone_chose_moves_to_where_it_lives_now(tmp_path: Path) -> None:
    path = from_beta(tmp_path, ("close_after_days = 7", "close_after_days = 3"))
    said = update(path)
    data = read(path)
    assert data["contact"]["close_after_days"] == 3 and "close_after_days" not in data["tickets"]
    assert any("moved to [contact]" in line for line in said)


def test_a_ping_someone_set_goes_on_each_of_its_lines(tmp_path: Path) -> None:
    text = BETA_1.read_text(encoding="utf-8")
    start = text.index("[feed.alerts]")
    at = text.index('ping = ""', start)
    settings = host(tmp_path)
    path = settings.parent / "discord.toml"
    path.write_text(text[:at] + 'ping = "@On Call"' + text[at + len('ping = ""'):], encoding="utf-8")
    update(path)
    data = read(path)
    assert data["messages"]["alerts-crashed"]["ping"] == "@On Call" and data["messages"]["alerts-hung"]["ping"] == "@On Call"
    assert "ping" not in data["feed"]["alerts"]


def test_what_has_no_new_home_stays_as_a_comment_saying_so(tmp_path: Path) -> None:
    path = from_beta(tmp_path, ('date_format = "discord"', 'date_format = "iso"'))
    said = update(path)
    text = path.read_text(encoding="utf-8")
    assert "# Huragok no longer uses this:" in text and '# date_format = "iso"' in text
    assert "date_format" not in tomllib.loads(text)
    assert any("date_format" in line for line in said)


def test_what_isnt_built_leaves_the_file_unless_someone_chose_it(tmp_path: Path) -> None:
    """Settings that do nothing, and that a later version would have to recognise for good."""
    path = from_beta(tmp_path, ("veteran_hours = 50", "veteran_hours = 99"), ('self_roles = {}', 'self_roles = { Seeder = "@Seeder" }'), ('chat = ""', 'chat = "123"'))
    said = update(path)
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    for gone in ("chat", "achievements", "matches", "community", "games", "moderation"):
        assert gone not in data, gone
    for built in ("link", "stats", "leaderboards", "roles"):  # in 0.6.1: what it has stays, what it hasn't goes
        assert set(data[built]) == set(defaults()[built]), built
    assert data["leaderboards"]["boards"] == defaults()["leaderboards"]["boards"]  # the first beta's placeholder list: today's
    assert "kills" not in data["feed"] and "chat" not in data["channels"] and "chat_per_minute" not in data["limits"]
    assert "# veteran_hours = 99" in text and "# self_roles =" in text and '# chat = "123"' in text
    assert "Not used yet: [roles] comes back" in text
    assert any("[roles] veteran_hours" in line and "isn't built yet" in line for line in said)
    assert load(path).warnings == []


def test_a_link_setting_the_first_beta_had_stays_as_it_was_set(tmp_path: Path) -> None:
    """[link] was a placeholder in the first beta and is built in 0.6.1: a host's own value there is theirs, and
    nothing of it becomes a comment saying it isn't built."""
    path = from_beta(tmp_path, ("code_minutes = 10", "code_minutes = 15"), ('nickname = "off"', 'nickname = "on"'))
    said = update(path)
    data = read(path)
    assert data["link"]["code_minutes"] == 15 and data["link"]["names"] == "discord"
    assert "nickname" not in data["link"] and '# nickname = "on"' in path.read_text(encoding="utf-8")  # still to come
    assert not any("[link] code_minutes" in line for line in said)


def test_the_names_that_changed_keep_what_was_set(tmp_path: Path) -> None:
    path = from_beta(tmp_path, ("raw = true", "raw = true"), ("moderators_raw_console = false", "moderators_raw_console = true"), ('message_menu = ["report"]', "message_menu = []"), ("notify_me = false", "notify_me = true"),
                     ("mods_command = true", "mods_command = false"))
    said = update(path)
    data = read(path)
    assert data["console"]["raw_rank"] == "moderator" and "raw" not in data["console"] and "moderators_raw_console" not in data["ranks"]
    assert data["reports"]["right_click"] is False and "message_menu" not in data["commands"]
    assert data["notify"]["enabled"] is True and "notify_me" not in data["status"]["seeding"]
    assert "mods_command" not in data["feed"]["calls"] and "mods" in read(path.parent / "commands.toml")["off"]["commands"]
    assert any("raw_rank" in line for line in said) and load(path).warnings == []


def test_console_lines_off_stays_off(tmp_path: Path) -> None:
    path = from_beta(tmp_path, ("raw = true", "raw = false"))
    update(path)
    assert read(path)["console"]["raw_rank"] == "off"


def test_a_contact_role_keeps_its_note_and_place(tmp_path: Path) -> None:
    """Moved from the first beta's [tickets] on its way, and renamed in place on a later file."""
    path = from_today(tmp_path, 'thread_role = ""               # the role', 'ping = "555"               # the role')
    update(path)
    data = read(path)
    assert data["contact"]["thread_role"] == "555" and "ping" not in data["contact"]
    assert "# the role brought into each thread" in next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("thread_role"))


def test_a_line_that_was_renamed_keeps_what_it_says(tmp_path: Path) -> None:
    line = next(one for one in template_text("discord.toml").splitlines() if one.startswith("notify-told"))
    path = from_today(tmp_path, line, 'seeding-notify = { channel = "info", colour = "red", post = true, ping = "@Seeders", remove_after = 5 }')
    update(path)
    messages = read(path)["messages"]
    assert "seeding-notify" not in messages
    assert messages["notify-told"] == {"channel": "info", "colour": "red", "post": True, "ping": "@Seeders", "remove_after_minutes": 5}
    assert load(path).warnings == []


def test_the_old_field_on_any_line_becomes_the_new_one(tmp_path: Path) -> None:
    line = next(one for one in template_text("discord.toml").splitlines() if one.startswith("alerts-crashed"))
    path = from_today(tmp_path, line, 'alerts-crashed = { channel = "alerts", colour = "red", post = true, ping = "", remove_after = 9 }')
    update(path)
    assert read(path)["messages"]["alerts-crashed"]["remove_after_minutes"] == 9


def test_the_notes_step_comes_after_the_new_lines() -> None:
    order = [step.key for step in migrate.STEPS]
    assert order.index("discord-lines") < next(number for number, key in enumerate(order) if key.endswith("/notes"))


def test_a_line_of_messages_gets_what_its_missing_and_keeps_what_it_has(tmp_path: Path) -> None:
    """A line's written all on one line, so nothing can be added under it: a new field has to go in it."""
    line = next(one for one in template_text("discord.toml").splitlines() if one.startswith("seeding-wanted"))
    path = from_today(tmp_path, line, 'seeding-wanted = { channel = "info", colour = "red", post = true, ping = "@Seeders" }')
    update(path)
    assert read(path)["messages"]["seeding-wanted"] == {"channel": "info", "colour": "red", "post": True, "ping": "@Seeders", "remove_after_minutes": 30}
    assert load(path).warnings == []


def test_a_default_that_was_too_loud_changes_only_if_nobody_chose_it(tmp_path: Path) -> None:
    path = from_beta(tmp_path / "one")
    update(path)
    data = read(path)
    assert data["tickets"]["stale_after_days"] == 2 and data["limits"]["status_seconds"] == 60
    path = from_today(tmp_path / "two", "stale_after_days = 2", "stale_after_days = 5")
    update(path)
    assert read(path)["tickets"]["stale_after_days"] == 5


def test_a_commands_toml_listing_the_old_name_follows_it(tmp_path: Path) -> None:
    """!reports was the probationary moderators' list: now !requests, where the host had it, and !reports is what's open."""
    settings = host(tmp_path)
    commands = settings.parent / "commands.toml"
    text = commands.read_text(encoding="utf-8")
    moderator = text.index("[moderator]")
    at = text.index("commands = [", moderator) + len("commands = [")
    commands.write_text(text[:at] + '"reports", ' + text[at:], encoding="utf-8")
    up_to_date(settings)
    listed = read(commands)
    assert "requests" in listed["moderator"]["commands"] and listed["moderator"]["commands"].count("reports") == 1
