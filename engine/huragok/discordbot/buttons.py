"""What the buttons on a ticket do (tickets.py). Each press that acts runs the command someone would type in game (the router's run_as), so it needs the same rank, can't touch someone of the
presser's rank or above, and lands in the ledger the same way. A custom_id says all there is to know (`huragok:t:<ticket>:<action>`), and the rest is read from the database, so a button from
before a restart still works, on a card in a log channel too. Imported with client.py, in bot mode alone.

Discord may not show the bot a webhook message's own text (Message Content), so a card is always built from what's kept, never read back.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import discord

from ..core.commandsfile import PROBATION, ROLES
from ..core.events import Event
from ..core.storage import acting
from ..core.text import TIME, describe_seconds, describe_time, seconds_of
from ..players.ledger import is_housekeeping, shown_kind
from ..players.roles import a_role
from ..players.targets import FOREVER
from ..servers.state import Player
from . import cards, rest, tidy
from .client import ANOTHER_DISCORD, SaysWhatWentWrong, acting_as, answer, elsewhere, named, someone_from
from .appeals import DENIED, LIFTED, SHORTENED, USUAL
from .tickets import CLOSED
from .ranks import as_they_may_see, can_act, game_rank, highest_rank, rank_of, rank_to_run
from .reports import UPHOLDS
from .speakers import say
from .workers import to_thread

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

# The commands behind them. What's a lift runs at once; what acts asks in a form first.
LIFTS = {"unmute": "unmute", "letback": "pardon", "unban": "unban"}
# What each form asks for how long: (what's in the box before anything's typed, whether it has to be filled). A ban doesn't ask: it's for good.
HOW_LONG = {"mute": ("2h, 7d, or perm", True), "kick": ("Empty: 10m or more. Or 30m, 12h", False)}
ACTS = {*HOW_LONG, "ban", "warn"}
# What each press has to be allowed to run, so it's refused at once rather than after a form's been filled in. Lifts and looks say for themselves.
COMMAND_OF = {"mute": "mute", "kick": "kick", "ban": "ban", "warn": "warn", "strike": "strike", "pardonstrike": "pardonplayer", "banall": "banall", "unmute": "unmute", "letback": "pardon", "unban": "unban"}
# A label is only right as of when the card last changed: what would no longer make sense is said, and the card brought up to date. Mute and Kick can still be meant.
ONLY_WHILE = {"unmute": ("muted", "They aren't muted any more."), "letback": ("kicked", "Nothing's keeping them out any more."), "unban": ("banned", "They aren't banned any more.")}
ONLY_UNLESS = {"ban": ("banned", "They're banned already.")}
LOOKS = {"whois": "whois", "said": "said"}
WORDS = {"s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s", "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m", "h": "h", "hr": "h", "hrs": "h", "hour": "h",
         "hours": "h", "d": "d", "day": "d", "days": "d", "w": "w", "week": "w", "weeks": "w"}
MOST_SHOWN = 1900
THREAD_ALREADY = 160004
MOVE_SECONDS = 30.0  # a Discuss on a card in a log channel waits this long for it to be posted back


def how_long(text: str) -> str | None:
    """What was typed, as the commands take it: "7 days" and "7d" are "7d", "forever" and "perm" are "perm", nothing is "". None: it can't be read."""
    typed = text.strip().lower()
    if not typed or typed in FOREVER:
        return "perm" if typed else ""
    found = re.fullmatch(r"(\d+)\s*([a-z]+)", typed)
    return f"{found[1]}{WORDS[found[2]]}" if found and found[2] in WORDS and int(found[1]) > 0 else None


def who_pressed(part: DiscordPart, interaction: discord.Interaction) -> Player:
    """As the game and its record have them. Their real name's kept beside it, for the cards, as `shown_as` is only for what players read."""
    who = f"discord:{interaction.user.id}"
    part.real_names[who] = named(interaction.user)
    return Player(0, acting_as(part, interaction.user), who)


def _a_rank(rank: str) -> str:
    return a_role(rank)


def _hear() -> tuple[list[str], Any]:
    heard: list[str] = []

    async def tell(_server: str, _player_id: str, text: str) -> None:
        heard.append(text)

    return heard, tell


def view_of(rows: list[dict[str, Any]]) -> discord.ui.View:
    """A card's buttons as discord.py sends them. Stopped: what happens on a press is on_interaction's, not this view's."""
    view = discord.ui.View(timeout=None)
    for number, row in enumerate(rows):
        for item in row["components"]:
            if item["type"] == 3:
                view.add_item(discord.ui.Select(custom_id=item["custom_id"], placeholder=item["placeholder"], row=number,
                                                options=[discord.SelectOption(label=option["label"], value=option["value"]) for option in item["options"]]))
            else:
                view.add_item(discord.ui.Button(style=discord.ButtonStyle(item["style"]), label=item["label"], custom_id=item["custom_id"], row=number, disabled=bool(item.get("disabled"))))
    view.stop()
    return view


async def _refresh(part: DiscordPart, interaction: discord.Interaction, number: int) -> None:
    """The ticket after someone's acted on it: in place, or back where tickets are worked on, with a word to say where as it's gone from under their finger. An edit Discord won't take is only behind: the card catches up."""
    tickets = part.tickets
    was_finished = tickets.get(number)["state"] not in ("open", "active") if tickets.get(number) is not None else False

    def back_up(message_id: str) -> None:
        ticket = tickets.get(number)
        if ticket is not None and message_id:
            asyncio.ensure_future(answer(interaction, say(part.file, "ticket.moved", link=tickets.link(ticket))))

    if was_finished:
        tickets.add_line(number, say(part.file, "ticket.brought_back", who=cards.clean(named(interaction.user))))
    if tickets.touched(number, then=back_up):
        return
    ticket = tickets.get(number)
    if ticket is None:
        return
    form = tickets.edit_form(ticket)
    try:
        await interaction.edit_original_response(embed=discord.Embed.from_dict(form["embeds"][0]), view=view_of(form["components"]))
    except discord.HTTPException as error:
        log.info("Couldn't update ticket %s just now: %s", number, error)
        tickets.dirty.add(number)


