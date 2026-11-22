"""What a server's folder lacks (content/gamefiles.py), and SteamCMD for Halo 3's files: a server folder copied in from
another Huragok folder or PC, as the host's first 0.4.0 test run had (19 October 2026). Since 0.7.0 Huragok only warns
and holds a server without its game files; `repair` (and I) put things right when the host asks."""

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
from testkit.workshop import workshop_root

from huragok import steam as steamsetup
from huragok.commandline import cli
from huragok.content import gamefiles as repair
from huragok.core import versions
from huragok.core.events import Event, EventBus
from huragok.core.logs import RecentLog
from huragok.core.serverwarnings import Warnings, load_rules
from huragok.core.requests import Result
from huragok.core.settings import launcher, load
from huragok.core.tomledit import read_toml, set_table_key, set_top_key
from huragok.games import files as game_files
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


def file_text(folder: Path) -> dict[str, bytes]:
    """Every file under a folder, as it is: to show nothing in it changed."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def rules(files: Any, server: str) -> set[str]:
    return {w.rule.id for w in files.warnings.of(server)}


def watching(settings: Any, **more: Any) -> Any:
    """GameFiles as Huragok runs it, with the warnings catalogue."""
    return repair.GameFiles(settings, EventBus(), fake_tool, warnings=Warnings(load_rules(), settings.records), **more)


# What a folder lacks: warned about, never changed by itself; repaired when the host asks -------------------------

def test_a_server_copied_in_is_warned_about_and_its_folder_left_as_it_is(tmp_path: Path) -> None:
    """0.7.0: a folder copied in without what it named is the host's. Huragok warns (its game files, a ban list that
    isn't here), says what puts each right, and changes nothing; a content folder that's only not there yet the
    program makes itself (no warning)."""
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    before = file_text(server.folder)
    files = watching(settings)
    files.server_added(server, running=False)
    wait = files.prelaunch(server)
    assert wait == f"needs Halo 3's files: press I, or run {launcher()} repair" and not files.tasks  # nothing got by itself
    assert rules(files, ALPHA) == {"game-files-missing", "ban-list-missing"}
    said = {w.rule.id: w.line() for w in files.warnings.of(ALPHA)}
    assert "lacks Halo 3's files" in said["game-files-missing"] and "I gets them" in said["game-files-missing"]
    assert f"{launcher()} repair" in said["game-files-missing"] and "../../bans.json" in said["ban-list-missing"]
    assert file_text(server.folder) == before and not (settings.home / "bans.json").exists()
    assert not (server.folder / "content").exists()  # the program makes it as it starts
    # A content folder or a ban list in a folder that isn't on this PC: warned about too.
    set_top_key(server.config_path, "content", "../../../../elsewhere/content")
    files.look(load(settings_path).server(ALPHA))
    assert "content-folder-missing" in rules(files, ALPHA)
    # Repaired when the host asks: each shown first, a version of its dedicated.toml kept before the first change.
    plan = repair.repairs(settings, server, fake_tool, lambda line: None)
    texts = [r.text for r in plan]
    assert texts[0].startswith("its ban list (../../bans.json) isn't on this PC: one of its own")
    assert texts[1].startswith("its content folder (../../../../elsewhere/content) isn't on this PC")
    assert any(text.startswith("Halo 3's files can't be got here: MCC isn't on this PC") for text in texts)
    done, failed = repair.run_repairs(settings, server, plan, lambda line: None)
    assert failed == [] and len(done) == 2
    own = server.folder / "bans.json"  # every server keeps a ban list of its own
    assert json.loads(own.read_text(encoding="utf-8")) == repair.EMPTY_BANS
    assert read_toml(server.config_path)["bans"] == "bans.json" and read_toml(server.config_path)["content"] == "content"
    assert all((server.folder / "content" / sub).is_dir() for sub in repair.CONTENT_FOLDERS)
    kept = versions.versions(settings.records, "settings", "alpha-server", "dedicated.toml")
    assert [v.how for v in kept] == ["repaired, as the host asked", versions.FIRST]
    assert repair.repairs(settings, server, fake_tool)[0].text.startswith("Halo 3's files can't be got here")


def test_a_server_sharing_a_ban_list_gets_its_own_copy_when_the_host_asks(tmp_path: Path) -> None:
    """A ban list shared with other servers (what 0.4.4 and before made) is the host's choice since 0.7.0: an info
    warning, and `repair` gives the server a copy of its own with everything on it: nobody is unbanned, no admin or
    vote-kick immunity lost. Never as it starts by itself any more."""
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    shared = settings.home / "bans.json"
    first, second, third = "a" * 64, "b" * 64, "c" * 64
    shared.write_text(json.dumps({"players": [{"id": first, "name": "Bob", "reason": "cheating"}], "ips": [],
                                  "vote_kick_immune": [{"id": second, "added_by": "Vergil (RCON)"}]}), encoding="utf-8")
    files = watching(settings)
    files.prelaunch(server)
    assert "ban-list-shared" in rules(files, ALPHA) and "ban-list-missing" not in rules(files, ALPHA)
    assert read_toml(server.config_path)["bans"] == "../../bans.json"  # found: left alone
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
    files = watching(settings)
    assert "needs Halo 3's files" in (files.prelaunch(server) or "")  # held, never pointed there by itself
    assert read_toml(server.config_path)["game"] == "D:/old-pc/reclaimer/game"
    plan = repair.repairs(settings, server, fake_tool, lambda line: None)
    assert any(r.text.startswith("its game folder (D:/old-pc/reclaimer/game) has no Halo 3 files: it uses") and
               'game = "../../game"' in r.text for r in plan)
    done, _ = repair.run_repairs(settings, server, plan, lambda line: None)
    assert read_toml(server.config_path)["game"] == "../../game"
    assert any("its game folder (D:/old-pc/reclaimer/game) has no Halo 3 files here: it uses" in line for line in done)
    assert files.prelaunch(load(settings_path).server(ALPHA)) is None
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

def test_halo3s_files_are_got_when_the_host_asks_and_the_server_starts_on_them(tmp_path: Path, run, caplog) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    settings = load(settings_path)
    released: list[bool] = []
    files = watching(settings, release=lambda: released.append(True))

    async def scenario() -> None:
        assert files.prelaunch(settings.server(ALPHA)) == f"needs Halo 3's files: press I, or run {launcher()} repair"
        assert not files.tasks and not files.getting  # never got by itself, MCC here or not (0.7.0)
        assert files.get("halo3", settings.home / "game")  # I: the host asks
        assert (files.prelaunch(settings.server(ALPHA)) or "").startswith("getting Halo 3's files")
        await asyncio.gather(*files.tasks)
        assert released == [True] and not files.getting and not files.failed
        assert files.prelaunch(settings.server(ALPHA)) is None  # it has them: it starts
        assert "game-files-missing" not in rules(files, ALPHA)

    with caplog.at_level(logging.INFO):
        run(scenario())
    assert game_files.has_game_files(settings.home / "game")
    assert read_toml(server.config_path)["game"] == "../../game"
    assert "Halo 3's files are in" in caplog.text and "the servers waiting for them start" in caplog.text


def test_with_no_mcc_and_no_steamcmd_the_server_waits_and_says_how_to_get_them(tmp_path: Path, run, caplog) -> None:
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    files = watching(settings)

    async def scenario() -> str | None:
        assert files.get("halo3", settings.home / "game")  # the host asked (I), with no way to get them
        await asyncio.gather(*files.tasks)
        return files.prelaunch(settings.server(ALPHA))

    with caplog.at_level(logging.ERROR):
        wait = run(scenario())
    assert wait == (f"needs Halo 3's files: press I, or run {launcher()} repair (the last try: MCC isn't on this PC "
                    "and SteamCMD isn't set up)")
    assert "there's no way to get them here" in caplog.text
    files.try_again()
    assert not files.failed


def test_a_download_is_offered_when_steamcmd_is_signed_in(tmp_path: Path) -> None:
    """Game files are only got when the host asks (0.7.0: [setup] download_game_files is retired), so SteamCMD is one
    of the ways whenever it's signed in; `downloads=False` leaves it out."""
    settings_path, _ = copied_in(tmp_path)
    signed_in_steamcmd(settings_path.parent)
    set_table_key(settings_path, "setup", "steam_username", "lilgremlin", before="process")
    set_table_key(settings_path, "setup", "download_game_files", False, before="process")  # read past
    settings = load(settings_path)
    found, why = repair.sources(settings, "halo3")
    assert [s.how for s in found] == ["steamcmd"] and why == ""
    assert repair.sources(settings, "halo3", downloads=False) == ([], "downloads are left out")
    assert any("[setup] download_game_files does nothing any more" in line for line in settings.retired)
    set_table_key(settings_path, "setup", "steam_username", "someoneelse", before="process")
    assert repair.sources(load(settings_path), "halo3")[1] == "someoneelse isn't signed in to SteamCMD here"


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


