"""What Discord refuses, so the fake refuses it too and a test fails here rather than in someone's Discord. Each function returns what's wrong with a form, in words; nothing means it would be taken.

The numbers are Discord's published limits, and the ones marked measured are what the real thing did for this bot.
"""

from __future__ import annotations

import re
from typing import Any

CHECK_SLASH_NAME = re.compile(r"[-_\w]{1,32}")
EPHEMERAL, SUPPRESS_EMBEDS, SILENT = 64, 4, 4096
ROW, BUTTON, TEXT_SELECT, TEXT_INPUT, LABEL = 1, 2, 3, 4, 18
PICKERS = {3, 5, 6, 7, 8}  # the kinds of select


def _too_long(what: str, value: Any, most: int, *, least: int = 0) -> list[str]:
    length = len(str(value if value is not None else ""))
    return [f"{what} is {length} characters, and must be {least} to {most}"] if not least <= length <= most else []


def embed_problems(embeds: list[dict[str, Any]]) -> list[str]:
    found = [f"{len(embeds)} embeds, at most 10"] if len(embeds) > 10 else []
    total = 0
    for embed in embeds:
        found += _too_long("an embed's title", embed.get("title"), 256) + _too_long("an embed's description", embed.get("description"), 4096)
        footer = str((embed.get("footer") or {}).get("text") or "")
        author = str((embed.get("author") or {}).get("name") or "")
        found += _too_long("an embed's footer", footer, 2048) + _too_long("an embed's author", author, 256)
        fields = embed.get("fields") or []
        if len(fields) > 25:
            found.append(f"{len(fields)} fields in an embed, at most 25")
        for field in fields:
            found += _too_long("a field's name", field.get("name"), 256, least=1) + _too_long("a field's value", field.get("value"), 1024, least=1)
            total += len(str(field.get("name") or "")) + len(str(field.get("value") or ""))
        total += len(str(embed.get("title") or "")) + len(str(embed.get("description") or "")) + len(footer) + len(author)
    return found + ([f"{total} characters across the embeds, at most 6000"] if total > 6000 else [])


def _select_problems(select: dict[str, Any]) -> list[str]:
    found = _too_long("a select's custom_id", select.get("custom_id"), 100, least=1) + _too_long("a select's placeholder", select.get("placeholder"), 150)
    least, most = int(select.get("min_values", 1)), int(select.get("max_values", 1))
    if not 0 <= least <= most <= 25:
        found.append(f"a select's min_values {least} and max_values {most} don't make sense")
    if select["type"] != TEXT_SELECT:
        return found
    options = select.get("options") or []
    if not 1 <= len(options) <= 25:
        found.append(f"a select has {len(options)} options, and must have 1 to 25")
    for option in options:
        found += _too_long("an option's label", option.get("label"), 100, least=1) + _too_long("an option's value", option.get("value"), 100, least=1) + _too_long("an option's description", option.get("description"), 100)
    values = [str(option.get("value")) for option in options]
    if len(set(values)) != len(values):
        found.append("two options of a select have the same value")
    if sum(1 for option in options if option.get("default")) > most:
        found.append("more options picked by default than max_values allows")
    return found


def component_problems(rows: list[dict[str, Any]]) -> list[str]:
    found = [f"{len(rows)} rows of components, at most 5"] if len(rows) > 5 else []
    seen: set[str] = set()
    for row in rows:
        if row.get("type") != ROW:
            found.append(f"a top-level component of type {row.get('type')}, not a row")
            continue
        items = row.get("components") or []
        if not 1 <= len(items) <= 5:
            found.append(f"a row with {len(items)} components, and it must have 1 to 5")
        if any(item.get("type") in PICKERS for item in items) and len(items) > 1:
            found.append("a select must have a row to itself")
        for item in items:
            custom_id = str(item.get("custom_id") or "")
            if custom_id in seen:
                found.append(f"two components with the custom_id {custom_id}")
            seen.add(custom_id)
            if item.get("type") == BUTTON:
                found += _too_long("a button's label", item.get("label"), 80, least=1 if not item.get("emoji") else 0) + _too_long("a button's custom_id", custom_id, 100, least=0 if item.get("url") else 1)
            elif item.get("type") in PICKERS:
                found += _select_problems(item)
    return found


def _mention_problems(allowed: dict[str, Any] | None) -> list[str]:
    allowed = allowed or {}
    parse = allowed.get("parse") or []
    return [f"allowed_mentions parses {kind} and also lists them" for kind in ("users", "roles") if kind in parse and allowed.get(kind)]


