"""The desktop around Huragok: open a folder or a file, a window following a log, start at boot.
Windows and Linux; each function says what it did, or why it couldn't.

enable_startup and disable_startup run `systemctl --user` on Linux, which can take a while: from the event loop
(the window), use their _async versions.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

STARTUP_CMD = "Huragok.cmd"
UNIT_NAME = "huragok.service"
OLD_NAMES = ("Reclaimer Supervisor.cmd", "reclaimer-supervisor.service")  # what 0.2.1 and earlier wrote
SYSTEMCTL_SECONDS = 20.0


def open_path(path: Path) -> str:
    """Open a folder (or a file with its default program)."""
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
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
    """Open a text file for editing: Notepad on Windows, the desktop's editor or $EDITOR on Linux."""
    try:
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(path)])
            return f"opened {path.name} in Notepad"
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
    line = f"{launcher} {' '.join(arguments)}"
    for program, flags in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                           ("xfce4-terminal", ["-e"]), ("xterm", ["-e"])):
        if shutil.which(program) and os.environ.get("DISPLAY"):
            try:
                args = [program, *flags, "sh", "-c", line] if flags != ["-e"] else [program, "-e", f"sh -c '{line}'"]
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
        return write_windows_startup(path, home) if os.name == "nt" else write_linux_startup(path, home)
    except (OSError, subprocess.SubprocessError) as error:
        return f"couldn't set it up: {error}"


async def enable_startup_async(home: Path) -> str:
    """enable_startup, off the event loop."""
    return await asyncio.to_thread(enable_startup, home)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=SYSTEMCTL_SECONDS)


def _remove_old() -> None:
    for old in old_startup_paths():
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


def disable_startup() -> str:
    path = startup_path()
    if path is None or not (path.exists() or old_startup_paths()):
        return "it wasn't set to start at boot"
    try:
        _remove_old()
        if path.exists():
            if os.name != "nt":
                _systemctl("disable", UNIT_NAME)
            path.unlink()
    except (OSError, subprocess.SubprocessError) as error:
        return f"couldn't remove it: {error}"
    return "Huragok will no longer start at boot"


async def disable_startup_async() -> str:
    """disable_startup, off the event loop."""
    return await asyncio.to_thread(disable_startup)
