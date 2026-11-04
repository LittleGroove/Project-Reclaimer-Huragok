"""What a kill was made with, from the console's `kill` event: its "report" byte (0.6.1).

Worked out from the live servers' copies, 3 November 2026: matched against the game reports' kills by weapon in 75
games, the byte is Halo 3's damage reporting type, the same list as the reports' weapon names (the report's own
"damage_61" is byte 61). Its low six bits are the weapon; +64 is a headshot (it matched the reports' headshots in
1,716 of 1,719 player-games); +128 on a grenade is a stick (1,719 of 1,719), on a melee from behind (an
assassination, 95%); +192 on a vehicle is a splatter (92%), and on anything else isn't named (it's on the Warthog's
and the Wraith's guns too, audit 0.6.1, 1.9). 1, 5, 7, 10, 23, 30 and 48 follow Halo 3's own list, as every kind
matched here does; 43, 53 and 61 aren't known yet.
"""

from __future__ import annotations

from dataclasses import dataclass

NAMES = {
    0: "the Guardians", 1: "a fall", 2: "a collision", 3: "melee", 4: "an explosion", 5: "Magnum", 6: "Plasma Pistol",
    7: "Needler", 8: "Mauler", 9: "SMG", 10: "Plasma Rifle",
    11: "Battle Rifle", 12: "Carbine", 13: "Shotgun", 14: "Sniper Rifle", 15: "Beam Rifle", 16: "Assault Rifle",
    17: "Spiker", 18: "Fuel Rod Gun", 19: "Missile Pod", 20: "Rocket Launcher", 21: "Spartan Laser", 22: "Brute Shot",
    23: "Flamethrower", 24: "Sentinel Beam", 25: "Energy Sword", 26: "Gravity Hammer", 27: "Frag Grenade",
    28: "Plasma Grenade", 29: "Spike Grenade", 30: "Firebomb Grenade", 31: "the flag", 35: "Machine Gun Turret", 36: "Plasma Cannon", 39: "Banshee", 40: "Ghost",
    41: "Mongoose", 44: "Spectre", 45: "Spectre gun", 46: "Warthog", 47: "Warthog gun", 48: "Gauss gun", 49: "Wraith", 50: "Wraith gun",
    51: "Scorpion", 57: "Sentinel", 58: "a teleporter", 60: "Elephant turret",
}
GRENADES = {27, 28, 29}
MELEE = {3, 25, 26}
VEHICLES = {39, 40, 41, 44, 46, 49, 51}


@dataclass(frozen=True)
class How:
    weapon: str  # "Battle Rifle", or "something" for a type nobody's named (a mod's own)
    headshot: bool = False
    stuck: bool = False
    from_behind: bool = False
    splatter: bool = False

    def words(self) -> str:
        extra = ("headshot" if self.headshot else "stuck" if self.stuck else "from behind" if self.from_behind
                 else "splatter" if self.splatter else "")
        return f"{self.weapon}, {extra}" if extra else self.weapon


def how(report: object) -> How:
    """What the byte says. Anything that isn't a byte is "something"."""
    value = report if isinstance(report, int) and not isinstance(report, bool) and 0 <= report < 256 else -1
    if value < 0:
        return How("something")
    kind, flags = value & 63, value & 192
    weapon = NAMES.get(kind, "something")
    if flags == 192:
        return How(weapon, splatter=kind in VEHICLES)
    if flags == 64:
        return How(weapon, headshot=True)
    if flags == 128:
        return How(weapon, stuck=kind in GRENADES, from_behind=kind in MELEE)
    return How(weapon)


__all__ = ["GRENADES", "How", "MELEE", "NAMES", "VEHICLES", "how"]
