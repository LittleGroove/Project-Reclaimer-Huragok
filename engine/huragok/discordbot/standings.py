"""The stats in Discord (0.6.1): a player's service record (/stats) and a leaderboard (/leaderboard), as Discord's own
cards, from what servers/leaderboards.py works out of every reported game.

Who's shown is by the name people know them by here when they've linked (links.py: [link] names, or their own /link
show), else the name they last played under. A name Vergil wouldn't let in a game (names.txt, a word of the word
list) is never shown to everyone: it reads "(name hidden)". Nobody's left off a board:
a spot earned is shown (the host's call, 3 November 2026: the boards don't discriminate).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..servers import leaderboards
from ..servers.leaderboards import Board, Card, words_of
from .cards import Card as FeedCard
from .cards import clean, heading
from .config import colour_value
from .speakers import say

if TYPE_CHECKING:
    from .part import DiscordPart

HIDDEN_NAME = "(name hidden)"
MOST_TEXT = 4000  # a card's text, under Discord's 4,096
MOST_MEDALS = 10  # a Carnage report's rare medals named; the rest counted
MOST_TIED = 5  # leaders named on a season's card; the rest counted
COLOUR = "blurple"
PERIOD_WORDS = {"all": "All time", "season": "This season", "month": "The last 30 days", "week": "The last 7 days",
                "day": "The last 24 hours"}  # the season to pick too (audit 0.6.1, 2.6)


def takes(part: DiscordPart) -> bool:
    return bool(part.file.section("stats").get("enabled")) and part.file.mode == "bot"


def shown(part: DiscordPart, player_id: str, game_name: str) -> str | None:
    """Their name as everyone reads it, or None to leave them off a board."""
    return clean(fit_to_show(part, part.links.shown_name(player_id, game_name), game_name))


def fit_to_show(part: DiscordPart, name: str, game_name: str) -> str:
    """A name Vergil wouldn't let in a game is never shown: on its own it reads "(name hidden)"; beside a Discord
    name, as "both" shows them, it's left out (audit 0.6.1, 5.4: "Cool_Guy (Bannedname)" was shown everywhere)."""
    unfit = getattr(part.ctx, "name_unfit", None)
    if unfit is None or not game_name or game_name not in name or not unfit(game_name):
        return name
    return HIDDEN_NAME if name == game_name else name.replace(f" ({game_name})", "")


