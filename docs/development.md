# How Huragok is built

Huragok is one program in three named parts, and the code is laid out the way
[lore.py](../engine/huragok/lore.py) names them:

| Named part | Tag | Colour | In the code |
| --- | --- | --- | --- |
| Huragok | `HRGK` | violet | [huragok.py](../engine/huragok/huragok.py): the program. It builds what everything shares, then the Superintendent, then Vergil, runs every background task, and owns what it builds and mends (`content/`, `updates/`), the window and the command line |
| the Superintendent | `SUPT` | amber | [superintendent.py](../engine/huragok/superintendent.py), built on Huragok: keeps every server up (`servers/`, with `program/` and `rcon.py`) |
| Vergil | `VRGL` | sky blue | [vergil.py](../engine/huragok/vergil.py), built on Huragok and the Superintendent: watches over the players (`players/`) |

`Huragok` holds the settings, the event bus, the database and the background tasks.
It builds `Superintendent(huragok)`, then `Vergil(huragok, superintendent)`, then
its own services (its updates, the server program's, the Workshop's, the game
files, the mod checks), and runs the parts of both. The Superintendent never
knows Vergil; Vergil uses the Superintendent's servers.

On top of them run the **parts**, each belonging to one named part (its `persona`):

| Part | Where | Settings | Speaks as | |
| --- | --- | --- | --- | --- |
| `commands` | [players/commands](../engine/huragok/players/commands) | `commands.toml` | Vergil (`VRGL`) | The in-game commands. Always on. |
| `automod` | [players/automod](../engine/huragok/players/automod) | `[automod]`, `automod.txt` | Vergil (`VRGL`) | The word list |
| `messages` | [players/messages](../engine/huragok/players/messages) | `intervalmessages.txt` (Huragok's folder and each server's) | `[Server]` | Interval messages. Always on; no messages until the host adds some |
| `watchdog` | [servers/watchdog.py](../engine/huragok/servers/watchdog.py) | `[watchdog]` | the Superintendent (`SUPT`) | Restarts a server that died, lets nobody in or got stuck; notices a slow one. Each case is watch, act or actnow |
| `eventlog` | [servers/eventlog.py](../engine/huragok/servers/eventlog.py) | `[eventlog]` | the Superintendent (`SUPT`) | Every event as JSON |
| `tempdiscord` | [servers/tempdiscord.py](../engine/huragok/servers/tempdiscord.py), [servers/statsrelay.py](../engine/huragok/servers/statsrelay.py) | none (always on, nothing to set) | the Superintendent (`SUPT`) | 0.5.2 until 0.6.0: the record the Discord bot and players' stats are built from, and a copy of every stats report each server sends. 0.6.0 takes it out |

Parts are a way to keep the code apart, not something hosts install: everything
ships together, and new work comes as a pull request. [parts.py](../engine/huragok/parts.py)
lists every one; every command shares one set of names, and
[tests/core/test_part_system.py](../engine/tests/core/test_part_system.py) fails if
two parts claim the same one.

## The layout
```text
engine/huragok/
  lore.py              who's who: the named parts, their tags and colours, who speaks for each logger, the notices
  huragok.py           HURAGOK: the program, and what starts everything
  superintendent.py    SUPERINTENDENT: keeps every server up. Built on Huragok
  vergil.py            VERGIL: watches over the players. Built on Huragok and the Superintendent
  parts.py             every part, in one list
  rcon.py              each server's console connection (the Superintendent's and Vergil's)
  steam.py             all of Steam: its library folders, MCC's location, SteamCMD, the Workshop's API
  winproc.py           kept at this path for Huragok 0.4.x (see "Hosts' files across updates")

  core/                what everything stands on: Huragok's own files and folders, each server's settings in
                       force, events, storage, the part system, text, files, TOML editing, processes, the lock,
                       the mailbox, logging
  games/               what a server can play: halo3.py, odst.py, their files, and what tells them apart
  program/             the server program (Project Reclaimer's `dedicated`): its commands, its settings, its releases
  servers/             the Superintendent's: one server's state machine, the fleet, the backends, live state,
                       logs, the keepalive, and the watchdog and event log parts
  players/             Vergil's: the ledger, roles, the router, who a command means, the relay, and the automod,
                       commands and messages parts
  content/             what Huragok builds and mends: new servers, packages, content, mods, game files, clean-up
  updates/             Huragok's own updates, the server program's, the Workshop's, and the host's files after one
  window/              the dashboard and its screens
  commandline/         start.cmd's commands, the start loop, the console without a window
```

A new game is a module in `games/` (as `odst.py` is) and a row in `games/plays.py`;
a new part is a module in the folder of the named part it belongs to, listed in
`parts.py`; a new screen goes in the `window/screens/` file of its group.

