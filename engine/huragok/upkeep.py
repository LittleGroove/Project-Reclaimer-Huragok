"""Upkeep that isn't about one setting: disk space, the firewall, and Halo 3's engine file after an MCC update.

Disk: what piles up and is safe to remove, each with its size: program files an update set aside
(`project-reclaimer.exe.old`, records/previous-program/ but the newest), the old copies a Workshop update left
(.huragok-workshop-previous/; never one whose item is missing from the live folder, which may be the only copy
left), SteamCMD's copies of mods no server lists any more (data/workshop-cache), a content folder set aside when
a server got its own, and Huragok's own old versions (records/previous-*/ but the newest by version number, the
one records/last-update.json names kept too). Chat logs are never offered: they're the record of what was said.

Firewall: `dedicated check` prints the New-NetFirewallRule commands for the configured ports. Each rule gets a
name of its own (its protocol and ports) and is only added when it isn't there, so running them again does
nothing; Windows asks for an administrator first. Names go in single quotes (nothing in them is run) and the
script's outcome is checked: a declined prompt or a failed rule is said as such.

Engine: after a Steam update of MCC, `dedicated check` says to copy halo3.dll again once a Project Reclaimer
release supports that MCC (and from 0.9.19 the start of an ODST server says the same of halo3odst.dll). The old
file is renamed first (a running server holds it), then the new one is copied in; the servers pick it up as they
restart. A game folder with Halo 3: ODST's files gets both. A game folder that is MCC's own (a link to it) is
left alone: the file is the same one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .statuscard import folder_size, size_text

if TYPE_CHECKING:
    from .config import Settings

FIREWALL = re.compile(r'New-NetFirewallRule -DisplayName "(?P<name>[^"]+)" (?P<rest>.*?-Protocol (?P<protocol>\w+) '
                      r'-LocalPort (?P<ports>\d+(?:[-,]\d+)*).*)$')
ENGINE_MISMATCH = "Copy halo3.dll again"
ODST_ENGINE_MISMATCH = "halo3odst.dll is not the ODST build"
ODST_FILES_MISSING = "Halo 3: ODST's files are not in the game folder"
CRLF = chr(13) + chr(10)  # PowerShell scripts end lines the Windows way


@dataclass
class Leftover:
    label: str
    path: Path
    size: int
    why: str

    @property
    def size_text(self) -> str:
        return size_text(self.size)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else folder_size(path)
    except OSError:
        return 0


def _newest_kept(folders: list[Path]) -> list[Path]:
    """All but the newest (by name: they're dated)."""
    return sorted(folders, key=lambda p: p.name)[:-1]


VERSIONED = re.compile(r"^previous-v?(\d+(?:\.\d+)*)(.*)$")


def _backup_order(path: Path) -> tuple[int, tuple[int, ...], int, str]:
    """records/previous-<version>[-<date>]: by version number (0.2.10 after 0.2.9), the plain name (the newest
    backup of a version) after a dated one; names without a version first, so they're never the one kept."""
    match = VERSIONED.match(path.name)
    if not match:
        return (0, (), 0, path.name)
    return (1, tuple(int(n) for n in match.group(1).split(".")), 0 if match.group(2) else 1, match.group(2))


def _huragok_backups(settings: Settings) -> list[Path]:
    """Huragok's own older versions to offer: all but the newest, and never the one the last update made."""
    folders = sorted((p for p in settings.records.glob("previous-*") if p.is_dir() and p.name != "previous-program"),
                     key=_backup_order)
    try:
        last = json.loads((settings.records / "last-update.json").read_text(encoding="utf-8")).get("backup")
    except (OSError, ValueError, AttributeError):
        last = None
    return [p for p in folders[:-1] if p.name != last]


def _workshop_copies(settings: Settings) -> list[Leftover]:
    """Old copies in each .huragok-workshop-previous/: only those whose item is still in the live folder (one
    whose item is missing may be the only copy left: Huragok puts it back), never one a job is using."""
    from .workshopupdates import ASIDE, ASIDE_NAME, MARKER, RECEIPT, kept_by_markers

    found: list[Leftover] = []
    roots: dict[Path, list] = {}
    for server in settings.servers:
        if server.content_dir is not None:
            roots.setdefault(server.content_dir.parent / ASIDE, []).append(server)
    for aside, servers in roots.items():
        if not aside.is_dir():
            continue
        busy = kept_by_markers(aside)
        for old in sorted(aside.iterdir()):
            if old.name.endswith(MARKER) or old in busy:
                continue
            match = ASIDE_NAME.search(old.name)
            if match and not match.group("failed"):
                owners = [s for s in servers if old.name.startswith(f"{s.slug}-")] or servers
                if not any((s.content_dir / "Mods" / "Workshop" / match.group("item") / RECEIPT).is_file()
                           for s in owners):
                    continue  # no whole copy in the live folder: this may be the only one
            why = "a partial new copy a Workshop update set aside" if match and match.group("failed") else \
                "a mod's old copy a Workshop update left"
            found.append(Leftover(f"{aside.parent.name}/{aside.name}/{old.name}", old, _size(old), why))
    return found


def cleanable(settings: Settings) -> list[Leftover]:
    found: list[Leftover] = []
    folders = [settings.home, *settings.server_folders]
    for folder in folders:
        for old in folder.glob("*.exe.old"):
            found.append(Leftover(f"{old.parent.name}/{old.name}", old, _size(old),
                                  "the program before its last update (still in use until that server restarts)"))
        for unused in folder.glob("content-unused-*"):
            found.append(Leftover(f"{folder.name}/{unused.name}", unused, _size(unused),
                                  "a content folder set aside when the server got its own"))
    previous = settings.records / "previous-program"
    if previous.is_dir():
        for old in _newest_kept([p for p in previous.iterdir() if p.is_dir()]):
            found.append(Leftover(f"records/previous-program/{old.name}", old, _size(old),
                                  "server program copies Huragok replaced (the newest set is kept)"))
    for old in _huragok_backups(settings):
        found.append(Leftover(f"records/{old.name}", old, _size(old), "an older Huragok (the newest is kept)"))
    found.extend(_workshop_copies(settings))
    listed: dict[Path, set[str]] = {}
    for server in settings.servers:
        listed.setdefault(server.folder, set()).update(str(i) for i in server.workshop_items)
    for folder, items in listed.items():
        cache = folder / "data" / "workshop-cache" / "steamapps" / "workshop" / "content" / "976730"
        if not cache.is_dir():
            continue
        for item in cache.iterdir():
            if item.is_dir() and item.name not in items:
                found.append(Leftover(f"{folder.name}: SteamCMD's copy of {item.name}", item, _size(item),
                                      "a Workshop item no server in this folder lists any more"))
    return found


def remove(leftovers: list[Leftover]) -> tuple[list[str], list[str]]:
    """Delete them: (what went, what couldn't, with why)."""
    gone, kept = [], []
    for leftover in leftovers:
        try:
            if leftover.path.is_dir():
                shutil.rmtree(leftover.path)
            else:
                leftover.path.unlink()
            gone.append(f"{leftover.label} ({leftover.size_text})")
        except OSError as error:
            kept.append(f"{leftover.label}: {error.strerror or error}")
    return gone, kept


def quoted(text: str) -> str:
    """A PowerShell single-quoted string: taken as it is ($(…) and "…" inside are never run), ' doubled."""
    return "'" + text.replace("'", "''") + "'"


def firewall_commands(check_text: str) -> list[str]:
    """`dedicated check`'s firewall commands, each under a name of its own and only added when missing."""
    commands = []
    for line in check_text.splitlines():
        match = FIREWALL.search(line.strip())
        if not match:
            continue
        name = quoted(f"{match.group('name')} {match.group('protocol')} {match.group('ports').strip()}")
        command = f"New-NetFirewallRule -DisplayName {name} {match.group('rest')}"
        commands.append(f"if (-not (Get-NetFirewallRule -DisplayName {name} -ErrorAction SilentlyContinue)) "
                        f"{{ {command} | Out-Null }}")
    return commands


def firewall_script(commands: list[str]) -> str:
    """The script the administrator PowerShell runs: stops at the first rule that fails, and says so in its exit
    code (0: every rule is there)."""
    body = CRLF.join(f"  {command}" for command in commands)
    return (f"$ErrorActionPreference = 'Stop'{CRLF}try {{{CRLF}{body}{CRLF}}} catch {{{CRLF}"
            f"  Write-Error $_{CRLF}  exit 1{CRLF}}}{CRLF}exit 0{CRLF}")


def firewall_command(script: Path) -> list[str]:
    """How the script is run as an administrator (Windows asks first), its exit code passed on: 0 done, 1 a rule
    failed; a declined prompt makes Start-Process fail, so 1 too. The path is quoted for both PowerShells."""
    inner = f'"{script}"'  # the elevated PowerShell's -File argument, in its own double quotes (spaces)
    return ["powershell", "-NoProfile", "-Command",
            "$ErrorActionPreference = 'Stop'; $p = Start-Process powershell -Verb RunAs -Wait -PassThru -ArgumentList "
            f"'-NoProfile','-ExecutionPolicy','Bypass','-File',{quoted(inner)}; exit $p.ExitCode"]


def open_firewall(commands: list[str], folder: Path) -> str:
    """Run them in an administrator PowerShell (Windows asks first), from a script in `folder` (records/), so no
    quoting can mangle them. Returns what happened, for the screen."""
    if os.name != "nt":
        return "the firewall commands are for Windows; on Linux open the same ports with ufw or firewalld"
    if not commands:
        return "nothing to open"
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "open-firewall.ps1"
    script.write_text(firewall_script(commands), encoding="utf-8")
    try:
        result = subprocess.run(firewall_command(script), timeout=600, capture_output=True, text=True, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"couldn't ask for an administrator: {error}"
    if result.returncode != 0:
        said = " ".join((result.stderr or result.stdout or "").split())[:200]
        return (f"Windows Firewall wasn't changed: the administrator prompt was declined, or a rule failed (exit code "
                f"{result.returncode}{': ' + said if said else ''}). To do it by hand, run {script} in an "
                "administrator PowerShell")
    return f"the ports are open in Windows Firewall ({script.name})"


def engine_out_of_date(check_text: str) -> bool:
    return ENGINE_MISMATCH in check_text or ODST_ENGINE_MISMATCH in check_text


def odst_files_missing(check_text: str) -> bool:
    return ODST_FILES_MISSING in check_text


def engine_files(game_dir: Path) -> list[str]:
    """The engine files a game folder holds: halo3.dll, and halo3odst.dll when it has Halo 3: ODST's files."""
    files = ["halo3/halo3.dll"]
    if (game_dir / "halo3odst").is_dir():
        files.append("halo3odst/halo3odst.dll")
    return files


def servers_using(settings: Settings, game_dir: Path) -> list[str]:
    """The servers whose game folder (`game` in their dedicated.toml) is `game_dir`: copy_engine changes theirs."""
    from .tomledit import read_toml

    try:
        wanted = game_dir.resolve()
    except OSError:
        wanted = game_dir
    names = []
    for server in settings.servers:
        try:
            game = str(read_toml(server.config_path).get("game") or "game")
            if (server.folder / game).resolve() == wanted:
                names.append(server.name)
        except (OSError, ValueError):
            continue
    return names


def _same_file(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def copy_engine(game_dir: Path, mcc_dir: Path, servers: list[str] | None = None) -> str:
    """MCC's engine files into the server's game folder; each old one is renamed (a running server holds it).
    A game folder that is MCC's own (a junction to it) has the very same file: nothing is done to it. `servers`:
    every server using that game folder, so the screen can name them."""
    stamp = time.strftime('%Y%m%d-%H%M%S')
    copied: list[str] = []
    same: list[str] = []
    for relative in engine_files(game_dir):
        source, target = mcc_dir / relative, game_dir / relative
        if not source.is_file():
            if relative.startswith("halo3/"):
                raise FileNotFoundError(f"{source} isn't there: is that your MCC folder?")
            continue  # ODST isn't installed in this MCC: its file stays as it is
        if _same_file(source, target):
            same.append(target.name)  # renaming it would take MCC's own file away
            continue
        if target.exists():
            os.replace(target, target.with_name(f"{target.name}.before-{stamp}"))
        shutil.copy2(source, target)
        copied.append(f"{source} -> {target}")
    several = bool(servers) and len(servers or []) > 1
    who = f"{', '.join(servers)} pick" if servers else "each server using it picks"
    said = [f"copied {'; '.join(copied)}; {who} it up as {'they restart' if several else 'it restarts'}"] \
        if copied else []
    if same:
        said.append(f"{' and '.join(same)} in {game_dir} is MCC's own file (the game folder links to MCC), so it is "
                    "already the one MCC has: nothing to copy")
    return "; ".join(said)
