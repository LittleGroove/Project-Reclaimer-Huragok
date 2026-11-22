"""Who a command means. Every in-game command that names a player finds them here, so every one has the same guards:
declared on the command (`@command(…, player="on" | "seen", acts=True)`: the router finds the player before the
command runs), or asked for by a command that reads its own words (`call.find`, `call.player_and_rest`).

Names can't be trusted to pick a player: they're free text, several players share one (30 "Player"s one night),
they have spaces, capitals and apostrophes, and they change at will. What a command acts on is a player ID, so:

- The player is the longest name the line starts with ("!kick Nate Higgers spam"), word for word in any case;
  quotes work too, as on the console. A name that is the start of a longer one, with more words after it, could be
  either: the caller is asked, unless the next word is one the command expects there (a time, a rule).
- A name more than one player has acts on nobody. The caller gets each of them, one a line, written the one way
  (core/tags.py: "Nate 1a2b3c4d (#4) [HugeGrass]") with what tells them apart (their team, how long they've been on
  or when they were last here, their role, a mute) and what to type: #4 for someone on this server, @1 for someone
  who isn't (the caller's own list). For a command that reaches players who left (scope "seen"), the ones seen here
  count too.
- The start of a name acts on nobody, except for a command that only looks or lifts (`start`), and then only when
  one player on has it.
- Nobody acts on someone of their own role or above (`acts`; faculty excepted), named by name, number or ID alike.

How each player was picked (#4, @2) is kept on the call (`call.picked`), so what the command says it did names them
with what was typed, and the ledger keeps it with the action.

@numbers stick (the host's call, 21 November 2026): anyone a reply names who isn't on the caller's server gets one,
and keeps it for ten minutes in every list and reply that names them, so @2 never quietly becomes someone else.
Lists of players who aren't on (!bans, !mutes, !kicks) number their entries the same way: `!unban @2`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..core.commandsfile import PROBATION, ROLES
from ..core.tags import SHORT, tag
from ..core.text import PLAYER_ID, lookalike
from ..servers.state import Player
from .roles import a_role

if TYPE_CHECKING:
    from .router import CommandCall

NUMBER = re.compile(r"#?(\d{1,4})")  # a player's #number; the number alone means it too, as on the console
PICK = re.compile(r"@(\d{1,3})")  # an entry of the caller's last list
FULL_ID = PLAYER_ID  # a whole player ID, either case
ID_START = re.compile(r"[0-9a-fA-F]{%d,63}" % SHORT)  # the start of a player ID: every tag shows the first 8
QUOTES = {'"': '"', "“": "”", "'": "'"}  # a name in quotes, as the console takes it: "Master Chief"
TIME = re.compile(r"\d+[smhdw]", re.IGNORECASE)
FOREVER = {"perm", "permanent", "forever", "never"}
TEAMS = {"red", "blue", "green", "orange", "purple", "gold", "brown", "pink"}
PICKS_SECONDS = 600  # how long @numbers stand for whoever they were given to, from the last time one was used or shown
MOST_PICKS = 999  # @numbers one caller can have at once (PICK reads three digits); past it they start again from @1
LISTED = 6  # players named in one "could be" answer
SCOPES = ("on", "any", "seen")  # this server; any server (or by player ID, someone who left); anyone seen


def is_time(word: str) -> bool:
    """A time after a player's name (!mute, !ban): 30m, 7d, perm."""
    return bool(TIME.fullmatch(word)) or word.lower() in FOREVER


def is_team(word: str) -> bool:
    return word.lower() in TEAMS


def plain(name: str) -> str:
    """A name as names are compared: any case, spaces as one."""
    return " ".join(name.split()).casefold()


def looks(name: str) -> str:
    """A name as it looks (core/text.py lookalike): ΒRAVO, ʙʀᴀᴠᴏ and Bravo all look like "bravo". Never enough to act
    on: names that look alike are offered to pick from."""
    return " ".join(lookalike(name).split())


