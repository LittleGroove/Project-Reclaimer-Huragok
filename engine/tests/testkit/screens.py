"""D's Huragok files screens, driven on a root made from the templates."""

from __future__ import annotations

from pathlib import Path

from conftest import free_port
from testkit.setup import ALPHA, SetupController, StrictKeyboard, make_server_folder
from testkit.window import FakeTerminal

from huragok.commandline.firstrun import write_files
from huragok.core.logs import RecentLog
from huragok.window.dashboard import Menu


def template_root(tmp_path: Path) -> Path:
    """A root as `init` writes it (the whole settings.toml template), with Alpha Server in SERVERS/."""
    root = tmp_path / "reclaimer"
    make_server_folder(root / "SERVERS" / ALPHA, ALPHA, free_port())
    write_files(root, backend="attach")
    return root / "settings.toml"


def screen(settings_path: Path, keys: list[str]) -> tuple[Menu, FakeTerminal]:
    terminal = FakeTerminal()
    return Menu(SetupController(settings_path), terminal, StrictKeyboard(keys), RecentLog()), terminal  # type: ignore[arg-type]


def text_of(terminal: FakeTerminal) -> str:
    return "".join(terminal.output)
