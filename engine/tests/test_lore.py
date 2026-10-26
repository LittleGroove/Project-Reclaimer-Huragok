"""Who's who (lore.py): the tag on every log line, whose screen each key opens, the Superintendent's face."""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from testkit.window import FakeController, FakeKeyboard, FakeTerminal, row

from huragok import lore
from huragok.commandline import common
from huragok.core import lock as lock_module
from huragok.core.logs import CONTINUED, RecentLog
from huragok.window.dashboard import Menu, key_rows, pack, render


@pytest.mark.parametrize("logger, tag", [
    ("huragok.servers.fleet", "SUPT"), ("part.watchdog", "SUPT"), ("huragok.servers.backends", "SUPT"), ("superintendent", "SUPT"),
    ("part.automod", "VRGL"), ("part.commands", "VRGL"), ("huragok.players.relay", "VRGL"), ("huragok.players.ledger", "VRGL"),
    ("huragok.content.newserver", "HRGK"), ("huragok.updates.engine", "HRGK"), ("huragok.window.dashboard", "HRGK"), ("websockets", "HRGK"),
    ("huragok.managerial", "HRGK"),  # a prefix match is by whole names, not letters
])
def test_each_logger_speaks_as_its_part(logger: str, tag: str) -> None:
    assert lore.speaker(logger).tag == tag


def test_the_dashboard_lines_carry_the_tag_and_line_up() -> None:
    recent = RecentLog()
    recent.emit(logging.LogRecord("huragok.servers.fleet", logging.INFO, __file__, 1, "[Alpha] up\nsecond line", None, None))
    recent.emit(logging.LogRecord("part.automod", logging.WARNING, __file__, 1, "[Alpha] Bob: strike", None, None))
    (_, first), (_, second), (level, third) = recent.recent(3)
    assert first[8:] == " SUPT INFO    [Alpha] up"
    assert second == CONTINUED + "second line" and len(first) - len("[Alpha] up") == len(CONTINUED) - 2
    assert level == logging.WARNING and third[8:].startswith(" VRGL WARNING [Alpha] Bob")


def test_every_key_belongs_to_a_part() -> None:
    menu = Menu(FakeController(), FakeTerminal(), FakeKeyboard([]))  # type: ignore[arg-type]
    assert set(menu.actions) <= set(lore.KEYS)
    shown = "".join(keys for _, keys, _ in key_rows(True))
    for key, persona in lore.KEYS.items():
        if key != "?":
            assert f"[{key}]" in shown, key
    owners = {who: keys for who, keys, _ in key_rows(True)}
    assert all(f"[{k}]" in owners["SUPERINTENDENT"] for k in "SXRKTL")
    assert all(f"[{k}]" in owners["VERGIL"] for k in "PMAB")


def test_the_key_rows_fit_the_widest_dashboard() -> None:
    for who, keys, _ in key_rows(False):
        assert len(f"  {who:<14}  {keys}") <= 110


def mood_of(*rows: dict[str, Any]) -> lore.Mood:
    return lore.mood(rows)


def test_the_superintendents_face_follows_the_worst_thing_going_on() -> None:
    up = row("Alpha")
    empty = row("Beta", 2, players="0/16")
    stopped = row("Gamma", 3, phase="idle", status="stopped", players="")
    assert mood_of(up, empty).expression == "happy" and mood_of(up).notice == "KEEP IT CLEAN!"
    assert mood_of(up, empty).summary == "2 of 2 servers up, 3 players on"
    assert mood_of(empty, stopped).expression == "bored" and mood_of(empty).notice == "YOUR TAX DOLLARS AT WORK!"
    assert mood_of(stopped).expression == "sleeping" and mood_of(stopped).notice == ""
    starting = row("Delta", phase="booting", status="STARTING")
    assert mood_of(up, starting).expression == "neutral" and "Delta is starting" in mood_of(starting).summary
    counting = row("Echo", phase="countdown", status="RESTART IN 4m (daily restart)")
    assert mood_of(starting, counting).expression == "confused"
    assert mood_of(counting).notice == "PLEASE REMAIN CALM!" and "Echo restart in 4m" in mood_of(counting).summary
    crashed = row("Fox", phase="backoff", status="CRASHED - retry in 5 s")
    assert mood_of(counting, crashed).expression == "sad" and mood_of(crashed).notice == "PARDON OUR DUST!"
    faulted = row("Golf", phase="faulted", status="FAULTED - [S] to retry")
    worst = mood_of(crashed, faulted, row("Hotel", phase="faulted"))
    assert worst.expression == "angry" and worst.notice == "METROPOLITAN EMERGENCY!" and worst.colour == "red"
    assert worst.summary == "Golf is FAULTED (and 1 more)"
    assert mood_of(row("India", phase="idle", status="CONFIG ERROR")).expression == "angry"
    assert lore.mood([]).expression == "confused" and lore.mood([up], answering=False).expression == "off"
    assert worst.line().startswith("[>_<] SUPERINTENDENT · METROPOLITAN EMERGENCY! · Golf")


