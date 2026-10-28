"""!novote: a player kept from calling votes has each one cancelled as it's called, and is told why; they still
answer everyone else's. For a time or until !allowvote, by player ID, through restarts, on every server or one."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, wait_for
from testkit.running import SERVER, everyone_joins, running, says, tells_to


def told(fake, player_id: str, text: str) -> bool:
    return any(text in line for line in tells_to(fake, player_id))


def cancels(fake) -> int:
    return len(fake.named("cancelvote"))


def rows(sup) -> list:
    return sup.storage.query("SELECT * FROM commands_no_votes ORDER BY number")


async def on_and_ready(sup, fake) -> None:
    await everyone_joins(fake)
    await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)


def test_a_player_whose_votes_are_away_has_each_vote_cancelled_until_given_back(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_and_ready(sup, fake)

            await fake.chat(MOD, "Mod", "!novote Bravo 10m spamming votes")
            await wait_for(lambda: "[-_-] OBEY POSTED LIMITS! Mod took Bravo's votes away for 10 minutes: spamming "
                                   "votes. Any vote they call is cancelled." in says(fake))
            row = rows(sup)[0]
            assert (row["target"], row["name"], row["by"], row["reason"], row["server"], row["length"]) == \
                (BRAVO, "Bravo", "Mod", "spamming votes", SERVER, "10 minutes")
            assert 590 < row["until"] - time.time() <= 600

            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            assert (await wait_for(lambda: fake.named("cancelvote")))[0]["by"] == "Vergil"
            await wait_for(lambda: told(fake, BRAVO, "[-_-] OBEY POSTED LIMITS! Your vote was cancelled: Mod took your "
                                                     "votes away for another 9 minutes"))
            assert told(fake, BRAVO, "(spamming votes). You can still vote on other players' votes.")
            assert "cancelled Bravo's vote to end the game: their votes are away for another" in caplog.text

            # Calling again at once: cancelled again, but told only once in a while.
            await fake.event("vote", state="called", subject="kick Alice", caller="Bravo")
            await wait_for(lambda: cancels(fake) == 2)
            # Everyone else's votes go ahead (handled in order: once Bravo's next one is cancelled, these are done).
            await fake.event("vote", state="called", subject="kick Bravo", caller="Alice")
            await fake.event("vote", state="passed", subject="end the game", caller="Bravo")
            await fake.event("vote", state="called", subject="end the round", caller="Bravo")
            await wait_for(lambda: cancels(fake) == 3)
            assert len([t for t in tells_to(fake, BRAVO) if "Your vote was cancelled" in t]) == 1

            # The list, and giving them back by its @number.
            await fake.chat(MOD, "Mod", "!novotes")
            await wait_for(lambda: told(fake, MOD, "@1 #4 Bravo ("))
            assert told(fake, MOD, "votes away for another 9 minutes, by Mod 0s ago: spamming votes")
            assert told(fake, MOD, "!allowvote @number gives one their votes back.")
            await fake.chat(MOD, "Mod", "!allowvote @1")
            await wait_for(lambda: told(fake, MOD, "Bravo can call votes again."))
            assert told(fake, BRAVO, "[^_^] GOOD CITIZENS DO THEIR PART! Mod gave your votes back: you can call votes "
                                     "again.")
            assert rows(sup)[0]["lifted_by"] == "Mod"
            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            await fake.chat(MOD, "Mod", "!allowvote Bravo")  # handled after the vote: nothing to give back
            await wait_for(lambda: told(fake, MOD, "Bravo can call votes already."))
            assert cancels(fake) == 3
            await fake.chat(MOD, "Mod", "!novotes")
            await wait_for(lambda: told(fake, MOD, "Everyone here can call votes"))

    run(scenario())


def test_no_time_is_until_given_back_and_a_new_time_replaces_the_old(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_and_ready(sup, fake)

            await fake.chat(MOD, "Mod", "!novote Bravo")
            await wait_for(lambda: "[-_-] OBEY POSTED LIMITS! Mod took Bravo's votes away until a moderator gives "
                                   "them back. Any vote they call is cancelled." in says(fake))
            assert rows(sup)[0]["until"] is None
            await fake.chat(MOD, "Mod", "!novote Bravo 2h")
            await wait_for(lambda: told(fake, MOD, "Bravo's votes were already away (until a moderator gives them "
                                                   "back); now for 2 hours."))
            first, second = rows(sup)
            assert first["lifted"] and second["until"] and second["lifted"] is None

            # Once the time is up, their votes go ahead (and !whois said so meanwhile).
            await fake.chat(MOD, "Mod", "!whois Bravo")
            await wait_for(lambda: told(fake, MOD, "Now: can't call votes, 1h more (by Mod)"))
            sup.storage.execute("UPDATE commands_no_votes SET until = ?", (time.time() - 1,))
            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            await fake.chat(MOD, "Mod", "!allowvote Bravo")
            await wait_for(lambda: told(fake, MOD, "Bravo can call votes already."))
            assert cancels(fake) == 0

    run(scenario())


def test_nobody_takes_the_votes_of_their_own_role_or_above_and_bad_times_are_refused(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await on_and_ready(sup, fake)
            await fake.chat(MOD, "Mod", "!novote Owner 10m")
            await wait_for(lambda: told(fake, MOD, "Owner is an owner; you can't do that to them."))
            await fake.chat(MOD, "Mod", "!novote Admin")
            await wait_for(lambda: told(fake, MOD, "Admin is an admin; you can't do that to them."))
            await fake.chat(ALICE, "Alice", "!novote Bravo")
            await wait_for(lambda: told(fake, ALICE, "!novote is for moderators and up"))
            await fake.chat(MOD, "Mod", "!novote Bravo 0m")
            await wait_for(lambda: told(fake, MOD, "Use: !novote <player> [time | perm] [reason]"))
            assert not rows(sup)
            await fake.chat(OWNER, "Owner", "!novote Mod perm")  # perm: the same as no time
            await wait_for(lambda: rows(sup))
            assert rows(sup)[0]["until"] is None and rows(sup)[0]["reason"] == ""

    run(scenario())


def test_someone_who_left_has_it_when_they_rejoin_and_a_shared_name_keeps_their_vote(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await on_and_ready(sup, fake)
            await fake.leave(4)  # Bravo
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 4)
            await fake.chat(MOD, "Mod", "!novote Bravo 1h")
            await wait_for(lambda: told(fake, MOD, "Bravo isn't on now: any vote they call once they're back is "
                                                   "cancelled, for 1 hour from now."))
            assert not [line for line in says(fake) if "OBEY POSTED LIMITS" in line]

            await fake.join(6, "Bravo", BRAVO)  # back: under the same ID, so still no votes
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 5)
            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            await wait_for(lambda: cancels(fake) == 1)

            # Another Bravo joins: "Bravo" could be either, so the vote isn't taken from someone who may have it.
            await fake.join(7, "Bravo", "8" * 64)
            await wait_for(lambda: len(sup.superintendent.states[SERVER].players) == 6)
            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            await fake.chat(MOD, "Mod", "!allowvote " + BRAVO)  # handled after the vote
            await wait_for(lambda: told(fake, MOD, "Bravo can call votes again."))
            assert cancels(fake) == 1

    run(scenario())


def test_a_dry_run_only_says_what_it_would_cancel(tmp_path: Path, run, caplog) -> None:
    caplog.set_level(logging.INFO)

    async def scenario() -> None:
        async with running(tmp_path, "dry_run = true") as (sup, fake, _):
            await on_and_ready(sup, fake)
            await fake.chat(MOD, "Mod", "!novote Bravo 10m")
            await wait_for(lambda: told(fake, MOD, "Dry run: their votes are only logged, not cancelled. Players would "
                                                   "read: [-_-] OBEY POSTED LIMITS! Mod took Bravo's"))  # wrapped there
            assert rows(sup) and not [line for line in says(fake) if "OBEY POSTED LIMITS" in line]
            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            await wait_for(lambda: "Dry run: would cancel Bravo's vote to end the game (their votes are away)"
                           in caplog.text)
            assert cancels(fake) == 0 and not told(fake, BRAVO, "Your vote was cancelled")

    run(scenario())


def test_with_across_servers_off_it_is_the_servers_it_was_given_on(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, "[automod]\nacross_servers = false\n") as (sup, fake, _):
            await on_and_ready(sup, fake)
            await fake.chat(MOD, "Mod", "!novote Bravo 10m")
            await wait_for(lambda: rows(sup))
            part = sup.runners["commands"].part
            assert part._no_vote_row(BRAVO, SERVER) is not None
            assert part._no_vote_row(BRAVO, "Other Server") is None
            sup.storage.execute("UPDATE commands_no_votes SET server = 'Other Server'")
            await fake.event("vote", state="called", subject="end the game", caller="Bravo")
            await fake.chat(MOD, "Mod", "!novotes")  # handled after the vote
            await wait_for(lambda: told(fake, MOD, "Everyone here can call votes"))
            assert cancels(fake) == 0

    run(scenario())
