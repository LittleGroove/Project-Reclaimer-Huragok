"""What Huragok 0.4.0 to 0.4.4 ask of the version that updates them, kept as they ask it.

The host's live Huragok installs an update and then keeps running on its own code for a while: it waits for any
server part way through a restart, and its start loop (the process start.cmd runs) stays on the old code for good,
starting the new version and taking it back out if it doesn't start. Whatever that old code reads from disk after the
swap comes from this version: the modules it imports late, the command it starts Huragok with, the files it writes
and reads in records/, the exit codes. Each is pinned here, as 0.4.x spells it.
"""

from __future__ import annotations

import importlib.util
import json
import pkgutil
import sys
import types
from pathlib import Path

from testkit import ENGINE
from testkit.releases import NEW, make_install, make_release_zip

import huragok
from huragok import winproc as old_winproc
from huragok.commandline import cli, common, launcher
from huragok.core import lock, mailbox, winproc
from huragok.core.settings import LIVE_FILE
from huragok.updates import engine


def test_what_0_4_imports_from_huragok_winproc_is_still_there() -> None:
    """0.4.x imports huragok.winproc for the first time when it next stops a server on Windows, which can be after
    the swap: the same names, the very same things as core/winproc.py's."""
    for name in ("CREATE_BREAKAWAY_FROM_JOB", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW", "CTRL_BREAK_EVENT",
                 "DETACHED_PROCESS", "ERROR_ACCESS_DENIED", "creation_flags", "ctrl_break",
                 "ctrl_break_helper_command", "send_ctrl_break"):
        assert getattr(old_winproc, name) is getattr(winproc, name), name


def test_huragok_winproc_loads_in_a_0_4_process(monkeypatch) -> None:
    """In the 0.4.x process that imports it after the swap, `huragok.core` is 0.4.x's own core.py: a module, not
    this version's package, so `huragok.core.winproc` can't be imported there. The shim still gives every name."""
    monkeypatch.setitem(sys.modules, "huragok.core", types.ModuleType("huragok.core"))  # no __path__: not a package
    spec = importlib.util.spec_from_file_location("huragok.winproc", ENGINE / "huragok" / "winproc.py")
    assert spec is not None and spec.loader is not None
    shim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shim)
    for name in ("CREATE_BREAKAWAY_FROM_JOB", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW", "CTRL_BREAK_EVENT",
                 "DETACHED_PROCESS", "ERROR_ACCESS_DENIED"):
        assert getattr(shim, name) == getattr(winproc, name), name
    assert shim.creation_flags(breakaway=True) == winproc.creation_flags(breakaway=True)
    assert shim.ctrl_break_helper_command(1234) == winproc.ctrl_break_helper_command(1234)
    assert callable(shim.ctrl_break) and callable(shim.send_ctrl_break)


def test_the_old_start_loop_can_start_this_version() -> None:
    """0.4.x's start loop runs `python -m huragok -c <settings> start --child [--verbose] [--no-menu]`, and its
    winproc a `_ctrl-break <pid>`."""
    args = cli.build_parser().parse_args(["-c", "settings.toml", "start", "--child", "--verbose", "--no-menu"])
    assert (args.command, args.config, args.child, args.verbose, args.no_menu) == (
        "start", "settings.toml", True, True, True)
    assert cli.build_parser().parse_args(["_ctrl-break", "1234"]).pid == 1234
    assert (ENGINE / "huragok" / "__main__.py").exists()


def test_the_version_is_read_from_its_own_line() -> None:
    """Every version finds the one on disk (and in a download) by this line of huragok/__init__.py."""
    text = (ENGINE / "huragok" / "__init__.py").read_text(encoding="utf-8")
    found = engine.VERSION_LINE.search(text)
    assert found is not None and found.group(1) == huragok.__version__


def test_exit_codes_mean_what_they_meant() -> None:
    """The old start loop decides what to do by Huragok's exit code: 10 is "a new version is installed, run it"."""
    assert (common.FAILED, common.SETTINGS_PROBLEM, common.SET_UP, common.ALREADY_RUNNING, common.NOT_RUNNING,
            common.RELAUNCH) == (1, 2, 3, 4, 5, 10)


def test_the_files_in_records_keep_their_names() -> None:
    assert (lock.LOCK_FILE, lock.OLD_LOCK_FILE) == ("huragok.pid", "supervisor.pid")
    assert (engine.LAST_UPDATE, launcher.FAILED_UPDATE) == ("last-update.json", "failed-update.json")
    assert (mailbox.FOLDER, LIVE_FILE) == ("requests", "live.json")


def test_last_update_json_reads_as_0_4_reads_it(tmp_path: Path) -> None:
    """0.4.x's start loop takes a version that didn't start back out by what records/last-update.json says,
    reading it its own way: these keys, these types."""
    home = make_install(tmp_path / "home")
    engine.install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    data = json.loads((home / "records" / engine.LAST_UPDATE).read_text(encoding="utf-8"))
    assert set(data) == {"version", "replaced", "backup", "added", "installed"}
    assert (data["version"], data["replaced"], data["backup"]) == (NEW, "0.0.1", "previous-0.0.1")
    assert isinstance(data["added"], list) and all(isinstance(path, str) for path in data["added"])
    assert (home / "records" / data["backup"]).is_dir()


def test_an_install_loads_every_module_before_it_swaps(tmp_path: Path, monkeypatch) -> None:
    """So that this version, running on after the swap like 0.4.x does, never imports a module of the next one."""
    order: list[str] = []
    load, swap = engine.load_every_module, engine._swap_in
    monkeypatch.setattr(engine, "load_every_module", lambda: (order.append("load"), load())[1])
    monkeypatch.setattr(engine, "_swap_in", lambda *args: (order.append("swap"), swap(*args))[1])
    home = make_install(tmp_path / "home")
    engine.install(make_release_zip(tmp_path / "r.zip"), home, NEW)
    assert order == ["load", "swap"]
    every = [m.name for m in pkgutil.walk_packages([str(engine.PACKAGE)], "huragok.")
             if not m.name.endswith("__main__")]
    assert len(every) > 100
    assert [name for name in every if name not in sys.modules] == []
