"""0.7.0's update step (plans/server-files-audit/plan-0.7.0.md 4.10): settings Huragok no longer uses go from
settings.toml (a host's own value is kept as a comment saying where it went), the new ones come in, and what Huragok
had set in the servers' folders is recorded for one warning per server. The folders themselves aren't touched."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from testkit.hostfiles import host, up_to_date

from huragok.updates.hostfiles import HANDS_OFF_RECORD, hands_off_lines


def test_what_huragok_set_in_the_servers_folders_is_recorded_for_their_warnings(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    records = settings_path.parent / "records"
    records.mkdir(exist_ok=True)
    alpha, bravo = tmp_path / "SERVERS" / "Alpha", tmp_path / "SERVERS" / "Bravo"
    (records / "environment-taken-over.json").write_text(json.dumps(
        {"RECLAIMER_DEDICATED_CONTENT": {str(alpha): "content2"}}), encoding="utf-8")
    (records / "workshop-mods.json").write_text(json.dumps(
        {"adopted": [{"server": "Alpha", "item": 2898980746, "title": "Albatross Battle"}]}), encoding="utf-8")
    up_to_date(settings_path)
    record = json.loads((records / HANDS_OFF_RECORD).read_text(encoding="utf-8"))
    assert record["every"] == ["[workshop] update_check_minutes = 5 (from settings.toml's [workshop_updates] "
                               "check_minutes)"]
    lines = hands_off_lines(records, SimpleNamespace(folder=alpha, name="Alpha"))
    assert len(lines) == 3 and "content = content2 (from RECLAIMER_DEDICATED_CONTENT" in lines[1]
    assert "Workshop item 2898980746 (Albatross Battle)" in lines[2]
    assert hands_off_lines(records, SimpleNamespace(folder=bravo, name="Bravo")) == record["every"]
    assert hands_off_lines(tmp_path / "nowhere", SimpleNamespace(folder=alpha, name="Alpha")) == []


def test_workshop_items_0_6_listed_by_itself_are_found_in_its_log(tmp_path: Path) -> None:
    """0.6.x put the program's own Workshop copies in a server's [workshop] items with no record but its log line (the
    simulation, 22 November 2026: LG Backend Test's Albatross Battle wasn't in its warning). Read from huragok.log and
    the five kept before it; one already recorded as adopted isn't said twice."""
    settings_path = host(tmp_path)
    root = settings_path.parent
    records, logs = root / "records", root / "logs"
    records.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    line = ("{when} INFO    content.workshopmods: [{server}] {title} (Workshop {item}) is in its content folder "
            "(Mods/Workshop/{item}) but wasn't in [workshop] items: the server loads it all the same, so it's listed "
            "now and kept up to date. To stop using it: G -> the server -> Workshop and mods -> R\n")
    (logs / "huragok.log").write_text(line.format(when="2026-11-08 05:58:20,606", server="Bravo",
                                                  title="Albatross Battle", item=2898980746), encoding="utf-8")
    (logs / "huragok.log.2").write_text(
        line.format(when="2026-11-02 23:25:09,644", server="Alpha", title="HugeGrass Rebalance", item=3820687243)
        + line.format(when="2026-11-02 23:25:10,000", server="Alpha", title="Albatross Battle", item=2898980746),
        encoding="utf-8")
    (records / "workshop-mods.json").write_text(json.dumps(
        {"adopted": [{"server": "Alpha", "item": 2898980746, "title": "Albatross Battle"}]}), encoding="utf-8")
    up_to_date(settings_path)
    servers = json.loads((records / HANDS_OFF_RECORD).read_text(encoding="utf-8"))["servers"]
    assert servers["Bravo"] == ["Workshop item 2898980746 (Albatross Battle) in [workshop] items, for a copy in its "
                                "content folder"]
    assert servers["Alpha"] == ["Workshop item 2898980746 (Albatross Battle) in [workshop] items, for a copy put in "
                                "by hand",
                                "Workshop item 3820687243 (HugeGrass Rebalance) in [workshop] items, for a copy in its "
                                "content folder"]


def test_a_hosts_own_values_are_kept_as_comments_and_check_hours_becomes_minutes(tmp_path: Path) -> None:
    settings_path = host(tmp_path)
    text = settings_path.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^install = "ask"$', 'install = "after_game"', text, count=1)
    text = re.sub(r"(?m)^(\[server_updates\][^\[]*?)check_hours = 1$", r"\g<1>check_hours = 2", text, count=1)
    settings_path.write_text(text, encoding="utf-8")
    changed = up_to_date(settings_path)
    assert "settings.toml: [server_updates] check_hours = 2 is check_minutes = 120 now" in changed
    assert any(line.startswith("settings.toml: [server_updates] install made a comment") for line in changed)
    after = settings_path.read_text(encoding="utf-8")
    assert '# install = "after_game"' in after and "Not read since 0.7.0: each server installs its own" in after
    program = after[after.index("\n[server_updates]\n"):after.index("\n[workshop_updates]\n")]
    assert "\ncheck_minutes = 120\n" in program and "\ncheck_hours" not in program  # [updates]' own stays
    assert up_to_date(settings_path) == [] or all("0.7.0" not in line for line in up_to_date(settings_path))
