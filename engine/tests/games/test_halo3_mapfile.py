"""Why the server skipped a mod map, from the map itself (mapcheck.py). On 19 October 2026 the host's LG Test Server
skipped a new Workshop upload of HugeGrass Rebalance with only "its tag index is damaged" to go on (the author
replaced it 45 minutes later); 0.9.21's checks behind that reason are made here on small maps built to fail each."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testkit.mapfiles import UPLOADED, broken, halo3_map, written

from huragok.games import halo3_mapfile as mapcheck
from huragok.games.halo3_mapfile import CUT_SHORT, DAMAGED_INDEX, NO_SCENARIO, NOT_HALO3, check, explain


def test_a_sound_map_passes_every_check(tmp_path: Path) -> None:
    verdict = check(written(tmp_path, halo3_map()))
    assert verdict == mapcheck.Verdict("", "3 tags, and its scenario reads", "Test Map", map_type=1, map_id=0)


@pytest.mark.parametrize(("change", "reason", "words"), [
    ("short", CUT_SHORT, "the file is only 4,096 bytes, less than a map's 16 KB header"),
    ("cut", CUT_SHORT, "its header says it is 20,736 bytes, so the file was cut short"),
    ("zip", NOT_HALO3, "its first four bytes are 50 4b 03 04, where a map has 'daeh'"),
    ("xbox", NOT_HALO3, "it is an Xbox 360 map"),
    ("version", NOT_HALO3, "it is a version 12 map; the server takes version 13"),
    ("foot", NOT_HALO3, "doesn't end with 'foot'"),
    ("empty", DAMAGED_INDEX, "its tags section is empty"),
    ("index low", DAMAGED_INDEX, "its tag index header is at 0x1ca19f000, before the map's tag data, which starts at "
                                 "0x1ca1a0000"),
    ("index high", DAMAGED_INDEX, "its tag index header starts at 0x1ca1a1040, past the end of the map's tag data at "
                                  "0x1ca1a1000"),
    ("tags", DAMAGED_INDEX, "its tag index lists 40,000 tags (the server takes at most 32,768)"),
    ("groups", DAMAGED_INDEX, "2,000 tag groups (at most 1,024)"),
    ("table", DAMAGED_INDEX, "its tag table (600 tags) runs to 0x1ca1a12c0, past the end of the map's tag data"),
    ("blank", DAMAGED_INDEX, "its tag index header is all zeros (the tag data is missing), so its tag group table is "
                             "at 0x0"),
    ("no scnr group", NO_SCENARIO, "none of its 3 tag groups is a scenario ('scnr')"),
    ("no scnr tag", NO_SCENARIO, "none of its 3 tags is a scenario ('scnr')"),
    ("scenario", DAMAGED_INDEX, "its scenario tag (tag 1, levels\\multi\\test\\test) is at 0x0, before the map's tag "
                                "data"),
])
def test_each_fault_is_named_with_the_reason_the_server_gives(tmp_path: Path, change: str, reason: str,
                                                              words: str) -> None:
    verdict = check(written(tmp_path, broken(change)))
    assert verdict.reason == reason
    assert words in verdict.detail


def test_the_explanation_says_which_upload_and_what_to_do(tmp_path: Path) -> None:
    item = tmp_path / "content" / "Mods" / "Workshop" / "3820687243"
    path = written(item / "maps", broken("table"), "fixedHugeGrassTrapasRemix.map")
    (item / mapcheck.RECEIPT).write_text(json.dumps({"item": 3820687243, "manifest": 1, "updated": UPLOADED}))
    why = explain(path, DAMAGED_INDEX)
    assert why.startswith("its tag table (600 tags) runs to 0x1ca1a12c0, past the end of the map's tag data")
    assert ("ask its author (Workshop 3820687243, the version uploaded 20 Oct 2026 05:57 UTC) for a fixed upload; "
            "Huragok installs the fixed version when it comes out") in why
    assert "installs the fixed version" not in explain(path, DAMAGED_INDEX, updates=False)
    # what the server said and what Huragok finds differ: the file changed after the server looked
    assert "(the server said 'it has no scenario': the file has changed since)" in explain(path, NO_SCENARIO)
    # a reason about the item's packing, not the map file: the map isn't read
    assert explain(path, "Map needs matching .mapinfo metadata") == (
        "the item's own packaging (Workshop 3820687243, the version uploaded 20 Oct 2026 05:57 UTC); ask its author")
    path.write_bytes(bytes(halo3_map()))  # the author's fix arrived after the server looked
    assert explain(path, DAMAGED_INDEX).startswith("Huragok finds nothing wrong with the file there now (3 tags")
    path.write_bytes(bytes(broken("cut")))
    assert "Fetch or copy it again; if it comes back the same, the upload itself is short" in explain(path, CUT_SHORT)
    loose = written(tmp_path / "Mods" / "mine", broken("version"))  # copied in by hand: no Workshop item to name
    assert explain(loose, NOT_HALO3).endswith("The map file itself is broken, not your setup: ask its author for a "
                                              "fixed upload")
    assert explain(tmp_path / "gone.map", DAMAGED_INDEX).startswith("Huragok couldn't read the file to see more")
