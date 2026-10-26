"""Is a mod good to go? What the server program would skip, refuse or trip over in each mod of a
content folder, found from the files before a game needs them.

The program says little, and only when a game or a start reaches the fault: a mod made for
another game is ignored without a word, a map whose description it can't pair is skipped at
every start ("Map needs matching .mapinfo metadata"), two maps with one map ID can't share a
session, a Workshop item missing a file is "incomplete". check_mods() reads content/Mods the
way the program does and says, for each mod, what's wrong and what to do; a mod with nothing
to say is good to go.

What it reads, and where the rule comes from (0.9.21, 20 October 2026):

- A package is a folder with a ModInfo.json (anywhere under Mods, as the program finds them),
  or a folder under Mods (or Mods/Workshop) holding .map files without one: a single map, which
  the program plays with or without a .mapinfo (host guide, Mods).
- ModInfo.json: readable; Engine "Halo3" (others are skipped silently: tried with `dedicated
  content`); in Mods/Workshop/<item>, HostedModIds naming that item (else the program refuses to
  install it: "ModInfo.json names a different Workshop item"); every MultiplayerMaps file there.
- Pairing: a package's .map (in maps/ or its own folder; shared, campaign and mainmenu aside) is
  used only when a MultiplayerMaps description's ScenarioFile ends in the map's file name, in
  any case: tried with `dedicated content` (a file name off by "-old", a description naming the
  scenario's path instead, one not listed: each skipped). Campaign mods (CampaignInfo.json) pair
  their own way and aren't paired here.
- Each map: halo3_mapfile's checks (a map the program can't read), a multiplayer scenario, and its
  map ID for the clashes below.
- A Workshop item's reclaimer-installed.json: every file it lists there, at its size (else the
  program says the installation "is incomplete").
- The host guide's limits: 8 maps and 512 files a mod; a map up to 4 GB, a sound bank or movie
  2 GB, an image 8 MB.
- Forge layouts it ships (Project Reclaimer Layouts): readable, their base map installed, at the
  map ID they were made on.
- customization.json (character mods): readable, and all 27 of Halo 3's multiplayer maps in the
  game folder, which the program needs to make their packs.
- Across mods: two maps with one map ID, or one file name in two mods (they can't share a session
  or a playlist).

Maps are read through halo3_mapfile (a few hundred KB each) and remembered by path, size and time, so
looking again costs nothing while nothing changed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.text import size_text
from ..games import halo3_mapfile
from ..games.halo3 import HALO3_MAPS

BROKEN = "broken"  # the server won't use it, or part of it
WARNING = "warning"  # trouble in some cases, or work for the host
NOTE = "note"

LAYOUTS = "Project Reclaimer Layouts"
INSTALLED = "reclaimer-installed.json"  # the program's list of a Workshop item's files, with their sizes
NOT_MAPS = {"shared", "campaign", "mainmenu"}
MAX_MAPS, MAX_FILES = 8, 512
MAX_MAP, MAX_MEDIA, MAX_IMAGE = 4 << 30, 2 << 30, 8 << 20
MEDIA = {".fsb", ".bik"}
IMAGES = {".png", ".jpg", ".jpeg", ".bmp"}


@dataclass(frozen=True)
class Finding:
    level: str  # BROKEN, WARNING or NOTE
    text: str


@dataclass
class ModReport:
    folder: Path
    title: str
    workshop: int | None = None
    uploaded: int | None = None  # the installed Workshop version (Unix seconds)
    maps: list[str] = field(default_factory=list)  # the maps the server plays from it (file names without .map)
    findings: list[Finding] = field(default_factory=list)
    disabled: bool = False

    @property
    def good(self) -> bool:
        return not any(f.level in (BROKEN, WARNING) for f in self.findings)

    @property
    def broken(self) -> bool:
        return any(f.level == BROKEN for f in self.findings)

    @property
    def name(self) -> str:
        """'HugeGrass Rebalance (Workshop 3820687243, uploaded 20 Oct 2026 05:57 UTC)'."""
        bits = [f"Workshop {self.workshop}"] if self.workshop else []
        if self.uploaded:
            bits.append("uploaded " + time.strftime("%d %b %Y %H:%M UTC", time.gmtime(self.uploaded)))
        return f"{self.title} ({', '.join(bits)})" if bits else self.title

    def summary(self) -> str:
        if self.disabled:
            state = "switched off (disabled-mods.json)"
        elif self.good:
            state = "good to go"
        else:
            problems = sum(1 for f in self.findings if f.level in (BROKEN, WARNING))
            state = f"{problems} problem{'s' if problems != 1 else ''}"
        maps = f" · {len(self.maps)} map{'s' if len(self.maps) != 1 else ''}" if self.maps else ""
        return f"{self.name}: {state}{maps}"

    def fingerprint(self) -> tuple[Any, ...]:
        """What changes when the report does (a new version, a fault found or put right)."""
        return self.uploaded, self.disabled, tuple(self.findings)

    def add(self, level: str, text: str) -> None:
        self.findings.append(Finding(level, text))


# Maps already read: (path, size, modified) -> verdict. A map is read again only when it changes.
_seen: dict[tuple[str, int, int], halo3_mapfile.Verdict] = {}
_seen_lock = threading.Lock()


def map_verdict(path: Path) -> halo3_mapfile.Verdict:
    """halo3_mapfile.check, remembered while the file stays as it is. OSError: it couldn't be read."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _seen_lock:
        if key in _seen:
            return _seen[key]
    verdict = halo3_mapfile.check(path)
    with _seen_lock:
        if len(_seen) > 512:
            _seen.clear()
        _seen[key] = verdict
    return verdict


