"""/server: a server's state and its power buttons, seen only by whoever asked. They aren't on the public board, where everyone would see them; an alert's ticket has a way here too. Each button asks first, and is on the
record. /status is the list, for anyone. Anyone with a rank can see this: the buttons are for whoever has the rank they need. Imported with client.py, in bot mode alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands, ui

from ..core.commandsfile import ROLES
from ..players.roles import a_role
from . import cards
from .client import SaysWhatWentWrong, ANOTHER_DISCORD, answer, elsewhere, named, part_of, someone_from
from .panels import Panel
from .ranks import can_act, highest_rank, rank_of
from .speakers import say
from .staff import server_choices

if TYPE_CHECKING:
    from .part import DiscordPart

log = logging.getLogger(__name__)

# What each button is: its label, whether it's a red one, and what it asks first.
POWERS = {"start": ("Start", False, "Start {server}?"), "stop": ("Stop", True, "Stop {server}? Anyone on it gets the usual countdown."),
          "restart": ("Restart", True, "Restart {server}? If anyone's on, they get the usual countdown first."), "restart-after-game": ("Restart after the game", False, "Restart {server} when this game ends?"),
          "restart-when-empty": ("Restart when empty", False, "Restart {server} once everyone has left?"), "cancel": ("Cancel what's planned", False, "Cancel the restart or stop planned for {server}?"),
          "updates": ("Check for updates", False, "Ask whether there are updates to Huragok and the server program?")}


def may_use_power(part: DiscordPart, interaction: discord.Interaction, server: str = "") -> bool:
    """On this server; with none named, on any."""
    needed = str(part.file.section("commands").get("power_rank") or "admin")
    someone = someone_from(interaction.user)
    rank = rank_of(part.file, someone, server) if server else highest_rank(part.file, someone, part.ctx.servers)
    return rank in ROLES and needed in ROLES and ROLES.index(rank) >= ROLES.index(needed)


def can_now(part: DiscordPart, server: str) -> list[str]:
    """Which of the buttons there are make sense for how this server is now: the server's own word, which the window goes by too."""
    wanted = [str(name) for name in part.file.section("commands").get("power_buttons") or []]
    row = next((row for row in part.status.rows() if row.get("name") == server), None)
    can = row.get("can") if row is not None else None
    if not isinstance(can, dict):  # nothing to say either way
        return [name for name in wanted if name in POWERS]
    running = str(row.get("phase") or "") == "running"
    allowed = {"start": can.get("start"), "stop": can.get("stop"), "restart": can.get("restart"), "restart-after-game": can.get("restart") and running, "restart-when-empty": can.get("restart") and running, "cancel": can.get("cancel"), "updates": True}
    return [name for name in wanted if name in POWERS and allowed.get(name)]


async def run_power(part: DiscordPart, server: str, action: str, who: str, why: str = "") -> tuple[bool, str]:
    """Do it. (Whether it worked, what to say.) Whoever's on reads the reason, and who asked."""
    if action == "updates":
        huragok = await part.ctx.request("check_update", {})
        program = await part.ctx.request("check_program_update", {})
        return huragok.ok and program.ok, f"{huragok.message}\n{program.message}"
    manager = part.ctx.server_manager(server)
    if manager is None:
        return False, f"There's no server called {cards.clean(server)} now."
    reason = f"{why} (requested by {who})" if why else f"requested by {who}"
    by = who
    if action == "start":
        result = manager.start()
    elif action == "stop":
        result = manager.stop(reason=reason, manual=True, by=by)
    elif action == "cancel":
        result = manager.cancel()
    else:
        when = {"restart": "now", "restart-after-game": "after_game", "restart-when-empty": "when_empty"}[action]
        result = manager.restart(reason=reason, manual=True, by=by, when=when)
    return bool(result.ok), str(result.message or "Done.")


