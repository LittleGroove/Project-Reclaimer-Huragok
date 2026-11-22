"""The window's custom games screen (G -> Custom games, and D -> a server -> G): the saved custom games (game modes)
in a server's content folder, listed, shown as they're written, copied to another server and set aside.

Huragok no longer makes or edits custom games, playlists or their options (0.7.0): the host does, by hand (or
imports MCC's). A copy and a set-aside happen only when the host asks: a copy is checked first by the target's own
program (`mode-check`), and when it refuses, its words are shown and the host is asked once; the file a copy replaces
is kept in that server's history first (core/versions.py). Setting one aside moves it out of Game Modes into the
"content set aside" folder beside the content folder (content/catalogue.py set_aside): nothing is deleted.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ...content.catalogue import SET_ASIDE, set_aside
from ...content.mccimport import copy_replacing
from ...core import versions
from ...games import halo3_options as go
from .base import Screens

HISTORY = "custom-games"  # their kept versions: records/custom-games/<server>/<file>/ (core/versions.py)


class GameScreens(Screens):
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""

    async def _copy_custom_game(self, settings: Any, server: Any, info: go.PresetInfo) -> None:
        """A custom game copied, exactly as it is, into the content folders of the other servers the host picks. Each
        copy is checked first by that server's own program (`mode-check`): refused, its words are shown with what
        copying anyway does, and the host is asked once. A server sharing this content folder has it already."""
        from ...program.dedicated import DedicatedError, tool_for

        self.heading(server.name, "Custom games", info.name, "Copy")
        try:
            preset = go.load_preset(info.path)
        except go.PresetError as error:
            self.note(f"Not copied: {error}", "yellow")
            await self.wait_any_key(6)
            return
        for target in await self._other_servers(settings, server, info.name):
            if target.content_dir is None:
                self.note(f"{target.name}: its content folder isn't known.", "yellow")
                continue
            dest = target.content_dir / info.relative
            try:
                if dest.is_file() and dest.resolve() == info.path.resolve():
                    self.note(f"{target.name}: shares this content folder, so it has {info.name} already.", "gray")
                    continue
                if dest.is_file() and dest.read_bytes() == info.path.read_bytes():
                    self.note(f"{target.name}: has it already, the same.", "gray")
                    continue
            except OSError as error:
                self.note(f"{target.name}: not copied: {error}", "yellow")
                continue
            playing = servers_playing(SimpleNamespace(servers=[target]),
                                      SimpleNamespace(path=dest, name=info.name, relative=info.relative))
            if dest.is_file() and not await self.prompt_yes_no(
                    f"  {target.name} has a {dest.name} of its own" + (
                        ", and plays it now: replace it? It plays the copy from its next game" if playing
                        else ": replace it?") + " (its version is kept in its history)", False):
                continue
            refused = ""
            try:
                tool = await asyncio.to_thread(tool_for, settings, target)  # may ask each copy its --version
            except DedicatedError as error:
                refused = f"its server program can't check it ({error})"
            else:
                with tempfile.TemporaryDirectory() as folder:
                    try:
                        result = await asyncio.to_thread(tool.mode_check, preset.check_copy(Path(folder)))
                    except go.PresetError as error:  # an MCC .bin variant: mode-check doesn't read those
                        result, refused = None, ""
                        self.note(f"{target.name}: not checked first: {error}.", "gray")
                    except OSError as error:
                        result, refused = None, f"it couldn't be checked ({error})"
                problem = go.mode_check_problem(result) if result is not None else None
                if problem:  # 0.10.8's mode-check answers 0 for a bad file too: its words say
                    refused = f"its server program refuses it: {problem}"
            if refused:
                self.note(f"{target.name}: {refused}", "yellow")
                self.note(f"Copied anyway, {target.name} skips it as it reads its content folder"
                          + (", and a server whose game is refused doesn't start, and takes no other change to its "
                             "file until it's put right." if playing else ": nothing plays it there now."), "yellow")
                if not await self.prompt_yes_no(f"  Copy it to {target.name} anyway?", False):
                    continue
            try:
                versions.keep_current(settings.records, HISTORY, target.slug, dest)
            except OSError as error:
                self.note(f"{target.name}: not copied: the file it would replace couldn't be kept in the history first "
                          f"({error}).", "yellow")
                continue
            try:
                copy_replacing(info.path, dest)  # a new file swapped in: a hard-linked copy elsewhere keeps its own
            except OSError as error:
                self.note(f"{target.name}: not copied: {error}", "yellow")
                continue
            try:
                versions.keep(settings.records, HISTORY, target.slug, dest, "copied", f"{server.name}'s {info.relative}")
            except OSError as error:
                self.note(f"{target.name}: copied, but its version couldn't be kept in the history: {error}", "yellow")
            self.note(f"{target.name}: copied to {dest.relative_to(target.content_dir).as_posix()}", "green")
        await self.wait_any_key(10)

    async def _set_aside_custom_game(self, settings: Any, server: Any, info: go.PresetInfo) -> None:
        """A custom game moved out of Game Modes into "content set aside" beside the content folder, when the host asks:
        never deleted. A server that plays it is named first, with what setting it aside does, and the host asked once."""
        content = server.content_dir
        aside = content.parent / SET_ASIDE
        if playing := servers_playing(settings, info):
            self.note(f"{', '.join(playing)} {'plays' if len(playing) == 1 else 'play'} {info.relative}. Set aside, it's "
                      f"gone from {'its' if len(playing) == 1 else 'their'} content folder: a server whose game file is "
                      "gone doesn't start, and a playlist naming it is refused.", "yellow")
            question = f"  Set {info.relative} aside anyway (moved to {aside}, never deleted)?"
        else:
            question = f"  Set {info.relative} aside (moved to {aside}, never deleted)?"
        if not await self.prompt_yes_no(question, False):
            return
        moved, kept = await asyncio.to_thread(set_aside, content, [info.path])
        for line in kept:
            self.note(f"Not moved: {line}. Close what holds it, and try again.", "yellow")
        if moved:
            self.note(f"Set aside: {info.relative} is in {moved[0].parent} now; move it back into Game Modes to use it "
                      "again." + (" The server writes the modes it ships again at its next check."
                                  if info.relative.casefold().startswith("game modes/project reclaimer/") else ""),
                      "green")

    async def _custom_games(self, settings: Any, tool: Any, server: Any) -> None:
        content = server.content_dir
        if content is None:
            self.note("Its content folder isn't known.", "yellow")
            return
        while True:
            presets = go.list_presets(content)
            self.heading(server.name, "What it plays", "Custom games",
                         note=f"saved custom games (game variants) in {content / 'Game Modes'}: a game type with its "
                              "own rules and options, listed with the game types and playable by name")
            rows = [(p.name, p.mode_label, p.relative) for p in presets]
            if rows:
                self.listing(rows)
            else:
                self.note("(none)")
            self.note(f"Custom games are made and changed by hand now (or imported from MCC: 6 on G's page); its "
                      f"{settings.config_name} or a playlist names one to play. (N, C, E and P have gone; this note "
                      "shows for one release.)", "yellow")
            choice = await self.choose("Which?", [
                ("S", "Show what one changes"), ("O", "Copy one to other servers (each checked by its own program)"),
                ("D", "Set one aside: moved out of Game Modes, never deleted")])
            if choice is None:
                return
            if not presets:
                self.note(f"There are none to pick in {content / 'Game Modes'}.", "yellow")
                continue
            answer = await self.prompt_line("  Which custom game? (number)")
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(presets):
                continue
            info = presets[int(answer) - 1]
            if choice == "S":
                try:
                    preset = go.load_preset(info.path)
                except go.PresetError as error:
                    self.note(str(error), "yellow")
                    continue
                self.heading(server.name, "Custom games", preset.name, note=f"{info.mode_label} · {info.relative}")
                lines = go.describe(preset.options, preset.rules, preset.world)
                if preset.kind == "bin":
                    lines = ["(an MCC game variant: its settings are in the file, in MCC's own format)"]
                await self._pages(lines or ["(plays as its game type has it)"])
                if preset.scripted:
                    self.note("It runs a script (a scripted mode): its options are here; its script is in the file"
                              + (" (a .rmeg: the settings are its header's)." if preset.kind == "rmeg" else "."))
                await self.wait_any_key(20)
            elif choice == "O":
                await self._copy_custom_game(settings, server, info)
            elif choice == "D":
                await self._set_aside_custom_game(settings, server, info)


def servers_playing(settings: Any, info: Any) -> list[str]:
    """The servers that play this file (a game mode: .json, .rmeg or .bin; a layout; a picture): as their `variant`,
    `game` or `map`, or as a game of a playlist they play, `playlist =` or any of `playlists = [...]` (by its name or
    its path in content/), as the one resolver finds it (content/filehealth.py who_uses: B14). A content folder can be
    shared, so every server counts that uses this one. Playlist files a server has but doesn't play aren't counted."""
    from ...content.filehealth import who_uses

    servers = list(getattr(settings, "servers", []))
    config = getattr(settings, "config_name", "dedicated.toml")
    playing: list[str] = []
    for use in who_uses(servers, Path(info.path), config_name=config if isinstance(config, str) else "dedicated.toml"):
        if not use.in_use:
            continue
        text = use.server if use.how.startswith(("variant", "map", "game")) else f"{use.server} (in its playlist)"
        if text not in playing:
            playing.append(text)
    return playing


__all__ = ["GameScreens", "HISTORY", "servers_playing"]
