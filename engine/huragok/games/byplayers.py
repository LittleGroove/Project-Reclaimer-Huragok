"""Playlists by player count: which playlist a server plays for how many players are on (plans/playlists-by-players.md).

A server folder's playlists-by-players.toml, beside its dedicated.toml, holds a [[server]] block for each server set up
(a folder can hold several). The server program never reads it. Huragok does (servers/byplayers.py): when the count
moves into another playlist's range and stays there for `wait_seconds`, and still is at the end of the game being
played, it writes that playlist into the server's own [[server]] block of dedicated.toml, and the server takes it from
its next vote (auto_reload). Back down to a smaller playlist only once the count is `margin` players below where the
bigger one starts, so a count at the line doesn't swap it back and forth. The window's G -> P sets it up. It's kept out of settings.toml on purpose: settings.toml's [[server]] blocks refuse keys
they don't know, so an older Huragok put back couldn't start; an older Huragok ignores this file.

    [[server]]
    name = "LG Test Server"
    on = true
    default = "playlists/slayer.playlist.json"    # played while it's off
    wait_seconds = 30                             # how long a new count must last before the playlist changes
    margin = 2                                    # back down only this many players below the bigger one's start

    [[server.step]]
    playlist = "playlists/small.playlist.json"
    up_to = 10                                    # the most players it's for

    [[server.step]]
    playlist = "playlists/big.playlist.json"      # the last has no up_to: every count above the one before

Nothing caps up_to (256 is fine): a playlist the server's max_players never reaches is a warning (unreachable), never
a refusal.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.files import write_atomic

FILE = "playlists-by-players.toml"
WAIT_SECONDS = 30
WAIT_MOST = 3600
MARGIN = 2  # the host's call, 2 Nov 2026: with a cap of 10, up at 11 and back down at 9
MARGIN_MOST = 50
MIN_STEPS = 2  # one playlist for every count is just using it (G -> Playlists -> U)

NOTES = """\
# Playlists by player count (Huragok): which playlist each server here plays for how many players are on.
# Set it up in Huragok's window: G, the server, then P. The server program doesn't read this file. Huragok does, and
# when the count moves into another playlist's range and stays there for wait_seconds (and, during a game, until
# that game ends), it writes that playlist into the server's block in dedicated.toml; the server plays it from its
# next vote, without a restart. For the first 10 minutes after a server starts, it waits: players are coming back.
#
#   on            true: Huragok picks the playlist by players; false: the server plays `default`
#   default       the playlist it plays while this is off
#   wait_seconds  how long a new player count must last before the playlist changes
#   margin        back down to a smaller playlist only this many players below where the bigger one starts
#   [[server.step]]  each playlist, from the fewest players up, with up_to: the most players it's for. The last has
#                    no up_to: it plays for every count above the one before.
#
# Huragok writes this file whole as it's changed in the window: comments added here aren't kept.
"""


class ByPlayersError(ValueError):
    """The file can't be read, or a block in it can't be used."""


@dataclass
class Step:
    playlist: str  # as dedicated.toml names it: "playlists/big.playlist.json"
    up_to: int | None = None  # the most players it's for; None: no cap, so it's the last


@dataclass
class Setup:
    """One server's playlists by player count."""

    name: str  # the server, as its dedicated.toml names it
    on: bool = False
    default: str = ""  # the playlist played while it's off ("": none chosen yet)
    wait_seconds: int = WAIT_SECONDS
    steps: list[Step] = field(default_factory=list)
    margin: int = MARGIN

    def ranges(self) -> list[tuple[int, int | None]]:
        """Each step's (fewest, most) players; most is None for the last, which has no cap."""
        found, low = [], 0
        for step in self.steps:
            found.append((low, step.up_to))
            low = (step.up_to + 1) if step.up_to is not None else low
        return found

    def step_for(self, count: int) -> int:
        """The step (from 0) that plays for `count` players: the first whose cap holds them, else the last."""
        for number, step in enumerate(self.steps):
            if step.up_to is None or count <= step.up_to:
                return number
        return len(self.steps) - 1

    def step_keeping(self, count: int, playing: int | None) -> int:
        """The step for `count`, while the server plays step `playing` (None: none of them). Up at once; down only
        once the count is `margin` below where `playing` starts, so one player at the line doesn't swap the playlist
        back and forth: with a cap of 10 and a margin of 2, up at 11 and back down at 9 (the host's call, 2 Nov 2026)."""
        target = self.step_for(count)
        if playing is None or not 0 <= playing < len(self.steps) or target >= playing:
            return target
        return target if count <= self.ranges()[playing][0] - self.margin else playing

    def playing_step(self, folder: Path | None, playlist: Any, count: int) -> int | None:
        """Which step the server plays now, given the playlist its dedicated.toml names (None: none of them), for
        step_keeping. A playlist in two steps counts as the one at or just above `count`'s: that's the one a margin
        holds it on."""
        steps = [n for n, step in enumerate(self.steps) if same_playlist(folder, playlist, step.playlist)]
        if not steps:
            return None
        target = self.step_for(count)
        return min((n for n in steps if n >= target), default=max(steps))

    def keeping(self, folder: Path | None, playlist: Any, count: int) -> int:
        """The step for `count` while the server plays `playlist`: step_keeping, from what it plays."""
        return self.step_keeping(count, self.playing_step(folder, playlist, count))

    def problems(self) -> list[str]:
        """What stops this setup being used at all (a warning is unreachable's, not here)."""
        found = []
        if len(self.steps) < MIN_STEPS:
            found.append(f"it needs {MIN_STEPS} playlists or more (one for every count is just using it)")
        before: int | None = None
        for number, step in enumerate(self.steps, 1):
            if not step.playlist:
                found.append(f"playlist {number} names no file")
            last = number == len(self.steps)
            if step.up_to is None and not last:
                found.append(f"playlist {number} has no cap, but isn't the last: the ones after it would never play")
            elif step.up_to is not None and last:
                found.append(f"the last playlist ({number}) has a cap, {step.up_to}: the last has none, so every "
                             "count above the one before plays it")
            elif step.up_to is not None:
                if step.up_to < 0:
                    found.append(f"playlist {number}'s cap, {step.up_to}, is below 0")
                elif before is not None and step.up_to <= before:
                    found.append(f"playlist {number}'s cap, {step.up_to}, isn't above the one before it, {before}")
                before = step.up_to
        if not 0 <= self.wait_seconds <= WAIT_MOST:
            found.append(f"wait_seconds is {self.wait_seconds}: 0 to {WAIT_MOST}")
        if not 0 <= self.margin <= MARGIN_MOST:
            found.append(f"margin is {self.margin}: 0 to {MARGIN_MOST}")
        return found

    def unreachable(self, max_players: int | None) -> list[int]:
        """The steps (from 0) that never play while the server lets in at most `max_players`: each starts above it."""
        if max_players is None:
            return []
        return [number for number, (low, _) in enumerate(self.ranges()) if low > max_players]


