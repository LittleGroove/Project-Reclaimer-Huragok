"""What a server arrives without, put right by Huragok itself (repair.py), and SteamCMD for Halo 3's files: a server
folder copied in from another Huragok folder or PC, as the host's first 0.4.0 test run had (19 October 2026)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest

from testkit.setup import ALPHA, SetupController, StrictKeyboard, fake_tool, setup_root
from testkit.window import FakeTerminal
from testkit.workshop import ITEM, NEW, FakeLifecycle, service_for, workshop_root

from huragok import steam as steamsetup
from huragok.commandline import cli
from huragok.content import gamefiles as repair
from huragok.core.events import Event, EventBus
from huragok.core.logs import RecentLog
from huragok.core.requests import Result
from huragok.core.settings import load
from huragok.core.tomledit import read_toml, set_table_key, set_top_key
from huragok.games import files as game_files
from huragok.steam import SteamItem
from huragok.window.dashboard import Menu

# What 0.9.20 printed on the host's PC when the copied server started without its shared game folder.
MISSING = [
    r"Error: The game folder C:\reclaimer\SERVERS\Alpha Server\../../game is missing files copied from your own "
    "MCC installation:",
    "  halo3/halo3.dll",
    "  halo3/maps/shared.map",
    "  a multiplayer map: halo3/maps/<map>.map with halo3/maps/info/<map>.mapinfo",
    'Copy them from "Halo The Master Chief Collection" keeping the same folders, or run `dedicated init --from '
    '"<your MCC folder>"` on a PC with MCC installed. See docs/dedicated-servers.md.',
    "Error: Dedicated supervisor stopped (exit code: 1)",
]


def copied_in(tmp_path: Path, **toml: str) -> tuple[Path, Any]:
    """A root whose Alpha Server was copied in from somewhere else: its dedicated.toml names what it shared there,
    and none of it came along. Returns (settings.toml, Alpha's ServerConfig)."""
    settings_path = setup_root(tmp_path, with_game=False)
    folder = settings_path.parent / "SERVERS" / ALPHA
    shutil.rmtree(folder / "game")
    (folder / "bans.json").unlink()
    shutil.rmtree(folder / "content")
    values = {"game": "../../game", "bans": "../../bans.json", "content": "content", **toml}
    for key, value in values.items():
        set_top_key(folder / "dedicated.toml", key, value)
    settings = load(settings_path)
    return settings_path, settings.server(ALPHA)


def halo3_files(game: Path, *, odst: bool = False, campaign: bool = False) -> Path:
    for file in ("halo3/halo3.dll", "halo3/maps/shared.map", "halo3/maps/guardian.map"):
        (game / file).parent.mkdir(parents=True, exist_ok=True)
        (game / file).write_bytes(b"x")
    if odst:
        for file in ("halo3odst/halo3odst.dll", "halo3odst/maps/shared.map"):
            (game / file).parent.mkdir(parents=True, exist_ok=True)
            (game / file).write_bytes(b"x")
    if campaign:
        for name in ("campaign", "010_jungle", "040_voi"):
            (game / "halo3" / "maps" / f"{name}.map").write_bytes(b"x")
    return game


def fake_mcc(folder: Path, *, odst: bool = False) -> Path:
    halo3_files(folder, odst=odst, campaign=True)
    return folder


def signed_in_steamcmd(root: Path, account: str = "lilgremlin") -> Path:
    """SteamCMD in the root's steamcmd/ with a saved sign-in for `account` (its config.vdf, as SteamCMD writes it)."""
    program = root / "steamcmd" / steamsetup.executable_name()
    (program.parent / "config").mkdir(parents=True)
    program.write_bytes(b"")
    (program.parent / "config" / "config.vdf").write_text(
        '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"Accounts"\n'
        f'\t\t\t\t{{\n\t\t\t\t\t"{account}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"SteamID"\t\t"1"\n\t\t\t\t\t}}\n\t\t\t\t}}\n'
        "\t\t\t}\n\t\t}\n\t}\n}\n", encoding="utf-8")
    return program


# Quick repairs ---------------------------------------------------------------------------------------------------

def test_a_server_copied_in_gets_its_ban_list_and_content_folder(tmp_path: Path) -> None:
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    done = repair.quick_repairs(settings, server)
    own = server.folder / "bans.json"  # every server keeps a ban list of its own
    assert json.loads(own.read_text(encoding="utf-8")) == repair.EMPTY_BANS
    assert read_toml(server.config_path)["bans"] == "bans.json" and not (settings.home / "bans.json").exists()
    assert any("its ban list (../../bans.json) wasn't there: it has its own now" in line and
               "its bans stayed there: copy that bans.json over this one to keep them" in line for line in done)
    assert all((server.folder / "content" / sub).is_dir() for sub in repair.CONTENT_FOLDERS)
    assert repair.quick_repairs(settings, server) == []  # nothing twice
    # A ban list in a folder that isn't on this PC: its own, too.
    set_top_key(server.config_path, "bans", "../../../../elsewhere/shared/bans.json")
    done = repair.quick_repairs(settings, server)
    assert read_toml(server.config_path)["bans"] == "bans.json"
    assert any("it has its own now" in line for line in done)
    # The same for content.
    set_top_key(server.config_path, "content", "../../../../elsewhere/content")
    shutil.rmtree(server.folder / "content")
    assert any("it has its own content/ now" in line for line in repair.quick_repairs(settings, server))
    assert read_toml(server.config_path)["content"] == "content"


def test_a_server_sharing_a_ban_list_gets_its_own_copy_as_it_starts(tmp_path: Path) -> None:
    """The host's call: every server keeps a ban list of its own (a /ban is that server's). Servers made up to 0.4.4
    share one; each gets a copy of it as it's about to start (the moment it reads the path), with everything on it:
    nobody is unbanned, no admin or vote-kick immunity lost. Never while it runs: it keeps reading the old one."""
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    shared = settings.home / "bans.json"
    first, second, third = "a" * 64, "b" * 64, "c" * 64
    shared.write_text(json.dumps({"players": [{"id": first, "name": "Bob", "reason": "cheating"}], "ips": [],
                                  "vote_kick_immune": [{"id": second, "added_by": "Vergil (RCON)"}]}), encoding="utf-8")
    assert not any("its own now" in line for line in repair.quick_repairs(settings, server))  # found: left alone
    assert read_toml(server.config_path)["bans"] == "../../bans.json"
    said = repair.own_bans(settings, repair.read_paths(server.folder, settings.config_name))
    own = server.folder / "bans.json"
    assert json.loads(own.read_text(encoding="utf-8")) == json.loads(shared.read_text(encoding="utf-8"))
    assert read_toml(server.config_path)["bans"] == "bans.json" and said is not None
    assert "every ban, admin and allowance on it stays" in said
    # Where it came from, for Huragok's list: its bans were every server's when given (the commands part).
    recorded = json.loads((settings.records / "shared-ban-lists.json").read_text(encoding="utf-8"))
    assert recorded == [str(shared.resolve())]
    assert repair.own_bans(settings, repair.read_paths(server.folder, settings.config_name)) is None  # its own now
    # One left in its folder from before it shared: both lists' entries, the old file kept aside.
    set_top_key(server.config_path, "bans", "../../bans.json")
    own.write_text(json.dumps({"players": [{"id": third, "name": "Cy"}], "ips": []}), encoding="utf-8")
    repair.own_bans(settings, repair.read_paths(server.folder, settings.config_name))
    merged = json.loads(own.read_text(encoding="utf-8"))
    assert [p["id"] for p in merged["players"]] == [third, first]
    assert merged["vote_kick_immune"] == [{"id": second, "added_by": "Vergil (RCON)"}]
    assert (server.folder / "bans.json.before-own").is_file()


def test_a_server_copied_in_is_pointed_at_game_files_that_are_here(tmp_path: Path) -> None:
    settings_path, server = copied_in(tmp_path, game="D:/old-pc/reclaimer/game")
    settings = load(settings_path)
    assert repair.fix_game_path(settings, repair.read_paths(server.folder, "dedicated.toml"), "") is None  # none here
    halo3_files(settings.home / "game")
    done = repair.quick_repairs(settings, server)
    assert read_toml(server.config_path)["game"] == "../../game"
    assert any("its game folder (D:/old-pc/reclaimer/game) has no Halo 3 files here: it uses" in line for line in done)
    # A game folder with Halo 3 in it is never swapped for another (its missing ODST is added to it instead).
    halo3_files(server.folder / "own-game")
    set_top_key(server.config_path, "game", "own-game")
    assert repair.fix_game_path(settings, repair.read_paths(server.folder, "dedicated.toml"), "odst") is None


def test_a_steamcmd_from_another_pc_is_swapped_for_this_ones(tmp_path: Path) -> None:
    settings_path, server = copied_in(tmp_path)
    set_table_key(server.config_path, "workshop", "steamcmd", "Z:/not/here/steamcmd.exe", before="master")
    set_table_key(server.config_path, "workshop", "username", "oldaccount", before="master")
    settings = load(settings_path)
    assert repair.fix_steamcmd(settings, repair.read_paths(server.folder, "dedicated.toml")) is None  # none here
    program = signed_in_steamcmd(settings.home)
    set_table_key(settings_path, "setup", "steam_username", "lilgremlin", before="process")
    settings = load(settings_path)
    said = repair.fix_steamcmd(settings, repair.read_paths(server.folder, "dedicated.toml"))
    assert said and "isn't on this PC: it uses" in said and "the account lilgremlin" in said
    assert read_toml(server.config_path)["workshop"] == {"steamcmd": str(program), "username": "lilgremlin"}


def test_what_is_missing_and_what_a_failed_start_says_is() -> None:
    assert game_files.kinds_in_failure(MISSING) == ["halo3"]
    assert game_files.kinds_in_failure(["Error: something else"]) == []
    odst = [MISSING[0], "  halo3odst/halo3odst.dll"]
    assert game_files.kinds_in_failure(odst) == ["odst"]
    assert game_files.kinds_in_failure([MISSING[0], "  halo3/maps/010_jungle.map"]) == ["campaign"]


# Getting the files -----------------------------------------------------------------------------------------------

def test_halo3s_files_are_copied_from_mcc_and_the_server_starts_on_them(tmp_path: Path, run, caplog) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    settings = load(settings_path)
    released: list[bool] = []
    files = repair.GameFiles(settings, EventBus(), fake_tool, release=lambda: released.append(True))

    async def scenario() -> None:
        wait = files.prelaunch(settings.server(ALPHA))
        assert wait and wait.startswith("getting Halo 3's files: looking for where to get them")
        await asyncio.gather(*files.tasks)
        assert released == [True] and not files.getting and not files.failed
        assert files.prelaunch(settings.server(ALPHA)) is None  # it has them: it starts

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert game_files.has_game_files(settings.home / "game")
    assert read_toml(server.config_path)["game"] == "../../game"
    assert "Halo 3's files are in" in caplog.text and "the servers waiting for them start" in caplog.text


def test_with_no_mcc_and_no_steamcmd_the_server_waits_and_says_how_to_get_them(tmp_path: Path, run, caplog) -> None:
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    files = repair.GameFiles(settings, EventBus(), fake_tool)

    async def scenario() -> str | None:
        files.prelaunch(settings.server(ALPHA))
        await asyncio.gather(*files.tasks)
        return files.prelaunch(settings.server(ALPHA))

    with caplog.at_level(logging.ERROR):
        wait = run(scenario())
    assert wait == "needs Halo 3's files: MCC isn't on this PC and SteamCMD isn't set up; press I"
    assert "there's no way to get them here" in caplog.text
    files.try_again()
    assert not files.failed


def test_a_download_is_off_when_the_host_says_so(tmp_path: Path) -> None:
    settings_path, _ = copied_in(tmp_path)
    signed_in_steamcmd(settings_path.parent)
    set_table_key(settings_path, "setup", "steam_username", "lilgremlin", before="process")
    settings = load(settings_path)
    found, why = repair.sources(settings, "halo3")
    assert [s.how for s in found] == ["steamcmd"] and why == ""
    set_table_key(settings_path, "setup", "download_game_files", False, before="process")
    settings = load(settings_path)
    found, why = repair.sources(settings, "halo3")
    assert found == [] and why == "downloads are off ([setup] download_game_files = false)"
    assert [s.how for s in repair.sources(settings, "halo3", downloads=True)[0]] == ["steamcmd"]  # I may, though
    set_table_key(settings_path, "setup", "steam_username", "someoneelse", before="process")
    assert repair.sources(load(settings_path), "halo3", downloads=True)[1] == \
        "someoneelse isn't signed in to SteamCMD here"


def test_halo3s_files_downloaded_with_steamcmd_keep_no_sound(tmp_path: Path, monkeypatch) -> None:
    settings_path, _ = copied_in(tmp_path)
    program = signed_in_steamcmd(settings_path.parent)
    settings = load(settings_path)
    asked: list[tuple[str, int]] = []

    def download(executable: Path, username: str, depot: int, timeout: float = 0) -> Path:
        asked.append((username, depot))
        folder = executable.parent / "steamapps" / "content" / "app_976730" / f"depot_{depot}"
        halo3_files(folder)
        (folder / "halo3" / "fmod" / "pc").mkdir(parents=True)
        (folder / "halo3" / "fmod" / "pc" / "sfx.fsb").write_bytes(b"sound")
        return folder

    monkeypatch.setattr(steamsetup, "download_depot", download)
    # Room for it, whatever the disk the test runs on (GitHub's Ubuntu runners have about 8 GB free).
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 100_000_000_000})())
    source = repair.Source("steamcmd", program, "lilgremlin")
    said = repair.fetch(settings, "halo3", source, settings.home / "game", fake_tool, lambda line: None)
    assert asked == [("lilgremlin", 976739)] and said.startswith("Halo 3's files are in")
    assert game_files.has_game_files(settings.home / "game") and not (settings.home / "game" / "halo3" / "fmod").exists()
    assert not (program.parent / "steamapps" / "content" / "app_976730" / "depot_976739").exists()  # not kept twice
    assert not (settings.records / "game-files-halo3").exists()
    # No room for it: said before anything is downloaded.
    monkeypatch.setattr(shutil, "disk_usage", lambda path: type("U", (), {"free": 1_000_000_000})())
    with pytest.raises(OSError, match="the download needs about 9.8 GB"):
        repair.fetch(settings, "halo3", source, settings.home / "game", fake_tool, lambda line: None)
    assert len(asked) == 1


