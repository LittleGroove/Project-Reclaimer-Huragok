"""The window's screens for managing a running server: A the console (with the server's events as
they happen, and your name on what you do), D settings: Huragok's own files (files.py) above every
server, and each server in groups (the settings of its dedicated.toml, Huragok's own for it, and what
it plays, shown only: that's set by hand now; its bans and interval messages (playlists, custom games and mods are G's); renaming it,
a content folder of its own), B bans, admins and the VPN block, U updates (Huragok's, the server
program's, Workshop mods).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from ...core import desktop
from ...core.events import Event
from ...core.tags import NO_NAME, shown_name, tag
from ...core.text import is_player_id
from ...program.settings import (
    APPLIES, FOLDER_SETTINGS, GROUPS, SERVER_SETTINGS, can_reset, catalogue, check_path, check_value, display, offered,
    parse, reset, summary, write,
)
from ...players.targets import QUOTES, is_team, is_time, plain
from ...rcon import masked as mask_console_line
from ..controllers import Row
from ..whoswho import Known, console_line, find
from .base import Screens
from .files import FILES

log = logging.getLogger(__name__)

ID_LISTS = ("admins", "vote_kick_immune")  # dedicated.toml's settings that list player IDs: shown with who each is


def _number(value: Any) -> int:
    """A player's #number as an event gives it; 0 for none."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdecimal() else 0


def _player(event: Event, known: Known | None) -> str:
    """The player an event is about, as their tag: the #number they had on the event's server, when it gives one (the
    console's own, so typed back there), and the server they were on. An address ban names no player: the address; an
    unban by what was typed: that."""
    data = event.data
    player_id = str(data.get("id") or "").lower()
    given = str(data.get("name") or "")
    number = _number(data.get("number"))
    if not is_player_id(player_id) and not number:
        return shown_name(given or data.get("ip") or data.get("target") or "") or "someone"
    server = str(event.server or "")
    if not number and is_player_id(player_id):
        live = known.on(player_id) if known is not None else None
        server = live.server if live is not None else ""
        number = live.number if live is not None and live.server == event.server else 0
    if not given and is_player_id(player_id) and event.name != "rcon.join":  # 0.10's join names nobody yet
        given = known.name(player_id) if known is not None else ""
    return tag(given or (NO_NAME if is_player_id(player_id) else ""), player_id, handle=f"#{number}" if number else "",
               server=server)


def _actor(data: dict[str, Any], known: Known | None) -> str:
    """" by Alice 33cc44dd": who acted, by their tag when the event gives their player ID."""
    by, by_id = str(data.get("by") or ""), str(data.get("by_id") or "").lower()
    if is_player_id(by_id):
        return f" by {tag(by or (known.name(by_id) if known is not None else ''), by_id)}"
    return f" by {shown_name(by)}" if shown_name(by) else ""


def describe_event(event: Event, known: Known | None = None) -> str | None:
    """One line for a server event, as a console shows it; None for events not worth a line. Each player as their tag
    (core/tags.py); `known`: who the window knows a player ID to be, for an event that names nobody (0.10 names a
    player after they join, and a leave can name nobody)."""
    name = event.name.removeprefix("rcon.")
    clock = time.strftime("%H:%M:%S", time.localtime(event.time))
    data = event.data
    who = _player(event, known)
    by = _actor(data, known)
    if name == "chat":
        channel = str(data.get("channel") or "all")
        prefix = "" if channel == "all" else f"[{channel}] "
        return f"{clock} {prefix}{who}: {data.get('text', '')}"
    if name == "join":
        return f"{clock} + {who} joined{' (admin)' if data.get('admin') else ''}"
    if name == "state.named":
        return f"{clock} = {who}: their name came in"
    if name == "leave":
        return f"{clock} - {who} left"
    if name == "refused":
        return f"{clock} ! {who} refused: {data.get('reason', '')}"
    if name in ("kick", "ban", "unban", "mute", "unmute", "pardon", "admin_added", "admin_removed"):
        reason = f": {data['reason']}" if data.get("reason") else ""
        return f"{clock} * {name.replace('_', ' ')} {who}{by}{reason}"
    if name in ("role_added", "role_removed"):
        role = f"role {data['role']}" if data.get("role") else "a role"
        given = name == "role_added"
        return f"{clock} * {role} {'given to' if given else 'taken from'} {who}{by}"
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


