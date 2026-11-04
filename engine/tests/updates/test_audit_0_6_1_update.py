"""The audit before 0.6.1 (plans/bug-audit-0.6.1.md): the faculty rename and the update from 0.6.0 (area 6)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from testkit.hostfiles import host, up_to_date

from huragok.core.commandsfile import add_player, load_permissions, player_notes, remove_player
from huragok.core.settings import load
from huragok.discordbot.config import remove_faculty

OWNER, CLAIMED = "a" * 64, "b" * 64
WRITTEN_BY_A_CLAIM = '''[ranks]
owners = [
    "111",  # Robbie
    "222",  # Sam
]
faculty = []
owners_in_game = "owner"
'''


def test_the_faculty_lists_notes_stay_and_one_section_is_left(tmp_path: Path) -> None:
    """6.1: the faculty list was written on one line, and each claimed ID's Discord name went with it. 6.3: a [faculty]
    written before the step (a claim, the window) left [owner] there for good."""
    settings = host(tmp_path)
    commands = settings.parent / "commands.toml"
    commands.write_text(commands.read_text(encoding="utf-8").replace(
        "[owner]\nplayers = [\n", f'[owner]\nplayers = [\n    "{OWNER}",  # the host\n'), encoding="utf-8")
    add_player(commands, "faculty", CLAIMED, "Buck: faculty, as they claimed Huragok's Discord bot")  # before the step: [faculty] made
    assert "[owner]" in commands.read_text(encoding="utf-8") and "\n[faculty]" in commands.read_text(encoding="utf-8")
    discord = settings.parent / "discord.toml"
    discord.write_text('mode = "off"\n' + WRITTEN_BY_A_CLAIM, encoding="utf-8")
    up_to_date(settings)
    text = commands.read_text(encoding="utf-8")
    assert "[owner]" not in text and text.count("[faculty]") == 1
    assert load_permissions(commands).members["faculty"] == {OWNER, CLAIMED}
    assert player_notes(commands)[OWNER] == "the host" and "claimed" in player_notes(commands)[CLAIMED]
    written = discord.read_text(encoding="utf-8")
    assert '"111",  # Robbie' in written and '"222",  # Sam' in written and "owners" not in written
    assert tomllib.loads(written)["ranks"]["faculty_in_game"] == "faculty"


def test_before_the_step_the_old_names_are_written_to_too(tmp_path: Path) -> None:
    """6.3, 6.4: before the step, the window said someone wasn't faculty any more while [owner] or owners still
    listed them."""
    settings = host(tmp_path)
    commands = settings.parent / "commands.toml"
    commands.write_text(commands.read_text(encoding="utf-8").replace(
        "[owner]\nplayers = [\n", f'[owner]\nplayers = [\n    "{OWNER}",  # the host\n'), encoding="utf-8")
    assert remove_player(commands, "faculty", OWNER) and OWNER not in load_permissions(commands).members["faculty"]
    discord = tmp_path / "discord.toml"
    discord.write_text(WRITTEN_BY_A_CLAIM, encoding="utf-8")
    assert remove_faculty(discord, "111") and "111" not in discord.read_text(encoding="utf-8")


def test_owner_however_written_is_faculty(tmp_path: Path) -> None:
    """6.6: "Owner", 'owner' or an exempt_roles over several lines stayed, and the window refused that relay user."""
    settings = host(tmp_path)
    text = settings.read_text(encoding="utf-8")
    start = text.index("exempt_roles")
    end = text.index("\n", start)
    text = text[:start] + 'exempt_roles = [\n    "admin",\n    \'Owner\',\n]' + text[end:]
    settings.write_text(text + '\n[[relay.user]]\nname = "Alice"\npassword = "a long password"\nrole = "Owner"  # runs the place\n',
                        encoding="utf-8")
    up_to_date(settings)
    written = settings.read_text(encoding="utf-8")
    assert "Owner" not in written and 'role = "faculty"  # runs the place' in written
    assert load(settings).parts["automod"].options["exempt_roles"] == ["admin", "faculty"]


def test_a_beta_hosts_own_values_come_back_once_built(tmp_path: Path) -> None:
    """6.2: what 0.6.0 kept as notes ("Not used yet: … comes back with the version that builds it") stayed notes, or
    was written over with the default."""
    settings = host(tmp_path)
    discord = settings.parent / "discord.toml"
    discord.write_text('mode = "off"\n[channels]\n# Not used yet: this channel comes back with the version that builds what it\'s for:\n'
                       '# stats = "#leaderboards"\n\n# Not used yet: [link] comes back with the version that builds it:\n'
                       '# code_minutes = 5\n# Not used yet: [chat] comes back with the version that builds it:\n# relay = true\n',
                       encoding="utf-8")
    said = up_to_date(settings)
    data = tomllib.loads(discord.read_text(encoding="utf-8"))
    assert data["channels"]["stats"] == "#leaderboards" and data["link"]["code_minutes"] == 5
    assert "# relay = true" in discord.read_text(encoding="utf-8")  # still not built: still a note
    assert any("[channels] stats back as you set it" in line for line in said)


def test_a_betas_values_that_mean_something_else_now_stay_notes(tmp_path: Path) -> None:
    """Second audit 9.1: a first beta's [roles] sync made a Discord role a rank; brought back, it took rank roles off
    everyone unlinked. Its boards, named as 0.6.1 can't read, showed nothing. 9.10: a 0.6.1-dev line said nothing about."""
    from huragok.discordbot.config import load as load_discord

    settings = host(tmp_path)
    discord = settings.parent / "discord.toml"
    discord.write_text('mode = "off"\n# Not used yet: [roles] comes back with the version that builds it:\n# sync = true\n'
                       '# Not used yet: [leaderboards] comes back with the version that builds it:\n# boards = ["season-kills"]\n'
                       '[messages]\ngames-medal = { channel = "games" }\n', encoding="utf-8")
    up_to_date(settings)
    data = tomllib.loads(discord.read_text(encoding="utf-8"))
    assert data["roles"]["sync"] is False and "# sync = true" in discord.read_text(encoding="utf-8")
    assert "season-kills" not in str(data["leaderboards"]["boards"])
    assert not any("games-medal" in line for line in load_discord(discord).warnings)
