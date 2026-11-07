"""What a server plays, as its dedicated.toml [[server]] block says it: one kind of thing per server (a
playlist, one map and game type, a variant, Halo 3's campaign, ODST), and the words the dashboard says it in.

From Project Reclaimer 0.10 a map may be Reach's, Combat Evolved's or Halo 2's (games/bungie.py): the words name
the game where it isn't Halo 3 ("Slayer on Sword Base (Reach)"), and the pickers offer each map the game types it
plays (takes): Reach's maps Halo 3's game types or Reach's own variants, Combat Evolved's and Halo 2's only their
own. 0.10 also takes a `reach = { … }` block the program doesn't document yet (its fields: map, shading, campaign,
firefight, start, mod_map); Huragok writes none, but knows it as a kind so a change of what it plays replaces it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bungie import GAMES, Game, game_of_map
from .halo3 import MISSIONS, entry_game
from .odst import console_refusal as odst_refusal
from .odst import describe as describe_odst


PLAYS_KEYS = ("playlist", "map", "game", "base_map", "variant", "campaign", "odst", "reach")  # one kind of these


CO_OP_MAX_PLAYERS = 16  # a campaign or ODST server holds no more: a higher max_players stops its start


def plays_kind(block: dict[str, Any]) -> str:
    """Which kind of thing a [[server]] block plays: playlist, campaign, odst, variant, map (one map and game type)
    or default (nothing set: Slayer on Guardian), or reach (0.10's `reach = { … }`, written by hand). One kind per
    server; this is the order the dashboard reads them."""
    for key in ("playlist", "odst", "campaign", "reach", "variant"):
        if key in block:
            return key
    return "map" if "map" in block or "game" in block else "default"


def plays_of(block: dict[str, Any]) -> str:
    """What a [[server]] block plays, as the dashboard says it: playlist slayer, Slayer on Guardian…"""
    if "playlist" in block:
        return f"playlist {Path(str(block['playlist'])).name.removesuffix('.playlist.json')}"
    if "odst" in block:
        return describe_odst(block["odst"])
    if "campaign" in block:
        campaign = block["campaign"] if isinstance(block["campaign"], dict) else {}
        mission = campaign.get("mission", "")
        title = MISSIONS.get(int(mission), "") if str(mission).isdecimal() else ""
        return f"campaign {title or mission} ({campaign.get('difficulty', 'normal')})".replace("  ", " ")
    if "reach" in block:
        return describe_reach(block["reach"])
    if "variant" in block:
        return f"variant {Path(str(block['variant'])).stem}"
    if "map" in block or "game" in block:
        map_ = block.get("map", "Guardian")
        game = entry_game({"map": map_})  # named where it isn't Halo 3's: "Slayer on Sword Base (Reach)"
        return f"{block.get('game', 'Slayer')} on {map_}" + (f" ({game.short})" if game and game.key != "halo3"
                                                               else "")
    return "Slayer on Guardian"


def describe_reach(setup: Any) -> str:
    """A `reach = { … }` block (0.10, undocumented) in a few words, from the fields it names: "Halo: Reach Firefight
    on Beachhead". Only what it says; what each field does isn't documented yet."""
    setup = setup if isinstance(setup, dict) else {}
    kind = "Firefight" if setup.get("firefight") else "campaign" if setup.get("campaign") else ""
    where = setup.get("map") or setup.get("mod_map")
    return " ".join(bit for bit in ("Halo: Reach", kind, f"on {where}" if where else "") if bit)


def games_in(games: list[Any]) -> list[Game]:
    """The games a playlist's maps are from, Halo 3's first (a map Huragok can't place counts as Halo 3's)."""
    keys = {(entry_game(g) or GAMES["halo3"]).key for g in games if isinstance(g, dict)}
    return [game for key, game in GAMES.items() if key in keys]


def map_game(entry: Any) -> Game:
    """The game a picker's map is from: the catalogue's word for it (`game`, from 0.10's listing), else its stock
    map's, else the game its file name says (`halo1_…`), else Halo 3's. A map variant is its base map's game."""
    key = str(getattr(entry, "game", "") or "")
    if key in GAMES:
        return GAMES[key]
    if getattr(entry, "kind", "") == "map variant" and getattr(entry, "base", ""):
        found = game_of_map(entry.base)
        if found is not None:
            return found
    for name in (getattr(entry, "reference", ""), getattr(entry, "name", "")):
        found = entry_game({"map": name})
        if found is not None:
            return found
    return GAMES["halo3"]


def game_type_game(entry: Any) -> Game:
    """The game a picker's game type or variant is from: the catalogue's word for it, else its source as the
    listing names it ("Halo: Reach", "Halo: Reach matchmaking"), else Halo 3's."""
    key = str(getattr(entry, "game", "") or "")
    if key in GAMES:
        return GAMES[key]
    source = str(getattr(entry, "source", "") or "").casefold()
    return GAMES["reach"] if "reach" in source else GAMES["halo3"]


def takes(map_: Game, game: Game, kind: str = "game type") -> bool:
    """Whether a map of `map_` plays a game type or variant (`kind`) of `game`, as 0.10's guide says: Combat
    Evolved's and Halo 2's maps their own only, Reach's Halo 3's game types or Reach's own variants, Halo 3's (and
    ODST's) Halo 3's. (0.10.0's check takes more, a Halo 3 type on a CE map plays CE's nearest one; the pickers
    offer what the guide says.)"""
    if map_.own_rules_only:
        return game.key == map_.key
    if map_.key == "reach":
        return game.key == "reach" or game.key == "halo3" and kind == "game type"
    return game.key == "halo3"


def is_co_op(block: dict[str, Any]) -> bool:
    """A campaign or ODST server: co-op, CO_OP_MAX_PLAYERS at most, and no third-person camera."""
    return "campaign" in block or "odst" in block


def console_refusal(plays_kind: str, line: str, version: str | None = None) -> str | None:
    """Why the console won't take this line on a server that plays `plays_kind` and runs program `version` (None: it
    will). An ODST server before 0.9.21 plays what its file names: a command that changes the game (map, mode,
    load…) is refused, with what to do instead; from 0.9.21 the program answers for itself."""
    return odst_refusal(line, version) if plays_kind == "odst" else None

