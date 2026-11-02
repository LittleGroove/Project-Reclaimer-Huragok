"""What anyone in the Discord can do to reach staff: /report and a right-click on a message (reports.py has what becomes of them). None of it needs a rank, so none of it shows anyone what they couldn't already see.
Imported with client.py, in bot mode alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands, ui

from ..players.targets import FULL_ID
from . import cards
from .client import ANOTHER_DISCORD, SaysWhatWentWrong, answer, elsewhere, named, part_of, someone_from
from .ranks import can_act, highest_rank
from .reports import ALREADY, OFF, TAKEN, TOO_MANY, TOO_SOON
from .speakers import say

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

MOST_CHOICES = 25
MOST_PROOF_BYTES = 25_000_000


def _part_of(interaction: discord.Interaction) -> Any:
    return part_of(interaction)


def _elsewhere(part: DiscordPart, interaction: discord.Interaction) -> bool:
    return bool(part.file.guild) and str(interaction.guild_id) != part.file.guild


async def on_now(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    """Whoever's on a server now: anyone in a game can see that. Who's been here isn't everyone's to search, so anyone else has to be typed in full."""
    part = part_of(interaction)
    found = [(player.name, player.id) for state in part.ctx.servers.values() for player in state.players.values() if player.name.strip() and typed.casefold() in player.name.casefold()]  # a blank name would stop the whole list
    return [app_commands.Choice(name=str(name)[:100], value=part.opaque(str(player_id))) for name, player_id in sorted(found)[:MOST_CHOICES]]


def who_is_meant(part: DiscordPart, typed: str) -> tuple[str, str, str | None, bool]:
    """(their ID, their name, the server they're on, whether several go by that name) for what was picked or typed; no ID if nobody can be said to be meant."""
    wanted = part.meant(typed)
    on = [(str(player.id), str(player.name), str(server)) for server, state in part.ctx.servers.items() for player in state.players.values()]
    picked = [one for one in on if one[0] == wanted.lower()]
    named = picked or [one for one in on if one[1].casefold() == wanted.casefold()]
    if named:
        several = len({one[0] for one in named}) > 1
        return ("", wanted, None, True) if several else (*named[0], False)
    if FULL_ID.fullmatch(wanted):
        record = part.ctx.players.get(wanted.lower())
        return (wanted.lower(), str(record.last_name), None, False) if record is not None else ("", wanted, None, False)
    seen = dict(part.ctx.players.named(wanted))
    if len(seen) > 1:
        return "", wanted, None, True
    return (*next(iter(seen.items())), None, False) if seen else ("", wanted, None, False)


def _answer_of(part: DiscordPart, outcome: str, name: str, reporter: str = "") -> str:
    config = part.file
    if outcome == TOO_SOON:
        return say(config, "reports.too_soon", when=f"<t:{int(part.reports.next_allowed(reporter))}:R>")
    if outcome == TOO_MANY:
        return say(config, "reports.too_many")
    if outcome == ALREADY:
        return say(config, "reports.already", player=cards.clean(name) or "that")
    if outcome == OFF:
        return say(config, "reports.off")
    return say(config, "reports.thanks") if config.section("reports").get("thank_reporter", True) else say(config, "reports.sent")


async def keep_proof(part: DiscordPart, proof: discord.Attachment, number: int, who: str) -> None:
    """What's attached to a command is only there for a while, so it's posted where the ticket is, for good, and the card links to it: cards move, and that message doesn't."""
    ticket = part.tickets.get(number)
    kind = str(proof.content_type or "")
    if proof.size > MOST_PROOF_BYTES or not kind.startswith(("image/", "video/")):  # it isn't read into memory, or posted under the bot's name: only pointed at, while Discord keeps it
        part.tickets.add_line(number, say(part.file, "reports.proof_link", who=cards.clean(who), name=cards.clean(proof.filename), link=proof.url))
        part.tickets.show(number)
        return
    channel = part.client.get_partial_messageable(int(part.tickets.keeps_in(ticket)), guild_id=int(part.file.guild or 0) or None)
    try:
        sent = await channel.send(say(part.file, "reports.proof", ticket=say(part.file, "ticket.number", number=number), who=cards.clean(who)), file=await proof.to_file(), allowed_mentions=discord.AllowedMentions.none())
        part.tickets.add_line(number, say(part.file, "reports.proof_line", name=cards.clean(proof.filename), link=sent.jump_url, who=cards.clean(who)))
    except (discord.DiscordException, OSError, aiohttp.ClientError) as error:  # a report's been filed by now, whatever became of this
        log.info("Couldn't keep what came with report %s: %s", number, error)
        part.tickets.add_line(number, say(part.file, "reports.proof_lost", who=cards.clean(who)))
    part.tickets.show(number)


@app_commands.command(name="report", description="Tell the staff about a player")
@app_commands.describe(player="Who: pick someone who's on now, or type their name in full", why="What they did", proof="A clip or a screenshot")
@app_commands.autocomplete(player=on_now)
async def report_command(interaction: discord.Interaction, player: str, why: str = "", proof: discord.Attachment | None = None) -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    player_id, name, server, several = who_is_meant(part, player)
    if not player_id:
        await answer(interaction, say(part.file, "reports.several" if several else "reports.no_such_player", player=cards.clean(name)))
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    who = named(interaction.user)
    outcome, number = part.reports.make(f"discord:{interaction.user.id}", who, player_id, name, server, why.strip(), "discord")
    if outcome in (TAKEN, ALREADY) and proof is not None:  # sent second is still the only evidence of a voice there can be
        await keep_proof(part, proof, number, who)
    await answer(interaction, say(part.file, "reports.added_answer") if outcome == ALREADY and (why.strip() or proof is not None) else _answer_of(part, outcome, name, f"discord:{interaction.user.id}"))


class _ReportMessage(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, reported: discord.Message) -> None:
        super().__init__(title=say(part.file, "reports.form_title")[:45], timeout=600)
        self.part, self.reported = part, reported
        self.why: ui.TextInput[Any] = ui.TextInput(required=False, max_length=300)
        self.add_item(ui.Label(text=say(part.file, "reports.form_why")[:45], component=self.why))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        message = self.reported
        outcome, _ = self.part.reports.message_reported(f"discord:{interaction.user.id}", named(interaction.user), message.author.display_name, getattr(message.channel, "name", "?"), message.jump_url, message.content, str(self.why.value).strip())
        await answer(interaction, _answer_of(self.part, outcome, "", f"discord:{interaction.user.id}"))


@app_commands.context_menu(name="Report message")
async def report_message(interaction: discord.Interaction, message: discord.Message) -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
    elif not part.reports.takes("discord"):
        await answer(interaction, say(part.file, "reports.off"))
    else:
        await interaction.response.send_modal(_ReportMessage(part, message))


def _already_appealed(part: DiscordPart, interaction: discord.Interaction, player_id: str, name: str) -> str:
    """Why not, when: nobody can see the card it's about, but anyone with a rank can, and may not know it's there."""
    number = part.appeals.open_one(player_id)
    appeal = part.appeals.of_ticket(number)
    text = say(part.file, "appeals.already", player=cards.clean(name), when=f"<t:{int(appeal['time'])}:R>" if appeal is not None else "a while back")
    ticket = part.tickets.get(number)
    if ticket is not None and can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
        text += " " + say(part.file, "appeals.already_staff", ticket=f"[{say(part.file, 'ticket.number', number=number)}]({part.tickets.link(ticket)})")
    return text


class _AppealForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, player_id: str, name: str, about: str, proved: bool = False) -> None:
        super().__init__(title=say(part.file, "appeals.form_title")[:45], timeout=900)
        self.part, self.player_id, self.name, self.about, self.proved = part, player_id, name, about, proved
        self.answers: list[tuple[str, ui.TextInput[Any]]] = []
        for question in part.appeals.questions():
            box: ui.TextInput[Any] = ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000)
            self.answers.append((question, box))
            self.add_item(ui.Label(text=question[:45], component=box))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        part = self.part
        if part.appeals.open_one(self.player_id) and not self.proved:  # someone got there while this was being written
            await answer(interaction, _already_appealed(part, interaction, self.player_id, self.name))
            return
        number = part.appeals.file(str(interaction.user.id), named(interaction.user), self.player_id, self.name, self.about, [(question, str(box.value).strip()) for question, box in self.answers], proved=self.proved)
        told = part.file.section("appeals").get("tell_outcome", "dm") == "dm"
        await answer(interaction, say(part.file, "appeals.sent") + (" " + say(part.file, "appeals.will_hear") if told else "") if number else say(part.file, "appeals.nowhere"))


