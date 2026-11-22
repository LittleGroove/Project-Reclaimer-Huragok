"""Saved custom games (game modes) and the option labels Project Reclaimer has renamed: what Huragok reads of them.

Huragok no longer makes or edits custom games, playlists or their game options (0.7.0: a server's files are the
host's). What's left here only reads:
- a saved custom game (game mode) in content/Game Modes: a preset .json (`mode` + `rules` + `options` + `world`), a
  scripted .rmeg (its TOML header holds the same fields: games/gamemodes.py) or an MCC .bin game variant. It's listed
  (list_presets), shown as it's written (describe), checked by the server's own program (`variant check`, through
  check_copy: a .json as a copy with an empty script added, as 0.9's `mode-check` only checked one with a script; a
  .rmeg as it is), copied to another server and set aside when the host asks (window/screens/games.py);
- the labels a program version takes for a choice (LABEL_CHANGES): 0.9.19 renamed "Vanilla characters" to "Halo 3",
  so 0.9.18 refuses "Halo 3" and 0.9.19 on refuse "Vanilla characters". `start.cmd check` warns of a playlist that
  names one its server's program refuses (games/halo3.py old_labels). "On, with firebombs" is what every version
  calls it; the "On with firebombs" some files have is refused by each of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# version_tuple lives in core/text.py now; still importable from here for the modules that haven't moved over yet
# (content/newserver.py, program/settings.py).
from ..core.text import version_tuple

GAME_TYPES: list[tuple[str, str]] = [  # (the name playlists and the dashboard use, the preset's `mode`)
    ("Slayer", "Slayer"), ("Capture the Flag", "CaptureTheFlag"), ("Oddball", "Oddball"),
    ("King of the Hill", "KingOfTheHill"), ("Assault", "Assault"), ("Infection", "Infection"),
    ("Territories", "Territories"), ("Juggernaut", "Juggernaut"), ("VIP", "Vip"), ("Escalation Slayer", "EscalationSlayer"),
]
_MODE_NAMES = {mode: name for name, mode in GAME_TYPES}


@dataclass(frozen=True)
class LabelChange:
    """A choice label that isn't the same in every program version."""

    option: str  # the key's last part: character_set is player.character_set and teams.red_character_set too
    old: str
    new: str
    since: str | None  # the first version calling it `new`; None: every version seen does (the old was never taken)

    def applies_to(self, key: str) -> bool:
        last = key.rsplit(".", 1)[-1]
        return last == self.option or last.endswith("_" + self.option)

    def taken(self, program_version: str | None) -> str | None:
        """The label a program of this version takes; None when that depends on the version and it isn't known."""
        if self.since is None:
            return self.new
        have = version_tuple(program_version)
        if have is None:
            return None
        return self.new if have >= (version_tuple(self.since) or ()) else self.old


LABEL_CHANGES = (
    LabelChange("character_set", "Vanilla characters", "Halo 3", "0.9.19"),
    LabelChange("grenade_regeneration", "On with firebombs", "On, with firebombs", None),
)
NEWEST = "9999"  # a program version newer than any: "as the newest program calls it"

EMPTY_SCRIPT = {"version": 1, "name": "check", "initial": [], "on_spawn": [], "on_kill": [], "on_death": []}


def mode_name(mode: str) -> str:
    return _MODE_NAMES.get(mode, mode)


def _shown(value: Any) -> str:
    """A value as the file writes it: a curve or a list as its JSON, the rest as it is."""
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)


def describe(options: dict[str, Any], rules: dict[str, Any] | None = None,
             world: dict[str, Any] | None = None) -> list[str]:
    """What a set of overrides changes, as the file writes them: 'rules.score_to_win: 50', 'player.speed: 3',
    'world.sprint: unlimited'. Read as written: the program's own check (`variant check`) says what it makes of them."""
    lines = [f"rules.{key}: {_shown(value)}" for key, value in (rules or {}).items()]
    lines += [f"{key}: {_shown(value)}" for key, value in (options or {}).items()]
    lines += [f"world.{key}: {_shown(value)}" for key, value in (world or {}).items()]
    return lines


# Presets (saved custom games) ----------------------------------------------------------------------

class PresetError(Exception):
    pass


