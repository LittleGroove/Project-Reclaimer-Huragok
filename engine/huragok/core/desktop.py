"""The desktop around Huragok: open a folder or a file, a window following a log, start at boot.
Windows, macOS and Linux; each function says what it did, or why it couldn't.

enable_startup and disable_startup run `systemctl --user` on Linux (`launchctl` on macOS), which can take a while:
from the event loop (the window), use their _async versions.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

from . import platforms

STARTUP_CMD = "Huragok.cmd"
UNIT_NAME = "huragok.service"
LAUNCH_LABEL = "io.github.littlegroove.huragok"  # macOS: the LaunchAgent's label, and its file's name
OLD_NAMES = ("Reclaimer Supervisor.cmd", "reclaimer-supervisor.service")  # what 0.2.1 and earlier wrote
SYSTEMCTL_SECONDS = 20.0
# Where Python and the tools usually are on a Mac (Homebrew on Apple Silicon, then on Intel and python.org's links):
# a LaunchAgent starts with only the system's own folders on its PATH.
MAC_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def mac_desktop() -> bool:
    """Signed in at the Mac itself, not over SSH: a window opened there is one the host sees."""
    return not (os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))


def mac_terminal(line: str) -> bool:
    """Run a shell line in a new Terminal window (macOS), through AppleScript. False when it couldn't be asked."""
    script = line.replace("\\", "\\\\").replace('"', '\\"')  # inside AppleScript's double quotes
    try:
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                          "-e", 'tell application "Terminal" to activate'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def keep_mac_awake() -> subprocess.Popen[bytes] | None:
    """On a Mac, keep it from sleeping while this process runs: `caffeinate -is -w <this process>`, which ends by
    itself when this process does (a sleeping Mac drops every game). None elsewhere, or without caffeinate."""
    if os.name == "nt" or not platforms.is_macos() or shutil.which("caffeinate") is None:
        return None
    try:
        return subprocess.Popen(["caffeinate", "-is", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def open_path(path: Path) -> str:
    """Open a folder (or a file with its default program)."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platforms.is_macos():
            subprocess.Popen(["open", str(path)])
        else:
            opener = shutil.which("xdg-open")
            if opener is None:
                return f"no desktop to open it with; it's at {path}"
            subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        return f"couldn't open {path}: {error}"
    return f"opened {path}"


def open_editor(path: Path) -> str:
    """Open a text file for editing: Notepad on Windows, the Mac's text editor (`open -t`), the desktop's editor or
    $EDITOR on Linux."""
    try:
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(path)])
            return f"opened {path.name} in Notepad"
        if platforms.is_macos() and mac_desktop():
            subprocess.Popen(["open", "-t", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"opened {path.name} in your text editor"
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        opener = shutil.which("xdg-open")
        if opener is not None and os.environ.get("DISPLAY"):
            subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"opened {path.name}"
        if editor:
            return f"edit it in another terminal: {editor} {path}"
        return f"edit it in another terminal: nano {path}"
    except OSError as error:
        return f"couldn't open {path}: {error}"


def open_log_window(home: Path, arguments: list[str], title: str) -> str:
    """A separate window following a log (start.cmd watch ...). Closing it does nothing to the servers."""
    if os.name == "nt":
        launcher = home / "start.cmd"
        command = f'"{launcher}" {" ".join(arguments)}'
        try:
            terminal = shutil.which("wt.exe")
            if terminal:
                subprocess.Popen([terminal, "-w", "0", "new-tab", "--title", title, "cmd", "/c", command])
                return f"opened a Windows Terminal tab: {title}"
            subprocess.Popen(f'start "{title}" cmd /c {command}', shell=True)
            return f"opened a window: {title}"
        except OSError as error:
            return f"couldn't open a window ({error}); run in another terminal: start.cmd {' '.join(arguments)}"
    launcher = home / "start.sh"
    line = f"{shlex.quote(str(launcher))} {' '.join(arguments)}"  # a home folder with a space in it (audit 2026-11, 7.9)
    if platforms.is_macos():
        if mac_desktop() and mac_terminal(f"sh {line}"):  # sh: a start.sh unzipped without its exec bit runs too
            return f"opened a Terminal window: {title}"
        return f"no window here (over SSH?); run in another terminal (or tmux): {line}"
    for program, flags in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                           ("xfce4-terminal", ["-e"]), ("xterm", ["-e"])):
        if shutil.which(program) and os.environ.get("DISPLAY"):
            try:
                args = [program, *flags, "sh", "-c", line] if flags != ["-e"] else [program, "-e", f"sh -c {shlex.quote(line)}"]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"opened a {program} window: {title}"
            except OSError:
                continue
    return f"no desktop terminal found; run in another terminal (or tmux): {line}"


# Start at boot -----------------------------------------------------------------------------------

def startup_path() -> Path | None:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_CMD
    if platforms.is_macos():
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def old_startup_paths() -> list[Path]:
    """Start-at-boot items written under the old name (Reclaimer Supervisor) that are still there."""
    path = startup_path()
    if path is None:
        return []
    return [old for old in (path.with_name(name) for name in OLD_NAMES) if old.suffix == path.suffix and old.exists()]


def startup_enabled() -> bool | None:
    path = startup_path()
    return None if path is None else path.exists() or bool(old_startup_paths())


def startup_description() -> str:
    if os.name == "nt":
        return ("When Windows logs in, a window opens with start.cmd and starts every autostart server. "
                f"(A small file in the Startup folder: {startup_path()})")
    if platforms.is_macos():
        return ("A LaunchAgent runs start.sh when you log in, without a window; ./start.sh in Terminal opens the "
                f"dashboard while it runs. ({startup_path()}; for a Mac that comes back on its own after a restart, "
                "turn on automatic login in System Settings > Users & Groups)")
    return ("A systemd user unit runs start.sh at login, without a window; start.sh opens the dashboard "
            f"from another terminal while it runs. ({startup_path()}; `loginctl enable-linger` makes it start "
            "at boot without a login)")


def enable_startup(home: Path) -> str:
    path = startup_path()
    if path is None:
        return "couldn't find where startup items go"
    try:
        _remove_old()  # one item, under the new name
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            return write_windows_startup(path, home)
        return write_mac_startup(path, home) if platforms.is_macos() else write_linux_startup(path, home)
    except (OSError, subprocess.SubprocessError) as error:
        return f"couldn't set it up: {error}"


async def enable_startup_async(home: Path) -> str:
    """enable_startup, off the event loop."""
    return await asyncio.to_thread(enable_startup, home)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=SYSTEMCTL_SECONDS)


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=SYSTEMCTL_SECONDS)


def _remove_old() -> None:
    for old in old_startup_paths():  # none on a Mac: 0.2.1 and earlier wrote no LaunchAgent
        if os.name != "nt":
            _systemctl("disable", old.name)
        old.unlink()


def batch_text(path: str) -> str:
    """A path as cmd.exe reads it inside a batch file's double quotes: a `%` would start a variable."""
    return path.replace("%", "%%")


def windows_startup_script(home: Path) -> bytes:
    """The Startup folder's .cmd. cmd.exe reads a batch file in the console's code page, not UTF-8, so a folder
    named with an accent (C:\\Users\\José) became "The system cannot find the path specified": the script switches
    the code page to UTF-8 on its first lines (plain ASCII, read the same either way), and the rest is UTF-8."""
    lines = ["@echo off", "chcp 65001 >nul",
             f'start "Huragok" /D "{batch_text(str(home))}" "{batch_text(str(home / "start.cmd"))}"']
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def write_windows_startup(path: Path, home: Path) -> str:
    """A small .cmd in the Startup folder that opens the window."""
    path.write_bytes(windows_startup_script(home))
    return "Huragok will now start when Windows logs in"


def systemd_quote(value: str) -> str:
    """One word of a unit's ExecStart=: quoted (a space in the path), with systemd's own specials escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def linux_startup_unit(home: Path) -> str:
    return (
        "[Unit]\nDescription=Huragok\nAfter=network-online.target\n\n"
        f"[Service]\nWorkingDirectory={str(home).replace('%', '%%')}\n"
        f"ExecStart={systemd_quote(str(home / 'start.sh'))} start --no-menu\n"
        # Only Huragok's start loop is told to stop (it passes that on; Huragok closes and leaves the servers running).
        # With systemd's default, every server in the unit's control group was killed with it, mid-game, as the
        # service stopped or its user logged out (audit 2026-11, 1.3).
        "KillMode=process\n"
        "Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n")


def write_linux_startup(path: Path, home: Path) -> str:
    """A systemd user unit that runs start.sh without a window, enabled for the next login."""
    path.write_text(linux_startup_unit(home), encoding="utf-8")
    try:
        _systemctl("daemon-reload")
        result = _systemctl("enable", UNIT_NAME)
    except (OSError, subprocess.SubprocessError) as error:
        return f"wrote {path}, but systemctl --user couldn't be run: {error}"
    if result.returncode != 0:
        return f"wrote {path}, but systemctl --user enable failed: {result.stderr.strip()[-200:]}"
    return "Huragok will now start when you log in (run `loginctl enable-linger` for boot without a login)"


def mac_path() -> str:
    """The PATH a LaunchAgent gets: the host's own (where start.sh found Python when they set Huragok up), then the
    usual folders, each once."""
    words = [w for w in os.environ.get("PATH", "").split(":") if w] + list(MAC_PATH)
    return ":".join(dict.fromkeys(words))


def mac_launch_agent(home: Path) -> bytes:
    """The LaunchAgent: start.sh without a window at login, started again after a crash (not after a close)."""
    job = {
        "Label": LAUNCH_LABEL,
        "ProgramArguments": ["/bin/sh", str(home / "start.sh"), "start", "--no-menu"],
        "WorkingDirectory": str(home),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # systemd's Restart=on-failure: a close (exit 0) stays closed
        "ThrottleInterval": 10,
        # Only Huragok is stopped with the job: launchd would otherwise kill whatever is left in its process group,
        # as systemd killed the unit's servers (audit 2026-11, 1.3). The servers run in sessions of their own anyway.
        "AbandonProcessGroup": True,
        # Unset, launchd throttles a job's processor and disk use, and the servers it starts with it: game servers
        # must keep their frame rate, so they run as an app does.
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": mac_path()},
        # No StandardOutPath: --no-menu writes every log line to the console, and huragok.log already has them.
    }
    return plistlib.dumps(job)


def write_mac_startup(path: Path, home: Path) -> str:
    """A LaunchAgent in ~/Library/LaunchAgents, which macOS loads at the next login. Not loaded now: that would start
    a second Huragok beside the one running. `launchctl enable` undoes a `launchctl disable` someone ran on it."""
    path.write_bytes(mac_launch_agent(home))
    try:
        uid = os.getuid()  # type: ignore[attr-defined]  # not on Windows, which never gets here
        _launchctl("enable", f"gui/{uid}/{LAUNCH_LABEL}")
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass  # the file alone is enough unless it was disabled by hand
    return ("Huragok will now start when you log in (macOS may say a background item was added; turn on automatic "
            "login for a Mac that starts on its own)")


def disable_startup() -> str:
    path = startup_path()
    if path is None or not (path.exists() or old_startup_paths()):
        return "it wasn't set to start at boot"
    try:
        _remove_old()
        if path.exists():
            if os.name != "nt" and not platforms.is_macos():  # a Mac: the file gone is enough for the next login
                _systemctl("disable", UNIT_NAME)
            path.unlink()
    except (OSError, subprocess.SubprocessError) as error:
        return f"couldn't remove it: {error}"
    return "Huragok will no longer start at boot"


async def disable_startup_async() -> str:
    """disable_startup, off the event loop."""
    return await asyncio.to_thread(disable_startup)
