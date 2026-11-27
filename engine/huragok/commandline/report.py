"""`report`: one zip for a host to send to whoever is helping when something goes wrong, on a computer nobody else can
look at (the first Mac): Huragok's log and each server's console, the window's transcript, the start loop's and
start.sh's lines, what `check` says, this computer's facts (on a Mac also what pmset, the firewall and launchd say) and
the settings files with every password, token and key taken out.

Never in it: discord.toml, records/discord-*, the database, the ban lists, the stats key or anything else in a
server's data folder but its status.json and server.log. Settings that can't be read still give a report: the logs
are what's needed most when nothing starts."""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any

from .. import __version__
from ..core import platforms, sysinfo
from ..core.settings import COMMANDS_FILE, Settings
from ..core.tables import ConfigError
from .common import FAILED
from .shell import console as out

LOG_TAIL = 20_000_000  # bytes kept of each log: its end, where the trouble is
LOG_FILES = ("*.log", "*.log.[0-9]", "*.txt", "*.json")  # logs/ itself: huragok.log and its old ones, consoles, faulted-*
RECORDS = ("running-servers.json", "last-update.json", "live.json", "program-releases.json", "files.json")
TAKEN_OUT = '"(taken out)"'
# key = value where the key names a secret: whole lines and inline tables alike
SECRET = re.compile(r"""(?im)(^|[{,\s])((?:[\w.-]*?)(?:password|passwd|token|secret|webhook|key)[\w.-]*\s*=\s*)"""
                    r"""("(?:[^"\\\n]|\\.)*"|'[^'\n]*'|[^,}\s#]+)""")


def redact(text: str) -> str:
    """A settings file with each secret's value replaced: password, token, secret, webhook, key (any key with one of
    those in its name). More is taken out than needs to be, never less."""
    def hide(match: re.Match[str]) -> str:
        value = match.group(3)
        if value in ("true", "false") or re.fullmatch(r"[-+]?\d[\d_.]*", value):
            return match.group(0)  # use_host_key = true says how it's set up, and hides nothing
        return f"{match.group(1)}{match.group(2)}{TAKEN_OUT}"

    return SECRET.sub(hide, text)


def _tail(path: Path, limit: int = LOG_TAIL) -> bytes:
    with path.open("rb") as file:
        file.seek(0, 2)
        size = file.tell()
        if size <= limit:
            file.seek(0)
            return file.read()
        file.seek(size - limit)
        return f"[the first {size - limit} bytes left out]\n".encode() + file.read()


def report_name(now: float | None = None) -> str:
    return time.strftime("huragok-report-%Y-%m-%d-%H%M%S.zip", time.localtime(now))


class Report:
    """The zip being written, and report.txt's lines: what went in, and what couldn't."""

    def __init__(self, zip_file: zipfile.ZipFile) -> None:
        self.zip = zip_file
        self.lines: list[str] = []
        self.added: list[str] = []

    def file(self, path: Path, name: str, *, secret: bool = False, tail: bool = False) -> None:
        try:
            if secret:
                data = redact(path.read_text(encoding="utf-8", errors="replace")).encode("utf-8")
            else:
                data = _tail(path) if tail else path.read_bytes()
        except OSError as error:
            self.lines.append(f"couldn't read {path}: {error}")
            return
        self.zip.writestr(name, data)
        self.added.append(name)

    def text(self, name: str, text: str) -> None:
        self.zip.writestr(name, text)
        self.added.append(name)


def _check_output(config: str) -> str:
    """What `check` says, as the host would read it."""
    from .check import cmd_check

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cmd_check(argparse.Namespace(config=config, set_aside=False))
        buffer.write(f"\n(check's exit code: {code})\n")
    except SystemExit as stop:
        buffer.write(f"\n(check stopped: {stop.code})\n")
    except Exception:  # noqa: BLE001 - a report is wanted most when things are broken
        buffer.write("\ncheck failed:\n" + traceback.format_exc())
    return buffer.getvalue()


def _facts_text(home: Path, settings: Settings | None, problem: str) -> str:
    lines = [f"Huragok {__version__} report, written {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
             f"folder    {home}", ""]
    lines += [f"{name:<16}{value}" for name, value in sysinfo.facts(home, ask_tools=True)]
    if problem:
        lines += ["", f"settings  couldn't be read: {problem}"]
    if settings is not None:
        lines += ["", "Servers"]
        for server in settings.servers:
            lines.append(f"  {server.name}  ({server.folder})")
            from ..program.releases import program_of

            try:
                program = program_of(settings, server)
            except Exception as error:  # noqa: BLE001
                lines.append(f"    program: couldn't tell ({error})")
                continue
            lines.append(f"    program: {program if program is not None else 'not found'}")
            if program is not None:
                for problem_text in sysinfo.program_problems(program):
                    lines.append(f"    PROBLEM {problem_text}")
        if not settings.servers:
            lines.append("  none found")
    return "\n".join(lines) + "\n"


