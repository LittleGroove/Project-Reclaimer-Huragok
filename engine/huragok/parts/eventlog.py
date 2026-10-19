"""eventlog: every event as one line of JSON in <folder>/events-YYYY-MM-DD.jsonl (UTC).

The quickest way to see what the server actually sends, e.g. while adding a command:
turn it on, play a game, and read the file. Player IPs are in join events, so
keep the folder private.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import IO

from ..events import Event, matches
from ..part import Part, on


class EventLog(Part):
    name = "eventlog"
    description = "Writes every event as a line of JSON, one file per day, to see what the server sends"
    defaults = {"folder": "logs/events", "include": ["*"], "exclude": []}
    _file: IO[str] | None = None

    async def setup(self) -> None:
        self.folder = Path(self.ctx.home) / str(self.config["folder"])
        self.folder.mkdir(parents=True, exist_ok=True)
        self._day = ""
        self._file: IO[str] | None = None

    def _wanted(self, name: str) -> bool:
        return (any(matches(p, name) for p in self.config["include"])
                and not any(matches(p, name) for p in self.config["exclude"]))

    @on("*")
    async def write(self, event: Event) -> None:
        if not self._wanted(event.name):
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(event.time))
        if day != self._day or self._file is None:
            if self._file:
                self._file.close()
            self._file = (self.folder / f"events-{day}.jsonl").open("a", encoding="utf-8", buffering=1)
            self._day = day
        line = {"time": round(event.time, 3), "server": event.server, "event": event.name, **event.data}
        self._file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    async def teardown(self) -> None:
        if self._file:
            self._file.close()
