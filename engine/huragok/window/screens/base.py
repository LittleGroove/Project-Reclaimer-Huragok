"""What every screen of the window stands on: drawing a screen and asking the host, the pickers several screens
share, and the screens one group opens in another.

    SetupScreens   (setup.py)    N new server, G content, I SteamCMD and game files, C check
    ManageScreens  (manage.py)   A console, D settings, B bans and admins, U updates
    GameScreens    (games.py)    custom games and a playlist's games
    FileScreens    (files.py)    D's Huragok files, O open

Each extends Screens; Menu (dashboard.py) is all of them, with the dashboard and its keys. A screen asks with
`choose`, `prompt_line`, `prompt_key`, `prompt_yes_no`; it draws with `title`, `heading`, `note`, `listing`; slow work
goes through `slow`, so every server is looked after meanwhile.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from ...commandline.watch import parts_for
from ...content.catalogue import Catalogue, Entry, load_catalogue, parse_picks
from ...content.mccimport import content_root_of
from ...core.requests import Result
from ...core.serverfolders import ServerConfig
from ...core.settings import Settings
from ...program.dedicated import SLOW, Dedicated, DedicatedError, ToolResult, tool_for
from ..controllers import Row
from ..tui import Parts, cells, format_listing, heading_parts, pad


PROMPT_TIMEOUT = 120.0


Block = list[Parts]  # a few lines of a screen, drawn again when log lines interrupt the question beneath them


WIDE = 110  # rules and columns never go wider than this, however wide the window


Option = tuple[str, str] | tuple[str, str, str]  # (key, label) or (key, label, note)
Line = str | tuple[str, str | None]  # a line for _pages: plain, or with its colour


PAGE = 30


class Screens:
    """The toolkit and the shared pickers; Menu sets these as it starts."""

    terminal: Any
    controller: Any
    keyboard: Any
    held: Any
    by_name: str | None = None  # who's acting, asked once per window
    _preselect: str | None = None

    # Drawing a screen and asking the host -----------------------------------------------------------------

    async def slow(self, work: Callable[..., Any], *args: Any, note: str | None = None, colour: str = "cyan") -> Any:
        """Thread work that takes a while (the game files' copy, SteamCMD's install, ODST's files, the content
        listing): every server is looked after meanwhile. Keys pressed while it runs are dropped once it's done,
        so a stray Esc doesn't cancel the next question, nor a Q then Y close the window."""
        self.note(note or "This takes a while: keys pressed meanwhile are ignored until it's done.", colour)
        try:
            return await asyncio.to_thread(work, *args)
        finally:
            self.keyboard.drain()

    def width(self) -> int:
        return min(WIDE, self.terminal.size()[0] - 1)

    def title(self, text: str, note: str | None = None) -> None:
        """`SUPERINTENDENT › RESTART`: the part whose screen it is, in its colour, then the screen."""
        self.terminal.print_parts([(f" {self.persona.name.upper()} › ", self.persona.colour), (text, "white")])
        if note:
            self.terminal.print(f" {note}", "gray")
        self.terminal.print(" " + "─" * (self.width() - 1), "gray")

    def heading(self, *crumbs: str, note: str | None = None) -> None:
        """A section's start: the path to where the host is, drawn as a rule, e.g. ─── Alpha › Playlists ───"""
        self.terminal.print()
        self.terminal.print_parts(heading_parts(" › ".join(c for c in crumbs if c), self.width()))
        if note:
            self.terminal.print(f"  {note}", "gray")

    def note(self, text: str, colour: str | None = "gray") -> None:
        self.terminal.print(f"  {text}", colour)

    def show(self, block: Block) -> None:
        for parts in block:
            self.terminal.print_parts(parts)

    def listing(self, rows: list[tuple[str, ...]], *, show: bool = True, **options: Any) -> Block:
        """Rows as aligned columns (tui.format_listing): numbered [ 1] or keyed [B], the first column bright,
        the rest gray. Returns the lines, to hand a question as its panel."""
        block = format_listing(rows, self.width(), **options)
        if show:
            self.show(block)
        return block

    async def choose(self, prompt: str, options: list[Option], *, default: str | None = None,
                     back: str | None = "Esc back", columns: int | None = None) -> str | None:
        """A menu block of [key] options in tidy columns (two when they fit, one when a note goes with
        each), the way back in gray, then one key. None when the host backs out."""
        labels = [f"[{o[0]}] {o[1]}" for o in options]
        notes = [o[2] if len(o) > 2 else "" for o in options]
        width = self.width()
        cell = max(cells(label) for label in labels) + 3
        if columns is None:
            columns = 2 if not any(notes) and len(options) >= 4 and 3 + 2 * cell <= width else 1
        block: Block = [[("", None)]]
        if columns == 1:
            label_width = max(cells(label) for label in labels)
            for label, note in zip(labels, notes):
                parts: Parts = [(f"   {pad(label, label_width) if note else label}", None)]
                if note:
                    parts.append((f"  {note}", "gray"))
                block.append(parts)
        else:
            for start in range(0, len(labels), columns):
                row = "".join(pad(label, cell) for label in labels[start:start + columns]).rstrip()
                block.append([(f"   {row}", None)])
        if back:
            block.append([(f"   {back}", "gray")])
        self.show(block)
        return await self.prompt_key(f"  {prompt}", [o[0] for o in options], default, panel=block)

    def _draw_again(self, panel: Block | None, text: str, buffer: list[str]) -> None:
        """The panel and the question, as they were before held lines pushed them up."""
        for parts in panel or []:
            self.terminal.print_parts(parts, record=False)
        self.terminal.print_parts([(text, "white"), ("".join(buffer), None)], end="", record=False)

    async def prompt_line(self, prompt: str, default: str = "", *, panel: Block | None = None,
                          secret: bool = False, masked: Callable[[str], str] | None = None) -> str | None:
        """A line of text; Esc or two idle minutes give None. A secret (or a prompt about a password) shows as
        stars and goes into the transcript as ********; `masked` hides the secret part of a line (the console's)."""
        self.held.flush()
        secret = secret or "password" in prompt.lower()
        text = f"{prompt} " if prompt.endswith(">") else f"{prompt}{' [' + default + ']' if default else ''}: "
        buffer: list[str] = []
        self.terminal.print(text, "white", end="")
        shown = (lambda: "*" * len(buffer)) if secret else (lambda: "".join(buffer))
        self.held.waiting(lambda: self._draw_again(panel, text, list(shown())), lambda: cells(text) + cells(shown()))
        try:
            while True:
                key = await self.keyboard.read(PROMPT_TIMEOUT)
                if key is None:
                    self.terminal.print(("" if not buffer else " ") + "(timed out)", "gray")
                    return None
                if key == "ENTER":
                    self.terminal.print()
                    answer = "".join(buffer).strip() or default
                    noted = "********" if answer and secret else masked(answer) if masked else answer
                    self.terminal.note("> " + noted)
                    return answer
                if key in ("ESC", "CTRL_C"):
                    self.terminal.print(("" if not buffer else " ") + "(cancelled)", "gray")
                    return None
                if key == "BACKSPACE":
                    if buffer:
                        gone = buffer.pop()
                        self.terminal.backspace(1 if secret else max(1, cells(gone)))
                    continue
                if len(key) == 1:
                    buffer.append(key)
                    self.terminal.print("*" if secret else key, end="", record=False)
        finally:
            self.held.done()

    async def prompt_key(self, prompt: str, keys: list[str], default: str | None = None, *,
                         panel: Block | None = None) -> str | None:
        self.held.flush()
        text = f"{prompt}: "
        self.terminal.print(text, "white", end="")
        self.held.waiting(lambda: self._draw_again(panel, text, []), lambda: cells(text))
        try:
            while True:
                key = await self.keyboard.read(PROMPT_TIMEOUT)
                if key is None:
                    self.terminal.print("(timed out)", "gray")
                    return None
                if key in ("ESC", "CTRL_C"):
                    self.terminal.print("(cancelled)", "gray")
                    return None
                choice = default if key == "ENTER" else key.upper()
                if choice and choice in keys:
                    self.terminal.print(choice)
                    self.terminal.note(f"> {choice}")
                    return choice
        finally:
            self.held.done()

    async def prompt_yes_no(self, prompt: str, default: bool = False) -> bool:
        choice = await self.prompt_key(f"{prompt} ({'Y/n' if default else 'y/N'})", ["Y", "N"], "Y" if default else "N")
        return choice == "Y"

    async def wait_any_key(self, seconds: float = 15) -> None:
        self.held.flush()
        self.terminal.print()
        text = f"  Press any key to go on (or wait {seconds:.0f}s)..."
        self.terminal.print(text, "gray")
        self.held.waiting(lambda: self.terminal.print(text, "gray", record=False), lambda: 0)
        try:
            await self.keyboard.read(seconds)
        finally:
            self.held.done()

    def server_rows(self, rows: list[Row], *, show: bool = True) -> Block:
        """The servers as a numbered list: name, state, who's on and what's playing."""
        table = []
        for row in rows:
            about = str(row.get("players") or "")
            if row.get("names"):
                about += f" {row['names']}"
            game = row.get("game") or (f"plays {row['plays']}" if row.get("plays") else "")
            table.append((row["name"], str(row.get("status", "")), " · ".join(b for b in (about.strip(), game) if b)))
        return self.listing(table, colours=[r.get("color") for r in rows], keys=[str(r["index"]) for r in rows],
                            show=show)

    async def select_servers(self, verb: str, *, single: bool = False,
                             allowed: Callable[[Row], bool] | None = None) -> list[Row]:
        rows = self.controller.rows()
        choices = [r for r in rows if allowed is None or allowed(r)]
        if self._preselect is not None:
            chosen = next((r for r in choices if r["name"] == self._preselect), None)
            if chosen is None:
                self.note(f"{self._preselect} can't be picked for that right now.", "yellow")
                await self.wait_any_key(5)
                return []
            return [chosen]
        if not choices:
            self.note(f"No server to {verb.lower()} right now.", "yellow")
            await self.wait_any_key(5)
            return []
        if len(choices) == 1 and len(rows) > 1:
            self.note(f"Only {choices[0]['name']} can be picked for this.")
        self.terminal.print()
        panel = self.server_rows(choices)
        hint = "number" if single else "numbers like 1,2, or A for all"
        answer = await self.prompt_line(f"  {verb} which server? ({hint}; Esc cancels)", panel=panel)
        if not answer:
            return []
        if not single and answer.strip().upper() == "A":
            return choices
        picked = []
        for part in answer.replace(",", " ").split():
            row = next((r for r in choices if str(r["index"]) == part.lstrip("#")
                        or r["name"].lower() == part.lower()), None)
            if row is None:
                self.note(f"'{part}' is not one of the choices.", "yellow")
                await self.wait_any_key(5)
                return []
            picked.append(row)
        return picked[:1] if single else picked

    def show_results(self, results: list[tuple[str, Result]]) -> None:
        for name, result in results:
            self.terminal.print(f"  {name}: {result.message}", "green" if result.ok else "yellow")
            for line in result.text.splitlines() if result.text and not result.ok else []:
                self.terminal.print(f"      {line}", "gray")

    async def warn_or_now(self, row: Row, doing: str) -> bool | None:
        """Players on: countdown or right now? True = now, False = warn, None = leave it."""
        if row["phase"] not in ("running", "external"):
            return False
        players = str(row.get("players", "")).split("/")[0]
        if players == "0":
            self.note(f"{row['name']}: nobody is on, so it goes straight away.")
            return False
        who = "the player count isn't known" if not players else f"{players} player(s) online" + (
            f" ({row['names']})" if row.get("names") else "")
        self.note(f"{row['name']}: {who}.", "yellow")
        choice = await self.choose(f"{row['name']}: which?", [("W", "Warn them first (the usual countdown)"),
                                                              ("N", f"{doing.capitalize()} it right now")],
                                   default="W", back="Esc leaves it as it is")
        if choice is None:
            return None
        return choice == "N"

    # Shared by several screens --------------------------------------------------------------------------

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
        from ...servers.logs import FileTail

        path = settings.console_log(server.slug)
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
            if text.isdecimal() and low <= int(text) <= high:
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

    # Opened from another group's screens (each defined by the group named) ------------------------------

    async def _offer_odst_files(self, settings: Settings, game: Path) -> bool:
        raise NotImplementedError("SetupScreens's")

    async def _playlists(self, settings: Settings, tool: Dedicated, server: ServerConfig, catalogue: Catalogue) -> None:
        raise NotImplementedError("SetupScreens's")

    def server_files(self, settings: Settings, server: ServerConfig) -> list[tuple[str, str, Path]]:
        raise NotImplementedError("SetupScreens's")

    async def _custom_games(self, settings: Any, tool: Any, server: Any) -> None:
        raise NotImplementedError("GameScreens's")

    async def _play_custom_game(self, settings: Any, server: Any, info: Any) -> dict[str, Any] | None:
        raise NotImplementedError("GameScreens's")

    async def _save_playlist(self, tool: Dedicated, server: ServerConfig, playlist: Any) -> bool:
        raise NotImplementedError("SetupScreens's")

    async def _playable(self, tool: Dedicated, server: ServerConfig, path: Path) -> bool:
        raise NotImplementedError("SetupScreens's")

    async def _plays_checked(self, settings: Any, server: Any, values: dict[str, Any]) -> bool:
        raise NotImplementedError("ManageScreens's")

    async def _edit_plays(self, settings: Any, server: Any, row: Row) -> None:
        raise NotImplementedError("ManageScreens's")

    async def _content_home(self, settings: Settings, server: ServerConfig, row: Row) -> None:
        raise NotImplementedError("SetupScreens's")

    async def _other_servers(self, settings: Settings, server: ServerConfig, what: str) -> list[ServerConfig]:
        raise NotImplementedError("SetupScreens's")

    async def _edit_entry(self, server: Any, catalogue: Any, playlist: Any, index: int) -> bool:
        raise NotImplementedError("GameScreens's")

    async def _bans(self, settings: Any, row: Row) -> None:
        raise NotImplementedError("ManageScreens's")

    async def _offer_restart(self, row: Row, name: str, setting: Any) -> None:
        raise NotImplementedError("ManageScreens's")

    def huragok_files(self, settings: Any) -> list[tuple[str, str, str, str]]:
        raise NotImplementedError("FileScreens's")

    async def huragok_file(self, settings: Any, key: str) -> None:
        raise NotImplementedError("FileScreens's")

    def server_page_extras(self, settings: Any, server: Any) -> list[tuple[str, str, str, str]]:
        raise NotImplementedError("FileScreens's")

    async def _server_page_extra(self, settings: Any, server: Any, row: Row, key: str) -> None:
        raise NotImplementedError("FileScreens's")
