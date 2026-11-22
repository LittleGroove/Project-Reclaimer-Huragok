"""RECLAIMER_DEDICATED_<SETTING> set where Huragok runs: the server program reads each over what its dedicated.toml
says (0.9.23 on: CONTENT, DATA, BANS, WORKSHOP_ITEMS, …; "1,2" or "1 2" for items, a number for minutes; relative
paths from the file's folder, like the file's own).

Since 0.7.0 Huragok keeps its hands off the servers' files (plans/server-files-audit/plan-0.7.0.md 4.2): it no longer
copies these variables into every dedicated.toml and no longer keeps them from the servers. Each server gets
Huragok's environment as it is (with RECLAIMER_DEDICATED_ONLY, which picks the one server each process runs), and
Huragok reads each server the way the program does: a variable wins over the file. Every override is listed on each
server once, so a host knows why a server isn't doing what its file says. With docker the containers' environment is
compose's, not Huragok's: nothing is read from Huragok's own there.

Before 0.7.0 Huragok copied CONTENT, DATA, BANS and the [workshop] ones into the files; records/environment-taken-over.json
keeps what it wrote where, read by the 0.7.0 update step for the list of what Huragok used to set (taken_over()).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from ..core.serverenv import NOT_READ, PREFIX, READ, SET_HERE, apply, overrides, parse, setting_name

RECORD = "environment-taken-over.json"  # in records/: before 0.7.0, each variable's value Huragok wrote into each file


def server_environment(base: Mapping[str, str] | None = None, **extra: str) -> dict[str, str]:
    """The environment a server program Huragok runs gets: Huragok's own, as it is, with `extra` on top."""
    env = dict(os.environ if base is None else base)
    env.update(extra)
    return env


def taken_over(records: Path) -> dict[str, dict[str, str]]:
    """{variable: {server folder: the value Huragok wrote into its dedicated.toml}}, from before 0.7.0."""
    try:
        data = json.loads((records / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(variable): {str(k): v for k, v in value.items() if isinstance(v, str)}
            for variable, value in data.items() if isinstance(value, dict)}


__all__ = ["NOT_READ", "PREFIX", "READ", "RECORD", "SET_HERE", "apply", "overrides", "parse", "server_environment", "setting_name",
           "taken_over"]
