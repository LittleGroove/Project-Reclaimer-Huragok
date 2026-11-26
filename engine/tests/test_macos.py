"""Huragok on a Mac (Apple Silicon, as the host's Mac mini would be): each place macOS differs from Linux, pinned
with system_as so these run the same on every runner. plans/macos-host.md has the findings they follow."""

from __future__ import annotations

import io
import json
import os
import plistlib
import shlex
import socket
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import free_port, write_root
from testkit import os_as, system_as

from huragok import steam
from huragok.content import upkeep
from huragok.core import desktop, platforms, priority
from huragok.core.events import EventBus
from huragok.core.settings import load
from huragok.program import dedicated, releases
from huragok.program.dedicated import DedicatedError, tool_for
from huragok.servers import backends, ports
from huragok.servers.backends import ProcessBackend
from huragok.servers.state import ServerState

PE = b"MZ" + bytes(62)  # a Windows program (0.9's project-reclaimer.exe)
ARM64 = bytes.fromhex("cffaedfe0c000001") + bytes(56)  # macOS's own program (arm64), as far as its first bytes go
X86_64 = bytes.fromhex("cffaedfe07000001") + bytes(56)  # Valve's steamcmd for macOS: an Intel program
DONE = 'tell application "Terminal" to do script "'
# The real lookups, taken before conftest's not_this_pcs_installs hides them: these tests give them a home folder
# of their own (Path.home), so nothing of this PC's is read.
REAL = {"candidates": steam.candidates, "_steam_roots": steam._steam_roots, "depot_folders": steam.depot_folders}
KEEP_MAC_AWAKE = desktop.keep_mac_awake  # conftest's never_this_pcs_caffeinate stands it down for every other test
SIGNED_IN = '"InstallConfigStore"\n{\n\t"Accounts"\n\t{\n\t\t"host"\n\t\t{\n\t\t}\n\t}\n}\n'


def applescript_text(literal: str) -> str:
    """The text an AppleScript "..." literal stands for (backslash escapes)."""
    out, chars = [], iter(literal)
    for char in chars:
        out.append(next(chars) if char == "\\" else char)
    return "".join(out)


def terminal_line(args: list[str]) -> str:
    """The shell line an osascript call asks Terminal to run."""
    assert args[:2] == ["osascript", "-e"] and args[2].startswith(DONE) and args[2].endswith('"')
    assert args[3:] == ["-e", 'tell application "Terminal" to activate']
    return applescript_text(args[2][len(DONE):-1])


@pytest.fixture
def at_the_mac(monkeypatch):
    """Signed in at the Mac itself, not over SSH."""
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)


