"""Every module says what it's for and exports only what it has, and the tests are laid out like the package."""

from __future__ import annotations

import ast
import importlib
import pkgutil

from testkit import ENGINE, TESTS

PACKAGE = ENGINE / "huragok"
MODULES = sorted(m.name for m in pkgutil.walk_packages([str(PACKAGE)], "huragok.") if not m.name.endswith("__main__"))


def path_of(module: str):
    parts = module.split(".")[1:]
    folder = PACKAGE.joinpath(*parts)
    return folder / "__init__.py" if folder.is_dir() else folder.with_suffix(".py")


def test_every_module_says_what_its_for() -> None:
    """Its first line is what someone opening the file reads first: what it's for, in the host's words."""
    silent = [m for m in MODULES if not ast.get_docstring(ast.parse(path_of(m).read_text(encoding="utf-8")))]
    assert silent == [], f"no docstring: {silent}"


def test_every_name_a_module_exports_is_there() -> None:
    """A name left in __all__ after its code moved breaks `from module import *` and misleads whoever reads it."""
    missing = [f"{m}.{name}" for m in MODULES for name in getattr(importlib.import_module(m), "__all__", [])
               if not hasattr(importlib.import_module(m), name)]
    assert missing == []


def test_the_tests_are_laid_out_like_the_package() -> None:
    """A folder of tests per layer of the package (tests/README.md), besides the tests' own."""
    own = {"architecture", "testkit", "data"}
    folders = {p.name for p in TESTS.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))}
    layers = {p.name for p in PACKAGE.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert folders - own <= layers, f"not a layer of the package: {sorted(folders - own - layers)}"
