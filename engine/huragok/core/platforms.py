"""Which system Huragok runs on: Windows, macOS or Linux (any other Unix is treated like Linux).

Most of Huragok only needs `os.name == "nt"` (Windows or not). These are for the places where macOS differs from
Linux: where Steam and SteamCMD keep their files, start at login (launchd, not systemd), the Terminal window, and
the server program's download (a -macos-aarch64.zip from Project Reclaimer 0.10). Tests pin a system with
monkeypatch.setattr(platforms, "system", lambda: "macos").
"""

from __future__ import annotations

import os
import platform
import sys

WINDOWS, MACOS, LINUX = "windows", "macos", "linux"
NAMES = {WINDOWS: "Windows", MACOS: "macOS", LINUX: "Linux"}


def system() -> str:
    """"windows", "macos" or "linux"."""
    if os.name == "nt":
        return WINDOWS
    return MACOS if sys.platform == "darwin" else LINUX


def is_macos() -> bool:
    return system() == MACOS


def system_name() -> str:
    """The system's name for a host to read: "Windows", "macOS" or "Linux"."""
    return NAMES[system()]


def apple_silicon() -> bool:
    """A Mac with an Apple processor (arm64): Project Reclaimer's macOS program is built for these alone, and
    Valve's SteamCMD for macOS is an Intel program, which runs on them through Rosetta 2."""
    return is_macos() and platform.machine().lower() in ("arm64", "aarch64")