def check_mods(content_root: Path, game: Path | None = None) -> list[ModReport]:
    """Every mod in content_root/Mods, each with what's wrong in it (nothing: good to go). game: the server's game
    folder, for what character mods need from it."""
    root = content_root / "Mods"
    if not root.is_dir():
        return []
    disabled = _disabled(content_root)
    reports: list[ModReport] = []
    ids: list[tuple[int, str, ModReport]] = []  # (map ID, map file name, its mod) for the clashes
    for folder in packages(root):
        report = _check_package(folder, content_root, disabled, ids)
        reports.append(report)
    _check_across(reports, ids)
    _check_layouts(reports, ids)
    if game is not None and any((r.folder / "customization.json").is_file() for r in reports if not r.disabled):
        missing = sorted(stem for stem in HALO3_MAPS if not (game / "halo3" / "maps" / f"{stem}.map").is_file())
        if missing:
            for report in reports:
                if (report.folder / "customization.json").is_file() and not report.disabled:
                    report.add(WARNING, f"character mods need all 27 of Halo 3's multiplayer maps in the game folder "
                                        f"to make their packs; {len(missing)} are missing ({', '.join(missing[:6])}"
                                        f"{'…' if len(missing) > 6 else ''}), so players see Halo 3's own characters "
                                        "in their place")
    return reports


def packages(root: Path) -> list[Path]:
    """The mod folders under Mods: each folder with a ModInfo.json, and each folder of Mods (or Mods/Workshop)
    holding .map files with no ModInfo.json in or above it."""
    found = sorted({info.parent for info in root.rglob("ModInfo.json") if info.is_file()})
    tops = [p for p in sorted(root.iterdir()) if p.is_dir()]
    for top in list(tops):
        if top.name.lower() == "workshop":
            tops.remove(top)
            tops.extend(p for p in sorted(top.iterdir()) if p.is_dir())
    for top in tops:
        if any(top == f or _inside(f, top) or _inside(top, f) for f in found):
            continue
        if _maps_in(top):
            found.append(top)
    return sorted(found)


