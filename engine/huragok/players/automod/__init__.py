"""automod: one word list, automod.txt in the supervisor's folder, one rule per line:
its words, then the kicks, the mutes and the ban its strikes lead to (see
rules.py). Mutes are Vergil's own list, put back as players join, since the
server forgets its mutes when it restarts. Names: a name with a listed word in it
(anywhere, or as a word of its own: [automod] name_match), or one in names.txt,
is kicked as it joins, every time, until it changes; no strike.
Edits to either file apply within seconds; a broken edit is reported and the old
rules stay. Commands: !strike (moderators, e.g. for voice; `!strike <player> name`
bans their name), !strikes, and the admins' !banword and !unbanword (the word
list from the game), !banname and !unbanname (names.txt), and !pardonplayer,
which clears strikes and lifts what automod did.
"""

from .automod import AutoMod

__all__ = ["AutoMod"]
