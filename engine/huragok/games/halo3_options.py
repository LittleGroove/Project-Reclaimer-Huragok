"""Game options: what a game type plays with, as data the window can show as labels instead of numbers.

Three places take them, the same keys in each (plans/game-variants.md has the research):
- a saved custom game, a preset .json in content/Game Modes: `mode` + `rules` + `options` + `world`, numbers
  only in `options`; `rules` must be there, and anything it leaves out plays as the game type has it;
- a playlist game: the same `rules`, `options` and `world` layered on that one game (no file); `options`
  take the label as well ("Battle rifle"), which keeps the playlist readable, so labels are written there;
- the catalogue in halo3_options_data.py: every key, its label, its range and the meaning of every number,
  generated from the program's own answers.

Tested against the 0.9.18 program on 2026-10-18: `rules: {}` is valid; `mode` must be an identifier
(`CaptureTheFlag`, not "Capture the Flag"); `mode-check` only checks a preset with a script, so a plain one
is checked as a copy with an empty script added. Re-probed against 0.9.20: the same keys and ranges, and the
character set's "Vanilla characters" called "Halo 3" since 0.9.19 (LABEL_CHANGES). A playlist must name a choice
as its own program calls it: 0.9.18 refuses "Halo 3" ("Character set has no choice 'Halo 3'") and 0.9.19 on refuse
"Vanilla characters". Every version takes the number (0.9.18 checked on 2026-10-18; 0.9.20's probe is numbers), so
the playlist editor writes those choices as numbers, and playlists.rename_old_labels puts a file's labels as the
server's own program calls them. "On, with firebombs" is what 0.9.15, 0.9.18 and 0.9.20 all call it; the "On with
firebombs" some files have is refused by each of them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.files import write_atomic
from ..core.text import slug
from .halo3_options_data import MODE_RULES, SHARED, TEAM_CHARACTER, TEAM_CHARACTER_SET, TEAM_COLOURS, TRAITS

GAME_TYPES: list[tuple[str, str]] = [  # (the name playlists and the dashboard use, the preset's `mode`)
    ("Slayer", "Slayer"), ("Capture the Flag", "CaptureTheFlag"), ("Oddball", "Oddball"),
    ("King of the Hill", "KingOfTheHill"), ("Assault", "Assault"), ("Infection", "Infection"),
    ("Territories", "Territories"), ("Juggernaut", "Juggernaut"), ("VIP", "Vip"), ("Escalation Slayer", "EscalationSlayer"),
]
_MODE_IDS = {**{name.lower(): mode for name, mode in GAME_TYPES}, **{mode.lower(): mode for _, mode in GAME_TYPES}}
_MODE_NAMES = {mode: name for name, mode in GAME_TYPES}


def version_tuple(text: str | None) -> tuple[int, ...] | None:
    """'0.9.19' or 'v0.9.20-beta' -> (0, 9, 19); None when it isn't a version."""
    match = re.match(r"v?(\d+(?:\.\d+)*)", str(text or "").strip())
    return tuple(int(n) for n in match.group(1).split(".")) if match else None


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
# Read either label, show the new one (the catalogue is 0.9.20's). Lowercase old -> new.
RENAMED_LABELS = {change.old.lower(): change.new for change in LABEL_CHANGES}
NEWEST = "9999"  # a program version newer than any: "as the newest program calls it"

EMPTY_SCRIPT = {"version": 1, "name": "check", "initial": [], "on_spawn": [], "on_kill": [], "on_death": []}
MAX_NAME = 48

