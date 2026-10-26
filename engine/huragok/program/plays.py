"""What a server plays, written into its dedicated.toml: one kind of thing at a time, and only what the server's
version of the program takes.

A server's settings come in three areas, each with its own home:
  Huragok's own        settings.toml: how Huragok runs, for every server and per server (core/settings.py,
                       core/serversettings.py)
  how the server runs  dedicated.toml: joining, voice, safety, mods, the network (program/settings.py)
  what it plays        dedicated.toml's [[server]] block names it (this module writes it; games/plays.py says which
                       kinds there are and how the dashboard says them), and the files it names: playlists
                       (games/halo3.py), custom games and game options (games/halo3_options.py), from what's
                       installed (content/catalogue.py)
"""

from __future__ import annotations

import json
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.files import write_atomic
from ..core.tomledit import (
    TomlEditError, change_server_settings, read_toml, remove_table_key, replace_server_blocks, set_table_key,
    set_top_key,
)
from ..games.halo3 import problems_from_show
from ..games.plays import PLAYS_KEYS
from .settings import version_problem

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig

# What-it-plays keys and campaign modifiers older programs don't have (0.9.18 refuses each as an unknown field).
PLAYS_SINCE = {"odst": "0.9.19", "sprint": "0.9.19", "sprint_speed_percent": "0.9.19",
               "sprint_stamina_seconds": "0.9.19", "skulls": "0.9.19"}
ODST_SINCE = {"story_characters": "0.9.21", "options": "0.9.21"}  # keys of an `odst = { … }` setup, by version


def plays_problem(values: dict[str, Any], program_version: str | None) -> str | None:
    """What-it-plays values (odst, campaign modifiers) a server on `program_version` can't take, or None."""
    keys = [k for k in values if k in PLAYS_SINCE]
    for kind in ("campaign", "odst"):
        modifiers = values.get(kind, {}).get("modifiers") if isinstance(values.get(kind), dict) else None
        keys += [k for k in modifiers if k in PLAYS_SINCE] if isinstance(modifiers, dict) else []
    setup = values.get("odst") if isinstance(values.get("odst"), dict) else {}
    since = {**PLAYS_SINCE, **{k: v for k, v in ODST_SINCE.items() if k in setup}}
    keys += [k for k in setup if k in ODST_SINCE]
    return next((p for k in keys if (p := version_problem(k, since[k], program_version))), None)


CHECK_FILE = ".huragok-check.toml"  # beside a server's dedicated.toml while its program checks a change
CHECK_PORTS = range(50500, 51000)  # the draft's own port: away from servers' (49170-49199) and Windows' own
CHECK_SECONDS = 300.0  # `dedicated check` takes a tenth of a second (0.9.21); more only to fetch a Workshop item


@dataclass(frozen=True)
class Problem:
    text: str
    game: int | None = None  # the playlist game it's about, numbered from 1; None: the whole thing


@dataclass
class PlaysCheck:
    problems: list[Problem] = field(default_factory=list)
    asked: bool = True  # False: the program couldn't be run, so only Huragok's own checks were made (why: unasked)
    unasked: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def check_plays(tool: Any, server: ServerConfig, values: dict[str, Any], playlist: Any = None) -> PlaysCheck:
    """What the server's own program says of playing `values` (the [[server]] keys write_plays writes), before any
    of it is written: the check its start and its reload make, which a refusal there would hold up (a reload
    applies nothing while a check fails). `playlist`: a draft of the playlist `values` names, checked as it would
    be saved. Huragok's own checks come first (a playlist's game count, weights, two games the same), at once;
    then `dedicated check` on a draft config beside the real one (CHECK_FILE: this server's block alone, with
    `values`, a port of its own, the master and self-update off), removed afterwards, as is the draft playlist.

    Only what the change brings counts: when the check fails, the server as it is now is checked the same way, and
    a problem it has already (game files missing, a ban list it can't read) is left to the window's C. A
    playlist's own games' problems always count, so a playlist the server can't play is never saved."""
    if playlist is not None:
        playlist.number_choices()
        local = [Problem(text) for text in playlist.problems()]
        local += [Problem(f"the same game as game {first} (a different name doesn't make it another game): take "
                          "one out, or raise one's weight to play it more often", number)
                  for number, first in playlist.duplicates().items()]
        if local:
            return PlaysCheck(local)
    draft = None
    try:
        data = read_toml(server.config_path)
        blocks = data.get("server", []) if isinstance(data.get("server"), list) else []
        own = next((b for b in blocks if str(b.get("name", "")).lower() == server.name.lower()), None)
        if own is None:
            return PlaysCheck([Problem(f"{server.config_path.name} has no [[server]] block called {server.name}")])
        block = {k: v for k, v in own.items() if k not in PLAYS_KEYS}
        block.update(values)
        if playlist is not None:
            draft = playlist.path.with_name(f".{playlist.path.name.removesuffix('.playlist.json')}.huragok-check.json")
            draft.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(draft, json.dumps(playlist.data(), indent=2, ensure_ascii=False) + "\n")
            block["playlist"] = draft_ref = draft.relative_to(server.folder).as_posix() \
                if draft.is_relative_to(server.folder) else str(draft)
        result = _run_check(tool, server, block)
        if result.ok:
            return PlaysCheck()
        problems = _refusals(result.text, server.name) or [Problem(result.problem)]
        before = _run_check(tool, server, dict(own))
        already = [] if before.ok else _refusals(before.text, server.name) or [Problem(before.problem)]
        problems = [p for p in problems if p.game is not None or p.text not in {a.text for a in already}]
        if not problems and already:  # its own problem may stop the check before it reaches the change
            return PlaysCheck(asked=False, unasked=f"the server program refuses its file already ({already[0].text}), "
                                                   "so the change couldn't be checked on its own; C says more")
        if problems and draft is not None:  # each game's own words, where `check` gave none
            named = {p.game for p in problems if p.game is not None}
            shown = tool.playlist_show(server.folder, draft_ref)
            problems += [Problem(why, number) for number, why in problems_from_show(shown.text).items()
                         if number not in named]
        return PlaysCheck(problems)
    except (OSError, ValueError, TomlEditError) as error:
        return PlaysCheck(asked=False, unasked=str(error))
    finally:
        if draft is not None:
            draft.unlink(missing_ok=True)


