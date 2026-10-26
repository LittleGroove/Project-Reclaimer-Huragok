"""Who's who in Huragok, and the New Mombasa look the window and the commands share.

Huragok is named for the Engineer of Halo 3: ODST. During the Battle of Mombasa a
Huragok called Quick to Adjust found New Mombasa's city AI, the Superintendent, dying
in its data centre beneath the city. It repaired it, then took in everything it held,
the Superintendent's subroutine Vergil included, and carried it out of the city. In
this program the three are its parts:

  HURAGOK         the Engineer: the program itself, and what it builds and mends
                  (new servers, packages, content, the Workshop, updates, the window)
  SUPERINTENDENT  the city AI that kept New Mombasa running: it keeps every server
                  up, starts, stops, restarts and counts down, and its watchdog
                  notices a server in trouble
  VERGIL          the subroutine Dr. Endesha wrote to watch over his daughter Sadie:
                  it watches over the players (automod, in-game commands, bans, mutes,
                  and the console relay for moderators)

Each has a four-letter tag on every log line, the way the Rookie's HUD labelled the
Engineer "VRGL", and a colour. The Superintendent speaks to players in the public
notices it put on New Mombasa's signs (NOTICES), and its face on the dashboard shows
how the servers are doing, with the eight expressions it had in the game (FACES).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Persona:
    name: str  # "Superintendent"
    tag: str  # "SUPT": on every log line it speaks
    colour: str  # a tui colour name
    does: str  # what it does here, in a few words
    story: str  # who it was in New Mombasa, and what it is here


HURAGOK = Persona(
    "Huragok", "HRGK", "violet", "builds and mends: new servers, content, the Workshop, updates",
    "the Engineer, Quick to Adjust: it mended the dying city AI and carried it out of the city. Here it builds "
    "and mends: new servers, packages, content, the Workshop, updates, this window")
SUPERINTENDENT = Persona(
    "Superintendent", "SUPT", "amber", "keeps the servers running: starts, stops, restarts, the watchdog",
    "New Mombasa's city AI, which kept the lights, trains and signs running. Here it keeps every server up: starts, "
    "stops, restarts, countdowns, and the watchdog that notices a server in trouble")
VERGIL = Persona(
    "Vergil", "VRGL", "sky", "watches over the players: automod, in-game commands, bans, mutes",
    "the subroutine Dr. Endesha wrote to watch over his daughter Sadie, through the city's cameras and microphones. "
    "Here it watches over the players: automod, in-game commands, bans, mutes, the moderators' console relay")
PERSONAS = (SUPERINTENDENT, VERGIL, HURAGOK)

# What the colours mean, the way an ODST's VISR marks what it sees: (colour, meaning).
VISR = (
    ("green", "friendly: running, healthy, done (VISR: allies)"),
    ("red", "hostile: crashed, FAULTED, errors (VISR: enemies)"),
    ("yellow", "look here: a warning, something to check (VISR: beacons and terminals)"),
    ("cyan", "starting up; players joining and leaving, chat, moderation"),
    ("magenta", "restarts and countdowns"),
)

# Which part speaks, by logger name (without the "huragok." prefix): the code's folders are the parts. servers/
# and each server's console link are the Superintendent's, players/ Vergil's, everything else Huragok's; parts log
# as "part.<name>". The first match wins, by whole dotted names.
SPEAKERS: tuple[tuple[str, Persona], ...] = (
    ("part.automod", VERGIL), ("part.commands", VERGIL), ("part.messages", VERGIL),
    ("part.watchdog", SUPERINTENDENT), ("part.eventlog", SUPERINTENDENT),
    ("players", VERGIL), ("vergil", VERGIL),
    ("servers", SUPERINTENDENT), ("superintendent", SUPERINTENDENT), ("rcon", SUPERINTENDENT),
    ("core.runner", SUPERINTENDENT), ("core.mailbox", SUPERINTENDENT), ("core.events", SUPERINTENDENT),
    ("core.winproc", SUPERINTENDENT),
)
# The module names before the engine was laid out by part (0.4.4 and earlier), matched whole: huragok.log keeps the
# logger's name, and a second window reads an older version's lines back.
OLD_LOGGERS = {
    **dict.fromkeys(("chat", "moderation", "permissions", "relay", "storage"), VERGIL),
    **dict.fromkeys(("manager", "lifecycle", "state", "runner", "mailbox", "winproc", "events", "files", "core",
                     "supervisor"), SUPERINTENDENT),
}
TAGS = {persona.tag: persona for persona in PERSONAS}


def speaker(logger: str) -> Persona:
    """The part behind a logger: `huragok.servers.fleet` and `part.watchdog` are the Superintendent's."""
    name = logger.removeprefix("huragok.")
    if name in OLD_LOGGERS:
        return OLD_LOGGERS[name]
    for prefix, persona in SPEAKERS:
        if name == prefix or name.startswith(prefix + "."):
            return persona
    return HURAGOK