### Who may import whom
Each layer uses only the ones below it at run time (imports under `TYPE_CHECKING`
don't count), and
[tests/architecture/test_layers.py](../engine/tests/architecture/test_layers.py)
fails on one that crosses the line, naming it:

| Layer | May import |
| --- | --- |
| `lore` | nothing |
| `games/` | `lore` |
| `core/` | `games/`, `lore` |
| `steam`, `rcon` | `core/`, `games/` |
| `program/` | `core/`, `games/`, `steam` |
| `servers/` | the above and `rcon` |
| `players/` | the above and `servers/` |
| `updates/` | the above and `players/` |
| `content/` | the above and `updates/` |
| `superintendent.py` | everything up to `servers/` (never `players/` or `vergil`) |
| `vergil.py` | everything up to `players/`, and `superintendent` |
| `huragok.py`, `parts.py` | everything but `window/` and `commandline/` |
| `window/`, `commandline/` | anything |

`core/files.py` and `core/text.py` import nothing of Huragok's, so any layer may
use them. Three imports go upward on purpose, inside a function (`LET_THROUGH`):
`core/settings.py`, `core/hostsettings.py` and `core/commandsfile.py` read
`parts.py`, because settings.toml's part sections and commands.toml's commands
are checked against the parts there are. What a lower layer needs from a higher
one at run time (a server's lifecycle, the game files' check before a start) is
handed to it, never imported.

### Per-server and Huragok-wide
The Superintendent owns one `Server` per server folder (its settings, console,
live state, log watcher and manager). Anything else that keeps something per
server (Vergil's relay, Huragok's game files) calls `superintendent.listen()` and
hears `server_added(server, running=…)` and `server_removed(name)`. What a server
uses for a setting has one answer, `settings.of(server)`
([core/serversettings.py](../engine/huragok/core/serversettings.py)): its
`[[server]]` block, else the Huragok-wide section, else the default. A new
per-server setting is a row in its `OVERRIDES`.

### Three areas of settings
What a host sets comes in three areas, each with its own home in the code:

| Area | The files | The code |
| --- | --- | --- |
| **Huragok's own**: how Huragok runs, for every server and per server | `settings.toml` (its `[[server]]` blocks for one server) | `core/settings.py`, `core/serversettings.py`, and `core/hostsettings.py` for the window's guided editors |
| **How the server runs**: joining, voice, safety, mods, the network | each server's `dedicated.toml` | `program/settings.py` (every setting, grouped for D) |
| **What it plays and how**: a playlist, one map and game type, a custom game, campaign co-op or ODST, with each game's rules and options | the `[[server]]` block of `dedicated.toml` names it; the playlist, variant and map files it names | `program/plays.py` writes it; `games/plays.py` (the kinds), `games/halo3.py` (playlists), `games/halo3_options.py` (custom games and options), `games/odst.py`; `content/catalogue.py` (what's installed to play); `servers/state.py` (what's on now) |

The third has one home in the window, G (D → What it plays opens it), and one gate in the code:
`program/plays.check_plays` checks every change of what a server plays with that server's own program
(`dedicated check` on `.huragok-check.toml`, a draft beside its file holding its block alone on a port of its own),
counting only the problems the change brings, before anything is written. Every version of a playlist is kept by
`core/versions.py` (`records/playlists/<server>/<file>/<time>/`, with how it was made and where it came from);
each ballot's pick is kept in the `ballots` table (`servers/logs.py` reads the server's ballot lines, the
Superintendent records them, G → Playlists → P shows them). Choices in a playlist are written as numbers
(`halo3_options.choices_as_numbers`); the window shows their labels.

## Module by module

**Huragok's own** (top level)

