"""The first start: settings.toml (the layout and the backend it finds), commands.toml, automod.txt, names.txt,
intervalmessages.txt and the starter packages, each written once and never replaced; and `init`."""

from __future__ import annotations

import argparse
import os
from importlib import resources
from pathlib import Path

from ..content.packages import PACKAGES_FOLDER
from ..core.serverfolders import SERVERS_FOLDER, find_server_folders
from ..core.settings import COMMANDS_FILE, SETTINGS_FILE, load
from ..core.tables import ConfigError
from ..discordbot.config import FILE as DISCORD_FILE, make_file
from ..program.dedicated import find_executable
from . import common
from .common import FAILED, LAUNCHER, SETTINGS_PROBLEM
from .shell import console as out


COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")


def template(name: str) -> str:
    return resources.files("huragok").joinpath(f"templates/{name}").read_text("utf-8")


def detect_layout(home: Path) -> tuple[str, str, list[Path]]:
    """(servers_folder, server_folder, the server folders found) for a first start in `home`."""
    servers = home / SERVERS_FOLDER
    found = find_server_folders(home, servers, None)
    if found:
        return SERVERS_FOLDER, "", found
    if (home / "dedicated.toml").is_file():  # unzipped into a server folder
        return SERVERS_FOLDER, ".", [home]
    if (home.parent / "dedicated.toml").is_file():  # the 0.1 layout: inside the server folder
        return SERVERS_FOLDER, "..", [home.parent]
    return SERVERS_FOLDER, "", []


def pick_backend(folders: list[Path]) -> tuple[str, dict[str, str], str]:
    """(backend, its settings, why) for what the server folders hold."""
    for folder in folders:
        if compose := next((name for name in COMPOSE_FILES if (folder / name).is_file()), None):
            return "docker", {"compose_file": compose}, f"found {compose}: the Superintendent runs the containers"
    for folder in folders:
        if exe := find_executable(folder):
            return "process", {"executable": exe.name}, f"found {exe.name}: the Superintendent runs the servers"
    return "process", {}, "the Superintendent runs the servers: project-reclaimer.exe in each server folder"


def _toml_text(value: str) -> str:
    """A value for settings.toml as the host would type it: a 'literal string' when it can be one, so a Windows
    path's backslashes stay backslashes; otherwise an escaped "basic string"."""
    from ..core.tomledit import _toml_string

    if "'" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return _toml_string(value)
    return f"'{value}'"


def write_files(home: Path, server_folder: str | None = None, backend: str | None = None,
                force: bool = False, settings_path: Path | None = None) -> list[str]:
    """settings.toml (or the settings file named with -c), commands.toml, automod.txt, names.txt and
    intervalmessages.txt, each only if it isn't there (or force; never the word lists or the messages). Returns what
    was written."""
    import tomllib

    from ..core.commandsfile import render_commands_toml

    home.mkdir(parents=True, exist_ok=True)
    written = []
    settings_path = settings_path or home / SETTINGS_FILE
    if force or not settings_path.exists():
        servers_folder, single, folders = detect_layout(home)
        if server_folder is not None:
            single = server_folder
            folders = [Path(home / server_folder).resolve()]
        picked, options, why = pick_backend(folders)
        backend = backend or picked
        if not folders:
            (home / servers_folder).mkdir(exist_ok=True)
        values = {"servers_folder": servers_folder, "server_folder": single, "backend": backend,
                  "executable": options.get("executable", "project-reclaimer.exe"),
                  "compose_file": options.get("compose_file", "compose.yaml")}
        text = template(SETTINGS_FILE).replace("{{", "\x00").replace("}}", "\x01")  # doubled braces are literal
        for key, value in values.items():
            text = text.replace("{" + key + "}", _toml_text(value))
        text = text.replace("\x00", "{").replace("\x01", "}")
        try:  # what's written must read back as written: a path is the likeliest thing to go wrong
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"{settings_path.name} as it would be written can't be read ({error}); "
                              "nothing was written") from None
        if (data.get("servers_folder"), data.get("server_folder")) != (servers_folder, single):
            raise ConfigError(f"{settings_path.name} as it would be written doesn't say the folders given; "
                              "nothing was written")
        settings_path.write_text(text, encoding="utf-8")
        where = (f"{len(folders)} server folder(s) found" if folders
                 else f"no server folders yet: put each one in {servers_folder}{os.sep}")
        written.append(f"{settings_path.name:<15} how it runs. {where}; backend {backend}"
                       + (f" ({why})" if backend == picked else ""))
    commands_path = home / COMMANDS_FILE
    if force or not commands_path.exists():
        commands_path.write_text(render_commands_toml(), encoding="utf-8")
        written.append(f"{COMMANDS_FILE:<15} who can use which in-game command. Put your player ID under [owner]")
    if make_file(home / DISCORD_FILE):
        written.append(f"{DISCORD_FILE:<15} the Discord bot, off until you set it up (J in the window)")
    words = home / "automod.txt"
    if not words.exists():
        words.write_text(template("automod.txt"), encoding="utf-8")
        written.append("automod.txt     Vergil's word list (automod), empty for now; it starts in dry run")
    names = home / "names.txt"
    if not names.exists():
        names.write_text(template("names.txt"), encoding="utf-8")
        written.append("names.txt       Vergil's banned names: kicked as they join until they change it")
    from ..players.messages.intervals import FILE as MESSAGES_FILE, write_template

    if write_template(home / MESSAGES_FILE):
        written.append(f"{MESSAGES_FILE}  interval messages every server says (a Discord link, the rules), none "
                       "yet; each server folder gets one of its own")
    packages = home / PACKAGES_FOLDER
    if not packages.exists():
        from ..content.packages import install_starters

        packages.mkdir(parents=True, exist_ok=True)
        starters = install_starters(packages)
        written.append(f"{PACKAGES_FOLDER + '/':<15} {len(starters)} starter packages for new servers "
                       "(N in the window, or `new`)")
    return written


def layout_help(home: Path) -> list[str]:
    return [
        f"Each server is one folder in {home / SERVERS_FOLDER}: the folder `dedicated init` makes, with",
        "dedicated.toml, project-reclaimer.exe and game/. For example:",
        f"  {home / SERVERS_FOLDER / 'Big Team Battle'}",
        f"  {home / SERVERS_FOLDER / 'Slayer'}",
        "Switch RCON on in each dedicated.toml ([rcon] password). Folders added later are picked up while running.",
    ]


def written_rows(lines: list[str]) -> list[tuple[str, str]]:
    """write_files' lines (`name   what it is`) as (name, what) rows for a listing."""
    rows = []
    for line in lines:
        name, _, what = line.partition("  ")
        rows.append((name.strip(), what.strip()))
    return rows


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config).resolve()
    home = path.parent
    if path.exists() and not args.force:
        out.fail(f"{path} already exists; --force replaces it (and {COMMANDS_FILE}).")
        return FAILED
    try:
        written = write_files(home, args.server_folder, args.backend, force=args.force, settings_path=path)
    except ConfigError as error:
        out.fail(str(error))
        return SETTINGS_PROBLEM
    out.heading("Written", note=str(home))
    out.listing(written_rows(written), index=False, wrap=True)
    out.write()
    try:
        load(path)
    except ConfigError as error:
        out.fail(f"Settings problem in what was written: {error}")
        return SETTINGS_PROBLEM
    out.say(f"Next: {LAUNCHER}{common.config_option(path)} check")
    return 0
