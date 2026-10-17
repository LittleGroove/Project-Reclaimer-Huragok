# Changelog

## 0.2.0
**The window, and one folder per server.** The supervisor is now a live dashboard
with single-key control, and the layout follows the servers: the supervisor at
the root, each server in `SERVERS/<name>/`.

- **The window:** every server's state, players and game, redrawn each second,
  with keys: S/X/R start, stop, restart (with a countdown for the players, or
  right now), K cancel, T toggle autostart, M message players, A an RCON
  console through the supervisor's own connection, P players, L a window
  following a log, U update the supervisor, E/O edit files and open the folder,
  W start at boot, H help, Q quit (servers keep running). Opening a second
  window shows the same dashboard and hands its keys to the first; it takes
  over when the first closes. `start --no-menu` is the old plain log, with
  typed commands.
- **One folder per server** under `SERVERS/`, each what `dedicated init`
  makes. Folders added while running are picked up within seconds. The 0.1
  drop-in layout still works (`server_folder = ".."`).
- **Per-server state machines:** idle, launch, booting (up once RCON answers),
  running, countdown, stopping, backoff, faulted, external. Crashes back off
  (`backoff_seconds`) and `max_failures` in a row or more than
  `restarts_per_hour` automatic restarts mean FAULTED, with
  `logs/faulted-<server>.txt`. Chat warnings before planned restarts follow
  `[restarts] warn_seconds` (5, 3, 1 minute, 30 s), skipped when nobody is on
  and ended early when everyone leaves. A `daily_restart` time, for all servers
  or one. What each server is wanted to do survives closing the window
  (`records/servers/`).
- **The `process` backend runs on Linux too** (the `windows` name still works):
  `project-reclaimer.exe` directly on Windows, through `wine` on Linux, or any
  `[process] command`; a clean stop is Ctrl+Break or SIGINT. Servers run in
  their own process group or session, so closing the supervisor never takes
  them down.
- **From outside:** `status`, `stop [server…] [--now]`, `close` (the supervisor
  closes, the servers keep running), `restart`, `say`, `watch`, and `rcon`
  through the running supervisor's connection, all via `records/requests/`;
  `records/live.json` is what the dashboard shows.
- Two supervisors started at the same instant from one folder can no longer
  both run (the lock is taken atomically).
- Settings: `servers_folder`, `[restarts]`, `[process]`, `[[server]] autostart`
  and `daily_restart`. Events: `server.ready`, `server.adopted`,
  `server.countdown`, `server.countdown_cancelled`, `server.faulted`.
- **Setting servers up** (docs/setup.md): **N** makes a new server from a
  package: `dedicated init`, Halo 3's files, one content folder and one ban list
  shared from the root (`[setup] share`; hard links, so no space is used), the
  package's playlists, content and Workshop items (`dedicated mods`), a made-up
  RCON password, `dedicated check`, then it starts. **G** is what a server plays:
  `dedicated content`, playlists (list, show, new, use, edit), Workshop items
  (add, remove, install), check, save as a package. **C** runs `dedicated check`.
  **E** edits any server's files too, and the server's reload result shows in
  the events. `PACKAGES/` with six starter packages (Team Slayer, Big Team
  Battle, MLG Hardcore, Infection, Scripted Modes, Campaign Co-op). Commands:
  `new`, `package`, `content`, `playlist`, `workshop`, `dedicated`. One
  `project-reclaimer.exe` at the root serves every server. Folders in `SERVERS/`
  starting with `.` or `_` are ignored. The dashboard knows what each server is
  set to play (`plays`).
