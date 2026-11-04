"""Linking (0.6.1): a player in game and a member of the Discord shown to be the same person, so the stats can carry the
name people know them by here: "claim your spot" (the host's ask, 3 November 2026).

`!link` in game tells the player a code, privately; `/link code` in Discord ties the two. That way round because game
chat is public: a code from Discord typed in game could be pasted by someone tricked into it, tying their record (and
once ranks follow links, their rank) to a stranger's account. A code only they were told shows the member in Discord
is the player in the game. A code works once, for [link] code_minutes, and a player can ask for CODES_AN_HOUR.

A link changes nobody's rank: Discord's roles and Huragok's ranks following each other is for a later version (the
host's call, 3 November 2026). One player, one member, and the other way round. On the stats, the leaderboards and the
game summaries a linked player shows by their name in this Discord ([link] names: discord, game or both), unless they
chose otherwise with /link show. Their name here is kept as it was last seen, and seen again whenever they use /link.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .cards import clean

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .part import DiscordPart

SCHEMA_STEP = """
    CREATE TABLE discord_links (
        player_id TEXT PRIMARY KEY, player_name TEXT NOT NULL, member TEXT NOT NULL UNIQUE, member_name TEXT NOT NULL,
        name_seen REAL NOT NULL, shown TEXT NOT NULL DEFAULT '', time REAL NOT NULL, by TEXT NOT NULL);
    CREATE TABLE discord_link_codes (
        code TEXT PRIMARY KEY, player_id TEXT NOT NULL, player_name TEXT NOT NULL, time REAL NOT NULL,
        expires REAL NOT NULL);
    CREATE INDEX discord_link_codes_player ON discord_link_codes (player_id, time);
    """
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # none that read as another: no I, O, 0 or 1
CODE_LETTERS = 6
CODES_AN_HOUR = 3
KEPT_SECONDS = 86400.0  # how long a code used or gone stays, for the count an hour
DISCORD, GAME, BOTH = "discord", "game", "both"
# How a linked player's name shows: never not at all. The boards don't discriminate (the host's call, 3 November 2026:
# "if you EARNED your spot on a board, you shall be graced with the spot indiscriminately and fairly").
SHOWN = (DISCORD, GAME, BOTH)
LINKED, NO_SUCH_CODE, MEMBER_TAKEN, PLAYER_TAKEN = "linked", "no such code", "member taken", "player taken"
TOO_MANY_WRONG = "too many wrong"
WRONG_CODES = 5  # a member's wrong codes in WRONG_SECONDS before they wait: a code can't be guessed (audit 0.6.1, 3.13)
WRONG_SECONDS = 600.0
CLAIMED = "as they claimed Huragok's Discord bot"  # the note faculty_claimed writes, and faculty_unclaimed looks for


def tidy_code(typed: str) -> str:
    """What was typed, as a code: spaces, dashes and case don't matter."""
    return "".join(letter for letter in typed.upper() if letter.isalnum())


def shown_code(code: str) -> str:
    return f"{code[:3]}-{code[3:]}" if len(code) == CODE_LETTERS else code


