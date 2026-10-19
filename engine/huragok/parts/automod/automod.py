from __future__ import annotations

import re
import time
from collections import deque
from pathlib import Path
from typing import Any

from ...chat import CommandCall, parse_command
from ...permissions import ROLES, Roles, a_role, all_commands
from ...events import Event
from ...part import Part, command, every, on
from ...rcon import RconError
from ...state import Player

from .matcher import Hit, Matcher, normalise
from .rules import (NAME_STRIKE, PARTS, BannedName, Rule, RuleError, Step, add_name, describe_seconds, describe_unnamed,
                    read_names, read_rules, remove_name, unnamed_ladders)

MIGRATIONS = [
    """
    CREATE TABLE automod_strikes (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, server TEXT, player_id TEXT NOT NULL,
        player_name TEXT, rule TEXT NOT NULL, grp TEXT NOT NULL, source TEXT NOT NULL, evidence TEXT,
        by TEXT NOT NULL, action TEXT NOT NULL, duration TEXT, expires REAL, dry_run INTEGER NOT NULL,
        ok INTEGER NOT NULL, pardoned REAL, pardoned_by TEXT);
    CREATE INDEX automod_strikes_player ON automod_strikes (player_id, grp, time);
    """,
    # A mute someone else lifted (!unmute): the strike still counts, the mute is over.
    """
    ALTER TABLE automod_strikes ADD COLUMN lifted REAL;
    ALTER TABLE automod_strikes ADD COLUMN lifted_by TEXT;
    """,
]

MESSAGES = {
    # What the player reads. {detail} is the line's name and the strike ("slurs, strike 2 of 3");
    # {label}, {strike}, {action}, {next} and {by} work too. The word itself is never repeated.
    "chat": "KEEP IT CLEAN! That isn't allowed here ({detail}): {action}.{next}",
    "name": "KEEP IT CLEAN! Change your name, it isn't allowed here ({detail}). Join again with another one.",
    "manual": "{by} gave you a strike ({detail}): {action}.{next}",
    "next": " Next time: {next}.",
    "muted": "KEEP IT CLEAN! You're still muted ({detail}): {left}.",  # on joining while a mute lasts
}
NAME_LABEL = "banned name"  # what players read for a name in names.txt
NAME_RETRY = 60.0  # a name kick the server refused, or a mute that didn't show, is tried again after this long
MUTE_MARGIN = 60.0  # a mute of Vergil's with less than this left isn't put back when the server's runs out
BAN_MATCH_SECONDS = 120.0  # a ban in the server's list this close to a strike of the same `by` is that strike's
PLACEHOLDERS = {"detail": "slurs, strike 1 of 3", "label": "slurs", "strike": "strike 1 of 3",
                "action": "removed for 1 hour", "next": " Next time: removed for 1 day.", "by": "Alice",
                "left": "2 days left"}


class _Blank(dict):  # type: ignore[type-arg]
    """For a host's own messages: a {placeholder} this one doesn't fill reads as nothing."""

    def __missing__(self, key: str) -> str:
        return ""


def check_messages(messages: Any) -> dict[str, str]:
    """[automod] messages over the defaults, each tried with every placeholder filled in: a stray { or }
    would otherwise fail every strike and every muted join."""
    if not isinstance(messages, dict):
        raise ValueError('messages must be a table, like messages = { chat = "…" }')
    if unknown := set(messages) - set(MESSAGES):
        raise ValueError(f"messages: unknown '{sorted(unknown)[0]}'; use {', '.join(MESSAGES)}")
    for key, text in messages.items():
        try:
            str(text).format_map(_Blank(PLACEHOLDERS))
        except Exception as error:  # noqa: BLE001 - str.format raises several kinds
            raise ValueError(f"messages: {key} can't be filled in ({error}): write {{{{ or }}}} for a brace; the "
                             f"placeholders are {', '.join('{' + p + '}' for p in PLACEHOLDERS)}") from None
    return {**MESSAGES, **{key: str(text) for key, text in messages.items()}}


def load_rules(config: dict[str, Any], home: Path) -> tuple[list[Rule], list[BannedName], list[Path]]:
    """The rules in the word list, the banned names, and the files to watch for edits."""
    words, names = home / str(config["words"]), home / str(config["names"])
    return read_rules(words), read_names(names), [words, names]


def name_rules(names: list[BannedName]) -> list[Rule]:
    """Each banned name as a one-word rule, so a hit says which line caught it."""
    return [Rule(name=f"line{n.line}", label=NAME_LABEL, words=[n.text], line=n.line) for n in names
            if normalise(n.text.strip("*")).strip()]  # a name of nothing would catch everyone


