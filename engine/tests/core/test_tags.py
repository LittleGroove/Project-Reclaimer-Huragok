"""One way to write a player (core/tags.py): name, the start of their ID, the handle, the server they're on. The host's
call, 21 November 2026 (plans/player-identity-audit)."""

from __future__ import annotations

from huragok.core.tags import NO_NAME, named_then, short_id, shown_name, tag

BRAVO = "1a2b3c4d" + "0" * 56


def test_a_tag_reads_name_id_handle_server() -> None:
    assert tag("Nate Higgers", BRAVO, handle="#4", server="HugeGrass") == "Nate Higgers 1a2b3c4d (#4) [HugeGrass]"


def test_what_isnt_known_is_left_out() -> None:
    assert tag("Bravo", BRAVO) == "Bravo 1a2b3c4d"
    assert tag("Bravo", BRAVO, handle="@2") == "Bravo 1a2b3c4d (@2)"
    assert tag("Bravo", BRAVO, server="Alpha") == "Bravo 1a2b3c4d [Alpha]"


def test_the_log_has_the_whole_id_and_discord_a_code_box() -> None:
    assert tag("Bravo", BRAVO, whole=True) == f"Bravo {BRAVO}"
    assert tag("Bravo", BRAVO, server="Alpha", code=True) == "Bravo `1a2b3c4d` [Alpha]"


def test_an_id_typed_in_capitals_is_shown_as_the_servers_send_them() -> None:
    assert tag("Bravo", BRAVO.upper()) == "Bravo 1a2b3c4d"


def test_someone_with_no_name_yet_and_something_that_isnt_a_player() -> None:
    assert tag("", BRAVO, handle="#3") == f"{NO_NAME} 1a2b3c4d (#3)"  # 0.10: the name comes a few seconds after the join
    assert tag("1.2.3.4", "") == "1.2.3.4"  # an address ban: nothing to add
    assert tag("", "") == "someone"
    assert tag("Bravo", "discord:123") == "Bravo"  # a Discord user acting is no player ID


def test_a_name_stays_on_one_line() -> None:
    assert shown_name("Bad\nName\r\x1b[31m") == "Bad Name [31m"
    assert tag("Two\tWords", BRAVO) == "Two Words 1a2b3c4d"


def test_read_from_the_right_a_name_with_brackets_still_parses() -> None:
    shown = tag("[SPR] Bob (#1)", BRAVO, handle="#4", server="Alpha")
    assert shown.endswith(" 1a2b3c4d (#4) [Alpha]") and shown.removesuffix(" 1a2b3c4d (#4) [Alpha]") == "[SPR] Bob (#1)"


def test_short_id_and_the_name_something_was_done_under() -> None:
    assert short_id(BRAVO.upper()) == "1a2b3c4d"
    assert named_then("Alpha", "Bravo") == "Alpha"
    assert named_then("bravo", "Bravo") == ""
    assert named_then("", "Bravo") == ""