TRAIT_SETS: list[tuple[str, str, tuple[str, ...]]] = [  # (key, who gets the traits, game types; () = every one)
    ("player", "every player, the whole game", ()),
    ("respawn_traits", "for a moment after respawning (Respawn traits duration)", ()),
    ("red_powerup", "while a red powerup lasts", ()),
    ("blue_powerup", "while a blue powerup lasts", ()),
    ("yellow_powerup", "while a yellow powerup lasts", ()),
    ("leader", "whoever leads the score", ("Slayer",)),
    ("flag_carrier", "while carrying the flag", ("CaptureTheFlag",)),
    ("ball_carrier", "while holding the ball", ("Oddball",)),
    ("hill_occupant", "while inside the hill", ("KingOfTheHill",)),
    ("vip", "the VIP", ("Vip",)),
    ("vip_team", "the VIP's team", ("Vip",)),
    ("vip_proximity", "players near their VIP (VIP influence radius)", ("Vip",)),
    ("juggernaut", "the Juggernaut", ("Juggernaut",)),
    ("defender", "the defending team", ("Territories",)),
    ("attacker", "the attacking team", ("Territories",)),
    ("bomb_carrier", "while carrying the bomb", ("Assault",)),
    ("bomb_arming", "while arming the bomb", ("Assault",)),
    ("infected", "infected players", ("Infection",)),
    ("initial_infected", "the players who start infected", ("Infection",)),
    ("safe_haven", "inside a safe haven", ("Infection",)),
    ("last_survivor", "the last uninfected player", ("Infection",)),
]
TRAIT_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Shields & health", ("resistance", "recharge", "vampirism", "headshots", "shields")),
    ("Weapons & damage", ("primary_weapon", "secondary_weapon", "damage", "grenades", "grenade_regeneration", "ammo",
                          "pickup")),
    ("Movement", ("speed", "gravity", "vehicles")),
    ("Sensors", ("radar", "radar_range")),
    ("Appearance", ("camouflage", "waypoint", "aura", "forced_color", "character_set", "character", "camera")),
]
NO_LOADOUT = {"red_powerup", "blue_powerup", "yellow_powerup", "flag_carrier", "ball_carrier", "bomb_carrier"}
LOADOUT = {"primary_weapon", "secondary_weapon", "grenades"}  # do nothing in powerup and carrier sets
RULE_LABELS = {"Kill points", "Death points", "Suicide points", "Betrayal points"}  # mode.* twins of `rules`


@dataclass(frozen=True)
class Option:
    key: str  # rounds.minutes, player.speed, mode.flag_type, rules.score_to_win, world.sprint
    label: str
    low: int = 0
    high: int = 0
    choices: tuple[tuple[Any, str], ...] | None = None
    category: str = ""
    group: str = ""

    @property
    def where(self) -> str:
        """The block it's written in: options, rules or world."""
        return self.key.split(".", 1)[0] if self.key.startswith(("rules.", "world.")) else "options"

    @property
    def name(self) -> str:
        """Its key inside that block: score_to_win for rules.score_to_win, the full key for options."""
        return self.key.split(".", 1)[1] if self.where != "options" else self.key

    def label_of(self, value: Any) -> str:
        if self.choices:
            value = RENAMED_LABELS.get(str(value).strip().lower(), value)
            for number, label in self.choices:
                if number == value or str(label).lower() == str(value).lower():
                    return label
        return "?" if value is None else str(value)

    def value_of(self, text: Any) -> Any:
        """A number (or a choice's label, any case) as the value it stands for. Raises ValueError."""
        if self.choices:
            text = RENAMED_LABELS.get(str(text).strip().lower(), text)
            for number, label in self.choices:
                if str(text).strip().lower() in (str(label).lower(), str(number)):
                    return number
            raise ValueError(f"{self.label}: one of " + ", ".join(str(label) for _, label in self.choices))
        try:
            value = int(str(text).strip())
        except ValueError:
            raise ValueError(f"{self.label}: a whole number") from None
        if not self.low <= value <= self.high:
            raise ValueError(f"{self.label}: {self.low} to {self.high}")
        return value


def _option(row: tuple[Any, ...], category: str, prefix: str = "", group: str = "") -> Option:
    key, label, low, high, choices = row
    return Option(f"{prefix}{key}", label, low, high, choices, category, group)