# Acting ---------------------------------------------------------------------------------------------------------------------

JUST_NOW_SECONDS = 60.0  # another moderator's act this recent, and what's pressed was probably meant before it happened
JUST_DONE = {"mute": "muted", "unmute": "unmuted", "kick": "kicked", "pardon": "let back", "ban": "banned", "unban": "unbanned", "warn": "warned"}


def _strikes(part: DiscordPart, sql: str, values: tuple[Any, ...]) -> list[Any]:
    """Vergil's table, which isn't there if Vergil has never run."""
    try:
        return list(part.ctx.db.query(sql, values))
    except sqlite3.Error:
        return []


def _someone_else_now(part: DiscordPart, number: int, player_id: str) -> str:
    """Why a form can't go ahead: the card was set to another player while it was open (Who is this?), and what was
    typed was about who it showed. Sent as it was, it acted on whoever the card names now (audit 2026-11, 6.9)."""
    ticket = part.tickets.get(number)
    if ticket is None or str(ticket["player_id"]).lower() == player_id.lower():
        return ""
    return f"This card is about {cards.clean(part.name_of(str(ticket['player_id']), str(ticket['player_name'] or '')))} now, not who the form was for. Nothing was done: press again if it's still meant."


def _got_there_first(part: DiscordPart, interaction: discord.Interaction, number: int) -> str:
    """Someone else just acted on this player, so this press may be about something that's no longer so: say who, once. Pressed again, it's meant."""
    ticket = part.tickets.get(number)
    target_id = str(ticket["player_id"])
    by_id = f"discord:{interaction.user.id}"
    key = (number, by_id)
    now = time.time()
    if now - part.tickets.warned.get(key, 0.0) < JUST_NOW_SECONDS:
        part.tickets.warned.pop(key, None)
        return ""
    rows = part.ctx.db.query("SELECT by, kind, duration, reason, evidence, time FROM actions WHERE target_id = ? AND time > ? AND ok = 1 AND dry_run = 0 AND source != 'automod' AND COALESCE(by_id, '') != ? ORDER BY number DESC LIMIT 5",
                             (target_id.lower(), now - JUST_NOW_SECONDS, by_id))
    row = next((row for row in rows if not is_housekeeping(row["evidence"]) and not str(row["evidence"] or "").startswith("kicked on ") and row["kind"] in JUST_DONE), None)
    if row is None:
        strikes = _strikes(part, "SELECT by FROM automod_strikes WHERE player_id = ? AND time > ? AND source = 'manual' AND by != ? ORDER BY time DESC LIMIT 1", (target_id.lower(), now - JUST_NOW_SECONDS, acting_as(part, interaction.user)))
        if not strikes:
            return ""
        part.tickets.warned[key] = now
        return f"{cards.clean(strikes[0]['by'])} gave them a strike a moment ago. Nothing was done: press again if you mean it."
    part.tickets.warned[key] = now
    done = JUST_DONE.get(shown_kind(str(row["kind"]), row["duration"], row["reason"]), JUST_DONE[str(row["kind"])])
    return f"{cards.clean(row['by'])} {done} them a moment ago. Nothing was done: press again if you mean it."



def _ledger_lines(part: DiscordPart, target_id: str, by_id: str, after: int) -> tuple[list[tuple[float, str]], bool]:
    """What that press did, as the card tells it, from the ledger, where it's sure to be by now; and whether any of it took."""
    found: list[tuple[float, str]] = []
    took = False
    for row in part.ctx.db.query("SELECT * FROM actions WHERE target_id = ? AND by_id = ? AND number > ? ORDER BY number", (target_id, by_id, after)):
        if is_housekeeping(row["evidence"]):
            continue
        took = took or (bool(row["ok"]) and not row["dry_run"])
        event = Event("moderation.action", row["server"], {"kind": row["kind"], "target_id": row["target_id"], "target_name": row["target_name"], "by": part.real_names.get(str(row["by_id"] or ""), row["by"]), "source": row["source"],
                                                           "reason": row["reason"], "duration": row["duration"], "dry_run": bool(row["dry_run"]), "ok": bool(row["ok"]), "text": row["reply"] or "", "pending": False})
        card = cards.moderation(part.file, event)
        if card is not None:
            found.append((float(row["time"]), f"{card.line} · case {row['number']}" if part.file.section("tickets").get("case_numbers", True) else card.line))
    return found, took


async def _act(part: DiscordPart, interaction: discord.Interaction, number: int, action: str, length: str = "", reason: str = "", *, quiet: bool = False, heard_back: list[str] | None = None) -> bool:
    """Run it as theirs, and whether it took: a command can run, and the server say no or the command turn it down, and only the ledger knows. The interaction has been answered by now, with a defer.
    The card says what happened: all that's said to them alone is when it didn't. `quiet`: not that either, as the caller has more to say."""
    ctx = part.ctx
    tickets = part.tickets
    ticket = tickets.get(number)
    target_id = str(ticket["player_id"])
    name = LIFTS.get(action, action)
    why = [f'"{reason}"'] if reason else []  # one word, in quotes: a reason starting "never" or "2h" isn't a length
    words = ["perm", *why] if action == "ban" else [*([length] if length else []), *why]
    where = part.where_it_lives(target_id, ("ban",)) if action == "unban" else part.where_it_lives(target_id, ("kick", "ban")) if action == "letback" else part.where_to_run(target_id, str(ticket["server"]))  # a mute follows the player, so its lift goes where they are
    rank = rank_to_run(part.file, someone_from(interaction.user), ctx.servers, where or None, name, timed=bool(length))
    state = ctx.servers.get(where)
    live = state.player_by_id(target_id) if state is not None else None
    who_name = part.name_of(target_id, str(ticket["player_name"] or ""))
    if name == "kick" and live is None and not length:
        await answer(interaction, f"{cards.clean(who_name)} isn't on a server, so nothing was done. Give a time to keep them off.")
        return False
    by = who_pressed(part, interaction)
    heard, tell = _hear()
    if rank == PROBATION and ctx.refuses(name, live or Player(0, who_name, target_id), server=where, player=by, role=rank) == "":  # theirs is a request, as in game: on this card, and with a card of its own to answer
        refused = await ctx.run_line(name, " ".join([target_id, *words]), server=where, player=by, role=rank, tell=tell)
        if refused is None:
            part.came_from_card(by.id, number)
            tickets.add_line(number, say(part.file, "ticket.asked", who=cards.clean(named(interaction.user)), what=say(part.file, f"button.{action}").lower()))
            tickets.show(number)
        await answer(interaction, cards.clean(refused or " ".join(heard) or "Asked.")[:MOST_SHOWN])
        return False
    after = int(ctx.db.query("SELECT COALESCE(MAX(number), 0) AS n FROM actions")[0]["n"])
    tickets.expect(target_id, by.id, number)
    refused = await ctx.run_as(name, live or Player(0, who_name, target_id), words, server=where, player=by, role=rank, tell=tell)
    found, took = _ledger_lines(part, target_id, by.id, after)
    if heard_back is not None:
        heard_back += heard
    if not found:
        tickets.not_expected(target_id, by.id)
    for when, line in found:
        tickets.add_line(number, line, when=when, touch=took)
    if took:
        if name in UPHOLDS:
            part.reports.upheld(number)
        await _refresh(part, interaction, number)
    elif found:
        tickets.show(number)  # it says it didn't work, and nothing's changed
    if not took and not quiet:
        await answer(interaction, cards.clean(refused or " ".join(heard) or "That didn't go through.")[:MOST_SHOWN])
    return took


