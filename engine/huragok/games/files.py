"""The game files a server needs, by kind, and where MCC keeps them on Steam:

    halo3           Halo 3's multiplayer: halo3.dll, shared.map and the 27 maps (every server: from 0.10 every
                    other game's maps are built on Guardian, so Halo 3 is always needed)
    campaign        Halo 3's campaign: campaign.map and the 11 missions (a campaign server)
    odst            Halo 3: ODST: halo3odst.dll and its maps (an ODST server, beside Halo 3's)
    reach           Halo: Reach: haloreach.dll, its multiplayer and Firefight maps (0.10)
    reach-campaign  Halo: Reach's campaign missions too (0.10: "for a Reach co-op server")
    halo1           Halo: Combat Evolved: halo1.dll, its multiplayer maps, sounds and game types (0.10)
    halo2           Halo 2: halo2.dll, its multiplayer maps, sounds and game types (0.10)

The server program copies them from MCC (`dedicated init --from --maps <word>`) and checks them as it starts;
Huragok gets what a server lacks (content/gamefiles.py) and reads what the program says about them here.

What 0.10.0's `dedicated init --from <MCC> --maps <word>` puts in a game folder (probed 2026-11-06; each also brings
Halo 3's halo3.dll, shared.map, guardian.map, campaign.map and 040_voi.map, the 1.6 GB every game is built on):
- reach: haloreach/haloreach.dll, haloreach/maps/ (shared, campaign, m05, m70, the 30 multiplayer and Firefight
  maps, info/*.mapinfo): 64 files, 7.1 GB. The program counts Reach installed with its dll, shared.map, campaign.map
  and a multiplayer map.
- reach-campaign: the same and the 10 missions (m10 … m70_bonus, m70_a): 86 files, 11 GB.
- halo1: halo1/halo1.dll, halo1/maps/ (bitmaps, ui, the 19 maps), halo1/sound/pc (2.8 GB), shaders, game_variants,
  hopper_game_variants: 244 files, 3.3 GB. Installed: its dll and a multiplayer map.
- halo2: halo2/halo2.dll, halo2/h2_maps_win64_dx11/ (shared, the 25 maps, sounds_*.dat, textures.dat), h2_fonts,
  game_variants, hopper_game_variants: 297 files, 3.2 GB. Installed: its dll and a multiplayer map.
Nothing of MCC/Content/Paks (the text fonts) is copied: a copied game folder's server says "no text font package …
the game shows no text", which a server with no screen never shows.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bungie import GAMES, NEW_GAMES
from .halo3 import entry_game


ENGINE_MISMATCH = "Copy halo3.dll again"


ODST_ENGINE_MISMATCH = "halo3odst.dll is not the ODST build"


ODST_FILES_MISSING = "Halo 3: ODST's files are not in the game folder"


def engine_out_of_date(check_text: str) -> bool:
    return ENGINE_MISMATCH in check_text or ODST_ENGINE_MISMATCH in check_text


def odst_files_missing(check_text: str) -> bool:
    return ODST_FILES_MISSING in check_text


def engine_files(game_dir: Path) -> list[str]:
    """The engine files a game folder holds: halo3.dll, and each other game's (halo3odst.dll, haloreach.dll,
    halo1.dll, halo2.dll) when it has that game's folder."""
    files = ["halo3/halo3.dll"]
    for key in ("odst", "reach", "halo1", "halo2"):
        if (game_dir / GAMES[key].folder).is_dir():
            files.append(GAMES[key].engine)
    return files


def has_game_files(game: Path) -> bool:
    return (game / "halo3" / "halo3.dll").is_file() and (game / "halo3" / "maps" / "shared.map").is_file()


def has_odst_files(game: Path) -> bool:
    """Halo 3: ODST's engine and maps, which an ODST server needs beside Halo 3's (0.9.19)."""
    return (game / "halo3odst" / "halo3odst.dll").is_file() and (game / "halo3odst" / "maps" / "shared.map").is_file()


def _any_map(folder: Path, key: str) -> bool:
    """Whether `folder` holds one of the game's stock multiplayer maps (its file: "bloodgulch.map" for
    halo1_bloodgulch)."""
    return any((folder / f"{map_.stem.removeprefix(key + '_')}.map").is_file() for map_ in GAMES[key].maps)


