"""Players as the window writes and finds them: one tag each (core/tags.py: "Nate Higgers 1a2b3c4d (#4) [HugeGrass]"),
and a #number, name or player ID typed at the console (A), at bans and admins (B) or for a role (D -> C) found the way
the game's own commands find one (players/targets.py): a name several players share, or one that only looks like
someone's, acts on nobody until the host picks from a numbered list.

The window knows who's on each server from the dashboard's rows (each lists its players apart, under `online`) and
who's been seen from Huragok's records, read read-only. A second window has both too, so it writes and finds a player
the same way as the supervising one. #numbers are each server's own: (#4) is shown for players on the server whose
console the host is typing to, where they can be typed back."""

from __future__ import annotations

import ipaddress
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.storage import PlayerRecord, last_names, seen_players
from ..core.tags import SHORT, tag
from ..core.text import is_player_id
from ..players.targets import LISTED, QUOTES, ago, looks, plain

__all__ = ["CONSOLE", "ConsoleLine", "Finding", "Found", "Known", "OnServer", "console_line", "find", "is_address",
           "on_server"]

Row = dict[str, Any]

NUMBER = re.compile(r"#?(\d{1,4})")  # a player's #number on a server; the number alone means it too, as on the console
ID_START = re.compile(r"[0-9a-fA-F]{%d,63}" % SHORT)  # the start of a player ID, as every tag shows it


@dataclass(frozen=True)
class OnServer:
    """A player on a server, as its dashboard row lists them."""

    server: str
    number: int
    name: str
    id: str
    team: str = ""
    admin: bool = False
    muted: bool = False
    joined: float = 0.0


def on_server(row: Row) -> list[OnServer] | None:
    """A dashboard row's players, by number. None when the row doesn't list them apart (records/live.json written by a
    version before 0.7.0, which has their names alone): who's on can't be checked then."""
    entries = row.get("online")
    if not isinstance(entries, list):
        return None
    players = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number, joined = entry.get("number"), entry.get("joined")
        players.append(OnServer(str(row.get("name") or ""), number if isinstance(number, int) else 0,
                                str(entry.get("name") or ""), str(entry.get("id") or "").lower(),
                                str(entry.get("team") or ""), bool(entry.get("admin")), bool(entry.get("muted")),
                                float(joined) if isinstance(joined, (int, float)) else 0.0))
    return players


