"""Who a command means. Every in-game command that names a player finds them here, so every one has the same guards:
declared on the command (`@command(…, player="on" | "seen", acts=True)`: the router finds the player before the
command runs), or asked for by a command that reads its own words (`call.find`, `call.player_and_rest`).

Names can't be trusted to pick a player: they're free text, several players share one (30 "Player"s one night),
they have spaces, capitals and apostrophes, and they change at will. What a command acts on is a player ID, so:

- The player is the longest name the line starts with ("!kick Nate Higgers spam"), word for word in any case;
  quotes work too, as on the console. A name that is the start of a longer one, with more words after it, could be
  either: the caller is asked, unless the next word is one the command expects there (a time, a rule).
- A name more than one player has acts on nobody. The caller gets each of them with what tells them apart (how the
  name is written, their team, how long they've been on or when they were last here, their role, a mute, the start
  of their player ID) and what to type: #4 for someone on this server, @1 for someone who isn't (the caller's own
  list, for ten minutes). For a command that reaches players who left (scope "seen"), the ones seen here count too.
- The start of a name acts on nobody, except for a command that only looks or lifts (`start`), and then only when
  one player on has it.
- Nobody acts on someone of their own role or above (`acts`; owners excepted), named by name, number or ID alike.

Lists of players who aren't on (!bans, !mutes, !kicks) number their entries the same way, so `!unban @2` is the
second ban the caller was shown.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from ..core.commandsfile import ROLES
from ..core.text import PLAYER_ID, lookalike
from ..servers.state import Player
from .roles import a_role

if TYPE_CHECKING:
    from .router import CommandCall

NUMBER = re.compile(r"#?(\d{1,4})")  # a player's #number; the number alone means it too, as on the console
PICK = re.compile(r"@(\d{1,3})")  # an entry of the caller's last list
FULL_ID = PLAYER_ID  # a whole player ID, either case
ID_START = re.compile(r"[0-9a-fA-F]{8,63}")  # the start of a player ID: lists show the first 8
QUOTES = {'"': '"', "“": "”", "'": "'"}  # a name in quotes, as the console takes it: "Master Chief"
TIME = re.compile(r"\d+[smhdw]", re.IGNORECASE)
FOREVER = {"perm", "permanent", "forever", "never"}
TEAMS = {"red", "blue", "green", "orange", "purple", "gold", "brown", "pink"}
PICKS_SECONDS = 600  # how long @numbers stand for a list's entries
LISTED = 6  # players named in one "could be" answer
SCOPES = ("on", "seen")


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

    def describe(self, handle: str, role: str = "", now: float | None = None) -> str:
        now = time.time() if now is None else now
        bits: list[str] = []
        if self.number:
            bits += [self.team.lower()] if self.team else []
            bits += [f"on {ago(now - self.since)}"] if self.since else []
        elif self.where:
            bits.append(f"on {self.where}")
        elif self.since:
            bits.append(f"seen {ago(now - self.since)} ago")
        bits += [role] if role and role != "player" else []
        bits += ["muted"] if self.muted else []
        bits += [self.note] if self.note else []
        bits += [self.id[:8]] if self.id else []
        return f"{handle} {self.name}" + (f" ({', '.join(bits)})" if bits else "")


class Picks:
    """Each caller's last list of players who aren't on: @1, @2… stand for its entries for ten minutes."""

    def __init__(self) -> None:
        self._lists: dict[str, tuple[float, list[Candidate]]] = {}

    def offer(self, caller: str, entries: list[Candidate]) -> None:
        self._lists[caller] = (time.monotonic(), list(entries))

    def get(self, caller: str, number: int) -> Candidate | str:
        """The entry, or why there's none."""
        made, entries = self._lists.get(caller, (0.0, []))
        if not entries:
            return f"You have no list for @{number}: @numbers are the players a list or a question named last."
        if time.monotonic() - made > PICKS_SECONDS:
            return f"Your last list is over {PICKS_SECONDS // 60} minutes old: ask again for a fresh one."
        if not 1 <= number <= len(entries):
            return f"Your last list has @1 to @{len(entries)}."
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
    if away and call.picks is not None:
        call.picks.offer(call.player.id, away)
    items, n = [], 0
    for c in shown:
        if c.number:
            handle = f"#{c.number}"
        else:
            n += 1
            handle = f"@{n}"
        items.append(c.describe(handle, _role(call, c)))
    more = f" and {len(found) - LISTED} more" if len(found) > LISTED else ""
    use = "#number" if not away else "@number" if len(away) == len(shown) else "#number or @number"
    if alike:
        await call.reply(f"Nobody here is called {about}. {'These look' if len(found) > 1 else 'This looks'} like it: "
                         f"{'; '.join(items)}{more}. Use the {use}.")
        return
    await call.reply(f"{about} could be {'; '.join(items)}{more}. Use the {use}.")


