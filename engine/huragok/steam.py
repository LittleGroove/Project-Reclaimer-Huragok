"""Steam: its library folders and MCC's place in them, the Workshop's API, and SteamCMD for the Workshop and
Halo 3's files (finding it, installing it, signing in, telling every server).

MCC is found only where Steam's own records say it is (the registry, libraryfolders.vdf, appmanifest_976730.acf),
never by searching drives.

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
   `[setup] steamcmd` / `steam_username` into settings.toml;
5. downloads one depot of MCC (a piece of it, such as Halo 3's multiplayer files) with `download_depot`, for a
   server with no MCC on its PC (content/gamefiles.py keeps what a server uses and leaves out sound and video).

Before a Workshop update restarts a server, cannot_fetch() says whether its SteamCMD can fetch anything here: a
copied server can name a SteamCMD that's on another PC, or an account with no saved sign-in on this one.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import __version__
from .core.files import clean_path
from .core.web import UpdateError, fetch

if TYPE_CHECKING:
    from .core.settings import Settings


STEAM_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


@dataclass
class SteamItem:
    item: int
    title: str = ""
    updated: int | None = None  # Steam's time_updated, Unix seconds
    manifest: str = ""  # Steam's hcontent_file


def workshop_details(items: list[int], timeout: float = 20.0) -> dict[int, SteamItem]:
    """Steam's public Workshop details (title, last update, content manifest) for these items. No sign-in needed."""
    form = {"itemcount": str(len(items))}
    for number, item in enumerate(items):
        form[f"publishedfileids[{number}]"] = str(item)
    request = urllib.request.Request(STEAM_DETAILS, data=urllib.parse.urlencode(form).encode(),
                                     headers={"User-Agent": f"huragok/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4_000_000)
    except urllib.error.HTTPError as error:
        raise UpdateError(f"Steam answered {error.code} {error.reason}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise UpdateError(f"couldn't reach Steam: {getattr(error, 'reason', error)}") from None
    try:
        details = json.loads(body).get("response", {}).get("publishedfiledetails", [])
    except (ValueError, AttributeError):
        raise UpdateError("Steam's answer wasn't the Workshop details") from None
    found: dict[int, SteamItem] = {}
    for entry in details if isinstance(details, list) else []:
        try:
            item = int(entry.get("publishedfileid"))
        except (TypeError, ValueError):
            continue
        if entry.get("result") != 1:
            continue
        updated = entry.get("time_updated")
        found[item] = SteamItem(item, str(entry.get("title") or ""), int(updated) if isinstance(updated, int) else None,
                                str(entry.get("hcontent_file") or ""))
    return found


def is_mcc_install(game: Path) -> bool:
    """MCC's own folder (a server's `game` may name it): linking it whole would link every game MCC has."""
    return (game / "mcclauncher.exe").is_file() or (game / "MCC").is_dir() and (game / "halo1").is_dir()


MCC_FOLDER = "Halo The Master Chief Collection"  # the folder Steam installs it in (its appmanifest says so)


def _steam_roots() -> list[Path]:
    """Where Steam is, as Steam itself records it: on Windows the registry's SteamPath (and the InstallPath its
    installer writes), and only when neither is there, Steam's own install folders; on Linux the ~/.steam links
    Steam keeps, its data folder and Flatpak's. Each once, and only one that's there."""
    roots: list[Path] = []
    if os.name == "nt":
        try:
            import winreg
        except ImportError:
            winreg = None  # type: ignore[assignment]
        for hive, key, value in (("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
                                 ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
            if winreg is None:
                break
            try:
                with winreg.OpenKey(getattr(winreg, hive), key) as handle:
                    roots.append(Path(str(winreg.QueryValueEx(handle, value)[0])))
            except OSError:
                continue
        if not roots:
            roots += [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                      Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam"]
    else:
        roots += [Path(p).expanduser() for p in ("~/.steam/steam", "~/.steam/root", "~/.local/share/Steam",
                                                  "~/.steam/debian-installation",
                                                  "~/.var/app/com.valvesoftware.Steam/.local/share/Steam")]
    found: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
            if key not in seen and root.is_dir():
                seen.add(key)
                found.append(root)
        except OSError:
            continue
    return found


def read_vdf(path: Path) -> dict[str, Any]:
    """Steam's KeyValues text (libraryfolders.vdf, appmanifest_*.acf) as nested dicts; {} when it can't be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    root: dict[str, Any] = {}
    stack = [root]
    key: str | None = None
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|([{}])', text):
        token, brace = match.group(1), match.group(2)
        if brace == "{":
            child: dict[str, Any] = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif key is None:
            key = re.sub(r"\\(.)", r"\1", token)
        else:
            stack[-1][key] = re.sub(r"\\(.)", r"\1", token)
            key = None
    return root


def steam_libraries() -> Iterator[tuple[Path, dict[str, Any] | None]]:
    """Each Steam library, as Steam lists them (steamapps/libraryfolders.vdf), with the games Steam says are in it
    (None from the old file, which doesn't say). Steam's own folder first."""
    seen: set[str] = set()
    for root in _steam_roots():
        listed: list[tuple[Path, dict[str, Any] | None]] = []
        for where in ("steamapps", "config"):  # Steam keeps the list in steamapps/, older Steam in config/
            data = read_vdf(root / where / "libraryfolders.vdf")
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for number, entry in (folders.items() if isinstance(folders, dict) else []):
                if not str(number).isdecimal():
                    continue
                if isinstance(entry, dict) and entry.get("path"):
                    apps = entry.get("apps")
                    listed.append((Path(str(entry["path"])), apps if isinstance(apps, dict) else None))
                elif isinstance(entry, str) and entry:  # before 2021: "1" "D:\\SteamLibrary"
                    listed.append((Path(entry), None))
        listed.append((root, None))  # Steam's own folder is a library, listed or not
        for library, apps in listed:
            key = str(library).lower().replace("/", "\\").rstrip("\\")
            if key in seen:
                continue
            seen.add(key)
            yield library, apps


def mcc_in(library: Path) -> Path | None:
    """The folder MCC's own manifest in this library names (appmanifest_976730.acf), or None: not installed here."""
    state = read_vdf(library / "steamapps" / f"appmanifest_{MCC_APP}.acf").get("AppState")
    folder = state.get("installdir") if isinstance(state, dict) else None
    return library / "steamapps" / "common" / str(folder) if folder else None


def _mcc_candidates(hint: str | None) -> Iterator[Path]:
    if hint and clean_path(hint):
        yield Path(clean_path(hint))
    for library, apps in steam_libraries():
        if apps is not None and str(MCC_APP) not in apps:  # Steam lists app ids as text
            continue  # Steam says MCC isn't in this library
        found = mcc_in(library)
        if found is not None:
            yield found


def find_mcc(hint: str | None = None) -> Path | None:
    """Where MCC is: the hint ([setup] mcc_folder), else where Steam's own records say it's installed. The first
    that has halo3/ wins; nothing past it is looked at, and nothing Steam doesn't name is looked at at all."""
    seen: set[str] = set()
    for path in _mcc_candidates(hint):
        if str(path).lower() in seen:
            continue
        seen.add(str(path).lower())
        try:
            if (path / "halo3").is_dir():
                return path
        except OSError:
            continue
    return None


WINDOWS_INSTALLER = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
LINUX_INSTALLER = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
STANDARD_WINDOWS = Path(r"C:\steamcmd")
MCC_APP = 976730
USERNAME = re.compile(r"[A-Za-z0-9_]{2,64}")
DEPOT_DONE = re.compile(r'Depot download complete\s*:\s*"([^"]+)"')
NO_PROMPTS = ["+@ShutdownOnFailedCommand", "1", "+@NoPromptForPassword", "1"]  # fail rather than wait for a person
DEPOT_HOURS = 4.0  # a depot is gigabytes: this long before SteamCMD is given up on


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
    hint = clean_path(hint or "")
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


def steam_roots(executable: Path) -> list[Path]:
    """Where SteamCMD keeps what it writes: beside it on Windows; on Linux steamcmd.sh keeps it in the Steam folder
    in the user's home (~/Steam, or the ~/.steam/steam and ~/.local/share/Steam a Steam client uses)."""
    roots = [executable.parent]
    if os.name != "nt":
        roots += [Path.home() / "Steam", Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"]
    return roots


TAR_FILTER = hasattr(tarfile, "data_filter")  # tarfile's extraction filters: Python 3.11.4 on


def extract_tar(bundle: tarfile.TarFile, target: Path) -> None:
    """A tarball unpacked into `target` and nowhere else: tarfile's "data" filter (Python 3.11.4 on), or on an older
    3.11 the same refusals by hand: no absolute path or "..", no link leading out, no device."""
    if TAR_FILTER:
        bundle.extractall(target, filter="data")
        return
    root = target.resolve()
    for member in bundle.getmembers():
        path = (root / member.name).resolve()
        lands = (path.parent / member.linkname).resolve() if member.issym() else root / member.linkname
        if not path.is_relative_to(root) or member.isdev() or (
                (member.issym() or member.islnk()) and not lands.is_relative_to(root)):
            raise tarfile.TarError(f"{member.name} would land outside {target}")
    bundle.extractall(target)


def cached_accounts(executable: Path) -> list[str]:
    """Accounts SteamCMD has a saved sign-in for (config/config.vdf, the "Accounts" block), wherever it keeps it."""
    text = ""
    for root in steam_roots(executable):
        try:
            text = (root / "config" / "config.vdf").read_text(encoding="utf-8", errors="replace")
            break
        except OSError:
            continue
    if not text:
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
    from .core.tomledit import read_toml

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
    target.mkdir(parents=True, exist_ok=True)
    url = WINDOWS_INSTALLER if os.name == "nt" else LINUX_INSTALLER
    say(f"downloading SteamCMD from {url}")
    try:
        data = fetch(url, 120, accept="application/octet-stream")
    except UpdateError as error:
        raise SteamSetupError(f"couldn't download SteamCMD: {error}") from None
    try:
        if os.name == "nt":
            with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                bundle.extractall(target)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
                extract_tar(bundle, target)
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
    if problem := check_username(username):
        return False, problem
    if username.lower() in [a.lower() for a in cached_accounts(executable)]:
        try:
            result = run(executable, ["+login", username, "+quit"], timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "SteamCMD waited for a password: the sign-in isn't cached"
        text = result.stdout
        ok = "Waiting for user info...OK" in text or "Logged in OK" in text
        return ok, text[-300:]
    return False, "no cached sign-in for that account"


def check_username(username: str) -> str | None:
    """Why this can't be a Steam account name, or None. Steam's are 2-64 letters, digits and underscores, so one
    with anything else (& | " ' ;) is refused before it reaches a command line."""
    if USERNAME.fullmatch(username or ""):
        return None
    return (f"{username[:30]!r} isn't a Steam account name (2-64 letters, digits and underscores: the name you sign "
            "in to Steam with, not the profile name)")


def login_window(executable: Path, username: str) -> str:
    """Open SteamCMD's sign-in in a window of its own, where Steam asks for the password and the Guard code."""
    if problem := check_username(username):
        return problem
    if os.name == "nt":
        if any(char in str(executable) for char in '"%'):  # cmd expands %…% even inside quotes
            return f"SteamCMD's path {executable} has a % or a quote, which a cmd window can't take: move SteamCMD"
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
    line = f"{shlex.quote(str(executable))} +login {username} +quit"
    for program, flags in (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]),
                           ("xfce4-terminal", ["-e"]), ("xterm", ["-e"])):
        if shutil.which(program) and os.environ.get("DISPLAY"):
            try:
                args = [program, *flags, "sh", "-c", f"{line}; echo; read -p 'Done: press Enter' x"] \
                    if flags != ["-e"] else [program, "-e", "sh -c " + shlex.quote(f"{line}; read -p Done x")]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return "a Steam sign-in window opened: type the password and the Steam Guard code there"
            except OSError:
                continue
    return f"no desktop terminal found; run this in another terminal, then come back: {line}"


def configure(settings: Settings, executable: Path, username: str) -> list[Path]:
    """Write [workshop] steamcmd and username into every server folder, and [setup] into settings.toml."""
    from .core.tomledit import set_table_key

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
        literal = str(folder).replace("'", "''")  # in PowerShell's single quotes: C:\Users\O'Neil\steamcmd
        script = ("$p = [Environment]::GetEnvironmentVariable('Path', 'User'); "
                  f"if (($p -split ';') -notcontains '{literal}') {{ [Environment]::SetEnvironmentVariable('Path', "
                  f"($p.TrimEnd(';') + ';{literal}'), 'User'); 'added' }} else {{ 'already there' }}")
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


def cannot_fetch(config: Path) -> str | None:
    """Why the server whose dedicated.toml this is can't fetch Workshop items with SteamCMD here, or None. Only what
    the files say: a saved sign-in that Steam has since expired shows when SteamCMD runs (the server's console says
    so, and its line turns red). No SteamCMD named: the Steam app on its PC fetches them, which no file tells."""
    path, username = workshop_settings(config)
    if not path:
        return None
    executable = Path(path)
    if not executable.is_file():
        return f"its SteamCMD ({path}) isn't on this PC"
    if not username:
        return "no Steam account is set for SteamCMD (an anonymous sign-in can't fetch MCC's items)"
    if username.lower() not in [a.lower() for a in cached_accounts(executable)]:
        return f"{username} has no saved SteamCMD sign-in here"
    return None


def depot_folders(executable: Path, depot: int) -> list[Path]:
    """Where SteamCMD puts a depot it downloaded: beside it on Windows, under the Steam folder on Linux."""
    tail = Path("steamapps") / "content" / f"app_{MCC_APP}" / f"depot_{depot}"
    roots = [executable.parent, Path.home() / "Steam", Path.home() / ".steam" / "steam",
             Path.home() / ".local" / "share" / "Steam"]
    return [root / tail for root in roots]


def download_depot(executable: Path, username: str, depot: int, timeout: float = DEPOT_HOURS * 3600) -> Path:
    """One depot of MCC with SteamCMD's download_depot, on the account's saved sign-in (never a password: with none
    saved it fails rather than asks). Returns the folder it's in. Gigabytes: run it in a thread."""
    if problem := check_username(username):
        raise SteamSetupError(problem)
    platform = [] if os.name == "nt" else ["+@sSteamCmdForcePlatformType", "windows"]  # MCC is Windows only
    words = [*NO_PROMPTS, *platform, "+login", username, "+download_depot", str(MCC_APP), str(depot), "+quit"]
    try:
        result = run(executable, words, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SteamSetupError(f"SteamCMD was still downloading after {DEPOT_HOURS:.0f} hours: try again (I)") from None
    text = result.stdout
    lower = text.lower()
    if "login failure" in lower or ("password" in lower and "logged in ok" not in lower):
        raise SteamSetupError(f"SteamCMD couldn't sign {username} in on its saved sign-in: press I to sign in again")
    if any(word in lower for word in ("no subscription", "access denied", "missing decryption key")):
        raise SteamSetupError(f"{username} doesn't own this part of MCC on Steam (depot {depot})")
    match = DEPOT_DONE.search(text)
    found = [Path(match.group(1))] if match else []
    for folder in found + depot_folders(executable, depot):
        try:
            if folder.is_dir() and any(folder.iterdir()):
                return folder
        except OSError:
            continue
    last = " | ".join(line.strip() for line in text.strip().splitlines()[-3:])
    raise SteamSetupError(f"SteamCMD didn't download depot {depot} ({last or 'it said nothing'})")
