"""What a server plays, as its dedicated.toml [[server]] block says it: one kind of thing per server (a
playlist, one map and game type, a variant, Halo 3's campaign, ODST), and the words the dashboard says it in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .halo3 import MISSIONS
from .odst import console_refusal as odst_refusal
from .odst import describe as describe_odst


PLAYS_KEYS = ("playlist", "map", "game", "base_map", "variant", "campaign", "odst")  # one kind of these


CO_OP_MAX_PLAYERS = 16  # a campaign or ODST server holds no more: a higher max_players stops its start


def plays_kind(block: dict[str, Any]) -> str:
    """Which kind of thing a [[server]] block plays: playlist, campaign, odst, variant, map (one map and game type)
    or default (nothing set: Slayer on Guardian). One kind per server; this is the order the dashboard reads them."""
    for key in ("playlist", "odst", "campaign", "variant"):
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
    if "variant" in block:
        return f"variant {Path(str(block['variant'])).stem}"
    if "map" in block or "game" in block:
        return f"{block.get('game', 'Slayer')} on {block.get('map', 'Guardian')}"
    return "Slayer on Guardian"


def is_co_op(block: dict[str, Any]) -> bool:
    """A campaign or ODST server: co-op, CO_OP_MAX_PLAYERS at most, and no third-person camera."""
    return "campaign" in block or "odst" in block


def console_refusal(plays_kind: str, line: str, version: str | None = None) -> str | None:
    """Why the console won't take this line on a server that plays `plays_kind` and runs program `version` (None: it
    will). An ODST server before 0.9.21 plays what its file names: a command that changes the game (map, mode,
    load…) is refused, with what to do instead; from 0.9.21 the program answers for itself."""
    return odst_refusal(line, version) if plays_kind == "odst" else None