async def _strike(part: DiscordPart, interaction: discord.Interaction, number: int, rule: str, note: str) -> None:
    """A strike from the card: the ladder, as for something said in voice, where a hand-set mute wouldn't count towards anything. What it does comes back as the strike's own event, which is this card's. Only the strikes table
    can say whether it happened: the command may only have answered, with Vergil off on that server, say."""
    ticket = part.tickets.get(number)
    target_id, who_name = str(ticket["player_id"]), part.name_of(str(ticket["player_id"]), str(ticket["player_name"] or ""))
    where = part.where_to_run(target_id, str(ticket["server"]))
    state = part.ctx.servers.get(where)
    by = who_pressed(part, interaction)
    heard, tell = _hear()
    # A strike that would remove them or ban reaches every server: the rank held on all of them, as a kick with a time
    rank = rank_to_run(part.file, someone_from(interaction.user), part.ctx.servers, where or None, "strike", timed=part.ctx.strike_removes(target_id, rule, where or None))
    if rule not in part.ctx.strike_rules():  # taken off the word list, or it only reports now, since the form opened: !strike would read the list's name as part of the player's (audit 6 November)
        await answer(interaction, f"The {cards.clean(rule, for_a_channel=False)} list isn't one a strike can be given on now: it's been taken off the word list, or it only reports. Lists: "
                                  f"{cards.clean(', '.join(part.ctx.strike_rules()) or 'none', for_a_channel=False)}. Nothing was done.")
        return
    words = [rule, *([f'"{note.replace(chr(34), chr(39))}"'] if note else [])]
    if rank == PROBATION:  # theirs is a request, as in game: on this card, and with a card of its own to answer
        refused = await part.ctx.run_line("strike", " ".join([target_id, *words]), server=where, player=by, role=rank, tell=tell)
        if refused is None:
            part.came_from_card(by.id, number)
            part.tickets.add_line(number, say(part.file, "ticket.asked", who=cards.clean(named(interaction.user)), what=say(part.file, "button.strike").lower()))
            part.tickets.show(number)
        await answer(interaction, cards.clean(as_they_may_see(part.file, rank, refused or " ".join(heard) or "Asked."))[:MOST_SHOWN])
        return

    def strikes() -> int:
        return sum(int(row["n"]) for row in _strikes(part, "SELECT COUNT(*) AS n FROM automod_strikes WHERE player_id = ?", (target_id.lower(),)))

    before = strikes()
    part.tickets.expect(target_id, by.id, number)
    live = state.player_by_id(target_id) if state is not None else None
    refused = await part.ctx.run_as("strike", live or Player(0, who_name, target_id), words, server=where, player=by, role=rank, tell=tell)
    if refused or strikes() == before:
        part.tickets.not_expected(target_id, by.id)
        await answer(interaction, cards.clean(as_they_may_see(part.file, rank, refused or " ".join(heard) or "That didn't go through."))[:MOST_SHOWN])
        return
    part.reports.upheld(number)
    await _refresh(part, interaction, number)


async def _pardon_strike(part: DiscordPart, interaction: discord.Interaction, number: int, why: str) -> None:
    """A strike Vergil got wrong: off the ladder, with what it did. Lifting the mute alone would leave it counting."""
    ticket = part.tickets.get(number)
    target_id, who_name, rule = str(ticket["player_id"]), part.name_of(str(ticket["player_id"]), str(ticket["player_name"] or "")), part.tickets.rule_of(ticket)
    where = part.where_to_run(target_id, str(ticket["server"]))
    by = who_pressed(part, interaction)
    heard, tell = _hear()

    def standing() -> int:
        return sum(int(row["n"]) for row in _strikes(part, "SELECT COUNT(*) AS n FROM automod_strikes WHERE player_id = ? AND pardoned IS NULL", (target_id.lower(),)))

    before = standing()
    # A card on more than one list clears the lists of the strikes that joined it, never the player's every strike: one from before the card isn't its to pardon (audit 6 November).
    rules = [rule] if rule else part.tickets.struck_on_card(ticket)
    if not rules:
        await answer(interaction, "There are no strikes on this card to take off.")
        return
    refused = None
    for one in rules:
        refused = await part.ctx.run_as("pardonplayer", Player(0, who_name, target_id), [one, *([f'"{why}"'] if why else [])], server=where, player=by,
                                        role=game_rank(part.file, someone_from(interaction.user), where or None), tell=tell) or refused
    if refused or standing() == before:
        await answer(interaction, cards.clean(refused or " ".join(heard) or "There were no strikes to take off.")[:MOST_SHOWN])
        return
    part.tickets.add_line(number, say(part.file, "ticket.strike_pardoned", who=cards.clean(named(interaction.user)), count=cards.counted(before - standing(), "strike"), why=f": {cards.clean(why)}" if why else ""))
    await _refresh(part, interaction, number)