def test_steamcmd_downloads_one_depot_on_its_saved_sign_in(tmp_path: Path, monkeypatch) -> None:
    program = signed_in_steamcmd(tmp_path)
    folder = program.parent / "steamapps" / "content" / "app_976730" / "depot_976739"
    said: list[list[str]] = []

    def answer(text: str) -> Any:
        def fake(executable: Path, words: list[str], timeout: float = 600.0) -> Any:
            said.append(words)
            return type("R", (), {"returncode": 0, "stdout": text})()
        return fake

    monkeypatch.setattr(steamsetup, "run", answer(f'Logged in OK\nDepot download complete : "{folder}" (809 files)'))
    folder.mkdir(parents=True)
    (folder / "x").write_bytes(b"x")
    assert steamsetup.download_depot(program, "lilgremlin", 976739) == folder
    assert "+@NoPromptForPassword" in said[0] and said[0][-4:] == ["+download_depot", "976730", "976739", "+quit"]
    monkeypatch.setattr(steamsetup, "run", answer("FAILED (Invalid Password)\npassword:"))
    with pytest.raises(steamsetup.SteamSetupError, match="press I to sign in again"):
        steamsetup.download_depot(program, "lilgremlin", 976739)
    monkeypatch.setattr(steamsetup, "run", answer("Logged in OK\nERROR! Download depot 976739 failed : No subscription"))
    with pytest.raises(steamsetup.SteamSetupError, match="doesn't own this part of MCC"):
        steamsetup.download_depot(program, "lilgremlin", 976739)
    shutil.rmtree(folder)
    monkeypatch.setattr(steamsetup, "run", answer("Logged in OK\n"))
    with pytest.raises(steamsetup.SteamSetupError, match="didn't download depot 976739"):
        steamsetup.download_depot(program, "lilgremlin", 976739)
    with pytest.raises(steamsetup.SteamSetupError, match="isn't a Steam account name"):
        steamsetup.download_depot(program, "a b; rm", 976739)


