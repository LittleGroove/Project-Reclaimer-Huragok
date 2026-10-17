"""The server program's own commands, run for a server folder: `dedicated init`, `check`,
`content`, `playlist`, `mods`, `reload`...

Windows runs project-reclaimer.exe itself; Linux runs it through wine, or through
whatever [process] command names before the word `dedicated`. The program is looked
for in the server's folder, then at the root (one copy for every server), then where
[setup] program points. Every call is a short process; the servers keep running.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

EXECUTABLE = "project-reclaimer.exe"
SLOW = 900.0  # seconds for check and mods, which may download Workshop items
QUICK = 120.0


class DedicatedError(Exception):
    pass


@dataclass
class ToolResult:
    ok: bool
    code: int
    text: str
    command: list[str] = field(default_factory=list)

    @property
    def lines(self) -> list[str]:
        return [line.rstrip() for line in self.text.splitlines() if line.strip()]

    @property
    def problem(self) -> str:
        """What went wrong, in one line, for a result that isn't ok."""
        lines = self.lines
        errors = [line for line in lines if line.lower().startswith("error")]
        return (errors or lines or [f"exit code {self.code}"])[0].strip()


@dataclass
class ContentItem:
    name: str
    reference: str  # what map = / game = accept: the name, or a file stem
    kind: str  # map, map variant, game type, game variant, mod map
    note: str = ""


@dataclass
class PlaylistInfo:
    file: str
    name: str = ""
    games: int = 0
    vote_seconds: int = 0
    played_by: str = ""
    problem: str = ""


def find_executable(folder: Path, name: str = EXECUTABLE) -> Path | None:
    """The server program in a folder: the configured name, or the release's own name for it."""
    if (folder / name).is_file():
        return folder / name
    found = sorted(folder.glob("project-reclaimer-dedicated*.exe"))
    return found[-1] if found else None


def find_program(home: Path, folders: list[Path], name: str = EXECUTABLE,
                 hint: str | os.PathLike[str] | None = None) -> Path | None:
    """One copy of the program to make and check folders with: the root's, the hinted one, or any server's."""
    if hint:
        path = Path(hint)
        if not path.is_absolute():
            path = home / path
        if path.is_file():
            return path
        if path.is_dir() and (found := find_executable(path, name)):
            return found
    if found := find_executable(home, name):
        return found
    for folder in folders:
        if found := find_executable(folder, name):
            return found
    return None


def program_prefix(executable: Path, template: list[str] | None = None) -> list[str]:
    """How the program is run: [exe] on Windows, [wine, exe] on Linux, or the template's words before `dedicated`."""
    if template and "dedicated" in template:
        words = template[: template.index("dedicated")]
        return [w.replace("{executable}", str(executable)) for w in words] or [str(executable)]
    return [str(executable)] if os.name == "nt" else ["wine", str(executable)]


def tool_for(settings: Settings) -> Dedicated:
    """The runner for this root, or a DedicatedError saying where the program should be."""
    options = settings.backend_options if settings.backend == "process" else {}
    name = str(options.get("executable") or EXECUTABLE)
    template = [str(w) for w in options.get("command") or []]
    program = find_program(settings.home, settings.server_folders, name, settings.setup.program)
    if program is None:
        raise DedicatedError(
            f"the server program ({name}) isn't in any server folder or at {settings.home}: download the dedicated "
            "server release from https://github.com/ProjectReclaimer/project-reclaimer-releases/releases/latest, "
            f"rename it {name} and put it at the root, or set [setup] program in {settings.path.name}")
    return Dedicated(program, template=template, config_name=settings.config_name)