class ActionForm(SaysWhatWentWrong, discord.ui.Modal):
    """How long (a mute or a kick) and why. It's also the chance to think again."""

    def __init__(self, part: DiscordPart, ticket: Any, action: str) -> None:
        name = part.name_of(str(ticket["player_id"]), str(ticket["player_name"] or ""))
        super().__init__(title=f"{say(part.file, f'button.{action}')}: {name}"[:45], timeout=600)
        self.part, self.number, self.action, self.player_id = part, int(ticket["number"]), action, str(ticket["player_id"])
        self.length: discord.ui.TextInput[Any] | None = None
        if action in HOW_LONG:
            placeholder, required = HOW_LONG[action]
            if action == "kick" and not part.tickets.standing(str(ticket["player_id"]))["on"]:  # the server's own kick is only for who's there
                placeholder, required = "They've left: how long to keep them off", True
            self.length = discord.ui.TextInput(required=required, max_length=20, placeholder=placeholder)
            self.add_item(discord.ui.Label(text="How long", description="This doesn't count towards their strikes: Strike does" if action == "mute" else None, component=self.length))
        self.why = discord.ui.TextInput(required=action in ("ban", "warn"), max_length=300)  # it's what they read for good, and what an appeal shows
        self.add_item(discord.ui.Label(text="Why (they'll read this)", component=self.why))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        length = how_long(str(self.length.value)) if self.length is not None else ""
        if length is None:
            await answer(interaction, f"Couldn't read \"{cards.clean(self.length.value)}\" as how long: try 30m, 12h, 7d, or perm for good. Nothing was done.")
            return
        if why := _someone_else_now(self.part, self.number, self.player_id) or _got_there_first(self.part, interaction, self.number):
            await answer(interaction, why)
            return
        await interaction.response.defer()
        await _act(self.part, interaction, self.number, self.action, length, str(self.why.value).strip())


class NoteForm(SaysWhatWentWrong, discord.ui.Modal):
    def __init__(self, part: DiscordPart, ticket: Any) -> None:
        super().__init__(title=f"Note on {part.name_of(str(ticket['player_id']), str(ticket['player_name'] or ''))}"[:45], timeout=600)
        self.part, self.number, self.player_id, self.player_name = part, int(ticket["number"]), str(ticket["player_id"]), str(ticket["player_name"])
        self.text = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(discord.ui.Label(text="The note", component=self.text))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        part = self.part
        if not can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
            await answer(interaction, "This is for staff.")
            return
        await interaction.response.defer()
        days = int(part.file.section("notes").get("expire_days", 0) or 0)
        who = named(interaction.user)
        with acting(f"discord:{interaction.user.id}"):
            part.ctx.players.add_note(self.player_id, str(self.text.value), who, expires=time.time() + days * 86400 if days else None)
        part.tickets.add_line(self.number, say(part.file, "ticket.noted", who=cards.clean(who)))
        await _refresh(part, interaction, self.number)


class StrikeForm(SaysWhatWentWrong, discord.ui.Modal):
    """Which rule, and what for. It goes on the ladder as any strike does: for something said in voice, say, that nothing else would catch."""

    def __init__(self, part: DiscordPart, ticket: Any, rules: list[str]) -> None:
        super().__init__(title=f"Strike: {part.name_of(str(ticket['player_id']), str(ticket['player_name'] or ''))}"[:45], timeout=600)
        self.part, self.number = part, int(ticket["number"])
        self.player_id = target = str(ticket["player_id"])
        self.rule: discord.ui.Select[Any] = discord.ui.Select(options=[discord.SelectOption(label=name[:100], value=name[:100], description=part.ctx.next_strike(target, name, part.where_to_run(target, str(ticket["server"])) or None)[:100] or None) for name in rules[:25]])  # so it's known before it's done: it can reach a ban
        self.add_item(discord.ui.Label(text="Which rule", component=self.rule))
        self.note = discord.ui.TextInput(required=False, max_length=300)
        self.add_item(discord.ui.Label(text="Why (they'll read this)", component=self.note))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if why := _someone_else_now(self.part, self.number, self.player_id) or _got_there_first(self.part, interaction, self.number):
            await answer(interaction, why)
            return
        await interaction.response.defer()
        await _strike(self.part, interaction, self.number, self.rule.values[0], str(self.note.value).strip())


class PardonStrikeForm(SaysWhatWentWrong, discord.ui.Modal):
    def __init__(self, part: DiscordPart, ticket: Any) -> None:
        rule = part.tickets.rule_of(ticket)
        rules = [rule] if rule else part.tickets.struck_on_card(ticket)  # as _pardon_strike takes them off
        super().__init__(title="Pardon the strike", timeout=600)
        self.part, self.number, self.player_id = part, int(ticket["number"]), str(ticket["player_id"])
        self.why = discord.ui.TextInput(required=False, max_length=300)
        self.add_item(discord.ui.Label(text="Why", description=(f"It clears every strike on {', '.join(rules)}, and lifts what they did" if rules else "There are no strikes on this card to take off")[:100], component=self.why))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if why := _someone_else_now(self.part, self.number, self.player_id):
            await answer(interaction, why)
            return
        await interaction.response.defer()
        await _pardon_strike(self.part, interaction, self.number, str(self.why.value).strip())


class DenyForm(SaysWhatWentWrong, discord.ui.Modal):
    def __init__(self, part: DiscordPart, ticket: Any) -> None:
        super().__init__(title="Deny the request", timeout=600)
        self.part, self.number = part, int(ticket["number"])
        self.why = discord.ui.TextInput(required=False, max_length=300)
        self.add_item(discord.ui.Label(text="Why, for whoever asked", component=self.why))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await _decide(self.part, interaction, self.number, "deny", str(self.why.value).strip())


