"""The part that runs the bot: reads discord.toml (and again whenever it changes), runs the outbox, and in bot mode keeps
the connection to Discord up. Always on: its switch is discord.toml's `mode`, and a mistake there or a Discord that says
no only ever turns the bot off, with the reason on the dashboard; the servers carry on.

Before signing in it asks Discord's web API about the token and the portal's switches, so a bad token or a missing switch
never costs one of the 1,000 logins a day. Past [limits] logins_per_day it stops until tomorrow and says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import sqlite3
from collections import Counter
import time
from pathlib import Path
from typing import Any

from ..core.background import background
from ..rcon import RconError
from ..core.events import Event
from ..core.commandsfile import ROLES
from ..core.text import TIME, describe_seconds, seconds_of
from ..players.ledger import shown_kind
from ..players.targets import player_and_rest
from ..core.part import Part, command, every, on
from ..core.settings import launcher
from ..lore import HURAGOK
from . import cards, features, records, rest, tidy
from ..lore import callout
from .alerts import Alerts
from .appeals import Appeals
from .cards import clean
from .ranks import WHOLE_ID
from .contact import Contact
from .config import FILE, DiscordConfig, DiscordFileError, load, reload_if_changed
from .facts import Facts, gather
from .feeds import EVENTS, Feeds
from .links import Links, shown_code
from .outbox import Job, Outbox
from .reports import ALREADY, OFF, TAKEN, TOO_MANY, TOO_SOON, Reports
from .rest import BadToken, DiscordError
from .speakers import Poster, message, say
from .status import SCHEMA, Status
from .tickets import Tickets
from .workers import to_thread

CONNECT_WAITS = (15, 30, 60, 120, 300)  # seconds between tries when Discord can't be reached
MOST_CHOICES_KEPT = 2000
SAY_AFTER_SECONDS = 120.0  # how long something's been wrong before it's worth a red line
OPEN_SHOWN = 8  # !reports, read in game chat
STATE_SECONDS = 30.0  # the state file says it's still true this often, for a second window
STOP_DRAIN_SECONDS = 3.0
KICK_BLOCKS = (600, 1800, 3600, 7200)  # how long the server keeps someone off, a kick after a kick
KICK_COUNT_RESETS = 86400
SIGN_IN_PAUSE_SECONDS = 1.5
LOOK_SECONDS = 300.0  # what's in the Discord is read again this often, for what's waiting and for a feed that was refused



class TooManyLogins(Exception):
    """Past [limits] logins_per_day: Discord resets the token at 1,000."""


def _how_long_before(seconds: float) -> str:
    return "just then" if seconds < 60 else f"{describe_seconds(seconds)} before"


class DiscordPart(Part):
    persona = HURAGOK
    name = "discord"
    description = "The Discord bot: discord.toml says what it does (J in the window, or /huragok setup)"
    always_on = True  # its switch is discord.toml's mode

    file: DiscordConfig
    client: Any = None
    facts: Facts | None = None  # what Discord said last: panels show it, the check always asks again
    state = "off"
    why = ""
    bot_name = ""
    guild_name = ""
    _token = ""  # the token the running bot signed in with
    _intents: frozenset[str] = frozenset()
    _bot_task: asyncio.Task[None] | None = None
    _signing_in_again: asyncio.Task[None] | None = None
    _state_written = 0.0
    _reload_problem = ""
    _waiting_until = 0.0  # monotonic: Discord asked for a long wait, on the dashboard until it's over
    _waiting_why = ""
    _looked = 0.0  # monotonic: when Discord was last read
    looking = 0  # reads of Discord under way

    async def setup(self) -> None:
        self.home = Path(self.ctx.home)
        self.path = self.home / FILE
        self.records = self.home / "records"
        self.file = load(self.path)
        self.outbox = Outbox(on_refused=self._refused, on_long_wait=self._long_wait,
                             long_wait=lambda: float(self.file.limit("wait_seconds")))
        self.poster = Poster(lambda: self.file, self.outbox)
        self.feeds = Feeds(self)
        self.alerts = Alerts(self)
        self.tickets = Tickets(self)
        self.reports = Reports(self)
        self.real_names: dict[str, str] = {}  # discord:<id> -> their name in this Discord, as `shown_as` may say otherwise
        self._look_again = False
        self._signing_in_since = 0.0
        self.kept_sending: dict[str, str] = {}  # what a kept post's edit on its way says
        self.choice_salt = secrets.token_hex(8)
        self.chosen: dict[str, str] = {}  # what a choice's value stands for
        self.reports_pinged: set[int] = set()
        self.appeals = Appeals(self)
        self.links = Links(self)
        self.roles_due = True  # rolesync.py: a look soon, and then now and then
        self.roles_looked = 0.0
        self.roles_running = False  # one look at a time (audit 0.6.1, 3.11)
        self.placing: dict[str, bool] = {}  # servers whose sides placement runs now: whether another's asked for (sides.py)
        self.roles_key: tuple[Any, ...] | None = None  # who should wear what, at the last tick (rolesync.due_key)
        self.contact = Contact(self)
        self.kept_tried: dict[str, float] = {}  # kept.py: when each kept post was last sent
        self.panel_open: dict[int, Any] = {}  # panels.py: each person's setup panel, as only the latest may delete itself
        self.tidy_paused: set[str] = set()  # tidy.py: channels the bot was refused in, until Discord's read again
        self.tidy_one_by_one: set[str] = set()
        self.tidy_said: set[str] = set()
        self.console_touched: dict[str, float] = {}  # consoleclear.py: a channel's last press, as that isn't a message
        self.console_running: dict[str, int] = {}  # lines running in each channel
        self.console_retry: dict[str, float] = {}
        self.console_said: set[str] = set()
        self.console_public_said: dict[str, float] = {}
        self._console_sweeping = False
        self._restarting = asyncio.Lock()
        self.ctx.moderation.adds_to_bans(self.appeals.in_the_reason)
        self._called: dict[str, float] = {}  # player -> when they last called staff
        self.ctx.db.migrate(SCHEMA)
        self.status = Status(self)
        self.ctx.spawn("Discord's outbox", self.outbox.run)
        self._say_warnings(self.file)
        if self.file.error:
            self.log.error("The Discord bot stays off: %s", self.file.error)
        await self._apply(previous=None)

    def _say_warnings(self, config: DiscordConfig) -> None:
        for line in config.warnings:
            self.log.warning(line)

    # What happened on the servers ---------------------------------------------------------------------------

    @on(*EVENTS)
    async def happened(self, event: Event) -> None:
        if event.name in ("server.ready", "server.adopted"):
            self.feeds.server_up(event.server or "")
        self.feeds.handle(event)
        if event.name == "supervisor.commands_reloaded":  # a rank changed, maybe: rank roles follow (rolesync.py)
            self.roles_due = True
        if event.name == "stats.game" and self.facts is not None:  # the boards moved: so may the top roles
            from . import rolesync

            self.roles_due = self.roles_due or rolesync.on(self, "top_roles")
        if event.name == "supervisor.commands_reloaded" and self.client is not None:  # a command switched off in commands.toml goes from Discord too
            self.client.commands_soon()
        if event.name == "rcon.phase" and event.get("phase") == "playing" and event.server:
            self.alerts.game_started(event.server, str(event.get("mode") or ""), str(event.get("map") or ""))
        if event.server and event.server in self.ctx.servers and self.takes_sides():  # onto the side they chose (sides.py)
            from . import sides

            if event.name == "rcon.phase" and event.get("phase") in (*sides.LOBBY, "finished"):
                sides.new_game(self, event.server)  # a switch of their own lasts the game it was made in
            if event.name == "rcon.phase" and (event.get("phase") == "playing" or event.get("phase") in sides.LOBBY):
                sides.soon(self, event.server)
            elif event.name in ("rcon.join", "rcon.leave") and (self.ctx.servers[event.server].phase == "playing"
                                                                or self.ctx.servers[event.server].phase in sides.LOBBY):
                sides.soon(self, event.server)  # someone joining or leaving may be what makes a waiting move fit
                # and once the server takes team changes again (refused for a few seconds after a join or leave), and
                # the one who joined has a team of their own (audit 0.6.1, 4.5; second audit, 8.9)
                sides.soon(self, event.server, delay=sides.MOVE_SECONDS)
        if event.name in ("stats.game", "stats.season"):
            await self.feeds.stats_happened(event)
        if event.name == "stats.season" and event.get("action") == "ended":
            from . import sides

            sides.season_ended(self)

    def where_to_run(self, player_id: str = "", preferred: str = "") -> str:
        """The server a command from Discord runs on, when nobody's said: where the player is; else the one it's about, if that's up; else any that's up. A stopped server can't do anything, and what's done
        on one is passed on to the rest. With none up, the one it's about or the first: looking still works, and acting says why it can't."""
        servers = self.ctx.servers
        player_id = player_id.lower()
        if player_id:
            for name, state in servers.items():
                if state.connected and state.player_by_id(player_id):  # while a console's down, its picture is only what it last was
                    return str(name)
        if preferred in servers and servers[preferred].connected:
            return preferred
        return next((str(name) for name, state in servers.items() if state.connected), preferred if preferred in servers else next(iter(servers), ""))

    def opaque(self, player_id: str, name: str = "") -> str:
        """What goes in a choice's value in place of someone's whole ID, which is theirs to know: anyone can read what an autocomplete sends. It means them for as long as Huragok's up."""
        value = "p:" + hashlib.sha256(f"{self.choice_salt}:{player_id.lower()}".encode("utf-8")).hexdigest()[:16]
        self.chosen.pop(value, None)  # to the end, as it's just been offered: set again in place, the oldest went first though shown a moment ago (audit 2026-11, 6.17)
        self.chosen[value] = player_id.lower()
        while len(self.chosen) > MOST_CHOICES_KEPT:
            self.chosen.pop(next(iter(self.chosen)))
        return value

    def meant(self, value: str) -> str:
        """What was picked or typed, as what the commands take: the ID behind a choice, or the words as they are."""
        text = str(value or "").strip()
        return self.chosen.get(text, text)

    def notice_for(self, player_id: str, text: str) -> None:
        """What they read as they next join, if the commands part is keeping those."""
        try:
            self.ctx.db.execute("INSERT INTO commands_notices (time, target, text) VALUES (?, ?, ?)", (time.time(), player_id, text))
        except sqlite3.Error as error:
            self.log.debug("Couldn't keep a notice for someone: %s", error)

    def came_from_card(self, asker: str, number: int) -> None:
        """A probationary moderator's press on a card: the request it became has that card to tell how it went."""
        rows = self.ctx.db.query("SELECT MAX(number) AS n FROM commands_requests WHERE by_id = ?", (asker,))
        if rows and rows[0]["n"]:
            self.ctx.db.execute("INSERT OR REPLACE INTO discord_posts (key, message, value, time) VALUES (?, '', ?, ?)", (f"request-from:{rows[0]['n']}", str(number), time.time()))

    def card_it_came_from(self, request: int) -> int:
        rows = self.ctx.db.query("SELECT value FROM discord_posts WHERE key = ?", (f"request-from:{request}",))
        return int(rows[0]["value"]) if rows and str(rows[0]["value"]).isdecimal() else 0

    def where_it_lives(self, player: str, kinds: tuple[str, ...] = ("ban", "kick", "mute")) -> str:
        """Where to lift something from: the server it was given on, as one given in game with / is only on that one. A player's ID or their name. `kinds`: what's being lifted, as they can be banned on
        one server and muted on another, and what's been lifted there already doesn't count."""
        lifted_by = {"ban": "unban", "kick": "pardon", "mute": "unmute"}
        wanted = {kind: lifted_by[kind] for kind in kinds}
        names = ", ".join(f"'{name}'" for name in (*wanted, *wanted.values()))
        rows = self.ctx.db.query(f"SELECT server, kind FROM actions WHERE (target_id = ? OR target_name = ?) AND kind IN ({names}) AND ok = 1 AND dry_run = 0 AND (evidence IS NULL OR evidence NOT LIKE 'kicked on %') "
                                 "ORDER BY number DESC LIMIT 60", (player.lower(), player))
        lifted: set[tuple[str, str]] = set()
        given_on = ""
        for row in rows:
            server = str(row["server"] or "")
            if str(row["kind"]) in wanted.values():
                lifted.add((server, str(row["kind"])))
            elif (server, wanted[str(row["kind"])]) not in lifted:
                given_on = server
                break
        state = self.ctx.servers.get(given_on)
        return given_on if state is not None and state.connected else self.where_to_run(player)

    def players_on(self, server: str) -> list[Any]:
        state = self.ctx.servers.get(server)
        return list(state.players.values()) if state is not None else []

    def last_visit(self, player_id: str) -> float | None:
        return self.ctx.last_visit(player_id) if player_id else None

    def old_name(self, player_id: str, name: str, when: float) -> str:
        """A name they went by before, if this one is new since they joined; "" if it isn't. On 0.10 the join's name is
        the one that came after it (feeds.py waits for it): Vergil records it as of the join, so it's new as it was."""
        record = self.ctx.players.get(player_id) if player_id and name else None
        if record is None or len(record.names) < 2:
            return ""
        rows = self.ctx.db.query("SELECT first_seen FROM player_names WHERE id = ? AND name = ?", (player_id.lower(), name))
        return record.names[1] if rows and rows[0]["first_seen"] >= when - 2 else ""

    # What the feeds need to know about a player ---------------------------------------------------------------

    def record_of(self, player_id: str, case: str = "") -> str:
        """What's on their record, or before this case: "2 mutes, 1 kick"."""
        before = int(case) if str(case).isascii() and str(case).isdecimal() else 2**62
        rows = self.ctx.db.query("SELECT kind, duration FROM actions WHERE target_id = ? AND dry_run = 0 AND ok = 1 AND number < ? AND kind IN "
                                 "('mute', 'kick', 'ban', 'warn') AND (evidence IS NULL OR (evidence NOT LIKE 'still muted:%' AND evidence NOT LIKE 'kicked on %'))", (player_id.lower(), before))
        counts = Counter(shown_kind(row["kind"], row["duration"]) for row in rows)
        return ", ".join(f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in counts.most_common())

    def said_by(self, player_id: str, lines: int, before: float) -> list[str]:
        """The last of what they said, oldest first, on any server, and how long before: it's read as tonight's words, by someone deciding what to do, so only what's recent counts. Nothing if the commands part keeps no chat."""
        since = before - float(self.file.section("tickets").get("evidence_minutes", 30) or 30) * 60
        try:
            rows = self.ctx.db.query("SELECT text, time FROM commands_chat WHERE player_id = ? AND time <= ? AND time >= ? ORDER BY time DESC LIMIT ?", (player_id.lower(), before, since, lines))
        except sqlite3.Error:
            return []
        return [f"{row['text']} ({_how_long_before(before - float(row['time']))})" for row in reversed(rows)]

    def recent_chat(self, server: str, lines: int) -> list[str]:
        now = time.time()
        since = now - float(self.file.section("tickets").get("evidence_minutes", 30) or 30) * 60
        try:
            rows = self.ctx.db.query("SELECT name, text, time FROM commands_chat WHERE server = ? AND time >= ? ORDER BY time DESC LIMIT ?", (server, since, lines))
        except sqlite3.Error:
            return []
        return [f"{clean(row['name'])}: {clean(row['text'])} ({_how_long_before(now - float(row['time']))})" for row in reversed(rows)]

    def kicked_lately(self, player_id: str, seconds: float, now: float) -> Any:
        """Their last kick, if the server's block ended under `seconds` ago, or hasn't yet (it blocks 10, 30, 60, then 120 minutes, the count starting
        again after a day without a kick; one with a time, that long). Not if it was pardoned: that's staff letting them back. A kick passed on to another
        server is the first server's block, so it doesn't count."""
        rows = self.ctx.db.query("SELECT time, by, reason, kind, duration FROM actions WHERE target_id = ? AND kind IN ('kick', 'pardon', 'ban', 'unban') AND ok = 1 AND dry_run = 0 "
                                 "AND (evidence IS NULL OR evidence NOT LIKE 'kicked on %') ORDER BY time DESC LIMIT 8", (player_id,))
        if not rows or shown_kind(rows[0]["kind"], rows[0]["duration"]) != "kick":  # let back, or banned for good
            return None
        kicks, later = 1, float(rows[0]["time"])
        for row in rows[1:]:
            if shown_kind(row["kind"], row["duration"]) == "kick":
                if later - float(row["time"]) >= KICK_COUNT_RESETS:
                    break
                kicks, later = kicks + 1, float(row["time"])
        duration = str(rows[0]["duration"] or "")  # a kick's own time, or the server's block as it announced it
        block = seconds_of(duration) if TIME.fullmatch(duration) else KICK_BLOCKS[min(kicks, len(KICK_BLOCKS)) - 1]
        return rows[0] if now - float(rows[0]["time"]) <= block + seconds else None

    def name_of(self, player_id: str, given: str = "") -> str:
        """Who a player is to anyone reading: the name they were last seen under. An old one means nothing to whoever comes next, and an ID means nothing to anyone."""
        record = self.ctx.players.get(player_id.lower()) if player_id else None
        return str(record.last_name) if record is not None and record.last_name else given or player_id[:8]

    def name_now(self, server: str, player_id: str, *, or_last: bool = False) -> str:
        """The name a player goes by on a server now, as its live picture has it; "" if it has none: 0.10's join names
        nobody, and it's known a few seconds later (state.named). `or_last`: else the name they were last seen under."""
        state = self.ctx.servers.get(server)
        player = state.player_by_id(player_id) if state is not None and player_id else None
        if player is not None and player.name:
            return str(player.name)
        record = self.ctx.players.get(player_id.lower()) if or_last and player_id else None
        return str(record.last_name) if record is not None and record.last_name else ""

    def named_in(self, text: str) -> str:
        """What a command answered, with whoever it's about by name: a server's answer often only has the ID it was given."""
        def named(found: re.Match[str]) -> str:
            record = self.ctx.players.get(found[0].lower())
            return str(record.last_name) if record is not None and record.last_name else found[0]

        return WHOLE_ID.sub(named, text)

    def banned_on(self, player_id: str) -> list[str]:
        """The servers whose last word on this player was a ban, for good: one with a time is a kick, and runs out without a word. A command's unban lifts what commands gave, on every server, wherever it was typed.
        A server's own /ban stays that server's until it's lifted there."""
        rows = self.ctx.db.query("SELECT server, kind, duration, source FROM actions WHERE target_id = ? AND ok = 1 AND dry_run = 0 AND kind IN ('ban', 'unban') ORDER BY number", (player_id.lower(),))
        last: dict[str, tuple[str, str]] = {}
        for row in rows:
            server = str(row["server"] or "")
            if row["kind"] == "unban":
                last.pop(server, None)
                if row["source"] == "commands":
                    last = {name: given for name, given in last.items() if given[1] == "in game"}
            else:
                last[server] = (shown_kind(row["kind"], row["duration"]), str(row["source"] or ""))
        return sorted(name for name, given in last.items() if name and given[0] == "ban")

    def banned_elsewhere(self, player_id: str, server: str) -> list[str]:
        """Other servers whose last word on this player was a ban for good."""
        return [name for name in self.banned_on(player_id) if name != server]

    def flags_of(self, player_id: str) -> list[Any]:
        return self.ctx.players.flags(player_id)

    def noted(self, player_id: str, lines: int) -> str:
        """What staff have written about them, for a card."""
        found = [f"Flagged **{clean(row['flag'])}**" + (f": {clean(row['why'])}" if row["why"] else "") for row in self.ctx.players.flags(player_id)]
        found += [f"{clean(row['text'])} ({clean(row['by'])})" for row in self.ctx.players.notes(player_id)[:lines]]
        return "\n".join(found)

    def staff_names(self) -> list[tuple[str, str]]:
        """(player ID, name) for every member of staff commands.toml lists."""
        permissions = self.ctx.permissions
        staff = set(permissions.members.get("faculty", set())) | set(permissions.members.get("moderator", set()))
        found = []
        for player_id in staff:
            record = self.ctx.players.get(player_id)
            if record is not None and record.last_name:
                found.append((player_id.lower(), record.last_name))
        return found

    # In game ------------------------------------------------------------------------------------------------

    def _call(self, server: str, player_id: str, name: str, why: str) -> bool:
        """Staff told on Discord. False if nobody's set to hear it, or this player called a moment ago."""
        options = self.file.section("feed", "calls")
        now = time.time()
        if now - self._called.get(player_id, 0.0) < float(options.get("cooldown_seconds", 0) or 0):
            return False
        card = cards.called(self.file, server, name, why, self.recent_chat(server, int(options.get("recent_lines", 0) or 0)))
        if self.file.mode == "off" or not self.feeds.reaches(card):
            return False
        self._called[player_id] = now
        self.feeds.deliver(card)
        self.tell_staff_in_game(callout("request", f"{name} called for an admin on {server}" + (f": {why}" if why else ".")), skip=player_id)
        return True

    def tell_staff_in_game(self, text: str, *, skip: str = "") -> None:
        """Every moderator and up on any server, privately: for what they're the only ones who can hear. [reports] tell_in_game says whether."""
        if not self.file.section("reports").get("tell_in_game", True):
            return

        async def tell() -> None:
            for server, state in list(self.ctx.servers.items()):
                for player in list(state.players.values()):
                    if player.id == skip or ROLES.index(self.ctx.highest_role(player)) < ROLES.index("moderator"):
                        continue
                    try:
                        await self.ctx.tell(server, player, text)
                    except (RconError, KeyError) as error:
                        self.log.debug("[%s] couldn't tell %s of a report: %s", server, player.name, error)

        background(tell(), "telling the staff in game", self.log)

    # Whether each can reach anyone: otherwise there's no such command, and !help doesn't offer what would do nothing.

    def discord_on(self) -> bool:
        return self.file.mode != "off"

    def takes_calls(self) -> bool:
        return self.discord_on() and features.BY_KEY["feed.calls"].is_on(self.file)

    def takes_reports(self) -> bool:
        return self.reports.takes("game")

    def has_open_tickets(self) -> bool:
        return self.file.mode == "bot" and (self.takes_calls() or self.takes_reports())

    def takes_links(self) -> bool:
        return self.links.takes()

    def takes_sides(self) -> bool:
        from . import sides

        return sides.takes(self)

    @command("calladmin", help="Call the staff on Discord", usage="[why]", role="player", when="takes_calls")
    async def calladmin(self, call: Any) -> None:
        if self._call(call.server, call.player.id, call.player.name, call.text):
            await call.reply(callout("request", "The staff have been told on Discord."))
        else:
            await call.reply("That can't reach anyone just now: you've called a moment ago, or calls aren't set up here.")

    @command("report", help="Tell the staff on Discord about a player", usage="<player> [why]", role="player", when="takes_reports")
    async def report(self, call: Any) -> None:
        options = self.file.section("reports")
        player, rest_of_line = await player_and_rest(call, scope="seen", start=True)
        if player is None:
            return
        if player.id == call.player.id:
            await call.reply("You can't report yourself.")
            return
        outcome, _ = self.reports.make(call.player.id, call.player.name, player.id, player.name, call.server, " ".join(rest_of_line), "game")
        if outcome == TOO_MANY:
            await call.reply(say(self.file, "reports.too_many"))
        elif outcome == TOO_SOON:
            await call.reply(f"You reported someone a moment ago. Give it {describe_seconds(float(options.get('reporter_cooldown_seconds', 0) or 0))}.")
        elif outcome == ALREADY:
            await call.reply("Added to your report." if rest_of_line else f"You've reported {player.name} already: the staff have it.")
        elif outcome == OFF:
            await call.reply("That can't reach anyone just now.")
        elif outcome == TAKEN and options.get("thank_reporter", True):
            await call.reply(callout("request", "Thanks: the staff have it on Discord."))
        elif outcome == TAKEN:
            await call.reply("Sent.")  # so they know it worked, whatever else isn't said

    @command("link", help="Your stats under your name on Discord: a code, told only to you, to give /link there", role="player", when="takes_links")
    async def link(self, call: Any) -> None:
        if call.channel == "discord":
            await call.reply("That's typed in game: the code comes to you there, privately. Then /link it here.")
            return
        linked = self.links.of_player(call.player.id)
        if linked is not None:
            await call.reply(f"You're linked to {linked['member_name']} on Discord already: /unlink there undoes it.")
            return
        code = self.links.new_code(call.player.id, call.player.name)
        if not code:
            await call.reply("That's three codes in an hour: use the last one you were told, or ask again later.")
            return
        from .standings import counted

        minutes = counted(self.links.options.get("code_minutes", 10), 10, 1, 1440)  # an inf broke !link (9.9)
        where = f"in {self.guild_name} on Discord" if self.guild_name else "on Discord"
        await call.reply(callout("link code", f"Your link code is {shown_code(code)}: {where}, type /link {shown_code(code)} "
                                              f"within {minutes} minute{'s' if minutes != 1 else ''}. Only you can see this."))

    @command("side", help="The team you're always put on: red, blue, or off", usage="<red|blue|off>", role="member",
             when="takes_sides")
    async def side(self, call: Any) -> None:
        from . import sides

        if call.channel == "discord":  # a side is a player's: here it's /side (audit 0.6.1, 4.7)
            await call.reply("That's a player's, typed in game; here it's /side, once your account's linked.")
            return
        wanted = call.text.strip().lower()
        if wanted not in ("red", "blue", "off", "none"):
            await call.reply(f"Use: {call.prefix}side red, blue or off")
            return
        held = sides.locked_to(self, call.player.id)
        if held and held != wanted:
            await call.reply(f"You're {held.title()} until this season ends: sides are kept for the season here.")
            return
        sides.choose(self, call.player.id, "" if wanted in ("off", "none") else wanted)
        if wanted in ("off", "none"):
            await call.reply("You'll play wherever the game puts you.")
            return
        await call.reply(f"You're {wanted.title()} from now on: put on that team during a round, as soon as it can't "
                         f"end the game or leave the teams uneven. {call.prefix}togglesides stops it.")
        if call.server in self.ctx.servers:
            sides.soon(self, call.server)

    @command("togglesides", help="Stop being put on your side, or start again", role="member", when="takes_sides")
    async def togglesides(self, call: Any) -> None:
        from . import sides

        if call.channel == "discord":
            await call.reply("That's a player's, typed in game; here it's /togglesides, once your account's linked.")
            return
        if kept := sides.held(self, call.player.id):  # [sides] force_for_season (the host's call, 3 November 2026)
            await call.reply(f"You're kept on {kept.title()} until this season ends: sides are forced here.")
            return
        now = sides.toggle(self, call.player.id)
        if now is None:
            await call.reply(f"You haven't chosen a side: {call.prefix}side red or {call.prefix}side blue.")
            return
        await call.reply("You're put on your side again." if now else "You'll play wherever the game puts you, keeping your side for when you switch it back.")
        if now and call.server in self.ctx.servers:
            sides.soon(self, call.server)

    @command("mods", help="Which staff are on the servers now", role="player", when="discord_on")
    async def mods(self, call: Any) -> None:
        on = [f"{player.name} ({server})" for server, state in self.ctx.servers.items() for player in state.players.values()
              if ROLES.index(self.ctx.highest_role(player)) >= ROLES.index("moderator")]
        if on:
            await call.reply("Staff on now: " + ", ".join(sorted(on)))
        elif call.channel == "discord":
            await call.reply("No staff are in a game just now.")
        else:
            await call.reply(f"No staff are in a game just now: {call.prefix}calladmin reaches them on Discord." if self.takes_calls() else "No staff are in a game just now.")

    @command("reports", help="What's open for staff on Discord: reports and calls for an admin, on every server", role="moderator", when="has_open_tickets")
    async def open_reports(self, call: Any) -> None:
        rows = self.ctx.db.query("SELECT * FROM discord_tickets WHERE state IN ('open', 'active') AND (kind LIKE 'reports-%' OR kind = 'calls-called') ORDER BY touched DESC")
        if not rows:
            await call.reply("Nothing's open.")
            return
        now = time.time()
        lines = []
        for row in rows[:OPEN_SHOWN]:
            kind = "A call" if str(row["kind"]) == "calls-called" else "A report"
            about = f" on {row['player_name']}" if row["player_name"] else ""
            where = f", {row['server']}" if row["server"] else ""
            hand = " (in hand)" if str(row["state"]) == "active" else ""
            lines.append(f"#{row['number']} {kind}{about}{where}, {describe_seconds(now - float(row['opened']))} ago{hand}")
        more = f"\n…and {len(rows) - OPEN_SHOWN} more" if len(rows) > OPEN_SHOWN else ""
        await call.reply(f"{len(rows)} open:\n" + "\n".join(lines) + more)

    @on("rcon.chat")
    async def heard(self, event: Event) -> None:
        """A chat word that calls, as !calladmin does."""
        words = self.file.section("feed", "calls").get("words") or []
        text = str(event.get("text") or "").casefold()
        player_id = str(event.get("id") or "").lower()
        if words and player_id and event.server and any(str(word).casefold() in text for word in words):
            self._call(event.server, player_id, str(event.get("name") or ""), str(event.get("text") or ""))

    def audit(self, kind: str, text: str, *, merge: bool = False) -> None:
        """Something done from Discord or the window, for the audit feed."""
        card = cards.audit(kind, text, self.file)
        card.merge = merge  # a burst of these, one after another, is one line
        self.feeds.deliver(card)

    @every(seconds=15)
    async def status_tick(self) -> None:
        await self.status.tick()
        if self.client is not None and self.state == "online":
            from .console import keep_menu

            keep_menu(self)
            self.contact.keep_card()
            from .standings import keep_boards

            await keep_boards(self, time.time())
            self._roles_tick(time.time())

    def _roles_tick(self, now: float) -> None:
        """Huragok's roles (rank, top and side roles) put right every [roles] check_minutes, and soon after a change (rolesync.py)."""
        from . import rolesync

        if self.facts is None or not any(rolesync.on(self, key) for key in ("roles", "top_roles", "side_roles")):
            return
        if (key := rolesync.due_key(self)) != self.roles_key:  # a rank, a link, a side or a setting changed
            self.roles_due, self.roles_key = True, key
        if self.roles_running or rolesync.changes_waiting(self):  # the last look first, and what it asked for
            return
        from .standings import counted

        every = counted(self.file.section("roles").get("check_minutes", 15), 15, 5, 1440) * 60.0  # 9.9
        if self.roles_due or now - self.roles_looked >= every:
            self.roles_due, self.roles_looked, self.roles_running = False, now, True
            background(self._put_roles_right(), "putting Huragok's roles right", self.log)

    async def _put_roles_right(self) -> None:
        from . import rolesync

        try:
            await rolesync.put_roles_right(self)
        finally:
            self.roles_running = False

    @every(seconds=60)
    async def ticket_sweep(self) -> None:
        if self.file.mode == "bot":
            background(self.contact.sweep(), "closing quiet Contact staff threads", self.log)
            tidy.sweep(self)
            self.tickets.sweep()
            if not self._console_sweeping:
                self._console_sweeping = True
                background(self._console_sweep(), "clearing the console", self.log)

    async def _console_sweep(self) -> None:
        """Slower than the tick when there's a lot to read, and one at a time."""
        from . import consoleclear

        try:
            await consoleclear.sweep(self)
        finally:
            self._console_sweeping = False

    # discord.toml, live -------------------------------------------------------------------------------------

    @every(seconds=2)
    async def look(self) -> None:
        await self.reload()
        self.feeds.flush()
        if not self._token and self.file.mode == "webhook" and time.monotonic() - self._looked >= LOOK_SECONDS:  # no bot means no look at Discord, and one refusal shouldn't stop a webhook for good
            self._looked = time.monotonic()
            self.feeds.unblock()
        if self._token and time.monotonic() - self._looked >= LOOK_SECONDS:
            self.look_at_discord()
        if self._waiting_why and time.monotonic() >= self._waiting_until:
            self._waiting_why = ""
            self.ctx.refresh_problems()
        client = self.client
        if client is not None and client.commands_retry_at is not None and time.monotonic() >= client.commands_retry_at:
            client.commands_retry_at = None
            client.commands_soon()
        if client is not None and self._signing_in_again is None and self.facts is not None and {intent for intent in self._intents if self.facts.intent_on(intent)} - client.asked_for:
            self.log.info("A switch in the developer portal has been turned on: signing in again to use it")  # it counts from the next sign-in
            self._sign_in_again_soon()
        if time.monotonic() - self._state_written >= STATE_SECONDS:
            self.write_state()

    async def reload(self, *, force: bool = False) -> bool:
        """discord.toml again if it changed (always, with force). True: a new one is in force."""
        try:
            fresh = reload_if_changed(self.file, force=force)
        except DiscordFileError as error:
            if str(error) != self._reload_problem:
                self._reload_problem = str(error)
                self.log.error("discord.toml not reloaded, keeping what was in force: %s", error)
                self.ctx.refresh_problems()
            return False
        if self._reload_problem:  # good again, even if that's just as it was
            self._reload_problem = ""
            self.ctx.refresh_problems()
        if fresh is None:
            return False
        previous, self.file = self.file, fresh
        self.feeds.unblock()
        self.log.info("discord.toml reloaded")
        self._say_warnings(fresh)
        if not force:  # with force it's from /huragok, which says who
            self.audit("settings", say(fresh, "audit.file"))
        await self._apply(previous)
        return True

    async def _apply(self, previous: DiscordConfig | None) -> None:
        """Make what runs what the file says: the bot only restarts for what needs it (a login costs)."""
        config = self.file
        token = config.token if config.mode == "bot" else ""
        intents = frozenset(features.needed_intents(config))
        if token != self._token or (token and intents != self._intents):
            same_token, client, facts = token == self._token, self.client, self.facts
            self._token, self._intents = token, intents
            if same_token and client is not None and facts is not None and {intent for intent in intents if facts.intent_on(intent)} == client.asked_for:
                pass  # what it would ask for is what it has: the portal's switch is off, say, and a login costs
            elif client is None:
                self._not_signing_in_again()
                await self._restart_bot()
            else:
                self._sign_in_again_soon()
        elif self.client is not None and previous is not None and config.guild != previous.guild:
            self.poster.delivery.forget()
            self.client.say_ready()
            self.client.commands_soon()
        elif self.client is not None and previous is not None and (features.slash_commands(config) != features.slash_commands(previous) or config.section("commands") != previous.section("commands")):
            self.client.commands_soon()
        if config.error:
            self.set_state("off", config.error)
        elif config.mode == "off":
            self.set_state("off", "")
        elif config.mode == "webhook":
            self.set_state("webhook", "")
        elif not token:
            self.set_state("waiting", "mode is bot, but there's no token")
        else:
            self.write_state()
            self.look_at_discord()
        self.ctx.refresh_problems()

    async def look_now(self) -> None:
        """Discord read this moment, for someone waiting to hear whether what they just did worked. Only ever after a press: it's three requests."""
        config = self.file
        if not config.token or not config.guild:
            return
        try:
            self.facts = await to_thread(gather, config.token, config.guild)
            self.tidy_paused.clear()
        except DiscordError as error:
            self.log.info("Couldn't read Discord just now: %s", error)

    def look_at_discord(self) -> None:
        """What's in the Discord now, for the panels: in the background, and a failure only leaves the last look."""
        if self.looking:  # one read running is all it takes, and one after it for what's changed since: dragging a category is an event for each channel in it
            self._look_again = True
            return
        config = self.file
        self._looked = time.monotonic()
        self.looking += 1

        async def look() -> None:
            try:
                self.facts = await to_thread(gather, config.token, config.guild)
                self.tidy_paused.clear()
                self.poster.delivery.no_webhook.clear()  # one refusal, or a proxy's, shouldn't keep it from posting as itself for good
                changed = await to_thread(features.names_to_ids, config, self.facts)
                self.feeds.unblock()  # worth another try, once: refused again, it waits again
                if self.state == "waiting" and str(self.why).startswith("Discord refused the token"):  # it just read Discord with that token: one 401 isn't for good
                    self.set_state("online" if self.client is not None and self.client.is_ready() else "signing in")
                    if self._bot_task is None or self._bot_task.done():
                        await self._restart_bot()
            except DiscordError as error:
                self.log.debug("Couldn't read Discord: %s", error)
            except OSError as error:  # a refused edit: the names still work
                self.log.warning("Couldn't put IDs in discord.toml: %s", error)
            else:
                if changed:
                    self.log.info("discord.toml: IDs put in place of names: %s", ", ".join(changed))
                    await self.reload(force=True)
            finally:
                self.looking -= 1
                if self._look_again:
                    self._look_again = False
                    self.look_at_discord()

        background(look(), "reading Discord", self.log)

    # The state ----------------------------------------------------------------------------------------------

    def set_state(self, state: str, why: str = "") -> None:
        changed = (state, why) != (self.state, self.why)
        if state == "signing in" and self.state != state:
            self._signing_in_since = time.monotonic()
        self.state, self.why = state, why
        if changed and why:
            (self.log.warning if state == "waiting" else self.log.info)("The Discord bot: %s", why)
        self.write_state()
        if changed:
            self.ctx.refresh_problems()

    def write_state(self) -> None:
        self._state_written = time.monotonic()
        try:
            records.write_state(self.records, mode=self.file.mode, state=self.state, why=self.why, bot=self.bot_name,
                                guild=self.guild_name, waiting=self.outbox.waiting)
        except OSError as error:
            self.log.debug("Couldn't write the bot's state: %s", error)

    def problems(self) -> list[str]:
        """What the dashboard shows in red."""
        lines: list[str] = []
        if not hasattr(self, "file"):  # not set up yet
            return lines
        if self.file.error:
            lines.append(f"Discord bot off: {self.file.error}")
        elif self._reload_problem:
            lines.append(f"discord.toml not reloaded: {self._reload_problem}")
        elif self.state == "waiting" and self.why:
            lines.append(f"Discord bot: {self.why}")
        if self._waiting_why and time.monotonic() < self._waiting_until:
            lines.append(f"Discord bot: {self._waiting_why}")
        lines += self.feeds.blocked_lines() if hasattr(self, "feeds") else []
        if self.file.mode == "bot" and not self.file.error:
            now = time.monotonic()
            if self.state == "signing in" and now - self._signing_in_since > SAY_AFTER_SECONDS:
                lines.append("Discord bot: it's been signing in for a while. If it stays like this, Discord can't be reached from here")
            if self.outbox.jobs and now - self.outbox.last_worked > SAY_AFTER_SECONDS and self.outbox.failing:
                lines.append(f"Discord bot: Discord can't be reached just now, and {self.outbox.waiting} posts are waiting")
            if self.state == "online" and self.facts is not None and self.facts.guild is None and self.file.guild:
                lines.append("Discord bot: it isn't in its Discord any more. Invite it back: the window's J has the link")
            waiting = [feature.title for feature in features.FEATURES if feature.stage <= features.BUILT and feature.is_on(self.file) and features.state_of(feature, self.file, self.facts)[0] == features.WAITING] if self.facts is not None else []
            if waiting:
                lines.append(f"Discord bot: {len(waiting)} of what's switched on {'is' if len(waiting) == 1 else 'are'} waiting on something: {', '.join(waiting[:3])}{'…' if len(waiting) > 3 else ''}. /huragok check says what")
        return lines

    def _long_wait(self, job: Job, seconds: float) -> None:
        self._waiting_until = time.monotonic() + seconds
        self._waiting_why = f"Discord asked it to wait {seconds / 60:.0f} minutes before {job.what or 'a post'}"
        self.log.warning("%s", self._waiting_why)
        self.ctx.refresh_problems()

    def _refused(self, job: Job, error: DiscordError) -> None:
        if not isinstance(error, BadToken) and error.status in (401, 403, 404) and job.tag.startswith(("channel:", "webhook:", "rename:", "voice:")):  # one bad card (a 400) says nothing about the channel
            self.feeds.refused(job.tag, str(error))
            self.ctx.refresh_problems()
        if isinstance(error, BadToken):
            self.set_state("waiting", f"Discord refused the token: fix it in Huragok's window (J). ({error})")

    # The bot ------------------------------------------------------------------------------------------------

    async def _run_bot(self, token: str) -> None:
        try:
            from . import client as gateway  # only in bot mode: webhook mode runs without discord.py
        except ImportError as error:
            self.set_state("waiting", f"discord.py isn't installed ({error}): start Huragok with {launcher()} again, which "
                                 "installs what's missing")
            return
        tries = 0
        started = 0.0
        while True:
            used = records.logins_today(self.records)
            allowed = int(self.file.limit("logins_per_day"))
            if used >= allowed:
                self.set_state("waiting", f"the bot signed in {used} times today ([limits] logins_per_day): it stays off "
                                     "until tomorrow, because Discord resets a token at 1,000. Something keeps "
                                     "restarting it")
                await asyncio.sleep(3600)
                continue
            self.set_state("signing in")
            try:
                application = await to_thread(rest.application, token)
                self.client = gateway.Bot(self, frozenset(features.needed_intents(self.file)), application)
                started = time.monotonic()
                await self.client.run_until_closed(token)
                return
            except asyncio.CancelledError:
                raise
            except BadToken as error:
                self.set_state("waiting", f"Discord refused the token: fix it in Huragok's window (J). ({error})")
                return
            except TooManyLogins as error:
                self.set_state("waiting", str(error))
            except gateway.FATAL as error:
                self.set_state("waiting", gateway.why_fatal(error))
                if not gateway.worth_trying_again(error):
                    return
            except Exception as error:  # noqa: BLE001 - no way to Discord: the bot tries again, nothing else notices
                self.log.debug("The bot's connection failed", exc_info=True)
                self.set_state("waiting", f"can't reach Discord just now{gateway.why_down(error)}; trying again")
            finally:
                await self._close_client()
            tries = 0 if started and time.monotonic() - started > CONNECT_WAITS[-1] else tries
            started = 0.0
            await asyncio.sleep(CONNECT_WAITS[min(tries, len(CONNECT_WAITS) - 1)])
            tries += 1

    async def _close_client(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                self.log.debug("Closing the bot's connection failed", exc_info=True)

    def _sign_in_again_soon(self) -> None:
        """What the bot needs has changed, maybe from a press in Discord. It gets its answer first, over the connection that's about to go, and several changes at once are one sign-in, as a login costs."""
        self._not_signing_in_again()

        async def again() -> None:
            await asyncio.sleep(SIGN_IN_PAUSE_SECONDS)
            self._signing_in_again = None  # past cancelling: from here it finishes
            await self._restart_bot()

        self._signing_in_again = asyncio.ensure_future(again())

    def _not_signing_in_again(self) -> None:
        if self._signing_in_again is not None:
            self._signing_in_again.cancel()
            self._signing_in_again = None

    async def _restart_bot(self) -> None:
        """Stop it, and start it if there's a token. One at a time, or two bots could end up connected, each hearing and doing everything."""
        async with self._restarting:
            await self._stop_bot()
            self.poster.delivery.forget()
            self.facts = None
            if self._token:
                self._bot_task = asyncio.ensure_future(self._run_bot(self._token))

    async def _stop_bot(self) -> None:
        task, self._bot_task = self._bot_task, None
        await self._close_client()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.bot_name = self.guild_name = ""

    def bot_ready(self, name: str, guild_name: str) -> None:
        self.bot_name, self.guild_name = name, guild_name
        self.set_state("online", "")
        self.look_at_discord()

    def bot_gone(self) -> None:
        if self.state == "online":
            self.set_state("signing in", "")

    def count_login(self) -> None:
        """Called before each login to the gateway (client.py)."""
        used = records.logins_today(self.records)
        allowed = int(self.file.limit("logins_per_day"))
        if used >= allowed:
            raise TooManyLogins(f"the bot signed in {used} times today ([limits] logins_per_day): it stays off until "
                                "tomorrow, because Discord resets a token at 1,000. Something keeps restarting it")
        records.count_login(self.records)

    async def teardown(self) -> None:
        self._not_signing_in_again()
        async with self._restarting:
            await self._stop_bot()
        self.feeds.flush(everything=True)
        await self.outbox.drain(STOP_DRAIN_SECONDS)
        self.state = "stopped"
        self.write_state()


__all__ = ["DiscordPart", "TooManyLogins"]
