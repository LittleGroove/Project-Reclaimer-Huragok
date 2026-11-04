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
import os
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
            if path.name == "discord.toml":  # a password has no business in a folder of old copies, and each update would leave another
                target.write_text(without_secrets(target.read_text(encoding="utf-8")), encoding="utf-8")
        self.backed_up.add(path)


SET_TOKEN = re.compile(r'''^(\s*token\s*=\s*)"[^"]*"''', re.M)
WEBHOOK_ADDRESS = re.compile(r"https://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/[^\s\"',\]}]+")


def without_secrets(text: str) -> str:
    return WEBHOOK_ADDRESS.sub("(left out of this copy)", SET_TOKEN.sub(r'\1""', text))


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


KICK_LIMIT_COMMENT = [  # the template's, above [automod] moderators_kick_at_most
    "The longest kick with a time (!kick Bravo 7d) a moderator can give, as a long kick",
    "is a ban in all but name. Admins and faculty have no limit.",
]


ADOPT_COMMENT = [  # the template's, above [workshop_updates] adopt
    "Mods copied into a server's content folder by hand that name a Workshop item (their",
    "ModInfo.json does) are never updated by the server program. true: Huragok fetches",
    "the Workshop's own copy while the server runs, then after a game (at once when nobody",
    "is on) puts it in the hand copy's place, keeps the hand copy in \"content set aside\"",
    "beside the content folder and lists the item in [workshop] items. A copy the program",
    "installed that isn't listed any more is listed again: the server loads it all the same.",
]
LAYOUTS_COMMENT = [  # the template's, above [workshop_updates] layouts
    "The Forge layouts each mod ships (\"Project Reclaimer Layouts\") copied into",
    "content/Maps/<mod> and kept in step with its new versions. A layout changed there by",
    "hand is left as it is.",
]


def workshop_adopt(ctx: Context) -> list[str]:
    """[workshop_updates] adopt and layouts (0.6.0): on by default, so the lines show the host what Huragok now does
    with the Workshop mods it finds. Only added where the file has [workshop_updates]."""
    from ..core.tomledit import add_table_key

    table = _read(ctx.settings_path).get("workshop_updates")
    if not isinstance(table, dict):
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    wanted = [(key, comment, after, words) for key, comment, after, words in (
        ("adopt", ADOPT_COMMENT, "check_minutes", "a mod copied into a server's content by hand that names a Workshop "
                                                  "item is kept up to date too"),
        ("layouts", LAYOUTS_COMMENT, "adopt", "the Forge layouts mods ship are copied into content/Maps and kept in "
                                              "step with each new version"))
        if key not in table and not commented_in(text, "workshop_updates", key)]
    said = [f"{ctx.settings_path.name}: [workshop_updates] {key} = true added: {words} (new in 0.6.0)"
            for key, _, _, words in wanted]
    if ctx.dry or not said:
        return said
    ctx.backup(ctx.settings_path)
    done = []
    for (key, comment, after, _), line in zip(wanted, said):
        if add_table_key(ctx.settings_path, "workshop_updates", key, True, comment, after=after):
            done.append(line)
    return done


