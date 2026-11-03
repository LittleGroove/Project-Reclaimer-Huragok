"""Files written whole (core/files.py write_atomic): swapped in at once, and on the disk first, so a power cut can't
leave an empty settings file or record (audit 2026-11); live.json, rewritten every 2 s, isn't worth the flush."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from huragok.core import files


def test_a_file_is_on_the_disk_before_it_replaces_the_old_one(tmp_path: Path, monkeypatch) -> None:
    synced: list[int] = []
    real = os.fsync
    monkeypatch.setattr(files.os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    path = tmp_path / "settings.toml"
    path.write_text("old\n", encoding="utf-8")
    files.write_atomic(path, "new\n")
    assert path.read_text(encoding="utf-8") == "new\n" and len(synced) == 1
    files.write_atomic(tmp_path / "live.json", "{}", durable=False)
    assert len(synced) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live.json", "settings.toml"]  # no temporary file left


def test_a_symlinked_file_is_changed_where_it_points(tmp_path: Path) -> None:
    """Linux: the link was replaced by a copy of the new text, the file it pointed at left as it was (audit 2026-11,
    4.9)."""
    import pytest

    real = tmp_path / "real.toml"
    real.write_text("old\n", encoding="utf-8")
    link = tmp_path / "settings.toml"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("this account can't make symlinks here (Windows without Developer Mode)")
    files.write_atomic(link, "new\n")
    assert link.is_symlink() and real.read_text(encoding="utf-8") == "new\n"


def test_a_file_that_can_only_be_written_not_replaced_is_written(tmp_path: Path, monkeypatch) -> None:
    """A single file bind-mounted into a container refuses a replace with EBUSY (audit 2026-11, 4.9)."""
    import errno

    def busy(source: object, target: object) -> None:
        raise OSError(errno.EBUSY, "Device or resource busy")

    path = tmp_path / "dedicated.toml"
    path.write_text("old\n", encoding="utf-8")
    monkeypatch.setattr(files.os, "replace", busy)
    files.write_atomic(path, "new\n")
    assert path.read_text(encoding="utf-8") == "new\n" and [p.name for p in tmp_path.iterdir()] == ["dedicated.toml"]


@pytest.mark.skipif(os.name != "nt", reason="Windows refuses a swap while a scan reads the file; Linux doesn't")
def test_settings_and_commands_writes_wait_out_a_virus_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D's settings and commands.toml's changes swapped their file in without the retry every other write has: a
    virus scan reading it just then failed the change (audit 2026-11, 4.8)."""
    from huragok.core import commandsfile, files, hostsettings

    assert hostsettings.replace is files.replace and commandsfile.replace is files.replace
    source, target = tmp_path / ".new", tmp_path / "settings.toml"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    tries: list[int] = []
    real = os.replace

    def scanned(a: Any, b: Any) -> None:
        tries.append(1)
        if len(tries) == 1:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        real(a, b)

    monkeypatch.setattr(files.os, "replace", scanned)
    monkeypatch.setattr(files, "REPLACE_WAIT_SECONDS", 0.0)
    files.replace(source, target)
    assert target.read_text(encoding="utf-8") == "new" and len(tries) == 2
