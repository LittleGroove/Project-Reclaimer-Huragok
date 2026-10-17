"""SteamCMD for the Workshop: finding it, installing it, signing in, and telling every server about it.

A dedicated server installs Workshop items itself (`[workshop] items`, `dedicated mods`), but on
a headless server it needs SteamCMD and an account that owns MCC: an anonymous SteamCMD
sign-in can't fetch MCC's items (checked). So the supervisor:

1. finds SteamCMD: [setup] steamcmd, <root>/steamcmd/, C:\\steamcmd\\ (the folder the server's
   own template names), PATH, ~/steamcmd (Linux);
2. installs it when missing, from Valve's installer, into <root>/steamcmd/ (kept with the
   project) or the standard folder, and runs it once so it updates itself;
3. signs in: `steamcmd +login <account>` in a window of its own, where Steam asks for the
   password and the Steam Guard code once; after that the account's login is cached and
   `dedicated mods` runs without prompts (never a password in any file);
4. writes `[workshop] steamcmd` and `username` into every server's dedicated.toml and
   `[setup] steamcmd` / `steam_username` into settings.toml.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import Settings

WINDOWS_INSTALLER = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
LINUX_INSTALLER = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
STANDARD_WINDOWS = Path(r"C:\steamcmd")
MCC_APP = 976730


class SteamSetupError(Exception):
    pass


@dataclass
class SteamStatus:
    executable: Path | None
    where: str  # how it was found
    username: str  # the account every server's [workshop] names, or ""
    accounts: list[str]  # accounts with a cached sign-in in SteamCMD's config
    servers_configured: int  # how many server folders name steamcmd under [workshop]
    servers_total: int

    @property
    def ready(self) -> bool:
        return self.executable is not None and bool(self.username) and self.username.lower() in [
            a.lower() for a in self.accounts]

    def summary(self) -> str:
        if self.executable is None:
            return "SteamCMD isn't installed where it's expected (press I to install it)"
        bits = [f"SteamCMD at {self.executable} ({self.where})"]
        if self.username:
            bits.append(f"account {self.username}" + ("" if self.username.lower() in [a.lower() for a in self.accounts]
                                                      else " (not signed in yet)"))
        else:
            bits.append("no Steam account set: Workshop items can't be downloaded")
        bits.append(f"{self.servers_configured} of {self.servers_total} server folder(s) set up for it")
        return " · ".join(bits)


def executable_name() -> str:
    return "steamcmd.exe" if os.name == "nt" else "steamcmd.sh"


def candidates(home: Path, hint: str = "") -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    if hint:
        path = Path(hint)
        if not path.is_absolute():
            path = home / path
        found.append((path if path.name.lower().startswith("steamcmd") and path.suffix else path / executable_name(),
                      "[setup] steamcmd"))
    found.append((home / "steamcmd" / executable_name(), "the root's steamcmd folder"))
    if os.name == "nt":
        found.append((STANDARD_WINDOWS / "steamcmd.exe", "the standard folder"))
    else:
        found.append((Path.home() / "steamcmd" / "steamcmd.sh", "~/steamcmd"))
        found.append((Path("/usr/games/steamcmd"), "the system's package"))
    on_path = shutil.which("steamcmd")
    if on_path:
        found.append((Path(on_path), "PATH"))
    return found


def find_steamcmd(home: Path, hint: str = "") -> tuple[Path | None, str]:
    for path, where in candidates(home, hint):
        if path.is_file():
            return path, where
    return None, ""


def cached_accounts(executable: Path) -> list[str]:
    """Accounts SteamCMD has a saved sign-in for (config/config.vdf, the "Accounts" block)."""
    config = executable.parent / "config" / "config.vdf"
    try:
        text = config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    match = re.search(r'"Accounts"\s*\{(.*)', text, re.S)
    if not match:
        return []
    block = match.group(1)
    names: list[str] = []
    depth = 1
    for line in block.splitlines():
        stripped = line.strip()
        if stripped == "{":
            depth += 1
        elif stripped == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and stripped.startswith('"') and stripped.endswith('"') and stripped.count('"') == 2:
            names.append(stripped.strip('"'))
    return names


def workshop_settings(config: Path) -> tuple[str, str]:
    from .tomledit import read_toml

    try:
        workshop = read_toml(config).get("workshop", {})
    except (OSError, ValueError):
        return "", ""
    if not isinstance(workshop, dict):
        return "", ""
    return str(workshop.get("steamcmd") or ""), str(workshop.get("username") or "")


def status(settings: Settings) -> SteamStatus:
    executable, where = find_steamcmd(settings.home, settings.setup.steamcmd)
    username = settings.setup.steam_username
    configured = 0
    for folder in settings.server_folders:
        path, user = workshop_settings(folder / settings.config_name)
        if path:
            configured += 1
        if not username and user:
            username = user
    accounts = cached_accounts(executable) if executable else []
    return SteamStatus(executable, where, username, accounts, configured, len(settings.server_folders))


def install(target: Path, say: Callable[[str], None] = print, timeout: float = 900.0) -> Path:
    """Download Valve's installer into target/ and run SteamCMD once so it updates itself. Returns the program."""
    from .updates import UpdateError, _get

    target.mkdir(parents=True, exist_ok=True)
    url = WINDOWS_INSTALLER if os.name == "nt" else LINUX_INSTALLER
    say(f"downloading SteamCMD from {url}")
    try:
        data = _get(url, 120, accept="application/octet-stream")
    except UpdateError as error:
        raise SteamSetupError(f"couldn't download SteamCMD: {error}") from None
    try:
        if os.name == "nt":
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                bundle.extractall(target)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
                bundle.extractall(target, filter="data")
    except (zipfile.BadZipFile, tarfile.TarError) as error:
        raise SteamSetupError(f"the SteamCMD installer didn't unpack: {error}") from None
    executable = target / executable_name()
    if not executable.is_file():
        raise SteamSetupError(f"the installer left no {executable_name()} in {target}")
    if os.name != "nt":
        executable.chmod(0o755)
    say("running SteamCMD once so it updates itself (a minute or two the first time)")
    result = run(executable, ["+quit"], timeout=timeout)
    if result.returncode not in (0, 7):  # 7: SteamCMD updated itself and asks to be run again
        raise SteamSetupError(f"SteamCMD didn't start: {result.stdout[-400:]}")
    say(f"SteamCMD is at {executable}")
    return executable