class Known:
    """Who a player ID is, as the window knows: the name they have on a server now (the dashboard's rows), else the one
    a server's event gave them, else the last one Huragok's records have; and the server they're on. Made for one screen:
    what the records say is looked up once."""

    def __init__(self, rows: Callable[[], list[Row]], database: Path | None | Callable[[], Path | None]) -> None:
        self.rows = rows
        self._database = database  # the records' file, or what says where it is (asked the first time it's needed)
        self.heard: dict[str, str] = {}  # names events gave, by player ID (0.10 names a player after they join)
        self._names: dict[str, str] = {}  # the records' last names, by player ID ("" for never seen)
        self._records: dict[str, PlayerRecord | None] = {}
        self._current: list[Row] = []
        self._read = -1e9  # time.monotonic() the rows were last asked for

    @property
    def database(self) -> Path | None:
        if callable(self._database):
            self._database = self._database()
        return self._database

    def _rows(self) -> list[Row]:
        """The rows as they are now: asked again after a second (one typed line looks at them several times)."""
        if time.monotonic() - self._read >= 1.0:
            self._current, self._read = list(self.rows()), time.monotonic()
        return self._current

    def everyone(self) -> list[OnServer]:
        """Everyone on every server, as the rows list them."""
        return [player for row in self._rows() for player in (on_server(row) or [])]

    def here(self, server: str) -> list[OnServer] | None:
        """Who's on that server; None when its row doesn't say (or there's no such row)."""
        row = next((r for r in self._rows() if r.get("name") == server), None)
        return None if row is None else on_server(row)

    def on(self, player_id: str) -> OnServer | None:
        """Where they're on now; None for nowhere."""
        player_id = str(player_id or "").lower()
        return next((p for p in self.everyone() if p.id == player_id), None) if player_id else None

    def learn(self, player_id: Any, name: Any) -> None:
        """A name a server's event gave them."""
        player_id = str(player_id or "").lower()
        if is_player_id(player_id) and str(name or "").strip():
            self.heard[player_id] = str(name)

    def record(self, player_id: str) -> PlayerRecord | None:
        """What Huragok's records have of them (every name they've used); None when they were never seen."""
        player_id = str(player_id or "").lower()
        if player_id not in self._records:
            found = seen_players(self.database, player_id, limit=1) if is_player_id(player_id) else []
            self._records[player_id] = found[0] if found and found[0].id == player_id else None
        return self._records[player_id]

    def names(self, player_ids: list[str]) -> dict[str, str]:
        """The name each goes by, as name() gives it, looked up together: by player ID in lower case."""
        wanted = [str(p).lower() for p in player_ids if is_player_id(str(p))]
        unknown = [p for p in wanted if p not in self._names]
        if unknown:
            found = last_names(self.database, unknown)
            self._names.update({p: found.get(p, "") for p in unknown})
        return {p: self.name(p) for p in wanted}

    def name(self, player_id: str, given: str = "") -> str:
        """The name they go by: on a server now, the one it has; else the one an event gave; else the last one the
        records have; else `given` (what a note or a record says)."""
        player_id = str(player_id or "").lower()
        live = self.on(player_id)
        if live is not None and live.name:
            return live.name
        if player_id in self.heard:
            return self.heard[player_id]
        if is_player_id(player_id) and player_id not in self._names:
            self._names[player_id] = last_names(self.database, [player_id]).get(player_id, "")
        return self._names.get(player_id) or given

    def is_name(self, name: str) -> bool:
        """Whether someone goes by exactly this name (any case): on a server now, or seen under it before."""
        folded = plain(name)
        if any(plain(p.name) == folded for p in self.everyone()):
            return True
        return any(plain(n) == folded for r in seen_players(self.database, name, limit=3) for n in [r.last_name, *r.names])

    def tag(self, player_id: str, given: str = "", *, here: str = "", whole: bool = False) -> str:
        """Their tag, with the server they're on now, and their #number when that's `here`: the server whose console
        the host types to."""
        live = self.on(player_id)
        handle = f"#{live.number}" if live is not None and here and live.server == here and live.number else ""
        return tag(self.name(player_id, given), player_id, handle=handle, server=live.server if live else "",
                   whole=whole)


# Finding a player ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Found:
    """Someone a typed reference could mean."""

    id: str
    name: str
    server: str = ""  # the server they're on now; "" for none
    number: int = 0  # their #number there
    since: float = 0.0  # on since (on a server), or when they were last seen
    team: str = ""
    muted: bool = False

    @classmethod
    def live(cls, player: OnServer) -> Found:
        return cls(player.id, player.name, player.server, player.number, player.joined, player.team, player.muted)

    def tag(self, here: str = "", *, whole: bool = False) -> str:
        """Their tag; their #number when they're on `here`, the server whose console the host types to."""
        handle = f"#{self.number}" if self.number and here and self.server == here else ""
        return tag(self.name, self.id, handle=handle, server=self.server, whole=whole)

    def describe(self, here: str = "", now: float | None = None) -> str:
        """"Nate 1a2b3c4d (#4) [HugeGrass]: red, on 12m": the tag, then what tells them apart."""
        now = time.time() if now is None else now
        bits: list[str] = []
        if self.server:
            bits += [self.team.lower()] if self.team else []
            bits += [f"on {ago(now - self.since)}"] if self.since else []
        elif self.since:
            bits.append(f"seen {ago(now - self.since)} ago")
        bits += ["muted"] if self.muted else []
        return self.tag(here) + (f": {', '.join(bits)}" if bits else "")


@dataclass
class Finding:
    """What a reference came to: one player (`found` of one), several to pick from (the host picks one, or none),
    nobody (`why`), or not a player at all (`as_typed`: an address, a range, a name only the server's own list has)."""

    found: list[Found] = field(default_factory=list)
    why: str = ""
    head: str = ""  # said above the list to pick from
    as_typed: bool = False
    unknown: bool = False  # nobody has that name or ID start, on a server or in the records (not a #number nobody has)

    @property
    def one(self) -> Found | None:
        return self.found[0] if len(self.found) == 1 and not self.head else None


def is_address(text: str) -> bool:
    """An address or a range of them (203.0.113.4, 203.0.113.0/24, 2001:db8::1): never a player's name or ID."""
    try:
        ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return False
    return True


def _unquoted(ref: str) -> str:
    ref = ref.strip()
    if len(ref) > 1 and ref[0] in QUOTES and ref[-1] == QUOTES[ref[0]]:
        return ref[1:-1].strip()
    return ref