def message_problems(body: dict[str, Any], *, editing: bool = False, may_be_ephemeral: bool = False) -> list[str]:
    found = _too_long("the content", body.get("content"), 2000) + embed_problems(body.get("embeds") or []) + component_problems(body.get("components") or []) + _mention_problems(body.get("allowed_mentions"))
    allowed_flags = SUPPRESS_EMBEDS | SILENT | (EPHEMERAL if may_be_ephemeral else 0)
    if int(body.get("flags") or 0) & ~allowed_flags:
        found.append(f"flags {body.get('flags')} can't be set here")
    if not editing and not (body.get("content") or body.get("embeds") or body.get("files") or body.get("attachments")):
        found.append("nothing to send: Cannot send an empty message")
    return found


def modal_problems(body: dict[str, Any]) -> list[str]:
    found = _too_long("a form's title", body.get("title"), 45, least=1) + _too_long("a form's custom_id", body.get("custom_id"), 100, least=1)
    parts = body.get("components") or []
    if not 1 <= len(parts) <= 5:
        found.append(f"a form with {len(parts)} parts, and it must have 1 to 5")
    for part in parts:
        if part.get("type") == LABEL:
            found += _too_long("a form's label", part.get("label"), 45, least=1) + _too_long("a form's label's description", part.get("description"), 100)
            inner = part.get("component") or {}
        elif part.get("type") == ROW:
            inner = (part.get("components") or [{}])[0]
            found += _too_long("a form's label", inner.get("label"), 45, least=1)
        else:
            found.append(f"a form part of type {part.get('type')}")
            continue
        if inner.get("type") == TEXT_INPUT:
            found += _too_long("a text box's placeholder", inner.get("placeholder"), 100) + _too_long("a text box's value", inner.get("value"), 4000)
            if int(inner.get("max_length") or 4000) > 4000:
                found.append(f"a text box's max_length is {inner.get('max_length')}, at most 4000")
            if inner.get("value") and len(str(inner["value"])) > int(inner.get("max_length") or 4000):
                found.append("a text box starts with more than its max_length")
        elif inner.get("type") in PICKERS:
            found += _select_problems(inner)
    return found


def _option_problems(options: list[dict[str, Any]], where: str) -> list[str]:
    found = [f"{where} has {len(options)} options, at most 25"] if len(options) > 25 else []
    optional = False
    for option in options:
        name = str(option.get("name") or "")
        if not CHECK_SLASH_NAME.fullmatch(name) or name != name.lower():
            found.append(f"{where}: the option name {name!r} isn't allowed")
        found += _too_long(f"{where}: {name}'s description", option.get("description"), 100, least=1)
        if option.get("type") in (1, 2):  # a subcommand, or a group of them
            found += _option_problems(option.get("options") or [], f"{where} {name}")
            continue
        if option.get("required"):
            if optional:
                found.append(f"{where}: the required option {name} comes after an optional one")
        else:
            optional = True
        if len(option.get("choices") or []) > 25:
            found.append(f"{where}: {name} has more than 25 choices")
    return found


def command_problems(commands: list[dict[str, Any]]) -> list[str]:
    found: list[str] = []
    slash = [command for command in commands if command.get("type", 1) == 1]
    if len(slash) > 100:
        found.append(f"{len(slash)} slash commands, at most 100")
    for kind, word in ((2, "member"), (3, "message")):
        if sum(1 for command in commands if command.get("type") == kind) > 5:
            found.append(f"more than 5 right-click commands on a {word}")
    names = [(command.get("type", 1), command.get("name")) for command in commands]
    if len(set(names)) != len(names):
        found.append("two commands with the same name")
    for command in commands:
        name = str(command.get("name") or "")
        if command.get("type", 1) == 1:
            if not CHECK_SLASH_NAME.fullmatch(name) or name != name.lower():
                found.append(f"the command name {name!r} isn't allowed")
            found += _too_long(f"/{name}'s description", command.get("description"), 100, least=1) + _option_problems(command.get("options") or [], f"/{name}")
        else:
            found += _too_long("a right-click command's name", name, 32, least=1)
            if command.get("description"):
                found.append(f"the right-click command {name} has a description")
    return found


__all__ = ["command_problems", "component_problems", "embed_problems", "message_problems", "modal_problems"]