async def _decide(part: DiscordPart, interaction: discord.Interaction, number: int, command: str, why: str = "") -> None:
    """A probationary moderator's request: the game's own !confirm or !deny, which then says so on the card. What it does is that card's, not one of its own."""
    ticket = part.tickets.get(number)
    rows = part.ctx.db.query("SELECT slot, status, server FROM commands_requests WHERE number = ?", (str(ticket["problem"]).removeprefix("request:"),))
    if not rows or rows[0]["status"] != "open":  # while the bot was off, say: nothing's left to do but finish the card
        await answer(interaction, "That's been decided already.")
        part.tickets.finish(number, CLOSED)
        return
    by = who_pressed(part, interaction)
    heard, tell = _hear()
    where = part.where_to_run(preferred=str(rows[0]["server"]))
    if command == "confirm":  # what's confirmed is theirs, and this card's. A denial does nothing to anyone
        part.tickets.expect(str(ticket["player_id"]), by.id, number)
    # The rank held on every server, as /confirm and /deny take it: what's confirmed may reach them all (audit 6.8)
    rank = rank_to_run(part.file, someone_from(interaction.user), part.ctx.servers, where or None, command)
    refused = await part.ctx.run_line(command, " ".join(word for word in (str(rows[0]["slot"]), why) if word), server=where, player=by, role=rank, tell=tell)
    undecided = part.ctx.db.query("SELECT status FROM commands_requests WHERE number = ?", (str(ticket["problem"]).removeprefix("request:"),))[0]["status"] == "open"
    if refused or undecided:  # their own request, say, or the server said no: it's still there to decide, and the card can't say why
        part.tickets.not_expected(str(ticket["player_id"]), by.id)
        await answer(interaction, cards.clean(refused or " ".join(heard) or "That didn't go through.")[:MOST_SHOWN])


# Appeals --------------------------------------------------------------------------------------------------------------------

# What lifts each thing someone can appeal: the button's name for it.
APPEAL_LIFTS = {"ban": "unban", "kick": "letback", "mute": "unmute"}


async def _lift(part: DiscordPart, interaction: discord.Interaction, number: int) -> None:
    appeal = part.appeals.of_ticket(number)
    if appeal is None or appeal["outcome"]:
        await answer(interaction, "That's been decided already.")
        return
    await interaction.response.defer()
    heard: list[str] = []
    if await _act(part, interaction, number, APPEAL_LIFTS[str(appeal["about"])], heard_back=heard):
        still = next((line for line in heard if line.startswith("Still banned on")), "")
        if still:  # lifted on one, and not on another: they aren't told it's over, and it's there to finish
            part.tickets.add_line(number, cards.clean(still))
            part.tickets.show(number)
            await answer(interaction, f"{cards.clean(still)} Press Lift it again to lift that one. They haven't been told anything yet.")
            return
        part.appeals.strike_comes_off(str(appeal["player_id"]), named(interaction.user))
        part.appeals.decide(number, LIFTED, named(interaction.user))


class ShortenForm(SaysWhatWentWrong, discord.ui.Modal):
    """What's left of it, from now: what's lifted comes back with a time. Anything with a time that keeps someone off is a kick."""

    def __init__(self, part: DiscordPart, ticket: Any) -> None:
        super().__init__(title="Shorten it", timeout=600)
        self.part, self.number = part, int(ticket["number"])
        self.length: discord.ui.TextInput[Any] = discord.ui.TextInput(max_length=20, placeholder="2h, 7d")
        self.add_item(discord.ui.Label(text="How long from now", description="A kick that long, off every server", component=self.length))
        self.why = discord.ui.TextInput(required=False, max_length=300)
        self.add_item(discord.ui.Label(text="Why (they'll read this)", component=self.why))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        part, number = self.part, self.number
        length = how_long(str(self.length.value))
        if not length or length == "perm":
            await answer(interaction, f"Couldn't read \"{cards.clean(self.length.value)}\" as a time: try 12h or 7d. Nothing was done.")
            return
        appeal = part.appeals.of_ticket(number)
        if appeal is None or appeal["outcome"]:
            await answer(interaction, "That's been decided already.")
            return
        about = str(appeal["about"])
        # The rank the new kick (or mute) will be given at: a kick with a time takes the one held on every server, so
        # judged at their highest, Let back went through, the new kick was refused, and the player was let back with
        # nothing against them (audit 2026-11, 6.12)
        rank = rank_to_run(part.file, someone_from(interaction.user), part.ctx.servers,
                           part.where_to_run(str(appeal["player_id"])) or None, "mute" if about == "mute" else "kick", timed=True)
        if why_not := _cant_shorten(part, rank, str(appeal["player_id"]), about, length):  # before anything's lifted: it can't be half done
            await answer(interaction, why_not)
            return
        await interaction.response.defer()
        why = str(self.why.value).strip()
        if not await _act(part, interaction, number, APPEAL_LIFTS[about]):
            return  # it says why, and the appeal's still open
        if await _act(part, interaction, number, "mute" if about == "mute" else "kick", length, why, quiet=True):
            part.appeals.decide(number, SHORTENED, named(interaction.user), why=why, length=length)
        else:
            await answer(interaction, say(part.file, "appeals.half_done"))


def _cant_shorten(part: DiscordPart, rank: str, player_id: str, about: str, length: str) -> str:
    """Why this time won't do, while nothing's been touched. A moderator's kick has a limit, and shortening is never making it longer."""
    wanted = seconds_of(length)
    command = "mute" if about == "mute" else "kick"
    needed = next((role for known, role in part.ctx.game_commands() if known.name == command), "moderator")
    if rank == PROBATION or ROLES.index(rank) < ROLES.index(needed):  # what's lifted would stay lifted (audit 6.12)
        return (f"Shortening it puts a {command} with a time on every server, and that takes {needed} on all of them. Nothing was done."
                if command == "kick" else f"A mute takes {needed} or up. Nothing was done.")
    if about != "mute" and ROLES.index(rank) < ROLES.index("admin"):
        most = str(part.ctx.part_option("automod", "moderators_kick_at_most", "1d")).strip().lower()
        most = most if TIME.fullmatch(most) else "1d"
        if wanted > seconds_of(most):
            return f"A moderator's kick lasts {describe_time(most)} at most here, so that would leave them with nothing against them. Nothing was done."
    rows = part.ctx.db.query("SELECT time, duration FROM actions WHERE target_id = ? AND kind = ? AND ok = 1 AND dry_run = 0 ORDER BY number DESC LIMIT 1", (player_id.lower(), "mute" if about == "mute" else "ban"))
    duration = str(rows[0]["duration"] or "") if rows else ""
    if duration and TIME.fullmatch(duration):
        left = float(rows[0]["time"]) + seconds_of(duration) - time.time()
        if 0 < left <= wanted:
            return f"That's no shorter: only {describe_seconds(left)} of it is left. Nothing was done."
    return ""


