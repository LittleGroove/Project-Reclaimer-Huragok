"""What a server plays, as its own program judges it: the check a change of its dedicated.toml or of a playlist
would meet, asked before a file the host chose is copied in (a playlist copied from another server, a version put
back), so the host reads the program's own words first; and its verdict on what the server plays now (plays_verdict:
`dedicated check` of a draft and `dedicated playlist list`), each refusal tied to its playlist file and game, with the
"Skipped content file …" and "[modes] …" lines beside them, which content/filehealth.py turns into warnings. Never a
gate: a warning, and the host's action goes ahead.

A server's settings come in three areas, each with its own home:
  Huragok's own        settings.toml: how Huragok runs, for every server and per server (core/settings.py,
                       core/serversettings.py)
  how the server runs  dedicated.toml: joining, voice, safety, mods, the network (program/settings.py)
  what it plays        dedicated.toml's [[server]] block names it, and the files it names: playlists
                       (games/halo3.py), custom games (games/halo3_options.py), from what's installed
                       (content/catalogue.py). Huragok never writes it (0.7.0): the host does, by hand;
                       games/plays.py says which kinds there are and how the dashboard says them.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.files import write_atomic
from ..core.tomledit import TomlEditError, read_toml, remove_table_key, replace_server_blocks, set_table_key, set_top_key
from ..games.halo3 import entry_game, game_problems, problems_from_show
from ..games.plays import PLAYS_KEYS
from .settings import version_problem

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig

# What-it-plays keys and campaign modifiers older programs don't have (0.9.18 refuses each as an unknown field).
PLAYS_SINCE = {"odst": "0.9.19", "sprint": "0.9.19", "sprint_speed_percent": "0.9.19",
               "sprint_stamina_seconds": "0.9.19", "skulls": "0.9.19", "reach": "0.10.0"}
ODST_SINCE = {"story_characters": "0.9.21", "options": "0.9.21"}  # keys of an `odst = { … }` setup, by version


def plays_problem(values: dict[str, Any], program_version: str | None) -> str | None:
    """What-it-plays values (odst, campaign modifiers, a map of Reach's, Combat Evolved's or Halo 2's) a server on
    `program_version` can't take, or None."""
    game = entry_game(values) if "map" in values else None
    if game is not None and not game.plays_on(program_version):
        return (f"{values['map']} is {game.title}'s map: it needs Project Reclaimer {game.since} or newer (this server "
                f"has {program_version})")
    keys = [k for k in values if k in PLAYS_SINCE]
    for kind in ("campaign", "odst"):
        modifiers = values.get(kind, {}).get("modifiers") if isinstance(values.get(kind), dict) else None
        keys += [k for k in modifiers if k in PLAYS_SINCE] if isinstance(modifiers, dict) else []
    setup = values.get("odst") if isinstance(values.get("odst"), dict) else {}
    since = {**PLAYS_SINCE, **{k: v for k, v in ODST_SINCE.items() if k in setup}}
    keys += [k for k in setup if k in ODST_SINCE]
    return next((p for k in keys if (p := version_problem(k, since[k], program_version))), None)


CHECK_FILE = ".huragok-check.toml"  # beside a server's dedicated.toml while its program checks a change: each check
# its own (.huragok-check-<process>-<n>.toml), so two at once (two windows) never write over each other's draft
# (audit 2026-11, 8.4)
_checks = itertools.count(1)


def _check_name(stem: str, suffix: str) -> str:
    return f"{stem}-{os.getpid()}-{next(_checks)}{suffix}"
CHECK_PORTS = range(50500, 51000)  # the draft's own port: away from servers' (49170-49199) and Windows' own
CHECK_SECONDS = 300.0  # `dedicated check` takes a tenth of a second (0.9.21); more only to fetch a Workshop item


@dataclass(frozen=True)
class Problem:
    text: str
    game: int | None = None  # the playlist game it's about, numbered from 1; None: the whole thing
    file: str | None = None  # the playlist file it names, as the program wrote it ("playlists/x.playlist.json")


@dataclass(frozen=True)
class PlaylistDraft:
    """A playlist file as it would be written, for check_plays: where it would go and its exact text."""

    path: Path
    text: str


@dataclass
class PlaysCheck:
    problems: list[Problem] = field(default_factory=list)
    asked: bool = True  # False: the program couldn't be run, so only Huragok's own checks were made (why: unasked)
    unasked: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def check_plays(tool: Any, server: ServerConfig, values: dict[str, Any], playlist: Any = None) -> PlaysCheck:
    """What the server's own program says of playing `values` (what its [[server]] block would name), before a file
    is written: the check its start and its reload make, which a refusal there would hold up (a reload applies
    nothing while a check fails). `playlist`: the playlist `values` names as it would be written, checked in its
    place (a PlaylistDraft, its exact text; or a Playlist, as its data). Only the program's own words: `dedicated
    check` on a draft config beside the real one (CHECK_FILE: this server's block alone, with `values`, a port of its
    own, the master and self-update off), removed afterwards, as is the draft playlist. Nothing of the server's is
    changed.

    Only what the change brings counts: when the check fails, the server as it is now is checked the same way, and
    a problem it has already (game files missing, a ban list it can't read) is left to the window's C. A
    playlist's own games' problems always count."""
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
            draft = playlist.path.with_name(
                _check_name(f".{playlist.path.name.removesuffix('.playlist.json')}.huragok-check", ".json"))
            draft.parent.mkdir(parents=True, exist_ok=True)
            text = playlist.text if isinstance(playlist, PlaylistDraft) else \
                json.dumps(playlist.data(), indent=2, ensure_ascii=False) + "\n"
            write_atomic(draft, text)
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
    check = _draft_config(server, block)
    try:
        return tool.run_sync(server.folder, "check", config=check.name, timeout=CHECK_SECONDS)
    finally:
        check.unlink(missing_ok=True)


LONG_LIST = re.compile(r",? expected one of .{80,}$")  # 0.10's "unknown field `rules`, expected one of `name`, …"


def _refusals(text: str, server_name: str) -> list[Problem]:
    """`dedicated check`'s refusals, as problems, each tied to its game when it names one. Two shapes:
    0.9: Error lines (and the lines under one): "Error: Server 'Alpha': playlists/x.playlist.json: Game 2 duplicates
    another map/game combination…".
    0.10 (checked 2026-11-06): the servers that can't start under "Not ready:", one line each, a playlist's games on
    it together ("  Alpha: playlists/x.playlist.json: game 2: no multiplayer map "Atlantiss" is installed; …; game 6:
    Guardian Slayer is listed twice (raise its weight instead)"); a value the program won't take on its own line,
    named by the server (`[[server]] "Alpha": launch_timeout: 0 to 300 seconds`, `"Alpha": max_players is 1
    through 128`) or by the file, with where in it on the next line (`x.toml: tick_rate is 30 or 60, not 45` /
    "in `server.tick_rate`")."""
    found: list[Problem] = []
    name = re.escape(server_name)
    under = ""  # error (0.9's Error lines), ready (0.10's Not ready list), file (0.10's file line, its `in` next)
    for line in text.splitlines():
        if line.startswith("Error:"):
            text_, under = line.removeprefix("Error:").strip(), "error"
        elif under == "error" and line.startswith("       ") and line.strip():
            text_ = line.strip()
        elif line.strip() == "Not ready:":
            under = "ready"
            continue
        elif under == "ready" and line.startswith("  ") and line.strip():
            text_ = re.sub(rf"^{name}:\s*", "", line.strip(), flags=re.IGNORECASE)  # the server it's about: this one
        elif under == "file" and line.startswith("in `") and found:
            found[-1] = Problem(f"{found[-1].text} (in {line.removeprefix('in ').strip('` ')})", found[-1].game)
            under = ""
            continue
        elif match := re.match(rf'^(?:\[\[server\]\] )?"{name}":\s*(.+)$', line, re.IGNORECASE):
            text_, under = match.group(1).strip(), ""
        elif match := re.match(r"^\S+\.toml:\s*(.+)$", line):
            text_, under = LONG_LIST.sub("", match.group(1).strip()), "file"
        else:
            under = ""
            continue
        text_ = re.sub(rf"^Server '{name}':\s*", "", text_, flags=re.IGNORECASE)
        named = re.match(r"^(\S+\.json):\s*", text_)
        file = named.group(1).replace("\\", "/") if named else None
        text_ = text_[named.end():] if named else text_
        games = game_problems(text_)
        if games:
            found += [Problem(f"Game {number}: {why}", number, file) for number, why in games.items()]
            continue
        game = re.match(r"^Game (\d+)\b", text_)
        found.append(Problem(text_, int(game.group(1)) if game else None, file))
    return found


# What else `dedicated check` (and `content`, `playlist`) says of the content folder (0.10.8, verified):
# "Skipped content file <path>: <reason>" for a file it leaves out, and "[modes] <path>: read without <field>, which
# this version does not know" or "[modes] <path>: the game type's value instead: <why>" for one it reads in part.
SKIPPED_FILE = re.compile(r"^(?:\d\d:\d\d:\d\d )?Skipped content file (?P<path>.+?): (?P<reason>.+?)\s*$")
MODES_NOTE = re.compile(r"^(?:\d\d:\d\d:\d\d )?\[modes\] (?P<path>.+?): (?P<note>(?:read without|the game type's "
                        r"value instead).+?)\s*$")
NO_STEAM = "No Steam runtime was found for Steam Workshop downloads"  # `workshop:` names make check fetch (6.2)


def skipped_files(text: str) -> dict[str, str]:
    """Every content file the program says it leaves out, by its path as printed (slashes made forward): its reason."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if match := SKIPPED_FILE.match(line.strip()):
            found[match.group("path").replace("\\", "/")] = match.group("reason").rstrip(".")
    return found


def mode_notes(text: str) -> list[tuple[str, str]]:
    """(path, what it says) for each `[modes]` line: a field or option of a game mode the program reads without."""
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        if (match := MODES_NOTE.match(line.strip())) and (pair := (match.group("path").replace("\\", "/"),
                                                                   match.group("note").rstrip("."))) not in found:
            found.append(pair)
    return found


def listed_problems(text: str) -> dict[str, list[Problem]]:
    """`dedicated playlist list`: each playlist file the program can't play as it is, by its path (slashes forward),
    with its problems, game by game where it numbers them. 0.10.8: "playlists\\x.playlist.json · played by A ·
    PROBLEM: playlists\\x.playlist.json: game 1: …"; 0.9: the file's line, its problems on the lines under it."""
    from .dedicated import parse_playlists

    found: dict[str, list[Problem]] = {}
    for info in parse_playlists(text):
        said = (info.problem or "").strip()
        if not said:
            continue
        file = info.file.replace("\\", "/")
        said = re.sub(r"^PROBLEM:\s*", "", said)
        said = re.sub(r"^\S+\.json:\s*", "", said)
        games = game_problems(said)
        found[file] = [Problem(f"Game {n}: {why}", n, file) for n, why in games.items()] if games else \
            [Problem(said, None, file)]
    return found


@dataclass
class Verdict:
    """What the server's own program said of what a server plays, its last time asked (plays_verdict): read from
    `dedicated check` of the server as it is (a draft beside its file: its block alone, master, UPnP, self-update and
    the Workshop off) and `dedicated playlist list` (every playlist file, played or not). `asked` False: it wasn't
    asked, or couldn't answer, and `unasked` says why."""

    refusals: list[Problem] = field(default_factory=list)  # its block's refusals (`Not ready:`), each with its file
    skipped: dict[str, str] = field(default_factory=dict)  # content files left out: path -> reason
    modes: list[tuple[str, str]] = field(default_factory=list)  # [modes] notes: (path, what it says)
    listed: dict[str, list[Problem]] = field(default_factory=dict)  # each playlist file's problems
    asked: bool = True
    unasked: str = ""
    when: float = 0.0
    version: str = ""  # the program's version, when known

    def to_json(self) -> dict[str, Any]:
        def problems(items: list[Problem]) -> list[list[Any]]:
            return [[p.text, p.game, p.file] for p in items]

        return {"refusals": problems(self.refusals), "skipped": self.skipped, "modes": [list(m) for m in self.modes],
                "listed": {file: problems(items) for file, items in self.listed.items()}, "asked": self.asked,
                "unasked": self.unasked, "when": self.when, "version": self.version}

    @classmethod
    def from_json(cls, data: Any) -> Verdict | None:
        if not isinstance(data, dict):
            return None

        def problems(items: Any) -> list[Problem]:
            return [Problem(str(p[0]), p[1] if isinstance(p[1], int) else None, p[2] if isinstance(p[2], str) else None)
                    for p in items if isinstance(p, list) and len(p) == 3] if isinstance(items, list) else []

        skipped = data.get("skipped") if isinstance(data.get("skipped"), dict) else {}
        listed = data.get("listed") if isinstance(data.get("listed"), dict) else {}
        modes = data.get("modes") if isinstance(data.get("modes"), list) else []
        return cls(problems(data.get("refusals")), {str(k): str(v) for k, v in skipped.items()},
                   [(str(m[0]), str(m[1])) for m in modes if isinstance(m, list) and len(m) == 2],
                   {str(k): problems(v) for k, v in listed.items()}, bool(data.get("asked", True)),
                   str(data.get("unasked") or ""), float(data.get("when") or 0.0), str(data.get("version") or ""))


def plays_verdict(tool: Any, server: ServerConfig, *, run_check: bool = True) -> Verdict:
    """The server's own program on what the server plays now, as its files are: `dedicated check` of a draft (its
    block alone beside its own file: nothing of the server's is changed, no Workshop item fetched), then `dedicated
    playlist list` with the same draft (every playlist file). Slow (seconds): never on the event loop. run_check False:
    only the list (a playlist naming `workshop:<item>/<map>` makes `check` fetch that item: plays-workshop-names)."""
    import time

    verdict = Verdict(when=time.time())
    try:
        data = read_toml(server.config_path)
        blocks = data.get("server", []) if isinstance(data.get("server"), list) else []
        own = next((b for b in blocks if str(b.get("name", "")).lower() == server.name.lower()), None)
        if own is None:
            return Verdict(asked=False, unasked=f"{server.config_path.name} has no [[server]] block called "
                                                 f"{server.name}", when=verdict.when)
        texts: list[str] = []
        if run_check:
            result = _run_check(tool, server, dict(own))
            texts.append(result.text)
            if NO_STEAM in result.text:
                verdict.asked, verdict.unasked = False, (
                    "its check wanted to fetch a Workshop item a playlist names as workshop:<item>/<map>")
            elif not result.ok:
                verdict.refusals = _refusals(result.text, server.name)
                if not verdict.refusals and getattr(result, "code", 0) in (124, 126, 127, 130):
                    verdict.asked, verdict.unasked = False, result.problem  # not run, or stopped: no verdict
                elif not verdict.refusals:
                    verdict.refusals = [Problem(result.problem)]
        listed = _run_list(tool, server, dict(own))
        texts.append(listed.text)
        if listed.ok:
            verdict.listed = listed_problems(listed.text)
        elif not run_check:
            verdict.asked, verdict.unasked = False, listed.problem
        text = "\n".join(texts)
        verdict.skipped = skipped_files(text)
        verdict.modes = mode_notes(text)
        return verdict
    except (OSError, ValueError, TomlEditError) as error:
        return Verdict(asked=False, unasked=str(error), when=verdict.when)


def _draft_config(server: ServerConfig, block: dict[str, Any]) -> Path:
    """A draft config beside the server's own (CHECK_FILE), holding `block` alone on a port of its own, without its
    console port (the running server holds it), the Workshop (a check mustn't fetch an item), self-update, UPnP or
    the master. The caller removes it."""
    check = server.folder / _check_name(CHECK_FILE.removesuffix(".toml"), ".toml")
    shutil.copy2(server.config_path, check)
    try:
        draft = {k: v for k, v in block.items() if k != "rcon_port"}
        replace_server_blocks(check, [{**draft, "port": _free_port()}])
        for key in ("items", "steamcmd", "mcc"):
            remove_table_key(check, "workshop", key)
        set_top_key(check, "auto_update", False)
        set_top_key(check, "upnp", False)
        set_table_key(check, "master", "enabled", False)
    except BaseException:
        check.unlink(missing_ok=True)
        raise
    return check


def _run_list(tool: Any, server: ServerConfig, block: dict[str, Any]) -> Any:
    """`dedicated playlist list` with the draft config: every playlist file in the server's playlists folder."""
    check = _draft_config(server, block)
    try:
        return tool.run_sync(server.folder, "playlist", "list", config=check.name, timeout=CHECK_SECONDS)
    finally:
        check.unlink(missing_ok=True)


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
    """When a change of what a server plays applies, as the program's guide has it (for saying what a host's change
    does). `before` and `after` are plays kinds (games/plays.py plays_kind): a new playlist from the next vote, a new
    map or game from the next game, a change of kind (and most campaign or ODST changes) once nobody is on, when the
    server restarts by itself."""
    before, after = ("map" if kind == "default" else "playlist" if kind == "playlists" else kind
                     for kind in (before, after))
    if after in ("campaign", "odst"):
        return "once nobody is on (the server restarts by itself for most campaign and ODST changes)"
    if before != after:
        return "once nobody is on (the server restarts by itself when the kind of game changes)"
    return "from the next vote" if after == "playlist" else "from the next game"
