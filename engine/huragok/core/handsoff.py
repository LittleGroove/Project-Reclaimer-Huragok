"""What Huragok had set in the servers' folders before 0.7.0, from its own records: the 0.7.0 update step
(updates/hostfiles.py `hands_off`) writes records/hands-off-0.7.0.json once, and each server lists its lines once in
the warnings (rule used-to-set) until the host dismisses it. Nothing in the folders is changed back."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

HANDS_OFF_RECORD = "hands-off-0.7.0.json"  # in records/


def hands_off_lines(records: Path, server: Any) -> list[str]:
    """What Huragok had set in this server's folder before 0.7.0, one line each: what it set in every server's file,
    what it set in this folder, and what it set for this server by name."""
    try:
        record = json.loads((records / HANDS_OFF_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(record, dict):
        return []
    folder = os.path.normcase(str(server.folder))
    lines = [line for line in record.get("every") or [] if _still_there(line, server)]
    for where, found in (record.get("folders") or {}).items():
        if os.path.normcase(str(where)) == folder:
            lines += found
    lines += (record.get("servers") or {}).get(server.name, [])
    return [str(line) for line in lines]


def _still_there(line: Any, server: Any) -> bool:
    """A setting 0.6.x wrote into every server's file is said only for a server whose file has it: one added since
    (a folder dropped in after the update) never had it from Huragok (the beta.2 tests, 25 November 2026)."""
    if not str(line).startswith("[workshop] update_check_minutes"):
        return True
    import tomllib

    try:
        data = tomllib.loads(Path(server.config_path).read_text(encoding="utf-8"))
    except (OSError, AttributeError, TypeError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return True  # can't tell: as the record says
    workshop = data.get("workshop")
    return isinstance(workshop, dict) and "update_check_minutes" in workshop