def has_reach_files(game: Path) -> bool:
    """Halo: Reach's engine, shared and campaign maps (the program counts Reach installed only with campaign.map)
    and a multiplayer map: what `--maps reach` brings (0.10)."""
    maps = game / "haloreach" / "maps"
    return (game / "haloreach" / "haloreach.dll").is_file() and all(
        (maps / f"{name}.map").is_file() for name in ("shared", "campaign")) and _any_map(maps, "reach")


REACH_MISSIONS = ("m10", "m20", "m30", "m35", "m45", "m50", "m52", "m60", "m70_bonus", "m70_a")  # m05, m70: `reach`


def has_reach_campaign(game: Path) -> bool:
    maps = game / "haloreach" / "maps"
    return has_reach_files(game) and all((maps / f"{name}.map").is_file() for name in ("m10", "m60"))


def has_halo1_files(game: Path) -> bool:
    """Halo: Combat Evolved's engine, shared bitmaps and a multiplayer map (0.10)."""
    maps = game / "halo1" / "maps"
    return (game / "halo1" / "halo1.dll").is_file() and (maps / "bitmaps.map").is_file() and _any_map(maps, "halo1")


def has_halo2_files(game: Path) -> bool:
    """Halo 2's engine, shared map and a multiplayer map: MCC keeps its maps in h2_maps_win64_dx11 (0.10)."""
    maps = game / "halo2" / "h2_maps_win64_dx11"
    return (game / "halo2" / "halo2.dll").is_file() and (maps / "shared.map").is_file() and _any_map(maps, "halo2")


MISSING_FILES = "is missing files copied from your own MCC installation"


CAMPAIGN_MISSIONS = ("005_intro", "010_jungle", "020_base", "030_outskirts", "040_voi", "050_floodvoi", "070_waste",
                     "100_citadel", "110_hc", "120_halo", "130_epilogue")


# Every kind, in the order they're got and shown: Halo 3's first (every other goes beside it).
KINDS = ("halo3", "campaign", "odst", "reach", "reach-campaign", "halo1", "halo2")


LABELS = {"halo3": "Halo 3's files", "campaign": "Halo 3's campaign missions", "odst": "Halo 3: ODST's files",
          "reach": "Halo: Reach's files", "reach-campaign": "Halo: Reach's campaign missions",
          "halo1": "Halo: Combat Evolved's files", "halo2": "Halo 2's files"}


MAPS_FOR = {"halo3": None, "campaign": ["campaign"], "odst": ["odst"],  # `dedicated init --from --maps …`
            "reach": ["reach"], "reach-campaign": ["reach-campaign"], "halo1": ["halo1"], "halo2": ["halo2"]}


GAME_OF_KIND = {"halo3": "halo3", "campaign": "halo3", "odst": "odst", "reach": "reach", "reach-campaign": "reach",
                "halo1": "halo1", "halo2": "halo2"}  # the bungie.GAMES key each kind is part of


SIZES = {"halo3": 5.9e9, "campaign": 4.0e9, "odst": 3.6e9, "reach": 7.1e9, "reach-campaign": 11e9, "halo1": 3.3e9,
         "halo2": 3.2e9}  # bytes `dedicated init` copies (0.10.0, MCC of 2026-11): hard links on the same drive


IN_MCC = {"halo3": "halo3/halo3.dll", "campaign": "halo3/maps/010_jungle.map", "odst": "halo3odst/halo3odst.dll",
          "reach": "haloreach/haloreach.dll", "reach-campaign": "haloreach/maps/m10.map", "halo1": "halo1/halo1.dll",
          "halo2": "halo2/halo2.dll"}  # the file that says MCC has `kind` installed


def in_mcc(mcc: Path | None, kind: str) -> bool:
    """Whether MCC's folder has `kind` installed, and Halo 3 (`dedicated init` copies it with every game)."""
    return mcc is not None and all((mcc / name).is_file() for name in {IN_MCC["halo3"], IN_MCC[kind]})


