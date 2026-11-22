"""Players as the window writes and finds them (window/whoswho.py): one tag each, and a #number, name or ID typed at the
console (A), at bans and admins (B) or for a role found the way the game's own commands find one."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from testkit.window import FakeController, FakeKeyboard, FakeTerminal, row

from huragok.core.logs import RecentLog
from huragok.core.requests import Result
from huragok.core.storage import Storage, last_names, seen_players
from huragok.window.dashboard import Menu, render
from huragok.window.tui import event_colour
from huragok.window.whoswho import Known, console_line, find, is_address, on_server

NATE, NATE_H, PLAYER1, PLAYER2, BRAVO, GONE = ("1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, "6" * 64)


def rows() -> list[dict[str, Any]]:
    """Alpha with Nate, Nate Higgers and two Players on; Beta with Bravo."""
    alpha = [{"number": 3, "name": "Nate", "id": NATE, "team": "red"}, {"number": 4, "name": "Nate Higgers", "id": NATE_H},
             {"number": 5, "name": "Player", "id": PLAYER1}, {"number": 6, "name": "Player", "id": PLAYER2, "muted": True}]
    return [row("Alpha", online=alpha, names="Nate, Nate Higgers, Player, Player"),
            row("Beta", 2, online=[{"number": 1, "name": "Bravo", "id": BRAVO}], names="Bravo")]


def records(tmp_path: Path) -> Path:
    """Huragok's records: everyone above, and Gone (seen, not on), who went by Bravo once too."""
    path = tmp_path / "records" / "huragok.sqlite3"
    storage = Storage(path)
    for when, (player_id, name) in enumerate(((NATE, "Nate"), (NATE_H, "Nate Higgers"), (PLAYER1, "Player"),
                                              (PLAYER2, "Player"), (BRAVO, "Bravo"), (GONE, "Bravo"), (GONE, "Gone Guy"))):
        storage.record_join(player_id, name, None, 1_700_000_000 + when)
    storage.close()
    return path


def test_the_records_read_read_only(tmp_path: Path) -> None:
    path = records(tmp_path)
    assert last_names(path, [GONE, BRAVO.upper(), "f" * 64]) == {GONE: "Gone Guy", BRAVO: "Bravo"}
    assert {r.id for r in seen_players(path, "bravo")} == {BRAVO, GONE}
    assert [r.names for r in seen_players(path, GONE[:8])] == [["Gone Guy", "Bravo"]]
    assert seen_players(path, "6666") == [] and last_names(tmp_path / "none.sqlite3", [GONE]) == {}  # 8 at least


def test_found_the_way_the_game_finds_a_player(tmp_path: Path) -> None:
    known = Known(rows, records(tmp_path))
    assert find(known, "Alpha", "#4").one.id == NATE_H and find(known, "Alpha", "4").one.id == NATE_H
    assert find(known, "Alpha", "#9").why == "Nobody is #9 on Alpha now."
    assert find(known, "Alpha", "nate higgers").one.id == NATE_H and find(known, "Alpha", '"Nate"').one.id == NATE
    assert find(known, "Alpha", NATE_H[:8]).one.id == NATE_H  # an ID's first 8 characters
    assert find(known, "Alpha", NATE_H.upper()).one.id == NATE_H
    # Two with one name: a list to pick from, never one of them.
    shared = find(known, "Alpha", "player")
    assert shared.one is None and [f.id for f in shared.found] == [PLAYER1, PLAYER2]
    assert shared.head == "'player' could be 2 players:"
    assert [f.describe("Alpha") for f in shared.found] == [f"Player 33333333 (#5) [Alpha]",
                                                           f"Player 44444444 (#6) [Alpha]: muted"]
    # Bravo is on Beta, and someone who's gone went by it too: both, for a command that reaches anyone seen.
    bravo = find(known, "Alpha", "Bravo")
    assert [f.tag("Alpha") for f in bravo.found] == ["Bravo 55555555 [Beta]", "Bravo 66666666"]
    assert find(known, "Alpha", "Bravo", scope="here").why == ("Nobody called 'Bravo' is on Alpha now: Bravo 55555555 "
                                                              "[Beta]; Bravo 66666666.")
    assert find(known, "Alpha", "Gone Guy").one.id == GONE
    # A name that only looks like someone's (a Greek Β) is offered, never acted on.
    alike = find(known, "Beta", "ΒRAVO")
    assert alike.one is None and [f.id for f in alike.found] == [BRAVO] and "looks" in alike.head
    assert find(known, "Alpha", "Nat").head.startswith("Nobody on Alpha is called 'Nat'. Did you mean")
    assert find(known, "Alpha", "Zed").unknown and find(known, "Alpha", "203.0.113.4").as_typed
    assert find(known, None, "#3").why.startswith("A #number is a server's own")
    assert is_address("203.0.113.0/24") and is_address("2001:db8::1") and not is_address("Bravo")
    # A row that doesn't list its players apart (an older live.json): the server finds them by its own rules.
    assert find(Known(lambda: [row("Alpha")], None), "Alpha", "Player").as_typed