def describe_range(low: int, high: int | None) -> str:
    """'up to 10 players', '11 to 50 players', '51 players and up'."""
    if high is None:
        return f"{low} player{'s' if low != 1 else ''} and up" if low else "any number of players"
    if low == 0:
        return f"up to {high} player{'s' if high != 1 else ''}"
    if low == high:
        return f"{low} player{'s' if low != 1 else ''}"
    return f"{low} to {high} players"


def unreachable_text(setup: Setup, max_players: int | None) -> list[str]:
    """A warning for each playlist max_players keeps from ever playing."""
    ranges = setup.ranges()
    return [f"playlist {number + 1} ({playlist_name(setup.steps[number].playlist)}) never plays while max_players is "
            f"{max_players}: it starts at {ranges[number][0]} players" for number in setup.unreachable(max_players)]


def warnings(setup: Setup, folder: Path, *, max_players: int | None, auto_reload: bool = True,
             kind: str = "playlist") -> list[str]:
    """Everything the window warns of about a setup: what stops it, playlists max_players never reaches, files gone
    or unreadable, and what keeps Huragok from changing the playlist (auto_reload off, not playing a playlist)."""
    from .halo3 import PlaylistError, load_playlist

    found = [f"it can't be used: {problem}" for problem in setup.problems()]
    found += unreachable_text(setup, max_players)
    named = [(f"playlist {number}", step.playlist) for number, step in enumerate(setup.steps, 1)]
    for label, reference in named + ([("the default", setup.default)] if setup.default else []):
        try:
            load_playlist(folder / reference)
        except PlaylistError as error:
            found.append(f"{label} ({playlist_name(reference)}) can't be played: {error}")
    if setup.on and not auto_reload:
        found.append("its dedicated.toml has auto_reload off, so the server wouldn't take a change until a reload: "
                     "Huragok leaves the playlist as it is until it's on (D -> Folders and files)")
    if setup.on and kind != "playlist":
        found.append("it plays something that isn't a playlist now (W), so nothing changes until it plays one")
    return found


def playlist_name(reference: str) -> str:
    """'playlists/big.playlist.json' -> 'big', as the window names a playlist file."""
    return Path(str(reference).replace("\\", "/")).name.removesuffix(".playlist.json")


