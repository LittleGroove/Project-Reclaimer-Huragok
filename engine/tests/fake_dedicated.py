"""A stand-in for `project-reclaimer.exe dedicated …`, for testing on Windows and Linux without the game.

`dedicated run` (in a server folder, with RECLAIMER_DEDICATED_ONLY naming the server):
- prints what the real one prints when a server starts;
- serves the fake RCON on the server's port and writes data/<server>/rcon-host.key;
- rewrites data/<server>/status.json every second, like the real server;
- stops cleanly on Ctrl+Break (Windows) or SIGINT (Linux), printing "All servers stopped.";
- exits with code 2 straight away if the server folder holds a file called fake_crash_on_start,
  and with code 3 as soon as a file called fake_crash appears while it runs.

The setup commands, with output shaped like the real program's (v0.9.18; ODST's parts v0.9.20):
- `dedicated init DIR [--from [MCC]] [--maps a,b]`: the folder, its dedicated.toml (the same keys and
  two example [[server]] blocks), playlists/slayer.playlist.json, bans.json, content/, game/
  (with --from: halo3.dll, shared.map and the 27 maps as empty files; MCC must be a folder when given;
  `--maps odst` copies Guardian and Halo 3: ODST's halo3odst/ instead, which MCC must have);
- `dedicated check`: game files, content, bans, each server's playlist (3+ games, every map and game
  known), unique names and ports; writes the scripted modes into content/Game Modes/Project Reclaimer
  on the first check, like the real one; "Everything is ready." or "Error: …" with exit code 2;
- `dedicated content [words]`, `dedicated playlist [list | show FILE | new NAME --maps --games
  [--vote-seconds N] [--replace]]`, `dedicated mods` (installs [workshop] items as fake folders),
  `dedicated reload`, `dedicated status`.

Workshop items behave as the real program's do (tested 2026-10-18): an installed item (one with a
reclaimer-workshop-version.json receipt) is kept as it is ("installed"), even when Steam has a newer
version; a missing one is fetched at Steam's newest version, which the server folder's
fake-steam.json gives ({"<item>": <updated, Unix seconds>}; 1791857542 otherwise). A file called
fake_steam_down in the server folder makes every fetch fail the way an expired SteamCMD sign-in does.

A fake never outlives the test session that started it: `run` and `slow` end by themselves (as after
Ctrl+Break) once the process HURAGOK_TEST_PARENT names is gone (conftest sets it), so a test that failed
half-way can't leave one running, holding its port.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from fake_rcon import FakeRcon  # noqa: E402

MAPS = [("Assembly", "descent"), ("Avalanche", "sidewinder"), ("Blackout", "lockout"), ("Citadel", "fortress"),
        ("Cold Storage", "chillout"), ("Construct", "construct"), ("Edge", "s3d_edge"), ("Epitaph", "salvation"),
        ("Foundry", "warehouse"), ("Ghost Town", "ghosttown"), ("Guardian", "guardian"), ("Heretic", "midship"),
        ("High Ground", "deadlock"), ("Icebox", "s3d_turf"), ("Isolation", "isolation"), ("Last Resort", "zanzibar"),
        ("Longshore", "docks"), ("Narrows", "chill"), ("Orbital", "spacecamp"), ("Rat's Nest", "armory"),
        ("Sandbox", "sandbox"), ("Sandtrap", "shrine"), ("Snowbound", "snowbound"), ("Standoff", "bunkerworld"),
        ("The Pit", "cyberdyne"), ("Valhalla", "riverworld"), ("Waterfall", "s3d_waterfall")]
MAP_VARIANTS = [("Boundless", "Snowbound"), ("MLG Amplified 8", "Foundry"), ("MLG Construct 8", "Construct"),
                ("MLG Guardian 8", "Guardian"), ("MLG Heretic 8", "Heretic"), ("MLG Narrows 8", "Narrows"),
                ("MLG Pit 8", "The Pit"), ("Pit Stop", "The Pit")]
GAME_TYPES = ["Slayer", "Capture the Flag", "Oddball", "King of the Hill", "Assault", "Infection", "Territories",
              "Juggernaut", "VIP", "Escalation Slayer"]
H3_VARIANTS = [("team_slayer", "Slayer"), ("multi_flag", "Capture the Flag"), ("team_king", "King of the Hill"),
               ("team_oddball", "Oddball"), ("alpha_zombie", "Infection"), ("rockets", "Slayer"),
               ("HARDCORE TS", "Slayer"), ("HARDCORE BALL", "Oddball"), ("HARDCORE KING", "King of the Hill"),
               ("HARDCORE FFA", "Slayer"), ("2V2 HARDCORE TS", "Slayer")]
SCRIPTED = {"gun-game": "Gun Game", "growth": "Growth", "one-in-the-chamber": "One in the Chamber",
            "zone-control": "Zone Control", "flood-infection": "Flood Infection", "faction-battlefront": "UNSC vs Covenant"}

TEMPLATE = """# Project Reclaimer dedicated servers (a stand-in for tests)
# Relative paths are relative to this file's folder.