def test_a_start_that_failed_for_missing_files_waits_for_them(tmp_path: Path, run) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    settings = load(settings_path)
    halo3_files(settings.home / "game")
    (settings.home / "game" / "halo3" / "maps" / "guardian.map").unlink()  # a copy that left a map out
    bus = EventBus()
    files = repair.GameFiles(settings, bus, fake_tool, warnings=Warnings(load_rules(), settings.records))

    async def scenario() -> None:
        assert files.prelaunch(settings.server(ALPHA)) is None  # halo3.dll and shared.map are there
        bus.publish(Event("server.start_failed", ALPHA, {"why": "it exited", "lines": MISSING}))
        assert files.prelaunch(settings.server(ALPHA)) == f"needs Halo 3's files: press I, or run {launcher()} repair"
        assert not files.tasks and "game-files-missing" in rules(files, ALPHA)  # held and warned; nothing got
        files.get("halo3", settings.home / "game")  # the host asks
        await asyncio.gather(*files.tasks)
        assert files.prelaunch(settings.server(ALPHA)) is None and ALPHA not in files.forced
        # The same failure just after they arrived: said so.
        bus.publish(Event("server.start_failed", ALPHA, {"why": "it exited", "lines": MISSING}))
        assert "still says files are missing" in (files.prelaunch(settings.server(ALPHA)) or "")

    run(scenario())
    assert (settings.home / "game" / "halo3" / "maps" / "guardian.map").is_file()


