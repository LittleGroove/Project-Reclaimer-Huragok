"""!respawn: a stuck player is moved to another team and straight back once the server says the first move is done,
which respawns them on their own team; admins name someone else. Team games during a round only."""

from __future__ import annotations

from pathlib import Path

from conftest import ADMIN, ALICE, BRAVO, MOD, OWNER, wait_for
from testkit.running import SERVER, everyone_joins, running, says, tells_to

from huragok.players.commands import respawn as respawn_module

TEAMS = {"Owner": "Red", "Mod": "Blue", "Alice": "Red", "Bravo": "Blue", "Admin": "Blue"}


def told(fake, player_id: str, text: str) -> bool:
    return any(text in line for line in tells_to(fake, player_id))


def moves(fake) -> list[list[str]]:
    return [c["args"] for c in fake.named("team")]


async def in_a_team_round(sup, fake, *, joined: bool = False) -> None:
    state = sup.superintendent.states[SERVER]
    if not joined:
        await everyone_joins(fake)
        await wait_for(lambda: len(state.players) == 5)
    for entry in fake.players:  # as the next look at `players` will have them
        entry["team"] = TEAMS[entry["name"]]
    for player in state.players.values():
        player.team = TEAMS[player.name]
    await fake.event("phase", phase="playing", map="Guardian", mode="Team Slayer")
    await wait_for(lambda: state.phase == "playing")


async def answered(fake, count: int, text: str = "", ok: bool = True) -> None:
    """The server's outcome of the move just sent: once Huragok has sent `count` moves."""
    await wait_for(lambda: len(moves(fake)) == count)
    await fake.event("control", action="team", ok=ok, text=text)


def test_a_player_respawns_themselves_by_a_move_away_and_back(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await in_a_team_round(sup, fake)

            await fake.chat(ALICE, "Alice", "!respawn stuck in a wall")
            await answered(fake, 1, "Alice was moved to Blue team.")
            await answered(fake, 2, "Alice was moved to Red team.")
            await wait_for(lambda: "[o_o] DETOUR! PROCEED WITH CAUTION! Alice respawned on Red team: stuck in a wall."
                           in says(fake))
            assert moves(fake) == [[ALICE, "blue"], [ALICE, "red"]]
            assert fake.named("team")[0]["by"] == "Alice"

            # Once a minute for their own (admins aren't held to it).
            await fake.chat(ALICE, "Alice", "!respawn")
            await wait_for(lambda: told(fake, ALICE, "You can respawn again in "))
            assert len(moves(fake)) == 2

            # A player's words are their reason, never someone else's name.
            await fake.chat(BRAVO, "Bravo", "!respawn Alice")
            await answered(fake, 3)
            await answered(fake, 4)
            await wait_for(lambda: "[o_o] DETOUR! PROCEED WITH CAUTION! Bravo respawned on Blue team: Alice."
                           in says(fake))
            assert moves(fake)[2:] == [[BRAVO, "red"], [BRAVO, "blue"]]

    run(scenario())


def test_an_admin_respawns_someone_else_but_not_their_own_rank(tmp_path: Path, run) -> None:
    async def scenario() -> None:
        async with running(tmp_path, prepare=lambda f: f.admins_configured.append(ADMIN)) as (sup, fake, _):
            await in_a_team_round(sup, fake)

            await fake.chat(ADMIN, "Admin", "!respawn Bravo under the map")
            await answered(fake, 1)
            await answered(fake, 2)
            await wait_for(lambda: "[o_o] DETOUR! PROCEED WITH CAUTION! Admin respawned Bravo on Blue team: under the "
                           "map." in says(fake))
            assert moves(fake) == [[BRAVO, "red"], [BRAVO, "blue"]]

            await fake.chat(ADMIN, "Admin", "!respawn Owner")
            await wait_for(lambda: told(fake, ADMIN, "Owner is an owner; you can't do that to them."))
            await fake.chat(ADMIN, "Admin", "!respawn")  # their own, again at once: an admin isn't held
            await answered(fake, 3)
            await answered(fake, 4)
            await wait_for(lambda: "[o_o] DETOUR! PROCEED WITH CAUTION! Admin respawned on Blue team." in says(fake))
            await fake.chat(OWNER, "Owner", "!respawn Admin")  # an owner may
            await wait_for(lambda: len(moves(fake)) == 5)

    run(scenario())


def test_what_the_server_refuses_and_when_theres_no_round_or_no_teams(tmp_path: Path, run, monkeypatch) -> None:
    monkeypatch.setattr(respawn_module, "ANSWER_SECONDS", 0.5)

    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await everyone_joins(fake)
            state = sup.superintendent.states[SERVER]
            await wait_for(lambda: len(state.players) == 5)
            await fake.chat(ALICE, "Alice", "!respawn")  # in the lobby
            await wait_for(lambda: told(fake, ALICE, "There's no round being played to respawn you in."))
            await fake.event("phase", phase="playing", map="Guardian", mode="Slayer")
            await wait_for(lambda: state.phase == "playing")
            await fake.chat(ALICE, "Alice", "!respawn")  # free-for-all: nobody has a team
            await wait_for(lambda: told(fake, ALICE, "This game has no teams, so there's no way to respawn anyone"))
            assert not moves(fake)

            await in_a_team_round(sup, fake, joined=True)
            # A team's last player: the server refuses the first move, and they read why.
            await fake.chat(MOD, "Mod", "!respawn")
            await answered(fake, 1, "A team's last player can't be moved during a round.", ok=False)
            await wait_for(lambda: told(fake, MOD, "Couldn't respawn you: A team's last player can't be moved during "
                                                   "a round."))
            assert len(moves(fake)) == 1

            # The move back refused: they're told where they are.
            await fake.chat(OWNER, "Owner", "!respawn")
            await answered(fake, 2)
            await answered(fake, 3, "Teams are locked until the next round starts.", ok=False)
            await wait_for(lambda: told(fake, OWNER, "You respawned, but on Blue team: the server wouldn't move you "
                                                     "back (Teams are locked until the next round starts.)."))

            # No answer at all: each move is taken as done after a moment.
            await fake.chat(BRAVO, "Bravo", "!respawn")
            await wait_for(lambda: "[o_o] DETOUR! PROCEED WITH CAUTION! Bravo respawned on Blue team." in says(fake),
                           timeout=10)
            assert moves(fake)[3:] == [[BRAVO, "red"], [BRAVO, "blue"]]

    run(scenario())


def test_the_team_they_go_back_to_is_the_one_the_server_has_now(tmp_path: Path, run) -> None:
    """Huragok's own list is up to 30 s old: someone moved since goes back to the team they're on now."""
    async def scenario() -> None:
        async with running(tmp_path) as (sup, fake, _):
            await in_a_team_round(sup, fake)
            next(e for e in fake.players if e["name"] == "Alice")["team"] = "Blue"  # moved a moment ago
            assert sup.superintendent.states[SERVER].player_by_id(ALICE).team == "Red"
            await fake.chat(ALICE, "Alice", "!respawn")
            await answered(fake, 1)
            await answered(fake, 2)
            await wait_for(lambda: "[o_o] DETOUR! PROCEED WITH CAUTION! Alice respawned on Blue team." in says(fake))
            assert moves(fake) == [[ALICE, "red"], [ALICE, "blue"]]

    run(scenario())
