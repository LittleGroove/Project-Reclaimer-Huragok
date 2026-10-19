"""The window's setup screens: N new server, G game content (what's there, playlists built from
numbered lists or from a mod's maps, each playlist game's own rules and options, custom games,
Workshop items, mods' Forge layouts, imports), C check, I SteamCMD (the Workshop account and Halo 3's
files), and E for each server's files.

Everything the host picks from is listed first, numbered: maps, map variants, game types
and variants, mods, packages, playlists. Nothing has to be typed from memory. The screens
act on files and run the server program's own `dedicated` commands in a thread, so the
dashboard's work goes on meanwhile; a second window (not supervising) can use them too.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from . import desktop, lore, steamsetup
from .catalogue import Catalogue, Entry, describe_games, install_layouts, load_catalogue, parse_picks
from .config import ServerConfig, Settings
from .contentimport import content_root_of
from .dedicated import SLOW, Dedicated, DedicatedError, ToolResult, tool_for
from .livesettings import write_plays
from .newserver import NewServer, SetupError, add_odst_files, create_server, game_folders, has_odst_files, \
    next_free_port, odst_mcc, valid_name
from .packages import Package, PackageError, list_packages, save_package, slug
from .playlists import MIN_GAMES, Playlist, PlaylistError, check_draft, load_playlist, new_playlist, playlist_files, \
    problems_from_show
from .setupcli import parse_item_ids, set_workshop_items, workshop_items
from .watch import parts_for

Row = dict[str, Any]
Line = str | tuple[str, str | None]  # a line for _pages: plain, or with its colour
PAGE = 30
PORTS = (1024, 65535)  # a new server's UDP game port
PLAYERS = (1, 127)  # players at once
CONSOLE_TAIL = 256 * 1024  # how much of the end of a console log is read for what it said lately


class SetupScreens:
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""

    terminal: Any
    controller: Any
    keyboard: Any
    held: Any

    def title(self, text: str, note: str | None = None) -> None: ...  # Menu's
    def heading(self, *crumbs: str, note: str | None = None) -> None: ...
    def note(self, text: str, colour: str | None = "gray") -> None: ...
    def listing(self, rows: list[tuple[str, ...]], **options: Any) -> list[Any]: ...
    def show(self, block: list[Any]) -> None: ...
    async def choose(self, prompt: str, options: list[Any], **more: Any) -> str | None: ...
    async def prompt_line(self, prompt: str, default: str = "", *, panel: Any = None) -> str | None: ...
    async def prompt_key(self, prompt: str, keys: list[str], default: str | None = None, *,
                         panel: Any = None) -> str | None: ...
    async def prompt_yes_no(self, prompt: str, default: bool = False) -> bool: ...
    async def wait_any_key(self, seconds: float = 15) -> None: ...
    async def select_servers(self, verb: str, *, single: bool = False, allowed: Any = None) -> list[Row]: ...
    async def slow(self, work: Any, *args: Any, note: str | None = None, colour: str = "cyan") -> Any: ...

    # Helpers ---------------------------------------------------------------------------------

    def _settings(self) -> Settings:
        return self.controller.refresh_settings()

    async def _tool(self, settings: Settings) -> Dedicated | None:
        try:  # in a thread: picking the newest copy may ask each one its --version
            return await asyncio.to_thread(tool_for, settings)
        except DedicatedError as error:
            self.note(str(error), "red")
            return None

    def _server(self, settings: Settings, row: Row) -> ServerConfig | None:
        try:
            return settings.server(row["name"])
        except KeyError:
            self.note(f"{row['name']} isn't in the settings any more.", "yellow")
            return None

    def _print_result(self, result: ToolResult, limit: int = 80) -> None:
        lines = result.lines
        for line in lines[:limit]:
            colour = "yellow" if not result.ok else ("red" if line.lower().startswith("error") else None)
            self.terminal.print(f"  {line}", colour)
        if len(lines) > limit:
            self.note(f"... {len(lines) - limit} more lines")
        if not lines:
            self.note("(no output)")

    async def _dedicated(self, tool: Dedicated, folder: Path, *words: str, timeout: float = SLOW) -> ToolResult:
        """Run one of the program's commands, its output shown as it comes; Esc ends it early."""
        self.note(f"running: dedicated {' '.join(words)}   (Esc cancels)", "cyan")
        cancel = asyncio.Event()

        def show(line: str) -> None:
            if line.strip():
                colour = "red" if line.lower().startswith("error") else ("yellow" if "skipped" in line.lower() else None)
                self.terminal.print(f"  {line}", colour)

        task = asyncio.ensure_future(tool.stream(folder, *words, on_line=show, cancel=cancel, timeout=timeout,
                                                 config=tool.config_name))
        while not task.done():
            key = await self.keyboard.read(0.25)
            if key in ("ESC", "CTRL_C"):
                cancel.set()
                self.note("cancelling…", "yellow")
                break
        result = await task
        if result.code == 130:
            self.note("cancelled.", "yellow")
        elif not result.ok:
            problem = result.problem
            shown = any(line.strip() == problem for line in result.lines)
            self.note("failed." if shown else f"failed: {problem}", "yellow")
        return result

    async def _follow_console(self, server: ServerConfig, settings: Settings, *, seconds: float = 900,
                              wanted: tuple[str, ...] = ("Workshop", "Skipped", "Error", "Configuration")) -> None:
        """Show a running server's console lines about Workshop items as they appear; Esc stops watching."""
        from .files import FileTail

        path = settings.logs / f"console-{server.slug}.log"
        tail = FileTail(path, from_start=False)
        tail.read_lines()
        self.note(f"watching {path.name} (Esc stops; the server keeps going)", "cyan")
        waited = 0.0
        while waited < seconds:
            for line in tail.read_lines():
                if any(word in line for word in wanted):  # coloured as `watch` colours the same log
                    self.terminal.print_parts([("    ", None), *parts_for(line.strip())])
            key = await self.keyboard.read(0.5)
            waited += 0.5
            if key in ("ESC", "CTRL_C", "ENTER"):
                return

    def _workshop_verdicts(self, server: ServerConfig, settings: Settings) -> list[str]:
        """What the server last said about each Workshop item, from the end of its console log (which grows by
        15 MB a day on a busy server: only the last CONSOLE_TAIL bytes are read, and in a thread)."""
        import re

        path = settings.logs / f"console-{server.slug}.log"
        try:
            lines = tail_lines(path)[-400:]
        except OSError:
            return []
        verdicts: dict[str, str] = {}
        skipped: list[str] = []
        for line in lines:
            match = re.search(r"Workshop (\d+): (.+)$", line)
            if match:
                verdicts[match.group(1)] = match.group(2).strip()
            match = re.search(r"Skipped map package .*[\\/]Workshop[\\/](\d+)[\\/].*?: (.+)$", line)
            if match:
                skipped.append(f"{match.group(1)}: its map was skipped: {match.group(2).strip()} (the item's own "
                               "packaging; ask its author)")
        return [f"{item}: {text}" for item, text in verdicts.items()] + skipped

    def _sayer(self, colour: str = "gray") -> Any:
        """A progress callback safe to call from the worker thread the builder runs in: the line is printed (and
        written to the transcript) on the loop, never from the thread."""
        loop = asyncio.get_running_loop()
        return lambda text: loop.call_soon_threadsafe(self.terminal.print, f"  {text}", colour)

    async def _number_line(self, prompt: str, default: str, bounds: tuple[int, int], what: str) -> str | None:
        """A whole number from low to high, asked again until it is one. None: cancelled."""
        low, high = bounds
        while True:
            text = await self.prompt_line(prompt, default)
            if text is None:
                return None
            text = text.strip()
            if text.isdigit() and low <= int(text) <= high:
                return str(int(text))
            self.note(f"{text!r} isn't {what} from {low} to {high}: type one (Esc leaves it).", "yellow")

    async def _pages(self, lines: list[Line], colour: str | None = None) -> bool:
        """Print lines a page at a time (a line may carry its own colour). False when the host stopped early."""
        for start in range(0, len(lines), PAGE):
            for line in lines[start:start + PAGE]:
                text, own = (line, colour) if isinstance(line, str) else line
                self.terminal.print(f"  {text}", own)
            if start + PAGE < len(lines):
                more = await self.prompt_line(f"  ({len(lines) - start - PAGE} more; Enter shows them, Esc stops)")
                if more is None:
                    return False
        return True

    async def _catalogue(self, tool: Dedicated, settings: Settings, server: ServerConfig) -> Catalogue | None:
        try:
            return await self.slow(load_catalogue, tool, server.folder,
                                   content_root_of(server.folder, settings.config_name), server.config_path,
                                   note="reading what the server can play (dedicated content; keys pressed meanwhile "
                                        "are ignored)…")
        except ValueError as error:
            self.note(f"couldn't read the content listing: {error}", "red")
            return None

    @staticmethod
    def _numbered(entries: list[Any], groups: dict[int, str] | None = None) -> list[Line]:
        """[  1] label lines, with a gray group heading where a group starts."""
        lines: list[Line] = []
        for number, entry in enumerate(entries, 1):
            if groups and number in groups:
                lines.append((f"── {groups[number]} ──", "gray"))
            lines.append(f"[{number:>3}] {entry.label if hasattr(entry, 'label') else entry}")
        return lines

    async def _pick(self, entries: list[Entry], what: str, *, groups: list[tuple[str, int]] | None = None,
                    single: bool = False) -> list[Entry] | None:
        """Choose from a numbered list, narrowed by words first when it's long. None: cancelled."""
        if not entries:
            self.note(f"No {what} to choose from.", "yellow")
            return None
        shown = entries
        if len(entries) > PAGE:
            words = await self.prompt_line(f"  {len(entries)} {what}: words to narrow the list (e.g. mlg), "
                                           "Enter for all")
            if words is None:
                return None
            if words.strip():
                needles = words.lower().split()
                shown = [e for e in entries if all(n in e.label.lower() for n in needles)]
                if not shown:
                    self.note(f"Nothing among the {what} has {words.strip()!r} in its name; the whole list:", "yellow")
                    shown = entries
        boundaries = {number: name for name, number in groups or []} if shown is entries else None
        if not await self._pages(self._numbered(shown, boundaries)):
            return None
        hint = "number" if single else "numbers like 1,3 5-8, or A for all"
        while True:
            answer = await self.prompt_line(f"  Which {what}? ({hint}; Esc cancels)")
            if answer is None:
                return None
            picks = parse_picks(answer, len(shown))
            if picks is not None:
                break
            self.note(f"That's not a list of numbers between 1 and {len(shown)}; try again.", "yellow")
        if single and len(picks) > 1:
            picks = picks[:1]
        return [shown[n - 1] for n in picks]

    def _server_line(self, server: ServerConfig, row: Row) -> str:
        return f"plays {server.plays}  ·  {row['status']}"

    # N: a new server ---------------------------------------------------------------------------

    async def _choose_package(self, settings: Settings) -> Package | None | str:
        """A package from the numbered list with what each plays; None for no package, "cancel" to stop."""
        packages = [p for p in list_packages(settings.packages_folder) if not p.name.endswith("(broken)")]
        while True:
            self.heading("Packages", note=f"in {settings.packages_folder}; a package is a playlist, settings and "
                                           "content a new server starts with")
            rows = [("no package", "Slayer on the five-map example rotation")]
            rows += [(package.name, package.description or package.summary()) for package in packages]
            block = self.listing(rows, keys=[str(n) for n in range(len(rows))], show=False)
            for number in range(len(packages), 0, -1):  # backwards, so the inserts don't shift what's left
                games = _package_games(packages[number - 1])
                if games:
                    block.insert(number + 1, [(f"        {len(games)} games: {describe_games(games)}", "gray")])
            self.show(block)
            answer = await self.prompt_line("  Package? (number; v<number> shows one in full; Enter for none)", "0",
                                            panel=block)
            if answer is None:
                return "cancel"
            answer = answer.strip().lower()
            if answer.startswith("v") and answer[1:].isdigit() and 1 <= int(answer[1:]) <= len(packages):
                package = packages[int(answer[1:]) - 1]
                self.heading(package.name, note=package.description)
                for key, value in package.server.items():
                    self.note(f"[server] {key} = {value}")
                for path in package.playlists:
                    try:
                        playlist = load_playlist(path)
                    except PlaylistError as error:
                        self.note(str(error), "yellow")
                        continue
                    self.note(f"{path.name}: \"{playlist.name}\", vote {playlist.vote_seconds} s")
                    await self._pages([f"  {n}. {label}" for n, label in enumerate(playlist.labels(), 1)])
                if package.workshop_items:
                    self.note(f"Workshop items: {package.workshop_items}")
                continue
            if answer in ("", "0"):
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(packages):
                return packages[int(answer) - 1]
            self.note("That's not one of the packages.", "yellow")

    async def screen_new(self) -> None:
        self.title("NEW SERVER", "a new folder in SERVERS/, made with the server program's `dedicated init`, "
                                 "from a package or the example rotation, then checked")
        settings = self._settings()
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(10)
            return
        self.note(f"It shares the root's {', '.join(settings.setup.share) or 'nothing'} "
                  "([setup] share in settings.toml), so no 5.5 GB copy is made.")
        package = await self._choose_package(settings)
        if package == "cancel":
            return
        assert package is None or isinstance(package, Package)
        self.heading("The new server")
        name = await self.prompt_line("  Server name (also its folder's name)")
        if not name:
            return
        if problem := valid_name(name):
            self.note(problem, "yellow")
            await self.wait_any_key(5)
            return
        if any(s.name.lower() == name.lower() for s in settings.servers):
            self.note(f"A server called {name} exists already.", "yellow")
            await self.wait_any_key(5)
            return
        free = await asyncio.to_thread(next_free_port, settings)  # it tries each port: never on the loop
        port_text = await self._number_line("  UDP game port (1024-65535)", str(free), PORTS, "a port")
        if port_text is None:
            return
        hidden = await self.prompt_yes_no("  Hidden from the server browser?", False)
        password = await self.prompt_line("  Join password (Enter for none)")
        if password is None:
            return
        default_players = str((package.server.get("max_players") if package else None) or 16)
        players_text = await self._number_line("  Players at once (1-127)", default_players, PLAYERS, "a player count")
        if players_text is None:
            return
        share = set(settings.setup.share)
        mcc_folder = settings.setup.mcc_folder
        needs_files = not game_folders(settings)
        if needs_files and mcc_folder is None:
            self.note("No server here has Halo 3's files yet, so they're copied from MCC (hard links on the same "
                      "drive, 5.5 GB otherwise).")
            answer = await self.prompt_line("  Your MCC folder (Enter for Steam's default)")
            if answer is None:
                return
            mcc_folder = unquote(answer)
        # The game folder to link Halo 3's files from (when `game` isn't shared) is create_server's to find
        # (newserver.game_to_link: the root's, then the servers' own), as for the `new` command.
        spec = NewServer(name=name, port=int(port_text), package=package, hidden=hidden, password=password,
                         max_players=int(players_text) if players_text != default_players else None,
                         share=share, mcc_folder=mcc_folder)
        self.heading(name, note=f"UDP {port_text} · {package.summary() if package else 'the example rotation'}"
                                f"{' · hidden' if hidden else ''}{' · join password' if password else ''}")
        if not await self.prompt_yes_no("  Make it?", True):
            return
        self.terminal.print()
        try:
            outcome = await self.slow(create_server, settings, spec, tool, self._sayer())
        except SetupError as error:
            self.note(f"Not made: {error}", "red")
            await self.wait_any_key(15)
            return
        self.terminal.print()
        self.note(f"Made {outcome.folder}" + (f". {lore.MOTTO}" if outcome.ready else ""),
                  "green" if outcome.ready else "yellow")
        self.note(f"RCON password written to its {settings.config_name}; join password "
                  f"{'set' if password else 'none'}.")
        if outcome.check is not None and not outcome.ready:
            self._print_result(outcome.check, 30)
        for note in outcome.notes:
            self.note(note, "yellow")
        self.controller.discover()
        if outcome.ready and await self.prompt_yes_no("  Start it now?", True):
            results = await self.controller.verb("start", outcome.name)
            for _, result in results:
                self.note(f"{outcome.name}: {result.message}", "green" if result.ok else "yellow")
                if not result.ok:
                    self.note("(it appears on the dashboard within 10 s; press S then)")
        await self.wait_any_key(10)

    # G: what a server plays ------------------------------------------------------------------

    async def screen_content(self) -> None:
        self.title("GAME CONTENT", "what a server can play: its maps, variants, game types, playlists, Workshop "
                                   "items and mods")
        settings = self._settings()
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(10)
            return
        picked = await self.select_servers("Look at", single=True)
        if not picked:
            return
        server = self._server(settings, picked[0])
        if server is None:
            return
        catalogue = await self._catalogue(tool, settings, server)
        if catalogue is None:
            await self.wait_any_key(10)
            return
        while True:
            self.heading(server.name, "Game content", note=self._server_line(server, picked[0]))
            self.note(catalogue.counts())
            choice = await self.choose("Which?", [
                ("1", "Browse: maps, variants, game types, mods"), ("2", "Playlists: list, build, edit, use"),
                ("3", "Workshop items and mods"), ("4", "Check it (dedicated check)"),
                ("5", "Save it as a package"), ("6", "Open its folder"),
                ("7", "Import variants from MCC or a folder"), ("8", "Reload the listing"),
                ("9", "Custom games: game types with rules of their own")])
            if choice is None:
                return
            if choice == "1":
                await self._browse(server, catalogue)
            elif choice == "2":
                await self._playlists(settings, tool, server, catalogue)
            elif choice == "3":
                await self._workshop(settings, tool, server, catalogue)
            elif choice == "4":
                self.heading(server.name, "Check")
                await self._dedicated(tool, server.folder, "check")
            elif choice == "5":
                self.heading(server.name, "Save as a package")
                name = await self.prompt_line("  Package name", server.name)
                if not name:
                    continue
                dest = settings.packages_folder / slug(name)
                try:
                    package = save_package(server.folder, server.name, dest, config_name=settings.config_name,
                                           name=name, description=f"Saved from {server.name}")
                except PackageError as error:
                    self.note(f"Not saved: {error}", "yellow")
                    continue
                self.note(f"Saved {package.folder}: {package.summary()}", "green")
            elif choice == "6":
                self.note(desktop.open_path(server.folder))
            elif choice == "7":
                self.heading(server.name, "Import variants")
                await self._import_content(settings, server)
            elif choice == "9":
                await self._custom_games(settings, tool, server)
            if choice in ("7", "8", "9"):
                fresh = await self._catalogue(tool, settings, server)
                if fresh is not None:
                    catalogue = fresh

    async def _browse(self, server: ServerConfig, catalogue: Catalogue) -> None:
        sections = [("Halo 3's maps", catalogue.maps), ("mod maps (installed mods and Workshop items)", catalogue.mod_maps),
                    ("map variants (Forge layouts, .mvar files)", catalogue.map_variants),
                    ("game types", catalogue.game_types), ("game variants (.bin, saved and scripted game types)",
                                                           catalogue.game_variants)]
        while True:
            self.heading(server.name, "Game content", "Browse",
                         note="the first name on a line is what playlists and map = / game = take; [brackets] show "
                              "the file reference when names collide")
            options = [(str(n), f"{len(entries):>4}  {name}") for n, (name, entries) in enumerate(sections, 1)]
            options.append(("6", f"{len(catalogue.mods):>4}  mods in the content folder (what each brings)"))
            options.append(("7", f"{len(catalogue.workshop_items):>4}  Workshop items in dedicated.toml"))
            choice = await self.choose("Which list?", options, columns=1)
            if choice is None:
                return
            if choice in "12345":
                name, entries = sections[int(choice) - 1]
                self.heading(server.name, name)
                shown = entries
                if len(entries) > PAGE:
                    words = await self.prompt_line("  Words to narrow the list (Enter for all)")
                    if words is None:
                        continue
                    if words.strip():
                        shown = [e for e in entries if all(w in e.label.lower() for w in words.lower().split())]
                await self._pages(self._numbered(shown) or ["(none)"])
            elif choice == "6":
                self.heading(server.name, "mods in the content folder")
                await self._pages([f"[{n}] {m.label}  ({m.folder.name})" for n, m in enumerate(catalogue.mods, 1)]
                                  or ["(no mods in the content folder)"])
            elif choice == "7":
                self.heading(server.name, "Workshop items")
                self.note(f"[workshop] items = {catalogue.workshop_items or '[]'}", None)

    # Playlists -------------------------------------------------------------------------------

    async def _playlists(self, settings: Settings, tool: Dedicated, server: ServerConfig, catalogue: Catalogue) -> None:
        while True:
            files = playlist_files(server.folder)
            self.heading(server.name, "Game content", "Playlists", note=f"{server.name} plays: {server.plays}")
            rows: list[tuple[str, ...]] = []
            games: list[str] = []
            for path in files:
                try:
                    playlist = load_playlist(path)
                    summary = f'"{playlist.name}" · {len(playlist.games)} games · vote {playlist.vote_seconds} s'
                    games.append(describe_games(playlist.games, 4))
                except PlaylistError as error:
                    summary = f"(can't read it: {error})"
                    games.append("")
                in_use = server.plays == f"playlist {path.name.removesuffix('.playlist.json')}"
                rows.append((path.name.removesuffix(".playlist.json"), summary + ("   ◄ in use" if in_use else "")))
            block = self.listing(rows, show=False)
            for n in range(len(rows) - 1, -1, -1):
                if games[n]:
                    block.insert(n + 1, [(f"        {games[n]}", "gray")])
            for parts in block:
                self.terminal.print_parts(parts)
            if not files:
                self.note("(no playlists in playlists/ yet)")
            choice = await self.choose("Which?", [
                ("B", "Build one from numbered lists"), ("M", "Build one from a mod's maps"), ("E", "Edit one"),
                ("S", "Show one as the server sees it"), ("U", "Use one for this server"),
                ("F", "Open one in the editor")])
            if choice is None:
                return
            if choice == "B":
                self.heading(server.name, "Playlists", "Build")
                await self._build_playlist(settings, tool, server, catalogue)
                continue
            if choice == "M":
                self.heading(server.name, "Playlists", "Build from a mod's maps")
                if not catalogue.mod_maps:
                    self.note("No mod maps installed: add Workshop items (G -> 3) or copy a mod into content/Mods.",
                              "yellow")
                    continue
                await self._build_playlist(settings, tool, server, catalogue, maps=catalogue.mod_maps)
                continue
            if not files:
                self.note("Build one first.", "yellow")
                continue
            answer = await self.prompt_line("  Which playlist? (number)")
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(files):
                continue
            path = files[int(answer) - 1]
            if choice == "E":
                try:
                    playlist = load_playlist(path)
                except PlaylistError as error:
                    self.note(str(error), "yellow")
                    continue
                await self._edit_playlist(tool, server, catalogue, playlist)
            elif choice == "S":
                self.heading(server.name, "Playlists", path.name)
                await self._dedicated(tool, server.folder, "playlist", "show", f"playlists/{path.name}", timeout=60)
            elif choice == "U":
                self._use_playlist(server, path)
            elif choice == "F":
                self.note(desktop.open_editor(path))

    def _use_playlist(self, server: ServerConfig, path: Path) -> None:
        relative = f"playlists/{path.name}"
        try:
            write_plays(server.config_path, server.name, {"playlist": relative})  # its map, game, campaign… out
        except OSError as error:
            self.note(f"Not changed: {error}", "red")
            return
        server.plays = f"playlist {path.name.removesuffix('.playlist.json')}"
        self.note(f"{server.name} now plays {relative}: from the next vote (auto_reload); the dashboard's events say "
                  "when it's applied.", "green")

    def _map_groups(self, catalogue: Catalogue) -> list[tuple[str, int]]:
        groups = [("Halo 3's maps", 1)]
        if catalogue.mod_maps:
            groups.append(("mod maps", len(catalogue.maps) + 1))
        if catalogue.map_variants:
            groups.append(("map variants (each plays on its own map)", len(catalogue.maps) + len(catalogue.mod_maps) + 1))
        return groups

    def _game_groups(self, catalogue: Catalogue) -> list[tuple[str, int]]:
        groups = [("game types", 1)]
        if catalogue.game_variants:
            groups.append(("game variants", len(catalogue.game_types) + 1))
        return groups

    async def _pick_pairs(self, catalogue: Catalogue,
                          maps_from: list[Entry] | None = None) -> list[tuple[Entry, Entry]] | None:
        self.note("Maps first:", "white")
        if maps_from is not None:
            maps = await self._pick(maps_from, "mod maps")
        else:
            maps = await self._pick(catalogue.all_maps, "maps", groups=self._map_groups(catalogue))
        if not maps:
            self.note("No maps picked, so nothing was built.", "yellow")
            return None
        self.note("Then game types or variants; every map × game pair becomes a game:", "white")
        games = await self._pick(catalogue.all_games, "game types or variants", groups=self._game_groups(catalogue))
        if not games:
            self.note("No game types picked, so nothing was built.", "yellow")
            return None
        pairs = [(m, g) for m in maps for g in games]
        self.note(f"{len(maps)} map(s) × {len(games)} game(s) = {len(pairs)} entries:", "white")
        await self._pages([f"[{n:>3}] {g.name} on {m.name}" for n, (m, g) in enumerate(pairs, 1)])
        answer = await self.prompt_line("  Leave out which? (numbers; Enter keeps them all)")
        if answer is None:
            return None
        out = parse_picks(answer, len(pairs)) or []
        return [pair for n, pair in enumerate(pairs, 1) if n not in out]

    async def _build_playlist(self, settings: Settings, tool: Dedicated, server: ServerConfig,
                              catalogue: Catalogue, maps: list[Entry] | None = None) -> None:
        self.note("Pick maps, then game types; every pair becomes a game players can vote for. "
                  "3 to 128 games; a map variant plays on its own map.")
        if maps is not None:
            self.note("Mod maps are written the way the server names them (workshop:<item>/<map>), so the right "
                      "mod is found; a playlist takes up to 23 mod maps from 16 mods.")
        name = await self.prompt_line("  Playlist name (shown in the lobby)", maps[0].name if maps else "")
        if not name:
            return
        playlist = new_playlist(server.folder, name)
        if playlist.path.exists() and not await self.prompt_yes_no(f"  {playlist.path.name} exists; replace it?", False):
            return
        vote = await self.prompt_line("  Seconds players have to vote (5-120)", "20")
        if vote is None:
            return
        playlist.vote_seconds = int(vote) if vote.isdigit() else 20
        pairs = await self._pick_pairs(catalogue, maps)
        if not pairs:
            return
        for map_entry, game_entry in pairs:
            playlist.add(map_entry.reference, game_entry.reference)
        await self._weights(playlist)
        await self._save_playlist(tool, server, playlist)

    async def _weights(self, playlist: Playlist) -> None:
        answer = await self.prompt_line("  Weights, e.g. 1=3 4=2 (a game with weight 3 comes up three times as often; "
                                        "Enter for none)")
        for part in (answer or "").split():
            if "=" in part:
                number, weight = part.split("=", 1)
                if number.isdigit() and weight.isdigit():
                    try:
                        playlist.set_weight(int(number), int(weight))
                    except PlaylistError as error:
                        self.note(f"{part}: {error}", "yellow")

    async def _save_playlist(self, tool: Dedicated, server: ServerConfig, playlist: Playlist) -> bool:
        """Have the server program check the playlist as a draft, and save it once it can play it: the playlist
        a server plays is never replaced by one it refuses (its next vote would fail). Games it can't play are
        named, and can be dropped; a playlist no server plays yet is saved anyway, to be fixed with [E] edit."""
        in_use = server.plays == f"playlist {playlist.path.name.removesuffix('.playlist.json')}"
        for attempt in range(3):
            self.note(f"Checking {playlist.relative} ({len(playlist.games)} games) with the server program before "
                      "it's saved:", "cyan")
            try:
                result = await asyncio.to_thread(check_draft, playlist, tool, server.folder)
            except PlaylistError as error:
                self.note(f"Not saved: {error}", "yellow")
                return False
            self._print_result(result, 40)
            if result.ok:
                if not self._write_playlist(playlist):
                    return False
                if await self.prompt_yes_no(f"  Use it for {server.name} now?", True):
                    self._use_playlist(server, playlist.path)
                return True
            problems = problems_from_show(result.text)
            if not problems or attempt == 2:
                self._refused(server, playlist, in_use)
                return False
            self.terminal.print()
            self.note(f"The server can't play {len(problems)} of its games:", "yellow")
            for number, why in problems.items():
                label = playlist.labels()[number - 1] if number - 1 < len(playlist.games) else f"game {number}"
                self.note(f"  {number}. {label}: {why}", "yellow")
            if len(playlist.games) - len(problems) < MIN_GAMES:
                self.note(f"Dropping them would leave fewer than {MIN_GAMES} games; add others with [E] edit.", "yellow")
                self._refused(server, playlist, in_use)
                return False
            leave = (("L", f"Leave it unsaved: {server.name} plays this playlist and keeps the one it has") if in_use
                     else ("K", "Keep it as it is: saved, to fix with [E] edit"))
            choice = await self.choose("Which?", [("D", "Drop them and save again"), leave], default="D", back=None)
            if choice != "D":
                self._refused(server, playlist, in_use)
                return False
            playlist.remove(list(problems))
        return False

    def _write_playlist(self, playlist: Playlist) -> bool:
        try:
            playlist.save()
        except (PlaylistError, OSError) as error:
            self.note(f"Not saved: {error}", "yellow")
            return False
        self.note(f"Saved {playlist.relative} ({len(playlist.games)} games).", "green")
        return True

    def _refused(self, server: ServerConfig, playlist: Playlist, in_use: bool) -> None:
        """The server program can't play the playlist as it is: the one a server plays stays as it was, another
        is saved anyway (the work isn't lost) to be fixed with [E] edit."""
        if in_use:
            self.note(f"Not saved: {server.name} plays this playlist, and keeps the one it has until the server "
                      "program can play the new one. Fix it with [E] edit.", "yellow")
        elif self._write_playlist(playlist):
            self.note("The server program can't play it as it is; the file is saved, fix it with [E] edit.", "yellow")

    async def _edit_playlist(self, tool: Dedicated, server: ServerConfig, catalogue: Catalogue,
                             playlist: Playlist) -> None:
        changed = False
        while True:
            self.heading(server.name, "Playlists", playlist.relative,
                         note=f'"{playlist.name}" · vote {playlist.vote_seconds} s · {len(playlist.games)} games'
                              f"{' · changed, not saved yet' if changed else ''}")
            await self._pages([f"[{n:>3}] {label}" for n, label in enumerate(playlist.labels(), 1)])
            choice = await self.choose("Which?", [
                ("A", "Add games (pick maps and game types)"), ("R", "Remove games"), ("W", "Weights"),
                ("G", "Change one game: name, rules and options, vote image"), ("V", "Vote seconds"),
                ("N", "Rename"), ("S", "Save and check")], back="Esc leaves it as it was")
            if choice is None:
                if changed:
                    self.note("Changes dropped.")
                return
            if choice == "A":
                pairs = await self._pick_pairs(catalogue)
                for map_entry, game_entry in pairs or []:
                    changed |= playlist.add(map_entry.reference, game_entry.reference)
            elif choice == "R":
                answer = await self.prompt_line("  Remove which? (numbers)")
                picks = parse_picks(answer or "", len(playlist.games)) or []
                changed |= bool(playlist.remove(picks))
            elif choice == "W":
                await self._weights(playlist)
                changed = True
            elif choice == "G":
                answer = await self.prompt_line("  Which game? (number)")
                if answer and answer.strip().isdigit() and 1 <= int(answer) <= len(playlist.games):
                    changed |= await self._edit_entry(server, catalogue, playlist, int(answer) - 1)
            elif choice == "V":
                vote = await self.prompt_line("  Seconds players have to vote (5-120)", str(playlist.vote_seconds))
                if vote and vote.isdigit():
                    playlist.vote_seconds, changed = int(vote), True
            elif choice == "N":
                name = await self.prompt_line("  Name shown in the lobby", playlist.name)
                if name:
                    playlist.name, changed = name, True
            elif choice == "S":
                if await self._save_playlist(tool, server, playlist):
                    return
                changed = True

    # Workshop and mods -----------------------------------------------------------------------

    async def _workshop(self, settings: Settings, tool: Dedicated, server: ServerConfig, catalogue: Catalogue) -> None:
        while True:
            items = workshop_items(server)
            steam = steamsetup.status(settings)
            running = any(r["name"] == server.name and r["can"]["talk"] for r in self.controller.rows())
            self.heading(server.name, "Game content", "Workshop and mods")
            if steam.executable is None:
                self.note("SteamCMD isn't installed yet: [S] installs it and signs your Steam account in", "yellow")
            else:
                self.note(steam.summary(), "green" if steam.ready else "yellow")
            self.note(f"Items in {server.name}'s {settings.config_name}: {', '.join(str(i) for i in items) or 'none'}",
                      "white")
            for verdict in await asyncio.to_thread(self._workshop_verdicts, server, settings):
                self.note(f"  {verdict}", "yellow" if "skipped" in verdict or "Error" in verdict else "gray")
            mods = catalogue.mods
            if mods:
                self.note(f"Mods installed ({len(mods)}):", "white")
                self.listing([(mod.label,) for mod in mods])
            choice = await self.choose("Which?", [
                ("A", "Add items (IDs or Workshop addresses)"), ("R", "Remove items"),
                ("I", "Fetch them now"), ("L", "Install a mod's Forge layouts into Maps"),
                ("S", "SteamCMD setup (install, sign in)"), ("M", "Mod maps the server lists")])
            if choice is None:
                return
            if choice in ("A", "R"):
                answer = await self.prompt_line("  Item IDs (or addresses), separated by spaces")
                ids = parse_item_ids(answer.split()) if answer else []
                if not ids:
                    continue
                new = [i for i in items if i not in ids] if choice == "R" else items + [i for i in ids if i not in items]
                set_workshop_items(server, new)
                catalogue.workshop_items = new
                self.note(f"items = {new}", "green")
                if choice == "A" and not steam.ready:
                    self.note("SteamCMD isn't set up with a signed-in account, so nothing can download yet: [S] first.",
                              "yellow")
                elif choice == "A" and running:
                    self.note("The running server fetches them itself now (auto_reload) and says so below; a big item "
                              "takes a while.", "cyan")
                    await self._follow_console(server, settings)
                elif choice == "A":
                    self.note("The server is stopped: [I] fetches them now, or it fetches them when it starts.")
            elif choice == "I":
                if not steam.ready:
                    self.note("SteamCMD isn't set up with a signed-in account; [S] sets it up first (an anonymous "
                              "sign-in can't fetch MCC's items).", "yellow")
                    continue
                if running:
                    self.note("The server is running and fetches its items itself; a second SteamCMD beside it would "
                              "wait on the first. Watching the server's console instead:", "cyan")
                    await self._follow_console(server, settings)
                else:
                    await self._dedicated(tool, server.folder, "mods")
                self.note("Playlists name Workshop maps as the listing shows them (workshop:<item ID>/<map>).")
                fresh = await self._catalogue(tool, settings, server)
                if fresh is not None:
                    catalogue.mods, catalogue.mod_maps = fresh.mods, fresh.mod_maps
            elif choice == "L":
                with_layouts = [m for m in mods if m.layouts]
                if not with_layouts:
                    self.note("No installed mod ships Forge layouts (a \"Project Reclaimer Layouts\" folder).", "yellow")
                    continue
                panel = self.listing([(mod.title, ", ".join(n for n, _ in mod.layouts)) for mod in with_layouts])
                answer = await self.prompt_line("  Which mod's layouts? (number)", panel=panel)
                if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(with_layouts):
                    continue
                root = content_root_of(server.folder, settings.config_name)
                copied = install_layouts(with_layouts[int(answer) - 1], root)
                self.note(f"Copied {len(copied)} layout(s) into {root / 'Maps'}: the server lists them as saved maps "
                          "(set base_map to the mod map in a playlist if it asks).", "green")
                fresh = await self._catalogue(tool, settings, server)
                if fresh is not None:
                    catalogue.map_variants = fresh.map_variants
            elif choice == "S":
                await self.screen_steam()
            elif choice == "M":
                self.heading(server.name, "Mod maps")
                await self._pages(self._numbered(catalogue.mod_maps)
                                  or ["(no mod maps installed: add Workshop items, or copy a mod into content/Mods)"])

    # I: SteamCMD -----------------------------------------------------------------------------

    async def screen_steam(self) -> None:
        """SteamCMD and everything it does for the servers: the Workshop account, and Halo 3's files."""
        from . import repair

        while True:
            self.title("STEAMCMD", "Workshop items and Halo 3's files, with one Steam account that owns MCC, signed in "
                                   "once (never a password in any file)")
            settings = self._settings()
            steam = await asyncio.to_thread(steamsetup.status, settings)
            self.heading("SteamCMD")
            self.note(steam.summary(), "green" if steam.ready else "yellow")
            self.heading("Halo 3's files")
            rows = await asyncio.to_thread(repair.game_status, settings)
            for label, text, colour in rows or [("shared game/", "no server folders yet", "gray")]:
                self.note(f"{label:<13} {text}", colour)
            choice = await self.choose("Which?", [
                ("1", "SteamCMD and the Steam account", "find or install it, sign in once, tell every server"),
                ("2", "Get Halo 3's files", f"{repair.DEPOTS['halo3'].holds}"),
                ("3", "Add the campaign missions", "co-op campaign, and every Forge map's campaign vehicles"),
                ("4", "Add Halo 3: ODST's files", "for ODST servers (Firefight, campaign)")])
            if choice is None:
                return
            if choice == "1":
                await self._steam_account()
            else:
                await self._game_files({"2": "halo3", "3": "campaign", "4": "odst"}[choice])

    async def _steam_account(self) -> None:
        self.heading("SteamCMD", "the Steam account", note="servers download Workshop items (and Huragok Halo 3's "
                                                          "files) with SteamCMD; anonymous can't fetch MCC's")
        settings = self._settings()
        steam = steamsetup.status(settings)
        executable = steam.executable
        if executable is not None:  # found: say where, and whether an account is set
            self.note(steam.summary(), "green" if steam.ready else "yellow")
        if executable is None:
            home_folder = settings.home / "steamcmd"
            standard = steamsetup.STANDARD_WINDOWS if desktop.os.name == "nt" else Path.home() / "steamcmd"
            self.heading("SteamCMD isn't installed yet")
            choice = await self.choose("Which?", [
                ("1", f"Install it into {home_folder}", "kept with Huragok"),
                ("2", f"Install it into {standard}", "where SteamCMD's own guide puts it"),
                ("3", "It's somewhere else: name the folder", "")])
            if choice is None:
                return
            if choice == "3":
                answer = unquote(await self.prompt_line("  Folder (or the program itself)") or "")
                if not answer:
                    return
                found, _ = await asyncio.to_thread(steamsetup.find_steamcmd, settings.home, answer)
                if found is None:
                    self.note(f"No {steamsetup.executable_name()} there.", "yellow")
                    await self.wait_any_key(6)
                    return
                executable = found
            else:
                target = home_folder if choice == "1" else standard
                try:
                    executable = await self.slow(steamsetup.install, target, self._sayer())
                except steamsetup.SteamSetupError as error:
                    self.note(str(error), "red")
                    await self.wait_any_key(10)
                    return
        accounts = steamsetup.cached_accounts(executable)
        default = steam.username or (accounts[0] if accounts else "")
        self.heading("The Steam account")
        if accounts:
            self.note(f"Accounts SteamCMD remembers: {', '.join(accounts)}")
        username = await self.prompt_line("  Steam account name (it must own MCC)", default)
        if not username:
            return
        if problem := steamsetup.check_username(username):
            self.note(f"Left as it was: {problem}.", "yellow")
            return
        ok, detail = await self.slow(steamsetup.signed_in, executable, username,  # SteamCMD: up to two minutes
                                     note="checking the sign-in (keys pressed meanwhile are ignored)…")
        if not ok:
            self.note(f"{username} isn't signed in to SteamCMD yet "
                      f"({detail.strip().splitlines()[-1] if detail.strip() else 'no cached sign-in'}).", "yellow")
            self.note(steamsetup.login_window(executable, username), "cyan")
            if await self.prompt_line("  Press Enter once the sign-in window says it's done (Esc to leave it)") is None:
                self.note("Left as it was: nothing was written. Press I again once the sign-in is done.", "yellow")
                await self.wait_any_key(6)
                return
            ok, detail = await self.slow(steamsetup.signed_in, executable, username,
                                         note="checking the sign-in again (keys pressed meanwhile are ignored)…")
            if not ok:
                self.note("Still not signed in; the account stays in settings.toml, try I again later.", "yellow")
        written = steamsetup.configure(settings, executable, username)
        self.note(f"Written: [workshop] steamcmd and username into {len(written)} server folder(s), and [setup] into "
                  "settings.toml.", "green")
        self.note("A server's [workshop] takes effect when its `dedicated` restarts (R); `dedicated mods` "
                  "(G -> 3 -> I) installs items right away.")
        if ok:
            self.note(f"{username} is signed in: the servers can download Workshop items.", "green")
        if await self.prompt_yes_no("  Put SteamCMD's folder on your PATH, for other tools?", False):
            # In a thread: Windows tells every open window about the change and waits on the ones that hang.
            self.note(await asyncio.to_thread(steamsetup.add_to_path, executable.parent))
        if ok:  # servers waiting for SteamCMD (to download their game files) look again now
            await self.controller.verb("game_files_retry")
        await self.wait_any_key(10)

    async def _game_files(self, kind: str) -> None:
        """One kind of game files into this folder's shared game/: every way there is, with sizes, first."""
        from . import repair

        settings = self._settings()
        label = repair.LABELS[kind]
        self.heading("SteamCMD", label)
        target, why = repair.shared_target(settings, kind)
        if target is None:
            self.note(why, "yellow")
            await self.wait_any_key(10)
            return
        depot = repair.DEPOTS[kind]
        found, no_download = await asyncio.to_thread(repair.sources, settings, kind, downloads=True)
        self.note(f"Into {target}: {depot.holds}. What's there stays as it is, and running servers keep running.")
        options = []
        for number, source in enumerate(found, 1):
            if source.how == "mcc":
                options.append((str(number), f"Copy them from MCC at {source.path}",
                                "hard links on the same drive: no space used"))
            else:
                options.append((str(number), f"Download them with SteamCMD as {source.username}",
                                f"{repair.gb(depot.download)} down, {repair.gb(depot.kept)} kept (no sound or video)"))
        if not any(source.how == "mcc" for source in found):
            self.note("MCC with these files isn't on this PC (Steam's own records say where it is: Steam, MCC, "
                      "Properties, DLC adds Halo 3 and ODST).", "yellow")
        if not any(source.how == "steamcmd" for source in found):
            self.note(f"No download: {no_download} (1 sets SteamCMD up).", "yellow")
        if not found:
            await self.wait_any_key(15)
            return
        choice = await self.choose("How?", options)
        if choice is None:
            return
        source = found[int(choice) - 1]
        results = await self.controller.verb("game_files", kind=kind, how=source.how)
        result = results[0][1] if results else None
        if result is not None and result.message not in ("not available",) and \
                not result.message.startswith("unknown request"):
            self.note(result.message, "green" if result.ok else "yellow")
            await self.wait_any_key(15)
            return
        # No running Huragok to hand it to (a window of its own): got here, with the window waiting on it.
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(10)
            return
        try:
            said = await self.slow(repair.fetch, settings, kind, source, target, lambda: tool, self._sayer("cyan"))
        except (OSError, steamsetup.SteamSetupError) as error:
            self.note(f"Not got: {error}", "yellow")
        else:
            self.note(said, "green")
        await self.wait_any_key(15)

    # Importing ---------------------------------------------------------------------------------

    async def _import_content(self, settings: Settings, server: ServerConfig) -> None:
        from .contentimport import find_mcc, import_content, importable

        # Looking for MCC reads Steam's records and walking a folder reads every file name: in a thread, never on the loop.
        found = await asyncio.to_thread(find_mcc, settings.setup.mcc_folder)
        self.note("MCC ships Halo 3's matchmaking map variants (MLG, Team Hardcore, Infection, Grifball…) and game "
                  "variants; the server lists only what's in its content folder.")
        source_text = unquote(await self.prompt_line("  Copy from (your MCC folder, or any folder or file)",
                                                     str(found) if found else "") or "")
        if not source_text:
            return
        source = Path(source_text)
        if not await asyncio.to_thread(source.exists):
            self.note(f"{source} isn't there.", "yellow")
            return
        words = await self.prompt_line("  Only names containing these words (Enter for everything)")
        if words is None:
            return
        items = await asyncio.to_thread(importable, source, words.split())
        if not items:
            self.note("Nothing to import there.", "yellow")
            return
        maps = sum(1 for i in items if i.target == "Maps")
        root = content_root_of(server.folder, settings.config_name)
        self.note(f"{maps} map variant(s) and {len(items) - maps} game variant(s) -> {root}", "white")
        if not await self.prompt_yes_no("  Copy them?", True):
            return
        copied, skipped = await asyncio.to_thread(import_content, items, root)
        self.note(f"Copied {len(copied)}" + (f"; {skipped} were there already" if skipped else "")
                  + ". The server lists them by name now ([1] shows them); playlists can name them.", "green")

    # C: dedicated check ---------------------------------------------------------------------

    async def screen_check(self) -> None:
        self.title("CHECK A SERVER", "the server program's own `dedicated check`: game files, settings, playlists, "
                                     "maps, ports; it installs missing Workshop items")
        settings = self._settings()
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(10)
            return
        picked = await self.select_servers("Check")
        for row in picked:
            server = self._server(settings, row)
            if server is None:
                continue
            self.heading(server.name, "Check")
            result = await self._dedicated(tool, server.folder, "check")
            self.note("ready" if result.ok else "problems found", "green" if result.ok else "yellow")
            await self._after_check(settings, server, result.text)
        if picked:
            await self.wait_any_key(30)

    async def _after_check(self, settings: Settings, server: ServerConfig, text: str) -> None:
        """What a check found that Huragok can do for the host: open the firewall, copy a new engine file."""
        from . import upkeep
        from .contentimport import find_mcc

        commands = upkeep.firewall_commands(text)
        if commands and desktop.os.name == "nt" and await self.prompt_yes_no(
                f"  Open these {len(commands)} port rule(s) in Windows Firewall? (Windows asks for an administrator; "
                "a rule that's there already is left alone)", False):
            self.note(await asyncio.to_thread(upkeep.open_firewall, commands, settings.records), "green")
        game = (server.folder / str(_read_key(server.config_path, "game") or "game")).resolve()
        if upkeep.engine_out_of_date(text):
            mcc = await asyncio.to_thread(find_mcc, settings.setup.mcc_folder)
            files = " and ".join(Path(f).name for f in upkeep.engine_files(game))
            self.note(f"The engine files in {game} don't match this Project Reclaimer: after an MCC update, copy "
                      "them again once a Project Reclaimer release supports that MCC.", "yellow")
            sharing = upkeep.servers_using(settings, game)
            if len(sharing) > 1:
                self.note(f"Every server using {game} gets them: {', '.join(sharing)}. Each needs a Project Reclaimer "
                          "release that supports this MCC.", "yellow")
            if mcc is not None and await self.prompt_yes_no(f"  Copy {files} from {mcc}?", False):
                try:
                    self.note(await asyncio.to_thread(upkeep.copy_engine, game, mcc, sharing), "green")
                except OSError as error:
                    self.note(f"Not copied: {error}", "yellow")
            elif mcc is None:
                await self._engine_from_steam(settings, game, files, sharing)
        if upkeep.odst_files_missing(text):
            await self._offer_odst_files(settings, game)

    async def _engine_from_steam(self, settings: Settings, game: Path, files: str, sharing: list[str]) -> None:
        """No MCC on this PC after an MCC update: the new engine files from Steam with SteamCMD instead (the whole
        multiplayer depot comes down; only the engine files are copied in, the old ones renamed)."""
        from . import repair, upkeep

        found, no_download = await asyncio.to_thread(repair.sources, settings, "halo3", downloads=True)
        steam = next((source for source in found if source.how == "steamcmd"), None)
        if steam is None:
            self.note(f"MCC isn't on this PC, and {no_download}: copy {files} from a PC with MCC, or set SteamCMD up "
                      "(I) to download them.", "yellow")
            return
        depot = repair.DEPOTS["halo3"]
        if not await self.prompt_yes_no(f"  MCC isn't on this PC: download Halo 3's files with SteamCMD "
                                        f"({repair.gb(depot.download)}) and copy the new {files} in?", False):
            return
        tool = await self._tool(settings)
        if tool is None:
            return
        stage = settings.records / "engine-download"
        try:
            downloaded = await self.slow(repair.gather, settings, "halo3", steam, stage, lambda: tool,
                                         self._sayer("cyan"))
            self.note(await asyncio.to_thread(upkeep.copy_engine, game, downloaded, sharing), "green")
        except (OSError, steamsetup.SteamSetupError) as error:
            self.note(f"Not copied: {error}", "yellow")
        finally:
            await asyncio.to_thread(shutil.rmtree, stage, True)

    async def _offer_odst_files(self, settings: Settings, game: Path) -> bool:
        """Halo 3: ODST's files into a game folder that lacks them, from this PC's MCC. True when they're there."""
        if has_odst_files(game):
            return True
        mcc = await asyncio.to_thread(odst_mcc, settings.setup.mcc_folder)
        self.note(f"{game} has no Halo 3: ODST files: an ODST server needs them (about 3.5 GB; hard links from MCC "
                  "on the same drive take no space).", "yellow")
        if mcc is None:
            self.note("No MCC with Halo 3: ODST installed was found on this PC: install it (it's a DLC: Steam, MCC, "
                      f"Properties, DLC), or name MCC's folder as [setup] mcc_folder in {settings.path.name}.",
                      "yellow")
            return False
        tool = await self._tool(settings)
        if tool is None or not await self.prompt_yes_no(f"  Add them from {mcc}?", True):
            return False
        try:  # its progress comes from the worker thread: printed on the loop (_sayer)
            await self.slow(add_odst_files, tool, game, str(mcc), settings.records / "odst-stage", self._sayer("cyan"))
        except (SetupError, OSError) as error:
            self.note(f"Not added: {error}", "yellow")
            return False
        return True

    # E: a file of the supervisor's, or of a server's --------------------------------------

    async def screen_edit(self) -> None:
        self.title("EDIT A FILE", "opens it in your editor; most files are re-read within seconds")
        settings = self._settings()
        files: list[tuple[str, str, Path | ServerConfig]] = [
            ("settings.toml", "how Huragok runs; applies when it starts again", self.controller.home / "settings.toml"),
            ("commands.toml", "who can use which in-game command; applies within seconds", self.controller.home / "commands.toml"),
            ("automod.txt", "Vergil's word list (automod); applies within seconds", self.controller.home / "automod.txt"),
            ("names.txt", "Vergil's banned names; applies within seconds", self.controller.home / "names.txt"),
        ]
        for server in settings.servers:
            files.append((f"{server.name}'s files", f"{settings.config_name}, playlists… in {server.folder}", server))
        self.terminal.print()
        panel = self.listing([(name, what) for name, what, _ in files])
        answer = await self.prompt_line("  Which? (number; Esc cancels)", panel=panel)
        if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(files):
            return
        target = files[int(answer) - 1][2]
        if isinstance(target, Path):
            self.note(desktop.open_editor(target))
            await self.wait_any_key(4)
            return
        await self._edit_server_file(settings, target)

    def server_files(self, settings: Settings, server: ServerConfig) -> list[tuple[str, str, Path]]:
        """What's worth editing in a server folder: (name, what it's for, path)."""
        folder = server.folder
        found: list[tuple[str, str, Path]] = [
            (settings.config_name, "its settings; most apply within seconds (auto_reload)", server.config_path)]
        for path in playlist_files(folder):
            found.append((f"playlists/{path.name}", "a rotation; applies from the next vote", path))
        try:
            from .tomledit import read_toml

            data = read_toml(server.config_path)
            bans = (folder / str(data.get("bans") or "bans.json")).resolve()
            content = (folder / str(data.get("content") or "content")).resolve()
        except (OSError, ValueError):
            bans, content = folder / "bans.json", folder / "content"
        if bans.is_file():
            found.append((bans.name if bans.parent == folder else f"{bans.name} (shared at {bans.parent})",
                          "bans, VPN allowances, console-added admins; applies within seconds", bans))
        if (content / "disabled-mods.json").is_file():
            found.append(("content/disabled-mods.json", "mods kept but not used; restart", content / "disabled-mods.json"))
        modes = content / "Game Modes"
        for path in sorted(modes.glob("*.json"))[:12] if modes.is_dir() else []:
            found.append((f"Game Modes/{path.name}", "a saved game type; picked up live", path))
        return found

    async def _edit_server_file(self, settings: Settings, server: ServerConfig) -> None:
        self.heading(server.name, "Files", note=str(server.folder))
        files = self.server_files(settings, server)
        rows = [(name, what) for name, what, _ in files] + [("open its folder", "")]
        panel = self.listing(rows, keys=[str(n) for n in range(1, len(files) + 1)] + ["O"])
        answer = await self.prompt_line("  Which? (number or O; Esc cancels)", panel=panel)
        if not answer:
            return
        if answer.strip().upper() == "O":
            self.note(desktop.open_path(server.folder))
            await self.wait_any_key(3)
            return
        if not answer.strip().isdigit() or not 1 <= int(answer) <= len(files):
            return
        name, _, path = files[int(answer) - 1]
        self.note(desktop.open_editor(path))
        if name == settings.config_name or name.startswith(("playlists/", "Game Modes/")):
            self.note("Save it and the server re-checks its files a couple of seconds later; the result shows in the "
                      "dashboard's events ('settings reloaded …', or what's wrong).", "cyan")
        await self.wait_any_key(6)


def unquote(text: str) -> str:
    """A typed or pasted path without the quotes around it (Explorer's "Copy as path" adds them)."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def tail_lines(path: Path, size: int = CONSOLE_TAIL) -> list[str]:
    """The lines in the last `size` bytes of a file (the first one dropped when it was cut). Raises OSError."""
    import os

    with path.open("rb") as file:
        end = file.seek(0, os.SEEK_END)
        file.seek(max(0, end - size))
        lines = file.read().decode("utf-8", errors="replace").splitlines()
    return lines[1:] if end > size else lines


def _read_key(config: Path, key: str) -> Any:
    from .tomledit import read_toml

    try:
        return read_toml(config).get(key)
    except (OSError, ValueError):
        return None


def _package_games(package: Package) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    for path in package.playlists:
        try:
            games.extend(load_playlist(path).games)
        except PlaylistError:
            continue
    return games
