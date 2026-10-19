"""Server packages: everything that makes a server *that* server, minus Halo 3's files and the data.

A package is a folder (PACKAGES/<folder>/ at the root; zipped to share):

    package.toml                 name, description, what the [[server]] block gets, Workshop items
    playlists/*.playlist.json    its rotations
    content/Maps, Game Modes,    map variants, game types and vote images the playlists name
    Images, Mods

`package.toml`:

    name = "Infection Night"
    description = "Halo 3 Infection on the classic maps"
    author = "LilGremlin"
    version = 1
    needs_maps = ["Guardian", "The Pit"]     # Halo 3 maps the server must have in its game folder
    needs_campaign = false                   # the campaign missions too (`dedicated init --maps campaign`)
    needs_odst = false                       # Halo 3: ODST's files too (`--maps odst`); a server with
                                             # odst = { … } needs them anyway, and Huragok adds them from MCC

    [server]                                 # goes into the server's [[server]] block
    playlist = "playlists/infection.playlist.json"
    max_players = 16

    [defaults]                               # optional: the folder's [defaults], for every server in it
    voice_proximity = false

    [workshop]
    items = [3014096841]                     # installed when the server is made

Host-specific things stay out of a package: the name, port, passwords, admins,
`public_address`, RCON. Deploying asks for those. The starter packages shipped in
templates/packages are copied to PACKAGES/ on the first start, once; edit them freely.
"""

from __future__ import annotations

import filecmp
import json
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .odst import describe as describe_odst

PACKAGE_FILE = "package.toml"
PACKAGES_FOLDER = "PACKAGES"
CONTENT_FOLDERS = ("Maps", "Game Modes", "Images", "Mods")
HOST_SPECIFIC = {"name", "port", "enabled", "password", "rcon_port", "rcon_password", "admins", "host_name"}
SERVER_KEYS_FIRST = ("playlist", "map", "game", "base_map", "variant", "campaign", "odst", "max_players",
                     "min_players")


class PackageError(Exception):
    pass


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "package"


@dataclass
class Package:
    folder: Path
    name: str
    description: str = ""
    author: str = ""
    version: int = 1
    needs_maps: list[str] = field(default_factory=list)
    needs_campaign: bool = False
    needs_odst: bool = False
    server: dict[str, Any] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    workshop_items: list[int] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.folder.name

    @property
    def playlists(self) -> list[Path]:
        folder = self.folder / "playlists"
        return sorted(folder.glob("*.playlist.json")) if folder.is_dir() else []

    @property
    def content_files(self) -> list[Path]:
        folder = self.folder / "content"
        return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []

    @property
    def plays(self) -> str:
        """What the package's server plays, in a few words."""
        server = self.server
        if "playlist" in server:
            return f"playlist {Path(str(server['playlist'])).name.removesuffix('.playlist.json')}"
        if "odst" in server:
            return describe_odst(server["odst"])
        if "campaign" in server:
            mission = server["campaign"].get("mission") if isinstance(server["campaign"], dict) else ""
            return f"campaign co-op (mission {mission})"
        if "variant" in server:
            return f"variant {Path(str(server['variant'])).stem}"
        if "map" in server or "game" in server:
            return f"{server.get('game', 'Slayer')} on {server.get('map', 'Guardian')}"
        return "Slayer on Guardian (nothing chosen)"

    def summary(self) -> str:
        bits = [self.plays]
        if self.workshop_items:
            bits.append(f"{len(self.workshop_items)} Workshop item(s)")
        if self.needs_campaign:
            bits.append("needs the campaign files")
        if self.needs_odst or "odst" in self.server:
            bits.append("needs Halo 3: ODST's files")
        return "; ".join(bits)