| Module | |
| --- | --- |
| `huragok.py` | **Wiring**: the settings, bus, database and runners; the Superintendent, Vergil and the parts (each with the context of its persona); the background tasks (restarted after `RESTART_SECONDS` if one fails); the requests only Huragok answers (`request(verb, …)`); the relaunch after an update (it waits for servers part way through a restart, `RELAUNCH_WAIT`); settled host-file steps (`_settle_files`) |
| `superintendent.py` | Servers found (`discover_servers()` every 10 s), added and removed, each a `Server`; the fleet; `SuperintendentContext` |
| `vergil.py` | The ledger, roles, the router, commands.toml in force, the relay; `VergilContext` |
| `parts.py` | `PARTS`, and checking a part's section of settings.toml |
| `rcon.py` | One RCON connection per server (a refused password is tried again only when trying can't add up to a lockout: see the top of `rcon.py`); `ban_list`, the server's ban list in one shape |
| `steam.py` | SteamCMD found, installed, signed in and written into every server; one depot of MCC downloaded (`download_depot`); whether a server's SteamCMD can fetch at all (`cannot_fetch`); MCC found only where Steam's own records say it is (`find_mcc`: the registry, `libraryfolders.vdf`, `appmanifest_976730.acf`; never a drive search); the Workshop's API |

**`core/`**

| Module | |
| --- | --- |
| `settings.py`, `tables.py` | `settings.toml`. Types and minimums (`MINIMUMS`) are checked as it loads; a key that isn't a setting (`KNOWN_KEYS`), a value an older version wrote (`OLD_VALUES`) and a retired setting (`RETIRED`) become warnings. `tables.py` reads each value with its kind and names every mistake with the file and key. `launcher()` is what the host types to run Huragok, for the messages that tell them to |
| `serverfolders.py`, `serversettings.py` | The servers: every `SERVERS/<folder>/dedicated.toml` (plus `server_folder`) read into a `ServerConfig`, checked against each other (unique names, slugs and ports; RCON on); what each one uses (`settings.of(server)`, above) |
| `commandsfile.py`, `hostsettings.py` | `commands.toml` (a file missing or empty for a moment keeps what's in force); settings.toml as a catalogue of every setting, for the window's guided editors |
| `part.py`, `runner.py`, `context.py` | The part system: `Part`, `@command`, `@on`, `@every`; each part's queue, its handlers' time limit (`max(handler_timeout, [rcon] command_timeout + 5)`), switching a failing part off and, after `COOL_DOWN`, on again; `PartContext` |
| `events.py`, `storage.py`, `mailbox.py`, `lock.py` | The bus; SQLite (players seen, the ledger, each part's tables); requests from outside (`start.cmd stop`, a second window) and their replies, as files; one Huragok per folder (`records/huragok.pid`) |
| `tomledit.py` | Editing `settings.toml` and `dedicated.toml` in place (keys, tables, `[[server]]` blocks), keeping comments. Every write makes the change in memory, reads the result back, and replaces the file (temp + replace) only if exactly that change differs; otherwise `TomlEditError`, with the file untouched |
| `versions.py` | Every version of a host's file Huragok keeps (playlists): `keep` before and after each change Huragok makes, `seen` for a change made by hand, `versions` newest first, each in a folder of its own under `records/<kind>/<owner>/<file>/` with its `about.json` |
| `files.py`, `text.py` | Written once for everyone: `write_atomic`, `read_text`, `mtime`, folder sizes; durations read and written (`seconds_of`, `describe_seconds`), `one_line`, `size_text`, `slug` (a name as a file's), player IDs (`PLAYER_ID`, `is_player_id`), text as it looks (`lookalike`: Greek, Cyrillic, small capitals, fullwidth and styled letters as Latin, for the word list and player lookups alike). They import nothing of Huragok's, so `games/` uses them too |
| `background.py` | Work started to run by itself (a reply, a restart, an install, a ban passed on): `background(work, what, log)` keeps the task until it ends, logs a failure at once as "<what> failed", and `stop()` calls off what's left as Huragok stops, before the database closes. **Never a bare `asyncio.ensure_future`** for work nobody waits on: asyncio keeps only a weak reference, and a failure is logged without saying what it was. Long-running tasks are `Huragok.spawn`'s (restarted after a failure) |
| `web.py`, `jobs.py`, `requests.py` | One way to fetch from GitHub (`fetch`, `UpdateError`); work that needs a server stopped (`StoppedJob`); what a request gets back (`Result`) |
| `logs.py`, `desktop.py`, `priority.py`, `winproc.py` | The tagged log lines (`TaggedFormatter`) and the dashboard's recent ones (`RecentLog`); opening folders, editors, log windows, start at boot; process priority; starting `dedicated` on Windows so it outlives Huragok, and Ctrl+Break |

**`games/`**

| Module | |
| --- | --- |
| `halo3.py` | Playlist files built and edited from picks; Halo 3's campaign missions, difficulties and its 27 maps |
| `halo3_options.py`, `halo3_options_data.py` | **Game options**: the catalogue (generated: see below), presets (saved custom games) and playlist-game overrides; `LABEL_CHANGES` |
| `halo3_mapfile.py` | Why the server program skipped a mod map, from its own checks on the `.map` |
| `odst.py` | ODST servers: what they can play and how Huragok says it |
| `plays.py`, `files.py` | What a server plays (`plays_kind`, co-op, ODST's console refusal); the game files each kind needs and where MCC keeps them (`DEPOTS`) |

**`program/`**

| Module | |
| --- | --- |
| `dedicated.py` | Running the server program's own `dedicated` commands (exe on Windows, wine on Linux; a server folder's own copy, else `[setup] program` or the newest copy here) and reading their listings, a timeout or Esc ending the whole process tree |
| `settings.py` | Every setting a host can change about how a server runs (`SETTINGS`: key, scope, type, default, group, when it applies; add a row for a new one); `path_setting` says where a server's `game`, `content` or `bans` points |
| `plays.py` | What a server plays, written into its `dedicated.toml` (`write_plays`: one kind in, every other kind out, in one write), and which kinds and campaign modifiers each version of the program takes (`PLAYS_SINCE`, `plays_problem`) |
| `releases.py` | The server program's releases on GitHub |

**`servers/`** (the Superintendent's)

| Module | |
| --- | --- |
| `server.py` | **One `ServerManager` per server**: the state machine below, ticked once a second. Timers run on `time.monotonic()`; wall time only for the hourly budget, history, uptime and the daily slot |
| `fleet.py` | The `Fleet`, which ticks every manager, writes `records/live.json`, answers requests from `records/requests/` and is what the window's keys call; `Lifecycle`, the thin view parts get (`restart`, `cancel`, `planned`, `busy`) |
| `backends.py`, `ports.py`, `keepalive.py` | The **backends**: `process` (one `dedicated` per server, Windows or Linux), `docker`, `command`, `attach`. A backend only starts, stops and watches processes; it decides nothing. `PortWatch` reads the system's listening sockets (psutil) at most once a second for every stopped server, so no check blocks the loop. `Keepalive` restarts a hung server. `memory_mb` is what a server's process tree uses, for the watchdog and V alike |
| `state.py`, `logs.py` | The live picture of each server; `server.log`, `status.json` and the console output as events. `logs.py` matches every pattern at the start of the server's own message, and the lines that carry someone's words (console echoes, refusals, kick and ban reasons: `ECHOES`) first, so typed text never passes for a server line |
| `watchdog.py`, `eventlog.py` | The Superintendent's parts |

**`players/`** (Vergil's)

| Module | |
| --- | --- |
| `ledger.py`, `roles.py` | One ledger for every warn, mute, kick and ban (`ModerationView` is a part's); what the roles mean |
| `router.py`, `targets.py`, `who.py` | `!` commands in chat (`CommandCall`); who a command means, with every guard (`targets.ask`, `offer`); who the server says acted |
| `outbox.py` | Each server's queue of what's passed on to it from another (a ban, an unban, an admin, a kick, a pardon, Huragok's list as it connects): worked through in the background one job at a time, `SPACING` apart, `JOB_SECONDS` each at most, so a frozen server holds up only its own queue and never a command or the part's time limit; `results(jobs, seconds)` for a reply that says how it went. A server not connected is skipped: Huragok's list is put on every server again as it connects and every ten minutes |
| `relay.py` | The console relay: the server's WebSocket console protocol served by Huragok, one port per server, moderators by password and role, commands gated by `commands.toml`'s lists and forwarded with `by`, events fanned out |
| `automod/`, `commands/`, `messages/` | Vergil's parts |

**`content/`** and **`updates/`** (Huragok's)

| Module | |
| --- | --- |
| `content/newserver.py`, `packages.py` | **Setting servers up**: making a server folder from a package with the root's shared `game/` and `content/` (never a ban list: every server keeps its own); packages (`PACKAGES/`, the starters in `templates/packages`) |
| `content/catalogue.py`, `mccimport.py` | What a server can play as one catalogue (the listing, the mods' ModInfo.json and Forge layouts, the Workshop items) with numbered picking; MCC's variants imported |
| `content/gamefiles.py` | **What a server arrives without**, put right as its folder is found and before each start (`GameFiles.prelaunch`, the Fleet's `prelaunch` hook): a missing ban list or content folder made, a ban list shared from before 0.5.0 copied into the server's folder as it starts (`own_bans`), a game path or `[workshop] steamcmd` from another PC pointed at this one's, Halo 3's files (and the campaign's, ODST's) got in the background from MCC here or with SteamCMD while the server waits as WAITING. Process backend only |
| `content/modcheck.py`, `mods.py` | Each mod's health, from its files; the servers' mods watched while Huragok runs |
| `content/serverops.py`, `upkeep.py` | Renaming a server and giving it its own content folder; clean-up, firewall rules, Halo 3's engine file |
| `updates/engine.py` | New versions of Huragok from GitHub (renames only, the backup in `records/previous-<version>/`, `records/last-update.json`, `put_back`) |
| `updates/program.py`, `workshop.py` | New releases of the server program (servers with `auto_update` update themselves; the others from GitHub after their game, with `follow`); new Workshop versions swapped in after the game, as a job the manager runs while the server is stopped (the `preparing` phase) |
| `updates/hostfiles.py` | **The host's own files brought up to date** after an update: the `STEPS`, run at Huragok's start under its lock, each once (`records/files.json`), each file backed up first (`records/previous-files/<version>/`). See "Hosts' files across updates" below |

**`window/`** and **`commandline/`**

| Module | |
| --- | --- |
| `window/dashboard.py`, `controllers.py` | The dashboard and its keys (`key_rows`); what the window talks to: the running Huragok (`LocalController`) or, in a second window, its files (`RemoteController`, fed by `live.json`) |
| `window/screens/` | One `Screens` base (drawing, asking, the pickers several screens share) and a file per group: `setup.py` (N, G, I, C), `manage.py` (A, D's server settings, B, U), `files.py` (D's guided editors for Huragok's own files, O), `games.py` (playlist games' rules and options, custom games) |
| `window/tui.py`, `statuscard.py` | The terminal: the keyboard (Windows console key events, `ReadConsoleInputW`, with `msvcrt` when the console can't be opened; Linux `termios`), colours and in-place redraw. `ConsoleWriter` writes the console from a thread of its own, so a paused console (Edit > Mark) never holds up the loop; `clean` takes control characters out of what players and servers send, `cells` measures wide characters. The status card (V) |
| `commandline/cli.py`, `common.py` | The commands `start`, `status`, `stop`, `close`, `restart`, `say`, `rcon`, `watch`, `check`, `update`, `init`, and `--help` grouped by named part (`HELP_GROUPS`); the exit codes |
| `commandline/launcher.py`, `run.py` | The start loop below; Huragok in this process (`start --child`) |
| `commandline/setup.py`, `check.py`, `firstrun.py` | The `new`, `package`, `content`, `playlist`, `variant`, `workshop`, `steam` and `dedicated` commands (they work without the fleet, as the window's setup screens do); `check`; the first start's files |
| `commandline/console.py`, `logsetup.py`, `shell.py`, `watch.py` | Typed commands without a dashboard (`--no-menu`); where log lines go; what the commands print; following a log |

## A server's phases
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

Before `launch` starts a stopped server, the Fleet's `prelaunch` hook (content/gamefiles.py)
puts right what its folder lacks; while something is missing (Halo 3's files being
got, or no way to get them) the server stays `idle` with `waiting_for` set: the row
says WAITING and why, it looks again every `WAIT_RECHECK` (30 s) or as soon as
`Fleet.release()` says the files arrived, and nothing counts as a failed start. A
stopped server whose port answers but writes no `status.json` in its own folder isn't
taken for `external`: it waits the same way (`waiting_kind = "port"`), since that's
another copy of it, running from another folder.

## The start loop and the exit codes
`start` (what `start.cmd` and `start.sh` run) doesn't run Huragok itself: its
`start_loop` (commandline/launcher.py) runs `python -m huragok start --child` as a process of its own and
waits for it, holding the lock (`records/huragok.pid`, marked `launcher`) while it
starts it again, so a second window or a `start.cmd stop` never steps into the
gap. This process stays on the code it started with, whatever an update puts on
disk. Every deliberate end has its own exit code (`commandline/common.py`):

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
back the backup `records/last-update.json` names (`put_back` in updates/engine.py, loaded before
the update ran), notes it in `records/failed-update.json`, and starts the old
version. Every step is printed and logged (`launcher:` lines in `huragok.log`, or
`logs/launcher.log` when settings.toml can't be read). It never touches a server.

## Who's who in what the host sees
[lore.py](../engine/huragok/lore.py) gives each named part its tag and colour
(the table at the top), and what the host reads follows it.

- **Tags.** `lore.speaker(logger)` maps a logger name (without the `huragok.`
  prefix) to its named part through `SPEAKERS`, by package and whole dotted names
  (`servers`, `superintendent`, `rcon` and a few of `core`'s are the Superintendent's;
  `players` and `vergil` are Vergil's), Huragok otherwise. `OLD_LOGGERS` holds the
  module names from before the engine was laid out by part (`manager`, `chat`…), so
  a second window still tags an older version's lines. `TaggedFormatter`
  (core/logs.py) uses it for the dashboard and the plain `--no-menu` log
  (`21:03:40 SUPT INFO    [Slayer] …`). `logs/huragok.log` keeps the module name
  instead (`2026-10-18 21:03:40,123 INFO    servers.server: [Slayer] …`); a second
  window turns it back into the tag when it shows the file's lines.
- **Keys and screens.** `KEYS` maps each dashboard key to its named part;
  `dashboard.key_rows` groups the keys the same way, and `Menu.title` starts every
  screen's title with its part, in its colour (`SUPERINTENDENT › RESTART`). A new
  key goes in `KEYS` too: `tests/test_lore.py` fails otherwise.
- **What players read.** `NOTICES` holds the Superintendent's own phrases, word for
  word, with where each is from (Bungie's 2008 "KEEP IT CLEAN" campaign, the manual and
  trailer, the levels' speakers, Sadie's Story's subtitles); `tests/test_lore.py` pins
  them, so a new one needs a source, never invented words. `CALLOUTS` says which face
  (`FACES`) and phrase head each line players read, and `callout(situation, text)`
  makes the line: the face, the phrase, then what happened, why and what comes next in
  plain words. A new line for
  players goes through `callout`, with a `CALLOUTS` entry of its own. Automod's
  `MESSAGES` take theirs as `{notice}` (`notice(situation)`: face and phrase), hardening
  with the step: `[o_O] KEEP IT CLEAN!` for a mute, `[>_<] CRIME DOESN'T PAY!` for a
  removal or a ban, and `[^_^] YOUR TAX DOLLARS AT WORK!` for the `public` line everyone
  reads. A cancelled countdown is a face and no phrase, ending with `SIGN_OFF`,
  `Keep it clean!`.
- **The face.** `lore.mood(rows)` picks the Superintendent's expression, a notice
  and a summary from the dashboard's rows, the worst thing first (FAULTED or a
  config error, then a crash backing off, a countdown, a start, players on, nobody
  on, everything stopped). The dashboard's second line and `status` show it.
- **What didn't change.** Settings keys and section names (`[watchdog]`,
  `[automod]`, `[relay]`…) stayed as they were, so every host's `settings.toml`
  still reads. A few internal names stayed too: `running_supervisor()`, the
  `supervising` flag and the `supervisor.*` events. Renaming them would change
  nothing a host sees, and would break anything that listens for those events.

## Adding a command
Most new things are commands. Put one in the file of
[players/commands](../engine/huragok/players/commands) it fits
(`players.py` for looking people up, `moderation.py` for acting on them,
`server.py` for running the server), or start a new file for a new group and add
its class to `Commands` in `players/commands/__init__.py`.

```python
# players/commands/players.py
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
- **A command that names a player declares it**, and the router finds them before the
  command runs, with every guard ([players/targets.py](../engine/huragok/players/targets.py)):

  ```python
  @command("kick", usage="<player> [reason]", help="…", role="moderator", player="on", acts=True)
  async def kick(self, call: CommandCall) -> None:
      await self.ctx.moderation.kick(call.server, call.target, unquote(call.rest), by=call.player.name)
  ```

  `player="on"` (someone on this server) or `"seen"` (someone who left too, as a
  `Player` with number 0, by player ID); `acts=True` refuses anyone of the caller's
  role or above; `start=True` lets a name's start do when one player on has it (for
  commands that only look or lift); `follows` names what may come right after the
  name (`"time"`, `"team"`, or a method of the part) so "Nate 10m" isn't taken for a
  typo of "Nate Higgers"; `words_after=False` when everything typed is the name.
  `call.target` is the player, `call.rest` the words after them, as typed. Without
  the declaration nobody is found for you: a command with its own grammar (`!strike`,
  `!admin add`) calls `await call.player_and_rest(text, scope=…, acts=…)` or
  `await call.find(ref, scope=…)`, the same engine.
- What the engine does, for every command alike: the longest name the line starts
  with (players on, names seen), quotes as on the console, `#number`, `@number` (the
  caller's last list), a player ID or its first 8+ characters; a name several players
  have acts on nobody and lists each with what tells them apart (team, time on or
  last here, role, muted, ID start), `#4` for those on, `@1` for those not
  (`targets.ask`); never a name's start for an action. A list command numbers its
  entries with `await offer(call, candidates)` so `@numbers` reach them.
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
one) in the folder of the named part it belongs to (`servers/` for the
Superintendent's, `players/` for Vergil's) with one `Part` subclass and its
`persona`, listed in [parts.py](../engine/huragok/parts.py).

```python
# players/greeter.py
from ..core.events import Event
from ..core.part import Part, command, every, on
from ..lore import VERGIL
from .router import CommandCall


class Greeter(Part):
    name = "greeter"                       # its section in settings.toml: [greeter]
    persona = VERGIL                       # whose part it is: its ctx (below) and the tag on its log lines
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
Superintendent or Vergil, so add it there too. `check` shows each part, whether it's on and any problem
with its settings: it runs the part's `check_config` on a stand-in context, and a
part that reads its own files can check them there too by defining
`preflight(config, home)`, as automod does with its word list.

## What `self.ctx` gives a part
A part's `ctx` is its persona's context ([core/context.py](../engine/huragok/core/context.py)),
each one the one before and more: `PartContext` is what every part has;
`SuperintendentContext` (superintendent.py) adds the servers, for the
Superintendent's parts; `VergilContext` (vergil.py) adds the players, for Vergil's.

| | | |
| --- | --- | --- |
| `ctx.servers` | Superintendent | Each server's live picture by name: `players` (by `#number`: `name`, `id`, `ip`, `admin`, `muted`, `team`), `phase`, `map`, `mode`, `max_players`, `admins`, `connected`, `status_age`, `rcon_latency_ms`. Servers added while running appear here. |
| `ctx.server(name)` | Superintendent | One of those |
| `ctx.say(server, text)` | Superintendent | A `[Server]` line everyone reads |
| `ctx.tell(server, player, text)` | Superintendent | A `[Server]` line one player reads (a `Player` or a player ID) |
| `ctx.players.get(id)`, `.find(text)` | Vergil | Everyone this Huragok has seen (one database per root): names used, IPs, first and last seen, visits |
| `ctx.role_of(server, player)`, `ctx.highest_role(player)` | Vergil | `player` (everyone), `member`, `moderator`, `admin` (the server's own admins) or `owner`; on whichever server ranks them highest |
| `ctx.permissions` | Vergil | `commands.toml` as it's in force now (it reloads when edited) |
| `ctx.moderation` | Vergil | `warn`, `mute`, `unmute`, `kick`, `ban`, `unban`, `pardon`: recorded with a reason, shown to players as by your part (or the `by=` you pass), and dry-run aware (`live=True` sends one through your part's dry run, for putting back what a person did; `pending=True` records a mute for someone who isn't on, for automod to apply as they join). Use these for anything done to a player |
| `ctx.lifecycle` | Superintendent | `restart(server, when="now" / "after_game" / "when_empty", reason=…, countdown=…)`, `cancel(server)`, `planned`, `busy`, `can_control`. `countdown=0` means right away; a number is the seconds of notice; none means the host's `warn_seconds` |
| `ctx.rcon(server)` | Superintendent | The raw console: `await ctx.rcon(server).command("load", "Guardian", "Slayer")`, for what the rest doesn't cover |
| `ctx.db` | every part | The shared SQLite database. `ctx.db.migrate([...])` creates your tables (prefix them with your part's name); `execute` and `query` run SQL |
| `ctx.config` | every part | Your section with `defaults` filled in (also `self.config`) |
| `ctx.emit(name, server, **data)` | every part | An event for the other parts, named `<your part>.<name>` |
| `ctx.log` | every part | A logger named `part.<your part>` (also `self.log`); `lore.SPEAKERS` decides the tag its lines carry in the window |
| `ctx.home` | every part | Huragok's folder, the root (settings.toml, commands.toml…), for files of your own |
| `ctx.dry_run` | every part | Whether the host set `dry_run` for your part (or for everything) |
| `ctx.server_option(server, key)` | Superintendent | One of Huragok's own settings for a server (its `[[server]]` block: `automod`, `priority`…), or None |
| `ctx.part_option(part, key, default)` | every part | A setting of another part's section (`[automod] across_servers`) |
| `ctx.running(part)` | every part | Whether a part is on and running now |

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
| `server.repaired` | `text`: what Huragok put right in a server's folder (a ban list made, a game path pointed here: content/gamefiles.py) |
| `log.falling_behind` | `fps`, `behind`, `window`, `average`, `longest_ms`, `players` |
| `log.upload` | `rate`, `unit`, `players` |
| `log.roster_lost` | `lost`, `of`, `present`, `details` (the engine's JSON snapshot, as text) |
| `log.connected`, `log.joined` | `name` (and `id` on `connected`): a player past the admission check, then in once the map has loaded (the roster line, or ODST's "joined the game in progress") |
| `log.rcon_command` | `address`, `by`, `command`: a console command the server echoed, which is never matched as one of its own lines |
| `log.rcon_wrong_password` | `address`, and at the fifth `times`, `refused`, `minutes`: the server locking an address out |
| `log.vote_refused` | `caller`, `reason` |
| `log.rcon_signin`, `log.rcon_refused`, `log.reload`, `log.online` | from `server.log` |
| `moderation.action` | `kind`, `target_id`, `target_name`, `by`, `source` (the part), `reason`, `duration`, `evidence`, `dry_run`, `ok`, `text`, `pending` (a mute for someone not on, applied by automod as they join) |
| `supervisor.started`, `supervisor.stopping` | `version` and `parts` on `started` |
| `supervisor.part_disabled`, `supervisor.part_enabled` | `part`; `reason`, `for_good` and `seconds` (until it's on again) on `part_disabled` |
| `supervisor.commands_reloaded` | `commands.toml` was read again |
| `supervisor.game_files` | `kind` (`halo3`, `campaign`, `odst`), `text`: game files arrived in this folder's `game/` |
| `automod.strike`, `automod.pardon` | `player_id`, `player_name`, `rule`, `strike`, `action` (`kick`, `kick 1h`, `mute 1d`, `mute perm`, `ban`), `source` (`chat`, `manual`), `evidence`, `by`, `dry_run` |
| `automod.name` | `player_id`, `player_name`, `caught_by` (`names.txt line 4`, `automod.txt slurs`), `by`, `dry_run`, `ok`: a name kicked as it joined |
| `automod.name_banned`, `automod.name_allowed` | `text` (the name), `by`; `player_id` and `note` on `name_banned` |
| `automod.words_banned`, `automod.word_allowed` | `!banword`: `words`, `allowed` (its `-word` exceptions), `rule`, `line`, `by`; `!unbanword`: `word`, `rules` (the lines it came off), `by` |
| `automod.rule_changed` | `!changebanword`: `rule`, `line`, `by` |
| `watchdog.health` | `level` (`healthy`, `degraded`, `critical`), `previous`, `reasons`; the dashboard shows it |

Switch on `[eventlog]` to see every event as JSON while you work.

## Ground rules
- **Hosts' files move forward with Huragok, never backward or by hand.** Since 0.4.0
  went out, every release reads `settings.toml`, `commands.toml`, `automod.txt`,
  `names.txt`, `intervalmessages.txt`, `PACKAGES/` and `records/` as any version from
  0.4.0 on wrote them, and
  a change to their format comes with a step in `updates/hostfiles.py` that brings an existing
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
- **Test against the fake server:** `engine/tests/testkit/fake_rcon.py` is a server's RCON,
  `engine/tests/testkit/fake_dedicated.py` a stand-in `dedicated run` that the process
  backend really starts, adopts, stops (Ctrl+Break / SIGINT) and sees crash.
  `servers/test_server.py` drives the state machine with a clock moved by hand;
  `servers/test_watchdog.py` feeds the watchdog the inputs it really gets (log events, the
  console's events, the server's state) and follows a log line to a restart;
  `window/test_dashboard.py` drives the window with a scripted keyboard; `test_surfaces.py`
  provokes every message the main flows don't reach (refusals, edge cases, the
  log lines for what goes wrong at run time) and checks the words the host reads;
  `core/test_tomledit.py` runs every kind of edit over unusual but valid files and
  checks each either reads back as exactly that change or leaves the file as it
  was; `players/test_relay.py` checks the relay's gate (roles, `[off]`, control characters,
  what moderators below admin don't see); `test_lore.py` checks who's who: the tag
  each logger gets, that every key belongs to a named part and shows on the
  dashboard, and the Superintendent's face for each state;
  `architecture/test_layers.py` checks who may import whom; `updates/test_from_0_4.py`
  what a 0.4.x Huragok asks of the version it updates to; `architecture/test_guards.py`
  checks the guards every test runs inside (below).

## Hosts' files across updates
An update replaces the program and nothing else, so a host's `settings.toml` is the one
their first start wrote, however many versions ago. Huragok keeps it in step itself:
`updates/hostfiles.py` runs as Huragok starts (`commandline/run.py`, under the lock, before
any server is looked after), and `check` lists what the next start would change. When a
release changes what a host's file should say:

1. **The reader first.** The code reads every older form, as if no step had run: a new
   setting has a default, a renamed one still reads its old name (`OLD_VALUES`,
   `RETIRED` name what an old file says). A step that fails, or a file that can't be
   read, must never stop Huragok.
2. **A step** in `hostfiles.STEPS`, keyed `<version>/<what>`: it looks before it changes
   anything (a second run changes nothing), changes TOML only through `tomledit`
   (`add_table_key`, `comment_out_table_key`, `replace_comments`, `set_table_key`), and
   calls `ctx.backup(path)` before its first change. Leave the host's own values and
   comments alone: add what's missing, and turn what does nothing into a comment that
   says why. A changed **note** needs no step (below).
3. **Files the version before can still read.** The start loop puts back a new version
   that fails to start, and the version put back runs on the files as the steps left
   them: add keys (an older version names an unknown key, and runs), don't remove or
   rename tables. A change the version before **can't** read is a `settled` step: the
   start loop that puts the old version back is the old version's own code (it ends
   the new one's run within its `FAILED_START_SECONDS`, 120 s so far), so a settled
   step runs from the running Huragok once it has been up `hostfiles.SETTLED_SECONDS`
   (150 s, `Huragok._settle_files`), never at the start. A new in-game command is one:
   0.4.0 to 0.4.4 refused to start with a command they don't have in `commands.toml`
   (since 0.5.0 it's a warning, `Permissions.unknown`, in the log and `check`).
4. **A test** in `tests/updates/test_hostfiles.py`, from the files 0.4.0 wrote (`tests/data/host-0.4.0`).
   `test_every_template_key_reaches_an_older_hosts_file` fails when the template gains a
   setting that no step adds to an older file (or that `NOT_ADDED` doesn't explain).

`always` steps run at every start and only add what's missing: in-game commands newer
than `commands.toml` (under their default role; settled, so 150 s after the start),
starter packages newer than `PACKAGES/` (`records/files.json` remembers which were
offered, so one the host removed stays removed).

**Notes need no step of their own** (0.5.0). Once a version, at its first start, the
`<version>/notes` step puts Huragok's notes in every host file as that version writes
them (`settings.toml`, `commands.toml`, `automod.txt`, `names.txt`, every
`intervalmessages.txt`), and keeps the host's where they put them, between Huragok's
lines too (`updates/notes.py` says how). It knows Huragok's lines word for word from
`huragok/templates/notes-history.json`, every note line each version since 0.4.0 wrote,
by the thing it was written above. So after changing a note in a template, in a step's
comment or in a command's `help`, run

    python engine/tools/notes_history.py

which adds this version's lines to the history (it only ever grows: hosts' files still
have the old ones). `tests/updates/test_notes.py` fails until you do. Never take a line
out of the history, and never edit it by hand.

A command whose default role changes doesn't move in a host's `commands.toml` by
itself (the file lists it under the old role, and the file wins). When a release
means to move it, a one-time step does, and only from where earlier versions put it:
0.5.0's `banname_for_admins` moves `!banname` from `[moderator]` to `[admin]`, and
leaves it alone anywhere else the host put it (another role, `[off]`), or once the
host moved it back.

### What 0.4.x asks of an update
An update swaps `engine/huragok` as one folder, by rename, and the Huragok that
installed it keeps running on its own code for a while: it installs the Python
packages, then waits for any server part way through a restart. Its start loop
stays on its own code until the window is reopened, starts the new version, and
puts it back when it ends within 120 s. What that old code reads from disk after
the swap is this version's, so these stay as 0.4.x spells them (and
[tests/updates/test_from_0_4.py](../engine/tests/updates/test_from_0_4.py) checks
each): `engine/huragok/__init__.py` with its literal `__version__ = "x.y.z"` line,
`python -m huragok -c <settings> start --child [--verbose] [--no-menu]`,
`_ctrl-break <pid>`, the exit codes, `records/huragok.pid`,
`records/last-update.json` (its keys), `records/previous-<v>/`,
`records/failed-update.json`, `records/requests/`, `engine/requirements.txt`, and
[winproc.py](../engine/huragok/winproc.py) at its old path with the same names
(a 0.4.x Huragok imports it the next time it stops a server on Windows). The
window screens a 0.4.x window could still open in those minutes (the status card,
renaming a server, clean-up) moved: such a window shows "That didn't work" until
it restarts. From 0.5.0 on, an install imports every module of the running
version before the swap (`load_every_module`), so no later version has to keep
anything at an old path.

## Running the tests
From the repository's top folder:

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine -q > tests.txt 2>&1
```

- Nearly a thousand, about four minutes on Windows (987 passed and 1 skipped at the
  last full run: a time-zone test that needs `time.tzset`, which Windows lacks).
  They're laid out like the package, one folder per layer, with what several share
  in `tests/testkit/` ([tests/README.md](../engine/tests/README.md)).
  They need pytest 9 (the `test` extra says so).
- **They start real processes.** The process backend starts, adopts, stops and
  crashes `fake_dedicated.py`; `commandline/test_cli.py` runs `start.cmd`/`start.sh` themselves
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
  checked by `architecture/test_guards.py`). A PC that runs Project Reclaimer servers has them on
  49175-49185, and Windows hands out a program's own ports from 49152 up, so
  `free_port()`/`free_ports()` never return a port from 49170 to 49199 or one in
  use, new servers made in a test are given ports past them, and a guard fails any
  test that connects to one of those ports or to anything past this machine
  (GitHub, Steam): a console's five wrong passwords lock every tool on the machine
  out of it. Steam, MCC and SteamCMD are found only where a test puts them, never
  this PC's own. A fake server ends itself once the test session's process is gone
  (`HURAGOK_TEST_PARENT`), and the servers a test's `running-servers.json` still
  names are ended after it (only those this session started).
- One folder, one file or one test: `pytest engine/tests/players`,
  `pytest engine/tests/servers/test_watchdog.py`, `pytest engine -k no_joins`.

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
`huragok/games/halo3_options_data.py` is generated, not written by hand: every key the server
program accepts, its label, range and the meaning of every number, from the program's
own answers kept in `tests/data/game-options-probe.txt`. After a Project Reclaimer
release that adds or renames game options, probe again from `engine/`:
`python tools/reprobe_gameoptions.py <project-reclaimer.exe> <a server folder>
tests/data/game-options-probe.txt new.txt` (`mode-check` with one option at 99999 per
game type, and `dedicated playlist show` on scratch playlists naming every number;
about 20 seconds; the server folder needs Guardian). Compare `new.txt` with the probe
file, move it over it, and run `python tools/gen_gameoptions.py`. `tests/games/test_halo3_options.py`
checks the catalogue against the probe file. A renamed label matters: a playlist that
names a label the server's program doesn't take stops `dedicated check`, so the
server won't start. Add it to `halo3_options.LABEL_CHANGES` (the option, the old label,
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
  `commands.toml`, `automod.txt`, `names.txt`, `intervalmessages.txt` and the
  `PACKAGES/` starters), and
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

**A beta** is released the same way, with a pre-release version
(`__version__ = "0.5.1-beta.1"`, tag `v0.5.1-beta.1`; `b1` and `-rc.1` read the same,
`core/web.version_key`) and **always published as a pre-release** (`gh release create …
--prerelease`). The rule has no exceptions: that mark is what keeps it from everyone else.
Hosts without `[updates] beta = true`, and every version up to 0.4.4, ask GitHub for
`/releases/latest`, which never names a pre-release; a beta published without the mark
would go to every 0.4.x host. Two guards back the rule up, neither of them in time for
0.4.x: the Release check workflow (`.github/workflows/release-check.yml`, on every release
published or edited) marks a beta tag published as a full release as a pre-release and
fails, fails a full release marked as a pre-release, and checks the release has its
`huragok-<version>.zip` with the tag's `__version__` inside; and Huragok 0.5.0 on, with
beta off, refuses a latest release whose tag is a beta. Hosts with beta on take the
highest version among the releases, so the full release (`v0.5.1`) passes its betas.
A 0.4.x host can't be offered a pre-release at all: the first beta after 0.4.x is a hand
install (README, "Trying a beta from 0.4.x").

Running copies of Huragok find it within `check_hours` and offer it to their host;
the first six lines of the notes (headings left out) are what their window shows,
so start with what changed for hosts. An update replaces `engine/` (except
`engine/.venv`), the start scripts, `docs/`, `README.md` and `LICENSE`; the host's own
files are brought up to date by the new version's `updates/hostfiles.py` as it starts. Before
tagging, install the zip over a copy of the previous release's install (its settings,
servers and records) and start it: it must come up with no warnings about its files,
and `check` must list nothing still to bring up to date. A copy from before the rename
(Reclaimer Supervisor 0.2.1) can't update itself to Huragok: its hosts reinstall (the
README says how).