class ManageScreens(Screens):
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""



    async def _who(self) -> str:
        if self.by_name is None:
            name = await self.prompt_line("  Your name, as players read it when you act (Enter: the Superintendent's)")
            self.by_name = (name or "").strip()
        return self.by_name

    def _known(self) -> Known:
        """Who's who for one screen: the servers' players from the dashboard's rows, the rest from the records."""
        return Known(self.controller.rows, lambda: getattr(self._settings(), "database", None))

    async def _player_line(self, server: str, line: str, known: Known) -> str | None:
        """A console line that names a player, with that player found first, the way the game's commands find one
        (whoswho.find): a #number, a name, a player ID or its first 8 characters. A name several players have, or one that
        only looks like someone's, is a numbered list to pick from. The line as the server is sent it: the player as
        their #number when they're on it, else as their player ID, and who that is said beneath. None: nobody (said
        why), nothing sent. A line that names no player, an address, or a server whose players the window can't see,
        goes as typed."""
        parsed = console_line(line, known.is_name)
        if parsed is None:
            return line
        here = known.here(server)
        words, after = parsed.ref.split(), (parsed.rest.split() or [""])[0]
        expected = is_time(after) or is_team(after) or after.lower() in ("0", "voice", "text", "all")
        if here and parsed.ref[:1] not in QUOTES and after and not expected:
            # "kick nate higgins spam" with Nate and Nate Higgers on: it can't be told which (a typo), so neither.
            longer = [p for p in here if len(p.name.split()) > len(words)
                      and plain(" ".join(p.name.split()[:len(words)])) == plain(parsed.ref)]
            if longer and known.is_name(parsed.ref):
                self.note(f"'{parsed.ref} {parsed.rest.split()[0]}…' could be someone else: put the name in quotes, or "
                          f"use the #number. Longer names that start the same: "
                          + "; ".join(known.tag(p.id, here=server) for p in longer), "yellow")
                return None
        finding = find(known, server, parsed.ref, scope="here" if parsed.sends == "here" else "seen")
        if finding.as_typed:
            return line
        if finding.unknown and parsed.sends == "id":
            self.note(f"Nobody called '{parsed.ref}' is on a server or in Huragok's records: sent as typed, for the "
                      "server to find in its own list.", "gray")
            return line
        found = await self.pick_player(finding, server)
        if found is None:
            return None
        sent = parsed.sent(found, server)
        self.note(f"{mask_console_line(sent)}  →  {found.tag(server)}", "cyan")
        log.info("[%s] the window's console line %s → %s", server, mask_console_line(sent),
                 found.tag(server, whole=True))
        return sent

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
        self.note("A player is their #number, name, or player ID (or its first 8 characters); a name several players "
                  "share is asked about first. Events arrive between commands; an empty line (or Esc) goes back to "
                  "the dashboard.")
        known = self._known()

        def on_event(event: Event) -> None:
            known.learn(event.get("id"), event.get("name"))
            line = describe_event(event, known)
            if line:
                self.held.push(f"    {line}", "gray" if event.name != "rcon.chat" else None)

        unwatch: Callable[[], None] = self.controller.watch(row["name"], on_event)
        try:
            while True:
                self.terminal.print()
                line = await self.prompt_line(f"  {row['name']}>", masked=mask_console_line)
                if not line:
                    return
                sent = await self._player_line(row["name"], line, known)
                if sent is not None:
                    await self._console(row["name"], sent)
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
            self.heading("Servers", note="each one's dedicated.toml, read-only, Huragok's own settings for it, its bans and "
                                         "interval messages")
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
        """One server's page (0.7.0, plans/server-files-audit/mockups-0.7.0.md 3): its dedicated.toml in groups,
        read-only (the host edits it; Huragok shows and warns), each group with the warnings about it, the environment's
        overrides, Huragok's own settings for it (H: editable, they're Huragok's), then opening the file, the quick
        actions, bans, interval messages and renaming it."""
        server = self._server(settings, row)
        if server is None:
            return
        version = await self._program_version(settings, server, row)  # a setting its program hasn't isn't offered
        while True:
            try:
                entries = [e for e in catalogue(server.config_path, server.name, settings) if offered(e[0], version)]
            except (OSError, ValueError) as error:
                self.note(f"Can't read {server.config_path}: {error}", "red")
                await self.wait_any_key(8)
                return
            row = next((r for r in self.controller.rows() if r["name"] == row["name"]), row)  # its phase now
            self.heading(server.name, "Settings",
                         note=f"{server.config_path} · read-only: you edit it, Huragok shows and warns")
            items = row.get("warning_items") or []
            own = [group for group in GROUPS if group[0] != "huragok"]
            counts = {key: sum(1 for s, _, _ in entries if s.group == key) for key, _, _ in GROUPS}
            warned = {key: sum(1 for item in items if warning_group(item) == key) for key, _, _ in GROUPS}
            rows = [(title, f"{counts[key]}", (summary(entries, key) or what)
                     + (f" · {warned[key]} warning{'s' if warned[key] != 1 else ''}" if warned[key] else ""))
                    for key, title, what in own]
            stopped = row.get("phase") in ("idle", "faulted", "offline")  # backoff: about to be restarted
            mates = self._folder_mates_running(settings, server)
            first = "stop it first (X)" if not stopped else f"stop {', '.join(mates)} first: in its folder" if mates else ""
            stopped = stopped and not mates
            extra = [("H", "Huragok's own for this server", "", (summary(entries, "huragok") or
                      "autostart, the daily restart, priority, Vergil") + f" ({settings.path.name}, editable)"),
                     ("O", f"open {server.config_path.name}", "", "in your editor: what's set here is yours to change")]
            if not server.rcon_password:
                extra.append(("Q", "add the console", "", "the [rcon] password it needs, shown before it's written"))
            extra += [(key, what, count, about) for key, what, count, about in self.server_page_extras(settings, server)
                      if key in ("B", "I")]
            kept = dedicated_history(settings, server.config_path)
            if kept:
                extra.append(("F", "history", str(len(kept)), f"versions of {server.config_path.name} kept before and "
                                                              "after each quick action: put one back"))
            extra.append(("N", "rename this server", "", first or "its name everywhere, when you ask: block, data, "
                                                                  "records, folder"))
            if self._shared_content(settings, server):
                extra.append(("C", "its own content folder", "", first or "stop sharing the root's content/ (no copy "
                              "on the same drive)"))
            keys = [str(n) for n in range(1, len(rows) + 1)] + [key for key, *_ in extra]
            panel = self.listing(rows + [(what, count, about) for _, what, count, about in extra], keys=keys)
            if lines := environment_lines(server, settings):
                self.note("Set where Huragok runs, and read by the program over the file: " + "; ".join(lines), "yellow")
            if loose := [item for item in items if warning_group(item) is None]:
                self.note(f"{len(loose)} more warning{'s' if len(loose) != 1 else ''} about this server: G shows them "
                          "all.", "yellow")
            answer = await self.prompt_line("  Which? (a group's number, or a letter; Esc goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            letters = [key for key, *_ in extra]
            if answer == "H":
                await self._settings_group(settings, server, row, next(g for g in GROUPS if g[0] == "huragok"))
            elif answer == "O":
                self.note(desktop.open_path(server.config_path))
            elif answer == "Q" and "Q" in letters:
                await self._quick_actions(settings, server, row)
            elif answer == "F" and "F" in letters:
                await self._put_back_dedicated(settings, server)
            elif answer == "N":
                if await self._rename(settings, server, stopped):
                    return
                continue
            elif answer == "C" and "C" in letters:
                await self._own_content(settings, server, stopped)
            elif answer in letters:
                await self._server_page_extra(settings, server, row, answer)
            elif answer.isdecimal() and 1 <= int(answer) <= len(own):
                await self._settings_group(settings, server, row, own[int(answer) - 1])
            else:
                continue
            settings = self._settings()
            server = self._server(settings, row) or server

    async def _put_back_dedicated(self, settings: Any, server: Any) -> None:
        """D -> F: a version of the server's dedicated.toml kept around a quick action, put back as it was (the file as
        it is now kept first, so that can be put back too). The server reads it at its next start."""
        from ...content.quickactions import HISTORY, owner_of
        from ...core import versions
        from ...core.files import write_atomic

        kept = dedicated_history(settings, server.config_path)
        self.heading(server.name, "Settings", "History", note=f"{server.config_path.name}, newest first")
        panel = self.listing([(version.stamp, version.how) for version in kept])
        answer = await self.prompt_line("  Put back which? (number; Esc goes back)", panel=panel)
        if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(kept):
            return
        version = kept[int(answer) - 1]
        if not await self.prompt_yes_no(f"  Put back the version of {version.stamp} ({version.how})? The file as it is "
                                        "now is kept first", False):
            return
        try:
            versions.keep_current(settings.records, HISTORY, owner_of(server.config_path), server.config_path)
            write_atomic(server.config_path, versions.read(version))
            versions.keep(settings.records, HISTORY, owner_of(server.config_path), server.config_path,
                          f"put back: the version of {version.stamp}")
        except OSError as error:
            self.note(f"Not put back: {error}", "yellow")
            return
        self.note(f"Put back. {server.name} reads it at its next start.", "green")

    async def _quick_actions(self, settings: Any, server: Any, row: Row) -> None:
        """D -> Q: the console for a server without one (content/quickactions.py), shown before it's written."""
        add_console = getattr(self, "screen_add_console", None)
        if add_console is not None:
            await add_console(server)

    def _setting_text(self, setting: Any, value: Any, known: Known | None = None) -> str:
        """A setting's value as the screens show it; a list of player IDs (admins, vote_kick_immune) with who each is,
        where Huragok knows: "Alice 1a2b3c4d, Bravo 9f8e7d6c". Only shown so: the file keeps the IDs alone."""
        if setting.key in ID_LISTS and isinstance(value, list) and value:
            known = known or self._known()
            known.names([str(v) for v in value])
            return ", ".join(known.tag(str(v)) if is_player_id(str(v)) else str(v) for v in value)
        return display(setting, value)

    async def _settings_group(self, settings: Any, server: Any, row: Row, group: tuple[str, str, str]) -> None:
        """One group of settings. A server's own (its dedicated.toml) are shown, never changed (0.7.0): each with what
        it does and, beside it, a variable that overrides it. Huragok's own for the server (settings.toml) change a step
        at a time, as before."""
        key, title, what = group
        version = await self._program_version(settings, server, row)
        while True:
            entries = [(s, v, w) for s, v, w in catalogue(server.config_path, server.name, settings)
                       if s.group == key and offered(s, version)]
            editable = key == "huragok"
            self.heading(server.name, "Settings", title, note=what if editable else
                         f"read-only · O opens {server.config_path.name} in your editor")
            rows = []
            known = self._known()
            overrides = {} if editable else override_texts(settings)
            for setting, value, where in entries:
                origin = "" if where in ("this server", "the file", f"[{setting.scope}]", "settings.toml") else \
                    f"({where})"
                about = setting.help + (f"  ! {overrides[setting.key]}" if setting.key in overrides else "")
                rows.append((setting.name, self._setting_text(setting, value, known), origin, about))
            panel = self.listing(rows, wrap=True)
            if not editable:
                answer = await self.prompt_line("  O opens the file; Esc goes back", panel=panel)
                if answer and answer.strip().upper() == "O":
                    self.note(desktop.open_path(server.config_path))
                    await self.wait_any_key(4)
                    continue
                return
            if len(entries) == 1:
                answer = "1" if await self.prompt_yes_no(f"  Change {entries[0][0].name}?", True) else ""
            else:
                answer = await self.prompt_line("  Which setting? (number; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(entries):
                return
            setting, value, where = entries[int(answer) - 1]
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
            options = [(str(n), choice, "now" if choice == str(value) else "")  # tick_rate's 60 is a number
                       for n, choice in enumerate(setting.choices, 1)]
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
            now = display(setting, value)
            if setting.key in ID_LISTS and isinstance(value, list) and value:
                known = self._known()
                known.names([str(v) for v in value])
                self.note("Now:")
                for entry in value:  # each whole, as it's typed back; with who it is, where that's known
                    self.note(f"  {known.tag(str(entry), whole=True) if is_player_id(str(entry)) else entry}")
                now = f"the {len(value)} above"
            text = await self.prompt_line(f"  New value ({hint}; now {now}; Enter leaves it)", secret=secret)
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
            self.note(f"{setting.key} = {self._setting_text(setting, new)} written for {setting.where}. Takes effect: "
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

    # The program a server runs ---------------------------------------------------------------

    async def _program_version(self, settings: Any, server: Any, row: Row) -> str | None:
        """The Project Reclaimer version a server runs: what it said while running, else its program file's own
        (`--version`, asked once per file, in a thread); None when neither is known."""
        if row.get("version"):
            return str(row["version"])
        from ...program.releases import program_of, program_version, readable

        program = program_of(settings, server)
        if program is None:
            return None
        version = await asyncio.to_thread(program_version, settings, program)
        return version if readable(version) else None

    # Renaming, and a content folder of its own -----------------------------------------------

    def _folder_mates_running(self, settings: Any, server: Any) -> list[str]:
        """The other servers in its folder (one dedicated.toml can have several) that aren't stopped. Renaming moves the
        folder under them, and its own content changes `content` for all of them: only the one being changed was
        checked (audit 2026-11, 7.5)."""
        phases = {str(r["name"]): r.get("phase") for r in self.controller.rows()}
        return [s.name for s in settings.servers if s is not server and s.config_path == server.config_path
                and phases.get(s.name, "idle") not in ("idle", "faulted", "offline")]

    def _shared_content(self, settings: Any, server: Any) -> bool:
        from ...content.serverops import shares_content

        return bool(shares_content(settings, server))

    async def _rename(self, settings: Any, server: Any, stopped: bool) -> bool:
        """True when it was renamed (the screen closes: the server has a new name on the dashboard)."""
        from ...content.serverops import ServerOpError, rename_server

        if not stopped:
            mates = self._folder_mates_running(settings, server)
            self.note(f"Renaming moves its folder, and {', '.join(mates)} run from it: stop them first." if mates else
                      "Renaming moves its data and folder: stop it first (X), then rename it.", "yellow")
            await self.wait_any_key(5)
            return False
        name = await self.prompt_line(f"  New name for {server.name}")
        if not name:
            return False
        row = next((r for r in self.controller.rows() if r["name"] == server.name), {})  # as it is now
        if mates := self._folder_mates_running(settings, server):  # started while the name was typed
            self.note(f"Not renamed: {', '.join(mates)} started meanwhile, from its folder. Stop them first.", "yellow")
            await self.wait_any_key(5)
            return False
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
        from ...content.serverops import ServerOpError, own_content, shares_content

        sharing = shares_content(settings, server)
        self.note(f"It shares {server.content_dir} with {', '.join(sharing)}. Its own copy lets the Superintendent "
                  "install Workshop updates after its games, and keeps its maps and game types its own.")
        if not stopped:
            mates = self._folder_mates_running(settings, server)
            self.note(f"Its folder's content setting is theirs too: stop {', '.join(mates)} first." if mates else
                      "Stop it first (X): the server program reads its content folder as it starts.", "yellow")
            await self.wait_any_key(6)
            return
        if not await self.prompt_yes_no("  Give it its own content folder?", True):
            return
        if mates := self._folder_mates_running(settings, server):
            self.note(f"Not done: {', '.join(mates)} started meanwhile, from its folder. Stop them first.", "yellow")
            await self.wait_any_key(6)
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
        known = self._known()
        while True:
            self.heading(row["name"], "Bans, admins and the VPN block",
                         note=f"as {self.by_name}" if self.by_name else "as the Superintendent")
            automod = settings.parts.get("automod")
            every = " (every server)" if automod is None or automod.options.get("across_servers", True) else ""
            choice = await self.choose("Which?", [
                ("L", "List bans"), ("A", "List admins"),
                ("U", f"Unban{every}"), ("+", f"Add an admin{every}"),
                ("N", f"Ban someone offline (ID, IP or range){every}"), ("-", f"Remove an admin{every}"),
                ("E", "Put one of its bans on every server (an in-game /ban)"),
                ("P", f"Pardon a kick{every}"), ("V", "VPN block: status / check an address"),
                ("W", "Let a player through the VPN block"), ("X", "Take that back")])
            if choice is None:
                return
            if choice == "L":
                await self._console(row["name"], "bans")
            elif choice == "E":
                self.note("Every server keeps its own ban list: an in-game /ban is that server's alone. This puts one "
                          "on Huragok's list, so every server's has it (and a new server's will).")
                target = await self.prompt_line("  The ban: its player ID, address or name, as L lists it")
                if not target:
                    continue
                for _, result in await self.controller.verb("banall", row["name"], target=target,
                                                            by=self.by_name or ""):
                    self.note(result.text or result.message, "green" if result.ok else "yellow")
            elif choice == "A":
                await self._console(row["name"], "admin list")
            elif choice == "V":
                address = await self.prompt_line("  An address to check (Enter: the block's status)")
                if address is None:
                    continue
                await self._console(row["name"], f"vpn {address}".strip())
            else:
                # A player is found as the game finds one (a name several have is asked about), then sent by their
                # #number or player ID: an ID's first 8 characters do too.
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
                line = await self._player_line(row["name"], f"{command} {target}", known)
                if line is not None:
                    await self._console(row["name"], line)

    # U: updates ------------------------------------------------------------------------------

    async def screen_update(self) -> None:
        self.title("UPDATES")
        choice = await self.choose("Which?", [
            ("1", "Huragok", "its GitHub releases; the servers keep running while it updates"),
            ("2", "The server program", "Project Reclaimer releases: each server installs its own (auto_update in "
                                        "its dedicated.toml); here, each server's version, the newest release and "
                                        "the warnings"),
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
        from ...content import upkeep

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
        from ...content.catalogue import parse_picks

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
        """A look, nothing more: each server installs its own releases (0.7.0)."""
        self.heading("The server program", note="each server installs its own releases: auto_update in its "
                                                "dedicated.toml")
        self.note("Asking GitHub for the newest Project Reclaimer release...", "cyan")
        results = await self.controller.verb("check_program_update")
        self.show_results(results)
        for _, result in results:
            for line in result.text.splitlines():
                self.terminal.print(f"    {line}", "yellow" if line.startswith("  ") else "gray")
        self.note("Huragok installs nothing: a server with auto_update on installs a release as it starts, and Huragok "
                  "restarts it after its game once a release is out ([server_updates] follow). One with auto_update "
                  "off stays on its version. Players must be on the server's exact version to join.")
        await self.wait_any_key(30)


    async def _update_workshop(self) -> None:
        self.heading("Workshop mods", note="the servers download and install their own mod updates; Huragok restarts "
                                           "one whose map in use holds an update up ([workshop_updates] restart)")
        self.note("Asking Steam for each item's newest version...", "cyan")
        results = await self.controller.verb("check_workshop")
        self.show_results(results)
        for _, result in results:
            for line in result.text.splitlines():
                self.terminal.print(f"    {line}", "white" if not line.startswith(" ") else "gray")
        if not any(r.ok and "waiting for a restart" in r.message for _, r in results):
            await self.wait_any_key(15)
            return
        if await self.choose("A downloaded update waits for a restart (its map is in use). Put it in now?", [
                ("R", "Restart its game now", "players are told, then dropped, and join again; the server installs "
                                              "the update as its game starts")],
                default="R", back="Esc leaves it waiting") is None:
            return
        self.show_results(await self.controller.verb("update_workshop"))
        await self.wait_any_key(10)


__all__ = ["ManageScreens", "describe_event", "FOLDER_SETTINGS", "SERVER_SETTINGS"]


# 0.7.0's read-only D (mockups-0.7.0.md 3): which group each warning is about, and the environment's overrides --------

WARNING_GROUPS = {"no-console": "rcon", "stats-relay-off": "network", "environment": "files"}


def warning_group(item: dict[str, Any]) -> str | None:
    """The settings group a warning (a dashboard row's warning_items entry) is about; None: about the server as a whole."""
    rule, area = str(item.get("rule") or ""), str(item.get("area") or "")
    if rule in WARNING_GROUPS:
        return WARNING_GROUPS[rule]
    if area == "plays":
        return "plays"
    if area == "updates":
        return "mods" if rule.startswith("mod-") else "updates"
    return None


def override_texts(settings: Any) -> dict[str, str]:
    """{setting key: "<VARIABLE> = <value> wins over the file"} for each RECLAIMER_DEDICATED_ variable set where
    Huragok runs (none with docker: the containers' environment is compose's)."""
    from ...core.serverenv import PREFIX, READ, overrides
    from ...core.serverfolders import server_environment_of

    environment = server_environment_of(settings.backend)
    if environment is None:
        return {}
    found: dict[str, str] = {}
    for full, value in overrides(environment).items():
        variable = full[len(PREFIX):]
        key = READ[variable][1] if variable in READ else variable.lower()
        found[key] = f"{full} = {value} is set where Huragok runs, and wins over the file"
    return found


def environment_lines(server: Any, settings: Any) -> list[str]:
    return [text.split(" is set where")[0] for text in override_texts(settings).values()]


def dedicated_history(settings: Any, path: Any) -> list[Any]:
    """The versions of a server folder's dedicated.toml kept around quick actions (content/quickactions.py)."""
    from ...content.quickactions import history

    try:
        return history(settings, path)
    except OSError:
        return []
