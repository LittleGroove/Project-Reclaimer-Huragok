"""The window's screens for managing a running server: A the console (with the server's events as
they happen, and your name on what you do), D its settings changed live, B bans, admins and the VPN
block, U updates (the supervisor's, and the server program's).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .events import Event
from .livesettings import APPLIES, FOLDER_SETTINGS, SERVER_SETTINGS, catalogue, parse, show, write
from .manager import Result

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
    def _tool(self, settings: Any) -> Any: ...

    async def _who(self) -> str:
        if self.by_name is None:
            name = await self.prompt_line("  Your name, as players read it when you act (Enter: the supervisor's)")
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
        self.title("CONSOLE", "commands to one server over the supervisor's own RCON connection; its events show as "
                              "they happen")
        picked = await self.select_servers("Send commands to", single=True, allowed=lambda r: r["can"]["talk"])
        if not picked:
            return
        row = picked[0]
        await self._who()
        self.heading(row["name"], "Console", note=f"as {self.by_name}" if self.by_name else "as the supervisor")
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
                line = await self.prompt_line(f"  {row['name']}>")
                if not line:
                    return
                await self._console(row["name"], line)
        finally:
            unwatch()

    # D: settings changed live -------------------------------------------------------------------

    async def screen_settings(self) -> None:
        self.title("SERVER SETTINGS", "dedicated.toml, changed in place and applied by the server while it runs")
        settings = self._settings()
        picked = await self.select_servers("Change settings of", single=True)
        if not picked:
            return
        server = self._server(settings, picked[0])
        if server is None:
            return
        while True:
            try:
                entries = catalogue(server.config_path, server.name)
            except (OSError, ValueError) as error:
                self.note(f"Can't read {server.config_path}: {error}", "red")
                await self.wait_any_key(8)
                return
            self.heading(server.name, "Settings", note=str(server.config_path))
            rows: list[tuple[str, ...]] = []
            for setting, value, where in entries:
                label = setting.key if setting.scope in ("server", "folder") else f"[{setting.scope}] {setting.key}"
                origin = "" if where in ("this server", "the file", f"[{setting.scope}]") else where.strip("[]")
                rows.append((label, show(value), origin, setting.help))
            split = len(SERVER_SETTINGS)
            self.note("This server:", "white")
            self.listing(rows[:split], keys=[str(n) for n in range(1, split + 1)])
            if rows[split:]:
                self.note("Every server in its folder:", "white")
                self.listing(rows[split:], keys=[str(n) for n in range(split + 1, len(rows) + 1)])
            answer = await self.prompt_line("  Which setting? (number; Esc goes back)")
            if not answer or not answer.strip().isdigit() or not 1 <= int(answer) <= len(entries):
                return
            setting, value, where = entries[int(answer) - 1]
            self.heading(server.name, "Settings", setting.key,
                         note=f"{setting.help}. Takes effect: {APPLIES[setting.applies]}.")
            if setting.key == "playlist" and setting.scope == "server":
                from .playlists import playlist_files

                files = playlist_files(server.folder)
                if not files:
                    self.note("(no playlists in playlists/; G builds one)", "yellow")
                    continue
                panel = self.listing([(f"playlists/{path.name}",) for path in files])
                picked_file = await self.prompt_line("  Which playlist? (number)", panel=panel)
                if not picked_file or not picked_file.strip().isdigit() or not 1 <= int(picked_file) <= len(files):
                    continue
                text = f"playlists/{files[int(picked_file) - 1].name}"
            elif setting.kind == "bool":
                text = await self.choose(f"{setting.key}?", [("Y", "on"), ("N", "off")], back="Esc leaves it")
                if text is None:
                    continue
                text = "on" if text == "Y" else "off"
            else:
                hint = {"int": f"{setting.low}-{setting.high}" if setting.low is not None else "a number",
                        "choice": " / ".join(setting.choices), "list": "separated by commas",
                        "str": "text"}[setting.kind]
                text = await self.prompt_line(f"  New value ({hint}; now {show(value)})")
                if text is None:
                    continue
            try:
                new = parse(setting, text)
            except ValueError as error:
                self.note(f"Not changed: {error}.", "yellow")
                continue
            try:
                write(server.config_path, server.name, setting, new)
            except OSError as error:
                self.note(f"Couldn't write {server.config_path.name}: {error}", "red")
                continue
            self.note(f"{setting.key} = {show(new)} written for {setting.where}. Takes effect: "
                      f"{APPLIES[setting.applies]}. The dashboard's events show the server's answer.", "green")
            if setting.applies == "restart" and picked[0]["can"]["restart"]:
                if await self.prompt_yes_no("  Restart this server now (players get the usual countdown)?", False):
                    self.show_results(await self.controller.verb("restart", server.name, immediate=False,
                                                                 reason=f"{setting.key} changed"))

    # B: bans, admins, VPN -----------------------------------------------------------------------

    async def screen_bans(self) -> None:
        self.title("BANS, ADMINS AND THE VPN BLOCK", "through the server's console; it must be running")
        settings = self._settings()
        picked = await self.select_servers("Look at", single=True)
        if not picked:
            return
        row = picked[0]
        live = bool(row["can"]["talk"])
        server = self._server(settings, row)
        if server is None:
            return
        if not live:
            self.heading(row["name"], "Bans")
            self.note(f"{row['name']} isn't running: the ban list from its folder (dedicated bans); bans and unbans "
                      "need it running, or `dedicated ban` from the command line.")
            tool = self._tool(settings)
            if tool is not None:
                result = await tool.run(server.folder, "bans", config=tool.config_name)
                for line in result.lines[:60]:
                    self.terminal.print(f"  {line}")
            await self.wait_any_key(20)
            return
        await self._who()
        while True:
            self.heading(row["name"], "Bans, admins and the VPN block",
                         note=f"as {self.by_name}" if self.by_name else "as the supervisor")
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
            ("1", "The supervisor", "its GitHub releases; the servers keep running while it updates"),
            ("2", "The server program", "Project Reclaimer releases: put in place of every copy, then each running "
                                        "server restarts after its game, or now")])
        if choice is None:
            return
        if choice == "1":
            await self._update_supervisor()
        else:
            await self._update_program()

    async def _update_supervisor(self) -> None:
        self.heading("The supervisor")
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


__all__ = ["ManageScreens", "describe_event", "FOLDER_SETTINGS", "SERVER_SETTINGS"]
