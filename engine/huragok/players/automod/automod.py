"""The automod part: strikes for listed words in chat, Vergil's mutes put back as players join, banned names
kicked as they join, and its commands (!strike, !strikes, !banword, !banname, !pardonplayer…). What a rule says is
rules.py's; finding its words however they're disguised, matcher.py's."""

from __future__ import annotations

import difflib
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ...core.commandsfile import PROBATION, ROLES, all_commands, rank_named
from ...core.events import Event
from ...core.files import mtime
from ...core.part import Part, command, every, on
from ...core.storage import ACTING_ID
from ...core.serversettings import automod_mode
from ...core.text import TIME, describe_seconds, seconds_of
from ... import lore
from ...lore import VERGIL
from ...rcon import RconError, ban_list
from ...servers.state import Player
from ..outbox import ASK_SECONDS, results
from ..roles import Roles, a_role
from ..router import CommandCall, parse_command, unquote, wrap
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
    # Names the staff allowed, asked about a word of a line that only reports: not asked about again. `plain` is the
    # name as same_name() compares names.
    """
    CREATE TABLE automod_names_allowed (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, name TEXT NOT NULL, plain TEXT NOT NULL, rule TEXT,
        by TEXT NOT NULL);
    CREATE INDEX automod_names_allowed_plain ON automod_names_allowed (plain);
    """,
    # A mute whose time starts when they're next on (after the server's own kick, or given while they were away):
    # `expires` is the end counted from the strike until they are.
    """
    ALTER TABLE automod_strikes ADD COLUMN waiting INTEGER NOT NULL DEFAULT 0;
    """,
]

MESSAGES = {
    # What the player reads, as one of the Superintendent's call-outs (lore.CALLOUTS): {notice} is the sign that
    # heads it, and it hardens with what Vergil did (a mute, a removal, a ban). {detail} is the line's name and the
    # strike ("slurs, strike 2 of 3"); {label}, {strike}, {action}, {next}, {by} and {player} work too. The word
    # itself is never repeated.
    "chat": "{notice} That isn't allowed here ({detail}): {action}.{next}",
    "name": "{notice} Change your name, it isn't allowed here ({detail}). Join again with another one.",
    "manual": "{notice} {by} gave you a strike ({detail}): {action}{note}.{next}",
    "next": " Next time: {next}.",
    "muted": "{notice} You're still muted ({detail}): {left}.",  # on joining while a mute lasts
    # What everyone on the server reads once a strike has been acted on (the server's own line says what was done:
    # "Bravo was muted by Vergil"); "" says nothing.
    "public": "{notice} {by} caught {player} ({detail}).",
}
NAME_LABEL = "banned name"  # what players read for a name kicked, whichever list caught it
TELLS_THE_STAFF = {"report", "calladmin"}
QUOTED_SECONDS = 60.0  # a player's report or call quoting a listed word goes unstruck once this often
BANWORD_USE = ("Use: !banword <words> <line> adds them to a line of the word list, with its steps (!banword "
               "badword slurs); !banword <words> | <mutes> | <kicks> | <ban> | <name> makes a new line, e.g. "
               "!banword badword | 1d 7d | 2, 1h 1d | 5 | slurs.")
NEW_LINE_HELP = ("mute times | strikes before the kicks, then each kick's time | strikes before the ban | its name; "
                 "a part left blank is none, and all three blank only reports, a ticket for the staff")
DISCORD_BANWORD_USE = ("Use: /banword words: badword listname: slurs adds words to a list, with its steps. With mutes, "
                       "kicks and bans it makes a new list: /banword words: badword mutes: 1d, 7d kicks: 2, 1h, 1d "
                       "bans: 5 listname: slurs. A new list with all three empty only reports: a ticket for the staff, "
                       "nothing in game.")
NAME_RETRY = 60.0  # a name kick the server refused, or a mute that didn't show, is tried again after this long
KEPT_TWICE_SECONDS = 15.0  # a mute recorded from Huragok's !mute, then announced by the server: kept once
MUTE_MARGIN = 60.0  # a mute of Vergil's with less than this left isn't put back when the server's runs out
FORGET_AFTER = 3600.0  # what Vergil keeps about a player for moments (forget_the_past) goes after this long
BAN_MATCH_SECONDS = 120.0  # a ban in the server's list this close to a strike of the same `by` is that strike's
PLACEHOLDERS = {"detail": "slurs, strike 1 of 3", "label": "slurs", "strike": "strike 1 of 3",
                "action": "kicked for 1 hour", "next": " Next time: kicked for 1 day.", "by": "Alice",
                "left": "2 days left", "notice": "KEEP IT CLEAN!", "player": "Bravo"}


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


def plain_name(text: str) -> str:
    """A name as same_name() compares it: what's left once matching has taken out what it ignores."""
    return "".join(normalise(text.strip().strip("*")).split())


def same_name(a: str, b: str) -> bool:
    return plain_name(a) == plain_name(b)


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
    return (rule.mutes, rule.mute_forever_after, rule.kick_after, rule.kicks, rule.ban_after)


def line_called(rules: list[Rule], text: str) -> Rule | None:
    """The line a name means: its name in any case, or that name with an s more or less (slur is slurs)."""
    name = text.strip().lower()
    by_name = {r.name: r for r in rules}
    if name in by_name:
        return by_name[name]
    return by_name.get(name + "s") or (by_name.get(name[:-1]) if name.endswith("s") else None)


def _discord(call: CommandCall) -> bool:
    """Typed in Discord, where a command's parts are boxes of their own (words:, mutes:…), not one line with bars."""
    return call.channel == "discord"


def _short_ladder(rule: Rule) -> str:
    """What its first strikes do: "1st: kicked for 1 hour; 2nd: muted for 1 day; 3rd: banned for good"."""
    steps = ladder(rule)
    text = "; ".join(steps[:3]) + ("…" if len(steps) > 3 else "")
    return text[:1].upper() + text[1:]


