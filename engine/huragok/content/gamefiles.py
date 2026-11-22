"""What a server's folder lacks: warned about, and put right only when the host asks (I, `start.cmd repair`).

A server folder copied in from another Huragok folder (or from another PC) brings its dedicated.toml, which points
at what it had there: `game = "../../game"`, `bans = "../../bans.json"`. Huragok reads what its dedicated.toml names
as soon as the folder is found and again before each start, and warns (core/serverwarnings.py, templates/
warnings.toml), changing nothing: since 0.7.0 a server's folder is the host's.

- A game folder without the files it needs for what it plays (Halo 3's, ODST's, the campaign's, and from Project
  Reclaimer 0.10 Reach's, CE's and Halo 2's, for a server whose map or playlist names their maps): the server waits
  ("needs Halo 3's files: press I, or run start.cmd repair") instead of failing its starts until it's FAULTED. A
  start that fails because the program says files are missing waits the same way.
- A content folder or a ban list named outside its folder and not on this PC; a ban list shared with other servers
  (a ban on one is a ban on all); a [workshop] steamcmd that isn't on this PC; Reach's files without its game and map
  variants (0.10.0's init left them out).

`repair` (start.cmd repair, and I → R in the window) puts them right when the host asks, each shown first: the game
folder pointed at one here that has the files, the files got (copied from MCC on this PC, or downloaded with
SteamCMD), a content folder or ban list of its own, this PC's SteamCMD in its [workshop] lines, Reach's variants from
MCC. A version of its dedicated.toml is kept before the first change (content/quickactions.py's history). The I key
gets a game's files into Huragok's own game/ when the host asks.

Nothing here deletes or overwrites anything that's there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import steam
from ..core.files import write_atomic
from ..games.bungie import GAMES
from ..games.files import (
    CAMPAIGN_MISSIONS, DEPOTS, GAME_OF_KIND, HAS, KINDS, LABELS, MAPS_FOR, SKIPPED, add_reach_variants, has, has_campaign,
    has_game_files, has_odst_files, has_reach_campaign, in_mcc, kinds_in_failure, lacking, missing, needed, needed_by,
    plays_on,
)
from ..steam import is_mcc_install
from .newserver import game_folders, link_tree, relative_posix

if TYPE_CHECKING:
    from ..core.serverfolders import ServerConfig
    from ..core.settings import Settings
    from ..program.dedicated import Dedicated
    from ..core.events import Event, EventBus
    from ..core.serverwarnings import Warnings

log = logging.getLogger(__name__)

EMPTY_BANS = {"players": [], "ips": []}
CONTENT_FOLDERS = ("Maps", "Game Modes", "Images", "Mods")
STILL_MISSING_SECONDS = 900.0  # a start that says files are missing this soon after they arrived: not fetched again


def gb(size: float) -> str:
    return f"{size / 1e9:.1f} GB"


# What a server's dedicated.toml names --------------------------------------------------------------------------

@dataclass
class ServerPaths:
    folder: Path
    config: Path
    game_raw: str
    game: Path
    content_raw: str
    content: Path
    bans_raw: str
    bans: Path
    steamcmd: str
    username: str
    blocks: list[dict[str, Any]] = field(default_factory=list)  # its [[server]] blocks

    def block(self, name: str) -> dict[str, Any]:
        """The [[server]] block of that name ({} when it isn't there)."""
        return next((b for b in self.blocks if str(b.get("name", "")).lower() == name.lower()), {})


def read_paths(folder: Path, config_name: str) -> ServerPaths | None:
    from ..core.tomledit import read_toml

    config = folder / config_name
    try:
        data = read_toml(config)
    except (OSError, ValueError):
        return None
    workshop = data.get("workshop") if isinstance(data.get("workshop"), dict) else {}

    def text(key: str, default: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) and value.strip() else default

    game, content, bans = text("game", "game"), text("content", "content"), text("bans", "bans.json")
    blocks = [b for b in data.get("server", []) if isinstance(b, dict)] if isinstance(data.get("server"), list) else []
    return ServerPaths(folder, config, game, (folder / game).resolve(), content, (folder / content).resolve(), bans,
                       (folder / bans).resolve(), str(workshop.get("steamcmd") or ""),
                       str(workshop.get("username") or ""), blocks)


UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PLAYER_ID = re.compile(r"^[0-9a-fA-F]{64}$")


def ban_list_problems(path: Path) -> list[str]:
    """What in a ban list the server program would refuse it for, as far as can be told without it: the program
    reads the whole file or none of it, and one it can't read stops every server using it from starting (the
    simulation, 26 October: an entry's `group` that wasn't a UUID). [] for a list that isn't there (the program
    makes an empty one) or one that reads."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f"{path} can't be read: {error}"]
    except json.JSONDecodeError as error:
        return [f"{path} isn't JSON (line {error.lineno}, column {error.colno}): {error.msg}"]
    if not isinstance(data, dict):
        return [f"{path} isn't a ban list (an object with players and ips)"]
    problems = []
    for kind in ("players", "ips", "devices"):
        rows = data.get(kind, [])
        if not isinstance(rows, list):
            problems.append(f"{path}: {kind} isn't a list")
            continue
        for number, entry in enumerate(rows, 1):
            where = f"{path}: {kind} entry {number}"
            if not isinstance(entry, dict):
                problems.append(f"{where} isn't an object")
                continue
            if kind == "players" and not PLAYER_ID.match(str(entry.get("id", ""))):
                problems.append(f"{where}: id {str(entry.get('id'))!r} isn't a player ID (64 hex digits)")
            if "group" in entry and not UUID.match(str(entry["group"])):
                problems.append(f"{where}: group {str(entry['group'])!r} isn't a UUID")
            for key in ("banned_at", "expires"):
                if key in entry and (isinstance(entry[key], bool) or not isinstance(entry[key], (int, float))):
                    problems.append(f"{where}: {key} isn't a number")
    return problems


def missing_kinds(paths: ServerPaths, plays_kind: str) -> list[str]:
    """What the server's game folder lacks for what it plays: halo3 first (the others go beside it)."""
    return missing(paths.game, plays_kind)


def server_version(settings: Settings, server: ServerConfig) -> str | None:
    """The release of the program the server runs (by its checksum when Huragok knows the file, else its
    `--version`, asked once per file: 0.10.1 says 0.10.0), or None."""
    from ..program.releases import file_version, program_of, readable

    try:
        program = program_of(settings, server)
        version = file_version(settings, program) if program is not None else None
    except OSError:
        return None
    return version if readable(version) else None


def server_kinds(settings: Settings, server: ServerConfig, paths: ServerPaths,
                 version: Callable[[], str | None] | None = None) -> list[str]:
    """Every kind the server needs for what its [[server]] block (and its playlist) plays: Halo 3's first, then
    each other game its maps are from, as far as the program it runs plays them. The program's version (`version`,
    else its `--version`: slow the first time, never on the loop) is asked only when another game than Halo 3 and
    ODST is named."""
    block = paths.block(server.name)
    kinds = needed_by(block, paths.folder) if block else needed(server.plays_kind)
    if any(GAME_OF_KIND[kind] not in ("halo3", "odst") for kind in kinds):
        known = version() if version is not None else server_version(settings, server)
        kinds = [kind for kind in kinds if plays_on(kind, known)]
    return kinds


def server_missing(settings: Settings, server: ServerConfig, paths: ServerPaths,
                   version: Callable[[], str | None] | None = None) -> list[str]:
    """What the server's game folder lacks for what it plays: halo3 first (the others go beside it)."""
    return lacking(paths.game, server_kinds(settings, server, paths, version))


# What a folder lacks: looked at, never changed ---------------------------------------------------------------------

def ban_list_missing(paths: ServerPaths) -> bool:
    """Its ban list is named outside its folder and isn't on this PC (one in its own folder that isn't there yet is
    fine: the program writes it with the first ban)."""
    return paths.bans.parent != paths.folder.resolve() and not paths.bans.exists()


def ban_list_shared(paths: ServerPaths) -> bool:
    """Its ban list is a file outside its folder: every server reading it shares its bans."""
    return paths.bans.parent != paths.folder.resolve() and paths.bans.is_file()


def content_missing(paths: ServerPaths) -> bool:
    """Its content folder isn't there, and neither is the folder it'd be in (one that only isn't there yet the program
    makes as it starts)."""
    return not paths.content.exists() and not paths.content.parent.is_dir()


def steamcmd_missing(paths: ServerPaths) -> bool:
    """Its [workshop] steamcmd names a program that isn't on this PC."""
    return bool(paths.steamcmd) and not Path(paths.steamcmd).is_file()


def reach_variants_missing(paths: ServerPaths) -> bool:
    """Reach's files without its game and map variants (0.10.0's `dedicated init --maps reach` leaves them out, and
    with them Reach's own game types), in a game folder Huragok may add to (never MCC's own)."""
    reach = paths.game / "haloreach"
    return (reach / "haloreach.dll").is_file() and not (reach / "hopper_game_variants").is_dir() \
        and not into_mcc(paths.game)


def game_path_candidate(settings: Settings, paths: ServerPaths, plays_kind: str,
                        kinds: list[str] | None = None) -> Path | None:
    """For a game folder that isn't there, or has no Halo 3 at all: a game folder here that has what it needs
    (`kinds`: every kind it needs, the one with the most of them first; Huragok's own game/ first on a tie). None for
    a folder with Halo 3 in it (whatever other game it lacks: their files go beside Halo 3's), or when there's none."""
    if (paths.game / "halo3" / "halo3.dll").is_file():
        return None
    candidates = [game for game in game_folders(settings) if game != paths.game and has_game_files(game)]
    if plays_kind == "odst":
        candidates = [game for game in candidates if has_odst_files(game)]
    if kinds:
        candidates.sort(key=lambda game: -sum(1 for kind in kinds if has(game, kind)))
    return candidates[0] if candidates else None


def findings(settings: Settings, server: ServerConfig, paths: ServerPaths,
             missing_kinds: list[str]) -> list[tuple[str, str, Path | str | None, dict[str, Any]]]:
    """What a server's folder lacks, as warnings (rule, key, the file it's about, fields): templates/warnings.toml's
    game-files-missing, content-folder-missing, ban-list-missing, ban-list-shared, steamcmd-missing and
    reach-variants-missing. Nothing is changed."""
    from ..core.settings import launcher

    run = launcher()
    found: list[tuple[str, str, Path | str | None, dict[str, Any]]] = []
    if missing_kinds:
        found.append(("game-files-missing", "", paths.config, {
            "game": str(paths.game), "what": _listed([LABELS[kind] for kind in missing_kinds]), "launcher": run}))
    if content_missing(paths):
        found.append(("content-folder-missing", "", paths.config, {"content": paths.content_raw, "launcher": run}))
    if ban_list_missing(paths):
        found.append(("ban-list-missing", "", paths.config, {"bans": paths.bans_raw, "launcher": run}))
    elif ban_list_shared(paths):
        found.append(("ban-list-shared", "", paths.config, {"bans": str(paths.bans), "launcher": run}))
    if steamcmd_missing(paths):
        found.append(("steamcmd-missing", "", paths.config, {"steamcmd": paths.steamcmd, "server": server.name,
                                                             "launcher": run}))
    if reach_variants_missing(paths):
        found.append(("reach-variants-missing", "", paths.config, {"game": str(paths.game), "launcher": run}))
    return found


def _listed(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


# Repairs, when the host asks: each shown first ----------------------------------------------------------------------

def _write_bans(path: Path) -> None:
    path.write_text(json.dumps(EMPTY_BANS, indent=2) + "\n", encoding="utf-8")


def fix_bans(settings: Settings, paths: ServerPaths) -> str | None:
    """A ban list named outside its folder that isn't on this PC: one of its own, empty, in its folder."""
    from ..core.tomledit import set_top_key

    if not ban_list_missing(paths):
        return None
    behind = ("if this server came from another Huragok folder or PC, its bans stayed there: copy that bans.json "
              "over this one to keep them (Huragok's own bans are put on it by themselves)")
    own = paths.folder / "bans.json"
    if not own.exists():
        _write_bans(own)
    set_top_key(paths.config, "bans", "bans.json")
    return f"its ban list ({paths.bans_raw}) wasn't there: it has its own now, {own}; {behind}"


def merge_ban_lists(own: dict[str, Any], shared: dict[str, Any]) -> dict[str, Any]:
    """Two ban list files as one: every entry of each list in either (the same entry once), the rest as `own` has
    it (or `shared`, for what `own` lacks)."""
    merged = dict(shared)
    merged.update(own)
    for key in set(own) | set(shared):
        mine, theirs = own.get(key), shared.get(key)
        if isinstance(mine, list) and isinstance(theirs, list):
            seen = {json.dumps(entry, sort_keys=True) for entry in mine}
            merged[key] = [*mine, *(entry for entry in theirs if json.dumps(entry, sort_keys=True) not in seen)]
    return merged


def own_bans(settings: Settings, paths: ServerPaths) -> str | None:
    """A server that shares a ban list gets one of its own, a copy of the shared one, when the host asks (a server
    already running keeps reading the list it started with until its next start)."""
    from ..core.tomledit import set_top_key

    if not ban_list_shared(paths):
        return None  # its own already; or none at all (fix_bans)
    own = paths.folder / "bans.json"
    shared_data = json.loads(paths.bans.read_text(encoding="utf-8") or "{}")
    if not isinstance(shared_data, dict):
        raise OSError(f"{paths.bans} isn't a ban list (the server program reads it as one: dedicated check)")
    if own.is_file():  # one left from before it shared: both lists' entries, nobody unbanned
        own_data = json.loads(own.read_text(encoding="utf-8") or "{}")
        data = merge_ban_lists(own_data if isinstance(own_data, dict) else {}, shared_data)
        backup = own.with_name("bans.json.before-own")
        if not backup.exists():
            shutil.copy2(own, backup)
    else:
        data = shared_data
    write_atomic(own, json.dumps(data, indent=2) + "\n")
    _record_shared(settings, paths.bans)
    set_top_key(paths.config, "bans", "bans.json")
    return (f"its ban list is its own now ({own}), a copy of the one it shared ({paths.bans}): every ban, admin and "
            "allowance on it stays. From its next start a /ban in game is this server's alone; Huragok's own bans "
            "are put on every server's")


def _record_shared(settings: Settings, shared: Path) -> None:
    """The shared list a server was given a copy of: its bans go on Huragok's list (players/commands/across.py)."""
    record = settings.records / "shared-ban-lists.json"
    try:
        known = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else []
    except (OSError, ValueError):
        known = []
    known = known if isinstance(known, list) else []
    if str(shared.resolve()) not in known:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps([*known, str(shared.resolve())], indent=2) + "\n", encoding="utf-8")


def fix_content(paths: ServerPaths) -> str | None:
    """A content folder named where there's no such folder on this PC: one of its own, with its four folders."""
    from ..core.tomledit import set_top_key

    if not content_missing(paths):
        return None
    target = paths.folder / "content"
    for sub in CONTENT_FOLDERS:
        (target / sub).mkdir(parents=True, exist_ok=True)
    set_top_key(paths.config, "content", "content")
    return (f"its content folder ({paths.content_raw}) names a folder that isn't on this PC: it has its own "
            "content/ now")


def fix_game_path(settings: Settings, paths: ServerPaths, plays_kind: str,
                  kinds: list[str] | None = None) -> str | None:
    """A game folder that isn't there, or has no Halo 3 at all: pointed at one here that has what it needs
    (game_path_candidate). A folder with Halo 3 in it is never swapped."""
    from ..core.tomledit import set_top_key

    game = game_path_candidate(settings, paths, plays_kind, kinds)
    if game is None:
        return None
    set_top_key(paths.config, "game", relative_posix(game, paths.folder))
    return f"its game folder ({paths.game_raw}) has no Halo 3 files here: it uses {game} now"


def fix_reach_variants(settings: Settings, paths: ServerPaths) -> str | None:
    """Reach's game and map variants added from MCC on this PC, when there is one. MCC's own folder is never
    written to."""
    if not reach_variants_missing(paths):
        return None
    from ..steam import find_mcc

    mcc = find_mcc(settings.setup.mcc_folder)
    if mcc is None or not (added := add_reach_variants(paths.game, mcc)):
        return None
    return f"Halo: Reach's game and map variants added to {paths.game} from MCC ({added} files)"


def fix_steamcmd(settings: Settings, paths: ServerPaths) -> str | None:
    """A [workshop] steamcmd that isn't on this PC: this PC's in its place (and the I key's account)."""
    from ..core.tomledit import set_table_key

    if not steamcmd_missing(paths):
        return None
    executable, _ = steam.find_steamcmd(settings.home, settings.setup.steamcmd)
    if executable is None:
        return None  # nothing to put in its place: I sets one up
    set_table_key(paths.config, "workshop", "steamcmd", str(executable), before="master")
    username = settings.setup.steam_username
    if username and username != paths.username:
        set_table_key(paths.config, "workshop", "username", username, before="master")
    return (f"its [workshop] steamcmd ({paths.steamcmd}) isn't on this PC: it uses {executable}"
            + (f" and the account {username}" if username and username != paths.username else "") + " now")


@dataclass
class Repair:
    """One thing `repair` would put right in a server's folder, as the host reads it before saying yes."""

    text: str
    run: Callable[[], str | None] | None  # does it (slow ones copy or download gigabytes); None: it can't be done here
    writes: bool = False  # it changes the server's dedicated.toml: a version is kept first
    slow: bool = False


def repairs(settings: Settings, server: ServerConfig, tool: Callable[[], Dedicated],
            say: Callable[[str], None] = print, version: Callable[[], str | None] | None = None) -> list[Repair]:
    """Everything `repair` would put right in the server's folder, in the order it does them; nothing is changed
    here. Reads Steam's records for MCC (slow-ish): run it in a thread."""
    paths = read_paths(server.folder, settings.config_name)
    if paths is None:
        return []
    plan: list[Repair] = []
    if ban_list_missing(paths):
        plan.append(Repair(f"its ban list ({paths.bans_raw}) isn't on this PC: one of its own, empty, in its folder "
                           '(bans = "bans.json")', lambda: fix_bans(settings, paths), writes=True))
    elif ban_list_shared(paths):
        plan.append(Repair(f"it shares its ban list ({paths.bans}) with any server reading it: a copy of its own in "
                           'its folder, every ban, admin and allowance kept (bans = "bans.json")',
                           lambda: own_bans(settings, paths), writes=True))
    if content_missing(paths):
        plan.append(Repair(f"its content folder ({paths.content_raw}) isn't on this PC: a content folder of its own, "
                           'empty (content = "content")', lambda: fix_content(paths), writes=True))
    if steamcmd_missing(paths):
        executable, _ = steam.find_steamcmd(settings.home, settings.setup.steamcmd)
        plan.append(Repair(f"its [workshop] steamcmd ({paths.steamcmd}) isn't on this PC: this PC's, {executable}",
                           lambda: fix_steamcmd(settings, paths), writes=True) if executable is not None else
                    Repair(f"its [workshop] steamcmd ({paths.steamcmd}) isn't on this PC, and SteamCMD isn't set up "
                           "here: I → 1 sets it up", None))
    kinds = server_kinds(settings, server, paths, version)
    game = paths.game
    candidate = game_path_candidate(settings, paths, server.plays_kind, kinds)
    if candidate is not None:
        plan.append(Repair(f"its game folder ({paths.game_raw}) has no Halo 3 files: it uses {candidate}, which "
                           f'has them (game = "{relative_posix(candidate, paths.folder)}")',
                           lambda: fix_game_path(settings, paths, server.plays_kind, kinds), writes=True))
        game = candidate
    for kind in lacking(game, kinds):
        target, why = target_for(settings, replace(paths, game=game), kind)
        if target is None:
            plan.append(Repair(f"{LABELS[kind]}: {why}", None))
            continue
        found, no_download = sources(settings, kind)
        if not found:
            plan.append(Repair(f"{LABELS[kind]} can't be got here: {no_way(kind, no_download)}", None))
            continue
        source = found[0]
        point = kind == "halo3" and target.resolve() != game.resolve()  # Huragok's own game/: the server is pointed at it

        def get(kind: str = kind, target: Path = target, source: Source = source, point: bool = point) -> str:
            said = fetch(settings, kind, source, target, tool, say)
            pointed = fix_game_path(settings, paths, server.plays_kind, kinds) if point else None
            return said + (f"; {pointed}" if pointed else "")

        plan.append(Repair(f"{LABELS[kind]} into {target}: {source.text(kind)}"
                           + (f'; then the server uses that folder (game = "{relative_posix(target, paths.folder)}")'
                              if point else ""), get, writes=point, slow=True))
    if reach_variants_missing(paths):
        plan.append(Repair(f"Halo: Reach's game and map variants into {paths.game}, from MCC on this PC",
                           lambda: fix_reach_variants(settings, paths)))
    return plan


def run_repairs(settings: Settings, server: ServerConfig, plan: list[Repair],
                say: Callable[[str], None] = print) -> tuple[list[str], list[str]]:
    """The repairs the host said yes to, in order: (what was done, what couldn't be). A version of the server's
    dedicated.toml is kept before the first change to it, and after the last (content/quickactions.py's history):
    when the first can't be kept, nothing that changes the file is done. Slow: run it in a thread."""
    from ..core import versions
    from .quickactions import HISTORY, owner_of

    done: list[str] = []
    failed: list[str] = []
    kept: bool | None = None  # None: not yet; False: couldn't be (no changes to the file then)
    owner = owner_of(server.config_path)
    for repair in plan:
        if repair.run is None:
            continue
        if repair.writes and kept is None:
            try:
                versions.keep_current(settings.records, HISTORY, owner, server.config_path)
                kept = True
            except OSError as error:
                kept = False
                failed.append(f"its {server.config_path.name} couldn't be kept in its history first ({error}), so "
                              "nothing in it was changed")
        if repair.writes and not kept:
            continue
        try:
            said = repair.run()
        except Exception as error:  # noqa: BLE001 - OSError, SteamSetupError, the program's own: the next one runs
            failed.append(f"{repair.text}: {error or type(error).__name__}")
            say(f"not done: {repair.text}: {error}")
            continue
        if said:
            done.append(said)
            say(said)
    if kept:
        try:
            versions.keep(settings.records, HISTORY, owner, server.config_path, "repaired, as the host asked")
        except OSError as error:
            log.warning("[%s] repaired, but its %s couldn't be kept in its history: %s", server.name,
                        server.config_path.name, error)
    return done, failed


# Getting game files ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """Where a kind of game files comes from: MCC on this PC, or SteamCMD."""

    how: str  # "mcc" or "steamcmd"
    path: Path  # MCC's folder, or SteamCMD
    username: str = ""

    def text(self, kind: str) -> str:
        if self.how == "mcc":
            return f"copying them from MCC at {self.path} (hard links on the same drive: no space used)"
        depot = DEPOTS[kind]
        return f"downloading them with SteamCMD ({gb(depot.download)}; {gb(depot.kept)} kept)"


def mcc_source(settings: Settings, kind: str) -> Path | None:
    """MCC on this PC, when it has `kind` installed (and Halo 3: `dedicated init` copies it with every game)."""
    from ..steam import find_mcc

    mcc = find_mcc(settings.setup.mcc_folder)
    return mcc if in_mcc(mcc, kind) else None


def mcc_only(kind: str) -> str:
    """Why a kind with no known SteamCMD depot can't be got here: Reach's, CE's and Halo 2's come from MCC."""
    game = GAMES[GAME_OF_KIND[kind]]
    return (f"{LABELS[kind]} come from your own MCC installation for now (no SteamCMD download yet): install "
            f"{game.title} in MCC on this PC (Steam, MCC, Properties, DLC), or name MCC's folder as [setup] "
            "mcc_folder in settings.toml")


def no_way(kind: str, why: str) -> str:
    """Why there's no way to get `kind` here, from sources()' why."""
    return f"MCC isn't on this PC and {why}" if kind in DEPOTS else why


def sources(settings: Settings, kind: str, *, downloads: bool = True) -> tuple[list[Source], str]:
    """Every way to get `kind` here, best first, and why there's no SteamCMD way when there isn't (for a kind
    SteamCMD can't get: why MCC is the only way). Only ever when the host asks (I, `repair`): `downloads` False leaves
    SteamCMD out."""
    found: list[Source] = []
    mcc = mcc_source(settings, kind)
    if mcc is not None:
        found.append(Source("mcc", mcc))
    if kind not in DEPOTS:
        return found, mcc_only(kind)
    executable, _ = steam.find_steamcmd(settings.home, settings.setup.steamcmd)
    username = settings.setup.steam_username
    if not downloads:
        why = "downloads are left out"
    elif executable is None:
        why = "SteamCMD isn't set up"
    elif not username:
        why = "no Steam account is set for SteamCMD"
    elif username.lower() not in [a.lower() for a in steam.cached_accounts(executable)]:
        why = f"{username} isn't signed in to SteamCMD here"
    else:
        found.append(Source("steamcmd", executable, username))
        why = ""
    return found, why


def gather(settings: Settings, kind: str, source: Source, stage: Path, tool: Callable[[], Dedicated],
           say: Callable[[str], None], *, room_in: Path | None = None) -> Path:
    """`kind`'s files in a scratch folder (stage/game, which is returned): copied from MCC with `dedicated init
    --from`, or downloaded with SteamCMD and stripped of sound and video. Checked complete; slow: run it in a
    thread. `room_in`: the folder they go to next, whose drive must have room. Raises OSError or SteamSetupError."""
    if stage.exists():
        shutil.rmtree(stage)
    if source.how == "mcc":
        program = tool()
        if GAME_OF_KIND[kind] not in ("halo3", "odst"):  # `--maps reach` is 0.10's: an older one takes it for a map
            version = program_version_of(program)
            if not plays_on(kind, version):
                raise OSError(f"the server program here is {version}: {LABELS[kind]} need Project Reclaimer "
                              f"{GAMES[GAME_OF_KIND[kind]].since} or later (U updates it)")
        say(f"{LABELS[kind]}: {source.text(kind)}")
        result = program.init(stage, mcc=str(source.path), maps=MAPS_FOR[kind])
        if not result.ok:
            raise OSError(f"dedicated init couldn't copy them: {result.problem}")
        if GAME_OF_KIND[kind] == "reach":
            add_reach_variants(stage / "game", source.path)
    elif kind not in DEPOTS:
        raise OSError(mcc_only(kind))
    else:
        depot = DEPOTS[kind]
        need = depot.download + 0.5e9
        free = shutil.disk_usage(source.path.parent).free
        if free < need:
            raise OSError(f"SteamCMD's drive has {gb(free)} free; the download needs about {gb(need)}")
        room = room_in or stage.parent
        while not room.exists() and room.parent != room:
            room = room.parent
        free = shutil.disk_usage(room).free
        if free < depot.kept:
            raise OSError(f"{room}'s drive has {gb(free)} free; the files need about {gb(depot.kept)}")
        say(f"{LABELS[kind]}: {source.text(kind)}; SteamCMD shows no progress, so nothing more is said until it ends")
        downloaded = steam.download_depot(source.path, source.username, depot.depot)
        kept = keep_server_files(downloaded, stage / "game")
        say(f"{LABELS[kind]}: {kept} files kept (sound and video left out)")
        shutil.rmtree(downloaded, ignore_errors=True)
    files = stage / "game"
    if not HAS[kind](files):
        raise OSError(f"{LABELS[kind]} didn't arrive complete")
    return files


def program_version_of(program: Dedicated) -> str | None:
    """A runner's program version (`--version`, asked once per file), or None when it doesn't say."""
    from ..program.dedicated import cached_version
    from ..program.releases import readable

    version = cached_version(program.executable, program.template)
    return version if readable(version) else None


def fetch(settings: Settings, kind: str, source: Source, target: Path, tool: Callable[[], Dedicated],
          say: Callable[[str], None]) -> str:
    """`kind`'s files into the game folder `target`, beside what's there (which stays as it is: a running server
    keeps its files). Slow: run it in a thread. Returns what was done; raises OSError or SteamSetupError."""
    stage = settings.records / f"game-files-{kind}"
    try:
        files = gather(settings, kind, source, stage, tool, say, room_in=target)
        target.mkdir(parents=True, exist_ok=True)
        linked, copied = link_tree(files, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return f"{LABELS[kind]} are in {target} ({linked} hard-linked, {copied} copied)"


MULTIPLAYER_SKIPPED = {"shared", "campaign", "mainmenu", *CAMPAIGN_MISSIONS}


def multiplayer_maps(game: Path) -> int:
    try:
        return sum(1 for path in (game / "halo3" / "maps").glob("*.map") if path.stem.lower() not in MULTIPLAYER_SKIPPED)
    except OSError:
        return 0


def into_mcc(folder: Path) -> bool:
    """Whether files put in `folder` would land in MCC's own install: the folder is it, is inside it, or is a link
    (a junction) to either. Huragok reads MCC's files; it never writes there (Steam owns that folder)."""
    try:
        resolved = folder.resolve()
    except OSError:
        return False
    return any(is_mcc_install(path) for path in [resolved, *list(resolved.parents)[:4]])


MCC_REFUSED = ("{folder} is MCC's own folder (or a link to it), and Huragok never writes into MCC: give the server a "
               "game/ of its own (I gets Halo 3's files into it), then {what} can go beside them")


def shared_target(settings: Settings, kind: str) -> tuple[Path | None, str]:
    """Where I puts `kind`: this folder's shared game/ (Halo 3 first: every other game goes beside it)."""
    game = settings.home / "game"
    if into_mcc(game):
        return None, MCC_REFUSED.format(folder=game, what=LABELS[kind])
    if kind != "halo3" and not has_game_files(game):
        return None, f"{game} has no Halo 3 files yet: get those first ({LABELS[kind]} go beside them)"
    return game, ""


def root_version(settings: Settings) -> str | None:
    """The version of the program this folder's commands run (the newest copy here), or None when it isn't known."""
    from ..program.dedicated import DedicatedError, tool_for

    try:
        return program_version_of(tool_for(settings))
    except (DedicatedError, OSError):
        return None


def games_text(game: Path, version: str | None = None) -> str:
    """One game folder's games as I says them: "Halo 3, 27 multiplayer maps · campaign · ODST · Reach · no CE ·
    no Halo 2". Reach's, CE's and Halo 2's only for a program that plays them (`version`; None: unknown, it does)."""
    maps = game / "halo3" / "maps"
    forge = (maps / "campaign.map").is_file() and (maps / "040_voi.map").is_file()
    bits = [f"Halo 3, {multiplayer_maps(game)} multiplayer maps",
            "every campaign mission" if has_campaign(game) else
            "Forge's campaign files (no missions)" if forge else "no campaign files",
            "ODST" if has_odst_files(game) else "no ODST"]
    for key in ("reach", "halo1", "halo2"):
        if not plays_on(key, version):
            continue
        short = GAMES[key].short
        if not has(game, key):
            bits.append(f"no {short}")
        else:
            bits.append(f"{short} with its campaign" if key == "reach" and has_reach_campaign(game) else short)
    return " · ".join(bits)


def game_status(settings: Settings, version: str | None = None) -> list[tuple[str, str, str]]:
    """What I shows: (what, how it stands, colour) for this folder's shared game/ and every server's game folder.
    `version`: the program's (None: the newest copy here is asked, once per file). Slow-ish: run it in a thread."""
    from .upkeep import servers_using

    version = version or root_version(settings)
    rows: list[tuple[str, str, str]] = []
    folders: dict[Path, list[str]] = {}
    lacking: list[str] = []
    for server in settings.servers:
        paths = read_paths(server.folder, settings.config_name)
        if paths is None:
            continue
        folders.setdefault(paths.game, []).append(server.name)
        if missing := server_missing(settings, server, paths):
            lacking.append(f"{server.name} ({', '.join(LABELS[k] for k in missing)})")
    shared = (settings.home / "game").resolve()
    for game in [shared, *[g for g in folders if g != shared]]:
        if game != shared and game not in folders:
            continue
        users = folders.get(game) or servers_using(settings, game)
        if not has_game_files(game):
            state, colour = "no Halo 3 files" + ("" if game.exists() else " (not there)"), "yellow" if users else "gray"
        else:
            state, colour = games_text(game, version), "green"
        used = f" · used by {len(users)} server(s)" if users else " · no server uses it"
        label = "shared game/" if game == shared else "game folder"
        rows.append((label, f"{game}: {state}{used}", colour))
    if lacking:
        rows.append(("missing", "; ".join(lacking), "yellow"))
    return rows


def keep_server_files(depot: Path, game: Path) -> int:
    """A downloaded depot's files a server uses, moved into `game` (halo3/…, halo3odst/…); sound and video aren't."""
    kept = 0
    for path in sorted(depot.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(depot)
        if any(part.lower() in SKIPPED for part in relative.parts[:-1]):
            continue
        destination = game / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
        kept += 1
    return kept


def target_for(settings: Settings, paths: ServerPaths, kind: str) -> tuple[Path | None, str]:
    """Where `kind` goes: Halo 3 into this folder's shared game/ (the server is pointed at it once it's there);
    ODST and the campaign beside the Halo 3 the server already has. Never into MCC's own folder: (None, why)."""
    shared = kind == "halo3" or not has_game_files(paths.game) or into_mcc(paths.game)
    target = settings.home / "game" if shared else paths.game
    if into_mcc(target):
        return None, MCC_REFUSED.format(folder=target, what=LABELS[kind])
    return target, ""


class GameFiles:
    """What a server's folder lacks, for the Superintendent: warned about whenever a server is found and before each
    start (never put right by itself, 0.7.0), and a server lacking its game files held until they're there. A game's
    files are got only when the host asks (I), in the background. Looking for MCC reads Steam's records and the
    getting is gigabytes: both run in threads, never on the loop."""

    def __init__(self, settings: Settings, bus: EventBus, tool: Callable[[], Dedicated],
                 release: Callable[[], None] | None = None, warnings: Warnings | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.tool = tool
        self.release = release  # tells the waiting servers to look again (Fleet.release)
        self.warnings = warnings  # the catalogue (core/serverwarnings.py) the findings go into
        self.getting: dict[str, str] = {}  # kind -> what's happening ("looking for…", "copying…", "downloading…")
        self.failed: dict[str, tuple[float, str]] = {}  # kind -> (when, why) the host's last ask for them failed
        self.fetched: dict[str, float] = {}  # kind -> when its files last arrived
        self.forced: dict[str, set[str]] = {}  # server -> kinds a failed start said are missing
        self.tasks: set[asyncio.Task[None]] = set()
        self.versions: dict[tuple[str, int, int], str | None] = {}  # a program file (as it is) -> its version
        bus.subscribe("server.start_failed", self._on_start_failed)

    def _version(self, server: ServerConfig) -> str | None:
        """The version of the program the server runs, once it's known; None (plays everything) until then: the
        first `--version` runs in a thread, never on the loop, and a new program file is asked again."""
        from ..program.releases import program_of

        program = program_of(self.settings, server)
        try:
            info = program.stat() if program is not None else None
        except OSError:
            info = None
        if program is None or info is None:
            return None
        key = (str(program), info.st_mtime_ns, info.st_size)
        if key not in self.versions:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # no loop (the command line): asked here
                self.versions[key] = server_version(self.settings, server)
                return self.versions[key]
            self.versions[key] = None

            async def look() -> None:
                self.versions[key] = await asyncio.to_thread(server_version, self.settings, server)
                if self.release is not None:
                    self.release()  # the servers waiting look again with it

            task = loop.create_task(look())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return self.versions[key]

    @property
    def here(self) -> bool:
        """Only the process backend runs the program on this PC with its folder's paths. In a container (docker)
        or wherever a command runs it, dedicated.toml's paths aren't this PC's: nothing is looked at from here."""
        return self.settings.backend == "process"

    def server_added(self, server: ServerConfig, *, running: bool) -> None:
        self.look(server)

    def server_removed(self, name: str) -> None:
        self.forced.pop(name, None)

    def look(self, server: ServerConfig) -> list[str]:
        """What its folder lacks, into the warnings (nothing is changed); for a server just found and before each
        start. The rules found."""
        if not self.here:
            return []
        paths, kinds = self.needed(server)
        return self._warn(server, paths, kinds)

    def _warn(self, server: ServerConfig, paths: ServerPaths | None, kinds: list[str]) -> list[str]:
        found = findings(self.settings, server, paths, kinds) if paths is not None else []
        if self.warnings is not None:
            self.warnings.sync(server.name, "files", found)
        return [rule for rule, *_ in found]

    def needed(self, server: ServerConfig) -> tuple[ServerPaths | None, list[str]]:
        paths = read_paths(server.folder, self.settings.config_name)
        if paths is None:
            return None, []
        kinds = server_missing(self.settings, server, paths, lambda: self._version(server))
        for kind in sorted(self.forced.get(server.name, set()), key=KINDS.index):
            if kind not in kinds:
                kinds.append(kind)
        return paths, kinds

    def prelaunch(self, server: ServerConfig) -> str | None:
        """Why the server can't start yet (it waits), or None. Never gets anything by itself: the host does (I, or
        `repair`), and the warning says so."""
        if not self.here:
            return None
        paths, kinds = self.needed(server)
        self._warn(server, paths, kinds)
        if paths is None or not kinds:
            self.forced.pop(server.name, None)
            return None
        for kind in kinds:
            if kind in self.getting:
                return f"getting {LABELS[kind]}: {self.getting[kind]}"
        from ..core.settings import launcher

        failed = next((self.failed[kind][1] for kind in kinds if kind in self.failed), None)
        return (f"needs {_listed([LABELS[kind] for kind in kinds])}: press I, or run {launcher()} repair"
                + (f" (the last try: {failed})" if failed else ""))

    def get(self, kind: str, target: Path, source: Source | None = None) -> bool:
        """Get `kind` into `target` in the background (from `source`, else the best way there is). False: it's
        being got already."""
        if kind in self.getting:
            return False
        self.getting[kind] = source.text(kind) if source else "looking for where to get them (MCC, SteamCMD)"
        self.failed.pop(kind, None)
        task = asyncio.ensure_future(self._get(kind, target, source))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def _get(self, kind: str, target: Path, source: Source | None) -> None:
        try:
            if source is None:
                found, why = await asyncio.to_thread(sources, self.settings, kind)
                if not found:
                    self.failed[kind] = (time.monotonic(), no_way(kind, why))
                    log.error("%s are missing and there's no way to get them here: %s. The servers that need them "
                              "wait; press I (%s)", LABELS[kind], no_way(kind, why),
                              "copy them from MCC on a PC that has it, or set up SteamCMD to download them"
                              if kind in DEPOTS else "it copies them once MCC has them")
                    return
                source = found[0]
            self.getting[kind] = source.text(kind)
            log.warning("%s: %s into %s; the servers that need them wait", LABELS[kind], self.getting[kind], target)
            said = await asyncio.to_thread(fetch, self.settings, kind, source, target, self.tool,
                                           lambda line: log.info("%s", line))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - OSError, SteamSetupError, the program's own: the servers wait
            self.failed[kind] = (time.monotonic(), f"getting them failed ({error or type(error).__name__})")
            log.error("%s couldn't be got: %s. The servers that need them wait; press I to try again", LABELS[kind],
                      error)
        else:
            self.fetched[kind] = time.monotonic()
            for kinds in self.forced.values():
                kinds.discard(kind)
            log.warning("%s; the servers waiting for them start", said)
            self.bus.emit("supervisor.game_files", None, kind=kind, text=said)
        finally:
            self.getting.pop(kind, None)
            if self.release is not None:
                self.release()

    def try_again(self) -> None:
        """I changed something (SteamCMD signed in, files got by hand): what failed is looked at again now."""
        self.failed.clear()
        if self.release is not None:
            self.release()

    def _on_start_failed(self, event: Event) -> None:
        kinds = kinds_in_failure([str(line) for line in event.get("lines") or []])
        if not kinds or not event.server:
            return
        for kind in kinds:
            if time.monotonic() - self.fetched.get(kind, float("-inf")) < STILL_MISSING_SECONDS:
                self.failed[kind] = (time.monotonic(), "they arrived, but the server still says files are missing "
                                                       "(its console, A, says which)")
        self.forced.setdefault(event.server, set()).update(kinds)
        log.warning("[%s] it stopped for missing game files (%s): it waits until they're there (I, or repair)",
                    event.server, ", ".join(LABELS[k] for k in kinds))

    def summary(self) -> list[str]:
        lines = [f"getting {LABELS[k]}: {how}" for k, how in self.getting.items()]
        lines += [f"{LABELS[k]}: {why}" for k, (_, why) in self.failed.items()]
        return lines
