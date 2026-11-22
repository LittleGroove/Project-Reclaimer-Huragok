"""RECLAIMER_DEDICATED_<SETTING> set where Huragok runs: the server program reads each over what its dedicated.toml
says (0.9.23 on: CONTENT, DATA, BANS, WORKSHOP_ITEMS, …; "1,2" or "1 2" for items, a number for minutes; relative
paths from the file's folder, like the file's own). Huragok reads each server the same way (0.7.0, plan 4.2): what
`apply` gives is the server as the program runs it, with every override listed as a warning. program/environment.py
has the rest (what a server's process is given, what Huragok copied into files before 0.7.0).
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any, Mapping

from .serverfolders import ServerConfig

PREFIX = "RECLAIMER_DEDICATED_"
SET_HERE = {"ONLY"}  # Huragok's own: which server one process runs
NOT_READ = {"GAME"}  # the program doesn't read these from its environment (0.9.23)
# The overrides Huragok reads for itself (variable after the prefix -> (table, key, kind)); "" is the top of the file
READ: dict[str, tuple[str, str, str]] = {
    "CONTENT": ("", "content", "path"),
    "DATA": ("", "data", "path"),
    "BANS": ("", "bans", "path"),
    "AUTO_UPDATE": ("", "auto_update", "bool"),
    "WORKSHOP_ITEMS": ("workshop", "items", "ids"),
    "WORKSHOP_UPDATE_CHECK_MINUTES": ("workshop", "update_check_minutes", "minutes"),
    "WORKSHOP_STEAMCMD": ("workshop", "steamcmd", "text"),
    "WORKSHOP_USERNAME": ("workshop", "username", "text"),
    "WORKSHOP_PROVISIONER": ("workshop", "provisioner", "text"),
}

def setting_name(variable: str) -> str:
    table, key, _ = READ[variable]
    return f"[{table}] {key}" if table else key


def parse(variable: str, text: str) -> Any:
    """The variable's value as dedicated.toml writes it, as the program reads it. ValueError: one it refuses."""
    kind = READ[variable][2] if variable in READ else "text"
    if kind == "ids":
        words = [w for w in re.split(r"[\s,]+", text.strip()) if w]
        if not words or not all(w.isdecimal() for w in words):
            raise ValueError("not a list of Workshop item IDs")
        return [int(w) for w in words]
    if kind == "minutes":
        if not text.strip().isdecimal():
            raise ValueError("not a number of minutes")
        return int(text.strip())
    if kind == "bool":
        word = text.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
        raise ValueError("not true or false")
    return text.strip()


def overrides(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """{full variable name: value} for every RECLAIMER_DEDICATED_ setting set where Huragok runs that the program
    reads over the servers' files. Huragok's own (ONLY) and those the program doesn't read aren't overrides."""
    found: dict[str, str] = {}
    for name, value in (os.environ if base is None else base).items():
        upper = name.upper()
        if upper.startswith(PREFIX) and upper[len(PREFIX):] not in (*SET_HERE, *NOT_READ) and value.strip():
            found[upper] = value
    return dict(sorted(found.items()))


def apply(server: ServerConfig, found: Mapping[str, str]) -> ServerConfig:
    """The server as the program runs it with these overrides: what Huragok reads of it (its content and data folders,
    Workshop items and checks, auto_update) taken from the variables, and one warning listing every override. A value
    the program would refuse is said and left out."""
    if not found:
        return server
    changes: dict[str, Any] = {}
    said: list[str] = []
    for full, text in found.items():
        variable = full[len(PREFIX):]
        if variable not in READ:
            said.append(f"{full} = {text}")
            continue
        try:
            value = parse(variable, text)
        except ValueError as error:
            said.append(f"{full} = {text} ({error}: the program refuses it)")
            continue
        said.append(f"{full} = {text} (over its {setting_name(variable)})")
        if variable == "CONTENT":
            changes["content_dir"] = (server.folder / str(value)).resolve()
        elif variable == "DATA":
            changes["data_dir"] = server.folder / str(value) / server.data_dir.name
        elif variable == "AUTO_UPDATE":
            changes["auto_update"] = value
        elif variable == "WORKSHOP_ITEMS":
            changes["workshop_items"] = value
        elif variable == "WORKSHOP_UPDATE_CHECK_MINUTES":
            changes["workshop_check_minutes"] = value
    return replace(server, **changes, warnings=[*server.warnings, ("environment", {"overrides": "; ".join(said)})])
