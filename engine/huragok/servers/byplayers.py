"""byplayers: plays each server's playlist for how many players are on, as its playlists-by-players.toml says
(games/byplayers.py; the window's G -> P sets it up).

Every few seconds it counts each running server's players (everyone connected, as the dashboard counts them) and finds
the playlist for that count. When that isn't the one the server's dedicated.toml names, and the count has stayed in
the new range for the setup's wait_seconds, it writes the playlist into the server's own [[server]] block, as the
window's G -> Playlists -> U does. The server re-reads its file by itself (auto_reload) and plays it from its next
vote: no restart, nobody dropped. Three things keep it from swapping back and forth (the host's calls, 2 Nov 2026):
- during a game, the change waits for the game to end, and is made only if the count still wants it then: the count
  has to hold until the next vote;
- back down to a smaller playlist only once the count is the setup's margin below where the bigger one starts (with a
  cap of 10 and a margin of 2: up at 11, back down at 9);
- for the first AFTER_START_SECONDS of a server's run nothing changes: its players are still coming back, and the
  count starts at 0.

Before writing, the server's own program checks the playlist (`dedicated check`, as every save in the window is
checked): a playlist it refuses, in its file, would hold up every other change to that file and its next start. One
refused isn't written; it's logged once, and tried again only after REFUSED_AGAIN, or once the count wants another.

It only acts while the setup is on and the server plays a playlist (switched to a campaign or one map in W, it's left
alone), while Huragok can read the server's console (the count isn't known otherwise), and while auto_reload isn't
off (the server wouldn't take the change until a reload; the window says so). Each change is an event,
byplayers.changed: the window's events show it and Discord's games channel posts it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.part import Part, every
from ..core.tomledit import read_toml
from ..games.byplayers import ByPlayersError, Setup, describe_range, playlist_name, read, same_playlist
from ..games.halo3 import PlaylistError, load_playlist
from ..games.plays import plays_kind
from ..lore import SUPERINTENDENT
from ..program.plays import write_plays

TICK_SECONDS = 5
REFUSED_AGAIN = 600.0  # a playlist its program refused is checked again after this long
AFTER_START_SECONDS = 600.0  # a run this young keeps its playlist: players are coming back (the host, 2 Nov 2026)
GAME_ON = {"playing", "round_over", "between_rounds"}  # the console's phases while a game is being played


@dataclass
class _Read:
    """A file as last read, by its modified time and size."""

    mtime: tuple[int, int] | None
    data: Any
    problem: str = ""


@dataclass
class _Wanted:
    """A playlist the count wants, and since when it has."""

    playlist: str
    step: int
    since: float


class ByPlayers(Part):
    persona = SUPERINTENDENT
    name = "byplayers"
    description = "Playlists by player count: plays each server's playlist for how many are on (G -> P)"
    always_on = True

    def check_config(self) -> None:
        self.clock = time.monotonic
        self.files: dict[Path, _Read] = {}
        self.wanted: dict[str, _Wanted] = {}
        self.refused: dict[str, tuple[str, float]] = {}  # server -> (the playlist its program refused, when)
        self.said: dict[str, str] = {}  # server -> what it last logged about not acting, so it's said once
        # server -> its change under way: run apart from the tick, as the server program's check can take longer than a
        # handler may (Wine, a virus scan, several servers at once), and one at a time (audit 2026-11, 8.4)
        self.changing: dict[str, asyncio.Task[bool]] = {}
        self.was_on: set[str] = set()  # servers it was on for, last look: turned off since, the default goes back

    async def teardown(self) -> None:
        for task in self.changing.values():
            task.cancel()
        await asyncio.gather(*self.changing.values(), return_exceptions=True)
        self.changing.clear()

    async def settled(self) -> None:
        """Every change under way, done (the tests' way of waiting for one)."""
        await asyncio.gather(*list(self.changing.values()), return_exceptions=True)

    # Reading the files, again only once they've changed -----------------------------------------------------------

    def _file(self, path: Path, reader: Any) -> _Read:
        try:
            stat = path.stat()
            mtime: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            mtime = None
        except OSError as error:
            return self.files.get(path) or _Read(None, None, str(error))
        known = self.files.get(path)
        if known is not None and known.mtime == mtime:
            return known
        try:
            read_now = _Read(mtime, reader(path) if mtime is not None else None)
        except (OSError, ValueError) as error:  # ByPlayersError and TOML's own among them
            read_now = _Read(mtime, None, str(error))
        self.files[path] = read_now
        return read_now

    def setup_for(self, server: str) -> Setup | None:
        """The server's playlists by player count, or None when it has none (or its file can't be read: logged)."""
        folder = self.ctx.server_option(server, "folder")
        if not isinstance(folder, Path):
            return None
        found = self._file(folder / "playlists-by-players.toml", lambda path: read(path.parent))
        if found.problem:
            self._say_once(server, f"its playlists-by-players.toml can't be used, so its playlist stays as it is: "
                                   f"{found.problem}", warn=True)
            return None
        return (found.data or {}).get(server.lower())

    def _block(self, server: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """(its [[server]] block, the whole file) from the server's dedicated.toml, or None."""
        config = self.ctx.config_path(server)
        if config is None:
            return None
        found = self._file(config, read_toml)
        if found.data is None:
            return None
        blocks = found.data.get("server", [])
        block = next((b for b in blocks if isinstance(b, dict) and str(b.get("name", "")).lower() == server.lower()),
                     None) if isinstance(blocks, list) else None
        return (block, found.data) if block is not None else None

    def _say_once(self, server: str, text: str, *, warn: bool = False) -> None:
        if self.said.get(server) != text:
            self.said[server] = text
            (self.log.warning if warn else self.log.info)("[%s] %s", server, text)

    # Every few seconds ---------------------------------------------------------------------------------------------

    @every(seconds=TICK_SECONDS)
    async def tick(self) -> None:
        for server in [s for s in self.wanted if s not in self.ctx.servers]:
            del self.wanted[server]
        for server, state in list(self.ctx.servers.items()):
            try:
                await self.look(server, state)
            except Exception:  # noqa: BLE001 - one server's trouble mustn't stop the others'
                self.log.exception("[%s] couldn't pick its playlist by players", server)

    async def look(self, server: str, state: Any) -> None:
        if server in self.changing:
            return  # its change is under way
        setup = self.setup_for(server)
        problems = setup.problems() if setup is not None and setup.on else []
        if setup is None or not setup.on or problems:
            self.wanted.pop(server, None)
            if problems:
                self._say_once(server, f"its playlists by player count can't be used: {problems[0]}", warn=True)
            if server in self.was_on and setup is not None and not setup.on and setup.default:
                # Turned off by hand (on = false in the file): off means the default, as the window's O does it
                task = asyncio.ensure_future(self.put_default(server, setup))
                self.changing[server] = task
                task.add_done_callback(lambda done, server=server: self._changed(server, done))
            self.was_on.discard(server)
            return
        self.was_on.add(server)
        if not state.connected or state.last_rcon_ok is None or not state.players_known:
            self.wanted.pop(server, None)  # the count isn't known (just connected: none read yet): wait till it is
            return
        found = self._block(server)
        if found is None:
            return
        block, data = found
        if plays_kind(block) != "playlist":
            self.wanted.pop(server, None)
            self._say_once(server, "plays something that isn't a playlist, so its playlists by player count wait")
            return
        if data.get("auto_reload") is False:
            self.wanted.pop(server, None)
            self._say_once(server, "its dedicated.toml has auto_reload off, so its playlist isn't changed by player "
                                   "count: the server wouldn't take it until a reload", warn=True)
            return
        started = self.ctx.run_started(server)
        if started is not None and time.time() - started < AFTER_START_SECONDS:
            self.wanted.pop(server, None)  # just started: the count starts at 0 while its players come back
            self._say_once(server, "just started: its playlist by players waits till it's been up 10 minutes")
            return
        self.said.pop(server, None)
        count = len(state.players)
        folder = self.ctx.server_option(server, "folder")
        step = setup.keeping(folder, block.get("playlist"), count)
        playlist = setup.steps[step].playlist
        if same_playlist(folder, block.get("playlist"), playlist):
            self.wanted.pop(server, None)  # back where it is (within the margin, or the count came back): nothing waits
            return
        now = self.clock()
        wanted = self.wanted.get(server)
        if wanted is None or wanted.playlist != playlist:
            wanted = self.wanted[server] = _Wanted(playlist, step, now)
        if now - wanted.since < setup.wait_seconds:
            return
        if state.phase in GAME_ON:
            return  # until the game ends: made then if the count still wants it
        refused = self.refused.get(server)
        if refused is not None and refused[0] == playlist and now - refused[1] < REFUSED_AGAIN:
            return
        task = asyncio.ensure_future(self.change(server, setup, step, count, str(block.get("playlist") or "")))
        self.changing[server] = task
        task.add_done_callback(lambda done, server=server: self._changed(server, done))

    def _changed(self, server: str, task: asyncio.Task[bool]) -> None:
        if self.changing.get(server) is task:
            del self.changing[server]
        if not task.cancelled() and task.exception() is not None:
            self.log.error("[%s] couldn't change its playlist by players: %s", server, task.exception(),
                           exc_info=task.exception())

    async def change(self, server: str, setup: Setup, step: int, count: int, was: str) -> bool:
        """The playlist for this many players into the server's dedicated.toml, once its program says it can play it."""
        folder = self.ctx.server_option(server, "folder")
        config = self.ctx.config_path(server)
        playlist = setup.steps[step].playlist
        if not isinstance(folder, Path) or config is None:
            return False
        try:
            name = (await asyncio.to_thread(load_playlist, folder / playlist)).name
        except PlaylistError as error:
            return self._refused(server, playlist, f"it can't be read: {error}")
        check = await asyncio.to_thread(self.ctx.check_plays, server, {"playlist": playlist})
        if not check.ok:
            return self._refused(server, playlist, "; ".join(p.text for p in check.problems[:3]))
        if not check.asked:
            self.log.info("[%s] %s wasn't checked by the server program (%s); Huragok's own checks passed", server,
                          playlist, check.unasked)
        # The setup and the server's file read again: turned off (the default put back) or set to something else in
        # the window while the check ran, this pick mustn't go over it (audit 2026-11, 8.9).
        now = self.setup_for(server)
        found = self._block(server)
        if now is None or not now.on or now.problems() or found is None or plays_kind(found[0]) != "playlist" \
                or now.steps[now.keeping(folder, found[0].get("playlist"), count)].playlist != playlist:
            self.wanted.pop(server, None)
            return False
        try:
            await asyncio.to_thread(write_plays, config, server, {"playlist": playlist})
        except (OSError, ValueError) as error:  # TomlEditError is an OSError: the file stays as it was
            # Said once and tried again after a while, as a refusal is: not a check and a warning every 5 s (8.5)
            return self._refused(server, playlist, f"it couldn't be written into {config.name}: {error}")
        self.files.pop(config, None)  # read again: a write within the clock's tick can keep the modified time
        self.wanted.pop(server, None)
        self.refused.pop(server, None)
        low, high = setup.ranges()[step]
        was_name = ""
        if was:
            try:
                was_name = (await asyncio.to_thread(load_playlist, folder / was)).name
            except PlaylistError:
                was_name = playlist_name(was)
        self.log.info("[%s] %s player%s on: plays %s (\"%s\", for %s) from the next vote, was %s", server, count,
                      "" if count == 1 else "s", playlist, name, describe_range(low, high), was or "nothing")
        self.ctx.emit("changed", server, players=count, playlist=playlist, name=name, step=step + 1,
                      steps=len(setup.steps), low=low, high=high, range=describe_range(low, high), was=was,
                      was_name=was_name)
        return True

    async def put_default(self, server: str, setup: Setup) -> bool:
        """The default back, once its program says it can play it, when the setup was turned off by hand. Nothing when
        the server plays something that isn't a playlist, or plays the default already (the window's O put it back)."""
        folder, config = self.ctx.server_option(server, "folder"), self.ctx.config_path(server)
        found = self._block(server)
        if not isinstance(folder, Path) or config is None or found is None or plays_kind(found[0]) != "playlist" \
                or same_playlist(folder, found[0].get("playlist"), setup.default):
            return False
        try:
            await asyncio.to_thread(load_playlist, folder / setup.default)
        except PlaylistError as error:
            return self._refused(server, setup.default, f"it can't be read: {error}")
        check = await asyncio.to_thread(self.ctx.check_plays, server, {"playlist": setup.default})
        if not check.ok:
            return self._refused(server, setup.default, "; ".join(p.text for p in check.problems[:3]))
        try:
            await asyncio.to_thread(write_plays, config, server, {"playlist": setup.default})
        except (OSError, ValueError) as error:
            return self._refused(server, setup.default, f"it couldn't be written into {config.name}: {error}")
        self.files.pop(config, None)
        self.log.info("[%s] playlists by player count turned off: plays its default, %s, from the next vote", server,
                      setup.default)
        return True

    def _refused(self, server: str, playlist: str, why: str) -> bool:
        known = self.refused.get(server)
        if known is None or known[0] != playlist:
            self.log.warning("[%s] not switched to %s, which its player count wants: %s. It's tried again in %d "
                             "minutes; G -> P shows its playlists", server, playlist, why, REFUSED_AGAIN // 60)
            self.ctx.emit("refused", server, playlist=playlist, why=why)
        self.refused[server] = (playlist, self.clock())
        return False


__all__ = ["AFTER_START_SECONDS", "ByPlayers", "REFUSED_AGAIN", "TICK_SECONDS"]
