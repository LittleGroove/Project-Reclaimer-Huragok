# Reclaimer Supervisor

One window that runs your [Project Reclaimer](https://projectreclaimer.dev)
servers. Every server is a folder under `SERVERS/`; the window shows them all,
starts them, keeps them up, and gives you single-key control. It:

- **runs your servers and keeps them up:** starts them when it opens, restarts
  one that crashed (backing off, and giving up rather than thrashing), warns
  players before any planned restart or stop, and restarts a struggling one
  after the game instead of mid-game;
- **removes players for words on your list (automod):** in chat and in names,
  with cascading timers (1 hour, then 1 day, then 1 week…) and a set number of
  chances before a ban;
- **gives in-game commands to the people you pick:** the server's console-only
  commands (mute, map, votes, say…) and its own, each for `everyone`,
  `moderator`, `admin` or `owner`, by player ID;
- **records everything:** every player seen (names, visits) and every warn, mute,
  kick and ban, with who did it and why;
- **sets servers up:** a new server from a package in one key (`dedicated init`,
  Halo 3's files shared from the root, the playlist and Workshop items of the
  package, `dedicated check`), playlists and Workshop items managed from the window,
  any server's files edited with the server's own reload result shown;
- **manages them while they run:** a console with the server's events and your name
  on what you do, settings changed live (the server applies most without a
  restart), bans and admins, and a **relay** so moderators elsewhere use the official
  RCON client through the supervisor, each with their own password and role;
- **keeps them current:** a new Project Reclaimer release is put in place of every
  copy and each server restarts after its game, not mid-game.

**Closing the window never stops the servers.** They keep running, unsupervised;
open the window again and it picks them straight back up. It works the same on
Windows and Linux (the owner runs the server natively through Wine; the
supervisor does too). It never patches the server or changes your `dedicated.toml`:
it uses what the server offers (its remote console, its files and its `dedicated`
command).

> **Status: early (0.2.0).** Tested against Project Reclaimer **v0.9.18**. Expect changes.

```text
 RECLAIMER SUPERVISOR 0.2.0   (supervising)
 Fri 16 Oct 2026  21:04:12      C:\reclaimer
 backend process  |  servers in SERVERS/  |  running: commands, automod (dry run), watchdog (watch)
 --------------------------------------------------------------------------------------------
  #  SERVER            PORT   AUTO STATUS                               PLAYERS            GAME
  1  Big Team Battle   49176  yes  RUNNING  up 5h 12m                   14/16 Alice, Bo... playing Valhalla Slayer
       daily restart 04:00 (in 6h 55m)  |  RCON 1 ms  |  status 3 s old
  2  Slayer            49177  yes  RESTART IN 2m 10s                    6/16 Cy, Dee, …    finished Guardian Slayer
       restart reason: the server is struggling   [K] cancels it
  3  Test              49178  no   stopped
 --------------------------------------------------------------------------------------------
  [S] Start      [X] Stop          [R] Restart         [K] Cancel countdown  [T] Toggle autostart
  [M] Message players  [A] Admin command  [P] Players online  [L] Watch a log   [H] Help
  [U] Update supervisor  [E] Edit a file  [O] Open folder  [W] Start at boot: ON  [Q] Quit
 --------------------------------------------------------------------------------------------
  Recent events (logs/supervisor.log)
  21:03:40 INFO    [Slayer] restart requested: the server is struggling
  21:03:40 WARNING [Slayer] Server restarting in 5 minutes: the server is struggling
```

## Set up
You need Python 3.11 or newer ([python.org](https://www.python.org/downloads/);
on Windows tick "Add python.exe to PATH", or run `winget install -e --id Python.Python.3.12`;
Debian/Ubuntu: `sudo apt install python3 python3-venv`). On Linux you also need
`wine`, which is how the server program runs there.

1. Download `reclaimer-supervisor-<version>.zip` from the
   [latest release](https://github.com/LittleGroove/Project-Reclaimer-Server-Supervisor/releases/latest)
   and unzip it where you want your servers to live. That folder is the **root**:
   ```text
   C:\reclaimer\                  the root: the supervisor
     start.cmd  start.sh          ← how your servers start from now on
     settings.toml  commands.toml  automod.txt     yours, written on the first start
     SERVERS\                     one folder per server
       Big Team Battle\           what `dedicated init` makes: dedicated.toml,
       Slayer\                    project-reclaimer.exe, game\, data\, bans.json…
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
   time, it installs what it needs, writes three files for you and stops
   (Windows may first warn that the publisher couldn't be verified, because the
   file came from the internet: choose **Run**):

   | File | What it's for |
   | --- | --- |
   | `settings.toml` | How it runs: how it starts the servers, warnings before restarts, the daily restart, automod and watchdog settings |
   | `commands.toml` | Who's who, and which in-game commands each role can use |
   | `automod.txt` | automod's word list |

4. **Make yourself owner:** put your player ID under `[owner]` in `commands.toml`
   (in game: Settings > Online services > Your player ID). Add your moderators
   under `[moderator]`. Look over `settings.toml`; it explains every setting.
5. **Stop your servers the way you ran them before** (and remove whatever
   started them, such as a startup task), then **start the supervisor again.**
   The window opens and starts every server.

Check everything without starting anything: `start.cmd check` (Linux: `./start.sh check`).

## The window
The dashboard redraws every second: each server's state, players and game, with
a line of detail, then the keys, then the latest events. Everything a server
needs (crash restarts, countdowns, automod, commands) keeps running while you
use a key.

Every screen is laid out the same way: a title, headings that show where you
are as a path (`LG Test Server › Game content › Playlists`), numbered lists, and
a block of `[key]` options with the way back beneath it. Log lines and server
events that arrive while a question waits are held, shown together once they
stop, and the question is asked again beneath them: a question on the screen is
always still waiting for you.

| Key | Does |
| --- | --- |
| **S / X / R** | start / stop / restart: pick servers by number (`1,2`) or `A` for all. With players on, it asks: **warn them first** (a countdown with chat warnings at 5, 3, 1 minute and 30 seconds) or **right now**. A restart asks for the reason players read. |
| **K** | cancel a countdown; players are told |
| **T** | toggle whether a server starts with the supervisor (written to `settings.toml`) |
| **M** | a `[Server]` chat line to a server's players, or all |
| **A** | the console: type RCON commands (`players`, `mute Bravo 30m`, `map Guardian`…) through the supervisor's own connection, so no moderator's spot is used; the server's events scroll in as they happen, and your name goes on what you do ("kicked by Alice"). See [docs/managing.md](docs/managing.md) |
| **P** | who's on each server |
| **D** | a server's settings changed while it runs: player cap, join password, voice, votes, welcome, playlist, hidden, anti-cheat… written to its `dedicated.toml`; the screen says when each applies (at once, next game, when empty) and the events show the server's answer |
| **B** | bans, admins and the VPN block: the lists, unban, ban someone offline, pardon a kick, add or remove an admin, let a player through the VPN block |
| **L** | open a separate window following a server's console output, or the supervisor's log |
| **U** | updates: the supervisor (servers keep running), or the **server program** (a new Project Reclaimer release put in place of every copy, each running server restarted after its game) |
| **N** | a **new server**: a folder in `SERVERS/` made with the server program's `dedicated init`, from a package in `PACKAGES/` (its playlist, game types, maps, Workshop items), sharing the root's Halo 3 files, content and ban list, checked, then started. See [docs/setup.md](docs/setup.md) |
| **G** | what a server plays: browse its maps, mod maps, variants and game types as numbered lists; build and edit playlists by picking from them; Workshop items and mods (and the Forge layouts mods ship); `dedicated check`; save it as a package; import MCC's own matchmaking map and game variants (MLG, Hardcore, Infection…) into its content |
| **I** | SteamCMD for Workshop items: find or install it, sign your Steam account in once (it must own MCC; anonymous can't fetch MCC's items), tell every server about it |
| **C** | a server's own `dedicated check`: game files, settings, playlists, maps, ports |
| **E / O** | edit `settings.toml`, `commands.toml`, `automod.txt`, or any server's `dedicated.toml`, playlists and ban list (the server applies most edits by itself; the result shows in the events); open the root folder |
| **W** | start at boot: Windows puts a small file in the Startup folder; Linux a systemd user unit |
| **H / Q** | help; close the window. **The servers keep running**, but unsupervised |

Add a server folder to `SERVERS/` while the window is open and it appears within
seconds; remove a folder once its server is stopped and it disappears.

**Opening the window while one is already open** shows the same dashboard and
hands its keys to the first window. When the first window closes, the second
takes over. From another terminal, without the window:

```text
start.cmd status                      what each server is doing
start.cmd stop [server…] [--now]      stop servers (none named: all, then the supervisor itself)
start.cmd close                       close the supervisor; the servers keep running (Q in the window)
start.cmd restart <server…> [--now] [--after-game] [--reason "…"]
start.cmd say <server|all> <text>     a [Server] chat line
start.cmd rcon [-s server] <command>  one console command
start.cmd watch <server|supervisor>   follow a log
start.cmd check | update              check the settings; update the supervisor
start.cmd update --program [--now]    a new Project Reclaimer release for the servers (docs/managing.md)
start.cmd new <name> [--package …]    a new server folder, checked and started (docs/setup.md)
start.cmd package [list|show|save]    the packages in PACKAGES/
start.cmd content | playlist | workshop | dedicated …   what a server plays (docs/setup.md)
start.cmd steam [status|install|login <account>|path]   SteamCMD for Workshop items
```
(Linux: `./start.sh …`.) They talk to the running window when there is one, and
work without it where they can: `stop` finds and stops the servers a closed
window left running. `start.cmd --help` lists them grouped like the window's
keys; what they print is coloured in a console and plain into a file or a pipe
(`NO_COLOR` turns the colours off).

### How it keeps servers up
Each server has a small state machine, looked at every second, so nothing waits
on anything else:

- **Starting:** a server is up once its RCON answers. One that never answers
  within `ready_seconds` is a failed start.
- **Crashes:** a server that dies is started again after `backoff_seconds` (5 s,
  then 10, 30, 60, 300). Dying within `good_run_seconds` of starting counts as a
  failed start; `max_failures` (5) of those in a row mean **FAULTED**: the
  window says so, `logs/faulted-<server>.txt` holds the reason and the last
  console lines, and **S** starts it again. More than `restarts_per_hour` (6)
  automatic restarts in an hour mean FAULTED too: something is looping.
- **Countdowns:** chat warnings at `warn_seconds` before a planned stop or
  restart; skipped when nobody is on, and ended early once everyone has left.
- **Daily restart:** `daily_restart = "04:00"`, for all servers or one, once a day
  within half an hour of that time, after `min_uptime_seconds`.
- **Hangs:** `status.json` not rewritten and RCON silent for `[keepalive]
  hang_seconds` means a restart right away. With `[watchdog] mode = "act"`, the
  watchdog restarts a *struggling* server (slow frames, stuck after a game) after
  the game, and a dead one (every player dropped at once after a game, nobody can
  join again) right away.
- **A folder that can't be read:** a `dedicated.toml` with a mistake in it, or two
  folders with the same server name, leave out that one folder; every other server
  runs as usual, and the window and `check` say what to fix.
- **What survives closing the window:** whether each server is wanted running,
  its failure count, and which process is its (`records/`). Opening the window
  again adopts what's running without touching it.

### How it runs your servers
`settings.toml` picks one `backend`; the first start chooses it from what's in your server folders:

| Backend | For | |
| --- | --- | --- |
| `process` | Windows and Linux | The supervisor runs one `dedicated` per server from its folder: `project-reclaimer.exe` itself on Windows, through `wine` on Linux (or whatever `[process] command` says). The default. |
| `docker` | Linux, the official Docker setup | The supervisor runs `docker compose` for each server's container. Chosen when `compose.yaml` is there. |
| `command` | systemd or your own scripts, anywhere | Your start, stop and restart commands. |
| `attach` | Anything else | Something else starts the servers; the supervisor watches and moderates but can't restart them. |

### Starting it at boot
Press **W** in the window. Windows: a file in the Startup folder opens the window
at login and starts every autostart server. Linux: a systemd user unit runs
`start.sh start --no-menu` (no window; open the dashboard from another terminal
with `./start.sh`). Either way, `start.cmd start --no-menu` (or a Task Scheduler
task / systemd unit running it) is the way to run it with no window at all: a
plain log, with typed commands.

## Who can do what
Players type commands in game chat, like `!mute Bravo 30m spam`. `commands.toml`
lists, for each role, its players and its commands; every role can also use the
commands of the roles before it:

| Role | Who | Starts with |
| --- | --- | --- |
| `everyone` | every player | `!help` `!strikes` |
| `moderator` | the player IDs you list | `!players` `!whois` `!warn` `!mute` `!unmute` `!kick` `!pardon` `!strike` `!health` |
| `admin` | the server's own admins (`admins` in `dedicated.toml`, or `!admin add` in game) | `!ban` `!unban` `!say` `!map` `!mode` `!load` `!nextmap` `!endgame` `!endround` `!shuffle` `!team` `!teamcount` `!maxping` `!startvote` `!cancelvote` `!passvote` `!servername` `!pardonplayer` |
| `owner` | the player IDs you list (you) | `!admin` `!reload` |

Move a command to another role's list to change who can use it, or put it in
`[off]` to switch it off. Edits apply within seconds. Roles follow the player ID, never the
name: anyone can use any name. Nobody can act on someone of their own role or
above. See [docs/commands.md](docs/commands.md).

## automod
One line in `automod.txt` per rule: the words, how long each offence keeps a
player out, and how many chances before a ban.

```text
# words              | timers       | chances | name
badword, otherword   | 1h, 1d, 7d   | 3       | slurs
kickword
```
The first `slurs` offence keeps a player out for an hour, the second a day, the
third a week, and the fourth is a ban; `kickword` is a plain kick every time.
Digits for letters, spacing, repeated and lookalike letters are caught for you.
Moderators use `!strike` for what's said in **voice**, which the server can't
show anyone. automod starts in dry run, logging what it would do. See [docs/automod.md](docs/automod.md).

## The folder
```text
reclaimer/                the root
  start.cmd, start.sh     open the window (and run the other commands)
  settings.toml           \
  commands.toml            > yours: written on the first start, never replaced
  automod.txt             /
  SERVERS/                one folder per server (yours); a folder starting with . or _ is ignored
  PACKAGES/               server packages: six starters on the first start, and the ones you save
  game/  content/  bans.json   written by the first new server you make: Halo 3's files once,
                          one content folder and one ban list shared by the servers made here
  records/                its database (players seen, every action), what it remembers about
                          each server, running-servers.json, requests from outside
  logs/                   supervisor.log, each server's console output, faulted-<server>.txt,
                          window.log (every screen as it was shown, asked and answered)
  engine/                 the program. Don't edit it: updates replace it
  docs/
```
Coming from 0.1, where this folder sat inside the one server folder? That still
works: `server_folder = ".."` in `settings.toml` names it. Moving the server
folder into `SERVERS/` is the layout to grow into.

## Updating
The supervisor looks for a new version on GitHub when it starts and every few
hours. It never installs one by itself: the window says what's new and waits
for you.

- **In the window:** press **U**.
- **From anywhere else** (or when it runs at boot with no window): `start.cmd update`
  (Linux: `./start.sh update`). It shows what's new and asks before installing.

Your servers keep running while it updates. Only the program is replaced
(`engine`, the start scripts, `docs`); your settings, commands, word list,
servers and records stay, and the replaced files are kept in `records/previous-<version>`.
To have it install new versions by itself, the next time a server restarts, set
`install_on_server_restart = true` under `[updates]` in `settings.toml`.

## Development
The code is in [engine/reclaimer_supervisor](engine/reclaimer_supervisor): the
per-server state machines (`manager.py`), the backends (`lifecycle.py`), the
window (`menu.py`, `tui.py`), and the parts: the in-game commands, automod, the
watchdog and the event log.
[docs/development.md](docs/development.md) explains how it's put together and how
to add a command or a part; [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) has the checklist.

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine
```
The tests use a fake server (its RCON, and a stand-in `dedicated` program that the
process backend really starts and stops), so they need no game. CI runs them on
Windows and Linux.

## Planned
- **reports:** `!report` and vote-mute, for voice abuse (servers can't tell tools who's talking)
- **Discord:** a moderation queue with buttons, alerts and a feed of every action
- **server updates:** install new Project Reclaimer releases at a quiet moment instead of mid-game
- mutes that survive restarts, `!restart` in game, a web panel, and a Windows download with
  Python included

## License
[GNU GPL v3](LICENSE). Not affiliated with the Project Reclaimer developers.
