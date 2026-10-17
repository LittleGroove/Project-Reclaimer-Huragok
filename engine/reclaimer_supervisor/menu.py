"""The window: a live dashboard of every server, and single-key commands.

The dashboard redraws once a second. Pressing a key opens a screen for that
action; the servers keep being looked after meanwhile, since the supervisor runs
in the same event loop. A prompt left alone for two minutes gives the window back.

Two controllers draw the same dashboard: the supervising window's own (LocalController),
and a second window's, fed by records/live.json and talking through the requests
mailbox (RemoteController). When the supervising window closes, the second one
takes over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

from . import __version__, desktop, mailbox
from .manager import Result
from .menu_manage import ManageScreens
from .menu_setup import SetupScreens
from .tui import Interruptions, Keyboard, Parts, RecentLog, Terminal, event_colour, format_listing, heading_parts

if TYPE_CHECKING:
    from .core import Supervisor

log = logging.getLogger(__name__)

PROMPT_TIMEOUT = 120.0
LIVE_STALE = 15.0
# A logs/supervisor.log line: date, time (ms dropped), level, the part that spoke (dropped), the message.
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d (?P<time>\d\d:\d\d:\d\d)(?:,\d+)? (?P<level>\w+)\s+(?:[\w.]+: )?(?P<message>.*)$")
TRANSCRIPT = "window.log"  # in logs/: every screen as it was shown, with what was asked and answered
TRANSCRIPT_MAX = 5_000_000
Row = dict[str, Any]


class Controller(Protocol):
    supervising: bool
    home: Path
    launcher: str

    def header(self) -> list[str]: ...
    def rows(self) -> list[Row]: ...
    def events(self, count: int) -> list[tuple[int, str]]: ...
    def problems(self) -> list[str]: ...
    def finished(self) -> str | None: ...
    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]: ...
    def request_quit(self) -> None: ...
    def refresh_settings(self) -> Any: ...  # the Settings, with the server folders as they are now
    def discover(self) -> None: ...  # look for server folders now (a new one was just made)
    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]: ...  # a server's events


# Rendering -----------------------------------------------------------------------------------

def table_lines(rows: list[Row], width: int) -> list[tuple[str, str | None]]:
    """The servers as the dashboard's table: a header, one coloured line per server, its detail beneath."""
    lines: list[tuple[str, str | None]] = []
    name_width = min(28, max(8, *(len(r["name"]) for r in rows))) if rows else 8
    status_width = 36
    players_width = 18
    fmt = f"  {{0:<2}} {{1:<{name_width}}} {{2:<6}} {{3:<4}} {{4:<{status_width}}} {{5:<{players_width}}} {{6}}"
    lines.append((fmt.format("#", "SERVER", "PORT", "AUTO", "STATUS", "PLAYERS", "GAME"), "white"))
    for row in rows:
        players = str(row.get("players", ""))
        if row.get("names"):
            players = f"{players} {row['names']}"
        if len(players) > players_width - 1:
            players = players[: players_width - 4] + "..."
        name = row["name"] if len(row["name"]) <= name_width else row["name"][: name_width - 1] + "…"
        lines.append((fmt.format(row.get("index", ""), name, row.get("port") or "", "yes" if row.get("autostart") else "no",
                                 str(row.get("status", ""))[:status_width], players,
                                 row.get("game") or (f"plays {row['plays']}" if row.get("plays") else "")),
                      row.get("color", "gray")))
        if row.get("detail"):
            lines.append((f"       {row['detail']}", "red" if str(row["detail"]).startswith("!") else "gray"))
    if not rows:
        lines.append(("  (no servers yet: put each server's folder in SERVERS/ and it appears here within seconds)",
                      "yellow"))
    return lines


