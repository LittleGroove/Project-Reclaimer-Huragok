"""What a host sends when something goes wrong far away (the first Mac): `report`'s zip, the computer's facts, what
stops a server program from running, a signal said by name, and the start's lines that say where it runs."""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import free_port, write_root
from testkit import os_as, system_as
from testkit.fleet import FakeBackend, build_world, cycle

from huragok.commandline import cli, run as run_module
from huragok.commandline import report as report_module
from huragok.core import desktop, sysinfo
from huragok.core.settings import load
from huragok.servers.backends import ProcessBackend
from huragok.core.events import EventBus
from huragok.servers.state import ServerState

KEEP_MAC_AWAKE = desktop.keep_mac_awake  # conftest's never_this_pcs_caffeinate stands it down in each test
ELF_PROGRAM = b"\x7fELF\x02\x01\x01" + b"\0" * 57
MAC_PROGRAM = b"\xcf\xfa\xed\xfe" + b"\0" * 60


@pytest.fixture
def at_the_mac(monkeypatch):
    monkeypatch.setattr(sysinfo, "os", os_as("posix"))
    monkeypatch.setattr(desktop, "os", os_as("posix"))  # the LaunchAgent's path, not this PC's Startup folder
    system_as(monkeypatch, "macos")


def program(folder: Path, data: bytes, *, runnable: bool = True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "project-reclaimer"
    path.write_bytes(data)
    path.chmod(path.stat().st_mode | stat.S_IXUSR if runnable else stat.S_IRUSR | stat.S_IWUSR)
    return path


# A signal by name -------------------------------------------------------------------------------------------------

def test_an_exit_code_says_a_signal_by_name() -> None:
    assert sysinfo.exit_text(1) == "exit code 1"
    assert sysinfo.exit_text(3221225477) == "exit code 0xc0000005"  # Windows' access violation
    assert sysinfo.exit_text(-11) == "ended by signal 11 (SIGSEGV)"
    if hasattr(signal, "SIGKILL"):
        assert sysinfo.exit_text(-9) == "ended by signal 9 (SIGKILL)"
    assert sysinfo.exit_text(-200) == "ended by signal 200"  # no such signal
    assert sysinfo.signal_hint(1, 3) == "" and sysinfo.signal_hint(None, 3) == ""


def test_signal_9_at_once_on_a_mac_names_what_macos_does(monkeypatch) -> None:
    system_as(monkeypatch, "macos")
    assert "won't let it run (quarantine" in sysinfo.signal_hint(-9, 2)
    assert "forced end" in sysinfo.signal_hint(-9, 3600)  # after an hour: not the quarantine
    system_as(monkeypatch, "linux")
    assert "forced end" in sysinfo.signal_hint(-9, 2)
    assert "crashed" in sysinfo.signal_hint(-11, 50)


# What stops a server program --------------------------------------------------------------------------------------

def test_a_mac_names_the_linux_program_a_missing_run_mark_and_the_quarantine(tmp_path: Path, monkeypatch,
                                                                             at_the_mac) -> None:
    asked: list[tuple[str, ...]] = []

    def ask(*command, **_):
        asked.append(command)
        return (0, "0083;6750a1b2;Safari;") if command[0] == "xattr" else None

    monkeypatch.setattr(sysinfo, "ask", ask)
    monkeypatch.setattr(sysinfo, "_quarantine_seen", {})
    linux = program(tmp_path / "linux", ELF_PROGRAM)
    found = sysinfo.program_problems(linux)
    assert found[0] == (f"{linux} is the Linux program: macOS needs project-reclaimer from Project Reclaimer's "
                        "-macos-aarch64.zip download")
    mac = program(tmp_path / "mac", MAC_PROGRAM)
    found = sysinfo.program_problems(mac)
    assert found == [f"macOS may stop project-reclaimer the moment it starts (signal 9): it came from a web browser's "
                     f"download and still carries macOS's quarantine. Take it off once, in Terminal: xattr -dr "
                     f"com.apple.quarantine \"{mac.parent}\""]
    assert ("xattr", "-p", "com.apple.quarantine", str(mac)) in asked
    count = len(asked)
    sysinfo.program_problems(mac)
    assert len(asked) == count  # asked once while the file is as it was
    monkeypatch.setattr(sysinfo.os, "access", lambda path, mode: False)
    monkeypatch.setattr(sysinfo, "ask", lambda *command, **_: (1, "No such xattr"))
    monkeypatch.setattr(sysinfo, "_quarantine_seen", {})
    assert sysinfo.program_problems(mac) == [f"project-reclaimer isn't marked as a program (unpacked without it): in "
                                             f"a terminal, chmod +x \"{mac}\""]


def test_linux_names_the_mac_program_and_windows_and_exe_files_are_left_alone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sysinfo, "os", os_as("posix"))
    system_as(monkeypatch, "linux")
    monkeypatch.setattr(sysinfo, "ask", lambda *command, **_: pytest.fail("no xattr off a Mac"))
    mac = program(tmp_path / "mac", MAC_PROGRAM)
    assert sysinfo.program_problems(mac)[0].endswith("is the macOS program: Linux needs project-reclaimer from "
                                                     "Project Reclaimer's -linux-<machine>.tar.gz download")
    exe = tmp_path / "project-reclaimer.exe"
    exe.write_bytes(b"MZ\x90\0")
    assert sysinfo.program_problems(exe) == []  # 0.9's .exe: wine's business, said elsewhere
    assert sysinfo.program_problems(tmp_path / "missing") == []
    monkeypatch.setattr(sysinfo, "os", os_as("nt"))
    assert sysinfo.program_problems(mac) == []