@dataclass
class Preset:
    path: Path | None
    data: dict[str, Any] = field(default_factory=dict)  # the whole file, as it was read (a .rmeg: its header)
    kind: str = "json"  # json, rmeg (a scripted game mode: its header), bin (an MCC game variant: nothing read)

    @property
    def name(self) -> str:
        return str(self.data.get("name") or "")

    @property
    def mode(self) -> str:
        return str(self.data.get("mode") or "Slayer")

    @property
    def options(self) -> dict[str, Any]:
        options = self.data.get("options")
        return options if isinstance(options, dict) else {}

    @property
    def rules(self) -> dict[str, Any]:
        rules = self.data.get("rules")
        return rules if isinstance(rules, dict) else {}

    @property
    def world(self) -> dict[str, Any]:
        world = self.data.get("world")
        return world if isinstance(world, dict) else {}

    @property
    def scripted(self) -> bool:
        return self.kind == "rmeg" or bool(self.data.get("script"))

    def check_copy(self, folder: Path) -> Path:
        """A copy `mode-check` can check, in `folder` (a temporary one): a .rmeg as it is; a .json with an empty script
        added (0.9's program only checked presets with a script). The host's file isn't touched. PresetError for a
        .bin: `mode-check` reads custom games, not MCC's variants (the server's own check reads those)."""
        if self.kind == "bin":
            raise PresetError("an MCC game variant (.bin): mode-check doesn't read those; the server's own check "
                              "(G → 5) says whether it reads it")
        if self.kind == "rmeg" and self.path is not None:
            path = folder / self.path.name
            path.write_bytes(self.path.read_bytes())
            return path
        data = dict(self.data)
        data.setdefault("version", 1)
        data.setdefault("rules", {})
        if not data.get("world"):
            data.pop("world", None)
        data.setdefault("script", EMPTY_SCRIPT)
        path = folder / "huragok-check.json"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return path


@dataclass
class PresetInfo:
    path: Path
    name: str
    mode: str
    relative: str  # inside content/
    scripted: bool
    kind: str = "json"  # json, rmeg (a scripted game mode), bin (an MCC game variant)
    problem: str = ""  # why Huragok can't read it as it is (a .rmeg's header): the program's check says more

    @property
    def mode_label(self) -> str:
        if self.kind == "bin":
            return "MCC game variant (.bin)"
        if self.kind == "rmeg":
            return (f"{mode_name(self.mode)} · " if self.mode else "") + "scripted mode (.rmeg)" + (
                f" · {self.problem}" if self.problem else "")
        return mode_name(self.mode) + (" · scripted" if self.scripted else "")


def load_preset(path: Path) -> Preset:
    """A saved custom game as it's written: a .json's whole file, a .rmeg's header (no header: Slayer, named after its
    file, as the program plays it), a .bin's name only (MCC's own format, not read here). Raises PresetError."""
    from .gamemodes import SCRIPTED, rmeg_header

    suffix = path.suffix.lower()
    if suffix in SCRIPTED:
        try:
            header = rmeg_header(path.read_bytes().decode("utf-8-sig", errors="replace"))
        except (OSError, ValueError) as error:
            raise PresetError(f"{path.name}: {error}") from None
        data = dict(header or {})
        data.setdefault("name", path.stem)
        data.setdefault("mode", "Slayer")
        return Preset(path, data, "rmeg")
    if suffix == ".bin":
        return Preset(path, {"name": path.stem, "mode": ""}, "bin")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PresetError(f"{path.name}: {error}") from None
    if not isinstance(data, dict) or "mode" not in data:
        raise PresetError(f"{path.name} isn't a saved custom game (no mode)")
    return Preset(path, data)


def list_presets(content_dir: Path | None) -> list[PresetInfo]:
    """The saved custom games in a content folder's Game Modes, subfolders and the shipped scripted ones too: .json
    game types, .rmeg scripted modes (named by their header) and MCC's .bin variants (games/gamemodes.py). A .json
    that isn't a game type (no mode, or not JSON) isn't listed: the program skips it and its check says why."""
    from .gamemodes import list_game_modes

    return [PresetInfo(m.path, m.name, m.mode, m.relative, m.scripted, m.kind, m.problem)
            for m in list_game_modes(content_dir) if m.kind != "json" or not m.problem]


def mode_check_problem(result: Any) -> str | None:
    """What the program's `mode-check` says is wrong with a file, or None when it reads it. 0.10.8 answers 0 for good
    and bad files alike (read its text): "Valid custom mode: …" or "Preset has no custom game script" (a plain game
    type that read fine; `[modes] …` lines before it are only notes) is good, anything else its reason. 0.9 says
    "Error: …" and fails."""
    lines = [line.strip() for line in str(getattr(result, "text", "") or "").splitlines() if line.strip()]
    if any(line.startswith("Valid custom mode") for line in lines):
        return None
    if getattr(result, "ok", False) and any(line == "Preset has no custom game script" for line in lines):
        return None
    if not getattr(result, "ok", False):
        return str(getattr(result, "problem", "") or "mode-check failed")
    said = [line for line in lines if not line.startswith("[modes]")]
    return said[0] if said else None


__all__ = ["EMPTY_SCRIPT", "GAME_TYPES", "LABEL_CHANGES", "LabelChange", "NEWEST", "Preset", "PresetError",
           "PresetInfo", "describe", "list_presets", "load_preset", "mode_check_problem", "mode_name",
           "version_tuple"]
