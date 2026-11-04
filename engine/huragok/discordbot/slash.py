"""Every in-game command as a slash command, made from the commands themselves so the two can't drift apart. Each runs through the router as if it had been typed in game (run_line): the same rank from
commands.toml, the same guards, and a probationary moderator's the same requests. Imported with client.py, in bot mode alone.

The options come from a command's usage line, one box to a thing: `<player> [time] [reason]` is player, time and reason. So what's written in one box can't be read as another: a reason starting "never" or
"2h" is only a reason. Answers are private unless `[commands] public` lists the command, since the feeds keep the shared record, and they stay out of the way (`[answers]`).

Their list is long, and most of it is staff's, so those only show to members with `[commands] staff_permission` (Discord can't show commands by anything of Huragok's). Rank is what decides, whatever shows.
"""

from __future__ import annotations

import inspect
import io
import logging
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands, ui

from ..core.commandsfile import PROBATION, ROLES
from ..players.router import LOOKING, role_plural, split_words
from ..players.targets import FULL_ID, NUMBER, PICK, ago
from ..servers.state import Player
from . import cards, tidy
from .client import ANOTHER_DISCORD, SaysWhatWentWrong, acting_as, answer, elsewhere, named, part_of, someone_from
from .ranks import Someone, as_they_may_see, can_act, game_rank, highest_rank, in_game, rank_to_run, times_a_kick
from .speakers import say
from .staff import as_choices, matching, name_shown, server_choices

if TYPE_CHECKING:
    from ..core.part import CommandSpec
    from .part import DiscordPart

log = logging.getLogger(__name__)

# These go where the thing was given, which for a /ban in game is the only server that has it. A mute follows the player: its lift goes where they are.
LIFTING = {"unban": ("ban",), "pardon": ("kick", "ban")}
# What only makes sense from in a game, or has a better form in Discord. /strikes means "mine", and until an account is linked to a player nobody here has any.
NOT_FROM_DISCORD = {"help", "calladmin", "report", "reports", "strikes", "link", "stats", "side", "togglesides"}  # /tickets is !reports' better form
TAKEN = {"huragok", "note", "flag", "notify", "ticket", "report", "appeal", "server", "status", "case", "help", "tickets", "link", "unlink", "stats", "leaderboard", "side", "togglesides"}  # Discord's own
MOST_SHOWN = 1900
# What's asked of the whole of Huragok, so no server's needed; and what's about each server, so leaving it out answers for them all.
EVERYWHERE = {"mods", "rules", "bannames", "roles", "requests", "confirm", "deny", "banword", "unbanword", "changebanword", "admin", "whois", "said", "promote", "demote", "banname", "unbanname", "pardonplayer", "season", "newseason", "endseason"}
EACH_SERVER = {"players", "health", "kicks", "bans", "mutes", "novotes"}
# What to offer as you type, for the commands where that isn't everyone: who that command can do anything for. It's only what's offered: anyone can still be typed in, by name or ID, and the command says why not.
ONLY_IF = {"unban": ("banned",), "pardon": ("kicked", "banned"), "unmute": ("muted",), "allowvote": ("novoted",), "pardonplayer": ("struck",), "demote": ("ranked",)}
UNLESS = {"ban": ("banned",), "mute": ("muted",), "novote": ("novoted",)}
ON_NOW_ONLY = {"warn", "team", "respawn"}  # it's said to them, or done to them, in a game
ON_NOW_FIRST = {"kick", "mute", "strike", "ban", "novote"}
STANDING_WORDS = {"banned": "banned", "kicked": "kicked", "muted": "muted", "novoted": "no votes", "struck": "has strikes"}
MOST_LOOKED_AT = 300  # of those who might stand that way, the latest first: it has three seconds to answer
MOST_CHOICES = 25
# Where a name that's fine in game would mislead here. [commands] names overrules them: { rules = "rules" } is the game's name back.
NAMED_FOR_DISCORD = {"rules": "bannedwords", "bannames": "bannednames", "pardon": "letback", "pardonplayer": "clearstrikes", "reload": "reloadserver"}
# What Discord shows of a command whose help is written for typing it in game.
DESCRIBED = {"banword": "Add words to a list of the word list, or make a new list with its mutes, kicks and bans",
             "changebanword": "New steps for a list of the word list: its mutes, kicks and bans",
             "unbanword": "Take words off the word list, or a whole list",
             "rules": "Every list of the word list: its words, and what each strike does",
             "bannames": "Every banned name, and the names the staff allowed"}
