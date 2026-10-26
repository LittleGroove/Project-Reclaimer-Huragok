"""The window's setup screens: N new server, G game content (what's there, playlists built from
numbered lists or from a mod's maps, each playlist game's own rules and options, custom games,
Workshop items, mods' Forge layouts, imports), C check, I SteamCMD (the Workshop account and Halo 3's
files), and the list of each server's files that O opens.

Everything the host picks from is listed first, numbered: maps, map variants, game types
and variants, mods, packages, playlists. Nothing has to be typed from memory. The screens
act on files and run the server program's own `dedicated` commands in a thread, so the
dashboard's work goes on meanwhile; a second window (not supervising) can use them too.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from ... import lore, steam
from ...commandline.setup import parse_item_ids, set_workshop_items, workshop_items
from ...content import modcheck
from ...content.catalogue import Catalogue, Entry, describe_games, install_layouts, parse_picks
from ...content.mccimport import content_root_of
from ...content.newserver import (
    NewServer, SetupError, add_odst_files, create_server, game_folders, next_free_port, odst_mcc, valid_name,
)
from ...content.packages import Package, PackageError, list_packages, save_package
from ...core import desktop, versions
from ...core.serverfolders import ServerConfig
from ...core.settings import Settings
from ...core.storage import read_picks
from ...core.text import slug
from ...games import halo3_mapfile
from ...games import files as game_files
from ...games.files import has_odst_files
from ...games.halo3 import MIN_GAMES, Playlist, PlaylistError, load_playlist, new_playlist, playlist_files
from ...players.messages.intervals import own_file
from ...program.dedicated import Dedicated, DedicatedError, tool_for
from ...program.plays import applies_when, check_plays, write_plays
from ...program.settings import path_setting
from .. import statuscard
from ..controllers import Row
from .base import PAGE, Screens

PORTS = (1024, 65535)  # a new server's UDP game port
PLAYERS = (1, 127)  # players at once
CONSOLE_TAIL = 256 * 1024  # how much of the end of a console log is read for what it said lately


class SetupScreens(Screens):
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""


    # Helpers ---------------------------------------------------------------------------------

    def _workshop_verdicts(self, server: ServerConfig, settings: Settings) -> list[str]:
        """What the server last said about each Workshop item, from the end of its console log (which grows by
        15 MB a day on a busy server: only the last CONSOLE_TAIL bytes are read, and in a thread)."""
        import re

        path = settings.console_log(server.slug)
        try:
            lines = tail_lines(path)[-400:]
        except OSError:
            return []
        verdicts: dict[str, str] = {}
        skipped: dict[str, tuple[str, str]] = {}  # the map's path: its item, the server's last reason
        for line in lines:
            match = re.search(r"Workshop (\d+): (.+)$", line)
            if match:
                verdicts[match.group(1)] = match.group(2).strip()
            match = re.search(r"Skipped map package (.*[\\/]Workshop[\\/](\d+)[\\/].*?): (.+)$", line)
            if match:
                skipped[match.group(1)] = (match.group(2), match.group(3).strip().rstrip("."))
        updates = settings.of(server).workshop_updates != "off"
        return [f"{item}: {text}" for item, text in verdicts.items()] + [
            f"{item}: its map {Path(path).name} was skipped: {reason}: "
            + halo3_mapfile.explain(Path(path), reason, updates=updates) for path, (item, reason) in skipped.items()]

    @staticmethod
    def _mod_health(server: ServerConfig, settings: Settings) -> list[tuple[str, str]]:
        """Is each mod good to go (modcheck.py)? Lines with their colours; read in a thread (it opens the maps)."""
        from ...content.gamefiles import read_paths

        paths = read_paths(server.folder, settings.config_name)
        root = server.content_dir or content_root_of(server.folder, settings.config_name)
        reports = modcheck.check_mods(root, paths.game if paths else None)
        if not reports:
            return []
        lines = [("Is each mod good to go?", "white")]
        for report in reports:
            colour = "gray" if report.disabled else "green" if report.good else "red" if report.broken else "yellow"
            lines.append((f"  {report.summary()}", colour))
            for finding in [] if report.disabled else report.findings:
                lines.append((f"    {finding.level}: {finding.text}",
                              {"broken": "red", "warning": "yellow"}.get(finding.level, "gray")))
        return lines

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
            if answer.startswith("v") and answer[1:].isdecimal() and 1 <= int(answer[1:]) <= len(packages):
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
            if answer.isdecimal() and 1 <= int(answer) <= len(packages):
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
        self.title("WHAT IT PLAYS", "what a server plays and how: what it's set to, what's on now and next, its "
                                    "playlists and custom games, and what it can play (maps, variants, mods)")
        settings = self._settings()
        picked = await self.select_servers("Look at", single=True)
        if not picked:
            return
        server = self._server(settings, picked[0])
        if server is not None:
            await self._content_home(settings, server, picked[0])

    async def _content_home(self, settings: Settings, server: ServerConfig, row: Row) -> None:
        """One server's home for what it plays (G, and D -> What it plays): what it's set to, what's on now and next
        and what players pick, with everything that changes it."""
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(10)
            return
        catalogue = await self._catalogue(tool, settings, server)
        if catalogue is None:
            await self.wait_any_key(10)
            return
        while True:
            fresh_row = next((r for r in self.controller.rows() if r.get("name") == server.name), row)
            self.heading(server.name, "What it plays", note=self._server_line(server, fresh_row))
            # Off the event loop: it reads dedicated.toml, the playlist and the ballots kept in the database.
            card = await asyncio.to_thread(statuscard.build, settings, server, fresh_row, {})
            plays = dict(card).get("What it plays", [])
            for label, text, colour in plays:
                self.terminal.print_parts([(f"  {label:<12}", "gray"), (text, colour or "white")])
            self.note(catalogue.counts())
            choice = await self.choose("Which?", [
                ("W", "What it plays: set it (a playlist, one map and game type, a custom game, campaign, ODST)"),
                ("1", "Browse: maps, variants, game types, mods"), ("2", "Playlists: list, build, edit, use, copy"),
                ("3", "Workshop items and mods"), ("4", "Check it (dedicated check)"),
                ("5", "Save it as a package"), ("6", "Open its folder"),
                ("7", "Import variants from MCC or a folder"), ("8", "Reload the listing"),
                ("9", "Custom games: game types with rules of their own")])
            if choice is None:
                return
            if choice == "W":
                await self._edit_plays(settings, server, fresh_row)
                settings = self._settings()
                server = self._server(settings, fresh_row) or server
                continue
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
                dest = settings.packages_folder / slug(name, "package")
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
            self.heading(server.name, "What it plays", "Browse",
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
            for path in files:  # a change made by hand since Huragok last looked, kept in the history
                versions.seen(settings.records, "playlists", server.slug, path)
            self.heading(server.name, "What it plays", "Playlists", note=f"{server.name} plays: {server.plays}")
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
                ("H", "History: every version kept, and put one back"),
                ("P", "Picks: what players picked on its ballots, and what never"),
                ("C", "Copy one to other servers (each checked by its own program)"), ("F", "Open one in the editor")])
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
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(files):
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
                if await self._playable(tool, server, path):
                    self._use_playlist(server, path)
            elif choice == "H":
                await self._playlist_history(settings, tool, server, path)
            elif choice == "P":
                await self._playlist_picks(settings, server, catalogue, path)
            elif choice == "C":
                await self._copy_playlist(settings, server, path)
            elif choice == "F":
                self.note(desktop.open_editor(path))

    async def _other_servers(self, settings: Settings, server: ServerConfig, what: str) -> list[ServerConfig]:
        """Every other server, numbered with what each plays, and the ones picked (A: all); [] when none."""
        others = [s for s in settings.servers if s.name != server.name]
        if not others:
            self.note("There's no other server to copy it to.", "yellow")
            return []
        panel = self.listing([(s.name, f"plays {s.plays}") for s in others])
        answer = await self.prompt_line(f"  Copy {what} to which? (numbers like 1,3, or A for all)", panel=panel)
        picks = parse_picks(answer or "", len(others))
        if picks is None:
            self.note(f"{answer.strip()[:20]} isn't one of the choices (1 to {len(others)}, or A): nothing copied.",
                      "yellow")
            return []
        return [others[n - 1] for n in picks]

    async def _copy_playlist(self, settings: Settings, server: ServerConfig, path: Path) -> None:
        """A playlist copied to other servers, each copy checked by that server's own program (its version, its
        content) before it's saved there: a server that lacks what it names is named, and keeps what it has. Each
        copy goes in that server's playlist history, from this one. Copied, not used: G -> Playlists -> U uses it."""
        try:
            source = load_playlist(path)
        except PlaylistError as error:
            self.note(str(error), "yellow")
            return
        self.heading(server.name, "Playlists", path.name, "Copy")
        for target in await self._other_servers(settings, server, path.name):
            copy = Playlist.from_data(target.folder / "playlists" / path.name, json.loads(json.dumps(source.data())))
            copy.name = source.name
            if copy.path.is_file():
                try:
                    if load_playlist(copy.path).data() == copy.data():
                        self.note(f"{target.name}: has it already, the same.", "gray")
                        continue
                except PlaylistError:
                    pass
                playing = _plays_playlist(target, copy.path)
                if not await self.prompt_yes_no(
                        f"  {target.name} has a {path.name} of its own" + (", and plays it now" if playing else "")
                        + ": replace it (its version is kept in its history)?"
                        + (" It plays the copy from its next vote." if playing else ""), False):
                    continue
            try:
                tool = await asyncio.to_thread(tool_for, settings, target)  # may ask each copy its --version
            except DedicatedError as error:
                self.note(f"{target.name}: not copied: {error}", "yellow")
                continue
            check = await asyncio.to_thread(check_plays, tool, target, {"playlist": copy.relative}, copy)
            if not check.ok:
                self.note(f"{target.name}: not copied: its server program won't play it:", "yellow")
                self._show_problems(copy, check.problems)
                continue
            if self._write_playlist(target, copy, "copied", f"{server.name}'s {path.name}"):
                self.note(f"{target.name}: copied to playlists/{path.name} " + (
                    "(it plays it: from its next vote)" if _plays_playlist(target, copy.path)
                    else "(not used yet: G -> Playlists -> U)"), "green")
        await self.wait_any_key(10)

    async def _playlist_picks(self, settings: Settings, server: ServerConfig, catalogue: Catalogue, path: Path) -> None:
        """What players picked on a playlist's ballots, game by game, most picked first, from what Huragok kept of
        each server's log ("Playlist selected: …"); a game never picked is marked, and a pick of a game the
        playlist no longer has is listed apart."""
        try:
            playlist = load_playlist(path)
        except PlaylistError as error:
            self.note(str(error), "yellow")
            return
        picks, since = await asyncio.to_thread(read_picks, settings.database, playlist.name)
        counted = sum(times for _, times, _ in picks)
        self.heading(server.name, "Playlists", path.name, "Picks",
                     note=f'"{playlist.name}": {counted} ballot(s) counted on every server that plays it'
                          + (f", since {time.strftime('%Y-%m-%d', time.localtime(since))}" if since else ""))
        by_label = {label.casefold(): (times, last) for label, times, last in picks}
        rows: list[tuple[int, str, str, str | None]] = []
        matched: set[str] = set()
        for game in playlist.games:
            label = picked_label(game, catalogue)
            times, last = by_label.get(label.casefold(), (0, 0.0))
            matched.add(label.casefold())
            share = f"{100 * times // counted}%" if counted and times else ""
            rows.append((times, label, f"{times} pick(s) {share}".strip() + (
                f" · last {time.strftime('%Y-%m-%d', time.localtime(last))}" if times else ""),
                None if times else "yellow"))
        rows.sort(key=lambda row: -row[0])
        lines: list[Any] = [(f"{label:<50} {about if times else 'never picked'}", colour or "white")
                            for times, label, about, colour in rows]
        gone = [(label, times) for label, times, _ in picks if label.casefold() not in matched]
        if gone:
            lines.append(("Picked, but not in the playlist now:", "gray"))
            lines += [(f"  {label:<48} {times} pick(s)", "gray") for label, times in gone]
        if not picks:
            lines.append(("(none counted yet: Huragok counts each ballot's pick from the server's log while it "
                          "runs)", "gray"))
        await self._pages(lines)
        await self.wait_any_key(30)

    async def _playlist_history(self, settings: Settings, tool: Dedicated, server: ServerConfig, path: Path) -> None:
        """Every version of a playlist Huragok kept (core/versions.py), newest first: what each holds, how it was
        made and where it came from; one is put back through the same check as any save."""
        while True:
            kept = versions.versions(settings.records, "playlists", server.slug, path.name)
            self.heading(server.name, "Playlists", path.name, "History",
                         note=f"{len(kept)} version(s) kept in {settings.records / 'playlists' / server.slug}")
            if not kept:
                self.note("(none kept yet: a version is kept each time it changes)")
                await self.wait_any_key(5)
                return
            rows = []
            for version in kept:
                try:
                    playlist = Playlist.from_data(path, json.loads(versions.read(version)))
                    holds = f'"{playlist.name}" · {len(playlist.games)} games'
                except (OSError, ValueError, PlaylistError):
                    holds = "(can't be read)"
                rows.append((version.stamp, f"{version.how}{f' ({version.source})' if version.source else ''}"
                             f" · {holds}"))
            self.listing(rows)
            choice = await self.choose("Which?", [("S", "Show one's games"), ("P", "Put one back")])
            if choice is None:
                return
            answer = await self.prompt_line("  Which version? (number)")
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(kept):
                continue
            version = kept[int(answer) - 1]
            try:
                playlist = Playlist.from_data(path, json.loads(versions.read(version)))
            except (OSError, ValueError, PlaylistError) as error:
                self.note(f"Can't read that version: {error}", "yellow")
                continue
            if choice == "S":
                self.heading(server.name, "Playlists", path.name, version.stamp)
                await self._pages([f"[{n:>3}] {label}" for n, label in enumerate(playlist.labels(), 1)])
                await self.wait_any_key(20)
            elif await self._save_playlist(tool, server, playlist, how="put back",
                                           source=f"the version of {version.stamp}"):
                return

    async def _playable(self, tool: Dedicated, server: ServerConfig, path: Path) -> bool:
        """Whether the server program can play a playlist file as it is, as this server's (check_plays): what it
        can't is named, and it isn't used (a playlist the server refuses holds up every other change to its file,
        and its start)."""
        check = await asyncio.to_thread(check_plays, tool, server, {"playlist": f"playlists/{path.name}"})
        if check.ok:
            if not check.asked:
                self.note(f"Not checked by the server program: {check.unasked}.", "yellow")
            return True
        self.note(f"Not used: the server program won't play {path.name} as it is:", "yellow")
        for problem in check.problems:
            self.note(f"  {problem.text}", "yellow")
        self.note("E edits it (each game, or take one out); then use it.", "yellow")
        return False

    def _use_playlist(self, server: ServerConfig, path: Path) -> None:
        relative = f"playlists/{path.name}"
        before = server.plays_kind
        try:
            write_plays(server.config_path, server.name, {"playlist": relative})  # its map, game, campaign… out
        except OSError as error:
            self.note(f"Not changed: {error}", "red")
            return
        server.plays, server.plays_kind = f"playlist {path.name.removesuffix('.playlist.json')}", "playlist"
        self.note(f"{server.name} now plays {relative}: {applies_when(before, 'playlist')} (auto_reload); the "
                  "dashboard's events say when it's applied.", "green")

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
        playlist.vote_seconds = int(vote) if vote.isdecimal() else 20
        pairs = await self._pick_pairs(catalogue, maps)
        if not pairs:
            return
        for map_entry, game_entry in pairs:
            playlist.add(map_entry.reference, game_entry.reference)
        await self._weights(playlist)
        await self._save_playlist(tool, server, playlist, catalogue=catalogue)

    async def _weights(self, playlist: Playlist) -> None:
        answer = await self.prompt_line("  Weights, e.g. 1=3 4=2 (a game with weight 3 comes up three times as often; "
                                        "Enter for none)")
        for part in (answer or "").split():
            if "=" in part:
                number, weight = part.split("=", 1)
                if number.isdecimal() and weight.isdecimal():
                    try:
                        playlist.set_weight(int(number), int(weight))
                    except PlaylistError as error:
                        self.note(f"{part}: {error}", "yellow")

    async def _save_playlist(self, tool: Dedicated, server: ServerConfig, playlist: Playlist, *, how: str = "saved in G",
                             source: str = "", catalogue: Catalogue | None = None, from_editor: bool = False) -> bool:
        """Saved only once the server's own program can play it (program/plays.check_plays: the check its start
        and its reload make), so a playlist it would refuse is never written. When it can't, each problem is shown
        with the game it's about, and the fixes are offered there: take those games out and check again, or change
        them in the editor. Leaving keeps the draft in the playlist's history, unsaved."""
        while True:
            self.note(f"Checking {playlist.relative} ({len(playlist.games)} games) with the server program before "
                      "it's saved:", "cyan")
            check = await asyncio.to_thread(check_plays, tool, server, {"playlist": playlist.relative}, playlist)
            if check.ok:
                if not check.asked:
                    self.note(f"The server program couldn't be asked ({check.unasked}); Huragok's own checks passed.",
                              "yellow")
                if not self._write_playlist(server, playlist, how, source):
                    return False
                if await self.prompt_yes_no(f"  Use it for {server.name} now?", True):
                    self._use_playlist(server, playlist.path)
                return True
            self.terminal.print()
            self.note("The server program won't play it as it is, so it isn't saved:", "yellow")
            self._show_problems(playlist, check.problems)
            games = sorted({p.game for p in check.problems if p.game is not None and p.game <= len(playlist.games)})
            fixes: list[tuple[str, str]] = []
            if games and len(playlist.games) - len(games) >= MIN_GAMES:
                fixes.append(("T", f"Take out game{'s' if len(games) > 1 else ''} {', '.join(map(str, games))}, "
                                   "and check again"))
            if from_editor:
                fixes.append(("E", "Back to the editor to change it (nothing is saved yet)"))
            elif catalogue is not None:
                fixes.append(("E", "Change it now, in the editor (nothing is saved yet)"))
            fixes.append(("L", "Leave it unsaved (the draft is kept in its history)"))
            choice = await self.choose("Which?", fixes, default=fixes[0][0], back=None)
            if choice == "T":
                playlist.remove(games)
                continue
            if choice == "E" and from_editor:
                return False
            if choice == "E" and catalogue is not None:
                await self._edit_playlist(tool, server, catalogue, playlist)
                return False  # the editor saves through here again, or leaves it
            self._keep_draft(server, playlist, check.problems[0].text)
            return False

    def _show_problems(self, playlist: Playlist, problems: list[Any]) -> None:
        labels = playlist.labels()
        for problem in problems:
            if problem.game is not None and problem.game <= len(labels):
                said = re.sub(rf"^Game {problem.game}:?\s*", "", problem.text)
                self.note(f"  game {problem.game} ({labels[problem.game - 1]}): {said}", "yellow")
            else:
                self.note(f"  {problem.text}", "yellow")

    def _keep_draft(self, server: ServerConfig, playlist: Playlist, why: str) -> None:
        try:
            kept = versions.keep_text(self._settings().records, "playlists", server.slug, playlist.path.name,
                                      json.dumps(playlist.data(), indent=2, ensure_ascii=False) + "\n",
                                      f"draft, not saved: {why[:120]}")
        except OSError as error:
            self.note(f"Not saved, and the draft couldn't be kept: {error}", "yellow")
            return
        self.note(f"Not saved: {playlist.relative} stays as it was"
                  + ("; the draft is in its history (Playlists -> H)." if kept else "."), "yellow")

    def _write_playlist(self, server: ServerConfig, playlist: Playlist, how: str = "saved in G", source: str = "") -> bool:
        """Saved, with the version it replaces and the one it writes kept in the history."""
        records = self._settings().records
        if playlist.path.is_file():
            versions.seen(records, "playlists", server.slug, playlist.path)
        try:
            playlist.save()
        except (PlaylistError, OSError) as error:
            self.note(f"Not saved: {error}", "yellow")
            return False
        try:
            versions.keep(records, "playlists", server.slug, playlist.path, how, source)
        except OSError as error:
            self.note(f"Saved, but its version couldn't be kept in the history: {error}", "yellow")
        self.note(f"Saved {playlist.relative} ({len(playlist.games)} games).", "green")
        return True

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
                if answer and answer.strip().isdecimal() and 1 <= int(answer) <= len(playlist.games):
                    changed |= await self._edit_entry(server, catalogue, playlist, int(answer) - 1)
            elif choice == "V":
                vote = await self.prompt_line("  Seconds players have to vote (5-120)", str(playlist.vote_seconds))
                if vote and vote.isdecimal():
                    playlist.vote_seconds, changed = int(vote), True
            elif choice == "N":
                name = await self.prompt_line("  Name shown in the lobby", playlist.name)
                if name:
                    playlist.name, changed = name, True
            elif choice == "S":
                if await self._save_playlist(tool, server, playlist, catalogue=catalogue, from_editor=True):
                    return
                changed = True

    # Workshop and mods -----------------------------------------------------------------------

    async def _workshop(self, settings: Settings, tool: Dedicated, server: ServerConfig, catalogue: Catalogue) -> None:
        while True:
            items = workshop_items(server)
            status = steam.status(settings)
            running = any(r["name"] == server.name and r["can"]["talk"] for r in self.controller.rows())
            self.heading(server.name, "What it plays", "Workshop and mods")
            if status.executable is None:
                self.note("SteamCMD isn't installed yet: [S] installs it and signs your Steam account in", "yellow")
            else:
                self.note(status.summary(), "green" if status.ready else "yellow")
            self.note(f"Items in {server.name}'s {settings.config_name}: {', '.join(str(i) for i in items) or 'none'}",
                      "white")
            for verdict in await asyncio.to_thread(self._workshop_verdicts, server, settings):
                self.note(f"  {verdict}", "yellow" if "skipped" in verdict or "Error" in verdict else "gray")
            mods = catalogue.mods
            if mods:
                self.note(f"Mods installed ({len(mods)}):", "white")
                self.listing([(mod.label,) for mod in mods])
            for text, colour in await asyncio.to_thread(self._mod_health, server, settings):
                self.note(text, colour)
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
                if choice == "A" and not status.ready:
                    self.note("SteamCMD isn't set up with a signed-in account, so nothing can download yet: [S] first.",
                              "yellow")
                elif choice == "A" and running:
                    self.note("The running server fetches them itself now (auto_reload) and says so below; a big item "
                              "takes a while.", "cyan")
                    await self._follow_console(server, settings)
                elif choice == "A":
                    self.note("The server is stopped: [I] fetches them now, or it fetches them when it starts.")
            elif choice == "I":
                if not status.ready:
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
                if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(with_layouts):
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
        from ...content import gamefiles

        while True:
            self.title("STEAMCMD", "Workshop items and Halo 3's files, with one Steam account that owns MCC, signed in "
                                   "once (never a password in any file)")
            settings = self._settings()
            status = await asyncio.to_thread(steam.status, settings)
            self.heading("SteamCMD")
            self.note(status.summary(), "green" if status.ready else "yellow")
            self.heading("Halo 3's files")
            rows = await asyncio.to_thread(gamefiles.game_status, settings)
            for label, text, colour in rows or [("shared game/", "no server folders yet", "gray")]:
                self.note(f"{label:<13} {text}", colour)
            choice = await self.choose("Which?", [
                ("1", "SteamCMD and the Steam account", "find or install it, sign in once, tell every server"),
                ("2", "Get Halo 3's files", f"{game_files.DEPOTS['halo3'].holds}"),
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
        status = steam.status(settings)
        executable = status.executable
        if executable is not None:  # found: say where, and whether an account is set
            self.note(status.summary(), "green" if status.ready else "yellow")
        if executable is None:
            home_folder = settings.home / "steamcmd"
            standard = steam.STANDARD_WINDOWS if desktop.os.name == "nt" else Path.home() / "steamcmd"
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
                found, _ = await asyncio.to_thread(steam.find_steamcmd, settings.home, answer)
                if found is None:
                    self.note(f"No {steam.executable_name()} there.", "yellow")
                    await self.wait_any_key(6)
                    return
                executable = found
            else:
                target = home_folder if choice == "1" else standard
                try:
                    executable = await self.slow(steam.install, target, self._sayer())
                except steam.SteamSetupError as error:
                    self.note(str(error), "red")
                    await self.wait_any_key(10)
                    return
        accounts = steam.cached_accounts(executable)
        default = status.username or (accounts[0] if accounts else "")
        self.heading("The Steam account")
        if accounts:
            self.note(f"Accounts SteamCMD remembers: {', '.join(accounts)}")
        username = await self.prompt_line("  Steam account name (it must own MCC)", default)
        if not username:
            return
        if problem := steam.check_username(username):
            self.note(f"Left as it was: {problem}.", "yellow")
            return
        ok, detail = await self.slow(steam.signed_in, executable, username,  # SteamCMD: up to two minutes
                                     note="checking the sign-in (keys pressed meanwhile are ignored)…")
        if not ok:
            self.note(f"{username} isn't signed in to SteamCMD yet "
                      f"({detail.strip().splitlines()[-1] if detail.strip() else 'no cached sign-in'}).", "yellow")
            self.note(steam.login_window(executable, username), "cyan")
            if await self.prompt_line("  Press Enter once the sign-in window says it's done (Esc to leave it)") is None:
                self.note("Left as it was: nothing was written. Press I again once the sign-in is done.", "yellow")
                await self.wait_any_key(6)
                return
            ok, detail = await self.slow(steam.signed_in, executable, username,
                                         note="checking the sign-in again (keys pressed meanwhile are ignored)…")
            if not ok:
                self.note("Still not signed in; the account stays in settings.toml, try I again later.", "yellow")
        written = steam.configure(settings, executable, username)
        self.note(f"Written: [workshop] steamcmd and username into {len(written)} server folder(s), and [setup] into "
                  "settings.toml.", "green")
        self.note("A server's [workshop] takes effect when its `dedicated` restarts (R); `dedicated mods` "
                  "(G -> 3 -> I) installs items right away.")
        if ok:
            self.note(f"{username} is signed in: the servers can download Workshop items.", "green")
        if await self.prompt_yes_no("  Put SteamCMD's folder on your PATH, for other tools?", False):
            # In a thread: Windows tells every open window about the change and waits on the ones that hang.
            self.note(await asyncio.to_thread(steam.add_to_path, executable.parent))
        if ok:  # servers waiting for SteamCMD (to download their game files) look again now
            await self.controller.verb("game_files_retry")
        await self.wait_any_key(10)

    async def _game_files(self, kind: str) -> None:
        """One kind of game files into this folder's shared game/: every way there is, with sizes, first."""
        from ...content import gamefiles

        settings = self._settings()
        label = game_files.LABELS[kind]
        self.heading("SteamCMD", label)
        target, why = gamefiles.shared_target(settings, kind)
        if target is None:
            self.note(why, "yellow")
            await self.wait_any_key(10)
            return
        depot = game_files.DEPOTS[kind]
        found, no_download = await asyncio.to_thread(gamefiles.sources, settings, kind, downloads=True)
        self.note(f"Into {target}: {depot.holds}. What's there stays as it is, and running servers keep running.")
        options = []
        for number, source in enumerate(found, 1):
            if source.how == "mcc":
                options.append((str(number), f"Copy them from MCC at {source.path}",
                                "hard links on the same drive: no space used"))
            else:
                options.append((str(number), f"Download them with SteamCMD as {source.username}",
                                f"{gamefiles.gb(depot.download)} down, {gamefiles.gb(depot.kept)} kept (no sound or video)"))
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
            said = await self.slow(gamefiles.fetch, settings, kind, source, target, lambda: tool, self._sayer("cyan"))
        except (OSError, steam.SteamSetupError) as error:
            self.note(f"Not got: {error}", "yellow")
        else:
            self.note(said, "green")
        await self.wait_any_key(15)

    # Importing ---------------------------------------------------------------------------------

    async def _import_content(self, settings: Settings, server: ServerConfig) -> None:
        from ...steam import find_mcc
        from ...content.mccimport import import_content, importable

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
        from ...content import upkeep
        from ...steam import find_mcc

        from ...content.catalogue import SET_ASIDE, set_aside, unreadable_variants

        root = server.content_dir
        bad = await asyncio.to_thread(unreadable_variants, root) if root is not None else []
        if bad and root is not None:
            self.note(f"{len(bad)} file(s) in its content folder aren't MCC variant files, so the server skips them "
                      "at every start:", "yellow")
            for path in bad[:10]:
                self.note(f"  {path.relative_to(root).as_posix()}", "yellow")
            if await self.prompt_yes_no(f"  Move them to {root.parent / SET_ASIDE}? (nothing is deleted)", False):
                moved, kept = await asyncio.to_thread(set_aside, root, bad)
                self.note(f"Moved {len(moved)} file(s) to {root.parent / SET_ASIDE}.", "green")
                for line in kept:
                    self.note(f"Couldn't move {line}: close what holds it, and check again.", "yellow")
        commands = upkeep.firewall_commands(text)
        if commands and desktop.os.name == "nt" and await self.prompt_yes_no(
                f"  Open these {len(commands)} port rule(s) in Windows Firewall? (Windows asks for an administrator; "
                "a rule that's there already is left alone)", False):
            self.note(await asyncio.to_thread(upkeep.open_firewall, commands, settings.records), "green")
        game = (server.folder / str(_read_key(server.config_path, "game") or "game")).resolve()
        if game_files.engine_out_of_date(text):
            mcc = await asyncio.to_thread(find_mcc, settings.setup.mcc_folder)
            files = " and ".join(Path(f).name for f in game_files.engine_files(game))
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
        if game_files.odst_files_missing(text):
            await self._offer_odst_files(settings, game)

    async def _engine_from_steam(self, settings: Settings, game: Path, files: str, sharing: list[str]) -> None:
        """No MCC on this PC after an MCC update: the new engine files from Steam with SteamCMD instead (the whole
        multiplayer depot comes down; only the engine files are copied in, the old ones renamed)."""
        from ...content import gamefiles, upkeep

        found, no_download = await asyncio.to_thread(gamefiles.sources, settings, "halo3", downloads=True)
        by_steamcmd = next((source for source in found if source.how == "steamcmd"), None)
        if by_steamcmd is None:
            self.note(f"MCC isn't on this PC, and {no_download}: copy {files} from a PC with MCC, or set SteamCMD up "
                      "(I) to download them.", "yellow")
            return
        depot = game_files.DEPOTS["halo3"]
        if not await self.prompt_yes_no(f"  MCC isn't on this PC: download Halo 3's files with SteamCMD "
                                        f"({gamefiles.gb(depot.download)}) and copy the new {files} in?", False):
            return
        tool = await self._tool(settings)
        if tool is None:
            return
        stage = settings.records / "engine-download"
        try:
            downloaded = await self.slow(gamefiles.gather, settings, "halo3", by_steamcmd, stage, lambda: tool,
                                         self._sayer("cyan"))
            self.note(await asyncio.to_thread(upkeep.copy_engine, game, downloaded, sharing), "green")
        except (OSError, steam.SteamSetupError) as error:
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

    # A server's files, for O (opened as they are) -----------------------------------------------

    def server_files(self, settings: Settings, server: ServerConfig) -> list[tuple[str, str, Path]]:
        """What's worth editing in a server folder: (name, what it's for, path)."""
        folder = server.folder
        found: list[tuple[str, str, Path]] = [
            (settings.config_name, "its settings; most apply within seconds (auto_reload)", server.config_path)]
        own = own_file(folder, settings.home)
        if own is not None and own.is_file():
            found.append((own.name, "its own interval messages; apply within seconds", own))
        for path in playlist_files(folder):
            found.append((f"playlists/{path.name}", "a rotation; applies from the next vote", path))
        try:
            from ...core.tomledit import read_toml

            data = read_toml(server.config_path)
            bans, content = path_setting(folder, data, "bans"), path_setting(folder, data, "content")
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


def picked_label(game: dict[str, Any], catalogue: Catalogue) -> str:
    """A playlist game as the server names it when a ballot picks it: its own name or its game's, on its map's
    ("BR Fiesta CTF on HugeGrass Rebalance")."""
    map_ref, game_ref = str(game.get("map") or ""), str(game.get("game") or "")
    map_entry, game_entry = catalogue.find_map(map_ref), catalogue.find_game(game_ref)
    name = str(game.get("name") or (game_entry.name if game_entry else game_ref))
    return f"{name} on {map_entry.name if map_entry else map_ref}"


def _read_key(config: Path, key: str) -> Any:
    from ...core.tomledit import read_toml

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


def _plays_playlist(server: ServerConfig, path: Path) -> bool:
    """Whether the server's own [[server]] block plays this playlist file now (`playlist =`)."""
    from ...core.tomledit import server_blocks

    try:
        block = next((b for b in server_blocks(server.config_path)
                      if str(b.get("name", "")).lower() == server.name.lower()), {})
        playlist = block.get("playlist")
        return isinstance(playlist, str) and (server.folder / playlist).resolve() == path.resolve()
    except (OSError, ValueError):
        return False