class Dedicated:
    def __init__(self, executable: Path, *, template: list[str] | None = None,
                 config_name: str = "dedicated.toml") -> None:
        self.executable = Path(executable)
        self.prefix = program_prefix(self.executable, template)
        self.config_name = config_name

    def command(self, *words: str, config: str | None = None) -> list[str]:
        extra = ["--config", config] if config else []
        return [*self.prefix, "dedicated", *extra, *words]

    def run_sync(self, folder: Path, *words: str, timeout: float = QUICK, config: str | None = None) -> ToolResult:
        command = self.command(*words, config=config)
        env = dict(os.environ, RECLAIMER_DEDICATED_AUTO_UPDATE="false")
        try:
            done = subprocess.run(command, cwd=str(folder), env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                                  encoding="utf-8", errors="replace", check=False)
        except FileNotFoundError:
            return ToolResult(False, 127, f"couldn't run {command[0]}: not found", command)
        except subprocess.TimeoutExpired:
            return ToolResult(False, 124, f"{' '.join(words)} took longer than {timeout:.0f} s and was stopped",
                              command)
        except OSError as error:
            return ToolResult(False, 126, f"couldn't run {command[0]}: {error}", command)
        return ToolResult(done.returncode == 0, done.returncode, done.stdout or "", command)

    async def run(self, folder: Path, *words: str, timeout: float = QUICK, config: str | None = None) -> ToolResult:
        return await asyncio.to_thread(self.run_sync, folder, *words, timeout=timeout, config=config)

    async def stream(self, folder: Path, *words: str, on_line: Any, cancel: asyncio.Event | None = None,
                     timeout: float = SLOW, config: str | None = None) -> ToolResult:
        """Run a command and hand each output line to on_line as it arrives; set `cancel` to end it early.

        A Workshop download or a long check shows its progress this way, and the window
        stays in control: Esc cancels (the process is ended; nothing waits on it).
        """
        command = self.command(*words, config=config)
        env = dict(os.environ, RECLAIMER_DEDICATED_AUTO_UPDATE="false")
        loop = asyncio.get_running_loop()
        try:
            process = subprocess.Popen(command, cwd=str(folder), env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
                                       errors="replace", bufsize=1)
        except FileNotFoundError:
            return ToolResult(False, 127, f"couldn't run {command[0]}: not found", command)
        except OSError as error:
            return ToolResult(False, 126, f"couldn't run {command[0]}: {error}", command)
        collected: list[str] = []

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                collected.append(line)
                loop.call_soon_threadsafe(on_line, line.rstrip("\r\n"))
            process.stdout.close()

        reader = asyncio.ensure_future(asyncio.to_thread(pump))
        waiter = asyncio.ensure_future(asyncio.to_thread(process.wait))
        cancel_wait = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
        timer = asyncio.ensure_future(asyncio.sleep(timeout))
        pending = {waiter, timer} | ({cancel_wait} if cancel_wait else set())
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        cancelled = cancel_wait in done
        timed_out = timer in done and waiter not in done
        if cancelled or timed_out:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(waiter), 10)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
        for task in (timer, cancel_wait):
            if task is not None and not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(reader, 5)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        code = process.returncode if process.returncode is not None else -1
        text = "".join(collected)
        if cancelled:
            return ToolResult(False, 130, text + "\n(cancelled)", command)
        if timed_out:
            return ToolResult(False, 124, text + f"\n({' '.join(words)} took longer than {timeout:.0f} s and was stopped)",
                              command)
        return ToolResult(code == 0, code, text, command)

    # The subcommands ------------------------------------------------------------------------

    def init(self, folder: Path, *, mcc: str | None = None, maps: list[str] | None = None) -> ToolResult:
        """Make a server folder (its parent must exist). mcc: copy Halo 3's files from there ("" for Steam's default)."""
        folder.parent.mkdir(parents=True, exist_ok=True)
        words = ["init", str(folder)]
        if mcc is not None:
            words.append("--from")
            if mcc:
                words.append(mcc)
        if maps:
            words.extend(["--maps", ",".join(maps)])
        return self.run_sync(folder.parent, *words, timeout=SLOW)

    def check(self, folder: Path) -> ToolResult:
        return self.run_sync(folder, "check", timeout=SLOW, config=self.config_name)

    def content(self, folder: Path, words: list[str] | None = None) -> ToolResult:
        return self.run_sync(folder, "content", *(words or []), config=self.config_name)

    def playlists(self, folder: Path) -> ToolResult:
        return self.run_sync(folder, "playlist", "list", config=self.config_name)

    def playlist_show(self, folder: Path, file: str) -> ToolResult:
        return self.run_sync(folder, "playlist", "show", file, config=self.config_name)

    def playlist_new(self, folder: Path, name: str, maps: list[str], games: list[str], *,
                     vote_seconds: int | None = None, replace: bool = False) -> ToolResult:
        words = ["playlist", "new", name, "--maps", ",".join(maps), "--games", ",".join(games)]
        if vote_seconds:
            words.extend(["--vote-seconds", str(vote_seconds)])
        if replace:
            words.append("--replace")
        return self.run_sync(folder, *words, config=self.config_name)

    def mods(self, folder: Path) -> ToolResult:
        return self.run_sync(folder, "mods", timeout=SLOW, config=self.config_name)

    def reload(self, folder: Path) -> ToolResult:
        return self.run_sync(folder, "reload", config=self.config_name)

    def status(self, folder: Path) -> ToolResult:
        return self.run_sync(folder, "status", config=self.config_name)

    def bans(self, folder: Path) -> ToolResult:
        return self.run_sync(folder, "bans", config=self.config_name)


