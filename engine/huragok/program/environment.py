"""Where a server's content is, and which Workshop items it has, is dedicated.toml's to say, and Huragok's to keep.

The server program reads RECLAIMER_DEDICATED_<SETTING> from its environment over what its dedicated.toml says
(0.9.23: CONTENT, DATA, BANS, WORKSHOP_ITEMS, …; "1,2" or "1 2" for items, a number for minutes; relative paths from
the file's folder, like the file's own). Huragok reads the file: a content folder set in the environment had the
program fetch, load and update mods in one folder while Huragok looked for its receipts, moved old copies aside and
put new ones in another, so a Workshop update never went in (tested on 0.9.23: `dedicated content` with
RECLAIMER_DEDICATED_CONTENT=content2 lists content2).

So each of these set where Huragok runs is taken over once, as Huragok starts: written into the dedicated.toml of every
server folder there is (a disabled server's too: the folders the servers have been using stay theirs) and never passed
on again, to a server or to a `dedicated` command. Once written into a folder's file it's the file's: the same value
again isn't written over a change made since (G, D, or by hand), even after a start with the variable unset (a service
started without it, say); a new value is. Each folder is kept apart (records/environment-taken-over.json): one that
couldn't be written is tried again at the next start, and one made later gets the value once, as the program would
have used it. WORKSHOP_UPDATE_CHECK_MINUTES is Huragok's own ([workshop_updates] check_minutes writes it into every
file), so it's only left out. The program's other overrides (game settings) are left as they are, said once.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ..core.files import write_atomic
from ..core.tomledit import read_toml, set_table_key, set_top_key

log = logging.getLogger(__name__)

PREFIX = "RECLAIMER_DEDICATED_"
SET_HERE = {"ONLY", "AUTO_UPDATE"}  # Huragok's own: which server one process runs; the file's auto_update decides
TAKEN_OVER: dict[str, tuple[str, str, str]] = {  # variable (after the prefix) -> (table, key, kind); "" is the top
    "CONTENT": ("", "content", "path"),
    "DATA": ("", "data", "path"),
    "BANS": ("", "bans", "path"),
    "WORKSHOP_ITEMS": ("workshop", "items", "ids"),
    "WORKSHOP_STEAMCMD": ("workshop", "steamcmd", "text"),
    "WORKSHOP_USERNAME": ("workshop", "username", "text"),
    "WORKSHOP_PROVISIONER": ("workshop", "provisioner", "text"),
}
# Left out of what the servers get, never written: Huragok writes [workshop] update_check_minutes into every file from
# [workshop_updates] check_minutes, so a value taken over was gone 30 seconds later (audit 6 November).
HURAGOKS = {"WORKSHOP_UPDATE_CHECK_MINUTES": "[workshop_updates] check_minutes in settings.toml"}
NOT_READ = {"GAME"}  # the program doesn't read these from its environment (0.9.23)
RECORD = "environment-taken-over.json"  # in records/: by variable, each server folder's value written into its file


def server_environment(base: Mapping[str, str] | None = None, **extra: str) -> dict[str, str]:
    """The environment a server program Huragok runs gets: Huragok's, without what dedicated.toml says for it."""
    env = {name: value for name, value in (os.environ if base is None else base).items()
           if not (name.upper().startswith(PREFIX) and name.upper()[len(PREFIX):] in (*TAKEN_OVER, *HURAGOKS))}
    env.update(extra)
    return env


def setting_name(variable: str) -> str:
    table, key, _ = TAKEN_OVER[variable]
    return f"[{table}] {key}" if table else key


def parse(variable: str, text: str) -> Any:
    """The variable's value as dedicated.toml writes it, as the program reads it. ValueError: one it refuses."""
    kind = TAKEN_OVER[variable][2] if variable in TAKEN_OVER else "minutes"
    if kind == "ids":
        words = [w for w in re.split(r"[\s,]+", text.strip()) if w]
        if not words or not all(w.isdecimal() for w in words):
            raise ValueError("not a list of Workshop item IDs")
        return [int(w) for w in words]
    if kind == "minutes":
        if not text.strip().isdecimal():
            raise ValueError("not a number of minutes")
        return int(text.strip())
    return text.strip()