# Halo 3 files copied from your own MCC installation.
game = "game"
# Maps, game types and mods shared by every server.
content = "content"
# Logs, chat logs and live status, one folder per server.
data = "data"
# Player and IP bans shared by every server.
bans = "bans.json"
public_address = ""
upnp = false
auto_update = false
auto_reload = true
# Shown beneath each server's name in the server browser.
host_name = "Dedicated server"
admins = [
]
stats_server = ""

# Optional: download Workshop mods automatically before resolving games.
# [workshop]
# items = [3663541185]
# update_check_minutes = 60

[master]
enabled = true
port = 49175
peers = []
origin = ""

[rcon]
# Off while the password is empty; 8 characters at least.
password = ""
address = "127.0.0.1"

[vpn]
lists = []

[defaults]
voice = true
voice_global = true
voice_team = true
voice_proximity = true
friendly_fire = true
text_chat = true
chat_log = true
votes = true
block_vpn = true
hidden = false
password = ""
max_players = 16
min_players = 1
#max_ping = 150
share_mods = false
share_rate = 20
#welcome = "Welcome to {server}, {name}!"

# One [[server]] block per server.

[[server]]
name = "Reclaimer Slayer"
port = 49176
playlist = "playlists/slayer.playlist.json"

[[server]]
name = "Reclaimer Guardian"
port = 49177
map = "Guardian"
game = "Slayer"
"""

EXAMPLE_PLAYLIST = {
    "version": 2, "name": "Halo 3 community rotation", "vote_seconds": 20,
    "games": [{"map": "Guardian", "game": "Slayer", "weight": 2}, {"map": "Valhalla", "game": "Slayer"},
              {"map": "Last Resort", "game": "Slayer"},
              {"map": "The Pit", "game": "Slayer", "name": "Slayer to 50", "rules": {"score_to_win": 50}},
              {"map": "Valhalla", "game": "Slayer", "name": "BR Starts",
               "options": {"player.primary_weapon": "Battle rifle", "player.secondary_weapon": "Magnum"}}]}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


_parent: list[Any] = []  # [psutil.Process or None], looked up once


def parent_gone() -> bool:
    """The test session that started this fake (HURAGOK_TEST_PARENT) has ended: no such process any more, or
    its number now belongs to another one. Without the variable (run by hand), never."""
    if not _parent:
        text = os.environ.get("HURAGOK_TEST_PARENT", "")
        try:
            import psutil

            _parent.append(psutil.Process(int(text)) if text.isdigit() else None)
        except ImportError:
            _parent.append(None)
        except Exception:  # noqa: BLE001 - psutil.NoSuchProcess: gone before this fake even looked
            return True
    parent = _parent[0]
    return parent is not None and not parent.is_running()  # is_running also compares its start time


def option(words: list[str], name: str, default: str | None = None) -> str | None:
    if name in words:
        at = words.index(name)
        value = words[at + 1] if at + 1 < len(words) and not words[at + 1].startswith("--") else ""
        del words[at: at + 2 if value else at + 1]
        return value
    return default


class Folder:
    """A server folder as the fake reads it."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.folder = config.parent
        with config.open("rb") as file:
            self.settings = tomllib.load(file)
        self.game = (self.folder / str(self.settings.get("game") or "game")).resolve()
        self.content = (self.folder / str(self.settings.get("content") or "content")).resolve()
        self.bans = (self.folder / str(self.settings.get("bans") or "bans.json")).resolve()

    def has_game(self) -> bool:
        return (self.game / "halo3" / "halo3.dll").is_file() and (self.game / "halo3" / "maps" / "shared.map").is_file()

    def has_odst(self) -> bool:
        return (self.game / "halo3odst" / "halo3odst.dll").is_file()

    def installed_maps(self) -> list[tuple[str, str]]:
        return [(t, s) for t, s in MAPS if (self.game / "halo3" / "maps" / f"{s}.map").is_file()]

    def variants(self) -> list[tuple[str, str]]:
        found = []
        modes = self.content / "Game Modes"
        if modes.is_dir():
            for path in sorted(modes.rglob("*.json")):
                try:
                    found.append((str(json.loads(path.read_text(encoding="utf-8")).get("name") or path.stem), "Slayer"))
                except ValueError:
                    continue
            for path in sorted(modes.rglob("*.bin")):
                found.append((path.stem, "Slayer"))
        if (self.game / "halo3" / "game_variants").is_dir():
            found.extend(H3_VARIANTS)
        return found

    def mods(self) -> list[tuple[str, str]]:
        """(mod title, map title) for every content/Mods/*/ModInfo.json, as the real program lists them."""
        found = []
        mods = self.content / "Mods"
        for info in sorted(mods.rglob("ModInfo.json")) if mods.is_dir() else []:
            try:
                data = json.loads(info.read_text(encoding="utf-8"))
            except ValueError:
                continue
            title = str((data.get("Title") or {}).get("Neutral") or info.parent.name)
            for relative in (data.get("GameModContents") or {}).get("MultiplayerMaps") or []:
                path = info.parent / str(relative).replace("\\", "/")
                try:
                    name = str((json.loads(path.read_text(encoding="utf-8")).get("Title") or {}).get("Neutral") or path.stem)
                except (OSError, ValueError):
                    name = path.stem
                found.append((title, name))
        return found

    def mod_maps(self) -> list[str]:
        mods = self.content / "Mods" / "Workshop"
        refs = []
        if mods.is_dir():
            for item in sorted(mods.iterdir()):
                for path in sorted((item / "maps").glob("*.map")) if (item / "maps").is_dir() else []:
                    refs.append(f"workshop:{item.name}/{path.stem}")
        return refs

    def write_scripted_modes(self) -> None:
        folder = self.content / "Game Modes" / "Project Reclaimer"
        folder.mkdir(parents=True, exist_ok=True)
        for stem, name in SCRIPTED.items():
            path = folder / f"{stem}.json"
            if not path.exists():
                path.write_text(json.dumps({"version": 1, "name": name, "mode": "Slayer"}), encoding="utf-8")

    def known_map(self, ref: str) -> bool:
        ref = str(ref)
        titles = {t.lower() for t, _ in self.installed_maps()} | {s.lower() for _, s in self.installed_maps()}
        if ref.lower() in titles or ref.lower() in {n.lower() for n, _ in MAP_VARIANTS}:
            return True
        maps = self.content / "Maps"
        if maps.is_dir() and any(p.stem.lower() == ref.lower() for p in maps.rglob("*.mvar")):
            return True
        if ref.lower() in {f"{m} · custom" for _, m in self.mods()}:
            return True
        if ref.startswith("workshop:"):
            return ref in self.mod_maps()
        if ref.startswith("Maps/"):
            return (self.content / ref).is_file()
        return False

    def known_game(self, ref: str) -> bool:
        ref = str(ref)
        names = {g.lower() for g in GAME_TYPES} | {n.lower() for n, _ in self.variants()}
        if ref.lower() in names:
            return True
        if ref.startswith("Game Modes/"):
            return (self.content / ref).is_file()
        return False

    def playlist_path(self, ref: str) -> Path:
        path = self.folder / ref
        if path.is_file():
            return path
        named = self.folder / "playlists" / f"{ref}.playlist.json"
        return named if named.is_file() else path

    def playlist_problems(self, path: Path) -> list[str]:
        return [why for _, why in self.game_problems(path)]

    def game_problems(self, path: Path) -> list[tuple[int, str]]:
        """(game number or 0 for the file, problem), as the real program reports them."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return [(0, f"{path.name}: {error}")]
        games = data.get("games", []) if isinstance(data, dict) else []
        problems: list[tuple[int, str]] = []
        if len(games) < 3:
            problems.append((0, f"{path.name}: a playlist needs 3 or more games"))
        seen = set()
        for number, game in enumerate(games, 1):
            pair = (str(game.get("map")), str(game.get("game")))
            same = (*pair, str(game.get("name", "")), json.dumps(game.get("rules")), json.dumps(game.get("options")))
            if same in seen:  # the real one tells entries apart by their name, rules and options too
                problems.append((number, f"{pair[1]} on {pair[0]} is listed twice"))
            seen.add(same)
            if not self.known_map(pair[0]):
                problems.append((number, f"No map is called \"{pair[0]}\"; dedicated content lists the names"))
            if not self.known_game(pair[1]):
                problems.append((number, f"No game type is called \"{pair[1]}\""))
        return problems


ODST_MISSIONS = [(4100, "Prepare to Drop", 0), (5000, "Mombasa Streets", 6), (6100, "Tayari Plaza", 3),
                 (4200, "Epilogue", 0)]
ODST_MAPS = [(6100, 4, "Crater"), (5000, 7, "Crater (Night)"), (6110, 4, "Lost Platoon")]
ODST_TYPES = [("odst_ff_classic", "Ff Classic"), ("odst_ff_floodfight", "Floodfight"),
              ("odst_ff_standard_1set", "Firefight"), ("odst_ff_floodfight_1set", "Floodfight")]


def cmd_init(words: list[str]) -> int:
    mcc = option(words, "--from", None)
    maps = option(words, "--maps", None)
    target = Path(words[0] if words and not words[0].startswith("--") else ".")
    if mcc:
        if not Path(mcc).is_dir():
            print(f"Error: {mcc} is not a folder")
            return 2
    for sub in ("game", "content/Maps", "content/Images", "content/Game Modes", "content/Mods", "playlists", "data"):
        (target / sub).mkdir(parents=True, exist_ok=True)
        print(f"Created {target}/{sub}")
    for name, text in (("dedicated.toml", TEMPLATE),
                       ("playlists/slayer.playlist.json", json.dumps(EXAMPLE_PLAYLIST, indent=2) + "\n"),
                       ("bans.json", '{\n  "players": [],\n  "ips": []\n}\n'),
                       ("content/README.txt", "Project Reclaimer content\n"),
                       ("game/COPY GAME FILES HERE.txt", "Copy halo3/halo3.dll and the maps here.\n")):
        path = target / name
        if not path.exists():
            path.write_text(text, encoding="utf-8")
            print(f"Created {target}/{name}")
    if mcc is not None:
        wanted = {m.strip().lower() for m in maps.split(",")} if maps else None
        if wanted and "odst" in wanted:
            if mcc and not (Path(mcc) / "halo3odst").is_dir():
                print(f"Error: Halo 3: ODST is not installed in {mcc}")
                return 2
            wanted = (wanted - {"odst"}) | {"guardian"}
            odst = target / "game" / "halo3odst"
            (odst / "maps").mkdir(parents=True, exist_ok=True)
            (odst / "game_variants").mkdir(exist_ok=True)
            (odst / "halo3odst.dll").write_bytes(b"odst dll")
            for stem in ("shared", "campaign", "c100", "h100", "sc100", "l300"):
                (odst / "maps" / f"{stem}.map").write_bytes(b"map")
            for variant in ODST_TYPES:
                (odst / "game_variants" / f"{variant[0]}.bin").write_bytes(b"variant")
            print(f"  halo3odst\\halo3odst.dll (11 MB)")
        halo3 = target / "game" / "halo3"
        (halo3 / "maps" / "info").mkdir(parents=True, exist_ok=True)
        (halo3 / "game_variants").mkdir(exist_ok=True)
        (halo3 / "halo3.dll").write_bytes(b"dll")
        (halo3 / "maps" / "shared.map").write_bytes(b"map")
        if wanted and "campaign" in wanted:  # --maps campaign: the missions, and campaign.map they share
            wanted = (wanted - {"campaign"}) | {"guardian"}
            for stem in ("campaign", "005_intro", "010_jungle", "020_base", "030_outskirts", "040_voi",
                         "050_floodvoi", "070_waste", "100_citadel", "110_hc", "120_halo", "130_epilogue"):
                (halo3 / "maps" / f"{stem}.map").write_bytes(b"map")
        for title, stem in MAPS:
            if wanted and title.lower() not in wanted and stem.lower() not in wanted:
                continue
            (halo3 / "maps" / f"{stem}.map").write_bytes(b"map")
            (halo3 / "maps" / "info" / f"{stem}.mapinfo").write_bytes(b"info")
        print(f"Copied Halo 3's files from {mcc or 'Steam'} (hard links)")
    print("\nNext:\n  1. Edit dedicated.toml (server names, ports, admins).\n  2. Run `project-reclaimer dedicated check`.")
    return 0


def cmd_check(folder: Folder) -> int:
    if not folder.has_game():
        print(f"Error: The game folder {folder.game} is missing files copied from your own MCC installation:\n"
              "  halo3/halo3.dll\n  halo3/maps/shared.map\n  a multiplayer map: halo3/maps/<map>.map")
        return 2
    folder.write_scripted_modes()
    maps = folder.installed_maps()
    print(f"Configuration  {folder.config.name}")
    print(f"Game files     {folder.game} ({len(maps)} multiplayer maps: {', '.join(t for t, _ in maps)})")
    print(f"Content        {folder.content}")
    if folder.bans.exists():
        try:
            bans = json.loads(folder.bans.read_text(encoding="utf-8"))
            print(f"Bans           {folder.bans} ({len(bans.get('players', []))} player IDs, {len(bans.get('ips', []))} addresses)")
        except ValueError as error:
            print(f"Error: {folder.bans}: {error}")
            return 2
    items = folder.settings.get("workshop", {}).get("items", [])
    if items:
        install_items(folder, items)
    print(f"Workshop       {len(items)} managed item(s)" if items else "Workshop       no managed items")
    rcon = folder.settings.get("rcon", {}).get("password", "")
    print(f"RCON           127.0.0.1 · tools on this machine only" if rcon else
          "RCON           off (set a password under [rcon] to turn it on)")
    problems = []
    seen_names: set[str] = set()
    seen_ports: set[int] = set()
    for block in folder.settings.get("server", []):
        name, port = str(block.get("name", "")), int(block.get("port", 0))
        if name.lower() in seen_names:
            problems.append(f"two servers are called {name}")
        if port in seen_ports or port == 49175:
            problems.append(f"port {port} is used twice")
        seen_names.add(name.lower())
        seen_ports.add(port)
        if "playlist" in block:
            path = folder.playlist_path(str(block["playlist"]))
            if not path.is_file():
                problems.append(f"{name}: playlist {block['playlist']} is not there")
            else:
                problems.extend(folder.playlist_problems(path))
            what = f"playlist {path.name}"
        elif "campaign" in block:
            what = f"campaign {block['campaign'].get('mission', '')}"
        elif "odst" in block:
            if not folder.has_odst():
                problems.append(f"Server '{name}': Halo 3: ODST's files are not in the game folder {folder.game}: "
                                "copy them from your MCC installation with `dedicated init <server folder> --from "
                                "\"<MCC folder>\" --maps odst`")
            players = block.get("max_players", folder.settings.get("defaults", {}).get("max_players", 16))
            if int(players) > 16:
                problems.append(f"Server '{name}': Halo 3: ODST supports at most 16 players")
            what = f"Halo 3: ODST, {block['odst'].get('mission', '')}"
        else:
            what = f"{block.get('game', 'Slayer')} on {block.get('map', 'Guardian')}"
            if "map" in block and not folder.known_map(str(block["map"])):
                problems.append(f"{name}: no map is called \"{block['map']}\"")
        print(f"Server         {name} · UDP {port} · {what}" + (" · hidden" if block.get("hidden") else ""))
    if problems:
        print("Error: " + problems[0])
        for problem in problems[1:]:
            print("       " + problem)
        return 2
    print("\nEverything is ready. Start the servers with: project-reclaimer dedicated")
    return 0


def print_odst(folder: Folder, keep: Any) -> None:
    """Halo 3: ODST's sections of `dedicated content` (0.9.19), when its files are in the game folder."""
    if not folder.has_odst():
        return
    print("Halo 3: ODST campaign missions:")
    for mission, title, high in ODST_MISSIONS:
        if keep(title, "campaign", "odst"):
            print(f"  odst = {{ mission = {mission} }}  {title}" + (f" (insertion 0 to {high})" if high else ""))
    print("Halo 3: ODST Firefight maps:")
    for mission, insertion, title in ODST_MAPS:
        if keep(title, "firefight", "odst"):
            print(f'  odst = {{ mode = "firefight", mission = {mission}, insertion = {insertion} }}  {title}')
    print("Halo 3: ODST Firefight game types: inside odst = { … }")
    for variant, title in ODST_TYPES:
        if keep(variant, title, "firefight", "odst"):
            print(f'  variant = "{variant}"{" " * max(2, 31 - len(variant))}{title}')


