"""The window's G -> P: a server's playlists by player count (games/byplayers.py, plans/playlists-by-players.md).

One page shows what it's doing now: on, off or not set up, the players on and the playlist that's for them, the
default (played while it's off), each playlist with its range, and warnings (a playlist max_players never reaches, a
file gone, auto_reload off). From there: set it up (again, as a new list), turn it on or off, pick the default, and
the wait before a change. Setting up asks for a playlist, then the most players it's for, then the next, each cap
above the last, until a playlist is given no cap: that one is last. Every playlist is checked by the server's own
program as it's picked, as G -> Playlists -> U checks one.

U and W picking a playlist while it's on ask first (`_by_players_before_use`): otherwise Huragok would switch it back
within seconds.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...core.tomledit import read_toml
from ...games import byplayers as bp
from ...games.halo3 import PlaylistError, load_playlist, playlist_files
from ...games.plays import plays_kind, plays_of
from ...program.plays import applies_when, write_plays
from ...program.settings import effective
from .base import Screens


def live_count(row: dict[str, Any]) -> int | None:
    """Players on now, from the dashboard's row ("12/16"); None when the server isn't up."""
    players = str(row.get("players") or "")
    head = players.split("/", 1)[0].strip()
    return int(head) if head.isdecimal() else None


def server_block(server: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """(its [[server]] block, its whole dedicated.toml); empty ones when the file can't be read."""
    try:
        data = read_toml(server.config_path)
    except (OSError, ValueError):
        return {}, {}
    blocks = data.get("server", [])
    block = next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == server.name.lower()),
                 {}) if isinstance(blocks, list) else {}
    return block, data


def max_players_of(data: dict[str, Any], server: Any) -> int | None:
    value = effective(data, "max_players", server.name) if data else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def playlist_label(folder: Path, reference: str) -> str:
    """'big ("Big Team Battle", 30 games)', or the file and why it can't be read."""
    try:
        playlist = load_playlist(folder / reference)
    except PlaylistError as error:
        return f"{bp.playlist_name(reference)} (can't be read: {error})"
    return f'{bp.playlist_name(reference)} ("{playlist.name}", {len(playlist.games)} games)'


def status_lines(setup: bp.Setup | None, server: Any, block: dict[str, Any],
                 count: int | None) -> list[tuple[str, str, str | None]]:
    """The page's head: (label, text, colour), the status card's too."""
    playing = block.get("playlist") if isinstance(block.get("playlist"), str) else ""
    on_now = f"plays {plays_of(block) if block else server.plays}"
    if count is not None:
        on_now = f"{count} player{'' if count == 1 else 's'} on · {on_now}"
    if setup is None:
        return [("By players", "not set up: it plays one playlist, as W or G -> Playlists -> U sets it", "gray"),
                ("Now", on_now, None)]
    lines = [("By players", "ON: Huragok picks the playlist by how many players are on" if setup.on
              else "off: it plays the default", "green" if setup.on else "yellow")]
    if setup.on and count is not None and not setup.problems():
        target = setup.step_for(count)
        number = setup.keeping(server.folder, playing, count)
        wanted = setup.steps[number].playlist
        if bp.same_playlist(server.folder, playing, wanted):
            on_now += f", playlist {number + 1}: for {bp.describe_range(*setup.ranges()[number])}"
            if number != target:
                on_now += f" (kept within the margin of {setup.margin})"
        else:
            on_now += (f"; playlist {number + 1} ({bp.playlist_name(wanted)}) is for {count} once the count has "
                       f"stayed {setup.wait_seconds} s, and the game being played is over")
    lines.append(("Now", on_now, None))
    lines.append(("Default", playlist_label(server.folder, setup.default) + ": played while it's off"
                  if setup.default else "(none picked: it keeps what it plays when turned off)", "gray"))
    lines.append(("Waits", f"{setup.wait_seconds} s: how long a new count must last before the playlist changes; during "
                           "a game, till it ends. Nothing in a run's first 10 minutes", "gray"))
    lines.append(("Margin", f"{setup.margin}: back down to a smaller playlist only {setup.margin} player"
                            f"{'' if setup.margin == 1 else 's'} below where the bigger one starts", "gray"))
    return lines


def step_rows(setup: bp.Setup, server: Any, playing: str) -> list[tuple[str, ...]]:
    rows = []
    for (low, high), step in zip(setup.ranges(), setup.steps):
        now = "   ◄ in use" if bp.same_playlist(server.folder, playing, step.playlist) else ""
        rows.append((bp.describe_range(low, high), playlist_label(server.folder, step.playlist) + now))
    return rows


