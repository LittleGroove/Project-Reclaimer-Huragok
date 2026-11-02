"""A setting with a wrong value says so, and nothing downstream ever sees it: a rank nobody has, or that everyone does, is worse than the default."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from huragok.discordbot.config import CHOICES, NAMES, defaults, load, make_file, set_message, set_switch
from huragok.discordbot.ranks import Someone, rank_of


def a_file(root: Path) -> Path:
    path = root / "discord.toml"
    make_file(path)
    return path


@pytest.mark.parametrize(("table", "key", "wrong"), [(("tickets",), "close_rank", "mods"), (("tickets",), "close_rank", "player"), (("commands",), "power_rank", "player"), (("commands",), "power_rank", "member"),
                                                     (("tickets",), "when_closed", "thread please"), (("console",), "layout", "picker2"), (("appeals",), "tell_outcome", "thread"), (("status", "board"), "layout", "grid"),
                                                     (("status", "presence"), "activity", "streaming"), (("commands",), "staff_permission", "Manage Messages"), (("commands",), "staff_permission", "all")])
def test_one_thing_out_of_a_few(tmp_path: Path, table: tuple[str, ...], key: str, wrong: str) -> None:
    path = a_file(tmp_path)
    set_switch(path, table, key, wrong)
    config = load(path)
    usual = defaults()
    for part in table:
        usual = usual[part]
    assert config.section(*table)[key] == usual[key]
    assert any(key in warning and wrong in warning for warning in config.warnings), config.warnings


def test_every_closed_settings_default_passes_its_own_check(tmp_path: Path) -> None:
    config = load(a_file(tmp_path))
    assert config.warnings == []
    made = defaults()
    for (*table, key), known in CHOICES.items():
        here = made
        for part in table:
            here = here[part]
        assert str(here[key]) in known, key
    for (*table, key), known in NAMES.items():
        here = made
        for part in table:
            here = here[part]
        assert all(str(name) in known for name in here[key]), key


def test_every_a_or_b_in_a_note_is_what_the_check_knows() -> None:
    """Each `a | b | c` written beside a checked setting: what the file says you can write is what it takes."""
    from huragok.discordbot.config import template_text

    lines = template_text().split("\n")
    table: tuple[str, ...] = ()
    checked = 0
    for line in lines:
        if line.startswith("["):
            table = tuple(line.strip("[] \r").split("."))
        key = line.partition("=")[0].strip()
        if (*table, key) in CHOICES and "#" in line and "|" in line.partition("#")[2]:
            words = {word.strip().split(" ")[0].strip(":") for word in line.partition("#")[2].split("|")}
            assert words <= set(CHOICES[(*table, key)]) | {"move:", "stay:", "thread:"} or words & set(CHOICES[(*table, key)]), (table, key, words)
            checked += 1
    assert checked >= 5


def test_seeding_numbers_cant_take_the_bot_down(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    set_switch(path, ("status", "seeding"), "seed_at", ["1", "3"])
    config = load(path)
    assert config.section("status", "seeding")["seed_at"] == [1, 3] and any("seed_at" in warning for warning in config.warnings)
    set_switch(path, ("status", "seeding"), "seed_at", [5, 1])
    assert any("lowest first" in warning for warning in load(path).warnings)


def test_a_guild_thats_a_name_is_said_not_crashed_on(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace('guild = ""', 'guild = "My Discord"', 1), encoding="utf-8")
    config = load(path)
    assert config.guild == "" and any("guild" in warning for warning in config.warnings)


def test_a_colour_that_isnt_says_so(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    set_switch(path, ("tickets",), "colours", {"closed": "not a colour"})
    assert any("not a colour" in warning for warning in load(path).warnings)


def test_a_mistyped_message_line_says_so(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    set_message(path, "moderation-bann", channel="logs")
    assert any("moderation-ban" in warning for warning in load(path).warnings)


def test_a_line_can_say_what_it_is(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    set_message(path, "reports-made", **{"as": "post"})
    assert not load(path).warnings
    set_message(path, "reports-made", **{"as": "card"})
    assert any("reports-made" in warning for warning in load(path).warnings)


def test_a_server_named_in_text_isnt_part_of_another_name(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    with path.open("a", encoding="utf-8") as file:
        file.write('\n[[ranks.scoped]]\nrole = "5050"\nrank = "admin"\nservers = "Server 10"\n')
    config = load(path)
    someone = Someone("1", {"5050": "x"})
    assert rank_of(config, someone, "Server 10") == "admin"
    assert rank_of(config, someone, "Server 1") != "admin"


def test_flags_taken_out_stay_out(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    set_switch(path, ("notes",), "flags", {"regular": "blue"})
    assert set(load(path).section("notes")["flags"]) == {"regular"}


def test_an_owners_rank_in_game_is_only_what_it_says(tmp_path: Path) -> None:
    path = a_file(tmp_path)
    set_switch(path, ("ranks",), "owners_in_game", "moderator")
    config = load(path)
    assert config.section("ranks")["owners_in_game"] == "owner" and any("owners_in_game" in warning for warning in config.warnings)


def test_every_template_line_is_valid_toml_after_all_that() -> None:
    from huragok.discordbot.config import template_text

    assert tomllib.loads(template_text())["console"]["clear_after_minutes"] == 10