def ago(seconds: float) -> str:
    """12m, 3h, 2d: how long, in its largest unit."""
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@dataclass(frozen=True)
class Candidate:
    """Someone a command could mean: on the caller's server (number), or not (a player ID seen here, or a list's
    entry)."""

    id: str
    name: str
    number: int = 0  # their #number on the caller's server; 0: not on it
    team: str | None = None
    since: float | None = None  # on since (number > 0), or last joined here (number == 0)
    muted: bool = False
    where: str = ""  # the other server they're on now
    note: str = ""  # what a list knows about them: "banned 2d ago by Alice: cheating"
    target: str = ""  # what the console takes for the entry when it isn't the player ID (an address ban's IP)

    def server(self, here: str) -> str:
        """The server they're on: the caller's (`here`) for someone with a #number there, else the one they're on."""
        return here if self.number else self.where

    def describe(self, handle: str, role: str = "", now: float | None = None, *, here: str = "",
                 plain: bool = False) -> str:
        """"Nate 1a2b3c4d (#4) [HugeGrass]: red, on 12m, moderator, muted": the tag (core/tags.py), then what tells
        them apart. `here`: the caller's server. `plain`: the tag alone, for a player asking (others' ranks and mutes
        aren't theirs)."""
        now = time.time() if now is None else now
        shown = tag(self.name, self.id, handle=handle, server=self.server(here))
        if plain:
            return shown
        bits: list[str] = []
        if self.number:
            bits += [self.team.lower()] if self.team else []
            bits += [f"on {ago(now - self.since)}"] if self.since else []
        elif self.since and not self.where:
            bits.append(f"seen {ago(now - self.since)} ago")
        bits += [role] if role and role != "player" else []
        bits += ["muted"] if self.muted else []
        bits += [self.note] if self.note else []
        return shown + (f": {', '.join(bits)}" if bits else "")

    @property
    def key(self) -> str:
        """What one entry is, whichever list names it: the player ID, else what the console takes (an address), else
        the name (a name banned as text)."""
        return (self.id or self.target or self.name).lower()


class Picks:
    """Each caller's @numbers: @1, @2… for the players replies named who weren't on the caller's server, and the
    entries of the lists they were shown (!bans, !mutes, !kicks). Sticky: someone keeps their number in every list and
    reply that names them, and new people get the next one, so a number never changes who it means. Ten minutes after
    the caller last used or was shown one, they all run out, and the next list starts again at @1."""

    def __init__(self) -> None:
        self._lists: dict[str, tuple[float, list[Candidate]]] = {}

    @contextmanager
    def adding(self, caller: str) -> Iterator[None]:
        """Several lists in one answer, one numbering through them all. Every list is numbered that way now (sticky
        @numbers), so this only keeps what calls it working."""
        yield

    def _live(self, caller: str) -> list[Candidate]:
        made, entries = self._lists.get(caller, (0.0, []))
        if entries and time.monotonic() - made > PICKS_SECONDS:
            self._lists.pop(caller, None)
            return []
        return entries

    def offer(self, caller: str, entries: list[Candidate]) -> list[tuple[int, Candidate]]:
        """Each entry with its number: the one it already has, or the next free one. What's newer about someone (a
        ban's note, the server they're on) replaces what was kept."""
        have = list(self._live(caller))
        if len(have) + len(entries) > MOST_PICKS:
            have = []
        index = {entry.key: number for number, entry in enumerate(have, 1)}
        numbered = []
        for entry in entries:
            number = index.get(entry.key)
            if number is None:
                have.append(entry)
                number = index[entry.key] = len(have)
            else:
                have[number - 1] = entry
            numbered.append((number, entry))
        self._lists[caller] = (time.monotonic(), have)
        return numbered

    def number_for(self, caller: str, entry: Candidate) -> int:
        """Their @number for this caller, given now if they had none."""
        return self.offer(caller, [entry])[0][0]

    def get(self, caller: str, number: int) -> Candidate | str:
        """The entry, or why there's none."""
        made, entries = self._lists.get(caller, (0.0, []))
        if not entries:
            return f"You have no @{number}: @numbers are the players a list or a reply named to you."
        if time.monotonic() - made > PICKS_SECONDS:
            return (f"Your @numbers ran out ({PICKS_SECONDS // 60} minutes without one being used or shown): ask "
                    "again for a fresh list.")
        if not 1 <= number <= len(entries):
            return f"You have @1 to @{len(entries)}."
        self._lists[caller] = (time.monotonic(), entries)  # used: they stay a while longer
        return entries[number - 1]


# Reading a name off the line ---------------------------------------------------------------------------------------

