"""Huragok's own releases: an install laid out as start.cmd runs it, and a release zip to update it with."""

from __future__ import annotations

import zipfile
from pathlib import Path

from huragok.updates.engine import Release


NEW = "99.0.0"


RELEASE = Release(NEW, "https://example.invalid/releases/v99.0.0", "## What's new\n- Better things\n- More things",
                  "https://example.invalid/huragok-99.0.0.zip")


def make_release_zip(path: Path, version: str = NEW, top: str = "huragok") -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr(f"{top}/engine/huragok/__init__.py", f'__version__ = "{version}"\n')
        bundle.writestr(f"{top}/engine/huragok/new_module.py", "")
        bundle.writestr(f"{top}/engine/requirements.txt", "websockets>=13\n")
        bundle.writestr(f"{top}/start.cmd", "new start.cmd")
        bundle.writestr(f"{top}/start.sh", "new start.sh")
        bundle.writestr(f"{top}/README.md", "new readme")
        bundle.writestr(f"{top}/docs/automod.md", "new docs")
    return path


def make_install(home: Path) -> Path:
    """A supervisor folder as start.cmd leaves it, with the host's own files."""
    (home / "engine" / "huragok").mkdir(parents=True)
    (home / "engine" / "huragok" / "__init__.py").write_text('__version__ = "0.0.1"\n', encoding="utf-8")
    (home / "engine" / "huragok" / "old_module.py").write_text("", encoding="utf-8")
    (home / "engine" / ".venv").mkdir()
    (home / "engine" / ".venv" / "marker").write_text("installed packages", encoding="utf-8")
    (home / "docs").mkdir()
    (home / "docs" / "old.md").write_text("old docs", encoding="utf-8")
    for name, text in {"start.cmd": "old", "start.sh": "old", "README.md": "old", "settings.toml": "mine",
                       "commands.toml": "mine", "automod.txt": "my words"}.items():
        (home / name).write_text(text, encoding="utf-8")
    (home / "records").mkdir()
    (home / "records" / "supervisor.sqlite3").write_text("my records", encoding="utf-8")
    return home
