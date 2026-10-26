"""An MCC installation with what the server program copies from it."""

from __future__ import annotations

from pathlib import Path


def fake_mcc(folder: Path) -> Path:
    halo3 = folder / "halo3"
    for sub, files in (("hopper_map_variants", ["mlg_guardian_8.mvar", "team_hardcore_pit.mvar", "infection_zanzibar.mvar"]),
                       ("hopper_game_variants", ["mlg_ts_v8.bin", "team_hardcore_010.bin", "infection_alpha.bin"]),
                       ("map_variants", ["h3_hardcoreGuardian.mvar"]), ("game_variants", ["slayer-0_010.bin"])):
        (halo3 / sub).mkdir(parents=True, exist_ok=True)
        for name in files:
            (halo3 / sub / name).write_bytes(b"variant " + name.encode())
    (halo3 / "halo3.dll").write_bytes(b"dll")
    return folder