def test_a_start_that_failed_for_missing_files_gets_them_before_it_tries_again(tmp_path: Path, run) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    settings = load(settings_path)
    halo3_files(settings.home / "game")
    (settings.home / "game" / "halo3" / "maps" / "guardian.map").unlink()  # a copy that left a map out
    bus = EventBus()
    files = repair.GameFiles(settings, bus, fake_tool)

    async def scenario() -> None:
        assert files.prelaunch(settings.server(ALPHA)) is None  # halo3.dll and shared.map are there
        bus.publish(Event("server.start_failed", ALPHA, {"why": "it exited", "lines": MISSING}))
        wait = files.prelaunch(settings.server(ALPHA))
        assert wait and wait.startswith("getting Halo 3's files")
        await asyncio.gather(*files.tasks)
        assert files.prelaunch(settings.server(ALPHA)) is None and ALPHA not in files.forced
        # The same failure just after they arrived: not got again and again.
        bus.publish(Event("server.start_failed", ALPHA, {"why": "it exited", "lines": MISSING}))
        assert "still says files are missing" in (files.prelaunch(settings.server(ALPHA)) or "")

    run(scenario())
    assert (settings.home / "game" / "halo3" / "maps" / "guardian.map").is_file()


def test_nothing_is_put_right_for_a_server_this_pc_doesnt_run(tmp_path: Path) -> None:
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    settings.backend = "docker"  # its dedicated.toml's paths are the container's
    files = repair.GameFiles(settings, EventBus(), fake_tool)
    assert files.arrived(server) == [] and files.prelaunch(server) is None
    assert not (settings.home / "bans.json").exists()