SHARED_OPTIONS = [_option(row, {"rounds": "Rounds & teams", "respawn": "Respawn", "powerups": "Powerup durations",
                                "map": "Map & equipment"}[row[0].split(".")[0]]) for row in SHARED]
RULES = [
    Option("rules.score_to_win", "Score to win (0 = none, -1 = unlimited)", -1, 500, None, "rules"),
    Option("rules.kill_points", "Kill points", -10, 10, None, "rules"),
    Option("rules.death_points", "Death points", -10, 10, None, "rules"),
    Option("rules.suicide_points", "Suicide points", -10, 10, None, "rules"),
    Option("rules.betrayal_points", "Betrayal points", -10, 10, None, "rules"),
]
WORLD = [
    Option("world.sprint", "Sprint", choices=(("off", "off"), ("unlimited", "unlimited"), ("stamina", "stamina")),
           category="World"),
    Option("world.sprint_speed_percent", "Sprint speed (% of walking; 150 by default)", 110, 200, None, "World"),
    Option("world.sprint_stamina_seconds", "Sprint stamina (seconds; 5 by default)", 1, 30, None, "World"),
]


def mode_id(name: str) -> str | None:
    """'Capture the Flag' or 'CaptureTheFlag' -> 'CaptureTheFlag'; None for anything else."""
    return _MODE_IDS.get(str(name).strip().lower())


def mode_name(mode: str) -> str:
    return _MODE_NAMES.get(mode, mode)


def trait_sets(mode: str | None) -> list[tuple[str, str]]:
    """The trait sets a game type has: (key, who gets them). Unknown game type: the ones every type has."""
    return [(key, who) for key, who, modes in TRAIT_SETS if not modes or (mode is not None and mode in modes)]


def trait_options(trait_set: str) -> list[Option]:
    group_of = {key: group for group, keys in TRAIT_GROUPS for key in keys}
    options = []
    for row in TRAITS:
        if trait_set in NO_LOADOUT and row[0] in LOADOUT:
            continue
        options.append(_option(row, "Traits", f"{trait_set}.", group_of.get(row[0], "")))
    order = [key for _, keys in TRAIT_GROUPS for key in keys]
    return sorted(options, key=lambda o: order.index(o.key.split(".", 1)[1]) if o.key.split(".", 1)[1] in order else 99)


def team_options() -> list[Option]:
    options = []
    for colour in TEAM_COLOURS:
        options.append(Option(f"teams.{colour}_character_set", f"{colour.title()} team's character set",
                              TEAM_CHARACTER_SET[0], TEAM_CHARACTER_SET[1], TEAM_CHARACTER_SET[2], "Team characters"))
        options.append(Option(f"teams.{colour}_character", f"{colour.title()} team's character",
                              TEAM_CHARACTER[0], TEAM_CHARACTER[1], TEAM_CHARACTER[2], "Team characters"))
    return options


def mode_rules(mode: str | None) -> list[Option]:
    title = f"{mode_name(mode)} rules" if mode else "Rules"
    rows = [_option(row, title) for row in MODE_RULES.get(mode or "", []) if row[1] not in RULE_LABELS]
    return [Option(o.key, o.label, o.low, o.high, o.choices, title) for o in RULES] + rows


def categories(mode: str | None) -> list[tuple[str, list[Option]]]:
    """The game type's options as the client groups them; Traits and Team characters are opened by set."""
    rules = mode_rules(mode)
    grouped: dict[str, list[Option]] = {}
    for option in SHARED_OPTIONS:
        grouped.setdefault(option.category, []).append(option)
    return [(rules[0].category, rules), *grouped.items(), ("World", WORLD)]


def all_options(mode: str | None) -> dict[str, Option]:
    """Every option of a game type, by key (traits of every set it has, team characters too)."""
    found = {o.key: o for _, options in categories(mode) for o in options}
    for trait_set, _ in trait_sets(mode):
        found.update({o.key: o for o in trait_options(trait_set)})
    found.update({o.key: o for o in team_options()})
    return found


