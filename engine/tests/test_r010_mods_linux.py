"""Project Reclaimer 0.10: its commands run with their output in a file (into a pipe they never end), mods for every
game it plays (Reach, Combat Evolved and Halo 2 as Halo 3's), and the program Linux has of its own (no wine) found,
run and installed, with Windows left exactly as it was."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import free_port, write_root
from testkit import os_as
from testkit.mods import a_map, a_mod

from huragok.commandline import firstrun
from huragok.content import modcheck, mods, workshopmods
from huragok.content.modcheck import BROKEN, NOTE, WARNING, check_mods
from huragok.core.events import Event, EventBus
from huragok.core.settings import load
from huragok.core.web import UpdateError
from huragok.program import dedicated, releases
from huragok.program.dedicated import Dedicated, Output, find_executable, program_prefix, windows_program, without_wine
from huragok.servers import backends
from huragok.servers.backends import ProcessBackend
from huragok.servers.state import ServerState

ELF = b"\x7fELF" + bytes(60)  # Linux's own program, as far as its first bytes go
PE = b"MZ" + bytes(62)  # a Windows program (0.9's project-reclaimer.exe)
RELEASES = Path(__file__).resolve().parents[3] / "releases" / "v0.10.0"


def texts(report: modcheck.ModReport, level: str | None = None) -> str:
    return "\n".join(f.text for f in report.findings if level is None or f.level == level)


# 0.10's commands never end while their output is a pipe ----------------------------------------------------------

# Like 0.10.0 (verified on this PC): its work done and printed, it waits for a line while its input is a pipe and never
# ends while its output is one; into a file it ends at once.
PIPE_SHY = '''import os, stat, sys, time
sys.stdout.reconfigure(encoding="utf-8")  # as the program writes
def pipe(fd):
    try:
        return stat.S_ISFIFO(os.fstat(fd).st_mode)
    except OSError:
        return False
print('Maps (map = "..."):')
print("  Guardian                 guardian (ID 320)")
print("  caf\\u00e9 line", flush=True)
if pipe(0):
    sys.stdin.readline()
if pipe(1):
    time.sleep(60)
'''


def pipe_shy(tmp_path: Path) -> Dedicated:
    script = tmp_path / "program.py"
    script.write_text(PIPE_SHY, encoding="utf-8")
    return Dedicated(Path(sys.executable), template=[sys.executable, str(script), "dedicated"])


def test_the_fake_never_ends_into_a_pipe(tmp_path: Path) -> None:
    """What the tests below stand on: the stand-in hangs as 0.10 does."""
    tool = pipe_shy(tmp_path)
    process = subprocess.Popen(tool.command("content"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(3)
    finally:
        process.kill()
        process.communicate()


def test_a_command_ends_and_says_everything_although_a_pipe_would_hold_it(tmp_path: Path) -> None:
    tool = pipe_shy(tmp_path)
    started = time.monotonic()
    result = tool.run_sync(tmp_path, "content", timeout=50)
    assert time.monotonic() - started < 25, "it waited as if its output were a pipe"
    assert result.ok and result.code == 0
    assert result.text == 'Maps (map = "..."):\n  Guardian                 guardian (ID 320)\n  café line\n'


def test_a_streamed_command_ends_too_its_lines_as_they_come(tmp_path: Path, run) -> None:
    tool = pipe_shy(tmp_path)
    lines: list[str] = []
    started = time.monotonic()
    result = run(tool.stream(tmp_path, "content", on_line=lines.append, timeout=50))
    assert time.monotonic() - started < 25
    assert result.ok and lines == ['Maps (map = "..."):', "  Guardian                 guardian (ID 320)", "  café line"]
    assert result.text.endswith("café line\n")


def test_the_output_file_is_read_in_whole_lines_and_removed() -> None:
    output = Output()
    path = output.path
    try:
        for data, final, expected in ((b"one\r", False, []), (b"\ntwo \xc3", False, ["one\n"]),
                                      (b"\xa9\rthree\r\n\r\nfour", False, ["two é\n", "three\n", "\n"]),
                                      (b"", True, ["four"])):
            output.writer.write(data)
            output.writer.flush()
            assert output.lines(final=final) == expected
    finally:
        output.handed_over()
        output.close()
    assert not path.exists()


def test_a_program_that_cant_start_leaves_no_output_file(tmp_path: Path, run) -> None:
    tool = Dedicated(tmp_path / "nowhere.exe", template=[str(tmp_path / "nowhere.exe"), "dedicated"])
    before = set(Path(dedicated.tempfile.gettempdir()).glob(f"{dedicated.OUTPUT_PREFIX}*"))
    assert tool.run_sync(tmp_path, "content").code == 127
    assert run(tool.stream(tmp_path, "content", on_line=lambda line: None)).code == 127
    assert set(Path(dedicated.tempfile.gettempdir()).glob(f"{dedicated.OUTPUT_PREFIX}*")) <= before


# Mods for every game ----------------------------------------------------------------------------------------------

def game_folder(tmp_path: Path, *games: str) -> Path:
    """A game folder with Halo 3's files and the named games' (their engine file is what says they're there)."""
    game = tmp_path / "game"
    for key in ("halo3", *games):
        engine = game / modcheck.GAMES[key].engine
        engine.parent.mkdir(parents=True, exist_ok=True)
        engine.write_bytes(b"")
    return game


def other_games_mods(content: Path) -> None:
    mods_folder = content / "Mods"
    a_mod(mods_folder / "reach", {"scarif": b"not a Halo 3 map"}, title="Battlefront Reach", engine="HaloReach")
    a_mod(mods_folder / "ce", {"bloodgulch": b"not a Halo 3 map"}, title="Cursed CE", engine="Halo1")
    a_mod(mods_folder / "h2", {"lockout_dw3": b"not a Halo 3 map"}, title="H2 Uncut", engine="Halo2")
    a_mod(mods_folder / "h4", {"ravine": a_map(9500)}, title="Halo 4 Pack", engine="Halo4")


def test_reach_ce_and_halo_2_mods_play_on_0_10_with_their_games_files(tmp_path: Path) -> None:
    content = tmp_path / "content"
    other_games_mods(content)
    reports = {r.folder.name: r for r in check_mods(content, game_folder(tmp_path, "reach", "halo1", "halo2"), "0.10.0")}
    for name, game, stem in (("reach", "reach", "scarif"), ("ce", "halo1", "bloodgulch"), ("h2", "halo2", "lockout_dw3")):
        report = reports[name]
        assert report.good and report.game == game and report.maps == [stem], texts(report)
        title = modcheck.GAMES[game].title
        assert texts(report, NOTE).startswith(f"made for {title}, played with ")  # its maps aren't read as Halo 3's
        assert "its maps aren't checked here" in texts(report, NOTE)
    assert texts(reports["h4"], BROKEN) == ("it is made for Halo4: the server plays Halo 3's, Halo: Reach's, Combat "
                                            "Evolved's and Halo 2's mods only, and ignores it without a word")
    # Without the game's files in the game folder the server can't play them.
    reports = {r.folder.name: r for r in check_mods(content, game_folder(tmp_path / "bare"), "0.10.0")}
    assert texts(reports["reach"], WARNING).startswith("it is made for Halo: Reach, whose files aren't in the server's "
                                                       "game folder (haloreach): the server can't play it until they are")
    assert not reports["reach"].good


def test_on_0_9_they_are_ignored_as_before(tmp_path: Path) -> None:
    content = tmp_path / "content"
    other_games_mods(content)
    reports = {r.folder.name: r for r in check_mods(content, game_folder(tmp_path, "reach"), "0.9.23")}
    for name, engine in (("reach", "HaloReach"), ("ce", "Halo1"), ("h2", "Halo2"), ("h4", "Halo4")):
        assert texts(reports[name]) == f"it is made for {engine}, not Halo 3: the server ignores it without a word"
        assert reports[name].broken and reports[name].maps == []


def test_their_maps_are_paired_like_halo_3s_and_never_named_alike(tmp_path: Path) -> None:
    content = tmp_path / "content"
    a_mod(content / "Mods" / "one", {"beach": b"x"}, title="One", engine="HaloReach")
    a_mod(content / "Mods" / "two", {"beach": b"x", "extra": b"x"}, {"beach": "beach"}, title="Two", engine="HaloReach")
    (content / "Mods" / "two" / "maps").rename(content / "Mods" / "two" / "Maps")  # as some Reach mods have it
    firefight = a_mod(content / "Mods" / "ff", {"ff_beach": b"x"}, title="Firefight", engine="HaloReach", listed=[])
    info = json.loads((firefight / "ModInfo.json").read_text(encoding="utf-8"))
    info["GameModContents"] = {"FirefightMaps": ["multiplayer/ff_beach.json"]}
    (firefight / "ModInfo.json").write_text(json.dumps(info), encoding="utf-8")
    reports = {r.folder.name: r for r in check_mods(content, None, None)}  # version not known: as 0.10
    assert "extra.map is skipped by the server" in texts(reports["two"], BROKEN)
    assert reports["two"].maps == ["beach"] and "also in" not in texts(reports["one"])  # halo3's clash note only
    assert reports["ff"].good and reports["ff"].maps == ["ff_beach"]
    assert "1 of its maps is for Firefight, which no server setting plays yet" in texts(reports["ff"], NOTE)
    assert "plays only with Reach's files in the server's game folder (haloreach)" in texts(reports["one"], NOTE)


def test_a_workshop_lines_advice_depends_on_the_program() -> None:
    line = "This Workshop item targets an engine other than Halo 3."
    assert mods.workshop_advice(line, "0.9.23") == ("it's a mod for another game in MCC: remove it from the server's "
                                                    "Workshop items")
    for version in ("0.10.0", None):
        assert mods.workshop_advice(line, version).startswith("it's a mod for another game in MCC, which the server's "
                                                              "Workshop installer doesn't take")
    assert mods.workshop_advice("This Workshop item contains no MCC mod package", "0.10.0") == (
        "it isn't an MCC mod (no ModInfo.json): remove it from the server's Workshop items")
    assert mods.workshop_advice("Workshop item is incomplete", "0.10.0") == mods.WORKSHOP_TROUBLE[0][1]
    assert mods.workshop_advice("Workshop 5: ready", None) is None


def test_another_games_skipped_map_isnt_read_as_halo_3s(tmp_path: Path, monkeypatch, caplog) -> None:
    folder = a_mod(tmp_path / "content" / "Mods" / "reach", {"scarif": b"x"}, engine="HaloReach")
    monkeypatch.setattr(mods.halo3_mapfile, "explain", lambda *a, **k: pytest.fail("read as Halo 3's map"))
    settings = SimpleNamespace(server=lambda name: (_ for _ in ()).throw(KeyError(name)), of=lambda server: server)
    watch = mods.ModWatch(settings, EventBus(), lambda *a, **k: None)
    with caplog.at_level(logging.WARNING):
        watch._note_workshop(Event("server.map_skipped", "Alpha", {"path": str(folder / "maps" / "scarif.map"),
                                                                     "reason": "its tag index is damaged"}))
    assert "it's Halo: Reach's map, and Huragok reads Halo 3's only" in caplog.text
    assert modcheck.game_of_map_file(tmp_path / "game" / "haloreach" / "maps" / "x.map").key == "reach"
    assert modcheck.game_of_map_file(tmp_path / "game" / "halo3" / "maps" / "x.map").key == "halo3"


def test_the_program_copies_of_other_games_mods_are_kept_on_0_10_only(tmp_path: Path) -> None:
    content = tmp_path / "content"
    item = content / "Mods" / "Workshop" / "3622263149"
    a_mod(item, {"scarif": b"x"}, engine="HaloReach", workshop=3622263149)
    a_mod(content / "Mods" / "hand", {"golan": b"x"}, engine="HaloReach", workshop=3622263150)
    a_mod(content / "Mods" / "h3", {"canyon": a_map()}, workshop=111)
    asked: list[int] = []

    def version(answer: str | None) -> Any:
        return lambda: asked.append(1) or answer

    found = workshopmods.look_through(content, version("0.10.0"))
    assert list(found.managed) == [3622263149] and list(found.hand) == [111]  # another game's hand copy: left
    assert len(asked) == 1  # asked once, however many such mods
    found = workshopmods.look_through(content, version("0.9.23"))
    assert list(found.managed) == [] and list(found.hand) == [111] and len(found.mods) == 3
    asked.clear()
    (item.parent).rename(content / "elsewhere")
    workshopmods.look_through(content / "Mods" / "h3", version("0.10.0"))
    assert asked == []  # only Halo 3's: the program isn't asked
    (content / "Mods" / "hand" / "maps").rename(content / "Mods" / "hand" / "Maps")
    assert workshopmods.map_files(content / "Mods" / "hand") == {"golan"}


def test_the_mod_watch_passes_each_content_folders_program_version(tmp_path: Path, monkeypatch) -> None:
    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], 'backend = "process"\n', backend=None,
                               exe=True))
    monkeypatch.setattr(mods, "program_version", lambda settings, path: "0.9.23")
    watch = mods.ModWatch(settings, EventBus(), lambda *a, **k: None)
    ((root, names, game, version),) = watch.content_roots()
    assert names == ["Alpha"] and version == "0.9.23" and watch._versions == {"Alpha": "0.9.23"}