class ServerPanel(Panel):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True  # each button asks for itself, and it's not an owner's to use

    def __init__(self, part: DiscordPart, server: str, said: str = "", *, ticket: int = 0, powers: bool = True) -> None:
        super().__init__(part)
        self.server, self.said, self.ticket = server, said, ticket
        for action in can_now(part, server) if powers else []:
            self.add_item(_Power(part, server, action, ticket))
        refresh = ui.Button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)

        async def again(interaction: discord.Interaction) -> None:
            await ServerPanel(part, server, ticket=ticket, powers=may_use_power(part, interaction, server)).open(interaction)

        refresh.callback = again  # type: ignore[method-assign]
        self.add_item(refresh)

    def card(self) -> discord.Embed:
        status = self.part.status
        row = next((row for row in status.rows() if row.get("name") == self.server), None)
        form = status.card(row) if row is not None else {"title": self.server, "description": "It isn't one of the servers now."}
        embed = discord.Embed.from_dict(form)
        if row is not None:  # the public card says how it looks from outside. Whoever's about to press something needs what it's doing
            embed.add_field(name="Now", value=cards.clean(str(row.get("status") or row.get("phase") or "?"))[:cards.MAX_FIELD], inline=False)
            why_down = str(row.get("fault_reason") or "")
            if why_down:
                retry = row.get("retry_at")
                embed.add_field(name="Why it's stopped", value=cards.clean(why_down)[:cards.MAX_FIELD - 40] + (f" Trying again <t:{int(float(retry))}:R>." if retry else " It stays off until someone starts it."), inline=False)
            for label, key in (("Planned", "planned"), ("Daily restart", "daily"), ("Waiting on", "waiting")):
                if row.get(key):
                    embed.add_field(name=label, value=cards.clean(row.get(key))[:cards.MAX_FIELD], inline=False)
            if row.get("health") not in (None, "", "healthy"):
                embed.add_field(name=f"Health: {cards.clean(row.get('health'))}", value="; ".join(cards.clean(reason) for reason in row.get("health_reasons") or [])[:cards.MAX_FIELD] or "-", inline=False)
        if self.said:
            embed.add_field(name="Just now", value=cards.clean(self.said)[:cards.MAX_FIELD])
        return embed


class _Power(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, server: str, action: str, ticket: int = 0) -> None:
        label, red, _ = POWERS[action]
        super().__init__(label=label, style=discord.ButtonStyle.danger if red else discord.ButtonStyle.secondary)
        self.part, self.server, self.action, self.ticket = part, server, action, ticket

    async def callback(self, interaction: discord.Interaction) -> None:
        if not may_use_power(self.part, interaction, self.server):
            await answer(interaction, f"That takes {a_role(str(self.part.file.section('commands').get('power_rank') or 'admin'))} or above.")
            return
        if self.action not in can_now(self.part, self.server):  # things have moved on since this was drawn
            await ServerPanel(self.part, self.server, f"{POWERS[self.action][0]} doesn't apply now: this is how it is.", ticket=self.ticket).open(interaction)
            return
        if self.action == "updates":  # it only asks, and it's about no one server
            await _finish_power(self.part, interaction, self.server, self.action, self.ticket)
            return
        await _Confirm(self.part, self.server, self.action, self.ticket).open(interaction)


class _Confirm(Panel):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True  # as ServerPanel

    def __init__(self, part: DiscordPart, server: str, action: str, ticket: int = 0) -> None:
        super().__init__(part)
        self.server, self.action = server, action
        self.add_item(_Yes(part, server, action, ticket))
        if action != "start":
            self.add_item(_YesWithReason(part, server, action, ticket))
        back = ui.Button(label="No", style=discord.ButtonStyle.secondary)

        async def go_back(interaction: discord.Interaction) -> None:
            await ServerPanel(part, server, ticket=ticket).open(interaction)

        back.callback = go_back  # type: ignore[method-assign]
        self.add_item(back)

    def card(self) -> discord.Embed:
        return discord.Embed(title=cards.clean(self.server), description=POWERS[self.action][2].format(server=cards.clean(self.server)), colour=discord.Colour.orange())


class _Yes(ui.Button[Panel]):
    def __init__(self, part: DiscordPart, server: str, action: str, ticket: int = 0) -> None:
        super().__init__(label="Yes", style=discord.ButtonStyle.danger if POWERS[action][1] else discord.ButtonStyle.primary)
        self.part, self.server, self.action, self.ticket = part, server, action, ticket

    async def callback(self, interaction: discord.Interaction) -> None:
        if not may_use_power(self.part, interaction, self.server):
            await answer(interaction, f"That takes {a_role(str(self.part.file.section('commands').get('power_rank') or 'admin'))} or above.")
            return
        await _finish_power(self.part, interaction, self.server, self.action, self.ticket)


class _YesWithReason(ui.Button[Panel]):
    """Whoever's on reads it: "requested by" alone doesn't say why they're being moved along."""

    def __init__(self, part: DiscordPart, server: str, action: str, ticket: int = 0) -> None:
        super().__init__(label="Yes, and say why", style=discord.ButtonStyle.secondary)
        self.part, self.server, self.action, self.ticket = part, server, action, ticket

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_ReasonForm(self.part, self.server, self.action, self.ticket))