def render(view: dict[str, Any], width: int, height: int) -> list[tuple[str, str | None]]:
    """The dashboard as lines with colours. Pure, so it can be tested without a terminal."""
    lines: list[tuple[str, str | None]] = []
    rule = " " + "─" * min(110, width - 2)
    mode = "supervising" if view.get("supervising") else "WINDOW ONLY - another window is supervising"
    lines.append((f" RECLAIMER SUPERVISOR {view.get('version', '')}   ({mode})", "white"))
    lines.append((f" {view.get('when', '')}      {view.get('home', '')}", "gray"))
    if view.get("header"):
        lines.append((" " + "  |  ".join(view["header"]), "gray"))
    lines.append((rule, "gray"))
    for problem in view.get("problems", []):
        lines.append((f"  {problem}", "red"))
    lines.extend(table_lines(view.get("rows", []), width))
    lines.append((rule, "gray"))
    startup = view.get("startup")
    startup_text = "n/a" if startup is None else ("ON" if startup else "OFF")
    lines.append(("  Servers  [S] Start  [X] Stop  [R] Restart  [K] Cancel countdown  [T] Autostart  [P] Players  "
                  "[M] Message", "white"))
    lines.append(("  Live     [A] Console  [D] Settings  [B] Bans & admins  [L] Watch a log", "white"))
    lines.append(("  Set up   [N] New server  [G] Game content  [I] Steam/Workshop  [C] Check  [E] Edit a file  "
                  "[O] Open folder", "white"))
    lines.append((f"  Program  [U] Updates  [W] Start at boot: {startup_text}  [H] Help  [Q] Quit", "white"))
    lines.append((rule, "gray"))
    lines.append((f"  Recent events  ·  {view.get('log_name', 'logs/supervisor.log')} has them all", "gray"))
    room = max(4, height - len(lines) - 2)
    for level, text in view.get("events", [])[-room:]:
        lines.append((f"  {text}", event_colour(level, text)))
    return lines


# Controllers ------------------------------------------------------------------------------------

class LocalController:
    """The supervising window: acts directly on the fleet."""

    supervising = True

    def __init__(self, supervisor: Supervisor, recent: RecentLog) -> None:
        self.supervisor = supervisor
        self.recent = recent
        self.home = supervisor.settings.home
        self.launcher = os.environ.get("RECLAIMER_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")

    def refresh_settings(self) -> Any:
        return self.supervisor.settings  # kept current by the supervisor's own discovery

    def discover(self) -> None:
        self.supervisor.discover()

    def header(self) -> list[str]:
        settings = self.supervisor.settings
        parts = []
        for name, runner in self.supervisor.runners.items():
            part = settings.parts[name]
            label = name
            if not runner.enabled:
                label += " (off: failing)"
            elif part.dry_run or settings.dry_run:
                label += " (dry run)"
            elif name == "watchdog":
                label += f" ({runner.part.config['mode']})"
            parts.append(label)
        where = settings.servers_folder.name if settings.servers_folder else str(settings.single_folder)
        bits = [f"backend {settings.backend}", f"servers in {where}/", "running: " + ", ".join(parts)]
        status = self.supervisor.program_updates.status()
        versions = sorted(set(status["running"].values()))
        if versions:
            bits.append("server program " + "/".join(versions) + (f" ({status['pending']} out: U)" if status["pending"]
                                                                   else ""))
        elif status["pending"]:
            bits.append(f"server program {status['pending']} is out (U)")
        consoles = self.supervisor.relay.connected()
        if consoles:
            bits.append("consoles: " + ", ".join(consoles))
        return bits

    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return self.supervisor.bus.subscribe("rcon.*", lambda event: listener(event) if event.server == server else None)

    def rows(self) -> list[Row]:
        return self.supervisor.fleet.rows()

    def events(self, count: int) -> list[tuple[int, str]]:
        return self.recent.recent(count)

    def problems(self) -> list[str]:
        return list(self.supervisor.problems)

    def finished(self) -> str | None:
        return "stopped" if self.supervisor.stopping else None

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        return await self.supervisor.fleet.verb(verb, server, **options)

    def request_quit(self) -> None:
        self.supervisor.stop()


