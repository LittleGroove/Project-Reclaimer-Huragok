"""The feeds: which event makes which card, what each says, where it goes, and how they keep from flooding."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from conftest import ALICE
from testkit.fake_discord import GUILD, MOD_ROLE, OWNER_USER, PUBLIC_CHANNEL, STAFF_CHANNEL, TOKEN, FakeDiscord, use_fake_discord
from testkit.running import SERVER, running, wait_for

from huragok.core.events import Event
from huragok.discordbot import cards, features, rest
from huragok.core.tomledit import read_toml
from huragok.discordbot.cards import clean
from huragok.discordbot.config import (
    DEFAULT_COLOUR, add_owner, colour_value, load, make_file, set_channel, set_guild, set_message, set_mode, set_switch, set_token,
)
from huragok.discordbot.facts import Facts, gather
from huragok.discordbot.feeds import SIMPLE, Feeds, sample_cards


class Part:
    """As much of the part as the feeds use: what's posted is kept."""

    def name_of(self, player_id: str, given: str = "") -> str:
        return given or player_id[:8]

    def __init__(self, path: Path, facts: Facts | None = None) -> None:
        self.file = load(path)
        self.facts = facts
        self.real_names: dict[str, str] = {}
        self.status = SimpleNamespace(stored=lambda _key: ("", ""), forget=lambda _key: None, store=lambda *_a, **_k: None, clock=time.time)
        self.posts: list[dict[str, Any]] = []
        self.poster = SimpleNamespace(post=self._post)
        self.players: list[Any] = []
        self.visits: dict[str, float | None] = {}
        self.names: dict[str, str] = {}

    def _post(self, persona: str, form: dict[str, Any], **where: Any) -> None:
        self.posts.append({"persona": persona, **form, **where})

    def players_on(self, server: str) -> list[Any]:
        return self.players

    def last_visit(self, player_id: str) -> float | None:
        return self.visits.get(player_id)

    def old_name(self, player_id: str, name: str, when: float) -> str:
        return self.names.get(player_id, "")

    def record_of(self, player_id: str, case: str = "") -> str:
        return ""

    def said_by(self, player_id: str, lines: int, before: float) -> list[str]:
        return []

    def kicked_lately(self, player_id: str, seconds: float, now: float) -> Any:
        return None

    def banned_elsewhere(self, player_id: str, server: str) -> list[str]:
        return []

    def staff_names(self) -> list[tuple[str, str]]:
        return []

    def flags_of(self, player_id: str) -> list[Any]:
        return []

    def noted(self, player_id: str, lines: int) -> str:
        return ""


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def feeds_for(tmp_path: Path, body: str, *, mode: str = "bot") -> tuple[Feeds, Part, Clock]:
    """Feeds on a file with `body`, its channels resolved by ID so no Discord is asked."""
    path = tmp_path / "discord.toml"
    path.write_text(f'mode = "{mode}"\n[channels]\nstaff = "{STAFF_CHANNEL}"\nalerts = "{STAFF_CHANNEL}"\ngames = "{PUBLIC_CHANNEL}"\nlogs = "{LOGS}"\n{body}',
                    encoding="utf-8")
    part, clock = Part(path), Clock()
    return _with_tickets(part, clock), part, clock


def _with_tickets(part: Part, clock: Clock) -> Feeds:
    """What's a ticket is test_tickets's: here it only has to be posted as a card."""
    feeds = Feeds(part, clock=clock)  # type: ignore[arg-type]
    part.reports = part.appeals = SimpleNamespace(case=lambda _event, _pressed=0: False)
    part.tickets = SimpleNamespace(update_worked=lambda: None, stopped_by_someone=lambda _server, _who: None, open=lambda card, options: feeds._send(card, {**options, "also": []}, one_line=False), take=lambda event: False, of_player=lambda player_id, leaving=0: None, signed_in=lambda server: None, slow_over=lambda server: None, problem_over=lambda server: False)
    return feeds


def kick(**more: Any) -> Event:
    return Event("moderation.action", "Alpha", {"kind": "kick", "target_id": ALICE, "target_name": "Alice", "by": "Mod", "source": "commands",
                                                "reason": "spam", "duration": None, "dry_run": False, "ok": True, "text": "", "case": 1432, **more})


ON = "[feed.moderation]\nenabled = true\n"
LOGS = "4009"