# Linux's own program (0.10 on), Windows as it was -----------------------------------------------------------------

def as_linux(monkeypatch: pytest.MonkeyPatch, machine: str = "x86_64") -> None:
    monkeypatch.setattr(dedicated, "os", os_as("posix"))
    monkeypatch.setattr(releases, "os", os_as("posix"))
    monkeypatch.setattr(releases, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(releases, "platform", SimpleNamespace(machine=lambda: machine))
    monkeypatch.setattr(backends, "_on_windows", lambda: False)


def as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dedicated, "os", os_as("nt"))
    monkeypatch.setattr(releases, "os", os_as("nt"))
    monkeypatch.setattr(releases, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(backends, "_on_windows", lambda: True)


def test_windows_runs_and_finds_the_program_as_before(tmp_path: Path, monkeypatch) -> None:
    as_windows(monkeypatch)
    exe, native = tmp_path / "project-reclaimer.exe", tmp_path / "project-reclaimer"
    exe.write_bytes(PE)
    native.write_bytes(ELF)
    assert find_executable(tmp_path) == exe and program_prefix(exe) == [str(exe)]
    assert program_prefix(exe, ["wine", "{executable}", "dedicated"]) == ["wine", str(exe)]  # the host's words, kept
    assert without_wine(["wine", "{executable}", "x"], native) == ["wine", "{executable}", "x"]
    exe.unlink()
    assert find_executable(tmp_path) is None  # Windows never runs Linux's program
    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], 'backend = "process"\n', backend=None,
                               exe=True))
    server = settings.servers[0]
    backend = ProcessBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    assert backend.command_for(server) == [str(server.folder / "project-reclaimer.exe"), "dedicated", "run",
                                           "--config", "dedicated.toml"]
    assert backend.problems(server) == []


