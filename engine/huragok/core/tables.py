"""Reading the TOML files Huragok checks (settings.toml, each server's dedicated.toml): every value read with its
kind, and every mistake named with the file and the key, said the way the host would put it right."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any



CLOCK_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


KIND_NAMES = {bool: "true or false", str: "text in quotes", int: "a whole number", float: "a number",
              list: "a list", dict: "a table"}


BACKSLASH_HINT = ("Windows paths: write them in 'single quotes' (like 'C:\\steamcmd\\steamcmd.exe') or with "
                  "forward slashes (C:/steamcmd/steamcmd.exe); inside \"double quotes\" a \\ starts an escape")


class ConfigError(Exception):
    pass


def _shown(value: Any) -> str:
    """A value as it reads in settings.toml: "no", 300, true."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "a table"
    return str(value)


class Table:
    """A TOML table with typed getters whose errors name the setting."""

    def __init__(self, data: dict[str, Any], where: str = "") -> None:
        self.data = data
        self.where = where

    def _name(self, key: str) -> str:
        return f"{self.where}.{key}" if self.where else key

    def get(self, key: str, kind: type, default: Any) -> Any:
        value = self.data.get(key, default)
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if value is not None and (not isinstance(value, kind) or (kind is int and isinstance(value, bool))):
            raise ConfigError(f"{self._name(key)} must be {KIND_NAMES.get(kind, kind.__name__)}, not {_shown(value)}")
        return value

    def at_least(self, key: str, default: float, minimum: float) -> float:
        """A number of seconds (or the like) that makes no sense below `minimum`."""
        value = self.get(key, float, default)
        if value < minimum:
            raise ConfigError(f"{self._name(key)} must be at least {minimum:g}, not {value:g}")
        return value

    def path(self, key: str, default: str | None) -> str | None:
        """A file or folder name. A control character in one is a Windows path typed into "double quotes", where
        \\n, \\t or \\b silently became a line break, a tab or a backspace."""
        value = self.get(key, str, default)
        if value and (bad := next((c for c in value if ord(c) < 0x20 or ord(c) == 0x7F), None)):
            raise ConfigError(f"{self._name(key)} has a control character in it ({json.dumps(bad)[1:-1]}), so the "
                              f"path isn't what you typed. {BACKSLASH_HINT}")
        return value

    def strings(self, key: str) -> list[str]:
        value = self.data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self._name(key)} must be a list of text")
        return [v.strip() for v in value if v.strip()]

    def numbers(self, key: str, default: list[float]) -> list[float]:
        value = self.data.get(key, default)
        if not isinstance(value, list) or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                 and v >= 0 for v in value):
            raise ConfigError(f"{self._name(key)} must be a list of seconds, like [300, 60, 30]")
        return [float(v) for v in value]

    def table(self, key: str) -> Table:
        value = self.data.get(key, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{self._name(key)} must be a table, like [{self._name(key)}]")
        return Table(value, self._name(key))


def _read_toml(path: Path, hint: str) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"{path} not found{hint}") from None
    except tomllib.TOMLDecodeError as error:
        text = str(error)
        hint = f". {BACKSLASH_HINT}" if "Unescaped '\\'" in text or "hex value" in text.lower() else ""
        raise ConfigError(f"{path}: {text}{hint}") from None


def check_clock_time(value: str, where: str) -> str:
    """'04:00' -> '04:00'; anything else is an error. Empty means none."""
    value = value.strip()
    if not value:
        return ""
    match = CLOCK_TIME.match(value)
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ConfigError(f"{where}: '{value}' isn't a time of day; use the 24-hour clock, like \"04:00\"")
    return f"{int(match.group(1)):02d}:{match.group(2)}"