def find(known: Known, here: str | None, ref: str, *, scope: str = "seen") -> Finding:
    """Who `ref` means, as the game's commands find a player: an address is none; then #number (or the number alone),
    a whole player ID, a name (exactly, in any case), the first 8 or more characters of a player ID. A name several
    players have, or one that only looks like someone's (letters of another script), is a list to pick from, never one
    of them. `here`: the server the command goes to (None: no server, as for a role: a #number means nothing there).
    `scope` "here": only someone on that server; "seen": anyone on a server or in the records."""
    ref = _unquoted(ref)
    if not ref:
        return Finding(why="Name a player: their #number, their name, or their player ID (or its first 8 characters).")
    if is_address(ref):
        return Finding(as_typed=True)
    on_here = known.here(here) if here else []
    if on_here is None:  # the row doesn't list who's on: the server finds them by its own rules, as ever
        return Finding(as_typed=True)
    everyone = known.everyone()
    where = f" on {here}" if here else ""
    if number := NUMBER.fullmatch(ref):
        if here is None:
            if ref.startswith("#"):
                return Finding(why="A #number is a server's own: type their name or player ID (or its first 8 "
                                   "characters) here.")
        else:
            player = next((p for p in on_here if p.number == int(number[1])), None)
            named = [p for p in on_here if not ref.startswith("#") and plain(p.name) == plain(ref) and p is not player]
            if player is not None and named:  # someone called "4" beside #4: the host picks
                return Finding([Found.live(p) for p in [player, *named]], head=f"'{ref}' could be {1 + len(named)} "
                                                                               "players:")
            if player is not None:
                return Finding([Found.live(player)])
            if ref.startswith("#"):
                return Finding(why=f"Nobody is #{number[1]}{where} now.")
    if is_player_id(ref):
        player_id = ref.lower()
        live = known.on(player_id)
        if scope == "here" and (live is None or live.server != here):
            return Finding(why=f"{known.tag(player_id)} isn't{where} now.")
        if live is not None:
            return Finding([Found.live(live)])
        record = known.record(player_id)
        return Finding([Found(player_id, known.name(player_id), since=record.last_seen if record else 0.0)])
    folded, shape = plain(ref), looks(ref)
    named_here = [p for p in on_here if plain(p.name) == folded]
    lookalikes = [p for p in on_here if looks(p.name) == shape and plain(p.name) != folded]
    if named_here and lookalikes:  # Bravo, and ΒRAVO with a Greek Β: the one an impersonator would want hit
        found = [Found.live(p) for p in named_here + lookalikes]
        return Finding(found, head=f"'{ref}' could be {len(found)} players:")
    away = _named_away(known, here, ref, everyone)
    if scope == "here":
        if len(named_here) == 1:
            return Finding([Found.live(named_here[0])])
        if named_here:
            return Finding([Found.live(p) for p in named_here], head=f"'{ref}' could be {len(named_here)} players:")
        if away:
            return Finding(why=f"Nobody called '{ref}' is{where} now: " + "; ".join(a.tag() for a in away[:LISTED])
                               + ".")
    else:
        found = [Found.live(p) for p in named_here] + away
        if len(found) == 1:
            return Finding(found)
        if found:
            return Finding(found[:LISTED * 2], head=f"'{ref}' could be {len(found)} players:")
    if ID_START.fullmatch(ref):
        start = ref.lower()
        live_ones = [p for p in (on_here if scope == "here" else everyone) if p.id.startswith(start)]
        ids = {p.id for p in everyone}
        seen = [] if scope == "here" else [r for r in seen_players(known.database, ref) if r.id.startswith(start)
                                           and r.id not in ids]
        found = [Found.live(p) for p in live_ones] + [Found(r.id, r.last_name, since=r.last_seen) for r in seen]
        if len(found) == 1:
            return Finding(found)
        if found:
            return Finding(found[:LISTED * 2], head=f"'{ref}' is the start of {len(found)} player IDs:")
    if lookalikes:
        found = [Found.live(p) for p in lookalikes]
        return Finding(found, head=f"Nobody{where} is called '{ref}'. {'These look' if len(found) > 1 else 'This looks'} "
                                   "like it:")
    starts = [p for p in on_here if plain(p.name).startswith(folded)]
    starts += [p for p in on_here if p not in starts and len(shape) >= 3 and looks(p.name).startswith(shape)]
    if starts:
        return Finding([Found.live(p) for p in starts[:LISTED * 2]],
                       head=f"Nobody{where} is called '{ref}'. Did you mean {'this' if len(starts) == 1 else 'one of these'}?")
    if scope == "here":
        return Finding(why=f"Nobody called '{ref}' is{where} now.", unknown=True)
    return Finding(why=f"Nobody called '{ref}' is on a server or in Huragok's records.", unknown=True)