def test_check_and_a_failed_start_name_what_stops_the_program(tmp_path: Path, monkeypatch, at_the_mac) -> None:
    root = tmp_path / "home"
    write_root(root, [("LG Mac", free_port())], backend="process")
    folder = root / "SERVERS" / "LG Mac"
    program(folder, ELF_PROGRAM)
    settings = load(str(root / "settings.toml"))
    from huragok.servers import backends

    monkeypatch.setattr(backends, "_on_windows", lambda: False)
    monkeypatch.setattr(sysinfo, "ask", lambda *command, **_: (1, ""))
    backend = ProcessBackend(settings, EventBus(), {"LG Mac": ServerState("LG Mac")})
    monkeypatch.setattr(backend, "executable_for", lambda server: folder / "project-reclaimer")
    server = settings.servers[0]
    linux = f"{folder / 'project-reclaimer'} is the Linux program"
    assert any(problem.startswith(linux) for problem in backend.problems(server))
    assert backend.program_problems(server)[0].startswith(linux)

    def refused(*args, **options):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(backend, "_popen", refused)
    from huragok.servers.backends import LifecycleError

    with pytest.raises(LifecycleError) as raised:
        backend.command_for = lambda server: [str(folder / "project-reclaimer"), "dedicated", "run"]
        import asyncio

        asyncio.run(backend.start(server))
    assert "Exec format error" in str(raised.value) and "is the Linux program" in str(raised.value)


def test_a_server_killed_at_once_logs_the_signal_and_maybe_why(tmp_path: Path, monkeypatch, run, caplog) -> None:
    class Killed(FakeBackend):
        def exit_code(self, server):
            return -9

        def program_problems(self, server):
            return ["macOS may stop project-reclaimer the moment it starts (signal 9): the quarantine"]

    system_as(monkeypatch, "macos")
    clock, settings, backend, fleet, manager, client, state, seen = build_world(tmp_path, monkeypatch,
                                                                                backend_type=Killed)

    async def scenario() -> None:
        fleet.boot()
        await cycle(manager, clock)
        backend.up.clear()  # macOS ended it as it started: the quarantine
        await cycle(manager, clock, 1)
        text = caplog.text
        assert "the server exited while it was starting (ended by signal 9" in text
        assert "macOS ends a program with signal 9 when it won't let it run" in text
        assert "maybe why: macOS may stop project-reclaimer the moment it starts" in text
        caplog.clear()
        await cycle(manager, clock, 5)
        assert manager.phase == "booting"
        state.connected = True
        await cycle(manager, clock)
        assert manager.phase == "running"
        backend.up.clear()  # later: ended while it ran
        state.connected = False
        await cycle(manager, clock, 3600)
        text = caplog.text
        assert "the server exited on its own after" in text and "(ended by signal 9" in text
        assert "maybe why: macOS may stop project-reclaimer the moment it starts" in text

    caplog.set_level(logging.INFO)
    run(scenario())


# This computer ----------------------------------------------------------------------------------------------------

def test_the_facts_say_the_system_and_python_everywhere(tmp_path: Path) -> None:
    rows = dict(sysinfo.facts(tmp_path))
    assert rows["system"] == sysinfo.system_line() and rows["python"].startswith("Python 3.")
    assert "GB free" in rows["disk"]


