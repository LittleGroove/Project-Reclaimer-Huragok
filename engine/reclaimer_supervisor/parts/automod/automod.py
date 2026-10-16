from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any

from ...chat import CommandCall
from ...permissions import ROLES
from ...events import Event
from ...part import Part, command, every, on
from ...state import PLAYER_ID, Player

from .matcher import Hit, Matcher, normalise
from .rules import Rule, RuleError, Step, read_rules

MIGRATIONS = [
    """
    CREATE TABLE automod_strikes (
        number INTEGER PRIMARY KEY, time REAL NOT NULL, server TEXT, player_id TEXT NOT NULL,
        player_name TEXT, rule TEXT NOT NULL, grp TEXT NOT NULL, source TEXT NOT NULL, evidence TEXT,
        by TEXT NOT NULL, action TEXT NOT NULL, duration TEXT, expires REAL, dry_run INTEGER NOT NULL,
        ok INTEGER NOT NULL, pardoned REAL, pardoned_by TEXT);
    CREATE INDEX automod_strikes_player ON automod_strikes (player_id, grp, time);
    """,
]

MESSAGES = {
    # What the player reads. {detail} is the line's name and the strike ("slurs, strike 2 of 3");
    # {label}, {strike}, {action}, {next} and {by} work too. The word itself is never repeated.
    "chat": "That isn't allowed here ({detail}): {action}.{next}",
    "name": "Change your name, it isn't allowed here ({detail}): {action}.{next}",
    "manual": "{by} gave you a strike ({detail}): {action}.{next}",
    "next": " Next time: {next}.",
}


def load_rules(config: dict[str, Any], home: Path) -> tuple[list[Rule], Path]:
    """The rules in the word list, and the file to watch for edits."""
    path = home / str(config["words"])
    return read_rules(path), path