def test_a_console_line_and_the_player_in_it() -> None:
    names = {"nate higgers"}
    line = console_line("kick Nate Higgers  spam it", lambda name: name.casefold() in names)
    assert line is not None and (line.head, line.ref, line.rest) == ("kick ", "Nate Higgers", "  spam it")
    quoted = console_line('mute "Nate Higgers" 30m', lambda name: False)
    assert quoted is not None and (quoted.head, quoted.ref, quoted.rest) == ("mute ", '"Nate Higgers"', " 30m")
    admin = console_line("admin add Bravo", lambda name: False)
    assert admin is not None and (admin.head, admin.ref, admin.sends) == ("admin add ", "Bravo", "number")
    assert console_line("players", lambda name: False) is None and console_line("admin list", lambda n: False) is None
    assert console_line("kick", lambda name: False) is None and console_line("say kick Bravo", lambda n: False) is None


class Console(FakeController):
    """Alpha and Beta with players on (rows()), the records beside them."""

    def __init__(self, database: Path) -> None:
        super().__init__()
        self._rows = [{**r, "can": {"start": False, "stop": True, "restart": True, "cancel": False, "talk": True}}
                      for r in rows()]
        self.database = database

    def refresh_settings(self) -> Any:
        return type("Settings", (), {"database": self.database})()

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        return [(server, Result(True, "ok", "done"))]


def test_the_console_finds_the_player_before_it_sends(tmp_path: Path, run, caplog) -> None:
    controller = Console(records(tmp_path))
    keys = ["A", "1", "ENTER", *"Alice", "ENTER",
            *"kick player camping", "ENTER", "2", "ENTER",  # two Players: the second, #6
            *"mute Nate Higgers 30m", "ENTER",  # a name of two words, unquoted
            *"kick nate higgins spam", "ENTER",  # Nate, or Nate Higgers mistyped: neither
            *"ban Bravo 7d cheating", "ENTER", "1", "ENTER",  # on Beta, or gone: Beta's, by their player ID
            *"pardon Ghost", "ENTER",  # a name only the server's list may have: as typed
            *"ban 203.0.113.4 7d spam", "ENTER",  # an address: as typed
            *"kick Bravo", "ENTER",  # on Beta, not here: not sent
            "ENTER", "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, FakeKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO):
        assert run(menu.run()) == "quit"
    sent = [options["text"] for verb, _, options in controller.calls if verb == "rcon"]
    assert sent == ["kick #6 camping", "mute #4 30m", f"ban {BRAVO} 7d cheating", "pardon Ghost",
                    "ban 203.0.113.4 7d spam"]
    printed = "".join(terminal.output)
    assert "'player' could be 2 players:" in printed and "Player 44444444 (#6) [Alpha]: muted" in printed
    assert "kick #6 camping  →  Player 44444444 (#6) [Alpha]" in printed
    assert "'nate higgins…' could be someone else" in printed and "Nate Higgers 22222222 (#4) [Alpha]" in printed
    assert "Nobody called 'Ghost' is on a server or in Huragok's records: sent as typed" in printed
    assert "Nobody called 'Bravo' is on Alpha now: Bravo 55555555 [Beta]; Bravo 66666666." in printed
    assert f"the window's console line ban {BRAVO} 7d cheating → Bravo {BRAVO} [Beta]" in caplog.text  # the whole ID


def test_bans_find_the_player_too(tmp_path: Path, run) -> None:
    from testkit.manage import ManageController, manage_root

    settings = manage_root(tmp_path)  # Alpha, a real one: B reads its settings
    settings.write_text(settings.read_text(encoding="utf-8").replace(
        'database = ""', f'database = "{records(tmp_path).as_posix()}"'), encoding="utf-8")

    class Bans(ManageController):
        def rows(self) -> list[dict[str, Any]]:
            return [{**r, "can": {"start": False, "stop": True, "restart": True, "cancel": False, "talk": True}}
                    for r in rows()]

    controller = Bans(settings)
    keys = ["B", "1", "ENTER", *"Alice", "ENTER",
            "U", *"Gone Guy", "ENTER",  # seen, not on: by player ID
            "+", *"nate", "ENTER",  # Nate is #3 here
            "-", *GONE[:8], "ENTER",  # an ID's first 8 characters
            "ESC", "Q", "Y"]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, FakeKeyboard(keys), RecentLog())  # type: ignore[arg-type]
    assert run(menu.run()) == "quit"
    sent = [options["text"] for verb, _, options in controller.calls if verb == "rcon"]
    assert sent == [f"unban {GONE}", "admin add #3", f"admin remove {GONE}"]
    assert "admin add #3  →  Nate 11111111 (#3) [Alpha]" in "".join(terminal.output)


