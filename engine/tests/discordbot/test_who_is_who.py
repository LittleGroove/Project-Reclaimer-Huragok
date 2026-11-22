"""Who's who in Discord (plans/player-identity-audit, the host's calls of 21 November 2026): every player the bot names
is written one way, their tag (core/tags.py), with the start of their ID in a code box and the server they're on;
staff by their name here, with their player where they're linked; every pick says who it came to, and a name two
players share is never settled by guessing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from conftest import ALICE, BRAVO

from huragok.core.events import Event
from huragok.discordbot import cards
from huragok.discordbot.appeals import Appeals
from huragok.discordbot.config import load
from huragok.discordbot.feeds import Feeds
from huragok.discordbot.part import DiscordPart
from huragok.players.naming import Names
from huragok.servers.state import Player, ServerState

OTHER = "1a2b3c4d" + "e" * 56  # another "Bravo"


def _config(tmp_path: Path) -> Any:
    path = tmp_path / "discord.toml"
    path.write_text('mode = "bot"\n', encoding="utf-8")
    return load(path)


class _History:
    """The players seen, as PlayerHistory answers: by ID, and by a name gone by."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names

    def get(self, player_id: str) -> Any:
        name = self.names.get(player_id.lower())
        return SimpleNamespace(id=player_id.lower(), last_name=name) if name is not None else None

    def named(self, text: str, limit: int = 25) -> list[tuple[str, str]]:
        return [(player_id, name) for player_id, name in self.names.items() if name.casefold() == text.strip().casefold()][:limit]

    def find(self, text: str, limit: int = 10) -> list[Any]:
        return [SimpleNamespace(id=player_id, last_name=name) for player_id, name in self.names.items()
                if player_id.startswith(text.lower()) or name.casefold() == text.casefold()][:limit]


def _part(tmp_path: Path, *, linked: dict[str, Any] | None = None) -> Any:
    """The real part's naming, on two servers: Bravo on Alpha as #4; another Bravo seen, on none now."""
    alpha = ServerState("Alpha")
    alpha.players[4] = Player(4, "Bravo", BRAVO)
    history = _History({BRAVO: "Bravo", OTHER: "Bravo", ALICE: "Alice"})
    part = DiscordPart.__new__(DiscordPart)
    part.ctx = SimpleNamespace(names=Names({"Alpha": alpha}, history), players=history, servers={"Alpha": alpha})
    part.file = _config(tmp_path)
    part.real_names = {}
    part.links = SimpleNamespace(of_member=lambda member: (linked or {}).get(member))
    return part


def test_a_card_says_who_the_one_way(tmp_path: Path) -> None:
    config = _config(tmp_path)
    event = Event("moderation.action", "Alpha", {"kind": "kick", "target_id": BRAVO, "target_name": "Bravo", "on_server": "Alpha", "picked_as": "#4",
                                                 "by": "Mod", "reason": "spam", "ok": True, "case": 7})
    card = cards.moderation(config, event)
    assert card is not None and card.line == "Mod kicked **Bravo** `dddddddd` [Alpha] (spam) · picked as #4"
    # Nobody to name: no name and no ID is "someone", never an address cut short
    assert cards.player(Event("moderation.action", "Alpha", {"target_id": "203.0.113.9", "target_name": ""})) == "someone"
    assert cards.player(Event("moderation.action", "Alpha", {"target_id": BRAVO, "target_name": ""})) == "(no name yet) `dddddddd`"


def test_what_was_written_for_discord_is_cleaned_once(tmp_path: Path) -> None:
    """Cleaned twice, a name with a _ in it showed a backslash in the audit feed (staff.py, console.py, slash.py)."""
    config = _config(tmp_path)
    written = cards.Shown(f"{cards.clean('Cool_Guy')}: note")
    assert cards.clean(written) is written
    assert cards.audit("notes", written, config).line == "Cool\\_Guy: note"
    assert cards.audit("notes", "Cool_Guy: note", config).line == "Cool\\_Guy: note"


