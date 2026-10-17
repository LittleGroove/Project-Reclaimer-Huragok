# How the supervisor is built

The supervisor is one program. Its core runs the servers and holds the pieces
everything shares (one RCON connection per server, the live state of each
server, the event bus, the records). On top of it run its **parts**:

| Part | Folder | Settings | |
| --- | --- | --- | --- |
| `commands` | [parts/commands](../engine/reclaimer_supervisor/parts/commands) | `commands.toml` | The in-game commands. Always on. |
| `automod` | [parts/automod](../engine/reclaimer_supervisor/parts/automod) | `[automod]`, `automod.txt` | The word list |
| `watchdog` | [parts/watchdog.py](../engine/reclaimer_supervisor/parts/watchdog.py) | `[watchdog]` | Restarts a server that died or got stuck after a game; notices a slow one |
| `eventlog` | [parts/eventlog.py](../engine/reclaimer_supervisor/parts/eventlog.py) | `[eventlog]` | Every event as JSON |

Parts are a way to keep the code apart, not something hosts install: everything
ships together, and new work comes as a pull request. Every command shares one
set of names, and [tests/test_parts.py](../engine/tests/test_parts.py) fails if
two parts claim the same one.

## The core, module by module
| Module | |
| --- | --- |
| `config.py` | `settings.toml`, and the servers: every `SERVERS/<folder>/dedicated.toml` (plus `server_folder`), checked against each other (unique names and ports; RCON on). `discover_servers()` re-reads them for folders added while running. |
| `manager.py` | **One `ServerManager` per server**: the state machine below, ticked once a second by the `Fleet`. The `Fleet` also writes `records/live.json`, answers requests from `records/requests/`, and is what the window's keys call. `Lifecycle` is the thin view parts get (`restart`, `cancel`, `planned`, `busy`). |
| `lifecycle.py` | The **backends**: `process` (one `dedicated` per server, Windows or Linux), `docker`, `command`, `attach`. A backend only starts, stops and watches processes; it decides nothing. `Keepalive` restarts a hung server. |
| `menu.py`, `tui.py` | The **window**: the dashboard, its screens, the keyboard (Windows `msvcrt`, Linux `termios`), colours and in-place redraw, the recent-events log handler. `RemoteController` is the second window, fed by `live.json`. |
| `mailbox.py` | Requests from outside (`start.cmd stop`, a second window) and their replies, as files. |
| `rcon.py`, `state.py`, `files.py` | One RCON connection per server; the live picture of each server; `server.log`, `status.json` and the console output as events. |
| `moderation.py`, `permissions.py`, `chat.py` | One ledger for every warn/mute/kick/ban; `commands.toml`; `!` commands in chat. |
| `storage.py`, `updates.py`, `desktop.py`, `watch.py`, `tomledit.py` | SQLite; new versions from GitHub; opening folders, editors, log windows, start at boot; following a log; editing `settings.toml` and `dedicated.toml` in place (keys, tables, `[[server]]` blocks), keeping comments. |
| `dedicated.py`, `newserver.py`, `packages.py` | **Setting servers up**: running the server program's own `dedicated` commands (exe on Windows, wine on Linux) and reading their listings; making a server folder from a package with the root's shared `game/`, `content/` and `bans.json`; packages (`PACKAGES/`, the starters in `templates/packages`). |
| `menu_setup.py`, `setupcli.py` | The window's N, G, C and E screens; the `new`, `package`, `content`, `playlist`, `workshop` and `dedicated` commands. Both work without the fleet, so a second window has them too. |
| `catalogue.py`, `playlists.py`, `steamsetup.py`, `contentimport.py` | What a server can play as one catalogue (the listing, the mods' ModInfo.json and Forge layouts, the Workshop items) with numbered picking; playlist files built and edited from picks; SteamCMD found, installed, signed in and written into every server; MCC's variants imported. |
| `livesettings.py`, `menu_manage.py` | **Managing a running server**: the settings catalogue (what each setting is, how it's typed, when the server applies it) and the window's A (console with events), D (settings), B (bans, admins, VPN) and U (updates) screens. |
| `serverupdates.py` | New releases of the server program: found on GitHub, compared with the version each server reports, checked against `SHA256SUMS.txt`, put in place of every copy, servers restarted after their game. |
| `relay.py` | The console relay: the server's WebSocket console protocol served by the supervisor, one port per server, moderators by password and role, commands gated by `commands.toml`'s lists and forwarded with `by`, events fanned out. |
| `core.py`, `cli.py` | Wiring; the commands `start`, `status`, `stop`, `restart`, `say`, `rcon`, `watch`, `check`, `update`, `init`. |

### A server's phases
```text
idle ──► launch ──► booting ──► running ──► countdown ──► stopping ──► idle | launch
  ▲                   │                        │  (cancel)                  ▲
  │                   ▼                        ▼                            │
  │                backoff ──► faulted      running                   restarting (a backend
  │                   ▲                                                with only `restart`)
  └── external (up, but not started from here) · offline (attach: not reachable)
```
Up means **RCON answers**, not the process existing. A crash backs off
(`backoff_seconds`); `max_failures` fast failures in a row, or more than
`restarts_per_hour` automatic restarts, mean `faulted`, and a person starts it
again. A countdown says the `warn_seconds` rungs as absolute deadlines (if a
slow tick lets two fall due at once, the smaller is said), skips itself when
nobody is on, and ends early once everyone has left. Nothing in a manager
waits: a stop in progress is a task the next tick looks at. What survives
closing the window is in `records/servers/<server>.json`.

## Adding a command
Most new things are commands. Put one in the file of
[parts/commands](../engine/reclaimer_supervisor/parts/commands) it fits
(`players.py` for looking people up, `moderation.py` for acting on them,
`server.py` for running the server), or start a new file for a new group and add
its class to `Commands` in `parts/commands/__init__.py`.

```python
# parts/commands/players.py
@command("seen", usage="<name>", help="When a player was last here", role="moderator")
async def seen(self, call: CommandCall) -> None:
    found = self.ctx.players.find(call.text)
    if not found:
        await call.reply(f"Nobody called '{call.text}' has been seen.")
        return
    when = time.strftime("%Y-%m-%d", time.gmtime(found[0].last_seen))
    await call.reply(f"{found[0].last_name} was last here on {when}.")
```

- `role` is only the default: hosts move commands between roles in
  `commands.toml`, and the file the first start writes lists every command with
  its `help` text, so give every command a `help` that says what it does.
- Commands are public chat: everyone in the channel sees them. Answer with
  `call.reply(text)`, which only the caller reads (long text is split into lines).
- `call.player`, `call.role`, `call.server`, `call.state`, `call.prefix`,
  `call.args` (split like a shell, so `"two words"` stay together) and `call.text`
  (the rest of the line as typed).
- `await call.find_player(ref)` resolves `#3`, a player ID, an exact name or the
  start of one, and tells the caller when it matches nobody or several. Names
  aren't unique (one real night had 30 players called "Player"), so prefer `#numbers`.
- `self._target(call, ref)` does the same and refuses players of the caller's
  role or above; use it for anything done *to* a player.
- `self._rcon(call, "command", *args)` passes a line to the server's console as
  the caller and shows them the answer; `self._line(call, "name")` passes the
  console command of the same name with the caller's words.
- Pick a name that says what the command does. `!yes` and `!no` are the server's
  votes; lines starting with `/` are the game's own.

## Adding a part
For something that isn't a command, such as watching events, running on a timer,
or keeping state of its own, write a part: a module (or a package, for a bigger
one) in [parts](../engine/reclaimer_supervisor/parts) with one `Part` subclass,
listed in `parts/__init__.py`.

```python
# parts/greeter.py
from ..chat import CommandCall
from ..events import Event
from ..part import Part, command, every, on


class Greeter(Part):
    name = "greeter"                       # its section in settings.toml: [greeter]
    description = "Welcomes players and answers !discord"
    acts_as = "Greeter"                    # players read "kicked by Greeter"; the host's `name` setting overrides
    defaults = {"invite": ""}              # settings the host may leave out
    enabled_by_default = False             # on unless [greeter] enabled = false, or the other way round

    def check_config(self) -> None:        # before setup; ValueError switches only this part off
        if not isinstance(self.config["invite"], str):
            raise ValueError("invite must be text")

    async def setup(self) -> None:         # once, before any event
        ...

    @on("rcon.join")
    async def welcome(self, event: Event) -> None:
        record = self.ctx.players.get(event["id"])
        if record and record.visits > 1:
            await self.ctx.tell(event.server, event["id"], f"Welcome back, {event['name']}!")

    @command("discord", help="Our Discord invite")
    async def discord(self, call: CommandCall) -> None:
        await call.reply(self.config["invite"] or "No Discord set.")

    @every(minutes=10)
    async def remind(self) -> None:
        ...

    async def teardown(self) -> None:      # once, when the supervisor stops
        ...
```

Then add its section to
[templates/settings.toml](../engine/reclaimer_supervisor/templates/settings.toml),
tests under `engine/tests/`, and a line in the README. `check` shows each part,
whether it's on and any problem with its settings; a part that reads its own
files can check them there too by defining `preflight(config, home)`, as automod
does with its word list.

## What `self.ctx` gives a part
| | |
| --- | --- |
| `ctx.servers` | Each server's live picture by name: `players` (by `#number`: `name`, `id`, `ip`, `admin`, `muted`, `team`), `phase`, `map`, `mode`, `max_players`, `admins`, `connected`, `status_age`, `rcon_latency_ms`. Servers added while running appear here. |
| `ctx.server(name)` | One of those |
| `ctx.say(server, text)` | A `[Server]` line everyone reads |
| `ctx.tell(server, player, text)` | A `[Server]` line one player reads (a `Player` or a player ID) |
| `ctx.players.get(id)`, `.find(text)` | Everyone ever seen on this machine: names used, IPs, first and last seen, visits |
| `ctx.role_of(server, player)` | `player` (everyone), `moderator`, `admin` (the server's own admins) or `owner` |
| `ctx.moderation` | `warn`, `mute`, `unmute`, `kick`, `ban`, `unban`, `pardon`: recorded with a reason, shown to players as by your part (or the `by=` you pass), and dry-run aware. Use these for anything done to a player |
| `ctx.lifecycle` | `restart(server, when="now" / "after_game" / "when_empty", reason=…, countdown=…)`, `cancel(server)`, `planned`, `busy`, `can_control`. `countdown=0` means right away; a number is the seconds of notice; none means the host's `warn_seconds` |
| `ctx.rcon(server)` | The raw console: `await ctx.rcon(server).command("load", "Guardian", "Slayer")`, for what the rest doesn't cover |
| `ctx.db` | The shared SQLite database. `ctx.db.migrate([...])` creates your tables (prefix them with your part's name); `execute` and `query` run SQL |
| `ctx.config` | Your section with `defaults` filled in (also `self.config`) |
| `ctx.emit(name, server, **data)` | An event for the other parts, named `<your part>.<name>` |
| `ctx.log` | A logger tagged with your part's name (also `self.log`) |
| `ctx.home` | The supervisor's folder (settings.toml, commands.toml…), for files of your own |
| `ctx.dry_run` | Whether the host set `dry_run` for your part (or for everything) |

## Events
Subscribe with `@on("name")`, `@on("group.*")` or `@on("*")`. Each event has
`name`, `server` (its name, or `None`), `time` (Unix seconds) and `data`, which
you read as `event["field"]` or `event.get("field")`.

The `rcon.*` events are the server's own, with the fields it sends (from the
server's RCON.md, v0.9.18):

| Event | Fields | When |
| --- | --- | --- |
| `rcon.chat` | `channel` (`all`, `team`, `server`; proximity seen in logs), `team`, `name`, `id`, `text` | A line everyone in the channel read. Muted players' lines and private lines never arrive. |
| `rcon.join` | `number`, `name`, `id`, `ip`, `admin` | A player was admitted |
| `rcon.leave` | `number`, `name`, `id` | A player left or was removed |
| `rcon.refused` | `name`, `id`, `ip`, `reason` | A joining player was turned away |
| `rcon.kick` | `name`, `id`, `by`, `reason`, `seconds` | `by` is an admin, `RCON`, `Alice (RCON)`, `vote`, or `Server` (load timeout) |
| `rcon.ban` / `rcon.unban` | `name`, `id`, `ip`, `device`, `by`, `reason`, `expires` / `target`, `by` | |
| `rcon.mute` / `rcon.unmute` | `name`, `id`, `by`, `reason`, `expires` / `time` when it ran out | |
| `rcon.pardon`, `rcon.admin_added`, `rcon.admin_removed` | `target` or `id`, `name`, `by` | |
| `rcon.vote` | `state` (`called`, `passed`, `failed`, `cancelled`), `subject`, `caller` | Players' votes |
| `rcon.kill` | `killer`, `victim`, `report` | A death. **`killer` and `victim` are engine IDs, not `#numbers`**; how they map to players is still to be worked out on a live server |
| `rcon.phase` | `phase` (`no_game`, `playing`, `round_over`, `between_rounds`, `finished`), `map`, `mode` | The server can send `finished` for the next game just before it starts; check it matches the game that was `playing` |
| `rcon.control` | `action`, `ok`, `text` | Reloads (`action` = `reload`), queued game actions |
| `rcon.cheat` | `name`, `id`, `category`, `detail`, `action` | Rejected game data |
| `rcon.connected` / `rcon.disconnected` / `rcon.lagged` / `rcon.full` | | The supervisor's connection |

The supervisor adds its own:

| Event | Fields |
| --- | --- |
| `server.starting`, `server.ready`, `server.adopted` | the manager started it; its RCON answered after a start; it was found running and taken over (`pid`) |
| `server.started`, `server.crashed` | from the built-in supervisor's console (backend `process`): `pid`; `reason`, `kind` (`hang`, `menus`, `error`, `panic`, `engine_exit`, `other`) |
| `server.stopping`, `server.stopped`, `server.all_stopped` | `expected` and `exit_code` on `stopped` |
| `server.countdown`, `server.countdown_cancelled` | `reason`, `coming_back`, `seconds`, `by` |
| `server.restart_planned`, `server.restarting`, `server.restarted`, `server.restart_failed` | `when`, `reason`, `by` |
| `server.faulted` | `reason`: the supervisor gave up starting it |
| `server.hung`, `server.recovered` | `status_age` |
| `log.falling_behind` | `fps`, `behind`, `window`, `average`, `longest_ms`, `players` |
| `log.upload` | `rate`, `unit`, `players` |
| `log.roster_lost` | `lost`, `of`, `present`, `details` (the engine's JSON snapshot, as text) |
| `log.rcon_signin`, `log.rcon_refused`, `log.reload`, `log.online` | from `server.log` |
| `moderation.action` | `kind`, `target_id`, `target_name`, `by`, `source` (the part), `reason`, `duration`, `dry_run`, `ok`, `text` |
| `supervisor.started`, `supervisor.stopping`, `supervisor.part_disabled` | `parts` on `started`; `part`, `reason` on `part_disabled` |
| `automod.strike`, `automod.pardon` | `player_id`, `player_name`, `rule`, `strike`, `action`, `source` (`chat`, `name`, `manual`), `evidence`, `by`, `dry_run` |
| `watchdog.health` | `level` (`healthy`, `degraded`, `critical`), `previous`, `reasons`; the dashboard shows it |

Switch on `[eventlog]` to see every event as JSON while you work.

## Ground rules
- **Don't block.** Handlers are `async`; use `await asyncio.to_thread(...)` for slow
  work. Each handler has 10 seconds (`handler_timeout` in the part's section changes it).
- **Each part has its own queue.** Events reach it in order, one at a time, and a
  slow part never holds up the others or the RCON connection.
- **Failures are contained.** An exception is logged; five within ten minutes switch
  that part off (and only that one), with a `supervisor.part_disabled` event.
- **Everything done to a player goes through `ctx.moderation`,** so it's recorded
  with who and why, respects dry run, and shows players who acted.
- **Never resend a moderation command** after an error: it may have run already.
- **Never retry a refused RCON password:** five in ten minutes lock out every tool on the machine.
- **A server never depends on the supervisor's process:** the process backend puts
  each `dedicated` in its own process group (Windows) or session (Linux), with its
  output in a file, so closing the window never takes a server down.
- **Test against the fake server:** `engine/tests/fake_rcon.py` is a server's RCON,
  `engine/tests/fake_dedicated.py` a stand-in `dedicated run` that the process
  backend really starts, adopts, stops (Ctrl+Break / SIGINT) and sees crash.
  `test_manager.py` drives the state machine with a clock moved by hand;
  `test_menu.py` drives the window with a scripted keyboard; `test_surfaces.py`
  provokes every message the main flows don't reach (refusals, edge cases, the
  log lines for what goes wrong at run time) and checks the words the host reads.

## How it fits together
```text
 SERVERS/<folder>/dedicated.toml ──► discovery (10 s) ──► a manager, RCON client and file watcher per server
 servers' RCON ──► one RCON client per server ─┐
 data/<server>/server.log, status.json ──► file watcher ─┼─► event bus ─► live server state
 `dedicated` console (process backend) ──► backend ─┘          ├─► in-game commands ─┐
                                                               └─► each part's queue ◄┘
 fleet: one state machine per server, ticked every second ◄── the window's keys · records/requests/
 keepalive (hung?) · moderation (one ledger) · commands.toml (reloaded when edited) · records/ · logs/
```

A server is only called hung when `status.json` (rewritten every 5 seconds) and
RCON have **both** gone quiet.

## Trying it on Linux
The process backend runs the server through `wine` on Linux. Without the game,
the whole suite still runs in a container, fake `dedicated` included:

```sh
docker run --rm -v "$PWD:/repo:ro" python:3.12-slim sh -c \
  'cp -r /repo /work && cd /work && pip install -q -e "engine[test]" && python -m pytest -q engine'
```

## Releasing
Hosts get the supervisor, and its updates, from GitHub releases. A release is a
tag plus a zip made from it with `git archive`, which leaves out what
`.gitattributes` marks `export-ignore` (the tests and the repository's own files):

```sh
# 1. Set __version__ in engine/reclaimer_supervisor/__init__.py, add the
#    changes to docs/changelog.md, commit and push.
# 2. Tag it, make the zip, publish:
git tag v0.2.0 && git push origin v0.2.0
git archive --format=zip --prefix=reclaimer-supervisor/ -o reclaimer-supervisor-0.2.0.zip v0.2.0
gh release create v0.2.0 reclaimer-supervisor-0.2.0.zip --title "Reclaimer Supervisor 0.2.0" --notes-file notes.md
```

Running supervisors find it within `check_hours` and offer it to their host. The
notes' first lines are what their window shows, so start with what changed for
hosts. An update replaces `engine/` (except `engine/.venv`), the start scripts,
`docs/`, `README.md` and `LICENSE`; never the host's own files.
