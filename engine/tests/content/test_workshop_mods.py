"""Every Workshop mod a server loads kept up to date: hand copies taken over, the program's unlisted copies listed
again, new items fetched at once, and the Forge layouts mods ship kept in step with them."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from conftest import free_port
from testkit.mods import a_map, a_mod
from testkit.setup import make_server_folder, setup_root
from testkit.workshop import ALPHA, ITEM, NEW, OLD, FakeLifecycle, service_for

from huragok.content.catalogue import SET_ASIDE, installed_mods, sync_layouts
from huragok.content.workshopmods import RECORD, STAGING, WorkshopKeeper, layouts_by_hand, look_through, \
    switch_workshop_copies
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_table_key, set_top_key
from huragok.steam import MCC_APP, SteamItem
from huragok.updates.workshop import installed_version

TITLE = "HugeGrass Rebalance"


def root_with_hand_copy(tmp_path: Path, *, steam: int = NEW, maps: dict[str, bytes] | None = None,
                        folder: str = "HugeGrass") -> tuple[Path, Path, Path]:
    """Alpha with no Workshop items, and a mod copied into its content/Mods by hand that names ITEM."""
    settings_path = setup_root(tmp_path)
    server = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    (server / "fake-steam.json").write_text(json.dumps({str(ITEM): steam}), encoding="utf-8")
    hand = a_mod(server / "content" / "Mods" / folder, maps if maps is not None else {f"fake{ITEM}": a_map()},
                 title=TITLE, workshop=ITEM)
    return settings_path, server, hand


def keeper_for(settings_path: Path, lifecycle: FakeLifecycle, steam: dict[int, SteamItem] | None = None
               ) -> WorkshopKeeper:
    updates = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, TITLE, NEW, "n", MCC_APP)}
                          if steam is None else steam)
    return WorkshopKeeper(updates.settings, updates.bus, lifecycle, updates)  # type: ignore[arg-type]


def items_of(server: Path) -> list[int]:
    return read_toml(server / "dedicated.toml").get("workshop", {}).get("items", [])


def staging_left(server: Path) -> list[Path]:
    """What's left in the staging folder beside the server's content folder."""
    return list((server / STAGING).iterdir()) if (server / STAGING).is_dir() else []


# Taking over hand copies ------------------------------------------------------------------------------------------

def test_a_hand_copy_is_taken_over_after_the_game(tmp_path: Path, run, caplog) -> None:
    settings_path, folder, hand = root_with_hand_copy(tmp_path)
    lifecycle = FakeLifecycle()
    keeper = keeper_for(settings_path, lifecycle)
    server = keeper.settings.server(ALPHA)
    assert installed_version(server.content_dir, ITEM) is None

    async def scenario() -> None:
        await keeper.look_at(server)
        await asyncio.sleep(0)
        # Fetched beside the running server; nothing it uses has changed yet.
        assert hand.is_dir() and items_of(folder) == [] and ITEM in keeper.adopting[ALPHA]
        assert [j.key for j in lifecycle.jobs[ALPHA]] == ["workshop-adopt"]
        assert lifecycle.restarts == [(ALPHA, "after_game", f"putting the Workshop's own copy of {TITLE} in place of "
                                                            "a hand copy, to keep it up to date")]
        assert lifecycle.told == [f"[^_^] YOUR TAX DOLLARS AT WORK! This server restarts after this game to bring "
                                  f"{TITLE} up to date."]
        await keeper.look_at(server)  # looked at again while it waits: no second fetch, no second plan
        await asyncio.sleep(0)
        assert len(lifecycle.restarts) == 1
        said = await lifecycle.jobs[ALPHA][0].run()  # the game ended: the server is stopped
        assert said.startswith(f"kept up to date from now on: {TITLE}")

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert not hand.exists() and (folder / SET_ASIDE / "Mods" / "HugeGrass" / "ModInfo.json").is_file()
    assert installed_version(server.content_dir, ITEM)["updated"] == NEW
    assert items_of(folder) == [ITEM] and server.workshop_items == [ITEM]
    assert staging_left(folder) == []
    assert "is a hand copy (Mods/HugeGrass) that nothing keeps up to date" in caplog.text
    assert f"{TITLE} (Workshop {ITEM}) is kept up to date now" in caplog.text
    record = json.loads((keeper.settings.records / RECORD).read_text(encoding="utf-8"))
    assert record["adopted"][0]["item"] == ITEM and record["adopted"][0]["server"] == ALPHA
    # From now on the update service keeps it up to date like any listed item.
    assert run(keeper.updates.check()) == {}


def test_a_stopped_servers_hand_copy_is_swapped_at_once(tmp_path: Path, run) -> None:
    settings_path, folder, hand = root_with_hand_copy(tmp_path)
    lifecycle = FakeLifecycle(phase="idle")
    keeper = keeper_for(settings_path, lifecycle)
    run(keeper.look_at(keeper.settings.server(ALPHA)))
    assert not hand.exists() and items_of(folder) == [ITEM] and not lifecycle.restarts and not lifecycle.jobs


def test_a_hand_copy_in_the_workshop_folder_is_replaced(tmp_path: Path, run) -> None:
    """Copied into Mods/Workshop/<item> by hand, with no receipt: the program refuses to fetch over it."""
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    a_mod(folder / "content" / "Mods" / "Workshop" / str(ITEM), {f"fake{ITEM}": a_map()}, title=TITLE, workshop=ITEM,
          receipt=False)
    lifecycle = FakeLifecycle(phase="idle")
    keeper = keeper_for(settings_path, lifecycle)
    server = keeper.settings.server(ALPHA)
    assert look_through(server.content_dir).hand.keys() == {ITEM}
    run(keeper.look_at(server))
    assert installed_version(server.content_dir, ITEM) is not None  # the program's own copy, with its receipt
    assert (folder / SET_ASIDE / "Mods" / "Workshop" / str(ITEM) / "ModInfo.json").is_file()
    assert items_of(folder) == [ITEM]


def test_hand_copies_left_as_they_are(tmp_path: Path, run, caplog) -> None:
    settings_path, folder, hand = root_with_hand_copy(tmp_path)
    content = folder / "content"
    a_mod(content / "Mods" / "Other Game", {"other": a_map()}, title="Reach map", workshop=111, engine="HaloReach")
    a_mod(content / "Mods" / "Hidden", {"hidden": a_map()}, title="My own upload", workshop=222)
    a_mod(content / "Mods" / "Not MCC", {"notmcc": a_map()}, title="Something else", workshop=333)
    a_mod(content / "Mods" / "Switched Off", {"off": a_map()}, title="Off", workshop=444)
    (content / "disabled-mods.json").write_text(json.dumps({"packages": ["Mods/Switched Off"]}), encoding="utf-8")
    steam = {ITEM: SteamItem(ITEM, TITLE, NEW, "n", MCC_APP), 333: SteamItem(333, "Else", NEW, "n", 4000)}
    asked: list[list[int]] = []
    lifecycle = FakeLifecycle()
    keeper = keeper_for(settings_path, lifecycle, steam)
    real = keeper.updates.fetch

    def fetch(items: list[int]) -> dict[int, SteamItem]:
        asked.append(items)
        return real(items)

    keeper.updates.fetch = fetch  # type: ignore[assignment]
    with caplog.at_level(logging.INFO):
        run(keeper.look_at(keeper.settings.server(ALPHA)))
    assert set(keeper.adopting[ALPHA]) == {ITEM}  # only the real one
    assert sorted(i for (i,) in asked) == [222, 333, ITEM]  # never asked about another game's or a switched-off one
    assert "My own upload is left a hand-installed mod (Mods/Hidden): Steam doesn't list Workshop item 222 " \
           "publicly" in caplog.text
    assert "Workshop item 333 isn't an MCC mod" in caplog.text


def test_a_mod_made_from_an_item_is_left_and_remembered(tmp_path: Path, run, caplog) -> None:
    """A host's own version of a map that kept the item's ID in its ModInfo.json: its maps aren't the item's."""
    settings_path, folder, hand = root_with_hand_copy(tmp_path, maps={"lg_hugegrass": a_map()}, folder="LG HugeGrass")
    lifecycle = FakeLifecycle()
    keeper = keeper_for(settings_path, lifecycle)
    with caplog.at_level(logging.INFO):
        run(keeper.look_at(keeper.settings.server(ALPHA)))
    assert hand.is_dir() and not keeper.adopting.get(ALPHA) and not lifecycle.restarts and items_of(folder) == []
    assert "is left as it is: Mods/LG HugeGrass names Workshop item" in caplog.text
    assert "its maps (lg_hugegrass) aren't the item's" in caplog.text
    assert staging_left(folder) == []
    # Huragok starts again: it doesn't fetch 2 GB again to find the same.
    again = keeper_for(settings_path, FakeLifecycle())
    tries: list[int] = []
    again._stage = lambda *a, **k: tries.append(1)  # type: ignore[assignment,method-assign]
    run(again.look_at(again.settings.server(ALPHA)))
    assert tries == []


def test_no_taking_over_when_workshop_updates_are_off_or_content_is_shared(tmp_path: Path, run, caplog) -> None:
    settings_path, folder, hand = root_with_hand_copy(tmp_path)
    settings = load(settings_path)
    settings.workshop_updates.install = "off"
    lifecycle = FakeLifecycle()
    updates = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, TITLE, NEW, "n", MCC_APP)})
    updates.settings = settings
    keeper = WorkshopKeeper(settings, updates.bus, lifecycle, updates)  # type: ignore[arg-type]
    run(keeper.look_at(settings.server(ALPHA)))
    assert hand.is_dir() and not keeper.adopting.get(ALPHA)
    # Beta shares Alpha's content folder: both would have to stop.
    second = tmp_path / "reclaimer" / "SERVERS" / "Beta"
    make_server_folder(second, "Beta", free_port())
    set_table_key(second / "dedicated.toml", "rcon", "password", "secretsecret")
    set_top_key(second / "dedicated.toml", "content", f"../{ALPHA}/content")
    keeper = keeper_for(settings_path, FakeLifecycle())
    with caplog.at_level(logging.INFO):
        run(keeper.look_at(keeper.settings.server(ALPHA)))
    assert hand.is_dir() and not keeper.adopting.get(ALPHA)
    assert "in a content folder Beta use too: it's left as it is" in caplog.text


def test_taking_over_switched_off(tmp_path: Path, run) -> None:
    settings_path, folder, hand = root_with_hand_copy(tmp_path)
    settings = load(settings_path)
    settings.workshop_updates.adopt = False
    lifecycle = FakeLifecycle(phase="idle")
    updates = service_for(settings_path, lifecycle, {})
    keeper = WorkshopKeeper(settings, updates.bus, lifecycle, updates)  # type: ignore[arg-type]
    run(keeper.look_at(settings.server(ALPHA)))
    assert hand.is_dir() and items_of(folder) == []


# The program's own copies that aren't listed ---------------------------------------------------------------------

def test_an_unlisted_copy_is_listed_again_unless_removed_with_r(tmp_path: Path, run, caplog) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    a_mod(folder / "content" / "Mods" / "Workshop" / str(ITEM), {f"fake{ITEM}": a_map()}, title=TITLE, workshop=ITEM)
    lifecycle = FakeLifecycle()
    keeper = keeper_for(settings_path, lifecycle)
    server = keeper.settings.server(ALPHA)
    with caplog.at_level(logging.INFO):
        run(keeper.look_at(server))
    assert items_of(folder) == [ITEM] and not lifecycle.restarts and not lifecycle.jobs  # no restart for that
    assert f"is in its content folder (Mods/Workshop/{ITEM}) but wasn't in [workshop] items: the server loads it all " \
           "the same, so it's listed now" in caplog.text
    # R in the window: out of the list, and its files switched off, so it isn't listed again.
    set_table_key(folder / "dedicated.toml", "workshop", "items", [], before="master")
    server.workshop_items = []
    lines = switch_workshop_copies(server, [ITEM], off=True)
    assert lines == [f"Mods/Workshop/{ITEM} switched off in disabled-mods.json: its files stay, but the server stops "
                     "loading them, and Huragok doesn't list it again"]
    run(keeper.look_at(server))
    assert items_of(folder) == []
    assert switch_workshop_copies(server, [ITEM], off=True) == []  # once
    assert switch_workshop_copies(server, [ITEM], off=False) == [f"Mods/Workshop/{ITEM} switched back on in "
                                                                "disabled-mods.json"]
    assert not (folder / "content" / "disabled-mods.json").exists()


# Fetching what's added ---------------------------------------------------------------------------------------------

def test_an_item_added_to_a_stopped_server_is_fetched_at_once(tmp_path: Path, run, caplog) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    lifecycle = FakeLifecycle(phase="idle")
    keeper = keeper_for(settings_path, lifecycle)
    server = keeper.settings.server(ALPHA)
    with caplog.at_level(logging.INFO):
        assert run(keeper.fetch_missing(server)) == [ITEM]
    assert installed_version(server.content_dir, ITEM) is not None
    assert f"fetching Workshop {ITEM} now (just added), so it's ready as the server starts" in caplog.text
    assert f"Workshop {ITEM} fetched (version of 13 Oct 2026)" in caplog.text
    assert run(keeper.fetch_missing(server)) == []  # there now


def test_a_fetch_that_fails_is_tried_again_later_three_times(tmp_path: Path, run, caplog) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    (folder / "fake_steam_down").write_text("", encoding="utf-8")
    keeper = keeper_for(settings_path, FakeLifecycle(phase="idle"))
    server = keeper.settings.server(ALPHA)
    with caplog.at_level(logging.INFO):
        assert run(keeper.fetch_missing(server)) == []
        assert run(keeper.fetch_missing(server)) == []  # not again straight away
        for _ in range(2):
            keeper.tries[(ALPHA, ITEM)] = (keeper.tries[(ALPHA, ITEM)][0] - 3600, keeper.tries[(ALPHA, ITEM)][1])
            run(keeper.fetch_missing(server))
        keeper.tries[(ALPHA, ITEM)] = (keeper.tries[(ALPHA, ITEM)][0] - 3600, keeper.tries[(ALPHA, ITEM)][1])
        run(keeper.fetch_missing(server))
    assert caplog.text.count("couldn't be fetched: ") == 3 and "(try 3 of 3)" in caplog.text
    assert "couldn't be fetched in 3 tries" in caplog.text
    (folder / "fake_steam_down").unlink()

    async def added_again() -> None:  # a person adds it again in the window: tried again at once
        ok, said = keeper.look_now(ALPHA)
        assert ok and said == "Huragok fetches what's new now; the log says when it's in"
        from conftest import wait_for

        await wait_for(lambda: installed_version(server.content_dir, ITEM) is not None)

    run(added_again())


def test_a_running_server_fetches_its_own_or_gets_it_beside(tmp_path: Path, run, caplog) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    set_table_key(folder / "dedicated.toml", "workshop", "items", [ITEM], before="master")
    keeper = keeper_for(settings_path, FakeLifecycle(phase="running"))
    server = keeper.settings.server(ALPHA)
    with caplog.at_level(logging.INFO):
        assert run(keeper.fetch_missing(server)) == []
    assert "the running server fetches it itself (auto_reload)" in caplog.text
    set_top_key(folder / "dedicated.toml", "auto_reload", False)
    assert run(keeper.fetch_missing(server)) == [ITEM]  # staged beside it and moved in: a new folder
    assert installed_version(server.content_dir, ITEM) is not None


# Forge layouts -----------------------------------------------------------------------------------------------------

def layout(path: Path, name: str, body: str = "v1") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(json.dumps({"name": name, "objects": body}), encoding="utf-8")


def test_layouts_follow_the_mods_versions(tmp_path: Path) -> None:
    content = tmp_path / "content"
    mod_folder = a_mod(content / "Mods" / "HugeGrass", {"hugegrass": a_map()}, title="HugeGrass")
    shipped = mod_folder / "Project Reclaimer Layouts"
    for name in ("Balanced", "Spirits", "Mine", "Gone"):
        layout(shipped, name)
    maps = content / "Maps" / "HugeGrass"
    layout(maps, "Mine", "made with L long ago")  # there before Huragok kept track, and different
    written: dict[str, str] = {}
    mod = installed_mods(content)[0]
    result = sync_layouts(mod, content, written)
    assert sorted(result.copied) == ["Maps/HugeGrass/Balanced.json", "Maps/HugeGrass/Gone.json",
                                     "Maps/HugeGrass/Spirits.json"]
    assert result.replaced == ["Maps/HugeGrass/Mine.json"]
    assert (tmp_path / SET_ASIDE / "Maps" / "HugeGrass" / "Mine.json").read_text(encoding="utf-8").count("long ago")
    # A new version of the mod; meanwhile someone changed Spirits here and deleted Gone.
    for name in ("Balanced", "Spirits", "Gone"):
        layout(shipped, name, "v2")
    layout(maps, "Spirits", "my own edit")
    (maps / "Gone.json").unlink()
    result = sync_layouts(installed_mods(content)[0], content, written)
    assert result.updated == ["Maps/HugeGrass/Balanced.json"] and result.kept == ["Maps/HugeGrass/Spirits.json"]
    assert "v2" in (maps / "Balanced.json").read_text(encoding="utf-8")
    assert "my own edit" in (maps / "Spirits.json").read_text(encoding="utf-8") and not (maps / "Gone.json").exists()
    assert not sync_layouts(installed_mods(content)[0], content, written).changed  # nothing more to do


def test_layouts_are_kept_in_step_by_the_keeper(tmp_path: Path, run, caplog) -> None:
    settings_path = setup_root(tmp_path)
    folder = tmp_path / "reclaimer" / "SERVERS" / ALPHA
    mod_folder = a_mod(folder / "content" / "Mods" / "HugeGrass", {"hugegrass": a_map()}, title="HugeGrass")
    layout(mod_folder / "Project Reclaimer Layouts", "Balanced")
    keeper = keeper_for(settings_path, FakeLifecycle())
    with caplog.at_level(logging.INFO):
        lines = keeper.sync_layouts()
    assert lines and "Forge layouts from HugeGrass: 1 copied in Maps/HugeGrass" in caplog.text
    assert (folder / "content" / "Maps" / "HugeGrass" / "Balanced.json").is_file()
    assert keeper.sync_layouts() == []  # once
    # The window's L remembers what it copied, so the next version is copied over it.
    layout(mod_folder / "Project Reclaimer Layouts", "Spirits")
    assert layouts_by_hand(keeper.settings, folder / "content", installed_mods(folder / "content")[0]) == \
        ["Maps/HugeGrass/Spirits.json"]
    layout(mod_folder / "Project Reclaimer Layouts", "Spirits", "v2")
    keeper.sync_layouts()
    assert "v2" in (folder / "content" / "Maps" / "HugeGrass" / "Spirits.json").read_text(encoding="utf-8")
    settings = load(settings_path)
    settings.workshop_updates.layouts = False
    assert WorkshopKeeper(settings, keeper.bus, keeper.lifecycle, keeper.updates).layouts_for(  # type: ignore[arg-type]
        settings.server(ALPHA)) == []


def test_a_swap_that_cant_move_the_hand_copy_leaves_it_and_waits(tmp_path: Path, run, monkeypatch, caplog) -> None:
    from huragok.content import workshopmods

    settings_path, folder, hand = root_with_hand_copy(tmp_path)
    lifecycle = FakeLifecycle()
    keeper = keeper_for(settings_path, lifecycle)
    server = keeper.settings.server(ALPHA)
    run(keeper.look_at(server))
    monkeypatch.setattr(workshopmods, "set_aside", lambda root, paths: ([], [f"{p.name} (in use)" for p in paths]))
    with caplog.at_level(logging.INFO):
        said = run(lifecycle.jobs[ALPHA][0].run())
    assert said == "no hand-copied Workshop mod taken over: HugeGrass Rebalance (HugeGrass (in use))"
    assert hand.is_dir() and items_of(folder) == [] and installed_version(server.content_dir, ITEM) is None
    assert "couldn't take over HugeGrass Rebalance (HugeGrass (in use)): it's left as it was, tried again in 10 " \
           "minutes" in caplog.text
    assert keeper.tries[(ALPHA, ITEM)][1] == 1 and staging_left(folder) == []
    stages: list[int] = []
    keeper._stage = lambda *a, **k: stages.append(1)  # type: ignore[assignment,method-assign]
    run(keeper.look_at(server))  # not fetched again at the next look
    assert stages == []
