"""Where Huragok's log lines go: the window's recent lines, the console (--no-menu, coloured when it's a real
one), and logs/huragok.log (each line with the module that spoke, which a second window turns back into its tag)."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from ..core.settings import Settings


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


class _FileFormatter(logging.Formatter):
    """huragok.log names the module that spoke without the package prefix: `servers.fleet:`, not
    `huragok.servers.fleet:`."""

    def format(self, record: logging.LogRecord) -> str:
        record.short = record.name.removeprefix("huragok.")
        return super().format(record)


class _ConsoleHandler(logging.StreamHandler):
    """The plain log (--no-menu): warnings yellow and errors red when the window is a real console."""

    def __init__(self) -> None:
        super().__init__()
        from ..window.tui import enable_vt

        try:
            self._colour = bool(self.stream.isatty()) and enable_vt() and not os.environ.get("NO_COLOR")
        except (AttributeError, ValueError):
            self._colour = False

    def format(self, record: logging.LogRecord) -> str:
        from ..window.tui import paint

        text = super().format(record)
        if not self._colour or record.levelno < logging.WARNING:
            return text
        return paint(text, "red" if record.levelno >= logging.ERROR else "yellow")


def setup(settings: Settings | None, verbose: bool, *, console: bool = True,
                   recent: logging.Handler | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if console:
        stream = _ConsoleHandler()
        from ..core.logs import TaggedFormatter

        stream.setFormatter(TaggedFormatter())
        root.addHandler(stream)
    if recent is not None:
        root.addHandler(recent)
    if settings and settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file = logging.handlers.RotatingFileHandler(settings.log_file, maxBytes=5_000_000, backupCount=5,
                                                    encoding="utf-8")
        file.setFormatter(_FileFormatter("%(asctime)s %(levelname)-7s %(short)s: %(message)s"))
        root.addHandler(file)
    logging.getLogger("websockets").setLevel(logging.WARNING)
