"""What the tests share: fakes of what Huragok talks to (the server program, its console, MCC, GitHub's
releases), folders to run it on, and the window driven by a scripted keyboard. Nothing here is a test."""

import os
from pathlib import Path
from types import SimpleNamespace

TESTS = Path(__file__).resolve().parents[1]  # engine/tests
DATA = TESTS / "data"
ENGINE = TESTS.parent  # engine/
REPO = ENGINE.parent  # the repository: start.cmd, start.sh, docs/
FAKE = Path(__file__).resolve().parent / "fake_dedicated.py"  # a stand-in `dedicated run`, started as a process


def os_as(name: str) -> SimpleNamespace:
    """The os module as the module under test sees it, with os.name set to `name` ("nt", "posix"): for
    monkeypatch.setattr(desktop, "os", os_as("nt")). Never patch os.name itself: it's the one os module of the whole
    process, and pathlib reads it too (pytest's own Path(os.getcwd()) fails on Linux once it says "nt")."""
    names = {key: getattr(os, key) for key in dir(os) if not key.startswith("__")}
    names["name"] = name
    return SimpleNamespace(**names)
