"""Text every part of Huragok writes the same way (core/text.py), and the files every part reads the same way
(core/files.py), and what the host types to run Huragok (core/settings.launcher)."""

from __future__ import annotations

import os
from pathlib import Path

from huragok.core import settings
from huragok.core.files import mtime
from huragok.core.serverfolders import slugify
from huragok.core.text import size_text, slug


def test_a_name_as_a_files() -> None:
    assert slug("LG Test Server") == "lg-test-server" and slug("  Big Team Battle!! ") == "big-team-battle"
    assert slug("!!!") == "" and slug("!!!", "playlist") == "playlist"  # each kind keeps its own word for nothing
    assert slugify("Сервер").startswith("server-")  # a server gets a hash instead, the same every time


def test_sizes_as_the_host_reads_them() -> None:
    assert size_text(500) == "500 bytes" and size_text(81 * 1024) == "81 KB"
    assert size_text(120 * (1 << 20)) == "120 MB" and size_text(3 * (1 << 29)) == "1.5 GB"
    assert size_text(20787, 1) == "20.3 KB" and size_text(1 << 30, 0) == "1.0 GB"  # a map file's one decimal


def test_a_files_time_or_none(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    assert mtime(path) == 0.0  # not there yet: nothing to compare, nothing raised
    path.write_text("x", encoding="utf-8")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    assert mtime(path) == 1_700_000_000


def test_what_the_host_types_to_run_huragok(monkeypatch) -> None:
    monkeypatch.setenv(settings.LAUNCHER_VARIABLE, "start.cmd")
    assert settings.launcher() == settings.launcher("huragok") == "start.cmd"  # what start.cmd says wins
    monkeypatch.delenv(settings.LAUNCHER_VARIABLE)
    assert settings.launcher("huragok") == "huragok"  # the installed command
    assert settings.launcher() == ("start.cmd" if os.name == "nt" else "./start.sh")
