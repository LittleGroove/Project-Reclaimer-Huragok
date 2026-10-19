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

from . import __version__, desktop, lore, mailbox
from .lore import HURAGOK, SUPERINTENDENT, VERGIL
from .manager import Result
from .menu_files import FileScreens
from .menu_games import GameScreens
from .menu_manage import ManageScreens
from .menu_setup import SetupScreens
from .tui import (Interruptions, Keyboard, Parts, RecentLog, Terminal, cells, cut, event_colour, format_listing,
                  heading_parts, pad)

if TYPE_CHECKING:
    from .core import Supervisor

log = logging.getLogger(__name__)

PROMPT_TIMEOUT = 120.0
LIVE_STALE = 15.0
# A logs/huragok.log line: date, time (ms dropped), level, the module that spoke (shown as its part's tag), the message.
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d (?P<time>\d\d:\d\d:\d\d)(?:,\d+)? (?P<level>\w+)\s+(?:(?P<name>[\w.]+): )?"
                      r"(?P<message>.*)$")
TRANSCRIPT = "window.log"  # in logs/: every screen as it was shown, with what was asked and answered
TRANSCRIPT_MAX = 5_000_000
Row = dict[str, Any]


def _relative(path: Path | None, home: Path) -> str:
    """logs/huragok.log, as the host would find it from the folder."""
    if path is None:
        return "no log file"
    try:
        return path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


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
    def card(self, server: str) -> list[tuple[str, list[tuple[str, str, str | None]]]]: ...  # V: its status card


# Rendering -----------------------------------------------------------------------------------

def table_lines(rows: list[Row], width: int) -> list[tuple[str, str | None]]:
    """The servers as the dashboard's table: a header, one coloured line per server, its detail beneath."""
    lines: list[tuple[str, str | None]] = []
    name_width = min(28, max(8, *(cells(str(r["name"])) for r in rows))) if rows else 8
    status_width = 36
    players_width = 18

    def line(index: Any, name: str, port: Any, auto: str, status: str, players: str, game: str) -> str:
        # Lined up by the columns each takes on the screen: a CJK name or an emoji takes two per character.
        return (f"  {pad(str(index), 2)} {pad(cut(name, name_width), name_width)} {pad(str(port), 6)} {pad(auto, 4)} "
                f"{pad(cut(status, status_width, ''), status_width)} {pad(players, players_width)} {game}")

    lines.append((line("#", "SERVER", "PORT", "AUTO", "STATUS", "PLAYERS", "GAME"), "white"))
    for row in rows:
        players = str(row.get("players", ""))
        if row.get("names"):
            players = f"{players} {row['names']}"
        players = cut(players, players_width - 1, "...")
        lines.append((line(row.get("index", ""), str(row["name"]), row.get("port") or "",
                           "yes" if row.get("autostart") else "no", str(row.get("status", "")), players,
                           str(row.get("game") or (f"plays {row['plays']}" if row.get("plays") else ""))),
                      row.get("color", "gray")))
        if row.get("detail"):
            lines.append((f"       {row['detail']}", "red" if str(row["detail"]).startswith("!") else "gray"))
    if not rows:
        lines.append(("  (no servers yet: put each server's folder in SERVERS/ and it appears here within seconds)",
                      "yellow"))
    return lines


def pack(bits: list[str], width: int, joint: str = "  |  ") -> list[str]:
    """Bits joined on as few lines as fit the width, never split."""
    lines: list[str] = []
    for bit in bits:
        if lines and len(lines[-1]) + len(joint) + len(bit) <= width:
            lines[-1] += joint + bit
        else:
            lines.append(bit)
    return lines


def key_rows(startup: bool | None) -> list[tuple[str, str, str]]:
    """The dashboard's keys, one row per part that answers them: (who, keys, colour)."""
    startup_text = "n/a" if startup is None else ("ON" if startup else "OFF")
    return [
        ("SUPERINTENDENT", "[S] Start  [X] Stop  [R] Restart  [K] Cancel  [T] Autostart  [L] Log  [V] View a server",
         SUPERINTENDENT.colour),
        ("VERGIL", "[P] Players  [M] Message  [A] Console  [B] Bans & admins", VERGIL.colour),
        ("HURAGOK", "[N] New server  [G] Game content  [I] SteamCMD  [D] Settings  [C] Check", HURAGOK.colour),
        ("", f"[O] Open  [U] Updates  [W] Start at boot: {startup_text}  [H] Help  [Q] Quit", "white"),
    ]


