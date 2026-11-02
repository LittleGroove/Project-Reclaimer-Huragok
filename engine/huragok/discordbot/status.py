"""Status: the servers at a glance. A board in a channel, edited in place; the bot's status line, its bio and nickname in the Discord; channel names, topics and voice
channel status. Each on its own timer, never under the floors in [limits], and only when what it says has changed: a channel's name and topic share 2 changes in 10 minutes,
and a message over an hour old has a hidden cap on its edits (error 30046: the board is then posted fresh, at most once an hour).

What was last sent is kept in the database (discord_posts), so a restart edits the same messages and doesn't rename anything again. The board says "updated <t:…:R>", which
Discord keeps counting by itself: if Huragok stops, readers see "updated 25 minutes ago" without the bot doing anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import string
import time
from typing import TYPE_CHECKING, Any, Callable

from . import cards, features, rest
from .alerts import SCHEMA_STEP
from .appeals import AGAIN_STEP as APPEAL_AGAIN_STEP
from .appeals import CODES_STEP as APPEAL_CODES_STEP
from .appeals import SCHEMA_STEP as APPEALS_STEP
from .reports import SCHEMA_STEP as REPORTS_STEP
from .tickets import NUMBERED_STEP, REPLACING_STEP
from .tidy import SCHEMA_STEP as TIDY_STEP
from .tickets import SCHEMA_STEP as TICKETS_STEP
from .cards import clean
from .config import DiscordConfig, colour_value
from .outbox import STATUS
from .rest import DiscordError, Reply
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

SCHEMA = ["""
    CREATE TABLE discord_posts (
        key TEXT PRIMARY KEY, message TEXT NOT NULL DEFAULT '', value TEXT NOT NULL DEFAULT '', time REAL NOT NULL);
    """, SCHEMA_STEP, TICKETS_STEP, REPORTS_STEP, APPEALS_STEP, TIDY_STEP, REPLACING_STEP, NUMBERED_STEP, APPEAL_CODES_STEP, APPEAL_AGAIN_STEP]
BOARD_LIST = "board:list"  # which cards the board has, in order, and where
UP = {"running", "external", "countdown"}  # it's still up for a countdown, with people playing: and Join is still worth pressing
ON_THE_WAY = {"restarting", "stopping", "launch", "booting", "preparing"}
STOPPED_ON_PURPOSE = {"idle"}
JOIN_PAGE = "https://projectreclaimer.dev/join?server="
UNCHANGED_EVERY = 600.0  # a board with nothing new is edited this often, so its "updated" stays true
UNKNOWN_WEBHOOK = 10015
TOO_MANY_EDITS = 30046  # Discord: this message has been edited too often
REPOST_EVERY = 3600.0
COUNT_EVERY_SECONDS = 300.0
NAMES_LIMIT = 900
EMOJI = {"up": "🟢", "busy": "🟡", "down": "🔴", "restarting": "⚪", "stopped": "⚫"}
STATE_WORDS = {"up": "Online", "busy": "Full", "down": "Offline", "restarting": "Restarting", "stopped": "Stopped"}


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""  # a placeholder with nothing to say yet ({top_player}, until stats): empty, never an error


def _all_there(template: str, values: dict[str, Any]) -> bool:
    """Whether everything this line would say has a value now."""
    try:
        return all(str(values.get(name.split(".")[0], "")).strip() for _, name, _, _ in string.Formatter().parse(template) if name)
    except ValueError:
        return True


def fill(template: str, values: dict[str, Any]) -> str:
    try:
        return re.sub(r"\s{2,}", " ", template.format_map(_Blank(values))).strip()
    except (ValueError, IndexError, AttributeError, KeyError):
        return template


def condition(row: dict[str, Any], state: Any) -> str:
    """How a server is: up, busy (full), restarting, or down. Which colour its card is."""
    phase = str(row.get("phase") or "")
    if phase in ON_THE_WAY:
        return "restarting"
    if phase in STOPPED_ON_PURPOSE:
        return "stopped"
    if phase not in UP:
        return "down"
    limit = getattr(state, "max_players", None)
    return "busy" if limit and len(getattr(state, "players", {})) >= limit else "up"


def health_dot(rows: list[dict[str, Any]]) -> str:
    """The bot's dot: green while everything meant to be up is, yellow while something meant to be up isn't, red when nothing is. A server stopped on purpose ("idle") isn't anything wrong. With none up and none meant to be: yellow."""
    meant = [row for row in rows if str(row.get("phase")) != "idle"]
    up = sum(1 for row in meant if str(row.get("phase")) in UP)
    if not meant:
        return "idle"
    return "online" if up == len(meant) else "dnd" if not up else "idle"