class AutoMod(Part):
    name = "automod"
    description = "One word list: per line, its words, cascading timers and chances before a ban"
    acts_as = "AutoMod"
    enabled_by_default = True
    defaults = {
        "name": "AutoMod",
        "words": "automod.txt",
        "forget_after_days": 0,    # strikes older than this stop counting; 0: never
        "exempt_roles": ["admin", "owner"],
        "check_names": True,
        "cooldown_seconds": 10.0,  # lines this close together count as one strike
        "split_seconds": 10.0,     # a word split over lines sent this close together is caught
        "messages": {},
    }

    @classmethod
    def preflight(cls, config: dict[str, Any], home: Path) -> list[str]:
        try:
            rules, _ = load_rules({**cls.defaults, **config}, home)
        except RuleError as error:
            raise ValueError(str(error)) from None
        return [] if rules else [f"{config.get('words', cls.defaults['words'])} has no words yet"]

    def check_config(self) -> None:
        if unknown := set(self.config["messages"]) - set(MESSAGES):
            raise ValueError(f"messages: unknown '{sorted(unknown)[0]}'; use {', '.join(MESSAGES)}")
        if bad := set(self.config["exempt_roles"]) - set(ROLES):
            raise ValueError(f"exempt_roles: '{sorted(bad)[0]}' isn't a role")
        forget = self.config["forget_after_days"]
        if isinstance(forget, bool) or not isinstance(forget, (int, float)) or forget < 0:
            raise ValueError("forget_after_days must be 0 (never) or more")
        self.messages = {**MESSAGES, **self.config["messages"]}
        try:
            self._install(*load_rules(self.config, self.ctx.home))
        except RuleError as error:
            raise ValueError(str(error)) from None

    def _install(self, rules: list[Rule], path: Path) -> None:
        self.rules = rules
        self.by_name = {r.name: r for r in rules}
        self.matcher = Matcher(rules)
        self.watched = {path: self._mtime(path)}

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    async def setup(self) -> None:
        self.ctx.db.migrate(MIGRATIONS)
        self.recent: dict[str, deque[tuple[float, str, str]]] = {}
        self.last_strike: dict[str, float] = {}
        self.log.info("%s rules%s", len(self.rules), " · dry run" if self.ctx.dry_run else "")

    @every(seconds=5)
    async def reload_if_changed(self) -> None:
        if all(self._mtime(f) == m for f, m in self.watched.items()):
            return
        try:
            self._install(*load_rules(self.config, self.ctx.home))
            self.log.info("rules reloaded: %s rules", len(self.rules))
        except RuleError as error:
            self.watched = {f: self._mtime(f) for f in self.watched}  # don't repeat the error every 5 s
            self.log.error("rules not reloaded, keeping the old ones: %s", error)

    # Watching -------------------------------------------------------------------------

    def _exempt(self, server: str, player: Player) -> bool:
        return self.ctx.role_of(server, player) in self.config["exempt_roles"]

    def _player(self, server: str, player_id: str, name: str) -> Player:
        return self.ctx.server(server).player_by_id(player_id) or Player(0, name, player_id)

    @on("rcon.chat")
    async def chat(self, event: Event) -> None:
        if event.get("channel") == "server" or not event.get("id"):
            return
        server, player_id, text = event.server or "", str(event["id"]).lower(), str(event.get("text", ""))
        player = self._player(server, player_id, str(event.get("name", "")))
        if self._exempt(server, player):
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
        if not self.config["check_names"] or not event.get("id"):
            return
        server = event.server or ""
        player = self._player(server, str(event["id"]).lower(), str(event.get("name", "")))
        hits = self.matcher.find(player.name)
        if hits and not self._exempt(server, player):
            await self._punish(server, player, hits, source="name", evidence=f"name: {player.name}")

    # Striking ------------------------------------------------------------------------

    def active_strikes(self, player_id: str, rule: Rule) -> int:
        forget = float(self.config["forget_after_days"])
        since = time.time() - forget * 86400 if forget else 0
        row = self.ctx.db.query(
            "SELECT COUNT(*) AS n FROM automod_strikes WHERE player_id = ? AND grp = ? AND pardoned IS NULL "
            "AND time >= ? AND dry_run = ?", (player_id, rule.name, since, int(self.ctx.dry_run)))
        return int(row[0]["n"])

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
        next_text = "" if step.action == "ban" else self.messages["next"].format(next=following.describe())
        template = self.messages["manual" if by else ("name" if source == "name" else "chat")]
        strike_text = rule.strike_text(number)
        message = template.format(detail=", ".join(t for t in (rule.label, strike_text) if t), label=rule.label,
                                  strike=strike_text, action=step.describe(), next=next_text, by=by or "")
        self.last_strike[player.id] = time.time()
        moderation = self.ctx.moderation
        actor = by or self.ctx.acts_as
        if step.action == "kick" and not step.duration:
            action = await moderation.kick(server, player, message, by=actor, evidence=evidence)
        else:  # a timed removal, or a ban: the server's ban, for that long or for good
            if player.number:
                try:
                    await self.ctx.tell(server, player, message)  # they may read it before the ban lands
                except Exception:  # noqa: BLE001
                    pass
            action = await moderation.ban(server, player, step.duration, message, by=actor, evidence=evidence)
        now = time.time()
        expires = now + step.seconds if step.seconds else None
        self.ctx.db.execute(
            "INSERT INTO automod_strikes (time, server, player_id, player_name, rule, grp, source, evidence, by, "
            "action, duration, expires, dry_run, ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, server, player.id, player.name, rule.name, rule.name, source, evidence, actor, step.action,
             step.duration, expires, int(action.dry_run), int(action.ok)))
        self.log.info("[%s] %s: %s %s → %s%s", server, player.name, rule.name, rule.strike_text(number), step,
                      " (dry run)" if action.dry_run else "" if action.ok else f" FAILED: {action.text}")
        self.ctx.emit("strike", server, player_id=player.id, player_name=player.name, rule=rule.name,
                      strike=number, action=str(step), source=source, evidence=evidence, by=actor,
                      dry_run=action.dry_run, ok=action.ok)
        return rule, step, number, action.ok

    # Commands -------------------------------------------------------------------------

    async def _resolve(self, call: CommandCall, ref: str) -> tuple[str, str] | None:
        """(player ID, name) for someone on the server, a player ID, or a name seen before."""
        if PLAYER_ID.fullmatch(ref):
            record = self.ctx.players.get(ref)
            return ref.lower(), record.last_name if record else ref[:8]
        online = call.state.find(ref)
        if len(online) == 1:
            return online[0].id, online[0].name
        if len(online) > 1:
            await call.find_player(ref)  # lists them
            return None
        seen = self.ctx.players.find(ref)
        if len(seen) == 1:
            return seen[0].id, seen[0].last_name
        if not seen:
            await call.reply(f"Nobody called '{ref}' is on or has been seen. Use their player ID.")
        else:
            await call.reply(f"'{ref}' could be " + ", ".join(f"{r.last_name} {r.id[:8]}" for r in seen[:5])
                             + ". Use the player ID.")
        return None

    def _outranks(self, call: CommandCall, player: Player) -> bool:
        return call.role == "owner" or ROLES.index(self.ctx.role_of(call.server, player)) < ROLES.index(call.role)

    @command("strike", usage="<player> <rule> [note]",
             help="Give a strike on an automod rule, e.g. for something said in voice", role="moderator")
    async def strike_command(self, call: CommandCall) -> None:
        if len(call.args) < 2 or call.args[1].lower() not in self.by_name:
            await call.reply("Use: !strike <player> <rule> [note]. Rules: " + ", ".join(self.by_name))
            return
        found = await self._resolve(call, call.args[0])
        if not found:
            return
        player = self._player(call.server, *found)
        if not self._outranks(call, player):
            await call.reply(f"{player.name} is a {self.ctx.role_of(call.server, player)}; you can't strike them.")
            return
        rule = self.by_name[call.args[1].lower()]
        note = " ".join(call.args[2:])
        _, step, number, ok = await self.strike(call.server, player, rule, source="manual", by=call.player.name,
                                                evidence=f"{call.player.name}: {note}" if note else call.player.name)
        result = "dry run, not sent" if self.ctx.dry_run else ("done" if ok else "the server refused it")
        await call.reply(f"{player.name}: {rule.name} {rule.strike_text(number)}, {step.describe()} ({result}).")

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
        await call.reply(f"{name}: " + ("; ".join(lines) if lines else "no strikes."))

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
        if any(r["action"] in ("ban", "kick") and r["duration"] and (r["expires"] or 0) > now for r in live) or \
                any(r["action"] == "ban" and not r["duration"] for r in live):
            action = await self.ctx.moderation.unban(call.server, player_id, by=call.player.name)
            lifted.append("ban lifted" if action.ok else f"unban failed: {action.text}")
        if any(r["action"] == "mute" and (r["expires"] is None or r["expires"] > now) for r in live):
            action = await self.ctx.moderation.unmute(call.server, player_id, by=call.player.name)
            lifted.append("unmuted" if action.ok else f"unmute failed: {action.text}")
        if any(r["action"] == "kick" and not r["duration"] and now - r["time"] < 7200 for r in live):
            action = await self.ctx.moderation.pardon(call.server, player_id, by=call.player.name)
            lifted.append("can rejoin now" if action.ok else f"kick pardon failed: {action.text}")
        rules = sorted({r["rule"] for r in rows})
        reason = " ".join(rest)
        self.log.info("[%s] %s pardoned %s: %s strikes (%s)%s", call.server, call.player.name, name, len(rows),
                      ", ".join(rules), f": {reason}" if reason else "")
        self.ctx.emit("pardon", call.server, player_id=player_id, player_name=name, strikes=len(rows),
                      rules=rules, by=call.player.name, reason=reason)
        summary = f"Pardoned {name}: {len(rows)} strike{'s' if len(rows) != 1 else ''} cleared ({', '.join(rules)})"
        await call.reply(summary + (f"; {', '.join(lifted)}." if lifted else "."))
