"""The window's D screens for Huragok's own files, a step at a time, and O, which opens them as they are.

D lists Huragok's five files above the servers:
  S  settings.toml   how Huragok runs, in groups (the Superintendent's, Vergil's, Huragok's own), each change
                     checked as Huragok will read the file at its next start, and the moderators' console logins
  C  commands.toml   who's who (owners, moderators, members, picked from the players seen), which role may use
                     each in-game command, which are off, the prefix and the cooldown
  A  automod.txt     Vergil's rules: the words, then what each strike does, picked from ready-made ladders or
                     asked one part at a time, each shown as players will meet it; a line of chat can be tried
  N  names.txt       the banned names, added and taken off; a name can be tried
  I  intervalmessages.txt  the interval messages every server says, added and taken off
and each server's own page there gains its playlists, custom games, bans, its mods switched on or off and its own
interval messages. Nobody
has to know a file's format: each value is asked for on its own, with what's there now listed first, and the
file keeps its comments and layout. O opens the folder, or any of these files in an editor, for what isn't here.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ...content.catalogue import (
    DisabledModsError, disabled_mods, installed_mods, is_disabled, mod_package, set_mod_disabled,
)
from ...content.mccimport import content_root_of
from ...core import desktop
from ...core import hostsettings as hs
from ...core.commandsfile import (
    LISTED, ROLES, TITLES, PermissionsError, add_player, all_commands, load_permissions, make_commands_toml,
    player_notes, remove_player, set_command_off, set_command_role, set_top,
)
from ...core.settings import COMMANDS_FILE
from ...core.storage import recent_players
from ...core.text import TIME
from ...lore import TAGS
from ...players.automod.automod import name_rules, same_name
from ...players.automod.matcher import NAME_MATCHES, Matcher, normalise
from ...players.automod.rules import (
    FOREVER, NAME, NAME_STRIKE, RuleError, add_name, change_rule, describe_unnamed, is_count, ladder, least_ban,
    least_forever, parse_line, read_names, read_rules_lenient, remove_name, rule_line, unnamed_ladders,
)
from ...players.messages.intervals import (
    MIN_SECONDS, MessageError, add_message, describe_every, own_file, read_messages, remove_message, seconds_every,
    summary,
)
from ...players.messages.intervals import FILE as MESSAGES_FILE
from ..controllers import Row
from .base import Screens

FILES = ("S", "C", "A", "N", "I")  # D's letters for Huragok's own files
EVERY_TIMES = ("5m", "10m", "15m", "30m", "45m", "1h", "2h")  # D -> I's times to pick from (T types another)
ROLE_TEXT = {"owner": "you: everything", "moderator": "what they can do is exactly what the commands give them",
             "member": "regulars, friends, clan: no vote can kick them"}
# (what it does; then as rule_line() takes them: mute times, muted for good, the kicks' start, kick times, the ban)
LADDERS: list[tuple[str, list[str], int | None, int | None, list[str], int | None]] = [
    ("kicked every time (the server's plain kick)", [], None, 0, [], None),
    ("muted: 1 day, then 3 days, then 7 days each time", ["1d", "3d", "7d"], None, None, [], None),
    ("muted 1 day, then 7 days, then out 1 day, then banned for good", ["1d", "7d"], None, 2, ["1d"], 3),
    ("banned for good the first time", [], None, 0, [], 0),
]


class FileScreens(Screens):
    """Mixed into Menu (last, so its stubs never stand in for another mixin's screens)."""


    # Huragok's files, as D lists them -----------------------------------------------------------

    def _words_path(self, settings: Any) -> Path:
        return settings.home / str(settings.parts["automod"].options.get("words") or "automod.txt")

    def _names_path(self, settings: Any) -> Path:
        return settings.home / str(settings.parts["automod"].options.get("names") or "names.txt")

    def huragok_files(self, settings: Any) -> list[tuple[str, str, str, str]]:
        """(D's letter, what it is, the file, what's in it now) for each of Huragok's own files."""
        files = [("S", "How Huragok runs", settings.path.name, self._settings_summary(settings)),
                 ("C", "Roles and commands", COMMANDS_FILE, self._commands_summary(settings))]
        words, names = self._words_path(settings), self._names_path(settings)
        try:
            rules, left = read_rules_lenient(words)
            about = f"{len(rules)} rule{'s' if len(rules) != 1 else ''}" + (
                f" ({sum(1 for r in rules if r.label)} named)" if rules else ": none yet") + (
                f" · {len(left)} line{'s' if len(left) != 1 else ''} left out" if left else "")
        except (RuleError, OSError) as error:
            about = f"can't be read: {error}"
        files.append(("A", "Vergil's word list", words.name, about))
        try:
            count = len(read_names(names))
            about = f"{count} banned name{'s' if count != 1 else ''}"
        except (RuleError, OSError) as error:
            about = f"can't be read: {error}"
        files.append(("N", "Vergil's banned names", names.name, about))
        try:
            messages, problems = read_messages(settings.home / MESSAGES_FILE)
            about = summary(messages) + (f" · {len(problems)} line{'s' if len(problems) != 1 else ''} left out"
                                         if problems else "")
        except (MessageError, OSError) as error:
            about = f"can't be read: {error}"
        files.append(("I", "Interval messages", MESSAGES_FILE, about))
        return files

    def _settings_summary(self, settings: Any) -> str:
        try:
            data = hs.read_toml(settings.path)
        except (OSError, ValueError) as error:
            return f"can't be read: {error}"
        automod = data.get("automod", {}) if isinstance(data.get("automod"), dict) else {}
        updates = data.get("updates", {}) if isinstance(data.get("updates"), dict) else {}
        vergil = "automod off" if automod.get("enabled") is False else (
            "automod in dry run" if automod.get("dry_run") else "automod acting")
        return (f"backend {hs.backend_of(data)} · {vergil} · Huragok updates "
                f"{'by themselves' if updates.get('auto_update') else 'when you say (U)'}")

    def _commands_summary(self, settings: Any) -> str:
        path = settings.home / COMMANDS_FILE
        if not path.exists():
            return "not there: every command has its default role"
        try:
            permissions = load_permissions(path)
        except PermissionsError as error:
            return f"can't be read: {error}"
        sizes = {role: len(permissions.members.get(role, set())) for role in ("owner", "moderator", "member")}
        counts = [f"{size} {role}{'s' if size != 1 else ''}" for role, size in sizes.items()]
        return " · ".join(counts) + (f" · {len(permissions.off)} off" if permissions.off else "")

    async def huragok_file(self, settings: Any, key: str) -> None:
        if key == "S":
            await self._host_settings(settings)
        elif key == "C":
            await self._commands_file(settings)
        elif key == "A":
            await self._word_list(settings)
        elif key == "N":
            await self._banned_names(settings)
        elif key == "I":
            await self._interval_messages(settings, None)

    # S: settings.toml ---------------------------------------------------------------------------

    async def _host_settings(self, settings: Any) -> None:
        path = settings.path
        while True:
            try:
                entries = hs.catalogue(path)
            except (OSError, ValueError) as error:
                self.note(f"Can't read {path.name}: {error}. O opens it, to put it right by hand.", "red")
                await self.wait_any_key(8)
                return
            self.heading("Huragok", "How it runs", note=f"{path.name} · a change applies {hs.APPLIES}")
            rows, colours = [], []
            for key, whose, title, what in hs.GROUPS:
                count = sum(1 for s, _, _ in entries if s.group == key)
                if key == "relay":
                    users = len(hs.relay_users(path))
                    what = f"{users} moderator{'s' if users != 1 else ''} · " + hs.summary(entries, key, 2)
                rows.append((title, whose, str(count), hs.summary(entries, key, 3) if key != "relay" else what))
                colours.append(TAGS[whose].colour)
            panel = self.listing(rows, colours=colours, wrap=True)
            answer = await self.prompt_line("  Which group? (number; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(hs.GROUPS):
                return
            await self._host_group(path, hs.GROUPS[int(answer) - 1])

    async def _host_group(self, path: Path, group: tuple[str, str, str, str]) -> None:
        key, _, title, what = group
        while True:
            try:
                entries = [e for e in hs.catalogue(path) if e[0].group == key]
            except (OSError, ValueError) as error:
                self.note(f"Can't read {path.name}: {error}", "red")
                await self.wait_any_key(6)
                return
            self.heading("Huragok", "How it runs", title, note=what)
            rows = [(s.where, hs.show(s, v), "" if where == "the file" else f"({where})", s.help)
                    for s, v, where in entries]
            keys = [str(n) for n in range(1, len(entries) + 1)]
            if key == "relay":
                users = hs.relay_users(path)
                rows.append(("moderators", str(len(users)), "", ", ".join(f"{u.name} ({u.role})" for u in users)
                             or "nobody yet: each has a name, a password and a role"))
                keys.append("M")
            panel = self.listing(rows, keys=keys, wrap=True)
            answer = await self.prompt_line("  Which setting? (number" + (", M for the moderators" if key == "relay"
                                                                           else "") + "; Esc goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            if answer == "M" and key == "relay":
                await self._relay_moderators(path)
                continue
            if not answer.isdecimal() or not 1 <= int(answer) <= len(entries):
                continue
            await self._edit_host_setting(path, *entries[int(answer) - 1])

    async def _edit_host_setting(self, path: Path, setting: hs.HostSetting, value: Any, where: str) -> None:
        self.heading("Huragok", "How it runs", setting.where, note=f"{setting.help}. Applies {hs.APPLIES}.")
        if setting.key == "backend" or setting.group == "files":
            self.note("Careful: this changes where Huragok looks for things, or how it treats every server, from its "
                      "next start. `check` (C) shows what it would find.", "yellow")
        new: Any
        if setting.kind == "bool":
            picked = await self.choose(f"{setting.key}?", [("Y", "on", "now" if value is True else ""),
                                                           ("N", "off", "now" if value is False else "")],
                                       back="Esc leaves it")
            if picked is None:
                return
            new = picked == "Y"
        elif setting.kind == "choice":
            options = [(str(n), choice, "now" if choice == value else "") for n, choice in enumerate(setting.choices, 1)]
            picked = await self.choose(f"{setting.key}?", options, back="Esc leaves it")
            if picked is None:
                return
            new = setting.choices[int(picked) - 1]
        elif setting.kind in ("roles", "share"):
            choices = hs.ROLES if setting.kind == "roles" else hs.SHARE
            have = [str(v) for v in value] if isinstance(value, list) else []
            panel = self.listing([(c, "yes" if c in have else "") for c in choices])
            answer = await self.prompt_line("  Which ones? (numbers like 4,5; - for none; Enter leaves it)",
                                            panel=panel)
            if not answer:
                return
            if answer.strip() == "-":
                new = []
            else:
                numbers = [p for p in answer.replace(",", " ").split()]
                if not all(p.isdecimal() and 1 <= int(p) <= len(choices) for p in numbers):
                    self.note("Not changed: the numbers in the list.", "yellow")
                    await self.wait_any_key(4)
                    return
                new = [choices[int(p) - 1] for p in dict.fromkeys(numbers)]
        else:
            back = " - puts the default back;" if setting.default is not None and not setting.optional else \
                " - takes the line out;" if setting.optional else ""
            text = await self.prompt_line(f"  New value ({hs.hint(setting)};{back} now {hs.show(setting, value)}; "
                                          "Enter leaves it)")
            if not text:
                return
            if text.strip() == "-" and (setting.default is not None or setting.optional):
                new = None if setting.optional else setting.default
            elif text.strip().lower() == "none" and setting.kind in ("numbers", "strings", "words"):
                new = []
            else:
                try:
                    new = hs.parse(setting, text)
                except hs.HostSettingsError as error:
                    self.note(f"Not changed: {error}.", "yellow")
                    await self.wait_any_key(4)
                    return
        if new == value and where == "the file":
            self.note("That's what it is already.")
            return
        try:
            hs.write(path, setting, new)
        except (hs.HostSettingsError, OSError) as error:
            text = str(error)
            self.note(text[0].upper() + text[1:] if text.startswith("not changed") else f"Not changed: {text}",
                      "yellow")
            await self.wait_any_key(8)
            return
        shown = "taken out of the file" if new is None else f"= {hs.show(setting, new)}"
        self.note(f"{setting.where} {shown}, written to {path.name}. Applies {hs.APPLIES}: close this window (Q; "
                  "the servers keep running) and start it again.", "green")

    async def _relay_moderators(self, path: Path) -> None:
        while True:
            try:
                users = hs.relay_users(path)
                enabled = hs.current(hs.read_toml(path), hs.find("relay", "enabled"))[0]
            except (OSError, ValueError) as error:
                self.note(f"Can't read {path.name}: {error}", "red")
                await self.wait_any_key(6)
                return
            self.heading("Huragok", "Moderators' console", "Moderators",
                         note="each connects with the official RCON client (or any WebSocket console tool) to "
                              "Huragok, by a password of their own; the commands their role allows pass")
            if not enabled:
                self.note("The relay is off: turn [relay] enabled on as well, for them to connect.", "yellow")
            rows = [(u.name, u.role, f"password set ({u.password_length} characters)") for u in users]
            panel = self.listing(rows + [("add a moderator", "", "")],
                                 keys=[str(n) for n in range(1, len(users) + 1)] + ["A"])
            answer = await self.prompt_line("  Which? (number, or A; Esc goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            try:
                if answer == "A":
                    await self._add_moderator(path)
                elif answer.isdecimal() and 1 <= int(answer) <= len(users):
                    await self._change_moderator(path, users[int(answer) - 1])
            except (hs.HostSettingsError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    async def _relay_role(self, now: str | None = None) -> str | None:
        picked = await self.choose("Which role?", [(str(n), role, "now" if role == now else "")
                                                   for n, role in enumerate(hs.RELAY_ROLES, 1)], back="Esc leaves it")
        return None if picked is None else hs.RELAY_ROLES[int(picked) - 1]

    async def _add_moderator(self, path: Path) -> None:
        name = await self.prompt_line("  Their name, as players read it (\"kicked by Alice\")")
        if not name:
            return
        password = await self.prompt_line("  Their password (8 to 128 characters, theirs alone)", secret=True)
        if not password:
            return
        role = await self._relay_role()
        if role is None:
            return
        hs.add_relay_user(path, name, password, role)
        self.note(f"{name.strip()} added as {role}. Applies {hs.APPLIES}.", "green")

    async def _change_moderator(self, path: Path, user: hs.RelayUser) -> None:
        choice = await self.choose(f"{user.name}?", [("R", "Their role", user.role), ("P", "A new password"),
                                                     ("X", "Take them out")])
        if choice == "R":
            role = await self._relay_role(user.role)
            if role is not None and role != user.role:
                hs.change_relay_user(path, user.name, role=role)
                self.note(f"{user.name} is {role} now. Applies {hs.APPLIES}.", "green")
        elif choice == "P":
            password = await self.prompt_line("  Their new password (8 to 128 characters)", secret=True)
            if password:
                if problem := hs.check_relay_user(path, user.name, password, user.role, replacing=user.name):
                    raise hs.HostSettingsError(problem)
                hs.change_relay_user(path, user.name, password=password)
                self.note(f"{user.name}'s password changed. Applies {hs.APPLIES}.", "green")
        elif choice == "X" and await self.prompt_yes_no(f"  Take {user.name} out?", False):
            hs.remove_relay_user(path, user.name)
            self.note(f"{user.name} taken out. Applies {hs.APPLIES}.", "green")

    # C: commands.toml ---------------------------------------------------------------------------

    async def _commands_file(self, settings: Any) -> None:
        path = settings.home / COMMANDS_FILE
        if not path.exists():
            self.note(f"There's no {COMMANDS_FILE}: every command has its default role and nobody is listed.",
                      "yellow")
            if not await self.prompt_yes_no(f"  Make {COMMANDS_FILE} now (every command under its default role)?",
                                            True):
                return
            try:
                make_commands_toml(path)
            except OSError as error:
                self.note(f"Couldn't write {COMMANDS_FILE}: {error}", "red")
                await self.wait_any_key(6)
                return
        while True:
            try:
                permissions = load_permissions(path)
            except PermissionsError as error:
                self.note(f"{error}. O opens it, to put it right by hand.", "red")
                await self.wait_any_key(8)
                return
            self.heading("Huragok", "Roles and commands", note=f"{COMMANDS_FILE} · Vergil reads it again within "
                                                               "seconds")
            moved = sum(1 for name, spec in all_commands().items() if permissions.role_for(spec) != spec.role)
            options = [(str(n), f"{role.capitalize()}s ({len(permissions.members.get(role, set()))})",
                        ROLE_TEXT[role]) for n, role in enumerate(("owner", "moderator", "member"), 1)]
            options += [("4", f"Commands ({len(all_commands())})", f"which role may use each: {moved} moved from "
                                                                   f"its default, {len(permissions.off)} off"),
                        ("5", "Prefix", f"{permissions.prefix} now: what commands start with"),
                        ("6", "Cooldown", f"{permissions.cooldown:g} seconds between uses of a command, per player")]
            self.note("Admins are each server's own: a server's settings (Safety and admins), or B with it running.")
            choice = await self.choose("Which?", options)
            if choice is None:
                return
            try:
                if choice in ("1", "2", "3"):
                    await self._role_players(settings, path, ("owner", "moderator", "member")[int(choice) - 1])
                elif choice == "4":
                    await self._command_roles(path)
                elif choice == "5":
                    text = await self.prompt_line(f"  New prefix (now {permissions.prefix}; Enter leaves it)")
                    if text and text.strip() != permissions.prefix:
                        set_top(path, "prefix", text.strip())
                        self.note(f"Commands start with {text.strip()} now: !help reads {text.strip()}help.", "green")
                elif choice == "6":
                    text = await self.prompt_line(f"  Seconds between uses (now {permissions.cooldown:g}; Enter "
                                                  "leaves it)")
                    if text:
                        try:
                            seconds = float(text)
                        except ValueError:
                            seconds = -1
                        if not 0 <= seconds <= 3600:
                            self.note("Not changed: a number of seconds, 0 to 3600.", "yellow")
                        else:
                            set_top(path, "cooldown_seconds", int(seconds) if seconds == int(seconds) else seconds)
                            self.note(f"cooldown_seconds = {seconds:g} written.", "green")
            except (PermissionsError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    async def _role_players(self, settings: Any, path: Path, role: str) -> None:
        while True:
            members = sorted(load_permissions(path).members.get(role, set()))
            notes = player_notes(path)
            self.heading("Huragok", "Roles and commands", f"{role.capitalize()}s", note=ROLE_TEXT[role])
            rows = [(*_who_and_how(notes.get(pid, ""), pid), f"{pid[:16]}…") for pid in members]
            panel = self.listing([(who, player_id, how) for who, how, player_id in rows]
                                 + [(f"add a{'n' if role == 'owner' else ''} {role}", "", "")],
                                 keys=[str(n) for n in range(1, len(members) + 1)] + ["A"])
            answer = await self.prompt_line("  Which? (a number takes them out, A adds one; Esc goes back)",
                                            panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            try:
                if answer == "A":
                    await self._add_role_player(settings, path, role)
                elif answer.isdecimal() and 1 <= int(answer) <= len(members):
                    player_id = members[int(answer) - 1]
                    who = _who_and_how(notes.get(player_id, ""), player_id)[0]
                    if await self.prompt_yes_no(f"  Take {who} out of the {role}s?", False):
                        remove_player(path, role, player_id)
                        self.note(f"{who} isn't {('an ' if role == 'owner' else 'a ') + role} any more.", "green")
            except (PermissionsError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    async def _add_role_player(self, settings: Any, path: Path, role: str) -> None:
        seen = recent_players(settings.database)
        listed = load_permissions(path).members
        if seen:
            self.note("Players seen lately (from Huragok's records), or type a player ID:")
            now = time.time()
            rows = [(name, player_id[:16] + "…", _ago(now - when) + next(
                (f" · {other} now" for other in LISTED if player_id in listed.get(other, set())), ""))
                    for player_id, name, when in seen]
            panel = self.listing(rows)
        else:
            self.note("No players in Huragok's records yet: type the player's ID (64 letters and digits; players "
                      "find theirs in game under Settings > Online services).")
            panel = []
        answer = await self.prompt_line("  Who? (a number, or a player ID; Esc goes back)", panel=panel)
        if not answer:
            return
        answer = answer.strip()
        if answer.isdecimal() and 1 <= int(answer) <= len(seen):
            player_id, name, _ = seen[int(answer) - 1]
        else:
            player_id, name = answer, ""
        who = name or player_id[:8]
        note = f"{name}: added as {role} in Huragok's window on {time.strftime('%Y-%m-%d')}" if name else \
            f"added as {role} in Huragok's window on {time.strftime('%Y-%m-%d')}"
        moved = add_player(path, role, player_id, note)
        self.note(f"{who} is {('an ' if role == 'owner' else 'a ') + role} now"
                  + (f" (no longer {', '.join(moved)}: one role each)" if moved else "") + ".", "green")

    async def _command_roles(self, path: Path) -> None:
        specs = all_commands()
        while True:
            permissions = load_permissions(path)
            ordered = sorted(specs.values(), key=lambda s: (ROLES.index(permissions.role_for(s)), s.name))
            self.heading("Huragok", "Roles and commands", "Commands",
                         note="each one's lowest role: every role above it can use it too")
            rows = [(f"{permissions.prefix}{s.name}", "OFF" if s.name in permissions.off else
                     TITLES[permissions.role_for(s)], "" if permissions.role_for(s) == s.role else
                     f"(default {TITLES[s.role]})", s.help) for s in ordered]
            colours = ["gray" if s.name in permissions.off else None for s in ordered]
            panel = self.listing(rows, colours=colours, wrap=True)
            answer = await self.prompt_line("  Which command? (number; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(ordered):
                return
            spec = ordered[int(answer) - 1]
            role = permissions.role_for(spec)
            off = spec.name in permissions.off
            options = [(str(n), TITLES[r], "now" if r == role else "default" if r == spec.role else "")
                       for n, r in enumerate(ROLES, 1)]
            if spec.name != "help":
                options.append(("O", "Switch it on again" if off else "Switch it off", "nobody can use it" if not
                                off else f"for {TITLES[role]} and up"))
            picked = await self.choose(f"Who may use {permissions.prefix}{spec.name}?", options, back="Esc leaves it")
            if picked is None:
                continue
            try:
                if picked == "O":
                    set_command_off(path, spec.name, not off)
                    self.note(f"{permissions.prefix}{spec.name} is {'on again' if off else 'off'}.", "green")
                elif ROLES[int(picked) - 1] != role:
                    set_command_role(path, spec.name, ROLES[int(picked) - 1])
                    self.note(f"{permissions.prefix}{spec.name}: {TITLES[ROLES[int(picked) - 1]]} and up.", "green")
            except (PermissionsError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    # A: automod.txt -----------------------------------------------------------------------------

    async def _word_list(self, settings: Any) -> None:
        path = self._words_path(settings)
        while True:
            try:
                rules, left = read_rules_lenient(path) if path.exists() else ([], [])
            except (RuleError, OSError) as error:
                self.note(f"{error}. O opens it, to put it right by hand.", "red")
                await self.wait_any_key(8)
                return
            self.heading("Huragok", "Vergil's word list",
                         note=f"{path.name} · Vergil reads it again within seconds; disguised words (b4dw0rd, "
                              "b a d w o r d) are caught without listing them")
            for item in left:
                self.note(f"Left out: {item.why}. Vergil runs without it; O opens the file to put it right.", "red")
            rows = [(rule.label or f"({rule.name})", ", ".join(rule.words) + (
                f" (never: {', '.join(rule.allow)})" if rule.allow else ""), "; ".join(ladder(rule)))
                    for rule in rules]
            rows += [("add a rule", "", ""), ("try a line of chat", "", "")]
            panel = self.listing(rows, keys=[str(n) for n in range(1, len(rules) + 1)] + ["A", "T"], wrap=True)
            answer = await self.prompt_line("  Which? (a rule's number changes it, A adds one, T tries a line; Esc "
                                            "goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            try:
                if answer == "A":
                    await self._new_rule(path, rules)
                elif answer == "T":
                    await self._try_line(rules)
                elif answer.isdecimal() and 1 <= int(answer) <= len(rules):
                    await self._change_rule(path, rules[int(answer) - 1], rules)
            except (RuleError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    async def _rule_words(self, now: list[str] | None = None) -> list[str] | None:
        self.note("word* also catches longer words starting with it, *word* catches it inside one; -word is never "
                  "caught by this rule.")
        text = await self.prompt_line("  The words, separated by commas" + (f" (now {', '.join(now)}; Enter leaves "
                                                                           "them)" if now else ""))
        if not text:
            return None
        return [w.strip() for w in text.split(",") if w.strip()]

    async def _rule_steps(self) -> tuple[list[str], int | None, int | None, list[str], int | None] | None:
        """What each strike does, as rule_line() takes it (mute times, muted for good, the kicks' start, kick times,
        ban): a ready-made ladder, or the host's own, a part at a time."""
        options = [(str(n), what) for n, (what, *_) in enumerate(LADDERS, 1)]
        options.append((str(len(LADDERS) + 1), "your own: the mutes, the kicks and the ban, a question each"))
        picked = await self.choose("What does each strike do?", options)
        if picked is None:
            return None
        if int(picked) <= len(LADDERS):
            _, mutes, forever_after, kick_after, kicks, ban_after = LADDERS[int(picked) - 1]
            return list(mutes), forever_after, kick_after, list(kicks), ban_after
        mutes: list[str] = []
        forever_after: int | None = None
        kick_after: int | None = 0  # no mutes: every strike kicks
        kicks: list[str] = []
        if await self.prompt_yes_no("  Muted at some point?", False):
            mutes = await self._times("  How long each mute lasts, in order from the first strike (like 1d, 3d, 7d; "
                                      "the last repeats until the kicks; perm last is for good)", required=True) or []
            if not mutes:
                return None
            if mutes[-1] != FOREVER and await self.prompt_yes_no(
                    f"  Muted for good after a number of strikes? (no: {mutes[-1]} each time from then on)", False):
                least = least_forever(mutes)
                forever_after = await self._strike_count(
                    f"How many strikes before muted for good? (at least {least}, so every mute time comes first; "
                    f"kicks or not, every strike after it mutes for good)", least)
                if forever_after is None:
                    return None
            kick_after = None
            if await self.prompt_yes_no("  Kicked after the mutes?", False):
                kick_after = await self._strike_count(
                    f"How many strikes before the first kick? (Enter: {len(mutes)}, once every mute time is used; a "
                    f"mute time past it still mutes them, once the kick is over)", 0, default=len(mutes))
                if kick_after is None:
                    return None
        if kick_after is not None:
            kicks = await self._times("  How long each kick keeps them out, in order (like 30m, 1h, 1d; the last "
                                      "repeats). Enter: the server's plain kick")
            if kicks is None:
                return None
        ban_after: int | None = None
        if await self.prompt_yes_no("  Banned for good at some point?", False):
            least = least_ban(mutes, forever_after, kick_after, kicks)
            ban_after = await self._strike_count(
                f"How many strikes before the ban? (at least {least}, so every mute and kick comes first)"
                if least else "How many strikes before the ban? (0: banned the first time)", least)
            if ban_after is None:
                return None
        return mutes, forever_after, kick_after, kicks, ban_after

    async def _times(self, prompt: str, *, required: bool = False) -> list[str] | None:
        """Times like 30m, 1d (perm too, where it's allowed): [] for none; None when the host backs out."""
        while True:
            text = await self.prompt_line(prompt)
            if text is None or not text.strip():
                return None if required or text is None else []
            times = [t.strip().lower() for t in text.split(",") if t.strip()]
            if bad := next((t for t in times if t != FOREVER and not TIME.fullmatch(t)), None):
                self.note(f"'{bad}' isn't a time: a number and s, m, h, d or w, like 30m or 7d.", "yellow")
                continue
            return times

    async def _strike_count(self, prompt: str, least: int = 0, *, default: int | None = None) -> int | None:
        """A number of strikes, `least` or more (Enter: `default`, or `least` when that's more than 0); None to back
        out."""
        shown = least if default is None else default
        while True:
            text = await self.prompt_line(f"  {prompt}", str(shown) if shown else "")
            if not text:
                return None
            if is_count(text.strip()) and int(text) >= least:
                return int(text)
            self.note(f"A number, {least} or more." if least else "A number, 0 or more.", "yellow")

    async def _rule_name(self, now: str = "", taken: tuple[str, ...] = ()) -> str | None:
        """A rule's name, asked again until the list can take it: "" for none, None to back out."""
        while True:
            text = await self.prompt_line("  Its name: one word players see and moderators type in !strike (letters, "
                                          "digits, - or _" + ("; - for none" if now else "; Enter for none") + ")", now)
            if text is None:
                return None
            name = "" if text.strip() == "-" else text.strip().lower()
            if name and not NAME.fullmatch(name):
                self.note("One word, up to 32 characters: letters, digits, - or _.", "yellow")
            elif name == NAME_STRIKE:
                self.note(f"Not {NAME_STRIKE}: !strike <player> {NAME_STRIKE} bans a player's name.", "yellow")
            elif name and name != now and name in taken:
                self.note(f"A rule is called {name} already: pick another name.", "yellow")
            else:
                return name

    async def _save_rule(self, path: Path, line: int | None, text: str, verb: str, done: str) -> bool:
        """Show the rule as players will meet it, and write it once the host says so."""
        try:
            rule = parse_line(text, line or 1)
        except RuleError as error:
            self.note(f"That rule can't be used: {str(error).removeprefix('line 1: ')}.", "yellow")
            await self.wait_any_key(6)
            return False
        assert rule is not None
        self.note(f"The line: {text}", None)
        for step in ladder(rule):
            self.note(f"  {step}", "cyan")
        if not await self.prompt_yes_no(f"  {verb} it?", True):
            return False
        rules = change_rule(path, line, text)
        self.note(f"{done}. Vergil reads {path.name} again within seconds.", "green")
        mine = next((r for r in rules if r.line == line), None) if line else (rules[-1] if rules else None)
        if mine is not None and mine in unnamed_ladders(rules):
            self.note(describe_unnamed(mine, path.name), "yellow")
        return True

    async def _new_rule(self, path: Path, rules: list[Any]) -> None:
        self.heading("Huragok", "Vergil's word list", "A new rule")
        words = await self._rule_words()
        if not words:
            return
        steps = await self._rule_steps()
        if steps is None:
            return
        name = await self._rule_name(taken=tuple(r.name for r in rules))
        if name is None:
            return
        await self._save_rule(path, None, rule_line(words, *steps, name), "Add", "Added")

    async def _change_rule(self, path: Path, rule: Any, rules: list[Any]) -> None:
        self.heading("Huragok", "Vergil's word list", rule.label or rule.name, note="; ".join(ladder(rule)))
        choice = await self.choose("Change what?", [("W", "Its words", ", ".join(rule.words)),
                                                    ("S", "What each strike does"),
                                                    ("N", "Its name", rule.label or "none"),
                                                    ("X", "Take it out")])
        if choice is None:
            return
        words = [*rule.words, *(f"-{w}" for w in rule.allow)]
        steps = (rule.mutes, rule.mute_forever_after, rule.kick_after, rule.kicks, rule.ban_after)
        name = rule.label
        if choice == "X":
            if await self.prompt_yes_no(f"  Take the {rule.label or rule.name} rule out?", False):
                change_rule(path, rule.line, None)
                self.note(f"Taken out. Strikes players had under {rule.name} stay in the records.", "green")
            return
        if choice == "W":
            words = await self._rule_words(words) or []
            if not words:
                return
        elif choice == "S":
            new = await self._rule_steps()
            if new is None:
                return
            steps = new
        elif choice == "N":
            picked = await self._rule_name(rule.label, tuple(r.name for r in rules if r is not rule))
            if picked is None or picked == rule.label:
                return
            if rule.label:
                self.note(f"Strikes players have under {rule.label} stay under that name, so they stop counting on "
                          "this rule.", "yellow")
            name = picked
        await self._save_rule(path, rule.line, rule_line(words, *steps, name), "Save", "Saved")

    async def _try_line(self, rules: list[Any]) -> None:
        text = await self.prompt_line("  A line of chat, as a player might type it")
        if not text:
            return
        hits = Matcher(rules).find(text)
        if not hits:
            self.note("Nothing on the list is in it.", "green")
        for hit in hits:
            self.note(f"'{text[hit.start:hit.end]}' is caught by {hit.rule.label or hit.rule.name}: "
                      f"{ladder(hit.rule)[0]}", "yellow")
        await self.wait_any_key(8)

    # N: names.txt -------------------------------------------------------------------------------

    async def _banned_names(self, settings: Any) -> None:
        path = self._names_path(settings)
        while True:
            try:
                names = read_names(path)
            except (RuleError, OSError) as error:
                self.note(f"{error}. O opens it, to put it right by hand.", "red")
                await self.wait_any_key(8)
                return
            self.heading("Huragok", "Vergil's banned names",
                         note=f"{path.name} · a player with one is kicked as they join, every time, until they change "
                              "it; capitals, digits for letters and lookalikes are caught for you")
            notes = _name_notes(path)
            rows = [(n.text, notes.get(n.line, "")) for n in names] + [("ban a name", ""), ("try a name", "")]
            panel = self.listing(rows, keys=[str(n) for n in range(1, len(names) + 1)] + ["A", "T"])
            answer = await self.prompt_line("  Which? (a number takes it off, A bans one, T tries one; Esc goes back)",
                                            panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            try:
                if answer == "A":
                    name = await self.prompt_line("  The name (*name* also catches it inside a longer one)")
                    if not name or not name.strip():
                        continue
                    if not normalise(name.strip().strip("*")).strip():
                        self.note("That name has no letters or digits left once accents and spacing are taken out, "
                                  "so it would catch everyone.", "yellow")
                        await self.wait_any_key(5)
                        continue
                    why = await self.prompt_line("  Why, for the note beside it (Enter: no note)")
                    add_name(path, name.strip(), self.by_name or "the host in Huragok's window", (why or "").strip())
                    self.note(f"{name.strip()} is banned: a player using it is kicked as they join.", "green")
                elif answer == "T":
                    await self._try_name(settings, names)
                elif answer.isdecimal() and 1 <= int(answer) <= len(names):
                    name = names[int(answer) - 1].text
                    if await self.prompt_yes_no(f"  Take {name} off the banned names?", False):
                        removed = remove_name(path, name, same_name)
                        self.note(f"{name} can be used again." if removed else f"{name} wasn't there any more.",
                                  "green")
            except (RuleError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    async def _try_name(self, settings: Any, names: list[Any]) -> None:
        text = await self.prompt_line("  A player's name")
        if not text:
            return
        hits = Matcher(name_rules(names)).find(text)
        if hits:
            line = hits[0].rule.line
            self.note(f"Kicked as they join: the banned name '{next(n.text for n in names if n.line == line)}'.",
                      "yellow")
        elif (options := settings.parts["automod"].options).get("check_names", True):
            try:
                rules = read_rules_lenient(self._words_path(settings))[0]
            except (RuleError, OSError):
                rules = []
            mode = options.get("name_match", "anywhere")
            hit = next(iter(Matcher(rules, names=mode if mode in NAME_MATCHES else "anywhere").find(text)), None)
            self.note(f"Kicked as they join: it has '{hit.found}' in it, the word list's {hit.word} "
                      f"({hit.rule.label or hit.rule.name} line)." if hit else "That name is fine.",
                      "yellow" if hit else "green")
        else:
            self.note("That name is fine.", "green")
        await self.wait_any_key(8)

    # I: intervalmessages.txt, for every server (D's list) and each server's own (its page) ----------------

    async def _interval_messages(self, settings: Any, server: Any | None) -> None:
        home = settings.home
        path = home / MESSAGES_FILE if server is None else own_file(server.folder, home)
        if path is None:
            self.note(f"{server.name}'s folder is Huragok's own, so its interval messages are the ones for every "
                      "server: I in D's list.", "yellow")
            await self.wait_any_key(6)
            return
        while True:
            try:
                messages, problems = read_messages(path)
                everywhere = read_messages(home / MESSAGES_FILE)[0] if server is not None else []
            except (MessageError, OSError) as error:
                self.note(f"{error}. O opens it, to put it right by hand.", "red")
                await self.wait_any_key(8)
                return
            crumbs = ("Huragok", "Interval messages") if server is None else (server.name, "Settings",
                                                                             "Interval messages")
            self.heading(*crumbs, note=f"{path} · said to everyone on {'every server' if server is None else 'this server'}, "
                                       "again and again, each after its own time, while someone's on; changes "
                                       "apply within seconds")
            for line in problems:
                self.note(f"{line}: left out, O opens the file to put it right", "yellow")
            if everywhere:
                self.note(f"It says the ones for every server too (I in D's list): {summary(everywhere)}.")
            rows = [(f"every {describe_every(m.seconds)}", m.text) for m in messages] + [("add a message", "")]
            panel = self.listing(rows, keys=[str(n) for n in range(1, len(messages) + 1)] + ["A"], wrap=True)
            answer = await self.prompt_line("  Which? (a number takes it off, A adds one; Esc goes back)", panel=panel)
            if not answer:
                return
            answer = answer.strip().upper()
            try:
                if answer == "A":
                    await self._add_interval_message(path, "every" if server is None else "server")
                elif answer.isdecimal() and 1 <= int(answer) <= len(messages):
                    message = messages[int(answer) - 1]
                    if await self.prompt_yes_no(f"  Take off \"{message.text}\" (every "
                                                f"{describe_every(message.seconds)})?", False):
                        gone = remove_message(path, message)
                        self.note("Taken off: it isn't said any more." if gone
                                  else "It wasn't there any more (the file changed): look again.", "green")
            except (MessageError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)

    async def _add_interval_message(self, path: Path, kind: str) -> None:
        options = [(str(n), f"every {describe_every(seconds_every(every))}")
                   for n, every in enumerate(EVERY_TIMES, 1)] + [("T", "another time", "typed: 1h30m, 90s, 1d")]
        picked = await self.choose("How often?", options, back="Esc cancels")
        if picked is None:
            return
        if picked == "T":
            every = await self.prompt_line(f"  How often? (s, m, h, d: 20m, 1h30m; once every "
                                           f"{describe_every(MIN_SECONDS)} at most)")
            if not every:
                return
            seconds_every(every)  # a time that isn't one is said before the message is typed
        else:
            every = EVERY_TIMES[int(picked) - 1]
        text = await self.prompt_line("  The message, as players will read it")
        if not text or not text.strip():
            return
        message = add_message(path, every, text, kind)
        self.note(f"Added: every {describe_every(message.seconds)}. It's first said within "
                  f"{describe_every(message.seconds)}, while someone's on.", "green")

    # A server's page: its mods on or off ----------------------------------------------------------

    async def _server_mods(self, settings: Any, server: Any, row: Row) -> None:
        root = content_root_of(server.folder, settings.config_name)
        while True:
            mods = installed_mods(root)
            try:
                off = disabled_mods(root)
            except DisabledModsError as error:
                self.note(f"{error}. O opens it, to put it right by hand.", "red")
                await self.wait_any_key(8)
                return
            self.heading(server.name, "Settings", "Mods on or off",
                         note=f"{root / 'Mods'} · a mod switched off stays on disk but isn't used; the server takes "
                              "the change when it restarts")
            if not mods:
                self.note("No mods in its content folder yet: G → Workshop items and mods adds them.")
                await self.wait_any_key(6)
                return
            packages = [mod_package(root, mod) for mod in mods]
            rows = [("OFF" if is_disabled(off, p) else "on", mod.label) for mod, p in zip(mods, packages)]
            panel = self.listing(rows, colours=["gray" if is_disabled(off, p) else None for p in packages], wrap=True)
            answer = await self.prompt_line("  Which? (a number switches it on or off; Esc goes back)", panel=panel)
            if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(mods):
                return
            package = packages[int(answer) - 1]
            switch_off = not is_disabled(off, package)
            try:
                set_mod_disabled(root, package, switch_off)
            except (DisabledModsError, OSError) as error:
                self.note(f"Not changed: {error}", "yellow")
                await self.wait_any_key(6)
                continue
            self.note(f"{mods[int(answer) - 1].title} switched {'off' if switch_off else 'on'}.", "green")
            await self._offer_restart(row, server.name, _Change("mods", "restart"))

    async def _server_page_extra(self, settings: Any, server: Any, row: Row, key: str) -> None:
        """D's letters for what a server has besides its dedicated.toml settings."""
        if key == "B":
            await self._bans(settings, row)
            return
        if key == "M":
            await self._server_mods(settings, server, row)
            return
        if key == "I":
            await self._interval_messages(settings, server)
            return
        tool = await self._tool(settings)
        if tool is None:
            await self.wait_any_key(8)
            return
        if key == "G":
            await self._custom_games(settings, tool, server)
            return
        catalogue = await self._catalogue(tool, settings, server)
        if catalogue is None:
            await self.wait_any_key(8)
            return
        await self._playlists(settings, tool, server, catalogue)

    def server_page_extras(self, settings: Any, server: Any) -> list[tuple[str, str, str, str]]:
        """(letter, what, count, what it holds) for a server's page in D."""
        from ...games.halo3 import playlist_files

        root = content_root_of(server.folder, settings.config_name)
        playlists = playlist_files(server.folder)
        modes = root / "Game Modes"
        custom = sorted(modes.glob("*.json")) if modes.is_dir() else []
        try:
            off = len(disabled_mods(root))
        except DisabledModsError:
            off = 0
        mods = len(installed_mods(root))
        own = own_file(server.folder, settings.home)
        try:
            messages = read_messages(own)[0] if own is not None else []
        except (MessageError, OSError):
            messages = []
        return [("P", "playlists", str(len(playlists)), ", ".join(p.name.removesuffix(".playlist.json")
                                                                   for p in playlists[:4]) or "build one"),
                ("G", "custom games", str(len(custom)), "game types with rules of their own"),
                ("B", "bans, admins and the VPN block", "", "through its console, while it runs"),
                ("M", "mods on or off", str(mods), f"{off} switched off" if off else "every mod in use"),
                ("I", "interval messages", str(len(messages)), "its own; it says the ones for every server too"
                 if own is not None else "the ones for every server (its folder is Huragok's)")]

    # O: open the folder, or a file as it is -------------------------------------------------------

    async def screen_open(self) -> None:
        self.title("OPEN", "the folder, or a file in your editor (D changes them a step at a time, without "
                           "needing their format)")
        settings = self._settings()
        targets: list[tuple[str, str, Path | Any]] = [
            ("the folder", str(self.controller.home), self.controller.home),
            (settings.path.name, "how Huragok runs; applies when it starts again", settings.path),
            (COMMANDS_FILE, "who can use which in-game command; applies within seconds",
             self.controller.home / COMMANDS_FILE),
            (self._words_path(settings).name, "Vergil's word list (automod); applies within seconds",
             self._words_path(settings)),
            (self._names_path(settings).name, "Vergil's banned names; applies within seconds",
             self._names_path(settings)),
            (MESSAGES_FILE, "the interval messages every server says; applies within seconds",
             settings.home / MESSAGES_FILE),
        ]
        for server in settings.servers:
            targets.append((f"{server.name}'s files", f"{settings.config_name}, playlists… in {server.folder}", server))
        self.terminal.print()
        panel = self.listing([(name, what) for name, what, _ in targets])
        answer = await self.prompt_line("  Which? (number; Esc cancels)", panel=panel)
        if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(targets):
            return
        target = targets[int(answer) - 1][2]
        if target == self.controller.home:
            self.note(desktop.open_path(self.controller.home))
            await self.wait_any_key(3)
            return
        if isinstance(target, Path):
            self.note(desktop.open_editor(target))
            await self.wait_any_key(4)
            return
        await self._open_server_file(settings, target)

    async def _open_server_file(self, settings: Any, server: Any) -> None:
        self.heading(server.name, "Files", note=str(server.folder))
        files = self.server_files(settings, server)
        rows = [("its folder", "")] + [(name, what) for name, what, _ in files]
        panel = self.listing(rows)
        answer = await self.prompt_line("  Which? (number; Esc cancels)", panel=panel)
        if not answer or not answer.strip().isdecimal() or not 1 <= int(answer) <= len(rows):
            return
        if answer.strip() == "1":
            self.note(desktop.open_path(server.folder))
            await self.wait_any_key(3)
            return
        name, _, path = files[int(answer) - 2]
        self.note(desktop.open_editor(path))
        if name == settings.config_name or name.startswith(("playlists/", "Game Modes/")):
            self.note("Save it and the server re-checks its files a couple of seconds later; the result shows in the "
                      "dashboard's events ('settings reloaded …', or what's wrong).", "cyan")
        await self.wait_any_key(6)


class _Change:
    """What _offer_restart needs to know about a change that isn't a dedicated.toml setting."""

    def __init__(self, key: str, applies: str) -> None:
        self.key, self.applies = key, applies


def _who_and_how(note: str, player_id: str) -> tuple[str, str]:
    """A listed player's note, "Bravo: promoted by Alice on 2026-10-18", as (who, how); their ID's start when the
    note doesn't say who."""
    who, colon, how = note.partition(":")
    if colon and who.strip():
        return who.strip(), how.strip()
    return player_id[:8], note.strip()


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"seen {max(1, int(seconds // 60))} min ago"
    if seconds < 86400 * 2:
        return f"seen {int(seconds // 3600)} h ago"
    return f"seen {int(seconds // 86400)} days ago"


def _name_notes(path: Path) -> dict[int, str]:
    """names.txt: the note above each banned name ("banned 2026-10-18 by Alice: slur"), by the name's line."""
    from ...core.files import read_text

    try:
        lines = read_text(path, RuleError).splitlines()
    except (OSError, RuleError):
        return {}
    return {number: lines[number - 2].strip().removeprefix("# ") for number in range(2, len(lines) + 1)
            if lines[number - 2].strip().startswith("# banned ") and lines[number - 1].strip()
            and not lines[number - 1].strip().startswith("#")}


__all__ = ["FILES", "LADDERS", "FileScreens"]
