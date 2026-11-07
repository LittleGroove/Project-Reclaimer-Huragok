"""Every Bungie Halo a server can play: Halo 3 and Halo 3: ODST, and from Project Reclaimer 0.10 Halo: Reach,
Halo: Combat Evolved and Halo 2 (the program's "early form").

One table for what the rest of Huragok needs to know about each game: its folder in MCC and in a server's game
folder, the engine file that says its files are there, the tag the server's log puts before what it says about the
game ("[reach] Halo: Reach not installed in …"), the `dedicated init --maps` word that copies its files, the Engine a
mod's ModInfo.json names for it, and its stock multiplayer maps (title, file name, map ID) as 0.10.0's
`dedicated content` lists them with MCC's files (probed 2026-11-06).

What the program's guide says about playing them (0.10.0):
- Halo 3 is always needed: every other game's maps are built on Guardian, and `init` brings it along.
- A map is named by its title, file name or ID, Halo 3's first, then Reach's, Combat Evolved's and Halo 2's:
  Halo 2's Lockout is `halo2_lockout`, as plain "Lockout" is Halo 3's Blackout by its file name.
- Reach's maps play Halo 3's game types, or Reach's own game variants by file name (`headhunter_054`).
- Combat Evolved's and Halo 2's maps play only their own game types (a Halo 3 type there plays that game's nearest
  one), and `options` and `rules` on those maps are refused.
- No server setting plays Reach's, Combat Evolved's or Halo 2's campaign or Reach's Firefight yet (0.10.0 documents
  `--maps reach-campaign` "for a Reach co-op server" and nothing else).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .halo3_options import version_tuple

SINCE = "0.10.0"  # the first program version that plays Reach, Combat Evolved and Halo 2


@dataclass(frozen=True)
class Map:
    title: str
    stem: str  # the map file's name: what `map = ` takes besides the title
    map_id: int
    firefight: bool = False  # one of Reach's Firefight maps (listed with its multiplayer maps)


@dataclass(frozen=True)
class Game:
    key: str  # halo3, odst, reach, halo1, halo2
    title: str  # "Halo: Reach"
    short: str  # "Reach", for tight spaces
    folder: str  # its folder in MCC and in a server's game folder
    engine: str  # the file that says its files are there, relative to the game folder
    tag: str  # the server log's tag for it: "[reach] …"
    init: str  # the `dedicated init --maps` word that copies its multiplayer files
    mod_engine: str  # what a mod's ModInfo.json "Engine" says for it
    since: str  # the first program version that plays it
    maps: tuple[Map, ...] = ()
    game_types: tuple[str, ...] = ()  # its own game types on its maps (empty: Halo 3's)
    own_rules_only: bool = False  # its maps refuse Halo 3's game types, `options` and `rules`

    @property
    def section(self) -> str:
        """How 0.10's `dedicated content` heads its maps: "Halo: Reach's maps"."""
        return "Maps" if self.key == "halo3" else f"{self.title}'s maps"

    def plays_on(self, version: str | None) -> bool:
        """Whether program `version` plays this game (None, unknown: assume it does)."""
        have, need = version_tuple(version), version_tuple(self.since)
        return have is None or need is None or have >= need


HALO3_MAPS = (
    Map("Assembly", "descent", 490), Map("Avalanche", "sidewinder", 470), Map("Blackout", "lockout", 520),
    Map("Citadel", "fortress", 740), Map("Cold Storage", "chillout", 600), Map("Construct", "construct", 300),
    Map("Edge", "s3d_edge", 703), Map("Epitaph", "salvation", 350), Map("Foundry", "warehouse", 480),
    Map("Ghost Town", "ghosttown", 590), Map("Guardian", "guardian", 320), Map("Heretic", "midship", 720),
    Map("High Ground", "deadlock", 310), Map("Icebox", "s3d_turf", 31), Map("Isolation", "isolation", 330),
    Map("Last Resort", "zanzibar", 30), Map("Longshore", "docks", 440), Map("Narrows", "chill", 380),
    Map("Orbital", "spacecamp", 500), Map("Rat's Nest", "armory", 580), Map("Sandbox", "sandbox", 730),
    Map("Sandtrap", "shrine", 400), Map("Snowbound", "snowbound", 360), Map("Standoff", "bunkerworld", 410),
    Map("The Pit", "cyberdyne", 390), Map("Valhalla", "riverworld", 340), Map("Waterfall", "s3d_waterfall", 706),
)

