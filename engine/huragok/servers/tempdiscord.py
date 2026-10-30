"""tempdiscord: a temporary record (0.5.2 until 0.6.0) of what the Discord bot and players' stats will be built on.

0.6.0 brings a Discord bot and players' stats (kill streaks, medals, wins, leaderboards), built on what the servers
send; some of that nobody has seen yet: a real `kill` event, what its `report` byte means, how the end of a game looks
in the players' totals, what a server tells the stats service after each game. This writes it down while people play,
into logs/temp-discord/temp-discord-YYYY-MM-DD.jsonl (UTC days, kept KEEP_DAYS), one JSON object a line, each with
its `kind`:

  start         Huragok starting: its version, each server's program version and what it is playing
  event         every event: the console's (kills, phases, joins, votes, chat…), the server.log lines Huragok knows
                (stats reports and each player's rank, countdowns, team changes…) and Huragok's own (restarts, moderation)
  players       the console's `players` reply (each player's team, score, kills, deaths, assists, betrayals, suicides,
                engine number, service tag): at every phase change, every POLL_SECONDS while a game is on, and when a
                kill names an engine number not seen yet
  status        the console's `status` reply, at every phase change
  stats_report  a server's report to the stats service went by: the file it's in, the service's answer
  note          what the stats capture did (statsrelay.py)

and every report a server sends the stats service, with the service's answer, into its own
logs/temp-discord/stats-reports/stats-reports-YYYY-MM-DD.jsonl (kept until 0.6.0 clears it out): statsrelay.py
passes each one on as it was sent and keeps a copy.

It's made to be sent to whoever builds 0.6.0, so nobody's private: no IP addresses or device fingerprints, player IDs
as stand-ins ("P-" and 10 letters: one player, one stand-in, worked out with a key kept in records/ that never leaves
this machine), chat lines as their length (a command keeps its name: "!stats …"). Names stay: the stats lines name
players. Always on, with nothing to set: everyone running 0.5.2 helps build 0.6.0. Gone in 0.6.0.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import secrets
import time
from pathlib import Path
from typing import IO, Any

from ..core.background import background
from ..core.events import Event
from ..core.part import Part, every, on
from ..lore import SUPERINTENDENT
from .statsrelay import StatsCapture

FOLDER = "logs/temp-discord"
REPORTS = "stats-reports"  # in FOLDER: the stats reports, kept
KEEP_DAYS = 30  # the daily record files; the reports are kept
KEY_FILE = "temp-discord.key"  # in records/: makes the stand-ins; stays on this machine
POLL_SECONDS = 15.0  # `players` while a game is on (Huragok's own look is every 30 s)
UNKNOWN_POLL_SECONDS = 5.0  # at most this often for a kill naming an engine number not seen yet
GAME_ON = {"playing", "round_over", "between_rounds"}
DROP_KEYS = {"ip", "ips", "device", "devices", "fingerprint", "fingerprints"}
SAID_KEYS = {"evidence", "said", "line", "lines", "words", "word"}  # what someone typed, in moderation events
HIDDEN = "(hidden)"
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?::\d{1,5})?(?![\w.])")
IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:/\d{1,3})?(?![\w:])")
HEX64 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


class Scrubber:
    """What leaves the machine in these files: no addresses or devices, player IDs as stand-ins."""

    def __init__(self, key: bytes) -> None:
        self.key = key

    def stand_in(self, player_id: str) -> str:
        return "P-" + hashlib.sha256(self.key + player_id.lower().encode("ascii")).hexdigest()[:10]

    def text(self, text: str) -> str:
        text = HEX64.sub(lambda m: self.stand_in(m.group()), text)
        return IPV6.sub(_ipv6, IPV4.sub(HIDDEN, text))

    def value(self, value: Any, *, said: bool = False) -> Any:
        """`value` without address and device fields, with stand-ins for IDs; `said`: also without what anyone typed."""
        if isinstance(value, dict):
            kept = {}
            for key, item in value.items():
                name = str(key).lower()
                if name in DROP_KEYS or (said and name in SAID_KEYS):
                    continue
                kept[self.text(str(key))] = self.value(item, said=said)  # the service keys answers by player ID
            return kept
        if isinstance(value, (list, tuple)):
            return [self.value(item, said=said) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def _ipv6(match: re.Match[str]) -> str:
    """An IPv6 address hidden; what only looks like one (a clock, 03:07:07) left as it is."""
    text = match.group()
    for end in (len(text), len(text) - 1):
        try:
            ipaddress.ip_network(text[:end], strict=False)
        except ValueError:
            continue
        return HIDDEN + text[end:]
    return text


def said(text: Any) -> str:
    """A chat line as these files keep it: a command's name and how much followed, else only how long it was."""
    text = str(text or "")
    words = text.split()
    if words and words[0][:1] in ("!", "/"):
        rest = len(text) - len(words[0])
        return words[0][:24].lower() + (f" (+{rest} characters)" if rest > 1 else "")
    return f"({len(text)} characters)"