class AppealDenyForm(SaysWhatWentWrong, discord.ui.Modal):
    """They're told why, so it has to say. If they won't be told, it's for the record alone."""

    def __init__(self, part: DiscordPart, ticket: Any) -> None:
        super().__init__(title="Deny the appeal", timeout=600)
        self.part, self.number = part, int(ticket["number"])
        told = part.file.section("appeals").get("tell_outcome", "dm") == "dm"
        self.why = discord.ui.TextInput(required=told, max_length=300, style=discord.TextStyle.paragraph)
        self.add_item(discord.ui.Label(text="Why (they'll read this)" if told else "Why", component=self.why))
        self.again: discord.ui.Select[Any] = discord.ui.Select(options=[discord.SelectOption(label=label, value=value, default=value == USUAL) for value, label in part.appeals.again_choices()])
        self.add_item(discord.ui.Label(text="Can they appeal this again?", description="Whoever appealed. Something new against them can always be", component=self.again))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.part.tickets.can_close(highest_rank(self.part.file, someone_from(interaction.user), self.part.ctx.servers)):
            await answer(interaction, f"That takes {_a_rank(str(self.part.file.section('tickets').get('close_rank') or 'moderator'))} or above.")
            return
        await interaction.response.defer()
        self.part.appeals.decide(self.number, DENIED, named(interaction.user), why=str(self.why.value).strip(), again=self.part.appeals.again_from(self.again.values[0] if self.again.values else USUAL))


# Looking and talking ------------------------------------------------------------------------------------------------------------


async def _look(part: DiscordPart, interaction: discord.Interaction, action: str, player_id: str) -> None:
    """Whois and Said, for anyone with a staff rank. Looking touches nothing. A probationary moderator's looks run as they do in game."""
    ctx = part.ctx
    await interaction.response.defer(ephemeral=True, thinking=True)
    record = ctx.players.get(player_id)
    where = part.where_to_run(player_id)
    if record is None or not where:
        await answer(interaction, "That player isn't in Huragok's records.")
        return
    rank = highest_rank(part.file, someone_from(interaction.user), ctx.servers)
    heard, tell = _hear()
    live = ctx.servers[where].player_by_id(player_id)
    why = await ctx.run_as(LOOKS[action], live or Player(0, record.last_name, player_id), [], server=where, player=who_pressed(part, interaction),
                           role="moderator" if rank == PROBATION else rank, tell=tell, looking=True)
    shown = cards.clean(as_they_may_see(part.file, rank, why or "\n".join(heard) or "Nothing to show."), for_a_channel=False)
    await answer(interaction, shown[:MOST_SHOWN] + ("…" if len(shown) > MOST_SHOWN else ""), lasts="looks", allowed_mentions=discord.AllowedMentions.none())


async def _thread(part: DiscordPart, interaction: discord.Interaction, number: int) -> None:
    """A thread under the card, for whoever wants one: a thread for every card would bury the channel, and Discord limits making them. Its ID is the card's. Starting one on a card in a log channel
    sends the card back first, so the talking is where the work is."""
    tickets = part.tickets
    await interaction.response.defer(ephemeral=True, thinking=True)
    ticket = tickets.get(number)
    if ticket["thread"]:
        if ticket["state"] not in ("open", "active"):  # talking about a finished card brings it back
            tickets.touched(number)
        if str(ticket["problem"]).startswith("contact:"):  # private: nobody sees it who isn't in it
            try:
                await to_thread(rest.add_thread_member, part.file.token, str(ticket["thread"]), str(interaction.user.id))
            except rest.DiscordError as error:
                log.info("Couldn't bring someone into a Contact staff thread: %s", error)
            tickets.add_line(number, say(part.file, "ticket.joined", who=cards.clean(named(interaction.user))))  # so nobody else thinks it's waiting for someone
            tickets.touched(number)
            tickets.show(number)
        await answer(interaction, f"<#{ticket['thread']}>")
        return
    posted: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def ready(message_id: str) -> None:
        if not posted.done():
            posted.set_result(message_id)

    if tickets.touched(number, then=ready):
        try:
            message_id = await asyncio.wait_for(posted, MOVE_SECONDS)
        except TimeoutError:
            await answer(interaction, "The card's on its way back, but it isn't up yet. Press Discuss again in a moment.")
            return
    else:
        message_id = str(tickets.get(number)["message"])
    ticket = tickets.get(number)
    name = f"{say(part.file, 'ticket.number', number=number)}: {ticket['player_name'] or ticket['kind']}".strip()[:100]
    try:
        await to_thread(rest.start_thread, part.file.token, str(ticket["channel"]), message_id, name)
        asyncio.ensure_future(tidy.notices(part, str(ticket["channel"]), (tidy.THREAD_STARTED,)))  # on a card that isn't the newest, Discord says so in the channel
    except rest.DiscordError as error:
        if error.code != THREAD_ALREADY:
            log.info("Couldn't make a thread on ticket %s: %s", number, error)
            await answer(interaction, "The bot can't make threads here: ask the host to give it Create Public Threads." if error.status == 403 else "Couldn't make a thread just now. Try again in a moment.")
            return
    part.ctx.db.execute("UPDATE discord_tickets SET thread = ? WHERE number = ?", (message_id, number))
    tickets.add_line(number, say(part.file, "ticket.discussing", who=cards.clean(named(interaction.user))))
    tickets.show(number)
    await answer(interaction, f"<#{message_id}>")