REACH_MAPS = (
    Map("Anchor 9", "dlc_slayer", 2001), Map("Battle Canyon", "cex_beaver_creek", 10020),
    Map("Beachhead", "ff50_park", 7060, True), Map("Boardwalk", "50_panopticon", 1035),
    Map("Boneyard", "70_boneyard", 1080), Map("Breakneck", "cex_headlong", 10050),
    Map("Breakpoint", "dlc_invasion", 2002), Map("Condemned", "condemned", 1500),
    Map("Corvette", "ff45_corvette", 7110, True), Map("Countdown", "45_launch_station", 1020),
    Map("Courtyard", "ff20_courtyard", 7020, True), Map("Forge World", "forge_halo", 3006),
    Map("Glacier", "ff60_icecave", 7130, True), Map("High Noon", "cex_hangemhigh", 10060),
    Map("Highlands", "trainingpreserve", 1510), Map("Holdout", "ff70_holdout", 7080, True),
    Map("Installation 04", "cex_ff_halo", 10080, True), Map("Outpost", "ff60_airview", 7030, True),
    Map("Overlook", "ff10_prototype", 7000, True), Map("Penance", "cex_damnation", 10010),
    Map("Powerhouse", "30_settlement", 1055), Map("Reflection", "52_ivory_tower", 1150),
    Map("Ridgeline", "cex_timberland", 10030), Map("Solitary", "cex_prisoner", 10070),
    Map("Spire", "35_island", 1200), Map("Sword Base", "20_sword_slayer", 1000),
    Map("Tempest", "dlc_medium", 2004), Map("Unearthed", "ff_unearthed", 7500, True),
    Map("Waterfront", "ff30_waterfront", 7040, True), Map("Zealot", "45_aftship", 1040),
)

HALO1_MAPS = (
    Map("Battle Creek", "halo1_beavercreek", 1219848476), Map("Blood Gulch", "halo1_bloodgulch", 1224137234),
    Map("Boarding Action", "halo1_boardingaction", 1219383063), Map("Derelict", "halo1_carousel", 1211402803),
    Map("Chill Out", "halo1_chillout", 1210042463), Map("Damnation", "halo1_damnation", 1216576038),
    Map("Danger Canyon", "halo1_dangercanyon", 1213916290), Map("Death Island", "halo1_deathisland", 1221309156),
    Map("Gephyrophobia", "halo1_gephyrophobia", 1212478424), Map("Hang 'Em High", "halo1_hangemhigh", 1212397335),
    Map("Ice Fields", "halo1_icefields", 1208733805), Map("Infinity", "halo1_infinity", 1219319161),
    Map("Longest", "halo1_longest", 1208926901), Map("Prisoner", "halo1_prisoner", 1216181921),
    Map("Chiron TL34", "halo1_putput", 1210332171), Map("Rat Race", "halo1_ratrace", 1212300829),
    Map("Sidewinder", "halo1_sidewinder", 1214491715), Map("Timberland", "halo1_timberland", 1209519283),
    Map("Wizard", "halo1_wizard", 1208160010),
)

HALO2_MAPS = (
    Map("Ascension", "halo2_ascension", 849311984), Map("Backwash", "halo2_backwash", 845124425),
    Map("Beaver Creek", "halo2_beavercreek", 850749724), Map("Burial Mounds", "halo2_burial_mounds", 846179691),
    Map("Coagulation", "halo2_coagulation", 852211095), Map("Colossus", "halo2_colossus", 840949404),
    Map("Containment", "halo2_containment", 849743679), Map("Ivory Tower", "halo2_cyclotron", 853848096),
    Map("Sanctuary", "halo2_deltatap", 845846126), Map("Desolation", "halo2_derelict", 850484679),
    Map("Relic", "halo2_dune", 843985635), Map("Elongation", "halo2_elongation", 852920591),
    Map("Foundation", "halo2_foundation", 847986916), Map("Gemini", "halo2_gemini", 853626254),
    Map("Headlong", "halo2_headlong", 855313501), Map("Tombstone", "halo2_highplains", 851755844),
    Map("Lockout", "halo2_lockout", 853868150), Map("Midship", "halo2_midship", 838974367),
    Map("Uplift", "halo2_needle", 850480578), Map("District", "halo2_street_sweeper", 839779902),
    Map("Terminal", "halo2_triplicate", 847567698), Map("Turf", "halo2_turf", 843826658),
    Map("Warlock", "halo2_warlock", 847415298), Map("Waterworks", "halo2_waterworks", 841109034),
    Map("Zanzibar", "halo2_zanzibar", 844392192),
)

