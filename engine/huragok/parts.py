"""Every part, the Superintendent's and Vergil's:

  commands   in-game commands; commands.toml says which role may use each (always on)
  automod    the word list, automod.txt: removes players for the words on it
  watchdog   notices a struggling server and restarts it at a good moment
  eventlog   every event as JSON, to see what the server sends
  tempdiscord  temporary (0.5.2 until 0.6.0): what the Discord bot and players' stats will need (always on)
  messages   interval messages, from intervalmessages.txt: for every server, and each server's own (always on)

The parts with settings each have a section in settings.toml ([automod]…). To
add a part, write it in the folder of the named part it belongs to (servers/ for
the Superintendent's, players/ for Vergil's), give it that `persona`, and list it
here; tests/core/test_part_system.py checks that no two parts claim the same chat
command or settings section.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from .core.part import Part
from .players.automod import AutoMod
from .players.commands import Commands
from .players.messages import Messages
from .servers.eventlog import EventLog
from .servers.tempdiscord import TempDiscord
from .servers.watchdog import Watchdog

if TYPE_CHECKING:
    from .core.settings import Settings

PARTS: dict[str, type[Part]] = {cls.name: cls for cls in (Commands, AutoMod, Watchdog, EventLog, Messages, TempDiscord)}


def config_problem(settings: Settings, name: str, cls: Any) -> str | None:
    """What stops a part at the start (its check_config), without starting it: the part made as the
    Superintendent makes it, on a stand-in for the running Huragok. `check` and the window's settings use it."""
    config = {**cls.defaults, **settings.parts[name].options}
    ctx = SimpleNamespace(name=name, config=config, home=settings.home, dry_run=True,
                          acts_as=str(config.get("name") or cls.acts_as or name.title()),
                          log=logging.getLogger(f"part.{name}"))
    try:
        cls(ctx).check_config()
    except ValueError as error:
        return str(error)
    except Exception as error:  # noqa: BLE001 - a part that needs the running Huragok to check itself
        logging.getLogger(__name__).debug("%s's check_config needs more than check gives it: %s", name, error)
    return None


__all__ = ["PARTS", "config_problem"]
