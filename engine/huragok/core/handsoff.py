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
    lines = list(record.get("every") or [])
    for where, found in (record.get("folders") or {}).items():
        if os.path.normcase(str(where)) == folder:
            lines += found
    lines += (record.get("servers") or {}).get(server.name, [])
    return [str(line) for line in lines]