def test_linux_runs_its_own_program_by_itself_and_0_9s_through_wine(tmp_path: Path, monkeypatch) -> None:
    as_linux(monkeypatch)
    exe, native = tmp_path / "project-reclaimer.exe", tmp_path / "project-reclaimer"
    exe.write_bytes(PE)
    assert windows_program(exe) and program_prefix(exe) == ["wine", str(exe)]
    native.write_bytes(ELF)
    assert not windows_program(native) and find_executable(tmp_path) == native  # an update to 0.10 put it beside
    assert program_prefix(native) == [str(native)]
    assert program_prefix(native, ["wine", "{executable}", "dedicated"]) == [str(native)]  # a 0.9 command
    assert program_prefix(exe, ["wine", "{executable}", "dedicated"]) == ["wine", str(exe)]
    (tmp_path / "odd.exe").write_bytes(ELF)  # named .exe, yet no Windows program
    assert not windows_program(tmp_path / "odd.exe") and windows_program(tmp_path / "empty.exe")  # can't tell: by name

    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], 'backend = "process"\n', backend=None,
                               exe=True))
    server = settings.servers[0]
    (server.folder / "project-reclaimer.exe").write_bytes(PE)
    backend = ProcessBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    exe = server.folder / "project-reclaimer.exe"
    assert backend.command_for(server)[:2] == ["wine", str(exe)]
    assert any("wine is not installed" in p for p in backend.problems(server))
    (server.folder / "project-reclaimer").write_bytes(ELF)
    native = server.folder / "project-reclaimer"
    assert backend.command_for(server) == [str(native), "dedicated", "run", "--config", "dedicated.toml"]
    assert backend.problems(server) == []  # no wine needed
    backend.command_template = ["wine", "{executable}", "dedicated", "run", "--config", "{config}"]
    assert backend.command_for(server) == [str(native), "dedicated", "run", "--config", "dedicated.toml"]
    assert backend.problems(server) == []
    monkeypatch.setattr(firstrun, "os", os_as("posix"))
    assert firstrun.program_file() == "project-reclaimer"
    assert firstrun.pick_backend([server.folder])[1] == {"executable": "project-reclaimer"}


