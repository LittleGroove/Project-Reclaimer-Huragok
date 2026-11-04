"""What anyone in the Discord can do to reach staff: /report and a right-click on a message (reports.py has what becomes of them). None of it needs a rank, so none of it shows anyone what they couldn't already see.
Imported with client.py, in bot mode alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands, ui

from ..core.commandsfile import ROLES
from ..players.targets import FULL_ID
from . import cards
from .client import ANOTHER_DISCORD, SaysWhatWentWrong, answer, elsewhere, named, part_of, someone_from
from .ranks import can_act, highest_rank, in_game, is_faculty, lowest_rank
from .reports import ALREADY, OFF, TAKEN, TOO_MANY, TOO_SOON
from .speakers import say

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

MOST_CHOICES = 25
MOST_PROOF_BYTES = 10 * 1024 * 1024  # what any server takes: one with boosts takes more, and says so (Guild.filesize_limit)


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
    guild = part.client.get_guild(int(part.file.guild or 0)) if part.file.guild else None
    most = max(MOST_PROOF_BYTES, int(getattr(guild, "filesize_limit", 0) or 0))  # over it, the post was refused and the clip lost (audit 2026-11, 6.14)
    if proof.size > most or not kind.startswith(("image/", "video/")):  # it isn't read into memory, or posted under the bot's name: only pointed at, while Discord keeps it
        part.tickets.add_line(number, say(part.file, "reports.proof_link", who=cards.clean(who), name=cards.clean(proof.filename), link=proof.url))
        part.tickets.show(number)
        return
    channel = part.client.get_partial_messageable(int(part.tickets.keeps_in(ticket)), guild_id=int(part.file.guild or 0) or None)
    try:
        sent = await channel.send(say(part.file, "reports.proof", ticket=say(part.file, "ticket.number", number=number), who=cards.clean(who)), file=await proof.to_file(), allowed_mentions=discord.AllowedMentions.none())
        part.tickets.add_line(number, say(part.file, "reports.proof_line", name=cards.clean(proof.filename), link=sent.jump_url, who=cards.clean(who)))
    except (discord.DiscordException, OSError, aiohttp.ClientError) as error:  # a report's been filed by now, whatever became of this
        log.info("Couldn't keep what came with report %s: %s", number, error)
        part.tickets.add_line(number, say(part.file, "reports.proof_link", who=cards.clean(who), name=cards.clean(proof.filename), link=proof.url))  # pointed at, while Discord keeps it
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


def _shown_words(part: DiscordPart, how: str) -> str:
    return say(part.file, f"link.shown_{how}")


def _tell_linked(part: DiscordPart, player_id: str, member_name: str) -> None:
    """Told in game too, if they're on: it's what they asked for there."""
    from ..core.background import background
    from ..lore import callout

    async def tell() -> None:
        for server, state in list(part.ctx.servers.items()):
            player = state.player_by_id(player_id)
            if player is not None:
                try:
                    await part.ctx.tell(server, player, callout("linked", say(part.file, "link.told", member=member_name)))
                except Exception as error:  # noqa: BLE001 - they're linked whether or not they heard
                    log.debug("[%s] couldn't tell %s they're linked: %s", server, player.name, error)
                return

    background(tell(), "telling a player they're linked", log)


@app_commands.command(name="link", description="Your stats under your name here: give the code !link told you in game")
@app_commands.describe(code="The code !link told you in game", show="How you show on the stats and the leaderboards")
@app_commands.choices(show=[app_commands.Choice(name="By my name here", value="discord"),
                            app_commands.Choice(name="By my name in game", value="game"),
                            app_commands.Choice(name="By both names", value="both")])  # never off them: a spot earned is shown
async def link_command(interaction: discord.Interaction, code: str = "", show: app_commands.Choice[str] | None = None) -> None:
    await _link(interaction, code, show)


class _FacultyLinkSure(SaysWhatWentWrong, ui.View):
    """Faculty of this bot linking: the player they link is made faculty in game, so they say first that it's them
    (audit 0.6.1, 3.3: "host, type /link ABC-DEF to see my stats" made a player faculty for good)."""

    def __init__(self, part: DiscordPart, code: str, show: app_commands.Choice[str] | None) -> None:
        super().__init__(timeout=120)
        self.part, self.code, self.show = part, code, show

    @ui.button(label="Yes, that's me", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _button: ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content=say(self.part.file, "link.linking"), view=None)
        await _link(interaction, self.code, self.show, sure=True)

    @ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content=say(self.part.file, "link.not_linked_then"), view=None)


async def _link(interaction: discord.Interaction, code: str, show: app_commands.Choice[str] | None, *, sure: bool = False) -> None:
    from .links import LINKED, MEMBER_TAKEN, NO_SUCH_CODE, TOO_MANY_WRONG

    part = part_of(interaction)
    config = part.file
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not part.links.takes():
        await answer(interaction, say(config, "link.off"))
        return
    member, member_name = str(interaction.user.id), named(interaction.user)
    part.links.seen(member, member_name)
    linked_now = ""
    if code.strip():
        before = part.links.of_member(member)
        if not sure and before is None and is_faculty(config, member) and in_game(config, "faculty") == "faculty" \
                and (whose := part.links.peek(code)):
            await interaction.response.send_message(say(config, "link.faculty_sure", player=cards.clean(whose)), view=_FacultyLinkSure(part, code, show),
                                                    ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        outcome, player_id, player_name = part.links.use_code(code, member, member_name)
        if outcome == TOO_MANY_WRONG:
            await answer(interaction, say(config, "link.too_many"))
            return
        if outcome == NO_SUCH_CODE:
            await answer(interaction, say(config, "link.no_code"))
            return
        if outcome != LINKED:
            await answer(interaction, say(config, "link.member_taken" if outcome == MEMBER_TAKEN else "link.player_taken", player=cards.clean(player_name)))
            return
        linked_now = player_name
        if before is None:  # this made it
            part.audit("link", say(config, "audit.link", who=member_name, player=player_name))
            _tell_linked(part, player_id, member_name)
            if await part.links.faculty_claimed(member):  # faculty of the bot: faculty in game too
                part.audit("link", say(config, "audit.faculty", player=player_name))
    if show is not None and not part.links.show(member, show.value):
        await answer(interaction, say(config, "link.not_linked"))
        return
    mine = part.links.of_member(member)
    if mine is None:
        from .standings import counted

        minutes = counted(part.links.options.get("code_minutes", 10), 10, 1, 1440)
        await answer(interaction, say(config, "link.how", minutes=minutes))
        return
    shown = _shown_words(part, part.links.how_shown(mine))
    if linked_now:
        await answer(interaction, say(config, "link.linked", player=cards.clean(linked_now), shown=shown))
    elif show is not None:
        await answer(interaction, say(config, "link.shown_as", shown=shown))
    else:
        await answer(interaction, say(config, "link.status", player=cards.clean(part.links.player_name(mine)), shown=shown))


async def _side_player(part: DiscordPart, interaction: discord.Interaction) -> str:
    """Whose side it is: the linked player of whoever typed it, a member or up. "" after saying why not."""
    from . import sides

    config = part.file
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return ""
    if not sides.takes(part):
        await answer(interaction, say(config, "link.no_sides"))
        return ""
    mine = part.links.of_member(str(interaction.user.id)) if part.links.takes() else None
    if mine is None:
        await answer(interaction, say(config, "side.not_linked"))
        return ""
    if not sides.may_choose(part, str(mine["player_id"])):
        await answer(interaction, say(config, "side.members"))
        return ""
    return str(mine["player_id"])


@app_commands.command(name="side", description="The team you're always put on in game: Red, Blue, or wherever the game puts you")
@app_commands.describe(side="Your side")
@app_commands.choices(side=[app_commands.Choice(name="Red", value="red"), app_commands.Choice(name="Blue", value="blue"),
                            app_commands.Choice(name="Wherever the game puts me", value="off")])
async def side_command(interaction: discord.Interaction, side: app_commands.Choice[str]) -> None:
    from . import sides

    part = part_of(interaction)
    player_id = await _side_player(part, interaction)
    if not player_id:
        return
    held = sides.locked_to(part, player_id)
    if held and held != side.value:
        await answer(interaction, say(part.file, "side.locked", side=held.title()))
        return
    sides.choose(part, player_id, side.value)
    for server, state in part.ctx.servers.items():
        if state.player_by_id(player_id):
            sides.soon(part, server)
    await answer(interaction, say(part.file, "link.side_off") if side.value == "off" else say(part.file, "link.side", side=side.name))


@app_commands.command(name="togglesides", description="Stop being put on your side in game, or start again")
async def togglesides_command(interaction: discord.Interaction) -> None:
    from . import sides

    part = part_of(interaction)
    player_id = await _side_player(part, interaction)
    if not player_id:
        return
    if kept := sides.held(part, player_id):  # [sides] force_for_season
        await answer(interaction, say(part.file, "side.held", side=kept.title()))
        return
    now = sides.toggle(part, player_id)
    await answer(interaction, say(part.file, "side.none") if now is None else say(part.file, "side.on" if now else "side.off"))
    if now:  # on again: onto their side wherever they're playing (audit 0.6.1, 4.8)
        for server, state in part.ctx.servers.items():
            if state.player_by_id(player_id):
                sides.soon(part, server)


@app_commands.command(name="unlink", description="Undo your link: your stats show under your name in game again")
@app_commands.describe(member="Admins: someone else's link to undo", player="Admins: a player's link, by their name in game or ID (for someone who's left)")
async def unlink_command(interaction: discord.Interaction, member: discord.Member | None = None, player: str = "") -> None:
    part = part_of(interaction)
    config = part.file
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not part.links.takes():
        await answer(interaction, say(config, "link.off"))
        return
    mine = someone_from(interaction.user)
    matches = part.links.find_players(player) if member is None and player.strip() else []
    if len(matches) > 1:
        await answer(interaction, say(config, "link.two_named", player=cards.clean(player.strip())))
        return
    found = part.links.of_member(str(member.id)) if member is not None else matches[0] if matches else None
    if (member is not None or player.strip()) and found is None:
        await answer(interaction, say(config, "link.not_linked_them"))
        return
    someone_else = found is not None and str(found["member"]) != str(interaction.user.id)
    rank = lowest_rank(config, mine, part.ctx.servers)  # an admin on every server: one server's admin isn't everyone's (audit 0.6.1, 3.6)
    is_admin = ROLES.index(rank) >= ROLES.index("admin")
    if someone_else and not is_admin:
        await answer(interaction, say(config, "link.not_staff"))
        return
    if someone_else and found is not None and rank != "faculty" and ROLES.index(_link_rank(part, found)) >= ROLES.index(rank):
        await answer(interaction, say(config, "link.outranked"))
        return
    if not someone_else and part.links.options.get("unlink", "self") == "staff" and not is_admin:
        await answer(interaction, say(config, "link.only_staff"))
        return
    whose_id = str(found["member"]) if someone_else and found is not None else str(interaction.user.id)
    gone = part.links.unlink(whose_id)
    if gone is None:
        await answer(interaction, say(config, "link.not_linked"))
        return
    player_name = part.links.player_name(gone)
    whose = named(member) if member is not None else str(gone["member_name"] or whose_id) if someone_else else named(interaction.user)
    await part.links.faculty_unclaimed(gone)
    part.audit("link", say(config, "audit.unlink", who=named(interaction.user), member=whose, player=player_name))
    await answer(interaction, say(config, "link.unlinked_them", member=cards.clean(whose), player=cards.clean(player_name)) if someone_else
                 else say(config, "link.unlinked", player=cards.clean(player_name)))


def _link_rank(part: DiscordPart, link: Any) -> str:
    """The rank of whoever's at the other end of a link: faculty of this bot by their user ID, else their player's rank
    on every server."""
    from .rolesync import rank_on

    if is_faculty(part.file, str(link["member"])):
        return "faculty"
    return rank_on(part, str(link["player_id"]), ("*",))


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