def write_report(config: str, *, now: float | None = None) -> tuple[Path, Report]:
    """The zip, written next to settings.toml. Returns where it is and what went in."""
    settings_path = Path(config).resolve()
    home = settings_path.parent
    settings: Settings | None = None
    problem = ""
    try:
        from ..core.settings import load

        settings = load(str(settings_path)) if settings_path.exists() else None
        if settings is None:
            problem = f"{settings_path} isn't there"
    except (ConfigError, OSError, ValueError) as error:
        problem = str(error)
    path = home / report_name(now)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
        report = Report(zip_file)
        report.text("computer.txt", _facts_text(home, settings, problem))
        report.text("check.txt", _check_output(str(settings_path)) if settings is not None else
                    f"check wasn't run: {problem}\n")
        logs = settings.logs if settings is not None else home / "logs"
        seen: set[Path] = set()
        for pattern in LOG_FILES:
            for log_path in sorted(logs.glob(pattern)) if logs.is_dir() else []:
                if log_path.is_file() and log_path not in seen:
                    seen.add(log_path)
                    report.file(log_path, f"logs/{log_path.name}", tail=True)
        if settings is not None and settings.log_file and settings.log_file.parent != logs:
            for log_path in sorted(settings.log_file.parent.glob(settings.log_file.name + "*")):
                report.file(log_path, f"logs/{log_path.name}", tail=True)
        for name in (settings_path.name, COMMANDS_FILE):
            if (home / name).is_file():
                report.file(home / name, name, secret=True)
        for name in RECORDS:
            if (home / "records" / name).is_file():
                report.file(home / "records" / name, f"records/{name}")
        servers_records = home / "records" / "servers"
        for record in sorted(servers_records.glob("*.json")) if servers_records.is_dir() else []:
            report.file(record, f"records/servers/{record.name}")
        if settings is not None:
            _servers(report, settings)
        _mac(report)
        report.lines.insert(0, f"In this report: {len(report.added) + 1} files")
        report.text("report.txt", "\n".join([*report.lines, "", *report.added, ""]))
    return path, report


def _servers(report: Report, settings: Settings) -> None:
    configs: set[Path] = set()
    for server in settings.servers:
        where = f"servers/{server.slug}"
        if server.config_path not in configs and server.config_path.is_file():
            configs.add(server.config_path)
            report.file(server.config_path, f"{where}/{server.config_path.name}", secret=True)
        for path in (server.status_path, server.log_path):
            if path.is_file():
                report.file(path, f"{where}/{path.name}", tail=True)


CRASH_REPORTS = ("project-reclaimer", "Python", "python3", "steamcmd")  # macOS's own crash reports kept, by program
CRASH_REPORT_DAYS = 7
CRASH_REPORTS_KEPT = 10
SYSTEM_LOG_HOURS = 6
SYSTEM_LOG_LINES = 3000
# What macOS's log says of the server program: Gatekeeper refusing it, memory pressure ending it, its crashes
SYSTEM_LOG_PREDICATE = ('eventMessage CONTAINS[c] "project-reclaimer" OR process == "project-reclaimer" OR '
                        '(process == "syspolicyd" AND eventMessage CONTAINS[c] "reclaimer")')


def _mac(report: Report, *, now: float | None = None) -> None:
    """On a Mac: the LaunchAgent as written, what launchd says of it (runs, the last exit code), macOS's own crash
    reports of the server program, Python and SteamCMD from the last week, and what the system log says of the server
    program in the last hours (Gatekeeper, memory pressure)."""
    if not platforms.is_macos():
        return
    from ..core import desktop

    agent = desktop.startup_path()
    if agent is not None and agent.is_file():
        report.file(agent, f"mac/{agent.name}")
        report.text("mac/launchd.txt", sysinfo.launchd_text() + "\n")
    folder = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    since = (now if now is not None else time.time()) - CRASH_REPORT_DAYS * 86400
    found = []
    for path in folder.glob("*") if folder.is_dir() else []:
        try:
            if path.is_file() and path.name.startswith(CRASH_REPORTS) and path.stat().st_mtime >= since:
                found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(found)[-CRASH_REPORTS_KEPT:]:
        report.file(path, f"mac/crash-reports/{path.name}", tail=True)
    answer = sysinfo.ask("log", "show", "--last", f"{SYSTEM_LOG_HOURS}h", "--style", "compact", "--predicate",
                         SYSTEM_LOG_PREDICATE, timeout=60)
    if answer is None:
        report.lines.append("macOS's log didn't answer in a minute (log show)")
    else:
        lines = answer[1].splitlines()
        report.text("mac/system-log.txt", "\n".join(lines[-SYSTEM_LOG_LINES:]) + "\n")


def cmd_report(args: argparse.Namespace) -> int:
    out.title("REPORT", f"Huragok {__version__}")
    try:
        path, report = write_report(args.config)
    except OSError as error:
        out.fail(f"couldn't write the report: {error}")
        return FAILED
    size = path.stat().st_size
    out.fields([("Written", f"{path} ({size / 1e6:.1f} MB)"), ("Files", str(len(report.added)))])
    for line in report.lines[1:]:
        out.warn(line)
    out.say("Send that file to whoever is helping. It holds Huragok's log and each server's console (player names and "
            "chat are in them), what check says and this computer's facts. Passwords, tokens and keys are taken out "
            "of the settings files; discord.toml, the database and the ban lists are left out.")
    if platforms.is_macos():
        out.say(f"In Finder: open {path.parent} (or in Terminal: open -R \"{path}\")")
    return 0


def add_parser(commands: Any) -> None:
    report = commands.add_parser("report", help="One zip of the logs, what check says and this computer's facts, to "
                                                "send to whoever is helping (passwords taken out)")
    report.set_defaults(func=cmd_report)


__all__ = ["add_parser", "cmd_report", "redact", "write_report"]