# Reading what the program prints --------------------------------------------------------------

SECTION = re.compile(r"^(Maps|Map variants|Game types|Game variants|Mod maps|Workshop maps)\b(.*)$")
ITEM = re.compile(r'^\s+"(?P<name>[^"]+)"(?:\s+or\s+"(?P<stem>[^"]+)")?(?:\s+"(?P<display>[^"]+)")?'
                  r'(?:\s+(?P<note>[^"\s].*?))?\s*$')
PLAYLIST_LINE = re.compile(r'^(?P<file>\S+\.playlist\.json)\s+·\s+"(?P<name>[^"]*)"\s+·\s+(?P<games>\d+) games?'
                           r'(?:\s+·\s+vote\s+(?P<vote>\d+) s)?(?:\s+·\s+played by\s+(?P<by>.+?))?\s*$')
WORKSHOP_REF = re.compile(r"workshop:\d+/[\w.-]+")


def parse_content(text: str) -> list[ContentItem]:
    """`dedicated content`'s listing as items. Lines that aren't items (hints, counts) are left out."""
    items: list[ContentItem] = []
    kind = ""
    for line in text.splitlines():
        header = SECTION.match(line)
        if header:
            kind = {"Maps": "map", "Map variants": "map variant", "Game types": "game type",
                    "Game variants": "game variant"}.get(header.group(1), header.group(1).lower().rstrip("s"))
            continue
        match = ITEM.match(line)
        if not match or not kind:
            continue
        name = match.group("display") or match.group("name")
        note = (match.group("note") or "").strip()
        items.append(ContentItem(name, match.group("stem") or match.group("name"), kind, note))
    return items


def parse_playlists(text: str) -> list[PlaylistInfo]:
    """`dedicated playlist list`: one entry per playlist file."""
    found: list[PlaylistInfo] = []
    for line in text.splitlines():
        match = PLAYLIST_LINE.match(line.strip())
        if match:
            found.append(PlaylistInfo(match.group("file"), match.group("name"), int(match.group("games")),
                                      int(match.group("vote") or 0), (match.group("by") or "").strip()))
        elif found and line.startswith("  ") and line.strip():
            found[-1].problem = (found[-1].problem + " " + line.strip()).strip()
    return found


def workshop_references(text: str) -> list[str]:
    """Every workshop:<id>/<map> reference `dedicated mods` printed, in order, once each."""
    seen: list[str] = []
    for ref in WORKSHOP_REF.findall(text):
        if ref not in seen:
            seen.append(ref)
    return seen


def summarize(result: ToolResult, limit: int = 40) -> list[str]:
    """The lines worth showing from a result: all of them up to a limit, then a count."""
    lines = result.lines
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], f"... {len(lines) - limit} more lines"]


def describe(value: Any) -> str:
    return str(value)
