"""`check`: the settings, the servers, the commands and roles, the mods and what the next start would change,
without running anything. Everything it finds is said with what to do."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..core import lock
from ..core.serverfolders import ServerConfig
from ..core.settings import COMMANDS_FILE, SETTINGS_FILE, Settings
from ..games.halo3 import old_labels, playlist_files
from . import common, firstrun
from .common import LAUNCHER, SETTINGS_PROBLEM
from .shell import console as out


def _check_mods(settings: Settings) -> None:
    """Each content folder's mods, good to go or with what's wrong (modcheck.py). Warnings: a broken mod doesn't stop
    a server, it leaves the mod (or one of its maps) out."""
    from ..content import modcheck
    from ..content.gamefiles import read_paths

    roots: dict[Path, tuple[list[str], Path | None]] = {}
    for server in settings.servers:
        paths = read_paths(server.folder, settings.config_name)
        root = server.content_dir or (paths.content if paths else None)
        if root is not None:
            roots.setdefault(root, ([], paths.game if paths else None))[0].append(server.name)
    for root, (names, game) in roots.items():
        try:
            reports = modcheck.check_mods(root, game)
        except OSError as error:
            out.heading(f"Mods ({', '.join(names)})")
            out.warn(f"couldn't look at them: {error}")
            continue
        if not reports:
            continue
        out.heading(f"Mods ({', '.join(names)}: {root / 'Mods'})")
        for report in reports:
            if report.good or report.disabled:
                out.say(report.summary())
            else:
                out.warn(report.summary())
            for finding in [] if report.disabled else report.findings:
                (out.say if finding.level == modcheck.NOTE else out.warn)(f"  {finding.level}: {finding.text}")


def _check_content_files(settings: Settings, move: bool) -> None:
    """Variant files in each content folder that the server can't read: named, and moved aside with --set-aside."""
    from ..content.catalogue import SET_ASIDE, set_aside, unreadable_variants

    roots: dict[Path, list[str]] = {}
    for server in settings.servers:
        if server.content_dir is not None:
            roots.setdefault(server.content_dir, []).append(server.name)
    for root, names in roots.items():
        bad = unreadable_variants(root)
        if not bad:
            continue
        out.heading(f"Content the server can't read ({', '.join(names)}: {root})")
        for path in bad:
            out.warn(f"{path.relative_to(root).as_posix()}: not an MCC variant file; the server skips it at every start")
        aside = root.parent / SET_ASIDE
        if move:
            moved, kept = set_aside(root, bad)
            out.ok(f"moved {len(moved)} file(s) to {aside} (nothing deleted)")
            for line in kept:
                out.warn(f"couldn't move {line}: close what holds it, and run it again")
        else:
            out.say(f"{LAUNCHER} check --set-aside moves them to {aside} (nothing is deleted)")


