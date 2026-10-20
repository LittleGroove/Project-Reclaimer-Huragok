"""The host's own files brought up to date after Huragok is updated.

An update replaces only the program (updates.py). The host's files stay as they are: settings.toml,
commands.toml, automod.txt, names.txt, PACKAGES/, records/ and each server's dedicated.toml. Since 0.4.0 went out
to hosts, every release reads those files as any version from 0.4.0 on wrote them, and brings them up to date by
itself: a host never starts over, and never has to know that a file's format changed.

Each change is a Step, run in order when Huragok starts (in its own process, under its lock, before any server is
looked after); records/files.json lists the steps done. A step:
- looks before it changes anything, so a second run changes nothing;
- leaves files the version before can still read: a new version that fails to start is put back by the start
  loop, and the version put back runs on the files as they are by then;
- changes TOML only through tomledit, which writes nothing but the change and keeps the host's comments;
- copies each file to records/previous-files/<version>/ before the first change made to it.
A step that fails is logged and tried again at the next start; the program reads the old form meanwhile. `always`
steps (new commands, new starter packages) run at every start: they only add what's missing.

`check` lists what the next start would change (pending()).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__

log = logging.getLogger(__name__)

FILES_RECORD = "files.json"  # in records/: which steps have run on this host's files
BACKUPS = "previous-files"  # in records/: each file as it was before a version's first change to it
# The starter packages 0.4.0 put in PACKAGES/. One of these that's gone was taken out by the host, so it isn't put
# back; a starter that came later is added once.
STARTERS_0_4_0 = ("big-team-battle", "campaign-coop", "infection", "mlg-hardcore", "odst-firefight",
                  "scripted-modes", "team-slayer")
OLD_REPOSITORY = "LittleGroove/Project-Reclaimer-Server-Supervisor"
NEW_REPOSITORY = "LittleGroove/Project-Reclaimer-Huragok"
# Settings that do nothing any more, turned into comments with why: (section, key) -> the comment above them.
AUTO_UPDATE_NOTE = ("Not read since 0.4.0: each server's own auto_update (its dedicated.toml, D in the\n"
                    "window) is the only switch for Project Reclaimer updates. Huragok made this a comment.")
RETIRED_COMMENTS = {
    ("process", "disable_auto_update"): AUTO_UPDATE_NOTE,
    ("windows", "disable_auto_update"): AUTO_UPDATE_NOTE,  # 0.1's name for [process]
}
STEAMCMD_COMMENT_0_4_0 = [
    "# SteamCMD, for Workshop items (the window's I key sets this up: finds or installs",
    "# it, signs the account in once, and writes [workshop] into every server folder).",
    "# Empty: looked for in steamcmd/ here, C:\\steamcmd, PATH. The account must own MCC;",
    "# an anonymous sign-in can't fetch MCC's Workshop items. Never a password here.",
]
STEAMCMD_COMMENT_0_4_1 = [
    "# SteamCMD, for Workshop items and Halo 3's files (the window's I key sets this up:",
    "# finds or installs it, signs the account in once, writes [workshop] into every",
    "# server folder, and gets Halo 3's files). Empty: looked for in steamcmd/ here,",
    "# C:\\steamcmd, PATH. The account must own MCC; an anonymous sign-in can't fetch",
    "# MCC's Workshop items or files. Never a password here.",
]
UPDATES_COMMENT_0_4_0 = [
    "# New versions of Huragok (not the game server), from its GitHub releases.",
    "# It never installs one unless you say so: when one is out, the window says so;",
    "# press U there, or run start.cmd update (Linux: ./start.sh update). Your servers",
    "# keep running while it updates, and only the program is replaced: your settings,",
    "# commands, word list and records stay.",
]
UPDATES_COMMENT_0_4_2 = [
    "# New versions of Huragok (not the game server), from its GitHub releases. Huragok",
    "# looks when it starts and every check_hours; when one is out, the window's HURAGOK",
    "# line says so until it's installed: press U there, or run start.cmd update (Linux:",
    "# ./start.sh update). Your servers keep running while it updates, and only the",
    "# program is replaced: your settings, commands, word list and records stay, and the",
    "# new version brings them up to date.",
]
INSTALL_ON_RESTART_COMMENT_0_4_0 = ["# true: install a new version by itself the next time a server restarts."]
AUTO_UPDATE_COMMENT = [
    "true: a new version installs itself as soon as Huragok finds it, rather than",
    "waiting for you (the servers keep running; a version that failed to install or",
    "to start isn't tried again by itself). false: it waits for U.",
]
RENAMED_TO_AUTO_UPDATE = ("Renamed auto_update in 0.4.2, which Huragok set to this value: true now installs a new\n"
                          "version as soon as it's found, rather than at the next server restart.")
DOWNLOAD_GAME_FILES_COMMENT = [
    "A server that's missing Halo 3's files (one copied in without the game/ it shared,",
    "say) gets them by itself: copied from MCC when it's on this PC, otherwise downloaded",
    "with SteamCMD (about 9.3 GB for multiplayer, 5.8 GB kept). false: nothing is",
    "downloaded without you; the server waits, and I gets them.",
]
PRIORITY_COMMENT = [  # the template's, above [process] priority
    "How much of the processor the servers get before everything else on this PC.",
    "\"high\": ahead of everything that runs at normal (browsers, Steam, Huragok).",
    "\"realtime\": ahead of Windows' own disk, network and input work too. Windows",
    "only gives it to a Huragok started as administrator (otherwise: High), and a",
    "server that runs away with the processor can leave the PC unresponsive.",
    "Also \"above_normal\", \"normal\", \"below_normal\", or \"off\" (left as it starts,",
    "for a host who sets it some other way). One server can have its own: D in the",
    "window, or priority in its [[server]] block below. On Linux these are nice",
    "values, and above normal needs root or CAP_SYS_NICE.",
]


@dataclass
class Context:
    """What a step works on. In a dry run (`check`) a step says what it would change and changes nothing."""

    home: Path
    settings_path: Path
    records: Path
    packages: Path
    dry: bool = False
    record: dict[str, Any] = field(default_factory=dict)
    backed_up: set[Path] = field(default_factory=set)

    @property
    def commands_path(self) -> Path:
        return self.home / "commands.toml"

    def backup(self, path: Path) -> None:
        """The file as it was before this version's first change to it, kept once (a later start doesn't refill
        it with a file this version already changed)."""
        if self.dry or path in self.backed_up or not path.is_file():
            return
        target = self.records / BACKUPS / __version__ / path.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        self.backed_up.add(path)


@dataclass(frozen=True)
class Step:
    key: str  # "0.4.1/retired-settings": the version that brought it, and what
    about: str  # for the log and `check`
    run: Callable[[Context], list[str]]  # what it changed (in a dry run: would change), one line each
    always: bool = False  # run at every start (it only adds what's missing), never marked done


def _read(path: Path) -> dict[str, Any]:
    from .tomledit import read_toml

    try:
        return read_toml(path)
    except (OSError, ValueError):
        return {}


def commented_in(text: str, table: str, key: str) -> bool:
    """The host wrote `# key = …` in [table] itself: their choice to leave it to its default. Only that table's
    lines count: another table's comments may name a key of the same name (dedicated.toml's auto_update, say)."""
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped.split("#", 1)[0].strip() == f"[{table}]"
        elif inside and re.match(rf"#\s*{re.escape(key)}\s*=", stripped):
            return True
    return False


# The steps ------------------------------------------------------------------------------------------------------

def retired_settings(ctx: Context) -> list[str]:
    from .tomledit import comment_out_table_key

    data = _read(ctx.settings_path)
    said: list[str] = []
    for (section, key), note in RETIRED_COMMENTS.items():
        table = data.get(section)
        if not isinstance(table, dict) or key not in table:
            continue
        said.append(f"{ctx.settings_path.name}: [{section}] {key} made a comment (it does nothing since 0.4.0)")
        if not ctx.dry:
            ctx.backup(ctx.settings_path)
            comment_out_table_key(ctx.settings_path, section, key, note)
    return said


def old_repository(ctx: Context) -> list[str]:
    from .tomledit import set_table_key

    updates = _read(ctx.settings_path).get("updates")
    if not isinstance(updates, dict) or updates.get("repository") != OLD_REPOSITORY:
        return []
    if not ctx.dry:
        ctx.backup(ctx.settings_path)
        set_table_key(ctx.settings_path, "updates", "repository", NEW_REPOSITORY)
    return [f"{ctx.settings_path.name}: [updates] repository is {NEW_REPOSITORY} (the repository's name since 0.4.0)"]


def download_game_files(ctx: Context) -> list[str]:
    from .tomledit import add_table_key

    setup = _read(ctx.settings_path).get("setup")
    if isinstance(setup, dict) and "download_game_files" in setup:
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if commented_in(text, "setup", "download_game_files"):
        return []  # written as a comment by the host: their choice to leave it to its default
    if ctx.dry:
        return [f"{ctx.settings_path.name}: [setup] download_game_files = true added (new in 0.4.1)"]
    ctx.backup(ctx.settings_path)
    if not add_table_key(ctx.settings_path, "setup", "download_game_files", True, DOWNLOAD_GAME_FILES_COMMENT,
                         after="steam_username"):
        return []
    return [f"{ctx.settings_path.name}: [setup] download_game_files = true added (new in 0.4.1)"]


def steamcmd_comment(ctx: Context) -> list[str]:
    from .tomledit import replace_comments

    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    stripped = [line.strip() for line in text.splitlines()]
    wanted = [line.strip() for line in STEAMCMD_COMMENT_0_4_0]
    found = any(stripped[i:i + len(wanted)] == wanted for i in range(len(stripped) - len(wanted) + 1))
    if not found:
        return []
    if not ctx.dry:
        ctx.backup(ctx.settings_path)
        if not replace_comments(ctx.settings_path, STEAMCMD_COMMENT_0_4_0, STEAMCMD_COMMENT_0_4_1):
            return []
    return [f"{ctx.settings_path.name}: the comment above [setup] steamcmd says it gets Halo 3's files too"]


def auto_update(ctx: Context) -> list[str]:
    """[updates] install_on_server_restart becomes auto_update (0.4.2): a server that keeps failing to start never
    restarts, so the old switch could wait for ever. Its value moves over; false, the template's default, just goes
    (with the template's comment above it), true stays as a comment saying where it went."""
    from .tomledit import add_table_key, comment_out_table_key, remove_table_key, replace_comments

    updates = _read(ctx.settings_path).get("updates")
    updates = updates if isinstance(updates, dict) else {}
    old = updates.get("install_on_server_restart")
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    add = "auto_update" not in updates and not commented_in(text, "updates", "auto_update")
    value = old if isinstance(old, bool) else False
    said: list[str] = []
    if old is not None:
        said.append(f"{ctx.settings_path.name}: [updates] install_on_server_restart is auto_update now"
                    + (f" (= {str(value).lower()})" if add else " (auto_update was set already)"))
    if add:
        said.append(f"{ctx.settings_path.name}: [updates] auto_update = {str(value).lower()} added: true installs new "
                    "versions of Huragok by themselves (new in 0.4.2)")
    if ctx.dry or not said:
        return said
    ctx.backup(ctx.settings_path)
    if old is not None:
        replace_comments(ctx.settings_path, INSTALL_ON_RESTART_COMMENT_0_4_0, [])
        if old is True:
            comment_out_table_key(ctx.settings_path, "updates", "install_on_server_restart", RENAMED_TO_AUTO_UPDATE)
        else:
            remove_table_key(ctx.settings_path, "updates", "install_on_server_restart")
    if add:
        add_table_key(ctx.settings_path, "updates", "auto_update", value, AUTO_UPDATE_COMMENT, after="check_hours")
    return said


def updates_comment(ctx: Context) -> list[str]:
    from .tomledit import replace_comments

    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    stripped = [line.strip() for line in text.splitlines()]
    wanted = [line.strip() for line in UPDATES_COMMENT_0_4_0]
    if not any(stripped[i:i + len(wanted)] == wanted for i in range(len(stripped) - len(wanted) + 1)):
        return []
    if not ctx.dry:
        ctx.backup(ctx.settings_path)
        if not replace_comments(ctx.settings_path, UPDATES_COMMENT_0_4_0, UPDATES_COMMENT_0_4_2):
            return []
    return [f"{ctx.settings_path.name}: the comment above [updates] says the window keeps saying a new version is out"]


def process_priority(ctx: Context) -> list[str]:
    """[process] priority (0.4.4): the servers run at High unless it says otherwise, so the line shows the host the
    choice. Only added where the file has a [process] table (or 0.1's [windows]); without one the default applies."""
    from .tomledit import add_table_key

    data = _read(ctx.settings_path)
    table = next((name for name in ("process", "windows") if isinstance(data.get(name), dict)), "")
    if not table or "priority" in data[table]:
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if commented_in(text, table, "priority"):
        return []  # written as a comment by the host: their choice to leave it to its default
    said = [f"{ctx.settings_path.name}: [{table}] priority = \"high\" added: the servers run at High priority, ahead "
            "of everything else on this PC that runs at normal (new in 0.4.4)"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    if not add_table_key(ctx.settings_path, table, "priority", "high", PRIORITY_COMMENT,
                         after="stop_servers_on_exit"):
        return []
    return said


def new_commands(ctx: Context) -> list[str]:
    """Commands newer than the host's commands.toml, put in their default role's list so the host sees them (a
    command in no list has its default role anyway: this changes nobody's rights)."""
    from .permissions import SECTIONS, TITLES, all_commands
    from .tomledit import add_to_list

    path = ctx.commands_path
    data = _read(path)
    if not path.is_file() or not data:
        return []
    listed: set[str] = set()
    for section in [*SECTIONS, "off"]:
        table = data.get(section)
        commands = table.get("commands") if isinstance(table, dict) else None
        if isinstance(commands, list):
            listed.update(str(c).lower() for c in commands)
    said: list[str] = []
    for spec in sorted(all_commands().values(), key=lambda s: s.name):
        if spec.name.lower() in listed:
            continue
        section = TITLES.get(spec.role, spec.role)
        said.append(f"{path.name}: !{spec.name} (new) listed under [{section}], its default")
        if not ctx.dry:
            ctx.backup(path)
            add_to_list(path, section, "commands", spec.name, spec.help)
    return said


def new_starters(ctx: Context) -> list[str]:
    """Starter packages newer than the host's PACKAGES/: each added once. One the host took out stays out."""
    from importlib import resources

    if not ctx.packages.is_dir():
        return []
    try:
        starters = resources.files("huragok").joinpath("templates/packages")
        names = sorted(child.name for child in starters.iterdir() if child.is_dir())
    except (FileNotFoundError, OSError):
        return []
    offered = set(ctx.record.get("starters") or STARTERS_0_4_0)
    said: list[str] = []
    for name in names:
        if name in offered or (ctx.packages / name).exists():
            offered.add(name)
            continue
        said.append(f"{ctx.packages.name}/{name}: a new starter package")
        if not ctx.dry:
            with resources.as_file(starters.joinpath(name)) as source:
                shutil.copytree(source, ctx.packages / name)
            offered.add(name)
    if not ctx.dry:
        ctx.record["starters"] = sorted(offered)
    return said


STEPS: list[Step] = [
    Step("0.4.1/retired-settings", "settings that do nothing any more become comments", retired_settings),
    Step("0.4.1/old-repository", "Huragok's repository by its name since 0.4.0", old_repository),
    Step("0.4.1/download-game-files", "[setup] download_game_files", download_game_files),
    Step("0.4.1/steamcmd-comment", "[setup] steamcmd's comment names Halo 3's files", steamcmd_comment),
    Step("0.4.2/auto-update", "[updates] install_on_server_restart becomes auto_update", auto_update),
    Step("0.4.2/updates-comment", "[updates]' comment", updates_comment),
    Step("0.4.4/process-priority", "[process] priority", process_priority),
    Step("commands", "new in-game commands listed in commands.toml", new_commands, always=True),
    Step("starters", "new starter packages in PACKAGES/", new_starters, always=True),
]


# Running them -----------------------------------------------------------------------------------------------------

def _load_record(records: Path) -> dict[str, Any]:
    try:
        data = json.loads((records / FILES_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_record(records: Path, record: dict[str, Any]) -> None:
    records.mkdir(parents=True, exist_ok=True)
    path = records / FILES_RECORD
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(record, indent=1), encoding="utf-8")
    temp.replace(path)


def context_for(settings_path: Path, records: Path, packages: Path, *, dry: bool = False) -> Context:
    return Context(settings_path.parent, settings_path, records, packages, dry, _load_record(records))


def _run(ctx: Context, steps: list[Step]) -> tuple[list[str], list[str]]:
    """(what changed, what failed): each step in order; a failure doesn't stop the ones after it."""
    done = set(ctx.record.get("steps") or [])
    changed: list[str] = []
    failed: list[str] = []
    for step in steps:
        if not step.always and step.key in done:
            continue
        try:
            changed.extend(step.run(ctx))
        except Exception as error:  # noqa: BLE001 - one step going wrong must never stop Huragok starting
            failed.append(f"{step.about}: {error}")
            if not ctx.dry:
                log.warning("Couldn't bring the files up to date (%s): %s; tried again at the next start", step.about,
                            error)
            continue
        if not step.always and not ctx.dry:
            done.add(step.key)
    if not ctx.dry:
        ctx.record["steps"] = sorted(done)
        ctx.record["version"] = __version__
        ctx.record["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return changed, failed


def bring_up_to_date(settings_path: Path, records: Path, packages: Path, steps: list[Step] | None = None) -> list[str]:
    """Run every step not done yet. Returns what changed (logged here too); records/files.json says what's done."""
    ctx = context_for(settings_path, records, packages)
    before = json.dumps(ctx.record, sort_keys=True)
    changed, _ = _run(ctx, STEPS if steps is None else steps)
    for line in changed:
        log.info("Brought up to date for Huragok %s: %s", __version__, line)
    if changed:
        log.info("The files as they were are in %s", records / BACKUPS / __version__)
    if json.dumps(ctx.record, sort_keys=True) != before:
        try:
            _save_record(records, ctx.record)
        except OSError as error:
            log.warning("Couldn't write %s (%s): the steps run again at the next start, changing nothing twice",
                        records / FILES_RECORD, error)
    return changed


def pending(settings_path: Path, records: Path, packages: Path, steps: list[Step] | None = None) -> list[str]:
    """What the next start would change, without changing anything."""
    import tomllib

    for path in (settings_path, settings_path.parent / "commands.toml"):
        try:
            tomllib.loads(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            return [f"can't yet: {path.name} can't be read ({error}); once it's fixed (O opens it), the next start "
                    "brings it up to date"]
    ctx = context_for(settings_path, records, packages, dry=True)
    changed, failed = _run(ctx, STEPS if steps is None else steps)
    return changed + [f"can't yet: {line}" for line in failed]