def uptime_words(row: dict[str, Any]) -> str:
    up = row.get("uptime")
    if not isinstance(up, (int, float)) or up <= 0:
        return str(up or "")
    minutes = int(up // 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


class Status:
    said_stuck = False  # that old cards can't be deleted: once is enough

    def __init__(self, part: DiscordPart, *, clock: Callable[[], float] = time.time) -> None:
        self.part = part
        self.clock = clock
        self._counted = (float("-inf"), 0)  # when the players were last counted, and how many
        self.due: dict[str, float] = {}  # what -> when it may next go
        self.shown: dict[str, str] = {}  # key -> what it last said, for not saying it twice
        self.rebuild = False  # a card's gone: post the whole list again, so it stays in order
        self.seen_at: dict[str, float] = {}  # a board -> when it was last edited
        self.reposted: dict[str, float] = {}  # a board -> when it was last posted fresh because Discord refused an edit
        self.rotation = 0

    @property
    def config(self) -> DiscordConfig:
        return self.part.file

    # What's kept --------------------------------------------------------------------------------------------------

    def stored(self, key: str) -> tuple[str, str]:
        rows = self.part.ctx.db.query("SELECT message, value FROM discord_posts WHERE key = ?", (key,))
        return (str(rows[0]["message"]), str(rows[0]["value"])) if rows else ("", "")

    def store(self, key: str, *, message: str | None = None, value: str | None = None) -> None:
        old_message, old_value = self.stored(key)
        self.part.ctx.db.execute("INSERT OR REPLACE INTO discord_posts (key, message, value, time) VALUES (?, ?, ?, ?)",
                                 (key, old_message if message is None else message, old_value if value is None else value, self.clock()))

    def forget(self, key: str) -> None:
        self.part.ctx.db.execute("DELETE FROM discord_posts WHERE key = ?", (key,))

    # What the servers are like ------------------------------------------------------------------------------------

    def rows(self) -> list[dict[str, Any]]:
        return self.part.ctx.server_rows()

    def values(self, row: dict[str, Any] | None = None) -> dict[str, Any]:
        """Every placeholder a template can use. With a server's row: its own too."""
        rows = self.rows()
        states = self.part.ctx.servers
        up = [r for r in rows if str(r.get("phase")) in UP]
        now = self.clock()
        if now - self._counted[0] > COUNT_EVERY_SECONDS:  # a whole table's worth of reading, for one number in a bio: and this is once per card
            known = self.part.ctx.db.query("SELECT COUNT(*) AS n FROM players")
            self._counted = (now, int(known[0]["n"]) if known else 0)
        values: dict[str, Any] = {
            "players_total": sum(len(states[str(r["name"])].players) for r in up if str(r["name"]) in states),
            "servers_up": len(up), "servers": len(rows), "unique_players": self._counted[1]}
        if row is None:  # the busiest server stands in for {server}, {map} and the rest on what's about them all
            row = max(up, key=lambda r: len(states[str(r["name"])].players) if str(r["name"]) in states else 0, default=None)
        if row is not None:
            name = str(row.get("name") or "")
            state = states.get(name)
            how = condition(row, state)
            values.update(server=clean(name), map=clean(getattr(state, "map", "") or ""), mode=clean(getattr(state, "mode", "") or ""),
                          players=len(getattr(state, "players", {}) or {}), max=getattr(state, "max_players", "") or "",
                          next=clean(row.get("next") or ""), state=STATE_WORDS[how], state_emoji=EMOJI[how], uptime=uptime_words(row),
                          playlist=clean(row.get("plays") or ""), version=clean(row.get("version") or ""))
        return values

    # Every so often -------------------------------------------------------------------------------------------------

    def on(self, key: str) -> bool:
        return features.state_of(features.BY_KEY[key], self.config, self.part.facts)[0] == features.ON

    def time_for(self, what: str, seconds: float) -> bool:
        now = self.clock()
        if now < self.due.get(what, 0.0):
            return False
        self.due[what] = now + seconds
        return True

    async def tick(self) -> None:
        if self.config.mode == "off":
            return
        if self.on("status.board") and self.time_for("board", float(self.config.section("status", "board")["update_seconds"])):
            self.board()
        if self.on("status.seeding"):
            self.seeding()
        if self.on("notify"):
            self.part.alerts.tick()
        if self.config.mode != "bot":
            return
        if self.on("status.presence") and self.time_for("presence", float(self.config.section("status", "presence")["rotate_seconds"])):
            await self.presence()
        if self.on("status.bio") and self.time_for("bio", float(self.config.section("status", "bio")["update_minutes"]) * 60):
            self.profile("bio", fill(str(self.config.section("status", "bio")["text"]), self.values()))
        if self.on("status.nickname") and self.time_for("nickname", float(self.config.section("status", "nickname")["update_minutes"]) * 60):
            self.profile("nick", fill(str(self.config.section("status", "nickname")["template"]), self.values())[:32])
        if self.on("status.voice_status") and self.time_for("voice", float(self.config.section("status", "voice_status")["update_minutes"]) * 60):
            self.voice_status()
        names, topics = self.on("status.channel_names"), self.on("status.topics")
        minutes = [float(self.config.limit("rename_minutes")), *[float(self.config.section("status", table).get("update_minutes", 0) or 0) for table, wanted in (("channel_names", names), ("topics", topics)) if wanted]]
        if (names or topics) and self.time_for("channels", max(minutes) * 60):
            self.channels(names, topics)

    # The board --------------------------------------------------------------------------------------------------------

    def card(self, row: dict[str, Any]) -> dict[str, Any]:
        options = self.config.section("status", "board")
        name = str(row.get("name") or "")
        state = self.part.ctx.servers.get(name)
        how = condition(row, state)
        show = options.get("show") or []
        lines: list[str] = []
        if "state" in show:
            lines.append(f"{EMOJI[how]} **{STATE_WORDS[how]}**")
        fields: list[dict[str, Any]] = []

        def field(label: str, value: Any) -> None:
            if value not in ("", None):
                fields.append({"name": label, "value": str(value)[:1000], "inline": True})

        values = self.values(row)
        if str(row.get("phase")) == "countdown" and row.get("countdown"):
            field("Restarting", row.get("countdown"))
        if how not in ("down", "stopped"):
            if "map" in show:
                field("Map", values["map"])
            if "mode" in show:
                field("Mode", values["mode"])
            if "players" in show:
                field("Players", f"{values['players']}/{values['max']}" if values["max"] else values["players"])
            if "next" in show:
                field("Next", values["next"])
            if "uptime" in show:
                field("Up for", values["uptime"])
            if "playlist" in show:
                field("Plays", values["playlist"])
            if "version" in show:
                field("Version", values["version"])
            if "votes" in show and row.get("voting"):
                field("Vote", "under way")
            everyone = sorted((p.name for p in getattr(state, "players", {}).values()), key=str.lower) if state is not None else []
            if options.get("player_list") == "names" and everyone:
                listed = clean(", ".join(everyone))
                fields.append({"name": "On now", "value": listed if len(listed) <= NAMES_LIMIT else listed[:NAMES_LIMIT] + "…", "inline": False})
        return {"title": clean(name)[:240], "description": "\n".join(lines), "color": colour_value(self.config, (options.get("colours") or {}).get(how)),
                "fields": fields}

    def buttons(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        options = self.config.section("status", "board")
        address = str((options.get("join_address") or {}).get(row.get("name")) or row.get("address") or "").strip()
        if not options.get("join_button") or not address or str(row.get("phase")) not in UP:
            return []
        return [{"type": 1, "components": [{"type": 2, "style": 5, "label": "Join", "url": f"{JOIN_PAGE}{address}"}]}]

    def board(self) -> None:
        options = self.config.section("status", "board")
        if f"webhook:{str(options.get('webhook') or '').strip()}" in self.part.feeds.blocked:
            return
        servers = options.get("servers", "*")
        rows = [row for row in self.rows() if servers == "*" or "*" in servers or row.get("name") in servers]
        if servers != "*" and "*" not in servers:  # in the order the file lists them
            rows.sort(key=lambda row: list(servers).index(row.get("name")))
        if options.get("layout") == "one":
            self._keep_in_order(["board:all"], options)
            self._show("board:all", self.summary(rows), [])
        else:
            self._keep_in_order([f"board:{row.get('name')}" for row in rows] + (["board:all"] if options.get("all_servers") else []), options)
            for row in rows:
                self._show(f"board:{row.get('name')}", [self.card(row)], self.buttons(row), not_news=uptime_words(row))
            if options.get("all_servers"):
                self._show("board:all", self.summary(rows), [])

    def _keep_in_order(self, keys: list[str], options: dict[str, Any]) -> None:
        """Discord can't reorder messages, so a server added, taken away or renamed, or a card someone deleted, means deleting the bot's own cards and posting the list again. Only its own are touched."""
        if not keys:  # nothing to show yet: never a reason to wipe what's there
            return
        webhook = str(options.get("webhook") or "").strip()
        channel = self._board_channel(options)
        where = webhook[-12:] or channel
        wanted = json.dumps({"keys": keys, "where": where})
        listed = self.stored(BOARD_LIST)[1]
        have = [row for row in self.part.ctx.db.query("SELECT key, message FROM discord_posts WHERE key LIKE 'board:%'") if row["key"] != BOARD_LIST]
        if listed == wanted and not self.rebuild:
            return
        if not listed and not self.rebuild and {row["key"] for row in have} <= set(keys) and len(have) in (0, len(keys)):  # the cards from before it was kept: as they are, if they're the right ones
            self.store(BOARD_LIST, value=wanted)
            return
        self.rebuild = False
        old = json.loads(listed).get("where", where) if listed else where
        for row in have:
            if row["message"]:
                self._delete_card(str(row["message"]), old, webhook)
            self.forget(row["key"])
        self.shown.clear()
        self.seen_at.clear()
        self.store(BOARD_LIST, value=wanted)

    def _delete_card(self, message_id: str, where: str, webhook: str) -> None:
        delivery = self.part.poster.delivery
        config = self.config

        async def send() -> Reply:
            if webhook:
                return await to_thread(rest.delete_webhook_message, webhook, message_id)
            return await to_thread(delivery.delete, config, where, message_id)

        def failed(error: DiscordError) -> bool:
            if error.status == 403 and not self.said_stuck:
                self.said_stuck = True
                log.warning("Old status cards can't be deleted: if the bot was removed and added back, Discord deleted the webhook that posted them. Delete them by hand, or give the bot Manage Messages")
            elif error.status != 403:
                log.info("Couldn't delete an old status card: %s", error)  # already gone, most likely
            return True

        self.part.outbox.put(send, priority=STATUS, bucket=f"board:{where}", what="an old status card", on_fail=failed)

    def summary(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        states = self.part.ctx.servers
        lines = []
        for row in rows:
            state = states.get(str(row.get("name")))
            how = condition(row, state)
            players = f"{len(state.players)}/{state.max_players or '?'}" if state is not None and how != "down" else ""
            game = " · ".join(clean(part) for part in (getattr(state, "map", ""), getattr(state, "mode", "")) if part) if how != "down" else ""
            lines.append(f"{EMOJI[how]} **{clean(row.get('name'))}** {players} {game}".rstrip())
        return [{"title": "Servers", "description": "\n".join(lines)[:3900] or "No servers yet.", "color": colour_value(self.config, (self.config.section("status", "board").get("colours") or {}).get("up"))}]

    def _show(self, key: str, embeds: list[dict[str, Any]], components: list[dict[str, Any]], *, not_news: str = "") -> None:
        """Post it, or edit what's there. Only when it says something new, and every so often to keep "updated" true. `not_news`: what changes by itself (an uptime), and isn't worth an edit every tick:
        Discord limits edits to older messages."""
        now = self.clock()
        said = repr((embeds, components))
        if not_news:
            said = said.replace(not_news, "")
        message_id, _ = self.stored(key)
        if message_id and self.shown.get(key) == said and now - self.seen_at.get(key, 0.0) < UNCHANGED_EVERY:
            return
        embeds = [dict(embed) for embed in embeds]
        embeds[-1]["description"] = (str(embeds[-1].get("description") or "") + f"\nupdated <t:{int(now)}:R>").strip()
        form = {"embeds": embeds, "components": components, "allowed_mentions": {"parse": []}}
        self.shown[key], self.seen_at[key] = said, now
        options = self.config.section("status", "board")
        webhook = str(options.get("webhook") or "").strip()
        channel = self._board_channel(options)
        if not webhook and not channel:
            return
        delivery = self.part.poster.delivery
        config = self.config

        def stored(reply: Any) -> None:
            if isinstance(reply, Reply) and isinstance(reply.data, dict) and reply.data.get("id"):
                self.store(key, message=str(reply.data["id"]))

        def failed(error: DiscordError) -> bool:
            """An edit Discord won't take: a message gone, or edited too often. Posted fresh next time, at most hourly for the latter."""
            if webhook and error.code == UNKNOWN_WEBHOOK:  # nothing's going to get through it: until Discord's read again, or the file changes
                self.part.feeds.refused(f"webhook:{webhook}", str(error))
                return True
            if error.status == 404 or error.code == TOO_MANY_EDITS:
                if error.code == TOO_MANY_EDITS and self.clock() - self.reposted.get(key, 0.0) < REPOST_EVERY:
                    return True
                self.reposted[key] = self.clock()
                self.rebuild = True  # the whole list again, so it stays in order
                return True
            return False

        if message_id:
            async def send() -> Reply:
                if webhook:
                    return await to_thread(rest.edit_webhook_message, webhook, message_id, form)
                return await to_thread(delivery.edit, config, channel, message_id, form)
        else:
            async def send() -> Reply:
                if webhook:
                    return await to_thread(rest.execute_webhook, webhook, {**form, **self._who()})
                return await to_thread(delivery.to_channel, config, channel, "superintendent", form)
        self.part.outbox.put(send, priority=STATUS, bucket=f"board:{webhook[-12:] or channel}", key=f"status:{key}", what=f"the status board ({key})",
                             on_done=stored, on_fail=failed)

    def _who(self) -> dict[str, str]:
        from .speakers import speaker

        return speaker(self.config, "superintendent")

    def _board_channel(self, options: dict[str, Any]) -> str:
        reference = self.config.channel_ref(options.get("channel"))
        facts = self.part.facts
        channel = facts.find_channel(reference) if facts is not None else None
        return str(channel.get("id")) if channel else reference if reference.isdecimal() else ""

    def seeding(self) -> None:
        """A server with a few players on: worth asking people to come and fill it. Once per `cooldown_minutes` a server."""
        options = self.config.section("status", "seeding")
        low, high = (list(options.get("seed_at") or [1, 3]) + [3])[:2]
        now = self.clock()
        for row in self.rows():
            name = str(row.get("name") or "")
            state = self.part.ctx.servers.get(name)
            count = len(state.players) if state is not None else 0
            if str(row.get("phase")) not in UP or not low <= count <= high:
                continue
            key = f"seeding:{name}"
            last = self.stored(key)[1]
            if last and now - float(last) < float(options.get("cooldown_minutes", 120) or 0) * 60:
                continue
            self.store(key, value=str(now))
            self.part.feeds.deliver(cards.seeding(self.config, name, count))

    # The bot itself -----------------------------------------------------------------------------------------------------

    async def presence(self) -> None:
        client = self.part.client
        if client is None:
            return
        options = self.config.section("status", "presence")
        lines = [str(line) for line in options.get("lines") or []]
        if not lines:
            return
        values = self.values()
        lines = [line for line in lines if _all_there(line, values)] or lines  # with no server up, "{map} on {server}" has nothing to say
        self.rotation = (self.rotation + 1) % len(lines)
        line = fill(lines[self.rotation], values)[:128]
        rows = self.rows()
        dot = "online" if options.get("dot") != "health" else health_dot(rows)
        said = f"{dot}|{line}"
        if self.shown.get("presence") != said and await client.show_presence(str(options.get("activity") or "watching"), line, dot):
            self.shown["presence"] = said

    def profile(self, field: str, text: str) -> None:
        """The bot's bio or nickname in this Discord, when it has changed."""
        key = f"profile:{field}"
        if not text or self.stored(key)[1] == text:
            return
        config = self.config

        async def send() -> Reply:
            return await to_thread(rest.edit_current_member, config.token, config.guild, **{field: text})

        self.part.outbox.put(send, priority=STATUS, bucket="profile", key=key, what=f"the bot's {field}", tag=f"profile:{field}",
                             on_done=lambda _: self.store(key, value=text))

    # Channels ----------------------------------------------------------------------------------------------------------

    def _templated(self, channels: dict[str, Any]) -> dict[str, str]:
        """{channel's ID: its text} for a table of `"#channel" = "template"` or `{ server = "...", template = "..." }`."""
        facts = self.part.facts
        rows = {str(row.get("name")): row for row in self.rows()}
        found: dict[str, str] = {}
        for reference, entry in channels.items():
            channel = facts.find_channel(self.config.channel_ref(reference)) if facts is not None else None
            if channel is None:
                continue
            template = str(entry.get("template") or "") if isinstance(entry, dict) else str(entry)
            server = rows.get(str(entry.get("server") or "")) if isinstance(entry, dict) else None
            found[str(channel.get("id"))] = fill(template, self.values(server))
        return found

    def channels(self, names: bool, topics: bool) -> None:
        """Names and topics, one change per channel: they share Discord's limit of 2 in 10 minutes."""
        changes: dict[str, dict[str, str]] = {}
        if names:
            for channel, text in self._templated(self.config.section("status", "channel_names").get("channels") or {}).items():
                changes.setdefault(channel, {})["name"] = text[:100]
        if topics:
            for channel, text in self._templated(self.config.section("status", "topics").get("channels") or {}).items():
                changes.setdefault(channel, {})["topic"] = text[:1024]
        token = self.config.token
        for channel, fields in changes.items():
            if f"rename:{channel}" in self.part.feeds.blocked:  # until Discord's read again, or the file changes
                continue
            key = f"channel:{channel}"
            said = repr(sorted(fields.items()))
            if self.stored(key)[1] == said:
                continue

            async def send(channel: str = channel, fields: dict[str, str] = fields) -> Reply:
                return await to_thread(rest.edit_channel, token, channel, fields)

            def done(_: Any, key: str = key, said: str = said) -> None:
                self.store(key, value=said)

            self.part.outbox.put(send, priority=STATUS, bucket=f"rename:{channel}", key=key, what="a channel's name or topic", tag=f"rename:{channel}", on_done=done,
                                 gap=float(self.config.limit("rename_minutes")) * 60)

    def voice_status(self) -> None:
        token = self.config.token
        for channel, text in self._templated(self.config.section("status", "voice_status").get("channels") or {}).items():
            if f"voice:{channel}" in self.part.feeds.blocked:  # until Discord's read again, or the file changes
                continue
            key = f"voice:{channel}"
            text = text[:500]
            if self.stored(key)[1] == text:
                continue

            async def send(channel: str = channel, text: str = text) -> Reply:
                return await to_thread(rest.set_voice_status, token, channel, text)

            def done(_: Any, key: str = key, text: str = text) -> None:
                self.store(key, value=text)

            self.part.outbox.put(send, priority=STATUS, bucket=f"voice:{channel}", key=key, what="a voice channel's status", tag=f"voice:{channel}", on_done=done)


__all__ = ["SCHEMA", "Status", "condition", "fill"]