# The word list's commands, whose parts are a line with bars in game (words | mutes | kicks | ban | name): here, a box each, so they can be filled in any order.
BARS = {"banword": ("words", "mutes", "kicks", "bans", "listname"), "changebanword": ("listname", "mutes", "kicks", "bans")}
PAGED = {"rules", "bannames"}  # lists shown whole however long, over as many messages as it takes, not as a file
TOKEN = re.compile(r"<[^<>]*>|\[(?:[^\[\]]|\[[^\[\]]*\])*\]")
TIME_SUGGESTED = ("10m", "30m", "1h", "12h", "1d", "7d", "30d", "perm")
MUTES_HINT = "Each mute's time from the 1st strike: 1d, 3d, 7d. A number last: strikes before muted for good"
KICKS_HINT = "Strikes before the kicks, then each kick's time: 2, 1h, 1d. 0 alone: the server's kick every time"
BANS_HINT = "Strikes before the ban for good: 0 bans the first time. Mutes, kicks, bans all empty: only reports"
RESTART_SUGGESTED = ("gameover", "5m", "10m", "30m", "1h")


@dataclass(frozen=True)
class Option:
    name: str
    description: str
    required: bool = False
    kind: str = "text"  # player, time, reason, number, server, one (one thing, however many words), list (the word list's), listed (its words) or text
    choices: tuple[str, ...] = ()


# Those whose usage says more than a list of boxes can.
SHAPES = {"admin": [Option("action", "What to do", True, choices=("list", "add", "remove")), Option("player", "Who: for add and remove", kind="player")],
          "startvote": [Option("vote", "What the vote's for", True, choices=("endround", "endgame", "shuffle", "kick", "playlist")), Option("player", "Who: for a kick vote", kind="player")],
          "nextmap": [Option("map", "The map. Empty: shows what's next", kind="one"), Option("mode", "The mode", kind="one")],
          "respawn": [Option("player", "Who: pick from the list as you type", True, "player")],
          "banword": [Option("words", "The words, separated by commas: badword, otherword. word* also catches longer words", True),
                      Option("mutes", MUTES_HINT), Option("kicks", KICKS_HINT), Option("bans", BANS_HINT),
                      Option("listname", "The list: one there already takes the words with its steps; a new name makes a list", kind="list")],
          "changebanword": [Option("listname", "The list: its name, or a word on it", True, "list"), Option("mutes", MUTES_HINT), Option("kicks", KICKS_HINT), Option("bans", BANS_HINT)],
          "unbanword": [Option("words", "The words to take off, separated by commas. Empty, with a listname: the whole list", kind="listed"),
                        Option("listname", "Only off this list. With no words: the whole list goes", kind="list")]}


def _one(spec: CommandSpec, token: str, first: bool) -> Option:
    required = token.startswith("<")
    inner = token[1:-1].strip()
    words = [word.strip().strip("<>[]").strip() for word in inner.split("|")]
    head = words[0].lower()
    if first and (bool(spec.player) or head in ("player", "@number")):
        return Option("player", "Who: pick from the list as you type", required or bool(spec.player and not token.startswith("[")), "player")
    if head == "time":
        hint = "How long: 30m, 2h, 7d, or perm for good" if "perm" in words else "When: 7m, or gameover" if "gameover" in words else "How long: 30m, 2h, 7d"
        return Option("time", hint, required, "time")
    if words == ["on", "off"]:
        return Option("state", "On or off. Empty: the other way from now", required, choices=("on", "off"))
    if head in ("why", "reason", "note"):
        return Option("reason", "Why", required, "reason")
    if head == "lines":
        return Option("lines", "How many lines", required, "number")
    if head == "number":
        return Option("number", "The request's number, from /requests", required, "number")
    if head in ("map", "mode"):
        return Option(head, f"The {head}", required, "one")
    if re.fullmatch(r"\d+-\d+", head):
        return Option("teams", f"How many teams, {head.replace('-', ' to ')}. Empty: shows them", required, "number")
    if "red" in words:
        return Option("team", "Which team", required, choices=tuple(words))
    if head == "rule":
        return Option("rule", "Which rule: its name, or all" if "all" in words else "Which rule: its name, or name to ban their name", required)
    if head == "line":
        return Option("search", "Part of a rule's name or a word. Empty: all of it", required)
    if head == "milliseconds":
        return Option("limit", "Milliseconds, or off. Empty: shows it", required)
    if head.startswith("command"):
        return Option("instead", "A command and its words, to do that instead", required)
    name = re.sub(r"[^a-z0-9_-]", "", head)[:32] or "words"
    return Option(name, {"name": "Part of a name" if not required else "The name", "text": "What to say", "word": "The word"}.get(name, inner[:100] or "Which"), required)