def test_which_system_it_is(monkeypatch) -> None:
    monkeypatch.setattr(platforms, "os", os_as("posix"))
    monkeypatch.setattr(platforms, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(platforms, "platform", SimpleNamespace(machine=lambda: "arm64"))
    assert platforms.system() == "macos" and platforms.system_name() == "macOS" and platforms.apple_silicon()
    monkeypatch.setattr(platforms, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    assert platforms.is_macos() and not platforms.apple_silicon()  # an Intel Mac: Project Reclaimer has no program
    monkeypatch.setattr(platforms, "sys", SimpleNamespace(platform="linux"))
    assert platforms.system() == "linux" and platforms.system_name() == "Linux" and not platforms.is_macos()
    monkeypatch.setattr(platforms, "os", os_as("nt"))
    assert platforms.system() == "windows" and platforms.system_name() == "Windows"


# The server program -----------------------------------------------------------------------------------------------

def test_a_mac_is_told_of_the_macos_download_and_its_checksum_is_the_zips(monkeypatch) -> None:
    """Huragok on a Mac named the Windows .exe as the newest release's program (only Linux had its own), and took
    GitHub's checksum of it for the program's: 0.10 ships a -macos-aarch64.zip for Apple Silicon."""
    names = ["project-reclaimer-dedicated-v0.10.10.exe", "project-reclaimer-dedicated-v0.10.10-linux-x86_64.tar.gz",
             "project-reclaimer-dedicated-v0.10.10-linux-aarch64.tar.gz",
             "project-reclaimer-dedicated-v0.10.10-macos-aarch64.zip",
             "project-reclaimer-client-v0.10.10-macos-aarch64.zip",
             "SHA256SUMS.txt"]
    listing = {"tag_name": "v0.10.10", "html_url": "https://example/r", "body": "",
               "assets": [{"name": n, "browser_download_url": f"https://example/{n}", "size": 10,
                           "digest": "sha256:" + "ab" * 32} for n in names]}
    monkeypatch.setattr(releases, "_get", lambda *a, **k: json.dumps(listing).encode())
    monkeypatch.setattr(releases, "os", os_as("posix"))
    monkeypatch.setattr(releases, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(releases, "platform", SimpleNamespace(machine=lambda: "arm64"))
    release = releases.latest_program_release()
    assert release is not None and release.exe_name == "project-reclaimer-dedicated-v0.10.10-macos-aarch64.zip"
    assert release.exe_url.endswith("-macos-aarch64.zip")
    assert releases.is_archive(release.exe_name)  # the zip's checksum: never remembered as the program's
    assert not releases.is_archive("project-reclaimer-dedicated-v0.10.10.exe")
    monkeypatch.setattr(releases, "platform", SimpleNamespace(machine=lambda: "x86_64"))  # an Intel Mac
    assert releases.latest_program_release().exe_name == "project-reclaimer-dedicated-v0.10.10.exe"


def test_a_mac_runs_its_own_program_and_says_so_when_only_the_exe_is_there(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dedicated, "os", os_as("posix"))
    monkeypatch.setattr(backends, "_on_windows", lambda: False)
    system_as(monkeypatch, "macos")
    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], 'backend = "process"\n', backend=None,
                               exe=True))
    server = settings.servers[0]
    (server.folder / "project-reclaimer.exe").write_bytes(PE)
    backend = ProcessBackend(settings, EventBus(), {"Alpha": ServerState("Alpha")})
    monkeypatch.setattr(backends.shutil, "which", lambda name: None)
    (problem,) = [p for p in backend.problems(server) if "wine" in p]
    assert "on macOS project-reclaimer.exe (a Windows program: Project Reclaimer 0.9)" in problem
    assert "Project Reclaimer 0.10 has a macOS program of its own" in problem and "Linux" not in problem
    native = server.folder / "project-reclaimer"
    native.write_bytes(ARM64)
    assert backend.command_for(server) == [str(native), "dedicated", "run", "--config", "dedicated.toml"]
    assert backend.problems(server) == []  # no wine needed


def test_a_mac_without_the_program_is_pointed_at_the_macos_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dedicated, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    settings = load(write_root(tmp_path / "root", [("Alpha", free_port())], 'backend = "process"\n', backend=None))
    with pytest.raises(DedicatedError, match="put the project-reclaimer from its macOS download at the root"):
        tool_for(settings)


# The desktop ----------------------------------------------------------------------------------------------------

def test_a_mac_starts_huragok_at_login_with_a_launch_agent(tmp_path: Path, monkeypatch) -> None:
    """W wrote a systemd unit on a Mac and ran systemctl, which isn't there."""
    mac = os_as("posix")
    mac.getuid = lambda: 501
    monkeypatch.setattr(desktop, "os", mac)
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("PATH", "/Users/host/.pyenv/shims:/usr/bin")
    calls: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda args, **_: calls.append(args) or SimpleNamespace(returncode=0, stderr=""))
    home = tmp_path / "my servers"
    home.mkdir()
    path = desktop.startup_path()
    assert path == tmp_path / "home" / "Library" / "LaunchAgents" / "io.github.littlegroove.huragok.plist"
    assert desktop.startup_enabled() is False
    assert desktop.enable_startup(home).startswith("Huragok will now start when you log in")
    job = plistlib.loads(path.read_bytes())
    assert job["Label"] == desktop.LAUNCH_LABEL
    assert job["ProgramArguments"] == ["/bin/sh", str(home / "start.sh"), "start", "--no-menu"]
    assert job["WorkingDirectory"] == str(home) and job["RunAtLoad"] is True
    assert job["KeepAlive"] == {"SuccessfulExit": False}  # a crash starts it again; a close stays closed
    assert job["AbandonProcessGroup"] is True  # the servers outlive Huragok's job
    assert job["ProcessType"] == "Interactive"  # launchd doesn't throttle the servers' frame rate
    assert "StandardOutPath" not in job and "StandardErrorPath" not in job  # huragok.log has every line already
    assert job["EnvironmentVariables"]["PATH"].split(":")[:3] == ["/Users/host/.pyenv/shims", "/usr/bin",
                                                                 "/opt/homebrew/bin"]
    # Never loaded now (a second Huragok beside this one); enabled in case someone disabled it by hand.
    assert calls == [["launchctl", "enable", "gui/501/io.github.littlegroove.huragok"]]
    assert desktop.startup_enabled() is True
    description = desktop.startup_description()
    assert "A LaunchAgent runs start.sh when you log in" in description and "automatic login" in description
    assert desktop.disable_startup() == "Huragok will no longer start at boot"
    assert not path.exists() and len(calls) == 1  # no systemctl on a Mac
    def no_launchctl(*args, **options):
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(desktop.subprocess, "run", no_launchctl)
    assert desktop.enable_startup(home).startswith("Huragok will now start")  # the file alone does it
    assert path.is_file()


