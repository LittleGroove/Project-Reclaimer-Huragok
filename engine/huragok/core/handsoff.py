"""What Huragok had set in the servers' folders before 0.7.0, from its own records: the 0.7.0 update step
(updates/hostfiles.py `hands_off`) writes records/hands-off-0.7.0.json once, and each server lists its lines once in
the warnings (rule used-to-set) until the host dismisses it. Nothing in the folders is changed back."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

HANDS_OFF_RECORD = "hands-off-0.7.0.json"  # in records/
ITEM_LINE = re.compile(r"^Workshop item (?P<item>\d+) ")  # what 0.6.x put in [workshop] items


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
    return [str(line) for line in lines if _still_there(line, server)]


def _still_there(line: Any, server: Any) -> bool:
    """A setting 0.6.x wrote into a server's file is said only while the file has it: a folder added since never had
    it from Huragok (the beta.2 tests, 25 November 2026), nor a new folder given an old server's name (the live host's
    new LG Test Server was told Huragok had listed the old one's HugeGrass Rebalance, 25 November 2026)."""
    item = ITEM_LINE.match(str(line))
    if item is None and not str(line).startswith("[workshop] update_check_minutes"):
        return True
    import tomllib

    try:
        data = tomllib.loads(Path(server.config_path).read_text(encoding="utf-8"))
    except (OSError, AttributeError, TypeError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return True  # can't tell: as the record says
    workshop = data.get("workshop")
    if not isinstance(workshop, dict):
        return False
    if item is not None:
        items = workshop.get("items")
        return isinstance(items, list) and any(str(listed).strip() == item.group("item") for listed in items)
    return "update_check_minutes" in workshop
