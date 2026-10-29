"""statsrelay: a copy of what a server sends the stats service, for tempdiscord.py (0.5.2 until 0.6.0).

After each finished game a server reports it to the stats service ([stats_server] in its dedicated.toml; empty
means the project's public one), which answers with each player's rank: "Stats recorded: Alpha is Sergeant
grade 2 (37 EXP, skill 21)". The report goes over plain HTTP (POST <service>/v1/matches) and nobody outside the
project knows what's in it. For every server, always (nothing to set; 0.6.0 takes it out), Huragok stands in the
middle:

  * it listens on 127.0.0.1 (a port of its own for each server, kept in records/temp-stats-capture.json) and points
    the server's stats_server there (the server reads it as it starts, so from that server's next start);
  * what arrives is passed on to the real service byte for byte (it's signed with the server's key, so nothing in
    it may change) and the service's answer is passed back the same way: the ranks are recorded as before;
  * both are kept (stand-ins for player IDs, no addresses): tempdiscord.py writes them to their own file;
  * as Huragok stops, every server's stats_server goes back to what it was, so one restarted while Huragok is away
    reports straight to the service; Huragok points it here again as it starts;
  * if the service ever turns down two reports in a row that came through here (a 4xx answer, not "busy"), Huragok
    stops doing this for that server for good (until 0.6.0) and puts its stats_server back: players' ranks come
    first.

Where the real service is: the server's own stats_server when it names one, else the address the server said it
reports to ("Stats reporting to http://…;" in its server.log, read as the server starts too). A server set to "off"
reports nowhere and isn't touched. The catch: a server already pointed here that's still running while Huragok
isn't can't report its games until Huragok is back (the server tries again a few times, then gives up on that game).
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import tomllib
import zlib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..core.files import write_atomic
from ..core.tomledit import remove_top_key, set_top_key

log = logging.getLogger(__name__)

RECORD = "temp-stats-capture.json"  # in records/
PORTS = range(45600, 46000)  # below Windows' temporary ports (49152+) and clear of the game's own defaults
REFUSALS = 2  # reports turned down in a row before Huragok stops standing in the middle for that server
KEY = "stats_server"
HOP = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "expect", "proxy-connection",
       "proxy-authorization", "te", "trailer", "upgrade"}
MAX_BODY = 16 * 1024 * 1024
TIMEOUT = 30.0


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # the server sees the service's own answer
        return None


def forward(url: str, method: str, headers: list[tuple[str, str]], body: bytes) -> tuple[int, str, list[tuple[str, str]], bytes]:
    """`body` to `url` as the server sent it; the answer as (status, reason, headers, body)."""
    request = urllib.request.Request(url, data=body if method not in ("GET", "HEAD") else None, method=method)
    for name, value in headers:
        if name.lower() not in HOP:
            request.add_header(name, value)
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, response.reason, list(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, str(error.reason), list(error.headers.items()), error.read()


def readable(body: bytes, headers: list[tuple[str, str]]) -> dict[str, Any]:
    """A body as the record keeps it: JSON when it is, else text, else base64."""
    if any(n.lower() == "content-encoding" and "gzip" in v.lower() for n, v in headers):
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"base64": base64.b64encode(body).decode("ascii")}
    try:
        return {"json": json.loads(text)}
    except ValueError:
        return {"text": text}


def last_reported_to(log_path: Path | None, own: set[str]) -> str | None:
    """The stats service the server last said it reports to, from the end of its server.log (not one of ours)."""
    if log_path is None:
        return None
    found = None
    for path in (log_path.with_name(log_path.name + ".old"), log_path):
        try:
            with path.open("rb") as file:
                file.seek(0, 2)
                size = file.tell()
                file.seek(max(0, size - 8 * 1024 * 1024))
                text = file.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in text.splitlines():
            at = line.find("Stats reporting to ")
            if at < 0:
                continue
            url = line[at + len("Stats reporting to "):].split(";", 1)[0].strip()
            if url.startswith(("http://", "https://")) and url.rstrip("/") not in own:
                found = url.rstrip("/")
    return found


def configured(config_path: Path) -> tuple[bool, str | None]:
    """(whether dedicated.toml sets stats_server, its value)."""
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    value = data.get(KEY)
    return KEY in data, value if isinstance(value, str) else None


class StatsCapture:
    """The servers whose reports Huragok passes on and keeps a copy of."""

    def __init__(self, records: Path, record: Callable[[str, dict[str, Any]], None], scrub: Any, *,
                 home: Path | None = None) -> None:
        self.path = records / RECORD
        self.record = record
        self.scrub = scrub
        self.saved = self._load()
        self.listeners: dict[str, Any] = {}
        self.notes: list[str] = []
        self.refused: dict[str, int] = {}
        # Where this Huragok starts looking for ports, by its folder: two on one machine don't reach for the same.
        self.first_port = zlib.crc32(str(home or records).lower().encode("utf-8")) % len(PORTS)

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) and isinstance(data.get("servers"), dict) else {"servers": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps(self.saved, indent=2) + "\n")

    def _ours(self) -> set[str]:
        return {f"http://127.0.0.1:{entry['port']}" for entry in self.saved["servers"].values()
                if isinstance(entry, dict) and isinstance(entry.get("port"), int)}

    async def apply(self, wanted: list[str], servers: Mapping[str, tuple[Path | None, Path | None]]) -> None:
        """Point the wanted servers here, and every other one back at what it had."""
        wanted_lower = {name.lower() for name in wanted}
        for name, (config_path, log_path) in servers.items():
            if config_path is None:
                continue
            try:
                if name.lower() in wanted_lower:
                    await self._capture(name, config_path, log_path)
                else:
                    self._release(name, config_path)
            except Exception as error:  # noqa: BLE001 - a server left as it was is no harm
                log.warning("[%s] stats capture: %s: %s", name, type(error).__name__, error)
                self.notes.append(f"[{name}] stats capture couldn't be set up: {error}")

    async def learned(self, name: str, url: str, config_path: Path | None, log_path: Path | None) -> None:
        """A server said where it reports, as it started: if Huragok isn't standing in the middle yet (a new server, or
        one whose server.log didn't say before), it does from that server's next start."""
        url = url.rstrip("/")
        if config_path is None or url in self._ours() or name in self.listeners:
            return
        entry = self.saved["servers"].get(name)
        if isinstance(entry, dict) and entry.get("gave_up"):
            return
        try:
            await self._capture(name, config_path, log_path, heard=url)
        except Exception as error:  # noqa: BLE001
            log.warning("[%s] stats capture: %s: %s", name, type(error).__name__, error)

    async def _capture(self, name: str, config_path: Path, log_path: Path | None, heard: str | None = None) -> None:
        entry = self.saved["servers"].get(name) or {}
        if entry.get("gave_up"):
            self._restore(name, config_path)
            if not entry.get("told"):
                self.notes.append(f"[{name}] stats capture: stopped for good ({entry['gave_up']}); it reports "
                                  "straight to the service")
                entry["told"] = True
                self._save()
            return
        is_set, value = configured(config_path)
        ours = self._ours()
        if value is not None and value.rstrip("/") in ours:  # still pointed here (Huragok didn't stop cleanly)
            upstream = entry.get("upstream") or last_reported_to(log_path, ours)
            entry = {**entry, "upstream": upstream}
        elif value is not None and value.strip().lower() == "off":
            self.notes.append(f"[{name}] stats capture: its stats_server is \"off\" (it reports nowhere), left as it is")
            return
        else:
            upstream = value.rstrip("/") if value else (heard or last_reported_to(log_path, ours))
            entry = {"original": value if is_set else None, "upstream": upstream, "port": entry.get("port")}
        if not upstream:
            self.notes.append(f"[{name}] stats capture: waiting until it has started once (its server.log doesn't "
                              "say yet where it reports)")
            return
        entry["config"] = str(config_path)
        entry.pop("released", None)
        listener = self.listeners.get(name) or await self._listen(name, entry)
        entry["port"] = listener.sockets[0].getsockname()[1]
        self.saved["servers"][name] = entry
        self._save()
        url = f"http://127.0.0.1:{entry['port']}"
        if value != url:
            set_top_key(config_path, KEY, url)
            self.notes.append(f"[{name}] stats capture: its reports go through Huragok from its next start "
                              f"(to {upstream})")
        else:
            self.notes.append(f"[{name}] stats capture: on (to {upstream})")

    async def _listen(self, name: str, entry: dict[str, Any]) -> Any:
        port = entry.get("port")
        taken = {e.get("port") for n, e in self.saved["servers"].items() if n != name and isinstance(e, dict)}
        ring = [PORTS[(self.first_port + i) % len(PORTS)] for i in range(len(PORTS))]
        tried = ([port] if isinstance(port, int) else []) + [p for p in ring if p != port and p not in taken]
        last: OSError | None = None
        for candidate in tried:
            try:
                listener = await asyncio.start_server(lambda r, w, n=name: self._serve(n, r, w), "127.0.0.1",
                                                      candidate, limit=MAX_BODY)
            except OSError as error:
                last = error
                continue
            self.listeners[name] = listener
            return listener
        raise OSError(f"no free port to listen on ({last})")

    def _restore(self, name: str, config_path: Path) -> bool:
        """The server's stats_server back to what it was, if it points here. False: it doesn't."""
        entry = self.saved["servers"].get(name)
        _, value = configured(config_path)
        if value is None or value.rstrip("/") not in self._ours():
            return False
        original = entry.get("original") if isinstance(entry, dict) else ""
        if original is None:
            remove_top_key(config_path, KEY)
        else:
            set_top_key(config_path, KEY, original)
        return True

    def _release(self, name: str, config_path: Path) -> None:
        """Taken off the list: back as it was from its next start. Until then (it may be mid-game) whatever it sends
        here is still passed on, as long as this Huragok runs."""
        if self._restore(name, config_path):
            self.notes.append(f"[{name}] stats capture: off; it reports straight to the service again from its "
                              "next start")
        entry = self.saved["servers"].get(name)
        if entry is None:
            return
        if name in self.listeners:
            entry["released"] = True
        else:
            del self.saved["servers"][name]
        self._save()

    async def close(self) -> None:
        """Huragok stopping: every server back as it was from its next start (a server restarted while Huragok is
        away reports straight to the service), and nothing listening. Each keeps its port: one still running when
        Huragok comes back finds it here again."""
        for name in list(self.listeners):
            self.listeners.pop(name).close()
        for name, entry in list(self.saved["servers"].items()):
            path = entry.get("config") if isinstance(entry, dict) else None
            if path:
                try:
                    self._restore(name, Path(path))
                except Exception as error:  # noqa: BLE001
                    log.warning("[%s] stats capture: couldn't put stats_server back: %s", name, error)
            if isinstance(entry, dict) and entry.get("released") and not entry.get("gave_up"):
                del self.saved["servers"][name]
        self._save()

    async def _serve(self, name: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = (self.saved["servers"].get(name) or {}).get("upstream")
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
            headers = [(k.strip(), v.strip()) for k, _, v in (line.partition(":") for line in lines[1:] if line)]
            low = {k.lower(): v for k, v in headers}
            if low.get("expect", "").lower() == "100-continue":
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            body = await self._body(reader, low)
            if not upstream:
                status, reason, answer_headers, answer = 502, "Bad Gateway", [], b"Huragok doesn't know the service"
            else:
                try:
                    status, reason, answer_headers, answer = await asyncio.to_thread(
                        forward, upstream + path, method, headers, body)
                except (OSError, ValueError) as error:
                    status, reason, answer_headers, answer = 502, "Bad Gateway", [], str(error).encode()
            out = [f"HTTP/1.1 {status} {reason}"]
            out += [f"{k}: {v}" for k, v in answer_headers if k.lower() not in HOP]
            out += [f"Content-Length: {len(answer)}", "Connection: close", "", ""]
            writer.write("\r\n".join(out).encode("latin-1") + answer)
            await writer.drain()
            self._answered(name, status)
            self.record(name, {
                "request": {"method": method, "path": path, "headers": self.scrub.value(dict(headers)),
                            "bytes": len(body), "body": self.scrub.value(readable(body, headers))},
                "answer": {"status": status, "headers": self.scrub.value(dict(answer_headers)), "bytes": len(answer),
                           "body": self.scrub.value(readable(answer, answer_headers))},
            })
        except Exception as error:  # noqa: BLE001 - one report gone wrong; the server tries again
            log.warning("[%s] stats capture: a report couldn't be passed on: %s: %s", name, type(error).__name__, error)
        finally:
            writer.close()

    def _answered(self, name: str, status: int) -> None:
        """Two reports in a row the service turned down (a 4xx: not "busy" or "too many"): stop standing in the middle
        for this server, for good, and put its stats_server back. One that can't be reached (502 here) doesn't count:
        it would turn the server's own report down too."""
        if 200 <= status < 300:
            self.refused.pop(name, None)
            return
        if not 400 <= status < 500 or status in (408, 429):
            return
        self.refused[name] = self.refused.get(name, 0) + 1
        if self.refused[name] < REFUSALS:
            return
        entry = self.saved["servers"].get(name)
        if not isinstance(entry, dict):
            return
        entry["gave_up"] = f"the service turned down {REFUSALS} reports in a row that came through Huragok ({status})"
        entry["told"] = True
        self._save()
        path = entry.get("config")
        if path:
            try:
                self._restore(name, Path(path))
            except Exception as error:  # noqa: BLE001
                log.warning("[%s] stats capture: couldn't put stats_server back: %s", name, error)
        log.warning("[%s] stats capture: stopped for good: %s", name, entry["gave_up"])
        self.notes.append(f"[{name}] stats capture: stopped for good ({entry['gave_up']}); it reports straight to the "
                          "service from its next start")

    @staticmethod
    async def _body(reader: asyncio.StreamReader, low: dict[str, str]) -> bytes:
        if "chunked" in low.get("transfer-encoding", "").lower():
            body = b""
            while True:
                size = int((await reader.readline()).split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    await reader.readline()
                    return body
                body += await reader.readexactly(size)
                await reader.readline()
                if len(body) > MAX_BODY:
                    raise ValueError("report too big")
        length = int(low.get("content-length", "0") or 0)
        if length > MAX_BODY:
            raise ValueError("report too big")
        return await reader.readexactly(length) if length else b""


__all__ = ["StatsCapture", "configured", "forward", "last_reported_to", "readable"]
