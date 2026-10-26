"""Halo 3: ODST servers (Project Reclaimer 0.9.19 and later): what they can play and how Huragok says it.

A server plays ODST with `odst = { … }` in its [[server]] block, in place of a playlist, map and game, variant or
campaign: a campaign mission (`mission`, `insertion` = where it starts, `difficulty`, `modifiers`), or Firefight
(`mode = "firefight"`, `mission` + `insertion` name the map, `variant` the Firefight game type). It holds 16
players at most. Its game folder needs ODST's files (`dedicated init --from --maps odst`, about 3.5 GB):
newserver.add_odst_files puts them there.

The program's own `dedicated content odst` lists what this ODST can play, with the setting for each; the tables
below are that listing from 0.9.20, used when the program can't be asked and to name a setup in a few words.

What an ODST server does differently, as the program's guide and its answers on 0.9.20 have it:
- it waits in its lobby for min_players, counts down and starts; when the game is over or its last player
  leaves, it opens a new lobby within seconds (RCON `status` says phase "no_game" there);
- a player who gets in logs "ODST: player <name> joined the game in progress and spawns now";
- its console: up to 0.9.20 it couldn't change the game, teams or rounds. The guide said the server refused those
  commands, but 0.9.20 still took `map`, `mode` and `load` in its lobby and queued a Halo 3 game as the next one,
  so Huragok refuses them itself on those versions (console_refusal). From 0.9.21 the program changes an ODST
  server's mission, Firefight map and type from the console (`map`, `mode`, `load`, `nextmap`, `endgame`, and
  `maps`/`modes` list them) and refuses the rest in its own words, so Huragok sends everything on.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import Any

from .halo3_options import version_tuple

FILES_HINT = "dedicated init --from --maps odst"

# (mission, title, the highest insertion: 0 is the mission's start, 1 and up its other starts)
MISSIONS: list[tuple[int, str, int]] = [
    (4100, "Prepare to Drop", 0), (5000, "Mombasa Streets", 6), (6100, "Tayari Plaza", 3),
    (6110, "Uplift Reserve", 3), (6120, "Kizingo Boulevard", 4), (6130, "ONI Alpha Site", 3),
    (6140, "NMPD HQ", 4), (6150, "Kikowani Station", 5), (5200, "Data Hive", 5), (5300, "Coastal Highway", 6),
    (4200, "Epilogue", 0),
]
# (mission, insertion, title): a Firefight map is a mission's area, named by both numbers.
FIREFIGHT_MAPS: list[tuple[int, int, str]] = [
    (6100, 4, "Crater"), (5000, 7, "Crater (Night)"), (6110, 4, "Lost Platoon"), (6120, 5, "Rally Point"),
    (5000, 8, "Rally (Night)"), (6130, 4, "Security Zone"), (6130, 5, "Alpha Site"), (6140, 5, "Windward"),
    (5200, 6, "Chasm Ten"), (5300, 7, "Last Exit"),
]
# (variant, title) as the program lists them; titles repeat, so the screens show the variant too.
FIREFIGHT_TYPES: list[tuple[str, str]] = [
    ("odst_ff_standard_1set", "Firefight"), ("odst_ff_classic", "Ff Classic"),
    ("odst_ff_drone_wars_1round", "Bug Hunt"), ("odst_ff_fiestafight_1set", "Fiestafight"),
    ("odst_ff_floodfight", "Floodfight"), ("odst_ff_gruntpocalypse_1round", "Gruntpocalypse"),
    ("odst_ff_robocalypse_1round", "Robocalypse"), ("odst_ff_rocketfight_1set", "Rocketfight"),
    ("odst_ff_sniperfight_1set", "Sniperfight"), ("odst_ff_bvt_1wave", "Firefight Bvt"),
    ("odst_ff_evolved_1set", "Firefight Elite"), ("odst_ff_fiestaattack_1round", "Fiesta Attack"),
    ("odst_ff_fistfight_1set", "Fistfight"), ("odst_ff_flierflight_1set", "Flierflight"),
    ("odst_ff_floodattack_1round", "Spore Attack"), ("odst_ff_floodfight_1set", "Floodfight"),
    ("odst_ff_frgattack_1round", "Frg Attack"), ("odst_ff_frgfight_1set", "Frg Fight"),
    ("odst_ff_limited_1set", "Ff Limited"), ("odst_ff_nadeattack_1round", "Nade Attack"),
    ("odst_ff_nadefight_1set", "Nadefight"), ("odst_ff_rocketattack_1round", "Rocket Attack"),
    ("odst_ff_scoreattack_1round", "Score Attack"), ("odst_ff_sniperattack_1round", "Sniper Attack"),
    ("odst_ff_splocketattack_1round", "Splocket Attack"), ("odst_ff_splocketfight_1set", "Splocketfight"),
    ("odst_ff_standard_1round", "Firefight"),
]
DEFAULT_TYPE = "odst_ff_standard_1set"  # what a Firefight server plays when `variant` is left out

# The console commands that would change an ODST server's game, teams or rounds, and what the program says to them.
CHANGES_GAME = {"maps", "modes", "map", "mode", "load", "teamcount", "shuffle", "shuffleteams", "team", "setteam",
                "endround", "endgame"}
REFUSAL = "This server plays Halo 3: ODST: the console cannot change its game, teams or rounds."
CONSOLE_SINCE = (0, 9, 21)  # from this version the program itself rules an ODST server's console


@dataclass(frozen=True)
class Choice:
    """One thing an ODST server can play, as the window's numbered lists show it."""
    kind: str  # mission, firefight map, firefight type
    title: str
    mission: int = 0
    insertion: int = 0  # a Firefight map's own; for a mission, its highest start
    variant: str = ""

    @property
    def label(self) -> str:
        if self.kind == "mission":
            starts = f"starts 0-{self.insertion}" if self.insertion else "one start"
            return f"{self.title}  ({self.mission}, {starts})"
        if self.kind == "firefight map":
            return f"{self.title}  ({self.mission}, start {self.insertion})"
        return f"{self.title}  [{self.variant}]"