def key_for(records: Path) -> bytes:
    """This machine's key for the stand-ins, made the first time."""
    path = records / KEY_FILE
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        if len(key) >= 16:
            return key
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    records.mkdir(parents=True, exist_ok=True)
    path.write_text(key.hex() + "\n", encoding="ascii")
    return key


class TempDiscord(Part):
    persona = SUPERINTENDENT
    name = "tempdiscord"
    description = "Temporary (until 0.6.0): records what the Discord bot and players' stats will need, in logs/temp-discord"
    always_on = True  # nothing to set: no section in settings.toml, nothing to switch
    _file: IO[str] | None = None
    capture: StatsCapture | None = None

    async def setup(self) -> None:
        self.days = KEEP_DAYS
        self.folder = Path(self.ctx.home) / FOLDER
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / REPORTS).mkdir(exist_ok=True)
        self.scrub = Scrubber(key_for(Path(self.ctx.home) / "records"))
        self._day = ""
        self._file = None
        self._last_poll: dict[str, float] = {}
        self._last_unknown: dict[str, float] = {}
        self._engines: dict[str, set[str]] = {}
        self._polling: dict[str, asyncio.Lock] = {}
        self.prune()
        from .. import __version__  # here: the package imports the parts

        self.write("start", None, huragok=__version__, servers=[self._describe(name) for name in self.ctx.servers])
        self.capture = StatsCapture(Path(self.ctx.home) / "records", self.record_report, self.scrub,
                                    home=Path(self.ctx.home))
        await self.capture.apply(list(self.ctx.servers), {name: self._paths(name) for name in self.ctx.servers})
        self._notes()

    def _paths(self, name: str) -> tuple[Path | None, Path | None]:
        return self.ctx.config_path(name), self.ctx.server_option(name, "log_path")

    def _notes(self) -> None:
        if self.capture is None:
            return
        for line in self.capture.notes:
            self.log.info("%s", line)
            self.write("note", None, text=line)
        self.capture.notes.clear()

    def _describe(self, name: str) -> dict[str, Any]:
        state = self.ctx.servers[name]
        return {"server": name, "program": state.version, "plays": self.ctx.plays_kind(name), "phase": state.phase,
                "map": state.map, "mode": state.mode, "max_players": state.max_players}

    # ── the file ──────────────────────────────────────────────────────────────────────────────────────────────────

    def write(self, kind: str, server: str | None, *, at: float | None = None, **data: Any) -> None:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self._day or self._file is None:
            if self._file:
                self._file.close()
            self._file = (self.folder / f"temp-discord-{day}.jsonl").open("a", encoding="utf-8", buffering=1)
            self._day = day
        line = {"at": round(now, 3), "kind": kind, "server": server, **({"time": at} if at is not None else {}), **data}
        self._file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    def prune(self) -> None:
        """Daily record files older than KEEP_DAYS go (the stats reports stay)."""
        cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - self.days * 86400))
        for path in self.folder.glob("temp-discord-*.jsonl"):
            day = path.stem.removeprefix("temp-discord-")
            if re.fullmatch(r"\d{4}-\d\d-\d\d", day) and day < cutoff:
                try:
                    path.unlink()
                except OSError as error:
                    self.log.debug("couldn't remove %s: %s", path.name, error)

    @every(hours=6)
    async def tidy(self) -> None:
        self.prune()

    def record_report(self, server: str, data: dict[str, Any]) -> None:
        """A report and the service's answer: the whole of it in stats-reports/, a line in the day's record."""
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        name = f"stats-reports-{day}.jsonl"
        line = {"at": round(now, 3), "server": server, **data}
        with (self.folder / REPORTS / name).open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        answer = data.get("answer") or {}
        self.write("stats_report", server, file=f"{REPORTS}/{name}", status=answer.get("status"),
                   bytes=(data.get("request") or {}).get("bytes"))
        self._notes()

    # ── what happens ──────────────────────────────────────────────────────────────────────────────────────────────

    @on("*")
    async def event(self, event: Event) -> None:
        if event.name.startswith(f"{self.name}."):
            return
        data = dict(event.data)
        if event.name == "rcon.chat" and "text" in data:
            data["text"] = said(data["text"])
        quiet = event.name.split(".", 1)[0] in ("moderation", "automod", "commands")
        self.write("event", event.server, at=round(event.time, 3), event=event.name,
                   data=self.scrub.value(data, said=quiet))
        if event.server is None or event.server not in self.ctx.servers:
            return
        if event.name == "log.stats_reporting" and self.capture is not None:
            # Where a server reports, as it starts: one Huragok hasn't pointed here yet (new, or its server.log
            # didn't say before) is from now on, from its next start.
            await self.capture.learned(event.server, str(event.get("url", "")), *self._paths(event.server))
            self._notes()
        if event.name == "rcon.phase":
            background(self.poll(event.server, f"phase {event.get('phase')}", status=True), what="tempdiscord poll")
        elif event.name == "rcon.kill":
            known = self._engines.get(event.server, set())
            named = {str(v) for v in (event.get("killer"), event.get("victim")) if v is not None}
            last = self._last_unknown.get(event.server, float("-inf"))
            if named - known and time.monotonic() - last >= UNKNOWN_POLL_SECONDS:
                self._last_unknown[event.server] = time.monotonic()
                background(self.poll(event.server, "a kill named an engine number not seen yet"), what="tempdiscord poll")

    @every(seconds=5)
    async def game_on(self) -> None:
        """While a game is on, the players' totals every POLL_SECONDS: the last look before a game ends is its result."""
        now = time.monotonic()
        for name, state in self.ctx.servers.items():
            busy = self._polling.get(name)
            if (state.connected and state.phase in GAME_ON and not (busy and busy.locked())
                    and now - self._last_poll.get(name, float("-inf")) >= POLL_SECONDS):
                background(self.poll(name, "game on"), what="tempdiscord poll")

    async def poll(self, server: str, why: str, *, status: bool = False) -> None:
        lock = self._polling.setdefault(server, asyncio.Lock())
        async with lock:
            await self._ask(server, why, status=status)

    async def _ask(self, server: str, why: str, *, status: bool) -> None:
        self._last_poll[server] = time.monotonic()
        try:
            client = self.ctx.rcon(server)
            if not client.connected:
                return
            state = self.ctx.servers[server]
            for command in (("status", "players") if status else ("players",)):
                reply = await client.command(command)
                data = reply.data if isinstance(reply.data, dict) else {"text": reply.text}
                if command == "players":
                    engines = {str(e.get("engine_id")) for e in [*(data.get("players") or []),
                                                                 *(data.get("guest_players") or [])]
                               if isinstance(e, dict) and e.get("engine_id") is not None}
                    self._engines[server] = engines
                self.write(command, server, why=why, phase=state.phase, map=state.map, mode=state.mode, ok=reply.ok,
                           data=self.scrub.value(data))
        except Exception as error:  # noqa: BLE001 - a look that failed is only a gap in this record
            self.log.debug("[%s] couldn't ask for %s: %s: %s", server, why, type(error).__name__, error)

    async def teardown(self) -> None:
        if self.capture is not None:
            await self.capture.close()
        if self._file:
            self._file.close()
            self._file = None


__all__ = ["Scrubber", "TempDiscord", "key_for", "said"]
