"""Huragok closing ends the long work in its threads (core/closing.py): a SteamCMD download of hours, `dedicated init`
copying from MCC, a Workshop fetch held the process open after Huragok was done, unwatched, its lock held, the next
version unable to start (audit 2026-11, 2.1). Work started after a close isn't touched by it."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from huragok import steam
from huragok.core import closing
from huragok.program.dedicated import _wait

SLEEPS = ["-c", "import time; time.sleep(60)"]


def test_a_program_huragok_waits_on_is_ended_as_it_closes() -> None:
    process = subprocess.Popen([sys.executable, *SLEEPS])
    try:
        threading.Timer(0.3, closing.close).start()
        started = time.monotonic()
        assert _wait(process, 60, None) == "stopped"
        assert time.monotonic() - started < 10
    finally:
        process.kill()
        process.wait()


def test_steamcmd_is_ended_as_huragok_closes_and_says_so() -> None:
    threading.Timer(0.3, closing.close).start()
    started = time.monotonic()
    result = steam.run(Path(sys.executable), SLEEPS, timeout=60)
    assert time.monotonic() - started < 10
    assert result.returncode == -1 and "Stopped: Huragok is closing" in result.stdout


def test_work_started_after_a_close_runs_and_a_timeout_is_still_a_timeout() -> None:
    closing.close()  # an earlier Huragok in this process (another test) closed
    result = steam.run(Path(sys.executable), ["-c", "print('done')"], timeout=60)
    assert result.returncode == 0 and "done" in result.stdout
    with pytest.raises(subprocess.TimeoutExpired):
        steam.run(Path(sys.executable), SLEEPS, timeout=0.5)
