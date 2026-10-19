"""The server program's own commands, run for a server folder: `dedicated init`, `check`,
`content`, `playlist`, `mods`, `reload`...

Windows runs project-reclaimer.exe itself; Linux runs it through wine, or through
whatever [process] command names before the word `dedicated`. Every call is a short
process; the servers keep running.

Which copy runs: a command for a server folder runs that folder's own copy when it has
one (the copy the server itself runs, which its auto_update keeps current), and the
root's copy only when it has none. Commands that aren't for one server (init,
mode-check) run where [setup] program points, or else the newest copy among the
root's and the servers' (by `--version`): a root copy nothing updates goes stale, and
an old program can't even read a newer dedicated.toml.

A `dedicated mods` or `check` may start SteamCMD, which keeps the output pipe open
after `dedicated` itself has gone. So a timeout or a cancel ends the whole process
tree, and nothing ever waits on the pipe without a limit.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import psutil

if TYPE_CHECKING:
    from .config import ServerConfig, Settings

EXECUTABLE = "project-reclaimer.exe"
SLOW = 900.0  # seconds for check and mods, which may download Workshop items
QUICK = 120.0
READER_GRACE = 5.0  # after the program has ended, how long its output may still take to arrive
CREATE_NEW_PROCESS_GROUP = 0x00000200  # Windows: so Ctrl+Break reaches it (and SteamCMD) and nothing else
RELEASE_NAME = re.compile(r"project-reclaimer-dedicated-?v?(\d+(?:\.\d+)*)", re.IGNORECASE)
VERSION_ANSWER = re.compile(r"project[-_ ]?reclaimer\b\D*?\bv?(\d+(?:\.\d+)+)", re.IGNORECASE)
BARE_VERSION = re.compile(r"^\s*v?(\d+(?:\.\d+)+)\b")


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


def _release_order(path: Path) -> tuple[int, tuple[int, ...], str]:
    """project-reclaimer-dedicated-v0.9.10.exe after v0.9.9: by the version in the name (numbers, not text)."""
    match = RELEASE_NAME.search(path.name)
    numbers = tuple(int(n) for n in match.group(1).split(".")) if match else ()
    return (1 if match else 0, numbers, path.name)


def find_executable(folder: Path, name: str = EXECUTABLE) -> Path | None:
    """The server program in a folder: the configured name, or the release's own name for it (the newest)."""
    if (folder / name).is_file():
        return folder / name
    found = sorted(folder.glob("project-reclaimer-dedicated*.exe"), key=_release_order)
    return found[-1] if found else None


def find_program(home: Path, folders: list[Path], name: str = EXECUTABLE,
                 hint: str | os.PathLike[str] | None = None, *, template: list[str] | None = None) -> Path | None:
    """One copy of the program for commands that aren't for one server folder: the hinted one, or else the newest
    of the root's and the server folders' copies (by `--version`; the root's when none is newer or none says)."""
    if hint:
        path = Path(hint)
        if not path.is_absolute():
            path = home / path
        if path.is_file():
            return path
        if path.is_dir() and (found := find_executable(path, name)):
            return found
    copies: list[Path] = []
    builds: set[tuple[int, int]] = set()
    for folder in [home, *folders]:
        found = find_executable(folder, name)
        if found is not None and found not in copies:
            copies.append(found)
            try:
                info = found.stat()
                builds.add((info.st_size, info.st_mtime_ns))
            except OSError:
                builds.add((-len(copies), 0))
    if len(copies) <= 1 or len(builds) == 1:  # one file, or the same one linked into each server: no need to ask
        return copies[0] if copies else None
    return newest_copy(copies, template)


def newest_copy(copies: list[Path], template: list[str] | None = None) -> Path:
    """The copy whose `--version` is the highest; the first of them on a tie, or when none says."""
    best, best_version = copies[0], None
    for copy in copies:
        numbers = _numbers(cached_version(copy, template))
        if numbers is not None and (best_version is None or numbers > best_version):
            best, best_version = copy, numbers
    return best