def describe(mode: str | None, options: dict[str, Any], rules: dict[str, Any] | None = None,
             world: dict[str, Any] | None = None) -> list[str]:
    """What a set of overrides changes, in words: 'Weapon set: No weapons', 'Score to win: 50'."""
    known = all_options(mode)
    lines = []
    for key, value in (rules or {}).items():
        option = known.get(f"rules.{key}")
        lines.append(f"{option.label if option else key}: {value}")
    for key, value in options.items():
        option = known.get(key)
        if option is None:
            lines.append(f"{key}: {value} (not an option of {mode_name(mode) if mode else 'this game type'})")
            continue
        who = ""
        if key.count(".") and key.split(".", 1)[0] in {k for k, _, _ in TRAIT_SETS} and not key.startswith("player."):
            who = f" ({key.split('.', 1)[0].replace('_', ' ')})"
        lines.append(f"{option.label}{who}: {option.label_of(value)}")
    for key, value in (world or {}).items():
        option = known.get(f"world.{key}")
        lines.append(f"{option.label if option else key}: {value}")
    return lines


# Presets (saved custom games) ----------------------------------------------------------------------

class PresetError(Exception):
    pass


@dataclass
class Preset:
    path: Path | None
    data: dict[str, Any] = field(default_factory=dict)  # the whole file: fields not edited here stay as they were

    @property
    def name(self) -> str:
        return str(self.data.get("name") or "")

    @property
    def mode(self) -> str:
        return str(self.data.get("mode") or "Slayer")

    @property
    def options(self) -> dict[str, Any]:
        return self.data.setdefault("options", {})

    @property
    def rules(self) -> dict[str, Any]:
        return self.data.setdefault("rules", {})

    @property
    def world(self) -> dict[str, Any]:
        world = self.data.get("world")
        if not isinstance(world, dict):
            world = self.data["world"] = {}
        return world

    @property
    def scripted(self) -> bool:
        return bool(self.data.get("script"))

    def get(self, option: Option) -> Any:
        block = {"options": self.options, "rules": self.rules, "world": self.world}[option.where]
        return block.get(option.name)

    def set(self, option: Option, value: Any) -> None:
        """None takes the override out (the game type's own value plays)."""
        block = {"options": self.options, "rules": self.rules, "world": self.world}[option.where]
        if value is None:
            block.pop(option.name, None)
        else:
            block[option.name] = option.value_of(value) if option.where == "options" else value

    def problems(self) -> list[str]:
        found = []
        if not self.name.strip():
            found.append("it needs a name")
        elif len(self.name) > MAX_NAME:
            found.append(f"its name is {len(self.name)} characters; {MAX_NAME} at most")
        if mode_name(self.mode) == self.name.strip() or mode_id(self.name) is not None:
            found.append(f"{self.name!r} is a game type's own name: give it one of its own")
        known = all_options(self.mode)
        for key, value in self.options.items():
            option = known.get(key)
            if option is None:
                found.append(f"{key} isn't an option of {mode_name(self.mode)}")
                continue
            try:
                option.value_of(value)
            except ValueError as error:
                found.append(str(error))
        return found

    def file_data(self) -> dict[str, Any]:
        data = dict(self.data)
        data.setdefault("version", 1)
        data.setdefault("rules", {})
        if not data.get("world"):
            data.pop("world", None)
        return data

    def save(self, path: Path | None = None) -> Path:
        """Written whole into a new file and then moved into place: a hard-linked content folder's copies elsewhere
        keep their own version."""
        path = path or self.path
        if path is None:
            raise PresetError("no file to save it in")
        if problems := self.problems():
            raise PresetError("; ".join(problems))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(self.file_data(), indent=2, ensure_ascii=False) + "\n")
        self.path = path
        return path

    def check_copy(self, folder: Path) -> Path:
        """A copy `mode-check` can check: the program only checks presets with a script, so add an empty one."""
        data = self.file_data()
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

    @property
    def mode_label(self) -> str:
        return mode_name(self.mode) + (" · scripted" if self.scripted else "")