class _ReasonForm(SaysWhatWentWrong, ui.Modal):
    def __init__(self, part: DiscordPart, server: str, action: str, ticket: int) -> None:
        super().__init__(title=POWERS[action][0][:45], timeout=600)
        self.part, self.server, self.action, self.ticket = part, server, action, ticket
        self.why: ui.TextInput[Any] = ui.TextInput(max_length=200)
        self.add_item(ui.Label(text="Why (everyone on will read this)", component=self.why))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not may_use_power(self.part, interaction, self.server):
            await answer(interaction, f"That takes {a_role(str(self.part.file.section('commands').get('power_rank') or 'admin'))} or above.")
            return
        await _finish_power(self.part, interaction, self.server, self.action, self.ticket, str(self.why.value).strip())


async def _finish_power(part: DiscordPart, interaction: discord.Interaction, server: str, action: str, ticket: int, why: str = "") -> None:
    await interaction.response.defer()  # asking about updates can take a while: Discord allows 3 seconds
    who = named(interaction.user)
    worked, said = await run_power(part, server, action, who, why)
    log.info("%s used %s on %s from Discord: %s", who, action, server, said)
    if worked and action != "updates":
        part.audit("power", say(part.file, "audit.power", who=who, what=POWERS[action][0].lower(), server=server))
        if ticket:  # pressed from an alert: it's that alert's story
            part.tickets.add_line(ticket, say(part.file, "ticket.power", who=cards.clean(who), what=POWERS[action][0].lower()))
            part.tickets.show(ticket)
    panel = ServerPanel(part, server, said if worked else f"Couldn't: {said}", ticket=ticket)
    await interaction.edit_original_response(embed=panel.card(), view=panel)


class _Fleet(Panel):
    """/server with several to pick from."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True  # as ServerPanel

    def __init__(self, part: DiscordPart) -> None:
        super().__init__(part)
        names = [str(name) for name in part.ctx.servers][:25]
        pick: ui.Select[Panel] = ui.Select(placeholder="Which server?" if len(part.ctx.servers) <= 25 else "The first 25: /server name: reaches any of them", options=[discord.SelectOption(label=name[:100], value=name[:100]) for name in names])

        async def picked(interaction: discord.Interaction) -> None:
            await ServerPanel(part, pick.values[0], powers=may_use_power(part, interaction, pick.values[0])).open(interaction)

        pick.callback = picked  # type: ignore[method-assign]
        self.add_item(pick)

    def card(self) -> discord.Embed:
        forms = self.part.status.summary(self.part.status.rows())
        return discord.Embed.from_dict(forms[0]) if forms else discord.Embed(title="Servers", description="There are none yet.")


def _part_of(interaction: discord.Interaction) -> Any:
    return part_of(interaction)


@app_commands.command(name="server", description="A server's state and its power buttons, for you alone")
@app_commands.describe(name="Which server")
@app_commands.autocomplete(name=server_choices)
async def server_panel(interaction: discord.Interaction, name: str = "") -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if not can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
        await answer(interaction, "This is for staff.")
        return
    servers = list(part.ctx.servers)
    if not servers:
        await answer(interaction, "There are no servers here yet.")
    elif name and name not in servers:
        await answer(interaction, f"There's no server called {cards.clean(name)} here.")
    elif not name and len(servers) != 1:
        await _Fleet(part).open(interaction)
    else:  # someone who can't see why it's down can't tell players, or who to wake. The buttons are for whoever may use them
        await ServerPanel(part, name or servers[0], powers=may_use_power(part, interaction, name or servers[0])).open(interaction)


@app_commands.command(name="status", description="How a server's doing, or all of them")
@app_commands.describe(server="Which one. Empty: all of them")
@app_commands.autocomplete(server=server_choices)
async def status_command(interaction: discord.Interaction, server: str = "") -> None:
    part = part_of(interaction)
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    rows = part.status.rows()
    row = next((one for one in rows if one.get("name") == server), None)
    if server and row is None:
        await answer(interaction, f"There's no server called {cards.clean(server)} here.")
        return
    forms = [part.status.card(row)] if row is not None else part.status.summary(rows)
    await answer(interaction, embeds=[discord.Embed.from_dict(form) for form in forms], lasts="looks")

