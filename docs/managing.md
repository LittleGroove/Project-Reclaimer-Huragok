# Managing a running server

Setting a server up is in [setup.md](setup.md). This is what the window and the
commands do with a server that's running: its status card, its console, the settings
(Huragok's own changed a step at a time, each server's own shown with its warnings),
bans and admins, updates of the server program and of its Workshop mods, cleaning up,
the stats relay, and a console for moderators who aren't at this PC. The Discord bot
is **J**, with a guide of its own: [discord.md](discord.md).

## A server's status card: V
**V** picks a server and shows, on one screen, everything the Superintendent keeps an
eye on:

| | |
| --- | --- |
| Running | its state and uptime, RCON's reply time, how fresh its status file is, its process and the memory it and its game use; a job waiting for its next start |
| Joining | the address players join at, listed or hidden, join password or open, players and the limit, ping limit, the welcome lines |
| What it plays | what its own file names: a playlist (name, games, vote seconds), playlists by lobby size, or a game; the game under way |
| Program | its Project Reclaimer version, whether it updates itself (auto_update) or is pinned to its version by its own file, the newest release, where its update stands |
| Mods | its Workshop items: installed version, Steam's newest, an update downloading or waiting for a restart; whether Huragok restarts it for one, and how often the server looks; the SteamCMD account; what they take on disk; mod sharing; character mods |
| Health | the watchdog's state and why, the starts, restarts and crashes of the last 24 hours with their reasons, failed starts, FAULTED and why |
| Safety | anti-cheat, the VPN block and its lists, admins, bans (and a ban list it reads from outside its folder, shared with any server reading it: `start.cmd repair` gives it its own) |
| Settings | voice channels, text chat, votes, friendly fire, team changing, live reload |
| Scheduled | autostart, the daily restart, a planned restart, Huragok at boot |
| Problems and notes | its configuration errors, a failed settings reload, an expired SteamCMD sign-in, a mod update waiting, and its warnings, the same as G's |

From the card: **D** its settings, **G** what it plays, **R** restart it, **L** watch
its console, each with the server already picked; **U** the updates screen and **C**
clean up, which are for every server, not only this one; **F** looks again. A second
window shows the card too, from the files and the supervising window's live line.

The dashboard's line under each server carries the important part: the watchdog's
reason (`watchdog: degraded (slow in 3 of the last 5 minutes (longest frame 412
ms))`, `watchdog: critical (4 players connected in 5 minutes and none got in)`),
`2 restart(s)/crash(es) in 24 h`, `mod update waiting`, and in red (starting `!`)
what needs you: a settings reload that failed, a SteamCMD sign-in that expired, a
stop that couldn't end the server's process (`! couldn't stop it: …`), a mod
update its map holds up (`! mod update waiting: …: its map is in use`) or that
restarts didn't put in, until it's fixed.

## The console: A
**A** picks a server and opens its console through Huragok's own RCON
connection (the spot the server keeps for its own machine, so moderators' 4 spots
stay free). The first time, it asks your name: the server records it and players
read "kicked by Alice" instead of "by an admin". While the console is open the
server's events scroll in as they happen: chat lines, joins and leaves, kicks,
bans, mutes, votes, the game's phases, reload results. Type any console command
(`help` lists them; `players`, `say`, `kick #3 camping`, `mute Bravo 30m spam`,
`map Guardian`, `nextmap`, `startvote playlist`, `reload`…). An empty line or Esc
goes back to the dashboard, and so do two minutes without typing, even while
events keep arriving (every question in the window gives the window back after
two minutes). A second window's console sends commands through the supervising
window but shows no events: those are in the supervising window. A line setting
the join password (`password …`) is masked in `logs/window.log`, `huragok.log` and
the relay's log. What players and servers send (names, chat, replies) is shown
with any control character as `?` and a tab as a space, here, on the dashboard and
in `window.log`, so a name can't drive your terminal.

From outside: `start.cmd rcon [-s server] [--by Alice] <command>`.

