"""/note and /flag: what staff write about a player, kept with the player's record, so !whois and the cards show it too. They exist while
[notes] is switched on (features.slash_commands). /notify: members' own alerts, for anyone. Imported with client.py, in bot mode alone. Every answer
is private.

Anyone with a staff rank can read and write notes, a probationary moderator too: a note acts on nobody. Taking one away and flags need a moderator.
"""

from __future__ import annotations

import io
import json
import re
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from ..core.storage import acting
from ..players.ledger import shown_kind
from ..players.targets import FULL_ID
from ..core.commandsfile import PROBATION
from . import features
from .alerts import GAME, MOST_PER_MEMBER, PLAYERS
from .cards import clean, lasting_words
from .cards import counted as cards_counted
from .client import ANOTHER_DISCORD, answer, elsewhere, named, part_of, someone_from
from .ranks import can_act, highest_rank
from .speakers import say

if TYPE_CHECKING:
    from ..core.storage import PlayerRecord
    from .part import DiscordPart

MOST_CHOICES = 25
MOST_SHOWN = 1900  # an answer's length, Discord's limit being 2,000
DAY = 86400.0


def rank_here(interaction: discord.Interaction) -> str:
    """Their rank at its highest: someone scoped to one server is still staff, for what isn't about a server."""
    part = part_of(interaction)
    return highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)


def matching(part: DiscordPart, typed: str, most: int = MOST_CHOICES) -> list[tuple[str, str]]:
    """(their ID, the name that matched) for what's typed, each of them once: a whole ID finds them, as someone may only have that, and a name they've gone by does too."""
    if FULL_ID.fullmatch(typed.strip()):
        player_id = typed.strip().lower()
        return [(player_id, part.name_of(player_id, "That player ID"))]
    return list({player_id: name for player_id, name in reversed(part.ctx.players.like(typed, most))}.items())[::-1]


def name_shown(part: DiscordPart, player_id: str, matched: str = "") -> str:
    """The name they go by now, and the one that was typed if it's an old one. Their ID isn't how anyone knows them."""
    now = part.name_of(player_id, matched)
    return f"{now} (was {matched})" if matched and now.casefold() != matched.casefold() and matched != "That player ID" else now


def as_choices(part: DiscordPart, interaction: discord.Interaction, found: list[tuple[str, str]]) -> list[app_commands.Choice[str]]:
    """What Discord shows, as (what it says, their ID). Only two of a name get a piece of their ID, to tell them apart. Who may not see IDs gets a stand-in for what's picked."""
    sees_ids = highest_rank(part.file, someone_from(interaction.user), part.ctx.servers) in ("admin", "owner") or bool(part.file.section("ranks").get("moderators_see_ids"))
    twice = {name for name, count in Counter(label.partition(" · ")[0].casefold() for label, _ in found).items() if count > 1}
    return [app_commands.Choice(name=(f"{label} · {player_id[:8]}" if label.partition(" · ")[0].casefold() in twice else label)[:100], value=player_id if sees_ids else part.opaque(player_id)) for label, player_id in found[:MOST_CHOICES]]


async def player_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    """Players whose name has this in it. Nothing for someone who isn't staff: who's been here isn't theirs to search."""
    if not can_act(rank_here(interaction)):
        return []
    part = interaction.client.part  # type: ignore[attr-defined]
    return as_choices(part, interaction, [(name_shown(part, player_id, name), player_id) for player_id, name in matching(part, typed)])


async def flag_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    flags = interaction.client.part.file.section("notes").get("flags") or {}  # type: ignore[attr-defined]
    return [app_commands.Choice(name=str(flag)[:100], value=str(flag)[:100]) for flag in flags if typed.casefold() in str(flag).casefold()][:25]