@dataclass
class Listing:
    missions: list[Choice]
    maps: list[Choice]
    types: list[Choice]

    @property
    def empty(self) -> bool:
        return not (self.missions or self.maps or self.types)


def builtin() -> Listing:
    """What 0.9.20 lists with every ODST file in place."""
    return Listing([Choice("mission", title, mission, high) for mission, title, high in MISSIONS],
                   [Choice("firefight map", title, mission, insertion) for mission, insertion, title in FIREFIGHT_MAPS],
                   [Choice("firefight type", title, variant=variant) for variant, title in FIREFIGHT_TYPES])


HEADER = re.compile(r"^Halo 3: ODST (?P<what>campaign missions|Firefight maps|Firefight game types)\b")
SETUP_LINE = re.compile(r"^\s+odst = (?P<setup>\{.*?\})\s{2,}(?P<title>.+?)\s*$")
TYPE_LINE = re.compile(r'^\s+variant = "(?P<variant>[^"]+)"\s+(?P<title>.+?)\s*$')
STARTS = re.compile(r"\s*\(insertion 0 to (?P<high>\d+)\)$")


def parse_listing(text: str) -> Listing:
    """`dedicated content odst` (or plain `dedicated content`): the ODST sections only. Empty when the program
    lists none, which means the server's game folder has no ODST files."""
    listing = Listing([], [], [])
    section = ""
    for line in text.splitlines():
        if header := HEADER.match(line):
            section = header.group("what")
            continue
        if line and not line[0].isspace():
            section = ""  # another section, or the closing hint
            continue
        if section == "Firefight game types":
            if match := TYPE_LINE.match(line):
                listing.types.append(Choice("firefight type", match.group("title"), variant=match.group("variant")))
            continue
        match = SETUP_LINE.match(line)
        if not section or not match:
            continue
        try:
            setup = tomllib.loads(f"odst = {match.group('setup')}")["odst"]
        except (tomllib.TOMLDecodeError, KeyError):
            continue
        title = match.group("title")
        if section == "campaign missions":
            starts = STARTS.search(title)
            listing.missions.append(Choice("mission", STARTS.sub("", title), int(setup.get("mission", 0)),
                                           int(starts.group("high")) if starts else 0))
        else:
            listing.maps.append(Choice("firefight map", title, int(setup.get("mission", 0)),
                                       int(setup.get("insertion", 0))))
    return listing


def is_firefight(setup: dict[str, Any]) -> bool:
    return str(setup.get("mode", "")).lower() == "firefight"


def mission_title(mission: Any) -> str:
    return next((title for number, title, _ in MISSIONS if str(number) == str(mission)), str(mission))


def map_title(mission: Any, insertion: Any) -> str:
    return next((title for m, i, title in FIREFIGHT_MAPS if str(m) == str(mission) and str(i) == str(insertion)),
                f"map {mission}/{insertion}")


def type_title(variant: Any) -> str:
    variant = str(variant or DEFAULT_TYPE)
    return next((title for v, title in FIREFIGHT_TYPES if v.lower() == variant.lower()), variant)


def describe(setup: Any) -> str:
    """An `odst = { … }` setup in a few words: "ODST Firefight Crater (Night), Ff Classic (heroic)",
    "ODST campaign Tayari Plaza (normal)"."""
    setup = setup if isinstance(setup, dict) else {}
    difficulty = str(setup.get("difficulty", "normal"))
    if is_firefight(setup):
        where = map_title(setup.get("mission"), setup.get("insertion", 0))
        kind = f", {type_title(setup['variant'])}" if setup.get("variant") else ""
        return f"ODST Firefight {where}{kind} ({difficulty})"
    insertion = setup.get("insertion", 0)
    start = f", start {insertion}" if str(insertion) not in ("0", "") else ""
    return f"ODST campaign {mission_title(setup.get('mission', ''))}{start} ({difficulty})"


def console_refusal(line: str, version: str | None = None) -> str | None:
    """What an ODST server's console says to a command that would change its game, or None to send it on.
    `version` is the program the server runs (as it reports it): from 0.9.21 everything goes on and the program
    answers for itself. Before, or when the version isn't known, the game-changing commands are refused here:
    `nextmap` on its own only asks, so it's let through; `startvote kick` is the one vote it holds."""
    words = line.strip().split()
    if not words:
        return None
    number = version_tuple(version)
    if number is not None and number >= CONSOLE_SINCE:
        return None
    command = words[0].lower()
    if command in CHANGES_GAME:
        return REFUSAL
    if command == "nextmap" and len(words) > 1:
        return REFUSAL
    if command == "startvote" and (len(words) < 2 or words[1].lower() != "kick"):
        return REFUSAL
    return None


__all__ = ["MISSIONS", "FIREFIGHT_MAPS", "FIREFIGHT_TYPES", "DEFAULT_TYPE", "REFUSAL", "CONSOLE_SINCE", "FILES_HINT",
           "Choice", "Listing", "builtin", "parse_listing", "describe", "is_firefight", "mission_title", "map_title",
           "type_title", "console_refusal"]