def release_json(version: str, names: list[str]) -> bytes:
    return json.dumps({"tag_name": f"v{version}", "html_url": "https://example/r", "body": "",
                       "assets": [{"name": n, "browser_download_url": f"https://example/{n}", "size": 10}
                                  for n in names]}).encode()


def test_each_system_gets_its_own_download(tmp_path: Path, monkeypatch) -> None:
    names = ["project-reclaimer-dedicated-v0.10.0.exe", "project-reclaimer-dedicated-v0.10.0-linux-aarch64.tar.gz",
             "project-reclaimer-dedicated-v0.10.0-linux-x86_64.tar.gz",
             "project-reclaimer-dedicated-v0.10.0-macos-aarch64.zip", "project-reclaimer-client-v0.10.0.exe",
             "SHA256SUMS.txt"]
    answer = {"json": release_json("0.10.0", names)}
    monkeypatch.setattr(releases, "_get", lambda url, timeout, accept="", **k: answer["json"])
    as_windows(monkeypatch)
    assert releases.latest_program_release().exe_name == "project-reclaimer-dedicated-v0.10.0.exe"
    as_linux(monkeypatch)
    assert releases.latest_program_release().exe_name == "project-reclaimer-dedicated-v0.10.0-linux-x86_64.tar.gz"
    as_linux(monkeypatch, "aarch64")
    release = releases.latest_program_release()
    assert release.exe_name.endswith("-linux-aarch64.tar.gz") and release.exe_url.endswith("-linux-aarch64.tar.gz")
    as_linux(monkeypatch, "riscv64")  # no program for it: the .exe, as before
    assert releases.latest_program_release().exe_name.endswith(".exe")
    as_linux(monkeypatch)
    answer["json"] = release_json("0.9.23", ["project-reclaimer-dedicated-v0.9.23.exe", "SHA256SUMS.txt"])
    assert releases.latest_program_release().exe_name == "project-reclaimer-dedicated-v0.9.23.exe"  # 0.9: wine