# Which part each of the dashboard's keys belongs to; screens are titled with it.
KEYS = {
    **dict.fromkeys("SXRKTLV", SUPERINTENDENT),
    **dict.fromkeys("PMAB", VERGIL),
    **dict.fromkeys("NGIDCOUWHQ?", HURAGOK),
}


# The Superintendent's public notices, word for word as it posted them in Bungie's "KEEP IT CLEAN" viral
# campaign of 2008. Players read them in front of what the Superintendent tells them; the dashboard shows one.
NOTICES = {
    "clean": "KEEP IT CLEAN!",
    "calm": "PLEASE REMAIN CALM!",
    "emergency": "METROPOLITAN EMERGENCY!",
    "detour": "DETOUR! PROCEED WITH CAUTION!",
    "hero": "BE A HERO! REPORT VIOLATORS!",
    "dust": "PARDON OUR DUST!",
    "taxes": "YOUR TAX DOLLARS AT WORK!",
    "limits": "OBEY POSTED LIMITS!",
    "uphill": "YIELD TO UPHILL TRAFFIC!",
    "fire": "IN CASE OF FIRE, USE STAIRS",
}
SIGN_OFF = "Keep it clean!"  # how it signed its messages

# Its eight expressions, as it showed them on New Mombasa's screens.
FACES = {
    "happy": "[^_^]", "neutral": "[o_o]", "bored": "[-_-]", "confused": "[o_O]",
    "sad": "[;_;]", "angry": "[>_<]", "sleeping": "[-.-]", "off": "[   ]",
}

BANNER = (
    r" _  _  _   _  ___     _      ___   ___   _  __",
    r"| || || | | || _ \   /_\    / __| / _ \ | |/ /",
    r"| __ || |_| ||   /  / _ \  | (_ || (_) || ' <",
    r"|_||_| \___/ |_|_\ /_/ \_\  \___| \___/ |_|\_\ ",
)
MOTTO = "Feet first into hell."  # the ODSTs'


@dataclass(frozen=True)
class Mood:
    expression: str  # one of FACES
    notice: str  # one of NOTICES' texts, or ""
    summary: str  # what's going on, in plain words
    colour: str

    @property
    def face(self) -> str:
        return FACES[self.expression]

    def line(self) -> str:
        """`[^_^] SUPERINTENDENT · KEEP IT CLEAN! · 2 of 2 servers up, 14 players on`"""
        return " · ".join(bit for bit in (f"{self.face} SUPERINTENDENT", self.notice, self.summary) if bit)


def _players(row: dict[str, Any]) -> int:
    count = str(row.get("players") or "").split("/")[0].strip()
    return int(count) if count.isdecimal() else 0


def _named(rows: list[dict[str, Any]], what: str) -> str:
    first = rows[0]
    more = f" (and {len(rows) - 1} more)" if len(rows) > 1 else ""
    return f"{first['name']} {what or str(first.get('status', '')).lower()}{more}"


def mood(rows: Iterable[dict[str, Any]], *, answering: bool = True) -> Mood:
    """The Superintendent's face for the servers as the dashboard's rows have them: the worst thing going on wins."""
    rows = list(rows)
    if not answering:
        return Mood("off", "", "the supervising window isn't answering", "gray")
    if not rows:
        return Mood("confused", NOTICES["dust"], "no servers yet: each one is a folder in SERVERS/", "yellow")

    def phase(*names: str) -> list[dict[str, Any]]:
        return [r for r in rows if r.get("phase") in names]

    faulted = [r for r in rows if r.get("phase") == "faulted" or str(r.get("status", "")).startswith("CONFIG ERROR")]
    if faulted:
        return Mood("angry", NOTICES["emergency"], _named(faulted, "is FAULTED"), "red")
    if crashed := phase("backoff"):
        return Mood("sad", NOTICES["dust"], _named(crashed, "crashed; trying again"), "red")
    if busy := phase("countdown", "stopping", "restarting"):
        return Mood("confused", NOTICES["calm"], _named(busy, ""), "yellow")
    if starting := phase("launch", "preparing", "booting"):
        return Mood("neutral", NOTICES["detour"], _named(starting, "is starting"), "cyan")
    up = phase("running", "external")
    if not up:
        return Mood("sleeping", "", f"every server is stopped ({len(rows)})", "gray")
    players = sum(_players(r) for r in up)
    summary = (f"{len(up)} of {len(rows)} server{'s' if len(rows) != 1 else ''} up, "
               f"{players} player{'s' if players != 1 else ''} on")
    if players:
        return Mood("happy", NOTICES["clean"], summary, "green")
    return Mood("bored", NOTICES["taxes"], summary, "green")


def who_is_who() -> list[tuple[str, str, str]]:
    """(tag, name, what it does) for each part, for the help."""
    return [(p.tag, p.name, p.does) for p in PERSONAS]