def new_preset(name: str, mode: str) -> Preset:
    return Preset(None, {"version": 1, "name": name, "mode": mode_id(mode) or mode, "rules": {}, "options": {}})


def load_preset(path: Path) -> Preset:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PresetError(f"{path.name}: {error}") from None
    if not isinstance(data, dict) or "mode" not in data:
        raise PresetError(f"{path.name} isn't a saved custom game (no mode)")
    return Preset(path, data)


def clone(preset: Preset, name: str) -> Preset:
    data = json.loads(json.dumps(preset.data))
    data["name"] = name
    return Preset(None, data)


def list_presets(content_dir: Path | None) -> list[PresetInfo]:
    """The saved custom games (.json) in a content folder's Game Modes, the shipped scripted ones too."""
    if content_dir is None or not (content_dir / "Game Modes").is_dir():
        return []
    found = []
    for path in sorted((content_dir / "Game Modes").rglob("*.json"), key=lambda p: str(p).lower()):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "mode" not in data:
            continue
        found.append(PresetInfo(path, str(data.get("name") or path.stem), str(data.get("mode")),
                                path.relative_to(content_dir).as_posix(), bool(data.get("script"))))
    return found


def preset_path(content_dir: Path, name: str) -> Path:
    """Where a new custom game goes: content/Game Modes/<slug>.json, never over an existing file."""
    base = content_dir / "Game Modes" / f"{slug(name, 'custom-game')}.json"
    path, number = base, 2
    while path.exists():
        path = base.with_name(f"{slug(name, 'custom-game')}-{number}.json")
        number += 1
    return path


# Playlist games --------------------------------------------------------------------------------------

def entry_value(option: Option, value: Any) -> Any:
    """How a playlist game stores it: a choice as its number, which every program version takes the same (a label
    reads well but breaks when a version renames it: "Vanilla characters" became "Halo 3" in 0.9.19, and 0.9.18
    refuses the new one). The window shows the label (label_of)."""
    if option.where == "options" and option.choices:
        return option.value_of(value)
    return value


def choices_as_numbers(game: dict[str, Any], mode: str | None = None) -> int:
    """A playlist game's option choices written as labels ("Battle rifle", by hand or by an older Huragok) turned
    into their numbers, as Huragok writes them now. A label the catalogue doesn't know stays as it is. Returns how
    many changed."""
    options = game.get("options")
    if not isinstance(options, dict):
        return 0
    catalogue = all_options(mode or entry_mode(game))
    changed = 0
    for key, value in list(options.items()):
        option = catalogue.get(key)
        if option is None or not option.choices or not isinstance(value, str):
            continue
        try:
            options[key] = option.value_of(value)
        except ValueError:
            continue
        changed += 1
    return changed


def entry_get(game: dict[str, Any], option: Option) -> Any:
    block = game.get(option.where if option.where != "options" else "options") or {}
    return block.get(option.name)


def entry_set(game: dict[str, Any], option: Option, value: Any) -> None:
    """Set (or with None take out) one override on a playlist game; an empty block is taken out too."""
    where = option.where
    block = dict(game.get(where) or {})
    if value is None:
        block.pop(option.name, None)
    else:
        block[option.name] = entry_value(option, value)
    if block:
        game[where] = block
    else:
        game.pop(where, None)


def entry_mode(game: dict[str, Any], base_of: dict[str, str] | None = None) -> str | None:
    """The game type a playlist game plays: its own name, or the base the catalogue knows for a variant."""
    name = str(game.get("game") or "")
    return mode_id(name) or mode_id((base_of or {}).get(name.lower(), ""))