class RemoteController:
    """A second window: the dashboard from records/live.json, requests through the mailbox."""

    supervising = False

    def __init__(self, settings: Any, is_running: Callable[[], int | None]) -> None:
        self.settings = settings
        self.home = settings.home
        self.records = settings.records
        self.launcher = os.environ.get("RECLAIMER_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")
        self.is_running = is_running
        self._live: dict[str, Any] = {}
        self._live_read = 0.0

    def refresh_settings(self) -> Any:
        from .config import ConfigError, load

        try:
            self.settings = load(self.settings.path)
        except ConfigError as error:
            log.warning("settings not re-read: %s", error)
        return self.settings

    def discover(self) -> None:
        pass  # the supervising window finds new folders by itself within 10 s

    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return lambda: None  # a second window has no connection of its own; the supervising window shows events

    def _read_live(self) -> dict[str, Any]:
        if time.monotonic() - self._live_read < 0.9:
            return self._live
        self._live_read = time.monotonic()
        try:
            self._live = json.loads((self.records / "live.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._live = {}
        return self._live

    def header(self) -> list[str]:
        live = self._read_live()
        bits = [f"backend {live.get('backend', self.settings.backend)}"]
        pid = self.is_running()
        if pid:
            bits.append(f"supervising window: process {pid}")
        return bits

    def rows(self) -> list[Row]:
        live = self._read_live()
        if time.time() - float(live.get("updated") or 0) > LIVE_STALE:
            return []
        return list(live.get("servers") or [])

    def problems(self) -> list[str]:
        live = self._read_live()
        age = time.time() - float(live.get("updated") or 0)
        if age > LIVE_STALE:
            return [f"the supervising window hasn't updated for {age:.0f} s"]
        return []

    def events(self, count: int) -> list[tuple[int, str]]:
        path = self.settings.log_file
        if not path or not path.exists():
            return []
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(max(0, size - 16 * 1024))
                lines = file.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for line in [l for l in lines if l.strip()][-count:]:
            level = logging.ERROR if " ERROR " in line or " CRITICAL " in line else \
                logging.WARNING if " WARNING " in line else logging.INFO
            match = LOG_LINE.match(line)
            if match:  # the file's line shown as the supervising window shows its own: time, level, message
                line = f"{match.group('time')} {match.group('level'):<7} {match.group('message')}"
            out.append((level, line))
        return out

    def finished(self) -> str | None:
        return None if self.is_running() else "takeover"

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        wait = 20.0 if verb in ("rcon", "say") else 10.0
        reply = await asyncio.to_thread(mailbox.send, self.records, verb, server, wait, **options)
        return [(server, Result(reply.ok, reply.message, reply.text))]

    def request_quit(self) -> None:
        pass


# The menu ----------------------------------------------------------------------------------------

Block = list[Parts]  # a few lines of a screen, drawn again when log lines interrupt the question beneath them
WIDE = 110  # rules and columns never go wider than this, however wide the window
Option = tuple[str, str] | tuple[str, str, str]  # (key, label) or (key, label, note)


class Menu(SetupScreens, ManageScreens):
    """Every screen is written the same way: a title, then sections headed by where you are (a path like
    `LG Test Server › Game content › Playlists`), numbered or lettered lists in aligned columns, a menu
    block of [key] options, and one question. Log lines and server events that arrive while a question
    waits are held and shown once they stop, with the menu block and the question drawn again."""

    def __init__(self, controller: Controller, terminal: Terminal, keyboard: Keyboard,
                 recent: RecentLog | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.controller = controller
        self.terminal = terminal
        self.keyboard = keyboard
        self.recent = recent
        self.clock = clock
        self.by_name = None
        self.held = Interruptions(terminal)
        self.actions: dict[str, Callable[[], Awaitable[str | None]]] = {
            "S": self.screen_start, "X": self.screen_stop, "R": self.screen_restart, "K": self.screen_cancel,
            "T": self.screen_autostart, "M": self.screen_message, "A": self.screen_admin, "P": self.screen_players,
            "L": self.screen_watch, "U": self.screen_update, "E": self.screen_edit, "O": self.screen_open,
            "N": self.screen_new, "G": self.screen_content, "C": self.screen_check, "I": self.screen_steam,
            "D": self.screen_settings, "B": self.screen_bans,
            "W": self.screen_startup, "H": self.screen_help, "?": self.screen_help, "Q": self.screen_quit,
        }

    # The loop ---------------------------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        return {
            "version": __version__, "supervising": self.controller.supervising,
            "when": time.strftime("%a %d %b %Y  %H:%M:%S", time.localtime(self.clock())),
            "home": str(self.controller.home), "header": self.controller.header(),
            "problems": self.controller.problems(), "rows": self.controller.rows(),
            "startup": desktop.startup_enabled(), "events": self.controller.events(40),
            "log_name": "logs/supervisor.log",
        }

    def draw(self) -> None:
        width, height = self.terminal.size()
        self.terminal.draw(render(self.view(), width, height))

    async def run(self) -> str:
        """Until the window is quit: 'quit', 'stopped' (the supervisor stopped) or 'takeover'."""
        self.held.attach(asyncio.get_running_loop())
        self.terminal.reset()
        self.terminal.hide_cursor()
        try:
            while True:
                finished = self.controller.finished()
                if finished:
                    return finished
                self.draw()
                key = await self.keyboard.read(1.0)
                if key is None:
                    await asyncio.sleep(0)  # a keyboard that answers at once must still let the loop breathe
                    continue
                action = self.actions.get(key.upper())
                if action is None:
                    continue
                self.terminal.clear()
                self.terminal.show_cursor()
                self._echo(True)
                opened = time.monotonic()
                self._open_transcript(key.upper())
                try:
                    outcome = await action()
                except Exception as error:  # noqa: BLE001 - a screen must never take the supervisor down
                    log.exception("the %s screen failed", key.upper())
                    self.terminal.print(f"  That didn't work: {type(error).__name__}: {error}", "red")
                    await self.wait_any_key(10)
                    outcome = None
                finally:
                    self._echo(False)
                    self.held.flush()
                    self._close_transcript(key.upper(), time.monotonic() - opened)
                    self.terminal.hide_cursor()
                    self.terminal.reset()
                if outcome == "quit":
                    return "quit"
        finally:
            self.held.detach()
            self.terminal.show_cursor()
            self.terminal.print()

    def _echo(self, on: bool) -> None:
        """While a screen is open, the dashboard's log lines are held and shown between questions (except the
        window's own bookkeeping, `window: G opened`, which stays in the log)."""
        if self.recent is None:
            return

        def echo(level: int, text: str) -> None:
            if " window: " not in text:
                self.held.push(f"  {text}", event_colour(level, text))

        self.recent.echo = echo if on else None

    # The window log: a plain copy of every screen, what was asked, what was answered, what was shown -------

    def _open_transcript(self, key: str) -> None:
        path = self.controller.home / "logs" / TRANSCRIPT
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > TRANSCRIPT_MAX:
                os.replace(path, path.with_suffix(".log.old"))
            self.terminal.transcript = path.open("a", encoding="utf-8")
        except OSError:
            self.terminal.transcript = None
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock()))
        self.terminal.note(f"\n===== {stamp}  key {key} =====")
        log.info("window: %s opened", key)

    def _close_transcript(self, key: str, seconds: float) -> None:
        transcript = self.terminal.transcript
        if transcript is None:
            return
        self.terminal.note(f"===== back to the dashboard after {seconds:.0f} s =====")
        self.terminal.transcript = None
        try:
            transcript.close()
        except OSError:
            pass
        log.info("window: %s closed after %.0f s (logs/%s has what was shown)", key, seconds, TRANSCRIPT)

    # Writing a screen -------------------------------------------------------------------------
    #
    #   title("GAME CONTENT", "what a server can play")      the screen's name, once, at the top
    #   heading("LG Test Server", "Playlists")                where you are now, as a path, before each section
    #   note("...")                                           an explanation, in gray
    #   listing([("name", "what it is"), ...])                a numbered list in aligned columns
    #   choose("Which?", [("1", "Browse"), ("2", "Playlists")])   a menu block of [key] options, then one key

    def width(self) -> int:
        return min(WIDE, self.terminal.size()[0] - 1)

    def title(self, text: str, note: str | None = None) -> None:
        self.terminal.print(f" {text}", "white")
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
        cell = max(len(label) for label in labels) + 3
        if columns is None:
            columns = 2 if not any(notes) and len(options) >= 4 and 3 + 2 * cell <= width else 1
        block: Block = [[("", None)]]
        if columns == 1:
            label_width = max(len(label) for label in labels)
            for label, note in zip(labels, notes):
                parts: Parts = [(f"   {label.ljust(label_width) if note else label}", None)]
                if note:
                    parts.append((f"  {note}", "gray"))
                block.append(parts)
        else:
            for start in range(0, len(labels), columns):
                row = "".join(label.ljust(cell) for label in labels[start:start + columns]).rstrip()
                block.append([(f"   {row}", None)])
        if back:
            block.append([(f"   {back}", "gray")])
        self.show(block)
        return await self.prompt_key(f"  {prompt}", [o[0] for o in options], default, panel=block)

    # Questions --------------------------------------------------------------------------------

    def _draw_again(self, panel: Block | None, text: str, buffer: list[str]) -> None:
        """The panel and the question, as they were before held lines pushed them up."""
        for parts in panel or []:
            self.terminal.print_parts(parts, record=False)
        self.terminal.print_parts([(text, "white"), ("".join(buffer), None)], end="", record=False)

    async def prompt_line(self, prompt: str, default: str = "", *, panel: Block | None = None) -> str | None:
        """A line of text; Esc or two idle minutes give None."""
        self.held.flush()
        text = f"{prompt} " if prompt.endswith(">") else f"{prompt}{' [' + default + ']' if default else ''}: "
        buffer: list[str] = []
        self.terminal.print(text, "white", end="")
        self.held.waiting(lambda: self._draw_again(panel, text, buffer), lambda: len(text) + len(buffer))
        try:
            while True:
                key = await self.keyboard.read(PROMPT_TIMEOUT)
                if key is None:
                    self.terminal.print(("" if not buffer else " ") + "(timed out)", "gray")
                    return None
                if key == "ENTER":
                    self.terminal.print()
                    answer = "".join(buffer).strip() or default
                    self.terminal.note("> " + ("********" if answer and "password" in prompt.lower() else answer))
                    return answer
                if key in ("ESC", "CTRL_C"):
                    self.terminal.print(("" if not buffer else " ") + "(cancelled)", "gray")
                    return None
                if key == "BACKSPACE":
                    if buffer:
                        buffer.pop()
                        self.terminal.print("\b \b", end="", record=False)
                    continue
                if len(key) == 1:
                    buffer.append(key)
                    self.terminal.print(key, end="", record=False)
        finally:
            self.held.done()

    async def prompt_key(self, prompt: str, keys: list[str], default: str | None = None, *,
                         panel: Block | None = None) -> str | None:
        self.held.flush()
        text = f"{prompt}: "
        self.terminal.print(text, "white", end="")
        self.held.waiting(lambda: self._draw_again(panel, text, []), lambda: len(text))
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
        text = f"  Press any key to return to the dashboard (or wait {seconds:.0f}s)..."
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

    # Screens ------------------------------------------------------------------------------------

    async def screen_start(self) -> None:
        self.title("START", "a stopped server starts and is looked after from then on")
        picked = await self.select_servers("Start", allowed=lambda r: r["can"]["start"])
        for row in picked:
            self.show_results(await self.controller.verb("start", row["name"]))
        if picked:
            await self.wait_any_key(4)

    async def screen_stop(self) -> None:
        await self._stop_or_restart(restart=False)

    async def screen_restart(self) -> None:
        await self._stop_or_restart(restart=True)

    async def _stop_or_restart(self, *, restart: bool) -> None:
        verb = "Restart" if restart else "Stop"
        self.title(verb.upper(), "with players on, you choose a countdown they can read, or right now")
        picked = await self.select_servers(verb, allowed=lambda r: r["can"]["restart" if restart else "stop"])
        if not picked:
            return
        for row in picked:
            options: dict[str, Any] = {"immediate": False}
            if row["phase"] in ("running", "external"):
                now = await self.warn_or_now(row, verb.lower())
                if now is None:
                    continue
                options["immediate"] = now
                if restart and not now and str(row.get("players", "")).split("/")[0] not in ("0", ""):
                    reason = await self.prompt_line('  Reason players read (Enter for "manual restart")', "")
                    if reason is None:
                        continue
                    if reason:
                        options["reason"] = reason
            elif row["phase"] == "countdown":
                options["immediate"] = await self.prompt_yes_no(
                    f"  {row['name']} is already counting down. Skip the rest and {verb.lower()} now?", False)
            self.show_results(await self.controller.verb(verb.lower(), row["name"], **options))
        await self.wait_any_key(5)

    async def screen_cancel(self) -> None:
        self.title("CANCEL A COUNTDOWN", "the server keeps running; players are told")
        picked = await self.select_servers("Cancel the countdown on", allowed=lambda r: r["can"]["cancel"])
        for row in picked:
            self.show_results(await self.controller.verb("cancel", row["name"]))
        if picked:
            await self.wait_any_key(4)

    async def screen_autostart(self) -> None:
        self.title("AUTOSTART", "an autostart server starts whenever the supervisor starts, e.g. after a reboot")
        self.note("It's written to settings.toml ([[server]] autostart). The AUTO column shows it.")
        picked = await self.select_servers("Toggle autostart on", single=True)
        for row in picked:
            self.show_results(await self.controller.verb("autostart", row["name"]))
        if picked:
            await self.wait_any_key(4)

    async def screen_message(self) -> None:
        self.title("MESSAGE PLAYERS", "a [Server] line in the chat of the servers you pick")
        picked = await self.select_servers("Message the players on", allowed=lambda r: r["can"]["talk"])
        if not picked:
            return
        text = await self.prompt_line("  Message")
        if not text:
            return
        for row in picked:
            self.show_results(await self.controller.verb("say", row["name"], text=text))
        await self.wait_any_key(5)

    async def screen_players(self) -> None:
        self.title("PLAYERS ONLINE")
        any_up = False
        for row in self.controller.rows():
            if not row["can"]["talk"]:
                continue
            any_up = True
            count = str(row.get("players", "")).split("/")[0] or "?"
            self.heading(row["name"], note=f"{count} online" + (f" · {row['game']}" if row.get("game") else ""))
            names = [n.strip() for n in str(row.get("names", "")).split(",") if n.strip()]
            self.listing([(name,) for name in names], colours=["cyan"] * len(names))
            if not names:
                self.note("(nobody right now)")
        if not any_up:
            self.note("No server is running.", "yellow")
        await self.wait_any_key(20)

    async def screen_watch(self) -> None:
        self.title("WATCH A LOG", "opens a separate window that follows a log; closing it does nothing to the servers")
        self.terminal.print()
        rows = self.controller.rows()
        table = [(row["name"], "its console output") for row in rows] + [("the supervisor's own log", "logs/supervisor.log")]
        panel = self.listing(table, keys=[str(r["index"]) for r in rows] + ["E"])
        answer = await self.prompt_line("  Which? (number or E; Esc cancels)", panel=panel)
        if not answer:
            return
        if answer.strip().upper() == "E":
            message = desktop.open_log_window(self.controller.home, ["watch", "supervisor"], "supervisor log")
        else:
            row = next((r for r in rows if str(r["index"]) == answer.strip() or r["name"].lower() == answer.lower()),
                       None)
            if row is None:
                return
            message = desktop.open_log_window(self.controller.home, ["watch", f'"{row["name"]}"'], f"{row['name']} console")
        self.note(message)
        await self.wait_any_key(5)

    async def screen_open(self) -> None:
        self.title("OPEN THE FOLDER")
        self.note(desktop.open_path(self.controller.home))
        await self.wait_any_key(3)

    async def screen_startup(self) -> None:
        self.title("START AT BOOT")
        enabled = desktop.startup_enabled()
        self.note(desktop.startup_description(), None)
        self.terminal.print()
        if enabled is None:
            self.note("Not available here.", "yellow")
        elif enabled:
            if await self.prompt_yes_no("  It starts at boot now. Turn that off?", False):
                self.note(desktop.disable_startup())
        elif await self.prompt_yes_no("  Turn it on?", True):
            self.note(desktop.enable_startup(self.controller.home))
        await self.wait_any_key(6)

    async def screen_help(self) -> None:
        self.title("HELP", "the dashboard shows every server folder in SERVERS/; one key opens a screen")
        launcher = self.controller.launcher
        groups: list[tuple[str, list[tuple[str, str]]]] = [
            ("Servers", [("S X R", "start, stop and restart; with players on you choose a countdown or right now"),
                         ("K", "cancel a countdown"), ("T", "autostart on or off (starts with the supervisor)"),
                         ("M", "a [Server] line in a server's chat"), ("P", "who's on")]),
            ("Live", [("A", "the console: commands over the supervisor's connection, events as they happen, "
                            "your name on what you do"),
                      ("D", "a server's settings, changed while it runs"),
                      ("B", "bans, admins and the VPN block"), ("L", "a window following a server's console or the log")]),
            ("Set up", [("N", "a new server: a folder in SERVERS/, from a package in PACKAGES/, checked and started"),
                        ("G", "what a server plays: maps, variants, game types, playlists built from numbered lists, "
                              "Workshop items and mods"),
                        ("I", "SteamCMD for Workshop items: finds or installs it, signs in, tells every server"),
                        ("C", "a server's own `dedicated check`"), ("E", "edit the supervisor's files, or a server's"),
                        ("O", "open the supervisor's folder")]),
            ("Program", [("U", "updates: the supervisor, or the server program (every copy, restarts after the game)"),
                         ("W", "start at boot"),
                         ("Q", "close this window; the servers KEEP RUNNING, and opening it again picks them up")]),
        ]
        for name, keys in groups:
            self.heading(name)
            self.listing([(key, what) for key, what in keys], index=False, wrap=True)
        self.heading("From another terminal")
        for line in (f"{launcher} status | stop [server] [--now] | close | restart <server> [--now] | say <server|all> <text>",
                     f"{launcher} rcon [-s server] <command> | watch <server|supervisor> | check | update",
                     f"{launcher} new <name> [--package ...] | package [list|show|save] | content | playlist | workshop | "
                     "dedicated ...",
                     "A second window opened on the same folder shows the same dashboard and hands its keys to the first."):
            self.note(line, None)
        await self.wait_any_key(60)

    async def screen_quit(self) -> str | None:
        self.title("QUIT", "closes this window only")
        rows = self.controller.rows()
        busy = [f"{r['name']} ({r['status']})" for r in rows
                if r["phase"] in ("countdown", "stopping", "restarting", "launch", "booting")]
        running = [r["name"] for r in rows if r["phase"] in ("running", "countdown", "external", "booting", "stopping")]
        if self.controller.supervising and busy:
            self.note(f"In the middle of something: {', '.join(busy)}", "red")
            self.note("Closing now abandons it partway: a countdown just stops (the server keeps running), a stop "
                      "already sent finishes by itself.", "red")
            self.terminal.print()
        if running:
            self.note(f"These keep running: {', '.join(running)}", "yellow")
            if self.controller.supervising:
                self.note("But while no window is open nothing watches them: no crash restarts, no daily restart, "
                          "no automod, no commands.", "yellow")
                self.note(f"Opening the window again picks them straight back up. To stop them too: "
                          f"{self.controller.launcher} stop", "yellow")
        if await self.prompt_yes_no("  Close the window?", False):
            self.controller.request_quit()
            return "quit"
        return None
