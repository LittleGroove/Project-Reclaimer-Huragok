"""Votes to kick: members, moderators and owners go on each server's vote-kick immune list, Vergil explains a vote
the server refused, and a vote on one of them that gets called anyway is cancelled."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

from conftest import ALICE, BRAVO, MOD, OWNER, free_port, staff, wait_for, write_server_folder, write_settings
from testkit.fake_rcon import FakeRcon
from testkit.running import SERVER, everyone_joins, running, says, tells_to

from huragok.commandline import cli
from huragok.core.commandsfile import load_permissions, render_commands_toml
from huragok.players.commands import votekicks
from huragok.players.roles import Roles
from huragok.servers.logs import parse_log_line
from huragok.servers.state import Player, ServerState

MEMBER = "7" * 64  # Charlie


def listed(*members: str) -> str:
    """commands.toml with OWNER and MOD, and these members (Charlie when none are named)."""
    ids = ", ".join(f'"{m}"' for m in members or (MEMBER,))
    return staff(f"[member]\nplayers = [{ids}]\n")


def changes(fake: FakeRcon) -> list[list[str]]:
    """Every `immune add` and `immune remove` the server was sent."""
    return [c["args"] for c in fake.named("immune") if c.get("args", ["list"])[0] != "list"]


def test_member_is_a_role_between_everyone_and_moderator(tmp_path: Path) -> None:
    path = tmp_path / "commands.toml"
    path.write_text(listed(), encoding="utf-8")
    permissions = load_permissions(path)
    assert permissions.members["member"] == {MEMBER} and permissions.listed() == {OWNER, MOD, MEMBER}
    state = ServerState(SERVER, admins=set())
    roles = Roles(SimpleNamespace(permissions=permissions))
    assert roles.role_of(state, Player(1, "Charlie", MEMBER)) == "member"
    assert roles.role_of(state, Player(2, "Mod", MOD)) == "moderator"
    assert Roles.allows("member", "player") and not Roles.allows("member", "moderator")

    text = render_commands_toml()  # what init writes: [member] with players to fill in and no commands
    assert "\n[member]\nplayers = [\n" in text and "no vote can kick them" in text
    path.write_text(text, encoding="utf-8")
    written = load_permissions(path)
    assert "member" in written.sections and "member" not in written.roles.values()


def test_check_points_out_a_commands_file_from_before_members(tmp_path: Path, capsys) -> None:
    root = tmp_path / "reclaimer"
    write_server_folder(root / "SERVERS" / "Alpha", free_port(), name="Alpha")
    path = write_settings(root, None, "", staff())
    cli.main(["-c", str(path), "check"])
    assert "commands.toml is from before members: add [member]" in "".join(capsys.readouterr())
    (root / "commands.toml").write_text(listed(), encoding="utf-8")
    cli.main(["-c", str(path), "check"])
    text = "".join(capsys.readouterr())
    assert "member (1 listed)" in text and "from before members" not in text


def test_the_refusal_line_in_server_log() -> None:
    assert parse_log_line("03:07:07 pjams could not call a vote: Charlie can't be vote-kicked on this server.") == (
        "log.vote_refused", {"caller": "pjams", "reason": "Charlie can't be vote-kicked on this server.",
                             "clock": "03:07:07"})


def test_everyone_listed_is_made_vote_kick_immune_and_taken_off_when_unlisted(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    def prepare(fake: FakeRcon) -> None:
        fake.immune.append({"id": BRAVO, "added_by": "RCON", "added_at": 0})  # the host's own: it stays
        fake.immune.append({"id": ALICE, "added_by": "Vergil (RCON)", "added_at": 0})  # listed once, not any more
        fake.immune_configured.append(OWNER)  # dedicated.toml's vote_kick_immune

    async def scenario() -> None:
        async with running(tmp_path, commands=listed(), prepare=prepare) as (sup, fake, _):
            await wait_for(lambda: fake.immune_ids() == {BRAVO, MOD, MEMBER})
            assert sorted(changes(fake)) == [["add", MEMBER], ["add", MOD], ["remove", ALICE]]
            assert all(c["by"] == "Vergil" for c in fake.named("immune") if c["args"][0] != "list")
            await wait_for(lambda: "77777777 and bbbbbbbb can't be vote-kicked now (listed in commands.toml)"
                           in caplog.text)
            assert "cccccccc can be vote-kicked again (no longer in commands.toml)" in caplog.text

            # The host takes the moderator out and lists Alice and two more as members.
            (tmp_path / "commands.toml").write_text(listed(MEMBER, ALICE, "5" * 64, "6" * 64).replace(
                f'[moderator]\nplayers = ["{MOD}"]', ""), encoding="utf-8")
            sup.settings.permissions.mtime = 0  # as if edited
            sup.vergil.check_files()
            await wait_for(lambda: fake.immune_ids() == {BRAVO, MEMBER, ALICE, "5" * 64, "6" * 64})
            await wait_for(lambda: "55555555, 66666666 and cccccccc can't be vote-kicked now" in caplog.text)
            assert "bbbbbbbb can be vote-kicked again" in caplog.text

            # Someone took Charlie off on the console: the next check puts them back.
            fake.immune = [e for e in fake.immune if e["id"] != MEMBER]
            await sup.runners["commands"].part.immune_check()
            assert MEMBER in fake.immune_ids()

    run(scenario())


def test_a_commands_file_gone_for_a_moment_takes_nobody_off_the_list(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, commands=listed()) as (sup, fake, _):
            await wait_for(lambda: fake.immune_ids() == {MOD, MEMBER, OWNER})
            path = tmp_path / "commands.toml"
            text = path.read_text(encoding="utf-8")
            path.unlink()  # an editor replacing the file as it saves
            sup.vergil.check_files()
            assert "commands.toml is missing or empty" in caplog.text
            assert sup.settings.permissions.listed() == {OWNER, MOD, MEMBER}
            await sup.runners["commands"].part.immune_check()
            path.write_text("", encoding="utf-8")  # or emptying it first
            sup.vergil.check_files()
            await sup.runners["commands"].part.immune_check()
            assert not [c for c in changes(fake) if c[0] == "remove"] and fake.immune_ids() == {MOD, MEMBER, OWNER}
            path.write_text(text, encoding="utf-8")  # the save finishes
            os.utime(path, (time.time() + 5, time.time() + 5))
            sup.vergil.check_files()
            assert caplog.text.count("missing or empty") == 2 and "commands.toml reloaded" in caplog.text

    run(scenario())


def test_a_refused_vote_is_explained_to_whoever_called_it(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, commands=listed()) as (sup, fake, _):
            await everyone_joins(fake)
            await fake.join(6, "Charlie", MEMBER)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 6)

            def refused(caller: str, reason: str) -> None:
                sup.bus.emit("log.vote_refused", SERVER, caller=caller, reason=reason)

            refused("Bravo", "Charlie can't be vote-kicked on this server.")
            assert await wait_for(lambda: tells_to(fake, BRAVO)) == [
                "BE A HERO! REPORT VIOLATORS! Charlie is a member here, so no vote can kick them. If they're "
                "breaking the rules, tell the host."]
            assert "Bravo tried to vote-kick Charlie, a member here; the server refused" in caplog.text
            refused("Bravo", "Mod can't be vote-kicked on this server.")  # told a moment ago: not again
            refused("Alice", "Admin can't be vote-kicked on this server.")
            await wait_for(lambda: tells_to(fake, ALICE))
            assert "Admin is an admin here" in tells_to(fake, ALICE)[0]
            refused("Owner", "Bravo can't be vote-kicked on this server.")  # the host's own list: no role to explain
            refused("Owner", "Another vote is under way. Wait for it to end.")
            refused("Mod", "Charlie can't be vote-kicked on this server.")  # handled in order: once Mod is told,
            await wait_for(lambda: tells_to(fake, MOD))                      # the two before are done
            assert len(tells_to(fake, BRAVO)) == 1 and not tells_to(fake, OWNER)
            assert "Owner tried to vote-kick Bravo; the server refused" in caplog.text
            assert not fake.named("cancelvote")

    run(scenario())


def test_on_a_program_without_the_list_a_vote_on_them_is_cancelled(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, commands=listed(), prepare=lambda f: setattr(f, "has_immune", False)) as (
                sup, fake, _):
            await wait_for(lambda: "this server program has no vote-kick immune list" in caplog.text)
            await everyone_joins(fake)
            await fake.join(6, "Charlie", MEMBER)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 6)

            await fake.event("vote", state="called", subject="kick Charlie", caller="Bravo")
            assert (await wait_for(lambda: fake.named("cancelvote")))[0]["by"] == "Vergil"
            await wait_for(lambda: "BE A HERO! REPORT VIOLATORS! Charlie is a member here: no vote can kick them."
                           in says(fake))
            await wait_for(lambda: "cancelled Bravo's vote to kick Charlie, a member here" in caplog.text)
            await fake.event("vote", state="called", subject="kick Mod (A7B)", caller="Alice")  # same-name suffix
            await wait_for(lambda: len(fake.named("cancelvote")) == 2)

            await fake.event("vote", state="called", subject="kick Bravo", caller="Alice")  # nobody protects Bravo
            await fake.join(7, "Charlie", "8" * 64)  # now "Charlie" could rightly mean this one
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 7)
            await fake.event("vote", state="called", subject="kick Charlie", caller="Alice")
            await fake.event("vote", state="passed", subject="kick Mod", caller="Alice")
            await fake.event("vote", state="called", subject="kick Mod", caller="Alice")  # handled in order: once
            await wait_for(lambda: len(fake.named("cancelvote")) >= 3)                  # this one is cancelled,
            assert len(fake.named("cancelvote")) == 3                                    # the three before are done
            assert caplog.text.count("has no vote-kick immune list") == 1  # though it looked again after each

    run(scenario())


def test_dry_run_says_what_it_would_do(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, "dry_run = true", commands=listed()) as (sup, fake, _):
            await wait_for(lambda: "Dry run: would make 77777777, aaaaaaaa and bbbbbbbb vote-kick immune"
                           in caplog.text)
            sup.bus.emit("supervisor.commands_reloaded")  # the same again: not said twice
            await fake.join(1, "Charlie", MEMBER)
            await wait_for(lambda: sup.superintendent.states[SERVER].players)
            await fake.event("vote", state="called", subject="kick Charlie", caller="Bravo")
            await wait_for(lambda: "Dry run: would cancel Bravo's vote to kick Charlie, a member here" in caplog.text)
            assert not changes(fake) and not fake.named("cancelvote")
            assert caplog.text.count("Dry run: would make") == 1

    run(scenario())


def test_what_goes_wrong_is_logged(tmp_path: Path, run, caplog, monkeypatch) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(votekicks, "TIMEOUT", 0.3)
    answers = {"list": {"ok": False, "text": "The ban list can't be read."}}

    def immune(message: dict) -> dict:
        action = message["args"][0]
        if action in answers:
            return answers[action]
        return {"ok": True, "text": "immune", "data": {"configured": [], "added": []}}

    async def scenario() -> None:
        async with running(tmp_path, commands=listed(), prepare=lambda f: f.replies.update(immune=immune)) as (
                sup, fake, _):
            await wait_for(lambda: "couldn't read the vote-kick immune list: The ban list can't be read." in caplog.text)
            del answers["list"]
            answers["add"] = {"ok": False, "text": "The ban list can't be written."}
            part = sup.runners["commands"].part
            await part.immune_check()
            await part.immune_check()
            assert caplog.text.count("couldn't make 77777777 vote-kick immune: The ban list can't be written.") == 1

            await fake.join(1, "Charlie", MEMBER)
            await wait_for(lambda: sup.superintendent.states[SERVER].players)
            fake.replies["cancelvote"] = lambda m: {"ok": False, "text": "No vote is under way."}
            await fake.event("vote", state="called", subject="kick Charlie", caller="Bravo")
            await wait_for(lambda: "couldn't cancel the vote to kick Charlie: No vote is under way." in caplog.text)
            fake.replies["cancelvote"] = lambda m: None  # no answer at all
            await fake.event("vote", state="called", subject="kick Charlie", caller="Bravo")
            await wait_for(lambda: "couldn't cancel the vote to kick Charlie: no reply to 'cancelvote'" in caplog.text)
            assert not says(fake)

    run(scenario())
