"""The host's own files brought up to date after Huragok is updated.

An update replaces only the program (updates.py). The host's files stay as they are: settings.toml,
commands.toml, automod.txt, names.txt, intervalmessages.txt, PACKAGES/, records/ and each server's dedicated.toml
(and its own intervalmessages.txt). Since 0.4.0 went out to hosts, every release reads those files as any version
from 0.4.0 on wrote them, and brings them up to date by itself: a host never starts over, and never has to know
that a file's format changed.

Each change is a Step, run in order when Huragok starts (in its own process, under its lock, before any server is
looked after); records/files.json lists the steps done. A step:
- looks before it changes anything, so a second run changes nothing;
- leaves files the version before can still read: a new version that fails to start is put back by the start
  loop, and the version put back runs on the files as they are by then;
- changes TOML only through tomledit, which writes nothing but the change and keeps the host's comments;
- copies each file to records/previous-files/<version>/ before the first change made to it.
A step that fails is logged and tried again at the next start; the program reads the old form meanwhile. `always`
steps (new commands, new starter packages) run at every start: they only add what's missing.

A change the version before can't read waits (`settled`): the start loop that put a new version in place is the
old version's own code, and it puts the old version back when the new one ends within its FAILED_START_SECONDS (120
in every version so far). So such a step runs once this version has been up SETTLED_SECONDS, from the running
Huragok (huragok.py), never at the start. A new command is one: up to 0.4.4 an unknown command in commands.toml stopped
Huragok starting (since 0.5.0 it's a warning).

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

from .. import __version__
from ..core.files import write_atomic

log = logging.getLogger(__name__)

FILES_RECORD = "files.json"  # in records/: which steps have run on this host's files
BACKUPS = "previous-files"  # in records/: each file as it was before a version's first change to it
SETTLED_SECONDS = 150  # up this long, a new version is past the start loop's FAILED_START_SECONDS: it stays
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
NAME_MATCH_COMMENT = [  # the template's, above [automod] name_match
    "How a word from automod.txt catches a player's name. \"anywhere\": anywhere in",
    "it, so hitler catches Hitler, DasHitler and xXhitlerXx; a short word catches",
    "more than you mean (ass in Classic) unless its line lists -classic. \"words\":",
    "only as a word of its own, the name taken apart where its capitals and digits",
    "change, so hitler catches DasHitler but not dashitler. Either way a listed",
    "number isn't caught inside a longer one (88 isn't in Mike1988).",
]
SHARE_COMMENT_0_4_0 = [  # above [setup] share, as 0.4.0 to 0.4.4 wrote it
    "# What new servers take from this folder instead of carrying their own copy:",
    "#   game     Halo 3's files (5.5 GB), once, at game/: linked from a server that has",
    "#            them, or copied from MCC with dedicated init",
    "#   bans     one bans.json: one ban list, one admin list, one VPN allowance list",
    "#   content  one content/ folder (maps, game types, mods, images) for every server. Left",
    "#            out by default: each server keeps its own, so its Workshop mods can be",
    "#            updated after its own games and its custom games stay its own (D gives a",
    "#            server that shares one its own copy: hard links, no space used)",
]
SHARE_COMMENT_0_5_0 = [
    "# What new servers take from this folder instead of carrying their own copy:",
    "#   game     Halo 3's files (5.5 GB), once, at game/: linked from a server that has",
    "#            them, or copied from MCC with dedicated init",
    "#   content  one content/ folder (maps, game types, mods, images) for every server. Left",
    "#            out by default: each server keeps its own, so its Workshop mods can be",
    "#            updated after its own games and its custom games stay its own (D gives a",
    "#            server that shares one its own copy: hard links, no space used)",
    "# Every server keeps a ban list of its own (bans.json in its folder): a /ban in game",
    "# is that server's. Huragok keeps its own list of the bans it gives (! commands,",
    "# Vergil) and the admins it makes, and puts them on every server's.",
]
ACROSS_COMMENT = [  # the template's, above [automod] across_servers
    "Huragok's moderation reaches every server: a mute by a ! command or Vergil",
    "follows the player to any server they join, a kick keeps them off every server",
    "until its time is up, and a ban reaches servers with a ban list of their own",
    "(servers sharing one have it already). The game's own /kick and /ban, and votes,",
    "stay on their server; Huragok only writes them on the player's record. false:",
    "Huragok's stay on their server too. Each server's own Vergil is on its page in D.",
]
BETA_COMMENT = [  # the template's, above [updates] beta
    "true: beta releases too (GitHub pre-releases), to try what's coming before it's",
    "a full release; a beta can have bugs the full release won't. false: full releases",
    "only. Turning it off doesn't go back: a beta stays until a full release passes it.",
]
BANNAME_ROLE_0_5_0 = "admin"  # !banname's role from 0.5.0; 0.4.0 to 0.4.4 listed it under [moderator]
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

    def backup(self, path: Path, name: str | None = None) -> None:
        """The file as it was before this version's first change to it, kept once (a later start doesn't refill
        it with a file this version already changed). `name`: where it's kept, when files share a name (each
        server's intervalmessages.txt as "<its folder>/intervalmessages.txt")."""
        if self.dry or path in self.backed_up or not path.is_file():
            return
        target = self.records / BACKUPS / __version__ / (name or path.name)
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
    settled: bool = False  # its change is one the version before can't read: run once this one has been up a while