class StaffCommands(app_commands.Group):
    def __init__(self, bot: Any, **options: Any) -> None:
        super().__init__(**options)
        self.bot = bot

    @property
    def part(self) -> DiscordPart:
        return self.bot.part

    async def _refused(self, interaction: discord.Interaction, *, moderator: bool = False) -> bool:
        if elsewhere(self.part, interaction):
            await answer(interaction, ANOTHER_DISCORD)
            return True
        rank = rank_here(interaction)
        if not can_act(rank):
            await answer(interaction, "This is for staff. An owner can add your role in `/huragok setup` → Ranks.")
            return True
        if moderator and rank == PROBATION:
            await answer(interaction, "That takes a moderator.")
            return True
        return False

    async def _player(self, interaction: discord.Interaction, text: str) -> PlayerRecord | None:
        """Who they meant: picked from the list, or typed, if only one player has gone by that."""
        found = self.part.ctx.players.find(text)
        if len(found) == 1:
            return found[0]
        await answer(interaction, f"Nobody here has gone by {clean(text)}. Pick from the list as you type." if not found else
                     f"{len(found)} players have gone by {clean(text)}. Pick the one you mean from the list as you type.")
        return None

    def _audit(self, interaction: discord.Interaction, key: str, record: PlayerRecord, **values: str) -> None:
        self.part.audit("notes", say(self.part.file, key, who=clean(named(interaction.user)), player=clean(record.last_name), **values))


class NoteCommands(StaffCommands):
    """/note: notes on a player, shared by staff."""

    def __init__(self, bot: Any) -> None:
        super().__init__(bot, name="note", description="Notes on a player, shared by staff")

    @app_commands.command(description="Write a note on a player")
    @app_commands.describe(player="Who: pick from the list as you type", text="The note", days="Days until it's dropped (0: never). Empty: the usual")
    @app_commands.autocomplete(player=player_choices)
    async def add(self, interaction: discord.Interaction, player: str, text: app_commands.Range[str, 1, 1000], days: app_commands.Range[int, 0, 3650] | None = None) -> None:
        if await self._refused(interaction):
            return
        record = await self._player(interaction, player)
        if record is None:
            return
        keep = int(self.part.file.section("notes").get("expire_days", 0) or 0) if days is None else days
        with acting(f"discord:{interaction.user.id}"):
            number = self.part.ctx.players.add_note(record.id, text, named(interaction.user), expires=time.time() + keep * DAY if keep else None)
        self._audit(interaction, "audit.note.add", record)
        await answer(interaction, f"Note #{number} on **{clean(record.last_name)}**" + (f", for {cards_counted(keep, 'day')}." if keep else "."))

    @app_commands.command(name="list", description="What staff have written about a player")
    @app_commands.describe(player="Who: pick from the list as you type")
    @app_commands.autocomplete(player=player_choices)
    async def list_notes(self, interaction: discord.Interaction, player: str) -> None:
        if await self._refused(interaction):
            return
        record = await self._player(interaction, player)
        if record is None:
            return
        players = self.part.ctx.players
        lines = [f"**{clean(record.last_name)}**"]
        lines += [f"Flagged **{clean(row['flag'])}** by {clean(row['by'])}" + (f": {clean(row['why'])}" if row["why"] else "") for row in players.flags(record.id)]
        lines += [f"#{row['number']} {clean(row['by'])}, {time.strftime('%Y-%m-%d', time.localtime(row['time']))}: {clean(row['text'])}" for row in players.notes(record.id)]
        await answer(interaction, ("\n".join(lines) if len(lines) > 1 else f"{lines[0]}: nothing written about them.")[:MOST_SHOWN], lasts="looks")

    @app_commands.command(description="Take a note away, by its number")
    @app_commands.describe(number="The note's number: /note list says")
    async def remove(self, interaction: discord.Interaction, number: app_commands.Range[int, 1]) -> None:
        if await self._refused(interaction, moderator=True):
            return
        players = self.part.ctx.players
        player_id = players.drop_note(number)
        if player_id is None:
            await answer(interaction, f"There's no note #{number}.")
            return
        record = players.get(player_id)
        if record is not None:
            self._audit(interaction, "audit.note.remove", record)
        await answer(interaction, f"Note #{number} is gone.")