def _role(call: CommandCall, candidate: Candidate) -> str:
    if call.role_of is None or not candidate.id:
        return ""
    return call.role_of(Player(candidate.number, candidate.name, candidate.id))


def _away(call: CommandCall, candidate: Candidate, scope: str) -> Player | None:
    """Someone who isn't on the caller's server, for a command of this scope: who they are (number 0), or None."""
    if scope == "seen":
        return Player(0, candidate.name, candidate.id)
    return None


async def _not_here(call: CommandCall, candidate: Candidate) -> None:
    if candidate.where:
        await call.reply(f"{candidate.name} is on {candidate.where}, not this server.")
    else:
        await call.reply(f"{candidate.name} isn't on this server now.")


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
        if player := state.player_by_id(got.id):
            return player
        if (away := _away(call, got, scope)) is None:
            await _not_here(call, got)
        return away
    number = NUMBER.fullmatch(ref)
    if number:
        player = state.players.get(int(number[1]))
        if player is not None:
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
        candidate = _seen(call, record) if record else Candidate(player_id, player_id[:8])
        if (away := _away(call, candidate, scope)) is None:
            await _not_here(call, candidate)
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
    if scope == "on" and here:
        away_named = []  # someone on has it: someone gone can't be acted on here anyway
    found = here + away_named
    if len(found) == 1:
        if found[0].number:
            return state.players[found[0].number]
        if (away := _away(call, found[0], scope)) is None:
            await _not_here(call, found[0])
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
            if (away := _away(call, candidate, scope)) is None:
                await _not_here(call, candidate)
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
        listed = ", ".join(f"{p.name} (#{p.number})" for p in starts[:LISTED])
        hint = f"#{starts[0].number}" if len(starts) == 1 else "the #number"
        await call.reply(f"Nobody here is called '{ref}'. Did you mean {listed}? Use {hint}.")
        return None
    if number:
        await call.reply(f"Nobody is #{number[1]} on this server now. {call.prefix}players lists them.")
    elif history is not None:
        await call.reply(f"Nobody called '{ref}' is on or has been seen. Use their #number or player ID.")
    else:
        await call.reply(f"No player matches '{ref}'. Use their #number from {call.prefix}players.")
    return None


def outranked(call: CommandCall, player: Player) -> str:
    """Why the caller can't act on this player ("" when they can): nobody acts on their own role or above, owners
    excepted. Roles follow the player ID, so this holds for someone who isn't on too."""
    if call.role == "owner" or call.role_of is None:
        return ""
    role = call.role_of(player)
    if ROLES.index(role) < ROLES.index(call.role):
        return ""
    return f"{player.name} is {a_role(role)}; you can't do that to them."


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
    if call.picks is not None:
        call.picks.offer(call.player.id, entries)
    return [c.describe(f"@{n}" + (f" #{c.number}" if c.number else ""), _role(call, c))
            for n, c in enumerate(entries, 1)]