def load_package(folder: Path) -> Package:
    path = folder / PACKAGE_FILE
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise PackageError(f"{folder} has no {PACKAGE_FILE}") from None
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PackageError(f"{path}: {error}") from None
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise PackageError(f"{path}: version must be a whole number, such as 1 (not {version!r})")
    for key in ("name", "description", "author"):
        if not isinstance(data.get(key, ""), str):
            raise PackageError(f"{path}: {key} must be text in quotes")
    for key in ("needs_campaign", "needs_odst"):
        if not isinstance(data.get(key, False), bool):
            raise PackageError(f"{path}: {key} must be true or false")
    server = data.get("server", {})
    if not isinstance(server, dict):
        raise PackageError(f"{path}: [server] must be a table")
    if stray := sorted(set(server) & HOST_SPECIFIC):
        raise PackageError(f"{path}: [server] can't set '{stray[0]}': that's chosen when the server is made")
    workshop = data.get("workshop", {})
    items = workshop.get("items", []) if isinstance(workshop, dict) else []
    if not isinstance(items, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in items):
        raise PackageError(f"{path}: [workshop] items must be a list of Workshop item IDs (numbers)")
    needs = data.get("needs_maps", [])
    if not isinstance(needs, list) or not all(isinstance(m, str) for m in needs):
        raise PackageError(f"{path}: needs_maps must be a list of map names")
    package = Package(
        folder=folder,
        name=str(data.get("name") or folder.name),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        version=version,
        needs_maps=[str(m) for m in needs],
        needs_campaign=bool(data.get("needs_campaign", False)),
        needs_odst=bool(data.get("needs_odst", False)),
        server=dict(server),
        defaults=dict(data.get("defaults", {})) if isinstance(data.get("defaults", {}), dict) else {},
        workshop_items=[int(i) for i in items],
    )
    playlist = package.server.get("playlist")
    if playlist is not None and not (isinstance(playlist, str) and (folder / playlist).is_file()):
        raise PackageError(f"{path}: [server] playlist names {playlist}, which isn't in the package")
    return package


def list_packages(folder: Path) -> list[Package]:
    """Every package in PACKAGES/ (a broken one is listed with its problem as the description)."""
    found: list[Package] = []
    if not folder.is_dir():
        return found
    for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or not (child / PACKAGE_FILE).is_file():
            continue
        try:
            found.append(load_package(child))
        except PackageError as error:
            found.append(Package(child, f"{child.name} (broken)", description=str(error)))
    return found


def find_package(folder: Path, ref: str) -> Package | None:
    """A package by its folder name, its name, or its number in the list (1-based)."""
    packages = list_packages(folder)
    ref = ref.strip()
    if ref.isdigit() and 1 <= int(ref) <= len(packages):
        return packages[int(ref) - 1]
    for package in packages:
        if ref.lower() in (package.key.lower(), package.name.lower()):
            return package
    return None


def install_starters(folder: Path) -> list[str]:
    """Copy the starter packages that aren't in PACKAGES/ yet. Returns the names copied."""
    copied: list[str] = []
    try:
        starters = resources.files("huragok").joinpath("templates/packages")
        names = sorted(child.name for child in starters.iterdir() if child.is_dir())
    except (FileNotFoundError, OSError):
        return copied
    for name in names:
        target = folder / name
        if target.exists():
            continue
        source = starters.joinpath(name)
        with resources.as_file(source) as path:
            shutil.copytree(path, target)
        copied.append(name)
    return copied