def _numbers(version: str | None) -> tuple[int, ...] | None:
    match = BARE_VERSION.match(version or "")
    return tuple(int(n) for n in match.group(1).split(".")) if match else None


_VERSIONS: dict[tuple[str, int, int, tuple[str, ...]], str | None] = {}


def cached_version(path: Path, template: list[str] | None = None) -> str | None:
    """`Dedicated(path).version()`, asked once per file as it is (its size and time; a new file asks again)."""
    try:
        info = path.stat()
    except OSError:
        return None
    key = (str(path), info.st_mtime_ns, info.st_size, tuple(template or ()))
    if key not in _VERSIONS:
        _VERSIONS[key] = Dedicated(path, template=template).version()
    return _VERSIONS[key]


def program_prefix(executable: Path, template: list[str] | None = None) -> list[str]:
    """How the program is run: [exe] on Windows, [wine, exe] on Linux, or the template's words before `dedicated`."""
    if template and "dedicated" in template:
        words = template[: template.index("dedicated")]
        return [w.replace("{executable}", str(executable)) for w in words] or [str(executable)]
    return [str(executable)] if os.name == "nt" else ["wine", str(executable)]


def tool_for(settings: Settings, server: ServerConfig | None = None) -> Dedicated:
    """The runner for this root (for `server`: the program that server runs), or a DedicatedError saying where the
    program should be. Whichever copy it starts with, a command for a server folder with its own copy runs that."""
    options = settings.backend_options if settings.backend == "process" else {}
    name = str(options.get("executable") or EXECUTABLE)
    template = [str(w) for w in options.get("command") or []]
    program = None
    if server is not None:  # the copy it runs: its own, else the root's (as the process backend picks)
        program = find_executable(server.folder, name) or find_executable(settings.home, name)
    if program is None:
        program = find_program(settings.home, settings.server_folders, name, settings.setup.program,
                               template=template)
    if program is None:
        raise DedicatedError(
            f"the server program ({name}) isn't in any server folder or at {settings.home}: download the dedicated "
            "server release from https://github.com/ProjectReclaimer/project-reclaimer-releases/releases/latest, "
            f"rename it {name} and put it at the root, or set [setup] program in {settings.path.name}")
    return Dedicated(program, template=template, config_name=settings.config_name, own_copies=name,
                     root=settings.home, server_folders=settings.server_folders)


def _resolved(folder: Path) -> Path:
    try:
        return Path(folder).resolve()
    except OSError:
        return Path(folder)


