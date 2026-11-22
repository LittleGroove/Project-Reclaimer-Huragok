"""Workshop mods as the files have them (0.7.0): what nothing keeps up to date, and what an update left behind, said as
warnings, with nothing written into a server's folder; and a mod's own Forge layouts copied into content/Maps when the
host asks, the server's version-2 rewrite of one not taken for a hand edit (bug B2)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import free_port
from testkit.mods import a_map, a_mod
from testkit.setup import make_server_folder, setup_root
from testkit.workshop import ALPHA, ITEM, FakeLifecycle, service_for, workshop_root

from huragok.content.catalogue import SET_ASIDE, installed_mods, layout_hashes, sync_layouts
from huragok.content import workshopmods
from huragok.content.workshopmods import RECORD, WorkshopFiles, layouts_by_hand, look_through, steam_folder_mods
from huragok.core.serverwarnings import Warnings, load_rules
from huragok.core.settings import load
from huragok.core.tomledit import set_table_key, set_top_key
from huragok.steam import MCC_APP

TITLE = "HugeGrass Rebalance"


def hashes_of(folder: Path) -> dict[str, str]:
    """Every file under the folder, by its content: what "nothing written" is checked against."""
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def files_for(settings_path: Path, steam: Path | None = None, updates=None) -> WorkshopFiles:  # type: ignore[no-untyped-def]
    settings = load(settings_path)
    return WorkshopFiles(settings, Warnings(load_rules(), settings.records), updates, steam_folder=lambda: steam)


def found_rules(files: WorkshopFiles, server: str = ALPHA) -> dict[str, dict]:  # type: ignore[type-arg]
    files.say(files.look())
    assert files.warnings is not None
    return {f"{w.rule.id}:{w.key}" if w.key else w.rule.id: {"text": w.line(), "rule": w.rule}
            for w in files.warnings.of(server)}


# What nothing keeps up to date (answer 18) ------------------------------------------------------------------------

def test_a_hand_copy_not_in_items_is_warned_about_and_left_as_it_is(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    server = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    a_mod(server / "content" / "Mods" / "Albatross Hand", {"shrine_albatross_rc": a_map()}, title="Albatross Battle",
          workshop=2898980746)
    before = hashes_of(server)
    files = files_for(settings_path)
    found = found_rules(files)
    warning = found["mod-hand-copy:2898980746:Mods/Albatross Hand"]
    assert warning["text"] == (
        "Check: This copy of Albatross Battle names Workshop item 2898980746 but isn't in this server's [workshop] "
        "items, so it never updates (Mods/Albatross Hand). Add the item to items and take the copy out: the server "
        "fetches and updates listed mods itself (its own check does it too).")
    assert not warning["rule"].discord
    assert hashes_of(server) == before  # nothing taken over, listed or moved
    set_table_key(server / "dedicated.toml", "workshop", "items", [2898980746], before="master")  # the host lists it
    files.settings = load(settings_path)
    assert "mod-hand-copy:2898980746:Mods/Albatross Hand" not in found_rules(files)


def test_the_programs_copy_of_an_item_no_longer_listed(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    a_mod(folder / "content" / "Mods" / "Workshop" / str(ITEM), {f"fake{ITEM}": a_map()}, title=TITLE)  # receipts too
    found = found_rules(files_for(settings_path))
    assert found[f"mod-unlisted:{ITEM}"]["text"] == (
        f"Check: Mods/Workshop/{ITEM} ({TITLE}) was installed by the server program, but Workshop item {ITEM} isn't "
        "in [workshop] items any more: the server still loads it and never updates it. Add the item back to items to "
        "keep it up to date, or move the folder out if it isn't wanted.")
    assert read_items(folder) == []  # never listed again by Huragok


def read_items(folder: Path) -> list[int]:
    from huragok.core.tomledit import read_toml

    return read_toml(folder / "dedicated.toml").get("workshop", {}).get("items", [])


def test_a_hand_copy_listed_by_another_server_on_the_folder_is_kept_up_to_date_by_it(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    alpha = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    a_mod(alpha / "content" / "Mods" / "Workshop" / str(ITEM), {f"fake{ITEM}": a_map()}, title=TITLE)
    beta = tmp_path / "reclaimer" / "SERVERS" / "Beta"
    make_server_folder(beta, "Beta", free_port())
    set_top_key(beta / "dedicated.toml", "content", f"../{ALPHA}/content")
    set_table_key(beta / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    files = files_for(settings_path)
    assert not [key for key in found_rules(files) if key.startswith("mod-")]
    assert not [key for key in found_rules(files, "Beta") if key.startswith("mod-")]


def test_a_switched_off_mod_and_other_games_hand_copies_are_left_out(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    content = tmp_path / "reclaimer" / "SERVERS" / ALPHA / "content"
    a_mod(content / "Mods" / "Off", {"off": a_map()}, title="Off", workshop=111111111)
    a_mod(content / "Mods" / "Reach", {"forge": a_map()}, title="Reach Mod", engine="HaloReach", workshop=222222222)
    (content / "disabled-mods.json").write_text(json.dumps({"packages": ["Mods/Off"]}), encoding="utf-8")
    assert not [k for k in found_rules(files_for(settings_path)) if k.startswith("mod-hand-copy")]
    assert look_through(content).hand == {}


# What an update left behind (answers 16 and 20) -----------------------------------------------------------------

def test_a_leftover_copy_of_a_dead_server_is_warned_about_with_its_size(tmp_path: Path, monkeypatch) -> None:
    settings_path, folder = workshop_root(tmp_path)
    content = folder / "content"
    dead = content / f".workshop-{ITEM}-6832-17"
    (dead / "maps").mkdir(parents=True)
    (dead / "maps" / "shrine.map").write_bytes(b"x" * 3000)
    live = content / f".workshop-{ITEM}-{os.getpid()}-1"  # a running process's: an update under way
    live.mkdir()
    monkeypatch.setattr(workshopmods, "program_running", lambda pid: pid == os.getpid())
    found = found_rules(files_for(settings_path))
    leftovers = {key: value for key, value in found.items() if key.startswith("mod-leftover")}
    assert list(leftovers) == [f"mod-leftover:.workshop-{ITEM}-6832-17"]
    text = leftovers[f"mod-leftover:.workshop-{ITEM}-6832-17"]["text"]
    assert "(process 6832) is no longer running" in text and "3 KB" in text and "Delete the folder" in text
    assert dead.is_dir()  # warned about only (answer 16)


def test_a_program_running_is_told_by_its_name(monkeypatch) -> None:
    assert not workshopmods.program_running(os.getpid())  # this is Python, not the server program
    assert not workshopmods.program_running(2 ** 22 + 12345)


def test_the_download_kept_beside_an_installed_update_is_said_with_its_size(tmp_path: Path, run) -> None:
    settings_path, folder = workshop_root(tmp_path)
    updates = service_for(settings_path, FakeLifecycle())
    updates.titles[ITEM] = TITLE
    cache = folder / "data" / "workshop-cache" / "steamapps" / "workshop" / "content" / str(MCC_APP) / str(ITEM)
    (cache / "maps").mkdir(parents=True)
    (cache / "maps" / "fake.map").write_bytes(b"x" * 2048)
    files = files_for(settings_path, updates=updates)
    assert not [k for k in found_rules(files) if k.startswith("mod-download-kept")]  # only after an install

    async def install() -> None:
        updates.bus.emit("server.workshop", ALPHA, item=str(ITEM), text="updated to the version of 18 Oct 2026 "
                                                                         "between games; 1 server(s) play it")

    run(install())
    found = found_rules(files)
    text = found[f"mod-download-kept:{ITEM}"]["text"]
    assert text.startswith(f"Info: {TITLE}'s download stays in data/workshop-cache/steamapps/workshop/content/976730/"
                           f"{ITEM} (2 KB) beside the installed copy")
    for path in sorted(cache.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    cache.rmdir()
    assert not [k for k in found_rules(files) if k.startswith("mod-download-kept")]


# This PC's own Steam Workshop folder ------------------------------------------------------------------------------

def test_mods_only_in_this_pcs_steam_folder_are_said_for_information(tmp_path: Path) -> None:
    settings_path, folder = workshop_root(tmp_path)
    steam = tmp_path / "Steam" / "steamapps" / "workshop" / "content" / str(MCC_APP)
    a_mod(steam / str(ITEM), {"x": a_map()}, title="Installed Here Too")  # the server has it: not "only"
    a_mod(steam / "2898916793", {"y": a_map()}, title="Fonts Pack")
    (steam / "3830420359").mkdir(parents=True)  # no ModInfo.json: named by its number
    assert steam_folder_mods(steam) == {2898916793: "Fonts Pack", 3830420359: "Workshop item 3830420359",
                                        ITEM: "Installed Here Too"}
    found = found_rules(files_for(settings_path, steam))
    text = found["mod-steam-folder"]["text"]
    assert text.startswith("Info: Loads 2 mod(s) found only in this PC's own Steam Workshop folder (")
    assert text.endswith("): Fonts Pack, Workshop item 3830420359. Steam updates or removes those on its own schedule; "
                         "to keep one, list its item in [workshop] items.")
    assert "mod-steam-folder" not in found_rules(files_for(settings_path, None))


def test_the_steam_folder_is_found_from_steams_own_records(tmp_path: Path, monkeypatch) -> None:
    from huragok import steam as steamsetup

    library = tmp_path / "Library"
    folder = library / "steamapps" / "workshop" / "content" / str(MCC_APP)
    folder.mkdir(parents=True)
    monkeypatch.setattr(steamsetup, "steam_libraries", lambda: iter([(tmp_path / "Other", {"440": "1"}),
                                                                     (library, {str(MCC_APP): "1"})]))
    assert steamsetup.workshop_content() == folder
    monkeypatch.setattr(steamsetup, "steam_libraries", lambda: iter([]))
    assert steamsetup.workshop_content() is None


# Layouts, only when the host asks (S3), and bug B2 --------------------------------------------------------------

def layout(path: Path, name: str, body: str = "v1", version: int = 1) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    target = path / f"{name}.json"
    target.write_text(json.dumps({"version": version, "name": name, "objects": body}, indent=2), encoding="utf-8")
    return target


def rewrite_as_version_two(path: Path) -> None:
    """What a 0.10 server does at its start: only the digit changes, the modified time kept."""
    stat = path.stat()
    path.write_bytes(path.read_bytes().replace(b'"version": 1', b'"version": 2', 1))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


def test_the_servers_version_two_rewrite_is_still_the_mods_layout(tmp_path: Path) -> None:
    copied = layout(tmp_path, "Balanced")
    original = layout_hashes(copied)
    rewrite_as_version_two(copied)
    assert original <= layout_hashes(copied)  # the rewrite reads as the file Huragok copied
    assert not layout_hashes(layout(tmp_path / "other", "Balanced", "an edit")) & original


def test_layouts_are_copied_when_asked_and_a_rewritten_one_still_gets_the_mods_next_version(tmp_path: Path) -> None:
    content = tmp_path / "content"
    mod_folder = a_mod(content / "Mods" / "HugeGrass", {"hugegrass": a_map()}, title="HugeGrass")
    shipped = mod_folder / "Project Reclaimer Layouts"
    for name in ("Balanced", "Spirits", "Mine", "Gone"):
        layout(shipped, name)
    maps = content / "Maps" / "HugeGrass"
    layout(maps, "Mine", "made with L long ago")  # there before Huragok kept track, and different
    written: dict[str, str] = {}
    result = sync_layouts(installed_mods(content)[0], content, written)
    assert sorted(result.copied) == ["Maps/HugeGrass/Balanced.json", "Maps/HugeGrass/Gone.json",
                                     "Maps/HugeGrass/Spirits.json"]
    assert result.replaced == ["Maps/HugeGrass/Mine.json"]
    assert (tmp_path / SET_ASIDE / "Maps" / "HugeGrass" / "Mine.json").read_text(encoding="utf-8").count("long ago")
    # The server starts and rewrites every copy as version 2 (bug B2: these read as hand edits before)
    for name in ("Balanced", "Spirits", "Gone", "Mine"):
        rewrite_as_version_two(maps / f"{name}.json")
    assert not sync_layouts(installed_mods(content)[0], content, written).changed  # the same layouts: nothing to do
    # The mod's next version; meanwhile someone changed Spirits here and deleted Gone
    for name in ("Balanced", "Spirits", "Gone"):
        layout(shipped, name, "v2")
    layout(maps, "Spirits", "my own edit", version=2)
    (maps / "Gone.json").unlink()
    result = sync_layouts(installed_mods(content)[0], content, written)
    assert result.updated == ["Maps/HugeGrass/Balanced.json"] and result.kept == ["Maps/HugeGrass/Spirits.json"]
    assert result.copied == ["Maps/HugeGrass/Gone.json"]  # asked for again: the deleted one comes back
    assert "v2" in (maps / "Balanced.json").read_text(encoding="utf-8")
    assert "my own edit" in (maps / "Spirits.json").read_text(encoding="utf-8")
    assert not sync_layouts(installed_mods(content)[0], content, written).changed


def test_the_windows_l_remembers_what_it_copied(tmp_path: Path) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    mod_folder = a_mod(folder / "content" / "Mods" / "HugeGrass", {"hugegrass": a_map()}, title="HugeGrass")
    layout(mod_folder / "Project Reclaimer Layouts", "Balanced")
    settings = load(settings_path)
    assert layouts_by_hand(settings, folder / "content", installed_mods(folder / "content")[0]) == \
        ["Maps/HugeGrass/Balanced.json"]
    rewrite_as_version_two(folder / "content" / "Maps" / "HugeGrass" / "Balanced.json")
    layout(mod_folder / "Project Reclaimer Layouts", "Balanced", "v2")
    assert layouts_by_hand(settings, folder / "content", installed_mods(folder / "content")[0]) == \
        ["Maps/HugeGrass/Balanced.json"]  # its next version over the server's rewrite of the last
    assert "v2" in (folder / "content" / "Maps" / "HugeGrass" / "Balanced.json").read_text(encoding="utf-8")
    assert json.loads((settings.records / RECORD).read_text(encoding="utf-8"))["layouts"]


@pytest.mark.parametrize("written", ['{"version":2,"name":"a"}', '{\n  "version": 2.5,\n  "name": "a"\n}'])
def test_only_a_whole_version_two_is_read_as_the_rewrite(tmp_path: Path, written: str) -> None:
    path = tmp_path / "a.json"
    path.write_text(written, encoding="utf-8")
    forms = layout_hashes(path)
    assert len(forms) == (2 if "2.5" not in written else 1)
