"""The game files a server needs, by kind, and where MCC keeps them on Steam:

    halo3      Halo 3's multiplayer: halo3.dll, shared.map and the 27 maps (every server)
    campaign   Halo 3's campaign: campaign.map and the 11 missions (a campaign server)
    odst       Halo 3: ODST: halo3odst.dll and its maps (an ODST server, beside Halo 3's)

The server program copies them from MCC (`dedicated init --from`) and checks them as it starts; Huragok gets what a
server lacks (content/gamefiles.py) and reads what the program says about them here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


ENGINE_MISMATCH = "Copy halo3.dll again"


ODST_ENGINE_MISMATCH = "halo3odst.dll is not the ODST build"


ODST_FILES_MISSING = "Halo 3: ODST's files are not in the game folder"


def engine_out_of_date(check_text: str) -> bool:
    return ENGINE_MISMATCH in check_text or ODST_ENGINE_MISMATCH in check_text


def odst_files_missing(check_text: str) -> bool:
    return ODST_FILES_MISSING in check_text


def engine_files(game_dir: Path) -> list[str]:
    """The engine files a game folder holds: halo3.dll, and halo3odst.dll when it has Halo 3: ODST's files."""
    files = ["halo3/halo3.dll"]
    if (game_dir / "halo3odst").is_dir():
        files.append("halo3odst/halo3odst.dll")
    return files


def has_game_files(game: Path) -> bool:
    return (game / "halo3" / "halo3.dll").is_file() and (game / "halo3" / "maps" / "shared.map").is_file()


def has_odst_files(game: Path) -> bool:
    """Halo 3: ODST's engine and maps, which an ODST server needs beside Halo 3's (0.9.19)."""
    return (game / "halo3odst" / "halo3odst.dll").is_file() and (game / "halo3odst" / "maps" / "shared.map").is_file()


MISSING_FILES = "is missing files copied from your own MCC installation"


CAMPAIGN_MISSIONS = ("005_intro", "010_jungle", "020_base", "030_outskirts", "040_voi", "050_floodvoi", "070_waste",
                     "100_citadel", "110_hc", "120_halo", "130_epilogue")


LABELS = {"halo3": "Halo 3's files", "campaign": "Halo 3's campaign missions", "odst": "Halo 3: ODST's files"}


MAPS_FOR = {"halo3": None, "campaign": ["campaign"], "odst": ["odst"]}  # `dedicated init --from --maps …`


@dataclass(frozen=True)
class Depot:
    """A piece of MCC on Steam that a server needs files from (MCC's app is 976730)."""

    kind: str
    depot: int
    download: float  # bytes SteamCMD downloads
    kept: float  # bytes kept: the rest is sound and video, which servers never play
    holds: str


DEPOTS = {
    "halo3": Depot("halo3", 976739, 9.33e9, 5.75e9, "Halo 3 multiplayer: halo3.dll, shared.map and the 27 maps"),
    "campaign": Depot("campaign", 976738, 9.01e9, 5.42e9, "Halo 3's campaign: campaign.map and the 11 missions"),
    "odst": Depot("odst", 1064222, 8.73e9, 3.49e9, "Halo 3: ODST: halo3odst.dll and its maps"),
}


SKIPPED = {"fmod", "waves", "bink"}  # sound and video: a server plays neither


def has_campaign(game: Path) -> bool:
    maps = game / "halo3" / "maps"
    return all((maps / f"{name}.map").is_file() for name in ("campaign", "010_jungle", "040_voi"))


def kinds_in_failure(lines: list[str]) -> list[str]:
    """What a start that failed for missing game files says is missing (the program lists them under its error)."""
    text = "\n".join(lines)
    if MISSING_FILES not in text:
        return []
    listed = text.split(MISSING_FILES, 1)[1]
    kinds = []
    if re.search(r"halo3/(halo3\.dll|maps/shared\.map)|multiplayer map", listed):
        kinds.append("halo3")
    if "halo3odst/" in listed:
        kinds.append("odst")
    if any(f"/{name}.map" in listed for name in (*CAMPAIGN_MISSIONS, "campaign")):
        kinds.append("campaign")
    return kinds or ["halo3"]


def needed(plays_kind: str) -> list[str]:
    """The kinds of game files a server needs for what it plays: Halo 3's always, first."""
    return ["halo3", *(["odst"] if plays_kind == "odst" else []), *(["campaign"] if plays_kind == "campaign" else [])]


def has(game: Path, kind: str) -> bool:
    return {"halo3": has_game_files, "campaign": has_campaign, "odst": has_odst_files}[kind](game)


def missing(game: Path, plays_kind: str) -> list[str]:
    """What a game folder lacks for what its server plays: Halo 3's files first (the others go beside them), so
    with none of Halo 3's, only those."""
    if not has_game_files(game):
        return ["halo3"]
    return [kind for kind in needed(plays_kind)[1:] if not has(game, kind)]