def write_package_toml(package: Package) -> Path:
    from .tomledit import _toml_value

    lines = [f"name = {_toml_value(package.name)}"]
    if package.description:
        lines.append(f"description = {_toml_value(package.description)}")
    if package.author:
        lines.append(f"author = {_toml_value(package.author)}")
    lines.append(f"version = {package.version}")
    if package.needs_maps:
        lines.append(f"needs_maps = {_toml_value(package.needs_maps)}")
    if package.needs_campaign:
        lines.append("needs_campaign = true")
    if package.needs_odst:
        lines.append("needs_odst = true")
    lines += ["", "[server]"]
    server = package.server
    for key in [*(k for k in SERVER_KEYS_FIRST if k in server), *(k for k in server if k not in SERVER_KEYS_FIRST)]:
        lines.append(f"{key} = {_toml_value(server[key])}")
    if package.defaults:
        lines += ["", "[defaults]", *(f"{k} = {_toml_value(v)}" for k, v in package.defaults.items())]
    lines += ["", "[workshop]", f"items = {_toml_value(package.workshop_items)}"]
    package.folder.mkdir(parents=True, exist_ok=True)
    path = package.folder / PACKAGE_FILE
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _content_references(playlist: Path) -> tuple[list[str], list[int]]:
    """(content files, Workshop item IDs) a playlist names."""
    files: list[str] = []
    items: list[int] = []
    try:
        data = json.loads(playlist.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return files, items
    for game in data.get("games", []) if isinstance(data, dict) else []:
        if not isinstance(game, dict):
            continue
        for key in ("map", "base_map", "game", "variant"):
            value = str(game.get(key) or "")
            if value.startswith("workshop:"):
                number = value.split(":", 1)[1].split("/", 1)[0]
                if number.isdigit() and int(number) not in items:
                    items.append(int(number))
            elif "/" in value or value.lower().endswith((".json", ".bin", ".mvar")):
                files.append(value.removeprefix("content/"))
        image = str(game.get("preview_image") or "")
        if image:
            files.append(f"Images/{image}")
    return files, items


def save_package(server_folder: Path, server_name: str, dest: Path, *, config_name: str = "dedicated.toml",
                 name: str | None = None, description: str = "", author: str = "") -> Package:
    """A package from a server that exists: its block, its playlist, and the content the playlist names."""
    from .tomledit import read_toml

    config = server_folder / config_name
    try:
        data = read_toml(config)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PackageError(f"{config}: {error}") from None
    block = next((b for b in data.get("server", []) if str(b.get("name", "")).lower() == server_name.lower()), None)
    if block is None:
        raise PackageError(f"{config} has no [[server]] called {server_name}")
    if dest.exists() and any(dest.iterdir()):
        raise PackageError(f"{dest} already exists")
    server = {k: v for k, v in block.items() if k not in HOST_SPECIFIC}
    workshop = data.get("workshop", {})
    items = [int(i) for i in workshop.get("items", []) if isinstance(i, int)] if isinstance(workshop, dict) else []
    content_root = server_folder / str(data.get("content") or "content")
    defaults = data.get("defaults")
    kept = {k: v for k, v in defaults.items() if k not in HOST_SPECIFIC} if isinstance(defaults, dict) else {}
    package = Package(dest, name or server_name, description, author, 1, needs_campaign="campaign" in server,
                      needs_odst="odst" in server, server=server, defaults=kept, workshop_items=items)
    dest.mkdir(parents=True, exist_ok=True)
    if "playlist" in server:
        source = server_folder / str(server["playlist"])
        if not source.is_file():
            raise PackageError(f"{server_name}'s playlist {source} isn't there")
        target = dest / "playlists" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        package.server["playlist"] = f"playlists/{source.name}"
        files, referenced = _content_references(source)
        for item in referenced:
            if item not in package.workshop_items:
                package.workshop_items.append(item)
        for relative in files:
            path = content_root / relative
            if path.is_file():
                copy = dest / "content" / relative
                copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, copy)
    for key in ("variant", "game", "map"):
        value = str(server.get(key) or "")
        # `variant` is relative to the server's folder ("content/Game Modes/x.json"); a `map` or `game` file is
        # named inside its content folder ("Game Modes/snipers.bin")
        path = ((server_folder if key == "variant" else content_root) / value).resolve() if value else None
        if path and path.is_file() and content_root.resolve() in path.parents:
            relative = path.relative_to(content_root.resolve())
            copy = dest / "content" / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy)
            if key == "variant":  # inside the package's content/; applying it points at the server's own
                package.server["variant"] = f"content/{relative.as_posix()}"
    write_package_toml(package)
    return load_package(dest)


def apply_package(package: Package, server_folder: Path, content_root: Path,
                  kept: list[str] | None = None) -> list[str]:
    """Copy a package's playlists and content into a server folder. Returns what was copied. A content file that's
    in the content folder already (the root's shared one: other servers use it) isn't replaced: when it differs,
    `kept` gets its name."""
    copied: list[str] = []
    for playlist in package.playlists:
        target = server_folder / "playlists" / playlist.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(playlist, target)
        copied.append(f"playlists/{playlist.name}")
    content = package.folder / "content"
    for file in package.content_files:
        relative = file.relative_to(content)
        target = content_root / relative
        if target.exists():
            if kept is not None and not (target.is_file() and filecmp.cmp(file, target, shallow=False)):
                kept.append(f"content/{relative.as_posix()}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
        copied.append(f"content/{relative.as_posix()}")
    return copied


def server_values(package: Package, server_folder: Path, content_root: Path) -> dict[str, Any]:
    """What the package puts in a server's [[server]] block, with a saved custom game (`variant`, kept in the
    package as "content/…") pointing into that server's own content folder, the root's shared one too."""
    values = dict(package.server)
    variant = values.get("variant")
    if isinstance(variant, str) and variant.startswith("content/"):
        from .newserver import relative_posix

        values["variant"] = relative_posix(content_root / variant.removeprefix("content/"), server_folder)
    return values