def test_nothing_is_looked_at_for_a_server_this_pc_doesnt_run(tmp_path: Path) -> None:
    settings_path, server = copied_in(tmp_path)
    settings = load(settings_path)
    settings.backend = "docker"  # its dedicated.toml's paths are the container's
    files = watching(settings)
    assert files.look(server) == [] and files.prelaunch(server) is None and files.warnings.of(ALPHA) == []
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
    assert any("Halo 3, 1 multiplayer maps · Forge's campaign files (no missions) · ODST · no Reach · no CE · "
               "no Halo 2 · used by 1 server(s)" in text for _, text, _ in rows)  # the fake program's version: unknown
    assert repair.shared_target(settings, "odst") == (settings.home / "game", "")
    shutil.rmtree(settings.home / "game")
    target, why = repair.shared_target(settings, "campaign")
    assert target is None and "get those first" in why


# The Workshop: whether a server's SteamCMD can fetch here ------------------------------------------------------------

def test_whether_a_servers_steamcmd_can_fetch_here(tmp_path: Path) -> None:
    settings_path, folder = workshop_root(tmp_path)
    config = folder / "dedicated.toml"
    set_table_key(config, "workshop", "steamcmd", str(tmp_path / "other-pc" / "steamcmd.exe"), before="master")
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
    assert "Halo 3's files are in" in out and "made its content folder" not in out  # nothing else changed (0.7.0)
    assert read_toml(server.config_path)["game"] == "../../game" and game_files.has_game_files(settings_path.parent / "game")
    assert not (server.folder / "content").exists() and not (server.folder / "bans.json").exists()
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


def test_the_supervisor_warns_about_a_folder_it_finds_and_gets_files_when_i_asks(tmp_path: Path, run, caplog) -> None:
    from huragok.huragok import Huragok

    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    before = file_text(server.folder)

    async def scenario() -> None:
        with caplog.at_level(logging.WARNING):
            sup = Huragok(load(settings_path))  # finding the folder: warned about, nothing changed (0.7.0)
        assert file_text(server.folder) == before
        assert {"game-files-missing", "ban-list-missing"} <= {w.rule.id for w in sup.superintendent.warnings.of(ALPHA)}
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
    assert "Its ban list (../../bans.json) isn't on this PC" in caplog.text


def test_repair_from_the_command_line_shows_first_and_asks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mcc = fake_mcc(tmp_path / "MCC")
    settings_path, server = copied_in(tmp_path)
    set_table_key(settings_path, "setup", "mcc_folder", str(mcc), before="process")
    before = file_text(server.folder)
    assert cli.main(["-c", str(settings_path), "repair"]) == 1  # no terminal to say yes: shown, nothing changed
    out = capsys.readouterr().out
    assert "its ban list (../../bans.json) isn't on this PC" in out and "Halo 3's files into" in out
    assert f'repair "{ALPHA}" --yes' in out and file_text(server.folder) == before
    assert cli.main(["-c", str(settings_path), "repair", ALPHA, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Halo 3's files are in" in out and game_files.has_game_files(settings_path.parent / "game")
    assert read_toml(server.config_path)["bans"] == "bans.json"
    assert cli.main(["-c", str(settings_path), "repair"]) == 0
    assert "Nothing to put right" in capsys.readouterr().out


def test_repair_in_the_window_shows_first_and_asks(tmp_path: Path, run) -> None:
    settings_path, server = copied_in(tmp_path)
    halo3_files(settings_path.parent / "game", odst=True)
    set_top_key(server.config_path, "game", "D:/old-pc/game")
    menu, terminal = i_screen(settings_path, ["R", "1", "ENTER", "N", "x", "ESC"])  # shown, then no
    run(menu.screen_steam())
    text = "".join(terminal.output)
    assert "1. its ban list (../../bans.json) isn't on this PC" in text and "Nothing was changed." in text
    assert read_toml(server.config_path)["game"] == "D:/old-pc/game"
    controller = HandingController(settings_path)
    menu, terminal = i_screen(settings_path, ["R", "1", "ENTER", "Y", "x", "ESC"], controller)
    run(menu.screen_steam())
    assert read_toml(server.config_path)["game"] == "../../game" and (server.folder / "bans.json").is_file()
    assert "Done." in "".join(terminal.output) and ("game_files_retry", "all", {}) in controller.calls


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
