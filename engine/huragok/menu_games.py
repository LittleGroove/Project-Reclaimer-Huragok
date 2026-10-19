"""The window's game screens: a playlist game's own rules and options (G -> Playlists -> Edit -> one game),
and saved custom games (G -> Custom games): made from a game type, copied, edited, checked, played.

Both use one option editor, laid out like the client's Game Options screen: the game type's rules, Rounds
& teams, Respawn, Powerup durations, Map & equipment, Traits (by who gets them, then Shields & health,
Weapons & damage, Movement, Sensors, Appearance), Team characters and World. Every choice is listed
numbered with its label; a number shows its range; R puts back the game type's own value. Nothing is
typed from memory.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import gameoptions as go
from .contentimport import copy_replacing
from .menu_setup import unquote

Row = dict[str, Any]
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
IMAGE_MAX = 4 * 1024 * 1024
PLAYLIST_IMAGES_MAX = 32


class Target:
    """What the option editor changes: a preset file, or one game of a playlist."""

    def __init__(self, get: Callable[[go.Option], Any], put: Callable[[go.Option, Any], None]) -> None:
        self.get = get
        self.put = put


class GameScreens:
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""

    terminal: Any
    controller: Any

    def heading(self, *crumbs: str, note: str | None = None) -> None: ...  # Menu's
    def note(self, text: str, colour: str | None = "gray") -> None: ...
    def listing(self, rows: list[tuple[str, ...]], **options: Any) -> list[Any]: ...
    async def choose(self, prompt: str, options: list[Any], **more: Any) -> str | None: ...
    async def prompt_line(self, prompt: str, default: str = "", *, panel: Any = None) -> str | None: ...
    async def prompt_yes_no(self, prompt: str, default: bool = False) -> bool: ...
    async def wait_any_key(self, seconds: float = 15) -> None: ...
    async def _pages(self, lines: list[Any], colour: str | None = None) -> bool: ...  # SetupScreens'
    async def _pick(self, entries: list[Any], what: str, **more: Any) -> list[Any] | None: ...
    async def _dedicated(self, tool: Any, folder: Path, *words: str, timeout: float = 900) -> Any: ...
    async def _tool(self, settings: Any) -> Any: ...

    # The option editor ------------------------------------------------------------------------

    @staticmethod
    def _changed(target: Target, options: list[go.Option]) -> int:
        return sum(1 for o in options if target.get(o) is not None)

    async def _option_editor(self, crumbs: tuple[str, ...], mode: str | None, target: Target) -> bool:
        """Walk the categories and change values. True when anything changed."""
        changed = False
        while True:
            categories = go.categories(mode)
            sets = go.trait_sets(mode)
            trait_total = sum(len(go.trait_options(key)) for key, _ in sets)
            trait_changed = sum(self._changed(target, go.trait_options(key)) for key, _ in sets)
            teams = go.team_options()
            rows = [(title, f"{self._changed(target, options)} of {len(options)} changed") for title, options in categories]
            rows.append(("Traits", f"{trait_changed} of {trait_total} changed · {len(sets)} sets: who gets them"))
            rows.append(("Team characters", f"{self._changed(target, teams)} of {len(teams)} changed"))
            self.heading(*crumbs, note=f"based on {go.mode_name(mode) if mode else 'its game type'}: each "
                                       "setting plays as the game type has it unless changed here")
            panel = self.listing(rows)
            answer = await self.prompt_line("  Which? (number; Esc when done)", panel=panel)
            if not answer or not answer.strip().isdigit():
                return changed
            number = int(answer)
            if 1 <= number <= len(categories):
                title, options = categories[number - 1]
                changed |= await self._option_rows((*crumbs, title), options, target)
            elif number == len(categories) + 1:
                changed |= await self._traits(crumbs, mode, target)
            elif number == len(categories) + 2:
                changed |= await self._option_rows((*crumbs, "Team characters"), teams, target)

    async def _traits(self, crumbs: tuple[str, ...], mode: str | None, target: Target) -> bool:
        changed = False
        while True:
            sets = go.trait_sets(mode)
            rows = [(key.replace("_", " "), who, f"{self._changed(target, go.trait_options(key))} changed")
                    for key, who in sets]
            self.heading(*crumbs, "Traits", note="pick who gets them; each trait is 'Unchanged' unless set here")
            panel = self.listing(rows)
            answer = await self.prompt_line("  Whose traits? (number; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(sets):
                return changed
            key, who = sets[int(answer) - 1]
            changed |= await self._option_rows((*crumbs, "Traits", key.replace("_", " ")), go.trait_options(key),
                                               target, note=who)

    async def _option_rows(self, crumbs: tuple[str, ...], options: list[go.Option], target: Target, *,
                           note: str | None = None) -> bool:
        changed = False
        while True:
            self.heading(*crumbs, note=note)
            rows, colours, group = [], [], None
            for option in options:
                value = target.get(option)
                shown = option.label_of(value) if value is not None else "(game type's)"
                rows.append((option.label, shown, option.group if option.group != group else ""))
                colours.append(None if value is not None else "gray")
                group = option.group
            panel = self.listing(rows, colours=colours)
            answer = await self.prompt_line("  Which? (number; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(options):
                return changed
            changed |= await self._option_value(crumbs, options[int(answer) - 1], target)

    async def _option_value(self, crumbs: tuple[str, ...], option: go.Option, target: Target) -> bool:
        value = target.get(option)
        self.heading(*crumbs, option.label, note=f"now: {option.label_of(value) if value is not None else 'as the game type has it'}")
        if option.choices:
            keys = [str(n) for n in range(1, len(option.choices) + 1)]
            panel = self.listing([(str(label),) for _, label in option.choices], keys=keys)
            self.note("R puts back the game type's own value.")
            answer = await self.prompt_line("  Which? (number, or R; Esc leaves it)", panel=panel)
            if not answer:
                return False
            if answer.strip().upper() == "R":
                target.put(option, None)
                return value is not None
            if not answer.strip().isdigit() or not 1 <= int(answer) <= len(option.choices):
                self.note("Not one of the numbers; left as it was.", "yellow")
                return False
            target.put(option, option.choices[int(answer) - 1][0])
            return True
        answer = await self.prompt_line(f"  {option.low} to {option.high} (R puts back the game type's own value)",
                                        str(value) if value is not None else "")
        if answer is None or answer.strip() == "":
            return False
        if answer.strip().upper() == "R":
            target.put(option, None)
            return value is not None
        try:
            target.put(option, option.value_of(answer))
        except ValueError as error:
            self.note(f"Not changed: {error}.", "yellow")
            return False
        return True

    # One game of a playlist -------------------------------------------------------------------

    async def _edit_entry(self, server: Any, catalogue: Any, playlist: Any, index: int) -> bool:
        """A playlist game's name, weight, rules and options, vote image, base map, anti-cheat. True: changed."""
        game = playlist.games[index]
        base_of = {e.reference.lower(): e.base for e in catalogue.game_variants if e.base}
        base_of.update({e.name.lower(): e.base for e in catalogue.game_variants if e.base})
        mode = go.entry_mode(game, base_of)
        target = Target(lambda option: go.entry_get(game, option), lambda option, value: go.entry_set(game, option, value))
        changed = False
        crumbs = (server.name, "Playlists", playlist.relative, f"game {index + 1}")
        while True:
            label = playlist.labels()[index]
            overrides = go.describe(mode, game.get("options") or {}, game.get("rules"), game.get("world"))
            self.heading(*crumbs, note=label)
            for line in overrides[:12]:
                self.note(f"  {line}")
            if len(overrides) > 12:
                self.note(f"  … {len(overrides) - 12} more")
            if not overrides:
                self.note("  (plays as its game type has it)")
            choice = await self.choose("Which?", [
                ("N", "Name players see", str(game.get("name") or "(the game type's)")),
                ("W", "Weight", f"{game.get('weight', 1)}: how often ballots offer it"),
                ("O", "Rules and options", f"{len(overrides)} changed"),
                ("I", "Vote image", str(game.get("preview_image") or "(none)")),
                ("B", "Base map", str(game.get("base_map") or "only for a Forge map on a mod map")),
                ("C", "Anti-cheat for this game", "off" if game.get("anti_cheat") is False else "on (the server's)"),
                ("X", "Take out every change", "it plays as its game type has it")], back="Esc goes back")
            if choice is None:
                return changed
            if choice == "N":
                name = await self.prompt_line("  Name players see (- takes it out)", str(game.get("name") or ""))
                if name:
                    if name.strip() == "-":
                        game.pop("name", None)
                    else:
                        game["name"] = name.strip()
                    changed = True
            elif choice == "W":
                weight = await self.prompt_line("  Weight (1-100: 3 comes up three times as often as 1)",
                                                str(game.get("weight", 1)))
                if weight and weight.strip().isdigit():
                    try:
                        playlist.set_weight(index + 1, int(weight))
                        changed = True
                    except Exception as error:  # noqa: BLE001 - PlaylistError
                        self.note(str(error), "yellow")
            elif choice == "O":
                changed |= await self._option_editor((*crumbs, "Rules and options"), mode, target)
            elif choice == "I":
                changed |= await self._vote_image(server, playlist, game)
            elif choice == "B":
                bases = catalogue.mod_maps
                if not bases:
                    self.note("No mod maps installed: base_map is only for a Forge map made on a mod map.", "yellow")
                    continue
                picked = await self._pick(bases, "mod maps", single=True)
                if picked:
                    game["base_map"] = picked[0].reference
                    changed = True
            elif choice == "C":
                if game.get("anti_cheat") is False:
                    game.pop("anti_cheat", None)
                elif await self.prompt_yes_no("  Play this game without the anti-cheat (a game can only turn it off)?",
                                              False):
                    game["anti_cheat"] = False
                changed = True
            elif choice == "X":
                if await self.prompt_yes_no("  Take out its name, rules, options, world and image?", False):
                    for key in ("name", "rules", "options", "world", "preview_image", "anti_cheat", "voice_team_only"):
                        game.pop(key, None)
                    changed = True

    async def _vote_image(self, server: Any, playlist: Any, game: dict[str, Any]) -> bool:
        content = server.content_dir
        if content is None:
            return False
        images = content / "Images"
        files = sorted(p for p in images.glob("*") if p.suffix.lower() in IMAGE_SUFFIXES) if images.is_dir() else []
        used = {g.get("preview_image") for g in playlist.games if g.get("preview_image")}
        self.heading(server.name, "Vote image", note=f"pictures in {images} (PNG or JPEG, 4 MiB at most; a playlist "
                                                     f"can show {PLAYLIST_IMAGES_MAX} different ones)")
        rows = [(p.name, f"{p.stat().st_size // 1024} KB" + (" · in this playlist" if p.name in used else ""))
                for p in files]
        keys = [str(n) for n in range(1, len(rows) + 1)]
        panel = self.listing(rows + [("import a picture", "copy a PNG or JPEG into Images"),
                                     ("no image", "take it out")], keys=keys + ["I", "-"])
        answer = await self.prompt_line("  Which? (number, I or -; Esc leaves it)", panel=panel)
        if not answer:
            return False
        answer = answer.strip()
        if answer == "-":
            game.pop("preview_image", None)
            return True
        if answer.upper() == "I":
            source_text = await self.prompt_line("  The picture's file")
            if not source_text:
                return False
            source = Path(unquote(source_text))
            if not source.is_file() or source.suffix.lower() not in IMAGE_SUFFIXES:
                self.note(f"{source} isn't a PNG or JPEG file.", "yellow")
                return False
            if source.stat().st_size > IMAGE_MAX:
                self.note(f"{source.name} is {source.stat().st_size // 1024} KB: 4 MiB at most.", "yellow")
                return False
            images.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(copy_replacing, source, images / source.name)  # never through a shared link
            game["preview_image"] = source.name
            self.note(f"Copied into {images}; players get it from the server.", "green")
            return True
        if answer.isdigit() and 1 <= int(answer) <= len(files):
            if files[int(answer) - 1].name not in used and len(used) >= PLAYLIST_IMAGES_MAX:
                self.note(f"This playlist shows {PLAYLIST_IMAGES_MAX} images already.", "yellow")
                return False
            game["preview_image"] = files[int(answer) - 1].name
            return True
        return False

    # Saved custom games -----------------------------------------------------------------------

    async def _custom_games(self, settings: Any, tool: Any, server: Any) -> None:
        content = server.content_dir
        if content is None:
            self.note("Its content folder isn't known.", "yellow")
            return
        while True:
            presets = go.list_presets(content)
            self.heading(server.name, "Game content", "Custom games",
                         note=f"saved custom games (game variants) in {content / 'Game Modes'}: a game type with its "
                              "own rules and options, listed with the game types and playable by name")
            rows = [(p.name, p.mode_label, p.relative) for p in presets]
            if rows:
                self.listing(rows)
            else:
                self.note("(none yet)")
            choice = await self.choose("Which?", [
                ("N", "New, from a game type"), ("C", "Copy one"), ("E", "Edit one"), ("S", "Show what one changes"),
                ("P", "Play one on this server"), ("D", "Delete one")])
            if choice is None:
                return
            if choice == "N":
                picked = await self.choose("Based on which game type?",
                                           [(str(n), name) for n, (name, _) in enumerate(go.GAME_TYPES, 1)][:9]
                                           + [("0", go.GAME_TYPES[9][0])])
                if picked is None:
                    continue
                name_, mode = go.GAME_TYPES[9 if picked == "0" else int(picked) - 1]
                title = await self.prompt_line("  Its name (players see it; 48 characters at most)", f"{name_} custom")
                if not title:
                    continue
                await self._edit_preset(settings, tool, server, go.new_preset(title.strip(), mode), new=True)
                continue
            if not presets:
                self.note("Make one first (N).", "yellow")
                continue
            answer = await self.prompt_line("  Which custom game? (number)")
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(presets):
                continue
            info = presets[int(answer) - 1]
            try:
                preset = go.load_preset(info.path)
            except go.PresetError as error:
                self.note(str(error), "yellow")
                continue
            if choice == "C":
                title = await self.prompt_line("  The copy's name", f"{preset.name} copy"[:go.MAX_NAME])
                if title:
                    await self._edit_preset(settings, tool, server, go.clone(preset, title.strip()), new=True)
            elif choice == "E":
                await self._edit_preset(settings, tool, server, preset, new=False)
            elif choice == "S":
                self.heading(server.name, "Custom games", preset.name, note=info.mode_label)
                lines = go.describe(preset.mode, preset.options, preset.rules, preset.world)
                await self._pages(lines or ["(plays as its game type has it)"])
                if preset.scripted:
                    self.note("It runs a script (a scripted mode): its options are here; its script is edited as a "
                              "file (O opens it).")
                await self.wait_any_key(20)
            elif choice == "P":
                from .livesettings import write_plays

                relative = Path(os.path.relpath(info.path, server.folder)).as_posix()
                write_plays(server.config_path, server.name, {"variant": relative})
                server.plays = f"variant {info.path.stem}"
                self.note(f"{server.name} plays {preset.name} now (variant = \"{relative}\"): it restarts once nobody "
                          "is on when the kind of game changes, otherwise from the next game.", "green")
            elif choice == "D":
                if playing := servers_playing(settings, info):
                    self.note(f"Not deleted: {', '.join(playing)} {'plays' if len(playing) == 1 else 'play'} "
                              f"{info.relative}, and a server whose game file is gone doesn't start. Give "
                              f"{'it' if len(playing) == 1 else 'them'} something else to play first (D -> What it "
                              "plays), then delete it.", "yellow")
                    continue
                if await self.prompt_yes_no(f"  Delete {info.relative}?", False):
                    info.path.unlink(missing_ok=True)
                    self.note(f"Deleted {info.relative}." + (" The server writes its shipped scripted modes again at "
                                                             "its next check." if info.scripted else ""), "green")

    async def _edit_preset(self, settings: Any, tool: Any, server: Any, preset: go.Preset, *, new: bool) -> bool:
        target = Target(preset.get, preset.set)
        crumbs = (server.name, "Custom games", preset.name)
        changed = await self._option_editor(crumbs, preset.mode, target)
        if not changed and not new:
            return False
        lines = go.describe(preset.mode, preset.options, preset.rules, preset.world)
        self.heading(*crumbs, note=f"{go.mode_name(preset.mode)} with {len(lines)} change(s)")
        await self._pages(lines or ["(no changes: it plays as the game type has it)"])
        if not await self.prompt_yes_no("  Save it?", True):
            self.note("Not saved.")
            return False
        if problems := preset.problems():
            self.note("Not saved: " + "; ".join(problems), "yellow")
            await self.wait_any_key(8)
            return False
        if tool is not None:
            with tempfile.TemporaryDirectory() as folder:
                copy = preset.check_copy(Path(folder))
                result = await asyncio.to_thread(tool.mode_check, copy)
            if not result.ok:
                self.note(f"The server program refuses it: {result.problem}", "yellow")
                await self.wait_any_key(10)
                return False
            self.note("The server program checked it: valid.", "green")
        path = preset.path if not new and preset.path else go.preset_path(server.content_dir, preset.name)
        try:
            preset.save(path)
        except (go.PresetError, OSError) as error:
            self.note(f"Not saved: {error}", "yellow")
            await self.wait_any_key(8)
            return False
        self.note(f"Saved {path.relative_to(server.content_dir).as_posix()}: the server lists it as \"{preset.name}\" "
                  "within seconds (playlists and What it plays can name it).", "green")
        await self.wait_any_key(6)
        return True


def servers_playing(settings: Any, info: go.PresetInfo) -> list[str]:
    """The servers that play this saved custom game: as their `variant`, or as a game of the playlist they use
    (by its name, its file name or its path in content/). A content folder can be shared, so every server counts."""
    from .playlists import PlaylistError, load_playlist
    from .tomledit import read_toml

    target = os.path.normcase(str(info.path.resolve()))
    names = {info.name.lower(), info.path.name.lower(), info.relative.lower()}
    playing: list[str] = []
    for server in getattr(settings, "servers", []):
        try:
            blocks = read_toml(server.config_path).get("server", [])
        except (OSError, ValueError):
            continue
        block = next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == server.name.lower()),
                     {}) if isinstance(blocks, list) else {}
        variant, playlist = block.get("variant"), block.get("playlist")
        if isinstance(variant, str) and os.path.normcase(str((server.folder / variant).resolve())) == target:
            playing.append(server.name)
        elif isinstance(playlist, str):
            try:
                games = load_playlist((server.folder / playlist).resolve()).games
            except (PlaylistError, OSError, ValueError):
                continue
            if any(str(game.get("game", "")).replace("\\", "/").lower() in names for game in games):
                playing.append(f"{server.name} (in its playlist)")
    return playing