async def split(call: CommandCall, text: str | None = None, *,
                follows: Callable[[str], bool] | None = None) -> tuple[str | None, list[str]]:
    """Who the line names, and the words after them: "Nate Higgers spam" is ("Nate Higgers", ["spam"]). The longest
    name it starts with, word for word in any case: a player's on this server, or (with the call's history) a name of
    more than one word seen here. Otherwise the first word ("#3", "@1", a player ID, a name of one word, or one nobody
    has: find says why). A name in quotes is taken as written. The words are the line's own, so an apostrophe in a
    name or a reason stays where it was typed.

    When the name found is the start of a longer one (Nate, with Nate Higgers on too) and words follow, the line can't
    say which: "!kick nate higgins spam" would otherwise kick Nate for a typo. The caller is asked, and the name is
    None. Unless the word after the shorter name is one the command expects there (`follows`): "!mute nate 10m"."""
    text = (call.text if text is None else text).strip()
    if text[:1] in QUOTES:
        close = QUOTES[text[0]]
        # The quote that ends the name ends a word too: the one in "it's" doesn't.
        end = next((i for i in range(1, len(text)) if text[i] == close and text[i + 1:i + 2] in ("", " ")), 0)
        if end > 1:
            return text[1:end].strip(), text[end + 1:].split()
    words = text.split()
    if not words:
        return "", []
    history = call.history
    on = {p.name for p in call.state.players.values()}
    seen = set(history.names_starting(words[0])) - on if history is not None and len(words) > 1 else set()
    folded = [w.casefold() for w in words]

    def starts(name: str, count: int) -> bool:
        parts = name.split()
        return len(parts) >= count and [p.casefold() for p in parts[:count]] == folded[:count]

    best, longest = words[0], 0
    for name in sorted(on | seen):
        count = len(name.split())
        if longest < count <= len(words) and starts(name, count):
            best, longest = name, count
    longest = max(longest, 1)  # no name: the first word, for find to make sense of
    if (longest == 1 and history is not None and len(words) > 1 and plain(best) not in {plain(n) for n in on}
            and any(len(name.split()) > 1 and starts(name, 1) for name in on | seen)):
        # A name of one word seen before ("Nate", gone) with longer ones to tell apart from.
        if any(plain(n) == folded[0] for r in history.find(words[0]) for n in [r.last_name, *r.names]):
            seen.add(words[0])
    matched = longest > 1 or any(len(name.split()) == 1 and starts(name, 1) for name in on | seen)
    longer = sorted(name for name in on | seen if len(name.split()) > longest and starts(name, longest))
    if matched and longer and len(words) > longest and not (follows and follows(words[longest])):
        found: list[Candidate] = []
        for name in [best, *longer]:
            here = [_on(p) for p in sorted(call.state.players.values(), key=lambda p: p.number) if p.name == name]
            found += here or _seen_named(call, name)
        await ask(call, f"'{' '.join(words[:longest + 1])}…'", found)
        return None, words[longest:]
    return best, words[longest:]


# Finding the one player -----------------------------------------------------------------------------------------------

def _on(player: Player) -> Candidate:
    return Candidate(player.id, player.name, player.number, player.team, player.joined, player.muted)


def _seen(call: CommandCall, record: Any, name: str = "") -> Candidate:
    """Someone seen here who isn't on the caller's server: on another one, or when they were last here."""
    where = next((server for server, state in (call.states or {}).items()
                  if server != call.server and state.player_by_id(record.id)), "")
    return Candidate(record.id, name or record.last_name, 0, since=record.last_seen, where=where)


def _seen_named(call: CommandCall, name: str) -> list[Candidate]:
    """Everyone seen here under this name (written as they wrote it) who isn't on the caller's server."""
    if call.history is None:
        return []
    found = []
    for record in call.history.find(name):
        written = next((n for n in [record.last_name, *record.names] if plain(n) == plain(name)), None)
        if written is not None and not call.state.player_by_id(record.id):
            found.append(_seen(call, record, written))
    return found


def named(call: CommandCall, name: str) -> list[Candidate]:
    """Everyone known by exactly this name (any case): on the caller's server, then seen here."""
    here = [_on(p) for p in sorted(call.state.players.values(), key=lambda p: p.number) if plain(p.name) == plain(name)]
    return here + [c for c in _seen_named(call, name) if c.id not in {h.id for h in here}]


async def ask(call: CommandCall, about: str, found: list[Candidate], *, alike: bool = False) -> None:
    """Several players could be meant: the caller gets each with what tells them apart, and what to type. Those not on
    this server are the caller's @1, @2… for ten minutes. `alike`: nobody has the name itself, these only look like
    it (letters of another script or style): said so, and picked the same way, even when there's one."""
    shown = found[:LISTED]
    away = [c for c in shown if not c.number]
    numbers = dict(zip((c.key for c in away), (n for n, _ in call.picks.offer(call.player.id, away))
                       if call.picks is not None else range(1, len(away) + 1)))
    # A plain player asking (!report) sees who they are and how to pick: others' ranks and mutes aren't theirs.
    plain = ROLES.index(call.role) < ROLES.index(PROBATION)
    items = [c.describe(f"#{c.number}" if c.number else f"@{numbers[c.key]}", _role(call, c), here=call.server,
                        plain=plain) for c in shown]
    more = [f"…and {len(found) - LISTED} more: a longer name or the ID narrows it."] if len(found) > LISTED else []
    use = "#number" if not away else "@number" if len(away) == len(shown) else "#number or @number"
    if alike:
        head = (f"Nobody here is called {about}. {'These look' if len(found) > 1 else 'This looks'} like it; use the "
                f"{use}:")
    else:
        head = f"{about} could be {len(found)} players. Use the {use}:"
    await call.reply("\n".join([head, *items, *more]))