def _read(path: Path) -> dict[str, Any]:
    from ..core.tomledit import read_toml

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
    from ..core.tomledit import comment_out_table_key

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
    from ..core.tomledit import set_table_key

    updates = _read(ctx.settings_path).get("updates")
    if not isinstance(updates, dict) or updates.get("repository") != OLD_REPOSITORY:
        return []
    if not ctx.dry:
        ctx.backup(ctx.settings_path)
        set_table_key(ctx.settings_path, "updates", "repository", NEW_REPOSITORY)
    return [f"{ctx.settings_path.name}: [updates] repository is {NEW_REPOSITORY} (the repository's name since 0.4.0)"]


def download_game_files(ctx: Context) -> list[str]:
    from ..core.tomledit import add_table_key

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
    from ..core.tomledit import replace_comments

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
    from ..core.tomledit import add_table_key, comment_out_table_key, remove_table_key, replace_comments

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
    from ..core.tomledit import replace_comments

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
    from ..core.tomledit import add_table_key

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


def name_match(ctx: Context) -> list[str]:
    """[automod] name_match (0.5.0): how a listed word catches a name. Its default, "anywhere", catches DasHitler with
    hitler listed, which 0.4.4 didn't (a word had to stand alone in a name), so the line shows the host the choice.
    Only added where the file has [automod]; without one the default applies."""
    from ..core.tomledit import add_table_key

    automod = _read(ctx.settings_path).get("automod")
    if not isinstance(automod, dict) or "name_match" in automod:
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if commented_in(text, "automod", "name_match"):
        return []  # written as a comment by the host: their choice to leave it to its default
    said = [f"{ctx.settings_path.name}: [automod] name_match = \"anywhere\" added: a word from automod.txt anywhere in "
            "a player's name gets them kicked until they change it, DasHitler with hitler listed (new in 0.5.0)"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    if not add_table_key(ctx.settings_path, "automod", "name_match", "anywhere", NAME_MATCH_COMMENT,
                         after="check_names"):
        return []
    return said


def across_servers(ctx: Context) -> list[str]:
    """[automod] across_servers (0.5.0): mutes, kicks and bans reach every server, on by default, so the line shows the
    host the choice. Only added where the file has [automod]; without one the default applies."""
    from ..core.tomledit import add_table_key

    automod = _read(ctx.settings_path).get("automod")
    if not isinstance(automod, dict) or "across_servers" in automod:
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if commented_in(text, "automod", "across_servers"):
        return []
    said = [f"{ctx.settings_path.name}: [automod] across_servers = true added: a mute, kick or ban on one server "
            "reaches the others (new in 0.5.0)"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    if not add_table_key(ctx.settings_path, "automod", "across_servers", True, ACROSS_COMMENT, after="name_match"):
        return []
    return said


def own_ban_lists(ctx: Context) -> list[str]:
    """Every server keeps a ban list of its own from 0.5.0 (the host's call: a /ban in game is that server's; Huragok
    keeps its own bans on every one), so [setup] share loses "bans" (new servers got one shared bans.json). The
    servers already sharing one get a copy of it as each next starts (repair.own_bans): nothing is lost."""
    from ..core.tomledit import replace_comments, set_table_key

    setup = _read(ctx.settings_path).get("setup")
    share = setup.get("share") if isinstance(setup, dict) else None
    if not isinstance(share, list) or "bans" not in [str(s).lower() for s in share]:
        return []
    kept = [s for s in share if str(s).lower() != "bans"]
    said = [f"{ctx.settings_path.name}: [setup] share = {kept!r} (\"bans\" taken out): every server keeps a ban list of "
            "its own from 0.5.0; the ones sharing one get a copy of it as each next starts"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    set_table_key(ctx.settings_path, "setup", "share", kept)
    replace_comments(ctx.settings_path, SHARE_COMMENT_0_4_0, SHARE_COMMENT_0_5_0)
    return said


def banname_for_admins(ctx: Context) -> list[str]:
    """!banname is an admin's from 0.5.0, beside the new !banword and !unbanword (the host's call: what's banned is
    for admins to say). 0.4.0 to 0.4.4 listed it under [moderator], their default; there it moves to [admin]. Listed
    anywhere else (or switched off), it's where the host put it, and it stays. The version before reads it either
    way, so this runs as Huragok starts."""
    from ..core.commandsfile import _checked, _listed_as, all_commands, entry_width, load_permissions
    from ..core.tomledit import add_to_list, remove_from_list

    path = ctx.commands_path
    if not path.is_file() or not _read(path):
        return []
    try:
        prefix = load_permissions(path).prefix
    except Exception:  # noqa: BLE001 - a file Huragok can't read isn't changed (check names its problem)
        return []
    written = _listed_as(path, "moderator", "banname", prefix)
    if not written:
        return []
    said = [f"{path.name}: !banname moved from [moderator] to [admin]: banning names, like the new !banword, is "
            "for admins from 0.5.0 (put it back under [moderator] to keep it theirs)"]
    if ctx.dry:
        return said
    ctx.backup(path)

    def change(copy: Path) -> None:
        for entry in written:
            remove_from_list(copy, "moderator", "commands", entry)
        if not _listed_as(copy, BANNAME_ROLE_0_5_0, "banname", prefix):
            add_to_list(copy, BANNAME_ROLE_0_5_0, "commands", "banname", all_commands()["banname"].help,
                        width=entry_width())

    _checked(path, change, "!banname for admins")
    return said


def beta_releases(ctx: Context) -> list[str]:
    """[updates] beta (0.5.0): beta releases are offered only to a host who turns them on, so the line, off, shows the
    host the choice. Only added where the file has [updates]; without one the default (off) applies. The version
    before reads the file with it (a key it doesn't know is a warning, never a stop)."""
    from ..core.tomledit import add_table_key

    updates = _read(ctx.settings_path).get("updates")
    if not isinstance(updates, dict) or "beta" in updates:
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if commented_in(text, "updates", "beta"):
        return []  # written as a comment by the host: their choice to leave it to its default
    said = [f"{ctx.settings_path.name}: [updates] beta = false added: true offers beta releases of Huragok too, "
            "before they're full releases (new in 0.5.0)"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    if not add_table_key(ctx.settings_path, "updates", "beta", False, BETA_COMMENT, after="auto_update"):
        return []
    return said


def _server_folders(ctx: Context, data: dict[str, Any]) -> list[Path]:
    """Every server folder settings.toml names (each one holding its dedicated.toml), Huragok's own folder left out."""
    from ..core.serverfolders import IGNORED_PREFIXES, SERVERS_FOLDER

    config_name = str(data.get("dedicated_config") or "dedicated.toml")
    folders: list[Path] = []
    for key, default in (("servers_folder", SERVERS_FOLDER), ("server_folder", "")):
        name = data.get(key, default)
        if not isinstance(name, str) or not name:
            continue
        folder = (ctx.home / name).resolve()
        if key == "server_folder":
            folders.append(folder)
        elif folder.is_dir():
            folders.extend(child for child in sorted(folder.iterdir(), key=lambda p: p.name.lower())
                           if child.is_dir() and not child.name.startswith(IGNORED_PREFIXES))
    return [folder for folder in folders if (folder / config_name).is_file() and folder != ctx.home.resolve()]


def interval_messages(ctx: Context) -> list[str]:
    """Interval messages from 0.5.0: intervalmessages.txt in Huragok's folder (for every server) and in each server
    folder (that server's own), written with their notes and no messages, so the host finds them where they go. A
    file that's there is left as it is. The version before doesn't read them."""
    from ..players.messages.intervals import FILE, write_template

    targets = [(ctx.home / FILE, "every")] + [(folder / FILE, "server")
                                              for folder in _server_folders(ctx, _read(ctx.settings_path))]
    said: list[str] = []
    for path, kind in targets:
        if path.exists():
            continue
        where = FILE if kind == "every" else f"{path.parent.name}/{FILE}"
        said.append(f"{where}: interval messages ({'for every server' if kind == 'every' else 'its own'}), "
                    "written with how to set them up; none yet")
        if not ctx.dry:
            write_template(path, kind)
    return said


def _words(ctx: Context) -> tuple[Path, str]:
    """The word list ([automod] words: automod.txt unless the host named another), and how the log names it."""
    automod = _read(ctx.settings_path).get("automod")
    name = str((automod if isinstance(automod, dict) else {}).get("words") or "automod.txt")
    return ctx.home / name, name


def _rules_called(lines: list[str], numbers: list[int]) -> str:
    """The word list's lines by their names, as the host knows them (their numbers change with the notes): "the hate
    and slurs lines", "the hate line and line 51"."""
    names, unnamed = [], []
    for number in numbers:
        parts = [part.strip() for part in lines[number - 1].split("|")]
        if len(parts) == 5 and parts[4]:
            names.append(parts[4])
        else:
            unnamed.append(str(number))

    def joined(items: list[str]) -> str:
        return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else items[0]

    said = [f"the {joined(names)} line" + ("s" if len(names) > 1 else "")] if names else []
    if unnamed:
        said.append(("lines " if len(unnamed) > 1 else "line ") + joined(unnamed))
    return " and ".join(said)


def automod_layout_note(ctx: Context) -> list[str]:
    """A word list from before 0.5.1 with no note naming its parts (the host took Huragok's notes out) gets the one
    its version wrote, `#   words | kicks | mutes | ban | name`, at the top. A list says its layout by that note, and
    one with none is read as this version's (the mutes first), which this one's lines aren't until automod_order()
    turns them. Only a note: the version before reads the list as it did."""
    from ..core.files import read_text
    from ..players.automod.rules import KICKS_FIRST_PARTS, says_layout

    path, label = _words(ctx)
    if not path.is_file():
        return []
    text = read_text(path, RuntimeError)  # one that can't be read: tried again at the next start
    if says_layout(text):
        return []
    if not ctx.dry:
        ctx.backup(path)
        write_atomic(path, f"#   {KICKS_FIRST_PARTS}\n" + text)
    return [f"{label}: a note naming its parts as they were up to 0.5.0 ({KICKS_FIRST_PARTS}) put at the top, so "
            "its lines are read as they were written until they're rewritten with the mutes first"]


def automod_order(ctx: Context) -> list[str]:
    """The word list in this version's layout, the mutes before the kicks (words | mutes | kicks | ban | name), the
    host's call (27 October): each line's mute times first, the strikes that came before the mutes now before the
    kicks, then the kick times (rules.in_this_layout()), and Huragok's notes as this version writes them, every note
    of the host's kept. The version before would take the mutes for kicks, so it waits until this one has settled;
    until then Vergil reads the list in this layout all the same (rules.kicks_first())."""
    from ..core.files import read_text
    from ..players.automod.rules import PARTS, in_this_layout, kicks_first, lines_of
    from .notes import NotesError, refresh_text

    path, label = _words(ctx)
    if not path.is_file():
        return []
    text = read_text(path, RuntimeError)  # one that can't be read: tried again at the next start
    if not kicks_first(text):
        return []
    turned = in_this_layout(text)
    try:
        new = refresh_text("automod.txt", turned.text).text
    except NotesError:
        new = turned.text  # the lines are what's read: its notes stay as they were but the one naming its parts
    if kicks_first(new):  # never: in_this_layout() names the parts this version's way
        raise RuntimeError(f"{label} would still read as the layout up to 0.5.0")
    lines = lines_of(text)
    said = [f"{label}: the mutes come first now ({PARTS}): {_rules_called(lines, turned.turned)} rewritten so, the "
            "strikes that came before the mutes now before the kicks; Huragok's notes as this version writes them"
            if turned.turned else f"{label}: Huragok's notes as this version writes them, the mutes first ({PARTS})"]
    said.extend(f"{label}: {_rules_called(lines, [number])}: kick time{'s' if len(cut) > 1 else ''} {', '.join(cut)} "
                "left out, the ban coming before them now" for number, cut in turned.cut.items())
    if not ctx.dry:
        ctx.backup(path)
        write_atomic(path, new)
    return said


def notes(ctx: Context) -> list[str]:
    """Huragok's notes in the host's files as this version writes them, every note of the host's kept where they put
    it (updates/notes.py). Once a version, at its first start: only notes change, so the version before reads every
    file as it did. A file that can't be read is said, and tried again at the next start; the others are done. A word
    list from before 0.5.1 is left: this version's notes would name its parts the new way before its lines are
    (automod_order() brings them, with its lines)."""
    from ..players.automod.rules import kicks_first
    from ..players.messages.intervals import FILE
    from .notes import NotesError, describe, read, refresh_text

    data = _read(ctx.settings_path)
    automod = data.get("automod") if isinstance(data.get("automod"), dict) else {}
    words, names = (str(automod.get(key) or default) for key, default in (("words", "automod.txt"),
                                                                           ("names", "names.txt")))
    # (the file, what it is, how the log names it, where its copy is kept: None for its own name)
    targets: list[tuple[Path, str, str, str | None]] = [
        (ctx.settings_path, "settings.toml", ctx.settings_path.name, None),
        (ctx.commands_path, "commands.toml", ctx.commands_path.name, None),
        (ctx.home / words, "automod.txt", words, None), (ctx.home / names, "names.txt", names, None),
        (ctx.home / FILE, "intervalmessages.txt", FILE, None)]
    targets += [(folder / FILE, "intervalmessages-server.txt", f"{folder.name}/{FILE}", f"{folder.name}/{FILE}")
                for folder in _server_folders(ctx, data)]
    said: list[str] = []
    problems: list[str] = []
    for path, kind, label, kept_as in targets:
        if not path.is_file():
            continue
        try:
            text = read(path, kind)
            if kind == "automod.txt" and kicks_first(text):
                continue
            result = refresh_text(kind, text)
        except (NotesError, OSError) as error:
            problems.append(f"{label}: {error}")
            continue
        if result.text == text:
            continue
        said.append(describe(label, result))
        if not ctx.dry:
            ctx.backup(path, kept_as)
            write_atomic(path, result.text)
    if problems:
        if not ctx.dry:
            for line in said:
                log.info("Brought up to date for Huragok %s: %s", __version__, line)
        raise RuntimeError("; ".join(problems))
    return said


def new_commands(ctx: Context) -> list[str]:
    """Commands newer than the host's commands.toml, put in their default role's list so the host sees them (a
    command in no list has its default role anyway: this changes nobody's rights)."""
    from ..core.commandsfile import SECTIONS, TITLES
    from ..core.commandsfile import all_commands, entry_width
    from ..core.tomledit import add_to_list

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
            add_to_list(path, section, "commands", spec.name, spec.help, width=entry_width())
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


# A step's key is never changed once a release has it: hosts' records/files.json list the steps done by key, and a
# renamed key would run its step again.
STEPS: list[Step] = [
    Step("0.4.1/retired-settings", "settings that do nothing any more become comments", retired_settings),
    Step("0.4.1/old-repository", "Huragok's repository by its name since 0.4.0", old_repository),
    Step("0.4.1/download-game-files", "[setup] download_game_files", download_game_files),
    Step("0.4.1/steamcmd-comment", "[setup] steamcmd's comment names Halo 3's files", steamcmd_comment),
    Step("0.4.2/auto-update", "[updates] install_on_server_restart becomes auto_update", auto_update),
    Step("0.4.2/updates-comment", "[updates]' comment", updates_comment),
    Step("0.4.4/process-priority", "[process] priority", process_priority),
    Step("0.5.0/name-match", "[automod] name_match", name_match),
    Step("0.5.0/across-servers", "[automod] across_servers", across_servers),
    Step("0.5.0/own-ban-lists", "every server's own ban list: [setup] share without bans", own_ban_lists),
    Step("0.5.0/banname-for-admins", "!banname moves to [admin] in commands.toml", banname_for_admins),
    Step("0.5.0/interval-messages", "intervalmessages.txt in Huragok's folder and each server's", interval_messages),
    Step("0.5.0/beta", "[updates] beta", beta_releases),
    Step("0.5.1/automod-layout-note", "the word list's note naming its parts, where it had none",
         automod_layout_note),
    # Once for each version (the one key that changes with it), after the steps above have added what they add.
    Step(f"{__version__}/notes", "Huragok's notes in your files, as this version writes them", notes),
    Step("0.5.1/automod-order", "the word list with the mutes before the kicks", automod_order, settled=True),
    Step("commands", "new in-game commands listed in commands.toml", new_commands, always=True, settled=True),
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
    write_atomic(path, json.dumps(record, indent=1))


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


def bring_up_to_date(settings_path: Path, records: Path, packages: Path, steps: list[Step] | None = None, *,
                     settled: bool = False) -> list[str]:
    """Run every step not done yet: as Huragok starts, those the version before can read; with `settled` (once this
    version has been up SETTLED_SECONDS), the rest. Returns what changed (logged here too); records/files.json says
    what's done."""
    ctx = context_for(settings_path, records, packages)
    before = json.dumps(ctx.record, sort_keys=True)
    changed, _ = _run(ctx, [step for step in (STEPS if steps is None else steps) if step.settled == settled])
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
    """What the next start would change, without changing anything (a settled step's change: a few minutes after)."""
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
    steps = STEPS if steps is None else steps
    changed, failed = _run(ctx, [step for step in steps if not step.settled])
    later, later_failed = _run(ctx, [step for step in steps if step.settled])
    after = f" ({SETTLED_SECONDS / 60:.1f} minutes after it starts: the version before can't read it)"
    return changed + [line + after for line in later] + [f"can't yet: {line}" for line in failed + later_failed]
