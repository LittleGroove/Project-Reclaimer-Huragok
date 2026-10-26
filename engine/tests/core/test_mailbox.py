"""Requests to a running supervisor through records/requests/, and editing settings.toml in place."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from huragok.core import mailbox
from huragok.core.tomledit import set_server_setting


def test_a_request_is_read_once_and_answered(tmp_path: Path) -> None:
    records = tmp_path / "records"
    request_id = mailbox.post(records, "stop", "LG Test Server", immediate=True)
    assert mailbox.take_reply(records, request_id) is None
    requests = mailbox.read(records)
    assert [(r.id, r.verb, r.server, r.options) for r in requests] == [
        (request_id, "stop", "LG Test Server", {"immediate": True})]
    assert mailbox.read(records) == []  # gone once read
    mailbox.reply(records, request_id, True, "stopping", "")
    reply = mailbox.take_reply(records, request_id)
    assert reply is not None and reply.ok and reply.message == "stopping"
    assert mailbox.take_reply(records, request_id) is None  # a reply is taken once


def test_send_waits_and_gives_up(tmp_path: Path) -> None:
    records = tmp_path / "records"
    reply = mailbox.send(records, "say", "all", wait=0.3, text="hi")
    assert not reply.ok and "didn't answer" in reply.message
    assert mailbox.read(records) == []  # the unanswered request was withdrawn


def test_the_command_line_says_an_unanswered_request_may_still_be_under_way(tmp_path: Path) -> None:
    """`update` waited 60 s for an answer that comes after the download and pip; no answer in time isn't a no."""
    from conftest import write_root

    from huragok.commandline import cli
    from huragok.core.settings import load

    settings = load(write_root(tmp_path / "reclaimer", []))
    reply = cli._ask(settings, "update", wait=0.3)
    assert not reply.ok and "No answer from Huragok after" in reply.message
    assert "It may still be doing it" in reply.message
    assert mailbox.read(settings.records) == []
    mailbox_reply = mailbox.Reply(False, "refused")
    original = mailbox.send
    try:
        mailbox.send = lambda *args, **kwargs: mailbox_reply  # type: ignore[assignment]  a real answer: as it is
        assert cli._ask(settings, "update", wait=5) is mailbox_reply
    finally:
        mailbox.send = original  # type: ignore[assignment]


def test_a_stale_request_is_refused(tmp_path: Path) -> None:
    records = tmp_path / "records"
    request_id = mailbox.post(records, "restart", "all")
    path = mailbox.folder(records) / f"{request_id}.json"
    text = path.read_text(encoding="utf-8").replace(f'"time": {mailbox.json.loads(path.read_text())["time"]}',
                                                     f'"time": {time.time() - 3600}')
    path.write_text(text, encoding="utf-8")
    assert mailbox.read(records) == []
    reply = mailbox.take_reply(records, request_id)
    assert reply is not None and not reply.ok and "too old" in reply.message
    mailbox.reply(records, "old", True, "x")
    old = mailbox.folder(records) / "old.reply.json"
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    mailbox.clear_old_replies(records)
    assert not old.exists()


def test_a_request_of_the_wrong_shape_is_answered_and_skipped(tmp_path: Path) -> None:
    """Anything can leave a file in records/requests/. Valid JSON of the wrong shape used to raise out of read(),
    and out of the fleet's tick with it."""
    records = tmp_path / "records"
    folder = mailbox.folder(records)
    good = mailbox.post(records, "say", "Alpha", text="hi")
    wrong = {"t1": {"verb": "status", "time": "now"}, "t2": {"verb": "stop", "options": ["now"]},
             "t3": {"verb": 3}, "t4": {"verb": "stop", "server": None}, "t5": {"verb": "stop", "id": "../../evil"},
             "t6": [1, 2], "t7": "stop"}
    for name, data in wrong.items():
        (folder / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    (folder / "t8.json").write_text("{not json", encoding="utf-8")
    os.utime(folder / "t8.json", (time.time() - 60, time.time() - 60))  # not one being written right now
    (folder / "t9.json").write_text("{half writ", encoding="utf-8")  # just now: perhaps still being written
    requests = mailbox.read(records)
    assert [r.id for r in requests] == [good]
    answers = {name: mailbox.take_reply(records, name) for name in (*wrong, "t8")}
    assert all(a is not None and not a.ok and a.message.startswith("not understood") for a in answers.values())
    assert "time must be a number" in answers["t1"].message and "options must be" in answers["t2"].message
    assert "id isn't a plain name" in answers["t5"].message and not (tmp_path / "evil.reply.json").exists()
    assert (folder / "t9.json").exists()  # left for the next look
    assert sorted(p.name for p in folder.iterdir()) == ["t9.json"]


SETTINGS = """# Huragok: how it runs.
servers_folder = "SERVERS"

[restarts]
warn_seconds = [300, 60]   # keep this comment

# [[server]]
# name = "Example"

[[server]]
name = "Slayer"   # the small one
daily_restart = "05:00"

[[server]]
name = "Big Team Battle"
autostart = true  # started at boot
"""


def test_set_server_setting_keeps_everything_else(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    path.write_text(SETTINGS, encoding="utf-8")
    set_server_setting(path, "big team battle", "autostart", False)
    text = path.read_text(encoding="utf-8")
    assert "autostart = false  # started at boot" in text
    assert "warn_seconds = [300, 60]   # keep this comment" in text and '# name = "Example"' in text
    set_server_setting(path, "Slayer", "autostart", False)
    text = path.read_text(encoding="utf-8")
    assert 'name = "Slayer"   # the small one\nautostart = false\ndaily_restart = "05:00"' in text
    set_server_setting(path, "Third", "autostart", False)
    text = path.read_text(encoding="utf-8")
    assert text.endswith('[[server]]\nname = "Third"\nautostart = false\n')
    import tomllib

    data = tomllib.loads(text)
    assert [(s["name"], s["autostart"]) for s in data["server"]] == [("Slayer", False), ("Big Team Battle", False),
                                                                    ("Third", False)]
    set_server_setting(tmp_path / "new.toml", "Only", "autostart", True)
    assert tomllib.loads((tmp_path / "new.toml").read_text(encoding="utf-8"))["server"][0]["autostart"] is True