def test_the_part_writes_players_the_one_way(tmp_path: Path) -> None:
    part = _part(tmp_path)
    assert DiscordPart.tag(part, BRAVO) == "Bravo `dddddddd` [Alpha]"  # where they are now
    assert DiscordPart.tag(part, OTHER, bold=True) == "**Bravo** `1a2b3c4d`"  # on no server: none said
    assert DiscordPart.label(part, BRAVO, handle="#4") == "Bravo dddddddd (#4) [Alpha]"  # plain, for a list or a menu
    assert DiscordPart.named_in(part, f"Unbanned {OTHER}.") == "Unbanned Bravo 1a2b3c4d."
    assert DiscordPart.named_in(part, f"Unbanned {'f' * 64}.") == f"Unbanned {'f' * 64}."  # never seen: as it was


def test_a_forms_title_fits_and_keeps_the_id(tmp_path: Path) -> None:
    part = _part(tmp_path)
    assert DiscordPart.titled(part, "Mute", BRAVO) == "Mute: Bravo dddddddd"
    long = DiscordPart.titled(part, "Mute", OTHER, "Nate Higgers The Very Long Name Indeed")
    assert len(long) <= 45 and long.endswith(" 1a2b3c4d")  # the name is cut short, never the ID


def test_staff_are_named_one_way(tmp_path: Path) -> None:
    """Their name here, and their player's tag where they're linked (H5); the tag alone when it's the same name."""
    linked = {"55": {"player_id": ALICE, "player_name": "Alice", "member_name": "Alice"}, "66": {"player_id": BRAVO, "player_name": "Bravo", "member_name": "Buck"}}
    part = _part(tmp_path, linked=linked)
    part.real_names = {"discord:66": "Buck"}
    assert DiscordPart.actor(part, "discord:77", "Mod") == "Mod"  # not linked: their name
    assert DiscordPart.actor(part, "discord:55", "a moderator") == "Alice `cccccccc`"  # linked, same name: the tag
    assert DiscordPart.actor(part, "discord:66", "a moderator") == "Buck (Bravo `dddddddd` [Alpha])"
    assert DiscordPart.actor(part, BRAVO, "Bravo") == "Bravo `dddddddd` [Alpha]"  # someone in game: their tag
    assert DiscordPart.actor(part, "", "Vergil") == "Vergil"


def test_an_appeal_by_a_shared_name_isnt_guessed(tmp_path: Path) -> None:
    """Two players have gone by Bravo: /appeal finds both, and asks, as the first with something to appeal was taken."""
    part = _part(tmp_path)
    appeals = Appeals.__new__(Appeals)
    appeals.part = part
    appeals.find_code = lambda typed: ("", "")  # type: ignore[method-assign]
    assert sorted(appeals.find("bravo")) == sorted([(BRAVO, "Bravo"), (OTHER, "Bravo")])
    assert appeals.find(OTHER[:8]) == [(OTHER, "Bravo")]  # the start of their ID, which every tag shows
    assert appeals.find(ALICE) == [(ALICE, "Alice")]


def test_a_card_is_about_where_they_were(tmp_path: Path) -> None:
    """The feeds fill in where whoever it's about was: what the event says; the server it was heard on; else where
    they are now. And whoever did it, the one way (part.actor)."""
    part = SimpleNamespace(file=_config(tmp_path), name_of=lambda player_id, given="": given, on_server=lambda player_id: "Beta" if player_id == BRAVO else "",
                           actor=lambda by_id, by="": cards.Shown(f"**{by}**"))
    feeds = Feeds(part)  # type: ignore[arg-type]
    heard = feeds.shown(Event("automod.heard", "Alpha", {"player_id": BRAVO, "player_name": "Bravo"}))
    assert heard["on_server"] == "Alpha"
    pardoned = feeds.shown(Event("automod.pardon", "Alpha", {"player_id": BRAVO, "player_name": "Bravo", "by": "Mod", "by_id": "discord:1"}))
    assert pardoned["on_server"] == "Beta" and pardoned["by_shown"] == "**Mod**"
    kicked = feeds.shown(Event("moderation.action", "Alpha", {"target_id": BRAVO, "target_name": "Bravo", "on_server": ""}))
    assert kicked["on_server"] == ""  # they were on none as it was done: not where they've gone since