HALO3_GAME_TYPES = ("Slayer", "Capture the Flag", "Oddball", "King of the Hill", "VIP", "Juggernaut", "Territories",
                    "Assault", "Infection", "Escalation Slayer", "Forge")

GAMES: dict[str, Game] = {game.key: game for game in (
    Game("halo3", "Halo 3", "Halo 3", "halo3", "halo3/halo3.dll", "[halo3]", "", "Halo3", "0", HALO3_MAPS,
         HALO3_GAME_TYPES),
    Game("odst", "Halo 3: ODST", "ODST", "halo3odst", "halo3odst/halo3odst.dll", "[odst]", "odst", "Halo3ODST",
         "0.9.19"),
    Game("reach", "Halo: Reach", "Reach", "haloreach", "haloreach/haloreach.dll", "[reach]", "reach", "HaloReach",
         SINCE, REACH_MAPS),
    Game("halo1", "Halo: Combat Evolved", "CE", "halo1", "halo1/halo1.dll", "[halo1]", "halo1", "Halo1", SINCE,
         HALO1_MAPS, ("Slayer", "Capture the Flag", "Oddball", "King of the Hill", "Race"), True),
    Game("halo2", "Halo 2", "Halo 2", "halo2", "halo2/halo2.dll", "[halo2]", "halo2", "Halo2", SINCE, HALO2_MAPS,
         ("Slayer", "Capture the Flag", "Assault", "Oddball", "King of the Hill", "Juggernaut", "Territories"), True),
)}

NEW_GAMES = ("reach", "halo1", "halo2")  # what 0.10 adds
MULTIPLAYER_GAMES = ("halo3", "reach", "halo1", "halo2")  # the games whose maps a playlist names (ODST has none)


def game_of_engine(engine: Any) -> Game | None:
    """The game a mod's ModInfo.json "Engine" names ("Halo3", "HaloReach", "Halo1", "Halo2"…), or None (Halo 4,
    Halo 2: Anniversary's multiplayer… what no Reclaimer version plays). Empty or missing reads as Halo 3, as MCC's
    oldest mods leave it out."""
    text = str(engine or "").strip().casefold()
    if not text:
        return GAMES["halo3"]
    return next((game for game in GAMES.values() if game.mod_engine.casefold() == text), None)


def game_of_map(name: Any) -> Game | None:
    """The game whose stock map this is, by title, file name or ID, in the order the program resolves them (Halo 3's
    first, then Reach's, Combat Evolved's and Halo 2's), or None (a mod map, a saved map, a typo)."""
    text = str(name or "").strip().casefold()
    if not text:
        return None
    for key in MULTIPLAYER_GAMES:
        for map_ in GAMES[key].maps:
            if text in (map_.title.casefold(), map_.stem.casefold(), str(map_.map_id)):
                return GAMES[key]
    return None


def game_of_tag(line: str) -> Game | None:
    """The game a server log or listing line is about ("[reach] Halo: Reach not installed in …"), or None."""
    line = line.lstrip()
    return next((game for game in GAMES.values() if line.startswith(game.tag)), None)


def installed_in_text(text: str) -> dict[str, bool]:
    """What a server program's `check`, `content` or log says about each game's files: {"reach": False, …} for every
    game it names ("[reach] Halo: Reach installed in …" / "… not installed in …"). Halo 3 is never named this way."""
    found: dict[str, bool] = {}
    for line in text.splitlines():
        game = game_of_tag(line)
        if game is None:
            continue
        said = line.lstrip()[len(game.tag):].strip()
        if said.startswith(f"{game.title} not installed in "):
            found[game.key] = False
        elif said.startswith(f"{game.title} installed in "):
            found[game.key] = True
    return found


__all__ = ["SINCE", "Map", "Game", "GAMES", "NEW_GAMES", "MULTIPLAYER_GAMES", "HALO3_MAPS", "REACH_MAPS", "HALO1_MAPS",
           "HALO2_MAPS", "HALO3_GAME_TYPES", "game_of_engine", "game_of_map", "game_of_tag", "installed_in_text"]