def test_a_macs_facts_ask_its_own_tools(tmp_path: Path, monkeypatch, at_the_mac) -> None:
    monkeypatch.setattr(sysinfo.platforms, "apple_silicon", lambda: True)
    monkeypatch.setattr(sysinfo.platform, "mac_ver", lambda: ("15.3", ("", "", ""), "arm64"))
    monkeypatch.setattr(sysinfo.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sysinfo, "socket_table_readable", lambda: False)
    monkeypatch.setattr(sysinfo.shutil, "which", lambda name: "/usr/bin/caffeinate" if name == "caffeinate" else
                        f"/usr/bin/{name}")
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(sysinfo.os, "getuid", lambda: 501, raising=False)
    answers = {
        "/usr/bin/arch": (1, "arch: posix_spawnp: /usr/bin/true: Bad CPU type in executable"),
        "pmset": (0, "System-wide power settings:\n Currently in use:\n standby 1\n sleep   1 (sleep prevented by "
                     "caffeinate)\n displaysleep 10"),
        sysinfo.FIREWALL: (0, "Firewall is disabled. (State = 0)"),
        "defaults": (1, "The domain/default pair of (/Library/Preferences/com.apple.loginwindow, autoLoginUser) does "
                        "not exist"),
    }
    monkeypatch.setattr(sysinfo, "ask", lambda *command, **_: answers.get(command[0]))
    rows = dict(sysinfo.facts(tmp_path, ask_tools=True))
    assert rows["system"] == "macOS 15.3 on Apple Silicon (arm64)"
    assert rows["rosetta 2"].startswith("not installed: SteamCMD won't run until `softwareupdate --install-rosetta")
    assert rows["keep awake"] == "caffeinate at /usr/bin/caffeinate"
    assert rows["socket table"].startswith("root only (normal on macOS)")
    assert rows["launch agent"] == "none (W is off)"
    assert rows["sleep"].startswith("after 1 min idle (caffeinate keeps it up while Huragok runs; (sleep prevented")
    assert rows["firewall"] == "Firewall is disabled. (State = 0)"
    assert rows["automatic login"].startswith("off (a Mac that restarts waits at the login screen")
    assert "launchd" not in rows  # no LaunchAgent written
    agent = desktop.startup_path()
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(b"<plist/>")
    answers["launchctl"] = (0, "gui/501/io.github.littlegroove.huragok = {\n\tactive count = 1\n\tstate = running\n"
                               "\truns = 3\n\tpid = 812\n\tlast exit code = 1\n}")
    rows = dict(sysinfo.facts(tmp_path, ask_tools=True))
    assert rows["launchd"] == "loaded: state running, runs 3, pid 812, last exit code 1"
    answers["launchctl"] = (113, "Could not find service")
    assert sysinfo.launchd_text() == "not loaded (it loads at the next login)"
    answers["pmset"] = (0, " sleep                0\n")
    assert dict(sysinfo.facts(tmp_path, ask_tools=True))["sleep"] == "never (pmset sleep 0)"


def test_launchd_says_it_started_huragok(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("HURAGOK_STARTED_BY", "launchd")
    assert sysinfo.started_by() == "launchd (Huragok's LaunchAgent: W)"
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(desktop, "mac_path", lambda: "/usr/bin:/bin")
    import plistlib

    job = plistlib.loads(desktop.mac_launch_agent(tmp_path / "servers"))
    assert job["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin", "HURAGOK_STARTED_BY": "launchd"}


def test_the_start_says_on_what_computer_and_mac(tmp_path: Path, monkeypatch, caplog) -> None:
    settings = load(str(write_root(tmp_path, [("Alpha", free_port())])))
    caplog.set_level(logging.INFO)
    run_module.summary(settings)
    assert f"on {sysinfo.system_line()} · Python " in caplog.text and "This Mac" not in caplog.text
    caplog.clear()
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(sysinfo, "mac_summary", lambda: ["Rosetta 2 installed", "W off"])
    run_module.summary(settings)
    assert "This Mac: Rosetta 2 installed · W off" in caplog.text


def test_a_crash_python_cant_catch_goes_to_logs_crash_log(tmp_path: Path, monkeypatch) -> None:
    settings = load(str(write_root(tmp_path, [("Alpha", free_port())])))
    enabled: list[object] = []
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: True)  # pytest has it: left alone
    monkeypatch.setattr(faulthandler, "enable", lambda **options: enabled.append(options["file"]))
    run_module.keep_crash_log(settings)
    assert enabled == []
    monkeypatch.setattr(faulthandler, "is_enabled", lambda: False)
    monkeypatch.setattr(run_module, "_crash_file", None)
    run_module.keep_crash_log(settings)
    assert len(enabled) == 1 and Path(enabled[0].name) == settings.logs / "crash.log"
    enabled[0].close()