async def _about(part: DiscordPart, interaction: discord.Interaction, number: int) -> None:
    """A call for an admin, once staff know who it's about: the buttons then act on them."""
    values = list((interaction.data or {}).get("values") or [])
    ticket = part.tickets.get(number)
    state = part.ctx.servers.get(str(ticket["server"]))
    found = state.player_by_id(part.meant(values[0])) if values and state is not None else None
    if found is None:
        await answer(interaction, "They've left that server: pick someone who's on it.")
        return
    await interaction.response.defer()
    part.ctx.db.execute("UPDATE discord_tickets SET player_id = ?, player_name = ? WHERE number = ?", (found.id, found.name, number))
    part.tickets.add_line(number, say(part.file, "ticket.about_set", who=cards.clean(named(interaction.user)), player=f"**{cards.clean(found.name)}**"))
    await _refresh(part, interaction, number)


async def _leave(part: DiscordPart, interaction: discord.Interaction, number: int, rank: str) -> None:
    """Close, inside a Contact staff thread: for whoever opened it, and for staff."""
    ticket = part.tickets.get(number)
    if ticket is None or ticket["state"] not in ("open", "active"):
        await answer(interaction, "That's been closed already.")
    elif str(ticket["problem"]) != f"contact:{interaction.user.id}" and not part.tickets.can_close(rank):
        await answer(interaction, "That's for whoever opened this, and the staff.")
    else:
        await interaction.response.defer()
        await part.contact.close(number, named(interaction.user))


async def _ban_everywhere(part: DiscordPart, interaction: discord.Interaction, number: int) -> None:
    """A ban one server gave, on all of them: what a card about someone banned elsewhere is usually for. Run where that ban is, as it's that server's list it reads, and it only says so if it's so."""
    ticket = part.tickets.get(number)
    target_id, who_name = str(ticket["player_id"]), part.name_of(str(ticket["player_id"]), str(ticket["player_name"] or ""))
    servers = part.ctx.servers
    where = next((name for name in part.banned_on(target_id) if name in servers and servers[name].connected), part.where_to_run(target_id, str(ticket["server"])))
    by = who_pressed(part, interaction)
    heard, tell = _hear()
    refused = await part.ctx.run_as("banall", Player(0, who_name, target_id), [], server=where, player=by, role=rank_to_run(part.file, someone_from(interaction.user), servers, where or None, "banall"), tell=tell)
    listed = part.ctx.db.query("SELECT 1 FROM commands_every_server WHERE kind = 'ban' AND target = ? AND lifted IS NULL LIMIT 1", (target_id.lower(),))
    if refused or not listed:
        await answer(interaction, cards.clean(refused or " ".join(heard) or "That didn't go through.")[:MOST_SHOWN])
        return
    part.tickets.add_line(number, say(part.file, "ticket.banned_everywhere", who=cards.clean(named(interaction.user))))
    await _refresh(part, interaction, number)


async def _name_answer(part: DiscordPart, interaction: discord.Interaction, ticket: Any, action: str) -> None:
    """A name Vergil asked about: banned (names.txt, as /banname does, kicking whoever has it), or allowed, and not asked about again. Either closes the card. It takes whoever may use /banname."""
    number, name = int(ticket["number"]), str(ticket["player_name"] or "")
    if ticket["state"] not in ("open", "active"):
        await answer(interaction, "That's been answered already.")
        return
    where = part.where_to_run(preferred=str(ticket["server"]))
    if not where or not name:
        await answer(interaction, "There's no server to run that on just now." if name else "This card has no name on it to answer for.")
        return
    by = who_pressed(part, interaction)
    rank = rank_to_run(part.file, someone_from(interaction.user), part.ctx.servers, where, "banname")
    if why := part.ctx.refuses("banname", None, server=where, player=by, role=rank):
        await answer(interaction, cards.clean(why, for_a_channel=False)[:MOST_SHOWN])
        return
    await interaction.response.defer()
    who = named(interaction.user)
    if action == "allowname":
        part.ctx.allow_name(name, who, where)
        part.tickets.close(number, who, key="ticket.name_allowed")
        return
    heard, tell = _hear()
    refused = await part.ctx.run_line("banname", name, server=where, player=by, role=rank, tell=tell, prefix="/")
    banned = any(listed.strip().casefold() == name.strip().strip("*").lstrip("#").strip().casefold() for listed in part.ctx.banned_names())
    if refused or not banned:
        await answer(interaction, cards.clean(refused or " ".join(heard) or "The name wasn't banned.", for_a_channel=False)[:MOST_SHOWN])
        return
    part.tickets.close(number, who, key="ticket.name_banned")


def _no_longer(part: DiscordPart, ticket: Any, action: str) -> str:
    """What the button said was true when the card last changed. Pressed now, would it still make sense?"""
    now = part.tickets.standing(str(ticket["player_id"]))
    if action in ONLY_WHILE and not now[ONLY_WHILE[action][0]]:
        return ONLY_WHILE[action][1]
    if action in ONLY_UNLESS and now[ONLY_UNLESS[action][0]]:
        return ONLY_UNLESS[action][1]
    return ""


def _refused_now(part: DiscordPart, interaction: discord.Interaction, ticket: Any, action: str, rank: str) -> str:
    """Refused at the press, not after someone's written out why: the rank, and whether they outrank who it's for."""
    target_id = str(ticket["player_id"])
    where = part.where_to_run(target_id, str(ticket["server"]))
    if not where:
        return "There's no server to run that on just now."
    command = COMMAND_OF[action]
    return part.ctx.refuses(command, Player(0, part.name_of(target_id, str(ticket["player_name"] or "")), target_id), server=where, player=who_pressed(part, interaction), role=rank_to_run(part.file, someone_from(interaction.user), part.ctx.servers, where, command))


# A press ---------------------------------------------------------------------------------------------------------------------------


