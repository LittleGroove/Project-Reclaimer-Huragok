# How Huragok is built

Huragok is one program. Its core runs the servers and holds the pieces
everything shares (one RCON connection per server, the live state of each
server, the event bus, the records). On top of it run its **parts**:

| Part | Folder | Settings | Speaks as | |
| --- | --- | --- | --- | --- |
| `commands` | [parts/commands](../engine/huragok/parts/commands) | `commands.toml` | Vergil (`VRGL`) | The in-game commands. Always on. |
| `automod` | [parts/automod](../engine/huragok/parts/automod) | `[automod]`, `automod.txt` | Vergil (`VRGL`) | The word list |
| `watchdog` | [parts/watchdog.py](../engine/huragok/parts/watchdog.py) | `[watchdog]` | the Superintendent (`SUPT`) | Restarts a server that died, lets nobody in or got stuck; notices a slow one. Each case is watch, act or actnow |
| `eventlog` | [parts/eventlog.py](../engine/huragok/parts/eventlog.py) | `[eventlog]` | the Superintendent (`SUPT`) | Every event as JSON |

Parts are a way to keep the code apart, not something hosts install: everything
ships together, and new work comes as a pull request. Every command shares one
set of names, and [tests/test_parts.py](../engine/tests/test_parts.py) fails if
two parts claim the same one.

