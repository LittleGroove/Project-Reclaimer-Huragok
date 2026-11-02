"""What mustn't get out, or be made to look like something it isn't. From the security audit of 1 November (plans/discord-audit/security-2.md): each goes through the real path."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO
from test_audit_findings import ADMIN_USER, FIRST, SECOND, SCOPED, on, said_back, up  # noqa: F401
from test_audit_findings import bot_file as audit_file
from test_console import bot_file as console_file
from test_console import reply_to
from test_tickets import bot_file, fake, fill, posts_in, strike  # noqa: F401
from testkit.fake_discord import ADMIN_ROLE, MOD_ROLE, PUBLIC_CHANNEL, STAFF_CHANNEL, FakeDiscord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.discordbot import cards, records
from huragok.discordbot.config import load, make_file, set_channel, set_message, set_switch, set_token
from huragok.discordbot.startover import start_over
from huragok.players.relay import line_refusal


def test_a_console_answer_has_no_address_in_it_whoever_asked(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Moderators read this channel because they use ! commands there. An admin's `players` put every address in it."""
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = huragok.runners["discord"].part
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            rcon.replies["players"] = lambda _message: {"ok": True, "text": f"#3 Alice {ALICE} 203.0.113.77 / 2001:db8::1"}
            line = fake.message(STAFF_CHANNEL, "players", roles=(ADMIN_ROLE,), user="4441")
            said = (await reply_to(fake, line))["content"]
            assert "203.0.113.77" not in said and "2001:db8" not in said and ALICE not in said

    run(scenario())


def test_a_name_cant_put_a_link_or_a_heading_on_a_card() -> None:
    masked = cards.clean("[View the clip on Discord](https://evil.example/login)")
    assert "[View" not in masked.replace("\\[View", "") and "\\[" in masked and "\\]" in masked
    heading = cards.clean("line\n# Approved by the owner\n- item\n1. one\n-# small")
    for start in ("# Approved", "- item", "1. one", "-# small"):
        assert f"\n{start}" not in heading, start


def test_compressed_addresses_go_too() -> None:
    assert "2001:db8::1" not in cards.no_addresses("from 2001:db8::1 today")
    assert "12:34:56" in cards.no_addresses("at 12:34:56 today")  # a time isn't one