**If the server refuses Huragok's console password**, Huragok doesn't keep trying
it: five wrong passwords in ten minutes lock every tool on this machine out of
the server. It tries again as soon as the server's `data/<server>/instance.json`
names another password (the server writes it as it starts, so a new password in
`dedicated.toml` counts from the server's next start), once after the server
restarts, and otherwise every 10 minutes, so it can never be the one that locks the machine
out. When another tool on this machine caused a lockout, the log says so
(`… locked out after five wrong passwords from tools on it, right password or
not …`) rather than telling you to fix the password. Until it's back, Huragok
can't moderate that server or warn its players.

A Halo 3: ODST server plays only what its file names, so the console can't change its
game, teams or rounds. Huragok refuses `map`, `mode`, `load`, `maps`, `modes`,
`nextmap <game>`, `teamcount`, `shuffle`, `team`, `endround`, `endgame` and every
`startvote` but `startvote kick` itself, from the window, `start.cmd rcon`, the relay
and Vergil's in-game commands (`!map`, `!load`, …) alike, with the program's own words ("This server plays Halo 3: ODST: the
console cannot change its game, teams or rounds."). The program's guide says it
refuses them too, but 0.9.20 still takes `map`, `mode` and `load` in an ODST
server's lobby and queues a Halo 3 game as the next one. `status`, `players`,
`kick`, `ban`, `nextmap` on its own and the rest work as usual.

## Settings: D
**D** changes Huragok's own settings a step at a time, so nobody has to know a
file's format: each value is asked for on its own, with what's there now listed
first (and choices numbered), every change is checked before it's written, and the
file keeps its comments and layout. A server's own `dedicated.toml` is shown, never
changed: you edit it (**O** opens it), and D shows each setting with what it does
and the warnings about it. D lists Huragok's own files first, by letter, then the
servers, by their number on the dashboard; Esc goes back a step, and from the list
back to the dashboard. From a server's status card (V), D goes straight to that
server.

| | | |
| --- | --- | --- |
| **S** | `settings.toml` | how Huragok runs |
| **C** | `commands.toml` | roles and in-game commands |
| **A** | `automod.txt` | Vergil's word list |
| **N** | `names.txt` | Vergil's banned names |
| **I** | `intervalmessages.txt` | the interval messages every server says |
| **1, 2…** | each server | its `dedicated.toml` in groups (read-only), Huragok's own settings for it, the console's quick action, its bans and its own interval messages |

### How Huragok runs: S
`settings.toml` in fourteen groups, each named for whose it is (SUPT, VRGL, HRGK) with
its most telling values: which servers and who starts them (and whether a renamed
server is followed, `follow_renames`), how the servers are run (the backend's own
settings and Huragok's console connection), restarts, the watchdog, the keepalive,
the event log, the stats relay; automod and the moderators' console (relay);
Huragok's updates, the server program's, Workshop updates, new servers and SteamCMD,
and its folders and files. Each setting shows its value, `(default)` when the file
doesn't set it, and what it does. On/off settings and choices (the watchdog's `watch`,
`act`, `actnow`) are picked; roles (`exempt_roles`) are picked by number from the
whole list; for a typed value, `-` puts the default back
(or takes out a line whose absence means something, such as `mcc_folder`), `none`
empties a list, and Enter leaves it as it was.

`settings.toml` is read as Huragok starts, so what you change here takes effect at its next
start, and the servers keep running through that. The dashboard says so in red until then:
`settings.toml has changed since Huragok started: it takes effect when Huragok starts again
(the servers keep running)`. `commands.toml`, the word lists, the interval messages and
`discord.toml` apply within seconds.

**A change that would stop Huragok starting is never written.** It's made on a copy of
the file first, and the copy is read the way Huragok reads `settings.toml` as it
starts, every part's own check included (`check` does the same): `Not changed
([server_updates] repository = not a repository): server_updates.repository: 'not a
repository' should look like owner/name`. A problem the file had already doesn't
stop another change. Edits to `settings.toml` apply when Huragok starts again: close
the window (Q; the servers keep running) and start it again. A server's own Huragok
settings (autostart, its daily restart, priority, Vergil on it, whether it's restarted
for mod updates) are on that server's page instead (**H**), where they're in force at
once.

The moderators' console (relay) group also lists **the moderators** (M): add one
(name, password, role), change a role or a password, or take one out. Passwords are 8
to 128 characters, no two alike, and show only as `password set (19 characters)`.

### Roles and commands: C
`commands.toml`: the **faculty**, **moderators** and **members**, each listed with the
note beside them (who they are, who added them), and **every in-game command** with
the lowest role that may use it, its default, and whether it's off. Adding someone
lists the players Huragok has seen lately (from its records), numbered, so no player
ID has to be typed; a player ID can be typed too. One role each: adding a moderator
who's a member takes them out of the members (as `!promote` does). A command is moved
to a role by picking it, and switched off (nobody can use it) or on again; `!help`
can't be switched off. The prefix and the cooldown are here too. Admins aren't
here: they're in each server's `dedicated.toml` (`admins`, which you edit; D shows
them under Safety and admins) or added with B while it runs, and with
`[automod] across_servers` on, an admin on one server's list is an admin on every
server. Vergil reads the file again within seconds.

### Vergil's word list: A
`automod.txt`, one rule per line, shown as players meet it: its name, its words, and
what each strike does (`1st: muted for 1 day; 2nd: muted for 7 days; 3rd: kicked for
1 day; 4th: banned for good`). **Add a rule**: the words, then what each strike does,
from five ready-made ladders (kicked every time; muted 1 day, 3 days, then 7 days;
muted 1 day, 7 days, then out 1 day, then banned; banned the first time; only reports, a
ticket for the staff on Discord and nothing in game) or your
own, asked a part at a time (whether it mutes and how long each mute lasts, whether
muted for good comes and when, whether and when the kicks start and how long each
keeps them out, whether and when the ban comes), then its name. The line and
its ladder are shown before it's added, and a rule the word list can't take is
refused with why. A rule's words, ladder or name can be changed, or the rule taken
out. **Try a line of chat** says which rule catches what, disguised words included.
The notes at the top of the file stay as they are; see [automod.md](automod.md).

### Vergil's banned names: N
`names.txt`: each banned name with the note above it (when, by whom, why). Ban a name
(with a note), take one off, or **try a name**: whether a player using it would be
kicked as they join, by a banned name or by a word from the word list.

### Interval messages: I
`intervalmessages.txt` in Huragok's folder: the messages every server says, each with
how often (`every 30 minutes  Join us on Discord: discord.gg/yourserver`). **A** adds
one: pick how often (every 5, 10, 15, 30 or 45 minutes, 1 or 2 hours) or type
another time (`1h30m`, `90s`, `1d`; once a minute at most), then type the message as
players will read it. A number takes one off. A server's page has **I** too, for the
messages only that server says, with the ones for every server named above them.
Changes apply within a few seconds; a line in the file that can't be read is shown
with why, and left out until it's put right (O opens the file).

The file is one message per line, how often, a bar, then the message:

```text
30m | Join us on Discord: discord.gg/yourserver
1h  | Be nice: no slurs, no cheating. Vergil is listening.
```

- **Who reads them:** everyone on the server, as a `[Server]` line; a long message
  goes out as a few lines. A server says the ones for every server and its own.
- **When:** each one again and again after its own time, counted from when Huragok
  first sees the server up. Messages with the same time take turns: two every 30
  minutes go out 15 minutes apart, each every 30 minutes (a server's own take turns
  with the ones for every server). Messages due at the same moment go out one after
  the other, in the file's order.
- **Only while someone's on:** one that comes due while the server is empty, or down,
  waits for its next time, so nobody joins to a pile of them.
- **Edits** apply within a few seconds. A message that was there keeps its time; a new
  one (or one whose time or words changed) starts its clock then.
- `check` lists every server's messages, and any line it leaves out with why.

A server's own file is Huragok's, not the server's, though it sits in the server's
folder: being there shows which server it's for. A server without one (a new one, or
one copied in) simply has no messages of its own until I adds one (which writes the
file). There are no in-game commands for them yet.

### A server's settings
A server's page shows its `dedicated.toml` **read-only**: you edit the file, Huragok
shows and warns. Its settings come in groups, each with how many it holds, its most
telling values and how many warnings are about it, then the group's settings with
their values, where each comes from (this server, `[defaults]`, the file, a table, or
the program's own default) and what it does; a setting the server's program is too
old for isn't shown. **O** on any group opens the file in your editor.

1. **Joining**: join password, hidden, max/min players, ping limit, load timeout,
   welcome lines, enabled
2. **What it plays**: what its file names (a playlist, `playlists = [...]` by lobby
   size, one map and game type, a saved custom game, campaign co-op or Halo 3:
   ODST), shown only; **G** has the rest
3. **Gameplay**: friendly fire, team changing, the camera (0.9.20: `first_person`
   for everyone, the default, which also puts third-person character mods in first
   person; `player_choice`, each player switching with P; or `third_person` for
   everyone; multiplayer only), votes, trust player games
4. **Voice and chat**: the voice channels, text chat, chat log
5. **Safety and admins**: anti-cheat, the VPN block and its lists, admins for every
   server in the folder and this server's own (each named with who they are, where
   Huragok knows: the file keeps the IDs alone)
6. **Mods and the Workshop**: mod sharing and its upload limit, character mods,
   Workshop items, SteamCMD's program and account
7. **Updates**: auto_update, the server's own Workshop update checks
   (`update_check_minutes`)
8. **Listing and network**: host names, port, public address, UPnP, stats server
   (the relay's address while `[stats] relay` is on), the discovery master
9. **Remote console**: its password and address, this server's own port and password
10. **Folders and files**: game files, content, data, the ban list, live reload

Below the groups, **variables set where Huragok runs** that override the file: the
program reads a `RECLAIMER_DEDICATED_<SETTING>` variable over its `dedicated.toml`,
and so does Huragok, so each one is listed (`RECLAIMER_DEDICATED_MAX_PLAYERS = 12`)
and named beside the setting it wins over. That's why a server isn't doing what its
file says. Each server's process gets them as they are, as it would without
Huragok. With docker the containers' environment is compose's, and none are listed.

**H** is **Huragok's own for this server** (in `settings.toml`), and these are
changed here, a step at a time, in force at once: autostart, its own daily restart
time (a time, emptied for no daily restart on this server, or `-` to go back to the
`[restarts]` one), its processor priority, whether it's restarted for mod updates
(`workshop_updates`: `off` leaves it out of `[workshop_updates] restart`), and
**Vergil on this server**: `on`, `dry_run` (it only logs what it would do) or `off`
(it leaves this server's chat and names alone); `-` goes back to `[automod]`'s.
Mutes already decided still follow players here, as `[automod] across_servers`
(D → S → Vergil) says. The top line shows each server's (`VRGL … automod (on:
Alpha; dry run: Bravo)`), and so does V (Safety → Vergil). See
[automod.md](automod.md#vergil-on-each-server).

A change you make to the file is read by the server a couple of seconds after you
save it (Project Reclaimer 0.9.18 and later, `auto_reload = true`), so no restart is
needed for most settings. When each applies:

| Applies | Settings |
| --- | --- |
| at once | join password, max/min players, max ping, load timeout, votes, voice (global, team, proximity; admins also switch these in game with `!toggleglobal`, `!toggleteam`, `!toggleproxy`, for a while or for good, and all voice off for 10 minutes with `!togglevoice`), text chat, welcome lines, mod sharing, host name, campaign's trust player games, the folder's admins |
| from the next game | friendly fire, team changing, the camera (a campaign or ODST server restarts once empty for friendly fire) |
| from the next vote | the playlist, playlists by lobby size |
| the server restarts by itself once empty | hidden, port, anti-cheat, VPN block, chat log, character mods, enabled, stats server, UPnP, the console password and address, anything about a campaign or ODST server's game |
| when `dedicated` restarts | auto_update, the Workshop update checks (`update_check_minutes`), SteamCMD's program and account, the VPN lists, public address, the discovery master, the game, content and data folders |

For a setting that applies when `dedicated` restarts, restart the server when it
suits you (**R**, or `start.cmd restart --after-game "<server>"`).

The dashboard's events show the server's answer: `settings reloaded: max_players
16 → 12`, or `settings NOT reloaded, nothing changed` with what's wrong (which then
stays in red on the server's line until a reload goes through, and is a Refused
warning on G and D). A program that meets a setting it doesn't know won't start
at all, and Huragok says why: when its program is older than the newest release,
put the newest program in its folder by hand or take that line out (its own
auto_update can't run before it has read its settings); when it's the newest
already, the line is a mistake. Three things change live over the console instead
of the file, until a restart: `servername`, `password` (join) and `maxping`.

Below that, by letter, what else the page does:
- **O** opens its `dedicated.toml` in your editor;
- **Q** adds the console, shown on a server without an `[rcon]` password: a random
  24-character `password` under `[rcon]`, and an `rcon_port` of its own only when
  its game port's number is taken on this PC. It's shown line by line before it's
  written, and written only on yes, with the file's version kept first (in
  `records/settings/`). The server uses it from its next start;
- **B** its bans, admins and the VPN block: as B, for this server;
- **I** its own interval messages, with the ones for every server named above them;
- **N** and **C**, below.

Two things on D need the server stopped (X), and say so otherwise (a server
waiting to be started again after a crash counts as running: "the Superintendent
is about to restart it (backoff): stop it first (X)", and so does another server
running from the same folder). Each is done only when you ask:
- **N rename this server**: its name everywhere it's used: its `[[server]]` block,
  its block in `settings.toml`, its data folder (logs, chat, status), Huragok's
  record of it and its console log, and its folder in `SERVERS/` when that's named
  after it. The folder is renamed first, and if any later step fails every step is
  put back ("Every step was put back: it's still Alpha"). A new name that would
  give the same file name as another server's ("LG Slayer" beside "LG-Slayer") is
  refused. A name you change by hand in the file is followed anyway
  (`follow_renames`), but leaves the program's own data folder behind;
- **C its own content folder** (shown when it shares one): a copy of the shared
  content folder in its own folder, hard-linked on the same drive so no space is
  used, and its `dedicated.toml` pointed at it. What Huragok writes into it later
  (imports, Forge layouts, copies) replaces the file rather than writing through
  the link, so the shared copy never changes with it.

## Open: O
**O** opens Huragok's folder (the first choice), or one of its files in your editor
(Notepad on Windows): `settings.toml`, `commands.toml`, `automod.txt`, `names.txt`,
`intervalmessages.txt`, or a server's: its folder first, then its `dedicated.toml`, its
own `intervalmessages.txt`, playlists, ban list, `content/disabled-mods.json` when
there is one, and its saved game types and `.rmeg` modes. That's how a server is
changed: Huragok shows its files and warns, and you edit them (D says so too when it
can't read one).

## Bans, admins and the VPN block: B
**B** on a running server: the ban list (`bans`), `unban`, a ban of someone who
isn't connected (player ID, address or range, with a time and a reason), `pardon`
(let a kicked player back in now), the admins (`admin list`, `admin add`, `admin
remove`, no restart), and the VPN block (`vpn`, `vpn <address>`, `vpnallow`,
`vpnrevoke`). Everything goes through the console with your name on it, so a ban,
an unban and an admin added or removed here reach every server, as Huragok's own
(`[automod] across_servers`). Every server keeps a ban list of its own, and with
`across_servers` on every ban on one goes on the others, an in-game `/ban` too
(one from before 0.7.0 at Huragok's next look, every 10 minutes). **E** puts one of
this server's bans on every server's at once (on Huragok's list, so a new server
gets it too), by the player ID, address or name the list shows: what `!banall`
does in game. On a stopped server
it shows the ban list from its folder (`dedicated bans`).

## Updates: U
**U** offers four things:

1. **Huragok** itself, from its GitHub releases: the servers keep running
   while it updates. While a new version waits, the dashboard's HURAGOK line says
   `Huragok <version> is out (U)`; with `[updates] auto_update = true` it installs
   itself as soon as it's found instead (off unless you turn it on). Only full releases
   are offered unless `[updates] beta = true` (also off unless you turn it on): then
   Huragok's beta releases too, the line saying `Huragok <version> beta is out (U)`, and
   a full release passes the betas before it. Turning beta off keeps the beta you have
   until a full release newer than it is out. The install is renames only, all or nothing (if a file can't
   be moved, say an antivirus is scanning the new copy, everything is put back),
   and checked before it counts; if the new version's Python packages don't
   install, the old files go back too. The replaced files go to
   `records/previous-<the version that was installed>/`; a backup of that version
   already there is kept beside it as `previous-<version>-<date and time>`, never
   deleted, and `records/last-update.json` says which backup the update made.
   Huragok then starts again on the new version once no server is part way through
   a restart, a start or a countdown, or has a restart planned or a job waiting
   (looked at every 2 s; after 30 minutes it goes ahead anyway, saying which servers
   were still busy). If the new version won't start (it ends within 2 minutes), its
   launcher puts the backup back, starts that version, and leaves a note
   (`records/failed-update.json`); a version that failed isn't installed again by
   itself at server restarts: **U**, `start.cmd update` or a newer release tries
   again. When GitHub can't be reached, U says so ("couldn't ask GitHub whether
   there's a newer Huragok: …").
2. **The server program** (Project Reclaimer): each server's version, its
   `auto_update`, the newest release, and the warnings about releases. Nothing is
   installed here: **each server installs its own releases**, and Huragok never
   downloads a program or puts one in place. Players must be on exactly the
   server's version, so a server left behind empties out. Each server's own
   `dedicated.toml` is the one switch (the dashboard header says `server program
   0.10.8 · 2 of 3 update themselves`):
   - **`auto_update = true`: the server updates itself.** It looks at the releases
     as it starts and every hour, and installs a new one by itself. Its own updater
     doesn't wait for the game to end: it stops the server at once, mid-game, and
     it's back in its lobby within seconds (17 s on 0.10.7). So Huragok:
     - **looks too, every 15 minutes** (`[server_updates] check_minutes`), and once
       a release is out, **restarts each self-updating server after its game**
       (at once when nobody is on), so it installs the release as it starts instead
       of having its game cut at its own hourly look. During the game players read,
       once, `[o_o] PARDON OUR DUST! Project Reclaimer <version> is out. This server
       restarts for it after this game, so the update lands sooner. Update your game
       to rejoin.`, then the usual after-game warning. The same happens to the other
       self-updating servers when one of them updates itself first.
       `[server_updates] follow = false` turns these restarts off: Huragok then never
       restarts a server for a release;
     - **tells players when the server's own updater is about to cut their game**: on
       `[update] downloading Project Reclaimer <version>` (about 8 s before it stops)
       they read `[-.-] PARDON OUR DUST! This server is updating to Project
       Reclaimer <version> and restarts in a few seconds. Update your game to
       rejoin.`, so they know it isn't a crash. The log says `PARDON OUR DUST!
       Project Reclaimer installed a new release by itself`, with the game it
       stopped and how far in, then `now on Project Reclaimer <version>`. The
       watchdog doesn't count it as a crash;
     - **moves it off its old program file**: after a self-update the program's
       supervisor still runs from the old, locked `project-reclaimer.exe.old`,
       which the next update might not be able to replace, so once nobody is on
       Huragok restarts the server onto the new file and deletes the old one. A
       server that doesn't empty for an hour is warned about (R restarts it when it
       suits you);
     - **warns** when a server should have updated and didn't: still on the old
       version 70 minutes after the release (or at once when it says its own update
       failed), with what its console said. A restart makes it look again as it
       starts.

     A server Huragok didn't start (found running) is restarted for a release only
     through its own console's `restart`. On Docker the image is the update, so
     Huragok only warns ("a new image is out") and restarts nothing.
   - **`auto_update = false`: pinned on purpose.** The server stays on its version.
     Huragok says so once, calmly, as a note on G and its card ("Pinned to Project
     Reclaimer 0.9.23 by its own file"), and nothing more.
   - **A program older than its `dedicated.toml`** can't start at all: it reads its
     settings before it looks for releases, and stops at one it doesn't know. Huragok
     asks the program its version (`--version`) and says what to do: put the newest
     program in its folder by hand (the release page is named), or take that line
     out; when it's the newest already, the line is a mistake.

   `start.cmd update --program` shows the same from a terminal: the newest release
   and where each server stands. Before 0.7.0 Huragok installed releases itself
   (`[server_updates] install`, `fallback`, `update --program --now`): the update
   takes those settings out of `settings.toml` (a value you'd set stays as a comment
   saying where it went), and they do nothing.
3. **Workshop mods**: every item's installed version and Steam's newest, what each
   server is doing about it (downloading, waiting for a restart, installed), and the
   warnings. When a downloaded update waits for a restart (its map is in use), **R**
   restarts that server's game now to put it in: players are told, then dropped, and
   join again (see below).
4. **Clean up**: what updates and mods leave behind, each with its size, numbered to
   pick from, in this order: program files a self-update set aside
   (`project-reclaimer.exe.old`) and content folders set aside when a server got its
   own, in each folder; the older sets in `records/previous-program/` (program copies
   Huragok replaced before 0.7.0; the newest stays); older Huragok versions in
   `records/previous-*` (the newest by version number stays, 0.2.10 after 0.2.9, and
   so does the one the last update made); a mod's old copy an update from before
   0.7.0 left (only when the live copy is whole: the old one may otherwise be the only
   one left) or a partial new copy it set aside; and SteamCMD's copies of mods no
   server lists any more. Chat logs and settings are never offered. A file still in
   use is kept and says why.

## Workshop mods: the servers update them, Huragok restarts
Each server keeps the Workshop items its `dedicated.toml` lists (`[workshop] items`)
up to date by itself: it looks 6 s after it starts and then every
`update_check_minutes` (in its `[workshop]` table: 5 at least, 60 unless set, 0
off), downloads a new version during a game, and installs it between games. A server
whose game doesn't use the mod installs it at once, even with players on. Huragok
writes nothing into the server's files for this (not `items`, not
`update_check_minutes`) and runs no SteamCMD of its own. It reads each mod's version
the way the program does (`reclaimer-installed.json`).

**Except over a mod's map in use.** A server's game keeps a mod's map open from the
first time it plays it, so on a server that plays the mod's map (every HugeGrass
server, say) the install fails every 5 seconds, for good, with players on or not:
`installing the update failed (cannot move … aside: Access is denied …)`. A restart
of its game is all it takes: the program's supervisor keeps the download and puts it
in in the second before the new game opens the map. So with `[workshop_updates]
restart = true` (the default), that line sets Huragok off:

- **Nobody on:** it restarts the server's game at once (the console's `restart`).
  Back in its lobby within seconds, and not counted as a crash.
- **Players on:** the line comes as the game ends, since the server only tries
  between games. Players read `[-.-] PARDON OUR DUST! Restarting for a mod update:
  the new version of <mod> goes in now. Join again in a minute.`, and about 3 s
  later it's `restart now`. They're dropped and rejoin as they first joined: there's
  no waiting room while a server restarts.
- It waits for `updated to the version … between games`. If the install still
  fails, it restarts once more, then only warns (`<mod>: 2 restart(s) didn't put the
  new version in…`): something else may hold the map open. A stop and start doesn't
  help (a cold start opens the map before the update goes in), so R on **U →
  Workshop mods** restarts its game again once that's closed.

`restart = false` leaves it to you: Huragok only warns that the update is waiting.
One server can differ: `workshop_updates = "off"` in its `[[server]]` block in
`settings.toml` (D → the server → H).

**Servers sharing a content folder** (one folder with several servers, or folders
whose `content` names the same path) can't install while any of them has the map
open, only one of them downloads an update at a time, and restarting only one fails.
Huragok handles them as one group:

1. Nobody on any of them: their games are restarted at the same moment (one
   install, all back within seconds).
2. Otherwise: the ones with nobody on are stopped, so nobody joins them meanwhile;
   each busy one is stopped as its game ends (its players read `Stopping for a mod
   update: this server is back once the new version of <mod> is in. Join again in a
   few minutes.`). When only one is left up and it's the one holding the download,
   it's restarted instead, and the others start already up to date. Otherwise, once
   the last is down, they're all started, and restarted again once one of them says
   a new version was downloaded.

With no limit on how long a game runs, the stopped ones stay off for as long as the
busiest game runs (hours, for a Forge session): the window says which server's game
holds the group up and for how long, so you can step in (end the game early with its
console's `endgame`). One server in the group with `workshop_updates = "off"` holds
the whole group: Huragok only warns, naming it. A server in the group that Huragok
didn't start is never stopped: the group's update then goes in once nobody is on any
of them, when they're restarted together.

**Warnings** say the rest, on G, D, the card and in `huragok.log`: a mod with a new
version on Steam that the server is still downloading (players who already have the
new version can't join until the server has it too: their game says "this PC's copy
is a different version from the host's"), an update waiting and why, a download kept
beside the installed copy (every Workshop mod is kept twice per server folder) with
its size, and a leftover `.workshop-*` folder (1 GB or so) from a server that was
ended or crashed rather than stopped cleanly, to delete while no server on that
content folder is updating. On Discord, the info channel gets `Mod update installed:
<mod> <old date> → <new date>` once it's in.

When SteamCMD's sign-in has expired, the server's line says so in red: press **I**
to sign in again. A server whose `[workshop] steamcmd` isn't on this PC (one copied
in from another PC) can't fetch its items at all: that's a warning, and **I → L** or
`start.cmd steam lines "<server>"` writes this PC's SteamCMD lines into it, shown
first.

## Mods Huragok finds: warned about
The server program only keeps up to date the items its `[workshop] items` list.
Huragok looks at every server's mods and warns, changing nothing:

- **A hand copy that names a Workshop item** (its `ModInfo.json` does, as every
  Workshop upload's does) but isn't in `items`: nothing ever updates it. Add the item
  to `items` and take the copy out: the server fetches and updates listed mods itself
  (its own check does it too).
- **The program's own copy of an item taken out of `items`** (it stays in
  `content/Mods/Workshop/<item>`): the server still loads it and never updates it.
  Add the item back, or move the folder out if it isn't wanted.
- **Mods found only in this PC's own Steam Workshop folder**: Steam updates or
  removes those on its own schedule; to keep one, list its item in `items`.

An item you add to `items` is fetched by the server itself: a running server a couple
of seconds after the file changes (auto_reload), a stopped one as it starts, or now
with **G → 4 → I** (`start.cmd workshop -s "<server>" install`, which runs
`dedicated mods`). A mod's own Forge layouts (its `Project Reclaimer Layouts`
folder) aren't read by the server: **G → 4 → L** copies them into `content/Maps/<mod>/`
when you ask.

Before 0.7.0 Huragok took over hand copies, listed the program's own copies again,
switched mods off in `disabled-mods.json` and swapped new versions in itself after
the game (`[workshop_updates] install`, `adopt`, `layouts`, `check_minutes`). The
update takes those settings out of `settings.toml` (a value you'd set stays as a
comment), and adds `restart`: `false` where you had `install = "off"`. What Huragok
had written into a server's files stays, listed once on the server (X dismisses it).

## Is each mod good to go?
The server program says little about a mod, and only once a game or a start reaches
the problem: a mod made for another game in MCC is ignored without a word, a map
whose description doesn't name it is skipped at every start, two maps with one map
ID can't share a session. Huragok reads every server's mods itself and says, for
each, **good to go** or what's wrong:

- **broken**: the server won't use the mod, or one of its maps. A map no description
  names (its `multiplayer/*.json` `ScenarioFile` must end in the map's file name), a
  map file the server can't read, a mod for another game, a Workshop item missing
  files.
- **warning**: trouble in some cases, or something for you to do. An old copy of a
  map left beside the new one (the server skips it at every start; deleting it frees
  the space), two maps with one map ID, a Workshop item copied in by hand (it can't
  be updated), a Forge layout made on a map that isn't installed, a mod too big for
  players to download from your server, character mods without all 27 of Halo 3's
  multiplayer maps in the game folder.
- **note**: worth knowing, nothing to fix (one map file name in two mods).

Where you see it: **G**, the server, **4** (Mods) lists every mod with its verdict;
`check` has a Mods section; and the log says each mod's verdict as Huragok starts,
then again whenever it changes (it looks every 10 minutes and just after a Workshop
item installs). While a server runs, what it says about mods it couldn't use
(characters left out, a mod package it can't share, too many mod maps for one
session, Forge objects left out…) is logged once a start with a `why:` line, and a
Workshop download or install that fails says what to do.

## Stats and the relay switch
Leaderboards, `/stats`, `!stats` and the kill feed come from Huragok's **stats
relay** (not the moderators' console below). After each finished game a server
reports it to Project Reclaimer's stats service (its `stats_server`). While
`[stats] relay = true` (the default) and Huragok runs, each server's `stats_server`
points at Huragok on this PC (from the server's next start), which passes every
report on to the real service unchanged, passes the answer back the same way, and
keeps a copy. Players' ranks are recorded as before. When Huragok stops, every
server's `stats_server` goes back to what it was (`records/temp-stats-capture.json`
keeps it), so a server restarted meanwhile reports straight to the service; Huragok
points it at itself again as it starts. If the service ever turns down two reports
in a row that came through Huragok, Huragok puts that server's `stats_server` back
for good: players' ranks come first. A server set to report nowhere (`"off"`) isn't
touched.

With `relay = false`, every server's own `stats_server` goes back as it was at once,
and Huragok never touches it again. Leaderboards and the kill feed then get nothing
new, and every server says so (a note on G, D and its card). It's the only line of a
server's `dedicated.toml` that Huragok changes by itself, and only while this is on.

## A console for moderators elsewhere: the relay
Moderators who aren't at this PC normally use the official RCON client against
the server's own console: one shared password, one of the 4 spots each, and five
wrong passwords lock an address out of the server.

The relay is Vergil's. With `[relay] enabled = true` (the section kept its name)
Huragok listens itself, at each server's game port plus `port_offset` (49176 →
50176), speaking the server's own WebSocket console protocol. So the **unchanged
official RCON client** (host `127.0.0.1` through an SSH tunnel or VPN, port 50176)
and any tool that speaks that protocol connect to Huragok instead, and:

- each moderator has **their own password** (`[[relay.user]]` with `name`,
  `password`, `role`): the password is what tells them apart, so no two alike;
- only the commands **their role allows** pass, from the same lists as
  `commands.toml` (a moderator can `mute` and `kick`, not `ban`; read-only
  commands such as `status`, `players`, `maps`, `bans` pass for every moderator;
  `admin`, `reload` and setting the join `password` need faculty, while `admin` or
  `password` typed alone only ask), and the reply says so otherwise. A word that isn't a
  command at all says that instead (`'foo' isn't a console command: help lists them.`). A
  command in `[off]` passes for nobody, faculty included;
- nobody acts on their own role or above, as in game (their highest role on any
  server): `kick`, `ban`, `banip`,
  `mute` and `startvote kick` on a player named by `#number`, player ID or exact
  name are refused for faculty, and for an admin or moderator when the user is
  one too or below ("Big Boss is faculty; you can't kick them…"). So is an address
  or a range that someone of that role is on now. A name quoted any way but the plain one
  (`"Master Chief"`) is refused, as what's checked has to be what the server acts on. The
  server's console acts with the host's authority and would do it, so the relay holds
  the line; faculty can act on anyone;
- one command per message, on one line: a line with a new line, a tab or another
  control character in it is refused, and so is a blank word in the protocol's
  `args` (a command sent with words is never taken for a question: before, an
  empty word let a moderator clear the faculty-only join password);
- the server reads **who acted**: players see "kicked by Alice", the server log
  and Huragok's log record it;
- events reach every signed-in moderator; players' addresses and device
  fingerprints only reach admins, in events and replies alike, on both protocols
  (decision: below admin, the relay's `status` hides the server's own address too,
  and IP addresses typed in chat are hidden as well);
- **one** upstream spot serves everyone, and a wrong password locks nobody out
  of the server (five in ten minutes lock that address out of the relay);
- the dashboard header shows who's signed in, on Vergil's line (`VRGL … consoles:
  Alice (Server 1)`).

The Discord bot's typed console sends its lines through these same checks, at the rank the
Discord role gives: [discord.md](discord.md).

Adding or removing a moderator is adding or deleting their block in
`settings.toml`, which is read as Huragok starts: close it (**Q**) and start it
again. The servers keep running meanwhile; no server restarts.

It isn't encrypted (neither is the server's console): keep `address =
"127.0.0.1"` and give moderators an SSH tunnel (`ssh -L 50176:127.0.0.1:50176
you@host`) or a VPN.

**Source RCON tools** (the protocol of Valve's games, which many panels and
libraries speak) use the same passwords at the game port plus
`source_port_offset` (49176 → 51176; `0` switches it off). Sign in with the
moderator's password, send a command, read the text; a refused password answers
with id -1 as Source servers do, and the end-of-reply marker clients send is
echoed back. Source RCON has no events.

## The Discord bot: J
**J** sets up the Discord bot a step at a time: webhook only or your own bot, Discord roles onto
Huragok's ranks, channels, features, messages, tickets, the typed console, reports, appeals and
Contact staff, a check and a test post. `/huragok setup` in Discord does the same with Discord's
own pickers, and both write `discord.toml`, which Huragok reads again within seconds of it changing.
It's off until you set it up. [discord.md](discord.md) goes from nothing to a bot that answers.

## What a server plays next
The dashboard's GAME column shows the game under way and, when one is set, the
next one (`playing Guardian Slayer → next Valhalla Slayer`, from a `nextmap`
override), or `(voting)` while a playlist ballot is open. A stopped server shows
what it's set to play (`plays playlist infection`).