def _check_package(folder: Path, content_root: Path, disabled: list[str], ids: list[tuple[int, str, ModReport]]
                   ) -> ModReport:
    workshop = int(folder.name) if folder.parent.name.lower() == "workshop" and folder.name.isdecimal() else None
    report = ModReport(folder, folder.name, workshop)
    try:
        package = folder.relative_to(content_root).as_posix()
    except ValueError:
        package = folder.as_posix()
    report.disabled = any(p.strip().strip("/").replace("\\", "/").lower() == package.lower() for p in disabled)
    report.uploaded = _uploaded(folder)
    info_path = folder / "ModInfo.json"
    maps = _maps_in(folder)
    campaign = (folder / "CampaignInfo.json").is_file()
    described: dict[str, str] = {}  # a map file name (lower case) -> the description naming it
    if info_path.is_file():
        info = _json(info_path)
        if not isinstance(info, dict):
            report.add(BROKEN, f"its ModInfo.json can't be read ({info if isinstance(info, str) else 'not an object'}"
                               "): the server can't read it either; ask its author")
            return report
        report.title = _neutral(info.get("Title")) or folder.name
        identifier = info.get("ModIdentifier") if isinstance(info.get("ModIdentifier"), dict) else {}
        hosted = identifier.get("HostedModIds") if isinstance(identifier.get("HostedModIds"), dict) else {}
        named = _number(hosted.get("SteamWorkshopId"))
        if workshop and named and named != workshop:
            report.add(WARNING, f"its ModInfo.json names Workshop item {named}, not {workshop} (this folder's): the "
                                "server refuses to install or update it (\"ModInfo.json names a different Workshop "
                                "item\"); the item's own packaging, ask its author")
        report.workshop = report.workshop or named
        engine = info.get("Engine")
        if isinstance(engine, str) and engine.strip().lower() not in ("halo3", ""):
            report.add(BROKEN, f"it is made for {engine}, not Halo 3: the server ignores it without a word")
            return report
        contents = info.get("GameModContents") if isinstance(info.get("GameModContents"), dict) else {}
        campaign = campaign or contents.get("HasCampaign") is True
        listed = contents.get("MultiplayerMaps")
        for relative in listed if isinstance(listed, list) else []:
            path = folder / str(relative).replace("\\", "/")
            description = _json(path)
            if description is None:
                report.add(WARNING, f"ModInfo.json lists {relative}, which isn't in the mod")
                continue
            scenario = description.get("ScenarioFile") if isinstance(description, dict) else None
            if not isinstance(scenario, str) or not scenario.strip():
                problem = description if isinstance(description, str) else "it names no ScenarioFile"
                report.add(WARNING, f"the map description {relative} can't be used ({problem})")
                continue
            stem = re.split(r"[\\/]", scenario.strip())[-1]
            described[stem.lower()] = str(relative)
            if not any(m.stem.lower() == stem.lower() for m in maps) and not campaign:
                report.add(WARNING, f"{relative} describes {stem}.map, which isn't in the mod, so that map can't be "
                                    "played")
        if not campaign:
            paired = [m.stem for m in maps if m.stem.lower() in described]
            for path in maps:
                if path.stem.lower() in described:
                    continue
                older = next((p for p in paired if path.stem.lower().startswith(p.lower())), None)
                size = _size_text(path)
                if older:
                    report.add(WARNING, f"{path.name} looks like an old copy of {older}.map: the server skips it at "
                                        f"every start (\"Map needs matching .mapinfo metadata\"); delete it to free "
                                        f"{size}")
                else:
                    report.add(BROKEN, f"{path.name} is skipped by the server (\"Map needs matching .mapinfo "
                                       f"metadata\"): none of the descriptions ModInfo.json lists (MultiplayerMaps) "
                                       f"has a ScenarioFile ending in {path.stem}; the item's own packaging, ask its "
                                       "author")
            maps = [m for m in maps if m.stem.lower() in described]
    for path in maps:
        _check_map(report, path, campaign, ids)
    _check_installed(report, folder)
    _check_limits(report, folder, maps)
    customization = folder / "customization.json"
    if customization.is_file() and not isinstance(_json(customization), dict):
        report.add(BROKEN, "its customization.json can't be read: its characters are left out")
    return report


