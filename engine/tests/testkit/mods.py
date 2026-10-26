"""Mods in a content folder, as the Workshop or a host leaves them."""

from __future__ import annotations

import json
import struct
from pathlib import Path

from testkit.mapfiles import SCENARIO, TAGS_FILE, UPLOADED, halo3_map

from huragok.content import modcheck


def a_map(map_id: int = 9001, kind: int = 1) -> bytes:
    data = halo3_map()
    struct.pack_into("<h", data, TAGS_FILE + SCENARIO, kind)
    struct.pack_into("<i", data, TAGS_FILE + SCENARIO + 0xC, map_id)
    return bytes(data)


def a_mod(folder: Path, maps: dict[str, bytes], scenarios: dict[str, str] | None = None, *, title: str = "Test Mod",
          engine: str = "Halo3", workshop: int | None = None, listed: list[str] | None = None,
          receipt: bool = True) -> Path:
    """A mod package: ModInfo.json, a multiplayer description per map (scenarios: description name -> its
    ScenarioFile; by default each map's own), the maps; in Mods/Workshop/<item>, the program's receipts."""
    (folder / "maps").mkdir(parents=True, exist_ok=True)
    (folder / "multiplayer").mkdir(exist_ok=True)
    for stem, data in maps.items():
        (folder / "maps" / f"{stem}.map").write_bytes(data)
    scenarios = {stem: stem for stem in maps} if scenarios is None else scenarios
    for name, scenario in scenarios.items():
        (folder / "multiplayer" / f"{name}.json").write_text(json.dumps(
            {"MapGuid": "6bd5cdf7-e40d-41d4-a464-57a1c1b3a256", "ScenarioFile": scenario, "Title": {"Neutral": name}}))
    identifier: dict[str, object] = {"ModGuid": "a6b35904-1342-460e-8789-e711ff47bb94"}
    if workshop:
        identifier["HostedModIds"] = {"SteamWorkshopId": workshop}
    contents = {"MultiplayerMaps": [f"multiplayer/{n}.json" for n in scenarios] if listed is None else listed}
    (folder / "ModInfo.json").write_text(json.dumps({"ModIdentifier": identifier, "Engine": engine,
                                                     "Title": {"Neutral": title}, "GameModContents": contents}))
    if folder.parent.name == "Workshop" and receipt:
        version = {"item": int(folder.name), "manifest": 1, "updated": UPLOADED}
        files = [[p.relative_to(folder).as_posix(), p.stat().st_size] for p in sorted(folder.rglob("*")) if p.is_file()]
        (folder / modcheck.INSTALLED).write_text(json.dumps({"item": int(folder.name), "files": files,
                                                             "version": version}))
        (folder / "reclaimer-workshop-version.json").write_text(json.dumps(version))
    return folder