def test_what_i_shows_about_the_game_folders(tmp_path: Path) -> None:
    settings_path, _ = copied_in(tmp_path)
    settings = load(settings_path)
    rows = repair.game_status(settings)
    assert ("missing", f"{ALPHA} (Halo 3's files)", "yellow") in rows
    assert any(text.endswith("no Halo 3 files (not there) · used by 1 server(s)") for _, text, _ in rows)
    halo3_files(settings.home / "game", odst=True)
    (settings.home / "game" / "halo3" / "maps" / "campaign.map").write_bytes(b"x")
    (settings.home / "game" / "halo3" / "maps" / "040_voi.map").write_bytes(b"x")
    rows = repair.game_status(settings)
    assert any("Halo 3, 1 multiplayer maps · Forge's campaign files (no missions) · ODST · used by 1 server(s)"
               in text for _, text, _ in rows)
    assert repair.shared_target(settings, "odst") == (settings.home / "game", "")
    shutil.rmtree(settings.home / "game")
    target, why = repair.shared_target(settings, "campaign")
    assert target is None and "get those first" in why


# The Workshop: no restart a fetch can't follow --------------------------------------------------------------------

def test_a_mod_update_waits_without_a_restart_while_steamcmd_cant_fetch_it(tmp_path: Path, run, caplog) -> None:
    settings_path, folder = workshop_root(tmp_path)
    set_table_key(folder / "dedicated.toml", "workshop", "steamcmd", str(tmp_path / "other-pc" / "steamcmd.exe"),
                  before="master")
    lifecycle = FakeLifecycle()
    service = service_for(settings_path, lifecycle, {ITEM: SteamItem(ITEM, "HugeGrass Rebalance", NEW, "n")})

    async def scenario() -> None:
        await service.check(quiet=False)
        await asyncio.sleep(0)
        assert lifecycle.restarts == [] and not lifecycle.jobs.get(ALPHA)
        flag = lifecycle.flags[ALPHA]["workshop"]
        assert "it can't be fetched: its SteamCMD" in flag and "isn't on this PC: press I" in flag
        ok, message = await service.install_now(ALPHA, "after_game")
        assert not ok and "press I to set up SteamCMD for it" in message

    with caplog.at_level(logging.WARNING):
        run(scenario())
    assert "No restart until it can" in caplog.text
    config = folder / "dedicated.toml"
    assert steamsetup.cannot_fetch(config) and "isn't on this PC" in steamsetup.cannot_fetch(config)
    program = signed_in_steamcmd(tmp_path / "here")
    set_table_key(config, "workshop", "steamcmd", str(program), before="master")
    assert steamsetup.cannot_fetch(config) == \
        "no Steam account is set for SteamCMD (an anonymous sign-in can't fetch MCC's items)"
    set_table_key(config, "workshop", "username", "stranger", before="master")
    assert steamsetup.cannot_fetch(config) == "stranger has no saved SteamCMD sign-in here"
    set_table_key(config, "workshop", "username", "lilgremlin", before="master")
    assert steamsetup.cannot_fetch(config) is None


