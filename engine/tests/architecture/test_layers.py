"""The engine is laid out by lore.py's three named parts (plans/restructure.md): each layer uses only the ones
below it, so the Superintendent never knows Vergil and nothing below a named part imports one. These tests read
every module's imports (at run time: the ones under `if TYPE_CHECKING:` don't count) and fail on one that crosses
the line, naming it."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from testkit import ENGINE

PACKAGE = ENGINE / "huragok"

# What each top-level piece may import at run time (besides itself). Anything not named here may import anything.
BELOW_PERSONAS = {"core", "games", "steam", "rcon", "program", "servers", "players", "content", "updates", "lore"}
ALLOWED: dict[str, set[str]] = {
    "lore": set(),
    "games": {"lore"},
    "core": {"games", "lore"},
    "steam": {"core", "games", "lore"},
    "rcon": {"core", "games", "lore"},
    "program": {"core", "games", "steam", "lore"},
    "servers": {"core", "games", "rcon", "program", "steam", "lore"},
    "players": {"core", "games", "rcon", "program", "steam", "servers", "lore"},
    "updates": {"core", "games", "rcon", "program", "steam", "servers", "players", "lore"},
    "content": {"core", "games", "rcon", "program", "steam", "servers", "players", "updates", "lore"},
    "superintendent": {"core", "games", "rcon", "program", "steam", "servers", "lore"},
    "vergil": {"core", "games", "rcon", "program", "steam", "servers", "players", "superintendent", "lore"},
    "discordbot": BELOW_PERSONAS,  # what happens on the servers, in Discord: uses everything below the named parts
    "parts": BELOW_PERSONAS | {"superintendent", "vergil", "discordbot"},
    "huragok": BELOW_PERSONAS | {"superintendent", "vergil", "parts", "discordbot"},
    "winproc": {"core"},  # the 0.4.x shim
}
# Helpers that import nothing of Huragok's, so any layer may use them (the game modules, under core/, too).
LEAVES = {"core.files", "core.text"}
# Imports a layer makes of one above it on purpose, each with why (reached only inside a function, never at import).
LET_THROUGH: dict[tuple[str, str], str] = {
    ("core.settings", "parts"): "settings.toml's part sections are checked against the parts there are",
    ("core.hostsettings", "parts"): "a changed settings.toml is checked as each part would read it",
    ("core.commandsfile", "parts"): "commands.toml lists every command of every part",
}


def module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def runtime_imports(path: Path) -> list[tuple[int, str]]:
    """(line, dotted module inside huragok) for each import that runs: not those under `if TYPE_CHECKING:`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    here = module_name(path)
    is_package = path.name == "__init__.py"
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            for inner in node.body:
                skipped.update(id(n) for n in ast.walk(inner))
    found = []
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = here.split(".") if here else []
                if not is_package:
                    base = base[:-1]
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                target = ".".join([*base, *([node.module] if node.module else [])])
                names = [target] if node.module else [".".join([*base, a.name]) for a in node.names]
            elif (node.module or "").startswith("huragok"):
                names = [(node.module or "").removeprefix("huragok").lstrip(".")]
            else:
                continue
            found.extend((node.lineno, name) for name in names)
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, a.name.removeprefix("huragok").lstrip("."))
                         for a in node.names if a.name.startswith("huragok"))
    return found


def top(name: str) -> str:
    return name.split(".")[0] if name else ""


def crossings() -> list[str]:
    wrong = []
    for path in sorted(PACKAGE.rglob("*.py")):
        here = module_name(path)
        layer = top(here)
        if layer not in ALLOWED:
            continue
        for line, target in runtime_imports(path):
            other = top(target)
            if other in ("", "__version__") or other == layer or other in ALLOWED[layer]:  # the package: its version
                continue
            if target in LEAVES or any(here == a and (target == b or target.startswith(b + "."))
                                       for a, b in LET_THROUGH):
                continue
            wrong.append(f"{path.relative_to(PACKAGE.parent).as_posix()}:{line}: {here} imports {target}")
    return wrong


def test_each_layer_uses_only_the_ones_below_it() -> None:
    wrong = crossings()
    assert not wrong, "imports across the layers (plans/restructure.md):\n" + "\n".join(wrong)


def test_the_superintendent_never_knows_vergil() -> None:
    for path in [PACKAGE / "superintendent.py", *(PACKAGE / "servers").rglob("*.py")]:
        for line, target in runtime_imports(path):
            assert top(target) not in ("vergil", "players"), f"{path.name}:{line} imports {target}"


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_every_layer_named_here_exists(layer: str) -> None:
    assert (PACKAGE / f"{layer}.py").exists() or (PACKAGE / layer / "__init__.py").exists()


@pytest.mark.parametrize("leaf", sorted(LEAVES))
def test_a_leaf_helper_imports_nothing_of_huragoks(leaf: str) -> None:
    path = PACKAGE.joinpath(*leaf.split(".")).with_suffix(".py")
    assert not runtime_imports(path), f"{leaf} must stay a leaf: every layer imports it"