def _run_check(tool: Any, server: ServerConfig, block: dict[str, Any]) -> Any:
    """`dedicated check` of a draft config beside the server's own, holding `block` alone on a port of its own: no
    console port of its own (the running server holds that one; the console follows the free game port) and no
    Workshop items (a check mustn't start SteamCMD fetching one)."""
    check = server.folder / CHECK_FILE
    try:
        shutil.copy2(server.config_path, check)
        draft = {k: v for k, v in block.items() if k != "rcon_port"}
        replace_server_blocks(check, [{**draft, "port": _free_port()}])
        remove_table_key(check, "workshop", "items")
        set_top_key(check, "auto_update", False)
        set_top_key(check, "upnp", False)
        set_table_key(check, "master", "enabled", False)
        return tool.run_sync(server.folder, "check", config=CHECK_FILE, timeout=CHECK_SECONDS)
    finally:
        check.unlink(missing_ok=True)


def _refusals(text: str, server_name: str) -> list[Problem]:
    """`dedicated check`'s Error lines (and the lines under one), as problems, each tied to its game when it names
    one: "Error: Server 'Alpha': playlists/x.playlist.json: Game 2 duplicates another map/game combination…"."""
    found: list[Problem] = []
    under = False
    for line in text.splitlines():
        if line.startswith("Error:"):
            text_ = line.removeprefix("Error:").strip()
            under = True
        elif under and line.startswith("       ") and line.strip():
            text_ = line.strip()
        else:
            under = False
            continue
        text_ = re.sub(rf"^Server '{re.escape(server_name)}':\s*", "", text_, flags=re.IGNORECASE)
        text_ = re.sub(r"^\S+\.json:\s*", "", text_)
        game = re.match(r"^Game (\d+)\b", text_)
        found.append(Problem(text_, int(game.group(1)) if game else None))
    return found


def _free_port() -> int:
    """A port free for UDP and TCP both (a server's games and its console), for the draft config's server."""
    for port in CHECK_PORTS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
                udp.bind(("0.0.0.0", port))
                tcp.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
    raise OSError(f"no free port from {CHECK_PORTS.start} to {CHECK_PORTS.stop - 1} for the check")


def applies_when(before: str, after: str) -> str:
    """When a change of what a server plays applies, as the program's guide has it. `before` and `after` are plays
    kinds (games/plays.py plays_kind): a new playlist from the next vote, a new map or game from the next game, a
    change of kind (and most campaign or ODST changes) once nobody is on, when the server restarts by itself."""
    before, after = ("map" if kind == "default" else kind for kind in (before, after))
    if after in ("campaign", "odst"):
        return "once nobody is on (the server restarts by itself for most campaign and ODST changes)"
    if before != after:
        return "once nobody is on (the server restarts by itself when the kind of game changes)"
    return "from the next vote" if after == "playlist" else "from the next game"


def write_plays(config: Path, server_name: str, values: dict[str, Any]) -> None:
    """What it plays: the new keys in, every other kind's keys out (a server plays one kind of thing), in one
    write, so a change that can't be made safely leaves the file as it was rather than playing nothing."""
    change_server_settings(config, server_name, values, PLAYS_KEYS, what="what it plays")

