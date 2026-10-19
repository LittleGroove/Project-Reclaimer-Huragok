"""A server's status card (V in the window): everything the Superintendent keeps an eye on, on one screen.

    Running    RUNNING up 3h 12m · RCON 4 ms · status 2 s old · process 1234 · 610 MB
    Joins at   203.0.113.9:49177 · listed · password set · 12/16 players
    Plays      playlist hugegrassremix: "HugegrassRemix", 3 games, 20 s votes · now CTF on Hugegrass
    Program    Project Reclaimer 0.9.18 · installs its own releases (auto_update) · newest 0.9.18
    Mods       2 Workshop items · new versions after the game · the server looks every 5 min
    ...

Built from the server's own files (dedicated.toml, its playlist, the ban list, the Workshop receipts), its
dashboard row (live.json carries the same row, so a second window can show the card too) and what the
supervising window adds: the newest program release, Steam's Workshop versions, memory in use.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .livesettings import current, find, plays_of, show
from .playlists import PlaylistError, load_playlist
from .tomledit import read_toml

if TYPE_CHECKING:
    from .config import ServerConfig, Settings

Line = tuple[str, str, str | None]  # (label, text, colour)
KINDS = {"start": "start", "restart": "restart", "stop": "stop", "crash": "CRASH", "faulted": "FAULTED"}


def folder_size(path: Path) -> int:
    """Bytes in a folder and everything under it (0 when it isn't there)."""
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += folder_size(Path(entry.path))
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def size_text(size: int) -> str:
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} GB"
    if size >= 1 << 20:
        return f"{size / (1 << 20):.0f} MB"
    return f"{size / 1024:.0f} KB"


def workshop_disk(server: ServerConfig) -> int:
    """What the server's Workshop items take: the installed copies and SteamCMD's own copies beside them."""
    total = 0
    for item in server.workshop_items:
        if server.content_dir is not None:
            total += folder_size(server.content_dir / "Mods" / "Workshop" / str(item))
        total += folder_size(server.folder / "data" / "workshop-cache" / "steamapps" / "workshop" / "content"
                             / "976730" / str(item))
    return total