def cmd_check(args: argparse.Namespace) -> int:
    from ..core.events import EventBus
    from ..core.part import commands_of
    from ..parts import PARTS, config_problem
    from ..core.commandsfile import ROLES, TITLES
    from ..core.commandsfile import all_commands
    from ..core.priority import unavailable as unavailable_priority
    from ..servers.state import ServerState

    settings = common.load_settings(args.config)
    ok = True
    warned = bool(settings.retired or settings.notes)
    out.title("CHECK", f"Huragok {__version__}{update_note(settings)}")
    fields: list[tuple[str, str]] = [("Folder", str(settings.home))]
    if settings.servers_folder:
        fields.append(("Servers", f"{settings.servers_folder}"
                                  f"{'' if settings.servers_folder.is_dir() else '  (not there yet)'}"))
    if settings.single_folder:
        fields.append(("Server folder", str(settings.single_folder)))
    fields.append(("Backend", settings.backend))
    if pid := lock.running_supervisor(settings):
        fields.append(("Running", f"yes (process {pid})"))
    out.fields(fields)
    backend = common.backend_for(settings, EventBus(), {s.name: ServerState(s.name) for s in settings.servers})
    if backend is None:
        return SETTINGS_PROBLEM
    if not settings.servers:
        out.heading("Servers")
        out.warn("none found")
        for line in firstrun.layout_help(settings.home):
            out.say(line)
        ok = False
    for server in settings.servers:
        endpoint = server.rcon_endpoint()
        status_age = None
        try:
            updated = json.loads(server.status_path.read_text(encoding="utf-8")).get("updated")
            status_age = time.time() - float(updated)
        except (OSError, ValueError, TypeError):
            pass
        out.heading(server.name)
        rows: list[tuple[str, str]] = [
            ("folder", str(server.folder)),
            ("data", f"{server.data_dir}{'' if server.data_dir.is_dir() else '  (not there yet)'}"),
            ("port", f"{server.game_port} · autostart {'yes' if server.autostart else 'no'}"
                     + (f" · daily restart {server.daily_restart}" if server.daily_restart else "")),
        ]
        problems = [error for error in server.errors if "RCON" not in error] + _backend_problems(backend, server)
        from ..content.gamefiles import ban_list_problems, read_paths

        if (paths := read_paths(server.folder, settings.config_name)) is not None:
            problems += [f"its ban list won't be read by the server program, and it won't start: {problem}"
                         for problem in ban_list_problems(paths.bans)[:5]]
        if endpoint:
            rows.append(("RCON", f"{endpoint.host}:{endpoint.port} · password set · host key "
                                 f"{'found' if server.host_key() else 'not found (server not started?)'}"))
        else:
            rows.append(("RCON", "off"))
            problems.insert(0, "RCON is off: set [rcon] password in dedicated.toml")
        rows.append(("updates", updates_text(settings, server)))
        priority_warning = ""
        if settings.backend == "process":
            level = settings.of(server).priority
            rows.append(("priority", ("off (left as it starts)" if level == "off" else level)
                         + (" (its own)" if server.priority else "")))
            if why := unavailable_priority(level):
                priority_warning = f"warning: priority {level}: {why}"
        if status_age is None:
            rows.append(("running", "unknown (no status.json)"))
        else:
            rows.append(("running", f"{'yes' if status_age < 30 else 'no'} (status.json written {status_age:.0f} s ago)"))
        out.fields(rows)
        for problem in problems:
            out.warn(f"PROBLEM  {problem}")
            ok = False
        if priority_warning:
            out.warn(priority_warning)
        for line in _label_warnings(settings, server):
            out.warn(line)
    _check_mods(settings)
    _check_content_files(settings, bool(getattr(args, "set_aside", False)))
    if settings.problems:
        out.heading("Left out")
        for problem in settings.problems:
            out.warn(f"PROBLEM  {problem}")
        ok = False
    if settings.retired:
        out.heading("Settings that do nothing")
        for line in settings.retired:
            out.warn(line)
    if settings.notes:
        out.heading("Values from an older version")
        for line in settings.notes:
            out.warn(line)
    from ..updates.hostfiles import pending

    upcoming = pending(settings.path, settings.records, settings.packages_folder)
    if upcoming:
        out.heading("Brought up to date at the next start")
        for line in upcoming:
            out.say(line)

    out.heading("Parts")
    rows = []
    notes: list[tuple[str, bool]] = []  # (line, is a problem)
    for name, cls in PARTS.items():
        part = settings.parts[name]
        if cls.always_on:
            continue
        state = ("on" + (", dry run" if part.dry_run or settings.dry_run else "")) if part.enabled else "off"
        rows.append((name, state, cls.description))
        if not part.enabled:
            continue
        try:
            warnings = cls.preflight(part.options, settings.home)
        except ValueError as error:
            notes.append((f"PROBLEM  [{name}] {error}", True))
            continue
        # The keys that aren't settings are named above, with what they were probably meant to be.
        notes.extend((f"{name}: {warning}", False) for warning in warnings if not _named_above(name, warning))
        if problem := config_problem(settings, name, cls):
            notes.append((f"PROBLEM  [{name}] {problem} ({name} wouldn't start)", True))
    out.listing(rows, index=False, wrap=True)
    for line, problem in notes:
        out.warn(line)
        ok = ok and not problem

    from ..players.messages.intervals import FILE as MESSAGES_FILE, overview

    message_rows, left_out = overview(settings.home, [(s.name, s.folder) for s in settings.servers])
    out.heading(f"Interval messages ({MESSAGES_FILE}: for every server, and each server's own)")
    out.listing(message_rows, index=False, wrap=True)
    for line in left_out:
        out.warn(f"warning: {line} (left out; the others are said)")

    permissions = settings.permissions
    prefix = permissions.prefix
    out.heading(f"Commands ({COMMANDS_FILE}{'' if permissions.path and permissions.path.exists() else ', missing: defaults'})")
    unavailable = {spec.name for name, cls in PARTS.items() if not settings.parts[name].enabled
                   for spec in commands_of(cls)}
    by_role: dict[str, list[str]] = {role: [] for role in ROLES}
    for spec in all_commands().values():
        if spec.name not in permissions.off:
            by_role[permissions.role_for(spec)].append(spec.name)
    admins = {a for s in settings.servers for a in s.admins}
    who = {"player": "",
           "member": f" ({len(permissions.members['member'])} listed)",
           "probationary-moderator": " (members on probation: moderators' commands as requests)",
           "moderator": f" ({len(permissions.members['moderator'])} listed)",
           "admin": f" (the server's own; {len(admins)} in dedicated.toml)",
           "owner": f" ({len(permissions.members['owner'])} listed)"}
    role_rows = []
    for role in ROLES:
        names = " ".join(prefix + n + ("*" if n in unavailable else "") for n in sorted(by_role[role]))
        role_rows.append((f"{TITLES[role]}{who[role]}", names or "-"))
    if permissions.off:
        role_rows.append(("off", " ".join(prefix + n for n in sorted(permissions.off))))
    out.listing(role_rows, index=False, wrap=True, widest=48)
    if unavailable & {n for names in by_role.values() for n in names}:
        out.note("* its part is switched off")
    out.note("Each role can also use the commands listed above it. No vote can kick a member or anyone above.")
    if not permissions.members["owner"]:
        out.warn(f"warning: nobody is owner yet: put your player ID under [owner] players in {COMMANDS_FILE}")
    for player_id, roles in permissions.doubles(a.lower() for a in admins).items():
        out.warn(f"warning: {player_id[:8]}… is listed as {' and '.join(roles)}: one role each, and {roles[0]} is "
                 f"the one that counts. Take the other line out of {COMMANDS_FILE} (or dedicated.toml's admins), "
                 "or !demote / !promote them in game, which keeps one")
    for line in permissions.unknown:
        out.warn(f"warning: {line}")
    if permissions.path and permissions.path.exists() and "member" not in permissions.sections:
        out.note(f"{COMMANDS_FILE} is from before members: add [member] with players = [\"<player ID>\", …] to "
                 "keep regulars safe from votes to kick")
    out.write()
    if ok and warned:
        out.ok(f"Ready, but read the settings warnings above: {SETTINGS_FILE} may not say what you meant.")
    elif ok:
        out.ok("Everything looks ready.")
    else:
        out.warn("Fix the problems above, then check again.")
    return 0 if ok else 1