## The core, module by module
| Module | |
| --- | --- |
| `config.py` | `settings.toml`, and the servers: every `SERVERS/<folder>/dedicated.toml` (plus `server_folder`), checked against each other (unique names, slugs and ports; RCON on). Types and minimums (`MINIMUMS`) are checked as it loads; a key that isn't a setting (`KNOWN_KEYS`), a value an older version wrote (`OLD_VALUES`) and a retired setting (`RETIRED`) become warnings. `discover_servers()` re-reads the folders for ones added while running. |
| `manager.py` | **One `ServerManager` per server**: the state machine below, ticked once a second by the `Fleet`. The `Fleet` also writes `records/live.json`, answers requests from `records/requests/`, and is what the window's keys call. `Lifecycle` is the thin view parts get (`restart`, `cancel`, `planned`, `busy`). Timers run on `time.monotonic()`; wall time only for the hourly budget, history, uptime and the daily slot. |
| `lifecycle.py` | The **backends**: `process` (one `dedicated` per server, Windows or Linux), `docker`, `command`, `attach`. A backend only starts, stops and watches processes; it decides nothing. `PortWatch` reads the system's listening sockets (psutil) at most once a second for every stopped server, so no check blocks the loop. `Keepalive` restarts a hung server. |
| `runner.py` | Each part's queue, its handlers' time limit (`max(handler_timeout, [rcon] command_timeout + 5)`), and switching a failing part off and, after `COOL_DOWN`, on again. |
| `menu.py`, `tui.py` | The **window**: the dashboard, its screens, the keyboard (Windows console key events, `ReadConsoleInputW`, with `msvcrt` when the console can't be opened; Linux `termios`), colours and in-place redraw, the recent-events log handler. `ConsoleWriter` writes the console from a thread of its own, so a paused console (Edit > Mark) never holds up the loop; `clean` takes control characters out of what players and servers send, `cells` measures wide characters. `TaggedFormatter` puts the speaking part's tag on the dashboard's and the plain log's lines; `RemoteController` is the second window, fed by `live.json`. |
| `lore.py` | **Who's who**: the three named parts (below) with their tags and colours, which part speaks for each logger (`SPEAKERS`), which part owns each key (`KEYS`), the Superintendent's notices (`NOTICES`, `SIGN_OFF`), faces and `mood()`, the banner, the motto and the VISR colour key. |
| `mailbox.py` | Requests from outside (`start.cmd stop`, a second window) and their replies, as files. |
| `rcon.py`, `state.py`, `files.py` | One RCON connection per server (a refused password is tried again only when trying can't add up to a lockout: see the top of `rcon.py`); the live picture of each server; `server.log`, `status.json` and the console output as events. `files.py` matches every pattern at the start of the server's own message, and the lines that carry someone's words (console echoes, refusals, kick and ban reasons: `ECHOES`) first, so typed text never passes for a server line. |
| `moderation.py`, `permissions.py`, `chat.py` | One ledger for every warn/mute/kick/ban; `commands.toml` (a file missing or empty for a moment keeps what's in force); `!` commands in chat, and `CommandCall.find_target`, who a command is for. |
| `storage.py`, `updates.py`, `desktop.py`, `watch.py`, `tomledit.py` | SQLite; new versions of Huragok from GitHub (renames only, the backup in `records/previous-<version>/`, `records/last-update.json`, `put_back`); opening folders, editors, log windows, start at boot; following a log; editing `settings.toml` and `dedicated.toml` in place (keys, tables, `[[server]]` blocks), keeping comments. Every `tomledit` write makes the change in memory, reads the result back, and replaces the file (temp + replace) only if exactly that change differs; otherwise `TomlEditError`, with the file untouched. |
| `dedicated.py`, `newserver.py`, `packages.py` | **Setting servers up**: running the server program's own `dedicated` commands (exe on Windows, wine on Linux; a server folder's own copy, else `[setup] program` or the newest copy here) and reading their listings, a timeout or Esc ending the whole process tree; making a server folder from a package with the root's shared `game/`, `content/` and `bans.json`; packages (`PACKAGES/`, the starters in `templates/packages`). |
| `menu_setup.py`, `setupcli.py` | The window's N, G, I, C and E screens; the `new`, `package`, `content`, `playlist`, `variant`, `workshop`, `steam` and `dedicated` commands. Both work without the fleet, so a second window has them too. |
| `catalogue.py`, `playlists.py`, `steamsetup.py`, `contentimport.py` | What a server can play as one catalogue (the listing, the mods' ModInfo.json and Forge layouts, the Workshop items) with numbered picking; playlist files built and edited from picks; SteamCMD found, installed, signed in and written into every server, one depot of MCC downloaded (`download_depot`), and whether a server's SteamCMD can fetch at all (`cannot_fetch`); MCC found only where Steam's own records say it is (`find_mcc`: the registry, `libraryfolders.vdf`, `appmanifest_976730.acf`; never a drive search) and its variants imported. |
| `repair.py` | **What a server arrives without**, put right as its folder is found and before each start (`GameFiles.prelaunch`, the Fleet's `prelaunch` hook): a missing ban list or content folder made, a game path or `[workshop] steamcmd` from another PC pointed at this one's, Halo 3's files (and the campaign's, ODST's) got in the background from MCC here or with SteamCMD (`DEPOTS`: which piece of MCC holds what) while the server waits as WAITING. Process backend only. |
| `migrate.py` | **The host's own files brought up to date** after an update: the `STEPS`, run at Huragok's start under its lock, each once (`records/files.json`), each file backed up first (`records/previous-files/<version>/`). See "Hosts' files across updates" below. |
| `livesettings.py`, `menu_manage.py` | **Managing a running server**: every setting a host can change (`SETTINGS`: key, scope, type, default, group, when it applies; add a row for a new one) and the window's A (console with events), D (settings in groups, what it plays, rename, own content), B (bans, admins, VPN) and U (updates, Workshop mods, clean up) screens. |
| `statuscard.py` | The **status card** (V): ten sections from a server's files, its dashboard row and what the supervising window adds. |
| `gameoptions.py`, `gameoptions_data.py`, `menu_games.py` | **Game options**: the catalogue (generated: see below), presets (saved custom games) and playlist-game overrides; the one option editor both use. |
| `serverops.py`, `upkeep.py` | Renaming a server and giving it its own content folder; clean-up, firewall rules, Halo 3's engine file. |
| `serverupdates.py`, `workshopupdates.py` | New releases of the server program (servers with `auto_update` update themselves; the others from GitHub after their game, with `follow`); new Workshop versions swapped in after the game, as a job the manager runs while the server is stopped (the `preparing` phase). |
| `relay.py` | The console relay: the server's WebSocket console protocol served by Huragok, one port per server, moderators by password and role, commands gated by `commands.toml`'s lists and forwarded with `by`, events fanned out. |
| `core.py`, `cli.py` | Wiring, and the relaunch after an update (it waits for servers part way through a restart, `RELAUNCH_WAIT`); the commands `start` (with the start loop below), `status`, `stop`, `close`, `restart`, `say`, `rcon`, `watch`, `check`, `update`, `init`, the exit codes, and `--help` grouped by named part (`HELP_GROUPS`). |

### A server's phases
```text
idle ──► launch ──► booting ──► running ──► countdown ──► stopping ──► idle | launch
  ▲       │  ▲        │                        │  (cancel)                   ▲
  │       ▼  │        ▼                        ▼                             │
  │     preparing  backoff ──► faulted      running                   restarting (a backend
  │                   ▲                                                with only `restart`)
  └── external (up, but not started from here) · offline (attach: not reachable)
```
Up means **RCON answers**, not the process existing. A crash backs off
(`backoff_seconds`); `max_failures` fast failures in a row, or more than
`restarts_per_hour` automatic restarts, mean `faulted`: a pause, after which it
tries once more (`faulted_retry_minutes`, 30 minutes then hourly; a failed try
comes straight back with the next cool-down, a run of `good_run_seconds` is back
to normal), and S starts it at once. A countdown says the `warn_seconds` rungs as absolute deadlines (if a
slow tick lets two fall due at once, the smaller is said), skips itself when
nobody is on, and ends early once everyone has left. `preparing` runs the jobs
queued for the next start (a program swap, a Workshop install) while the server is
stopped: a snapshot taken as it begins (a job added meanwhile waits for the next
start), each job with `JOB_LIMIT` (20 minutes), then `launch` again; a stop calls
them off. A restart planned for later (`after_game`, `when_empty`) belongs to the
run it was made in: `_run_ended` drops it when that run ends some other way. Nothing
in a manager waits: a stop in progress is a task the next tick looks at, and a stop
whose process outlives the kill leaves the server running and flagged (`! couldn't
stop it`), never reported stopped. What survives closing the window is in
`records/servers/<server>.json` and `records/running-servers.json`.

Before `launch` starts a stopped server, the Fleet's `prelaunch` hook (repair.py)
puts right what its folder lacks; while something is missing (Halo 3's files being
got, or no way to get them) the server stays `idle` with `waiting_for` set: the row
says WAITING and why, it looks again every `WAIT_RECHECK` (30 s) or as soon as
`Fleet.release()` says the files arrived, and nothing counts as a failed start. A
stopped server whose port answers but writes no `status.json` in its own folder isn't
taken for `external`: it waits the same way (`waiting_kind = "port"`), since that's
another copy of it, running from another folder.

### The start loop and the exit codes
`start` (what `start.cmd` and `start.sh` run) doesn't run Huragok itself: its
`_start_loop` runs `python -m huragok start --child` as a process of its own and
waits for it, holding the lock (`records/huragok.pid`, marked `launcher`) while it
starts it again, so a second window or a `start.cmd stop` never steps into the
gap. This process stays on the code it started with, whatever an update puts on
disk. Every deliberate end has its own exit code (`cli.py`):

| Code | Meaning | The start loop |
| --- | --- | --- |
| 0 | done: quit, stop, close | ends |
| 1 | it didn't work (also what Python exits with on an uncaught exception) | a crash: starts it again |
| 2 | a settings problem, or a name it doesn't know | with the window: ends (with `--hold`, the window stays open); with `--no-menu`: tries again every 5 minutes, or as soon as settings.toml changes |
| 3 | first start: the files were written | ends |
| 4 | `start`: Huragok already runs from this folder | ends |
| 5 | `status`: nothing runs from this folder | (not a `start` code) |
| 10 | a new version is installed: start it | starts the new version |

Any other end is a crash: it waits `CRASH_WAITS` (5, 15, 30, 60, then 300 s,
repeating) and starts Huragok again, forever, the count starting over once a run
lasted `GOOD_RUN_SECONDS` (10 minutes); a request made meanwhile (`start.cmd stop`,
`close`) ends the wait early so the new Huragok answers it. When the new version a
10 asked for ends within `FAILED_START_SECONDS` (2 minutes) with a crash or a
settings problem (it may be stricter about the file than the old one), the loop puts
back the backup `records/last-update.json` names (`updates.put_back`, loaded before
the update ran), notes it in `records/failed-update.json`, and starts the old
version. Every step is printed and logged (`launcher:` lines in `huragok.log`, or
`logs/launcher.log` when settings.toml can't be read). It never touches a server.

## Who's who: the three named parts
The program is named for Halo 3: ODST's Engineer and the two minds it carried out
of New Mombasa, and [lore.py](../engine/huragok/lore.py) splits it into three named
parts. They are names for what the host sees, not code boundaries (and not the
`Part`s above): a module or a `Part` belongs to one by what it does.

| Named part | Tag | Colour | Owns |
| --- | --- | --- | --- |
| Huragok | `HRGK` | violet | the program, and what it builds and mends: new servers, packages, content, the Workshop and SteamCMD, settings files, checks, updates, the window. Every logger not listed for the other two |
| the Superintendent | `SUPT` | amber | keeping the servers running: `manager`, `lifecycle`, `state`, `runner`, `mailbox`, `rcon`, `winproc`, `events`, `files`, `core`, `part.watchdog`, `part.eventlog` |
| Vergil | `VRGL` | sky blue | the players: `part.automod`, `part.commands`, `chat`, `moderation`, `permissions`, `relay`, `storage` |

- **Tags.** `lore.speaker(logger)` maps a logger name (without the `huragok.`
  prefix) to its named part through `SPEAKERS`, by whole dotted names, first match
  wins, Huragok otherwise. `tui.TaggedFormatter` uses it for the dashboard and the
  plain `--no-menu` log (`21:03:40 SUPT INFO    [Slayer] …`). `logs/huragok.log`
  keeps the module name instead (`2026-10-18 21:03:40,123 INFO    manager: [Slayer] …`);
  a second window turns it back into the tag when it shows the file's lines.
- **Keys and screens.** `KEYS` maps each dashboard key to its named part;
  `menu.key_rows` groups the keys the same way, and `Menu.title` starts every
  screen's title with its part, in its colour (`SUPERINTENDENT › RESTART`). A new
  key goes in `KEYS` too: `test_lore.py` fails otherwise.
- **What players read.** `NOTICES` and `SIGN_OFF` are the Superintendent's public
  notices from Bungie's 2008 "KEEP IT CLEAN" campaign. `manager.warning_text`
  starts countdowns with `PLEASE REMAIN CALM!`; the restart itself and a restart
  after the game start with `PARDON OUR DUST!`; a cancelled countdown ends with
  `Keep it clean!`; automod's `MESSAGES` start with `KEEP IT CLEAN!`.
- **The face.** `lore.mood(rows)` picks the Superintendent's expression, a notice
  and a summary from the dashboard's rows, the worst thing first (FAULTED or a
  config error, then a crash backing off, a countdown, a start, players on, nobody
  on, everything stopped). The dashboard's second line and `status` show it.
- **What didn't change.** Settings keys and section names (`[watchdog]`,
  `[automod]`, `[relay]`…) stayed as they were, so every host's `settings.toml`
  still reads. Internal names stayed too: `class Supervisor` in `core.py`,
  `running_supervisor()`, the `supervising` flag and the `supervisor.*` events.
  Renaming them would change nothing a host sees, and would break anything that
  listens for those events.

## Adding a command
Most new things are commands. Put one in the file of
[parts/commands](../engine/huragok/parts/commands) it fits
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
  start of one among the players on now, and tells the caller when it matches
  nobody or several. Names aren't unique (one real night had 30 players called
  "Player"), so prefer `#numbers`. Use it only where a wrong pick harms nobody
  (`!whois`, `!team`).
- `await call.find_target(ref, history=self.ctx.players, offline=…)` is for a
  command that acts on a player: `#3` or `3`, a full player ID, an exact name on the
  server, a name `history` has seen them use, or 8+ characters of an ID; never the
  start of a name alone (it answers "did you mean …?"), since the one meant may have
  left and someone else matches. With `offline`, someone who isn't on comes back as
  a `Player` with number 0.
- `self._target(call, ref, offline=…)` does that and refuses players of the
  caller's role or above (by ID too); use it for anything done *to* a player.
- `call.reply` never raises: a line the console can't deliver (a slow or
  restarting server) is logged and the rest dropped, so it never counts against
  the part.
- `self._rcon(call, "command", *args)` passes a line to the server's console as
  the caller and shows them the answer; `self._line(call, "name")` passes the
  console command of the same name with the caller's words.
- Pick a name that says what the command does. `!yes` and `!no` are the server's
  votes; lines starting with `/` are the game's own.

## Adding a part
For something that isn't a command, such as watching events, running on a timer,
or keeping state of its own, write a part: a module (or a package, for a bigger
one) in [parts](../engine/huragok/parts) with one `Part` subclass,
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

    async def teardown(self) -> None:      # once, when Huragok stops
        ...
```

Then add its section to
[templates/settings.toml](../engine/huragok/templates/settings.toml) under the
banner of the named part it belongs to, tests under `engine/tests/`, and a line in
the README. Its section's settings are `enabled`, `dry_run`, `handler_timeout` and
the keys of `defaults`: any other key there is named at the start and by `check`
as a setting that does nothing, so give every setting a default. Its log lines
carry the `HRGK` tag unless `part.<name>` is in `lore.SPEAKERS` with the
Superintendent or Vergil. `check` shows each part, whether it's on and any problem
with its settings: it runs the part's `check_config` on a stand-in context, and a
part that reads its own files can check them there too by defining
`preflight(config, home)`, as automod does with its word list.

## What `self.ctx` gives a part
| | |
| --- | --- |
| `ctx.servers` | Each server's live picture by name: `players` (by `#number`: `name`, `id`, `ip`, `admin`, `muted`, `team`), `phase`, `map`, `mode`, `max_players`, `admins`, `connected`, `status_age`, `rcon_latency_ms`. Servers added while running appear here. |
| `ctx.server(name)` | One of those |
| `ctx.say(server, text)` | A `[Server]` line everyone reads |
| `ctx.tell(server, player, text)` | A `[Server]` line one player reads (a `Player` or a player ID) |
| `ctx.players.get(id)`, `.find(text)` | Everyone this Huragok has seen (one database per root): names used, IPs, first and last seen, visits |
| `ctx.role_of(server, player)` | `player` (everyone), `member`, `moderator`, `admin` (the server's own admins) or `owner` |
| `ctx.permissions` | `commands.toml` as it's in force now (it reloads when edited) |
| `ctx.moderation` | `warn`, `mute`, `unmute`, `kick`, `ban`, `unban`, `pardon`: recorded with a reason, shown to players as by your part (or the `by=` you pass), and dry-run aware. Use these for anything done to a player |
| `ctx.lifecycle` | `restart(server, when="now" / "after_game" / "when_empty", reason=…, countdown=…)`, `cancel(server)`, `planned`, `busy`, `can_control`. `countdown=0` means right away; a number is the seconds of notice; none means the host's `warn_seconds` |
| `ctx.rcon(server)` | The raw console: `await ctx.rcon(server).command("load", "Guardian", "Slayer")`, for what the rest doesn't cover |
| `ctx.db` | The shared SQLite database. `ctx.db.migrate([...])` creates your tables (prefix them with your part's name); `execute` and `query` run SQL |
| `ctx.config` | Your section with `defaults` filled in (also `self.config`) |
| `ctx.emit(name, server, **data)` | An event for the other parts, named `<your part>.<name>` |
| `ctx.log` | A logger named `part.<your part>` (also `self.log`); `lore.SPEAKERS` decides the tag its lines carry in the window |
| `ctx.home` | Huragok's folder, the root (settings.toml, commands.toml…), for files of your own |
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
| `rcon.connected` / `rcon.disconnected` / `rcon.lagged` / `rcon.full` | | Huragok's connection |
| `rcon.auth_failed` | `error`, `lockout` | The server refused Huragok's console password; `lockout` when this machine is locked out (by any tool's wrong passwords) rather than the password being wrong |

Huragok adds its own:

| Event | Fields |
| --- | --- |
| `server.starting`, `server.ready`, `server.adopted` | the manager started it; its RCON answered after a start; it was found running and taken over (`pid`) |
| `server.started`, `server.crashed` | from the built-in supervisor's console (backend `process`): `pid`; `reason`, `kind` (`hang`, `menus`, `error`, `panic`, `engine_exit`, `other`) |
| `server.stopping`, `server.stopped`, `server.all_stopped` | `expected` and `exit_code` on `stopped` |
| `server.countdown`, `server.countdown_cancelled` | `reason`, `coming_back`, `seconds`, `by` |
| `server.restart_planned`, `server.restarting`, `server.restarted` | `when`, `reason`, `by` |
| `server.start_failed` | `why`, `lines` (the console since the start): a start that never came up |
| `server.faulted` | `reason`, `retry_at` (Unix seconds; None with `faulted_retry_minutes = []`): the Superintendent stopped trying for now |
| `server.fault_retry` | `attempt`, `reason`: one more try after a FAULTED server's cool-down |
| `server.hung`, `server.recovered` | `status_age` on `hung` |
| `server.workshop`, `server.map_skipped` | from the console: `item`, `text`; `path`, `reason` |
| `server.workshop_update`, `server.workshop_updated` | a new Workshop version seen (`item`, `newest`, `installed`); swapped in (`items`) |
| `server.program_updated` | `version`, and `before` (a server that updated itself) or `restarted` (Huragok's install) |
| `server.repaired` | `text`: what Huragok put right in a server's folder (a ban list made, a game path pointed here: repair.py) |
| `log.falling_behind` | `fps`, `behind`, `window`, `average`, `longest_ms`, `players` |
| `log.upload` | `rate`, `unit`, `players` |
| `log.roster_lost` | `lost`, `of`, `present`, `details` (the engine's JSON snapshot, as text) |
| `log.connected`, `log.joined` | `name` (and `id` on `connected`): a player past the admission check, then in once the map has loaded (the roster line, or ODST's "joined the game in progress") |
| `log.rcon_command` | `address`, `by`, `command`: a console command the server echoed, which is never matched as one of its own lines |
| `log.rcon_wrong_password` | `address`, and at the fifth `times`, `refused`, `minutes`: the server locking an address out |
| `log.vote_refused` | `caller`, `reason` |
| `log.rcon_signin`, `log.rcon_refused`, `log.reload`, `log.online` | from `server.log` |
| `moderation.action` | `kind`, `target_id`, `target_name`, `by`, `source` (the part), `reason`, `duration`, `dry_run`, `ok`, `text` |
| `supervisor.started`, `supervisor.stopping` | `version` and `parts` on `started` |
| `supervisor.part_disabled`, `supervisor.part_enabled` | `part`; `reason`, `for_good` and `seconds` (until it's on again) on `part_disabled` |
| `supervisor.commands_reloaded` | `commands.toml` was read again |
| `supervisor.game_files` | `kind` (`halo3`, `campaign`, `odst`), `text`: game files arrived in this folder's `game/` |
| `automod.strike`, `automod.pardon` | `player_id`, `player_name`, `rule`, `strike`, `action` (`kick`, `kick 1h`, `mute 1d`, `mute perm`, `ban`), `source` (`chat`, `manual`), `evidence`, `by`, `dry_run` |
| `automod.name` | `player_id`, `player_name`, `caught_by` (`names.txt line 4`, `automod.txt slurs`), `by`, `dry_run`, `ok`: a name kicked as it joined |
| `automod.name_banned`, `automod.name_allowed` | `text` (the name), `by`; `player_id` and `note` on `name_banned` |
| `watchdog.health` | `level` (`healthy`, `degraded`, `critical`), `previous`, `reasons`; the dashboard shows it |

Switch on `[eventlog]` to see every event as JSON while you work.

## Ground rules
- **Hosts' files move forward with Huragok, never backward or by hand.** Since 0.4.0
  went out, every release reads `settings.toml`, `commands.toml`, `automod.txt`,
  `names.txt`, `PACKAGES/` and `records/` as any version from 0.4.0 on wrote them, and
  a change to their format comes with a step in `migrate.py` that brings an existing
  file up to date. No release may need a clean install. See "Hosts' files across
  updates".
- **Don't block.** Handlers are `async`; use `await asyncio.to_thread(...)` for slow
  work (a file walk, a drive probe, a subprocess, a blocking socket). Each handler
  has `handler_timeout` (10 seconds; the part's section changes it), and always at
  least `[rcon] command_timeout` + 5, so a slow server times out in the RCON call
  first. The window's screens follow the same rule: nothing they do may hold up
  the loop that looks after every server.
- **Each part has its own queue.** Events reach it in order, one at a time, and a
  slow part never holds up the others or the RCON connection.
- **Failures are contained.** An exception is logged; five within ten minutes switch
  that part off (and only that one), with a `supervisor.part_disabled` event, and
  on again after ten minutes (`supervisor.part_enabled`). A part whose setup failed
  stays off. An `RconError` from a handler (a slow or restarting server) is logged,
  not counted as the part failing.
- **Everything done to a player goes through `ctx.moderation`,** so it's recorded
  with who and why, respects dry run, and shows players who acted.
- **Never resend a moderation command** after an error: it may have run already.
- **Never retry a refused RCON password on a whim:** five wrong ones in ten minutes
  lock out every tool on the machine. `rcon.py` tries again only on a new password,
  once after the server restarts (at most twice in ten minutes), or after ten
  minutes.
- **A server never depends on Huragok's process:** the process backend puts
  each `dedicated` in its own process group (Windows; out of Huragok's job object
  too, `CREATE_BREAKAWAY_FROM_JOB`, where Windows allows it, so ending a Task
  Scheduler task doesn't take the servers) or session (Linux), with its output in a
  file, so closing the window never takes a server down.
- **Text from players and servers is cleaned before it's shown:** `tui.clean`
  turns control characters into `?`, so a name can't drive the terminal.
- **Test against the fake server:** `engine/tests/fake_rcon.py` is a server's RCON,
  `engine/tests/fake_dedicated.py` a stand-in `dedicated run` that the process
  backend really starts, adopts, stops (Ctrl+Break / SIGINT) and sees crash.
  `test_manager.py` drives the state machine with a clock moved by hand;
  `test_watchdog.py` feeds the watchdog the inputs it really gets (log events, the
  console's events, the server's state) and follows a log line to a restart;
  `test_menu.py` drives the window with a scripted keyboard; `test_surfaces.py`
  provokes every message the main flows don't reach (refusals, edge cases, the
  log lines for what goes wrong at run time) and checks the words the host reads;
  `test_tomledit.py` runs every kind of edit over unusual but valid files and
  checks each either reads back as exactly that change or leaves the file as it
  was; `test_relay.py` checks the relay's gate (roles, `[off]`, control characters,
  what moderators below admin don't see); `test_lore.py` checks who's who: the tag
  each logger gets, that every key belongs to a named part and shows on the
  dashboard, and the Superintendent's face for each state; `test_guards.py` checks
  the guards every test runs inside (below).

## Hosts' files across updates
An update replaces the program and nothing else, so a host's `settings.toml` is the one
their first start wrote, however many versions ago. Huragok keeps it in step itself:
`migrate.py` runs as Huragok starts (`cli._run_supervisor`, under the lock, before any
server is looked after), and `check` lists what the next start would change. When a
release changes what a host's file should say:

1. **The reader first.** The code reads every older form, as if no step had run: a new
   setting has a default, a renamed one still reads its old name (`OLD_VALUES`,
   `RETIRED` name what an old file says). A step that fails, or a file that can't be
   read, must never stop Huragok.
2. **A step** in `migrate.STEPS`, keyed `<version>/<what>`: it looks before it changes
   anything (a second run changes nothing), changes TOML only through `tomledit`
   (`add_table_key`, `comment_out_table_key`, `replace_comments`, `set_table_key`), and
   calls `ctx.backup(path)` before its first change. Leave the host's own values and
   comments alone: add what's missing, turn what does nothing into a comment that says
   why, and replace only comment blocks that are exactly what an earlier version wrote.
3. **Files the version before can still read.** The start loop puts back a new version
   that fails to start, and the version put back runs on the files as the steps left
   them: add keys (an older version names an unknown key, and runs), don't remove or
   rename tables.
4. **A test** in `test_migrate.py`, from the files 0.4.0 wrote (`tests/data/host-0.4.0`).
   `test_every_template_key_reaches_an_older_hosts_file` fails when the template gains a
   setting that no step adds to an older file (or that `NOT_ADDED` doesn't explain).

`always` steps run at every start and only add what's missing: in-game commands newer
than `commands.toml` (under their default role), starter packages newer than `PACKAGES/`
(`records/files.json` remembers which were offered, so one the host removed stays
removed).

## Running the tests
From the repository's top folder:

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine -q > tests.txt 2>&1
```

- Over 750 tests, about three minutes on Windows (774 passed and 1 skipped at the
  last full run: a time-zone test that needs `time.tzset`, which Windows lacks).
  They need pytest 9 (the `test` extra says so).
- **They start real processes.** The process backend starts, adopts, stops and
  crashes `fake_dedicated.py`; `test_cli.py` runs `start.cmd`/`start.sh` themselves
  (making a venv, on Windows from a folder with accents in its name); a few start two
  Huragoks at once to race for the lock. They need a writable temp folder and free
  local ports, and they take longer on a busy machine. Those tests are marked
  `slow`: `-m "not slow"` leaves them out for a quick run (the default runs
  everything).
- **Write the output to a file**, as above: a console that's slow to draw, or one
  where text is selected, holds the run up.
- **A hang ends the run with every thread's stack** (`faulthandler_timeout = 180`
  and `faulthandler_exit_on_timeout` in `pyproject.toml`; CI passes 120 s, and each
  CI job has 20 minutes).
- **Errors nobody sees fail the test**: an exception in a destructor, a callback or a
  thread, a coroutine never awaited (`filterwarnings` in `pyproject.toml`), and an
  error asyncio logs, such as "Task exception was never retrieved" (a test that
  provokes one on purpose is marked `@pytest.mark.asyncio_errors`). Markers must be
  registered (`--strict-markers`).
- **The tests stay off the network and away from real servers** (`conftest.py`,
  checked by `test_guards.py`). A PC that runs Project Reclaimer servers has them on
  49175-49185, and Windows hands out a program's own ports from 49152 up, so
  `free_port()`/`free_ports()` never return a port from 49170 to 49199 or one in
  use, new servers made in a test are given ports past them, and a guard fails any
  test that connects to one of those ports or to anything past this machine
  (GitHub, Steam): a console's five wrong passwords lock every tool on the machine
  out of it. Steam, MCC and SteamCMD are found only where a test puts them, never
  this PC's own. A fake server ends itself once the test session's process is gone
  (`HURAGOK_TEST_PARENT`), and the servers a test's `running-servers.json` still
  names are ended after it (only those this session started).
- One file or one test: `pytest engine/tests/test_watchdog.py`, `pytest engine -k no_joins`.

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

## The game option catalogue
`huragok/gameoptions_data.py` is generated, not written by hand: every key the server
program accepts, its label, range and the meaning of every number, from the program's
own answers kept in `tests/data/game-options-probe.txt`. After a Project Reclaimer
release that adds or renames game options, probe again from `engine/`:
`python tools/reprobe_gameoptions.py <project-reclaimer.exe> <a server folder>
tests/data/game-options-probe.txt new.txt` (`mode-check` with one option at 99999 per
game type, and `dedicated playlist show` on scratch playlists naming every number;
about 20 seconds; the server folder needs Guardian). Compare `new.txt` with the probe
file, move it over it, and run `python tools/gen_gameoptions.py`. `tests/test_games.py`
checks the catalogue against the probe file. A renamed label matters: a playlist that
names a label the server's program doesn't take stops `dedicated check`, so the
server won't start. Add it to `gameoptions.LABEL_CHANGES` (the option, the old label,
the new one, and the first version that calls it the new one, or `None` when every
version does). 0.9.19 renamed the character set's "Vanilla characters" to "Halo 3"
(0.9.18 refuses "Halo 3", 0.9.19 on refuse the old one); "On, with firebombs" is
what every version calls it, and the "On with firebombs" some files have is
refused by all of them. As it starts a server, the Superintendent writes its
playlists' labels the way the program about to run calls them (its file's own
`--version`, else the version it last reported; when neither is known, only what
no version takes is rewritten), and `check` names them. The option editor writes
a version-dependent choice as its number, which every version takes.

## Linux
Huragok is run on Windows 11. Linux is experimental and on hold: the code keeps
working there (CI runs the suite on Ubuntu), but it isn't tested end to end with a
real server. The process backend runs the server through `wine` there. Without the
game, the whole suite runs in a container, fake `dedicated` included. From the
repository's top folder, in sh or bash:

```sh
docker run --rm -v "$PWD:/repo:ro" python:3.12-slim sh -c 'cp -r /repo /work && cd /work && pip install -q -e "engine[test]" && python -m pytest -q engine'
```

and in PowerShell, where `"$PWD:…"` is read as a drive-qualified variable and fails,
and a `\` at the end of a line doesn't continue it:

```powershell
docker run --rm -v "${PWD}:/repo:ro" python:3.12-slim sh -c 'cp -r /repo /work && cd /work && pip install -q -e engine[test] && python -m pytest -q engine'
```

The project's working folder also has `linux-check\run.ps1` beside the repository
(not in it): the suite, then a deployed root with two fake servers run the way a
host runs it (`start.sh`, `status`, a restart, the window in a pseudo-terminal,
`stop`), in a container (`pwsh -File linux-check\run.ps1 [-Image python:3.11-slim]`).
It fails when the tests fail.

## CI
`.github/workflows/ci.yml`, on every push and pull request, each job with a time
limit (20 minutes for the suites, 10 for the rest) so a hang fails the run:

- **tests**: the suite on Windows and Ubuntu, Python 3.11, 3.12, 3.13 and 3.14
  (`-o faulthandler_timeout=120`);
- **the release zip works on its own**: `git archive HEAD` unzipped and the suite
  run from that, so a module that was never committed fails here even though every
  test passes in a working copy; then a plain (not editable) install of the
  unzipped engine runs `init` and must write every template (`settings.toml`,
  `commands.toml`, `automod.txt`, `names.txt` and the `PACKAGES/` starters), and
  `check` reads them;
- **start.sh sets itself up**: a first start (exit code 3), `init --force --backend
  attach`, `check`, `status`;
- **docker image**: builds `engine/`, runs `--version`, then `init` and `check` on a
  mounted `/huragok`.

## Releasing
Hosts get Huragok, and its updates, from GitHub releases. A release is a
tag plus a zip made from it with `git archive`, which leaves out what
`.gitattributes` marks `export-ignore` (the tests and the repository's own files).
The zip holds only what's committed: a new module that was never added to git is
missing from it, and every host's Huragok fails to start. So:

```sh
# 1. Set __version__ in engine/huragok/__init__.py; put the release's section at the
#    top of docs/changelog.md, its first lines saying what changed for hosts.
# 2. Commit everything: `git status` must show nothing untracked under engine/.
#    Push, and wait for CI to pass, the release-zip job included.
# 3. The notes: the changelog's section for this version, in notes.md.
# 4. Tag it, make the zip, publish:
git tag v0.4.0 && git push origin v0.4.0
git archive --format=zip --prefix=huragok/ -o huragok-0.4.0.zip v0.4.0
gh release create v0.4.0 huragok-0.4.0.zip --title "Huragok 0.4.0" --notes-file notes.md
```

Running copies of Huragok find it within `check_hours` and offer it to their host;
the first six lines of the notes (headings left out) are what their window shows,
so start with what changed for hosts. An update replaces `engine/` (except
`engine/.venv`), the start scripts, `docs/`, `README.md` and `LICENSE`; the host's own
files are brought up to date by the new version's `migrate.py` as it starts. Before
tagging, install the zip over a copy of the previous release's install (its settings,
servers and records) and start it: it must come up with no warnings about its files,
and `check` must list nothing still to bring up to date. A copy from before the rename
(Reclaimer Supervisor 0.2.1) can't update itself to Huragok: its hosts reinstall (the
README says how).
