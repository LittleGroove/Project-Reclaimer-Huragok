"""Kept at this path for Huragok 0.4.0 to 0.4.4.

An update swaps engine/huragok while the old Huragok is still running (it installs the Python packages, then waits
for any server part way through a restart before it restarts on the new version). Its imports inside functions then
read this folder, and the first start or stop of a server on Windows imports `huragok.winproc`: these names, with
the same meaning, keep that working. Everything new imports `huragok.core.winproc`.

In that old process `huragok.core` is 0.4.x's own core.py, a module and not this folder's package, so
`huragok.core.winproc` can't be imported there: core/winproc.py (the standard library only, the same code as every
0.4.x winproc.py) is then loaded by its path.
tests/updates/test_from_0_4.py checks that what 0.4.x imports from here is still here, and loads in such a process.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _core_winproc() -> ModuleType:
    core = sys.modules.get(f"{__package__}.core")
    if core is None or hasattr(core, "__path__"):  # this version's own process: core/ is the package
        from .core import winproc

        return winproc
    path = Path(__file__).with_name("core") / "winproc.py"
    spec = importlib.util.spec_from_file_location(f"{__package__}._winproc_for_0_4", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"can't load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_winproc = _core_winproc()
CREATE_BREAKAWAY_FROM_JOB = _winproc.CREATE_BREAKAWAY_FROM_JOB
CREATE_NEW_PROCESS_GROUP = _winproc.CREATE_NEW_PROCESS_GROUP
CREATE_NO_WINDOW = _winproc.CREATE_NO_WINDOW
CTRL_BREAK_EVENT = _winproc.CTRL_BREAK_EVENT
DETACHED_PROCESS = _winproc.DETACHED_PROCESS
ERROR_ACCESS_DENIED = _winproc.ERROR_ACCESS_DENIED
creation_flags = _winproc.creation_flags
ctrl_break = _winproc.ctrl_break
ctrl_break_helper_command = _winproc.ctrl_break_helper_command
send_ctrl_break = _winproc.send_ctrl_break
