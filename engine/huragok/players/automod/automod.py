"""The automod part: strikes for listed words in chat, Vergil's mutes put back as players join, banned names
kicked as they join, and its commands (!strike, !strikes, !banword, !banname, !pardonplayer…). What a rule says is
rules.py's; finding its words however they're disguised, matcher.py's."""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...core.commandsfile import ROLES, all_commands
from ...core.events import Event
from ...core.files import mtime
from ...core.part import Part, command, every, on
from ...core.serversettings import automod_mode
from ...core.text import TIME, describe_seconds, seconds_of
from ...lore import VERGIL
from ...rcon import ban_list
from ...servers.state import Player
from ..outbox import ASK_SECONDS, results
from ..roles import Roles, a_role
from ..router import CommandCall, parse_command, unquote
from ..targets import PICK, Candidate, offer, outranked, plain, split
from ..who import acting_name, from_console
from .matcher import NAME_MATCHES, Hit, Matcher, normalise
from .rules import (
    FOREVER, NAME_STRIKE, PARTS, BannedName, LeftOut, Rule, RuleError, Step, add_name, change_rule, describe_unnamed,
    ladder, line_of, parse_line, read_names, read_rules_lenient, remove_name, unnamed_ladders,
)

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
    # A mute that came with a kick: how long the kick kept them out (the mute's expires counts from its end).
    """
    ALTER TABLE automod_strikes ADD COLUMN kick TEXT;
    """,
    # A moderator's !mute, kept by Vergil as its own are: the server forgets every mute when it restarts.
    """
    CREATE TABLE automod_mutes (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, server TEXT, player_id TEXT NOT NULL, player_name TEXT,
        by TEXT NOT NULL, reason TEXT, duration TEXT, expires REAL, lifted REAL, lifted_by TEXT);
    CREATE INDEX automod_mutes_player ON automod_mutes (player_id);
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
NAME_LABEL = "banned name"  # what players read for a name kicked, whichever list caught it
BANWORD_USE = ("Use: !banword <words> <line> adds them to a line of the word list, with its steps (!banword "
               "badword slurs); !banword <words> | <kicks> | <mutes> | <ban> | <name> makes a new line, e.g. "
               "!banword badword | 1h 1d | 2, 1d 7d | 5 | slurs.")
NEW_LINE_HELP = ("kick times | strikes before the mutes, then each mute's time | strikes before the ban | its name; "
                 "a part left blank is none")
NAME_RETRY = 60.0  # a name kick the server refused, or a mute that didn't show, is tried again after this long
KEPT_TWICE_SECONDS = 15.0  # a mute recorded from Huragok's !mute, then announced by the server: kept once
MUTE_MARGIN = 60.0  # a mute of Vergil's with less than this left isn't put back when the server's runs out
FORGET_AFTER = 3600.0  # what Vergil keeps about a player for moments (forget_the_past) goes after this long
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


def load_rules_lenient(config: dict[str, Any], home: Path) -> tuple[list[Rule], list[BannedName], list[Path],
                                                                   list[LeftOut]]:
    """The rules in the word list (the lines that read: one that doesn't is left out and said, instead of stopping
    them all), the banned names, and the files to watch for edits."""
    words, names = home / str(config["words"]), home / str(config["names"])
    rules, left = read_rules_lenient(words)
    return rules, read_names(names), [words, names], left


def left_out_text(left: LeftOut, file: str) -> str:
    return f"{file} {left.why}: Vergil runs without that line until it's fixed (the other lines are in force)"


def name_rules(names: list[BannedName]) -> list[Rule]:
    """Each banned name as a one-word rule, so a hit says which line caught it."""
    return [Rule(name=f"line{n.line}", label=NAME_LABEL, words=[n.text], line=n.line) for n in names
            if normalise(n.text.strip("*")).strip()]  # a name of nothing would catch everyone


def same_name(a: str, b: str) -> bool:
    def plain(text: str) -> str:
        return "".join(normalise(text.strip().strip("*")).split())
    return plain(a) == plain(b)


def check_name_match(value: Any) -> str:
    if value not in NAME_MATCHES:
        raise ValueError(f"name_match: '{value}' isn't one of {', '.join(NAME_MATCHES)} (anywhere: a listed word "
                         "anywhere in a name; words: only as a word of its own)")
    return str(value)


def _times(text: str) -> str:
    """Times typed in game, split by spaces or commas ("10m 30m 1h"), as automod.txt writes them ("10m, 30m, 1h")."""
    return ", ".join(text.replace(",", " ").split())


def _all_times(text: str) -> bool:
    return all(TIME.fullmatch(t.lower()) or t.lower() == FOREVER for t in text.replace(",", " ").split())


def _ladder_of(rule: Rule) -> tuple[Any, ...]:
    return (rule.kicks, rule.mute_after, rule.mutes, rule.ban_after)


def line_called(rules: list[Rule], text: str) -> Rule | None:
    """The line a name means: its name in any case, or that name with an s more or less (slur is slurs)."""
    name = text.strip().lower()
    by_name = {r.name: r for r in rules}
    if name in by_name:
        return by_name[name]
    return by_name.get(name + "s") or (by_name.get(name[:-1]) if name.endswith("s") else None)


def _short_ladder(rule: Rule) -> str:
    """What its first strikes do: "1st: removed for 1 hour; 2nd: muted for 1 day; 3rd: banned for good"."""
    steps = ladder(rule)
    text = "; ".join(steps[:3]) + ("…" if len(steps) > 3 else "")
    return text[:1].upper() + text[1:]


class AutoMod(Part):
    persona = VERGIL
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
        "name_match": "anywhere",  # how a listed word catches a name: anywhere in it, or as a word of its own
        "across_servers": True,  # mutes (and, in the commands part, kicks and bans) reach every server
        "cooldown_seconds": 10.0,  # lines this close together count as one strike
        "split_seconds": 10.0,     # a word split over lines sent this close together is caught
        "messages": {},
    }

    @classmethod
    def preflight(cls, config: dict[str, Any], home: Path) -> list[str]:
        config = {**cls.defaults, **config}
        check_messages(config["messages"])
        check_name_match(config["name_match"])
        if not isinstance(config["across_servers"], bool):
            raise ValueError("across_servers must be true or false")
        try:
            rules, _, _, left = load_rules_lenient(config, home)
        except (RuleError, OSError) as error:
            raise ValueError(str(error)) from None
        warnings = [left_out_text(item, str(config["words"])) for item in left]
        if not rules:
            warnings.append(f"{config['words']} has no words yet")
        old = [str(r.line) for r in rules if r.old_layout]
        if old:
            warnings.append(f"{config['words']} line{'s' if len(old) > 1 else ''} {', '.join(old)}: the 0.2 "
                            f"layout (words | timers | chances | name), read as before. The layout now has "
                            f"mutes: {PARTS}")
        warnings.extend(describe_unnamed(r, str(config["words"])) for r in unnamed_ladders(rules))
        warnings.extend(f"{config['words']} line {r.line}: {', '.join(r.zero_times())} is no time at all (a kick of 0m "
                        "is the server's plain kick, a mute of 0m kicks instead): write how long, like 10m, or perm"
                        for r in rules if r.zero_times())
        return warnings

    def check_config(self) -> None:
        self.messages = check_messages(self.config["messages"])
        self.name_match = check_name_match(self.config["name_match"])
        if not isinstance(self.config["across_servers"], bool):
            raise ValueError("across_servers must be true or false")
        if bad := set(self.config["exempt_roles"]) - set(ROLES):
            raise ValueError(f"exempt_roles: '{sorted(bad)[0]}' isn't a role")
        forget = self.config["forget_after_days"]
        if isinstance(forget, bool) or not isinstance(forget, (int, float)) or forget < 0:
            raise ValueError("forget_after_days must be 0 (never) or more")
        self.names_path = self.ctx.home / str(self.config["names"])
        self.mutes_restored: dict[str, tuple[float, float]] = {}  # player ID -> (when Vergil re-muted, their join)
        self._kept_lately: dict[str, tuple[float, str, float | None]] = {}  # player ID -> (when, by, expires)
        self._unread = ""  # a word list that couldn't be read, last said
        try:
            rules, names, paths, left = load_rules_lenient(self.config, self.ctx.home)
        except (RuleError, OSError) as error:
            raise ValueError(str(error)) from None
        self._install(rules, names, paths)
        self.left_out = left  # lines Vergil runs without; an edit leaving only these out still applies
        for item in left:
            self.ctx.log.error("%s", left_out_text(item, str(self.config["words"])))

    def _install(self, rules: list[Rule], names: list[BannedName], paths: list[Path]) -> None:
        self.rules = rules
        self.by_name = {r.name: r for r in rules}
        self.matcher = Matcher(rules)
        self.word_names = Matcher(rules, names=self.name_match)  # the same words, as they catch players' names
        self._install_names(names)
        self.watched = {path: mtime(path) for path in paths}

    def _install_names(self, names: list[BannedName]) -> None:
        self.names = names
        self.name_matcher = Matcher(name_rules(names))
        self.names_checked: set[tuple[str, str]] = set()  # (player ID, name) found fine by these lists
        if hasattr(self, "watched"):
            self.watched[self.names_path] = mtime(self.names_path)

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
        modes = {server: self.mode(server) for server in self.ctx.servers}
        self.log.info("%s rules, %s banned names%s", len(self.rules), len(self.names),
                      "".join(f" · {server}: {mode.replace('_', ' ')}" for server, mode in modes.items())
                      or (" · dry run" if self.ctx.dry_run else ""))
        self._warn_unnamed()

    @every(minutes=10)
    async def forget_the_past(self) -> None:
        """What Vergil keeps about each player only matters for moments (a line split over two within split_seconds,
        a strike's cooldown, the join a name kick or a put-back mute was for), but it was kept for every player ever
        seen, for as long as Huragok ran: a slow climb on a busy server (the simulation's soak, 26 October). Kept
        an hour, and names found fine only for players on now."""
        cutoff = time.time() - FORGET_AFTER
        for table, when in ((self.recent, lambda v: v[-1][0] if v else 0.0), (self.last_strike, lambda v: v),
                            (self.name_kicks, lambda v: v[1]), (self.mutes_restored, lambda v: v[0]),
                            (self._kept_lately, lambda v: v[0])):
            for player_id in [k for k, v in table.items() if when(v) < cutoff]:
                del table[player_id]
        on = {(p.id, p.name) for state in self.ctx.servers.values() for p in state.players.values()}
        self.names_checked &= on

    @every(seconds=5)
    async def reload_if_changed(self) -> None:
        if any(mtime(f) != m for f, m in self.watched.items()):
            try:
                rules, names, paths, left = load_rules_lenient(self.config, self.ctx.home)
                # A mistake the edit brings keeps the rules in force (a typo mustn't drop a line that worked); one
                # already left out (since the start, or an edit since) doesn't hold up an edit to the other lines.
                known = {item.text.strip() for item in getattr(self, "left_out", [])}
                if new := [item for item in left if item.text.strip() not in known]:
                    raise RuleError("; ".join(item.why for item in new))
                loaded = (rules, names, paths)
            except OSError as error:
                # An editor holding the file while it saves: the old rules stay, and the next pass tries again.
                problem = (f"{Path(error.filename or '').name or 'a word list'} can't be read right now "
                           f"({error.strerror or error}); keeping the old rules and trying again")
                if problem != self._unread:
                    self._unread = problem
                    self.log.warning("rules not reloaded yet: %s", problem)
            except RuleError as error:
                self._unread = ""
                self.watched = {f: mtime(f) for f in self.watched}  # don't repeat the error every 5 s
                self.log.error("rules not reloaded, keeping the old ones: %s", error)
            else:
                self._unread = ""
                self._install(*loaded)
                self.left_out = left
                self.log.info("rules reloaded: %s rules, %s banned names%s", len(self.rules), len(self.names),
                              f" ({len(left)} line{'' if len(left) == 1 else 's'} still left out)" if left else "")
                self._warn_unnamed()
        await self.check_names()

    # Each server's Vergil ---------------------------------------------------------------------

    def mode(self, server: str | None) -> str:
        """Vergil on this server: "on", "dry_run" (it watches, and only logs what it would do) or "off" (it
        doesn't watch its chat or names). The server's own [[server]] automod, else [automod] dry_run. Only Vergil's
        own calls follow it: a mute already decided (a moderator's, a strike's on a server where Vergil is on) is put
        back wherever the player joins, as `across_servers` says."""
        own = self.ctx.server_option(server, "automod") if server else None
        return automod_mode(own, self.ctx.dry_run, self.ctx.everything_dry)

    def _dry(self, server: str | None) -> bool:
        return self.mode(server) == "dry_run"

    def _send(self, server: str | None) -> dict[str, bool]:
        """How an action of Vergil's on this server goes: sent (live), or only recorded (dry run)."""
        dry = self._dry(server)
        return {"dry_run": dry, "live": not dry}

    @property
    def across(self) -> bool:
        return bool(self.config["across_servers"])

    # Watching -------------------------------------------------------------------------

    def _exempt(self, server: str, player: Player) -> bool:
        return self.ctx.highest_role(player) in self.config["exempt_roles"]  # their rank on any server

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
        if self.mode(server) == "off":
            return
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
        if self.mode(server) != "off" and (hit := self.name_hit(player.name)):
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
            # Through the console (!unmute, the relay, the window): Huragok's, so every server's. The game's own
            # (a /unmute, when it has one) is that server's alone: Vergil's list stays.
            if from_console(by, self.ctx.servers, event.server):
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
    async def unmuted_through_console(self, event: Event) -> None:
        """An !unmute (or another part's unmute) ends Vergil's mute too, whatever the server answered: for a
        player who left, or a mute the server had forgotten, there may be no unmute event to see. A moderator's
        !mute that the server took is kept (`_keep_mute`)."""
        if event.get("dry_run") or not event.get("target_id"):
            return
        if event.get("kind") == "mute" and event.get("ok") and event.get("source") != self.ctx.name:
            self._keep_mute(event)
            return
        if event.get("kind") != "unmute":
            return
        self._lift(event.server, str(event["target_id"]).lower(), str(event.get("target_name") or ""),
                   str(event.get("by") or "someone"))

    def _keep_mute(self, event: Event) -> None:
        """A moderator's !mute on Vergil's list, so it lasts its time: the server forgets every mute when it
        restarts (a crash, the daily restart, the watchdog's), and Vergil mutes them again as they join, as it
        does its own. A mute with no time is kept until someone unmutes them. The newer replaces an older one.
        A !mute for someone who isn't on (`pending`) starts as they join; its time counts from now."""
        duration = str(event.get("duration") or "") or None
        try:
            seconds = seconds_of(duration.lower()) if duration else None
        except (AttributeError, KeyError):
            return  # not a time Vergil can count (the server took it): it lasts until the server restarts
        self._keep(event.server, str(event["target_id"]).lower(), str(event.get("target_name") or ""),
                   str(event.get("by") or "someone"), str(event.get("reason") or ""), duration, seconds)

    def _keep(self, server: str | None, player_id: str, name: str, by: str, reason: str, duration: str | None,
              seconds: float | None) -> None:
        now = time.time()
        expires = now + seconds if seconds is not None else None
        last = self._kept_lately.get(player_id)
        if (last and now - last[0] < KEPT_TWICE_SECONDS and last[1] == acting_name(by)
                and (last[2] is None) == (expires is None) and abs((last[2] or 0) - (expires or 0)) < KEPT_TWICE_SECONDS):
            return  # the same mute seen twice: Huragok's !mute, then the server announcing it
        self._kept_lately[player_id] = (now, acting_name(by), expires)
        self.ctx.db.execute("UPDATE automod_mutes SET lifted = ?, lifted_by = ? WHERE player_id = ? AND lifted IS NULL",
                            (now, "a newer mute", player_id))
        self.ctx.db.execute(
            "INSERT INTO automod_mutes (time, server, player_id, player_name, by, reason, duration, expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (now, server, player_id, name, acting_name(by), reason, duration, expires))
        self.log.info("[%s] %s's mute by %s (%s) is on Vergil's list: muted again as they join until %s",
                      server, name or player_id[:8], by, duration or "no time",
                      "it's up" if duration else "someone unmutes them")

    @on("rcon.mute")
    async def muted_by_someone(self, event: Event) -> None:
        """A mute sent through the console some other way than Huragok's own !mute (the relay, the window's console)
        is kept as a !mute is. Vergil's own (its strikes, and putting mutes back) are on its list already. Only what
        came through the console ("Alice (RCON)"): the game's own / commands stay on their server (the game has no
        mute today, but if it gets one, it's that server's)."""
        player_id = str(event.get("id") or "").lower()
        by = str(event.get("by") or "").strip()
        if not player_id or not by or acting_name(by) == self.ctx.acts_as:
            return
        if not from_console(by, self.ctx.servers, event.server):
            return
        expires = event.get("expires")
        seconds: float | None = None
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            expires = expires / 1000 if expires > 1e11 else expires  # milliseconds, from a server that writes them
            seconds = expires - time.time() if expires > 1e9 else float(expires)  # a time, or seconds from now
            if seconds <= 0:
                return
        self._keep(event.server, player_id, str(event.get("name") or ""), by, str(event.get("reason") or ""),
                   f"{round(seconds)}s" if seconds is not None else None, seconds)

    def _lift(self, server: str | None, player_id: str, name: str, by: str) -> None:
        now = time.time()
        lifted = self.ctx.db.execute(
            "UPDATE automod_strikes SET lifted = ?, lifted_by = ? WHERE player_id = ? AND action = 'mute' "
            "AND lifted IS NULL AND pardoned IS NULL AND (expires IS NULL OR expires > ?)",
            (now, by, player_id, now)).rowcount
        kept = self.ctx.db.execute(
            "UPDATE automod_mutes SET lifted = ?, lifted_by = ? WHERE player_id = ? AND lifted IS NULL "
            "AND (expires IS NULL OR expires > ?)", (now, by, player_id, now)).rowcount
        if lifted:
            self.log.info("[%s] %s was unmuted by %s: Vergil won't mute them again for it (the strike still counts)",
                          server, name or player_id[:8], by)
        elif kept:
            self.log.info("[%s] %s was unmuted by %s: Vergil won't mute them again", server, name or player_id[:8], by)

    # Names ---------------------------------------------------------------------------

    def name_hit(self, name: str) -> Hit | None:
        """What a name is caught by: a banned name, or (with check_names) a word on the list, found as name_match
        says (anywhere in the name, or as a word of its own)."""
        hits = self.name_matcher.find(name) or (self.word_names.find(name) if self.config["check_names"] else [])
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
                    if last and last[0] == player.name and (self._dry(server) or now - last[1] < NAME_RETRY):
                        continue  # kicked for it a moment ago: the server is taking them out
                    if self._exempt(server, player):
                        self.names_checked.add(key)
                        continue
                    if self.mode(server) != "off" and (hit := self.name_hit(player.name)):
                        # Each server's queue, one waiting per player: this runs every few seconds, and a frozen
                        # server must hold up neither the others nor the automod's own time limit.
                        self.ctx.outbox.send(server, f"Vergil's name kick of {player.name}",
                                             lambda server=server, player=player, hit=hit: self.kick_name(
                                                 server, player, hit, evidence=f"name: {player.name}"),
                                             key=f"name kick {player.id}")
                        continue
                    self.names_checked.add(key)
                elif self._exempt(server, player):
                    continue
                # Their mute, apart from their name: the server may have lost it (a restart while the console
                # was away) or ended it (a moderator's shorter mute in its place) since the name was looked at.
                if not player.muted and self._mute_due(server, player, now):
                    self.ctx.outbox.send(server, f"Vergil's mute of {player.name}, put back",
                                         lambda server=server, player=player: self._mute_again(server, player),
                                         key=f"mute again {player.id}")

    def _mute_due(self, server: str, player: Player, now: float) -> bool:
        last = self.mutes_restored.get(player.id)
        if last and last[1] == player.joined and now - last[0] < NAME_RETRY:
            return False  # put back already; the server's mute event may not have come yet
        return self.active_mute(player.id, server) is not None

    async def kick_name(self, server: str, player: Player, hit: Hit, *, evidence: str,
                        by: str | None = None) -> bool:
        """A kick, and nothing else: no strike, no ban. They're let straight back in, to come back as
        someone else; with the same name, they're kicked again. Players read "banned name" whichever list caught
        it ({label} is the word list line's name, for a host's own message)."""
        self.name_kicks[player.id] = (player.name, time.time(), player.joined)
        actor = by or self.ctx.acts_as
        message = self._format("name", detail=NAME_LABEL, label=hit.rule.label or NAME_LABEL, action="kicked",
                               by=by or "")
        moderation = self.ctx.moderation
        action = await moderation.kick(server, player, message, by=actor, evidence=evidence, **self._send(server))
        if action.ok and not action.dry_run:
            await moderation.pardon(server, player.id, by=actor, live=True)  # the server's kick would keep them out
        caught = f"names.txt line {hit.rule.line}" if hit.rule.label == NAME_LABEL else f"automod.txt {hit.rule.name}"
        self.log.info("[%s] %s: banned name (%s%s) → kicked until they change it%s", server, player.name, caught,
                      f": {hit.word}" if hit.word and hit.rule.label != NAME_LABEL else "",
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

    def active_strikes(self, player_id: str, rule: Rule, *, dry: bool = False) -> int:
        """The strikes that still count on this line: a dry run's (`dry`) apart from real ones."""
        forget = float(self.config["forget_after_days"])
        since = time.time() - forget * 86400 if forget else 0
        row = self.ctx.db.query(
            "SELECT COUNT(*) AS n FROM automod_strikes WHERE player_id = ? AND grp = ? AND pardoned IS NULL "
            "AND time >= ? AND dry_run = ?", (player_id, rule.name, since, int(dry)))
        return int(row[0]["n"])

    def active_mute(self, player_id: str, server: str | None = None) -> dict[str, Any] | None:
        """The mute on Vergil's list still running for this player (the longest), or None: a strike's, or a
        moderator's !mute (`kept`). It's Vergil's list, not the server's: the server forgets its mutes when it
        restarts. With `server` and across_servers off, only one given there. A dry run's strikes never mute.
        {rule (the line, or "!mute by Alice: spam"), expires, number, kept}."""
        now = time.time()
        here = " AND server = ?" if server is not None and not self.across else ""
        found: list[dict[str, Any]] = []
        for row in self.ctx.db.query(
                "SELECT * FROM automod_strikes WHERE player_id = ? AND action = 'mute' AND pardoned IS NULL "
                f"AND lifted IS NULL AND dry_run = 0 AND (expires IS NULL OR expires > ?){here} "
                "ORDER BY expires IS NULL DESC, expires DESC LIMIT 1", (player_id, now, *([server] if here else []))):
            found.append({**{key: row[key] for key in row.keys()}, "kept": False})
        for row in self.ctx.db.query(
                "SELECT * FROM automod_mutes WHERE player_id = ? AND lifted IS NULL AND (expires IS NULL OR "
                f"expires > ?){here} ORDER BY expires IS NULL DESC, expires DESC LIMIT 1",
                (player_id, now, *([server] if here else []))):
            found.append({"rule": f"{self.ctx.permissions.prefix}mute by {row['by']}"
                                  + (f": {row['reason']}" if row["reason"] else ""),
                          "expires": row["expires"], "number": row["number"], "kept": True, "by": row["by"],
                          "reason": row["reason"]})
        return max(found, key=lambda m: float("inf") if m["expires"] is None else m["expires"], default=None)

    def _mute_left(self, row: Any) -> str:
        return "for good" if row["expires"] is None else describe_seconds(row["expires"] - time.time()) + " left"

    async def _mute_again(self, server: str, player: Player) -> None:
        row = self.active_mute(player.id, server)
        if not row:
            return
        rule = self.by_name.get(row["rule"])
        if row["kept"]:
            label = f"muted by {row['by']}" + (f": {row['reason']}" if row["reason"] else "")
        else:
            label = (rule.label if rule else "") or row["rule"]
        duration = None if row["expires"] is None else f"{max(1, round(row['expires'] - time.time()))}s"
        message = self._format("muted", detail=label, label=label, left=self._mute_left(row))
        self.mutes_restored[player.id] = (time.time(), player.joined)
        # A mute already decided (a moderator's, a real strike's) isn't Vergil's call to make again: it's put back
        # whatever Vergil is on this server.
        action = await self.ctx.moderation.mute(
            server, player, duration, message, live=True,
            evidence=f"still muted: {row['rule']}" if row["kept"] else
            f"still muted: strike {row['number']} ({row['rule']})")
        self.log.info("[%s] %s: still muted (%s), %s%s", server, player.name, row["rule"], self._mute_left(row),
                      " (dry run)" if action.dry_run else "" if action.ok else f" FAILED: {action.text}")

    async def _punish(self, server: str, player: Player, hits: list[Hit], *, source: str, evidence: str,
                      by: str | None = None) -> tuple[Rule, Step, int, bool]:
        """One strike, on the matched rule whose next step is the most severe."""
        choices = []
        for hit in hits:
            previous = self.active_strikes(player.id, hit.rule, dry=self._dry(server))
            choices.append((hit.rule, hit.rule.step_for(previous), previous))
        rule, step, previous = max(choices, key=lambda choice: choice[1].severity)
        return await self.strike(server, player, rule, source=source, evidence=evidence, by=by,
                                 step=step, previous=previous)

    async def strike(self, server: str, player: Player, rule: Rule, *, source: str, evidence: str,
                     by: str | None = None, step: Step | None = None,
                     previous: int | None = None) -> tuple[Rule, Step, int, bool]:
        previous = self.active_strikes(player.id, rule, dry=self._dry(server)) if previous is None else previous
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
        send = self._send(server)
        dry, ok, text, pending, away = send["dry_run"], True, "", False, False
        # Wherever they are now (a moderator's !strike may come from another server); only here with across_servers
        # off.
        places = self._online(player.id) if self.across else [(s, p) for s, p in self._online(player.id) if s == server]
        places = places or ([(server, player)] if player.number else [])
        places.sort(key=lambda place: place[0] != server)  # the server it was struck on first: acted on at once
        if step.action == "kick" and not step.duration:
            if places:
                where, who = places[0]
                action = await moderation.kick(where, who, message, by=actor, evidence=evidence, **send)
                dry, ok, text = action.dry_run, action.ok, action.text
                for where, who in places[1:]:  # each other server's queue: a frozen one holds up only itself
                    self.ctx.outbox.send(where, f"Vergil's kick of {who.name} (struck on {server})",
                                         lambda where=where, who=who: moderation.kick(where, who, message, by=actor,
                                                                                      evidence=evidence, **send))
            away = not places  # the server's kick needs them on: the strike still counts
            ok = ok and not away
        elif step.action == "mute" and not step.kick:
            # If they're on no server, it starts when they next join.
            if places:
                where, who = places[0]
                action = await moderation.mute(where, who, step.duration, message, by=actor, evidence=evidence,
                                               **send)
                dry, ok, text = action.dry_run, action.ok, action.text
                for where, who in places[1:]:
                    self.ctx.outbox.send(where, f"Vergil's mute of {who.name} (struck on {server})",
                                         lambda where=where, who=who: moderation.mute(
                                             where, who, step.duration, message, by=actor, evidence=evidence, **send))
            pending = not places
        else:  # a timed removal (a mute to follow it, perhaps), or a ban: the server's ban, for that long or for good
            where, who = places[0] if places else (server, player)
            if who.number and not dry:
                try:
                    await self.ctx.tell(where, who, message)  # they may read it before the ban lands
                except Exception:  # noqa: BLE001
                    pass
            # A mute that comes with a kick starts as they join again (Vergil's own mute list, kept by this row).
            action = await moderation.ban(where, who, step.kick or step.duration, message, by=actor, evidence=evidence,
                                          **send)
            dry, ok, text = action.dry_run, action.ok, action.text
        now = time.time()
        expires = now + step.kick_seconds + step.seconds if step.seconds is not None else None  # 0m: no mute left
        self.ctx.db.execute(
            "INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, "
            "action, duration, kick, expires, dry_run, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, server, player.id, player.name, rule.name, rule.name, source, evidence, actor, step.action,
             step.duration, step.kick, expires, int(dry), int(ok)))
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
        """(player ID, name) for someone on the server or who left (targets.find, scope "seen"): #number, @number, a
        player ID (or its first 8+ characters), a name; several with one name: the caller picks."""
        player = await call.find(ref, scope="seen")
        return (player.id, player.name) if player else None

    def _outranks(self, call: CommandCall, player: Player) -> bool:
        return call.role == "owner" or ROLES.index(self.ctx.highest_role(player)) < ROLES.index(call.role)

    def _strike_word(self, word: str) -> bool:
        """What comes right after the player in !strike: a line's name, or name."""
        return word.lower() in self.by_name or word.lower() == NAME_STRIKE

    def _pardon_word(self, word: str) -> bool:
        return word.lower() in self.by_name or word.lower() == "all"

    @command("strike", usage="<player> <rule | name> [note]",
             help="Give a strike on an automod rule, e.g. for something said in voice; 'name' bans their name",
             role="moderator")
    async def strike_command(self, call: CommandCall) -> None:
        # The player's name may have spaces ("!strike Nate Higgers name"): it ends where a name players use does.
        known = [*self.by_name, NAME_STRIKE]
        if self.mode(call.server) == "off":
            await call.reply("Vergil is off on this server (its own setting, in Huragok's window): no strikes here.")
            return
        if not call.text.strip():
            await call.reply(f"Use: {call.prefix}strike <player> <rule> [note]. Rules: " + ", ".join(self.by_name)
                             + f"; or {NAME_STRIKE}, to ban their name")
            return
        ref, rest = await split(call, follows=self._strike_word)
        if ref is None:
            return
        reason = rest[0].lower() if rest else ""
        if reason not in known:
            later = next((n for n, word in enumerate(rest) if word.lower() in known), None)
            if not ref or later is None:
                await call.reply(f"Use: {call.prefix}strike <player> <rule> [note]. Rules: " + ", ".join(self.by_name)
                                 + f"; or {NAME_STRIKE}, to ban their name")
                return
            # A rule further on: the words before it are the name, of someone nobody on has, or has been seen
            # with. find says so (or finds them after all).
            ref, rest = " ".join([ref, *rest[:later]]), rest[later:]
            reason = rest[0].lower()
        found = await self._resolve(call, ref)
        if not found:
            return
        player = self._player(call.server, *found)
        if why := outranked(call, player):
            await call.reply(why)
            return
        note = unquote(rest[1:])
        evidence = f"{call.player.name}: {note}" if note else call.player.name
        if reason == NAME_STRIKE:
            await self._strike_name(call, player, note, evidence)
            return
        rule = self.by_name[reason]
        _, step, number, ok = await self.strike(call.server, player, rule, source="manual", by=call.player.name,
                                                evidence=evidence)
        if self._dry(call.server):
            result = "dry run, not sent"
        elif step.action == "mute" and not step.kick and not self._online(player.id) and not player.number:
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
            for where, who in sorted(self._online(player.id), key=lambda place: place[0] != call.server):
                if where == call.server:
                    kicked += await self.kick_name(where, who, hit[0], evidence=f"name: {who.name} ({evidence})",
                                                   by=call.player.name)
                    continue
                self.ctx.outbox.send(where, f"Vergil's name kick of {who.name}",  # its queue: never held up here
                                     lambda where=where, who=who: self.kick_name(
                                         where, who, hit[0], evidence=f"name: {who.name} ({evidence})",
                                         by=call.player.name), key=f"name kick {who.id}")
                kicked += 1
        await self.check_names()  # anyone else on with it
        already = "was already banned" if listed else "is banned now"
        if self._dry(call.server):
            done = "dry run: nobody is kicked for it yet"
        elif kicked:
            done = "they're kicked, and will be every time they join with it"
        else:
            done = "they're kicked if they join with it"
        await call.reply(f"The name {name} {already}: {done}. No strike, no ban; !unbanname {name} lifts it.")

    @command("banname", usage="<player | name>",
             help="Ban a name: whoever uses it is kicked as they join, until they change it (names.txt)",
             role="admin")
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
                await call.reply(f"{player.name} is {a_role(self.ctx.highest_role(player))}; you can't ban "
                                 "their name.")
                return
        elif number:
            # A player's number who just left, most likely: banned as text, "9" would kick everyone with a 9 in
            # their name until someone took it off.
            await call.reply(f"Nobody is #{number[1]} on this server now ({call.prefix}players lists them). A name "
                             "that's only a number isn't banned: it would catch every name with that number in it.")
            return
        else:
            player = Player(0, text, "")
        await self._strike_name(call, player, "", call.player.name)

    @command("unbanname", usage="<name>", help="Take a name off the banned names, names.txt", role="admin")
    async def unbanname(self, call: CommandCall) -> None:
        name = call.text.strip()
        if not name:
            await call.reply(f"Use: {call.prefix}unbanname <name | @number from {call.prefix}bannames>. Banned names: "
                             + (", ".join(n.text for n in self.names[-8:]) or "none"))
            return
        if pick := PICK.fullmatch(name):
            got = call.picks.get(call.player.id, int(pick[1])) if call.picks is not None else "No list to pick from."
            if isinstance(got, str):
                await call.reply(got)
                return
            name = got.target or got.name
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
                caught = f"automod.txt's {hit.rule.name} line ({hit.word}): !unbanword {hit.word} takes it off"
            await call.reply(("Taken off, but it's" if removed else f"{name} isn't banned by itself: it's")
                             + f" still caught by {caught}.")
        elif removed:
            await call.reply(f"{name} can be used again.")
        else:
            await call.reply(f"{name} isn't a banned name.")

    # The word list from the game ------------------------------------------------------------

    @property
    def words_path(self) -> Path:
        return self.ctx.home / str(self.config["words"])

    def _read_words(self) -> list[Rule]:
        """The word list as the file is now (an edit made in the last few seconds included); none yet without one.
        Raises RuleError or OSError."""
        try:
            return read_rules_lenient(self.words_path)[0]  # a line left out doesn't hold up a change to the others
        except RuleError:
            if not self.words_path.exists():
                return []
            raise

    def _reload_words(self) -> None:
        """A change made from the game applies at once, not at the next look at the file."""
        rules, names, paths, left = load_rules_lenient(self.config, self.ctx.home)
        self._install(rules, names, paths)
        self.left_out = left

    def _lines(self) -> str:
        return ", ".join(r.name for r in self.rules) or "none yet"

    @command("banword", usage="<words> <line> | <words> | [kicks] | [mutes] | [ban] | <name>",
             help="Add words to a line of the word list (!banword badword slurs), or make a new line: words | kicks | "
                  "mutes | ban | name. Names with them are kicked too", role="admin")
    async def banword(self, call: CommandCall) -> None:
        fields = [f.strip() for f in call.text.strip().split("|")]
        if not fields[0]:
            await call.reply(f"{BANWORD_USE} Lines: {self._lines()}.")
            return
        if len(fields) > 5:
            await call.reply(f"At most 5 parts: {PARTS}.")
            return
        words_file = str(self.config["words"])
        try:
            rules = self._read_words()
        except (RuleError, OSError) as error:
            await call.reply(f"{words_file} can't be changed now ({error}); nothing was added.")
            return
        by_name = {r.name: r for r in rules}
        target: Rule | None = None  # the line the words join; None: a new line
        if len(fields) == 1:
            # !banword <words> <line>: the last word is the line the words join, with its steps (slur finds slurs).
            # A new line needs its steps, so it's written out in full, with bars.
            words_text, _, line_name = fields[0].rpartition(" ")
            if not words_text.strip():
                words_text, line_name = fields[0], ""
            entries = [w.strip() for w in words_text.split(",") if w.strip()]
            listed = Matcher(rules)
            if not line_name and entries and all(listed.find(w.strip("*")) for w in entries if not w.startswith("-")):
                pass  # already on the list: said below, nothing to ask
            elif not line_name or (target := line_called(rules, line_name)) is None:
                await call.reply(self._which_line(call, words_text.strip(), line_name))
                return
            fields = [words_text.strip()]
        else:
            entries = [w.strip() for w in fields[0].split(",") if w.strip()]
        # <words> | <a line's name>. A second part that starts with a digit and isn't a line is a kick time mistyped.
        second = fields[1] if len(fields) == 2 else ""
        shorthand = target is not None or line_called(rules, second) is not None or (
            bool(second) and not _all_times(second) and not second[0].isdigit())
        if shorthand and target is None:
            target = line_called(rules, second)
            if target is None:
                await call.reply(self._which_line(call, fields[0], second))
                return
        if shorthand:
            fields = [fields[0]]
        fields += [""] * (5 - len(fields))
        name = "-".join(fields[4].lower().split())  # "racist slurs" -> racist-slurs: one word
        try:
            # The words as a line of their own: refused as the file would refuse it (a word of nothing, a bad time).
            wanted = parse_line(" | ".join([", ".join(entries), _times(fields[1]), _times(fields[2]), fields[3],
                                            name]), 1)
        except RuleError as error:
            await call.reply(f"Not added: {str(error).removeprefix('line 1: ')}. {PARTS}")
            return
        assert wanted is not None  # five parts and a word: never a blank line
        if not shorthand and wanted.label and wanted.label in by_name:
            target = by_name[wanted.label]
            if any(fields[1:4]) and _ladder_of(target) != _ladder_of(wanted):
                await call.reply(f"There's a {target.name} line already ({_short_ladder(target)}). !banword <words> | "
                                 f"{target.name} adds words to it; or give this line another name.")
                return
        # Words the list catches already (on this line or another) aren't written twice.
        matcher = Matcher(rules)
        caught = {w: hits[0] for w in entries if not w.startswith("-") and (hits := matcher.find(w.strip("*")))}
        new = [w for w in wanted.words if w not in caught]
        allow = [w for w in wanted.allow if target is None or w not in target.allow]
        already = "; ".join(f"{w} is on the {h.rule.name} line" + (f" ({h.word})" if not same_name(h.word, w) else "")
                            for w, h in caught.items())
        if not new and not (target is not None and allow):
            await call.reply(f"Nothing added: {already}.")
            return
        try:
            if target is not None:
                changed = replace(target, words=[*target.words, *new], allow=[*target.allow, *allow])
                change_rule(self.words_path, target.line, line_of(changed))
                line = target.line
            else:
                rules = change_rule(self.words_path, None, line_of(replace(wanted, words=new)))
                line = rules[-1].line
            self._reload_words()
        except (RuleError, OSError) as error:
            self.log.error("[%s] %s couldn't add words to %s: %s", call.server, call.player.name, words_file, error)
            await call.reply(f"Not added: {words_file} {str(error).removeprefix(words_file + ' ')}.")
            return
        rule = next(r for r in self.rules if r.line == line)
        added = ", ".join([*new, *(f"-{w}" for w in allow)])
        self.log.info("[%s] %s added %s to %s line %s (%s)", call.server, call.player.name, added, words_file, line,
                      rule.name)
        self.ctx.emit("words_banned", call.server, words=new, allowed=allow, rule=rule.name, line=line,
                      by=call.player.name)
        names = ""
        if self.config["check_names"] and new:
            names = (" Names with " + ("it" if len(new) == 1 else "them") + " in them are kicked as they join, until "
                     "changed.")
        dry = (" Vergil is in dry run on this server: it only logs what it would do." if self._dry(call.server)
               else "")
        steps = _short_ladder(rule)
        await call.reply(f"Added to {words_file} line {line} ({rule.name}): {added}. {steps}"
                         + ("" if steps.endswith("…") else ".") + names
                         + (f" Already there: {already}." if already else "") + dry)
        await self.check_names()  # anyone on now whose name has it

    def _which_line(self, call: CommandCall, words: str, line: str) -> str:
        """No line by that name (or none named): which there are, and how to make one, the words written in."""
        p = call.prefix
        name = "-".join(line.lower().split()) or "slurs"
        make = f"{p}banword {words} | 1h 1d | 2, 1d 7d | 5 | {name} ({NEW_LINE_HELP})"
        if not self.rules:
            return f"The word list has no lines yet. Make the first one with its steps: {make}."
        start = f"There's no line called '{line}' yet." if line else f"Which line should {words} go on?"
        return (f"{start} Lines: {self._lines()}: {p}banword {words} {self.rules[0].name} adds it to one. Or make "
                f"the line with its steps: {make}.")

    @command("unbanword", usage="<word>", help="Take a word off automod.txt (a line left with no words goes too)",
             role="admin")
    async def unbanword(self, call: CommandCall) -> None:
        word = call.text.strip()
        if not word:
            await call.reply(f"Use: !unbanword <word>, as automod.txt lists it. Lines: {self._lines()}.")
            return
        words_file = str(self.config["words"])
        try:
            rules = self._read_words()
        except (RuleError, OSError) as error:
            await call.reply(f"{words_file} can't be changed now ({error}); nothing was taken off.")
            return
        listed = [(r, [w for w in r.words if same_name(w, word)]) for r in rules]
        listed = [(r, entries) for r, entries in listed if entries]
        if not listed:
            hit = next(iter(Matcher(rules).find(word.strip("*"))), None)
            if hit:
                await call.reply(f"{word} isn't listed by itself: the {hit.rule.name} line's {hit.word} catches it. "
                                 f"!unbanword {hit.word} takes that off, or -{word} on that line lets it through.")
            else:
                await call.reply(f"{word} isn't on the word list.")
            return
        gone: list[str] = []
        try:
            for rule, entries in sorted(listed, key=lambda item: -item[0].line):  # the last first: lines don't move
                left = [w for w in rule.words if w not in entries]
                change_rule(self.words_path, rule.line, line_of(replace(rule, words=left)) if left else None)
                if not left:
                    gone.append(rule.name)
            self._reload_words()
        except (RuleError, OSError) as error:
            self.log.error("[%s] %s couldn't take %s off %s: %s", call.server, call.player.name, word, words_file,
                           error)
            await call.reply(f"Not taken off: {words_file} {str(error).removeprefix(words_file + ' ')}.")
            return
        off = ", ".join(f"{', '.join(entries)} ({rule.name})" for rule, entries in listed)
        self.log.info("[%s] %s took %s off %s%s", call.server, call.player.name, off, words_file,
                      f"; the line{'s' if len(gone) > 1 else ''} {', '.join(gone)} went with no words left" if gone
                      else "")
        self.ctx.emit("word_allowed", call.server, word=word, rules=[r.name for r, _ in listed], by=call.player.name)
        reply = f"Taken off {words_file}: {off}."
        if gone:
            reply += f" The {', '.join(gone)} line{'s' if len(gone) > 1 else ''} had no other words, so it went."
        if still := self.matcher.find(word.strip("*")):
            reply += f" It's still caught by the {still[0].rule.name} line's {still[0].word}."
        await call.reply(reply + " Strikes already given for it stay.")

    @command("rules", usage="[line | word]", help="The word list: each line's name, words and what its strikes do",
             role="moderator")
    async def rules_command(self, call: CommandCall) -> None:
        """Every line in short, or one line (by its name, or a word on it) in full. The words too: the answer is
        the caller's alone."""
        if not self.rules:
            await call.reply(f"The word list ({self.config['words']}) has no lines yet: {call.prefix}banword adds one.")
            return
        ask_for = call.text.strip()
        if ask_for:
            rule = self._line_named(ask_for)
            if rule is None:
                await call.reply(f"No line is called {ask_for} or lists it. Lines: {self._lines()}.")
                return
            words = ", ".join([*rule.words, *(f"-{w}" for w in rule.allow)])
            await call.reply("\n".join([f"{rule.name} (line {rule.line}): {words}", *ladder(rule)]))
            return
        lines = [f"{r.name}: {', '.join(r.words[:6])}{'…' if len(r.words) > 6 else ''}. {_short_ladder(r)}"
                 for r in self.rules]
        await call.reply("\n".join([*lines, f"{call.prefix}rules <line> shows one in full."]))

    def _line_named(self, text: str) -> Rule | None:
        """A line by its name, or the line that lists this word."""
        return self.by_name.get(text.lower()) or next(
            (r for r in self.rules if any(same_name(w, text) for w in r.words)), None)

    @command("bannames", usage="[text]", help="The banned names (names.txt); @numbers for !unbanname",
             role="moderator")
    async def bannames(self, call: CommandCall) -> None:
        names = [n for n in reversed(self.names) if plain(call.text) in plain(n.text)] if call.text.strip() else \
            list(reversed(self.names))
        if not names:
            await call.reply("No banned names" + (f" with '{call.text.strip()}' in them." if call.text.strip() else
                                                  f" in {self.config['names']}.") + " Names with a word from the word "
                             f"list in them are kicked too ({call.prefix}rules).")
            return
        entries = [Candidate("", n.text, target=n.text) for n in names]
        lines = await offer(call, entries[:15])
        more = [f"…and {len(names) - 15} more: {call.prefix}bannames <text> narrows it."] if len(names) > 15 else []
        await call.reply("\n".join([*lines, *more, f"{call.prefix}unbanname @number takes one off."]))

    @command("changebanword", usage="<line | word> | [kicks] | [mutes] | [ban]",
             help="New steps for a line of the word list: its kicks, mutes and ban, as !banword takes them",
             role="admin")
    async def changebanword(self, call: CommandCall) -> None:
        """A line's ladder replaced, its words and name kept (its strikes are kept under its name). A part left blank
        is none: no kicks, never muted, never banned."""
        fields = [f.strip() for f in call.text.strip().split("|")]
        if not fields[0] or len(fields) < 2:
            await call.reply(f"Use: {call.prefix}changebanword <line | word> | <kicks> | <mutes> | <ban>, e.g. "
                             f"{call.prefix}changebanword slurs | 1h 1d | 2, 1d 7d | 5 (a part left blank: none). "
                             f"Lines: {self._lines()}.")
            return
        if len(fields) > 4:
            await call.reply("The name stays (strikes are kept under it): give the line, then kicks | mutes | ban.")
            return
        words_file = str(self.config["words"])
        try:
            rules = self._read_words()
        except (RuleError, OSError) as error:
            await call.reply(f"{words_file} can't be changed now ({error}); nothing was changed.")
            return
        by_name = {r.name: r for r in rules}
        rule = by_name.get(fields[0].lower()) or next(
            (r for r in rules if any(same_name(w, fields[0]) for w in r.words)), None)
        if rule is None:
            await call.reply(f"No line is called {fields[0]} or lists it. Lines: {', '.join(by_name) or 'none'}.")
            return
        fields += [""] * (4 - len(fields))
        try:
            new = parse_line(" | ".join([", ".join([*rule.words, *(f"-{w}" for w in rule.allow)]), _times(fields[1]),
                                         _times(fields[2]), fields[3], rule.label]), 1)
        except RuleError as error:
            await call.reply(f"Not changed: {str(error).removeprefix('line 1: ')}.")
            return
        assert new is not None
        if _ladder_of(new) == _ladder_of(rule):
            await call.reply(f"The {rule.name} line does that already: {_short_ladder(rule)}")
            return
        before = _short_ladder(rule)
        try:
            change_rule(self.words_path, rule.line, line_of(new))
            self._reload_words()
        except (RuleError, OSError) as error:
            await call.reply(f"Not changed: {words_file} {str(error).removeprefix(words_file + ' ')}.")
            return
        self.log.info("[%s] %s changed the %s line's steps: %s → %s", call.server, call.player.name, rule.name,
                      " | ".join(fields[1:]), line_of(new))
        self.ctx.emit("rule_changed", call.server, rule=rule.name, line=rule.line, by=call.player.name)
        changed = self.by_name.get(rule.name)
        await call.reply(f"The {rule.name} line now: " + "; ".join(ladder(changed or new)) + f". Before: {before} "
                         "Strikes already given count toward the new steps.")

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
            count = self.active_strikes(player_id, rule, dry=self._dry(call.server))
            if count:
                lines.append(f"{rule.name}: {rule.strike_text(count)}, next: {rule.step_for(count).describe()}")
        mute = self.active_mute(player_id)
        muted = f" Muted ({mute['rule']}): {self._mute_left(mute)}." if mute else ""
        await call.reply(f"{name}: " + ("; ".join(lines) if lines else "no strikes.") + muted)

    @command("mutes", help="Who's muted: Vergil's list (strikes, moderators' mutes) and the server's; @numbers for "
             "!unmute", role="moderator")
    async def mutes(self, call: CommandCall) -> None:
        now = time.time()
        found: dict[str, Candidate] = {}
        for player_id in {r["player_id"] for r in self.ctx.db.query(
                "SELECT player_id FROM automod_strikes WHERE action = 'mute' AND pardoned IS NULL AND lifted IS NULL "
                "AND dry_run = 0 AND (expires IS NULL OR expires > ?) UNION SELECT player_id FROM automod_mutes "
                "WHERE lifted IS NULL AND (expires IS NULL OR expires > ?)", (now, now))}:
            mute = self.active_mute(player_id, call.server)
            if mute is None:
                continue
            on = call.state.player_by_id(player_id)
            record = self.ctx.players.get(player_id)
            name = on.name if on else record.last_name if record else str(mute.get("player_name") or player_id[:8])
            found[player_id] = Candidate(player_id, name, on.number if on else 0, since=on.joined if on else None,
                                         note=f"{mute['rule']}, {self._mute_left(mute)}")
        for player in call.state.players.values():
            if player.muted and player.id not in found:
                found[player.id] = Candidate(player.id, player.name, player.number, since=player.joined,
                                             note="muted by the server only: it forgets at its restart")
        if not found:
            await call.reply("Nobody is muted (on Vergil's list, or on this server).")
            return
        entries = sorted(found.values(), key=lambda c: (not c.number, c.name.casefold()))
        lines = await offer(call, entries[:10])
        more = [f"…and {len(entries) - 10} more."] if len(entries) > 10 else []
        await call.reply("\n".join([*lines, *more, f"{call.prefix}unmute @number lifts one."]))

    @command("pardonplayer", usage="<player | player ID> [rule | all] [reason]",
             help="Clear someone's automod strikes and lift what automod did to them", role="admin", player="seen",
             follows="_pardon_word")
    async def pardonplayer(self, call: CommandCall) -> None:
        assert call.target is not None
        player_id, name, rest = call.target.id, call.target.name, call.rest
        only = None
        if rest and rest[0].lower() in self.by_name:
            only = rest[0].lower()
            rest = rest[1:]
        elif rest and rest[0].lower() == "all":
            rest = rest[1:]
        now = time.time()
        where = "player_id = ? AND pardoned IS NULL"
        params: list[Any] = [player_id]
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
        real = [r for r in rows if not r["dry_run"]]  # a dry run's did nothing to lift
        live = [r for r in real if r["ok"]]
        # Bans automod gave that may still be in force: timed out ones too (the server may have run them).
        banned = [r for r in real if (r["action"] == "ban" and not r["duration"])
                  or (r["action"] in ("ban", "kick") and r["duration"] and (r["expires"] or 0) > now)
                  or (r["kick"] and r["time"] + seconds_of(r["kick"]) > now)]
        if banned:
            lifted.append(await self._lift_bans(call, player_id, name, banned))
        if any(r["action"] == "mute" and r["lifted"] is None and (r["expires"] is None or r["expires"] > now)
               for r in real) and not self.active_mute(player_id):
            # On the servers they're on; elsewhere, Vergil just won't mute them again.
            places = [where for where, _ in self._online(player_id)] or [call.server]
            lifted.append(await self._each_server(places, "unmute", "unmuted", lambda where: self.ctx.moderation.unmute(
                where, player_id, by=call.player.name, live=True)))
        kicks = [r for r in live if r["action"] == "kick" and not r["duration"] and now - r["time"] < 7200]
        if kicks:  # where Vergil kicked them (its pardon there lets them back on every server: across.py)
            lifted.append(await self._each_server(
                sorted({r["server"] or call.server for r in kicks}), "kick pardon", "can rejoin now",
                lambda where: self.ctx.moderation.pardon(where, player_id, by=call.player.name, live=True)))
        lifted = [text for text in lifted if text]
        rules = sorted({r["rule"] for r in rows})
        reason = unquote(rest)
        self.log.info("[%s] %s pardoned %s: %s strikes (%s)%s", call.server, call.player.name, name, len(rows),
                      ", ".join(rules), f": {reason}" if reason else "")
        self.ctx.emit("pardon", call.server, player_id=player_id, player_name=name, strikes=len(rows),
                      rules=rules, by=call.player.name, reason=reason)
        summary = f"Pardoned {name}: {len(rows)} strike{'s' if len(rows) != 1 else ''} cleared ({', '.join(rules)})"
        await call.reply(summary + (f"; {', '.join(lifted)}." if lifted else "."))

    async def _each_server(self, servers: list[str], what: str, done: str,
                           act: Callable[[str], Awaitable[Any]]) -> str:
        """`act` on each server through its queue (a frozen one holds up only itself), waiting ASK_SECONDS at most
        for the answers: `done`, or what didn't go yet and where."""
        async def ok(where: str) -> bool:
            return bool((await act(where)).ok)

        jobs = {where: self.ctx.outbox.send(where, f"Vergil's {what}", lambda where=where: ok(where))
                for where in servers}
        went, failed, waiting = await results(jobs, ASK_SECONDS)
        if not failed and not waiting:
            return done
        return "; ".join(text for text in (f"{what} failed on {', '.join(failed)}" if failed else "",
                                           f"{', '.join(waiting)} not answering yet" if waiting else "") if text)

    async def _lift_bans(self, call: CommandCall, player_id: str, name: str, strikes: list[Any]) -> str:
        """Lift the bans these strikes gave, on the server each was given on (from there Huragok's list lifts it on
        every server: an unban through the console is Huragok's)."""
        said = [await self._lift_bans_on(call, server, player_id, name,
                                         [r for r in strikes if (r["server"] or call.server) == server])
                for server in sorted({r["server"] or call.server for r in strikes})]
        return "; ".join(text for text in said if text)

    async def _lift_bans_on(self, call: CommandCall, server: str, player_id: str, name: str,
                            strikes: list[Any]) -> str:
        """Lift the bans these strikes gave on `server`, and nothing else. The server's unban lifts every ban on an
        ID with all that's linked to it, so it's sent only when the server's list has no one else's ban on them."""
        moderation = self.ctx.moderation
        if server not in self.ctx.servers or not self.ctx.servers[server].connected:
            return (f"ban not lifted: {server} isn't connected, so its ban list can't be read; !unban <their player "
                    "ID> there lifts it")
        data, why = await ban_list(self.ctx.rcon(server), strict=True)
        if data is None:
            return f"ban not lifted: the ban list couldn't be read ({why}); !unban <their player ID> lifts it"
        bans = [e for e in data["players"] if str(e.get("id") or "").lower() == player_id]
        ours = [e for e in bans if self._gave(e, strikes)]
        theirs = [e for e in bans if not self._gave(e, strikes)]
        if theirs:
            who = ", ".join(sorted({str(e.get("banned_by") or "someone").removesuffix(" (RCON)") for e in theirs}))
            why = next((f" ({e['reason']})" for e in theirs if e.get("reason")), "")
            self.log.info("[%s] %s's ban by %s kept: !pardonplayer only lifts automod's", server, name, who)
            if ours:
                return (f"ban not lifted: {who} banned them too{why}, and the server's unban lifts every ban on "
                        "them; !unban lifts both")
            return f"{who}'s ban stays{why}: it isn't automod's; !unban lifts it"
        if not ours:
            return ""  # ran out, or lifted already
        action = await moderation.unban(server, player_id, by=call.player.name, live=True)
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