def _bans(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no ban list yet" if not path.exists() else f"{path.name} can't be read"
    players, ips = len(data.get("players") or []), len(data.get("ips") or [])
    return f"{players} player ban(s), {ips} address ban(s)"


def self_update_text(settings: Settings) -> str:
    """How a server with auto_update on keeps up."""
    text = "installs its own releases (auto_update): at its start and hourly, restarting at once"
    if settings.server_updates.fallback and settings.backend == "process":
        text += "; if one doesn't arrive, Huragok installs it after the game"
    return text

def build(settings: Settings, server: ServerConfig, row: dict[str, Any], extra: dict[str, Any] | None = None
          ) -> list[tuple[str, list[Line]]]:
    """The card's sections: (title, lines)."""
    extra = extra or {}
    try:
        data = read_toml(server.config_path)
        unreadable = ""
    except (OSError, ValueError) as error:
        data, unreadable = {}, f"{server.config_path.name} can't be read: {error}"

    def value(key: str, scope: str = "server") -> Any:
        return current(data, find(key, scope), server.name, settings)[0]

    block = next((b for b in data.get("server", []) if str(b.get("name", "")).lower() == server.name.lower()), {})
    sections: list[tuple[str, list[Line]]] = []

    # Running
    bits = []
    if row.get("rcon_ms") is not None:
        bits.append(f"RCON {row['rcon_ms']} ms")
    if row.get("status_age") is not None:
        bits.append(f"status {row['status_age']} s old")
    if row.get("pid"):
        bits.append(f"process {row['pid']}")
    if extra.get("memory_mb"):
        bits.append(f"{extra['memory_mb']:.0f} MB in use")
    running: list[Line] = [("Status", str(row.get("status") or "?"), row.get("color"))]
    if bits:
        running.append(("", " · ".join(bits), "gray"))
    if row.get("jobs"):
        running.append(("Before it starts", ", ".join(row["jobs"]), "cyan"))
    sections.append(("Running", running))

    # Joining
    address = row.get("address") or (f"{value('public_address', 'folder') or 'this machine'}:{server.game_port}")
    players = str(row.get("players") or f"0/{value('max_players')}")
    password = value("password")
    sections.append(("Joining", [
        ("Joins at", address, None),
        ("", " · ".join([("hidden (by address only)" if value("hidden") else "listed in the browser"),
                         ("join password set" if password else "anyone can join"),
                         f"{players} players" if row.get("players") else f"{value('max_players')} at most",
                         f"ping limit {value('max_ping')} ms" if value("max_ping") else "no ping limit"]), "gray"),
        ("Welcome", show(value("welcome")) if value("welcome") else "(none)", "gray"),
    ]))

    # Plays
    plays: list[Line] = [("Plays", plays_of(block) if block else server.plays, None)]
    if "playlist" in block:
        try:
            playlist = load_playlist(server.folder / str(block["playlist"]))
            plays.append(("", f'"{playlist.name}" · {len(playlist.games)} games · {playlist.vote_seconds} s votes',
                          "gray"))
        except PlaylistError as error:
            plays.append(("", f"the playlist can't be read: {error}", "red"))
    if row.get("game"):
        plays.append(("Now", str(row["game"]), "gray"))
    sections.append(("What it plays", plays))

    # Program
    program = [("Version", f"Project Reclaimer {row.get('version') or '?'}", None),
               ("Updates", self_update_text(settings) if server.auto_update else "by Huragok (U), after the game",
                "gray")]
    if extra.get("release"):
        program.append(("Newest", f"Project Reclaimer {extra['release']}", "gray"))
    if extra.get("program_note"):
        program.append(("", str(extra["program_note"]), "yellow"))
    sections.append(("Program", program))

    # Mods
    mods: list[Line] = []
    if server.workshop_items:
        mode = settings.workshop_install(server)
        shared = [s.name for s in settings.servers if s is not server and s.content_dir == server.content_dir]
        how = {"after_game": "new versions installed after the game",
               "when_empty": "new versions installed by the server once nobody's on",
               "off": "Workshop updates off"}[mode]
        if mode == "after_game" and shared:
            how = f"new versions installed by the server once nobody's on (content shared with {', '.join(shared)})"
        minutes = server.workshop_check_minutes or 60
        mods.append(("Workshop", f"{len(server.workshop_items)} item(s) · {how} · the server looks every "
                                 f"{minutes} min", None))
        for line in extra.get("workshop") or _receipt_lines(server):
            mods.append(("", line.strip(), "yellow" if "WAITING" in line else "gray"))
        steam = (data.get("workshop") or {}) if isinstance(data.get("workshop"), dict) else {}
        mods.append(("SteamCMD", f"as {steam['username']}" if steam.get("username") else
                     "no account set (I sets it up)", "gray" if steam.get("username") else "yellow"))
        if extra.get("workshop_disk") is not None:
            mods.append(("On disk", f"{size_text(extra['workshop_disk'])} (each item twice: installed, and "
                                    "SteamCMD's copy)", "gray"))
    else:
        mods.append(("Workshop", "no Workshop items", "gray"))
    mods.append(("Sharing", ("players download missing mods from it, " + f"{value('share_rate')} Mbit/s at most"
                             if value("share_mods") else "mods aren't shared from the server"), "gray"))
    if value("customization"):
        mods.append(("Characters", show(value("customization")), "gray"))
    sections.append(("Mods", mods))

    # Health
    health: list[Line] = [("Watchdog", str(row.get("health") or "healthy")
                           + (f": {'; '.join(row['health_reasons'])}" if row.get("health_reasons") else ""),
                           "yellow" if row.get("health") not in (None, "", "healthy") else None)]
    history = [h for h in row.get("history") or [] if time.time() - float(h[0]) < 86400]
    if history:
        for when, kind, what in history[-6:]:
            health.append(("", f"{time.strftime('%a %H:%M', time.localtime(float(when)))}  {KINDS.get(kind, kind)}: "
                               f"{what}", "red" if kind in ("crash", "faulted") else "gray"))
    else:
        health.append(("", "no restarts or crashes in the last 24 hours", "gray"))
    if row.get("fault_reason"):
        health.append(("FAULTED", str(row["fault_reason"]), "red"))
        if isinstance(row.get("retry_at"), (int, float)):
            health.append(("Next try", time.strftime("%a %H:%M", time.localtime(float(row["retry_at"])))
                           + " (the Superintendent tries once more; S starts it now)", "yellow"))
    elif row.get("fail_count"):
        health.append(("Failed starts", f"{row['fail_count']} in a row", "yellow"))
    sections.append(("Health", health))

    # Safety
    bans_path = (server.folder / str(data.get("bans") or "bans.json")).resolve()
    admins = len(value("admins", "folder") or []) + len(block.get("admins") or [])
    sections.append(("Safety", [
        ("Anti-cheat", str(value("anti_cheat")), None),
        ("VPN block", "on" + (f", lists: {show(value('lists', 'vpn'))}" if value("lists", "vpn") else "")
         if value("block_vpn") else "off", "gray"),
        ("Admins", f"{admins} in {server.config_path.name} (plus any added from the console)", "gray"),
        ("Bans", _bans(bans_path) + ("" if bans_path.parent == server.folder.resolve() else
                                     f" · shared: {bans_path}"), "gray"),
    ]))

    # Settings
    voice = [name for key, name in (("voice_global", "global"), ("voice_team", "team"), ("voice_proximity", "nearby"))
             if value(key)] if value("voice") else []
    co_op = "campaign" in block or "odst" in block  # the camera is for multiplayer games only
    camera = "(not for co-op)" if co_op else str(value("camera")).replace("_", " ")
    sections.append(("Settings", [
        ("Voice", ", ".join(voice) if voice else "off", None),
        ("Camera", camera, "gray"),
        ("", " · ".join([f"text chat {show(value('text_chat'))}", f"votes {show(value('votes'))}",
                         f"friendly fire {show(value('friendly_fire'))}", f"team changing {value('team_changing')}",
                         f"live reload {show(value('auto_reload', 'folder'))}"]), "gray"),
    ]))

    # Scheduled
    scheduled: list[Line] = [("Autostart", "on: it starts whenever Huragok starts" if server.autostart else "off",
                              None)]
    if row.get("daily"):
        scheduled.append(("Daily", str(row["daily"]), "gray"))
    if row.get("planned"):
        scheduled.append(("Planned", f"restart {row['planned']}", "yellow"))
    if extra.get("startup") is not None:
        scheduled.append(("At boot", "Huragok starts at boot" if extra["startup"] else "Huragok doesn't start at boot "
                                                                                       "(W)", "gray"))
    sections.append(("Scheduled", scheduled))

    # Problems
    problems = [*server.errors, *(extra.get("problems") or []), *(f for f in row.get("flags") or [] if f.startswith("!"))]
    if unreadable:
        problems.insert(0, unreadable)
    notes = [f for f in row.get("flags") or [] if not f.startswith("!")]
    lines: list[Line] = [("", problem.lstrip("! "), "red") for problem in problems]
    lines += [("", note, "yellow") for note in notes]
    sections.append(("Problems and notes", lines or [("", "none", "green")]))
    return sections


def _receipt_lines(server: ServerConfig) -> list[str]:
    """Each item's installed version, from its receipt, when no Workshop service is there to say more."""
    from .workshopupdates import date_text, installed_version

    lines = []
    for item in server.workshop_items:
        installed = installed_version(server.content_dir, item)
        lines.append(f"{item}   " + (f"installed {date_text(installed['updated'])}" if installed else
                                     "not installed yet"))
    return lines
