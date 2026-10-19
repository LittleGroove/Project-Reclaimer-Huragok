"""Changes to a server that touch more than one setting: renaming it, and giving it a content folder of its
own. Both need the server stopped; each step says what it did, and nothing half-done is left silently.

Renaming moves everything that carries the name: its [[server]] block, its block in settings.toml, its data
folder (logs, chat, status), Huragok's record of it and its console log, and its folder in SERVERS/ when the
folder is named after it. The folder goes first: on Windows a program with a file open in it (an editor,
Explorer's preview, a log viewer) stops that, and then nothing else has changed. A later step that fails puts
every earlier one back, so the server keeps its old name whole; only when putting back fails too does the
error list what's left half-done. A server that's running, starting or waiting to be restarted (backoff: the
Superintendent is about to start it again) isn't renamed.

Its own content folder: a server sharing the root's content/ gets a copy (hard links on the same drive, so no
space is used) and its dedicated.toml points at it. The shared folder stays for the other servers. Workshop
updates after the game need this: the server program swaps a shared item for every server using it at once.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .config import slug_taken, slugify
from .tomledit import read_toml, server_blocks, set_server_setting, set_top_key

if TYPE_CHECKING:
    from .config import ServerConfig, Settings

Say = Callable[[str], None]
STOPPED = ("idle", "faulted", "offline")  # phases where nothing starts the server by itself
_NOT_STOPPED = {"backoff": "the Superintendent is about to restart it (backoff)", "launch": "it's starting",
                "preparing": "it's starting (jobs before the start)", "booting": "it's starting",
                "running": "it's running", "countdown": "it's running (a restart countdown)",
                "external": "it's running (started outside Huragok)", "stopping": "it's stopping",
                "restarting": "it's restarting", "gone": "it isn't on the dashboard now"}


class ServerOpError(Exception):
    partly = False  # True: a step couldn't be put back, so what the caller knew of the server is out of date


def check_stopped(phase: str | None, pid: int | None = None) -> None:
    """Raise ServerOpError unless the server is stopped and nothing is about to start it. `phase` and `pid` are
    the dashboard row's, read just before acting (None: not known, as from the command line)."""
    if pid:
        raise ServerOpError("its program is still running: stop it first (X)")
    if phase is not None and phase not in STOPPED:
        raise ServerOpError(f"{_NOT_STOPPED.get(phase, f'it is {phase}')}: stop it first (X)")


def _moved(path: Path, old: Path, new: Path) -> Path:
    """Where a path inside a renamed folder is now."""
    try:
        return new / path.relative_to(old)
    except ValueError:
        return path