def _role(call: CommandCall, candidate: Candidate) -> str:
    if call.role_of is None or not candidate.id:
        return ""
    return call.role_of(Player(candidate.number, candidate.name, candidate.id))


def _away(call: CommandCall, candidate: Candidate, scope: str, explicit: bool = False) -> Player | None:
    """Someone who isn't on the caller's server, for a command of this scope: who they are (number 0), or None. Scope
    "any" (a kick: every server's, [automod] across_servers): someone on another server, or anyone named by player ID or
    @number (`explicit`); a name alone doesn't reach someone who's left, whom another player may be called now."""
    if scope == "seen" or (scope == "any" and (candidate.where or explicit)):
        return Player(0, candidate.name, candidate.id)
    return None


async def _not_here(call: CommandCall, candidate: Candidate, scope: str = "on") -> None:
    who = call.tag(candidate.id, candidate.name) if candidate.id else candidate.name
    if candidate.where:
        await call.reply(f"{who} is on another server, not this one.")
    elif scope == "any":
        await call.reply(f"{who} isn't on any server now: their ID or @number names them anyway.")
    else:
        await call.reply(f"{who} isn't on this server now.")


async def find(call: CommandCall, ref: str, *, scope: str = "on", start: bool = False) -> Player | None:
    """The one player `ref` means, or None after telling the caller why. With scope "seen", someone who isn't on this
    server comes back too, as a Player with number 0 (by their player ID); with "on", they're "not on now".

    In order: @1 (the caller's last list), #number (or the number alone), a player ID, a name, the first 8 or more
    characters of a player ID; the start of a name only with `start`, and only when one player on has it."""
    ref = ref.strip()
    if len(ref) > 1 and ref[0] in QUOTES and ref[-1] == QUOTES[ref[0]]:
        ref = ref[1:-1].strip()  # "Master Chief", as the console takes it
    state, history = call.state, call.history
    if not ref:
        await call.reply(f"Name a player: their #number from {call.prefix}players, their name or player ID.")
        return None
    if pick := PICK.fullmatch(ref):
        got = call.picks.get(call.player.id, int(pick[1])) if call.picks is not None else "You have no list to pick from."
        if isinstance(got, str):
            await call.reply(got)
            return None
        if got.id:
            call.picked[got.id] = f"@{pick[1]}"
        if player := state.player_by_id(got.id):
            return player
        if (away := _away(call, got, scope, explicit=True)) is None:
            await _not_here(call, got, scope)
        return away
    number = NUMBER.fullmatch(ref)
    if number:
        player = state.players.get(int(number[1]))
        named = [p for p in sorted(state.players.values(), key=lambda p: p.number)
                 if not ref.startswith("#") and plain(p.name) == plain(ref) and p is not player]
        if player is not None and named:
            # Someone called "4" beside #4: a troll's name to have the wrong player hit, so the caller picks (5.9)
            await ask(call, f"'{ref}'", [_on(player), *(_on(p) for p in named)])
            return None
        if player is not None:
            call.picked[player.id] = f"#{player.number}"
            return player
        if ref.startswith("#"):
            await call.reply(f"Nobody is #{number[1]} on this server now. {call.prefix}players lists them.")
            return None
        # The number alone could still be someone's name.
    if FULL_ID.fullmatch(ref):
        player_id = ref.lower()
        if player := state.player_by_id(player_id):
            return player
        record = history.get(player_id) if history is not None else None
        candidate = _seen(call, record) if record else Candidate(player_id, "(never seen here)")
        if (away := _away(call, candidate, scope, explicit=True)) is None:
            await _not_here(call, candidate, scope)
        return away
    folded, shape = plain(ref), looks(ref)
    here = [_on(p) for p in sorted(state.players.values(), key=lambda p: p.number) if plain(p.name) == folded]
    # Someone on whose name only looks like it (ΒRAVO, a Greek Β, beside Bravo): the one an impersonator would want
    # acted on in their place, so the caller picks, as between two players with one name.
    lookalikes = [_on(p) for p in sorted(state.players.values(), key=lambda p: p.number)
                  if looks(p.name) == shape and plain(p.name) != folded]
    if here and lookalikes:
        await ask(call, f"'{ref}'", here + lookalikes)
        return None
    away_named = [c for c in _seen_named(call, ref) if c.id not in {h.id for h in here}]
    if scope in ("on", "any") and here:
        away_named = []  # someone on has it: someone gone can't be acted on here anyway
    if scope == "any":
        away_named = [c for c in away_named if c.where] or away_named[:1]  # on another server; else one who's left, told so
    found = here + away_named
    if len(found) == 1:
        if found[0].number:
            return state.players[found[0].number]
        if (away := _away(call, found[0], scope)) is None:
            await _not_here(call, found[0], scope)
        return away
    if found:
        await ask(call, f"'{ref}'", found)
        return None
    if ID_START.fullmatch(ref):
        id_start = ref.lower()
        on = [p for p in state.players.values() if p.id.startswith(id_start)]
        if len(on) == 1:
            return on[0]
        records = [r for r in (history.find(ref) if history is not None else []) if r.id.startswith(id_start)]
        if len(records) == 1 and not on:
            candidate = _seen(call, records[0])
            if (away := _away(call, candidate, scope, explicit=True)) is None:
                await _not_here(call, candidate, scope)
            return away
        if on or records:
            await ask(call, f"'{ref}'", [_on(p) for p in on] + [_seen(call, r) for r in records
                                                                if not state.player_by_id(r.id)])
            return None
    # Letters of another script or style (ΒRAVO for Bravo, or Bravo for a player who wrote ΒRAVO): never acted on,
    # always asked, as for two players with one name.
    if lookalikes:
        await ask(call, f"'{ref}'", lookalikes, alike=True)
        return None
    starts = sorted((p for p in state.players.values() if plain(p.name).startswith(folded)), key=lambda p: p.number)
    if start and len(starts) == 1:
        return starts[0]
    starts += sorted((p for p in state.players.values() if p not in starts and len(shape) >= 3
                      and looks(p.name).startswith(shape)), key=lambda p: p.number)  # offered, never acted on
    if starts:
        listed = [call.tag(p) for p in starts[:LISTED]]
        hint = f"#{starts[0].number}" if len(starts) == 1 else "the #number"
        head = f"Nobody here is called '{ref}'. Did you mean {'this' if len(listed) == 1 else 'one of these'}? Use {hint}:"
        await call.reply("\n".join([head, *listed]))
        return None
    if number:
        await call.reply(f"Nobody is #{number[1]} on this server now. {call.prefix}players lists them.")
    elif history is not None:
        await call.reply(f"Nobody called '{ref}' is on or has been seen. Use their #number or player ID.")
    else:
        await call.reply(f"No player matches '{ref}'. Use their #number from {call.prefix}players.")
    return None


