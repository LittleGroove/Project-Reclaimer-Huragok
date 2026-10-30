"""What the bot keeps in records/ for the window and for its next run: its state (a second window reads it), the one-time
claim code, the logins made today (Discord resets a token at 1,000 a day), and which commands are registered where (200
registrations a day per Discord, so only when the list changes)."""

from __future__ import annotations

import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

from ..core.files import write_atomic

STATE_FILE = "discord-state.json"
CLAIM_FILE = "discord-claim.json"
LOGINS_FILE = "discord-logins.json"
COMMANDS_FILE = "discord-commands.json"
CLAIM_MINUTES = 15
CLAIM_TRIES = 5  # wrong codes before this one stops working
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # none to take for another
STATE_STALE = 90.0  # a state older than this is from a Huragok that isn't running


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, json.dumps(data, indent=1))


# The state, for the window ------------------------------------------------------------------------------------


def write_state(records: Path, **state: Any) -> None:
    _write(records / STATE_FILE, {**state, "updated": time.time()})


def read_state(records: Path) -> dict[str, Any]:
    """The bot's state as last written; `stale` when no running Huragok has said so lately."""
    state = _read(records / STATE_FILE)
    if state:
        state["stale"] = time.time() - float(state.get("updated") or 0) > STATE_STALE
    return state


# The claim code -----------------------------------------------------------------------------------------------


def new_code(records: Path) -> str:
    """A code for /huragok claim: whoever types it in Discord becomes an owner, the host's say-so carried across."""
    code = "".join(secrets.choice(ALPHABET) for _ in range(8))
    shown = f"{code[:4]}-{code[4:]}"
    _write(records / CLAIM_FILE, {"code": code, "expires": time.time() + CLAIM_MINUTES * 60, "tries": 0})
    return shown


def use_code(records: Path, typed: str) -> bool:
    """True once: the code is gone after it's used, after CLAIM_TRIES wrong ones, or when it's out of time."""
    path = records / CLAIM_FILE
    data = _read(path)
    code = str(data.get("code") or "")
    if not code:
        return False
    if time.time() > float(data.get("expires") or 0):
        path.unlink(missing_ok=True)
        return False
    if hmac.compare_digest(typed.replace("-", "").replace(" ", "").upper().encode("utf-8"), code.encode("utf-8")):
        path.unlink(missing_ok=True)
        return True
    data["tries"] = int(data.get("tries") or 0) + 1
    if data["tries"] >= CLAIM_TRIES:
        path.unlink(missing_ok=True)
    else:
        _write(path, data)
    return False


# Logins -------------------------------------------------------------------------------------------------------


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def logins_today(records: Path) -> int:
    data = _read(records / LOGINS_FILE)
    return int(data.get("count") or 0) if data.get("day") == _today() else 0


def count_login(records: Path) -> int:
    count = logins_today(records) + 1
    _write(records / LOGINS_FILE, {"day": _today(), "count": count})
    return count


# Commands registered ------------------------------------------------------------------------------------------


def registered(records: Path) -> dict[str, str]:
    """Discord -> what its commands were last registered as (a digest)."""
    return {str(key): str(value) for key, value in _read(records / COMMANDS_FILE).items()}


def save_registered(records: Path, known: dict[str, str]) -> None:
    _write(records / COMMANDS_FILE, known)