def plays_on(kind: str, version: str | None) -> bool:
    """Whether program `version` plays this kind's game (None, unknown: it does)."""
    return GAMES[GAME_OF_KIND[kind]].plays_on(version)


def kinds_for(version: str | None) -> list[str]:
    """The kinds program `version` can use (Reach, CE and Halo 2 from 0.10, ODST from 0.9.19)."""
    return [kind for kind in KINDS if plays_on(kind, version)]


@dataclass(frozen=True)
class Depot:
    """A piece of MCC on Steam that a server needs files from (MCC's app is 976730)."""

    kind: str
    depot: int
    download: float  # bytes SteamCMD downloads
    kept: float  # bytes kept: the rest is sound and video, which servers never play
    holds: str


# Only the depots checked by hand: Reach's, CE's and Halo 2's aren't known yet, so their files come from MCC alone.
DEPOTS = {
    "halo3": Depot("halo3", 976739, 9.33e9, 5.75e9, "Halo 3 multiplayer: halo3.dll, shared.map and the 27 maps"),
    "campaign": Depot("campaign", 976738, 9.01e9, 5.42e9, "Halo 3's campaign: campaign.map and the 11 missions"),
    "odst": Depot("odst", 1064222, 8.73e9, 3.49e9, "Halo 3: ODST: halo3odst.dll and its maps"),
}


HOLDS = {
    **{kind: depot.holds for kind, depot in DEPOTS.items()},
    "reach": "Halo: Reach: haloreach.dll, its 21 multiplayer and 9 Firefight maps",
    "reach-campaign": "Halo: Reach's campaign: its 12 missions, for a Reach co-op server",
    "halo1": "Halo: Combat Evolved: halo1.dll, its 19 multiplayer maps, sounds and game types",
    "halo2": "Halo 2: halo2.dll, its 25 multiplayer maps, sounds and game types",
}


SKIPPED = {"fmod", "waves", "bink"}  # sound and video: a server plays neither


def has_campaign(game: Path) -> bool:
    maps = game / "halo3" / "maps"
    return all((maps / f"{name}.map").is_file() for name in ("campaign", "010_jungle", "040_voi"))


NO_MAP = re.compile(r'no multiplayer map "([^"]+)" is installed')  # 0.10's word for a map its game folder lacks
REACH_NEEDS_CAMPAIGN = "Halo 3's campaign is needed to host a game of Reach's"  # `--maps reach` brings it


def kinds_in_failure(lines: list[str]) -> list[str]:
    """What a start that failed for missing game files says is missing: up to 0.9 the program lists the files under
    its error; 0.10 says `no multiplayer map "Sword Base" is installed (this game folder has no Halo: Reach, …)`,
    and the map says which game's files to get. A map that's no game's own (a mod's, a typo) gets nothing: Halo 3's
    files are never got for another game's map."""
    text = "\n".join(lines)
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    if MISSING_FILES in text:
        listed = text.split(MISSING_FILES, 1)[1]
        if re.search(r"halo3/(halo3\.dll|maps/shared\.map)|multiplayer map", listed):
            add("halo3")
        if "halo3odst/" in listed:
            add("odst")
        if any(f"/{name}.map" in listed for name in (*CAMPAIGN_MISSIONS, "campaign")):
            add("campaign")
        if any(f"haloreach/maps/{name}.map" in listed for name in REACH_MISSIONS):
            add("reach-campaign")
        elif "haloreach/" in listed:
            add("reach")
        for key in ("halo1", "halo2"):
            if f"{key}/" in listed:
                add(key)
        if not kinds:
            add("halo3")
    for match in NO_MAP.finditer(text):
        game = entry_game({"map": match.group(1)})
        if game is not None:
            add(game.key)
    if REACH_NEEDS_CAMPAIGN in text:
        add("reach")
    return kinds


def needed(plays_kind: str) -> list[str]:
    """The kinds of game files a server needs for what it plays: Halo 3's always, first."""
    return ["halo3", *(["odst"] if plays_kind == "odst" else []), *(["campaign"] if plays_kind == "campaign" else [])]