class FlagCommands(StaffCommands):
    """/flag: a word on a player, from [notes] flags. Those in watch_flags tell staff when the player joins."""

    def __init__(self, bot: Any) -> None:
        super().__init__(bot, name="flag", description="Flags on a player")

    @app_commands.command(name="list", description="The flags on a player")
    @app_commands.describe(player="Who: pick from the list as you type")
    @app_commands.autocomplete(player=player_choices)
    async def list_flags(self, interaction: discord.Interaction, player: str) -> None:
        if await self._refused(interaction):
            return
        record = await self._player(interaction, player)
        if record is None:
            return
        rows = self.part.flags_of(record.id)
        lines = [f"**{clean(row['flag'])}**" + (f": {clean(row['why'])}" if row["why"] else "") for row in rows]
        await answer(interaction, f"**{clean(record.last_name)}**: " + ("\n".join(lines) if lines else "no flags."), lasts="looks")

    @app_commands.command(name="set", description="Flag a player")
    @app_commands.describe(player="Who: pick from the list as you type", flag="Which: pick from the list", why="Why, for the other staff")
    @app_commands.autocomplete(player=player_choices, flag=flag_choices)
    async def set_flag(self, interaction: discord.Interaction, player: str, flag: str, why: app_commands.Range[str, 0, 500] = "") -> None:
        if await self._refused(interaction, moderator=True):
            return
        flags = self.part.file.section("notes").get("flags") or {}
        if flag not in flags:
            await answer(interaction, f"{clean(flag)} isn't a flag here." + (f" These are: {clean(', '.join(flags))}." if flags else " None are set up."))
            return
        record = await self._player(interaction, player)
        if record is None:
            return
        with acting(f"discord:{interaction.user.id}"):
            self.part.ctx.players.set_flag(record.id, flag, why, named(interaction.user))
        self._audit(interaction, "audit.flag.set", record, flag=clean(flag))
        await answer(interaction, f"**{clean(record.last_name)}** is flagged **{clean(flag)}**.")

    @app_commands.command(description="Take a flag off a player")
    @app_commands.describe(player="Who: pick from the list as you type", flag="Which")
    @app_commands.autocomplete(player=player_choices, flag=flag_choices)
    async def clear(self, interaction: discord.Interaction, player: str, flag: str) -> None:
        if await self._refused(interaction, moderator=True):
            return
        record = await self._player(interaction, player)
        if record is None:
            return
        if not self.part.ctx.players.clear_flag(record.id, flag):
            await answer(interaction, f"**{clean(record.last_name)}** isn't flagged {clean(flag)}.")
            return
        self._audit(interaction, "audit.flag.clear", record, flag=clean(flag))
        await answer(interaction, f"**{clean(record.last_name)}** isn't flagged **{clean(flag)}** any more.")


async def server_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    names = interaction.client.part.ctx.servers  # type: ignore[attr-defined]
    return [app_commands.Choice(name=str(name)[:100], value=str(name)[:100]) for name in names if typed.casefold() in str(name).casefold()][:25]


KIND_WORDS = {"reports": "report", "calls": "a call", "appeals": "appeal", "contact": "Contact", "requests": "request", "alerts": "alert", "vergil": "strike", "watch": "watch list", "tickets": "by hand", "moderation": "action"}