def cmd_content(folder: Folder, words: list[str]) -> int:
    odst_only = bool(words) and words[0].lower() == "odst"
    wanted = [w.lower() for w in (words[1:] if odst_only else words)]

    def keep(*texts: str) -> bool:
        return all(any(w in t.lower() for t in texts) for w in wanted)

    if odst_only:
        print('Map variants: map = "…"; each plays on its own map')
        print('Game variants: game = "…"')
        print_odst(folder, keep)
        print(f"\nNames ignore case. Put map variants in {folder.content / 'Maps'} and game variants in "
              f"{folder.content / 'Game Modes'}; Workshop mods appear once installed (dedicated mods).")
        return 0
    print('Maps: map = "…" (and base_map), or a playlist game\'s "map"')
    for title, stem in folder.installed_maps():
        if keep(title, stem):
            print(f'  "{title}"' + (f'  or "{stem}"' if stem != title.lower() else ""))
    for mod_title, map_title in folder.mods():
        if keep(mod_title, map_title):
            print(f'  "{map_title} · Custom"  mod · {mod_title}')
    print('Map variants: map = "…"; each plays on its own map')
    for name, base in MAP_VARIANTS:
        if keep(name, base):
            print(f'  "{name}"  map variant · {base}')
    for path in sorted((folder.content / "Maps").rglob("*.mvar")) if (folder.content / "Maps").is_dir() else []:
        if keep(path.stem):
            print(f'  "{path.stem}"  map variant · (your content folder)')
    print('Game types: game = "…", or a playlist game\'s "game"')
    for name in GAME_TYPES:
        if keep(name):
            print(f'  "{name}"')
    print('Game variants: game = "…"')
    for name, base in folder.variants():
        if keep(name, base):
            print(f'  "{name}"  game variant · {base}')
    refs = folder.mod_maps()
    if refs:
        print('Mod maps: map = "workshop:…"')
        for ref in refs:
            if keep(ref):
                print(f'  "{ref}"  mod map')
    print_odst(folder, keep)
    print(f"\nNames ignore case. Put map variants in {folder.content / 'Maps'} and game variants in "
          f"{folder.content / 'Game Modes'}; Workshop mods appear once installed (dedicated mods).")
    return 0


