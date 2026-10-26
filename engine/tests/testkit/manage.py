"""A server to manage: its console stubbed, the server program's releases served from here."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port, free_ports, staff, write_root
from testkit.fake_dedicated import TEMPLATE

from huragok.core.events import Event
from huragok.core.requests import Result
from huragok.core.settings import load
from huragok.core.tomledit import replace_server_blocks
from huragok.program import releases as serverupdates
from huragok.program.releases import ProgramRelease
from huragok.rcon import Reply


class ManageController:
    """A controller over a real root: rcon and restart calls are logged, events can be pushed to a watcher."""

    supervising = True
    launcher = "start.cmd"

    def __init__(self, settings_path: Path) -> None:
        self.settings = load(settings_path)
        self.home = self.settings.home
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.watchers: dict[str, Any] = {}

    def refresh_settings(self) -> Any:
        self.settings = load(self.settings.path)
        return self.settings

    def discover(self) -> None:
        pass

    def watch(self, server: str, listener: Any) -> Any:
        self.watchers[server] = listener
        return lambda: self.watchers.pop(server, None)

    def header(self) -> list[str]:
        return []

    def rows(self) -> list[dict[str, Any]]:
        return [{"index": i, "name": s.name, "slug": s.slug, "port": s.game_port, "autostart": True, "phase": "running",
                 "status": "RUNNING", "color": "green", "players": "1/16", "names": "Alice", "game": "", "detail": "",
                 "plays": s.plays, "can": {"start": False, "stop": True, "restart": True, "cancel": False, "talk": True}}
                for i, s in enumerate(self.settings.servers, 1)]

    def events(self, count: int) -> list[tuple[int, str]]:
        return []

    def problems(self) -> list[str]:
        return []

    def finished(self) -> str | None:
        return None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        self.calls.append((verb, server, options))
        if verb == "rcon":
            if server in self.watchers:  # the server answers with an event too, as a real one does
                self.watchers[server](Event("rcon.chat", server, {"channel": "all", "name": "Alice", "text": "hi"}))
            return [(server, Result(True, "ok", f"reply to {options.get('text')}"))]
        if verb == "check_program_update":
            return [("Huragok", Result(True, "server program 0.9.19 is out (Alpha 0.9.18)", "notes"))]
        if verb == "update_program":
            return [("Huragok", Result(True, f"installed; restarting {options.get('when')}"))]
        return [(server, Result(True, "done"))]

    def request_quit(self) -> None:
        pass


def manage_root(tmp_path: Path) -> Path:
    root = tmp_path / "reclaimer"
    folder = root / "SERVERS" / "Alpha"
    folder.mkdir(parents=True)
    (folder / "dedicated.toml").write_text(TEMPLATE, encoding="utf-8")
    replace_server_blocks(folder / "dedicated.toml", [{"name": "Alpha", "port": free_port(), "map": "Guardian", "game": "Slayer"}])
    from huragok.core.tomledit import set_table_key

    set_table_key(folder / "dedicated.toml", "rcon", "password", "secretsecret")
    (folder / "project-reclaimer.exe").write_bytes(b"")
    return write_root(root, [], 'backend = "attach"\n', staff())


def self_updating_root(tmp_path: Path, *auto: str, body: str = 'backend = "attach"\n') -> Path:
    """Alpha and Beta, each with its own program; the ones named in `auto` have auto_update = true."""
    root = tmp_path / "reclaimer"
    write_root(root, list(zip(["Alpha", "Beta"], free_ports(2))), body, staff(), exe=True)
    for name in auto:
        config = root / "SERVERS" / name / "dedicated.toml"
        config.write_text("auto_update = true\n" + config.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def program_release(version: str = "0.9.19", published: float | None = None) -> ProgramRelease:
    return ProgramRelease(version, f"https://example/v{version}", "", "https://example/x.exe",
                          f"project-reclaimer-dedicated-v{version}.exe", "https://example/sums", published)


def serve_program(monkeypatch: pytest.MonkeyPatch, release: ProgramRelease, payload: bytes) -> None:
    """GitHub, as far as the updater asks it: the release, its program and its checksum list."""
    monkeypatch.setattr(serverupdates, "latest_program_release", lambda *a, **k: release)

    def fake_get(url: str, timeout: float, accept: str = "") -> bytes:
        if url.endswith("sums"):
            return (hashlib.sha256(payload).hexdigest() + f"  {release.exe_name}\n").encode()
        return payload

    monkeypatch.setattr(serverupdates, "_get", fake_get)


class StubClient:
    def __init__(self) -> None:
        self.version = "0.9.18"
        self.connected = True
        self.sent: list[tuple[str, tuple[str, ...], str | None]] = []

    async def command(self, command: str, *args: str, by: str | None = None, timeout: float | None = None) -> Reply:
        self.sent.append((command, args, by))
        if command.startswith("players"):
            return Reply(True, "1/16 players", {"count": 1}, {})
        return Reply(True, f"did {command}", None, {})
