"""/stats and /leaderboard (0.6.1), for anyone in the Discord: what servers/leaderboards.py works out of every reported
game, as standings.py shows it. Answered privately, as /status is. Imported with client.py, in bot mode alone."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from ..servers import leaderboards
from ..servers.leaderboards import Board, words_of
from . import standings
from .cards import clean
from .client import ANOTHER_DISCORD, answer, elsewhere, part_of

log = logging.getLogger(__name__)

MOST_CHOICES = 25
MOST_IN_A_MESSAGE = 1900
PERIODS = [app_commands.Choice(name=words, value=key) for key, words in standings.PERIOD_WORDS.items()]


async def _show(part: Any, interaction: discord.Interaction, form: dict[str, Any]) -> None:
    """Privately, as the host's default is ([stats] private, the host's call, 3 November 2026), or for the channel."""
    if part.file.section("stats").get("private", True):
        await answer(interaction, embeds=[discord.Embed.from_dict(form)], lasts="looks")
        return
    embed = discord.Embed.from_dict(form)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def _player(part: Any, typed: str) -> tuple[str, str]:
    """(their ID, their name) for a name they've played under, the one with the most games if several have. Found
    through the names Huragok keeps (indexed), not by reading every stats row: 8 s on a busy year (audit 0.6.1, 2.3)."""
    wanted = typed.strip()
    found = part.ctx.players.named(wanted)
    if len(found) > 1:
        games = {player_id: _games(part, player_id) for player_id, _ in found}
        found = sorted(found, key=lambda one: -games[one[0]])
    if found:
        return found[0]
    # Someone in the stats Huragok never saw join (games read in from elsewhere): the long way, in a thread (5.2).
    rows = await leaderboards.read_soon(part.ctx.db, _by_stats_name, wanted)
    return (str(rows[0]["player_id"]), str(rows[0]["name"])) if rows else ("", wanted)


def _by_stats_name(db: Any, wanted: str) -> list[Any]:
    return db.query("SELECT people.player_id, p.name, COUNT(*) AS games FROM stats_players p JOIN stats_people people ON "
                    "people.number = p.person WHERE casefold(p.name) = casefold(?) GROUP BY p.person ORDER BY games DESC LIMIT 1", (wanted,))


def _games(part: Any, player_id: str) -> int:
    rows = part.ctx.db.query("SELECT COUNT(*) AS n FROM stats_players p JOIN stats_people people ON people.number = p.person "
                             "WHERE people.player_id = ?", (player_id.lower(),))
    return int(rows[0]["n"]) if rows else 0


async def _players_with_stats(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    part = part_of(interaction)
    # The names Huragok keeps, those with stats: a keystroke read every stats row (audit 0.6.1, 2.3).
    rows = part.ctx.db.query(
        "SELECT n.id AS player_id, n.name FROM player_names n JOIN stats_people people ON people.player_id = n.id "
        "WHERE n.folded LIKE '%' || ? || '%' ESCAPE '\\' ORDER BY n.last_seen DESC LIMIT ?",
        (typed.strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"), MOST_CHOICES * 4))
    names = []
    for row in rows:
        if getattr(part.ctx, "name_unfit", lambda _: False)(str(row["name"])):  # 5.4
            continue
        names.append(str(row["name"]))
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in dict.fromkeys(names)][:MOST_CHOICES]


async def _stats_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    part = part_of(interaction)
    wanted = typed.casefold().strip()
    found = [(stat.title, key) for key, stat in leaderboards.STATS.items()] + [("Red against Blue", standings.RED_BLUE)]
    found += [(f"{words_of(name)} (medal)", f"medal:{name}") for name in leaderboards.choices(part.ctx.db, "medal", most=500)]
    found += [(f"{words_of(name)} kills", f"weapon:{name}") for name in leaderboards.choices(part.ctx.db, "weapon", most=500)]
    return [app_commands.Choice(name=title[:100], value=value) for title, value in found
            if wanted in title.casefold() or wanted in value][:MOST_CHOICES]


def _choices_of(kind: str) -> Any:
    async def suggest(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=name[:100], value=name[:100]) for name in leaderboards.choices(part_of(interaction).ctx.db, kind, typed)]

    return suggest


@app_commands.command(name="stats", description="A player's service record on these servers")
@app_commands.describe(player="Their name in game. Left out: yours, once you've linked (!link in game)")
@app_commands.autocomplete(player=_players_with_stats)
async def stats_command(interaction: discord.Interaction, player: str = "") -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not standings.takes(part):
        await answer(interaction, "The stats aren't switched on here.")
        return
    await interaction.response.defer(ephemeral=bool(part.file.section("stats").get("private", True)), thinking=True)  # 5.2
    mine = part.links.of_member(str(interaction.user.id)) if part.links.takes() else None
    if player.strip():
        player_id, name = await _player(part, player)
    elif mine is not None:
        player_id, name = str(mine["player_id"]), part.links.player_name(mine)
    else:
        await answer(interaction, "Whose? Give their name in game, or link yours: type **!link** in game." if part.links.takes()
                     else "Whose? Give their name in game.")
        return
    own = mine is not None and str(mine["player_id"]) == player_id
    card = await leaderboards.read_soon(part.ctx.db, leaderboards.card, player_id) if player_id else None
    if card is None:
        await answer(interaction, f"No reported games for {clean(name)} yet: a game counts once its server reports it.")
        return
    await _show(part, interaction, standings.record_of(part, card, mine=own))


@app_commands.command(name="leaderboard", description="The best on these servers at anything they count")
@app_commands.describe(stat="What's counted: kills, K/D, wins, a medal, a weapon…", period="Over how long",
                       server="One server's games", map="One map's games", mode="One kind of game",
                       season="A past season's, by its number")
@app_commands.choices(period=PERIODS)
@app_commands.autocomplete(stat=_stats_choices, server=_choices_of("server"), map=_choices_of("map"), mode=_choices_of("mode"))
async def leaderboard_command(interaction: discord.Interaction, stat: str = "kills", period: app_commands.Choice[str] | None = None,
                              server: str = "", map: str = "", mode: str = "", season: int = 0) -> None:  # noqa: A002 - the name people see
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not standings.takes(part):
        await answer(interaction, "The stats aren't switched on here.")
        return
    wanted = stat.strip().casefold()
    wanted = next((key for key, known in leaderboards.STATS.items() if wanted in (key, known.title.casefold())), wanted)
    if wanted != standings.RED_BLUE and not leaderboards.known(wanted):
        await answer(interaction, f"There's no {clean(stat)} to count: pick one from the list as you type.")
        return
    past = leaderboards.season_numbered(part.ctx.db, season) if season > 0 else None
    if season > 0 and past is None:
        await answer(interaction, f"There's no season {season}.")
        return
    board = Board(wanted if wanted != standings.RED_BLUE else "wins", "season" if past is not None else period.value if period is not None else "all",
                  server.strip(), map.strip(), mode.strip())
    await interaction.response.defer(ephemeral=bool(part.file.section("stats").get("private", True)), thinking=True)  # a big board takes a moment
    await _show(part, interaction, await standings.red_blue_of(part, past, "" if past is not None or period is None else period.value) if wanted == standings.RED_BLUE else await standings.board_of(part, board, season=past))


async def kills_pressed(part: Any, interaction: discord.Interaction, custom_id: str) -> None:
    """A Carnage report's Kill feed: that game's kills, to whoever pressed it; a file when they're too many for a message."""
    import io
    import re

    from .speakers import say

    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    number = custom_id.removeprefix(standings.KILLS_BUTTON)
    await interaction.response.defer(ephemeral=True, thinking=True)  # a big game's feed takes a moment (audit 0.6.1, 5.3)
    lines = standings.kill_feed(part, int(number)) if number.isdecimal() else []
    if not lines:
        await answer(interaction, say(part.file, "stats.kills_gone"))
        return
    text = "\n".join(lines)
    if len(text) <= MOST_IN_A_MESSAGE:
        await answer(interaction, text, lasts="looks")
        return
    plain = re.sub(r"[*`]", "", text).replace("\\", "")
    await answer(interaction, f"{len(lines):,} kills: here they are as a file.", lasts="looks",
                 file=discord.File(io.BytesIO(plain.encode("utf-8")), filename="kill-feed.txt"))


__all__ = ["kills_pressed", "leaderboard_command", "stats_command"]