def render(view: dict[str, Any], width: int, height: int) -> list[tuple[str, str | None]]:
    """The dashboard as lines with colours. Pure, so it can be tested without a terminal."""
    lines: list[tuple[str, str | None]] = []
    rule = " " + "─" * min(WIDE, width - 2)
    mode = "supervising" if view.get("supervising") else "WINDOW ONLY - another window is supervising"
    lines.append((f" HURAGOK {view.get('version', '')}   ({mode})   the Engineer for Project Reclaimer servers",
                  HURAGOK.colour))
    mood = lore.mood(view.get("rows", []), answering=view.get("answering", True))
    lines.append((f" {mood.line()}", mood.colour))
    lines.append((f" {view.get('when', '')}      {view.get('home', '')}", "gray"))
    for text in pack(view.get("header") or [], min(WIDE, width - 2)):
        lines.append((f" {text}", "gray"))
    lines.append((rule, "gray"))
    for problem in view.get("problems", []):
        lines.append((f"  {problem}", "red"))
    lines.extend(table_lines(view.get("rows", []), width))
    lines.append((rule, "gray"))
    for who, keys, colour in key_rows(view.get("startup")):
        lines.append((f"  {who:<14}  {keys}", colour))
    lines.append((rule, "gray"))
    tags = "  ".join(f"{p.tag} {p.name}" for p in lore.PERSONAS)
    lines.append((f"  RECENT EVENTS  ·  {tags}  ·  {view.get('log_name', 'logs/huragok.log')} has them all", "gray"))
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
        self.launcher = os.environ.get("HURAGOK_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")

    def refresh_settings(self) -> Any:
        return self.supervisor.settings  # kept current by the supervisor's own discovery

    def discover(self) -> None:
        self.supervisor.discover()

    @property
    def log_name(self) -> str:
        return _relative(self.supervisor.settings.log_file, self.home)

    def header(self) -> list[str]:
        """What each part is running: `SUPT backend process · …  |  VRGL commands · automod (dry run)  |  HRGK …`."""
        settings = self.supervisor.settings
        where = settings.servers_folder.name if settings.servers_folder else str(settings.single_folder)
        bits: dict[lore.Persona, list[str]] = {SUPERINTENDENT: [f"backend {settings.backend}", f"servers in {where}/"],
                                               VERGIL: [], HURAGOK: []}
        for name, runner in self.supervisor.runners.items():
            part = settings.parts[name]
            label = name
            if not runner.enabled:
                label += " (off: failing)"
            elif part.dry_run or settings.dry_run:
                label += " (dry run)"
            elif name == "watchdog":
                label += f" ({runner.part.config['mode']})"
            bits[lore.speaker(f"part.{name}")].append(label)
        consoles = self.supervisor.relay.connected()
        if consoles:
            bits[VERGIL].append("consoles: " + ", ".join(consoles))
        if update := self.supervisor.updates.status_text():  # stays here: the events scroll it away
            bits[HURAGOK].append(update)
        status = self.supervisor.program_updates.status()
        versions = sorted(set(status["running"].values()))
        themselves = len(status.get("self_updating") or [])
        updating = (f" · {themselves} of {len(settings.servers)} update themselves" if themselves else "")
        if versions:
            bits[HURAGOK].append("server program " + "/".join(versions)
                                 + (f" ({status['pending']} out: U)" if status["pending"] else "") + updating)
        elif status["pending"]:
            bits[HURAGOK].append(f"server program {status['pending']} is out (U){updating}")
        return [f"{persona.tag} " + " · ".join(said) for persona, said in bits.items() if said]

    def watch(self, server: str, listener: Callable[[Any], None]) -> Callable[[], None]:
        return self.supervisor.bus.subscribe("rcon.*", lambda event: listener(event) if event.server == server else None)

    def rows(self) -> list[Row]:
        return self.supervisor.fleet.rows()

    def card(self, server: str) -> list[tuple[str, list[tuple[str, str, str | None]]]]:
        """V: the server's status card, with what only the supervising window knows."""
        from . import statuscard

        sup = self.supervisor
        config = sup.settings.server(server)
        manager = sup.fleet.managers.get(server)
        row = manager.row(manager_index(sup.fleet, server)) if manager else {}
        pending = sup.program_updates.pending
        extra = {"release": pending.version if pending else None,
                 "program_note": sup.program_updates.note(server),
                 "workshop": sup.workshop_updates.item_lines(config) if config.workshop_items else None,
                 "workshop_disk": statuscard.workshop_disk(config) if config.workshop_items else None,
                 "memory_mb": memory_mb(row.get("pid")), "startup": desktop.startup_enabled(),
                 "problems": list(sup.backend.problems(config))}
        return statuscard.build(sup.settings, config, row, extra)

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
        self.launcher = os.environ.get("HURAGOK_LAUNCHER", "start.cmd" if os.name == "nt" else "./start.sh")
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
        """records/live.json as the supervising window last wrote it. A read that fails (the file missing for a
        moment, or Windows refusing it while the writer replaces it) or a file that isn't what it should be keeps
        the last good copy: one bad read must not flash "no servers" across the dashboard."""
        if time.monotonic() - self._live_read < 0.9:
            return self._live
        self._live_read = time.monotonic()
        try:
            data = json.loads((self.records / "live.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._live
        if isinstance(data, dict) and _stamp(data.get("updated")) is not None:
            servers = data.get("servers")
            data["servers"] = [r for r in servers if isinstance(r, dict) and isinstance(r.get("name"), str)] \
                if isinstance(servers, list) else []
            self._live = data
        return self._live

    def _age(self) -> float | None:
        """Seconds since the supervising window last wrote live.json; None when no good copy was read yet."""
        updated = _stamp(self._read_live().get("updated"))
        return None if updated is None else time.time() - updated

    @property
    def log_name(self) -> str:
        return _relative(self.settings.log_file, self.home)

    def answering(self) -> bool:
        age = self._age()
        return age is not None and age <= LIVE_STALE

    def header(self) -> list[str]:
        live = self._read_live()
        bits = [f"{SUPERINTENDENT.tag} backend {live.get('backend', self.settings.backend)}"]
        pid = self.is_running()
        if pid:
            update = str(live.get("huragok_update") or "")
            bits.append(f"{HURAGOK.tag} supervising window: process {pid}" + (f" · {update}" if update else ""))
        return bits

    def rows(self) -> list[Row]:
        if not self.answering():
            return []
        return list(self._read_live().get("servers") or [])

    def card(self, server: str) -> list[tuple[str, list[tuple[str, str, str | None]]]]:
        """V from a second window: the card from the files and the supervising window's live row."""
        from . import statuscard

        config = self.settings.server(server)
        row = next((r for r in self.rows() if r.get("name") == server), {})
        return statuscard.build(self.settings, config, row, {
            "workshop_disk": statuscard.workshop_disk(config) if config.workshop_items else None,
            "startup": desktop.startup_enabled()})

    def problems(self) -> list[str]:
        age = self._age()
        if age is None:
            return ["no word from the supervising window yet (it writes records/live.json every second or two)"]
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
            if match:  # the file's line shown as the supervising window shows its own: time, tag, level, message
                tag = lore.speaker(match.group("name") or "").tag
                line = f"{match.group('time')} {tag} {match.group('level'):<7} {match.group('message')}"
            out.append((level, line))
        return out

    def finished(self) -> str | None:
        return None if self.is_running() else "takeover"

    async def verb(self, verb: str, server: str = "all", **options: Any) -> list[tuple[str, Result]]:
        """A request through the mailbox, answered by the supervising window. The wait is longer than the work:
        a download or a Steam check can take minutes."""
        wait = remote_wait(verb)
        request_id = await asyncio.to_thread(mailbox.post, self.records, verb, server, **options)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            reply = await asyncio.to_thread(mailbox.take_reply, self.records, request_id)
            if reply is not None:
                return [(server, Result(reply.ok, reply.message, reply.text))]
            await asyncio.sleep(0.25)
        request = mailbox.folder(self.records) / f"{request_id}.json"
        if request.exists():  # never picked up: taken back, so it doesn't happen later by surprise
            request.unlink(missing_ok=True)
            return [(server, Result(False, f"the supervising window didn't take the request in {wait:.0f} s "
                                           "(is it still running?); nothing was done"))]
        return [(server, Result(False, f"no answer yet after {wait:.0f} s: it may still be doing it (the "
                                       "supervising window's events will say)"))]

    def request_quit(self) -> None:
        pass


# How long a second window waits for the supervising window's answer, by request: longer than the work takes.
REMOTE_WAITS = {"update": 600.0, "update_program": 900.0, "update_workshop": 600.0, "check_update": 180.0,
                "check_program_update": 180.0, "check_workshop": 300.0, "rcon": 30.0, "say": 30.0,
                "game_files": 60.0, "game_files_retry": 30.0}


def remote_wait(verb: str) -> float:
    return REMOTE_WAITS.get(verb, 15.0)


def _stamp(value: Any) -> float | None:
    """live.json's `updated` as a number of seconds; None when it isn't one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def manager_index(fleet: Any, name: str) -> int:
    return list(fleet.managers).index(name) + 1 if name in fleet.managers else 0


def memory_mb(pid: int | None) -> float | None:
    """What a server's process and everything it started use, in MB (None when it isn't known)."""
    if not pid:
        return None
    try:
        import psutil

        process = psutil.Process(int(pid))
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return total / (1 << 20)
    except Exception:  # noqa: BLE001 - psutil.Error, or no such process: just not shown
        return None


# The menu ----------------------------------------------------------------------------------------

Block = list[Parts]  # a few lines of a screen, drawn again when log lines interrupt the question beneath them
WIDE = 110  # rules and columns never go wider than this, however wide the window
Option = tuple[str, str] | tuple[str, str, str]  # (key, label) or (key, label, note)


class Menu(SetupScreens, ManageScreens, GameScreens, FileScreens):
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
        self.persona = HURAGOK  # whose screen is open: its title says so (lore.KEYS)
        self.held = Interruptions(terminal)
        self._preselect: str | None = None  # a server already chosen (the status card's jumps): not asked again
        self.actions: dict[str, Callable[[], Awaitable[str | None]]] = {
            "S": self.screen_start, "X": self.screen_stop, "R": self.screen_restart, "K": self.screen_cancel,
            "T": self.screen_autostart, "M": self.screen_message, "A": self.screen_admin, "P": self.screen_players,
            "L": self.screen_watch, "U": self.screen_update, "O": self.screen_open,
            "N": self.screen_new, "G": self.screen_content, "C": self.screen_check, "I": self.screen_steam,
            "D": self.screen_settings, "B": self.screen_bans, "V": self.screen_view,
            "W": self.screen_startup, "H": self.screen_help, "?": self.screen_help, "Q": self.screen_quit,
        }

    # The loop ---------------------------------------------------------------------------------

    def view(self) -> dict[str, Any]:
        answering = getattr(self.controller, "answering", None)
        return {
            "version": __version__, "supervising": self.controller.supervising,
            "when": time.strftime("%a %d %b %Y  %H:%M:%S", time.localtime(self.clock())),
            "home": str(self.controller.home), "header": self.controller.header(),
            "problems": self.controller.problems(), "rows": self.controller.rows(),
            "startup": desktop.startup_enabled(), "events": self.controller.events(40),
            "log_name": getattr(self.controller, "log_name", "logs/huragok.log"),
            "answering": answering() if answering else True,
        }

    def draw(self) -> None:
        width, height = self.terminal.size()
        self.terminal.draw(render(self.view(), width, height))

    _draw_logged = 0.0

    def _draw_failed(self) -> None:
        """The dashboard couldn't be drawn: say so (in the log once a minute) and keep taking keys."""
        if time.monotonic() - self._draw_logged >= 60:
            self._draw_logged = time.monotonic()
            log.exception("the dashboard couldn't be drawn")
        try:
            self.terminal.draw([("  The dashboard couldn't be drawn (logs/huragok.log says why). The keys still work: "
                                 "Q closes the window.", "red")])
        except Exception:  # noqa: BLE001
            pass

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
                try:
                    self.draw()
                except Exception:  # noqa: BLE001 - a frame that can't be drawn must not end the window
                    self._draw_failed()
                key = await self.keyboard.read(1.0)
                if key is None:
                    await asyncio.sleep(0)  # a keyboard that answers at once must still let the loop breathe
                    continue
                if key == "CTRL_C":
                    key = "Q"  # asks first, like Q: closing the window ends the servers' care
                action = self.actions.get(key.upper())
                if action is None:
                    continue
                self.terminal.clear()
                self.terminal.show_cursor()
                self.persona = lore.KEYS.get(key.upper(), HURAGOK)
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
            self.terminal.flush(2.0)  # on the screen before whatever prints next (the command line's own lines)

    async def slow(self, work: Callable[..., Any], *args: Any, note: str | None = None, colour: str = "cyan") -> Any:
        """Thread work that takes a while (the game files' copy, SteamCMD's install, ODST's files, the content
        listing): every server is looked after meanwhile. Keys pressed while it runs are dropped once it's done,
        so a stray Esc doesn't cancel the next question, nor a Q then Y close the window."""
        self.note(note or "This takes a while: keys pressed meanwhile are ignored until it's done.", colour)
        try:
            return await asyncio.to_thread(work, *args)
        finally:
            self.keyboard.drain()

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

    # Questions --------------------------------------------------------------------------------

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
        self.title("AUTOSTART", "an autostart server starts whenever Huragok starts, e.g. after a reboot")
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

    async def screen_view(self) -> None:
        self.title("SERVER STATUS", "everything the Superintendent keeps an eye on for one server")
        picked = await self.select_servers("Look at", single=True)
        if not picked:
            return
        name = picked[0]["name"]
        card = getattr(self.controller, "card", None)
        while True:
            try:
                sections = card(name) if card else []
            except KeyError:
                self.note(f"{name} isn't in the settings any more.", "yellow")
                await self.wait_any_key(5)
                return
            self.heading(name, "Status")
            rows: list[tuple[str, ...]] = []
            colours: list[str | None] = []
            for title, lines in sections:
                for number, (label, text, colour) in enumerate(lines):
                    rows.append((title if number == 0 else "", label, text))
                    colours.append(colour)
            if rows:
                self.listing(rows, index=False, wrap=True, colours=colours)
            else:
                self.note("(no card for this server here)")
            choice = await self.choose("Which?", [
                ("D", "Its settings"), ("G", "What it plays (game content)"), ("R", "Restart it"),
                ("L", "Watch its console"), ("U", "Updates"), ("C", "Clean up disk space"), ("F", "Look again")],
                back="Esc back to the dashboard")
            if choice is None:
                return
            if choice == "F":
                continue
            if choice == "L":
                self.note(desktop.open_log_window(self.controller.home, ["watch", f'"{name}"'], f"{name} console"))
                continue
            screens = {"D": self.screen_settings, "G": self.screen_content, "R": self.screen_restart,
                       "U": self.screen_update, "C": self.screen_cleanup}
            before, self.persona, self._preselect = self.persona, lore.KEYS.get(choice, HURAGOK), name
            try:
                await screens[choice]()
            finally:
                self.persona, self._preselect = before, None

    async def screen_watch(self) -> None:
        self.title("WATCH A LOG", "opens a separate window that follows a log; closing it does nothing to the servers")
        self.terminal.print()
        rows = self.controller.rows()
        log_name = getattr(self.controller, "log_name", "logs/huragok.log")
        table = [(row["name"], "its console output") for row in rows] + [("Huragok's own log", log_name)]
        panel = self.listing(table, keys=[str(r["index"]) for r in rows] + ["E"])
        answer = await self.prompt_line("  Which? (number or E; Esc cancels)", panel=panel)
        if not answer:
            return
        if answer.strip().upper() == "E":
            message = desktop.open_log_window(self.controller.home, ["watch", "huragok"], "Huragok log")
        else:
            row = next((r for r in rows if str(r["index"]) == answer.strip() or r["name"].lower() == answer.lower()),
                       None)
            if row is None:
                return
            message = desktop.open_log_window(self.controller.home, ["watch", f'"{row["name"]}"'], f"{row['name']} console")
        self.note(message)
        await self.wait_any_key(5)

    async def screen_startup(self) -> None:
        self.title("START AT BOOT")
        enabled = desktop.startup_enabled()
        self.note(desktop.startup_description(), None)
        self.terminal.print()
        if enabled is None:
            self.note("Not available here.", "yellow")
        elif enabled:  # in a thread: on Linux, systemctl answers when it will
            if await self.prompt_yes_no("  It starts at boot now. Turn that off?", False):
                self.note(await asyncio.to_thread(desktop.disable_startup))
        elif await self.prompt_yes_no("  Turn it on?", True):
            self.note(await asyncio.to_thread(desktop.enable_startup, self.controller.home))
        await self.wait_any_key(6)

    async def screen_help(self) -> None:
        for line in lore.BANNER:
            self.terminal.print(f"   {line}", HURAGOK.colour)
        self.terminal.print(f"   the Engineer for Project Reclaimer servers · {lore.MOTTO}", "gray")
        self.terminal.print()
        self.title("HELP", "the dashboard shows every server folder in SERVERS/; one key opens a screen")
        launcher = self.controller.launcher
        self.heading("Who's who", note="Halo 3: ODST's Engineer, and the two minds it carried out of New Mombasa")
        self.listing([(p.tag, p.name, p.story) for p in lore.PERSONAS], index=False, wrap=True,
                     colours=[p.colour for p in lore.PERSONAS])
        groups: list[tuple[lore.Persona | None, list[tuple[str, str]]]] = [
            (SUPERINTENDENT, [("S X R", "start, stop and restart; with players on you choose a countdown or right now"),
                              ("K", "cancel a countdown"), ("T", "autostart on or off (starts with Huragok)"),
                              ("L", "a window following a server's console or Huragok's log"),
                              ("V", "one server's status card: running, joining, what it plays, program and mods, "
                                    "health and restarts, safety, schedule, problems; jumps to its settings")]),
            (VERGIL, [("P", "who's on"), ("M", "a [Server] line in a server's chat"),
                      ("A", "the console: commands over Huragok's connection, events as they happen, "
                            "your name on what you do"),
                      ("B", "bans, admins and the VPN block")]),
            (HURAGOK, [("N", "a new server: a folder in SERVERS/, from a package in PACKAGES/, checked and started"),
                       ("G", "what a server plays: maps, variants, game types, playlists built from numbered lists, "
                             "Workshop items and mods"),
                       ("I", "SteamCMD: finds or installs it, signs in once, tells every server; gets Halo 3's files "
                             "(copied from MCC here, or downloaded), the campaign missions and ODST's"),
                       ("D", "settings, a step at a time, no file formats to know: Huragok's own (how it runs, "
                             "roles and commands, Vergil's word list and banned names), and everything about each "
                             "server in groups, changed while it runs (or after the game when it needs a restart), "
                             "with its playlists, custom games, bans and mods"),
                       ("C", "a server's own `dedicated check`"),
                       ("O", "open: Huragok's folder, or any of its files or a server's in your editor")]),
            (None, [("U", "updates: Huragok, the server program (servers with auto_update update themselves), "
                          "Workshop mods"),
                    ("W", "start at boot"),
                    ("Q", "close this window; the servers KEEP RUNNING, and opening it again picks them up")]),
        ]
        for persona, keys in groups:
            self.heading(persona.name if persona else "Program", note=persona.does if persona else None)
            self.listing([(key, what) for key, what in keys], index=False, wrap=True,
                         colours=[persona.colour if persona else None] * len(keys))
        self.heading("Colours", note="as the VISR in an ODST's helmet marks friend and foe")
        self.listing(list(lore.VISR), index=False, wrap=True, colours=[colour for colour, _ in lore.VISR])
        self.heading("From another terminal")
        for line in (f"{launcher} status | stop [server] [--now] | close | restart <server> [--now] | say <server|all> <text>",
                     f"{launcher} rcon [-s server] <command> | watch <server|huragok> | check | update",
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
                self.note("But while no window is open nothing watches them: the Superintendent is off (no crash "
                          "restarts, no daily restart) and Vergil isn't listening (no automod, no commands).", "yellow")
                self.note(f"Opening the window again picks them straight back up. To stop them too: "
                          f"{self.controller.launcher} stop", "yellow")
        if await self.prompt_yes_no("  Close the window?", False):
            self.controller.request_quit()
            return "quit"
        return None
