"""What this computer is, said plainly, for the start's first lines, `check` and `report` (the file a host sends when
something goes wrong on a computer nobody here can look at, like the first Mac). The start and `check` take the cheap
facts only; `report` also asks the system's own tools (pmset, the firewall, launchctl), each with a short time limit.

And what stops a server program from running on Linux or macOS, said before it is started and again when it dies at
once: not marked as a program, macOS's quarantine on a web browser's download (macOS may end it the moment it starts,
with signal 9), or the program for another system."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import platforms

ROSETTA_FILE = Path("/Library/Apple/usr/share/rosetta/rosetta")  # there once Rosetta 2 is installed
QUARANTINE = "com.apple.quarantine"
FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"
ASK_SECONDS = 5.0

# A program file's first bytes, by the system it is for
ELF = b"\x7fELF"
MACH_O = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe")
WINDOWS_EXE = b"MZ"

_quarantine_seen: dict[tuple[str, int, int], bool] = {}


def ask(*command: str, timeout: float = ASK_SECONDS) -> tuple[int, str] | None:
    """A system tool's exit code and answer (its output and errors, stripped), or None when it isn't here, couldn't be
    run or took too long. Never raises: a fact that can't be had is left out, never a reason to fail."""
    if shutil.which(command[0]) is None and not Path(command[0]).is_file():
        return None
    try:
        done = subprocess.run(list(command), capture_output=True, text=True, errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.returncode, (done.stdout + done.stderr).strip()


# This computer ----------------------------------------------------------------------------------------------------

def system_line() -> str:
    """'macOS 15.3 on Apple Silicon (arm64)', 'Windows 11 (10.0.26200, AMD64)', 'Linux 6.8.0 (x86_64, Ubuntu 24.04)'."""
    machine = platform.machine() or "unknown"
    kind = platforms.system()
    if kind == platforms.MACOS:
        version = platform.mac_ver()[0] or platform.release()
        return f"macOS {version} on {'Apple Silicon' if platforms.apple_silicon() else 'Intel'} ({machine})"
    if kind == platforms.WINDOWS:
        return f"Windows {platform.release()} ({platform.version()}, {machine})"
    try:
        distro = platform.freedesktop_os_release().get("PRETTY_NAME", "")
    except (OSError, AttributeError):
        distro = ""
    return f"Linux {platform.release()} ({machine}{', ' + distro if distro else ''})"


def python_line() -> str:
    return f"Python {platform.python_version()} ({sys.executable})"


def started_by() -> str:
    """What started Huragok, as far as it can tell: launchd (Huragok's LaunchAgent), systemd, an SSH session, a
    terminal. '' when nothing says."""
    from .desktop import LAUNCH_LABEL, STARTED_BY

    if os.environ.get(STARTED_BY) == "launchd" or (platforms.is_macos()
                                                   and os.environ.get("XPC_SERVICE_NAME") == LAUNCH_LABEL):
        return "launchd (Huragok's LaunchAgent: W)"
    if platforms.system() == platforms.LINUX and os.environ.get("INVOCATION_ID"):
        return "systemd"
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return "an SSH session"
    if terminal := os.environ.get("TERM_PROGRAM"):
        return terminal
    return ""


def rosetta() -> bool | None:
    """macOS: True when Rosetta 2 runs Intel programs (SteamCMD for macOS is one); None elsewhere."""
    if not platforms.is_macos():
        return None
    if not platforms.apple_silicon():
        return True  # an Intel Mac runs Intel programs itself
    answer = ask("/usr/bin/arch", "-x86_64", "/usr/bin/true")
    if answer is not None:
        return answer[0] == 0
    return ROSETTA_FILE.exists()


def socket_table_readable() -> bool:
    """Whether the system shows which ports are taken (macOS shows them to root only: Huragok then checks a game
    port by binding it)."""
    import psutil

    try:
        psutil.net_connections("udp")
    except (psutil.AccessDenied, PermissionError):
        return False
    except (psutil.Error, OSError):
        return False
    return True


def mac_summary() -> list[str]:
    """macOS: the start's one line of what matters on a Mac, from cheap facts only. [] elsewhere."""
    if not platforms.is_macos():
        return []
    from . import desktop

    has_rosetta = True if not platforms.apple_silicon() else ROSETTA_FILE.exists()
    agent = desktop.startup_path()
    return ["Rosetta 2 installed" if has_rosetta else "no Rosetta 2 (SteamCMD can't run)",
            "caffeinate found" if shutil.which("caffeinate") else "no caffeinate (the Mac may sleep)",
            "socket table readable" if socket_table_readable() else "socket table root only (ports checked by binding)",
            "W on (LaunchAgent)" if agent is not None and agent.exists() else "W off"]


def facts(home: Path | None = None, *, ask_tools: bool = False) -> list[tuple[str, str]]:
    """This computer, as (name, value) rows. ask_tools: also what the system's tools say (`report` only: each one is
    a process, and the start and `check` must stay quick)."""
    rows = [("system", system_line()), ("python", python_line())]
    if by := started_by():
        rows.append(("started by", by))
    if home is not None:
        try:
            usage = shutil.disk_usage(home)
            rows.append(("disk", f"{usage.free / 1e9:.1f} GB free of {usage.total / 1e9:.0f} GB ({home})"))
        except OSError:
            pass
    try:
        import psutil

        memory = psutil.virtual_memory()
        rows.append(("memory", f"{memory.available / 1e9:.1f} GB free of {memory.total / 1e9:.1f} GB · "
                               f"{os.cpu_count()} processors"))
    except Exception:  # noqa: BLE001 - a fact left out
        pass
    if platforms.is_macos():
        rows.extend(_mac_facts(ask_tools))
    return rows


def _mac_facts(ask_tools: bool) -> list[tuple[str, str]]:
    from . import desktop

    rows = []
    if ask_tools:
        has = rosetta()
    else:
        has = True if not platforms.apple_silicon() else ROSETTA_FILE.exists()
    rows.append(("rosetta 2", "installed (SteamCMD, an Intel program, can run)" if has else
                 "not installed: SteamCMD won't run until `softwareupdate --install-rosetta --agree-to-license`"))
    rows.append(("keep awake", f"caffeinate at {path}" if (path := shutil.which("caffeinate")) else
                 "no caffeinate: the Mac may sleep while Huragok runs"))
    rows.append(("socket table", "readable" if socket_table_readable() else
                 "root only (normal on macOS): Huragok checks a game port by binding it"))
    agent = desktop.startup_path()
    rows.append(("launch agent", f"{agent} (W is on)" if agent and agent.exists() else "none (W is off)"))
    if not ask_tools:
        return rows
    if (answer := ask("pmset", "-g")) is not None:
        rows.append(("sleep", _sleep_text(answer[1])))
    if (answer := ask(FIREWALL, "--getglobalstate")) is not None:
        rows.append(("firewall", answer[1].splitlines()[0] if answer[1] else f"exit {answer[0]}"))
    answer = ask("defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser")
    rows.append(("automatic login", answer[1] if answer is not None and answer[0] == 0 and answer[1] else
                 "off (a Mac that restarts waits at the login screen, and W starts nothing until someone logs in)"))
    if agent and agent.exists():
        rows.append(("launchd", launchd_text()))
    return rows


def _sleep_text(pmset: str) -> str:
    """pmset -g's ` sleep  N` line: 0 never sleeps."""
    for line in pmset.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "sleep":
            if words[1] == "0":
                return "never (pmset sleep 0)"
            return f"after {words[1]} min idle (caffeinate keeps it up while Huragok runs; {' '.join(words[2:])})" \
                .replace("; )", ")")
    return "unknown (pmset -g has no sleep line)"


def launchd_text() -> str:
    """What launchd says of Huragok's LaunchAgent: loaded or not, its state, how many runs, the last exit code."""
    from .desktop import LAUNCH_LABEL

    try:
        uid = os.getuid()  # type: ignore[attr-defined]  # not on Windows
    except AttributeError:
        return "n/a"
    answer = ask("launchctl", "print", f"gui/{uid}/{LAUNCH_LABEL}")
    if answer is None:
        return "launchctl didn't answer"
    if answer[0] != 0:
        return "not loaded (it loads at the next login)"
    wanted = ("state", "runs", "last exit code", "last terminating signal", "pid")
    found = []
    for line in answer[1].splitlines():
        name, _, value = line.strip().partition(" = ")
        if name in wanted and value:
            found.append(f"{name} {value}")
    return "loaded: " + (", ".join(dict.fromkeys(found)) or "no details")


# A server program -------------------------------------------------------------------------------------------------

def quarantined(path: Path) -> bool:
    """macOS: the file carries com.apple.quarantine (a web browser's download, and what Archive Utility unpacked from
    one): macOS won't run it until that's taken off. Asked once for each state of the file."""
    if not platforms.is_macos():
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    key = (str(path), stat.st_mtime_ns, stat.st_ctime_ns)  # taking the mark off changes its ctime
    if key not in _quarantine_seen:
        answer = ask("xattr", "-p", QUARANTINE, str(path))
        _quarantine_seen[key] = answer is not None and answer[0] == 0
    return _quarantine_seen[key]


def program_problems(path: Path) -> list[str]:
    """What stops this server program from running here (Linux and macOS; Windows runs what it runs), each with what to
    do. A Windows program (0.9's .exe) is wine's business, said elsewhere."""
    if os.name == "nt" or not path.is_file():
        return []
    try:
        with path.open("rb") as file:
            head = file.read(4)
    except OSError as error:
        return [f"{path} can't be read: {error}"]
    if head.startswith(WINDOWS_EXE) or (path.suffix.lower() == ".exe" and not head.startswith((ELF, *MACH_O))):
        return []  # a Windows program, or named as one (an empty .exe can't say): wine runs it, no run mark needed
    found = []
    system = platforms.system()
    if system == platforms.MACOS and head.startswith(ELF):
        found.append(f"{path} is the Linux program: macOS needs project-reclaimer from Project Reclaimer's "
                     "-macos-aarch64.zip download")
    elif system == platforms.LINUX and head.startswith(MACH_O):
        found.append(f"{path} is the macOS program: Linux needs project-reclaimer from Project Reclaimer's "
                     "-linux-<machine>.tar.gz download")
    if not os.access(path, os.X_OK):
        found.append(f"{path.name} isn't marked as a program (unpacked without it): in a terminal, "
                     f"chmod +x \"{path}\"")
    if quarantined(path):
        # Whether macOS then refuses it depends on the Mac (GitHub's Macs ran it): Project Reclaimer's own README says
        # to take it off, and that's harmless either way
        found.append(f"macOS may stop {path.name} the moment it starts (signal 9): it came from a web browser's "
                     f"download and still carries macOS's quarantine. Take it off once, in Terminal: "
                     f"xattr -dr {QUARANTINE} \"{path.parent}\"")
    return found


def exit_text(code: int) -> str:
    """An exit code as the host can look it up: a signal by number and name (Linux, macOS), Windows' own codes in
    hexadecimal."""
    if code < 0:  # Python's way of saying a signal ended it (Windows' codes come back as positive numbers)
        try:
            return f"ended by signal {-code} ({signal.Signals(-code).name})"
        except ValueError:
            return f"ended by signal {-code}"
    if code > 0xFFFF:
        return f"exit code {code:#010x}"
    return f"exit code {code}"


def signal_hint(code: int | None, uptime: float) -> str:
    """Why a server program ended by a signal may have, in words: '' when nothing more can be said."""
    if code is None or code >= 0:
        return ""
    if -code == getattr(signal, "SIGKILL", 9):
        if platforms.is_macos() and uptime < 15:
            return ("macOS ends a program with signal 9 when it won't let it run (quarantine, a damaged signature) or "
                    "when memory runs out")
        return "signal 9 is a forced end: something ended it (the system, when memory runs out, or someone)"
    if -code in (getattr(signal, "SIGSEGV", 11), getattr(signal, "SIGBUS", 10), getattr(signal, "SIGABRT", 6)):
        return "the server program itself crashed: its console's last lines (and data/<server>/server.log) say more"
    return ""