def _named_away(known: Known, here: str | None, ref: str, everyone: list[OnServer]) -> list[Found]:
    """Everyone with exactly this name (any case) who isn't on `here`: on another server, or seen before (written as
    they wrote it)."""
    folded = plain(ref)
    found = [Found.live(p) for p in everyone if p.server != here and plain(p.name) == folded]
    on_ids = {p.id for p in everyone}
    for record in seen_players(known.database, ref, limit=LISTED * 2):
        if record.id in on_ids:
            continue
        written = next((n for n in [record.last_name, *record.names] if plain(n) == folded), None)
        if written is not None:
            found.append(Found(record.id, written, since=record.last_seen))
    return found


# A console line that names a player -------------------------------------------------------------------------------

# The console's commands that name a player, by their first word or two, and how the window sends the one it found:
#   "here"    someone on that server, sent as their #number
#   "number"  anyone, sent as their #number when they're on that server, else as their player ID
#   "id"      anyone, sent as their player ID (the command takes no #number); a name nobody's been seen under goes as
#             typed, since the server keeps names in that list ("the name they were kicked under")
CONSOLE: dict[tuple[str, ...], str] = {
    ("kick",): "here", ("mute",): "here", ("unmute",): "here", ("tell",): "here", ("team",): "here",
    ("setteam",): "here", ("banip",): "here", ("startvote", "kick"): "here",
    ("ban",): "number", ("vpnallow",): "number", ("admin", "add"): "number", ("admins", "add"): "number",
    ("immune", "add"): "number", ("role", "add"): "number", ("moderator", "add"): "number",
    ("unban",): "id", ("pardon",): "id", ("vpnrevoke",): "id", ("admin", "remove"): "id", ("admins", "remove"): "id",
    ("immune", "remove"): "id", ("role", "remove"): "id", ("moderator", "remove"): "id",
}


@dataclass(frozen=True)
class ConsoleLine:
    """A console line that names a player, in three: what comes before them, them as typed, and the rest (a time, a
    reason), each as typed."""

    head: str  # "kick ", "admin add "
    ref: str  # "#3", "Nate Higgers", '"Nate Higgers"'
    rest: str  # " camping"
    sends: str  # CONSOLE's: "here", "number" or "id"

    def sent(self, found: Found, server: str) -> str:
        """The line as the server is sent it, the player as it takes them."""
        if self.sends != "id" and found.number and found.server == server:
            return f"{self.head}#{found.number}{self.rest}"
        return f"{self.head}{found.id}{self.rest}"


def console_line(line: str, is_name: Callable[[str], bool]) -> ConsoleLine | None:
    """The player a console line names, when its command names one; None for any other line. The player is the
    longest name the words after the command start with, word for word (`is_name` says which are names: a player's
    on a server, or one seen), else the next word; a name in quotes is taken as written."""
    words = list(re.finditer(r"\S+", line))
    if not words:
        return None
    for size in (2, 1):
        key = tuple(w.group().lower() for w in words[:size])
        if len(key) == size and key in CONSOLE and len(words) > size:
            sends = CONSOLE[key]
            break
    else:
        return None
    after = words[size - 1].end()
    text = line[after:]
    start = len(text) - len(text.lstrip())
    if text[start:start + 1] in QUOTES:
        close = QUOTES[text[start]]
        end = next((i for i in range(start + 1, len(text)) if text[i] == close and text[i + 1:i + 2] in ("", " ")), 0)
        if end > start + 1:
            return ConsoleLine(line[:after + start], text[start:end + 1], text[end + 1:], sends)
    rest_words = words[size:]
    take = 1
    for count in range(len(rest_words), 1, -1):
        name = " ".join(w.group() for w in rest_words[:count])
        if is_name(name):
            take = count
            break
    end = rest_words[take - 1].end()
    return ConsoleLine(line[:rest_words[0].start()], line[rest_words[0].start():end], line[end:], sends)