class AutoMod(Part):
    persona = VERGIL
    name = "automod"
    description = "One word list: per line, its words, then mutes, kicks and a ban as strikes add up; banned names"
    acts_as = "Vergil"
    enabled_by_default = True
    defaults = {
        "name": "Vergil",
        "words": "automod.txt",
        "names": "names.txt",
        "forget_after_days": 0,    # strikes older than this stop counting; 0: never
        "exempt_roles": ["admin", "faculty"],
        "check_names": True,
        "name_match": "anywhere",  # how a listed word catches a name: anywhere in it, or as a word of its own
        "moderators_kick_at_most": "1d",  # read by the commands part: a kick with a time from a moderator
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
        if bad := {rank_named(role) for role in self.config["exempt_roles"]} - set(ROLES):
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
        self.last_strike: dict[str, float] = {}  # lines this close together are one strike
        self.last_report: dict[str, float] = {}  # ...or one report, of a line that only reports: a strike isn't held up by it
        self.quoted: dict[str, float] = {}  # player ID -> when a report or call of theirs quoting a word went unstruck
        self.name_kicks: dict[str, tuple[str, float, float]] = {}  # player ID -> (name, when, their join)
        self.names_asked: dict[str, float] = {}  # a name the staff were asked about (plain_name) -> when
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
                            (self.last_report, lambda v: v), (self.quoted, lambda v: v),
                            (self.name_kicks, lambda v: v[1]), (self.mutes_restored, lambda v: v[0]),
                            (self._kept_lately, lambda v: v[0]), (self.names_asked, lambda v: v)):
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
        return self.ctx.highest_role(player) in {rank_named(role) for role in self.config["exempt_roles"]}  # their rank on any server

    def _player(self, server: str, player_id: str, name: str) -> Player:
        return self.ctx.server(server).player_by_id(player_id) or Player(0, name, player_id)

    def _online(self, player_id: str) -> list[tuple[str, Player]]:
        """Each server this player is on now, with them as that server sees them."""
        return [(server, player) for server, state in list(self.ctx.servers.items())
                if (player := state.player_by_id(player_id))]

    def _staff_command(self, server: str, player: Player, text: str) -> bool:
        """A command line quoting a word isn't saying it: from a moderator or above, one they may use, as in !strike or !mute; from a probationary moderator, what they can ask for. Telling the staff
        about it is _telling_the_staff's. A player's !help <word> is still chat."""
        permissions = self.ctx.permissions
        parsed = parse_command(text, permissions.prefix)
        if parsed is None:
            return False
        specs = all_commands()
        spec = specs.get(parsed[0]) or next((s for s in specs.values() if parsed[0] in s.aliases), None)
        if spec is None or spec.name in permissions.off:
            return False
        if spec.name in TELLS_THE_STAFF:
            return False  # anyone's: _telling_the_staff
        role = self.ctx.role_of(server, player)
        needed = permissions.role_for(spec)
        if role == PROBATION:
            return needed == "moderator"
        return Roles.allows(role, "moderator") and Roles.allows(role, needed)

    def _telling_the_staff(self, player: Player, text: str, now: float) -> bool:
        """A report or a call for an admin quoting a listed word: whoever's helping isn't struck for what they're reporting.
        Once a minute (QUOTED_SECONDS), and only one that says something: a line starting !report is still chat
        everyone reads, so it was a way round the word list, line after line, refused or naming nobody (audit 2026-11,
        5.3). With no Discord to tell, it's chat with a ! on it."""
        permissions = self.ctx.permissions
        parsed = parse_command(text, permissions.prefix)
        if parsed is None or not parsed[1]:
            return False
        specs = all_commands()
        spec = specs.get(parsed[0]) or next((s for s in specs.values() if parsed[0] in s.aliases), None)
        if spec is None or spec.name not in TELLS_THE_STAFF or spec.name in permissions.off:
            return False
        if not self.ctx.command_there(spec.name) or now - self.quoted.get(player.id, 0.0) < QUOTED_SECONDS:
            return False
        self.quoted[player.id] = now
        return True

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
        if self._telling_the_staff(player, text, now):
            return
        # A burst of lines is one strike, or one report: but a report's doesn't hold up a strike, or a word that only
        # reports would buy cooldown_seconds of free slurs (audit 2026-11, 5.4).
        timer = self.last_report if all(hit.rule.reports for hit in hits) else self.last_strike
        if now - timer.get(player_id, 0) < self.config["cooldown_seconds"]:
            return
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
        if player.name:  # 0.10's join names nobody yet: their name is looked at once it's known (named)
            if self.mode(server) != "off" and (hit := self.name_hit(player.name)):
                await self.kick_name(server, player, hit, evidence=f"name: {player.name}")
                return
            self.names_checked.add((player.id, player.name))
            if self.mode(server) != "off" and (hit := self.name_to_ask_about(player.name)):
                await self.ask_about_name(server, player, hit)
        await self._mute_again(server, player)

    @on("state.named")
    async def named(self, event: Event) -> None:
        """A player who joined with no name (0.10) has one now: looked at as everyone on is (check_names), at once
        rather than at the next look."""
        await self.check_names()

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
            "AND lifted IS NULL AND pardoned IS NULL AND (expires IS NULL OR expires > ? OR waiting = 1)",
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
        """What a name is kicked for: a banned name, or (with check_names) a word on the list, found as name_match
        says (anywhere in the name, or as a word of its own). Not a word of a line that only reports: the staff are
        asked about that one (name_to_ask_about)."""
        hits = self.name_matcher.find(name) or [hit for hit in (self.word_names.find(name) if self.config["check_names"]
                                                                else []) if not hit.rule.reports]
        return hits[0] if hits else None

    def name_to_ask_about(self, name: str) -> Hit | None:
        """A word of a line that only reports, in a name nothing kicks for: the staff are asked whether to ban the
        name or allow it. None once they've allowed it."""
        if not self.config["check_names"] or self.name_hit(name):
            return None
        hits = [hit for hit in self.word_names.find(name) if hit.rule.reports]
        if not hits or self.name_allowed(name):
            return None
        return hits[0]

    def name_allowed(self, name: str) -> bool:
        return bool(self.ctx.db.query("SELECT 1 FROM automod_names_allowed WHERE plain = ? LIMIT 1", (plain_name(name),)))

    def allowed_names(self) -> list[Any]:
        """The names the staff allowed, the latest first."""
        return self.ctx.db.query("SELECT * FROM automod_names_allowed ORDER BY number DESC")

    def allow_name(self, name: str, by: str, server: str | None = None) -> bool:
        """The staff's answer to a name Vergil asked about: it's fine, and isn't asked about again. Whether it's new
        (False: allowed already). A banned name stays banned: names.txt is what kicks."""
        if not plain_name(name) or self.name_allowed(name):
            return False
        hit = next((h for h in self.word_names.find(name) if h.rule.reports), None)
        self.ctx.db.execute("INSERT INTO automod_names_allowed (time, name, plain, rule, by) VALUES (?, ?, ?, ?, ?)",
                            (time.time(), name, plain_name(name), hit.rule.name if hit else None, by))
        self.log.info("[%s] %s allowed the name %s: Vergil won't ask about it again", server, by, name)
        self.ctx.emit("name_allowed", server, text=name, by=by, key=f"name:{plain_name(name)}", asked=True)
        return True

    async def ask_about_name(self, server: str, player: Player, hit: Hit) -> None:
        """A name with a word of a line that only reports: nothing done to them and nothing said to them. The staff
        decide: a ticket on Discord (ban the name, or allow it), and staff in that game are told, to them alone. Once
        an hour at most for one name: they come and go."""
        key = plain_name(player.name)
        now = time.time()
        if now - self.names_asked.get(key, 0.0) < FORGET_AFTER:
            return
        self.names_asked[key] = now
        rule = hit.rule
        self.log.info("[%s] %s: the name has a word of the %s line (%s), which only reports → the staff are asked",
                      server, player.name, rule.name, hit.word)
        self.ctx.emit("name_asked", server, player_id=player.id, player_name=player.name, rule=rule.name, word=hit.word,
                      key=f"name:{key}")
        p = self.ctx.permissions.prefix
        on_discord = " There's a ticket for it on Discord." if self.ctx.command_there("mods") else ""
        ban = f"{p}banname #{player.number}" if player.number else f"{p}banname {player.name}"
        lines = wrap(f"{VERGIL.name}: {player.name} joined with a name that has a word of the {rule.label or rule.name} "
                     f"list in it, which takes no action: nothing was done or said to them. {ban} bans the name."
                     f"{on_discord}")
        state = self.ctx.servers.get(server)
        for staff in list(state.players.values()) if state is not None else []:
            role = self.ctx.highest_role(staff)
            if staff.id != player.id and role in ROLES and ROLES.index(role) >= ROLES.index("moderator"):
                self.ctx.outbox.send(server, f"Vergil telling {staff.name} about {player.name}'s name",
                                     lambda staff=staff: self._tell_all(server, staff, lines))

    async def _tell_all(self, server: str, player: Player, lines: list[str]) -> None:
        for line in lines:
            await self.ctx.tell(server, player, line)

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
                    if self.mode(server) != "off" and (hit := self.name_to_ask_about(player.name)):
                        await self.ask_about_name(server, player, hit)  # a ticket and tells, each through a queue
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
        self._start_waiting(player)
        return self.active_mute(player.id, server) is not None

    def _start_waiting(self, player: Player) -> None:
        """A mute kept for when they're back, after the server's own kick or given while they were away, starts its time
        now they are: from the join (audit 2026-11, 5.10). Not for someone still listed from before the strike, whom
        the kick hasn't reached yet."""
        now = time.time()
        for row in self.ctx.db.query(
                "SELECT number, time, duration FROM automod_strikes WHERE player_id = ? AND action = 'mute' AND waiting = 1 "
                "AND pardoned IS NULL AND lifted IS NULL AND dry_run = 0", (player.id,)):
            if player.joined <= row["time"]:
                continue
            back = min(player.joined, now)
            expires = None if not row["duration"] else back + seconds_of(str(row["duration"]))
            self.ctx.db.execute("UPDATE automod_strikes SET waiting = 0, expires = ? WHERE number = ?",
                                (expires, row["number"]))

    async def kick_name(self, server: str, player: Player, hit: Hit, *, evidence: str,
                        by: str | None = None) -> bool:
        """A kick, and nothing else: no strike, no ban. They're let straight back in, to come back as
        someone else; with the same name, they're kicked again. Players read "banned name" whichever list caught
        it ({label} is the word list line's name, for a host's own message)."""
        self.name_kicks[player.id] = (player.name, time.time(), player.joined)
        actor = by or self.ctx.acts_as
        message = self._format("name", notice=lore.notice("name"), detail=NAME_LABEL,
                               label=hit.rule.label or NAME_LABEL, action="kicked", by=by or "", player=player.name)
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

    def active_mute(self, player_id: str, server: str | None = None, *, waiting: bool = False) -> dict[str, Any] | None:
        """The mute on Vergil's list still running for this player (the longest), or None: a strike's, or a
        moderator's !mute (`kept`). It's Vergil's list, not the server's: the server forgets its mutes when it
        restarts. With `server` and across_servers off, only one given there. A dry run's strikes never mute. With
        `waiting`, one waiting for them to be back too, however long they've been away (`waiting` in the row).
        {rule (the line, or "!mute by Alice: spam"), expires, number, kept}."""
        now = time.time()
        here = " AND server = ?" if server is not None and not self.across else ""
        found: list[dict[str, Any]] = []
        for row in self.ctx.db.query(
                "SELECT * FROM automod_strikes WHERE player_id = ? AND action = 'mute' AND pardoned IS NULL "
                f"AND lifted IS NULL AND dry_run = 0 AND (expires IS NULL OR expires > ?{' OR waiting = 1' if waiting else ''}){here} "
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
        if row.get("waiting"):
            return "for good once they're back" if not row.get("duration") else \
                f"{describe_seconds(seconds_of(str(row['duration'])))} once they're back"
        return "for good" if row["expires"] is None else describe_seconds(row["expires"] - time.time()) + " left"

    async def _mute_again(self, server: str, player: Player) -> None:
        self._start_waiting(player)
        row = self.active_mute(player.id, server)
        if not row:
            return
        rule = self.by_name.get(row["rule"])
        if row["kept"]:
            label = f"muted by {row['by']}" + (f": {row['reason']}" if row["reason"] else "")
        else:
            label = (rule.label if rule else "") or row["rule"]
        duration = None if row["expires"] is None else f"{max(1, round(row['expires'] - time.time()))}s"
        message = self._format("muted", notice=lore.notice("still muted"), detail=label, label=label,
                               left=self._mute_left(row), player=player.name)
        self.mutes_restored[player.id] = (time.time(), player.joined)
        # A mute already decided (a moderator's, a real strike's) isn't Vergil's call to make again: it's put back
        # whatever Vergil is on this server.
        action = await self.ctx.moderation.mute(
            server, player, duration, message, live=True,
            evidence=f"still muted: {row['rule']}" if row["kept"] else
            f"still muted: strike {row['number']} ({row['rule']})")
        self.log.info("[%s] %s: still muted (%s), %s%s", server, player.name or player.short_id, row["rule"],
                      self._mute_left(row), " (dry run)" if action.dry_run else "" if action.ok else f" FAILED: {action.text}")

    async def _punish(self, server: str, player: Player, hits: list[Hit], *, source: str, evidence: str,
                      by: str | None = None) -> tuple[Rule, Step, int, bool]:
        """One strike, on the matched rule whose next step is the most severe."""
        choices = []
        for hit in hits:
            previous = self.active_strikes(player.id, hit.rule, dry=self._dry(server))
            choices.append((hit.rule, hit.rule.step_for(previous), previous))
        rule, step, previous = max(choices, key=lambda choice: choice[1].severity)
        if step.action == "report":  # every line it's caught on only reports
            self.report(server, player, rule, next(hit for hit in hits if hit.rule is rule), source=source,
                        evidence=evidence)
            return rule, step, previous, True
        return await self.strike(server, player, rule, source=source, evidence=evidence, by=by,
                                 step=step, previous=previous)

    def report(self, server: str, player: Player, rule: Rule, hit: Hit, *, source: str, evidence: str) -> None:
        """A word of a line that only reports: nothing done in game, nothing said to anyone there, and no strike. A
        ticket for the staff on Discord, who decide: how a word's said can matter more than the word. A burst of lines
        is one, with a timer of its own: a strike right after it isn't held up (audit 2026-11, 5.4)."""
        self.last_report[player.id] = time.time()
        self.log.info("[%s] %s: %s (%s), a line that only reports → a ticket for the staff", server, player.name,
                      rule.name, hit.word)
        self.ctx.emit("heard", server, player_id=player.id, player_name=player.name, rule=rule.name, word=hit.word,
                      source=source, evidence=evidence, dry_run=self._dry(server))

    async def strike(self, server: str, player: Player, rule: Rule, *, source: str, evidence: str,
                     by: str | None = None, step: Step | None = None,
                     previous: int | None = None) -> tuple[Rule, Step, int, bool]:
        previous = self.active_strikes(player.id, rule, dry=self._dry(server)) if previous is None else previous
        step = step or rule.step_for(previous)
        number = previous + 1
        following = rule.step_for(number)
        next_text = "" if step.action == "ban" else self._format("next", next=following.describe())
        if step.action == "mute" and not step.kick:  # muted chat never arrives, so nothing would ever answer them, and they'd not know why
            next_text += f" Your {self.ctx.permissions.prefix} commands won't work while you're muted."
        template = "manual" if by else ("name" if source == "name" else "chat")
        written = evidence.partition(": ")[2] if by and ": " in evidence else ""  # what a moderator wrote: it's for them to read too
        note_text = f" ({written})" if written else ""
        strike_text = rule.strike_text(number)
        detail = ", ".join(t for t in (rule.label, strike_text) if t)
        # The sign hardens with what's done: a mute, a removal (a mute after it or not), a ban.
        notice = lore.notice("ban" if step.action == "ban" else "kick" if step.removes else "mute")
        message = self._format(template, notice=notice, detail=detail, label=rule.label, strike=strike_text,
                               action=step.describe(), next=next_text, by=by or "", player=player.name, note=note_text)
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
        if (step.action == "kick" and not step.duration) or step.plain_kick:
            # The server's plain kick; a mute after it is kept by this strike's row, and put on as they join again.
            if places:
                where, who = places[0]
                action = await moderation.kick(where, who, message, by=actor, evidence=evidence, **send)
                dry, ok, text = action.dry_run, action.ok, action.text
                for where, who in places[1:]:  # each other server's queue: a frozen one holds up only itself
                    self.ctx.outbox.send(where, f"Vergil's kick of {who.name} (struck on {server})",
                                         lambda where=where, who=who: moderation.kick(where, who, message, by=actor,
                                                                                      evidence=evidence, **send))
            # The server's kick needs them on: the strike still counts, and a mute with it starts when they next join.
            away = not places and not step.plain_kick
            pending = not places and step.plain_kick
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
            # The message goes as the ban's reason, which the server tells them as they're removed (as a kick's and a
            # mute's do): no tell of its own first, or they'd read it twice.
            # A mute that comes with a kick starts as they join again (Vergil's own mute list, kept by this row).
            action = await moderation.ban(where, who, step.kick or step.duration, message, by=actor, evidence=evidence,
                                          **send)
            dry, ok, text = action.dry_run, action.ok, action.text
        now = time.time()
        expires = now + step.kick_seconds + step.seconds if step.seconds is not None else None  # 0m: no mute left
        # After the server's own kick (out 10 to 120 minutes, its count, not ours), or for someone on no server, the
        # mute's time starts when they're back (_start_waiting): counted from the strike, a 10 minute mute was over
        # before they could be (audit 2026-11, 5.10). A timed kick's is counted from its end, above.
        waiting = step.action == "mute" and not step.kick and (step.plain_kick or pending)
        self.ctx.db.execute(
            "INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, "
            "action, duration, kick, expires, dry_run, ok, waiting) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, server, player.id, player.name, rule.name, rule.name, source, evidence, actor, step.action,
             step.duration, step.kick, expires, int(dry), int(ok), int(waiting)))
        result = (" (dry run)" if dry else " (not on now: muted when they next join)" if pending
                  else " (not on now: nobody to kick; the strike counts)" if away else "" if ok else f" FAILED: {text}")
        self.log.info("[%s] %s: %s %s → %s%s", server, player.name, rule.name, rule.strike_text(number), step,
                      result)
        self.ctx.emit("strike", server, player_id=player.id, player_name=player.name, rule=rule.name,
                      strike=number, action=str(step), source=source, evidence=evidence, by=actor, by_id=ACTING_ID.get(), manual=bool(by),
                      dry_run=dry, ok=ok)
        if places and ok and not dry:  # where they are, by the name they have there
            await self._caught(places[0][0], detail=detail, label=rule.label, strike=strike_text,
                               action=step.describe(), by=actor, player=places[0][1].name or player.name)
        return rule, step, number, ok

    async def _caught(self, server: str, **values: Any) -> None:
        """Everyone on the server reads why: the server's own line says only what was done ("Bravo was muted by
        Vergil"). The [automod] messages' `public`; "" says nothing."""
        line = self._format("public", notice=lore.notice("caught"), **values).strip()
        if not line:
            return
        try:
            await self.ctx.say(server, line)
        except (RconError, KeyError) as error:  # a line that doesn't arrive changes nothing
            self.log.info("[%s] couldn't say why %s was struck: %s", server, values.get("player"), error)

    # Commands -------------------------------------------------------------------------

    async def _resolve(self, call: CommandCall, ref: str) -> tuple[str, str] | None:
        """(player ID, name) for someone on the server or who left (targets.find, scope "seen"): #number, @number, a
        player ID (or its first 8+ characters), a name; several with one name: the caller picks."""
        player = await call.find(ref, scope="seen")
        return (player.id, player.name) if player else None

    def _outranks(self, call: CommandCall, player: Player) -> bool:
        return call.role == "faculty" or ROLES.index(self.ctx.highest_role(player)) < ROLES.index(call.role)

    def strike_rules(self) -> list[str]:
        """The lines a strike can be given on: not one that only reports, where it would do nothing."""
        return [rule.name for rule in self.rules if not rule.reports]

    def word_lists(self) -> list[tuple[str, str]]:
        """Each line by its name, with what its strikes do in a few words."""
        return [(rule.name, _short_ladder(rule)) for rule in self.rules]

    def _strike_word(self, word: str) -> bool:
        """What comes right after the player in !strike: a line's name, or name."""
        return word.lower() in self.by_name or word.lower() == NAME_STRIKE

    def _pardon_word(self, word: str) -> bool:
        return line_called(self.rules, word) is not None or word.lower() == "all"

    def _not_a_list(self, call: CommandCall, name: str, word: str) -> str:
        lists = [rule.name for rule in self.rules]
        close = difflib.get_close_matches(word.lower(), lists, n=1, cutoff=0.6)
        return (f"There's no list called {word}." + (f" Did you mean {close[0]}?" if close else "")
                + f" Lists: {', '.join(lists) or 'none'}. {call.prefix}pardonplayer {name} <list> clears that list's "
                f"strikes; {call.prefix}pardonplayer {name} all <why> clears every strike they have.")

    def _pardon_list(self, player_id: str, word: str) -> str:
        """The list a pardon's word names: one there now, said as !banword takes it (slur is slurs), or one taken off
        the word list that they still have strikes on, kept under its name. "" for neither: up to 6 November only the
        list's exact name counted, and anything else was the reason, so `slur` cleared every strike they had."""
        if (rule := line_called(self.rules, word)) is not None:
            return rule.name
        old = word.strip().lower()
        if self.ctx.db.query("SELECT 1 FROM automod_strikes WHERE player_id = ? AND grp = ? AND pardoned IS NULL LIMIT 1",
                             (player_id, old)):
            return old
        return ""

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
            await call.reply(f"Use: {call.prefix}strike <player> <rule> [note]. Rules: " + ", ".join(self.strike_rules())
                             + f"; or {NAME_STRIKE}, to ban their name")
            return
        ref, rest = await split(call, follows=self._strike_word)
        if ref is None:
            return
        reason = rest[0].lower() if rest else ""
        if reason not in known:
            later = next((n for n, word in enumerate(rest) if word.lower() in known), None)
            if not ref or later is None:
                await call.reply(f"Use: {call.prefix}strike <player> <rule> [note]. Rules: " + ", ".join(self.strike_rules())
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
        if rule.reports:
            await call.reply(f"The {rule.name} list only reports, so a strike on it does nothing: {call.prefix}mute, "
                             f"{call.prefix}kick or {call.prefix}ban them, or strike them on a list that has steps"
                             + (f" ({', '.join(self.strike_rules())})." if self.strike_rules() else "."))
            return
        if self._bans_for_an_admin(call, player, rule):
            # Only Huragok and admins ban (the host's call, 6 November): the strike goes to the admins, who may well
            # ban them anyway.
            if not await self.ctx.ask_an_admin(call, player, [rule.name, *rest[1:]]):
                await call.reply(f"That strike would ban {player.name} for good ({rule.name}), and only an admin bans: "
                                 "ask one.")
            return
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

    def _bans_for_an_admin(self, call: CommandCall, player: Player, rule: Rule) -> bool:
        """A strike from someone below admin whose next step is the ban for good. Not in dry run, where nothing's
        sent; not Vergil's own, which never come through !strike."""
        if ROLES.index(call.role) >= ROLES.index("admin") or self._dry(call.server):
            return False
        return rule.step_for(self.active_strikes(player.id, rule)).action == "ban"

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
                          note=note, key=f"name:{plain_name(name)}")
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

    @command("banword", usage="<words> <line> | <words> | [mutes] | [kicks] | [ban] | <name>",
             help="Add words to a line of the word list (!banword badword slurs), or make a new line: words | mutes | "
                  "kicks | ban | name. Names with them are kicked too", role="admin")
    async def banword(self, call: CommandCall) -> None:
        fields = [f.strip() for f in call.text.strip().split("|")]
        if not fields[0]:
            await call.reply(f"{DISCORD_BANWORD_USE if _discord(call) else BANWORD_USE} Lines: {self._lines()}.")
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
        # <words> | <a line's name>. A second part that starts with a digit and isn't a line is a mute time mistyped.
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
        hashed = next((w for w in entries if w.lstrip("-").strip().startswith("#")), None)
        if wanted is None or hashed:  # first, a word starting with # makes the line a note; anywhere, it catches nothing more
            await call.reply(f"Not added: {hashed or '#'} starts with #, and a line starting with # is a note, not a "
                             "rule: leave the # off (badtag also catches #badtag).")
            return
        if not shorthand and wanted.label and not any(fields[1:4]) and wanted.label not in by_name:
            target = line_called(rules, wanted.label)  # no steps, so not a new line's: slur is slurs
        if not shorthand and wanted.label and wanted.label in by_name:
            target = by_name[wanted.label]
            if any(fields[1:4]) and _ladder_of(target) != _ladder_of(wanted):
                adds = (f"{call.prefix}banword words: <words> listname: {target.name}, with mutes, kicks and bans empty, "
                        f"adds words to it; {call.prefix}changebanword changes its steps" if _discord(call) else
                        f"{call.prefix}banword <words> | {target.name} adds words to it")
                await call.reply(f"There's a {target.name} line already ({_short_ladder(target)}). {adds}; or give "
                                 "this line another name.")
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
        if self.config["check_names"] and new and rule.reports:
            names = (" Names with " + ("it" if len(new) == 1 else "them") + " in them aren't kicked: the staff are "
                     "asked whether to ban the name or allow it.")
        elif self.config["check_names"] and new:
            names = (" Names with " + ("it" if len(new) == 1 else "them") + " in them are kicked as they join, until "
                     "changed.")
        dry = (" Vergil is in dry run on this server: it only logs what it would do." if self._dry(call.server)
               and not rule.reports else "")
        steps = _short_ladder(rule)
        await call.reply(f"Added to {words_file} line {line} ({rule.name}): {added}. {steps}"
                         + ("" if steps.endswith("…") else ".") + names
                         + (f" Already there: {already}." if already else "") + dry)
        await self.check_names()  # anyone on now whose name has it

    def _which_line(self, call: CommandCall, words: str, line: str) -> str:
        """No line by that name (or none named): which there are, and how to make one, the words written in."""
        p = call.prefix
        name = "-".join(line.lower().split()) or "slurs"
        if _discord(call):
            make = (f"{p}banword words: {words} mutes: 1d, 7d kicks: 2, 1h, 1d bans: 5 listname: {name} (mutes, kicks and "
                    "bans all empty: a list that only reports, a ticket for the staff and nothing in game)")
            start = f"There's no list called '{line}' yet." if line else f"Which list should {words} go on?"
            if not self.rules:
                return f"The word list has no lists yet. Make the first one with its steps: {make}."
            return f"{start} Lists: {self._lines()}: give one in listname. Or make a new list: {make}."
        make = f"{p}banword {words} | 1d 7d | 2, 1h 1d | 5 | {name} ({NEW_LINE_HELP})"
        if not self.rules:
            return f"The word list has no lines yet. Make the first one with its steps: {make}."
        start = f"There's no line called '{line}' yet." if line else f"Which line should {words} go on?"
        return (f"{start} Lines: {self._lines()}: {p}banword {words} {self.rules[0].name} adds it to one. Or make "
                f"the line with its steps: {make}.")

    @command("unbanword", usage="<words> [| line] | | <line>",
             help="Take words off automod.txt (a line left with no words goes too), or with | <line> before nothing, "
                  "the whole line", role="admin")
    async def unbanword(self, call: CommandCall) -> None:
        """Words off the word list, wherever it lists them (`-word` too); with `| <line>`, off that line only; and
        `| <line>` alone takes the whole line off. Strikes given on it stay, under its name."""
        words_text, _, line_text = call.text.strip().partition("|")
        words = [w.strip() for w in words_text.split(",") if w.strip()]
        line_name = line_text.strip()
        if not words and not line_name:
            use = (f"Use: {call.prefix}unbanword words: <word, word> takes them off wherever they're listed (with "
                   f"listname, off that list only); {call.prefix}unbanword listname: <list> alone takes off the whole "
                   "list." if _discord(call) else
                   f"Use: {call.prefix}unbanword <word>[, <word>] as automod.txt lists them; {call.prefix}unbanword "
                   "<word> | <line> takes it off that line only; | <line> alone takes off the whole line.")
            await call.reply(f"{use} Lines: {self._lines()}.")
            return
        words_file = str(self.config["words"])
        try:
            rules = self._read_words()
        except (RuleError, OSError) as error:
            await call.reply(f"{words_file} can't be changed now ({error}); nothing was taken off.")
            return
        only = None
        if line_name:
            only = line_called(rules, line_name)
            if only is None:
                await call.reply(f"There's no line called {line_name}. Lines: "
                                 f"{', '.join(r.name for r in rules) or 'none'}.")
                return
            if not words:
                await self._remove_line(call, only)
                return

        def matches(entry: str, word: str) -> bool:
            return entry.startswith("-") == word.startswith("-") and same_name(entry.lstrip("-"), word.lstrip("-"))

        listed: list[tuple[Rule, list[str], list[str]]] = []  # (the line, its words to take off, its -words to)
        for rule in rules if only is None else [only]:
            entries = [w for w in rule.words if any(matches(w, word) for word in words)]
            allows = [w for w in rule.allow if any(matches(f"-{w}", word) for word in words)]
            if entries or allows:
                listed.append((rule, entries, allows))

        def taken(word: str) -> list[str]:
            """The lines this word comes off."""
            return [rule.name for rule, entries, allows in listed
                    if any(matches(entry, word) for entry in [*entries, *(f"-{w}" for w in allows)])]

        found = [word for word in words if taken(word)]
        hints = []
        for word in (word for word in words if word not in found):
            hit = None if word.startswith("-") else next(iter(Matcher(rules if only is None else [only]).find(
                word.strip("*"))), None)
            if hit:
                hints.append(f"{word} isn't listed by itself: the {hit.rule.name} line's {hit.word} catches it. "
                             f"{call.prefix}unbanword {hit.word} takes that off, or -{word} on that line lets it "
                             "through.")
            else:
                hints.append(f"{word} isn't on {'the ' + only.name + ' line' if only else 'the word list'}.")
        if not listed:
            await call.reply(" ".join(hints))
            return
        gone: list[str] = []
        try:
            for rule, entries, allows in sorted(listed, key=lambda item: -item[0].line):  # the last first: lines don't move
                left = [w for w in rule.words if w not in entries]
                kept = [w for w in rule.allow if w not in allows]
                change_rule(self.words_path, rule.line, line_of(replace(rule, words=left, allow=kept))
                            if left or not entries else None)
                if entries and not left:
                    gone.append(rule.name)
            self._reload_words()
        except (RuleError, OSError) as error:
            self.log.error("[%s] %s couldn't take %s off %s: %s", call.server, call.player.name, ", ".join(words),
                           words_file, error)
            await call.reply(f"Not taken off: {words_file} {str(error).removeprefix(words_file + ' ')}.")
            return
        off = ", ".join(f"{', '.join([*entries, *(f'-{w}' for w in allows)])} ({rule.name})"
                        for rule, entries, allows in listed)
        self.log.info("[%s] %s took %s off %s%s", call.server, call.player.name, off, words_file,
                      f"; the line{'s' if len(gone) > 1 else ''} {', '.join(gone)} went with no words left" if gone
                      else "")
        for word in found:
            self.ctx.emit("word_allowed", call.server, word=word, rules=taken(word), by=call.player.name)
        reply = f"Taken off {words_file}: {off}."
        if gone:
            reply += f" The {', '.join(gone)} line{'s' if len(gone) > 1 else ''} had no other words, so it went."
        still = [f"{word} by the {hits[0].rule.name} line's {hits[0].word}" for word in found
                 if not word.startswith("-") and (hits := self.matcher.find(word.strip("*")))]
        if still:
            reply += f" Still caught: {'; '.join(still)}."
        if hints:
            reply += f" Not taken off: {' '.join(hints)}"
        await call.reply(reply + f" Strikes already given for {'it' if len(found) == 1 else 'them'} stay.")

    async def _remove_line(self, call: CommandCall, rule: Rule) -> None:
        """A whole line off the word list, with every word on it. Its strikes stay, under its name: a line made with
        that name again counts them."""
        words_file = str(self.config["words"])
        try:
            change_rule(self.words_path, rule.line, None)
            self._reload_words()
        except (RuleError, OSError) as error:
            self.log.error("[%s] %s couldn't take the %s line off %s: %s", call.server, call.player.name, rule.name,
                           words_file, error)
            await call.reply(f"Not taken off: {words_file} {str(error).removeprefix(words_file + ' ')}.")
            return
        count = len(rule.words) + len(rule.allow)
        words = f"{count} word{'' if count == 1 else 's'}"
        self.log.info("[%s] %s took the %s line off %s (line %s, %s)", call.server, call.player.name, rule.name,
                      words_file, rule.line, words)
        self.ctx.emit("rule_changed", call.server, rule=rule.name, line=f"taken off the word list, with its {words}",
                      by=call.player.name)
        await call.reply(f"The {rule.name} line is off {words_file}, with its {words}: "
                         f"{', '.join([*rule.words, *(f'-{w}' for w in rule.allow)]) or 'none'}. Strikes already given "
                         "on it stay, and count again if a line's given its name.")

    @command("rules", usage="[line | word]", help="The word list: each line's name, words and what its strikes do",
             role="moderator")
    async def rules_command(self, call: CommandCall) -> None:
        """Every line in short, or one line (by its name, or a word on it) in full. The words too: the answer is
        the caller's alone."""
        if not self.rules and not (_discord(call) and getattr(self, "left_out", [])):
            await call.reply(f"The word list ({self.config['words']}) has no lines yet: {call.prefix}banword adds one.")
            return
        ask_for = call.text.strip()
        if _discord(call):  # room for all of it: every word and every step
            await call.reply(self._whole_list(call, ask_for))
            return
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

    def _whole_list(self, call: CommandCall, search: str) -> str:
        """Every line of the word list in full, for Discord: its name, every word, and what each strike does; the
        lines left out, with why. With `search`, the lines with it in their name or a word."""
        wanted = plain(search) if search.strip() else ""
        rules = [rule for rule in self.rules if not wanted or wanted in plain(rule.name)
                 or any(wanted in plain(word) for word in [*rule.words, *rule.allow])]
        if not rules:
            return f"No list is called {search} or has it in a word. Lists: {self._lines()}."
        words = sum(len(rule.words) for rule in rules)
        out = [f"{len(rules)} list{'' if len(rules) == 1 else 's'}, {words} word{'' if words == 1 else 's'}"
               + (f" with '{search.strip()}'" if wanted else f" ({self.config['words']})") + ":"]
        for rule in rules:
            listed = ", ".join([*rule.words, *(f"-{w}" for w in rule.allow)]) or "no words: only a strike climbs it"
            out += ["", f"{rule.name} (line {rule.line}): {listed}"]
            out += (["  Only reports: a ticket for the staff each time, nothing in game"] if rule.reports
                    else [f"  {step[:1].upper()}{step[1:]}" for step in ladder(rule)])
        left = getattr(self, "left_out", [])
        if left and not wanted:
            out += ["", f"Not in force until fixed in {self.config['words']}:"]
            out += [f"  line {item.line}: {item.why.removeprefix(f'line {item.line}: ')}" for item in left]
        if not wanted and self.config["check_names"]:
            out += ["", "Names with one of these words in them are kicked as they join, or, for a list that only "
                        f"reports, the staff are asked about them. {call.prefix}bannames shows the banned names."]
        return "\n".join(out)

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
            allowed = [] if call.text.strip() or not _discord(call) else self.allowed_names()
            await call.reply("No banned names" + (f" with '{call.text.strip()}' in them." if call.text.strip() else
                                                  f" in {self.config['names']}.") + " Names with a word from the word "
                             f"list in them are kicked too ({call.prefix}rules)."
                             + "".join(["\n\nAllowed by the staff, asked about a word of a list that only reports:",
                                        *[f"\n  {row['name']}, by {row['by']}" for row in allowed]] if allowed else []))
            return
        entries = [Candidate("", n.text, target=n.text) for n in names]
        if _discord(call):  # every one: there's room
            allowed = [] if call.text.strip() else self.allowed_names()
            lines = await offer(call, entries)
            await call.reply("\n".join([f"{len(names)} banned name{'' if len(names) == 1 else 's'} ({self.config['names']}),"
                                        " the latest first:", *lines, "", f"{call.prefix}unbanname @number takes one off.",
                                        *(["", "Allowed by the staff, asked about a word of a list that only reports "
                                           f"({call.prefix}banname bans one after all):"] if allowed else []),
                                        *[f"  {row['name']}, by {row['by']}" for row in allowed]]))
            return
        lines = await offer(call, entries[:15])
        more = [f"…and {len(names) - 15} more: {call.prefix}bannames <text> narrows it."] if len(names) > 15 else []
        await call.reply("\n".join([*lines, *more, f"{call.prefix}unbanname @number takes one off."]))

    @command("changebanword", usage="<line | word> | [mutes] | [kicks] | [ban]",
             help="New steps for a line of the word list: its mutes, kicks and ban, as !banword takes them",
             role="admin")
    async def changebanword(self, call: CommandCall) -> None:
        """A line's ladder replaced, its words and name kept (its strikes are kept under its name). A part left blank
        is none: never muted, never kicked (with no mutes either, the server's plain kick), never banned. From Discord,
        where each part is a box of its own, a box left empty keeps that part and `none` clears it (the host's call, 6
        November: `bans: 5` alone took the mutes and kicks away)."""
        fields = [f.strip() for f in call.text.strip().split("|")]
        if not fields[0] or len(fields) < 2:
            use = (f"{call.prefix}changebanword listname: slurs mutes: 1d, 7d kicks: 2, 1h, 1d bans: 5" if _discord(call)
                   else f"{call.prefix}changebanword <line | word> | <mutes> | <kicks> | <ban>, e.g. {call.prefix}"
                        "changebanword slurs | 1d 7d | 2, 1h 1d | 5")
            await call.reply(f"Use: {use} (a part left blank: none; all three blank: the line only reports, a ticket "
                             f"for the staff). Lines: {self._lines()}.")
            return
        if len(fields) > 4:
            await call.reply("The name stays (strikes are kept under it): give the line, then mutes | kicks | ban.")
            return
        words_file = str(self.config["words"])
        try:
            rules = self._read_words()
        except (RuleError, OSError) as error:
            await call.reply(f"{words_file} can't be changed now ({error}); nothing was changed.")
            return
        by_name = {r.name: r for r in rules}
        rule = by_name.get(fields[0].lower()) or line_called(rules, fields[0]) or next(
            (r for r in rules if any(same_name(w, fields[0]) for w in r.words)), None)
        if rule is None:
            await call.reply(f"No line is called {fields[0]} or lists it. Lines: {', '.join(by_name) or 'none'}.")
            return
        fields += [""] * (4 - len(fields))
        if _discord(call):
            if not any(fields[1:4]):
                await call.reply(f"Nothing to change: fill in mutes, kicks or bans (an empty box keeps that part; none "
                                 f"clears it). The {rule.name} line now: {_short_ladder(rule)}")
                return
            now = ([part.strip() for part in line_of(rule).split("|")] + [""] * 5)[1:4]
            fields[1:4] = [kept if not given else "" if given.lower() == "none" else given
                           for given, kept in zip(fields[1:4], now)]
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
        changed = self.by_name.get(rule.name)
        self.ctx.emit("rule_changed", call.server, rule=rule.name, line="; ".join(ladder(changed or new)),
                      by=call.player.name)
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
                "AND dry_run = 0 AND (expires IS NULL OR expires > ? OR waiting = 1) UNION SELECT player_id FROM automod_mutes "
                "WHERE lifted IS NULL AND (expires IS NULL OR expires > ?)", (now, now))}:
            mute = self.active_mute(player_id, call.server, waiting=True)
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
        if rest and (named := self._pardon_list(player_id, rest[0])):
            only = named
            rest = rest[1:]
        elif rest and rest[0].lower() == "all":
            rest = rest[1:]
        elif rest:
            # Neither a list nor all: refused, never read as the reason. A typo cleared every strike they had (the host's
            # call, 6 November: a reason for clearing everything comes after `all`).
            await call.reply(self._not_a_list(call, name, rest[0]))
            return
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
        kicks = [r for r in live if now - r["time"] < 7200
                 and ((r["action"] == "kick" and not r["duration"]) or self._kicked_then_muted(r))]
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

    def _kicked_then_muted(self, row: Any) -> bool:
        """A strike that was the server's own kick, its mute kept for when they're back (`waiting`, until they are). A
        mute for someone on no server waits the same way with nobody kicked, so the ledger says which: the kick made
        as the strike was (audit 6 November: `!pardonplayer` lifted the mute and left them kicked)."""
        if row["action"] != "mute" or row["kick"] or not row["waiting"]:
            return False
        return bool(self.ctx.db.query(
            "SELECT 1 FROM actions WHERE target_id = ? AND kind = 'kick' AND ok = 1 AND dry_run = 0 AND by = ? "
            "AND evidence IS ? AND time BETWEEN ? AND ? LIMIT 1",
            (row["player_id"], row["by"], row["evidence"], float(row["time"]) - 30, float(row["time"]) + 1)))

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