def _check_map(report: ModReport, path: Path, campaign: bool, ids: list[tuple[int, str, ModReport]]) -> None:
    try:
        verdict = map_verdict(path)
    except OSError as error:
        report.add(BROKEN, f"{path.name} can't be read: {error.strerror or error}")
        return
    if verdict.reason:
        report.add(BROKEN, f"{path.name} is skipped by the server ({verdict.reason}): {verdict.detail}; the map file "
                           "itself is broken, ask its author for a fixed upload")
        return
    report.maps.append(path.stem)
    if not campaign and verdict.map_type not in (None, 1):
        kind = {0: "a campaign", 2: "a main menu"}.get(verdict.map_type, f"a type {verdict.map_type}")
        report.add(WARNING, f"{path.name} holds {kind} scenario, not a multiplayer one, so it can't be played in "
                            "multiplayer games")
    if verdict.map_id:
        ids.append((verdict.map_id, path.stem, report))


def _check_installed(report: ModReport, folder: Path) -> None:
    """A Workshop item against the program's list of its files (reclaimer-installed.json)."""
    path = folder / INSTALLED
    if not path.is_file():
        if report.workshop and folder.parent.name.lower() == "workshop":
            report.add(WARNING, f"it has no installation receipt ({INSTALLED}): copied in by hand? The server can't "
                                "update it as it is (\"exists without an installation receipt\"): move the folder "
                                "aside and fetch the item")
        return
    data = _json(path)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        report.add(WARNING, f"its {INSTALLED} can't be read: the server will want to fetch it again")
        return
    gone: list[str] = []
    for entry in files:
        if not (isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str)):
            continue
        name, size = entry
        try:
            if (folder / name).stat().st_size != size:
                gone.append(f"{name} (another size)")
        except OSError:
            gone.append(f"{name} (missing)")
    if gone:
        report.add(BROKEN, f"its Workshop installation is incomplete: {len(gone)} of {len(files)} files are missing "
                           f"or changed ({', '.join(gone[:3])}{'…' if len(gone) > 3 else ''}); the server won't use it "
                           "like this: fetch the item again")


def _check_limits(report: ModReport, folder: Path, maps: list[Path]) -> None:
    if len(maps) > MAX_MAPS:
        report.add(WARNING, f"it has {len(maps)} maps; a mod can hold {MAX_MAPS}, so players can't get it from the "
                            "server")
    try:
        files = [p for p in folder.rglob("*") if p.is_file() and not p.name.startswith("reclaimer-")]
    except OSError:
        return
    if len(files) > MAX_FILES:
        report.add(WARNING, f"it has {len(files)} files; a mod can hold {MAX_FILES}, so players can't get it from the "
                            "server")
    for path in files:
        suffix = path.suffix.lower()
        limit = MAX_MAP if suffix == ".map" else MAX_MEDIA if suffix in MEDIA else MAX_IMAGE if suffix in IMAGES else 0
        try:
            if limit and path.stat().st_size > limit:
                report.add(WARNING, f"{path.relative_to(folder).as_posix()} is {_size_text(path)}, over the "
                                    f"{size_text(limit, 1)} a mod's {'map' if suffix == '.map' else 'file of its kind'} can "
                                    "be: players can't get it from the server")
        except OSError:
            continue


