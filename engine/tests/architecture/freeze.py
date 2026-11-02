"""Writes frozen.json, what test_what_cant_change.py holds the code to. Run it only for what no release has had yet: a released step is on hosts' databases, and changing it here only stops the test saying so.

    python tests/architecture/freeze.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from huragok.core.commandsfile import all_commands  # noqa: E402
from huragok.core.storage import CORE_MIGRATIONS  # noqa: E402
from huragok.discordbot.slash import NOT_FROM_DISCORD, plan_of  # noqa: E402
from huragok.discordbot.status import SCHEMA  # noqa: E402
from huragok.players.automod.automod import MIGRATIONS as AUTOMOD_MIGRATIONS  # noqa: E402
from huragok.players.commands.schema import MIGRATIONS as COMMANDS_MIGRATIONS  # noqa: E402


def _hashes(steps: list[str]) -> list[str]:
    return [hashlib.sha256(step.encode("utf-8")).hexdigest() for step in steps]


frozen = {"schema": {"core": _hashes(CORE_MIGRATIONS), "automod": _hashes(AUTOMOD_MIGRATIONS), "commands": _hashes(COMMANDS_MIGRATIONS), "discord": _hashes(SCHEMA)},
          "slash": {spec.name: [option.name for option in plan_of(spec)] for spec in sorted(all_commands().values(), key=lambda spec: spec.name) if spec.name not in NOT_FROM_DISCORD}}
path = Path(__file__).parent / "frozen.json"
path.write_text(json.dumps(frozen, indent=1) + "\n", encoding="utf-8")
print(f"Written: {path}")