def test_a_mac_opens_terminal_and_its_text_editor(tmp_path: Path, monkeypatch, at_the_mac) -> None:
    monkeypatch.setattr(desktop, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    monkeypatch.delenv("DISPLAY", raising=False)  # X11's: never a Mac's way to a window
    started: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **_: started.append(args))
    home = tmp_path / "my servers"
    message = desktop.open_log_window(home, ["watch", '"LG Test Server"'], "LG Test Server console")
    assert message == "opened a Terminal window: LG Test Server console"
    assert shlex.split(terminal_line(started[-1])) == ["sh", str(home / "start.sh"), "watch", "LG Test Server"]
    notes = tmp_path / "commands.toml"
    assert desktop.open_editor(notes) == "opened commands.toml in your text editor"
    assert started[-1] == ["open", "-t", str(notes)]
    assert desktop.open_path(tmp_path) == f"opened {tmp_path}" and started[-1] == ["open", str(tmp_path)]
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 50000 10.0.0.9 22")  # over SSH: a window there helps no one
    count = len(started)
    assert desktop.open_log_window(home, ["watch", "huragok"], "Huragok log").startswith("no window here (over SSH?)")
    assert desktop.open_editor(notes).startswith("edit it in another terminal")
    assert len(started) == count


def test_a_mac_stays_awake_while_huragok_runs(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/caffeinate" if name == "caffeinate" else None)
    started: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **_: started.append(args) or "caffeinate")
    assert KEEP_MAC_AWAKE() == "caffeinate"
    assert started == [["caffeinate", "-is", "-w", str(os.getpid())]]  # ends by itself with this process
    system_as(monkeypatch, "linux")
    assert KEEP_MAC_AWAKE() is None and len(started) == 1


# SteamCMD -------------------------------------------------------------------------------------------------------