def a_linux_release(path: Path, program: bytes = ELF, extra: list[tarfile.TarInfo] | None = None) -> Path:
    """A release's .tar.gz as 0.10.0's: <folder>/project-reclaimer and its notes, plus whatever `extra` adds."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data in (("project-reclaimer-v0.10.0-linux-x86_64/README.txt", b"read me"),
                           ("project-reclaimer-v0.10.0-linux-x86_64/project-reclaimer", program)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"evil" * (info.size // 4)) if info.isreg() else None)
    return path


def test_the_program_alone_is_unpacked_never_where_the_archive_says(tmp_path: Path) -> None:
    def member(name: str, kind: bytes = tarfile.REGTYPE, link: str = "", size: int = 4) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type, info.linkname, info.size = kind, link, size if kind == tarfile.REGTYPE else 0
        return info

    archive = a_linux_release(tmp_path / "r.tar.gz", extra=[
        member("../project-reclaimer"), member("/tmp/project-reclaimer"), member("../../escape.txt"),
        member("x/y/z/project-reclaimer", tarfile.SYMTYPE, "/etc/passwd")])
    folder = tmp_path / "out" / "unpacked"
    program = releases.unpack_program(archive, folder)
    assert program == folder / "project-reclaimer" and program.read_bytes() == ELF
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == [
        "out", "out/unpacked", "out/unpacked/project-reclaimer", "r.tar.gz"]
    with tarfile.open(tmp_path / "none.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo("README.txt")
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(UpdateError, match="has no project-reclaimer in it"):
        releases.unpack_program(tmp_path / "none.tar.gz", tmp_path / "out2")
    (tmp_path / "broken.tar.gz").write_bytes(b"not gzip")
    with pytest.raises(UpdateError, match="can't be unpacked"):
        releases.unpack_program(tmp_path / "broken.tar.gz", tmp_path / "out3")


@pytest.mark.skipif(not (RELEASES / "project-reclaimer-dedicated-v0.10.0-linux-x86_64.tar.gz").is_file(),
                    reason="the 0.10.0 Linux download isn't on this PC")
def test_the_real_0_10_linux_download_unpacks(tmp_path: Path) -> None:
    program = releases.unpack_program(RELEASES / "project-reclaimer-dedicated-v0.10.0-linux-x86_64.tar.gz", tmp_path)
    assert program.name == "project-reclaimer" and program.stat().st_size == 84541096
    assert program.read_bytes()[:4] == b"\x7fELF" and not windows_program(program)


def linux_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *names: str) -> Any:
    settings = load(write_root(tmp_path / "root", [(n, free_port()) for n in names], 'backend = "process"\n',
                               backend=None, exe=True))
    for server in settings.servers:
        (server.folder / "project-reclaimer.exe").write_bytes(PE + server.name.encode())
    monkeypatch.setattr(releases, "program_version", lambda settings, path: None)  # an old program: never asked
    as_linux(monkeypatch)
    return settings


def test_linux_installs_its_own_program_in_place_of_0_9s(tmp_path: Path, monkeypatch) -> None:
    settings = linux_root(tmp_path, monkeypatch, "Alpha", "Beta")
    archive = a_linux_release(tmp_path / "project-reclaimer-dedicated-v0.10.0-linux-x86_64.tar.gz")
    replaced = releases.install_program(settings, archive, "0.10.0")
    for server in settings.servers:
        native = server.folder / "project-reclaimer"
        assert native in replaced and native.read_bytes() == ELF and find_executable(server.folder) == native
        assert not (server.folder / "project-reclaimer.exe").exists()  # with the old files, for going back
        kept = list(settings.records.glob(f"previous-program/*/{server.folder.name}/project-reclaimer.exe"))
        assert [p.read_bytes() for p in kept] == [PE + server.name.encode()]
        assert not list(server.folder.glob(".huragok-unpacked-*")) and not list(server.folder.glob("*.new"))


def test_a_failed_linux_install_puts_every_windows_program_back(tmp_path: Path, monkeypatch) -> None:
    import os

    settings = linux_root(tmp_path, monkeypatch, "Alpha", "Beta")
    archive = a_linux_release(tmp_path / "release.tar.gz")
    real_replace = os.replace

    def replace(source: Any, target: Any) -> None:
        if str(source).endswith(".new") and Path(target).parent.name == "Beta":
            raise PermissionError(13, "Access is denied", str(target))
        real_replace(source, target)

    monkeypatch.setattr(releases.os, "replace", replace)
    with pytest.raises(UpdateError, match="nothing was changed"):
        releases.install_program(settings, archive, "0.10.0")
    for server in settings.servers:
        assert (server.folder / "project-reclaimer.exe").read_bytes() == PE + server.name.encode()
        assert not (server.folder / "project-reclaimer").exists()
        assert not list(server.folder.glob(".huragok-unpacked-*"))


def test_linux_swaps_one_servers_program_for_its_own(tmp_path: Path, monkeypatch) -> None:
    settings = linux_root(tmp_path, monkeypatch, "Alpha")
    server = settings.servers[0]
    archive = a_linux_release(tmp_path / "release.tar.gz")
    said = releases.swap_program(settings, server, archive, "0.10.0")
    native = server.folder / "project-reclaimer"
    assert said.startswith(f"Project Reclaimer 0.10.0 put in place at {native}; the old one is in ")
    assert native.read_bytes() == ELF and not (server.folder / "project-reclaimer.exe").exists()
    assert releases.swap_program(settings, server, archive, "0.10.0") == \
        "its program file is Project Reclaimer 0.10.0 already"


def test_windows_installs_the_exe_where_it_was(tmp_path: Path, monkeypatch) -> None:
    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], 'backend = "process"\n', backend=None,
                               exe=True))
    monkeypatch.setattr(releases, "program_version", lambda settings, path: None)
    as_windows(monkeypatch)
    server = settings.servers[0]
    (server.folder / "project-reclaimer.exe").write_bytes(PE + b"old")
    (server.folder / "project-reclaimer").write_bytes(ELF)  # a Linux program lying there means nothing to Windows
    new = tmp_path / "project-reclaimer-dedicated-v0.10.0.exe"
    new.write_bytes(PE + b"new")
    assert releases.install_program(settings, new, "0.10.0") == [server.folder / "project-reclaimer.exe"]
    assert (server.folder / "project-reclaimer.exe").read_bytes() == PE + b"new"
    assert (server.folder / "project-reclaimer").read_bytes() == ELF
    new.write_bytes(PE + b"newer")
    said = releases.swap_program(settings, server, new, "0.10.1")
    assert said.startswith(f"Project Reclaimer 0.10.1 put in place at {server.folder / 'project-reclaimer.exe'}")
    assert (server.folder / "project-reclaimer.exe").read_bytes() == PE + b"newer"


def test_old_linux_downloads_are_removed_with_old_exes(tmp_path: Path, monkeypatch, run) -> None:
    from huragok.updates.program import DOWNLOADS, ProgramUpdateService

    folder = tmp_path / DOWNLOADS
    folder.mkdir()
    (folder / "project-reclaimer-dedicated-v0.9.23.exe").write_bytes(b"old")
    (folder / "project-reclaimer-dedicated-v0.10.0-linux-x86_64.tar.gz").write_bytes(b"old")
    release = releases.ProgramRelease("0.10.1", "p", "", "https://x/a.tar.gz",
                                      "project-reclaimer-dedicated-v0.10.1-linux-x86_64.tar.gz")

    def download(release: Any, where: Path) -> Path:
        path = where / release.exe_name
        path.write_bytes(b"new")
        return path

    monkeypatch.setattr(releases, "download_program", download)
    service = SimpleNamespace(_download_lock=asyncio.Lock(), downloads={}, settings=SimpleNamespace(records=tmp_path))
    path = run(ProgramUpdateService._download(service, release))  # type: ignore[arg-type]
    assert [p.name for p in folder.iterdir()] == [path.name]