def test_the_p_list_shows_each_player_as_their_tag(run) -> None:
    controller = FakeController()
    controller._rows = [row("Alpha", names="Bob, Jr, Cy", online=[{"number": 2, "name": "Bob, Jr", "id": NATE,
                                                                   "team": "Red", "admin": True},
                                                                  {"number": 7, "name": "Cy", "id": PLAYER1}])]
    terminal = FakeTerminal()
    menu = Menu(controller, terminal, FakeKeyboard(["x"]))  # type: ignore[arg-type]
    run(menu.screen_players())
    printed = "".join(terminal.output)
    assert "Bob, Jr 11111111 (#2) [Alpha]" in printed and "red, admin" in printed  # one player, comma and all
    assert "Cy 33333333 (#7) [Alpha]" in printed and "[ 1]" not in printed  # the #number, not a list number
    old = FakeController()  # a live.json from before 0.7.0: names alone
    menu = Menu(old, FakeTerminal(), FakeKeyboard(["x"]))  # type: ignore[arg-type]
    run(menu.screen_players())
    assert on_server(old.rows()[0]) is None


def test_lines_are_coloured_by_huragoks_words_not_a_players_name() -> None:
    assert event_colour(logging.INFO, "[Alpha] FAILED 1a2b3c4d joined") == "yellow"  # the name, read as a word
    assert event_colour(logging.INFO, "[Alpha] FAILED 1a2b3c4d joined", ["FAILED"]) == "cyan"
    assert event_colour(logging.INFO, "[Alpha] something failed for FAILED", ["FAILED"]) == "yellow"
    view = {"rows": [row("Alpha", online=[{"number": 1, "name": "Crashed", "id": NATE}])],
            "events": [(logging.INFO, "12:00:00 [Alpha] Crashed 11111111 (#1) [Alpha] joined")]}
    colours = dict(render(view, 100, 40))
    assert colours[next(t for t in colours if "Crashed 11111111" in t)] == "cyan"


def test_discord_faculty_are_listed_with_the_name_noted_beside_them(tmp_path: Path, run, monkeypatch) -> None:
    """The bot notes a username beside whoever claims it; J lists it, and notes one when the window adds someone."""
    from testkit.screens import screen, template_root, text_of

    from huragok.discordbot import config as discord, rest
    from huragok.window.screens import discordsetup

    settings = template_root(tmp_path)
    path = settings.parent / "discord.toml"
    discord.make_file(path)
    discord.add_faculty(path, "111", "alice")
    menu, terminal = screen(settings, ["ENTER"])
    run(menu._discord_faculty(path))
    assert "alice" in text_of(terminal) and "111" in text_of(terminal)
    monkeypatch.setattr(rest, "guild_member", lambda token, guild, user: {"user": {"username": "bob\x1b[2J"}})
    known = type("Config", (), {"token": "t", "guild": "g"})()
    menu, terminal = screen(settings, [*"222", "ENTER"])
    run(menu._discord_faculty(path, known))  # type: ignore[arg-type]
    assert "bob [2J (222) is faculty now." in text_of(terminal)
    assert discordsetup._list_notes(path) == {"111": "alice", "222": "bob [2J"}
    assert discord.load(path).section("ranks")["faculty"] == ["111", "222"]
