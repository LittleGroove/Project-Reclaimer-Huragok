"""Requests to the running supervisor from outside it: `start.cmd stop`, a second window, scripts.

A request is one JSON file in records/requests/, picked up within a second by the
supervisor, which answers in <id>.reply.json. A request nobody picked up for ten
minutes is thrown away: it isn't what anyone wants any more.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .files import mtime, write_atomic

FOLDER = "requests"
STALE_SECONDS = 600
VERBS = ("start", "stop", "restart", "cancel", "say", "rcon", "autostart", "server_setting", "stop_all", "close",
         "update", "check_update", "check_program_update", "update_program", "check_workshop", "update_workshop",
         "game_files", "game_files_retry")


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
    write_atomic(path, json.dumps(data))


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
    return Reply(False, "Huragok didn't answer (is it still running?)")


def parse(data: Any, fallback_id: str) -> Request:
    """A request file's contents as a Request; ValueError (saying what's wrong) when it isn't one. Anything can
    leave a file here, so nothing about its shape is taken on trust."""
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    request_id = data.get("id") or fallback_id
    verb, server, options, stamp = data.get("verb", ""), data.get("server", "all"), data.get("options") or {}, \
        data.get("time") or 0
    if not isinstance(request_id, str) or not request_id or any(c in request_id for c in "/\\:") or ".." in request_id:
        raise ValueError("its id isn't a plain name")
    if not isinstance(verb, str) or not verb:
        raise ValueError("verb must be a word")
    if not isinstance(server, str):
        raise ValueError("server must be a name")
    if not isinstance(options, dict):
        raise ValueError("options must be a JSON object")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        raise ValueError("time must be a number (Unix seconds)")
    return Request(request_id, verb, server, options, float(stamp))


def read(records: Path) -> list[Request]:
    """Every waiting request, oldest first; each file is removed as it's read. One that can't be understood is
    answered so (when it can be) and skipped."""
    requests: list[Request] = []
    path = records / FOLDER
    if not path.is_dir():
        return requests
    for file in sorted(path.glob("*.json"), key=mtime):
        if file.name.endswith(".reply.json"):
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
            if file.exists() and mtime(file) > time.time() - 5:
                continue  # perhaps still being written by something that doesn't swap it in whole: next time
        try:
            file.unlink(missing_ok=True)
        except OSError:
            continue
        try:
            request = parse(data, file.stem) if data is not None else None
        except ValueError as error:
            _refuse(records, file.stem, f"not understood: {error}")
            continue
        if request is None:
            _refuse(records, file.stem, "not understood: it isn't JSON")
        elif time.time() - request.time > STALE_SECONDS:
            _refuse(records, request.id, "too old: nobody picked it up for ten minutes")
        else:
            requests.append(request)
    return requests


def _refuse(records: Path, request_id: str, message: str) -> None:
    try:
        reply(records, request_id, False, message)
    except OSError:
        pass


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