def _start(command: list[str], folder: Path, *, group: bool = False) -> subprocess.Popen[str]:
    """The program, its output (and errors) on one pipe. group: in a process group of its own, for end_tree's
    gentle stop."""
    env = dict(os.environ, RECLAIMER_DEDICATED_AUTO_UPDATE="false")
    options: dict[str, Any] = {}
    if group and os.name == "nt":
        options["creationflags"] = CREATE_NEW_PROCESS_GROUP
    elif group:
        options["start_new_session"] = True
    return subprocess.Popen(command, cwd=str(folder), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", bufsize=1, **options)


def end_tree(process: subprocess.Popen[Any], *, gently: bool = False, grace: float = 3.0) -> None:
    """End the program and everything it started (SteamCMD). gently (a process started with group=True):
    Ctrl+Break on Windows, SIGTERM on Linux first, so it can stop cleanly; then whatever is left is killed."""
    try:
        root = psutil.Process(process.pid)
        tree = [*root.children(recursive=True), root]  # before anything ends: an orphan can't be found later
    except psutil.Error:
        tree = []
    if gently and process.poll() is None:
        try:
            if os.name == "nt":
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass  # no console to send it on: killed below
        try:
            process.wait(grace)
        except subprocess.TimeoutExpired:
            pass
    for member in tree:
        try:
            member.kill()  # psutil checks it is still the process it was (a reused number is left alone)
        except psutil.Error:
            pass
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        pass


def _pump(stream: Any, collected: list[str], on_line: Callable[[str], None] | None = None) -> None:
    """Read the output to its end, then close the pipe: the reader closes it, as only the reader knows when it
    is done (one left waiting on a child that holds the pipe closes it once that child is gone)."""
    try:
        for line in stream:
            collected.append(line)
            if on_line is not None:
                on_line(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _wait(process: subprocess.Popen[Any], timeout: float, stop: threading.Event | None) -> str:
    """'' once it has ended by itself, 'timeout' or 'stopped' (stop was set) while it still runs."""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return "timeout"
        try:
            process.wait(min(left, 0.2) if stop is not None else left)
            return ""
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                return "stopped"


class Dedicated:
    def __init__(self, executable: Path, *, template: list[str] | None = None,
                 config_name: str = "dedicated.toml", own_copies: str | None = None, root: Path | None = None,
                 server_folders: list[Path] | None = None) -> None:
        self.executable = Path(executable)
        self.template = template
        self.prefix = program_prefix(self.executable, template)
        self.config_name = config_name
        self.own_copies = own_copies  # the program's file name: a folder with a copy of its own runs that copy
        self.root = root  # a server folder without a copy runs the root's, as the server itself does
        self._server_folders = {_resolved(f) for f in server_folders or []}

    def executable_for(self, folder: Path | None) -> Path:
        """The copy a `dedicated` command for `folder` runs: the one that server runs (its own, else the root's),
        else this runner's."""
        if self.own_copies and folder is not None:
            own = find_executable(folder, self.own_copies)
            if own is not None:
                return own
            if self.root is not None and _resolved(folder) in self._server_folders:
                shared = find_executable(self.root, self.own_copies)
                if shared is not None:
                    return shared
        return self.executable

    def command(self, *words: str, config: str | None = None, program: bool = False,
                folder: Path | None = None) -> list[str]:
        """`dedicated <words>`, or with program the program's own commands (`mode-check <file>`)."""
        if program:
            return [*self.prefix, *words]
        executable = self.executable_for(folder)
        prefix = self.prefix if executable == self.executable else program_prefix(executable, self.template)
        extra = ["--config", config] if config else []
        return [*prefix, "dedicated", *extra, *words]

    def run_sync(self, folder: Path, *words: str, timeout: float = QUICK, config: str | None = None,
                 program: bool = False, stop: threading.Event | None = None) -> ToolResult:
        """Run it and wait (at most `timeout`; set `stop` to end it sooner). Either way the whole process tree is
        ended, so a SteamCMD it started can't hold this up."""
        command = self.command(*words, config=config, program=program, folder=folder)
        try:
            process = _start(command, folder)
        except FileNotFoundError:
            return ToolResult(False, 127, f"couldn't run {command[0]}: not found", command)
        except OSError as error:
            return ToolResult(False, 126, f"couldn't run {command[0]}: {error}", command)
        collected: list[str] = []
        reader = threading.Thread(target=_pump, args=(process.stdout, collected), daemon=True,
                                  name=f"dedicated {' '.join(words[:1])}")
        reader.start()
        ended = _wait(process, timeout, stop)
        if ended:
            end_tree(process)
        reader.join(READER_GRACE)  # a child it left running may hold the pipe open: never wait on that
        text = "".join(collected)
        if ended == "timeout":
            return ToolResult(False, 124, f"{' '.join(words)} took longer than {timeout:.0f} s and was stopped"
                              + (f"\n{text}" if text else ""), command)
        if ended == "stopped":
            return ToolResult(False, 130, f"{' '.join(words)} was stopped before it finished"
                              + (f"\n{text}" if text else ""), command)
        code = process.returncode if process.returncode is not None else -1
        return ToolResult(code == 0, code, text, command)

    def version(self) -> str | None:
        """The program's own version (`--version`: "project-reclaimer 0.9.20"). None when it doesn't answer (an
        old program, or one that can't run); its first line as it is when it answers with something that isn't a
        version this can read, so that copy counts as unknown, never as older."""
        result = self.run_sync(self.executable.parent, "--version", timeout=30, program=True)
        if not result.ok:
            return None
        match = VERSION_ANSWER.search(result.text) or BARE_VERSION.match(result.text)
        if match:
            return match.group(1)
        return (result.lines or ["(no answer)"])[0].strip()[:80]

    async def run(self, folder: Path, *words: str, timeout: float = QUICK, config: str | None = None) -> ToolResult:
        return await asyncio.to_thread(self.run_sync, folder, *words, timeout=timeout, config=config)

    async def stream(self, folder: Path, *words: str, on_line: Any, cancel: asyncio.Event | None = None,
                     timeout: float = SLOW, config: str | None = None) -> ToolResult:
        """Run a command and hand each output line to on_line as it arrives; set `cancel` to end it early.

        A Workshop download or a long check shows its progress this way, and the window
        stays in control: Esc cancels. The program runs in a process group of its own, so
        it gets Ctrl+Break (it and SteamCMD stop cleanly, and SteamCMD lets go of the
        download the servers' own fetches wait on); whatever is left is then killed, and
        nothing waits on its output pipe.
        """
        command = self.command(*words, config=config, folder=folder)
        loop = asyncio.get_running_loop()
        try:
            process = _start(command, folder, group=True)
        except FileNotFoundError:
            return ToolResult(False, 127, f"couldn't run {command[0]}: not found", command)
        except OSError as error:
            return ToolResult(False, 126, f"couldn't run {command[0]}: {error}", command)
        collected: list[str] = []
        finished: asyncio.Future[None] = loop.create_future()
        listening = [True]

        def deliver(line: str) -> None:
            if listening[0]:
                on_line(line.rstrip("\r\n"))

        def done_reading() -> None:
            if not finished.done():
                finished.set_result(None)

        def pump() -> None:
            try:
                _pump(process.stdout, collected, lambda line: loop.call_soon_threadsafe(deliver, line))
            except RuntimeError:
                pass  # the loop has closed
            finally:
                try:
                    loop.call_soon_threadsafe(done_reading)
                except RuntimeError:
                    pass

        threading.Thread(target=pump, daemon=True, name=f"dedicated {' '.join(words[:1])}").start()
        deadline = loop.time() + timeout
        ended = ""
        try:
            while process.poll() is None:
                if cancel is not None and cancel.is_set():
                    ended = "cancelled"
                    break
                if loop.time() >= deadline:
                    ended = "timeout"
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            end_tree(process)  # the window itself is going: no time for a gentle stop
            listening[0] = False
            raise
        if ended:
            await asyncio.to_thread(end_tree, process, gently=True)
        try:
            await asyncio.wait_for(asyncio.shield(finished), READER_GRACE)
        except (TimeoutError, asyncio.TimeoutError):
            pass  # something it started still holds its output: left to end by itself
        await asyncio.sleep(0)  # the lines already read reach on_line first
        listening[0] = False
        code = process.returncode if process.returncode is not None else -1
        text = "".join(collected)
        if ended == "cancelled":
            return ToolResult(False, 130, text + "\n(cancelled)", command)
        if ended == "timeout":
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

    def mods(self, folder: Path, *, stop: threading.Event | None = None) -> ToolResult:
        return self.run_sync(folder, "mods", timeout=SLOW, config=self.config_name, stop=stop)

    def mode_check(self, path: Path) -> ToolResult:
        """The program's own check of a saved custom game (one with a script; gameoptions adds an empty one)."""
        return self.run_sync(path.parent, "mode-check", str(path), program=True)

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
        if line and not line[0].isspace():
            kind = ""  # a section this doesn't read (Halo 3: ODST's, from 0.9.19), or the closing hint
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