# What a card says ---------------------------------------------------------------------------------------------------


def test_a_kick_says_who_what_why_and_its_case(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick())
    [post] = part.posts
    card = post["embeds"][0]
    assert card["description"] == "Mod kicked **Alice** (spam)" and card["footer"]["text"] == "Alpha · case 1432"
    assert card["title"] == "Kick · CRIME DOESN'T PAY!"
    assert post["channel"] == LOGS and post["persona"] == "vergil" and post["flags"] == 4096
    assert post["allowed_mentions"] == {"parse": [], "roles": []}


def test_the_plain_voice_has_plain_headings(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.config.data["voice"] = "plain"
    feeds.handle(kick())
    assert part.posts[0]["embeds"][0]["title"] == "Kick"


def test_the_compact_style_makes_a_post_one_line(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, '[feed.games]\nenabled = true\nmin_players = 0\nstyle = "compact"\n[messages]\ngames-started = { post = true }\n')
    feeds.handle(Event("rcon.phase", "Alpha", {"phase": "playing", "map": "Guardian", "mode": "Slayer"}))
    [post] = part.posts
    assert post["content"].startswith("Alpha · ") and "Guardian" in post["content"] and "embeds" not in post


def test_in_a_log_channel_what_has_nothing_to_act_on_is_one_line(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.votes]\nenabled = true\n")
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "passed", "subject": "end the game", "caller": "Alice"}))
    [post] = part.posts
    assert post["channel"] == LOGS and post["content"].startswith("Alpha · ") and "embeds" not in post


def test_a_person_acting_is_a_card_in_a_log_channel(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick())
    assert part.posts[0]["channel"] == LOGS and "embeds" in part.posts[0]


def test_vergils_own_action_makes_no_card_as_its_strike_does(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick(source="automod", by="Vergil"))
    assert part.posts == []


def test_any_text_can_be_reworded(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON + '[text]\n"moderation.kick" = "{player} got the boot from {by}"\n')
    feeds.handle(kick())
    assert part.posts[0]["embeds"][0]["description"] == "**Alice** got the boot from Mod"


@pytest.mark.parametrize("text, hidden", [("spam from 203.0.113.9:11774", "203.0.113.9"), ("on 2001:db8:1:2:3:4:5:6", "2001:db8")])
def test_no_address_is_ever_posted(tmp_path: Path, text: str, hidden: str) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick(reason=text))
    assert hidden not in str(part.posts) and "(hidden)" in str(part.posts)


def test_a_players_name_cant_format_or_ping(tmp_path: Path) -> None:
    assert clean("@everyone") == "@​everyone" and clean("**bold** `x`") == "\\*\\*bold\\*\\* \\`x\\`"
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick(target_name="@everyone **x**"))
    text = part.posts[0]["embeds"][0]["description"]
    assert "@everyone" not in text and "**x**" not in text


def test_a_failed_action_says_so(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick(ok=False, text="no such player"))
    card = part.posts[0]["embeds"][0]
    assert "Didn't work: Mod kicked **Alice** (spam) (no such player)" in card["description"]


def test_an_action_for_someone_not_on_says_when(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick(pending=True))
    assert part.posts[0]["embeds"][0]["fields"][0]["value"] == "As they next join: they aren't on now"


def test_a_cards_colour_is_its_lines(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON + '[messages]\nmoderation-kick = { colour = "#112233" }\n')
    feeds.handle(kick())
    assert part.posts[0]["embeds"][0]["color"] == 0x112233


def test_every_key_a_card_uses_has_its_words() -> None:
    import inspect
    import re

    from huragok.discordbot import feeds as module
    from huragok.discordbot.speakers import TEXTS

    source = inspect.getsource(cards) + inspect.getsource(module)
    used = set(re.findall(r'"((?:moderation|vergil|vote|alert|server|game|join|content|audit)\.[a-z_]+)"', source)) - set(SIMPLE) - {"moderation.action", "moderation.seen"}
    used |= {f"vote.{state}" for state in cards.VOTE_STATES} | set(cards.VERBS.values()) | set(cards.JOIN_TEXTS.values())
    assert used - set(TEXTS) == set() or used - set(TEXTS) <= {"server.ready", "server.stopped", "server.recovered", "server.countdown_cancelled", "server.plan_cancelled", "server.crashed"}  # the last few are events