class Links:
    def __init__(self, part: DiscordPart, clock: Callable[[], float] = time.time) -> None:
        self.wrong: dict[str, list[float]] = {}  # member -> when each wrong code was typed, lately
        self.part = part
        self.clock = clock

    @property
    def db(self) -> Any:
        return self.part.ctx.db

    @property
    def options(self) -> dict[str, Any]:
        return self.part.file.section("link")

    def takes(self) -> bool:
        return bool(self.options.get("enabled")) and self.part.file.mode == "bot"

    def of_player(self, player_id: str) -> Any:
        rows = self.db.query("SELECT * FROM discord_links WHERE player_id = ?", (player_id.lower(),))
        return rows[0] if rows else None

    def of_member(self, member: str) -> Any:
        rows = self.db.query("SELECT * FROM discord_links WHERE member = ?", (str(member),))
        return rows[0] if rows else None

    def asked_lately(self, player_id: str) -> int:
        """Codes this player asked for in the last hour."""
        return int(self.db.query("SELECT COUNT(*) AS n FROM discord_link_codes WHERE player_id = ? AND time > ?",
                                 (player_id.lower(), self.clock() - 3600))[0]["n"])

    def new_code(self, player_id: str, player_name: str) -> str:
        """A code for this player, the one before it no longer working. "" past CODES_AN_HOUR."""
        player_id = player_id.lower()
        if self.asked_lately(player_id) >= CODES_AN_HOUR:
            return ""
        now = self.clock()
        self.db.execute("DELETE FROM discord_link_codes WHERE time < ?", (now - KEPT_SECONDS,))
        self.db.execute("UPDATE discord_link_codes SET expires = ? WHERE player_id = ? AND expires > ?", (now, player_id, now))
        from .standings import counted

        minutes = float(counted(self.options.get("code_minutes", 10), 10, 1, 1440))
        while True:
            code = "".join(secrets.choice(LETTERS) for _ in range(CODE_LETTERS))
            if not self.db.query("SELECT 1 FROM discord_link_codes WHERE code = ?", (code,)):
                self.db.execute("INSERT INTO discord_link_codes (code, player_id, player_name, time, expires) VALUES (?, ?, ?, ?, ?)",
                                (code, player_id, player_name, now, now + minutes * 60))
                return code

    def peek(self, typed: str) -> str:
        """The name of the player a fresh code is for, without using it: "" for no such code."""
        code = tidy_code(typed)
        rows = self.db.query("SELECT player_name FROM discord_link_codes WHERE code = ? AND expires > ?", (code, self.clock())) if code else []
        return str(rows[0]["player_name"]) if rows else ""

    def find_players(self, typed: str) -> list[Any]:
        """The links of a player: by their whole ID, or by their name in game (as linked, or as last seen). More than one
        by a name: whoever asks gives the ID (second audit, 8.5: the first was taken)."""
        text = typed.strip()
        if found := self.of_player(text):
            return [found]
        rows = self.db.query("SELECT * FROM discord_links")
        return [row for row in rows if text.casefold() in {str(row["player_name"] or "").casefold(), self.player_name(row).casefold()}]

    def use_code(self, typed: str, member: str, member_name: str) -> tuple[str, str, str]:
        """(what came of it, the player's ID, their name). A code works once, and only while it's fresh."""
        code = tidy_code(typed)
        now = self.clock()
        lately = [when for when in self.wrong.get(str(member), []) if when > now - WRONG_SECONDS]
        if len(lately) >= WRONG_CODES:
            return TOO_MANY_WRONG, "", ""
        rows = self.db.query("SELECT * FROM discord_link_codes WHERE code = ? AND expires > ?", (code, now)) if code else []
        if not rows:
            self.wrong[str(member)] = [*lately, now]
            return NO_SUCH_CODE, "", ""
        player_id, player_name = str(rows[0]["player_id"]), str(rows[0]["player_name"])
        mine = self.of_member(member)
        if mine is not None:
            return (LINKED if str(mine["player_id"]) == player_id else MEMBER_TAKEN), str(mine["player_id"]), self.player_name(mine)
        theirs = self.of_player(player_id)
        if theirs is not None:
            return PLAYER_TAKEN, player_id, player_name
        self.part.roles_due = True  # their roles follow (rolesync.py)
        with self.db.transaction():
            self.db.execute("UPDATE discord_link_codes SET expires = ? WHERE code = ?", (now, code))
            self.db.execute("INSERT INTO discord_links (player_id, player_name, member, member_name, name_seen, time, by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (player_id, player_name, str(member), member_name, now, now, "code"))
        return LINKED, player_id, player_name

    async def faculty_claimed(self, member: str) -> str:
        """Faculty of this bot (/huragok claim: [ranks] faculty) whose account is linked is faculty in game too, listed
        in commands.toml as !promote lists a rank: the only way to faculty but writing commands.toml by hand (the host's
        call, 3 November 2026). The player's name, or "" when there was nothing to do."""
        import asyncio
        from pathlib import Path

        from ..core.commandsfile import PermissionsError, add_player
        from .ranks import is_faculty

        from .ranks import in_game

        link = self.of_member(member)
        if link is None or not is_faculty(self.part.file, member) or in_game(self.part.file, "faculty") != "faculty":
            return ""  # faculty_in_game keeps faculty of the bot to a lower rank in game: so it stays
        player_id = str(link["player_id"]).lower()
        if player_id in {listed.lower() for listed in self.part.ctx.permissions.members.get("faculty", set())}:
            return ""
        name = self.player_name(link)
        path = self.part.ctx.permissions.path or Path(self.part.ctx.home) / "commands.toml"
        try:
            members = self.part.ctx.permissions.members  # the rank they had, said in the note: an unlink gives it back (8.3)
            was = next((role for role in ("moderator", "member") if player_id in {one.lower() for one in members.get(role, set())}), "")
            await asyncio.to_thread(add_player, path, "faculty", player_id,
                                    f"{name}: faculty, {CLAIMED}{f' (was {was})' if was else ''}, their account linked on {time.strftime('%Y-%m-%d')}")
        except (OSError, PermissionsError) as error:
            log.warning("Couldn't make %s faculty in commands.toml: %s", name, error)
            return ""
        self.part.ctx.reload_permissions()
        self.part.roles_due = True
        log.info("%s is faculty in game now: faculty of the bot, with their account linked", name)
        return name

    async def faculty_unclaimed(self, link: Any) -> bool:
        """A link undone takes away the faculty its claim gave (faculty_claimed's note says so): the account was
        someone else's, or faculty can link again. Faculty written into commands.toml by hand stay."""
        import asyncio
        from pathlib import Path

        import re

        from ..core.commandsfile import LISTED, PermissionsError, add_player, player_notes, remove_player

        player_id = str(link["player_id"]).lower()
        path = self.part.ctx.permissions.path or Path(self.part.ctx.home) / "commands.toml"
        try:
            note = (await asyncio.to_thread(player_notes, path)).get(player_id, "")
            if CLAIMED not in note:
                return False
            await asyncio.to_thread(remove_player, path, "faculty", player_id)
            was = re.search(r"\(was ([a-z-]+)\)", note)
            if was and was.group(1) in LISTED and was.group(1) != "faculty":  # the rank the claim took, back (8.3)
                await asyncio.to_thread(add_player, path, was.group(1), player_id,
                                        f"{self.player_name(link)}: {was.group(1)} again, their link undone on {time.strftime('%Y-%m-%d')}")
        except (OSError, PermissionsError) as error:
            log.warning("Couldn't take %s out of faculty in commands.toml: %s", self.player_name(link), error)
            return False
        self.part.ctx.reload_permissions()
        self.part.roles_due = True
        log.info("%s isn't faculty in game any more: their link was undone", self.player_name(link))
        return True

    def player_name(self, link: Any) -> str:
        """The linked player's name as they were last seen in game, or as they were when they linked."""
        return self.part.name_of(str(link["player_id"]), str(link["player_name"] or ""))

    def unlink(self, member: str) -> Any:
        """The member's link gone: what it was, or None."""
        mine = self.of_member(member)
        if mine is not None:
            self.db.execute("DELETE FROM discord_links WHERE member = ?", (str(member),))
            self.part.roles_due = True
        return mine

    def seen(self, member: str, member_name: str) -> None:
        """Their name in this Discord as it is now, for the stats: names change."""
        self.db.execute("UPDATE discord_links SET member_name = ?, name_seen = ? WHERE member = ? AND member_name != ?",
                        (member_name, self.clock(), str(member), member_name))

    def show(self, member: str, how: str) -> bool:
        """How they want to show on the stats. False: they aren't linked."""
        return self.db.execute("UPDATE discord_links SET shown = ? WHERE member = ?",
                               (how if how in SHOWN else "", str(member))).rowcount > 0

    def how_shown(self, link: Any) -> str:
        """discord, game or both: their own choice, else [link] names."""
        chosen = str(link["shown"] or "") if link is not None else ""
        default = str(self.options.get("names") or DISCORD)
        return chosen if chosen in SHOWN else default if default in SHOWN else DISCORD

    def shown_name(self, player_id: str, game_name: str) -> str:
        """A player's name on the stats: their name here when they're linked and want it, else the game's."""
        link = self.of_player(player_id) if self.takes() else None
        if link is None:
            return game_name
        how = self.how_shown(link)
        member_name = str(link["member_name"] or "").strip() or game_name  # as it is: whoever shows it cleans it, once (audit 0.6.1, 5.4)
        return (game_name if how == GAME else
                f"{member_name} ({game_name})" if how == BOTH and member_name != game_name else member_name)


__all__ = ["BOTH", "CLAIMED", "CODES_AN_HOUR", "DISCORD", "GAME", "LINKED", "Links", "MEMBER_TAKEN", "NO_SUCH_CODE",
           "PLAYER_TAKEN", "SCHEMA_STEP", "SHOWN", "TOO_MANY_WRONG", "shown_code", "tidy_code"]