def playlist_line(folder: Folder, path: Path) -> tuple[str, list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    games = data.get("games", [])
    players = [str(b.get("name")) for b in folder.settings.get("server", [])
               if folder.playlist_path(str(b.get("playlist", ""))) == path]
    line = f'{path.relative_to(folder.folder).as_posix()} · "{data.get("name", "")}" · {len(games)} games · vote {data.get("vote_seconds", 20)} s'
    if players:
        line += " · played by " + ", ".join(players)
    return line, folder.playlist_problems(path)


def cmd_playlist(folder: Folder, words: list[str]) -> int:
    sub = words[0] if words else "list"
    if sub == "list":
        files = sorted((folder.folder / "playlists").glob("*.playlist.json"))
        if not files:
            print("No playlists in playlists/. Make one with: dedicated playlist new")
            return 0
        for path in files:
            line, problems = playlist_line(folder, path)
            print(line)
            for problem in problems:
                print(f"    {problem}")
        return 0
    if sub == "show":
        path = folder.playlist_path(words[1] if len(words) > 1 else "")
        if not path.is_file():
            print(f"Error: no playlist file {words[1] if len(words) > 1 else ''}")
            return 2
        line, _ = playlist_line(folder, path)
        print(line)
        data = json.loads(path.read_text(encoding="utf-8"))
        games = data.get("games", [])
        total = sum(int(g.get("weight", 1)) for g in games) or 1
        by_game = folder.game_problems(path)
        for number, game in enumerate(games, 1):
            weight = int(game.get("weight", 1))
            label = f"{game.get('name') or game['game']} on {game['map']}"
            print(f"   {number}. {label}" + (f" · weight {weight}" if weight != 1 else "") + f" · {100 * weight // total}%")
            for at, why in by_game:
                if at == number:
                    print(f"        PROBLEM: {why}")
        bad = {at for at, _ in by_game if at}
        if by_game:
            for at, why in by_game:
                if at == 0:
                    print(f"Error: {why}")
            if bad:
                print(f"Error: {len(bad)} of {len(games)} games cannot be played; `dedicated content` lists the names this server has")
            return 2
        print("Every game is installed.")
        return 0
    if sub == "new":
        rest = words[1:]
        maps = option(rest, "--maps", "") or ""
        games = option(rest, "--games", "") or ""
        vote = int(option(rest, "--vote-seconds", "20") or 20)
        replace = "--replace" in rest
        rest = [w for w in rest if w != "--replace"]
        name = rest[0] if rest else ""
        if not name or not maps or not games:
            print("Error: playlist new needs a name, --maps and --games")
            return 2
        entries = [{"map": m.strip(), "game": g.strip()} for m in maps.split(",") for g in games.split(",")]
        path = folder.folder / "playlists" / f"{slugify(name)}.playlist.json"
        if path.exists() and not replace:
            print(f"Error: {path.relative_to(folder.folder).as_posix()} exists; --replace overwrites it")
            return 2
        bad = [e for e in entries if not folder.known_map(e["map"]) or not folder.known_game(e["game"])]
        if bad:
            print(f"Error: no map or game type is called \"{bad[0]['map']}\" / \"{bad[0]['game']}\"; "
                  "dedicated content lists the names")
            return 2
        if len(entries) < 3:
            print("Error: a playlist needs 3 or more games")
            return 2
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 2, "name": name, "vote_seconds": vote, "games": entries}, indent=2) + "\n",
                        encoding="utf-8")
        print(f"Wrote {path.relative_to(folder.folder).as_posix()} · {len(entries)} games")
        return 0
    print(f"Error: unknown playlist command {sub}")
    return 2


