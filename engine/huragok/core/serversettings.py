"""What each server uses: its own setting, from its [[server]] block in settings.toml, or Huragok's for every server.

settings.toml sets most things for every server; a [[server]] block (`name = "Slayer"`) sets some of them again for
that one server. This is the one place that says which wins and what the setting in force means, so the
Superintendent, Vergil, the window, `check` and the status card all say the same thing:

  [[server]] key       for every server                    in force
  autostart            (its own only; on unless set)        its own
  daily_restart        [restarts] daily_restart             its own, else every server's
  warn_seconds         [restarts] warn_seconds              its own, else every server's
  min_uptime_seconds   [restarts] min_uptime_seconds        its own, else every server's
  workshop_updates     [workshop_updates] restart           on (restarted to put in a mod update its map in use
                                                            holds up) or off (only warned about); 0.6's
                                                            after_game and when_empty are read as unset
  priority             [process] priority                   "off" unless the process backend runs it
  automod              [automod] dry_run                    "off" while [automod] enabled = false
  service, data, rcon_host, rcon_port                       where the server is: its own only

A new per-server setting is a row in OVERRIDES, and a line in templates/settings.toml's commented [[server]]
example; the settings a person may change from the window are in CHANGEABLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .priority import PRIORITIES
from .priority import DEFAULT as DEFAULT_PRIORITY

if TYPE_CHECKING:
    from .serverfolders import ServerConfig
    from .settings import Settings

AUTOMOD_MODES = ("on", "dry_run", "off")  # Vergil on one server
WORKSHOP_UPDATES = ("on", "off")  # a server restarted for its mod updates, or only warned about
WORKSHOP_OLD = ("after_game", "when_empty")  # 0.6's values: still read, as unset (the general setting applies)


@dataclass(frozen=True)
class Override:
    key: str  # in a [[server]] block
    section: str = ""  # the settings.toml section it sets again for one server ("": one only a server has)
    general: str = ""  # the key there
    choices: tuple[str, ...] = ()


OVERRIDES = {override.key: override for override in (
    Override("autostart"),
    Override("daily_restart", "restarts", "daily_restart"),
    Override("warn_seconds", "restarts", "warn_seconds"),
    Override("min_uptime_seconds", "restarts", "min_uptime_seconds"),
    Override("workshop_updates", "workshop_updates", "restart", WORKSHOP_UPDATES),
    Override("priority", "process", "priority", PRIORITIES),
    Override("automod", "automod", "dry_run", AUTOMOD_MODES),
    Override("service"), Override("data"), Override("rcon_host"), Override("rcon_port"),
)}
CHANGEABLE = ("autostart", "daily_restart", "workshop_updates", "priority", "automod")  # from the window (D)
SERVER_KEYS = {"name", *OVERRIDES}  # what a [[server]] block may hold

# What Huragok does about a server's Workshop mod updates, as the host reads it (the server downloads and installs
# them itself; its map in use can hold one up, which only a restart frees).
WORKSHOP_HOW = {"on": "the server installs its own mod updates; Huragok restarts it when its map in use holds one up",
                "off": "the server installs its own mod updates; Huragok only warns when its map in use holds one up"}


def automod_mode(own: Any, dry_run: bool, everything_dry: bool = False) -> str:
    """Vergil on a server: its own [[server]] automod, else dry_run while [automod] (or all of Huragok) is in dry
    run, else on. While all of Huragok is (settings.toml's own dry_run), a server's own "on" is a dry run too: the
    ledger sends nothing then, and players mustn't be told of strikes that never land."""
    if own in AUTOMOD_MODES:
        return "dry_run" if everything_dry and own == "on" else str(own)
    return "dry_run" if dry_run or everything_dry else "on"


class ServerSettings:
    """One server's settings as Huragok has them in force: `settings.of(server)`."""

    def __init__(self, settings: Settings, server: ServerConfig) -> None:
        self.settings = settings
        self.server = server

    def own(self, key: str) -> Any:
        """The server's own value, from its [[server]] block, or None when it has none."""
        block = self.settings.server_overrides.get(self.server.name.lower(), {})
        if key in block:
            value = block[key]
        elif key in ("daily_restart", "workshop_updates", "priority", "automod"):
            value = getattr(self.server, key, None)  # as the block was read with its dedicated.toml
        else:
            return None
        return None if key == "workshop_updates" and value in WORKSHOP_OLD else value

    def where(self, key: str) -> str:
        """Where the value in force comes from: "settings.toml" (its own [[server]] block) or the section for every
        server ("[restarts]"), as the window shows it."""
        if key == "automod" and self.automod == "off" and self.own(key) != "off":
            return "[automod] enabled = false"  # the switch for every server wins over a server's own "on"
        if self.own(key) is not None:
            return "settings.toml"
        override = OVERRIDES[key]
        return f"[{override.section}]" if override.section else "default"

    def value(self, key: str) -> Any:
        """The value in force for any of OVERRIDES."""
        if key in ("priority", "automod", "workshop_updates", "daily_restart", "autostart"):
            return getattr(self, key)
        if OVERRIDES[key].section == "restarts":
            return self.restart(key)
        return self.own(key)

    # Each setting as it's meant --------------------------------------------------------------------------------

    @property
    def autostart(self) -> bool:
        return bool(self.server.autostart)

    def restart(self, key: str) -> Any:
        """A [restarts] value as this server has it: its own [[server]] one (warn_seconds, min_uptime_seconds,
        daily_restart), else every server's."""
        own = self.own(key)
        return own if own is not None else getattr(self.settings.restarts, key)

    @property
    def daily_restart(self) -> str:
        """"HH:MM", or "" for none."""
        return str(self.restart("daily_restart") or "")

    @property
    def priority(self) -> str:
        """The priority its processes run at. Only the process backend runs them, so only it sets one."""
        if self.settings.backend != "process":
            return "off"
        return str(self.own("priority") or self.settings.backend_options.get("priority", DEFAULT_PRIORITY))

    @property
    def workshop_updates(self) -> str:
        """"on": Huragok restarts the server to put in a mod update its map in use holds up; "off": it only warns.
        Its own [[server]] value, else [workshop_updates] restart."""
        own = self.own("workshop_updates")
        if own in WORKSHOP_UPDATES:
            return str(own)
        return "on" if self.settings.workshop_updates.restart else "off"

    @property
    def workshop_restart(self) -> bool:
        return self.workshop_updates == "on"

    def shares_content_with(self) -> list[str]:
        """Other servers using the same content folder, so the same installed Workshop items."""
        if self.server.content_dir is None:
            return []
        return [s.name for s in self.settings.servers
                if s is not self.server and s.content_dir == self.server.content_dir]

    def workshop_text(self) -> str:
        """What happens to its mod updates, with the servers it shares them with (one update reaches all of them)."""
        shared = self.shares_content_with()
        return WORKSHOP_HOW[self.workshop_updates] + (f" (content shared with {', '.join(shared)}: one update "
                                                      "reaches all of them at once)" if shared else "")

    @property
    def automod(self) -> str:
        """Vergil on this server: on, dry_run (it only logs what it would do) or off."""
        part = self.settings.parts.get("automod")
        if part is None or not part.enabled:
            return "off"
        return automod_mode(self.own("automod"), part.dry_run or self.settings.dry_run, self.settings.dry_run)