def test_a_players_whole_id_isnt_in_what_report_offers(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Anyone can read what an autocomplete sends. It's what roles are keyed on."""
    from huragok.discordbot.asking import on_now, who_is_meant

    async def scenario() -> None:
        audit_file(tmp_path, ("reports",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            part = await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            interaction = type("I", (), {"client": type("C", (), {"part": part})(), "guild_id": 0})()
            choices = await on_now(interaction, "Ali")
            assert choices and all(ALICE not in choice.value for choice in choices)
            assert who_is_meant(part, choices[0].value)[0] == ALICE

    run(scenario())


def test_a_rank_on_one_server_doesnt_reach_every_server(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """A moderator scoped to one server made a global moderator in commands.toml, in three presses."""
    async def scenario() -> None:
        audit_file(tmp_path, ("commands",), text=SCOPED.replace('rank = "moderator"', 'rank = "admin"'))
        fake.roles.append(FakeDiscord._role("5200", "First's admins", 1, "0"))
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            sent = len(consoles[FIRST].named("ban"))
            _, token = fake.command("ban", user="7777", roles=("5200",), player=ALICE, reason="x", server=FIRST)
            assert "reaches every server" in await said_back(fake, token)
            assert len(consoles[FIRST].named("ban")) == sent

    run(scenario())


def test_contact_cant_be_opened_again_and_again(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path, ("contact",))
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, _):
            part = await up(huragok)
            number = part.tickets.open_as("contact-opened", cards.contacted(part.file, "Someone", "", "hello", "contact:7001"), "reports")
            part.tickets.finish(number, "closed")
            assert part.contact.next_allowed("7001") > 0  # its own wait, though it's closed
            assert part.contact.next_allowed("7002") == 0

    run(scenario())


def test_a_token_in_the_wrong_box_isnt_said_back(tmp_path: Path) -> None:
    secret = "MTIz.abc.defghijklmnopqrstuvwxyz1234567"
    path = tmp_path / "discord.toml"
    path.write_text(f'token_variable = "{secret}"\nguild = "{secret}"\n', encoding="utf-8")
    config = load(path)
    assert secret not in " ".join(config.warnings) and config.warnings
    path.write_text(f'mode = "{secret}"\n', encoding="utf-8")
    assert secret not in load(path).error


def test_a_public_log_keeps_a_card_where_it_is(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Who reported whom, and what was said, isn't for anyone to read: it opens with that checked, and closing it wasn't."""
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_channel(path, "logs", PUBLIC_CHANNEL, "servers")
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            post = await strike(huragok, rcon, fake)
            number = int(huragok.storage.query("SELECT number FROM discord_tickets ORDER BY number DESC LIMIT 1")[0]["number"])
            part.tickets.get(number)
            set_switch(path, ("feeds",), "public", False)
            ticket = part.tickets.get(number)
            huragok.storage.execute("UPDATE discord_tickets SET log_in = 'logs' WHERE number = ?", (number,))
            assert not part.tickets.finish(number, "closed")  # it didn't move
            assert not [one for one in posts_in(fake, PUBLIC_CHANNEL) if one["id"] != post["id"]]
            assert part.tickets.keeps_in(part.tickets.get(number)) == str(ticket["channel"])

    run(scenario())


def test_a_name_like_vergils_isnt_taken_for_vergil() -> None:
    from types import SimpleNamespace

    from huragok.discordbot.client import acting_as

    part = SimpleNamespace(file=SimpleNamespace(section=lambda *_: {}))
    assert acting_as(part, SimpleNamespace(display_name="Vergil", name="x")) != "Vergil"
    assert acting_as(part, SimpleNamespace(display_name="vergil", name="x")).startswith("vergil")
    assert acting_as(part, SimpleNamespace(display_name="Mod", name="x")) == "Mod"


def test_who_it_really_is_is_on_the_card(tmp_path: Path) -> None:
    config = load(tmp_path / "none.toml")
    card = cards.contacted(config, "ModAlice", "", "hi", "contact:7001", mention=" (<@7001>)")
    assert "<@7001>" in card.line and "**ModAlice**" in card.line


def test_a_quoted_name_is_refused_unless_its_plain() -> None:
    for line in ('kick "Boss"x annoying', 'kick "Boss', "kick 'Boss annoying", 'startvote kick "Boss"x'):
        assert "quoted oddly" in line_refusal(line), line
    for line in ('kick "Master Chief" annoying', "kick #3", "kick Boss annoying", 'say "hello"x', 'kick "Boss"'):
        assert not line_refusal(line), line


def test_a_password_goes_when_they_say_no(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from test_console import buttons, press_until

    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = huragok.runners["discord"].part
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            line = fake.message(STAFF_CHANNEL, "password hunter2", roles=(ADMIN_ROLE,), user="4441")
            asked = await reply_to(fake, line)
            assert "hunter2" not in asked["content"]
            await wait_for(lambda: line in fake.deleted, timeout=10)  # it goes while they decide
            await press_until(fake, lambda: any(c.get("type") == 7 for c in fake.callbacks), buttons(asked)["No"], asked, roles=(ADMIN_ROLE,), user="4441")

    run(scenario())


def test_a_password_goes_when_their_rank_is_refused(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        console_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = huragok.runners["discord"].part
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            line = fake.message(STAFF_CHANNEL, "password hunter2", roles=(MOD_ROLE,), user="4442")
            await reply_to(fake, line)
            await wait_for(lambda: line in fake.deleted, timeout=10)

    run(scenario())


def test_a_reason_thats_only_a_time_isnt_read_as_how_long(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        audit_file(tmp_path)
        async with fleet(tmp_path, up=(FIRST,)) as (huragok, consoles):
            await up(huragok)
            await on(huragok, consoles[FIRST], FIRST)
            await huragok.vergil.moderation.act("mute", FIRST, ALICE, by="Mod", source="commands", reason="7d")
            assert consoles[FIRST].named("mute")[-1]["args"][1:] == ["7d."]

    run(scenario())


def test_wrong_claim_codes_from_one_person_dont_burn_it_for_everyone(tmp_path: Path) -> None:
    code = records.new_code(tmp_path)
    for _ in range(records.CLAIM_TRIES + 2):
        assert not records.use_code(tmp_path, "WRONGONE", "stranger")
    assert records.use_code(tmp_path, code, "the host")


def test_the_check_says_when_other_roles_can_read_what_staff_read(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from huragok.discordbot import features
    from huragok.discordbot.facts import gather
    from testkit.fake_discord import GUILD, TOKEN

    path = audit_file(tmp_path)
    role = FakeDiscord._role("5299", "Member", 1, str(1 << 10))
    fake.roles.append(role)
    channel = next(one for one in fake.channels if one["id"] == STAFF_CHANNEL)
    channel.setdefault("permission_overwrites", []).append({"id": role["id"], "type": 0, "allow": str(1 << 10), "deny": "0"})
    findings = features._others_who_can_read(load(path), gather(TOKEN, GUILD))
    assert any("@Member can read it too" in finding.text for finding in findings)


def test_the_check_warns_of_more_than_the_bot_needs(tmp_path: Path, fake: FakeDiscord) -> None:
    from huragok.discordbot import features
    from huragok.discordbot.facts import gather
    from testkit.fake_discord import BOT_ROLE, GUILD, TOKEN

    audit_file(tmp_path)
    role = next(one for one in fake.roles if one["id"] == BOT_ROLE)
    role["permissions"] = str(int(role["permissions"]) | (1 << 3))
    assert any("Administrator" in finding.text for finding in features._more_than_it_needs(gather(TOKEN, GUILD)))


def test_the_rank_check_doesnt_tell_a_host_to_open_staff_to_members(tmp_path: Path, fake: FakeDiscord) -> None:
    from huragok.discordbot import features
    from huragok.discordbot.facts import gather
    from testkit.fake_discord import GUILD, TOKEN

    path = audit_file(tmp_path)
    fake.roles.append(FakeDiscord._role("5298", "Member", 1, "0"))
    text = path.read_text(encoding="utf-8").replace("\nmember = []", '\nmember = ["5298"]')
    path.write_text(text, encoding="utf-8")
    assert not [finding for finding in features._ranks(load(path), gather(TOKEN, GUILD)) if finding.where == "Rank member" and not finding.good]


def test_a_ping_for_a_deleted_role_gets_a_finding(tmp_path: Path, fake: FakeDiscord) -> None:
    from huragok.discordbot import features
    from huragok.discordbot.facts import gather
    from testkit.fake_discord import GUILD, TOKEN

    path = audit_file(tmp_path)
    set_message(path, "alerts-crashed", ping="999999")
    assert any(finding.where == "Message alerts-crashed" for finding in features._pings(load(path), gather(TOKEN, GUILD)))


def test_start_over_keeps_a_token_variable(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    make_file(path)
    set_token(path, "x")
    text = path.read_text(encoding="utf-8").replace('token = "x"', 'token = ""').replace('token_variable = ""', 'token_variable = "MY_TOKEN"')
    path.write_text(text, encoding="utf-8")
    start_over(path, tmp_path / "none.sqlite3")
    assert load(path).data["token_variable"] == "MY_TOKEN"