def same_name(a: str, b: str) -> bool:
    def plain(text: str) -> str:
        return "".join(normalise(text.strip().strip("*")).split())
    return plain(a) == plain(b)


class AutoMod(Part):
    name = "automod"
    description = "One word list: per line, its words, then kicks, mutes and a ban as strikes add up; banned names"
    acts_as = "Vergil"
    enabled_by_default = True
    defaults = {
        "name": "Vergil",
        "words": "automod.txt",
        "names": "names.txt",
        "forget_after_days": 0,    # strikes older than this stop counting; 0: never
        "exempt_roles": ["admin", "owner"],
        "check_names": True,
        "cooldown_seconds": 10.0,  # lines this close together count as one strike
        "split_seconds": 10.0,     # a word split over lines sent this close together is caught
        "messages": {},
    }

    @classmethod
    def preflight(cls, config: dict[str, Any], home: Path) -> list[str]:
        config = {**cls.defaults, **config}
        check_messages(config["messages"])
        try:
            rules, _, _ = load_rules(config, home)
        except (RuleError, OSError) as error:
            raise ValueError(str(error)) from None
        warnings = [] if rules else [f"{config['words']} has no words yet"]
        old = [str(r.line) for r in rules if r.old_layout]
        if old:
            warnings.append(f"{config['words']} line{'s' if len(old) > 1 else ''} {', '.join(old)}: the 0.2 "
                            f"layout (words | timers | chances | name), read as before. The layout now has "
                            f"mutes: {PARTS}")
        warnings.extend(describe_unnamed(r, str(config["words"])) for r in unnamed_ladders(rules))
        return warnings

    def check_config(self) -> None:
        self.messages = check_messages(self.config["messages"])
        if bad := set(self.config["exempt_roles"]) - set(ROLES):
            raise ValueError(f"exempt_roles: '{sorted(bad)[0]}' isn't a role")
        forget = self.config["forget_after_days"]
        if isinstance(forget, bool) or not isinstance(forget, (int, float)) or forget < 0:
            raise ValueError("forget_after_days must be 0 (never) or more")
        self.names_path = self.ctx.home / str(self.config["names"])
        self.mutes_restored: dict[str, tuple[float, float]] = {}  # player ID -> (when Vergil re-muted, their join)
        self._unread = ""  # a word list that couldn't be read, last said
        try:
            self._install(*load_rules(self.config, self.ctx.home))
        except (RuleError, OSError) as error:
            raise ValueError(str(error)) from None

    def _install(self, rules: list[Rule], names: list[BannedName], paths: list[Path]) -> None:
        self.rules = rules
        self.by_name = {r.name: r for r in rules}
        self.matcher = Matcher(rules)
        self._install_names(names)
        self.watched = {path: self._mtime(path) for path in paths}

    def _install_names(self, names: list[BannedName]) -> None:
        self.names = names
        self.name_matcher = Matcher(name_rules(names))
        self.names_checked: set[tuple[str, str]] = set()  # (player ID, name) found fine by these lists
        if hasattr(self, "watched"):
            self.watched[self.names_path] = self._mtime(self.names_path)

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _format(self, key: str, **values: Any) -> str:
        try:
            return self.messages[key].format_map(_Blank(values))
        except Exception as error:  # noqa: BLE001 - check_messages tried it; never let a message stop a strike
            self.log.warning("messages: %s can't be filled in (%s): players read the default", key, error)
            return MESSAGES[key].format_map(_Blank(values))

    def _warn_unnamed(self) -> None:
        for rule in unnamed_ladders(self.rules):
            self.log.warning("%s", describe_unnamed(rule, str(self.config["words"])))

    async def setup(self) -> None:
        self.ctx.db.migrate(MIGRATIONS)
        self.recent: dict[str, deque[tuple[float, str, str]]] = {}
        self.last_strike: dict[str, float] = {}
        self.name_kicks: dict[str, tuple[str, float, float]] = {}  # player ID -> (name, when, their join)
        self.log.info("%s rules, %s banned names%s", len(self.rules), len(self.names),
                      " · dry run" if self.ctx.dry_run else "")
        self._warn_unnamed()

    @every(seconds=5)
    async def reload_if_changed(self) -> None:
        if any(self._mtime(f) != m for f, m in self.watched.items()):
            try:
                loaded = load_rules(self.config, self.ctx.home)
            except OSError as error:
                # An editor holding the file while it saves: the old rules stay, and the next pass tries again.
                problem = (f"{Path(error.filename or '').name or 'a word list'} can't be read right now "
                           f"({error.strerror or error}); keeping the old rules and trying again")
                if problem != self._unread:
                    self._unread = problem
                    self.log.warning("rules not reloaded yet: %s", problem)
            except RuleError as error:
                self._unread = ""
                self.watched = {f: self._mtime(f) for f in self.watched}  # don't repeat the error every 5 s
                self.log.error("rules not reloaded, keeping the old ones: %s", error)
            else:
                self._unread = ""
                self._install(*loaded)
                self.log.info("rules reloaded: %s rules, %s banned names", len(self.rules), len(self.names))
                self._warn_unnamed()
        await self.check_names()

    # Watching -------------------------------------------------------------------------

    def _exempt(self, server: str, player: Player) -> bool:
        return self.ctx.role_of(server, player) in self.config["exempt_roles"]

    def _player(self, server: str, player_id: str, name: str) -> Player:
        return self.ctx.server(server).player_by_id(player_id) or Player(0, name, player_id)

    def _online(self, player_id: str) -> list[tuple[str, Player]]:
        """Each server this player is on now, with them as that server sees them."""
        return [(server, player) for server, state in list(self.ctx.servers.items())
                if (player := state.player_by_id(player_id))]

    def _staff_command(self, server: str, player: Player, text: str) -> bool:
        """A command line from a moderator or above that they may use: quoting a word in !strike or !mute
        isn't saying it. A player's !help <word> is still chat."""
        role = self.ctx.role_of(server, player)
        if not Roles.allows(role, "moderator"):
            return False
        permissions = self.ctx.permissions
        parsed = parse_command(text, permissions.prefix)
        if parsed is None:
            return False
        specs = all_commands()
        spec = specs.get(parsed[0]) or next((s for s in specs.values() if parsed[0] in s.aliases), None)
        return spec is not None and spec.name not in permissions.off and Roles.allows(role, permissions.role_for(spec))

    @on("rcon.chat")
    async def chat(self, event: Event) -> None:
        if event.get("channel") == "server" or not event.get("id"):
            return
        server, player_id, text = event.server or "", str(event["id"]).lower(), str(event.get("text", ""))
        player = self._player(server, player_id, str(event.get("name", "")))
        if self._exempt(server, player) or self._staff_command(server, player, text):
            return
        now = time.time()
        recent = self.recent.setdefault(player_id, deque(maxlen=3))
        hits, evidence = self.matcher.find(text), text
        if not hits:
            earlier = [(raw, normal) for when, raw, normal in recent if now - when <= self.config["split_seconds"]]
            if earlier:
                prefix = " ".join(normal for _, normal in earlier)
                hits = self.matcher.find(f"{prefix} {normalise(text)}", from_position=len(prefix) + 1)
                evidence = " / ".join([*(raw for raw, _ in earlier), text])
        recent.append((now, text, normalise(text)))
        if not hits:
            return
        recent.clear()
        if now - self.last_strike.get(player_id, 0) < self.config["cooldown_seconds"]:
            return  # a burst of lines: one strike
        await self._punish(server, player, hits, source="chat", evidence=evidence)

    @on("rcon.join")
    async def join(self, event: Event) -> None:
        if not event.get("id"):
            return
        server = event.server or ""
        player = self._player(server, str(event["id"]).lower(), str(event.get("name", "")))
        if self._exempt(server, player):
            return
        last = self.name_kicks.get(player.id)
        if last and last[2] == player.joined:
            return  # check_names() saw this join first, and kicked them for it
        if hit := self.name_hit(player.name):
            await self.kick_name(server, player, hit, evidence=f"name: {player.name}")
            return
        self.names_checked.add((player.id, player.name))
        await self._mute_again(server, player)

    @on("rcon.connected")
    async def reconnected(self, event: Event) -> None:
        """The server may have restarted while its console was away, with players back on and their mutes
        gone: everyone is looked at again on the next check, names and mutes."""
        self.names_checked.clear()
        self.mutes_restored.clear()

    @on("rcon.unmute")
    async def unmuted(self, event: Event) -> None:
        """Someone lifted a mute (!unmute, the console): Vergil won't put it back when they rejoin. A mute that
        ran out ("time") while Vergil's has longer to go (a moderator's shorter !mute replaced it) is put back."""
        by = str(event.get("by") or "")
        if not event.get("id") or not by:
            return
        player_id = str(event["id"]).lower()
        if by.lower() != "time":
            self._lift(event.server, player_id, str(event.get("name") or ""), by)
            return
        row = self.active_mute(player_id)
        player = self.ctx.servers[event.server].player_by_id(player_id) if event.server in self.ctx.servers else None
        if row is None or player is None or self._exempt(event.server or "", player):
            return
        if row["expires"] is not None and row["expires"] - time.time() < MUTE_MARGIN:
            return  # Vergil's own mute, ending as the server's did
        last = self.mutes_restored.get(player_id)
        if last and time.time() - last[0] < 5:
            return  # put back a moment ago; the server ending it again at once is no reason to loop
        await self._mute_again(event.server or "", player)

    @on("moderation.action")
    async def unmuted_through_huragok(self, event: Event) -> None:
        """An !unmute (or another part's unmute) ends Vergil's mute too, whatever the server answered: for a
        player who left, or a mute the server had forgotten, there may be no unmute event to see."""
        if event.get("kind") != "unmute" or event.get("dry_run") or not event.get("target_id"):
            return
        self._lift(event.server, str(event["target_id"]).lower(), str(event.get("target_name") or ""),
                   str(event.get("by") or "someone"))

    def _lift(self, server: str | None, player_id: str, name: str, by: str) -> None:
        now = time.time()
        lifted = self.ctx.db.execute(
            "UPDATE automod_strikes SET lifted = ?, lifted_by = ? WHERE player_id = ? AND action = 'mute' "
            "AND lifted IS NULL AND pardoned IS NULL AND dry_run = ? AND (expires IS NULL OR expires > ?)",
            (now, by, player_id, int(self.ctx.dry_run), now)).rowcount
        if lifted:
            self.log.info("[%s] %s was unmuted by %s: Vergil won't mute them again for it (the strike still counts)",
                          server, name or player_id[:8], by)

    # Names ---------------------------------------------------------------------------

    def name_hit(self, name: str) -> Hit | None:
        """What a name is caught by: a banned name, or (with check_names) a word on the list."""
        hits = self.name_matcher.find(name) or (self.matcher.find(name) if self.config["check_names"] else [])
        return hits[0] if hits else None

    async def check_names(self) -> None:
        """Everyone on now, against the lists as they are: a name banned while they play, or Huragok
        starting with them already on (their mutes too, if the server restarted while it was off)."""
        now = time.time()
        for server, state in list(self.ctx.servers.items()):
            for player in list(state.players.values()):
                key = (player.id, player.name)
                if key not in self.names_checked:
                    last = self.name_kicks.get(player.id)
                    if last and last[0] == player.name and (self.ctx.dry_run or now - last[1] < NAME_RETRY):
                        continue  # kicked for it a moment ago: the server is taking them out
                    if self._exempt(server, player):
                        self.names_checked.add(key)
                        continue
                    if hit := self.name_hit(player.name):
                        await self.kick_name(server, player, hit, evidence=f"name: {player.name}")
                        continue
                    self.names_checked.add(key)
                elif self._exempt(server, player):
                    continue
                # Their mute, apart from their name: the server may have lost it (a restart while the console
                # was away) or ended it (a moderator's shorter mute in its place) since the name was looked at.
                if not player.muted and self._mute_due(player, now):
                    await self._mute_again(server, player)

    def _mute_due(self, player: Player, now: float) -> bool:
        last = self.mutes_restored.get(player.id)
        if last and last[1] == player.joined and (self.ctx.dry_run or now - last[0] < NAME_RETRY):
            return False  # put back already; the server's mute event may not have come yet
        return self.active_mute(player.id) is not None

    async def kick_name(self, server: str, player: Player, hit: Hit, *, evidence: str,
                        by: str | None = None) -> bool:
        """A kick, and nothing else: no strike, no ban. They're let straight back in, to come back as
        someone else; with the same name, they're kicked again."""
        self.name_kicks[player.id] = (player.name, time.time(), player.joined)
        actor = by or self.ctx.acts_as
        detail = hit.rule.label or "listed word"
        message = self._format("name", detail=detail, label=detail, action="kicked", by=by or "")
        moderation = self.ctx.moderation
        action = await moderation.kick(server, player, message, by=actor, evidence=evidence)
        if action.ok and not action.dry_run:
            await moderation.pardon(server, player.id, by=actor)  # the server's kick would keep them out
        caught = f"names.txt line {hit.rule.line}" if hit.rule.label == NAME_LABEL else f"automod.txt {hit.rule.name}"
        self.log.info("[%s] %s: name not allowed (%s) → kicked until they change it%s", server, player.name, caught,
                      " (dry run)" if action.dry_run else "" if action.ok else f" FAILED: {action.text}")
        self.ctx.emit("name", server, player_id=player.id, player_name=player.name, caught_by=caught, by=actor,
                      dry_run=action.dry_run, ok=action.ok)
        return action.ok

    def _write_name(self, name: str, by: str, note: str) -> None:
        """Add a name to names.txt (written back as UTF-8, whatever it was saved as). Raises RuleError or
        OSError when the file can't be read or written."""
        add_name(self.names_path, name, by, note)
        self._install_names(read_names(self.names_path))

    # Striking ------------------------------------------------------------------------

    def active_strikes(self, player_id: str, rule: Rule) -> int:
        forget = float(self.config["forget_after_days"])
        since = time.time() - forget * 86400 if forget else 0
        row = self.ctx.db.query(
            "SELECT COUNT(*) AS n FROM automod_strikes WHERE player_id = ? AND grp = ? AND pardoned IS NULL "
            "AND time >= ? AND dry_run = ?", (player_id, rule.name, since, int(self.ctx.dry_run)))
        return int(row[0]["n"])

    def active_mute(self, player_id: str) -> Any:
        """The mute of Vergil's still running for this player (the longest), or None. It's Vergil's
        list, not the server's: the server forgets its mutes when it restarts."""
        rows = self.ctx.db.query(
            "SELECT * FROM automod_strikes WHERE player_id = ? AND action = 'mute' AND pardoned IS NULL "
            "AND lifted IS NULL AND dry_run = ? AND (expires IS NULL OR expires > ?) "
            "ORDER BY expires IS NULL DESC, expires DESC LIMIT 1", (player_id, int(self.ctx.dry_run), time.time()))
        return rows[0] if rows else None

    def _mute_left(self, row: Any) -> str:
        return "for good" if row["expires"] is None else describe_seconds(row["expires"] - time.time()) + " left"

    async def _mute_again(self, server: str, player: Player) -> None:
        row = self.active_mute(player.id)
        if not row:
            return
        rule = self.by_name.get(row["rule"])
        label = (rule.label if rule else "") or row["rule"]
        duration = None if row["expires"] is None else f"{max(1, round(row['expires'] - time.time()))}s"
        message = self._format("muted", detail=label, label=label, left=self._mute_left(row))
        self.mutes_restored[player.id] = (time.time(), player.joined)
        action = await self.ctx.moderation.mute(server, player, duration, message,
                                                evidence=f"still muted: strike {row['number']} ({row['rule']})")
        self.log.info("[%s] %s: still muted (%s), %s%s", server, player.name, row["rule"], self._mute_left(row),
                      " (dry run)" if action.dry_run else "" if action.ok else f" FAILED: {action.text}")

    async def _punish(self, server: str, player: Player, hits: list[Hit], *, source: str, evidence: str,
                      by: str | None = None) -> tuple[Rule, Step, int, bool]:
        """One strike, on the matched rule whose next step is the most severe."""
        choices = []
        for hit in hits:
            previous = self.active_strikes(player.id, hit.rule)
            choices.append((hit.rule, hit.rule.step_for(previous), previous))
        rule, step, previous = max(choices, key=lambda choice: choice[1].severity)
        return await self.strike(server, player, rule, source=source, evidence=evidence, by=by,
                                 step=step, previous=previous)

    async def strike(self, server: str, player: Player, rule: Rule, *, source: str, evidence: str,
                     by: str | None = None, step: Step | None = None,
                     previous: int | None = None) -> tuple[Rule, Step, int, bool]:
        previous = self.active_strikes(player.id, rule) if previous is None else previous
        step = step or rule.step_for(previous)
        number = previous + 1
        following = rule.step_for(number)
        next_text = "" if step.action == "ban" else self._format("next", next=following.describe())
        template = "manual" if by else ("name" if source == "name" else "chat")
        strike_text = rule.strike_text(number)
        detail = ", ".join(t for t in (rule.label, strike_text) if t)
        message = self._format(template, detail=detail, label=rule.label, strike=strike_text,
                               action=step.describe(), next=next_text, by=by or "")
        self.last_strike[player.id] = time.time()
        moderation = self.ctx.moderation
        actor = by or self.ctx.acts_as
        dry, ok, text, pending, away = self.ctx.dry_run, True, "", False, False
        # Wherever they are now (a moderator's !strike may come from another server).
        places = self._online(player.id) or ([(server, player)] if player.number else [])
        if step.action == "kick" and not step.duration:
            for where, who in places:
                action = await moderation.kick(where, who, message, by=actor, evidence=evidence)
                if where == places[0][0]:
                    dry, ok, text = action.dry_run, action.ok, action.text
            away = not places  # the server's kick needs them on: the strike still counts
            ok = ok and not away
        elif step.action == "mute":
            # If they're on no server, it starts when they next join.
            for where, who in places:
                action = await moderation.mute(where, who, step.duration, message, by=actor, evidence=evidence)
                if where == places[0][0]:
                    dry, ok, text = action.dry_run, action.ok, action.text
            pending = not places
        else:  # a timed removal, or a ban: the server's ban, for that long or for good
            where, who = places[0] if places else (server, player)
            if who.number and not self.ctx.dry_run:
                try:
                    await self.ctx.tell(where, who, message)  # they may read it before the ban lands
                except Exception:  # noqa: BLE001
                    pass
            action = await moderation.ban(where, who, step.duration, message, by=actor, evidence=evidence)
            dry, ok, text = action.dry_run, action.ok, action.text
        now = time.time()
        expires = now + step.seconds if step.seconds else None
        self.ctx.db.execute(
            "INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, "
            "action, duration, expires, dry_run, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, server, player.id, player.name, rule.name, rule.name, source, evidence, actor, step.action,
             step.duration, expires, int(dry), int(ok)))
        result = (" (dry run)" if dry else " (not on now: muted when they next join)" if pending
                  else " (not on now: nobody to kick; the strike counts)" if away else "" if ok else f" FAILED: {text}")
        self.log.info("[%s] %s: %s %s → %s%s", server, player.name, rule.name, rule.strike_text(number), step,
                      result)
        self.ctx.emit("strike", server, player_id=player.id, player_name=player.name, rule=rule.name,
                      strike=number, action=str(step), source=source, evidence=evidence, by=actor,
                      dry_run=dry, ok=ok)
        return rule, step, number, ok

    # Commands -------------------------------------------------------------------------

    async def _resolve(self, call: CommandCall, ref: str) -> tuple[str, str] | None:
        """(player ID, name) for someone on the server (#number, exact name), a player ID (or its first 8+
        characters), or a name seen before. A name's start alone picks nobody: when the one meant has left,
        it would land on someone else."""
        player = await call.find_target(ref, history=self.ctx.players, offline=True)
        return (player.id, player.name) if player else None

    def _outranks(self, call: CommandCall, player: Player) -> bool:
        return call.role == "owner" or ROLES.index(self.ctx.role_of(call.server, player)) < ROLES.index(call.role)

    @command("strike", usage="<player> <rule | name> [note]",
             help="Give a strike on an automod rule, e.g. for something said in voice; 'name' bans their name",
             role="moderator")
    async def strike_command(self, call: CommandCall) -> None:
        reason = call.args[1].lower() if len(call.args) >= 2 else ""
        if reason not in self.by_name and reason != NAME_STRIKE:
            await call.reply("Use: !strike <player> <rule> [note]. Rules: " + ", ".join(self.by_name)
                             + f"; or {NAME_STRIKE}, to ban their name")
            return
        found = await self._resolve(call, call.args[0])
        if not found:
            return
        player = self._player(call.server, *found)
        if not self._outranks(call, player):
            await call.reply(f"{player.name} is {a_role(self.ctx.role_of(call.server, player))}; you can't strike them.")
            return
        note = " ".join(call.args[2:])
        evidence = f"{call.player.name}: {note}" if note else call.player.name
        if reason == NAME_STRIKE:
            await self._strike_name(call, player, note, evidence)
            return
        rule = self.by_name[reason]
        _, step, number, ok = await self.strike(call.server, player, rule, source="manual", by=call.player.name,
                                                evidence=evidence)
        if self.ctx.dry_run:
            result = "dry run, not sent"
        elif step.action == "mute" and not self._online(player.id) and not player.number:
            result = "not on now: muted when they next join"
        elif step.action == "kick" and not step.duration and not self._online(player.id) and not player.number:
            result = "not on now, so nobody was kicked; the strike counts"
        else:
            result = "done" if ok else "the server refused it"
        await call.reply(f"{player.name}: {rule.name} {rule.strike_text(number)}, {step.describe()} ({result}).")

    async def _strike_name(self, call: CommandCall, player: Player, note: str, evidence: str) -> None:
        name = player.name.strip().strip("*").lstrip("#").strip()  # a leading # would make it a note
        if not normalise(name).strip():
            await call.reply(f"{player.name!r} has nothing in it to ban; kick them instead.")
            return
        listed = self.name_matcher.find(name)
        if not listed:
            try:
                self._write_name(name, call.player.name, note)
            except (RuleError, OSError) as error:
                self.log.error("[%s] %s couldn't ban the name %s: %s", call.server, call.player.name, name, error)
                await call.reply(f"The name {name} isn't banned: {self.config['names']} can't be changed ({error}).")
                return
            self.log.info("[%s] %s banned the name %s%s", call.server, call.player.name, name,
                          f": {note}" if note else "")
            self.ctx.emit("name_banned", call.server, text=name, player_id=player.id, by=call.player.name,
                          note=note)
        hit = self.name_matcher.find(name)
        kicked = 0
        if hit:
            for where, who in self._online(player.id):
                kicked += await self.kick_name(where, who, hit[0], evidence=f"name: {who.name} ({evidence})",
                                               by=call.player.name)
        await self.check_names()  # anyone else on with it
        already = "was already banned" if listed else "is banned now"
        if self.ctx.dry_run:
            done = "dry run: nobody is kicked for it yet"
        elif kicked:
            done = "they're kicked, and will be every time they join with it"
        else:
            done = "they're kicked if they join with it"
        await call.reply(f"The name {name} {already}: {done}. No strike, no ban; !unbanname {name} lifts it.")

    @command("banname", usage="<player | name>",
             help="Ban a name: whoever uses it is kicked as they join, until they change it (names.txt)",
             role="moderator")
    async def banname(self, call: CommandCall) -> None:
        text = call.text.strip()
        if not text:
            await call.reply("Use: !banname <#number | name>. Banned names: "
                             + (", ".join(n.text for n in self.names[-8:]) or "none"))
            return
        # A player on this server by #number (or the number alone) or exact name: their name, and they're kicked.
        # Anything else is banned as the name itself, for whoever joins with it.
        state = self.ctx.server(call.server)
        number = re.fullmatch(r"#?(\d+)", text)
        on = [state.players[int(number[1])]] if number and int(number[1]) in state.players else \
            [p for p in state.players.values() if p.name.casefold() == text.casefold()]
        if len(on) == 1:
            player = on[0]
            if not self._outranks(call, player):
                await call.reply(f"{player.name} is {a_role(self.ctx.role_of(call.server, player))}; you can't ban "
                                 "their name.")
                return
        else:
            player = Player(0, text, "")
        await self._strike_name(call, player, "", call.player.name)

    @command("unbanname", usage="<name>", help="Take a name off the banned names, names.txt", role="admin")
    async def unbanname(self, call: CommandCall) -> None:
        name = call.text.strip()
        if not name:
            await call.reply("Use: !unbanname <name>. Banned names: "
                             + (", ".join(n.text for n in self.names[-8:]) or "none"))
            return
        try:
            removed = remove_name(self.names_path, name, same_name)
            if removed:
                self._install_names(read_names(self.names_path))
        except (RuleError, OSError) as error:
            await call.reply(f"{self.config['names']} can't be changed ({error}); {name} is still banned.")
            return
        if removed:
            self.log.info("[%s] %s took the name %s off the banned names", call.server, call.player.name, name)
            self.ctx.emit("name_allowed", call.server, text=name, by=call.player.name)
        if hit := self.name_hit(name):
            if hit.rule.label == NAME_LABEL:
                caught = f"the banned name '{next(n.text for n in self.names if n.line == hit.rule.line)}'"
            else:
                caught = f"automod.txt's {hit.rule.name} line, so edit that file"
            await call.reply(("Taken off, but it's" if removed else f"{name} isn't banned by itself: it's")
                             + f" still caught by {caught}.")
        elif removed:
            await call.reply(f"{name} can be used again.")
        else:
            await call.reply(f"{name} isn't a banned name.")

    @command("strikes", usage="[player]", help="Your automod strikes (moderators: anyone's)")
    async def strikes(self, call: CommandCall) -> None:
        if call.args:
            if ROLES.index(call.role) < ROLES.index("moderator"):
                await call.reply("You can only see your own strikes: type !strikes")
                return
            found = await self._resolve(call, call.text)
            if not found:
                return
            player_id, name = found
        else:
            player_id, name = call.player.id, call.player.name
        lines = []
        for rule in self.rules:
            count = self.active_strikes(player_id, rule)
            if count:
                lines.append(f"{rule.name}: {rule.strike_text(count)}, next: {rule.step_for(count).describe()}")
        mute = self.active_mute(player_id)
        muted = f" Muted ({mute['rule']}): {self._mute_left(mute)}." if mute else ""
        await call.reply(f"{name}: " + ("; ".join(lines) if lines else "no strikes.") + muted)

    @command("pardonplayer", usage="<player | player ID> [rule | all] [reason]",
             help="Clear someone's automod strikes and lift what automod did to them", role="admin")
    async def pardonplayer(self, call: CommandCall) -> None:
        if not call.args:
            await call.reply("Use: !pardonplayer <player | player ID> [rule | all] [reason]")
            return
        found = await self._resolve(call, call.args[0])
        if not found:
            return
        player_id, name = found
        rest = call.args[1:]
        only = None
        if rest and rest[0].lower() in self.by_name:
            only = rest[0].lower()
            rest = rest[1:]
        elif rest and rest[0].lower() == "all":
            rest = rest[1:]
        now, dry = time.time(), int(self.ctx.dry_run)
        where = "player_id = ? AND pardoned IS NULL AND dry_run = ?"
        params: list[Any] = [player_id, dry]
        if only:
            where += " AND grp = ?"
            params.append(only)
        rows = self.ctx.db.query(f"SELECT * FROM automod_strikes WHERE {where}", params)
        if not rows:
            await call.reply(f"{name} has no automod strikes to pardon.")
            return
        self.ctx.db.execute(f"UPDATE automod_strikes SET pardoned = ?, pardoned_by = ? WHERE {where}",
                            [now, call.player.name, *params])
        lifted = []
        live = [r for r in rows if r["ok"]]
        # Bans automod gave that may still be in force: timed out ones too (the server may have run them).
        banned = [r for r in rows if (r["action"] == "ban" and not r["duration"])
                  or (r["action"] in ("ban", "kick") and r["duration"] and (r["expires"] or 0) > now)]
        if banned:
            lifted.append(await self._lift_bans(call, player_id, name, banned))
        if any(r["action"] == "mute" and r["lifted"] is None and (r["expires"] is None or r["expires"] > now)
               for r in rows) and not self.active_mute(player_id):
            # On the servers they're on; elsewhere, Vergil just won't mute them again.
            places = [where for where, _ in self._online(player_id)] or [call.server]
            results = [await self.ctx.moderation.unmute(where, player_id, by=call.player.name) for where in places]
            failed = [a for a in results if not a.ok]
            lifted.append("unmuted" if not failed else f"unmute failed: {failed[0].text}")
        if any(r["action"] == "kick" and not r["duration"] and now - r["time"] < 7200 for r in live):
            action = await self.ctx.moderation.pardon(call.server, player_id, by=call.player.name)
            lifted.append("can rejoin now" if action.ok else f"kick pardon failed: {action.text}")
        lifted = [text for text in lifted if text]
        rules = sorted({r["rule"] for r in rows})
        reason = " ".join(rest)
        self.log.info("[%s] %s pardoned %s: %s strikes (%s)%s", call.server, call.player.name, name, len(rows),
                      ", ".join(rules), f": {reason}" if reason else "")
        self.ctx.emit("pardon", call.server, player_id=player_id, player_name=name, strikes=len(rows),
                      rules=rules, by=call.player.name, reason=reason)
        summary = f"Pardoned {name}: {len(rows)} strike{'s' if len(rows) != 1 else ''} cleared ({', '.join(rules)})"
        await call.reply(summary + (f"; {', '.join(lifted)}." if lifted else "."))

    async def _lift_bans(self, call: CommandCall, player_id: str, name: str, strikes: list[Any]) -> str:
        """Lift the bans these strikes gave, and nothing else. The server's unban lifts every ban on an ID with
        all that's linked to it, so it's sent only when the server's list has no one else's ban on them."""
        moderation = self.ctx.moderation
        if self.ctx.dry_run:
            action = await moderation.unban(call.server, player_id, by=call.player.name)
            return "ban lifted" if action.ok else f"unban failed: {action.text}"
        try:
            reply = await self.ctx.rcon(call.server).command("bans")
        except RconError as error:
            return f"ban not lifted: the ban list couldn't be read ({error}); !unban <their player ID> lifts it"
        entries = reply.data.get("players") if reply.ok and isinstance(reply.data, dict) else None
        if not isinstance(entries, list):
            return f"ban not lifted: the ban list couldn't be read ({reply.text}); !unban <their player ID> lifts it"
        bans = [e for e in entries if isinstance(e, dict) and str(e.get("id") or "").lower() == player_id]
        ours = [e for e in bans if self._gave(e, strikes)]
        theirs = [e for e in bans if not self._gave(e, strikes)]
        if theirs:
            who = ", ".join(sorted({str(e.get("banned_by") or "someone").removesuffix(" (RCON)") for e in theirs}))
            why = next((f" ({e['reason']})" for e in theirs if e.get("reason")), "")
            self.log.info("[%s] %s's ban by %s kept: !pardonplayer only lifts automod's", call.server, name, who)
            if ours:
                return (f"ban not lifted: {who} banned them too{why}, and the server's unban lifts every ban on "
                        "them; !unban lifts both")
            return f"{who}'s ban stays{why}: it isn't automod's; !unban lifts it"
        if not ours:
            return ""  # ran out, or lifted already
        action = await moderation.unban(call.server, player_id, by=call.player.name)
        return "ban lifted" if action.ok else f"unban failed: {action.text}"

    @staticmethod
    def _gave(entry: dict[str, Any], strikes: list[Any]) -> bool:
        """Whether a ban in the server's list is one of these strikes': the same `by`, at the same moment."""
        by = str(entry.get("banned_by") or "")
        mine = [r for r in strikes if by in (r["by"], f"{r['by']} (RCON)")]
        at = entry.get("banned_at")
        if not mine or isinstance(at, bool) or not isinstance(at, (int, float)):
            return bool(mine)
        at = at / 1000 if at > 1e11 else float(at)  # milliseconds, from a server that writes them
        return any(abs(at - r["time"]) <= BAN_MATCH_SECONDS for r in mine)