def rename_server(settings: Settings, server: ServerConfig, new_name: str, say: Say = print, *,
                  phase: str | None = None, pid: int | None = None) -> Path:
    """Rename a stopped server everywhere its name is used. Returns its folder (renamed when it was named after it).
    Raises ServerOpError with what happened: nothing changed, everything put back, or what's left half-done."""
    from .newserver import valid_name

    new_name = new_name.strip()
    if problem := valid_name(new_name):
        raise ServerOpError(problem)
    if new_name.lower() == server.name.lower() and new_name == server.name:
        raise ServerOpError("that's its name already")
    if any(s.name.lower() == new_name.lower() for s in settings.servers if s is not server):
        raise ServerOpError(f"a server called {new_name} exists already")
    if clash := slug_taken(settings, new_name, ignore=server.name):
        raise ServerOpError(f"{new_name} and {clash.name} would share their records and logs (both are "
                            f"{clash.slug} to Huragok): pick a name that differs in more than punctuation")
    check_stopped(phase, pid)
    old, old_slug, new_slug = server.name, server.slug, slugify(new_name)
    try:
        named = any(str(b.get("name", "")).lower() == old.lower() for b in server_blocks(server.config_path))
    except (OSError, ValueError) as error:
        raise ServerOpError(f"can't read {server.config_path}: {error}") from None
    if not named:
        raise ServerOpError(f"{server.config_path.name} has no [[server]] block named {old} now (changed since the "
                            "window read it?): nothing was changed; go back and pick it again")
    folder, config, data_dir = server.folder, server.config_path, server.data_dir
    undo: list[tuple[str, Callable[[], object]]] = []
    try:
        if (settings.servers_folder is not None and folder.parent == settings.servers_folder.resolve()
                and folder.name.lower() == old.lower() and not _is_link(folder)):
            target = folder.with_name(new_name)
            if target.exists() and target.resolve() != folder.resolve():
                say(f"its folder stays {folder.name}: {target.name} exists")
            else:
                try:
                    os.replace(folder, target)
                except OSError as error:
                    raise ServerOpError(f"its folder SERVERS/{folder.name} couldn't be renamed "
                                        f"({error.strerror or error}): a program may have a file in it open (an "
                                        "editor, Explorer, a log viewer); close it and try again. Nothing was "
                                        "changed") from None
                undo.append((f"the folder SERVERS/{target.name} back to {folder.name}",
                             lambda source=folder, moved=target: os.replace(moved, source)))
                say(f"folder SERVERS/{folder.name} -> SERVERS/{target.name}")
                config, data_dir = _moved(config, folder, target), _moved(data_dir, folder, target)
                folder = target
        set_server_setting(config, old, "name", new_name)
        undo.append((f"the name in {config.name}", lambda: set_server_setting(config, new_name, "name", old)))
        say(f"{config.name}: name = \"{new_name}\"")
        if old.lower() in settings.server_overrides:
            set_server_setting(settings.path, old, "name", new_name)
            undo.append((f"its block in {settings.path.name}",
                         lambda: set_server_setting(settings.path, new_name, "name", old)))
            say(f"{settings.path.name}: its [[server]] block renamed")
        for source, target in ((data_dir, data_dir.with_name(new_slug)),
                               (settings.records / "servers" / f"{old_slug}.json",
                                settings.records / "servers" / f"{new_slug}.json"),
                               (settings.logs / f"console-{old_slug}.log", settings.logs / f"console-{new_slug}.log")):
            if source.name.startswith(old_slug) and source.exists() and not target.exists() and old_slug != new_slug:
                os.replace(source, target)
                undo.append((f"{target.name} back to {source.name}",
                             lambda source=source, target=target: os.replace(target, source)))
                say(f"moved {source.name} -> {target.name}")
    except (OSError, ServerOpError) as error:  # a TomlEditError is an OSError
        if not undo:
            raise ServerOpError(str(error)) from None
        stuck = []
        for what, step in reversed(undo):
            try:
                step()
            except OSError as undo_error:
                stuck.append(f"{what} ({undo_error})")
        if stuck:
            partial = ServerOpError(f"{error}. It's partly renamed: putting back failed for {'; '.join(stuck)}. "
                                    "Fix those by hand (E), then pick it again")
            partial.partly = True
            raise partial from None
        say("every step put back as it was")
        raise ServerOpError(f"{error}. Every step was put back: it's still {old}") from None
    return folder


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(os.stat(path, follow_symlinks=False), "st_reparse_tag", 0))
    except OSError:
        return False


def shares_content(settings: Settings, server: ServerConfig) -> list[str]:
    return [s.name for s in settings.servers if s is not server and s.content_dir == server.content_dir]


def own_content(settings: Settings, server: ServerConfig, say: Say = print) -> Path:
    """Copy (hard-link) the shared content folder into the server's own folder and point its dedicated.toml at it."""
    shared = server.content_dir
    if shared is None or not shared.is_dir():
        raise ServerOpError("its content folder isn't there")
    target = server.folder / "content"
    if shared.resolve() == target.resolve():
        raise ServerOpError("it has a content folder of its own already")
    if target.exists() and any(target.iterdir()):  # an unused folder from before it shared: kept, set aside
        aside = target.with_name(f"content-unused-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(target, aside)
        say(f"its unused {target.name}/ was set aside as {aside.name}/")
    linked = copied = 0
    for source in shared.rglob("*"):
        relative = source.relative_to(shared)
        destination = target / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
            linked += 1
        except OSError:
            shutil.copy2(source, destination)
            copied += 1
    for sub in ("Maps", "Game Modes", "Images", "Mods"):
        (target / sub).mkdir(parents=True, exist_ok=True)
    set_top_key(server.config_path, "content", "content")
    say(f"{linked + copied} file(s) into {target} ({linked} hard-linked, {copied} copied); content = \"content\"")
    return target


def content_setting(server: ServerConfig) -> str:
    try:
        return str(read_toml(server.config_path).get("content") or "content")
    except (OSError, ValueError):
        return "content"
