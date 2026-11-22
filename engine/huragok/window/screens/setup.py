"""The window's setup screens: N new server (the program's own `dedicated init`, then its quick actions), G what a
server plays (what it's set to and what it can play, its playlists: shown, ballot picks, history and put back, copied
to another server; custom games, Workshop items, mods' Forge layouts, imports), C check, I SteamCMD (the Workshop
account, a server's SteamCMD lines, every game's files: Halo 3's, the campaign's, ODST's, and from Project Reclaimer
0.10 Reach's, CE's and Halo 2's, and repair), the quick actions (shown before they're written: mockup 5), and the list
of each server's files that O opens.

Huragok no longer makes playlists or changes what a server plays (0.7.0): the host does, by hand, in the server's own
files. What G still writes, it writes only when the host asks (a copy, a version put back), after the server's own
program has had its say, with the version it replaces kept first (core/versions.py).

Everything the host picks from is listed first, numbered. The screens act on files and run the server program's own
`dedicated` commands in a thread, so the dashboard's work goes on meanwhile; a second window (not supervising) can use
them too.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from ... import lore, steam
from ...commandline.setup import workshop_items
from ...content import modcheck
from ...content.catalogue import Catalogue, Entry, describe_games, parse_picks
from ...content.workshopmods import layouts_by_hand
from ...content.mccimport import content_root_of, copy_replacing
from ...content import quickactions
from ...content.newserver import Outcome, SetupError, add_odst_files, create_server, finish, odst_mcc, valid_name
from ...content.quickactions import QuickAction, QuickActionError
from ...core import desktop, versions
from ...core.serverfolders import ServerConfig, slugify
from ...core.settings import Settings
from ...core.storage import read_picks
from ...games import halo3_mapfile
from ...games import files as game_files
from ...games.files import has_odst_files
from ...games.halo3 import Playlist, PlaylistError, load_playlist, playlist_files
from ...players.messages.intervals import own_file
from ...program.dedicated import Dedicated, DedicatedError, tool_for
from ...program.plays import PlaylistDraft, applies_when, check_plays
from ...program.settings import path_setting
from .. import statuscard
from ..controllers import Row
from .base import PAGE, Screens, map_sections

CONSOLE_TAIL = 256 * 1024  # how much of the end of a console log is read for what it said lately
BROWSE_KEYS = "123456789ABCDEFGHIJKLNOPQRSTUV"  # G's lists (M is the mods, W the Workshop items)
MORE = {"mod maps": "mod maps (installed mods and Workshop items)",
        "map variants (each plays on its own map)": "map variants (Forge layouts, .mvar files; each plays on its map)"}


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
        from ...content.gamefiles import read_paths, server_version
        from ...core.text import version_tuple

        paths = read_paths(server.folder, settings.config_name)
        root = server.content_dir or content_root_of(server.folder, settings.config_name)
        # The oldest program of the servers on that content folder: a Reach, CE or Halo 2 mod is fine on 0.10 only
        sharing = [s for s in settings.servers
                   if (s.content_dir or content_root_of(s.folder, settings.config_name)).resolve() == root.resolve()]
        known = [v for v in (server_version(settings, s) for s in sharing or [server]) if version_tuple(v) is not None]
        version = min(known, key=lambda v: version_tuple(v) or ()) if known else None
        reports = modcheck.check_mods(root, paths.game if paths else None, version)
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

    async def screen_new(self) -> None:
        """N (mockup 6): a folder in SERVERS/ made with the program's own `dedicated init` and nothing more, then its
        quick actions, each shown before it's written. Esc leaves the folder as it is. It's given its name (and so
        picked up, and started once its ports are free) only once the host is done with it."""
        self.title("NEW SERVER", "a folder in SERVERS/ made with the server program's own `dedicated init`, and "
                                 "nothing more; then its quick actions, each shown before it's written")
        settings = self._settings()
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(10)
            return
        self.heading("New server")
        name = await self.prompt_line("  Server name (also its folder's name)")
        if not name:
            return
        if problem := valid_name(name):
            self.note(problem, "yellow")
            await self.wait_any_key(5)
            return
        self.terminal.print()
        try:
            outcome = await self.slow(create_server, settings, name, tool, self._sayer())
        except SetupError as error:
            self.note(f"Not made: {error}", "red")
            await self.wait_any_key(15)
            return
        try:
            await self._new_server_actions(settings, outcome)
        finally:
            try:
                folder = finish(outcome)
            except SetupError as error:
                self.note(str(error), "yellow")
            else:
                self.controller.discover()
                self.note(f"{folder} is ready: Huragok picks it up within seconds and starts it once its ports are "
                          f"free on this PC. G shows what it still needs. {lore.MOTTO}", "green")
        await self.wait_any_key(10)

    async def _new_server_actions(self, settings: Settings, outcome: Outcome) -> None:
        """The quick actions after N (answer 33): one server only, free ports, the console, or all three in turn, each
        shown before it's written, until the host is done (Esc)."""
        written = False
        while True:
            self.heading("New server", outcome.name)
            await self._describe_new(settings, outcome)
            self.terminal.print()
            self.note("Quick actions (each shown before it's written; Esc leaves the folder "
                      + ("as it is now)" if written else "exactly as init made it)"), "white")
            choice = await self.choose("Which?", [
                ("1", "One server only", f'keep "{outcome.name}", take out the other [[server]] blocks'),
                ("2", "Free ports", "a free game port, and the master off or on a free port"),
                ("3", "Add the console", "a random [rcon] password; it listens on its game port's number"),
                ("A", "All three")], back="Esc done")
            if choice is None:
                return
            for step in ("1", "2", "3") if choice == "A" else (choice,):
                done = await self._new_quick_action(settings, outcome, step)
                if done is None:  # Esc: the rest aren't asked
                    break
                written = written or done

    async def _describe_new(self, settings: Settings, outcome: Outcome) -> None:
        """What the new folder holds now (mockup 6): its servers and their ports, its master, and which are taken."""
        from ...content.newserver import read_folder

        servers, master = await asyncio.to_thread(read_folder, outcome.config)
        taken = await asyncio.to_thread(quickactions.taken_ports, settings, outcome.config,
                                        [port for _, port in servers if port] + ([master] if master else []))
        made = str(Path(outcome.folder.parent.name) / outcome.folder.name)
        self.note(f"Made {made} with the program's own `dedicated init`"
                  + (f" (Project Reclaimer {outcome.version})." if outcome.version else "."), "white")
        ports = [str(port) for _, port in servers if port]
        count = {1: "one server", 2: "two servers", 3: "three servers"}.get(len(servers), f"{len(servers)} servers")
        self.note(f"It holds {count}" + (f", on port{'s' if len(ports) > 1 else ''} {_listed(ports)}" if ports else "")
                  + (f", with its master on {master}." if master else ", with its master off."), "white")
        games = [(port, whose) for port, whose in taken if port != master]
        if games:
            whose = [w for _, w in games if w]
            self.note(f"! {_listed([str(p) for p, _ in games])} {'is' if len(games) == 1 else 'are'} in use on this PC"
                      + (f" ({', '.join(whose)})" if whose else "") + ": it won't start until that changes.", "yellow")
        if any(port == master for port, _ in taken):
            self.note(f"Its master's port {master} is in use too: the program lists its servers on the master already "
                      "there.", "gray")

    async def _new_quick_action(self, settings: Settings, outcome: Outcome, step: str) -> bool | None:
        config, where, owner = outcome.config, outcome.shown, slugify(outcome.name)
        if step == "1":
            return await self.quick_action(settings, outcome.name, lambda: quickactions.one_server(
                config, outcome.name, where=where, owner=owner))
        if step == "2":
            master = await self._ask_master(config)
            if master is None:
                return None
            return await self.quick_action(settings, outcome.name, lambda: quickactions.free_ports(
                settings, config, master=master, where=where, owner=owner))
        return await self.quick_action(settings, outcome.name, lambda: quickactions.add_console(
            settings, config, where=where, owner=owner))

    async def _ask_master(self, config: Path) -> str | None:
        """Free ports: the folder's master off, or on a free port ("off", "free"; None: Esc). Not asked when it's off."""
        from ...content.newserver import read_folder

        _, master = await asyncio.to_thread(read_folder, config)
        if master is None:
            return "off"
        self.note(f"Its master (port {master}) lists this folder's servers for players on this PC's network; the "
                  "project's public masters list them anyway, and one master per PC does it.")
        answer = await self.prompt_key("  Its master: O off, F on a free port", ["O", "F"], "O")
        return None if answer is None else "off" if answer == "O" else "free"

    # Quick actions: shown before they're written (mockup 5) ------------------------------------

    async def quick_action(self, settings: Settings, crumb: str, build: Callable[[], QuickAction]) -> bool | None:
        """One quick action (content/quickactions.py) as mockup 5 draws it: what it writes, worked out on a copy of
        the file, then yes or no; written (a version of the file kept first) only on yes. True: written; False: not
        (no, nothing to write, or it couldn't be); None: Esc."""
        try:
            action = await asyncio.to_thread(build)  # it reads every server folder and this PC's ports
        except QuickActionError as error:
            self.heading(crumb, "Quick action")
            self.note(f"Not offered: {error}", "yellow")
            return False
        self.heading(crumb, "Quick action", action.title)
        for text, colour in action.preview():
            self.note(text, colour)
        if not action.needed:
            return False
        self.terminal.print()
        answer = await self.prompt_key(f"  {action.ask} (y/N)", ["Y", "N"], "N")
        if answer is None:
            return None
        if answer != "Y":
            self.note("Nothing was written.")
            return False
        try:
            await asyncio.to_thread(quickactions.write, settings, action)
        except (QuickActionError, OSError) as error:
            self.note(f"Not written: {error}", "yellow")
            return False
        self.note(f"Written in {action.where}; the version before it is kept in its history.", "green")
        return True

    async def screen_add_console(self, server: ServerConfig) -> bool | None:
        """D → a server → Q → Add the console (mockup 5): the lines that give it its console, shown, and written when
        the host says yes, a version of its dedicated.toml kept first. For the D screen to call; the same answers as
        quick_action."""
        settings = self._settings()
        where = shown_path(settings, server.config_path)
        return await self.quick_action(settings, server.name, lambda: quickactions.add_console(
            settings, server.config_path, server.name, where=where))

    async def _steamcmd_lines(self, settings: Settings) -> None:
        """I → L: one server's [workshop] steamcmd and username (this PC's SteamCMD and the I key's account), shown
        before they're written. Never every server at once (0.7.0)."""
        picked = await self.select_servers("SteamCMD lines for", single=True)
        server = self._server(settings, picked[0]) if picked else None
        if server is None:
            return
        where = shown_path(settings, server.config_path)
        await self.quick_action(settings, server.name, lambda: quickactions.steamcmd_lines(
            settings, server.config_path, where=where))
        await self.wait_any_key(10)

    async def screen_repair(self) -> None:
        """I → R: what a server's folder lacks (content/gamefiles.py's repairs: game files, a game folder pointed here,
        a content folder or ban list of its own, SteamCMD lines, Reach's variants), listed first and put right when
        the host says so, a version of its dedicated.toml kept before the first change."""
        from ...content import gamefiles

        settings = self._settings()
        picked = await self.select_servers("Repair", single=True)
        server = self._server(settings, picked[0]) if picked else None
        if server is None:
            return
        self.heading(server.name, "Repair", note="what its folder lacks; nothing is changed until you say yes")
        say = self._sayer("cyan")
        plan = await asyncio.to_thread(gamefiles.repairs, settings, server, lambda: tool_for(settings, server), say)
        if not plan:
            self.note("Nothing to put right: its folder has what it needs, as far as Huragok can tell.", "green")
            await self.wait_any_key(10)
            return
        for number, repair in enumerate(plan, 1):
            self.note(f"{number}. {repair.text}" + ("" if repair.run else " (can't be done from here)"),
                      "white" if repair.run else "yellow")
        doable = [repair for repair in plan if repair.run is not None]
        if not doable:
            await self.wait_any_key(15)
            return
        if any(repair.writes for repair in doable):
            self.note(f"A copy of its {settings.config_name} is kept first (D → a server → F puts it back).")
        if not await self.prompt_yes_no("  Put these right?", False):
            self.note("Nothing was changed.")
            await self.wait_any_key(5)
            return
        done, failed = await self.slow(gamefiles.run_repairs, settings, server, doable, say)
        for line in failed:
            self.note(f"Not done: {line}", "yellow")
        if done:
            self.note("Done. A server that's running takes a changed path at its next start.", "green")
        await self.controller.verb("game_files_retry")  # the servers waiting for files look again now
        await self.wait_any_key(15)

    # G: what a server plays ------------------------------------------------------------------

    async def screen_content(self) -> None:
        self.title("WHAT IT PLAYS", "what a server plays, from its own files: what it's set to, what's on now and "
                                    "next, its playlists and custom games, and what it can play (maps, variants, mods)")
        settings = self._settings()
        picked = await self.select_servers("Look at", single=True)
        if not picked:
            return
        server = self._server(settings, picked[0])
        if server is not None:
            await self._content_home(settings, server, picked[0])

    async def _content_home(self, settings: Settings, server: ServerConfig, row: Row) -> None:
        """One server's home for what it plays (G): what its own file sets, what's on now and next and what players
        pick, and what it has and can play. Nothing here changes what it plays: the host sets that by hand."""
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
            for label, text, colour in dict(card).get("What it plays", []):
                self.terminal.print_parts([(f"  {label:<14}", "gray"), (text, colour or "white")])
            items = fresh_row.get("warning_items") or []
            self._file_health(items)
            self.note(catalogue.counts())
            self.note(f"Playlists and custom games are made by hand now, and what it plays is set in its "
                      f"{settings.config_name}: F opens its folder, E the file. (W, P, the builders and packages "
                      "have gone; this note shows for one release.)", "yellow")
            options = [("1", "Playlists", "show as the server reads them, ballot picks, history, copy, set aside"),
                       ("2", "Game modes", "what each changes, copy, set aside (.json, .rmeg, .bin)"),
                       ("3", "Maps and layouts", "what it can play: maps, saved maps, variants"),
                       ("4", "Mods", "its Workshop items, the mods installed, why one would be skipped"),
                       ("5", "Check it", "the server's own check, read-only"),
                       ("6", "Import from MCC", "variants and saved maps, into its content folder"),
                       ("F", "Open its folder"), ("E", f"Open {settings.config_name} in your editor")]
            if items:
                options.append(("X", "Dismiss a warning", "it comes back if its file changes"))
            choice = await self.choose("Which?", options)
            if choice is None:
                return
            if choice == "1":
                await self._playlists(settings, tool, server, catalogue)
            elif choice == "2":
                await self._custom_games(settings, tool, server)
            elif choice == "3":
                await self._browse(server, catalogue)
            elif choice == "4":
                await self._workshop(settings, tool, server, catalogue)
            elif choice == "5":
                self.heading(server.name, "Check")
                await self._dedicated(tool, server.folder, "check")
            elif choice == "6":
                self.heading(server.name, "Import variants")
                await self._import_content(settings, server)
            elif choice == "F":
                self.note(desktop.open_path(server.folder))
            elif choice == "E":
                self.note(desktop.open_path(server.config_path))
            elif choice == "X":
                await self._dismiss_warning(server, items)
            if choice in ("2", "6"):
                fresh = await self._catalogue(tool, settings, server)
                if fresh is not None:
                    catalogue = fresh

    def _file_health(self, items: list[dict[str, Any]]) -> None:
        """G's file health (mockups-0.7.0.md 1): the server's warnings, refused first, each marked by its kind."""
        from ...core.serverwarnings import KINDS, MARKS, WORDS

        if not items:
            self.terminal.print_parts([(f"  {'File health':<14}", "gray"), ("nothing to say", "green")])
            return
        counts = {kind: sum(1 for item in items if item.get("kind") == kind) for kind in KINDS}
        words = {"refused": "refused", "seen": "seen", "check": "to check", "info": "for your information"}
        summary = " · ".join(f"{counts[kind]} {words[kind]}" for kind in KINDS if counts[kind])
        self.terminal.print_parts([(f"  {'File health':<14}", "gray"), (summary, "white")])
        colours = {"refused": "red", "seen": "yellow", "check": "yellow", "info": "gray"}
        for item in items:
            kind = str(item.get("kind") or "info")
            text = str(item.get("text") or "") + (f" {item['fix']}" if item.get("fix") else "")
            self.terminal.print_parts([(f"   {MARKS.get(kind, 'i')} {WORDS.get(kind, 'Info'):<8} ", colours.get(kind, "gray")),
                                       (text, "white")])

    async def _dismiss_warning(self, server: ServerConfig, items: list[dict[str, Any]]) -> None:
        """X: one of the server's warnings hidden until its file changes (for good, for one about no file)."""
        self.heading(server.name, "What it plays", "Dismiss a warning")
        panel = self.listing([(str(item.get("text") or ""),) for item in items], wrap=True)
        answer = await self.prompt_line("  Which? (number; Esc goes back)", panel=panel)
        if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(items):
            return
        for _, result in await self.controller.verb("dismiss_warning", server.name, id=items[int(answer) - 1]["id"]):
            self.note(result.message, "green" if result.ok else "yellow")

    async def _browse(self, server: ServerConfig, catalogue: Catalogue) -> None:
        # Each game's maps and its mods' maps (0.10: Reach's, CE's, Halo 2's beside Halo 3's), as the pickers head
        # them (map_sections), each heading a list of its own; before 0.10 Halo 3's maps, mod maps, map variants
        sections: list[tuple[str, list[Entry]]] = []
        for _, entries, headings in map_sections(catalogue):
            for number, (heading, start) in enumerate(headings):
                end = headings[number + 1][1] - 1 if number + 1 < len(headings) else len(entries)
                sections.append((heading, entries[start - 1:end]))
        names = [name for name, _ in sections]
        if "Halo 3's maps" not in names:
            sections.insert(0, ("Halo 3's maps", []))
        if getattr(catalogue, "shape", "0.9") != "0.10" and "mod maps" not in names:
            sections.insert(1, ("mod maps", []))
        if not any(name.startswith("map variants") for name in names):
            sections.append(("map variants (each plays on its own map)", []))
        sections = [(MORE.get(name, name), entries) for name, entries in sections]
        if catalogue.canvases:
            sections.append(("campaign canvases (campaign missions' areas, for Forge)", catalogue.canvases))
        sections += [("game types", catalogue.game_types),
                     ("game variants (.bin, saved and scripted game types)", catalogue.game_variants)]
        while True:
            self.heading(server.name, "What it plays", "Browse",
                         note="the first name on a line is what playlists and map = / game = take; [brackets] show "
                              "the file reference when names collide")
            options = [(key, f"{len(entries):>4}  {name}") for key, (name, entries) in zip(BROWSE_KEYS, sections)]
            options.append(("M", f"{len(catalogue.mods):>4}  mods in the content folder (what each brings)"))
            options.append(("W", f"{len(catalogue.workshop_items):>4}  Workshop items in dedicated.toml"))
            choice = await self.choose("Which list?", options, columns=1)
            if choice is None:
                return
            if choice in BROWSE_KEYS:
                name, entries = sections[BROWSE_KEYS.index(choice)]
                self.heading(server.name, name)
                shown = entries
                if len(entries) > PAGE:
                    words = await self.prompt_line("  Words to narrow the list (Enter for all)")
                    if words is None:
                        continue
                    if words.strip():
                        shown = [e for e in entries if all(w in e.label.lower() for w in words.lower().split())]
                await self._pages(self._numbered(shown) or ["(none)"])
            elif choice == "M":
                self.heading(server.name, "mods in the content folder")
                await self._pages([f"[{n}] {m.label}  ({m.folder.name})" for n, m in enumerate(catalogue.mods, 1)]
                                  or ["(no mods in the content folder)"])
            elif choice == "W":
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
            uses = _playlist_uses(server)  # how its own file names each (playlist =, or playlists by lobby size)
            for path in files:
                try:
                    playlist = load_playlist(path)
                    summary = f'"{playlist.name}" · {len(playlist.games)} games · vote {playlist.vote}'
                    games.append(describe_games(playlist.games, 4))
                except PlaylistError as error:
                    summary = f"(can't read it: {error})"
                    games.append("")
                how = uses.get(_same(path))
                rows.append((path.name.removesuffix(".playlist.json"), summary + (f"   ◄ in use ({how})" if how else "")))
            block = self.listing(rows, show=False)
            for n in range(len(rows) - 1, -1, -1):
                if games[n]:
                    block.insert(n + 1, [(f"        {games[n]}", "gray")])
            for parts in block:
                self.terminal.print_parts(parts)
            if not files:
                self.note("(no playlists in playlists/ yet)")
            self.note(f"Playlists are made and changed by hand now (F opens one in your editor), and its "
                      f"{settings.config_name} names the one it plays. (B, M, E and U have gone; this note shows for "
                      "one release.)", "yellow")
            choice = await self.choose("Which?", [
                ("S", "Show one as the server sees it"),
                ("H", "History: every version kept, and put one back"),
                ("P", "Picks: what players picked on its ballots, and what never"),
                ("C", "Copy one to other servers (each checked by its own program)"), ("F", "Open one in the editor")])
            if choice is None:
                return
            if not files:
                self.note(f"There are none to pick: a playlist is made by hand in {server.folder / 'playlists'}.",
                          "yellow")
                continue
            answer = await self.prompt_line("  Which playlist? (number)")
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(files):
                continue
            path = files[int(answer) - 1]
            if choice == "S":
                self.heading(server.name, "Playlists", path.name)
                await self._dedicated(tool, server.folder, "playlist", "show", f"playlists/{path.name}", timeout=60)
            elif choice == "H":
                await self._playlist_history(settings, server, path)
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
        """A playlist copied, exactly as it is, to the other servers the host picks. Each copy is checked first by that
        server's own program (its version, its content); when it refuses, its own words are shown with what copying
        anyway does, and the host is asked once. The version a copy replaces, and the copy, go in that server's
        playlist history. Copied, not played: what a server plays is named in its own file."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            self.note(f"{path.name} can't be read: {error}", "yellow")
            return
        self.heading(server.name, "Playlists", path.name, "Copy")
        for target in await self._other_servers(settings, server, path.name):
            dest = target.folder / "playlists" / path.name
            try:
                if dest.is_file() and dest.resolve() == path.resolve():
                    self.note(f"{target.name}: shares this playlists folder, so it has {path.name} already.", "gray")
                    continue
                if dest.is_file() and dest.read_bytes() == path.read_bytes():
                    self.note(f"{target.name}: has it already, the same.", "gray")
                    continue
            except OSError as error:
                self.note(f"{target.name}: not copied: {error}", "yellow")
                continue
            playing = _plays_playlist(target, dest)
            if dest.is_file() and not await self.prompt_yes_no(
                    f"  {target.name} has a {path.name} of its own" + (", and plays it now" if playing else "")
                    + ": replace it (its version is kept in its history)?", False):
                continue
            if playing:
                after = (f"Copied anyway, it replaces the one {target.name} plays: the server refuses the change and "
                         "keeps playing the old one, no other change to its file applies until it's put right, and "
                         "stopped, it won't start.")
            else:
                after = (f"Copied anyway, it sits in {target.name}'s playlists folder unplayed (its file doesn't name "
                         "it): nothing changes for its players, and it can't be played as it is.")
            if not await self._checked_or_asked(settings, target, dest, text, after, f"Copy it to {target.name} anyway"):
                continue
            if self._write_kept(settings, target, dest, path, "copied", f"{server.name}'s {path.name}"):
                self.note(f"{target.name}: copied to playlists/{path.name} " + (
                    f"(it plays it: {applies_when('playlist', 'playlist')})" if playing
                    else f"(not played: its {settings.config_name} names what it plays)"), "green")
        await self.wait_any_key(10)

    async def _checked_or_asked(self, settings: Settings, target: ServerConfig, dest: Path, text: str, after: str,
                                question: str) -> bool:
        """Whether a playlist file the host chose may be written at `dest`, in `target`'s playlists folder: checked
        first by that server's own program, as the file it would be (check_plays). Taken: yes. Refused: the program's
        own words, what writing it anyway does (`after`), and the host asked once (`question`); never refused
        outright. Not asked (no program): said so, and asked the same way."""
        relative = f"playlists/{dest.name}"
        try:
            tool = await asyncio.to_thread(tool_for, settings, target)  # may ask each copy its --version
        except DedicatedError as error:
            self.note(f"{target.name}: its server program can't check it ({error}).", "yellow")
            return await self.prompt_yes_no(f"  {question}?", False)
        check = await asyncio.to_thread(check_plays, tool, target, {"playlist": relative}, PlaylistDraft(dest, text))
        if check.ok:
            if not check.asked:
                self.note(f"{target.name}: not checked by its server program: {check.unasked}.", "yellow")
            return True
        self.note(f"{target.name}: its server program won't play it as it is:", "yellow")
        self._show_problems(_labels_of(dest, text), check.problems)
        self.note(after, "yellow")
        return await self.prompt_yes_no(f"  {question}?", False)

    def _show_problems(self, labels: list[str], problems: list[Any]) -> None:
        """The program's own words, each beside the game it's about (`labels`: the playlist's games, numbered from 1)."""
        for problem in problems:
            if problem.game is not None and problem.game <= len(labels):
                said = re.sub(rf"^Game {problem.game}:?\s*", "", problem.text)
                self.note(f"  game {problem.game} ({labels[problem.game - 1]}): {said}", "yellow")
            else:
                self.note(f"  {problem.text}", "yellow")

    def _write_kept(self, settings: Settings, server: ServerConfig, path: Path, source: Path, how: str,
                    where_from: str) -> bool:
        """`source` copied over a server's playlist `path`, exactly as it is, once the version it replaces is kept in
        that server's playlist history (not kept: not written). The new version is kept too, with how it came."""
        records = settings.records
        try:
            versions.keep_current(records, "playlists", server.slug, path)
        except OSError as error:
            self.note(f"{server.name}: not written: the version it would replace couldn't be kept in the history first "
                      f"({error}).", "yellow")
            return False
        try:
            copy_replacing(source, path)  # a new file swapped in: a hard-linked copy elsewhere keeps its own
        except OSError as error:
            self.note(f"{server.name}: not written: {error}", "yellow")
            return False
        try:
            versions.keep(records, "playlists", server.slug, path, how, where_from)
        except OSError as error:
            self.note(f"{server.name}: written, but its version couldn't be kept in the history: {error}", "yellow")
        return True

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

    async def _playlist_history(self, settings: Settings, server: ServerConfig, path: Path) -> None:
        """Every version of a playlist Huragok kept (core/versions.py), newest first: what each holds, how it was
        made and where it came from. One is put back, exactly as it was kept, when the host asks: checked first by
        the server's own program, and asked once if it refuses (_checked_or_asked); the version it replaces is kept."""
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
                continue
            playing = _plays_playlist(server, path)
            after = (f"Put back anyway, {server.name} refuses the change and keeps playing the playlist as it is now; "
                     "no other change to its file applies until it's put right, and stopped, it won't start."
                     if playing else "Put back anyway, it can't be played as it is (its file doesn't name it now).")
            try:
                text = versions.read(version)
            except OSError as error:
                self.note(f"Can't read that version: {error}", "yellow")
                continue
            if not await self._checked_or_asked(settings, server, path, text, after, "Put it back anyway"):
                continue
            if self._write_kept(settings, server, path, version.copy, "put back", f"the version of {version.stamp}"):
                self.note(f"Put back: {path.name} is the version of {version.stamp} again"
                          + (f" (it plays it: {applies_when('playlist', 'playlist')})." if playing else "."), "green")
                await self.wait_any_key(6)
                return

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
            self.note(f"[workshop] items is yours to write (O opens {settings.config_name}): the server fetches the "
                      "items it lists and keeps them up to date by itself, and Huragok restarts it when a mod's map in "
                      "use holds an update up.")
            for verdict in await asyncio.to_thread(self._workshop_verdicts, server, settings):
                self.note(f"  {verdict}", "yellow" if "skipped" in verdict or "Error" in verdict else "gray")
            mods = catalogue.mods
            if mods:
                self.note(f"Mods installed ({len(mods)}):", "white")
                self.listing([(mod.label,) for mod in mods])
            for text, colour in await asyncio.to_thread(self._mod_health, server, settings):
                self.note(text, colour)
            choice = await self.choose("Which?", [
                ("I", "Fetch them now"), ("L", "Install a mod's Forge layouts into Maps (only when you ask)"),
                ("S", "SteamCMD setup (install, sign in)"), ("M", "Mod maps the server lists")])
            if choice is None:
                return
            if choice == "I":
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
                copied = await asyncio.to_thread(layouts_by_hand, settings, root, with_layouts[int(answer) - 1])
                self.note(f"Copied {len(copied)} layout(s) into {root / 'Maps'}: the server lists them as saved maps "
                          "(set base_map to the mod map in a playlist if it asks). The server doesn't read a mod's own "
                          "layouts otherwise: after the mod's next version, L brings its new ones (a layout you "
                          "changed there is left as it is).", "green")
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
        """SteamCMD and everything it does for the servers: the Workshop account, and every game's files."""
        from ...content import gamefiles

        while True:
            self.title("STEAMCMD", "Workshop items and the games' files, with one Steam account that owns MCC, signed "
                                   "in once (never a password in any file)")
            settings = self._settings()
            status = await asyncio.to_thread(steam.status, settings)
            self.heading("SteamCMD")
            self.note(status.summary(), "green" if status.ready else "yellow")
            self.heading("Game files")
            version = await asyncio.to_thread(gamefiles.root_version, settings)  # `--version`, once per file
            rows = await asyncio.to_thread(gamefiles.game_status, settings, version)
            for label, text, colour in rows or [("shared game/", "no server folders yet", "gray")]:
                self.note(f"{label:<13} {text}", colour)
            rows_for = {
                "halo3": ("Get Halo 3's files", game_files.HOLDS["halo3"]),
                "campaign": ("Add the campaign missions", "co-op campaign, and every Forge map's campaign vehicles"),
                "odst": ("Add Halo 3: ODST's files", "for ODST servers (Firefight, campaign)"),
                "reach": ("Add Halo: Reach's files", "its multiplayer and Firefight maps (from MCC)"),
                "reach-campaign": ("Add Halo: Reach's campaign", "its missions, for a Reach co-op server (from MCC)"),
                "halo1": ("Add Halo: Combat Evolved's files", "its multiplayer maps (from MCC)"),
                "halo2": ("Add Halo 2's files", "its multiplayer maps (from MCC)"),
            }
            kinds = game_files.kinds_for(version)  # Reach, CE and Halo 2 only on a program that plays them (0.10)
            options = [("1", "SteamCMD and the Steam account", "find or install it, sign in once")]
            options += [(str(n), *rows_for[kind]) for n, kind in enumerate(kinds, 2)]
            options += [("L", "SteamCMD lines for a server", "its [workshop] steamcmd and username, shown first"),
                        ("R", "Repair a server's folder", "what it lacks (game files, paths), shown first")]
            choice = await self.choose("Which?", options)
            if choice is None:
                return
            if choice == "1":
                await self._steam_account()
            elif choice == "L":
                await self._steamcmd_lines(settings)
            elif choice == "R":
                await self.screen_repair()
            else:
                await self._game_files(kinds[int(choice) - 2])

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
        steam.configure(settings, executable, username)
        self.note("Written: [setup] steamcmd and steam_username in settings.toml.", "green")
        if ok:
            self.note(f"{username} is signed in: Huragok can download game files with it.", "green")
        self.note("A server's own [workshop] steamcmd and username are written only when you ask, one server at a "
                  "time, shown first (L on the SteamCMD screen).")
        if await self.prompt_yes_no("  Put SteamCMD's folder on your PATH, for other tools?", False):
            # In a thread: Windows tells every open window about the change and waits on the ones that hang.
            self.note(await asyncio.to_thread(steam.add_to_path, executable.parent))
        if ok:  # what failed for want of SteamCMD (I's last ask for a game's files) is looked at again now
            await self.controller.verb("game_files_retry")
        if await self.prompt_yes_no("  Write the SteamCMD lines into a server's dedicated.toml now?", False):
            await self._steamcmd_lines(self._settings())
            return
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
        depot = game_files.DEPOTS.get(kind)
        found, no_download = await asyncio.to_thread(gamefiles.sources, settings, kind, downloads=True)
        self.note(f"Into {target}: {game_files.HOLDS[kind]}. What's there stays as it is, and running servers keep "
                  "running.")
        options = []
        for number, source in enumerate(found, 1):
            if source.how == "mcc":
                options.append((str(number), f"Copy them from MCC at {source.path}",
                                f"hard links on the same drive: no space used ({gamefiles.gb(game_files.SIZES[kind])} "
                                "otherwise)"))
            elif depot is not None:
                options.append((str(number), f"Download them with SteamCMD as {source.username}",
                                f"{gamefiles.gb(depot.download)} down, {gamefiles.gb(depot.kept)} kept (no sound or video)"))
        if depot is None:  # Reach's, CE's, Halo 2's: MCC is the only way for now
            if not found:
                self.note(f"{no_download}.", "yellow")
        else:
            if not any(source.how == "mcc" for source in found):
                self.note("MCC with these files isn't on this PC (Steam's own records say where it is: Steam, MCC, "
                          "Properties, DLC adds each game).", "yellow")
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
        from ...games.gamemodes import list_game_modes

        # Its game modes an editor opens: .json game types and .rmeg scripted modes (MCC's .bin variants aren't text)
        for mode in [m for m in list_game_modes(content) if m.kind != "bin" and not m.bundled][:12]:
            what = "a scripted game mode (its header names it); picked up live" if mode.kind == "rmeg" else \
                "a saved game type; picked up live"
            found.append((mode.relative, what, mode.path))
        return found


def shown_path(settings: Settings, path: Path) -> str:
    """A file as the host finds it from Huragok's folder, SERVERS/LG Test 2/dedicated.toml in this system's own
    spelling (the whole path when it's elsewhere)."""
    try:
        return str(path.resolve().relative_to(settings.home.resolve()))
    except ValueError:
        return str(path)


def _listed(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""


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


def _labels_of(path: Path, text: str) -> list[str]:
    """A playlist's games in words, numbered from 1 by their place ([] when its text isn't a playlist Huragok reads)."""
    try:
        return Playlist.from_data(path, json.loads(text)).labels()
    except (ValueError, PlaylistError):
        return []


def _same(path: Path) -> str:
    """A path as one file, to compare (case, `..`, links)."""
    import os

    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _playlist_uses(server: ServerConfig) -> dict[str, str]:
    """Each playlist file the server's own file names (its block's, else [defaults]'), as _same has it: how it names
    it ("playlist =", or "playlists, 1-8" for one of 0.10.1's playlists by lobby size)."""
    from ...core.tomledit import read_toml
    from ...games.plays import playlist_refs, plays_block

    try:
        data = read_toml(server.config_path)
    except (OSError, ValueError):
        return {}
    blocks = data.get("server", []) if isinstance(data.get("server"), list) else []
    block = next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == server.name.lower()),
                 {})
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else None
    return {_same(ref.path(server.folder)): "playlist =" if ref.how == "playlist" else
            f"playlists, {ref.players}" if ref.players else "playlists"
            for ref in playlist_refs(plays_block(block, defaults))}


def _plays_playlist(server: ServerConfig, path: Path) -> bool:
    """Whether the server's own file plays this playlist file now: `playlist =`, or one of `playlists = [...]`."""
    return _same(path) in _playlist_uses(server)