def moderators_kick_at_most(ctx: Context) -> list[str]:
    """[automod] moderators_kick_at_most (0.6.0): !kick takes a time now, so the line shows the host its limit. Only added where the file has [automod]."""
    from ..core.tomledit import add_table_key

    automod = _read(ctx.settings_path).get("automod")
    if not isinstance(automod, dict) or "moderators_kick_at_most" in automod:
        return []
    try:
        text = ctx.settings_path.read_text(encoding="utf-8")
    except OSError:
        return []
    if commented_in(text, "automod", "moderators_kick_at_most"):
        return []
    said = [f"{ctx.settings_path.name}: [automod] moderators_kick_at_most = \"1d\" added: the longest kick with a time a moderator can give (new in 0.6.0)"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    if not add_table_key(ctx.settings_path, "automod", "moderators_kick_at_most", "1d", KICK_LIMIT_COMMENT, after="across_servers"):
        return []
    return said


RESTART_BUDGET_NOTE = ("Not read since 0.6.0: automatic restarts aren't held to a number an hour any more\n"
                       "(a budget that stopped them stopped updates and the watchdog doing their job).\n"
                       "Many in an hour are only said in the log. Huragok made this a comment.")
RESTART_BUDGET_OLD_NOTE = "# More automatic restarts than this in one hour means something is looping: FAULTED."


def restart_budget(ctx: Context) -> list[str]:
    """[restarts] restarts_per_hour does nothing from 0.6.0 (the host's call). Left at Huragok's own 6, under Huragok's
    own note, both go: the file is then a new host's. A number of the host's own becomes a comment saying why, so they
    still see what they'd set. A version before reads either: it takes its default (6)."""
    from ..core.tomledit import comment_out_table_key, remove_table_key

    restarts = _read(ctx.settings_path).get("restarts")
    if not isinstance(restarts, dict) or "restarts_per_hour" not in restarts:
        return []
    theirs = restarts.get("restarts_per_hour") != 6
    said = [f"{ctx.settings_path.name}: [restarts] restarts_per_hour " + ("made a comment" if theirs else "taken out")
            + ": automatic restarts aren't held to a number an hour since 0.6.0"]
    if ctx.dry:
        return said
    ctx.backup(ctx.settings_path)
    done = (comment_out_table_key(ctx.settings_path, "restarts", "restarts_per_hour", RESTART_BUDGET_NOTE) if theirs
            else remove_table_key(ctx.settings_path, "restarts", "restarts_per_hour"))
    if not done:
        return []
    lines = ctx.settings_path.read_bytes().decode("utf-8").splitlines(keepends=True)  # its own line endings kept
    kept = [line for line in lines if line.rstrip("\r\n") != RESTART_BUDGET_OLD_NOTE]
    if len(kept) != len(lines):
        write_atomic(ctx.settings_path, "".join(kept), newline="")
    return said


def watchdog_memory(ctx: Context) -> list[str]:
    """[watchdog] memory only warns unless turned on, from 0.6.0 (the host's call): the "act" every earlier version
    wrote becomes "watch" where memory_mb is still 0 (the case was never on). A host who set memory_mb keeps theirs."""
    from ..core.tomledit import set_table_key

    watchdog = _read(ctx.settings_path).get("watchdog")
    if not isinstance(watchdog, dict) or watchdog.get("memory") != "act" or watchdog.get("memory_mb", 0) not in (0, 0.0):
        return []
    said = [f"{ctx.settings_path.name}: [watchdog] memory = \"watch\": it only warns unless you turn it on (\"act\"), "
            "and counts growth past memory_mb once a run has settled (new in 0.6.0)"]
    if not ctx.dry:
        ctx.backup(ctx.settings_path)
        set_table_key(ctx.settings_path, "watchdog", "memory", "watch")
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


def requests_command(ctx: Context) -> list[str]:
    """!reports, the probationary moderators' list, is !requests from 0.6.0, beside the new !report; and !reports is the list of what's open for staff. Where a host listed the old one, the new name goes in its place, so it stays where they put it.
    Settled, so that a put-back finds the name it knows."""
    from ..core.commandsfile import SECTIONS, _checked, _listed_as, all_commands, entry_width, load_permissions
    from ..core.tomledit import add_to_list, remove_from_list

    path = ctx.commands_path
    if not path.is_file() or not _read(path):
        return []
    try:
        prefix = load_permissions(path).prefix
    except Exception:  # noqa: BLE001 - a file Huragok can't read isn't changed (check names its problem)
        return []
    where = {section: written for section in [*SECTIONS, "off"] if (written := _listed_as(path, section, "reports", prefix))}
    if not where or any(_listed_as(path, section, "requests", prefix) for section in [*SECTIONS, "off"]):
        return []
    said = [f"{path.name}: !reports is !requests now, where you had it: {prefix}reports lists what's open for staff"]
    if ctx.dry:
        return said
    ctx.backup(path)

    def change(copy: Path) -> None:
        for section, written in where.items():
            for entry in written:
                remove_from_list(copy, section, "commands", entry)
            add_to_list(copy, section, "commands", "requests", all_commands()["requests"].help, width=entry_width())

    _checked(path, change, "!requests")
    return said


NOT_USED = re.compile(r"#\s*Not used yet: (?:\[([^\]]+)\] comes back|this (channel|limit) comes back)")


def _means_the_same(where: tuple[str, ...], key: str, value: Any) -> bool:
    """Whether a beta's value means now what it meant then: [roles] sync made a Discord role a rank in the first beta,
    and takes rank roles off whoever isn't linked now; its boards were named in a way 0.6.1 can't read (second audit,
    9.1). Those stay notes."""
    from ..servers.leaderboards import PERIODS, known

    if (where, key) == (("roles",), "sync"):
        return False
    if (where, key) == (("leaderboards",), "boards"):
        def readable(entry: Any) -> bool:
            words = str(entry).strip().lower().split()
            return words == ["red-blue"] or (1 <= len(words) <= 2 and known(words[0]) and (len(words) == 1 or words[1] in PERIODS))

        return isinstance(value, list) and all(readable(entry) for entry in value)
    return True


def discord_built(ctx: Context) -> list[str]:
    """A setting 0.6.0 took out as a note ("Not used yet: … comes back with the version that builds it"), for what this
    version builds: back as the host set it. Left as notes, the host's own values were lost, and [channels] stats was
    written over with the default (audit 0.6.1, 6.2). One whose table this version still doesn't build stays a note."""
    import tomllib

    from ..core.tomledit import set_nested_key
    from .notes import template_text

    path = ctx.home / "discord.toml"
    if not path.is_file() or not _read(path):
        return []
    template = tomllib.loads(template_text("discord.toml"))
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    back: list[tuple[tuple[str, ...], str, Any]] = []
    number = 0
    while number < len(lines):
        found = NOT_USED.match(lines[number])
        if not found:
            kept.append(lines[number])
            number += 1
            continue
        where = tuple(found.group(1).split(".")) if found.group(1) else ("channels",) if found.group(2) == "channel" else ("limits",)
        block, body, end = [lines[number]], [], number + 1
        while end < len(lines) and lines[end].startswith("# ") and not NOT_USED.match(lines[end]):
            block.append(lines[end])
            body.append(lines[end][2:])
            end += 1
        try:
            values = tomllib.loads("".join(body))
        except tomllib.TOMLDecodeError:
            values = {}
        table: Any = template
        for part in where:
            table = table.get(part) if isinstance(table, dict) else None
        if values and isinstance(table, dict) and all(key in table and not isinstance(value, dict) and _means_the_same(where, key, value)
                                                      for key, value in values.items()):
            back += [(where, key, value) for key, value in values.items()]
        else:
            kept += block
        number = end
    if not back:
        return []
    said = [f"{path.name}: [{'.'.join(where)}] {key} back as you set it, now it's built" for where, key, _ in back]
    if not ctx.dry:
        ctx.backup(path)
        write_atomic(path, "".join(kept))
        for where, key, value in back:
            set_nested_key(path, where, key, value)
    return said


FACULTY_NOTES = {  # 0.6.0's notes on the lines the faculty step renames, and this version's
    "# Discord USER IDs with owner rights from Discord. People, never a role.":
        "# Discord USER IDs with faculty rights from Discord (whoever types /huragok claim). People, never a role.",
    "# what an owner of the bot acts as, running commands from Discord: owner | admin":
        "# what faculty act as, running commands from Discord: faculty | admin",
}

def faculty(ctx: Context) -> list[str]:
    """The top rank was "owner" before 0.6.1. It's faculty (the host's call, 3 November 2026: "the true owner of the
    server is the one who controls the machine its on, otherwise it really is faculty"), wherever it's written:
    [owner] in commands.toml is [faculty], its notes and players kept; a setting that names the rank says faculty
    (settings.toml's exempt_roles and relay users, discord.toml's owners, owners_in_game, [roles] owner and the ranks
    in close_rank, power_rank and raw_rank); and the records say faculty for the rank of whoever gave each action.
    Settled: the version before knows only the old name, and this one reads both until it's gone."""
    import sqlite3
    from contextlib import closing

    from ..core.commandsfile import OLD_RANK, all_commands, entry_width, player_notes
    from ..core.tomledit import (add_to_list, remove_empty_table, remove_nested_key, remove_table_key, rename_nested_key,
                                 set_nested_key)
    from .notes import NotesError, refresh_text

    said: list[str] = []
    path = ctx.commands_path
    text = path.read_text(encoding="utf-8") if path.is_file() and _read(path) else ""  # one that can't be read is never touched
    owner_header = re.compile(rf'(?mi)^[ \t]*\[[ \t]*"?{OLD_RANK}"?[ \t]*\](?=[ \t]*(#.*)?$)')  # however it's written (6.3)
    if owner_header.search(text):
        said.append(f"{path.name}: [{OLD_RANK}] is [faculty] now, everyone in it kept")
        if not ctx.dry:
            ctx.backup(path)
            faculty_now = (_read(path) or {}).get("faculty")
            if isinstance(faculty_now, dict):
                # Written before this step (a claim, the window, a new command listed): into [owner], with their notes,
                # and [owner] renamed as it stands. Left beside it, [owner] stayed for good: its faculty couldn't be taken
                # out, and 0.6.0 refused the file (audit 0.6.1, 6.3).
                notes = player_notes(path)
                prefix = str((_read(path) or {}).get("prefix") or "!")
                for player_id in faculty_now.get("players") or []:
                    add_to_list(path, OLD_RANK, "players", str(player_id).lower(), notes.get(str(player_id).lower(), ""))
                for command in faculty_now.get("commands") or []:
                    spec = all_commands().get(str(command).strip().lower().removeprefix(prefix))
                    add_to_list(path, OLD_RANK, "commands", str(command), spec.help if spec else "", width=entry_width())
                for key in list(faculty_now):
                    remove_table_key(path, "faculty", key)
                remove_empty_table(path, ("faculty",))
                text = path.read_text(encoding="utf-8")
            renamed = owner_header.sub("[faculty]", text, count=1)
            try:  # its notes as this version writes them, now its sections are named as this version's are
                renamed = refresh_text("commands.toml", renamed).text
            except NotesError:
                pass
            write_atomic(path, renamed)
    settings = ctx.settings_path
    text = settings.read_text(encoding="utf-8") if settings.is_file() and _read(settings) else ""
    lines, table, changed = text.splitlines(keepends=True), "", False
    quoted = re.compile(rf"""(["']){OLD_RANK}\1""", re.IGNORECASE)  # "owner", 'Owner'…, as 0.6.0 took them all (6.6)
    in_roles = False
    for number, line in enumerate(lines):
        if found := re.match(r"\s*\[\[?([^\]]+)\]\]?", line):
            table = found.group(1).strip()
        new, value = line, line.split("#", 1)[0]  # the value only: a note of the host's is theirs
        if table == "automod" and (in_roles or re.match(r"\s*exempt_roles\s*=", line)):
            new = quoted.sub('"faculty"', value) + line[len(value):]
            in_roles = "]" not in value  # an exempt_roles over several lines
        elif table == "relay.user" and re.match(r"\s*role\s*=", line):
            new = quoted.sub('"faculty"', value, count=1) + line[len(value):]
        if new != line:
            lines[number], changed = new, True
    if changed:
        said.append(f"{settings.name}: the rank {OLD_RANK} is faculty now, where a setting names it")
        if not ctx.dry:
            ctx.backup(settings)
            write_atomic(settings, "".join(lines))
    discord = ctx.home / "discord.toml"
    data = _read(discord) if discord.is_file() else {}
    renames = [(("ranks",), "owners", "faculty"), (("ranks",), "owners_in_game", "faculty_in_game"), (("roles",), OLD_RANK, "faculty")]
    wanted = [(table, old, new) for table, old, new in renames if old in (data.get(table[0]) or {})]  # the host's, over a default beside it
    values = [(("ranks",), "faculty_in_game"), (("tickets",), "close_rank"), (("commands",), "power_rank"), (("console",), "raw_rank")]
    if wanted or any(str((data.get(table[0]) or {}).get(key, "")).lower() == OLD_RANK for table, key in values) \
            or str((data.get("ranks") or {}).get("owners_in_game", "")).lower() == OLD_RANK:
        said.append(f"{discord.name}: owners are faculty now")
        if not ctx.dry:
            ctx.backup(discord)
            for table, old, new in wanted:
                given = data.get(table[0]) or {}
                if new in given:
                    # The new name's an update's default, or a claim made before this step: what it has goes onto the
                    # old list with its note, and the old's renamed as it stands, each ID's note beside it kept (the
                    # Discord name a claim writes: audit 0.6.1, 6.1). A single value: the old one, as it was set.
                    if isinstance(given[old], list) and isinstance(given[new], list):
                        written = discord.read_text(encoding="utf-8")
                        for entry in given[new]:
                            if entry not in given[old]:
                                found = re.search(rf'"{re.escape(str(entry))}"\s*,?\s*#\s*(.*)', written)
                                add_to_list(discord, table[0], old, str(entry), found.group(1).strip() if found else "")
                    remove_nested_key(discord, table, new)
                rename_nested_key(discord, table, old, new)
            data = _read(discord)
            for table, key in values:
                if str((data.get(table[0]) or {}).get(key, "")).lower() == OLD_RANK:
                    set_nested_key(discord, table, key, "faculty")
            written = discord.read_text(encoding="utf-8")  # 0.6.0's own notes on the renamed lines, as this version writes them
            for was, now in FACULTY_NOTES.items():
                written = written.replace(was, now)
            write_atomic(discord, written)
    database = ctx.home / str((_read(ctx.settings_path) or {}).get("database") or "records/huragok.sqlite3")
    if database.is_file():
        try:
            with closing(sqlite3.connect(str(database), timeout=10)) as db:
                tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                kept = 0
                for name in ("actions", "commands_no_votes"):  # who gave each, with the rank they had then
                    if name in tables:
                        kept += (db.execute(f"SELECT COUNT(*) FROM {name} WHERE by_role = ?", (OLD_RANK,)).fetchone()[0] if ctx.dry
                                 else db.execute(f"UPDATE {name} SET by_role = 'faculty' WHERE by_role = ?", (OLD_RANK,)).rowcount)
                db.commit()
            if kept:
                said.append(f"{database.name}: {kept} record{'s' if kept != 1 else ''} of what faculty did say faculty, not {OLD_RANK}")
        except sqlite3.Error as error:
            log.warning("Couldn't bring the records' ranks up to date: %s", error)
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


def _kicking(line: str) -> str:
    """A word list's line with nothing in its mutes, kicks and ban, with the 0 in its kicks that keeps it the server's
    kick every time: the rest of it as the host wrote it."""
    parts = line.rstrip().split("|")
    if len(parts) < 3:  # "kickword", or "kickword |": its mutes are empty, and so are its kicks but for the 0
        return parts[0].rstrip() + " | | 0"
    parts[2] = " 0".ljust(max(len(parts[2]), 3)) if len(parts) > 3 else " 0"  # in its column, if it has one
    return "|".join(parts).rstrip()


def automod_reports(ctx: Context) -> list[str]:
    """Since 0.6.0 a line of the word list with nothing in its mutes, kicks and ban only reports (a ticket for the staff
    on Discord, nothing in game); up to then it was the server's kick every time. The host's lines like that get the
    0 in their kicks that says so (`kickword | | 0`), so they go on kicking: the version before reads that the same.
    The 0.2 layout's are left, read as they always were; a list still in the layout up to 0.5.0 gets its 0s as
    automod_order() turns it (rules.from_kicks_first())."""
    from ..core.files import read_text
    from ..players.automod.rules import RuleError, kicks_first, lines_of, parse_line, parse_lenient

    path, label = _words(ctx)
    if not path.is_file():
        return []
    text = read_text(path, RuntimeError)  # one that can't be read: tried again at the next start
    if kicks_first(text):
        return []
    lines = lines_of(text)
    changed: list[int] = []
    for number, line in enumerate(lines, 1):
        try:
            rule = parse_line(line, number)
        except RuleError:
            continue  # left out as it is, and said at the start: the host's to fix
        if rule is not None and rule.reports:
            lines[number - 1] = _kicking(line)
            changed.append(number)
    if not changed:
        return []
    new = "\n".join(lines) + "\n"
    if any(rule.reports for rule in parse_lenient(new)[0] if rule.line in changed):  # never: _kicking() writes the 0
        raise RuntimeError(f"{label} would still only report on line {changed[0]}")
    if not ctx.dry:
        ctx.backup(path)
        write_atomic(path, new)
    return [_kicking_said(label, lines, changed)]


def _kicking_said(label: str, lines: list[str], numbers: list[int]) -> str:
    many = len(numbers) > 1
    return (f"{label}: {_rules_called(lines, numbers)} had nothing in {'their' if many else 'its'} mutes, kicks and ban, "
            f"the server's kick every time: a 0 in {'their' if many else 'its'} kicks keeps {'them' if many else 'it'} "
            "kicking, as a line with nothing in them only reports now (a ticket for the staff on Discord)")


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
    if turned.kicking:
        said.append(_kicking_said(label, lines, turned.kicking))
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
        (ctx.home / "discord.toml", "discord.toml", "discord.toml", None),
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


def discord_file(ctx: Context) -> list[str]:
    """discord.toml, for the bot: every feature off. Nothing before 0.6.0 reads it, so the version before doesn't mind."""
    from .notes import template_text

    path = ctx.home / "discord.toml"
    if path.exists():
        return []
    if not ctx.dry:
        write_atomic(path, template_text("discord.toml"))
        if os.name != "nt":  # it'll hold a password
            os.chmod(path, 0o600)
    return ["discord.toml: new, for the Discord bot; it's off until you set it up (J in the window)"]


# Where a message's line moved when channels were sorted out (30 October), and whether it stopped posting by default. Only a line still at its old channel moves: one a host pointed elsewhere is theirs.
MOVED_LINES = {**{f"moderation-{kind}": ("staff", "logs", None) for kind in ("ban", "kick", "mute", "warn", "unmute", "unban", "pardon")},
               "vergil-would": ("staff", "huragok-logs", None), "watch-first-time-quiet": ("staff", "huragok-logs", None), "calls-called": ("staff", "reports", None),
               "alerts-recovered": ("alerts", "huragok-logs", None), "alerts-huragok-started": ("alerts", "huragok-logs", None),
               **{f"servers-{kind}": ("alerts", "huragok-logs", None) for kind in ("started", "adopted", "stopped", "restarted", "repaired")},
               **{f"servers-{kind}": ("alerts", "info", None) for kind in ("restart-planned", "program-updated", "workshop-updated")},
               "servers-countdown": ("alerts", "info", False), "games-started": ("games", "games", False),
               "seeding-wanted": ("public", "info", None), "seeding-notify": ("public", "info", None), "content-workshop": ("logs", "huragok-logs", None)}


def discord_channels(ctx: Context) -> list[str]:
    """discord.toml from 0.6.0-beta.1 on, to tickets, logs, boards and feeds: `public` is `status` now, and each message's line goes to its new channel, unless the host chose where it went."""
    from ..core.tomledit import remove_table_key, set_nested_key, set_table_key

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []
    said: list[str] = []
    channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
    if "public" in channels:
        said.append("discord.toml: [channels] public is status now: only the server cards go there. Make an info channel for the rest")
    lines = data.get("messages") if isinstance(data.get("messages"), dict) else {}
    moved = {key: move for key, move in MOVED_LINES.items() if isinstance(lines.get(key), dict) and lines[key].get("channel") == move[0] and (move[0] != move[1] or (move[2] is not None and lines[key].get("post") is not move[2]))}
    if moved:
        said.append(f"discord.toml: [messages]: {len(moved)} lines moved to their new channels")
    board = (data.get("status") or {}).get("board") or {}
    if ctx.dry or not said and board.get("channel") != "public":
        return said
    ctx.backup(path)
    if "public" in channels:
        if not str(channels.get("status") or "").strip():
            set_table_key(path, "channels", "status", str(channels["public"]))
        remove_table_key(path, "channels", "public")
    for key, (_, new, post) in moved.items():
        set_table_key(path, "messages", key, {**lines[key], "channel": new, **({} if post is None else {"post": post})})
    if board.get("channel") == "public":
        set_nested_key(path, ("status", "board"), "channel", "status")
    return said


# What the first beta wrote, and what they are now. Only a list still as it was written: one the host has edited is theirs, and the file's check says what's wrong in it.
# What each list said in a released or a test version, exactly: anything else is the host's own, which stays, and warns if a name in it means nothing now.
OLD_BUTTONS = {"reports": ([["mute-30m", "kick", "ban-1d", "ban-7d", "ban", "whois", "dismiss"], ["mute", "kick", "ban", "whois", "note"]], ["mute", "kick", "ban", "strike", "warn", "whois", "note"]),
               "appeals": ([["unban", "shorten", "deny"], ["lift", "shorten", "refuse"], ["lift", "shorten", "deny", "close"]], ["lift", "shorten", "deny"])}


def discord_buttons(ctx: Context) -> list[str]:
    """The first beta's lists of buttons held names that no longer mean anything, so those buttons were simply missing."""
    from ..core.tomledit import set_table_key

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []
    said: list[str] = []
    for table, (olds, new) in OLD_BUTTONS.items():
        section = data.get(table)
        if isinstance(section, dict) and section.get("buttons") in olds:
            said.append(f"discord.toml: [{table}] buttons: the ones this version has")
            if not ctx.dry:
                ctx.backup(path)
                set_table_key(path, table, "buttons", new)
    return said


def discord_lines(ctx: Context) -> list[str]:
    """Every setting discord.toml's template has and the host's file doesn't, in whatever table, so each can be seen and changed. Only what's missing: nothing the host set is touched,
    and a line they took out comes back with its default, which is what it does anyway. Before the notes step, so these get their notes in the same update."""
    import tomllib

    from ..core.tomledit import set_nested_key, set_table_key
    from .notes import template_text

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []
    written = template_text("discord.toml")
    one_line: set[tuple[tuple[str, ...], str]] = set()
    table: tuple[str, ...] = ()
    for line in written.splitlines():
        if line.startswith("[") and not line.startswith("[["):
            table = tuple(line.split("#", 1)[0].strip().strip("[]").split("."))
        elif found := re.match(r'"?([\w -]+)"?\s*=\s*\{', line):
            one_line.add((table, found.group(1).strip()))
    missing: list[tuple[tuple[str, ...], str, Any]] = []
    fuller: list[tuple[str, dict[str, Any]]] = []  # a line of [messages] that's there, with fields it hasn't heard of

    def walk(wanted: dict[str, Any], have: dict[str, Any], where: tuple[str, ...]) -> None:
        for key, value in wanted.items():
            if where == ("messages",) and isinstance(value, dict) and isinstance(have.get(key), dict):
                if set(value) - set(have[key]):
                    fuller.append((key, {**value, **have[key]}))
            elif isinstance(value, dict) and value and (where, key) not in LEAVE_ALONE and (where, key) not in one_line:  # written on one line, it's one value: nothing can be added under it
                inner = have.get(key)
                if inner is None or isinstance(inner, dict):
                    walk(value, inner or {}, (*where, key))
            elif key not in have and where:  # the top of the file is set up by the steps that make it
                missing.append((where, key, value))

    walk(tomllib.loads(written), data, ())
    if not missing and not fuller:
        return []
    if not ctx.dry:
        ctx.backup(path)
        for where, key, value in missing:
            set_nested_key(path, where, key, value)
        for key, line in fuller:
            set_table_key(path, "messages", key, line)
    return [f"discord.toml: {len(missing) + len(fuller)} new setting{'s' if len(missing) + len(fuller) != 1 else ''}"]


def discord_old_copies(ctx: Context) -> list[str]:
    """The first beta kept discord.toml as it stood in its folder of old copies, token and all."""
    changed = []
    for copy in sorted((ctx.records / BACKUPS).glob("*/discord.toml")):
        text = copy.read_text(encoding="utf-8")
        if without_secrets(text) != text:
            if not ctx.dry:
                copy.write_text(without_secrets(text), encoding="utf-8")
            changed.append(f"took the token out of the old copy {copy.parent.name}/discord.toml")
    return changed


def discord_appeals_channel(ctx: Context) -> list[str]:
    """Appeals have a channel of their own to set, which is reports until someone does, so nothing moves today."""
    from ..core.tomledit import set_table_key

    path = ctx.home / "discord.toml"
    data = _read(path)
    lines = data.get("messages") if isinstance(data.get("messages"), dict) else {}
    line = lines.get("appeals-made")
    if not path.is_file() or not isinstance(line, dict) or line.get("channel") != "reports":
        return []
    if not ctx.dry:
        ctx.backup(path)
        set_table_key(path, "messages", "appeals-made", {**line, "channel": "appeals"})
    return ["discord.toml: appeals have a channel of their own to set in [channels]: until then, they stay in reports"]


def discord_one_line(ctx: Context) -> list[str]:
    """A line of [messages] written as a table of its own, as an earlier discord-lines did by mistake: the same values, back on one line, so the panels can change it and its notes come back."""
    import tomllib

    from ..core.tomledit import remove_empty_table, remove_nested_key, set_table_key

    path = ctx.home / "discord.toml"
    if not path.is_file() or not _read(path):
        return []
    text = path.read_text(encoding="utf-8")
    keys = re.findall(r"^\[messages\.\"?([\w-]+)\"?\]", text, re.M)
    if not keys:
        return []
    if not ctx.dry:
        ctx.backup(path)
        data = tomllib.loads(text)
        for key in keys:
            line = data["messages"][key]
            for field in list(line):
                remove_nested_key(path, ("messages", key), field)
            remove_empty_table(path, ("messages", key))
            set_table_key(path, "messages", key, line)
    return [f"discord.toml: {len(keys)} line{'s' if len(keys) != 1 else ''} of [messages] back on one line, as they should be"]


def discord_unbuilt(ctx: Context) -> list[str]:
    """Settings for what isn't built gone from discord.toml: they do nothing, and each is something a later version would have to recognise for good. They come back, with their notes, in the version that builds them.
    Left as the first beta wrote it, a setting just goes. Anything else was someone's choice, so it stays as a comment saying so."""
    import json
    from importlib import resources

    from ..core.tomledit import comment_out_nested_key, remove_empty_table, remove_nested_key, remove_table_key, set_nested_key

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []
    import tomllib

    from .notes import template_text

    shipped = json.loads(resources.files("huragok").joinpath("templates/discord-not-built.json").read_text(encoding="utf-8"))
    built_since = tomllib.loads(template_text("discord.toml"))  # a later version built some ([link] in 0.6.1): those stay
    found: list[tuple[tuple[str, ...], str, Any]] = []  # what's still there: each with where it lives
    tables: list[tuple[str, ...]] = []  # deepest last, and taken out the other way round

    def is_built(where: tuple[str, ...], key: str) -> bool:
        table: Any = built_since
        for part in where:
            table = table.get(part) if isinstance(table, dict) else None
        return isinstance(table, dict) and key in table

    built_found: list[tuple[tuple[str, ...], str, Any]] = []  # what's built since: kept, but a first beta's own value gets today's

    def walk(given: dict[str, Any], where: tuple[str, ...]) -> None:
        tables.append(where)
        for key, value in given.items():
            if is_built(where, key):
                built_found.append((where, key, value))
                continue
            if isinstance(value, dict) and (where, key) not in ONE_VALUE_TABLES:
                walk(value, (*where, key))
            else:
                found.append((where, key, value))

    for top in shipped["tables"]:
        if top == "feed":
            kills = (data.get("feed") or {}).get("kills")
            if isinstance(kills, dict):
                walk(kills, ("feed", "kills"))
        elif isinstance(data.get(top), dict):
            walk(data[top], (top,))
    if not found and not tables and not any(key in (data.get("channels") or {}) for key in shipped["channels"]) and not any(key in (data.get("limits") or {}) for key in shipped["limits"]):
        return []
    said: list[str] = []
    gone = 0

    def first_said(where: tuple[str, ...], key: str) -> Any:
        table: Any = shipped["tables"]
        for part in where:
            table = table.get(part) if isinstance(table, dict) else None
        return table.get(key) if isinstance(table, dict) else None

    def default_now(where: tuple[str, ...], key: str) -> Any:
        table: Any = built_since
        for part in where:
            table = table.get(part) if isinstance(table, dict) else None
        return table.get(key) if isinstance(table, dict) else None

    if not ctx.dry:
        ctx.backup(path)
    for where, key, value in built_found:  # the first beta's placeholder, never someone's choice: what it is now
        if value == first_said(where, key) and value != default_now(where, key) and not ctx.dry:
            set_nested_key(path, where, key, default_now(where, key))
    for where, key, value in found:
        if value == first_said(where, key):
            gone += 1
            if not ctx.dry:
                remove_nested_key(path, where, key)
        else:
            said.append(f"discord.toml: [{'.'.join(where)}] {key}: what it's for isn't built yet, so it's a comment for now, as you'd set it")
            if not ctx.dry:
                comment_out_nested_key(path, where, key, f"Not used yet: [{'.'.join(where)}] comes back with the version that builds it:")
    for name in shipped["channels"]:
        reference = (data.get("channels") or {}).get(name)
        if reference is None or is_built(("channels",), name):
            continue
        if str(reference).strip():
            said.append(f"discord.toml: [channels] {name}: what it's for isn't built yet, so it's a comment for now, as you'd set it")
            if not ctx.dry:
                comment_out_nested_key(path, ("channels",), name, "Not used yet: this channel comes back with the version that builds what it's for:")
        else:
            gone += 1
            if not ctx.dry:
                remove_table_key(path, "channels", name)
    for name, first in shipped["limits"].items():
        value = (data.get("limits") or {}).get(name)
        if value is None:
            continue
        if value == first:
            gone += 1
            if not ctx.dry:
                remove_table_key(path, "limits", name)
        else:
            said.append(f"discord.toml: [limits] {name}: what it's for isn't built yet, so it's a comment for now, as you'd set it")
            if not ctx.dry:
                comment_out_nested_key(path, ("limits",), name, "Not used yet: this limit comes back with the version that builds what it's for:")
    if not ctx.dry:
        for where in reversed(tables):
            remove_empty_table(path, where)
    if gone:
        said.append(f"discord.toml: {gone} settings for what isn't built yet are gone: they come back with the version that builds them")
    return said


def discord_moved(ctx: Context) -> list[str]:
    """Every setting that has a new name, or a new home, moved to it with its value. The table's in core/discordmoved.py: a row there reaches hosts with nothing more to do, so this runs at every start and does nothing once the old names have gone."""
    from ..core.discordmoved import KEYS, LINE_FIELDS, LINES
    from ..core.tomledit import remove_nested_key, remove_table_key, rename_nested_key, set_nested_key, set_table_key

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []

    def table_of(parts: tuple[str, ...]) -> dict[str, Any]:
        table: Any = data
        for part in parts:
            table = table.get(part) if isinstance(table, dict) else None
        return table if isinstance(table, dict) else {}

    said: list[str] = []
    console, ranks = table_of(("console",)), table_of(("ranks",))
    backed_up = False

    def backup() -> None:
        nonlocal backed_up
        if not backed_up and not ctx.dry:
            ctx.backup(path)
            backed_up = True

    if "raw" in console or "moderators_raw_console" in ranks:  # two keys for one choice
        rank = "off" if console.get("raw") is False else "moderator" if ranks.get("moderators_raw_console") else "admin"
        said.append(f"discord.toml: who can type console lines is [console] raw_rank now: {rank}")
        backup()
        if not ctx.dry:
            set_nested_key(path, ("console",), "raw_rank", rank)
            remove_nested_key(path, ("console",), "raw")
            remove_nested_key(path, ("ranks",), "moderators_raw_console")
    menu = table_of(("commands",)).get("message_menu")
    if menu is not None:
        said.append("discord.toml: [commands] message_menu is [reports] right_click now")
        backup()
        if not ctx.dry:
            set_nested_key(path, ("reports",), "right_click", "report" in (menu if isinstance(menu, list) else []))
            remove_nested_key(path, ("commands",), "message_menu")
    seeding = table_of(("status", "seeding"))
    if "notify_me" in seeding:
        said.append("discord.toml: [status.seeding] notify_me is [notify] enabled now, a feature of its own")
        backup()
        if not ctx.dry:
            set_nested_key(path, ("notify",), "enabled", bool(seeding["notify_me"]))
            if isinstance(seeding.get("cooldown_minutes"), (int, float)):
                set_nested_key(path, ("notify",), "cooldown_minutes", seeding["cooldown_minutes"])  # it shared this one until now
            remove_nested_key(path, ("status", "seeding"), "notify_me")
    for (where, key), (home, new_key) in KEYS.items():
        if key in ("raw", "moderators_raw_console", "message_menu", "notify_me") or key not in table_of(where):
            continue  # those need their values turned: above
        said.append(f"discord.toml: [{'.'.join(where)}] {key} is [{'.'.join(home)}] {new_key} now")
        backup()
        if ctx.dry:
            continue
        if where == home:
            rename_nested_key(path, where, key, new_key)
        else:
            set_nested_key(path, home, new_key, table_of(where)[key])
            remove_nested_key(path, where, key)
    lines = table_of(("messages",))
    for key, line in list(lines.items()):
        if not isinstance(line, dict):
            continue
        new_key = LINES.get(key, key)
        new_line = {LINE_FIELDS.get(field, field): value for field, value in line.items()}
        if new_key == key and new_line == line:
            continue
        said.append(f"discord.toml: [messages] {key} is {new_key} now" if new_key != key else f"discord.toml: [messages] {key}: remove_after is remove_after_minutes now")
        backup()
        if ctx.dry:
            continue
        if new_key != key:
            remove_table_key(path, "messages", key)
        set_table_key(path, "messages", new_key, new_line)
    commands = table_of(("commands",))
    for key in ("public", "hidden"):
        written = commands.get(key)
        if isinstance(written, list) and "reports" in written:
            said.append(f"discord.toml: [commands] {key}: reports is requests now")
            backup()
            if not ctx.dry:
                set_nested_key(path, ("commands",), key, ["requests" if name == "reports" else name for name in written])
    if isinstance(commands.get("names"), dict) and "reports" in commands["names"]:
        said.append("discord.toml: [commands] names: reports is requests now")
        backup()
        if not ctx.dry:
            set_nested_key(path, ("commands",), "names", {"requests" if name == "reports" else name: value for name, value in commands["names"].items()})
    confirm = console.get("confirm")
    if isinstance(confirm, list) and "reports" in confirm:
        said.append("discord.toml: [console] confirm: reports is requests now")
        backup()
        if not ctx.dry:
            set_nested_key(path, ("console",), "confirm", ["requests" if name == "reports" else name for name in confirm])
    if table_of(("feed", "calls")).get("mods_command") is not None:
        off = table_of(("feed", "calls"))["mods_command"] is False
        said.append("discord.toml: [feed.calls] mods_command is gone" + (": !mods is in [off] in commands.toml now" if off else ""))
        backup()
        if not ctx.dry:
            remove_nested_key(path, ("feed", "calls"), "mods_command")
            if off and ctx.commands_path.is_file():
                from ..core.tomledit import add_to_list

                ctx.backup(ctx.commands_path)
                add_to_list(ctx.commands_path, "off", "commands", "mods")
    return said


# Defaults that turned out too loud, or wrong, where they're still as an earlier version wrote them. Anything else is someone's choice, and stays.
OLD_DEFAULTS: dict[tuple[str, ...], dict[str, tuple[Any, Any]]] = {
    ("limits",): {"status_seconds": (120, 60), "rename_minutes": (10, 5), "voice_status_minutes": (3, 1), "presence_seconds": (120, 30)},
    ("tickets",): {"stale_after_days": (7, 2)},
    ("status", "presence"): {"lines": (["Watching {players_total} Spartans on {servers_up} servers", "{map} · {mode} on {server}"], ["{players_total} Spartans on {servers_up} servers", "{map} · {mode} on {server}"])},
    ("contact",): {"categories": (["Question", "Report a player", "Ban appeal", "Other"], ["Question", "A problem with a server", "Something else"])},
    ("appeals",): {"questions": (["What happened?", "Why should the ban be lifted?"], ["What happened?", "Why should it be lifted?"])},
}


def discord_defaults(ctx: Context) -> list[str]:
    """What's changed since the first beta in what a new file would say, for a file that's still as it was."""
    from ..core.tomledit import set_nested_key

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []
    said: list[str] = []
    for where, values in OLD_DEFAULTS.items():
        table: Any = data
        for part in where:
            table = table.get(part) if isinstance(table, dict) else None
        for key, (old, new) in values.items():
            if isinstance(table, dict) and table.get(key) == old:
                said.append(f"discord.toml: [{'.'.join(where)}] {key}: the new default")
                if not ctx.dry:
                    ctx.backup(path)
                    set_nested_key(path, where, key, new)
    return said


# Tables whose keys are the host's to name: a key that's not there was taken out, and it isn't missing.
ONE_VALUE_TABLES = {(("roles",), "per_server"), (("roles",), "top"), (("chat",), "channels"), (("community",), "team_voice_channels"), (("community",), "self_roles"), (("community",), "level_roles")}  # one-line tables in what isn't built: one value, not keys to walk
LEAVE_ALONE = {(("notes",), "flags"), (("tickets",), "colours"), (("status", "board"), "colours"), (("status", "board"), "join_address"), (("commands",), "names")}
# What the first beta's file said for settings that have gone since: left as it was, it just goes. Anything else was someone's choice.
DEAD_IN_BETA_1: dict[tuple[str, ...], dict[str, Any]] = {
    (): {"time_zone": "local", "date_format": "discord"},
    ("privacy",): {"player_ids": "staff", "opt_out": True, "keep_days": 365, "forget_me": True},
    ('ranks',): {"needs_link": False},
    ('channels',): {"public": ""},
    ('feeds',): {"ping": ""},
    ('feed', 'moderation'): {"channel": "staff", "kinds": ["kick", "ban", "mute", "unmute", "unban", "pardon"], "evidence_lines": 5, "record": True, "case_numbers": True, "threads": True, "buttons": ["undo", "ban-1d", "ban-7d", "ban", "extend-mute", "whois", "said", "note"], "requests": True},
    ('feed', 'vergil'): {"channel": "staff", "kinds": ["strike", "name-ban", "word-ban", "word-allowed", "rule-changed"], "dry_run": True},
    ('feed', 'votes'): {"channel": "staff", "kinds": ["called", "passed", "failed", "cancelled", "novote"], "evidence_lines": 5, "buttons": ["cancel-vote", "whois", "said"]},
    ('feed', 'alerts'): {"channel": "alerts", "ping": "", "ping_for": ["crashed", "faulted", "hung", "start-failed", "update-failed", "huragok-down"], "kinds": ["crashed", "faulted", "hung", "recovered", "watchdog-restart", "start-failed", "update-failed", "part-off", "rcon-sign-in", "relay-lockout", "huragok-restarted", "canary"]},
    ('feed', 'servers'): {"channel": "alerts", "kinds": ["started", "stopped", "restarted", "restart-planned", "countdown", "program-updated", "workshop-updated", "repaired"]},
    ('feed', 'games'): {"channel": "games", "kinds": ["started", "ended"]},
    ('feed', 'joins'): {"channel": "staff", "who": "first-time", "refused": True, "new_names": True, "leaves": False},
    ('feed', 'watch'): {"channel": "staff", "ping": "", "kinds": ["flagged-joined", "back-after-kick", "staff-lookalike-name", "first-time-quiet", "banned-elsewhere", "anti-cheat"]},
    ('feed', 'calls'): {"channel": "staff", "ping": "", "command": "calladmin"},
    ('feed', 'content'): {"channel": "alerts", "kinds": ["playlist", "workshop", "map-skipped"]},
    ('feed', 'audit'): {"channel": "staff", "kinds": ["console", "command", "settings", "roles", "link", "setup"]},
    ('status', 'board'): {"staff_buttons": False, "pin": True, "power_buttons": ["start", "stop", "restart", "restart-after-game", "restart-when-empty", "cancel", "updates"]},
    ('status', 'seeding'): {"role": "", "channel": "public", "notify_channel": ""},
    ('status', 'channel_names'): {"only_on_change": True},
    ('commands',): {"private": ["whois", "said", "bans", "kicks", "mutes", "players", "rcon"], "everyone": ["status", "servers", "stats", "leaderboard", "link", "unlink", "report", "appeal", "game"], "member_menu": ["whois", "link", "stats"]},
    ('reports',): {"channel": "reports", "ping": ""},
    ('appeals',): {"channel": "appeals"},
    ('tickets',): {"enabled": False, "channel": "", "categories": ["Question", "Report a player", "Ban appeal", "Other"], "ping": "", "transcript": True, "close_after_days": 7},
    ('notes',): {"reasons": ["Slurs in chat: 1 day", "Slurs in voice: 7 days", "Team killing: kick", "Cheating: ban"], "evidence_files": True, "appeal_codes": False, "appeal_text": "Appeal at {invite} with code {code}"},
}
# Where a choice someone made has moved to: (the table it's in now, its key).
MOVED_TO = {(("tickets",), "enabled"): (("contact",), "enabled"), (("tickets",), "channel"): (("contact",), "channel"), (("tickets",), "categories"): (("contact",), "categories"), (("tickets",), "ping"): (("contact",), "ping"),
            (("tickets",), "transcript"): (("contact",), "transcript"), (("tickets",), "close_after_days"): (("contact",), "close_after_days"), (("status", "board"), "power_buttons"): (("commands",), "power_buttons"),
            (("feed", "moderation"), "evidence_lines"): (("tickets",), "evidence_lines"), (("feed", "moderation"), "record"): (("tickets",), "record"), (("feed", "moderation"), "case_numbers"): (("tickets",), "case_numbers"), (("feed", "moderation"), "threads"): (("tickets",), "threads")}
# A ping, which is on the lines of [messages] now.
PINGS_NOW_ON = {(("feed", "alerts"), "ping"): ("alerts-crashed", "alerts-faulted", "alerts-hung", "alerts-start-failed"), (("feed", "watch"), "ping"): ("watch-flagged-joined", "watch-back-after-kick", "watch-staff-lookalike", "watch-banned-elsewhere", "watch-anti-cheat"),
                (("feed", "calls"), "ping"): ("calls-called",), (("status", "seeding"), "role"): ("seeding-wanted",), (("reports",), "ping"): ("reports-many",), (("feeds",), "ping"): ()}


def discord_retired(ctx: Context) -> list[str]:
    """Settings that do nothing any more, gone from discord.toml so nobody's left editing a line with no effect. Left as the first beta wrote it, it goes. A host's own value moves to where that lives now, or,
    with no new home, stays as a comment saying so: they chose it, so they should hear what became of it."""
    import tomllib

    from ..core.tomledit import comment_out_nested_key, remove_nested_key, set_nested_key, set_table_key
    from .notes import template_text

    path = ctx.home / "discord.toml"
    data = _read(path)
    if not path.is_file() or not data:
        return []
    said: list[str] = []
    backed_up = False
    gone_quietly = 0
    from ..core.tomledit import remove_empty_table

    for where, gone in DEAD_IN_BETA_1.items():
        table: Any = data
        for part in where:
            table = table.get(part) if isinstance(table, dict) else None
        if not isinstance(table, dict):
            continue
        for key, first in gone.items():
            if key not in table or isinstance(first, dict):
                continue
            value = table[key]
            if value == first:
                gone_quietly += 1
                action = "remove"
            elif (where, key) in MOVED_TO:
                home, new_key = MOVED_TO[(where, key)]
                said.append(f"discord.toml: {'.'.join(where)} {key} moved to [{'.'.join(home)}] {new_key}")
                action = "move"
            elif (where, key) in PINGS_NOW_ON:
                said.append(f"discord.toml: {'.'.join(where)} {key}: each message has its own ping now, and it's on those lines")
                action = "ping"
            else:
                said.append(f"discord.toml: {'.'.join(where) or 'the top'} {key} does nothing any more: it's a comment now, as you'd set it")
                action = "comment"
            if ctx.dry:
                continue
            if not backed_up:
                ctx.backup(path)
                backed_up = True
            if action == "move":
                home, new_key = MOVED_TO[(where, key)]
                set_nested_key(path, home, new_key, value)
            elif action == "ping":
                template = tomllib.loads(template_text("discord.toml"))["messages"]
                written = _read(path).get("messages") or {}
                for line in PINGS_NOW_ON[(where, key)]:
                    set_table_key(path, "messages", line, {**template[line], **(written.get(line) or {}), "ping": value})
            if action == "comment":
                comment_out_nested_key(path, where, key, "Huragok no longer uses this:")
            else:
                remove_nested_key(path, where, key)
    if not ctx.dry:
        remove_empty_table(path, ("privacy",))
    if gone_quietly:
        said.append(f"discord.toml: {gone_quietly} settings that did nothing any more are gone")
    return said


def new_commands(ctx: Context) -> list[str]:
    """Commands newer than the host's commands.toml, put in their default role's list so the host sees them (a
    command in no list has its default role anyway: this changes nobody's rights)."""
    from ..core.commandsfile import OLD_SECTIONS, SECTIONS, TITLES
    from ..core.commandsfile import all_commands, entry_width
    from ..core.tomledit import add_to_list

    path = ctx.commands_path
    data = _read(path)
    if not path.is_file() or not data:
        return []
    listed: set[str] = set()
    for section in [*SECTIONS, *OLD_SECTIONS, "off"]:  # [owner] too, as a file before 0.6.1 has it
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


# The steps that turn a word list from before into this version's: done already for one Huragok writes itself.
WORD_LIST_STEPS = ("0.5.1/automod-layout-note", "0.5.1/automod-order", "0.6.0/automod-reports")
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
    Step("0.6.0/discord-file", "discord.toml, for the Discord bot", discord_file),
    Step("0.6.0/moderators-kick-at-most", "[automod] moderators_kick_at_most", moderators_kick_at_most),
    Step("0.6.0/discord-channels", "discord.toml to tickets, logs, boards and feeds", discord_channels),
    Step("0.6.0/discord-buttons", "the first beta's lists of buttons in discord.toml", discord_buttons),
    Step("0.6.0/discord-retired", "settings in discord.toml that do nothing any more", discord_retired),
    Step("0.6.0/discord-defaults", "defaults in discord.toml that were too loud, where they're unchanged", discord_defaults),
    Step("0.6.0/discord-old-copies", "the token out of old copies of discord.toml", discord_old_copies),
    Step("0.6.0/discord-one-line", "lines of [messages] back on one line each", discord_one_line),
    Step("0.6.0/discord-unbuilt", "settings for what isn't built out of discord.toml", discord_unbuilt),
    Step("0.6.0/discord-appeals-channel", "appeals get a channel of their own to set", discord_appeals_channel),
    Step("0.6.0/workshop-adopt", "[workshop_updates] adopt and layouts", workshop_adopt),
    Step("0.6.0/automod-reports", "the word list's lines with nothing in them go on kicking", automod_reports),
    Step("0.6.0/restart-budget", "[restarts] restarts_per_hour does nothing any more", restart_budget),
    Step("0.6.0/watchdog-memory", "[watchdog] memory only warns unless turned on", watchdog_memory),
    Step("0.6.1/discord-built", "what 0.6.0 kept as notes, for what's built now, back as it was set", discord_built),
    Step("discord-moved", "settings in discord.toml by the names they have now", discord_moved, always=True),
    Step("discord-lines", "new settings in discord.toml", discord_lines, always=True),
    # Once for each version (the one key that changes with it), after the steps above have added what they add.
    Step(f"{__version__}/notes", "Huragok's notes in your files, as this version writes them", notes),
    Step("0.5.1/automod-order", "the word list with the mutes before the kicks", automod_order, settled=True),
    Step("0.6.0/requests-command", "!reports is !requests in commands.toml", requests_command, settled=True),
    Step("0.6.1/faculty", "the top rank is faculty, not owner, wherever it's written", faculty, settled=True),
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


def word_list_written(records: Path) -> None:
    """Huragok has just written the word list from this version's template (a first start, `init`, or a list that was
    deleted): its steps are done, so the next start doesn't take it, or the list the host puts in its place before
    then, for one from before. A first start runs no steps (it only writes the files), so the second ran them all: a
    new host's own list in today's layout, with no note naming its parts, got the old layout's note, then had its mutes
    and kicks swapped for good once the version had settled, and a line that only reports would have been made a kick
    (the playlists thread's audit, 2 November). Only the word list's: the other steps add what a first start doesn't
    write (each server's own interval messages, say)."""
    record = _load_record(records)
    steps = set(record.get("steps") or [])
    if steps >= set(WORD_LIST_STEPS):
        return
    record["steps"] = sorted(steps | set(WORD_LIST_STEPS))
    try:
        _save_record(records, record)
    except OSError as error:
        log.warning("Couldn't write %s (%s): the next start may take the word list for one from before",
                    records / FILES_RECORD, error)


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
