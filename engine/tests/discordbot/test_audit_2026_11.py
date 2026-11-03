"""What people do in the Discord, as the November 2026 audit found it (area 6b in plans/bug-audit-2026-11.md): each of
these did something it shouldn't, and was proven to before it was fixed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from conftest import ALICE, BRAVO
from test_bot_running import OTHER, answer_text
from test_tickets import ADMIN_ROLE, MOD_ROLE, MOD_USER, PROBATION_ROLE, REPORTS, STAFF, buttons_of, edited, form_of, posts_in, requests_file, row_of, said_back, up
from test_tickets import bot_file as tickets_file
from test_tickets import fake  # noqa: F401 - the fixture
from testkit.fake_discord import FakeDiscord
from testkit.running import SERVER, fleet, running, wait_for

from huragok.discordbot import records
from huragok.discordbot.ranks import as_they_may_see, times_a_kick

FIRST, SECOND = "LG Test Server", "Sim Slayer"
SCOPED_ROLE = "5200"
SCOPED_ADMIN = "5201"


async def choices(fake: FakeDiscord, token: str) -> list[dict[str, Any]]:
    await wait_for(lambda: any(c.get("token") == token for c in fake.callbacks), timeout=10)
    return next(c for c in fake.callbacks if c.get("token") == token)["data"]["choices"]


def scoped(path: Path, *, admin: bool = False) -> Path:
    """A moderator's role (and an admin's) that only counts on the first server."""
    from huragok.discordbot.config import set_switch

    set_switch(path, ("commands",), "enabled", True)
    extra = f'\n[[ranks.scoped]]\nrole = "{SCOPED_ROLE}"\nrank = "moderator"\nservers = ["{FIRST}"]\n'
    if admin:
        extra += f'\n[[ranks.scoped]]\nrole = "{SCOPED_ADMIN}"\nrank = "admin"\nservers = ["{FIRST}"]\n'
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return path


def scoped_roles(fake: FakeDiscord) -> None:
    fake.roles.append(FakeDiscord._role(SCOPED_ROLE, "First's mods", 1, "0"))
    fake.roles.append(FakeDiscord._role(SCOPED_ADMIN, "First's admins", 1, "0"))


# 6.8: a rank held on one server reached every server ---------------------------------------------------------------------


def test_a_time_anywhere_in_a_kick_takes_the_rank_held_everywhere() -> None:
    assert times_a_kick(["Alice", "1h"]) and times_a_kick(["Bravo", "perm"]) and times_a_kick(["x", "30M"])
    assert not times_a_kick(["Alice", "spawn", "killing"]) and not times_a_kick([])
    assert not times_a_kick(["Alice", "never", "again"])  # "never" starts a reason


def test_a_one_server_moderator_cant_confirm_a_kick_off_every_server_from_the_card(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot.config import set_switch

        path = scoped(tickets_file(tmp_path))
        set_switch(path, ("feed", "requests"), "enabled", True)
        scoped_roles(fake)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await consoles[FIRST].join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[FIRST].player_by_id(ALICE), timeout=10)
            fake.command("kick", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE, time="1h", reason="spam", server=FIRST)
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[-1]
            fake.press(buttons_of(post)["Confirm"], post, user=MOD_USER, roles=(SCOPED_ROLE,))
            await asyncio.sleep(2)
            assert not huragok.storage.query("SELECT 1 FROM actions WHERE target_id = ? AND kind = 'ban' AND ok = 1", (ALICE,))
            assert [row["status"] for row in huragok.storage.query("SELECT status FROM commands_requests")] == ["open"]

    run(scenario())


def test_a_one_server_rank_cant_reach_every_server_from_the_console_or_the_player_box(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from test_console import bot_file as console_file
    from test_console import reply_to

    async def scenario() -> None:
        scoped(console_file(tmp_path), admin=True)
        scoped_roles(fake)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            await consoles[FIRST].join(3, "Alice", ALICE)
            await consoles[FIRST].join(4, "Bravo", BRAVO)
            await wait_for(lambda: huragok.superintendent.states[FIRST].player_by_id(BRAVO), timeout=10)
            line = fake.message(STAFF, f"{FIRST}: !kick Alice 1h spam", roles=(SCOPED_ROLE,), user=MOD_USER)
            await reply_to(fake, line)
            line = fake.message(STAFF, f"{FIRST}: !kick Bravo perm cheating", roles=(SCOPED_ADMIN,), user="3555")
            await reply_to(fake, line)
            _, token = fake.command("kick", user=MOD_USER, roles=(SCOPED_ROLE,), player="Alice 1h", server=FIRST)
            assert "reaches every server" in await said_back(fake, token)
            _, token = fake.command("kick", user="3555", roles=(SCOPED_ADMIN,), player="Bravo perm", server=FIRST)
            assert "reaches every server" in await said_back(fake, token)
            await asyncio.sleep(1)
            assert not huragok.storage.query("SELECT 1 FROM actions WHERE kind IN ('ban', 'kick') AND ok = 1")
            assert not huragok.storage.query("SELECT 1 FROM commands_every_server")

    run(scenario())


# 6.9: a form acted on whoever the card was about when it was sent ---------------------------------------------------------


def test_a_form_opened_about_one_player_does_nothing_once_the_card_is_about_another(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        tickets_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(BRAVO, "Bravo", "!calladmin Alice is team killing")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            menu = next(item for row in post["components"] for item in row["components"] if item["type"] == 3)
            fake.choose(menu["custom_id"], post, [ALICE], user="3401", roles=(ADMIN_ROLE,))
            await wait_for(lambda: fake.original_edits, timeout=10)
            _, opened = fake.press(buttons_of(edited(fake))["Ban"], post, user="3401", roles=(ADMIN_ROLE,))
            assert "Alice" in (await form_of(fake, opened))["title"]
            before = len(fake.original_edits)
            fake.choose(menu["custom_id"], post, [BRAVO], user="3402", roles=(ADMIN_ROLE,))  # another admin: it's Bravo
            await wait_for(lambda: len(fake.original_edits) > before, timeout=10)
            assert row_of(huragok)["player_id"] == BRAVO
            _, token = fake.fill_in(opened, post, {"Why (they'll read this)": "team killing"}, user="3401", roles=(ADMIN_ROLE,))
            assert "Nothing was done" in await said_back(fake, token)
            await asyncio.sleep(1)
            assert not rcon.named("ban")

    run(scenario())


# 6.10: Confirm pressed twice acted twice ----------------------------------------------------------------------------------


def test_confirm_pressed_twice_acts_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        requests_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            moderation = huragok.vergil.moderation
            real_act = moderation.act

            async def slow_act(kind: str, *args: Any, **kwargs: Any) -> Any:
                if kind == "warn":
                    await asyncio.sleep(0.4)  # a busy server's answer
                return await real_act(kind, *args, **kwargs)

            moderation.act = slow_act
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.superintendent.states[SERVER].players, timeout=10)
            fake.command("warn", user=OTHER, roles=(PROBATION_ROLE,), player=ALICE, reason="language")
            await wait_for(lambda: posts_in(fake, REPORTS), timeout=15)
            post = posts_in(fake, REPORTS)[0]
            confirm = buttons_of(post)["Confirm"]
            fake.press(confirm, post, user=MOD_USER, roles=(MOD_ROLE,))
            await asyncio.sleep(0.15)  # a double click
            fake.press(confirm, post, user=MOD_USER, roles=(MOD_ROLE,))
            await asyncio.sleep(3)
            assert len(huragok.storage.query("SELECT 1 FROM actions WHERE target_id = ? AND kind = 'warn'", (ALICE,))) == 1

    run(scenario())


# 6.11: a moderator's pick from the list wasn't found ----------------------------------------------------------------------


def test_a_moderators_pick_from_the_list_is_found(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        from huragok.discordbot.config import set_switch

        set_switch(tickets_file(tmp_path), ("commands",), "enabled", True)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await wait_for(lambda: huragok.storage.player(ALICE) is not None, timeout=10)
            _, token = fake.typing("note", "add", "player", "lic", user=MOD_USER, roles=(MOD_ROLE,))
            value = (await choices(fake, token))[0]["value"]
            assert value.startswith("p:")  # a stand-in, as moderators don't see IDs
            _, token = fake.slash("note", "add", user=MOD_USER, roles=(MOD_ROLE,), player=value, text="seen spawn killing")
            assert "Nobody here has gone by" not in await answer_text(fake, token)
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM player_notes WHERE player_id = ?", (ALICE,)), timeout=10)
            _, token = fake.command("ticket", user=MOD_USER, roles=(MOD_ROLE,), player=value)
            assert "Nobody here has gone by" not in await answer_text(fake, token)

    run(scenario())


# 6.15 to 6.17 --------------------------------------------------------------------------------------------------------------


def test_the_claim_code_stops_working_for_whoever_ran_out_of_tries(tmp_path: Path) -> None:
    code = records.new_code(tmp_path)
    for _ in range(records.CLAIM_TRIES):
        assert not records.use_code(tmp_path, "WRONGONE", "stranger")
    assert not records.use_code(tmp_path, code, "stranger")  # the right one too, once theirs have run out
    assert records.use_code(tmp_path, code, "the host")


def test_a_public_answer_never_shows_a_whole_id() -> None:
    class File:
        def section(self, _name: str) -> dict[str, Any]:
            return {"moderators_see_ids": True}

    text = f"Alice is {ALICE}"
    assert as_they_may_see(File(), "owner", text) == text  # theirs alone
    assert ALICE not in as_they_may_see(File(), "owner", text, everyone=True) and ALICE[:8] in as_they_may_see(File(), "owner", text, everyone=True)


def test_a_pick_just_offered_is_the_last_forgotten(tmp_path: Path) -> None:
    from huragok.discordbot import part as part_module

    class Part:
        chosen: dict[str, str] = {}
        choice_salt = "salt"

    stand_in = Part()
    stand_in.chosen = {}
    most = part_module.MOST_CHOICES_KEPT
    first = part_module.DiscordPart.opaque(stand_in, "a" * 64)  # type: ignore[arg-type]
    for number in range(most - 1):
        part_module.DiscordPart.opaque(stand_in, f"{number:064x}")  # type: ignore[arg-type]
    part_module.DiscordPart.opaque(stand_in, "a" * 64)  # offered again
    part_module.DiscordPart.opaque(stand_in, "b" * 64)  # one more: the oldest goes, and it isn't the one just offered
    assert stand_in.chosen.get(first) == "a" * 64


# 6.13, 6.14 ----------------------------------------------------------------------------------------------------------------


def test_close_pressed_twice_on_a_contact_thread_keeps_one_transcript(run: Any, monkeypatch: Any) -> None:
    from huragok.discordbot import contact as contact_module

    said: list[str] = []
    monkeypatch.setattr(contact_module.rest, "post_message", lambda _token, _thread, form: said.append(form["content"]))
    monkeypatch.setattr(contact_module.rest, "close_thread", lambda *_args: None)
    monkeypatch.setattr(contact_module, "say", lambda _config, key, **_values: key)

    class Tickets:
        closed = 0

        def get(self, _number: int) -> dict[str, Any]:
            return {"state": "open", "thread": "77"}

        def close(self, _number: int, _who: str) -> None:
            self.closed += 1

    class File:
        token = "t"

        def section(self, _name: str) -> dict[str, Any]:
            return {"transcript": True}

    class Part:
        tickets = Tickets()
        file = File()

    contact = contact_module.Contact(Part())  # type: ignore[arg-type]
    kept: list[int] = []

    async def slow_keep(number: int, _ticket: Any, _thread: str) -> None:
        kept.append(number)
        await asyncio.sleep(0.2)

    contact._keep_transcript = slow_keep  # type: ignore[method-assign]

    async def scenario() -> None:
        await asyncio.gather(contact.close(1, "Mod"), contact.close(1, "Mod"))

    run(scenario())
    assert kept == [1] and said == ["contact.closed"] and Part.tickets.closed == 1
    assert not contact.closing  # and it can be closed again, if it's opened again


def test_a_clip_that_cant_be_posted_keeps_its_link(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    from test_reports import ALICE_ON, LOGS, on
    from test_reports import bot_file as reports_file

    async def scenario() -> None:
        reports_file(tmp_path)
        fake.upload_limit = 4  # a small server: Discord refuses the post, whatever the bot thought it took
        async with running(tmp_path) as (huragok, rcon, _):
            part = await up(huragok)
            await on(huragok, rcon, ALICE_ON)
            _, token = fake.command_with_file("report", "proof", "clip.mp4", "the clip itself", user=OTHER, player=ALICE)
            await said_back(fake, token)
            await wait_for(lambda: part.tickets.lines_of(1), timeout=15)
            line = part.tickets.lines_of(1)[0]["text"]
            assert "clip.mp4" in line and "/cdn/clip.mp4" in line  # pointed at, while Discord keeps it
            assert fake.refused == ["files over the 4-byte upload limit"]
            fake.refused.clear()
            assert not [post for post in fake.posted if post.get("channel") == LOGS and post.get("files")]

    run(scenario())


# 6.12 ----------------------------------------------------------------------------------------------------------------------


def test_shortening_by_someone_admin_on_one_server_lifts_nothing(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """Judged at their highest rank, Let back went through, the new kick (which reaches every server) was refused, and
    the player was let back with nothing against them. It's refused before anything's lifted now."""
    from conftest import OWNER
    from test_appeals import appeal
    from test_appeals import bot_file as appeals_file
    from test_tickets import LOGS, fill

    async def scenario() -> None:
        path = appeals_file(tmp_path, **{"for": ["ban", "kick"]})  # kicks can be appealed
        path.write_text(path.read_text(encoding="utf-8") + f'\n[[ranks.scoped]]\nrole = "{SCOPED_ADMIN}"\nrank = "admin"\nservers = ["{FIRST}"]\n', encoding="utf-8")
        scoped_roles(fake)
        async with fleet(tmp_path, up=(FIRST, SECOND)) as (huragok, consoles):
            await up(huragok)
            rcon = consoles[FIRST]
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(1, "Owner", OWNER)
            await wait_for(lambda: len(huragok.superintendent.states[FIRST].players) == 2, timeout=10)
            await rcon.chat(OWNER, "Owner", "!kick Alice 3d spawn killing")  # a kick with a time: its Let back stays on the one server
            await wait_for(lambda: rcon.named("ban"), timeout=15)
            await wait_for(lambda: posts_in(fake, LOGS), timeout=15)
            post = await appeal(fake)
            sent = await fill(fake, "Shorten it", post, {"How long from now": "12h"}, user="3555", roles=(SCOPED_ADMIN,))  # within a moderator's limit
            assert "takes moderator on all of them. Nothing was done" in await said_back(fake, sent)
            await asyncio.sleep(1)
            assert not rcon.named("unban") and not consoles[SECOND].named("unban") and not rcon.named("pardon")
            assert huragok.storage.query("SELECT 1 FROM actions WHERE target_id = ? AND kind IN ('unban', 'pardon') AND ok = 1", (ALICE,)) == []

    run(scenario())


# 6.3, 6.5, 6.7: delivery ---------------------------------------------------------------------------------------------------


def test_a_kept_post_tried_again_while_discord_is_down_is_one_job() -> None:
    """The console menu or the Contact card, posted again every 10 minutes while Discord's API was down, queued a copy
    each time, and every copy was posted (and pinned) once it came back (audit 2026-11, 6.3)."""
    from huragok.discordbot import kept
    from huragok.discordbot.outbox import Outbox

    class Status:
        now = 1000.0

        def clock(self) -> float:
            return self.now

        def stored(self, _key: str) -> tuple[str, str]:
            return "", ""  # never got there

        def store(self, _key: str, **_values: Any) -> None:
            pass

        def forget(self, _key: str) -> None:
            pass

    class Part:
        status = Status()
        kept_tried: dict[str, float] = {}
        kept_sending: dict[str, str] = {}
        outbox = Outbox()
        file = object()
        poster = type("Poster", (), {"delivery": None})()

    part = Part()
    for _ in range(3):
        kept.keep(part, "console-menu", "123", {"content": "the menu"})  # type: ignore[arg-type]
        part.status.now += kept.RETRY_SECONDS + 1
    assert len(part.outbox.jobs) == 1


def test_an_error_answer_that_cant_be_read_is_tried_again(monkeypatch: Any) -> None:
    """An error whose body was cut off (a timeout reading it) raised what nothing expected, and the job was dropped
    with no retry (audit 2026-11, 6.5)."""
    import urllib.error

    import pytest

    from huragok.discordbot import rest

    class CutOff:
        def read(self, *_args: Any) -> bytes:
            raise TimeoutError("The read operation timed out")

        def close(self) -> None:
            pass

    def urlopen(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, CutOff())  # type: ignore[arg-type]

    monkeypatch.setattr(rest.urllib.request, "urlopen", urlopen)
    with pytest.raises(rest.Unreachable):  # tried again, as an outage is
        rest.send("POST", "https://discord.com/api/v10/channels/1/messages", token="t", body={"content": "x"})


def test_a_board_refused_reaches_the_refused_channels(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    """The board's posts carried no channel tag, so a refused board never reached `/huragok check`'s or the
    dashboard's list of refused channels (audit 2026-11, 6.7)."""
    from test_status import bot_file as status_file
    from test_status import up as status_up

    from testkit.fake_discord import PUBLIC_CHANNEL

    async def scenario() -> None:
        status_file(tmp_path, "board")
        async with running(tmp_path) as (huragok, _rcon, _):
            part = await status_up(huragok)
            refused = {"message": "Missing Access", "code": 50001}
            # Through its webhook or as the bot, whichever it has: refused either way
            fake.refuse_next += [("/webhooks/", 403, refused, {}), (f"/channels/{PUBLIC_CHANNEL}/messages", 403, refused, {})]
            await part.status.tick()
            await wait_for(lambda: any(tag.startswith(("channel:", "webhook:")) for tag in part.feeds.blocked), timeout=15)
            fake.refuse_next.clear()

    run(scenario())