def test_check_says_the_computer_and_on_a_mac_what_it_needs(tmp_path: Path, monkeypatch, capsys) -> None:
    path = write_root(tmp_path, [("Alpha", free_port())])
    cli.main(["-c", str(path), "check"])
    text = capsys.readouterr().out
    assert "Computer" in text and sysinfo.system_line() in text and "This Mac" not in text
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(sysinfo, "facts", lambda *a, **k: [("system", "macOS 15.3"), ("rosetta 2", "not installed: x"),
                                                           ("socket table", "root only")])
    cli.main(["-c", str(path), "check"])
    text = capsys.readouterr().out
    assert "This Mac" in text and "rosetta 2" in text and "root only" in text


# report -----------------------------------------------------------------------------------------------------------

def test_secrets_are_taken_out_and_the_rest_kept() -> None:
    text = report_module.redact('password = "abc"\nuse_host_key = true\nrcon = { password = \'x\', port = 4 }\n'
                                'token=abc123 # note\nkey_count = 5\nname = "LG"\n# password = "old"\n')
    assert text == ('password = "(taken out)"\nuse_host_key = true\nrcon = { password = "(taken out)", port = 4 }\n'
                    'token="(taken out)" # note\nkey_count = 5\nname = "LG"\n# password = "(taken out)"\n')