@app_commands.command(name="appeal", description="Ask the staff to lift what's against you")
@app_commands.describe(who="Your name in game, in full, your player ID, or a code if your ban's message gave you one")
async def appeal_command(interaction: discord.Interaction, who: str) -> None:
    part = part_of(interaction)
    config = part.file
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not part.appeals.takes():
        await answer(interaction, say(config, "appeals.off"))
        return
    player_id, name = part.appeals.find(who)
    proved = bool(part.appeals.find_code(who)[0])
    if player_id and not proved and part.appeals.codes_used() and config.section("appeals").get("code_only"):
        await answer(interaction, say(config, "appeals.code_needed"))
        return
    about = part.appeals.what_stands(player_id) if player_id else ""
    if not about:
        await answer(interaction, say(config, "appeals.not_here", what=say(config, f"appeals.about_{other}")) if player_id and (other := part.appeals.what_else_stands(player_id)) else say(config, "appeals.nothing", who=cards.clean(who)))
    elif part.appeals.open_one(player_id) and not proved:
        await answer(interaction, _already_appealed(part, interaction, player_id, name))
    elif part.appeals.no_more(str(interaction.user.id), player_id, about):
        await answer(interaction, say(config, "appeals.no_more"))
    elif allowed := part.appeals.next_allowed(str(interaction.user.id), player_id, about):
        await answer(interaction, say(config, "appeals.too_soon", when=f"<t:{int(allowed)}:R>"))
    else:
        await interaction.response.send_modal(_AppealForm(part, player_id, name, about, proved))


