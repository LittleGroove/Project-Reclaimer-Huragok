from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_rcon import PASSWORD  # noqa: E402

# Player IDs for tests (not real players).
OWNER = "a" * 64
MOD = "b" * 64
ALICE = "c" * 64
BRAVO = "d" * 64
ADMIN = "e" * 64


def write_server_folder(folder: Path, port: int, *, name: str = "LG Test Server", host_key: str | None = None,
                        status_age: float | None = None, extra_server: str = "") -> Path:
    """A server folder like `dedicated init` makes, with one server whose RCON is on `port`."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "dedicated.toml").write_text(f"""
data = "data"
admins = ["{ADMIN}"]
[rcon]
password = "{PASSWORD}"
[[server]]
name = "{name}"
port = {port}
{extra_server}
""", encoding="utf-8")
    data = folder / "data" / name.lower().replace(" ", "-")
    data.mkdir(parents=True, exist_ok=True)
    if host_key:
        (data / "rcon-host.key").write_text(host_key, encoding="utf-8")
    if status_age is not None:
        (data / "status.json").write_text(json.dumps({"name": name, "players": [],
                                                      "updated": int(time.time() - status_age)}), encoding="utf-8")
    return data


def write_settings(folder: Path, server_folder: Path, body: str = "", commands: str | None = None) -> Path:
    """settings.toml (no database or log file), and commands.toml when `commands` is given."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "settings.toml"
    path.write_text(f"""
server_folder = "{server_folder.as_posix()}"
database = ""
log_file = ""
{body}
""", encoding="utf-8")
    if commands is not None:
        (folder / "commands.toml").write_text(commands, encoding="utf-8")
    return path


def staff(extra: str = "", cooldown: float = 0) -> str:
    """commands.toml with OWNER and MOD listed, every command at its default role."""
    return f"""
cooldown_seconds = {cooldown}
[owner]
players = ["{OWNER}"]
[moderator]
players = ["{MOD}"]
{extra}"""


async def wait_for(predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for a condition")


@pytest.fixture(autouse=True)
def no_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never ask GitHub for releases; test_updates.py hands out its own."""
    from reclaimer_supervisor import updates

    monkeypatch.setattr(updates, "latest_release", lambda *args, **kwargs: None)


@pytest.fixture
def run() -> Callable[..., Any]:
    def runner(coroutine: Any) -> Any:
        return asyncio.run(coroutine)

    return runner