FAKE_STEAM_UPDATED = 1791857542  # 13 Oct 2026


def steam_version(folder: Folder, item: int) -> int:
    try:
        versions = json.loads((folder.config.parent / "fake-steam.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        versions = {}
    return int(versions.get(str(item), FAKE_STEAM_UPDATED))


def install_items(folder: Folder, items: list[int]) -> list[str]:
    """Each item's reference; prints what the real program prints. Raises SystemExit(2) when a fetch fails."""
    refs = []
    for item in items:
        target = folder.content / "Mods" / "Workshop" / str(item)
        if (target / "reclaimer-workshop-version.json").is_file():
            print(f"Workshop {item}: installed")
            refs.append(f"workshop:{item}/fake{item}")
            continue
        print(f"Workshop {item}: acquiring from Steam")
        if (folder.config.parent / "fake_steam_down").exists():
            print(f"Error: Workshop {item}: signing in as host failed: sign in to SteamCMD again")
            raise SystemExit(2)
        updated = steam_version(folder, item)
        (target / "maps").mkdir(parents=True, exist_ok=True)
        (target / "maps" / f"fake{item}.map").write_bytes(f"map {updated}".encode())
        version = {"item": item, "manifest": updated * 7, "updated": updated}
        (target / "reclaimer-workshop-version.json").write_text(json.dumps(version), encoding="utf-8")
        (target / "reclaimer-installed.json").write_text(json.dumps({"item": item, "files": [], "version": version}),
                                                         encoding="utf-8")
        print(f"Workshop {item}: ready (version of {time.strftime('%d %b %Y', time.gmtime(updated))})")
        refs.append(f"workshop:{item}/fake{item}")
    return refs


def cmd_mods(folder: Folder) -> int:
    items = folder.settings.get("workshop", {}).get("items", [])
    if not items:
        print("No mod maps installed. Add Workshop IDs to [workshop] items in dedicated.toml.")
        return 0
    for item, ref in zip(items, install_items(folder, items)):
        print(f"{ref}  Fake Map {item} · Custom")
    return 0


async def run_server(config: Path) -> int:
    folder = config.parent
    with config.open("rb") as file:
        settings = tomllib.load(file)
    only = os.environ.get("RECLAIMER_DEDICATED_ONLY", "")
    block = next((b for b in settings["server"] if b["name"] == only), settings["server"][0])
    name, port = block["name"], int(block["port"])
    password = settings.get("rcon", {}).get("password", "")
    data = folder / settings.get("data", "data") / slugify(name)
    data.mkdir(parents=True, exist_ok=True)
    if (folder / "fake_crash_on_start").exists():
        print(f"[{name}] stopped: its game met an error it could not recover from; restarting in 5 s. See x",
              flush=True)
        return 2
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(stopping.set))
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stopping.set))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopping.set)
    async with FakeRcon(password or "unset", port=port) as rcon:
        (data / "rcon-host.key").write_text("f" * 64, encoding="utf-8")
        print(f"[{name}] started (process {os.getpid()}); players join at 0.0.0.0:{port}.", flush=True)
        while not stopping.is_set():
            (data / "status.json").write_text(json.dumps({"name": name, "map": "Guardian", "mode": "Slayer",
                                                          "players": rcon.players, "updated": int(time.time())}),
                                              encoding="utf-8")
            if (folder / "fake_crash").exists():
                print(f"[{name}] panic", flush=True)
                os._exit(3)
            if parent_gone():
                print(f"[{name}] the test session that started this fake is gone", flush=True)
                break
            try:
                await asyncio.wait_for(stopping.wait(), 1.0)
            except TimeoutError:
                pass
    print("Stopping every server... All servers stopped.", flush=True)
    return 0