# I and `steam files` ------------------------------------------------------------------------------------------------

class HandingController(SetupController):
    """A running Huragok takes game_files (the job runs in its background); `handed` says what it was asked."""

    def __init__(self, settings_path: Path, answer: str = "getting Halo 3's files in the background") -> None:
        super().__init__(settings_path)
        self.answer = answer

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        return [("Huragok", Result(self.answer != "not available", self.answer))]


def i_screen(settings_path: Path, keys: list[str], controller: SetupController | None = None) -> tuple[Menu, Any]:
    controller = controller or HandingController(settings_path)
    terminal = FakeTerminal()
    return Menu(controller, terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


def test_i_shows_steamcmd_and_the_game_files_and_gets_them(tmp_path: Path, run) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, _ = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    controller = HandingController(settings_path)
    menu, terminal = i_screen(settings_path, ["2", "1", "x", "ESC"], controller)
    run(menu.screen_steam())
    text = "".join(terminal.output)
    assert "STEAMCMD" in text and "isn't installed where it's expected" in text and "Halo 3's files" in text
    assert f"{ALPHA} (Halo 3's files)" in text and "Copy them from MCC at" in text
    assert "No download: SteamCMD isn't set up (1 sets SteamCMD up)." in text
    assert ("game_files", "all", {"kind": "halo3", "how": "mcc"}) in controller.calls
    assert "getting Halo 3's files in the background" in text


def test_i_gets_them_itself_when_no_huragok_runs(tmp_path: Path, run, monkeypatch) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, _ = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    monkeypatch.setattr(SetupController, "_tool", lambda self, settings: fake_tool(), raising=False)
    menu, terminal = i_screen(settings_path, ["2", "1", "x", "ESC"], HandingController(settings_path, "not available"))
    run(menu.screen_steam())
    assert "Halo 3's files are in" in "".join(terminal.output)
    assert game_files.has_game_files(settings_path.parent / "game")


def test_i_says_when_there_is_no_way_and_what_comes_first(tmp_path: Path, run) -> None:
    settings_path, _ = copied_in(tmp_path)
    menu, terminal = i_screen(settings_path, ["2", "x", "3", "x", "ESC"])
    run(menu.screen_steam())
    text = "".join(terminal.output)
    assert "MCC with these files isn't on this PC" in text and "No download: SteamCMD isn't set up" in text
    assert "has no Halo 3 files yet: get those first" in text


def test_steam_files_from_the_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    settings_path, server = copied_in(tmp_path)
    assert cli.main(["-c", str(settings_path), "steam", "files", "nope"]) == 2
    assert cli.main(["-c", str(settings_path), "steam", "files"]) == 1
    out = capsys.readouterr()
    assert "No way to get Halo 3's files here: MCC isn't on this PC" in out.err + out.out
    mcc = fake_mcc(tmp_path / "MCC")
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    assert cli.main(["-c", str(settings_path), "steam", "files"]) == 0
    out = capsys.readouterr().out
    assert "Halo 3's files are in" in out and "made its content folder" in out  # and what else it lacked
    assert read_toml(server.config_path)["game"] == "../../game" and game_files.has_game_files(settings_path.parent / "game")
    assert cli.main(["-c", str(settings_path), "steam", "files", "--download"]) == 1  # no SteamCMD way


def test_engine_files_after_an_mcc_update_from_steamcmd(tmp_path: Path, run, monkeypatch) -> None:
    settings_path, _ = copied_in(tmp_path)
    game = halo3_files(settings_path.parent / "game")
    (game / "halo3" / "halo3.dll").write_bytes(b"old build")
    menu, terminal = i_screen(settings_path, [])
    run(menu._engine_from_steam(load(settings_path), game, "halo3.dll", [ALPHA]))
    assert "MCC isn't on this PC, and SteamCMD isn't set up" in "".join(terminal.output)
    program = signed_in_steamcmd(settings_path.parent)
    set_table_key(settings_path, "setup", "steam_username", "lilgremlin", before="process")

    def gather(settings: Any, kind: str, source: Any, stage: Path, tool: Any, say: Any, **more: Any) -> Path:
        halo3_files(stage / "game")
        (stage / "game" / "halo3" / "halo3.dll").write_bytes(b"new build")
        return stage / "game"

    monkeypatch.setattr(repair, "gather", gather)
    monkeypatch.setattr(Menu, "_tool", lambda self, settings: asyncio.sleep(0, fake_tool()), raising=False)
    menu, terminal = i_screen(settings_path, ["Y"])
    run(menu._engine_from_steam(load(settings_path), game, "halo3.dll", [ALPHA]))
    assert (game / "halo3" / "halo3.dll").read_bytes() == b"new build"
    assert list((game / "halo3").glob("halo3.dll.before-*"))  # the old one renamed: a running server holds it
    assert not (settings_path.parent / "records" / "engine-download").exists() and program.is_file()


def test_the_supervisor_puts_right_a_folder_it_finds_and_gets_files_when_i_asks(tmp_path: Path, run, caplog) -> None:
    from huragok.huragok import Huragok

    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    set_table_key(settings_path, "setup", "download_game_files", False, before="process")

    async def scenario() -> None:
        with caplog.at_level(logging.WARNING):
            sup = Huragok(load(settings_path))  # finding the folder puts its ban list and content right at once
        assert (server.folder / "bans.json").is_file() and (server.folder / "content" / "Maps").is_dir()
        assert sup.superintendent.fleet.prelaunch == sup.game_files.prelaunch
        nothing = await sup.request("game_files", {"kind": "halo3", "how": "steamcmd"})
        assert not nothing.ok and nothing.message == ("no way to get Halo 3's files here: SteamCMD isn't set up")
        asked = await sup.request("game_files", {"kind": "halo3"})
        assert asked.ok and asked.message.startswith("getting Halo 3's files in the background: copying them from MCC")
        again = await sup.request("game_files", {"kind": "halo3"})
        assert not again.ok and "being got already" in again.message
        await asyncio.gather(*sup.game_files.tasks)
        assert game_files.has_game_files(settings_path.parent / "game")
        assert (await sup.request("game_files", {"kind": "nope"})).message.startswith("'nope' isn't one of")
        assert (await sup.request("game_files_retry", {})).ok
        sup.storage.close()

    run(scenario())
    assert "its ban list (../../bans.json) wasn't there" in caplog.text


def test_game_files_are_never_fetched_into_mccs_own_folder(tmp_path: Path) -> None:
    """A server (or this folder's shared game/) whose game folder is MCC's own install, or a link to it: Huragok reads
    MCC's files but never writes there, so ODST or the campaign aren't fetched into it; the server waits, saying why."""
    settings = load(setup_root(tmp_path))
    mcc = tmp_path / "Steam" / "steamapps" / "common" / "Halo The Master Chief Collection"
    (mcc / "MCC").mkdir(parents=True)
    (mcc / "halo1").mkdir()
    (mcc / "mcclauncher.exe").write_bytes(b"MZ")
    assert repair.into_mcc(mcc) and repair.into_mcc(mcc / "halo3") and not repair.into_mcc(tmp_path / "game")
    paths = repair.read_paths(settings.servers[0].folder, settings.config_name)
    assert paths is not None
    assert repair.target_for(settings, paths, "odst") == (paths.game, "")  # beside its own Halo 3: fine
    shutil.rmtree(paths.game, ignore_errors=True)
    shutil.copytree(mcc, paths.game)  # its game folder is MCC's (a junction to it resolves there): not there
    assert repair.target_for(settings, paths, "odst") == (settings.home / "game", "")  # this folder's own game/
    shutil.rmtree(settings.home / "game", ignore_errors=True)
    shutil.copytree(mcc, settings.home / "game")  # and that one is MCC's too: nowhere, said why
    target, why = repair.target_for(settings, paths, "odst")
    assert target is None and "is MCC's own folder (or a link to it)" in why
    assert repair.shared_target(settings, "halo3")[0] is None