def run(executable: Path, words: list[str], timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(executable), *words], cwd=str(executable.parent), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, encoding="utf-8",
                          errors="replace", check=False)


def signed_in(executable: Path, username: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Whether SteamCMD signs `username` in without asking (a cached sign-in). (ok, the last lines)."""
    if username.lower() in [a.lower() for a in cached_accounts(executable)]:
        try:
            result = run(executable, ["+login", username, "+quit"], timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "SteamCMD waited for a password: the sign-in isn't cached"
        text = result.stdout
        ok = "Waiting for user info...OK" in text or "Logged in OK" in text
        return ok, text[-300:]
    return False, "no cached sign-in for that account"


def login_window(executable: Path, username: str) -> str:
    """Open SteamCMD's sign-in in a window of its own, where Steam asks for the password and the Guard code."""
    if os.name == "nt":
        command = f'"{executable}" +login {username} +quit'
        try:
            terminal = shutil.which("wt.exe")
            if terminal:
                subprocess.Popen([terminal, "-w", "0", "new-tab", "--title", "Steam sign-in", "cmd", "/c",
                                  f"{command} & echo. & echo Done: close this window & pause"],
                                 cwd=str(executable.parent))
            else:
                subprocess.Popen(f'start "Steam sign-in" cmd /c "{command} & pause"', shell=True,
                                 cwd=str(executable.parent))
        except OSError as error:
            return f"couldn't open a window ({error}); run this yourself: {command}"
        return ("a Steam sign-in window opened: type the account's password and the Steam Guard code there, "
                "then come back")
    line = f"{executable} +login {username} +quit"
    for program, flags in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                           ("xfce4-terminal", ["-e"]), ("xterm", ["-e"])):
        if shutil.which(program) and os.environ.get("DISPLAY"):
            try:
                args = [program, *flags, "sh", "-c", f"{line}; echo; read -p 'Done: press Enter' x"] \
                    if flags != ["-e"] else [program, "-e", f"sh -c '{line}; read -p Done x'"]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "a Steam sign-in window opened: type the password and the Steam Guard code there"
            except OSError:
                continue
    return f"no desktop terminal found; run this in another terminal, then come back: {line}"


def configure(settings: Settings, executable: Path, username: str) -> list[Path]:
    """Write [workshop] steamcmd and username into every server folder, and [setup] into settings.toml."""
    from .tomledit import set_table_key

    written: list[Path] = []
    for folder in settings.server_folders:
        config = folder / settings.config_name
        if not config.is_file():
            continue
        set_table_key(config, "workshop", "steamcmd", str(executable), before="master")
        set_table_key(config, "workshop", "username", username, before="master")
        written.append(config)
    set_table_key(settings.path, "setup", "steamcmd", str(executable), before="process")
    set_table_key(settings.path, "setup", "steam_username", username, before="process")
    settings.setup.steamcmd = str(executable)
    settings.setup.steam_username = username
    return written


def add_to_path(folder: Path) -> str:
    """Put SteamCMD's folder on the user's PATH, for every other tool that wants it."""
    if os.name == "nt":
        script = ("$p = [Environment]::GetEnvironmentVariable('Path', 'User'); "
                  f"if (($p -split ';') -notcontains '{folder}') {{ [Environment]::SetEnvironmentVariable('Path', "
                  f"($p.TrimEnd(';') + ';{folder}'), 'User'); 'added' }} else {{ 'already there' }}")
        try:
            result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                    timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"couldn't change PATH: {error}"
        answer = (result.stdout or result.stderr).strip()
        return f"{folder} {answer} on your PATH (new windows see it)" if result.returncode == 0 else \
            f"couldn't change PATH: {answer[-200:]}"
    return add_to_profile(folder, Path.home() / ".profile")


def add_to_profile(folder: Path, profile: Path) -> str:
    """The Linux half of add_to_path: an export line in the shell's profile, once."""
    line = f'export PATH="$PATH:{folder}"'
    try:
        text = profile.read_text(encoding="utf-8") if profile.exists() else ""
        if line not in text:
            profile.write_text(text.rstrip("\n") + f"\n{line}\n", encoding="utf-8")
            return f"added to {profile} (new shells see it)"
        return f"{profile} already has it"
    except OSError as error:
        return f"couldn't edit {profile}: {error}"


def workshop_download(executable: Path, username: str, item: int, timeout: float = 1800.0) -> tuple[bool, str]:
    """Fetch one item with SteamCMD itself (`dedicated mods` does this for the servers; this is for checking)."""
    try:
        result = run(executable, ["+login", username, "+workshop_download_item", str(MCC_APP), str(item), "+quit"],
                     timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "SteamCMD took too long (waiting for a password?)"
    ok = "Success" in result.stdout and "ERROR" not in result.stdout
    return ok, result.stdout[-400:]
