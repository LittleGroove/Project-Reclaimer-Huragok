"""automod: one word list, automod.txt in the supervisor's folder, one rule per line:
its words, cascading timers, and chances before a ban (see rules.py). Edits
apply within seconds; a broken edit is reported and the old rules stay.
Commands: !strike (moderators, e.g. for voice), !strikes, and !pardonplayer
(admins), which clears strikes and lifts what automod did.
"""

from .automod import AutoMod

__all__ = ["AutoMod"]