# Which feed wants it ------------------------------------------------------------------------------------------------


def test_a_feed_thats_off_or_not_built_posts_nothing(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "")
    feeds.handle(kick())
    assert part.posts == []
    feeds, part, _ = feeds_for(tmp_path, "[feed.watch]\nenabled = true\n")
    assert feeds.wanted(cards.Card("kills", "x", "x")) is None and feeds.wanted(cards.Card("watch", "x", "x")) is None


def test_mode_off_posts_nothing(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON, mode="off")
    feeds.handle(kick())
    assert part.posts == []


def test_servers_and_sources_narrow_a_feed(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON + 'servers = ["Beta"]\n')
    feeds.handle(kick())
    assert part.posts == []
    feeds, part, _ = feeds_for(tmp_path, ON + 'sources = ["/"]\n')
    feeds.handle(kick())
    assert part.posts == []
    feeds.handle(Event("moderation.seen", "Alpha", {**kick().data, "source": "in game", "case": 9}))
    assert len(part.posts) == 1


def test_dry_run_is_for_vergils_feed_alone(tmp_path: Path) -> None:
    body = ON + "[feed.vergil]\nenabled = true\n"
    feeds, part, _ = feeds_for(tmp_path, body)
    feeds.handle(kick(dry_run=True, source="automod"))
    assert part.posts == []
    strike = Event("automod.strike", "Alpha", {"player_id": ALICE, "player_name": "Alice", "rule": "slurs", "strike": 2, "action": "mute 1h",
                                               "evidence": "a bad word", "dry_run": True, "ok": True})
    feeds.handle(strike)
    assert "Vergil's only watching: **Alice** would get strike" in part.posts[0]["content"]  # nothing to act on, so one line
    feeds, part, _ = feeds_for(tmp_path, "[feed.vergil]\nenabled = true\n[messages]\nvergil-would = { post = false }\n")
    feeds.handle(strike)
    assert part.posts == []


def test_a_copy_goes_to_a_partners_webhook(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON + 'also = ["https://discord.com/api/webhooks/1/x"]\n')
    feeds.handle(kick())
    assert [p.get("channel") or p.get("webhook") for p in part.posts] == [LOGS, "https://discord.com/api/webhooks/1/x"]


def test_webhook_mode_posts_only_where_it_has_an_address(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON, mode="webhook")
    feeds.handle(kick())
    assert part.posts == []
    feeds, part, _ = feeds_for(tmp_path, ON + 'webhook = "https://discord.com/api/webhooks/1/x"\n', mode="webhook")
    feeds.handle(kick())
    assert part.posts[0]["webhook"] == "https://discord.com/api/webhooks/1/x"


def test_a_ping_only_for_the_kinds_it_says(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, f'[feed.alerts]\nenabled = true\n[messages]\nalerts-crashed = {{ ping = "{MOD_ROLE}" }}\n')
    feeds.handle(Event("server.crashed", "Alpha", {"why": "exit code 1"}))
    [post] = part.posts
    assert post["content"].startswith(f"<@&{MOD_ROLE}>") and post["allowed_mentions"]["roles"] == [MOD_ROLE] and "flags" not in post
    feeds.handle(Event("server.adopted", "Beta", {}))
    feeds.handle(Event("supervisor.started", None, {"version": "1.0"}))
    assert "<@&" not in str(part.posts[-1]["content"] if "content" in part.posts[-1] else "")


# Every message is a setting --------------------------------------------------------------------------------------------


def test_every_line_of_the_file_has_a_card_and_every_card_a_line(tmp_path: Path) -> None:
    config = load(tmp_path / "none")
    made = {card.message for card in sample_cards(config)}
    assert made == set(config.section("messages")) and len(made) >= 50
    assert all(card.line and "{" not in card.line for card in sample_cards(config))