def test_a_report_holds_the_logs_and_settings_but_no_secret(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    path = write_root(home, [("LG Mac", free_port())], body='[stats]\napi_key = "s3cret-key"\n')
    logs = home / "logs"
    logs.mkdir()
    (logs / "huragok.log").write_text("2026-11-27 10:00:00 INFO    superintendent: Huragok 0.7.0 on macOS\n",
                                      encoding="utf-8")
    (logs / "huragok.log.1").write_text("older\n", encoding="utf-8")
    (logs / "console-lg-mac.log").write_text("the server's own lines\n", encoding="utf-8")
    (logs / "faulted-lg-mac.txt").write_text("stopped trying\n", encoding="utf-8")
    (logs / "crash.log").write_text("Fatal Python error: Segmentation fault\n", encoding="utf-8")
    (logs / "temp-discord").mkdir()
    (logs / "temp-discord" / "chat.log").write_text("someone's chat", encoding="utf-8")
    (home / "discord.toml").write_text('token = "discord-token"\n', encoding="utf-8")
    records = home / "records"
    (records / "servers").mkdir(parents=True)
    (records / "discord-state.json").write_text('{"token": "x"}', encoding="utf-8")
    (records / "temp-discord.key").write_text("key", encoding="utf-8")
    (records / "running-servers.json").write_text("{}", encoding="utf-8")
    (records / "servers" / "lg-mac.json").write_text('{"wanted": "running"}', encoding="utf-8")
    data = home / "SERVERS" / "LG Mac" / "data" / "lg-mac"
    (data / "stats-key.json").write_text('{"secret": "x"}', encoding="utf-8")
    (data / "server.log").write_text("server.log lines\n", encoding="utf-8")
    assert cli.main(["-c", str(path), "report"]) == 0
    said = capsys.readouterr().out
    written = sorted(home.glob("huragok-report-*.zip"))
    assert len(written) == 1 and written[0].name in said and "Send that file to whoever is helping" in said
    with zipfile.ZipFile(written[0]) as zip_file:
        names = set(zip_file.namelist())
        everything = b"".join(zip_file.read(name) for name in names)
        settings_text = zip_file.read("settings.toml").decode()
        dedicated = zip_file.read("servers/lg-mac/dedicated.toml").decode()
        computer = zip_file.read("computer.txt").decode()
        check = zip_file.read("check.txt").decode()
    assert {"computer.txt", "check.txt", "report.txt", "settings.toml", "logs/huragok.log", "logs/huragok.log.1",
            "logs/console-lg-mac.log", "logs/faulted-lg-mac.txt", "logs/crash.log", "records/running-servers.json",
            "records/servers/lg-mac.json", "servers/lg-mac/dedicated.toml", "servers/lg-mac/server.log"} <= names
    assert not any("discord" in name or "stats-key" in name or name.endswith(".key") for name in names)
    for secret in (b"discord-token", b"s3cret-key", b"someone's chat", b"PASSWORD"):
        assert secret not in everything
    from conftest import PASSWORD

    assert PASSWORD.encode() not in everything
    assert 'api_key = "(taken out)"' in settings_text and 'password = "(taken out)"' in dedicated
    assert sysinfo.system_line() in computer and "LG Mac" in computer
    assert "CHECK" in check and "(check's exit code:" in check


def test_a_report_is_written_when_the_settings_cant_be_read(tmp_path: Path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.toml").write_text("this isn't = = toml\n", encoding="utf-8")
    (home / "logs").mkdir()
    (home / "logs" / "launcher.log").write_text("Huragok stopped unexpectedly\n", encoding="utf-8")
    (home / "logs" / "start.log").write_text("Huragok needs Python 3.11 or newer\n", encoding="utf-8")
    path, report = report_module.write_report(str(home / "settings.toml"))
    with zipfile.ZipFile(path) as zip_file:
        names = set(zip_file.namelist())
        computer = zip_file.read("computer.txt").decode()
        check = zip_file.read("check.txt").decode()
    assert {"logs/launcher.log", "logs/start.log", "settings.toml"} <= names
    assert "settings  couldn't be read:" in computer and check.startswith("check wasn't run:")


def test_a_report_on_a_mac_has_the_launch_agent_and_what_launchd_says(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    path = write_root(home, [("LG Mac", free_port())])
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(desktop, "os", os_as("posix"))
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(sysinfo, "facts", lambda *a, **k: [("system", "macOS 15.3")])
    monkeypatch.setattr(sysinfo, "launchd_text", lambda: "loaded: state running, runs 1")
    asked: list[tuple[str, ...]] = []
    monkeypatch.setattr(sysinfo, "ask", lambda *command, **_: asked.append(command) or
                        (0, "2026-11-27 10:00:01 syspolicyd: project-reclaimer: rejected"))
    agent = desktop.startup_path()
    agent.parent.mkdir(parents=True, exist_ok=True)
    agent.write_bytes(desktop.mac_launch_agent(home))
    reports = tmp_path / "user" / "Library" / "Logs" / "DiagnosticReports"
    reports.mkdir(parents=True)
    (reports / "project-reclaimer-2026-11-27-100000.ips").write_text('{"crash": "SIGSEGV"}', encoding="utf-8")
    (reports / "Safari-2026-11-27-100000.ips").write_text("not ours", encoding="utf-8")
    old = reports / "project-reclaimer-2026-11-01-100000.ips"
    old.write_text("a month old", encoding="utf-8")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    written, _ = report_module.write_report(str(path))
    with zipfile.ZipFile(written) as zip_file:
        names = set(zip_file.namelist())
        assert zip_file.read(f"mac/{agent.name}") == agent.read_bytes()
        assert zip_file.read("mac/launchd.txt").decode() == "loaded: state running, runs 1\n"
        assert zip_file.read("mac/crash-reports/project-reclaimer-2026-11-27-100000.ips") == b'{"crash": "SIGSEGV"}'
        assert "syspolicyd: project-reclaimer: rejected" in zip_file.read("mac/system-log.txt").decode()
    assert not any("Safari" in name or "2026-11-01" in name for name in names)  # others' crashes, and old ones
    assert any(command[:2] == ("log", "show") for command in asked)


# The start loop and the LaunchAgent say what they couldn't do -----------------------------------------------------

def test_a_mac_that_cant_be_kept_awake_says_so(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert KEEP_MAC_AWAKE().startswith("Couldn't keep this Mac awake (caffeinate isn't on the PATH)")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/caffeinate")

    def broken(*args, **options):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(desktop.subprocess, "Popen", broken)
    assert KEEP_MAC_AWAKE().startswith("Couldn't keep this Mac awake (caffeinate wouldn't start: [Errno 13]")


def test_a_launchctl_that_says_no_is_in_the_answer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(desktop, "os", os_as("posix"))
    system_as(monkeypatch, "macos")
    monkeypatch.setattr(desktop.Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(desktop.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda args, **_: SimpleNamespace(returncode=5, stdout="", stderr="Input/output error"))
    (tmp_path / "home").mkdir()
    said = desktop.enable_startup(tmp_path / "home")
    assert said.startswith("Huragok will now start when you log in")
    assert said.endswith("(launchctl enable didn't work: Input/output error; that matters only if it was turned off "
                         "by hand)")