- **Managing a running server** (docs/managing.md): **A** the console shows the
  server's events as they happen and asks your name once, which the server
  records on what you do. **D** changes a server's settings live: the catalogue
  with current values, written to `dedicated.toml` in place, each with when it
  applies. **B** bans, admins and the VPN block. **U** also updates the **server
  program**: the newest Project Reclaimer release, checked against its
  `SHA256SUMS.txt`, put in place of every copy (the old ones kept in
  `records/previous-program/`), each running server restarted after its game or
  now; `[server_updates]` (check, install = ask / after_game / now);
  `update --program [--now]`; the dashboard header shows the servers' version
  and when a newer one is out. **The console relay** (`[relay]`): moderators
  elsewhere connect the official RCON client to the supervisor at the server's
  game port + 1000, each with their own password and role; only the commands
  their role allows pass (commands.toml's lists), the server reads who acted,
  events are relayed (players' addresses to admins only), five wrong passwords
  lock an address out of the relay, not the server; Source RCON tools use the
  same passwords at game port + 2000. **G** also imports MCC's own matchmaking
  map and game variants (or any folder's) into a server's content
  (`content --import`). The dashboard shows the next game (a `nextmap`
  override) and when a playlist vote is open. Events: `server.program_updated`.
- **Everything to choose from is listed first.** **G** reads the server's whole
  catalogue (maps, mod maps, variants, game types, variants, mods with their Forge
  layouts, Workshop items) and shows it as numbered, pageable lists; playlists are
  **built and edited by picking** maps and game types from those lists (weights,
  leave-outs, vote seconds, rename), checked with the server program and offered
  to the server; packages show their games before you pick one; **D** picks a
  playlist from the files. A mod's shipped Forge layouts can be installed into
  `content/Maps`. **I** / `steam`: SteamCMD for Workshop items: found or
  installed (root or standard folder), the account signed in once in its own
  window, `[workshop]` written into every server, optional PATH; the Workshop
  screen shows its status. Listings with a quoted display name (`"file"
  "Name" · Base`) are read right.
- **Nothing hangs the window any more.** The server program's slow commands
  (`check`, `mods`, `playlist`) show their output as it comes and **Esc
  cancels** them. Adding Workshop items to a *running* server no longer starts a
  second SteamCMD beside the server's own fetch: the screen follows the server's
  console instead, and shows each item's verdict (fetched, or its map skipped
  for missing `.mapinfo`). The server's Workshop lines are events
  (`server.workshop`, `server.map_skipped`) and appear in the dashboard's events.
  Messages on several lines keep their time and level in the dashboard (the
  rest indented under the first); the server's reload replies are logged as one
  line. The dashboard's keys are grouped (Servers / Live / Set up / Program) and
  a server in the lobby reads `lobby: Guardian Slayer`. A playlist the server
  program can't play as built names the games it can't play and offers to drop
  them; a cancelled pick says so. **`logs/window.log`** is a plain copy of every
  screen: what was shown, asked and answered (passwords masked), with the key and
  the time, for finding out what happened afterwards; the supervisor's log notes
  each screen opened and closed.
- **Every screen is laid out the same way:** a title with one line on what the
  screen does, section headings that show where you are as a path (`LG Test
  Server › Game content › Playlists`), numbered lists in aligned columns with the
  description in gray, and a menu block of `[key]` options in two columns when
  they fit (one when each has a note), with the way back in gray beneath it.
  **Log lines and server events no longer land inside a question.** While a
  question waits they are held, shown together once they stop coming (a quiet
  second, or every few seconds while they keep coming), and the menu block and
  the question are drawn again beneath them, with whatever was typed so far.
  The console (A) shows chat and events the same way, between commands.
- **The commands print in the same look:** `status` draws the dashboard's own
  table; `check` is sectioned (the supervisor, each server, parts, commands) with
  a green or yellow verdict; `package list`, `playlist`, `workshop`, `steam` and
  the first start are numbered, aligned lists with the description in gray;
  `--help` groups the commands as the dashboard groups its keys. Colours only on
  a real console (`NO_COLOR` turns them off); into a file or a pipe the same lines
  come out plain. `logs/supervisor.log` names the part that spoke without the
  package prefix (`manager:`), and the plain log (`--no-menu`) shows warnings in
  yellow and errors in red. `watch` colours what it follows the way the dashboard
  colours its events: time and level in gray, problems red, warnings yellow, a
  server coming up or a reload green, stops and restarts magenta, players and the
  Workshop cyan; the server program's own output (`dedicated`, `playlist show`,
  `workshop install`) gets the same treatment. A server that's off answers `rcon`
  and `say` with "isn't running" instead of a Windows error number. The console
  relay reports a port beyond 65535 as a problem instead of failing. The
  dashboard's Recent events and the lines held during a screen use the same
  colours as `watch` (the window's own `window: G opened` lines stay in the log); the console log's session marker and the
  `logs/faulted-<server>.txt` file are laid out like the rest (why, what to do,
  where its folder and console are, then the last console lines). A second
  window shows the log's lines the way the first shows its events (time, level,
  message), and the Workshop screen's follow of a running server's console
  colours lines as `watch` does. The settings screen says "Takes effect: …" so
  every phrase reads as a sentence; the Workshop screen names S (not I) for the
  SteamCMD setup; N says why a port or player count that isn't a number ends it;
  the Linux launcher is named in the "stop them with" line; the plain log's typed
  `status` no longer ends in a dangling separator. From reading every message the
  main flows don't reach: a package's broken playlist is named once, not twice; a
  failed `dedicated` command says "failed." beneath its red Error line instead of
  repeating it; a stop command that prints nothing no longer leaves a dangling
  colon; the console relay says "the port is in use" instead of two nested error
  numbers; the watchdog's health line says "phase unknown" rather than "?"; and
  `status` with no servers gives the layout help the same "Server folders"
  heading the first start does. From re-reading every screen and command on the
  final build: a cancelled or timed-out line prompt shows "(cancelled)" one
  space after the question, as a key prompt does; wrapped listings break a long
  path after a `\` or `/` instead of mid-name (`status`, `check`, the file
  lists); the "Several servers" refusal reads the same from every command; the
  first start's backend note lost its nested parentheses; and `new`'s final
  "Fix the problems" line is no longer indented on stderr.
- **Keeping servers up, from the live server's crash log** (85,000 lines, 27
  starts): the watchdog's "every player dropped at once" restart now needs nobody
  left (`drop_left = 0`). All seven post-game deaths left nobody; the one
  mid-game drop that left 3 of 73 on recovered by itself, so a drop like that is
  only logged now. A server folder whose `dedicated.toml` can't be read, or
  that repeats another server's name, is left out alone (named in the window, the
  log and `check`) instead of keeping the supervisor from starting at all; the
  folder named after the server wins over a copy, and a running server whose file
  breaks is kept as it was. A `[[server]]` block or `servers` entry in
  settings.toml naming a server that isn't there is a problem shown, not a
  reason to stop. On Windows the window's QuickEdit is switched off: a click in
  the classic console no longer pauses the supervisor until Enter.

## 0.1.1
- The README says what to do when Windows warns that a downloaded `start.cmd`
  comes from an unknown publisher.

## 0.1.0
First version, tested against Project Reclaimer v0.9.18.

- **One folder you put in your server folder.** `start.cmd` (Windows) or
  `start.sh` (Linux) is the one way to start it: the first time it installs
  what it needs into `engine/.venv`, writes `settings.toml`, `commands.toml` and
  `automod.txt`, and picks how to run the servers from what's in the server
  folder. The program lives in `engine/`; updating means replacing it.
- **Keeping servers up:** backends `windows` (the supervisor runs each server),
  `docker`, `command` and `attach`; restarts now, after the game or when empty;
  hang detection. Closing the supervisor leaves the servers running;
  `stop` stops both. Only one supervisor runs from a folder at a time.
- **In-game commands with permissions:** `commands.toml` lists, for each role
  (`everyone`, `moderator`, `admin`, `owner`), its players and its commands, and
  `[off]` for commands nobody can use. Edits apply while running. Admins are the
  server's own, read live over RCON so the lists never drift apart. The
  commands: moderation (`!warn` `!mute` `!kick` `!ban`…), looking people up
  (`!players` `!whois`), and running the server (`!map` `!say` votes, teams…).
- **automod:** one word list, a rule per line: words, cascading timers and
  chances before a ban; `!strike`, `!strikes`, `!pardonplayer`.
- **watchdog** (health, watch mode) and **eventlog**. The watchdog restarts at
  once when the server drops most of its players in one go (`Host roster lost …`
  in `server.log`), without a countdown when nobody is left.
- Core: one RCON connection per server on the host's reserved spot, live server
  state, `server.log` and `status.json` watching, event bus, SQLite records of
  players and actions. Each part runs on its own queue with time limits and is
  switched off alone if it keeps failing.
- **Updates from GitHub releases:** it looks for a new version at start and
  every few hours, says what's new in its window, and installs it only when
  the host types `update` there or runs `start.cmd update` (or, with
  `[updates] install_on_server_restart`, when a server restarts). Servers keep
  running while it updates; only the program files are replaced.
- Commands: `start`, `check`, `stop`, `update`, `rcon`, `init`. Typed in the
  supervisor's window: `update`, `check`, `status`, `stop`, `help`.