def counted(value: Any, default: int, low: int, high: int) -> int:
    """A count from discord.toml, kept to what makes sense: a 0, an inf or a nan from a hand's edit took a default, or
    stopped every tick, or made a board too long for Discord (audit 0.6.1, 2.11)."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _min_games(part: DiscordPart) -> int:
    return counted(part.file.section("stats").get("min_games", leaderboards.MIN_GAMES), leaderboards.MIN_GAMES, 1, 1000)


def _rows(part: DiscordPart) -> int:
    return counted(part.file.section("stats").get("rows", leaderboards.ROWS), leaderboards.ROWS, 1, 25)


async def board_of(part: DiscordPart, wanted: Board, *, season: Any = None) -> dict[str, Any]:
    """A leaderboard as a card. `season`: a past season's (/leaderboard season); a season board is otherwise of the
    season running, or between seasons of the one just ended."""
    shown_season = season if season is not None else leaderboards.season_shown(part.ctx.db) if wanted.period == "season" else None
    between = ((float(shown_season["started"]), float(shown_season["ended"] or 1e18))
               if season is not None and shown_season is not None else None)
    rows = await leaderboards.board_soon(part.ctx.db, Board(wanted.stat, wanted.period, wanted.server, wanted.map,
                                                            wanted.mode, _rows(part)),
                                         min_games=_min_games(part), between=between)
    lines = [f"**{row.place}.** {shown(part, row.player_id, row.name) or HIDDEN_NAME} · {row.shown}"
             + (f" ({row.games} game{'s' if row.games != 1 else ''})" if wanted.stat in leaderboards.STATS and leaderboards.STATS[wanted.stat].rate else "")
             for row in rows]
    rate = wanted.stat in leaderboards.STATS and leaderboards.STATS[wanted.stat].rate
    least = _min_games(part)
    footer = f"At least {least} game{'s' if least != 1 else ''} to be on it · " if rate else ""
    empty = ("No season has been played yet: faculty start one with !newseason." if wanted.period == "season" and shown_season is None
             else "No games were reported in that season." if wanted.period == "season" and shown_season["ended"]
             else "Nobody yet: it fills up as games are reported.")
    title = wanted.title
    if wanted.period == "season" and shown_season is not None:
        title = title.replace(" · this season", " · " + clean(leaderboards.season_named(shown_season["number"], shown_season["name"]))
                              + (" (over)" if shown_season["ended"] else ""))
    text = "\n".join(lines)
    if len(text) > MOST_TEXT:  # 25 long names (audit 0.6.1, 5.5)
        text = text[:MOST_TEXT].rsplit("\n", 1)[0]
    return {"title": title[:256], "description": text or empty,
            "color": colour_value(part.file, COLOUR), "footer": {"text": f"{footer}From every game the servers reported"}}


KILLS_BUTTON = "huragok:kills:"  # and the game's number: a Carnage report's Kill feed


shown_as = shown  # the kill feed's own `shown` keeps each name it's looked up


def kill_feed(part: DiscordPart, number: int) -> list[str]:
    """A game's kills, one a line, as kept with it (servers/stats.py): [] when they're gone (KILLS_KEPT_DAYS) or never were."""
    import gzip
    import json

    from ..servers.weapons import how

    rows = part.ctx.db.query("SELECT kills FROM stats_kills WHERE game = ?", (number,))
    if not rows:
        return []
    lines = []
    names: dict[tuple[str, str], str | None] = {}  # each player's name looked up once: a big game has thousands of kills (5.3)

    def shown(player_id: str, name: str) -> str | None:
        if (player_id, name) not in names:
            names[(player_id, name)] = shown_as(part, player_id, name)
        return names[(player_id, name)]

    for at, killer_id, killer, victim_id, victim, report in json.loads(gzip.decompress(rows[0]["kills"]).decode("utf-8")):
        when = f"`{int(at) // 60:02d}:{int(at) % 60:02d}`"
        made = how(report).words()
        them = shown(victim_id, victim) if victim else "someone"
        if not killer:
            lines.append(f"{when} {them or HIDDEN_NAME} died · {made}")
        elif killer_id == victim_id:
            lines.append(f"{when} {them or HIDDEN_NAME} killed themselves · {made}")
        else:
            lines.append(f"{when} **{shown(killer_id, killer) or HIDDEN_NAME}** ▸ {them or HIDDEN_NAME} · {made}")
    return lines


RARE_MEDALS = ("killtacular", "killtrocity", "killimanjaro", "killtastrophe", "killpocalypse", "killionaire", "rampage",
               "untouchable", "invincible", "kills_in_a_row_35", "kills_in_a_row_40", "extermination", "perfection")
MOST_MEDAL_CARDS = 5  # from one game: past this, the Carnage Report has them


def _score(game: dict[str, Any]) -> str:
    if game["team_game"]:
        red, blue = int(game["red"] or 0), int(game["blue"] or 0)
        return f"**Red {red} – {blue} Blue**"
    return ""


def carnage_report(part: DiscordPart, summary: dict[str, Any], server: str | None) -> FeedCard:
    """After each game, its Postgame Carnage Report (Halo 3's own name for it)."""
    config, game = part.file, summary["game"]
    what = " · ".join(clean(text) for text in (game["map_title"] or game["map"], game["variant"] or game["mode"]) if text)
    winner = {"0": "Red wins", "1": "Blue wins", "tie": "a tie"}.get(str(game["winner"]), "")
    if not game["team_game"] and summary["top"]:
        winner = f"{shown(part, summary['top'][0]['player_id'], summary['top'][0]['name']) or HIDDEN_NAME} wins"
    line = say(config, "game.report", what=what, result=", ".join(part_ for part_ in (_score(game), winner) if part_) or "over",
               players=summary["players"])
    fields = []
    if summary["top"]:
        fields.append(("Top", "\n".join(f"{row['place'] or number}. {shown(part, row['player_id'], row['name']) or HIDDEN_NAME} · "
                                        f"{row['score']} ({row['kills']}/{row['deaths']})" for number, row in enumerate(summary["top"], 1))))
    if summary["best_streak"]:
        row = summary["best_streak"]
        fields.append(("Best streak", f"{shown(part, row['player_id'], row['name']) or HIDDEN_NAME} · {row['best_streak']}"))
    listed = list(_rare(part))
    seen = leaderboards.rarity()
    rare = sorted((medal for medal in summary["medals"] if medal["medal"] in listed),  # the rarest first (second audit, 7.2)
                  key=lambda medal: (seen.get(medal["medal"], 0) if seen else -listed.index(medal["medal"]), medal["name"]))
    if rare:
        fields.append(("Medals", ", ".join(f"{shown(part, medal['player_id'], medal['name']) or HIDDEN_NAME}: "
                                           f"{words_of(medal['medal'])}{' ×' + str(medal['count']) if medal['count'] > 1 else ''}"
                                           for medal in rare[:MOST_MEDALS]) + (f" and {len(rare) - MOST_MEDALS} more" if len(rare) > MOST_MEDALS else "")))
    card = FeedCard("games", "report", line, heading(config, "taxes", "Carnage report"), fields, server=server)
    if part.file.section("feed", "games").get("kill_feed", True) and part.ctx.db.query("SELECT 1 FROM stats_kills WHERE game = ?", (game["number"],)):
        card.components = [{"type": 1, "components": [{"type": 2, "style": 2, "label": say(config, "button.kill_feed")[:80],
                                                       "custom_id": f"{KILLS_BUTTON}{game['number']}"}]}]
    return card


def _rare(part: DiscordPart) -> set[str]:
    listed = part.file.section("feed", "games").get("rare_medals")
    return {str(name) for name in listed} if isinstance(listed, list) else set(RARE_MEDALS)


async def season_card(part: DiscordPart, data: dict[str, Any]) -> FeedCard:
    """A season begun or over, Red against Blue in it."""
    config = part.file
    name = clean(str(data.get("name") or ""))
    if data.get("action") == "started":
        ends = data.get("ends")
        line = say(config, "game.season_started", number=data.get("number"), name=name, season=clean(leaderboards.season_named(data.get("number"), data.get("name"))),
                   until=f", until <t:{int(float(ends))}:f>" if ends else "")
        return FeedCard("games", "season", line, heading(config, "hello", "New season"))
    red, blue, ties = int(data.get("red") or 0), int(data.get("blue") or 0), int(data.get("ties") or 0)
    outcome = "Red takes it" if red > blue else "Blue takes it" if blue > red else "Neither side takes it"
    line = say(config, "game.season_ended", number=data.get("number"), name=name, season=clean(leaderboards.season_named(data.get("number"), data.get("name"))), score=f"**Red {red} – {blue} Blue**",
               ties=f", {ties} tied" if ties else "", outcome=outcome)
    fields = []
    start, end = float(data.get("started") or 0), float(data.get("ended") or 0)
    for stat, title in (("kills", "Most kills"), ("wins", "Most wins")):  # as the boards have them: those off them skipped (2.10)
        best = await leaderboards.board_soon(part.ctx.db, Board(stat, rows=1), between=(start, end or 1e18), ties=True)
        if best:  # everyone tied names the place (the host's call 16)
            named = ", ".join(shown(part, row.player_id, _name_of(part, row.player_id)) or HIDDEN_NAME for row in best[:MOST_TIED])
            more = f" and {len(best) - MOST_TIED} more" if len(best) > MOST_TIED else ""
            fields.append((title, f"{named}{more} · {best[0].shown}"))
    return FeedCard("games", "season", line, heading(config, "last train", "Season over"), fields)


def _name_of(part: DiscordPart, player_id: str) -> str:
    rows = part.ctx.db.query("SELECT p.name FROM stats_players p JOIN stats_people people ON people.number = p.person "
                             "WHERE people.player_id = ? ORDER BY p.ended DESC LIMIT 1", (player_id,))
    return str(rows[0]["name"]) if rows else part.name_of(player_id)


RED_BLUE = "red-blue"
BOARD_KEY = "leaderboard:"  # in discord_posts: "board:" is the status board's (audit W12)
FLOOR_MINUTES = 5.0


def _season_channel(part: DiscordPart, options: dict[str, Any], channel: str) -> str:
    """[leaderboards] season_channel, or the boards' own channel when it's not set, or isn't a channel the bot can post
    in: one that wasn't took the season's boards down and tried every 10 minutes (second audit, 7.1). Said once."""
    from . import features, kept

    ref = str(options.get("season_channel") or "").strip()
    if not ref:
        return channel
    found = kept.channel_of(part, ref)
    where = part.facts.find_channel(found) if part.facts is not None and found else None
    if where is not None and not features._missing_permissions(features.POSTS, part.facts, where):
        return found
    if ref not in part.__dict__.setdefault("season_channel_said", set()):
        part.season_channel_said.add(ref)
        part.log.warning("[leaderboards] season_channel: %r isn't a channel the bot can post in: the season's boards go "
                         "with the rest until it is", ref)
    return channel


def is_season_board(entry: Any) -> bool:
    """A season's board (or Red against Blue, which is the season's first): kept in the season channel."""
    words = str(entry or "").strip().lower().split()
    return bool(words) and (words[0] == RED_BLUE or (len(words) == 2 and words[1] == "season"))


def ordered_boards(entries: list[Any]) -> list[Any]:
    """The host's order, all time's boards first and the season's below them (the host's call, 3 November 2026)."""
    return [entry for entry in entries if not is_season_board(entry)] + [entry for entry in entries if is_season_board(entry)]


def board_wanted(entry: Any) -> Board | None:
    """A line of [leaderboards] boards, "kills season" or "medal:overkill week", as the board it means."""
    words = str(entry or "").strip().lower().split()
    if not words or words[0] == RED_BLUE or len(words) > 2 or (len(words) == 2 and words[1] not in leaderboards.PERIODS):
        return None  # "kills seasonal" isn't all time: it's nothing, and said so (keep_boards)
    return Board(words[0], words[1] if len(words) == 2 else "all") if leaderboards.known(words[0]) else None


async def red_blue_of(part: DiscordPart, season: Any = None, period: str = "") -> dict[str, Any]:
    """Red against Blue: the season's score (the one running, or between seasons the one just ended, or one asked for),
    all time's, and who's chosen each side. `period` (month, week or day, from /leaderboard): its score first (the
    host's call, 4 November 2026: it was ignored)."""
    import time

    db = part.ctx.db
    season = season if season is not None else leaderboards.season_shown(db)
    now = await leaderboards.read_soon(db, leaderboards.red_blue, float(season["started"]), float(season["ended"]) if season["ended"] else None) \
        if season is not None else None  # in a thread (7.3)
    ever = await leaderboards.read_soon(db, leaderboards.red_blue)
    lines = []
    if leaderboards.PERIODS.get(period):
        span = await leaderboards.read_soon(db, leaderboards.red_blue, time.time() - float(leaderboards.PERIODS[period]))
        lines.append(f"**{PERIOD_WORDS.get(period, period)}: Red {span['red']} – {span['blue']} Blue**" + (f", {span['ties']} tied" if span["ties"] else ""))
    if season is not None and now is not None:
        lines.append(f"**Red {now['red']} – {now['blue']} Blue**" + (f", {now['ties']} tied" if now["ties"] else "")
                     + f" in {clean(leaderboards.season_named(season['number'], season['name']))[:1].lower()}{clean(leaderboards.season_named(season['number'], season['name']))[1:]}, since <t:{int(float(season['started']))}:D>"
                     + (f", until <t:{int(float(season['ended']))}:D> (over)" if season["ended"] else
                        f", until <t:{int(float(season['ends']))}:D>" if season["ends"] else ""))
    lines.append(f"All time: Red {ever['red']} – {ever['blue']} Blue" + (f", {ever['ties']} tied" if ever["ties"] else ""))
    from . import sides

    if sides.takes(part):
        chosen = db.query("SELECT side, COUNT(*) AS n FROM discord_sides WHERE side IN ('red', 'blue') GROUP BY side")
        counts = {str(row["side"]): int(row["n"]) for row in chosen}
        if counts:
            lines.append(f"Fighting for Red: {counts.get('red', 0)} · for Blue: {counts.get('blue', 0)} (`!side` in game)")
    return {"title": "Red against Blue" + (f" · season {season['number']}" if season is not None else ""),
            "description": "\n".join(lines), "color": colour_value(part.file, COLOUR),
            "footer": {"text": "Team games won, from every game the servers reported"}}


async def keep_boards(part: DiscordPart, now: float) -> None:
    """The boards [leaderboards] lists, each one message in its channel, brought up to date every update_minutes
    (nothing goes to Discord unless one has changed). Taken off the list, a board's message goes."""
    from . import features, kept

    options = part.file.section("leaderboards")
    if part.facts is None:  # Discord not read yet (the bot started again): that isn't "off" (audit 0.6.1, 5.6)
        return
    state = features.state_of(features.BY_KEY["leaderboards"], part.file, part.facts)[0]
    if state == features.WAITING:
        return
    every = counted(options.get("update_minutes", 15), 15, int(FLOOR_MINUTES), 1440) * 60.0  # an inf stopped them (9.9)
    # What they're of: the boards, their order, their channels, their rows. A change to those is kept at the next tick,
    # not at the next update_minutes: what the host changes in setup shows as they change it (the host, 4 November 2026:
    # a new list and order, an hour's update_minutes, and the old boards stayed up for the hour).
    shape = repr((options.get("boards"), options.get("channel"), options.get("season_channel"), state,
                  part.file.section("stats").get("rows"), part.file.section("stats").get("min_games")))
    if state == features.ON and now - getattr(part, "boards_kept", 0.0) < every and shape == getattr(part, "boards_shape", None):
        return
    part.boards_kept, part.boards_shape = now, shape
    channel = kept.channel_of(part, options.get("channel")) if state == features.ON else ""
    season_channel = _season_channel(part, options, channel) if channel else ""
    wanted: dict[str, tuple[str, dict[str, Any]]] = {}
    if channel:
        for number, entry in enumerate(ordered_boards(options.get("boards") or [])):  # all time first, the season's below (12)
            key = f"{BOARD_KEY}{number}:{str(entry).strip().lower()}"
            where = season_channel if is_season_board(entry) else channel
            if str(entry).strip().lower() == RED_BLUE:
                wanted[key] = (where, await red_blue_of(part))
            elif (board := board_wanted(entry)) is not None:
                wanted[key] = (where, await board_of(part, board))
            elif str(entry) not in part.__dict__.setdefault("boards_unknown", set()):  # once each (audit 0.6.1, 5.10)
                part.boards_unknown.add(str(entry))
                part.log.warning("[leaderboards] boards: %r isn't a board (a stat, then all, season, month, week or day), so it's left out", str(entry))
    for row in part.ctx.db.query("SELECT key FROM discord_posts WHERE key LIKE ?", (BOARD_KEY + "%",)):
        if str(row["key"]) not in wanted:
            kept.take_down(part, str(row["key"]))
    for key, (where, form) in wanted.items():
        kept.keep(part, key, where, {"embeds": [form], "allowed_mentions": {"parse": []}})


def _hours(seconds: int) -> str:
    hours = seconds / 3600
    return f"{hours:.1f} hours" if hours < 100 else f"{hours:,.0f} hours"


def record_of(part: DiscordPart, card: Card, *, mine: bool = False) -> dict[str, Any]:
    """A player's service record as a card."""
    name = part.links.shown_name(card.player_id, card.name) if part.links.takes() else card.name
    name = fit_to_show(part, name, card.name)  # their own too: it can be posted for the channel ([stats] private = false)
    played = card.wins + card.losses + card.ties
    line = (f"{card.games:,} game{'s' if card.games != 1 else ''} · {card.wins} won, {card.losses} lost"
            + (f", {card.ties} tied" if card.ties else "") + (f" ({card.win_rate * 100:.0f}%)" if played else ""))
    fields = [("K/D", f"{card.kd:.2f}"), ("Kills · deaths · assists", f"{card.kills:,} · {card.deaths:,} · {card.assists:,}"),
              ("Headshots", f"{card.headshots:,}"), ("Best streak", str(card.best_streak)), ("Time played", _hours(card.seconds))]
    if card.best_multi_kill:
        fields.append(("Best multi-kill", f"{words_of(card.best_multi_kill[0])} ×{card.best_multi_kill[1]}"))
    if card.best_spree:
        fields.append(("Best spree", f"{words_of(card.best_spree[0])} ×{card.best_spree[1]}"))
    if card.medals:
        fields.append(("Medals", ", ".join(f"{words_of(medal)} ×{count}" for medal, count in card.medals)))
    if card.weapon:
        fields.append(("Most kills with", f"{words_of(card.weapon[0])} ({card.weapon[1]:,})"))
    for title, them in (("Nemesis", card.nemesis), ("Favourite victim", card.victim)):
        if them:
            fields.append((title, f"{shown(part, them[0], them[1]) or HIDDEN_NAME} ({them[2]})"))
    official = card.official
    if official is not None and str(official["title"] or ""):
        grade = f" grade {official['grade']}" if official["grade"] else ""
        fields.append(("Project Reclaimer rank", f"{clean(official['title'])}{grade} · {official['exp']} EXP · as of <t:{int(official['as_of'])}:R>"))
    return {"title": f"Service record · {clean(name)}", "description": line, "color": colour_value(part.file, COLOUR),
            "fields": [{"name": title, "value": value[:1024], "inline": True} for title, value in fields][:25],
            "footer": {"text": "From every game these servers reported"},
            **({"timestamp": _iso(card.last)} if card.last else {})}


def _iso(when: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).isoformat()


__all__ = ["HIDDEN_NAME", "KILLS_BUTTON", "RARE_MEDALS", "kill_feed", "board_of", "carnage_report", "record_of",
           "season_card", "shown", "takes"]
