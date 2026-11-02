"""Starting the bot over: a Discord that's been wiped, or a test from the top. The token stays, as making a new one isn't what anyone came to do, and so do notes and flags: they're about players, not this Discord.
Used from Huragok's window. With Huragok running it's safe too: the bot reads the file again within seconds, and whatever it's holding for a card that's gone finds nothing there.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from ..core.tomledit import set_top_key
from .config import DiscordFileError, make_file, read, set_mode, set_token

TABLES = ("discord_posts", "discord_tickets", "discord_ticket_lines", "discord_reports", "discord_appeals", "discord_alerts", "discord_tidy")


def start_over(path: Path, database: Path) -> list[str]:
    """What it did, to say so. discord.toml is kept beside itself first, as it can hold more than one host's afternoon of choices."""
    try:
        config = read(path)
    except DiscordFileError:  # what's the likeliest reason for wanting this
        config = None
    variable = str(config.data.get("token_variable") or "").strip() if config is not None else ""
    token = config.token if config is not None and not variable else ""
    mode = str(config.data.get("mode") or "off") if config is not None else "off"
    said: list[str] = []
    if path.exists():
        saved = path.with_name(f"{path.stem}.before-start-over-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
        shutil.copy2(path, saved)
        said.append(f"The old file is kept as {saved.name}. It holds the token, so keep it private or delete it.")
        path.unlink()
    make_file(path)
    if token:
        set_token(path, token)
    if variable:
        set_top_key(path, "token_variable", variable)
    if mode == "bot" and (token or variable):
        set_mode(path, "bot")
    said.append("discord.toml is new, with every feature off" + (" and the same token." if token or variable else "."))
    if not database.exists():
        return said
    emptied = 0
    connection = sqlite3.connect(str(database), timeout=10)
    try:
        for table in TABLES:
            try:
                emptied += connection.execute(f"DELETE FROM {table}").rowcount
            except sqlite3.OperationalError:
                continue  # a version before that table: there's nothing in it
        connection.commit()
    finally:
        connection.close()
    said.append(f"The bot's cards and records of this Discord are gone ({emptied} rows). What staff wrote about players stays.")
    return said


__all__ = ["TABLES", "start_over"]
