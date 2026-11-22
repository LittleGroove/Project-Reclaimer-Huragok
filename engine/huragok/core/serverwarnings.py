"""Huragok's warnings (0.7.0, plans/server-files-audit/plan-0.7.0.md section 5): what's wrong with a server, or worth
knowing, in plain words, with why and what to do. Huragok warns and carries on; it never refuses on a guess, since a
guess may be wrong after a Project Reclaimer update.

The words live as data, in templates/warnings.toml: each rule has its kind, its words, what to do, the newest Project
Reclaimer version it was checked on, and whether the staff channel hears of it. A new release only needs the rules
brought up to date, not new code. A server on a version newer than every rule was checked on gets one line saying so.

Four kinds:
  refused  the server's own program said no (`check`, a reload, a start)
  check    Huragok's own judgement of something the server lets through ("probably wrong")
  seen     what the server said while it played (a game skipped)
  info     nothing wrong, worth knowing

Each warning belongs to one server and is known by its rule and a key (which file, which game). A checker either sets
and clears warnings one at a time, or hands over everything it found (`sync`): what it no longer finds is cleared.

A warning can be dismissed per server (G or D, X): it stays hidden until the file it's about changes, or for good when
it's about no file. Dismissals are kept in records/warnings-dismissed.json.
"""

from __future__ import annotations

import json
import logging
import re
import time
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable

from .files import write_atomic

log = logging.getLogger(__name__)

KINDS = ("refused", "seen", "check", "info")  # in the order they're shown
WORDS = {"refused": "Refused", "seen": "Seen", "check": "Check", "info": "Info"}
MARKS = {"refused": "✗", "seen": "!", "check": "?", "info": "i"}
DISMISSED = "warnings-dismissed.json"  # in records/
CHECKED_UP_TO = "checked-up-to"  # the rule for a server newer than every rule was checked on


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    text: str  # str.format() with the fields the checker gives
    fix: str = ""  # what to do, the same way
    checked: str = ""  # the newest Project Reclaimer version this rule was checked on
    discord: bool = False  # said in the staff channel too (only what the host must act on)
    area: str = ""  # plays, runs, updates, huragok: where it's shown first


@dataclass(frozen=True)
class ServerWarning:
    server: str
    rule: Rule
    key: str
    text: str
    fix: str
    file: str | None  # the file it's about, if any: a dismissal holds until it changes
    since: float
    source: str = ""

    @property
    def id(self) -> str:
        return f"{self.rule.id}:{self.key}" if self.key else self.rule.id

    @property
    def kind(self) -> str:
        return self.rule.kind

    def line(self) -> str:
        """One line, as huragok.log and `start.cmd check` say it."""
        return f"{WORDS[self.kind]}: {self.text}" + (f" {self.fix}" if self.fix else "")


def version_key(version: str | None) -> tuple[int, ...]:
    """'0.10.9' -> (0, 10, 9); '' -> (). Betas and suffixes are ignored: only the numbers count."""
    return tuple(int(part) for part in re.findall(r"\d+", version or "")[:4])


def load_rules(text: str | None = None) -> dict[str, Rule]:
    """The rules from templates/warnings.toml (or `text`, for tests). RuleError on one that can't be used."""
    if text is None:
        text = resources.files("huragok").joinpath("templates", "warnings.toml").read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise RuleError(f"warnings.toml: {error}") from None
    rules: dict[str, Rule] = {}
    for number, entry in enumerate(data.get("rule") or [], 1):
        if not isinstance(entry, dict):
            raise RuleError(f"warnings.toml: rule {number} isn't a [[rule]] block")
        rule_id, kind = str(entry.get("id") or ""), str(entry.get("kind") or "")
        if not rule_id or kind not in KINDS or not isinstance(entry.get("text"), str):
            raise RuleError(f"warnings.toml: rule {number} needs an id, a kind ({', '.join(KINDS)}) and a text")
        if rule_id in rules:
            raise RuleError(f"warnings.toml: two rules are called {rule_id}")
        rules[rule_id] = Rule(rule_id, kind, entry["text"], str(entry.get("fix") or ""), str(entry.get("checked") or ""),
                              bool(entry.get("discord", False)), str(entry.get("area") or ""))
    return rules


def _fingerprint(file: str | None) -> str:
    if not file:
        return ""
    try:
        stat = Path(file).stat()
    except OSError:
        return "gone"
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _fill(template: str, fields: dict[str, Any], rule_id: str) -> str:
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError) as error:  # a rule's words naming a field the checker didn't give
        log.warning("warnings.toml: rule %s's words need %s; said as they are", rule_id, error)
        return template