def _label_warnings(settings: Settings, server: ServerConfig) -> list[str]:
    """Choice labels in the server's playlists that its own program refuses. Which those are depends on its version
    (0.9.19 renamed "Vanilla characters" to "Halo 3": 0.9.18 refuses "Halo 3"), asked of the program file only when
    a playlist has one of the labels at all."""
    from ..games.halo3_options import NEWEST

    files = [path for path in playlist_files(server.folder) if old_labels(path, NEWEST) or old_labels(path, "0")]
    if not files:
        return []
    version = _program_version(settings, server)
    found = []
    for path in files:
        if not (labels := sorted(set(old_labels(path, version)))):
            continue
        named = f"{path.name} names {', '.join(repr(label) for label in labels)}"
        if version:
            found.append(f"{named}, which Project Reclaimer {version} (this server's program) calls otherwise: the "
                         "Superintendent writes them as its program calls them as it starts the server")
        else:
            found.append(f"{named}, which no version of Project Reclaimer takes: the Superintendent writes the name "
                         "it takes as it starts the server")
    return found


def _program_version(settings: Settings, server: ServerConfig) -> str | None:
    """The version of the program file the server runs, as its `--version` says (asked once per file); None when
    it isn't known."""
    from ..program.releases import program_of, program_version, readable

    program = program_of(settings, server)
    version = program_version(settings, program) if program is not None else None
    return version if readable(version) else None


def _backend_problems(backend: Any, server: ServerConfig) -> list[str]:
    """What would stop the backend starting this server (a [process] command starting with {executable} is
    judged on the program it names: the backend does that itself)."""
    return list(backend.problems(server))


def _named_above(part: str, warning: str) -> bool:
    """A part's own "isn't a setting" warning for a key that check already named with what it was meant to be."""
    return part == "watchdog" and warning.endswith("isn't a watchdog setting, so it does nothing")


def updates_text(settings: Settings, server: ServerConfig) -> str:
    """How a server keeps up: Project Reclaimer itself or by Huragok, and its Workshop mods."""
    text = "Project Reclaimer " + ("installs its own releases (auto_update)" + (
        "; Huragok steps in when one doesn't arrive" if settings.server_updates.fallback and
        settings.backend == "process" else "") if server.auto_update else "updated by Huragok (U)")
    if not server.workshop_items:
        return text
    return f"{text} · {len(server.workshop_items)} Workshop item(s): {settings.of(server).workshop_text()}"


def update_note(settings: Settings) -> str:
    if not settings.updates.check:
        return " (not looking for updates)"
    from ..core.web import UpdateError
    from ..updates.engine import is_newer, latest_release

    try:
        release = latest_release(settings.updates.repository, timeout=5, beta=settings.updates.beta)
    except UpdateError as error:
        return f" (couldn't look for updates: {error})"
    if release is not None and is_newer(release):
        return f" · {release.version}{' (beta)' if release.beta else ''} is out: {LAUNCHER} update"
    return " (the newest)"