def _check_across(reports: list[ModReport], ids: list[tuple[int, str, ModReport]]) -> None:
    """Two maps with one map ID, or one file name in two mods: they can't share a session or a playlist."""
    live = [entry for entry in ids if not entry[2].disabled]
    for number, (map_id, stem, report) in enumerate(live):
        for other_id, other_stem, other in live[number + 1:]:
            if other is report and other_stem == stem:
                continue
            if map_id == other_id:
                text = (f"{stem}.map and {other_stem}.map{'' if other is report else f' ({other.name})'} share map "
                        f"ID {map_id}: they can't both be played in one session or playlist (the server leaves one "
                        "out)")
                report.add(WARNING, text)
                if other is not report:
                    other.add(WARNING, f"{other_stem}.map and {stem}.map ({report.name}) share map ID {map_id}: they "
                                       "can't both be played in one session or playlist (the server leaves one out)")
    by_stem: dict[str, list[ModReport]] = {}
    for report in reports:
        if not report.disabled:
            for stem in report.maps:
                by_stem.setdefault(stem.lower(), []).append(report)
    for stem, owners in by_stem.items():
        if len(owners) > 1:
            for report in owners:
                others = ", ".join(o.name for o in owners if o is not report)
                report.add(NOTE, f"{stem}.map is also in {others}: two mods' maps with one file name can't be in one "
                                 "playlist, and a playlist names this one as workshop:<item>/<map>")


def _check_layouts(reports: list[ModReport], ids: list[tuple[int, str, ModReport]]) -> None:
    """The Forge layouts a mod ships: readable, their base map installed, at the map ID they were made on."""
    installed = {stem.lower(): stem for report in reports if not report.disabled for stem in report.maps}
    map_ids = {stem.lower(): map_id for map_id, stem, report in ids if not report.disabled}
    for report in reports:
        folder = report.folder / LAYOUTS
        if report.disabled or not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            layout = _json(path)
            if not isinstance(layout, dict):
                report.add(WARNING, f"the Forge layout {path.name} can't be read")
                continue
            name = layout.get("name") if isinstance(layout.get("name"), str) else path.stem
            base = layout.get("base_map") if isinstance(layout.get("base_map"), dict) else {}
            stem, made_on = base.get("stem"), _number(base.get("id"))
            if not isinstance(stem, str) or not stem.strip() or stem.lower() in HALO3_MAPS:
                continue
            if stem.lower() not in installed:
                report.add(WARNING, f"the Forge layout \"{name}\" is made on {stem}.map, which isn't installed, so it "
                                    "can't be played")
            elif made_on and map_ids.get(stem.lower()) and map_ids[stem.lower()] != made_on:
                report.add(WARNING, f"the Forge layout \"{name}\" was made on another version of {stem}.map (map ID "
                                    f"{made_on}; the installed one is {map_ids[stem.lower()]}), so it may not load")


def _maps_in(folder: Path) -> list[Path]:
    """A package's maps: in its maps folder and its own (shared, campaign and mainmenu aside)."""
    try:
        found = [*folder.glob("*.map"), *(folder / "maps").glob("*.map")]
    except OSError:
        return []
    return sorted(p for p in found if p.is_file() and p.stem.lower() not in NOT_MAPS)


def _uploaded(folder: Path) -> int | None:
    data = _json(folder / halo3_mapfile.RECEIPT)
    updated = data.get("updated") if isinstance(data, dict) else None
    return updated if isinstance(updated, int) and not isinstance(updated, bool) else None


def _disabled(content_root: Path) -> list[str]:
    data = _json(content_root / "disabled-mods.json")
    packages = data.get("packages") if isinstance(data, dict) else None
    return [p for p in packages if isinstance(p, str)] if isinstance(packages, list) else []


def _json(path: Path) -> Any:
    """The file's JSON; None when there's no such file; the problem as text when it can't be read."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        return getattr(error, "strerror", None) or str(error)
    except ValueError as error:
        return f"line {error.lineno}: {error.msg}" if isinstance(error, json.JSONDecodeError) else str(error)


def _neutral(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("Neutral")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value or None
    return int(value) if isinstance(value, str) and value.strip().isdecimal() else None


def _inside(path: Path, folder: Path) -> bool:
    return folder in path.parents


def _size_text(path: Path) -> str:
    try:
        return size_text(path.stat().st_size, 1)
    except OSError:
        return "?"


