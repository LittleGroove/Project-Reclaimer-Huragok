"""What a server arrives without, put right by Huragok itself.

A server folder copied in from another Huragok folder (or from another PC) brings its dedicated.toml, which points
at what it shared there: `game = "../../game"`, `bans = "../../bans.json"`. When those aren't here the server
program refuses to start (no game files), or starts with an empty ban list and says nothing. So as soon as a
server's folder is found, and again before each start, Huragok reads what its dedicated.toml names and:

- a ban list that isn't there is made, empty, where the file says; when even its folder isn't on this PC the server
  is pointed at this folder's shared bans.json. Either way the host is told its old bans may have stayed behind;
- a content folder that isn't there is made, with its four folders;
- a game folder without Halo 3's files: the server is pointed at a game folder here that has them (this folder's
  shared game/ first); with none, game/ is filled: copied from MCC when it's on this PC, otherwise downloaded with
  SteamCMD ([setup] download_game_files). With neither, the server waits ("needs Halo 3's files: press I")
  instead of failing its starts until it's FAULTED. ODST's files and the campaign missions, for a server that plays
  them, the same way;
- a [workshop] steamcmd that isn't on this PC: this folder's SteamCMD (the one I set up) in its place;
- a start that fails because the program says files are missing (a map the copy left out) gets them too.

Nothing here deletes or overwrites anything that's there.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import steamsetup
from .newserver import game_folders, has_game_files, has_odst_files, is_mcc_install, link_tree, relative_posix

if TYPE_CHECKING:
    from .config import ServerConfig, Settings
    from .dedicated import Dedicated
    from .events import Event, EventBus

log = logging.getLogger(__name__)

EMPTY_BANS = {"players": [], "ips": []}
CONTENT_FOLDERS = ("Maps", "Game Modes", "Images", "Mods")
MISSING_FILES = "is missing files copied from your own MCC installation"
CAMPAIGN_MISSIONS = ("005_intro", "010_jungle", "020_base", "030_outskirts", "040_voi", "050_floodvoi", "070_waste",
                     "100_citadel", "110_hc", "120_halo", "130_epilogue")
LABELS = {"halo3": "Halo 3's files", "campaign": "Halo 3's campaign missions", "odst": "Halo 3: ODST's files"}
MAPS_FOR = {"halo3": None, "campaign": ["campaign"], "odst": ["odst"]}  # `dedicated init --from --maps …`
RETRY_SECONDS = 3600.0  # a fetch that failed isn't tried again by itself for this long (I tries at once)
STILL_MISSING_SECONDS = 900.0  # a start that says files are missing this soon after they arrived: not fetched again


@dataclass(frozen=True)
class Depot:
    """A piece of MCC on Steam that a server needs files from (MCC's app is 976730)."""

    kind: str
    depot: int
    download: float  # bytes SteamCMD downloads
    kept: float  # bytes kept: the rest is sound and video, which servers never play
    holds: str


DEPOTS = {
    "halo3": Depot("halo3", 976739, 9.33e9, 5.75e9, "Halo 3 multiplayer: halo3.dll, shared.map and the 27 maps"),
    "campaign": Depot("campaign", 976738, 9.01e9, 5.42e9, "Halo 3's campaign: campaign.map and the 11 missions"),
    "odst": Depot("odst", 1064222, 8.73e9, 3.49e9, "Halo 3: ODST: halo3odst.dll and its maps"),
}
SKIPPED = {"fmod", "waves", "bink"}  # sound and video: a server plays neither


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


def read_paths(folder: Path, config_name: str) -> ServerPaths | None:
    from .tomledit import read_toml

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
    return ServerPaths(folder, config, game, (folder / game).resolve(), content, (folder / content).resolve(), bans,
                       (folder / bans).resolve(), str(workshop.get("steamcmd") or ""),
                       str(workshop.get("username") or ""))


def has_campaign(game: Path) -> bool:
    maps = game / "halo3" / "maps"
    return all((maps / f"{name}.map").is_file() for name in ("campaign", "010_jungle", "040_voi"))


def missing_kinds(paths: ServerPaths, plays_kind: str) -> list[str]:
    """What the server's game folder lacks for what it plays: halo3 first (the others go beside it)."""
    if not has_game_files(paths.game):
        return ["halo3"]
    kinds = []
    if plays_kind == "odst" and not has_odst_files(paths.game):
        kinds.append("odst")
    if plays_kind == "campaign" and not has_campaign(paths.game):
        kinds.append("campaign")
    return kinds


def kinds_in_failure(lines: list[str]) -> list[str]:
    """What a start that failed for missing game files says is missing (the program lists them under its error)."""
    text = "\n".join(lines)
    if MISSING_FILES not in text:
        return []
    listed = text.split(MISSING_FILES, 1)[1]
    kinds = []
    if re.search(r"halo3/(halo3\.dll|maps/shared\.map)|multiplayer map", listed):
        kinds.append("halo3")
    if "halo3odst/" in listed:
        kinds.append("odst")
    if any(f"/{name}.map" in listed for name in (*CAMPAIGN_MISSIONS, "campaign")):
        kinds.append("campaign")
    return kinds or ["halo3"]


# Quick repairs: no copying, no downloading ----------------------------------------------------------------------

def _write_bans(path: Path) -> None:
    path.write_text(json.dumps(EMPTY_BANS, indent=2) + "\n", encoding="utf-8")


def fix_bans(settings: Settings, paths: ServerPaths) -> str | None:
    from .tomledit import set_top_key

    if paths.bans.exists():
        return None
    behind = ("if this server came from another Huragok folder or PC, its bans stayed there: copy that bans.json "
              "over this one to keep them")
    if paths.bans.parent.is_dir():
        _write_bans(paths.bans)
        return f"its ban list ({paths.bans_raw}) wasn't there: made an empty one at {paths.bans}; {behind}"
    shared = settings.home / "bans.json"
    if not shared.exists():
        _write_bans(shared)
    set_top_key(paths.config, "bans", relative_posix(shared, paths.folder))
    return (f"its ban list ({paths.bans_raw}) names a folder that isn't on this PC: it uses this folder's shared "
            f"{shared} now; {behind}")


def fix_content(paths: ServerPaths) -> str | None:
    from .tomledit import set_top_key

    if paths.content.is_dir():
        return None
    if paths.content.exists():
        return None  # a file of that name: the host's to sort out (`dedicated check` says so)
    target, said = paths.content, f"made its content folder {paths.content} (it wasn't there)"
    if not paths.content.parent.is_dir():
        target = paths.folder / "content"
        set_top_key(paths.config, "content", "content")
        said = (f"its content folder ({paths.content_raw}) names a folder that isn't on this PC: it has its own "
                f"content/ now")
    for sub in CONTENT_FOLDERS:
        (target / sub).mkdir(parents=True, exist_ok=True)
    return said


def fix_game_path(settings: Settings, paths: ServerPaths, plays_kind: str) -> str | None:
    """A game folder that isn't there, or has no Halo 3 at all: pointed at one here that has what it needs. A
    folder with Halo 3 in it is never swapped (missing ODST or campaign files are added to it instead)."""
    from .tomledit import set_top_key

    if (paths.game / "halo3" / "halo3.dll").is_file():
        return None
    for game in game_folders(settings):
        if game == paths.game or not has_game_files(game):
            continue
        if plays_kind == "odst" and not has_odst_files(game):
            continue
        set_top_key(paths.config, "game", relative_posix(game, paths.folder))
        return f"its game folder ({paths.game_raw}) has no Halo 3 files here: it uses {game} now"
    return None


def fix_steamcmd(settings: Settings, paths: ServerPaths) -> str | None:
    from .tomledit import set_table_key

    if not paths.steamcmd or Path(paths.steamcmd).is_file():
        return None
    executable, _ = steamsetup.find_steamcmd(settings.home, settings.setup.steamcmd)
    if executable is None:
        return None  # nothing to put in its place: its Workshop updates wait, flagged, until I sets one up
    set_table_key(paths.config, "workshop", "steamcmd", str(executable), before="master")
    username = settings.setup.steam_username
    if username and username != paths.username:
        set_table_key(paths.config, "workshop", "username", username, before="master")
    return (f"its [workshop] steamcmd ({paths.steamcmd}) isn't on this PC: it uses {executable}"
            + (f" and the account {username}" if username and username != paths.username else "") + " now")


def quick_repairs(settings: Settings, server: ServerConfig) -> list[str]:
    """What was put right (each as the host reads it); nothing when nothing needed it."""
    paths = read_paths(server.folder, settings.config_name)
    if paths is None:
        return []
    done = []
    for fix in (lambda: fix_bans(settings, paths), lambda: fix_content(paths),
                lambda: fix_game_path(settings, paths, server.plays_kind), lambda: fix_steamcmd(settings, paths)):
        try:
            said = fix()
        except OSError as error:  # TomlEditError too: the file is as it was
            log.warning("[%s] couldn't put this right by itself: %s", server.name, error)
            continue
        if said:
            done.append(said)
    return done


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
    from .contentimport import find_mcc

    mcc = find_mcc(settings.setup.mcc_folder)
    if mcc is None:
        return None
    needed = {"halo3": mcc / "halo3" / "halo3.dll", "campaign": mcc / "halo3" / "maps" / "010_jungle.map",
              "odst": mcc / "halo3odst" / "halo3odst.dll"}[kind]
    return mcc if needed.is_file() else None


def sources(settings: Settings, kind: str, *, downloads: bool | None = None) -> tuple[list[Source], str]:
    """Every way to get `kind` here, best first, and why there's no SteamCMD way when there isn't."""
    found: list[Source] = []
    mcc = mcc_source(settings, kind)
    if mcc is not None:
        found.append(Source("mcc", mcc))
    allowed = settings.setup.download_game_files if downloads is None else downloads
    executable, _ = steamsetup.find_steamcmd(settings.home, settings.setup.steamcmd)
    username = settings.setup.steam_username
    if not allowed:
        why = "downloads are off ([setup] download_game_files = false)"
    elif executable is None:
        why = "SteamCMD isn't set up"
    elif not username:
        why = "no Steam account is set for SteamCMD"
    elif username.lower() not in [a.lower() for a in steamsetup.cached_accounts(executable)]:
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
        say(f"{LABELS[kind]}: {source.text(kind)}")
        result = tool().init(stage, mcc=str(source.path), maps=MAPS_FOR[kind])
        if not result.ok:
            raise OSError(f"dedicated init couldn't copy them: {result.problem}")
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
        downloaded = steamsetup.download_depot(source.path, source.username, depot.depot)
        kept = keep_server_files(downloaded, stage / "game")
        say(f"{LABELS[kind]}: {kept} files kept (sound and video left out)")
        shutil.rmtree(downloaded, ignore_errors=True)
    files = stage / "game"
    ready = {"halo3": has_game_files, "odst": has_odst_files, "campaign": has_campaign}[kind]
    if not ready(files):
        raise OSError(f"{LABELS[kind]} didn't arrive complete")
    return files


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


def shared_target(settings: Settings, kind: str) -> tuple[Path | None, str]:
    """Where I puts `kind`: this folder's shared game/ (Halo 3 first: ODST and the campaign go beside it)."""
    game = settings.home / "game"
    if kind != "halo3" and not has_game_files(game):
        return None, f"{game} has no Halo 3 files yet: get those first ({LABELS[kind]} go beside them)"
    return game, ""


def game_status(settings: Settings) -> list[tuple[str, str, str]]:
    """What I shows: (what, how it stands, colour) for this folder's shared game/ and every server's game folder."""
    from .upkeep import servers_using

    rows: list[tuple[str, str, str]] = []
    folders: dict[Path, list[str]] = {}
    lacking: list[str] = []
    for server in settings.servers:
        paths = read_paths(server.folder, settings.config_name)
        if paths is None:
            continue
        folders.setdefault(paths.game, []).append(server.name)
        if missing := missing_kinds(paths, server.plays_kind):
            lacking.append(f"{server.name} ({', '.join(LABELS[k] for k in missing)})")
    shared = (settings.home / "game").resolve()
    for game in [shared, *[g for g in folders if g != shared]]:
        if game != shared and game not in folders:
            continue
        users = folders.get(game) or servers_using(settings, game)
        if not has_game_files(game):
            state, colour = "no Halo 3 files" + ("" if game.exists() else " (not there)"), "yellow" if users else "gray"
        else:
            maps = game / "halo3" / "maps"
            forge = (maps / "campaign.map").is_file() and (maps / "040_voi.map").is_file()
            bits = [f"Halo 3, {multiplayer_maps(game)} multiplayer maps",
                    "every campaign mission" if has_campaign(game) else
                    "Forge's campaign files (no missions)" if forge else "no campaign files",
                    "ODST" if has_odst_files(game) else "no ODST"]
            state, colour = " · ".join(bits), "green"
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


def target_for(settings: Settings, paths: ServerPaths, kind: str) -> Path:
    """Where `kind` goes: Halo 3 into this folder's shared game/ (the server is pointed at it once it's there);
    ODST and the campaign beside the Halo 3 the server already has."""
    if kind == "halo3" or not has_game_files(paths.game) or is_mcc_install(paths.game):
        return settings.home / "game"
    return paths.game


class GameFiles:
    """The repairs above, for the Superintendent: quick ones whenever a server is found or about to start, and the
    game files got in the background while the servers that need them wait. Looking for MCC reads Steam's records
    and the getting is gigabytes: both run in threads, never on the loop."""

    def __init__(self, settings: Settings, bus: EventBus, tool: Callable[[], Dedicated],
                 release: Callable[[], None] | None = None) -> None:
        self.settings = settings
        self.bus = bus
        self.tool = tool
        self.release = release  # tells the waiting servers to look again (Fleet.release)
        self.getting: dict[str, str] = {}  # kind -> what's happening ("looking for…", "copying…", "downloading…")
        self.failed: dict[str, tuple[float, str]] = {}  # kind -> (when, why): not tried again by itself for a while
        self.fetched: dict[str, float] = {}  # kind -> when its files last arrived
        self.forced: dict[str, set[str]] = {}  # server -> kinds a failed start said are missing
        self.tasks: set[asyncio.Task[None]] = set()
        bus.subscribe("server.start_failed", self._on_start_failed)

    @property
    def here(self) -> bool:
        """Only the process backend runs the program on this PC with its folder's paths. In a container (docker)
        or wherever a command runs it, dedicated.toml's paths aren't this PC's: nothing is put right from here."""
        return self.settings.backend == "process"

    def arrived(self, server: ServerConfig) -> list[str]:
        """The quick repairs, each logged; for a server just found and before each start."""
        if not self.here:
            return []
        done = quick_repairs(self.settings, server)
        for line in done:
            log.warning("[%s] %s", server.name, line)
            self.bus.emit("server.repaired", server.name, text=line)
        return done

    def needed(self, server: ServerConfig) -> tuple[ServerPaths | None, list[str]]:
        paths = read_paths(server.folder, self.settings.config_name)
        if paths is None:
            return None, []
        kinds = missing_kinds(paths, server.plays_kind)
        for kind in sorted(self.forced.get(server.name, set())):
            if kind not in kinds:
                kinds.append(kind)
        return paths, kinds

    def prelaunch(self, server: ServerConfig) -> str | None:
        """Why the server can't start yet (it waits), or None. Sets about getting what it lacks when it may."""
        if not self.here:
            return None
        self.arrived(server)
        paths, kinds = self.needed(server)
        if paths is None or not kinds:
            self.forced.pop(server.name, None)
            return None
        for kind in kinds:
            if kind in self.getting:
                return f"getting {LABELS[kind]}: {self.getting[kind]}"
        kind = kinds[0]
        failed = self.failed.get(kind)
        if failed is not None and time.monotonic() - failed[0] < RETRY_SECONDS:
            return f"needs {LABELS[kind]}: {failed[1]}; press I"
        self.get(kind, target_for(self.settings, paths, kind))
        return f"getting {LABELS[kind]}: {self.getting[kind]}"

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
                    self.failed[kind] = (time.monotonic(), f"MCC isn't on this PC and {why}")
                    log.error("%s are missing and there's no way to get them here: MCC isn't on this PC and %s. The "
                              "servers that need them wait; press I (copy them from MCC on a PC that has it, or set "
                              "up SteamCMD to download them)", LABELS[kind], why)
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
        log.warning("[%s] it stopped for missing game files (%s): Huragok gets them before it tries again",
                    event.server, ", ".join(LABELS[k] for k in kinds))

    def summary(self) -> list[str]:
        lines = [f"getting {LABELS[k]}: {how}" for k, how in self.getting.items()]
        lines += [f"{LABELS[k]}: {why}" for k, (_, why) in self.failed.items()]
        return lines
