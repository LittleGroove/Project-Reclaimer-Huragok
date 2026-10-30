"""J in the window, what's said to whom (speakers.py), the start loop's post, and discord.toml as a host's file: written at
the first start and by the update, and its notes kept up to date."""

from __future__ import annotations

import asyncio
import re
import tomllib
from pathlib import Path
from typing import Any, Iterator

import pytest

from testkit.fake_discord import (
    ADMIN_ROLE, GUILD, MOD_ROLE, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord,
)
from testkit.hostfiles import host, up_to_date
from testkit.screens import screen, template_root, text_of

from huragok.commandline import launcher
from huragok.core.tomledit import read_toml
from huragok.discordbot import records, rest, speakers
from huragok.discordbot.config import add_owner, load, make_file, set_guild, set_launcher_webhook, set_mode, set_token
from huragok.discordbot.outbox import Outbox
from huragok.discordbot.speakers import Delivery, Poster, message, say, speaker
from huragok.updates import notes


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def keys(text: str) -> list[str]:
    """What's typed, a key at a time, with Enter."""
    return [*text, "ENTER"]


def opened(tmp_path: Path, *typed: list[str]) -> tuple[Any, Any, Path]:
    settings = template_root(tmp_path)
    flat = [key for group in typed for key in group]
    menu, terminal = screen(settings, flat)
    return menu, terminal, settings.parent / "discord.toml"


def bot_ready(path: Path) -> None:
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")


# J -------------------------------------------------------------------------------------------------------------


def test_the_key_is_on_the_dashboard_and_goes_to_huragok() -> None:
    from huragok import lore
    from huragok.window.dashboard import key_rows

    assert any("[J] Discord" in keys_row for _, keys_row, _ in key_rows(None))
    assert lore.KEYS["J"] is lore.HURAGOK