def plan_of(spec: CommandSpec) -> list[Option]:
    """The boxes a command gets, required ones first as Discord insists."""
    if spec.name in SHAPES:
        return list(SHAPES[spec.name])
    tokens = TOKEN.findall(spec.usage)
    if TOKEN.sub("", spec.usage).strip():  # alternatives, as in "<words> <line> | <words> | [mutes]": more than boxes can say
        return [Option("words", spec.usage[:100], spec.usage.startswith("<"))]
    made: list[Option] = []
    for token in tokens:
        option = _one(spec, token, not made)
        if option.name not in {one.name for one in made}:
            made.append(option)
    return sorted(made, key=lambda option: not option.required)


def rank_for(part: DiscordPart, spec: CommandSpec, someone: Someone, servers: list[str], *, timed: bool = False) -> str:
    """What they can do with this command. What reaches every server takes the rank they have on all of them: a rank on one server isn't a rank on the others. A look only needs it somewhere."""
    if spec.name in LOOKING:
        return in_game(part.file, highest_rank(part.file, someone, part.ctx.servers))
    return rank_to_run(part.file, someone, part.ctx.servers, servers[0] if servers else None, spec.name, timed=timed)


def description_of(spec: CommandSpec) -> str:
    """What Discord shows, up to 100 characters: its first sentence if that fits, or cut between words."""
    text = (spec.help or spec.name).strip()
    first = re.split(r"(?<=[.!?:;])\s", text, maxsplit=1)[0].rstrip(":;")
    if len(first) <= 100:
        return first
    return text[:99].rsplit(" ", 1)[0] + "…"


def name_of(spec: CommandSpec, names: dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9_-]", "", str(names.get(spec.name) or NAMED_FOR_DISCORD.get(spec.name) or spec.name).lower())[:32] or spec.name


def _server_for(part: DiscordPart, spec: CommandSpec, given: str, player: str, asker: str = "") -> tuple[list[str], str]:
    """The servers a command is for, and why it couldn't tell if there are none. What can't be told isn't guessed: a stranger on another server shouldn't be who gets banned."""
    servers = part.ctx.servers
    if not servers:
        return [], "There are no servers here to run that on."
    if given:
        return ([given], "") if given in servers else ([], f"There's no server called {cards.clean(given)} here.")
    if spec.name in LIFTING and (pick := PICK.fullmatch(player)):  # someone from the list they were just given: the lift goes where theirs was given
        entry = part.ctx.pick_of(asker, int(pick[1]))
        if not isinstance(entry, str) and getattr(entry, "id", ""):
            return [part.where_it_lives(str(entry.id), LIFTING[spec.name])], ""
    if spec.name in EVERYWHERE:
        return [part.where_to_run(player)] if servers else [], ""
    up = {name: state for name, state in servers.items() if state.connected}
    if spec.name in EACH_SERVER:
        return list(up) or list(servers), ""
    if player:
        if spec.name in LIFTING:
            return [part.where_it_lives(player, LIFTING[spec.name])], ""
        named = {someone.id: name for name, state in up.items() for someone in state.players.values() if someone.name.casefold() == player.casefold()}
        if len(named) > 1:
            return [], f"More than one player on the servers goes by {cards.clean(player)}: pick the one you mean from the list as you type, or say which `server`."
        if named:
            return [next(iter(named.values()))], ""
        if on := next((name for name, state in up.items() if state.player_by_id(player.lower())), ""):
            return [on], ""
        if NUMBER.fullmatch(player) and len(up) > 1:
            return [], "A #number is different on each server: say which `server`."
        return [part.where_to_run(player)], ""  # someone who's left is the same to every server
    if len(servers) == 1:
        return list(servers), ""
    return [], "Which server? Pick one in the command's `server` option."


def _barred(name: str, given: dict[str, Any]) -> tuple[str, str]:
    """The word list's commands as the game takes them, a part to each bar, whatever order the boxes were filled in: (the line, or why not)."""
    parts = {box: str(given.get(box) or "").strip() for box in (*BARS.get(name, ()), "words", "listname")}
    if bad := next((box for box, text in parts.items() if "|" in text), None):
        return "", f"`{bad}` can't have a | in it: each part has a box of its own here. Nothing was done."
    if name == "unbanword":
        return parts["words"] + (f" | {parts['listname']}" if parts["listname"] else ""), ""
    return " | ".join(parts[box] for box in BARS[name]), ""


