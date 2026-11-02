"""The settings of discord.toml that have a new name or a new home, in one place: the update step moves them on hosts' files, and the file's own check tells anyone who writes an old name by hand what it's called now.

A row here reaches every host at their next start: the step runs every time, and does nothing once the old name's gone. Never take a row out, as a host can skip versions. Two things it can't do: a rename that narrows who can do what
needs a settled step of its own, or a put-back runs on the looser default; and a placeholder in a text is never renamed, since a host's own wording uses it (pass the old name too).
"""

from __future__ import annotations

# (the table, the key) -> (the table it's in now, its key)
KEYS: dict[tuple[tuple[str, ...], str], tuple[tuple[str, ...], str]] = {
    (("contact",), "ping"): (("contact",), "thread_role"),
    (("commands",), "message_menu"): (("reports",), "right_click"),
    (("status", "seeding"), "notify_me"): (("notify",), "enabled"),
    (("console",), "raw"): (("console",), "raw_rank"),
    (("ranks",), "moderators_raw_console"): (("console",), "raw_rank"),
}
# A line of [messages], and a field any line can have.
LINES = {"seeding-notify": "notify-told"}
LINE_FIELDS = {"remove_after": "remove_after_minutes"}
# What [text] can reword: a host's wording keeps working under the name it was written for.
TEXTS = {"seeding.players": "notify.players", "seeding.game": "notify.game"}


__all__ = ["KEYS", "LINES", "LINE_FIELDS", "TEXTS"]