def found(base: Mapping[str, str] | None = None) -> tuple[dict[str, str], list[str]]:
    """({variable after the prefix: value} for those taken over, [full names of the program's other overrides]).
    Huragok's own and those the program doesn't read are neither."""
    taken: dict[str, str] = {}
    others: list[str] = []
    for name, value in (os.environ if base is None else base).items():
        upper = name.upper()
        if not upper.startswith(PREFIX) or upper[len(PREFIX):] in (*SET_HERE, *HURAGOKS, *NOT_READ):
            continue
        if upper[len(PREFIX):] in TAKEN_OVER:
            if value.strip():
                taken[upper[len(PREFIX):]] = value
        else:
            others.append(upper)
    return taken, sorted(others)


def _read_record(path: Path, folders: list[Path]) -> dict[str, dict[str, str]]:
    """{variable: {folder: the value written into its file}}. Before 6 November a variable had one value, written into
    every folder there was then: read as each folder's now."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    record: dict[str, dict[str, str]] = {}
    for variable, value in data.items():
        if isinstance(value, str):
            record[variable] = {str(folder): value for folder in folders}
        elif isinstance(value, dict):
            record[variable] = {str(k): v for k, v in value.items() if isinstance(v, str)}
    return record


def _current(config: Path, table: str, key: str) -> Any:
    data = read_toml(config)
    return (data.get(table) or {}).get(key) if table else data.get(key)


def take_over(folders: list[Path], config_name: str, records: Path,
              base: Mapping[str, str] | None = None) -> list[str]:
    """Each taken-over variable set where Huragok runs, written into the dedicated.toml of each of `folders` (every
    server folder there is) that hasn't had that value before. Returns what was changed, one line per file and setting
    (logged too)."""
    taken, others = found(base)
    for name, setting in HURAGOKS.items():
        if PREFIX + name in {key.upper() for key in (os.environ if base is None else base)}:
            log.warning("%s%s is set where Huragok runs: Huragok sets that for every server from %s, so it's no longer "
                        "passed to the servers", PREFIX, name, setting)
    for name in others:
        log.warning("%s is set where Huragok runs: the program reads it over every server's %s, so what Huragok's "
                    "screens show for that setting isn't what the servers use. Set it in %s instead and remove the "
                    "variable", name, config_name, config_name)
    record_path = records / RECORD
    record = _read_record(record_path, folders)  # kept while a variable's unset: a start without it forgets nothing
    before = json.dumps(record, sort_keys=True)
    changed: list[str] = []
    for variable, text in sorted(taken.items()):
        full = PREFIX + variable
        done = record.setdefault(full, {})
        waiting = [folder for folder in folders if done.get(str(folder)) != text]
        if not waiting:
            continue  # written already: the files say it now, or what the host changed it to since
        try:
            value = parse(variable, text)
        except ValueError as error:
            log.warning("%s is set where Huragok runs, but %s (%r), which the program would refuse: it's no longer "
                        "passed to the servers, and their %s stays as it is", full, error, text, config_name)
            continue
        table, key, _ = TAKEN_OVER[variable]
        for folder in waiting:
            config = folder / config_name
            try:
                if _current(config, table, key) == value:
                    done[str(folder)] = text
                    continue
                if table:
                    set_table_key(config, table, key, value, before="master")
                else:
                    set_top_key(config, key, value)
            except (OSError, ValueError) as error:  # this folder alone: tried again at the next start
                log.error("Couldn't write %s = %r into %s (%s): until it's there, the server uses its file's %s, not "
                          "%s's", setting_name(variable), value, config, error, setting_name(variable), full)
                continue
            done[str(folder)] = text
            line = f"{config}: {setting_name(variable)} = {value!r}, from {full}"
            changed.append(line)
            log.warning("%s was set where Huragok runs, so the program used it over %s's %s. Huragok keeps it in "
                        "the file now (%s = %r) and no longer passes the variable on: change it there (G, D) from now "
                        "on", full, config, setting_name(variable), setting_name(variable), value)
    record = {variable: done for variable, done in record.items() if done}
    if json.dumps(record, sort_keys=True) != before:
        try:
            records.mkdir(parents=True, exist_ok=True)
            write_atomic(record_path, json.dumps(record, indent=1, sort_keys=True) + "\n")
        except OSError as error:
            log.warning("Couldn't write %s (%s): the variables are compared with the files again next time",
                        record_path, error)
    return changed


__all__ = ["HURAGOKS", "PREFIX", "TAKEN_OVER", "found", "parse", "server_environment", "take_over"]
