"""The desktop around the supervisor: open a folder or a file, a window following a log, start at boot.
Windows and Linux; each function says what it did, or why it couldn't.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

STARTUP_CMD = "Reclaimer Supervisor.cmd"
UNIT_NAME = "reclaimer-supervisor.service"


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


def startup_enabled() -> bool | None:
    path = startup_path()
    return None if path is None else path.exists()


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
        path.parent.mkdir(parents=True, exist_ok=True)
        return write_windows_startup(path, home) if os.name == "nt" else write_linux_startup(path, home)
    except OSError as error:
        return f"couldn't set it up: {error}"


def write_windows_startup(path: Path, home: Path) -> str:
    """A small .cmd in the Startup folder that opens the window."""
    script = f'@echo off\r\nstart "Reclaimer Supervisor" /D "{home}" "{home / "start.cmd"}"\r\n'
    path.write_text(script, encoding="utf-8")
    return "the supervisor will now start when Windows logs in"


def write_linux_startup(path: Path, home: Path) -> str:
    """A systemd user unit that runs start.sh without a window, enabled for the next login."""
    unit = (
        "[Unit]\nDescription=Reclaimer Supervisor\nAfter=network-online.target\n\n"
        f"[Service]\nWorkingDirectory={home}\nExecStart={home / 'start.sh'} start --no-menu\n"
        "Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n")
    path.write_text(unit, encoding="utf-8")
    result = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    result = subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], capture_output=True, text=True)
    if result.returncode != 0:
        return f"wrote {path}, but systemctl --user enable failed: {result.stderr.strip()[-200:]}"
    return "the supervisor will now start when you log in (run `loginctl enable-linger` for boot without a login)"


def disable_startup() -> str:
    path = startup_path()
    if path is None or not path.exists():
        return "it wasn't set to start at boot"
    try:
        if os.name != "nt":
            subprocess.run(["systemctl", "--user", "disable", UNIT_NAME], capture_output=True, text=True)
        path.unlink()
    except OSError as error:
        return f"couldn't remove it: {error}"
    return "the supervisor will no longer start at boot"