class NotifyCommands(app_commands.Group):
    """/notify: being told when something's on, for any member. They're pinged where notify-told posts."""

    def __init__(self, bot: Any) -> None:
        super().__init__(name="notify", description="Get pinged when a server fills up or a game type starts")
        self.bot = bot

    @property
    def part(self) -> DiscordPart:
        return self.bot.part

    async def _refused(self, interaction: discord.Interaction) -> bool:
        if elsewhere(self.part, interaction):
            await answer(interaction, ANOTHER_DISCORD)
            return True
        return False

    async def _add(self, interaction: discord.Interaction, kind: str, value: str, server: str, said: str) -> None:
        if server and server not in self.part.ctx.servers:
            await answer(interaction, f"There's no server called {clean(server)} here.")
        elif self.part.alerts.add(str(interaction.user.id), kind, value, server):
            await answer(interaction, said)
        else:
            await answer(interaction, f"That's the most you can have: {MOST_PER_MEMBER}. `/notify stop` clears them.")

    @app_commands.command(description="Get pinged when a server reaches this many players")
    @app_commands.describe(count="How many players", server="Which server: any, if you leave it out")
    @app_commands.autocomplete(server=server_choices)
    async def players(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 128], server: str = "") -> None:
        if not await self._refused(interaction):
            await self._add(interaction, PLAYERS, str(count), server, f"You'll be pinged when {clean(server) or 'a server'} reaches {cards_counted(count, 'player')}.")

    @app_commands.command(description="Get pinged when a game type starts")
    @app_commands.describe(mode="The game type, or a word from it: Slayer, Infection", server="Which server: any, if you leave it out")
    @app_commands.autocomplete(server=server_choices)
    async def game(self, interaction: discord.Interaction, mode: app_commands.Range[str, 2, 60], server: str = "") -> None:
        if not await self._refused(interaction):
            await self._add(interaction, GAME, mode.strip(), server, f"You'll be pinged when {clean(mode)} starts on {clean(server) or 'any server'}.")

    @app_commands.command(name="list", description="What you've asked to be told")
    async def list_alerts(self, interaction: discord.Interaction) -> None:
        if await self._refused(interaction):
            return
        rows = self.part.alerts.of(str(interaction.user.id))
        lines = [(f"{clean(row['server']) or 'Any server'} reaching {row['value']} players" if row["kind"] == PLAYERS else
                  f"{clean(row['value'])} starting on {clean(row['server']) or 'any server'}") for row in rows]
        await answer(interaction, "\n".join(lines) if lines else "Nothing yet. `/notify players` or `/notify game` will do it.", lasts="looks")

    @app_commands.command(description="Stop all of them")
    async def stop(self, interaction: discord.Interaction) -> None:
        if not await self._refused(interaction):
            gone = self.part.alerts.stop(str(interaction.user.id))
            await answer(interaction, f"Stopped {cards_counted(gone, 'alert')}." if gone else "You had none.")


@app_commands.command(name="ticket", description="A card with the buttons, for any player, any time")
@app_commands.describe(player="Who: pick from the list as you type")
@app_commands.autocomplete(player=player_choices)
async def open_ticket(interaction: discord.Interaction, player: str) -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not part.tickets.can_close(rank_here(interaction)):
        await answer(interaction, f"This is for {part.file.section('tickets').get('close_rank') or 'moderator'}s and up.")
        return
    found = part.ctx.players.find(player)
    if len(found) != 1:
        await answer(interaction, f"Nobody here has gone by {clean(player)}. Pick from the list as you type." if not found else
                     f"{len(found)} players have gone by {clean(player)}. Pick the one you mean from the list as you type.")
        return
    record = found[0]
    where = next((server for server, state in part.ctx.servers.items() if state.player_by_id(record.id)), "")
    if part.tickets.open_by_hand(named(interaction.user), record.id, record.last_name, where):
        await answer(interaction, f"A ticket on **{clean(record.last_name)}** is on its way.")
    else:
        await answer(interaction, "It has nowhere to post: staff has no channel. An owner can set one in `/huragok setup` → Channels.")


@app_commands.command(name="case", description="One recorded action: what, who and why, with that player's tickets")
@app_commands.describe(number="The case number on a card or in !whois (not its ticket number)")
async def case_command(interaction: discord.Interaction, number: int) -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not can_act(rank_here(interaction)):
        await answer(interaction, "This is for staff.")
        return
    rows = part.ctx.db.query("SELECT * FROM actions WHERE number = ?", (number,))
    if not rows:
        await answer(interaction, f"There's no case {number}.")
        return
    row = rows[0]
    target = str(row["target_id"] or "")
    lines = [f"**{shown_kind(row['kind'], row['duration'], row['reason'])}** of **{clean(part.name_of(target, str(row['target_name'] or '')))}**, by {clean(str(row['by'] or '').removesuffix(' (Discord)'))} <t:{int(row['time'])}:R>",
             *([f"On {clean(row['server'])}"] if row["server"] else []), *([f"Why: {clean(row['reason'])}"] if row["reason"] else []), *([f"Length: {lasting_words(str(row['duration']))}"] if row["duration"] else [])]
    tickets = part.ctx.db.query("SELECT number, channel, message FROM discord_tickets WHERE player_id = ? AND message != '' ORDER BY number DESC LIMIT 5", (target,)) if target else []
    if tickets:
        lines.append("Tickets on them: " + ", ".join(f"[{one['number']}](https://discord.com/channels/{part.file.guild}/{one['channel']}/{one['message']})" for one in tickets))
    await answer(interaction, embed=discord.Embed(title=f"Case {number}", description="\n".join(lines)[:4000], colour=discord.Colour.blurple()), lasts="looks")