def _map_kinds(entry: dict[str, Any]) -> list[str]:
    """The kinds a [[server]] block or a playlist game needs for the maps it names (Halo 3's own: none more)."""
    kinds = []
    if "campaign" in entry:
        kinds.append("campaign")
    if "odst" in entry:
        kinds.append("odst")
    for key in ("map", "base_map"):
        game = entry_game({"map": entry.get(key)})
        if game is not None and game.key in NEW_GAMES:
            kinds.append(game.key)
    return kinds


def playlist_kinds(path: Path) -> list[str]:
    """The kinds a playlist file's games need beyond Halo 3's ([] when it can't be read: the program says why)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    games = data.get("games") if isinstance(data, dict) else None
    return [kind for game in (games if isinstance(games, list) else []) if isinstance(game, dict)
            for kind in _map_kinds(game)]


def needed_by(block: dict[str, Any], folder: Path | None = None, version: str | None = None) -> list[str]:
    """The kinds of game files a [[server]] block needs for what it plays: Halo 3's always, first, then each other
    game its `map`, `odst`, `campaign` or playlist names (the playlist file read from `folder`, the server's). On
    program `version` (None: unknown) only what it can play: a 0.9 program plays no Reach map, so none is needed."""
    from .plays import plays_kind

    kind = plays_kind(block)
    found = needed(kind)
    if kind == "playlist" and folder is not None and isinstance(block.get("playlist"), str):
        found += playlist_kinds(folder / block["playlist"])
    elif kind in ("map", "variant"):
        found += _map_kinds(block)
    elif kind == "reach":  # 0.10's undocumented `reach = { … }` (`--maps reach-campaign`: "for a Reach co-op server")
        setup = block["reach"] if isinstance(block["reach"], dict) else {}
        found += ["reach", *(["reach-campaign"] if setup.get("campaign") else [])]
    return [k for k in KINDS if k in found and plays_on(k, version)]


HAS = {"halo3": has_game_files, "campaign": has_campaign, "odst": has_odst_files, "reach": has_reach_files,
       "reach-campaign": has_reach_campaign, "halo1": has_halo1_files, "halo2": has_halo2_files}


# Reach's own game and map variants (MCC's and its matchmaking's, 750 small files, 20 MB): what `game =` names Reach's
# game types by (`headhunter_054`). 0.10.0's `dedicated init --maps reach` copies its maps but not these, and a game
# folder without them lists none of Reach's variants (probed 6 November 2026: 0, and 429 with them).
REACH_VARIANTS = ("game_variants", "hopper_game_variants", "map_variants", "hopper_map_variants")


def add_reach_variants(game: Path, mcc: Path) -> int:
    """Reach's variants from MCC's haloreach folder into game/haloreach, beside what's there (nothing replaced): hard
    links on the same drive, else copies. How many files were added; 0 when MCC has none or the folder has no Reach."""
    source, target = mcc / "haloreach", game / "haloreach"
    if not source.is_dir() or not target.is_dir():
        return 0
    added = 0
    for folder in REACH_VARIANTS:
        if not (source / folder).is_dir():
            continue
        (target / folder).mkdir(exist_ok=True)
        for file in (source / folder).iterdir():
            into = target / folder / file.name
            if not file.is_file() or into.exists():
                continue
            try:
                os.link(file, into)
            except OSError:
                shutil.copy2(file, into)
            added += 1
    return added


def has(game: Path, kind: str) -> bool:
    return HAS[kind](game)


def lacking(game: Path, kinds: list[str]) -> list[str]:
    """What a game folder lacks of `kinds`: Halo 3's files first (the others go beside them), so with none of Halo
    3's, only those."""
    if not has_game_files(game):
        return ["halo3"]
    return [kind for kind in kinds if kind != "halo3" and not has(game, kind)]


def missing(game: Path, plays: str | list[str]) -> list[str]:
    """What a game folder lacks for what its server plays: a plays kind ("odst"), or the kinds it needs
    (needed_by). Halo 3's files first (the others go beside them), so with none of Halo 3's, only those."""
    return lacking(game, needed(plays) if isinstance(plays, str) else plays)


def present(game: Path, version: str | None = None) -> dict[str, bool]:
    """Each kind program `version` can use: whether the game folder has it."""
    return {kind: has(game, kind) for kind in kinds_for(version)}