def test_the_notices_are_the_superintendents_own() -> None:
    # Word for word from Bungie's 2008 "KEEP IT CLEAN" campaign: never paraphrase them.
    assert set(lore.NOTICES.values()) == {
        "KEEP IT CLEAN!", "PLEASE REMAIN CALM!", "METROPOLITAN EMERGENCY!", "DETOUR! PROCEED WITH CAUTION!",
        "BE A HERO! REPORT VIOLATORS!", "PARDON OUR DUST!", "YOUR TAX DOLLARS AT WORK!", "OBEY POSTED LIMITS!",
        "YIELD TO UPHILL TRAFFIC!", "IN CASE OF FIRE, USE STAIRS"}
    assert set(lore.FACES) == {"happy", "neutral", "bored", "confused", "sad", "angry", "sleeping", "off"}
    assert len({len(face) for face in lore.FACES.values()}) == 1  # the faces line up


def test_the_dashboard_wears_the_theme() -> None:
    view = {"version": "9.9", "supervising": True, "rows": [row("Alpha")], "startup": None, "events": [],
            "header": ["SUPT backend process · servers in SERVERS/", "VRGL commands", "HRGK server program 0.9.18"]}
    lines = render(view, 120, 40)
    text = [t for t, _ in lines]
    assert text[0].startswith(" HURAGOK 9.9   (supervising)")
    assert lines[1] == (" [^_^] SUPERINTENDENT · KEEP IT CLEAN! · 1 of 1 server up, 3 players on", "green")
    assert any(t.startswith("  SUPERINTENDENT  [S] Start") for t in text)
    assert dict(lines)["  VERGIL          [P] Players  [M] Message  [A] Console  [B] Bans & admins"] == "sky"
    assert any("SUPT Superintendent  VRGL Vergil  HRGK Huragok" in t for t in text)
    assert pack(["aaaa", "bbbb", "cccc"], 12, " | ") == ["aaaa | bbbb", "cccc"]


def test_screens_are_titled_with_their_part(run) -> None:
    terminal = FakeTerminal()
    menu = Menu(FakeController(), terminal, FakeKeyboard(["H", "x", "P", "x", "Q", "Y"]))  # type: ignore[arg-type]
    assert run(menu.run()) == "quit"
    printed = re.sub(r"\x1b\[[0-9;]*m", "", "".join(terminal.output))
    assert "HURAGOK › HELP" in printed and "VERGIL › PLAYERS ONLINE" in printed and "HURAGOK › QUIT" in printed
    assert "Who's who" in printed and "Quick to Adjust" in printed and "Dr. Endesha" in printed
    assert lore.BANNER[0] in printed and "Feet first into hell." in printed and "VISR" in printed


def test_the_commands_wear_it_too(tmp_path, monkeypatch, capsys) -> None:
    import json
    import time

    from conftest import free_port, write_root, write_server_folder

    from huragok.commandline import cli
    from huragok.core.settings import load

    first = tmp_path / "first"
    write_server_folder(first / "SERVERS" / "Alpha", free_port(), exe=True)
    assert cli.main(["-c", str(first / "settings.toml"), "start"]) == common.SET_UP
    out = capsys.readouterr().out
    assert lore.BANNER[1] in out and "HURAGOK" in out and "THE FIRST DROP" in out and "Feet first into hell." in out

    root = tmp_path / "running"
    write_root(root, [("Alpha", free_port())])
    settings = load(root / "settings.toml")
    settings.records.mkdir(parents=True, exist_ok=True)
    (settings.records / "live.json").write_text(json.dumps(
        {"updated": time.time(), "version": "9.9", "backend": "process", "servers": [row("Alpha")]}), encoding="utf-8")
    monkeypatch.setattr(lock_module, "running_supervisor", lambda settings: 4242)
    assert cli.main(["-c", str(root / "settings.toml"), "status"]) == 0
    out = capsys.readouterr().out
    assert "HURAGOK 9.9" in out and "[^_^] SUPERINTENDENT · KEEP IT CLEAN! · 1 of 1 server up, 3 players on" in out

    text = "\n".join("".join(t for t, _ in parts) for parts in cli.help_lines())
    assert lore.BANNER[0] in text and "Superintendent · keeps the servers running" in text
    assert "Vergil · watches over the players" in text and "watch [server|huragok]" in text


def test_what_the_old_name_left_behind_is_still_understood(tmp_path, monkeypatch) -> None:
    """A start-at-boot item or a settings.toml from Reclaimer Supervisor 0.2.1 keeps working under the new name."""
    from types import SimpleNamespace

    from conftest import free_port, write_root

    from huragok.core import desktop
    from huragok.core.settings import load

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(desktop.subprocess, "run", lambda *args, **options: SimpleNamespace(returncode=0, stderr=""))
    path = desktop.startup_path()
    assert path is not None
    old = path.with_name("Reclaimer Supervisor.cmd" if path.suffix == ".cmd" else "reclaimer-supervisor.service")
    old.parent.mkdir(parents=True)
    old.write_text("the old item", encoding="utf-8")
    assert desktop.startup_enabled() is True  # W shows ON, as it starts at boot
    desktop.enable_startup(tmp_path / "root")  # turned on again: one item, under the new name
    assert path.is_file() and not old.exists()
    old.write_text("the old item", encoding="utf-8")
    assert desktop.disable_startup() == "Huragok will no longer start at boot"
    assert not path.exists() and not old.exists() and desktop.startup_enabled() is False

    root = tmp_path / "root"
    write_root(root, [("Alpha", free_port())], body='[updates]\nrepository = "LittleGroove/Project-Reclaimer-Server-Supervisor"\n')
    assert load(root / "settings.toml").updates.repository == "LittleGroove/Project-Reclaimer-Huragok"