async def pressed(part: DiscordPart, interaction: discord.Interaction, custom_id: str) -> None:
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    someone = someone_from(interaction.user)
    rank = highest_rank(part.file, someone, part.ctx.servers)  # below, the rank on the card's own server, once there's a card
    pieces = custom_id.split(":")
    if custom_id.endswith(":leave") and len(pieces) == 4 and pieces[2].isdecimal():
        await _leave(part, interaction, int(pieces[2]), rank)
        return
    if not can_act(rank):
        await answer(interaction, "This is for staff. Faculty can add your role in `/huragok setup` → Ranks.")
        return
    if len(pieces) != 4 or pieces[1] != "t" or not pieces[2].isdecimal():
        await answer(interaction, "This card is from an earlier version, and its buttons no longer work.")
        return
    number, action = int(pieces[2]), pieces[3]
    ticket = part.tickets.get(number)
    if ticket is None:
        await answer(interaction, f"There's no ticket {number}.")
        return
    pressed_on = str(getattr(interaction.message, "id", "") or "")
    if pressed_on and str(ticket["message"]) and pressed_on != str(ticket["message"]) and not str(ticket["replacing"]).endswith(f":{pressed_on}"):
        await answer(interaction, "This card isn't the one for that ticket any more.")  # two Huragoks on one token, or a card that's been replaced
        return
    if str(ticket["server"]) in part.ctx.servers:
        rank = rank_of(part.file, someone, str(ticket["server"]))
        if not can_act(rank):
            await answer(interaction, "This card's server isn't one you're staff on.")
            return
    player_id = str(ticket["player_id"])
    action = "deny" if action == "refuse" else action  # as a card from before that was renamed still says
    if player_id and (why := _no_longer(part, ticket, action)):
        part.tickets.show(number)
        await answer(interaction, why)
        return
    if action in COMMAND_OF and player_id and (why := _refused_now(part, interaction, ticket, action, rank)):
        await answer(interaction, why)
        return
    if action in LOOKS and player_id:
        await _look(part, interaction, action, player_id)
    elif action == "note" and player_id:
        await interaction.response.send_modal(NoteForm(part, ticket))
    elif action == "thread":
        await _thread(part, interaction, number)
    elif action == "about":
        await _about(part, interaction, number)
    elif action == "server":
        from .serverpanel import ServerPanel, may_use_power

        panel = ServerPanel(part, str(ticket["server"]), ticket=number, powers=may_use_power(part, interaction, str(ticket["server"])))  # anyone with a rank can see why it's down
        await interaction.response.send_message(embed=panel.card(), view=panel, ephemeral=True)
    elif action == "lift":
        await _lift(part, interaction, number)
    elif action == "shorten":
        appeal = part.appeals.of_ticket(number)
        about = str(appeal["about"]) if appeal is not None else "ban"
        if why := _refused_now(part, interaction, ticket, APPEAL_LIFTS.get(about, "unban"), rank):  # it lifts first, so they need to be able to
            await answer(interaction, why)
            return
        await interaction.response.send_modal(ShortenForm(part, ticket))
    elif action == "confirm":
        await interaction.response.defer()
        await _decide(part, interaction, number, "confirm")
    elif action == "appealagain":
        if not part.tickets.can_close(rank):
            await answer(interaction, f"That takes {_a_rank(str(part.file.section('tickets').get('close_rank') or 'moderator'))} or above.")
            return
        await interaction.response.defer()
        part.appeals.let_again(number, named(interaction.user))
        await _refresh(part, interaction, number)
    elif action == "deny" and str(ticket["problem"]).startswith("appeal:"):
        if not part.tickets.can_close(rank):
            await answer(interaction, f"That takes {_a_rank(str(part.file.section('tickets').get('close_rank') or 'moderator'))} or above.")
            return
        await interaction.response.send_modal(AppealDenyForm(part, ticket))
    elif action == "deny":
        if why := part.ctx.refuses("deny", None, server=part.where_to_run(preferred=str(ticket["server"])), player=who_pressed(part, interaction), role=rank):
            await answer(interaction, why)
            return
        await interaction.response.send_modal(DenyForm(part, ticket))
    elif action in ("acknowledge", "close", "dismiss"):
        if not part.tickets.can_close(rank):
            await answer(interaction, f"That takes {_a_rank(str(part.file.section('tickets').get('close_rank') or 'moderator'))} or above.")
            return
        await interaction.response.defer()
        who = named(interaction.user)
        if action == "acknowledge" and part.tickets.still_wrong(ticket):  # /tickets saying nothing's waiting, with a server down, misleads whoever looks next. It closes itself when it's over
            part.tickets.add_line(number, say(part.file, "alert.on_it", who=cards.clean(who)))
            await _refresh(part, interaction, number)
            return
        if str(ticket["problem"]).startswith("contact:"):
            await part.contact.close(number, who)
            return
        if str(ticket["problem"]).startswith("appeal:"):
            part.appeals.closed(number, who)  # what's been done is on the card: nothing more's wanted
        if action == "dismiss":
            part.reports.dismissed(number)
        part.tickets.close(number, who, key="ticket.dismissed" if action == "dismiss" else "")
    elif action in ("banthename", "allowname"):
        await _name_answer(part, interaction, ticket, action)
    elif action == "strike" and player_id:
        rules = part.ctx.strike_rules()
        if not rules:
            await answer(interaction, "There are no rules to give a strike on: Vergil's word list is empty, or it's off.")
            return
        await interaction.response.send_modal(StrikeForm(part, ticket, rules))
    elif action == "pardonstrike" and player_id:
        await interaction.response.send_modal(PardonStrikeForm(part, ticket))
    elif action == "banall" and player_id:
        await interaction.response.defer()
        await _ban_everywhere(part, interaction, number)
    elif action in LIFTS and player_id:
        if why := _got_there_first(part, interaction, number):  # a lift has no form, so it would go at once: on a card someone else just changed, that may not be what was meant
            part.tickets.show(number)
            await answer(interaction, why)
            return
        await interaction.response.defer()
        await _act(part, interaction, number, action)
    elif action in ACTS and player_id:
        await interaction.response.send_modal(ActionForm(part, ticket, action))
    else:
        await answer(interaction, "This button isn't one this version knows.")