@dataclass
class Warnings:
    """Every server's warnings now, and what's been dismissed. `bus` hears warning.raised and warning.cleared."""

    rules: dict[str, Rule]
    records: Path | None = None
    bus: Any = None
    clock: Callable[[], float] = time.time
    _now: dict[str, dict[str, ServerWarning]] = field(default_factory=dict)
    _dismissed: dict[str, dict[str, str]] | None = None

    @property
    def newest_checked(self) -> str:
        """The newest Project Reclaimer version any rule was checked on."""
        return max((rule.checked for rule in self.rules.values() if rule.checked), key=version_key, default="")

    def set(self, server: str, rule_id: str, key: str = "", *, about: Path | str | None = None, source: str = "",
            **fields: Any) -> ServerWarning | None:
        """The warning, raised (said once) or kept up to date. None for a rule warnings.toml doesn't have."""
        rule = self.rules.get(rule_id)
        if rule is None:
            log.warning("[%s] a warning for a rule warnings.toml doesn't have: %s", server, rule_id)
            return None
        text, fix = _fill(rule.text, fields, rule_id), _fill(rule.fix, fields, rule_id) if rule.fix else ""
        where = str(about) if about is not None else None  # the file it's about: a dismissal holds until it changes
        mine = self._now.setdefault(server, {})
        warning_id = f"{rule_id}:{key}" if key else rule_id
        old = mine.get(warning_id)
        if old is not None and (old.text, old.fix, old.file) == (text, fix, where):
            return old
        warning = ServerWarning(server, rule, key, text, fix, where, old.since if old else self.clock(), source)
        mine[warning_id] = warning
        if old is None:
            log.log(logging.INFO if rule.kind == "info" else logging.WARNING, "[%s] %s", server, warning.line())
            self._emit("warning.raised", warning)
        return warning

    def clear(self, server: str, rule_id: str, key: str = "") -> bool:
        warning = self._now.get(server, {}).pop(f"{rule_id}:{key}" if key else rule_id, None)
        if warning is not None:
            log.info("[%s] no longer: %s", server, warning.text)
            self._emit("warning.cleared", warning)
        return warning is not None

    def sync(self, server: str, source: str, found: Iterable[tuple[str, str, Path | str | None, dict[str, Any]]]) -> None:
        """Everything `source` finds on the server now, as (rule, key, file, fields): set, and what it found before but
        not now cleared."""
        keep = set()
        for rule_id, key, about, fields in found:
            warning = self.set(server, rule_id, key, about=about, source=source, **fields)
            if warning is not None:
                keep.add(warning.id)
        for warning in [w for w in self._now.get(server, {}).values() if w.source == source and w.id not in keep]:
            self.clear(server, warning.rule.id, warning.key)

    def version(self, server: str, version: str | None) -> None:
        """One line for a server newer than every rule was checked on: what Huragok warns about may be out of date."""
        newest = self.newest_checked
        if version and newest and version_key(version) > version_key(newest) and CHECKED_UP_TO in self.rules:
            self.set(server, CHECKED_UP_TO, source="version", version=version, checked=newest)
        else:
            self.clear(server, CHECKED_UP_TO)

    def of(self, server: str, *, dismissed: bool = False) -> list[ServerWarning]:
        """The server's warnings, refused first; dismissed ones only when asked."""
        shown = [w for w in self._now.get(server, {}).values() if dismissed or not self.is_dismissed(w)]
        return sorted(shown, key=lambda w: (KINDS.index(w.kind), w.since, w.id))

    def counts(self, server: str) -> dict[str, int]:
        found: dict[str, int] = {}
        for warning in self.of(server):
            found[warning.kind] = found.get(warning.kind, 0) + 1
        return found

    def summary(self, server: str) -> str:
        """'1 refused · 2 to check', for the V card and G's home; '' with none."""
        words = {"refused": "refused", "seen": "seen", "check": "to check", "info": "for your information"}
        counts = self.counts(server)
        return " · ".join(f"{counts[kind]} {words[kind]}" for kind in KINDS if counts.get(kind))

    # Dismissing ---------------------------------------------------------------------------------------------------

    def _load_dismissed(self) -> dict[str, dict[str, str]]:
        if self._dismissed is None:
            self._dismissed = {}
            if self.records is not None:
                try:
                    data = json.loads((self.records / DISMISSED).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                if isinstance(data, dict):
                    self._dismissed = {str(s): {str(k): str(v) for k, v in d.items()} for s, d in data.items()
                                       if isinstance(d, dict)}
        return self._dismissed

    def _save_dismissed(self) -> None:
        if self.records is None:
            return
        try:
            self.records.mkdir(parents=True, exist_ok=True)
            write_atomic(self.records / DISMISSED, json.dumps(self._load_dismissed(), indent=1, sort_keys=True) + "\n")
        except OSError as error:
            log.warning("Couldn't keep the dismissed warnings in %s: %s", self.records / DISMISSED, error)

    def is_dismissed(self, warning: ServerWarning) -> bool:
        kept = self._load_dismissed().get(warning.server, {}).get(warning.id)
        return kept is not None and kept == _fingerprint(warning.file)

    def dismiss(self, server: str, warning_id: str) -> bool:
        """Hidden on this server until its file changes (for good, for a warning about no file)."""
        warning = self._now.get(server, {}).get(warning_id)
        if warning is None:
            return False
        self._load_dismissed().setdefault(server, {})[warning_id] = _fingerprint(warning.file)
        self._save_dismissed()
        log.info("[%s] dismissed: %s", server, warning.text)
        return True

    def undismiss(self, server: str) -> int:
        """Every dismissed warning of the server shown again. Returns how many."""
        gone = self._load_dismissed().pop(server, {})
        if gone:
            self._save_dismissed()
        return len(gone)

    # The server itself ---------------------------------------------------------------------------------------------

    def forget(self, server: str) -> None:
        """A server no longer looked after: its warnings go (silently), its dismissals stay for when it's back."""
        self._now.pop(server, None)

    def rename(self, old: str, new: str) -> None:
        """A server renamed (core/renames.py): its warnings and dismissals follow it."""
        if old in self._now:
            self._now[new] = {wid: ServerWarning(new, w.rule, w.key, w.text, w.fix, w.file, w.since, w.source)
                              for wid, w in self._now.pop(old).items()}
        dismissed = self._load_dismissed()
        if old in dismissed:
            dismissed[new] = {**dismissed.pop(old), **dismissed.get(new, {})}
            self._save_dismissed()

    def _emit(self, name: str, warning: ServerWarning) -> None:
        if self.bus is not None:
            self.bus.emit(name, warning.server, id=warning.id, rule=warning.rule.id, kind=warning.kind,
                          text=warning.text, fix=warning.fix, discord=warning.rule.discord)