def mac_installer() -> bytes:
    """Valve's steamcmd_osx.tar.gz, as far as Huragok cares: steamcmd.sh and the Intel program it starts."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in (("steamcmd.sh", b"#!/usr/bin/env bash\n"), ("steamcmd", X86_64)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_steamcmd_on_a_mac(tmp_path: Path, monkeypatch, at_the_mac) -> None:
    monkeypatch.setattr(steam, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(steam.Path, "home", lambda: tmp_path / "home")
    for name, real in REAL.items():
        monkeypatch.setattr(steam, name, real)
    where = [w for _, w in steam.candidates(tmp_path / "root")]
    assert "~/Steam (Valve's folder for it on macOS)" in where and "the system's package" not in where
    # Steam keeps everything in Application Support: the libraries, SteamCMD's saved sign-in, its depots.
    support = tmp_path / "home" / "Library" / "Application Support" / "Steam"
    (support / "config").mkdir(parents=True)
    (support / "config" / "config.vdf").write_text(SIGNED_IN, encoding="utf-8")
    executable = tmp_path / "home" / "Steam" / "steamcmd.sh"
    assert steam.cached_accounts(executable) == ["host"]
    assert support / "steamapps" / "content" / "app_976730" / "depot_976731" in steam.depot_folders(executable, 976731)
    assert support in steam._steam_roots()
    # A new shell's PATH: zsh reads ~/.zprofile, never ~/.profile.
    assert steam.add_to_path(tmp_path / "steamcmd") == f"added to {tmp_path / 'home' / '.zprofile'} (new shells see it)"
    assert not (tmp_path / "home" / ".profile").exists()
    # The sign-in, in a Terminal window.
    started: list[list[str]] = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **_: started.append(args))
    assert steam.login_window(executable, "host").startswith("a Steam sign-in window opened in Terminal")
    signing_in = f"{shlex.quote(str(executable))} +login host +quit"
    assert terminal_line(started[-1]) == f"{signing_in}; echo; echo 'Done: close this window'"
    monkeypatch.setenv("SSH_TTY", "/dev/ttys001")
    assert steam.login_window(executable, "host").startswith("no window here (over SSH?); run this in another terminal")
    assert len(started) == 1


def test_steamcmd_installs_from_valves_mac_download_and_asks_for_rosetta(tmp_path: Path, monkeypatch) -> None:
    """The Linux tarball was downloaded on a Mac, and its program can't run there. Valve's macOS one is an Intel
    program: without Rosetta 2, macOS says "Bad CPU type in executable", which is turned into what to do."""
    monkeypatch.setattr(steam, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(steam.Path, "home", lambda: tmp_path / "home")
    fetched: list[str] = []
    monkeypatch.setattr(steam, "fetch", lambda url, timeout, accept="": fetched.append(url) or mac_installer())
    monkeypatch.setattr(steam, "run", lambda executable, words, timeout=600.0: subprocess.CompletedProcess(
        [], 0, "Steam Console Client (c) Valve Corporation\nLoading Steam API...OK\n", None))
    target = tmp_path / "steamcmd"
    assert steam.install(target, say=lambda line: None) == target / "steamcmd.sh"
    assert fetched == ["https://steamcdn-a.akamaihd.net/client/installer/steamcmd_osx.tar.gz"]
    if os.name != "nt":
        assert (target / "steamcmd").stat().st_mode & 0o111 and (target / "steamcmd.sh").stat().st_mode & 0o111
    no_rosetta = subprocess.CompletedProcess(
        [], 126, f"{target}/steamcmd.sh: line 37: {target}/steamcmd: Bad CPU type in executable\n", None)
    monkeypatch.setattr(steam, "run", lambda *a, **k: no_rosetta)
    rosetta = "run `softwareupdate --install-rosetta --agree-to-license` in Terminal, then try again"
    with pytest.raises(steam.SteamSetupError, match="SteamCMD for macOS is an Intel program") as error:
        steam.install(tmp_path / "again", say=lambda line: None)
    assert rosetta in str(error.value)
    support = tmp_path / "home" / "Library" / "Application Support" / "Steam" / "config"
    support.mkdir(parents=True)
    (support / "config.vdf").write_text(SIGNED_IN, encoding="utf-8")
    assert steam.signed_in(target / "steamcmd.sh", "host") == (False, steam.needs_rosetta(no_rosetta.stdout))
    assert steam.workshop_download(target / "steamcmd.sh", "host", 3622263149)[1].endswith(rosetta)
    with pytest.raises(steam.SteamSetupError, match="no Rosetta 2"):
        steam.download_depot(target / "steamcmd.sh", "host", 976731)
    assert steam.needs_rosetta("Logged in OK") is None


# Wording ----------------------------------------------------------------------------------------------------------

def test_a_mac_is_told_its_own_way_about_priority_and_the_firewall(tmp_path: Path, monkeypatch) -> None:
    mac = os_as("posix")
    mac.geteuid = lambda: 501
    monkeypatch.setattr(priority, "os", mac)
    monkeypatch.setattr(priority, "_has_sys_nice", lambda: False)
    system_as(monkeypatch, "macos")
    assert priority.why_short("high", "normal") == "above normal needs root on macOS"
    assert priority.unavailable("high") == ("above normal needs root on macOS, which Huragok doesn't have here, so "
                                            "the servers stay at normal")
    assert priority.unavailable("normal") == ""
    monkeypatch.setattr(upkeep, "os", os_as("posix"))
    message = upkeep.open_firewall(["New-NetFirewallRule ..."], tmp_path)
    assert message.startswith("the firewall commands are for Windows; macOS's firewall is off unless it was turned on")
    assert "say Allow when it asks about project-reclaimer" in message and not (tmp_path / "open-firewall.ps1").exists()


# Ports ------------------------------------------------------------------------------------------------------------

def test_a_stopped_servers_game_port_is_tried_by_binding_where_the_table_cant_be_read(monkeypatch) -> None:
    """macOS gives the system's socket table to root alone, so a game port taken by a server Huragok doesn't run
    went unseen: it's tried by binding it instead, at once (a bind doesn't wait like a connect)."""
    monkeypatch.setattr(ports, "listening_ports", lambda: None)
    monkeypatch.setattr(ports, "bound_udp_ports", lambda: None)
    clock = [100.0]
    monkeypatch.setattr(ports, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    port = free_port()
    server = SimpleNamespace(game_port=port)
    watch = ports.PortWatch()
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        holder.bind(("0.0.0.0", port))
        assert watch.game_port_in_use(server) is True  # the first look answers: a first start is covered too
    finally:
        holder.close()
    assert watch.game_port_in_use(server) is True  # kept for a second
    clock[0] += ports.PortWatch.LOOK_EVERY
    assert watch.game_port_in_use(server) is False
    assert watch.game_port_in_use(SimpleNamespace(game_port=0)) is False