class ByPlayersScreens(Screens):
    """Mixed into Menu: uses its terminal, controller, prompts and layout helpers."""

    async def _by_players(self, settings: Any, tool: Any, server: Any, row: dict[str, Any]) -> None:
        """G -> P: what it's doing now, and everything that changes it."""
        while True:
            fresh = next((r for r in self.controller.rows() if r.get("name") == server.name), row)
            try:
                setup = await asyncio.to_thread(bp.setup_of, server.folder, server.name)
            except bp.ByPlayersError as error:
                self.heading(server.name, "What it plays", "By player count")
                self.note(f"{bp.FILE} can't be read: {error}", "red")
                self.note("Fix it by hand (G -> 6 opens its folder) or take the file away; its playlist stays as it "
                          "is meanwhile.", "yellow")
                await self.wait_any_key(10)
                return
            block, data = await asyncio.to_thread(server_block, server)
            count = live_count(fresh)
            self.heading(server.name, "What it plays", "By player count",
                         note="a playlist for each range of player counts, picked by Huragok as players come and go; "
                              "a change plays from the next vote, without a restart")
            for label, text, colour in status_lines(setup, server, block, count):
                self.terminal.print_parts([(f"  {label:<12}", "gray"), (text, colour or "white")])
            options: list[tuple[str, str] | tuple[str, str, str]] = []
            if setup is not None:
                self.terminal.print()
                self.listing(step_rows(setup, server, str(block.get("playlist") or "")))
                kind = plays_kind(block) if block else server.plays_kind
                for warning in await asyncio.to_thread(
                        bp.warnings, setup, server.folder, max_players=max_players_of(data, server),
                        auto_reload=data.get("auto_reload") is not False, kind=kind):
                    self.note(f"! {warning}", "yellow")
                options.append(("S", "Set it up again: a new list"))
                options.append(("O", "Turn it off: play the default" if setup.on else "Turn it on"))
            else:
                options.append(("S", "Set it up: a playlist for each range of player counts"))
            options.append(("D", "Default playlist", "played while it's off" if setup is not None else "uses it now"))
            if setup is not None:
                options.append(("W", "Wait before a change", f"{setup.wait_seconds} s"))
                options.append(("M", "Margin before going back down", f"{setup.margin} players"))
            choice = await self.choose("Which?", options)
            if choice is None:
                return
            if choice == "S":
                await self._by_players_set_up(tool, server, setup, block, data, count)
            elif choice == "O" and setup is not None:
                await (self._by_players_off(tool, server, setup) if setup.on
                       else self._by_players_on(tool, server, setup, block, count))
            elif choice == "D":
                await self._by_players_default(tool, server, setup)
            elif choice == "W" and setup is not None:
                seconds = await self._number_line("  Seconds a new player count must last before the playlist "
                                                  "changes (0 changes it at once)", str(setup.wait_seconds),
                                                  (0, bp.WAIT_MOST), "a number of seconds")
                if seconds is not None:
                    setup.wait_seconds = int(seconds)
                    self._by_players_save(server, setup, f"Waits {seconds} s before a change.")
            elif choice == "M" and setup is not None:
                margin = await self._number_line("  Players below where a bigger playlist starts before going back "
                                                 "down to the smaller one (0: at once)", str(setup.margin),
                                                 (0, bp.MARGIN_MOST), "a number of players")
                if margin is not None:
                    setup.margin = int(margin)
                    self._by_players_save(server, setup, f"Goes back down {margin} below where the bigger playlist "
                                                         "starts.")

    # Setting it up --------------------------------------------------------------------------------------------------

    @staticmethod
    def _playlist_rows(server: Any) -> tuple[list[Path], list[tuple[str, ...]]]:
        files = playlist_files(server.folder)
        rows: list[tuple[str, ...]] = []
        for path in files:
            try:
                playlist = load_playlist(path)
                rows.append((path.name.removesuffix(".playlist.json"), f'"{playlist.name}" · {len(playlist.games)} games'))
            except PlaylistError as error:
                rows.append((path.name.removesuffix(".playlist.json"), f"(can't read it: {error})"))
        return files, rows

    async def _pick_one_playlist(self, tool: Any, server: Any, prompt: str) -> str | None:
        """A playlist from playlists/, numbered, checked by the server program: its reference, or None."""
        files, rows = self._playlist_rows(server)
        if not files:
            self.note("No playlists in its playlists/ yet: G -> 2 (Playlists) builds one.", "yellow")
            await self.wait_any_key(5)
            return None
        while True:
            panel = self.listing(rows)
            answer = await self.prompt_line(prompt, panel=panel)
            if answer is None:
                return None
            if not answer.strip().isdecimal() or not 1 <= int(answer) <= len(files):
                self.note(f"Type a number from 1 to {len(files)} (Esc goes back).", "yellow")
                continue
            path = files[int(answer) - 1]
            if tool is None or await self._playable(tool, server, path):
                return f"playlists/{path.name}"

    async def _by_players_set_up(self, tool: Any, server: Any, old: bp.Setup | None, block: dict[str, Any],
                                 data: dict[str, Any], count: int | None) -> None:
        self.heading(server.name, "By player count", "Set up",
                     note="the playlist for the fewest players first, then the most players it's for; then the next "
                          "playlist, with a higher number, and so on. Enter instead of a number makes that playlist "
                          "the last: it plays for every count above the one before")
        steps: list[bp.Step] = []
        while True:
            low = 0 if not steps else (steps[-1].up_to or 0) + 1
            which = f"from {low} players" if steps else "for the fewest players"
            playlist = await self._pick_one_playlist(
                tool, server, f"  Playlist {len(steps) + 1}, {which}: which? (number; Esc stops, nothing saved)")
            if playlist is None:
                self.note("Nothing saved." + (" It stays as it was." if old is not None else ""), "gray")
                return
            how = f"a number above {low - 1}; Enter: no cap, so it's the last" if steps else "a number"
            while True:
                cap = await self.prompt_line(f"  The most players {bp.playlist_name(playlist)} is for ({how})")
                if cap is None:
                    self.note("Nothing saved." + (" It stays as it was." if old is not None else ""), "gray")
                    return
                cap = cap.strip()
                if cap == "":
                    if not steps:
                        self.note(f"The first playlist needs a cap: one playlist for every count is just using it "
                                  f"(G -> Playlists -> U). Give the most players it's for.", "yellow")
                        continue
                    steps.append(bp.Step(playlist))
                    break
                if not cap.isdecimal() or int(cap) < low:
                    self.note(f"{cap!r} isn't a number of {low} or more" + (
                        f" (above the cap before, {low - 1})" if steps else "") + ".", "yellow")
                    continue
                steps.append(bp.Step(playlist, int(cap)))
                break
            if steps[-1].up_to is None:
                break
        default = old.default if old is not None and old.default else (
            block["playlist"] if isinstance(block.get("playlist"), str) else steps[0].playlist)
        setup = bp.Setup(server.name, False, default, old.wait_seconds if old is not None else bp.WAIT_SECONDS, steps,
                         old.margin if old is not None else bp.MARGIN)
        self.heading(server.name, "By player count", "Set up", note="as it would be saved")
        self.listing(step_rows(setup, server, ""))
        self.note(f"Default (played while it's off): {playlist_label(server.folder, default)}; D changes it.")
        for warning in bp.unreachable_text(setup, max_players_of(data, server)):
            self.note(f"! {warning}", "yellow")
        choice = await self.choose("Save it?", [("Y", "Save it and turn it on"), ("S", "Save it, off for now")],
                                   default="Y", back="Esc: don't save it")
        if choice is None:
            self.note("Nothing saved." + (" It stays as it was." if old is not None else ""), "gray")
            return
        if choice == "S":
            self._by_players_save(server, setup, "Saved, off: it plays the default until it's turned on (O).")
            return
        await self._by_players_on(tool, server, setup, block, count)

    # On, off and the default ----------------------------------------------------------------------------------------

    def _by_players_save(self, server: Any, setup: bp.Setup, said: str) -> bool:
        try:
            bp.save(server.folder, setup)
        except (OSError, bp.ByPlayersError) as error:
            self.note(f"Not saved: {error}", "red")
            return False
        self.note(said, "green")
        return True

    def _write_playlist_now(self, server: Any, reference: str) -> bool:
        try:
            write_plays(server.config_path, server.name, {"playlist": reference})
        except OSError as error:  # TomlEditError among them: the file stays as it was
            self.note(f"Couldn't write {server.config_path.name}: {error}", "red")
            return False
        server.plays, server.plays_kind = f"playlist {bp.playlist_name(reference)}", "playlist"
        return True

    async def _by_players_on(self, tool: Any, server: Any, setup: bp.Setup, block: dict[str, Any],
                             count: int | None) -> None:
        """On, with the playlist for the players on now (0 when it's not running) written at once, once checked."""
        if problems := setup.problems():
            self.note(f"Not turned on: {problems[0]}. S sets it up again.", "yellow")
            await self.wait_any_key(6)
            return
        number = setup.step_for(count or 0)
        playlist = setup.steps[number].playlist
        path = server.folder / playlist
        already = bp.same_playlist(server.folder, block.get("playlist"), playlist)
        if not already and tool is not None and not await self._playable(tool, server, path):
            self.note("Not turned on.", "yellow")
            await self.wait_any_key(8)
            return
        setup.on = True
        if not self._by_players_save(server, setup, "On."):
            return
        before = plays_kind(block) if block else server.plays_kind
        if not already and self._write_playlist_now(server, playlist):
            why = (f"{count} player{'' if count == 1 else 's'} on, so it plays" if count is not None
                   else "It isn't running, so it starts on")
            self.note(f"{why} {bp.playlist_name(playlist)} (for {bp.describe_range(*setup.ranges()[number])}): "
                      f"{applies_when(before, 'playlist')}. Huragok changes it as players come and go.", "green")
        await self.wait_any_key(8)

    async def _by_players_off(self, tool: Any, server: Any, setup: bp.Setup) -> None:
        """Off, with the default put back at once (when there is one, and the server plays a playlist)."""
        setup.on = False
        block, _ = server_block(server)
        if setup.default and "playlist" in block and not bp.same_playlist(server.folder, block.get("playlist"),
                                                                           setup.default):
            if tool is not None and not await self._playable(tool, server, server.folder / setup.default):
                self.note("The default can't be played, so it isn't put back: D picks another. Turned off anyway; it "
                          "keeps what it plays now.", "yellow")
                self._by_players_save(server, setup, "Off.")
                await self.wait_any_key(8)
                return
            if self._by_players_save(server, setup, "Off.") and self._write_playlist_now(server, setup.default):
                self.note(f"It plays the default, {bp.playlist_name(setup.default)}, from the next vote.", "green")
        else:
            self._by_players_save(server, setup, "Off: it keeps what it plays now.")
        await self.wait_any_key(6)

    async def _by_players_default(self, tool: Any, server: Any, setup: bp.Setup | None) -> None:
        playlist = await self._pick_one_playlist(tool, server, "  Default playlist: which? (number; Esc leaves it)")
        if playlist is None:
            return
        if setup is None:  # not set up: the default is simply what it plays
            if self._write_playlist_now(server, playlist):
                self.note(f"It plays {bp.playlist_name(playlist)} from the next vote.", "green")
            await self.wait_any_key(5)
            return
        setup.default = playlist
        if setup.on:
            self._by_players_save(server, setup, f"Default: {bp.playlist_name(playlist)}, played once it's off.")
        elif self._by_players_save(server, setup, f"Default: {bp.playlist_name(playlist)}."):
            if self._write_playlist_now(server, playlist):
                self.note("It's off, so it plays the default from the next vote.", "green")
        await self.wait_any_key(5)

    # Asked from U and W --------------------------------------------------------------------------------------------

    async def _by_players_before_use(self, server: Any, reference: str | None) -> bool:
        """Before U or W changes what it plays: while playlists by player count are on, a playlist picked (reference)
        can be the default instead, or turn them off; anything else turns them off. True: go on and write it."""
        try:
            setup = await asyncio.to_thread(bp.setup_of, server.folder, server.name)
        except bp.ByPlayersError:
            return True
        if setup is None or not setup.on:
            return True
        if reference is None:
            setup.on = False
            return self._by_players_save(server, setup, "Playlists by player count turned off: it plays this instead "
                                                        "(G -> P turns them back on).")
        choice = await self.choose(f"{server.name} picks its playlist by player count (G -> P), so Huragok would "
                                   "change this back. Which?", [
                                       ("D", "Make it the default", "played once player count is turned off"),
                                       ("O", "Turn player count off and play it now")], back="Esc: leave it")
        if choice == "D":
            setup.default = reference
            self._by_players_save(server, setup, f"Default: {bp.playlist_name(reference)}. Player count still picks "
                                                 "what it plays.")
            return False
        if choice == "O":
            setup.on = False
            setup.default = reference
            return self._by_players_save(server, setup, "Playlists by player count turned off; this is the default "
                                                        "now.")
        return False


__all__ = ["ByPlayersScreens", "live_count", "max_players_of", "playlist_label", "server_block", "status_lines",
           "step_rows"]
