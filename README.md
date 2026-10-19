# Huragok

The Engineer for your [Project Reclaimer](https://projectreclaimer.dev) servers:
one window that runs them. Every server is a folder under `SERVERS/`; the window
shows them all, starts them, keeps them up, and gives you single-key control. It:

- **runs your servers and keeps them up** (the Superintendent): starts them when
  it opens, restarts one that crashed (backing off, and giving up rather than
  thrashing), warns players before any planned restart or stop, and restarts a
  struggling one after the game instead of mid-game;
- **acts on words on your list** (Vergil's automod): each strike in chat is the
  next step on that word's line, kicks (out 1 hour, then 1 day…), then mutes (1
  day, then 3…), then a ban, as you set it. Mutes last through restarts. A name
  with a listed word, or a name a moderator banned, is kicked as it joins until
  it changes;
- **gives in-game commands to the people you pick** (Vergil): the server's
  console-only commands (mute, map, votes, say…) and its own, each for `everyone`,
  `member`, `moderator`, `admin` or `owner`, by player ID, and keeps everyone you
  list safe from votes to kick. One role each, moved a step at a time in game
  (`!promote`, `!demote`), and a ban takes it away;
- **records everything** (Vergil): every player seen (names, visits) and every
  warn, mute, kick and ban, with who did it and why;
- **sets servers up** (Huragok): a new server from a package in one key
  (`dedicated init`, Halo 3's files shared from the root, the playlist and Workshop
  items of the package, `dedicated check`), playlists and Workshop items managed
  from the window, any server's files edited with the server's own reload result
  shown;
- **manages them while they run:** a console with the server's events and your name
  on what you do, settings changed live (the server applies most without a
  restart), bans and admins, and a **relay** so moderators elsewhere use the official
  RCON client through Huragok, each with their own password and role;
- **keeps them current** (Huragok): a new Project Reclaimer release is put in place
  of every copy and each server restarts after its game, not mid-game.

**Closing the window never stops the servers.** They keep running, unsupervised;
open the window again and it picks them straight back up. If Huragok itself stops
unexpectedly, its launcher starts it again within seconds, and it picks the servers
up the same way. Huragok is run on Windows 11. Linux works too (the server program
runs through `wine` there), but it's experimental and on hold: it isn't tested end
to end. It never patches the server: it uses what the server offers (its remote
console, its files and its `dedicated` command), and it changes a server's
`dedicated.toml` only when you do, from the window's D, G and I screens.

> **Status: 0.4.0, the Windows release, on its way to 1.0.** Tested against Project
> Reclaimer **v0.9.20** on Windows 11; Linux is experimental until 0.5.0. Up to 0.2.1 this was
> called Reclaimer Supervisor: see [Coming from Reclaimer Supervisor 0.2.1](#coming-from-reclaimer-supervisor-021).

```text
 HURAGOK 0.4.0   (supervising)   the Engineer for Project Reclaimer servers
 [o_O] SUPERINTENDENT · PLEASE REMAIN CALM! · Slayer restart in 2m 10s
 Sat 18 Oct 2026  21:04:12      C:\reclaimer
 SUPT backend process · servers in SERVERS/ · watchdog (act) · eventlog
 VRGL commands · automod (dry run) · consoles: Alice (Big Team Battle)  |  HRGK server program 0.9.20
 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  #  SERVER          PORT   AUTO STATUS                               PLAYERS            GAME
  1  Big Team Battle 49176  yes  RUNNING  up 5h 12m                   14/16 Alice, B...  playing Valhalla Slayer
       daily restart 04:00 (in 6h 55m)  |  RCON 1 ms  |  status 3 s old
  2  Slayer          49177  yes  RESTART IN 2m 10s                    6/16 Cy, Dee, …    finished: Guardian Slayer
       restart reason: new map pack   [K] cancels it
  3  Test            49178  no   stopped
 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  SUPERINTENDENT  [S] Start  [X] Stop  [R] Restart  [K] Cancel  [T] Autostart  [L] Log  [V] View a server
  VERGIL          [P] Players  [M] Message  [A] Console  [B] Bans & admins
  HURAGOK         [N] New server  [G] Game content  [I] SteamCMD  [D] Settings  [C] Check
                  [O] Open  [U] Updates  [W] Start at boot: ON  [H] Help  [Q] Quit
 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
  RECENT EVENTS  ·  SUPT Superintendent  VRGL Vergil  HRGK Huragok  ·  logs/huragok.log has them all
  21:01:22 SUPT INFO    [Slayer] restart requested: new map pack
  21:03:52 VRGL INFO    [Big Team Battle] Bo: slurs strike 1 of 3 → kick 1h (dry run)
```

## Who's who
Huragok is named for the Engineer in Halo 3: ODST. During the Battle of Mombasa a
Huragok called [Quick to Adjust](https://www.halopedia.org/Quick_to_Adjust)
reached the data centre beneath New Mombasa, repaired the city's badly damaged AI,
[the Superintendent](https://www.halopedia.org/Superintendent), and took in most
of its data, the subroutine [Vergil](https://www.halopedia.org/Vergil) included.
The ODSTs escorted it out of the city while it shielded them. Huragok can't leave
a broken machine alone; this one runs your servers, and the two minds it carried
are its parts:

| Tag | Part | In New Mombasa | Here |
| --- | --- | --- | --- |
| `HRGK` | **Huragok** (violet) | the Engineer, Quick to Adjust | the program itself, and what it builds and mends: new servers, packages, game content, the Workshop and SteamCMD, settings files, checks, updates, the window |
| `SUPT` | **the Superintendent** (amber) | the city's AI, running its signs, traffic and trains | keeps the servers running: starts, stops, restarts, countdowns, crash backoff and FAULTED, the daily restart, the keepalive, the watchdog, the event log |
| `VRGL` | **Vergil** (sky blue) | the subroutine Dr. Endesha wrote to watch over his daughter Sadie through the city's cameras | watches over the players: automod, in-game commands and who may use them, warns, mutes, kicks and bans, the bans and admins screen, the console relay for moderators, the record of every player |

The four-letter tags follow the Rookie's HUD, which labelled the Engineer "VRGL":
the window puts the tag of the part that spoke on every event line, and its keys
and screens are grouped and titled by part. The Superintendent spoke to New Mombasa through its
signs; here it speaks to players with the same public notices, from Bungie's
["KEEP IT CLEAN"](https://www.halopedia.org/Superintendent_(Viral_Campaign))
campaign (`PLEASE REMAIN CALM!`, `PARDON OUR DUST!`, `KEEP IT CLEAN!`), and its
face on the dashboard shows how the servers are doing.

The names change nothing you set: `settings.toml` keeps every section and key it
had (`[watchdog]`, `[automod]`, `[relay]`…).

## Set up
You need Python 3.11 or newer ([python.org](https://www.python.org/downloads/);
on Windows tick "Add python.exe to PATH", or run `winget install -e --id Python.Python.3.12`;
Debian/Ubuntu: `sudo apt install python3 python3-venv`). On Linux you also need
`wine`, which is how the server program runs there.

1. Download `huragok-<version>.zip` from the
   [latest release](https://github.com/LittleGroove/Project-Reclaimer-Huragok/releases/latest)
   and unzip it where you want your servers to live. The `huragok` folder it holds
   (rename it as you like) is the **root**:
   ```text
   C:\reclaimer\                  the root: Huragok
     start.cmd  start.sh          ← how your servers start from now on
     settings.toml  commands.toml  automod.txt  names.txt    yours, written on the first start
     SERVERS\                     one folder per server
       Big Team Battle\           what `dedicated init` makes: dedicated.toml,
       Slayer\                    project-reclaimer.exe, game\, data\, bans.json…
     PACKAGES\                    server packages, for new servers (N)
     engine\  docs\  records\  logs\
   ```
2. **Put each server's folder in `SERVERS\`.** A server folder is what the
   server's own `dedicated init` makes (see the
   [hosting guide](https://projectreclaimer.dev/host)): run
   `project-reclaimer.exe dedicated init "SERVERS\Slayer" --from <your MCC folder>`
   once per server, or move the folder you already have. Each folder's
   `dedicated.toml` usually holds one `[[server]]`; two folders must not use the
   same server name or port. Switch RCON on in each: set `[rcon] password`.
3. **Start it** (Windows: double-click `start.cmd`; Linux: `./start.sh`). The first
   time, it installs what it needs (into `engine\.venv`), writes four files and the
   `PACKAGES\` folder for you, and stops with the window held open to read them
   (Windows may first warn that the publisher couldn't be verified, because the
   file came from the internet: choose **Run**):

   | File | What it's for |
   | --- | --- |
   | `settings.toml` | How it runs, grouped by part: the Superintendent's restarts, warnings and watchdog; Vergil's automod and relay; Huragok's updates and setup; then how it starts the servers |
   | `commands.toml` | Who's who among your players, and which in-game commands each role can use |
   | `automod.txt` | automod's word list |
   | `names.txt` | the banned names |
   | `PACKAGES\` | seven starter packages for new servers (Team Slayer, Big Team Battle, MLG Hardcore, Infection, Scripted Modes, Campaign Co-op, ODST Firefight); see [docs/setup.md](docs/setup.md) |

4. **Make yourself owner:** put your player ID under `[owner]` in `commands.toml`
   (in game: Settings > Online services > Your player ID). Add your moderators
   under `[moderator]`, and regulars no vote should kick under `[member]`. Look
   over `settings.toml`; it explains every setting. Write a Windows path there in
   'single quotes' (`'D:\SteamLibrary\steamapps\common\Halo The Master Chief Collection'`)
   or with forward slashes: inside "double quotes" a backslash starts an escape. A
   setting it doesn't know (a misspelling, or a line under the wrong `[heading]`) is
   named when Huragok starts and by `check`, with what it was likely meant to be.
5. **Stop your servers the way you ran them before** (and remove whatever
   started them, such as a startup task), then **start Huragok again.**
   The window opens and starts every server.

Check everything without starting anything: `start.cmd check` (Linux: `./start.sh check`).

## The window
The dashboard redraws every second. Its first line says which version runs and
whether this is the supervising window; the second is the Superintendent's face,
one of its notices and what's going on, in plain words. Under the time and the
folder, each part says what it's running, after its tag (`SUPT`, `VRGL`, `HRGK`).
Then each server's state,
players and game, with a line of detail; the keys, grouped by the part that
answers them; and the latest events, each with the tag of the part that spoke.
Everything a server needs (crash restarts, countdowns, automod, commands) keeps
running while you use a key, and while the window itself is paused (text selected
with Edit > Mark): only the drawing waits. **Ctrl+C** in the window asks first,
like **Q**: it never ends the servers' care by itself.

The face shows the worst thing going on:

| Face | Notice | When |
| --- | --- | --- |
| `[>_<]` angry | METROPOLITAN EMERGENCY! | a server is FAULTED, or its folder has a mistake (CONFIG ERROR) |
| `[;_;]` sad | PARDON OUR DUST! | a server crashed and waits to start again |
| `[o_O]` confused | PLEASE REMAIN CALM! | a countdown, a stop or a restart is under way |
| `[o_o]` neutral | DETOUR! PROCEED WITH CAUTION! | a server is starting |
| `[^_^]` happy | KEEP IT CLEAN! | servers up, with players on |
| `[-_-]` bored | YOUR TAX DOLLARS AT WORK! | servers up, nobody on |
| `[-.-]` sleeping | | every server is stopped |
| `[o_O]` confused | PARDON OUR DUST! | no servers yet |
| `[   ]` off | | in a second window: the supervising window has stopped answering |

Every screen is laid out the same way: a title naming the part whose screen it is
(`SUPERINTENDENT › RESTART`, `VERGIL › BANS, ADMINS AND THE VPN BLOCK`), headings
that show where you are as a path (`LG Test Server › Game content › Playlists`),
numbered lists, and a block of `[key]` options with the way back beneath it. Log
lines and server events that arrive while a question waits are held, shown
together once they stop, and the question is asked again beneath them: a question
on the screen is always still waiting for you.

| Key | Does |
| --- | --- |
| | **The Superintendent** |
| **S / X / R** | start / stop / restart: pick servers by number (`1,2`) or `A` for all. With players on, it asks: **warn them first** (a countdown with chat warnings at 5, 3, 1 minute and 30 seconds) or **right now**. A restart asks for the reason players read. |
| **K** | cancel a countdown; players are told |
| **T** | toggle whether a server starts with Huragok (written to `settings.toml`) |
| **L** | open a separate window following a server's console output, or Huragok's log |
| **V** | a server's **status card**: everything the Superintendent keeps an eye on (running, joining, what it plays, its program version, its mods, health and the last day's restarts, safety, settings, schedule, problems), with keys to its settings, game content, a restart and its console. See [docs/managing.md](docs/managing.md) |
| | **Vergil** |
| **P** | who's on each server |
| **M** | a `[Server]` chat line to a server's players, or all |
| **A** | the console: type RCON commands (`players`, `mute Bravo 30m`, `map Guardian`…) through Huragok's own connection, so no moderator's spot is used; the server's events scroll in as they happen, and your name goes on what you do ("kicked by Alice"). See [docs/managing.md](docs/managing.md) |
| **B** | bans, admins and the VPN block: the lists, unban, ban someone offline, pardon a kick, add or remove an admin, let a player through the VPN block |
| | **Huragok** |
| **N** | a **new server**: a folder in `SERVERS/` made with the server program's `dedicated init`, from a package in `PACKAGES/` (its playlist, game types, maps, Workshop items), sharing the root's Halo 3 files and ban list (and one content folder, if you choose), checked, then started. See [docs/setup.md](docs/setup.md) |
| **G** | what a server plays: browse its maps, mod maps, variants and game types as numbered lists; build and edit playlists by picking from them; Workshop items and mods (and the Forge layouts mods ship); `dedicated check`; save it as a package; import MCC's own matchmaking map and game variants (MLG, Hardcore, Infection…) into its content |
| **I** | SteamCMD: find or install it, sign your Steam account in once (it must own MCC; anonymous can't fetch MCC's items), tell every server about it; and **Halo 3's files**, the campaign missions and ODST's, copied from MCC on this PC or downloaded with SteamCMD. See [docs/setup.md](docs/setup.md#steamcmd-i-in-the-window) |
| **D** | **settings, a step at a time**, with no file format to know. Huragok's own files come first: **S** `settings.toml` in groups (restarts, the watchdog, Vergil, updates, new servers…, and the moderators' console logins), each change checked the way Huragok reads the file as it starts, so one that would stop it starting is never written; **C** `commands.toml`: owners, moderators and members picked from the players seen lately, which role may use each in-game command, which are off; **A** `automod.txt`: Vergil's rules, the words then what each strike does (ready-made ladders, or your own a question at a time), shown as players will meet them, with a line of chat to try; **N** `names.txt`: banned names added and taken off, with a name to try. Then every server: its `dedicated.toml` in groups, changed while it runs (player cap, join password, voice, votes, welcome, what it plays, anti-cheat…; a value the server program would refuse, or one its version is too old for, is refused before anything is written; the screen says when each applies and the events show the server's answer), and its playlists, custom games, bans and mods switched on or off. See [docs/managing.md](docs/managing.md#settings-d) |
| **C** | a server's own `dedicated check`: game files, settings, playlists, maps, ports |
| **O** | **open**: the root folder first, then `settings.toml`, `commands.toml`, `automod.txt`, `names.txt`, or any server's folder, `dedicated.toml`, playlists and ban list in your editor, for what D doesn't change (the server applies most edits by itself; the result shows in the events) |
| **U** | four things: **Huragok** itself (servers keep running), the **server program** (a new Project Reclaimer release for the servers that don't install it themselves, each restarted after its game), **Workshop mods** (new versions, installed after the game) and **clean up** (what updates and mods leave behind). See [docs/managing.md](docs/managing.md#updates-u) |
| **W** | start at boot: Windows puts a small file in the Startup folder; Linux a systemd user unit |
| **H / Q** | help: who's who, every key by part, what the colours mean; close the window. **The servers keep running**, but unsupervised |

The colours follow an ODST's VISR: green is friendly (running, healthy, done), red
hostile (crashed, FAULTED, errors), yellow means look here (a warning), cyan is a
server starting and players joining, leaving, chatting and being moderated,
magenta is restarts and countdowns.

Add a server folder to `SERVERS/` while the window is open and it appears within
seconds; remove a folder once its server is stopped and it disappears.

**Opening the window while one is already open** shows the same dashboard and
hands its keys to the first window, the supervising one. When the first window
closes, the second takes over. From another terminal, without the window:

```text
start.cmd status                      what each server is doing
start.cmd stop [server…] [--now]      stop servers (none named: all, then Huragok itself)
start.cmd restart [server…] [--now] [--after-game] [--reason "…"]
                                      none named: every server that's running
start.cmd watch <server|huragok>      follow a server's console output, or Huragok's log
start.cmd say <server|all> <text>     a [Server] chat line
start.cmd rcon [-s server] <command>  one console command
start.cmd check                       check the settings, servers and commands
start.cmd new <name> [--package …]    a new server folder, checked and started (docs/setup.md)
start.cmd package [list|show|save]    the packages in PACKAGES/
start.cmd content | playlist | variant | workshop | dedicated …   what a server plays (docs/setup.md)
start.cmd steam [status|install|login <account>|path]   SteamCMD for Workshop items
start.cmd close                       close Huragok; the servers keep running (Q in the window)
start.cmd update                      update Huragok
start.cmd update --program [--now]    a new Project Reclaimer release for the servers (docs/managing.md)
start.cmd start --no-menu             run with no window: a plain log, with typed commands (a task at boot)
```
(Linux: `./start.sh …`.) A server is named by its name (in quotes when it has
spaces: `"Big Team Battle"`), its folder's file name (`big-team-battle`) or its
number on the dashboard (`2` or `#2`). They talk to the running window when there
is one, and work without it where they can: `stop` finds and stops the servers a
closed window left running (only those Huragok started), and names any it couldn't
stop. `restart` needs
Huragok running, since the Superintendent brings the server back; with no server
named it restarts the ones that are up and leaves stopped ones as they are. While
Huragok runs, `status` shows the Superintendent's face above the table; with
nothing running it says so and exits with 5, so a script can tell. Each command
waits as long as its work takes (an update up to 15 minutes); when the answer is
late it says so ("It may still be doing it") rather than calling it a failure.
`start.cmd --help` lists them grouped by part, like the window's keys:
the Superintendent's (`status`, `stop`, `restart`, `watch`), Vergil's (`say`,
`rcon`), Huragok's (`check`, `new`, `package`, `content`, `playlist`, `variant`,
`workshop`, `steam`, `dedicated`, `init`) and the program's (`start`, `close`,
`update`), with the exit codes: 0 done, 1 it didn't work, 2 a settings problem or
a name it doesn't know, 3 the first start wrote the files, 4 `start` found Huragok
already running, 5 `status` found nothing running. What they print is coloured in
a console and plain into a file or a pipe (`NO_COLOR` turns the colours off).

### How the Superintendent keeps servers up
Each server has a small state machine, looked at every second, so nothing waits
on anything else:

- **Starting:** a server is up once its RCON answers. One that never answers
  within `ready_seconds` is a failed start. Before a start, Huragok puts right what
  the server's folder lacks (a ban list, a content folder, Halo 3's files, a SteamCMD
  that's on this PC: see [docs/setup.md](docs/setup.md#a-server-folder-from-somewhere-else));
  while something is still missing the server shows **WAITING** and why, and that
  never counts as a failed start.
- **Crashes:** a server that dies is started again after `backoff_seconds` (5 s,
  then 10, 30, 60, 300). Dying within `good_run_seconds` of starting counts as a
  failed start; `max_failures` (5) of those in a row mean **FAULTED**: the
  window says so, `logs/faulted-<server>.txt` holds the reason and the last
  console lines. More than `restarts_per_hour` (6) automatic restarts in an hour
  mean FAULTED too: something is looping. FAULTED is a pause, not the end: the
  Superintendent tries once more after 30 minutes, then every hour
  (`faulted_retry_minutes`), until a run lasts; the server's line shows when.
  **S** starts it at once.
- **Countdowns:** chat warnings at `warn_seconds` before a planned stop or
  restart; skipped when nobody is on, and ended early once everyone has left.
- **Daily restart:** `daily_restart = "04:00"` under `[restarts]` for every server,
  or in a server's own `[[server]]` block for that one (`daily_restart = ""` there:
  no daily restart for it), once a day within half an hour of that time, after
  `min_uptime_seconds`. A time inside the hour the clocks skip in spring still
  happens; countdowns and backoff don't jump when the clock is changed.
- **Hangs:** `status.json` not rewritten and RCON silent for `[keepalive]
  hang_seconds` (3 minutes) means a restart right away.
- **The watchdog's cases:** each minute it looks for the cases below, and each has
  its own line in `[watchdog]`: `"actnow"` restarts right away (30 s of warning
  when players can be warned; the log says `METROPOLITAN EMERGENCY! Restarting it
  now: …`), `"act"` at the end of the game (right away when no game is on or
  nobody is playing), `"watch"` only logs it and shows it. `mode = "watch"` makes
  them all watch. The dashboard's line under the server says what it saw
  (`watchdog: degraded (slow in 3 of the last 5 minutes (longest frame 412 ms))`).

  | Case | What it is | Default | Its numbers |
  | --- | --- | --- | --- |
  | `dead` | every player dropped at once ("Host roster lost 112 of 112"), and nobody can join again | actnow | `drop_players`, `drop_share`, `drop_left` |
  | `no_joins` | players keep connecting and none gets in, while there's room: a player counts as in once they're still on 2 minutes after connecting | actnow | `no_joins_connections` in `no_joins_minutes` |
  | `stuck` | on the scoreboard after a game, players waiting (judged only while Huragok's console is connected) | actnow | `stuck_minutes` (3) |
  | `slow` | the server falling behind (its log's "under 54 frames a second") for a good share of several minutes | watch | `slow_share`, `slow_minutes`, `slow_window_minutes` |
  | `very_slow` | falling behind nearly all the time, minutes on end | watch | `very_slow_share`, `very_slow_minutes` |
  | `memory` | the server program using more than `memory_mb` | act | `memory_mb` (0: off) |

  Slow frames don't restart by default: a full server is slow anyway, and on a
  real 128-player server's log restarting for them would have restarted it
  mid-game hundreds of times. The same logs, replayed, had `dead` and `no_joins`
  restart it only while it really was dead. Only the server's own lines count:
  a chat line, a kick reason or a ban reason that quotes "Host roster lost…" is
  never taken for one.
- **Huragok itself:** `start.cmd` (or `start.sh`, or a task at boot) runs Huragok
  in a process of its own and starts it again if it stops unexpectedly: after 5,
  15, 30 and 60 seconds, then every 5 minutes for as long as it takes. The window
  and the log say so (`Huragok stopped unexpectedly (exit code 1); starting it
  again in 5 s.`), the servers are left as they are, and the new Huragok picks
  them up. A new version that won't start is taken back out and the one before
  it started instead.
- **A folder that can't be read:** a `dedicated.toml` with a mistake in it, or two
  folders with the same server name (or names that only differ in punctuation,
  like "LG Slayer" and "LG-Slayer", which would share their records and logs),
  leave out that one folder; every other server runs as usual, and the window and
  `check` say what to fix.
- **What survives closing the window:** whether each server is wanted running,
  its failure count, and which process is its (`records/`). Opening the window
  again adopts what's running without touching it. A stop that couldn't end a
  server's process says so on its line (`! couldn't stop it: …`) instead of
  calling it stopped.

What players read in chat when the Superintendent acts:

| When | They read |
| --- | --- |
| a countdown | `PLEASE REMAIN CALM! Server restarting in 5 minutes: daily restart.` (`Server shutting down in …` for a stop) |
| the moment it happens | `PARDON OUR DUST! Server is restarting now.` (`… shutting down now.`) |
| a restart planned for after the game | `PARDON OUR DUST! This server restarts after this game: <reason>` |
| a countdown cancelled (K) | `The server restart has been cancelled. Keep it clean!` |

The name it acts under is `name` at the top of `settings.toml` (`"Superintendent"`
when the first start writes it).

### How it runs your servers
`settings.toml` picks one `backend`; the first start chooses it from what's in your server folders:

| Backend | For | |
| --- | --- | --- |
| `process` | Windows and Linux | The Superintendent runs one `dedicated` per server from its folder: `project-reclaimer.exe` itself on Windows, through `wine` on Linux (or whatever `[process] command` says). The default. |
| `docker` | Linux, the official Docker setup | The Superintendent runs `docker compose` for each server's container. Chosen when `compose.yaml` is there. |
| `command` | systemd or your own scripts, anywhere | Your start, stop and restart commands. |
| `attach` | Anything else | Something else starts the servers; Huragok watches and moderates but can't restart them. |

### Starting it at boot
Press **W** in the window. Windows: a file in the Startup folder (`Huragok.cmd`)
opens the window at login and starts every autostart server. Linux: a systemd
user unit (`huragok.service`) runs `start.sh start --no-menu` (no window; open the
dashboard from another terminal with `./start.sh`). Either way,
`start.cmd start --no-menu` (or a Task Scheduler task / systemd unit running it)
is the way to run it with no window at all: a plain log, each line tagged with the
part that spoke, with typed commands. The Startup folder runs at **login**, not at
boot: after a restart nobody logs in to (Windows Update's, say), no server starts.
A Task Scheduler task that runs `start.cmd start --no-menu` "at startup" is the
way to cover that (W doesn't make one); open the window later from `start.cmd` to
watch it (it shows the same dashboard and hands its keys to the running Huragok).
The servers Huragok starts leave the task's job where Windows allows it, so ending
the task doesn't take them down.

## Who can do what
Vergil looks after the in-game commands. Players type them in game chat, like
`!mute Bravo 30m spam`. `commands.toml` lists, for each role, its players and its
commands; every role can also use the commands of the roles before it:

| Role | Who | Starts with |
| --- | --- | --- |
| `everyone` | every player | `!help` `!strikes` |
| `member` | the player IDs you list | no commands of their own yet: no vote can kick them |
| `moderator` | the player IDs you list | `!players` `!whois` `!warn` `!mute` `!unmute` `!kick` `!pardon` `!strike` `!health` |
| `admin` | the server's own admins (`admins` in `dedicated.toml`, or `!admin add` in game) | `!ban` `!unban` `!say` `!map` `!mode` `!load` `!nextmap` `!endgame` `!endround` `!shuffle` `!team` `!teamcount` `!maxping` `!startvote` `!cancelvote` `!passvote` `!servername` `!pardonplayer` `!unbanname` |
| `owner` | the player IDs you list (you) | `!admin` `!reload` |

Move a command to another role's list to change who can use it, or put it in
`[off]` to switch it off. Edits apply within seconds. Roles follow the player ID, never the
name: anyone can use any name. Nobody can act on someone of their own role or
above. **No vote can kick a member, moderator, admin or owner:** Vergil puts
everyone you list on each server's vote-kick immune list, so the server refuses a
vote to kick them as it's called, and Vergil tells the caller why. See
[docs/commands.md](docs/commands.md).

## automod
Vergil's word list is `automod.txt`: one line per rule, with the words and what
each strike leads to: kicks, then mutes, then a ban.

```text
# words              | kicks        | mutes          | ban | name
badword, otherword   | 1h           | 1, 1d, 7d      | 3   | slurs
rudeword             |              | 0, 1d, 3d, 7d  |     | rude
worstword            |              |                | 0   | threats
kickword
```
The first `slurs` strike keeps a player out for an hour, the second mutes them for
a day, the third for a week, and the fourth is a ban. `rude` mutes from the first
strike (a day, then 3 days, then a week each time) and never bans; `threats` bans
at once; `kickword` is a plain kick every time. A muted player stays in the game,
but nobody hears their voice or reads their chat; Vergil mutes them again as they
join, so a mute lasts through restarts and on every server. Digits for letters,
spacing, repeated and lookalike letters are caught for you, while a number stays a
number ("455 kills" is not a three-letter word). The player reads why, and the
server names Vergil as the one who acted:

> KEEP IT CLEAN! That isn't allowed here (rude, strike 1): muted for 1 day. Next time: muted for 3 days.

Moderators use `!strike <player> <rule>` for what's said in **voice**, which the
server can't show anyone. **Names** are kicked, never struck: a name with a listed
word in it, or one in `names.txt`, is kicked each time it joins until the player
changes it. `!strike <player> name` adds a player's name to `names.txt`. The first
start writes `dry_run = true` under `[automod]`, so automod starts in dry run,
logging what it would do. See [docs/automod.md](docs/automod.md).

## The folder
```text
reclaimer/                the root
  start.cmd, start.sh     open the window (and run the other commands)
  settings.toml           \
  commands.toml            | yours: written on the first start; an update brings them up to date
  automod.txt              | and never takes away what you wrote (see Updating)
  names.txt               /
  SERVERS/                one folder per server (yours); a folder starting with . or _ is ignored
  PACKAGES/               server packages: seven starters on the first start, and the ones you save
  game/  bans.json        written by the first new server you make: Halo 3's files once and one
                          ban list, shared by the servers made here (content/ too, if you choose)
  records/                its database (huragok.sqlite3: players seen, every action, Vergil's
                          strikes and mutes), the lock (huragok.pid), what it remembers about
                          each server, running-servers.json, requests from outside, the copies
                          an update replaced (previous-<version>/, previous-program/)
  logs/                   huragok.log, each server's console output, faulted-<server>.txt,
                          window.log (every screen as it was shown, asked and answered),
                          launcher.log (only when settings.toml couldn't be read)
  engine/                 the program. Don't edit it: updates replace it
  docs/
```
Those are the default names; a `settings.toml` that names other paths
(`database`, `log_file`) keeps them. Coming from 0.1, where this folder sat inside
the one server folder? That still works: `server_folder = ".."` in `settings.toml`
names it. Moving the server folder into `SERVERS/` is the layout to grow into.

## Updating
Huragok looks for a new version on GitHub when it starts and every few hours.
Unless you set it to install them itself (below), it waits for you: the events say
what's new, and the HURAGOK line on the dashboard keeps saying `Huragok <version> is
out (U)` until it's installed.

- **In the window:** press **U**.
- **From anywhere else** (or when it runs at boot with no window): `start.cmd update`
  (Linux: `./start.sh update`). It shows what's new and asks before installing.

Your servers keep running while it updates. Only the program is replaced
(`engine`, the start scripts, `docs`, `README.md`, `LICENSE`); your settings,
commands, word lists, servers and records stay. Then, as the new version starts, it
brings your files up to date itself: a new setting is added to `settings.toml` with
its comment, a setting that does nothing any more becomes a comment saying why, a new
in-game command is listed in `commands.toml`, a new starter package goes into
`PACKAGES/`. What you wrote stays as you wrote it, each file is kept as it was in
`records/previous-files/<version>/`, and `check` lists what the next start will bring
up to date. You never need to reinstall or start over. The install is all or nothing:
anything that can't be moved into place (an antivirus scanning the new files, a
console whose current folder is in `engine`) puts every file back as it was. The
replaced files are kept in `records/previous-<version>/` (a backup of the same
version already there is kept too, as `previous-<version>-<date>`), and U's
**clean up** offers the older ones. Huragok then starts on the new version once no
server is part way through a restart (at most 30 minutes later); if the new
version won't start, the one before it is put back and started, and that version
isn't installed again by itself.
To have it install new versions by itself, as soon as it finds them, set
`auto_update = true` under `[updates]` in `settings.toml` (off unless you turn it on;
0.4.1 and earlier called it `install_on_server_restart` and waited for a server to
restart, which a server that keeps failing to start never does: the next start renames
the line and keeps your value).

### Coming from Reclaimer Supervisor 0.2.1
0.2.1 can't update itself to this version (the program was renamed), so install
it by hand, over the old one, in the same folder:

1. Close the old window (**Q**, or `start.cmd close`): the servers keep running.
2. Delete the old `engine` folder (your settings, servers and records aren't in it),
   then unzip `huragok-<version>.zip` and copy what's in its `huragok` folder into
   the root, replacing `start.cmd`, `start.sh`, `docs`, `README.md` and `LICENSE`.
3. Start it as usual (`start.cmd`). It sets its Python packages up again, picks
   the running servers up, and names anything in `settings.toml` an older version
   wrote that means something else now (`check` lists them too).

Your `settings.toml` stays as it is, so it keeps the names it was written with:
`logs/supervisor.log`, `records/supervisor.sqlite3`, and for what players read
`name = "Supervisor"` and `[automod] name = "AutoMod"`. Change them there if you
want the new ones. Its `[process] disable_auto_update` does nothing any more (each
server's `auto_update` is the switch: delete the line), and its old `[updates]
repository` is read as the new one. Start at boot is now `Huragok.cmd` (Linux:
`huragok.service`); an old `Reclaimer Supervisor.cmd` (`reclaimer-supervisor.service`)
still counts as on, and **W** replaces it with the new one (or removes it). A Docker
setup mounts `/huragok` now, not `/supervisor`
([docs/docker-compose.example.yaml](docs/docker-compose.example.yaml)).

## Development
The code is in [engine/huragok](engine/huragok): the
per-server state machines (`manager.py`), the backends (`lifecycle.py`), the
window (`menu.py`, `tui.py`), who's who (`lore.py`: the parts' names, tags,
colours, notices and faces), and the parts: the in-game commands, automod, the
watchdog and the event log.
[docs/development.md](docs/development.md) explains how it's put together and how
to add a command or a part; [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) has the checklist.

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine -q > tests.txt 2>&1
```
The tests use a fake server (its RCON, and a stand-in `dedicated` program that the
process backend really starts and stops), so they need no game; the whole suite
takes about three minutes. CI runs them on Windows and Linux, Python 3.11 to 3.14,
and from the release zip itself. They stay off the network and away from the
ports real servers use, so they're safe to run on a PC that hosts servers;
[docs/development.md](docs/development.md#running-the-tests) says more.

## Planned
- **reports:** `!report` and vote-mute, for voice abuse (servers can't tell tools who's talking)
- **Discord:** a moderation queue with buttons, alerts and a feed of every action
- moderators' `!mute` lasting through restarts (Vergil's own mutes already do), `!restart`
  in game, a web panel, and a Windows download with Python included

## License
[GNU GPL v3](LICENSE). Not affiliated with the Project Reclaimer developers.
