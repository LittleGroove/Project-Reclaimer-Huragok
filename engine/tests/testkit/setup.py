"""A Huragok folder to set servers up in, the server program faked by a script, the setup screens' controller."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from conftest import free_port, staff, write_settings
from testkit import FAKE
from testkit.window import FakeKeyboard

from huragok.core.requests import Result
from huragok.core.settings import load
from huragok.core.tomledit import replace_server_blocks, set_table_key
from huragok.program.dedicated import Dedicated




PYTHON = Path(sys.executable).as_posix()


TEMPLATE_COMMAND = [PYTHON, FAKE.as_posix(), "dedicated", "run", "--config", "{config}"]


ALPHA = "Alpha Server"


def fake_tool() -> Dedicated:
    return Dedicated(FAKE, template=TEMPLATE_COMMAND)


def make_server_folder(folder: Path, name: str, port: int, *, with_game: bool = True) -> None:
    """A server folder as the fake `dedicated init --from` makes it, with one server and RCON on."""
    result = fake_tool().init(folder, mcc="" if with_game else None)
    assert result.ok, result.text
    config = folder / "dedicated.toml"
    set_table_key(config, "rcon", "password", "secretsecret")
    replace_server_blocks(config, [{"name": name, "port": port, "playlist": "playlists/slayer.playlist.json"}])


def setup_root(tmp_path: Path, *, with_game: bool = True, extra: str = "") -> Path:
    """A root with Alpha Server made by the fake init; [process] command runs the fake, so tool_for finds it."""
    root = tmp_path / "reclaimer"
    make_server_folder(root / "SERVERS" / ALPHA, ALPHA, free_port(), with_game=with_game)
    (root / "SERVERS" / ALPHA / "project-reclaimer.exe").write_bytes(b"")  # found; never run (the command is the fake)
    command = ", ".join(json.dumps(w) for w in TEMPLATE_COMMAND)
    body = f'backend = "process"\n[process]\ncommand = [{command}]\n{extra}'
    return write_settings(root, None, body, staff(), backend=None)


class StrictKeyboard(FakeKeyboard):
    """Fails the test when the script runs out of keys, instead of letting the window loop forever."""

    async def read(self, timeout: float | None = None) -> str | None:
        key = await super().read(timeout)
        if key is None and not (timeout is not None and timeout < 1):
            raise RuntimeError("the scripted keys ran out: a screen read them differently than the test expected")
        return key


class SetupController:
    """A controller over a real root, as the supervising window's is, with the fleet replaced by a call log."""

    supervising = True
    launcher = "start.cmd"

    def __init__(self, settings_path: Path) -> None:
        self.settings = load(settings_path)
        self.home = self.settings.home
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.discovered = 0

    def refresh_settings(self) -> Any:
        self.settings = load(self.settings.path)
        return self.settings

    def discover(self) -> None:
        self.discovered += 1

    def header(self) -> list[str]:
        return []

    def rows(self) -> list[dict[str, Any]]:
        return [{"index": i, "name": s.name, "slug": s.slug, "port": s.game_port, "autostart": s.autostart,
                 "phase": "idle", "status": "stopped", "color": "gray", "players": "", "names": "", "game": "",
                 "detail": "", "can": {"start": True, "stop": False, "restart": True, "cancel": False, "talk": False}}
                for i, s in enumerate(self.settings.servers, 1)]

    def events(self, count: int) -> list[tuple[int, str]]:
        return []

    def problems(self) -> list[str]:
        return []

    def finished(self) -> str | None:
        return None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        return [(server, Result(True, "starting"))]

    def request_quit(self) -> None:
        pass
