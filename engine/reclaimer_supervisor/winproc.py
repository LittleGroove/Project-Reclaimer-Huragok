"""Windows: start `dedicated` so it outlives us, and stop it cleanly with Ctrl+Break.

Tested against v0.9.18:
- Ending `dedicated` ends its servers too, so `dedicated` must not depend on us:
  it gets its own window-less console and process group, and writes its output
  to a file (a pipe would break if we exited, and a program writing to a broken
  pipe can crash).
- Ctrl+Break makes it print "Stopping every server… All servers stopped." and exit
  in well under a second. Windows only delivers console signals to processes on
  the same console, so a small helper process attaches to `dedicated`'s console
  and sends it. That works after our own restart too.
"""

from __future__ import annotations

import os
import subprocess
import sys

CTRL_BREAK_EVENT = 1
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def creation_flags() -> int:
    return CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP


def send_ctrl_break(pid: int) -> int:
    """Run inside the helper process: attach to pid's console and send Ctrl+Break.

    Returns 0 on success. Only call this in a process of its own: it gives up
    the caller's console.
    """
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return 2
    kernel32.SetConsoleCtrlHandler(None, True)  # don't stop ourselves
    ok = kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)
    kernel32.FreeConsole()
    return 0 if ok else 3


def ctrl_break_helper_command(pid: int) -> list[str]:
    """The command that runs send_ctrl_break(pid) in a process of its own."""
    if getattr(sys, "frozen", False):  # a bundled build: the executable is our CLI
        return [sys.executable, "_ctrl-break", str(pid)]
    return [sys.executable, "-m", "reclaimer_supervisor", "_ctrl-break", str(pid)]


def ctrl_break(pid: int, timeout: float = 10.0) -> bool:
    """Ask the process (and its process group) to stop cleanly. True if it was sent."""
    if os.name != "nt":
        raise OSError("Ctrl+Break is Windows-only")
    result = subprocess.run(ctrl_break_helper_command(pid), creationflags=DETACHED_PROCESS,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout)
    return result.returncode == 0
