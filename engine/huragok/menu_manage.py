"""The window's screens for managing a running server: A the console (with the server's events as
they happen, and your name on what you do), D settings: Huragok's own files (menu_files) above every
server, and each server in groups (what it plays, the settings of its dedicated.toml, Huragok's own
for it; its playlists, custom games, bans and mods; renaming it, a content folder of its own),
B bans, admins and the VPN block, U updates (Huragok's, the server program's, Workshop mods).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .events import Event
from .livesettings import (APPLIES, FOLDER_SETTINGS, GROUPS, SERVER_SETTINGS, can_reset, catalogue, check_path,
                           check_value, display, parse, plays_problem, reset, show, summary, write, write_plays)
from .manager import Result
from .menu_files import FILES
from .rcon import masked as mask_console_line

Row = dict[str, Any]


def describe_event(event: Event) -> str | None:
    """One line for a server event, as a console shows it; None for events not worth a line."""
    name = event.name.removeprefix("rcon.")
    clock = time.strftime("%H:%M:%S", time.localtime(event.time))
    data = event.data
    who = str(data.get("name") or data.get("target") or "")
    by = f" by {data['by']}" if data.get("by") else ""
    if name == "chat":
        channel = str(data.get("channel") or "all")
        prefix = "" if channel == "all" else f"[{channel}] "
        return f"{clock} {prefix}{who}: {data.get('text', '')}"
    if name == "join":
        return f"{clock} + {who} joined (#{data.get('number', '?')}{', admin' if data.get('admin') else ''})"
    if name == "leave":
        return f"{clock} - {who} left"
    if name == "refused":
        return f"{clock} ! {who} refused: {data.get('reason', '')}"
    if name in ("kick", "ban", "unban", "mute", "unmute", "pardon", "admin_added", "admin_removed"):
        reason = f": {data['reason']}" if data.get("reason") else ""
        return f"{clock} * {name.replace('_', ' ')} {who}{by}{reason}"
    if name == "vote":
        return f"{clock} vote {data.get('state', '')}: {data.get('subject', '')} (by {data.get('caller', '?')})"
    if name == "phase":
        return f"{clock} game: {str(data.get('phase', '')).replace('_', ' ')} {data.get('map', '')} {data.get('mode', '')}"
    if name == "control":
        return f"{clock} {data.get('action', '')}: {data.get('text', '')}"
    if name == "cheat":
        return f"{clock} anti-cheat {data.get('action', '')}: {who} ({data.get('detail', '')})"
    if name in ("connected", "disconnected"):
        return f"{clock} (console {name})"
    return None


class ManageScreens:
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""

    terminal: Any
    controller: Any
    held: Any  # lines that arrive while a question waits (Menu's Interruptions)
    by_name: str | None = None  # who's acting, asked once per window

    def title(self, text: str, note: str | None = None) -> None: ...
    def heading(self, *crumbs: str, note: str | None = None) -> None: ...
    def note(self, text: str, colour: str | None = "gray") -> None: ...
    def listing(self, rows: list[tuple[str, ...]], **options: Any) -> list[Any]: ...
    async def choose(self, prompt: str, options: list[Any], **more: Any) -> str | None: ...
    async def prompt_line(self, prompt: str, default: str = "", *, panel: Any = None) -> str | None: ...
    async def prompt_key(self, prompt: str, keys: list[str], default: str | None = None, *,
                         panel: Any = None) -> str | None: ...
    async def prompt_yes_no(self, prompt: str, default: bool = False) -> bool: ...
    async def wait_any_key(self, seconds: float = 15) -> None: ...
    async def select_servers(self, verb: str, *, single: bool = False, allowed: Any = None) -> list[Row]: ...
    def show_results(self, results: list[tuple[str, Result]]) -> None: ...
    def _settings(self) -> Any: ...
    def _server(self, settings: Any, row: Row) -> Any: ...
    async def _tool(self, settings: Any) -> Any: ...
    async def _catalogue(self, tool: Any, settings: Any, server: Any) -> Any: ...  # SetupScreens'
    async def _pick(self, entries: list[Any], what: str, **more: Any) -> list[Any] | None: ...
    def _map_groups(self, catalogue: Any) -> list[tuple[str, int]]: ...
    def _game_groups(self, catalogue: Any) -> list[tuple[str, int]]: ...
    async def _offer_odst_files(self, settings: Any, game: Path) -> bool: ...  # SetupScreens'
    async def slow(self, work: Any, *args: Any, note: str | None = None, colour: str = "cyan") -> Any: ...  # Menu's
    def server_rows(self, rows: list[Row], *, show: bool = True) -> list[Any]: ...  # Menu's
    if TYPE_CHECKING:  # FileScreens', which comes after this one: a stub here would stand in for them
        def huragok_files(self, settings: Any) -> list[tuple[str, str, str, str]]: ...
        async def huragok_file(self, settings: Any, key: str) -> None: ...
        def server_page_extras(self, settings: Any, server: Any) -> list[tuple[str, str, str, str]]: ...
        async def _server_page_extra(self, settings: Any, server: Any, row: Row, key: str) -> None: ...
    _preselect: str | None = None

    async def _who(self) -> str:
        if self.by_name is None:
            name = await self.prompt_line("  Your name, as players read it when you act (Enter: the Superintendent's)")
            self.by_name = (name or "").strip()
        return self.by_name

    async def _console(self, server: str, line: str) -> list[str]:
        """One console command through the supervisor's connection; the reply's lines, printed."""
        results = await self.controller.verb("rcon", server, text=line, by=self.by_name or "")
        lines: list[str] = []
        for _, result in results:
            if not result.ok and not result.text:
                self.note(f"({result.message})", "yellow")
                continue
            if not result.text.strip():
                self.note("(no reply)")
            for text in result.text.splitlines():
                self.terminal.print(f"  {text}", None if result.ok else "yellow")
                lines.append(text)
        return lines

    # A: the console ----------------------------------------------------------------------------

    async def screen_admin(self) -> None:
        self.title("CONSOLE", "commands to one server over Huragok's own RCON connection; its events show as "
                              "they happen")
        picked = await self.select_servers("Send commands to", single=True, allowed=lambda r: r["can"]["talk"])
        if not picked:
            return
        row = picked[0]
        await self._who()
        self.heading(row["name"], "Console", note=f"as {self.by_name}" if self.by_name else "as the Superintendent")
        self.note("For example:  players   status   say hello   kick #3 camping   mute Bravo 30m   maps   help")
        self.note("Events arrive between commands; an empty line (or Esc) goes back to the dashboard.")

        def on_event(event: Event) -> None:
            line = describe_event(event)
            if line:
                self.held.push(f"    {line}", "gray" if event.name != "rcon.chat" else None)

        unwatch: Callable[[], None] = self.controller.watch(row["name"], on_event)
        try:
            while True:
                self.terminal.print()
                line = await self.prompt_line(f"  {row['name']}>", masked=mask_console_line)
                if not line:
                    return
                await self._console(row["name"], line)
        finally:
            unwatch()

    # D: settings: Huragok's own files, and every server's, changed a step at a time ----------------------------

    async def screen_settings(self) -> None:
        self.title("SETTINGS", "Huragok's own and every server's, a step at a time: each change checked, then written "
                               "in place with the file's comments kept")
        if self._preselect is not None:  # a server already chosen (its status card): straight to it
            picked = await self.select_servers("Change settings of", single=True)
            if picked:
                await self._server_settings(self._settings(), picked[0])
            return
        while True:
            settings = self._settings()
            files = self.huragok_files(settings)
            self.heading("Huragok", note=f"its own files, in {settings.home}")
            panel = self.listing([(what, name, about) for _, what, name, about in files],
                                 keys=[key for key, *_ in files], wrap=True)
            rows = self.controller.rows()
            self.heading("Servers", note="each one's dedicated.toml in groups, its playlists, custom games, bans and "
                                         "mods")
            if rows:
                panel += self.server_rows(rows)
            else:
                self.note("(no servers yet: N makes one)")
            answer = await self.prompt_line("  Which? (a server's number, or a letter; Esc goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip()
            if answer.upper() in FILES:
                await self.huragok_file(settings, answer.upper())
                continue
            row = next((r for r in rows if str(r["index"]) == answer.lstrip("#") or r["name"].lower() == answer.lower()),
                       None)
            if row is None:
                self.note(f"'{answer}' is not one of the choices.", "yellow")
                continue
            await self._server_settings(settings, row)

    async def _server_settings(self, settings: Any, row: Row) -> None:
        """One server's page: its dedicated.toml in groups and Huragok's own for it, then what else it has (its
        playlists, custom games, bans, mods), renaming it and a content folder of its own."""
        server = self._server(settings, row)
        if server is None:
            return
        while True:
            try:
                entries = catalogue(server.config_path, server.name, settings)
            except (OSError, ValueError) as error:
                self.note(f"Can't read {server.config_path}: {error}", "red")
                await self.wait_any_key(8)
                return
            self.heading(server.name, "Settings", note=f"{server.config_path} · Huragok's own in {settings.path.name}")
            counts = {key: sum(1 for s, _, _ in entries if s.group == key) for key, _, _ in GROUPS}
            rows = [(title, f"{counts[key]}", summary(entries, key) or what) for key, title, what in GROUPS]
            row = next((r for r in self.controller.rows() if r["name"] == row["name"]), row)  # its phase now
            stopped = row.get("phase") in ("idle", "faulted", "offline")  # backoff: about to be restarted
            extra = [(key, what, count, about) for key, what, count, about in self.server_page_extras(settings, server)]
            extra.append(("N", "rename this server", "", "its name everywhere: block, data, records, folder"
                          if stopped else "stop it first (X)"))
            if self._shared_content(settings, server):
                extra.append(("O", "its own content folder", "", "stop sharing the root's content/ (no copy on the "
                              "same drive)" if stopped else "stop it first (X)"))
            keys = [str(n) for n in range(1, len(rows) + 1)] + [key for key, *_ in extra]
            panel = self.listing(rows + [(what, count, about) for _, what, count, about in extra], keys=keys)
            answer = await self.prompt_line("  Which group? (number, or a letter; Esc goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            letters = [key for key, *_ in extra]
            if answer == "N":
                if await self._rename(settings, server, stopped):
                    return
                continue
            if answer == "O" and "O" in letters:
                await self._own_content(settings, server, stopped)
                settings = self._settings()
                server = self._server(settings, row) or server
                continue
            if answer in letters:
                await self._server_page_extra(settings, server, row, answer)
                settings = self._settings()
                server = self._server(settings, row) or server
                continue
            if not answer.isdigit() or not 1 <= int(answer) <= len(GROUPS):
                continue
            await self._settings_group(settings, server, row, GROUPS[int(answer) - 1])

    async def _settings_group(self, settings: Any, server: Any, row: Row, group: tuple[str, str, str]) -> None:
        key, title, what = group
        while True:
            entries = [(s, v, w) for s, v, w in catalogue(server.config_path, server.name, settings) if s.group == key]
            self.heading(server.name, "Settings", title, note=what)
            rows = []
            for setting, value, where in entries:
                origin = "" if where in ("this server", "the file", f"[{setting.scope}]", "settings.toml") else \
                    f"({where})"
                rows.append((setting.name, display(setting, value), origin, setting.help))
            panel = self.listing(rows, wrap=True)
            if len(entries) == 1:
                answer = "1" if await self.prompt_yes_no(f"  Change {entries[0][0].name}?", True) else ""
            else:
                answer = await self.prompt_line("  Which setting? (number; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(entries):
                return
            setting, value, where = entries[int(answer) - 1]
            if setting.kind == "plays":
                await self._edit_plays(settings, server, row)
            else:
                await self._edit_setting(settings, server, row, setting, value, where)
            settings = self._settings()
            server = self._server(settings, row) or server
            if len(entries) == 1:
                return

    async def _edit_setting(self, settings: Any, server: Any, row: Row, setting: Any, value: Any, where: str) -> None:
        self.heading(server.name, "Settings", setting.name,
                     note=f"{setting.help}. Takes effect: {APPLIES[setting.applies]}.")
        resettable = can_reset(setting, where)
        if resettable:
            self.note("This server sets its own value; - (or D) takes it out, so the general value or the default "
                      "applies again.")
        text: str | None
        if setting.kind == "bool":
            options = [("Y", "on"), ("N", "off")] + ([("D", "use the default")] if resettable else [])
            text = await self.choose(f"{setting.name}?", options, back="Esc leaves it")
            if text is None:
                return
            text = {"Y": "on", "N": "off", "D": "-"}[text]
        elif setting.kind == "choice":
            options = [(str(n), choice, "now" if choice == value else "") for n, choice in enumerate(setting.choices, 1)]
            if resettable:
                options.append(("D", "use the default", ""))
            picked = await self.choose(f"{setting.name}?", options, back="Esc leaves it")
            if picked is None:
                return
            text = "-" if picked == "D" else setting.choices[int(picked) - 1]
        else:
            hint = {"int": f"{setting.low}-{setting.high}" if setting.low is not None else "a number",
                    "list": "separated by commas", "urls": "https:// addresses, separated by commas",
                    "ids": "Workshop IDs or addresses, separated by spaces",
                    "lines": "one line, or several separated by |", "time": "HH:MM on the 24-hour clock, empty for none",
                    "path": f"relative to {server.folder.name}", "str": "text"}.get(setting.kind, "text")
            secret = "password" in setting.key
            text = await self.prompt_line(f"  New value ({hint}; now {display(setting, value)}; Enter leaves it)",
                                          secret=secret)
            if text is None:
                return
            if not text.strip():
                console = setting.key == "rcon_password" or setting.scope == "rcon" and setting.key == "password"
                if value in (None, "", []) or setting.kind in ("int", "path") or console:
                    self.note("Left as it was." + (" (Huragok needs the console: it can't be emptied here.)"
                                                   if console and value else ""))
                    await self.wait_any_key(3)
                    return
                if not await self.prompt_yes_no(f"  Empty {setting.key}?", False):
                    self.note("Left as it was.")
                    await self.wait_any_key(3)
                    return
                text = ""
        if resettable and text.strip() == "-":
            await self._apply_setting(server, row, setting, None)
            return
        try:
            new = parse(setting, text)
        except ValueError as error:
            self.note(f"Not changed: {error}.", "yellow")
            await self.wait_any_key(4)
            return
        version = await self._program_version(settings, server, row)
        if problem := check_path(setting, server.folder, str(new)) or check_value(
                setting, new, server.config_path, server.name, settings, version):
            self.note(f"Not changed: {problem}.", "yellow")
            await self.wait_any_key(4)
            return
        await self._apply_setting(server, row, setting, new)

    async def _apply_setting(self, server: Any, row: Row, setting: Any, new: Any) -> None:
        """Write it (None takes this server's own value out), say when it applies, offer a restart if it needs one."""
        if setting.scope == "huragok":
            self.show_results(await self.controller.verb("server_setting", server.name, key=setting.key, value=new))
            return
        try:
            if new is None:
                reset(server.config_path, server.name, setting)
            else:
                write(server.config_path, server.name, setting, new)
        except OSError as error:
            self.note(f"Couldn't write {server.config_path.name}: {error}", "red")
            await self.wait_any_key(4)
            return
        if new is None:
            self.note(f"{setting.key}: this server's own value taken out; the default applies. Takes effect: "
                      f"{APPLIES[setting.applies]}.", "green")
        else:
            self.note(f"{setting.key} = {display(setting, new)} written for {setting.where}. Takes effect: "
                      f"{APPLIES[setting.applies]}. The dashboard's events show the server's answer.", "green")
        await self._offer_restart(row, server.name, setting)

    async def _offer_restart(self, row: Row, name: str, setting: Any) -> None:
        """A setting `dedicated` only reads as it starts: restart it at a good moment, or leave it for later."""
        if setting.applies != "restart" or not row["can"]["restart"] or row.get("phase") not in ("running",
                                                                                                "external", None):
            return
        when = await self.choose(f"{setting.key} applies when the server restarts. Restart it when?", [
            ("A", "After this game", "players get a short warning once it ends"),
            ("E", "When nobody's on"),
            ("N", "Now", "players get the usual countdown"),
            ("L", "Later", "it takes effect at the server's next restart")], default="A", back="Esc: later")
        if when in (None, "L"):
            return
        self.show_results(await self.controller.verb("restart", name, when={"A": "after_game", "E": "when_empty",
                                                                            "N": "now"}[when],
                                                     reason=f"{setting.key} changed"))

    # What it plays ---------------------------------------------------------------------------

    async def _program_version(self, settings: Any, server: Any, row: Row) -> str | None:
        """The Project Reclaimer version a server runs: what it said while running, else its program file's own
        (`--version`, asked once per file, in a thread); None when neither is known."""
        if row.get("version"):
            return str(row["version"])
        from .serverupdates import program_of, program_version, readable

        program = program_of(settings, server)
        if program is None:
            return None
        version = await asyncio.to_thread(program_version, settings, program)
        return version if readable(version) else None

    async def _edit_plays(self, settings: Any, server: Any, row: Row) -> None:
        self.heading(server.name, "Settings", "What it plays", note=f"now: {server.plays}")
        choice = await self.choose("Play what?", [
            ("1", "A playlist", "players vote on a rotation of 3 or more games; from the next vote"),
            ("2", "One map and game type", "the same game every time"),
            ("3", "A saved custom game", "a game type file (.json) from its content folder, with its own rules"),
            ("4", "Campaign co-op", "a Halo 3 mission for up to 16 players"),
            ("5", "Halo 3: ODST", "a campaign mission or Firefight, on ODST's own engine, for up to 16 players")])
        if choice is None:
            return
        values: dict[str, Any] | None = None
        if choice == "1":
            from .playlists import load_playlist, playlist_files

            files = playlist_files(server.folder)
            if not files:
                self.note("No playlists in its playlists/ yet: G -> Playlists builds one.", "yellow")
                await self.wait_any_key(5)
                return
            rows = []
            for path in files:
                try:
                    playlist = load_playlist(path)
                    rows.append((path.name.removesuffix(".playlist.json"), f'"{playlist.name}" · '
                                                                           f"{len(playlist.games)} games"))
                except Exception as error:  # noqa: BLE001 - PlaylistError: listed, not picked
                    rows.append((path.name, f"(can't read it: {error})"))
            panel = self.listing(rows)
            answer = await self.prompt_line("  Which playlist? (number)", panel=panel)
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(files):
                return
            values = {"playlist": f"playlists/{files[int(answer) - 1].name}"}
        elif choice == "2":
            tool = await self._tool(settings)
            catalogue_ = await self._catalogue(tool, settings, server) if tool else None
            if catalogue_ is None:
                await self.wait_any_key(6)
                return
            maps = await self._pick(catalogue_.all_maps, "maps", groups=self._map_groups(catalogue_), single=True)
            if not maps:
                return
            games = await self._pick(catalogue_.all_games, "game types or variants",
                                     groups=self._game_groups(catalogue_), single=True)
            if not games:
                return
            values = {"map": maps[0].reference, "game": games[0].reference}
            if maps[0].kind == "map variant" and catalogue_.mod_maps and await self.prompt_yes_no(
                    "  Was this map variant made on a mod map? (base_map: needed when a mod shares its map's number)",
                    False):
                base = await self._pick(catalogue_.mod_maps, "mod maps", single=True)
                if base:
                    values["base_map"] = base[0].reference
        elif choice == "3":
            from .gameoptions import list_presets

            presets = list_presets(server.content_dir) if server.content_dir else []
            if not presets:
                self.note("No saved custom games (.json) in its content/Game Modes yet: G -> Game variants makes "
                          "one.", "yellow")
                await self.wait_any_key(5)
                return
            panel = self.listing([(p.name, p.mode_label, p.relative) for p in presets])
            answer = await self.prompt_line("  Which custom game? (number)", panel=panel)
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(presets):
                return
            path = presets[int(answer) - 1].path
            values = {"variant": Path(os.path.relpath(path, server.folder)).as_posix()}
        elif choice == "4":
            values = await self._campaign_values(server)
            if values is None:
                return
        else:
            values = await self._odst_values(settings, server)
            if values is None:
                return
        if problem := plays_problem(values, await self._program_version(settings, server, row)):
            self.note(f"Not changed: {problem}.", "yellow")
            await self.wait_any_key(4)
            return
        if ("campaign" in values or "odst" in values) and not await self._co_op_players(server):
            return
        try:
            write_plays(server.config_path, server.name, values)
        except OSError as error:
            self.note(f"Couldn't write {server.config_path.name}: {error}", "red")
            await self.wait_any_key(5)
            return
        from .livesettings import plays_of

        server.plays = plays_of(values)
        when = "from the next vote" if choice == "1" and server.plays.startswith("playlist") else \
            "once nobody is on (the server restarts by itself when the kind of game changes; otherwise from the next game)"
        self.note(f"{server.name} now plays {server.plays}: {when}. The dashboard's events show the server's answer.",
                  "green")
        await self.wait_any_key(6)

    def _playing_now(self, server: Any, key: str) -> dict[str, Any]:
        """The server's own `campaign`/`odst` setup as its file has it ({} when it plays something else)."""
        from .tomledit import read_toml

        try:
            block = next((b for b in read_toml(server.config_path).get("server", [])
                          if str(b.get("name", "")).lower() == server.name.lower()), {})
        except (OSError, ValueError):
            return {}
        return block[key] if isinstance(block.get(key), dict) else {}

    async def _co_op_players(self, server: Any) -> bool:
        """A campaign or ODST server holds 16 players at most, and a higher max_players stops its start: offer to
        bring this server's down first. False: the host would rather not switch."""
        from .livesettings import CO_OP_MAX_PLAYERS, current, find
        from .tomledit import read_toml

        setting = find("max_players", "server")
        try:
            value, where = current(read_toml(server.config_path), setting, server.name)
        except (OSError, ValueError):
            return True
        if not isinstance(value, int) or value <= CO_OP_MAX_PLAYERS:
            return True
        self.note(f"max_players is {value} ({where}): a campaign or ODST server takes {CO_OP_MAX_PLAYERS} at most and "
                  "won't start with more.", "yellow")
        if not await self.prompt_yes_no(f"  Set this server's max_players to {CO_OP_MAX_PLAYERS}?", True):
            self.note("Nothing changed.", "yellow")
            await self.wait_any_key(4)
            return False
        try:
            write(server.config_path, server.name, setting, CO_OP_MAX_PLAYERS)
        except OSError as error:
            self.note(f"Couldn't write {server.config_path.name}: {error}", "red")
            await self.wait_any_key(5)
            return False
        self.note(f"max_players = {CO_OP_MAX_PLAYERS} written for this server.", "green")
        return True

    async def _modifier_values(self, existing: dict[str, Any]) -> dict[str, Any] | None:
        """Campaign modifiers, a Halo 3 or ODST campaign server's: enemy health and damage, and (0.9.19) sprint.
        What this doesn't ask about, such as skulls written by hand, is kept. None: cancelled."""
        modifiers = dict(existing)
        if not await self.prompt_yes_no("  Change the campaign modifiers (enemy health and damage, sprint)?", False):
            return modifiers
        numbers = (("health_percent", "Enemy health %", 25, 1000, 25),
                   ("damage_percent", "Enemy damage %", 25, 1000, 25),
                   ("health_per_player_percent", "Extra enemy health % per player", 0, 100, 5),
                   ("damage_per_player_percent", "Extra enemy damage % per player", 0, 100, 5))
        for key, label, low, high, step in numbers:
            if not await self._modifier_number(modifiers, key, label, low, high, step):
                return None
        if await self.prompt_yes_no("  Sprint: every player can sprint (hold Sprint while running forward)?",
                                    bool(modifiers.get("sprint"))):
            modifiers["sprint"] = True
            if not await self._modifier_number(modifiers, "sprint_speed_percent", "Sprint speed %", 110, 200, 10):
                return None
            if not await self._modifier_number(modifiers, "sprint_stamina_seconds", "Seconds a full meter sprints for",
                                               1, 30, 1):
                return None
        else:
            for key in ("sprint", "sprint_speed_percent", "sprint_stamina_seconds"):
                modifiers.pop(key, None)
        return modifiers

    async def _modifier_number(self, modifiers: dict[str, Any], key: str, label: str, low: int, high: int,
                               step: int) -> bool:
        """One number into modifiers, checked; Enter leaves it, - takes it out. False: cancelled."""
        now = f"; now {modifiers[key]}, - takes it out" if key in modifiers else ""
        steps = f" in steps of {step}" if step > 1 else ""
        text = await self.prompt_line(f"  {label} ({low}-{high}{steps}; Enter leaves it{now})")
        if text is None:
            return False
        text = text.strip()
        if text == "-":
            modifiers.pop(key, None)
        elif text.isdigit() and low <= int(text) <= high and (int(text) - low) % step == 0:
            modifiers[key] = int(text)
        elif text:
            self.note(f"{text!r} isn't {low}-{high}{steps}; left as it was.", "yellow")
        return True

    async def _odst_values(self, settings: Any, server: Any) -> dict[str, Any] | None:
        """Halo 3: ODST (0.9.19): a campaign mission or Firefight, picked from what the program lists for this
        server's game folder, which needs ODST's files first (offered from this PC's MCC)."""
        from . import odst
        from .livesettings import DIFFICULTIES
        from .tomledit import read_toml

        try:
            game_key = str(read_toml(server.config_path).get("game") or "game")
        except (OSError, ValueError):
            game_key = "game"
        if not await self._offer_odst_files(settings, (server.folder / game_key).resolve()):
            await self.wait_any_key(8)
            return None
        tool = await self._tool(settings)
        catalogue_ = await self._catalogue(tool, settings, server) if tool else None
        listing = catalogue_.odst if catalogue_ is not None and not catalogue_.odst.empty else odst.builtin()
        old = self._playing_now(server, "odst")
        if old:
            self.note(f"Now: {odst.describe(old)}")
        kind = await self.choose("Which Halo 3: ODST?", [
            ("1", "A campaign mission", "Alpha-Nine's night in New Mombasa; everyone moves on to the next mission "
                                        "together"),
            ("2", "Firefight", "wave after wave on one of ODST's ten Firefight maps")],
            default="2" if odst.is_firefight(old) else "1")
        if kind is None:
            return None
        setup: dict[str, Any] = {}
        if kind == "1":
            picked = await self._pick(listing.missions, "missions", single=True)
            if not picked:
                return None
            mission = picked[0]
            setup["mission"], setup["insertion"] = mission.mission, 0
            if mission.insertion:
                rows = [("the mission's start",)] + [(f"start {n}",) for n in range(1, mission.insertion + 1)]
                panel = self.listing(rows, keys=[str(n) for n in range(mission.insertion + 1)])
                answer = await self.prompt_line(f"  Where does {mission.title} start? (0-{mission.insertion})", "0",
                                                panel=panel)
                if answer is None or not answer.strip().isdigit() or int(answer) > mission.insertion:
                    return None
                setup["insertion"] = int(answer)
        else:
            setup["mode"] = "firefight"
            maps = await self._pick(listing.maps, "Firefight maps", single=True)
            if not maps:
                return None
            setup["mission"], setup["insertion"] = maps[0].mission, maps[0].insertion
            types = await self._pick(listing.types, "Firefight game types", single=True)
            if not types:
                return None
            setup["variant"] = types[0].variant
        picked_difficulty = await self.choose("Difficulty?", [(str(n), d) for n, d in enumerate(DIFFICULTIES, 1)],
                                              default="2")
        if picked_difficulty is None:
            return None
        setup["difficulty"] = DIFFICULTIES[int(picked_difficulty) - 1]
        if kind == "1":
            modifiers = await self._modifier_values(old.get("modifiers") or {})
            if modifiers is None:
                return None
            if modifiers:
                setup["modifiers"] = modifiers
        return {"odst": setup}

    async def _campaign_values(self, server: Any) -> dict[str, Any] | None:
        from .livesettings import DIFFICULTIES, MISSION_STARTS, MISSIONS

        missions = list(MISSIONS.items())
        self.note("Its game folder needs the campaign missions (dedicated init --from --maps campaign).")
        panel = self.listing([(title, str(number)) for number, title in missions])
        answer = await self.prompt_line("  Which mission? (number)", panel=panel)
        if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(missions):
            return None
        mission = missions[int(answer) - 1][0]
        high = MISSION_STARTS.get(mission, 0)
        insertion = await self.prompt_line(f"  Start at rally point (0 is the mission's start; 0-{high})", "0")
        if insertion is None or not insertion.strip().isdigit() or int(insertion) > high:
            return None
        picked = await self.choose("Difficulty?", [(str(n), d) for n, d in enumerate(DIFFICULTIES, 1)], default="2")
        if picked is None:
            return None
        campaign: dict[str, Any] = {"mission": mission, "insertion": int(insertion),
                                    "difficulty": DIFFICULTIES[int(picked) - 1]}
        if await self.prompt_yes_no("  Players as their own characters (custom characters)?", False):
            campaign["custom_characters"] = True
        old = self._playing_now(server, "campaign")
        if old.get("customization"):  # character mods, set by hand: kept
            campaign["customization"] = old["customization"]
        modifiers = await self._modifier_values(old.get("modifiers") or {})
        if modifiers is None:
            return None
        if modifiers:
            campaign["modifiers"] = modifiers
        return {"campaign": campaign}

    # Renaming, and a content folder of its own -----------------------------------------------

    def _shared_content(self, settings: Any, server: Any) -> bool:
        from .serverops import shares_content

        return bool(shares_content(settings, server))

    async def _rename(self, settings: Any, server: Any, stopped: bool) -> bool:
        """True when it was renamed (the screen closes: the server has a new name on the dashboard)."""
        from .serverops import ServerOpError, rename_server

        if not stopped:
            self.note("Renaming moves its data and folder: stop it first (X), then rename it.", "yellow")
            await self.wait_any_key(5)
            return False
        name = await self.prompt_line(f"  New name for {server.name}")
        if not name:
            return False
        row = next((r for r in self.controller.rows() if r["name"] == server.name), {})  # as it is now
        try:
            rename_server(settings, server, name, say=lambda text: self.note(text, "gray"),
                          phase=row.get("phase", "gone"), pid=row.get("pid"))
        except (ServerOpError, OSError) as error:
            self.note(f"Not renamed: {error}", "yellow")
            self.controller.discover()
            await self.wait_any_key(6)
            return getattr(error, "partly", False)  # half-done: this screen's view of it is stale
        self.controller.discover()
        self.note(f"Renamed to {name}. It shows on the dashboard under its new name within seconds; S starts it.",
                  "green")
        await self.wait_any_key(6)
        return True

    async def _own_content(self, settings: Any, server: Any, stopped: bool) -> None:
        from .serverops import ServerOpError, own_content, shares_content

        sharing = shares_content(settings, server)
        self.note(f"It shares {server.content_dir} with {', '.join(sharing)}. Its own copy lets the Superintendent "
                  "install Workshop updates after its games, and keeps its maps and game types its own.")
        if not stopped:
            self.note("Stop it first (X): the server program reads its content folder as it starts.", "yellow")
            await self.wait_any_key(6)
            return
        if not await self.prompt_yes_no("  Give it its own content folder?", True):
            return
        try:
            await self.slow(own_content, settings, server, lambda text: None)
        except (ServerOpError, OSError) as error:
            self.note(f"Not done: {error}", "yellow")
            await self.wait_any_key(6)
            return
        self.note(f"{server.name} has its own content folder now ({server.folder / 'content'}); S starts it.", "green")
        await self.wait_any_key(6)

    # B: bans, admins, VPN -----------------------------------------------------------------------

    async def screen_bans(self) -> None:
        self.title("BANS, ADMINS AND THE VPN BLOCK", "through the server's console; it must be running")
        settings = self._settings()
        picked = await self.select_servers("Look at", single=True)
        if not picked:
            return
        await self._bans(settings, picked[0])

    async def _bans(self, settings: Any, row: Row) -> None:
        row = next((r for r in self.controller.rows() if r["name"] == row["name"]), row)  # as it is now
        live = bool(row["can"]["talk"])
        server = self._server(settings, row)
        if server is None:
            return
        if not live:
            self.heading(row["name"], "Bans")
            self.note(f"{row['name']} isn't running: the ban list from its folder (dedicated bans); bans and unbans "
                      "need it running, or `dedicated ban` from the command line.")
            tool = await self._tool(settings)
            if tool is not None:
                result = await tool.run(server.folder, "bans", config=tool.config_name)
                for line in result.lines[:60]:
                    self.terminal.print(f"  {line}")
            await self.wait_any_key(20)
            return
        await self._who()
        while True:
            self.heading(row["name"], "Bans, admins and the VPN block",
                         note=f"as {self.by_name}" if self.by_name else "as the Superintendent")
            choice = await self.choose("Which?", [
                ("L", "List bans"), ("A", "List admins"),
                ("U", "Unban"), ("+", "Add an admin"),
                ("N", "Ban someone offline (ID, IP or range)"), ("-", "Remove an admin"),
                ("P", "Pardon a kick"), ("V", "VPN block: status / check an address"),
                ("W", "Let a player through the VPN block"), ("X", "Take that back")])
            if choice is None:
                return
            if choice == "L":
                await self._console(row["name"], "bans")
            elif choice == "A":
                await self._console(row["name"], "admin list")
            elif choice == "V":
                address = await self.prompt_line("  An address to check (Enter: the block's status)")
                if address is None:
                    continue
                await self._console(row["name"], f"vpn {address}".strip())
            else:
                prompts = {"U": ("unban", "Name, player ID, device fingerprint, IP or range"),
                           "N": ("ban", "Player ID, IP or range, then a time (30m, 7d) and a reason, e.g. "
                                        "203.0.113.4 7d spamming"),
                           "P": ("pardon", "Player ID, IP, range or the name they were kicked under"),
                           "+": ("admin add", "A connected player (#number, name) or a player ID"),
                           "-": ("admin remove", "The admin (player, player ID, or the name they were added under)"),
                           "W": ("vpnallow", "Player, player ID, IP or range, then a note"),
                           "X": ("vpnrevoke", "Player ID, IP or range, written as it was allowed")}
                command, hint = prompts[choice]
                target = await self.prompt_line(f"  {command}: {hint}")
                if not target:
                    continue
                await self._console(row["name"], f"{command} {target}")

    # U: updates ------------------------------------------------------------------------------

    async def screen_update(self) -> None:
        self.title("UPDATES")
        choice = await self.choose("Which?", [
            ("1", "Huragok", "its GitHub releases; the servers keep running while it updates"),
            ("2", "The server program", "Project Reclaimer releases: servers with auto_update on install them "
                                        "themselves (Huragok steps in when one doesn't); the others get them "
                                        "from here, after their game"),
            ("3", "Workshop mods", "new versions of the servers' Workshop items: installed when a game ends"),
            ("4", "Clean up", "what updates and mods leave behind: old program copies, old mod copies…")])
        if choice is None:
            return
        if choice == "1":
            await self._update_supervisor()
        elif choice == "2":
            await self._update_program()
        elif choice == "3":
            await self._update_workshop()
        else:
            await self.screen_cleanup()

    async def screen_cleanup(self) -> None:
        from . import upkeep

        settings = self._settings()
        self.heading("Clean up", note="what's safe to remove, with its size; chat logs and settings are never offered")
        leftovers = await self.slow(upkeep.cleanable, settings, note="measuring what's there (keys pressed meanwhile "
                                                                     "are ignored)…")
        if not leftovers:
            self.note("Nothing to clean up.", "green")
            await self.wait_any_key(8)
            return
        total = sum(item.size for item in leftovers)
        panel = self.listing([(item.label, item.size_text, item.why) for item in leftovers], wrap=True)
        self.note(f"{len(leftovers)} item(s), {upkeep.size_text(total)} in all.")
        answer = await self.prompt_line("  Remove which? (numbers like 1,3 5-8, or A for all; Esc keeps them)",
                                        panel=panel)
        if not answer:
            return
        from .catalogue import parse_picks

        picks = list(range(1, len(leftovers) + 1)) if answer.strip().upper() == "A" else \
            parse_picks(answer, len(leftovers)) or []
        gone, kept = await asyncio.to_thread(upkeep.remove, [leftovers[n - 1] for n in picks])
        for line in gone:
            self.note(f"removed {line}", "green")
        for line in kept:
            self.note(f"kept {line}", "yellow")
        await self.wait_any_key(10)

    async def _update_supervisor(self) -> None:
        self.heading("Huragok")
        self.note("Asking GitHub...", "cyan")
        results = await self.controller.verb("check_update")
        self.show_results(results)
        for _, result in results:
            for line in result.text.splitlines():
                self.terminal.print(f"    {line}", "gray")
        if not any(r.ok and r.text for _, r in results):
            await self.wait_any_key(10)
            return
        if not await self.prompt_yes_no("  Install it now? (this window closes and reopens on the new version)", True):
            return
        self.show_results(await self.controller.verb("update"))
        await self.wait_any_key(8)

    async def _update_program(self) -> None:
        self.heading("The server program")
        self.note("Asking GitHub for the newest Project Reclaimer release...", "cyan")
        results = await self.controller.verb("check_program_update")
        self.show_results(results)
        for _, result in results:
            for line in result.text.splitlines():
                self.terminal.print(f"    {line}", "gray")
        if not any(r.ok and r.text for _, r in results):
            await self.wait_any_key(10)
            return
        self.note("Players must be on the server's exact version: once a server is updated, players update their "
                  "game and come back.")
        when = await self.choose("Install it and restart each running server when?",
                                 [("A", "After its game"), ("N", "Now (mid-game)")], default="A",
                                 back="Esc leaves it")
        if when is None:
            return
        self.note("Downloading and checking it...", "cyan")
        self.show_results(await self.controller.verb("update_program", when="after_game" if when == "A" else "now"))
        await self.wait_any_key(15)


    async def _update_workshop(self) -> None:
        self.heading("Workshop mods")
        self.note("Asking Steam for each item's newest version...", "cyan")
        results = await self.controller.verb("check_workshop")
        self.show_results(results)
        for _, result in results:
            for line in result.text.splitlines():
                self.terminal.print(f"    {line}", "white" if not line.startswith(" ") else "gray")
        if not any(r.ok and "waiting" in r.message for _, r in results):
            await self.wait_any_key(15)
            return
        when = await self.choose("Install the new versions when?", [
            ("A", "After the game", "each server stops once its game ends, swaps the mod in (about a minute) and "
                                    "starts again"),
            ("N", "Now", "players get the usual countdown")], default="A", back="Esc leaves it to the setting")
        if when is None:
            return
        self.show_results(await self.controller.verb("update_workshop", when="after_game" if when == "A" else "now"))
        await self.wait_any_key(10)


__all__ = ["ManageScreens", "describe_event", "FOLDER_SETTINGS", "SERVER_SETTINGS"]