class _ContactForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart) -> None:
        super().__init__(title=say(part.file, "contact.title")[:45], timeout=900)
        self.part = part
        categories = list(dict.fromkeys(str(category).strip()[:100] for category in part.contact.options.get("categories") or [] if str(category).strip()))[:25]  # Discord refuses two the same
        self.category: ui.Select[Any] | None = ui.Select(options=[discord.SelectOption(label=category, value=category) for category in categories]) if categories else None
        if self.category is not None:
            self.add_item(ui.Label(text=say(part.file, "contact.form_category")[:45], component=self.category))
        self.text: ui.TextInput[Any] = ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(ui.Label(text=say(part.file, "contact.what")[:45], component=self.text))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        part = self.part
        if ongoing := part.contact.open_one(str(interaction.user.id)):  # a second press, from another device, or before the first form's answer came
            await answer(interaction, say(part.file, "contact.already", thread=f"<#{ongoing['thread']}>"))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        category = self.category.values[0] if self.category is not None and self.category.values else ""
        thread = await part.contact.open(str(interaction.user.id), named(interaction.user), category, str(self.text.value).strip())
        await answer(interaction, say(part.file, "contact.sent", thread=f"<#{thread}>") if thread else say(part.file, "contact.failed"))


async def contact_pressed(part: DiscordPart, interaction: discord.Interaction) -> None:
    """The button: one thread at a time for each person."""
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
    elif not part.contact.on():
        await answer(interaction, say(part.file, "contact.off"))
    elif ongoing := part.contact.open_one(str(interaction.user.id)):
        await answer(interaction, say(part.file, "contact.already", thread=f"<#{ongoing['thread']}>"))
    elif allowed := part.contact.next_allowed(str(interaction.user.id)):
        await answer(interaction, say(part.file, "contact.too_soon", when=f"<t:{int(allowed)}:R>"))
    else:
        await interaction.response.send_modal(_ContactForm(part))