MODES = ("CaptureTheFlag", "Slayer", "Forge", "Oddball", "KingOfTheHill", "Vip", "Juggernaut", "Territories",
         "Assault", "Infection", "EscalationSlayer")
OPTION_RANGES = {"map.weapons": ("Weapon set", 0, 13), "player.speed": ("Movement speed", 0, 10),
                 "player.primary_weapon": ("Starting primary weapon", -3, 29), "rounds.minutes":
                 ("Time limit (minutes; 0 = unlimited)", 0, 60), "mode.flag_type": ("Flag type", 0, 2)}


def cmd_mode_check(path: Path) -> int:
    """`mode-check <file>` as 0.9.18 answers it (tested 2026-10-18): rules required (may be empty), mode an
    identifier, a script needed for any check, options in range."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"Error: {error}")
        return 1
    if "rules" not in data:
        print("Error: Invalid custom game: missing field `rules`")
        return 1
    if data.get("mode") not in MODES:
        print(f"Error: Invalid custom game: unknown variant `{data.get('mode')}`, expected one of "
              + ", ".join(f"`{m}`" for m in MODES))
        return 1
    if not data.get("script"):
        print("Error: Preset has no custom game script")
        return 1
    for key, value in (data.get("options") or {}).items():
        if key in OPTION_RANGES:
            label, low, high = OPTION_RANGES[key]
            if not isinstance(value, int) or not low <= value <= high:
                print(f"Error: {label} must be {low}..={high}")
                return 1
    print(f"Valid custom mode: {data['script'].get('name', data.get('name'))} (script version 1)")
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # UTF-8 whatever the console's code page, like the real program
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    words = sys.argv[1:]
    if words and words[0] == "mode-check":
        return cmd_mode_check(Path(words[1]))
    if words and words[0] == "dedicated":
        words = words[1:]
    config_name = option(words, "--config", "dedicated.toml") or "dedicated.toml"
    words = [w for w in words if w != "--verbose"]
    sub = words[0] if words else "run"
    rest = words[1:]
    if sub == "init":
        return cmd_init(rest)
    config = Path.cwd() / config_name
    if sub == "run":
        return asyncio.run(run_server(config))
    try:
        folder = Folder(config)
    except FileNotFoundError:
        print(f"Error: {config} not found; `dedicated init` creates one")
        return 2
    except tomllib.TOMLDecodeError as error:
        print(f"Error: {config}: {error}")
        return 2
    if sub == "check":
        return cmd_check(folder)
    if sub == "content":
        return cmd_content(folder, rest)
    if sub == "playlist":
        return cmd_playlist(folder, rest)
    if sub == "mods":
        return cmd_mods(folder)
    if sub == "slow":  # for tests: lines over time, until ended
        for number in range(int(rest[0]) if rest else 30):
            if parent_gone():
                return 0
            print(f"slow line {number}", flush=True)
            time.sleep(0.5)
        return 0
    if sub == "reload":
        print("Reload result: nothing changed")
        return 0
    if sub == "status":
        print("No server is running from this folder")
        return 1
    if sub == "bans":
        print(f"No bans in {folder.bans}.")
        return 0
    print(f"Error: unknown command {sub}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
