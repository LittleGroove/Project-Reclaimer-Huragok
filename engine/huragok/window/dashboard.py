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
import logging
import os
import time
from typing import Any, Awaitable, Callable

from .. import __version__, lore
from ..core import desktop
from ..core.logs import RecentLog
from ..lore import HURAGOK, SUPERINTENDENT, VERGIL
from .controllers import Controller, Row
from .screens.base import WIDE
from .screens.discordsetup import DiscordScreens
from .screens.files import FileScreens
from .screens.games import GameScreens
from .screens.manage import ManageScreens
from .screens.setup import SetupScreens
from .tui import Interruptions, Keyboard, Terminal, cells, cut, event_colour, pad


log = logging.getLogger(__name__)

TRANSCRIPT = "window.log"  # in logs/: every screen as it was shown, with what was asked and answered
TRANSCRIPT_MAX = 5_000_000


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
        ("HURAGOK", "[N] New server  [G] What it plays  [I] SteamCMD  [D] Settings  [J] Discord  [C] Check", HURAGOK.colour),
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


# The menu ----------------------------------------------------------------------------------------


class Menu(SetupScreens, ManageScreens, GameScreens, FileScreens, DiscordScreens):
    """Every screen is written the same way: a title, then sections headed by where you are (a path like
    `LG Test Server › What it plays › Playlists`), numbered or lettered lists in aligned columns, a menu
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
            "D": self.screen_settings, "B": self.screen_bans, "V": self.screen_view, "J": self.screen_discord,
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

    # Questions --------------------------------------------------------------------------------

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
                # Off the event loop: it walks each Workshop item's folders and reads the server's files, slow on a
                # big or busy disk, and the servers are looked after meanwhile. What it reads of the live picture is
                # only read.
                sections = await asyncio.to_thread(card, name) if card else []
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
                ("D", "Its settings"), ("G", "What it plays"), ("R", "Restart it"),
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
                       ("J", "the Discord bot, a step at a time: webhook only or your own bot, Discord roles onto "
                             "Huragok's ranks, channels, each feature's state and its switch, a check, a test post"),
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