def _line_of(part: DiscordPart, plan: list[Option], given: dict[str, Any], name: str = "") -> tuple[str, str]:
    """What it comes to as a line in game: (the line, or why not). Each box is what it says: the reason in quotes, so nothing in it can be read as something else."""
    from .buttons import how_long

    pieces: list[str] = []
    for option in plan:
        value = given.get(option.name)
        text = str(value).strip() if value not in (None, "", 0) else ""
        if not text or option.kind == "server":
            continue
        if option.kind == "time":
            known = how_long(text)
            restart = name == "restartserver"
            if restart and (known == "perm" or (known is None and text.lower() != "gameover")):  # its time is when, and it has no "for good"
                return "", f"Couldn't read \"{cards.clean(text)}\" as when: try 7m, or gameover. Nothing was done."
            if known is None and not restart:
                return "", f"Couldn't read \"{cards.clean(text)}\" as how long: try 30m, 12h or 7d, or perm for good. Nothing was done."
            text = known or text.lower()
        elif option.kind == "reason":
            text = '"' + text.replace('"', "'") + '"'
        elif " " in text and (option.kind == "one" or (option.kind == "player" and any(given.get(later.name) for later in plan[plan.index(option) + 1:] if later.kind != "server"))):
            text = '"' + text.replace('"', "'") + '"'  # one thing, with something after it
        pieces.append(text)
    return " ".join(pieces), ""


class _Sure(SaysWhatWentWrong, ui.View):
    """What the console asks before it does, asked here too."""

    def __init__(self, part: DiscordPart, spec: CommandSpec, line: str, servers: list[str], private: bool, timed: bool = False) -> None:
        super().__init__(timeout=120)
        self.part, self.spec, self.line, self.servers, self.private, self.timed = part, spec, line, servers, private, timed

    @ui.button(label="Yes", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _button: ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content=say(self.part.file, "console.sending", line=f"`{self.spec.name} {self.part.named_in(self.line)}`"[:300], server=cards.clean(", ".join(self.servers))), view=None)
        await _go(self.part, interaction, self.spec, self.line, self.servers, self.private, timed=self.timed)

    @ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: ui.Button[Any]) -> None:
        self.stop()
        await interaction.response.edit_message(content=say(self.part.file, "console.not_sent"), view=None)