def same_playlist(folder: Path, one: Any, other: Any) -> bool:
    """Whether two references (dedicated.toml's `playlist`, a step's) name the same file, however written."""
    if not isinstance(one, str) or not isinstance(other, str) or not one or not other:
        return False

    def where(reference: str) -> str:
        path = Path(reference.replace("\\", "/"))
        return os.path.normcase(os.path.normpath(str(path if path.is_absolute() else folder / path)))

    return where(one) == where(other)


# The file ------------------------------------------------------------------------------------------------------------

def path_of(folder: Path) -> Path:
    return folder / FILE


def read(folder: Path) -> dict[str, Setup]:
    """Every server's setup in this folder's file, by lowercase name; {} when there's no file. ByPlayersError when the
    file, or a block in it, can't be read."""
    path = path_of(folder)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        raise ByPlayersError(f"{FILE} can't be read: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise ByPlayersError(f"{FILE} isn't valid TOML: {error}") from None
    blocks = data.get("server", [])
    if not isinstance(blocks, list) or not all(isinstance(block, dict) for block in blocks):
        raise ByPlayersError(f"{FILE}: each server is a [[server]] block")
    found: dict[str, Setup] = {}
    for number, block in enumerate(blocks, 1):
        setup = _setup(block, number)
        found[setup.name.lower()] = setup
    return found


def _setup(block: dict[str, Any], number: int) -> Setup:
    name = block.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ByPlayersError(f"{FILE} [[server]] {number}: name the server as its dedicated.toml does")
    where = f"{FILE} [[server]] {name}"

    def typed(key: str, kind: type, default: Any) -> Any:
        value = block.get(key, default)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            wanted = "true or false" if kind is bool else "a whole number" if kind is int else "text"
            raise ByPlayersError(f"{where}: {key} must be {wanted}")
        return value

    steps = []
    raw = block.get("step", [])
    if not isinstance(raw, list) or not all(isinstance(step, dict) for step in raw):
        raise ByPlayersError(f"{where}: each playlist is a [[server.step]] block")
    for count, step in enumerate(raw, 1):
        playlist, up_to = step.get("playlist"), step.get("up_to")
        if not isinstance(playlist, str):
            raise ByPlayersError(f"{where}, playlist {count}: playlist must be the file, as dedicated.toml names it")
        if up_to is not None and (not isinstance(up_to, int) or isinstance(up_to, bool)):
            raise ByPlayersError(f"{where}, playlist {count}: up_to must be a whole number")
        steps.append(Step(playlist, up_to))
    return Setup(name.strip(), typed("on", bool, False), typed("default", str, ""),
                 typed("wait_seconds", int, WAIT_SECONDS), steps, typed("margin", int, MARGIN))


def setup_of(folder: Path, server: str) -> Setup | None:
    """One server's setup, or None when it has none. ByPlayersError when the file can't be read."""
    return read(folder).get(server.lower())


def _string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a TOML basic string


def render(setups: list[Setup]) -> str:
    lines = [NOTES]
    for setup in setups:
        lines += ["[[server]]", f"name = {_string(setup.name)}", f"on = {'true' if setup.on else 'false'}",
                  f"default = {_string(setup.default)}", f"wait_seconds = {setup.wait_seconds}",
                  f"margin = {setup.margin}", ""]
        for step in setup.steps:
            lines += ["[[server.step]]", f"playlist = {_string(step.playlist)}"]
            lines += [f"up_to = {step.up_to}"] if step.up_to is not None else []
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def save(folder: Path, setup: Setup) -> None:
    """This server's setup written, the other servers' in the file kept as they are."""
    setups = read(folder)
    setups[setup.name.lower()] = setup
    write_atomic(path_of(folder), render(list(setups.values())))


def remove(folder: Path, server: str) -> bool:
    """This server's setup taken out (the file too, once it holds none); False when it had none."""
    setups = read(folder)
    if setups.pop(server.lower(), None) is None:
        return False
    if setups:
        write_atomic(path_of(folder), render(list(setups.values())))
    else:
        path_of(folder).unlink(missing_ok=True)
    return True


def rename(folder: Path, old: str, new: str) -> bool:
    """A renamed server keeps its setup under its new name; False when it had none."""
    setups = read(folder)
    setup = setups.pop(old.lower(), None)
    if setup is None:
        return False
    setup.name = new
    setups[new.lower()] = setup
    write_atomic(path_of(folder), render(list(setups.values())))
    return True


__all__ = ["ByPlayersError", "FILE", "MIN_STEPS", "Setup", "Step", "WAIT_MOST", "WAIT_SECONDS", "describe_range",
           "playlist_name", "read", "remove", "rename", "same_playlist", "save", "setup_of", "unreachable_text",
           "warnings"]