# Decision 12 (the host's call, 2 Nov 2026): a ticket's webhook post Discord took but answered too late for --------------


def test_a_ticket_card_discord_took_but_answered_late_for_is_one_card(tmp_path: Path, run: Any, fake: FakeDiscord, monkeypatch: Any) -> None:
    """A webhook's post takes no nonce: after a timeout or a 5xx its retry posted a second card nothing updates. The
    retry reads the channel first, and the card already there with the ticket's buttons is the one."""
    from huragok.discordbot import rest

    real = rest.execute_webhook
    calls: list[dict[str, Any]] = []

    def answered_late(address: str, payload: dict[str, Any]) -> Any:
        ticket = "huragok:t:1:" in json.dumps(payload.get("components") or [])
        if ticket:
            calls.append(payload)
        reply = real(address, payload)  # Discord takes it...
        if ticket and len(calls) == 1:
            raise rest.Unreachable("couldn't reach Discord: TimeoutError")  # ...and the answer never comes
        return reply

    monkeypatch.setattr(rest, "execute_webhook", answered_late)

    async def scenario() -> None:
        tickets_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            await up(huragok)
            await rcon.join(3, "Alice", ALICE)
            await rcon.join(4, "Bravo", BRAVO)
            await wait_for(lambda: len(huragok.superintendent.states[SERVER].players) == 2, timeout=10)
            await rcon.chat(BRAVO, "Bravo", "!calladmin Alice is team killing")
            await wait_for(lambda: huragok.storage.query("SELECT 1 FROM discord_tickets WHERE number = 1 AND message != ''"), timeout=30)
            await asyncio.sleep(1)
            cards = posts_in(fake, REPORTS)
            assert len(calls) == 1 and len(cards) == 1  # one card, adopted: not posted again
            assert row_of(huragok)["message"] == cards[0]["id"]

    run(scenario())