async def run(part: DiscordPart, interaction: discord.Interaction, spec: CommandSpec, plan: list[Option], given: dict[str, Any]) -> None:
    """As if they'd typed it in game, with the answer here instead. What can be refused is, before anything's shown: a refusal is theirs alone, whatever the answer would have been."""
    if elsewhere(part, interaction):
        await answer(interaction, ANOTHER_DISCORD)
        return
    if given.get("player"):
        given = {**given, "player": part.meant(str(given["player"]))}
    player = str(given.get("player") or "").strip()
    if spec.name == "banname" and FULL_ID.fullmatch(player):  # picked from the list: it's their name that's banned
        record = part.ctx.players.get(player.lower())
        given = {**given, "player": str(record.last_name) if record is not None else player}
    line, why = _barred(spec.name, given) if spec.name in (*BARS, "unbanword") else _line_of(part, plan, given, spec.name)
    if why:
        await answer(interaction, why)
        return
    if spec.name == "banword" and not str(given.get("listname") or "").strip():  # a line with no name is the game's, where it's typed with bars
        lists = ", ".join(cards.clean(name, for_a_channel=False) for name, _ in part.ctx.word_lists()) or "none yet"
        await answer(interaction, f"Which list? Lists: {lists}. Give one in `listname`, or a new name to make a list with the mutes, kicks and bans given "
                                  "(all three empty: a list that only reports, a ticket for the staff and nothing in game). Nothing was done.")
        return
    whole_list = spec.name == "unbanword" and not str(given.get("words") or "").strip() and bool(str(given.get("listname") or "").strip())  # every word on it: asked first
    asker = f"discord:{interaction.user.id}"
    servers, why = _server_for(part, spec, str(given.get("server") or "").strip(), player, asker)
    if not servers:
        await answer(interaction, why or "There are no servers here to run that on.")
        return
    someone = someone_from(interaction.user)
    options = part.file.section("commands")
    shown_name = name_of(spec, options.get("names") or {})
    timed = bool(given.get("time")) or times_a_kick(" ".join(str(value) for key, value in given.items()
                                                               if key not in ("server", "time")).split())
    rank = rank_for(part, spec, someone, servers, timed=timed)
    needed = next((role for known, role in part.ctx.game_commands() if known.name == spec.name), spec.role)
    if rank != PROBATION and ROLES.index(rank) < ROLES.index(needed):
        here = game_rank(part.file, someone, servers[0])
        if here in ROLES and ROLES.index(here) >= ROLES.index(needed):  # they have the rank where they are, and this goes further than that
            await answer(interaction, f"/{shown_name} reaches every server, and your rank is only on some of them. In the console, a `{spec.name}` line stays that server's own.")
        else:
            await answer(interaction, f"/{shown_name} is for {role_plural(needed)} and up.")
        return
    if spec.name == "respawn" and not can_act(rank):  # an account isn't linked to a player yet, so nobody can mean themselves
        await answer(interaction, "That's for staff from here: `!respawn` in game does it for you.")
        return
    public = {str(name) for name in options.get("public") or []}
    private = spec.name not in public and shown_name not in public  # by the name in the game, or the one here
    if spec.name in {str(word).lower() for word in part.file.section("console").get("confirm") or []} or whole_list:
        shown = f"`/{shown_name} {part.named_in(line)}`".replace(" `", "`")[:300]  # what they picked from the list was an ID: they know them by name
        if spec.name in (*BARS, "unbanword"):  # as the boxes were filled in, not as bars
            shown = f"`/{shown_name} " + " ".join(f"{box}: {str(value).strip()}" for box, value in given.items() if str(value or "").strip() and box != "server").replace("`", "'")[:280] + "`"
        await interaction.response.send_message(say(part.file, "console.sure", line=shown, server=cards.clean(", ".join(servers))), view=_Sure(part, spec, line, servers, private, timed), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        return
    await interaction.response.defer(ephemeral=private, thinking=True)
    await _go(part, interaction, spec, line, servers, private, timed=timed)


def _as_named_here(part: DiscordPart, text: str) -> str:
    """What a command says about other commands, by the names they have here: /pardon is /letback in this Discord."""
    names = part.file.section("commands").get("names") or {}
    known = {spec.name: name_of(spec, names) for spec, _ in part.ctx.game_commands()}
    return re.sub(r"(?<![\w/])/([a-z_]+)", lambda found: f"/{known.get(found[1], found[1])}", text)


async def _go(part: DiscordPart, interaction: discord.Interaction, spec: CommandSpec, line: str, servers: list[str], private: bool, *, timed: bool = False) -> None:
    """Run it on each server it's for, and say what came back. What comes back after that (a respawn's result, say) follows as it comes."""
    someone = someone_from(interaction.user)
    who = Player(0, acting_as(part, interaction.user), f"discord:{interaction.user.id}")
    part.real_names[who.id] = named(interaction.user)
    answered = False
    ran = False
    blocks: list[str] = []
    before = int(part.ctx.db.query("SELECT COALESCE(MAX(number), 0) AS n FROM actions")[0]["n"])
    together = part.ctx.picks_through(who.id) if len(servers) > 1 else nullcontext()  # each server's list in one answer, numbered through
    with together:
        for server in servers:
            rank = rank_for(part, spec, someone, [server], timed=timed)
            heard: list[str] = []

            async def tell(_server: str, _player_id: str, text: str, heard: list[str] = heard, rank: str = rank) -> None:
                if not answered:
                    heard.append(text)
                    return
                try:
                    await _say(part, interaction, _as_named_here(part, cards.clean(as_they_may_see(part.file, rank, text, everyone=not private), for_a_channel=False)), private)
                except discord.HTTPException as error:  # the moment's passed: the command did what it did
                    log.info("Couldn't pass on what /%s said later: %s", spec.name, error)

            first, _, after = line.partition(" ")
            state = part.ctx.servers.get(server)
            if spec.name == "kick" and timed and FULL_ID.fullmatch(first) and state is not None and not state.player_by_id(first.lower()):  # picked from the list, so there's no doubt who: and a kick with a time needs nobody to be on
                record = part.ctx.players.get(first.lower())
                refused = await part.ctx.run_as("kick", Player(0, str(record.last_name) if record is not None else first[:8], first.lower()), split_words(after), server=server, player=who, role=rank, tell=tell)
            else:
                refused = await part.ctx.run_line(spec.name, line, server=server, player=who, role=rank, tell=tell, prefix="/")
            ran = ran or refused is None
            said = _as_named_here(part, cards.clean(as_they_may_see(part.file, rank, part.named_in(refused or "\n".join(heard) or "Done."), everyone=not private), for_a_channel=False))
            blocks.append(f"**{cards.clean(server)}**\n{said}" if len(servers) > 1 else said)
    answered = True  # from here, what comes back goes on its own
    await _say(part, interaction, "\n\n".join(blocks), private, paged=spec.name in PAGED)
    new = part.ctx.db.query("SELECT COUNT(*) AS n FROM actions WHERE number > ? AND by_id = ?", (before, who.id))[0]["n"]
    if ran and spec.name not in LOOKING and not new:  # what reached the ledger has a card of its own, and what was refused didn't happen
        part.audit("console", say(part.file, "audit.console", who=who.name, server=", ".join(servers), line=as_they_may_see(part.file, "moderator", f"/{name_of(spec, part.file.section('commands').get('names') or {})} {line}".strip())))


def pages(text: str, most: int = MOST_SHOWN) -> list[str]:
    """A long answer as messages that each fit, broken between lines, or a line too long for one between its words (a list of hundreds of words is one line)."""
    out: list[str] = []
    page = ""
    for line in text.split("\n"):
        while len(line) > most:
            cut = line.rfind(", ", 0, most - 1)
            cut = cut + 1 if cut > 0 else line.rfind(" ", 0, most) if line.rfind(" ", 0, most) > 0 else most
            head, line = line[:cut].rstrip(), "  " + line[cut:].lstrip()
            if page:
                out.append(page)
                page = ""
            out.append(head)
        if page and len(page) + 1 + len(line) > most:
            out.append(page)
            page = ""
        page = f"{page}\n{line}" if page else line
    if page.strip():
        out.append(page)
    return [one if one.strip() else "\u200b" for one in out] or [""]


async def _say(part: DiscordPart, interaction: discord.Interaction, text: str, private: bool, *, paged: bool = False) -> None:
    """Never a ping, whatever's in a player's name. A long answer is a file, as a message holds 2,000 characters, or with `paged` as many messages as it takes; no address is ever in either, as `text` has
    been cleaned."""
    quiet = discord.AllowedMentions.none()
    if paged and len(text) > MOST_SHOWN:
        for page in pages(text):
            sent = await interaction.followup.send(page, ephemeral=private, allowed_mentions=quiet, wait=True)
            tidy.answered(part, interaction, sent, private)
        return
    if len(text) <= MOST_SHOWN:
        sent = await interaction.followup.send(text, ephemeral=private, allowed_mentions=quiet, wait=True)
    else:
        plain = re.sub(r"\\([*_~`|>\\\[\]#\-\d])", r"\1", text).replace("**", "")  # a file doesn't format, so nothing's to be escaped in it
        sent = await interaction.followup.send("It's long, so here's a file.", ephemeral=private, allowed_mentions=quiet, wait=True, file=discord.File(io.BytesIO(plain.encode("utf-8")), "answer.txt"))
    tidy.answered(part, interaction, sent, private)


def _how_they_stand(part: DiscordPart, player_id: str) -> dict[str, Any]:
    """Everything a list of who to act on might care about."""
    now: dict[str, Any] = dict(part.tickets.standing(player_id))
    clock = part.status.clock()
    try:
        now["novoted"] = bool(part.ctx.db.query("SELECT 1 FROM commands_no_votes WHERE target = ? AND lifted IS NULL AND (until IS NULL OR until > ?) LIMIT 1", (player_id, clock)))
    except sqlite3.Error:  # the commands part's been off since before there was such a table
        now["novoted"] = False
    try:
        now["struck"] = bool(part.ctx.db.query("SELECT 1 FROM automod_strikes WHERE player_id = ? AND pardoned IS NULL LIMIT 1", (player_id,)))
    except sqlite3.Error:  # Vergil's off, and always has been
        now["struck"] = False
    rank = part.ctx.highest_role(Player(0, part.name_of(player_id), player_id))
    now["ranked"] = rank not in ("player", "faculty")  # commands.toml alone changes faculty
    now["rank"] = rank
    return now


def _who_might_stand(part: DiscordPart, wanted: tuple[str, ...]) -> list[str]:
    """Their IDs, the latest first. How they stand now is for `_how_they_stand`: an unban since, or a time run out, is why these are only the ones to ask about."""
    db = part.ctx.db
    found: list[Any] = []

    def add(sql: str, *values: Any) -> None:
        try:
            found.extend(row[0] for row in db.query(sql, values))
        except sqlite3.Error:  # that part's been off since before it had a table
            pass

    if {"banned", "kicked", "muted"} & set(wanted):
        add("SELECT target_id FROM actions WHERE target_id != '' AND kind IN ('ban', 'kick', 'mute') AND ok = 1 AND dry_run = 0 GROUP BY target_id ORDER BY MAX(number) DESC LIMIT ?", MOST_LOOKED_AT)
        add("SELECT target FROM commands_every_server WHERE kind = 'ban' AND lifted IS NULL ORDER BY rowid DESC LIMIT ?", MOST_LOOKED_AT)  # brought over from older lists, with no word in the ledger
    if "novoted" in wanted:
        add("SELECT target FROM commands_no_votes WHERE lifted IS NULL GROUP BY target ORDER BY MAX(number) DESC LIMIT ?", MOST_LOOKED_AT)
    if "struck" in wanted:
        add("SELECT player_id FROM automod_strikes WHERE pardoned IS NULL GROUP BY player_id ORDER BY MAX(number) DESC LIMIT ?", MOST_LOOKED_AT)
    if "ranked" in wanted:
        members = part.ctx.permissions.members
        found.extend(player_id for rank in ("moderator", "member") for player_id in members.get(rank, set()))
        found.extend(player_id for state in part.ctx.servers.values() for player_id in (state.admins or set()))
    return [player_id for player_id in dict.fromkeys(str(one).lower() for one in found) if FULL_ID.fullmatch(player_id)][:MOST_LOOKED_AT]


def _standing_words(now: dict[str, Any]) -> list[str]:
    return [*(words for key, words in STANDING_WORDS.items() if now.get(key)), *([str(now["rank"]).replace("-", " ")] if now.get("ranked") else [])]


async def _player_choices(interaction: discord.Interaction, typed: str, *, command: str = "") -> list[app_commands.Choice[str]]:
    """Who a command can do anything to, with this in their name, saying where they are and how they stand. Nothing for someone who isn't staff: who's been here isn't theirs to search. A whole ID is always answered, with how they stand, as whoever's typed it knows who they mean."""
    part = part_of(interaction)
    someone = someone_from(interaction.user)
    if not can_act(highest_rank(part.file, someone, part.ctx.servers)):
        return []
    only, unless = ONLY_IF.get(command, ()), UNLESS.get(command, ())
    wanted = typed.strip().casefold()
    whole_id = bool(FULL_ID.fullmatch(typed.strip()))
    clock = part.status.clock()

    def fits(now: dict[str, Any]) -> bool:
        return whole_id or ((not only or any(now.get(key) for key in only)) and not any(now.get(key) for key in unless))

    def label(player_id: str, name: str, where: str = "") -> str:
        now = _how_they_stand(part, player_id)
        record = part.ctx.players.get(player_id)
        last = getattr(record, "last_seen", None)
        return " · ".join([name, *([where] if where else []), *_standing_words(now), *([f"seen {ago(max(0.0, clock - float(last)))} ago"] if last and not where else [])])

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not only or whole_id:
        for server, state in part.ctx.servers.items():
            if not state.connected:
                continue
            for player in state.players.values():
                if (wanted in player.name.casefold() or player.id == wanted) and player.id not in seen:
                    seen.add(player.id)
                    if fits(_how_they_stand(part, player.id)):
                        found.append((label(player.id, player.name, f"{server} · #{player.number}"), player.id))
    if command not in ON_NOW_ONLY or whole_id:
        if only and not whole_id:
            named = dict(matching(part, typed, MOST_LOOKED_AT)) if wanted else {}
            candidates = [(player_id, named.get(player_id, "")) for player_id in _who_might_stand(part, only) if not wanted or player_id in named or wanted in part.name_of(player_id).casefold()]
        else:
            candidates = matching(part, typed, MOST_LOOKED_AT if unless else MOST_CHOICES)
        for player_id, matched in candidates:
            if player_id in seen or len(found) >= MOST_CHOICES:
                continue
            seen.add(player_id)
            if fits(_how_they_stand(part, player_id)):
                found.append((label(player_id, name_shown(part, player_id, matched)), player_id))
    return as_choices(part, interaction, found)


async def _list_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    """The word list's lists, with what each does, for staff: what's on it isn't anyone else's to search. Anything can still be typed: a new list's name, for /banword."""
    part = part_of(interaction)
    if not can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
        return []
    wanted = typed.strip().casefold()
    return [app_commands.Choice(name=f"{name} · {does}"[:100], value=name[:100]) for name, does in part.ctx.word_lists() if wanted in name.casefold()][:MOST_CHOICES]


async def _word_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    """The words on the list with this in them, after the last comma: what's typed before it stays."""
    part = part_of(interaction)
    if not can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
        return []
    before, _, last = typed.rpartition(",")
    start = f"{before.strip()}, " if before.strip() else ""
    wanted = last.strip().casefold()
    found = [(word, name) for word, name in part.ctx.listed_words() if wanted in word.casefold()]
    return [app_commands.Choice(name=f"{start}{word} · {name}"[:100], value=f"{start}{word}"[:100]) for word, name in found if len(start) + len(word) <= 100][:MOST_CHOICES]


async def _banned_name_choices(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    part = part_of(interaction)
    if not can_act(highest_rank(part.file, someone_from(interaction.user), part.ctx.servers)):
        return []
    wanted = typed.strip().casefold()
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in part.ctx.banned_names() if wanted in name.casefold()][:MOST_CHOICES]


async def _time_choices(_interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
    wanted = RESTART_SUGGESTED if getattr(_interaction.command, "name", "") == "restartserver" else TIME_SUGGESTED
    return [app_commands.Choice(name=time, value=time) for time in wanted if time.startswith(typed.strip().lower())]


def build(part: DiscordPart, spec: CommandSpec, role: str) -> app_commands.Command[Any, Any, Any]:
    """One command, with a box for each thing it takes."""
    options = part.file.section("commands")
    plan = plan_of(spec)
    if spec.name not in EVERYWHERE:
        hint = "Which server. Empty: all of them" if spec.name in EACH_SERVER else "Which server: where they are, if you leave it out" if any(one.kind == "player" for one in plan) else "Which server"
        plan = [*plan, Option("server", hint, kind="server")]

    async def callback(interaction: discord.Interaction, **given: Any) -> None:
        await run(part, interaction, spec, plan, given)

    callback.__signature__ = inspect.Signature([inspect.Parameter("interaction", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=discord.Interaction),  # type: ignore[attr-defined]
                                                *[inspect.Parameter(option.name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=int if option.kind == "number" else str,
                                                                    default=inspect.Parameter.empty if option.required else 0 if option.kind == "number" else "") for option in plan]])
    command: app_commands.Command[Any, Any, Any] = app_commands.Command(name=name_of(spec, options.get("names") or {}), description=DESCRIBED.get(spec.name) or description_of(spec), callback=callback)
    for option in plan:
        param = command._params[option.name]
        param.description = option.description[:100]
        if option.choices:
            param.choices = [app_commands.Choice(name=choice, value=choice) for choice in option.choices]
        elif option.kind == "player":
            async def players(interaction: discord.Interaction, typed: str, command: str = spec.name) -> list[app_commands.Choice[str]]:
                return await _player_choices(interaction, typed, command=command)

            command.autocomplete(option.name)(_two_arguments(players))
        elif option.kind == "time":
            command.autocomplete(option.name)(_time_choices)
        elif option.kind == "server":
            command.autocomplete(option.name)(server_choices)
        elif option.kind == "list":
            command.autocomplete(option.name)(_list_choices)
        elif option.kind == "listed":
            command.autocomplete(option.name)(_word_choices)
        elif spec.name == "unbanname" and option.name == "name":
            command.autocomplete(option.name)(_banned_name_choices)
    needed = str(options.get("staff_permission") or "")
    if needed and role not in ("player", "member") and needed in discord.Permissions.VALID_FLAGS:
        command.default_permissions = discord.Permissions(**{needed: True})
    command.guild_only = True
    return command


def _two_arguments(callback: Any) -> Any:
    """What Discord's library accepts for a list of suggestions: exactly the two arguments, whatever was bound into it."""

    async def suggest(interaction: discord.Interaction, typed: str) -> list[app_commands.Choice[str]]:
        return await callback(interaction, typed)

    return suggest


def commands_for(part: DiscordPart) -> list[app_commands.Command[Any, Any, Any]]:
    """Every in-game command that isn't hidden, on and in commands.toml. A name Discord's own commands have, or a second one the same, is left out and said in the log."""
    options = part.file.section("commands")
    hidden = {str(name) for name in options.get("hidden") or []}
    made: list[app_commands.Command[Any, Any, Any]] = []
    seen = set(TAKEN)
    for spec, role in part.ctx.game_commands():
        name = name_of(spec, options.get("names") or {})
        if spec.name in NOT_FROM_DISCORD or spec.name in hidden or name in hidden:  # by the name in the game, or the one here
            continue
        if name in seen:
            log.warning("/%s is already a command in Discord, so !%s has no slash command: [commands] names can give it another", name, spec.name)
            continue
        seen.add(name)
        made.append(build(part, spec, role))
    return made


__all__ = ["Option", "build", "commands_for", "description_of", "name_of", "pages", "plan_of", "run"]
