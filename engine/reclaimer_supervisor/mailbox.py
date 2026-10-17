"""Requests to the running supervisor from outside it: `start.cmd stop`, a second window, scripts.

A request is one JSON file in records/requests/, picked up within a second by the
supervisor, which answers in <id>.reply.json. A request nobody picked up for ten
minutes is thrown away: it isn't what anyone wants any more.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FOLDER = "requests"
STALE_SECONDS = 600
VERBS = ("start", "stop", "restart", "cancel", "say", "rcon", "autostart", "stop_all", "close", "update", "check_update",
         "check_program_update", "update_program")


@dataclass
class Request:
    id: str
    verb: str
    server: str = "all"
    options: dict[str, Any] = field(default_factory=dict)
    time: float = field(default_factory=time.time)


@dataclass
class Reply:
    ok: bool
    message: str
    text: str = ""


def folder(records: Path) -> Path:
    path = records / FOLDER
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def post(records: Path, verb: str, server: str = "all", **options: Any) -> str:
    """Leave a request for the supervisor. Returns its id."""
    request = Request(uuid.uuid4().hex, verb, server, options)
    _write_atomic(folder(records) / f"{request.id}.json",
                  {"id": request.id, "verb": verb, "server": server, "options": options, "time": request.time})
    return request.id


def take_reply(records: Path, request_id: str) -> Reply | None:
    path = folder(records) / f"{request_id}.reply.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    path.unlink(missing_ok=True)
    return Reply(bool(data.get("ok")), str(data.get("message", "")), str(data.get("text", "")))


def send(records: Path, verb: str, server: str = "all", wait: float = 15.0, **options: Any) -> Reply:
    """Post a request and wait for the answer (blocking; for the command line)."""
    request_id = post(records, verb, server, **options)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        reply = take_reply(records, request_id)
        if reply is not None:
            return reply
        time.sleep(0.2)
    (folder(records) / f"{request_id}.json").unlink(missing_ok=True)
    return Reply(False, "the supervisor didn't answer (is it still running?)")


def read(records: Path) -> list[Request]:
    """Every waiting request, oldest first; each file is removed as it's read."""
    requests: list[Request] = []
    path = records / FOLDER
    if not path.is_dir():
        return requests
    for file in sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if file.name.endswith(".reply.json"):
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        finally:
            file.unlink(missing_ok=True)
        request = Request(str(data.get("id") or file.stem), str(data.get("verb", "")), str(data.get("server", "all")),
                          dict(data.get("options") or {}), float(data.get("time") or 0))
        if time.time() - request.time > STALE_SECONDS:
            reply(records, request.id, False, "too old: nobody picked it up for ten minutes")
            continue
        requests.append(request)
    return requests


def reply(records: Path, request_id: str, ok: bool, message: str, text: str = "") -> None:
    _write_atomic(folder(records) / f"{request_id}.reply.json", {"ok": ok, "message": message, "text": text})


def clear_old_replies(records: Path, older_than: float = 3600) -> None:
    path = records / FOLDER
    if not path.is_dir():
        return
    cutoff = time.time() - older_than
    for file in path.glob("*.reply.json"):
        try:
            if file.stat().st_mtime < cutoff:
                file.unlink()
        except OSError:
            pass