@app_commands.command(name="help", description="The commands you can use here, or what one of them does")
@app_commands.describe(command="One command, to say more about it")
async def help_command(interaction: discord.Interaction, command: str = "") -> None:
    from ..core.commandsfile import ROLES
    from .slash import NOT_FROM_DISCORD, name_of, plan_of

    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    rank = rank_here(interaction)
    names = part.file.section("commands").get("names") or {}
    known = [(name_of(spec, names), spec, role) for spec, role in part.ctx.game_commands() if spec.name not in NOT_FROM_DISCORD and spec.name not in (part.file.section("commands").get("hidden") or [])]
    if command:
        found = next((one for one in known if one[0] == command.strip().lstrip("/!").lower()), None)
        if found is None:
            await answer(interaction, f"There's no /{clean(command)}.", lasts="looks")
            return
        boxes = " ".join(f"<{option.name}>" if option.required else f"[{option.name}]" for option in plan_of(found[1]))
        await answer(interaction, f"**/{found[0]} {boxes}**".replace(" **", "**") + f"\n{clean(found[1].help)}\nFor {found[2]}s and up.", lasts="looks")
        return
    allowed = sorted(name for name, spec, role in known if (ROLES.index(rank) >= ROLES.index(role) or (rank == "probationary-moderator" and role == "moderator")) and (spec.name != "respawn" or can_act(rank)))
    wanted = features.slash_commands(part.file)
    everyone = [name for name in ("status", "report", "appeal", "notify") if name in wanted]
    staff = [name for name in ("tickets", "ticket", "case", "server", "note", "flag") if name in wanted] if can_act(rank) else []
    lines = ["You can use: " + ", ".join(f"`/{name}`" for name in sorted({*allowed, *everyone, *staff}))]
    if part.contact.on():
        lines.append("Contact the staff has a button of its own, for anything else.")
    await answer(interaction, "\n".join(lines)[:1900] + "\n`/help <command>` says more about one.", lasts="looks")


@app_commands.command(name="tickets", description="What's waiting on staff, by channel, with links")
async def tickets_command(interaction: discord.Interaction) -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
        await answer(interaction, "This is for staff.")
        return
    rows = part.ctx.db.query("SELECT * FROM discord_tickets WHERE state IN ('open', 'active') ORDER BY touched DESC")
    if not rows:
        await answer(interaction, "Nothing's waiting.", lasts="looks")
        return
    # What someone's waiting on first, then what's wrong, then the rest: 15 strikes mustn't hide a report
    first = {"reports": 0, "calls": 0, "appeals": 0, "contact": 0, "requests": 0, "alerts": 1}
    rows = sorted(rows, key=lambda row: first.get(str(row["kind"]).partition("-")[0], 2))
    lines = [f"**{cards_counted(len(rows), 'ticket')} waiting**"]
    for row in rows:
        kind = str(row["kind"]).partition("-")[0]
        said = part.tickets.lines_of(int(row["number"]))
        shown = [f"[{row['number']}]({part.tickets.link(row)})" if row["message"] else f"{row['number']} (its card hasn't got there yet)", KIND_WORDS.get(kind, kind), clean(row["player_name"] or ""), clean(row["server"] or ""),
                 re.sub(r"[*_]", "", str(said[-1]["text"]))[:80] + f", <t:{int(said[-1]['time'])}:R>" if said else f"nobody yet, <t:{int(row['opened'])}:R>", "thread" if row["thread"] else ""]
        lines.append(" · ".join(part for part in shown if part))
    text = "\n".join(lines)
    if len(text) <= 1900:
        await answer(interaction, text, lasts="looks", allowed_mentions=discord.AllowedMentions.none())
    else:
        await answer(interaction, f"**{cards_counted(len(rows), 'ticket')} waiting**: too many to show here, so it's a file.", file=discord.File(io.BytesIO(re.sub(r"\[(\d+)\]\((\S+)\)", r"\1 \2", text).encode("utf-8")), "tickets.txt"), lasts="looks", allowed_mentions=discord.AllowedMentions.none())