def test_a_message_goes_where_its_line_says(tmp_path: Path) -> None:
    body = '[feed.votes]\nenabled = true\n[feed.moderation]\nenabled = true\n[messages]\nvotes-failed = { channel = "#somewhere", colour = "#123456" }\n'
    path = tmp_path / "discord.toml"
    path.write_text(f'mode = "bot"\n[channels]\nstaff = "{STAFF_CHANNEL}"\nlogs = "{LOGS}"\n{body}', encoding="utf-8")
    part, clock = Part(path), Clock()
    feeds = _with_tickets(part, clock)
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "called", "subject": "kick Bravo", "caller": "Alice"}))
    assert part.posts[-1]["channel"] == LOGS and "content" in part.posts[-1]  # the default: logs, one line
    feeds.handle(kick())
    assert part.posts[-1]["channel"] == LOGS and part.posts[-1]["embeds"][0]["color"] == 0xE67E22  # orange
    part.facts = SimpleNamespace(guild=None, find_channel=lambda ref: {"id": "77", "name": "somewhere"} if ref == "#somewhere" else None, find_role=lambda ref: None)
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "failed", "subject": "kick Bravo"}))
    assert part.posts[-1]["channel"] == "77" and "content" in part.posts[-1]  # sent to a channel of its own, it's still what it was: a line


def test_one_message_can_be_switched_off_and_the_rest_of_its_feed_stays(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.votes]\nenabled = true\n[messages]\nvotes-called = { post = false }\n")
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "called", "subject": "x"}))
    assert part.posts == []
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "failed", "subject": "x"}))
    assert len(part.posts) == 1


def test_a_refused_channel_doesnt_stop_the_feeds_others(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.votes]\nenabled = true\n[messages]\nvotes-failed = { channel = \"staff\" }\n")
    feeds.refused(f"channel:{LOGS}", "no")
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "called", "subject": "x"}))
    assert part.posts == []
    feeds.handle(Event("rcon.vote", "Alpha", {"state": "failed", "subject": "x"}))
    assert part.posts[0]["channel"] == STAFF_CHANNEL