def test_setting_the_token_checks_it_and_never_shows_it(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    menu, terminal, path = opened(tmp_path, ["M", "3"], keys(TOKEN), ["ESC"])
    run(menu.screen_discord())
    data = read_toml(path)
    assert data["mode"] == "bot" and data["token"] == TOKEN
    shown = text_of(terminal)
    assert TOKEN not in shown and "Signed in as Huragok Test" in shown
    assert "discord.com/oauth2/authorize?client_id=2001" in shown and "applications.commands" in shown
    assert "Public Bot" not in shown.split("Signed in")[1]


def test_a_refused_token_changes_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    menu, terminal, path = opened(tmp_path, ["M", "3"], keys("wrong.token"), ["ESC"])
    run(menu.screen_discord())
    data = read_toml(path)
    assert data["mode"] == "off" and data["token"] == ""
    assert "refused the token" in text_of(terminal) and "wrong.token" not in text_of(terminal)


def test_webhook_mode_and_off(tmp_path: Path, run: Any) -> None:
    menu, _, path = opened(tmp_path, ["M", "2"], ["ESC"])
    run(menu.screen_discord())
    assert read_toml(path)["mode"] == "webhook"
    menu, _, path = opened(tmp_path / "again", ["M", "1"], ["ESC"])
    run(menu.screen_discord())
    assert read_toml(path)["mode"] == "off"


def test_picking_the_discord(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    menu, terminal, path = opened(tmp_path, ["W"], keys("1"), ["ESC"])
    make_file(path)
    set_token(path, TOKEN)
    run(menu.screen_discord())
    assert read_toml(path)["guild"] == GUILD
    assert f'guild = "{GUILD}"  # Test Discord' in path.read_text(encoding="utf-8")
    assert "works in Test Discord" in text_of(terminal)


def test_with_no_token_the_steps_say_so(tmp_path: Path, run: Any) -> None:
    menu, terminal, _ = opened(tmp_path, ["W"], ["R"], ["N"], ["ESC"])
    run(menu.screen_discord())
    assert text_of(terminal).count("no bot token yet") == 3


def test_ranks_from_the_discords_own_roles(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    # Listed highest first, without @everyone and without the bot's own: 1 SPR-Admin, 2 SPR-Mod. Admin, moderator, then
    # Enter for the other two, and Enter for no owner.
    menu, terminal, path = opened(tmp_path, ["R"], keys("1"), keys("2"), ["ENTER"], ["ENTER"], ["ENTER"], ["ESC"])
    bot_ready(path)
    run(menu.screen_discord())
    ranks = read_toml(path)["ranks"]
    assert ranks["admin"] == [ADMIN_ROLE] and ranks["moderator"] == [MOD_ROLE] and ranks["member"] == []
    shown = text_of(terminal)
    assert "@SPR-Admin" in shown and "@@everyone" not in shown and "@Huragok" not in shown  # nor the bot's own role


def test_owners_by_user_id(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    add = [["R"], ["ENTER"] * 4, keys("123456"), ["ESC"]]
    menu, terminal, path = opened(tmp_path, *add)
    bot_ready(path)
    run(menu.screen_discord())
    assert read_toml(path)["ranks"]["owners"] == ["123456"]
    menu, terminal, path = opened(tmp_path / "again", ["R"], ["ENTER"] * 4, keys("not a number"), ["ESC"])
    bot_ready(path)
    run(menu.screen_discord())
    assert read_toml(path)["ranks"]["owners"] == [] and "isn't a user ID" in text_of(terminal)


def test_channels_numbered_with_who_can_read_them(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    # 1 #admin-log (private), 2 #servers; "staff" first, then Esc to leave the rest.
    menu, terminal, path = opened(tmp_path, ["N"], keys("1"), ["ESC"], ["ESC"])
    bot_ready(path)
    run(menu.screen_discord())
    assert read_toml(path)["channels"]["staff"] == STAFF_CHANNEL
    assert f'staff = "{STAFF_CHANNEL}"  # #admin-log' in path.read_text(encoding="utf-8")
    shown = text_of(terminal)
    assert "private" in shown and "@everyone reads" in shown


def test_features_show_what_is_built_and_switch(tmp_path: Path, run: Any) -> None:
    menu, terminal, path = opened(tmp_path, ["F"], keys("1"), ["ESC"], ["ESC"])
    run(menu.screen_discord())
    assert read_toml(path)["personas"]["enabled"] is False
    shown = text_of(terminal)
    assert "more arrive in later betas" in shown and "Moderation feed" not in shown


def test_the_check_in_the_window(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    menu, terminal, path = opened(tmp_path, ["C"], ["ESC"])
    bot_ready(path)
    run(menu.screen_discord())
    shown = text_of(terminal)
    assert "signed in as Huragok Test" in shown and "/huragok claim" in shown and TOKEN not in shown


def test_a_test_post_from_the_window(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    menu, terminal, path = opened(tmp_path, ["T"], keys("2"), ["ESC"])
    bot_ready(path)
    run(menu.screen_discord())
    assert [post["username"] for post in fake.posted] == ["Superintendent", "Vergil", "Huragok"]
    assert {post["channel"] for post in fake.posted} == {PUBLIC_CHANNEL}
    assert "through the bot's webhook" in text_of(terminal)


def test_a_test_post_through_a_webhook(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    address = rest.create_webhook(TOKEN, STAFF_CHANNEL, "Mine")["url"]
    menu, terminal, path = opened(tmp_path, ["T"], keys(address), ["ESC"])
    make_file(path)
    set_mode(path, "webhook")
    run(menu.screen_discord())
    assert [post["username"] for post in fake.posted] == ["Superintendent", "Vergil", "Huragok"]
    assert "Three posts sent" in text_of(terminal)


def test_the_claim_code_works_once(tmp_path: Path, run: Any) -> None:
    menu, terminal, _ = opened(tmp_path, ["K"], ["ESC"])
    run(menu.screen_discord())
    shown = text_of(terminal)
    code = re.search(r"/huragok claim ([A-Z2-9]{4}-[A-Z2-9]{4})", shown).group(1)  # type: ignore[union-attr]
    home = tmp_path / "reclaimer" / "records"
    assert records.use_code(home, "WRONG") is False
    assert records.use_code(home, code) is True and records.use_code(home, code) is False


def test_the_start_loops_webhook_is_checked_before_it_is_kept(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    address = rest.create_webhook(TOKEN, STAFF_CHANNEL, "Alerts")["url"]
    menu, terminal, path = opened(tmp_path, ["L"], keys(address + "0"), ["L"], keys(address), ["ESC"])
    run(menu.screen_discord())
    assert read_toml(path)["feed"]["alerts"]["launcher_webhook"] == address
    assert "It posts through 'Alerts'" in text_of(terminal)


def test_every_text_is_listed(tmp_path: Path, run: Any) -> None:
    menu, terminal, _ = opened(tmp_path, ["X"], ["ESC"])
    run(menu.screen_discord())
    for key in speakers.TEXTS:
        assert key in text_of(terminal)


def test_a_broken_file_is_said_and_can_be_opened(tmp_path: Path, run: Any) -> None:
    menu, terminal, path = opened(tmp_path, ["ESC"])
    path.write_text("[broken", encoding="utf-8")
    run(menu.screen_discord())
    assert "isn't valid TOML" in text_of(terminal)


# Who says what -------------------------------------------------------------------------------------------------


def test_the_voice_and_the_hosts_own_words(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    assert "WELCOME! ACCESS GRANTED!" in say(load(path), "test.superintendent")
    path.write_text('voice = "plain"\n', encoding="utf-8")
    assert "ACCESS GRANTED" not in say(load(path), "test.superintendent")
    path.write_text('[text]\n"launcher.stopped" = "Down: {why}, {nonsense}"\n', encoding="utf-8")
    assert say(load(path), "launcher.stopped", why="crash", wait="5") == "Down: crash, {nonsense}"


def test_who_it_posts_as(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    assert speaker(load(path), "vergil") == {"username": "Vergil"}
    path.write_text('[personas]\nvergil = { name = "Warden", picture = "https://x/y.png" }\n', encoding="utf-8")
    assert speaker(load(path), "vergil") == {"username": "Warden", "avatar_url": "https://x/y.png"}
    assert speaker(load(path), "huragok") == {"username": "Huragok"}
    path.write_text('voice = "plain"\n', encoding="utf-8")
    assert speaker(load(path), "vergil") == {}
    path.write_text("[personas]\nenabled = false\n", encoding="utf-8")
    assert speaker(load(path), "vergil") == {}


def test_nothing_pings_unless_named() -> None:
    form = message("@everyone look", ping_roles=("5",))
    assert form["allowed_mentions"] == {"parse": [], "roles": ["5"]} and form["flags"] == speakers.SILENT
    assert "flags" not in message("x", silent=False)
    assert len(message("y" * 3000)["content"]) == 2000


def test_a_deleted_webhook_is_only_a_post_as_the_bot_and_a_new_one_next_time(tmp_path: Path, fake: FakeDiscord) -> None:
    path = tmp_path / "discord.toml"
    bot_ready(path)
    config, delivery = load(path), Delivery()
    delivery.to_channel(config, STAFF_CHANNEL, "vergil", message("one"))
    assert fake.posted[-1]["via"] == "webhook"
    fake.webhooks.clear()
    delivery.to_channel(config, STAFF_CHANNEL, "vergil", message("two"))
    assert fake.posted[-1]["via"] == "bot" and fake.posted[-1]["content"] == "**Vergil**: two"
    delivery.to_channel(config, STAFF_CHANNEL, "vergil", message("three"))
    assert fake.posted[-1]["via"] == "webhook" and len(fake.webhooks) == 1


def test_a_webhook_without_its_url_is_still_found(tmp_path: Path, fake: FakeDiscord) -> None:
    path = tmp_path / "discord.toml"
    bot_ready(path)
    hook = rest.create_webhook(TOKEN, STAFF_CHANNEL, speakers.WEBHOOK_NAME)
    del fake.webhooks[hook["id"]]["url"]  # Discord's documents don't promise it
    Delivery().to_channel(load(path), STAFF_CHANNEL, "vergil", message("one"))
    assert fake.posted[-1]["via"] == "webhook" and len(fake.webhooks) == 1


def test_a_channel_full_of_webhooks_gets_posts_from_the_bot(tmp_path: Path, fake: FakeDiscord) -> None:
    path = tmp_path / "discord.toml"
    bot_ready(path)
    fake.refuse_next.append((f"/channels/{STAFF_CHANNEL}/webhooks", 400, {"message": "Maximum webhooks", "code": 30007}, {}))
    delivery = Delivery()
    delivery.to_channel(load(path), STAFF_CHANNEL, "vergil", message("one"))
    assert fake.posted[-1]["via"] == "bot" and STAFF_CHANNEL in delivery.no_webhook


def test_the_check_fits_in_one_message() -> None:
    import pytest as _pytest

    _pytest.importorskip("discord")
    from huragok.discordbot.client import CHECK_TOTAL, findings_cards
    from huragok.discordbot.features import Finding

    cards = findings_cards([Finding(False, f"Thing {n}", "x" * 300) for n in range(100)])
    assert sum(len(card.description or "") for card in cards) < 6000 and CHECK_TOTAL < 6000
    assert "more: the window's check" in (cards[-1].description or "")
    assert cards[0].title == "100 things to fix"
    assert findings_cards([])[0].title == "Everything checks out"


def test_a_plain_voice_posts_as_the_bot_itself(tmp_path: Path, fake: FakeDiscord) -> None:
    path = tmp_path / "discord.toml"
    bot_ready(path)
    text = path.read_text(encoding="utf-8").replace('voice = "lore"', 'voice = "plain"')
    path.write_text(text, encoding="utf-8")
    Delivery().to_channel(load(path), STAFF_CHANNEL, "vergil", message("one"))
    assert fake.posted[-1]["via"] == "bot" and fake.posted[-1]["content"] == "one" and not fake.webhooks


def test_webhook_mode_posts_through_the_outbox_under_each_name(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    address = rest.create_webhook(TOKEN, STAFF_CHANNEL, "Mine")["url"]
    config = load(tmp_path / "discord.toml")

    async def scenario() -> None:
        outbox = Outbox()
        task = asyncio.ensure_future(outbox.run())
        Poster(lambda: config, outbox).post("superintendent", message("up"), webhook=address)
        for _ in range(500):
            if fake.posted:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())
    assert fake.posted[0]["username"] == "Superintendent" and fake.posted[0]["via"] == "webhook"


# The start loop ------------------------------------------------------------------------------------------------


def test_the_start_loop_says_huragok_is_down_when_a_webhook_is_set(tmp_path: Path, fake: FakeDiscord) -> None:
    settings = load_settings(tmp_path)
    launcher._tell_discord(settings, "exit code 1", 5)  # nothing set: nothing asked
    assert fake.requests == []
    address = rest.create_webhook(TOKEN, STAFF_CHANNEL, "Alerts")["url"]
    set_launcher_webhook(settings.home / "discord.toml", address)
    launcher._tell_discord(settings, "exit code 1", 15)
    post = fake.posted[-1]
    assert post["username"] == "Superintendent" and "flags" not in post  # it does notify
    assert "exit code 1" in post["content"] and "15 s" in post["content"] and "METROPOLITAN EMERGENCY!" in post["content"]


def test_a_webhook_that_fails_never_stops_the_start_loop(tmp_path: Path, fake: FakeDiscord) -> None:
    settings = load_settings(tmp_path)
    set_launcher_webhook(settings.home / "discord.toml", f"{rest.API}/webhooks/1/none")
    launcher._tell_discord(settings, "exit code 1", 5)
    assert "Couldn't tell Discord" in (settings.home / "logs" / "huragok.log").read_text(encoding="utf-8")


def load_settings(tmp_path: Path) -> Any:
    from huragok.core.settings import load as load_host_settings

    return load_host_settings(template_root(tmp_path))


# discord.toml as a host's file ---------------------------------------------------------------------------------


def test_the_first_start_writes_it_off(tmp_path: Path) -> None:
    settings = template_root(tmp_path)
    data = read_toml(settings.parent / "discord.toml")
    assert data["mode"] == "off"


def test_an_update_writes_it_once_and_never_again(tmp_path: Path) -> None:
    settings = host(tmp_path)
    path = settings.parent / "discord.toml"
    assert any("discord.toml: new" in line for line in up_to_date(settings))
    assert read_toml(path)["mode"] == "off"
    path.write_text('mode = "webhook"\n', encoding="utf-8")
    up_to_date(settings)
    assert read_toml(path)["mode"] == "webhook"


def test_notes_are_kept_up_to_date_and_the_hosts_values_and_notes_stay() -> None:
    text = notes.template_text("discord.toml")
    changed = text.replace('mode = "off"', 'mode = "bot"').replace("# Slash commands and right-click commands.",
                                                                    "# Slash commands and right-click commands.\n# my own note")
    changed = changed.replace('staff = ""                     # e.g. "#admin-log"', 'staff = "77"  # #mine')
    result = notes.refresh_text("discord.toml", changed)
    data = tomllib.loads(result.text)
    assert data["mode"] == "bot" and data["channels"]["staff"] == "77"
    assert "# my own note" in result.text and "# #mine" in result.text


def test_the_token_stays_through_the_notes_step(tmp_path: Path) -> None:
    settings = host(tmp_path)
    path = settings.parent / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    add_owner(path, "111", "the host")
    up_to_date(settings)
    data = read_toml(path)
    assert data["token"] == TOKEN and data["ranks"]["owners"] == ["111"]
