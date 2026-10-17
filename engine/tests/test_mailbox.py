"""Requests to a running supervisor through records/requests/, and editing settings.toml in place."""

from __future__ import annotations

import os
import time
from pathlib import Path

from reclaimer_supervisor import mailbox
from reclaimer_supervisor.tomledit import set_server_setting


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


SETTINGS = """# Reclaimer Supervisor: how it runs.
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