def test_a_colour_that_isnt_one_is_said(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    path.write_text('[messages]\nvotes-called = { colour = "not a colour" }\n[colours]\nmine = "#010203"\n', encoding="utf-8")
    config = load(path)
    assert config.warnings == ["discord.toml: [messages] votes-called: there's no colour 'not a colour': a name from [colours], or \"#RRGGBB\""]
    assert colour_value(config, "not a colour") == DEFAULT_COLOUR and colour_value(config, "mine") == 0x010203
    assert colour_value(config, "sky") == 0x5DADE2 and colour_value(config, "#ABCDEF") == 0xABCDEF


def test_every_default_colour_is_on_the_wheel_and_there_are_plenty(tmp_path: Path) -> None:
    config = load(tmp_path / "none")
    assert not config.warnings and len(config.data["colours"]) >= 30
    used = {line["colour"] for line in config.section("messages").values()}
    assert len(used) >= 25 and used <= set(config.data["colours"])


def test_the_gallery_is_every_message_in_a_few_posts(tmp_path: Path) -> None:
    feeds, _, _ = feeds_for(tmp_path, "")
    posts = feeds.gallery()
    assert 1 < len(posts) <= 8 and all(len(post) <= 10 for post in posts)
    shown = [card["title"] for post in posts for card in post]
    assert len(shown) == len(feeds.config.section("messages"))
    assert "votes-called → #" in " ".join(shown) and any("· off" in title for title in shown)
    colours = {card["color"] for post in posts for card in post}
    assert len(colours) >= 25
    assert all(card["footer"]["text"] for post in posts for card in post)


def test_set_message_changes_only_what_it_says(tmp_path: Path) -> None:
    path = tmp_path / "discord.toml"
    make_file(path)
    set_message(path, "votes-called", channel="staff")
    set_message(path, "votes-called", post=False)
    line = read_toml(path)["messages"]["votes-called"]
    assert line == {"channel": "staff", "colour": "sky", "post": False, "ping": ""}
    assert "# Every kind of message the feeds post" in path.read_text(encoding="utf-8")
    assert read_toml(path)["messages"]["votes-failed"]["channel"] == "logs"


# Not flooding ---------------------------------------------------------------------------------------------------------


def test_a_burst_is_the_first_then_one_card(tmp_path: Path) -> None:
    feeds, part, clock = feeds_for(tmp_path, "[feed.joins]\nenabled = true\n")
    for number in range(5):
        feeds.handle(_joined(f"{number}" * 64, f"P{number}"))
    assert len(part.posts) == 1  # the first, at once
    feeds.flush()
    assert len(part.posts) == 1  # the window isn't over
    clock.now += 31
    feeds.flush()
    assert len(part.posts) == 2
    text = part.posts[1]["content"]
    assert all(f"**P{n}**" in text for n in range(1, 5)) and "**P0**" not in text


def test_a_big_burst_is_still_one_card(tmp_path: Path) -> None:
    feeds, part, clock = feeds_for(tmp_path, "[feed.joins]\nenabled = true\n")
    for number in range(100):
        feeds.handle(_joined(f"{number:02d}" * 32, f"P{number}"))
    clock.now += 31
    feeds.flush()
    assert len(part.posts) == 2 and "…and 69 more" in part.posts[1]["content"]


def test_stopping_says_what_was_waiting(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick())
    feeds.handle(kick(target_name="Bravo"))
    feeds.flush(everything=True)
    assert len(part.posts) == 2 and feeds.bursts == {}


def test_different_kinds_and_servers_arent_merged(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    feeds.handle(kick())
    feeds.handle(kick(kind="ban"))
    feeds.handle(Event("moderation.action", "Beta", kick().data))
    assert len(part.posts) == 3


def test_an_ongoing_problem_posts_once_then_says_when_its_over(tmp_path: Path) -> None:
    feeds, part, clock = feeds_for(tmp_path, "[feed.alerts]\nenabled = true\nrepeat_minutes = 30\n")
    crash = Event("server.crashed", "Alpha", {"why": "exit code 1"})
    feeds.handle(crash)
    feeds.handle(crash)
    clock.now += 60
    feeds.handle(crash)
    assert len(part.posts) == 1
    clock.now += 1800
    feeds.handle(crash)
    assert len(part.posts) == 2  # still going, half an hour on
    clock.now += 60
    feeds.handle(Event("server.ready", "Alpha", {}))
    assert "It's back after 32 minutes" in part.posts[-1]["content"]
    assert feeds.problems == {}
    feeds.handle(Event("server.ready", "Alpha", {}))
    assert len(part.posts) == 3  # no problem, no recovery


def test_a_restart_on_purpose_isnt_a_recovery(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.alerts]\nenabled = true\n")
    feeds.handle(Event("server.ready", "Alpha", {}))
    assert part.posts == []


def test_performance_alerts_wait_out_a_start(tmp_path: Path) -> None:
    feeds, part, clock = feeds_for(tmp_path, "[feed.alerts]\nenabled = true\ngrace_seconds = 120\n")
    feeds.server_up("Alpha")
    slow = Event("watchdog.health", "Alpha", {"level": "unwell", "previous": "healthy", "reasons": ["slow"]})
    feeds.handle(slow)
    assert part.posts == []
    feeds.handle(Event("server.crashed", "Alpha", {"why": "x"}))
    assert len(part.posts) == 1  # a crash isn't performance
    clock.now += 121
    feeds.handle(slow)
    assert len(part.posts) == 2
    feeds.handle(Event("watchdog.health", "Alpha", {"level": "healthy", "previous": "unwell", "reasons": []}))
    assert len(part.posts) == 2


# Refused ------------------------------------------------------------------------------------------------------------


def test_a_refused_feed_stops_until_discord_is_read_again(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON + "merge_seconds = 0\n")
    feeds.handle(kick())
    assert len(part.posts) == 1
    feeds.refused(f"channel:{LOGS}", "Missing Access (403)")
    feeds.handle(kick())
    assert len(part.posts) == 1  # never into a refusal: 10,000 of them blocks this PC's address
    assert feeds.blocked_lines() == ["Discord refused posts to a channel: Missing Access (403)"]
    feeds.unblock()
    feeds.handle(kick())
    assert len(part.posts) == 2


# Games and joins ----------------------------------------------------------------------------------------------------


def test_a_game_starts_and_ends_and_a_false_finished_is_nothing(tmp_path: Path) -> None:
    feeds, part, clock = feeds_for(tmp_path, "[feed.games]\nenabled = true\nmin_players = 1\n[messages]\ngames-started = { post = true }\ngames-ended = { post = true }\n")
    part.players = [object(), object()]
    feeds.handle(Event("rcon.phase", "Alpha", {"phase": "finished", "map": "Next", "mode": "Slayer"}))
    assert part.posts == []  # the next game's false `finished`
    feeds.handle(Event("rcon.phase", "Alpha", {"phase": "playing", "map": "Guardian", "mode": "Slayer"}))
    clock.now += 600
    feeds.handle(Event("rcon.phase", "Alpha", {"phase": "finished", "map": "Guardian", "mode": "Slayer"}))
    assert [p["channel"] for p in part.posts] == [PUBLIC_CHANNEL, PUBLIC_CHANNEL]
    assert "Guardian · Slayer · 2 playing" in part.posts[0]["embeds"][0]["description"]
    assert "over after 10 minutes" in part.posts[1]["embeds"][0]["description"]


def test_a_game_with_too_few_players_isnt_posted(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.games]\nenabled = true\nmin_players = 2\n")
    part.players = [object()]
    feeds.handle(Event("rcon.phase", "Alpha", {"phase": "playing", "map": "Guardian", "mode": "Slayer"}))
    assert part.posts == []


def _joined(player: str = "b" * 64, name: str = "Bravo") -> Event:
    return Event("rcon.join", "Alpha", {"id": player, "name": name, "ip": "203.0.113.9", "number": 1})


def test_only_first_joins_post_unless_asked(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.joins]\nenabled = true\nmerge_seconds = 0\n")  # first-time only, by default
    part.visits["b" * 64] = 1.0
    feeds.handle(_joined())
    assert part.posts == []  # seen before
    feeds.handle(_joined("c" * 64, "Charlie"))
    assert "**Charlie** joined for the first time" in part.posts[0]["content"]
    assert "203.0.113.9" not in str(part.posts)


def test_a_new_name_on_a_known_player(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.joins]\nenabled = true\n")
    part.visits["b" * 64] = 1.0
    part.names["b" * 64] = "OldName"
    feeds.handle(_joined())
    assert "**Bravo** was last seen as OldName" in part.posts[0]["content"]


def test_a_regular_back_after_a_long_time(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.joins]\nenabled = true\nreturning_days = 30\n[messages]\njoins-back = { post = true }\n")
    event = _joined()
    part.visits["b" * 64] = event.time - 31 * 86400
    feeds.handle(event)
    assert "is back after a long time" in part.posts[0]["content"]
    part.posts.clear()
    feeds.bursts.clear()
    part.visits["b" * 64] = event.time - 86400
    feeds.handle(event)
    assert part.posts == []


def test_refused_joins_and_leaves(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, "[feed.joins]\nenabled = true\nmerge_seconds = 0\n[messages]\njoins-left = { post = true }\n")
    feeds.handle(Event("rcon.refused", "Alpha", {"id": "b" * 64, "name": "Bravo", "ip": "203.0.113.9", "reason": "banned"}))
    feeds.handle(Event("rcon.leave", "Alpha", {"id": "b" * 64, "name": "Bravo", "number": 1}))
    said = [p["content"] for p in part.posts]
    assert said == ["Alpha · **Bravo** was turned away: banned", "Alpha · **Bravo** left"]


# The real part, on the fake Discord ---------------------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDiscord]:
    discord = FakeDiscord()
    discord.start()
    use_fake_discord(discord, monkeypatch)
    yield discord
    discord.stop()


def bot_file(root: Path) -> Path:
    path = root / "discord.toml"
    make_file(path)
    set_token(path, TOKEN)
    set_mode(path, "bot")
    set_guild(path, GUILD, "Test Discord")
    add_owner(path, OWNER_USER, "the host")
    set_channel(path, "staff", STAFF_CHANNEL, "admin-log")
    set_channel(path, "logs", STAFF_CHANNEL, "admin-log")
    set_channel(path, "alerts", STAFF_CHANNEL, "admin-log")
    set_switch(path, ("feed", "moderation"), "enabled", True)
    set_switch(path, ("feed", "alerts"), "enabled", True)
    return path


def part_of(huragok: Any) -> Any:
    return huragok.runners["discord"].part


def test_every_source_of_moderation_reaches_the_channel_once(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        bot_file(tmp_path)
        async with running(tmp_path) as (huragok, rcon, _):
            part = part_of(huragok)
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands", reason="spam")
            await wait_for(lambda: len(fake.posted) == 1, timeout=15)
            card = fake.posted[0]
            assert card["username"] == "Vergil" and card["via"] == "webhook" and card["channel"] == STAFF_CHANNEL
            case = huragok.storage.actions_for(ALICE)[0]["number"] if "number" in huragok.storage.actions_for(ALICE)[0].keys() else None
            assert "case" in card["embeds"][0]["footer"]["text"]
            assert case is None or str(case) in card["embeds"][0]["footer"]["text"]
            await rcon.event("ban", id="b" * 64, name="Bravo", by="Admin", reason="cheating", expires=0)  # someone's own /ban in game
            await wait_for(lambda: len(fake.posted) == 2, timeout=15)
            assert "Admin banned **Bravo** (cheating)" in fake.posted[1]["embeds"][0]["description"]
            assert fake.unversioned == 0

    run(scenario())


def test_a_refused_channel_stops_posting_and_comes_back_with_the_file(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            # Past the reads of the Discord a start makes: one ending now would lift the block (worth one more try).
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            fake.refuse_next.append((f"/channels/{STAFF_CHANNEL}/", 403, {"message": "Missing Access", "code": 50001}, {}))
            fake.refuse_next.append((f"/channels/{STAFF_CHANNEL}/", 403, {"message": "Missing Access", "code": 50001}, {}))
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands")
            await wait_for(lambda: f"channel:{STAFF_CHANNEL}" in part.feeds.blocked, timeout=15)
            await wait_for(lambda: any("refused posts to #admin-log" in line for line in huragok.problems), timeout=15)
            posting = lambda: [r for r in fake.requests if r[0] == "POST"]  # noqa: E731 - reads of the Discord go on by themselves
            asked = len(posting())
            part.feeds.bursts.clear()
            await huragok.vergil.moderation.act("kick", SERVER, ALICE, by="Mod", source="commands")
            import asyncio

            await asyncio.sleep(0.5)
            assert len(posting()) == asked  # not asked again
            set_switch(path, ("feed", "votes"), "enabled", True)  # the file changes: worth another try
            await wait_for(lambda: f"channel:{STAFF_CHANNEL}" not in part.feeds.blocked, timeout=15)

    run(scenario())


def test_the_window_only_shows_what_is_built(tmp_path: Path) -> None:
    built = [feature.key for feature in features.FEATURES if feature.stage <= features.BUILT]
    assert "feed.moderation" in built and "status.board" in built and "feed.kills" not in built


def test_setup_is_on_the_audit_feed(tmp_path: Path, run: Any, fake: FakeDiscord) -> None:
    async def scenario() -> None:
        path = bot_file(tmp_path)
        set_switch(path, ("feed", "audit"), "enabled", True)
        set_channel(path, "logs", STAFF_CHANNEL, "admin-log")
        async with running(tmp_path) as (huragok, _, _):
            part = part_of(huragok)
            await wait_for(lambda: part.state == "online" and part.facts is not None and part.looking == 0, timeout=20)
            set_switch(path, ("feed", "votes"), "enabled", True)  # by hand, or in the window
            await wait_for(lambda: any("The bot's settings were changed" in str(p.get("content")) for p in fake.posted), timeout=15)

    run(scenario())


def test_facts_are_read_for_the_channel(fake: FakeDiscord) -> None:
    assert rest.bot_user(TOKEN)["username"] and gather(TOKEN, GUILD).guild is not None


def test_a_mod_update_names_the_mod_and_its_versions(tmp_path: Path) -> None:
    feeds, part, _ = feeds_for(tmp_path, ON)
    config = part.file
    card = cards.workshop_updated(config, Event("server.workshop_updated", "Alpha", {
        "items": [3820687243], "names": ["HugeGrass Rebalance 16 Oct 2026 -> 18 Oct 2026"]}))
    assert card.line == "Mod update installed: HugeGrass Rebalance 16 Oct 2026 → 18 Oct 2026"
    older = cards.workshop_updated(config, Event("server.workshop_updated", "Alpha", {"items": [3820687243]}))
    assert older.line == "Mod update installed: 3820687243"  # an event without names (a version before)
    waiting = cards.workshop_waiting(config, Event("server.workshop_update", "Alpha", {
        "item": 3820687243, "title": "HugeGrass Rebalance"}))
    assert waiting.line == ("A new version of HugeGrass Rebalance (Workshop item 3820687243) is out, and waits for "
                            "the next update")
    untitled = cards.workshop_waiting(config, Event("server.workshop_update", "Alpha", {"item": 3820687243}))
    assert untitled.line.startswith("A new version of Workshop item 3820687243 is out")
