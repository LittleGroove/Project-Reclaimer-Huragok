"""Huragok's log lines as the window shows them (core/logs.py), and text kept to one line."""

from __future__ import annotations

import logging

from huragok.core.logs import RecentLog
from huragok.core.text import one_line


def test_log_lines_keep_their_time_and_level() -> None:
    recent = RecentLog()
    logger = logging.getLogger("test.quality")
    logger.addHandler(recent)
    logger.setLevel(logging.INFO)
    echoed: list[tuple[int, str]] = []
    recent.echo = lambda level, text: echoed.append((level, text))
    try:
        logger.warning("[Slayer] settings reloaded: Configuration reloaded.\nRestart `dedicated` to apply [workshop].\n")
    finally:
        logger.removeHandler(recent)
    lines = recent.recent(5)
    assert len(lines) == 2 and lines[0][1].endswith("settings reloaded: Configuration reloaded.")
    assert lines[1][1].strip().startswith("↳ Restart `dedicated`") and lines[1][0] == logging.WARNING
    assert echoed == lines
    assert one_line("Configuration reloaded.\n  Restart `dedicated` to apply [workshop] update checks.\n") == \
        "Configuration reloaded. | Restart `dedicated` to apply [workshop] update checks."
