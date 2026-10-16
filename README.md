# Reclaimer Supervisor

One program for running a [Project Reclaimer](https://projectreclaimer.dev)
server. Put its folder in your server folder and start your server with it from
now on. It:

- **starts your servers and keeps them up:** it restarts a server that crashed or
  hung, and can restart a struggling one after the game instead of mid-game;
- **removes players for words on your list (automod):** in chat and in names,
  with cascading timers (1 hour, then 1 day, then 1 week…) and a set number of
  chances before a ban;
- **gives in-game commands to the people you pick:** the server's console-only
  commands (mute, map, votes, say…) and its own, each for `everyone`,
  `moderator`, `admin` or `owner`, by player ID;
- **records everything:** every player seen (names, visits) and every warn, mute,
  kick and ban, with who did it and why.

It works the same on Windows and Linux. It never patches the server or changes
your `dedicated.toml`: it uses what the server offers (its remote console, its
files and its `dedicated` command).

> **Status: early (0.1.0).** Tested against Project Reclaimer **v0.9.18**. Expect changes.

## Set up
You need Python 3.11 or newer ([python.org](https://www.python.org/downloads/);
on Windows tick "Add python.exe to PATH", or run `winget install -e --id Python.Python.3.12`),
and RCON switched on in your server: set `[rcon] password` in `dedicated.toml`.

1. Download `reclaimer-supervisor-<version>.zip` from the
   [latest release](https://github.com/LittleGroove/Project-Reclaimer-Server-Supervisor/releases/latest)
   and unzip it **inside your server folder**, next to `dedicated.toml`:
   ```text
   C:\reclaimer\                     your server folder
     dedicated.toml
     project-reclaimer.exe
     data\  game\  content\ …
     reclaimer-supervisor\           this folder
       start.cmd                     ← how your server starts from now on
   ```
2. **Start it** (Windows: double-click `start.cmd`; Linux: `./start.sh`). The first
   time, it installs what it needs, writes three files for you and stops:
   (Windows may first warn that the publisher couldn't be verified, because the
   file came from the internet: choose **Run**. To skip that, right-click the
   zip, Properties, tick **Unblock**, then unzip it.)

   | File | What it's for |
   | --- | --- |
   | `settings.toml` | How it runs: your server folder, how it starts the servers, automod and watchdog settings |
   | `commands.toml` | Who's who, and which in-game commands each role can use |
   | `automod.txt` | automod's word list |

3. **Make yourself owner:** put your player ID under `[owner]` in `commands.toml`
   (in game: Settings > Online services > Your player ID). Add your moderators
   under `[moderator]`. Look over `settings.toml`; it explains every setting.
4. **Stop your server the way you ran it before** (and remove its startup task, if
   it has one), then **start the supervisor again.** It starts the server.

Check everything without starting anything: `start.cmd check` (Linux: `./start.sh check`).

### Starting it at boot
- **Windows:** a Task Scheduler task that runs `start.cmd` with the argument `start`,
  "at startup", set to restart on failure. It replaces whatever started your server before.
- **Linux:** a systemd service whose `ExecStart` is the full path to `start.sh`.

### How it runs your servers
`settings.toml` picks one `backend`; the first start chooses it from what's in your server folder:

| Backend | For | |
| --- | --- | --- |
| `windows` | Windows | The supervisor runs each server. Chosen when `project-reclaimer.exe` is there. |
| `docker` | Linux, the official Docker setup | The supervisor runs `docker compose` for each server's container. Chosen when `compose.yaml` is there. |
| `command` | systemd or your own scripts, anywhere | Your start, stop and restart commands. |
| `attach` | Anything else | Something else starts the servers; the supervisor watches and moderates but can't restart them. |

Closing the supervisor leaves the servers running, so players aren't disturbed;
starting it again picks them up. To stop the servers too: `start.cmd stop`
(Linux: `./start.sh stop`).

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
reclaimer-supervisor/
  start.cmd, start.sh     start it (and run its other commands: check, stop, update, rcon)
  settings.toml           \
  commands.toml            > yours: written on the first start, never replaced
  automod.txt             /
  records/                its database: players seen, every action
  logs/                   supervisor.log, and each server's console with backend windows
  engine/                 the program. Don't edit it: updates replace it
  docs/
```

## Updating
The supervisor looks for a new version on GitHub when it starts and every few
hours. It never installs one by itself: its window says what's new and waits
for you.

- **In its window:** type `update` and press Enter.
- **From anywhere else** (or when it runs at boot with no window): `start.cmd update`
  (Linux: `./start.sh update`). It shows what's new and asks before installing.

Your servers keep running while it updates. Only the program is replaced
(`engine`, the start scripts, `docs`); your settings, commands, word list and
records stay, and the replaced files are kept in `records/previous-<version>`.
To have it install new versions by itself, the next time a server restarts, set
`install_on_server_restart = true` under `[updates]` in `settings.toml`.

## Development
The code is in [engine/reclaimer_supervisor](engine/reclaimer_supervisor), one
program in parts: the in-game commands, automod, the watchdog and the event log.
[docs/development.md](docs/development.md) explains how to add a command or a
part; [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) has the checklist.

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine
```
The tests use a fake server, so they need no game. CI runs them on Windows and Linux.

## Planned
- **reports:** `!report` and vote-mute, for voice abuse (servers can't tell tools who's talking)
- **Discord:** a moderation queue with buttons, alerts and a feed of every action
- **server updates:** install new Project Reclaimer releases at a quiet moment instead of mid-game
- mutes that survive restarts, `!restart` in game, a web panel, and a Windows download with Python included

## License
[GNU GPL v3](LICENSE). Not affiliated with the Project Reclaimer developers.
