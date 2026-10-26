"""A host's files as 0.4.0 wrote them, to bring up to date."""

from __future__ import annotations

import shutil
from pathlib import Path

from testkit import DATA

from huragok.updates import hostfiles as migrate


HOST_0_4_0 = DATA / "host-0.4.0"


def host(tmp_path: Path, *, packages: bool = True) -> Path:
    """A root as 0.4.0 left it: its four files, and PACKAGES/ with 0.4.0's starters."""
    root = tmp_path / "huragok"
    root.mkdir(parents=True)
    for path in HOST_0_4_0.iterdir():
        shutil.copy2(path, root / path.name)
    if packages:
        for name in migrate.STARTERS_0_4_0:
            (root / "PACKAGES" / name).mkdir(parents=True)
            (root / "PACKAGES" / name / "package.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    (root / "SERVERS").mkdir()
    return root / "settings.toml"


def up_to_date(settings_path: Path, *, settled: bool = True, notes: bool = True) -> list[str]:
    """What a start changes; with `settled`, then what the running Huragok changes once it's past the point where the
    start loop would put the version before back (a new command in commands.toml). `notes`: False leaves out the
    step that brings Huragok's notes up to date, for a test of what another step changes."""
    root = settings_path.parent
    steps = None if notes else [step for step in migrate.STEPS if not step.key.endswith("/notes")]
    changed = migrate.bring_up_to_date(settings_path, root / "records", root / "PACKAGES", steps)
    if settled:
        changed += migrate.bring_up_to_date(settings_path, root / "records", root / "PACKAGES", steps, settled=True)
    return changed