def outranked(call: CommandCall, player: Player) -> str:
    """Why the caller can't act on this player ("" when they can): nobody acts on their own role or above, faculty
    excepted. Roles follow the player ID, so this holds for someone who isn't on too."""
    if call.role == "faculty" or call.role_of is None:
        return ""
    role = call.role_of(player)
    if ROLES.index(role) < ROLES.index(call.role):
        return ""
    return f"{call.tag(player)} is {a_role(role)}; you can't do that to them."


async def player_and_rest(call: CommandCall, text: str | None = None, *, scope: str = "on", acts: bool = False,
                          start: bool = False,
                          follows: Callable[[str], bool] | None = None) -> tuple[Player | None, list[str]]:
    """The player a line names first, however many words their name has, and the words after them (a time, a rule, a
    reason); (None, …) after telling the caller why nobody."""
    ref, rest = await split(call, text, follows=follows)
    if ref is None:
        return None, rest
    player = await find(call, ref, scope=scope, start=start)
    if player is not None and acts and (why := outranked(call, player)):
        await call.reply(why)
        return None, rest
    return player, rest


async def offer(call: CommandCall, entries: list[Candidate]) -> list[str]:
    """A list's entries numbered @1, @2… for the caller (ten minutes), each described: for !bans, !mutes, !kicks."""
    numbered = call.picks.offer(call.player.id, entries) if call.picks is not None else list(enumerate(entries, 1))
    return [c.describe(f"@{n}", _role(call, c), here=call.server) for n, c in numbered]
